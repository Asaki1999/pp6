/* 存量包修订：上传 zip → 选对象 → 改已有字段名称 / 加新字段 → 在原包上打补丁下载新 zip。
   复用 app.js 的 saveBlob / toast / humanSize；本文件在 app.js 之后加载。

   产品红线（2026-11-18）：这个工作区对用户开放后，已有业务对象只允许改「名称」。
   类型 / 选项值 / 长度 / 必填 / 索引 / 说明都是只读，且**不能删除字段**——
   后端 patch.py 同样硬拦，前端只是不把口子露出来。 */
const P = {
  snap: null,
  fields: [],
  // 已有字段的改名草稿：{ file: { 字段编码: 新名称 } }。
  // 按 file 分开存：不同对象可以有同名字段（name 几乎每个对象都有）。
  renames: {},
  selected: new Set(),
  fileName: "",
  // 勾了「关联项目」的对象（file 集合）与逐字段对齐决策
  // { file: { 字段编码: true|false } }。默认空 = 只做自动对齐（精确同名 + 同族）
  link: new Set(),
  align: {},
  alignReport: null,
};


const pEsc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
));
const pId = (id) => document.getElementById(id);

/* 存量包页「业务对象关联项目」的三档（在上传卡片上方，读包前就定）：
 *   off  全不勾 / on 全勾 / keep 按包内（保留包里现状，不预置）
 * 存 localStorage（pp6.plPrePatch）。和普通模式那三档分开存：同一档套在
 * 「Excel 里的对象」和「zip 里的对象」上意思不一样，混用会互相改掉。
 * localStorage 读不到（隐私模式）就按 keep —— 宁可不动，也别悄悄改用户的包。 */
const P_PRE_PATCH_KEY = "pp6.plPrePatch";
const P_PRE_PATCH_MODES = ["off", "on", "keep"];

function pPlPrePatch() {
  try {
    const raw = window.localStorage ? window.localStorage.getItem(P_PRE_PATCH_KEY) : null;
    return P_PRE_PATCH_MODES.indexOf(raw) >= 0 ? raw : "keep";
  } catch (err) {
    return "keep";
  }
}

/* 前端也能认中文类型名，和后端 fieldtypes.resolve 同一套口径 */
const P_TYPE_ALIAS = {
  字符串: "varchar", 单行文本: "varchar", text: "varchar",
  数字: "number", 日期时间: "datetime", datePicker: "datetime",
  大文本: "clob", 多行文本: "clob", textarea: "clob",
  用户: "user", 人员: "user", 部门: "department",
  选项: "dic", 数据字典: "dic", dictionary: "dic",
  对象: "object", 关联对象: "object", select: "object",
  自动编号: "autoNumber", 附件列表: "attachment", 公式: "formula",
  用户组: "userGroupCate", 表格: "table", 国际化: "i18n",
};

async function pApi(path, payload, raw = false) {
  const opt = raw
    ? { method: "POST", body: payload }
    : { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload || {}) };
  const res = await fetch(path, opt);
  if (!res.ok) {
    let msg = `请求失败 ${res.status}`;
    const ct = res.headers.get("Content-Type") || "";
    if (ct.includes("json")) {
      const data = await res.json().catch(() => ({}));
      msg = data.error || msg;
    }
    throw new Error(msg);
  }
  return res;
}

async function pDownload(res, fallback) {
  const blob = await res.blob();
  const raw = res.headers.get("X-Filename");
  const name = raw ? decodeURIComponent(raw) : fallback;
  return saveBlob(blob, name);
}

function pFail(err) {
  toast("操作失败：" + (err && err.message ? err.message : err), 6500);
}

function pBusy(btn, on, text) {
  if (!btn) return;
  btn.disabled = on;
  if (on) {
    btn._label = btn.textContent;
    if (text) btn.textContent = text;
  } else if (btn._label) {
    btn.textContent = btn._label;
  }
}

// 当前填的字段编码里，哪些在选中对象的包里已经存在 —— 这些只会应用「名称」
function pDupCodes() {
  if (!P.snap) return [];
  const have = new Set();
  (P.snap.objects || []).forEach((o) => {
    if (P.selected.has(o.file)) (o.fields || []).forEach((f) => have.add(f.code));
  });
  return P.fields.map((f) => String(f.code || "").trim()).filter((c) => c && have.has(c));
}

// 选中对象上实际改了几个名称（和原名称不同才算）
function pRenamePairs() {
  if (!P.snap) return [];
  const out = [];
  (P.snap.objects || []).forEach((o) => {
    if (!P.selected.has(o.file)) return;
    const ren = P.renames[o.file] || {};
    (o.fields || []).forEach((f) => {
      const name = String(ren[f.code] || "").trim();
      if (name && name !== String(f.name || "").trim()) {
        out.push({ file: o.file, code: f.code, from: f.name || "", to: name,
                   object: o.name || o.code });
      }
    });
  });
  return out;
}

function pHint() {
  const el = pId("patchHint");
  const n = P.selected.size;
  const dup = pDupCodes();
  // 编码和已有字段重名的行不算「新字段」——它只会被当成改名
  const f = P.fields.filter((x) => {
    const c = (x.code || "").trim();
    return c && !dup.includes(c);
  }).length;
  const renames = pRenamePairs();
  // 空态分支并入这里作单点真相：pEmptyState() 不改 hint，避免两处互相覆盖。
  const empty = P.snap && (P.snap.objects || []).length === 0;
  el.style.color = "";
  if (empty) {
    el.textContent = "包内没有可新增字段的 bo/*.xml；请在「普通模式」反解后重新出包";
    return;
  }
  if (!P.snap) {
    el.textContent = "请先上传 zip，再选择对象与字段";
    return;
  }
  const kinds = [];
  if (renames.length) kinds.push(`<b>改 ${renames.length} 个字段名称</b>`);
  if (f) kinds.push(`${f} 个新字段`);
  el.innerHTML = `已选 ${n} 个对象${kinds.length ? " · " + kinds.join(" · ") : ""}`
    + (n && kinds.length ? " · 可以出包了" : "");
  if (dup.length) {
    // 最容易踩的坑：以为填了编码就能改类型/选项 —— 其实只会应用名称
    el.style.color = "var(--warn)";
    el.innerHTML += ` <span style="color:var(--warn)">· ${dup.length} 个编码包里已有`
      + `（${dup.slice(0, 4).map(pEsc).join("、")}${dup.length > 4 ? "…" : ""}）：`
      + `只应用「名称」，类型/选项等改动会被忽略</span>`;
  }
  pPaintVSteps();
}

/* ---------------- ③④ 修改字段：改名 / 新增合在一张卡的两个页签 ----------------
 * patch.js 只负责数据与渲染，页签的显隐在这里收口：
 *   - 改名面板：有包且选中了对象才可用（否则给一句提示，不整块消失）
 *   - 新增面板：有包就可填
 * 这样 pRenderExisting() 不再直接写 hidden，避免把另一个页签顶出来。 */
