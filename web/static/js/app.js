/* app.js —— 入口与胶水：会话（登记/切换/持久化）/ 标签页 / 运行流 / 事件分发 / 引导 */
import { $ } from './util.js';
import { ndjsonStream, getJSON, postJSON } from './api.js';
import * as chat from './chat.js';
import * as materials from './materials.js';
import * as shots from './shots.js';
import * as pipeline from './pipeline.js';
import * as result from './result.js';
import * as sess from './sessions.js';
import { initTemplates } from './templates.js';
import { initCull } from './cull.js';

/* ---------- 会话 ---------- */
const PH_FIRST = '先用一句话描述要剪成什么样，例如：把这两段拼起来，中间加 0.5 秒 fade 转场，输出 720p';
const PH_MORE  = '继续提修改要求，例如：把花字改成 3 秒 / 输出换成 1080p；没看懂它的反问也可以直接回答';
const WELCOME  = '还没有素材 —— 先把视频/图片拖进左侧，再用一句话描述要剪成什么样。';

function _newSessionId(){
  return (window.crypto && crypto.randomUUID)
    ? crypto.randomUUID()
    : 's' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
}
let sessionId = localStorage.getItem('st_session');
if (!sessionId){ sessionId = _newSessionId(); localStorage.setItem('st_session', sessionId); }
let turnCount = parseInt(localStorage.getItem('st_turns_' + sessionId) || '0', 10);

/* 本轮运行的成品快照（持久化到 st_state_<id>，切会话 / 刷新页面可恢复） */
let lastDone = null, lastExecPlan = null, lastMath = null, lastVerify = null;

function refreshPlaceholder(){
  $('#task').placeholder = turnCount > 0 ? PH_MORE : PH_FIRST;
}

/* ---------- 每会话持久化：对话 st_chat_<id> + 成品状态 st_state_<id> ---------- */
function firstUserText(){
  const u = chat.getTranscript().find(e => e.k === 'user');
  if (!u || !u.t) return '';
  const t = u.t.replace(/\s+/g, ' ').trim();
  return t.length > 24 ? t.slice(0, 24) + '…' : t;
}

/* 空会话（没发过消息、没有成品快照）不入册不落盘，
   否则连点几次「启动新任务」下拉里就堆一列「未命名任务」 */
function sessionHasContent(){
  return chat.getTranscript().some(e => e && (e.k === 'user' || e.k === 'assistant'))
    || !!(lastDone || lastExecPlan || lastMath || lastVerify);
}

function paintSessionSelect(){
  sess.renderSelect($('#sessSel'), sessionId, switchSession,
    sessionHasContent() ? '当前任务' : '当前任务（未开始）');
}

function saveChat(){
  if (!sessionHasContent()){
    try { localStorage.removeItem('st_chat_' + sessionId); } catch {}
    paintSessionSelect();
    return;
  }
  try {
    localStorage.setItem('st_chat_' + sessionId, JSON.stringify(chat.getTranscript()));
  } catch {                         // 配额超限：丢工具调用参数再存一次
    try {
      const slim = chat.getTranscript().map(e => e.tcs ? { ...e, tcs: null } : e);
      localStorage.setItem('st_chat_' + sessionId, JSON.stringify(slim));
    } catch {}
  }
  sess.touch(sessionId, firstUserText());
  paintSessionSelect();
}

function saveState(){
  try {
    if (lastDone || lastExecPlan || lastMath || lastVerify){
      localStorage.setItem('st_state_' + sessionId, JSON.stringify(
        { ev: lastDone, plan: lastExecPlan, math: lastMath, verify: lastVerify }));
    } else {
      localStorage.removeItem('st_state_' + sessionId);
    }
  } catch {}
}

/* 恢复某会话的成品区 / 计划卡（切换与启动共用） */
function restoreState(id){
  lastDone = null; lastExecPlan = null; lastMath = null; lastVerify = null;
  pipeline.resetPipeline();
  shots.hideAnalysis();
  let st = null;
  try { st = JSON.parse(localStorage.getItem('st_state_' + id) || 'null'); } catch {}
  result.resetResult();             // 先全清，避免残留上一个会话的报告/胶片条
  if (st && st.ev){
    lastDone = st.ev;
    result.showResult(st.ev, { activate: false });   // 不抢当前标签页
  }
  if (st && st.plan){
    lastExecPlan = st.plan;
    pipeline.renderPlan(st.plan, { editable: true });
    $('#replanBtn').disabled = busy;
  } else {
    pipeline.renderPlan(null);
    $('#replanBtn').disabled = true;
  }
  if (st && st.math){
    lastMath = st.math;
    pipeline.renderMath(st.math);
    if (lastExecPlan) result.feedTimeline(lastExecPlan, lastMath);
  }
  if (st && st.verify) renderVerify(st.verify);
}

