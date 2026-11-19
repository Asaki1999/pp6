const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/* 存量包修订是否可见（开关在 query.js 的 PP6_FEATURES）。
   隐藏期间不提「workarea 3」，免得用户照着文案找一页不存在的入口。 */
const patchModeOn = () => !window.PP6_FEATURES || window.PP6_FEATURES.patch !== false;

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
  // 表格（子表）：以前没这一条，预览里直接露出英文 table
  table: "表格",
  i18n: "国际化",
  attachment: "附件列表",
  formula: "公式",
  date: "日期",
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
  // 表格字段（子表）控件 / 嵌列表的明细表控件：两者不是一回事，
  // 以前都没有中文名，预览里会露出 tableField / dataTemplate
  tableField: "表格（子表）",
  dataTemplate: "明细表",
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
const PL_TARGET_KEY = "pp6.plTargetKind";
const PL_TARGET_NAME_KEY = "pp6.plTargetName";
const PL_PRE_MODES = ["off", "on"];
const PL_PRE_PATCH_MODES = ["off", "on", "keep"];
const PL_PRE_LABEL = { off: "不关联", on: "关联" };
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

/* 当前关联目标的显示名（项目 / 手输的业务对象名称）。 */
function plTargetLabel() {
  if (state.plTarget && state.plTarget.name) return state.plTarget.name;
  if (state.plTargetKind === "other" && state.plTargetName) return state.plTargetName;
  return "项目";
}

/* 链接字段的默认显示名：项目 = 所属项目；其它对象 = 关联合同（后端同口径）。 */
function plTargetLinkLabel() {
  if (state.plTarget && state.plTarget.linkLabel) return state.plTarget.linkLabel;
  if (state.plTargetKind === "other") return `关联${plTargetLabel()}`;
  return "所属项目";
}

/* 目标是否已经核对通过（项目永远算通过）。
 * 连库解析出来的对象 matchedBy 不是 zip，不依赖 token；上传 zip 的必须有 token。 */
function plTargetReady() {
  if (state.plTargetKind === "project") return true;
  if (!state.plTarget) return false;
  if (state.plSnapshotToken) return true;
  return !!state.plTarget.matchedBy && state.plTarget.matchedBy !== "zip";
}

