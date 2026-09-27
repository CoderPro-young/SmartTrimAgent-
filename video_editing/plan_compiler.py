"""编辑计划编译器（工程层）。

职责（对应 docs/v2.0-multi-material-editing.md §2/§5/§6）：
  ① 校验   → plan_schema.validate_plan
  ② 推导   → 画布 / 片段时长 d_i / 起点 S_i / xfade offset / overlay 绝对时间 / 总时长 D
  ③ 生成   → 归一化命令 × N + 主命令（无转场 concat -c copy 快速路径 / 有转场 filter_complex）
  ④ 执行   → 逐条 run_ffmpeg（写 sidecar：concat list、drawtext 文本文件）
  ⑤ 回验   → ffprobe 输出时长 ≈ D

LLM 只产出意图（计划 JSON），本模块把意图变成确定性的命令序列。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import ffmpeg_exec
import signal_detection
from plan_schema import clip_duration, time_scale_of, validate_plan
from skills import is_prepass, is_vf, get_skill

FONT_PATH = "C:/Windows/Fonts/msyh.ttc"
# ffmpeg 的 filtergraph 用 ':' 分隔选项，Windows 路径里的盘符冒号必须转义，
# 否则 drawtext 解析失败（报 "Both text and text file provided"）。
FONT_PATH_FILTER = FONT_PATH.replace(":", "\\:")
MARGIN = 20        # 画中画贴边距
TEXT_MARGIN = 40   # 花字贴边距


class CompileError(Exception):
    """计划编译失败；errors 为中文错误列表，可直接展示或回传 LLM。"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


class Cancelled(Exception):
    """任务被用户取消（Web 端「取消」按钮）。"""


@dataclass
class Command:
    """一条待执行的命令：stage 区分检测/归一化/渲染三个阶段。"""

    stage: str          # "detect" | "normalize" | "render"
    description: str
    argv: list[str]

    @property
    def line(self) -> str:
        return " ".join(self.argv)


@dataclass
class CompileResult:
    """compile_plan 的完整产出，后续 execute/verify 就地填充 prechecks/executes/verify。"""

    plan: dict
    commands: list[Command] = field(default_factory=list)
    sidecars: dict[str, str] = field(default_factory=dict)   # path -> content
    math: dict = field(default_factory=dict)                 # 推导结果，供展示/调试
    expansions: dict = field(default_factory=dict)           # V5 粗剪展开报告（select/cuts）
    prechecks: list[dict] = field(default_factory=list)      # dry-run 预检结果（T5）
    executes: list[dict] = field(default_factory=list)       # 执行结果
    verify: dict | None = None

    def render_summary(self) -> str:
        """把命令序列渲染成可读文本（demo 展示用）。"""
        lines = []
        for c in self.commands:
            lines.append(f"[{c.stage}] {c.description}\n  {c.line}")
        return "\n".join(lines)


def _effects_to_vf(effects: list) -> str:
    """把计划里的 effects 列表翻译成 ffmpeg -vf 滤镜串（如 `hflip,eq=...`）。

    翻译函数来自 skills 注册表（skill.build），本模块不再硬编码效果名。
    PREPASS 类（face_mosaic）不并入滤镜链，由 _prepass_commands 单独处理。
    """
    parts = []
    for e in effects:
        name = e.get("name")
        skill = get_skill(name)
        if skill is None or skill.build is None:
            continue
        expr = skill.build(e.get("args") or {})
        if expr:
            parts.append(expr)
    return ",".join(parts)


def _prepass_commands(cid: str, src: str, effects: list) -> tuple[list[Command], str]:
    """执行 PREPASS 类技能（如人脸打码），返回 (命令列表, 替换后的输入路径)。

    产出 TMP/{cid}_<skill>.mp4，供后续归一化作为输入。多个 prepass 顺序串联。
    """
    commands: list[Command] = []
    for eff in effects:
        name = eff.get("name")
        skill = get_skill(name)
        if skill is None or skill.prepass is None:
            continue
        out = f"TMP/{cid}_{name}.mp4"
        argv = skill.prepass(src, out, eff.get("args") or {})
        commands.append(Command("detect", f"{name} 前置处理 {cid} ({src})", argv))
        src = out
    return commands, src


def _clip_by_id(plan: dict, cid: str) -> dict:
    """按 id 查找 clip 定义（校验阶段已保证存在）。"""
    return next(c for c in plan["clips"] if c["id"] == cid)


def _overlay_pos_expr(position: str, axis: str) -> str:
    """画中画 overlay 的 x/y 表达式（基于 main/overlay 宽高，贴边留 MARGIN）。"""
    edge = str(MARGIN)
    if axis == "x":
        return {
            "top-left": edge, "left": edge, "bottom-left": edge,
            "top": "(main_w-overlay_w)/2", "center": "(main_w-overlay_w)/2",
            "bottom": "(main_w-overlay_w)/2",
            "top-right": f"main_w-overlay_w-{MARGIN}",
            "right": f"main_w-overlay_w-{MARGIN}",
            "bottom-right": f"main_w-overlay_w-{MARGIN}",
        }[position]
    return {
        "top-left": edge, "top": edge, "top-right": edge,
        "left": "(main_h-overlay_h)/2", "center": "(main_h-overlay_h)/2",
        "right": "(main_h-overlay_h)/2",
        "bottom-left": f"main_h-overlay_h-{MARGIN}",
        "bottom": f"main_h-overlay_h-{MARGIN}",
        "bottom-right": f"main_h-overlay_h-{MARGIN}",
    }[position]


def _drawtext_pos(position: str) -> tuple[str, str]:
    """花字 drawtext 的 x/y 表达式（贴边留 TEXT_MARGIN，居中用文本宽高计算）。"""
    m = TEXT_MARGIN
    x = {"left": str(m), "center": "(w-text_w)/2", "right": f"w-text_w-{m}",
         "top-left": str(m), "top": "(w-text_w)/2", "top-right": f"w-text_w-{m}",
         "bottom-left": str(m), "bottom": "(w-text_w)/2", "bottom-right": f"w-text_w-{m}",
         }[position]
    y = {"top": str(m), "top-left": str(m), "top-right": str(m),
         "left": "(h-text_h)/2", "center": "(h-text_h)/2", "right": "(h-text_h)/2",
         "bottom": f"h-text_h-{m}", "bottom-left": f"h-text_h-{m}", "bottom-right": f"h-text_h-{m}",
         }[position]
    return x, y


def _round2(x: float) -> float:
    return round(x, 2)


# --------------------------------------------------------------------------- #
# 推导换算（②）
# --------------------------------------------------------------------------- #

