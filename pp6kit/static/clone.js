/* ==========================================================================
   克隆现有对象（v7，2026-11-27）
   设计稿：out/界面设计-v7/对象克隆/（说明见同目录落地说明.md）

   职责：
     ① 首屏第三张入口卡（#entryClone）的选文件 / 拖入
     ② 克隆设置弹窗（#pcMask）：解析 zip 对象清单 → 单选 → 命名 → 提交
     ③ 克隆态预览页的对话词条与提示条（对象信息条由 app.js 的 renderAiBo 出）

   字段定义由服务端从 zip 程序化复制，这里只决定「新对象叫什么、编码是什么」。
   ========================================================================== */

(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

  // 与服务端 clone_apply 的同一条规则：小写字母开头，2–40 位
  const CODE_RE = /^[a-z][a-z0-9_]{1,39}$/;

  let zipFile = null;
  let objects = [];            // [{code,name,tbl,fields,flow,suggestedName,suggestedCode}]
  let picked = "";
  let codePool = null;         // 撞库用的编码集合（null = 还没拉）
  let busy = false;

  /* ---------------- 撞库 / 编码校验 ---------------- */

  async function knownCodes() {
    if (codePool) return codePool;
    try {
      const res = await fetch("/api/objects", { cache: "no-store" });
      const d = await res.json();
      codePool = new Set((d.objects || []).map((o) => String(o.code || "").toLowerCase()));
    } catch (_) {
      codePool = new Set();     // 拉不到就不做即时撞库，提交时服务端仍会拦
    }
    return codePool;
  }

  function checkCode() {
    const el = $("#pcCode");
    const tip = $("#pcCodeCheck");
    if (!el || !tip) return false;
    const v = el.value.trim();
    if (!v) {
      el.classList.remove("bad");
      tip.className = "";
      tip.textContent = "";
      return false;
    }
    if (!CODE_RE.test(v)) {
      el.classList.add("bad");
      tip.className = "bad";
      tip.textContent = "编码不合法：小写字母开头，只用小写字母、数字、下划线（2–40 位）";
      return false;
    }
    el.classList.remove("bad");
    tip.className = "ok";
    tip.textContent = "✓ 编码格式可用";
    return true;
  }

  async function checkCodeTaken() {
    const el = $("#pcCode");
    const tip = $("#pcCodeCheck");
    if (!el || !tip) return;
    const v = el.value.trim();
    if (!CODE_RE.test(v)) return;
    const pool = await knownCodes();
    if (pool.has(v.toLowerCase())) {
      el.classList.add("bad");
      tip.className = "bad";
      tip.textContent = `编码 ${v} 已被系统里的对象占用，换一个`;
      return;
    }
    if (!el.classList.contains("bad")) {
      tip.className = "ok";
      tip.textContent = "✓ 编码可用";
    }
  }

  /* ---------------- 弹窗 ---------------- */

  function renderObjects() {
    const box = $("#pcObjects");
    if (!box) return;
    box.innerHTML = objects.map((o) => `
      <label class="pc-obj ${o.code === picked ? "on" : ""}">
        <input type="radio" name="pcObj" value="${esc(o.code)}" ${o.code === picked ? "checked" : ""}>
        <span class="pc-obj-name">${esc(o.name)}</span>
        <span class="pc-obj-code">${esc(o.code)}</span>
        ${o.flow ? '<span class="chip flow-chip" title="流程对象：流程冗余字段照带；流程图不克隆，去「新建流程配置」重画">流程</span>' : ""}
        <span class="pc-obj-count">${Number(o.fields || 0)} 个字段</span>
      </label>`).join("");

    box.querySelectorAll("input[name='pcObj']").forEach((radio) => {
      radio.onchange = () => {
        picked = radio.value;
        box.querySelectorAll(".pc-obj").forEach((el) => {
          el.classList.toggle("on", el.querySelector("input").checked);
        });
        prefill();
      };
    });
  }

  function prefill() {
    const o = objects.find((x) => x.code === picked) || {};
    const nameEl = $("#pcName");
    const codeEl = $("#pcCode");
    if (nameEl) nameEl.value = o.suggestedName || (o.name ? `${o.name}副本` : "");
    if (codeEl) codeEl.value = o.suggestedCode || (o.code ? `${o.code}_copy` : "");
    checkCode();
    checkCodeTaken();
    if (nameEl) nameEl.focus();
  }

  function setStatus(text, bad) {
    const box = $("#pcStatus");
    if (!box) return;
    box.hidden = !text;
    box.textContent = text || "";
    box.className = "pc-status" + (bad ? " bad" : "");
  }

  function openModal() {
    const mask = $("#pcMask");
    if (!mask) return;
    mask.hidden = false;
    document.body.classList.add("pl-modal-open");
  }

  function closeModal() {
    const mask = $("#pcMask");
    if (mask) mask.hidden = true;
    document.body.classList.remove("pl-modal-open");
    setStatus("");
  }

  async function openClone(zip) {
    zipFile = zip;
    objects = [];
    picked = "";
    const list = $("#pcObjects");
    if (list) list.innerHTML = `<div class="pc-loading">正在读取 ${esc(zip.name)}…</div>`;
    setStatus("");
    openModal();
    try {
      const buf = await zip.arrayBuffer();
      const res = await fetch("/api/clone/inspect", {
        method: "POST", body: buf,
        headers: { "Content-Type": "application/octet-stream" },
      });
      const d = await res.json().catch(() => ({}));
      if (!res.ok || d.ok === false) throw new Error(d.error || `读取失败 ${res.status}`);
      objects = d.objects || [];
      if (!objects.length) throw new Error("zip 里没有可克隆的业务对象");
      picked = objects[0].code;
      renderObjects();
      prefill();
      const sub = $("#pcSub");
      if (sub) {
        sub.textContent = objects.length > 1
          ? `这份 zip 里有 ${objects.length} 个对象，选一个克隆；字段会原样复制到新对象。`
          : "选择 zip 里的对象，起个新名字；字段会原样复制到新对象。";
      }
    } catch (err) {
      if (list) list.innerHTML = `<div class="pc-loading bad">${esc(err.message)}</div>`;
      setStatus(`没能读取这份 zip：${err.message}`, true);
    }
  }

  /* ---------------- 提交 ---------------- */

  async function submit() {
    if (busy) return;
    if (!zipFile || !picked) { setStatus("先选择要克隆的对象", true); return; }
    const name = ($("#pcName").value || "").trim();
    const code = ($("#pcCode").value || "").trim();
    if (!name) { setStatus("新对象名称不能为空", true); $("#pcName").focus(); return; }
    if (!CODE_RE.test(code)) { checkCode(); setStatus("对象编码不合法", true); $("#pcCode").focus(); return; }

    const go = $("#pcGo");
    busy = true;
    if (go) { go.disabled = true; go.textContent = "正在克隆…"; }
    setStatus("正在按原对象逐字段复制定义，并生成表单与列表…");
    try {
      const buf = await zipFile.arrayBuffer();
      const q = new URLSearchParams({ src: picked, name, code });
      const res = await fetch(`/api/clone?${q.toString()}`, {
        method: "POST", body: buf,
        headers: { "Content-Type": "application/octet-stream" },
      });
      const d = await res.json().catch(() => ({}));
      if (!res.ok || d.ok === false) throw new Error(d.error || `克隆失败 ${res.status}`);
      closeModal();
      const box = $("#aiResult");
      if (box) box.innerHTML = renderAiBo(d);
      if (typeof renderAiActions === "function") renderAiActions(d);
      decorateChat(d);
      if (typeof goStep === "function") goStep(2);
      const n = (d.bo && d.bo.fields || []).length;
      if (typeof toast === "function") {
        toast(`已克隆出「${d.bo.name}」：${n} 个字段，可继续对话增删`, 6000);
      }
    } catch (err) {
      setStatus(`克隆失败：${err.message}`, true);
    } finally {
      busy = false;
      if (go) { go.disabled = false; go.textContent = "开始克隆"; }
    }
  }

  /* ---------------- 克隆态的对话区 ---------------- */

  function decorateChat(d) {
    const from = d && d.bo && d.bo.cloneFrom;
    if (!from) return;
    // 四条常用词条换成克隆场景（结构不动，只改文案与 data-q）
    const labels = [
      "只保留必填字段",
      "去掉申请日期和附件，再加一个审批意见",
      "字段名称去掉「变更」二字",
      "加一个风险等级，选项高、中、低",
    ];
    document.querySelectorAll("#aiChat .chip-btn").forEach((b, i) => {
      if (!labels[i]) return;
      b.textContent = labels[i];
      b.dataset.q = labels[i];
    });
    const log = $("#aiChatLog");
    if (log && !document.querySelector(".clone-chat-hint")) {
      const hint = document.createElement("div");
      hint.className = "clone-chat-hint";
      hint.innerHTML = `克隆会话：字段与「${esc(from.name)}」一致，说「去掉 / 加一个」直接改字段`;
      log.parentElement.insertBefore(hint, log);
    }
  }

  /* ---------------- 绑定 ---------------- */

  function bind() {
    const pick = $("#btnClonePick");
    const input = $("#cloneZipInput");
    if (pick && input) {
      pick.onclick = () => input.click();
      input.onchange = (e) => {
        const f = e.target.files && e.target.files[0];
        e.target.value = "";
        if (f) openClone(f);
      };
    }

    const mask = $("#pcMask");
    if (mask) {
      if ($("#pcClose")) $("#pcClose").onclick = closeModal;
      if ($("#pcCancel")) $("#pcCancel").onclick = closeModal;
      if ($("#pcGo")) $("#pcGo").onclick = submit;
      mask.addEventListener("mousedown", (e) => {
        if (e.target === mask) closeModal();
      });
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && !mask.hidden) closeModal();
      });
      const codeEl = $("#pcCode");
      if (codeEl) {
        codeEl.addEventListener("input", () => {
          checkCode();
          clearTimeout(codeEl._t);
          codeEl._t = setTimeout(checkCodeTaken, 300);
        });
      }
      const nameEl = $("#pcName");
      if (nameEl) {
        nameEl.addEventListener("keydown", (e) => {
          if (e.key === "Enter") { e.preventDefault(); submit(); }
        });
      }
    }

    // 整张入口卡都能拖入 zip（与首屏 AI 卡同款计数写法，避免经过子元素闪）
    const card = $("#entryClone");
    if (card) {
      let depth = 0;
      const hasFiles = (e) => e.dataTransfer
        && Array.from(e.dataTransfer.types || []).includes("Files");
      card.addEventListener("dragenter", (e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        card.classList.add("drag");
      });
      card.addEventListener("dragover", (e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
      });
      card.addEventListener("dragleave", () => {
        if (depth > 0 && --depth === 0) card.classList.remove("drag");
        card.classList.remove("drag");
      });
      card.addEventListener("drop", (e) => {
        e.preventDefault();
        card.classList.remove("drag");
        const f = (e.dataTransfer.files || [])[0];
        if (f) openClone(f);
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }

  // 供 app.js 的 renderAiBo 之外的入口复用（目前只有本文件用）
  window.PP6Clone = { open: openClone, decorateChat };
})();
