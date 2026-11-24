
/* 时间显示成人话：今天 11:56 / 昨天 17:40 / 11-22 09:12。
   之前直接显示 2026-11-24T11:56:54+08:00，又长又难读（2026-11-24 用户反馈右栏「有点丑」）。 */
function histWhen(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return String(iso || "");
  const p = n => String(n).padStart(2, "0");
  const day = x => `${x.getFullYear()}-${x.getMonth()}-${x.getDate()}`;
  const hm = `${p(d.getHours())}:${p(d.getMinutes())}`;
  const now = new Date(), yesterday = new Date();
  yesterday.setDate(now.getDate() - 1);
  if (day(d) === day(now)) return `今天 ${hm}`;
  if (day(d) === day(yesterday)) return `昨天 ${hm}`;
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${hm}`;
}

async function refreshHistory() {
  const box = $("#historyItems");
  try {
    const res = await fetch("/api/history", {cache: "no-store"});
    if (!res.ok) throw new Error("读取失败");
    const data = await res.json();
    box.innerHTML = (data.items || []).map(row => `
      <div class="hist-item">
        <input type="checkbox" data-compare="${esc(row.id)}" aria-label="选择对比记录">
        <div class="hist-main">
          <button class="hist-open" data-history="${esc(row.id)}" data-file="restore" title="取回原输入继续修改">
            <b>${esc(row.packageName)}</b>
          </button>
          <span class="hist-meta">${esc(histWhen(row.createdAt))}</span>
          <div class="hist-acts">
            ${[['input.xlsx', '取回原输入'], ['output.zip', '下载历史包'], ['record.json', '参数与报告']]
              .map(([file, label]) => `<button data-history="${esc(row.id)}" data-file="${file}">${label}</button>`).join('')}
          </div>
        </div>
      </div>`).join('') || '<div class="hist-empty"><b>暂无出包记录</b><span>成功出包后自动保存在本机</span></div>';
  } catch (_) { box.innerHTML = '<div class="hist-empty"><b>任务记录读取失败</b><span>点右上角按钮重试</span></div>'; }
  // 没有记录（或读取失败）时，「对比选中的两条记录」无从谈起，连同底栏一起收起
  const foot = box.parentElement.querySelector('.hist-foot');
  if (foot) foot.hidden = !!box.querySelector('.hist-empty');
}
/* 刷新按钮在 <summary> 里：不阻止默认行为会把整张卡片折叠起来 */
$("#refreshHistory")?.addEventListener("click", e => { e.preventDefault(); refreshHistory(); });
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

/* 对比区固定在卡片底部：按钮与结果框都在 index.html 里（#historyCompare / #historyCompareOut），
   不再往卡片上 append 没有内边距的按钮和 <pre>——那会让它们贴边、被卡片裁掉。 */
const compareButton = $('#historyCompare');
const comparison = $('#historyCompareOut');
function showCompare(text, hint) {
  comparison.hidden = false;
  comparison.textContent = text;
  comparison.classList.toggle('hint', !!hint);
}
compareButton.onclick = async () => {
  const keys = Array.from(document.querySelectorAll('[data-compare]:checked')).map(e => e.dataset.compare);
  if (keys.length !== 2) { showCompare('请选择两条记录', true); return; }
  try {
    const res = await fetch('/api/history/compare?left=' + keys[0] + '&right=' + keys[1]);
    const data = await res.json(); if (!res.ok) throw new Error(data.error || '比较失败');
    showCompare('产物文件差异（不代表目标数据库变化）\n' + JSON.stringify(data, null, 2));
  } catch (e) { showCompare(e.message, true); }
};

/* 首屏右栏默认展开「实施任务记录」：details 的 toggle 不会为初始 open 触发，
   不主动拉一次就只剩一句说明。失败由 refreshHistory 自己兜住，不会抛到全局。 */
if (document.querySelector("#taskHistory")?.open) refreshHistory();
