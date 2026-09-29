/* 素材库：列表（缩略图 + 索引状态）/ 上传 / 删除 / 有界预览（素材或镜头区间回放） */
import { $, el, fmtSize } from './util.js';
import { getJSON, postJSON } from './api.js';
import * as chat from './chat.js';
import { renderShots } from './shots.js';

const KIND_LABEL = { video: 'VIDEO', image: 'IMG', audio: 'AUDIO' };
const IDX_LABEL = {
  ready: '已索引', analyzing: '索引中…', failed: '索引失败',
  unsupported: '', none: '未索引',
};

let items = [];               // 最近一次 /api/inputs 的结果
let selectedMat = null;       // 当前预览素材名
let hasMaterials = null;      // null=未知；true/false=已确认（运行键使能）
let onHasMaterials = () => {};
let pollTimer = null;

export function setMaterialsCallback(fn){ onHasMaterials = fn; }
export function getItems(){ return items; }
export function busyMaterials(){ return hasMaterials; }

export function matUrl(name){ return '/input/' + encodeURIComponent(name); }
export function thumbUrl(name, t = 0.5, h = 84){
  return `/thumb/${encodeURIComponent(name)}?t=${t}&h=${h}`;
}

/* ---------- 格式化 ---------- */
function fmtMeta(it){
  const p = it.probe;
  if (!p || !p.ok) return fmtSize(it.size);
  const bits = [];
  if (p.width) bits.push(p.width + '×' + p.height);
  if (p.duration != null) bits.push(p.duration + 's');
  else if (it.kind === 'image') bits.push('静图');
  if (p.fps) bits.push(p.fps + 'fps');
  if (it.kind === 'audio'){
    if (p.channels) bits.push(p.channels + 'ch');
    if (p.sample_rate) bits.push(Math.round(p.sample_rate / 1000) + 'kHz');
    if (p.codec) bits.push(p.codec);
  } else if (p.has_audio === false) bits.push('无音轨');
  bits.push(fmtSize(it.size));
  return bits.join(' · ');
}
function fmtChip(it){
  const p = it.probe;
  if (!p || !p.ok) return fmtSize(it.size);
  const bits = [];
  if (p.width) bits.push(p.width + '×' + p.height);
  if (p.duration != null) bits.push(p.duration + 's');
  else if (it.kind === 'image') bits.push('静图');
  if (it.kind === 'audio' && p.channels) bits.push(p.channels + 'ch');
  bits.push(fmtSize(it.size));
  return bits.join(' · ');
}

/* ---------- 有界预览（点素材行 = 全段；点镜头 = [start,end] 钳制回放） ---------- */
export function showBoundedPreview({ name, kind, start = null, end = null, note = '' }){
  selectedMat = name;
  for (const n of document.querySelectorAll('#matList .mat'))
    n.classList.toggle('on', n.dataset.name === name);

  const box = $('#matPrev'); box.replaceChildren();
  if (!name){ box.append(el('span', 'empty', '点上方素材或镜头卡即可预览')); return; }

  const url = matUrl(name);
  let media;
  if (kind === 'image')      media = el('img');
  else if (kind === 'audio'){ media = el('audio'); media.controls = true; }
  else                       { media = el('video'); media.controls = true; }
  media.src = url;
  if (media.tagName !== 'IMG') media.preload = 'metadata';

  // 镜头区间回放：seek 到 start，timeupdate 钳制到 end（借鉴 LosslessCut
  // 「直接播原文件 + 窗口钳制」，/input 路由支持 Range，无需切片）
  if (kind !== 'image' && start != null){
    media.addEventListener('loadedmetadata', () => {
      try { media.currentTime = start; } catch {}
    });
    if (end != null){
      media.addEventListener('timeupdate', () => {
        if (media.currentTime >= end){ media.pause(); }
      });
    }
  }
  media.onerror = () => {
    media.remove();
    box.prepend(el('div', 'empty',
      '浏览器解不开这个格式（常见于 hevc / mkv），点下方链接用本地播放器打开。'));
  };
  box.append(media);

  if (start != null && end != null){
    box.append(el('span', 'boundhint',
      `区间回放 ${start.toFixed(1)}–${end.toFixed(1)}s${note ? ' · ' + note : ''}，到区间末尾自动停`));
  }

  const it = items.find(x => x.name === name);
  const cap = el('div', 'cap');
  cap.append(el('div', null, 'INPUT/' + name + (it ? ' · ' + fmtMeta(it) : '')));
  const a = el('a', null, '在新标签打开 / 下载');
  a.href = url; a.target = '_blank'; a.rel = 'noopener';
  cap.append(a);
  box.append(cap);
}