/* 切回历史会话：重放对话 + 恢复成品。运行中禁止切（事件流不能写到别的会话）。 */
function switchSession(id){
  if (busy || id === sessionId){
    sess.renderSelect($('#sessSel'), sessionId, switchSession);
    return;
  }
  sessionId = id;
  localStorage.setItem('st_session', id);
  turnCount = parseInt(localStorage.getItem('st_turns_' + id) || '0', 10);
  refreshPlaceholder();
  let entries = [];
  try { entries = JSON.parse(localStorage.getItem('st_chat_' + id) || '[]'); } catch {}
  chat.setTranscript(entries);      // 触发 onChange → 归档到最前 + 刷新下拉
  restoreState(id);
}

/* ---------- busy / 计时 / 取消 ---------- */
let busy = false, t0 = 0, timer = null;

function setBusy(v){
  busy = v;
  $('#send').disabled = v || materials.busyMaterials() === false;
  $('#replanBtn').disabled = v || !lastExecPlan;
  $('#exportFinalBtn').disabled = v;
  $('#cancelBtn').style.display = v ? '' : 'none';
  $('#newTaskBtn').disabled = v;
}
function startTimer(){
  t0 = performance.now();
  timer = setInterval(() => {
    $('#elapsed').textContent = ((performance.now() - t0) / 1000).toFixed(1) + ' s';
  }, 100);
}
function stopTimer(){
  clearInterval(timer);
  $('#elapsed').textContent = ((performance.now() - t0) / 1000).toFixed(1) + ' s';
}

materials.setMaterialsCallback(has => {
  $('#send').disabled = busy || has === false;
  $('#send').title = has === false ? '请先上传素材' : '';
});

/* ---------- 等待指示（后端 heartbeat 驱动：LLM/渲染静默期可见） ---------- */
let waitTicker = null, waitBase = { idle: 0, at: 0 };
let llmStreaming = false;   // 后端 llm_first_token 事件置位：首 chunk 已到，是慢生成不是挂死

function markWaiting(idle){
  if (!(idle >= 5)) return;                       // 短暂间隙不闪条
  waitBase = { idle, at: Date.now() };
  $('#waitBar').hidden = false;
  if (!waitTicker){ paintWaiting(); waitTicker = setInterval(paintWaiting, 1000); }
}
function paintWaiting(){
  const s = waitBase.idle + (Date.now() - waitBase.at) / 1000;
  $('#waitBar').classList.toggle('slow', s >= 60);
  $('#waitText').textContent = s >= 60
    ? `后端响应偏慢 · 已等待 ${Math.floor(s)}s（请求超时会自动重试，也可点「取消」）`
    : llmStreaming
      ? `模型生成中 · 已等待 ${Math.floor(s)}s（token 持续到达）`
      : `模型响应中 · 已等待 ${Math.floor(s)}s`;
}
function clearWaiting(){
  llmStreaming = false;
  $('#waitBar').hidden = true;
  if (waitTicker){ clearInterval(waitTicker); waitTicker = null; }
}

/* ---------- 事件分发 ---------- */
function renderVerify(v){
  const box = $('#verifyBox'); box.replaceChildren();
  const span = document.createElement('span');
  span.className = 'pill ' + (v.ok ? 'ok' : 'bad');
  span.textContent = v.duration != null
    ? `回验 ${v.ok ? '通过' : '不一致'} · 实际 ${v.duration}s / 预期 ${v.expected}s`
    : '回验：' + (v.note || v.error || '无结果');
  box.append(span);
}

