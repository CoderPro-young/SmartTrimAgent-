"""一键成片 workflow 宏（V7）：WORKFLOW SKILL 的「编译器化」实现。

设计参考 FireRed-OpenStoryline 的 WORKFLOW SKILL 概念（Apache-2.0，
https://github.com/FireRedTeam/FireRed-OpenStoryline —— 其 default_editing_workflow
定义了「加载→切镜头→理解→筛选→分组→时间线→渲染」的主流程，由 LLM 逐步
调用节点工具执行）。本模块把同一类「主流程」改造成**计划级宏**：LLM 只挑
工作流 + 填少量参数，粗筛/选材/编排全部由本模块确定性展开成普通计划，
再走既有的编译→dry-run→执行→回验链路——延续「LLM 出意图、编译器保正确」。

与 skills.py 同构的注册表模式：一处定义 → 校验/展开/提示词三处派生。

可用 workflow：
- one_click_reel  自动集锦：粗筛丢废料 → 画质选材 → 静音比过滤 →
                  预算轮转装填 → 默认转场 → 出片（+EDL/CSV，V6 交付）
- speech_clean    口播净化：有声音的素材逐条剪静音，按序拼接出片
- smart_create    智能创作：确定性选材 → 把选中镜头回喂 LLM 写文案 →
                  编译器逐片段绑定烧录（V7.2：文案跟画面走，不再悬空）

V7.2 变更（实测教训）：① 候选最短 1.5s（闪帧/频闪碎片不再入片）；
② 装填改跨素材轮转（单素材长镜头不再占满预算挤出其他来源）；
③ captions 变为可选——缺省时由编译器在选材**之后**调 LLM 现写
（FireRed 流水线里 generate_script 同样排在 filter/group 之后）。

V7.3 变更：smart_create 新增 subtitle_style 参数——"credits" 把全部文案
合并成一整块逐行居中的文本，跨全片时长从画面底匀速滚到顶（电影片尾
致谢体，白字描边）。排版策略收在 CAPTION_STYLES 预设表里，渲染层只有
sequential / scroll 两个原语；新样式 = 加预设 + 补 usage 文案，不动渲染。

V7.4 变更：文案改为可插拔提供器链（CAPTION_PROVIDERS：plan → llm →
labels，计划自带文案时生成模块完全不参与）；credits + 计划自带文案 +
未显式给 budget_seconds 时，成片总时长由文案驱动（行数 ×
seconds_per_line），选材软预算装填后由 _fit_duration_to_captions 精确
收齐——「文案定长度，画面来填满」。

V7.5 变更：自动配乐（参考 FireRed select_bgm 的「召回 → LLM 终选」，
Apache-2.0）。one_click_reel / smart_create 展开时把纯音频素材当 BGM
候选（编译器 _gather_ctx 探好时长），候选多个时回喂 LLM 按主题关键词
终选一首，注入顶层 audio 块（低音量 + fade + loop 铺满 + 有人声时
ducking）。优先级：计划自带 audio > workflow.bgm 指定曲目 > 自动选曲；
LLM 不可用/选飞 → 回退第一候选，配乐是锦上添花，不卡出片。

V7.5.1 变更：内置曲库 MUSIC/（Mixkit 免费授权 16 首，溯源见
MUSIC/README.md + meta.json；原始音频禁止再分发故不入 git）——
候选来源 = MUSIC/ 曲库优先 + INPUT/ 纯音频；workflow.bgm / 计划
audio.source 的合法前缀放宽为 INPUT/ 与 MUSIC/（AUDIO_PREFIXES）。

V7.5.2 变更：workflow.bgm 支持氛围描述自由文本（如「轻松欢快」
「旅游」）——无路径前缀的字符串按氛围交给 LLM 选曲，与镜头筛选的
keyword 解耦（氛围偏好拿去过滤画面会把镜头全筛没）；曲库候选附带
meta.json 的 title/genre 作选曲依据。

V7.7 变更（音频控制）：① 顶层 audio.original——**缺省 mute**（加 BGM
即删素材原声，模型手写 audio 块同样生效；skills.original_mode 单一判定，
ducking 隐含 keep），显式 "keep" 才与原声混音；自动配乐注入
original=mute + volume=1.0；② clip 级
audio 块逐段控声：{"mute": true} 静音本段原声 / {"source": ...} 换掉
本段声音（循环铺满或裁到片段时长，volume/fade 可调），在归一化阶段
落地，下游 concat/xfade/BGM 链路零特判；与 cut_silence/cut_black
互斥（检测依据被覆盖）。

V7.5.3 变更：曲库打标（scripts/label_music.py）——meta.json 增加
Mixkit 人工标签 tags（MUSIC_META_FIELDS 白名单透传进选曲 prompt）；
omni 听感打标通道预留（DASHSCOPE_API_KEY + --provider omni）。本地
DSP 推 mood 的方案实测不可行（RMS/响度密度/起始率均无感知区分度），
已放弃——感知语义要么用人工标注，要么用音频大模型。

V7.11 变更（旁白制，对比 FireRed generate_script/plan_timeline 的结论）：
「一镜一句」升级为「按叙事组写整段旁白 → 标点断句 → 字符加权铺轴」——
分组/断句/铺轴全是纯函数（_group_picked/_split_narration_units/
plan_compiler._apply_narration），LLM 一次调用写全片旁白 + 标题（组字数
预算 = 时长 × 3~5 字/秒）。一个镜头可挂多句、一句可跨剪辑点；失败静默
回退一镜一句链；自带文案（plan）与 credits 滚动体不走旁白。

V7.6 变更（文案质量）：打标加 desc 一句话画面描述（content_analysis，
镜头卡的输入从标签词升级为具体画面）；_write_captions 重写 prompt
（few-shot + 禁用词 + 首句钩子/末句收束），解析失败或句数不齐带反馈
重试一次而非直接作废；labels 兜底在前端摘要显式标注来源。

V8.0 变更（创作流 agent 化，见 docs/v8.0-agent-workflow.md）：
① **取消创作流强制粗筛**——_usable_cards 不再用 culling.judge_material
判废拦截（判断权交给模型：agent 路径经 list_shots 看到全量镜头池，
宏路径由镜头级 quality/静音过滤兜底）；素材库 UI 的手动粗筛报告
（/api/cull/report|apply）不受影响。② 本模块的宏整体**降位为兜底**：
创作类任务的默认路径是 agent 逐步执行 WORKFLOW SKILL（video_agent 的
提示词 + list_shots/list_music 工具），手写完整计划后仍走同一编译链。
"""

