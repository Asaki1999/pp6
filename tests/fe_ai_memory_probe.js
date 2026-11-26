/* 前端探针：AI 对话的记忆——回第 1 步后继续说，要在已有结果上改。
 *
 * 守的是 2026-11-26 用户报的 bug：生成完 → 返回上一步 → 让 AI 加/删字段，
 * 首屏输入框永远 fresh 从零生成（/api/ai/bo），结果只剩新提的那一个字段。
 *
 * 断言四件事：
 *   ① state.aiData 存在时，首屏发消息 → /api/ai/refine，body 带当前 bo + instruction
 *   ② 点「从头重新生成」（#aiRedo1）→ aiData 清空 → 再发 → /api/ai/bo
 *   ③ 冷启动（从未生成过）→ /api/ai/bo
 *   ④ 提示条 / 首屏按钮文案随 aiData 显隐切换
 *
 * 只读产品代码（app.js），不引第三方依赖；DOM 用最小 stub。
 * 用法：node tests/fe_ai_memory_probe.js pp6kit/static/app.js
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
    onclick: null, onchange: null, title: "",
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    appendChild() {}, focus() {}, blur() {}, click() {}, remove() {},
    scrollTo() {},
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

// mock 响应：refine 返回完整结构（sendAiChat 成功路径要跑 renderAiBo / renderAiActions）
const MOCK = {
  ok: true,
  bo: { code: "caigou", name: "采购申请", fields: [
    { code: "name", name: "申请名称", type: "varchar", length: 200 },
    { code: "amount", name: "总金额", type: "number" },
  ]},
  form: { key: "create_caigou", name: "新建采购申请",
          fields: [{ name: "申请名称", label: "申请名称", type: "varchar",
                     group: "基本信息", required: false, fullRow: false,
                     cascadeParent: "" }] },
  list: { key: "caigou_list", name: "采购申请列表", views: ["grid"],
          columns: [{ code: "name", label: "申请名称" }] },
  notices: [], changes: [], types: [], objects: [],
  model: "mock", seconds: 0.1,
};

const calls = [];
const sandbox = {
  console,
  setTimeout: () => 0,
  clearTimeout: () => {},
  Promise, JSON, Math, Date, Set, Map, Object, Array, String, Number, Boolean,
  Error, RegExp, encodeURIComponent, decodeURIComponent, escape, unescape,
  fetch: (url, opts) => {
    calls.push({ url: String(url), opts: opts || {} });
    return Promise.resolve({
      ok: true, status: 200,
      headers: { get: () => "application/json" },
      json: async () => JSON.parse(JSON.stringify(MOCK)),
      blob: async () => ({}),
      arrayBuffer: async () => new ArrayBuffer(0),
      text: async () => "",
    });
  },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.scrollTo = () => {};        // goStep 的滚动兜底
sandbox.PP6_AI_READY = true;        // sendAiChat 的 AI 配置闸（未设会直接 return）
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
  + "globalThis.__sendAiChat = sendAiChat;"
  + "globalThis.__syncEntryResume = syncEntryResume;",
  ctx, { filename: FILE });

const st = ctx.__state;

// 首屏处于第 1 步（#stepInput 可见）= sendAiChat 里的 fromEntry 判定
q("#stepInput").hidden = false;

const out = {};

(async () => {
  // ---- ① 有结果：首屏说「加一个金额字段」→ 必须 /api/ai/refine 且带当前结果
  st.aiData = JSON.parse(JSON.stringify(MOCK));
  st.aiData.bo.fields = [
    { code: "name", name: "申请名称", type: "varchar", length: 200 },
    { code: "amount", name: "总金额", type: "number" },
  ];
  ctx.__syncEntryResume();
  out.resume_shown = q("#aiResumeBar").hidden === false;
  out.send1_label = q("#aiChatSend1").textContent;
  q("#aiChatText1").value = "再加一个金额字段";
  q("#aiChatSend1").onclick();
  await new Promise((r) => setTimeout(r, 10));
  const refineCall = calls.find((c) => c.url === "/api/ai/refine");
  out.refine_called = !!refineCall;
  if (refineCall) {
    const body = JSON.parse(refineCall.opts.body);
    out.refine_has_instruction = body.instruction === "再加一个金额字段";
    out.refine_has_bo = !!(body.bo && body.bo.fields
                            && body.bo.fields.length === 2);
    out.refine_has_form = !!body.form;
    out.refine_has_list = !!body.list;
  }

  // ---- ② 点「从头重新生成」：aiData 清空，再发 → /api/ai/bo（从零设计）
  const redo = q("#aiRedo1");
  out.redo_bound = typeof redo.onclick === "function";
  redo.onclick();
  out.cleared = st.aiData === null;
  out.resume_hidden_after_redo = q("#aiResumeBar").hidden === true;
  out.send1_label_after_redo = q("#aiChatSend1").textContent;
  calls.length = 0;
  q("#aiChatText1").value = "帮我设计一个采购申请单";
  q("#aiChatSend1").onclick();
  await new Promise((r) => setTimeout(r, 10));
  out.fresh_called = calls.some((c) => c.url === "/api/ai/bo");
  out.fresh_body_desc = calls.some((c) => c.url === "/api/ai/bo")
    && (() => { try { return !!JSON.parse(calls.find((c) => c.url === "/api/ai/bo").opts.body).description; } catch (e) { return false; } })();

  // ---- ③ 冷启动：从未生成过 → /api/ai/bo
  st.aiData = null;
  ctx.__syncEntryResume();
  calls.length = 0;
  q("#aiChatText1").value = "做一个合同台账";
  q("#aiChatSend1").onclick();
  await new Promise((r) => setTimeout(r, 10));
  out.cold_called = calls.some((c) => c.url === "/api/ai/bo");

  // ---- ④ 首屏 chips 不再强制 fresh（增量语义的词，有结果时应走 refine）。
  //    stub 的 querySelectorAll 恒为空数组，绑定跑不到，改在源码层钉住：
  //    首屏三处调用（按钮 / Enter / chip）都不许再传 { fresh: true }。
  out.no_forced_fresh = !src.includes(
    'aiChatSendFrom("aiChatText1", "aiChatLog1", { fresh: true })');

  // ---- ⑤ 首屏发起的 refine 成功后要跳到第 2 步（结果在预览页，别让人停在输入框）
  // （上面 ① 已跑过一遍：stub 下 stepPreview.hidden 被 goStep(2) 置 false）
  out.jumped_to_preview = q("#stepPreview").hidden === false
    && q("#stepInput").hidden === true;

  process.stdout.write(JSON.stringify(out) + "\n");
})().catch((err) => {
  process.stderr.write(String((err && err.stack) || err) + "\n");
  process.exit(1);
});
