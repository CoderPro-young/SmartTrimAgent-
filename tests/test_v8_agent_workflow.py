"""V8.0 创作流 agent 化离线单测：list_shots / list_music 工具、agent 手写文案键
（_narration/_captions/_caption_style/_title）的 schema 校验与编译端到端、
粗筛取消后的宏兜底行为。

运行：.venv/Scripts/python.exe tests/test_v8_agent_workflow.py
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
import plan_schema             # noqa: E402
import video_agent as va       # noqa: E402
import workflow as wf_mod      # noqa: E402


def _shot(s, e, scene="草地", quality="good", pc=0, desc=""):
    return {"start": s, "end": e,
            "label": {"person_count": pc, "scene": scene, "activity": "空镜",
                      "mood": "", "tags": [], "quality": quality,
                      "usable": quality != "poor", "desc": desc}}


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
    os.makedirs(os.path.join(tmp, "MUSIC"), exist_ok=True)
    for n in ("a.mp4", "b.mp4", "c.mp4", "narration.mp3"):
        with open(os.path.join(tmp, "INPUT", n), "wb") as f:
            f.write(b"x" * 64)
    with open(os.path.join(tmp, "MUSIC", "x.mp3"), "wb") as f:
        f.write(b"x" * 32)
    ca._save_json(ca._sidecar_path(
        tmp, os.path.join(tmp, "INPUT/a.mp4"), "INPUT/a.mp4"),
        _card("INPUT/a.mp4",
              [_shot(0, 6, "日落", desc="穿红裙的女孩走向金色草地深处，风把裙摆吹起"),
               _shot(6, 12, "海边", quality="poor")], dur=12))
    ca._save_json(ca._sidecar_path(
        tmp, os.path.join(tmp, "INPUT/b.mp4"), "INPUT/b.mp4"),
        _card("INPUT/b.mp4", [_shot(0, 10, "草地")],
              silences=[[0, 10]], dur=10))
    # c.mp4 不建卡 → missing 提示
    return tmp


def _patch_agent_paths(tmp: str):
    va.PROJECT_ROOT = tmp
    va.INPUT_DIR = os.path.join(tmp, "INPUT")


# ------------------------------------------------------------ 工具 ------- #

def t01_list_shots_full_pool():
    """全量镜头池：无粗筛无预过滤；⚠ 只标注不拦截；音频跳过；缺卡提示。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        _patch_agent_paths(tmp)
        out = va.list_shots.func()
        # a 两个镜头 + b 一个镜头全在（poor / 高静音照列，仅带 ⚠）
        assert "INPUT/a.mp4#01" in out and "INPUT/a.mp4#02" in out
        assert "INPUT/b.mp4#01" in out
        assert "画质差" in out and "高静音" in out, out
        assert "共 3 个镜头" in out, out
        assert "穿红裙" in out and len(out.splitlines()) == 5, out   # 头1+镜头3+尾1
        assert "还没有内容索引" in out and "INPUT/c.mp4" in out
        assert "narration.mp3" not in out                            # 纯音频不进池


def t02_list_shots_no_material():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "INPUT"), exist_ok=True)
        _patch_agent_paths(tmp)
        assert "没有任何视频素材" in va.list_shots.func()


def t03_list_music_library_and_uploads():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "MUSIC"), exist_ok=True)
        os.makedirs(os.path.join(tmp, "INPUT"), exist_ok=True)
        import json
        with open(os.path.join(tmp, "MUSIC/meta.json"), "w", encoding="utf-8") as f:
            json.dump([{"file": "MUSIC/x.mp3", "title": "Sunset", "genre": "Ambient",
                        "duration": 100, "tags": ["轻松"]}], f)
        with open(os.path.join(tmp, "INPUT/up.mp3"), "wb") as f:
            f.write(b"x" * 32)
        _patch_agent_paths(tmp)
        orig_probe = va.ffmpeg_exec.probe
        va.ffmpeg_exec.probe = lambda rel: {"duration": 42.5}
        try:
            out = va.list_music.func()
        finally:
            va.ffmpeg_exec.probe = orig_probe
        assert "MUSIC/x.mp3" in out and "Sunset" in out and "轻松" in out
        assert "INPUT/up.mp3" in out and "42.5s" in out
        assert "用户上传" in out


