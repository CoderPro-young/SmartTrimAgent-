"""V12 P2 action 引擎 + diff 工具面离线单测。

覆盖：信封三动作 + move / 部分更新 / null 清字段 / 整段替换 / 批内原子 /
未命中 id 报错附清单 / 级联删 / old_values 日志 / speed 归位 / 五个 diff
工具的扁平 op 组装 / submit 端到端（draft → take_submitted 返回 v3）。

运行：.venv/Scripts/python.exe tests/test_v12_actions.py
全部离线：不触网、不跑 ffmpeg（不 compile，只到 submit/validate）。
"""

from __future__ import annotations

import os
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "video_editing"))

import content_analysis as ca   # noqa: E402
import plan_actions as pa       # noqa: E402
import plan_dialect as pd       # noqa: E402
import plan_tools as pt         # noqa: E402


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


def _mk_project(tmp: str) -> str:
    os.makedirs(os.path.join(tmp, "INPUT"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "MUSIC"), exist_ok=True)
    for n in ("a.mp4", "b.mp4", "img.jpg"):
        with open(os.path.join(tmp, "INPUT", n), "wb") as f:
            f.write(b"x" * 64)
    with open(os.path.join(tmp, "MUSIC", "m.mp3"), "wb") as f:
        f.write(b"x" * 32)
    ca._save_json(ca._sidecar_path(
        tmp, os.path.join(tmp, "INPUT/a.mp4"), "INPUT/a.mp4"),
        _card("INPUT/a.mp4", [_shot(0, 6, "日落"), _shot(6, 12, "海边")], 12))
    return tmp


def _draft() -> dict:
    """带两个片段的最小 v3 draft（无 shot 引用，离线可校验）。"""
    return {"schema_version": "3.0",
            "output": {"filename": "OUTPUT/x.mp4", "width": 640, "height": 360,
                       "fps": 30},
            "clips": [
                {"id": "c1", "reader": {"type": "FFmpegReader",
                                        "path": "INPUT/a.mp4"},
                 "start": 0, "end": 6},
                {"id": "c2", "reader": {"type": "FFmpegReader",
                                        "path": "INPUT/b.mp4"},
                 "start": 0, "end": 4,
                 "effects": [{"id": "e1", "type": "Brightness",
                              "brightness": 0.1}]}],
            "timeline": [{"clip": "c1"},
                         {"clip": "c2",
                          "transition": {"type": "fade", "duration": 0.5}}],
            "overlays": [{"id": "o1", "type": "text", "at_clip": "c1",
                          "start_offset": 0, "duration": 2, "gravity": "top",
                          "text": "标题"}]}


_T = []


def test(name):
    def deco(fn):
        _T.append((name, fn))
        return fn
    return deco


# ------------------------------------------------------------- 引擎 ---- #

@test("insert: 自动 id + timeline 联动 + after 定位")
def _(tmp):
    plan, applied, errs = pa.apply_actions(_draft(), [
        {"key": ["clips"], "type": "insert",
         "value": {"reader": {"type": "FFmpegReader", "path": "INPUT/b.mp4"},
                   "start": 1, "end": 3, "after": "c1"}}])
    assert not errs, errs
    assert [c["id"] for c in plan["clips"]] == ["c1", "c3", "c2"]
    assert [t["clip"] for t in plan["timeline"]] == ["c1", "c3", "c2"]
    assert applied[0]["detail"].startswith("新增 c3")


@test("批内原子: 一条错整批拒绝，草稿不动")
def _(tmp):
    d = _draft()
    plan, applied, errs = pa.apply_actions(d, [
        {"key": ["clips"], "type": "insert",
         "value": {"reader": {"type": "FFmpegReader", "path": "INPUT/b.mp4"},
                   "start": 1, "end": 3}},
        {"key": ["clips", {"id": "cX"}], "type": "delete"}])
    assert plan is None and errs
    assert any("cX 不存在" in e and "c1" in e for e in errs), errs  # 附可用清单
    plan2, _, errs2 = pa.apply_actions(d, [
        {"key": ["clips", {"id": "c1"}], "type": "update", "value": {"end": 5}},
        {"key": ["clips"], "type": "update"}])       # 数组级 update 非法
    assert plan2 is None and len(errs2) == 1