function pApplyEditTabs() {
  const box = pId("patchEditTabs");
  if (!box) return;
  const active = box.querySelector(".tab.active") || box.querySelector(".tab");
  const name = active ? active.dataset.tab : "rename";
  const ex = pId("patchExistingCard");
  const add = pId("patchFieldsCard");
  // 没勾对象时也保留「改名」面板：里面有一句「请先勾选业务对象」，
  // 直接藏掉会只剩一个空卡片，用户不知道少了什么。
  if (ex) ex.hidden = name !== "rename";
  if (add) add.hidden = name !== "add";
}

/* 左侧纵向步骤：只反映「走到哪」，点击滚动到对应卡片；锁住的步骤不响应 */
function pPaintVSteps() {
  const snap = P.snap;
  const total = ((snap && snap.objects) || []).length;
  const picked = P.selected.size;
  const renames = pRenamePairs().length;
  const newFields = (P.fields || []).filter((f) => String(f.code || "").trim()).length;
  const set = (n, kind, sub) => {
    const el = document.querySelector(`.vstep[data-vstep="${n}"]`);
    if (!el) return;
    el.classList.remove("done", "now", "lock");
    if (kind) el.classList.add(kind);
    const hint = pId(`vstep${n}Sub`);
    if (hint && sub) hint.textContent = sub;
  };
  set(1, snap ? "done" : "now",
      snap ? `${P.fileName || "原包"} · ${total} 个对象` : "选择一个 zip 开始");
  set(2, !snap ? "lock" : picked ? "done" : "now", `${picked} / ${total} 个对象`);
  set(3, !snap ? "lock" : picked ? "now" : "lock",
      `${renames} 个改名 · ${newFields} 个新字段`);
  set(4, !snap ? "lock" : "", "预览改动后下载");
}

/* ---------------- ① 上传原包 ---------------- */

async function pUploadZip(file) {
  if (!file) return;
  if (!/\.zip$/i.test(file.name)) { toast("请选择 .zip 文件"); return; }
  const btn = pId("patchPickZip");
  pBusy(btn, true, "正在读取…");
  try {
    const res = await pApi("/api/patch/inspect", await file.arrayBuffer(), true);
    P.snap = await res.json();
    P.fileName = file.name;
    P.selected = new Set();
    P.renames = {};

    // 换包就清掉上一包的对齐勾选，否则会给新包里同名 file 算出奇怪的决策。
    // 再按初始页那一档预置「关联项目」：on = 每个对象卡片都勾上，off = 全不勾，
    // keep = 保持现状（不预置）。只是预置勾选，不强制跑对齐分析。
    P.link = new Set();
    if (pPlPrePatch() === "on") {
      (P.snap.objects || []).forEach((o) => P.link.add(o.file));
    }
    P.align = {};
    P.alignReport = null;
    const alignCard = pId("patchAlignCard");
    if (alignCard) alignCard.hidden = true;
    if (P.snap.objects.length) P.selected.add(P.snap.objects[0].file);
    // 不再自动塞一个空行：草稿为空时给出空态，用户按「手动加一行」或导入

    pId("patchFileInfo").hidden = false;
    pId("patchFileInfo").innerHTML =
      `<span class="dot"></span><strong>${pEsc(file.name)}</strong>` +
      `<span class="muted">${humanSize(file.size)} · ${P.snap.entryCount} 个条目 · ${P.snap.boCount} 份业务对象 XML</span>`;
    const notes = P.snap.notes || [];
    const box = pId("patchNotes");
    box.hidden = notes.length === 0;
    box.innerHTML = notes.map((n) => `<div>· ${pEsc(n)}</div>`).join("");

    ["patchObjectsCard", "patchEditCard", "patchBuildCard"].forEach(
      (id) => { pId(id).hidden = false; });
    pApplyEditTabs();
    pRenderObjects();
    pRenderExisting();
    pRenderFields();
    pId("patchReport").hidden = true;
    pHint();
    // P0-2：消费 P.snap 已有的 entries/comment/copies（后端 patch.py 零改动）
    pRenderPackageStruct();
    pEmptyState();
    toast(`已读取 ${P.snap.objects.length} 份业务对象 XML，请选择对象后新增字段`, 4500);
  } catch (err) {
    pFail(err);
  } finally {
    pBusy(btn, false);
  }
}

function pRenderObjects() {
  const list = pId("patchObjList");
  const cols = [];
  P.snap.objects.forEach((o, i) => {
    const checked = P.selected.has(o.file) ? "checked" : "";
    const dup = o.copies > 1
      ? `<span class="patch-tag warn">包内 ${o.copies} 份副本</span>` : "";
    const noid = o.hasId ? "" : `<span class="patch-tag warn">没有对象 id</span>`;
    const chips = o.fields.map((f) =>
      `<code title="${pEsc(f.name || f.code)} · ${pEsc(f.type || "")}">${pEsc(f.code)}</code>`).join("");
    cols.push(`<div class="patch-obj ${checked ? "on" : ""}" data-file="${pEsc(o.file)}">
      <label class="patch-obj-head">
        <input type="checkbox" data-pick="${pEsc(o.file)}" ${checked}>
        <span class="patch-obj-title"><b>${pEsc(o.name || o.code)}</b>
          <code>${pEsc(o.code)}</code></span>
        <span class="patch-obj-meta">${pEsc(o.file)} · ${o.fieldCount} 个字段 · ${pEsc(o.tbl || "无表名")}</span>
        ${dup}${noid}
      </label>
      <label class="patch-obj-link">
        <input type="checkbox" data-link="${pEsc(o.file)}"
               ${P.link.has(o.file) ? "checked" : ""}>
        关联项目
        <span class="muted">勾选后按项目对象定义对齐字段；已有字段只改类型/选项/长度/必填/索引，
          编码与物理列名不动（给系统里还没有的新对象用）</span>
      </label>
      <details><summary>查看已有字段（${o.fieldCount}）</summary>
        <div class="patch-chips">${chips || "<span class=sub>无</span>"}</div></details>
    </div>`);
  });
  list.innerHTML = cols.join("");
  pId("patchObjSummary").textContent =
    `共 ${P.snap.objects.length} 份 bo XML，勾中的都会加上同一批新字段`;
  list.querySelectorAll("[data-pick]").forEach((box) => {
    box.onchange = () => {
      const file = box.dataset.pick;
      if (box.checked) P.selected.add(file); else P.selected.delete(file);
      box.closest(".patch-obj").classList.toggle("on", box.checked);
      pRenderExisting();
      pHint();
    };
  });
  // 「关联项目」勾选：勾上就自动跑一次对齐分析（纯本地，不上传）
  list.querySelectorAll("[data-link]").forEach((box) => {
    box.onchange = () => {
      const file = box.dataset.link;
      if (box.checked) {
        P.link.add(file);
        if (!P.selected.has(file)) P.selected.add(file);
      } else {
        P.link.delete(file);
        delete P.align[file];
      }
      pRenderObjects();
      pAlign();
      pHint();
    };
  });
  pPaintVSteps();
}

