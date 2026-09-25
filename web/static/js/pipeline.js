/* 流水线标签：计划参数卡（T16 回改）/ 数学换算 / 命令序列 / 执行日志 / 历史产物 */
import { $, el, jsonBlock } from './util.js';
import { getJSON } from './api.js';

const TRANSITIONS = ['fade', 'dissolve', 'wipeleft', 'wiperight', 'wipeup', 'wipedown',
  'slideleft', 'slideright', 'slideup', 'slidedown', 'circleopen', 'circleclose'];
const RES_PRESETS = [
  ['1920×1080', 1920, 1080], ['1280×720', 1280, 720],
  ['1080×1080', 1080, 1080], ['720×1280', 720, 1280],
];

/* 工作副本：所有输入框直接改这份对象，replan 时原样提交 */
let workPlan = null;
let editable = false;

export function getWorkPlan(){ return workPlan; }

/* ---------- 计划卡渲染 ---------- */
export function renderPlan(plan, { editable: canEdit = false } = {}){
  workPlan = JSON.parse(JSON.stringify(plan || {}));
  editable = canEdit;
  const box = $('#planCards');
  box.replaceChildren();
  if (!workPlan || (!workPlan.clips && !workPlan.select)){
    box.append(el('span', 'empty', '等待 agent 提交 submit_plan…'));
    $('#planMeta').textContent = '';
    $('#planJson').replaceChildren();
    return;
  }

  if (workPlan.select){
    /* select 宏：展示筛选条件（只读；执行时由编译器展开成 clips） */
    const s = workPlan.select;
    const d = el('div', 'pclip');
    d.append(el('div', 'head'));
    d.append(el('div', null,
      `筛选宏：${(s.sources || []).join('、')}｜条件 ${JSON.stringify(s.where || {})}`
      + (s.budget_seconds != null ? `｜预算 ${s.budget_seconds}s` : '')));
    box.append(d);
  } else {
    const wrap = el('div', 'plancards');
    for (let i = 0; i < (workPlan.clips || []).length; i++){
      wrap.append(clipCard(i));
    }
    box.append(wrap);
    box.append(globalCard());
  }

  const clips = (workPlan.clips || []).length;
  const ov = (workPlan.overlays || []).length;
  $('#planMeta').textContent = `${clips} 素材 · ${ov} 叠加` + (editable ? ' · 可直接改' : '');

  /* 原始 JSON 折叠进「高级」 */
  const jsonDetails = $('#planJson');
  jsonDetails.replaceChildren(jsonBlock(plan));
}

function clipCard(i){
  const c = workPlan.clips[i];
  const card = el('div', 'pclip');

  const head = el('div', 'head');
  head.append(el('span', 'cid', c.id));
  const src = el('span', 'src', c.source);
  head.append(src);
  card.append(head);

  const fields = el('div', 'fields');
  const num = (label, key, step = 0.1) => {
    const f = el('label', 'pfield', label);
    const inp = el('input');
    inp.type = 'number'; inp.step = String(step); inp.min = '0';
    inp.value = c[key] ?? '';
    inp.disabled = !editable;
    inp.onchange = () => { c[key] = parseFloat(inp.value); };
    f.append(inp);
    return f;
  };
  if (c.kind === 'image'){
    fields.append(num('展示秒数', 'duration', 0.5));
  } else {
    fields.append(num('起点 s', 'trim_start'), num('终点 s', 'trim_end'));
  }

  /* 效果 chips（点 × 移除；不可新增——新增效果请用对话说需求） */
  for (const eff of (c.effects || [])){
    const chip = el('span', 'effchip', eff.name);
    if (editable){
      const x = el('span', 'x', '×');
      x.title = '移除该效果';
      x.onclick = () => {
        c.effects = (c.effects || []).filter(e => e !== eff);
        renderPlan(workPlan, { editable });
      };
      chip.append(x);
    }
    fields.append(chip);
  }
  /* 剪除参数（cut_silence/cut_black 已在编译期展开；一般不会再出现） */
  for (const k of ['cut_silence', 'cut_black']){
    if (c[k]) fields.append(el('span', 'effchip', k));
  }
  card.append(fields);

  /* 时间轴位置：转场（挂在当前 clip 的时间轴项上） */
  const tlIdx = (workPlan.timeline || []).findIndex(t => t.clip === c.id);
  if (tlIdx >= 1){
    const tr = (workPlan.timeline[tlIdx].transition = workPlan.timeline[tlIdx].transition
      || { type: 'fade', duration: 0.5 });
    const f = el('label', 'pfield', '转场');
    const sel = el('select');
    for (const t of TRANSITIONS){
      const o = el('option', null, t); o.value = t;
      if (tr.type === t) o.selected = true;
      sel.append(o);
    }
    sel.disabled = !editable;
    sel.onchange = () => { tr.type = sel.value; };
    const dur = el('input');
    dur.type = 'number'; dur.step = '0.1'; dur.min = '0.1';
    dur.value = tr.duration; dur.disabled = !editable;
    dur.style.width = '56px';
    dur.onchange = () => { tr.duration = parseFloat(dur.value); };
    f.append(sel, dur);
    fields.append(f);
  }

  if (editable && (workPlan.clips || []).length > 1){
    const acts = el('div', 'pactions');
    const mk = (label, fn, cls) => {
      const b = el('button', 'ghost' + (cls ? ' ' + cls : ''), label);
      b.onclick = fn;
      return b;
    };
    acts.append(
      mk('↑ 上移', () => moveClip(i, -1)),
      mk('↓ 下移', () => moveClip(i, 1)),
      mk('删除片段', () => removeClip(i), 'danger'),
    );
    card.append(acts);
  }
  return card;
}

