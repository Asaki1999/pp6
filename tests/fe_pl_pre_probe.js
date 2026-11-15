/* 前端探针：真跑「第 1 步 · 业务对象关联项目」的**导入前两档**。
 *
 * 用法（零依赖，不引 jsdom）：
 *     node tests/fe_pl_pre_probe.js pp6kit/static/app.js pp6kit/static/index.html
 * 结果以一行 JSON 打到 stdout，由人/脚本断言。
 *
 * 为什么要真跑：这次改的是**顺序 + 决策来源**，grep 只能证明两档控件在 HTML 里，
 * 证明不了
 *   - 初始页只剩两档（不关联 / 全部关联），第三档「按 Excel」整块删干净；
 *   - 默认「不关联」也走第二次解析、发全量 linked:false（不发明示的 false，
 *     Excel 里的「是」会漏回来），且导入后不再弹第 1 步；
 *   - 「全部关联」第二次解析的头里真的包含**全部**对象且全 true；
 *   - state.plLinked 与头里那份决策**完全一致**（出包入口只认它）；
 *   - 第二次解析失败时退回第一次的结果、决策清空（不留下对不上的头）；
 *   - 第 2 步差异弹窗的自动弹出逻辑没被这条改动影响；
 *   - 存量包页三档真的预置了每个对象卡片的勾选；
 *   - localStorage 持久化 + 普通/流程两个菜单共用同一档，旧的 "excel" 自动落回 "off"。
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const FILE = process.argv[2];
const HTML_FILE = process.argv[3];
if (!FILE) {
  console.error("usage: node tests/fe_pl_pre_probe.js <app.js> [index.html]");
  process.exit(2);
}
const src = fs.readFileSync(FILE, "utf8");
const html = HTML_FILE && fs.existsSync(HTML_FILE)
  ? fs.readFileSync(HTML_FILE, "utf8") : "";
const STATIC_DIR = path.dirname(FILE);
const PATCH_FILE = path.join(STATIC_DIR, "patch.js");
const CSS_FILE = path.join(STATIC_DIR, "style.css");

const out = {};

/* ---------------- 最小 DOM stub ---------------- */
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
    type: "",
    value: "",
    dataset: {},
    style: {},
    onclick: null,
    onchange: null,
    oninput: null,
    onkeydown: null,
    classList: mkClassList(),
    appendChild() {},
    querySelector: () => makeEl("nested:" + sel),
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
    scrollIntoView() {},
    scrollTo() {},
    getBoundingClientRect: () => ({ top: 0, left: 0, width: 0, height: 0 }),
  };
}

/* 三组单选都注册成**真的** <input type="radio">：app.js 的 bindPlPre() 在加载时
 * 就会去 document.querySelectorAll('input[name="plPreImport"]') 取它们。
 * 这样探针断言的是线上那条路径（onchange → state → localStorage → 同步），
 * 而不是探针自己另写一套改 state。
 * 初始页只剩两档；存量包页仍是三档（「按包内」是独立的一档）。 */
const RADIO_GROUPS = {
  plPreImport: ["off", "on"],
  plPreWorkflow: ["off", "on"],
  plPrePatch: ["off", "on", "keep"],
};

function buildRadios() {
  const map = {};
  Object.keys(RADIO_GROUPS).forEach((name) => {
    map[`input[name="${name}"]`] = RADIO_GROUPS[name].map((value) => {
      const el = makeEl(`${name}:${value}`);
      el.type = "radio";
      el.name = name;
      el.value = value;
      el.checked = false;
      el.onchange = null;
      el.fire = () => { if (typeof el.onchange === "function") el.onchange(); };
      return el;
    });
  });
  return map;
}

/* 一个「新开一次页面」= 一个独立 context：state 在加载时按 localStorage 初始化，
 * 想测「持久化生效」就必须重新加载，改内存里的 state 是测不出来的。 */