from __future__ import annotations

import json
import os

from dataclasses import dataclass

import culling

# 计划顶层 workflow 块的合法字段（plan_schema 白名单与此同源）
WORKFLOW_KEYS = {"name", "budget_seconds", "transition", "keyword", "order",
                 "captions", "subtitle_style", "seconds_per_line", "bgm"}

DEFAULT_BUDGET = 30.0        # 一键集锦默认成片时长
DEFAULT_TRANSITION = 0.3     # 默认转场时长（fade）
MIN_SHOT_DURATION = 1.5      # 候选最短时长：更短的碎片（闪帧/频闪）不进片

# credits 致谢体的文案驱动时长（V7.4）：成片总时长 = 文案行数 × 每行阅读秒数
DEFAULT_SECONDS_PER_LINE = 1.2
SECONDS_PER_LINE_MIN = 0.5
SECONDS_PER_LINE_MAX = 5.0

# 字幕排版预设（V7.3）：smart_create.subtitle_style 的合法值与排版策略。
# 渲染层只实现两个原语（sequential 逐条 enable / scroll 整块滚动 y 表达式），
# 新样式 = 加一行预设 + usage 文案一句话，不动渲染代码。
CAPTION_STYLES: dict[str, dict] = {
    # bottom_margin_ratio：底部字幕的贴底留白占画布高度的比例——固定像素边距
    # 在高分辨率下只占高度几个百分点，会压到相机自带水印（影石/DJI 常驻底部）
    "bottom":  {"mode": "sequential", "position": "bottom", "font_size": 44,
                "bottom_margin_ratio": 0.12},
    "credits": {"mode": "scroll", "position": "center", "font_size": 40,
                "line_spacing": 14, "stroke": 2},
}
DEFAULT_CAPTION_STYLE = "bottom"

# credits 致谢体行数上限（普通逐句字幕一条文案对一个镜头，12 条足够；
# 致谢名单是分类头 + 名单行，行数天然多）
CAPTION_LINES_LIMIT = 12
CAPTION_LINES_LIMIT_CREDITS = 60

# 自动配乐（V7.5→V7.7）：注入顶层 audio 块的缺省参数。
# V7.7 起自动配乐是「纯 BGM」：original=mute 去掉素材原声（成片里杂乱的
# 环境音/碎语被 BGM 替代），音乐即全部声音，音量给 1.0（不再有原声参照，
# 0.3 会太小）。想保留口播/原声的场景由模型显式写 audio 块：
# original="keep" + ducking=true（有人声自动压低音乐）。
BGM_SOLO_VOLUME = 1.0
BGM_FADE_IN = 0.5
BGM_FADE_OUT = 1.0

# 内置曲库目录（V7.5.1）：Mixkit 免费授权曲目，溯源见 MUSIC/README.md +
# meta.json。原始音频文件授权禁止再分发，故 MUSIC/ 不入 git（见 .gitignore）。
# BGM 候选来源 = 本曲库 + INPUT/ 里的纯音频，曲库优先。
MUSIC_DIR = "MUSIC"
# 音频源的合法前缀（BGM/曲库文件）；skills.validate_audio 与 plan_schema 同源
AUDIO_PREFIXES = ("INPUT/", "MUSIC/")


def caption_style_menu() -> str:
    """合法 subtitle_style 清单（进校验错误信息）。"""
    return "/".join(sorted(CAPTION_STYLES))


def _round2(x: float) -> float:
    return round(float(x), 2)


# ------------------------------------------------- 文案提供器（V7.4） ---- #
# 可插拔链：先命中先赢。plan = 计划/用户自带（生成模块完全不参与）；
# llm = 把选中镜头序列回喂模型现写；labels = 无 LLM 时的标签兜底。
# 新来源 = 加一个 (name, fn) + usage 文案一句话；fn(wf, picked, ctx)
# -> (captions | None, source | None)，返回 None 让位下一个提供器。

def _captions_from_plan(wf: dict, picked: list[dict], ctx: dict):
    caps = [c for c in (wf.get("captions") or []) if str(c).strip()]
    return (caps, "plan") if caps else (None, None)


def _captions_from_llm(wf: dict, picked: list[dict], ctx: dict):
    return _write_captions(picked, (wf.get("keyword") or "").strip())


def _captions_from_labels(wf: dict, picked: list[dict], ctx: dict):
    return _label_captions(picked), "labels"


CAPTION_PROVIDERS: dict[str, object] = {
    "plan": _captions_from_plan,
    "llm": _captions_from_llm,
    "labels": _captions_from_labels,
}


# ------------------------------------------------- 时长对齐（V7.4） ---- #

def _fit_duration_to_captions(res: dict, target: float, trans) -> dict:
    """把选中片段的投影总时长（含转场重叠）精确修到 target。

    文案驱动时长的收尾：选材阶段用「软预算 = target + 最长候选」装填
    （允许略超 target），这里收短或丢弃末段把总时长精确压回 target——
    末段 trim 是线性减法（转场重叠数不变），D 随 trim 量 1:1 减少。
    末段收短不低于 MIN_SHOT_DURATION；违反时在「收到下限」与「整段
    丢弃」之间取投影时长更贴近 target 者。素材不足以达到 target
    （镜头太长装不满）时诚实接受欠额，由 report 记录实际值。
    """
    clips = [dict(c) for c in res["clips"]]
    timeline = [dict(t) for t in res["timeline"]]
    picked = list(res["picked"])
    ov = max(0.0, float(trans))

    def d_of(c: dict) -> float:
        return (c.get("trim_end") or 0) - (c.get("trim_start") or 0)

    def proj(cs: list[dict]) -> float:
        return sum(d_of(c) for c in cs) - ov * max(0, len(cs) - 1)

    d_now = proj(clips)
    if d_now > target + 1e-6 and clips:
        excess = _round2(d_now - target)
        last = clips[-1]
        d_last = d_of(last)
        new_d = _round2(d_last - excess)
        if new_d >= MIN_SHOT_DURATION - 1e-6:
            last["trim_end"] = _round2((last.get("trim_start") or 0) + new_d)
        elif len(clips) > 1:
            d_trim = d_now - (d_last - MIN_SHOT_DURATION)
            d_drop = d_now - d_last + ov
            if abs(d_drop - target) < abs(d_trim - target):
                clips.pop()
                timeline.pop()
                picked.pop()
            else:
                last["trim_end"] = _round2(
                    (last.get("trim_start") or 0) + MIN_SHOT_DURATION)
    return {**res, "clips": clips, "timeline": timeline, "picked": picked}


