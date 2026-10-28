"""计划构建工具集（V10）：LLM 通过工具组合构建/更新剪辑计划 draft。

设计契约（docs/dev-notes-2026-10-28.md）：
- 首轮生成从「一次性 submit_plan 手写整份 JSON」改为工具逐步构建——
  每次操作的参数极少且就地校验，错误单步返回（反馈回路最短）；
- 单步校验（引用存在/编号范围/类型）就地做；全量校验（转场<片段、
  overlay 越界等跨片段规则）在 validate_plan()/submit() 做——片段没加完
  时全量规则会误报；
- shot 引用 + probe 宿主回填（「引用代替誊抄」）：add_clips 的参数里
  不出现时间戳誊抄与 probe 抄写这两个最大错误源；
- submit() 后的编译/校验/dry-run/渲染/回验链零改动（draft 就是原来的
  计划 JSON，shot 引用由编译器 _resolve_shot_refs 展开）。

draft 状态挂宿主侧（模块级）：RUN_LOCK 串行 + CLI 单发，同一时刻只有
一个 run 在构建；重试不重置 draft——模型只需修正出问题的部分再 submit。
"""

from __future__ import annotations

import os

from langchain_core.tools import tool

import content_analysis
import plan_schema
from skills import SKILLS, check_args_keys, effects_menu, validate_audio

PROJECT_ROOT = "."          # 由 video_agent 装配时注入（测试可覆写）

# 模块级 draft 状态：{"plan": dict|None, "submitted": bool, "validated": str|None}
_STATE: dict = {"plan": None, "submitted": False, "validated": None}


# ------------------------------------------------------------------ 宿主 API -- #

def reset_draft(plan: dict | None = None) -> None:
    """新 run 开始时初始化（修订轮将来可传 last_plan 作初态）。"""
    _STATE.update(plan=plan, submitted=False, validated=None)


def take_submitted() -> dict | None:
    """取走已 submit 的 draft 计划（消费型；未提交/已消费返回 None）。"""
    if not _STATE["submitted"] or _STATE["plan"] is None:
        return None
    _STATE["submitted"] = False            # 消费：重试轮需模型再次 submit
    return _STATE["plan"]


def has_draft_activity() -> bool:
    """本轮 run 是否用过构建工具（plan_with_retry 据此选择回传措辞）。"""
    return _STATE["plan"] is not None


# ------------------------------------------------------------------ 内部工具 -- #

def _draft() -> dict | None:
    return _STATE["plan"]


def _need_draft() -> dict | None:
    if _STATE["plan"] is None:
        return None
    return _STATE["plan"]


def _next_id(plan: dict) -> str:
    import re
    mx = 0
    for c in plan.get("clips") or []:
        m = re.match(r"^[Cc](\d+)$", str(c.get("id") or ""))
        if m:
            mx = max(mx, int(m.group(1)))
    return f"c{mx + 1}"


def _shot_span(src: str, shot: int) -> tuple[float, float] | None:
    """从内容卡取第 shot 个镜头（1 起）的 (start, end)。"""
    card = content_analysis.load_cached_card(PROJECT_ROOT, src) if src else None
    shots = (card or {}).get("shots") or []
    if not (isinstance(shot, int) and 1 <= shot <= len(shots)):
        return None
    sh = shots[shot - 1]
    try:
        return float(sh.get("start") or 0.0), float(sh.get("end") or 0.0)
    except (TypeError, ValueError):
        return None


def _clip_dur_display(c: dict) -> str:
    """摘要用时长（shot 引用从卡上取边界；与编译展开同源）。"""
    ts, te = c.get("trim_start"), c.get("trim_end")
    if isinstance(ts, (int, float)) and isinstance(te, (int, float)):
        return f"{te - ts:g}s"
    span = _shot_span(c.get("source") or "", c.get("shot") or 0) \
        if c.get("shot") else None
    if span:
        return f"{span[1] - span[0]:g}s"
    if c.get("kind") == "image" and isinstance(c.get("duration"), (int, float)):
        return f"{c['duration']:g}s"
    return "?"