def _derive(plan: dict) -> dict:
    """计算 d_i / S_i / offsets / overlay 绝对时间 / D，返回 math 字典。"""
    W = plan["output"]["resolution"]["width"]
    H = plan["output"]["resolution"]["height"]
    fps = plan["output"].get("fps", 30)

    timeline = plan["timeline"]
    durations = {}      # clip_id -> d_i
    starts = {}         # clip_id -> S_i（按 timeline 顺序，含重复）
    order = []          # timeline 顺序的 clip_id
    trans = {}          # 位置 i（>=1）-> transition dict 或 None

    S = 0.0
    prev_d = 0.0
    for i, item in enumerate(timeline):
        cid = item["clip"]
        clip = _clip_by_id(plan, cid)
        d = clip_duration(clip)
        # T7b：缩放类效果（speed 等）修正 d_i。d_i 是全部时间轴数学的唯一源头，
        # 起点/转场 offset/总时长/overlay 绝对时间随之一并正确。
        scale = time_scale_of((clip or {}).get("effects") or [])
        if scale != 1.0 and d is not None:
            d = _round2(d * scale)
        durations[cid] = d
        order.append(cid)
        t = item.get("transition")
        trans[i] = t
        if i == 0:
            starts[cid] = 0.0
        else:
            S = starts[cid] = _round2(S + prev_d - (t["duration"] if t else 0.0))
        prev_d = d

    D = _round2(starts[order[-1]] + durations[order[-1]])

    # overlay 绝对时间
    overlay_abs = []
    for ov in plan.get("overlays") or []:
        ts = _round2(starts[ov["at_clip"]] + ov["start_offset"])
        overlay_abs.append({
            "type": ov["type"], "at_clip": ov["at_clip"],
            "start": ts, "end": _round2(ts + ov["duration"]),
        })

    # xfade offset = 各 clip 全局起点 S_i（有转场的接缝）
    offsets = {cid: starts[cid] for cid in order if starts[cid] > 0}

    return {
        "width": W, "height": H, "fps": fps,
        "durations": durations, "starts": starts, "order": order,
        "transitions": trans, "D": D, "offsets": offsets,
        "overlays": overlay_abs,
    }


# --------------------------------------------------------------------------- #
# 命令生成（③）
# --------------------------------------------------------------------------- #

def _norm_cache_path(project_root: str, clip: dict, src: str, vf: str,
                     af: str, W: int, H: int, fps, d) -> str:
    """归一化产物的内容寻址缓存路径（V7 性能优化）。

    键 = 输入（prepass 后路径 + 原素材 stat）+ 裁剪 + 滤镜 + 目标规格：
    参数卡片回改（replan）只重编码被改的片段，重试/未变片段全部秒级复用。
    """
    import hashlib
    orig = clip.get("source") or ""
    try:
        st = os.stat(os.path.join(project_root, orig))
        stat_part = f"{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        stat_part = "0|0"
    key = "|".join([
        src, stat_part,
        str(clip.get("trim_start") or 0), str(clip.get("trim_end")),
        str(clip.get("duration")), vf, af, f"{W}x{H}@{fps}", str(d),
    ])
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"TMP/norm/{h}.mp4"


def _normalize_commands(plan: dict, math: dict,
                        project_root: str) -> tuple[list[Command], dict, dict]:
    """每个 clip 一条归一化命令（去重；产物内容寻址缓存命中则跳过）。

    返回 (命令列表, sidecars, {cid: 归一化产物路径})——路径供渲染段引用。
    """
    W, H, fps = math["width"], math["height"], math["fps"]
    commands: list[Command] = []
    sidecars: dict[str, str] = {}
    seen: set[str] = set()
    norm_paths: dict[str, str] = {}          # cid -> 归一化产物路径（concat 用）
    for c in plan["clips"]:
        cid = c["id"]
        if cid in seen:
            continue
        seen.add(cid)
        src = c["source"]
        effects = c.get("effects") or []
        # PREPASS 类技能（face_mosaic 等）：先插入前置命令，归一化输入改为其中间产物
        prepass_cmds, src = _prepass_commands(cid, src, effects)
        commands.extend(prepass_cmds)
        filter_effects = [e for e in effects if is_vf(e.get("name"))]
        kind = c["kind"]
        vf = _effects_to_vf(filter_effects)
        vf += ("," if vf else "") + (
            f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,fps={fps},format=yuv420p,setsar=1"
        )
        # 音频效果（如 speed 的 atempo）：收集 abuild 拼一条 -af。
        # 音频在归一化阶段变速，下游 concat/xfade 用的都是已变速的 norm 文件。
        af_parts = []
        for e in effects:
            s = get_skill(e.get("name")) if isinstance(e, dict) else None
            if s and s.abuild:
                af_parts.append(s.abuild(e.get("args") or {}))
        af = ",".join(af_parts)
        argv = ["ffmpeg", "-y"]
        if kind == "image":
            # 图片素材：loop 定长展示 + 静音音轨对齐时长
            d = c["duration"]
            argv += ["-loop", "1", "-t", str(d), "-i", src,
                     "-f", "lavfi", "-t", str(d), "-i", "anullsrc=r=48000:cl=stereo"]
            amap = "1:a"
        else:
            # 视频素材：按 trim 点裁剪；探测确认无声时补静音轨
            d = math["durations"][cid]
            ts = c.get("trim_start") or 0
            te = c.get("trim_end")
            if ts:
                argv += ["-ss", str(ts)]
            probe = c.get("probe") or {}
            no_audio = probe.get("has_audio") is False
            argv += ["-i", src]
            if no_audio:
                argv += ["-f", "lavfi", "-t", str(d), "-i", "anullsrc=r=48000:cl=stereo"]
                amap = "1:a"
            else:
                amap = "0:a:0?"
        argv += ["-vf", vf]
        if af:
            argv += ["-af", af]
        argv += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                 "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
                 "-t", str(d),
                 "-map", "0:v:0", "-map", amap]
        out = _norm_cache_path(project_root, c, src, vf, af, W, H, fps, d)
        norm_paths[cid] = out
        # 内容寻址缓存命中：同参数片段（replan 未改/重试）直接复用，不重编码
        if os.path.isfile(os.path.join(project_root, out)) and \
                os.path.getsize(os.path.join(project_root, out)) > 0:
            continue
        argv += [out]
        commands.append(Command("normalize", f"归一化 {cid} ({src})", argv))

    # concat list（快速路径需要）；concat 文件在 TMP/ 下，行内路径相对 TMP/
    list_lines = [f"file '{norm_paths[c].replace('TMP/', '', 1)}'"
                  for c in math["order"]]
    sidecars["TMP/concat_list.txt"] = "\n".join(list_lines) + "\n"
    return commands, sidecars, norm_paths


