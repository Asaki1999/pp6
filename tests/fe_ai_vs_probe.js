/* QA D4 前端探针：真跑 app.js 的 renderAiVsPop()（值来源面板）。
 * 只读产品代码，不引第三方依赖；DOM 用最小 stub。
 * 用法：node /tmp/qa_fe_probe.js pp6kit/static/app.js
 * 输出：一行 JSON 到 stdout，由 QA 脚本断言。
 */
const fs = require("fs");
const vm = require("vm");

const FILE = process.argv[2];
const src = fs.readFileSync(FILE, "utf8");

function makeEl(sel) {
  return {
    _sel: sel,
    _handlers: {},
    innerHTML: "",
    textContent: "",
    hidden: false,
    className: "",
    checked: false,
    disabled: false,
    value: "",
    offsetWidth: 360,
    offsetHeight: 320,
    dataset: {},
    style: {},
    onclick: null,
    onchange: null,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    appendChild() {},
    querySelector: (s) => makeEl("nested:" + s),
    querySelectorAll: () => [],
    addEventListener(type, fn) { (this._handlers[type] = this._handlers[type] || []).push(fn); },
    removeEventListener() {},
    setAttribute(k, v) { this[k] = v; },
    getAttribute(k) { return this[k] ?? null; },
    focus() {}, blur() {}, click() {}, remove() {},
    insertAdjacentHTML() {},
    closest: () => null,
    contains: () => false,
    getBoundingClientRect: () => ({ top: 0, left: 0, width: 0, height: 0, bottom: 0, right: 0 }),
  };
}

const els = new Map();
function q(sel) {
  if (!els.has(sel)) els.set(sel, makeEl(sel));
  return els.get(sel);
}