def _validate_clip_item(item: dict, project_root: str) -> list[str]:
    """add_clips 单项校验：复用 plan_schema 全部单片段规则（含 shot 引用、
    未知字段、素材存在性）——用合成单片段计划跑一遍，零规则重复。"""
    probe = {"duration": 10.0, "width": 1920, "height": 1080, "fps": 30,
             "has_audio": True, "has_video": True}
    synthetic = {
        "schema_version": "2.0",
        "output": {"filename": "OUTPUT/_draft_check.mp4",
                   "resolution": {"width": 640, "height": 360}},
        "clips": [dict(item, id="cX", probe=item.get("probe") or probe)],
        "timeline": [{"clip": "cX"}],
    }
    errs = plan_schema.validate_plan(synthetic, project_root)
    # 剔除与上下文相关的误报：合成计划里没有转场/时长推导问题
    return [e for e in errs if "cX" in e or "clips[0]" in e]


def _resolved_copy(plan: dict) -> dict:
    """shot 引用解析成卡上边界的校验副本（不动 draft 本体）。

    plan_schema 的跨片段规则（转场 < 片段时长、overlay 越界）依赖
    clip_duration，而 shot 引用未展开时取不到时长 → 全量校验会漏放。
    validate/submit 用这份副本跑规则；draft 本体保持 shot 形态，
    展开仍由编译器 _resolve_shot_tags 完成（单一展开点）。
    """
    import copy
    snap = copy.deepcopy(plan)
    for c in snap.get("clips") or []:
        shot = c.get("shot")
        if shot is None:
            continue
        span = _shot_span(c.get("source") or "", shot)
        if span:
            c["trim_start"], c["trim_end"] = round(span[0], 3), round(span[1], 3)
        c.pop("shot", None)          # 校验副本里 trim/shot 互斥，解析后须移除
    return snap


def _summary(plan: dict) -> str:
    """draft 紧凑状态摘要（get_plan/各工具返回值共用）。"""
    out = plan.get("output") or {}
    lines = [f"draft「{out.get('filename', '?')}」"
             f" {out.get('resolution', {}).get('width')}x"
             f"{out.get('resolution', {}).get('height')}"]
    trans = {t.get("clip"): t.get("transition")
             for t in plan.get("timeline") or []}
    total = 0.0
    for c in plan.get("clips") or []:
        tr = trans.get(c.get("id"))
        tr_s = "硬切" if not tr else f"{tr.get('type')}{tr.get('duration')}s"
        dur = _clip_dur_display(c)
        try:
            total += float(dur.rstrip("s"))
        except ValueError:
            pass
        tr_dur = (tr or {}).get("duration") or 0
        total -= tr_dur if isinstance(tr_dur, (int, float)) else 0
        src_s = str(c.get("source", "")).rsplit("/", 1)[-1]
        if c.get("shot"):
            ref = f"#{c['shot']}"
        else:
            ts_v = c.get("trim_start")
            te_v = c.get("trim_end")
            ts_s = f"{ts_v:g}" if isinstance(ts_v, (int, float)) else "0"
            te_s = f"{te_v:g}" if isinstance(te_v, (int, float)) else "?"
            ref = f"{ts_s}-{te_s}s"
        eff = ",".join(e.get("name", "?") if isinstance(e, dict) else str(e)
                       for e in (c.get("effects") or [])) or "-"
        mute = " [静音]" if (c.get("audio") or {}).get("mute") else ""
        lines.append(f"  {c.get('id')}  {src_s} {ref}  {dur}  转场:{tr_s}"
                     f"  效果:{eff}{mute}")
    n_trans = sum(1 for t in (plan.get("timeline") or [])
                  if t.get("transition"))
    lines.append(f"共 {len(plan.get('clips') or [])} 片段 / 约 {round(total, 1)}s"
                 f"（{n_trans} 个转场）")
    audio = plan.get("audio")
    if audio:
        lines.append(f"BGM: {str(audio.get('source', '')).rsplit('/', 1)[-1]}"
                     f" 音量{audio.get('volume', 0.3)}"
                     f"{' 卡点' if audio.get('snap_beats') else ''}")
    else:
        lines.append("BGM: 无")
    ovs = plan.get("overlays") or []
    if ovs:
        lines.append(f"文字/画中画 {len(ovs)} 条：" + "、".join(
            f"[{i}]{str(o.get('text') or o.get('source') or '')[:12]}"
            for i, o in enumerate(ovs)))
    narr = plan.get("_narration")
    if narr:
        lines.append(f"旁白 {len(narr)} 组（title={plan.get('_title') or '无'}）")
    elif plan.get("_captions"):
        lines.append(f"字幕 {len(plan['_captions'])} 句"
                     f"（style={plan.get('_caption_style') or 'bottom'}）")
    if _STATE["validated"]:
        lines.append(f"上次试算：{_STATE['validated']}")
    return "\n".join(lines)


