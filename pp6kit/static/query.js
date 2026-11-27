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
/* 存量包修订：2026-11-18 起对外开放。
   开放口径：导入已有业务对象后，**已有字段只能改名称**、可以新增字段；
   类型/选项/长度等定义不给改，也不删字段（后端 patch.py 同样硬拦）。
   关闭时：把 patch 改回 false，按钮、快捷键 3、关于页文案一起收起来，
   patch.js、#mode-patch 面板、/api/patch/* 后端都保留。 */
window.PP6_FEATURES = { patch: true };
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
      ? "新建业务配置 · 新建流程配置 · 存量包修订 · 数据查询 · 系统设置"
      : "新建业务配置 · 新建流程配置 · 数据查询 · 系统设置";
  }
}
applyFeatureFlags();

/* ---------------- 模式切换 ---------------- */
/* 上下文栏 / 状态栏跟着工作区走，避免「切了页面但标题还是上一个」 */
const MODE_META = {
  import:   { title: "新建业务配置", desc: "说需求或导入规格表，生成业务对象、表单与列表" },
  patch:    { title: "存量包修订", desc: "在已有导入包上加字段或改字段名称" },
  query:    { title: "数据查询",   desc: "用一句话查库，只读执行" },
  workflow: { title: "新建流程配置",   desc: "与新建业务配置同一套操作，另附固定三步流程" },
  settings: { title: "系统设置",   desc: "大模型、数据库连接与升级" },
};

function switchMode(mode) {
  // 存量包修订被隐藏时，任何来源（快捷键 / 旧链接 / 残留按钮）都回落到新建业务配置
  if (mode === "patch" && !PATCH_ON()) mode = "import";
  document.querySelectorAll(".mode").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  // 新建流程配置复用新建业务配置的工作区：导入/AI/预览/编辑完全同一套，
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
  // 数据查询：⌘/Ctrl + Enter 直接执行，光标在问题框里也算（Enter 仍可换行）
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
    const box = document.getElementById("qQuestion");
    if (box && document.activeElement === box) {
      const go = document.getElementById("qAsk");
      if (go && !go.disabled) { e.preventDefault(); go.click(); return; }
    }
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

/* ---------------- 配置 ----------------
 * 不再提供「平台预设」：接口地址和模型名都由用户自己填（2026-11-26）。
 * 渠道标识固定为 custom，后端只是存个名字，不参与路由。 */
const CHANNEL_KEY = "custom";

/* 当前选的模型名（手填框） */
function currentModel() {
  return (Q.get("qModel").value || "").trim();
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
    /* 没连库/连不上时保持「还没连接数据库」，用户去设置里配；
       但示例问法还是给几条（只填进输入框，不执行），别让页面空着。 */
    Q.templates = [];
    renderTemplates();
  }
}

/* ---------------- Key / 密码：已保存就显示黑点 ----------------
   以前「已保存」只是把输入框留空、在 placeholder 写「留空则沿用」，
   看着跟没配一样。现在直接填一串黑点：一眼看出有值；点进去自动清空
   可直接覆盖，不改就还原黑点，提交时按「未修改」处理，语义不变。 */
const SECRET_DOTS = "•".repeat(20);

function maskSecret(el, on, ph) {
  if (!el) return;
  if (on) {
    el.value = SECRET_DOTS;
    el.dataset.masked = "1";
    el.dataset.saved = "1";
    el.placeholder = "";
  } else {
    el.value = "";
    el.dataset.masked = "";
    el.dataset.saved = "";
    el.placeholder = ph || el.dataset.phEdit || "";
  }
}

/* 掩码只是展示：聚焦即清空让用户输入新值，失焦仍为空则还原黑点 */
function bindSecretFocus(el, phEdit) {
  if (!el) return;
  el.dataset.phEdit = phEdit;
  el.addEventListener("focus", () => {
    if (el.dataset.masked !== "1") return;
    el.dataset.masked = "";
    el.value = "";
    el.placeholder = phEdit;
  });
  el.addEventListener("blur", () => {
    if (!el.value && el.dataset.saved === "1") maskSecret(el, true);
  });
}

/* 掩码态 = 用户没动过，提交时当空值（后端沿用已存的 Key / 密码） */
function secretValue(el) {
  return el && el.dataset.masked === "1" ? "" : (el ? el.value : "");
}

/* 切页面会重新读配置：只有框里本来就空着、或是我们自己填的黑点时才重新掩码，
   用户手打进去还没保存的值不能被覆盖。 */
function canMask(el) {
  return !!el && (!el.value || el.dataset.masked === "1");
}

bindSecretFocus(Q.get("qKey"), "输入新 Key 以替换已保存的");
bindSecretFocus(Q.get("qPass"), "输入新密码以替换已保存的");

