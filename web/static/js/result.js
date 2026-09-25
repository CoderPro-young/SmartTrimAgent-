/* 成品标签：播放器 + 审阅报告（T18：这次实际做了什么） */
import { $, el, fmtClock } from './util.js';
import { setTimelineData } from './timeline.js';

export function showResult(ev){
  const card = $('#cardResult');
  card.hidden = false;
  const p = $('#player');
  p.src = ev.url;
  const a = $('#dl');
  a.href = ev.url;
  a.setAttribute('download', (ev.output || '').split('/').pop());
  $('#outMeta').textContent = ev.output;
  renderReport(ev.report);
  $('#tabResult').click();
  card.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function rline(parts){
  const l = el('div', 'rline');
  for (const x of parts) l.append(x);
  return l;
}

export function renderReport(rep){
  const box = $('#reportBody');
  box.replaceChildren();
  if (!rep){ box.append(el('span', 'empty', '—')); return; }
  const r = el('div', 'report');

  const b = el('b', null, `本片 ${rep.clips} 个片段 · 总时长 ${fmtClock(rep.duration)}`);
  r.append(rline([b]));

  if (rep.cuts && rep.cuts.length){
    const total = rep.removed_total_seconds ?? 0;
    const segs = rep.cuts.reduce((n, c) => n + (c.kept_segments || 0), 0);
    r.append(rline([
      el('b', null, `已剪除静音/黑场 ${total}s`),
      el('span', null, `（${rep.cuts.length} 个素材展开为 ${segs} 个保留片段）`),
    ]));
  }
  if (rep.select){
    r.append(rline([
      el('b', null, `筛选命中 ${rep.selected_shots ?? (rep.select.picked || []).length} 个镜头`),
      el('span', 'sub', `（按条件自动挑选，总长 ${fmtClock(rep.select.total_seconds)}s）`),
    ]));
  }
  if ((rep.effects || []).length){
    r.append(rline([el('span', null, '效果：' + rep.effects.join('、'))]));
  }
  if (rep.transitions){
    r.append(rline([el('span', null, `转场 ${rep.transitions} 处`)]));
  }
  if (rep.bgm) r.append(rline([el('span', null, '含 BGM 配乐')]));
  if ((rep.overlays || 0) > 0) r.append(rline([el('span', null, `叠加 ${rep.overlays} 处`)]));

  r.append(el('span', 'rtag', '✓ 生成完毕，可直接下载发布；要微调请用左侧对话或流水线标签的参数卡'));
  box.append(r);
}

/* done 之后把时间线数据喂给胶片条（放在这里：result 与 timeline 同标签联动） */
export function feedTimeline(plan, math){
  setTimelineData(plan, math);
}
