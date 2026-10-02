"""V7.3 致谢滚动字幕（smart_create.subtitle_style="credits"）离线单测。

运行：.venv/Scripts/python.exe tests/test_v73_credits.py
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


def _shot(s, e, scene="草地", quality="good", pc=0):
    return {"start": s, "end": e,
            "label": {"person_count": pc, "scene": scene, "activity": "空镜",
                      "mood": "", "tags": [], "quality": quality, "usable": quality != "poor"}}


def _card(rel, shots, dur=None):
    return {"schema_version": 2, "source": rel, "duration": dur,
            "signals": {"audio": {"silences": None, "silence_ratio": None},
                        "video": {"blacks": []}},
            "shots": shots}


def _make_project(tmp: str):
    os.makedirs(os.path.join(tmp, "INPUT"), exist_ok=True)
    for n in ("a.mp4", "b.mp4"):
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


CARDS = {
    "INPUT/a.mp4": _card("INPUT/a.mp4",
                         [_shot(0, 6, "日落"), _shot(6, 12, "海边")], dur=12),
    "INPUT/b.mp4": _card("INPUT/b.mp4", [_shot(0, 8, "草地")], dur=8),
}

CREDITS_LINES = ["【着重鸣谢】", "内蒙古之旅", "【交通支持】", "上海航空、厦门航空",
                 "【美食支持】", "烤羊排、锅包肉", "【特别感谢】", "勇敢出发的自己"]


def _plan(tmp, wf_extra, out="OUTPUT/credits.mp4"):
    return {"schema_version": "2.0",
            "output": {"filename": out,
                       "resolution": {"width": 640, "height": 360}},
            "workflow": {"name": "smart_create", "budget_seconds": 12,
                         "transition": 0, **wf_extra}}


def t01_schema_rejects_hallucinated_style():
    """杜撰的样式名必须被打回，且报错带合法值清单。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        errs = plan_schema.validate_plan(_plan(tmp, {"subtitle_style": "movie_credits"}), tmp)
        assert any("subtitle_style" in e and "bottom/credits" in e for e in errs), errs
        # 非 smart_create 的宏不消费该参数
        p = _plan(tmp, {})
        p["workflow"]["name"] = "one_click_reel"
        p["workflow"]["subtitle_style"] = "credits"
        errs = plan_schema.validate_plan(p, tmp)
        assert any("smart_create" in e for e in errs), errs
        # 合法值 + 合法宏 → 通过
        assert plan_schema.validate_plan(
            _plan(tmp, {"subtitle_style": "credits", "captions": CREDITS_LINES}), tmp) == []


def t02_captions_limit_relaxed_for_credits():
    """credits 致谢体一行就是一行，行数上限放宽到 60；普通样式维持 12。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        many = [f"第{i}行" for i in range(20)]
        p = _plan(tmp, {"subtitle_style": "credits", "captions": many})
        assert plan_schema.validate_plan(p, tmp) == []
        p2 = _plan(tmp, {"captions": many})
        errs = plan_schema.validate_plan(p2, tmp)
        assert any("captions" in e for e in errs), errs


def t03_expansion_passes_style_through():
    """宏展开把 subtitle_style 装进暂存键，report 记录来源。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        ctx = _ctx(tmp, {"INPUT/a.mp4": CARDS["INPUT/a.mp4"]})
        plan, report = wf_mod.WORKFLOWS["smart_create"].expand(
            {"subtitle_style": "credits", "budget_seconds": 6}, ctx)
        assert plan["_caption_style"] == "credits"
        assert report["subtitle_style"] == "credits"
        assert plan["_captions"]                      # 兜底文案仍在
        # 缺省 → bottom
        plan2, report2 = wf_mod.WORKFLOWS["smart_create"].expand(
            {"budget_seconds": 6}, ctx)
        assert plan2["_caption_style"] == "bottom"
        assert report2["subtitle_style"] == "bottom"


def _compile_credits(tmp, transition):
    cards = {"INPUT/a.mp4": CARDS["INPUT/a.mp4"]}
    _ctx(tmp, cards)
    plan = _plan(tmp, {"subtitle_style": "credits", "transition": transition,
                       "captions": CREDITS_LINES})
    return plan_compiler.compile_plan(plan, tmp)


def t04_credits_compiles_to_single_scrolling_overlay():
    """credits：全部文案合并为一整块跨全片 overlay，滚动表达式进渲染命令。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        result = _compile_credits(tmp, transition=0)   # 无转场路径
        assert result.math["D"] == 12.0
        ovs = result.plan.get("overlays") or []
        assert len(ovs) == 1, ovs
        ov = ovs[0]
        assert ov["scroll"] == "up" and ov["type"] == "text"
        assert ov["duration"] == 12.0 and ov["start_offset"] == 0.0
        assert ov["text"] == "\n".join(CREDITS_LINES)
        assert ov["line_spacing"] > 0 and ov["stroke"] > 0
        # 暂存键被消费
        assert "_captions" not in result.plan and "_caption_style" not in result.plan
        # 渲染命令带滚动 y 表达式与描边；链末强制 yuv420p（xfade 会协商成 444）
        render = next(c for c in result.commands if c.stage == "render")
        arg = " ".join(render.argv)
        assert "h-(h+text_h)*(t-0.0)/12.0" in arg, arg[:400]
        assert "borderw=2" in arg and "line_spacing=" in arg
        assert "format=yuv420p[vout]" in arg, arg[:400]
        # 滚动文本作为 textfile sidecar 落盘（多行完整）
        txt = [v for p, v in result.sidecars.items()
               if p.startswith("TMP/txt_")]
        assert txt and txt[0] == ov["text"]


def t05_credits_with_transition_path():
    """转场路径同样产出滚动滤镜（xfade 链之后应用）。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        result = _compile_credits(tmp, transition=0.4)
        ovs = result.plan.get("overlays") or []
        assert len(ovs) == 1 and ovs[0]["scroll"] == "up"
        render = next(c for c in result.commands if c.stage == "render")
        arg = " ".join(render.argv)
        assert "xfade" in arg and "h-(h+text_h)" in arg, arg[:400]
        assert "format=yuv420p[vout]" in arg, arg[:400]


def t06_default_bottom_unchanged():
    """不传 subtitle_style：行为与 V7.2 完全一致（逐片段绑定，无滚动字段）。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        _ctx(tmp, {"INPUT/a.mp4": CARDS["INPUT/a.mp4"]})
        plan = _plan(tmp, {"captions": ["山间日落", "海边散步"]})
        result = plan_compiler.compile_plan(plan, tmp)
        ovs = result.plan.get("overlays") or []
        assert len(ovs) == 2
        assert all("scroll" not in o and o["position"] == "bottom" for o in ovs)
        render = next(c for c in result.commands if c.stage == "render")
        assert "text_h)*(t-" not in " ".join(render.argv)


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
    print(f"\nV7.3 credits 离线测试：{total - failed}/{total} 项通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
