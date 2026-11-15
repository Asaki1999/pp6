/* 前端探针：真跑 app.js 里的 projLinkHeader() / renderObjects() / toggleProjLink()。
 *
 * 由 tests/test_projlink.py 的 PL42–PL44 调用（`node tests/fe_projlink_probe.js
 * pp6kit/static/app.js`），结果以一行 JSON 打到 stdout 由 Python 断言。
 *
 * 为什么要真跑：只 grep 源码只能证明「字符串在文件里」，证明不了
 *   - 开关真能压住 ProjLink 的 linked（而不是被它覆盖）；
 *   - 流程对象的卡片上也有开关、勾选态真的来自 bo.projectLink；
 *   - 重新解析失败时 checkbox 与 state.plLinked 真的回到拨之前那一档。
 * 不引第三方依赖（打包机只有 openpyxl、也不能联网），DOM 用最小 stub。
 */
const fs = require("fs");
const vm = require("vm");

const FILE = process.argv[2];
const src = fs.readFileSync(FILE, "utf8");

/* ---------------- 最小 DOM stub：按选择器缓存元素，够 app.js 顶层绑定跑完 ---------------- */
function makeEl(sel) {
  return {
    _sel: sel,
    innerHTML: "",
    textContent: "",
    hidden: false,
    className: "",
    checked: false,
    disabled: false,
    dataset: {},
    style: {},
    onclick: null,
    onchange: null,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    appendChild() {},
    querySelector: (s) => makeEl("nested:" + s),
    querySelectorAll: () => [],
    addEventListener() {},
    removeEventListener() {},
    setAttribute(k, v) { this[k] = v; },
    getAttribute(k) { return this[k] ?? null; },
    focus() {}, blur() {}, click() {}, remove() {},
    insertAdjacentHTML() {},
    closest: () => null,
    contains: () => false,
    getBoundingClientRect: () => ({ top: 0, left: 0, width: 0, height: 0 }),
  };
}

const els = new Map();
function q(sel) {
  if (!els.has(sel)) els.set(sel, makeEl(sel));
  return els.get(sel);
}
const OBJ_PANEL = '.panel[data-panel="objects"]';