def _has_transition(plan: dict) -> bool:
    """时间轴上是否存在任何转场（决定走快速路径还是 xfade 路径）。"""
    return any(item.get("transition") for item in plan["timeline"])


def _overlay_abs(math: dict, ov: dict) -> tuple[float, float]:
    """计算 overlay 的 (start, end) 绝对时间：所属片段起点 S_i + 片段内偏移。"""
    start = _round2(math["starts"][ov["at_clip"]] + ov["start_offset"])
    return start, _round2(start + ov["duration"])


# --------------------------------------------------------------------------- #
# 音频：BGM 混音（v2.2）
# --------------------------------------------------------------------------- #

def _bgm_input_args(audio: dict) -> list[str]:
    """BGM 的输入参数。默认循环：短音乐自动铺满整条时间轴。"""
    args: list[str] = []
    if audio.get("loop", True):
        args += ["-stream_loop", "-1"]
    args += ["-i", audio["source"]]
    return args


def _mix_bgm(filters: list[str], voice: str, audio: dict,
             bgm_idx: int, D: float) -> str:
    """把 BGM 混到人声轨上，返回混音后的标签。

    voice  —— 已对齐到 D 的原始音频标签，如 "[0:a]" / "[acat]"
    bgm_idx—— BGM 在 -i 输入里的下标

    处理顺序：裁到 D → 音量 → 淡入淡出 → (可选)侧链闪避 → amix。
    """
    vol = audio.get("volume", 0.3)
    fade_in = audio.get("fade_in") or 0
    fade_out = audio.get("fade_out") or 0

    chain = [f"atrim=0:{D}", "asetpts=PTS-STARTPTS"]
    if vol != 1:
        chain.append(f"volume={vol}")
    if fade_in:
        chain.append(f"afade=t=in:st=0:d={fade_in}")
    if fade_out:
        st = _round2(max(0.0, D - fade_out))
        chain.append(f"afade=t=out:st={st}:d={fade_out}")
    filters.append(f"[{bgm_idx}:a]" + ",".join(chain) + "[bg]")

    if audio.get("ducking"):
        # 侧链压缩：用原始人声当触发信号，讲话时自动压低 BGM
        filters.append(f"{voice}asplit=2[avo][asc]")
        filters.append(
            "[bg][asc]sidechaincompress="
            "threshold=0.05:ratio=8:attack=20:release=300[bgd]"
        )
        filters.append("[avo][bgd]amix=inputs=2:duration=first:normalize=0[aout]")
    else:
        filters.append(f"{voice}[bg]amix=inputs=2:duration=first:normalize=0[aout]")
    return "[aout]"


def _audio_segments(plan: dict, math: dict) -> tuple[list[str], list[list[str]], list[str]]:
    """按"是否有转场"把时间轴切成音频段，与视频段结构完全一致。

    返回 (滤镜片段, 分段, 段末标签)；段内用 acrossfade、段间用 concat——
    这样音频与视频的 xfade 缩短量一致，**顺带修掉 v2.1 的音频硬切与音画漂移**。
    """
    order = math["order"]
    segments: list[list[str]] = []
    cur = [order[0]]
    for i in range(1, len(order)):
        if math["transitions"].get(i):
            cur.append(order[i])
        else:
            segments.append(cur)
            cur = [order[i]]
    segments.append(cur)

    filters: list[str] = []
    labels: list[str] = []
    for seg in segments:
        label = f"[{order.index(seg[0])}:a]"
        for cid in seg[1:]:
            idx = order.index(cid)
            t = math["transitions"][idx]
            new = f"[ax{idx}]"
            filters.append(
                f"{label}[{idx}:a]acrossfade=d={t['duration']}:c1=tri:c2=tri{new}"
            )
            label = new
        labels.append(label)
    return filters, segments, labels


def _render_no_transition(plan: dict, math: dict) -> tuple[list[Command], dict]:
    """快速路径：concat -c copy。

    只有「无 overlay 且无 BGM」才真正走单条 copy；
    有 overlay 或 BGM 时先 concat 到中间件，再一条 filter_complex 收尾
    （视频叠 overlay / 音频混 BGM 可同一条命令完成）。
    """
    W = math["width"]
    out = plan["output"]["filename"]
    overlays = plan.get("overlays") or []
    audio = plan.get("audio")
    sidecars: dict[str, str] = {}
    commands: list[Command] = []

    if not overlays and not audio:
        argv = ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", "TMP/concat_list.txt", "-c", "copy", out]
        commands.append(Command("render", "拼接（快速路径 concat -c copy）", argv))
        return commands, sidecars

    # 有 overlay 或 BGM：concat demuxer 直接当 0 号输入，
    # 一条 filter_complex 同时完成叠图与混音（不落中间文件）
    inputs = ["-f", "concat", "-safe", "0", "-i", "TMP/concat_list.txt"]
    filters: list[str] = []
    cur = "[0:v]"
    pip_idx = 1  # 0 号输入是拼接结果，pip 从 1 开始
    step = 0
    for ov in overlays:
        ts, te = _overlay_abs(math, ov)
        enable = f"between(t,{ts},{te})"
        step += 1
        label = f"[v{step}]"
        if ov["type"] == "pip":
            if ov["kind"] == "image":
                inputs += ["-loop", "1", "-t", str(ov["duration"]), "-i", ov["source"]]
            else:
                inputs += ["-t", str(ov["duration"]), "-i", ov["source"]]
            idx = pip_idx
            pip_idx += 1
            sw = int(W * ov.get("scale", 0.25))
            x = _overlay_pos_expr(ov.get("position", "bottom-right"), "x")
            y = _overlay_pos_expr(ov.get("position", "bottom-right"), "y")
            filters.append(
                f"[{idx}:v]scale={sw}:-2[pip{step}];"
                f"{cur}[pip{step}]overlay={x}:{y}:enable='{enable}'{label}"
            )
        else:  # text
            txt_path = f"TMP/txt_{step}.txt"
            sidecars[txt_path] = ov["text"]
            x, y = _drawtext_pos(ov.get("position", "center"))
            fs = ov.get("font_size", 48)
            color = ov.get("color", "#FFFFFF")
            font = f"fontfile='{FONT_PATH_FILTER}':" if os.path.isfile(FONT_PATH) else ""
            filters.append(
                f"{cur}drawtext={font}textfile='{txt_path}':fontsize={fs}:"
                f"fontcolor={color}:x={x}:y={y}:enable='{enable}'{label}"
            )
        cur = label

    # 视频：有 overlay 走滤镜重编码，否则原样 copy
    if overlays:
        filters.append(f"{cur}copy[vout]")
        vmap, vcodec = "[vout]", ["-c:v", "libx264", "-crf", "20"]
    else:
        vmap, vcodec = "0:v", ["-c:v", "copy"]

    # 音频：有 BGM 则混音（acodec 重编），否则原样 copy
    if audio:
        bgm_idx = pip_idx  # pip 之后的第一个输入
        inputs += _bgm_input_args(audio)
        amap = _mix_bgm(filters, "[0:a]", audio, bgm_idx, math["D"])
        acodec = ["-c:a", "aac"]
    else:
        amap, acodec = "0:a", ["-c:a", "copy"]

    argv = (["ffmpeg", "-y"] + inputs
            + ["-filter_complex", ";".join(filters), "-map", vmap, "-map", amap]
            + vcodec + acodec + [out])
    desc = "叠加渲染（overlay/drawtext）" if overlays else "音频混音（BGM）"
    commands.append(Command("render", desc, argv))
    return commands, sidecars


