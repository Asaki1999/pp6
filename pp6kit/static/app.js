const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const TYPE_LABEL = {
  varchar: "单行文本",
  clob: "多行文本",
  number: "数字",
  datetime: "日期时间",
  user: "人员",
  department: "部门",
  object: "关联对象",
  dic: "字典",
  autoNumber: "自动编号",
  userGroupCate: "用户组",
};

const KIND_LABEL = {
  create: "新建表单", detail: "详情表单", condition: "条件专用", import: "导入专用", bundle: "标准全套",
};

const CTRL_LABEL = {
  text: "文本框",
  editor: "富文本",
  select: "下拉选择",
  // 字典控件在系统里的真实名字是 dictionary（不是 select），
  // 少这一条预览就会直接露出英文
  dictionary: "数据字典",
  linkdata: "关联数据",
  dropSelector: "人员选择",
  dropSelectOrg: "部门选择",
  autoNumber: "自动编号",
  attachment: "附件",
  number: "数字",
  date: "日期",
  // datetime 字段线上用的是 datePicker，date 仍保留给老的 date 类型
  datePicker: "日期选择",
  // clob 字段
  textarea: "多行文本",
  hidden: "隐藏",
};

/* ---------------------------------- 第 1 步：业务对象关联项目（两档，导入前就定）
 *
 * 以前这一层是**导入后弹出的第 1 步弹窗**：传完 Excel、解析一次，再问「哪些对象
 * 要关联项目」，点「开始分析」又解析一次。用户的反馈是「顺序反了」——对象级选择
 * 应该在导入前就在初始页定好，导入完直接看结果。
 *
 * 现在只有两档，常驻在上传卡片上方：
 *   off → 全部不关联（显式发 linked:false，Excel 里的「是」不会漏回来）
 *   on  → 全部关联
 * 原来的第三档「按 Excel」被用户砍掉了：要么全关联、要么全不关联，
 * 想看单个对象，用对象卡片上的「关联项目」开关。`1_业务对象` 的「关联项目」
 * 列仍然有效（命令行 / 接口直接传 Excel 时照旧认它），只是应用内以两档为准。
 * 普通模式 / 流程模式共享同一档（localStorage: pp6.plPre），存量包用 pp6.plPrePatch。
 *
 * 协议没变：仍然是 POST /api/parse + X-Project-Link，服务端照旧重新分析、不信任前端。
 */
const PL_PRE_KEY = "pp6.plPre";
const PL_PRE_PATCH_KEY = "pp6.plPrePatch";
const PL_PRE_MODES = ["off", "on"];
const PL_PRE_PATCH_MODES = ["off", "on", "keep"];
const PL_PRE_LABEL = { off: "全部不关联项目", on: "全部关联项目" };
const PL_PRE_PATCH_LABEL = { off: "不关联", on: "关联", keep: "按包内" };

/* 读本地偏好：值不在白名单里（旧版本留下的、手改过的）就当没设过，用默认档。
 * 隐私模式下 localStorage 会抛异常，也不能因此让整页起不来 → 静默退回默认。
 * 注意：老版本存过 "excel"（已删掉的第三档）→ 不在白名单 → 自动落回默认 "off"，
 * 不需要额外的迁移代码。 */
function readPref(key, allowed, fallback) {
  try {
    const store = window.localStorage;
    const raw = store ? store.getItem(key) : null;
    return allowed.indexOf(raw) >= 0 ? raw : fallback;
  } catch (err) {
    return fallback;
  }
}

function savePref(key, value) {
  try {
    if (window.localStorage) window.localStorage.setItem(key, value);
  } catch (err) {
    /* 存不了不影响本次会话，只是下次打开回到默认档 */
  }
}

const state = {
  file: null, data: null, formIndex: 0, desktop: false,
  picked: new Set(), imported: new Set(), aiData: null, aiFile: null,

  // ↓ P0-1 / P0-4 / P0-5：第 7 tab 两态 + 反解 / 出包状态机
  packageMode: "predict",      // "predict" | "actual"
  buildReport: null,           // X-Build-Report 解码对象 | null
  inspectSummary: null,        // X-Package-Summary 解码对象 | null
  zipPhase: { build: "idle", inspect: "idle" },
  // 项目对齐：弹窗只在「同一份文件的第一次解析」自动弹一次（关掉不再打扰）
  plModalShown: false,
  plPending: 0,
  // 分析完先弹「相近字段确认」，用户选完/保留后才渲染预览（结果页）。
  // 这样不会出现「预览已经生成、又被遮罩盖住」的错觉。
  plDeferred: false,
  // 第 1 步「业务对象关联项目」的两档（初始页控件，导入前就定）：
  // "off" 不关联（默认） / "on" 全部关联。
  // 普通模式与流程模式共享同一档，存 localStorage。
  plPre: readPref(PL_PRE_KEY, PL_PRE_MODES, "off"),
  // 存量包修订页的三档："off" 全不勾 / "on" 全勾 / "keep" 按包内（保留现状）
  plPrePatch: readPref(PL_PRE_PATCH_KEY, PL_PRE_PATCH_MODES, "keep"),
  // 对象卡片上的「关联项目」开关：{ 对象编码: true|false }
  // 只记「用户亲手拨过的」（勾/取消都算），没拨过的对象仍按 Excel 的列走；
  // 初始页选了「全部关联 / 不关联」时这里是**全量**决策（每个对象都显式 true/false）
  plLinked: {},
  // 「相近字段确认」弹窗里勾过的字段：{ 对象编码: { 字段编码: true } }
  // 只发勾上的（true）——没勾的就是「保留我方」，服务端不需要知道它们。
  // 换文件时清空（字段编码换了一批），拨开关重解析时保留。
  plAlign: {},
};

/* 桌面壳（pywebview）会注入 window.pywebview.api，同时置 PP6_DESKTOP 供自检读取 */
const native = () => (window.pywebview && window.pywebview.api) || null;

function detectDesktop() {
  if (native()) {
    state.desktop = true;
    window.PP6_DESKTOP = true;
  }
}
window.addEventListener("pywebviewready", detectDesktop);
detectDesktop();

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function toast(msg, ms = 3200) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.hidden = true; }, ms);

  // 状态栏同步显示最近一次操作，操作完自动回到「就绪」
  const status = $("#statusText");
  if (status) {
    status.textContent = msg.length > 52 ? `${msg.slice(0, 52)}…` : msg;
    clearTimeout(status._t);
    status._t = setTimeout(() => { status.textContent = "就绪"; }, ms);
  }
}

function humanSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / 1024 / 1024).toFixed(1) + " MB";
}

function setLoading(on, text = "正在解析…") {
  $("#loading").hidden = !on;
  if (text) $("#loadingText").textContent = text;
}

/* ------------------------------------------------ 上传 */

/* 一次新的解析（上传 / 拨开关）后都要做的清理：
 * 第 7 tab 的实测包结构、进度态都不能跟着旧文件留下来。
 * 所有写入 state.data 的路径（手动上传 / 载入示例 / applyAiBo thenBuild /
 * AI sheet apply）都汇聚到 handleFile()，这里是唯一重置点。 */
function resetParseArtifacts() {
  // 换一份 Excel 就清掉上一次的实测包结构，避免第 7 tab 残留旧包清单。
  state.buildReport = null;
  state.inspectSummary = null;
  state.packageMode = "predict";
  state.zipPhase = { build: "idle", inspect: "idle" };
  const staleBuild = $("#pp6zipBuildReport");
  if (staleBuild) { staleBuild.hidden = true; staleBuild.innerHTML = ""; }
  const staleInspect = $("#pp6zipInspectCard");
  if (staleInspect) { staleInspect.hidden = true; staleInspect.innerHTML = ""; }
}

/* 一次 POST /api/parse。plHeader 非空才带 Content-Type / X-Project-Link。
 * 第一次解析不带决策头（只是为了拿到对象清单，用来构造全量决策）；
 * 服务端不信任前端：带不带头都自己重新分析。 */
async function postParse(buf, plHeader) {
  const opt = { method: "POST", body: buf };
  if (plHeader) {
    opt.headers = {
      "Content-Type": "application/octet-stream",
      "X-Project-Link": plHeader,
    };
  }
  const res = await fetch("/api/parse", opt);
  const data = await res.json();
  if (!data.ok) throw new Error(data.error || "解析失败");
  return data;
}

async function handleFile(file, { fromAi = false, quiet = false } = {}) {
  if (!file) return;
  if (!/\.(xlsx|xlsm)$/i.test(file.name)) {
    toast("请选择 .xlsx 文件");
    return;
  }
  state.file = file;
  setLoading(true);
  try {
    const buf = await file.arrayBuffer();
    let data = await postParse(buf, null);
    /* 第 1 步（对象级选择）已经在**导入前**定了：#plPreImportBar 两档。
     * 这里不再弹窗、也不再 `return` 打断流程 —— 导入完直接出结果。
     *   off / on 都要构造**全量**决策（每个对象都显式 linked true/false）再解析一次，
     *   否则 Excel 里本来是「是」的对象会漏回来（页面没关联、包里有「所属项目」）。
     * Excel 里一个对象都没有时 plPreDecisions() 返回 null，跳过第二次解析。
     * fromAi / quiet 走同一套：示例和 AI 生成也能验证两档。 */
    let decisions = plPreDecisions(data.objects);
    if (decisions) {
      const modeLabel = PL_PRE_LABEL[state.plPre] || state.plPre;
      setLoading(true, `正在按「${modeLabel}」分析…`);
      const prevLinked = state.plLinked;
      state.plLinked = decisions;             // projLinkHeader() 的直接来源
      try {
        data = await postParse(buf, projLinkHeader());
      } catch (err) {
        // 退回第一次解析：结果页照常出，决策清掉（否则包里会带一份
        // 和页面上选的档位对不上的头）
        state.plLinked = prevLinked;
        decisions = null;
        toast(`按「${modeLabel}」分析失败：${err.message}，已按 Excel 里的内容原样分析`, 7000);
      }
    }
    state.data = data;
    resetParseArtifacts();
    // 换了文件 → 项目对齐的弹窗可以再弹一次（决策仍按文件指纹缓存在本地）
    state.plModalShown = false;
    // 「关联项目」开关是按对象编码记的，换了 Excel 编码就换了一批，
    // 不清会给新文件里的同名对象套上上一份的决策（这里只在**换文件**时清，
    // 拨开关的重新解析走 toggleProjLink()，不经过 handleFile）。
    // 初始页选了「全部关联 / 不关联」时，这里的决策是**全量**：没关联的对象也要
    // 显式记 false，只记 true 的话下一次 /api/build 里 Excel 的「是」又会漏回来。
    state.plLinked = decisions ? Object.assign({}, decisions) : {};
    // 换文件 → 上一份的「按项目覆盖」勾选也作废：字段编码换了一批，
    // 留着会给新文件里同名编码的字段套上旧勾选（服务端还会当成有效决策）
    state.plAlign = {};
    // 分析完先弹「相近字段确认」，用户选完/保留后才渲染预览（结果页）。
    // 只在普通导入 / AI 套用走这条；流程页的 quiet 上传仍直接出结果（不抢页面）。
    state.plDeferred = false;
    if (!quiet && window.ProjLink) {
      const vm = data.projectAlign;
      const pending = window.ProjLink.pendingCount(vm);
      const direct = window.ProjLink.directDiffCount
        ? window.ProjLink.directDiffCount(vm) : 0;
      if (pending || direct) {
        state.plDeferred = true;
        state.plModalShown = true;
        setLoading(false);
        window.ProjLink.openModal(vm);
        toast(`分析完成：有 ${pending + direct} 个字段需要确认，确认完再生成预览`, 6000);
        return;
      }
    }
    renderAll();
    // 直接从 AI 预览下来的 Excel → 进第 3 步；手动传的也进第 3 步，
    // 因为「结果」区里就是模块勾选 + 下载，这是流程的终点。
    // quiet：人在「流程模式」页上传，结果区照样准备好，但不要把工作区滚回顶部
    goStep(3, { scroll: !quiet });
    if (quiet) {
      const n = (state.data.objects || []).filter((o) => o.flowDriven).length;
      toast(n
        ? `已解析：${n} 个流程对象，可以导出对象包或下载 XML`
        : "这份 Excel 里没有流程驱动对象：到 1_业务对象 把「流程驱动」改成「是」", 6000);
    }
    if (fromAi) toast("已按预览生成，选择模块后下载 zip", 5000);
  } catch (err) {
    toast("解析失败：" + err.message);
    if (!quiet) goStep(1);
  } finally {
    setLoading(false);
  }
}

async function loadDemo() {
  try {
    const res = await fetch("/api/template");
    const blob = await res.blob();
    await handleFile(new File([blob], "示例-采购申请.xlsx", {
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }));
  } catch (err) {
    toast("载入示例失败：" + err.message);
  }
}

/* -------------------------------------- 第 1 步：业务对象关联项目（导入前）
 *
 * 这一层现在是**初始页上的两档控件**，不再是导入后的弹窗：
 *   不关联（默认） / 全部关联
 * 两档都要把决策显式发回去、再解析一次——不发明示的 false，Excel 里本来是
 * 「是」的对象就会漏回来。
 *
 * 这里只是**前端流程**：协议没变，仍然是 POST /api/parse + X-Project-Link，
 * 服务端照旧重新分析、不信任前端。
 */

/* 初始页选的是「全部关联 / 不关联」时，返回**全量**决策 { 对象编码: true|false }；
 * 一个对象都没有（或都取不到编码）时返回 null，调用方跳过第二次解析。
 * 必须全量：只发勾上的对象，Excel 里本来是「是」的那些会漏回来。 */
function plPreDecisions(objects) {
  const linked = state.plPre === "on";
  const map = {};
  (objects || []).forEach((o) => {
    const code = String((o && o.code) || "");
    if (code) map[code] = linked;
  });
  return Object.keys(map).length ? map : null;
}

/* 两个菜单的两档单选同步：普通模式 / 流程模式共享同一档（state.plPre），
 * 存量包修订是独立的三档（state.plPrePatch）。 */
function syncPlPreInputs() {
  $$('input[name="plPreImport"], input[name="plPreWorkflow"]').forEach((el) => {
    el.checked = el.value === state.plPre;
  });
  $$('input[name="plPrePatch"]').forEach((el) => {
    el.checked = el.value === state.plPrePatch;
  });
}

/* 绑定单选控件：点了就写 state + localStorage，并把另一个菜单的同名字段刷成一致。
 * 值不在白名单里就落到该组的默认档（脏值不该把整页带进未定义状态）。 */
function bindPlPre() {
  const groups = [
    { name: "plPreImport", allowed: PL_PRE_MODES, key: PL_PRE_KEY, patch: false, fallback: "off" },
    { name: "plPreWorkflow", allowed: PL_PRE_MODES, key: PL_PRE_KEY, patch: false, fallback: "off" },
    { name: "plPrePatch", allowed: PL_PRE_PATCH_MODES, key: PL_PRE_PATCH_KEY, patch: true, fallback: "keep" },
  ];
  groups.forEach((g) => {
    $$(`input[name="${g.name}"]`).forEach((el) => {
      el.onchange = () => {
        if (!el.checked) return;
        const value = g.allowed.indexOf(el.value) >= 0 ? el.value : g.fallback;
        if (g.patch) state.plPrePatch = value;
        else state.plPre = value;
        savePref(g.key, value);
        syncPlPreInputs();
      };
    });
  });
  syncPlPreInputs();
}

/* ------------------------------------------------ 渲染 */

