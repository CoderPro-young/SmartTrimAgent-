/* 对话流渲染：用户 / 系统 / agent（含工具调用折叠）/ 反问 / 幻觉 / 错误 */
import { $, el, jsonBlock } from './util.js';

const msgsEl = $('#msgs');
const chatScroll = $('#chatScroll');

function scrollDown(){ chatScroll.scrollTop = chatScroll.scrollHeight; }

export function addUser(text){
  const m = el('div', 'msg user');
  m.append(el('div', 'who', '你'));
  const b = el('div', 'bubble'); b.textContent = text;
  m.append(b);
  msgsEl.append(m); scrollDown();
}

export function addSys(text, running){
  const m = el('div', 'msg sys');
  const line = el('div', 'sys-line' + (running ? ' run' : ''));
  line.append(el('span', 'dot'), el('span', null, text));
  m.append(line); msgsEl.append(m); scrollDown();
  return line;
}

export function addAssistant(text, toolCalls){
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
  msgsEl.append(m); scrollDown();
}

export function addError(text){
  const m = el('div', 'msg');
  m.append(el('div', 'who', '错误'));
  const d = el('div', 'err'); d.textContent = text;
  m.append(d); msgsEl.append(m); scrollDown();
}

/* agent 反问：气泡 + 可点选项（点击填进输入框，不自动提交） */
export function addQuestion(question, options, onPick){
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
      c.onclick = () => (onPick || (() => {}))(o);
      w.append(c);
    }
    b.append(w);
  }
  m.append(b); msgsEl.append(m); scrollDown();
}

/* 幻觉标记：只提示不拦截 */
export function addHallucination(ev){
  const m = el('div', 'msg');
  m.append(el('div', 'who', '系统提示'));
  const d = el('div', 'hallu');
  d.textContent = (ev.level === 'error' ? '幻觉纠偏失败，请人工确认' : '疑似幻觉')
    + `（${ev.signal || '?'}）：${ev.evidence || ''}`;
  m.append(d); msgsEl.append(m); scrollDown();
}

export function clearChat(){
  msgsEl.replaceChildren();
}

export function insertIntoComposer(s){
  const ta = $('#task');
  const pos = ta.selectionStart ?? ta.value.length;
  const pre = ta.value.slice(0, pos), post = ta.value.slice(pos);
  ta.value = pre + (pre && !/\s$/.test(pre) ? ' ' : '') + s + post;
  ta.focus();
  ta.dispatchEvent(new Event('input'));
}