def _render_with_transition(plan: dict, math: dict) -> tuple[list[Command], dict]:
    """转场路径：链式 xfade + overlay/drawtext + 音频 concat，一次 filter_complex。"""
    W = math["width"]
    out = plan["output"]["filename"]
    order = math["order"]
    sidecars: dict[str, str] = {}

    inputs: list[str] = []
    for cid in order:
        inputs += ["-i", math["norm_paths"][cid]]
    for ov in plan.get("overlays") or []:
        if ov["type"] == "pip":
            if ov["kind"] == "image":
                inputs += ["-loop", "1", "-t", str(ov["duration"]), "-i", ov["source"]]
            else:
                inputs += ["-t", str(ov["duration"]), "-i", ov["source"]]

    filters: list[str] = []
    n_clips = len(order)

    # 1) 视频：按"是否有转场"分段，段内 xfade 链，段间 concat
    segments: list[list[str]] = []
    cur_seg = [order[0]]
    for i in range(1, n_clips):
        if math["transitions"].get(i):
            cur_seg.append(order[i])
        else:
            segments.append(cur_seg)
            cur_seg = [order[i]]
    segments.append(cur_seg)

    seg_labels: list[str] = []
    for seg in segments:
        seg_start = math["starts"][seg[0]]
        label = f"[{order.index(seg[0])}:v]"
        for j in range(1, len(seg)):
            cid = seg[j]
            idx = order.index(cid)
            t = math["transitions"][idx]
            # xfade offset 是"段内相对起点"：全局 S 减去段首 S
            offset = _round2(math["starts"][cid] - seg_start)
            new_label = f"[x{idx}]"
            filters.append(
                f"{label}[{idx}:v]xfade=transition={t['type']}:"
                f"duration={t['duration']}:offset={offset}{new_label}"
            )
            label = new_label
        seg_labels.append(label)

    if len(seg_labels) == 1:
        vmain = seg_labels[0]
    else:
        vmain = "[vcat]"
        filters.append(f"{''.join(seg_labels)}concat=n={len(seg_labels)}:v=1:a=0{vmain}")

    # 2) overlay/drawtext 应用到 vmain
    cur = vmain
    pip_i = n_clips
    step = 0
    for ov in plan.get("overlays") or []:
        ts, te = _overlay_abs(math, ov)
        enable = f"between(t,{ts},{te})"
        label = f"[vo{step}]"
        if ov["type"] == "pip":
            sw = int(W * ov.get("scale", 0.25))
            x = _overlay_pos_expr(ov.get("position", "bottom-right"), "x")
            y = _overlay_pos_expr(ov.get("position", "bottom-right"), "y")
            filters.append(
                f"[{pip_i}:v]scale={sw}:-2[pip{step}];"
                f"{cur}[pip{step}]overlay={x}:{y}:enable='{enable}'{label}"
            )
            pip_i += 1
        else:  # text
            txt_path = f"TMP/txt_{step}.txt"
            sidecars[txt_path] = ov["text"]
            x, y = _drawtext_pos(ov.get("position", "center"))
            fs = ov.get("font_size", 48)
            color = ov.get("color", "#FFFFFF")
            font = f"fontfile='{FONT_PATH_FILTER}':" if os.path.isfile(FONT_PATH) else ""
            filters.append(
                f"{cur}drawtext={font}textfile='{txt_path}':fontsize={fs}:"
                f"fontcolor={color}:x={x}:y={y}:enable='{enable}'{label}"
            )
        cur = label
        step += 1
    vout = "[vout]"
    filters.append(f"{cur}copy{vout}")

    # 3) 音频：与视频同构分段（段内 acrossfade、段间 concat）
    #    这样音频与视频被 xfade 缩短的量一致 —— 修掉 v2.1 的音频硬切与音画漂移
    a_filters, _segs, a_labels = _audio_segments(plan, math)
    filters.extend(a_filters)
    if len(a_labels) == 1:
        a_cur = a_labels[0]
    else:
        filters.append(f"{''.join(a_labels)}concat=n={len(a_labels)}:v=0:a=1[acat]")
        a_cur = "[acat]"
    filters.append(f"{a_cur}atrim=0:{math['D']},asetpts=PTS-STARTPTS[avoice]")

    audio = plan.get("audio")
    if audio:
        inputs += _bgm_input_args(audio)
        amap = _mix_bgm(filters, "[avoice]", audio, pip_i, math["D"])
    else:
        amap = "[avoice]"

    argv = ["ffmpeg", "-y"] + inputs + ["-filter_complex", ";".join(filters),
                     "-map", vout, "-map", amap,
                     "-c:v", "libx264", "-crf", "20", "-c:a", "aac", out]
    commands = [Command("render", "转场渲染（xfade 链式 + 叠加 + 音频交错淡化）", argv)]
    return commands, sidecars


# --------------------------------------------------------------------------- #
# V5 粗剪：编译期计划展开（select 宏 / cut_silence / cut_black）
#
# 全部发生在校验之后、_derive 之前——展开产物是一份普通计划，时间轴数学、
# dry-run 预检、执行、回验对它一视同仁，不需要任何特判。
# --------------------------------------------------------------------------- #

