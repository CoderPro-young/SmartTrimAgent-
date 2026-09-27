/* 粗筛面板（V6 漏斗第①层）：废料检查报告 + 勾选 + 一键应用。
   判定在后端（culling.py 纯规则），这里只展示与执行用户拍板后的移除。 */
import { $, el } from './util.js';
import { getJSON, postJSON } from './api.js';
import * as chat from './chat.js';
import { loadInputs, thumbUrl } from './materials.js';

let report = null;
let checked = new Set();

export function initCull(){
  $('#cullBtn').onclick = runReport;
}

async function runReport(){
  const btn = $('#cullBtn');
  btn.disabled = true;
  btn.textContent = '检查中…';
  try {
    report = await getJSON('/api/cull-report');
    checked = new Set(report.junk.map(j => j.name));
    render();
  } catch (e) {
    chat.addError('粗筛报告获取失败：' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '🔍 检查废料';
  }
}

function render(){
  const box = $('#cullBody');
  box.replaceChildren();
  if (!report) return;

  const st = report.stats || {};
  const head = el('div', 'cullstats');
  head.append(el('span', null,
    `共 ${st.total} 个素材：建议保留 ${st.keep} · 建议丢弃 ${st.junk}` +
    (st.unindexed ? ` · 未索引 ${st.unindexed}（不判定）` : '')));
  box.append(head);

  if (!report.junk.length){
    box.append(el('span', 'empty', '没有发现废料 —— 全静音/全黑场/画质全差/过短 都不存在。'));
    return;
  }

  const list = el('div', 'culllist');
  for (const j of report.junk){
    const row = el('div', 'cullrow');
    const cb = el('input');
    cb.type = 'checkbox';
    cb.checked = checked.has(j.name);
    cb.onchange = () => {
      cb.checked ? checked.add(j.name) : checked.delete(j.name);
      syncApply();
    };
    row.append(cb);
    const img = el('img', 'thumb');
    img.src = thumbUrl(j.name, 0.5, 84);
    img.loading = 'lazy';
    row.append(img);
    const wrap = el('div', 'nmwrap');
    wrap.append(el('div', 'nm', j.name));
    wrap.append(el('div', 'meta', (j.reasons || []).join('；')));
    row.append(wrap);
    row.dataset.name = j.name;
    list.append(row);
  }
  box.append(list);

  const actions = el('div', 'pactions');
  const apply = el('button', 'ghost danger', `移除选中的 ${checked.size} 个废料`);
  apply.id = 'cullApply';
  apply.onclick = doApply;
  const note = el('span', null, '');
  note.style.cssText = 'font-size:11px;color:var(--dim)';
  note.textContent = '移除可恢复（环境不允许真删时进 TMP/removed/）';
  actions.append(apply, note);
  box.append(actions);
}

function syncApply(){
  const apply = $('#cullApply');
  if (apply) apply.textContent = `移除选中的 ${checked.size} 个废料`;
}

async function doApply(){
  const names = [...checked];
  if (!names.length) return;
  if (!confirm(`确定移除这 ${names.length} 个素材？（可在 TMP/removed/ 找回）`)) return;
  const { ok, status, json } = await postJSON('/api/cull-apply', { names });
  if (!ok){
    chat.addError('应用粗筛失败：' + (json.error || ('HTTP ' + status)));
    return;
  }
  const moved = (json.cleared || []).filter(c => c.mode === 'moved').length;
  chat.addSys(`粗筛已应用：移除 ${(json.cleared || []).length} 个素材` +
    (moved ? `（其中 ${moved} 个因环境限制移入 ${json.cleared.find(c => c.mode === 'moved').detail.split('/')[0]}/）` : '') +
    ((json.errors || []).length ? `；失败 ${json.errors.length} 个` : ''), false);
  report = null;
  $('#cullBody').replaceChildren(el('span', 'empty', '点「检查废料」重新扫描'));
  loadInputs();
}