function moveClip(i, delta){
  const clips = workPlan.clips, tl = workPlan.timeline;
  const j = i + delta;
  if (j < 0 || j >= clips.length) return;
  [clips[i], clips[j]] = [clips[j], clips[i]];
  [tl[i], tl[j]] = [tl[j], tl[i]];
  renderPlan(workPlan, { editable });
}

function removeClip(i){
  const cid = workPlan.clips[i].id;
  workPlan.clips.splice(i, 1);
  workPlan.timeline = (workPlan.timeline || []).filter(t => t.clip !== cid);
  workPlan.overlays = (workPlan.overlays || []).filter(o => o.at_clip !== cid);
  renderPlan(workPlan, { editable });
}

function globalCard(){
  const out = workPlan.output || {};
  const card = el('div', 'pclip');
  const head = el('div', 'head');
  head.append(el('span', 'src', '输出'));
  card.append(head);
  const fields = el('div', 'fields');

  const fFn = el('label', 'pfield', '文件名');
  const fn = el('input');
  fn.type = 'text'; fn.value = (out.filename || '').replace(/^OUTPUT\//, '');
  fn.style.width = '180px'; fn.disabled = !editable;
  fn.onchange = () => { out.filename = 'OUTPUT/' + fn.value.trim().replace(/^OUTPUT\//, ''); };
  fFn.append(fn);
  fields.append(fFn);

  const fRes = el('label', 'pfield', '分辨率');
  const sel = el('select');
  const cur = `${(out.resolution || {}).width}×${(out.resolution || {}).height}`;
  for (const [label, w, h] of RES_PRESETS){
    const o = el('option', null, label); o.value = w + ',' + h;
    if (label === cur) o.selected = true;
    sel.append(o);
  }
  sel.disabled = !editable;
  sel.onchange = () => {
    const [w, h] = sel.value.split(',').map(Number);
    out.resolution = out.resolution || {};
    out.resolution.width = w; out.resolution.height = h;
  };
  fRes.append(sel);
  fields.append(fRes);

  if (workPlan.audio && workPlan.audio.source){
    fields.append(el('span', 'effchip', 'BGM ' + (workPlan.audio.source || '').split('/').pop()));
  }
  card.append(fields);
  return card;
}

/* ---------- 数学换算 ---------- */
export function renderMath(m){
  const box = $('#mathBody'); box.replaceChildren();
  const grid = el('div', 'stats');
  const order = m.order || [];
  const dl = order.map(id => `${id}=${m.durations[id]}s`).join('  ');
  const sl = order.map(id => `${id}=${m.starts[id]}s`).join('  ');
  for (const [k, v] of [
    ['画布', `${m.width}×${m.height}`],
    ['帧率', `${m.fps} fps`],
    ['片段时长 d_i', dl],
    ['片段起点 S_i', sl],
    ['总时长 D', `${m.D} s`],
  ]){
    const s = el('div', 'stat');
    s.append(el('div', 'k', k), el('div', 'v', v));
    grid.append(s);
  }
  box.append(grid);

  if ((m.overlays || []).length){
    const t = el('div', null);
    t.style.cssText = 'margin-top:11px;font-size:12.5px;color:var(--muted)';
    t.textContent = '叠加绝对时间：' + m.overlays.map(o =>
      `${o.type} @ ${o.at_clip} → ${o.start}s~${o.end}s`).join('　|　');
    box.append(t);
  }
  const trs = Object.entries(m.transitions || {}).filter(([, v]) => v);
  if (trs.length){
    const t = el('div', null);
    t.style.cssText = 'margin-top:7px;font-size:12.5px;color:var(--muted)';
    t.textContent = '转场：' + trs.map(([i, v]) => `#${i} ${v.type} ${v.duration}s`).join('　|　');
    box.append(t);
  }
}

/* ---------- 命令序列 ---------- */
export function renderCommands(cmds){
  const box = $('#cmdBody'); box.replaceChildren();
  for (const c of cmds){
    const d = el('div', 'cmd');
    const meta = el('div', 'meta');
    meta.append(el('span', 'stage ' + c.stage, c.stage), el('span', 'desc', c.description));
    d.append(meta, el('code', null, c.line));
    box.append(d);
  }
}

/* ---------- 执行日志 ---------- */
let logRows = null;
export function resetPipeline(){
  logRows = null;
  $('#mathBody').replaceChildren(el('span', 'empty', '编译后显示片段时长 / 起点 / 总时长'));
  $('#cmdBody').replaceChildren(el('span', 'empty', '等待编译器生成命令…'));
  $('#logBody').replaceChildren(el('span', 'empty', '尚未执行'));
  $('#verifyBox').textContent = '';
}
export function appendLog(ok, desc, detail){
  if (!logRows){ logRows = el('div', 'log'); $('#logBody').replaceChildren(logRows); }
  const row = el('div', 'logrow');
  row.append(el('span', 'badge ' + (ok ? 'ok' : 'fail'), ok ? 'OK' : 'FAIL'),
             el('span', null, desc || ''));
  logRows.append(row);
  if (detail){
    const jb = jsonBlock(detail);
    jb.style.flexBasis = '100%';
    row.append(jb);
  }
}

/* ---------- 历史产物 ---------- */
export async function loadOutputs(){
  try {
    const d = await getJSON('/api/outputs');
    const box = $('#outBody'); box.replaceChildren();
    if (!d.outputs.length){
      box.append(el('span', 'empty', 'OUTPUT/ 下还没有产物'));
      return;
    }
    for (const o of d.outputs){
      const row = el('div', 'logrow');
      const a = el('a', null, o.name);
      a.href = o.url;
      row.append(a, el('span', null, (o.size / 1048576).toFixed(2) + ' MB'));
      box.append(row);
    }
  } catch {}
}