function renderAll() {
  const d = state.data;
  // 流程工作区是独立菜单，但数据来自同一次解析：这里顺带刷一遍，
  // 用户上传完 Excel 直接点左侧「流程」就能看到，不用再手动刷新
  if (window.Workflow) window.Workflow.refresh();
  $("#result").hidden = false;
  window.__importHasResult = true;
  // 旧版这里是 #actionbar / #dropzone 显隐，现在改成步骤条控制：
  // 出包区由 goStep(3) 负责显示，输入区自然隐藏
  $("#fileName").textContent = state.file.name;
  $("#fileSize").textContent = humanSize(state.file.size);

  // 统计不再做成 7 个等权方块——「1 个对象」和「25 个控件」并列会让人无法判断
  // 这包到底多大。压成一行，数字仍读得到，但不再假装是仪表盘。0 的不显示。
  const stats = [
    ["业务对象", d.stats["业务对象"] || 0],
    ["字段", d.stats["字段"] || 0],
    ["表单", d.stats["表单"] || 0],
    ["表单控件", d.stats["表单控件"] || 0],
    ["列表", d.stats["列表"] || 0],
    ["数据集", d.stats["数据集"] || 0],
    // 流程是裸 XML、单独下载，统计里不显示会让人以为没生成
    ["流程", d.stats["流程"] || 0],
  ].filter(([, n]) => n > 0);
  $("#stats").innerHTML = stats.length
    ? stats.map(([k, n]) => `<span class="stat-i"><b>${n}</b>${k}</span>`).join('<span class="sep">·</span>')
    : '<span class="stat-i empty">这份 Excel 里还没有任何内容</span>';

  // 流程模式的对象不随同步包出包，数字对不上时得说清楚为什么，
  // 否则用户会以为包里少导了一个对象
  const note = $("#statsNote");
  if (note) {
    const flowCount = (d.objects || []).filter((o) => o.flowDriven).length;
    note.hidden = !flowCount;
    note.textContent = flowCount
      ? `其中 ${flowCount} 个是流程模式的对象：不随同步包出包，到左侧「流程模式 › 导出流程对象包」单独拿。`
      : "";
  }

  const errCount = d.errorCount;
  const warnCount = d.warnCount;
  const banner = $("#banner");
  if (errCount) {
    // P0-5：保留原结论，补「哪类错误 / 影响哪个模块 / 去哪个页签修」三要素。
    // 模块与页签一律从真实 issues[]（error 级第一条）推导，推不出就降级，
    // 绝不写死假模块名 / 假页签名。
    const firstErr = (d.issues || []).find((i) => i.level === "error");
    let where = "";
    let tab = "校验结果";
    if (firstErr) {
      where = String(firstErr.where || "").trim();
      const msg = String(firstErr.message || "").trim();
      const hit = ["1_业务对象", "2_字段", "3_表单", "4_表单控件",
                   "5_列表", "6_数据集", "7_表单脚本", "8_隐藏规则"]
        .find((t) => where.indexOf(t) >= 0 || msg.indexOf(t) >= 0);
      if (hit) tab = hit;
    }
    banner.className = "banner error";
    banner.innerHTML = `<strong>${errCount} 个错误</strong>：必须先修好才能生成，详见「校验结果」。`
      + `<div class="sub">哪类：校验错误（${esc(firstErr ? String(firstErr.message).slice(0, 60) : "结构 / 引用错误")}）`
      + ` · 影响 ${esc(where || "详见『校验结果』页签")} · 去「${esc(tab)}」修；`
      + `不要往包里加 manifest、txt、图标。</div>`;
    banner.hidden = false;
  } else if (warnCount) {
    banner.className = "banner warn";
    banner.innerHTML = `<strong>${warnCount} 个警告</strong>：不影响生成，建议核对一下。`;
    banner.hidden = false;
  } else {
    banner.className = "banner ok";
    banner.innerHTML = `<strong>校验通过</strong>：结构完整，可以生成。`;
    banner.hidden = false;
  }

  const badge = $("#issueBadge");
  badge.textContent = errCount + warnCount;
  badge.className = "badge" + (errCount ? " has-error" : warnCount ? " has-warn" : "");

  // 字段体检的角标：只表示「有几处要你核对」，不是错误，所以不标红
  const fBadge = $("#fieldBadge");
  if (fBadge) {
    const fcAll = (d.fieldChecks || []).length;
    const fcWarn = d.fieldCheckWarn || 0;
    fBadge.textContent = fcAll;
    fBadge.className = "badge" + (fcWarn ? " has-warn" : "");
  }

  renderObjects();
  renderRelations();
  renderProjectLink();
  renderPlEntry();
  renderFieldCheck();
  renderForms();
  renderLists();
  renderIssues();
  state.imported = new Set();
  state.picked = new Set(
    (d.modules || []).filter((m) => m.count > 0).map((m) => m.code)
  );
  renderModules();

  updateBar();
  // P0-1：第 7 tab 两态同渲染；predict 用 state.data，actual 用 state.buildReport。
  // 放在 updateBar() 之后，不动 updateBar / #btnPack 文案与行为。
  renderPackage(state.data, state.packageMode);

  // 有流程才显示「下载流程 XML」：流程是裸 XML，不走同步包，单独一个入口。
  // 只切 hidden，不碰 updateBar 里对 #btnPack 的禁用逻辑（两者互不影响）。
  const wfBtn = $("#btnWorkflow");
  if (wfBtn) {
    const wfMod = (d.modules || []).find((m) => m.code === "workflow");
    wfBtn.hidden = !(wfMod && wfMod.count > 0);
  }
}

/* 底部按钮与提示随勾选变化 */
function updateBar() {
  const d = state.data;
  const mods = (d.modules || []).filter((m) => m.count > 0);
  const picked = mods.filter((m) => state.picked.has(m.code));
  const btn = $("#btnPack") || $("#btnBuild");
  if (btn) btn.disabled = d.errorCount > 0 || !picked.length;
  $("#barSummary").textContent =
    `${d.objects.length} 个对象 · ${d.forms.length} 个表单 · ${d.lists.length} 个列表 · ${d.datasets.length} 个数据集`;
  $("#barHint").textContent = d.errorCount
    ? "有错误，已禁用生成"
    : picked.length
      ? `已选 ${picked.length} 个模块（${picked.map((m) => m.label).join(" / ")}）· 也可在下面逐个导入`
      : "还没选模块";
  btn.textContent = picked.length === mods.length ? "导出全部模块" : `导出选中的 ${picked.length} 个模块`;
}

/* 所属关系确认：把后端 relationships view model 渲染成勾选区，并暴露决策给出包/导出 */
function renderRelations() {
  const d = state.data;
  const vm = d.relationships;
  const box = $("#relBox");
  const badge = $("#relBadge");
  const btn = $("#btnConfirmedExcel");
  const relCount = vm && vm.items
    ? vm.items.reduce((n, it) => n
        + (it.candidates || []).length
        + (it.excelRelation && it.excelRelation.specified ? 1 : 0), 0)
    : 0;
  if (badge) badge.textContent = relCount;
  if (box && window.Relations) {
    window.Relations.reset();
    window.Relations.render(box, vm, { scope: "parse" });
  }
  if (btn) btn.hidden = !relCount;
}

/* 项目字段对齐：渲染页签、算 badge、有待确认的相近字段时自动开一次确认弹窗。
 *
 * 2026-11-15 口径：只有「直接对应」的字段自动覆盖，语义相近 / 可能相关 / 冲突 /
 * 多候选都要用户逐条确认。所以这里不再用 diffCount 开弹窗（差异是「勾了会变成
 * 什么」的说明，不是「要不要勾」的问题），改为 pendingCount>0 才弹；
 * 只有直接对应（E0）有差异、没有待确认项时也弹一次——用户要看到「哪些不一致」，
 * 不能因为已经自动覆盖就静默改掉。
 * plModalShown 保证同一份文件只自动弹一次（勾完重解析后不再糊遮罩）。 */
function renderProjectLink() {
  const vm = state.data && state.data.projectAlign;
  const box = $("#plBox");
  if (!window.ProjLink) return;
  window.ProjLink.reset();
  window.ProjLink.render(box, vm);      // render 内部会顺手 updateBadge()
  const count = window.ProjLink.pendingCount(vm);
  const direct = window.ProjLink.directDiffCount
    ? window.ProjLink.directDiffCount(vm) : 0;
  state.plPending = count;
  if ((count || direct) && !state.plModalShown) {
    state.plModalShown = true;
    window.ProjLink.openModal(vm);
  }
}

/* 分析完成 → 先弹确认，用户选完/保留后才真正渲染预览和结果页。
 * 之前是 handleFile 先 renderAll + goStep(3)，弹窗只是盖在上面，用户会觉得
 * 「预览已经生成了，还让我确认什么」。 */
function finishPlDeferred() {
  if (!state.plDeferred) return false;
  state.plDeferred = false;
  state.plModalShown = true;      // 已确认过这一轮，renderAll 里别再自动弹
  renderAll();
  goStep(3);
  return true;
}

/* 结果页顶部的「关联项目」入口条。
 *
 * 开关本身在「业务对象」页签的卡片右上角，但那一屏要往下滚三个卡片才到，
 * 用户打开结果页只会看到「要导出哪些模块」，反馈就是「我还是没看到」。
 * 这里只做两件事：说清有几个还没关联、一键跳过去闪一下开关。
 * 不碰任何出包逻辑（开关仍然走 toggleProjLink 带决策头重新解析）。
 *
 * 可能在页面还没解析完 / 结果区还 hidden 时被 renderAll() 调到，
 * 拿不到节点就安静返回，不报错。
 */
function renderPlEntry() {
  const bar = $("#plEntry");
  if (!bar) return;                       // 旧 HTML / 还没解析到这块
  const txt = $("#plEntryText");
  const go = $("#plEntryGo");
  const objects = (state.data && state.data.objects) || [];
  const total = objects.length;
  const on = objects.filter((o) => o.projectLink).length;
  const off = total - on;

  if (!total) {                           // 一个对象都没有：这条横条没有意义
    bar.hidden = true;
    if (go) go.onclick = null;
    return;
  }

  if (txt) {
    txt.textContent = on === 0
      ? `业务对象关联项目：${total} 个对象都未关联 —— 勾选后自动补「所属项目」，同名同族字段按项目定义覆盖，相近字段会逐条问你`
      : off === 0
        ? `业务对象关联项目：${total} 个对象已全部关联`
        : `业务对象关联项目：已关联 ${on} 个，还有 ${off} 个未关联`;
  }
  if (go) {
    go.textContent = off > 0 ? "去设置" : "查看";
    go.onclick = () => jumpToProjectLink();
  }
  bar.hidden = false;
}

/* 跳到「业务对象」页签，并把第一个未勾选的开关闪一下（全勾了就都闪第一个）。
 * 页签切换复用现有 onclick（active + panel.hidden），不另写一套显隐。 */
function jumpToProjectLink() {
  const tab = document.querySelector('#tabs .tab[data-panel="objects"]');
  if (tab) tab.click();
  const panel = document.querySelector('.panel[data-panel="objects"]');
  if (!panel) return;
  if (typeof panel.scrollIntoView === "function") {
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  const first = panel.querySelector('[data-pl-link]:not(:checked)')
    || panel.querySelector('[data-pl-link]');
  if (!first || typeof first.closest !== "function") return;
  const box = first.closest(".pl-switch");
  if (!box) return;
  box.classList.add("pl-flash");
  // 闪一下就够：一直闪会让人以为开关是坏的
  setTimeout(() => box.classList.remove("pl-flash"), 1800);
}

/* 统一的「关联项目」决策头：界面开关（linked）+ 确认弹窗里勾的字段（align）。
 *
 * 同步包 / 流程对象包 / 确认版 Excel 三个入口，以及「拨开关后重新解析」，都用它。
 * 各自拼一份迟早会出现「预览有 projectId、包里没有」——那种差异只有用户导入后
 * 才会发现（页面全绿，系统里就是没有「所属项目」）。同理，勾了「按项目覆盖」却
 * 只在预览里生效、包里没生效，是更糟的一种：用户以为改了，导入后定义还是旧的。
 *
 * 格式（服务端 parse_decisions 唯一契约）：
 *   {version:1, objects:{ 对象编码: {linked: true|false,
 *                                   align: {字段编码: true | "项目字段编码"}} }}
 * align 只发勾上的：true = 按最匹配的项目字段覆盖，字符串 = 按选中的候选覆盖
 * （多候选下拉）；没勾 = 保留我方，不需要显式 false。
 */
function projLinkHeader() {
  const objects = {};
  const codes = new Set(Object.keys(state.plLinked || {}));
  Object.keys(state.plAlign || {}).forEach((code) => codes.add(code));
  codes.forEach((code) => {
    const entry = {};
    if (Object.prototype.hasOwnProperty.call(state.plLinked, code)) {
      // 显式写 true/false：只写 true 的话，用户关掉的开关会被 Excel 里的「是」顶回来
      entry.linked = !!state.plLinked[code];
    }
    const align = state.plAlign[code] || {};
    const picked = {};
    Object.keys(align).forEach((field) => {
      const val = align[field];
      if (val === true) picked[field] = true;
      // 多候选：值是项目字段编码（用户在下拉里挑的那个），原样发；false/空不发
      else if (typeof val === "string" && val) picked[field] = val;
    });
    if (Object.keys(picked).length) entry.align = picked;
    if (Object.keys(entry).length) objects[code] = entry;
  });
  return Object.keys(objects).length
    ? JSON.stringify({ version: 1, objects })
    : null;
}

/* 弹窗里「按勾选覆盖」：把勾选结果并进 state.plAlign，然后带决策头重新解析。
 * 失败只 toast（弹窗还开着，用户能改勾选再试），成功才关弹窗 + 重新渲染。
 * 预览与 zip 都读 state.plAlign（projLinkHeader），所以两边一定一致。 */
async function applyProjectPicks() {
  if (!state.file) {
    window.ProjLink.closeModal();
    toast("示例数据没有源文件：请先上传 Excel，再勾选要覆盖的字段");
    return;
  }
  const got = (window.ProjLink && window.ProjLink.collect()) || {};
  const n = Object.keys(got).reduce((acc, c) => acc + Object.keys(got[c]).length, 0);
  if (!n) {
    // 一个都没勾 = 保留我方：不发请求、不改决策（省一次解析，也不留空头）
    window.ProjLink.closeModal();
    toast("没有勾选任何字段：这些相近字段都按原规则生成", 5000);
    finishPlDeferred();          // 延迟渲染的话，现在才出预览/结果页
    return;
  }
  const merged = Object.assign({}, state.plAlign);
  Object.keys(got).forEach((code) => {
    merged[code] = Object.assign({}, merged[code], got[code]);
  });
  state.plAlign = merged;
  const btn = $("#plApplyPicked");
  if (btn) btn.disabled = true;
  setLoading(true, "正在按你勾的字段重新分析…");
  try {
    const buf = await state.file.arrayBuffer();
    const headers = { "Content-Type": "application/octet-stream" };
    const plHeader = projLinkHeader();
    if (plHeader) headers["X-Project-Link"] = plHeader;
    const res = await fetch("/api/parse", { method: "POST", body: buf, headers });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "解析失败");
    state.data = data;
    const wasDeferred = state.plDeferred;
    state.plDeferred = false;
    state.plModalShown = true;            // 已确认过这一轮，别再自动弹
    resetParseArtifacts();
    window.ProjLink.closeModal();
    renderAll();
    if (wasDeferred) goStep(3);           // 弹窗确认完才真正出预览/结果页
    toast(n
      ? `已按勾选覆盖 ${n} 个字段，预览与下载的包都已更新`
      : "已保留全部字段：没有勾选任何「按项目覆盖」", 6000);
  } catch (err) {
    // 保持弹窗开着：勾选还在（state.plAlign 已在请求前合并），用户可以直接重试
    toast("按勾选覆盖失败：" + err.message, 6000);
  } finally {
    if (btn) btn.disabled = false;
    setLoading(false);
  }
}

/* 对象卡片上的「关联项目」开关：拨一下就带决策头重新解析（预览即时生效）。
 * 只改 bo.project_link，不动字段定义；字段对齐仍由后端 apply 负责。 */
async function toggleProjLink(code, checked, input) {
  if (!state.file) {
    if (input) input.checked = !checked;
    toast("示例数据请先上传 Excel，再在对象卡片上勾「关联项目」");
    return;
  }
  const had = Object.prototype.hasOwnProperty.call(state.plLinked, code);
  const prev = state.plLinked[code];
  // 提示里用中文对象名：卡片上写的是「采购申请」+ 编码 chip，只回报编码
  // （demo_purchase）用户得自己对一遍才知道是哪个对象
  const found = ((state.data && state.data.objects) || []).find((o) => o.code === code);
  const label = found ? `${found.name}（${code}）` : code;
  state.plLinked[code] = !!checked;
  if (input) input.disabled = true;
  setLoading(true, "正在按「关联项目」重新分析…");
  try {
    const buf = await state.file.arrayBuffer();
    const headers = { "Content-Type": "application/octet-stream" };
    const plHeader = projLinkHeader();
    if (plHeader) headers["X-Project-Link"] = plHeader;
    const res = await fetch("/api/parse", { method: "POST", body: buf, headers });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "解析失败");
    state.data = data;
    // 与 handleFile 同一套重置：新关联的项目对象要能弹字段差异对照，
    // 第 7 tab 不能残留上一份包结构/实测清单
    state.plModalShown = false;
    state.packageMode = "predict";
    state.buildReport = null;
    state.inspectSummary = null;
    state.zipPhase = { build: "idle", inspect: "idle" };
    const staleBuild = $("#pp6zipBuildReport");
    if (staleBuild) { staleBuild.hidden = true; staleBuild.innerHTML = ""; }
    const staleInspect = $("#pp6zipInspectCard");
    if (staleInspect) { staleInspect.hidden = true; staleInspect.innerHTML = ""; }
    renderAll();
    toast(`对象 ${label}：关联项目=${checked ? "是" : "否"}，已重新分析`, 4000);
  } catch (err) {
    // 回滚到拨之前的那一次决策（不是无条件 true/false：之前可能压根没决策）
    if (had) state.plLinked[code] = prev;
    else delete state.plLinked[code];
    if (input) { input.checked = !checked; input.disabled = false; }
    toast("按「关联项目」重新分析失败：" + err.message, 6000);
  } finally {
    setLoading(false);
  }
}

