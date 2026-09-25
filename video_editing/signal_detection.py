"""确定性 A/V 信号检测（T10）：静音区间、黑场区间与区间数学。

「LLM 出意图，编译器保正确」不变；本模块给粗剪提供零幻觉的废段信号：

    ffmpeg silencedetect / blackdetect（确定性，只产出时间区间）
      → stderr 正则解析 → [[start, end], ...]
      → kept_intervals 补集反转（借鉴 LosslessCut 的 invert segments：
        「剪掉静音」的语义 = 静音区间的补集，再按 keep_padding 收缩边界）

实现模式参考 LosslessCut（src/main/ffmpeg.ts）：
- silencedetect 输出在 stderr：`silence_end: T | silence_duration: D`，
  start 由 end - duration 反推；文件末尾的静音只有 silence_start 没有
  silence_end，用总时长补闭合。
- blackdetect 输出在 stderr：`black_start: S black_end: E black_duration: D`。

所有函数纯确定性、可离线测试（run_fn 注入假 ffmpeg）。
"""

from __future__ import annotations

import re

import ffmpeg_exec

DETECT_TIMEOUT = 300          # 检测整段解码，与场景切分同量级
DEFAULT_NOISE_DB = -35.0      # 低于此视为静音（口播环境常用 -35~-45dB）
DEFAULT_MIN_SILENCE = 0.4     # 短于此时长的静音不触发（避免切掉字间自然停顿）
DEFAULT_BLACK_MIN_DUR = 1.0   # 黑场最短持续时间
DEFAULT_KEEP_PADDING = 0.15   # 切口两侧保留的静音秒数（防止吃掉词头词尾）
DEFAULT_MIN_KEEP = 0.3        # 补集后短于此的碎片段丢弃

_SILENCE_END_RE = re.compile(
    r"silence_end:\s*([\d.]+)\s*\|?\s*silence_duration:\s*([\d.]+)")
_SILENCE_START_RE = re.compile(r"silence_start:\s*([\d.]+)")
_BLACK_RE = re.compile(r"black_start:\s*([\d.]+)\s+black_end:\s*([\d.]+)")


# --------------------------------------------------------------- 解析 ------- #

def parse_silencedetect(stderr: str, total_duration: float | None = None) -> list[list[float]]:
    """从 silencedetect 的 stderr 提取静音区间（排序 + 合并重叠）。

    total_duration 提供时，补闭合「只有 silence_start 没等到 silence_end」
    的末尾静音（录音结尾死寂是最常见形态）。
    """
    intervals: list[list[float]] = []
    last_start: float | None = None
    for line in (stderr or "").splitlines():
        m = _SILENCE_END_RE.search(line)
        if m:
            end, dur = float(m.group(1)), float(m.group(2))
            intervals.append([max(0.0, end - dur), end])
            last_start = None          # 已闭合，等待下一个 start
            continue
        m = _SILENCE_START_RE.search(line)
        if m:
            last_start = float(m.group(1))
    if last_start is not None and total_duration and total_duration > last_start:
        intervals.append([last_start, total_duration])
    return merge_intervals(intervals)


def parse_blackdetect(stderr: str) -> list[list[float]]:
    """从 blackdetect 的 stderr 提取黑场区间（排序 + 合并重叠）。"""
    intervals = [[float(m.group(1)), float(m.group(2))]
                 for m in _BLACK_RE.finditer(stderr or "")]
    return merge_intervals(intervals)


# ------------------------------------------------------------ 区间数学 ------ #

def merge_intervals(intervals: list, lo: float | None = None,
                    hi: float | None = None) -> list[list[float]]:
    """排序、合并重叠，并裁剪到 [lo, hi]；越界/零长区间丢弃。"""
    ivs = sorted((sorted([s, e]) for s, e in intervals if e > s), key=lambda x: x[0])
    if lo is not None or hi is not None:
        l = 0.0 if lo is None else float(lo)
        h = float("inf") if hi is None else float(hi)
        ivs = [[max(s, l), min(e, h)] for s, e in ivs if min(e, h) > max(s, l)]
    out: list[list[float]] = []
    for s, e in ivs:
        if out and s <= out[-1][1] + 1e-6:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [[round(s, 3), round(e, 3)] for s, e in out]


def kept_intervals(removed: list, lo: float, hi: float,
                   pad: float = DEFAULT_KEEP_PADDING,
                   min_keep: float = DEFAULT_MIN_KEEP) -> list[list[float]]:
    """「剪掉废段」的补集反转：removed 的补集，切口两侧留 pad，碎片段丢弃。

    - 区间内部边界（与废段相邻的边）向内收缩 pad，保留一点缓冲
      （口播剪停顿时不吃词头词尾）；lo/hi 外侧边界不动。
    - 收缩后短于 min_keep 的碎片段直接丢弃——比转场还短的片段没有意义。
    """
    rs = merge_intervals(removed, lo, hi)
    kept: list[list[float]] = []
    cur = lo
    for s, e in rs:
        if s > cur:
            kept.append([cur, s])
        cur = max(cur, e)
    if cur < hi:
        kept.append([cur, hi])
    out: list[list[float]] = []
    for s, e in kept:
        if s > lo:
            s += pad
        if e < hi:
            e -= pad
        if e - s >= min_keep:
            out.append([round(s, 3), round(e, 3)])
    return out


