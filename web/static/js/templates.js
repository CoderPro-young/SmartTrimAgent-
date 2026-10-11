/* 任务模板（T19）：新手不知道能说什么——点一下把需求骨架填进输入框 */
import { $, el } from './util.js';
import { insertIntoComposer } from './chat.js';

/* 双模式（V8）：粗剪模式 = 减法漏斗；智能创作 = agent 逐步执行创作 skill
   （V8.1 注册表：general/travel/food，旁白制字幕 + 自选 BGM + 可选节拍卡点）。
   模式只影响模板集/占位文案（任务文本自然携带意图，服务端无感知）。 */
const TEMPLATES_ROUGH = [
  { label: '🗂 先粗剪一轮', text: '帮我对这些素材做一轮粗剪：每条素材剪掉静音和黑场废段，跳过画质差的镜头，把可用内容按顺序拼成一条粗版成片，输出 720p（我之后要拿去精剪，请保留可导入剪辑软件的时间线）' },
  { label: '✂ 剪掉口播静音', text: '把 INPUT/ 里这段口播的静音和长停顿都剪掉，保留讲话内容，直接出片' },
  { label: '🎬 筛镜头出集锦', text: '从这些素材里筛出「和朋友聚会」的镜头，剪成 30 秒左右的集锦' },
  { label: '🧹 去掉黑屏废段', text: '把素材开头结尾的黑屏和镜头盖误录的片段去掉，保留有效内容' },
  { label: '🎞 拼接 + 转场', text: '把这几个素材按顺序拼起来，片段之间加 0.5 秒 fade 转场' },
  { label: '🙈 人脸打码', text: '把视频里出现的人脸全部打码（马赛克），其他保持不变' },
  { label: '💧 去水印', text: '把视频右上角的水印区域糊掉' },
  { label: '🎵 加背景音乐', text: '给这段视频配上我上传的音乐作 BGM，音量 0.3，有人声时自动压低' },
  { label: '📱 转竖屏 720p', text: '输出竖屏 720×1280、720p，画面居中加黑边' },
];

const TEMPLATES_SMART = [
  { label: '✨ 一键成片·带旁白', text: '帮我把这些素材做成一条带旁白字幕的成片：先看素材内容，按叙事分组挑镜头，每组写一段第一人称的旁白，配一首合适的曲库音乐，fade 转场，25 秒左右，输出 1080p' },
  { label: '📝 旅行 Vlog 成片', text: '用我的素材做一条旅行 vlog：按行程的起承转合挑镜头分组，旁白写具体的地方和感受（不要套路词），节奏轻快，配轻快的曲库音乐，30 秒左右' },
  { label: '🍽 美食成片', text: '把素材做成一条美食成片：围绕做菜的过程组织镜头（食材、烹饪动作、成品），第一句旁白配最有食欲的画面，动词和感官词优先，25 秒左右' },
  { label: '🎵 节奏卡点成片', text: '把这些素材剪成一条音乐卡点的快节奏成片：选短促有力的镜头，配节奏感强的曲库音乐，剪辑点尽量卡在音乐节拍上，20 秒左右' },
  { label: '🎬 纪实感短片', text: '把素材剪成有纪实感的短片：选最有故事感的镜头，旁白用克制的短句，fade 转场稍慢，20 秒左右，输出 1080p' },
];

const PLACEHOLDER = {
  rough: '先用一句话描述要剪成什么样，例如：把这两段拼起来，中间加 0.5 秒 fade 转场，输出 720p',
  smart: '描述想要的成片感觉，例如：做成一条 25 秒的旅行 vlog，旁白像本人随手记录，配轻快的音乐',
};

export let mode = localStorage.getItem('st_mode') || 'rough';

function renderTemplates(){
  const box = $('#tpls');
  box.replaceChildren();
  const list = mode === 'smart' ? TEMPLATES_SMART : TEMPLATES_ROUGH;
  for (const t of list){
    const c = el('span', 'tpl', t.label);
    c.title = '点击填入输入框，可再修改：' + t.text;
    c.onclick = () => {
      const ta = $('#task');
      ta.value = t.text;
      ta.dispatchEvent(new Event('input'));
      ta.focus();
      ta.setSelectionRange(ta.value.length, ta.value.length);
    };
    box.append(c);
  }
  const ta = $('#task');
  if (!ta.value.trim()) ta.placeholder = PLACEHOLDER[mode];
}

export function initTemplates(){
  for (const m of document.querySelectorAll('#modes .mode')){
    m.classList.toggle('on', m.dataset.mode === mode);
    m.onclick = () => {
      mode = m.dataset.mode;
      localStorage.setItem('st_mode', mode);
      document.querySelectorAll('#modes .mode')
        .forEach(x => x.classList.toggle('on', x === m));
      renderTemplates();
    };
  }
  renderTemplates();
}
