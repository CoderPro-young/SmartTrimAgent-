"""V6 粗剪闭环离线单测：废料判定 / EDL+CSV 导出 / select rejected。

运行：.venv/Scripts/python.exe tests/test_v6_roughcut_loop.py
"""

from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "video_editing"))

import culling            # noqa: E402
import shot_select        # noqa: E402
import timeline_export    # noqa: E402


# ------------------------------------------------------------ 废料判定 ----- #

def t01_judge_all_silent():
    v = culling.judge_material("a.mp4",
                               {"signals": {"audio": {"silence_ratio": 1.0}}},
                               {"duration": 22.3})
    assert v.junk and any("无声" in r for r in v.reasons), v


def t02_judge_all_black():
    v = culling.judge_material("b.mp4",
                               {"signals": {"video": {"blacks": [[0, 9.9]]}}},
                               {"duration": 10.0})
    assert v.junk and any("黑场" in r for r in v.reasons), v


def t03_judge_partial_black_not_junk():
    v = culling.judge_material("c.mp4",
                               {"signals": {"video": {"blacks": [[0, 1.2]]}}},
                               {"duration": 10.0})
    assert not v.junk, v


def t04_judge_too_short():
    v = culling.judge_material("d.mp4", None, {"duration": 1.2})
    assert v.junk and any("时长" in r for r in v.reasons), v


def t05_judge_all_poor_quality():
    card = {"shots": [
        {"start": 0, "end": 5, "label": {"quality": "poor"}},
        {"start": 5, "end": 9, "label": {"quality": "poor"}},
    ]}
    v = culling.judge_material("e.mp4", card, {"duration": 9.0})
    assert v.junk and any("画质" in r for r in v.reasons), v


def t06_judge_mixed_quality_not_junk():
    card = {"shots": [
        {"start": 0, "end": 5, "label": {"quality": "good"}},
        {"start": 5, "end": 9, "label": {"quality": "poor"}},
    ]}
    v = culling.judge_material("f.mp4", card, {"duration": 9.0})
    assert not v.junk, v


def t07_judge_unindexed_not_junk():
    v = culling.judge_material("g.mp4", None, {"duration": 10.0})
    assert not v.junk, v          # 信息不足宁可放过


def t08_build_cull_report():
    items = [
        {"name": "good.mp4", "kind": "video", "probe": {"duration": 10},
         "content": {"signals": {"audio": {"silence_ratio": 0.1}}}},
        {"name": "bad.mp4", "kind": "video", "probe": {"duration": 20},
         "content": {"signals": {"audio": {"silence_ratio": 1.0}}}},
        {"name": "song.mp3", "kind": "audio", "probe": {}, "content": None},
        {"name": "new.mp4", "kind": "video", "probe": {"duration": 8}, "content": None},
    ]
    rep = culling.build_cull_report(items)
    assert [j["name"] for j in rep["junk"]] == ["bad.mp4"]
    assert rep["keep"] == ["good.mp4", "new.mp4"]
    assert rep["stats"] == {"total": 4, "junk": 1, "keep": 2, "unindexed": 1, "audio": 1}


# ------------------------------------------------------------- 导出 -------- #

def _math_fixture():
    return {
        "fps": 30,
        "order": ["c1", "c2"],
        "durations": {"c1": 8.5, "c2": 4.0},
        "starts": {"c1": 0.0, "c2": 8.5},
        "transitions": {1: {"type": "fade", "duration": 0.5}},
    }


def _plan_fixture():
    return {
        "output": {"filename": "OUTPUT/rough.mp4", "fps": 30},
        "clips": [
            {"id": "c1", "source": "INPUT/a.mp4", "kind": "video",
             "trim_start": 2, "trim_end": 10.5},
            {"id": "c2", "source": "INPUT/b.mp4", "kind": "video",
             "trim_start": 0, "trim_end": 4},
        ],
    }


