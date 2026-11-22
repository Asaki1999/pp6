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
window.PP6_FEATURES = { patch: true, inspect: false };
const PATCH_ON = () => window.PP6_FEATURES.patch !== false;

/* 按开关调整导航：隐藏的工作区按钮 / 快捷键说明 / 关于页文案 */
function applyFeatureFlags() {
  const on = PATCH_ON();
  const btn = document.querySelector('.mode[data-feature="patch"]');
  if (btn) btn.hidden = !on;
  /* 反解入口：2026-11-18 下线（与存量包修订的「加字段」重叠，且重造包会丢未建模内容）。
     inspectZip()、反解摘要卡片、/api/inspect、X-Package-Summary 全部保留没删；
     恢复：把上面 inspect 改成 true，HTML 里那个 hidden 会被这里覆盖掉。 */
  const inspectBtn = document.getElementById("btnInspect");
  if (inspectBtn) inspectBtn.hidden = window.PP6_FEATURES.inspect !== true;
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
  import:   { title: "普通模式", desc: "普通业务对象及其表单、列表、数据集，生成一个导入包（导入路径：发布管理 › 导入同步包）" },
  patch:    { title: "存量包修订", desc: "在已有导入包上新增字段或修改字段名称；类型、选项等定义不可修改，也不删除字段，其余内容保持不变" },
  query:    { title: "数据查询",   desc: "用自然语言查询数据库，生成 SQL 并查看结果" },
  workflow: { title: "流程模式",   desc: "操作与普通模式一致；固定 开始 › 审核 › 通过，导出时另附流程 XML" },
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
  sel.innerHTML = `<option value="">选择平台后自动填充</option>` +
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

    Q_PROVIDERS = d.providers || [];
    renderProviders(Q_PROVIDERS);
    if (d.instructions) Q.get("qInstr").value = d.instructions;

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
  if (pillText) pillText.textContent = AI_READY ? "AI 已配置" : "AI 未配置";

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
  if (!base) { toast("请先选一个平台，或手填接口地址"); return; }
  if (!model) { toast("请填写模型名称（选择平台后自动填充）"); return; }
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
  "每个项目状态有多少个项目",
  "列出执行中的项目，按创建时间倒序",
  "本月新增的任务按负责人统计",
  "风险等级为「高」的项目有哪些",
];

function renderTemplates() {
  const box = Q.get("qTemplates");
  if (!box) return;
  if (!Q.templates.length) {
    box.innerHTML = `<span class="q-tpl-note">示例问法：</span>`
      + Q_FALLBACK.map((q) => `<button data-fill="1">${qEsc(q)}</button>`).join("");
    box.querySelectorAll("button[data-fill]").forEach((btn) => {
      btn.onclick = () => {
        const input = Q.get("qQuestion");
        if (!input) return;
        input.value = btn.textContent;
        input.focus();
      };
    });
    return;
  }
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

function renderResult(d, ms) {
  clearEmpty();
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
      : '<div class="empty">查询成功，无匹配数据。</div>';
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

async function ask(engine, clarified) {
  Q.get("qRuleBox").hidden = true;
  const question = Q.get("qQuestion").value.trim();
  if (!question) { toast("请先输入查询需求"); return; }
  const btn = Q.get("qAsk");
  btn.disabled = true; btn.textContent = "生成中…";
  const t0 = performance.now();
  let acc = "";
  try {
    // 流式优先：能一边生成一边看 SQL，长 SQL 也不用干等
    Q.get("qSqlCard").hidden = false;
    Q.get("qSql").value = "";
    Q.get("qSqlMeta").textContent = "";
    let d;
    try {
      d = await askStream({ question, engine, clarified: !!clarified }, (piece) => {
        acc += piece;
        // 生成中先显示原始输出，done 之后 showSql 会用校验过的版本覆盖
        const ta = Q.get("qSql");
        ta.value = acc;
        ta.scrollTop = ta.scrollHeight;   // 跟着最新写出来的字往下滚
      }, (ev) => {
        // 查证模式：工具调用没有 delta，进度就靠这行字告诉用户
        Q.get("qSqlMeta").textContent = "查证中：" + toolLabel(ev);
      });
    } catch (e) {
      if (String(e.message) !== "stream-unavailable") throw e;
      d = await qApi("/api/nlq/ask", { question, engine, clarified: !!clarified });
    }
    if (d.sql) {
      showSql(d.sql, question, true);
      renderOrigin(d);
    } else {
      Q.get("qSqlCard").hidden = true;
      Q.get("qOrigin").hidden = true;
    }
    Q.get("qSqlMeta").textContent = (question ? "来自：" + question : "") +
      (d.note ? "　✎ " + d.note : "");   // 复用人工修正时带上他自己写的原因
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
    // 结果在 SQL 卡片下面，长 SQL 会把它挤出视野；直接带用户过去
    Q.get("qResultCard").scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (e) {
    Q.get("qSqlCard").hidden = false;
    // 清空旧的 SQL：被安全闸拒掉时如果还留着上一次的结果，
    // 很容易被当成「这次生成的」，真的复制去库里跑就麻烦了。
    // 流式吐了一半再失败也算：半截 SQL 比没有更危险。
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
  if (!text.trim()) { toast("暂无 SQL 可复制"); return; }

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
    toast("复制失败，请手动选择后按 ⌘C / Ctrl+C");
  }
}

Q.get("qCopySql").onclick = copySql;
Q.get("qRetryAi").onclick = () => ask("llm");
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

/* 生成方式：照 DBX 的 Ask / Agent 双模式。
   ask   = 规则优先，规则没把握才调大模型（pp6 原有的 hybrid，也是默认）
   agent = 查证：先 list/describe/sample/试跑，再落笔
   模式是切换器而不是第三个按钮——DBX 也是这么分层的。 */
let ASK_MODE = "ask";
try {
  if (localStorage.getItem("pp6.askMode") === "agent") ASK_MODE = "agent";
} catch (e) { /* 桌面壳里 localStorage 可能被禁，忽略即可 */ }

(function initAskMode() {
  const box = Q.get("qMode");
  if (!box) return;
  box.querySelectorAll("button").forEach((b) => {
    b.classList.toggle("on", b.dataset.mode === ASK_MODE);
    b.onclick = () => {
      ASK_MODE = b.dataset.mode === "agent" ? "agent" : "ask";
      try { localStorage.setItem("pp6.askMode", ASK_MODE); } catch (e) { /* 同上 */ }
      box.querySelectorAll("button").forEach((x) => {
        x.classList.toggle("on", x === b);
      });
    };
  });
})();

Q.get("qAsk").onclick = () => ask(ASK_MODE === "agent" ? "agent" : "hybrid");

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

/* 首屏把光标放进输入框：打开就能打字，省掉「先点一下」这一步。
   preventScroll 防止长页面被浏览器拽到输入框位置。 */
window.addEventListener("load", () => {
  const t = document.getElementById("aiChatText1");
  if (!t || t.value) return;
  try { t.focus({ preventScroll: true }); } catch (_) { t.focus(); }
});
