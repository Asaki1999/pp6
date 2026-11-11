const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const TYPE_LABEL = {
  varchar: "单行文本",
  clob: "多行文本",
  number: "数字",
  datetime: "日期时间",
  user: "人员",
  department: "部门",
  object: "关联对象",
  dic: "字典",
  autoNumber: "自动编号",
  userGroupCate: "用户组",
};

const KIND_LABEL = {
  create: "新建表单", detail: "详情表单", condition: "条件专用", import: "导入专用", bundle: "标准全套",
};

const CTRL_LABEL = {
  text: "文本框",
  editor: "富文本",
  select: "下拉选择",
  // 字典控件在系统里的真实名字是 dictionary（不是 select），
  // 少这一条预览就会直接露出英文
  dictionary: "数据字典",
  linkdata: "关联数据",
  dropSelector: "人员选择",
  dropSelectOrg: "部门选择",
  autoNumber: "自动编号",
  attachment: "附件",
  number: "数字",
  date: "日期",
  // datetime 字段线上用的是 datePicker，date 仍保留给老的 date 类型
  datePicker: "日期选择",
  // clob 字段
  textarea: "多行文本",
  hidden: "隐藏",
};

const state = { file: null, data: null, formIndex: 0, desktop: false, picked: new Set(), imported: new Set(), aiData: null, aiFile: null };

/* 桌面壳（pywebview）会注入 window.pywebview.api，同时置 PP6_DESKTOP 供自检读取 */
const native = () => (window.pywebview && window.pywebview.api) || null;

function detectDesktop() {
  if (native()) {
    state.desktop = true;
    window.PP6_DESKTOP = true;
  }
}
window.addEventListener("pywebviewready", detectDesktop);
detectDesktop();

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function toast(msg, ms = 3200) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.hidden = true; }, ms);
}

function humanSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / 1024 / 1024).toFixed(1) + " MB";
}

function setLoading(on, text = "正在解析…") {
  $("#loading").hidden = !on;
  if (text) $("#loadingText").textContent = text;
}

/* ------------------------------------------------ 上传 */

async function handleFile(file, { fromAi = false } = {}) {
  if (!file) return;
  if (!/\.(xlsx|xlsm)$/i.test(file.name)) {
    toast("请选择 .xlsx 文件");
    return;
  }
  state.file = file;
  setLoading(true);
  try {
    const buf = await file.arrayBuffer();
    const res = await fetch("/api/parse", { method: "POST", body: buf });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "解析失败");
    state.data = data;
    renderAll();
    // 直接从 AI 预览下来的 Excel → 进第 3 步；手动传的也进第 3 步，
    // 因为「结果」区里就是模块勾选 + 下载，这是流程的终点
    goStep(3);
    if (fromAi) toast("已按预览生成，选择模块后下载 zip", 5000);
  } catch (err) {
    toast("解析失败：" + err.message);
    goStep(1);
  } finally {
    setLoading(false);
  }
}

async function loadDemo() {
  try {
    const res = await fetch("/api/template");
    const blob = await res.blob();
    await handleFile(new File([blob], "示例-采购申请.xlsx", {
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }));
  } catch (err) {
    toast("载入示例失败：" + err.message);
  }
}

/* ------------------------------------------------ 渲染 */

function renderAll() {
  const d = state.data;
  $("#result").hidden = false;
  window.__importHasResult = true;
  // 旧版这里是 #actionbar / #dropzone 显隐，现在改成步骤条控制：
  // 出包区由 goStep(3) 负责显示，输入区自然隐藏
  $("#fileName").textContent = state.file.name;
  $("#fileSize").textContent = humanSize(state.file.size);

  const stats = [
    ["业务对象", d.stats["业务对象"] || 0],
    ["字段", d.stats["字段"] || 0],
    ["表单", d.stats["表单"] || 0],
    ["表单控件", d.stats["表单控件"] || 0],
    ["列表", d.stats["列表"] || 0],
    ["数据集", d.stats["数据集"] || 0],
  ];
  $("#stats").innerHTML = stats.map(([k, n]) => `
    <div class="stat ${n ? "" : "zero"}"><div class="n">${n}</div><div class="k">${k}</div></div>
  `).join("");

  const errCount = d.errorCount;
  const warnCount = d.warnCount;
  const banner = $("#banner");
  if (errCount) {
    banner.className = "banner error";
    banner.innerHTML = `<strong>${errCount} 个错误</strong>：必须先修好才能生成，详见「校验结果」。`;
    banner.hidden = false;
  } else if (warnCount) {
    banner.className = "banner warn";
    banner.innerHTML = `<strong>${warnCount} 个警告</strong>：不影响生成，建议核对一下。`;
    banner.hidden = false;
  } else {
    banner.className = "banner ok";
    banner.innerHTML = `<strong>校验通过</strong>：结构完整，可以生成。`;
    banner.hidden = false;
  }

  const badge = $("#issueBadge");
  badge.textContent = errCount + warnCount;
  badge.className = "badge" + (errCount ? " has-error" : warnCount ? " has-warn" : "");

  // 字段体检的角标：只表示「有几处要你核对」，不是错误，所以不标红
  const fBadge = $("#fieldBadge");
  if (fBadge) {
    const fcAll = (d.fieldChecks || []).length;
    const fcWarn = d.fieldCheckWarn || 0;
    fBadge.textContent = fcAll;
    fBadge.className = "badge" + (fcWarn ? " has-warn" : "");
  }

  renderObjects();
  renderRelations();
  renderFieldCheck();
  renderForms();
  renderLists();
  renderIssues();
  state.imported = new Set();
  state.picked = new Set(
    (d.modules || []).filter((m) => m.count > 0).map((m) => m.code)
  );
  renderModules();

  updateBar();
}

/* 底部按钮与提示随勾选变化 */
function updateBar() {
  const d = state.data;
  const mods = (d.modules || []).filter((m) => m.count > 0);
  const picked = mods.filter((m) => state.picked.has(m.code));
  const btn = $("#btnPack") || $("#btnBuild");
  if (btn) btn.disabled = d.errorCount > 0 || !picked.length;
  $("#barSummary").textContent =
    `${d.objects.length} 个对象 · ${d.forms.length} 个表单 · ${d.lists.length} 个列表 · ${d.datasets.length} 个数据集`;
  $("#barHint").textContent = d.errorCount
    ? "有错误，已禁用生成"
    : picked.length
      ? `已选 ${picked.length} 个模块（${picked.map((m) => m.label).join(" / ")}）· 也可在下面逐个导入`
      : "还没选模块";
  btn.textContent = picked.length === mods.length ? "导出全部模块" : `导出选中的 ${picked.length} 个模块`;
}

/* 所属关系确认：把后端 relationships view model 渲染成勾选区，并暴露决策给出包/导出 */
function renderRelations() {
  const d = state.data;
  const vm = d.relationships;
  const box = $("#relBox");
  const badge = $("#relBadge");
  const btn = $("#btnConfirmedExcel");
  const relCount = vm && vm.items
    ? vm.items.reduce((n, it) => n
        + (it.candidates || []).length
        + (it.excelRelation && it.excelRelation.specified ? 1 : 0), 0)
    : 0;
  if (badge) badge.textContent = relCount;
  if (box && window.Relations) {
    window.Relations.reset();
    window.Relations.render(box, vm, { scope: "parse" });
  }
  if (btn) btn.hidden = !relCount;
}