# ------------------------------------------------------------------- 工具 -- #

@tool
def new_plan(filename: str, width: int, height: int, fps: int = 30) -> str:
    """Initialize a new editing-plan draft (call this FIRST, before any clip
    tools). Returns a confirmation plus next-step hints.

    Args:
        filename: output file name, e.g. "OUTPUT/my_video.mp4" (OUTPUT/ prefix).
        width: canvas width in px (positive even number, e.g. 1280; 720 for vertical use 720).
        height: canvas height in px (positive even number, e.g. 720; vertical use 1280).
        fps: output fps, default 30.
    """
    if not (isinstance(width, int) and width > 0 and width % 2 == 0
            and isinstance(height, int) and height > 0 and height % 2 == 0):
        return f"✗ width/height 必须是正偶数（收到 {width}x{height}）。"
    fn = filename if filename.startswith("OUTPUT/") else f"OUTPUT/{filename}"
    _STATE.update(
        plan={"schema_version": "2.0",
              "output": {"filename": fn,
                         "resolution": {"width": width, "height": height},
                         "fps": fps},
              "clips": [], "timeline": [], "overlays": []},
        submitted=False, validated=None)
    return ("✓ draft 已初始化。下一步：add_clips 添加片段（优先用 shot 编号"
            "引用 list_shots 的镜头），然后 set_transition / set_audio / "
            "set_narration，最后 validate_plan → submit。\n" + _summary(_draft()))


@tool
def add_clips(clips: list, after: str | None = None) -> str:
    """Append clips to the draft (batch, one call for 1..N clips — PREFER one
    call for the whole sequence). Each clip item is a dict:
      {"source": "INPUT/a.mp4", "shot": 3}            ← PREFERRED: reference
        the N-th shot from list_shots (INPUT/a.mp4#03); exact card boundaries
        are filled by the host — no timestamp copying, no probe writing.
      {"source": "INPUT/a.mp4", "trim_start": 12, "trim_end": 18.5}   ← only
        when splitting/merging shots
      {"source": "INPUT/img.jpg", "kind": "image", "duration": 3}     ← stills
    Do NOT write "probe" — the host fills it. kind is auto-detected for videos.
    Args:
        after: insert position AFTER this clip id (omit = append at end).
    """
    plan = _draft()
    if plan is None:
        return "✗ 还没有 draft——先调用 new_plan 初始化。"
    if not isinstance(clips, list) or not clips:
        return "✗ clips 必须是非空数组，每项 {source, shot?/trim?, duration?}。"
    idx = None
    if after is not None:
        tl_ids = [t.get("clip") for t in plan["timeline"]]
        if after not in tl_ids:
            return f"✗ after={after} 不在当前时间轴里（有 {tl_ids}）。"
        idx = tl_ids.index(after) + 1
    results, added = [], 0
    for i, item in enumerate(clips):
        if not isinstance(item, dict):
            results.append(f"  [{i}] ✗ 必须是对象")
            continue
        item = dict(item)
        item.pop("probe", None)                    # probe 一律宿主回填
        if "kind" not in item:
            ext = os.path.splitext(str(item.get("source") or ""))[1].lower()
            item["kind"] = "image" if ext in (".jpg", ".jpeg", ".png",
                                              ".bmp", ".webp", ".gif") else "video"
        errs = _validate_clip_item(item, PROJECT_ROOT)
        if errs:
            results.append(f"  [{i}] ✗ " + "；".join(errs[:2]))
            continue
        cid = _next_id(plan)
        item["id"] = cid
        plan["clips"].append(item)
        plan["timeline"].insert(len(plan["timeline"]) if idx is None
                                else idx + added, {"clip": cid})
        span_s = f" #{item['shot']}" if item.get("shot") else ""
        results.append(f"  [{i}] ✓ {cid} = {str(item['source']).rsplit('/', 1)[-1]}"
                       f"{span_s}（{_clip_dur_display(item)}）")
        added += 1
    _STATE["validated"] = None
    head = f"添加 {added}/{len(clips)} 个片段：\n" + "\n".join(results)
    return head + "\n" + _summary(plan)


