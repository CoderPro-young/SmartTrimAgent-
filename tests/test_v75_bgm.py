"""V7.5 自动配乐（workflow.bgm / 自动选曲注入顶层 audio 块）离线单测。

运行：.venv/Scripts/python.exe tests/test_v75_bgm.py
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import types

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "video_editing"))

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


def _make_project(tmp: str, audio_files=("a.mp3", "b.mp3")):
    os.makedirs(os.path.join(tmp, "INPUT"), exist_ok=True)
    for n in ("a.mp4", *audio_files):
        with open(os.path.join(tmp, "INPUT", n), "wb") as f:
            f.write(b"x" * 64)
    return tmp


def _ctx(tmp, music, probes=None, plan_audio=None):
    return {
        "cards": {"INPUT/a.mp4": _card("INPUT/a.mp4", [_shot(0, 6, "日落")], dur=6)},
        "probes": probes or {"INPUT/a.mp4": {"duration": 6, "has_audio": True,
                                             "has_video": True}},
        "music": music,
        "plan_audio": plan_audio,
        "project_root": tmp,
    }


MUSIC_ONE = {"INPUT/a.mp3": {"duration": 120}}
MUSIC_TWO = {"INPUT/a.mp3": {"duration": 120}, "INPUT/b.mp3": {"duration": 90}}


@contextlib.contextmanager
def _fake_llm(respond):
    """把 workflow._pick_bgm_llm 里的 `from model import get_model` 换成假模型。

    respond(prompt) -> 字符串回复（或抛异常模拟模型不可用）。
    """
    saved = sys.modules.get("model")

    def invoke(prompt, *a, **kw):
        return types.SimpleNamespace(content=respond(prompt))

    sys.modules["model"] = types.SimpleNamespace(
        get_model=lambda: types.SimpleNamespace(invoke=invoke))
    try:
        yield
    finally:
        if saved is not None:
            sys.modules["model"] = saved
        else:
            sys.modules.pop("model", None)


def t01_keys_and_docs():
    assert "bgm" in wf_mod.WORKFLOW_KEYS
    doc = wf_mod.render_workflow_doc()
    assert "Auto-BGM" in doc and 'bgm: false' in doc, doc


def t02_schema_bgm_validation():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        base = {"schema_version": "2.0",
                "output": {"filename": "OUTPUT/o.mp4",
                           "resolution": {"width": 640, "height": 480}},
                "workflow": {"name": "one_click_reel"}}

        def errs(wf_extra, audio=None):
            plan = {**base,
                    "workflow": {**base["workflow"], **wf_extra}}
            if audio:
                plan["audio"] = audio
            return plan_schema.validate_plan(plan, tmp)

        assert errs({}) == []
        assert errs({"bgm": False}) == []
        assert errs({"bgm": True}) == []
        assert errs({"bgm": "INPUT/a.mp3"}) == []
        assert errs({"bgm": "MUSIC/mixkit-pop-03-700.mp3"}) == []
        assert errs({"bgm": "轻松欢快"}) == []          # 氛围描述自由文本
        assert any("workflow.bgm" in e for e in errs({"bgm": ""}))
        assert any("workflow.bgm" in e for e in errs({"bgm": 123}))
        assert any("workflow.bgm" in e
                   for e in errs({"bgm": {"source": "INPUT/a.mp3"}}))
        # speech_clean 不消费 bgm
        plan = {**base, "workflow": {"name": "speech_clean", "bgm": False}}
        assert any("workflow.bgm" in e for e in plan_schema.validate_plan(plan, tmp))
        # workflow 与顶层 audio 块共存合法（audio 优先于自动配乐）；
        # audio.source 允许 MUSIC/ 内置曲库（文件需真实存在）
        os.makedirs(os.path.join(tmp, "MUSIC"), exist_ok=True)
        with open(os.path.join(tmp, "MUSIC", "m.mp3"), "wb") as f:
            f.write(b"x" * 64)
        assert errs({}, audio={"source": "INPUT/a.mp3"}) == []
        assert errs({}, audio={"source": "MUSIC/m.mp3"}) == []
        plan = {**base, "audio": {"source": "MUSIC/不存在.mp3"}}
        assert any("文件不存在" in e for e in plan_schema.validate_plan(plan, tmp))


def t03_auto_single_candidate():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        ctx = _ctx(tmp, MUSIC_ONE)
        plan, report = wf_mod.WORKFLOWS["one_click_reel"].expand({}, ctx)
        audio = plan["audio"]
        assert audio["source"] == "INPUT/a.mp3"
        assert audio["loop"] is True
        # V7.7：自动配乐是纯 BGM——去掉素材原声，音乐即成片声音
        assert audio["original"] == "mute"
        assert audio["volume"] == wf_mod.BGM_SOLO_VOLUME
        assert "ducking" not in audio
        assert audio["fade_in"] == wf_mod.BGM_FADE_IN
        assert audio["fade_out"] == wf_mod.BGM_FADE_OUT
        assert report["bgm"]["selection"] == "auto"
        assert report["bgm"]["original"] == "mute"


def t04_ducking_is_explicit_only():
    """ducking 不再由自动配乐产出——保留口播走显式 audio 块（原样透传）。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        probes = {"INPUT/a.mp4": {"duration": 6, "has_audio": False,
                                  "has_video": True}}
        plan, _ = wf_mod.WORKFLOWS["one_click_reel"].expand(
            {}, _ctx(tmp, MUSIC_ONE, probes))
        assert plan["audio"]["original"] == "mute"

        mine = {"source": "INPUT/a.mp3", "volume": 0.4, "loop": True,
                "original": "keep", "ducking": True}
        plan, report = wf_mod.WORKFLOWS["one_click_reel"].expand(
            {}, _ctx(tmp, MUSIC_ONE, plan_audio=mine))
        assert "audio" not in plan            # 用户显式块由编译器合并保留
        assert report["bgm"] == {"selection": "plan"}