@dataclass(frozen=True)
class Workflow:
    """一个一键成片工作流。

    name        —— 计划里 workflow.name 的合法取值
    summary     —— 一句话说明（进提示词）
    expand      —— (wf, ctx) -> (plan dict, report dict)；ctx 由编译器注入
                   （素材卡片/探针/project_root），异常抛 CompileError 语义
                   的 ValueError（中文，可回传 LLM）
    prompt_doc  —— 多行用法说明（进提示词）
    """
    name: str
    summary: str
    expand: object            # Callable[[dict, dict], tuple[dict, dict]]
    prompt_doc: str = ""


# --------------------------------------------------------------- 选材 ----- #

def _usable_cards(ctx: dict, sources: list[str] | None):
    """取素材卡片（V8.0 起不做粗筛拦截：所有有卡素材进选材池）。

    ctx —— {cards: {rel: card}, probes: {rel: probe}, project_root}
    缺卡的素材返回 (cards, missing) 由调用方决定报错。
    历史行为（V6–V7.11）：culling.judge_material 判废的素材不进池——V8.0
    创作流 agent 化后判断权交给模型（list_shots 全量可见 + 镜头级 quality/
    静音信号作参考），素材库 UI 的手动粗筛报告（/api/cull/report）不受影响。
    """
    cards, missing, culled = {}, [], []
    for rel, card in (ctx.get("cards") or {}).items():
        if sources and rel not in sources:
            continue
        if not card or card.get("error") or not card.get("shots"):
            missing.append(rel)
            continue
        cards[rel] = card
    return cards, missing, culled


# ------------------------------------------------- 自动配乐（V7.5） ---- #

# 选曲元数据白名单：meta.json 里哪些字段进候选/提示词
MUSIC_META_FIELDS = ("title", "genre", "tags",
                     "mood", "scene", "energy", "description")


def _music_candidates(ctx: dict) -> list[dict]:
    """BGM 候选：内置曲库 MUSIC/ 优先，其后 INPUT/ 里的纯音频素材。

    ctx.music 由编译器 _gather_ctx 探好时长；排序决定「回退第一候选」
    和「唯一候选」落在谁身上——曲库是精选曲目，排在用户随手上传之前。
    曲库条目附带 meta.json 元数据（曲名/曲风/人工标签/omni 听感标签，
    见 scripts/label_music.py）作选曲语义信号；INPUT/ 素材只有文件名。
    """
    music = ctx.get("music") or {}
    rels = sorted(music, key=lambda r: (0 if r.startswith(MUSIC_DIR + "/")
                                        else 1, r))
    lib = _music_meta(ctx)
    cands = []
    for rel in rels:
        c = {"source": rel, "duration": (music[rel] or {}).get("duration")}
        meta_c = lib.get(rel) or {}
        c.update({k: meta_c[k] for k in MUSIC_META_FIELDS if k in meta_c})
        cands.append(c)
    return cands


def _music_meta(ctx: dict) -> dict:
    """读内置曲库的 meta.json（MUSIC/ 下的溯源+标签文件）→ {rel: 字段集}。

    缺文件/字段不全时按能读到的算——元数据是锦上添花，不影响可用性。
    """
    root = ctx.get("project_root") or "."
    path = os.path.join(root, MUSIC_DIR, "meta.json")
    try:
        with open(path, encoding="utf-8") as f:
            entries = json.load(f)
        return {e["file"]: {k: e[k] for k in MUSIC_META_FIELDS
                            if e.get(k) not in (None, "", [])}
                for e in entries if e.get("file")}
    except (OSError, ValueError, TypeError):
        return {}


def _pick_bgm_llm(cands: list[dict], keyword: str,
                  style: str = "") -> tuple[str | None, str]:
    """候选曲库回喂 LLM 终选一首（FireRed select_bgm 的轻量版）。

    判断依据 = 文件名 + 曲库元数据（曲名/曲风/人工标签/听感标签）+
    主题关键词 + 氛围描述（workflow.bgm 的自由文本，如「轻松欢快」
    「旅游」）。模型未配置 / 回复解析不出 / 选了候选之外的曲目 →
    返回 (None, 原因)，调用方回退确定性选择——配乐是锦上添花，
    不能卡住出片。
    """
    lines = []
    for i, c in enumerate(cands, 1):
        dur = c["duration"] if c["duration"] is not None else "?"
        info = []
        if c.get("title") and c["title"] not in c["source"]:
            info.append(f"曲名 {c['title']}")
        if c.get("genre"):
            info.append(f"曲风 {c['genre']}")
        if c.get("tags"):
            info.append("标签 " + "/".join(str(t) for t in c["tags"][:5]))
        if c.get("mood"):
            info.append("氛围 " + "/".join(str(t) for t in c["mood"][:3]))
        if c.get("energy"):
            info.append(f"能量 {c['energy']}")
        extra = "，" + "，".join(info) if info else ""
        lines.append(f"{i}. {c['source']}（时长 {dur}s{extra}）")
    prompt = (
        "你在为一条短视频挑背景音乐（BGM）。候选曲目（曲名暗示情绪/场景；"
        "标签为曲库的人工标注与听感标注，供氛围匹配）：\n"
        + "\n".join(lines)
        + (f"\n视频主题关键词：{keyword}。" if keyword else "")
        + (f"\n用户想要的配乐氛围/场景：{style}。" if style else "")
        + "\n选一首最贴合视频氛围的。只输出 JSON 对象："
          '{"source": "<候选原文路径>", "reason": "一句话理由"}，不要任何解释。\n')
    try:
        from model import get_model
        msg = get_model().invoke(prompt)
        content = msg.content if isinstance(msg.content, str) else "".join(
            str(b.get("text") or "") for b in (msg.content or [])
            if isinstance(b, dict))
        s, e = content.find("{"), content.rfind("}")
        if s < 0 or e <= s:
            return None, "llm 回复不含 JSON"
        picked = json.loads(content[s:e + 1])
        src = str(picked.get("source") or "").strip()
        if src not in {c["source"] for c in cands}:
            return None, f"llm 选了候选之外的曲目 {src!r}"
        return src, str(picked.get("reason") or "").strip()
    except Exception as exc:                      # noqa: BLE001 —— 一律降级
        return None, f"llm 不可用：{type(exc).__name__}"


