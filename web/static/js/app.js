/* app.js —— 入口与胶水：会话 / 标签页 / 运行流 / 事件分发 / 引导 */
import { $ } from './util.js';
import { ndjsonStream, getJSON, postJSON } from './api.js';
import * as chat from './chat.js';
import * as materials from './materials.js';
import * as shots from './shots.js';
import * as pipeline from './pipeline.js';
import * as result from './result.js';
import { initTemplates } from './templates.js';

/* ---------- 会话（v3.1 语义保持不变） ---------- */
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

function refreshPlaceholder(){
  $('#task').placeholder = turnCount > 0 ? PH_MORE : PH_FIRST;
}

/* ---------- busy / 计时 / 取消 ---------- */
let busy = false, t0 = 0, timer = null;
let lastExecPlan = null;

function setBusy(v){
  busy = v;
  $('#send').disabled = v || materials.busyMaterials() === false;
  $('#replanBtn').disabled = v || !lastExecPlan;
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

/* ---------- 事件分发 ---------- */
function handleEvent(ev){
  switch (ev.type){
    case 'attempt':
      if (ev.max > 1) chat.addSys(`第 ${ev.n}/${ev.max} 轮：让 agent 出编辑计划…`, true);
      else chat.addSys('正在分析素材并生成编辑计划…', true);
      break;
    case 'materials': chat.addSys('本次可用素材：' + ev.names.join('、'), false); break;
    case 'llm':      chat.addAssistant(ev.text, ev.tool_calls); break;
    case 'plan':     pipeline.renderPlan(ev.plan, { editable: false }); break;
    case 'exec_plan':
      lastExecPlan = ev.plan;
      pipeline.renderPlan(ev.plan, { editable: true });
      $('#replanBtn').disabled = busy;
      break;
    case 'math':
      pipeline.renderMath(ev.math);
      if (lastExecPlan) result.feedTimeline(lastExecPlan, ev.math);
      break;
    case 'commands': pipeline.renderCommands(ev.commands); break;
    case 'status':   chat.addSys(ev.text, true); break;
    case 'exec':     pipeline.appendLog(ev.ok, ev.description, ev.ok ? null : (ev.error || ev.stderr_tail)); break;
    case 'verify':{
      const v = ev.verify || {};
      const box = $('#verifyBox'); box.replaceChildren();
      const span = document.createElement('span');
      span.className = 'pill ' + (v.ok ? 'ok' : 'bad');
      span.textContent = v.duration != null
        ? `回验 ${v.ok ? '通过' : '不一致'} · 实际 ${v.duration}s / 预期 ${v.expected}s`
        : '回验：' + (v.note || v.error || '无结果');
      box.append(span);
      break;
    }
    case 'done':
      turnCount += 1;
      localStorage.setItem('st_turns_' + sessionId, String(turnCount));
      refreshPlaceholder();
      result.showResult(ev);
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
    stopTimer();
    setBusy(false);
  }
}

function run(task){
  return streamRun('/api/run', { task, session_id: sessionId }, { silentUser: task });
}

function replan(){
  const plan = pipeline.getWorkPlan();
  if (!plan || busy) return;
  return streamRun('/api/replan', { plan, session_id: sessionId },
                   { silentUser: '（按参数卡的修改重新渲染）' });
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
  const { ok, status, json } = await postJSON('/api/session/reset', { session_id: sessionId });
  if (!ok){
    chat.addError('启动新任务失败：' + (json.error || ('HTTP ' + status)) + '，当前会话保持不变。');
    return;
  }
  sessionId = _newSessionId();
  localStorage.setItem('st_session', sessionId);
  turnCount = 0;
  localStorage.setItem('st_turns_' + sessionId, '0');
  chat.clearChat();
  chat.addSys(WELCOME, false);
  pipeline.resetPipeline();
  pipeline.renderPlan(null);
  lastExecPlan = null;
  $('#replanBtn').disabled = true;
  $('#cardResult').hidden = true;
  $('#player').removeAttribute('src');
  materials.clearSelected();
  materials.loadInputs();
  refreshPlaceholder();
  const ta = $('#task');
  ta.value = '';
  ta.dispatchEvent(new Event('input'));
  ta.focus();
}

/* ---------- 引导 ---------- */
async function boot(){
  initTabs();
  initTemplates();
  materials.initUploads();
  materials.loadInputs();
  pipeline.loadOutputs();
  pipeline.resetPipeline();
  refreshPlaceholder();
  chat.addSys(WELCOME, false);

  $('#send').onclick = () => {
    const v = $('#task').value.trim();
    if (!v || busy) return;
    $('#task').value = '';
    $('#task').style.height = 'auto';
    run(v);
  };
  $('#replanBtn').onclick = replan;
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
