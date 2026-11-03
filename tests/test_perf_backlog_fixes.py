"""perf 待办首批修复的单测（V1/V2/V3/A2）。

运行：.venv/Scripts/python.exe tests/test_perf_backlog_fixes.py

覆盖：
- V1  VLM 请求体带 enable_thinking:false（extra_body 通道）
- V2  analyze_media 在途去重：同素材并发只跑一遍管线，后来者命中缓存
- V3  tag_shots 默认并发 2（VLM_CONCURRENCY 未设置时）
- A2  感知层并发上限：多素材并行 analyze_media 被信号量限流（默认 2）
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "video_editing"))

import content_analysis as ca   # noqa: E402
import model                    # noqa: E402


def _fake_probe(full):
    return {"ok": True, "kind": "video", "duration": 3.0, "has_audio": True,
            "width": 640, "height": 360}


def _make_env(tmp: str, name: str) -> str:
    os.makedirs(os.path.join(tmp, "INPUT"), exist_ok=True)
    full = os.path.join(tmp, "INPUT", name)
    with open(full, "wb") as f:
        f.write(b"\x00\x00\x00\x18ftypmp42fake")
    return full


def _no_transnet(*_a, **_k):
    return None


# --------------------------------------------------------------------------- #
# V1
# --------------------------------------------------------------------------- #

def test_v1_vlm_disables_thinking():
    vlm = model.get_vlm_model()
    assert vlm is not None, "本机应已配 VLM（SiliconFlow key）"
    body = getattr(vlm, "extra_body", None) or (getattr(vlm, "model_kwargs", None) or {})
    assert body.get("enable_thinking") is False, f"extra_body={body}"
    assert model.VLM_THINKING is False
    assert body.get("max_tokens") == model.VLM_MAX_TOKENS


def test_main_model_disables_thinking():
    """主模型默认关思考（2026-11-03 定案）；MODEL_THINKING=1 可开回。"""
    m = model.get_model()
    body = getattr(m, "extra_body", None) or {}
    assert body.get("enable_thinking") is False, f"extra_body={body}"
    assert model.MODEL_THINKING is False
    # 开关翻转生效（get_model 读模块级常量，可运行时覆盖）
    old = model.MODEL_THINKING
    try:
        model.MODEL_THINKING = True
        assert model.get_model().extra_body.get("enable_thinking") is True
    finally:
        model.MODEL_THINKING = old


# --------------------------------------------------------------------------- #
# V3
# --------------------------------------------------------------------------- #

def test_v3_tag_shots_default_two_workers():
    saved_env = os.environ.pop("VLM_CONCURRENCY", None)
    active = {"n": 0, "max": 0}
    lock = threading.Lock()
    saved_tag = ca._tag_once

    def slow_tag(m, frames):
        with lock:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
        time.sleep(0.15)
        with lock:
            active["n"] -= 1
        return [{"scene": "测试"}] * len(frames)

    ca._tag_once = slow_tag
    try:
        shots = [{"start": float(i), "end": float(i + 1)} for i in range(6)]
        ca.tag_shots(shots, ["data:x"] * 6, object(), name="v3", batch_size=1)
        assert all(s.get("label", {}).get("scene") == "测试" for s in shots)
        assert 1 <= active["max"] <= 2, f"并发上限失效：max={active['max']}"
    finally:
        ca._tag_once = saved_tag
        if saved_env is not None:
            os.environ["VLM_CONCURRENCY"] = saved_env


# --------------------------------------------------------------------------- #
# V2：同素材并发只分析一次
# --------------------------------------------------------------------------- #

def test_v2_inflight_dedup_same_file():
    tmp = tempfile.mkdtemp(prefix="v2_dedup_")
    _make_env(tmp, "dedup_case.mp4")
    null_calls: list[list] = []
    lock = threading.Lock()

    def fake_run(argv, timeout=None, cwd=None):
        a = list(argv)
        if "-f" in a and "null" in a:                  # 信号/切分探测
            with lock:
                first = len(null_calls) == 0
                null_calls.append(a)
            if first:
                time.sleep(0.5)                        # owner 慢半拍，留等待窗口
            return {"ok": False, "returncode": 1, "stdout": "",
                    "stderr": "fake-fail", "command": a}
        with open(a[-1], "wb") as f:                   # 抽帧：写真 jpg
            f.write(b"\xff\xd8FAKEJPEG")
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": "", "command": a}

    results: dict[str, dict] = {}

    def worker(tag: str):
        results[tag] = ca.analyze_media(
            "INPUT/dedup_case.mp4", tmp, model=False, run_fn=fake_run,
            probe_fn=_fake_probe, transnet_hook=_no_transnet)

    t1 = threading.Thread(target=worker, args=("A",))
    t1.start()
    time.sleep(0.15)                                   # 确保 A 先持有在途锁
    t2 = threading.Thread(target=worker, args=("B",))
    t2.start()
    t1.join(30)
    t2.join(30)

    assert "error" not in results["A"], results["A"]
    assert results["B"].get("cached") is True, "后来者应命中缓存"
    assert not results["A"].get("cached")
    assert len(null_calls) == 2, f"管线应只跑一遍（探测+切分各1）：{len(null_calls)}"


# --------------------------------------------------------------------------- #
# A2：感知层并发上限
# --------------------------------------------------------------------------- #

def test_a2_analyze_parallelism_capped():
    if int(os.environ.get("ANALYZE_PARALLELISM", "2")) != 2:
        print("SKIP（ANALYZE_PARALLELISM 被环境覆盖）")
        return
    tmp = tempfile.mkdtemp(prefix="a2_cap_")
    for i in range(4):
        _make_env(tmp, f"cap_{i}.mp4")
    active = {"n": 0, "max": 0}
    lock = threading.Lock()

    def fake_run(argv, timeout=None, cwd=None):
        a = list(argv)
        if "-f" in a and "null" in a:
            with lock:
                active["n"] += 1
                active["max"] = max(active["max"], active["n"])
            time.sleep(0.3)
            with lock:
                active["n"] -= 1
            return {"ok": False, "returncode": 1, "stdout": "",
                    "stderr": "fake-fail", "command": a}
        with open(a[-1], "wb") as f:
            f.write(b"\xff\xd8FAKEJPEG")
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": "", "command": a}

    results: list[dict] = [{} for _ in range(4)]

    def worker(i: int):
        results[i] = ca.analyze_media(
            f"INPUT/cap_{i}.mp4", tmp, model=False, run_fn=fake_run,
            probe_fn=_fake_probe, transnet_hook=_no_transnet)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)

    assert all("error" not in r for r in results), results
    assert active["max"] <= 2, f"感知层并发应被限到 2：max={active['max']}"


# --------------------------------------------------------------------------- #
# A3：HEVC 素材感知层解码走 d3d11va（按 codec 条件启用）
# --------------------------------------------------------------------------- #

def _has_hw(argv: list) -> bool:
    return ("-hwaccel" in argv and argv[argv.index("-hwaccel") + 1] == "d3d11va"
            and argv.index("-hwaccel") < argv.index("-i"))


def test_a3_detect_av_hwaccel_flag():
    import signal_detection
    seen: list[list] = []

    def fake_run(argv, timeout=None, cwd=None):
        seen.append(list(argv))
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": "", "command": argv}

    signal_detection.detect_av("x.mp4", hwaccel=True, run_fn=fake_run)
    signal_detection.detect_av("x.mp4", hwaccel=False, run_fn=fake_run)
    assert _has_hw(seen[0]) and not _has_hw(seen[1]), seen


def test_a3_analyze_wires_hwaccel_by_codec():
    for codec, expect in (("hevc", True), ("h264", False)):
        tmp = tempfile.mkdtemp(prefix=f"a3_{codec}_")
        _make_env(tmp, "a3.mp4")
        seen_run: list[list] = []
        seen_bin: list[list] = []

        def fake_run(argv, timeout=None, cwd=None):
            a = list(argv)
            seen_run.append(a)
            if "-f" in a and "null" in a:
                return {"ok": False, "returncode": 1, "stdout": "",
                        "stderr": "fake", "command": a}
            with open(a[-1], "wb") as f:
                f.write(b"\xff\xd8FAKEJPEG")
            return {"ok": True, "returncode": 0, "stdout": "", "stderr": "", "command": a}

        def fake_bin(argv, timeout=None, cwd=None):
            seen_bin.append(list(argv))
            return {"ok": False, "returncode": 1, "stdout": b"", "stderr": "",
                    "command": argv}

        def probe(full, _c=codec):
            return {"ok": True, "kind": "video", "duration": 3.0, "has_audio": True,
                    "width": 640, "height": 360, "codec": _c}

        ca.analyze_media("INPUT/a3.mp4", tmp, model=False, run_fn=fake_run,
                         probe_fn=probe, run_binary_fn=fake_bin)
        null_cmds = [a for a in seen_run if "-f" in a and "null" in a]
        assert null_cmds, "信号检测/切分阶梯应发出全解命令"
        assert all(_has_hw(a) == expect for a in null_cmds), (codec, null_cmds)
        if seen_bin:                       # TransNet 管道（装了 torch 才会走到）
            assert _has_hw(seen_bin[0]) == expect, (codec, seen_bin[0])


# --------------------------------------------------------------------------- #
# 手动 runner
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:   # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