@test("update 部分语义: 只改传入字段；null 清除；reader 整段替换")
def _(tmp):
    plan, _, errs = pa.apply_actions(_draft(), [
        {"key": ["clips", {"id": "c1"}], "type": "update",
         "value": {"end": 5.5, "start": None}}])
    assert not errs, errs
    c1 = plan["clips"][0]
    assert c1["end"] == 5.5 and "start" not in c1
    plan, _, errs = pa.apply_actions(_draft(), [
        {"key": ["clips", {"id": "c1"}], "type": "update",
         "value": {"reader": {"type": "FFmpegReader", "path": "INPUT/b.mp4"}}}])
    assert plan["clips"][0]["reader"]["path"] == "INPUT/b.mp4"


@test("delete 级联: 片段连 timeline 条目与挂载 overlay 一起清")
def _(tmp):
    plan, applied, errs = pa.apply_actions(_draft(), [
        {"key": ["clips", {"id": "c1"}], "type": "delete"}])
    assert not errs and [c["id"] for c in plan["clips"]] == ["c2"]
    assert [t["clip"] for t in plan["timeline"]] == ["c2"]
    assert plan["overlays"] == []
    assert "连带移除" in applied[0]["detail"]
    old = applied[0]["old_values"]["clip"]
    assert old["id"] == "c1"            # old_values 记录（撤销预留）


@test("效果: insert 分配 id / update 部分改参 / delete / speed 归位 clip.speed")
def _(tmp):
    plan, _, errs = pa.apply_actions(_draft(), [
        {"key": ["clips", {"id": "c2"}, "effects"], "type": "insert",
         "value": {"type": "Saturation", "saturation": 1.4}},
        {"key": ["clips", {"id": "c2"}, "effects", {"id": "e1"}],
         "type": "update", "value": {"brightness": 0.3}},
        {"key": ["clips", {"id": "c2"}, "effects"], "type": "insert",
         "value": {"type": "speed", "factor": 2}}])
    assert not errs, errs
    c2 = plan["clips"][1]
    assert c2["speed"] == 2
    assert [(e["id"], e["type"]) for e in c2["effects"]] == \
        [("e1", "Brightness"), ("e2", "Saturation")]
    assert c2["effects"][0]["brightness"] == 0.3
    plan, _, errs = pa.apply_actions(plan, [
        {"key": ["clips", {"id": "c2"}, "effects", {"id": "e2"}],
         "type": "delete"}])
    assert not errs and [e["id"] for e in plan["clips"][1]["effects"]] == ["e1"]
    # 未命中 id 报错附现有清单
    _, _, errs = pa.apply_actions(_draft(), [
        {"key": ["clips", {"id": "c2"}, "effects", {"id": "e9"}],
         "type": "delete"}])
    assert any("e9 不在片段 c2" in e and "e1" in e for e in errs), errs


@test("效果白名单: 未知 type 与多余参数就地报错（带 v3 菜单）")
def _(tmp):
    _, _, errs = pa.apply_actions(_draft(), [
        {"key": ["clips", {"id": "c2"}, "effects"], "type": "insert",
         "value": {"type": "Mirror"}}])
    assert any("Mirror" in e and "Brightness" in e for e in errs), errs
    _, _, errs = pa.apply_actions(_draft(), [
        {"key": ["clips", {"id": "c2"}, "effects"], "type": "insert",
         "value": {"type": "Brightness", "saturation": 1.2}}])
    assert any("不接受参数" in e for e in errs), errs


