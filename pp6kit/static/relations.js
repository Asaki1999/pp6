/* 所属关系确认组件：解析页与 AI 预览页共用，不复制业务规则。
 *
 * 数据来自后端统一 view model（parse 与 AI 三个入口同构的 relationships 字段）：
 *   { status, objectSource, historySource, historySourceLabel, notice, parentOptions, items:[...] }
 *
 * 产品红线（与后端一致，勿改）：
 *   - 自动建议默认「不建立」，用户点选某条候选才建立；
 *   - 每个对象最多一条关系，选第二条自动替换第一条（radio 天然互斥）；
 *   - PARALLEL 只能手动选，PARENT 旁提示数据归属影响。
 */
window.Relations = (function () {
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
  const CONF_LABEL = { high: "高", medium: "中", low: "低" };
  const _cancel = new Set();      // 用户显式取消的 Excel 关系（bo_code）

  function render(box, vm, opts = {}) {
    const scope = opts.scope || "parse";
    if (!box) return;
    if (!vm || !(vm.items || []).length) {
      box.hidden = true;
      box.innerHTML = "";
      return;
    }
    const hasAny = vm.items.some((it) =>
      (it.candidates || []).length || (it.excelRelation && it.excelRelation.specified));
    if (!hasAny) {
      box.hidden = true;
      box.innerHTML = "";
      return;
    }
    box.hidden = false;

    const cards = vm.items.filter((it) =>
      (it.candidates || []).length || (it.excelRelation && it.excelRelation.specified)
    ).map((it) => {
      const cands = (it.candidates || []).map((c) => `
        <label class="rel-cand">
          <input type="radio" name="rel-${esc(it.objectCode)}"
                 data-parent="${esc(c.parentObj)}" data-field="${esc(c.fieldCode)}">
          <span class="rel-cand-main">
            <span class="rel-parent">
              ${esc(c.parentName)} <code>${esc(c.parentObj)}</code>
              ${c.packageNew
                ? '<span class="chip">本包新建</span>'
                : '<span class="chip muted">系统已有</span>'}
              ${c.historyParentCount ? `<span class="muted">· 已有 ${c.historyParentCount} 个对象挂其下</span>` : ""}
            </span>
            <span class="rel-field">关联字段：<code>${esc(c.parentField)}</code>（${esc(c.rule)}）</span>
            ${(c.evidence || []).length
              ? `<span class="rel-evidence muted">${c.evidence.map(esc).join(" · ")}</span>` : ""}
          </span>
          <span class="rel-conf conf-${esc(c.confidence)}">${CONF_LABEL[c.confidence] || c.confidence}</span>
        </label>`).join("");

      const excel = it.excelRelation && it.excelRelation.specified ? `
        <div class="rel-excel ${it.excelRelation.valid ? "" : "bad"}">
          <span class="chip warn">Excel 已指定</span>
          <span>父对象 <code>${esc(it.excelRelation.parentObj)}</code>
             · 字段 <code>${esc(it.excelRelation.parentField || "自动")}</code>
             · ${esc(it.excelRelation.relType)}</span>
          ${(it.excelRelation.errors || []).map((e) => `<div class="muted">⚠ ${esc(e)}</div>`).join("")}
          ${it.excelRelation.valid
            ? `<button class="btn ghost sm" data-rel-cancel="${esc(it.objectCode)}">取消此关系</button>` : ""}
        </div>` : "";

      return `
      <div class="rel-card" data-rel-card="${esc(it.objectCode)}" data-rel-scope="${esc(scope)}">
        <div class="rel-card-title">
          <strong>${esc(it.objectName)}</strong> <code>${esc(it.objectCode)}</code>
        </div>
        ${excel}
        ${cands
          ? `<div class="rel-cands">${cands}</div>`
          : '<div class="muted">未识别到可建立的关系，可去 Excel 的「父对象编码」列手动填</div>'}
        <div class="rel-type" data-rel-type hidden>
          关系类型：
          <label><input type="radio" name="reltype-${esc(it.objectCode)}" value="PARENT" checked>
            PARENT（父子，会决定数据归属与列表范围）</label>
          <label><input type="radio" name="reltype-${esc(it.objectCode)}" value="PARALLEL">
            PARALLEL（平行关联）</label>
        </div>
      </div>`;
    }).join("");

    box.innerHTML = `
      <div class="rel-head">
        <strong>所属关系确认</strong>
        <span class="chip muted">${esc(vm.historySourceLabel || vm.historySource || "无历史")}</span>
        <span class="muted rel-head-tip">自动识别仅供参考，默认不建立；勾选后才生成父子/平行关系</span>
      </div>
      ${vm.notice ? `<div class="rel-notice">${esc(vm.notice)}</div>` : ""}
      <div class="rel-cards">${cards}</div>
      ${opts.actions ? opts.actions : ""}`;

    box.querySelectorAll('input[type="radio"][data-parent]').forEach((r) => {
      r.addEventListener("change", () => {
        const card = r.closest("[data-rel-card]");
        const wrap = card && card.querySelector("[data-rel-type]");
        if (wrap) wrap.hidden = false;
      });
    });
    box.querySelectorAll("[data-rel-cancel]").forEach((b) => {
      b.addEventListener("click", () => {
        _cancel.add(b.dataset.relCancel);
        b.closest(".rel-excel").classList.add("is-cancelled");
        b.textContent = "已取消（出包时不再生成这条关系）";
        b.disabled = true;
      });
    });
  }

  /* 收集用户决策 → X-Relations 的 decisions 对象。只含用户动过的对象。 */
  function collect(scope) {
    const decisions = {};
    const sel = scope
      ? `[data-rel-card][data-rel-scope="${scope}"]`
      : "[data-rel-card]";
    document.querySelectorAll(sel).forEach((card) => {
      const bo = card.dataset.relCard;
      if (_cancel.has(bo)) {
        decisions[bo] = { enabled: false };
        return;
      }
      const picked = card.querySelector('input[type="radio"][data-parent]:checked');
      if (!picked) return;              // 没选 = 不建立（自动建议永不生效）
      const relType = (card.querySelector(`input[name="reltype-${bo}"]:checked`) || {}).value || "PARENT";
      decisions[bo] = {
        enabled: true,
        parentObj: picked.dataset.parent,
        fieldCode: picked.dataset.field,
        relType,
      };
    });
    return decisions;
  }

  function decisionsHeader(scope) {
    const d = collect(scope);
    return Object.keys(d).length ? JSON.stringify({ version: 1, decisions: d }) : null;
  }

  function reset() {
    _cancel.clear();
  }

  return { render, collect, decisionsHeader, reset };
})();