function loadApp(prefs) {
  const byId = new Map();
  const misc = new Map();
  const radios = buildRadios();
  const net = { requests: [], queue: [], last: null };
  const counters = { render: 0, toasts: [], steps: [] };

  function q(sel) {
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

  const store = Object.assign({}, prefs || {});
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
    // 逗号分隔的选择器（app.js 用了一条合并的 'input[name="plPreImport"], …'）
    // 在真实 DOM 里是「并集」，stub 也要照做，否则探针看不到两个菜单的同步
    querySelectorAll: (sel) => String(sel)
      .split(",")
      .reduce((acc, part) => acc.concat(radios[part.trim()] || []), []),
    // 真实浏览器里查不到的 id 返回 null；这里不自动造，才能断言 #plSelect 真没了
    getElementById: (id) => byId.get(id) || null,
    addEventListener: () => {},
    createElement: () => makeEl("created"),
    body: makeEl("body"),
  };
  sandbox.window.addEventListener = () => {};
  sandbox.scrollTo = () => {};
  sandbox.localStorage = {
    _m: store,
    getItem(k) { return Object.prototype.hasOwnProperty.call(this._m, k) ? this._m[k] : null; },
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

  sandbox.fetch = async (url, init) => {
    net.requests.push({
      url: String(url),
      headers: (init && init.headers) || {},
      body: init && init.body,
    });
    const item = net.queue.length ? net.queue.shift() : net.last;
    const payload = item && item.__error ? { ok: false, error: item.__error } : item;
    return {
      ok: true,
      status: 200,
      json: async () => payload,
      blob: async () => null,
      headers: { get: () => null },
      text: async () => "",
    };
  };

  vm.runInContext(src + "\n;globalThis.__state = state;", ctx, { filename: FILE });

  // 只关心「有没有渲染 / 走到第几步」：整页渲染依赖的 DOM 太多，桩一遍没意义
  ctx.renderAll = () => { counters.render += 1; sandbox.__importHasResult = true; };
  const realGoStep = ctx.goStep;
  ctx.goStep = (n, opt) => {
    counters.steps.push(n);
    return realGoStep(n, opt);
  };
  ctx.toast = (msg) => { counters.toasts.push(String(msg)); };

  return { ctx, sandbox, st: ctx.__state, byId, q, radios, net, counters };
}

/* 点一下某个单选：真实浏览器只对**新选中**的那个触发 change */
function pickRadio(page, name, value) {
  const list = page.radios[`input[name="${name}"]`] || [];
  const hit = list.find((el) => el.value === value);
  if (!hit) throw new Error(`没有 ${name}=${value} 这个单选`);
  list.forEach((el) => { el.checked = el === hit; });
  hit.fire();
  return hit;
}

function radioState(page, name) {
  const list = page.radios[`input[name="${name}"]`] || [];
  return list.filter((el) => el.checked).map((el) => el.value);
}

function mkFile(name, size) {
  return {
    name: name || "pre.xlsx",
    size: size || 2048,
    type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    arrayBuffer: async () => new ArrayBuffer(8),
  };
}

function mkPayload(objects, flowMarker) {
  return {
    ok: true,
    objects,
    stats: {
      "业务对象": objects.length, "字段": objects.length * 3,
      "表单": 0, "表单控件": 0, "列表": 0, "数据集": 0,
      // 用来分辨「渲染的是第一次还是第二次解析的结果」：
      // 第二次的 payload 带 marker，fallback 断言看它是不是 0
      "流程": flowMarker || 0,
    },
    modules: [], forms: [], lists: [], datasets: [], issues: [],
    fieldChecks: [], errorCount: 0, warnCount: 0,
    relationships: { items: [] },
    projectAlign: { items: [], summary: {}, status: "ok" },
  };
}

function obj(code, projectLink, flowDriven) {
  return {
    code, name: code + "对象", tbl: "pp_" + code,
    projectLink: !!projectLink, flowDriven: !!flowDriven,
    fields: [
      { name: "n", code: "c1", fieldName: "f_1", type: "varchar", length: 64 },
      { name: "n", code: "c2", fieldName: "f_2", type: "varchar", length: 64 },
    ],
  };
}

const OBJS = [obj("a", false), obj("b", true), obj("c", false)];

function decodeHeader(rec) {
  const raw = (rec && rec.headers && rec.headers["X-Project-Link"]) || "";
  if (!raw) return null;
  try { return JSON.parse(raw); } catch (e) { return { __bad: raw }; }
}

function linkedOf(decoded) {
  if (!decoded || !decoded.objects) return null;
  const map = {};
  Object.keys(decoded.objects).forEach((k) => { map[k] = decoded.objects[k].linked; });
  return map;
}

/* ---------------- ① 静态：初始页两档控件在，第三档和旧弹窗都不在 ---------------- */
function staticChecks() {
  const css = fs.existsSync(CSS_FILE) ? fs.readFileSync(CSS_FILE, "utf8") : "";
  out["0_static"] = {
    hasImportBar: /id="plPreImportBar"/.test(html),
    hasWorkflowBar: /id="plPreWorkflowBar"/.test(html),
    hasPatchBar: /id="plPrePatchBar"/.test(html),
    importValues: (html.match(/name="plPreImport" value="([a-z]+)"/g) || []).map((s) => s.split('"')[3]),
    workflowValues: (html.match(/name="plPreWorkflow" value="([a-z]+)"/g) || []).map((s) => s.split('"')[3]),
    patchValues: (html.match(/name="plPrePatch" value="([a-z]+)"/g) || []).map((s) => s.split('"')[3]),
    // 默认勾的是「不关联」
    importDefaultOff: /name="plPreImport" value="off" checked/.test(html),
    workflowDefaultOff: /name="plPreWorkflow" value="off" checked/.test(html),
    patchDefaultKeep: /name="plPrePatch" value="keep" checked/.test(html),
    step1Numbering: /第 1 步 · 业务对象关联项目/.test(html),
    // 「全部关联」是新文案，旧的「关联」（两字）不该再单独出现
    saysAllLinked: /全部关联/.test(html),
    // 第三档「按 Excel」整块删干净：值、文案、说明都不留
    noExcelValue: !/value="excel"/.test(html),
    noExcelLabel: !/按 Excel/.test(html),
    // 说明是大白话：两档各是什么后果 + 导入后不再弹
    hintExplainsGears: /全部关联=这份 Excel 里每个对象都关联项目/.test(html)
      && /不关联=一个都不关联/.test(html)
      && /导入后不再弹选择框/.test(html),
    // 旧的第 1 步弹窗必须整块消失（DOM / 按钮 / 样式都不留死代码）
    noSelectModal: !/id="plSelect"/.test(html) && !/plSelectGo/.test(html),
    noSelectCss: !/\.pl-select-row|#plSelect\[hidden\]|pl-select-open/.test(css),
    hasPreCss: /\.pl-pre\s*\{/.test(css),
    // 出包后回第 1 步改 Excel：同一个文件再选一次必须能触发 change
    reimportResetsInput: /fileInput"\)\.onchange[\s\S]{0,300}e\.target\.value = ""/.test(src),
    backResetsAndReopens: /function backToInput\(\)[\s\S]{0,700}plModalShown = false/.test(src),
    hasBackEdit: /id="plBackEdit"/.test(html) && /plBackEdit/.test(src),
  };
}

/* ---------------- ② 普通模式：默认「不关联」 ---------------- */
async function scenarioDefault() {
  const page = loadApp();
  const { st, net, counters, byId, ctx } = page;
  const before = {
    plPre: st.plPre,
    plPrePatch: st.plPrePatch,
    importRadios: radioState(page, "plPreImport"),
    workflowRadios: radioState(page, "plPreWorkflow"),
    patchRadios: radioState(page, "plPrePatch"),
  };

  // 第一次解析不带决策头（只为拿对象清单），第二次带全量决策
  net.queue = [mkPayload(OBJS, 0), mkPayload(OBJS, 7)];
  counters.render = 0;
  counters.toasts = [];
  counters.steps = [];
  net.requests = [];
  await ctx.handleFile(mkFile("default.xlsx"));
  await new Promise((r) => setImmediate(r));

  const second = decodeHeader(net.requests[1]);
  out["1_default"] = {
    before,
    requestCount: net.requests.length,
    // 默认档也要发头：只是全量 false
    firstHasHeader: !!decodeHeader(net.requests[0]),
    urls: net.requests.map((r) => r.url),
    version: second && second.version,
    headerCodes: second ? Object.keys(second.objects || {}).sort() : [],
    headerLinked: linkedOf(second),
    plLinked: Object.assign({}, st.plLinked),
    // 渲染的是第二次解析的结果（marker=7）
    dataFlowMarker: st.data ? st.data.stats["流程"] : null,
    rendered: counters.render,
    steps: counters.steps.slice(),
    inputHidden: byId.get("stepInput") ? byId.get("stepInput").hidden : null,
    resultHidden: byId.get("result") ? byId.get("result").hidden : null,
    // 旧弹窗连节点都没被创建（没有代码再去碰它）
    selectEl: page.sandbox.document.getElementById("plSelect"),
    // 导入后不再弹第 1 步（弹窗只在第 2 步差异时有，且默认 false 时不会自动开）
    plModalShown: st.plModalShown,
    toasts: counters.toasts.slice(),
  };
}

/* ---------------- ③ 「全部关联」：第二次解析，头里必须全量 true ---------------- */
async function scenarioOn() {
  const page = loadApp();
  const { st, net, counters, byId, ctx, sandbox } = page;

  // 从普通模式那一档点开，流程模式的同名字段必须跟着变（共享状态）
  pickRadio(page, "plPreImport", "on");
  const afterPick = {
    plPre: st.plPre,
    stored: sandbox.localStorage.getItem("pp6.plPre"),
    importRadios: radioState(page, "plPreImport"),
    workflowRadios: radioState(page, "plPreWorkflow"),
    patchUnaffected: radioState(page, "plPrePatch"),
  };

  net.queue = [mkPayload(OBJS, 0), mkPayload(OBJS, 7)];
  counters.render = 0;
  counters.toasts = [];
  net.requests = [];
  await ctx.handleFile(mkFile("on.xlsx"));
  await new Promise((r) => setImmediate(r));

  const second = decodeHeader(net.requests[1]);
  out["2_on"] = {
    afterPick,
    requestCount: net.requests.length,
    firstHasHeader: !!decodeHeader(net.requests[0]),
    urls: net.requests.map((r) => r.url),
    version: second && second.version,
    // 头里必须有全部 3 个对象，且全部 linked:true
    headerCodes: second ? Object.keys(second.objects || {}).sort() : [],
    headerLinked: linkedOf(second),
    plLinked: Object.assign({}, st.plLinked),
    // 渲染的是第二次解析的结果
    dataFlowMarker: st.data ? st.data.stats["流程"] : null,
    rendered: counters.render,
    steps: counters.steps.slice(),
    inputHidden: byId.get("stepInput") ? byId.get("stepInput").hidden : null,
    plModalShown: st.plModalShown,
    toasts: counters.toasts.slice(),
  };
}

/* ---------------- ④ 「不关联」：头里全量 false ---------------- */
async function scenarioOff() {
  const page = loadApp();
  const { st, net, counters, ctx, sandbox } = page;

  // 这次从**流程模式**那一档点开：两个菜单确实共享同一档
  pickRadio(page, "plPreWorkflow", "off");
  const afterPick = {
    plPre: st.plPre,
    stored: sandbox.localStorage.getItem("pp6.plPre"),
    importRadios: radioState(page, "plPreImport"),
    workflowRadios: radioState(page, "plPreWorkflow"),
  };

  net.queue = [mkPayload(OBJS, 0), mkPayload(OBJS, 7)];
  counters.render = 0;
  counters.toasts = [];
  net.requests = [];
  // 流程模式页上传走 quiet 分支：同一套两档必须照样生效
  await ctx.handleFile(mkFile("off.xlsx"), { quiet: true });
  await new Promise((r) => setImmediate(r));

  const second = decodeHeader(net.requests[1]);
  out["3_off"] = {
    afterPick,
    requestCount: net.requests.length,
    headerCodes: second ? Object.keys(second.objects || {}).sort() : [],
    headerLinked: linkedOf(second),
    plLinked: Object.assign({}, st.plLinked),
    dataFlowMarker: st.data ? st.data.stats["流程"] : null,
    rendered: counters.render,
    toasts: counters.toasts.slice(),
  };
}

/* ---------------- ⑤ 第二次解析失败：退回第一次，决策清空 ---------------- */
async function scenarioFail() {
  const page = loadApp();
  const { st, net, counters, byId, ctx } = page;
  pickRadio(page, "plPreImport", "on");

  net.queue = [mkPayload(OBJS, 0), { __error: "pp_project 读不到" }];
  counters.render = 0;
  counters.toasts = [];
  counters.steps = [];
  net.requests = [];
  await ctx.handleFile(mkFile("fail.xlsx"));
  await new Promise((r) => setImmediate(r));

  out["4_fail_fallback"] = {
    requestCount: net.requests.length,
    // 第一次解析的结果照常出（marker 还是 0，不是第二次的），页面不能白屏
    dataFlowMarker: st.data ? st.data.stats["流程"] : null,
    rendered: counters.render,
    steps: counters.steps.slice(),
    inputHidden: byId.get("stepInput") ? byId.get("stepInput").hidden : null,
    // 失败就不该留下那份头：否则出包会带一份和选的档位对不上的决策
    plLinked: Object.assign({}, st.plLinked),
    toasts: counters.toasts.slice(),
    saidFallback: counters.toasts.some((t) => t.indexOf("原样分析") >= 0),
  };
}

/* ---------------- ⑥ 第 2 步弹窗：逻辑不受影响 ---------------- */
async function scenarioDiffModal() {
  const page = loadApp();
  const { st, ctx, sandbox, net, counters } = page;
  const calls = [];
  /* 2026-11-15 起弹窗由「待确认的相近字段数」（pendingCount）驱动，
   * 不再是「差异条数」（diffCount）：差异只是说明，待确认才需要问用户。 */
  sandbox.ProjLink = {
    reset() {},
    render() {},
    diffCount: (vmData) => (vmData && vmData.__diff ? 1 : 0),
    pendingCount: (vmData) => (vmData && vmData.__diff ? 1 : 0),
    directDiffCount: () => 0,
    openModal: (vmData) => calls.push(vmData && vmData.__diff),
  };

  // 场景 1：解析后 plModalShown=false，有待确认的相近字段 → 自动开一次
  pickRadio(page, "plPreImport", "on");
  net.queue = [mkPayload(OBJS, 0), mkPayload(OBJS, 7)];
  counters.render = 0;
  net.requests = [];
  await ctx.handleFile(mkFile("diff.xlsx"));
  await new Promise((r) => setImmediate(r));
  const afterHandle = { plModalShown: st.plModalShown };

  st.data.projectAlign = { __diff: true, items: [] };
  ctx.renderProjectLink();
  // 已经弹过一次（plModalShown 现在是 true）→ 再渲染不该重复弹
  ctx.renderProjectLink();
  const opened = calls.slice();

  // 场景 2：没有待确认的（纯 E0 / 全无关）→ 不弹
  calls.length = 0;
  st.plModalShown = false;
  st.data.projectAlign = { __diff: false, items: [] };
  ctx.renderProjectLink();

  out["5_diff_modal"] = {
    afterHandle,
    openedTimes: opened.length,
    openedValues: opened,
    noDiffOpened: calls.length,
  };

  /* 5b：分析完有待确认 → handleFile 不渲染、不进第 3 步，先弹；
     finish 之后才 renderAll + goStep(3)。 */
  calls.length = 0;
  counters.render = 0;
  counters.steps = [];
  const p2 = mkPayload(OBJS, 7);
  p2.projectAlign = { __diff: true, items: [] };
  net.queue = [mkPayload(OBJS, 0), p2];
  await ctx.handleFile(mkFile("defer.xlsx"));
  out["5b_defer"] = {
    deferred: st.plDeferred,
    shown: st.plModalShown,
    steps: counters.steps.slice(),
    opened: calls.slice(),
    renders: counters.render,
  };
  const finished = ctx.finishPlDeferred();
  out["5b_finish"] = {
    finished,
    steps: counters.steps.slice(),
    renders: counters.render,
    deferred: st.plDeferred,
  };
}

/* ---------------- ⑦ 存量包：三档预置勾选 ---------------- */
function makePatchSandbox(prefs, patchSrc) {
  const byId = new Map();
  function q(id) {
    if (!byId.has(id)) {
      const el = makeEl(id);
      el.id = id;
      byId.set(id, el);
    }
    return byId.get(id);
  }
  const sandbox = {
    console,
    setTimeout: () => 0,
    clearTimeout: () => {},
    Promise, JSON, Math, Date, Set, Map, Object, Array, String, Number, Boolean,
    Error, RegExp, encodeURIComponent, decodeURIComponent,
    // patch.js 复用 app.js 的这几个工具函数
    toast: () => {},
    humanSize: (n) => String(n) + " B",
    saveBlob: async () => true,
    native: () => null,
    pickFile: async () => null,
    apiPost: async () => ({}),
    fetch: null,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.document = {
    getElementById: q,
    querySelector: () => makeEl("q"),
    querySelectorAll: () => [],
    addEventListener: () => {},
    createElement: () => makeEl("created"),
    body: makeEl("body"),
  };
  sandbox.window.addEventListener = () => {};
  const store = Object.assign({}, prefs || {});
  sandbox.localStorage = {
    _m: store,
    getItem(k) { return Object.prototype.hasOwnProperty.call(this._m, k) ? this._m[k] : null; },
    setItem(k, v) { this._m[k] = String(v); },
    removeItem(k) { delete this._m[k]; },
  };
  const ctx = vm.createContext(sandbox);
  vm.runInContext(patchSrc + "\n;globalThis.__P = P;", ctx, { filename: PATCH_FILE });
  return { ctx, sandbox, byId, q, P: ctx.__P };
}

function mkSnap() {
  const mk = (code) => ({
    file: `bo/${code}.xml`, code, name: code + "对象", tbl: "pp_" + code,
    fieldCount: 1, copies: 1, hasId: true,
    fields: [{ code: "f_1", name: "字段1", type: "varchar" }],
  });
  return {
    token: "tk", entryCount: 3, boCount: 2, notes: [],
    entries: [{ name: "bo/a.xml", kind: "bo", size: 10 }],
    types: [], objects: [mk("a"), mk("b")],
  };
}

async function patchPreset(prefValue) {
  const patchSrc = fs.readFileSync(PATCH_FILE, "utf8");
  const prefs = prefValue === undefined ? {} : { "pp6.plPrePatch": prefValue };
  const page = makePatchSandbox(prefs, patchSrc);
  const snap = mkSnap();
  page.sandbox.fetch = async () => ({
    ok: true, status: 200,
    json: async () => snap,
    blob: async () => null,
    headers: { get: () => null },
    text: async () => "",
  });
  await page.ctx.pUploadZip({ name: "pkg.zip", size: 2048, arrayBuffer: async () => new ArrayBuffer(8) });

  // 用户看到的：每个对象卡片上「关联项目」勾选框的 checked 状态
  const rendered = page.byId.get("patchObjList");
  const checks = {};
  const re = /data-link="([^"]*)"\s*(checked)?/g;
  let m = re.exec(rendered.innerHTML);
  while (m) {
    checks[m[1]] = /checked/.test(m[2] || "");
    m = re.exec(rendered.innerHTML);
  }
  return {
    mode: page.ctx.pPlPrePatch(),
    link: Array.from(page.P.link).sort(),
    checks,
  };
}

async function scenarioPatch() {
  out["6_patch_preset"] = {
    off: await patchPreset("off"),
    on: await patchPreset("on"),
    keep: await patchPreset("keep"),
    // 没存过 → 默认「按包内」：不预置，不悄悄改用户的包
    unset: await patchPreset(undefined),
    // 脏值 → 也退回 keep
    dirty: await patchPreset("whatever"),
  };
}

/* ---------------- ⑧ localStorage 持久化：重新打开页面还在 ---------------- */
function scenarioPersist() {
  // 上一次会话选了 全部关联 / 不关联，下次打开（新 context）应当原样恢复。
  // 这里用 "on"：默认档就是 "off"，用默认值测不出「真的读回来了」。
  const reopened = loadApp({ "pp6.plPre": "on", "pp6.plPrePatch": "on" });
  // 脏值 / 空值退回默认档
  const dirty = loadApp({ "pp6.plPre": "bogus", "pp6.plPrePatch": "nope" });
  // 老版本存过已删掉的第三档 "excel"：白名单挡掉 → 落回 "off"，不用迁移代码
  const legacy = loadApp({ "pp6.plPre": "excel" });
  // 写回：点一下单选就落盘
  const page = loadApp();
  pickRadio(page, "plPreWorkflow", "on");
  pickRadio(page, "plPrePatch", "off");

  out["7_persist"] = {
    reopened: {
      plPre: reopened.st.plPre,
      plPrePatch: reopened.st.plPrePatch,
      importRadios: radioState(reopened, "plPreImport"),
      workflowRadios: radioState(reopened, "plPreWorkflow"),
      patchRadios: radioState(reopened, "plPrePatch"),
    },
    dirty: { plPre: dirty.st.plPre, plPrePatch: dirty.st.plPrePatch },
    legacyExcel: {
      plPre: legacy.st.plPre,
      importRadios: radioState(legacy, "plPreImport"),
      workflowRadios: radioState(legacy, "plPreWorkflow"),
    },
    written: {
      plPre: page.sandbox.localStorage.getItem("pp6.plPre"),
      plPrePatch: page.sandbox.localStorage.getItem("pp6.plPrePatch"),
      // 两个菜单的两档必须同步：点流程那一个，普通模式那一个也变
      importRadios: radioState(page, "plPreImport"),
      workflowRadios: radioState(page, "plPreWorkflow"),
      patchRadios: radioState(page, "plPrePatch"),
      state: { plPre: page.st.plPre, plPrePatch: page.st.plPrePatch },
    },
  };
}

async function main() {
  staticChecks();
  await scenarioDefault();
  await scenarioOn();
  await scenarioOff();
  await scenarioFail();
  await scenarioDiffModal();
  await scenarioPatch();
  scenarioPersist();
  console.log(JSON.stringify(out));
}

main().catch((err) => {
  console.error("PROBE FAILED:", err && err.stack ? err.stack : err);
  process.exit(1);
});
