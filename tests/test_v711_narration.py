"""V7.11 旁白制（整片按组写旁白 → 断句 → 字符加权铺轴）离线单测。

运行：.venv/Scripts/python.exe tests/test_v711_narration.py
"""

from __future__ import annotations

import os
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "video_editing"))

import plan_compiler   # noqa: E402
import workflow as wf_mod  # noqa: E402


# ---------------------------------------------------------------- 纯函数 ---- #

def _clips_picked(specs):
    """specs = [(dur, scene, desc)] → (clips, picked) 平行数组（select_shots 口径）。"""
    clips, picked = [], []
    for i, (dur, scene, desc) in enumerate(specs):
        clips.append({"id": f"s{i:02d}", "source": f"INPUT/m{i}.mp4", "kind": "video",
                      "trim_start": 0, "trim_end": dur})
        picked.append({"duration": dur, "start": 0, "end": dur,
                       "label": {"scene": scene, "desc": desc, "activity": "空镜",
                                 "mood": "", "tags": [], "quality": "good",
                                 "usable": True}})
    return clips, picked


def t01_budget_formula():
    assert wf_mod._narration_budget(10) == (30, 50)      # 3~5 字/秒
    assert wf_mod._narration_budget(1) == (8, 14)        # 极短组下限
    assert wf_mod._narration_budget(0) == (8, 14)
    assert wf_mod._narration_budget(2) == (8, 14)        # hi 至少 lo+6


def t02_grouping_scene_and_cap():
    clips, picked = _clips_picked([
        (6, "草原", "羊群下山"), (6, "草原", "羊群过河"),       # 同场景 → 同组 12s
        (6, "湖边", "湖面起风"), (6, "湖边", "友人回头"),
        (6, "湖边", "落日"), (6, "湖边", "天黑收工"),           # 24s ≤ 25 仍同组
        (6, "湖边", "补拍空镜"),                               # 24+6=30 超 25 → 同场景开新组
        (30, "夜景", "城市灯光"),                               # 场景变化 → 独立成组
    ])
    groups = wf_mod._group_picked(clips, picked)
    assert [g["group_id"] for g in groups] == ["g1", "g2", "g3", "g4"], groups
    assert groups[0]["clip_ids"] == ["s00", "s01"]         # 草原 12s
    assert groups[1]["clip_ids"] == ["s02", "s03", "s04", "s05"]  # 湖边 24s（达上限）
    assert groups[2]["clip_ids"] == ["s06"] and groups[2]["seconds"] == 6  # 超限拆出
    assert groups[3]["clip_ids"] == ["s07"] and groups[3]["seconds"] == 30
    assert "scene" not in groups[0]                        # 内部字段不外泄


def t03_split_units_punct_merge_wrap():
    split = wf_mod._split_narration_units
    # 标点断句
    assert split("我把车开到了没有信号的地方，羊群接管了整片山坡。") == \
        ["我把车开到了没有信号的地方", "羊群接管了整片山坡"]
    # 碎片并入前条（「它来了」3 字 < 5 → 并入前条）
    assert split("风突然停了，它来了。") == ["风突然停了它来了"]
    # 超长硬换行（>18 字按 18 切）
    out = split("这是一个故意写得特别特别长根本没有标点的句子要被硬换行处理掉")
    assert all(len(u) <= wf_mod.UNIT_MAX_CHARS for u in out) and len(out) >= 2
    # 空输入
    assert split("") == [] and split(None) == []


# ------------------------------------------------------------ 生成与回退 ---- #

class _FakeResp:
    def __init__(self, content):
        self.content = content


def _fake_model(responses):
    """sys.modules["model"] 替身：按队列吐回复，记录 prompt。"""
    box = {"prompts": [], "left": list(responses)}

    class _M:
        def invoke(self, prompt):
            box["prompts"].append(prompt)
            return _FakeResp(box["left"].pop(0) if box["left"] else "{}")
    mod = type(sys)("model")
    mod.get_model = lambda: _M()
    mod.box = box
    return mod


def _with_model(responses, fn):
    import importlib
    fake = _fake_model(responses)
    saved = sys.modules.get("model")
    sys.modules["model"] = fake
    try:
        return fn(fake.box)
    finally:
        if saved is not None:
            sys.modules["model"] = saved
        else:
            sys.modules.pop("model", None)
    del importlib


