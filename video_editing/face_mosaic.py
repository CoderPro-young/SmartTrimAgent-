"""人脸检测 + 打码模块（v3.0）。

用 OpenCV YuNet（MIT 许可）逐帧检测人脸，经跟踪平滑后对 bbox 区域做
像素化（mosaic）或高斯模糊（blur）打码，输出打码后的视频（尽量保留音频）。

两种用法：
1. 作为模块调用：
       from video_editing.face_mosaic import apply_face_mosaic
       apply_face_mosaic("INPUT/sample.mp4", "TMP/c1_masked.mp4")
2. 作为 CLI 子命令（plan_compiler 生成）：
       python -m video_editing.face_mosaic --input INPUT/sample.mp4 --output TMP/c1_masked.mp4

YuNet 模型：models/face_detection_yunet_2023mar.onnx（随仓库分发）。
"""

from __future__ import annotations

import argparse
import os
import sys

import cv2
import numpy as np

MODEL_FILENAME = "face_detection_yunet_2023mar.onnx"

# 跟踪平滑默认参数
IOU_THRESH = 0.3      # 相邻帧 IoU 匹配阈值
MAX_MISS = 8          # 丢失后沿用旧框的最大帧数
SMOOTH_ALPHA = 0.3    # bbox 指数平滑系数（越大越跟手，越小越稳）
PAD_RATIO = 0.20      # bbox 外扩比例（防边缘漏码）


def _default_model_path() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根
    candidates = [
        os.path.join(root, "models", MODEL_FILENAME),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), MODEL_FILENAME),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return candidates[0]


class _Track:
    """一个被跟踪的人脸目标。"""

    __slots__ = ("bbox", "score", "missed")

    def __init__(self, bbox: np.ndarray, score: float):
        self.bbox = bbox.astype(np.float32)  # [x, y, w, h]
        self.score = score
        self.missed = 0


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    """两个 [x, y, w, h] 框的 IoU。"""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _smooth_tracks(tracks: list[_Track], dets: list[np.ndarray]) -> list[_Track]:
    """用新检测更新 track 列表（IoU 匹配 + 指数平滑 + 丢失沿用）。

    dets: 每个元素为 [x, y, w, h, score]。
    """
    used = [False] * len(dets)
    # 1) 匹配已有 track
    for t in tracks:
        best_i, best_j = -1.0, -1
        for j, d in enumerate(dets):
            if used[j]:
                continue
            iou = _iou(t.bbox, d[:4])
            if iou > best_i:
                best_i, best_j = iou, j
        if best_j >= 0 and best_i >= IOU_THRESH:
            d = dets[best_j]
            used[best_j] = True
            t.bbox = (1 - SMOOTH_ALPHA) * t.bbox + SMOOTH_ALPHA * d[:4].astype(np.float32)
            t.score = float(d[4])
            t.missed = 0
        else:
            t.missed += 1
    # 2) 丢弃长时间丢失的 track
    tracks = [t for t in tracks if t.missed <= MAX_MISS]
    # 3) 未匹配的检测 → 新 track
    for j, d in enumerate(dets):
        if not used[j]:
            tracks.append(_Track(d[:4], float(d[4])))
    return tracks


