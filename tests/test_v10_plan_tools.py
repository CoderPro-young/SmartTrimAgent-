"""V10 计划构建工具集离线单测：draft 状态 / 各工具正反例 / submit 端到端编译。

运行：.venv/Scripts/python.exe tests/test_v10_plan_tools.py
全部离线：不触网、不跑 ffmpeg（compile_plan 只生成命令不执行）。
"""

from __future__ import annotations

import os
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "video_editing"))

import content_analysis as ca   # noqa: E402
import plan_compiler           # noqa: E402
import plan_tools              # noqa: E402


def _shot(s, e, scene="草地", quality="good"):
    return {"start": s, "end": e,
            "label": {"person_count": 0, "scene": scene, "activity": "空镜",
                      "mood": "", "tags": [], "quality": quality,
                      "usable": quality != "poor", "desc": ""}}


def _card(rel, shots, dur=None):
    return {"schema_version": 2, "source": rel, "duration": dur,
            "signals": {"audio": {"silences": None, "silence_ratio": None},
                        "video": {"blacks": []}},
            "shots": shots}


def _make_project(tmp: str):
    os.makedirs(os.path.join(tmp, "INPUT"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "MUSIC"), exist_ok=True)
    for n in ("a.mp4", "b.mp4", "c.mp4", "img.jpg"):
        with open(os.path.join(tmp, "INPUT", n), "wb") as f:
            f.write(b"x" * 64)
    with open(os.path.join(tmp, "MUSIC", "m.mp3"), "wb") as f:
        f.write(b"x" * 32)
    ca._save_json(ca._sidecar_path(
        tmp, os.path.join(tmp, "INPUT/a.mp4"), "INPUT/a.mp4"),
        _card("INPUT/a.mp4", [_shot(0, 6, "日落"), _shot(6, 12, "海边")], 12))
    ca._save_json(ca._sidecar_path(
        tmp, os.path.join(tmp, "INPUT/b.mp4"), "INPUT/b.mp4"),
        _card("INPUT/b.mp4", [_shot(0, 10)], 10))
    # c.mp4 无卡
    return tmp


class _T:
    """每个测试独立的 draft 环境。"""

    def __init__(self, tmp):
        self.tmp = tmp
        plan_tools.PROJECT_ROOT = tmp
        plan_tools.reset_draft()

    def call(self, t, **kwargs):
        return t.invoke(kwargs)


TESTS = []


def t(fn):
    TESTS.append(fn)
    return fn


@t
def t01_new_plan_and_add_clips():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        h = _T(tmp)
        r = h.call(plan_tools.new_plan, filename="OUTPUT/x.mp4",
                   width=640, height=360)
        assert r.startswith("✓")
        # 非法画布
        r = h.call(plan_tools.new_plan, filename="x.mp4", width=641, height=360)
        assert "正偶数" in r
        # 未初始化时 add_clips 被拦
        plan_tools.reset_draft()
        assert "先调用 new_plan" in h.call(
            plan_tools.add_clips, clips=[{"source": "INPUT/a.mp4", "shot": 1}])
        h.call(plan_tools.new_plan, filename="OUTPUT/x.mp4", width=640, height=360)
        r = h.call(plan_tools.add_clips, clips=[
            {"source": "INPUT/a.mp4", "shot": 1},
            {"source": "INPUT/b.mp4", "shot": 1},
            {"source": "INPUT/img.jpg", "duration": 3},
        ])
        assert r.count("✓") == 3, r
        plan = plan_tools._STATE["plan"]
        assert [c["id"] for c in plan["clips"]] == ["c1", "c2", "c3"]
        assert [t["clip"] for t in plan["timeline"]] == ["c1", "c2", "c3"]
        assert plan["clips"][2]["kind"] == "image"          # kind 自动判定
        assert "✗" not in r.split("draft「")[0]
        assert "3 片段" in r and "c1  a.mp4 #1" in r


@t
def t02_add_clips_item_level_rejects():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        h = _T(tmp)
        h.call(plan_tools.new_plan, filename="OUTPUT/x.mp4", width=640, height=360)
        # 批量中坏项被逐条拒绝，好项照常入库
        r = h.call(plan_tools.add_clips, clips=[
            {"source": "INPUT/nope.mp4", "shot": 1},          # 素材不存在
            {"source": "INPUT/c.mp4", "shot": 1},             # 无卡
            {"source": "INPUT/a.mp4", "shot": 9},             # 编号超范围
            {"source": "INPUT/a.mp4", "shot": 1, "trim_start": 0, "trim_end": 3},  # 互斥
            {"source": "INPUT/img.jpg"},                      # 图片缺 duration
            {"source": "INPUT/a.mp4", "shot": 2},             # ✓
        ])
        assert "添加 1/6" in r, r
        plan = plan_tools._STATE["plan"]
        assert len(plan["clips"]) == 1 and plan["clips"][0]["id"] == "c1"
        assert "不存在" in r and "analyze_media" in r and "共 2 个镜头" in r \
            and "互斥" in r and "duration" in r