/* ---------------- ③ 已有字段：只允许改名称 ----------------
   每行只给一个可编辑输入框（新名称），其余列是纯展示。
   为什么不直接在下方字段表里「填编码 + 填名称」：那张表是给新字段用的完整定义，
   放进来用户就会顺手改类型/选项，正是这次要收掉的口子。 */

function pRenameOf(file, code) {
  const row = P.renames[file] || {};
  return row[code] === undefined ? "" : row[code];
}

function pRenderExisting() {
  const card = pId("patchExistingCard");
  const box = pId("patchExistingBox");
  if (!card || !box) return;
  const files = ((P.snap && P.snap.objects) || []).filter((o) => P.selected.has(o.file));
  // 空只影响「改名」页签的内容，不把整张卡藏掉（另一个页签还要用）
  card.dataset.empty = (!P.snap || files.length === 0) ? "1" : "";
  if (!files.length) {
    box.innerHTML = '<div class="sub">请先在上一步勾选至少一个业务对象。</div>';
    const badge = pId("patchRenameBadge");
    if (badge) badge.textContent = "0";
    pApplyEditTabs();
    return;
  }
  box.innerHTML = files.map((o) => {
    const changed = Object.keys(P.renames[o.file] || {}).length;
    const rows = (o.fields || []).map((f) => {
      const val = pRenameOf(o.file, f.code);
      return `<tr class="${val.trim() ? "on" : ""}">
        <td><code>${pEsc(f.code)}</code></td>
        <td class="muted">${pEsc(f.type || "")}</td>
        <td class="muted"><code>${pEsc(f.fieldName || "")}</code></td>
        <td>${pEsc(f.name || "")}</td>
        <td><input data-rf="${pEsc(o.file)}" data-rc="${pEsc(f.code)}"
              value="${pEsc(val)}" placeholder="${pEsc(f.name || "（未命名）")}"
              spellcheck="false" autocomplete="off"></td>
      </tr>`;
    }).join("");
    return `<details open class="patch-exist">
      <summary><b>${pEsc(o.name || o.code)}</b> <code>${pEsc(o.file)}</code>
        <span class="muted">${(o.fields || []).length} 个字段</span>
        ${changed ? `<span class="patch-tag">已改 ${changed}</span>` : ""}</summary>
      <div class="tbl-wrap"><table class="patch-table patch-exist-table">
        <thead><tr><th>字段编码</th><th>类型</th><th>物理列名</th>
          <th>原名称</th><th>新名称（可改）</th></tr></thead>
        <tbody>${rows}</tbody></table></div></details>`;
  }).join("");
  // 边打边存，不重渲染（重渲染会丢焦点）
  box.querySelectorAll("input[data-rf]").forEach((el) => {
    el.addEventListener("input", () => {
      const file = el.dataset.rf;
      const code = el.dataset.rc;
      const val = el.value;
      P.renames[file] = P.renames[file] || {};
      if (!val.trim()) delete P.renames[file][code];
      else P.renames[file][code] = val;
      el.closest("tr").classList.toggle("on", !!val.trim());
      const changed = files.reduce((n, o) => n + Object.keys(P.renames[o.file] || {}).length, 0);
      const badge = pId("patchRenameBadge");
      if (badge) badge.textContent = String(changed);
      pHint();
    });
  });
  const changed = files.reduce((n, o) => n + Object.keys(P.renames[o.file] || {}).length, 0);
  const badge = pId("patchRenameBadge");
  if (badge) badge.textContent = String(changed);
  pApplyEditTabs();
  pPaintVSteps();
}

/* ---------------- ④ 字段表格（只放新字段） ---------------- */

function pEmptyField() {
  return { code: "", name: "", type: "varchar", choices: "", objectType: "",
           length: "", precision: "", default: "", required: false,
           index: false, desc: "", src: "manual" };
}

function pNormalizeType(v) {
  const text = String(v || "").trim();
  return P_TYPE_ALIAS[text] || text || "varchar";
}

/* 类型下拉用中文名（后台「添加字段」的叫法），值仍是代码。
   设计稿只列了 7 种常用，这里按 /api/patch/inspect 返回的 types 全量给，
   避免 AI 返回 date/department 这类合法类型在界面上显示成空。 */
function pTypeLabel(code) {
  const list = (P.snap && P.snap.types) || [];
  const hit = list.find((t) => t.code === code);
  return hit ? hit.label : (code || "字符串");
}

function pTypeOptions(selected) {
  const list = (P.snap && P.snap.types) || [];
  const seen = new Set();
  const rows = [];
  list.forEach((t) => {
    seen.add(t.code);
    rows.push(`<option value="${pEsc(t.code)}"${t.code === selected ? " selected" : ""}>${pEsc(t.label)}</option>`);
  });
  if (selected && !seen.has(selected)) {
    rows.push(`<option value="${pEsc(selected)}" selected>${pEsc(pTypeLabel(selected))}</option>`);
  }
  return rows.join("");
}

/* 展开的完整定义按索引记：行被删/重排时全部收起，避免展开错行 */
let pMoreOpen = new Set();

function pSrcBadge(src) {
  // 列宽只有 40px：写全「Excel」会挤掉行尾按钮，用短标 + title 说明来源
  const map = {
    manual: ["手输", "", "手动填写"],
    excel: ["表", "excel", "从 Excel 导入"],
    ai: ["AI", "ai", "AI 对话加入"],
  };
  const [text, cls, title] = map[src] || map.manual;
  return `<span class="v5a-src ${cls}" title="${title}">${text}</span>`;
}

