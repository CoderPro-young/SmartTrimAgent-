"""V7.7 音频控制离线单测：顶层 audio.original + clip 级 audio 逐段控声。

运行：.venv/Scripts/python.exe tests/test_v77_audio_control.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "video_editing"))

import plan_compiler   # noqa: E402
import plan_schema     # noqa: E402


def _make_project(tmp: str):
    os.makedirs(os.path.join(tmp, "INPUT"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "MUSIC"), exist_ok=True)
    for n in ("a.mp4", "narration.m4a"):
        with open(os.path.join(tmp, "INPUT", n), "wb") as f:
            f.write(b"x" * 64)
    with open(os.path.join(tmp, "MUSIC", "m.mp3"), "wb") as f:
        f.write(b"x" * 64)
    return tmp


def _plan(tmp: str, clip_extra: dict | None = None, audio: dict | None = None):
    plan = {
        "schema_version": "2.0",
        "output": {"filename": "OUTPUT/o.mp4",
                   "resolution": {"width": 640, "height": 480}},
        "clips": [{"id": "c1", "source": "INPUT/a.mp4", "kind": "video",
                   "trim_end": 5,
                   "probe": {"duration": 10, "width": 1280, "height": 720,
                             "fps": 30, "has_audio": True, "has_video": True},
                   **(clip_extra or {})}],
        "timeline": [{"clip": "c1"}],
    }
    if audio:
        plan["audio"] = audio
    return plan


def t01_audio_original_field():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        bgm = {"source": "MUSIC/m.mp3"}
        # V7.7.1：不写 original = 缺省 mute（加配乐即删原声）
        assert plan_schema.validate_plan(_plan(tmp, audio=bgm), tmp) == []
        assert plan_schema.validate_plan(
            _plan(tmp, audio={**bgm, "original": "mute", "volume": 1.0}),
            tmp) == []
        # ducking 单独出现 → 判错：保留原声必须显式 original=keep
        errs = plan_schema.validate_plan(
            _plan(tmp, audio={**bgm, "ducking": True}), tmp)
        assert any("original=keep" in e for e in errs), errs
        assert plan_schema.validate_plan(
            _plan(tmp, audio={**bgm, "original": "keep", "ducking": True,
                              "volume": 0.3}), tmp) == []
        errs = plan_schema.validate_plan(
            _plan(tmp, audio={**bgm, "original": "loud"}), tmp)
        assert any("original" in e for e in errs), errs
        # 显式 mute 与 ducking 互斥（闪避需要原声当触发）
        errs = plan_schema.validate_plan(
            _plan(tmp, audio={**bgm, "original": "mute", "ducking": True}),
            tmp)
        assert any("ducking" in e and "mute" in e for e in errs), errs


def t02_clip_audio_mute():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        assert plan_schema.validate_plan(
            _plan(tmp, clip_extra={"audio": {"mute": True}}), tmp) == []
        # mute 与 source/volume 互斥
        for bad in ({"mute": True, "source": "INPUT/narration.m4a"},
                    {"mute": True, "volume": 0.5},
                    {"mute": True, "fade_in": 1}):
            errs = plan_schema.validate_plan(
                _plan(tmp, clip_extra={"audio": bad}), tmp)
            assert any("互斥" in e for e in errs), (bad, errs)
        # mute 与 cut_silence 冲突（检测依据被覆盖）
        errs = plan_schema.validate_plan(
            _plan(tmp, clip_extra={"audio": {"mute": True},
                                   "cut_silence": {"noise_db": -35}}), tmp)
        assert any("cut_silence" in e and "冲突" in e for e in errs), errs


def t03_clip_audio_replacement():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        ok = {"source": "INPUT/narration.m4a", "volume": 0.9,
              "fade_in": 0.5, "fade_out": 0.5, "loop": True}
        assert plan_schema.validate_plan(
            _plan(tmp, clip_extra={"audio": ok}), tmp) == []
        assert plan_schema.validate_plan(
            _plan(tmp, clip_extra={"audio": {"source": "MUSIC/m.mp3"}}),
            tmp) == []
        # 文件必须存在；loop 无 source 无意义；volume 越界
        errs = plan_schema.validate_plan(
            _plan(tmp, clip_extra={"audio": {"source": "INPUT/缺失.mp3"}}),
            tmp)
        assert any("不存在" in e for e in errs), errs
        errs = plan_schema.validate_plan(
            _plan(tmp, clip_extra={"audio": {"loop": True}}), tmp)
        assert any("loop" in e for e in errs), errs
        errs = plan_schema.validate_plan(
            _plan(tmp, clip_extra={"audio": {"volume": 1.5}}), tmp)
        assert any("volume" in e for e in errs), errs
        # 曲库文件也能换声
        assert plan_schema.validate_plan(
            _plan(tmp, clip_extra={"audio": {"source": "MUSIC/m.mp3"}}),
            tmp) == []


def t04_global_mute_vs_clip_replacement():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        plan = _plan(tmp, clip_extra={"audio": {"source": "INPUT/narration.m4a"}},
                     audio={"source": "MUSIC/m.mp3", "original": "mute"})
        mismatches = plan_schema.check_material_fit(plan, tmp)
        assert any(m.type == "mute_vs_clip_audio" for m in mismatches), \
            [m.type for m in mismatches]
        # clip 级 mute 与全局 mute 不冲突（语义一致：都要静）
        plan2 = _plan(tmp, clip_extra={"audio": {"mute": True}},
                      audio={"source": "MUSIC/m.mp3", "original": "mute"})
        assert not any(m.type == "mute_vs_clip_audio"
                       for m in plan_schema.check_material_fit(plan2, tmp))


# ------------------------------------------------------- 编译器命令层 ---- #

def _math():
    return {"width": 640, "height": 480, "fps": 30,
            "durations": {"c1": 5.0}, "order": ["c1"], "D": 5.0}


def _cmd_argv(cmds):
    assert len(cmds) == 1
    return cmds[0].argv


def t05_normalize_replacement():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        plan = _plan(tmp, clip_extra={
            "audio": {"source": "INPUT/narration.m4a", "volume": 0.8,
                      "fade_in": 0.5}})
        cmds, _side, _paths = plan_compiler._normalize_commands(
            plan, _math(), tmp)
        argv = _cmd_argv(cmds)
        # 替换音频是独立输入，loop 缺省开（铺满片段），apad 兜底
        assert "-stream_loop" in argv and "-1" in argv
        assert "INPUT/narration.m4a" in argv
        assert "anullsrc" not in " ".join(argv)
        i_af = argv.index("-af")
        af = argv[i_af + 1]
        assert "volume=0.8" in af and "apad" in af
        assert "afade=t=in:st=0:d=0.5" in af
        assert argv[argv.index("-map", argv.index("-map") + 1) + 1] == "1:a"


def t06_normalize_mute_and_volume():
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        plan = _plan(tmp, clip_extra={"audio": {"mute": True}})
        argv = _cmd_argv(plan_compiler._normalize_commands(plan, _math(), tmp)[0])
        assert "anullsrc" in " ".join(argv)          # 静音轨顶替原声
        assert "narration.m4a" not in argv
        assert "-af" not in argv                     # 静音没有可调参数

        # 原声调音量（无 source）：amap 仍取原声，af 挂 volume
        plan2 = _plan(tmp, clip_extra={"audio": {"volume": 0.4}})
        argv2 = _cmd_argv(plan_compiler._normalize_commands(plan2, _math(), tmp)[0])
        assert "anullsrc" not in " ".join(argv2)
        assert argv2[argv2.index("-map", argv2.index("-map") + 1) + 1] == "0:a:0?"
        assert "volume=0.4" in argv2[argv2.index("-af") + 1]


def t07_normalize_image_replacement():
    """图片 + 换声：照片配乐。anullsrc 之外多一个音频输入，amap 指向它。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        plan = {"schema_version": "2.0",
                "output": {"filename": "OUTPUT/o.mp4",
                           "resolution": {"width": 640, "height": 480}},
                "clips": [{"id": "c1", "source": "INPUT/narration.m4a",
                           "kind": "image", "duration": 3,
                           "audio": {"source": "MUSIC/m.mp3"}}],
                "timeline": [{"clip": "c1"}]}
        assert plan_schema.validate_plan(plan, tmp) == []
        argv = _cmd_argv(plan_compiler._normalize_commands(plan, _math(), tmp)[0])
        assert "MUSIC/m.mp3" in argv
        assert argv[argv.index("-map", argv.index("-map") + 1) + 1] == "2:a"