function handleEvent(ev){
  if (ev.type === 'heartbeat'){ markWaiting(ev.idle); return; }
  clearWaiting();                                  // 任何真实事件都算有进展
  switch (ev.type){
    case 'attempt':
      if (ev.max > 1) chat.addSys(`第 ${ev.n}/${ev.max} 轮：让 agent 出编辑计划…`, true);
      else chat.addSys('正在分析素材并生成编辑计划…', true);
      break;
    case 'materials':
      /* 编号用服务端下发的（numbers），与素材库徽标/agent 感知同一来源 */
      chat.addSys('本次可用素材：' + ev.names
        .map(n => ev.numbers ? `#${ev.numbers[n]} ${n}` : n)
        .join('、'), false);
      break;
    case 'llm_first_token':                          // L2/L3：该请求首 chunk 已到（chunk 持续流动 = 慢生成，非挂死）
      llmStreaming = true; markWaiting(5); break;    // 重挂等待条，文案切「生成中」
    case 'llm':      chat.addAssistant(ev.text, ev.tool_calls); break;
    case 'plan':     pipeline.renderPlan(ev.plan, { editable: false }); break;
    case 'exec_plan':
      lastExecPlan = ev.plan;
      pipeline.renderPlan(ev.plan, { editable: true });
      $('#replanBtn').disabled = busy;
      saveState();
      break;
    case 'math':
      lastMath = ev.math;
      pipeline.renderMath(ev.math);
      if (lastExecPlan) result.feedTimeline(lastExecPlan, ev.math);
      saveState();
      break;
    case 'commands': pipeline.renderCommands(ev.commands); break;
    case 'status':   chat.addSys(ev.text, true); break;
    case 'exec':     pipeline.appendLog(ev.ok, ev.description, ev.ok ? null : (ev.error || ev.stderr_tail)); break;
    case 'verify':{
      const v = ev.verify || null;
      if (v){ lastVerify = v; renderVerify(v); saveState(); }
      break;
    }
    case 'done':
      turnCount += 1;
      localStorage.setItem('st_turns_' + sessionId, String(turnCount));
      refreshPlaceholder();
      lastDone = ev;
      result.showResult(ev);
      saveState();
      pipeline.loadOutputs();
      materials.loadInputs();          // 侧栏索引状态可能已更新
      break;
    case 'compile_error':
      chat.addSys('计划未通过编译器校验：', false);
      chat.addError(ev.errors.map(e => '· ' + e).join('\n'));
      break;
    case 'error':     chat.addError(ev.message); break;
    case 'traceback': chat.addSys('内部堆栈已折叠', false); break;
    case 'question':  chat.addQuestion(ev.question, ev.options, chat.insertIntoComposer); break;
    case 'hallucination': chat.addHallucination(ev); break;
    case 'analysis':  shots.renderAnalysis(ev); break;
    case 'plan_diff': pipeline.renderDiff(ev); break;               // V9.0 修订差异卡
    case 'status_answer': chat.addAssistant(ev.text, null); break;  // V9.0 状态直答（无工具调用）
    case 'cancelled': chat.addSys(ev.message || '任务已取消。', false); break;
  }
}

/* ---------- 运行（/api/run）与回改重渲（/api/replan） ---------- */
async function streamRun(url, payload, { silentUser = null } = {}){
  setBusy(true);
  pipeline.resetPipeline();
  shots.hideAnalysis();
  if (silentUser) chat.addUser(silentUser);
  startTimer();
  try {
    await ndjsonStream(url, payload, handleEvent);
    chat.addSys('本轮结束。', false);
  } catch (e){
    chat.addError('请求失败：' + e.message);
  } finally {
    clearWaiting();
    stopTimer();
    setBusy(false);
  }
}

function run(task){
  return streamRun('/api/run', {
    task, session_id: sessionId,
    preview: $('#previewToggle').checked,   // 快速预览档：低分辨率快编，导出再全量
  }, { silentUser: task });
}

function replan(preview, silentUser){
  const plan = pipeline.getWorkPlan();
  if (!plan || busy) return;
  if (typeof preview !== 'boolean') preview = $('#previewToggle').checked;
  return streamRun('/api/replan', { plan, session_id: sessionId, preview },
                   { silentUser: silentUser || '（按参数卡的修改重新渲染）' });
}

async function cancelRun(){
  if (!busy) return;
  const { ok, status, json } = await postJSON('/api/cancel', {});
  if (!ok) chat.addSys('取消失败：' + (json.error || ('HTTP ' + status)), false);
  else chat.addSys('已请求取消，正在停止…', true);
}

/* ---------- 标签页 ---------- */
function initTabs(){
  const tabs = document.querySelectorAll('.tab');
  const panes = document.querySelectorAll('.tabpane');
  tabs.forEach(t => t.onclick = () => {
    tabs.forEach(x => x.classList.toggle('on', x === t));
    panes.forEach(p => p.classList.toggle('on', p.id === t.dataset.pane));
  });
}

