"""全链路耗时埋点（per-task / per-stage perf trace）。

动机（2026-11-01）：性能优化阶段每次看耗时分布都要开会话、翻 LangSmith 或
跑 TMP/_timed_run.py 客户端脚本，效率低。这里在服务端进程内埋点，每次任务
（/api/run、/api/replan、上传预热）自动落盘一份耗时报告：

    TMP/perf/<时间>_<会话前8>_t<轮次>_<origin>.json   完整 span 瀑布数据
    同名 .txt                                          人读摘要（阶段表 + 聚合）

观察性能瓶颈只需打开最新 .txt。用法见 docs/perf-instrumentation.md。

设计原则：
- 观测绝不影响主链路。无活动 trace 时所有调用 no-op（各模块的 ImportError
  兜底 shim 亦同）；写盘/汇总异常静默吞掉。
- flat span 列表，无父子指针：跨线程（VLM 批并发、ffmpeg 批并行）天然安全，
  嵌套关系按「区间包含」事后推导，独占时间 = stage 时长 − 与之重叠的
  llm/vlm/ffmpeg span 的合并区间和。
- trace 靠 contextvars 随线程流动。RUN_LOCK 保证同一时刻只有一个任务；
  上传预热（prewarm）与任务并发时按线程天然隔离。例外：ThreadPoolExecutor
  不继承 contextvars，两处 submit 点（content_analysis.tag_shots、
  plan_compiler.execute）显式 copy_context() 传播。
- span kind：stage（环节）/ llm（主模型请求，含 TTFT）/ vlm（打标单批）/
  ffmpeg（单条子进程，ffprobe 同类）。同一次 VLM 请求会产生 vlm span 和其
  内嵌的 llm span，汇总时按 kind 分开、独占时间按区间合并不会重复扣减。

开关：环境变量 PERF_TRACE（默认 "1"），置 "0" 完全禁用（start 不建 trace）。
"""

from __future__ import annotations

import contextvars
import json
import os
import re
import sys
import threading
import time
from collections import Counter
from contextlib import contextmanager

_ENABLED = os.environ.get("PERF_TRACE", "1") != "0"

# 当前活动 trace（随线程/上下文流动；无 trace 时取值为 None）
_current: "contextvars.ContextVar[PerfTrace | None]" = contextvars.ContextVar(
    "perf_trace_current", default=None)

# spans 追加锁：VLM 批 / ffmpeg 并行工作线程都会追加。追加是 O(1)，竞争可忽略。
_LOCK = threading.Lock()


class _SpanCtx:
    """trace.span(name) 返回的 context manager。

    用法：`with trace.span("agent_loop", kind="stage") as s: ...`；
    s.meta 可在块内续写（如分析完才知道 cached 与否）。异常不吞（恒返回
    False），但 span 照常记账并标记 meta.error。
    """

    __slots__ = ("trace", "name", "kind", "meta", "t0", "started_at")

    def __init__(self, trace: "PerfTrace", name: str, kind: str, meta: dict):
        self.trace = trace
        self.name = name
        self.kind = kind
        self.meta = meta
        self.t0 = 0.0           # perf_counter 起点（TTFT 等相对量用它）
        self.started_at = 0.0   # epoch 起点（跨 trace 瀑布对齐用它）

    def __enter__(self) -> "_SpanCtx":
        self.t0 = time.perf_counter()
        self.started_at = time.time()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            self.meta["error"] = exc_type.__name__
        rec = {
            "name": self.name,
            "kind": self.kind,
            "t_start": round(self.started_at, 4),
            "dur_s": round(time.perf_counter() - self.t0, 4),
            "thread": threading.current_thread().name,
            "meta": self.meta,
        }
        with _LOCK:
            self.trace.spans.append(rec)
        return False


