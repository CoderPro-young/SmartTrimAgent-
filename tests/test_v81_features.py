"""V8.1–V8.3 离线单测：创作流注册表化 / BGM 节拍卡点 / aes_score 美学分。

运行：.venv/Scripts/python.exe tests/test_v81_features.py
"""

from __future__ import annotations

import os
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "video_editing"))

import beat_detect            # noqa: E402
import creative_skills as cs  # noqa: E402
import plan_compiler          # noqa: E402
import plan_schema            # noqa: E402
import shot_select            # noqa: E402
import video_agent as va      # noqa: E402


# ------------------------------------------------------ ① 注册表化 ------- #

def t01_registry_and_doc():
    assert set(cs.CREATIVE_SKILLS) == {"general_vlog", "travel_vlog", "food_vlog"}
    doc = cs.render_creative_doc()
    for s in cs.CREATIVE_SKILLS.values():
        assert f"`{s.name}`" in doc and s.summary.split("（")[0] in doc
        if s.flavor:
            assert s.flavor.splitlines()[0] in doc
    # 公共段含硬规则与手写键示例
    assert "80%" in doc and "_narration" in doc and "snap_beats" in doc


def t02_prompt_assembly():
    p = va.V2_SYSTEM_PROMPT
    assert "{CREATIVE_DOC}" not in p
    assert p.count("## When to ask instead of submit") == 1   # 修复 V8.0 的重复标题
    for name in cs.CREATIVE_SKILLS:
        assert name in p
    assert "snap_beats" in p


# ------------------------------------------------------ ② 节拍卡点 ------ #

def _click_pcm(bpm: float = 120.0, seconds: float = 24.0):
    """合成打击乐轨：每拍一个短促能量脉冲（f32le PCM bytes）。"""
    import numpy as np
    sr, n = beat_detect.SR, int(beat_detect.SR * seconds)
    x = np.zeros(n, dtype="<f4")
    period = 60.0 / bpm
    k = 0
    while k * period < seconds - 0.05:
        s = int(k * period * sr)
        ln = int(0.03 * sr)                       # 30ms 脉冲
        env = np.linspace(1.0, 0.0, ln)
        x[s:s + ln] += (env * np.sin(
            np.arange(ln) * 2 * np.pi * 220 / sr)).astype("<f4")
        k += 1
    return x.tobytes()


def _fake_run_binary(pcm: bytes):
    def run(argv, timeout=120):
        return {"ok": True, "stdout": pcm, "returncode": 0}
    return run


def _tmp_bgm(tmp: str, pcm: bytes):
    os.makedirs(os.path.join(tmp, "INPUT"), exist_ok=True)
    with open(os.path.join(tmp, "INPUT", "bgm.mp3"), "wb") as f:
        f.write(pcm)
    return "INPUT/bgm.mp3"


def t03_detect_click_track():
    """120BPM 打击轨 → BPM≈120、节拍落在半秒栅格、置信度高。"""
    with tempfile.TemporaryDirectory() as tmp:
        src = _tmp_bgm(tmp, _click_pcm(120.0))
        res = beat_detect.detect_beats(
            src, tmp, run_binary=_fake_run_binary(_click_pcm(120.0)))
        assert res and res["bpm"] is not None, res
        assert abs(res["bpm"] - 120) < 3, res["bpm"]
        assert res["confidence"] >= 1.6
        # 节拍应贴近 0.5s 栅格（±60ms）
        off_grid = [b for b in res["beats"][:20]
                    if abs((b % 0.5) - 0.5) > 0.06 and (b % 0.5) > 0.06]
        assert not off_grid, off_grid[:5]


def t04_detect_silence_returns_none():
    import numpy as np
    pcm = (np.random.RandomState(7).rand(beat_detect.SR * 20) * 0.002)
    with tempfile.TemporaryDirectory() as tmp:
        src = _tmp_bgm(tmp, pcm.astype("<f4").tobytes())
        res = beat_detect.detect_beats(src, tmp,
                                       run_binary=_fake_run_binary(b"x"))
        assert res is None, res          # 无清晰节拍 → None，卡点优雅缺席


