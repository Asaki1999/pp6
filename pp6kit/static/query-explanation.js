window.PP6QueryExplanation = function (data) {
  let panel = document.getElementById('queryExplanation');
  if (!panel) {
    panel = document.createElement('details');
    panel.id = 'queryExplanation';
    const meta = document.getElementById('qResultMeta');
    // 插在「结果」标题行之后，不钻进标题行的 flex 里；旧模板没有 .q-meta-row 时退回原位
    const row = meta && meta.closest ? meta.closest('.q-meta-row') : null;
    const host = row || meta;
    if (host && host.parentNode) host.after(panel);
  }
  const info = data && data.explanation;
  panel.hidden = !info;
  if (!info) {panel.innerHTML='';return;}
  panel.innerHTML='<summary>本次查询口径（SQL 结构）</summary><p>业务定义、币种与关联去重是否正确仍需核对。</p>' +
    [['tables','涉及表'],['filters','过滤'],['having','聚合后过滤'],['groups','分组'],['aggregates','聚合表达式'],['limits','行数限制']].map(([key,label])=>`<p><b>${label}</b>：${qEsc((info[key]||[]).join('；')||'无或未识别')}</p>`).join('')+`<pre>${qEsc(info.sql)}</pre>`;
};