/* AI 预览页的关系确认（第 2 步），与解析页同一组件、不同 scope */
function renderAiRelations(d) {

  const box = $("#aiRelBox");
  const panel = $("#aiRelPanel");
  const vm = d && d.relationships;
  const relCount = vm && vm.items
    ? vm.items.reduce((n, it) => n
        + (it.candidates || []).length
        + (it.excelRelation && it.excelRelation.specified ? 1 : 0), 0)
    : 0;
  if (panel) panel.hidden = !relCount;
  if (box && window.Relations) {
    window.Relations.reset();
    window.Relations.render(box, vm, { scope: "ai" });
  }
}

/* 下载「确认版 Excel」：把勾选的关系写回父对象/关联字段/关系类型三列，作为新文件返回 */
async function downloadConfirmedExcel() {
  if (!state.file) return;
  try {
    const buf = await state.file.arrayBuffer();
    const headers = { "Content-Type": "application/octet-stream" };
    const relHeader = window.Relations ? window.Relations.decisionsHeader("parse") : null;
    if (relHeader) headers["X-Relations"] = relHeader;
    // 与出包同一份决策头：确认版 Excel 里的「关联项目」列要与页面上的勾选一致，
    // 否则下次拿这份 Excel 再解析，开关又回到原样
    const plHeader = projLinkHeader();
    if (plHeader) headers["X-Project-Link"] = plHeader;
    const res = await fetch("/api/confirmed-excel?name=" + encodeURIComponent(state.file.name), {
      method: "POST", body: buf, headers,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || "生成失败");
    }
    const blob = await res.blob();
    const name = decodeURIComponent(res.headers.get("X-Filename") || "已确认.xlsx");
    if (await saveBlob(blob, name)) {
      toast(`已下载 ${name}：下次用它继续，勾选的关系不会丢`, 5000);
    }
  } catch (err) {
    toast("下载确认版 Excel 失败：" + err.message, 6000);
  }
}

/* 下载流程定义：流程是裸 XML，不进同步包，走独立的 /api/workflow 接口。
   单条流程直接回 .xml，多条回一个 zip（内含 {流程名}.xml）。 */
async function downloadWorkflow(btn) {
  if (!state.file) return;
  const label = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "正在生成…"; }
  try {
    const buf = await state.file.arrayBuffer();
    const res = await fetch("/api/workflow", {
      method: "POST",
      body: buf,
      headers: { "Content-Type": "application/octet-stream" },
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || "生成失败");
    }
    const blob = await res.blob();
    const name = decodeURIComponent(res.headers.get("X-Filename") || "流程.xml");
    if (await saveBlob(blob, name)) {
      toast(`已下载 ${name}：到 管理 › 流程设计 › 导入 上传`, 6000);
    }
  } catch (err) {
    toast("下载流程 XML 失败：" + err.message, 6000);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}

/* 导出流程对象包：流程模式的对象 + 它的表单/列表/数据集。
   和同步包复用同一个 /api/build，用 X-Object-Scope 分开——同步包只出普通对象，
   流程对象在这里出。导入是两步：对象包 → 管理 › 业务对象 › 导入；
   流程 XML → 管理 › 流程设计 › 导入。 */
const WF_OBJECT_MODULES = ["bo", "form", "list", "dataset", "filter"];
async function downloadWorkflowObjects(btn) {
  if (!state.file) return;
  const label = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "正在生成…"; }
  try {
    const buf = await state.file.arrayBuffer();
    const headers = {
      "Content-Type": "application/octet-stream",
      "X-Object-Scope": "workflow",
      "X-Modules": JSON.stringify(WF_OBJECT_MODULES),
    };
    // 流程对象包同样要带「关联项目」决策：流程对象也是业务对象，勾了开关却没带
    // 就是「页面有 projectId、这个包里没有」——这一包是单独导的，最容易漏
    const plHeader = projLinkHeader();
    if (plHeader) headers["X-Project-Link"] = plHeader;
    const res = await fetch("/api/build", {
      method: "POST",
      body: buf,
      headers,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || "生成失败");
    }
    const blob = await res.blob();
    const name = decodeURIComponent(res.headers.get("X-Filename") || "流程对象.zip");
    if (await saveBlob(blob, name)) {
      toast(`已下载 ${name}：到 管理 › 业务对象 › 导入 上传；流程 XML 再点「下载流程 XML」`, 7000);
    }
  } catch (err) {
    toast("导出流程对象包失败：" + err.message, 6000);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}

/* 按模块导入 / 导出：系统里每个模块单独导入，所以左边一个开关、右边两个动作 */function renderModules() {
  const box = $("#modList");
  const mods = state.data.modules || [];
  if (!mods.length) { box.innerHTML = ""; return; }
  mods.forEach((m) => { MOD_LABEL[m.code] = m.label; });
  box.innerHTML = mods.map((m) => {
    const empty = !m.count;
    const need = (m.needs || []).filter((n) => !state.imported.has(n));
    // 只提示不拦截：对象可能早就在系统里导过了，禁掉反而挡住正常操作
    const hint = !empty && need.length > 0;
    const done = state.imported.has(m.code);
    const picked = state.picked.has(m.code);
    const flowHint = m.code === "workflow";
    const badges = [
      empty ? '<span class="chip">无内容</span>' : "",
      done ? '<span class="chip accent">已导入</span>' : "",
      hint ? `<span class="chip warn">建议先导入${need.map((n) => MOD_LABEL[n] || n).join("、")}</span>` : "",
    ].join("");
    return `
      <div class="mod-row ${empty ? "is-empty" : ""}">
        <label class="mod-pick">
          <input type="checkbox" data-mod="${m.code}" ${picked ? "checked" : ""} ${empty ? "disabled" : ""}>
        </label>
        <div class="mod-main">
          <div class="mod-name">
            ${esc(m.label)}
            <span class="chip">${m.count} 项</span>
            ${badges}
          </div>
          <div class="mod-menu">${esc(m.menu)}${m.folder ? ` · 目录 <code>${esc(m.folder)}/</code>` : ""}${m.zipped ? "" : " · 裸 XML"}</div>
          ${m.items.length
            ? `<div class="mod-items">${m.items.slice(0, 6).map((x) => `<span class="chip">${esc(x)}</span>`).join("")}${m.items.length > 6 ? `<span class="muted">等 ${m.items.length} 项</span>` : ""}</div>`
            : m.note ? `<div class="mod-note">${esc(m.note)}</div>` : ""}
        </div>
        <div class="mod-btns">
          <button class="btn ghost sm" data-act="import" data-mod="${m.code}"
                  ${empty || flowHint ? "disabled" : ""}
                  title="${flowHint ? "流程是裸 XML，不进 zip：请到左侧「流程」工作区下载" : "单独生成这一个模块的 zip 并保存"}">导入</button>
          <button class="btn ghost sm" data-act="export" data-mod="${m.code}"
                  ${empty || flowHint ? "disabled" : ""}
                  title="${flowHint ? "流程是裸 XML，不进 zip：请到左侧「流程」工作区下载" : "只导出这一个模块的 zip"}">导出</button>
        </div>
      </div>`;
  }).join("");

  $$("#modList input[data-mod]").forEach((cb) => {
    cb.onchange = () => {
      if (cb.checked) state.picked.add(cb.dataset.mod);
      else state.picked.delete(cb.dataset.mod);
      updateBar();
    };
  });
  $$("#modList button[data-act]").forEach((btn) => {
    btn.onclick = () => {
      const code = btn.dataset.mod;
      if (btn.dataset.act === "import") importOne(code, btn);
      else buildZip([code], btn);
    };
  });
}

const MOD_LABEL = {};

function renderObjects() {
  const panel = $('.panel[data-panel="objects"]');
  const objects = state.data.objects;
  if (!objects.length) {
    panel.innerHTML = `<div class="card"><div class="empty">还没有业务对象，请先在 1_业务对象 表里填写。</div></div>`;
    return;
  }
  // 开关是「拨了就重新解析」，所以没有文件（理论上不会发生，示例也走 handleFile）
  // 时禁用并说清原因，而不是让人点了没反应
  const canToggle = !!state.file;
  const linkTip = canToggle
    ? "勾选后自动补「所属项目」链接字段，并把与项目对象同名/近义/相似的字段照项目定义覆盖（差异会汇总成一个对照弹窗）"
    : "示例数据请先上传 Excel，再在对象卡片上勾「关联项目」";
  panel.innerHTML = `<div class="obj-tip">这里勾选即可，不用改 Excel；
      Excel 的「关联项目」列仍然有效（以勾选为准）。</div>`
    + objects.map((bo) => `
    <div class="card">
      <div class="card-head">
        <div>
          <h3>${esc(bo.name)} <span class="chip accent">${esc(bo.code)}</span>${bo.flowDriven ? ' <span class="chip">流程模式</span>' : ""}${bo.projectLink ? ' <span class="chip accent">关联项目</span>' : ""}</h3>
          <div class="sub">表名 ${esc(bo.tbl)} · ${bo.fields.length} 个字段${bo.withLog ? " · 记录变更日志" : ""}${bo.flowDriven ? " · 走「流程模式 › 导出流程对象包」，不随同步包" : ""}${bo.projectLink ? " · 已补「所属项目」链接字段，并已对齐与项目同名的字段" : ""}</div>
        </div>
        <div class="obj-side">
          <label class="pl-switch" title="${esc(linkTip)}">
            <input type="checkbox" data-pl-link="${esc(bo.code)}"
                   ${bo.projectLink ? "checked" : ""} ${canToggle ? "" : "disabled"}>
            <span>关联项目</span>
          </label>
          <span class="sub">${esc(bo.moduleId || "未指定模块")}</span>
        </div>
      </div>
      <div class="tbl-wrap">
        <table>
          <thead><tr>
            <th>字段名称</th><th>字段编码</th><th>物理列名</th><th>类型</th><th>长度</th><th>备注</th>
          </tr></thead>
          <tbody data-bo="${esc(bo.code)}">
            ${wfRows(bo.fields, bo.fields.length > OBJ_ROW_LIMIT)}
          </tbody>
        </table>
      </div>
      ${bo.fields.length > OBJ_ROW_LIMIT
        ? `<div class="tbl-more"><button class="link" data-more="${esc(bo.code)}">
             展开全部 ${bo.fields.length} 个字段</button></div>`
        : ""}
    </div>
  `).join("");

  // 「关联项目」开关：改变即重新解析，所以只挂 change（不挂 click，
  // 避免和 label 的默认行为重复触发一次请求）
  $$('.panel[data-panel="objects"] [data-pl-link]').forEach((el) => {
    el.onchange = () => toggleProjLink(el.dataset.plLink, el.checked, el);
  });

  // 字段多的对象默认只显示前 N 行：5 个对象 × 45 个字段 = 225 行，
  // 一屏滚不到底，也没人需要一眼看完。需要时再展开，展开后还能收起。
  $$('.panel[data-panel="objects"] [data-more]').forEach((btn) => {
    btn.onclick = () => {
      const code = btn.dataset.more;
      const tbody = document.querySelector(`tbody[data-bo="${code}"]`);
      const bo = objects.find((o) => o.code === code);
      // dataset.open = "1" 表示「当前已展开」
      const open = btn.dataset.open === "1";
      tbody.innerHTML = wfRows(bo.fields, open);
      btn.dataset.open = open ? "0" : "1";
      btn.textContent = open
        ? `展开全部 ${bo.fields.length} 个字段`
        : "收起，只看前 " + OBJ_ROW_LIMIT + " 个";
    };
  });
}

/* 字段行：折叠时只出前 OBJ_ROW_LIMIT 行 */
const OBJ_ROW_LIMIT = 10;
function wfRows(fields, collapsed) {
  const list = collapsed ? fields.slice(0, OBJ_ROW_LIMIT) : fields;
  return list.map((f) => `
    <tr>
      <td>${esc(f.name)}</td>
      <td><code>${esc(f.code)}</code></td>
      <td><code>${esc(f.fieldName)}</code></td>
      <td>${esc(TYPE_LABEL[f.type] || f.type)}</td>
      <td>${f.length ?? "—"}${f.precision ? `,${f.precision}` : ""}</td>
      <td class="sub">${[
        f.index ? "索引" : "",
        f.objectType ? "关联 " + f.objectType : "",
      ].filter(Boolean).map(esc).join(" / ") || "—"}</td>
    </tr>
  `).join("");
}

function renderForms() {
  const panel = $('.panel[data-panel="forms"]');
  const forms = state.data.forms;
  if (!forms.length) {
    panel.innerHTML = `<div class="card"><div class="empty">还没有表单，请先在 3_表单 与 4_表单控件 里填写。</div></div>`;
    return;
  }
  if (state.formIndex >= forms.length) state.formIndex = 0;
  const form = forms[state.formIndex];

  // 系统隐藏控件（主键 / 版本 / 拼音）不画在预览图纸上：
  // 它们对填写者本来就不可见，出包时仍按官方表单结构写进 XML（见 gen_form._HIDDEN_FIELDS）。
  panel.innerHTML = `
    <div class="form-picker">
      ${forms.map((f, i) => `<button data-idx="${i}" class="${i === state.formIndex ? "active" : ""}">${esc(f.name)}</button>`).join("")}
    </div>
    <div class="paper">
      <div class="paper-head">
        <h3>${esc(form.name)}</h3>
        <span class="chip">${esc(form.key)}</span>
        <span class="sub muted">${esc(KIND_LABEL[form.kind] || "")} · ${form.layout === 1 ? "一行一列" : "一行两列"} · 标签宽 ${form.labelWidth}px · 面板 ${form.width}% · 绑定 ${esc(form.boName || form.boCode || "未绑定")} · ${form.fieldCount} 个控件</span>
      </div>
      ${(form.groups || [{ title: "", rows: form.rows || [], count: form.fieldCount }]).map((g) => `
        <div class="fgroup">
          ${g.title ? `<div class="fgroup-head">${esc(g.title)}<span class="fgroup-cnt">${g.count || 0} 项</span></div>` : ""}
          ${g.rows.map((row) => `
            <div class="frow">
              ${row.map((cell) => `
                <div class="fcell" style="flex:${cell.span}">
                  <div class="flabel">${cell.required ? '<span class="req">*</span>' : ""}${esc(cell.label)}</div>
                  <div class="fctrl">
                    ${controlHtml(cell)}
                    <div class="ftype">${esc(CTRL_LABEL[cell.type] || cell.type)}${cell.valueSource ? " · " + esc(cell.valueSource) : ""}${cell.missing ? ' · <span class="miss">字段不存在</span>' : ""}</div>
                  </div>
                </div>
              `).join("")}
            </div>
          `).join("")}
        </div>
      `).join("")}
    </div>
  `;

  $$(".form-picker button", panel).forEach((btn) => {
    btn.onclick = () => {
      state.formIndex = Number(btn.dataset.idx);
      renderForms();
    };
  });
}

function controlHtml(cell) {
  if (cell.type === "editor") return `<div class="mock area">请输入…</div>`;
  if (cell.type === "attachment") return `<div class="mock dashed">点击上传附件</div>`;
  if (cell.type === "autoNumber") return `<div class="mock disabled">保存时自动生成</div>`;
  if (["select", "linkdata", "dropSelector", "dropSelectOrg"].includes(cell.type)) {
    const hint = cell.type === "dropSelector" ? "请选择人员" : cell.type === "dropSelectOrg" ? "请选择部门" : "请选择";
    return `<div class="mock select">${hint}</div>`;
  }
  return `<div class="mock"><span class="fill"></span><span>请输入</span></div>`;
}

function renderLists() {
  const panel = $('.panel[data-panel="lists"]');
  const { lists, datasets } = state.data;
  const listCards = lists.length
    ? lists.map((l) => `
      <div class="card">
        <div class="card-head">
          <div>
            <h3>${esc(l.name)} <span class="chip accent">${esc(l.key)}</span></h3>
            <div class="sub">对象 ${esc(l.boCode)}${l.sort ? " · 排序 " + esc(l.sort) : ""}${l.tree ? " · 树形" : ""}</div>
          </div>
          <div>${l.views.map((v) => `<span class="chip">${esc(v)}</span>`).join(" ")}</div>
        </div>
        <div style="padding:14px 18px;display:flex;gap:6px;flex-wrap:wrap;">
          ${l.columns.length
            ? l.columns.map((c) => `<span class="chip">${esc(c)}</span>`).join("")
            : '<span class="muted">未指定显示列，将使用全部字段</span>'}
        </div>
      </div>`).join("")
    : `<div class="card"><div class="empty">还没有列表配置。</div></div>`;

  const dsCards = datasets.length
    ? `<div class="card">
        <div class="card-head"><div><h3>数据集 / 值来源</h3><div class="sub">下拉框的取数来源</div></div></div>
        <table><thead><tr><th>名称</th><th>Key</th><th>主表</th><th>类型</th></tr></thead>
        <tbody>${datasets.map((d) => `<tr>
          <td>${esc(d.name)}</td><td><code>${esc(d.key)}</code></td>
          <td><code>${esc(d.table)}</code></td><td>${esc(d.type)}</td></tr>`).join("")}
        </tbody></table></div>`
    : "";

  panel.innerHTML = listCards + dsCards;
}