/* ---------- 素材行 ---------- */
function matRow(it){
  const node = el('div', 'mat');
  node.dataset.name = it.name;
  node.title = it.path + (it.uploaded
    ? '（网页上传，「启动新任务」时会自动清除）'
    : '（手动放置，不会被自动清除，可在此移除）');

  let thumb;
  if (it.kind === 'image'){
    thumb = el('img', 'thumb'); thumb.src = matUrl(it.name); thumb.loading = 'lazy';
  } else if (it.kind === 'video'){
    const dur = it.probe && it.probe.duration;
    const t = Math.min(0.5, (dur || 1) * 0.1);   // 开头 0.5s 处，避开纯黑首帧
    thumb = el('img', 'thumb');
    thumb.src = thumbUrl(it.name, t);
    thumb.loading = 'lazy';
    thumb.onerror = () => { thumb.remove(); };
  } else {
    thumb = el('span', 'thumb audio', '♪');
  }
  node.append(thumb);

  node.append(el('span', 'kind ' + it.kind, KIND_LABEL[it.kind] || 'FILE'));
  const wrap = el('div', 'nmwrap');
  const nm = el('div', 'nm', it.name);
  const meta = el('div', 'meta', fmtChip(it));
  nm.title = it.path; meta.title = fmtMeta(it);
  wrap.append(nm, meta);
  node.append(wrap);

  if (it.kind !== 'audio'){
    const st = it.analysis_state || 'none';
    const lbl = IDX_LABEL[st] ?? '';
    if (lbl) node.append(el('span', 'idxstate ' + st, lbl));
  }

  const act = el('span', 'act', '引用');
  act.title = '把路径插入到下面的需求输入框';
  act.onclick = e => { e.stopPropagation(); chat.insertIntoComposer(it.path); };
  node.append(act);

  const del = el('span', 'del', '×');
  del.title = '把素材移出 INPUT/（不再提供给 agent）';
  del.onclick = async e => {
    e.stopPropagation();
    if (!confirm('把素材 ' + it.name + ' 移出 INPUT/ ？')) return;
    if (selectedMat === it.name) showBoundedPreview({});   // 先停预览：浏览器还在拉流时，Windows 删不掉被打开的文件
    const { ok, status, json } = await postJSON('/api/delete', { name: it.name });
    if (!ok){ chat.addError(json.error || `移除失败（HTTP ${status}）`); return; }
    chat.addSys(json.mode === 'moved'
      ? `已把「${it.name}」移出素材区（文件被占用暂不能真删，移到了 ${json.detail}）`
      : `已删除素材「${it.name}」`, false);
    loadInputs();
  };
  node.append(del);

  node.onclick = () => showBoundedPreview({ name: it.name, kind: it.kind });
  return node;
}

/* ---------- 加载 + 轮询（有素材在建立索引时自动刷新） ---------- */
export async function loadInputs(){
  const box = $('#matList');
  try {
    const d = await getJSON('/api/inputs?probe=1');
    items = d.inputs || [];
    hasMaterials = items.length > 0;
    onHasMaterials(hasMaterials);
    box.replaceChildren();
    if (!items.length){
      box.append(el('span', 'empty', '还没有素材 —— 点下方「＋ 上传素材」，或把文件拖到左侧面板'));
      showBoundedPreview({});
    } else {
      for (const it of items) box.append(matRow(it));
      const sel = items.find(x => x.name === selectedMat);
      if (sel) showBoundedPreview({ name: sel.name, kind: sel.kind });
    }
    $('#matMeta').textContent = items.length ? `${items.length} 个素材` : '';
    renderShots(items);          // 镜头库与素材列表同一份数据
    schedulePoll();
  } catch {
    hasMaterials = null;
    onHasMaterials(null);
    box.replaceChildren(el('span', 'empty', '素材列表加载失败'));
  }
}

