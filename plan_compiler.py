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

FONT_PATH = "C:/Windows/Fonts/msyh.ttc"
MARGIN = 20


class CompileError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass
class Command:
    stage: str          # "normalize" | "render"
    description: str
    argv: list[str]

    @property
    def line(self) -> str:
        return " ".join(self.argv)


@dataclass
class CompileResult:
    plan: dict
    commands: list[Command] = field(default_factory=list)
    sidecars: dict[str, str] = field(default_factory=dict)   # path -> content
    math: dict = field(default_factory=dict)                 # 推导结果，供展示/调试
    executes: list[dict] = field(default_factory=list)       # 执行结果
    verify: dict | None = None

    def render_summary(self) -> str:
        lines = []
        for c in self.commands:
            lines.append(f"[{c.stage}] {c.description}\n  {c.line}")
        return "\n".join(lines)


def _effects_to_vf(effects: list) -> str:
    parts = []
    for e in effects:
        name = e["name"]
        args = e.get("args") or {}
        if name == "hflip":
            parts.append("hflip")
        elif name == "vflip":
            parts.append("vflip")
        elif name == "transpose":
            parts.append(f"transpose={args.get('dir', 1)}")
        elif name == "eq":
            kv = {k: v for k, v in args.items()
                  if k in ("brightness", "contrast", "saturation")}
            if kv:
                parts.append("eq=" + ":".join(f"{k}={v}" for k, v in kv.items()))
            else:
                parts.append("eq")
    return ",".join(parts)


def _clip_by_id(plan: dict, cid: str) -> dict:
    return next(c for c in plan["clips"] if c["id"] == cid)


def _overlay_pos_expr(position: str, axis: str) -> str:
    """画中画 overlay 的 x/y 表达式（基于 main/overlay 宽高，margin=20）。"""
    if axis == "x":
        return {
            "top-left": "20", "left": "20", "bottom-left": "20",
            "top": "(main_w-overlay_w)/2", "center": "(main_w-overlay_w)/2",
            "bottom": "(main_w-overlay_w)/2",
            "top-right": "main_w-overlay_w-20", "right": "main_w-overlay_w-20",
            "bottom-right": "main_w-overlay_w-20",
        }[position]
    return {
        "top-left": "20", "top": "20", "top-right": "20",
        "left": "(main_h-overlay_h)/2", "center": "(main_h-overlay_h)/2",
        "right": "(main_h-overlay_h)/2",
        "bottom-left": "main_h-overlay_h-20", "bottom": "main_h-overlay_h-20",
        "bottom-right": "main_h-overlay_h-20",
    }[position]


def _drawtext_pos(position: str) -> tuple[str, str]:
    m = 40
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
        out = f"TMP/{cid}_norm.mp4"
        kind = c["kind"]
        vf = _effects_to_vf(c.get("effects") or [])
        vf += ("," if vf else "") + (
            f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,fps={fps},format=yuv420p,setsar=1"
        )
        argv = ["ffmpeg", "-y"]
        if kind == "image":
            d = c["duration"]
            argv += ["-loop", "1", "-t", str(d), "-i", src,
                     "-f", "lavfi", "-t", str(d), "-i", "anullsrc=r=48000:cl=stereo"]
            amap = "1:a"
        else:
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
    return any(item.get("transition") for item in plan["timeline"])


def _overlay_abs(math: dict, ov: dict) -> tuple[float, float]:
    """根据 math 里预计算的结果，返回 overlay 的 (start, end) 绝对时间。"""
    for a in math["overlays"]:
        if a["type"] == ov["type"] and a["at_clip"] == ov["at_clip"] \
                and abs(a["start"] - (math["starts"][ov["at_clip"]] + ov["start_offset"])) < 1e-6:
            return a["start"], a["end"]
    raise KeyError("overlay 绝对时间未找到")


def _render_no_transition(plan: dict, math: dict) -> tuple[list[Command], dict]:
    """快速路径：concat -c copy（有 overlay 则再加一步 overlay 渲染）。"""
    W = math["width"]
    out = plan["output"]["filename"]
    sidecars: dict[str, str] = {}
    commands: list[Command] = []

    if not plan.get("overlays"):
        argv = ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", "TMP/concat_list.txt", "-c", "copy", out]
        commands.append(Command("render", "拼接（快速路径 concat -c copy）", argv))
        return commands, sidecars

    # 有 overlay：先 concat copy 到中间，再叠加渲染
    mid = "TMP/_concat.mp4"
    commands.append(Command("render", "拼接（concat -c copy）",
                            ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                             "-i", "TMP/concat_list.txt", "-c", "copy", mid]))

    inputs = ["-i", mid]
    filters: list[str] = []
    cur = "[0:v]"
    pip_idx = 1  # 0 号输入是拼接结果，pip 从 1 开始
    step = 0
    for ov in plan["overlays"]:
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
            font = f"fontfile='{FONT_PATH}':" if os.path.isfile(FONT_PATH) else ""
            filters.append(
                f"{cur}drawtext={font}textfile='{txt_path}':fontsize={fs}:"
                f"fontcolor={color}:x={x}:y={y}:enable='{enable}'{label}"
            )
        cur = label
    filters.append(f"{cur}copy[vout]")
    argv = ["ffmpeg", "-y"] + inputs + ["-filter_complex", ";".join(filters),
                     "-map", "[vout]", "-map", "0:a", "-c:v", "libx264", "-crf", "20",
                     "-c:a", "copy", out]
    commands.append(Command("render", "叠加渲染（overlay/drawtext）", argv))
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
            font = f"fontfile='{FONT_PATH}':" if os.path.isfile(FONT_PATH) else ""
            filters.append(
                f"{cur}drawtext={font}textfile='{txt_path}':fontsize={fs}:"
                f"fontcolor={color}:x={x}:y={y}:enable='{enable}'{label}"
            )
        cur = label
        step += 1
    vout = "[vout]"
    filters.append(f"{cur}copy{vout}")

    # 3) 音频：所有片段 concat 后 atrim 到 D（硬切语义）
    a_in = "".join(f"[{i}:a]" for i in range(n_clips))
    filters.append(
        f"{a_in}concat=n={n_clips}:v=0:a=1[a0];"
        f"[a0]atrim=0:{math['D']},asetpts=PTS-STARTPTS[aout]"
    )

    argv = ["ffmpeg", "-y"] + inputs + ["-filter_complex", ";".join(filters),
                     "-map", vout, "-map", "[aout]",
                     "-c:v", "libx264", "-crf", "20", "-c:a", "aac", out]
    commands = [Command("render", "转场渲染（xfade 链式 + 叠加 + 音频拼接）", argv)]
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