def _attach_bgm(plan: dict, wf: dict, ctx: dict, report: dict) -> None:
    """自动配乐：给一键成片挂顶层 audio 块（V7.5→V7.7）。

    优先级：计划自带 audio（用户显式写了完整参数）> workflow.bgm 指定
    曲目 > 自动选曲。workflow.bgm 的取值：
    - false 关闭；true / 缺省 = 自动；
    - "MUSIC/x.mp3" / "INPUT/x.mp3"（带路径前缀）= 指定曲目，不存在则
      抛 ValueError 回传 LLM（宁可报错也不静默换曲）；
    - 其他非空字符串 = 氛围/场景描述（如「轻松欢快」「旅游」），交给
      LLM 按描述选曲——与镜头筛选的 keyword 解耦，氛围偏好不该拿去
      过滤画面。
    自动选曲任何失败都降级不阻塞出片。决定记录在 report.bgm。
    """
    if ctx.get("plan_audio"):
        report["bgm"] = {"selection": "plan"}
        return
    mode = wf.get("bgm")
    if mode is False:
        report["bgm"] = {"selection": "disabled"}
        return
    cands = _music_candidates(ctx)
    style, src, reason, how = "", None, None, None
    if isinstance(mode, str):
        if mode.startswith(AUDIO_PREFIXES):
            if mode not in {c["source"] for c in cands}:
                raise ValueError(
                    f"workflow.bgm 指定的曲目不可用：{mode}。"
                    "必须是 INPUT/ 或 MUSIC/ 下真实存在的音频文件。")
            src, reason, how = mode, "workflow.bgm 指定曲目", "explicit"
        else:
            style = mode.strip()          # 氛围描述 → 走下面的自动选曲
    if src is None:                               # 自动选曲路径
        if not cands:
            report["bgm"] = {"selection": "none",
                             "reason": "没有可用的配乐候选（曲库缺失且 "
                                       "INPUT/ 里没有纯音频素材）"}
            return
        if len(cands) > 1:
            src, reason = _pick_bgm_llm(
                cands, (wf.get("keyword") or "").strip(), style)
            if src:
                how = "llm"
            else:
                src = cands[0]["source"]
                reason = f"{reason}；回退第一候选 {src}"
                how = "auto"
        else:
            src, reason, how = cands[0]["source"], "唯一候选", "auto"

    # V7.7：自动配乐 = 纯 BGM。素材原声（多为杂乱环境音/碎语）被音乐
    # 替代；要保留口播的场景由模型显式写 original="keep"+ducking。
    plan["audio"] = {"source": src, "volume": BGM_SOLO_VOLUME,
                     "fade_in": BGM_FADE_IN, "fade_out": BGM_FADE_OUT,
                     "loop": True, "original": "mute"}
    report["bgm"] = {"source": src, "selection": how, "original": "mute",
                     "reason": reason or None, **({"style": style} if style else {})}


# -------------------------------------------------- ① one_click_reel ----- #

def _expand_one_click_reel(wf: dict, ctx: dict) -> tuple[dict, dict]:
    """自动集锦：确定性「粗筛 → 选材 → 编排」。

    用户一句话「帮我剪个视频」时的默认工作流：不需要用户给筛选条件，
    以画质可用性为底线 + 可选关键词，预算内贪心装填，默认 fade 转场。
    """
    import shot_select

    budget = float(wf.get("budget_seconds") or DEFAULT_BUDGET)
    keyword = (wf.get("keyword") or "").strip()
    cards, missing, culled = _usable_cards(ctx, None)
    if missing:
        raise ValueError(
            "以下素材还没有内容索引：" + "、".join(missing) +
            "。请先对这些素材调用 analyze_media（通常上传后已自动建好）。")
    if not cards:
        raise ValueError("INPUT/ 里没有任何已建卡的视频素材，没有可用画面。")

    where: dict = {"quality_in": ["good", "ok"], "max_silence_ratio": 0.8,
                   "min_duration": MIN_SHOT_DURATION}
    if keyword:
        # text_any = 跨字段 OR（场景/活动/氛围/标签任一命中）——
        # 关键词语义不该要求「同时」匹配多个字段
        where["text_any"] = [keyword]
    sel_block = {
        "sources": sorted(cards),
        "where": where,
        "budget_seconds": budget,
        "order": wf.get("order") or "best_first",
    }
    res = shot_select.select_shots(cards, sel_block)
    if not res["clips"]:
        raise ValueError(
            f"一键集锦没有挑到可用镜头（共 {res['matched']} 个候选，全部被条件/"
            f"预算拒掉）。请调大 budget_seconds 或去掉 keyword 限制。")

    trans = wf.get("transition", DEFAULT_TRANSITION)
    timeline = []
    for i in range(len(res["timeline"])):
        item = dict(res["timeline"][i])
        if i > 0 and trans:
            item["transition"] = {"type": "fade", "duration": float(trans)}
        timeline.append(item)

    plan = {
        "clips": res["clips"],
        "timeline": timeline,
    }
    report = {
        "workflow": "one_click_reel",
        "culled": culled,                       # 粗筛丢弃的素材（带原因）
        "picked": len(res["picked"]),
        "rejected_total": len(res.get("rejected") or []),
        "budget_seconds": budget,
        "keyword": keyword or None,
    }
    _attach_bgm(plan, wf, ctx, report)
    return plan, report


# ---------------------------------------------------- ② speech_clean ----- #

def _expand_speech_clean(wf: dict, ctx: dict) -> tuple[dict, dict]:
    """口播净化：有音轨且非全静音的素材逐条剪静音，按序硬切拼接。

    全静音/无音轨的素材自动跳过（不报错——粗剪语义里它们本来就没得剪）。
    """
    probes = ctx.get("probes") or {}
    cards = ctx.get("cards") or {}
    clips, timeline, skipped = [], [], []
    cut_params = {"noise_db": -35, "min_silence": 0.4,
                  "keep_padding": 0.15, "min_keep": 0.3}
    for rel in sorted(cards):
        probe = probes.get(rel) or {}
        if probe.get("has_audio") is False:
            skipped.append({"source": rel, "reason": "无音轨，无从剪静音"})
            continue
        verdict = culling.judge_material(rel, cards[rel], probe)
        if any("无声" in r for r in verdict.reasons):
            skipped.append({"source": rel, "reason": "音轨整体无声"})
            continue
        cid = f"c{len(clips) + 1}"
        clips.append({
            "id": cid, "source": rel, "kind": "video",
            "cut_silence": dict(cut_params),
        })
        timeline.append({"clip": cid})
    if not clips:
        raise ValueError(
            "没有任何素材带有效声音（无音轨或整条静音），口播净化无事可做。"
            "请换有说话内容的素材。")
    plan = {"clips": clips, "timeline": timeline}
    report = {"workflow": "speech_clean", "clips": len(clips),
              "skipped": skipped}
    return plan, report