class PerfTrace:
    """一次任务/预热的完整计时记录。由 start() 创建并挂到 contextvar。"""

    def __init__(self, origin: str, session_id=None, **ctx):
        self.origin = origin            # run | replan | prewarm
        self.session_id = session_id
        self.ctx = dict(ctx)            # task / preview / file / turn 等自由字段
        self.started_at = time.time()
        self.spans: list[dict] = []
        self.events: list[dict] = []    # 瞬时点（不占时长的标记）
        self.error: str | None = None
        self.file: str | None = None    # finish() 落盘的 json 路径（回填）

    # ---- 记录 ----

    def span(self, name: str, kind: str = "stage", **meta) -> _SpanCtx:
        return _SpanCtx(self, name, kind, meta)

    def event(self, name: str, **meta) -> None:
        with _LOCK:
            self.events.append({"name": name, "t": round(time.time(), 4), **meta})

    def set_error(self, text: str) -> None:
        if self.error is None and text:
            self.error = str(text)[:300]

    # ---- 收尾 ----

    def report(self) -> dict:
        spans = sorted(self.spans, key=lambda s: s["t_start"])
        return {
            "version": 1,
            "origin": self.origin,
            "session_id": self.session_id,
            **self.ctx,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                        time.localtime(self.started_at)),
            "total_s": round(time.time() - self.started_at, 3),
            "error": self.error,
            "summary": _build_summary(spans),
            "spans": spans,
            "events": list(self.events),
        }

    def finish(self, out_dir: str | None = None,
               print_summary: bool = True) -> dict:
        """生成报告并落盘（json + txt）。失败抛出——由 finish_current 兜底。"""
        rep = self.report()
        out_dir = out_dir or os.path.join(_PROJECT_ROOT, "TMP", "perf")
        os.makedirs(out_dir, exist_ok=True)
        base = _report_basename(self)
        json_path = os.path.join(out_dir, base + ".json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, default=str)
        txt_path = os.path.join(out_dir, base + ".txt")
        text = text_summary(rep)
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        self.file = json_path
        rep["file"] = json_path
        if print_summary:
            try:
                sys.stderr.write(text + "\n")
            except OSError:
                pass
        return rep


_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def _report_basename(tr: PerfTrace) -> str:
    sid = re.sub(r"[^A-Za-z0-9_-]", "_", str(tr.session_id or "nosid"))[:8]
    turn = tr.ctx.get("turn") or 0
    ms = int((tr.started_at % 1) * 1000)   # 同秒多条报告（多素材预热）不重名
    ts = time.strftime("%m%d_%H%M%S", time.localtime(tr.started_at))
    return f"{ts}_{ms:03d}_{sid}_t{turn}_{tr.origin}"


# --------------------------------------------------------------------------- #
# 汇总：阶段独占时间 + 按 kind 聚合
# --------------------------------------------------------------------------- #

def _merge_intervals(intervals: list[tuple[float, float]]) -> list[list[float]]:
    out: list[list[float]] = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _build_summary(spans: list[dict]) -> dict:
    leaves = [s for s in spans if s["kind"] != "stage"]
    stages = []
    for s in (x for x in spans if x["kind"] == "stage"):
        s0, s1 = s["t_start"], s["t_start"] + s["dur_s"]
        ivs = [(max(s0, c["t_start"]), min(s1, c["t_start"] + c["dur_s"]))
               for c in leaves
               if c["t_start"] < s1 and c["t_start"] + c["dur_s"] > s0]
        busy = sum(b - a for a, b in _merge_intervals(ivs))
        stages.append({
            "name": s["name"],
            "dur_s": s["dur_s"],
            # 独占时间：扣掉与 llm/vlm/ffmpeg 重叠的部分（并发子 span 按合并
            # 区间扣，不会重复扣减；嵌套 stage 不扣——agent_loop 的独占就是
            # 「除 LLM/打标/渲染外的自身开销」）
            "exclusive_s": round(max(0.0, s["dur_s"] - busy), 4),
            "meta": s["meta"],
        })

    def _agg(kind: str) -> dict | None:
        ss = [s for s in spans if s["kind"] == kind]
        if not ss:
            return None
        slow = max(ss, key=lambda s: s["dur_s"])
        out: dict = {
            "count": len(ss),
            "total_s": round(sum(s["dur_s"] for s in ss), 3),
            "slowest": {"name": slow["name"], "dur_s": slow["dur_s"],
                        "meta": slow["meta"]},
        }
        if kind == "llm":
            ttfts = [s["meta"]["ttft_s"] for s in ss
                     if isinstance(s.get("meta"), dict)
                     and "ttft_s" in s["meta"]]
            if ttfts:
                out["ttft_avg_s"] = round(sum(ttfts) / len(ttfts), 3)
        if kind == "ffmpeg":
            out["by_tool"] = dict(Counter(s["name"] for s in ss))
        return out

    return {"stages": stages, "llm": _agg("llm"),
            "vlm": _agg("vlm"), "ffmpeg": _agg("ffmpeg")}


def _fmt_dur(sec: float) -> str:
    return f"{sec:.1f}s" if sec >= 10 else f"{sec:.2f}s"


def text_summary(rep: dict) -> str:
    """人读摘要（.txt 内容 / 服务端控制台打印）。"""
    lines = [
        f"== perf [{rep['origin']}] sid={str(rep.get('session_id') or 'nosid')[:8]}"
        f" turn={rep.get('turn', 0)} total={_fmt_dur(rep['total_s'])}"
        f" ({rep['started_at']}) =="
    ]
    if rep.get("origin") == "prewarm" and rep.get("file"):
        lines.append(f"file: {rep['file']}")
    elif rep.get("task"):
        lines.append(f"task: {str(rep['task'])[:80]}")
    if rep.get("error"):
        lines.append(f"error: {rep['error']}")
    summ = rep["summary"]
    stages = summ.get("stages") or []
    if stages:
        lines.append("stages (总耗时 / 独占):")
        for st in stages:
            lines.append(f"  {st['name']:<30} {_fmt_dur(st['dur_s']):>9}"
                         f" / {_fmt_dur(st['exclusive_s']):>9}")
    for kind, label in (("llm", "LLM"), ("vlm", "VLM"), ("ffmpeg", "ffmpeg")):
        agg = summ.get(kind)
        if not agg:
            continue
        extra = ""
        if kind == "llm" and "ttft_avg_s" in agg:
            extra = f"  ttft avg {_fmt_dur(agg['ttft_avg_s'])}"
        slow = agg["slowest"]
        lines.append(f"{label} ×{agg['count']}  total {_fmt_dur(agg['total_s'])}"
                     f"{extra}  slowest: {slow['name']} {_fmt_dur(slow['dur_s'])}")
    leaves = sorted((s for s in rep["spans"] if s["kind"] != "stage"),
                    key=lambda s: s["dur_s"], reverse=True)[:10]
    if leaves:
        lines.append("slowest spans:")
        for s in leaves:
            meta = json.dumps(s.get("meta") or {}, ensure_ascii=False,
                              default=str)[:120]
            lines.append(f"  {_fmt_dur(s['dur_s']):>9}  [{s['kind']}]"
                         f" {s['name']}  {meta}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 模块级入口（无活动 trace 时全部 no-op）
# --------------------------------------------------------------------------- #

def start(origin: str, session_id=None, **ctx) -> "PerfTrace | None":
    """开一条 trace 并挂到当前上下文。PERF_TRACE=0 时返回 None。"""
    if not _ENABLED:
        return None
    tr = PerfTrace(origin, session_id, **ctx)
    _current.set(tr)
    return tr


def current() -> "PerfTrace | None":
    return _current.get() if _ENABLED else None


def set_ctx(**kv) -> None:
    """中途补记上下文字段（如会话载入后才知道 turn）。"""
    tr = _current.get()
    if tr is not None:
        tr.ctx.update(kv)


def finish_current(out_dir: str | None = None,
                   print_summary: bool = True) -> "dict | None":
    """收尾当前 trace 并落盘；写盘失败静默（观测不能影响主链路）。"""
    tr = _current.get()
    if tr is None:
        return None
    _current.set(None)
    try:
        return tr.finish(out_dir=out_dir, print_summary=print_summary)
    except Exception:       # noqa: BLE001
        return None


@contextmanager
def _null_span():
    yield None


def span(name: str, kind: str = "stage", **meta):
    """模块级便捷入口：无活动 trace 时返回 no-op context。"""
    tr = _current.get()
    if tr is None:
        return _null_span()
    return tr.span(name, kind, **meta)


def event(name: str, **meta) -> None:
    tr = _current.get()
    if tr is not None:
        tr.event(name, **meta)