function schedulePoll(){
  const anyAnalyzing = items.some(it => it.analysis_state === 'analyzing');
  if (anyAnalyzing && !pollTimer){
    pollTimer = setInterval(() => {
      if (!items.some(it => it.analysis_state === 'analyzing')){
        clearInterval(pollTimer); pollTimer = null; return;
      }
      loadInputs();
    }, 3000);
  } else if (!anyAnalyzing && pollTimer){
    clearInterval(pollTimer); pollTimer = null;
  }
}

/* ---------- 上传 ---------- */
function uploadFile(file){
  return new Promise(resolve => {
    const box = $('#matList');
    const isImg = /^image\//.test(file.type) || /\.(png|jpe?g|jfif|webp|bmp|gif|tiff?|heic|avif)$/i.test(file.name);
    const kind = isImg ? 'image' : 'video';
    const node = el('div', 'mat up');
    node.append(el('span', 'kind ' + kind, isImg ? 'IMG' : 'VIDEO'));
    const wrap = el('div', 'nmwrap');
    const meta = el('div', 'meta', '上传中 0%');
    wrap.append(el('div', 'nm', file.name), meta);
    node.append(wrap);
    const bar = el('div', 'bar');
    node.append(bar);
    const empty = box.querySelector('.empty');
    if (empty) empty.remove();
    box.prepend(node);

    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/upload?name=' + encodeURIComponent(file.name));
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.upload.onprogress = e => {
      if (!e.lengthComputable) return;
      const pc = Math.round(e.loaded / e.total * 100);
      meta.textContent = '上传中 ' + pc + '%';
      bar.style.width = pc + '%';
    };
    xhr.onload = () => {
      let j = {};
      try { j = JSON.parse(xhr.responseText); } catch {}
      if (xhr.status === 200 && j.ok){
        meta.textContent = '索引排队中…';
        if (j.renamed) chat.addSys(`「${file.name}」与已有文件重名，已存为 ${j.name}`, false);
        loadInputs();
        resolve(true);
      } else {
        node.classList.remove('up');
        node.classList.add('err');
        meta.textContent = '已拒绝';
        bar.remove();
        const del = el('span', 'del', '×');
        del.onclick = ev => { ev.stopPropagation(); node.remove(); };
        node.append(del);
        chat.addError(`上传 ${file.name} 被拒绝：${j.error || ('HTTP ' + xhr.status)}`);
        resolve(false);
      }
    };
    xhr.onerror = () => {
      node.classList.remove('up'); node.classList.add('err');
      meta.textContent = '网络错误'; bar.remove();
      resolve(false);
    };
    xhr.send(file);
  });
}

async function uploadFiles(files){
  const list = Array.from(files || []);
  if (!list.length) return;
  chat.addSys(`开始上传 ${list.length} 个素材…`, true);
  let ok = 0;
  for (const f of list) if (await uploadFile(f)) ok++;
  chat.addSys(`上传完成：${ok}/${list.length} 个可用。上传后自动建立内容索引（后台进行）。`, false);
}

export function clearSelected(){ selectedMat = null; showBoundedPreview({}); }

export function initUploads(){
  $('#pickBtn').onclick = () => $('#fileInput').click();
  $('#fileInput').onchange = e => { uploadFiles(e.target.files); e.target.value = ''; };

  const col = document.querySelector('.col.left');
  const over = $('#dropover');
  let depth = 0;
  col.addEventListener('dragenter', e => { e.preventDefault(); depth++; over.classList.add('on'); });
  col.addEventListener('dragover', e => e.preventDefault());
  col.addEventListener('dragleave', () => { if (--depth <= 0){ depth = 0; over.classList.remove('on'); } });
  col.addEventListener('drop', e => {
    e.preventDefault(); depth = 0; over.classList.remove('on');
    if (e.dataTransfer && e.dataTransfer.files) uploadFiles(e.dataTransfer.files);
  });
  window.addEventListener('dragover', e => e.preventDefault());
  window.addEventListener('drop', e => e.preventDefault());
}