# ---------------------------------------------------- ③ smart_create ---- #

_CAPTION_PROMPT = (
    "你是短视频口播字幕写手。成片按顺序选用了 {n} 个镜头，清单如下（真实画面内容）。\n"
    "【任务】为每个镜头写一句字幕，只输出一个 JSON 字符串数组（正好 {n} 项，"
    "第 i 项对应第 i 个镜头）。\n"
    "【要求】\n"
    "1. 第 i 句以第 i 个镜头的画面为主体，前后句要自然衔接；\n"
    "2. 每句不超过 14 个字；用具体的名词和动作，口语化，像博主本人在说话；\n"
    "3. 第 1 句是钩子（提问/断言/悬念，让人想看下去），最后 1 句收束（感受或呼应主题）；\n"
    "4. 禁用这些词：治愈、宁静、岁月静好、定格、美好时光、难忘、风景如画、"
    "心旷神怡、流连忘返、逃离城市；\n"
    "5. 不要复述画面清单里的标签词（如「空镜」「无」），写画面里具体的人和物。\n"
    "{kw}"
    "【示例】3 个镜头的输入 → 输出：\n"
    "输入：\n"
    "1. [3.2s] 穿红裙的女孩背对镜头走向金色草地深处（场景=户外草地）\n"
    "2. [4.0s] 一群羊从山坡涌下来，扬起薄尘（场景=草原）\n"
    "3. [2.8s] 女孩回头笑，夕阳照亮侧脸（场景=日落）\n"
    '输出：["把车开到了没有信号的地方", "羊群接管了整片山坡", "她说下次还来"]\n\n'
    "现在开始。镜头清单：\n{listing}"
)

_RETRY_NOTE = (
    "\n\n注意：你上一次的输出{why}，但必须正好 {n} 句——第 i 句对应第 i 个镜头。"
    "请重新只输出 JSON 字符串数组，不要任何解释。"
)


def _write_captions(picked: list[dict], keyword: str = "") -> tuple[list[str] | None, str]:
    """选材之后写文案：把选中的镜头序列回喂 LLM，一句一镜（V7.6）。

    输入升级（V7.6）：镜头卡带 desc（VLM 一句话画面描述）时用 desc——文案
    模型的输入从「几个标签词」变成「一句具体画面」；旧卡无 desc 时退回标签
    清单。prompt 加 few-shot、禁用词与叙事结构（首句钩子/末句收束）。
    解析失败或句数不齐时带错误反馈重试一次，仍不齐才作废（此前是直接作废）。
    模型未配置 key / 两次都不行 → 返回 (None, "failed")，调用方退标签拼接
    ——文案是锦上添花，不能卡住出片。
    """
    lines = []
    for i, c in enumerate(picked, 1):
        lab = c.get("label") or {}
        dur = c.get("duration", 0)
        scene = str(lab.get("scene") or "").strip() or "未知"
        desc = str(lab.get("desc") or "").strip()
        if desc:
            lines.append(f"{i}. [{dur:.1f}s] {desc}（场景={scene}）")
        else:
            tags = "、".join(str(t) for t in (lab.get("tags") or [])[:4])
            lines.append(
                f"{i}. [{dur:.1f}s] 场景={scene}"
                f" 活动={lab.get('activity') or '未知'} 氛围={lab.get('mood') or '未知'}"
                f" 人数={lab.get('person_count', 0)} 标签={tags or '无'}")
    prompt = _CAPTION_PROMPT.format(
        n=len(picked),
        kw=(f"主题关键词：{keyword}（尽量在首句或末句体现）。\n" if keyword else ""),
        listing="\n".join(lines))

    def _invoke(text: str) -> list[str] | None:
        from model import get_model
        msg = get_model().invoke(text)
        content = msg.content if isinstance(msg.content, str) else "".join(
            str(b.get("text") or "") for b in (msg.content or [])
            if isinstance(b, dict))
        s, e = content.find("["), content.rfind("]")
        if s < 0 or e <= s:
            return None
        caps = json.loads(content[s:e + 1])
        if not isinstance(caps, list):
            return None
        caps = [str(x).strip() for x in caps if str(x).strip()]
        return caps or None

    try:
        caps = _invoke(prompt)
        if caps is None or len(caps) != len(picked):
            why = "解析失败" if caps is None else f"只有 {len(caps)} 句"
            caps = _invoke(prompt + _RETRY_NOTE.format(why=why, n=len(picked)))
        if caps is None or len(caps) != len(picked):
            return None, "failed"
        return caps, "llm"
    except Exception:
        return None, "failed"


def _label_captions(picked: list[dict]) -> list[str]:
    """LLM 不可用时的兜底文案：场景·活动，仍是一句一镜。"""
    caps = []
    for c in picked:
        lab = c.get("label") or {}
        scene = str(lab.get("scene") or "").strip() or "精彩瞬间"
        act = str(lab.get("activity") or "").strip()
        caps.append(f"{scene}·{act}" if act and act != scene else scene)
    return caps


# ------------------------------------------------- 旁白制（V7.11） ------- #
# 对比 FireRed-OpenStoryline generate_script.py / plan_timeline.py 的结论：
# 「一镜一句」是文案质感的瓶颈——他们按叙事组写整段旁白（字数预算 =
# 组时长 × 3~5 字/秒），按标点确定性断句，再按字数加权铺进组的绝对
# 时间窗（一个镜头可挂多句、一句可跨剪辑点）。本节把同一套结构搬进来：
# 分组/断句/铺轴都是纯函数（编译器哲学），LLM 只负责写旁白本身。

MAX_GROUP_SECONDS = 25.0     # 单组时长上限：旁白的最小叙事单元
NARRATION_SPLIT_PUNCT = "。！？；，,.！？；"   # 断句标点（含中英）
UNIT_MIN_CHARS = 5           # 短于此的断句碎片并入相邻单元
UNIT_MAX_CHARS = 18          # 单条字幕上限，超出硬换行
CAPTION_BANNED_WORDS = (
    "治愈、宁静、岁月静好、定格、美好时光、难忘、风景如画、心旷神怡、"
    "流连忘返、逃离城市、家人们谁懂啊、原来快乐如此简单")