def _expand_select(plan: dict, project_root: str) -> tuple[dict, dict]:
    """select 筛选宏 → 从内容卡片确定性生成 clips/timeline。

    LLM 只写条件（where + budget），「挑哪些镜头」由 shot_select 纯函数完成，
    杜绝模型手抄多区间 trim 的漏抄/错抄。卡片必须已由 analyze_media 建好
    （缓存 sidecar）；缺卡返回可回传 LLM 的错误（模型补 analyze 后重提即可）。
    """
    import content_analysis
    import shot_select

    sel = plan["select"]
    cards: dict[str, dict] = {}
    missing = []
    for src in sel["sources"]:
        card = content_analysis.load_cached_card(project_root, src)
        if card and card.get("shots"):
            cards[src] = card
        else:
            missing.append(src)
    if missing:
        raise CompileError([
            "select.sources 里的素材还没有内容索引：" + "、".join(missing) +
            "。请先对每个素材调用 analyze_media 建立内容卡片，再重新提交计划。"
        ])

    res = shot_select.select_shots(cards, sel)
    if not res["clips"]:
        if res.get("matched"):
            raise CompileError([
                f"select 有 {res['matched']} 个镜头满足条件，但单个最短也有 "
                f"{res.get('min_matched_duration')}s，装不进 "
                f"{sel.get('budget_seconds')}s 预算。请调大 budget_seconds。"
            ])
        raise CompileError([
            "select 没有命中任何镜头（条件：" + json.dumps(sel.get("where") or {},
                                                          ensure_ascii=False) +
            "）。请放宽条件重试；若素材里确实没有匹配内容，请改用 ask_user "
            "向用户如实说明卡片里实际有什么。"
        ])

    new_plan = {k: v for k, v in plan.items()
                if k not in ("select", "clips", "timeline", "overlays")}
    new_plan["clips"] = res["clips"]
    new_plan["timeline"] = res["timeline"]
    rejected = res.get("rejected") or []
    report = {
        "picked": [{"source": c["source"], "start": c["start"], "end": c["end"],
                    "duration": c["duration"], "label": c["label"]}
                   for c in res["picked"]],
        "total_seconds": round(sum(c["duration"] for c in res["picked"]), 3),
        # 被拒镜头（前 50 条进报告，防事件过大）；rejected_total 是全量数
        "rejected": [{"source": r["source"], "start": r["start"], "end": r["end"],
                      "reason": r.get("reason", "")} for r in rejected[:50]],
        "rejected_total": len(rejected),
    }
    return new_plan, report


def _probe_has_audio(project_root: str, src: str, clip: dict) -> bool:
    """clip 是否有音轨：先信计划自报的 probe，没有就现场 ffprobe。"""
    pp = clip.get("probe") or {}
    if "has_audio" in pp:
        return bool(pp["has_audio"])
    full = os.path.join(project_root, src)
    streams = (ffmpeg_exec.probe(full).get("data") or {}).get("streams") or []
    return any(s.get("codec_type") == "audio" for s in streams)


def _clip_trim_range(plan: dict, clip: dict, project_root: str) -> tuple[float, float]:
    """clip 的裁剪范围 [ts, te]；te 缺失时现场 ffprobe 补齐。"""
    ts = clip.get("trim_start") or 0
    te = clip.get("trim_end")
    if te is None:
        pp = clip.get("probe") or {}
        dur = pp.get("duration")
        if dur is None:
            full = os.path.join(project_root, clip["source"])
            data = ffmpeg_exec.probe(full).get("data") or {}
            raw = (data.get("format") or {}).get("duration")
            try:
                dur = float(raw)
            except (TypeError, ValueError):
                dur = None
        if dur is None:
            raise CompileError([f"clip {clip.get('id')} 无法确定素材时长，"
                                f"无法展开剪除区间。"])
        te = dur
    return float(ts), float(te)


