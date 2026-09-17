"""能力注册表：Skill 的单一来源（single source of truth）。

一处定义，同时派生三处消费：
  ① plan_schema   —— effects 参数校验（validate）
  ② plan_compiler —— ffmpeg 滤镜翻译（build / prepass）
  ③ video_agent   —— V2_SYSTEM_PROMPT 的能力文档（summary / prompt_doc）

这是 2026-09-11「mirror 幻觉」的结构性解法：提示词与白名单不再人工同步，
而是同源派生，脱节从根上消失（原坑：README 示例"水平镜像"→ 模型猜
`{"name":"mirror"}`，因为提示词里没写全白名单）。

新增一个能力 = 在 SKILLS 里加一条，其余三处自动生效。

pipeline 三种取值：
  VF       —— 并入 -vf 滤镜链（大多数视觉滤镜）
  PREPASS  —— 需要独立前置命令 + 输入替换（如 face_mosaic 的人脸检测）
音频轨（BGM）不是逐 clip 效果，走 AUDIO 规格（validate_audio + AUDIO_PROMPT_DOC）。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Callable

VF = "vf"
PREPASS = "prepass"


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


@dataclass(frozen=True)
class Skill:
    """一个可被编辑计划引用的能力。

    name        —— 计划里 effects[].name 的合法取值
    summary     —— 一句话说明（进提示词）
    pipeline    —— VF | PREPASS
    arg_keys    —— args 的合法 key 集合；多写的 key 由 plan_schema 统一报错拦下
    validate    —— (args, tag, clip) -> 错误列表；tag 形如 clips[0].effects[1]
                   只负责「值的合法性」，key 的合法性由 arg_keys 保证
    build       —— args -> 滤镜表达式（仅 VF 类）
    prepass     —— (input_path, output_path, args) -> argv（仅 PREPASS 类）
    prompt_doc  —— 多行用法说明（进提示词）
    """

    name: str
    summary: str
    pipeline: str = VF
    arg_keys: frozenset[str] = frozenset()
    validate: Callable[[dict, str, dict], list[str]] | None = None
    build: Callable[[dict], str] | None = None
    prepass: Callable[[str, str, dict], list[str]] | None = None
    prompt_doc: str = ""


# --------------------------------------------------------------------------- #
# hflip / vflip
# --------------------------------------------------------------------------- #

def _always_ok(args: dict, tag: str, clip: dict) -> list[str]:
    return []


SKILL_HFLIP = Skill(
    name="hflip",
    summary="horizontal mirror (left-right flip).",
    validate=_always_ok,
    build=lambda args: "hflip",
)

SKILL_VFLIP = Skill(
    name="vflip",
    summary="vertical flip (upside down).",
    validate=_always_ok,
    build=lambda args: "vflip",
)


# --------------------------------------------------------------------------- #
# transpose
# --------------------------------------------------------------------------- #

def _val_transpose(args: dict, tag: str, clip: dict) -> list[str]:
    d = args.get("dir")
    if d is not None and (not _num(d) or int(d) not in (0, 1, 2, 3)):
        return [f"{tag}.args.dir 必须是 0~3 的整数"
                f"（0=逆时针90°+垂直翻转, 1=顺时针90°, 2=逆时针90°, 3=顺时针90°+垂直翻转）。"]
    return []


SKILL_TRANSPOSE = Skill(
    name="transpose",
    summary="rotate / flip 90 degrees.",
    arg_keys=frozenset({"dir"}),
    validate=_val_transpose,
    build=lambda args: f"transpose={int(args.get('dir', 1))}",
    prompt_doc="  Optional `args.dir`: 0..3 (default 1 = rotate 90° clockwise).",
)


# --------------------------------------------------------------------------- #
# eq（色彩调整）
# --------------------------------------------------------------------------- #

_EQ_KEYS = ("brightness", "contrast", "saturation")
_EQ_RANGE = {"brightness": (-1.0, 1.0), "contrast": (-1000.0, 1000.0),
             "saturation": (0.0, 3.0)}


def _val_eq(args: dict, tag: str, clip: dict) -> list[str]:
    errors: list[str] = []
    for k in _EQ_KEYS:
        v = args.get(k)
        if v is None:
            continue
        if not _num(v):
            errors.append(f"{tag}.args.{k} 必须是数字。")
            continue
        lo, hi = _EQ_RANGE[k]
        if not (lo <= v <= hi):
            errors.append(f"{tag}.args.{k} 必须在 {lo}~{hi} 内。")
    return errors


def _build_eq(args: dict) -> str:
    kv = {k: v for k, v in args.items() if k in _EQ_KEYS}
    if not kv:
        return "eq"
    return "eq=" + ":".join(f"{k}={v}" for k, v in kv.items())


SKILL_EQ = Skill(
    name="eq",
    summary="adjust color: brightness / contrast / saturation.",
    arg_keys=frozenset(_EQ_KEYS),
    validate=_val_eq,
    build=_build_eq,
    prompt_doc=("  Optional `args`: `brightness` (-1..1, default 0), "
                "`contrast` (-1000..1000, default 1), `saturation` (0..3, default 1).\n"
                "  Example: {\"name\":\"eq\",\"args\":{\"brightness\":0.1,\"saturation\":1.2}}\n"
                "  NOTE: `saturation: 0` turns the clip black & white (grayscale)."),
)


# --------------------------------------------------------------------------- #
# delogo（去水印）
# --------------------------------------------------------------------------- #

def _val_delogo(args: dict, tag: str, clip: dict) -> list[str]:
    errors: list[str] = []
    region = args.get("region")
    if not (isinstance(region, list) and len(region) == 4 and all(_num(v) for v in region)):
        return [f"{tag}.args.region 必须是 [x,y,w,h] 四个数字（素材原始像素坐标），"
                f"例如 [20,20,220,90]。"]
    x, y, w, h = region
    if x < 0 or y < 0:
        errors.append(f"{tag}.args.region 的 x/y 不能为负。")
    if w <= 0 or h <= 0:
        errors.append(f"{tag}.args.region 的 w/h 必须为正。")

    # 有 probe 尺寸时做越界与贴边校验：delogo 需要四周像素做插值，
    # 区域贴边会导致滤镜报错（必须留至少 1px 边距）。
    probe = (clip or {}).get("probe") or {}
    pw, ph = probe.get("width"), probe.get("height")
    if _num(pw) and _num(ph) and w > 0 and h > 0:
        if x + w > pw - 1 or y + h > ph - 1:
            errors.append(
                f"{tag}.args.region 越界或贴边：区域右下角为 ({x + w},{y + h})，"
                f"素材尺寸 {pw}x{ph}；delogo 需要四周留白，请保证 x+w <= {int(pw) - 1} "
                f"且 y+h <= {int(ph) - 1}。"
            )
    return errors


def _build_delogo(args: dict) -> str:
    x, y, w, h = args["region"]
    return f"delogo=x={int(x)}:y={int(y)}:w={int(w)}:h={int(h)}"


SKILL_DELOGO = Skill(
    name="delogo",
    summary="remove a watermark / logo by covering a rectangular area.",
    arg_keys=frozenset({"region"}),
    validate=_val_delogo,
    build=_build_delogo,
    prompt_doc=(
        "  Required `args.region` = [x, y, w, h] in ORIGINAL source pixels\n"
        "  (the area runs on the untrimmed, unscaled frame).\n"
        "  The area must NOT touch the frame edge — leave at least 1px margin,\n"
        "  because delogo needs surrounding pixels to interpolate.\n"
        "  This is a cover/blend, NOT AI inpainting: it hides the logo area but\n"
        "  cannot reconstruct what was behind it.\n"
        "  Example: {\"name\":\"delogo\",\"args\":{\"region\":[20,20,220,90]}}"
    ),
)


# --------------------------------------------------------------------------- #
# face_mosaic（人脸打码，PREPASS）
# --------------------------------------------------------------------------- #

MOSAIC_MODES = {"mosaic", "blur"}
MOSAIC_TARGETS = {"all", "largest"}


def _val_face_mosaic(args: dict, tag: str, clip: dict) -> list[str]:
    errors: list[str] = []
    if "mode" in args and args["mode"] not in MOSAIC_MODES:
        errors.append(f"{tag}.args.mode 必须是 mosaic 或 blur。")
    bs = args.get("block_size")
    if bs is not None and (not _num(bs) or not (2 <= bs <= 64)):
        errors.append(f"{tag}.args.block_size 必须在 2~64 内。")
    st = args.get("score_threshold")
    if st is not None and (not _num(st) or not (0.3 <= st <= 0.99)):
        errors.append(f"{tag}.args.score_threshold 必须在 0.3~0.99 内。")
    tg = args.get("target")
    if tg is not None and tg not in MOSAIC_TARGETS:
        errors.append(f"{tag}.args.target 必须是 all 或 largest。")
    return errors


def _prepass_face_mosaic(input_path: str, output_path: str, args: dict) -> list[str]:
    """生成人脸打码的 Python 子进程命令（不是 ffmpeg）。"""
    argv = [
        sys.executable, "video_editing/face_mosaic.py",
        "--input", input_path,
        "--output", output_path,
        "--mode", args.get("mode", "mosaic"),
    ]
    if "block_size" in args:
        argv += ["--block-size", str(int(args["block_size"]))]
    if "score_threshold" in args:
        argv += ["--score-threshold", str(args["score_threshold"])]
    if "target" in args:
        argv += ["--target", str(args["target"])]
    return argv


SKILL_FACE_MOSAIC = Skill(
    name="face_mosaic",
    summary="mosaic / blur ALL detected faces in the clip (privacy).",
    pipeline=PREPASS,
    arg_keys=frozenset({"mode", "block_size", "score_threshold", "target"}),
    validate=_val_face_mosaic,
    prepass=_prepass_face_mosaic,
    prompt_doc=(
        "  Optional `args`: `mode` (\"mosaic\" pixelate, default | \"blur\"),\n"
        "  `block_size` (2..64, default 20), `score_threshold` (0.3..0.99, default 0.7),\n"
        "  `target` (\"all\" faces, default | \"largest\" face only).\n"
        "  Detected automatically over the whole clip — no coordinates needed.\n"
        "  If faces are small / far away, LOWER `score_threshold` to 0.3~0.5,\n"
        "  otherwise most faces are missed.\n"
        "  Example: {\"name\":\"face_mosaic\",\"args\":{\"mode\":\"mosaic\",\"score_threshold\":0.4}}"
    ),
)


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #

SKILLS: dict[str, Skill] = {
    s.name: s for s in (
        SKILL_HFLIP, SKILL_VFLIP, SKILL_TRANSPOSE, SKILL_EQ,
        SKILL_DELOGO, SKILL_FACE_MOSAIC,
    )
}


def get_skill(name: str) -> Skill | None:
    return SKILLS.get(name)


def is_vf(name: str) -> bool:
    s = SKILLS.get(name)
    return bool(s and s.pipeline == VF)


def is_prepass(name: str) -> bool:
    s = SKILLS.get(name)
    return bool(s and s.pipeline == PREPASS)


def check_args_keys(skill: Skill, args: dict, tag: str) -> list[str]:
    """args 的 key 是否都在该能力的白名单里。

    单独抽出来，是为了让「多写的字段」在编译前就被拦下——
    否则它们会被静默忽略，用户以为做成了、实际没做（2026-09-16 实测到的缺陷）。
    """
    extra = [k for k in args if k not in skill.arg_keys]
    if not extra:
        return []
    allowed = ", ".join(sorted(skill.arg_keys)) if skill.arg_keys else "（该效果不接受任何参数）"
    return [
        f"{tag}.args 含不支持的参数 {extra}；`{skill.name}` 只接受：{allowed}。"
        f"多写的参数不会被忽略，而是直接判为无效计划。"
    ]


# --------------------------------------------------------------------------- #
# 音频轨（BGM）：不是逐 clip 效果，单独一套规格
# --------------------------------------------------------------------------- #

def validate_audio(audio: dict, tag: str, project_root: str) -> list[str]:
    """校验计划的顶层 audio（BGM）对象。"""
    import os

    errors: list[str] = []
    src = audio.get("source")
    if not isinstance(src, str) or not src:
        errors.append(f"{tag}.source 必须是 INPUT/ 下的音频文件名，如 INPUT/bgm.mp3。")
    elif not src.startswith("INPUT/"):
        errors.append(f"{tag}.source 必须以 INPUT/ 开头。")
    elif not os.path.isfile(os.path.join(project_root, src)):
        errors.append(f"{tag}.source 文件不存在：{src}。")

    vol = audio.get("volume")
    if vol is not None and (not _num(vol) or not (0 < vol <= 1)):
        errors.append(f"{tag}.volume 必须在 (0, 1] 内（默认 0.3）。")
    for k in ("fade_in", "fade_out"):
        v = audio.get(k)
        if v is not None and (not _num(v) or v < 0):
            errors.append(f"{tag}.{k} 必须是非负数（秒）。")
    for k in ("loop", "ducking"):
        v = audio.get(k)
        if v is not None and not isinstance(v, bool):
            errors.append(f"{tag}.{k} 必须是 true/false。")

    unknown = [k for k in audio if k not in
               ("source", "volume", "fade_in", "fade_out", "loop", "ducking")]
    if unknown:
        errors.append(f"{tag} 含未知字段 {unknown}；只支持 "
                      f"source/volume/fade_in/fade_out/loop/ducking。")
    return errors


AUDIO_PROMPT_DOC = """## Background music (optional top-level `audio` object)

