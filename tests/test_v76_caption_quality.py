"""V7.6 文案质量升级（desc 画面描述输入 + prompt 重写 + 数量重试）离线单测。

运行：.venv/Scripts/python.exe tests/test_v76_caption_quality.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import types

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "video_editing"))

import content_analysis as ca  # noqa: E402
import workflow as wf_mod  # noqa: E402


def _picked(n=2, with_desc=True):
    out = []
    for i in range(n):
        lab = {"person_count": 1, "scene": f"场景{i}", "activity": "行走",
               "mood": "轻快", "tags": [f"标签{i}"], "quality": "good",
               "usable": True}
        if with_desc:
            lab["desc"] = f"穿蓝外套的旅人背着包走向湖边，晨雾还没散"
        out.append({"duration": 4.0 + i, "label": lab})
    return out


class _FakeModelModule:
    """sys.modules["model"] 替身：按队列吐回复，记录每次收到的 prompt。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def get_model(self):
        outer = self

        class _M:
            def invoke(self, prompt):
                outer.prompts.append(prompt)
                content = outer.responses.pop(0) if outer.responses else "[]"

                class _R:
                    pass
                _R.content = content
                return _R()
        return _M()


def _with_fake_model(responses, fn):
    fake = _FakeModelModule(responses)
    saved = sys.modules.get("model")
    sys.modules["model"] = fake
    try:
        return fn(fake)
    finally:
        if saved is not None:
            sys.modules["model"] = saved
        else:
            sys.modules.pop("model", None)


def t01_sanitize_keeps_desc_bounded():
    lab = ca.sanitize_label({"desc": "很" * 100, "scene": "草地", "quality": "good"})
    assert 0 < len(lab["desc"]) <= 48, lab["desc"]
    assert ca.sanitize_label({})["desc"] == ""          # 旧卡/缺字段优雅为空
    assert "desc" in ca._TAG_PROMPT                     # 打标提示词要求 desc


def t02_captions_prompt_uses_desc_as_input():
    def run(fake):
        caps, src = wf_mod._write_captions(_picked(2, with_desc=True))
        return fake.prompts, caps, src
    prompts, caps, src = _with_fake_model(['["钩子句", "收束句"]'], run)
    assert src == "llm" and caps == ["钩子句", "收束句"]
    listing = prompts[0]
    assert "穿蓝外套的旅人" in listing, listing[:300]   # desc 进入文案输入
    assert "标签=" not in listing                        # 有 desc 就不再堆标签词


def t03_captions_prompt_falls_back_to_tags_for_old_cards():
    """旧镜头卡没有 desc：退回标签清单，不报错。"""
    def run(fake):
        caps, src = wf_mod._write_captions(_picked(2, with_desc=False))
        return fake.prompts[0], caps, src
    prompt, caps, src = _with_fake_model(['["钩子句", "收束句"]'], run)
    assert src == "llm"
    assert "标签=标签0" in prompt and "场景=场景0" in prompt


def t04_prompt_has_fewshot_banned_words_and_structure():
    def run(fake):
        caps, src = wf_mod._write_captions(_picked(1))
        return fake.prompts[0]
    prompt = _with_fake_model(['["钩子句"]'], run)
    assert "禁用" in prompt and "岁月静好" in prompt     # 负面清单
    assert "钩子" in prompt and "收束" in prompt         # 叙事结构
    assert "把车开到了没有信号的地方" in prompt           # few-shot 示例
    assert "正好 1 项" in prompt                          # 数量硬约束声明


def t05_count_mismatch_retries_with_feedback():
    """第一次句数不齐 → 带错误反馈重试一次 → 命中。"""
    def run(fake):
        caps, src = wf_mod._write_captions(_picked(2))
        return len(fake.prompts), fake.prompts[-1], caps, src
    n_calls, last_prompt, caps, src = _with_fake_model(
        ['["只有一句"]', '["第一句", "第二句"]'], run)
    assert src == "llm" and caps == ["第一句", "第二句"]
    assert n_calls == 2
    assert "只有 1 句" in last_prompt                    # 反馈带上了上次错在哪


