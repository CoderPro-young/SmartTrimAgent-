"""预览档渲染质量（compile_plan quality="preview"）离线单测。

预览档 = 同一条滤镜链在低分辨率/快档编码下出片，产物落 *_preview.* 文件，
plan 本身不被修改；正式导出以 final 档重编译。归一化缓存键两档天然分开。

运行：.venv/Scripts/python.exe tests/test_preview_quality.py
"""

from __future__ import annotations

import os
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "video_editing"))

import plan_compiler   # noqa: E402
from workflow import CAPTION_STYLES  # noqa: E402


def _shot(s, e, scene="草地", quality="good", pc=0):
    return {"start": s, "end": e,
            "label": {"person_count": pc, "scene": scene, "activity": "空镜",
                      "mood": "", "tags": [], "quality": quality, "usable": quality != "poor"}}


def _card(rel, shots, dur=None):
    return {"schema_version": 2, "source": rel, "duration": dur,
            "signals": {"audio": {"silences": None, "silence_ratio": None},
                        "video": {"blacks": []}},
            "shots": shots}


def _make_project(tmp: str):
    os.makedirs(os.path.join(tmp, "INPUT"), exist_ok=True)
    for n in ("a.mp4", "b.mp4"):
        with open(os.path.join(tmp, "INPUT", n), "wb") as f:
            f.write(b"x" * 64)
    return tmp


def _ctx(tmp, cards):
    import content_analysis as ca
    for rel, card in cards.items():
        full = os.path.join(tmp, rel)
        ca._save_json(ca._sidecar_path(tmp, full, rel), card)
    probes = {rel: {"duration": c.get("duration"), "has_audio": True,
                    "has_video": True} for rel, c in cards.items()}
    return {"cards": cards, "probes": probes, "project_root": tmp}


CARDS = {
    "INPUT/a.mp4": _card("INPUT/a.mp4",
                         [_shot(0, 6, "日落"), _shot(6, 12, "海边")], dur=12),
    "INPUT/b.mp4": _card("INPUT/b.mp4", [_shot(0, 8, "草地")], dur=8),
}

LINES = ["【特别感谢】", "勇敢出发的自己", "【出品】", "深剪智能"]


def _plan(tmp, wf_extra, out="OUTPUT/final.mp4", w=1280, h=720):
    return {"schema_version": "2.0",
            "output": {"filename": out,
                       "resolution": {"width": w, "height": h}},
            "workflow": {"name": "smart_create", "budget_seconds": 12,
                         "subtitle_style": "credits", "captions": LINES,
                         **wf_extra}}


def _compile(tmp, quality, transition=0, w=1280, h=720):
    _ctx(tmp, {"INPUT/a.mp4": CARDS["INPUT/a.mp4"]})
    plan = _plan(tmp, {"transition": transition}, w=w, h=h)
    return plan, plan_compiler.compile_plan(plan, tmp, quality=quality)


def t01_preview_scales_canvas_and_keeps_plan_clean():
    """预览档：math 画布缩到短边 540、plan 原样、产物落 _preview 文件。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        plan, result = _compile(tmp, "preview")
        m = result.math
        assert m["preview"] is True
        assert (m["width"], m["height"]) == (960, 540), (m["width"], m["height"])
        assert (m["src_width"], m["src_height"]) == (1280, 720)
        # plan 不被档位污染：分辨率与文件名保持原样
        assert plan["output"]["resolution"] == {"width": 1280, "height": 720}
        assert plan["output"]["filename"] == "OUTPUT/final.mp4"
        render = next(c for c in result.commands if c.stage == "render")
        assert render.argv[-1] == "OUTPUT/final_preview.mp4", render.argv[-1]
        # 同一计划 final 档：无 preview 标记、原分辨率、原文件名
        _make_project(tmp)
        _, result_f = _compile(tmp, "final")
        assert "preview" not in result_f.math
        assert (result_f.math["width"], result_f.math["height"]) == (1280, 720)
        render_f = next(c for c in result_f.commands if c.stage == "render")
        assert render_f.argv[-1] == "OUTPUT/final.mp4"


def t02_codec_args_split_by_quality():
    """归一化/渲染的编码参数分档：preview=ultrafast，final 维持原档。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        _, result = _compile(tmp, "preview")
        norm = next(c for c in result.commands if c.stage == "normalize")
        assert "-preset" in norm.argv and \
            norm.argv[norm.argv.index("-preset") + 1] == "ultrafast", norm.argv
        render = next(c for c in result.commands if c.stage == "render")
        assert "-preset" in render.argv and \
            render.argv[render.argv.index("-preset") + 1] == "ultrafast"
        assert render.argv[render.argv.index("-crf") + 1] == "24"

        _make_project(tmp)
        _, result_f = _compile(tmp, "final")
        norm_f = next(c for c in result_f.commands if c.stage == "normalize")
        assert norm_f.argv[norm_f.argv.index("-preset") + 1] == "veryfast"
        render_f = next(c for c in result_f.commands if c.stage == "render")
        assert render_f.argv[render_f.argv.index("-crf") + 1] == "20"
        assert "ultrafast" not in " ".join(render_f.argv)