def t05_plan_audio_wins():
    """计划自带 audio 时 expand 不注入——合并后以用户原参数为准（编译器职责）。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        mine = {"source": "INPUT/a.mp3", "volume": 0.5, "loop": True}
        plan, report = wf_mod.WORKFLOWS["one_click_reel"].expand(
            {}, _ctx(tmp, MUSIC_ONE, plan_audio=mine))
        assert "audio" not in plan
        assert report["bgm"] == {"selection": "plan"}


def t06_bgm_false_disables():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        plan, report = wf_mod.WORKFLOWS["one_click_reel"].expand(
            {"bgm": False}, _ctx(tmp, MUSIC_ONE))
        assert "audio" not in plan
        assert report["bgm"] == {"selection": "disabled"}


def t07_no_music_reports_none():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp, audio_files=())
        plan, report = wf_mod.WORKFLOWS["one_click_reel"].expand({}, _ctx(tmp, {}))
        assert "audio" not in plan
        assert report["bgm"]["selection"] == "none"


def t08_multi_candidates_llm_picks():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        seen = {}

        def respond(prompt):
            seen["prompt"] = prompt
            return '前置噪声 {"source": "INPUT/b.mp3", "reason": "轻快贴题"} 后置噪声'

        with _fake_llm(respond):
            plan, report = wf_mod.WORKFLOWS["one_click_reel"].expand(
                {"keyword": "日落"}, _ctx(tmp, MUSIC_TWO))
        assert plan["audio"]["source"] == "INPUT/b.mp3"
        assert report["bgm"]["selection"] == "llm"
        assert report["bgm"]["reason"] == "轻快贴题"
        assert "日落" in seen["prompt"] and "INPUT/a.mp3" in seen["prompt"]


def t09_llm_unavailable_falls_back():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)

        def respond(_prompt):
            raise RuntimeError("no key")

        with _fake_llm(respond):
            plan, report = wf_mod.WORKFLOWS["one_click_reel"].expand(
                {}, _ctx(tmp, MUSIC_TWO))
        assert plan["audio"]["source"] == "INPUT/a.mp3"   # 排序第一的候选
        assert report["bgm"]["selection"] == "auto"
        assert "回退" in (report["bgm"]["reason"] or "")


def t10_llm_off_list_falls_back():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        with _fake_llm(lambda _p: '{"source": "INPUT/hack.mp3", "reason": "x"}'):
            plan, report = wf_mod.WORKFLOWS["one_click_reel"].expand(
                {}, _ctx(tmp, MUSIC_TWO))
        assert plan["audio"]["source"] == "INPUT/a.mp3"
        assert report["bgm"]["selection"] == "auto"


def t11_explicit_track():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        plan, report = wf_mod.WORKFLOWS["one_click_reel"].expand(
            {"bgm": "INPUT/b.mp3"}, _ctx(tmp, MUSIC_TWO))
        assert plan["audio"]["source"] == "INPUT/b.mp3"
        assert report["bgm"]["selection"] == "explicit"
        try:
            wf_mod.WORKFLOWS["one_click_reel"].expand(
                {"bgm": "INPUT/nope.mp3"}, _ctx(tmp, MUSIC_TWO))
            raise AssertionError("不存在的指定曲目应抛 ValueError")
        except ValueError as exc:
            assert "曲目不可用" in str(exc)


def t12_smart_create_auto_bgm():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp, audio_files=())
        ctx = _ctx(tmp, {"INPUT/quiet.mp3": {"duration": 200}})
        plan, report = wf_mod.WORKFLOWS["smart_create"].expand(
            {"captions": ["山间日落", "风穿过草地"], "budget_seconds": 12}, ctx)
        assert plan["audio"]["source"] == "INPUT/quiet.mp3"
        assert report["bgm"]["selection"] == "auto"


def t13_music_dir_priority():
    """内置曲库 MUSIC/ 排在 INPUT/ 纯音频之前——回退第一候选落在曲库上。"""
    ctx_music = {"INPUT/z.mp3": {"duration": 60},
                 "MUSIC/b.mp3": {"duration": 120},
                 "INPUT/a.mp3": {"duration": 30},
                 "MUSIC/a.mp3": {"duration": 90}}
    cands = wf_mod._music_candidates({"music": ctx_music})
    assert [c["source"] for c in cands] == \
        ["MUSIC/a.mp3", "MUSIC/b.mp3", "INPUT/a.mp3", "INPUT/z.mp3"]
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        with _fake_llm(lambda _p: (_ for _ in ()).throw(RuntimeError("no key"))):
            plan, report = wf_mod.WORKFLOWS["one_click_reel"].expand(
                {}, _ctx(tmp, ctx_music))
        assert plan["audio"]["source"] == "MUSIC/a.mp3"
        assert report["bgm"]["selection"] == "auto"
        # 显式指定曲库曲目同样可用
        plan, report = wf_mod.WORKFLOWS["one_click_reel"].expand(
            {"bgm": "MUSIC/b.mp3"}, _ctx(tmp, ctx_music))
        assert plan["audio"]["source"] == "MUSIC/b.mp3"
        assert report["bgm"]["selection"] == "explicit"


def t14_gather_ctx_music_dir_hermetic():
    """MUSIC/ 目录扫描不炸、无效文件不进任何池（离线，无 ffprobe 依赖）。"""
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "MUSIC"), exist_ok=True)
        os.makedirs(os.path.join(tmp, "INPUT"), exist_ok=True)
        for n in ("a.mp3", "readme.md"):
            with open(os.path.join(tmp, "MUSIC", n), "wb") as f:
                f.write(b"x" * 64)
        with open(os.path.join(tmp, "INPUT", "v.mp4"), "wb") as f:
            f.write(b"x" * 64)
        import plan_compiler
        ctx = plan_compiler._gather_ctx(tmp)
        # 无 ffprobe 语义下文件无法分类：MUSIC/ 文件不进 music 也不进视频池
        assert ctx["music"] == {} and ctx["cards"] == {}
        assert not any(rel.startswith("MUSIC/")
                       for rel in ctx["probes"])


def t15_bgm_mood_description():
    """bgm 自由文本 = 氛围描述：透传给选曲 prompt（含曲库标签元数据），
    失败回退第一候选；不存在的路径前缀串仍报错。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        os.makedirs(os.path.join(tmp, "MUSIC"), exist_ok=True)
        with open(os.path.join(tmp, "MUSIC", "meta.json"), "w",
                  encoding="utf-8") as f:
            json.dump([{"file": "MUSIC/a.mp3", "title": "Just Kidding",
                        "genre": "Children",
                        "tags": ["Humorous", "Lifestyle"]}], f,
                      ensure_ascii=False)
        music = {"MUSIC/a.mp3": {"duration": 179},
                 "MUSIC/b.mp3": {"duration": 120}}
        seen = {}

        def respond(prompt):
            seen["prompt"] = prompt
            return '{"source": "MUSIC/b.mp3", "reason": "轻快贴题"}'

        with _fake_llm(respond):
            plan, report = wf_mod.WORKFLOWS["one_click_reel"].expand(
                {"bgm": "轻松欢快"}, _ctx(tmp, music))
        assert plan["audio"]["source"] == "MUSIC/b.mp3"
        assert report["bgm"]["selection"] == "llm"
        assert report["bgm"]["style"] == "轻松欢快"
        assert "轻松欢快" in seen["prompt"]
        assert "标签 Humorous/Lifestyle" in seen["prompt"]   # 曲库标签进候选
        assert "曲风 Children" in seen["prompt"]

        # LLM 不可用 → 回退曲库第一候选，style 仍记录在报告
        with _fake_llm(lambda _p: (_ for _ in ()).throw(RuntimeError("x"))):
            plan, report = wf_mod.WORKFLOWS["one_click_reel"].expand(
                {"bgm": "旅游 vlog"}, _ctx(tmp, music))
        assert plan["audio"]["source"] == "MUSIC/a.mp3"
        assert report["bgm"]["selection"] == "auto"
        assert report["bgm"]["style"] == "旅游 vlog"

        # 带路径前缀但不存在 → 仍报错（精确指定不静默换曲）
        try:
            wf_mod.WORKFLOWS["one_click_reel"].expand(
                {"bgm": "MUSIC/nope.mp3"}, _ctx(tmp, music))
            raise AssertionError("缺失的指定曲目应抛 ValueError")
        except ValueError as exc:
            assert "曲目不可用" in str(exc)


