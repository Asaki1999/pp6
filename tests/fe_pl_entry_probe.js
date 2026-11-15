/* 前端探针：真跑 app.js 里的 renderPlEntry() / jumpToProjectLink()。
 *
 * 用法（零依赖，不引 jsdom）：
 *     node tests/fe_pl_entry_probe.js pp6kit/static/app.js
 * 结果以一行 JSON 打到 stdout，由人/脚本断言。
 *
 * 为什么要真跑：「结果页顶部的入口条」是纯可见性修复，grep 源码只能证明字符串
 * 存在，证明不了
 *   - 三种计数（全未关联 / 部分 / 全关联）的文案真的按状态切换；
 *   - 一个对象都没有（或 state.data 还没到）时横条真的隐藏、且不抛错；
 *   - 点「去设置」真的切到「业务对象」页签、滚动到面板、并把第一个未勾的开关闪一下。
 *
 * 注意：不加进 tests/test_*.py，避免改变回归用例总数（前端只做探针，不改 Python 用例）。
 */
const fs = require("fs");
const vm = require("vm");

const FILE = process.argv[2];
if (!FILE) {
  console.error("usage: node tests/fe_pl_entry_probe.js <app.js>");
  process.exit(2);
}
const src = fs.readFileSync(FILE, "utf8");

/* ---------------- 最小 DOM stub ---------------- */
const out = {};

function mkClassList() {
  const set = new Set();
  return {
    _set: set,
    add(c) { set.add(c); },
    remove(c) { set.delete(c); },
    contains: (c) => set.has(c),
    toggle(c, force) {
      const on = force === undefined ? !set.has(c) : !!force;
      if (on) set.add(c); else set.delete(c);
      return on;
    },
  };
}

function makeEl(sel) {
  return {
    _sel: sel,
    innerHTML: "",
    textContent: "",
    title: "",
    hidden: false,
    className: "",
    checked: false,
    disabled: false,
    dataset: {},
    style: {},
    onclick: null,
    onchange: null,
    classList: mkClassList(),
    appendChild() {},
    // 顶层的 bind() 会拿 `$("#reimport").querySelector("span").onclick = ...`，
    // 所以非特化的元素也要能 QuerySelector 出一个可挂事件的桩
    querySelector: (s) => makeEl("nested:" + s),
    querySelectorAll: () => [],
    addEventListener() {},
    removeEventListener() {},
    setAttribute(k, v) { this[k] = v; },
    getAttribute(k) { return this[k] ?? null; },
    focus() {}, blur() {},
    click() { if (typeof this.onclick === "function") this.onclick(); },
    remove() {},
    insertAdjacentHTML() {},
    closest: () => null,
    contains: () => false,
    getBoundingClientRect: () => ({ top: 0, left: 0, width: 0, height: 0 }),
  };
}

/* 页签 / 面板 / 开关：jumpToProjectLink() 会碰到的就只有这几个 */
const TAB = makeEl("tab-objects");
TAB.dataset.panel = "objects";
const PANEL = makeEl("panel-objects");
PANEL.dataset.panel = "objects";
PANEL.hidden = true;
let scrollCalls = 0;
PANEL.scrollIntoView = () => { scrollCalls += 1; };

const switches = [];   // [{input, box}]  顺序 = 页面上的顺序
function mkSwitch(code, checked) {
  const box = makeEl("pl-switch");
  const input = makeEl("input");
  input.dataset.plLink = code;
  input.checked = !!checked;
  input.closest = (s) => (s === ".pl-switch" ? box : null);
  switches.push({ input, box });
  return input;
}
PANEL.querySelector = (sel) => {
  if (sel === '[data-pl-link]:not(:checked)') {
    const hit = switches.find((s) => !s.input.checked);
    return hit ? hit.input : null;
  }
  if (sel === '[data-pl-link]') return switches.length ? switches[0].input : null;
  return null;
};

const byId = new Map();
const misc = new Map();
function q(sel) {
  if (sel === '#tabs .tab[data-panel="objects"]') return TAB;
  if (sel === '.panel[data-panel="objects"]') return PANEL;
  if (typeof sel === "string" && sel.startsWith("#")) {
    const id = sel.slice(1);
    if (!byId.has(id)) {
      const el = makeEl(id);
      el.id = id;
      byId.set(id, el);
    }
    return byId.get(id);
  }
  if (!misc.has(sel)) misc.set(sel, makeEl(sel));
  return misc.get(sel);
}

