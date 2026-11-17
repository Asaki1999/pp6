/* 数据查询模式：连接 → 提问 → SQL → 结果 */
const Q = {
  get: (id) => document.getElementById(id),
  templates: [],
  last: null,
  generatedSql: "",   // 机器给的原版 SQL，用户改动后据此提示「保存修正」
};

const qEsc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function qApi(path, body) {
  const opt = body === undefined
    ? { method: "GET" }
    : { method: "POST", body: JSON.stringify(body) };
  const res = await fetch(path, opt);
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) throw new Error(data.error || `请求失败 ${res.status}`);
  return data;
}

/* ---------------- 功能开关 ---------------- */
/* 存量包修订：先隐藏入口（按钮、快捷键 3、关于页文案一起收起来）。
   patch.js、#mode-patch 面板、/api/patch/* 后端全部保留，代码没动；
   恢复：把 patch 改回 true，其他都不用改。 */
window.PP6_FEATURES = { patch: false };
const PATCH_ON = () => window.PP6_FEATURES.patch !== false;

/* 按开关调整导航：隐藏的工作区按钮 / 快捷键说明 / 关于页文案 */
function applyFeatureFlags() {
  const on = PATCH_ON();
  const btn = document.querySelector('.mode[data-feature="patch"]');
  if (btn) btn.hidden = !on;
  const keys = document.getElementById("keysModes");
  if (keys) {
    keys.innerHTML = on
      ? '<kbd>1</kbd>–<kbd>5</kbd><span>切换工作区</span>'
      : '<kbd>1</kbd><kbd>2</kbd><kbd>4</kbd><kbd>5</kbd><span>切换工作区</span>';
  }
  const label = document.getElementById("aboutModeLabel");
  if (label) label.textContent = on ? "五个工作区" : "四个工作区";
  const modes = document.getElementById("aboutModes");
  if (modes) {
    modes.textContent = on
      ? "普通模式 · 流程模式 · 存量包修订 · 数据查询 · 系统设置"
      : "普通模式 · 流程模式 · 数据查询 · 系统设置";
  }
}
applyFeatureFlags();

/* ---------------- 模式切换 ---------------- */
/* 上下文栏 / 状态栏跟着工作区走，避免「切了页面但标题还是上一个」 */
const MODE_META = {
  import:   { title: "普通模式", desc: "普通业务对象 + 表单/列表/数据集，出一个同步包（导入走 发布管理 › 导入同步包）" },
  patch:    { title: "存量包修订", desc: "在原 zip 的 bo/*.xml 上打最小补丁，除新增字段外字节原样保留；对齐系统管理 › 发布管理 › 导入同步包，变更区非必经；反解会重造包，补字段请用这里" },
  query:    { title: "数据查询",   desc: "用一句话查数据库，生成 SQL 并查看结果" },
  workflow: { title: "流程模式",   desc: "和普通模式同一套操作；固定 开始→审核→通过，导出 zip 时额外给一份流程 XML" },
  settings: { title: "系统设置",   desc: "大模型、数据库连接与本地配置" },
};

function switchMode(mode) {
  // 存量包修订被隐藏时，任何来源（快捷键 / 旧链接 / 残留按钮）都回落到普通模式
  if (mode === "patch" && !PATCH_ON()) mode = "import";
  document.querySelectorAll(".mode").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  // 流程模式复用普通模式的工作区：导入/AI/预览/编辑完全同一套，
  // 只有导出时是否附带固定三步 XML 不同。
  document.getElementById("mode-import").hidden = !(mode === "import" || mode === "workflow");
  document.getElementById("mode-patch").hidden = mode !== "patch";
  document.getElementById("mode-query").hidden = mode !== "query";
  document.getElementById("mode-settings").hidden = mode !== "settings";
  if (window.PP6SetFlowMode) window.PP6SetFlowMode(mode === "workflow");
  // 底部 dock 只放状态栏：主操作统一留在卡片里，
  // 避免切换工作区时操作条忽隐忽现、工作区高度跟着跳。
  const bar = document.getElementById("actionbar");
  if (bar) bar.hidden = true;

  const meta = MODE_META[mode] || MODE_META.import;
  const put = (id, text) => {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  };
  put("ctxTitle", meta.title);
  put("ctxDesc", meta.desc);
  put("statusMode", meta.title);
  put("statusText", "就绪");
  document.title = `${meta.title} · PP6 Studio`;

  const ws = document.getElementById("workspace");
  if (ws) ws.scrollTop = 0;
  window.__currentMode = mode;

  if (mode === "query") { loadConfig(); ensureQueryReady(); }
  if (mode === "settings") loadConfig();
  if (mode === "workflow" && window.Workflow) window.Workflow.render();
}