def t03_drawtext_scales_with_canvas():
    """预览档字号/描边/行距按画布缩放比缩小，成品档保持原值。"""
    st = CAPTION_STYLES["credits"]          # 计划显式指定 subtitle_style=credits
    ratio = 540 / 720
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        _, result = _compile(tmp, "preview")
        render = next(c for c in result.commands if c.stage == "render")
        arg = " ".join(render.argv)
        fs_p = max(12, int(round(st["font_size"] * ratio)))
        ls_p = int(round(st["line_spacing"] * ratio))
        bw_p = int(round(st["stroke"] * ratio))
        assert f"fontsize={fs_p}" in arg, arg[:400]
        assert f"line_spacing={ls_p}" in arg, arg[:400]
        assert f"borderw={bw_p}" in arg, arg[:400]

        _make_project(tmp)
        _, result_f = _compile(tmp, "final")
        render_f = next(c for c in result_f.commands if c.stage == "render")
        arg_f = " ".join(render_f.argv)
        assert f"fontsize={st['font_size']}" in arg_f, arg_f[:400]
        assert f"line_spacing={st['line_spacing']}" in arg_f, arg_f[:400]


def t04_transition_path_also_previews():
    """xfade 转场路径：预览档同样低分辨率快编 + _preview 文件名。"""
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        _, result = _compile(tmp, "preview", transition=0.4)
        render = next(c for c in result.commands if c.stage == "render")
        arg = " ".join(render.argv)
        assert "xfade" in arg and "ultrafast" in arg, arg[:400]
        assert render.argv[-1] == "OUTPUT/final_preview.mp4"


def t05_cache_and_filename_units():
    """小画布不缩放只打标；缓存键两档不同；output_filename 单元断言。"""
    import re
    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        # 短边已 ≤540：不缩放，但仍打 preview 标（快编生效、字号不缩放）
        _, result = _compile(tmp, "preview", w=640, h=360)
        m = result.math
        assert m["preview"] is True and (m["width"], m["height"]) == (640, 360)
        assert "src_height" not in m

        clip = {"source": "INPUT/a.mp4", "trim_start": 0, "trim_end": 4}
        kw = dict(clip=clip, src="INPUT/a.mp4", vf="eq", af="",
                  W=1280, H=720, fps=30, d=4.0, amap="0:a:0?")
        h_final = plan_compiler._norm_cache_path(tmp, preview=False, **kw)
        h_prev = plan_compiler._norm_cache_path(tmp, preview=True, **kw)
        assert h_final != h_prev
        # 内容寻址：同参数键确定性，路径形如 TMP/norm/<sha1前16位>.mp4
        assert h_final == plan_compiler._norm_cache_path(tmp, preview=False, **kw)
        assert re.fullmatch(r"TMP/norm/[0-9a-f]{16}\.mp4", h_final), h_final

        plan = {"output": {"filename": "OUTPUT/final.mp4"}}
        assert plan_compiler.output_filename(plan, {"preview": True}) == \
            "OUTPUT/final_preview.mp4"
        assert plan_compiler.output_filename(plan, {}) == "OUTPUT/final.mp4"