def _narration_budget(seconds: float) -> tuple[int, int]:
    """组时长 → 旁白字数预算（FireRed _estimate_script_budget 同款公式）。"""
    seconds = max(0.0, float(seconds or 0))
    lo = max(round(seconds * 3), 8)
    hi = max(round(seconds * 5), lo + 6)
    return lo, hi


def _group_picked(clips: list[dict], picked: list[dict]) -> list[dict]:
    """选中镜头 → 叙事组（纯规则）：场景标签连续 + 组时长 ≤ 上限。

    clips[i]["id"] 与 picked[i] 一一对应（select_shots 按同一次序构建）。
    场景变化或超上限即开新组；每组至少一个镜头。
    """
    groups: list[dict] = []
    for c, p in zip(clips, picked):
        cid = c["id"]
        d = round((c.get("trim_end") or 0) - (c.get("trim_start") or 0), 3)
        scene = str((p.get("label") or {}).get("scene") or "").strip()
        if groups and scene and groups[-1]["scene"] == scene \
                and groups[-1]["seconds"] + d <= MAX_GROUP_SECONDS:
            groups[-1]["clip_ids"].append(cid)
            groups[-1]["seconds"] = round(groups[-1]["seconds"] + d, 3)
        else:
            groups.append({"group_id": f"g{len(groups) + 1}", "scene": scene,
                           "clip_ids": [cid], "seconds": d})
    for g in groups:
        g.pop("scene")               # 内部字段，不进暂存键
    return groups


def _split_narration_units(text: str) -> list[str]:
    """旁白段 → 字幕单元（纯函数）：标点断句 + 碎片并入前条 + 超长硬换行。"""
    parts, buf = [], ""
    for ch in str(text or ""):
        buf += ch
        if ch in NARRATION_SPLIT_PUNCT:
            parts.append(buf)
            buf = ""
    if buf.strip():
        parts.append(buf)
    units = [p.strip("。，！？；,.!?；、 \n\t") for p in parts]
    units = [u for u in units if u]
    # 碎片并入前一条：自身太短、或前一条还太短时合并（合并后不超上限）
    merged: list[str] = []
    for u in units:
        if merged and (len(u) < UNIT_MIN_CHARS or len(merged[-1]) < UNIT_MIN_CHARS) \
                and len(merged[-1]) + len(u) <= UNIT_MAX_CHARS:
            merged[-1] += u
        else:
            merged.append(u)
    # 超长硬换行（中文无词边界，按字数切）
    final: list[str] = []
    for u in merged:
        while len(u) > UNIT_MAX_CHARS:
            final.append(u[:UNIT_MAX_CHARS])
            u = u[UNIT_MAX_CHARS:]
        if u:
            final.append(u)
    return final


def _write_narration(clips: list[dict], picked: list[dict],
                     keyword: str = "") -> tuple[list[dict] | None, str | None]:
    """整片一次旁白（V7.11，FireRed 同构）：按组写整段 → 断句成字幕单元。

    返回 (narration, title)；narration = [{group_id, clip_ids, units}]，
    时间窗与加权铺轴由编译器在时间轴推导后完成（_apply_captions）。
    解析失败/组缺失带反馈重试一次；两次不行返回 (None, None)，
    调用方回退 CAPTION_PROVIDERS 链——旁白是升级不是依赖。
    """
    groups = _group_picked(clips, picked)
    if not groups:
        return None, None
    # clips/picked 平行数组 → 组内镜头描述行
    idx = 0
    blocks = []
    for g in groups:
        lo, hi = _narration_budget(g["seconds"])
        rows = []
        for _ in g["clip_ids"]:
            p = picked[idx]
            lab = p.get("label") or {}
            desc = str(lab.get("desc") or "").strip()
            scene = str(lab.get("scene") or "").strip() or "未知"
            rows.append(f"  - [{p.get('duration', 0):.1f}s] "
                        + (desc if desc else f"场景={scene}"))
            idx += 1
        blocks.append(f"[{g['group_id']}] 时长 {g['seconds']:.1f}s "
                      f"字数预算 {lo}~{hi} 字\n" + "\n".join(rows))
    prompt = (
        "你是资深短视频/Vlog 文案策划大师。你将化身视频的主角（第一人称「我」），"
        "用轻叙事感的口语，把碎片素材串联成有温度、有逻辑的故事。\n"
        "【输入】按播放顺序给出镜头组（组间有时长与字数预算，预算是关键约束，"
        "按 3~5 字/秒 与时长匹配），组内每行列出一个镜头的真实画面。\n"
        "【任务】为每组写一段旁白 raw_text：字数严格落在该组预算内；全片连贯——"
        "第一组开头是钩子（提问/断言/悬念），最后一组收束（感受或呼应）；"
        "同时为全片起一个标题。\n"
        "【写法】第一人称「我」视角，口语化，像博主本人说话；用具体的名词和动作；"
        "每组最多 1 个 emoji；不用括号和省略号。\n"
        f"【禁用词】{CAPTION_BANNED_WORDS}。\n"
        "【信息保真】只写画面里有的人/事/物，禁止编造画面没有的内容；专有名词保留。\n"
        + (f"【主题关键词】{keyword}（在钩子或收束里体现）。\n" if keyword else "")
        + "【输出】只输出 JSON：{\"title\": \"8~15字标题\", \"groups\": "
        "[{\"group_id\": \"...\", \"raw_text\": \"...\"}]}，"
        "groups 必须覆盖输入的每一个 group_id。\n\n镜头组：\n" + "\n\n".join(blocks))

    def _invoke(text: str):
        from model import get_model
        msg = get_model().invoke(text)
        content = msg.content if isinstance(msg.content, str) else "".join(
            str(b.get("text") or "") for b in (msg.content or [])
            if isinstance(b, dict))
        s, e = content.find("{"), content.rfind("}")
        if s < 0 or e <= s:
            return None
        try:
            obj = json.loads(content[s:e + 1])
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None
        return obj

    try:
        obj = _invoke(prompt)
        gmap = {str(x.get("group_id")): str(x.get("raw_text") or "").strip()
                for x in (obj or {}).get("groups", []) if isinstance(x, dict)} \
            if isinstance(obj, dict) else {}
        if obj is None or not gmap or any(not gmap.get(g["group_id"]) for g in groups):
            missing = [g["group_id"] for g in groups if not gmap.get(g["group_id"])]
            why = "解析失败" if obj is None else f"缺少 {','.join(missing or ['全部'])} 组"
            obj = _invoke(prompt + f"\n\n注意：你上一次的输出{why}。"
                                    "请重新只输出符合要求的 JSON。")
            gmap = {str(x.get("group_id")): str(x.get("raw_text") or "").strip()
                    for x in (obj or {}).get("groups", []) if isinstance(x, dict)} \
                if isinstance(obj, dict) else {}
        if not gmap or any(not gmap.get(g["group_id"]) for g in groups):
            return None, None
        narration = []
        for g in groups:
            raw = gmap[g["group_id"]]
            units = _split_narration_units(raw)
            if not units:
                return None, None
            narration.append({"group_id": g["group_id"],
                              "clip_ids": g["clip_ids"], "units": units})
        title = str((obj or {}).get("title") or "").strip()[:30] or None
        return narration, title
    except Exception:
        return None, None