const state = {
  file: null, data: null, formIndex: 0, desktop: false,
  flowMode: false,             // 流程模式 = 普通模式同一套 UI，导出时附带固定三步 XML
  entryMode: "",               // 第 1 步先选入口："ai" / "excel"；空=还在选择页
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
  // 用户上传的目标对象 zip 快照 token（没连库、或库里查不到时用它代替字段池）。
  // 每次 parse/build 都随 X-Project-Link.snapshot 发回服务端。
  plSnapshotToken: "",
  plSnapshotStatus: null,
  // 「关联到哪个业务对象」：project（默认） / other（手输名称）。
  // other 时必须先把名称核对通过（库里精确匹配或上传 zip），否则不解析 Excel。
  plTargetKind: readPref(PL_TARGET_KEY, ["project", "other"], "project"),
  plTargetName: (() => {
    try {
      return (window.localStorage && window.localStorage.getItem(PL_TARGET_NAME_KEY)) || "";
    } catch (err) { return ""; }
  })(),
  // 核对通过后的目标对象：{code,name,tbl,tableLabel,linkCode,linkField,linkLabel,...}
  plTarget: null,
  plTargetError: "",
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

/* 解析结果算不算「标准模板」：一个对象/表单/列表都没有 = 用户自己的表格。
 * 这张卡的承诺是「AI 识别字段后生成」，标准模板才直接解析；不是模板就该
 * 交给 AI，而不是解析成 0 个对象停在结果页（用户会以为「不支持 AI 分析文件」）。
 *
 * 以服务端的 looksTemplate 为准（看页签名）：空模板也是模板，要提示去
 * 1_业务对象 填一行，不能当成陌生文件丢给 AI。老服务端没有这个字段时按
 * 解析出来的内容判断。 */
function looksLikeTemplate(data) {
  if (data && typeof data.looksTemplate === "boolean") return data.looksTemplate;
  const s = (data && data.stats) || {};
  return (s["业务对象"] || 0) + (s["表单"] || 0) + (s["列表"] || 0) > 0;
}

async function handleFile(file, { fromAi = false, quiet = false } = {}) {
  if (!file) return;
  const ext = (file.name.split(".").pop() || "").toLowerCase();
  // 拖到下面「已有 Excel」区域也认 PDF / Word：转去 AI 输入口，别报“请选择 xlsx”
  if (ext === "pdf" || ext === "docx" || ext === "doc") {
    // 文件入口选 PDF/Word：切到 AI 面板并填入提取出的文字，
    // 不让用户在一个「Excel」面板里看不到结果。
    state.entryMode = "ai";
    renderEntry();
    await handleAiSource(file);
    return;
  }
  if (!/\.(xlsx|xlsm)$/i.test(file.name)) {
    toast("请选择 .xlsx 文件");
    return;
  }
  // 关联目标是「其他业务对象」时，名称没核对通过就不解析这份 Excel：
  // 否则会按默认的 project 去对齐字段，用户导入后才发现全错，代价比一次拦截大。
  if (state.plPre === "on" && !(await ensureLinkTarget())) {
    toast(state.plTargetKind === "other"
      ? "关联对象还没核对通过，文件未解析：请先在第 1 步输入名称并点「核对」"
      : "项目字段池还没准备好，文件未解析：请先导入项目 zip", 9000);
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

    // 不是标准模板（自家 Excel）→ 交给 AI 识别，别当模板解析成一页 0 个对象。
    // 只在用户主动传文件的路径上兜底：AI 回填的 Excel（fromAi）和流程页的
    // quiet 上传保持原行为。
    if (!fromAi && !quiet && !looksLikeTemplate(data)) {
      setLoading(false);
      if (!window.PP6_AI_READY) {
        toast("这不是标准 Excel 模板，需要 AI 识别；先到「设置」里填一个 Key", 9000);
        if (typeof window.PP6_OPEN_AI === "function") window.PP6_OPEN_AI();
        return;
      }
      toast(`「${file.name}」不是标准模板，已交给 AI 识别字段…`, 8000);
      await analyzeSheet(file);
      return;
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
      const all = state.data.objects || [];
      const n = all.filter((o) => o.flowDriven).length;
      if (!all.length) {
        toast("已解析，但这份 Excel 里没有业务对象：先到 1_业务对象 填一行", 7000);
      } else {
        toast(n
          ? `已解析：${n} 个流程对象，可以导出对象包`
          : `已解析：${all.length} 个对象`, 6000);
      }
    }
    if (fromAi) toast("已按预览生成，选择模块后下载 zip", 5000);
  } catch (err) {
    toast("解析失败：" + err.message);
    if (!quiet) goStep(1);
  } finally {
    setLoading(false);
  }
}

/* 空白模板：14 个页签、表头与每列说明齐全，一条数据都不预填。
   这个入口以前随「入口改为自然语言生成 / 文件生成」一起下线了，2026-11-18 加回来——
   想手填的人不该只能从「载入示例」起步（示例会直接进预览，本地拿不到文件）。
   与「载入示例」的分工：这个只下载，不解析、不进预览。 */
async function downloadBlankTemplate() {
  try {
    const res = await fetch("/api/template?blank=1");
    if (!res.ok) throw new Error(`请求失败 ${res.status}`);
    const blob = await res.blob();
    const name = decodeURIComponent(res.headers.get("X-Filename") || "PP6-填写模板-空白.xlsx");
    if (await saveBlob(blob, name)) {
      // 已打包的 App 里 Python 代码是打包那一刻的：旧后端会忽略 ?blank=1，
      // 返回带示例的模板（文件名里没有「空白」）。别在这儿骗用户说下到的是空白表。
      if (name.includes("空白")) {
        toast("已下载空白模板，填好再拖回这里：" + name, 7000);
      } else {
        toast("当前 App 后端还是旧版：这次下载的是带「采购申请」示例的模板；"
              + "空白模板要重新打包后才生效（网页版 ./bin/pp6 ui 已经是新的）", 10000);
      }
    }
  } catch (err) {
    toast("下载模板失败：" + err.message, 6000);
  }
}

async function loadDemo() {
  state.entryMode = "excel";
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

/* AI 预览的「下一步」按钮：只有选了关联才需要核对字段；
   不关联时就是生成预览，别让用户以为还要做字段确认。 */
function syncAiNextLabel() {
  const btn = $("#btnToPack");
  if (!btn) return;
  const check = state.plPre === "on";
  btn.textContent = check ? "下一步：核对字段 →" : "下一步：生成预览 →";
  btn.title = check
    ? `先核对${plTargetLabel()}字段；确认后才下载导入包`
    : "生成预览后，在结果页下载导入包";
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
  syncPlTargetUI();
}

/* 第 1 步的「关联目标」控件同步：只在选了「关联」时展开；选其他对象时
 * 露出手输名称 + 核对按钮。状态、提示、已核对 chip 全部从 state 投影。 */
function syncPlTargetUI() {
  const box = $("#plTargetBox");
  if (box) box.hidden = state.plPre !== "on";
  // 页签标题跟着目标走：关联合同时还写「项目对齐」会让人以为对错了对象
  const tabTitle = $("#plTabTitle");
  if (tabTitle) tabTitle.textContent = `${plTargetLabel()}对齐`;
  const kind = $("#plTargetKind");
  if (kind) kind.value = state.plTargetKind;
  const other = $("#plTargetOther");
  if (other) other.hidden = state.plTargetKind !== "other";
  const input = $("#plTargetName");
  if (input && input.value !== state.plTargetName) input.value = state.plTargetName;

  const info = $("#plTargetInfo");
  if (info) {
    const t = state.plTarget;
    if (state.plTargetKind === "other" && t && plTargetReady()) {
      const bits = [t.tbl, t.tableLabel].filter(Boolean);
      info.hidden = false;
      info.classList.remove("bad");
      info.textContent = `已核对：${t.name}（${t.code}${bits.length ? " · " + bits.join(" · ") : ""}）`;
    } else if (state.plTargetKind === "other" && state.plTargetError) {
      info.hidden = false;
      info.classList.add("bad");
      info.textContent = state.plTargetError;
    } else {
      info.hidden = true;
      info.textContent = "";
    }
  }

  const hint = $("#plPreHint");
  if (hint) {
    hint.textContent = state.plPre !== "on"
      ? "导入前先选：关联=这份 Excel 里的对象都关联到所选业务对象；不关联=一个都不关联。"
      : (state.plTargetKind === "project"
        ? "关联=这份 Excel 里的对象都关联项目；项目字段池优先用库里快照，没连库时上传项目 zip。"
        : `关联=这份 Excel 里的对象都关联「${plTargetLabel()}」；先输名称点「核对」，没连库时上传该对象的 zip。`);
  }
  syncAiNextLabel();
}

function plTargetReadyText(t) {
  const bits = [t.tbl, t.tableLabel].filter(Boolean);
  return `${t.name}（${t.code}${bits.length ? " · " + bits.join(" · ") : ""}）`;
}

/* 把核对通过的目标对象落进 state。token 只有上传 zip 的路径才有；
 * DB 解析出来的对象用库快照（parse/build 时服务端会再核对一次）。 */
function applyPlTarget(t, token = "") {
  state.plTarget = t || null;
  state.plTargetError = "";
  if (t && t.name) {
    state.plTargetName = t.name;
    savePref(PL_TARGET_NAME_KEY, state.plTargetName);
  }
  state.plSnapshotToken = token || "";
  state.plSnapshotStatus = null;
  syncPlTargetUI();
}

/* 核对用户在「其他业务对象」里输入的名称。
 *   连库 → GET /api/link-target 精确比对（读 iform_bo_def.name_ / 表说明）
 *   没连库 → 弹 zip 上传，POST /api/link-target 解析同名对象
 * 返回 true 表示目标可用；false 时 state.plTargetError 已写明原因。 */
async function resolvePlTarget({ silent = false } = {}) {
  if (state.plTargetKind === "project") {
    state.plTarget = null;
    state.plTargetError = "";
    state.plSnapshotToken = "";
    state.plSnapshotStatus = null;
    syncPlTargetUI();
    return true;
  }
  const name = String(state.plTargetName || "").trim();
  if (!name) {
    state.plTargetError = "请先输入业务对象名称";
    syncPlTargetUI();
    if (!silent) toast("请先输入业务对象名称，再点「核对」");
    return false;
  }
  state.plTargetError = "";
  syncPlTargetUI();
  let d = null;
  try {
    const res = await fetch(`/api/link-target?name=${encodeURIComponent(name)}`,
                            { cache: "no-store" });
    d = await res.json().catch(() => ({}));
    if (!res.ok || d.ok === false) throw new Error(d.error || `核对失败 ${res.status}`);
  } catch (err) {
    state.plTargetError = `核对失败：${err.message}`;
    syncPlTargetUI();
    if (!silent) toast(state.plTargetError, 8000);
    return false;
  }

  if (d.resolved && d.target) {
    applyPlTarget(d.target);
    if (!silent) {
      toast(`已核对「${plTargetReadyText(d.target)}」：${d.fieldCount || 0} 个字段`, 6000);
    }
    if (state.file && state.data) await reparseForTarget();
    return true;
  }

  if (!d.dbConfigured) {
    if (silent) return false;
    return await askProjectZip(d, name);
  }

  // 连了库但名称没有精确命中：按用户口径不自动改用 zip，直接把候选报出来，
  // 让用户改名或改用对象编码；绝不猜一个相似对象去对齐。
  const sugg = (d.suggestions || [])
    .map((x) => `${x.name}（${x.code}）`).join("、");
  state.plTargetError = `数据库里没有名称完全一致的「${name}」`
    + (sugg ? `；相近的有：${sugg}` : "");
  syncPlTargetUI();
  if (!silent) toast(state.plTargetError, 9000);
  return false;
}

/* 关联目标的统一入口：项目走原有的项目快照（库/随包/上传 zip），
 * 其他对象走名称核对。任何一条路径没准备好都不允许继续解析。 */
async function ensureLinkTarget() {
  if (state.plTargetKind === "project") return await ensureProjectSnapshot();
  if (plTargetReady()) return true;
  return await resolvePlTarget();
}

/* 目标对象变了而 Excel 已经解析过：带新决策头重新解析，保证预览=产物。 */
async function reparseForTarget() {
  if (!state.file || !state.data) return;
  if (!Object.keys(state.plLinked || {}).length) {
    state.plLinked = plPreDecisions(state.data.objects) || {};
  }
  const header = projLinkHeader();
  if (!header) return;
  setLoading(true, `正在按「${plTargetLabel()}」重新分析…`);
  try {
    const buf = await state.file.arrayBuffer();
    const res = await fetch("/api/parse", {
      method: "POST", body: buf,
      headers: { "Content-Type": "application/octet-stream", "X-Project-Link": header },
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "解析失败");
    state.data = data;
    state.plModalShown = false;
    resetParseArtifacts();
    renderAll();
    goStep(3);
    toast(`已按「${plTargetLabel()}」重新分析`, 5000);
  } catch (err) {
    toast(`按「${plTargetLabel()}」重新分析失败：${err.message}`, 8000);
  } finally {
    setLoading(false);
  }
}

/* 绑定「项目 / 其他业务对象」选择、名称输入与核对按钮。 */
function bindPlTarget() {
  const kind = $("#plTargetKind");
  const input = $("#plTargetName");
  const check = $("#plTargetCheck");
  if (kind) {
    kind.onchange = async () => {
      state.plTargetKind = kind.value === "other" ? "other" : "project";
      savePref(PL_TARGET_KEY, state.plTargetKind);
      state.plTarget = null;
      state.plTargetError = "";
      state.plSnapshotToken = "";
      state.plSnapshotStatus = null;
      syncPlTargetUI();
      if (state.plTargetKind === "other") {
        if (input) input.focus();
      } else if (state.file && state.data) {
        await reparseForTarget();
      }
    };
  }
  if (input) {
    input.oninput = () => {
      state.plTargetName = input.value;
      savePref(PL_TARGET_NAME_KEY, state.plTargetName);
      if (state.plTarget && state.plTarget.name !== input.value.trim()) {
        state.plTarget = null;
        state.plSnapshotToken = "";
      }
      state.plTargetError = "";
      syncPlTargetUI();
    };
    input.onkeydown = (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        resolvePlTarget();
      }
    };
  }
  if (check) check.onclick = () => resolvePlTarget();
  syncPlTargetUI();
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
      el.onchange = async () => {
        if (!el.checked) return;
        const value = g.allowed.indexOf(el.value) >= 0 ? el.value : g.fallback;
        // 选「关联」时先确认目标对象可用：项目看项目字段池，其他对象看名称核对。
        // 用户取消 → 保持原来那档，不要留下一个勾着但没法对齐的状态。
        if (value === "on" && !(await ensureLinkTarget())) {
          syncPlPreInputs();
          toast(state.plTargetKind === "other"
            ? "关联对象还没核对通过，已保持原来的设置"
            : "未导入项目包，已保持原来的关联项目设置");
          return;
        }
        if (g.patch) state.plPrePatch = value;
        else state.plPre = value;
        savePref(g.key, value);
        syncPlPreInputs();
        syncAiNextLabel();
      };
    });
  });
  syncPlPreInputs();
  syncAiNextLabel();
}

