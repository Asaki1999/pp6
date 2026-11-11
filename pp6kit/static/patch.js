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
  el.textContent = P.snap
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
      `导入方式：系统管理 → 发布管理 → 导入同步包，直接上传新 zip。`;
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
