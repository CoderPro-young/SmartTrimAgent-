"""编辑计划 JSON 的 schema 定义与校验器。

LLM 通过 submit_plan 工具提交计划（dict），本模块负责：
- 结构校验：字段齐全、类型正确、白名单（转场/效果/位置）
- 语义校验：时间不越界、引用存在、转场时长 < 相邻片段时长

校验失败返回错误列表（中文，可直接回传给 LLM 重新出计划）。
详细设计见 docs/v2.0-multi-material-editing.md §4。
"""

from __future__ import annotations

import os

# ---- 白名单 ----

TRANSITIONS = {
    "fade", "dissolve", "wipeleft", "wiperight", "wipeup", "wipedown",
    "slideleft", "slideright", "slideup", "slidedown",
    "circleopen", "circleclose",
}

EFFECTS = {"hflip", "vflip", "transpose", "eq"}

POSITIONS = {
    "top-left", "top", "top-right",
    "left", "center", "right",
    "bottom-left", "bottom", "bottom-right",
}

SCHEMA_VERSION = "2.0"


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def clip_duration(clip: dict) -> float | None:
    """计算 clip 裁剪后的片段时长 d_i；信息不足返回 None。

    video: trim_end - trim_start（trim_end 为 null 时需 probe.duration）
    image: duration
    """
    if clip.get("kind") == "image":
        return clip.get("duration")
    ts = clip.get("trim_start") or 0
    te = clip.get("trim_end")
    if te is None:
        probe = clip.get("probe") or {}
        dur = probe.get("duration")
        if dur is None:
            return None
        te = dur
    return round(te - ts, 3)