# ------------------------------------------------------------ schema ----- #

def _agent_plan(tmp, **over):
    plan = {
        "schema_version": "2.0",
        "output": {"filename": "OUTPUT/v8.mp4",
                   "resolution": {"width": 640, "height": 360}},
        "clips": [
            {"id": "c1", "source": "INPUT/a.mp4", "kind": "video",
             "trim_start": 0, "trim_end": 6,
             "probe": {"duration": 12, "width": 1920, "height": 1080, "fps": 30,
                       "has_audio": True, "has_video": True}},
            {"id": "c2", "source": "INPUT/b.mp4", "kind": "video",
             "trim_start": 0, "trim_end": 4,
             "probe": {"duration": 10, "width": 1920, "height": 1080, "fps": 30,
                       "has_audio": True, "has_video": True}},
        ],
        "timeline": [{"clip": "c1"},
                     {"clip": "c2", "transition": {"type": "fade", "duration": 0.3}}],
        "audio": {"source": "MUSIC/x.mp3", "volume": 1.0, "loop": True},
        "_title": "海边的一天",
        "_narration": [
            {"group_id": "g1", "clip_ids": ["c1"],
             "units": ["把车开到了没有信号的地方。", "风比想象的大。"]},
            {"group_id": "g2", "clip_ids": ["c2"], "units": ["海是蓝色的。"]},
        ],
    }
    plan.update(over)
    return plan


def t04_schema_accepts_agent_narration():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        assert plan_schema.validate_plan(_agent_plan(tmp), tmp) == []


def t05_schema_rejects_bad_shapes():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        # clip_ids 引用不存在的 clip
        errs = plan_schema.validate_plan(_agent_plan(
            tmp, _narration=[{"group_id": "g1", "clip_ids": ["nope"],
                              "units": ["a。"]}]), tmp)
        assert any("不存在的 clip" in e for e in errs), errs
        # units 非字符串数组
        errs = plan_schema.validate_plan(_agent_plan(
            tmp, _narration=[{"group_id": "g1", "clip_ids": ["c1"],
                              "units": [1, 2]}]), tmp)
        assert any("_narration[0].units" in e for e in errs), errs
        # 组里混入未知字段
        errs = plan_schema.validate_plan(_agent_plan(
            tmp, _narration=[{"group_id": "g1", "clip_ids": ["c1"],
                              "units": ["a。"], "scene": "x"}]), tmp)
        assert any("不支持的字段" in e for e in errs), errs
        # 非法样式名
        errs = plan_schema.validate_plan(
            _agent_plan(tmp, _caption_style="neon"), tmp)
        assert any("_caption_style" in e for e in errs), errs
        # 空 title
        errs = plan_schema.validate_plan(_agent_plan(tmp, _title="  "), tmp)
        assert any("_title" in e for e in errs), errs


def t06_schema_mutual_exclusions():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        # _narration 与 _captions 互斥
        errs = plan_schema.validate_plan(
            _agent_plan(tmp, _captions=["一", "二"]), tmp)
        assert any("互斥" in e for e in errs), errs
        # workflow 宏与手写文案键互斥
        plan = _agent_plan(tmp, workflow={"name": "smart_create"})
        errs = plan_schema.validate_plan(plan, tmp)
        assert any("workflow 宏与手写文案键" in e for e in errs), errs


def t07_schema_agent_captions_shape():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        plan = _agent_plan(tmp)
        plan.pop("_narration")
        plan["_captions"] = ["一句", "二句"]
        assert plan_schema.validate_plan(plan, tmp) == []
        # 超上限
        plan["_captions"] = [f"句{i}" for i in range(13)]
        errs = plan_schema.validate_plan(plan, tmp)
        assert any("超过上限" in e for e in errs), errs


# -------------------------------------------------------- 编译端到端 ----- #

