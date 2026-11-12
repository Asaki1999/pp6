/* 已有包加字段：上传 zip → 选对象 → 填字段 → 在原包上打补丁下载新 zip。
   复用 app.js 的 saveBlob / toast / humanSize；本文件在 app.js 之后加载。 */
const P = {
  snap: null,
  fields: [],
  selected: new Set(),
  fileName: "",
};

const pEsc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
));
const pId = (id) => document.getElementById(id);

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

function pHint() {
  const el = pId("patchHint");
  const n = P.selected.size;
  const f = P.fields.filter((x) => (x.code || "").trim()).length;
  // 空态分支并入这里作单点真相：pEmptyState() 不改 hint，避免两处互相覆盖。
  const empty = P.snap && (P.snap.objects || []).length === 0;
  el.textContent = empty
    ? "包里没有可加字段的 bo/*.xml → 改用工作区 1 反解后重新出包"
    : P.snap
      ? `已选 ${n} 个对象 · ${f} 个新字段${n && f ? " · 可以出包了" : ""}`
      : "先上传 zip，再选对象和字段";
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
    if (P.snap.objects.length) P.selected.add(P.snap.objects[0].file);
    if (!P.fields.length) P.fields.push(pEmptyField());

    pId("patchFileInfo").hidden = false;
    pId("patchFileInfo").innerHTML =
      `<span class="dot"></span><strong>${pEsc(file.name)}</strong>` +
      `<span class="muted">${humanSize(file.size)} · ${P.snap.entryCount} 个条目 · ${P.snap.boCount} 份业务对象 XML</span>`;
    const notes = P.snap.notes || [];
    const box = pId("patchNotes");
    box.hidden = notes.length === 0;
    box.innerHTML = notes.map((n) => `<div>· ${pEsc(n)}</div>`).join("");

    ["patchObjectsCard", "patchFieldsCard", "patchBuildCard"].forEach(
      (id) => { pId(id).hidden = false; });
    pRenderObjects();
    pRenderFields();
    pId("patchReport").hidden = true;
    pHint();
    // P0-2：消费 P.snap 已有的 entries/comment/copies（后端 patch.py 零改动）
    pRenderPackageStruct();
    pEmptyState();
    toast(`读到了 ${P.snap.objects.length} 份业务对象 XML，选一个开始加字段`, 4500);
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
      `<code title="${pEsc(f.name)}">${pEsc(f.code)}</code>`).join("");
    cols.push(`<div class="patch-obj ${checked ? "on" : ""}" data-file="${pEsc(o.file)}">
      <label class="patch-obj-head">
        <input type="checkbox" data-pick="${pEsc(o.file)}" ${checked}>
        <span class="patch-obj-title"><b>${pEsc(o.name || o.code)}</b>
          <code>${pEsc(o.code)}</code></span>
        <span class="patch-obj-meta">${pEsc(o.file)} · ${o.fieldCount} 个字段 · ${pEsc(o.tbl || "无表名")}</span>
        ${dup}${noid}
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
      pHint();
    };
  });
}

/* ---------------- ③ 字段表格 ---------------- */

function pEmptyField() {
  return { code: "", name: "", type: "varchar", choices: "", objectType: "",
           length: "", precision: "", default: "", required: false,
           index: false, desc: "" };
}

function pNormalizeType(v) {
  const text = String(v || "").trim();
  return P_TYPE_ALIAS[text] || text || "varchar";
}

function pRenderFields() {
  const types = (P.snap && P.snap.types) || [];
  const body = pId("patchRows");
  body.innerHTML = P.fields.map((f, i) => `
    <tr data-i="${i}">
      <td><input data-k="code" value="${pEsc(f.code)}" placeholder="riskLevel" spellcheck="false"></td>
      <td><input data-k="name" value="${pEsc(f.name)}" placeholder="风险等级"></td>
      <td><input data-k="type" value="${pEsc(f.type)}" list="patchTypes" placeholder="字符串"></td>
      <td><input data-k="choices" value="${pEsc(f.choices)}" placeholder="高,中,低"></td>
      <td><input data-k="objectType" value="${pEsc(f.objectType)}" placeholder="project"></td>
      <td><input data-k="length" value="${pEsc(f.length)}" class="num"></td>
      <td><input data-k="precision" value="${pEsc(f.precision)}" class="num"></td>
      <td><input data-k="default" value="${pEsc(f.default)}"></td>
      <td class="center"><input type="checkbox" data-k="required" ${f.required ? "checked" : ""}></td>
      <td class="center"><input type="checkbox" data-k="index" ${f.index ? "checked" : ""}></td>
      <td><input data-k="desc" value="${pEsc(f.desc)}"></td>
      <td><button class="link" data-del="${i}" title="删掉这行">×</button></td>
    </tr>`).join("");
  let dl = pId("patchTypes");
  if (!dl) {
    dl = document.createElement("datalist");
    dl.id = "patchTypes";
    document.body.appendChild(dl);
  }
  dl.innerHTML = types.map((t) =>
    `<option value="${pEsc(t.code)}">${pEsc(t.label)}${t.verified ? "" : "（未验证）"}</option>`).join("");

  body.querySelectorAll("input[data-k]").forEach((el) => {
    const apply = () => {
      const i = Number(el.closest("tr").dataset.i);
      const k = el.dataset.k;
      P.fields[i][k] = el.type === "checkbox" ? el.checked : el.value;
      pHint();
    };
    el.addEventListener("input", apply);
    el.addEventListener("change", apply);
  });
  body.querySelectorAll("[data-del]").forEach((btn) => {
    btn.onclick = () => {
      P.fields.splice(Number(btn.dataset.del), 1);
      if (!P.fields.length) P.fields.push(pEmptyField());
      pRenderFields(); pHint();
    };
  });
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
  const fields = pCollectFields();
  if (!fields.length) throw new Error("请至少填一个字段编码");
  return {
    token: P.snap.token,
    targets: picked,
    fields,
    mode: pId("patchMode").value,
    scope: pId("patchScope").value,
    allCopies: pId("patchAllCopies").checked,
    syncForm: pId("patchSyncForm").checked,
  };
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
    const rows = (data.fields || []).map((f) => ({ ...pEmptyField(), ...f }));
    if (!rows.length) { toast("字段表里没有读到字段行"); return; }
    P.fields = P.fields.filter((f) => String(f.code || "").trim())
      .concat(rows);
    pRenderFields(); pHint();
    toast(`导入了 ${rows.length} 个字段`, 4000);
  } catch (err) { pFail(err); }
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
    toast("预览完成，确认无误再下载", 4000);
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
    toast(`已生成 ${jobs.length} 份新 zip`, 5000);
  } catch (err) { pFail(err); } finally { pBusy(btn, false); }
}

async function pWorkbook(btn) {
  pBusy(btn, true, "正在生成…");
  try {
    const res = await pApi("/api/patch/workbook", pSpec());
    await pDownload(res, "pp6-加字段-规格表.xlsx");
    toast("规格表已下载，可继续补表单/列表后走主流程出包", 5000);
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
  box.innerHTML = `
    <div class="card">
      <div class="card-head"><div><h3>包结构（${entries.length} 条 · 原包 ${pEsc(P.fileName || "")}）</h3>
        <div class="sub">来自 /api/patch/inspect 已有数据，后端零改动；按顶层目录分组，可折叠</div></div></div>
      ${groupHtml || '<div class="empty">包内没有可列出的条目。</div>'}
      <div class="set-note"><strong>comment 原文</strong>：<code>${pEsc(P.snap.comment || "")}</code></div>
      ${copyChips}
      <div class="set-note">${staticNotes}</div>
    </div>`;
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
  pId("patchPickZip").onclick = async () => {
    if (typeof native === "function" && native()) {
      const file = await pickFile("zip");
      if (file) await pUploadZip(file);
    } else zipInput.click();
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

  pId("patchObjAll").onclick = () => {
    P.selected = new Set(P.snap.objects.map((o) => o.file));
    pRenderObjects(); pHint();
  };
  pId("patchObjNone").onclick = () => {
    P.selected = new Set(); pRenderObjects(); pHint();
  };

  pId("patchAddRow").onclick = () => {
    P.fields.push(pEmptyField()); pRenderFields(); pHint();
  };
  pId("patchPasteAdd").onclick = () => {
    const rows = pParsePaste(pId("patchPaste").value);
    if (!rows.length) { toast("没解析出字段行，检查列顺序"); return; }
    P.fields = P.fields.filter((f) => String(f.code || "").trim()).concat(rows);
    pId("patchPaste").value = "";
    pRenderFields(); pHint();
    toast(`追加了 ${rows.length} 个字段`, 3500);
  };
  pId("patchPickXlsx").onclick = () => pId("patchXlsxInput").click();
  pId("patchXlsxInput").onchange = (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (file) pImportFields(file);
  };
  pId("patchTpl").onclick = (e) => pTemplate(e.currentTarget);
  pId("patchDryRun").onclick = (e) => pPreview(e.currentTarget);
  pId("patchBuild").onclick = (e) => pBuild(e.currentTarget);
  pId("patchWorkbook").onclick = (e) => pWorkbook(e.currentTarget);
}

pBind();