def validate_plan(plan: dict, project_root: str) -> list[str]:
    """校验计划，返回错误列表（空列表 = 通过）。"""
    errors: list[str] = []

    if not isinstance(plan, dict):
        return ["计划必须是 JSON 对象。"]

    # ---- output ----
    out = plan.get("output")
    if not isinstance(out, dict):
        errors.append("缺少 output 字段（输出文件名/分辨率/帧率）。")
    else:
        fn = out.get("filename", "")
        if not isinstance(fn, str) or not fn.startswith("OUTPUT/"):
            errors.append("output.filename 必须以 OUTPUT/ 开头，如 OUTPUT/final.mp4。")
        res = out.get("resolution")
        if not (isinstance(res, dict) and _num(res.get("width")) and _num(res.get("height"))
                and res["width"] > 0 and res["height"] > 0
                and res["width"] % 2 == 0 and res["height"] % 2 == 0):
            errors.append("output.resolution 必须是正偶数宽高，如 {\"width\":1280,\"height\":720}。")
        fps = out.get("fps", 30)
        if not (_num(fps) and fps > 0):
            errors.append("output.fps 必须为正数（默认 30）。")

    # ---- clips ----
    clips = plan.get("clips")
    if not isinstance(clips, list) or not clips:
        errors.append("clips 不能为空（至少一个素材进入时间轴）。")
        clips = []
    ids: set[str] = set()
    for i, c in enumerate(clips):
        tag = f"clips[{i}]"
        if not isinstance(c, dict):
            errors.append(f"{tag} 必须是对象。")
            continue
        cid = c.get("id")
        if not isinstance(cid, str) or not cid:
            errors.append(f"{tag}.id 必须是非空字符串。")
        elif cid in ids:
            errors.append(f"clip id 重复：{cid}。")
        else:
            ids.add(cid)

        src = c.get("source", "")
        if not isinstance(src, str) or not src.startswith("INPUT/"):
            errors.append(f"{tag}.source 必须以 INPUT/ 开头。")
        elif not os.path.isfile(os.path.join(project_root, src)):
            errors.append(f"{tag}.source 文件不存在：{src}。")

        kind = c.get("kind")
        if kind not in ("video", "image"):
            errors.append(f"{tag}.kind 必须是 video 或 image。")
        elif kind == "image":
            if not (_num(c.get("duration")) and c["duration"] > 0):
                errors.append(f"{tag} 是图片，必须给正数 duration（展示秒数）。")
        else:
            ts, te = c.get("trim_start"), c.get("trim_end")
            if ts is not None and (not _num(ts) or ts < 0):
                errors.append(f"{tag}.trim_start 必须是非负数。")
            if te is not None and (not _num(te) or (ts is not None and te <= ts)):
                errors.append(f"{tag}.trim_end 必须大于 trim_start。")
            if clip_duration(c) is None:
                errors.append(
                    f"{tag} 无法确定片段时长：ffprobe 不可用时，video 素材必须显式给出 "
                    f"trim_end（probe.duration 未知不能为 null）。"
                )
            probe = c.get("probe")
            if probe is not None and not isinstance(probe, dict):
                errors.append(f"{tag}.probe 必须是对象。")

        # effects（v2 扩展点①，可选）
        for j, eff in enumerate(c.get("effects") or []):
            name = eff.get("name") if isinstance(eff, dict) else None
            if name not in EFFECTS:
                errors.append(f"{tag}.effects[{j}].name 必须在白名单 {sorted(EFFECTS)} 内。")

    # ---- timeline ----
    timeline = plan.get("timeline")
    if not isinstance(timeline, list) or not timeline:
        errors.append("timeline 不能为空（至少一个条目）。")
        timeline = []
    for i, item in enumerate(timeline):
        tag = f"timeline[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{tag} 必须是对象。")
            continue
        if "clip" not in item:
            errors.append(f"{tag} 缺少 clip 字段（v2.0 不支持 layout 布局块，见详细设计 §12）。")
        elif item["clip"] not in ids:
            errors.append(f"{tag}.clip 引用了不存在的 clip：{item['clip']}。")
        tr = item.get("transition")
        if tr is not None:
            if not isinstance(tr, dict) or tr.get("type") not in TRANSITIONS:
                errors.append(
                    f"{tag}.transition.type 必须在白名单 {sorted(TRANSITIONS)} 内。"
                )
            elif not (_num(tr.get("duration")) and 0 < tr["duration"]):
                errors.append(f"{tag}.transition.duration 必须为正数。")

    # ---- overlays ----
    overlays = plan.get("overlays") or []
    if not isinstance(overlays, list):
        errors.append("overlays 必须是数组。")
        overlays = []
    for i, ov in enumerate(overlays):
        tag = f"overlays[{i}]"
        if not isinstance(ov, dict):
            errors.append(f"{tag} 必须是对象。")
            continue
        typ = ov.get("type")
        if typ not in ("pip", "text"):
            errors.append(f"{tag}.type 必须是 pip 或 text。")
            continue
        at = ov.get("at_clip")
        if at not in ids:
            errors.append(f"{tag}.at_clip 引用了不存在的 clip：{at}。")
            continue
        # 时间语义：start_offset 基于【裁剪后片段内时间】
        so, dur = ov.get("start_offset"), ov.get("duration")
        if not (_num(so) and so >= 0):
            errors.append(f"{tag}.start_offset 必须是非负数（相对裁剪后片段内时间）。")
            continue
        if not (_num(dur) and dur > 0):
            errors.append(f"{tag}.duration 必须为正数。")
            continue
        clip = next(c for c in clips if c.get("id") == at)
        d = clip_duration(clip)
        if d is not None and so + dur > d + 1e-6:
            errors.append(
                f"{tag} 越界：start_offset({so}) + duration({dur}) 超出片段 {at} 的长度 {d}s。"
            )
        if typ == "pip":
            src = ov.get("source", "")
            if not isinstance(src, str) or not os.path.isfile(os.path.join(project_root, src)):
                errors.append(f"{tag}.source 文件不存在：{src}（素材须放在 INPUT/）。")
            kind = ov.get("kind")
            if kind not in ("video", "image"):
                errors.append(f"{tag}.kind 必须是 video 或 image。")
            pos = ov.get("position", "bottom-right")
            if pos not in POSITIONS:
                errors.append(f"{tag}.position 必须是九宫格之一：{sorted(POSITIONS)}。")
            scale = ov.get("scale", 0.25)
            if not (_num(scale) and 0 < scale <= 1):
                errors.append(f"{tag}.scale 必须在 (0, 1] 内。")
        else:  # text
            if not isinstance(ov.get("text"), str) or not ov["text"].strip():
                errors.append(f"{tag}.text 不能为空。")
            fs = ov.get("font_size", 48)
            if not (_num(fs) and fs > 0):
                errors.append(f"{tag}.font_size 必须为正数。")
            pos = ov.get("position", "center")
            if pos not in POSITIONS:
                errors.append(f"{tag}.position 必须是九宫格之一：{sorted(POSITIONS)}。")

    # ---- 转场时长 < 相邻片段时长（需要片段时长可得）----
    for i in range(1, len(timeline)):
        item = timeline[i]
        if not isinstance(item, dict):
            continue
        tr = item.get("transition")
        if not (isinstance(tr, dict) and _num(tr.get("duration"))):
            continue
        prev_c = next((c for c in clips if c.get("id") == timeline[i - 1].get("clip")), None)
        cur_c = next((c for c in clips if c.get("id") == item.get("clip")), None)
        d_prev = clip_duration(prev_c) if prev_c else None
        d_cur = clip_duration(cur_c) if cur_c else None
        if d_prev and tr["duration"] >= d_prev:
            errors.append(f"timeline[{i}] 转场时长 {tr['duration']}s 不小于前一片段长度 {d_prev}s。")
        if d_cur and tr["duration"] >= d_cur:
            errors.append(f"timeline[{i}] 转场时长 {tr['duration']}s 不小于当前片段长度 {d_cur}s。")

    return errors
