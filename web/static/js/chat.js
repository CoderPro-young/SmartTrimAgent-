/* 对话流渲染：用户 / 系统 / agent（含工具调用折叠）/ 反问 / 幻觉 / 错误。
   所有消息同步记入 transcript（app.js 按 session 存 localStorage），
   切换历史会话 / 刷新页面时用 setTranscript 重放，对话不再「新建任务就丢」。 */
import { $, el, jsonBlock } from './util.js';

const msgsEl = $('#msgs');
const chatScroll = $('#chatScroll');
const MAX_ENTRIES = 300;          // localStorage 容量护栏：只留最近 300 条

let transcript = [];
let onChange = () => {};          // app.js 挂进来做持久化

function scrollDown(){ chatScroll.scrollTop = chatScroll.scrollHeight; }

export function setOnChange(fn){ onChange = fn; }
export function getTranscript(){ return transcript; }

function record(entry){
  transcript.push(entry);
  if (transcript.length > MAX_ENTRIES)
    transcript.splice(0, transcript.length - MAX_ENTRIES);
  try { onChange(); } catch {}
}

/* ---- 底层绘制（不动 transcript；实时追加与重放共用） ---- */
function paintUser(text){
  const m = el('div', 'msg user');
  m.append(el('div', 'who', '你'));
  const b = el('div', 'bubble'); b.textContent = text;
  m.append(b);
  msgsEl.append(m);
}

function paintSys(text){
  const m = el('div', 'msg sys');
  const line = el('div', 'sys-line');
  line.append(el('span', 'dot'), el('span', null, text));
  m.append(line); msgsEl.append(m);
}

function paintAssistant(text, toolCalls){
  const m = el('div', 'msg');
  m.append(el('div', 'who', 'agent'));
  if (text){ const b = el('div', 'bubble'); b.textContent = text; m.append(b); }
  if (toolCalls && toolCalls.length){
    const wrap = el('div', 'tc');
    for (const tc of toolCalls){
      const d = el('details', 'tc-item');
      const s = el('summary');
      s.append(el('span', 'tc-name', tc.name || '?'));
      const n = JSON.stringify(tc.args ?? {});
      s.append(el('span', null, n.length > 68 ? n.slice(0, 68) + ' …' : n));
      d.append(s, jsonBlock(tc.args ?? {}));
      wrap.append(d);   // submit_plan 的完整计划在流水线标签展示，这里保持折叠
    }
    m.append(wrap);
  }
  msgsEl.append(m);
}

function paintError(text){
  const m = el('div', 'msg');
  m.append(el('div', 'who', '错误'));
  const d = el('div', 'err'); d.textContent = text;
  m.append(d); msgsEl.append(m);
}

/* agent 反问：气泡 + 可点选项（点击填进输入框，不自动提交） */
function paintQuestion(question, options){
  const m = el('div', 'msg');
  m.append(el('div', 'who', 'agent · 反问'));
  const b = el('div', 'bubble');
  b.append(el('span', 'qtag', '需要确认'));
  b.append(el('div', null, question || '需要确认一下你的要求。'));
  if (options && options.length){
    const w = el('div', 'opts');
    for (const o of options){
      const c = el('span', 'opt', o);
      c.title = '填进输入框，可补充后再运行';
      c.onclick = () => insertIntoComposer(o);
      w.append(c);
    }
    b.append(w);
  }
  m.append(b); msgsEl.append(m);
}

function paintHallucination(ev){
  const m = el('div', 'msg');
  m.append(el('div', 'who', '系统提示'));
  const d = el('div', 'hallu');
  d.textContent = (ev.level === 'error' ? '幻觉纠偏失败，请人工确认' : '疑似幻觉')
    + `（${ev.signal || '?'}）：${ev.evidence || ''}`;
  m.append(d); msgsEl.append(m);
}

/* ---- 公开接口：绘制 + 记录 ---- */

export function addUser(text){
  paintUser(text); scrollDown();
  record({ k: 'user', t: text });
}

/* running 只影响实时展示的呼吸点；持久化后一律是普通系统行 */
export function addSys(text, running){
  if (running){
    const m = el('div', 'msg sys');
    const line = el('div', 'sys-line run');
    line.append(el('span', 'dot'), el('span', null, text));
    m.append(line); msgsEl.append(m); scrollDown();
  } else {
    paintSys(text); scrollDown();
  }
  record({ k: 'sys', t: text });
}

export function addAssistant(text, toolCalls){
  paintAssistant(text, toolCalls); scrollDown();
  record({ k: 'assistant', t: text || '', tcs: toolCalls && toolCalls.length ? toolCalls : null });
}

export function addError(text){
  paintError(text); scrollDown();
  record({ k: 'error', t: text });
}

export function addQuestion(question, options, onPick){
  paintQuestion(question, options); scrollDown();
  void onPick;   // 重放时选项统一走 insertIntoComposer，原回调不入档
  record({ k: 'question', q: question, o: options || null });
}

export function addHallucination(ev){
  paintHallucination(ev); scrollDown();
  record({ k: 'hallu', ev });
}

/* ---- 重放：清空 DOM 按档内条目重建（切换会话 / 启动恢复用） ---- */
export function setTranscript(entries){
  transcript = (Array.isArray(entries) ? entries : [])
    .filter(e => e && e.k).slice(-MAX_ENTRIES);
  msgsEl.replaceChildren();
  for (const e of transcript){
    if (e.k === 'user') paintUser(e.t);
    else if (e.k === 'sys') paintSys(e.t);
    else if (e.k === 'assistant') paintAssistant(e.t, e.tcs);
    else if (e.k === 'error') paintError(e.t);
    else if (e.k === 'question') paintQuestion(e.q, e.o);
    else if (e.k === 'hallu') paintHallucination(e.ev || {});
  }
  scrollDown();
  try { onChange(); } catch {}      // 通知 app.js：会话归档（排序/标题）要跟着刷新
}

export function insertIntoComposer(s){
  const ta = $('#task');
  const pos = ta.selectionStart ?? ta.value.length;
  const pre = ta.value.slice(0, pos), post = ta.value.slice(pos);
  ta.value = pre + (pre && !/\s$/.test(pre) ? ' ' : '') + s + post;
  ta.focus();
  ta.dispatchEvent(new Event('input'));
}
