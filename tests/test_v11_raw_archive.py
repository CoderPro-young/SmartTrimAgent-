"""R11 双存修复回归：展开版不可重编译（2026-10-28 sim-1028-v10a 轮 2 换 BGM 熔断）。

覆盖四件事：
1. compile_plan 留存 raw_plan（校验过、未展开的模型提交语义）
2. raw + 换 BGM 重编译通过，且铺出的旁白字幕与首轮一致（轮 2 事故直接复现）
3. 校验豁免只对编译器自产字幕（_src 标），模型手写越界照拦
4. status_answer 时长口径计入图片片段（71.5s vs 74.95s 实测偏差）

运行：.venv/Scripts/python.exe tests/test_v11_raw_archive.py
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "video_editing"))

import plan_compiler   # noqa: E402
import plan_schema     # noqa: E402
import intent          # noqa: E402
import content_analysis as ca  # noqa: E402


def _mk_project(tmp: str) -> None:
    os.makedirs(os.path.join(tmp, "INPUT"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "MUSIC"), exist_ok=True)
    for rel, data in (("INPUT/a.mp4", b"x" * 64), ("MUSIC/a.mp3", b"x" * 8),
                      ("MUSIC/b.mp3", b"x" * 8)):
        with open(os.path.join(tmp, rel), "wb") as f:
            f.write(data)


def _narration_plan() -> dict:
    """c1 短(2s) + c2 长(8s)，首句旁白 8s 从 c1 起铺 → 必然跨片（轮 2 事故形态）。"""
    return {
        "schema_version": "2.0",
        "output": {"filename": "OUTPUT/o.mp4",
                   "resolution": {"width": 640, "height": 360}},
        "clips": [
            {"id": "c1", "source": "INPUT/a.mp4", "kind": "video",
             "trim_start": 0, "trim_end": 2},
            {"id": "c2", "source": "INPUT/a.mp4", "kind": "video",
             "trim_start": 2, "trim_end": 10},
        ],
        "timeline": [{"clip": "c1", "transition": None},
                     {"clip": "c2", "transition": None}],
        "audio": {"source": "MUSIC/a.mp3", "volume": 0.8, "loop": True},
        "_narration": [{"clip_ids": ["c1", "c2"],
                        "units": ["这一句旁白十二个字必然跨过片段", "第二句短"]}],
    }


def t01_compile_keeps_raw():
    """compile_plan 同时返回展开版与 raw 版，两者语义分离。"""
    with tempfile.TemporaryDirectory() as tmp:
        _mk_project(tmp)
        plan = _narration_plan()
        res = plan_compiler.compile_plan(plan, tmp)
        raw = res.raw_plan
        assert raw is not None, "CompileResult 必须带 raw_plan"
        assert "_narration" in raw, "raw 保留宏（未展开）"
        assert not raw.get("overlays"), "raw 不含铺轴字幕"
        assert "_narration" not in res.plan and res.plan.get("overlays"), \
            "展开版完成旁白→字幕"
        assert raw["audio"]["source"] == "MUSIC/a.mp3"
        # raw 是深拷贝：后续修改 compile 内部 plan 不影响 raw
        assert raw["clips"][0] is not res.plan["clips"][0]


def t02_revise_raw_recompile():
    """轮 2 事故直接复现：raw + 换 BGM 重编译 → 通过，字幕与首轮逐字一致。"""
    with tempfile.TemporaryDirectory() as tmp:
        _mk_project(tmp)
        r1 = plan_compiler.compile_plan(_narration_plan(), tmp)
        texts_1 = [o["text"] for o in r1.plan["overlays"]]

        # 模拟 apply_ops(set_audio)：宿主只改 audio，其余原样
        revised = copy.deepcopy(r1.raw_plan)
        revised["audio"] = {"source": "MUSIC/b.mp3", "volume": 0.8, "loop": True}
        r2 = plan_compiler.compile_plan(revised, tmp)   # 修复前此处 CompileError
        texts_2 = [o["text"] for o in r2.plan["overlays"]]
        assert texts_2 == texts_1, "旁白铺轴重算后应与首轮一致"
        # 首条字幕确实跨片（duration 超 c1 长度 2s）——豁免生效的证据
        ov0 = r2.plan["overlays"][0]
        assert ov0["duration"] > 2.0 and ov0["at_clip"] == "c1", ov0
        assert r2.raw_plan["audio"]["source"] == "MUSIC/b.mp3"


def t03_schema_exempt_only_tagged():
    """越界豁免只认编译器自产标：_src:narration 放行，无标照报。"""
    def _plan(ov):
        return {
            "schema_version": "2.0",
            "output": {"filename": "OUTPUT/o.mp4",
                       "resolution": {"width": 640, "height": 360}},
            "clips": [{"id": "c1", "source": "INPUT/a.mp4", "kind": "video",
                       "trim_start": 0, "trim_end": 3}],
            "timeline": [{"clip": "c1", "transition": None}],
            "overlays": [ov],
        }

    bad = {"type": "text", "at_clip": "c1", "start_offset": 0.0,
           "duration": 5.0, "position": "bottom", "text": "越界字幕"}
    errs = plan_schema.validate_plan(_plan(bad), ".")
    assert any("越界" in e for e in errs), "模型手写越界必须照拦"

    tagged = dict(bad, _src="narration")
    errs = plan_schema.validate_plan(_plan(tagged), ".")
    assert not any("越界" in e for e in errs), "编译器自产跨片字幕应豁免"

    # _src 是白名单键：手写计划多写它不再触发未知字段错（但越界照拦）
    assert not any("不支持的字段" in e for e in
                   plan_schema.validate_plan(_plan(tagged), "."))


def t04_status_answer_counts_image():
    """时长口径：image 的 duration 计入（修复 71.5s vs 74.95s 偏差）。"""
    session = {"turn": 2, "last_output": "OUTPUT/x.mp4",
               "history": [],
               "last_plan": {"clips": [
                   {"id": "c1", "kind": "video", "source": "INPUT/a.mp4",
                    "trim_start": 0, "trim_end": 5},
                   {"id": "c2", "kind": "image", "source": "INPUT/img.jpg",
                    "duration": 3},
               ], "audio": {"source": "MUSIC/b.mp3"}}}
    ans = intent.build_status_answer(session)
    assert "约 8s" in ans, ans   # 5 + 3：修复前图片算 0 → 报 5s
    assert "b.mp3" in ans, ans


if __name__ == "__main__":
    io = json.JSONEncoder(ensure_ascii=False)
    for name, fn in sorted(globals().items()):
        if name.startswith("t0") and callable(fn):
            fn()
            print(f"{name} PASS")
    print("ALL PASS")
