"""clip 级 audio.bgm:false（逐段排除全局 BGM）离线单测。

覆盖：schema 校验（放行 bool / 拒绝非 bool）、_bgm_holes 区间推导、
_mix_bgm 静音洞滤镜、无转场快速路径渲染命令的端到端滤镜串。

运行：.venv/Scripts/python.exe tests/test_clip_bgm_exclude.py
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

BGM = {"source": "MUSIC/m.mp3"}


def _make_project(tmp: str):
    os.makedirs(os.path.join(tmp, "INPUT"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "MUSIC"), exist_ok=True)
    with open(os.path.join(tmp, "INPUT", "a.mp4"), "wb") as f:
        f.write(b"x" * 64)
    with open(os.path.join(tmp, "MUSIC", "m.mp3"), "wb") as f:
        f.write(b"x" * 64)
    return tmp


def _clip(cid: str, trim_end: float = 4.0, **extra) -> dict:
    return {"id": cid, "source": "INPUT/a.mp4", "kind": "video",
            "trim_end": trim_end,
            "probe": {"duration": 10, "width": 1280, "height": 720,
                      "fps": 30, "has_audio": True, "has_video": True},
            **extra}


def _plan(tmp: str, clips: list[dict], audio: dict | None = BGM) -> dict:
    plan = {
        "schema_version": "2.0",
        "output": {"filename": "OUTPUT/o.mp4",
                   "resolution": {"width": 640, "height": 480}},
        "clips": clips,
        "timeline": [{"clip": c["id"]} for c in clips],
    }
    if audio:
        plan["audio"] = audio
    return plan


# ------------------------------------------------------------- schema ---- #

def t01_bgm_false_valid():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        errs = plan_schema.validate_plan(
            _plan(tmp, [_clip("c1", audio={"bgm": False})]), tmp)
        assert errs == [], errs


def t02_bgm_must_be_bool():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        for bad in ("no", 1, None):
            errs = plan_schema.validate_plan(
                _plan(tmp, [_clip("c1", audio={"bgm": bad})]), tmp)
            assert any("bgm" in e for e in errs), (bad, errs)


def t03_unknown_audio_key_still_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        errs = plan_schema.validate_plan(
            _plan(tmp, [_clip("c1", audio={"bgm": False, "nope": 1})]), tmp)
        assert any("nope" in e for e in errs), errs


def t04_mute_and_bgm_false_coexist():
    """mute(原声) 与 bgm:false(排 BGM) 互不冲突——组合 = 该片段整段无声。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        errs = plan_schema.validate_plan(
            _plan(tmp, [_clip("c1", audio={"mute": True, "bgm": False})]), tmp)
        assert errs == [], errs


# ------------------------------------------------------- 编译器挖洞 ---- #

def _three_clip_plan(tmp: str) -> dict:
    """c1[0-4] c2[4-12] c3[12-16]，c2 排 BGM。"""
    return _plan(tmp, [
        _clip("c1"),
        _clip("c2", trim_end=8.0, audio={"bgm": False}),
        _clip("c3"),
    ])


def t05_bgm_holes_ranges():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        plan = _three_clip_plan(tmp)
        math = plan_compiler._derive(plan)
        assert plan_compiler._bgm_holes(plan, math) == [(4.0, 12.0)]
        # 没有 bgm:false → 空表，混音链保持原样
        plan2 = _plan(tmp, [_clip("c1")])
        math2 = plan_compiler._derive(plan2)
        assert plan_compiler._bgm_holes(plan2, math2) == []


def t06_mix_bgm_appends_volume_enable():
    filters: list[str] = []
    lbl = plan_compiler._mix_bgm(filters, "[0:a]", dict(BGM), 1, 16.0,
                                 holes=[(4.0, 12.0)])
    # 缺省 original=mute：链照建，返回 [bg]
    assert lbl == "[bg]"
    assert "volume=0:enable='between(t,4.0,12.0)'" in filters[-1], filters
    # 不传 holes → 无 enable 表达式
    filters2: list[str] = []
    plan_compiler._mix_bgm(filters2, "[0:a]", dict(BGM), 1, 16.0)
    assert all("enable=" not in f for f in filters2), filters2


def t07_render_command_carries_hole():
    """端到端：无转场快速路径的渲染命令里带静音洞表达式。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        plan = _three_clip_plan(tmp)
        math = plan_compiler._derive(plan)
        commands, _ = plan_compiler._render_no_transition(plan, math)
        render = [c for c in commands if c.stage == "render"]
        assert render, commands
        argv_s = " ".join(render[0].argv)
        assert "volume=0:enable='between(t,4.0,12.0)'" in argv_s, argv_s


TESTS = [t01_bgm_false_valid, t02_bgm_must_be_bool,
         t03_unknown_audio_key_still_rejected, t04_mute_and_bgm_false_coexist,
         t05_bgm_holes_ranges, t06_mix_bgm_appends_volume_enable,
         t07_render_command_carries_hole]


def main() -> int:
    failed = 0
    print("clip.bgm:false 逐段排 BGM 离线测试：")
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
    print(f"\n逐段排 BGM 离线测试：{total - failed}/{total} 项通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
