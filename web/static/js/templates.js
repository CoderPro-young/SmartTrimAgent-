/* 任务模板（T19）：新手不知道能说什么——点一下把需求骨架填进输入框 */
import { $, el } from './util.js';
import { insertIntoComposer } from './chat.js';

const TEMPLATES = [
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

export function initTemplates(){
  const box = $('#tpls');
  for (const t of TEMPLATES){
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
}