def t05_detect_cached_second_call():
    calls = {"n": 0}

    def counting_run(argv, timeout=120):
        calls["n"] += 1
        return {"ok": True, "stdout": _click_pcm(), "returncode": 0}

    with tempfile.TemporaryDirectory() as tmp:
        src = _tmp_bgm(tmp, b"m")
        r1 = beat_detect.detect_beats(src, tmp, run_binary=counting_run)
        r2 = beat_detect.detect_beats(src, tmp, run_binary=counting_run)
        assert calls["n"] == 1, "第二次应命中缓存不再解码"
        assert r1 == r2


def _snap_plan(tmp: str, snap=True, with_probe=True):
    os.makedirs(os.path.join(tmp, "INPUT"), exist_ok=True)
    with open(os.path.join(tmp, "INPUT", "a.mp4"), "wb") as f:
        f.write(b"x" * 64)
    probe = {"duration": 12, "width": 1280, "height": 720, "fps": 30,
             "has_audio": True, "has_video": True} if with_probe else None
    clips = [
        {"id": "c1", "source": "INPUT/a.mp4", "kind": "video",
         "trim_start": 0, "trim_end": 4},
        {"id": "c2", "source": "INPUT/a.mp4", "kind": "video",
         "trim_start": 4, "trim_end": 8},
        {"id": "c3", "source": "INPUT/a.mp4", "kind": "video",
         "trim_start": 8, "trim_end": 12},
    ]
    if probe:
        for c in clips:
            c["probe"] = dict(probe)
    return {
        "schema_version": "2.0",
        "output": {"filename": "OUTPUT/b.mp4",
                   "resolution": {"width": 640, "height": 360}},
        "clips": clips,
        "timeline": [{"clip": "c1"},
                     {"clip": "c2", "transition": {"type": "fade", "duration": 0.3}},
                     {"clip": "c3", "transition": {"type": "fade", "duration": 0.3}}],
        "audio": {"source": _tmp_bgm(tmp, b"m"), "volume": 1.0,
                  "loop": True, "snap_beats": snap},
    }


_BEATS = {"bpm": 120.0, "offset": 0.0,
          "beats": [round(i * 1.0, 3) for i in range(20)], "confidence": 2.5}


def t06_snap_boundaries_math():
    """整秒节拍栅格：cut1=3.85→4.0(+0.15)、cut2=7.7→8.0(+0.3)，均 ≤0.35。"""
    with tempfile.TemporaryDirectory() as tmp:
        plan = _snap_plan(tmp)
        orig = beat_detect.detect_beats
        beat_detect.detect_beats = lambda src, root, **kw: dict(_BEATS)
        try:
            rep = plan_compiler._snap_to_beats(plan, tmp)
        finally:
            beat_detect.detect_beats = orig
        assert rep["snapped"] == 2 and rep["skipped"] == 0, rep
        assert rep["max_shift"] == 0.3
        c1 = next(c for c in plan["clips"] if c["id"] == "c1")
        c2 = next(c for c in plan["clips"] if c["id"] == "c2")
        assert c1["trim_end"] == 4.15, c1
        assert c2["trim_end"] == 8.3, c2


def t07_snap_no_probe_shrink_only():
    """无 probe 的片段只能缩短：正向位移跳过、负向位移允许。"""
    neg = {"bpm": 90, "offset": 0.0,
           "beats": [round(i * 0.6667, 3) for i in range(30)], "confidence": 3}
    # cut1 = 3.85 → 最近节拍 3.333(|Δ|>0.35 跳过) 或 4.0(+0.15 无probe跳过)
    with tempfile.TemporaryDirectory() as tmp:
        plan = _snap_plan(tmp, with_probe=False)
        orig = beat_detect.detect_beats
        beat_detect.detect_beats = lambda src, root, **kw: dict(neg)
        try:
            rep = plan_compiler._snap_to_beats(plan, tmp)
        finally:
            beat_detect.detect_beats = orig
        assert rep["skipped"] >= 1, rep


def t08_schema_snap_beats():
    with tempfile.TemporaryDirectory() as tmp:
        plan = _snap_plan(tmp)
        assert plan_schema.validate_plan(plan, tmp) == []
        bad = _snap_plan(tmp)
        bad["audio"]["snap_beats"] = "yes"
        errs = plan_schema.validate_plan(bad, tmp)
        assert any("snap_beats" in e for e in errs), errs