@tool
def set_clip(clip: str, trim_start: float | None = None,
             trim_end: float | None = None, shot: int | None = None,
             mute: bool | None = None, volume: float | None = None) -> str:
    """Modify ONE existing clip in the draft (only the fields you pass change).
    Use shot=N to re-reference a shot, or trim_start/trim_end for custom cuts.
    Args:
        clip: clip id, e.g. "c2" (verbatim from the draft — case sensitive).
        mute: True mutes this clip's original sound; False unmutes.
        volume: clip-level original-sound volume (0, 1].
    """
    plan, c = _draft(), None
    if plan is None:
        return "✗ 还没有 draft。"
    c = next((x for x in plan["clips"] if x.get("id") == clip), None)
    if c is None:
        ids = "、".join(x.get("id", "?") for x in plan["clips"]) or "（无）"
        return f"✗ 片段 {clip} 不存在（id 大小写敏感）。可用：{ids}。"
    if shot is not None:
        span = _shot_span(c.get("source") or "", shot)
        if span is None:
            card = content_analysis.load_cached_card(PROJECT_ROOT, c.get("source") or "")
            n = len((card or {}).get("shots") or [])
            return f"✗ shot={shot} 无效（该素材共 {n} 个镜头）。"
        c.pop("trim_start", None); c.pop("trim_end", None)
        c["shot"] = shot
    if trim_start is not None:
        c["trim_start"] = trim_start; c.pop("shot", None)
    if trim_end is not None:
        c["trim_end"] = trim_end
    audio = c.setdefault("audio", {})
    touched_audio = False
    if mute is not None:
        audio["mute"] = bool(mute); touched_audio = True
    if volume is not None:
        audio["volume"] = volume; touched_audio = True
    if not touched_audio and not audio:
        c.pop("audio", None)
    _STATE["validated"] = None
    return f"✓ 已更新 {clip}。\n" + _summary(plan)


@tool
def set_effect(clip: str, name: str, effect_args: dict | None = None) -> str:
    """Attach an effect to a clip, or update its arguments if already attached
    (same name = update, e.g. re-call with different block_size).
    Args:
        clip: clip id (verbatim, case sensitive).
        name: effect name from the effect menu (hflip, speed, face_mosaic, ...).
        effect_args: effect arguments dict, e.g. {"factor": 0.6} for speed.
    """
    plan = _draft()
    if plan is None:
        return "✗ 还没有 draft。"
    c = next((x for x in plan["clips"] if x.get("id") == clip), None)
    if c is None:
        return f"✗ 片段 {clip} 不存在。"
    skill = SKILLS.get(name)
    if skill is None:
        return f"✗ 效果 {name!r} 不在白名单 {effects_menu()} 内。"
    eargs = effect_args or {}
    errs = check_args_keys(skill, eargs, f"{clip}.{name}") \
        + (skill.validate(eargs, f"{clip}.{name}", c) if skill.validate else [])
    if errs:
        return "✗ " + "；".join(errs)
    effects = c.setdefault("effects", [])
    effects[:] = [e for e in effects if e.get("name") != name]
    effects.append({"name": name, **({"args": eargs} if eargs else {})})
    _STATE["validated"] = None
    return f"✓ {clip} 效果 {name} 已设置。\n" + _summary(plan)


