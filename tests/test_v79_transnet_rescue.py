"""V7.9 TransNetV2 过碎复核离线单测（全 mock，不联网、不装 torch 也可跑）。

覆盖：
  1. 首轮切分过碎 + 复核可用 → 采纳模型边界，how=transnet，跳过升阈值；
  2. 复核不可用（None）→ 回退升阈值阶梯，行为与 V7.8 完全一致；
  3. 首轮粒度正常 → 复核函数根本不被调用（快路径零开销）；
  4. 模型比启发式还碎 → 忽略模型，继续升阈值；
  5. 模型判整段一镜（空边界）→ 采纳为单镜头；
  6. transnet.scenes_to_boundaries 的口径转换（字符串秒/滤 ~0/排序去重）；
  7. analyze_media 卡片记录 segmentation 字段；
  8. TRANSNET_RESCUE=0 → 复核不接线。
运行：python tests/test_v79_transnet_rescue.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import contextmanager

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "video_editing"))

import content_analysis as ca  # noqa: E402
import transnet  # noqa: E402


@contextmanager
def legacy_primary():
    """V7.9 旧形态（模型仅过碎复核）：TRANSNET_PRIMARY=0。"""
    old = os.environ.get("TRANSNET_PRIMARY")
    os.environ["TRANSNET_PRIMARY"] = "0"
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TRANSNET_PRIMARY", None)
        else:
            os.environ["TRANSNET_PRIMARY"] = old


# ---------------------------------------------------------------- fakes ---- #

def showinfo_stderr(times: list[float]) -> str:
    """构造 select+showinfo 的 stderr（时间戳序列）。"""
    return "".join(
        f"[Parsed_showinfo @ 0x...] n: {i} pts: {i} pts_time:{t}\n"
        for i, t in enumerate(times))


def fake_run_factory(rounds: list[str | None]):
    """假 ffmpeg：blackdetect（信号检测）回空 stderr；场景切分探测按轮次
    回 stderr（None=失败）；抽帧写假 jpg。

    rounds 与升阈值阶梯（0.3/0.45/0.6）逐轮对应；返回自身记录的调用数。"""
    calls = {"detect": 0, "extract": 0}

    def run(argv, timeout=None, cwd=None):
        if any("blackdetect" in a for a in argv):       # ⓪ 信号检测（先于切分）
            return {"ok": True, "returncode": 0, "stdout": "", "stderr": "",
                    "command": argv}
        if "-f" in argv and "null" in argv:            # ① 场景切分探测
            i = calls["detect"]
            calls["detect"] += 1
            err = rounds[i] if i < len(rounds) else rounds[-1]
            return {"ok": err is not None, "returncode": 0 if err else 1,
                    "stdout": "", "stderr": err or "boom", "command": argv}
        out = argv[-1]                                  # 抽帧
        calls["extract"] += 1
        with open(out, "wb") as f:
            f.write(b"\xff\xd8FAKEJPEG")
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": "",
                "command": argv}
    run.calls = calls
    return run


# 30s 素材、首轮 0.3 切成 ~80 段（平均粒度 0.35s → 判过碎；复刻频闪误切形态：
# 碎段密集铺满全片，平均粒度自然低于 OVERSEG_AVG）
FRAGMENTED = showinfo_stderr([0.0] + [round(0.35 * k, 2) for k in range(1, 80)])
# 升阈值后 0.45 只剩 2 个真切点（粒度 10s，达标）
RESCUED = showinfo_stderr([0.0, 10.0, 20.0])
# 粒度正常（8s/12s）
CLEAN = showinfo_stderr([0.0, 8.0, 20.0])
# 比启发式还碎的「模型」输出（144 边界 > 79，粒度 0.2s → 应被忽略）
FINER = [round(0.2 * k, 2) for k in range(1, 145)]


def probe_video(duration=30.0):
    return lambda full: {"ok": True, "kind": "video", "duration": duration,
                         "width": 1920, "height": 1080, "has_audio": False}


class FakeVLM:
    model_name = "fake/vlm"

    def invoke(self, messages):
        class R: content = "[]"
        return R()


def make_project(tmp: str, name="a.mp4") -> str:
    os.makedirs(os.path.join(tmp, "INPUT"), exist_ok=True)
    p = os.path.join(tmp, "INPUT", name)
    with open(p, "wb") as f:
        f.write(b"x" * 1024)
    return p


def rescue_spy(result):
    """复核函数假件：记录调用，返回既定结果（list 或 None）。"""
    spy = {"called": 0}

    def fn(run_binary, full, duration):
        spy["called"] += 1
        return result
    fn.spy = spy
    return fn


# ------------------------------------------------- _detect_boundaries ---- #

def t01_rescue_adopted():
    """首轮过碎 → 采纳模型边界，how=transnet，且不再升阈值。"""
    with legacy_primary():
        run = fake_run_factory([FRAGMENTED, RESCUED, RESCUED])
        spy = rescue_spy([0.0, 9.5, 19.5])          # 模型：3 段，平均粒度 10s
        b, how = ca._detect_boundaries(run, "a.mp4", 30.0, 24, transnet_fn=spy)
        assert how == "transnet", how
        assert b == [0.0, 9.5, 19.5], b
        assert spy.spy["called"] == 1, "复核应恰好调用一次"
        assert run.calls["detect"] == 1, "采纳模型后不应再跑升阈值探测"


def t02_rescue_unavailable_falls_back():
    """复核不可用（None）→ 升阈值阶梯原样生效。"""
    with legacy_primary():
        run = fake_run_factory([FRAGMENTED, RESCUED, RESCUED])
        spy = rescue_spy(None)
        b, how = ca._detect_boundaries(run, "a.mp4", 30.0, 24, transnet_fn=spy)
        assert how == "scene_rescued", how
        assert spy.spy["called"] == 1
        assert run.calls["detect"] == 2, "0.3 过碎后应升到 0.45 重试"


def t03_clean_path_never_rescues():
    """首轮粒度正常 → 复核零调用（快路径零开销）。"""
    with legacy_primary():
        run = fake_run_factory([CLEAN])
        spy = rescue_spy([0.0, 9.5])
        b, how = ca._detect_boundaries(run, "a.mp4", 30.0, 24, transnet_fn=spy)
        assert how == "scene", how
        assert spy.spy["called"] == 0, "干净切分不应触发复核"
        assert run.calls["detect"] == 1


def t04_model_finer_is_ignored():
    """模型比启发式还碎 → 忽略，继续升阈值。"""
    with legacy_primary():
        run = fake_run_factory([FRAGMENTED, RESCUED, RESCUED])
        spy = rescue_spy(FINER)
        b, how = ca._detect_boundaries(run, "a.mp4", 30.0, 24, transnet_fn=spy)
        assert how == "scene_rescued", how
        assert run.calls["detect"] == 2


def t05_model_single_shot_adopted():
    """模型判整段一镜（空边界）→ 采纳，等价单镜头。"""
    with legacy_primary():
        run = fake_run_factory([FRAGMENTED, RESCUED])
        spy = rescue_spy([])
        b, how = ca._detect_boundaries(run, "a.mp4", 30.0, 24, transnet_fn=spy)
        assert how == "transnet", how
        assert b == [], b
        shots = ca.build_shots(b, 30.0)
        assert shots[0]["start"] == 0.0, "空边界经 build_shots 补 0 成单镜头"


def t06_detect_failure_before_rescue():
    """首轮探测命令失败 → 直接均匀兜底，复核不参与。"""
    with legacy_primary():
        run = fake_run_factory([None])
        spy = rescue_spy([0.0, 9.5])
        b, how = ca._detect_boundaries(run, "a.mp4", 30.0, 24, transnet_fn=spy)
        assert how == "uniform", how
        assert spy.spy["called"] == 0


    # ------------------------------------------------------ transnet 纯函数 ---- #

    def t07_scenes_to_boundaries():
        scenes = [
            {"shot_id": 1, "start_time": "0.000", "end_time": "9.520"},
            {"shot_id": 2, "start_time": "9.520", "end_time": "19.480"},
            {"shot_id": 3, "start_time": "19.480", "end_time": "30.000"},
            {"shot_id": 4, "start_time": "0.004", "end_time": "0.100"},   # ~0 滤掉
        ]
        assert transnet.scenes_to_boundaries(scenes) == [9.52, 19.48]
        # 容错：坏元素跳过、乱序输入按序输出（首元素恒为 0 起点，被跳过）
        messy = [{"start_time": "0.000"}, {"start_time": "12.400"}, None,
                 {"start_time": "3.200"}, {"start_time": "x"}]
        assert transnet.scenes_to_boundaries(messy) == [3.2, 12.4]


def t08_enabled_switch():
    old = os.environ.get("TRANSNET_RESCUE")
    try:
        for v, want in [("1", True), ("0", False), ("false", False),
                        ("off", False), ("", True), (None, True)]:
            if v is None:
                os.environ.pop("TRANSNET_RESCUE", None)
            else:
                os.environ["TRANSNET_RESCUE"] = v
            assert transnet.enabled() is want, (v, want)
    finally:
        if old is None:
            os.environ.pop("TRANSNET_RESCUE", None)
        else:
            os.environ["TRANSNET_RESCUE"] = old


# ------------------------------------------------------- analyze_media ---- #

def t09_card_records_segmentation():
    with tempfile.TemporaryDirectory() as tmp:
        make_project(tmp)
        os.environ.pop("TRANSNET_RESCUE", None)
        events = []
        ca.set_progress_hook(lambda ev: events.append(ev))
        try:
            run = fake_run_factory([FRAGMENTED, RESCUED, RESCUED])
            run_binary = lambda argv, timeout=None: {"ok": False}    # noqa: E731
            # 直接注入复核结果，绕开真实模型（离线）
            card = ca.analyze_media(
                "INPUT/a.mp4", tmp, model=False, probe_fn=probe_video(30.0),
                run_fn=run, transnet_hook=lambda rb, f, d: [0.0, 9.5, 19.5],
                run_binary_fn=run_binary)
        finally:
            ca.set_progress_hook(None)
        assert "error" not in card, card.get("error")
        assert card["segmentation"] == "transnet", card.get("segmentation")
        assert any(ev.get("stage") == "transnet" for ev in events), events
        starts = [s["start"] for s in card["shots"]]
        assert starts == [0.0, 9.5, 19.5], starts


def t10_env_off_skips_rescue():
    with tempfile.TemporaryDirectory() as tmp:
        make_project(tmp)
        os.environ["TRANSNET_RESCUE"] = "0"
        try:
            run = fake_run_factory([FRAGMENTED, RESCUED, RESCUED])
            card = ca.analyze_media(
                "INPUT/a.mp4", tmp, model=False, probe_fn=probe_video(30.0),
                run_fn=run,
                transnet_hook=lambda rb, f, d: (_ for _ in ()).throw(
                    AssertionError("开关关闭时复核不应被调用")))
        finally:
            os.environ.pop("TRANSNET_RESCUE", None)
        assert "error" not in card, card.get("error")
        assert card["segmentation"] == "scene_rescued", card.get("segmentation")


def t11_cached_card_without_field_loads():
    """旧版卡片（无 segmentation 字段）读缓存不受影响。"""
    assert ca.load_cached_card  # 存在即用；字段缺失由 .get 消费者兜底

# ------------------------------------------------- V7.10 主切分（B 方案）---- #

def t12_primary_model_first_ffmpeg_not_run():
    """主切分默认开：模型先行，ffmpeg 启发式一次都不跑。"""
    run = fake_run_factory([FRAGMENTED, RESCUED, RESCUED])
    spy = rescue_spy([0.0, 9.5, 19.5])
    b, how = ca._detect_boundaries(run, "a.mp4", 30.0, 24, transnet_fn=spy)
    assert how == "transnet", how
    assert b == [0.0, 9.5, 19.5], b
    assert spy.spy["called"] == 1
    assert run.calls["detect"] == 0, "主切分采纳后不应再跑 ffmpeg 探测"


def t13_primary_none_falls_back_to_ladder_without_recall():
    """模型失败（None）→ 整体落 ffmpeg 阶梯，且阶梯内不再重复调用模型。"""
    run = fake_run_factory([FRAGMENTED, RESCUED, RESCUED])
    spy = rescue_spy(None)
    b, how = ca._detect_boundaries(run, "a.mp4", 30.0, 24, transnet_fn=spy)
    assert how == "scene_rescued", how
    assert spy.spy["called"] == 1, "模型刚失败，不应在过碎复核里再调一次"
    assert run.calls["detect"] == 2, "0.3 过碎后升 0.45 重试"


def t14_primary_off_restores_legacy():
    """TRANSNET_PRIMARY=0 → 回 V7.9 过碎复核形态（首轮正常则模型不参与）。"""
    old = os.environ.get("TRANSNET_PRIMARY")
    os.environ["TRANSNET_PRIMARY"] = "0"
    try:
        run = fake_run_factory([CLEAN])
        spy = rescue_spy([0.0, 9.5])
        b, how = ca._detect_boundaries(run, "a.mp4", 30.0, 24, transnet_fn=spy)
        assert how == "scene", how
        assert spy.spy["called"] == 0, "旧形态下干净切分不请模型"
        assert run.calls["detect"] == 1
    finally:
        if old is None:
            os.environ.pop("TRANSNET_PRIMARY", None)
        else:
            os.environ["TRANSNET_PRIMARY"] = old




TESTS = [v for k, v in sorted(globals().items()) if k.startswith("t") and
         k[1:3].isdigit() and callable(v)]


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
    print(f"\nV7.9 TransNet 复核离线测试：{len(TESTS) - failed}/{len(TESTS)} 项通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