function pRenderFields() {
  const body = pId("patchRows");
  if (!body) return;
  body.innerHTML = P.fields.map((f, i) => {
    const options = f.type === "dic"
      ? `<input type="text" data-k="choices" value="${pEsc(f.choices)}" placeholder="高,中,低">`
      : f.type === "object"
        ? `<input type="text" data-k="objectType" value="${pEsc(f.objectType)}" placeholder="project">`
        : `<span class="v5a-none">—</span>`;
    const more = pMoreOpen.has(i) ? "" : " hidden";
    return `<tr data-i="${i}">
      <td><input type="text" data-k="name" value="${pEsc(f.name)}" placeholder="风险等级"></td>
      <td><input type="text" data-k="code" class="v5a-code" value="${pEsc(f.code)}" placeholder="riskLevel" spellcheck="false"></td>
      <td class="v5a-type"><select class="v5a-typesel" data-k="type">${pTypeOptions(f.type)}</select></td>
      <td class="v5a-opts"><span class="v5a-optwrap">${options}<label class="v5a-req"><input type="checkbox" data-k="required"${f.required ? " checked" : ""}>必填</label></span></td>
      <td class="v5a-srctd">${pSrcBadge(f.src)}</td>
      <td class="v5a-acts">
        <button type="button" class="v5a-rowact more" data-more="${i}" title="展开完整定义：长度、精度、默认值、索引、说明">⋯</button>
        <button type="button" class="v5a-rowact" data-del="${i}" title="从本次草稿移除（不影响包里已有字段）">✕</button>
      </td>
    </tr>
    <tr class="v5a-more" data-i="${i}"${more}>
      <td colspan="6"><div class="v5a-moregrid">
        <label>长度<input type="text" data-k="length" value="${pEsc(f.length)}" placeholder="如 64"></label>
        <label>精度<input type="text" data-k="precision" value="${pEsc(f.precision)}" placeholder="小数位"></label>
        <label>默认值<input type="text" data-k="default" value="${pEsc(f.default)}" placeholder="—"></label>
        <label>索引<select class="v5a-index" data-k="index"><option value="">无</option><option value="1"${f.index ? " selected" : ""}>有</option></select></label>
        <label>说明<textarea data-k="desc" placeholder="给导入后维护的人看">${pEsc(f.desc)}</textarea></label>
      </div></td>
    </tr>`;
  }).join("");

  const empty = pId("patchEmpty");
  if (empty) empty.hidden = P.fields.length > 0;
  const count = pId("patchDraftCount");
  if (count) count.textContent = String(P.fields.length);

  body.querySelectorAll("[data-k]").forEach((el) => {
    const apply = () => {
      const tr = el.closest("tr[data-i]");
      if (!tr) return;
      const i = Number(tr.dataset.i);
      const k = el.dataset.k;
      if (!P.fields[i]) return;
      if (el.type === "checkbox") P.fields[i][k] = el.checked;
      else if (k === "index") P.fields[i][k] = el.value === "1";
      else P.fields[i][k] = el.value;
      if (k === "type") pRenderFields();   // 类型变了，第 4 列要换成选项/关联输入
      else pHint();
    };
    el.addEventListener("input", apply);
    el.addEventListener("change", apply);
  });
  body.querySelectorAll("[data-more]").forEach((btn) => {
    btn.onclick = () => {
      const i = Number(btn.dataset.more);
      if (pMoreOpen.has(i)) pMoreOpen.delete(i); else pMoreOpen.add(i);
      pRenderFields();
    };
  });
  body.querySelectorAll("[data-del]").forEach((btn) => {
    btn.onclick = () => {
      P.fields.splice(Number(btn.dataset.del), 1);
      pMoreOpen = new Set();
      pRenderFields(); pHint();
    };
  });
  pPaintVSteps();
}

/* 新加入的行淡蓝底 4 秒（CSS v5a-new 的 animation 负责淡出） */
function pFlashRows(indexes) {
  indexes.forEach((i) => {
    const tr = document.querySelector(`#patchRows tr[data-i="${i}"]`);
    if (!tr) return;
    tr.classList.add("v5a-new");
    tr.scrollIntoView({ block: "nearest", behavior: "smooth" });
    setTimeout(() => tr.classList.remove("v5a-new"), 4200);
  });
}

/* 包里已有字段（只给编码与名称）：自由导入判重名、对话红线都靠它 */
function pExistingFields() {
  const out = [];
  ((P.snap && P.snap.objects) || []).forEach((o) => {
    (o.fields || []).forEach((f) => {
      if (f && f.code) out.push({ code: f.code, name: f.name || "" });
    });
  });
  return out;
}

function pCollectFields() {
  return P.fields
    .filter((f) => String(f.code || "").trim())
    .map((f) => {
      const out = {
        code: String(f.code).trim(),
        name: String(f.name || "").trim(),
        type: pNormalizeType(f.type),
        choices: String(f.choices || "").trim(),
        objectType: String(f.objectType || "").trim(),
        default: String(f.default || "").trim(),
        desc: String(f.desc || "").trim(),
        required: !!f.required,
        index: !!f.index,
      };
      for (const k of ["length", "precision"]) {
        const v = String(f[k] || "").trim();
        if (v && Number.isFinite(Number(v))) out[k] = Number(v);
      }
      return out;
    });
}
function pSpec(targets) {
  if (!P.snap) throw new Error("请先上传原来的 zip 包");
  const picked = targets || Array.from(P.selected);
  if (!picked.length) throw new Error("请勾选至少一个业务对象");
  const newFields = pCollectFields();
  const renames = pRenamePairs();
  // 勾了「关联项目」时允许一个字段都不填：纯对齐（覆盖已有字段定义 + 补链接字段）
  const linkProject = picked.filter((f) => P.link.has(f));
  if (!newFields.length && !renames.length && !linkProject.length) {
    throw new Error("请至少改一个字段名称、填一个新字段，或勾选「关联项目」");
  }
  // 已有字段改名按 file 分开传（不同对象可以有同名字段：name 几乎每个对象都有）。
  // byTarget 里同时带上新字段：后端 _fields_for 优先读 byTarget，漏掉就等于新字段丢了。
  const byTarget = {};
  picked.forEach((file) => {
    const obj = (P.snap.objects || []).find((o) => o.file === file) || {};
    const have = new Set((obj.fields || []).map((f) => f.code));
    const rows = [];
    const seen = new Set();
    pRenamePairs().filter((r) => r.file === file).forEach((r) => {
      rows.push({ code: r.code, name: r.to, rename: true });
      seen.add(r.code);
    });
    newFields.forEach((f) => {
      if (have.has(f.code)) {
        // 已经有这个字段：只认名称；定义照原样带上，让后端在报告里写明「已忽略」
        if (!f.name || seen.has(f.code)) return;
        rows.push({ ...f, name: f.name, rename: true });
        seen.add(f.code);
        return;
      }
      rows.push(f);
    });
    byTarget[file] = rows;
  });
  // 对齐决策只传布尔许可，服务端会重新分析（不信任前端给的 code/定义）
  const align = {};
  linkProject.forEach((file) => {
    const row = P.align[file];
    if (row && Object.keys(row).length) align[file] = row;
  });
  return {
    token: P.snap.token,
    targets: picked,
    fields: [],
    byTarget,
    // 已有字段只允许改名称，语义固定为 rename（skip 会让改名整批失效）
    mode: "rename",
    scope: pId("patchScope").value,
    allCopies: pId("patchAllCopies").checked,
    syncForm: pId("patchSyncForm").checked,
    linkProject,
    align,
  };
}

/* ---------------- ③ 项目对齐分析 ---------------- */