@tool
def remove_effect(clip: str, name: str) -> str:
    """Remove an effect from a clip."""
    plan = _draft()
    if plan is None:
        return "✗ 还没有 draft。"
    c = next((x for x in plan["clips"] if x.get("id") == clip), None)
    if c is None:
        return f"✗ 片段 {clip} 不存在。"
    before = len(c.get("effects") or [])
    c["effects"] = [e for e in (c.get("effects") or [])
                    if e.get("name") != name]
    if len(c["effects"]) == before:
        have = [e.get("name") for e in c.get("effects") or []]
        return f"✗ {clip} 上没有效果 {name}（现有：{have}）。"
    _STATE["validated"] = None
    return f"✓ 已移除 {clip} 的 {name}。"


@tool
def remove_clip(clip: str) -> str:
    """Remove a clip (and overlays attached to it) from the draft."""
    plan = _draft()
    if plan is None:
        return "✗ 还没有 draft。"
    if not any(c.get("id") == clip for c in plan["clips"]):
        return f"✗ 片段 {clip} 不存在。"
    plan["clips"] = [c for c in plan["clips"] if c.get("id") != clip]
    plan["timeline"] = [t for t in plan["timeline"] if t.get("clip") != clip]
    n_ov = len(plan.get("overlays") or [])
    plan["overlays"] = [o for o in (plan.get("overlays") or [])
                        if o.get("at_clip") != clip]
    dropped = n_ov - len(plan["overlays"])
    _STATE["validated"] = None
    note = f"（连带移除 {dropped} 条挂载文字）" if dropped else ""
    return f"✓ 已删除 {clip}{note}。\n" + _summary(plan)


@tool
def move_clip(clip: str, after: str | None = None) -> str:
    """Move a clip in the timeline. after omitted = move to the FRONT."""
    plan = _draft()
    if plan is None:
        return "✗ 还没有 draft。"
    tl_ids = [t.get("clip") for t in plan["timeline"]]
    if clip not in tl_ids:
        return f"✗ 片段 {clip} 不在时间轴里。"
    entry = next(t for t in plan["timeline"] if t.get("clip") == clip)
    plan["timeline"] = [t for t in plan["timeline"] if t.get("clip") != clip]
    if after is None:
        plan["timeline"].insert(0, entry)
    else:
        tl_ids = [t.get("clip") for t in plan["timeline"]]
        if after not in tl_ids:
            return f"✗ after={after} 不在时间轴里。"
        plan["timeline"].insert(
            next(i for i, t in enumerate(plan["timeline"])
                 if t.get("clip") == after) + 1, entry)
    _STATE["validated"] = None
    return f"✓ {clip} 已移动。\n" + _summary(plan)


@tool
def set_transition(clip: str, type: str, duration: float = 0.5) -> str:
    """Set the TRANSITION between `clip` and the clip BEFORE it (transitions
    live on the later clip; the first timeline clip has none — that is a cut).
    Args:
        clip: the later clip id.
        type: one of fade/dissolve/wipeleft/wiperight/wipeup/wipedown/
              slideleft/slideright/slideup/slidedown/circleopen/circleclose;
              use "none" to make it a hard cut.
        duration: transition seconds (must be shorter than both neighbors —
        full validation runs at validate_plan/submit).
    """
    plan = _draft()
    if plan is None:
        return "✗ 还没有 draft。"
    tl = plan["timeline"]
    pos = next((i for i, t in enumerate(tl) if t.get("clip") == clip), None)
    if pos is None:
        return f"✗ 片段 {clip} 不在时间轴里。"
    if pos == 0:
        return f"✗ {clip} 是首个片段，没有前置转场（首段即硬切）。"
    if type != "none" and type not in plan_schema.TRANSITIONS:
        return f"✗ 转场类型 {type!r} 不在白名单 {sorted(plan_schema.TRANSITIONS)}。"
    if not (isinstance(duration, (int, float)) and 0 < duration < 5):
        return f"✗ duration 必须在 (0, 5) 秒内。"
    tl[pos]["transition"] = None if type == "none" else \
        {"type": type, "duration": float(duration)}
    _STATE["validated"] = None
    return f"✓ {clip} 前置转场已设置。\n" + _summary(plan)


