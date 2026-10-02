"""V7.4 文案可插拔提供器 + 文案驱动时长 离线单测。

运行：.venv/Scripts/python.exe tests/test_v74_caption_module.py
"""

from __future__ import annotations

import os
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "video_editing"))

import plan_compiler   # noqa: E402
import plan_schema     # noqa: E402
import workflow as wf_mod  # noqa: E402


def _shot(s, e, scene="草地", quality="good"):
    return {"start": s, "end": e,
            "label": {"person_count": 0, "scene": scene, "activity": "空镜",
                      "mood": "", "tags": [], "quality": quality, "usable": quality != "poor"}}


def _card(rel, shots, dur=None):
    return {"schema_version": 2, "source": rel, "duration": dur,
            "signals": {"audio": {"silences": None, "silence_ratio": None},
                        "video": {"blacks": []}},
            "shots": shots}


def _make_project(tmp: str, names=("a.mp4",)):
    os.makedirs(os.path.join(tmp, "INPUT"), exist_ok=True)
    for n in names:
        with open(os.path.join(tmp, "INPUT", n), "wb") as f:
            f.write(b"x" * 64)
    return tmp


def _ctx(tmp, cards):
    import content_analysis as ca
    for rel, card in cards.items():
        full = os.path.join(tmp, rel)
        ca._save_json(ca._sidecar_path(tmp, full, rel), card)
    probes = {rel: {"duration": c.get("duration"), "has_audio": True,
                    "has_video": True} for rel, c in cards.items()}
    return {"cards": cards, "probes": probes, "project_root": tmp}


def _two_shot_cards():
    return {"INPUT/a.mp4": _card("INPUT/a.mp4",
                                 [_shot(0, 6, "日落"), _shot(6, 12, "海边")], dur=12)}


CREDITS_LINES = ["内蒙古之旅", "【着重鸣谢】", "中国摄影协会", "【交通支持】",
                 "上海航空、厦门航空", "【美食支持】", "烤羊排、锅包肉",
                 "【特别感谢】", "勇敢出发的自己"]          # 9 行 × 1.2s = 10.8s


def t01_provider_chain_skips_llm_for_plan_captions():
    """计划自带文案 → plan 提供器命中，llm 提供器根本不被调用。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        ctx = _ctx(tmp, _two_shot_cards())
        called = {"llm": 0}
        orig = wf_mod._write_captions

        def _spy(picked, kw=""):
            called["llm"] += 1
            return [f"shot{i}" for i in range(1, len(picked) + 1)], "llm"
        wf_mod._write_captions = _spy
        try:
            plan, report = wf_mod.WORKFLOWS["smart_create"].expand(
                {"subtitle_style": "credits", "captions": CREDITS_LINES}, ctx)
            assert report["captions_source"] == "plan"
            assert plan["_captions"] == CREDITS_LINES
            assert called["llm"] == 0, "自带文案时不得调用生成模块"
            # 无自带文案 → llm 被调用
            plan2, report2 = wf_mod.WORKFLOWS["smart_create"].expand(
                {"subtitle_style": "credits"}, ctx)
            assert called["llm"] == 1
            assert report2["captions_source"] == "llm"
        finally:
            wf_mod._write_captions = orig


def t02_captions_drive_credits_duration():
    """credits + 自带文案 + 未给预算：D = 行数 × seconds_per_line（精确对齐）。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        ctx = _ctx(tmp, _two_shot_cards())
        plan, report = wf_mod.WORKFLOWS["smart_create"].expand(
            {"subtitle_style": "credits", "transition": 0.4,
             "captions": CREDITS_LINES}, ctx)              # 9 行 × 1.2 = 10.8
        assert report["duration_source"] == "captions"
        assert report["target_seconds"] == 10.8
        # 软预算装进 6+6，末段收短 0.8s 精确对齐
        ds = [round(c["trim_end"] - c["trim_start"], 3) for c in plan["clips"]]
        assert ds == [6.0, 5.2], ds
        result = plan_compiler.compile_plan(
            {"schema_version": "2.0",
             "output": {"filename": "OUTPUT/o.mp4",
                        "resolution": {"width": 640, "height": 360}},
             "clips": plan["clips"], "timeline": plan["timeline"]}, tmp)
        assert result.math["D"] == 10.8, result.math["D"]


