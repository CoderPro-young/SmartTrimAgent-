"""perf_trace 埋点模块单测（span 记账 / 独占时间 / 线程隔离 / 咽喉点 smoke）。

运行：.venv/Scripts/python.exe tests/test_perf_trace.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import traceback
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "video_editing"))

import perf_trace   # noqa: E402


def _cleanup():
    """测试间清理：确保上下文里没有残留 trace（否则串测）。"""
    perf_trace._current.set(None)


def _tmpdir() -> str:
    return tempfile.mkdtemp(prefix="perf_test_")


# --------------------------------------------------------------------------- #
# 基础记账
# --------------------------------------------------------------------------- #

def test_span_records_duration_meta_and_error():
    tr = perf_trace.start("run", session_id="t-span", task="测试任务")
    assert tr is not None
    with tr.span("s1", kind="stage") as s:
        s.meta["k"] = 1
        time.sleep(0.02)
    try:
        with tr.span("s2", kind="stage"):
            raise ValueError("boom")
    except ValueError:
        pass
    rep = tr.finish(out_dir=_tmpdir(), print_summary=False)
    names = [x["name"] for x in rep["spans"]]
    assert names == ["s1", "s2"], names
    s1, s2 = rep["spans"]
    assert s1["dur_s"] >= 0.015 and s1["meta"]["k"] == 1
    assert s2["meta"]["error"] == "ValueError"          # 异常 span 照常记账
    assert rep["error"] is None                          # span 异常不算 trace 级错误
    assert rep["task"] == "测试任务"


def test_event_and_set_ctx():
    tr = perf_trace.start("run", session_id="t-ctx")
    perf_trace.set_ctx(turn=3)
    perf_trace.event("llm_first_token", model="m1")
    rep = tr.finish(out_dir=_tmpdir(), print_summary=False)
    assert rep["turn"] == 3
    assert rep["events"] and rep["events"][0]["name"] == "llm_first_token"


def test_noop_without_trace():
    _cleanup()
    with perf_trace.span("x", kind="stage"):    # 无 trace 时 no-op，不报错
        pass
    perf_trace.event("y")
    assert perf_trace.finish_current() is None
    assert perf_trace.current() is None


def test_finish_writes_json_and_txt():
    tr = perf_trace.start("prewarm", session_id=None, file="INPUT/a.mp4")
    with tr.span("analyze_media", kind="stage", src="a.mp4") as s:
        s.meta["cached"] = False
    rep = tr.finish(out_dir=_tmpdir(), print_summary=False)
    assert os.path.isfile(rep["file"]) and rep["file"].endswith(".json")
    txt = rep["file"][:-5] + ".txt"
    assert os.path.isfile(txt)
    data = json.load(open(rep["file"], encoding="utf-8"))
    assert data["origin"] == "prewarm"
    assert data["summary"]["stages"][0]["name"] == "analyze_media"
    assert "perf [prewarm]" in open(txt, encoding="utf-8").read()


def test_disabled_env_kills_start(monkeypatch=None):
    _cleanup()
    old = perf_trace._ENABLED
    try:
        perf_trace._ENABLED = False
        assert perf_trace.start("run") is None
        assert perf_trace.current() is None
    finally:
        perf_trace._ENABLED = old


# --------------------------------------------------------------------------- #
# 汇总数学
# --------------------------------------------------------------------------- #

def test_exclusive_time_merges_concurrent_children():
    """独占时间 = stage 时长 − 非 stage 子 span 的合并区间交叠。
    构造：stage 0→10；llm 2→6；llm2 3→5（与 llm 完全嵌套，模拟并发双计
    防护）；ffmpeg 7→9；vlm 9.5→11.5（尾部越界，只扣 0.5）。
    期望独占 = 10 − (4 + 2 + 0.5) = 3.5。"""
    spans = [
        {"name": "stage_a", "kind": "stage", "t_start": 0.0, "dur_s": 10.0,
         "thread": "m", "meta": {}},
        {"name": "llm", "kind": "llm", "t_start": 2.0, "dur_s": 4.0,
         "thread": "m", "meta": {}},
        {"name": "llm2", "kind": "llm", "t_start": 3.0, "dur_s": 2.0,
         "thread": "w1", "meta": {}},
        {"name": "ffmpeg", "kind": "ffmpeg", "t_start": 7.0, "dur_s": 2.0,
         "thread": "m", "meta": {}},
        {"name": "vlm", "kind": "vlm", "t_start": 9.5, "dur_s": 2.0,
         "thread": "w2", "meta": {}},
    ]
    summ = perf_trace._build_summary(spans)
    a = summ["stages"][0]
    assert abs(a["exclusive_s"] - 3.5) < 1e-6, a
    assert summ["llm"]["count"] == 2 and abs(summ["llm"]["total_s"] - 6.0) < 1e-6
    assert summ["ffmpeg"]["count"] == 1
    assert summ["vlm"]["slowest"]["name"] == "vlm"


def test_llm_ttft_aggregate():
    spans = [
        {"name": "llm", "kind": "llm", "t_start": 0.0, "dur_s": 5.0,
         "thread": "m", "meta": {"ttft_s": 1.0}},
        {"name": "llm", "kind": "llm", "t_start": 9.0, "dur_s": 3.0,
         "thread": "m", "meta": {"ttft_s": 3.0}},
        {"name": "llm", "kind": "llm", "t_start": 20.0, "dur_s": 1.0,
         "thread": "m", "meta": {}},       # 非 streaming，无 ttft
    ]
    summ = perf_trace._build_summary(spans)
    assert abs(summ["llm"]["ttft_avg_s"] - 2.0) < 1e-6


# --------------------------------------------------------------------------- #
# 线程隔离
# --------------------------------------------------------------------------- #

def test_thread_isolation_between_traces():
    """两个线程各开各的 trace（模拟 run 与 prewarm 并发），span 不串。"""
    results: dict[str, list] = {}

    def worker(tag: str):
        tr = perf_trace.start("run", session_id=tag)
        with tr.span(f"span_{tag}", kind="stage"):
            time.sleep(0.01)
        results[tag] = [x["name"] for x in tr.spans]
        tr.finish(out_dir=_tmpdir(), print_summary=False)

    threads = [threading.Thread(target=worker, args=(t,)) for t in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results["A"] == ["span_A"] and results["B"] == ["span_B"]


def test_pool_worker_ctx_propagation_pattern():
    """ThreadPoolExecutor 不继承 contextvars；submit 点 copy_context 传播后
    工作线程的 span 落进当前 trace（与 content_analysis/_execute_impl 同款）。"""
    import contextvars
    from concurrent.futures import ThreadPoolExecutor

    tr = perf_trace.start("run", session_id="t-pool")
    seen: list[str] = []
    lock = threading.Lock()

    def work(i: int):
        with perf_trace.span(f"w{i}", kind="ffmpeg"):
            with lock:
                seen.append(str(i))

    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = []
        for i in range(4):
            ctx = contextvars.copy_context()
            futs.append(ex.submit(ctx.run, work, i))
        for f in futs:
            f.result()

    rep = tr.finish(out_dir=_tmpdir(), print_summary=False)
    names = sorted(x["name"] for x in rep["spans"])
    assert names == [f"w{i}" for i in range(4)], names


# --------------------------------------------------------------------------- #
# 咽喉点 smoke
# --------------------------------------------------------------------------- #

def test_ffmpeg_exec_records_span():
    import ffmpeg_exec
    tr = perf_trace.start("run", session_id="t-ff")
    res = ffmpeg_exec._run_tracked(
        [sys.executable, "-c", "print('ok')"], 10, None)
    assert res["ok"] is True
    rep = tr.finish(out_dir=_tmpdir(), print_summary=False)
    spans = [x for x in rep["spans"] if x["kind"] == "ffmpeg"]
    assert len(spans) == 1
    assert spans[0]["name"].startswith("python")
    assert spans[0]["meta"]["ok"] is True and spans[0]["meta"]["rc"] == 0


def test_model_stream_records_llm_span():
    import model
    from langchain_core.messages import AIMessageChunk
    from langchain_core.outputs import ChatGenerationChunk
    from langchain_openai import ChatOpenAI

    tr = perf_trace.start("run", session_id="t-llm")
    # 流式真实产出的是 ChatGenerationChunk（文本在 .message 上）——
    # 2026-11-01 实测：用 AIMessageChunk 当假数据会漏掉 .content 直读的坑
    chunks = [ChatGenerationChunk(message=AIMessageChunk(content="he")),
              ChatGenerationChunk(message=AIMessageChunk(content="llo"))]
    chat = model.StreamingChatOpenAI(model="test-model", api_key="sk-test",
                                     streaming=True)
    with mock.patch.object(ChatOpenAI, "_stream",
                           return_value=iter(chunks)) as m:
        out = list(chat._stream([{"role": "user", "content": "hi"}]))
    assert m.called and len(out) == 2
    rep = tr.finish(out_dir=_tmpdir(), print_summary=False)
    llm = [x for x in rep["spans"] if x["kind"] == "llm"]
    assert len(llm) == 1
    meta = llm[0]["meta"]
    assert meta["model"] == "test-model" and meta["streaming"] is True
    assert meta["n_chunks"] == 2 and meta["out_chars"] == 5
    assert "ttft_s" in meta


def test_model_generate_nonstream_records_span():
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_openai import ChatOpenAI
    import model

    tr = perf_trace.start("run", session_id="t-gen")
    chat = model.StreamingChatOpenAI(model="test-model", api_key="sk-test",
                                     streaming=False)
    fake = ChatResult(generations=[ChatGeneration(
        message=AIMessage(content="answer"))])
    with mock.patch.object(ChatOpenAI, "_generate", return_value=fake):
        res = chat._generate([{"role": "user", "content": "q"}])
    assert res.generations[0].message.content == "answer"
    rep = tr.finish(out_dir=_tmpdir(), print_summary=False)
    llm = [x for x in rep["spans"] if x["kind"] == "llm"]
    assert len(llm) == 1 and llm[0]["meta"]["streaming"] is False


def test_no_trace_model_and_ffmpeg_still_work():
    """无活动 trace 时咽喉点行为不变（观测零侵入的回归护栏）。"""
    _cleanup()
    import ffmpeg_exec
    res = ffmpeg_exec._run_tracked([sys.executable, "-c", "print('ok')"], 10, None)
    assert res["ok"] is True

    import model
    from langchain_core.messages import AIMessageChunk
    from langchain_openai import ChatOpenAI
    chat = model.StreamingChatOpenAI(model="m", api_key="k", streaming=True)
    with mock.patch.object(ChatOpenAI, "_stream",
                           return_value=iter([AIMessageChunk(content="ok")])):
        out = list(chat._stream([{"role": "user", "content": "q"}]))
    assert len(out) == 1


# --------------------------------------------------------------------------- #
# 手动 runner（与仓库其他测试同款：可直接 python 运行）
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            _cleanup()
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:   # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
        finally:
            _cleanup()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