@tool
def set_audio(source: str, volume: float = 0.3, fade_in: float = 0.5,
              fade_out: float = 1.0, loop: bool = True,
              original: str = "mute", ducking: bool = False,
              snap_beats: bool = False) -> str:
    """Set / replace the background music (tracks come from list_music).
    original="mute" (default) replaces original sound; "keep" mixes both
    (then ducking=True auto-dips music under speech). snap_beats=True aligns
    cut points to the music's beats (within ±0.35s when durations allow).
    """
    plan = _draft()
    if plan is None:
        return "✗ 还没有 draft。"
    audio = {"source": source, "volume": volume, "fade_in": fade_in,
             "fade_out": fade_out, "loop": loop, "original": original,
             "ducking": ducking, "snap_beats": snap_beats}
    errs = validate_audio(audio, "audio", PROJECT_ROOT)
    if errs:
        return "✗ " + "；".join(errs)
    plan["audio"] = audio
    _STATE["validated"] = None
    return f"✓ BGM 已设置。\n" + _summary(plan)


@tool
def clear_audio() -> str:
    """Remove the background music from the draft (original sound only)."""
    plan = _draft()
    if plan is None:
        return "✗ 还没有 draft。"
    plan.pop("audio", None)
    _STATE["validated"] = None
    return "✓ BGM 已清除。\n" + _summary(plan)


@tool
def add_text(text: str, at_clip: str, duration: float,
             start_offset: float = 0, position: str = "bottom",
             font_size: int | None = None, color: str = "#FFFFFF") -> str:
    """Add a text overlay attached to ONE clip (title, captions...).
    start_offset/duration are seconds WITHIN that clip; full validation
    (offset+duration ≤ clip length) runs at validate_plan/submit.
    position: top-left/top/top-right/left/center/right/bottom-left/bottom/
    bottom-right.
    """
    plan = _draft()
    if plan is None:
        return "✗ 还没有 draft。"
    if not any(c.get("id") == at_clip for c in plan["clips"]):
        return f"✗ 片段 {at_clip} 不存在。"
    if not (isinstance(duration, (int, float)) and duration > 0):
        return "✗ duration 必须为正数（秒）。"
    ov = {"type": "text", "text": text, "at_clip": at_clip,
          "start_offset": float(start_offset), "duration": float(duration),
          "position": position, "color": color}
    if font_size is not None:
        ov["font_size"] = font_size
    plan.setdefault("overlays", []).append(ov)
    _STATE["validated"] = None
    return f"✓ 文字已加到 {at_clip}（overlay[{len(plan['overlays']) - 1}]）。\n" \
        + _summary(plan)


@tool
def remove_overlay(index: int) -> str:
    """Remove an overlay by its index number (see get_plan listing, 0-based)."""
    plan = _draft()
    if plan is None:
        return "✗ 还没有 draft。"
    ovs = plan.get("overlays") or []
    if not (isinstance(index, int) and 0 <= index < len(ovs)):
        return f"✗ index={index} 超范围（当前 {len(ovs)} 条，0~{len(ovs) - 1}）。"
    ovs.pop(index)
    _STATE["validated"] = None
    return f"✓ overlay[{index}] 已移除。"


