/* 值来源页签前端探针：真跑 app.js 的 renderAiValues()，断言清单/开关/锁定。
 * 用法：node tests/fe_values_probe.js pp6kit/static/app.js
 * 输出一行 JSON（由调用方断言）。不引第三方依赖，DOM 用最小 stub。
 */
const fs = require("fs");
const vm = require("vm");

const FILE = process.argv[2];
const src = fs.readFileSync(FILE, "utf8");

function makeEl(sel) {
  return {
    _sel: sel, innerHTML: "", textContent: "", hidden: false, className: "",
    checked: false, disabled: false, value: "", dataset: {}, style: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    appendChild() {}, insertAdjacentHTML() {}, remove() {},
    querySelector: (s) => makeEl("nested:" + s),
    querySelectorAll: () => [],
    addEventListener() {}, removeEventListener() {},
    setAttribute(k, v) { this[k] = v; }, getAttribute(k) { return this[k] ?? null; },
    focus() {}, blur() {}, click() {}, closest: () => null, contains: () => false,
    getBoundingClientRect: () => ({ top: 0, left: 0, width: 0, height: 0 }),
  };
}
const els = new Map();
const q = (sel) => { if (!els.has(sel)) els.set(sel, makeEl(sel)); return els.get(sel); };

const sandbox = {
  console, setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0,
  clearInterval: () => {}, Promise, JSON, Math, Date, Set, Map, Object, Array,
  String, Number, Boolean, Error, RegExp, encodeURIComponent, decodeURIComponent,
  fetch: () => Promise.resolve({ ok: true, json: async () => ({ fields: [] }) }),
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.document = {
  querySelector: q, querySelectorAll: () => [], addEventListener: () => {},
  getElementById: (id) => q("#" + id), createElement: () => makeEl("created"),
  body: Object.assign(makeEl("body"), { appendChild() {} }),
};
sandbox.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
sandbox.addEventListener = () => {};          // window === sandbox
sandbox.removeEventListener = () => {};
[
  "URLSearchParams", "URL", "Blob", "File", "FormData", "TextDecoder", "TextEncoder",
  "atob", "btoa", "Uint8Array", "Uint16Array", "Uint32Array", "ArrayBuffer",
  "DataView", "Int8Array", "Float64Array", "queueMicrotask", "structuredClone",
  "Intl", "crypto", "performance", "AbortController", "Headers", "Request", "Response",
  "setInterval", "clearInterval",
].forEach((k) => { if (k in global) sandbox[k] = global[k]; });
sandbox.navigator = { userAgent: "node" };
sandbox.location = { search: "", hash: "", href: "http://127.0.0.1/" };

const ctx = vm.createContext(sandbox);
vm.runInContext(src + "\n;globalThis.__state = state;", ctx, { filename: FILE });

const st = ctx.__state;
st.aiData = {
  bo: { code: "purchase", name: "采购申请", fields: [] },
  form: null, list: null,
  valueSources: [
    { key: "purchase_apply_value", name: "采购申请值来源", target: "purchase",
      targetName: "采购申请", origin: "auto", originLabel: "自动", enabled: true,
      toggleable: false, resultAll: true, resultFields: [], filterRules: [],
      filterLogic: "AND", filterNone: true, usedBy: [] },
    { key: "project_value", name: "项目值来源", target: "project", targetName: "项目",
      origin: "filtered", originLabel: "过滤", enabled: true, toggleable: true,
      resultAll: false, resultFields: ["id_", "name_"],
      filterRules: [{ field: "classification_id_", operator: "equal", source: "dynamic", control: "classificationId" }],
      filterLogic: "AND", filterNone: false, usedBy: ["新建采购申请·所属项目"] },
  ],
};

ctx.renderAiValues();
const html = q("#aiValuesBox").innerHTML;

const autoRow = html.split('data-vsrc-row="0"')[1].split("</tr>")[0];
const filtRow = html.split('data-vsrc-row="1"')[1].split("</tr>")[0];
// 关闭态单独造一行：过滤那份关掉后应置灰 + 有 off 标记（不再有删除按钮）
const st2 = JSON.parse(JSON.stringify(st.aiData.valueSources));
st2[1].enabled = false;
st.aiData.valueSources = st2;
ctx.renderAiValues();
const htmlOff = q("#aiValuesBox").innerHTML;
const offRow = htmlOff.split('data-vsrc-row="1"')[1].split("</tr>")[0];

process.stdout.write(JSON.stringify({
  has_add_button: html.includes('id="aiVsrcAdd"'),
  no_delete_button: !html.includes("data-vsrc-del="),
  auto_locked: /data-vsrc-on="0"[^>]*disabled/.test(autoRow),
  filtered_toggleable: !/data-vsrc-on="1"[^>]*disabled/.test(filtRow),
  filtered_rule_count: filtRow.includes("1 条规则"),
  used_by_shown: filtRow.includes("所属项目"),
  off_row_marked: /class="off"/.test(offRow),
  chips: ["ai-vsrc-auto", "ai-vsrc-filtered"].every((c) => html.includes(c)),
}) + "\n");
