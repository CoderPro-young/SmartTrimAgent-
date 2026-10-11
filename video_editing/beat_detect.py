"""BGM 节拍检测（V8.2 节拍卡点的信号源）：能量包络 + 自相关定速，零新依赖。

思路（FireRed select_bgm 用 librosa；本项目不引重依赖，用 ~80 行 numpy
实现同等的"BPM + 节拍时刻"输出，曲库以 Mixkit 流行/电子为主，节拍清晰）：

  ffmpeg 解码到单声道 f32 PCM @22050（复用 ffmpeg_exec.run_binary 管道，
  进程可被「取消任务」杀掉）→ 分帧能量（win 1024 / hop 512）→ onset 包络
  = 能量正差分半波整流 → 自相关在 60~180 BPM 拉格范围找周期定 BPM →
  相位扫描找最优偏移 → 节拍栅格 offset + k·period → 置信度 =
  节拍点处 onset 强度 / 全局均值（低于阈值判「无清晰节拍」，ambient 类
  乐曲会安静地返回 None，卡点自动缺席，绝不硬凑）。

结果按文件 stat 缓存（TMP/probe/beats_<key>.json，与内容卡同策略）。
"""

from __future__ import annotations

import hashlib
import json
import os

SR = 22050          # 分析采样率
WIN = 1024          # 能量分析窗
HOP = 512           # 帧移（≈23ms 一帧）
BPM_MIN, BPM_MAX = 60.0, 180.0
CONF_MIN = 1.6      # 节拍处 onset 强度 ≥ 全局均值 1.6 倍才认为节拍可信


def _cache_path(project_root: str, src: str) -> str:
    full = os.path.join(project_root, src)
    key_raw = f"{src}|{os.path.getsize(full) if os.path.isfile(full) else 0}"
    key = hashlib.sha1(key_raw.encode("utf-8")).hexdigest()[:16]
    return os.path.join(project_root, "TMP", "probe", f"beats_{key}.json")


def _decode_pcm(src: str, project_root: str, run_binary) -> "list[float] | None":
    """解码到单声道 f32le PCM（秒数截到 180s——BPM 不需要更长）。"""
    import ffmpeg_exec
    argv = ["ffmpeg", "-nostdin", "-loglevel", "error",
            "-t", "180", "-i", os.path.join(project_root, src),
            "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    res = (run_binary or ffmpeg_exec.run_binary)(argv, timeout=120)
    if not res.get("ok"):
        return None
    data = res.get("stdout") or b""
    if len(data) < SR * 2:                    # 不足 2 秒音频无从谈节拍
        return None
    import numpy as np
    return np.frombuffer(data[: len(data) // 4 * 4], dtype="<f4")


def _analyze(pcm) -> dict | None:
    import numpy as np

    x = pcm.astype("float64")
    n = (len(x) - WIN) // HOP + 1
    if n < 40:
        return None
    frames = np.lib.stride_tricks.as_strided(
        x, shape=(n, WIN), strides=(x.strides[0] * HOP, x.strides[0]))
    energy = (frames ** 2).mean(axis=1)
    # onset 包络：能量正差分（半波整流）+ 轻平滑
    flux = np.diff(energy, prepend=energy[0])
    flux[flux < 0] = 0
    kernel = np.array([0.25, 0.5, 0.25])
    onset = np.convolve(flux, kernel, mode="same")

    # 自相关定 BPM：帧周期 → 拉格（60~180 BPM）
    fps = SR / HOP
    lag_min = int(60.0 / BPM_MAX * fps)
    lag_max = int(60.0 / BPM_MIN * fps)
    onset_c = onset - onset.mean()
    ac = np.correlate(onset_c, onset_c, mode="full")[len(onset_c) - 1:]
    ac = ac[: lag_max + 1]
    if ac[0] <= 0:
        return None
    ac = ac / ac[0]
    # 倍频修正：自相关常在周期的 2 倍处更高（隔拍对齐无泄漏）——
    # 取「达到峰值 85% 的最小拉格」作为基频（经典 tempo octave 问题）
    peak = float(ac[lag_min: lag_max + 1].max())
    if peak < 0.15:                           # 周期性太弱（如 ambient）
        return None
    lag = lag_min
    for k in range(lag_min, lag_max + 1):
        if ac[k] >= 0.85 * peak:
            lag = k
            break
    # 亚帧周期细化（抛物线插值）：帧量化在 120bpm 处就有 ~4.6% 周期误差，
    # 二十几秒就漂出容差——必须把周期 refinement 到亚帧精度
    if lag_min < lag < lag_max:
        a, b, c = float(ac[lag - 1]), float(ac[lag]), float(ac[lag + 1])
        denom = a - 2 * b + c
        if abs(denom) > 1e-12:
            lag = lag + max(-1.0, min(1.0, 0.5 * (a - c) / denom))
    period = lag / fps
    bpm = round(60.0 / period, 1)

    # 相位：0~period 内扫描偏移（以帧为单位的栅格），节拍处 onset 均值最高者胜
    idx = np.arange(n)

    def _near_frames(offset_frames: float, tol_frames: float):
        rel = (idx - offset_frames) / lag          # 每帧的「拍位」（浮点）
        return (np.abs(rel - np.round(rel)) * lag) < tol_frames

    best_off, best_score = 0.0, -1.0
    for k in range(int(lag)):
        near = _near_frames(float(k), 1.5)         # 距栅格 ≤1.5 帧（≈35ms）
        if near.sum() < 4:
            continue
        score = onset[near].mean()
        if score > best_score:
            best_score, best_off = score, k / fps

    # 置信度：节拍处强度 vs 全局均值（±2.5 帧 ≈ 58ms 内算“踩在拍上”）
    on_beat = _near_frames(best_off * fps, 2.5)
    if on_beat.sum() < 4:
        return None
    conf = float(onset[on_beat].mean() / (onset.mean() + 1e-9))
    if conf < CONF_MIN:
        return None
    beats = [round(best_off + i * period, 3)
             for i in range(int((len(x) / SR - best_off) / period) + 1)]
    return {"bpm": bpm, "offset": round(best_off, 3), "beats": beats,
            "confidence": round(conf, 2)}


def detect_beats(src: str, project_root: str, *,
                 run_binary=None) -> dict | None:
    """检测 BGM 节拍；任何失败/低置信度返回 None（调用方优雅跳过卡点）。

    返回 {"bpm", "offset", "beats": [秒...], "confidence"}，按文件缓存。
    """
    full = os.path.join(project_root, src)
    if not os.path.isfile(full):
        return None
    cache = _cache_path(project_root, src)
    try:
        with open(cache, encoding="utf-8") as f:
            hit = json.load(f)
        return hit if hit.get("bpm") else None           # {"bpm": null} 也算已判
    except (OSError, ValueError):
        pass
    result: dict | None = None
    try:
        pcm = _decode_pcm(src, project_root, run_binary)
        if pcm is not None:
            result = _analyze(pcm)
    except Exception:                                     # noqa: BLE001
        result = None
    try:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(result or {"bpm": None}, f, ensure_ascii=False)
    except OSError:
        pass
    return result
