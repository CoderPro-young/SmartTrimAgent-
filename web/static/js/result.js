/* 成品标签：播放器 + 审阅报告（T18：这次实际做了什么） */
import { $, el, fmtClock } from './util.js';
import { setTimelineData } from './timeline.js';

export function showResult(ev){
  const card = $('#cardResult');
  card.hidden = false;
  const p = $('#player');
  p.src = ev.url;
  const a = $('#dl');
  a.href = ev.url;
  a.setAttribute('download', (ev.output || '').split('/').pop());
  a.textContent = ev.preview ? '下载预览视频' : '下载 / 打开原视频';
  /* 预览轮：亮出徽标 + 「导出成品」按钮，提醒还要做一次完整编码 */
  $('#previewBadge').hidden = !ev.preview;
  $('#exportFinalBtn').hidden = !ev.preview;
  $('#outMeta').textContent = ev.output;
  renderReport(ev.report, ev.preview);
  renderExports(ev.exports);
  $('#tabResult').click();
  card.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/* V6 交付：EDL/CSV 时间线导出链接（交给剪映/Premiere/Resolve 精剪） */
function renderExports(exports){
  const bar = $('#exportBar');
  if (!bar) return;
  bar.replaceChildren();
  if (!exports || !exports.length){
    bar.append(el('span', 'empty', '（本片无时间线导出）'));
    return;
  }
  for (const ex of exports){
    const a = el('a', null, (ex.file.split('/').pop()) + ' ↓');
    a.href = ex.url;
    a.setAttribute('download', ex.file.split('/').pop());
    const ext = ex.file.split('.').pop().toLowerCase();
    a.title = ext === 'edl'
      ? 'CMX3600 EDL：剪映专业版 / Premiere / Resolve / FCP 可导入继续精剪'
      : '剪辑表 CSV：源入出点 / 时间线位置，可用表格核对';
    bar.append(a);
  }
}

function rline(parts){
  const l = el('div', 'rline');
  for (const x of parts) l.append(x);
  return l;
}

export function renderReport(rep, preview = false){
  const box = $('#reportBody');
  box.replaceChildren();
  if (!rep){ box.append(el('span', 'empty', '—')); return; }
  const r = el('div', 'report');

  const b = el('b', null, `本片 ${rep.clips} 个片段 · 总时长 ${fmtClock(rep.duration)}`);
  r.append(rline([b]));

  /* V7.11 旁白制附带的全片标题（可复制去当发布文案） */
  if (rep.title){
    r.append(rline([el('span', null, `标题：${rep.title}`)]));
  }

  if (rep.cuts && rep.cuts.length){
    const total = rep.removed_total_seconds ?? 0;
    const segs = rep.cuts.reduce((n, c) => n + (c.kept_segments || 0), 0);
    r.append(rline([
      el('b', null, `已剪除静音/黑场 ${total}s`),
      el('span', null, `（${rep.cuts.length} 个素材展开为 ${segs} 个保留片段）`),
    ]));
  }
  if (rep.select){
    r.append(rline([
      el('b', null, `筛选命中 ${rep.selected_shots ?? (rep.select.picked || []).length} 个镜头`),
      el('span', 'sub', `（按条件自动挑选，总长 ${fmtClock(rep.select.total_seconds)}s）`),
    ]));
    /* V6 筛选报告：被拒镜头的原因分布（未进片的镜头去哪了） */
    const rejected = rep.select.rejected || [];
    if (rejected.length){
      const byReason = {};
      for (const x of rejected){
        const key = (x.reason || '未知').replace(/\d+(\.\d+)?%?/g, 'N');  // 数值归并同类
        byReason[key] = (byReason[key] || 0) + 1;
      }
      const top = Object.entries(byReason).sort((a, b) => b[1] - a[1]).slice(0, 4);
      r.append(rline([
        el('span', 'sub',
          `未入选 ${rep.select.rejected_total ?? rejected.length} 个镜头：`
          + top.map(([k, n]) => `${k}×${n}`).join('，')),
      ]));
    }
  }
  if ((rep.effects || []).length){
    r.append(rline([el('span', null, '效果：' + rep.effects.join('、'))]));
  }
  if (rep.transitions){
    r.append(rline([el('span', null, `转场 ${rep.transitions} 处`)]));
  }
  if (rep.bgm) r.append(rline([el('span', null, '含 BGM 配乐')]));
  if ((rep.overlays || 0) > 0) r.append(rline([el('span', null, `叠加 ${rep.overlays} 处`)]));
  /* V8.0 agent 手写文案：旁白制/一镜一句的来源与句数 */
  if (rep.captions && rep.captions.captions){
    const c = rep.captions;
    r.append(rline([el('span', 'sub',
      c.mode === 'narration'
        ? `旁白 ${c.captions} 句（agent 按叙事组撰写，编译器按字数加权铺轴）`
        : `字幕 ${c.captions} 句（agent 逐镜头撰写）`)]));
  }
  /* V7.5 文案来源透明化：labels 兜底不是创作，提醒用户别把它当文案水平 */
  const wf = rep.workflow || {};
  if (wf.captions_source === 'labels'){
    r.append(rline([el('span', 'sub',
      '⚠ 字幕为标签兜底（文案模型未产出）——在对话里描述想要的文案或直接给文案后重渲更好')]));
  }

  r.append(el('span', 'rtag', preview
    ? '⚡ 这是快速预览版（低分辨率）：确认构图与节奏后，点上方「导出成品（完整编码）」出正式成片；微调可直接继续对话'
    : '✓ 生成完毕，可直接下载发布；要微调请用左侧对话或流水线标签的参数卡'));
  box.append(r);
}

/* done 之后把时间线数据喂给胶片条（放在这里：result 与 timeline 同标签联动） */
export function feedTimeline(plan, math){
  setTimelineData(plan, math);
}