const sandbox = {
  console,
  setTimeout: () => 0,
  clearTimeout: () => {},
  Promise, JSON, Math, Date, Set, Map, Object, Array, String, Number, Boolean,
  Error, RegExp, encodeURIComponent, decodeURIComponent, escape, unescape,
  fetch: () => Promise.resolve({ json: async () => ({ fields: [] }) }),
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
// 真实浏览器里 #aiVsPop 初次并不存在：只有这两个 id 走「先 null 再创建」，
// 否则 aiVsPopEl() 会拿到预置 stub 直接 return，change 处理器永远绑不上。
const lazyIds = new Set(["aiVsPop"]);
const bodyEl = makeEl("body");
bodyEl.appendChild = (el) => { if (el && el.id) els.set("#" + el.id, el); };
sandbox.document = {
  querySelector: q,
  querySelectorAll: () => [],
  getElementById: (id) => {
    if (els.has("#" + id)) return els.get("#" + id);
    if (lazyIds.has(id)) return null;
    return q("#" + id);
  },
  addEventListener: () => {},
  createElement: (tag) => makeEl("created:" + tag),
  body: bodyEl,
};
sandbox.window.addEventListener = () => {};
sandbox.localStorage = {
  _m: {},
  getItem(k) { return this._m[k] ?? null; },
  setItem(k, v) { this._m[k] = String(v); },
  removeItem(k) { delete this._m[k]; },
};

const ctx = vm.createContext(sandbox);
[
  "URLSearchParams", "URL", "Blob", "File", "FormData", "TextDecoder",
  "TextEncoder", "atob", "btoa", "Uint8Array", "Uint16Array", "Uint32Array",
  "ArrayBuffer", "DataView", "Int8Array", "Float64Array", "queueMicrotask",
  "structuredClone", "Intl", "crypto", "performance", "AbortController",
  "Headers", "Request", "Response", "setInterval", "clearInterval",
].forEach((k) => { if (k in global) sandbox[k] = global[k]; });
sandbox.location = { search: "", hash: "", href: "http://127.0.0.1/" };
sandbox.navigator = { userAgent: "node", clipboard: { writeText: async () => {} } };

vm.runInContext(src + "\n;globalThis.__state = state;"
  + "globalThis.__setIdx = (i) => { aiVsIdx = i; };"
  + "globalThis.__summary = (i) => aiVsSummary(state.aiData.bo.fields[i]);",
  ctx, { filename: FILE });

const st = ctx.__state;
const out = {};

// ---- 构造「AI 生成的对象 + 目标对象字段清单（project）」
st.aiData = {
  bo: {
    code: "qaVs", name: "值来源探针", fields: [
      { code: "suoShuXiangMu", name: "所属项目", type: "object", objectType: "project",
        // custom=true = 用户动过面板。默认态（custom 缺省）另测（D4d 那组）。
        sourceConfig: { custom: true, resultFields: ["id", "name"], linkage: [], filterField: "" } },
      { code: "xiangMuLeiXing", name: "项目类型", type: "object", objectType: "classification" },
      { code: "zhuanYe", name: "专业", type: "varchar" },
    ],
  },
};
// 字段清单里**带系统列**：面板要渲染它们、但默认不勾（后端「返回全部业务字段」
// 永远不含系统列）。清单里没这几列的话，「系统列不勾」那条断言恒真，改坏了也没人发现。
st.objFieldCache = {
  project: {
    ok: true, code: "project", fields: [
      { code: "id", name: "主键", column: "id_", type: "varchar", system: true, key: true },
      { code: "name", name: "项目名称", column: "name_", type: "varchar", system: false, key: false },
      { code: "zhuanYe", name: "专业", column: "zhuan_ye_", type: "varchar", system: false, key: false },
      { code: "createTime", name: "创建时间", column: "create_time_", type: "datetime", system: true, key: false },
      { code: "tenantId", name: "租户", column: "tenant_id_", type: "varchar", system: true, key: false },
      { code: "processNode", name: "流程节点", column: "process_node_", type: "varchar", system: true, key: false },
    ],
  },
};
// ---- 默认态（用户没动过面板）：按后端口径「返回全部业务字段」显示
ctx.__setIdx(0);
st.aiData.bo.fields[0].sourceConfig = {};
ctx.renderAiVsPop();
const htmlDefault = q("#aiVsPop").innerHTML;
const pkDefault = htmlDefault.match(/<input type="checkbox"[^>]*data-vspick="id_"[^>]*>/);
out.D4d_default_pk_checked = !!(pkDefault && /\schecked/.test(pkDefault[0]));
// 非系统列（项目名称 / 专业）默认勾上；面板必须说明这是默认口径
out.D4d_default_name_checked = /data-vspick="name_"[^>]*\schecked/.test(htmlDefault);
out.D4d_default_biz_checked = /data-vspick="zhuan_ye_"[^>]*\schecked/.test(htmlDefault);
out.D4d_default_note = htmlDefault.indexOf("全部业务字段") >= 0;
out.D4d_default_summary = ctx.__summary(0);
// 默认态下 resultFields 仍是空（后端据此走「全部字段」，不写 6_数据集）
out.D4d_default_resultFields = JSON.stringify(st.aiData.bo.fields[0].sourceConfig.resultFields || []);
// 系统列：渲染出来（用户要能看见它不返回）但不勾
out.D4d_system_rendered = htmlDefault.indexOf('data-vspick="create_time_"') >= 0
out.D4d_system_unchecked = !/data-vspick="create_time_"[^>]*\schecked/.test(htmlDefault)
out.D4d_process_unchecked = !/data-vspick="process_node_"[^>]*\schecked/.test(htmlDefault)

// 默认态下点一次「取消专业」：不能变成「只剩他点的那一列」
q("#aiVsPop").innerHTML = "";
st.aiData.bo.fields[0].sourceConfig = {};
ctx.renderAiVsPop();
const boxesAll = [
  { checked: true, disabled: true, dataset: { vspick: "id_", vscode: "id" } },
  { checked: true, disabled: false, dataset: { vspick: "name_", vscode: "name" } },
  { checked: true, disabled: false, dataset: { vspick: "zhuan_ye_", vscode: "zhuanYe" } },
];
q("#aiVsPop").querySelectorAll = (sel) => (sel === "[data-vspick]" ? boxesAll : []);
const uncheckFirst = {
  target: {
    dataset: { vspick: "zhuan_ye_", vscode: "zhuanYe" },
    checked: false,
    closest: (s) => (s === "[data-vspick]" ? { dataset: { vspick: "zhuan_ye_", vscode: "zhuanYe" }, checked: false } : null),
  },
};
(q("#aiVsPop")._handlers.change || []).forEach((fn) => fn(uncheckFirst));
out.D4d_seed_then_uncheck = JSON.stringify(st.aiData.bo.fields[0].sourceConfig.resultFields);
out.D4d_custom_after_click = st.aiData.bo.fields[0].sourceConfig.custom === true;

// ---- 事件桩：面板用 e.target.closest("[data-vsxxx]") 认控件，
// 桩元素要知道「自己带哪些 data-*」，并实现 hasAttribute（区分字段/运算符/来源）。
function evEl(attrs, extra) {
  const names = Object.keys(attrs);
  const node = Object.assign({ dataset: {}, value: "" }, extra || {});
  names.forEach((k) => {
    const camel = k.replace(/^data-/, "").replace(/-([a-z])/g, (m, c) => c.toUpperCase());
    node.dataset[camel] = attrs[k];
  });
  node.hasAttribute = (n) => names.indexOf(n) >= 0;
  node.closest = (sel) => (sel.split(",").map((s) => s.trim().replace(/^\[|\]$/g, ""))
    .some((n) => names.indexOf(n) >= 0) ? node : null);
  return node;
}

// ---- 加一条过滤规则（= 动过面板）：必须同样先铺默认全选，否则面板显示
// 「一列没勾」、摘要退回「值来源（自动）」，而产物仍是全部业务字段；
// 用户接着勾一列，产物就只剩那一列（把默认全字段悄悄缩掉）。
st.aiData.bo.fields[0].sourceConfig = {};
ctx.renderAiVsPop();
const popF = q("#aiVsPop");
popF.querySelectorAll = (sel) => (sel === "[data-vspick]" ? [
  { checked: true, disabled: true, dataset: { vspick: "id_", vscode: "id" } },
  { checked: true, disabled: false, dataset: { vspick: "name_", vscode: "name" } },
  { checked: true, disabled: false, dataset: { vspick: "zhuan_ye_", vscode: "zhuanYe" } },
  { checked: false, disabled: false, dataset: { vspick: "create_time_", vscode: "createTime" } },
] : []);
(popF._handlers.click || []).forEach((fn) => fn({ target: evEl({ "data-vsaddr": "" }) }));
const stF = st.aiData.bo.fields[0].sourceConfig;
out.D4f_rule_added = Array.isArray(stF.filterRules) && stF.filterRules.length === 1;
// 值来源固定在「表单控件」，再选本表单字段（= 按「项目类型」过滤）
(popF._handlers.change || []).forEach((fn) =>
  fn({ target: evEl({ "data-vssrc": "0" }, { value: "dynamic/formField" }) }));
(popF._handlers.change || []).forEach((fn) =>
  fn({ target: evEl({ "data-vsval": "0" }, { value: "xiangMuLeiXing" }) }));
out.D4f_filter_resultFields = JSON.stringify(stF.resultFields);
out.D4f_filter_summary = ctx.__summary(0);
out.D4f_filter_custom = stF.custom === true;
// 老字段 filterField 不再被面板改写（规则成为唯一真相）。
// 注意必须兜住 undefined：JSON.stringify(undefined) 会让这个键整个消失。
out.D4f_filter_filterField = JSON.stringify(stF.filterField ?? "");
out.D4f_filter_rule = JSON.stringify((stF.filterRules || [])[0] || null);

// 回到「动过面板」的初始态，跑原来的 D4 断言
st.aiData.bo.fields[0].sourceConfig = {
  custom: true, resultFields: ["id", "name"], linkage: [], filterField: "",
};
ctx.renderAiVsPop();
const pop = q("#aiVsPop");
const html = pop.innerHTML;
out.D4_html_len = html.length;

// ① 主键那一行：checkbox 必须 disabled + checked（面板据此置灰必选）
const pkLine = html.match(/<input type="checkbox"[^>]*data-vspick="id_"[^>]*>/);
out.D4_pk_tag = pkLine ? pkLine[0] : null;
out.D4_pk_disabled = !!(pkLine && /\sdisabled/.test(pkLine[0]));
out.D4_pk_checked = !!(pkLine && /\schecked/.test(pkLine[0]));
out.D4_pk_locked = !!(pkLine && /data-locked="1"/.test(pkLine[0]));
// 未勾的「专业」列：不出现联动下拉
out.D4_no_link_select_before = html.indexOf('data-vslink="zhuan_ye_"') < 0;

// ② 勾上「专业」后再渲染：该行出现联动下拉，且下拉里能选到本表单控件 zhuanYe
st.aiData.bo.fields[0].sourceConfig.resultFields = ["id", "name", "zhuanYe"];
ctx.renderAiVsPop();
const html2 = q("#aiVsPop").innerHTML;
out.D4_link_select_after = html2.indexOf('data-vslink="zhuan_ye_"') >= 0;
const selFrag = (html2.match(/<select class="ai-vs-link"[^>]*data-vslink="zhuan_ye_"[^>]*>[\s\S]*?<\/select>/) || [""])[0];
out.D4_link_options = selFrag.replace(/\s+/g, " ").slice(0, 300);
out.D4_link_can_pick_zhuanYe = /value="zhuanYe"/.test(selFrag);

// ③ 过滤规则行：「值来源=表单控件」时，值下拉里要能选到本表单的关联字段（项目类型）
const keepCfg = JSON.stringify(st.aiData.bo.fields[0].sourceConfig);
st.aiData.bo.fields[0].sourceConfig.filterRules = [
  { field: "", operator: "equal", source: "dynamic", fieldSource: "formField",
    value: null, control: "", label: "" },
];
ctx.renderAiVsPop();
out.D4_filter_has_xiangMuLeiXing =
  q("#aiVsPop").innerHTML.indexOf('<option value="xiangMuLeiXing"') >= 0;
st.aiData.bo.fields[0].sourceConfig = JSON.parse(keepCfg);
ctx.renderAiVsPop();

// ④ 事件：勾掉「专业」→ linkage 里的 zhuan_ye_ 必须同步删掉
const st2 = st.aiData.bo.fields[0].sourceConfig;
st2.linkage = [{ name: "zhuan_ye_", field: "zhuanYe", label: "专业" }];
// 面板上此时勾着的 / 置灰的复选框（真实 DOM 里就是这些）
let boxes = [
  { checked: true, disabled: true, dataset: { vspick: "id_" } },
  { checked: true, disabled: false, dataset: { vspick: "name_" } },
  { checked: false, disabled: false, dataset: { vspick: "zhuan_ye_" } },
];
pop.querySelectorAll = (sel) => (sel === "[data-vspick]" ? boxes : []);
const changeHandlers = pop._handlers.change || [];
out.D4_has_change_handler = changeHandlers.length > 0;
const uncheckEvent = {
  target: {
    dataset: { vspick: "zhuan_ye_", vscode: "zhuanYe" },
    checked: false,
    closest: (s) => (s === "[data-vspick]" ? { dataset: { vspick: "zhuan_ye_", vscode: "zhuanYe" }, checked: false } : null),
  },
};
changeHandlers.forEach((fn) => fn(uncheckEvent));
out.D4_linkage_after_uncheck = JSON.stringify(st2.linkage);
out.D4_resultFields_after_uncheck = JSON.stringify(st2.resultFields);

// ⑤ 勾上「专业」→ resultFields 增、linkage 保留（只要该列还勾着）
boxes = [
  { checked: true, disabled: true, dataset: { vspick: "id_" } },
  { checked: true, disabled: false, dataset: { vspick: "name_" } },
  { checked: true, disabled: false, dataset: { vspick: "zhuan_ye_" } },
];
const checkEvent = {
  target: {
    dataset: { vspick: "zhuan_ye_", vscode: "zhuanYe" },
    checked: true,
    closest: (s) => (s === "[data-vspick]" ? { dataset: { vspick: "zhuan_ye_", vscode: "zhuanYe" }, checked: true } : null),
  },
};
changeHandlers.forEach((fn) => fn(checkEvent));
out.D4_resultFields_after_check = JSON.stringify(st2.resultFields);

// ⑥ 主键被强行点掉 → 仍然保持勾选
const pkEvent = {
  target: {
    dataset: { vspick: "id_", vscode: "id", locked: "1" },
    checked: false,
    closest: (s) => (s === "[data-vspick]" ? { dataset: { vspick: "id_", vscode: "id", locked: "1" }, checked: false } : null),
  },
};
changeHandlers.forEach((fn) => fn(pkEvent));
out.D4_resultFields_after_pk_click = JSON.stringify(st2.resultFields);

// ⑦ 过滤规则：选值来源=固定值 → 选目标列 → 填值，三步都要落到规则上。
// （2026-11-20 起面板是「规则行列表」，不再有 filterField 那个单选下拉）
st2.filterRules = [{ field: "", operator: "equal", source: "dynamic",
  fieldSource: "formField", value: null, control: "", label: "" }];
changeHandlers.forEach((fn) =>
  fn({ target: evEl({ "data-vssrc": "0" }, { value: "fixed/formField" }) }));
changeHandlers.forEach((fn) =>
  fn({ target: evEl({ "data-vsrule": "0" }, { value: "zhuan_ye_" }) }));
changeHandlers.forEach((fn) =>
  fn({ target: evEl({ "data-vsop": "0" }, { value: "not_equal" }) }));
changeHandlers.forEach((fn) =>
  fn({ target: evEl({ "data-vsval": "0" }, { value: "Y" }) }));
out.D4_rule = JSON.stringify((st2.filterRules || [])[0] || null);
out.D4_filterField = JSON.stringify(st2.filterField ?? "");
// 删掉唯一一条 → **明确不过滤**（filter_rules=[]），不能退回「按同名推导」
(pop._handlers.click || []).forEach((fn) => fn({ target: evEl({ "data-vsdel": "0" }) }));
out.D4_rules_after_delete = JSON.stringify(st2.filterRules);
out.D4_logic_locked = st2.filterLogic;

console.log(JSON.stringify(out));
