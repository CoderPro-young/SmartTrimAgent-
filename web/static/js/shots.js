/* 镜头库（V5 粗剪主界面）：跨素材聚合内容卡片 → 缩略图卡 + 标签筛选 + 区间回放 */
import { $, el, fmtT } from './util.js';
import { showBoundedPreview, thumbUrl, matUrl } from './materials.js';

/* 筛选状态（纯前端过滤 /api/inputs 返回的卡片数据） */
const filters = {
  person: 'all',      // all | with | without
  quality: 'all',     // all | good | ok
  scene: '',          // 具体场景名（下拉聚合）
};

function shotImg(it, shot){
  const img = el('img');
  if (shot.frame){
    img.src = '/probe-frame/' + encodeURIComponent(shot.frame);
    img.onerror = () => { img.src = thumbUrl(it.name, shot.start || 0.5, 184); };
  } else {
    img.src = thumbUrl(it.name, shot.start || 0.5, 184);
  }
  img.loading = 'lazy';
  img.alt = '';
  return img;
}

function shotMatchesFilter(shot){
  const lab = shot.label || {};
  if (filters.person === 'with' && !(lab.person_count >= 1)) return false;
  if (filters.person === 'without' && (lab.person_count || 0) >= 1) return false;
  if (filters.quality === 'good' && lab.quality !== 'good') return false;
  if (filters.quality === 'ok' && lab.quality === 'poor') return false;
  if (filters.scene && (lab.scene || '') !== filters.scene) return false;
  return true;
}

function shotCard(it, shot){
  const card = el('div', 'shotcard');
  const im = el('div', 'im');
  im.append(shotImg(it, shot));
  im.append(el('span', 'rng', `${fmtT(shot.start)}–${fmtT(shot.end)}s`));
  card.append(im);

  const lab = shot.label || {};
  const labBox = el('div', 'lab');
  if (shot.label){
    const head = `${lab.person_count || 0}人 · ${lab.scene || '—'} / ${lab.activity || '—'}`;
    labBox.append(el('div', null, head));
    const sub = [lab.mood, ...(lab.tags || [])].filter(Boolean).join(' · ');
    if (sub) labBox.append(el('div', 'dim', sub));
  } else if (shot.tag_failed){
    labBox.append(el('div', 'dim', '（该镜头打标失败）'));
  } else {
    labBox.append(el('div', 'dim', '（未打标签，仅场景切分）'));
  }
  card.append(labBox);

  const foot = el('div', 'foot');
  if (shot.label) foot.append(el('span', 'qchip ' + (lab.quality || 'ok'), lab.quality || 'ok'));
  const sil = ((it.content || {}).signals || {}).audio;
  if (sil && sil.silence_ratio != null && sil.silence_ratio >= 0.3){
    foot.append(el('span', 'idxstate', `静音${Math.round(sil.silence_ratio * 100)}%`));
  }
  card.append(foot);

  card.title = '点击在上方预览该镜头区间';
  card.onclick = () => showBoundedPreview({
    name: it.name, kind: it.kind, start: shot.start, end: shot.end, note: it.name,
  });
  return card;
}

function filterBar(scenes){
  const bar = el('div', 'shotfilters');

  const mk = (label, key, val) => {
    const c = el('span', 'fchip' + (filters[key] === val ? ' on' : ''), label);
    c.onclick = () => { filters[key] = val; renderShots(lastItems); };
    return c;
  };
  bar.append(el('span', 'dim', '筛选：'));
  bar.append(mk('全部', 'person', 'all'), mk('有人', 'person', 'with'), mk('无人', 'person', 'without'));
  bar.append(el('span', 'tsep'));
  bar.append(mk('不限画质', 'quality', 'all'), mk('画质好', 'quality', 'good'), mk('非差画质', 'quality', 'ok'));

  if (scenes.length > 1){
    bar.append(el('span', 'tsep'));
    const sel = el('select');
    sel.style.cssText = 'font:inherit;font-size:11.5px;padding:2px 6px;background:#12161d;'
      + 'color:var(--text);border:1px solid var(--line);border-radius:6px';
    const optAll = el('option', null, '全部场景');
    optAll.value = '';
    sel.append(optAll);
    for (const s of scenes){
      const o = el('option', null, s); o.value = s;
      sel.append(o);
    }
    sel.value = filters.scene;
    sel.onchange = () => { filters.scene = sel.value; renderShots(lastItems); };
    bar.append(sel);
  }
  return bar;
}

let lastItems = [];

export function renderShots(items){
  lastItems = items || [];
  const box = $('#shotList');
  const meta = $('#shotMeta');
  const withCards = lastItems.filter(it => it.content && !it.content.error
    && (it.content.shots || []).length);
  if (!withCards.length){
    box.replaceChildren(el('span', 'empty',
      '上传视频后会自动建立镜头级内容索引（场景切分 + 语义标签），这里可按标签浏览和筛选镜头'));
    meta.textContent = '';
    return;
  }

  // 聚合场景名（下拉筛选用）
  const scenes = [...new Set(withCards.flatMap(it =>
    (it.content.shots || []).map(s => s.label && s.label.scene).filter(Boolean)))].sort();

  box.replaceChildren();
  box.append(filterBar(scenes));

  let shown = 0, total = 0;
  for (const it of withCards){
    const shots = it.content.shots || [];
    total += shots.length;
    const kept = shots.filter(shotMatchesFilter);
    shown += kept.length;
    if (!kept.length) continue;

    const group = el('div', 'cfile');
    const head = el('div', 'fn', `${it.name} · ${it.content.summary || ''}`);
    group.append(head);
    const grid = el('div', 'shotgrid');
    for (const s of kept) grid.append(shotCard(it, s));
    group.append(grid);
    box.append(group);
  }
  meta.textContent = `${withCards.length} 个素材已索引 · ${shown}/${total} 个镜头`;
}

/* V4 感知层进度条（任务运行中的 analysis 事件） */
export function renderAnalysis(ev){
  const p = $('#anaProg');
  p.hidden = false;
  p.replaceChildren();
  const txt = el('span');
  if (ev.stage === 'start'){
    txt.textContent = `开始分析 ${ev.file}（${ev.kind || ''}）：场景切分 + 抽帧…`;
    p.append(txt);
  } else if (ev.stage === 'sample'){
    txt.textContent = ev.upgrade
      ? `${ev.file}：旧索引升级（补充静音/黑场信号）…`
      : `${ev.file}：抽帧完成（${ev.shots} 个镜头），VLM 打标中…`;
    p.append(txt);
  } else if (ev.stage === 'tag'){
    const pc = ev.total ? Math.round(100 * ev.done / ev.total) : 0;
    txt.textContent = `${ev.file || ''}：VLM 打标 ${ev.done}/${ev.total} 批`;
    const bar = el('span', 'pbar'), i = el('i');
    i.style.width = pc + '%';
    bar.append(i);
    p.append(txt, bar);
  } else if (ev.stage === 'done'){
    txt.textContent = ev.cached
      ? `${ev.file}：读取缓存索引（${ev.shots} 镜头）`
      : `${ev.file}：索引完成（${ev.shots} 镜头）${ev.summary ? ' —— ' + ev.summary : ''}`;
    p.append(txt);
    setTimeout(() => { p.hidden = true; }, 6000);
  }
}

export function hideAnalysis(){ $('#anaProg').hidden = true; }

/* 镜头库缩略图缺帧时的兜底（导出给 timeline 复用） */
export { thumbUrl, matUrl };