def t08_compile_agent_narration_plan():
    """手写 _narration 计划全链编译：铺轴成 overlays、暂存键移除、报告记 agent。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        result = plan_compiler.compile_plan(_agent_plan(tmp), tmp)
        cap = result.expansions["captions"]
        assert cap["source"] == "agent" and cap["mode"] == "narration"
        assert cap["title"] == "海边的一天"
        assert cap["captions"] == 3
        texts = [o["text"] for o in result.plan["overlays"]]
        assert texts == ["把车开到了没有信号的地方。", "风比想象的大。", "海是蓝色的。"]
        # 铺轴后暂存键全部移除（exec_plan / replan 不再见内部键）
        for k in ("_narration", "_captions", "_caption_style", "_title"):
            assert k not in result.plan
        # 渲染命令存在（全 mock 编译）
        assert any(c.stage == "render" for c in result.commands)


def t09_compile_agent_captions_binding():
    """手写 _captions（一镜一句）与片段数一致 → 逐段绑定。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        plan = _agent_plan(tmp)
        plan.pop("_narration")
        plan["_captions"] = ["日落开场", "海边收尾"]
        result = plan_compiler.compile_plan(plan, tmp)
        cap = result.expansions["captions"]
        assert cap["mode"] == "captions" and cap["captions"] == 2
        hosts = {o["at_clip"]: o["text"] for o in result.plan["overlays"]}
        assert hosts == {"c1": "日落开场", "c2": "海边收尾"}


def t10_narration_text_field():
    """e2e 实测发现：模型自然写 text（整段原文）+ units —— 应被接受；
    只写 text 不写 units 也合法（编译器断句）；text 非字符串仍拒。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        # text + units 共存（e2e 第一稿的实际形态，此前被误拒）
        plan = _agent_plan(tmp, _narration=[
            {"group_id": "g1", "clip_ids": ["c1"],
             "text": "把车开到了没有信号的地方。风比想象的大。",
             "units": ["把车开到了没有信号的地方。", "风比想象的大。"]},
            {"group_id": "g2", "clip_ids": ["c2"], "text": "海是蓝色的。"},
        ])
        assert plan_schema.validate_plan(plan, tmp) == []
        # 只写 text：编译器用标点断句铺轴（断句剥句尾标点，V7.11 既有行为）
        result = plan_compiler.compile_plan(plan, tmp)
        texts = [o["text"] for o in result.plan["overlays"]]
        assert texts == ["把车开到了没有信号的地方。", "风比想象的大。",
                         "海是蓝色的"], texts
        # text 非字符串 → 拒
        errs = plan_schema.validate_plan(_agent_plan(tmp, _narration=[
            {"group_id": "g1", "clip_ids": ["c1"], "text": 123}]), tmp)
        assert any("_narration[0].text" in e for e in errs), errs
        # 既无 units 也无 text → 拒
        errs = plan_schema.validate_plan(_agent_plan(tmp, _narration=[
            {"group_id": "g1", "clip_ids": ["c1"]}]), tmp)
        assert any("units" in e for e in errs), errs


def t11_macro_junk_material_now_in_pool():
    """V8.0 粗筛取消：全 poor 素材不再被拦下（由镜头级条件拒绝并报原因）。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        ctx = {"cards": {"INPUT/b.mp4": _card("INPUT/b.mp4", [
            _shot(0, 4, "草地", quality="good"),
            _shot(4, 8, "草地", quality="poor")], dur=8)},
               "probes": {"INPUT/b.mp4": {"duration": 8, "has_audio": True,
                                          "has_video": True}},
               "project_root": tmp}
        plan, report = wf_mod.WORKFLOWS["one_click_reel"].expand(
            {"budget_seconds": 10}, ctx)
        assert report["culled"] == []
        assert len(plan["clips"]) == 1           # good 镜头入片，poor 被候选层拒绝
        assert report["rejected_total"] == 1


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
    print(f"\nV8.0 agent workflow 离线测试：{len(TESTS) - failed}/{len(TESTS)} 项通过")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