To lay music over the WHOLE timeline, add a top-level `audio` object — do NOT
create a clip entry for the music:

    "audio": {"source": "INPUT/bgm.mp3", "volume": 0.3,
              "fade_in": 1, "fade_out": 2, "loop": true, "ducking": false}

- `source` (required): an audio file in INPUT/ (mp3/wav/m4a/flac/ogg/aac...).
  A video file also works if you only want its audio.
- `volume` (0, 1], default 0.3 — keep music clearly below speech.
- `fade_in` / `fade_out` seconds, default 0.
- `loop` default true: short music repeats to fill the video.
- `ducking` default false: true automatically lowers the music while people
  are speaking (sidechain compression).
The music is mixed with the clips' original audio; the compiler handles it."""


# --------------------------------------------------------------------------- #
# 提示词派生（③）
# --------------------------------------------------------------------------- #

def render_prompt_doc() -> str:
    """从注册表生成 effects 能力清单，供 V2_SYSTEM_PROMPT 自动拼装。

    提示词不再手写效果名 —— 注册表加一条，提示词自动多一条。
    """
    lines: list[str] = []
    for s in SKILLS.values():
        kind = " (runs as an automatic pre-pass)" if s.pipeline == PREPASS else ""
        lines.append(f"- `{s.name}`: {s.summary}{kind}")
        if s.prompt_doc:
            lines.append(s.prompt_doc)
    return "\n".join(lines)


def effects_menu() -> list[str]:
    """合法 effects 名列表（校验错误信息里用）。"""
    return sorted(SKILLS)