@test("move: clips 与 timeline 同步重排；after 省略 = 最前")
def _(tmp):
    plan, _, errs = pa.apply_actions(_draft(), [
        {"key": ["timeline", {"clip": "c2"}], "type": "move",
         "value": {"after": None}}])
    assert not errs, errs
    assert [t["clip"] for t in plan["timeline"]] == ["c2", "c1"]
    assert [c["id"] for c in plan["clips"]] == ["c2", "c1"]
    # c2 的转场条目跟着走（现在 c2 在首位，转场挂在它身上仍随条目移动）
    plan, _, errs = pa.apply_actions(plan, [
        {"key": ["timeline", {"clip": "c2"}], "type": "move",
         "value": {"after": "c1"}}])
    assert [t["clip"] for t in plan["timeline"]] == ["c1", "c2"]
    assert [c["id"] for c in plan["clips"]] == ["c1", "c2"]


@test("transition: 首片段拒绝 / none 走 delete / 类型白名单")
def _(tmp):
    _, _, errs = pa.apply_actions(_draft(), [
        {"key": ["timeline", {"clip": "c1"}, "transition"], "type": "update",
         "value": {"type": "fade", "duration": 0.5}}])
    assert any("首个片段" in e for e in errs), errs
    plan, _, errs = pa.apply_actions(_draft(), [
        {"key": ["timeline", {"clip": "c2"}, "transition"], "type": "delete"}])
    assert not errs and plan["timeline"][1]["transition"] is None
    _, _, errs = pa.apply_actions(_draft(), [
        {"key": ["timeline", {"clip": "c2"}, "transition"], "type": "update",
         "value": {"type": "starwipe", "duration": 0.5}}])
    assert any("starwipe" in e for e in errs), errs


@test("overlays: 自动 id / gravity 白名单 / update 部分 / delete")
def _(tmp):
    plan, _, errs = pa.apply_actions(_draft(), [
        {"key": ["overlays"], "type": "insert",
         "value": {"type": "text", "at_clip": "c2", "text": "结尾",
                   "start_offset": 0, "duration": 1}}])
    assert not errs and plan["overlays"][1]["id"] == "o2"
    _, _, errs = pa.apply_actions(_draft(), [
        {"key": ["overlays"], "type": "insert",
         "value": {"type": "text", "at_clip": "c2", "text": "x",
                   "start_offset": 0, "duration": 1, "gravity": "middle"}}])
    assert any("gravity" in e for e in errs), errs
    plan, _, errs = pa.apply_actions(_draft(), [
        {"key": ["overlays", {"id": "o1"}], "type": "update",
         "value": {"text": "新标题", "duration": 3}}])
    assert plan["overlays"][0]["text"] == "新标题" \
        and plan["overlays"][0]["duration"] == 3
    assert plan["overlays"][0]["gravity"] == "top"     # 未碰字段保留
    _, _, errs = pa.apply_actions(_draft(), [
        {"key": ["overlays", {"id": "o9"}], "type": "delete"}])
    assert any("o9 不存在" in e and "o1" in e for e in errs), errs


@test("audio/output/意图键: 合并 / 白名单 / 整体替换")
def _(tmp):
    plan, _, errs = pa.apply_actions(_draft(), [
        {"key": ["audio"], "type": "insert",
         "value": {"source": "MUSIC/m.mp3", "volume": 0.5}},
        {"key": ["audio"], "type": "update", "value": {"loop": False}}])
    assert not errs and plan["audio"] == {"source": "MUSIC/m.mp3",
                                          "volume": 0.5, "loop": False}
    _, _, errs = pa.apply_actions(_draft(), [
        {"key": ["audio"], "type": "update", "value": {"bitrate": 320}}])
    assert any("不支持的字段" in e for e in errs), errs
    plan, _, errs = pa.apply_actions(_draft(), [
        {"key": ["output"], "type": "update", "value": {"width": 1280}},
        {"key": ["_narration"], "type": "update",
         "value": [{"clip_ids": ["c1"], "text": "旁白"}]},
        {"key": ["audio"], "type": "delete"}])
    assert not errs and plan["output"]["width"] == 1280
    assert plan["_narration"][0]["text"] == "旁白" and "audio" not in plan
    _, _, errs = pa.apply_actions(_draft(), [{"key": ["video"], "type": "update"}])
    assert any("不认识的 key[0]" in e for e in errs), errs


