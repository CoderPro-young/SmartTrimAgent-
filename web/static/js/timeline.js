/* 只读时间线胶片条：按编译 math 展示成品结构（每 clip 一格，宽∝时长）。
   只读不拖拽——遵守 Scope Guard「不做 GUI 轨道编辑器」；点击格跳到源素材预览。 */
import { $, el, fmtClock } from './util.js';
import { showBoundedPreview, thumbUrl } from './materials.js';

let lastPlan = null, lastMath = null;

export function setTimelineData(plan, math){
  lastPlan = plan; lastMath = math;
  render();
}

function render(){
  const box = $('#filmstrip');
  box.replaceChildren();
  if (!lastPlan || !lastMath || !(lastMath.order || []).length){
    box.append(el('span', 'empty', '出片后这里展示成品的时间线结构（片段 / 转场 / 时长）'));
    return;
  }
  const strip = el('div', 'filmstrip');
  const D = lastMath.D || 1;
  const byId = Object.fromEntries((lastPlan.clips || []).map(c => [c.id, c]));
  const kindByName = {};
  for (const c of lastPlan.clips || []) kindByName[c.source] = c.kind;

  (lastMath.order || []).forEach((cid, i) => {
    const c = byId[cid];
    if (!c) return;
    const dur = lastMath.durations[cid] ?? 1;
    const clip = el('div', 'fsclip');
    clip.style.flexGrow = String(Math.max(dur, 0.4));
    const img = el('img');
    img.loading = 'lazy';
    if (c.kind === 'image') img.src = c.source.replace('INPUT/', '/input/');
    else img.src = thumbUrl(c.source.split('/').pop(), (c.trim_start || 0) + 0.3, 128);
    img.alt = '';
    clip.append(img, el('span', 'cid', cid),
                el('span', 'dur', fmtClock(dur)));
    clip.title = `${c.source}\n裁剪 ${c.trim_start ?? 0}–${c.trim_end ?? '尾'}s · 成片中 ${fmtClock(dur)}`
      + ((c.effects || []).length ? '\n效果：' + c.effects.map(e => e.name).join('、') : '');
    clip.onclick = () => showBoundedPreview({
      name: c.source.split('/').pop(),
      kind: c.kind,
      start: c.trim_start ?? 0,
      end: c.trim_end ?? null,
      note: `计划片段 ${cid}`,
    });
    strip.append(clip);

    const tr = (lastMath.transitions || {})[i + 1];
    if (tr && i + 1 < (lastMath.order || []).length){
      strip.append(el('span', 'fstrans', `${tr.type} ${tr.duration}s`));
    }
  });
  box.append(strip);

  const ruler = el('div', 'fsruler');
  ruler.append(el('span', null, '0s'), el('span', null, fmtClock(D / 2)),
               el('span', null, fmtClock(D)));
  box.append(ruler);
}
