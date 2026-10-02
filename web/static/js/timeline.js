/* 成片时间线胶片条：按编译 math 的 starts/durations 精确布局（转场重叠如实呈现）。
   播放头与播放器双向联动：条上单击/拖动定位成片进度，播放时同步移动并高亮当前片段；
   双击片段回看源素材。只读展示、不做轨道编辑（Scope Guard 维持）。 */
import { $, el, fmtClock } from './util.js';
import { showBoundedPreview, thumbUrl } from './materials.js';

let lastPlan = null, lastMath = null;
let playheadEl = null, stripEl = null;
let playerBound = false, scrubbing = false, wasPlaying = false;

export function setTimelineData(plan, math){
  lastPlan = plan; lastMath = math;
  bindPlayer();
  render();
}

/* ---- 播放器联动（首次出片时绑一次） ---- */
function bindPlayer(){
  if (playerBound) return;
  const p = $('#player');
  if (!p) return;
  playerBound = true;
  p.addEventListener('timeupdate', () => { updatePlayhead(); updateTransport(); });
  p.addEventListener('seeked', () => { updatePlayhead(); updateTransport(); });
  p.addEventListener('loadedmetadata', () => { updatePlayhead(); updateTransport(); });
  ['play', 'pause', 'ended', 'emptied'].forEach(ev =>
    p.addEventListener(ev, updateTransport));

  const btn = $('#tlPlay');
  if (btn) btn.onclick = togglePlay;

  /* 空格播放/暂停（输入框内不拦截） */
  document.addEventListener('keydown', e => {
    if (e.code !== 'Space' || e.repeat) return;
    const t = e.target;
    if (t && (t.tagName === 'TEXTAREA' || t.tagName === 'INPUT'
              || t.isContentEditable)) return;
    if (!p.src) return;
    e.preventDefault();
    togglePlay();
  });
}

function togglePlay(){
  const p = $('#player');
  if (!p || !p.src) return;
  if (p.paused || p.ended) p.play().catch(() => {});
  else p.pause();
}

function updateTransport(){
  const p = $('#player');
  const btn = $('#tlPlay'), time = $('#tlTime');
  if (!p || !btn || !time) return;
  const ready = !!p.src && isFinite(p.duration) && p.duration > 0;
  btn.disabled = !ready;
  btn.textContent = (ready && !p.paused && !p.ended) ? '⏸ 暂停' : '▶ 播放';
  time.textContent = ready
    ? `${fmtClock(p.currentTime)} / ${fmtClock(p.duration)}`
    : '0s / —';
}

function fracFromEvent(e){
  const rect = stripEl.getBoundingClientRect();
  return Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
}

function seekFrac(frac){
  const p = $('#player');
  if (!p || !isFinite(p.duration) || p.duration <= 0) return;
  p.currentTime = Math.min(p.duration - 0.05, Math.max(0, frac * p.duration));
  updatePlayhead();
}

function updatePlayhead(){
  const p = $('#player');
  if (!p || !playheadEl || !isFinite(p.duration) || p.duration <= 0) return;
  const frac = p.currentTime / p.duration;
  playheadEl.style.left = (frac * 100).toFixed(2) + '%';
  highlightAt(frac * (lastMath?.D || 0));
}

function highlightAt(t){
  if (!stripEl || !lastMath) return;
  let active = null;
  for (const cid of lastMath.order || []){
    const s = lastMath.starts[cid] || 0;
    const d = lastMath.durations[cid] || 0;
    if (t >= s - 1e-6 && t < s + d){ active = cid; break; }
  }
  for (const c of stripEl.querySelectorAll('.fsclip'))
    c.classList.toggle('on', c.dataset.cid === active);
}

/* ---- 渲染 ---- */
function render(){
  const box = $('#filmstrip');
  box.replaceChildren();
  playheadEl = null; stripEl = null;
  if (!lastPlan || !lastMath || !(lastMath.order || []).length || !lastMath.D){
    box.append(el('span', 'empty',
      '出片后这里展示成片结构：拖动播放头定位进度，双击片段回看源素材'));
    return;
  }
  const D = lastMath.D;
  const byId = Object.fromEntries((lastPlan.clips || []).map(c => [c.id, c]));

  const strip = el('div', 'filmstrip');
  stripEl = strip;

  (lastMath.order || []).forEach((cid, i) => {
    const c = byId[cid];
    if (!c) return;
    const d = lastMath.durations[cid] ?? 1;
    const s = lastMath.starts[cid] ?? 0;
    const clip = el('div', 'fsclip');
    clip.dataset.cid = cid;
    clip.style.left = (s / D * 100).toFixed(3) + '%';
    clip.style.width = (d / D * 100).toFixed(3) + '%';
    const img = el('img');
    img.loading = 'lazy'; img.alt = ''; img.draggable = false;
    if (c.kind === 'image') img.src = c.source.replace('INPUT/', '/input/');
    else img.src = thumbUrl(c.source.split('/').pop(), (c.trim_start || 0) + 0.3, 128);
    clip.append(img, el('span', 'cid', cid), el('span', 'dur', fmtClock(d)));
    clip.title = `${c.source}\n裁剪 ${c.trim_start ?? 0}–${c.trim_end ?? '尾'}s`
      + ` · 成片中 ${fmtClock(s)}–${fmtClock(s + d)}`
      + ((c.effects || []).length ? '\n效果：' + c.effects.map(e => e.name).join('、') : '')
      + '\n双击回看源片段';
    clip.ondblclick = () => showBoundedPreview({
      name: c.source.split('/').pop(),
      kind: c.kind,
      start: c.trim_start ?? 0,
      end: c.trim_end ?? null,
      note: `计划片段 ${cid}`,
    });
    strip.append(clip);

    const tr = (lastMath.transitions || {})[i + 1];
    if (tr && i + 1 < (lastMath.order || []).length){
      const nid = lastMath.order[i + 1];
      const badge = el('span', 'fstrans', `${tr.type} ${tr.duration}s`);
      badge.style.left = (((lastMath.starts[nid] ?? 0) / D) * 100).toFixed(3) + '%';
      strip.append(badge);
    }
  });

  const ph = el('div', 'fsplayhead');
  ph.style.left = '0%';
  playheadEl = ph;
  strip.append(ph);
  box.append(strip);

  /* 单击 / 拖动定位：pointer 统一处理（CSS 已设 touch-action:none） */
  strip.addEventListener('pointerdown', e => {
    if (e.button !== 0) return;
    const p = $('#player');
    scrubbing = true;
    wasPlaying = !!(p && !p.paused && !p.ended);
    if (p) p.pause();
    try { strip.setPointerCapture(e.pointerId); } catch {}
    seekFrac(fracFromEvent(e));
  });
  strip.addEventListener('pointermove', e => {
    if (scrubbing) seekFrac(fracFromEvent(e));
  });
  const endScrub = () => {
    if (!scrubbing) return;
    scrubbing = false;
    const p = $('#player');
    if (wasPlaying && p) p.play().catch(() => {});
    wasPlaying = false;
  };
  strip.addEventListener('pointerup', endScrub);
  strip.addEventListener('pointercancel', endScrub);

  const ruler = el('div', 'fsruler');
  ruler.append(el('span', null, '0s'), el('span', null, fmtClock(D / 2)),
               el('span', null, fmtClock(D)));
  box.append(ruler);
}
