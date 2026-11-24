
async function refreshHistory() {
  const box = $("#historyItems");
  try {
    const res = await fetch("/api/history", {cache: "no-store"});
    if (!res.ok) throw new Error("读取失败");
    const data = await res.json();
    box.innerHTML = (data.items || []).map(row => `
      <div class="issue"><div><input type="checkbox" data-compare="${esc(row.id)}" aria-label="选择对比记录"><button class="btn" data-history="${esc(row.id)}" data-file="restore">恢复编辑</button><strong>${esc(row.packageName)}</strong>
        <p class="sub">${esc(row.createdAt)} · 工具 ${esc(row.toolVersion)}</p>
        ${[['input.xlsx', '取回原输入'], ['output.zip', '下载历史包'], ['record.json', '参数与报告']]
          .map(([file, label]) => `<button class="btn ghost sm" data-history="${esc(row.id)}" data-file="${file}">${label}</button>`).join(' ')}
      </div></div>`).join('') || '<p class="sub">暂无出包记录</p>';
  } catch (_) { box.textContent = "任务记录读取失败，请刷新重试。"; }
}
$("#refreshHistory")?.addEventListener("click", refreshHistory);
$("#taskHistory")?.addEventListener("toggle", e => { if (e.target.open) refreshHistory(); });
$("#historyItems")?.addEventListener("click", async e => {
  const button = e.target.closest("[data-history]");
  if (!button) return;
  button.disabled = true;
  try {
    if (button.dataset.file === 'restore') {
      const base = '/api/history/' + encodeURIComponent(button.dataset.history);
      const [recordResponse, inputResponse] = await Promise.all([fetch(base+'/record.json'), fetch(base+'/input.xlsx')]);
      if (!recordResponse.ok || !inputResponse.ok) throw new Error('历史文件无法读取');
      await PP6TaskBridge.restore(await recordResponse.json(), new File([await inputResponse.blob()], '恢复任务.xlsx'));
      return;
    }
    const res = await fetch(`/api/history/${encodeURIComponent(button.dataset.history)}/${button.dataset.file}`);
    if (!res.ok) throw new Error("任务文件无法读取");
    await saveBlob(await res.blob(), `PP6-${button.dataset.history.slice(0, 8)}-${button.dataset.file}`);
  } catch (err) { toast(err.message); }
  finally { button.disabled = false; }
});

const compareButton=document.createElement('button');compareButton.className='btn';compareButton.textContent='对比选中的两条记录';
const comparison=document.createElement('pre');comparison.style.whiteSpace='pre-wrap';
$('#taskHistory').append(compareButton,comparison);
compareButton.onclick=async()=>{
  const keys=Array.from(document.querySelectorAll('[data-compare]:checked')).map(e=>e.dataset.compare);
  if(keys.length!==2){comparison.textContent='请选择两条记录';return;}
  try{
    const res=await fetch('/api/history/compare?left='+keys[0]+'&right='+keys[1]);
    const data=await res.json();if(!res.ok)throw new Error(data.error||'比较失败');
    comparison.textContent='产物文件差异（不代表目标数据库变化）\n'+JSON.stringify(data,null,2);
  }catch(e){comparison.textContent=e.message;}
};