function renderIssues() {
  const panel = $('.panel[data-panel="issues"]');
  const issues = state.data.issues;
  if (!issues.length) {
    panel.innerHTML = `<div class="card"><div class="empty">没有任何问题，可以直接生成。</div></div>`;
    return;
  }
  const order = { error: 0, warn: 1 };
  const sorted = [...issues].sort((a, b) => (order[a.level] ?? 2) - (order[b.level] ?? 2));
  panel.innerHTML = `<div class="card">${sorted.map((i) => `
    <div class="issue ${esc(i.level)}">
      <div class="lv">${i.level === "error" ? "错误" : "警告"}</div>
      <div>
        <div class="where">${esc(i.where)}</div>
        <div>${esc(i.message)}</div>
      </div>
    </div>`).join("")}</div>`;
}

/* 字段体检：跨对象/跨表单的疑似重复与冲突。只提示，不拦出包 */
const FIELD_KIND = {
  "near-name": "名字很像",
  "name-type": "同名但类型不一致",
  "code-type": "同字段但类型不一致",
  option: "候选值不一致",
  "form-type": "表单控件类型不一致",
};

function renderFieldCheck() {
  const panel = $('.panel[data-panel="fields"]');
  const list = state.data.fieldChecks || [];
  if (!list.length) {
    panel.innerHTML = `<div class="card"><div class="empty">
      没有发现疑似重复或冲突的字段。</div></div>`;
    return;
  }
  const groups = {};
  list.forEach((f) => { (groups[f.kind] = groups[f.kind] || []).push(f); });

  const body = Object.keys(groups).map((kind) => {
    const items = groups[kind];
    return `<div class="card">
      <div class="card-head">
        <div>
          <h3>${esc(FIELD_KIND[kind] || kind)}</h3>
          <div class="sub">${items.length} 条，每条下面列了它出现在哪几个地方</div>
        </div>
      </div>
      ${items.map((f) => `
        <div class="issue ${esc(f.level)}">
          <div class="lv">${f.level === "warn" ? "疑似" : "提示"}</div>
          <div>
            <div class="fc-title">${esc(f.title)}</div>
            <div>${esc(f.detail)}</div>
            <ul class="fc-places">${(f.places || []).map((p) => `<li>${esc(p)}</li>`).join("")}</ul>
          </div>
        </div>`).join("")}
    </div>`;
  }).join("");

  panel.innerHTML = `<div class="card fc-intro"><div class="sub">
    同名同义写了两遍、或者同一个名字配了不同东西的，都列在这里。
    这些<b>不影响出包</b>，改哪个由你判断——两个名字是不是同一件东西，工具不敢替你定。
  </div></div>${body}`;
}

/* ------------------------------------------------ 下载 */

function b64ToFile(b64, filename, mime) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
  return new File([bytes], filename, { type: mime });
}

async function blobToB64(blob) {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let bin = "";
  const step = 0x8000;
  for (let i = 0; i < bytes.length; i += step) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + step));
  }
  return btoa(bin);
}

/* 保存：桌面壳用系统保存对话框；浏览器用 a[download] */
async function saveBlob(blob, filename) {
  const api = native();
  if (api) {
    try {
      const res = await api.save_file(filename, await blobToB64(blob));
      if (res && res.path) toast("已保存到 " + res.path, 6000);
      return true;
    } catch (err) {
      toast("保存失败：" + err, 6000);
      return false;
    }
  }
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
  return true;
}

/* 选文件：桌面壳用系统打开对话框；浏览器用 input[type=file] */
async function pickFile(kind) {
  const api = native();
  if (!api) return null;
  const isZip = kind === "zip";
  const picked = await (isZip ? api.pick_zip() : api.pick_excel());
  if (!picked) return null;
  const mime = isZip ? "application/zip" : "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
  return b64ToFile(picked.data, picked.name, mime);
}

async function buildZip(codes, trigger) {
  if (!state.file) return;
  const list = codes || Array.from(state.picked);
  if (!list.length) {
    // 空态：恢复三连，而不是只禁用按钮 / 只丢一句 toast
    setZipPhase("build", "empty");
    toast("先勾选要导出的模块。恢复：①「载入示例看效果」② 回到第 1 步描述需求 ③ 去 1_业务对象 填一行", 6000);
    return;
  }
  const btn = trigger || $("#btnPack") || $("#btnBuild");
  const label = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "正在生成…"; }
  setZipPhase("build", "busy");
  try {
    const buf = await state.file.arrayBuffer();
    const headers = { "X-Modules": JSON.stringify(list) };
    const relHeader = window.Relations ? window.Relations.decisionsHeader("parse") : null;
    if (relHeader) headers["X-Relations"] = relHeader;
    const plHeader = projLinkHeader();
    if (plHeader) headers["X-Project-Link"] = plHeader;
    const res = await fetch("/api/build", {
      method: "POST",
      body: buf,
      headers,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || "生成失败");
    }
    const blob = await res.blob();
    const name = decodeURIComponent(res.headers.get("X-Filename") || "pp6-import.zip");
    const report = decodeHeaderJson(res.headers.get("X-Build-Report"));
    if (report) {
      // 头可解码：回填实测包结构 + 交付报告卡片；下载照常发生。
      state.buildReport = report;
      state.packageMode = "actual";
      setZipPhase("build", "ok", { report, name });
    }
    // 头缺失 / 解码失败（legacy）：仅下载 + 改动前逐字一致的 toast，
    // packageMode 保持 predict，不渲染交付报告卡片。
    if (await saveBlob(blob, name)) {
      toast(list.length === 1
        ? `已导出「${MOD_LABEL[list[0]] || list[0]}」：${name}`
        : `已导出 ${list.length} 个模块：${name}`);
    }
  } catch (err) {
    // 异常三要素：哪类错误 / 影响哪个模块 / 去哪个页签修。
    // 「影响哪个模块」「去哪个页签」必须从真实 state.data.issues[] 推导，
    // 拼不出时降级为「详见『校验结果』页签」，不编造假模块名 / 假页签名。
    const d = state.data || {};
    const firstErr = (d.issues || []).find((i) => i.level === "error");
    let where = "";
    let tab = "校验结果";
    if (firstErr) {
      where = String(firstErr.where || "").trim();
      const msg = String(firstErr.message || "").trim();
      const hit = ["1_业务对象", "2_字段", "3_表单", "4_表单控件",
                   "5_列表", "6_数据集", "7_表单脚本", "8_隐藏规则"]
        .find((t) => where.indexOf(t) >= 0 || msg.indexOf(t) >= 0);
      if (hit) tab = hit;
      if (!where && msg) where = msg.slice(0, 40);
    }
    const category = firstErr ? "校验错误" : "出包错误";
    const whereText = where ? `影响 ${where}` : "详见『校验结果』页签";
    setZipPhase("build", "error", { category, module: whereText, tab, message: err.message });
    toast(`生成失败（${category} / ${whereText}）：${err.message}。去「${tab}」修，不要往包里加 manifest、txt、图标。`, 6000);
    // 不清空 state.data / state.buildReport / #aiResult（A5）
  } finally {
    btn.disabled = false;
    btn.textContent = label;
    updateBar();
  }
}

/* 单个模块导入：不打包汇总，直接给系统能吃的那个包 */
async function importOne(code, btn) {
  if (!state.file) return;
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "生成中…";
  try {
    const buf = await state.file.arrayBuffer();
    // 每个模块行上的「导入」是最显眼的按钮，同样要按页面上的「关联项目」开关
    // 出包：漏一个头，用户就会拿到「有对象、没 projectId」的单模块包
    const headers = { "Content-Type": "application/octet-stream" };
    const pl = projLinkHeader();
    if (pl) headers["X-Project-Link"] = pl;
    const res = await fetch(`/api/import/${code}`, { method: "POST", body: buf, headers });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || "生成失败");
    }
    const blob = await res.blob();
    const name = decodeURIComponent(res.headers.get("X-Filename") || `${code}.zip`);
    if (await saveBlob(blob, name)) {
      state.imported.add(code);
      toast(`「${MOD_LABEL[code] || code}」已保存：${name}　→　拿到系统里 ${(state.data.modules.find((m) => m.code === code) || {}).menu || ""} 上传`);
      renderModules();
    }
  } catch (err) {
    toast("生成失败：" + err.message, 6000);
  } finally {
    btn.textContent = label;
    btn.disabled = false;
  }
}

async function inspectZip(file) {
  if (!file && state.desktop) file = await pickFile("zip");
  if (!file) return;
  if (!/\.zip$/i.test(file.name)) {
    toast("请选择 .zip 导出包");
    return;
  }
  setLoading(true, "正在反解导出包…");
  setZipPhase("inspect", "busy");
  try {
    const buf = await file.arrayBuffer();
    const res = await fetch("/api/inspect", { method: "POST", body: buf });
    // 先读头再判 res.ok：失败响应（400）同样带 X-Package-Summary。
    const summary = decodeHeaderJson(res.headers.get("X-Package-Summary"));
    state.inspectSummary = summary;
    if (summary === null) {
      // legacy：头缺失 / 坏 base64 / 非 JSON -> 逐字走改动前行为，不报错、不阻断。
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.error || "反解失败");
      }
      const blob = await res.blob();
      const name = decodeURIComponent(res.headers.get("X-Filename") || "inspect.xlsx");
      if (await saveBlob(blob, name)) toast("已反解为 Excel：" + name);
      setZipPhase("inspect", "legacy");
      return;
    }
    let parsedErr = null;
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      parsedErr = new Error(err.error || "反解失败");
    }
    const cls = classifyInspect(res, summary, parsedErr);
    if (cls.skipDownload) {
      // 空 / 异常态：跳过 saveBlob()（D4，不下载误导性的空 xlsx），
      // 渲染分类卡片（实际条目名 + 恢复动作）+ 带恢复动作的 toast。
      setZipPhase("inspect", cls.kind === "empty" ? "empty" : "error",
                  { classify: cls, summary });
      toast(`反解失败（${cls.title}）：${cls.message} ${cls.recovery}`, 6500);
      return;
    }
    const blob = await res.blob();
    const name = decodeURIComponent(res.headers.get("X-Filename") || "inspect.xlsx");
    if (await saveBlob(blob, name)) toast("已反解为 Excel：" + name);
    setZipPhase("inspect", "ok", { classify: cls, summary });
  } catch (err) {
    // 真·异常（网络断 / 解码意外）：沿用改动前的通用文案。
    toast("反解失败：" + err.message, 6000);
  } finally {
    setLoading(false);
    goStep(1);
  }
}

/* ------------------------------------------------ AI 生成业务对象 */

async function apiPost(path, payload) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const d = await res.json().catch(() => ({}));
  if (!res.ok || d.ok === false) throw new Error(d.error || "请求失败");
  return d;
}

function renderAiBo(d) {
  const bo = d.bo;
  state.aiData = d;                   // 编辑时直接改这份，导出用它
  // 字段层 14 种类型，叫法照系统「添加字段 › 字段类型」下拉
  const types = d.types && d.types.length ? d.types : [
    { code: "varchar", label: "字符串" }, { code: "number", label: "数字" },
    { code: "datetime", label: "日期时间" }, { code: "clob", label: "大文本" },
    { code: "user", label: "用户" }, { code: "department", label: "部门" },
    { code: "attachment", label: "附件列表" }, { code: "i18n", label: "国际化" },
    { code: "dic", label: "选项" }, { code: "object", label: "对象" },
    { code: "userGroupCate", label: "用户组" }, { code: "table", label: "表格" },
    { code: "autoNumber", label: "自动编号" }, { code: "formula", label: "公式" },
  ];
  const objs = d.objects || [];
  const objSet = new Set(objs.map((o) => o.code));
  const rows = bo.fields.map((f, i) => {
    const typeOpts = types.map((t) =>
      `<option value="${esc(t.code)}" ${t.code === f.type ? "selected" : ""}>${esc(t.label)}</option>`).join("");
    let rel;
    if (f.type === "dic") {
      // 选项字段：这里填候选值，不是对象编码
      // （字段层只有「选项」，没有「数据字典」——字典是表单控件的取数方式）
      rel = `<input class="ai-in" data-k="objectType" value="${esc((f.choices || []).join(","))}"
                 placeholder="选项值，逗号分隔" list="aiDicVals">`;
    } else if (f.type === "object") {
      // 关联对象：必须是系统里真实存在的对象编码，给下拉而不是自由输入
      const cur = (f.objectType || "").trim();
      const opts = [`<option value="">（选一个业务对象）</option>`]
        .concat(objs.map((o) =>
          `<option value="${esc(o.code)}" ${o.code === cur ? "selected" : ""}>${esc(o.code)}｜${esc(o.name)}</option>`));
      if (cur && !objSet.has(cur)) {
        opts.push(`<option value="${esc(cur)}" selected>⚠ ${esc(cur)}（对象库中不存在）</option>`);
      }
      rel = `<select class="ai-in ai-obj" data-k="objectType">${opts.join("")}</select>`;
    } else {
      rel = '<span class="muted">—</span>';
    }
    return `
    <tr data-fi="${i}" class="${f.type === "object" && f.objectType && !objSet.has(f.objectType) ? "bad" : ""}">
      <td><input class="ai-in" data-k="name" value="${esc(f.name)}"></td>
      <td><code>${esc(f.code)}</code></td>
      <td><code class="muted">${esc(f.fieldName)}</code></td>
      <td><select class="ai-in" data-k="type">${typeOpts}</select></td>
      <td><input class="ai-in ai-num" data-k="length" value="${f.length ?? ""}"></td>
      <td>${rel}</td>
      <td><button class="ai-del" data-del="${i}" title="删掉这个字段">×</button></td>
    </tr>`;
  }).join("");

  const notes = (d.notices || []).length
    ? `<div class="ai-notes">${d.notices.map((n) => `<div>· ${esc(n)}</div>`).join("")}</div>` : "";

  const steps = (d.steps || []).map((s) => s.error
    ? `<span class="ai-step bad">${esc(s.step)}失败</span>`
    : `<span class="ai-step">${esc(s.step)} ${s.seconds}s</span>`).join("");

  const fromSheet = d.sheets && d.sheets.length
    ? `<span class="chip">读自：${esc(d.sheets.join("、"))}</span>` : "";
  const objInfo = d.objectSource && d.objectSource.count
    ? `<span class="chip" title="关联对象（object 类型）只能选这里的编码，选错导入后是空下拉框">对象库 ${d.objectSource.count} 个 · ${esc(d.objectSource.source)}</span>`
    : "";
  // 系统里有同名对象：不再自动按「更新」导入（2026-11-11 起），必须显式确认。
  // AI 生成的对象默认是新建；要更新同名对象，下载 Excel 后在「已有对象ID」列填 id。
  const exist = d.existObject;
  const existChip = exist && exist.exists
    ? `<span class="chip warn" title="本次下载的 Excel 按「新建」导入，不会动系统里这个对象。若要更新它：下载 Excel 后在 1_业务对象 的「已有对象ID」列填 ${esc(exist.id || "该对象的 id")}">
         ⚠ 系统里已有同名对象 ${esc(exist.code)}（id ${esc(exist.id || "未知")}）· 将按「新建」导入</span>`
    : `<span class="chip" title="系统里没有同名对象，导入会新建一个">＋ 新对象</span>`;

  return `
    <div class="ai-card">
      <div class="ai-card-head">
        <div>
          <strong>${esc(bo.name)}</strong>
          <span class="chip accent">${esc(bo.code)}</span>
          <span class="muted">${bo.fields.length} 个字段</span>
          ${fromSheet}
          ${objInfo}
          ${existChip}
        </div>
        <div class="ai-steps">${steps}<span class="muted">共 ${d.seconds}s</span></div>
      </div>

      <div class="ai-sheetpick" id="aiSheetPick" hidden></div>

      <div class="ai-tabs" id="aiTabs">
        <button class="ai-tab active" data-ai="obj">业务对象</button>
        ${d.form ? '<button class="ai-tab" data-ai="form">表单布局</button>' : ""}
        ${d.list ? '<button class="ai-tab" data-ai="list">列表视图</button>' : ""}
      </div>

      <div class="ai-panel" data-ai-panel="obj">
        <div class="ai-edit-tip">下表可以直接改：字段名、类型、长度、关联对象/选项值。「关联对象」是下拉，只能选对象库里已有的对象——手填中文名或自造编码，导入后是空下拉框。改完再下载 Excel。</div>
        <div class="tbl-wrap">
          <table class="ai-edit">
            <thead><tr><th>字段名称</th><th>编码</th><th>物理列名</th><th>类型</th><th>长度</th><th>关联 / 选项</th><th></th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
        <datalist id="aiDicVals"></datalist>
      </div>

      ${d.form ? `<div class="ai-panel" data-ai-panel="form" hidden>${renderAiForm(d.form)}</div>` : ""}
      ${d.list ? `<div class="ai-panel" data-ai-panel="list" hidden>${renderAiList(d.list)}</div>` : ""}

      ${notes}
      <div class="ai-card-foot" id="aiActions"></div>
    </div>`;
}

