/* 前端探针：真跑 projlink.js 的「相近字段确认」弹窗（openModal / render / selectAll / collect）。
 *
 * 用法（零依赖，不引 jsdom）：
 *     node tests/fe_pl_diff_probe.js pp6kit/static/projlink.js pp6kit/static/index.html
 * 结果以一行 JSON 打到 stdout，由 Python（PL52）断言。
 *
 * 为什么要真跑：这次改的是**语义 + 交互**（相近字段默认不覆盖，勾了才覆盖），
 * grep 只能证明模板字符串在文件里，证明不了
 *   - 弹窗只列「需要确认」的行，自动覆盖 / 无关的行不许出现（否则用户以为要重选一遍）；
 *   - 分组是「语义相近 / 可能相关 / 需确认」，且每行有依据 + 差异摘要；
 *   - 勾选框**默认不勾**（默认保留我方）；
 *   - 全选 / 全不选真的改状态，冲突行永远勾不上；
 *   - collect() 交出去的是 {对象: {字段: true}}（只发勾上的）；
 *   - 没有待确认项（纯 E0 / 全无关）时**不弹**。
 */
const fs = require("fs");
const vm = require("vm");

const FILE = process.argv[2];
const HTML = process.argv[3];
if (!FILE) {
  console.error("usage: node tests/fe_pl_diff_probe.js <projlink.js> [index.html]");
  process.exit(2);
}
const src = fs.readFileSync(FILE, "utf8");

const out = {};

/* ---------------- 最小 DOM stub：只实现本组件用到的几个 API ---------------- */
function makeEl(sel) {
  return {
    _sel: sel,
    innerHTML: "",
    textContent: "",
    hidden: true,
    className: "",
    checked: false,
    disabled: false,
    dataset: {},
    _attrs: {},
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); },
      remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
    },
    setAttribute(k, v) { this._attrs[k] = v; },
    getAttribute(k) { return this._attrs[k] ?? null; },
    querySelector: () => makeEl("nested"),
    querySelectorAll: () => [],
    addEventListener() {},
    focus() {},
  };
}

const els = new Map();
/* 键就是 id 本身（不带 #）：projlink.js 里走的是 document.getElementById("plMask")，
   探针这边必须取到**同一个**对象，否则断言看的是另一个空壳元素。 */
function el(id) {
  if (!els.has(id)) els.set(id, makeEl("#" + id));
  return els.get(id);
}