const sandbox = {
  console,
  setTimeout: () => 0,
  clearTimeout: () => {},
  Promise, JSON, Math, Date, Set, Map, Object, Array, String, Number, Boolean,
  Error, RegExp, encodeURIComponent, decodeURIComponent, escape, unescape,
  fetch: null,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.document = {
  querySelector: q,
  querySelectorAll: () => [],
  getElementById: (id) => q("#" + id),
  addEventListener: () => {},
  createElement: () => makeEl("created"),
  body: makeEl("body"),
};
sandbox.window.addEventListener = () => {};
sandbox.localStorage = {
  _m: {},
  getItem(k) { return this._m[k] ?? null; },
  setItem(k, v) { this._m[k] = String(v); },
  removeItem(k) { delete this._m[k]; },
};

const ctx = vm.createContext(sandbox);
// 只补宿主标准全局：白名单式（拷整个 globalThis 会踩 node 内部惰性 getter 直接 abort）
[
  "URLSearchParams", "URL", "Blob", "File", "FormData", "TextDecoder",
  "TextEncoder", "atob", "btoa", "Uint8Array", "Uint16Array", "Uint32Array",
  "ArrayBuffer", "DataView", "Int8Array", "Float64Array", "queueMicrotask",
  "structuredClone", "Intl", "crypto", "performance", "AbortController",
  "Headers", "Request", "Response", "setInterval", "clearInterval",
].forEach((k) => { if (k in global) sandbox[k] = global[k]; });
sandbox.location = { search: "", hash: "", href: "http://127.0.0.1/" };
sandbox.navigator = { userAgent: "node", clipboard: { writeText: async () => {} } };

// state 是 `const`（不在 global 上），末尾追加一行把它露出来才能构造勾选态
vm.runInContext(src + "\n;globalThis.__state = state;", ctx, { filename: FILE });

// 这些是 `function` 声明（挂在 global 上），运行时替换成桩即生效
let renderAllCalls = 0;
ctx.renderAll = () => { renderAllCalls += 1; };
let toasts = [];
ctx.toast = (msg) => { toasts.push(msg); };
let loadings = [];
ctx.setLoading = (on, text) => { loadings.push([on, text]); };

const st = ctx.__state;
const out = {};

/* ---------------- G · 决策头 ---------------- */
/* 2026-11-15 起决策头有两个来源：界面开关（linked）+ 「相近字段确认」弹窗里
 * 勾过的字段（align）。两者都会影响出包，绝不能各拼一份。
 * 这一组顺带守住「绝不回去问 ProjLink 要整份决策」（老语义：组件自己攒 align，
 * 前后端两套口径）。 */
let decisionRaw = null;
const plCalls = { decisionsHeader: 0, collect: 0, restore: 0 };
ctx.ProjLink = {
  decisionsHeader: () => { plCalls.decisionsHeader += 1; return decisionRaw; },
  collect: () => { plCalls.collect += 1; return {}; },
  restore: () => { plCalls.restore += 1; },
  reset: () => {},
  render: () => {},
  updateBadge: () => {},
  diffCount: () => 0,
  pendingCount: () => 0,
  selectAll: () => 0,
  openModal: () => {},
  closeModal: () => {},
};

// ① 开关 + 勾选同时存在：一份头里两个键都要在，false 也要显式写
st.plLinked = { contract: false };
st.plAlign = { contract: { projectState: true, memo: false } };
decisionRaw = JSON.stringify({
  version: 1,
  objects: { contract: { linked: true, align: { projectState: true } } },
});
out.G_switchWins = ctx.projLinkHeader();

// ② 都没有 → 不带头
st.plLinked = {};
st.plAlign = {};
decisionRaw = null;
out.G_emptyIsNull = ctx.projLinkHeader();

// ③ 只勾了字段、没拨开关：align 单独也要能出去（否则勾选只在预览里生效）
st.plLinked = { apply: true };
st.plAlign = { contract: { projectState: true } };
decisionRaw = JSON.stringify({ version: 1, objects: { contract: { linked: true, align: {} } } });
out.G_plusUntouched = ctx.projLinkHeader();

// ④ 多候选：align 的值可以是**项目字段编码**（用户在弹窗下拉里挑的那个），
//    必须原样发出去；不能把它压成 true（那就变成「按最匹配覆盖」了）
st.plLinked = { contract: false };
st.plAlign = { contract: { projectState: "gradeId" } };
out.G_candPick = ctx.projLinkHeader();

out.G_plCalls = Object.assign({}, plCalls);

/* ---------------- H · 对象卡片渲染 ---------------- */
function oneField() {
  return [{
    name: "项目编号", code: "xiangMuBianHao", fieldName: "xiang_mu_bian_hao_",
    type: "varchar", length: 64,
  }];
}
function twoObjects() {
  return [
    { code: "contract", name: "合同", tbl: "pp_contract", fields: oneField(),
      moduleId: "m1", flowDriven: false, projectLink: true },
    { code: "apply", name: "采购申请", tbl: "pp_apply", fields: oneField(),
      moduleId: "m2", flowDriven: true, projectLink: false },
  ];
}
// 开关元素从面板 innerHTML 里挑出来（真 DOM 里就是这些 input），顺带记下每个的附加属性
let plEls = [];
sandbox.document.querySelectorAll = (sel) => {
  if (sel.indexOf("[data-pl-link]") < 0) return [];
  const html = q(OBJ_PANEL).innerHTML;
  const hits = [];
  const re = /<input type="checkbox" data-pl-link="([^"]*)"([^>]*)>/g;
  let m;
  while ((m = re.exec(html))) {
    const el = makeEl("pl");
    el.dataset.plLink = m[1];
    out["H_attr_" + m[1]] = m[2].trim();
    hits.push(el);
  }
  plEls = hits;
  return hits;
};

st.file = null;                                  // 载入示例：没有真文件
st.data = { objects: twoObjects(), stats: {}, modules: [], issues: [] };
ctx.renderObjects();
const noFileHtml = q(OBJ_PANEL).innerHTML;
out.H_tip = noFileHtml.indexOf("这里勾选即可，不用改 Excel") >= 0;
out.H_noFileDisabled = /data-pl-link="contract"[^>]*disabled/.test(noFileHtml);
out.H_noFileTipInTitle = noFileHtml.indexOf("示例数据请先上传 Excel") >= 0;