/* ------------------------------------------------ 渲染 */

function renderAll() {
  const d = state.data;
  // 流程工作区是独立菜单，但数据来自同一次解析：这里顺带刷一遍，
  // 用户上传完 Excel 直接点左侧「流程」就能看到，不用再手动刷新
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

  // 流程对象在普通模式只出普通对象，在流程模式随 zip 一起出，数字对不上时得说清楚为什么，
  // 否则用户会以为包里少导了一个对象
  const note = $("#statsNote");
  if (note) {
    const flowCount = (d.objects || []).filter((o) => o.flowDriven).length;
    note.hidden = !flowCount;
    note.textContent = flowCount
      ? (state.flowMode
        ? `其中 ${flowCount} 个流程对象会一起进 zip，并另给一份固定三步流程 XML。`
        : `其中 ${flowCount} 个是流程对象：普通模式只出普通对象，切流程模式导出时会一起出。`)
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
  // 流程模块不再作为可导出模块出现：流程对象走「流程模式」的对象包，
  // 固定三步，不生成流程 XML。
  state.picked = new Set(
    (d.modules || []).filter((m) => m.count > 0 && m.code !== "workflow")
      .map((m) => m.code)
  );
  renderModules();

  updateBar();
  // P0-1：第 7 tab 两态同渲染；predict 用 state.data，actual 用 state.buildReport。
  // 放在 updateBar() 之后，不动 updateBar / #btnPack 文案与行为。
  renderPackage(state.data, state.packageMode);
}

/* 底部按钮与提示随勾选变化 */
function updateBar() {
  const d = state.data;
  const xmlBtn = $("#btnFlowXml");
  if (!d) {
    const pack = $("#btnPack") || $("#btnBuild");
    if (pack) pack.disabled = true;
    if (xmlBtn) { xmlBtn.hidden = !state.flowMode; xmlBtn.disabled = true; }
    return;
  }
  const mods = (d.modules || []).filter((m) => m.count > 0 && m.code !== "workflow");
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
  if (xmlBtn) {
    xmlBtn.hidden = !state.flowMode;
    xmlBtn.disabled = !state.file || d.errorCount > 0 || !picked.length;
  }
}

/* 流程模式：复用普通工作区，仅切换导出时是否附带固定三步 XML。 */
window.PP6SetFlowMode = (on) => {
  state.flowMode = !!on;
  const bar = $("#flowModeBar");
  if (bar) bar.hidden = !state.flowMode;
  updateBar();
};

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
    const head = state.plTargetKind === "other"
      ? `业务对象关联「${plTargetLabel()}」` : "业务对象关联项目";
    const link = plTargetLinkLabel();
    txt.textContent = on === 0
      ? `${head}：${total} 个对象都未关联 —— 勾选后自动补「${link}」，同名同族字段按${plTargetLabel()}定义覆盖，相近字段会逐条问你`
      : off === 0
        ? `${head}：${total} 个对象已全部关联`
        : `${head}：已关联 ${on} 个，还有 ${off} 个未关联`;
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

/* ---------------- 项目字段快照：没连库时导入一份项目 zip ----------------
 * 关联项目要对齐 pp_project 的字段池。有库用库，没库时：
 *   ① 先看随包快照有没有；
 *   ② 仍然提示用户导入一份项目 zip（随包快照可能是旧版本的，字段对不上）。
 * 上传成功后 token 记在 state，parse/build 都会随 X-Project-Link.snapshot 发回。
 */
async function projectSnapshotStatus(force = false) {
  if (state.plSnapshotStatus && !force) return state.plSnapshotStatus;
  const res = await fetch("/api/project-snapshot", { cache: "no-store" });
  const d = await res.json().catch(() => ({}));
  if (!res.ok || d.ok === false) throw new Error(d.error || `读取项目快照失败 ${res.status}`);
  state.plSnapshotStatus = d;
  return d;
}

let psPending = null;

function closeProjectZipModal(ok) {
  const mask = $("#psMask");
  if (mask) mask.hidden = true;
  document.body.classList.remove("pl-modal-open");
  const done = psPending;
  psPending = null;
  if (done) done(!!ok);
}

function askProjectZip(st, targetName = "") {
  return new Promise((resolve) => {
    const mask = $("#psMask");
    if (!mask) { resolve(true); return; }   // 老页面没有这个弹窗：不拦主流程
    psPending = resolve;
    const isProject = state.plTargetKind === "project";
    const name = String(targetName || "").trim() || plTargetLabel();
    const target = isProject ? "项目" : `「${name}」`;
    const title = $("#psTitle");
    const sub = $("#psSub");
    const lead1 = $("#psLead1");
    const lead2 = $("#psLead2");
    const pick = $("#psPick");
    if (title) title.textContent = isProject ? "导入项目 zip" : `导入${target}业务对象 zip`;
    if (sub) sub.textContent = `关联${target}需要该对象的字段定义`;
    if (lead1) {
      lead1.innerHTML = (st && st.dbConfigured)
        ? `数据库里没有名称完全一致的${esc(target)}业务对象，读不到它的字段定义。`
        : `当前 App 没有配置数据库连接，读不到系统里${esc(target)}对象的字段。`;
    }
    if (lead2) {
      lead2.innerHTML = isProject
        ? `请导入一份<strong>项目 zip</strong>（包里 <code>bo/*.xml</code> 有 <code>code="project"</code> 的同步包；项目对象、项目列表/表单导出的包都可以）。导入后用它和你的 Excel 对比，找出同名与相似字段。`
        : `请导入一份<strong>${esc(target)}对象 zip</strong>（包里 <code>bo/*.xml</code> 的名称与「${esc(name)}」完全一致）。导入后用它和你的 Excel 对比，找出同名与相似字段。`;
    }
    if (pick) pick.textContent = isProject ? "选择项目 zip" : `选择${target} zip`;
    const status = $("#psStatus");
    if (status) {
      status.classList.remove("bad");
      const bits = [];
      if (st && st.sourceLabel) bits.push(`当前快照：${st.sourceLabel}`);
      if (st && st.fieldCount) bits.push(`${st.fieldCount} 个字段`);
      if (st && st.generatedAt) bits.push(String(st.generatedAt).slice(0, 10));
      status.textContent = bits.length
        ? bits.join(" · ")
        : (isProject ? "当前没有可用的项目字段快照。" : `当前没有「${name}」的字段快照。`);
    }
    mask.hidden = false;
    document.body.classList.add("pl-modal-open");
    if (pick) pick.focus();
  });
}

async function ensureProjectSnapshot() {
  if (state.plSnapshotToken) return true;
  let st = null;
  try {
    st = await projectSnapshotStatus();
  } catch (err) {
    // 状态接口不可用（老服务端 / 断网 / 探针环境）：不拦主流程，
    // 让后端按它自己的快照档位降级（随包或 unavailable）。
    return true;
  }
  // 没有 dbConfigured 字段 = 不是这个接口的响应（探针/老版本），同样不拦
  if (!st || typeof st.dbConfigured !== "boolean") return true;
  // 连了库：直接用库里的（或已经刷新过的）快照，不打扰用户
  if (st.dbConfigured) return true;
  return await askProjectZip(st);
}

function bindProjectSnapshotModal() {
  const mask = $("#psMask");
  if (!mask) return;
  const file = $("#psFile");
  if ($("#psClose")) $("#psClose").onclick = () => closeProjectZipModal(false);
  if ($("#psCancel")) $("#psCancel").onclick = () => closeProjectZipModal(false);
  if ($("#psPick")) $("#psPick").onclick = () => file && file.click();
  if (file) {
    file.onchange = async () => {
      const f = file.files && file.files[0];
      file.value = "";
      if (!f) return;
      const btn = $("#psPick");
      if (btn) { btn.disabled = true; btn.textContent = "正在读取…"; }
      try {
        const buf = await f.arrayBuffer();
        const isProject = state.plTargetKind === "project";
        const name = String(state.plTargetName || "").trim();
        const url = isProject
          ? "/api/project-snapshot"
          : `/api/link-target?name=${encodeURIComponent(name)}`;
        const res = await fetch(url, {
          method: "POST", body: buf,
          headers: { "Content-Type": "application/octet-stream" },
        });
        const d = await res.json().catch(() => ({}));
        if (!res.ok || d.ok === false) throw new Error(d.error || `读取失败 ${res.status}`);
        if (isProject) {
          state.plSnapshotToken = d.token || "";
          state.plSnapshotStatus = null;    // 下次重新问服务端
          closeProjectZipModal(true);
          toast(`已读取项目对象：${d.fieldCount || 0} 个字段，将用它对比字段`, 6000);
        } else {
          if (!d.target) throw new Error("服务端没有返回目标对象信息");
          applyPlTarget(d.target, d.token || "");
          closeProjectZipModal(true);
          toast(`已读取「${d.target.name}」：${d.fieldCount || 0} 个字段，将用它对比字段`, 6000);
          if (state.file && state.data) await reparseForTarget();
        }
      } catch (err) {
        const status = $("#psStatus");
        if (status) {
          status.textContent = "读取失败：" + err.message;
          status.classList.add("bad");
        }
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.textContent = state.plTargetKind === "project"
            ? "选择项目 zip" : `选择「${plTargetLabel()}」zip`;
        }
      }
    };
  }
  mask.addEventListener("mousedown", (e) => {
    if (e.target === mask) closeProjectZipModal(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !mask.hidden) closeProjectZipModal(false);
  });
}