def t04_write_narration_happy_path():
    clips, picked = _clips_picked([(10, "草原", "羊群下山"), (10, "湖边", "湖面起风")])
    ok_json = ('{"title": "没有信号的两天", "groups": ['
               '{"group_id": "g1", "raw_text": "我把车开进了草原深处，手机彻底没了信号。"},'
               '{"group_id": "g2", "raw_text": "湖边起了大风，浪拍在石头上，我坐到天黑。"}]}')

    def run(box):
        narration, title = wf_mod._write_narration(clips, picked, keyword="逃离城市")
        return narration, title, box["prompts"]
    narration, title, prompts = _with_model([ok_json], run)
    assert title == "没有信号的两天"
    assert [g["group_id"] for g in narration] == ["g1", "g2"]
    assert narration[0]["units"] == ["我把车开进了草原深处", "手机彻底没了信号"]
    p = prompts[0]
    assert "字数预算 30~50 字" in p                       # 组时长 10s → 3~5 字/秒
    assert "第一人称" in p and "禁用词" in p and "逃离城市" in p
    assert "羊群下山" in p and "湖面起风" in p            # desc 进输入


def t05_missing_group_retries_then_fails():
    clips, picked = _clips_picked([(6, "A", "a"), (6, "B", "b")])
    bad = '{"title": "t", "groups": [{"group_id": "g1", "raw_text": "只有一组"}]}'
    good = ('{"title": "t", "groups": ['
            '{"group_id": "g1", "raw_text": "第一组旁白。"},'
            '{"group_id": "g2", "raw_text": "第二组旁白。"}]}')

    def run(box):
        n1, _ = wf_mod._write_narration(clips, picked)     # 第一次缺 g2 → 重试成功
        n2, __ = None, None
        return n1, len(box["prompts"]), box["prompts"][-1]
    n1, calls, last = _with_model([bad, good], run)
    assert n1 is not None and calls == 2
    assert "缺少 g2 组" in last

    def run2(box):
        return wf_mod._write_narration(clips, picked)      # 两次都缺 → None
    n2, _ = _with_model([bad, bad], run2)
    assert n2 is None


# ------------------------------------------------------------ 编译铺轴 ---- #

def _plan_with_narration(tmp, narration, trans=0):
    os.makedirs(os.path.join(tmp, "INPUT"), exist_ok=True)
    for n in ("m0.mp4", "m1.mp4", "m2.mp4"):
        with open(os.path.join(tmp, "INPUT", n), "wb") as f:
            f.write(b"x" * 64)
    clips, picked = _clips_picked([(10, "A", "a"), (10, "B", "b"), (10, "C", "c")])
    timeline = [{"clip": c["id"]} for i, c in enumerate(clips)]
    if trans:
        for i in range(1, len(timeline)):
            timeline[i]["transition"] = {"type": "fade", "duration": trans}
    return {"schema_version": "2.0",
            "output": {"filename": "OUTPUT/o.mp4",
                       "resolution": {"width": 640, "height": 360}},
            "clips": clips, "timeline": timeline,
            "_narration": narration, "_caption_style": "bottom"}


def t06_compiler_weights_units_across_group_window():
    """组窗口内字符加权：长句占更久；一个镜头可挂多句。

    临时键 _narration 只在编译器内部流转（validate 之后注入），这里直接
    调 _derive + _apply_captions 单测铺轴，与 _apply_captions 的调用位次一致。
    """
    narration = [{"group_id": "g1", "clip_ids": ["s00", "s01"],
                  "units": ["很长的第一句字幕占十五个字", "短句"]}]
    with tempfile.TemporaryDirectory() as tmp:
        base = _plan_with_narration(tmp, narration)
        base.pop("_narration"), base.pop("_caption_style")
        math = plan_compiler._derive(base)
        base["_narration"] = narration
        base["_caption_style"] = "bottom"
        plan_compiler._apply_captions(base, math)
        ovs = base["overlays"]
        assert len(ovs) == 2
        assert all(o["position"] == "bottom" and o["y_margin"] > 0 for o in ovs)
        assert "_narration" not in base
        # 组窗口 = s00[0,10] + s01[10,20]（无转场）→ 权重 11:2
        d_long, d_short = ovs[0]["duration"], ovs[1]["duration"]
        assert d_long > d_short * 2, (d_long, d_short)
        # 第一句起点 0 挂 s00，且时长超过 s00 的 10s —— 一句跨过剪辑点
        assert ovs[0]["at_clip"] == "s00" and ovs[0]["start_offset"] >= 0.04
        assert d_long > 10, d_long
        assert ovs[1]["at_clip"] == "s01"
        # 游标铺满：末句绝对终点 ≈ 组窗口终点 20s（±0.3 容差）
        assert abs((10 + ovs[1]["start_offset"] + d_short) - 20) < 0.3


