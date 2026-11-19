/* 项目字段对齐组件：解析页「项目对齐」页签 + 「相近字段确认」弹窗。
 * 不复制业务规则——关系、依据、差异全部由后端算好，这里只负责画与收集勾选。
 *
 * 数据来自 /api/parse 的 projectAlign（契约见 docs/架构-项目关联字段-20261114.md）：
 *   { status, snapshotSourceLabel, snapshotGeneratedAt, project, notice, warnings, summary,
 *     items: [{objectCode, objectName, linked, updateMode, linkField, exact[], similar[],
 *              pending[], pendingCount, relations[], relationSummary{},
 *              conflicts[], skipped[], diffs[], diffCount}],
 *     relations: [{fieldCode, fieldName, status, rule, basis, confidence,
 *                  target: {code,name,fieldName,type}|null,
 *                  candidates: [{code,name,fieldName,type}],
 *                  diff: {…}|null, action, note}],
 *     relationSummary: {total, auto, pending, applied, ambiguous, conflict,
 *                       unrelated, unknown, needsConfirm},
 *     pendingCount,
 *     diffs: [{objectCode, objectName, fieldCode, fieldName, targetCode, targetName,
 *              rule, updateMode, count,
 *              changes: [{key, label, mine, project}],
 *              options: [{name, mineKey, mineName, projectKey, projectName, state}]}],
 *     diffCount,
 *     addedLinks: [{objectCode, objectName, fieldName, code}], addedLinkCount,
 *     newFields:  [{objectCode, objectName, fieldCode, fieldName}], newFieldCount }
 *
 * 产品口径（2026-11-15 用户拍板，勿改）：
 *   - **只有「直接对应」自动覆盖**：字段名完全一致 + 类型同族 + 不在可疑名单 + 候选唯一；
 *   - 语义相近（S3/S4）、可能相关（S2/S1）、同名但可疑/跨族/多候选/冲突 → 逐条列出，
 *     **默认保留我方**，勾「按项目覆盖」才覆盖；
 *   - 未匹配字段保留、按原规则生成，不弹窗；
 *   - 勾选结果由 collect() 交给 app.js 拼进 X-Project-Link 的 objects[code].align，
 *     重新解析后预览与 zip 都按勾选走（预览=产物）。
 */