/* 表单布局预览：按组显示，必填打星号，整行的单独一行。
   分行规则必须和生成器（gen_form._plan_rows）保持一致：
   每行最多 2 个普通控件。之前只按 fullRow 断行，导致某一组字段一多
   就横向挤成竖条——预览和实际出包长得不一样，用户没法据此判断。 */
const AI_MAX_PER_ROW = 2;

function renderAiForm(form) {
  // 分区标题只在「换到另一个分区」时打一次。不能再用「只有每组第一个字段
  // 带 group」的旧契约——ai_form 序列化时给每个字段都写了 group，
  // 那样每字段都会断行并重复一次分区名。
  let curGroup = "";
  const perRow = form.layout === 1 ? 1 : AI_MAX_PER_ROW;
  const lines = [];
  let buf = [];
  const flush = () => {
    if (buf.length) { lines.push(buf); buf = []; }
  };
  form.fields.forEach((f) => {
    const group = (f.group || "").trim();
    if (group && group !== curGroup) {
      flush();                       // 换分区必换行，不能接着上一区排
      curGroup = group;
      lines.push({ title: curGroup });
    }
    if (f.fullRow) {
      // 多行文本/附件等独占整行：先把本行已有的收掉，再让它自己成行
      flush();
      lines.push([f]);
      return;
    }
    buf.push(f);
    if (buf.length >= perRow) flush();
  });
  flush();

  const body = lines.map((row) => {
    if (row.title && !Array.isArray(row)) {
      return `<div class="ai-group">${esc(row.title)}</div>`;
    }
    return `<div class="ai-frow">${row.map((f) => `
      <div class="ai-fcell ${f.fullRow ? "full" : ""}">
        <span class="ai-flabel">${f.required ? '<i>*</i>' : ""}${esc(f.label)}</span>
        <span class="ai-fctrl">${esc(f.placeholder || "")}</span>
        <span class="ai-ftype">${esc(CTRL_LABEL[f.type] || f.type)}</span>
        ${f.cascadeParent ? `<span class="ai-ftype ai-fcascade" title="父级选了什么，这里只列它下面的选项">级联 ${esc(labelOf(form, f.cascadeParent))}</span>` : ""}
      </div>`).join("")}</div>`;
  }).join("");

  const req = form.fields.filter((f) => f.required).length;
  const rules = (form.hideRules || []);
  const ruleHtml = rules.length ? `
    <div class="ai-rules">
      <div class="ai-rules-head">条件隐藏 <span class="muted">${rules.length} 条 · 条件成立即隐藏</span></div>
      ${rules.map((r) => `<div class="ai-rule">
        <span class="chip">${esc(labelOf(form, r.source))} ${esc(OP_LABEL[r.operator] || r.operator)} ${esc(r.valueLabel || r.value || "")}</span>
        <span class="muted">→ 隐藏</span>
        <span class="chip accent">${esc(labelOf(form, r.target))}</span>
      </div>`).join("")}
    </div>` : "";

  return `
    <div class="ai-form-head">
      <span class="chip">${esc(form.name)}</span>
      <span class="muted">${form.fieldCount} 个控件 · ${req} 个必填 · ${form.layout === 1 ? "一行一列" : "一行两列"} · 标签宽 ${form.labelWidth}px</span>
    </div>
    <div class="ai-form-body">${body}</div>${ruleHtml}`;
}

/* 控件编码 → 显示标签，级联/隐藏规则里都靠它说人话 */
function labelOf(form, name) {
  const hit = (form.fields || []).find((f) => f.name === name);
  return hit ? hit.label : name;
}

const OP_LABEL = { equal: "等于", not_equal: "不等于", is_empty: "为空", is_not_empty: "不为空" };

/* 列表配置预览：视图 + 列 + 排序 */
function renderAiList(list) {
  const VIEW = { grid: "表格", board: "看板", gantt: "甘特", chart: "图表" };
  const views = list.views.map((v) => `<span class="chip accent">${VIEW[v] || v}</span>`).join(" ");
  const cols = list.columns.map((c) => `<span class="chip">${esc(c.label)}</span>`).join(" ");
  const extra = [];
  if (list.boardGroup) extra.push(`看板按「${esc(list.boardGroup)}」分列`);
  if (list.gantt) extra.push(`甘特 ${esc(list.gantt.start)} → ${esc(list.gantt.end)}`);
  if (list.sort) extra.push(`排序 ${esc(list.sort)}`);
  // 级联详情：下拉选一个显示列，点它打开详情表单（全字段只读）。
  // 选择直接写进 state.aiData.list.detailField，出包原样带回。
  const opts = ['<option value="">不开详情联动</option>']
    .concat((list.columns || []).map(
      (c) => `<option value="${esc(c.code)}"${list.detailField === c.code ? " selected" : ""}>${esc(c.label)}</option>`
    )).join("");
  return `
    <div class="ai-list-body">
      <div class="ai-kv"><span>视图</span><b>${views}</b></div>
      <div class="ai-kv"><span>显示列</span><b>${cols}</b></div>
      <div class="ai-kv"><span>级联详情</span>
        <b><select id="aiDetailField" class="ai-input" style="max-width:220px">${opts}</select></b>
      </div>
      <div class="ai-note-line">· 开启后生成两个表单：新建表单（可编辑）+ 详情表单（全只读）；列表里点击所选列即全屏打开详情</div>
      ${extra.map((t) => `<div class="ai-note-line">· ${t}</div>`).join("")}
    </div>`;
}

// 级联详情下拉的选择写回 state（渲染完成后调用一次）
function bindDetailFieldPicker() {
  const df = $("#aiDetailField");
  if (df) {
    df.onchange = () => {
      if (state.aiData && state.aiData.list) state.aiData.list.detailField = df.value;
      toast(df.value ? `点击「${df.selectedOptions[0].textContent}」列将打开详情表单` : "已关闭详情联动");
    };
  }
}

function renderAiActions(d) {
  const foot = $("#aiActions");
  if (!foot) return;
  // 卡内只留「重新生成」，其余动作统一放在外面的 step-foot，
  // 避免同一个操作在两处出现、改一处忘一处
  foot.innerHTML = `
    <span class="sub">上面的表格可以直接改字段名、类型、选项值</span>
    <div class="ai-btns">
      <button class="btn ghost" id="aiRedo">不满意，重新生成</button>
    </div>`;
  $("#aiRedo").onclick = () => {
    state.aiData = null;
    $("#aiResult").innerHTML = "";
    goStep(1);
    const t = $("#aiChatText1");
    if (t) t.focus();
  };

  // 对象 / 表单 / 列表 三个预览页签
  $$(".ai-tab").forEach((tab) => {
    tab.onclick = () => {
      $$(".ai-tab").forEach((t) => t.classList.toggle("active", t === tab));
      $$("[data-ai-panel]").forEach((panel) => {
        panel.hidden = panel.dataset.aiPanel !== tab.dataset.ai;
      });
    };
  });

  bindDetailFieldPicker();
  bindAiEdit();
  renderAiRelations(d);
}

/* 预览表格的编辑：改什么直接写回 state.aiData.bo.fields，导出时就是改后的 */
function bindAiEdit() {
  const d = state.aiData;
  if (!d || !d.bo) return;

  $$("#aiResult .ai-edit tbody tr").forEach((tr) => {
    const idx = Number(tr.dataset.fi);
    const f = d.bo.fields[idx];
    if (!f) return;

    tr.querySelectorAll("[data-k]").forEach((el) => {
      const handler = () => {
        const k = el.dataset.k;
        if (k === "length") {
          const n = parseInt(el.value, 10);
          f.length = Number.isFinite(n) && n > 0 ? n : null;
        } else if (k === "objectType") {
          // 选项字段这里填的是候选值（逗号分隔），关联对象字段填对象编码
          if (f.type === "dic") {
            f.choices = el.value.split(/[,，;；|\n]/).map((s) => s.trim()).filter(Boolean);
          } else {
            f.objectType = el.value.trim();
          }
        } else {
          f[k] = el.value;
        }
      };
      el.addEventListener(el.tagName === "SELECT" ? "change" : "input", handler);
      if (el.tagName === "SELECT") {
        // 类型一改，「关联/选项」那列的意义就变了，直接重渲染整块最省事
        el.addEventListener("change", () => {
          f.type = el.value;
          // 下拉框 ↔ 选项 互转时，旧的值清掉：对象编码混进候选值会生成垃圾选项，
          // 反过来候选值当对象编码又会变成空下拉
          if (f.type === "dic") {
            f.objectType = "";
            f.choices = f.choices || [];
          } else if (f.type === "object") {
            f.choices = [];
          }
          const keep = d.bo.fields;
          $("#aiResult").innerHTML = renderAiBo(d);
          renderAiActions(d);
          if (keep.length !== d.bo.fields.length) toast("字段数变了，已刷新预览");
        });
      }
    });

    const del = tr.querySelector("[data-del]");
    if (del) {
      del.onclick = () => {
        d.bo.fields.splice(idx, 1);
        $("#aiResult").innerHTML = renderAiBo(d);
        renderAiActions(d);
        toast(`已删掉「${f.name}」`);
      };
    }
  });
}

/* 把项目经理自己整理的 Excel 交给 AI，映射成标准对象再预览 */
async function analyzeSheet(file, sheet) {
  const btn = $("#btnPickSheet") || $("#aiFromSheet");
  const box = $("#aiResult");
  const d1 = $("#aiChatText1");
  const hint = d1 ? d1.value.trim() : "";   // 首屏聊天框里写了就当作补充说明
  state.aiFile = file;                        // 换一张表单时不用再选一次文件
  goStep(2);
  if (btn) { btn.disabled = true; btn.textContent = "读表中…"; }
  box.hidden = false;
  box.classList.remove("is-error");
  const which = sheet ? `「${esc(sheet)}」` : `「${esc(file.name)}」`;
  box.innerHTML = `<div class="ai-loading"><div class="spinner"></div>
    <span>正在读${which}并让 AI 映射字段…（表单多时约半分钟）</span></div>`;
  try {
    const q = `hint=${encodeURIComponent(hint)}` +
      (sheet ? `&sheet=${encodeURIComponent(sheet)}` : "");
    const res = await fetch(`/api/ai/sheet?${q}`, {
      method: "POST",
      body: await file.arrayBuffer(),
    });
    const d = await res.json().catch(() => ({}));
    if (!res.ok || d.ok === false) throw new Error(d.error || `分析失败 ${res.status}`);
    box.innerHTML = renderAiBo(d);
    bindAiSheetPick(d);
    renderAiActions(d);
    toast(`已按「${d.sheets && d.sheets[0] ? d.sheets[0] : "你的表"}」生成字段清单，可直接改`);
  } catch (err) {
    box.classList.add("is-error");
    box.innerHTML = `<div class="ai-err">✗ ${esc(err.message)}</div>`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "选择 Excel"; }
  }
}

/* 一张 Excel 里可能塞了二三十张表单——只生成第一张多半不是他要的，给个下拉换 */
function bindAiSheetPick(d) {
  const box = $("#aiSheetPick");
  if (!box) return;
  const list = (d.formSheets || []).filter((x) => x.fieldCount > 0);
  if (list.length < 2) { box.hidden = true; return; }

  const cur = (d.sheets && d.sheets[0]) || "";
  const forms = list.filter((x) => x.kind === "form").length;
  const opts = list.map((x) => {
    const tag = x.kind === "form" ? "表单" : "数据表";
    const label = `${x.name}｜${tag} ${x.fieldCount} 项`;
    return `<option value="${esc(x.name)}" ${x.name === cur ? "selected" : ""}>${esc(label)}</option>`;
  }).join("");

  box.hidden = false;
  box.innerHTML = `
    <span class="ai-sheetpick-tip">
      这个文件里有 <b>${list.length}</b> 张表${forms ? `（${forms} 张表单）` : ""}，当前分析的是：
    </span>
    <select id="aiSheetSel">${opts}</select>
    <span class="muted">换一张会重新分析（约半分钟）</span>`;

  $("#aiSheetSel").onchange = (e) => {
    const name = e.target.value;
    if (!name || name === cur || !state.aiFile) return;
    analyzeSheet(state.aiFile, name);
  };
}

/* 把 AI 生成的对象写进一份完整模板 Excel，交给用户继续填表单/列表。
   thenBuild=true 时不下载，而是直接把这份 Excel 交给第 3 步出包。 */
async function applyAiBo(d, { thenBuild = false } = {}) {
  const btns = ["#aiApply", "#btnExportXlsx2", "#btnToPack"]
    .map((sel) => $(sel)).filter(Boolean);
  const label0 = thenBuild ? "生成导入包…" : "生成 Excel…";
  btns.forEach((b) => { b.disabled = true; });
  if (thenBuild) $("#btnToPack").textContent = label0;
  try {
    const relHeader = window.Relations ? window.Relations.decisionsHeader("ai") : null;
    const blob = await apiPostBlob("/api/ai/bo/apply", {
      bo: d.bo, form: d.form, list: d.list,
    }, relHeader ? { "X-Relations": relHeader } : {});
    const name = `PP6-${d.bo.name}.xlsx`;
    if (thenBuild) {
      // 用这份 Excel 走原有的解析 → 出包链路（预览里的改动才不会丢）
      const file = new File([blob], name, { type: blob.type });
      await handleFile(file, { fromAi: true });
    } else if (await saveBlob(blob, name)) {
      toast(`已导出 ${name}，线下改完拖回第 1 步即可继续`, 7000);
    }
  } catch (err) {
    toast("生成失败：" + err.message, 6000);
  } finally {
    btns.forEach((b) => { b.disabled = false; });
    const apply = $("#aiApply");
    if (apply) apply.textContent = "套用并下载 Excel";
    const toPack = $("#btnToPack");
    if (toPack) toPack.textContent = "下一步：生成导入包 →";
  }
}

