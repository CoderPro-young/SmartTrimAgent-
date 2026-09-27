"""V4 感知层：镜头级内容理解（场景切分 + VLM 语义打标 + sidecar 缓存）。

「LLM 出意图，编译器保正确」不变；本模块给规划模型补上"看得见内容"的信号：

    ffmpeg 场景切分（确定性，产生时间边界）
      → 每镜头抽一帧代表画面（长边 640px JPEG）
      → VLM 批量打标（只出语义标签，绝不输出时间戳）
      → 内容卡片（纯文本）经 analyze_media 工具返回给规划模型
      → 缓存 TMP/probe/<hash>.json（roadmap T11 sidecar），复用零重算

设计原则（docs/plan-vlm-perception-layer.md §4.1）：
  1. 时间由确定性信号产生，语义由 VLM 产生；
  2. 图片不进 agent 主循环——VLM 调用只是本模块内部的一次性请求；
  3. 优雅降级：无 VLM key → 只有场景切分；某批打标失败 → 该批标 tag_failed；
     场景检测失败 → 按时长均匀切分兜底；
  4. 标签 schema 固定、与任务无关——缓存按文件生效，不能按问题定制。
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
from collections import Counter
from datetime import datetime

import ffmpeg_exec
import signal_detection
from model import get_vlm_model

SCENE_THRESHOLD = 0.3     # 场景切分灵敏度（roadmap T9 预定值）
MAX_SHOT_LEN = 12.0       # 超过则等分为多个子镜头（长镜头粒度兜底）
FRAME_EDGE = 640          # 代表帧长边像素（控 token）
JPEG_Q = 5                # ffmpeg -q:v（2~31，越小越清晰）
DETECT_TIMEOUT = 300      # 场景切分整段解码，放宽
EXTRACT_TIMEOUT = 60      # 单帧 -ss 抽取，很快
CARD_SCHEMA_VERSION = 2   # v2 = 增补 signals（静音/黑场，T10）


def _frame_budget() -> int:
    try:
        return max(1, int(os.environ.get("VLM_FRAME_BUDGET", "24")))
    except ValueError:
        return 24


def _batch_size() -> int:
    try:
        return max(1, int(os.environ.get("VLM_BATCH", "6")))
    except ValueError:
        return 6


# ------------------------------------------------------------ 进度钩子 ---- #

_hook = None


def set_progress_hook(fn):
    """注册进度回调 fn(dict)，返回旧值。dict 形如：
    {"stage": "start"|"sample"|"tag"|"done", "file": 名字, ...}
    web 端把它转成 NDJSON analysis 事件；CLI 打印；None = 不上报。
    """
    global _hook
    old = _hook
    _hook = fn
    return old


def _report(**fields):
    if _hook is not None:
        try:
            _hook(fields)
        except Exception:
            pass                     # 进度上报永远不影响分析本身


# --------------------------------------------------------- sidecar 缓存 -- #

def _sidecar_path(project_root: str, full_path: str, rel_name: str) -> str:
    """缓存键 = 文件名+大小+mtime（与 web._probe_file 同策略）。"""
    st = os.stat(full_path)
    raw = f"{rel_name}|{st.st_size}|{int(st.st_mtime)}"
    h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return os.path.join(project_root, "TMP", "probe", f"{h}.json")


def _load_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _save_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def load_cached_card(project_root: str, rel_path: str) -> dict | None:
    """读已缓存的内容卡片（/api/inputs 展示用）；没有或读不了返回 None。"""
    rel = (rel_path or "").replace("\\", "/")
    full = os.path.join(project_root, rel)
    if not rel.startswith("INPUT/") or not os.path.isfile(full):
        return None
    card = _load_json(_sidecar_path(project_root, full, rel))
    if card is None:
        return None
    card["cached"] = True
    return card


# ------------------------------------------------------------- 素材探测 ---- #

def _probe_meta(full_path: str) -> dict:
    """ffprobe 元数据收敛成 {ok, kind, duration, ...}（自探自足，不信模型转述）。"""
    res = ffmpeg_exec.probe(full_path)
    if not res.get("available") or not res.get("data"):
        return {"ok": False, "error": (res.get("error") or "ffprobe 不可用")[:200]}
    streams = res["data"].get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        if audio is None:
            return {"ok": False, "error": "既无视频轨也无音频轨"}
        return {"ok": True, "kind": "audio"}
    raw = (res["data"].get("format") or {}).get("duration") or video.get("duration")
    try:
        duration = round(float(raw), 3)
    except (TypeError, ValueError):
        duration = None              # 静态图片没有时长，正常
    return {
        "ok": True,
        "kind": "image" if duration is None else "video",
        "duration": duration,
        "width": video.get("width"),
        "height": video.get("height"),
        "has_audio": audio is not None,
    }


# --------------------------------------------------------- ⓪ 信号检测 ------ #

def _signals_block(run, full: str, duration, has_audio: bool) -> dict | None:
    """T10 静音/黑场检测 → 卡片的 signals 块；失败降级为 None（不影响镜头数据）。

    单次解码同时跑 blackdetect + silencedetect；末尾静音用总时长补闭合。
    """
    res = signal_detection.detect_av(
        full, has_audio=has_audio, total_duration=duration, run_fn=run)
    if not res.get("ok"):
        return None
    audio = None
    if res.get("silences") is not None:
        sil = res["silences"]
        ratio = round(sum(e - s for s, e in sil) / duration, 3) if duration else None
        audio = {"silences": sil, "silence_ratio": ratio}
    return {"audio": audio, "video": {"blacks": res.get("blacks") or []}}


def _augment_cached_card(card: dict, project_root: str, full: str, run) -> dict:
    """v1 卡片就地升级到 v2：只补跑信号检测，不重跑 VLM（省 token 零幻觉）。"""
    has_audio = _probe_audio_only(full)
    block = _signals_block(run, full, card.get("duration"), has_audio)
    if block is not None:
        card["signals"] = block
        card["degradations"] = sorted(set(card.get("degradations") or []) - {"signals_failed"})
    card["schema_version"] = CARD_SCHEMA_VERSION
    _save_json(_sidecar_path(project_root, full, card["source"]), card)
    return card


def _probe_audio_only(full: str) -> bool:
    """有没有音轨（升级旧卡用；失败当没有，宁可少报不误报）。"""
    res = ffmpeg_exec.probe(full)
    if not res.get("available") or not res.get("data"):
        return False
    streams = res["data"].get("streams") or []
    return any(s.get("codec_type") == "audio" for s in streams)


# --------------------------------------------------------- ① 场景切分 ---- #

_SHOWINFO_RE = re.compile(r"pts_time:(\d+(?:\.\d+)?)")


def parse_showinfo(stderr: str) -> list[float]:
    """从 showinfo 的 stderr 按序提取被 select 选中的帧时间戳（镜头起始点）。"""
    return [float(m.group(1)) for m in _SHOWINFO_RE.finditer(stderr or "")]


def _detect_boundaries(run, full: str, duration, budget: int) -> tuple[list[float], str]:
    """场景切分（select=gt(scene,0.3) + showinfo）；失败按时长均匀兜底。

    返回 (边界时间列表, "scene"|"uniform")。首帧 eq(n,0) 保证 0s 也是边界。
    """
    res = run(
        ["ffmpeg", "-nostdin", "-loglevel", "info", "-i", full,
         "-vf", f"select='eq(n,0)+gt(scene,{SCENE_THRESHOLD})',showinfo",
         "-f", "null", "-"],
        timeout=DETECT_TIMEOUT,
    )
    if res.get("ok"):
        times = parse_showinfo(res.get("stderr") or "")
        if times:
            return times, "scene"
    if not duration:
        return [0.0], "uniform"
    return uniform_boundaries(duration, budget), "uniform"


def uniform_boundaries(duration: float, budget: int) -> list[float]:
    """均匀切分兜底：粒度约每 5s 一个采样，总量不超过帧预算。"""
    n = max(1, min(budget, math.ceil(duration / 5)))
    return [round(i * duration / n, 3) for i in range(n)]


def build_shots(boundaries: list[float], duration, max_shot_len: float = MAX_SHOT_LEN) -> list[dict]:
    """把镜头起始点变成 [start, end] 区间；超长镜头等分加密粒度。

    duration 缺失时用最后边界 +1s 兜底——区间可能不准，但标签仍可用
    （调用方应已在降级标记里注明探测失败）。
    """
    b = sorted({round(max(0.0, t), 3) for t in boundaries if t >= 0})
    if not b or b[0] > 0.01:
        b = [0.0] + b
    end = duration if (duration and duration > b[-1]) else b[-1] + 1.0
    shots: list[dict] = []
    for i, s in enumerate(b):
        e = b[i + 1] if i + 1 < len(b) else end
        if e > s:
            shots.append({"start": s, "end": round(e, 3)})
    refined: list[dict] = []
    for sh in shots:
        span = sh["end"] - sh["start"]
        n = max(1, math.ceil(span / max_shot_len))      # 12s 一个粒度
        step = span / n
        for k in range(n):
            refined.append({
                "start": round(sh["start"] + k * step, 3),
                "end": round(sh["start"] + (k + 1) * step, 3),
            })
    return refined


def downsample(items: list, budget: int) -> list:
    """超预算时均匀抽样（保首尾，端点覆盖不丢）。"""
    if budget >= len(items):
        return items
    if budget <= 1:
        return items[:1]
    idx = [round(i * (len(items) - 1) / (budget - 1)) for i in range(budget)]
    out, seen = [], set()
    for i in idx:
        if i not in seen:
            seen.add(i)
            out.append(items[i])
    return out


# ----------------------------------------------------------- ② 抽帧 ---- #

def _extract_frame(run, full: str, t: float, out_jpg: str) -> bool:
    argv = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
            "-ss", f"{t:.3f}", "-i", full, "-frames:v", "1",
            "-vf", f"scale={FRAME_EDGE}:-2", "-q:v", str(JPEG_Q), out_jpg]
    res = run(argv, timeout=EXTRACT_TIMEOUT)
    return bool(res.get("ok")) and os.path.isfile(out_jpg) and os.path.getsize(out_jpg) > 0


def _data_url(jpg_path: str) -> str:
    with open(jpg_path, "rb") as f:
        return "data:image/jpeg;base64," + base64.b64encode(f.read()).decode("ascii")


# ------------------------------------------------------- ③ VLM 打标 ---- #

_TAG_PROMPT = (
    "你在给同一支视频里按时间顺序抽取的镜头代表帧打结构化标签（用于后续剪辑检索）。"
    "本次共 {n} 帧，序号 0 到 {n_1}，每帧对应一个镜头。"
    "对每一帧输出一个 JSON 对象，字段固定为：\n"
    "person_count（整数，画面人数，0=无人）、has_children（布尔，是否有儿童）、"
    "scene（场景，如 餐厅/海边/街道/家中/户外草地）、"
    "activity（活动，如 聚餐/合影/奔跑/讲话/表演/风景空镜）、"
    "mood（氛围，如 欢笑/温馨/安静/热闹）、"
    "tags（2-5 个补充标签的字符串数组，如 多人、举杯、夜景、日落、宠物）、"
    'quality（"good"|"ok"|"poor"，模糊/过曝/严重抖动为 poor）。\n'
    "只输出一个 JSON 数组（长度必须等于 {n}，顺序与帧一致），不要输出任何其他文字。"
    "标签语言用中文。严格禁止输出时间戳或时间信息——时间由系统另行管理。"
)


def _parse_tag_json(text: str) -> list | None:
    """容错解析模型输出（剥 ```json 围栏、截取首尾中括号）；不合法返回 None。"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    i, j = t.find("["), t.rfind("]")
    if i < 0 or j <= i:
        return None
    try:
        arr = json.loads(t[i:j + 1])
    except json.JSONDecodeError:
        return None
    return arr if isinstance(arr, list) else None


def sanitize_label(raw) -> dict:
    """把单帧标签收敛进固定 schema（防模型夹带字段 / 类型漂移）。"""
    lab = raw if isinstance(raw, dict) else {}

    def _s(v, max_len=24):
        return (str(v).strip() if v is not None else "")[:max_len]

    try:
        pc = max(0, int(lab.get("person_count") or 0))
    except (TypeError, ValueError):
        pc = 0
    tags = [str(x).strip()[:16] for x in (lab.get("tags") or []) if str(x).strip()][:5]
    quality = str(lab.get("quality") or "ok").lower()
    if quality not in ("good", "ok", "poor"):
        quality = "ok"
    return {
        "person_count": pc,
        "has_children": bool(lab.get("has_children")),
        "scene": _s(lab.get("scene")),
        "activity": _s(lab.get("activity")),
        "mood": _s(lab.get("mood")),
        "tags": tags,
        "quality": quality,
        "usable": quality != "poor",
    }


def _tag_once(model, frames_b64: list[str]) -> list | None:
    """一批帧发一次请求；任何异常都返回 None（由调用方决定重试/降级）。"""
    if not frames_b64:
        return []
    content: list[dict] = [{
        "type": "text",
        "text": _TAG_PROMPT.format(n=len(frames_b64), n_1=len(frames_b64) - 1),
    }]
    for u in frames_b64:
        content.append({"type": "image_url", "image_url": {"url": u}})
    try:
        resp = model.invoke([{"role": "user", "content": content}])
    except Exception:
        return None
    text = resp.content
    if not isinstance(text, str):      # content blocks 形式
        text = "\n".join(b.get("text", "") for b in text if isinstance(b, dict))
    return _parse_tag_json(text)


def tag_shots(shots: list[dict], frames_b64: list[str], model, name: str = "",
              batch_size: int | None = None) -> None:
    """就地给 shots[i] 写 label / tag_failed。每批失败重试一次，再失败只标记。

    frames_b64 与 shots 一一对应。永远不抛异常——打标失败是降级不是错误。
    批次间并发（V7 性能优化，参考 FireRed 实测：串行 VLM 是最大瓶颈，
    见其 docs/性能瓶颈分析与优化建议.md P0）：默认 4 路，环境变量
    VLM_CONCURRENCY 可调；进度按批号上报（乱序完成时单调递增）。
    """
    from concurrent.futures import ThreadPoolExecutor

    bs = batch_size or _batch_size()
    batches = [list(range(i, min(i + bs, len(shots))))
               for i in range(0, len(shots), bs)]
    _report(stage="tag", file=name, done=0, total=len(batches))

    def tag_batch(bi: int, idxs: list[int]):
        frames = [frames_b64[i] for i in idxs]
        labels = _tag_once(model, frames)
        if labels is None:
            labels = _tag_once(model, frames)       # 重试一次
        return bi, idxs, labels

    try:
        workers = max(1, int(os.environ.get("VLM_CONCURRENCY", "4")))
    except ValueError:
        workers = 4
    workers = min(workers, max(1, len(batches)))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        # as_completed：进度条按完成数递增（不受批间快慢影响）
        pending = [ex.submit(tag_batch, bi, idxs)
                   for bi, idxs in enumerate(batches)]
        done = 0
        by_batch: dict[int, tuple] = {}
        for fut in _as_completed(pending):
            bi, idxs, labels = fut.result()
            by_batch[bi] = (idxs, labels)
            done += 1
            _report(stage="tag", file=name, done=done, total=len(batches))
        for bi in sorted(by_batch):
            idxs, labels = by_batch[bi]
            if labels is None or len(labels) != len(idxs):
                for i in idxs:
                    shots[i]["tag_failed"] = True
                continue
            for k, i in enumerate(idxs):
                shots[i]["label"] = sanitize_label(labels[k])
    _report(stage="tag", file=name, done=len(batches), total=len(batches))


def _as_completed(futures):
    """ThreadPool futures 的完成迭代器（避免直接依赖 concurrent.futures.as_completed
    的导入位置差异；None 兜底保证测试环境缺库时仍可串行退化）。"""
    try:
        from concurrent.futures import as_completed
        return as_completed(futures)
    except ImportError:            # pragma: no cover
        return (f for f in futures)


def build_summary(shots: list[dict], duration, signals: dict | None = None) -> str:
    """规则式摘要（确定性、零成本、可测）：镜头数 + 高频场景/活动 + 多人数。"""
    parts = [f"共 {len(shots)} 个镜头" + (f"/{duration:g}s" if duration else "")]
    labeled = [s.get("label") or {} for s in shots]
    if signals:
        ratio = (signals.get("audio") or {}).get("silence_ratio")
        if ratio is not None and ratio >= 0.15:
            parts.append(f"静音占比 {ratio:.0%}")
    if not any(labeled):
        return parts[0] + "；未打语义标签"
    for key, label in (("scene", "场景"), ("activity", "活动")):
        cnt = Counter(l[key] for l in labeled if l.get(key))
        if cnt:
            top = "、".join(f"{k}×{v}" for k, v in cnt.most_common(3))
            parts.append(f"{label}：{top}")
    multi = sum(1 for l in labeled if l.get("person_count", 0) >= 2)
    if multi:
        parts.append(f"多人镜头 {multi} 个")
    poor = sum(1 for l in labeled if l.get("quality") == "poor")
    if poor:
        parts.append(f"画质差 {poor} 个")
    return "；".join(parts)


# ----------------------------------------------------------- 编排入口 ---- #

def analyze_media(path: str, project_root: str, *, model=None, run_fn=None,
                  probe_fn=None) -> dict:
    """对单个素材做内容分析，返回内容卡片 dict（sidecar 命中时零计算）。

    依赖注入（离线测试用）：model（假 VLM；传 False = 强制禁用走降级路径）/
    run_fn（假 ffmpeg）/ probe_fn（假 ffprobe）。失败从不抛出——返回
    {"error": ...} 或带 degradations 的降级卡片，保证 analyze_media 工具
    永远有结构化返回。
    """
    rel = (path or "").replace("\\", "/").strip()
    full = os.path.join(project_root, rel)
    name = rel.rsplit("/", 1)[-1]
    if not rel.startswith("INPUT/") or not os.path.isfile(full):
        return {"error": f"素材不存在：{rel}（内容分析只接受 INPUT/ 下的文件）"}
    run = run_fn or ffmpeg_exec.run
    probe = probe_fn or _probe_meta
    budget = _frame_budget()

    card = _load_json(_sidecar_path(project_root, full, rel))
    if card is not None:
        # v1 卡就地升级到 v2（只补信号检测，不重跑 VLM）
        if card.get("schema_version", 0) < CARD_SCHEMA_VERSION and card.get("duration"):
            _report(stage="sample", file=name, upgrade=True)
            card = _augment_cached_card(card, project_root, full, run)
        card["cached"] = True
        _report(stage="done", file=name, cached=True,
                shots=len(card.get("shots") or []),
                summary=card.get("summary") or "")
        return card

    meta = probe(full)
    if not meta.get("ok"):
        return {"error": f"探测失败：{meta.get('error') or '未知原因'}"}
    if meta.get("kind") == "audio":
        return {"error": "纯音频素材没有画面，无需内容分析（只能作 BGM）。"}

    _report(stage="start", file=name, kind=meta.get("kind"))
    degradations: list[str] = []
    duration = meta.get("duration")

    # ⓪ A/V 信号（T10）：静音/黑场区间——确定性、与镜头切分并行独立，
    #    失败只记降级不阻塞（镜头/标签仍是可用索引）
    signals = None
    if meta.get("kind") == "video":
        signals = _signals_block(run, full, duration, bool(meta.get("has_audio")))
        if signals is None:
            degradations.append("signals_failed")

    # ① 时间边界：场景切分（确定性）；失败/过疏按时长均匀兜底
    if meta.get("kind") == "image":
        shots: list[dict] = [{"start": 0.0, "end": None}]
    else:
        boundaries, how = _detect_boundaries(run, full, duration, budget)
        if how == "uniform":
            degradations.append("uniform_fallback")
        shots = downsample(build_shots(boundaries, duration), budget)

    # ② 代表帧抽取（每镜头 0.35 分位处，长边 640px JPEG）
    frames_dir = os.path.join(project_root, "TMP", "probe", "frames")
    os.makedirs(frames_dir, exist_ok=True)
    tag_prefix = hashlib.sha1(
        f"{rel}|{os.path.getmtime(full)}".encode("utf-8")).hexdigest()[:12]
    frames_b64: list[str] = []
    kept: list[dict] = []
    for i, sh in enumerate(shots):
        span = (sh["end"] - sh["start"]) if sh["end"] is not None else 0.0
        jpg_name = f"{tag_prefix}_{i:03d}.jpg"
        jpg = os.path.join(frames_dir, jpg_name)
        if _extract_frame(run, full, sh["start"] + span * 0.35, jpg):
            frames_b64.append(_data_url(jpg))
            kept.append({**sh, "frame": jpg_name})   # frame = 前端可请求的缩略图名
    shots = kept
    _report(stage="sample", file=name, shots=len(shots))
    if not shots:
        return {"error": "抽帧全部失败（ffmpeg 解码异常），无法建立内容索引。"}

    # ③ VLM 批量打标（无 key → 跳过，纯 L1 信号；model=False 强制禁用）
    if model is False:
        vlm = None
    else:
        vlm = model if model is not None else get_vlm_model()
    vlm_name = None
    if vlm is None:
        degradations.append("vlm_unavailable")
    else:
        vlm_name = getattr(vlm, "model_name", None) or os.environ.get("VLM_MODEL", "")
        tag_shots(shots, frames_b64, vlm, name=name)
        if any(s.get("tag_failed") for s in shots):
            degradations.append("tag_partial")

    card = {
        "schema_version": CARD_SCHEMA_VERSION,
        "source": rel,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "vlm_model": vlm_name,
        "duration": duration,
        "frame_budget": budget,
        "degradations": degradations,
        "signals": signals,
        "summary": build_summary(shots, duration, signals),
        "shots": shots,
    }
    _save_json(_sidecar_path(project_root, full, rel), card)
    card["cached"] = False
    _report(stage="done", file=name, cached=False, shots=len(shots),
            summary=card["summary"])
    return card