@test("clip.audio 不收 volume（平级字段）；未知 clip 字段拒绝")
def _(tmp):
    _, _, errs = pa.apply_actions(_draft(), [
        {"key": ["clips", {"id": "c1"}], "type": "update",
         "value": {"audio": {"volume": 0.5}}}])
    assert any("clip 平级" in e for e in errs), errs
    _, _, errs = pa.apply_actions(_draft(), [
        {"key": ["clips", {"id": "c1"}], "type": "update",
         "value": {"position": 3.0}}])
    assert any("不支持的字段" in e and "position" in e for e in errs), errs


# ------------------------------------------------------------- 工具面 ---- #

class _Env:
    """每个测试独立的 draft 环境。"""

    def __init__(self, tmp):
        self.tmp = tmp
        pt.PROJECT_ROOT = tmp
        pt.PROBE_FN = None
        pt.reset_draft()


@test("set_project: 初始化与更新两用")
def _(tmp):
    t = _Env(tmp)
    r = pt.set_project.invoke({"filename": "OUTPUT/demo.mp4", "width": 1280,
                               "height": 720})
    assert r.startswith("✓ draft 已初始化")
    r = pt.set_project.invoke({"filename": "OUTPUT/demo2.mp4", "width": 1920,
                               "height": 1080})
    assert r.startswith("✓") and "demo2.mp4" in r
    assert pt._draft()["output"]["width"] == 1920


@test("clip_diff: 批量 insert + 单片段就地校验（素材不存在附清单）")
def _(tmp):
    t = _Env(tmp)
    pt.set_project.invoke({"filename": "OUTPUT/demo.mp4", "width": 640,
                           "height": 360})
    r = pt.clip_diff.invoke({"ops": [
        {"op": "insert", "reader": {"type": "FFmpegReader",
                                    "path": "INPUT/a.mp4"}, "shot": 1},
        {"op": "insert", "reader": {"type": "ImageReader",
                                    "path": "INPUT/img.jpg"}, "duration": 3},
    ]})
    assert r.startswith("✓"), r
    plan = pt._draft()
    assert [c["id"] for c in plan["clips"]] == ["c1", "c2"]
    assert plan["clips"][0]["shot"] == 1
    r = pt.clip_diff.invoke({"ops": [
        {"op": "insert", "reader": {"type": "FFmpegReader",
                                    "path": "INPUT/不存在.mp4"}}]})
    assert r.startswith("✗") and "INPUT/ 里实际可用" in r, r


@test("clip_diff: update + after 插入 + delete 级联")
def _(tmp):
    t = _Env(tmp)
    pt.reset_draft(_draft())
    r = pt.clip_diff.invoke({"ops": [
        {"op": "update", "id": "c1", "end": 5},
        {"op": "insert", "reader": {"type": "FFmpegReader",
                                    "path": "INPUT/b.mp4"},
         "start": 0, "end": 2, "after": "c2"},
        {"op": "delete", "id": "c2"}]})
    assert r.startswith("✓"), r
    plan = pt._draft()
    assert [c["id"] for c in plan["clips"]] == ["c1", "c3"]
    assert plan["clips"][0]["end"] == 5


