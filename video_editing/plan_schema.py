"""编辑计划 JSON 的 schema 定义与校验器。

LLM 通过 submit_plan 工具提交计划（dict），本模块负责：
- 结构校验：字段齐全、类型正确、白名单（转场/效果/位置）
- 语义校验：时间不越界、引用存在、转场时长 < 相邻片段时长
- 素材匹配校验（Preflight Tier 2，check_material_fit）：计划要求 × 素材现实

effects 的参数校验**不在本文件硬编码**，而是从 skills.SKILLS 注册表派生
（一处定义 → 校验/编译/提示词三处同源，见 skills.py 顶部说明）。

校验失败返回错误列表（中文，可直接回传给 LLM 重新出计划）。
详细设计见 docs/v2.0-multi-material-editing.md §4；
多轮/反问背景见 docs/v3.1-multi-turn-interaction.md §5.3。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from skills import SKILLS, check_args_keys, effects_menu, validate_audio

# ---- 白名单 ----

TRANSITIONS = {
    "fade", "dissolve", "wipeleft", "wiperight", "wipeup", "wipedown",
    "slideleft", "slideright", "slideup", "slidedown",
    "circleopen", "circleclose",
}

# effects 白名单来自注册表（skills.py），不再手工维护。
EFFECTS = set(SKILLS)

POSITIONS = {
    "top-left", "top", "top-right",
    "left", "center", "right",
    "bottom-left", "bottom", "bottom-right",
}

# ---- 各层允许出现的字段（未知字段拒绝）----
# 为什么必须逐层校验：多写的字段会被编译器静默忽略，于是「用户以为做成了、实际没做」。
# 2026-09-16 实测：模型为「2 倍速」臆造了 clip 级字段 "speed"，校验返回 0 错误、
# 成品是原速、系统却报告成功。这是最坏的失败模式，必须堵死。

PLAN_KEYS = {"schema_version", "output", "clips", "timeline", "overlays", "audio"}
OUTPUT_KEYS = {"filename", "resolution", "fps"}
RESOLUTION_KEYS = {"width", "height"}
CLIP_KEYS = {"id", "source", "kind", "trim_start", "trim_end", "duration",
             "probe", "effects"}
EFFECT_KEYS = {"name", "args"}
TIMELINE_KEYS = {"clip", "transition"}
TRANSITION_KEYS = {"type", "duration"}
OVERLAY_BASE_KEYS = {"type", "at_clip", "start_offset", "duration", "position"}
OVERLAY_PIP_KEYS = OVERLAY_BASE_KEYS | {"source", "kind", "scale"}
OVERLAY_TEXT_KEYS = OVERLAY_BASE_KEYS | {"text", "font_size", "color"}

SCHEMA_VERSION = "2.0"


def _available_sources(project_root: str) -> list[str]:
    """INPUT/ 下实际可用的素材名（用于让「文件不存在」的错误可直接自救）。"""
    d = os.path.join(project_root, "INPUT")
    if not os.path.isdir(d):
        return []
    return sorted(
        n for n in os.listdir(d)
        if not n.startswith(".") and os.path.isfile(os.path.join(d, n))
    )


def _missing_source_msg(tag: str, src: str, project_root: str) -> str:
    """素材不存在时，把「实际有哪些」一起回传——模型据此一般一次就能改对。"""
    avail = _available_sources(project_root)
    listing = "、".join(avail) if avail else "（INPUT/ 当前为空）"
    return f"{tag}.source 指向的素材不存在：{src}；INPUT/ 里实际可用的是：{listing}。"


def _unknown_keys(obj: dict, allowed: set, tag: str) -> list[str]:
    """返回「多写的字段」错误（空列表 = 合法）。"""
    extra = [k for k in obj if k not in allowed]
    if not extra:
        return []
    return [
        f"{tag} 含不支持的字段 {extra}；该层只接受：{sorted(allowed)}。"
        f"多写的字段会被编译器忽略（等于没做），因此判为无效计划——"
        f"请删掉它们；若你本想表达某个效果，请改用受支持的能力。"
    ]


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

    errors.extend(_unknown_keys(plan, PLAN_KEYS, "计划顶层"))

    # ---- output ----
    out = plan.get("output")
    if not isinstance(out, dict):
        errors.append("缺少 output 字段（输出文件名/分辨率/帧率）。")
    else:
        errors.extend(_unknown_keys(out, OUTPUT_KEYS, "output"))
        fn = out.get("filename", "")
        if not isinstance(fn, str) or not fn.startswith("OUTPUT/"):
            errors.append("output.filename 必须以 OUTPUT/ 开头，如 OUTPUT/final.mp4。")
        res = out.get("resolution")
        if not (isinstance(res, dict) and _num(res.get("width")) and _num(res.get("height"))
                and res["width"] > 0 and res["height"] > 0
                and res["width"] % 2 == 0 and res["height"] % 2 == 0):
            errors.append("output.resolution 必须是正偶数宽高，如 {\"width\":1280,\"height\":720}。")
        elif isinstance(res, dict):
            errors.extend(_unknown_keys(res, RESOLUTION_KEYS, "output.resolution"))
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
        errors.extend(_unknown_keys(c, CLIP_KEYS, tag))
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
            errors.append(_missing_source_msg(tag, src, project_root))

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
            elif isinstance(probe, dict) and probe.get("has_video") is False:
                errors.append(
                    f"{tag} 指向的素材没有视频轨（纯音频），不能作为 clip——"
                    f"音频请放到顶层 audio.source 作 BGM。"
                )

        # effects（v2 扩展点①）：校验逻辑派发到 skills 注册表
        for j, eff in enumerate(c.get("effects") or []):
            tag_eff = f"{tag}.effects[{j}]"
            if not isinstance(eff, dict):
                errors.append(f"{tag_eff} 必须是对象。")
                continue
            errors.extend(_unknown_keys(eff, EFFECT_KEYS, tag_eff))
            name = eff.get("name")
            skill = SKILLS.get(name)
            if skill is None:
                errors.append(
                    f"{tag_eff}.name 必须在白名单 {effects_menu()} 内"
                    f"（收到 {name!r}）。"
                )
                continue
            args = eff.get("args")
            if args is None:
                args = {}
            if not isinstance(args, dict):
                errors.append(f"{tag_eff}.args 必须是对象。")
                continue
            errors.extend(check_args_keys(skill, args, tag_eff))
            if skill.validate:
                errors.extend(skill.validate(args, tag_eff, c))

    # ---- audio（BGM 轨，可选；规格来自 skills.validate_audio）----
    audio = plan.get("audio")
    if audio is not None:
        if not isinstance(audio, dict):
            errors.append("audio 必须是对象（含 source/volume/fade_in/fade_out/loop/ducking）。")
        else:
            errors.extend(validate_audio(audio, "audio", project_root))

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
        errors.extend(_unknown_keys(item, TIMELINE_KEYS, tag))
        if "clip" not in item:
            errors.append(f"{tag} 缺少 clip 字段（v2.0 不支持 layout 布局块，见详细设计 §12）。")
        elif item["clip"] not in ids:
            errors.append(f"{tag}.clip 引用了不存在的 clip：{item['clip']}。")
        tr = item.get("transition")
        if tr is not None:
            if isinstance(tr, dict):
                errors.extend(_unknown_keys(tr, TRANSITION_KEYS, f"{tag}.transition"))
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
        allowed_ov = OVERLAY_PIP_KEYS if typ == "pip" else OVERLAY_TEXT_KEYS
        errors.extend(_unknown_keys(ov, allowed_ov, tag))
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
                errors.append(f"{tag}.source 文件不存在：{src}（素材须放在 INPUT/）。"
                              f"INPUT/ 里实际可用的是：{_available_sources(project_root)}。")
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


# ----------------------------------------------------------- Preflight ----- #
# 素材-需求匹配校验（Tier 2）。与 validate_plan 分工：
#   validate_plan     → 计划合不合法（结构/白名单/引用/数学）
#   check_material_fit → 计划与素材现实合不合（时长/音轨/分辨率…）
# 详见 docs/v3.1-multi-turn-interaction.md §5.3。

@dataclass
class Mismatch:
    """素材现实与用户要求/计划不符的结构化记录。"""
    type: str                 # over_range / empty_range / no_audio_track / bgm_too_short
                              # / pip_too_short / probe_mismatch / upscale / ...
    tag: str                  # 出问题的位置，如 "clips[0].trim_end"
    requested: object = None  # 想要的
    actual: object = None     # 实际有的
    user_decidable: bool = True   # True=只有用户能决（问用户）；False=模型可修（重试）
    level: str = "block"          # "block"=阻断 | "warn"=提示后继续
    message: str = ""             # 可直接展示给用户的中文事实陈述


def _default_probe_fn(project_root: str):
    """CLI 场景的兜底探针（直接 ffprobe，无缓存）。

    Web 侧应传入服务端带 mtime 缓存的 `_probe_file`，由 server 反向注入
    （plan_schema 不能 import server，避免循环依赖）。
    probe_fn(src) -> {"ok": bool, "duration": float|None, "width": int|None,
                       "height": int|None, "has_audio": bool, "has_video": bool}
    """
    import ffmpeg_exec

    def probe(src: str) -> dict:
        full = os.path.join(project_root, src)
        res = ffmpeg_exec.probe(full)
        if not res.get("available") or not res.get("data"):
            return {"ok": False}
        data = res["data"]
        streams = data.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        if video is None and audio is None:
            return {"ok": False}
        raw = (data.get("format") or {}).get("duration") or (video or audio).get("duration")
        try:
            dur = round(float(raw), 3)
        except (TypeError, ValueError):
            dur = None
        return {
            "ok": True,
            "duration": dur,
            "width": video.get("width") if video else None,
            "height": video.get("height") if video else None,
            "has_audio": audio is not None,
            "has_video": video is not None,
        }

    return probe


def _plan_duration(plan: dict) -> float | None:
    """总时长 D = Σd_i − Σ转场时长（与编译器 _derive 同公式）；信息不足返回 None。"""
    clips = {c.get("id"): c for c in (plan.get("clips") or []) if isinstance(c, dict)}
    total = 0.0
    for item in plan.get("timeline") or []:
        if not isinstance(item, dict):
            continue
        c = clips.get(item.get("clip"))
        if not c:
            continue
        d = clip_duration(c)
        if d is None:
            return None
        total += d
        tr = item.get("transition")
        if isinstance(tr, dict) and _num(tr.get("duration")):
            total -= tr["duration"]
    return round(total, 3)


def check_material_fit(plan: dict, project_root: str, probe_fn=None) -> list[Mismatch]:
    """Preflight Tier 2：素材现实 × 计划要求的确定性匹配校验（每轮必跑）。

    一律以 probe_fn（服务端缓存的真实探针）为准；plan 里模型自报的 probe
    字段只用于一致性比对（probe_mismatch——它本身就是幻觉信号）。
    返回 Mismatch 列表，空列表 = 全部匹配。
    """
    if probe_fn is None:
        probe_fn = _default_probe_fn(project_root)
    out: list[Mismatch] = []

    def real(src: str) -> dict:
        try:
            return probe_fn(src) or {}
        except Exception:
            return {}

    clips = [c for c in (plan.get("clips") or []) if isinstance(c, dict)]

    # ---- 主轨 clip：裁剪范围 & probe 真实性 ----
    for i, c in enumerate(clips):
        tag = f"clips[{i}]"
        src = c.get("source", "")
        if not isinstance(src, str) or not src.startswith("INPUT/"):
            continue
        info = real(src)
        if not info.get("ok"):
            continue
        dur = info.get("duration")
        ts = c.get("trim_start") or 0
        te = c.get("trim_end")
        if c.get("kind") == "video" and _num(dur):
            if _num(te) and te > dur + 0.05:
                out.append(Mismatch(
                    "over_range", f"{tag}.trim_end", te, dur,
                    message=f"素材 {src} 实际时长 {dur}s，计划要裁到 {te}s，"
                            f"超出 {round(te - dur, 2)}s。"))
            if _num(ts) and ts >= dur - 0.001:
                out.append(Mismatch(
                    "empty_range", f"{tag}.trim_start", ts, dur,
                    message=f"素材 {src} 只有 {dur}s，从第 {ts}s 开始裁不到任何内容。"))
        pp = c.get("probe")
        if isinstance(pp, dict) and _num(pp.get("duration")) and _num(dur) \
                and abs(float(pp["duration"]) - dur) > 0.5:
            out.append(Mismatch(
                "probe_mismatch", f"{tag}.probe.duration", pp.get("duration"), dur,
                user_decidable=False,
                message=f"{tag}.probe 声称时长 {pp.get('duration')}s，"
                        f"与真实探测结果 {dur}s 不符。"))
        if isinstance(pp, dict) and "has_audio" in pp and "has_audio" in info \
                and bool(pp["has_audio"]) != bool(info["has_audio"]):
            out.append(Mismatch(
                "probe_mismatch", f"{tag}.probe.has_audio",
                pp.get("has_audio"), info.get("has_audio"),
                user_decidable=False,
                message=f"{tag}.probe 声称 has_audio={pp.get('has_audio')}，"
                        f"实际为 {info.get('has_audio')}。"))

    # ---- BGM：长度 & ducking 前提 ----
    audio = plan.get("audio")
    if isinstance(audio, dict):
        src = audio.get("source")
        if isinstance(src, str) and src.startswith("INPUT/"):
            info = real(src)
            D = _plan_duration(plan)
            if info.get("ok") and _num(info.get("duration")) and D \
                    and not audio.get("loop") and info["duration"] < D - 0.05:
                out.append(Mismatch(
                    "bgm_too_short", "audio.source",
                    f"成片约 {D}s", f"BGM 只有 {info['duration']}s",
                    message=f"BGM {src} 只有 {info['duration']}s，成片约 {D}s "
                            f"且未开启 loop，音乐会中途静默结束。"))
        if audio.get("ducking"):
            any_audio = any(
                real(c.get("source", "")).get("has_audio")
                for c in clips if isinstance(c.get("source"), str)
            )
            if clips and not any_audio:
                out.append(Mismatch(
                    "no_audio_track", "audio.ducking", "ducking 自动压低配乐",
                    "主轨素材全部没有音轨",
                    message="你要求 ducking（有人声时自动压低配乐），但所有主轨素材"
                            "都没有音轨，没有可被压低的人声。"))

    # ---- PiP：视频素材长度 ----
    for i, ov in enumerate(plan.get("overlays") or []):
        if not isinstance(ov, dict) or ov.get("type") != "pip":
            continue
        src = ov.get("source")
        want = ov.get("duration")
        if not isinstance(src, str) or not src.startswith("INPUT/") or not _num(want):
            continue
        info = real(src)
        if info.get("ok") and _num(info.get("duration")) \
                and info["duration"] < want - 0.05:
            out.append(Mismatch(
                "pip_too_short", f"overlays[{i}].duration",
                f"要叠加 {want}s", f"素材只有 {info['duration']}s",
                message=f"画中画素材 {src} 只有 {info['duration']}s，"
                        f"计划叠加 {want}s，播不满。"))

    # ---- upscale：warn 级，不阻断 ----
    out_res = (plan.get("output") or {}).get("resolution") or {}
    ow, oh = out_res.get("width"), out_res.get("height")
    max_w = max(
        (real(c["source"]).get("width") or 0
         for c in clips if isinstance(c.get("source"), str)),
        default=0,
    )
    if _num(ow) and _num(oh) and max_w and ow > 2 * max_w:
        out.append(Mismatch(
            "upscale", "output.resolution", f"{ow}×{oh}",
            f"源素材最大宽度 {max_w}",
            user_decidable=False, level="warn",
            message=f"输出分辨率 {ow}×{oh} 超过源素材最大宽度 {max_w} 的 2 倍，"
                    f"属于放大输出（可能有损清晰度）。"))
    return out
