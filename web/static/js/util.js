/* 通用 DOM / 格式化工具 */
export const $ = s => document.querySelector(s);

export function el(tag, cls, text){
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

export function esc(s){
  return String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
}

export function jsonBlock(obj){
  const pre = document.createElement('pre');
  pre.textContent = JSON.stringify(obj, null, 2);
  return pre;
}

export function fmtSize(n){
  if (n >= 1048576) return (n / 1048576).toFixed(1) + 'MB';
  if (n >= 1024) return (n / 1024).toFixed(0) + 'KB';
  return n + 'B';
}

/* 秒 → 12.3s / 1:05.3 两种展示 */
export function fmtT(v){
  return v == null ? '—' : Math.round(v * 10) / 10;
}
export function fmtClock(v){
  if (v == null) return '—';
  v = Math.round(v * 10) / 10;
  if (v < 60) return v + 's';
  const m = Math.floor(v / 60), s = Math.round(v % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}