def _expand_cuts(plan: dict, project_root: str, run_fn=None) -> tuple[dict, list[dict]]:
    """cut_silence / cut_black：一个 clip 展开为 N 个保留子段。

    - 检测在编译期实时跑（确定性、不依赖缓存卡），范围锚定 [trim_start, trim_end]
    - 保留区间 = 废段补集 + keep_padding 收缩 + min_keep 过滤（signal_detection）
    - 子段继承原 clip 的 effects/probe；子段之间是硬切；原 timeline 条目的
      转场落在首个子段条目；overlay 按「源时间包含关系」重新指到子段
    """
    clips = plan.get("clips") or []
    if not any(c.get("cut_silence") is not None or c.get("cut_black") is not None
               for c in clips):
        return plan, []

    run = run_fn or ffmpeg_exec.run
    new_clips: list[dict] = []
    id_map: dict[str, list[str]] = {}      # 原 id -> 子段 id 列表（保持顺序）
    reports: list[dict] = []

    for clip in clips:
        cut_sil = clip.get("cut_silence")
        cut_blk = clip.get("cut_black")
        if cut_sil is None and cut_blk is None:
            new_clips.append(clip)
            continue
        cid = clip["id"]
        src = clip["source"]
        ts, te = _clip_trim_range(plan, clip, project_root)
        windows: list = []

        if cut_sil is not None:
            if not _probe_has_audio(project_root, src, clip):
                raise CompileError([
                    f"clip {cid}（{src}）没有音轨，cut_silence 无法检测静音；"
                    f"请去掉该参数，或换有声音的素材。"
                ])
            res = signal_detection.detect_silences(
                src, offset=ts, limit=te - ts,
                noise_db=float(cut_sil.get("noise_db", signal_detection.DEFAULT_NOISE_DB)),
                min_silence=float(cut_sil.get("min_silence", signal_detection.DEFAULT_MIN_SILENCE)),
                run_fn=run, cwd=project_root)
            if not res.get("ok"):
                raise CompileError([f"clip {cid}（{src}）静音检测失败：{res.get('error')}"])
            windows += res["intervals"]

        if cut_blk is not None:
            res = signal_detection.detect_blacks(
                src, offset=ts, limit=te - ts,
                min_duration=float(cut_blk.get("min_duration", signal_detection.DEFAULT_BLACK_MIN_DUR)),
                run_fn=run, cwd=project_root)
            if not res.get("ok"):
                raise CompileError([f"clip {cid}（{src}）黑场检测失败：{res.get('error')}"])
            windows += res["intervals"]

        cut_opts = cut_sil if cut_sil is not None else cut_blk
        pad = float(cut_opts.get("keep_padding", signal_detection.DEFAULT_KEEP_PADDING))
        min_keep = float(cut_opts.get("min_keep", signal_detection.DEFAULT_MIN_KEEP))
        kept = signal_detection.kept_intervals(windows, ts, te, pad=pad, min_keep=min_keep)
        if not kept:
            raise CompileError([
                f"clip {cid}（{src}）在 [{ts:.2f}, {te:.2f}] 内剪除静音/黑场后没有"
                f"剩余内容——该素材的音轨很可能整体接近无声（可用 analyze_media 看"
                f"卡片 signals.audio.silence_ratio 确认，接近 1.0 即整条无声）。"
                f"这不是参数问题，调阈值救不了：请改用 ask_user 向用户如实说明"
                f"「素材音轨本身没有有效声音」，建议保留原样、只剪画面或换素材，"
                f"不要再用不同参数重试 cut_silence。"
            ])

        sub_ids: list[str] = []
        keep_effects = clip.get("effects")
        for k, (ks, ke) in enumerate(kept):
            sub = {
                "id": f"{cid}__k{k}",
                "source": src,
                "kind": "video",
                "trim_start": ks,
                "trim_end": ke,
            }
            if "probe" in clip:
                sub["probe"] = clip["probe"]
            if keep_effects:
                sub["effects"] = keep_effects
            new_clips.append(sub)
            sub_ids.append(sub["id"])
        id_map[cid] = sub_ids

        removed = round(sum(e - s for s, e in signal_detection.merge_intervals(windows, ts, te)), 3)
        reports.append({
            "clip": cid, "source": src,
            "original_seconds": round(te - ts, 3),
            "kept_segments": len(kept),
            "removed_seconds": removed,
            "removed_segments": len(signal_detection.merge_intervals(windows, ts, te)),
            "kept_ranges": kept,
        })

    # timeline 重映射：首个子段条目继承原条目的转场，其余为硬切
    new_timeline: list[dict] = []
    for item in plan.get("timeline") or []:
        cid = item.get("clip")
        subs = id_map.get(cid)
        if not subs:
            new_timeline.append(item)
            continue
        for k, sub_id in enumerate(subs):
            entry = {"clip": sub_id}
            if k == 0 and item.get("transition"):
                entry["transition"] = item["transition"]
            new_timeline.append(entry)

    # overlay 重映射：按源时间落点指到包含它的子段，offset 换算为子段内相对时间
    sub_clip_by_id = {c["id"]: c for c in new_clips}
    for ov in plan.get("overlays") or []:
        subs = id_map.get(ov.get("at_clip"))
        if not subs:
            continue
        base = next(c for c in plan["clips"] if c["id"] == ov["at_clip"])
        src_time = (base.get("trim_start") or 0) + ov.get("start_offset", 0)
        target = None
        for sub_id in subs:
            sub = sub_clip_by_id[sub_id]
            if sub["trim_start"] <= src_time < sub["trim_end"]:
                target = sub
                break
        if target is None:
            # 落点在被剪除的废段里：就近挂到下一个子段（没有就挂最后一个）
            for sub_id in subs:
                if sub_clip_by_id[sub_id]["trim_start"] > src_time:
                    target = sub_clip_by_id[sub_id]
                    src_time = target["trim_start"]
                    break
            if target is None:
                target = sub_clip_by_id[subs[-1]]
                src_time = target["trim_end"] - 0.01
        ov["at_clip"] = target["id"]
        ov["start_offset"] = round(max(0.0, src_time - target["trim_start"]), 3)

    new_plan = dict(plan)
    new_plan["clips"] = new_clips
    new_plan["timeline"] = new_timeline

    # 展开可能产生比转场还短的子段——xfade 会渲染失败，提前拦下（可回传重试）
    _check_transition_fit(new_plan)
    return new_plan, reports


def _check_transition_fit(plan: dict) -> None:
    """转场时长必须小于相邻片段（变速后）长度；违反抛 CompileError。"""
    by_id = {c.get("id"): c for c in plan.get("clips") or []}
    for i in range(1, len(plan.get("timeline") or [])):
        item = plan["timeline"][i]
        tr = item.get("transition")
        if not (isinstance(tr, dict) and tr.get("duration")):
            continue
        for side, which in ((plan["timeline"][i - 1].get("clip"), "前一片段"),
                            (item.get("clip"), "当前片段")):
            c = by_id.get(side)
            if not c:
                continue
            d = clip_duration(c)
            if d is None:
                continue
            d = round(d * time_scale_of(c.get("effects") or []), 3)
            if tr["duration"] >= d:
                raise CompileError([
                    f"timeline[{i}] 转场时长 {tr['duration']}s 不小于片段 {side}"
                    f"（剪除废段/变速后仅 {d}s）。请缩短转场时长，或调大 "
                    f"min_keep 保留更长的片段。"
                ])


def _gather_ctx(project_root: str) -> dict:
    """收集全部 INPUT/ 素材的 {卡片, 探针}（workflow 宏的展开上下文）。"""
    import content_analysis
    cards, probes = {}, {}
    input_dir = os.path.join(project_root, "INPUT")
    if not os.path.isdir(input_dir):
        return {"cards": cards, "probes": probes, "project_root": project_root}
    for name in sorted(os.listdir(input_dir)):
        if name.startswith(".") or not os.path.isfile(os.path.join(input_dir, name)):
            continue
        rel = "INPUT/" + name
        ext = os.path.splitext(name)[1].lower()
        if ext in (".mp3", ".wav", ".m4a", ".flac", ".ogg"):
            continue                     # 纯音频不进视频素材池
        card = content_analysis.load_cached_card(project_root, rel)
        if card:
            cards[rel] = card
        data = (ffmpeg_exec.probe(os.path.join(project_root, rel)).get("data") or {})
        streams = data.get("streams") or []
        raw_dur = (data.get("format") or {}).get("duration")
        try:
            dur = round(float(raw_dur), 3)
        except (TypeError, ValueError):
            dur = None
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        probes[rel] = {"duration": dur,
                       "has_audio": audio is not None,
                       "has_video": video is not None}
    return {"cards": cards, "probes": probes, "project_root": project_root}


def _expand_workflow(plan: dict, project_root: str) -> tuple[dict, dict]:
    """V7 一键成片：workflow 宏 → 确定性展开成普通计划（clips/timeline）。

    展开产物仍可含 select/cut_silence（如 speech_clean 产出 cut 字段），
    由后续 _expand_select/_expand_cuts 继续处理——宏之间可组合。
    """
    import workflow as wf_mod

    wf = plan["workflow"]
    name = wf.get("name")
    entry = wf_mod.WORKFLOWS.get(name)
    if entry is None:
        raise CompileError([f"workflow.name 必须是 {wf_mod.workflow_menu()} 之一。"])
    ctx = _gather_ctx(project_root)
    try:
        sub_plan, report = entry.expand(wf, ctx)
    except ValueError as exc:            # workflow 用 ValueError 表达可回传错误
        raise CompileError([str(exc)])
    new_plan = {k: v for k, v in plan.items()
                if k not in ("workflow", "clips", "timeline", "overlays")}
    new_plan.update(sub_plan)
    return new_plan, {"name": name, **report}