/* 步骤切换：1 输入 / 2 预览 / 3 出包 */
function goStep(n, { scroll = true } = {}) {
  const steps = { 1: "#stepInput", 2: "#stepPreview", 3: "#result" };
  Object.entries(steps).forEach(([k, sel]) => {
    const el = $(sel);
    if (el) el.hidden = Number(k) !== n;
  });
  $$("#steps .step").forEach((el) => {
    const cur = Number(el.dataset.step);
    el.classList.toggle("active", cur === n);
    el.classList.toggle("done", cur < n);
  });
  if (!scroll) return;   // 在「流程模式」页上传时不抢滚动位置
  // 页面滚动已交给 .workspace，window.scrollTo 作为兼容兜底保留
  const ws = $("#workspace");
  if (ws) ws.scrollTo({ top: 0, behavior: "smooth" });
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function backToInput() {
  // 回到第 1 步：清空文件选择框，改完 Excel 后再选同一个文件也能触发 change；
  // 同时允许下一次解析重新弹「相近字段确认」，不会因为上一轮已弹过而被吞掉。
  const fi = $("#fileInput");
  if (fi) fi.value = "";
  const wf = $("#wfFileInput");
  if (wf) wf.value = "";
  state.plModalShown = false;
  goStep(1);
}

/* 第 1 步：描述 → AI 生成预览 */
async function generatePreview() {
  const desc = ($("#aiChatText1") ? $("#aiChatText1").value : "").trim();
  if (!desc) { toast("先说说这个对象要记录什么"); return; }
  if (!window.PP6_AI_READY) {
    toast("还没配置 AI，先填一个免费的 Key", 4000);
    if (typeof window.PP6_OPEN_AI === "function") window.PP6_OPEN_AI();
    return;
  }
  const btn = $("#aiChatSend1");
  const box = $("#aiResult");
  btn.disabled = true; btn.textContent = "生成中…";
  goStep(2);
  box.classList.remove("is-error");
  box.innerHTML = `<div class="ai-loading"><div class="spinner"></div>
    <span>正在让 AI 设计字段、表单和列表…（约 20 秒）</span></div>`;
  try {
    const known = state.data ? state.data.objects.map((o) => o.code) : [];
    const d = await apiPost("/api/ai/bo", { description: desc, known });
    box.innerHTML = renderAiBo(d);
    renderAiActions(d);
    // 新一轮生成，上一轮的对话记录不再适用
    const log = $("#aiChatLog");
    if (log) log.innerHTML = "";
  } catch (err) {
    box.classList.add("is-error");
    box.innerHTML = `<div class="ai-err">✗ ${esc(err.message)}</div>`;
  } finally {
    btn.disabled = false; btn.textContent = "生成预览";
  }
}

/* ---------------- 预览后的 AI 对话式微调 ----------------
   用户看到结果不满意时，不用回到第 1 步重新描述；
   直接在对话框里说「把 X 改成必填」「再加一个 Y 字段」，
   后端在现有 JSON 上做增量修改，已经调好的部分不会被推翻。 */

/* 气泡写到当前输入框所属的日志区（首屏 or 预览页） */
function aiChatLogEl() {
  return document.getElementById(aiChatTarget) || $("#aiChatLog");
}

function aiChatAppend(role, text) {
  const log = aiChatLogEl();
  if (!log) return;
  const el = document.createElement("div");
  el.className = `ai-msg ${role}`;
  el.textContent = text;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

function aiChatAppendChanges(changes) {
  if (!changes || !changes.length) return;
  const log = aiChatLogEl();
  if (!log) return;
  const el = document.createElement("div");
  el.className = "ai-msg bot";
  el.innerHTML = `<div class="ai-changes">${changes
    .map((c) => `<div class="ai-change">· ${esc(c)}</div>`).join("")}</div>`;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
}

/* 一句话就是全部输入：没有结果时它负责「从零设计」，有结果时负责「增量改」。
   用户不该先点一个「生成」按钮才能开始说话。 */
async function sendAiChat(text, { fresh = false } = {}) {
  const msg = (text != null ? text : $("#aiChatText").value).trim();
  if (!msg) { toast("说说你要建什么"); return; }
  if (!window.PP6_AI_READY) {
    toast("还没配置 AI，先填一个免费的 Key", 4000);
    if (typeof window.PP6_OPEN_AI === "function") window.PP6_OPEN_AI();
    return;
  }

  // 首轮 = 还没有任何结果，或者用户是从第 1 步首屏重新说需求（fresh）。
  // 只按 state.aiData 判断会把「返回第 1 步改完描述再点开始生成」误当成
  // 对旧结果的增量修改，于是只调 /api/ai/refine、不 goStep(2)，看起来卡在第 1 步。
  const first = !!fresh || !state.aiData;
  const input = $("#aiChatText");
  const btn = $("#aiChatSend");
  input.value = "";
  aiChatAppend("user", msg);
  const pending = aiChatAppend("bot pending", first ? "正在设计字段、表单和列表…" : "正在修改…");
  btn.disabled = true;
  btn.textContent = first ? "生成中…" : "修改中…";

  const box = $("#aiResult");
  if (first) {
    goStep(2);                       // 让右侧结果区显现
    box.classList.remove("is-error");
    box.innerHTML = `<div class="ai-loading"><div class="spinner"></div>
      <span>正在让 AI 设计字段、表单和列表…（约 20 秒）</span></div>`;
  }

  try {
    let d;
    if (first) {
      const known = state.data ? state.data.objects.map((o) => o.code) : [];
      d = await apiPost("/api/ai/bo", { description: msg, known });
      d._first = true;
    } else {
      d = await apiPost("/api/ai/refine", {
        instruction: msg,
        bo: state.aiData.bo,
        form: state.aiData.form,
        list: state.aiData.list,
        objects: state.data ? state.data.objects.map((o) => ({ code: o.code, name: o.name })) : [],
      });
    }
    // 后端返回的都是完整结果，直接整块替换预览（手改过的字段以 AI 结果为准）
    state.aiData = d;
    box.classList.remove("is-error");
    box.innerHTML = renderAiBo(d);
    renderAiActions(d);
    pending.remove();
    const nf = (d.bo && d.bo.fields ? d.bo.fields.length : 0);
    aiChatAppend("bot", first
      ? `做好了：${d.bo.name}，${nf} 个字段，表单和列表也一起配好了。右侧你可以直接改，也可以继续跟我说要调的地方。`
      : `改好了，用时 ${d.seconds || "?"} 秒。`);
    if (!first) aiChatAppendChanges(d.changes);
    if (d.notices && d.notices.length) {
      aiChatAppend("bot note", d.notices.slice(0, 4).join("\n"));
    }
    $("#aiChatHint") && ($("#aiChatHint").textContent = "在现有基础上改，已经调好的字段不会被推翻");
    toast(first ? "已生成，继续说要改的地方即可" : "已按你的要求更新预览");
  } catch (err) {
    pending.remove();
    if (first) {
      box.classList.add("is-error");
      box.innerHTML = `<div class="ai-err">✗ ${esc(err.message)}</div>`;
      goStep(1);
    }
    aiChatAppend("bot err", (first ? "没生成出来：" : "没改成：") + err.message);
  } finally {
    btn.disabled = false;
    // 首屏的按钮永远回到「开始生成」；只有预览页的增量修改才是「发送修改」
    btn.textContent = first ? "开始生成" : "发送修改";
    input.focus();
  }
}

/* 首屏和预览页各一套对话框：首屏负责「从零」，预览页负责「接着改」。
   两边共用同一个输入源，任一框里说的都走 sendAiChat。 */
function aiChatSendFrom(idText, idLog, { fresh = false } = {}) {
  const ta = document.getElementById(idText);
  const msg = ta ? ta.value : "";
  if (ta) ta.value = "";
  aiChatTarget = idLog;            // 气泡写到当前这个输入框所属的日志区
  sendAiChat(msg, { fresh });
}

let aiChatTarget = "aiChatLog";

function bindAiChat() {
  // 首屏
  const s1 = $("#aiChatSend1");
  if (s1) s1.onclick = () => aiChatSendFrom("aiChatText1", "aiChatLog1", { fresh: true });
  const t1 = $("#aiChatText1");
  if (t1) {
    t1.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        aiChatSendFrom("aiChatText1", "aiChatLog1", { fresh: true });
      }
    });
  }
  const c1 = $("#aiChatClear1");
  if (c1) {
    c1.onclick = () => {
      const log = $("#aiChatLog1");
      if (log) log.innerHTML = "";
      toast("对话已清空");
    };
  }

  // 预览页
  const send = $("#aiChatSend");
  if (send) send.onclick = () => aiChatSendFrom("aiChatText", "aiChatLog");
  const input = $("#aiChatText");
  if (input) {
    // Enter 发送，Shift+Enter 换行
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        aiChatSendFrom("aiChatText", "aiChatLog");
      }
    });
  }
  const clear = $("#aiChatClear");
  if (clear) {
    clear.onclick = () => {
      const log = $("#aiChatLog");
      if (log) log.innerHTML = "";
      toast("对话已清空");
    };
  }

  // 首屏快捷 chip：填进输入框再发，用户看得见自己要的是什么
  $$("#aiChatLaunch .chip-btn").forEach((b) => {
    b.onclick = () => {
      const q = b.dataset.q || b.textContent;
      const ta = $("#aiChatText1");
      if (ta) ta.value = q;
      aiChatSendFrom("aiChatText1", "aiChatLog1", { fresh: true });
    };
  });
}

async function apiPostBlob(path, payload, extraHeaders) {
  const res = await fetch(path, {
    method: "POST",
    headers: Object.assign({ "Content-Type": "application/json" }, extraHeaders || {}),
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || "请求失败");
  }
  return res.blob();
}

/* ================================================ P0-1 / P0-4 / P0-5 新增函数块
   只新增下列命名，禁止覆盖 / 重命名任何现有 renderXxx() / buildZip() / inspectZip()。
   两态共用同一个 renderPackage() + 同一段 renderPackageTable() 模板；
   复用既有 CSS 类，style.css 零改动。                                       */

const PP6ZIP_IMPORT_PATH = "系统管理 › 发布管理 › 导入同步包";
const PP6ZIP_NOTE_FORMAT = "CONFIG_SYNC_NOTE:<发布说明>";
const PP6ZIP_LEGAL_RE = /^(bo|form|data_template|dataset)\/\d{3}-[a-z0-9_]+\.xml$/;
const PP6ZIP_TIPS = [
  "导入后到「系统管理 › 发布管理 › 导入同步包」历史表核对：4 个数量列（bo / form / data_template / dataset，口径待复核）与状态「成功」。",
  "常见失败原因：包里多放了 manifest / catalog / txt / 图标 / 说明文件 → 线上报「不支持的同步对象类型」。",
  "外层再套一层文件夹、或平铺多个 zip → 线上报「同步包文件缺少业务类型目录」。",
  "序号重号会出现 Duplicate name 警告并静默丢内容；重复推送不去重，导入端按 id 处理。",
  "不要往包里加 manifest、txt、图标；也不要把里面的文件拆出来单独传。",
];

/**
 * base64(UTF-8 JSON) -> object；头缺失 / 解码失败一律返回 null（降级）。
 * 关键：atob 输出是 latin-1 二进制串，必须经 TextDecoder("utf-8")
 * 解码后再交给 JSON.parse；直接把 atob 结果喂给 JSON.parse 会让中文乱码（严禁）。
 */
function decodeHeaderJson(raw) {
  if (!raw) return null;
  try {
    const bin = atob(raw);
    const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
    return JSON.parse(new TextDecoder("utf-8").decode(bytes));
  } catch (_) {
    return null;                 // 降级：不抛、不阻断、不报错给用户
  }
}

/**
 * 反解分类（§6.3 六类优先级，先命中先返回）。
 * @returns {{kind, skipDownload, title, message, recovery, summary}}
 */
function classifyInspect(res, summary, err) {
  const base = { summary: summary || null };
  if (summary === null) {
    return Object.assign(base, {
      kind: "legacy", skipDownload: false,
      title: "", message: "", recovery: "",
    });
  }
  const entries = Array.isArray(summary.entries) ? summary.entries : [];
  const white = ["bo", "form", "data_template", "dataset"];
  const names = entries.map((e) => e.name || "");
  const listText = names.length
    ? "实际读到：" + names.slice(0, 30).join("、")
      + (summary.truncated && !summary.summaryOnly && summary.entriesTotal > names.length
        ? ` …（仅显示前 ${names.length} 条，共 ${summary.entriesTotal} 条）` : "")
    : "实际读到 0 个条目";

  // 1 非 zip / 损坏
  if (!res.ok || summary.ok === false) {
    return Object.assign(base, {
      kind: "not_zip", skipDownload: true,
      title: "这不是一个有效的 zip 包（或包已损坏）",
      message: (summary.message || (err && err.message) || "无法打开为 zip 包。") + "。" + listText,
      recovery: "确认选的是 .zip；重新从系统「发布 ZIP」下载（.crdownload 直接改名 .zip）；或到工作区 3「存量包修订」验证能否打开。",
    });
  }
  /* 是否「外层多套了一层」：首段不是合法目录，但路径里出现 /bo/ 之类。
     必须与后端的 _nested() 同口径，否则会出现「后端说套层、前端说多余条目」。 */
  const isNestedName = (name) => {
    const low = String(name || "").toLowerCase();
    if (!low.endsWith(".xml")) return false;
    if (white.indexOf(low.split("/", 1)[0]) >= 0) return false;
    return /(^|\/)(bo|form|data_template)\//.test(low);
  };
  // 2 外层被套了一层（必须先判：套层包的顶层目录同样不在白名单里）
  const nested = entries.filter((e) => isNestedName(e.name));
  if (nested.length) {
    return Object.assign(base, {
      kind: "nested_dir", skipDownload: true,
      title: "包外层被套了一层文件夹（线上原话：同步包文件缺少业务类型目录）",
      message: "服务端按最外层目录名识别业务类型，多套一层就找不到 bo/ form/ data_template/。" + listText,
      recovery: "解压后把 bo/ form/ data_template/ 直接放在 zip 最外层重新压缩，不要再套一层文件夹。",
    });
  }
  // 3 多余条目（排除已判为套层的条目，避免同一批文件报两次）
  const extras = entries.filter((e) => white.indexOf(e.kind) < 0 && !isNestedName(e.name));
  if (extras.length) {
    return Object.assign(base, {
      kind: "extra_entries", skipDownload: true,
      title: "包里有系统不认的多余条目（线上原话：不支持的同步对象类型）",
      message: "顶层只认 bo/ form/ data_template/（数据菜单手建数据集为 dataset/ 特例）。" + listText,
      recovery: "只保留 bo/ form/ data_template/ 三类裸 XML；删掉 manifest / txt / 图标 / 说明文件 / __MACOSX；不要往包里加任何新文件。",
    });
  }
  // 4 XML 编码错误
  const badXml = entries.filter((e) => e.utf8 === false);
  if (badXml.length) {
    return Object.assign(base, {
      kind: "encoding", skipDownload: true,
      title: "包内 XML 不是合法 UTF-8 编码",
      message: "这些条目无法按 UTF-8 解码：" + badXml.map((e) => e.name).join("、") + "。" + listText,
      recovery: "把 XML 另存为 UTF-8 无 BOM 后重新打包；或走工作区 1 反解后重新出包。",
    });
  }
  // 5 空包
  if (summary.empty === true) {
    return Object.assign(base, {
      kind: "empty", skipDownload: true,
      title: "包内没有可反解的 bo/form/data_template XML",
      message: "没有下载会误导人的空 xlsx。" + listText,
      recovery: "去工作区 1 重新出包（空模板 / 全不选会被拦）；只是想给线上对象补字段请改用工作区 3「存量包修订」。",
    });
  }
  // 6 正常
  return Object.assign(base, {
    kind: "ok", skipDownload: false,
    title: "反解完成", message: "", recovery: "",
  });
}

/** 统一状态机入口（§6.1）：写 state.zipPhase、切 #banner、调对应 render。 */
function setZipPhase(op, phase, payload) {
  state.zipPhase[op] = phase;
  const p = payload || {};
  const banner = $("#banner");
  if (op === "build") {
    if (phase === "ok") {
      if (banner) {
        banner.className = "banner ok";
        banner.innerHTML = `<strong>交付包已生成</strong>：状态「待导入」，按下方「交付报告」的验收提示自查后到 ${esc(PP6ZIP_IMPORT_PATH)} 导入。`;
        banner.hidden = false;
      }
      renderBuildReport(p.report, p.name);
      renderPackage(p.report, "actual");
    } else if (phase === "error") {
      if (banner) {
        banner.className = "banner error";
        banner.innerHTML = `<strong>生成失败（${esc(p.category || "出包错误")} / ${esc(p.module || "详见『校验结果』页签")}）</strong>：`
          + `${esc(p.message || "请核对后重试")}。去「${esc(p.tab || "校验结果")}」修；`
          + `不要往包里加 manifest、txt、图标。`;
        banner.hidden = false;
      }
    } else if (phase === "empty") {
      if (banner) {
        banner.className = "banner warn";
        banner.innerHTML = `<strong>还没有可导出的内容</strong>：先勾选要导出的模块；`
          + `恢复动作：①「载入示例看效果」② 回到第 1 步描述需求 ③ 去 <code>1_业务对象</code> 填一行。`;
        banner.hidden = false;
      }
    }
    // busy / idle 不动 banner，保留 renderAll() 的校验结论
  } else if (op === "inspect") {
    if (phase === "ok" || phase === "empty" || phase === "error") {
      renderInspectCard({ classify: p.classify, summary: p.summary });
    } else {
      const card = $("#pp6zipInspectCard");
      if (card) { card.hidden = true; card.innerHTML = ""; }
    }
  }
}

/** 归一化：把两种 data 收敛成同一个 PackageVM（D1 单一算法）。 */
function normalizePackageVM(data, mode) {
  const isActual = mode === "actual";
  const d = data || {};
  const vm = {
    mode: isActual ? "actual" : "predict",
    title: isActual ? "实测包结构" : "预计包结构",
    note: isActual ? "实测包结构 · 来自本次下载" : "预计，出包后回填实测",
    dirs: [],
    modules: [],                 // predict 态逐模块占位行（§5.1：说明列来自 modules[]）
    entries: null,
    comment: { text: "", isRaw: false, truncated: false },
    counts: { bo: 0, form: 0, data_template: 0, dataset: 0 },
    countsNote: "口径待复核",
    importPath: PP6ZIP_IMPORT_PATH,
    tips: PP6ZIP_TIPS.slice(),
    truncated: false, entriesTotal: 0, summaryOnly: false, entryCount: 0,
  };
  if (isActual) {
    const c = d.counts || {};
    vm.counts = {
      bo: Number(c.bo || 0), form: Number(c.form || 0),
      data_template: Number(c.data_template || 0), dataset: Number(c.dataset || 0),
    };
    vm.dirs = [
      { kind: "bo", count: vm.counts.bo, label: "业务对象", source: "X-Build-Report.counts", notes: [] },
      { kind: "form", count: vm.counts.form, label: "表单", source: "X-Build-Report.counts", notes: [] },
      { kind: "data_template", count: vm.counts.data_template, label: "列表 / 数据集 / 筛选器", source: "X-Build-Report.counts", notes: [] },
      { kind: "dataset", count: vm.counts.dataset, label: "数据集（手建形态，本工具不产出）", source: "X-Build-Report.counts", notes: [] },
    ];
    vm.entries = Array.isArray(d.entries) ? d.entries : [];
    vm.comment = { text: String(d.comment || ""), isRaw: true, truncated: !!d.commentTruncated };
    vm.truncated = !!d.truncated;
    vm.entriesTotal = Number(d.entriesTotal || d.entryCount || 0);
    vm.summaryOnly = !!d.summaryOnly;
    vm.entryCount = Number(d.entryCount || 0);
  } else {
    // predict 唯一真相来源：modules[].folder / count / zipped；
    // entries 必须为 null，绝不伪造 001-xxx.xml / 假 size / 假 comment 原文。
    const mods = Array.isArray(d.modules) ? d.modules : [];
    const byFolder = {};
    mods.forEach((m) => {
      const k = m.folder || m.code || "other";
      if (!byFolder[k]) {
        byFolder[k] = { kind: k, count: 0, label: m.label || k,
                        source: "modules[].folder / count", notes: [] };
      }
      byFolder[k].count += Number(m.count || 0);
      byFolder[k].label = m.label || k;
      if (!m.zipped && byFolder[k].notes.indexOf("不进包（zipped=false，裸 XML）") < 0) {
        byFolder[k].notes.push("不进包（zipped=false，裸 XML）");
      }
    });
    vm.dirs = Object.keys(byFolder).map((k) => byFolder[k]);
    vm.modules = mods.map((m) => ({
      folder: m.folder || m.code || "other",
      label: m.label || m.folder || m.code || "",
      count: Number(m.count || 0),
      items: Array.isArray(m.items) ? m.items.slice(0, 3) : [],
      zipped: m.zipped !== false,
    }));
    const c = (k) => Number((byFolder[k] && byFolder[k].count) || 0);
    vm.counts = { bo: c("bo"), form: c("form"), data_template: c("data_template"), dataset: 0 };
    vm.comment = { text: PP6ZIP_NOTE_FORMAT, isRaw: false, truncated: false };
    vm.entries = null;
  }
  return vm;
}