/* AI 预览页的关系确认（第 2 步），与解析页同一组件、不同 scope */
function renderAiRelations(d) {
  const box = $("#aiRelBox");
  const panel = $("#aiRelPanel");
  const vm = d && d.relationships;
  const relCount = vm && vm.items
    ? vm.items.reduce((n, it) => n
        + (it.candidates || []).length
        + (it.excelRelation && it.excelRelation.specified ? 1 : 0), 0)
    : 0;
  if (panel) panel.hidden = !relCount;
  if (box && window.Relations) {
    window.Relations.reset();
    window.Relations.render(box, vm, { scope: "ai" });
  }
}

/* 下载「确认版 Excel」：把勾选的关系写回父对象/关联字段/关系类型三列，作为新文件返回 */
async function downloadConfirmedExcel() {
  if (!state.file) return;
  try {
    const buf = await state.file.arrayBuffer();
    const headers = { "Content-Type": "application/octet-stream" };
    const relHeader = window.Relations ? window.Relations.decisionsHeader("parse") : null;
    if (relHeader) headers["X-Relations"] = relHeader;
    const res = await fetch("/api/confirmed-excel?name=" + encodeURIComponent(state.file.name), {
      method: "POST", body: buf, headers,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || "生成失败");
    }
    const blob = await res.blob();
    const name = decodeURIComponent(res.headers.get("X-Filename") || "已确认.xlsx");
    if (await saveBlob(blob, name)) {
      toast(`已下载 ${name}：下次用它继续，勾选的关系不会丢`, 5000);
    }
  } catch (err) {
    toast("下载确认版 Excel 失败：" + err.message, 6000);
  }
}

/* 按模块导入 / 导出：系统里每个模块单独导入，所以左边一个开关、右边两个动作 */
function renderModules() {
  const box = $("#modList");
  const mods = state.data.modules || [];
  if (!mods.length) { box.innerHTML = ""; return; }
  mods.forEach((m) => { MOD_LABEL[m.code] = m.label; });
  box.innerHTML = mods.map((m) => {
    const empty = !m.count;
    const need = (m.needs || []).filter((n) => !state.imported.has(n));
    // 只提示不拦截：对象可能早就在系统里导过了，禁掉反而挡住正常操作
    const hint = !empty && need.length > 0;
    const done = state.imported.has(m.code);
    const picked = state.picked.has(m.code);
    const flowHint = m.code === "workflow";
    const badges = [
      empty ? '<span class="chip">无内容</span>' : "",
      done ? '<span class="chip accent">已导入</span>' : "",
      hint ? `<span class="chip warn">建议先导入${need.map((n) => MOD_LABEL[n] || n).join("、")}</span>` : "",
    ].join("");
    return `
      <div class="mod-row ${empty ? "is-empty" : ""}">
        <label class="mod-pick">
          <input type="checkbox" data-mod="${m.code}" ${picked ? "checked" : ""} ${empty ? "disabled" : ""}>
        </label>
        <div class="mod-idx">${m.order}</div>
        <div class="mod-main">
          <div class="mod-name">
            ${esc(m.label)}
            <span class="chip">${m.count} 项</span>
            ${badges}
          </div>
          <div class="mod-menu">${esc(m.menu)}${m.folder ? ` · 目录 <code>${esc(m.folder)}/</code>` : ""}${m.zipped ? "" : " · 裸 XML"}</div>
          ${m.items.length
            ? `<div class="mod-items">${m.items.slice(0, 6).map((x) => `<span class="chip">${esc(x)}</span>`).join("")}${m.items.length > 6 ? `<span class="muted">等 ${m.items.length} 项</span>` : ""}</div>`
            : m.note ? `<div class="mod-note">${esc(m.note)}</div>` : ""}
        </div>
        <div class="mod-btns">
          <button class="btn ghost sm" data-act="import" data-mod="${m.code}"
                  ${empty || flowHint ? "disabled" : ""}
                  title="单独生成这一个模块的 zip 并保存">导入</button>
          <button class="btn ghost sm" data-act="export" data-mod="${m.code}"
                  ${empty || flowHint ? "disabled" : ""}
                  title="只导出这一个模块的 zip">导出</button>
        </div>
      </div>`;
  }).join("");

  $$("#modList input[data-mod]").forEach((cb) => {
    cb.onchange = () => {
      if (cb.checked) state.picked.add(cb.dataset.mod);
      else state.picked.delete(cb.dataset.mod);
      updateBar();
    };
  });
  $$("#modList button[data-act]").forEach((btn) => {
    btn.onclick = () => {
      const code = btn.dataset.mod;
      if (btn.dataset.act === "import") importOne(code, btn);
      else buildZip([code], btn);
    };
  });
}

const MOD_LABEL = {};