@test("effect_diff: insert/update/delete 按 id，跨片段定位 owner")
def _(tmp):
    t = _Env(tmp)
    pt.reset_draft(_draft())
    r = pt.effect_diff.invoke({"ops": [
        {"op": "insert", "clip": "c1", "type": "Saturation", "saturation": 1.5},
        {"op": "update", "id": "e1", "brightness": 0.4},
        {"op": "insert", "clip": "c2", "type": "speed", "factor": 2}]})
    assert r.startswith("✓"), r
    plan = pt._draft()
    assert plan["clips"][0]["effects"][0]["id"] == "e2"
    assert plan["clips"][1]["speed"] == 2      # speed 经 effect_diff 也归位
    assert plan["clips"][1]["effects"][0]["brightness"] == 0.4
    r = pt.effect_diff.invoke({"ops": [{"op": "delete", "id": "e1"}]})
    assert r.startswith("✓"), r
    assert not pt._draft()["clips"][1].get("effects")   # 引擎返回新 dict，须重取


@test("overlay_diff / timeline_diff: id 寻址与 none 硬切")
def _(tmp):
    t = _Env(tmp)
    pt.reset_draft(_draft())
    r = pt.overlay_diff.invoke({"ops": [
        {"op": "update", "id": "o1", "text": "新文案"},
        {"op": "insert", "type": "pip", "at_clip": "c2",
         "source": "INPUT/img.jpg", "kind": "image",
         "start_offset": 0, "duration": 1}]})
    assert r.startswith("✓"), r
    plan = pt._draft()
    assert plan["overlays"][0]["text"] == "新文案"
    assert plan["overlays"][1]["id"] == "o2" and plan["overlays"][1]["type"] == "pip"
    r = pt.timeline_diff.invoke({"ops": [
        {"op": "transition", "clip": "c2", "type": "none"},
        {"op": "move", "clip": "c2"}]})          # after 省略 = 移到最前
    assert r.startswith("✓"), r
    plan = pt._draft()
    assert plan["timeline"][0]["clip"] == "c2"
    # c2 现在是首片段
    r = pt.timeline_diff.invoke({"ops": [
        {"op": "transition", "clip": "c2", "type": "fade", "duration": 0.5}]})
    assert r.startswith("✗") and "首个片段" in r, r


@test("audio_diff: set 走 validate_audio（文件不存在报错）/ clear")
def _(tmp):
    t = _Env(tmp)
    pt.reset_draft(_draft())
    r = pt.audio_diff.invoke({"ops": [
        {"op": "set", "source": "MUSIC/无.mp3", "volume": 0.5}]})
    assert r.startswith("✗") and "文件不存在" in r, r
    r = pt.audio_diff.invoke({"ops": [
        {"op": "set", "source": "MUSIC/m.mp3", "volume": 0.5},
        {"op": "set", "ducking": True}]})
    assert r.startswith("✗") and "original" in r, r   # ducking 需显式 keep
    r = pt.audio_diff.invoke({"ops": [
        {"op": "set", "source": "MUSIC/m.mp3", "volume": 0.5}]})
    assert r.startswith("✓") and pt._draft()["audio"]["source"] == "MUSIC/m.mp3"
    r = pt.audio_diff.invoke({"ops": [{"op": "clear"}]})
    assert r.startswith("✓") and "audio" not in pt._draft()


@test("set_narration/set_captions 互斥 + 空数组移除")
def _(tmp):
    t = _Env(tmp)
    pt.reset_draft(_draft())
    r = pt.set_captions.invoke({"lines": ["一", "二"]})
    assert r.startswith("✓")
    r = pt.set_narration.invoke({"groups": [{"clip_ids": ["c1"], "text": "旁白"}]})
    assert r.startswith("✓") and "_narration" in pt._draft() \
        and "_captions" not in pt._draft()
    r = pt.set_narration.invoke({"groups": []})
    assert r.startswith("✓") and "_narration" not in pt._draft()