st.file = { name: "a.xlsx", size: 10 };            // 有文件：可拨
ctx.renderObjects();
const html = q(OBJ_PANEL).innerHTML;
out.H_rows = (html.match(/data-pl-link="/g) || []).length;
out.H_flowHasSwitch = /data-pl-link="apply"/.test(html);
out.H_normalHasSwitch = /data-pl-link="contract"/.test(html);
out.H_checkedFromModel = /data-pl-link="contract"[^>]*checked/.test(html);
out.H_uncheckedWhenFalse = !/data-pl-link="apply"[^>]*checked/.test(html);
out.H_enabled = !/data-pl-link="contract"[^>]*disabled/.test(html);
out.H_chip = html.indexOf('<span class="chip accent">关联项目</span>') >= 0;
// 处理器真的挂上了（挂不上就是「点了没反应」）
out.H_handlerOk = plEls.length > 0 && plEls.every((el) => typeof el.onchange === "function");

/* ---------------- I · 拨开关后的状态 ---------------- */
async function main() {
  // ① 成功
  st.plLinked = {};
  st.plModalShown = true;
  st.buildReport = { x: 1 };
  st.inspectSummary = { y: 2 };
  st.zipPhase = { build: "done", inspect: "done" };
  st.packageMode = "actual";
  st.data = { objects: twoObjects(), stats: {}, modules: [], issues: [] };
  st.file = { name: "a.xlsx", size: 10, arrayBuffer: async () => new ArrayBuffer(8) };
  const restored = [];
  let collectCalls = 0;
  let restoreCalls = 0;
  ctx.ProjLink = {
    decisionsHeader: () => null,
    collect: () => { collectCalls += 1; return { contract: { align: { projectState: true } } }; },
    restore: (k) => { restoreCalls += 1; restored.push(k); },
    reset: () => {}, render: () => {}, updateBadge: () => {},
    diffCount: () => 0, pendingCount: () => 0, openModal: () => {}, closeModal: () => {},
  };
  const sent = [];
  ctx.fetch = async (url, opts) => {
    sent.push([url, opts && opts.headers]);
    return { json: async () => ({ ok: true, objects: twoObjects(), stats: {}, modules: [], issues: [] }) };
  };
  const input = makeEl("chk");
  input.checked = true;
  renderAllCalls = 0;
  toasts = [];
  await ctx.toggleProjLink("contract", true, input);
  out.I_ok = {
    plLinked: JSON.parse(JSON.stringify(st.plLinked)),
    plModalShown: st.plModalShown,
    buildReport: st.buildReport,
    inspectSummary: st.inspectSummary,
    zipPhase: st.zipPhase,
    renderAll: renderAllCalls,
    restored,
    collectCalls,
    restoreCalls,
    url: sent[0] && sent[0][0],
    header: sent[0] && sent[0][1] && sent[0][1]["X-Project-Link"],
    toast: toasts.slice(),
  };

  // ② 失败：之前根本没决策 → 回滚成「没这个键」
  st.plLinked = {};
  ctx.fetch = async () => ({ json: async () => ({ ok: false, error: "后端炸了" }) });
  const input2 = makeEl("chk2");
  input2.checked = true;
  toasts = [];
  await ctx.toggleProjLink("contract", true, input2);
  out.I_fail = {
    hasKey: Object.prototype.hasOwnProperty.call(st.plLinked, "contract"),
    inputChecked: input2.checked,
    inputDisabled: input2.disabled,
    toast: toasts.slice(),
  };

  // ③ 失败：之前是「否」→ 回滚成 false，不是无条件 true/false
  st.plLinked = { contract: false };
  ctx.fetch = async () => { throw new Error("网络断了"); };
  const input3 = makeEl("chk3");
  input3.checked = true;
  await ctx.toggleProjLink("contract", true, input3);
  out.I_failPrev = {
    plLinked: JSON.parse(JSON.stringify(st.plLinked)),
    inputChecked: input3.checked,
  };

  // ④ 载入示例（没有文件）：不发请求，只提示
  st.file = null;
  st.plLinked = {};
  let calls = 0;
  ctx.fetch = async () => { calls += 1; return { json: async () => ({ ok: true }) }; };
  const input4 = makeEl("chk4");
  input4.checked = true;
  toasts = [];
  await ctx.toggleProjLink("contract", true, input4);
  out.I_noFile = {
    fetchCalls: calls,
    inputChecked: input4.checked,
    plLinked: JSON.parse(JSON.stringify(st.plLinked)),
    toast: toasts.slice(),
  };

  // ⑤ 勾上后再取消：决策要写成 linked:false（不是删掉键，否则会退回 Excel 的「是」）
  st.file = { name: "a.xlsx", size: 10, arrayBuffer: async () => new ArrayBuffer(8) };
  st.plLinked = { contract: true };
  ctx.ProjLink = { decisionsHeader: () => null, collect: () => ({}), restore: () => {} };
  const sent2 = [];
  ctx.fetch = async (url, opts) => {
    sent2.push([url, opts && opts.headers]);
    return { json: async () => ({ ok: true, objects: twoObjects(), stats: {}, modules: [], issues: [] }) };
  };
  const input5 = makeEl("chk5");
  input5.checked = false;
  await ctx.toggleProjLink("contract", false, input5);
  out.I_uncheck = {
    header: sent2[0] && sent2[0][1] && sent2[0][1]["X-Project-Link"],
    plLinked: JSON.parse(JSON.stringify(st.plLinked)),
  };

  console.log(JSON.stringify(out));
}

main().catch((e) => {
  console.error("PROBE FAIL:", (e && e.stack) || e);
  process.exit(1);
});