@t
def t03_clip_and_effect_ops():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        h = _T(tmp)
        h.call(plan_tools.new_plan, filename="OUTPUT/x.mp4", width=640, height=360)
        h.call(plan_tools.add_clips, clips=[{"source": "INPUT/a.mp4", "shot": 1}])
        # set_clip：shot 重引用
        r = h.call(plan_tools.set_clip, clip="c1", shot=2)
        assert r.startswith("✓") and "#2" in r
        c1 = plan_tools._STATE["plan"]["clips"][0]
        assert c1["shot"] == 2 and "trim_start" not in c1
        # set_clip：id 大小写敏感
        assert "不存在" in h.call(plan_tools.set_clip, clip="C1", mute=True)
        # set_effect：参数非法被拦
        r = h.call(plan_tools.set_effect, clip="c1", name="speed",
                   effect_args={"factor": 9})
        assert r.startswith("✗"), r
        r = h.call(plan_tools.set_effect, clip="c1", name="speed",
                   effect_args={"factor": 0.6})
        assert r.startswith("✓")
        # 同名覆盖 = 改参数
        h.call(plan_tools.set_effect, clip="c1", name="speed", effect_args={"factor": 1.5})
        effs = plan_tools._STATE["plan"]["clips"][0]["effects"]
        assert len(effs) == 1 and effs[0]["args"]["factor"] == 1.5
        # 未知效果名
        assert "白名单" in h.call(plan_tools.set_effect, clip="c1", name="superzoom")
        # remove_effect
        assert h.call(plan_tools.remove_effect, clip="c1", name="speed").startswith("✓")
        assert plan_tools._STATE["plan"]["clips"][0]["effects"] == []


@t
def t04_remove_move_transition_audio_text():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        h = _T(tmp)
        h.call(plan_tools.new_plan, filename="OUTPUT/x.mp4", width=640, height=360)
        h.call(plan_tools.add_clips, clips=[
            {"source": "INPUT/a.mp4", "shot": 1},
            {"source": "INPUT/b.mp4", "shot": 1},
            {"source": "INPUT/img.jpg", "duration": 3},
        ])
        # 首片段无前置转场
        assert "首个片段" in h.call(plan_tools.set_transition, clip="c1",
                                    type="fade", duration=0.5)
        assert h.call(plan_tools.set_transition, clip="c2", type="fade",
                      duration=0.4).startswith("✓")
        # 硬切
        h.call(plan_tools.set_transition, clip="c3", type="none")
        tl = plan_tools._STATE["plan"]["timeline"]
        assert tl[1]["transition"] == {"type": "fade", "duration": 0.4}
        assert "transition" not in tl[2] or tl[2].get("transition") is None
        # move 到最前
        h.call(plan_tools.move_clip, clip="c3")
        assert [t["clip"] for t in plan_tools._STATE["plan"]["timeline"]] \
            == ["c3", "c1", "c2"]
        # set_audio：源不存在
        assert "不存在" in h.call(plan_tools.set_audio, source="MUSIC/none.mp3")
        assert h.call(plan_tools.set_audio, source="MUSIC/m.mp3",
                      snap_beats=True).startswith("✓")
        assert plan_tools._STATE["plan"]["audio"]["snap_beats"] is True
        assert h.call(plan_tools.clear_audio).startswith("✓")
        assert "audio" not in plan_tools._STATE["plan"]
        # add_text / remove_overlay
        h.call(plan_tools.add_text, text="标题", at_clip="c1", duration=2)
        assert len(plan_tools._STATE["plan"]["overlays"]) == 1
        assert "超范围" in h.call(plan_tools.remove_overlay, index=5)
        assert h.call(plan_tools.remove_overlay, index=0).startswith("✓")
        # remove_clip 连带清挂载文字
        h.call(plan_tools.add_text, text="t", at_clip="c2", duration=1)
        r = h.call(plan_tools.remove_clip, clip="c2")
        assert "连带移除 1 条" in r
        assert all(c["id"] != "c2" for c in plan_tools._STATE["plan"]["clips"])