def t16_candidates_carry_meta():
    """_music_candidates 附带曲库 meta（title/genre/tags）；无元数据时不带键。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        os.makedirs(os.path.join(tmp, "MUSIC"), exist_ok=True)
        with open(os.path.join(tmp, "MUSIC", "meta.json"), "w",
                  encoding="utf-8") as f:
            json.dump([{"file": "MUSIC/a.mp3", "title": "Vastness",
                        "genre": "Ambient",
                        "tags": ["Atmospheric", "Cinematic"],
                        "bpm": 123.0}], f, ensure_ascii=False)
        ctx = _ctx(tmp, {"MUSIC/a.mp3": {"duration": 230},
                         "INPUT/u.mp3": {"duration": 60}})
        cands = wf_mod._music_candidates(ctx)
        by_src = {c["source"]: c for c in cands}
        assert by_src["MUSIC/a.mp3"]["genre"] == "Ambient"
        assert by_src["MUSIC/a.mp3"]["title"] == "Vastness"
        assert by_src["MUSIC/a.mp3"]["tags"] == ["Atmospheric", "Cinematic"]
        assert "bpm" not in by_src["MUSIC/a.mp3"]      # 白名单外字段不透传
        assert "genre" not in by_src["INPUT/u.mp3"]
        assert cands[0]["source"] == "MUSIC/a.mp3"    # 曲库优先


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
    print(f"\nV7.5 自动配乐离线测试：{total - failed}/{total} 项通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
