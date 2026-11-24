/* 聚合已有解析证据；不把本地校验误称为目标环境验证。 */
function renderPreflight(d, picked) {
  const box = $("#preflightReport");
  if (!box) return;
  const issues = d.issues || [];
  const errors = issues.filter(i => i.level === "error");
  const warnings = issues.filter(i => i.level === "warn");
  const notes = d.notes || [];
  const list = items => items.length
    ? `<ul>${items.map(text => `<li>${esc(text)}</li>`).join("")}</ul>`
    : '<p class="sub">无记录</p>';
  const status = errors.length ? "存在错误，暂不能出包"
    : !picked.length ? "尚未选择导出模块" : "本地校验通过，待核对目标环境";
  const environment = window.PP6Environment?.status;
  const manual = [
    "当前报告基于输入规格和本地校验，未与目标系统完整比对；不判定新增或覆盖，也不代表实际导入成功。",
    "导入前核对目标环境中的模块、分类、关联对象和字典；导入后检查表单、列表及数据来源。"
  ];
  if (environment) manual.push(`当前环境：${environment.name}；平台版本：${environment.platformVersion || '未知'}；元数据未核验。${environment.boundSnippetsSha256 !== environment.currentSnippetsSha256 ? '骨架已变化，请重新核对兼容性。' : '骨架与档案一致。'}`);
  if ((d.lists || []).some(l => (l.views || []).includes("chart")))
    manual.push("包含图表视图：导入后核对并配置图表样式与数据源。");
  if (state.flowMode) manual.push("流程模式使用固定的开始、审核、通过流程，导入后核对审批人员及业务规则。");
  box.innerHTML = `
    <div class="card-head"><div><h3>出包前检查报告</h3><p class="sub">${esc(status)}</p></div>
      <span class="chip ${errors.length ? 'warn' : 'accent'}">${errors.length} 错误 · ${warnings.length} 警告</span></div>
    <p>本次导出：${esc(picked.map(m => `${m.label}（${m.count} 项）`).join("、") || "未选择模块")}</p>
    <details ${errors.length ? 'open' : ''}><summary>校验问题（${issues.length}）</summary>
      ${list(issues.map(i => `${i.level === 'error' ? '错误' : i.level === 'warn' ? '警告' : '提示'} · ${i.where}：${i.message}`))}</details>
    <details><summary>自动处理与解析提示（${notes.length}）</summary>${list(notes)}</details>
    <details><summary>字段体检（${(d.fieldChecks || []).length}）</summary>
      <p class="sub">在下方「字段体检」页签核对重复字段、类型冲突与候选值差异。</p></details>
    <details open><summary>需要人工核对</summary>${list(manual)}</details>`;
}