/** 第 7 tab 单一 HTML 模板；vm.entries === null 时渲染占位行而非假文件名。 */
function renderPackageTable(vm) {
  const countRow = `
    <div class="stats">
      <div class="stat ${vm.counts.bo ? "" : "zero"}"><div class="n">${vm.counts.bo}</div><div class="k">bo</div></div>
      <div class="stat ${vm.counts.form ? "" : "zero"}"><div class="n">${vm.counts.form}</div><div class="k">form</div></div>
      <div class="stat ${vm.counts.data_template ? "" : "zero"}"><div class="n">${vm.counts.data_template}</div><div class="k">data_template</div></div>
      <div class="stat ${vm.counts.dataset ? "" : "zero"}"><div class="n">${vm.counts.dataset}</div><div class="k">dataset</div></div>
    </div>
    <div class="set-note">4 个数量列固定为 bo / form / data_template / dataset（<b>${esc(vm.countsNote)}</b>）。
      <code>dataset/</code> 是「数据菜单手建数据集」形态，本工具不产出，恒为 0。
      ${vm.mode === "predict"
        ? "当前是预计值，唯一真相来源是 modules[].count；与 #stats 同源、口径不同时以模块数为准。"
        : "当前是出包后实测值，来自 X-Build-Report（从返回的 zip 字节读取）。"}</div>`;

  const dirRows = (vm.dirs || []).map((g) => `
    <tr><td><code>${esc(g.kind)}/</code></td><td>${Number(g.count)}</td>
        <td>${esc(g.label || "")}${(g.notes || []).length
          ? ` <span class="chip warn">${esc(g.notes.join("、"))}</span>` : ""}</td>
        <td class="sub">${esc(g.source || "")}</td></tr>`).join("")
    || `<tr><td colspan="4" class="sub">（还没有模块数据，先解析一份 Excel）</td></tr>`;

  let entryRows;
  if (vm.entries === null) {
    // D1 硬禁令：predict 态只给占位行，绝不出现伪造的 001-xxx.xml / 假 size。
    // 逐模块一行（§5.1：说明列 = label · 预计 count 项 · items.slice(0,3)），
    // 与 actual 态共用同一段模板。
    const placeholders = (vm.modules || []).length
      ? vm.modules
      : (vm.dirs || []).map((g) => ({ folder: g.kind, label: g.label,
                                      count: g.count, items: [], zipped: true }));
    entryRows = placeholders.map((m) => `
      <tr><td><code>${esc(m.folder)}/</code></td>
          <td class="sub">（出包后显示实际条目名）</td>
          <td>—</td><td>—</td>
          <td class="sub">${esc(m.label)} · 预计 ${Number(m.count)} 项${(m.items || []).length
            ? " · " + esc(m.items.join("、")) : ""}${m.zipped === false
            ? " · 不进包（zipped=false，裸 XML）" : ""}</td></tr>`).join("")
      || `<tr><td colspan="5" class="sub">（出包后显示实际条目名）</td></tr>`;
  } else {
    entryRows = vm.entries.map((e) => `
      <tr><td><code>${esc(e.kind || "")}</code></td>
          <td><code>${esc(e.name || "")}</code></td>
          <td>${e.seq == null ? "—" : e.seq}</td>
          <td>${esc(humanSize(Number(e.size || 0)))}</td>
          <td class="sub">${e.utf8 === false ? '<span class="chip warn">XML 非 UTF-8</span> ' : ""}${PP6ZIP_LEGAL_RE.test(e.name || "") ? "命名合规" : "命名不合规"}</td></tr>`).join("")
      || `<tr><td colspan="5" class="sub">${vm.summaryOnly
            ? "清单过大，仅显示统计摘要（不报错、不阻断）"
            : "（本次包内没有条目）"}</td></tr>`;
  }
  const truncNote = vm.truncated && !vm.summaryOnly
    ? ` <span class="chip warn">仅显示前 ${(vm.entries || []).length} 条，共 ${vm.entriesTotal || vm.entryCount} 条</span>` : "";
  const commentNote = vm.comment.isRaw
    ? (vm.comment.truncated
        ? ' <span class="muted">（原文过长，已截断；CONFIG_SYNC_NOTE: 前缀完整保留）</span>'
        : "")
    : ' <span class="chip warn">格式说明，非原文，出包后回填实测</span>';
  const tips = (vm.tips || []).map((t) => `<li>${esc(t)}</li>`).join("");

  return `
    <div class="card">
      <div class="card-head"><div><h3>${esc(vm.title)}</h3>
        <div class="sub">${esc(vm.note)}</div></div></div>
      ${countRow}
      <div class="set-note"><strong>目录分布</strong>（${esc(vm.countsNote)}）</div>
      <div class="tbl-wrap"><table>
        <thead><tr><th>目录</th><th>条数</th><th>说明</th><th>来源</th></tr></thead>
        <tbody>${dirRows}</tbody></table></div>
      <div class="set-note"><strong>entry 清单</strong>${truncNote}</div>
      <div class="tbl-wrap"><table>
        <thead><tr><th>目录</th><th>条目名</th><th>序号</th><th>大小</th><th>说明 / 来源</th></tr></thead>
        <tbody>${entryRows}</tbody></table></div>
      <div class="set-note"><strong>comment</strong>：<code>${esc(vm.comment.text)}</code>${commentNote}</div>
      <div class="set-note"><strong>导入路径</strong>：${esc(vm.importPath)}</div>
      <div class="set-note"><strong>验收提示</strong><ul>${tips}</ul></div>
    </div>`;
}

/** 第 7 tab「同步包结构」唯一渲染入口（两态共用同一模板）。 */
function renderPackage(data, mode) {
  const panel = $('.panel[data-panel="package"]');
  if (!panel) return;
  panel.innerHTML = renderPackageTable(normalizePackageVM(data, mode));
}

/** 反解摘要 / 空态 / 异常分类卡片（容器在 #stepInput 内，goStep(1) 后可见）。 */
function renderInspectCard(payload) {
  const box = $("#pp6zipInspectCard");
  if (!box) return;
  const cls = payload && payload.classify ? payload.classify : null;
  const s = payload && payload.summary ? payload.summary : null;
  if (!cls || cls.kind === "legacy" || !s) {
    box.hidden = true; box.innerHTML = ""; return;
  }
  const entries = Array.isArray(s.entries) ? s.entries : [];
  const names = entries.map((e) => e.name || "");
  const badNames = names.filter((n) => !PP6ZIP_LEGAL_RE.test(n));
  const seqs = entries.map((e) => e.seq).filter((x) => typeof x === "number");
  const dup = seqs.length !== new Set(seqs).size;
  const sorted = [...seqs].sort((a, b) => a - b);
  const gap = sorted.some((v, i) => i > 0 && v !== sorted[i - 1] + 1);
  let verdict;
  if (!names.length) verdict = "没有条目，无法判定";
  else if (badNames.length) verdict = `有 ${badNames.length} 条不符合 ^(bo|form|data_template)/\\d{3}-[a-z0-9_]+\\.xml$（dataset/ 为特例）`;
  else if (dup || gap) verdict = "序号存在重号或不连续（Duplicate name 警告会静默丢内容）";
  else verdict = "命名合规、序号跨目录连续无重号";

  const counts = s.counts || {};
  const dirText = Object.keys(counts).length
    ? Object.keys(counts).map((k) => `${esc(k)}/ ${Number(counts[k])}`).join(" · ")
    : "（无）";
  const rows = entries.map((e) => `
    <tr><td>${esc(e.kind || "")}</td><td><code>${esc(e.name || "")}</code></td>
        <td>${e.seq == null ? "—" : e.seq}</td>
        <td>${esc(humanSize(Number(e.size || 0)))}</td>
        <td>${e.utf8 === false
          ? '<span class="chip warn">非 UTF-8</span>'
          : (PP6ZIP_LEGAL_RE.test(e.name || "") ? "合规" : "不合规")}</td></tr>`).join("");
  const isOk = cls.kind === "ok";
  const headCls = isOk ? "banner ok" : (cls.kind === "empty" ? "banner warn" : "banner error");
  const recovery = cls.recovery
    ? `<div class="set-note"><strong>恢复动作</strong>：${esc(cls.recovery)}</div>` : "";
  const extraOk = isOk
    ? `<div class="set-note">反解会拆成 Excel 重造包，原生包未建模内容（对象角色 / 日志模板 / 字典样式 / 权限）会丢；补字段请改用工作区 3「存量包修订」。</div>`
    : "";
  box.hidden = false;
  box.innerHTML = `
    <div class="card">
      <div class="card-head"><div><h3>反解摘要</h3>
        <div class="sub">${isOk ? "已下载 Excel；下面是上传包的实测结构" : "未下载 Excel（避免误导性的空 xlsx）"}</div></div></div>
      <div class="${headCls}">${isOk
        ? `<strong>${esc(cls.title)}</strong>：包内 ${Number(s.entryCount || 0)} 个条目，命名合规结论见下表。`
        : `<strong>${esc(cls.title)}</strong>：${esc(cls.message || "")}`}</div>
      <div class="tbl-wrap"><table><tbody>
        <tr><th>条目数</th><td>${Number(s.entryCount || 0)}${s.truncated && !s.summaryOnly
          ? ` <span class="chip warn">仅显示前 ${entries.length} 条，共 ${s.entriesTotal || s.entryCount} 条</span>` : ""}${s.summaryOnly
          ? ' <span class="chip warn">清单过大，仅显示统计摘要</span>' : ""}</td></tr>
        <tr><th>目录分布</th><td>${dirText} <span class="muted">（口径待复核）</span></td></tr>
        <tr><th>comment 原文</th><td><code>${esc(s.comment || "")}</code>${s.commentTruncated
          ? ' <span class="muted">（已截断，CONFIG_SYNC_NOTE: 前缀完整保留）</span>' : ""}</td></tr>
        <tr><th>实际读到的条目名</th><td>${names.length ? `<code>${esc(names.join("、"))}</code>` : "（无）"}</td></tr>
        <tr><th>命名合规结论</th><td>${esc(verdict)}</td></tr>
      </tbody></table></div>
      ${entries.length
        ? `<div class="tbl-wrap"><table><thead><tr><th>目录</th><th>条目名</th><th>序号</th><th>大小</th><th>结论</th></tr></thead><tbody>${rows}</tbody></table></div>`
        : '<div class="empty">包内没有可列出的条目。</div>'}
      ${recovery}${extraOk}
    </div>`;
}

/** 出包「交付报告」卡片 8 字段（包名 / 目录清单 / 条目数 / comment / 状态 / 时间 / 导入路径 / 验收提示）。 */
function renderBuildReport(report, name) {
  const box = $("#pp6zipBuildReport");
  if (!box || !report) return;
  const c = report.counts || {};
  const n = (k) => Number(c[k] || 0);
  const pkgName = name || report.packageName || "pp6-import.zip";
  const time = report.time || (new Date().toLocaleString("zh-CN") + "（本地生成时间）");
  const tips = PP6ZIP_TIPS.map((t) => `<li>${esc(t)}</li>`).join("");
  box.hidden = false;
  box.innerHTML = `
    <div class="card">
      <div class="card-head"><div>
        <h3>交付报告 <span class="chip accent">${esc(report.status || "待导入")}</span></h3>
        <div class="sub">实测包结构 · 来自本次下载；本地留痕，不连线上系统</div></div></div>
      <div class="tbl-wrap"><table><tbody>
        <tr><th>包名</th><td><code>${esc(pkgName)}</code></td></tr>
        <tr><th>目录清单</th><td>bo/ ${n("bo")} 条 · form/ ${n("form")} 条 · data_template/ ${n("data_template")} 条 · dataset/ ${n("dataset")} 条
            <span class="muted">（口径待复核；dataset/ 为数据菜单手建数据集形态，本工具不产出）</span></td></tr>
        <tr><th>条目数</th><td>${Number(report.entryCount || 0)}${report.truncated && !report.summaryOnly
          ? ` <span class="chip warn">仅显示前 ${(report.entries || []).length} 条，共 ${report.entriesTotal || report.entryCount} 条</span>` : ""}${report.summaryOnly
          ? ' <span class="chip warn">清单过大，仅显示统计摘要</span>' : ""}</td></tr>
        <tr><th>comment 原文</th><td><code>${esc(report.comment || "")}</code>${report.commentTruncated
          ? ' <span class="muted">（原文过长，已截断；CONFIG_SYNC_NOTE: 前缀完整保留）</span>' : ""}</td></tr>
        <tr><th>状态</th><td><span class="chip accent">${esc(report.status || "待导入")}</span></td></tr>
        <tr><th>生成时间</th><td>${esc(time)}</td></tr>
        <tr><th>导入路径</th><td>${esc(report.importPath || PP6ZIP_IMPORT_PATH)}</td></tr>
      </tbody></table></div>
      <div class="set-note"><strong>验收提示</strong><ul>${tips}</ul></div>
    </div>`;
}

/* ------------------------------------------------ 事件绑定 */