/* 键盘：1–5 直达工作区，⌘/Ctrl+K 回生成页；输入时不抢按键 */
document.addEventListener("keydown", (e) => {
  const t = e.target;
  const typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" ||
                       t.tagName === "SELECT" || t.isContentEditable);
  if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === "k") {
    e.preventDefault();
    switchMode("import");
    return;
  }
  if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
  const hit = { "1": "import", "2": "workflow", "3": "patch", "4": "query", "5": "settings" }[e.key];
  // 3 号工作区被隐藏时按了不做事（也不能 preventDefault，避免吞掉正常输入）
  if (hit && (hit !== "patch" || PATCH_ON())) { e.preventDefault(); switchMode(hit); }
});

/* 状态栏时钟：本地运行，不请求网络 */
function paintClock() {
  const el = document.getElementById("statusClock");
  if (el) el.textContent = new Date().toLocaleTimeString("zh-CN",
    { hour: "2-digit", minute: "2-digit", hour12: false });
}
paintClock();
setInterval(paintClock, 30000);

/* 设置页的三个分栏 */
document.querySelectorAll(".set-tab").forEach((tab) => {
  tab.onclick = () => {
    document.querySelectorAll(".set-tab").forEach((t) => t.classList.toggle("active", t === tab));
    document.querySelectorAll("[data-set-panel]").forEach((p) => {
      p.hidden = p.dataset.setPanel !== tab.dataset.set;
    });
  };
});

document.querySelectorAll(".mode").forEach((b) => {
  b.onclick = () => switchMode(b.dataset.mode);
});

/* ---------------- 配置 ---------------- */
let Q_PROVIDERS = [];
let Q_CHANNEL_KEY = "";        // 当前渠道的标识，保存时带上

function fillProvider(p) {
  if (!p) return;
  Q_CHANNEL_KEY = p.key;
  if (p.base_url) Q.get("qBase").value = p.base_url;

  // 模型：有预设列表就用下拉，用户也能改（下拉里带「自定义」输入）
  // 自定义渠道（自建网关 / Ollama）永远给输入框——网关上有什么模型我们不可能知道，
  // 给下拉等于把人锁死在预设的几个名字上（曾因此在 newapi 上报 model_not_found）
  const pick = Q.get("qModelPick");
  const inp = Q.get("qModel");
  const models = p.models || (p.model ? [p.model] : []);
  if (models.length && p.key !== "custom") {
    pick.hidden = false;
    inp.hidden = true;
    pick.innerHTML = models
      .map((m) => `<option value="${qEsc(m)}">${qEsc(m)}${m === p.model ? " · 默认" : ""}</option>`)
      .join("");
    pick.value = p.model || models[0];
  } else {
    pick.hidden = true;
    inp.hidden = false;
    inp.value = p.model || "";
  }

  const hint = Q.get("qProvHint");
  if (p.free_note) {
    hint.hidden = false;
    hint.innerHTML = `${qEsc(p.free_note)}` +
      (p.apply_url ? ` · <a href="${qEsc(p.apply_url)}" target="_blank">去申请 Key</a>` : "");
  } else {
    hint.hidden = true;
  }
}

/* 当前选的模型名（下拉或手填都兼容） */
function currentModel() {
  const pick = Q.get("qModelPick");
  if (!pick.hidden) return pick.value.trim();
  return Q.get("qModel").value.trim();
}

function renderProviders(list) {
  const sel = Q.get("qProvider");
  sel.innerHTML = `<option value="">— 选一个免费平台，自动填好 —</option>` +
    list.map((p) => `<option value="${qEsc(p.key)}">${qEsc(p.name)}${p.free ? " · 免费" : ""}</option>`).join("");
  sel.onchange = () => {
    const p = Q_PROVIDERS.find((x) => x.key === sel.value);
    if (p) fillProvider(p);
  };
}