def _expand_plan(plan: dict, project_root: str, run_fn=None) -> tuple[dict, dict]:
    """V5/V7 计划展开总入口：workflow 宏 → select 宏；cut 参数 → 保留子段。"""
    expansions: dict = {}
    if plan.get("workflow"):
        plan, report = _expand_workflow(plan, project_root)
        expansions["workflow"] = report
    if plan.get("select"):
        plan, report = _expand_select(plan, project_root)
        expansions["select"] = report
    plan, cuts = _expand_cuts(plan, project_root, run_fn=run_fn)
    if cuts:
        expansions["cuts"] = cuts
    return plan, expansions


def compile_plan(plan: dict, project_root: str) -> CompileResult:
    """校验 → 展开（V5）→ 推导 → 生成命令。失败抛 CompileError(errors)。"""
    errors = validate_plan(plan, project_root)
    if errors:
        raise CompileError(errors)

    plan, expansions = _expand_plan(plan, project_root)
    math = _derive(plan)
    norm_cmds, norm_sidecars, norm_paths = _normalize_commands(plan, math, project_root)
    math["norm_paths"] = norm_paths      # 渲染段按内容寻址路径引用归一化产物

    if _has_transition(plan):
        render_cmds, render_sidecars = _render_with_transition(plan, math)
    else:
        render_cmds, render_sidecars = _render_no_transition(plan, math)

    sidecars = {**norm_sidecars, **render_sidecars}
    return CompileResult(
        plan=plan,
        commands=norm_cmds + render_cmds,
        sidecars=sidecars,
        math=math,
        expansions=expansions,
    )


# --------------------------------------------------------------------------- #
# dry-run 语法预检（T5）：编译完成 → 执行之前
#
# 静态校验（plan_schema）只保证「效果名合法、参数在范围内」，不保证编译器拼出的
# filtergraph 能被 ffmpeg 解析——这类错误原本要到渲染中途才以 stderr 炸出，慢且
# 不可回传。预检把每条带滤镜的命令先在 0.5s 合成素材上跑一遍，秒级暴露语法错误。
# --------------------------------------------------------------------------- #

PRECHECK_DUR = 0.5      # dry-run 只处理 0.5 秒合成素材
PRECHECK_TIMEOUT = 60   # 单条预检超时（真实渲染可几分钟，预检必须秒级收场）
_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac"}


def _ensure_precheck_refs(project_root: str, W: int, H: int) -> tuple[str, str] | None:
    """生成/复用 dry-run 用的合成参考素材（相对项目根的路径）。

    vref —— 输出分辨率的黑帧 + 静音轨。render 阶段所有 TMP/*_norm.mp4 都是 WxH 且
            必有音轨（归一化对无声素材补过 anullsrc），用 vref 顶替能保持
            filter_complex 里 [i:v]/[i:a] 引用的流布局不变。
    aref —— 纯静音音频，顶替 BGM 等纯音频输入。
    """
    vref = f"TMP/_precheck_{W}x{H}.mp4"
    aref = "TMP/_precheck_audio.m4a"
    if not os.path.isfile(os.path.join(project_root, vref)):
        res = ffmpeg_exec.run(
            ["ffmpeg", "-y", "-hide_banner", "-v", "error",
             "-f", "lavfi", "-i", f"color=black:s={W}x{H}:r=25:d={PRECHECK_DUR}",
             "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={PRECHECK_DUR}",
             "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
             "-shortest", vref],
            cwd=project_root)
        if not res["ok"]:
            return None
    if not os.path.isfile(os.path.join(project_root, aref)):
        res = ffmpeg_exec.run(
            ["ffmpeg", "-y", "-hide_banner", "-v", "error",
             "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={PRECHECK_DUR}",
             "-c:a", "aac", aref],
            cwd=project_root)
        if not res["ok"]:
            return None
    return vref, aref


def _src_dims(plan: dict, src: str) -> tuple[int, int]:
    """源素材分辨率：先按 source 匹配 clip；PREPASS 替换过的 TMP/<cid>_*.mp4 按 cid 回查。

    归一化输入可能是 face_mosaic 等前置命令的中间产物，文件名形如 TMP/c1_masked.mp4，
    而 delogo 等效果的坐标是按【原始素材】像素校验的，所以尺寸必须追回原 clip 的 probe。
    """
    probe: dict = {}
    for c in plan.get("clips") or []:
        if c.get("source") == src:
            probe = c.get("probe") or {}
            break
    else:
        cid = os.path.basename(src).split("_")[0]
        for c in plan.get("clips") or []:
            if c.get("id") == cid:
                probe = c.get("probe") or {}
                break
    return int(probe.get("width") or 1280), int(probe.get("height") or 720)


def _dry_argv_normalize(argv: list[str], plan: dict) -> list[str]:
    """归一化命令的 dry-run：合成黑帧替换输入，重建最小等价命令。

    归一化的 -vf 链先跑效果（原始像素坐标，如 delogo 的 region）再 scale/pad，
    所以黑帧尺寸必须取【源素材探测分辨率】——用输出分辨率会让大坐标的 delogo
    被假越界误报。取不到 probe 时兜底 1280x720。
    """
    vf = argv[argv.index("-vf") + 1]
    src = argv[argv.index("-i") + 1]
    w, h = _src_dims(plan, src)
    out = ["ffmpeg", "-y", "-hide_banner", "-v", "error",
           "-f", "lavfi", "-i", f"color=black:s={w}x{h}:r=25:d={PRECHECK_DUR}",
           "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={PRECHECK_DUR}",
           "-vf", vf]
    # 音频链（如 speed 的 atempo）一并预检：不带上则 atempo 语法不受检
    if "-af" in argv:
        out += ["-af", argv[argv.index("-af") + 1]]
    out += ["-t", str(PRECHECK_DUR), "-f", "null", "-"]
    return out


def _dry_argv_render(argv: list[str], vref: str, aref: str) -> list[str]:
    """渲染命令（filter_complex）的 dry-run：外科手术式替换，索引与流布局不动。

      - 真实文件输入 → 合成参考（纯音频扩展名 → aref，其余 → vref；
        图片画中画输入统一 vref，graph 里只会引用它的 [i:v]）
      - concat demuxer（-f concat -safe 0）→ 撤掉封装标记当普通视频输入
      - -ss / -stream_loop / -loop 删除（对 0.5s 参考素材无意义甚至死循环）
      - 所有 -t 截到 PRECHECK_DUR
      - 末尾输出路径 → -f null -
    """
    out: list[str] = []
    i, n = 0, len(argv)
    while i < n:
        tok = argv[i]
        if tok in ("-ss", "-stream_loop", "-loop", "-safe"):
            i += 2
            continue
        if tok == "-t":
            out += ["-t", str(PRECHECK_DUR)]
            i += 2
            continue
        if tok == "-i":
            path = argv[i + 1]
            if out[-2:] == ["-f", "concat"]:
                del out[-2:]
            ext = os.path.splitext(path)[1].lower()
            out += ["-i", aref if ext in _AUDIO_EXTS else vref]
            i += 2
            continue
        out.append(tok)
        i += 1
    # 渲染命令最后一个参数必为输出路径；换成 null 不落盘
    if out and not out[-1].startswith("-"):
        out[-1:] = ["-f", "null", "-"]
    return out