/* 统一的「关联项目」决策头：界面开关（linked）+ 确认弹窗里勾的字段（align）。
 *
 * 同步包 / 流程模式 zip / 确认版 Excel 入口，以及「拨开关后重新解析」，都用它。
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
  if (!Object.keys(objects).length) return null;
  const payload = { version: 1, objects };
  // 关联「其他业务对象」：把目标名称/编码带上；服务端会重新在库里核对，
  // 不信任前端给的目标（与字段决策同一个原则）。
  if (state.plTargetKind === "other" && state.plTarget && state.plTarget.code) {
    payload.target = { name: state.plTarget.name, code: state.plTarget.code };
  }
  // 没连库时用的是用户上传的对象包快照；预览和出包都认这个 token
  if (state.plSnapshotToken) payload.snapshot = state.plSnapshotToken;
  return JSON.stringify(payload);
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
      : `已保留全部字段：没有勾选任何「按${plTargetLabel()}覆盖」`, 6000);
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
    toast(`示例数据请先上传 Excel，再在对象卡片上勾「关联${plTargetLabel()}」`);
    return;
  }
  // 没连库、也没传过目标包 → 先让用户导入一份，再继续重新解析
  if (checked && !(await ensureLinkTarget())) {
    if (input) input.checked = false;
    toast(`关联对象没准备好，「关联${plTargetLabel()}」没有打开`);
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
  setLoading(true, `正在按「关联${plTargetLabel()}」重新分析…`);
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
    toast(`对象 ${label}：关联${plTargetLabel()}=${checked ? "是" : "否"}，已重新分析`, 4000);
  } catch (err) {
    // 回滚到拨之前的那一次决策（不是无条件 true/false：之前可能压根没决策）
    if (had) state.plLinked[code] = prev;
    else delete state.plLinked[code];
    if (input) { input.checked = !checked; input.disabled = false; }
    toast(`按「关联${plTargetLabel()}」重新分析失败：` + err.message, 6000);
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

/* 流程模式导出：zip 走 /api/build（X-Object-Scope=all），流程 XML 单独走
   /api/workflow；固定 开始 → 审核 → 通过，不在这里画流程图。 */