/* 进查询页就自动读一次结构：连接状态、可查表数、示例问题都不该等用户
   先去「设置」点一遍「连接并读取结构」才出现。 */
async function ensureQueryReady() {
  if (Q.readyTried) return;
  Q.readyTried = true;
  try {
    const d = await qApi("/api/nlq/test");
    renderConnStats(d);
    Q.templates = d.templates || [];
    renderTemplates();
  } catch (_) {
    /* 没连库/连不上时保持「还没连接数据库」，用户去设置里配 */
  }
}

async function loadConfig() {
  try {
    const d = await qApi("/api/nlq/config");
    const db = d.db || {};
    if (!Q.get("qHost").value) Q.get("qHost").value = db.host || "127.0.0.1";
    if (!Q.get("qPort").value) Q.get("qPort").value = db.port || 3306;
    if (!Q.get("qUser").value) Q.get("qUser").value = db.user || "root";
    if (!Q.get("qDb").value) Q.get("qDb").value = db.database || "";
    if (db.password_set) Q.get("qPass").placeholder = "已保存，留空则不修改";

    Q_PROVIDERS = d.providers || [];
    renderProviders(Q_PROVIDERS);

    const ch = ((d.llm || {}).channels || [])[0] || {};
    if (ch.base_url) Q.get("qBase").value = ch.base_url;
    if (ch.model) {
      Q.get("qModel").value = ch.model;
      const pick = Q.get("qModelPick");
      if (!pick.hidden) {
        if (![...pick.options].some((o) => o.value === ch.model)) {
          pick.insertAdjacentHTML("beforeend",
            `<option value="${qEsc(ch.model)}">${qEsc(ch.model)} · 已保存</option>`);
        }
        pick.value = ch.model;
      }
    }
    Q_CHANNEL_KEY = ch.key || "";
    if (ch.key) Q.get("qProvider").value = ch.key;
    if (ch.api_key_set) {
      Q.get("qKey").placeholder = "已记住（留空则沿用）";
      const box = Q.get("qLlmSaved");
      box.hidden = false;
      Q.get("qLlmSavedText").textContent =
        "已记住你的 Key · " + (ch.model || "未选模型") + "，下次打开不用再填";
      // 顶栏状态 + 查询页摘要：让用户一眼知道配没配
      AI_READY = true;
      AI_BRIEF = `${ch.model || "已配 Key"}`;
      paintAiState();
    }
  } catch (e) {
    /* 首次使用没有配置：状态灯保持灰，提示用户去设置 */
    paintAiState();
  }
}

/* 顶栏「AI 设置」按钮上那盏灯：绿=已配置，灰=没配 */
let AI_READY = false;
let AI_BRIEF = "";

function paintAiState() {
  // 导航项「设置」后面的小圆点
  const dot = document.getElementById("aiDot");
  if (dot) dot.classList.toggle("on", AI_READY);

  // 设置页顶部的状态标签
  const status = document.getElementById("setLlmStatus");
  if (status) {
    status.textContent = AI_READY ? `已配置 · ${AI_BRIEF}` : "未配置";
    status.classList.toggle("on", AI_READY);
  }

  // 顶栏右侧的 AI 状态胶囊
  const pill = document.getElementById("ctxAiPill");
  const pillDot = document.getElementById("ctxAiDot");
  const pillText = document.getElementById("ctxAiText");
  if (pill) {
    pill.classList.toggle("on", AI_READY);
    pill.title = AI_READY ? `已接入 ${AI_BRIEF}` : "到「设置」里填一个免费的 Key 就能用 AI";
  }
  if (pillDot) pillDot.classList.toggle("on", AI_READY);
  if (pillText) pillText.textContent = AI_READY ? "AI 已配置" : "AI 未配置";

  // 给 app.js（导入页）用：没配就让「生成业务对象」先引导去设置
  window.PP6_AI_READY = AI_READY;
  const aiHint = document.getElementById("aiHint");
  if (aiHint) {
    if (AI_READY) {
      aiHint.textContent = `已接入 ${AI_BRIEF} · 字段类型会自动选：金额→数字，审批人→人员，备注→多行文本`;
      aiHint.classList.remove("warn");
    } else {
      aiHint.textContent = "还没配置 AI，点左侧「设置」填一个免费的 Key 就能用";
      aiHint.classList.add("warn");
    }
  }
}