function pAlignRows(report) {
  if (!report) return "";
  const lines = [];
  (report.linkFields || []).forEach((it) => {
    lines.push(it.action === "add"
      ? `<div class="pl-line ok">+ 新增 <code>${pEsc(it.code)}</code>（${pEsc(it.fieldName)} ${pEsc(it.type)}/${pEsc(it.length)}）</div>`
      : `<div class="pl-line">= ${pEsc(it.note || "复用已有链接字段")}</div>`);
  });
  (report.aligned || []).forEach((it) => {
    lines.push(`<div class="pl-line ok">~ 覆盖 <code>${pEsc(it.fieldCode)}</code>
      （${pEsc(it.fieldName)}）→ ${pEsc(it.type)}
      ${it.objectType ? "· " + pEsc(it.objectType) : ""}
      <span class="muted">保留物理列 ${pEsc(it.column || it.newField || "")}${
        it.projectField ? `（项目侧 ${pEsc(it.projectField)}）` : ""}</span></div>`);
  });
  (report.skipped || []).forEach((it) => {
    lines.push(`<div class="pl-line muted">- 跳过 ${pEsc(it.fieldCode)}：${pEsc(it.message)}</div>`);
  });
  (report.conflicts || []).forEach((it) => {
    lines.push(`<div class="pl-line bad">✗ ${pEsc(it.message)}</div>`);
  });
  const s = report.summary || {};
  lines.push(`<div class="set-note">合计：新增 ${s.linkFields || 0} / 覆盖 ${s.aligned || 0}
    / 跳过 ${s.skipped || 0}</div>`);
  return lines.join("");
}

async function pAlign() {
  const card = pId("patchAlignCard");
  const box = pId("patchAlignBox");
  if (!card || !box) return;
  if (!P.snap || !P.link.size) {
    card.hidden = true;
    P.alignReport = null;
    return;
  }
  card.hidden = false;
  box.innerHTML = '<div class="sub">正在分析…</div>';
  try {
    const spec = pSpec();
    // 只预览对齐：把用户手填字段与改名清空，报告里就只剩「项目对齐」一节
    const res = await pApi("/api/patch/dry-run",
      { ...spec, fields: [], byTarget: {}, linkProject: Array.from(P.link) });
    const data = await res.json();
    P.alignReport = data.projectAlign || null;
    box.innerHTML = pAlignRows(P.alignReport);
  } catch (err) {
    P.alignReport = null;
    box.innerHTML = `<div class="patch-tag warn">${pEsc(err.message || String(err))}</div>`;
  }
}

/* ---------------- 批量录入 ---------------- */

function pParsePaste(text) {
  const rows = [];
  String(text || "").split(/\r?\n/).forEach((line) => {
    if (!line.trim()) return;
    let cols;
    if (line.includes("\t")) {
      cols = line.split("\t");
    } else {
      const parts = line.split(/[,，]/);
      // 选项值里也会带逗号：第 4 列之后的溢出部分并回 choices
      cols = parts.length > 8
        ? [...parts.slice(0, 3), parts.slice(3, parts.length - 4).join(","),
           ...parts.slice(-4)]
        : parts;
    }
    const get = (i) => (cols[i] || "").trim();
    rows.push({
      ...pEmptyField(),
      code: get(0),
      name: get(1),
      type: pNormalizeType(get(2)),
      choices: get(3),
      length: get(4),
      precision: get(5),
      default: get(6),
      desc: get(7),
    });
  });
  return rows.filter((r) => r.code);
}

async function pImportFields(file) {
  if (!file) return;
  try {
    const res = await pApi("/api/patch/fields", await file.arrayBuffer(), true);
    const data = await res.json();
    const rows = (data.fields || []).map((f) => ({ ...pEmptyField(), ...f, src: "excel" }));
    if (!rows.length) { toast("字段表中未读取到字段行"); return; }
    P.fields = P.fields.filter((f) => String(f.code || "").trim())
      .concat(rows);
    pRenderFields(); pHint();
    toast(`导入了 ${rows.length} 个字段`, 4000);
  } catch (err) { pFail(err); }
}

/* ---------------- ④ 任意 Excel 识别 + 对话调整（v5） ---------------- */

let pLlmReady = false;

function pChatHtml() {
  return `<div class="v5a-chathead"><svg class="i sm"><use href="#i-spark"/></svg>AI 调整字段
      <span class="tag ok" id="v5aCfgTag">已配置</span></div>
    <div class="v5a-log" id="v5aLog" aria-live="polite"></div>
    <div class="v5a-ask" id="v5aAsk">
      <textarea id="v5aText" rows="1" placeholder="加、删或改哪个字段？"></textarea>
      <button type="button" class="btn primary v5a-send" id="v5aSend">添加</button>
    </div>`;
}

function pBindChat() {
  const send = pId("v5aSend");
  const text = pId("v5aText");
  if (!send) return;
  send.onclick = () => pChatSend();
  if (text) {
    text.onkeydown = (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); pChatSend(); }
    };
  }
  document.querySelectorAll("#v5aLog [data-row]").forEach((chip) => {
    chip.onclick = () => pFlashRows([Number(chip.dataset.row)]);
  });
}

function pRailNoLlm() {
  const log = pId("v5aLog");
  const ask = pId("v5aAsk");
  if (ask) ask.hidden = true;
  if (log) log.innerHTML = `<div class="v5a-nollm"><b>配置大模型后可对话调整字段</b>
    <div style="margin-top:9px"><button type="button" class="btn ghost sm" id="v5aGoSet">打开设置</button></div></div>`;
  const go = pId("v5aGoSet");
  if (go) go.onclick = () => {
    const m = document.querySelector('.mode[data-mode="settings"]');
    if (m) m.click();
  };
}

/* 恢复对话卡（候选清单取消/确认后回到普通态） */
function pRailDefault() {
  const chat = pId("v5aChat");
  if (!chat) return;
  chat.innerHTML = pChatHtml();
  const log = pId("v5aLog");
  if (log) {
    log.innerHTML = P.chatLogHtml
      || `<div class="v5a-emptylog">说一句要加或去掉的字段，例如「加一个风险等级，选项高、中、低」</div>`;
  }
  if (!pLlmReady) pRailNoLlm();
  pBindChat();
}

function pChatMsg(role, html, chips) {
  const log = pId("v5aLog");
  if (!log) return;
  const box = document.createElement("div");
  box.className = "v5a-msg " + role;
  const chipHtml = (chips || []).map((c) =>
    `<button type="button" class="v5a-chip ${c.cls || ""}" data-row="${c.row}">${pEsc(c.text)}</button>`).join("");
  box.innerHTML = html + (chipHtml ? `<div class="v5a-chips">${chipHtml}</div>` : "");
  log.appendChild(box);
  log.scrollTop = log.scrollHeight;
  box.querySelectorAll("[data-row]").forEach((chip) => {
    chip.onclick = () => pFlashRows([Number(chip.dataset.row)]);
  });
  P.chatLogHtml = log.innerHTML;
}