/* ---------- 启动新任务 ---------- */
async function newTask(){
  if (busy) return;
  materials.clearSelected();       // 先停预览拉流：Windows 上被打开的文件 reset 删不掉
  const { ok, status, json } = await postJSON('/api/session/reset', { session_id: sessionId });
  if (!ok){
    chat.addError('启动新任务失败：' + (json.error || ('HTTP ' + status)) + '，当前会话保持不变。');
    return;
  }
  if (json.errors && json.errors.length){
    chat.addError('部分素材没清掉（多半被播放器占用，关掉后可手动 × 移除）：\n'
      + json.errors.join('\n'));
  } else if (json.cleared && json.cleared.length){
    chat.addSys(`已清掉上一次的 ${json.cleared.length} 个素材。`, false);
  }
  /* 旧会话就地归档（对话与成品留在历史列表），后端会话上下文也保留，可随时切回 */
  sessionId = _newSessionId();
  localStorage.setItem('st_session', sessionId);
  turnCount = 0;
  localStorage.setItem('st_turns_' + sessionId, '0');

  /* 成品区全清：播放器 / 审阅报告 / 胶片条 / 导出链接 / 验证徽标不再残留上一次的成品 */
  lastDone = null; lastExecPlan = null; lastMath = null; lastVerify = null;
  saveState();
  result.resetResult();
  pipeline.resetPipeline();
  pipeline.renderPlan(null);
  $('#replanBtn').disabled = true;
  shots.hideAnalysis();
  materials.loadInputs();          // reset 已清空素材区，重拉列表

  chat.setTranscript([{ k: 'sys', t: WELCOME }]);   // 触发 onChange → 空会话不落盘
  sess.pruneEmpty();                // 顺手清掉没有内容的历史会话
  refreshPlaceholder();
  paintSessionSelect();
  const ta = $('#task');
  ta.value = '';
  ta.dispatchEvent(new Event('input'));
  ta.focus();
}

/* ---------- 引导 ---------- */
async function boot(){
  initTabs();
  initTemplates();
  initCull();
  materials.initUploads();
  materials.loadInputs();
  pipeline.resetPipeline();

  chat.setOnChange(saveChat);
  sess.pruneEmpty();                // 治历史遗留的「未命名任务」堆积
  restoreState(sessionId);          // 刷新页面恢复上次成品与计划卡（先于对话恢复，快照参与判空）
  let entries = [];
  try { entries = JSON.parse(localStorage.getItem('st_chat_' + sessionId) || '[]'); } catch {}
  if (entries.length) chat.setTranscript(entries);   // 刷新页面恢复对话
  else chat.setTranscript([{ k: 'sys', t: WELCOME }]);
  pipeline.loadOutputs();
  paintSessionSelect();
  refreshPlaceholder();

  $('#send').onclick = () => {
    const v = $('#task').value.trim();
    if (!v || busy) return;
    $('#task').value = '';
    $('#task').style.height = 'auto';
    run(v);
  };
  $('#replanBtn').onclick = () => replan();
  $('#exportFinalBtn').onclick = () =>
    replan(false, '（导出成品：按原始分辨率完整编码）');
  $('#cancelBtn').onclick = cancelRun;
  $('#newTaskBtn').onclick = newTask;
  $('#task').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); $('#send').click(); }
  });
  $('#task').addEventListener('input', e => {
    e.target.style.height = 'auto';
    e.target.style.height = Math.min(150, e.target.scrollHeight) + 'px';
  });

  try {
    const h = await getJSON('/api/health');
    const ff = $('#ffPill');
    if (h.ffmpeg && h.ffprobe){
      ff.className = 'pill ok';
      ff.textContent = 'ffmpeg 就绪' + (h.ffmpeg_version
        ? ' · ' + h.ffmpeg_version.replace('ffmpeg version ', '').split(' Copyright')[0] : '');
      ff.title = h.ffmpeg + '\n' + h.ffprobe;
    } else {
      ff.className = 'pill bad';
      ff.textContent = 'ffmpeg 未安装，渲染会失败';
    }
    $('#modelPill').textContent = '模型 · ' + h.model;
    if (h.upload && h.upload.exts){
      $('#fileInput').accept = h.upload.exts.join(',');
      $('#pickBtn').title =
        `支持 ${h.upload.exts.length} 种扩展名，单个上限 ${h.upload.max_mb}MB\n`
        + h.upload.exts.join('  ');
    }
  } catch {
    $('#ffPill').className = 'pill bad';
    $('#ffPill').textContent = '后端未响应';
  }
}

boot();
