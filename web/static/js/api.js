/* API 封装：普通 JSON 请求 + NDJSON 事件流读取 */

export async function getJSON(url){
  const r = await fetch(url);
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

export async function postJSON(url, payload){
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });
  let j = null;
  try { j = await r.json(); } catch {}
  return { ok: r.ok && (!j || j.ok === undefined || j.ok), status: r.status, json: j || {} };
}

/**
 * 读取 NDJSON 事件流（/api/run 与 /api/replan 共用）。
 * onEvent(ev) 逐条回调；流结束 resolve。
 */
export async function ndjsonStream(url, payload, onEvent){
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });
  if (!resp.ok || !resp.body) throw new Error('HTTP ' + resp.status);
  const reader = resp.body.getReader(), dec = new TextDecoder();
  let buf = '';
  for (;;){
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf('\n')) >= 0){
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 1);
      if (!line) continue;
      let ev;
      try { ev = JSON.parse(line); } catch { continue; }
      onEvent(ev);
    }
  }
}
