/* 任务模板（T19）：新手不知道能说什么——点一下把需求骨架填进输入框 */
import { $, el } from './util.js';
import { insertIntoComposer } from './chat.js';

/* 双模式（V8）：粗剪模式 = 减法漏斗；智能创作 = FireRed 式一键成片（文案+画面）。
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
  { label: '✨ 一键成片·带文案', text: '帮我把这些素材做成一条带文案的成片：先看素材内容，写 5 条左右贴合画面的中文短文案，自动选画面加 fade 转场，文案烧在画面下方，25 秒左右，输出 1080p' },
  { label: '📝 旅行 Vlog 成片', text: '用我的素材做一条旅行 vlog：按画面顺序挑镜头，写有画面感的短文案打在画面下方，节奏轻快，fade 转场，30 秒左右' },
  { label: '🎞 作品展示成片', text: '把这些素材做成展示成片：挑画质最好的镜头，第一句文案当标题，其余文案突出亮点，画面下方字幕，25 秒左右' },
  { label: '🎬 纪实感短片', text: '把素材剪成有纪实感的短片：选最有故事感的镜头，文案用克制的短句，fade 转场稍慢，20 秒左右，输出 1080p' },
];

const PLACEHOLDER = {
  rough: '先用一句话描述要剪成什么样，例如：把这两段拼起来，中间加 0.5 秒 fade 转场，输出 720p',
  smart: '描述想要的成片感觉，例如：做成一条 25 秒的旅行 vlog，文案走治愈风，配我上传的音乐',
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