async function downloadWorkflow(btn) {
  if (!state.file) { toast("先导入 Excel 或让 AI 生成对象"); return; }
  const label = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "正在生成…"; }
  try {
    const buf = await state.file.arrayBuffer();
    const headers = { "Content-Type": "application/octet-stream" };
    if (state.flowMode) headers["X-Workflow-All"] = "1";
    const res = await fetch("/api/workflow", {
      method: "POST", body: buf, headers,
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

/* 按模块导入 / 导出：系统里每个模块单独导入，所以左边一个开关、右边两个动作 */function renderModules() {
  const box = $("#modList");
  // 流程不再作为独立导出模块：对象包在「流程模式」里出，不生成流程 XML
  const mods = (state.data.modules || []).filter((m) => m.code !== "workflow");
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
  const tName = plTargetLabel();
  const linkLabel = plTargetLinkLabel();
  const linkTip = canToggle
    ? `勾选后自动补「${linkLabel}」链接字段，并把与${tName}对象同名/近义/相似的字段照${tName}定义覆盖（差异会汇总成一个对照弹窗）`
    : `示例数据请先上传 Excel，再在对象卡片上勾「关联${tName}」`;
  panel.innerHTML = `<div class="obj-tip">这里勾选即可，不用改 Excel；
      Excel 的关联列仍然有效（以勾选为准）。</div>`
    + objects.map((bo) => `
    <div class="card">
      <div class="card-head">
        <div>
          <h3>${esc(bo.name)} <span class="chip accent">${esc(bo.code)}</span>${bo.flowDriven ? ' <span class="chip">流程模式</span>' : ""}${bo.projectLink ? ` <span class="chip accent">关联${esc(tName)}</span>` : ""}</h3>
          <div class="sub">表名 ${esc(bo.tbl)} · ${bo.fields.length} 个字段${bo.withLog ? " · 记录变更日志" : ""}${bo.flowDriven ? " · 流程对象：流程模式导出时随 zip 一起出" : ""}${bo.projectLink ? ` · 已补「${esc(linkLabel)}」链接字段，并已对齐与${esc(tName)}同名的字段` : ""}</div>
        </div>
        <div class="obj-side">
          <label class="pl-switch" title="${esc(linkTip)}">
            <input type="checkbox" data-pl-link="${esc(bo.code)}"
                   ${bo.projectLink ? "checked" : ""} ${canToggle ? "" : "disabled"}>
            <span>关联${esc(tName)}</span>
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
        // 表格字段（子表）：把前几列点出来，列名写错一眼能看到。
        // 列的增删改在「2.1_表格列」，这里只读。
        tableColsText(f),
      ].filter(Boolean).map(esc).join(" / ") || "—"}</td>
    </tr>
  `).join("");
}

/* 子表列摘要：子表 3 列：物料名称 / 数量 / 单价 … */
function tableColsText(f) {
  const cols = Array.isArray(f.columns) ? f.columns : [];
  if (!cols.length) return "";
  const names = cols.slice(0, 3).map((c) => c.name).join(" / ");
  return `子表 ${cols.length} 列：${names}${cols.length > 3 ? " …" : ""}`;
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
        <span class="sub muted">${esc(KIND_LABEL[form.kind] || "")} · ${form.layoutStyle === "flat" ? "拖拽布局V1.0（无分组）" : "字段分组"} · ${form.layout === 1 ? "一行一列" : "一行两列"} · 标签宽 ${form.labelWidth}px · 面板 ${form.width}% · 绑定 ${esc(form.boName || form.boCode || "未绑定")} · ${form.fieldCount} 个控件</span>
      </div>
      ${(form.layoutStyle === "flat"
          ? [{ title: "", rows: form.rows || [], count: form.fieldCount }]
          : (form.groups || [{ title: "", rows: form.rows || [], count: form.fieldCount }])
        ).map((g) => `
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
  const isSource = kind === "source";
  let picked;
  if (isZip) picked = await api.pick_zip();
  else if (isSource && api.pick_source) picked = await api.pick_source();
  else picked = await api.pick_excel();
  if (!picked) return null;
  const ext = (picked.name.split(".").pop() || "").toLowerCase();
  const mime = ext === "pdf" ? "application/pdf"
    : ext === "docx" ? "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
      : ext === "zip" ? "application/zip"
        : "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
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
    // 流程模式：对象按流程模式出（全部对象），zip 里不含 XML；
    // XML 由结果页单独的「下载流程 XML」按钮给，两个文件一起交付。
    if (state.flowMode) {
      headers["X-Object-Scope"] = "all";
      headers["X-Workflow-All"] = "1";
    }
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
    if (state.flowMode) headers["X-Workflow-All"] = "1";
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
  closeAiChoicePop();                 // 整表重渲染后旧浮层的行号就失效了
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
      // 选项字段：候选值放下拉面板里看/删/加。
      // 以前是一行逗号分隔的文本，选项一多就横着铺满、想删中间一个只能
      // 靠数逗号（2026-11-16 用户要求改成下拉可查看、可删除）。
      const n = (f.choices || []).length;
      rel = `<button type="button" class="ai-choice-btn" data-ctoggle="${i}"
                     title="查看 / 删除选项值">
               <span data-ccount="${i}">${n ? `${n} 个选项` : "未设置选项"}</span>
               <span class="ai-caret">▾</span>
             </button>`;
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
        <div class="ai-edit-tip">下表可以直接改：字段名、类型、长度、关联对象/选项值。选项字段点「N 个选项」展开，可逐个删除或继续添加；「关联对象」是下拉，只能选对象库里已有的对象——手填中文名或自造编码，导入后是空下拉框。改完再下载 Excel。</div>
        <div class="tbl-wrap">
          <table class="ai-edit">
            <thead><tr><th>字段名称</th><th>编码</th><th>物理列名</th><th>类型</th><th>长度</th><th>关联 / 选项</th><th></th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
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
  // 拖拽布局 V1.0：不画分区标题，所有控件按顺序平铺
  const flat = form.layoutStyle === "flat";
  const perRow = form.layout === 1 ? 1 : AI_MAX_PER_ROW;
  const lines = [];
  let buf = [];
  const flush = () => {
    if (buf.length) { lines.push(buf); buf = []; }
  };
  form.fields.forEach((f) => {
    const group = (f.group || "").trim();
    if (!flat && group && group !== curGroup) {
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
      <label class="ai-formstyle">布局
        <select id="aiFormStyle" class="ai-input">
          <option value="grouped"${flat ? "" : " selected"}>字段分组</option>
          <option value="flat"${flat ? " selected" : ""}>拖拽布局V1.0</option>
        </select>
      </label>
    </div>
    <div class="ai-form-body">${body}</div>${ruleHtml}`;
}

/* 控件编码 → 显示标签，级联/隐藏规则里都靠它说人话 */
function labelOf(form, name) {
  const hit = (form.fields || []).find((f) => f.name === name);
  return hit ? hit.label : name;
}

const OP_LABEL = { equal: "等于", not_equal: "不等于", is_empty: "为空", is_not_empty: "不为空" };

/* 列表配置预览：视图 + 列 + 排序。显示列可自己勾选（2026-11-16 用户要求）。 */
function renderAiList(list) {
  const VIEW = { grid: "表格", board: "看板", gantt: "甘特", chart: "图表" };
  const views = list.views.map((v) => `<span class="chip accent">${VIEW[v] || v}</span>`).join(" ");
  const fields = ((state.aiData && state.aiData.bo && state.aiData.bo.fields) || []);
  const byCode = new Map(fields.map((f) => [f.code, f]));
  // 对象里删掉的字段不再显示（也不写回列表），避免出包带一个不存在的列
  const kept = (list.columns || []).filter((c) => byCode.has(c.code));
  if (kept.length !== (list.columns || []).length) list.columns = kept;
  const chosen = new Set(kept.map((c) => c.code));
  const cols = kept.map((c) => `<span class="chip">${esc((byCode.get(c.code) || {}).name || c.label)}</span>`).join(" ");
  const items = fields.map((f) =>
    `<label class="ai-col-item"><input type="checkbox" data-col="${esc(f.code)}"${chosen.has(f.code) ? " checked" : ""}>
       <span>${esc(f.name)}</span><code>${esc(f.code)}</code></label>`).join("");
  const extra = [];
  if (list.boardGroup) extra.push(`看板按「${esc(list.boardGroup)}」分列`);
  if (list.gantt) extra.push(`甘特 ${esc(list.gantt.start)} → ${esc(list.gantt.end)}`);
  if (list.sort) extra.push(`排序 ${esc(list.sort)}`);
  // 级联详情：下拉选一个显示列，点它打开详情表单（全字段只读）。
  // 选择直接写进 state.aiData.list.detailField，出包原样带回。
  const opts = ['<option value="">不开详情联动</option>']
    .concat(kept.map(
      (c) => `<option value="${esc(c.code)}"${list.detailField === c.code ? " selected" : ""}>${esc((byCode.get(c.code) || {}).name || c.label)}</option>`
    )).join("");
  return `
    <div class="ai-list-body">
      <div class="ai-kv"><span>视图</span><b>${views}</b></div>
      <div class="ai-kv"><span>显示列</span><b>${cols || '<span class="muted">还没选</span>'}</b></div>
      <div class="ai-col-head">
        <span class="muted">勾选要出现在列表里的字段（按对象字段顺序，至少 1 个）</span>
        <span class="ai-col-actions">
          <button type="button" class="link" id="aiColAll">全选</button>
          <button type="button" class="link" id="aiColNone">清空</button>
        </span>
      </div>
      <div class="ai-col-grid" id="aiListCols">${items || '<span class="muted">对象还没有字段</span>'}</div>
      <div class="ai-kv"><span>级联详情</span>
        <b><select id="aiDetailField" class="ai-input" style="max-width:220px">${opts}</select></b>
      </div>
      <div class="ai-note-line">· 开启后生成两个表单：新建表单（可编辑）+ 详情表单（全只读）；列表里点击所选列即全屏打开详情</div>
      ${extra.map((t) => `<div class="ai-note-line">· ${t}</div>`).join("")}
    </div>`;
}

/* 显示列勾选：直接改 state.aiData.list.columns（导出/出包都读它）。
 * 只重渲染列表面板，不动其它页签；至少保留 1 列，避免出包退回「全字段」。 */
function bindListColumnPicker(d) {
  const box = $("#aiListCols");
  if (!box || !state.aiData || !state.aiData.list) return;
  const list = state.aiData.list;
  const fields = ((d && d.bo && d.bo.fields) || []);
  const apply = () => {
    const panel = document.querySelector('[data-ai-panel="list"]');
    if (!panel) return;
    panel.innerHTML = renderAiList(list);
    bindListColumnPicker(d);
    bindDetailFieldPicker();
  };

  box.querySelectorAll("input[data-col]").forEach((el) => {
    el.onchange = () => {
      const code = el.dataset.col;
      const f = fields.find((x) => x.code === code);
      let cols = list.columns || [];
      if (el.checked) {
        if (!cols.some((c) => c.code === code)) {
          cols = cols.concat([{ code, label: f ? f.name : code }]);
        }
      } else {
        if (cols.length <= 1) {
          el.checked = true;
          toast("列表至少要保留 1 列");
          return;
        }
        cols = cols.filter((c) => c.code !== code);
        if (list.detailField === code) list.detailField = "";
      }
      list.columns = cols;
      apply();
    };
  });

  const all = $("#aiColAll");
  if (all) {
    all.onclick = () => {
      if (!fields.length) return;
      list.columns = fields.map((f) => ({ code: f.code, label: f.name }));
      apply();
      toast(`显示列已设为全部 ${fields.length} 个字段`);
    };
  }
  const none = $("#aiColNone");
  if (none) {
    none.onclick = () => {
      if (!fields.length) return;
      list.columns = fields.slice(0, 1).map((f) => ({ code: f.code, label: f.name }));
      if (!list.columns.some((c) => c.code === list.detailField)) list.detailField = "";
      apply();
      toast("已清空，只保留第 1 列（列表至少要有一列）", 5000);
    };
  }
}

// 表单布局样式切换：字段分组 ⇄ 拖拽布局 V1.0（写回 state，出包原样带走）
function bindAiFormStyle(d) {
  const sel = $("#aiFormStyle");
  if (!sel || !state.aiData || !state.aiData.form) return;
  sel.onchange = () => {
    state.aiData.form.layoutStyle = sel.value;
    const panel = document.querySelector('[data-ai-panel="form"]');
    if (panel) {
      panel.innerHTML = renderAiForm(state.aiData.form);
      bindAiFormStyle(d);
    }
    toast(sel.value === "flat"
      ? "已改为拖拽布局 V1.0：整张表单一个画布，不套字段分组"
      : "已改为字段分组：按分区生成 fieldset");
  };
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
  bindListColumnPicker(d);
  bindAiFormStyle(d);
  bindAiEdit();
  renderAiRelations(d);
}

/* ---------------- 选项值下拉面板 ----------------
 * 全表共用一个浮层，挂在 body 上：.tbl-wrap 有 max-height + overflow:auto，
 * 直接放在单元格里会被裁掉。点击「N 个选项」定位到按钮下方，点外面/滚动/Esc 关闭。
 */
let aiChoiceIdx = -1;

function aiChoicePopEl() {
  let pop = document.getElementById("aiChoicePop");
  if (pop) return pop;
  pop = document.createElement("div");
  pop.id = "aiChoicePop";
  pop.className = "ai-choice-pop";
  pop.hidden = true;
  pop.innerHTML = `
    <div class="ai-choice-head">选项值 <span class="muted" data-popcount></span></div>
    <div class="ai-choice-tags" data-poptags></div>
    <input class="ai-choice-add" data-popadd
           placeholder="输入选项值，回车添加（可粘贴逗号分隔的多个）">
    <div class="ai-choice-hint">点选项右边的 × 删除；改完直接下载 Excel 即可</div>`;
  document.body.appendChild(pop);

  // 删除：标签里的 ×
  pop.addEventListener("click", (e) => {
    const del = e.target.closest("[data-cdel]");
    if (!del) return;
    const f = state.aiData && state.aiData.bo && state.aiData.bo.fields[aiChoiceIdx];
    if (!f) return;
    const j = Number(del.dataset.cdel);
    f.choices.splice(j, 1);
    renderAiChoicePop();
  });

  // 添加：回车（支持逗号/换行批量粘贴）
  const add = pop.querySelector("[data-popadd]");
  add.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    const f = state.aiData && state.aiData.bo && state.aiData.bo.fields[aiChoiceIdx];
    if (!f) return;
    const vals = add.value.split(/[,，;；|\n]/).map((s) => s.trim()).filter(Boolean);
    if (!vals.length) return;
    f.choices = f.choices || [];
    vals.forEach((v) => { if (!f.choices.includes(v)) f.choices.push(v); });
    add.value = "";
    renderAiChoicePop();
  });
  return pop;
}

function renderAiChoicePop() {
  const pop = aiChoicePopEl();
  const f = state.aiData && state.aiData.bo && state.aiData.bo.fields[aiChoiceIdx];
  if (!f) return;
  f.choices = f.choices || [];
  pop.querySelector("[data-popcount]").textContent =
    f.choices.length ? `· ${f.choices.length} 个` : "· 还没有";
  pop.querySelector("[data-poptags]").innerHTML = f.choices.length
    ? f.choices.map((v, j) =>
        `<span class="ai-choice-tag">${esc(v)}<button type="button" data-cdel="${j}"
           title="删除「${esc(v)}」">×</button></span>`).join("")
    : `<span class="ai-choice-empty">还没有选项值，在下面输入后回车</span>`;
  updateAiChoiceCount(aiChoiceIdx);
}

function updateAiChoiceCount(i) {
  const f = state.aiData && state.aiData.bo && state.aiData.bo.fields[i];
  const el = document.querySelector(`[data-ccount="${i}"]`);
  if (!f || !el) return;
  el.textContent = (f.choices || []).length ? `${f.choices.length} 个选项` : "未设置选项";
}

function openAiChoicePop(i, anchor) {
  const f = state.aiData && state.aiData.bo && state.aiData.bo.fields[i];
  if (!f || !anchor) return;
  aiChoiceIdx = i;
  const pop = aiChoicePopEl();
  renderAiChoicePop();
  pop.hidden = false;

  // 定位：优先按钮正下方，下面放不下就翻到上方
  const r = anchor.getBoundingClientRect();
  const pw = pop.offsetWidth || 280;
  const ph = pop.offsetHeight || 200;
  let left = Math.min(r.left, window.innerWidth - pw - 12);
  left = Math.max(12, left);
  let top = r.bottom + 6;
  if (top + ph > window.innerHeight - 8) top = Math.max(8, r.top - ph - 6);
  pop.style.left = `${left}px`;
  pop.style.top = `${top}px`;
  const add = pop.querySelector("[data-popadd]");
  if (add) add.focus();
}

function closeAiChoicePop() {
  const pop = document.getElementById("aiChoicePop");
  if (pop) pop.hidden = true;
  aiChoiceIdx = -1;
}

// 点外面 / 滚动 / Esc 关闭；只绑一次
document.addEventListener("mousedown", (e) => {
  const pop = document.getElementById("aiChoicePop");
  if (!pop || pop.hidden) return;
  if (pop.contains(e.target) || e.target.closest("[data-ctoggle]")) return;
  closeAiChoicePop();
});
window.addEventListener("scroll", (e) => {
  const pop = document.getElementById("aiChoicePop");
  if (!pop || pop.hidden) return;
  // 面板内部（选项多时）滚动不该把面板关掉
  if (pop.contains(e.target)) return;
  closeAiChoicePop();
}, true);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeAiChoicePop();
});

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
          // 只有「关联对象」还走这个 key；选项值已经改成下拉面板，
          // 增删直接写 f.choices（见 aiChoicePopEl）
          f.objectType = el.value.trim();
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

    // 选项值：点开下拉面板查看/删除/添加
    const ct = tr.querySelector("[data-ctoggle]");
    if (ct) {
      ct.onclick = (e) => {
        e.stopPropagation();
        const pop = document.getElementById("aiChoicePop");
        if (pop && !pop.hidden && aiChoiceIdx === idx) {
          closeAiChoicePop();
          return;
        }
        openAiChoicePop(idx, ct);
      };
    }
  });
}