def t03_seconds_per_line_override():
    """seconds_per_line 调快节奏：9 行 × 0.5 = 4.5s，单镜收到 4.5s。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        ctx = _ctx(tmp, _two_shot_cards())
        plan, report = wf_mod.WORKFLOWS["smart_create"].expand(
            {"subtitle_style": "credits", "transition": 0.4,
             "seconds_per_line": 0.5, "captions": CREDITS_LINES}, ctx)
        assert report["target_seconds"] == 4.5
        ds = [round(c["trim_end"] - c["trim_start"], 3) for c in plan["clips"]]
        assert ds == [4.5], ds                             # 单镜直接收齐


def t04_undershoot_reported_honestly():
    """素材装不满目标（镜头太长/太少）时接受欠额，不虚报。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        cards = {"INPUT/a.mp4": _card("INPUT/a.mp4", [_shot(0, 8, "日落")], dur=8)}
        ctx = _ctx(tmp, cards)
        plan, report = wf_mod.WORKFLOWS["smart_create"].expand(
            {"subtitle_style": "credits", "transition": 0.4,
             "captions": CREDITS_LINES}, ctx)              # 目标 10.8，只有 8s 素材
        assert report["duration_source"] == "captions"
        assert report["target_seconds"] == 10.8
        result = plan_compiler.compile_plan(
            {"schema_version": "2.0",
             "output": {"filename": "OUTPUT/o.mp4",
                        "resolution": {"width": 640, "height": 360}},
             "clips": plan["clips"], "timeline": plan["timeline"]}, tmp)
        assert result.math["D"] == 8.0                     # 欠额如实呈现


def t05_explicit_budget_wins():
    """显式给 budget_seconds 时预算优先（文案仍整块铺满实际时长）。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        ctx = _ctx(tmp, _two_shot_cards())
        plan, report = wf_mod.WORKFLOWS["smart_create"].expand(
            {"subtitle_style": "credits", "transition": 0,
             "budget_seconds": 8, "captions": CREDITS_LINES}, ctx)
        assert report["duration_source"] == "budget"
        assert report["target_seconds"] is None
        result = plan_compiler.compile_plan(
            {"schema_version": "2.0",
             "output": {"filename": "OUTPUT/o.mp4",
                        "resolution": {"width": 640, "height": 360}},
             "clips": plan["clips"], "timeline": plan["timeline"]}, tmp)
        assert result.math["D"] == 6.0                     # 预算内装 6s


def t06_fit_drops_last_clip_when_trim_below_floor():
    """末段收不进 1.5s 下限时，取「收到下限/整段丢弃」中更贴近目标者。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp, ("a.mp4", "b.mp4"))
        cards = {"INPUT/a.mp4": _card("INPUT/a.mp4",
                                      [_shot(0, 6, "日落"), _shot(6, 12, "海边")], dur=12),
                 "INPUT/b.mp4": _card("INPUT/b.mp4",
                                      [_shot(0, 2, "草地"), _shot(2, 4, "草原")], dur=4)}
        ctx = _ctx(tmp, cards)
        plan, _ = wf_mod.WORKFLOWS["smart_create"].expand(
            {"subtitle_style": "credits", "transition": 0.4,
             "captions": CREDITS_LINES}, ctx)
        # 软预算装进 6+2+6+2（proj 14.8）→ 末段 2s 收不进 → 丢弃更贴近 → 3 段 13.2
        assert len(plan["clips"]) == 3, plan["clips"]
        result = plan_compiler.compile_plan(
            {"schema_version": "2.0",
             "output": {"filename": "OUTPUT/o.mp4",
                        "resolution": {"width": 640, "height": 360}},
             "clips": plan["clips"], "timeline": plan["timeline"]}, tmp)
        assert result.math["D"] == 13.2, result.math["D"]


def t07_bottom_style_not_caption_driven():
    """文案驱动时长只属于 credits；bottom 缺省预算走 DEFAULT_BUDGET。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        ctx = _ctx(tmp, _two_shot_cards())
        plan, report = wf_mod.WORKFLOWS["smart_create"].expand(
            {"transition": 0, "captions": ["一句", "两句"]}, ctx)
        assert report["duration_source"] == "default"
        assert report["target_seconds"] is None
        assert report["subtitle_style"] == "bottom"


def t08_schema_seconds_per_line():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        base = {"schema_version": "2.0",
                "output": {"filename": "OUTPUT/o.mp4",
                           "resolution": {"width": 640, "height": 360}},
                "workflow": {"name": "smart_create"}}
        p = dict(base)
        p["workflow"] = {"name": "smart_create", "seconds_per_line": 0.3}
        assert any("seconds_per_line" in e
                   for e in plan_schema.validate_plan(p, tmp))
        p["workflow"] = {"name": "smart_create", "seconds_per_line": 6.0}
        assert any("seconds_per_line" in e
                   for e in plan_schema.validate_plan(p, tmp))
        p["workflow"] = {"name": "one_click_reel", "seconds_per_line": 1.0}
        assert any("smart_create" in e
                   for e in plan_schema.validate_plan(p, tmp))
        p["workflow"] = {"name": "smart_create", "seconds_per_line": 2.0}
        assert plan_schema.validate_plan(p, tmp) == []


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
    print(f"\nV7.4 caption module 离线测试：{total - failed}/{total} 项通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