async function loadConfig() {
  try {
    const d = await qApi("/api/nlq/config");
    const db = d.db || {};
    if (!Q.get("qHost").value) Q.get("qHost").value = db.host || "127.0.0.1";
    if (!Q.get("qPort").value) Q.get("qPort").value = db.port || 3306;
    if (!Q.get("qUser").value) Q.get("qUser").value = db.user || "root";
    if (!Q.get("qDb").value) Q.get("qDb").value = db.database || "";
    // 用户刚敲进去、还没保存的新值不能被「已保存」的黑点盖掉
    if (db.password_set && canMask(Q.get("qPass"))) maskSecret(Q.get("qPass"), true);

    if (d.instructions) Q.get("qInstr").value = d.instructions;

    const ch = ((d.llm || {}).channels || [])[0] || {};
    if (ch.base_url) Q.get("qBase").value = ch.base_url;
    if (ch.model) Q.get("qModel").value = ch.model;
    Q_CHANNEL_KEY = ch.key || CHANNEL_KEY;
    if (ch.api_key_set) {
      if (canMask(Q.get("qKey"))) maskSecret(Q.get("qKey"), true);
      const box = Q.get("qLlmSaved");
      box.hidden = false;
      Q.get("qLlmSavedText").textContent =
        "已记住你的 Key · " + (ch.model || "未选模型") + "，下次打开不用再填";
      // 顶栏状态 + 查询页摘要：让用户一眼知道配没配
      AI_READY = true;
      AI_BRIEF = `${ch.model || "已配 Key"}`;
      paintAiState();
    }
    // 首屏右栏的数据库一行：这里只知道「配了哪个库」，不知道通不通；
    // 真正连上之后由 renderConnStats() 改成「已连接」。
    const homeDb = document.getElementById("homeDbState");
    if (homeDb) {
      homeDb.innerHTML = `<i class="led" id="homeDbLed"></i>`
        + (db.database ? `已配置 ${qEsc(db.database)}` : "未连接");
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
    pill.title = AI_READY ? `已接入 ${AI_BRIEF}` : "在「系统设置」中配置 API Key 后可用";
  }
  if (pillDot) pillDot.classList.toggle("on", AI_READY);
  if (pillText) {
    const brief = AI_READY && AI_BRIEF ? ` · ${AI_BRIEF}` : "";
    pillText.textContent = AI_READY ? `AI 已配置${brief}` : "AI 未配置";
  }

  // 首屏右栏 / 查询页连接条：同一份状态投影，别让两处说不一样的话
  const homeLlm = document.getElementById("homeLlmState");
  if (homeLlm) {
    homeLlm.innerHTML = `<i class="led ${AI_READY ? "brand" : ""}" id="homeLlmLed"></i>`
      + (AI_READY ? (AI_BRIEF || "已配置") : "未配置");
  }
  const qLlm = document.getElementById("qLlmState");
  if (qLlm) qLlm.textContent = AI_READY ? (AI_BRIEF || "已配置") : "未配置";
  const qLlmLed = document.getElementById("qLlmLed");
  if (qLlmLed) qLlmLed.classList.toggle("brand", AI_READY);

  // 给 app.js（导入页）用：没配就让「生成业务对象」先引导去设置
  window.PP6_AI_READY = AI_READY;
  const aiHint = document.getElementById("aiHint");
  if (aiHint) {
    if (AI_READY) {
      aiHint.textContent = `已接入 ${AI_BRIEF} · 字段类型自动识别：金额为数字、审批人为用户、备注为大文本`;
      aiHint.classList.remove("warn");
    } else {
      aiHint.textContent = "未配置大模型；在「系统设置」中填写 API Key 后可用";
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
    password: secretValue(Q.get("qPass")),
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
    // 连接条只留「连的是哪个库」；表/字段数交给旁边的 q-stats，别重复两遍
    hint.textContent = `数据库 ${d.database}`;
    // 鼠标悬停能看到具体口径与验证进度，排查「为什么它这么理解」时有用
    hint.title = `已连接 ${d.database} · ${d.tableCount} 张表${lib}${sem}`
      + (d.semantic || []).map((x) => `\n- ${x.rule}`).join("");
  }
  const led = document.getElementById("qConnLed");
  if (led) led.classList.add("ok");
  const homeDb = document.getElementById("homeDbState");
  if (homeDb) {
    homeDb.innerHTML = `<i class="led ok" id="homeDbLed"></i>已连接 ${qEsc(d.database)}`;
  }
}

Q.get("qTest").onclick = async () => {
  const btn = Q.get("qTest");
  btn.disabled = true; btn.textContent = "连接中…";
  try {
    await qApi("/api/nlq/save-config", { db: dbPayload() });
    const d = await qApi("/api/nlq/test");
    maskSecret(Q.get("qPass"), true);
    renderConnStats(d);
    Q.templates = d.templates || [];
    renderTemplates();
    toast(`已连接 ${d.database}，读到 ${d.tableCount} 张业务表`);
    // 连生产库时这条提示比「连上了」重要得多
    if (d.readonly && d.readonly.session === false) {
      toast("当前连接为「可写」账号，建议改用只读账号连接客户库。" +
            "工具只发送 SELECT，但账号级只读是代码之外的最终防线。", 9000);
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
  const key = secretValue(Q.get("qKey")).trim();
  if (!base) { toast("请填写接口地址"); return; }
  if (!model) { toast("请填写模型名称"); return; }
  if (!key && !AI_READY) { toast("请粘贴 API Key"); return; }

  btn.disabled = true; btn.textContent = "测试中…";
  box.hidden = false; box.textContent = "正在测试连接…";

  try {
    // 第一步：测连通性。Key 空（已保存过）则不传 channel，用已存配置测。
    const payload = key
      ? { channel: { key: Q_CHANNEL_KEY || CHANNEL_KEY, base_url: base, api_key: key, model: model } }
      : {};
    const d = await qApi("/api/nlq/test-llm", payload);
    if (!d.ok) {
      box.textContent = "✗ 连接失败，未保存：" + d.error;
      return;
    }

    // 第二步：测通了才保存。没有平台预设了，备选模型也不存在
    await qApi("/api/nlq/save-config", {
      llm: {
        channels: [{
          key: Q_CHANNEL_KEY || CHANNEL_KEY, base_url: base, api_key: key,
          model: model, fallbacks: [], enabled: true,
        }],
      },
      // 全局指令跟 Key 一起存，用户不用再找个单独的地方点保存
      instructions: Q.get("qInstr").value.trim(),
    });

    // 第三步：保存成功，输入框换成黑点（不是留空，留空看着像没配）
    maskSecret(Q.get("qKey"), true);
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
  assist: { icon: "✦", label: "AI 助手", cls: "src-ai",
            tip: "按你选的动作（Ask / Agent）由大模型生成或解释" },
};

function originBadge(d) {
  const o = ORIGIN[d.origin] || ORIGIN.rule;
  // 具体动作名（解释 SQL / 生成样例…）比笼统的「AI 助手」有用，优先用它
  const label = d.origin_label || o.label;
  const model = d.origin === "ai" && d.model ? ` · ${qEsc(d.model)}` : "";
  return `<span class="src-badge ${o.cls}" title="${qEsc(o.tip)}">` +
         `<i>${o.icon}</i>${qEsc(label)}${model}</span>`;
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
      html += `<span class="src-learned" title="已存入示例库，同类问题优先复用">已记录</span>`;
    }
    if (d.conflict) {
      html += `<span class="src-warn" title="${qEsc(d.conflict)}">未保存</span>`;
    }
  } else if (d.origin === "memory" && d.source_question) {
    html += `<span class="src-why">来自之前问过：${qEsc(d.source_question)}</span>`;
  } else if (d.origin === "agent") {
    // 查证模式：把「查了几次、查了什么」摊开，用户才知道这条 SQL 是有依据的
    const n = (d.trace || []).length;
    if (n) {
      const steps = (d.trace || []).map((t) => t.tool).join(" → ");
      html += `<span class="src-why" title="模型先取证据再写 SQL">查证 ${n} 次：${qEsc(steps)}</span>`;
    }
    if (d.note) html += `<span class="src-why">${qEsc(d.note)}</span>`;
  } else if (d.ai_error) {
    html += `<span class="src-why">AI 不可用（${qEsc(d.ai_error)}）</span>`;
  }
  if (d.uncertain) {
    html += `<span class="src-warn" title="${qEsc(d.uncertain)}">结果待核对</span>`;
  }
  // 没执行的动作必须标明：用户看到一条 SQL 很容易以为它已经跑过了
  if (d.warning && d.sql && d.executed === false) {
    html += `<span class="src-warn" title="${qEsc(d.warning)}">未执行</span>`;
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
/* 没连库时的问法示例：点一下只填进输入框，不执行——库还没连，执行必然报错，
   先让用户知道「这个框能问什么」比给一排空按钮有用。 */
const Q_FALLBACK = [
  { title: "项目清单", question: "列出执行中的项目，按创建时间倒序" },
  { title: "按状态统计", question: "每个项目状态有多少个项目" },
  { title: "任务统计", question: "本月新增的任务按负责人统计" },
  { title: "风险清单", question: "风险等级为「高」的项目有哪些" },
];

function renderTemplates() {
  const box = Q.get("qTemplates");
  if (!box) return;
  if (!Q.templates.length) {
    box.innerHTML = Q_FALLBACK
      .map((q) => `<button class="ex" data-fill="1"><b>${qEsc(q.title)}</b><span>${qEsc(q.question)}</span></button>`)
      .join("");
    box.querySelectorAll("button[data-fill]").forEach((btn) => {
      btn.onclick = () => {
        const input = Q.get("qQuestion");
        if (!input) return;
        input.value = btn.querySelector("span").textContent;
        input.focus();
      };
    });
    return;
  }
  box.innerHTML = Q.templates
    .map((t, i) => `<button class="ex" data-i="${i}"><b>${qEsc(t.title)}</b><span>${qEsc(t.question)}</span></button>`)
    .join("");
  box.querySelectorAll("button").forEach((btn) => {
    btn.onclick = () => {
      const t = Q.templates[Number(btn.dataset.i)];
      Q.get("qQuestion").value = t.question;
      showSql(t.sql, t.question, true);
      runSql(t.sql);
    };
  });
}

/* ---------------- 追问上下文 ----------------
   上一轮的 SQL 是最有用的上下文：用户说「只看执行中的」「再按项目分组」时，
   没有它模型只能从头猜一遍，等于每句话都要重说完整需求。 */
Q.thread = null;

function paintThread() {
  const bar = Q.get("qThread");
  if (!bar) return;
  const t = Q.thread;
  bar.hidden = !t;
  if (t) {
    const q = (t.question || "上一轮查询").trim();
    const el = Q.get("qThreadQ");
    el.textContent = q.length > 28 ? q.slice(0, 28) + "…" : q;
    el.title = q;
  }
}

function setThread(question, sql) {
  if (!sql) return;
  Q.thread = { question: (question || "").trim(), sql: String(sql).trim() };
  paintThread();
}

Q.get("qThreadClear").onclick = () => {
  Q.thread = null;
  paintThread();
  toast("已清除上下文，下一句当新问题");
};

/* ---------------- 最近查询 ----------------
   问过的不用重打。历史只存问句与 SQL，不存结果行。 */
Q.hist = [];

function shortQ(q, n) {
  const text = String(q || "").replace(/\s+/g, " ").trim();
  const max = n || 14;
  return text.length > max ? text.slice(0, max) + "…" : text;
}

function paintHistory() {
  const bar = Q.get("qHistBar");
  const list = Q.get("qHistList");
  if (!bar || !list) return;
  const items = (Q.hist || []).slice(0, 6);
  bar.hidden = !items.length;
  list.innerHTML = items.map((h, i) =>
    `<button type="button" class="q-hist-item" data-h="${i}"
       title="${qEsc(h.ts + " · " + (h.rows || 0) + " 行")}">${qEsc(shortQ(h.question))}</button>`
  ).join("");
  list.querySelectorAll("[data-h]").forEach((btn) => {
    btn.onclick = () => {
      const h = Q.hist[Number(btn.dataset.h)];
      if (!h) return;
      Q.get("qQuestion").value = h.question || "";
      showSql(h.sql, h.question || "", true);
      Q.get("qErr").hidden = true;
      Q.get("qSqlWarn").hidden = true;
      renderOrigin({ origin: "assist", origin_label: "历史查询",
                     source: "来自 " + h.ts });
      setThread(h.question, h.sql);
      clearEmpty();
    };
  });
}

async function loadHistory() {
  try {
    const d = await qApi("/api/nlq/history");
    Q.hist = d.items || [];
    paintHistory();
  } catch (e) {
    /* 后端还没有这个接口时静默：历史是加分项，不该让页面报错 */
  }
}

Q.get("qHistClear").onclick = async () => {
  if (!confirm("清空最近查询记录？")) return;
  try {
    await qApi("/api/nlq/history/clear", {});
    Q.hist = [];
    paintHistory();
    toast("已清空");
  } catch (e) { toast("清空失败：" + e.message); }
};

/* 结果表筛选 / 排序的入口 */
Q.get("qFilter").addEventListener("input", paintTable);
Q.get("qClearFilter").onclick = () => {
  Q.sort = null;
  Q.get("qFilter").value = "";
  paintTable();
};

/* ---------------- 生成 / 执行 ---------------- */
/* 空状态只在「这次会话第一次出结果之前」出现，一旦有 SQL 或结果就撤掉 */
function clearEmpty() {
  const e = Q.get("qEmpty");
  if (e) e.hidden = true;
}

function showSql(sql, question, generated) {
  clearEmpty();
  Q.get("qSqlCard").hidden = false;
  Q.get("qSql").value = sql;
  Q.get("qSqlMeta").textContent = question ? "来自：" + question : "";
  Q.get("qErr").hidden = true;
  // 记住「机器给的原版」，用户改动后据此判断要不要亮出「保存修正」
  if (generated) Q.generatedSql = sql;
  if (Q.get("qSaveCorrect")) syncCorrectBtn();
}

/* 极简 Markdown：只支持模型真会用的几种（代码块 / 表格 / 列表 / 小标题 / 行内代码与粗体）。
   不引第三方库——桌面包里多一个依赖就多一份体积与升级成本。 */
function mdLite(src) {
  const fences = [];
  let text = String(src || "").replace(/\r\n/g, "\n");
  text = text.replace(/```[ \t]*[A-Za-z]*[ \t]*\n([\s\S]*?)```/g, (_, code) => {
    fences.push(code.replace(/\n+$/, ""));
    return `\u0000F${fences.length - 1}\u0000`;
  });

  const inline = (s) => qEsc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");

  const cells = (row) => row.trim().replace(/^\|/, "").replace(/\|$/, "")
    .split("|").map((c) => c.trim());

  const out = [];
  const lines = text.split("\n");
  let list = null;
  const flush = () => { if (list) { out.push(`</${list}>`); list = null; } };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    // 表格：本行以 | 开头、下一行是 |---|--- 分隔行
    if (/^\s*\|/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      flush();
      const head = cells(line);
      i += 2;
      const body = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) { body.push(cells(lines[i])); i++; }
      i--;
      out.push('<div class="q-tbl"><table><thead><tr>' +
        head.map((c) => `<th>${inline(c)}</th>`).join("") + "</tr></thead><tbody>" +
        body.map((r) => "<tr>" + r.map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>").join("") +
        "</tbody></table></div>");
      continue;
    }

    const fence = line.match(/^\u0000F(\d+)\u0000$/);
    if (fence) {
      flush();
      out.push(`<pre class="q-code"><code>${qEsc(fences[Number(fence[1])] || "")}</code></pre>`);
      continue;
    }

    const head = line.match(/^(#{1,4})\s+(.*)$/);
    if (head) { flush(); out.push(`<h4>${inline(head[2])}</h4>`); continue; }

    const ul = line.match(/^\s*[-*•]\s+(.*)$/);
    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ul || ol) {
      const want = ul ? "ul" : "ol";
      if (list !== want) { flush(); out.push(`<${want}>`); list = want; }
      out.push(`<li>${inline((ul || ol)[1])}</li>`);
      continue;
    }

    if (!line.trim()) { flush(); continue; }

    flush();
    out.push(`<p>${inline(line)}</p>`);
  }
  flush();
  return out.join("");
}

/* 文字类动作的标题：同一段回答，叫「SQL 解释」还是「问题在哪」，用户一眼就知道在说什么 */
const ANSWER_TITLE = {
  chat: "回答", explain: "SQL 解释", optimize: "改了什么", fix: "问题在哪",
  dialect: "转换说明", sample: "样例说明", schema: "表结构",
  run_explain: "结果说明", dryrun: "说明",
};

function renderAnswer(d) {
  const card = Q.get("qAnswerCard");
  if (!card) return;
  const text = (d.answer || "").trim();
  if (!text) { card.hidden = true; return; }
  card.hidden = false;
  Q.get("qAnswerTitle").textContent = ANSWER_TITLE[d.action] || "回答";
  Q.get("qAnswerMeta").textContent = d.source || "";
  Q.get("qAnswer").innerHTML = mdLite(text);
}

function renderSqlWarn(d) {
  const box = Q.get("qSqlWarn");
  if (!box) return;
  // 执行过的不用警告；只生成 / 改写的才需要「这条还没跑」
  const show = !!(d.sql && d.executed === false && d.warning);
  box.hidden = !show;
  if (show) box.textContent = d.warning;
}

function renderResult(d, ms) {
  clearEmpty();
  window.PP6QueryExplanation(d);
  if (d.clarify) return renderClarify(d);

  renderAnswer(d);
  renderSqlWarn(d);

  // 只有真执行出一批列，才谈得上「结果」。解释 / 问答 / 只生成都没有结果集，
  // 硬塞一张空结果卡会让人以为「查询成功但 0 行」。
  const hasResult = !!(d.columns && d.columns.length);
  if (!hasResult) {
    Q.get("qResultCard").hidden = true;
    Q.last = d;
    return;
  }

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
  const oLabel = d.origin_label || o.label;
  // 500 行是硬上限。不标出来，用户会拿「前 500 行」当全量去汇报
  const trunc = d.rows.length >= 500
    ? ' <span class="src-warn" title="单次最多返回 500 行，结果可能没取全；要全量请用聚合或加筛选">可能被截断</span>'
    : "";
  const slow = ms && ms > 5000
    ? ` <span class="src-warn" title="超过 5 秒，可加时间范围或索引">较慢</span>`
    : "";
  meta.innerHTML = `${d.rows.length} 行${ms ? " · " + ms + " ms" : ""}${trunc}${slow}` +
    ` <span class="src-badge ${o.cls} sm" title="${qEsc(o.tip)}">${o.icon}${qEsc(oLabel)}</span>`;
  Q.last = d;
  if (!d.rows.length) {
    // 0 行时给出诊断（表空？条件太严？取值写错？），别只丢一句"没有数据"
    const hint = d.hint
      ? `<div class="empty"><b>查询成功，无匹配结果</b><div class="empty-hint">${qEsc(d.hint)}</div></div>`
      : '<div class="empty">查询成功，无匹配数据。</div>';
    Q.get("qResult").innerHTML = hint;
    if (Q.get("qTools")) Q.get("qTools").hidden = true;
    return;
  }
  // 每次换结果都重置筛选与排序，否则上一轮的排序会「莫名」套在新结果上
  Q.sort = null;
  if (Q.get("qFilter")) Q.get("qFilter").value = "";
  if (Q.get("qTools")) Q.get("qTools").hidden = false;
  paintTable();
}

/* 结果表：前端筛选 + 点表头排序。
   几百行里找一行，比回头改 SQL 加 WHERE 快得多；也避免为了多看一眼再花一次模型调用。 */
function paintTable() {
  const d = Q.last;
  if (!d || !d.columns || !d.rows) return;
  const kw = (Q.get("qFilter") ? Q.get("qFilter").value : "").trim().toLowerCase();
  let rows = d.rows;
  if (kw) {
    rows = rows.filter((r) => r.some((v) => v !== null && String(v).toLowerCase().includes(kw)));
  }

  const sort = Q.sort;
  if (sort) {
    rows = rows.slice().sort((a, b) => {
      const x = a[sort.col], y = b[sort.col];
      let cmp;
      if (x === null || x === "") cmp = 1;
      else if (y === null || y === "") cmp = -1;
      else if (x !== null && y !== null && !isNaN(Number(x)) && !isNaN(Number(y))) {
        cmp = Number(x) - Number(y);
      } else {
        cmp = String(x).localeCompare(String(y), "zh-CN");
      }
      return sort.dir === "desc" ? -cmp : cmp;
    });
  }

  const head = d.columns.map((c, i) => {
    const on = sort && sort.col === i;
    const mark = on ? (sort.dir === "desc" ? " ↓" : " ↑") : "";
    return `<th data-col="${i}" class="q-th${on ? " on" : ""}" title="点击排序">${qEsc(c)}${mark}</th>`;
  }).join("");
  const body = rows.map((r) => `<tr>${r.map((v) =>
    v === null || v === ""
      ? '<td class="null">—</td>'
      : `<td>${qEsc(String(v).length > 80 ? String(v).slice(0, 80) + "…" : v)}</td>`
  ).join("")}</tr>`).join("");

  Q.get("qResult").innerHTML = rows.length
    ? `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`
    : `<div class="empty">没有匹配「${qEsc(kw)}」的行</div>`;

  Q.get("qResult").querySelectorAll("th[data-col]").forEach((th) => {
    th.onclick = () => {
      const col = Number(th.dataset.col);
      // 升序 → 降序 → 取消，三下回到默认顺序
      Q.sort = (!Q.sort || Q.sort.col !== col) ? { col, dir: "asc" }
        : (Q.sort.dir === "asc" ? { col, dir: "desc" } : null);
      paintTable();
    };
  });

  const cnt = Q.get("qFilterCount");
  if (cnt) cnt.textContent = (kw || sort)
    ? `${rows.length} / ${d.rows.length} 行` : "";
  const clear = Q.get("qClearFilter");
  if (clear) clear.hidden = !(kw || sort);
}

/* 缺时间范围时只问一句，点选项补进问题里再查；不打断别的查询 */
function renderClarify(d) {
  window.PP6QueryExplanation(null);
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
      // 补完条件用当前菜单的动作重跑：Ask·自动直接交给大模型，Agent 会查证
      ask({ clarified: true });
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
    window.PP6QueryExplanation(null);
    // 执行报错把原文带上：模型拿到报错基本一次就能改对
    renderErr("执行失败：" + e.message, { sql, error: String(e.message) });
    Q.get("qResultCard").hidden = true;
  }
}

/* 流式问答：AI 生成时把 SQL 实时写进框里，不用盯着按钮转圈。

   SSE 帧格式是 `data: {json}\n\n`，浏览器按 chunk 给到，所以要带缓冲地切帧
   ——一个 chunk 可能是半帧，也可能一次给好几帧。
   服务端不支持流式、或非 200 时抛 stream-unavailable，由调用方退回同步接口。 */
async function askStream(payload, onDelta, onEvent) {
  const res = await fetch("/api/nlq/ask-stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok || !res.body) throw new Error("stream-unavailable");

  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  let result = null;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const frames = buf.split("\n\n");
    buf = frames.pop();                 // 最后一段可能是半帧，留到下一轮
    for (const frame of frames) {
      const line = frame.split("\n").find((l) => l.startsWith("data:"));
      if (!line) continue;              // 心跳 / 空帧
      let ev;
      try { ev = JSON.parse(line.slice(5).trim()); } catch { continue; }
      if (ev.stage === "delta") onDelta(ev.delta || "");
      else if (ev.stage === "tool") { if (onEvent) onEvent(ev); }
      else if (ev.stage === "done") result = ev.result;
      else if (ev.stage === "error") throw new Error(ev.error || "生成失败");
    }
  }
  if (!result) throw new Error("没有拿到结果，请重试");
  return result;
}

/* 查证模式的进度文案：让用户知道模型此刻在干什么，而不是干等 */
function toolLabel(ev) {
  const a = ev.args || {};
  if (ev.tool === "list_tables") return "查找表「" + (a.keyword || "全部") + "」";
  if (ev.tool === "describe_table") return "看 " + (a.table || "") + " 的字段";
  if (ev.tool === "sample_values") {
    return "看 " + (a.table || "") + "." + (a.column || "") + " 的实际取值";
  }
  if (ev.tool === "run_sql") return "试跑一条查询验证条件";
  return ev.tool || "调用工具";
}

/* 哪些动作的回答是文字（进回答卡），其余是 SQL（进 SQL 框） */
const TEXT_ACTIONS = new Set(["chat", "explain", "schema"]);

/* 错误条：除了原因，还给一个「用 AI 修复」——把报错原样交给模型，
   比自己盯着 MySQL 错误码猜快得多。 */
function renderErr(msg, fix) {
  const box = Q.get("qErr");
  if (!box) return;
  box.hidden = false;
  box.innerHTML = '<div class="q-err-text"></div>' +
    (fix ? '<button class="btn ghost sm" id="qFixWithAi">用 AI 修复这条 SQL</button>' : "");
  box.querySelector(".q-err-text").textContent = msg;
  const btn = document.getElementById("qFixWithAi");
  if (btn) {
    btn.onclick = () => ask({ mode: "ask", action: "fix", sql: fix.sql, error: fix.error });
  }
}

/* 统一入口。opts 可覆盖 mode / action / question / sql / error，
   「用 AI 修复」就是把当前 SQL 和报错一起带进来重跑。 */
async function ask(opts = {}) {
  const mode = opts.mode || ACT.mode;
  const action = opts.action || ACT.picked[mode] || "auto";
  const spec = actSpec(mode, action);
  const question = (opts.question !== undefined ? opts.question : Q.get("qQuestion").value).trim();
  const sqlText = (opts.sql !== undefined ? opts.sql : Q.get("qSql").value).trim();
  const errText = opts.error || "";

  if (!question && !sqlText) { toast("请先输入查询需求"); return; }
  if (spec.needs_sql && !sqlText) {
    Q.get("qSqlCard").hidden = false;
    Q.get("qSql").focus();
    toast("这个动作需要一条 SQL：先用「生成 SQL」，或把 SQL 贴进下面的框");
    return;
  }

  Q.get("qRuleBox").hidden = true;
  Q.get("qErr").hidden = true;
  const btn = Q.get("qAsk");
  const labelEl = Q.get("qAskLabel");
  btn.disabled = true;
  if (labelEl) labelEl.textContent = "处理中…";

  const isText = TEXT_ACTIONS.has(action);
  const t0 = performance.now();
  let acc = "";

  const paintDelta = (piece) => {
    acc += piece;
    if (isText) {
      Q.get("qAnswerCard").hidden = false;
      Q.get("qAnswerTitle").textContent = ANSWER_TITLE[action] || "回答";
      Q.get("qAnswerMeta").textContent = "生成中…";
      // 流的中间态按纯文本铺，等 done 再用 mdLite 整体渲染（半张表没法解析）
      Q.get("qAnswer").textContent = acc;
    } else {
      const ta = Q.get("qSql");
      ta.value = acc;
      ta.scrollTop = ta.scrollHeight;   // 跟着最新写出来的字往下滚
    }
  };

  try {
    // 先清场：上一次的 SQL / 结果留着，很容易被当成这一次的产物
    if (isText) {
      Q.get("qAnswerCard").hidden = false;
      Q.get("qAnswerTitle").textContent = ANSWER_TITLE[action] || "回答";
      Q.get("qAnswerMeta").textContent = "生成中…";
      Q.get("qAnswer").textContent = "";
      Q.get("qResultCard").hidden = true;
    } else {
      Q.get("qSqlCard").hidden = false;
      Q.get("qSql").value = "";
      Q.get("qSqlMeta").textContent = "";
      Q.get("qAnswerCard").hidden = true;
      Q.get("qResultCard").hidden = true;
    }

    const payload = {
      mode, action, question,
      sql: sqlText || undefined,
      error: errText || undefined,
      target: Q.get("qDialect") ? Q.get("qDialect").value : undefined,
      clarified: !!opts.clarified,
      // 追问：把上一轮的问句与 SQL 交给模型，它才知道「改哪条」
      context: Q.thread ? { question: Q.thread.question, sql: Q.thread.sql } : undefined,
    };

    let d;
    try {
      d = await askStream(payload, paintDelta, (ev) => {
        // 查证模式没 delta，进度就靠这行字告诉用户
        const line = "查证中：" + toolLabel(ev);
        if (isText) Q.get("qAnswerMeta").textContent = line;
        else Q.get("qSqlMeta").textContent = line;
      });
    } catch (e) {
      if (String(e.message) !== "stream-unavailable") throw e;
      d = await qApi("/api/nlq/ask", payload);
    }

    if (d.sql) {
      showSql(d.sql, question, true);
    } else {
      Q.get("qSqlWarn").hidden = true;
      if (!isText) {
        Q.get("qSqlCard").hidden = true;
        Q.get("qOrigin").hidden = true;
      }
    }
    if (d.sql || d.answer) renderOrigin(d);
    if (d.sql) {
      Q.get("qSqlMeta").textContent = (question ? "来自：" + question : "") +
        (d.note ? "　✎ " + d.note : "");   // 复用人工修正时带上他自己写的原因
    }
    if (d.retried) toast("SQL 执行报错，已自动修正");
    if (d.learned) toast("已记录，同类问题将优先使用规则");
    const ruleBox = Q.get("qRuleBox");
    if (d.ruleSql && d.ruleSql !== d.sql) {
      ruleBox.hidden = false;
      Q.get("qRuleSql").value = d.ruleSql;
    } else {
      ruleBox.hidden = true;
    }
    renderResult(d, Math.round(performance.now() - t0));
    if (d.sql) setThread(question, d.sql);
    loadHistory();          // 历史是异步的，不挡结果展示
    const target = isText ? Q.get("qAnswerCard") : Q.get("qResultCard");
    if (target && !target.hidden) {
      target.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  } catch (e) {
    // 清空旧的 SQL：被安全闸拒掉时如果还留着上一次的结果，
    // 很容易被当成「这次生成的」，真的复制去库里跑就麻烦了。
    // 流式吐了一半再失败也算：半截 SQL 比没有更危险。
    if (!isText) Q.get("qSql").value = "";
    Q.get("qRuleBox").hidden = true;
    Q.get("qSqlWarn").hidden = true;
    window.PP6QueryExplanation(null);
    const msg = e.message +
      (String(e.message).includes("API Key")
        ? "\n\n不配模型也能用：换成像「项目的名称和项目经理」这样带上对象名和字段名的说法，会走本地规则解析。"
        : "");
    // 只有「SQL 跑不通」才值得修；缺 Key / 被安全闸拒绝，修 SQL 解决不了
    const fixable = !!sqlText && !/API Key|未配置大模型|需要大模型|拒绝|不是纯只读/.test(String(e.message));
    renderErr(msg, fixable ? { sql: sqlText, error: String(e.message) } : null);
    Q.get("qResultCard").hidden = true;
    if (isText && !acc) Q.get("qAnswerCard").hidden = true;
  } finally {
    btn.disabled = false;
    applyAction();       // 按菜单当前选择恢复按钮文案，别停在「处理中…」
  }
}

/* 复制文本：桌面 WebView 里 navigator.clipboard 不一定可用，退回 execCommand。
   SQL 和回答共用一份，省得两处各写一遍三档降级。 */
async function copyText(text, okMsg) {
  const t = String(text || "");
  if (!t.trim()) { toast("没有可复制的内容"); return false; }

  let done = false;

  // 桌面壳：系统剪贴板最稳（WebView 里 clipboard API 常被禁用）
  const api = window.pywebview && window.pywebview.api;
  if (api && api.copy_text) {
    try {
      const r = await api.copy_text(t);
      done = !!(r && r.ok);
    } catch (_) { done = false; }
  }

  if (!done && navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(t);
      done = true;
    } catch (_) { done = false; }
  }
  if (!done) {
    // 临时 textarea：回答区不是可选中控件，只能借一个能 select 的元素
    const ta = document.createElement("textarea");
    ta.value = t;
    ta.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    try { done = document.execCommand("copy"); } catch (_) { done = false; }
    document.body.removeChild(ta);
  }
  toast(done ? (okMsg || "已复制到剪贴板") : "复制失败，请手动选择后按 ⌘C / Ctrl+C");
  return done;
}

async function copySql() {
  const text = Q.get("qSql").value;
  if (!text.trim()) { toast("暂无 SQL 可复制"); return; }
  const done = await copyText(text, "SQL 已复制到剪贴板");
  if (done) {
    const btn = Q.get("qCopySql");
    const old = btn.textContent;
    btn.textContent = "已复制 ✓";
    setTimeout(() => { btn.textContent = old; }, 1400);
  }
}

Q.get("qCopySql").onclick = copySql;
Q.get("qCopyAnswer").onclick = () => copyText(Q.get("qAnswer").innerText || "",
                                              "回答已复制到剪贴板");
Q.get("qRetryAi").onclick = () => ask({ mode: "agent", action: "query" });
Q.get("qVerify").onclick = async () => {
  const question = Q.get("qQuestion").value.trim();
  const sql = Q.get("qSql").value.trim();
  if (!question || !sql) { toast("请先执行一次查询"); return; }
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
    maskSecret(Q.get("qKey"), false, "粘贴平台的 Key（sk-...）");
    AI_READY = false;
    AI_BRIEF = "";
    paintAiState();
    toast("已清除 Key");
  } catch (e) { toast("清除失败：" + e.message); }
};

/* ---------------- 工作方式与动作：照 DBX 的 Ask / Agent ----------------
   Ask   = 只生成 / 解释 / 改写，不碰库（解释 SQL 没配 Key 也能给语法层摘要）
   Agent = 可以先查表结构、看取值、试跑，证据够了再落笔
   Ask·自动 = 直接交给大模型；没配 Key 或模型全挂才回退本地规则
   （规则引擎是离线兜底，不是主路径——2026-11-27 用户明确要求）。
   动作清单由 /api/nlq/actions 下发，前端不写死。 */
const ACT = {
  actions: { ask: [], agent: [] },
  targets: [],
  ready: { llm: false, db: false },
  mode: "ask",
  picked: { ask: "auto", agent: "auto" },
};

try {
  if (localStorage.getItem("pp6.assistMode") === "agent") ACT.mode = "agent";
  const a = localStorage.getItem("pp6.assistAction.ask");
  const g = localStorage.getItem("pp6.assistAction.agent");
  if (a) ACT.picked.ask = a;
  if (g) ACT.picked.agent = g;
} catch (e) { /* 桌面壳里 localStorage 可能被禁，用默认值 */ }

/* 后端拿不到时的兜底清单：空菜单等于功能不存在 */
const ACT_FALLBACK = {
  ask: [
    { id: "auto", label: "自动", icon: "sparkle", hint: "大模型直接生成，不可用时回退本地", needs_sql: false },
    { id: "chat", label: "通用问答", icon: "chat", hint: "问业务概念与数据口径，不生成 SQL", needs_sql: false },
    { id: "generate", label: "生成 SQL", icon: "wand", hint: "只生成不执行，可以先改再跑", needs_sql: false },
    { id: "explain", label: "解释 SQL", icon: "help", hint: "逐段讲清这条 SQL 在查什么", needs_sql: true },
    { id: "optimize", label: "优化 SQL", icon: "bolt", hint: "改写为更快更稳的写法", needs_sql: true },
    { id: "fix", label: "修复错误", icon: "wrench", hint: "按报错信息修正 SQL", needs_sql: true },
    { id: "dialect", label: "转换方言", icon: "swap", hint: "MySQL 转成其它数据库方言", needs_sql: true },
    { id: "sample", label: "生成样例", icon: "rows", hint: "生成测试数据语句，绝不执行", needs_sql: false },
  ],
  agent: [
    { id: "auto", label: "自动", icon: "sparkle", hint: "有 SQL 就执行并解释，没有就查数据", needs_sql: false },
    { id: "chat", label: "通用问答", icon: "chat", hint: "先查证再回答，不生成 SQL", needs_sql: false },
    { id: "query", label: "查询数据", icon: "play", hint: "生成 SQL 并执行", needs_sql: false },
    { id: "schema", label: "查看表结构", icon: "table", hint: "字段含义、类型、外键与空列", needs_sql: false },
    { id: "run_explain", label: "执行并解释", icon: "play-help", hint: "跑完再用人话讲结果", needs_sql: false },
    { id: "dryrun", label: "生成但不执行", icon: "pause", hint: "只给 SQL，不碰库", needs_sql: false },
  ],
};

ACT.actions = ACT_FALLBACK;

/* 线性小图标，观感对齐 DBX 那套菜单 */
const ACT_ICONS = {
  sparkle: '<path d="M8 1.8l1.35 3.45L12.8 6.6l-3.45 1.35L8 11.4 6.65 7.95 3.2 6.6l3.45-1.35z"/>',
  chat: '<path d="M3.1 2.9h9.8v7.2H6.6L3.1 12.7v-2.6h0z"/>',
  wand: '<path d="M3.2 12.2l6.2-6.2"/><path d="M10.2 1.9l.5 1.2 1.2.5-1.2.5-.5 1.2-.5-1.2-1.2-.5 1.2-.5z"/>',
  help: '<circle cx="8" cy="8" r="5.7"/><path d="M6.3 6.1a1.75 1.75 0 011.7-1.15c1.02 0 1.72.6 1.72 1.42 0 1.32-1.6 1.18-1.6 2.5"/><circle cx="8" cy="11.2" r=".65" fill="currentColor" stroke="none"/>',
  bolt: '<path d="M9.3 1.9L4 8.7h3.2L6.6 14l5.3-6.9H8.6z"/>',
  wrench: '<path d="M10.8 2.2a3.25 3.25 0 00-3.1 4.25l-4 4a1.55 1.55 0 002.15 2.15l4-4A3.25 3.25 0 0014 5.5l-1.95 1.95-1.4-.4-.4-1.4z"/>',
  swap: '<path d="M2.6 5.5h9.1M9.3 3.3l2.4 2.2-2.4 2.2M13.4 10.5H4.3M6.7 8.3l-2.4 2.2 2.4 2.2"/>',
  rows: '<rect x="2.7" y="2.9" width="10.6" height="10.2" rx="1.4"/><path d="M2.7 6.3h10.6M2.7 9.7h10.6M6.2 6.3v6.8"/>',
  play: '<path d="M5.1 3.3l6.9 4.7-6.9 4.7z"/>',
  table: '<rect x="2.7" y="2.9" width="10.6" height="10.2" rx="1.4"/><path d="M2.7 6.3h10.6M6.8 6.3v6.8"/>',
  "play-help": '<path d="M4.6 3.3l6.4 4.4-6.4 4.4z"/><path d="M12.5 9.4a2.1 2.1 0 11-1.7 3.35"/>',
  pause: '<rect x="4.5" y="3.3" width="2.5" height="9.4" rx=".6"/><rect x="9" y="3.3" width="2.5" height="9.4" rx=".6"/>',
};

function actIcon(id) {
  const d = ACT_ICONS[id] || ACT_ICONS.sparkle;
  return `<svg class="q-act-i" viewBox="0 0 16 16" aria-hidden="true">${d}</svg>`;
}

/* 主按钮说什么、左下提示什么。
   按钮文案写错比没按钮更糟：按「生成」的预期点了「执行」，就真的跑了库。 */
const ACTION_META = {
  auto:        { btn: "生成 SQL 并执行", hint: "大模型直接生成，不可用时回退本地" },
  chat:        { btn: "提问",             hint: "只回答，不生成 SQL" },
  generate:    { btn: "生成 SQL",         hint: "只生成，不执行" },
  explain:     { btn: "解释这条 SQL",     hint: "逐段讲清在查什么，不执行" },
  optimize:    { btn: "优化这条 SQL",     hint: "改写为更快更稳，不执行" },
  fix:         { btn: "修复这条 SQL",     hint: "按报错信息修正，不执行" },
  dialect:     { btn: "转换方言",         hint: "只换写法，不执行" },
  sample:      { btn: "生成样例数据",     hint: "只生成，绝不执行" },
  query:       { btn: "生成 SQL 并执行", hint: "先查证再落笔，直接出结果" },
  schema:      { btn: "查看表结构",       hint: "字段、类型、外键与空列" },
  run_explain: { btn: "执行并解释",       hint: "跑完再用人话讲结果" },
  dryrun:      { btn: "生成但不执行",     hint: "查证后给 SQL，不碰库" },
};

function actSpec(mode, id) {
  return (ACT.actions[mode] || []).find((a) => a.id === id) || {};
}

function currentAction() {
  return ACT.picked[ACT.mode] || "auto";
}

function modeLabel(mode) {
  return mode === "agent" ? "Agent" : "Ask";
}

function renderActList() {
  const list = Q.get("qActList");
  if (!list) return;
  const acts = ACT.actions[ACT.mode] || [];
  list.innerHTML = acts.map((a) => {
    const on = a.id === (ACT.picked[ACT.mode] || "auto");
    const need = a.needs_llm && !ACT.ready.llm;
    return `<button type="button" class="q-act-item${on ? " on" : ""}" data-act="${a.id}"
              role="menuitemradio" aria-checked="${on}">
      ${actIcon(a.icon)}
      <span class="q-act-text"><b>${qEsc(a.label)}</b><small>${qEsc(a.hint || "")}</small></span>
      ${need ? '<span class="q-act-need" title="在「系统设置」里填 API Key 后可用">需配 Key</span>' : ""}
      ${on ? '<svg class="q-act-check" viewBox="0 0 16 16" aria-hidden="true"><path d="M3.2 8.4l3.2 3.2 6.4-6.6"/></svg>' : ""}
    </button>`;
  }).join("");
  list.querySelectorAll("[data-act]").forEach((btn) => {
    btn.onclick = () => pickAction(btn.dataset.act);
  });
}

/* 界面上所有跟动作有关的东西，一处改完 */
function applyAction() {
  const act = currentAction();
  const meta = ACTION_META[act] || ACTION_META.auto;
  const label = Q.get("qActLabel");
  if (label) label.textContent = `${modeLabel(ACT.mode)} · ${actSpec(ACT.mode, act).label || "自动"}`;
  const btnLabel = Q.get("qAskLabel");
  if (btnLabel) btnLabel.textContent = meta.btn;
  const hint = Q.get("qModeHint");
  if (hint) hint.textContent = meta.hint;

  document.querySelectorAll("#qActTabs button").forEach((b) => {
    b.classList.toggle("on", b.dataset.tab === ACT.mode);
  });
  const dw = Q.get("qDialectWrap");
  if (dw) dw.hidden = !(ACT.mode === "ask" && act === "dialect");

  // 需要 SQL 的动作：把 SQL 卡亮出来，否则用户不知道要处理的 SQL 该贴哪儿
  const spec = actSpec(ACT.mode, act);
  if (spec.needs_sql && !Q.get("qSql").value.trim()) {
    Q.get("qSqlCard").hidden = false;
    Q.get("qSql").placeholder = `把要${spec.label || "处理"}的 SQL 贴在这里，或在问题框里描述需求`;
  }

  renderActList();
}

function pickAction(id) {
  ACT.picked[ACT.mode] = id;
  try { localStorage.setItem("pp6.assistAction." + ACT.mode, id); } catch (e) { /* 同上 */ }
  closeActMenu();
  applyAction();
}

function switchActTab(mode) {
  ACT.mode = mode === "agent" ? "agent" : "ask";
  try { localStorage.setItem("pp6.assistMode", ACT.mode); } catch (e) { /* 同上 */ }
  applyAction();
}

function openActMenu() {
  const menu = Q.get("qActMenu");
  const btn = Q.get("qActBtn");
  if (!menu) return;
  menu.hidden = false;
  if (btn) btn.setAttribute("aria-expanded", "true");
  renderActList();
}

function closeActMenu() {
  const menu = Q.get("qActMenu");
  const btn = Q.get("qActBtn");
  if (menu) menu.hidden = true;
  if (btn) btn.setAttribute("aria-expanded", "false");
}

function fillDialects() {
  const sel = Q.get("qDialect");
  if (!sel || sel.dataset.filled) return;
  const list = ACT.targets.length ? ACT.targets : ["PostgreSQL", "SQL Server", "Oracle", "SQLite"];
  sel.innerHTML = list.map((t) => `<option>${qEsc(t)}</option>`).join("");
  sel.dataset.filled = "1";
}

async function loadActions() {
  try {
    const d = await qApi("/api/nlq/actions");
    if (d && d.actions && d.actions.ask && d.actions.ask.length) {
      ACT.actions = d.actions;
      ACT.targets = d.targets || [];
      ACT.ready = d.ready || ACT.ready;
      // 版本升级后动作可能改名或删除，存过的值要回落到自动
      for (const m of ["ask", "agent"]) {
        if (!(ACT.actions[m] || []).some((a) => a.id === ACT.picked[m])) ACT.picked[m] = "auto";
      }
      fillDialects();
      applyAction();
    }
  } catch (e) {
    /* 后端还没升级也别让菜单空着：继续用内置清单 */
  }
}

(function initActMenu() {
  const btn = Q.get("qActBtn");
  if (btn) {
    btn.onclick = (e) => {
      e.stopPropagation();
      if (Q.get("qActMenu").hidden) openActMenu(); else closeActMenu();
    };
  }
  document.querySelectorAll("#qActTabs button").forEach((b) => {
    b.onclick = (e) => { e.stopPropagation(); switchActTab(b.dataset.tab); };
  });
  // 点空白关菜单；菜单内部不关
  document.addEventListener("click", (e) => {
    const menu = Q.get("qActMenu");
    if (!menu || menu.hidden) return;
    if (!menu.contains(e.target) && !e.target.closest("#qActBtn")) closeActMenu();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeActMenu();
  });
  fillDialects();
  applyAction();
})();

Q.get("qAsk").onclick = () => ask();

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
renderTemplates();      // 先用本地兜底问法把空状态填满，连库后会被真实模板替换
loadConfig();
loadActions();          // 动作菜单：拿到后端清单后替换内置兜底
loadHistory();          // 最近查询：问过的不用重打

/* 首屏把光标放进输入框：打开就能打字，省掉「先点一下」这一步。
   preventScroll 防止长页面被浏览器拽到输入框位置。 */
window.addEventListener("load", () => {
  const t = document.getElementById("aiChatText1");
  if (!t || t.value) return;
  try { t.focus({ preventScroll: true }); } catch (_) { t.focus(); }
});