def t08_norm_cache_key_distinguishes_audio():
    """同一 clip 只改音频规格 → 缓存路径必须不同（否则回改不重编码）。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        outs = []
        for ca in ({"mute": True},
                   {"source": "INPUT/narration.m4a"},
                   {"source": "INPUT/narration.m4a", "volume": 0.5},
                   None):
            plan = _plan(tmp, clip_extra={"audio": ca} if ca else None)
            _c, _s, paths = plan_compiler._normalize_commands(plan, _math(), tmp)
            outs.append(paths["c1"])
        assert len(set(outs)) == len(outs), outs


def t09_mix_bgm_original_modes():
    # 缺省（不写 original）= mute：BGM 即成片音轨，无 amix
    filters: list[str] = []
    out = plan_compiler._mix_bgm(filters, "[0:a]",
                                 {"source": "MUSIC/m.mp3"}, 1, 5.0)
    assert out == "[bg]"
    assert not any("amix" in f for f in filters), filters

    # 显式 keep（含 ducking）：与原声 amix
    for audio in ({"source": "MUSIC/m.mp3", "original": "keep"},
                  {"source": "MUSIC/m.mp3", "original": "keep",
                   "ducking": True, "volume": 0.3}):
        filters2: list[str] = []
        out2 = plan_compiler._mix_bgm(filters2, "[0:a]", audio, 1, 5.0)
        assert out2 == "[aout]", audio
        assert any("amix" in f for f in filters2), (audio, filters2)
    # keep + ducking：侧链压缩出现在滤镜里
    filters3: list[str] = []
    plan_compiler._mix_bgm(filters3, "[0:a]",
                           {"source": "MUSIC/m.mp3", "original": "keep",
                            "ducking": True}, 1, 5.0)
    assert any("sidechaincompress" in f for f in filters3), filters3


def t10_end_to_end_render_command_mute():
    """缺省（audio 块不写 original）= 纯 BGM：渲染命令映射 [bg]，无 amix。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        plan = _plan(tmp, audio={"source": "MUSIC/m.mp3", "volume": 1.0})
        assert plan_schema.validate_plan(plan, tmp) == []
        math = {"width": 640, "height": 480, "fps": 30,
                "durations": {"c1": 5.0}, "order": ["c1"],
                "starts": {"c1": 0.0}, "transitions": {}, "D": 5.0,
                "norm_paths": {"c1": "TMP/norm/x.mp4"}}
        cmds, _side = plan_compiler._render_no_transition(plan, math)
        argv = cmds[0].argv
        i_fc = argv.index("-filter_complex")
        fc = argv[i_fc + 1]
        assert "amix" not in fc
        assert "[bg]" in fc
        assert argv[argv.index("-map", argv.index("-map") + 1) + 1] == "[bg]"