function renderObjects() {
  const panel = $('.panel[data-panel="objects"]');
  const objects = state.data.objects;
  if (!objects.length) {
    panel.innerHTML = `<div class="card"><div class="empty">还没有业务对象，请先在 1_业务对象 表里填写。</div></div>`;
    return;
  }
  panel.innerHTML = objects.map((bo) => `
    <div class="card">
      <div class="card-head">
        <div>
          <h3>${esc(bo.name)} <span class="chip accent">${esc(bo.code)}</span></h3>
          <div class="sub">表名 ${esc(bo.tbl)} · ${bo.fields.length} 个字段${bo.withLog ? " · 记录变更日志" : ""}</div>
        </div>
        <div class="sub">${esc(bo.moduleId || "未指定模块")}</div>
      </div>
      <div class="tbl-wrap">
        <table>
          <thead><tr>
            <th>字段名称</th><th>字段编码</th><th>物理列名</th><th>类型</th><th>长度</th><th>备注</th>
          </tr></thead>
          <tbody>
            ${bo.fields.map((f) => `
              <tr>
                <td>${esc(f.name)}</td>
                <td><code>${esc(f.code)}</code></td>
                <td><code>${esc(f.fieldName)}</code></td>
                <td>${esc(TYPE_LABEL[f.type] || f.type)}</td>
                <td>${f.length ?? "—"}${f.precision ? `,${f.precision}` : ""}</td>
                <td class="sub">${[
                  f.index ? "索引" : "",
                  f.objectType ? "关联 " + f.objectType : "",
                ].filter(Boolean).map(esc).join(" / ") || "—"}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    </div>
  `).join("");
}

function renderForms() {
  const panel = $('.panel[data-panel="forms"]');
  const forms = state.data.forms;
  if (!forms.length) {
    panel.innerHTML = `<div class="card"><div class="empty">还没有表单，请先在 3_表单 与 4_表单控件 里填写。</div></div>`;
    return;
  }
  if (state.formIndex >= forms.length) state.formIndex = 0;
  const form = forms[state.formIndex];

  // 系统隐藏控件（主键 / 版本 / 拼音）不画在预览图纸上：
  // 它们对填写者本来就不可见，出包时仍按官方表单结构写进 XML（见 gen_form._HIDDEN_FIELDS）。
  panel.innerHTML = `
    <div class="form-picker">
      ${forms.map((f, i) => `<button data-idx="${i}" class="${i === state.formIndex ? "active" : ""}">${esc(f.name)}</button>`).join("")}
    </div>
    <div class="paper">
      <div class="paper-head">
        <h3>${esc(form.name)}</h3>
        <span class="chip">${esc(form.key)}</span>
        <span class="sub muted">${esc(KIND_LABEL[form.kind] || "")} · ${form.layout === 1 ? "一行一列" : "一行两列"} · 标签宽 ${form.labelWidth}px · 面板 ${form.width}% · 绑定 ${esc(form.boName || form.boCode || "未绑定")} · ${form.fieldCount} 个控件</span>
      </div>
      ${(form.groups || [{ title: "", rows: form.rows || [], count: form.fieldCount }]).map((g) => `
        <div class="fgroup">
          ${g.title ? `<div class="fgroup-head">${esc(g.title)}<span class="fgroup-cnt">${g.count || 0} 项</span></div>` : ""}
          ${g.rows.map((row) => `
            <div class="frow">
              ${row.map((cell) => `
                <div class="fcell" style="flex:${cell.span}">
                  <div class="flabel">${cell.required ? '<span class="req">*</span>' : ""}${esc(cell.label)}</div>
                  <div class="fctrl">
                    ${controlHtml(cell)}
                    <div class="ftype">${esc(CTRL_LABEL[cell.type] || cell.type)}${cell.valueSource ? " · " + esc(cell.valueSource) : ""}${cell.missing ? ' · <span class="miss">字段不存在</span>' : ""}</div>
                  </div>
                </div>
              `).join("")}
            </div>
          `).join("")}
        </div>
      `).join("")}
    </div>
  `;

  $$(".form-picker button", panel).forEach((btn) => {
    btn.onclick = () => {
      state.formIndex = Number(btn.dataset.idx);
      renderForms();
    };
  });
}

function controlHtml(cell) {
  if (cell.type === "editor") return `<div class="mock area">请输入…</div>`;
  if (cell.type === "attachment") return `<div class="mock dashed">点击上传附件</div>`;
  if (cell.type === "autoNumber") return `<div class="mock disabled">保存时自动生成</div>`;
  if (["select", "linkdata", "dropSelector", "dropSelectOrg"].includes(cell.type)) {
    const hint = cell.type === "dropSelector" ? "请选择人员" : cell.type === "dropSelectOrg" ? "请选择部门" : "请选择";
    return `<div class="mock select">${hint}</div>`;
  }
  return `<div class="mock"><span class="fill"></span><span>请输入</span></div>`;
}

function renderLists() {
  const panel = $('.panel[data-panel="lists"]');
  const { lists, datasets } = state.data;
  const listCards = lists.length
    ? lists.map((l) => `
      <div class="card">
        <div class="card-head">
          <div>
            <h3>${esc(l.name)} <span class="chip accent">${esc(l.key)}</span></h3>
            <div class="sub">对象 ${esc(l.boCode)}${l.sort ? " · 排序 " + esc(l.sort) : ""}${l.tree ? " · 树形" : ""}</div>
          </div>
          <div>${l.views.map((v) => `<span class="chip">${esc(v)}</span>`).join(" ")}</div>
        </div>
        <div style="padding:14px 18px;display:flex;gap:6px;flex-wrap:wrap;">
          ${l.columns.length
            ? l.columns.map((c) => `<span class="chip">${esc(c)}</span>`).join("")
            : '<span class="muted">未指定显示列，将使用全部字段</span>'}
        </div>
      </div>`).join("")
    : `<div class="card"><div class="empty">还没有列表配置。</div></div>`;

  const dsCards = datasets.length
    ? `<div class="card">
        <div class="card-head"><div><h3>数据集 / 值来源</h3><div class="sub">下拉框的取数来源</div></div></div>
        <table><thead><tr><th>名称</th><th>Key</th><th>主表</th><th>类型</th></tr></thead>
        <tbody>${datasets.map((d) => `<tr>
          <td>${esc(d.name)}</td><td><code>${esc(d.key)}</code></td>
          <td><code>${esc(d.table)}</code></td><td>${esc(d.type)}</td></tr>`).join("")}
        </tbody></table></div>`
    : "";

  panel.innerHTML = listCards + dsCards;
}

function renderIssues() {
  const panel = $('.panel[data-panel="issues"]');
  const issues = state.data.issues;
  if (!issues.length) {
    panel.innerHTML = `<div class="card"><div class="empty">没有任何问题，可以直接生成。</div></div>`;
    return;
  }
  const order = { error: 0, warn: 1 };
  const sorted = [...issues].sort((a, b) => (order[a.level] ?? 2) - (order[b.level] ?? 2));
  panel.innerHTML = `<div class="card">${sorted.map((i) => `
    <div class="issue ${esc(i.level)}">
      <div class="lv">${i.level === "error" ? "错误" : "警告"}</div>
      <div>
        <div class="where">${esc(i.where)}</div>
        <div>${esc(i.message)}</div>
      </div>
    </div>`).join("")}</div>`;
}

/* 字段体检：跨对象/跨表单的疑似重复与冲突。只提示，不拦出包 */
const FIELD_KIND = {
  "near-name": "名字很像",
  "name-type": "同名但类型不一致",
  "code-type": "同字段但类型不一致",
  option: "候选值不一致",
  "form-type": "表单控件类型不一致",
};

function renderFieldCheck() {
  const panel = $('.panel[data-panel="fields"]');
  const list = state.data.fieldChecks || [];
  if (!list.length) {
    panel.innerHTML = `<div class="card"><div class="empty">
      没有发现疑似重复或冲突的字段。</div></div>`;
    return;
  }
  const groups = {};
  list.forEach((f) => { (groups[f.kind] = groups[f.kind] || []).push(f); });

  const body = Object.keys(groups).map((kind) => {
    const items = groups[kind];
    return `<div class="card">
      <div class="card-head">
        <div>
          <h3>${esc(FIELD_KIND[kind] || kind)}</h3>
          <div class="sub">${items.length} 条，每条下面列了它出现在哪几个地方</div>
        </div>
      </div>
      ${items.map((f) => `
        <div class="issue ${esc(f.level)}">
          <div class="lv">${f.level === "warn" ? "疑似" : "提示"}</div>
          <div>
            <div class="fc-title">${esc(f.title)}</div>
            <div>${esc(f.detail)}</div>
            <ul class="fc-places">${(f.places || []).map((p) => `<li>${esc(p)}</li>`).join("")}</ul>
          </div>
        </div>`).join("")}
    </div>`;
  }).join("");

  panel.innerHTML = `<div class="card fc-intro"><div class="sub">
    同名同义写了两遍、或者同一个名字配了不同东西的，都列在这里。
    这些<b>不影响出包</b>，改哪个由你判断——两个名字是不是同一件东西，工具不敢替你定。
  </div></div>${body}`;
}

/* ------------------------------------------------ 下载 */

function b64ToFile(b64, filename, mime) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
  return new File([bytes], filename, { type: mime });
}

async function blobToB64(blob) {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let bin = "";
  const step = 0x8000;
  for (let i = 0; i < bytes.length; i += step) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + step));
  }
  return btoa(bin);
}

/* 保存：桌面壳用系统保存对话框；浏览器用 a[download] */
async function saveBlob(blob, filename) {
  const api = native();
  if (api) {
    try {
      const res = await api.save_file(filename, await blobToB64(blob));
      if (res && res.path) toast("已保存到 " + res.path, 6000);
      return true;
    } catch (err) {
      toast("保存失败：" + err, 6000);
      return false;
    }
  }
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
  return true;
}

/* 选文件：桌面壳用系统打开对话框；浏览器用 input[type=file] */
async function pickFile(kind) {
  const api = native();
  if (!api) return null;
  const isZip = kind === "zip";
  const picked = await (isZip ? api.pick_zip() : api.pick_excel());
  if (!picked) return null;
  const mime = isZip ? "application/zip" : "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
  return b64ToFile(picked.data, picked.name, mime);
}

async function buildZip(codes, trigger) {
  if (!state.file) return;
  const list = codes || Array.from(state.picked);
  if (!list.length) { toast("先勾选要导出的模块"); return; }
  const btn = trigger || $("#btnPack") || $("#btnBuild");
  const label = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "正在生成…"; }
  try {
    const buf = await state.file.arrayBuffer();
    const headers = { "X-Modules": JSON.stringify(list) };
    const relHeader = window.Relations ? window.Relations.decisionsHeader("parse") : null;
    if (relHeader) headers["X-Relations"] = relHeader;
    const res = await fetch("/api/build", {
      method: "POST",
      body: buf,
      headers,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || "生成失败");
    }
    const blob = await res.blob();
    const name = decodeURIComponent(res.headers.get("X-Filename") || "pp6-import.zip");
    if (await saveBlob(blob, name)) {
      toast(list.length === 1
        ? `已导出「${MOD_LABEL[list[0]] || list[0]}」：${name}`
        : `已导出 ${list.length} 个模块：${name}`);
    }
  } catch (err) {
    toast("生成失败：" + err.message, 6000);
  } finally {
    btn.disabled = false;
    btn.textContent = label;
    updateBar();
  }
}

/* 单个模块导入：不打包汇总，直接给系统能吃的那个包 */
async function importOne(code, btn) {
  if (!state.file) return;
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "生成中…";
  try {
    const buf = await state.file.arrayBuffer();
    const res = await fetch(`/api/import/${code}`, { method: "POST", body: buf });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || "生成失败");
    }
    const blob = await res.blob();
    const name = decodeURIComponent(res.headers.get("X-Filename") || `${code}.zip`);
    if (await saveBlob(blob, name)) {
      state.imported.add(code);
      toast(`「${MOD_LABEL[code] || code}」已保存：${name}　→　拿到系统里 ${(state.data.modules.find((m) => m.code === code) || {}).menu || ""} 上传`);
      renderModules();
    }
  } catch (err) {
    toast("生成失败：" + err.message, 6000);
  } finally {
    btn.textContent = label;
    btn.disabled = false;
  }
}

async function inspectZip(file) {
  if (!file && state.desktop) file = await pickFile("zip");
  if (!file) return;
  if (!/\.zip$/i.test(file.name)) {
    toast("请选择 .zip 导出包");
    return;
  }
  setLoading(true, "正在反解导出包…");
  try {
    const buf = await file.arrayBuffer();
    const res = await fetch("/api/inspect", { method: "POST", body: buf });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || "反解失败");
    }
    const blob = await res.blob();
    const name = decodeURIComponent(res.headers.get("X-Filename") || "inspect.xlsx");
    if (await saveBlob(blob, name)) toast("已反解为 Excel：" + name);
  } catch (err) {
    toast("反解失败：" + err.message, 6000);
  } finally {
    setLoading(false);
    goStep(1);
  }
}

/* ------------------------------------------------ AI 生成业务对象 */

async function apiPost(path, payload) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const d = await res.json().catch(() => ({}));
  if (!res.ok || d.ok === false) throw new Error(d.error || "请求失败");
  return d;
}

function renderAiBo(d) {
  const bo = d.bo;
  state.aiData = d;                   // 编辑时直接改这份，导出用它
  // 字段层 14 种类型，叫法照系统「添加字段 › 字段类型」下拉
  const types = d.types && d.types.length ? d.types : [
    { code: "varchar", label: "字符串" }, { code: "number", label: "数字" },
    { code: "datetime", label: "日期时间" }, { code: "clob", label: "大文本" },
    { code: "user", label: "用户" }, { code: "department", label: "部门" },
    { code: "attachment", label: "附件列表" }, { code: "i18n", label: "国际化" },
    { code: "dic", label: "选项" }, { code: "object", label: "对象" },
    { code: "userGroupCate", label: "用户组" }, { code: "table", label: "表格" },
    { code: "autoNumber", label: "自动编号" }, { code: "formula", label: "公式" },
  ];
  const objs = d.objects || [];
  const objSet = new Set(objs.map((o) => o.code));
  const rows = bo.fields.map((f, i) => {
    const typeOpts = types.map((t) =>
      `<option value="${esc(t.code)}" ${t.code === f.type ? "selected" : ""}>${esc(t.label)}</option>`).join("");
    let rel;
    if (f.type === "dic") {
      // 选项字段：这里填候选值，不是对象编码
      // （字段层只有「选项」，没有「数据字典」——字典是表单控件的取数方式）
      rel = `<input class="ai-in" data-k="objectType" value="${esc((f.choices || []).join(","))}"
                 placeholder="选项值，逗号分隔" list="aiDicVals">`;
    } else if (f.type === "object") {
      // 关联对象：必须是系统里真实存在的对象编码，给下拉而不是自由输入
      const cur = (f.objectType || "").trim();
      const opts = [`<option value="">（选一个业务对象）</option>`]
        .concat(objs.map((o) =>
          `<option value="${esc(o.code)}" ${o.code === cur ? "selected" : ""}>${esc(o.code)}｜${esc(o.name)}</option>`));
      if (cur && !objSet.has(cur)) {
        opts.push(`<option value="${esc(cur)}" selected>⚠ ${esc(cur)}（对象库中不存在）</option>`);
      }
      rel = `<select class="ai-in ai-obj" data-k="objectType">${opts.join("")}</select>`;
    } else {
      rel = '<span class="muted">—</span>';
    }
    return `
    <tr data-fi="${i}" class="${f.type === "object" && f.objectType && !objSet.has(f.objectType) ? "bad" : ""}">
      <td><input class="ai-in" data-k="name" value="${esc(f.name)}"></td>
      <td><code>${esc(f.code)}</code></td>
      <td><code class="muted">${esc(f.fieldName)}</code></td>
      <td><select class="ai-in" data-k="type">${typeOpts}</select></td>
      <td><input class="ai-in ai-num" data-k="length" value="${f.length ?? ""}"></td>
      <td>${rel}</td>
      <td><button class="ai-del" data-del="${i}" title="删掉这个字段">×</button></td>
    </tr>`;
  }).join("");

  const notes = (d.notices || []).length
    ? `<div class="ai-notes">${d.notices.map((n) => `<div>· ${esc(n)}</div>`).join("")}</div>` : "";

  const steps = (d.steps || []).map((s) => s.error
    ? `<span class="ai-step bad">${esc(s.step)}失败</span>`
    : `<span class="ai-step">${esc(s.step)} ${s.seconds}s</span>`).join("");

  const fromSheet = d.sheets && d.sheets.length
    ? `<span class="chip">读自：${esc(d.sheets.join("、"))}</span>` : "";
  const objInfo = d.objectSource && d.objectSource.count
    ? `<span class="chip" title="关联对象（object 类型）只能选这里的编码，选错导入后是空下拉框">对象库 ${d.objectSource.count} 个 · ${esc(d.objectSource.source)}</span>`
    : "";
  // 系统里有同名对象：不再自动按「更新」导入（2026-11-11 起），必须显式确认。
  // AI 生成的对象默认是新建；要更新同名对象，下载 Excel 后在「已有对象ID」列填 id。
  const exist = d.existObject;
  const existChip = exist && exist.exists
    ? `<span class="chip warn" title="本次下载的 Excel 按「新建」导入，不会动系统里这个对象。若要更新它：下载 Excel 后在 1_业务对象 的「已有对象ID」列填 ${esc(exist.id || "该对象的 id")}">
         ⚠ 系统里已有同名对象 ${esc(exist.code)}（id ${esc(exist.id || "未知")}）· 将按「新建」导入</span>`
    : `<span class="chip" title="系统里没有同名对象，导入会新建一个">＋ 新对象</span>`;

  return `
    <div class="ai-card">
      <div class="ai-card-head">
        <div>
          <strong>${esc(bo.name)}</strong>
          <span class="chip accent">${esc(bo.code)}</span>
          <span class="muted">${bo.fields.length} 个字段</span>
          ${fromSheet}
          ${objInfo}
          ${existChip}
        </div>
        <div class="ai-steps">${steps}<span class="muted">共 ${d.seconds}s</span></div>
      </div>

      <div class="ai-sheetpick" id="aiSheetPick" hidden></div>

      <div class="ai-tabs" id="aiTabs">
        <button class="ai-tab active" data-ai="obj">业务对象</button>
        ${d.form ? '<button class="ai-tab" data-ai="form">表单布局</button>' : ""}
        ${d.list ? '<button class="ai-tab" data-ai="list">列表视图</button>' : ""}
      </div>

      <div class="ai-panel" data-ai-panel="obj">
        <div class="ai-edit-tip">下表可以直接改：字段名、类型、长度、关联对象/选项值。「关联对象」是下拉，只能选对象库里已有的对象——手填中文名或自造编码，导入后是空下拉框。改完再下载 Excel。</div>
        <div class="tbl-wrap">
          <table class="ai-edit">
            <thead><tr><th>字段名称</th><th>编码</th><th>物理列名</th><th>类型</th><th>长度</th><th>关联 / 选项</th><th></th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
        <datalist id="aiDicVals"></datalist>
      </div>

      ${d.form ? `<div class="ai-panel" data-ai-panel="form" hidden>${renderAiForm(d.form)}</div>` : ""}
      ${d.list ? `<div class="ai-panel" data-ai-panel="list" hidden>${renderAiList(d.list)}</div>` : ""}

      ${notes}
      <div class="ai-card-foot" id="aiActions"></div>
    </div>`;
}

/* 表单布局预览：按组显示，必填打星号，整行的单独一行。
   分行规则必须和生成器（gen_form._plan_rows）保持一致：
   每行最多 2 个普通控件。之前只按 fullRow 断行，导致某一组字段一多
   就横向挤成竖条——预览和实际出包长得不一样，用户没法据此判断。 */
const AI_MAX_PER_ROW = 2;

function renderAiForm(form) {
  // 分区标题只在「换到另一个分区」时打一次。不能再用「只有每组第一个字段
  // 带 group」的旧契约——ai_form 序列化时给每个字段都写了 group，
  // 那样每字段都会断行并重复一次分区名。
  let curGroup = "";
  const perRow = form.layout === 1 ? 1 : AI_MAX_PER_ROW;
  const lines = [];
  let buf = [];
  const flush = () => {
    if (buf.length) { lines.push(buf); buf = []; }
  };
  form.fields.forEach((f) => {
    const group = (f.group || "").trim();
    if (group && group !== curGroup) {
      flush();                       // 换分区必换行，不能接着上一区排
      curGroup = group;
      lines.push({ title: curGroup });
    }
    if (f.fullRow) {
      // 多行文本/附件等独占整行：先把本行已有的收掉，再让它自己成行
      flush();
      lines.push([f]);
      return;
    }
    buf.push(f);
    if (buf.length >= perRow) flush();
  });
  flush();

  const body = lines.map((row) => {
    if (row.title && !Array.isArray(row)) {
      return `<div class="ai-group">${esc(row.title)}</div>`;
    }
    return `<div class="ai-frow">${row.map((f) => `
      <div class="ai-fcell ${f.fullRow ? "full" : ""}">
        <span class="ai-flabel">${f.required ? '<i>*</i>' : ""}${esc(f.label)}</span>
        <span class="ai-fctrl">${esc(f.placeholder || "")}</span>
        <span class="ai-ftype">${esc(CTRL_LABEL[f.type] || f.type)}</span>
        ${f.cascadeParent ? `<span class="ai-ftype ai-fcascade" title="父级选了什么，这里只列它下面的选项">级联 ${esc(labelOf(form, f.cascadeParent))}</span>` : ""}
      </div>`).join("")}</div>`;
  }).join("");

  const req = form.fields.filter((f) => f.required).length;
  const rules = (form.hideRules || []);
  const ruleHtml = rules.length ? `
    <div class="ai-rules">
      <div class="ai-rules-head">条件隐藏 <span class="muted">${rules.length} 条 · 条件成立即隐藏</span></div>
      ${rules.map((r) => `<div class="ai-rule">
        <span class="chip">${esc(labelOf(form, r.source))} ${esc(OP_LABEL[r.operator] || r.operator)} ${esc(r.valueLabel || r.value || "")}</span>
        <span class="muted">→ 隐藏</span>
        <span class="chip accent">${esc(labelOf(form, r.target))}</span>
      </div>`).join("")}
    </div>` : "";

  return `
    <div class="ai-form-head">
      <span class="chip">${esc(form.name)}</span>
      <span class="muted">${form.fieldCount} 个控件 · ${req} 个必填 · ${form.layout === 1 ? "一行一列" : "一行两列"} · 标签宽 ${form.labelWidth}px</span>
    </div>
    <div class="ai-form-body">${body}</div>${ruleHtml}`;
}

/* 控件编码 → 显示标签，级联/隐藏规则里都靠它说人话 */
function labelOf(form, name) {
  const hit = (form.fields || []).find((f) => f.name === name);
  return hit ? hit.label : name;
}

const OP_LABEL = { equal: "等于", not_equal: "不等于", is_empty: "为空", is_not_empty: "不为空" };

/* 列表配置预览：视图 + 列 + 排序 */
function renderAiList(list) {
  const VIEW = { grid: "表格", board: "看板", gantt: "甘特", chart: "图表" };
  const views = list.views.map((v) => `<span class="chip accent">${VIEW[v] || v}</span>`).join(" ");
  const cols = list.columns.map((c) => `<span class="chip">${esc(c.label)}</span>`).join(" ");
  const extra = [];
  if (list.boardGroup) extra.push(`看板按「${esc(list.boardGroup)}」分列`);
  if (list.gantt) extra.push(`甘特 ${esc(list.gantt.start)} → ${esc(list.gantt.end)}`);
  if (list.sort) extra.push(`排序 ${esc(list.sort)}`);
  return `
    <div class="ai-list-body">
      <div class="ai-kv"><span>视图</span><b>${views}</b></div>
      <div class="ai-kv"><span>显示列</span><b>${cols}</b></div>
      ${extra.map((t) => `<div class="ai-note-line">· ${t}</div>`).join("")}
    </div>`;
}

function renderAiActions(d) {
  const foot = $("#aiActions");
  if (!foot) return;
  // 卡内只留「重新生成」，其余动作统一放在外面的 step-foot，
  // 避免同一个操作在两处出现、改一处忘一处
  foot.innerHTML = `
    <span class="sub">上面的表格可以直接改字段名、类型、选项值</span>
    <div class="ai-btns">
      <button class="btn ghost" id="aiRedo">不满意，重新生成</button>
    </div>`;
  $("#aiRedo").onclick = () => {
    state.aiData = null;
    $("#aiResult").innerHTML = "";
    goStep(1);
    const t = $("#aiChatText1");
    if (t) t.focus();
  };

  // 对象 / 表单 / 列表 三个预览页签
  $$(".ai-tab").forEach((tab) => {
    tab.onclick = () => {
      $$(".ai-tab").forEach((t) => t.classList.toggle("active", t === tab));
      $$("[data-ai-panel]").forEach((panel) => {
        panel.hidden = panel.dataset.aiPanel !== tab.dataset.ai;
      });
    };
  });

  bindAiEdit();
  renderAiRelations(d);
}

/* 预览表格的编辑：改什么直接写回 state.aiData.bo.fields，导出时就是改后的 */
function bindAiEdit() {
  const d = state.aiData;
  if (!d || !d.bo) return;

  $$("#aiResult .ai-edit tbody tr").forEach((tr) => {
    const idx = Number(tr.dataset.fi);
    const f = d.bo.fields[idx];
    if (!f) return;

    tr.querySelectorAll("[data-k]").forEach((el) => {
      const handler = () => {
        const k = el.dataset.k;
        if (k === "length") {
          const n = parseInt(el.value, 10);
          f.length = Number.isFinite(n) && n > 0 ? n : null;
        } else if (k === "objectType") {
          // 选项字段这里填的是候选值（逗号分隔），关联对象字段填对象编码
          if (f.type === "dic") {
            f.choices = el.value.split(/[,，;；|\n]/).map((s) => s.trim()).filter(Boolean);
          } else {
            f.objectType = el.value.trim();
          }
        } else {
          f[k] = el.value;
        }
      };
      el.addEventListener(el.tagName === "SELECT" ? "change" : "input", handler);
      if (el.tagName === "SELECT") {
        // 类型一改，「关联/选项」那列的意义就变了，直接重渲染整块最省事
        el.addEventListener("change", () => {
          f.type = el.value;
          // 下拉框 ↔ 选项 互转时，旧的值清掉：对象编码混进候选值会生成垃圾选项，
          // 反过来候选值当对象编码又会变成空下拉
          if (f.type === "dic") {
            f.objectType = "";
            f.choices = f.choices || [];
          } else if (f.type === "object") {
            f.choices = [];
          }
          const keep = d.bo.fields;
          $("#aiResult").innerHTML = renderAiBo(d);
          renderAiActions(d);
          if (keep.length !== d.bo.fields.length) toast("字段数变了，已刷新预览");
        });
      }
    });

    const del = tr.querySelector("[data-del]");
    if (del) {
      del.onclick = () => {
        d.bo.fields.splice(idx, 1);
        $("#aiResult").innerHTML = renderAiBo(d);
        renderAiActions(d);
        toast(`已删掉「${f.name}」`);
      };
    }
  });
}

/* 把项目经理自己整理的 Excel 交给 AI，映射成标准对象再预览 */
async function analyzeSheet(file, sheet) {
  const btn = $("#btnPickSheet") || $("#aiFromSheet");
  const box = $("#aiResult");
  const d1 = $("#aiChatText1");
  const hint = d1 ? d1.value.trim() : "";   // 首屏聊天框里写了就当作补充说明
  state.aiFile = file;                        // 换一张表单时不用再选一次文件
  goStep(2);
  if (btn) { btn.disabled = true; btn.textContent = "读表中…"; }
  box.hidden = false;
  box.classList.remove("is-error");
  const which = sheet ? `「${esc(sheet)}」` : `「${esc(file.name)}」`;
  box.innerHTML = `<div class="ai-loading"><div class="spinner"></div>
    <span>正在读${which}并让 AI 映射字段…（表单多时约半分钟）</span></div>`;
  try {
    const q = `hint=${encodeURIComponent(hint)}` +
      (sheet ? `&sheet=${encodeURIComponent(sheet)}` : "");
    const res = await fetch(`/api/ai/sheet?${q}`, {
      method: "POST",
      body: await file.arrayBuffer(),
    });
    const d = await res.json().catch(() => ({}));
    if (!res.ok || d.ok === false) throw new Error(d.error || `分析失败 ${res.status}`);
    box.innerHTML = renderAiBo(d);
    bindAiSheetPick(d);
    renderAiActions(d);
    toast(`已按「${d.sheets && d.sheets[0] ? d.sheets[0] : "你的表"}」生成字段清单，可直接改`);
  } catch (err) {
    box.classList.add("is-error");
    box.innerHTML = `<div class="ai-err">✗ ${esc(err.message)}</div>`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "选择 Excel"; }
  }
}

/* 一张 Excel 里可能塞了二三十张表单——只生成第一张多半不是他要的，给个下拉换 */
function bindAiSheetPick(d) {
  const box = $("#aiSheetPick");
  if (!box) return;
  const list = (d.formSheets || []).filter((x) => x.fieldCount > 0);
  if (list.length < 2) { box.hidden = true; return; }

  const cur = (d.sheets && d.sheets[0]) || "";
  const forms = list.filter((x) => x.kind === "form").length;
  const opts = list.map((x) => {
    const tag = x.kind === "form" ? "表单" : "数据表";
    const label = `${x.name}｜${tag} ${x.fieldCount} 项`;
    return `<option value="${esc(x.name)}" ${x.name === cur ? "selected" : ""}>${esc(label)}</option>`;
  }).join("");

  box.hidden = false;
  box.innerHTML = `
    <span class="ai-sheetpick-tip">
      这个文件里有 <b>${list.length}</b> 张表${forms ? `（${forms} 张表单）` : ""}，当前分析的是：
    </span>
    <select id="aiSheetSel">${opts}</select>
    <span class="muted">换一张会重新分析（约半分钟）</span>`;

  $("#aiSheetSel").onchange = (e) => {
    const name = e.target.value;
    if (!name || name === cur || !state.aiFile) return;
    analyzeSheet(state.aiFile, name);
  };
}

/* 把 AI 生成的对象写进一份完整模板 Excel，交给用户继续填表单/列表。
   thenBuild=true 时不下载，而是直接把这份 Excel 交给第 3 步出包。 */
async function applyAiBo(d, { thenBuild = false } = {}) {
  const btns = ["#aiApply", "#btnExportXlsx2", "#btnToPack"]
    .map((sel) => $(sel)).filter(Boolean);
  const label0 = thenBuild ? "生成导入包…" : "生成 Excel…";
  btns.forEach((b) => { b.disabled = true; });
  if (thenBuild) $("#btnToPack").textContent = label0;
  try {
    const relHeader = window.Relations ? window.Relations.decisionsHeader("ai") : null;
    const blob = await apiPostBlob("/api/ai/bo/apply", {
      bo: d.bo, form: d.form, list: d.list,
    }, relHeader ? { "X-Relations": relHeader } : {});
    const name = `PP6-${d.bo.name}.xlsx`;
    if (thenBuild) {
      // 用这份 Excel 走原有的解析 → 出包链路（预览里的改动才不会丢）
      const file = new File([blob], name, { type: blob.type });
      await handleFile(file, { fromAi: true });
    } else if (await saveBlob(blob, name)) {
      toast(`已导出 ${name}，线下改完拖回第 1 步即可继续`, 7000);
    }
  } catch (err) {
    toast("生成失败：" + err.message, 6000);
  } finally {
    btns.forEach((b) => { b.disabled = false; });
    const apply = $("#aiApply");
    if (apply) apply.textContent = "套用并下载 Excel";
    const toPack = $("#btnToPack");
    if (toPack) toPack.textContent = "下一步：生成导入包 →";
  }
}

/* 步骤切换：1 输入 / 2 预览 / 3 出包 */
function goStep(n) {
  const steps = { 1: "#stepInput", 2: "#stepPreview", 3: "#result" };
  Object.entries(steps).forEach(([k, sel]) => {
    const el = $(sel);
    if (el) el.hidden = Number(k) !== n;
  });
  $$("#steps .step").forEach((el) => {
    const cur = Number(el.dataset.step);
    el.classList.toggle("active", cur === n);
    el.classList.toggle("done", cur < n);
  });
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function backToInput() {
  goStep(1);
}

/* 第 1 步：描述 → AI 生成预览 */
async function generatePreview() {
  const desc = ($("#aiChatText1") ? $("#aiChatText1").value : "").trim();
  if (!desc) { toast("先说说这个对象要记录什么"); return; }
  if (!window.PP6_AI_READY) {
    toast("还没配置 AI，先填一个免费的 Key", 4000);
    if (typeof window.PP6_OPEN_AI === "function") window.PP6_OPEN_AI();
    return;
  }
  const btn = $("#aiChatSend1");
  const box = $("#aiResult");
  btn.disabled = true; btn.textContent = "生成中…";
  goStep(2);
  box.classList.remove("is-error");
  box.innerHTML = `<div class="ai-loading"><div class="spinner"></div>
    <span>正在让 AI 设计字段、表单和列表…（约 20 秒）</span></div>`;
  try {
    const known = state.data ? state.data.objects.map((o) => o.code) : [];
    const d = await apiPost("/api/ai/bo", { description: desc, known });
    box.innerHTML = renderAiBo(d);
    renderAiActions(d);
    // 新一轮生成，上一轮的对话记录不再适用
    const log = $("#aiChatLog");
    if (log) log.innerHTML = "";
  } catch (err) {
    box.classList.add("is-error");
    box.innerHTML = `<div class="ai-err">✗ ${esc(err.message)}</div>`;
  } finally {
    btn.disabled = false; btn.textContent = "生成预览";
  }
}

/* ---------------- 预览后的 AI 对话式微调 ----------------
   用户看到结果不满意时，不用回到第 1 步重新描述；
   直接在对话框里说「把 X 改成必填」「再加一个 Y 字段」，
   后端在现有 JSON 上做增量修改，已经调好的部分不会被推翻。 */

/* 气泡写到当前输入框所属的日志区（首屏 or 预览页） */
function aiChatLogEl() {
  return document.getElementById(aiChatTarget) || $("#aiChatLog");
}

function aiChatAppend(role, text) {
  const log = aiChatLogEl();
  if (!log) return;
  const el = document.createElement("div");
  el.className = `ai-msg ${role}`;
  el.textContent = text;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

function aiChatAppendChanges(changes) {
  if (!changes || !changes.length) return;
  const log = aiChatLogEl();
  if (!log) return;
  const el = document.createElement("div");
  el.className = "ai-msg bot";
  el.innerHTML = `<div class="ai-changes">${changes
    .map((c) => `<div class="ai-change">· ${esc(c)}</div>`).join("")}</div>`;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
}

/* 一句话就是全部输入：没有结果时它负责「从零设计」，有结果时负责「增量改」。
   用户不该先点一个「生成」按钮才能开始说话。 */
async function sendAiChat(text) {
  const msg = (text != null ? text : $("#aiChatText").value).trim();
  if (!msg) { toast("说说你要建什么"); return; }
  if (!window.PP6_AI_READY) {
    toast("还没配置 AI，先填一个免费的 Key", 4000);
    if (typeof window.PP6_OPEN_AI === "function") window.PP6_OPEN_AI();
    return;
  }

  const first = !state.aiData;      // 首轮：还没有任何结果
  const input = $("#aiChatText");
  const btn = $("#aiChatSend");
  input.value = "";
  aiChatAppend("user", msg);
  const pending = aiChatAppend("bot pending", first ? "正在设计字段、表单和列表…" : "正在修改…");
  btn.disabled = true;
  btn.textContent = first ? "生成中…" : "修改中…";

  const box = $("#aiResult");
  if (first) {
    goStep(2);                       // 让右侧结果区显现
    box.classList.remove("is-error");
    box.innerHTML = `<div class="ai-loading"><div class="spinner"></div>
      <span>正在让 AI 设计字段、表单和列表…（约 20 秒）</span></div>`;
  }

  try {
    let d;
    if (first) {
      const known = state.data ? state.data.objects.map((o) => o.code) : [];
      d = await apiPost("/api/ai/bo", { description: msg, known });
      d._first = true;
    } else {
      d = await apiPost("/api/ai/refine", {
        instruction: msg,
        bo: state.aiData.bo,
        form: state.aiData.form,
        list: state.aiData.list,
        objects: state.data ? state.data.objects.map((o) => ({ code: o.code, name: o.name })) : [],
      });
    }
    // 后端返回的都是完整结果，直接整块替换预览（手改过的字段以 AI 结果为准）
    state.aiData = d;
    box.classList.remove("is-error");
    box.innerHTML = renderAiBo(d);
    renderAiActions(d);
    pending.remove();
    const nf = (d.bo && d.bo.fields ? d.bo.fields.length : 0);
    aiChatAppend("bot", first
      ? `做好了：${d.bo.name}，${nf} 个字段，表单和列表也一起配好了。右侧你可以直接改，也可以继续跟我说要调的地方。`
      : `改好了，用时 ${d.seconds || "?"} 秒。`);
    if (!first) aiChatAppendChanges(d.changes);
    if (d.notices && d.notices.length) {
      aiChatAppend("bot note", d.notices.slice(0, 4).join("\n"));
    }
    $("#aiChatHint") && ($("#aiChatHint").textContent = "在现有基础上改，已经调好的字段不会被推翻");
    toast(first ? "已生成，继续说要改的地方即可" : "已按你的要求更新预览");
  } catch (err) {
    pending.remove();
    if (first) {
      box.classList.add("is-error");
      box.innerHTML = `<div class="ai-err">✗ ${esc(err.message)}</div>`;
      goStep(1);
    }
    aiChatAppend("bot err", (first ? "没生成出来：" : "没改成：") + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = state.aiData ? "发送修改" : "开始生成";
    input.focus();
  }
}

/* 首屏和预览页各一套对话框：首屏负责「从零」，预览页负责「接着改」。
   两边共用同一个输入源，任一框里说的都走 sendAiChat。 */
function aiChatSendFrom(idText, idLog) {
  const ta = document.getElementById(idText);
  const msg = ta ? ta.value : "";
  if (ta) ta.value = "";
  aiChatTarget = idLog;            // 气泡写到当前这个输入框所属的日志区
  sendAiChat(msg);
}

let aiChatTarget = "aiChatLog";

function bindAiChat() {
  // 首屏
  const s1 = $("#aiChatSend1");
  if (s1) s1.onclick = () => aiChatSendFrom("aiChatText1", "aiChatLog1");
  const t1 = $("#aiChatText1");
  if (t1) {
    t1.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        aiChatSendFrom("aiChatText1", "aiChatLog1");
      }
    });
  }
  const c1 = $("#aiChatClear1");
  if (c1) {
    c1.onclick = () => {
      const log = $("#aiChatLog1");
      if (log) log.innerHTML = "";
      toast("对话已清空");
    };
  }

  // 预览页
  const send = $("#aiChatSend");
  if (send) send.onclick = () => aiChatSendFrom("aiChatText", "aiChatLog");
  const input = $("#aiChatText");
  if (input) {
    // Enter 发送，Shift+Enter 换行
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        aiChatSendFrom("aiChatText", "aiChatLog");
      }
    });
  }
  const clear = $("#aiChatClear");
  if (clear) {
    clear.onclick = () => {
      const log = $("#aiChatLog");
      if (log) log.innerHTML = "";
      toast("对话已清空");
    };
  }

  // 首屏快捷 chip：填进输入框再发，用户看得见自己要的是什么
  $$("#aiChatLaunch .chip-btn").forEach((b) => {
    b.onclick = () => {
      const q = b.dataset.q || b.textContent;
      const ta = $("#aiChatText1");
      if (ta) ta.value = q;
      aiChatSendFrom("aiChatText1", "aiChatLog1");
    };
  });
}

async function apiPostBlob(path, payload, extraHeaders) {
  const res = await fetch(path, {
    method: "POST",
    headers: Object.assign({ "Content-Type": "application/json" }, extraHeaders || {}),
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || "请求失败");
  }
  return res.blob();
}

/* ------------------------------------------------ 事件绑定 */

function bind() {
  const pickAndLoad = async () => {
    if (state.desktop) {
      const file = await pickFile("excel");
      if (file) handleFile(file);
      return;
    }
    $("#fileInput").click();
  };

  // ---- 第 1 步：一个输入口（描述 + 拖文件都行）
  const dz = $("#dzInline");
  $("#btnPickSheet").onclick = async () => {
    if (state.desktop) {
      // 桌面壳里 <input type=file> 不可靠，走系统文件对话框
      const file = await pickFile("excel");
      if (file) await analyzeSheet(file);
      return;
    }
    $("#aiSheetInput").click();
  };
  $("#aiSheetInput").onchange = async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (file) await analyzeSheet(file);
  };
  // 拖到输入区 = 走 AI 读表（不是模板导入）
  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => {
    const file = e.dataTransfer.files[0];
    if (file) analyzeSheet(file);
  });

  // ---- 第 1 步（下半）：已经有一份标准 Excel，直接进预览
  $("#reimport").querySelector("span").onclick = pickAndLoad;
  $("#fileInput").onchange = (e) => handleFile(e.target.files[0]);
  const reimportBox = $("#reimport");
  ["dragenter", "dragover"].forEach((ev) =>
    reimportBox.addEventListener(ev, (e) => { e.preventDefault(); reimportBox.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) =>
    reimportBox.addEventListener(ev, (e) => { e.preventDefault(); reimportBox.classList.remove("drag"); }));
  reimportBox.addEventListener("drop", (e) => {
    const file = e.dataTransfer.files[0];
    if (file) handleFile(file);
  });

  $$("#tabs .tab").forEach((tab) => {
    tab.onclick = () => {
      $$("#tabs .tab").forEach((t) => t.classList.toggle("active", t === tab));
      $$(".panel").forEach((p) => { p.hidden = p.dataset.panel !== tab.dataset.panel; });
    };
  });

  // 第 3 步「返回上一步」：有 AI 预览就回预览，否则回输入
  $("#btnReset").onclick = () => {
    if (state.aiData) { goStep(2); return; }
    backToInput();
  };
  $("#btnBackInput").onclick = backToInput;

  const btnConfirmed = $("#btnConfirmedExcel");
  if (btnConfirmed) btnConfirmed.onclick = downloadConfirmedExcel;

  $("#modAll").onclick = () => {
    state.picked = new Set((state.data.modules || []).filter((m) => m.count > 0).map((m) => m.code));
    renderModules(); updateBar();
  };
  $("#modNone").onclick = () => {
    state.picked = new Set();
    renderModules(); updateBar();
  };

  $("#btnDemo").onclick = loadDemo;
  const packBtn = $("#btnPack") || $("#btnBuild");
  if (packBtn) packBtn.onclick = () => buildZip();

  // ---- 首屏对话已由 bindAiChat 绑定，这里不再重复绑「生成预览」

  // 预览后的对话式微调
  bindAiChat();

  // 预览底部：导出 Excel / 去出包
  $("#btnExportXlsx2").onclick = () => {
    if (state.aiData) applyAiBo(state.aiData);
    else toast("先点「生成预览」");
  };
  $("#btnToPack").onclick = () => {
    if (!state.aiData) { toast("还没有预览内容，先生成"); return; }
    // 预览里的改动要落成一份 Excel，再走原有的出包链路
    applyAiBo(state.aiData, { thenBuild: true });
  };
  $("#aiSheetInput").onchange = async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (file) await analyzeSheet(file);
  };

  // ?demo=1 直接进示例视图，便于截图与巡检
  if (new URLSearchParams(location.search).get("demo")) loadDemo();
  $("#btnInspect").onclick = async () => {
    if (state.desktop) { await inspectZip(null); return; }
    $("#inspectInput").click();
  };

  // 空白模板入口已下线：主流程改成「说需求或传表 → AI 生成 → 预览」，
  // 不再要求先下模板再填。需要一份现成结构可以点「载入示例看效果」。
  $("#inspectInput").onchange = (e) => {
    inspectZip(e.target.files[0]);
    e.target.value = "";
  };
}

  detectDesktop();
  bind();
