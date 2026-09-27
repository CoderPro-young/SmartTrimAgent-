"""V7 一键成片 workflow 宏离线单测。

运行：.venv/Scripts/python.exe tests/test_v7_workflow.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "video_editing"))

import plan_compiler   # noqa: E402
import plan_schema     # noqa: E402
import workflow as wf_mod  # noqa: E402


def _shot(s, e, scene="草地", quality="good", pc=0):
    return {"start": s, "end": e,
            "label": {"person_count": pc, "scene": scene, "activity": "空镜",
                      "mood": "", "tags": [], "quality": quality, "usable": quality != "poor"}}


def _card(rel, shots, silences=None, dur=None):
    ratio = None
    if silences is not None and dur:
        ratio = round(sum(e - s for s, e in silences) / dur, 3)
    return {"schema_version": 2, "source": rel, "duration": dur,
            "signals": {"audio": {"silences": silences, "silence_ratio": ratio},
                        "video": {"blacks": []}},
            "shots": shots}


def _make_project(tmp: str):
    os.makedirs(os.path.join(tmp, "INPUT"), exist_ok=True)
    for n in ("a.mp4", "b.mp4", "silent.mp4"):
        with open(os.path.join(tmp, "INPUT", n), "wb") as f:
            f.write(b"x" * 64)
    return tmp


def _ctx(tmp, cards, probes):
    import content_analysis as ca
    for rel, card in cards.items():
        full = os.path.join(tmp, rel)
        ca._save_json(ca._sidecar_path(tmp, full, rel), card)
    return {"cards": cards, "probes": probes, "project_root": tmp}


CARDS = {
    "INPUT/a.mp4": _card("INPUT/a.mp4",
                         [_shot(0, 6, "日落"), _shot(6, 12, "海边")], dur=12),
    "INPUT/b.mp4": _card("INPUT/b.mp4",
                         [_shot(0, 8, "草地", quality="poor")], dur=8),
    # 整条静音 → 粗筛应丢弃
    "INPUT/silent.mp4": _card("INPUT/silent.mp4", [_shot(0, 10, "草地")],
                              silences=[[0, 10]], dur=10),
}
PROBES = {
    "INPUT/a.mp4": {"duration": 12, "has_audio": True, "has_video": True},
    "INPUT/b.mp4": {"duration": 8, "has_audio": True, "has_video": True},
    "INPUT/silent.mp4": {"duration": 10, "has_audio": True, "has_video": True},
}


def t01_registry_and_prompt_doc():
    assert set(wf_mod.WORKFLOWS) == {"one_click_reel", "speech_clean"}
    doc = wf_mod.render_workflow_doc()
    assert "one_click_reel" in doc and "budget_seconds" in doc


def t02_validate_workflow_block():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        plan = {"schema_version": "2.0",
                "output": {"filename": "OUTPUT/o.mp4",
                           "resolution": {"width": 640, "height": 480}},
                "workflow": {"name": "one_click_reel", "budget_seconds": 15}}
        assert plan_schema.validate_plan(plan, tmp) == []
        bad = dict(plan); bad["workflow"] = {"name": "nope"}
        errs = plan_schema.validate_plan(bad, tmp)
        assert any("workflow.name" in e for e in errs), errs
        clash = dict(plan); clash["clips"] = [{"id": "c", "source": "INPUT/a.mp4",
                                               "kind": "video", "trim_end": 5}]
        errs = plan_schema.validate_plan(clash, tmp)
        assert any("互斥" in e for e in errs), errs


def t03_one_click_reel_expansion():
    """粗筛丢 silent → poor 镜头被拒 → 预算内挑 good 镜头 → 默认 fade。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        ctx = _ctx(tmp, CARDS, PROBES)
        plan, report = wf_mod.WORKFLOWS["one_click_reel"].expand(
            {"budget_seconds": 10}, ctx)
        ids = [c["id"] for c in plan["clips"]]
        assert ids == ["s00"], ids                      # 日落 6s 装进 10s 预算
        assert plan["timeline"][0] == {"clip": "s00"}   # 单段无转场
        culled_srcs = {c["source"] for c in report["culled"]}
        assert culled_srcs == {"INPUT/b.mp4", "INPUT/silent.mp4"}, culled_srcs
        # 全 poor 素材在粗筛层就被拦下；进筛选的只剩 a.mp4 的两个镜头
        # （第二个被 10s 预算拒掉）
        assert report["rejected_total"] == 1
        assert report["picked"] == 1