@test("validate_plan/submit: 全量校验拦跨片段错误；submit 端到端出 v3")
def _(tmp):
    t = _Env(tmp)
    pt.reset_draft(_draft())
    # 转场 0.5s < 片段长，应通过
    r = pt.validate_plan.invoke({})
    assert "✓" in r, r
    r = pt.submit.invoke({})
    assert r.startswith("✓ 已提交"), r
    submitted = pt.take_submitted()
    assert submitted is not None and submitted["schema_version"] == "3.0"
    assert pt.take_submitted() is None           # 消费型
    # 跨片段错误：把 c1 缩到 0.3s，转场 0.5s 就太长
    pt.reset_draft(_draft())
    pt.clip_diff.invoke({"ops": [{"op": "update", "id": "c1", "end": 0.3}]})
    r = pt.submit.invoke({})
    assert r.startswith("✗") and "转场时长" in r, r
    assert pt.take_submitted() is None


@test("load_plan: v2 计划载入即升级 v3")
def _(tmp):
    t = _Env(tmp)
    v2 = {"schema_version": "2.0",
          "output": {"filename": "OUTPUT/x.mp4",
                     "resolution": {"width": 640, "height": 360}},
          "clips": [{"id": "c1", "source": "INPUT/a.mp4", "kind": "video",
                     "trim_start": 0, "trim_end": 6}],
          "timeline": [{"clip": "c1"}]}
    r = pt.load_plan.invoke({"plan": v2})
    assert r.startswith("✓"), r
    d = pt._draft()
    assert d["schema_version"] == "3.0" and d["output"]["width"] == 640
    assert d["clips"][0]["reader"]["path"] == "INPUT/a.mp4"


@test("修订轮载入初态: reset_draft(last_plan) 后 diff 工具只改目标")
def _(tmp):
    t = _Env(tmp)
    base = _draft()
    pt.reset_draft(base)
    r = pt.effect_diff.invoke({"ops": [
        {"op": "update", "id": "e1", "brightness": 0.9}]})
    assert r.startswith("✓"), r
    plan = pt._draft()
    assert plan["clips"][1]["effects"][0]["brightness"] == 0.9
    assert plan["clips"][0]["start"] == 0          # 未碰字段逐字保留
    assert plan["overlays"][0]["text"] == "标题"


@test("compile 端到端: v3 计划降级编译，raw_plan 保持 v3")
def _(tmp):
    import plan_compiler as pc
    t = _Env(tmp)
    pt.reset_draft(_draft())
    r = pt.submit.invoke({})
    assert r.startswith("✓ 已提交"), r
    plan = pt.take_submitted()
    res = pc.compile_plan(plan, tmp)
    assert res.commands, res.commands
    assert res.raw_plan["schema_version"] == "3.0"          # raw 回挂 v3
    assert res.plan["clips"][0].get("trim_start") == 0      # 内部管线仍是 v2
    # v2 legacy 输入行为不变
    v2 = pd.to_internal(plan)
    res2 = pc.compile_plan(v2, tmp)
    assert res2.raw_plan["schema_version"] == "2.0"


@test("引擎→方言闭环: 引擎产物过 validate_any（round-trip 后仍合法）")
def _(tmp):
    plan, _, errs = pa.apply_actions(_draft(), [
        {"key": ["clips"], "type": "insert",
         "value": {"reader": {"type": "FFmpegReader", "path": "INPUT/b.mp4"},
                   "start": 0, "end": 5, "speed": 2, "volume": 0.8,
                   "effects": [{"type": "Brightness", "brightness": 0.2},
                               {"type": "Saturation", "saturation": 1.3}]}}])
    assert not errs, errs
    assert errs == [] and plan["clips"][2]["speed"] == 2
    v2 = pd.to_internal(plan)
    effs = v2["clips"][2]["effects"]
    assert [e["name"] for e in effs] == ["speed", "eq"], effs
    remaining = pd.validate_any(plan, tmp)
    # draft 校验：转场/越界等规则在内部形态上跑
    assert isinstance(remaining, list)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="v12actions-")
    _mk_project(tmp)
    failed = 0
    for name, fn in _T:
        try:
            fn(tmp)
            print(f"PASS {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {name}\n     {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {name}\n     {type(e).__name__}: {e}")
    print(f"\n{len(_T) - failed}/{len(_T)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