async function pLoadLlm() {
  try {
    const res = await fetch("/api/nlq/config", { cache: "no-store" });
    const data = await res.json();
    pLlmReady = !!(data && data.llm && data.llm.ready);
  } catch (_) {
    pLlmReady = false;
  }
  const tag = pId("v5aCfgTag");
  if (tag) {
    tag.textContent = pLlmReady ? "已配置" : "未配置";
    tag.className = "tag " + (pLlmReady ? "ok" : "warn");
  }
  if (!pLlmReady) {
    pRailNoLlm();
  } else if (document.querySelector("#v5aChat .v5a-nollm")) {
    // 首屏先按「未配置」渲染，状态回来后要把空态换回可输入的对话
    pRailDefault();
  }
}

function pShowCandidates(data, filename) {
  const chat = pId("v5aChat");
  if (!chat) return;
  const fields = data.fields || [];
  const existing = new Set(pExistingFields().map((f) => f.name));
  const rows = fields.map((f, i) => {
    const dup = existing.has(f.name);
    const sub = [f.code, pTypeLabel(f.type), f.choices].filter(Boolean).map(pEsc).join(" · ");
    return `<label class="v5a-row"><input type="checkbox" data-ci="${i}"${dup ? "" : " checked"}>
      <span class="v5a-rtx"><b>${pEsc(f.name)}</b>
      <span class="v5a-sub">${sub}</span>
      ${dup ? `<span class="v5a-warn">包里已有「${pEsc(f.name)}」，默认跳过</span>` : ""}</span></label>`;
  }).join("");
  const notes = (data.notes || []).length
    ? `<div class="v5a-note">${data.notes.map((n) => `· ${pEsc(n)}`).join("<br>")}</div>` : "";
  chat.innerHTML = `<div class="v5a-confirm">
    <div class="v5a-chathead">从「${pEsc(filename)}」识别出 ${fields.length} 个候选字段</div>
    <div class="v5a-cand">${rows}</div>${notes}
    <div class="v5a-cfoot">
      <button type="button" class="linkbtn" data-all>全选</button>
      <span style="flex:1"></span>
      <button type="button" class="btn ghost sm" data-cancel>取消</button>
      <button type="button" class="btn primary sm" data-ok></button>
    </div></div>`;
  const ok = chat.querySelector("[data-ok]");
  const picked = () => Array.from(chat.querySelectorAll("[data-ci]:checked")).length;
  const paint = () => {
    ok.textContent = `导入勾选的 ${picked()} 个`;
    ok.disabled = picked() === 0;
  };
  chat.querySelectorAll("[data-ci]").forEach((cb) => { cb.onchange = paint; });
  chat.querySelector("[data-all]").onclick = () => {
    const boxes = Array.from(chat.querySelectorAll("[data-ci]"));
    const turnOn = boxes.some((cb) => !cb.checked);
    boxes.forEach((cb) => { cb.checked = turnOn; });
    paint();
  };
  chat.querySelector("[data-cancel]").onclick = () => pRailDefault();
  ok.onclick = () => {
    const list = [];
    chat.querySelectorAll("[data-ci]:checked").forEach((cb) => {
      const f = fields[Number(cb.dataset.ci)];
      if (f) list.push({ ...pEmptyField(), ...f, src: "excel" });
    });
    if (!list.length) return;
    P.fields = P.fields.filter((f) => String(f.code || "").trim()).concat(list);
    pRailDefault();
    pRenderFields(); pHint();
    pFlashRows(list.map((_, k) => P.fields.length - list.length + k));
    toast(`已导入 ${list.length} 个字段到草稿`, 4000);
  };
  paint();
}