window.PP6_OPEN_AI = () => openAiModal();

function openAiModal() {
  switchMode("settings");
  loadConfig();
}

/* 查询页、导入页都能一键跳到设置 */
document.getElementById("qOpenSettings").onclick = () => openAiModal();
window.PP6_OPEN_AI = () => openAiModal();

function dbPayload() {
  return {
    host: Q.get("qHost").value.trim(),
    port: Q.get("qPort").value.trim() || "3306",
    user: Q.get("qUser").value.trim(),
    password: Q.get("qPass").value,
    database: Q.get("qDb").value.trim(),
  };
}

/* 连接统计：设置页和查询页各有一块，同步更新 */
function renderConnStats(d) {
  // 连接是不是只读的：本工具只发 SELECT，但「这条连接物理上写不了」才是
  // 真正兜底的一层，用户有权在连上之后立刻知道。
  const ro = d.readonly || {};
  const roOk = ro.session === true || ro.server === true || ro.super === true;
  const roText = ro.session === true ? "只读会话"
    : (ro.super === true || ro.server === true) ? "服务器只读"
    : ro.session === false ? "可写会话" : "未知";
  const html = `
    <div><b>${d.tableCount}</b>业务表</div>
    <div><b>${d.columnCount}</b>字段</div>
    <div><b>${d.dictCount}</b>字典</div>
    <div><b>${d.version.split("-")[0]}</b>MySQL</div>
    <div title="${roOk ? "这条连接写不了数据，查询出错也伤不到库"
                       : "这条连接能写数据，请只用只读账号连生产库"}"><b>${roText}</b></div>`;
  for (const id of ["qStats", "qConnStats"]) {
    const el = document.getElementById(id);
    if (!el) continue;
    el.hidden = false;
    el.innerHTML = html;
  }
  const hint = document.getElementById("qConnHint");
  if (hint) {
    const lib = d.library ? ` · 已验证 ${d.library.verified}/${d.library.total} 条` : "";
    const sem = d.semantic ? ` · 口径 ${d.semantic.length} 条` : "";
    hint.textContent = `已连接 ${d.database} · ${d.tableCount} 张表${lib}${sem}`;
    // 鼠标悬停能看到具体口径，排查「为什么它这么理解」时有用
    hint.title = (d.semantic || []).map((x) => `- ${x.rule}`).join("\n");
  }
}

Q.get("qTest").onclick = async () => {
  const btn = Q.get("qTest");
  btn.disabled = true; btn.textContent = "连接中…";
  try {
    await qApi("/api/nlq/save-config", { db: dbPayload() });
    const d = await qApi("/api/nlq/test");
    Q.get("qPass").value = "";
    Q.get("qPass").placeholder = "已保存，留空则不修改";
    renderConnStats(d);
    Q.templates = d.templates || [];
    renderTemplates();
    toast(`已连接 ${d.database}，读到 ${d.tableCount} 张业务表`);
    // 连生产库时这条提示比「连上了」重要得多
    if (d.readonly && d.readonly.session === false) {
      toast("注意：这条连接是「可写」的。建议改用只读账号连客户库——" +
            "程序只发 SELECT，但账号级只读是唯一绕过代码后还挡得住的一层。", 9000);
    }
  } catch (e) {
    toast("连接失败：" + e.message, 6000);
  } finally {
    btn.disabled = false; btn.textContent = "连接并读取结构";
  }
};

/* 「保存并测试」：先测连通性，测通了才落盘。
   不再拆成「保存」和「测试」两个按钮——拆开后用户会困惑：
   「测试能通，保存完反而连不上」，其实是保存和测试各自读输入框、时机不同。
   现在一步到位：测通 = 保存，测不通 = 不保存并显示原因。 */