function bind() {
  const pickAndLoad = async () => {
    if (state.desktop) {
      const file = await pickFile("excel");
      if (file) handleFile(file);
      return;
    }
    $("#fileInput").click();
  };

  // ---- 第 1 步：一个输入口（描述 + 拖文件都行）
  const dz = $("#dzInline");
  $("#btnPickSheet").onclick = async () => {
    if (state.desktop) {
      // 桌面壳里 <input type=file> 不可靠，走系统文件对话框
      const file = await pickFile("excel");
      if (file) await analyzeSheet(file);
      return;
    }
    $("#aiSheetInput").click();
  };
  $("#aiSheetInput").onchange = async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (file) await analyzeSheet(file);
  };
  // 拖到输入区 = 走 AI 读表（不是模板导入）
  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => {
    const file = e.dataTransfer.files[0];
    if (file) analyzeSheet(file);
  });

  // ---- 第 1 步（下半）：已经有一份标准 Excel，直接进预览
  $("#reimport").querySelector("span").onclick = pickAndLoad;
  $("#fileInput").onchange = (e) => {
    const f = e.target.files && e.target.files[0];
    // 同一个文件再选一次也要触发 change：否则「出包 → 返回上一步 → 改 Excel →
    // 再选同一个文件」会一点反应都没有，用户以为流程卡死。
    e.target.value = "";
    if (f) handleFile(f);
  };
  const reimportBox = $("#reimport");
  ["dragenter", "dragover"].forEach((ev) =>
    reimportBox.addEventListener(ev, (e) => { e.preventDefault(); reimportBox.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) =>
    reimportBox.addEventListener(ev, (e) => { e.preventDefault(); reimportBox.classList.remove("drag"); }));
  reimportBox.addEventListener("drop", (e) => {
    const file = e.dataTransfer.files[0];
    if (file) handleFile(file);
  });

  $$("#tabs .tab").forEach((tab) => {
    tab.onclick = () => {
      $$("#tabs .tab").forEach((t) => t.classList.toggle("active", t === tab));
      $$(".panel").forEach((p) => { p.hidden = p.dataset.panel !== tab.dataset.panel; });
    };
  });

  // 第 3 步「返回上一步」：有 AI 预览就回预览，否则回输入
  $("#btnReset").onclick = () => {
    if (state.aiData) { goStep(2); return; }
    backToInput();
  };
  $("#btnBackInput").onclick = backToInput;

  const btnConfirmed = $("#btnConfirmedExcel");
  if (btnConfirmed) btnConfirmed.onclick = downloadConfirmedExcel;

  // 「相近字段确认」弹窗：底部四个出口——全选 / 全不选 / 保留我的 / 按勾选覆盖。
  // 「按勾选覆盖」会把勾选并进 state.plAlign 再重新解析（预览与 zip 同一份决策），
  // 「保留我的」只关弹窗、不发 align（等于全部保留我方定义）。
  const plModal = $("#plMask");
  if (plModal) {
    // × / 点遮罩 / Esc = 保留我方并继续：分析已经完成，不能让用户关掉弹窗
    // 之后还停在空白第 1 步；延迟渲染的话，这里才出预览/结果页。
    const dismiss = () => {
      window.ProjLink.closeModal();
      finishPlDeferred();
    };
    const close = $("#plModalClose");
    if (close) close.onclick = dismiss;
    const all = $("#plPickAll");
    if (all) all.onclick = () => window.ProjLink.selectAll(true);
    const none = $("#plPickNone");
    if (none) none.onclick = () => window.ProjLink.selectAll(false);
    // 从确认弹窗直接回第 1 步改 Excel：比先关弹窗、再点结果页「返回上一步」少一步。
    // 回去后文件选择框已清空，同一个文件再选一次会重新解析、重新弹本弹窗。
    const backEdit = $("#plBackEdit");
    if (backEdit) {
      backEdit.onclick = () => {
        state.plDeferred = false;       // 回第 1 步改 Excel：这次不出预览
        window.ProjLink.closeModal();
        backToInput();
        toast("改完 Excel 后，重新选一次这个文件即可重新解析", 6000);
      };
    }
    const back = $("#plLater");
    if (back) {
      back.onclick = () => {
        window.ProjLink.closeModal();
        toast("已保留你方定义：这些相近字段都按原规则生成", 5000);
        finishPlDeferred();
      };
    }
    const confirm = $("#plApplyPicked");
    if (confirm) confirm.onclick = () => { applyProjectPicks(); };
    plModal.addEventListener("click", (e) => {
      if (e.target === plModal) dismiss();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !plModal.hidden) dismiss();
    });
  }
  const plOpen = $("#btnPlModal");
  if (plOpen) {
    plOpen.onclick = () => {
      const vm = state.data && state.data.projectAlign;
      if (!window.ProjLink) return;
      const pending = window.ProjLink.pendingCount(vm);
      const direct = window.ProjLink.directDiffCount
        ? window.ProjLink.directDiffCount(vm) : 0;
      if (!pending && !direct) {
        toast("项目字段没有差异：同名字段已经是项目定义", 5000);
        return;
      }
      window.ProjLink.openModal(vm);
    };
  }

  // 第 1 步「业务对象关联项目」：初始页两档控件（导入前就定），
  // 普通模式 / 流程模式共享同一档，存量包页「按包内」独立一档
  bindPlPre();

  $("#modAll").onclick = () => {
    state.picked = new Set((state.data.modules || []).filter((m) => m.count > 0).map((m) => m.code));
    renderModules(); updateBar();
  };
  $("#modNone").onclick = () => {
    state.picked = new Set();
    renderModules(); updateBar();
  };

  $("#btnDemo").onclick = loadDemo;
  const packBtn = $("#btnPack") || $("#btnBuild");
  if (packBtn) packBtn.onclick = () => buildZip();
  const wfBtn = $("#btnWorkflow");
  if (wfBtn) wfBtn.onclick = () => downloadWorkflow(wfBtn);

  // ---- 首屏对话已由 bindAiChat 绑定，这里不再重复绑「生成预览」

  // 预览后的对话式微调
  bindAiChat();

  // 预览底部：导出 Excel / 去出包
  $("#btnExportXlsx2").onclick = () => {
    if (state.aiData) applyAiBo(state.aiData);
    else toast("先点「生成预览」");
  };
  $("#btnToPack").onclick = () => {
    if (!state.aiData) { toast("还没有预览内容，先生成"); return; }
    // 预览里的改动要落成一份 Excel，再走原有的出包链路
    applyAiBo(state.aiData, { thenBuild: true });
  };
  $("#aiSheetInput").onchange = async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (file) await analyzeSheet(file);
  };

  // ?demo=1 直接进示例视图，便于截图与巡检
  if (new URLSearchParams(location.search).get("demo")) loadDemo();
  $("#btnInspect").onclick = async () => {
    if (state.desktop) { await inspectZip(null); return; }
    $("#inspectInput").click();
  };

  // 空白模板入口已下线：主流程改成「说需求或传表 → AI 生成 → 预览」，
  // 不再要求先下模板再填。需要一份现成结构可以点「载入示例看效果」。
  $("#inspectInput").onchange = (e) => {
    inspectZip(e.target.files[0]);
    e.target.value = "";
  };
}

  detectDesktop();
  bind();

/* ============================================================ 流程工作区
 *
 * 流程是**裸 XML**，不进同步包（导入走 管理 › 流程设计 › 导入），
 * 所以它单独占一个左侧工作区，而不是混在「要导出哪些模块」里。
 *
 * 这个工作区自带 Excel 导入：流程模式的对象、它的表单/列表、流程定义
 * 都从同一份填写表来，没必要让用户先去「普通模式」传一遍再切回来。
 *
 * 数据来自同一次 /api/parse（webui._wf_payload），这里只负责渲染：
 * 左列流程清单、右列节点表 + 流转表。下载复用 downloadWorkflow()。
 */

/* 导入区状态：文件名 + 这次会出多少东西。
   数字从 state.data 算，和 parse 接口同源，不另开统计口径。 */
function renderWfImport() {
  const tag = $("#wfFileTag");
  const stat = $("#wfImportStat");
  if (!tag || !stat) return;             // 面板不在（旧 HTML）
  if (!state.file) {
    tag.hidden = true;
    stat.className = "wf-import-stat";
    stat.textContent = "还没有导入：点右上「选择 Excel」，或把 .xlsx 直接拖到这里";
    return;
  }
  tag.hidden = false;
  tag.textContent = state.file.name;
  tag.title = state.file.name;

  const d = state.data || {};
  const objs = (d.objects || []).filter((o) => o.flowDriven);
  if (!objs.length) {
    stat.className = "wf-import-stat";
    stat.innerHTML = `已解析 <b>${esc(state.file.name)}</b>，但里面没有流程驱动对象：` +
      `到 <code>1_业务对象</code> 把「流程驱动」改成「是」就会自动补一条默认流程。`;
    return;
  }
  const codes = new Set(objs.map((o) => o.code));
  const forms = (d.forms || []).filter((f) => codes.has(f.boCode));
  const wfs = (d.workflows || []).length;
  stat.className = "wf-import-stat ok";
  stat.innerHTML = `本次会出：<b>${objs.length}</b> 个流程对象 · <b>${forms.length}</b> 张表单 · ` +
    `<b>${wfs}</b> 条流程定义`;
}

/* 流程页上传：解析完留在本页（quiet），直接能出对象包和 XML */
function bindWfImport() {  const pick = $("#btnWfPick");
  const input = $("#wfFileInput");
  const box = $("#wfImport");
  if (!pick || !input) return;
  pick.onclick = () => input.click();
  input.onchange = (e) => {
    const f = e.target.files && e.target.files[0];
    e.target.value = "";                 // 同一个文件再选一次也要触发 change
    if (f) handleFile(f, { quiet: true });
  };
  if (!box) return;
  box.ondragover = (e) => { e.preventDefault(); box.classList.add("dz-over"); };
  box.ondragleave = () => box.classList.remove("dz-over");
  box.ondrop = (e) => {
    e.preventDefault();
    box.classList.remove("dz-over");
    const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) handleFile(f, { quiet: true });
  };
}

/* 一句话生成/修改流程：把当前 xlsx + 描述发给 /api/ai/workflow，
   后端只重写 9/10/11 三张表并回一份新 Excel；这里拿它替换当前文件，
   后面的预览和导出就都基于新流程。 */
async function aiMakeWorkflow() {
  if (!state.file) { toast("先在上面导入 Excel：AI 要知道流程挂在哪个对象上"); return; }
  const box = $("#wfAiText");
  const text = (box && box.value || "").trim();
  if (!text) { toast("先说清楚流程要怎么走，例如：部门经理 → 财务 → 总经理"); if (box) box.focus(); return; }
  const btn = $("#wfAiSend");
  const label = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "生成中…"; }
  try {
    const buf = await state.file.arrayBuffer();
    const cur = WfView.current();
    const qs = new URLSearchParams({ instruction: text, focus: cur ? cur.name : "" });
    const res = await fetch("/api/ai/workflow?" + qs.toString(), {
      method: "POST",
      body: buf,
      headers: { "Content-Type": "application/octet-stream" },
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || "生成失败");
    }
    const blob = await res.blob();
    const name = decodeURIComponent(res.headers.get("X-Filename") || "流程草稿.xlsx");
    let summary = null;
    const rawSum = res.headers.get("X-AI-Summary");
    if (rawSum) {
      try { summary = JSON.parse(decodeURIComponent(rawSum)); } catch (_) { /* 头缺失就只提示成功 */ }
    }
    // 用新草稿替换当前文件：原 xlsx 不动，后面导出的都是这次的结果
    await handleFile(new File([blob], name, { type: blob.type }), { quiet: true });
    const notes = (summary && summary.notes) || [];
    if (notes.length) toast(notes.slice(0, 3).join("；"), 9000);
    else toast(`已按描述生成流程（${name}），原文件没动`, 7000);
    if (box) box.value = "";
  } catch (err) {
    toast("生成流程失败：" + err.message, 9000);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}

/* 流程模式概要里的「关联项目」开关：与对象卡片上那枚同一款、同一套行为
 * （勾了就带决策头重新解析，预览即时生效）。
 * 绑定对象是没勾流程驱动的对象 / 没有对象时不出这个开关，避免点了没反应。 */
function bindWfProjLink(wf) {
  const box = $("#wfMeta");
  if (!box || !wf || !wf.boCode) return;
  const bo = ((state.data && state.data.objects) || []).find((o) => o.code === wf.boCode);
  if (!bo) return;
  const canToggle = !!state.file;
  const tip = canToggle
    ? "勾选后自动补「所属项目」链接字段，并把与项目对象同名/近义/相似的字段照项目定义覆盖（差异会汇总成一个对照弹窗）"
    : "示例数据请先上传 Excel，再勾「关联项目」";
  const wrap = document.createElement("label");
  wrap.className = "pl-switch wf-meta-pl";
  wrap.title = tip;
  wrap.innerHTML = `<input type="checkbox" data-pl-link="${esc(wf.boCode)}"
      ${bo.projectLink ? "checked" : ""} ${canToggle ? "" : "disabled"}><span>关联项目</span>`;
  box.appendChild(wrap);
  const el = wrap.querySelector("[data-pl-link]");
  if (el) el.onchange = () => toggleProjLink(el.dataset.plLink, el.checked, el);
}

const WfView = {
  // 选中项按**流程名**记，不按下标：重新解析后行序可能变，
  // 按下标会「选着 A 结果显示 B」。名字没了就回落到第一条。
  picked: "",

  current() {
    const list = (state.data && state.data.workflows) || [];
    if (!list.length) return null;
    return list.find((w) => w.name === this.picked) || list[0];
  },

  render() {
    renderWfImport();
    const list = (state.data && state.data.workflows) || [];
    const empty = $("#wfEmpty");
    const body = $("#wfBody");
    const btn = $("#btnWfDownload");
    // 流程对象包：和 XML 是两步导入，数据到位才允许点，避免导出一份空包
    const objs = $("#btnWfObjects");
    if (!empty || !body || !btn) return;      // 面板不在（旧 HTML）

    if (!state.data) {
      empty.hidden = false;
      body.hidden = true;
      btn.disabled = true;
      if (objs) objs.disabled = true;
      $("#wfEmptySub").textContent =
        "点上面的「选择 Excel」（或把文件拖进那个虚线框），流程对象和节点安排会出现在这里。";
      $("#wfList").innerHTML = '<div class="wf-list-empty">还没有数据</div>';
      return;
    }
    if (!Array.isArray(state.data.workflows)) {
      empty.hidden = false;
      body.hidden = true;
      btn.disabled = true;
      if (objs) objs.disabled = true;
      $("#wfEmptySub").textContent =
        "这次解析没有返回流程数据。后端可能是旧版本，重启应用后再试。";
      $("#wfList").innerHTML = "";
      return;
    }
    if (!list.length) {
      empty.hidden = false;
      body.hidden = true;
      btn.disabled = true;
      if (objs) objs.disabled = true;
      $("#wfEmptySub").textContent =
        "这份 Excel 里没有流程驱动对象。到「1_业务对象」把「流程驱动」改成「是」，" +
        "就会自动生成一条 开始 → 部门审批 → 通过，不用填 9_流程 / 10_流程节点。";
      $("#wfList").innerHTML = '<div class="wf-list-empty">9_流程 为空</div>';
      return;
    }

    empty.hidden = true;
    body.hidden = false;
    btn.disabled = false;
    if (objs) objs.disabled = false;

    const wf = this.current();
    this.picked = wf.name;

    // ---- 左列：流程清单
    $("#wfList").innerHTML = list.map((w) => {
      const on = w.name === wf.name ? " active" : "";
      return `<button class="wf-item${on}" data-wf="${esc(w.name)}">
        <div class="wf-item-name">${esc(w.name)}${w.auto
          ? '<span class="wf-auto">默认</span>' : ""}</div>
        <div class="wf-item-sub">${esc(w.boName || w.boCode || "未绑定对象")}${
          w.boName && w.boCode ? `（${esc(w.boCode)}）` : ""}</div>
        <div class="wf-item-meta">${w.nodeCount} 节点 · ${w.edgeCount} 流转</div>
      </button>`;
    }).join("");
    $$("#wfList .wf-item").forEach((el) => {
      el.onclick = () => { this.picked = el.dataset.wf; this.render(); };
    });

    // ---- 右列：概要
    $("#wfMeta").innerHTML = [
      ["流程名称", wf.name],
      ["绑定对象", `${wf.boName || ""}${wf.boCode ? `（${wf.boCode}）` : ""}` || "—"],
      ["来源", wf.auto ? "自动生成（可在系统里改）" : "9_流程 里配置"],
      ["启用", wf.active ? "是" : "否"],
      ["规模", `${wf.nodeCount} 个节点 / ${wf.edgeCount} 条流转`],
    ].map(([k, v]) => `<div class="wf-meta-item"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("");
    // 流程模式里也放同一款「关联项目」开关：这块的「绑定对象」就是流程驱动对象，
    // 人正在看这条流程时最想勾的就是它，绕回结果页反而更远。
    // 只加一个 pill，不改 .wf-meta 的 flex 排布（flex-wrap 会自动换行）。
    bindWfProjLink(wf);

    // ---- 节点：主行「序号 + 名称 + 类型」，第二行放元信息。
    // 之前是 8 列表格，宽容器里会把卡片顶宽、把右上按钮挤出视口；
    // 行列表天然自适应宽度，节点多了也不会变形。
    const meta = (pairs) => pairs
      .filter(([, v]) => v !== null && v !== undefined && v !== "")
      .map(([k, v]) => `<span><i>${esc(k)}</i>${v}</span>`).join("");

    $("#wfNodeCount").textContent = `${wf.nodeCount} 个`;
    $("#wfNodes").innerHTML = wf.nodes.map((n) => `
      <div class="wf-row">
        <div class="wf-idx">${n.id}</div>
        <div class="wf-row-body">
          <div class="wf-row-name">
            ${esc(n.name)}
            <span class="wf-tag wf-tag-${esc(n.kind)}">${esc(n.kind)}</span>
          </div>
          <div class="wf-row-meta">${meta([
            ["处理人", n.handler ? esc(n.handler)
                     : (n.kind === "审批" ? '<span class="muted">待指定</span>' : "—")],
            ["表单", n.formKey ? `<code>${esc(n.formKey)}</code>` : "自动"],
            ["业务状态", esc(n.state || "—")],
            ["办理时限", n.days ? `${n.days} 天` : ""],
            ["可退回", n.kind === "审批" ? (n.goBack ? esc(n.goBack) : "自动") : ""],
          ])}</div>
        </div>
      </div>`).join("");

    // ---- 流转：同一套行列表，起点 → 终点当主行
    $("#wfEdgeCount").textContent = `${wf.edgeCount} 条`;
    $("#wfEdges").innerHTML = wf.edges.length
      ? wf.edges.map((e) => `
        <div class="wf-row">
          <div class="wf-idx">${e.order || 0}</div>
          <div class="wf-row-body">
            <div class="wf-row-name">
              ${esc(e.src)}<span class="wf-arrow">→</span>${esc(e.dst)}
            </div>
            <div class="wf-row-meta">${meta([
              ["流转", esc(e.name || "—")],
              ["条件", esc(e.condition || "无条件")],
            ])}</div>
          </div>
        </div>`).join("")
      : '<div class="wf-rows-empty">没有流转</div>';
  },
};

window.Workflow = {
  render() { WfView.render(); },
  // renderAll 里也用这个名字，语义更清楚：数据变了，重画
  refresh() { WfView.render(); },
};

$("#btnWfDownload") && ($("#btnWfDownload").onclick = () => downloadWorkflow($("#btnWfDownload")));
$("#btnWfObjects") && ($("#btnWfObjects").onclick = () => downloadWorkflowObjects($("#btnWfObjects")));
bindWfImport();
$("#wfAiSend") && ($("#wfAiSend").onclick = aiMakeWorkflow);
$("#wfAiText") && ($("#wfAiText").onkeydown = (e) => {
  // Enter 发送、Shift+Enter 换行，和普通模式的对话区一致
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); aiMakeWorkflow(); }
});