def _expand_smart_create(wf: dict, ctx: dict) -> tuple[dict, dict]:
    """智能创作（FireRed 一键成片的无配音版）：自动画面编排 + 文案烧录。

    确定性编排：粗筛 → 画质选材（可选 keyword，最短 1.5s、跨素材轮转
    装填）→ fade 转场。文案走 CAPTION_PROVIDERS 可插拔链（V7.4）：
    plan（计划自带，**生成模块完全不参与**）→ llm（选中镜头回喂现写）
    → labels（兜底），先命中先赢，来源记录在 report.captions_source。
    credits 模式 + 计划自带文案 + 未显式给 budget_seconds 时，成片总
    时长由文案驱动（V7.4：行数 × seconds_per_line）——选材用软预算
    允许略超，再由 _fit_duration_to_captions 精确对齐；显式给了
    budget_seconds 则预算优先（模型应在二者间替用户做明确选择）。
    captions 的烧录由编译器在时间轴推导之后完成（_apply_captions），
    经 _captions/_caption_style 暂存键传递。
    """
    import shot_select

    plan_captions = [c for c in (wf.get("captions") or []) if str(c).strip()]
    keyword = (wf.get("keyword") or "").strip()
    style = str(wf.get("subtitle_style") or DEFAULT_CAPTION_STYLE).strip()

    cards, missing, culled = _usable_cards(ctx, None)
    if missing:
        raise ValueError(
            "以下素材还没有内容索引：" + "、".join(missing) +
            "。请先对这些素材调用 analyze_media（通常上传后已自动建好）。")
    if not cards:
        raise ValueError("INPUT/ 里没有任何已建卡的视频素材，没有可用画面。")

    where: dict = {"quality_in": ["good", "ok"], "max_silence_ratio": 0.8,
                   "min_duration": MIN_SHOT_DURATION}
    if keyword:
        where["text_any"] = [keyword]

    budget = wf.get("budget_seconds")
    duration_source = "budget" if budget is not None else "default"
    target = None
    if style == "credits" and plan_captions and budget is None:
        spl = float(wf.get("seconds_per_line") or DEFAULT_SECONDS_PER_LINE)
        target = _round2(len(plan_captions) * spl)
        cands, _rej = shot_select.collect_candidates(cards, where)
        # 软预算：允许装填略超 target（至多多一个最长候选），事后精确收齐
        soft = _round2(target + max((c["duration"] for c in cands), default=0.0))
        budget = soft
        duration_source = "captions"
    else:
        budget = float(budget or DEFAULT_BUDGET)

    res = shot_select.select_shots(cards, {
        "sources": sorted(cards), "where": where,
        "budget_seconds": budget, "order": "best_first"})
    if not res["clips"]:
        raise ValueError(
            f"smart_create 没有挑到可用镜头（{res['matched']} 个候选全部被拒）。"
            f"请调大 budget_seconds 或放宽条件。")

    if target is not None:
        res = _fit_duration_to_captions(
            res, target, float(wf.get("transition", DEFAULT_TRANSITION)))

    narration, title = None, None
    if not plan_captions and style != "credits":
        # V7.11 旁白制优先：整片一次按组写旁白（多句/镜、句可跨剪辑点）；
        # 失败静默回退下方的一镜一句链——旁白是升级不是依赖
        narration, title = _write_narration(res["clips"], res["picked"], keyword)

    if narration:
        captions, cap_src = None, "narration"
    else:
        captions, cap_src = None, None
        for provide in CAPTION_PROVIDERS.values():
            captions, cap_src = provide(wf, res["picked"], ctx)
            if captions:
                break
        if not captions:
            raise ValueError("所有文案提供器都没有产出文案（plan/llm/labels 全部落空）。")

    trans = wf.get("transition", DEFAULT_TRANSITION)
    timeline = []
    for i in range(len(res["timeline"])):
        item = dict(res["timeline"][i])
        if i > 0 and trans:
            item["transition"] = {"type": "fade", "duration": float(trans)}
        timeline.append(item)

    plan = {
        "clips": res["clips"],
        "timeline": timeline,
    }
    if narration:
        plan["_narration"] = narration       # 编译器推导时间轴后消费（字符加权铺轴）
    elif captions:
        plan["_captions"] = captions         # 一镜一句回退路径（见 _apply_captions）
    plan["_caption_style"] = style
    report = {
        "workflow": "smart_create",
        "captions": (sum(len(g["units"]) for g in narration) if narration
                     else len(captions or [])),
        "captions_source": cap_src,
        "title": title,                      # V7.11 旁白制附带的全片标题
        "subtitle_style": style,
        "duration_source": duration_source,
        "target_seconds": target,
        "culled": culled,
        "picked": len(res["picked"]),
        "rejected_total": len(res.get("rejected") or []),
        "budget_seconds": budget,
        "keyword": keyword or None,
    }
    _attach_bgm(plan, wf, ctx, report)
    return plan, report