Q.get("qSaveLlm").onclick = async () => {
  const btn = Q.get("qSaveLlm");
  const box = Q.get("qLlmStatus");
  const base = Q.get("qBase").value.trim();
  const model = currentModel();
  const key = Q.get("qKey").value.trim();
  if (!base) { toast("请先选一个平台，或手填接口地址"); return; }
  if (!model) { toast("请填写模型名（选平台会自动填好）"); return; }
  if (!key && !AI_READY) { toast("请粘贴 API Key"); return; }

  btn.disabled = true; btn.textContent = "测试中…";
  box.hidden = false; box.textContent = "正在测试连接…";

  try {
    // 第一步：测连通性。Key 空（已保存过）则不传 channel，用已存配置测。
    const payload = key
      ? { channel: { key: Q_CHANNEL_KEY, base_url: base, api_key: key, model: model } }
      : {};
    const d = await qApi("/api/nlq/test-llm", payload);
    if (!d.ok) {
      box.textContent = "✗ 连接失败，未保存：" + d.error;
      return;
    }

    // 第二步：测通了才保存
    const prov = Q_PROVIDERS.find((x) => x.key === Q_CHANNEL_KEY);
    const fallbacks = ((prov && prov.models) || [])
      .filter((m) => m !== model).slice(0, 4);
    await qApi("/api/nlq/save-config", {
      llm: {
        channels: [{
          key: Q_CHANNEL_KEY, base_url: base, api_key: key,
          model: model, fallbacks: fallbacks, enabled: true,
        }],
      },
    });

    // 第三步：保存成功，清空输入框、刷新状态
    Q.get("qKey").value = "";
    Q.get("qKey").placeholder = "已保存，留空则不修改";
    AI_READY = true;
    AI_BRIEF = model;
    paintAiState();
    box.innerHTML = `✓ 已保存并验证通过 · ${qEsc(d.model)} · ${d.seconds}s`;
    toast("已保存并验证通过");
  } catch (e) {
    box.textContent = "✗ " + e.message;
  } finally {
    btn.disabled = false; btn.textContent = "保存并测试";
  }
};


/* ---------------- 来源标识：一眼看出是规则还是 AI ---------------- */
const ORIGIN = {
  rule:   { icon: "⚙", label: "规则生成", cls: "src-rule",
            tip: "本地规则引擎生成，不调用大模型，速度快、零成本" },
  ai:     { icon: "✦", label: "AI 生成", cls: "src-ai",
            tip: "由大模型理解后生成" },
  memory: { icon: "◈", label: "复用的 AI 答案", cls: "src-mem",
            tip: "之前 AI 答对过并已保存，现在直接复用，不再调用大模型" },
  corrected: { icon: "✎", label: "人工修正", cls: "src-mem",
               tip: "你手工改过的 SQL，已记下并优先使用" },
  clarify: { icon: "?", label: "待确认", cls: "src-warn",
             tip: "缺关键条件，先确认再查，避免把全量历史算进去" },
};

function originBadge(d) {
  const o = ORIGIN[d.origin] || ORIGIN.rule;
  const model = d.origin === "ai" && d.model ? ` · ${qEsc(d.model)}` : "";
  return `<span class="src-badge ${o.cls}" title="${qEsc(o.tip)}">` +
         `<i>${o.icon}</i>${o.label}${model}</span>`;
}

/* 这一条是怎么来的（放 SQL 卡片标题下） */
function renderOrigin(d) {
  const box = Q.get("qOrigin");
  if (!box) return;
  let html = originBadge(d);

  if (d.origin === "ai") {
    if (d.rule_reason) {
      html += `<span class="src-why">规则没把握：${qEsc(d.rule_reason)}</span>`;
    }
    if (d.learned) {
      html += `<span class="src-learned" title="已存入示例库，下次同类问题直接复用">已记住</span>`;
    }
    if (d.conflict) {
      html += `<span class="src-warn" title="${qEsc(d.conflict)}">未保存</span>`;
    }
  } else if (d.origin === "memory" && d.source_question) {
    html += `<span class="src-why">来自之前问过：${qEsc(d.source_question)}</span>`;
  } else if (d.ai_error) {
    html += `<span class="src-why">AI 不可用（${qEsc(d.ai_error)}）</span>`;
  }
  if (d.uncertain) {
    html += `<span class="src-warn" title="${qEsc(d.uncertain)}">结果待核对</span>`;
  }
  if (d.verified) {
    html += `<span class="src-verified" title="人工确认过，已进已验证库，下次优先复用">✓ 已验证</span>`;
  }
  if (d.summary) {
    html += `<div class="q-understood"><b>查询理解</b>：${qEsc(d.summary)}</div>`;
  }
  box.innerHTML = html;
  box.hidden = false;
}