@t
def t05_narration_and_captions():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        h = _T(tmp)
        h.call(plan_tools.new_plan, filename="OUTPUT/x.mp4", width=640, height=360)
        h.call(plan_tools.add_clips, clips=[
            {"source": "INPUT/a.mp4", "shot": 1},
            {"source": "INPUT/a.mp4", "shot": 2},
        ])
        r = h.call(plan_tools.set_narration, groups=[
            {"clip_ids": ["c1", "nope"], "units": ["a。"]}], title="t")
        assert "不存在" in r
        r = h.call(plan_tools.set_narration, title="草原", groups=[
            {"clip_ids": ["c1"], "text": "第一组。第二句。",
             "units": ["第一组。", "第二句。"]},
            {"clip_ids": ["c2"], "text": "结尾。"}])
        assert r.startswith("✓") and "2 组" in r
        plan = plan_tools._STATE["plan"]
        assert plan["_title"] == "草原" and len(plan["_narration"]) == 2
        # captions 覆盖 narration（互斥）
        h.call(plan_tools.set_captions, lines=["一", "二"], style="credits")
        assert "_narration" not in plan and plan["_captions"] == ["一", "二"]


@t
def t06_validate_and_submit_gate():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        h = _T(tmp)
        h.call(plan_tools.new_plan, filename="OUTPUT/x.mp4", width=640, height=360)
        h.call(plan_tools.add_clips, clips=[
            {"source": "INPUT/a.mp4", "shot": 1},
            {"source": "INPUT/a.mp4", "shot": 2},
        ])
        # 转场时长 ≥ 片段长度：单步各自合法（转场 4s < 5s、片段 6s），
        # set_clip 把片段剪到 2s 后才构成违规——全量试算拦截
        assert h.call(plan_tools.set_transition, clip="c2", type="fade",
                      duration=4).startswith("✓")
        assert h.call(plan_tools.set_clip, clip="c1", shot=None,
                      trim_start=0, trim_end=2).startswith("✓")
        r = h.call(plan_tools.validate_plan)
        assert r.startswith("试算发现"), r
        r = h.call(plan_tools.submit)
        assert r.startswith("✗ submit 被拦下"), r
        assert plan_tools.take_submitted() is None      # 未真正提交
        # 修正后放行（转场缩到 0.4s）
        h.call(plan_tools.set_transition, clip="c2", type="fade", duration=0.4)
        assert h.call(plan_tools.validate_plan).startswith("✓")
        assert h.call(plan_tools.submit).startswith("✓ 已提交")
        plan = plan_tools.take_submitted()
        assert plan is not None and plan["output"]["filename"] == "OUTPUT/x.mp4"
        assert plan_tools.take_submitted() is None      # 消费型


@t
def t07_submit_compiles_end_to_end():
    """draft（shot 引用 + 转场 + BGM + 旁白）→ compile_plan 全链。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        h = _T(tmp)
        h.call(plan_tools.new_plan, filename="OUTPUT/o.mp4", width=640, height=360)
        h.call(plan_tools.add_clips, clips=[
            {"source": "INPUT/a.mp4", "shot": 1},
            {"source": "INPUT/b.mp4", "shot": 1},
            {"source": "INPUT/img.jpg", "duration": 3},
        ])
        h.call(plan_tools.set_transition, clip="c2", type="fade", duration=0.4)
        h.call(plan_tools.set_transition, clip="c3", type="fade", duration=0.4)
        h.call(plan_tools.set_audio, source="MUSIC/m.mp3")
        h.call(plan_tools.set_narration, title="傍晚", groups=[
            {"clip_ids": ["c1", "c2"], "text": "一句。两句。"},
            {"clip_ids": ["c3"], "text": "收尾。"}])
        assert h.call(plan_tools.submit).startswith("✓")
        plan = plan_tools.take_submitted()
        result = plan_compiler.compile_plan(plan, tmp)
        # shot 引用被展开成卡上边界
        c1 = next(c for c in result.plan["clips"] if c["id"] == "c1")
        assert (c1["trim_start"], c1["trim_end"]) == (0, 6)
        assert result.expansions["shots"]["resolved"] == 2
        # 旁白铺成字幕 overlay；时间轴数学按展开后时长
        assert result.expansions["captions"]["title"] == "傍晚"
        assert abs(result.math["durations"]["c1"] - 6.0) < 1e-6
        # 全部命令生成（不执行）
        assert any(c.stage == "normalize" for c in result.commands)


@t
def t08_retry_keeps_draft():
    """重试不重置 draft：has_draft_activity 持续为真，模型可增量修正。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        h = _T(tmp)
        plan_tools.reset_draft()
        assert not plan_tools.has_draft_activity()
        h.call(plan_tools.new_plan, filename="OUTPUT/x.mp4", width=640, height=360)
        assert plan_tools.has_draft_activity()
        # 模拟宿主重试轮：不 reset，draft 保留
        h.call(plan_tools.add_clips, clips=[{"source": "INPUT/a.mp4", "shot": 1}])
        assert len(plan_tools._STATE["plan"]["clips"]) == 1


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
    print(f"\nV10 计划工具集离线测试：{len(TESTS) - failed}/{len(TESTS)} 项通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