def t04_one_click_reel_multi_transition():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        cards = {
            "INPUT/a.mp4": _card("INPUT/a.mp4",
                                 [_shot(0, 5, "日落"), _shot(5, 10, "日落")], dur=10),
        }
        ctx = _ctx(tmp, cards, {"INPUT/a.mp4": PROBES["INPUT/a.mp4"]})
        plan, _ = wf_mod.WORKFLOWS["one_click_reel"].expand(
            {"budget_seconds": 30, "transition": 0.4}, ctx)
        assert len(plan["clips"]) == 2
        assert plan["timeline"][1]["transition"] == {"type": "fade", "duration": 0.4}
        assert "transition" not in plan["timeline"][0]


def t05_one_click_reel_keyword_filter():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        ctx = _ctx(tmp, CARDS, PROBES)
        plan, report = wf_mod.WORKFLOWS["one_click_reel"].expand(
            {"budget_seconds": 30, "keyword": "日落"}, ctx)
        srcs = {c["source"] for c in plan["clips"]}
        assert srcs == {"INPUT/a.mp4"}, srcs             # 只命中日落镜头
        assert report["keyword"] == "日落"


def t06_one_click_reel_no_usable():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        ctx = _ctx(tmp, {"INPUT/silent.mp4": CARDS["INPUT/silent.mp4"]},
                   {"INPUT/silent.mp4": PROBES["INPUT/silent.mp4"]})
        try:
            wf_mod.WORKFLOWS["one_click_reel"].expand({}, ctx)
            assert False, "应抛 ValueError"
        except ValueError as exc:
            assert "废料" in str(exc)


def t07_speech_clean_skips_silent_and_no_audio():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        probes = dict(PROBES)
        probes["INPUT/a.mp4"] = {"duration": 12, "has_audio": False, "has_video": True}
        ctx = _ctx(tmp, CARDS, probes)
        plan, report = wf_mod.WORKFLOWS["speech_clean"].expand({}, ctx)
        srcs = [c["source"] for c in plan["clips"]]
        assert srcs == ["INPUT/b.mp4"], srcs            # a 无音轨、silent 全静音 → 跳过
        skipped = {s["source"]: s["reason"] for s in report["skipped"]}
        assert "无音轨" in skipped["INPUT/a.mp4"]
        assert "整体无声" in skipped["INPUT/silent.mp4"]
        assert plan["clips"][0]["cut_silence"]["noise_db"] == -35


def t08_compile_plan_with_workflow_end_to_end():
    """workflow 宏 → 完整编译（全 mock，无 ffmpeg 真跑）。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        _ctx(tmp, CARDS, PROBES)
        plan = {"schema_version": "2.0",
                "output": {"filename": "OUTPUT/oneclick.mp4",
                           "resolution": {"width": 640, "height": 360}},
                "workflow": {"name": "one_click_reel", "budget_seconds": 8}}
        result = plan_compiler.compile_plan(plan, tmp)
        assert result.expansions["workflow"]["name"] == "one_click_reel"
        culled_srcs = {c["source"] for c in result.expansions["workflow"]["culled"]}
        assert "INPUT/silent.mp4" in culled_srcs
        assert [c["id"] for c in result.plan["clips"]] == ["s00"]
        assert any(c.stage == "render" for c in result.commands)
        # 归一化产物走内容寻址缓存路径
        norm = [c for c in result.commands if c.stage == "normalize"]
        assert all("TMP/norm/" in c.argv[-1] for c in norm), norm


def t09_norm_cache_reuse_on_replan():
    """同参数二次编译：缓存文件已存在 → 归一化命令被跳过。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        _ctx(tmp, {"INPUT/a.mp4": CARDS["INPUT/a.mp4"]},
             {"INPUT/a.mp4": PROBES["INPUT/a.mp4"]})
        base = {"schema_version": "2.0",
                "output": {"filename": "OUTPUT/c.mp4",
                           "resolution": {"width": 640, "height": 360}},
                "clips": [{"id": "c1", "source": "INPUT/a.mp4", "kind": "video",
                           "trim_start": 0, "trim_end": 5}],
                "timeline": [{"clip": "c1"}]}
        r1 = plan_compiler.compile_plan(base, tmp)
        assert len([c for c in r1.commands if c.stage == "normalize"]) == 1
        # 模拟首次执行落盘了缓存产物
        os.makedirs(os.path.join(tmp, "TMP", "norm"), exist_ok=True)
        for c in r1.commands:
            if c.stage == "normalize":
                with open(os.path.join(tmp, c.argv[-1]), "wb") as f:
                    f.write(b"cached")
        r2 = plan_compiler.compile_plan(base, tmp)
        assert not [c for c in r2.commands if c.stage == "normalize"]
        # 改 trim → 键变 → 重新生成命令
        base["clips"][0]["trim_end"] = 6
        r3 = plan_compiler.compile_plan(base, tmp)
        assert len([c for c in r3.commands if c.stage == "normalize"]) == 1


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
    print(f"\nV7 workflow 离线测试：{total - failed}/{total} 项通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