def precheck(result: CompileResult, project_root: str) -> CompileResult:
    """T5 dry-run 语法预检：所有带滤镜的 ffmpeg 命令先在合成素材上试跑。

    结果记录在 result.prechecks（Web/CLI 可展示）；失败抛 CompileError，
    错误中文、带 ffmpeg stderr 尾巴，可直接进回传重试通道。
    跳过项（记 note，不算失败）：非 ffmpeg 命令（detect 阶段 Python 子进程）、
    无 -vf/-filter_complex 的命令（concat -c copy 快速路径没有滤镜图可检）。
    """
    if not ffmpeg_exec.ffmpeg_available():
        result.prechecks.append({"ok": True, "note": "ffmpeg 未安装，跳过 dry-run 预检。"})
        return result

    W = int(result.math.get("width") or 1280)
    H = int(result.math.get("height") or 720)
    refs = _ensure_precheck_refs(project_root, W, H)
    if refs is None:
        result.prechecks.append({"ok": True, "note": "合成参考素材生成失败，跳过预检。"})
        return result
    vref, aref = refs

    errors: list[str] = []
    for c in result.commands:
        if not c.argv or c.argv[0] != "ffmpeg":
            result.prechecks.append({"ok": True, "stage": c.stage,
                                     "description": c.description,
                                     "note": "非 ffmpeg 命令，预检跳过"})
            continue
        if "-filter_complex" not in c.argv and "-vf" not in c.argv:
            result.prechecks.append({"ok": True, "stage": c.stage,
                                     "description": c.description,
                                     "note": "无滤镜图（流复制），预检跳过"})
            continue
        if "-filter_complex" in c.argv:
            dry = _dry_argv_render(c.argv, vref, aref)
        else:
            dry = _dry_argv_normalize(c.argv, result.plan)
        res = ffmpeg_exec.run(dry, cwd=project_root, timeout=PRECHECK_TIMEOUT)
        rec = {"ok": bool(res["ok"]), "stage": c.stage, "description": c.description}
        if not res["ok"]:
            lines = (res.get("stderr") or res.get("error") or "").strip().splitlines()
            # 优先抓实质错误行（如 "No such filter: 'xxx'"）——它通常在 stderr 前部，
            # 尾部几行往往只是 "Error opening output files" 这类泛化收尾
            key = [l for l in lines if any(
                k in l.lower() for k in ("no such filter", "invalid", "error",
                                         "unable", "failed", "cannot", "找"))]
            tail = "\n".join((key[:3] or lines[-3:]))[-400:] if lines else "(无 stderr)"
            rec["stderr_tail"] = tail
            errors.append(
                f"[{c.stage}] {c.description}：dry-run 语法预检失败（命令未执行）。\n"
                f"  ffmpeg 报错：{tail}"
            )
        result.prechecks.append(rec)

    if errors:
        raise CompileError(errors)
    return result


def execute(result: CompileResult, project_root: str, should_stop=None) -> CompileResult:
    """④ 执行：写 sidecar 文件 → dry-run 预检 → 运行命令（cwd 锚定项目根）。

    detect/normalize 阶段的命令相互独立，**2 路并行**（V7 性能优化：
    多片段任务不再串行逐条编码）；render 阶段依赖前序产物，保持串行。
    should_stop() 返回 True 时在命令边界抛 Cancelled（Web 取消按钮用；
    ffmpeg 进程本身由 ffmpeg_exec.kill_all() 硬终止，这里是软检查点）。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    os.makedirs(os.path.join(project_root, "TMP", "norm"), exist_ok=True)
    for path, content in result.sidecars.items():
        with open(os.path.join(project_root, path), "w", encoding="utf-8") as f:
            f.write(content)
    # drawtext 的 textfile 是 sidecar，所以预检必须在写完 sidecar 之后
    precheck(result, project_root)  # 失败抛 CompileError，命令一条都不会真跑

    def run_one(c: Command) -> dict:
        if should_stop is not None and should_stop():
            raise Cancelled(f"已取消：未执行「{c.description}」")
        return {"description": c.description, **ffmpeg_exec.run(c.argv, cwd=project_root)}

    parallel = [c for c in result.commands if c.stage in ("detect", "normalize")]
    renders = [c for c in result.commands if c.stage not in ("detect", "normalize")]

    done: dict[int, dict] = {}
    if parallel:
        with ThreadPoolExecutor(max_workers=min(2, len(parallel))) as ex:
            futures = {ex.submit(run_one, c): i for i, c in enumerate(parallel)}
            cancelled: Cancelled | None = None
            for fut in as_completed(futures):
                i = futures[fut]
                try:
                    done[i] = fut.result()
                except Cancelled as exc:
                    cancelled = cancelled or exc
            if cancelled is not None:
                # 已完成的照常入账，再统一抛取消
                result.executes = [done[i] for i in sorted(done)]
                raise cancelled
    for i, c in enumerate(renders):
        done[len(parallel) + i] = run_one(c)
    result.executes = [done[i] for i in sorted(done)]
    return result


def verify(result: CompileResult, project_root: str) -> CompileResult:
    """⑤ 回验：ffprobe 检查输出时长 ≈ D。"""
    out = os.path.join(project_root, result.plan["output"]["filename"])
    if not ffmpeg_exec.ffprobe_available():
        result.verify = {"ok": False, "note": "ffprobe 未安装，跳过回验。"}
        return result
    if not os.path.isfile(out):
        result.verify = {"ok": False, "note": f"产物不存在：{out}"}
        return result
    res = ffmpeg_exec.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", out]
    )
    try:
        dur = float(json.loads(res["stdout"])["format"]["duration"])
        D = result.math["D"]
        result.verify = {
            "ok": abs(dur - D) <= 0.5,
            "duration": dur, "expected": D,
        }
    except Exception:
        result.verify = {"ok": False, "note": f"ffprobe 解析失败：{(res.get('stderr') or '')[:200]}"}
    return result
