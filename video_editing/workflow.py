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
                  预算贪心 → 默认转场 → 出片（+EDL/CSV，V6 交付）
- speech_clean    口播净化：有声音的素材逐条剪静音，按序拼接出片
"""

from __future__ import annotations

from dataclasses import dataclass

import culling

# 计划顶层 workflow 块的合法字段（plan_schema 白名单与此同源）
WORKFLOW_KEYS = {"name", "budget_seconds", "transition", "keyword", "order",
                 "captions"}

DEFAULT_BUDGET = 30.0        # 一键集锦默认成片时长
DEFAULT_TRANSITION = 0.3     # 默认转场时长（fade）


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
    """取素材卡片并先做粗筛：废料（culling 规则）不进选材池。

    ctx —— {cards: {rel: card}, probes: {rel: probe}, project_root}
    缺卡的素材返回 (cards, missing) 由调用方决定报错。
    """
    cards, missing, culled = {}, [], []
    for rel, card in (ctx.get("cards") or {}).items():
        if sources and rel not in sources:
            continue
        if not card or card.get("error") or not card.get("shots"):
            missing.append(rel)
            continue
        verdict = culling.judge_material(rel, card, (ctx.get("probes") or {}).get(rel))
        if verdict.junk:
            culled.append({"source": rel, "reasons": verdict.reasons})
            continue
        cards[rel] = card
    return cards, missing, culled


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
        raise ValueError(
            "所有素材都被粗筛判定为废料（全静音/全黑场/画质全差/过短），"
            "没有可用的画面。请换素材，或用 ask_user 向用户说明实情。")

    where: dict = {"quality_in": ["good", "ok"], "max_silence_ratio": 0.8}
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

def _expand_smart_create(wf: dict, ctx: dict) -> tuple[dict, dict]:
    """智能创作（FireRed 一键成片的无配音版）：LLM 文案 + 自动画面编排。

    文案（captions）由 LLM 读镜头卡后创作——创意归模型；本函数只做确定性
    编排：粗筛 → 画质选材（可选 keyword）→ 预算贪心 → fade 转场。
    captions 的铺字幕（逐行均分时间轴、烧录在画面下方）由编译器在
    时间轴推导之后完成（_apply_captions），经 _captions 暂存键传递。
    """
    import shot_select

    captions = [c for c in (wf.get("captions") or []) if str(c).strip()]
    if not captions:
        raise ValueError(
            "smart_create 需要 captions（文案行数组）——请先 analyze_media "
            "读镜头卡，写几条贴合画面的短文案再提交。")
    budget = float(wf.get("budget_seconds") or DEFAULT_BUDGET)
    keyword = (wf.get("keyword") or "").strip()

    cards, missing, culled = _usable_cards(ctx, None)
    if missing:
        raise ValueError(
            "以下素材还没有内容索引：" + "、".join(missing) +
            "。请先对这些素材调用 analyze_media（通常上传后已自动建好）。")
    if not cards:
        raise ValueError("所有素材都被粗筛判定为废料，没有可用画面。")

    where: dict = {"quality_in": ["good", "ok"], "max_silence_ratio": 0.8}
    if keyword:
        where["text_any"] = [keyword]
    res = shot_select.select_shots(cards, {
        "sources": sorted(cards), "where": where,
        "budget_seconds": budget, "order": "best_first"})
    if not res["clips"]:
        raise ValueError(
            f"smart_create 没有挑到可用镜头（{res['matched']} 个候选全部被拒）。"
            f"请调大 budget_seconds 或放宽条件。")

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
        "_captions": captions,          # 编译器推导时间轴后消费（见 _apply_captions）
    }
    report = {
        "workflow": "smart_create",
        "captions": len(captions),
        "culled": culled,
        "picked": len(res["picked"]),
        "rejected_total": len(res.get("rejected") or []),
        "budget_seconds": budget,
        "keyword": keyword or None,
    }
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
                "  (`transition` seconds, 0 = hard cuts). No hand-written trims."
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
            summary="smart one-click video with LLM-written captions burned in.",
            expand=_expand_smart_create,
            prompt_doc=(
                "  `smart_create` — creative one-click: YOU write the captions\n"
                "  (short punchy lines, Chinese, 3-8 lines). Call analyze_media\n"
                "  first to see what the footage actually shows, then submit\n"
                "  {\"workflow\": {\"name\": \"smart_create\", \"captions\": [...],\n"
                "   \"budget_seconds\": 25, \"keyword\": \"...\"}}. The compiler\n"
                "  picks shots, adds fade transitions, spreads your captions\n"
                "  evenly across the timeline (burned at bottom), honors the\n"
                "  `audio` BGM block. Captions are YOUR creative job — write\n"
                "  lines that fit the actual footage."
            ),
        ),
    ]
}


def workflow_menu() -> str:
    """合法 workflow 名清单（进校验错误信息）。"""
    return "/".join(sorted(WORKFLOWS))


def render_workflow_doc() -> str:
    """派生提示词里的 workflow 用法文档（与 skills.render_prompt_doc 同思想）。"""
    lines = ["## Workflows (V7 one-click macros — replace clips/timeline/overlays)",
             "When the request is vague (\"just edit it for me\" / one-click),",
             "submit a top-level `workflow` block instead of hand-writing clips:"]
    for w in WORKFLOWS.values():
        lines.append(f"- `{w.name}`: {w.summary}")
        if w.prompt_doc:
            lines.append(w.prompt_doc)
    lines.append(
        "  Shape: {\"workflow\": {\"name\": \"one_click_reel\", \"budget_seconds\": 30,\n"
        "            \"transition\": 0.3, \"keyword\": \"sunset\"}}\n"
        "  `budget_seconds`/`transition`/`keyword`/`order` are all optional.\n"
        "  Every workflow still honors `output` and `audio` (BGM) blocks."
    )
    return "\n".join(lines)
