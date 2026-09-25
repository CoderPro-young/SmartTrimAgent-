"""V5 粗剪离线单测：信号检测（T10）/ cut 展开（T12）/ select 宏。

全 mock 不联网：假 ffmpeg（run_fn）/ 手工构造的内容卡片 sidecar。
运行：.venv/Scripts/python.exe tests/test_roughcut.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "video_editing"))

import content_analysis as ca          # noqa: E402
import plan_compiler                   # noqa: E402
import plan_schema                     # noqa: E402
import shot_select                     # noqa: E402
import signal_detection as sd          # noqa: E402

SILENCE_STDERR = """
[silencedetect @ 0x1] silence_start: 1.0
[silencedetect @ 0x1] silence_end: 3.0 | silence_duration: 2.0
[silencedetect @ 0x1] silence_start: 6.5
[silencedetect @ 0x1] silence_end: 7.5 | silence_duration: 1.0
[silencedetect @ 0x1] silence_start: 9.0
"""
BLACK_STDERR = """
[blackdetect @ 0x2] black_start:0.000 black_end:1.200 black_duration:1.200
[blackdetect @ 0x2] black_start:8.000 black_end:10.000 black_duration:2.000
"""


def make_project(tmp: str, *names: str) -> str:
    os.makedirs(os.path.join(tmp, "INPUT"), exist_ok=True)
    for n in names:
        with open(os.path.join(tmp, "INPUT", n), "wb") as f:
            f.write(b"x" * 64)
    return tmp


# ------------------------------------------------------------ 信号解析 ----- #

def t01_parse_silencedetect():
    ivs = sd.parse_silencedetect(SILENCE_STDERR)
    assert ivs == [[1.0, 3.0], [6.5, 7.5]], ivs


def t02_parse_silencedetect_trailing_closure():
    ivs = sd.parse_silencedetect(SILENCE_STDERR, total_duration=12.0)
    assert ivs == [[1.0, 3.0], [6.5, 7.5], [9.0, 12.0]], ivs


def t03_parse_blackdetect():
    ivs = sd.parse_blackdetect(BLACK_STDERR)
    assert ivs == [[0.0, 1.2], [8.0, 10.0]], ivs


def t04_merge_intervals_clip_and_merge():
    assert sd.merge_intervals([[5, 7], [1, 3], [2.5, 4]], lo=0, hi=6) == [[1, 4], [5, 6]]


def t05_kept_intervals_pad_and_minkeep():
    # 0-10s 区间剪掉 [1,3] 和 [6.5,7.5]：补集 [0,1],[3,6.5],[7.5,10]，
    # pad=0.15 收缩内边界 → [0,0.85],[3.15,6.35],[7.65,10]
    kept = sd.kept_intervals([[1, 3], [6.5, 7.5]], 0, 10, pad=0.15, min_keep=0.3)
    assert kept == [[0, 0.85], [3.15, 6.35], [7.65, 10.0]], kept


def t06_kept_intervals_drops_tiny_fragments():
    # 0.2s 的碎片段低于 min_keep=0.3，被丢弃
    kept = sd.kept_intervals([[1, 3], [3.2, 5]], 0, 6, pad=0.0, min_keep=0.3)
    assert kept == [[0, 1], [5, 6]], kept


def t07_detect_silences_offset_and_limit():
    """-ss 前置 seek 后时间戳归零：检测结果的相对区间要加回 offset。"""
    def fake_run(argv, timeout=None, cwd=None):
        assert "-ss" in argv and argv[argv.index("-ss") + 1] == "100.000"
        assert "-t" in argv
        return {"ok": True, "returncode": 0, "stdout": "",
                "stderr": SILENCE_STDERR, "command": argv}
    res = sd.detect_silences("INPUT/a.mp4", offset=100.0, limit=20.0,
                             total_duration=120.0, run_fn=fake_run)
    assert res["ok"]
    assert res["intervals"] == [[101.0, 103.0], [106.5, 107.5], [109.0, 120.0]], res


def t08_overlap_seconds():
    assert sd.overlap_seconds([[1, 3], [2.5, 4]], 2, 3.5) == 1.5


# -------------------------------------------------------- 卡片 signals ---- #

def _card_probe(duration=12.0, has_audio=True):
    return lambda full: {"ok": True, "kind": "video", "duration": duration,
                         "width": 1920, "height": 1080, "has_audio": has_audio}


def _signal_run(showinfo=None):
    def run(argv, timeout=None, cwd=None):
        joined = " ".join(argv)
        if "showinfo" in joined:
            return {"ok": showinfo is not None, "returncode": 0,
                    "stdout": "", "stderr": showinfo or "", "command": argv}
        if "blackdetect" in joined and "silencedetect" in joined:
            return {"ok": True, "returncode": 0, "stdout": "",
                    "stderr": BLACK_STDERR + SILENCE_STDERR, "command": argv}
        if "silencedetect" in joined:
            return {"ok": True, "returncode": 0, "stdout": "",
                    "stderr": SILENCE_STDERR, "command": argv}
        out = argv[-1]
        with open(out, "wb") as f:
            f.write(b"\xff\xd8FAKE")
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": "", "command": argv}
    return run


def t09_card_carries_signals():
    with tempfile.TemporaryDirectory() as tmp:
        make_project(tmp, "a.mp4")
        card = ca.analyze_media("INPUT/a.mp4", tmp, model=False,
                                run_fn=_signal_run("pts_time:0\npts_time:6\n"),
                                probe_fn=_card_probe())
        sig = card.get("signals")
        assert sig and sig["audio"]["silences"] == [[1.0, 3.0], [6.5, 7.5], [9.0, 12.0]]
        assert abs(sig["audio"]["silence_ratio"] - 6.0 / 12.0) < 1e-6
        assert sig["video"]["blacks"] == [[0.0, 1.2], [8.0, 10.0]]
        assert card["schema_version"] == 2


def t10_card_no_audio_signals_none():
    with tempfile.TemporaryDirectory() as tmp:
        make_project(tmp, "a.mp4")
        card = ca.analyze_media("INPUT/a.mp4", tmp, model=False,
                                run_fn=_signal_run("pts_time:0\n"),
                                probe_fn=_card_probe(has_audio=False))
        assert card["signals"]["audio"] is None


def t11_card_v1_upgraded_in_place():
    """v1 旧卡命中缓存时只补信号，不重跑 VLM/场景切分。"""
    with tempfile.TemporaryDirectory() as tmp:
        make_project(tmp, "a.mp4")
        full = os.path.join(tmp, "INPUT", "a.mp4")
        v1 = {"schema_version": 1, "source": "INPUT/a.mp4", "duration": 12.0,
              "summary": "共 1 个镜头", "shots": [{"start": 0.0, "end": 12.0}]}
        ca._save_json(ca._sidecar_path(tmp, full, "INPUT/a.mp4"), v1)
        # 升级路径会现场 ffprobe 查音轨——把探测 stub 到 content_analysis
        orig_probe = ca.ffmpeg_exec.probe
        ca.ffmpeg_exec.probe = lambda p: {
            "available": True, "data": {"streams": [{"codec_type": "audio"}]}}
        try:
            card = ca.analyze_media("INPUT/a.mp4", tmp, model=False,
                                    run_fn=_signal_run(None))
        finally:
            ca.ffmpeg_exec.probe = orig_probe
        assert card["cached"] is True
        assert card["schema_version"] == 2
        assert card["signals"]["audio"]["silences"] == [[1.0, 3.0], [6.5, 7.5], [9.0, 12.0]]


# ------------------------------------------------------------ select 宏 --- #

def _fake_card(source, shots, duration=60.0, silences=None):
    return {"schema_version": 2, "source": source, "duration": duration,
            "signals": {"audio": {"silences": silences, "silence_ratio": None},
                        "video": {"blacks": []}},
            "shots": shots}


def _shot(s, e, **lab):
    base = {"person_count": 0, "has_children": False, "scene": "", "activity": "",
            "mood": "", "tags": [], "quality": "ok", "usable": True}
    base.update(lab)
    return {"start": s, "end": e, "label": base}


def t12_shot_matches_text_loose():
    where = {"scene_any": ["聚会"]}
    assert shot_select.shot_matches(
        _shot(0, 5, scene="家庭聚会", tags=["多人"]), where)
    assert shot_select.shot_matches(
        _shot(0, 5, scene="餐厅", tags=["聚会合影"]), where)
    assert not shot_select.shot_matches(_shot(0, 5, scene="海边"), where)


def t13_shot_matches_numbers_and_quality():
    shot = _shot(0, 5, person_count=2, quality="poor")
    assert shot_select.shot_matches(shot, {"person_count_min": 2})
    assert not shot_select.shot_matches(shot, {"person_count_min": 3})
    assert not shot_select.shot_matches(shot, {"quality_in": ["good", "ok"]})


def t14_select_budget_greedy():
    cards = {"INPUT/a.mp4": _fake_card("INPUT/a.mp4", [
        _shot(0, 10, scene="聚会"), _shot(10, 20, scene="聚会"), _shot(20, 21, scene="聚会"),
    ])}
    res = shot_select.select_shots(cards, {
        "sources": ["INPUT/a.mp4"], "where": {"scene_any": ["聚会"]},
        "budget_seconds": 25})
    # 10+10 装满后跳过 10s 的第三段？——前两段已 20s，第三段 1s 放得下 → 21s
    assert len(res["clips"]) == 3
    assert res["clips"][0]["trim_start"] == 0 and res["clips"][0]["trim_end"] == 10
    assert res["timeline"] == [{"clip": "s00"}, {"clip": "s01"}, {"clip": "s02"}]


def t15_select_budget_skips_too_long():
    cards = {"INPUT/a.mp4": _fake_card("INPUT/a.mp4", [
        _shot(0, 30, scene="聚会"), _shot(30, 40, scene="聚会"),
    ])}
    res = shot_select.select_shots(cards, {
        "sources": ["INPUT/a.mp4"], "where": {"scene_any": ["聚会"]},
        "budget_seconds": 15})
    # 30s 放不进 15s 预算 → 跳过；10s 的第二段放得下 → 只选它
    assert len(res["clips"]) == 1
    assert res["clips"][0]["trim_start"] == 30


def t16_select_max_silence_ratio():
    cards = {"INPUT/a.mp4": _fake_card("INPUT/a.mp4", [
        _shot(0, 10, scene="聚会"), _shot(10, 20, scene="聚会"),
    ], silences=[[0, 8]])}     # 第一镜头 80% 是静音
    res = shot_select.select_shots(cards, {
        "sources": ["INPUT/a.mp4"], "where": {"scene_any": ["聚会"],
                                              "max_silence_ratio": 0.5}})
    assert len(res["clips"]) == 1 and res["clips"][0]["trim_start"] == 10


def t17_select_best_first_order():
    cards = {"INPUT/a.mp4": _fake_card("INPUT/a.mp4", [
        _shot(0, 5, scene="x", quality="ok"),
        _shot(5, 10, scene="x", quality="good"),
    ])}
    res = shot_select.select_shots(cards, {
        "sources": ["INPUT/a.mp4"], "where": {}, "order": "best_first"})
    assert res["picked"][0]["quality"] == "good"


# ------------------------------------------------- schema / 编译器展开 ---- #

def _ok_plan(**clip_extra):
    return {
        "schema_version": "2.0",
        "output": {"filename": "OUTPUT/o.mp4",
                   "resolution": {"width": 640, "height": 480}, "fps": 30},
        "clips": [{"id": "c1", "source": "INPUT/a.mp4", "kind": "video",
                   "trim_start": 0, "trim_end": 10,
                   "probe": {"duration": 10.0, "has_audio": True,
                             "has_video": True}, **clip_extra}],
        "timeline": [{"clip": "c1"}],
    }


def t18_validate_cut_fields():
    with tempfile.TemporaryDirectory() as tmp:
        make_project(tmp, "a.mp4", "a.png")
        errs = plan_schema.validate_plan(
            _ok_plan(cut_silence={"noise_db": -35, "min_silence": 0.4}), tmp)
        assert errs == [], errs
        errs = plan_schema.validate_plan(
            _ok_plan(cut_silence={"noise_db": 100}), tmp)
        assert any("noise_db" in e for e in errs), errs
        # 图片 clip 不许用 cut_silence
        bad = {"schema_version": "2.0",
               "output": {"filename": "OUTPUT/o.mp4",
                          "resolution": {"width": 640, "height": 480}},
               "clips": [{"id": "c1", "source": "INPUT/a.png", "kind": "image",
                          "duration": 3, "cut_silence": {}}],
               "timeline": [{"clip": "c1"}]}
        errs = plan_schema.validate_plan(bad, tmp)
        assert any("只适用于 video" in e for e in errs), errs


def t19_validate_select_mutual_exclusion():
    with tempfile.TemporaryDirectory() as tmp:
        make_project(tmp, "a.mp4")
        sel = {"sources": ["INPUT/a.mp4"], "where": {}}
        plan = {"schema_version": "2.0",
                "output": {"filename": "OUTPUT/o.mp4",
                           "resolution": {"width": 640, "height": 480}},
                "select": sel, "clips": [{"id": "c", "source": "INPUT/a.mp4",
                                          "kind": "video", "trim_end": 5}]}
        errs = plan_schema.validate_plan(plan, tmp)
        assert any("互斥" in e for e in errs), errs
        del plan["clips"]
        errs = plan_schema.validate_plan(plan, tmp)
        assert errs == [], errs


def t20_check_material_fit_cut_no_audio():
    with tempfile.TemporaryDirectory() as tmp:
        make_project(tmp, "a.mp4")
        plan = _ok_plan(cut_silence={})
        plan["clips"][0]["probe"]["has_audio"] = False
        ms = plan_schema.check_material_fit(plan, tmp, probe_fn=lambda src: {
            "ok": True, "duration": 10.0, "has_audio": False, "has_video": True})
        assert any(m.type == "cut_no_audio" for m in ms), ms


def _cut_run(intervals):
    """假 ffmpeg：cut 检测命令返回给定静音区间（相对 seek 点）。"""
    stderr = "".join(
        f"[silencedetect @ 0x1] silence_start:{s}\n"
        f"[silencedetect @ 0x1] silence_end:{e} | silence_duration:{e - s}\n"
        for s, e in intervals)
    def run(argv, timeout=None, cwd=None):
        if "silencedetect" in " ".join(argv):
            return {"ok": True, "returncode": 0, "stdout": "", "stderr": stderr,
                    "command": argv}
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": "",
                "command": argv}
    return run


def t21_expand_cuts_basic():
    with tempfile.TemporaryDirectory() as tmp:
        make_project(tmp, "a.mp4")
        plan = _ok_plan(cut_silence={"keep_padding": 0.1, "min_keep": 0.3})
        new_plan, reports = plan_compiler._expand_cuts(plan, tmp,
                                                       run_fn=_cut_run([[2, 4], [6, 8]]))
        cids = [c["id"] for c in new_plan["clips"]]
        assert cids == ["c1__k0", "c1__k1", "c1__k2"], cids
        ranges = [(c["trim_start"], c["trim_end"]) for c in new_plan["clips"]]
        assert ranges == [(0, 1.9), (4.1, 5.9), (8.1, 10)], ranges
        assert new_plan["timeline"] == [{"clip": "c1__k0"}, {"clip": "c1__k1"},
                                        {"clip": "c1__k2"}]
        assert reports[0]["removed_seconds"] == 4.0
        assert reports[0]["kept_segments"] == 3
        # 展开后的计划本身是一份合法计划
        assert plan_schema.validate_plan(new_plan, tmp) == []


def t22_expand_cuts_transition_lands_on_first():
    with tempfile.TemporaryDirectory() as tmp:
        make_project(tmp, "a.mp4", "b.mp4")
        plan = _ok_plan(cut_silence={})
        plan["clips"].append({"id": "c2", "source": "INPUT/b.mp4", "kind": "video",
                              "trim_start": 0, "trim_end": 5,
                              "probe": {"duration": 5.0, "has_audio": True,
                                        "has_video": True}})
        plan["timeline"] = [{"clip": "c1"},
                            {"clip": "c2", "transition": {"type": "fade",
                                                          "duration": 0.5}}]
        new_plan, _ = plan_compiler._expand_cuts(plan, tmp,
                                                 run_fn=_cut_run([[2, 4]]))
        assert new_plan["timeline"][0] == {"clip": "c1__k0"}
        assert new_plan["timeline"][1] == {"clip": "c1__k1"}
        assert new_plan["timeline"][2] == {"clip": "c2",
                                           "transition": {"type": "fade",
                                                          "duration": 0.5}}


def t23_expand_cuts_all_removed_errors():
    with tempfile.TemporaryDirectory() as tmp:
        make_project(tmp, "a.mp4")
        plan = _ok_plan(cut_silence={})
        try:
            plan_compiler._expand_cuts(plan, tmp, run_fn=_cut_run([[0, 10]]))
            assert False, "应当抛 CompileError"
        except plan_compiler.CompileError as exc:
            assert "没有剩余内容" in exc.errors[0]


def t24_expand_cuts_no_audio_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        make_project(tmp, "a.mp4")
        plan = _ok_plan(cut_silence={})
        del plan["clips"][0]["probe"]["has_audio"]
        orig = plan_compiler.ffmpeg_exec.probe
        plan_compiler.ffmpeg_exec.probe = lambda p: {
            "available": True, "data": {"streams": [{"codec_type": "video"}]}}
        try:
            try:
                plan_compiler._expand_cuts(plan, tmp, run_fn=_cut_run([]))
                assert False, "应当抛 CompileError"
            except plan_compiler.CompileError as exc:
                assert "没有音轨" in exc.errors[0]
        finally:
            plan_compiler.ffmpeg_exec.probe = orig


def t25_compile_plan_with_select_end_to_end():
    """select → 编译出完整命令序列（全程无 ffmpeg 调用，卡片走假 sidecar）。"""
    with tempfile.TemporaryDirectory() as tmp:
        make_project(tmp, "a.mp4")
        full = os.path.join(tmp, "INPUT", "a.mp4")
        card = _fake_card("INPUT/a.mp4", [
            _shot(0, 4, scene="聚会", person_count=3, quality="good"),
            _shot(4, 12, scene="海边", person_count=0),
            _shot(12, 20, scene="聚会", person_count=2, quality="ok"),
        ])
        ca._save_json(ca._sidecar_path(tmp, full, "INPUT/a.mp4"), card)
        plan = {"schema_version": "2.0",
                "output": {"filename": "OUTPUT/reel.mp4",
                           "resolution": {"width": 640, "height": 480}},
                "select": {"sources": ["INPUT/a.mp4"],
                           "where": {"scene_any": ["聚会"]},
                           "budget_seconds": 12}}
        result = plan_compiler.compile_plan(plan, tmp)
        assert [c["id"] for c in result.plan["clips"]] == ["s00", "s01"]
        assert result.expansions["select"]["total_seconds"] == 12.0
        assert any(c.stage == "render" for c in result.commands)
        assert result.math["D"] == 12.0


def t26_compile_plan_select_missing_card_errors():
    with tempfile.TemporaryDirectory() as tmp:
        make_project(tmp, "a.mp4")
        plan = {"schema_version": "2.0",
                "output": {"filename": "OUTPUT/reel.mp4",
                           "resolution": {"width": 640, "height": 480}},
                "select": {"sources": ["INPUT/a.mp4"], "where": {}}}
        try:
            plan_compiler.compile_plan(plan, tmp)
            assert False, "应当抛 CompileError"
        except plan_compiler.CompileError as exc:
            assert "analyze_media" in exc.errors[0]


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("t") and callable(v)]


def main() -> int:
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"  [ok] {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  [FAIL] {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  [ERROR] {fn.__name__}: {type(exc).__name__}: {exc}")
    total = len(TESTS)
    print(f"\nV5 粗剪离线测试：{total - failed}/{total} 项通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