/* ---------------- 模板 ---------------- */
function renderTemplates() {
  const box = Q.get("qTemplates");
  if (!Q.templates.length) { box.innerHTML = ""; return; }
  box.innerHTML = Q.templates
    .map((t, i) => `<button data-i="${i}">${qEsc(t.title)}</button>`).join("");
  box.querySelectorAll("button").forEach((btn) => {
    btn.onclick = () => {
      const t = Q.templates[Number(btn.dataset.i)];
      Q.get("qQuestion").value = t.question;
      showSql(t.sql, t.question, true);
      runSql(t.sql);
    };
  });
}

/* ---------------- 生成 / 执行 ---------------- */
function showSql(sql, question, generated) {
  Q.get("qSqlCard").hidden = false;
  Q.get("qSql").value = sql;
  Q.get("qSqlMeta").textContent = question ? "来自：" + question : "";
  Q.get("qErr").hidden = true;
  // 记住「机器给的原版」，用户改动后据此判断要不要亮出「保存修正」
  if (generated) Q.generatedSql = sql;
  if (Q.get("qSaveCorrect")) syncCorrectBtn();
}

function renderResult(d, ms) {
  if (d.clarify) return renderClarify(d);
  Q.get("qResultCard").hidden = false;
  // 规则/记忆答的都可以一键让 AI 重答；已经是 AI 的就不用了
  const retry = Q.get("qRetryAi");
  if (retry) retry.hidden = !d.origin || d.origin === "ai";
  const verify = Q.get("qVerify");
  if (verify) {
    verify.hidden = !d.sql || !!d.verified || d.origin === "clarify";
    verify.disabled = false;
    verify.textContent = "标记为已验证";
  }
  const meta = Q.get("qResultMeta");
  const o = ORIGIN[d.origin] || ORIGIN.rule;
  meta.innerHTML = `${d.rows.length} 行${ms ? " · " + ms + " ms" : ""}` +
    ` <span class="src-badge ${o.cls} sm" title="${qEsc(o.tip)}">${o.icon}${o.label}</span>`;
  if (!d.rows.length) {
    // 0 行时给出诊断（表空？条件太严？取值写错？），别只丢一句"没有数据"
    const hint = d.hint
      ? `<div class="empty"><b>没有查到数据</b><div class="empty-hint">${qEsc(d.hint)}</div></div>`
      : '<div class="empty">查询执行成功，但没有匹配的数据。</div>';
    Q.get("qResult").innerHTML = hint;
    Q.last = d;
    return;
  }
  const head = d.columns.map((c) => `<th>${qEsc(c)}</th>`).join("");
  const body = d.rows.map((r) => `<tr>${r.map((v) =>
    v === null || v === ""
      ? '<td class="null">—</td>'
      : `<td>${qEsc(String(v).length > 80 ? String(v).slice(0, 80) + "…" : v)}</td>`
  ).join("")}</tr>`).join("");
  Q.get("qResult").innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  Q.last = d;
}

/* 缺时间范围时只问一句，点选项补进问题里再查；不打断别的查询 */
function renderClarify(d) {
  const c = d.clarify || {};
  Q.get("qResultCard").hidden = false;
  Q.get("qResultMeta").textContent = "需要确认";
  const retry = Q.get("qRetryAi");
  if (retry) retry.hidden = true;
  const verify = Q.get("qVerify");
  if (verify) verify.hidden = true;
  const opts = (c.options || []).map((o) =>
    `<button class="btn ghost" data-opt="${qEsc(o)}">${qEsc(o)}</button>`).join("");
  Q.get("qResult").innerHTML = `
    <div class="q-clarify">
      <div class="q-clarify-q">${qEsc(c.question || "需要补充一个条件")}</div>
      ${c.why ? `<div class="q-clarify-why">${qEsc(c.why)}</div>` : ""}
      <div class="q-clarify-opts">${opts}</div>
    </div>`;
  Q.get("qResult").querySelectorAll("button[data-opt]").forEach((btn) => {
    btn.onclick = () => {
      const q = Q.get("qQuestion");
      q.value = q.value.trim() + `（时间范围：${btn.dataset.opt}）`;
      // 补完条件直接交给 AI：规则对「新增 + 时间 + 分组」这类组合容易过度解释
      ask("llm", true);
    };
  });
  Q.last = d;
}