def overlap_seconds(ivals: list, lo: float, hi: float) -> float:
    """区间列表与 [lo, hi] 的重叠总时长（算镜头静音占比用）。"""
    total = 0.0
    for s, e in merge_intervals(ivals, lo, hi):
        total += e - s
    return round(total, 3)


# --------------------------------------------------------------- 检测 ------- #

def detect_silences(src: str, *, offset: float = 0.0, limit: float | None = None,
                    noise_db: float = DEFAULT_NOISE_DB,
                    min_silence: float = DEFAULT_MIN_SILENCE,
                    total_duration: float | None = None,
                    run_fn=None, cwd: str | None = None) -> dict:
    """检测静音区间，返回 {ok, intervals, error}。

    intervals 是**绝对时间**（已加回 offset）。-ss 放 -i 前（输入级快速
    seek，时间戳从 0 重起，与 LosslessCut 同款）。offset 提供且末尾静音
    未闭合时，用 offset+limit（或 total_duration）补闭合。
    """
    run = run_fn or ffmpeg_exec.run
    argv = ["ffmpeg", "-nostdin", "-hide_banner"]
    if offset > 0:
        argv += ["-ss", f"{offset:.3f}"]
    argv += ["-i", src, "-map", "0:a:0",
             "-af", f"silencedetect=noise={noise_db:g}dB:d={min_silence:g}"]
    if limit:
        argv += ["-t", f"{limit:.3f}"]
    argv += ["-f", "null", "-"]
    res = run(argv, timeout=DETECT_TIMEOUT, cwd=cwd)
    if not res.get("ok"):
        return {"ok": False, "intervals": [],
                "error": (res.get("stderr") or res.get("error") or "silencedetect 失败")[-400:]}
    # 末尾静音补闭合用的是【相对 seek 点】的时长；-ss 在 -i 前时间戳从 0 重起
    total_rel = limit
    if total_rel is None and total_duration is not None:
        total_rel = max(0.0, total_duration - offset)
    ivs = parse_silencedetect(res.get("stderr") or "", total_rel)
    if offset > 0:
        ivs = [[s + offset, e + offset] for s, e in ivs]
    return {"ok": True, "intervals": merge_intervals(ivs)}


def detect_blacks(src: str, *, offset: float = 0.0, limit: float | None = None,
                  min_duration: float = DEFAULT_BLACK_MIN_DUR,
                  run_fn=None, cwd: str | None = None) -> dict:
    """检测黑场区间（镜头盖误录/片头尾黑屏），返回 {ok, intervals, error}。"""
    run = run_fn or ffmpeg_exec.run
    argv = ["ffmpeg", "-nostdin", "-hide_banner"]
    if offset > 0:
        argv += ["-ss", f"{offset:.3f}"]
    argv += ["-i", src, "-map", "0:v:0",
             "-vf", f"blackdetect=d={min_duration:g}:pic_th=0.98:pix_th=0.10"]
    if limit:
        argv += ["-t", f"{limit:.3f}"]
    argv += ["-f", "null", "-"]
    res = run(argv, timeout=DETECT_TIMEOUT, cwd=cwd)
    if not res.get("ok"):
        return {"ok": False, "intervals": [],
                "error": (res.get("stderr") or res.get("error") or "blackdetect 失败")[-400:]}
    ivs = parse_blackdetect(res.get("stderr") or "")
    if offset > 0:
        ivs = [[s + offset, e + offset] for s, e in ivs]
    return {"ok": True, "intervals": merge_intervals(ivs)}


def detect_av(src: str, *, has_audio: bool = True,
              noise_db: float = DEFAULT_NOISE_DB,
              min_silence: float = DEFAULT_MIN_SILENCE,
              black_min_duration: float = DEFAULT_BLACK_MIN_DUR,
              total_duration: float | None = None,
              run_fn=None, cwd: str | None = None) -> dict:
    """单次解码同时检测静音 + 黑场（内容卡片建立索引时用，省一遍解码）。

    返回 {"ok", "silences", "blacks", "error"}。失败返回 ok=False 且带
    error——调用方按降级处理，绝不影响卡片里已有的镜头/标签数据。
    """
    run = run_fn or ffmpeg_exec.run
    argv = ["ffmpeg", "-nostdin", "-hide_banner", "-i", src,
            "-map", "0:v:0", "-vf",
            f"blackdetect=d={black_min_duration:g}:pic_th=0.98:pix_th=0.10"]
    if has_audio:
        argv += ["-map", "0:a:0", "-af",
                 f"silencedetect=noise={noise_db:g}dB:d={min_silence:g}"]
    argv += ["-f", "null", "-"]
    res = run(argv, timeout=DETECT_TIMEOUT, cwd=cwd)
    if not res.get("ok"):
        return {"ok": False, "silences": None, "blacks": None,
                "error": (res.get("stderr") or res.get("error") or "信号检测失败")[-400:]}
    stderr = res.get("stderr") or ""
    return {
        "ok": True,
        "silences": parse_silencedetect(stderr, total_duration) if has_audio else None,
        "blacks": parse_blackdetect(stderr),
    }
