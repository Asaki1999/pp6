/* 前端探针：AI 生成链真的把「关联项目」决策带上了吗？
 *
 * 守的是 2026-11-21 用户报的那个 bug 的**界面侧**：开关在页面上，但
 * /api/ai/bo、/api/ai/sheet、/api/ai/bo/apply 三个请求都不带
 * X-Project-Link，服务端根本不知道用户勾了「关联」。
 *
 * 只读产品代码（app.js），不引第三方依赖；DOM 用最小 stub。
 * 用法：node tests/fe_ai_pl_probe.js pp6kit/static/app.js
 * 输出：一行 JSON 到 stdout，由 Python 用例断言。
 */
const fs = require("fs");
const vm = require("vm");

const FILE = process.argv[2];
const src = fs.readFileSync(FILE, "utf8");

function makeEl(sel) {
  return {
    _sel: sel,
    innerHTML: "", textContent: "", value: "", hidden: false, className: "",
    checked: false, disabled: false, dataset: {}, style: {},
    onclick: null, onchange: null,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    appendChild() {}, focus() {}, blur() {}, click() {}, remove() {},
    querySelector: (s) => makeEl("nested:" + s),
    querySelectorAll: () => [],
    addEventListener() {}, removeEventListener() {},
    setAttribute(k, v) { this[k] = v; },
    getAttribute(k) { return this[k] ?? null; },
    insertAdjacentHTML() {},
    closest: () => null,
    contains: () => false,
    getBoundingClientRect: () => ({ top: 0, left: 0, width: 0, height: 0,
                                    bottom: 0, right: 0 }),
  };
}

const els = new Map();
function q(sel) {
  if (!els.has(sel)) els.set(sel, makeEl(sel));
  return els.get(sel);
}

const calls = [];
const sandbox = {
  console,
  setTimeout: () => 0,
  clearTimeout: () => {},
  Promise, JSON, Math, Date, Set, Map, Object, Array, String, Number, Boolean,
  Error, RegExp, encodeURIComponent, decodeURIComponent, escape, unescape,
  // 记下每次 fetch 的 (url, options)，用来断言「请求真的带了头」
  fetch: (url, opts) => {
    calls.push({ url: String(url), opts: opts || {} });
    return Promise.resolve({
      ok: true, status: 200,
      headers: { get: () => "application/json" },
      json: async () => ({ ok: true }),
      blob: async () => ({}),
      arrayBuffer: async () => new ArrayBuffer(0),
      text: async () => "",
    });
  },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.document = {
  querySelector: q,
  querySelectorAll: () => [],
  getElementById: (id) => q("#" + id),
  addEventListener: () => {},
  createElement: (tag) => makeEl("created:" + tag),
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

vm.runInContext(
  src
  + "\n;globalThis.__state = state;"
  + "globalThis.__aiLinkHeader = aiLinkHeader;"
  + "globalThis.__aiHeaders = aiHeaders;"
  + "globalThis.__apiPost = apiPost;"
  + "globalThis.__apiPostBlob = apiPostBlob;",
  ctx, { filename: FILE });

const st = ctx.__state;
const out = {};

(async () => {
  // ---- ① 第 1 步选「不关联」：一个头都不许发（旧行为逐字不变）
  st.plPre = "off";
  out.off_header = ctx.__aiLinkHeader();
  out.off_headers = JSON.stringify(ctx.__aiHeaders());

  // ---- ② 选「关联项目」：头里 objects 为空（AI 还没起名），但不许是 null
  st.plPre = "on";
  st.plTargetKind = "project";
  st.plTarget = null;
  st.plSnapshotToken = "";
  const proj = ctx.__aiLinkHeader();
  out.proj_header = proj;
  out.proj_is_null = proj === null;
  if (proj) {
    const p = JSON.parse(decodeURIComponent(proj));
    out.proj_version = p.version;
    out.proj_objects_empty = Object.keys(p.objects || {}).length === 0;
    out.proj_has_target = Object.prototype.hasOwnProperty.call(p, "target");
    out.proj_has_snapshot = Object.prototype.hasOwnProperty.call(p, "snapshot");
  }

  // ---- ③ 选「其他业务对象 = 合同」+ 没连库（上传了对象包）：target 与 snapshot 都要带
  st.plTargetKind = "other";
  st.plTarget = { code: "contract", name: "合同", linkLabel: "合同选择" };
  st.plSnapshotToken = "0123456789abcdef0123";
  const conRaw = ctx.__aiLinkHeader();
  // 2026-11-26：target.name 是中文，header 值只允许 ISO-8859-1——
  // 必须整段 encodeURIComponent，否则浏览器抛
  // "String contains non ISO-8859-1 code point"，请求根本发不出去
  out.con_encoded = !/[^\x21-\x7e]/.test(conRaw) && conRaw.includes("%7B");
  try {
    const con = JSON.parse(decodeURIComponent(conRaw));
    out.con_target = JSON.stringify(con.target || null);
    out.con_snapshot = con.snapshot || "";
  } catch (e) {
    out.con_target = "decode-failed";
    out.con_snapshot = "";
  }

  // ---- ④ 三个入口真的把它发出去了（apiPost / apiPostBlob / fetch）
  st.plPre = "on";
  st.plTargetKind = "project";
  st.plTarget = null;
  const headers = ctx.__aiHeaders();
  await ctx.__apiPost("/api/ai/bo", { description: "x" }, headers);
  await ctx.__apiPost("/api/ai/refine", { instruction: "x" }, headers);
  await ctx.__apiPostBlob("/api/ai/bo/apply", { bo: {} }, headers);

  const byUrl = {};
  calls.forEach((c) => { byUrl[c.url] = c.opts.headers || {}; });
  out.bo_has_header = !!(byUrl["/api/ai/bo"] || {})["X-Project-Link"];
  out.refine_has_header = !!(byUrl["/api/ai/refine"] || {})["X-Project-Link"];
  out.apply_has_header = !!(byUrl["/api/ai/bo/apply"] || {})["X-Project-Link"];
  // apiPost 本来只发 Content-Type；加了额外头以后不能把 Content-Type 弄丢
  out.bo_content_type = ((byUrl["/api/ai/bo"] || {})["Content-Type"] || "");

  // ---- ⑤ 「不关联」时上面三个请求都不许带这个头
  calls.length = 0;
  st.plPre = "off";
  await ctx.__apiPost("/api/ai/bo", { description: "x" }, ctx.__aiHeaders());
  await ctx.__apiPostBlob("/api/ai/bo/apply", { bo: {} }, ctx.__aiHeaders());
  const offUrls = {};
  calls.forEach((c) => { offUrls[c.url] = c.opts.headers || {}; });
  out.off_bo_no_header = !(offUrls["/api/ai/bo"] || {})["X-Project-Link"];
  out.off_apply_no_header = !(offUrls["/api/ai/bo/apply"] || {})["X-Project-Link"];

  process.stdout.write(JSON.stringify(out) + "\n");
})().catch((err) => {
  process.stderr.write(String((err && err.stack) || err) + "\n");
  process.exit(1);
});