/* 第 1 步的文件入口：Excel 走「读表 → AI 映射」；PDF/Word 只抽文字，
 * 填进输入框让用户确认/删改后再生成。两条路都不直接出包。 */
async function handleAiSource(file) {
  const ext = (file.name.split(".").pop() || "").toLowerCase();
  if (ext === "pdf" || ext === "docx" || ext === "doc") {
    await extractDocText(file);
    return;
  }
  await analyzeSheet(file);
}

async function extractDocText(file) {
  const btn = $("#btnPickSheet") || $("#aiFromSheet");
  const old = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "读文档中…"; }
  try {
    const res = await fetch("/api/extract", {
      method: "POST",
      body: await file.arrayBuffer(),
      headers: {
        "Content-Type": "application/octet-stream",
        "X-Filename": encodeURIComponent(file.name),
      },
    });
    const d = await res.json().catch(() => ({}));
    if (!res.ok || d.ok === false) throw new Error(d.error || `读取失败 ${res.status}`);
    if (!d.text || !d.chars) {
      // 扫描版 PDF 没有文字层：别把空文本填进输入框让用户一脸茫然
      throw new Error("这份文档没有可提取的文字（扫描版 PDF 需要 OCR，或先转成 Word）");
    }
    const ta = $("#aiChatText1");
    if (ta) {
      ta.value = d.text;
      ta.focus();
      try { ta.setSelectionRange(ta.value.length, ta.value.length); } catch (_) {}
    }
    state.aiFile = null;              // 文档不是 Excel，别让旧的换表下拉串进来
    toast(`已从 ${file.name} 提取 ${d.chars} 字${d.truncated ? "（太长已截断）" : ""}，确认/修改后点「开始生成」`, 9000);
  } catch (err) {
    toast("读取文档失败：" + err.message, 8000);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = old || "选择文件"; }
  }
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
    if (btn) { btn.disabled = false; btn.textContent = "选择文件"; }
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
async function applyAiBo(d, { thenBuild = false, workflow = false } = {}) {
  const btns = ["#aiApply", "#btnExportXlsx2", "#btnToPack"]
    .map((sel) => $(sel)).filter(Boolean);
  const label0 = thenBuild
    ? (state.plPre === "on" ? "正在核对字段…" : "正在生成预览…")
    : "生成 Excel…";
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
      // 流程页的 AI 生成走 quiet：不要跳回普通模式第 3 步/抢滚动，
      // 解析完由 renderAll() 刷新流程页的计数与对象包按钮。
      await handleFile(file, workflow ? { quiet: true } : { fromAi: true });
    } else if (await saveBlob(blob, name)) {
      toast(`已导出 ${name}，线下改完拖回第 1 步即可继续`, 7000);
    }
    return true;
  } catch (err) {
    toast("生成失败：" + err.message, 6000);
    return false;
  } finally {
    btns.forEach((b) => { b.disabled = false; });
    const apply = $("#aiApply");
    if (apply) apply.textContent = "套用并下载 Excel";
    syncAiNextLabel();
  }
}