window.ProjLink = (function () {
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));

  let _vm = null;
  /* 勾选态："对象编码.字段编码" → 覆盖成什么。
   *   true         按最匹配的项目字段覆盖
   *   "targetCode" 多候选时用户在下拉里挑的那个项目字段
   * 放内存不放 DOM：全选/全不选、重渲染、探针都只认这一份，DOM 只是它的投影。 */
  const _picked = new Map();
  /* 多候选下拉的选择："对象编码.字段编码" → 项目字段编码（为空 = 最匹配） */
  const _candChoice = new Map();

  /* 对齐目标（项目 / 其他业务对象）。文案一律从这里取：组件本身不假设
   * 「关联的一定是项目」。老包（后端还没带 target）按项目兜底，保证存量
   * JSON 也能画出来。 */
  function tgtOf(vm) {
    const v = (vm || _vm || {}).target;
    if (v && (v.name || v.code)) return v;
    const p = (vm || _vm || {}).project || {};
    return { name: "项目", code: p.code || "project", tbl: p.tbl || "pp_project",
      link_label: "所属项目", is_project: true };
  }
  /* 目标名（原样，用于已经会被再转义一次的文本流，如差异摘要） */
  function tName(vm) { return String(tgtOf(vm).name || "项目"); }
  /* 目标名 / 物理表 / 链接字段中文名（已转义，直接拼进 HTML） */
  const TN = () => esc(tName());
  const TTBL = () => esc(tgtOf().tbl || "");
  const TLINK = () => esc(tgtOf().link_label || "所属项目");

  /* 快照来源兜底文案（正常走后端 snapshotSourceLabel）。做成函数：上传包那档
   * 要写「上传的合同包」，常量表会在模块加载时把「项目」写死。 */
  function sourceLabel(code) {
    if (code === "package_upload") return `上传的${tName()}包`;
    return { target_db: "目标库", package_snapshot: "随包快照",
      unavailable: "无" }[code] || code || "无";
  }

  /* 关系状态 → 显示文案（后端给 status，前端只翻译） */
  const STATUS = {
    auto: { cls: "ok", text: "已自动覆盖" },
    applied: { cls: "ok", text: "已按你的选择覆盖" },
    pending: { cls: "warn", text: "待确认" },
    ambiguous: { cls: "warn", text: "多候选，待确认" },
    conflict: { cls: "bad", text: "保留我方" },
    unrelated: { cls: "", text: "按原规则生成" },
    unknown: { cls: "muted", text: "不参与对齐" },
  };

  /* 判断依据的短标签（长句在 basis 里，chip 只放 4 个字） */
  const RULE_LABEL = {
    E0: "同名",
    S3: "编码/物理列一致",
    S4: "同义词",
    S2: "只差一个字",
    S1: "名称包含",
  };

  const CONFIDENCE = { high: "高", medium: "中", low: "低" };

  /* 弹窗的三个分组：由后端 status/rule 决定，前端不做二次判断 */
  const GROUPS = [
    { key: "similar", title: "语义相近",
      tip: "名字不同但是同一个意思（同义词归一 / 编码或物理列相同）" },
    { key: "related", title: "可能相关",
      tip: "名字只差一两个字，或者一个包含另一个——很可能只是巧合" },
    { key: "confirm", title: "需确认",
      tip: "多候选 / 冲突 / 可疑快照 / 类型跨族：不敢替你决定" },
  ];

  const CONFIRM_STATUS = { pending: 1, ambiguous: 1, conflict: 1 };

  function linkedItems(vm) {
    return ((vm && vm.items) || []).filter((it) => it && it.linked);
  }

  function diffsOf(vm) {
    return ((vm && vm.diffs) || []).filter(Boolean);
  }

  /* 扁平关系表（每条都补上 objectCode：后端顶层的 relations 是跨对象拼的，
   * 勾选态要用「对象.字段」当 key，缺了会串到别的对象上） */
  function relationsOf(vm) {
    const got = vm || _vm;
    if (!got) return [];
    const items = linkedItems(got);
    if (items.length) {
      const out = [];
      items.forEach((it) => {
        (it.relations || []).forEach((r) => {
          if (r) out.push(Object.assign({}, r, { objectCode: r.objectCode || it.objectCode }));
        });
      });
      return out;
    }
    return Array.isArray(got.relations) ? got.relations.filter(Boolean) : [];
  }

  /* 需要用户看一眼的关系行（auto/applied/无关/不参与 都不进弹窗） */
  function confirmRows(vm) {
    return relationsOf(vm).filter((r) => CONFIRM_STATUS[r.status]);
  }

  /* 弹窗与 badge 的唯一口径：需要确认的条数（含冲突，冲突也要让用户看见） */
  function pendingCount(vm) {
    const got = vm || _vm;
    if (!got) return 0;
    if (typeof got.pendingCount === "number") return got.pendingCount;
    return confirmRows(got).length;
  }

  function diffCount(vm) {
    const got = vm || _vm;
    if (!got) return 0;
    if (typeof got.diffCount === "number") return got.diffCount;
    return diffsOf(got).length;
  }

  /* 直接对应、但项目定义和我们的写法有差异的行（已自动覆盖，只读展示）。
   * 用户要「哪些不一致」：不能因为 autofix 了就不给看。 */
  function hasDiff(r) {
    return !!(r && r.diff && String(diffText(r.diff) || "").trim());
  }

  function directRows(vm) {
    const got = vm || _vm;
    if (!got) return [];
    return relationsOf(got).filter((r) => r && r.status === "auto" && hasDiff(r));
  }

  function directDiffCount(vm) {
    return directRows(vm).length;
  }

  function summaryOf(vm) {
    const got = vm || _vm;
    const rs = (got && got.relationSummary) || {};
    return {
      auto: rs.auto || 0,
      pending: rs.pending || 0,
      ambiguous: rs.ambiguous || 0,
      conflict: rs.conflict || 0,
      unrelated: rs.unrelated || 0,
      unknown: rs.unknown || 0,
      applied: rs.applied || 0,
      total: rs.total || 0,
      needsConfirm: typeof rs.needsConfirm === "number"
        ? rs.needsConfirm : (rs.pending || 0) + (rs.ambiguous || 0) + (rs.conflict || 0),
    };
  }

  function updateBadge() {
    const badge = document.getElementById("plBadge");
    if (!badge) return;
    const n = pendingCount();
    badge.textContent = String(n);
    badge.className = "badge" + (n ? " has-warn" : "");
  }

  /* 空值统一显示成「（空）」：表里留一片空白用户分不清是没值还是没渲染 */
  function cell(v) {
    const text = v === null || v === undefined ? "" : String(v).trim();
    return text === "" ? '<span class="muted">（空）</span>' : esc(text);
  }

  function short(v) {
    const text = v === null || v === undefined ? "" : String(v).trim();
    return text === "" ? "（空）" : text;
  }

  /* ---------------------------------------------------------------- 关系行 */

  function rowKey(objCode, fieldCode) {
    return String(objCode || "") + "." + String(fieldCode || "");
  }

  /* 一条关系的分组（语义相近 / 可能相关 / 需确认） */
  function groupOf(r) {
    if (r.status === "conflict" || r.status === "ambiguous") return "confirm";
    if (r.rule === "E0") return "confirm";     // 同名但可疑 / 跨族
    if (r.rule === "S3" || r.rule === "S4") return "similar";
    if (r.rule === "S2" || r.rule === "S1") return "related";
    return "confirm";
  }

  /* 差异摘要：一格里说清「勾了会变成什么」（类型 字符串→选项；选项 基建 01→1） */
  function diffLines(d) {
    const out = [];
    ((d && d.changes) || []).forEach((c) => {
      out.push(`${c.label || c.key} ${short(c.mine)}→${short(c.project)}`);
    });
    ((d && d.options) || []).forEach((o) => {
      const mine = o.mineKey || o.mineName || "";
      const theirs = o.projectKey || o.projectName || "";
      const state = { changed: "", only_project: "（你没有）",
        only_mine: `（${tName()}没有）` }[o.state] || "";
      out.push(`选项 ${o.name} ${short(mine)}→${short(theirs)}${state}`);
    });
    return out;
  }

  function diffText(d, limit) {
    const lines = diffLines(d);
    if (!lines.length) return "";
    const max = limit || 3;
    const head = lines.slice(0, max).join("；");
    return lines.length > max ? `${head} …（共 ${lines.length} 处）` : head;
  }

  function targetLabel(t) {
    if (!t) return '<span class="muted">（无）</span>';
    return `<b>${esc(t.name)}</b> <code>${esc(t.code)}</code>`;
  }

  /* 多候选：下拉里挑「究竟覆盖成哪个项目字段」（默认最匹配的那个）。
   * 选中的 code 会原样进 state.plAlign（`align[字段] = "项目字段编码"`），
   * 服务端只当**选择**用，规则/差异仍由它自己重算。 */
  function candidateHtml(r) {
    const list = [r.target].concat(r.candidates || []).filter(Boolean);
    if (!r.target || list.length < 2) return "";
    const key = rowKey(r.objectCode, r.fieldCode);
    const opts = list.map((t, i) => `
      <option value="${esc(t.code)}"${i === 0 ? " selected" : ""}>
        ${esc(t.name)}（${esc(t.code)} · ${esc(t.type || "")}）${i === 0 ? " — 最匹配" : ""}
      </option>`).join("");
    return `
      <div class="pl-rel-cand">
        <label>按这个覆盖
          <select data-pl-cand="${esc(key)}" data-pl-best="${esc(r.target.code)}"
                  data-pl-cand-obj="${esc(r.objectCode)}"
                  data-pl-cand-field="${esc(r.fieldCode)}">${opts}</select>
        </label>
        <span class="muted">默认最匹配；选了别的就按你选的那个覆盖</span>
      </div>`;
  }

  function rowHtml(objCode, r, auto = false) {
    const st = STATUS[r.status] || { cls: "", text: r.status || "" };
    const key = rowKey(objCode, r.fieldCode);
    const checked = _picked.has(key);
    const disabled = r.status === "conflict";
    const diff = diffText(r.diff);
    const cand = (r.status !== "conflict"
      && (r.status === "ambiguous" || (r.candidates && r.candidates.length)))
      ? candidateHtml({ ...r, objectCode: objCode }) : "";
    const note = r.note
      ? `<div class="pl-rel-note">⚠ ${esc(r.note)}</div>` : "";
    const pick = auto
      ? `<span class="pl-rel-locked muted" title="字段名完全一致，已按${TN()}定义自动覆盖">已自动覆盖</span>`
      : disabled
        ? `<span class="pl-rel-locked muted" title="冲突：${TN()}字段已被别的字段认领">保留我的</span>`
        : `<label class="pl-rel-pick">
             <input type="checkbox" data-pl-pick="${esc(key)}"
                    data-pl-obj="${esc(objCode)}" data-pl-field="${esc(r.fieldCode)}"
                    ${checked ? "checked" : ""}>
             <span>按${TN()}覆盖</span>
           </label>`;
    return `
      <div class="pl-rel-row ${esc(st.cls)}" data-pl-rel="${esc(key)}">
        <div class="pl-rel-main">
          <div class="pl-rel-title">
            <span class="pl-rel-mine">你的字段 <b>${esc(r.fieldName)}</b>
              <code>${esc(r.fieldCode)}</code></span>
            <span class="pl-rel-chip ${esc(st.cls)}">${esc(RULE_LABEL[r.rule] || st.text)}
              ${r.rule ? `<i>${esc(r.rule)}</i>` : ""}</span>
            <span class="pl-rel-arrow">→</span>
            <span class="pl-rel-target">${TN()}字段 ${targetLabel(r.target)}</span>
            <span class="pl-rel-status ${esc(st.cls)}">${esc(st.text)}</span>
          </div>
          <div class="pl-rel-basis">
            依据：${esc(r.basis || st.text)}
            ${r.confidence ? `· 把握 ${esc(CONFIDENCE[r.confidence] || r.confidence)}` : ""}
            ${r.action ? `· 处理：${esc(r.action)}` : ""}
          </div>
          ${diff ? `<div class="pl-rel-diff">差异：${esc(diff)}</div>` : ""}
          ${note}
          ${cand}
        </div>
        <div class="pl-rel-act">${pick}</div>
      </div>`;
  }

  /* ---------------------------------------------------------------- 页签 */

  /* 无关字段清单：项目里没有对应字段 → 按原规则生成。
   * 默认收起（大对象可能几十个），但**能展开看到每一个字段名**——
   * 用户问的就是「哪些字段和项目无关」，只给一个数字等于没回答。
   * 只列名字 + 编码（不做成表格）：一行行表格会把弹窗刷成几十行。 */
  function unrelatedHtml(rows) {
    if (!rows.length) return "";
    const names = rows.map(
      (r) => `<span class="pl-rel-name" data-pl-unrelated-field="${esc(r.fieldCode)}"
        title="${esc(r.note || "")}">${esc(r.fieldName)}<code>${esc(r.fieldCode)}</code></span>`)
      .join("");
    return `
      <details class="pl-rel-unrelated" data-pl-unrelated="${rows.length}">
        <summary>无关字段（${rows.length}）—— ${TN()}里没有对应字段，将按原规则生成</summary>
        <div class="pl-rel-unrelated-list">${names}</div>
      </details>`;
  }

  function relTableHtml(item) {
    const rows = (item.relations || []).filter(
      (r) => r && r.status !== "unrelated" && r.status !== "unknown");
    if (!rows.length) return "";
    const body = rows.map((r) => {
      const st = STATUS[r.status] || { cls: "", text: r.status || "" };
      return `
        <tr class="${esc(st.cls)}">
          <td><b>${esc(r.fieldName)}</b> <code>${esc(r.fieldCode)}</code></td>
          <td><span class="pl-rel-status ${esc(st.cls)}">${esc(st.text)}</span></td>
          <td>${targetLabel(r.target)}</td>
          <td>${esc(r.basis || "")}${r.note ? `<span class="muted">（${esc(r.note)}）</span>` : ""}</td>
          <td>${esc(r.action || "")}</td>
        </tr>`;
    }).join("");
    return `
      <table class="pl-rel-table">
        <thead><tr><th>字段</th><th>状态</th><th>${TN()}字段</th><th>依据</th><th>处理</th></tr></thead>
        <tbody>${body}</tbody>
      </table>`;
  }

  function ruleChip(d) {
    const chip = d.rule ? `<span class="chip muted">${esc(d.rule)}</span>` : "";
    const mode = d.updateMode
      ? '<span class="chip warn">更新模式：编码/物理列保留</span>' : "";
    return chip + mode;
  }

  /* 选项值差异的三种状态（后端给 state，前端只翻译成人话）。
   * 做成函数而不是常量表：「项目新增」在关联其他对象时要变成「合同新增」，
   * 常量会在模块加载时（_vm 还是 null）把「项目」写死。 */
  function optState() {
    return {
      changed: { cls: "warn", text: "码不同" },
      only_project: { cls: "add", text: `${tName()}新增` },
      only_mine: { cls: "mine", text: "将被替换" },
    };
  }

  /* 选项值一格：「名称 业务码」；只有一侧有的值直接写清「你没有 / 项目没有」 */
  function optionCell(side, row) {
    if (side === "project") {
      if (row.projectName === null && row.projectKey === null) {
        return '<span class="pl-none mine">（你没有）</span>';
      }
      const name = row.projectName || row.projectKey || "";
      const key = row.projectKey && row.projectName ? ` ${row.projectKey}` : "";
      return esc(name + key);
    }
    if (row.mineName === null && row.mineKey === null) {
      return `<span class="pl-none project">（${TN()}没有）</span>`;
    }
    const name = row.mineName || row.mineKey || "";
    const key = row.mineKey && row.mineName ? ` ${row.mineKey}` : "";
    return esc(name + key);
  }

  function optionRow(row) {
    const meta = optState()[row.state] || { cls: "warn", text: row.state || "" };
    return `
      <tr class="pl-opt ${esc(meta.cls)}">
        <th>选项值<span class="pl-opt-tag ${esc(meta.cls)}">${esc(meta.text)}</span></th>
        <td>${optionCell("project", row)}</td>
        <td>${optionCell("mine", row)}</td>
      </tr>`;
  }

  /* 差异表：左标签列固定 90px，两个值列等宽；移动端整表横向滚动 */
  function tableHtml(d) {
    const changes = (d.changes || []).map((c) => `
      <tr>
        <th>${esc(c.label || c.key)}</th>
        <td>${cell(c.project)}</td>
        <td>${cell(c.mine)}</td>
      </tr>`).join("");
    const options = (d.options || []).map(optionRow).join("");
    if (!changes && !options) return "";
    return `
      <div class="pl-dtable-wrap">
        <table class="pl-dtable">
          <thead>
            <tr><th>项</th><th>${TTBL()}</th><th>你的 Excel</th></tr>
          </thead>
          <tbody>${changes}${options}</tbody>
        </table>
      </div>`;
  }

  /* 状态查表：差异卡要说清「这张卡是已经覆盖了，还是等你勾」 */
  function statusOf(vm, d) {
    const hit = relationsOf(vm).find(
      (r) => r.fieldCode === d.fieldCode && (r.objectCode || d.objectCode) === d.objectCode);
    return hit ? hit.status : "";
  }

  function cardHtml(d, status) {
    const n = (d.changes || []).length + (d.options || []).length;
    const st = STATUS[status] || null;
    const tag = st ? `<span class="pl-rel-status ${esc(st.cls)}">${esc(st.text)}</span>` : "";
    return `
      <div class="pl-card" data-pl-diff="${esc(d.objectCode)}.${esc(d.fieldCode)}">
        <div class="pl-card-title">
          <strong>${esc(d.objectName || d.objectCode)}</strong>
          <span class="pl-arrow">·</span>
          ${esc(d.fieldName)} <code>${esc(d.fieldCode)}</code>
          <span class="pl-arrow">→</span>
          ${esc(d.targetName)} <code>${esc(d.targetCode)}</code>
          ${ruleChip(d)}
          ${tag}
          <span class="chip accent">${n} 处差异</span>
        </div>
        ${warnHtml(d)}
        ${tableHtml(d)}
      </div>`;
  }

  /* 对齐会丢东西（自定义候选值被替换 / 多选变单选）：必须能看见，
   * 不能只写在 zip 注释里——没人会去读注释 */
  function warnHtml(d) {
    return ((d && d.warnings) || [])
      .map((w) => `<span class="pl-warn">⚠ ${esc(w)}</span>`).join("");
  }

  function addedLinkHtml(vm) {
    const links = (vm && vm.addedLinks) || [];
    if (!links.length) return "";
    return links.map((l) => `
      <div class="pl-line ok">+ 自动补「${esc(l.fieldName)}」<code>${esc(l.code)}</code>
        ${esc(l.objectName || l.objectCode)}
        <span class="muted">（Excel 里没有这一列，按${TN()}对象范式补的）</span>
      </div>`).join("");
  }

  function emptyStateHtml(vm) {
    const links = addedLinkHtml(vm);
    if (links) {
      return `<div class="pl-card">${links}
        <div class="pl-line muted">字段与${TN()}定义一致，没有需要覆盖的差异</div></div>`;
    }
    return `<div class="pl-card"><div class="pl-line muted">字段与${TN()}一致，无需对齐</div></div>`;
  }

  function headLine(vm, items) {
    const s = summaryOf(vm);
    const head = (vm && vm.summary) || {};
    return `关联${TN()} ${esc(head.linkedObjects || items.length)} 个对象`
      + ` · 直接对应 ${esc(s.auto)} 个已自动覆盖`
      + ` · 待确认 ${esc(s.needsConfirm)} 个`
      + ` · 无关 ${esc(s.unrelated)} 个按原规则生成`
      + ` · 新字段 ${esc((vm && vm.newFieldCount) || 0)} 个（按原规则生成）`;
  }

  /* 页签面板：没有勾选对象时整块隐藏（不留空壳） */
  function render(box, vm) {
    _vm = vm;
    if (!box) return;
    const items = linkedItems(vm);
    if (!items.length) {
      box.hidden = true;
      box.innerHTML = "";
      updateBadge();
      return;
    }
    box.hidden = false;
    const src = (vm && vm.snapshotSourceLabel)
      || sourceLabel(vm && vm.snapshotSource);
    const when = vm && vm.snapshotGeneratedAt
      ? ` ${String(vm.snapshotGeneratedAt).slice(0, 10)}` : "";
    const rows = diffsOf(vm);
    const cards = rows.length
      ? rows.map((d) => cardHtml(d, statusOf(vm, d))).join("")
      : emptyStateHtml(vm);
    const tables = items.map((it) => {
      const s = it.relationSummary || {};
      const rels = it.relations || [];
      const unrelated = rels.filter((r) => r && r.status === "unrelated");
      const unknown = s.unknown || 0;
      return `
        <div class="pl-obj">
          <div class="pl-obj-head">
            <strong>${esc(it.objectName || it.objectCode)}</strong>
            <code>${esc(it.objectCode)}</code>
            ${sumChips(it)}
          </div>
          ${relTableHtml(it)}
          ${unrelatedHtml(unrelated)}
          ${unknown ? `<div class="pl-line muted">另有 ${esc(unknown)} 个字段不参与对齐
            （系统字段 / 本对象自身的名称·标题 / 已是「${TLINK()}」）</div>` : ""}
        </div>`;
    }).join("");
    const conflicts = items.reduce((acc, it) => acc.concat((it.conflicts || []).map(
      (c) => `<div class="pl-line bad">✗ ${esc(it.objectName)} ${esc(c.message)}</div>`)), []);
    box.innerHTML = `
      <div class="pl-head">
        <strong>${TN()}字段对齐</strong>
        <span class="chip muted">字段快照：${esc(src)}${esc(when)}</span>
        <span class="muted pl-head-tip">${headLine(vm, items)}</span>
      </div>
      ${(vm && vm.notice) ? `<div class="pl-notice">${esc(vm.notice)}</div>` : ""}
      ${((vm && vm.warnings) || []).map((w) => `<div class="pl-notice">${esc(w)}</div>`).join("")}
      ${tables}
      ${cards}
      ${conflicts.join("")}`;
    updateBadge();
  }

  function sumChips(it) {
    const s = it.relationSummary || {};
    const out = [];
    if (s.auto) out.push(`<span class="chip ok">自动覆盖 ${esc(s.auto)}</span>`);
    if (s.needsConfirm) out.push(`<span class="chip warn">待确认 ${esc(s.needsConfirm)}</span>`);
    return out.join("");
  }

  /* ---------------------------------------------------------------- 弹窗 */

  /* 「相近字段确认」：默认全部不勾（保留我方），用户勾了才覆盖。
   * 没有可确认的项就不弹（纯 E0 / 全无关时不该糊一层遮罩）。 */
  function openModal(vm) {
    _vm = vm || _vm;
    const mask = document.getElementById("plMask");
    const body = document.getElementById("plModalBody");
    const head = document.getElementById("plModalTitle");
    if (!mask || !body) return;
    const rows = confirmRows(_vm);
    // 只有直接对应（E0）的差异、没有待确认项时也要弹：用户要看到「哪些不一致」，
    // 不能因为已经自动覆盖就静默改掉。
    if (!rows.length && !directRows(_vm).length) return;
    _picked.clear();                       // 每次打开都从「保留我方」开始
    _candChoice.clear();                   // 多候选下拉也回到「最匹配」
    if (head) {
      head.textContent = rows.length
        ? `这些字段和${tName()}相近，要按${tName()}定义覆盖吗？`
        : `${tName()}字段已自动对齐，下面是改动明细`;
    }
    body.innerHTML = bodyHtml(_vm);
    mask.hidden = false;
    document.body.classList.add("pl-modal-open");
    bindPicks(body);
    updateFoot();
    const first = body.querySelector && body.querySelector("[data-pl-pick]");
    if (first && first.focus) first.focus();
  }

  /* 勾选框 → 内存勾选态。用元素上的 onchange（不依赖事件冒泡），
   * 探针的最小 DOM 没有事件系统也不会炸。 */
  function bindPicks(body) {
    if (!body.querySelectorAll) return;
    const boxes = body.querySelectorAll("[data-pl-pick]") || [];
    for (let i = 0; i < boxes.length; i += 1) {
      const el = boxes[i];
      el.onchange = () => {
        const key = boxKey(el);
        if (el.checked) _picked.set(key, rowPickValue(key));
        else _picked.delete(key);
        updateFoot();
      };
    }
    // 多候选下拉：选了哪个项目字段，就覆盖成哪个（顺手打勾：选候选 = 我要覆盖）
    const cands = body.querySelectorAll("[data-pl-cand]") || [];
    for (let i = 0; i < cands.length; i += 1) {
      const sel = cands[i];
      sel.onchange = () => {
        const key = attrOf(sel, "data-pl-cand");
        chooseCandidate(key, sel.value);
        const box = body.querySelector ? body.querySelector(
          `[data-pl-pick="${key}"]`) : null;
        if (box && box.checked === false) box.checked = true;
      };
    }
  }

  /* 元素属性读三个来源：真 DOM 的 getAttribute / 探针的 _attrs / dataset */
  function attrOf(el, name) {
    if (!el) return "";
    if (typeof el.getAttribute === "function") {
      const got = el.getAttribute(name);
      if (got) return String(got);
    }
    return String((el.dataset && el.dataset[name]) || "");
  }

  function boxKey(el) {
    return attrOf(el, "data-pl-pick");
  }

  /* 这一行「覆盖成什么」：多候选下拉选了非最匹配的，就带那个项目字段编码 */
  function rowPickValue(key) {
    const chosen = _candChoice.get(key) || "";
    const r = relationOf(key);
    const best = (r && r.target && r.target.code) || "";
    return (chosen && chosen !== best) ? chosen : true;
  }

  /* 一行关系（按 "对象.字段" 反查，用于取「最匹配」是哪一个） */
  function relationOf(key) {
    const dot = key.lastIndexOf(".");
    const field = key.slice(dot + 1);
    return confirmRows(_vm).find((r) => rowKey(
      r.objectCode || objOf(_vm, r.fieldCode), r.fieldCode) === key) || null;
  }

  /* 多候选下拉选了哪个项目字段 → 记下来，并且**直接算作「按项目覆盖」**
   * （选候选本身就是「我要覆盖成这个」的动作，不用再点一次勾）。 */
  function chooseCandidate(key, code) {
    _candChoice.set(key, String(code || ""));
    _picked.set(key, rowPickValue(key));
    updateFoot();
    return _picked.get(key);
  }

  /* 底部按钮的可用状态跟着勾选走（0 个勾选时「按勾选覆盖」没有意义） */
  function updateFoot() {
    const btn = document.getElementById("plApplyPicked");
    if (!btn) return;
    btn.textContent = _picked.size
      ? `按勾选覆盖（${_picked.size} 个）` : "按勾选覆盖";
    btn.disabled = !_picked.size;
    btn.title = _picked.size ? "" : "先勾选要覆盖的字段；一个都不改就点「保留我的」";
  }

  function bodyHtml(vm) {
    const rows = confirmRows(vm);
    const s = summaryOf(vm);
    const links = addedLinkHtml(vm);
    const newCount = (vm && vm.newFieldCount) || 0;
    const meta = `
      <div class="muted">勾了「关联${TN()}」的对象，只有<b>直接对应</b>（字段名完全一致 +
        类型同族）的字段会照 ${TTBL()} 的定义自动覆盖。下面这些是「相近」或「需确认」的：
        <b>默认保留你的定义</b>，勾上「按${TN()}覆盖」才会改。</div>
      <div class="pl-modal-meta muted">
        ${TN()}对象：${esc(tgtOf(vm).code || "project")}
        （${esc((vm && vm.project && vm.project.tbl) || tgtOf(vm).tbl || "pp_project")}）·
        字段快照来源：${esc((vm && (vm.snapshotSourceLabel || vm.snapshotSource)) || "无")}
        ${vm && vm.snapshotGeneratedAt
    ? " " + esc(String(vm.snapshotGeneratedAt).slice(0, 10)) : ""}
      </div>
      <div class="pl-rel-summary">
        直接对应 <b>${esc(s.auto)}</b> 个已自动覆盖；下面 <b>${esc(rows.length)}</b>
        个需要你确认；无关 <b>${esc(s.unrelated)}</b> 个按原规则生成。
      </div>`;
    const groups = GROUPS.map((g) => {
      const mine = rows.filter((r) => groupOf(r) === g.key);
      if (!mine.length) return "";
      return `
        <div class="pl-rel-group" data-pl-group="${esc(g.key)}">
          <div class="pl-rel-group-head">
            <strong>${esc(g.title)}</strong>
            <span class="chip muted">${mine.length}</span>
            <span class="muted">${esc(g.tip)}</span>
          </div>
          ${mine.map((r) => rowHtml(r.objectCode || objOf(vm, r.fieldCode), r)).join("")}
        </div>`;
    }).join("");
    const autoRows = directRows(vm);
    const autoGroup = autoRows.length ? `
      <div class="pl-rel-group" data-pl-group="auto">
        <div class="pl-rel-group-head">
          <strong>直接对应（已自动覆盖）</strong>
          <span class="chip muted">${autoRows.length}</span>
          <span class="muted">字段名完全一致；下面是${TN()}定义改了什么，只读</span>
        </div>
        ${autoRows.map((r) => rowHtml(
          r.objectCode || objOf(vm, r.fieldCode), r, true)).join("")}
      </div>` : "";
    const unrelated = relationsOf(vm).filter((r) => r && r.status === "unrelated");
    return `${meta}${links}${autoGroup}${groups}
      ${unrelatedHtml(unrelated)}
      ${newCount ? `<div class="pl-modal-meta muted">其余 ${esc(newCount)} 个新字段
        （${TN()}对象里没有）按原规则生成，不参与覆盖。</div>` : ""}`;
  }

  /* 扁平 relations 里没有 objectCode（后端按对象给 items 时才有）：
   * 兜底从 items 里反查一次，保证行上的 key 唯一。 */
  function objOf(vm, fieldCode) {
    const it = linkedItems(vm).find(
      (x) => (x.relations || []).some((r) => r.fieldCode === fieldCode));
    return it ? it.objectCode : "";
  }

  /* 全选 / 全不选：只作用于「能勾的行」，冲突行永远不勾。
   * 全选时多候选行取下拉当前值（默认最匹配 → true）。 */
  function selectAll(on) {
    const rows = confirmRows(_vm).filter((r) => r.status !== "conflict");
    _picked.clear();
    if (on) {
      rows.forEach((r) => {
        const key = rowKey(r.objectCode || objOf(_vm, r.fieldCode), r.fieldCode);
        _picked.set(key, rowPickValue(key));
      });
    }
    const body = document.getElementById("plModalBody");
    if (body && body.querySelectorAll) {
      const boxes = body.querySelectorAll("[data-pl-pick]") || [];
      for (let i = 0; i < boxes.length; i += 1) boxes[i].checked = !!on;
    }
    updateFoot();
    return _picked.size;
  }

  function isPicked(objCode, fieldCode) {
    return _picked.has(rowKey(objCode, fieldCode));
  }

  /* 交给 app.js：{对象编码: {字段编码: true | "项目字段编码"}}
   * 只发勾上的；值为 true = 按最匹配覆盖，字符串 = 按指定的候选覆盖。 */
  function collect() {
    const out = {};
    _picked.forEach((value, key) => {
      const dot = key.lastIndexOf(".");
      const obj = key.slice(0, dot);
      const field = key.slice(dot + 1);
      if (!obj || !field) return;
      out[obj] = out[obj] || {};
      out[obj][field] = (typeof value === "string" && value) ? value : true;
    });
    return out;
  }

  function closeModal() {
    const mask = document.getElementById("plMask");
    if (mask) mask.hidden = true;
    document.body.classList.remove("pl-modal-open");
  }

  function summary(vm) {
    const got = vm || _vm;
    const s = summaryOf(got);
    return `关联${tName(got)} ${((got && got.summary) || {}).linkedObjects || 0} 个对象`
      + ` / 自动覆盖 ${s.auto} 个 / 待确认 ${s.needsConfirm} 个 / 无关 ${s.unrelated} 个`;
  }

  function reset() {
    _vm = null;
    _picked.clear();
    _candChoice.clear();
  }

  return {
    render, openModal, closeModal, summary, reset,
    diffCount, pendingCount, directDiffCount, updateBadge, selectAll, collect, isPicked,
    chooseCandidate,
    // 供探针/调试：分组与差异摘要的纯函数
    groupOf, diffText, confirmRows,
  };
})();
