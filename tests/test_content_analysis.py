"""content_analysis 离线单测（V4 感知层）。

全 mock 不联网：假 ffmpeg（run_fn）/ 假 ffprobe（probe_fn）/ 假 VLM（model）。
运行：.venv/Scripts/python.exe tests/test_content_analysis.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "video_editing"))

import content_analysis as ca  # noqa: E402

SHOWINFO_STDERR = """
[Parsed_showinfo @ 0x...] n:   0 pts: 0       pts_time:0
[Parsed_showinfo @ 0x...] n: 210 pts: 210*256 pts_time:8.312
[Parsed_showinfo @ 0x...] n: 402 pts: 402*256 pts_time:15.24
"""


# ---------------------------------------------------------------- fakes ---- #

def fake_run_factory(showinfo: str | None = SHOWINFO_STDERR):
    """假 ffmpeg：detect 命令回 showinfo stderr；抽帧命令写真 jpg 文件。"""
    def run(argv, timeout=None, cwd=None):
        if "-f" in argv and "null" in argv:            # 场景切分探测
            return {"ok": showinfo is not None, "returncode": 0 if showinfo else 1,
                    "stdout": "", "stderr": showinfo or "boom", "command": argv}
        out = argv[-1]                                  # 抽帧：最后一个参数是输出
        with open(out, "wb") as f:
            f.write(b"\xff\xd8FAKEJPEG")
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": "", "command": argv}
    return run


class FakeVLM:
    """假 VLM：scripts 依次决定每次 invoke 的返回（["bad"] / [json文本] / None=抛异常）。"""

    def __init__(self, scripts, model_name="fake/vlm"):
        self.scripts = list(scripts)
        self.model_name = model_name
        self.calls: list[int] = []      # 每次请求带的帧数

    def invoke(self, messages):
        self.calls.append(len(messages[0]["content"]) - 1)
        step = self.scripts.pop(0) if self.scripts else "[]"
        if step is None:
            raise RuntimeError("network down")

        class R: content = step
        return R()


def make_project(tmp: str, name="a.mp4", size=1024) -> str:
    os.makedirs(os.path.join(tmp, "INPUT"), exist_ok=True)
    p = os.path.join(tmp, "INPUT", name)
    with open(p, "wb") as f:
        f.write(b"x" * size)
    return p


def probe_video(duration=20.0):
    return lambda full: {"ok": True, "kind": "video", "duration": duration,
                         "width": 1920, "height": 1080}


LABELS_3 = json.dumps([
    {"person_count": 3, "has_children": False, "scene": "餐厅", "activity": "聚餐",
     "mood": "欢笑", "tags": ["多人", "举杯"], "quality": "good"},
    {"person_count": 0, "has_children": False, "scene": "海边", "activity": "风景空镜",
     "mood": "安静", "tags": ["日落"], "quality": "ok"},
    {"person_count": 1, "has_children": True, "scene": "家中", "activity": "讲话",
     "mood": "温馨", "tags": ["儿童"], "quality": "poor"},
], ensure_ascii=False)

LABELS_1 = json.dumps([
    {"person_count": 2, "has_children": False, "scene": "餐厅", "activity": "合影",
     "mood": "欢笑", "tags": ["多人"], "quality": "good"},
], ensure_ascii=False)


# ---------------------------------------------------------------- tests ---- #

def t01_parse_showinfo():
    ts = ca.parse_showinfo(SHOWINFO_STDERR)
    assert ts == [0.0, 8.312, 15.24], ts


def t02_build_shots_basic():
    shots = ca.build_shots([0.0, 8.312, 15.24], 20.0)
    assert [s["start"] for s in shots] == [0.0, 8.312, 15.24]
    assert shots[-1]["end"] == 20.0


def t03_build_shots_long_split():
    # 60s 无切分点 → 按 12s 粒度等分为 5 个子镜头
    shots = ca.build_shots([0.0], 60.0)
    assert len(shots) == 5, len(shots)
    assert shots[0]["start"] == 0.0 and abs(shots[-1]["end"] - 60.0) < 0.01


def t04_build_shots_duration_missing():
    shots = ca.build_shots([0.0, 8.0], None)
    assert shots[-1]["end"] == 9.0          # 最后边界 +1s 兜底


def t05_downsample():
    items = list(range(10))
    out = ca.downsample(items, 4)
    assert out[0] == 0 and out[-1] == 9 and len(out) == 4
    assert ca.downsample(items, 20) == items
    assert ca.downsample(items, 1) == [0]


def t06_uniform_boundaries():
    b = ca.uniform_boundaries(60.0, 24)
    assert len(b) == 12                      # min(24, ceil(60/5))
    assert b[0] == 0.0


def t07_sanitize_label():
    lab = ca.sanitize_label({"person_count": "7", "has_children": "yes",
                             "scene": "x" * 50, "activity": None, "mood": "ok",
                             "tags": ["a", "", "b", "c", "d", "e", "f"],
                             "quality": "GREAT", "extra": "junk"})
    assert lab["person_count"] == 7
    assert lab["has_children"] is True
    assert len(lab["scene"]) == 24
    assert lab["activity"] == ""
    assert len(lab["tags"]) == 5             # 上限 5
    assert lab["quality"] == "ok"            # 非法值回落
    assert lab["usable"] is True
    assert "extra" not in lab
    assert ca.sanitize_label("not-a-dict")["person_count"] == 0


def t08_parse_tag_json():
    good = ca._parse_tag_json('[{"a":1}]')
    assert good == [{"a": 1}]
    assert ca._parse_tag_json("```json\n[{\"a\":1}]\n```") == [{"a": 1}]
    assert ca._parse_tag_json("前置说明 [{\"a\":1}] 后置说明") == [{"a": 1}]
    assert ca._parse_tag_json("完全不是JSON") is None
    assert ca._parse_tag_json('{"a":1}') is None        # 不是数组


def t09_tag_shots_retry():
    shots = [{"start": 0}, {"start": 5}, {"start": 10}]
    vlm = FakeVLM(["坏掉的输出", LABELS_3])             # 第一次坏，重试成功
    ca.tag_shots(shots, ["u0", "u1", "u2"], vlm, batch_size=6)
    assert all("label" in s for s in shots)
    assert shots[0]["label"]["scene"] == "餐厅"
    assert vlm.calls == [3, 3]


def t10_tag_shots_batch_failure_marks():
    shots = [{"start": 0}, {"start": 5}, {"start": 10}]
    vlm = FakeVLM([None, None])                        # 两次都异常
    ca.tag_shots(shots, ["u0", "u1", "u2"], vlm, batch_size=6)
    assert all(s.get("tag_failed") for s in shots)
    assert all("label" not in s for s in shots)


def t11_summary():
    shots = [{"label": {"scene": "餐厅", "activity": "聚餐", "person_count": 3,
                        "quality": "good"}},
             {"label": {"scene": "餐厅", "activity": "合影", "person_count": 4,
                        "quality": "ok"}},
             {"label": {"scene": "海边", "activity": "风景", "person_count": 0,
                        "quality": "poor"}}]
    s = ca.build_summary(shots, 30.0)
    assert "3 个镜头/30s" in s
    assert "餐厅×2" in s and "多人镜头 2 个" in s and "画质差 1 个" in s
    assert "未打语义标签" in ca.build_summary([{"start": 0}], 10)


def t12_analyze_full_flow_and_cache():
    with tempfile.TemporaryDirectory() as tmp:
        make_project(tmp)
        vlm = FakeVLM([LABELS_3])
        card = ca.analyze_media("INPUT/a.mp4", tmp, model=vlm,
                                run_fn=fake_run_factory(),
                                probe_fn=probe_video(20.0))
        assert card.get("cached") is False
        assert card["vlm_model"] == "fake/vlm"
        assert card["duration"] == 20.0
        assert len(card["shots"]) == 3                     # 3 个切分点
        assert card["shots"][0]["label"]["activity"] == "聚餐"
        assert card["shots"][2]["label"]["usable"] is False   # poor
        assert card["degradations"] == []
        assert "餐厅×2" in card["summary"] or "餐厅" in card["summary"]

        # 第二次：sidecar 命中，零计算（不再调 VLM）
        card2 = ca.analyze_media("INPUT/a.mp4", tmp, model=FakeVLM([]),
                                 run_fn=fake_run_factory(showinfo=None),
                                 probe_fn=probe_video(20.0))
        assert card2["cached"] is True
        assert card2["summary"] == card["summary"]

        # 改 mtime → 缓存失效重算
        os.utime(os.path.join(tmp, "INPUT", "a.mp4"), (2000000000, 2000000000))
        card3 = ca.analyze_media("INPUT/a.mp4", tmp, model=FakeVLM([LABELS_3]),
                                 run_fn=fake_run_factory(),
                                 probe_fn=probe_video(20.0))
        assert card3["cached"] is False


def t13_analyze_vlm_unavailable():
    with tempfile.TemporaryDirectory() as tmp:
        make_project(tmp)
        card = ca.analyze_media("INPUT/a.mp4", tmp, model=False,
                                run_fn=fake_run_factory(),
                                probe_fn=probe_video(20.0))
        assert "vlm_unavailable" in card["degradations"]
        assert len(card["shots"]) == 3
        assert all("label" not in s for s in card["shots"])
        assert "未打语义标签" in card["summary"]


def t14_analyze_uniform_fallback():
    with tempfile.TemporaryDirectory() as tmp:
        make_project(tmp)
        card = ca.analyze_media("INPUT/a.mp4", tmp, model=False,
                                run_fn=fake_run_factory(showinfo=None),
                                probe_fn=probe_video(20.0))
        assert "uniform_fallback" in card["degradations"]
        assert len(card["shots"]) == 4                    # 20s → 每 5s 一格


def t15_analyze_image_single_shot():
    with tempfile.TemporaryDirectory() as tmp:
        make_project(tmp, "pic.png")
        vlm = FakeVLM([LABELS_1])
        card = ca.analyze_media("INPUT/pic.png", tmp, model=vlm,
                                run_fn=fake_run_factory(showinfo=None),
                                probe_fn=lambda f: {"ok": True, "kind": "image",
                                                    "duration": None})
        assert vlm.calls == [1]                            # 单帧单请求
        assert card["shots"][0]["start"] == 0.0 and card["shots"][0]["end"] is None
        assert card["shots"][0]["label"]["scene"] == "餐厅"
        assert "uniform_fallback" not in card["degradations"]


def t16_analyze_error_paths():
    with tempfile.TemporaryDirectory() as tmp:
        make_project(tmp)
        miss = ca.analyze_media("INPUT/nope.mp4", tmp, model=None,
                                run_fn=fake_run_factory(), probe_fn=probe_video())
        assert "error" in miss
        audio = ca.analyze_media("INPUT/a.mp4", tmp, model=None,
                                 run_fn=fake_run_factory(),
                                 probe_fn=lambda f: {"ok": True, "kind": "audio"})
        assert "error" in audio
        outside = ca.analyze_media("OUTPUT/x.mp4", tmp, model=None,
                                   run_fn=fake_run_factory(), probe_fn=probe_video())
        assert "error" in outside


def t17_load_cached_card():
    with tempfile.TemporaryDirectory() as tmp:
        make_project(tmp)
        assert ca.load_cached_card(tmp, "INPUT/a.mp4") is None
        ca.analyze_media("INPUT/a.mp4", tmp, model=None, run_fn=fake_run_factory(),
                         probe_fn=probe_video(20.0))
        card = ca.load_cached_card(tmp, "INPUT/a.mp4")
        assert card is not None and card["cached"] is True
        assert ca.load_cached_card(tmp, "INPUT/别的.mp4") is None


def t18_progress_hook_events():
    events = []
    ca.set_progress_hook(lambda ev: events.append(ev))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            make_project(tmp)
            ca.analyze_media("INPUT/a.mp4", tmp, model=FakeVLM([LABELS_3]),
                             run_fn=fake_run_factory(), probe_fn=probe_video(20.0))
    finally:
        ca.set_progress_hook(None)
    stages = [e["stage"] for e in events]
    assert stages[0] == "start" and "sample" in stages and "tag" in stages
    assert stages[-1] == "done"
    done = events[-1]
    assert done["cached"] is False and done["shots"] == 3


def t19_get_vlm_model_no_key(monkey_env=None):
    # 直接验证 key 缺失 → None（不动真实 .env，用子进程级环境隔离太重，
    # 这里只测环境变量全空时的行为）
    import importlib
    import model as model_mod
    saved = {k: os.environ.pop(k, None) for k in
             ("VLM_API_KEY", "SILICONFLOW_API_KEY")}
    try:
        got = model_mod.get_vlm_model()
        assert got is None, got
        os.environ["VLM_API_KEY"] = "sk-test"
        os.environ["VLM_BASE_URL"] = "https://example.com/v1"
        os.environ["VLM_MODEL"] = "fake/model"
        m = model_mod.get_vlm_model()
        assert m is not None
        assert m.model_name == "fake/model"
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


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
    print(f"\ncontent_analysis 离线测试：{total - failed}/{total} 项通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