def t07_narration_crosses_clip_boundary_with_transition():
    """转场路径全链路（workflow 块 → 旁白 → 铺轴 → 渲染命令）：
    组窗口含转场重叠；多句挂多镜；enable 是绝对时间窗。"""
    import content_analysis as ca
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "INPUT"), exist_ok=True)
        with open(os.path.join(tmp, "INPUT", "a.mp4"), "wb") as f:
            f.write(b"x" * 64)
        card = {"schema_version": 2, "source": "INPUT/a.mp4", "duration": 24,
                "signals": {"audio": {"silences": None, "silence_ratio": None},
                            "video": {"blacks": []}},
                "shots": [{"start": 0 + 8 * k, "end": 8 * (k + 1), "label": {
                    "person_count": 0, "scene": "草原", "activity": "空镜",
                    "mood": "", "tags": [], "desc": f"草原镜头{k}",
                    "quality": "good", "usable": True}} for k in range(3)]}
        full = os.path.join(tmp, "INPUT", "a.mp4")
        ca._save_json(ca._sidecar_path(tmp, full, "INPUT/a.mp4"), card)
        ok_json = ('{"title": "草原一日", "groups": ['
                   '{"group_id": "g1", "raw_text": "第一句铺垫。第二句推进。第三句收束。"}]}')
        plan = {"schema_version": "2.0",
                "output": {"filename": "OUTPUT/o.mp4",
                           "resolution": {"width": 640, "height": 360}},
                "workflow": {"name": "smart_create", "transition": 0.5}}

        def run(box):
            return plan_compiler.compile_plan(plan, tmp)
        result = _with_model([ok_json], run)
        # 3×8s - 2×0.5 重叠 = 23s；同场景 24s ≤ 25 → 单组三镜
        assert result.math["D"] == 23.0
        ovs = result.plan["overlays"]
        assert len(ovs) == 3
        assert [o["text"] for o in ovs] == ["第一句铺垫", "第二句推进", "第三句收束"]
        # 绝对时间语义：三句时窗单调铺满组窗口 [0, 23]，末句终点 ≈ D
        st = result.math["starts"]
        abs_starts = [round(st[o["at_clip"]] + o["start_offset"], 2) for o in ovs]
        assert abs_starts == sorted(abs_starts), abs_starts
        last_end = abs_starts[-1] + ovs[-1]["duration"]
        assert abs(last_end - 23.0) < 0.3, last_end
        render = next(c for c in result.commands if c.stage == "render")
        arg = " ".join(render.argv)
        assert arg.count("enable='between(t,") >= 3
        assert result.expansions["workflow"]["captions_source"] == "narration"
        assert result.expansions["workflow"]["title"] == "草原一日"


