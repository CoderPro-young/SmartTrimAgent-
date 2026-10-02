"""dry-run 预检在 xfade 任务上挂起（v7.8 修复）回归测试。

现场：5 片段全接缝转场 + 逐片段字幕的任务，预检报「dry-run 语法预检失败」，
ffmpeg 报错却只有 libx264 启动日志（末行 non-strictly-monotonic PTS）。实测
根因：dry-run 用 0.5s 合成素材顶替真实输入，而 xfade 的 offset 是真实时间轴
起点（可达几十秒）——第一个输入早早 EOF，xfade 等不到 offset 时刻的帧直接
死锁（ffmpeg 6.1 实测不吃 EOF），挂满 60s 超时被杀；且报错提取把超时原因
（error 字段）让位给了无害的 stderr 日志。

修复：_dry_argv_render 把 filter_complex 里 xfade 的 offset 压成 0（语法预检
无需语义保真，实测 offset=0 时 duration 超长也不会挂）；precheck 的报错正文
让运行层原因（超时/启动失败）优先于 stderr 日志。

运行：python tests/test_v78_precheck_offset.py
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "video_editing"))

import plan_compiler as pc   # noqa: E402


def _plan_math():
    """复刻现场形态：5 片段全接缝转场 + 5 条字幕 + 循环 BGM（侧链闪避）。"""
    cids = [f"c{i}" for i in range(1, 6)]
    durs = {"c1": 8.5, "c2": 5.0, "c3": 11.667, "c4": 2.267, "c5": 5.0}
    trans = {i: {"type": "fade", "duration": 0.8} for i in range(1, 5)}
    plan = {
        "output": {"filename": "v78.mp4"},
        "overlays": [
            {"type": "text", "text": f"字幕 {i}", "at_clip": cids[i],
             "start_offset": 0.05, "duration": 1.0,
             "position": "bottom", "font_size": 44, "color": "#FFFFFF"}
            for i in range(5)
        ],
        # V7.7.1 起 ducking 必须伴随显式 original="keep"（缺省 mute 时
        # 原声链整条不建，也就没有 acrossfade 可压）
        "audio": {"source": "MUSIC/bgm.mp3", "volume": 0.3, "fade_in": 1.0,
                  "fade_out": 2.0, "loop": True, "ducking": True,
                  "original": "keep"},
    }
    math = {
        "width": 1920, "height": 1080, "fps": 30,
        "order": cids, "durations": durs, "starts": {}, "transitions": trans,
        "D": 0.0, "offsets": {}, "overlays": [],
        "norm_paths": {cid: f"TMP/norm/v78_{cid}.mp4" for cid in cids},
    }
    S = 0.0
    for i, cid in enumerate(cids):
        math["starts"][cid] = round(S, 2)
        S = round(S + durs[cid] - (trans[i]["duration"] if i in trans else 0.0), 2)
    math["D"] = round(S + durs[cids[-1]], 2)
    math["offsets"] = {cid: math["starts"][cid] for cid in cids
                       if math["starts"][cid] > 0}
    return plan, math


def test_dry_argv_clamps_xfade_offsets():
    plan, math = _plan_math()
    cmds, _sidecars = pc._render_with_transition(plan, math)
    argv = cmds[0].argv
    fc = argv[argv.index("-filter_complex") + 1]
    offs = [float(x) for x in re.findall(r"offset=([\d.]+)", fc)]
    assert offs and max(offs) > pc.PRECHECK_DUR, "用例本身应含超出预检素材的 offset"

    dry = pc._dry_argv_render(
        argv, "TMP/_precheck_1920x1080.mp4", "TMP/_precheck_audio.m4a")
    dfc = dry[dry.index("-filter_complex") + 1]
    offs = [float(x) for x in re.findall(r"offset=([\d.]+)", dfc)]
    assert offs and max(offs) <= pc.PRECHECK_DUR, f"offset 未压平：{offs}"
    ds = [float(x) for x in re.findall(r"acrossfade=d=([\d.]+)", dfc)]
    assert ds and max(ds) <= pc.PRECHECK_ACROSSFADE_D, f"acrossfade d 未压小：{ds}"
    assert dry[-3:] == ["-f", "null", "-"]
    # 真实素材路径不得残留（只查 -i 的输入路径；filter_complex 里的
    # amix normalize=0 等参数含 "norm" 属正常）
    ins = [dry[i + 1] for i, t in enumerate(dry) if t == "-i"]
    assert ins and all(p.endswith(("_precheck_1920x1080.mp4", "_precheck_audio.m4a"))
                       for p in ins), ins


def test_dryrun_no_hang_with_real_ffmpeg():
    if not pc.ffmpeg_exec.ffmpeg_available():
        print("ffmpeg 未安装，跳过")
        return
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "TMP", "norm"))   # 真实流程由 execute() 预先创建
        refs = pc._ensure_precheck_refs(tmp, 320, 180)   # 小画布，秒级
        assert refs is not None
        plan, math = _plan_math()
        cmds, sidecars = pc._render_with_transition(plan, math)
        for path, content in sidecars.items():      # 真实流程由 execute() 先写 sidecar
            with open(os.path.join(tmp, path), "w", encoding="utf-8") as f:
                f.write(content)
        dry = pc._dry_argv_render(cmds[0].argv, *refs)
        t0 = time.time()
        res = pc.ffmpeg_exec.run(dry, cwd=tmp, timeout=30)
        dt = time.time() - t0
        assert res["ok"], (res.get("error"), (res.get("stderr") or "")[-600:])
        assert dt < 30, f"dry-run 耗时 {dt:.1f}s，疑似仍挂起"


def test_timeout_reason_surfaces_over_stderr():
    # 复刻现场：进程被杀时 stderr 只有编码器启动日志，error 字段是唯一线索
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "TMP", "norm"))
        assert pc._ensure_precheck_refs(tmp, 320, 180) is not None

        def fake_run(argv, timeout=60, cwd=None):
            return {"ok": False, "returncode": 1, "stdout": "",
                    "stderr": "[libx264 @ x] using cpu capabilities\n"
                              "[libx264 @ x] profile High, level 4.0\n"
                              "[libx264 @ x] non-strictly-monotonic PTS",
                    "error": "命令超时（>60s）", "command": argv}

        orig = pc.ffmpeg_exec.run
        pc.ffmpeg_exec.run = fake_run
        try:
            result = pc.CompileResult(
                plan={"output": {"filename": "x.mp4"}},
                math={"width": 320, "height": 180},
                commands=[pc.Command(
                    "render", "转场渲染",
                    ["ffmpeg", "-y", "-i", "TMP/a.mp4", "-i", "TMP/b.mp4",
                     "-filter_complex",
                     "[0:v][1:v]xfade=transition=fade:duration=0.8:offset=8.5[x1]",
                     "-map", "[x1]", "out.mp4"])])
            try:
                pc.precheck(result, tmp)
                raise AssertionError("预检应当失败")
            except pc.CompileError as exc:
                msg = "\n".join(exc.errors)
                assert "命令超时" in msg, f"超时原因未透出：{msg}"
                assert "non-strictly-monotonic" in msg, f"stderr 尾部丢失：{msg}"
        finally:
            pc.ffmpeg_exec.run = orig


if __name__ == "__main__":
    test_dry_argv_clamps_xfade_offsets()
    print("PASS test_dry_argv_clamps_xfade_offsets")
    test_dryrun_no_hang_with_real_ffmpeg()
    print("PASS test_dryrun_no_hang_with_real_ffmpeg")
    test_timeout_reason_surfaces_over_stderr()
    print("PASS test_timeout_reason_surfaces_over_stderr")
    print("ALL PASS")
