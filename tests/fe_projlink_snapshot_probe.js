/* 前端探针：没连库时「关联项目」的字段池决策（真跑 app.js）。
 *
 * 由 tests/test_projlink.py 的 PL83–PL86 调用（`node tests/fe_projlink_snapshot_probe.js
 * pp6kit/static/app.js`），结果以一行 JSON 打到 stdout 由 Python 断言。
 *
 * 为什么要真跑：这次的 bug 是「用户在第 1 步选了关联，弹窗却只给导入项目 zip，
 * 用户点取消 → 关联没生效 → AI 生成的对象没有『项目选择』字段」。
 * grep 源码只能证明文案在文件里，证明不了：
 *   · 有可用随包快照时，#psCancel 真的变成「使用随包快照」并 resolve true；
 *   · 快照不可用时它真的退回「取消」→ resolve false；
 *   · 上一次的「使用随包快照」处理器不会漏到下一次弹窗；
 *   · state.plPre=on 但结果没有链接字段时，预览里真的多一条醒目提示。
 * 不引第三方依赖（打包机只有 openpyxl、也不能联网），DOM 用最小 stub。
 */
const fs = require("fs");
const vm = require("vm");

const FILE = process.argv[2];
const src = fs.readFileSync(FILE, "utf8");

/* ---------------- 最小 DOM stub：按选择器缓存元素，够 app.js 顶层绑定跑完 ----------------
 * classList 用真的 Set 存，不用空壳：这次要断言的正是「主/次按钮（primary/ghost）
 * 有没有按可用快照交换」，空壳会把这条验证悄悄变成永远通过。
 */