def t11_transition_render_mute_no_dangling_voice():
    """转场路径 + 纯 BGM：[avoice] 链整条不建（悬空输出会被 ffmpeg 拒绝）。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        plan = {
            "schema_version": "2.0",
            "output": {"filename": "OUTPUT/o.mp4",
                       "resolution": {"width": 640, "height": 480}},
            "clips": [
                {"id": "c1", "source": "INPUT/a.mp4", "kind": "video",
                 "trim_end": 5, "audio": {"mute": True}},
                {"id": "c2", "source": "INPUT/a.mp4", "kind": "video",
                 "trim_start": 5, "trim_end": 9,
                 "audio": {"source": "MUSIC/m.mp3"}},
            ],
            "timeline": [{"clip": "c1"},
                         {"clip": "c2",
                          "transition": {"type": "fade", "duration": 0.4}}],
            "audio": {"source": "MUSIC/m.mp3", "volume": 1.0,
                      "original": "mute"},
        }
        assert plan_schema.validate_plan(plan, tmp) == []
        math = {"width": 640, "height": 480, "fps": 30,
                "durations": {"c1": 5.0, "c2": 4.0}, "order": ["c1", "c2"],
                "starts": {"c1": 0.0, "c2": 4.6},
                "transitions": {1: {"type": "fade", "duration": 0.4}},
                "D": 8.6,
                "norm_paths": {"c1": "TMP/norm/x1.mp4",
                               "c2": "TMP/norm/x2.mp4"}}
        cmds, _side = plan_compiler._render_with_transition(plan, math)
        fc = cmds[0].argv[cmds[0].argv.index("-filter_complex") + 1]
        assert "avoice" not in fc          # 原声链不存在 → 无悬空输出
        assert "acrossfade" not in fc
        assert fc.rstrip().endswith("[bg]") or "[bg]" in fc
        assert "amix" not in fc


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
    print(f"\nV7.7 音频控制离线测试：{total - failed}/{total} 项通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