def t08_expand_smart_create_narration_first_and_fallback():
    """全链路：无自带文案 → 旁白优先；旁白失败 → 一镜一句链不受影响。"""
    import content_analysis as ca
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "INPUT"), exist_ok=True)
        with open(os.path.join(tmp, "INPUT", "a.mp4"), "wb") as f:
            f.write(b"x" * 64)
        card = {"schema_version": 2, "source": "INPUT/a.mp4", "duration": 20,
                "signals": {"audio": {"silences": None, "silence_ratio": None},
                            "video": {"blacks": []}},
                "shots": [{"start": 0, "end": 10, "label": {"person_count": 0,
                           "scene": "草原", "activity": "空镜", "mood": "", "tags": [],
                           "desc": "羊群从山坡涌下来", "quality": "good", "usable": True}},
                          {"start": 10, "end": 20, "label": {"person_count": 0,
                           "scene": "湖边", "activity": "空镜", "mood": "", "tags": [],
                           "desc": "湖面起风浪拍石头", "quality": "good", "usable": True}}]}
        full = os.path.join(tmp, "INPUT", "a.mp4")
        ca._save_json(ca._sidecar_path(tmp, full, "INPUT/a.mp4"), card)
        ctx = {"cards": {"INPUT/a.mp4": card},
               "probes": {"INPUT/a.mp4": {"duration": 20, "has_audio": True,
                                          "has_video": True}},
               "project_root": tmp}
        ok_json = ('{"title": "去有风的地方", "groups": ['
                   '{"group_id": "g1", "raw_text": "羊群涌下山坡，我在车里看了很久。"},'
                   '{"group_id": "g2", "raw_text": "湖边起风了，浪一下一下拍石头。"}]}')

        # ① 旁白成功：_narration 进 plan，report 带 title / captions_source=narration
        def run(box):
            plan, report = wf_mod.WORKFLOWS["smart_create"].expand(
                {"budget_seconds": 20, "transition": 0}, ctx)
            return plan, report
        plan, report = _with_model([ok_json], run)
        assert "_narration" in plan and "_captions" not in plan
        assert report["captions_source"] == "narration"
        assert report["title"] == "去有风的地方"
        assert report["captions"] == 4                    # 每组断成 2 句

        # ② 旁白失败（两次坏 JSON）→ 回退一镜一句（llm 写句成功）
        per_shot = '["羊群下山了", "湖边起风了"]'

        def run2(box):
            plan2, report2 = wf_mod.WORKFLOWS["smart_create"].expand(
                {"budget_seconds": 20, "transition": 0}, ctx)
            return plan2, report2
        plan2, report2 = _with_model(["not json", "not json", per_shot], run2)
        assert report2["captions_source"] == "llm"
        assert plan2["_captions"] == ["羊群下山了", "湖边起风了"]

        # ③ 自带文案 → 旁白与 llm 都不参与（plug 链优先级不变）
        plan3, report3 = wf_mod.WORKFLOWS["smart_create"].expand(
            {"budget_seconds": 20, "transition": 0,
             "captions": ["自定义一", "自定义二"]}, ctx)
        assert report3["captions_source"] == "plan"
        assert plan3["_captions"] == ["自定义一", "自定义二"]


def t09_credits_mode_skips_narration():
    """credits 风格不走旁白（滚动整块语义不同），保持原路径。"""
    import content_analysis as ca
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "INPUT"), exist_ok=True)
        with open(os.path.join(tmp, "INPUT", "a.mp4"), "wb") as f:
            f.write(b"x" * 64)
        card = {"schema_version": 2, "source": "INPUT/a.mp4", "duration": 20,
                "signals": {"audio": {"silences": None, "silence_ratio": None},
                            "video": {"blacks": []}},
                "shots": [{"start": 0, "end": 20, "label": {"person_count": 0,
                           "scene": "草原", "activity": "空镜", "mood": "", "tags": [],
                           "quality": "good", "usable": True}}]}
        full = os.path.join(tmp, "INPUT", "a.mp4")
        ca._save_json(ca._sidecar_path(tmp, full, "INPUT/a.mp4"), card)
        ctx = {"cards": {"INPUT/a.mp4": card},
               "probes": {"INPUT/a.mp4": {"duration": 20, "has_audio": True,
                                          "has_video": True}},
               "project_root": tmp}
        # credits + 无自带文案：旁白不触发，llm 写句（模拟失败退 labels 也行）
        def run(box):
            plan, report = wf_mod.WORKFLOWS["smart_create"].expand(
                {"budget_seconds": 20, "transition": 0,
                 "subtitle_style": "credits"}, ctx)
            return plan, report, len(box["prompts"])
        plan, report, llm_calls = _with_model(
            ['["滚动文案一"]'], run)
        assert report["captions_source"] == "llm"
        assert "_narration" not in plan


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
    print(f"\nV7.11 narration 离线测试：{total - failed}/{total} 项通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