function makeEl(sel) {
  const cls = new Set();
  return {
    _sel: sel,
    _cls: cls,
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
    classList: {
      add(...names) { names.forEach((n) => cls.add(n)); },
      remove(...names) { names.forEach((n) => cls.delete(n)); },
      toggle(name, force) {
        const on = force === undefined ? !cls.has(name) : !!force;
        if (on) cls.add(name); else cls.delete(name);
        return on;
      },
      contains: (name) => cls.has(name),
      get value() { return [...cls].join(" "); },
    },
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

// state 是 `const`（不在 global 上），末尾追加一行把它露出来
vm.runInContext(src + "\n;globalThis.__state = state;", ctx, { filename: FILE });

const st = ctx.__state;
const out = {};

/* ---------------- E · 「使用随包快照」真的能 resolve true ---------------- */

// 让 vm 里的微任务跑完（ensureProjectSnapshot 是 async，弹窗渲染发生在 await 之后）
function flush() {
  return new Promise((r) => setImmediate(r));
}

const USABLE = {
  ok: true, dbConfigured: false, source: "package_snapshot",
  sourceLabel: "随包快照", fieldCount: 51,
  generatedAt: "2026-11-14T17:52:44+08:00", status: "ok",
};
const UNAVAILABLE = {
  ok: true, dbConfigured: false, source: "unavailable",
  sourceLabel: "无", fieldCount: 0, generatedAt: "", status: "unavailable",
};

function resetSnapshotState() {
  st.plSnapshotToken = "";
  st.plSnapshotStatus = null;
  st.plTargetKind = "project";
  st.plTargetName = "";
}

/* 打开弹窗（ensureProjectSnapshot 会停在弹窗上等用户选），
 * 等 mask 真的显出来，再把三个按钮的当前态交出去。
 * 注意返回的是 { pending } 而不是 pending：async 函数直接 return 一个 promise
 * 会把它 await 掉（等于卡死在弹窗上等点击）。 */
async function openModalWith(status) {
  resetSnapshotState();
  q("#psMask").hidden = true;
  q("#psTitle").textContent = "";
  q("#psCancel").textContent = "";
  q("#psCancel").onclick = null;
  q("#psPick").textContent = "";
  q("#psSub").textContent = "";
  q("#psLead2").innerHTML = "";
  ctx.fetch = async () => ({ ok: true, json: async () => status });
  // 直接调 askProjectZip：ensureProjectSnapshot 在「随包快照够用」时会静默放行，
  // 不再打开弹窗（2026-11-24 用户反馈「点关联就跳项目弹窗」）。弹窗自身的
  // 文案/主次按钮/处理器仍要逐条验证，所以从这里进。
  const pending = ctx.askProjectZip(status);
  for (let i = 0; i < 40 && q("#psMask").hidden; i += 1) await flush();
  return { pending };
}

function modalSnapshot() {
  const cancel = q("#psCancel");
  const pick = q("#psPick");
  return {
    maskHidden: q("#psMask").hidden,
    title: q("#psTitle").textContent,
    cancelText: cancel.textContent,
    cancelIsFn: typeof cancel.onclick === "function",
    cancelPrimary: cancel.classList.contains("primary"),
    cancelGhost: cancel.classList.contains("ghost"),
    pickText: pick.textContent,
    pickPrimary: pick.classList.contains("primary"),
    pickGhost: pick.classList.contains("ghost"),
    sub: q("#psSub").textContent,
    lead1: q("#psLead1").innerHTML,
    lead2: q("#psLead2").innerHTML,
    status: q("#psStatus").textContent,
  };
}

/* ---------------- F · 预览里的兜底提示 ---------------- */

function aiPayload(opts) {
  const d = {
    bo: {
      code: opts.code || "caigou",
      name: opts.name || "采购申请",
      fields: opts.fields,
    },
    types: [{ code: "varchar", label: "字符串" }],
    objects: [],
    notices: opts.notices || [],
    steps: [],
    seconds: 3,
  };
  if (opts.linkTarget) d.linkTarget = opts.linkTarget;
  return d;
}

function plainField() {
  return {
    name: "申请单号", code: "shenQingDanHao",
    fieldName: "shen_qing_dan_hao_", type: "varchar", length: 64,
  };
}
function linkField() {
  return {
    name: "项目选择", code: "projectId",
    fieldName: "project_id_", type: "object", objectType: "project", isLink: true,
  };
}

async function main() {
  /* 场景 A：随包快照可用 → 取消键就是「使用随包快照」，点了 resolve true */
  let { pending: p } = await openModalWith(USABLE);
  const a = modalSnapshot();
  out.E_usable = a;
  if (a.cancelIsFn) q("#psCancel").onclick();
  out.E_usableResolve = await p;

  /* 场景 B：快照不可用 → 退回「取消」，点了 resolve false
   * （同时证明上一次的「使用随包快照」处理器没有漏过来） */
  p = (await openModalWith(UNAVAILABLE)).pending;
  const b = modalSnapshot();
  out.E_unavailable = b;
  if (b.cancelIsFn) q("#psCancel").onclick();
  out.E_unavailableResolve = await p;

  /* 场景 C：再打开一次可用的 → 文案与处理器必须回到「使用随包快照」
   *（顺序 A→B→A：两个方向都测了一遍「每次打开显式重设」） */
  p = (await openModalWith(USABLE)).pending;
  const c = modalSnapshot();
  out.E_reopen = c;
  if (c.cancelIsFn) q("#psCancel").onclick();
  out.E_reopenResolve = await p;

  /* ---------------- G · 连库时：非阻塞提示（真跑 ensureProjectSnapshot） ----------------
   * 用户把「连库静默跳过 zip 弹窗」误认成弹窗被删了。这里验证：
   *   · dbConfigured=true → 不弹 #psMask、resolve true，用 toast 说清字段来源；
   *   · 同一份文件再调用一次不再 toast（防刷屏）；
   *   · 换文件（模拟 handleFile 重置标志）后才再提示一次；
   *   · 没连库弹「字段来源」选择（2026-11-26 用户要求恢复）；
   *   · 已有 token（导过 zip）的快速返回也不提示。
   */
  const DB = {
    ok: true, dbConfigured: true, source: "target_db",
    sourceLabel: "目标库", fieldCount: 51,
    generatedAt: "2026-11-14T17:52:44+08:00", status: "ok",
  };
  const realToast = ctx.toast;
  const toasts = [];
  ctx.toast = (msg, ms) => { toasts.push(String(msg)); return realToast(msg, ms); };

  async function callWithDb() {
    resetSnapshotState();          // 清 token/缓存，强制重新读 dbConfigured
    q("#psMask").hidden = true;
    ctx.fetch = async () => ({ ok: true, json: async () => DB });
    return await ctx.ensureProjectSnapshot();
  }

  st.plDbHintShown = false;        // 新文件：handleFile() 的重置点
  toasts.length = 0;
  out.G_dbResolve = await callWithDb();
  out.G_first = {
    toasts: toasts.slice(),
    maskHidden: q("#psMask").hidden,
    flag: st.plDbHintShown,
  };

  toasts.length = 0;
  out.G_dbResolveAgain = await callWithDb();
  out.G_second = {
    toasts: toasts.slice(),
    maskHidden: q("#psMask").hidden,
    flag: st.plDbHintShown,
  };

  // 换文件：handleFile() 清掉标志，提示应该重新出现一次
  st.plDbHintShown = false;
  toasts.length = 0;
  await callWithDb();
  out.G_afterNewFile = toasts.slice();

  /* 没连库（随包快照够用）：弹「字段来源」选择弹窗——主按钮「使用随包快照」，
   * 次按钮「导入项目 zip」。2026-11-26 用户要求恢复弹窗：不弹他找不到导 zip
   * 的入口。导过 zip 有 token 后不再弹（G_token 场景管）。 */
  const NO_DB_OK = {
    ok: true, dbConfigured: false, source: "package_snapshot",
    sourceLabel: "随包快照", fieldCount: 51, status: "ok",
  };
  toasts.length = 0;               // 清掉 G_db 段遗留，别让它混进本段断言
  resetSnapshotState();
  st.plDbHintShown = false;
  q("#psMask").hidden = true;
  ctx.fetch = async () => ({ ok: true, json: async () => NO_DB_OK });
  const okPending = ctx.ensureProjectSnapshot();
  for (let i = 0; i < 40 && q("#psMask").hidden; i += 1) await flush();
  out.G_noDbMaskHidden = q("#psMask").hidden;          // false = 弹了
  out.G_noDbTitle = q("#psTitle").textContent;
  out.G_noDbCancelLabel = q("#psCancel").textContent;  // 主按钮：使用随包快照
  out.G_noDbPickLabel = q("#psPick").textContent;      // 次按钮：导入项目 zip（刷新）
  out.G_noDbToasts = toasts.slice();                   // 提示靠弹窗本身，不用 toast
  if (q("#psCancel").onclick) q("#psCancel").onclick(); // 选「使用随包快照」
  out.G_noDbResolve = await okPending;

  // 快照不可用：仍然必须拦住（这里才该弹窗，取消即不继续）
  const NO_DB_BAD = {
    ok: true, dbConfigured: false, source: "unavailable",
    fieldCount: 0, status: "unavailable",
  };
  const badPending = (await (async () => {
    resetSnapshotState();
    st.plDbHintShown = false;
    q("#psMask").hidden = true;
    ctx.fetch = async () => ({ ok: true, json: async () => NO_DB_BAD });
    const pending = ctx.ensureProjectSnapshot();
    for (let i = 0; i < 40 && q("#psMask").hidden; i += 1) await flush();
    return { pending };
  })()).pending;
  out.G_noDbUnavailable = { maskHidden: q("#psMask").hidden, title: q("#psTitle").textContent };
  if (q("#psCancel").onclick) q("#psCancel").onclick();
  out.G_noDbUnavailableResolve = await badPending;

  // 已有上传 token：快速返回，不读状态接口、不提示
  resetSnapshotState();
  st.plSnapshotToken = "probe-token";
  toasts.length = 0;
  ctx.fetch = () => { throw new Error("有 token 时不该读状态接口"); };
  out.G_tokenResolve = await ctx.ensureProjectSnapshot();
  out.G_tokenToasts = toasts.slice();
  st.plSnapshotToken = "";
  ctx.toast = realToast;

  /* ---------------- F · renderAiBo 的「关联开着但没链接字段」提示 ---------------- */
  const WARN = "没有生成「项目选择」字段";

  // ① 关联开着、字段里没有 isLink、目标是别的对象 → 必须提示
  st.plPre = "on";
  let d = aiPayload({
    fields: [plainField()],
    linkTarget: { code: "project", name: "项目" },
    notices: ["原有提示：已补链接字段"],
  });
  let html = ctx.renderAiBo(d);
  out.F_warned = {
    hasWarn: html.indexOf(WARN) >= 0,
    keepsNotices: html.indexOf("原有提示：已补链接字段") >= 0,
    // 后端返回的 notices 原数组不许被改（只动渲染用的副本）
    noticesLen: d.notices.length,
  };

  // ② 有 isLink 字段 → 不提示
  d = aiPayload({
    fields: [linkField(), plainField()],
    linkTarget: { code: "project", name: "项目" },
    notices: [],
  });
  html = ctx.renderAiBo(d);
  out.F_linkOk = html.indexOf(WARN) < 0;

  // ③ 生成的对象本身就是关联目标（自关联）→ 不提示
  d = aiPayload({
    code: "project", name: "项目",
    fields: [plainField()],
    linkTarget: { code: "project", name: "项目" },
    notices: [],
  });
  html = ctx.renderAiBo(d);
  out.F_selfTarget = html.indexOf(WARN) < 0;

  // ④ 关联是关的 → 不提示（没选关联就不该有链接字段）
  st.plPre = "off";
  d = aiPayload({
    fields: [plainField()],
    linkTarget: { code: "project", name: "项目" },
    notices: [],
  });
  html = ctx.renderAiBo(d);
  out.F_offSilent = html.indexOf(WARN) < 0;

  console.log(JSON.stringify(out));
}

main().catch((e) => {
  console.error("PROBE FAIL:", (e && e.stack) || e);
  process.exit(1);
});