const sandbox = {
  console, JSON, Math, Date, Object, Array, String, Number, Boolean, RegExp, Error,
  Set, Map,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.document = {
  getElementById: el,
  querySelector: () => makeEl("q"),
  querySelectorAll: () => [],
  body: makeEl("body"),
  addEventListener() {},
};

const ctx = vm.createContext(sandbox);
vm.runInContext(src, ctx, { filename: FILE });
const PL = ctx.ProjLink;

/* ---------------- 造一份「四种关系都有」的 projectAlign ---------------- */

/* 差异明细（勾了会变成什么） */
function diff(extra) {
  return Object.assign({
    objectCode: "contract",
    objectName: "合同",
    fieldCode: "projCategory",
    fieldName: "项目类别",
    targetCode: "classificationId",
    targetName: "项目类型",
    rule: "S4",
    updateMode: false,
    count: 3,
    changes: [
      { key: "name", label: "显示名", mine: "项目类别", project: "项目类型" },
      { key: "type", label: "类型", mine: "选项", project: "对象" },
      { key: "length", label: "长度", mine: "", project: "64" },
    ],
    options: [
      { name: "基建", mineKey: "01", mineName: "基建", projectKey: "1",
        projectName: "基建", state: "changed" },
      { name: "检修", mineKey: null, mineName: null, projectKey: "3",
        projectName: "检修", state: "only_project" },
      { name: "大修", mineKey: "04", mineName: "大修", projectKey: null,
        projectName: null, state: "only_mine" },
    ],
  }, extra || {});
}

const REL_AUTO = {
  fieldCode: "xiangMuBianHao", fieldName: "项目编号", status: "auto", rule: "E0",
  basis: "字段名完全一致", confidence: "high",
  target: { code: "code", name: "项目编号", fieldName: "code_", type: "字符串" },
  candidates: [], diff: null, action: "已自动覆盖", note: "",
};
const REL_AUTO_DIFF = {
  fieldCode: "xiangMuBianHao", fieldName: "项目编号", status: "auto", rule: "E0",
  basis: "字段名完全一致", confidence: "high",
  target: { code: "code", name: "项目编号", fieldName: "code_", type: "字符串" },
  candidates: [], diff: diff({
    fieldCode: "xiangMuBianHao", fieldName: "项目编号",
    targetCode: "code", targetName: "项目编号", rule: "E0",
  }), action: "已自动覆盖", note: "",
};
const REL_S4 = {
  fieldCode: "projCategory", fieldName: "项目类别", status: "pending", rule: "S4",
  basis: "同义词（项目类别≈项目类型，归一后都是「项目类型」）", confidence: "medium",
  target: { code: "classificationId", name: "项目类型",
    fieldName: "classification_id_", type: "对象" },
  candidates: [], diff: diff(), action: "待确认", note: "",
};
const REL_S2 = {
  fieldCode: "yuSuanJin", fieldName: "预算金", status: "pending", rule: "S2",
  basis: "名称只差 1 个字", confidence: "medium",
  target: { code: "totalBudget", name: "总预算", fieldName: "total_budget_", type: "数字" },
  candidates: [], diff: null, action: "待确认", note: "",
};
const REL_SUSPECT = {
  fieldCode: "projectName", fieldName: "项目名称", status: "pending", rule: "E0",
  basis: "字段名完全一致", confidence: "high",
  target: { code: "name", name: "项目名称", fieldName: "name_", type: "字符串" },
  candidates: [], diff: null, action: "待确认",
  note: "可疑快照：可能只是抄项目名称的文本；通用字段名，匹配可能不准",
};
const REL_AMBIG = {
  fieldCode: "projectState", fieldName: "项目状态", status: "ambiguous", rule: "E0",
  basis: "字段名完全一致", confidence: "low",
  target: { code: "stateId", name: "项目状态", fieldName: "state_id_", type: "对象" },
  candidates: [{ code: "stateOldId", name: "项目状态", fieldName: "state_old_id_",
    type: "对象" }],
  diff: null, action: "待确认（多候选）", note: "多候选（2 个同分）",
};
const REL_CONFLICT = {
  fieldCode: "projectStateB", fieldName: "项目 状态", status: "conflict", rule: "E0",
  basis: "字段名完全一致", confidence: "medium",
  target: { code: "stateId", name: "项目状态", fieldName: "state_id_", type: "对象" },
  candidates: [], diff: null, action: "保留我方",
  note: "项目字段 stateId 已被字段「项目状态」认领，已跳过",
};
const REL_UNRELATED = {
  fieldCode: "signPlace", fieldName: "签署地点", status: "unrelated", rule: "",
  basis: "", confidence: "low", target: null, candidates: [], diff: null,
  action: "按原规则生成", note: "项目对象里没有对应字段，按原规则生成",
};

const RELS = [REL_AUTO, REL_S4, REL_S2, REL_SUSPECT, REL_AMBIG, REL_CONFLICT,
  REL_UNRELATED];

function relSummary(rels) {
  const s = { total: rels.length };
  ["auto", "pending", "applied", "ambiguous", "conflict", "unrelated", "unknown"]
    .forEach((k) => { s[k] = rels.filter((r) => r.status === k).length; });
  s.needsConfirm = s.pending + s.ambiguous + s.conflict;
  return s;
}

function vmOf(rels, extra) {
  const rs = relSummary(rels);
  return Object.assign({
    status: "ok",
    snapshotSource: "target_db",
    snapshotSourceLabel: "目标库",
    snapshotGeneratedAt: "2026-11-15T09:12:00+08:00",
    project: { code: "project", name: "项目", tbl: "pp_project" },
    notice: null,
    warnings: [],
    summary: { linkedObjects: 1, exact: 1, similar: 0, skipped: 0, needsConfirm: rs.needsConfirm },
    items: [{
      objectCode: "contract", objectName: "合同", linked: true, updateMode: false,
      linkField: { action: "add", code: "projectId", fieldName: "project_id_",
        name: "所属项目" },
      exact: [], similar: [], pending: [], conflicts: [],
      skipped: [], diffs: [], diffCount: 0,
      relations: rels, relationSummary: rs, pendingCount: rs.needsConfirm,
    }],
    /* 顶层 relations 故意**不带 objectCode**：组件必须自己从 items 里补上，
       否则勾选态会串到别的对象（"对象.字段" 的 key 拼不出来） */
    relations: rels.map((r) => (Object.assign({}, r))),
    relationSummary: rs,
    pendingCount: rs.needsConfirm,
    diffs: rels.filter((r) => r.diff).map((r) => r.diff),
    diffCount: rels.filter((r) => r.diff).length,
    addedLinks: [{ objectCode: "contract", objectName: "合同", fieldName: "所属项目",
      code: "projectId" }],
    addedLinkCount: 1,
    newFields: [{ objectCode: "contract", objectName: "合同", fieldCode: "memo",
      fieldName: "合同备注" }],
    newFieldCount: 1,
  }, extra || {});
}

const mask = el("plMask");
const body = el("plModalBody");
const title = el("plModalTitle");
const box = el("plBox");
const badge = el("plBadge");

const vmAll = vmOf(RELS);

/* ① 页签：关系表 + 差异卡（没有勾选框） */
PL.render(box, vmAll);
/* 无关字段块（<details> 整块；用来断言「能看到每一个字段名」） */
function unrelatedBlock(html) {
  const at = html.indexOf('<details class="pl-rel-unrelated"');
  if (at < 0) return "";
  const end = html.indexOf("</details>", at);
  return end > at ? html.slice(at, end + 10) : html.slice(at);
}

const tab = box.innerHTML;
out.R_badge = badge.textContent;
out.R_hasTable = tab.indexOf("pl-rel-table") >= 0;
out.R_headers = ["字段", "状态", "项目字段", "依据", "处理"]
  .every((k) => tab.indexOf(">" + k + "</th>") >= 0);
out.R_summaryLine = tab.indexOf("直接对应") >= 0 && tab.indexOf("待确认") >= 0;
out.R_noCheckbox = tab.indexOf('type="checkbox"') < 0;
out.R_autoShown = tab.indexOf("已自动覆盖") >= 0;
// 无关字段：页签里是**可展开的清单**，不是一句「另有 N 个无关」
const tabUnrelated = unrelatedBlock(tab);
out.R_unrelatedToggle = tabUnrelated !== ""
  && tabUnrelated.indexOf("<summary>无关字段（1）") >= 0;
out.R_unrelatedNames = tabUnrelated.indexOf("签署地点") >= 0;
out.R_unrelatedCollapsed = tabUnrelated.indexOf(" open") < 0;

/* ② 弹窗：标题 / 摘要 / 三组 / 每行内容 / 默认不勾 */
mask.hidden = true;
PL.openModal(vmAll);
out.M_opened = mask.hidden === false;
out.M_title = title.textContent;
const html = body.innerHTML;
out.M_titleOk = html.indexOf("要按项目定义覆盖吗") >= 0
  || (title.textContent || "").indexOf("要按项目定义覆盖吗") >= 0;
out.M_summary = html.indexOf("已自动覆盖") >= 0
  && html.indexOf("需要你确认") >= 0 && html.indexOf("按原规则生成") >= 0;
out.M_groups = ["语义相近", "可能相关", "需确认"].map((g) => html.indexOf(g) >= 0);
out.M_groupKeys = ["similar", "related", "confirm"].map(
  (k) => html.indexOf('data-pl-group="' + k + '"') >= 0);
// 自动覆盖的行不许出现在弹窗里（否则用户以为要一条条重选）
out.M_hidesAuto = html.indexOf("项目编号") < 0;
// 无关字段：单独一块 `<details>`，默认收起但**能展开看到每一个字段名**
const modalUnrelated = unrelatedBlock(html);
out.M_hasUnrelatedToggle = modalUnrelated !== ""
  && modalUnrelated.indexOf("<summary>无关字段（1）") >= 0
  && modalUnrelated.indexOf("按原规则生成") >= 0;
out.M_unrelatedNames = modalUnrelated.indexOf("签署地点") >= 0;
out.M_unrelatedCollapsed = modalUnrelated.indexOf(" open") < 0;
// 每行：你的字段 / 分类 chip / 项目字段 / 依据 / 差异摘要
out.M_rowFields = html.indexOf("项目类别") >= 0 && html.indexOf("classificationId") >= 0;
out.M_chip = html.indexOf("同义词") >= 0 && html.indexOf("S4") >= 0;
out.M_basis = html.indexOf("依据：") >= 0;
out.M_diff = html.indexOf("类型 选项→对象") >= 0;
out.M_note = html.indexOf("可疑快照") >= 0;
// 多候选给下拉；冲突只说原因、不给覆盖
out.M_candSelect = html.indexOf("data-pl-cand=") >= 0
  && html.indexOf("stateOldId") >= 0;
out.M_conflictLocked = html.indexOf("pl-rel-locked") >= 0
  && html.indexOf('data-pl-pick="contract.projectStateB"') < 0;
// 默认一个都不勾
out.M_defaultUnchecked = html.indexOf("checked") < 0;
out.M_pickBoxes = (html.match(/data-pl-pick="/g) || []).length;
out.M_hasFooterHint = html.indexOf("默认保留你的定义") >= 0;

/* ③ 全选 / 全不选 / collect（冲突行永远勾不上） */
out.S_all = PL.selectAll(true);
out.S_collect = PL.collect();
out.S_none = PL.selectAll(false);
out.S_collectAfterNone = PL.collect();
out.S_allAgain = PL.selectAll(true);
out.S_collectAgain = PL.collect();
out.S_noConflict = (out.S_collectAgain.contract || {}).projectStateB === undefined;

/* ③b 多候选：在下拉里换成另一个项目字段 → collect 里带的是那个编码，不是 true
   （align 支持 true / "项目字段编码" 两种值，服务端据此决定覆盖成哪个） */
PL.selectAll(false);
out.S_candBest = PL.chooseCandidate("contract.projectState", "stateId");
out.S_candBestCollect = PL.collect();
out.S_candOther = PL.chooseCandidate("contract.projectState", "stateOldId");
out.S_candOtherCollect = PL.collect();
out.S_candBackToBest = PL.chooseCandidate("contract.projectState", "stateId");
out.S_candBackCollect = PL.collect();

/* ④ 没有待确认项（纯 E0 + 无关）→ 不弹 */
mask.hidden = true;
PL.closeModal();
const vmPure = vmOf([REL_AUTO, REL_UNRELATED], { diffs: [], diffCount: 0 });
out.Z_pending = PL.pendingCount(vmPure);
out.Z_rows = PL.confirmRows(vmPure).length;
PL.openModal(vmPure);
out.Z_notOpened = mask.hidden === true;
PL.render(box, vmPure);
out.Z_tabStillRenders = box.innerHTML.indexOf("pl-rel-table") >= 0;

/* ④b 只有直接对应（E0）有差异、没有待确认项 → 也要弹。
   用户要看到「哪些不一致」；已自动覆盖不等于不该提示。 */
const vmAuto = vmOf([REL_AUTO_DIFF, REL_UNRELATED],
  { diffs: [REL_AUTO_DIFF.diff], diffCount: 1 });
mask.hidden = true;
PL.closeModal();
out.A_direct = PL.directDiffCount(vmAuto);
PL.openModal(vmAuto);
out.A_opened = mask.hidden === false;
const autoHtml = body.innerHTML;
out.A_autoGroup = autoHtml.indexOf("直接对应（已自动覆盖）") >= 0;
out.A_autoField = autoHtml.indexOf("项目编号") >= 0;
out.A_autoNoPick = autoHtml.indexOf('data-pl-pick="contract.xiangMuBianHao"') < 0;
out.A_title = el("plModalTitle").textContent;
out.A_primaryDisabled = el("plApplyPicked").disabled === true;

/* ⑤ 弹窗底部按钮：全选 / 全不选 / 保留我的 / 按勾选覆盖（HTML 里，不在 JS 里拼） */
if (HTML) {
  const page = fs.readFileSync(HTML, "utf8");
  out.B_pickAll = page.indexOf('id="plPickAll"') >= 0 && page.indexOf("全选") >= 0;
  out.B_pickNone = page.indexOf('id="plPickNone"') >= 0 && page.indexOf("全不选") >= 0;
  out.B_confirm = page.indexOf('id="plApplyPicked"') >= 0
    && page.indexOf("按勾选覆盖") >= 0;
  out.B_keepMine = page.indexOf('id="plLater"') >= 0 && page.indexOf("保留我的") >= 0;
  out.B_noRejectAll = page.indexOf('id="plRejectAll"') < 0;
  out.B_step2 = page.indexOf("确认相近字段") >= 0;
  out.B_title = page.indexOf("要按项目定义覆盖吗") >= 0;
}

console.log(JSON.stringify(out));