def t06_two_failures_report_failed():
    """两次都不行 → (None, "failed")，调用方退标签兜底。"""
    caps, src = _with_fake_model(
        ['["只有一句"]', '["还是一句"]'],
        lambda fake: wf_mod._write_captions(_picked(2)))
    assert caps is None and src == "failed"


def t07_parse_failure_also_retries():
    def run(fake):
        caps, src = wf_mod._write_captions(_picked(1))
        return len(fake.prompts), caps, src
    n_calls, caps, src = _with_fake_model(
        ['这不是 JSON', '["好了"]'], run)
    assert src == "llm" and caps == ["好了"] and n_calls == 2


def t08_bottom_captions_lifted_by_canvas_ratio():
    """V7.6：底部字幕按画高比例抬离底缘（避相机水印），渲染命令用换算像素。"""
    import plan_compiler
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "INPUT"), exist_ok=True)
        with open(os.path.join(tmp, "INPUT", "a.mp4"), "wb") as f:
            f.write(b"x" * 64)
        import content_analysis as ca_mod
        card = {"schema_version": 2, "source": "INPUT/a.mp4", "duration": 10,
                "signals": {"audio": {"silences": None, "silence_ratio": None},
                            "video": {"blacks": []}},
                "shots": [{"start": 0, "end": 5, "label": {"person_count": 0,
                           "scene": "日落", "activity": "空镜", "mood": "", "tags": [],
                           "quality": "good", "usable": True}},
                          {"start": 5, "end": 10, "label": {"person_count": 0,
                           "scene": "海边", "activity": "空镜", "mood": "", "tags": [],
                           "quality": "good", "usable": True}}]}
        full = os.path.join(tmp, "INPUT", "a.mp4")
        ca_mod._save_json(ca_mod._sidecar_path(tmp, full, "INPUT/a.mp4"), card)
        plan = {"schema_version": "2.0",
                "output": {"filename": "OUTPUT/o.mp4",
                           "resolution": {"width": 640, "height": 360}},
                "workflow": {"name": "smart_create", "budget_seconds": 10,
                             "transition": 0,
                             "captions": ["山间日落", "海边散步"]}}
        result = plan_compiler.compile_plan(plan, tmp)
        ovs = result.plan.get("overlays") or []
        assert len(ovs) == 2
        # 640x360 → 360 × 0.12 = 43.2
        assert all(abs(o["y_margin"] - 43.2) < 1e-6 for o in ovs), ovs
        render = next(c for c in result.commands if c.stage == "render")
        assert "y=h-text_h-43" in " ".join(render.argv)
        # credits 滚动体不携带 y_margin（整块滚动，位置由 y 表达式控制）
        cards = {"INPUT/a.mp4": card}
        plan2 = dict(plan)
        plan2["workflow"] = {"name": "smart_create", "budget_seconds": 10,
                             "transition": 0, "subtitle_style": "credits",
                             "captions": ["【致谢】", "所有人"]}
        result2 = plan_compiler.compile_plan(plan2, tmp)
        ov2 = (result2.plan.get("overlays") or [])[0]
        assert ov2["scroll"] == "up" and "y_margin" not in ov2


def t09_schema_y_margin():
    import plan_schema
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "INPUT"), exist_ok=True)
        with open(os.path.join(tmp, "INPUT", "a.mp4"), "wb") as f:
            f.write(b"x" * 64)
        base = {"schema_version": "2.0",
                "output": {"filename": "OUTPUT/o.mp4",
                           "resolution": {"width": 640, "height": 360}},
                "clips": [{"id": "c1", "source": "INPUT/a.mp4", "kind": "video",
                           "trim_start": 0, "trim_end": 5}],
                "timeline": [{"clip": "c1"}],
                "overlays": [{"type": "text", "text": "hi", "at_clip": "c1",
                              "start_offset": 0, "duration": 2,
                              "position": "bottom", "y_margin": 100}]}
        assert plan_schema.validate_plan(base, tmp) == []
        bad = dict(base)
        bad["overlays"] = [dict(base["overlays"][0], y_margin=-3)]
        assert any("y_margin" in e for e in plan_schema.validate_plan(bad, tmp))


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
    print(f"\nV7.6 caption quality 离线测试：{total - failed}/{total} 项通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
