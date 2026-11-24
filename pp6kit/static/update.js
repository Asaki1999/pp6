/* 版本与自助升级：关于页显示真实构建指纹，一条路走完「检查 → 下载 → 重启」。
 *
 * 后端三个接口：
 *   GET  /api/version        当前版本/提交/构建时间
 *   GET  /api/update/check   最新 Release（?force=1 绕过缓存）
 *   POST /api/update/stream  SSE：下载进度 + 替换结果
 *
 * 自动检查一天只发一次请求（服务端还有 24 小时缓存）；有新版本才亮小圆点，
 * 不打断正在做的事。真正替换前必须再点一次「确认升级」。
 */
(function () {
  const $ = (id) => document.getElementById(id);
  const state = { info: null, latest: null, busy: false };

  const fmtTime = (iso) => {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  };

  const channelLabel = (ch) => ({
    dev: "开发版", local: "本地构建", nightly: "每日构建", release: "",
  }[ch] ?? "");

  function setState(text, kind) {
    const el = $("updateState");
    if (!el) return;
    el.textContent = text;
    if (kind) el.dataset.kind = kind; else delete el.dataset.kind;
  }

  function setBar(ratio, got, total) {
    const box = $("updateProgress");
    const bar = $("updateBar");
    const pct = $("updatePct");
    if (!box || !bar || !pct) return;
    box.hidden = false;
    if (ratio === null || ratio === undefined) {
      bar.style.width = "100%";
      bar.classList.add("indeterminate");
      pct.textContent = got ? `${Math.round(got / 1048576)}MB` : "…";
      return;
    }
    bar.classList.remove("indeterminate");
    const p = Math.max(0, Math.min(100, Math.round(ratio * 100)));
    bar.style.width = p + "%";
    pct.textContent = total ? `${p}% · ${Math.round(got / 1048576)}/${Math.round(total / 1048576)}MB`
                            : `${p}%`;
  }

  function showDots(on) {
    ["updateDot", "tabUpdateDot"].forEach((id) => {
      const el = $(id);
      if (el) el.hidden = !on;
    });
  }

  function showLink(url) {
    const link = $("updateLink");
    if (!link) return;
    link.hidden = !url;
    if (url) link.href = url;
  }

  function resetButtons() {
    state.busy = false;
    const check = $("btnCheckUpdate");
    const go = $("btnDoUpdate");
    if (check) check.disabled = false;
    if (go) go.disabled = false;
  }

  /* ---------------- 版本 ---------------- */
  async function loadVersion() {
    try {
      const res = await fetch("/api/version");
      const info = await res.json();
      state.info = info;
      const ver = $("aboutVersion");
      const build = $("aboutBuild");
      if (ver) {
        const tag = channelLabel(info.channel);
        ver.textContent = info.version + (tag ? ` · ${tag}` : "");
      }
      if (build) {
        build.textContent = fmtTime(info.builtAt) + (info.commit ? ` · ${info.commit}` : "");
      }
    } catch {
      const ver = $("aboutVersion");
      if (ver) ver.textContent = "读取失败";
    }
  }

  /* ---------------- 检查 ---------------- */
  function apply(data) {
    state.latest = data;
    const box = $("updateNotesBox");
    const go = $("btnDoUpdate");
    // 有新版或检查失败时才给「发布页」出口；已经最新就不必打扰
    const latestUrl = (data.latest && data.latest.url) || "";
    const fallback = state.info && state.info.repo
      ? `https://github.com/${state.info.repo}/releases` : "";
    showLink((data.hasUpdate && latestUrl) || (!data.ok && fallback) || "");
    if (!data.ok) {
      setState(data.error || "检查更新失败", "error");
      if (go) go.hidden = true;
      showDots(false);
      return;
    }
    const latestVersion = (data.latest && data.latest.version) || "";
    if (data.hasUpdate) {
      setState(`有新版本 ${latestVersion}`, "warn");
      if (go) go.hidden = false;
      showDots(true);
      // 每个版本只提醒一次：不打断正在做的事，也别忘了往哪升级
      try {
        const key = "pp6.update.seen";
        if (localStorage.getItem(key) !== latestVersion) {
          localStorage.setItem(key, latestVersion);
          if (typeof toast === "function") {
            toast(`有新版本 ${latestVersion}，可在「系统设置 › 关于」升级`);
          }
        }
      } catch { /* 隐私模式忽略 */ }
      const notes = (data.latest && data.latest.notes) || "";
      if (box) {
        box.hidden = !notes;
        const pre = $("updateNotes");
        if (pre) pre.textContent = notes || "这个版本没有写更新说明。";
      }
    } else {
      setState(latestVersion ? `已是最新版本 ${latestVersion}` : "已是最新版本", "ok");
      if (go) go.hidden = true;
      showDots(false);
      if (box) box.hidden = true;
    }
  }

  async function check(force) {
    if (state.busy) return;
    const btn = $("btnCheckUpdate");
    if (btn && force) btn.disabled = true;
    setState("正在检查更新…");
    try {
      const res = await fetch("/api/update/check" + (force ? "?force=1" : ""));
      apply(await res.json());
    } catch {
      setState("检查失败，请稍后再试", "error");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  /* ---------------- 升级（SSE 进度） ---------------- */
  function handleDone(result) {
    const mode = (result && result.mode) || "";
    $("updateConfirm").hidden = true;
    if (mode === "relaunch") {
      setState("正在替换并重启，稍后可用", "ok");
      setBar(1, 0, 0);
      showDots(false);
      return;
    }
    $("updateProgress").hidden = true;
    resetButtons();
    if (mode === "uptodate") {
      setState((result && result.message) || "已经是最新版本", "ok");
      showDots(false);
      return;
    }
    // manual：包已经下好，但没法自动替换——把位置说清楚
    setState((result && result.message) || "请手动替换", "warn");
    const path = $("updatePath");
    if (path) {
      path.hidden = !(result && result.path);
      path.textContent = (result && result.path) || "";
    }
  }

  async function upgrade() {
    if (state.busy) return;
    state.busy = true;
    $("btnDoUpdate").hidden = true;
    $("btnCheckUpdate").disabled = true;
    $("updateConfirm").hidden = true;
    $("updateProgress").hidden = false;
    setBar(0, 0, 0);
    setState("正在准备升级…");

    try {
      const res = await fetch("/api/update/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      if (!res.ok || !res.body) throw new Error("stream-unavailable");

      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      let finished = false;
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const frames = buf.split("\n\n");
        buf = frames.pop();
        for (const frame of frames) {
          const line = frame.split("\n").find((l) => l.startsWith("data:"));
          if (!line) continue;
          let ev;
          try { ev = JSON.parse(line.slice(5).trim()); } catch { continue; }
          if (ev.stage === "status") setState(ev.text || "处理中…");
          else if (ev.stage === "progress") setBar(ev.total ? ev.got / ev.total : null, ev.got, ev.total);
          else if (ev.stage === "done") { finished = true; handleDone(ev.result); }
          else if (ev.stage === "error") throw new Error(ev.error || "升级失败");
        }
      }
      if (!finished) throw new Error("升级中断，请重试");
    } catch (err) {
      $("updateProgress").hidden = true;
      setState(err.message === "stream-unavailable" ? "当前界面不支持自动升级" : (err.message || "升级失败"), "error");
      resetButtons();
      $("btnDoUpdate").hidden = false;
    }
  }

  /* ---------------- 绑定 ---------------- */
  function init() {
    if (!$("updateBox")) return;
    loadVersion();
    $("btnCheckUpdate").onclick = () => check(true);
    $("btnDoUpdate").onclick = () => {
      $("updateConfirm").hidden = false;
      const path = $("updatePath");
      if (path) path.hidden = true;
    };
    $("btnCancelUpdate").onclick = () => { $("updateConfirm").hidden = true; };
    $("btnConfirmUpdate").onclick = () => upgrade();

    // 进入设置页时刷新一次版本（打包后第一次打开就能看到真实版本）
    document.querySelectorAll('.mode[data-mode="settings"]').forEach((btn) => {
      btn.addEventListener("click", () => setTimeout(loadVersion, 0));
    });

    // 自动检查：一天一次，静默进行；有新版本只亮圆点，不打断
    const KEY = "pp6.update.autoAt";
    let last = 0;
    try { last = Number(localStorage.getItem(KEY) || 0); } catch { last = 0; }
    if (Date.now() - last > 6 * 3600 * 1000) {
      setTimeout(() => {
        check(false);
        try { localStorage.setItem(KEY, String(Date.now())); } catch { /* 隐私模式忽略 */ }
      }, 2500);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
