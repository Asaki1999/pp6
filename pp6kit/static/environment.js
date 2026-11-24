window.PP6Environment = {id: null, status: null};
async function refreshEnvironment() {
  const response = await fetch('/api/environments');
  if (!response.ok) throw new Error('读取环境失败');
  const data = await response.json();
  PP6Environment.id = data.activeId; PP6Environment.status = data.status;
  const box = document.querySelector('#environmentPanel');
  box.innerHTML = `<summary>当前环境：${esc(data.status.name)}</summary><p class="sub">实施档案不切换数据库连接；平台版本为手工记录，兼容性未核验。映射仅作参考，不覆盖 Excel。</p>
    <select id="environmentSelect">${data.items.map(x => `<option value="${esc(x.id)}" ${x.id === data.activeId ? 'selected' : ''}>${esc(x.name)}</option>`).join('')}</select>
    <form id="environmentForm"><input name="name" placeholder="环境名称" required value="${esc(data.status.name)}"><input name="platformVersion" placeholder="平台版本（未知可留空）" value="${esc(data.status.platformVersion)}"><textarea name="notes" placeholder="备注">${esc(data.status.notes)}</textarea><textarea name="mappings" aria-label="参考映射 JSON">${esc(JSON.stringify(data.status.mappings))}</textarea><label><input type="checkbox" name="rebind">重新绑定当前骨架</label><button class="btn">保存当前</button><button type="button" id="environmentNew">保存为新环境</button><button type="button" id="environmentDelete">删除当前</button></form>
    <p class="sub">元数据来源与采集时间：未知；骨架${data.status.boundSnippetsSha256 === data.status.currentSnippetsSha256 ? '一致' : '已变化，请核对'}</p><div id="environmentError" role="alert"></div>`;
  const change = async (action, payload) => {
    try {
      const res = await fetch('/api/environments/' + action, {method:'POST', body:JSON.stringify(payload)});
      const result = await res.json(); if (!res.ok) throw new Error(result.error || '环境保存失败');
      await refreshEnvironment();
    } catch (error) { document.querySelector('#environmentError').textContent = error.message; }
  };
  box.querySelector('select').onchange = e => change('select', {id:e.target.value});
  const save = newItem => {
    try {
      const values = new FormData(box.querySelector('form'));
      change('save', {id:newItem ? undefined : data.activeId, name:values.get('name'), platformVersion:values.get('platformVersion'), notes:values.get('notes'), mappings:JSON.parse(values.get('mappings')), rebind:values.has('rebind')});
    } catch (e) { document.querySelector('#environmentError').textContent=e.message; }
  };
  box.querySelector('form').onsubmit=e=>{e.preventDefault();save(false);};
  box.querySelector('#environmentNew').onclick=()=>save(true);
  box.querySelector('#environmentDelete').onclick=()=>change('delete',{id:data.activeId});
  window.dispatchEvent(new Event('pp6-environment-change'));
}
refreshEnvironment().catch(e=>{document.querySelector('#environmentPanel').textContent=e.message;});