async function pFreeImport(file) {
  if (!file) return;
  const chat = pId("v5aChat");
  if (!chat) return;
  chat.innerHTML = `<div class="v5a-parsing"><div class="spinner"></div>
    <span>正在读「${pEsc(file.name)}」并识别字段…</span></div>`;
  try {
    const res = await fetch("/api/patch/fields/free?name=" + encodeURIComponent(file.name), {
      method: "POST",
      body: await file.arrayBuffer(),
      headers: { "X-Filename": encodeURIComponent(file.name) },
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) throw new Error(data.error || `识别失败 ${res.status}`);
    pShowCandidates(data, file.name);
  } catch (err) {
    pRailDefault();
    pChatMsg("ai warn", `没能识别这份 Excel：${pEsc(err.message)}<br>
      可以改用「有标准模板？直接导入」，或从 Excel 粘贴。`);
    toast("识别失败，已给出替代路径", 6000);
  }
}

function pApplyAi(data) {
  const removed = new Set(data.remove || []);
  if (removed.size) {
    P.fields = P.fields.filter((f) => !removed.has(String(f.code || "").trim()));
  }
  (data.modify || []).forEach((patch) => {
    const f = P.fields.find((x) => x.code === patch.code);
    if (f) Object.assign(f, patch);
  });
  const added = [];
  (data.add || []).forEach((item) => {
    if (!item || !item.code) return;
    if (P.fields.some((f) => f.code === item.code)) return;
    P.fields.push({ ...pEmptyField(), ...item, src: "ai" });
    added.push(P.fields.length - 1);
  });
  pRenderFields(); pHint();
  if (added.length) pFlashRows(added);
  return added;
}

async function pChatSend() {
  const ta = pId("v5aText");
  const btn = pId("v5aSend");
  const message = (ta && ta.value ? ta.value : "").trim();
  if (!message) { if (ta) ta.focus(); return; }
  if (!pLlmReady) { toast("还没配置大模型：去系统设置里填一个平台和 Key", 6000); return; }
  ta.value = "";
  pChatMsg("user", pEsc(message));
  btn.disabled = true;
  btn.textContent = "思考中…";
  P.chatHistory = P.chatHistory || [];
  try {
    const res = await fetch("/api/ai/patch-fields", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        draft: P.fields, existing: pExistingFields(),
        history: P.chatHistory.slice(-6), message,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) throw new Error(data.error || `调整失败 ${res.status}`);
    const before = P.fields.length;
    const added = pApplyAi(data);
    const addedNames = added.map((i) => P.fields[i] && P.fields[i].name).filter(Boolean);
    P.chatHistory.push({ role: "user", content: message });
    P.chatHistory.push({ role: "assistant", content: data.reply || "" });
    const chips = added.map((i) => ({ text: `看这一行`, row: i }));
    let html = pEsc(data.reply || "已按你的要求调整草稿");
    if (addedNames.length) html += `<br><span class="hint">已加入：${pEsc(addedNames.join("、"))}</span>`;
    if ((data.remove || []).length) {
      html += `<br><span class="hint">已从草稿去掉：${pEsc(data.remove.join("、"))}</span>`;
    }
    if (data.refused) pChatMsg("ai warn", html, chips);
    else pChatMsg("ai", html, chips);
  } catch (err) {
    pChatMsg("ai warn", `没调整成功：${pEsc(err.message)}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "添加";
    if (ta) ta.focus();
  }
}

/* ---------------- ④ 出包 ---------------- */

async function pPreview(btn) {
  pBusy(btn, true, "正在预览…");
  try {
    const res = await pApi("/api/patch/dry-run", pSpec());
    const data = await res.json();
    const box = pId("patchReport");
    box.hidden = false;
    box.textContent = data.report || JSON.stringify(data, null, 2);
    toast("预览完成，确认无误后下载", 4000);
  } catch (err) { pFail(err); } finally { pBusy(btn, false); }
}

async function pBuild(btn) {
  const spec = (() => { try { return pSpec(); } catch (e) { pFail(e); return null; } })();
  if (!spec) return;
  const split = pId("patchSplit").checked;
  const jobs = split ? spec.targets.map((t) => [t]) : [spec.targets];
  pBusy(btn, true, "正在生成…");
  try {
    for (let i = 0; i < jobs.length; i += 1) {
      const one = { ...spec, targets: jobs[i] };
      // 分包时报告里的「等 N 个对象」不会误导：每次只带一个目标
      const res = await pApi("/api/patch/build", one);
      await pDownload(res, "pp6-加字段.zip");
      if (jobs.length > 1) await new Promise((r) => setTimeout(r, 350));
    }
    pId("patchReport").hidden = false;
    pId("patchReport").textContent =
      `已生成 ${jobs.length} 份新 zip。\n` +
      `原包 ${P.fileName} 未改动；包里除 bo/*.xml 外的内容均按原字节保留。\n` +
      `导入方式：系统管理 → 发布管理 → 导入同步包，直接上传新 zip。\n` +
      `本包对齐 CONFIG_SYNC_NOTE: 注释格式（发布说明写在 zip comment 里）。`;
    toast(`已生成 ${jobs.length} 个导入包`, 5000);
  } catch (err) { pFail(err); } finally { pBusy(btn, false); }
}

async function pWorkbook(btn) {
  pBusy(btn, true, "正在生成…");
  try {
    const res = await pApi("/api/patch/workbook", pSpec());
    await pDownload(res, "pp6-加字段-规格表.xlsx");
    toast("规格表已下载；补齐表单、列表后可回到主流程出包", 5000);
  } catch (err) { pFail(err); } finally { pBusy(btn, false); }
}

async function pTemplate(btn) {
  pBusy(btn, true, "正在生成…");
  try {
    const res = await fetch("/api/patch/fields-template");
    if (!res.ok) throw new Error(`下载失败 ${res.status}`);
    await pDownload(res, "PP6-新增字段表.xlsx");
  } catch (err) { pFail(err); } finally { pBusy(btn, false); }
}

/* ---------------- P0-2：包结构表 + 空态（后端零改动，全部消费 P.snap） ----------------
   复用 app.js 的 humanSize()/toast()/saveBlob()，不重复定义；
   复用既有 .card / .card-head / .tbl-wrap / .set-note / .patch-tag.warn / .chip.warn。 */

function pRenderPackageStruct() {
  const box = pId("pp6zipPatchStruct");
  if (!box || !P.snap) return;
  const entries = Array.isArray(P.snap.entries) ? P.snap.entries : [];
  const groups = {};
  entries.forEach((e) => {
    const k = e.kind || "(root)";
    (groups[k] = groups[k] || []).push(e);
  });

  // 同一对象重复推送过：按 code 去重后给一个「包内 N 份副本」chip
  const seen = new Set();
  const copies = (P.snap.objects || []).filter((o) => {
    if (Number(o.copies || 0) <= 1) return false;
    const key = String(o.code || o.name || o.file);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  const copyChips = copies.length
    ? `<div class="set-note"><strong>重复副本</strong>：${copies.map((o) =>
        `<span class="patch-tag warn">${pEsc(o.name || o.code || o.file)} · 包内 ${Number(o.copies)} 份副本</span>`).join(" ")}
        <span class="muted">（重复推送不去重；默认只改选中的一份，可勾「同一对象的重复副本一起改」）</span></div>`
    : "";

  const seqOf = (name) => {
    const m = /(\d{3})-/.exec(String(name || "").split("/").pop() || "");
    return m ? m[1] : "—";
  };
  const groupHtml = Object.keys(groups).map((k) => {
    const rows = groups[k].map((e) => `
      <tr><td><code>${pEsc(e.name)}</code></td>
          <td>${pEsc(seqOf(e.name))}</td>
          <td>${pEsc(humanSize(Number(e.size || 0)))}</td>
          <td>${pEsc(e.kind || "")}${k === "dataset"
            ? ' <span class="chip warn">数据菜单手建数据集形态，本工具不产出</span>' : ""}</td></tr>`).join("");
    return `<details open>
      <summary><code>${pEsc(k)}/</code> · ${groups[k].length} 条</summary>
      <div class="tbl-wrap"><table>
        <thead><tr><th>条目名</th><th>序号</th><th>大小</th><th>目录类型 / kind</th></tr></thead>
        <tbody>${rows}</tbody></table></div></details>`;
  }).join("");

  const staticNotes = [
    "包内只允许 bo/ form/ data_template/ 三类裸 XML（dataset/ 为数据菜单手建数据集特例）。",
    "多放 manifest / catalog / txt / 图标 / 说明文件 → 线上报「不支持的同步对象类型」。",
    "外层再套一层文件夹或平铺多个 zip → 线上报「同步包文件缺少业务类型目录」。",
  ].map((n) => `<div>· ${pEsc(n)}</div>`).join("");

  box.hidden = false;
  // 包结构是核对用的旁证，不是改字段的主路径：默认收起，别把「选择对象」挤到折叠线下。
  box.innerHTML = `
    <details class="zip-struct">
      <summary><b>包结构</b><span class="hint">${entries.length} 条 · ${pEsc(P.fileName || "")}</span></summary>
      <div class="card">
        ${groupHtml || '<div class="empty">包内没有可列出的条目。</div>'}
        ${P.snap.comment ? `<div class="set-note"><strong>comment 原文</strong>：<code>${pEsc(P.snap.comment)}</code></div>` : ""}
        ${copyChips}
        <div class="set-note">${staticNotes}</div>
      </div>
    </details>`;
}

function pEmptyState() {
  const empty = !P.snap || (P.snap.objects || []).length === 0;
  ["patchWorkbook", "patchDryRun", "patchBuild"].forEach((id) => {
    const b = pId(id);
    if (b) b.disabled = empty;
  });
  const notes = pId("patchNotes");
  if (!notes) return;
  if (empty) {
    notes.hidden = false;                    // 高亮：空态强制显形
    const tip = "包里没有可加字段的 bo/*.xml → 改用工作区 1 反解后重新出包。"
      + " 多余条目会触发「不支持的同步对象类型」，外层套目录会触发「同步包文件缺少业务类型目录」。";
    if (notes.innerHTML.indexOf("改用工作区 1 反解后重新出包") < 0) {
      notes.innerHTML += `<div class="patch-tag warn">${pEsc(tip)}</div>`;
    }
  }
}

/* ---------------- 绑定 ---------------- */

function pBind() {
  const zipInput = pId("patchZipInput");
  // 选文件守卫必须和普通工作区是同一把锁（app.js 暴露的 window.PP6_PICK）：
  // 两处各自弹一个原生面板，就是用户看到的「同时两个选择窗口」。
  const pickGuard = () => window.PP6_PICK || null;
  pId("patchPickZip").onclick = async () => {
    if (typeof native === "function" && native()) {
      const guard = pickGuard();
      if (guard && !guard.begin()) return;      // 别处正开着面板：不再叠一个
      let file = null;
      try {
        file = await pickFile("zip");
      } finally {
        if (guard) guard.end();                 // 只锁对话框，读完包还能再选
      }
      // 取消 / 失败以前是静默 return，用户以为程序卡住
      if (!file) { toast("未选择文件"); return; }
      await pUploadZip(file);
      return;
    }
    const guard = pickGuard();
    if (guard) guard.viaInput(zipInput);
    else zipInput.click();
  };
  zipInput.onchange = (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (file) pUploadZip(file);
  };
  const drop = pId("patchDrop");
  drop.addEventListener("dragover", (e) => {
    e.preventDefault(); drop.classList.add("dragging");
  });
  drop.addEventListener("dragleave", () => drop.classList.remove("dragging"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault(); drop.classList.remove("dragging");
    const file = (e.dataTransfer.files || [])[0];
    if (file) pUploadZip(file);
  });

  // 「重名字段」下拉已随「已有字段只改名」一起下线：模式固定 rename，
  // 这里不再有需要监听的模式控件。

  pId("patchObjAll").onclick = () => {
    P.selected = new Set(P.snap.objects.map((o) => o.file));
    pRenderObjects(); pRenderExisting(); pHint();
  };
  pId("patchObjNone").onclick = () => {
    P.selected = new Set(); pRenderObjects(); pRenderExisting(); pHint();
  };
  const reset = pId("patchExistingReset");
  if (reset) reset.onclick = () => {
    P.renames = {};
    pRenderExisting(); pHint();
    toast("已还原全部名称修改", 3000);
  };

  pId("patchAddRow").onclick = () => {
    P.fields.push(pEmptyField());
    pMoreOpen = new Set([P.fields.length - 1]);   // 新行直接展开，长度/说明就在眼前
    pRenderFields(); pHint();
  };
  pId("patchPasteAdd").onclick = () => {
    const rows = pParsePaste(pId("patchPaste").value)
      .map((r) => ({ ...r, src: "manual" }));
    if (!rows.length) { toast("未解析出字段行，请检查列顺序"); return; }
    P.fields = P.fields.filter((f) => String(f.code || "").trim()).concat(rows);
    pId("patchPaste").value = "";
    pRenderFields(); pHint();
    toast(`已追加 ${rows.length} 个字段`, 3500);
  };
  pId("patchPasteToggle").onclick = () => {
    const box = pId("patchPasteBox");
    if (box) box.open = true;
    const ta = pId("patchPaste");
    if (ta) ta.focus();
  };
  pId("patchPickXlsx").onclick = () => pId("patchXlsxInput").click();
  pId("patchXlsxInput").onchange = (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (file) pFreeImport(file);
  };
  // 模板路径保持原样：导入仍走 /api/patch/fields，按钮挪到右栏
  pId("patchTpl").onclick = () => pId("patchTplInput").click();
  pId("patchTplInput").onchange = (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (file) pImportFields(file);
  };
  pId("patchTplDownload").onclick = (e) => pTemplate(e.currentTarget);
  pId("patchDryRun").onclick = (e) => pPreview(e.currentTarget);
  pId("patchBuild").onclick = (e) => pBuild(e.currentTarget);
  pId("patchWorkbook").onclick = (e) => pWorkbook(e.currentTarget);
  // 对齐分析：勾了「关联项目」后自动跑（pRenderObjects 里触发），这里给个手动重算入口
  const reAlign = pId("btnPatchAlign");
  if (reAlign) reAlign.onclick = () => pAlign();

  // 修改字段的两个页签：改名 / 新增
  const editTabs = pId("patchEditTabs");
  if (editTabs) {
    editTabs.querySelectorAll(".tab").forEach((tab) => {
      tab.onclick = () => {
        editTabs.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
        pApplyEditTabs();
      };
    });
  }

  // 整张「新增字段」卡都是接收区：拖 Excel 进来走自由导入（与 v4 文件入口同一套）
  const fieldCard = pId("patchFieldsCard");
  const fieldVeil = fieldCard && fieldCard.querySelector(".drop-veil");
  if (fieldCard && fieldVeil) {
    let depth = 0;
    const hasFiles = (e) => e.dataTransfer
      && Array.from(e.dataTransfer.types || []).includes("Files");
    fieldCard.addEventListener("dragenter", (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      if (++depth === 1) fieldVeil.hidden = false;
    });
    fieldCard.addEventListener("dragover", (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
    });
    fieldCard.addEventListener("dragleave", () => {
      if (depth > 0 && --depth === 0) fieldVeil.hidden = true;
    });
    fieldCard.addEventListener("drop", (e) => {
      e.preventDefault();
      depth = 0;
      fieldVeil.hidden = true;
      const file = (e.dataTransfer.files || [])[0];
      if (file) pFreeImport(file);
    });
  }

  // 左侧纵向步骤：点到哪滚到哪，锁住的步骤不响应
  document.querySelectorAll(".vstep[data-vstep]").forEach((btn) => {
    btn.onclick = () => {
      if (btn.classList.contains("lock")) return;
      const map = {
        1: "patchUploadCard", 2: "patchObjectsCard",
        3: "patchEditCard", 4: "patchBuildCard",
      };
      const target = pId(map[btn.dataset.vstep]);
      if (!target || target.hidden) return;
      target.scrollIntoView({ behavior: "smooth", block: "start" });
    };
  });
}

pBind();
pRenderFields();
pRailDefault();
pLoadLlm();

/* 首屏：左栏先把「上传原包」标成当前步，后面三步锁住；
   上传成功后再由 pUploadZip → pPaintVSteps 更新。 */
pPaintVSteps();
