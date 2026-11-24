window.PP6Environment = {id: null, status: null};
async function refreshEnvironment() {
  const response = await fetch('/api/environments');
  if (!response.ok) throw new Error('读取环境失败');
  const data = await response.json();
  PP6Environment.id = data.activeId; PP6Environment.status = data.status;
  const box = document.querySelector('#environmentPanel');
  /* 2026-11-24 用户反馈右栏「有点丑」：长说明压成一句；名称成为标题旁的徽标；
     表单字段带标签；参考映射 JSON 与「重新绑定」收进「高级」折叠区；三个按钮排成一行。 */
  box.innerHTML = `<summary><span>当前环境</span><b class="env-name" title="${esc(data.status.name)}">${esc(data.status.name)}</b></summary>
    <p class="sub env-lead">环境档案不改变数据库连接</p>
    <select id="environmentSelect" aria-label="切换环境">${data.items.map(x => `<option value="${esc(x.id)}" ${x.id === data.activeId ? 'selected' : ''}>${esc(x.name)}</option>`).join('')}</select>
    <details class="env-fold env-edit"><summary>编辑当前环境</summary>
    <form id="environmentForm">
      <label>名称<input name="name" placeholder="环境名称" required value="${esc(data.status.name)}"></label>
      <label>平台版本<input name="platformVersion" placeholder="未知可留空" value="${esc(data.status.platformVersion)}"></label>
      <label>备注<textarea name="notes" placeholder="备注">${esc(data.status.notes)}</textarea></label>
      <details class="env-fold env-adv"><summary>高级：参考映射与重新绑定</summary>
        <textarea name="mappings" aria-label="参考映射 JSON">${esc(JSON.stringify(data.status.mappings))}</textarea>
        <label class="env-check"><input type="checkbox" name="rebind">重新绑定当前骨架</label>
      </details>
      <div class="env-actions">
        <button class="btn">保存当前</button>
        <button type="button" class="btn" id="environmentNew">保存为新环境</button>
        <button type="button" class="btn danger" id="environmentDelete">删除当前</button>
      </div>
    </form>
    </details>
    <p class="hint" title="元数据来源与采集时间：未知">骨架${data.status.boundSnippetsSha256 === data.status.currentSnippetsSha256 ? '与当前一致' : '已变化，请核对'}</p>
    <div id="environmentError" role="alert"></div>`;
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