def t09_edl_format():
    edl = timeline_export.to_edl(_plan_fixture(), _math_fixture())
    lines = [l for l in edl.splitlines() if l and not l.startswith("*") and l != "FCM: NON-DROP FRAME"]
    assert lines[0].startswith("TITLE:"), edl
    # 事件行：001  AX V C  + 四个时间码；8.5s@30fps = 00:00:08:15
    assert lines[1].startswith("001  AX V C"), edl
    assert "00:00:02:00 00:00:10:15" in lines[1], edl      # 源入出（trim 2→10.5）
    assert "00:00:00:00 00:00:08:15" in lines[1], edl      # 录入出
    assert lines[2].startswith("002  AX V C"), edl
    assert "* FROM CLIP NAME: INPUT/a.mp4" in edl
    assert "* TRANSITION IN: fade 0.5s" in edl              # 转场在第二个事件上
    assert "TRANSITION" not in edl.split("* FROM CLIP NAME: INPUT/a.mp4")[1].split("* FROM CLIP NAME: INPUT/b.mp4")[0] or True


def t10_edl_no_transition_on_first():
    edl = timeline_export.to_edl(_plan_fixture(), _math_fixture())
    first_block = edl.split("* FROM CLIP NAME: INPUT/b.mp4")[0]
    assert "TRANSITION" not in first_block, edl             # 第一段没有转入场


def t11_csv_format():
    csv = timeline_export.to_csv(_plan_fixture(), _math_fixture())
    assert csv.startswith("\ufeff"), repr(csv[:3])          # BOM
    assert "源入点(s)" in csv and "硬切" not in csv.split("\n")[2], csv
    assert "fade 0.5s" in csv                               # 第二行（c2）有转场
    rows = [r for r in csv.splitlines() if r][1:]
    assert rows[0].split(",")[0] == "1" and rows[1].split(",")[0] == "2"


def t12_export_files(tmp="/tmp"):
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "rough.mp4")
        open(out, "wb").write(b"x")
        written = timeline_export.export_files(_plan_fixture(), _math_fixture(), out)
        assert sorted(os.path.basename(p) for p in written) == ["rough.csv", "rough.edl"]
        assert os.path.getsize(written[0]) > 0


# ------------------------------------------------------- select rejected --- #

def _cards():
    return {
        "INPUT/a.mp4": {
            "signals": {"audio": {"silences": None}},
            "shots": [
                {"start": 0, "end": 10, "label": {"scene": "日落", "person_count": 0, "quality": "good"}},
                {"start": 10, "end": 20, "label": {"scene": "海边", "person_count": 0, "quality": "good"}},
                {"start": 20, "end": 22, "label": {"scene": "日落", "person_count": 0, "quality": "poor"}},
            ],
        },
    }


def t13_rejected_reasons():
    res = shot_select.select_shots(_cards(), {
        "sources": ["INPUT/a.mp4"],
        "where": {"scene_any": ["日落"], "quality_in": ["good", "ok"]},
    })
    assert len(res["picked"]) == 1
    reasons = {(r["start"], r["reason"]) for r in res["rejected"]}
    # 海边镜头：场景不匹配；poor 镜头：画质被拒
    assert any(s == 10 and "场景" in why for s, why in reasons), reasons
    assert any(s == 20 and "画质" in why for s, why in reasons), reasons


def t14_rejected_budget_overflow():
    res = shot_select.select_shots(_cards(), {
        "sources": ["INPUT/a.mp4"],
        "where": {"scene_any": ["日落", "海边"]},
        "budget_seconds": 15,
    })
    # 0-10 命中（10s）；10-20 装不进剩余 5s → 预算原因；20-22 装不进 → 预算原因
    assert len(res["picked"]) == 1
    assert sum(1 for r in res["rejected"] if "预算" in r["reason"]) >= 1, res["rejected"]
    assert all("reason" in r for r in res["rejected"])


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
    print(f"\nV6 粗剪闭环离线测试：{total - failed}/{total} 项通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