const sandbox = {
  console,
  setTimeout: () => 0,               // 不让 pl-flash 被摘掉，便于断言
  clearTimeout: () => {},
  Promise, JSON, Math, Date, Set, Map, Object, Array, String, Number, Boolean,
  Error, RegExp, encodeURIComponent, decodeURIComponent, escape, unescape,
  fetch: null,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.document = {
  querySelector: q,
  // 顶层会绑页签点击（$$("#tabs .tab")）和切换显隐（$$(".panel")）
  querySelectorAll: (sel) => {
    if (sel === "#tabs .tab") return [TAB];
    if (sel === ".panel") return [PANEL];
    return [];
  },
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
[
  "URLSearchParams", "URL", "Blob", "File", "FormData", "TextDecoder",
  "TextEncoder", "atob", "btoa", "Uint8Array", "Uint16Array", "Uint32Array",
  "ArrayBuffer", "DataView", "Int8Array", "Float64Array", "queueMicrotask",
  "structuredClone", "Intl", "crypto", "performance", "AbortController",
  "Headers", "Request", "Response", "setInterval", "clearInterval",
].forEach((k) => { if (k in global) sandbox[k] = global[k]; });
sandbox.location = { search: "", hash: "", href: "http://127.0.0.1/" };
sandbox.navigator = { userAgent: "node", clipboard: { writeText: async () => {} } };

// state 是 `const`，末尾追加一行才拿得到
vm.runInContext(src + "\n;globalThis.__state = state;", ctx, { filename: FILE });
const st = ctx.__state;

const entry = q("#plEntry");
const entryText = q("#plEntryText");
const entryGo = q("#plEntryGo");

function obj(code, projectLink) {
  return {
    code, name: code, tbl: "pp_" + code, projectLink: !!projectLink,
    fields: [{ name: "n", code: "c", fieldName: "f_", type: "varchar", length: 64 }],
  };
}

/* ---------------- A · 没有对象 → 隐藏 ---------------- */
st.data = { objects: [], stats: {}, modules: [], issues: [] };
st.file = { name: "a.xlsx", size: 10 };
ctx.renderPlEntry();
out.A_emptyHidden = entry.hidden === true;

/* ---------------- B · state.data 还没到 → 隐藏且不抛错 ---------------- */
st.data = null;
let threw = false;
try { ctx.renderPlEntry(); } catch (e) { threw = true; }
out.B_nullData = { threw, hidden: entry.hidden === true };

/* ---------------- C · 一个都没关联 ---------------- */
st.data = { objects: [obj("a", false), obj("b", false), obj("c", false)], stats: {}, modules: [], issues: [] };
ctx.renderPlEntry();
out.C_allOff = {
  hidden: entry.hidden,
  text: entryText.textContent,
  btn: entryGo.textContent,
  handler: typeof entryGo.onclick === "function",
};

/* ---------------- D · 部分关联 ---------------- */
st.data = { objects: [obj("a", true), obj("b", false), obj("c", false)], stats: {}, modules: [], issues: [] };
ctx.renderPlEntry();
out.D_partial = { text: entryText.textContent, btn: entryGo.textContent };

/* ---------------- E · 全部关联 ---------------- */
st.data = { objects: [obj("a", true), obj("b", true)], stats: {}, modules: [], issues: [] };
ctx.renderPlEntry();
out.E_allOn = { text: entryText.textContent, btn: entryGo.textContent };

/* ---------------- F · 点「去设置」→ 切页签 + 滚动 + 闪第一个未勾的开关 ---------------- */
switches.length = 0;
mkSwitch("a", true);
mkSwitch("b", false);       // 第一个未勾选的
mkSwitch("c", false);
scrollCalls = 0;
TAB.classList.remove("active");
st.data = { objects: [obj("a", true), obj("b", false), obj("c", false)], stats: {}, modules: [], issues: [] };
ctx.renderPlEntry();
entryGo.onclick();
out.F_click = {
  tabActive: TAB.classList.contains("active"),
  scrolled: scrollCalls,
  flashedFirstUnchecked: switches[1].box.classList.contains("pl-flash"),
  notFlashedChecked: !switches[0].box.classList.contains("pl-flash"),
  notFlashedThird: !switches[2].box.classList.contains("pl-flash"),
};

/* ---------------- G · 全勾时点「查看」：回落到第一个开关，仍要闪 ---------------- */
switches.length = 0;
mkSwitch("a", true);
mkSwitch("b", true);
scrollCalls = 0;
st.data = { objects: [obj("a", true), obj("b", true)], stats: {}, modules: [], issues: [] };
ctx.renderPlEntry();
entryGo.onclick();
out.G_allCheckedClick = {
  btn: entryGo.textContent,
  scrolled: scrollCalls,
  flashedFallback: switches[0].box.classList.contains("pl-flash"),
};

/* ---------------- H · 面板不在（旧 HTML）→ 静默返回 ---------------- */
const origQS = sandbox.document.querySelector;
sandbox.document.querySelector = (sel) => (sel === "#plEntry" ? null : origQS(sel));
let threwH = false;
try { ctx.renderPlEntry(); } catch (e) { threwH = true; }
sandbox.document.querySelector = origQS;
out.H_noEntryEl = { threw: threwH };

console.log(JSON.stringify(out));