WORKFLOWS: dict[str, Workflow] = {
    w.name: w for w in [
        Workflow(
            name="one_click_reel",
            summary="one-click highlight reel: cull junk, pick good shots, "
                    "default fade transitions, within a budget.",
            expand=_expand_one_click_reel,
            prompt_doc=(
                "  `one_click_reel` — the default when the user just says\n"
                "  \"help me edit this\" with no specifics. Deterministically:\n"
                "  cull junk materials (all-silent/all-black/poor/too-short),\n"
                "  pick good-quality shots (optional `keyword` filter), greedy\n"
                "  fill within `budget_seconds` (default 30), fade transitions\n"
                "  (`transition` seconds, 0 = hard cuts). No hand-written trims.\n"
                "  Auto-BGM: a built-in music library (MUSIC/) is used as\n"
                "  the soundtrack — the clips' original audio is muted and\n"
                "  the music loops to fill. To keep speech under music,\n"
                "  write an explicit `audio` block (original: \"keep\" +\n"
                "  ducking: true). `bgm: false` disables; `bgm` also takes\n"
                "  a track path or a vibe like \"轻松欢快\" / \"旅游\"; an\n"
                "  explicit top-level `audio` block wins over all of it.\n"
                "  Mood wishes go in `bgm`, never `keyword`."
            ),
        ),
        Workflow(
            name="speech_clean",
            summary="cut silence/pauses from talking materials and join them.",
            expand=_expand_speech_clean,
            prompt_doc=(
                "  `speech_clean` — for \"clean up my talking footage\": every\n"
                "  material with real audio gets cut_silence; silent/no-audio\n"
                "  materials are skipped automatically (reported, not failed)."
            ),
        ),
        Workflow(
            name="smart_create",
            summary="smart one-click video; captions written after shot "
                    "selection and bound per clip.",
            expand=_expand_smart_create,
            prompt_doc=(
                "  `smart_create` — creative one-click video with captions\n"
                "  burned in. `captions` is OPTIONAL now: omit it and the\n"
                "  compiler picks shots first, then has the model write one\n"
                "  caption per picked shot (aligned to the actual footage).\n"
                "  Supply `captions` yourself only when the user gave exact\n"
                "  lines (count == clip count binds 1:1; otherwise spread\n"
                "  evenly). Submit {\"workflow\": {\"name\": \"smart_create\",\n"
                "  \"budget_seconds\": 25, \"keyword\": \"...\"}} — read the\n"
                "  cards only to choose keyword/budget, no need to pre-write\n"
                "  copy. Shots shorter than 1.5s never get picked; sources\n"
                "  take turns so every material contributes. Honors `audio`:\n"
                "  a BGM from the built-in MUSIC/ library is auto-picked and\n"
                "  the clips' original audio muted (explicit `audio` block\n"
                "  with original: \"keep\" keeps speech). `bgm` takes false,\n"
                "  a track path, or a vibe like \"轻松欢快\" / \"旅游\" —\n"
                "  never put mood wishes in `keyword` (that filters shots).\n"
                "  `subtitle_style` — ONLY \"credits\" (anything else is\n"
                "  rejected): all captions render as ONE centered text block\n"
                "  scrolling bottom→top across the whole video (movie\n"
                "  end-credits style). Use it when the user asks for 滚动\n"
                "  字幕/片尾致谢/鸣谢名单 style, and supply `captions`\n"
                "  yourself in that format (【分类头】 header + name lines,\n"
                "  up to 60 short lines); omitting captions falls back to\n"
                "  per-shot lines scrolling. Default (omit) = per-shot\n"
                "  captions near the bottom.\n"
                "  Captions are a PLUGGABLE chain: plan-supplied captions\n"
                "  are used verbatim and the caption-writing model is never\n"
                "  invoked; omitted captions are written per-shot by the\n"
                "  model; label captions are the last-resort fallback.\n"
                "  credits + plan captions + NO budget_seconds: the video\n"
                "  length is DERIVED from the copy (lines × seconds_per_line,\n"
                "  default 1.2s/line, range 0.5–5.0) so the scroll pace fits\n"
                "  the text — omit budget_seconds to let the captions set\n"
                "  the duration, or pass budget_seconds to override."
            ),
        ),
    ]
}


def workflow_menu() -> str:
    """合法 workflow 名清单（进校验错误信息）。"""
    return "/".join(sorted(WORKFLOWS))


def render_workflow_doc() -> str:
    """派生提示词里的 workflow 用法文档（与 skills.render_prompt_doc 同思想）。"""
    lines = ["## Workflows (V7 one-click macros — DETERMINISTIC FALLBACK)",
             "V8.0: creative/one-click tasks should use the Creative workflow above",
             "(you drive: list_shots → filter → group → narration → music →",
             "hand-written plan). These macros remain for simple batch/parameter-",
             "only tasks, or as a fallback when the creative path is unnecessary.",
             "A macro replaces clips/timeline/overlays with one `workflow` block:"]
    for w in WORKFLOWS.values():
        lines.append(f"- `{w.name}`: {w.summary}")
        if w.prompt_doc:
            lines.append(w.prompt_doc)
    lines.append(
        "  Shape: {\"workflow\": {\"name\": \"one_click_reel\", \"budget_seconds\": 30,\n"
        "            \"transition\": 0.3, \"keyword\": \"sunset\"}}\n"
        "  `budget_seconds`/`transition`/`keyword`/`order` are all optional.\n"
        "  Every workflow still honors `output` and `audio` (BGM) blocks.\n"
        "  Auto-BGM (one_click_reel / smart_create): a music library ships\n"
        "  in MUSIC/ (mood spread: happy/chill/uplifting/ambient/corporate/\n"
        "  hip-hop/jazz/techno) and INPUT/ audio files also count. When the\n"
        "  plan has no `audio` block, one track is picked automatically and\n"
        "  the clips' ORIGINAL AUDIO IS MUTED — the music IS the soundtrack\n"
        "  (looped to fill, fade in/out). To keep speech under the music,\n"
        "  write an explicit `audio` block instead: `original: \"keep\"` +\n"
        "  `ducking: true`. Per-segment sound control: a clip may carry its\n"
        "  own `audio` block — `{\"mute\": true}` silences it, `{\"source\":\n"
        "  \"INPUT/x.mp3\"}` replaces its sound (see the audio doc).\n"
        "  `bgm` accepts: false = off; a track path (\"MUSIC/x.mp3\" or\n"
        "  \"INPUT/x.mp3\") = force that track; a free-text mood/scene\n"
        "  description (\"轻松欢快\", \"旅游 vlog\") = pick by that vibe.\n"
        "  Put MUSIC mood wishes in `bgm`, NEVER in `keyword` — `keyword`\n"
        "  filters the footage shots and will reject everything."
    )
    return "\n".join(lines)