@tool
def set_narration(groups: list, title: str | None = None) -> str:
    """Set narration (旁白制): groups cover ALL clips in narrative order.
    Each group: {"clip_ids": ["c1","c2"], "text": "整段旁白原文",
                 "units": ["句一。", "句二。"]} — units optional (compiler
    splits text by punctuation). Subtitle timing is computed by the compiler
    from each group's time window — you never write timestamps.
    """
    plan = _draft()
    if plan is None:
        return "✗ 还没有 draft。"
    ids = {c.get("id") for c in plan["clips"]}
    errs = []
    for i, g in enumerate(groups if isinstance(groups, list) else []):
        if not isinstance(g, dict) or not g.get("clip_ids"):
            errs.append(f"groups[{i}] 缺 clip_ids")
            continue
        bad = [x for x in g["clip_ids"] if x not in ids]
        if bad:
            errs.append(f"groups[{i}].clip_ids 引用不存在的片段 {bad}")
    if errs:
        return "✗ " + "；".join(errs)
    plan["_narration"] = groups
    plan.pop("_captions", None); plan.pop("_caption_style", None)
    if title is not None:
        plan["_title"] = title
    _STATE["validated"] = None
    return f"✓ 旁白 {len(groups)} 组已设置。\n" + _summary(plan)


@tool
def set_captions(lines: list, style: str = "bottom") -> str:
    """Simple captions: one short line per clip, in timeline order.
    style: "bottom" (subtitle bar) or "credits" (centered ending credits)."""
    plan = _draft()
    if plan is None:
        return "✗ 还没有 draft。"
    if not (isinstance(lines, list) and lines
            and all(isinstance(x, str) and x.strip() for x in lines)):
        return "✗ lines 必须是非空字符串数组。"
    plan["_captions"] = lines
    plan["_caption_style"] = style
    plan.pop("_narration", None); plan.pop("_title", None)
    _STATE["validated"] = None
    return f"✓ 字幕 {len(lines)} 句已设置。\n" + _summary(plan)


@tool
def get_plan() -> str:
    """Show the current draft: clip table (id / source / range / duration /
    transition / effects), BGM, overlays, narration, estimated total length
    and last validation result. Call this whenever you are unsure what the
    draft looks like so far."""
    plan = _draft()
    if plan is None:
        return "还没有 draft（先 new_plan 初始化）。"
    return _summary(plan)


@tool
def validate_plan() -> str:
    """Run FULL validation on the draft (cross-clip rules: transitions vs
    clip lengths, overlay bounds, narration shapes...). Always call this
    before submit; fix reported issues with the plan tools, then submit."""
    plan = _draft()
    if plan is None:
        return "✗ 还没有 draft。"
    errs = plan_schema.validate_plan(_resolved_copy(plan), PROJECT_ROOT)
    if errs:
        _STATE["validated"] = f"✗ {len(errs)} 处问题"
        return "试算发现以下问题（用工具修正后再 submit）：\n- " \
            + "\n- ".join(errs)
    _STATE["validated"] = "✓ 通过"
    return "✓ 试算通过，可以 submit。"


@tool
def submit() -> str:
    """Submit the draft for compilation and rendering. Runs full validation
    first; on success the host compiles (dry-run → render → verify) and the
    draft becomes the session's current plan. Ask via ask_user instead if the
    request cannot be fulfilled."""
    plan = _draft()
    if plan is None:
        return "✗ 还没有 draft。"
    if not plan.get("clips"):
        return "✗ draft 里还没有片段（先 add_clips）。"
    errs = plan_schema.validate_plan(_resolved_copy(plan), PROJECT_ROOT)
    if errs:
        _STATE["validated"] = f"✗ {len(errs)} 处问题"
        return "✗ submit 被拦下（未提交），先用工具修正：\n- " \
            + "\n- ".join(errs)
    _STATE["submitted"] = True
    _STATE["validated"] = "✓ 通过（已提交）"
    return "✓ 已提交编译执行。\n" + _summary(plan)


PLAN_TOOLS = [new_plan, add_clips, set_clip, set_effect, remove_effect,
              remove_clip, move_clip, set_transition, set_audio, clear_audio,
              add_text, remove_overlay, set_narration, set_captions,
              get_plan, validate_plan, submit]