async function runSql(sql) {
  const t0 = performance.now();
  try {
    const d = await qApi("/api/nlq/run", { sql });
    Q.get("qErr").hidden = true;
    showSql(d.sql, Q.get("qSqlMeta").textContent.replace(/^来自：/, ""));
    renderResult(d, Math.round(performance.now() - t0));
    // 结果在 SQL 卡片下面，长 SQL 会把它挤出视野；直接带用户过去
    Q.get("qResultCard").scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (e) {
    Q.get("qErr").hidden = false;
    Q.get("qErr").textContent = e.message;
    Q.get("qResultCard").hidden = true;
  }
}

async function ask(engine, clarified) {
  Q.get("qRuleBox").hidden = true;
  const question = Q.get("qQuestion").value.trim();
  if (!question) { toast("先写一句你想查什么"); return; }
  const btn = Q.get("qAsk");
  btn.disabled = true; btn.textContent = "生成中…";
  const t0 = performance.now();
  try {
    const d = await qApi("/api/nlq/ask", { question, engine, clarified: !!clarified });
    if (d.sql) {
      showSql(d.sql, question, true);
      renderOrigin(d);
    } else {
      Q.get("qSqlCard").hidden = true;
      Q.get("qOrigin").hidden = true;
    }
    Q.get("qSqlMeta").textContent = (question ? "来自：" + question : "") +
      (d.note ? "　✎ " + d.note : "");   // 复用人工修正时带上他自己写的原因
    if (d.retried) toast("第一次 SQL 执行报错，已自动修正");
    if (d.learned) toast("这条已记下，下次同类问题规则能直接答");
    const ruleBox = Q.get("qRuleBox");
    if (d.ruleSql && d.ruleSql !== d.sql) {
      ruleBox.hidden = false;
      Q.get("qRuleSql").value = d.ruleSql;
    } else {
      ruleBox.hidden = true;
    }
    renderResult(d, Math.round(performance.now() - t0));
    // 结果在 SQL 卡片下面，长 SQL 会把它挤出视野；直接带用户过去
    Q.get("qResultCard").scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (e) {
    Q.get("qSqlCard").hidden = false;
    // 清空旧的 SQL：被安全闸拒掉时如果还留着上一次的结果，
    // 很容易被当成「这次生成的」，真的复制去库里跑就麻烦了。
    Q.get("qSql").value = "";
    Q.get("qRuleBox").hidden = true;
    Q.get("qErr").hidden = false;
    Q.get("qErr").textContent = e.message +
      (String(e.message).includes("API Key")
        ? "\n\n不配模型也能用：换成像「项目的名称和项目经理」这样带上对象名和字段名的说法，会走本地规则解析。"
        : "");
    Q.get("qResultCard").hidden = true;
  } finally {
    btn.disabled = false; btn.textContent = "生成 SQL 并执行";
  }
}

/* 复制 SQL：桌面 WebView 里 navigator.clipboard 不一定可用，退回 execCommand */
async function copySql() {
  const ta = Q.get("qSql");
  const text = ta.value;
  if (!text.trim()) { toast("还没有 SQL 可复制"); return; }

  let done = false;

  // 桌面壳：系统剪贴板最稳（WebView 里 clipboard API 常被禁用）
  const api = window.pywebview && window.pywebview.api;
  if (api && api.copy_text) {
    try {
      const r = await api.copy_text(text);
      done = !!(r && r.ok);
    } catch (_) { done = false; }
  }

  if (!done && navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      done = true;
    } catch (_) { done = false; }
  }
  if (!done) {
    ta.focus();
    ta.select();
    ta.setSelectionRange(0, text.length);
    try { done = document.execCommand("copy"); } catch (_) { done = false; }
    ta.setSelectionRange(text.length, text.length);
  }
  if (done) {
    toast("SQL 已复制到剪贴板");
    const btn = Q.get("qCopySql");
    const old = btn.textContent;
    btn.textContent = "已复制 ✓";
    setTimeout(() => { btn.textContent = old; }, 1400);
  } else {
    toast("复制失败，请手动选中后按 ⌘C / Ctrl+C");
  }
}

Q.get("qCopySql").onclick = copySql;
Q.get("qRetryAi").onclick = () => ask("llm");
Q.get("qVerify").onclick = async () => {
  const question = Q.get("qQuestion").value.trim();
  const sql = Q.get("qSql").value.trim();
  if (!question || !sql) { toast("先查一次再标记"); return; }
  const btn = Q.get("qVerify");
  btn.disabled = true; btn.textContent = "保存中…";
  try {
    const d = await qApi("/api/nlq/verify", { question, sql });
    toast(`已验证 ${d.verified} 条（共 ${d.total} 条）`);
    btn.textContent = "已入库 ✓";
    if (Q.last) Q.last.verified = true;
    renderOrigin(Q.last);
  } catch (e) {
    btn.disabled = false; btn.textContent = "标记为已验证";
    toast("保存失败：" + e.message, 5000);
  }
};
Q.get("qLlmForget").onclick = async () => {
  if (!confirm("清除后下次需要重新填写 Key，确定吗？")) return;
  try {
    await qApi("/api/nlq/save-config", {
      llm: { channels: [{ key: Q_CHANNEL_KEY, base_url: Q.get("qBase").value,
                          api_key: "", model: currentModel(), clear_key: true }] },
    });
    Q.get("qLlmSaved").hidden = true;
    Q.get("qKey").placeholder = "粘贴平台的 Key（sk-...）";
    AI_READY = false;
    AI_BRIEF = "";
    paintAiState();
    toast("已清除 Key");
  } catch (e) { toast("清除失败：" + e.message); }
};

Q.get("qAsk").onclick = () => ask("hybrid");
Q.get("qAskAi").onclick = () => ask("llm");

Q.get("qRun").onclick = () => runSql(Q.get("qSql").value);

/* 用户改了 SQL：亮出「保存修正」+ 备注输入——人改对的那条是最值钱的训练数据 */
function syncCorrectBtn() {
  const bar = Q.get("qFixBar");
  if (!bar) return;
  const now = Q.get("qSql").value.trim();
  bar.hidden = !Q.generatedSql || !now || now === Q.generatedSql;
}
Q.get("qSql").addEventListener("input", syncCorrectBtn);

Q.get("qSaveCorrect").onclick = async () => {
  const question = Q.get("qQuestion").value.trim();
  const sql = Q.get("qSql").value.trim();
  const note = Q.get("qFixNote").value.trim();
  if (!question || !sql) { toast("问题和 SQL 都要有"); return; }
  const btn = Q.get("qSaveCorrect");
  btn.disabled = true; btn.textContent = "保存中…";
  try {
    const d = await qApi("/api/nlq/correct", {
      question, sql, note, original_sql: Q.generatedSql || "",
    });
    if (!d.ok) { toast(d.error || "没能记录"); return; }
    Q.generatedSql = sql;
    syncCorrectBtn();
    toast(d.note || "已记下你的改法");
  } catch (e) {
    toast("保存失败：" + e.message);
  } finally {
    btn.disabled = false; btn.textContent = "保存修正";
  }
};

Q.get("qSaveExample").onclick = async () => {
  const question = Q.get("qQuestion").value.trim();
  const sql = Q.get("qSql").value.trim();
  if (!question || !sql) { toast("问题和 SQL 都要有"); return; }
  try {
    const d = await qApi("/api/nlq/example", { question, sql });
    toast(`已存为示例（共 ${d.examples} 条），下次同类问题会更准`);
  } catch (e) { toast("保存失败：" + e.message); }
};

Q.get("qExport").onclick = () => {
  const d = Q.last;
  if (!d || !d.rows.length) { toast("没有可导出的数据"); return; }
  const csv = [d.columns, ...d.rows]
    .map((r) => r.map((v) => `"${String(v ?? "").replace(/"/g, '""')}"`).join(","))
    .join("\r\n");
  const blob = new Blob(["\ufeff" + csv], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "查询结果-" + new Date().toISOString().slice(0, 19).replace(/[:T]/g, "") + ".csv";
  a.click();
};

/* 启动就拉一次配置：顶栏的 AI 状态灯和导入页提示都依赖它，
   不能等用户切到查询页才加载——否则第一眼看到的永远是「未配置」。 */
loadConfig();