def _mosaic_region(frame: np.ndarray, x: int, y: int, w: int, h: int, block: int) -> None:
    """像素化打码：缩小再放大。block 越大马赛克越粗。"""
    if block < 1:
        block = 1
    roi = frame[y:y + h, x:x + w]
    small = cv2.resize(roi, (max(1, w // block), max(1, h // block)),
                       interpolation=cv2.INTER_LINEAR)
    frame[y:y + h, x:x + w] = cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)


def _blur_region(frame: np.ndarray, x: int, y: int, w: int, h: int) -> None:
    """高斯模糊打码：核大小随脸宽自适应。"""
    k = int(max(15, w // 8))
    if k % 2 == 0:
        k += 1
    roi = frame[y:y + h, x:x + w]
    frame[y:y + h, x:x + w] = cv2.GaussianBlur(roi, (k, k), 0)


def _pad_bbox(bbox: np.ndarray, fw: int, fh: int) -> tuple[int, int, int, int]:
    """外扩 bbox 并裁剪到画面内，返回 (x, y, w, h)。"""
    x, y, w, h = bbox
    pw, ph = w * PAD_RATIO, h * PAD_RATIO
    x0 = int(max(0, x - pw))
    y0 = int(max(0, y - ph))
    x1 = int(min(fw, x + w + pw))
    y1 = int(min(fh, y + h + ph))
    return x0, y0, x1 - x0, y1 - y0


def apply_face_mosaic(
    src: str,
    dst: str,
    mode: str = "mosaic",
    block_size: int = 20,
    score_threshold: float = 0.7,
    target: str = "all",
    model_path: str | None = None,
    keep_audio: bool = True,
    nms_threshold: float = 0.3,
    top_k: int = 500,
) -> dict:
    """对视频逐帧检测人脸并打码，输出到 dst。

    Returns:
        dict: {"ok": bool, "frames": int, "faces_detected": int, "note": str, "output": str}
    """
    model_path = model_path or _default_model_path()
    if not os.path.isfile(model_path):
        return {"ok": False, "note": f"人脸检测模型不存在：{model_path}", "output": dst}
    if not os.path.isfile(src):
        return {"ok": False, "note": f"输入文件不存在：{src}", "output": dst}

    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        return {"ok": False, "note": f"无法打开输入视频：{src}", "output": dst}

    fw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    fh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    if fw <= 0 or fh <= 0:
        cap.release()
        return {"ok": False, "note": "无法读取视频尺寸", "output": dst}

    detector = cv2.FaceDetectorYN.create(
        model_path, "", (fw, fh), score_threshold, nms_threshold, top_k
    )
    detector.setInputSize((fw, fh))

    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    writer = cv2.VideoWriter(dst, cv2.VideoWriter_fourcc(*"mp4v"), fps, (fw, fh))
    if not writer.isOpened():
        cap.release()
        return {"ok": False, "note": "无法创建输出视频（编码器不可用）", "output": dst}

    tracks: list[_Track] = []
    frames = 0
    faces_detected = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames += 1

        _, faces = detector.detect(frame)  # Nx15 或 None
        dets: list[np.ndarray] = []
        if faces is not None:
            for f in faces:
                dets.append(np.array(f[:5], dtype=np.float32))  # [x,y,w,h,score]
            faces_detected += len(dets)

        tracks = _smooth_tracks(tracks, dets)

        # 选择要打码的目标
        active = list(tracks)
        if target == "largest" and active:
            active = [max(active, key=lambda t: t.bbox[2] * t.bbox[3])]

        for t in active:
            x, y, w, h = _pad_bbox(t.bbox, fw, fh)
            if w <= 0 or h <= 0:
                continue
            if mode == "blur":
                _blur_region(frame, x, y, w, h)
            else:
                _mosaic_region(frame, x, y, w, h, block_size)

        writer.write(frame)

    cap.release()
    writer.release()

    # 音频保留：ffmpeg 可用时从源 copy 音频合流（不重编码）
    if keep_audio:
        try:
            from ffmpeg_exec import ffmpeg_available, run
            if ffmpeg_available():
                mid = dst + ".video.mp4"
                os.replace(dst, mid)
                res = run(
                    ["ffmpeg", "-y", "-i", mid, "-i", src,
                     "-map", "0:v", "-map", "1:a?", "-c", "copy", dst]
                )
                if res["ok"]:
                    os.remove(mid)
                else:
                    os.replace(mid, dst)  # 回退为纯视频
        except Exception:
            pass  # 音频合流失败不阻断主流程，仅输出无音频视频

    return {
        "ok": True,
        "frames": frames,
        "faces_detected": faces_detected,
        "note": f"共处理 {frames} 帧，累计检测 {faces_detected} 个人脸框",
        "output": dst,
    }


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="人脸检测打码（YuNet）")
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--mode", choices=["mosaic", "blur"], default="mosaic")
    p.add_argument("--block-size", type=int, default=20)
    p.add_argument("--score-threshold", type=float, default=0.7)
    p.add_argument("--target", default="all")
    p.add_argument("--model", default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    result = apply_face_mosaic(
        src=args.input, dst=args.output, mode=args.mode,
        block_size=args.block_size, score_threshold=args.score_threshold,
        target=args.target, model_path=args.model,
    )
    print(result["note"])
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