def t09_compile_with_snap():
    with tempfile.TemporaryDirectory() as tmp:
        plan = _snap_plan(tmp)
        orig = beat_detect.detect_beats
        beat_detect.detect_beats = lambda src, root, **kw: dict(_BEATS)
        try:
            result = plan_compiler.compile_plan(plan, tmp)
        finally:
            beat_detect.detect_beats = orig
        beats = result.expansions.get("beats")
        assert beats and beats["snapped"] == 2, beats
        # 时间轴数学跟随吸附后的时长（c1 4.15s 起点 0）
        assert abs(result.math["durations"]["c1"] - 4.15) < 1e-6


# ------------------------------------------------------ ③ aes_score ---- #

def t10_sanitize_aes():
    f = va  # noqa: F841  （保持 import 集中）
    import content_analysis as ca
    lab = ca.sanitize_label({"aes_score": 1.5})
    assert lab["aes_score"] == 1.0
    lab = ca.sanitize_label({"aes_score": "0.75"})
    assert lab["aes_score"] == 0.75
    lab = ca.sanitize_label({"aes_score": None})
    assert lab["aes_score"] is None
    assert "aes_score" in ca._TAG_PROMPT


def t11_select_aes_min():
    def shot(aes):
        return {"start": 0, "end": 4, "label": {
            "person_count": 0, "scene": "草地", "activity": "空镜", "mood": "",
            "tags": [], "quality": "good", "aes_score": aes, "usable": True}}

    cards = {"INPUT/a.mp4": {"source": "INPUT/a.mp4", "duration": 8,
                             "signals": {"audio": {}, "video": {}},
                             "shots": [shot(0.85), shot(0.5)]}}
    res = shot_select.select_shots(cards, {
        "sources": ["INPUT/a.mp4"], "where": {"aes_min": 0.6},
        "budget_seconds": 30})
    assert len(res["clips"]) == 1
    assert any("美学分" in r["reason"] for r in res["rejected"])
    # 旧卡无 aes_score：条件视为未约束，不拦（两个镜头都清掉 aes）
    for s in cards["INPUT/a.mp4"]["shots"]:
        s["label"]["aes_score"] = None
    res = shot_select.select_shots(cards, {
        "sources": ["INPUT/a.mp4"], "where": {"aes_min": 0.6},
        "budget_seconds": 30})
    assert len(res["clips"]) == 2


def t12_list_shots_shows_aes():
    import content_analysis as ca
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "INPUT"), exist_ok=True)
        with open(os.path.join(tmp, "INPUT", "a.mp4"), "wb") as f:
            f.write(b"x" * 64)
        ca._save_json(ca._sidecar_path(
            tmp, os.path.join(tmp, "INPUT/a.mp4"), "INPUT/a.mp4"), {
            "schema_version": 2, "source": "INPUT/a.mp4", "duration": 8,
            "signals": {"audio": {"silences": None, "silence_ratio": None},
                        "video": {"blacks": []}},
            "shots": [{"start": 0, "end": 8, "label": {
                "person_count": 0, "scene": "草地", "activity": "空镜",
                "mood": "", "tags": [], "quality": "good",
                "aes_score": 0.82, "usable": True, "desc": ""}}]})
        va.PROJECT_ROOT, va.INPUT_DIR = tmp, os.path.join(tmp, "INPUT")
        out = va.list_shots.func()
        assert "美学0.82" in out, out


def t13_select_where_whitelist():
    with tempfile.TemporaryDirectory() as tmp:
        plan = {"schema_version": "2.0",
                "output": {"filename": "OUTPUT/o.mp4",
                           "resolution": {"width": 640, "height": 360}},
                "select": {"sources": ["INPUT/a.mp4"],
                           "where": {"aes_min": 0.6}, "budget_seconds": 30}}
        errs = plan_schema.validate_plan(plan, tmp)
        assert not any("where" in e and "不支持" in e for e in errs), errs


TESTS = [v for k, v in sorted(globals().items())
         if k[:1] == "t" and k[1:3].isdigit()]


def main():
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\nV8.1–V8.3 离线测试：{len(TESTS) - failed}/{len(TESTS)} 项通过")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
