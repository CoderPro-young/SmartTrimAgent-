"""V7.2 guard tests (offline): shot-split guards, fill guards, captions.

Pure ASCII on purpose - this session's tool transport garbles CJK.
Run: .venv/Scripts/python.exe tests/test_v72_guards.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import types

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "video_editing"))

import content_analysis as ca    # noqa: E402
import plan_compiler             # noqa: E402
import shot_select as ss         # noqa: E402
import workflow as wf_mod        # noqa: E402


def _shot(s, e, scene="sunset", quality="good"):
    return {"start": s, "end": e,
            "label": {"person_count": 0, "scene": scene, "activity": "empty",
                      "mood": "", "tags": [], "quality": quality,
                      "usable": quality != "poor"}}


def _card(rel, shots, silences=None, dur=None):
    ratio = None
    if silences is not None and dur:
        ratio = round(sum(b - a for a, b in silences) / dur, 3)
    return {"schema_version": 2, "source": rel, "duration": dur,
            "signals": {"audio": {"silences": silences, "silence_ratio": ratio},
                        "video": {"blacks": []}},
            "shots": shots}


def _make_files(tmp, names=("a.mp4", "b.mp4")):
    os.makedirs(os.path.join(tmp, "INPUT"), exist_ok=True)
    for n in names:
        with open(os.path.join(tmp, "INPUT", n), "wb") as f:
            f.write(b"x" * 64)


# --------------------------------------------------- shot-split guards ---- #

def t01_merge_short_shots_chain():
    shots = ca.build_shots([0.0, 1.9, 2.0, 2.1, 2.3], 10.1)
    assert [(s["start"], s["end"]) for s in shots] == [(0.0, 2.3), (2.3, 10.1)]


def t02_leading_tiny_absorbed_forward():
    shots = ca.build_shots([0.0, 0.1, 3.0], 10.0)
    assert [(s["start"], s["end"]) for s in shots] == [(0.0, 3.0), (3.0, 10.0)]


def t03_all_tiny_collapses_to_one():
    shots = ca.build_shots([0.0, 0.1, 0.2, 0.3], 1.0)
    assert len(shots) == 1 and abs(shots[0]["end"] - 1.0) < 1e-6


def t04_avg_shot_span():
    assert abs(ca._avg_shot_span([0.0, 2.0, 4.0], 10.0) - 4.0) < 1e-6
    assert ca._avg_shot_span([0.0], 10.0) == float("inf")
    assert abs(ca._avg_shot_span([0.0, 2.0, 4.0], None) - 2.0) < 1e-6


def t05_overseg_retries_higher_threshold():
    calls = []

    def fake_run(argv, timeout=None):
        th = float([a for a in argv if "gt(scene," in a][0]
                   .split("gt(scene,")[1].split(")")[0])
        calls.append(th)
        times = {0.3: [0.0, 0.5, 1.0, 1.5, 2.0],    # duration 3 -> avg .625
                 0.45: [0.0, 1.5]}[th]              # avg 1.5 -> rescued
        stderr = "".join(f"pts_time:{t:.3f} " for t in times)
        return {"ok": True, "stderr": stderr}

    times, how = ca._detect_boundaries(fake_run, "x.mp4", 3.0, 4)
    assert calls == [0.3, 0.45] and how == "scene_rescued"
    assert times == [1.5]


def t06_still_overseg_keeps_coarsest_attempt():
    def fake_run(argv, timeout=None):
        th = float([a for a in argv if "gt(scene," in a][0]
                   .split("gt(scene,")[1].split(")")[0])
        times = {0.3: [0.0, 0.5, 1.0, 1.5, 2.0],
                 0.45: [0.0, 0.6, 1.2, 1.8],
                 0.6: [0.0, 0.9, 1.8]}[th]
        stderr = "".join(f"pts_time:{t:.3f} " for t in times)
        return {"ok": True, "stderr": stderr}

    times, how = ca._detect_boundaries(fake_run, "x.mp4", 3.0, 4)
    assert how == "scene_rescued" and times == [0.9, 1.8]


def t07_run_failure_falls_back_to_uniform():
    times, how = ca._detect_boundaries(lambda *a, **k: {"ok": False},
                                       "x.mp4", 20.0, 4)
    assert how == "uniform" and times == [0.0, 5.0, 10.0, 15.0]


def t08_no_cuts_means_single_long_shot():
    def fake_run(argv, timeout=None):
        return {"ok": True, "stderr": "pts_time:0.000000 "}

    times, how = ca._detect_boundaries(fake_run, "x.mp4", 20.0, 4)
    assert times == [0.0] and how == "scene"


def t09_build_shots_regressions_intact():
    shots = ca.build_shots([0.0, 8.312, 15.24], 20.0)
    assert [(s["start"], s["end"]) for s in shots] == \
        [(0.0, 8.312), (8.312, 15.24), (15.24, 20.0)]
    long_split = ca.build_shots([0.0], 60.0)
    assert len(long_split) == 5
    assert all(abs((s["end"] - s["start"]) - 12.0) < 1e-6 for s in long_split)


# ----------------------------------------------------- fill guards -------- #

def t10_min_duration_filters_fragments():
    cards = {"INPUT/f.mp4": _card("INPUT/f.mp4",
             [_shot(0, 0.4), _shot(0.4, 6.4)], dur=6.4)}
    res = ss.select_shots(cards, {"budget_seconds": 6, "order": "best_first",
                                  "where": {"min_duration": 1.5}})
    assert [c["trim_start"] for c in res["clips"]] == [0.4]
    assert any(r["duration"] < 1.5 for r in res["rejected"])


def t11_round_robin_balances_sources():
    cards = {
        "INPUT/a.mp4": _card("INPUT/a.mp4",
                             [_shot(0, 11), _shot(11, 22)], dur=22),
        "INPUT/b.mp4": _card("INPUT/b.mp4",
                             [_shot(0, 10), _shot(10, 20)], dur=20),
    }
    res = ss.select_shots(cards, {"budget_seconds": 22, "order": "best_first"})
    srcs = [c["source"] for c in res["clips"]]
    assert srcs == ["INPUT/a.mp4", "INPUT/b.mp4"]
    used = sum(c["trim_end"] - c["trim_start"] for c in res["clips"])
    assert abs(used - 21.0) < 1e-6


def t12_round_robin_tries_shorter_from_same_source():
    cards = {
        "INPUT/a.mp4": _card("INPUT/a.mp4",
                             [_shot(0, 11), _shot(11, 16)], dur=16),
        "INPUT/b.mp4": _card("INPUT/b.mp4", [_shot(0, 20)], dur=20),
    }
    res = ss.select_shots(cards, {"budget_seconds": 16, "order": "best_first"})
    spans = [round(c["trim_end"] - c["trim_start"], 3) for c in res["clips"]]
    assert spans == [11.0, 5.0]
    assert {c["source"] for c in res["clips"]} == {"INPUT/a.mp4"}
    assert any("16" in r["reason"] for r in res["rejected"])


def t13_no_budget_keeps_flat_best_first_order():
    cards = {
        "INPUT/a.mp4": _card("INPUT/a.mp4",
                             [_shot(0, 11), _shot(11, 22)], dur=22),
        "INPUT/b.mp4": _card("INPUT/b.mp4",
                             [_shot(0, 10), _shot(10, 20)], dur=20),
    }
    res = ss.select_shots(cards, {"order": "best_first"})
    assert len(res["clips"]) == 4
    assert res["clips"][0]["trim_start"] == 0.0          # a.s1 first


# --------------------------------------------- captions after pick -------- #

def _fake_model_module(reply):
    class _M:
        def __init__(self, content):
            self._content = content

        def invoke(self, prompt):
            class _R:
                content = self._content
            return _R()

    fake = types.ModuleType("model")
    fake.get_model = lambda: _M(reply)
    return fake


def t14_smart_create_expansion_writes_captions_after_pick():
    cards = {"INPUT/a.mp4": _card("INPUT/a.mp4",
             [_shot(0, 6, "sunset"), _shot(6, 12, "seaside")], dur=12)}
    with tempfile.TemporaryDirectory() as tmp:
        _make_files(tmp, ("a.mp4",))
        ctx = {"cards": cards,
               "probes": {"INPUT/a.mp4": {"duration": 12, "has_audio": True,
                                          "has_video": True}},
               "project_root": tmp}
        fake = _fake_model_module(
            '["sunset glow is soft", "sea breeze is fair"]')
        saved = sys.modules.get("model")
        sys.modules["model"] = fake
        try:
            plan, report = wf_mod.WORKFLOWS["smart_create"].expand(
                {"budget_seconds": 12}, ctx)
        finally:
            if saved is not None:
                sys.modules["model"] = saved
            else:
                sys.modules.pop("model", None)
        assert report["captions_source"] == "llm"
        assert plan["_captions"] == ["sunset glow is soft",
                                     "sea breeze is fair"]
        assert len(plan["clips"]) == 2


def _seed_cards(tmp, cards):
    for rel, card in cards.items():
        full = os.path.join(tmp, rel)
        ca._save_json(ca._sidecar_path(tmp, full, rel), card)


def t15_captions_bind_per_clip_when_counts_match():
    cards = {"INPUT/a.mp4": _card("INPUT/a.mp4",
             [_shot(0, 6, "sunset"), _shot(6, 12, "seaside")], dur=12)}
    plan = {"schema_version": "2.0",
            "output": {"filename": "OUTPUT/o.mp4",
                       "resolution": {"width": 640, "height": 360}},
            "workflow": {"name": "smart_create", "budget_seconds": 12,
                         "transition": 0,
                         "captions": ["line one", "line two"]}}
    with tempfile.TemporaryDirectory() as tmp:
        _make_files(tmp, ("a.mp4",))
        _seed_cards(tmp, cards)
        result = plan_compiler.compile_plan(plan, tmp)
    ovs = result.plan.get("overlays") or []
    assert [o["at_clip"] for o in ovs] == ["s00", "s01"]
    assert ovs[0]["start_offset"] == 0.05
    assert abs(ovs[0]["duration"] - 5.9) < 1e-6
    assert "_captions" not in result.plan
    assert any(c.stage == "render" for c in result.commands)


def t16_count_mismatch_keeps_even_spread():
    cards = {"INPUT/a.mp4": _card("INPUT/a.mp4",
             [_shot(0, 6, "sunset"), _shot(6, 12, "seaside")], dur=12)}
    plan = {"schema_version": "2.0",
            "output": {"filename": "OUTPUT/o.mp4",
                       "resolution": {"width": 640, "height": 360}},
            "workflow": {"name": "smart_create", "budget_seconds": 12,
                         "transition": 0,
                         "captions": ["one", "two", "three"]}}
    with tempfile.TemporaryDirectory() as tmp:
        _make_files(tmp, ("a.mp4",))
        _seed_cards(tmp, cards)
        result = plan_compiler.compile_plan(plan, tmp)
    ovs = result.plan.get("overlays") or []
    assert len(ovs) == 3
    assert [o["at_clip"] for o in ovs] == ["s00", "s00", "s01"]


TESTS = [v for k, v in sorted(globals().items())
         if k.startswith("t") and callable(v)]


def main() -> int:
    ok = fail = 0
    for fn in TESTS:
        try:
            fn()
            ok += 1
            print(f"  [ok] {fn.__name__}")
        except AssertionError as exc:
            fail += 1
            print(f"  [FAIL] {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            fail += 1
            print(f"  [ERR] {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"V7.2 guards: {ok}/{ok + fail} passed")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
