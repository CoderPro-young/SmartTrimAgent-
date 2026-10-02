"""V7.9 TransNetV2 镜头边界复核（借鉴 FireRed-OpenStoryline，小红书 FireRedTeam）。

ffmpeg 的 scene 分数是帧差启发式：频闪/快速甩镜会把分数间歇性顶过阈值，
把一段连续镜头误切成 0.1s 级碎片。TransNetV2 是专用的镜头边界检测网络
（输入仅 48×27@25fps，CPU 可跑，pip 包自带权重），对这类素材鲁棒得多。

V7.10 起（B 方案）TransNetV2 升级为**主切分器**（与 OpenStoryline 同款）：
所有视频默认模型先行，ffmpeg 启发式阶梯降级为「依赖缺失/模型失败」时的
兜底——模型对渐变转场的敏感度是帧差法没有的，漏切（欠切）此前根本没有
触发复核的信号。回退开关：TRANSNET_PRIMARY=0 回到 V7.9 的「过碎复核」
旧形态；TRANSNET_RESCUE=0 整体不接模型（纯 ffmpeg，行为同 V7.8）。

任何失败（未装 torch、权重缺失、解码失败、超时）返回 None，调用方回退
启发式阶梯——不装依赖的环境行为零变化。

依赖（可选）：pip install transnetv2_pytorch==1.0.5（MIT，包内含权重）。
TRANSNET_WEIGHTS 可指向自定义 .pth 覆盖权重（如 OpenStoryline 仓库内同源
副本）；TRANSNET_DEVICE 默认 cpu（确定性优先）。
"""

from __future__ import annotations

import functools
import os

FPS = 25                  # TransNetV2 输入帧率（包内约定）
INPUT_W = 48
INPUT_H = 27
THRESHOLD = 0.5           # 转场概率阈值（OpenStoryline 同款默认）
DECODE_TIMEOUT = 300      # 整段解码 + CPU 推理，与场景切分同宽
SCALE_FLAGS = "fast_bilinear"

_DISABLE = {"0", "false", "off", "no"}


def enabled() -> bool:
    """模型总开关：TRANSNET_RESCUE=0 整体关闭（默认开）。"""
    return os.environ.get("TRANSNET_RESCUE", "1").strip().lower() not in _DISABLE


def primary_enabled() -> bool:
    """主切分开关（V7.10 B 方案）：TRANSNET_PRIMARY=0 回到「过碎复核」旧形态。"""
    return os.environ.get("TRANSNET_PRIMARY", "1").strip().lower() not in _DISABLE


@functools.lru_cache(maxsize=1)
def _load_model():
    """加载并缓存模型；pip 包自带权重，TRANSNET_WEIGHTS 存在则覆盖。"""
    import torch
    from transnetv2_pytorch import TransNetV2

    device = os.environ.get("TRANSNET_DEVICE", "cpu").strip() or "cpu"
    model = TransNetV2(device=device)
    custom = os.environ.get("TRANSNET_WEIGHTS", "").strip()
    if custom and os.path.isfile(custom):
        model.load_state_dict(torch.load(custom, map_location=model.device))
    model.eval()
    return model


def availability() -> str | None:
    """None = 可用；否则返回不可用原因（短文本，报告/排查用）。"""
    try:
        _load_model()
        return None
    except Exception as exc:  # noqa: BLE001
        return f"TransNetV2 不可用：{type(exc).__name__}: {exc}"[:200]


def scenes_to_boundaries(scenes) -> list:
    """TransNetV2 scene 列表 → 镜头起始点列表。

    与 parse_showinfo 同口径：滤掉 ≈0（首场景起点恒为 0，build_shots 会补）；
    start_time 在包里是 "ss.mmm" 字符串。"""
    pts: list[float] = []
    for sc in list(scenes)[1:]:
        try:
            t = float(sc.get("start_time"))
        except (AttributeError, TypeError, ValueError):
            continue
        if t > 0.01:
            pts.append(round(t, 3))
    return sorted(set(pts))


def detect(run, full: str, duration) -> list | None:
    """TransNetV2 重切镜头边界；任何失败返回 None（调用方回退升阈值阶梯）。

    run 为 ffmpeg_exec.run_binary 形态的执行器（stdout 是 bytes）。解码
    argv 与 OpenStoryline read_video_frames_as_rgb24 一致：fps+scale 到
    48×27 rgb24 经 rawvideo 管道输出，内存占用每帧约 3.8KB。
    """
    try:
        import numpy as np
        import torch

        res = run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
             "-i", full, "-an",
             "-vf", f"fps={FPS},scale={INPUT_W}:{INPUT_H}:flags={SCALE_FLAGS}",
             "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"],
            timeout=DECODE_TIMEOUT,
        )
        if not res.get("ok"):
            return None
        raw = res.get("stdout") or b""
        bpf = INPUT_W * INPUT_H * 3
        n = len(raw) // bpf
        if n < 2:                          # 不足两帧：没有切分意义
            return None
        frames = np.frombuffer(raw[: n * bpf], dtype=np.uint8).copy().reshape(
            n, INPUT_H, INPUT_W, 3)        # copy：frombuffer 只读，torch 要可写
        model = _load_model()
        with torch.inference_mode():
            single, _all = model.predict_raw(
                torch.from_numpy(frames).unsqueeze(0).contiguous())
        prediction = single.detach().cpu().numpy().reshape(-1)
        scenes = model.predictions_to_scenes_with_data(
            prediction, fps=float(FPS), threshold=THRESHOLD)
        return scenes_to_boundaries(scenes)
    except Exception:                      # 复核失败不是错误：静默回退
        return None