def t06_prepass_args_in_artifact_name():
    """v7.7 已知边界修复：prepass 参数哈希进产物名，改打码参数不再命中旧缓存。"""
    import re
    def _mosaic_plan(score):
        return {"schema_version": "2.0",
                "output": {"filename": "OUTPUT/o.mp4",
                           "resolution": {"width": 640, "height": 480}},
                "clips": [{"id": "c1", "source": "INPUT/a.mp4", "kind": "video",
                           "trim_end": 5,
                           "probe": {"duration": 10, "width": 1280, "height": 720,
                                     "fps": 30, "has_audio": True, "has_video": True},
                           "effects": [{"name": "face_mosaic",
                                        "args": {"mode": "mosaic",
                                                 "score_threshold": score}}]}],
                "timeline": [{"clip": "c1"}]}

    def _prepass_out(result):
        det = next(c for c in result.commands if c.stage == "detect")
        return det.argv[det.argv.index("--output") + 1]

    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        r1 = plan_compiler.compile_plan(_mosaic_plan(0.4), tmp)
        out1 = _prepass_out(r1)
        assert re.fullmatch(r"TMP/c1_face_mosaic_[0-9a-f]{8}\.mp4", out1), out1
        # 参数不变 → 产物名稳定（幂等，可被存在即跳过逻辑复用）
        assert _prepass_out(plan_compiler.compile_plan(_mosaic_plan(0.4), tmp)) == out1
        # 改打码参数 → 产物名变，且归一化输入跟着变（归一化缓存键含 src，
        # 自动失效——旧固定名时代这里会命中旧缓存，新效果不出片）
        r2 = plan_compiler.compile_plan(_mosaic_plan(0.6), tmp)
        out2 = _prepass_out(r2)
        assert out2 != out1, (out1, out2)
        norm1 = next(c for c in r1.commands if c.stage == "normalize")
        norm2 = next(c for c in r2.commands if c.stage == "normalize")
        i1 = norm1.argv[norm1.argv.index("-i") + 1]
        i2 = norm2.argv[norm2.argv.index("-i") + 1]
        assert i1 == out1 and i2 == out2, (i1, i2)


def t07_prepass_artifact_reuse():
    """prepass 产物存在且非空 → 跳过全片重跑；空文件不跳过。"""
    def _mosaic_plan():
        return {"schema_version": "2.0",
                "output": {"filename": "OUTPUT/o.mp4",
                           "resolution": {"width": 640, "height": 480}},
                "clips": [{"id": "c1", "source": "INPUT/a.mp4", "kind": "video",
                           "trim_end": 5,
                           "probe": {"duration": 10, "width": 1280, "height": 720,
                                     "fps": 30, "has_audio": True, "has_video": True},
                           "effects": [{"name": "face_mosaic",
                                        "args": {"mode": "mosaic"}}]}],
                "timeline": [{"clip": "c1"}]}

    with tempfile.TemporaryDirectory() as tmp:
        _make_project(tmp)
        r1 = plan_compiler.compile_plan(_mosaic_plan(), tmp)
        det = next(c for c in r1.commands if c.stage == "detect")
        out = det.argv[det.argv.index("--output") + 1]
        # 产物非空 → replan 未改参数时不再生成 detect 命令，归一化输入不变
        os.makedirs(os.path.join(tmp, "TMP"), exist_ok=True)
        with open(os.path.join(tmp, out), "wb") as f:
            f.write(b"x" * 64)
        r2 = plan_compiler.compile_plan(_mosaic_plan(), tmp)
        assert not [c for c in r2.commands if c.stage == "detect"]
        norm = next(c for c in r2.commands if c.stage == "normalize")
        assert norm.argv[norm.argv.index("-i") + 1] == out
        # 空文件（上次失败的半成品）不视为有效缓存 → 重跑
        with open(os.path.join(tmp, out), "wb") as f:
            f.write(b"")
        r3 = plan_compiler.compile_plan(_mosaic_plan(), tmp)
        assert [c for c in r3.commands if c.stage == "detect"]


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
    print(f"\n预览档渲染质量离线测试：{total - failed}/{total} 项通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
