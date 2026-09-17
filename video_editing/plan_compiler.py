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
from plan_schema import clip_duration, validate_plan
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
    """compile_plan 的完整产出，后续 execute/verify 就地填充 executes/verify。"""

    plan: dict
    commands: list[Command] = field(default_factory=list)
    sidecars: dict[str, str] = field(default_factory=dict)   # path -> content
    math: dict = field(default_factory=dict)                 # 推导结果，供展示/调试
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
        d = clip_duration(_clip_by_id(plan, cid))
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

def _normalize_commands(plan: dict, math: dict) -> tuple[list[Command], dict]:
    """每个 clip 一条归一化命令（去重）。"""
    W, H, fps = math["width"], math["height"], math["fps"]
    commands: list[Command] = []
    sidecars: dict[str, str] = {}
    seen: set[str] = set()
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
        out = f"TMP/{cid}_norm.mp4"
        kind = c["kind"]
        vf = _effects_to_vf(filter_effects)
        vf += ("," if vf else "") + (
            f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,fps={fps},format=yuv420p,setsar=1"
        )
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
        argv += ["-vf", vf,
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                 "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
                 "-t", str(d),
                 "-map", "0:v:0", "-map", amap, out]
        commands.append(Command("normalize", f"归一化 {cid} ({src})", argv))

    # concat list（快速路径需要）
    list_lines = [f"file '{c}_norm.mp4'" for c in math["order"]]
    sidecars["TMP/concat_list.txt"] = "\n".join(list_lines) + "\n"
    return commands, sidecars


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
        inputs += ["-i", f"TMP/{cid}_norm.mp4"]
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


def compile_plan(plan: dict, project_root: str) -> CompileResult:
    """校验 → 推导 → 生成命令。失败抛 CompileError(errors)。"""
    errors = validate_plan(plan, project_root)
    if errors:
        raise CompileError(errors)

    math = _derive(plan)
    norm_cmds, norm_sidecars = _normalize_commands(plan, math)

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
    )


def execute(result: CompileResult, project_root: str) -> CompileResult:
    """④ 执行：写 sidecar 文件 + 逐条运行命令（cwd 锚定项目根）。"""
    os.makedirs(os.path.join(project_root, "TMP"), exist_ok=True)
    for path, content in result.sidecars.items():
        with open(os.path.join(project_root, path), "w", encoding="utf-8") as f:
            f.write(content)
    for c in result.commands:
        res = ffmpeg_exec.run(c.argv, cwd=project_root)
        result.executes.append({"description": c.description, **res})
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