/* 第 1 步入口选择：先选 AI / Excel，再展开对应面板，避免首屏一次铺满 */
function renderEntry() {
  const choice = $("#entryChoice");
  const panels = $("#entryPanels");
  const ai = $("#entryAi");
  const excel = $("#entryExcel");
  if (!choice || !panels) return;
  const mode = state.entryMode;
  choice.hidden = !!mode;
  panels.hidden = !mode;
  if (ai) ai.hidden = mode !== "ai";
  if (excel) excel.hidden = mode !== "excel";
}

/* 步骤切换：1 输入 / 2 预览 / 3 出包 */
function goStep(n, { scroll = true } = {}) {
  const steps = { 1: "#stepInput", 2: "#stepPreview", 3: "#result" };
  Object.entries(steps).forEach(([k, sel]) => {
    const el = $(sel);
    if (el) el.hidden = Number(k) !== n;
  });
  if (n === 1) renderEntry();
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
    syncAiNextLabel();
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
      recovery: "确认选的是 .zip；重新从系统「发布 ZIP」下载（.crdownload 直接改名 .zip）"
        + (patchModeOn() ? "；或到工作区 3「存量包修订」验证能否打开。" : "。"),
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
      recovery: "去工作区 1 重新出包（空模板 / 全不选会被拦）"
        + (patchModeOn() ? "；只是想给线上对象补字段请改用工作区 3「存量包修订」。" : "。"),
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
    const visibleModules = (vm.modules || []).filter((m) => m.code !== "workflow");
    const placeholders = visibleModules.length
      ? visibleModules
      : (vm.dirs || []).filter((g) => g.kind !== "workflow")
        .map((g) => ({ folder: g.kind, label: g.label,
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
    ? `<div class="set-note">反解会拆成 Excel 重造包，原生包未建模内容（对象角色 / 日志模板 / 字典样式 / 权限）会丢${patchModeOn() ? "；补字段请改用工作区 3「存量包修订」" : ""}。</div>`
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
      const file = await pickFile("source");
      if (file) handleFile(file);
      return;
    }
    $("#fileInput").click();
  };

  // ---- 第 1 步：先选入口，再展开对应面板
  $$("#entryChoice [data-entry]").forEach((b) => {
    b.onclick = () => {
      state.entryMode = b.dataset.entry;
      renderEntry();
      const focus = state.entryMode === "ai" ? $("#aiChatText1") : $("#fileInput");
      if (focus && focus.focus) focus.focus();
    };
  });
  const entryBack = $("#entryBack");
  if (entryBack) entryBack.onclick = () => {
    state.entryMode = "";
    renderEntry();
  };

  // ---- 第 1 步：一个输入口（描述 + 拖文件都行）
  const dz = $("#dzInline");
  $("#btnPickSheet").onclick = async () => {
    if (state.desktop) {
      // 桌面壳里 <input type=file> 不可靠，走系统文件对话框（Excel / PDF / Word）
      try {
        const file = await pickFile("source");
        if (file) await handleAiSource(file);
      } catch (err) {
        toast("打开文件选择失败：" + (err && err.message ? err.message : err), 7000);
      }
      return;
    }
    $("#aiSheetInput").click();
  };
  $("#aiSheetInput").onchange = async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (file) await handleAiSource(file);
  };
  // 拖到输入区 = 走 AI 读表 / 读文档（不是模板导入）
  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => {
    const file = e.dataTransfer.files[0];
    if (file) handleAiSource(file);
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
  bindPlTarget();
  bindProjectSnapshotModal();

  $("#modAll").onclick = () => {
    state.picked = new Set((state.data.modules || []).filter((m) => m.count > 0).map((m) => m.code));
    renderModules(); updateBar();
  };
  $("#modNone").onclick = () => {
    state.picked = new Set();
    renderModules(); updateBar();
  };

  $("#btnDemo").onclick = loadDemo;
  const blankTplBtn = $("#btnBlankTpl");
  if (blankTplBtn) blankTplBtn.onclick = downloadBlankTemplate;
  const packBtn = $("#btnPack") || $("#btnBuild");
  if (packBtn) packBtn.onclick = () => buildZip();
  const xmlBtn = $("#btnFlowXml");
  if (xmlBtn) xmlBtn.onclick = () => downloadWorkflow(xmlBtn);

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
    if (file) await handleAiSource(file);
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
  renderEntry();
