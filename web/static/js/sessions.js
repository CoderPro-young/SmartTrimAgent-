/* 会话登记与切换 UI：「启动新任务」后旧会话仍在这里列出，可随时切回。
   登记表只存 {id,title,ts}（st_sessions）；对话与成品状态由 app.js 分存
   st_chat_<id> / st_state_<id>，登记表超限时连带清理。
   空会话（没发过消息也没出过片）不入册，启动时再统一清一次——
   否则连点几次「启动新任务」，下拉里就堆一列「未命名任务」。 */

const REG_KEY = 'st_sessions';
const MAX_SESSIONS = 20;      // 与后端 SESSIONS 内存上限对齐
const DEFAULT_TITLE = '未命名任务';

function loadReg(){
  try {
    const list = JSON.parse(localStorage.getItem(REG_KEY) || '[]');
    return Array.isArray(list) ? list.filter(x => x && x.id) : [];
  } catch { return []; }
}

function saveReg(list){
  try { localStorage.setItem(REG_KEY, JSON.stringify(list)); } catch {}
}

function dropStores(id){
  for (const k of ['st_chat_', 'st_state_', 'st_turns_']){
    try { localStorage.removeItem(k + id); } catch {}
  }
}

/* 会话是否有实际内容：对话出现过 user/assistant，或留有成品快照 */
export function hasContent(id){
  try {
    const chat = JSON.parse(localStorage.getItem('st_chat_' + id) || '[]');
    if (Array.isArray(chat) && chat.some(e => e && (e.k === 'user' || e.k === 'assistant')))
      return true;
  } catch {}
  try {
    const st = JSON.parse(localStorage.getItem('st_state_' + id) || 'null');
    if (st && (st.ev || st.plan || st.math || st.verify)) return true;
  } catch {}
  return false;
}

/* 清登记表：把没有内容的会话连同落盘数据一起丢掉（治「未命名任务」堆积） */
export function pruneEmpty(){
  const list = loadReg();
  const kept = list.filter(s => hasContent(s.id));
  if (kept.length === list.length) return;
  saveReg(kept);
  for (const gone of list){
    if (!kept.some(s => s.id === gone.id)) dropStores(gone.id);
  }
}

/* 登记会话并移到最前；title 只在会话还没有真名（首条任务描述）时补默认值 */
export function touch(id, title){
  const list = loadReg();
  const i = list.findIndex(s => s.id === id);
  const known = i >= 0 ? list[i] : null;
  const item = {
    id,
    title: title || (known && known.title) || DEFAULT_TITLE,
    ts: Date.now(),
  };
  if (i >= 0) list.splice(i, 1);
  list.unshift(item);
  while (list.length > MAX_SESSIONS){
    const gone = list.pop();
    if (gone && gone.id !== id) dropStores(gone.id);
  }
  saveReg(list);
  return item;
}

export function list(){ return loadReg(); }

/* 按最近使用排序填充下拉；选中其它会话时回调 onPick(id)。
   currentTitle：当前会话还没入册时（空会话）用它的文案补一个合成项，
   保住下拉「显示当前会话」的语义。 */
export function renderSelect(sel, currentId, onPick, currentTitle = null){
  if (!sel) return;
  sel.replaceChildren();
  sel.hidden = false;
  const pad = n => String(n).padStart(2, '0');
  const mk = (id, label) => {
    const o = document.createElement('option');
    o.value = id; o.textContent = label;
    return o;
  };
  const list = loadReg();
  if (currentTitle != null && !list.some(s => s.id === currentId))
    sel.append(mk(currentId, currentTitle));
  for (const s of list){
    const d = new Date(s.ts || Date.now());
    sel.append(mk(s.id,
      `${s.title || DEFAULT_TITLE} · ${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
      + ` ${pad(d.getHours())}:${pad(d.getMinutes())}`));
  }
  sel.value = currentId;
  sel.onchange = () => {
    if (sel.value && sel.value !== currentId) onPick(sel.value);
  };
}
