"""V12 P1 方言层离线单测：v2↔v3 双向转换 / round-trip 黄金用例 / 校验复用。

运行：.venv/Scripts/python.exe tests/test_v12_dialect.py
全部离线：不触网、不跑 ffmpeg。夹具只建 INPUT/MUSIC 假文件（校验只查存在性）。
"""

from __future__ import annotations

import copy
import os
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "video_editing"))

import plan_dialect as pd      # noqa: E402
import plan_schema             # noqa: E402


def _mk_project(tmp: str) -> str:
    os.makedirs(os.path.join(tmp, "INPUT"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "MUSIC"), exist_ok=True)
    for rel in ("INPUT/a.mp4", "INPUT/b.mp4", "INPUT/img.jpg", "MUSIC/m.mp3"):
        with open(os.path.join(tmp, rel), "wb") as f:
            f.write(b"x" * 64)
    return tmp


def _golden_v2() -> dict:
    """覆盖全部转换面的 v2 黄金计划（R11 raw 形态：宏未展开、无铺轴字幕）。"""
    return {
        "schema_version": "2.0",
        "output": {"filename": "OUTPUT/demo.mp4",
                   "resolution": {"width": 1280, "height": 720}, "fps": 30},
        "clips": [
            {"id": "c1", "source": "INPUT/a.mp4", "kind": "video",
             "trim_start": 4.2, "trim_end": 9.0,
             "probe": {"duration": 12.0, "width": 1920, "height": 1080,
                       "fps": 30, "has_audio": True, "has_video": True}},
            {"id": "c2", "source": "INPUT/b.mp4", "kind": "video",
             "trim_start": 0, "trim_end": 6.0,
             "effects": [{"name": "speed", "args": {"factor": 2}},
                         {"name": "eq", "args": {"brightness": 0.15,
                                                 "contrast": 1.1,
                                                 "saturation": 1.3}},
                         {"name": "hflip"}],
             "audio": {"mute": True}},
            {"id": "c3", "source": "INPUT/img.jpg", "kind": "image",
             "duration": 3},
        ],
        "timeline": [
            {"clip": "c1"},
            {"clip": "c2", "transition": {"type": "fade", "duration": 0.5}},
            {"clip": "c3"},
        ],
        "overlays": [
            {"type": "text", "at_clip": "c1", "start_offset": 0,
             "duration": 2, "position": "top", "text": "周末骑行",
             "font_size": 48, "color": "#FFFFFF"},
        ],
        "audio": {"source": "MUSIC/m.mp3", "volume": 0.3, "loop": True,
                  "original": "mute"},
        "_narration": [
            {"clip_ids": ["c1", "c2"],
             "text": "这周末我们去了郊外，拍到了不少好画面。",
             "units": ["这周末我们去了郊外。", "拍到了不少好画面。"]},
        ],
        "_title": "骑行 vlog",
    }


_T = []


def test(name):
    def deco(fn):
        _T.append((name, fn))
        return fn
    return deco


# ---------------------------------------------------------------- 升级 ---- #

@test("upgrade: output 拍平 + reader + start/end")
def _(tmp):
    v3 = pd.upgrade_v2_to_v3(_golden_v2())
    assert v3["schema_version"] == "3.0"
    out = v3["output"]
    assert out["width"] == 1280 and out["height"] == 720, out
    assert "resolution" not in out and out["filename"] == "OUTPUT/demo.mp4"
    c1 = v3["clips"][0]
    assert c1["reader"] == {"type": "FFmpegReader", "path": "INPUT/a.mp4"}
    assert c1["start"] == 4.2 and c1["end"] == 9.0
    assert "trim_start" not in c1 and "source" not in c1 and "kind" not in c1
    assert "probe" not in c1          # probe 不进 v3 协议


@test("upgrade: eq 三参数拆 Brightness+Saturation，speed 提升为 clip.speed")
def _(tmp):
    v3 = pd.upgrade_v2_to_v3(_golden_v2())
    c2 = v3["clips"][1]
    types = [e["type"] for e in c2["effects"]]
    assert types == ["Brightness", "Saturation", "hflip"], types
    assert c2["effects"][0]["contrast"] == 1.1
    assert c2["effects"][1]["saturation"] == 1.3
    assert c2["speed"] == 2                     # speed effect → 平级标量
    assert all("name" not in e and "args" not in e for e in c2["effects"])
    assert [e["id"] for e in c2["effects"]] == ["e1", "e2", "e3"]  # 全局 id
    assert c2["audio"] == {"mute": True}        # 无 volume 字段时 audio 原样


@test("upgrade: 仅 saturation 的 eq → Saturation；volume 提升为平级")
def _(tmp):
    v2 = _golden_v2()
    v2["clips"][0]["effects"] = [{"name": "eq", "args": {"saturation": 0.5}}]
    v2["clips"][0]["audio"] = {"volume": 0.8}
    v3 = pd.upgrade_v2_to_v3(v2)
    c1 = v3["clips"][0]
    assert [e["type"] for e in c1["effects"]] == ["Saturation"]
    assert c1["volume"] == 0.8 and "audio" not in c1


@test("upgrade: overlay position→gravity + id；_narration/_title 保留")
def _(tmp):
    v3 = pd.upgrade_v2_to_v3(_golden_v2())
    ov = v3["overlays"][0]
    assert ov["gravity"] == "top" and "position" not in ov and ov["id"] == "o1"
    assert v3["_narration"] == _golden_v2()["_narration"]
    assert v3["_title"] == "骑行 vlog"
    assert v3["audio"]["source"] == "MUSIC/m.mp3"   # BGM 块原样


@test("upgrade: 宏计划（workflow）原样升级，clips 不动")
def _(tmp):
    v2 = {"schema_version": "2.0",
          "output": {"filename": "OUTPUT/x.mp4",
                     "resolution": {"width": 720, "height": 1280}},
          "workflow": {"name": "one_click_reel", "budget_seconds": 30,
                       "bgm": True}}
    v3 = pd.upgrade_v2_to_v3(v2)
    assert v3["schema_version"] == "3.0"
    assert v3["output"]["width"] == 720 and v3["workflow"]["name"] == "one_click_reel"
    assert "clips" not in v3


@test("upgrade: 纯函数——入参不被修改")
def _(tmp):
    v2 = _golden_v2()
    snap = copy.deepcopy(v2)
    pd.upgrade_v2_to_v3(v2)
    assert v2 == snap


# ---------------------------------------------------------------- 降级 ---- #

@test("downgrade: v3→v2 与原始 v2 等价（eq 相邻合并还原；probe 按设计不保留）")
def _(tmp):
    v2 = _golden_v2()
    v3 = pd.upgrade_v2_to_v3(v2)
    back = pd.downgrade_v3_to_v2(v3)
    expect = copy.deepcopy(v2)
    for c in expect["clips"]:
        c.pop("probe", None)       # probe 是宿主侧数据，不进 v3 协议（宿主回填）
    assert back == expect, "round-trip 不精确：\n" + repr(back)


@test("downgrade: kind 按扩展名注入；图片保留 duration")
def _(tmp):
    v3 = pd.normalize({"schema_version": "3.0",
                       "output": {"filename": "OUTPUT/x.mp4", "width": 640,
                                  "height": 360},
                       "clips": [{"id": "c1",
                                  "reader": {"type": "ImageReader",
                                             "path": "INPUT/img.jpg"}},
                                 {"id": "c2",
                                  "reader": {"type": "FFmpegReader",
                                             "path": "INPUT/a.mp4"},
                                  "start": 1, "end": 4}],
                       "timeline": [{"clip": "c1"}, {"clip": "c2"}]})
    v2 = pd.to_internal(v3)
    assert v2["clips"][0]["kind"] == "image"
    assert v2["clips"][1]["kind"] == "video"


@test("downgrade: Brightness+Saturation 合并为一个 eq；speed 回到 effects")
def _(tmp):
    v3 = pd.normalize({"schema_version": "3.0",
                       "output": {"filename": "OUTPUT/x.mp4", "width": 640,
                                  "height": 360},
                       "clips": [{"id": "c1",
                                  "reader": {"type": "FFmpegReader",
                                             "path": "INPUT/a.mp4"},
                                  "start": 0, "end": 6, "speed": 0.5,
                                  "effects": [
                                      {"id": "e1", "type": "Brightness",
                                       "brightness": 0.2},
                                      {"id": "e2", "type": "Saturation",
                                       "saturation": 1.5}]}],
                       "timeline": [{"clip": "c1"}]})
    v2 = pd.to_internal(v3)
    effs = v2["clips"][0]["effects"]
    assert [e["name"] for e in effs] == ["speed", "eq"], effs
    assert effs[0]["args"] == {"factor": 0.5}
    assert effs[1]["args"] == {"brightness": 0.2, "saturation": 1.5}


@test("downgrade: clip.volume + clip.audio 合成 v2 audio 对象")
def _(tmp):
    v3 = pd.normalize({"schema_version": "3.0",
                       "output": {"filename": "OUTPUT/x.mp4", "width": 640,
                                  "height": 360},
                       "clips": [{"id": "c1",
                                  "reader": {"type": "FFmpegReader",
                                             "path": "INPUT/a.mp4"},
                                  "start": 0, "end": 6, "volume": 0.7,
                                  "audio": {"fade_in": 0.5}}],
                       "timeline": [{"clip": "c1"}]})
    v2 = pd.to_internal(v3)
    assert v2["clips"][0]["audio"] == {"fade_in": 0.5, "volume": 0.7}


@test("downgrade: 非 v3 输入原样返回")
def _(tmp):
    v2 = _golden_v2()
    assert pd.to_internal(v2) == v2


# ------------------------------------------------------------ 校验复用 ---- #

@test("validate_any: v3 黄金计划通过（降级后跑既有校验，零新规则）")
def _(tmp):
    v3 = pd.upgrade_v2_to_v3(_golden_v2())
    assert pd.validate_any(v3, tmp) == [], pd.validate_any(v3, tmp)


@test("validate_any: v3 错误信息用 v3 词汇（start/end/reader.path）")
def _(tmp):
    v3 = pd.normalize({"schema_version": "3.0",
                       "output": {"filename": "OUTPUT/x.mp4", "width": 640,
                                  "height": 360},
                       "clips": [{"id": "c1",
                                  "reader": {"type": "FFmpegReader",
                                             "path": "INPUT/不存在.mp4"},
                                  "start": 5, "end": 2}],
                       "timeline": [{"clip": "c1"}]})
    errs = pd.validate_any(v3, tmp)
    joined = "；".join(errs)
    assert any("INPUT/不存在.mp4" in e for e in errs), joined
    assert "trim_end" not in joined and "trim_start" not in joined, joined
    assert any("end 必须大于 start" in e for e in errs), joined


@test("validate_any: 未知 effect type 给 v3 白名单错误")
def _(tmp):
    v3 = pd.normalize({"schema_version": "3.0",
                       "output": {"filename": "OUTPUT/x.mp4", "width": 640,
                                  "height": 360},
                       "clips": [{"id": "c1",
                                  "reader": {"type": "FFmpegReader",
                                             "path": "INPUT/a.mp4"},
                                  "start": 0, "end": 4,
                                  "effects": [{"id": "e1",
                                               "type": "Mirror"}]}],
                       "timeline": [{"clip": "c1"}]})
    errs = pd.validate_any(v3, tmp)
    assert any("Mirror" in e for e in errs), errs
    assert any("Brightness" in "；".join(errs) for _ in [0]), errs  # 白名单含 v3 名


@test("validate_any: clip.speed 越界报错（经降级映射到 speed factor）")
def _(tmp):
    v3 = pd.normalize({"schema_version": "3.0",
                       "output": {"filename": "OUTPUT/x.mp4", "width": 640,
                                  "height": 360},
                       "clips": [{"id": "c1",
                                  "reader": {"type": "FFmpegReader",
                                             "path": "INPUT/a.mp4"},
                                  "start": 0, "end": 4, "speed": 9}],
                       "timeline": [{"clip": "c1"}]})
    errs = pd.validate_any(v3, tmp)
    assert any("0.25" in e for e in errs), errs


@test("check_material_fit_any: v3 计划走降级探针路径")
def _(tmp):
    v3 = pd.normalize({"schema_version": "3.0",
                       "output": {"filename": "OUTPUT/x.mp4", "width": 640,
                                  "height": 360},
                       "clips": [{"id": "c1",
                                  "reader": {"type": "FFmpegReader",
                                             "path": "INPUT/a.mp4"},
                                  "start": 0, "end": 999}],
                       "timeline": [{"clip": "c1"}]})

    def probe_fn(src):
        return {"ok": True, "duration": 12.0, "width": 1920, "height": 1080,
                "has_audio": True, "has_video": True}

    ms = pd.check_material_fit_any(v3, tmp, probe_fn)
    assert any(m.type == "over_range" for m in ms), ms
    assert all("trim_end" not in m.tag for m in ms), [m.tag for m in ms]


@test("normalize: v3 原样深拷贝；v2 升级；未知版本按 v2")
def _(tmp):
    v3 = pd.upgrade_v2_to_v3(_golden_v2())
    assert pd.normalize(v3) == v3
    assert pd.normalize(v3) is not v3
    no_ver = {k: v for k, v in _golden_v2().items() if k != "schema_version"}
    assert pd.normalize(no_ver)["schema_version"] == "3.0"


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="v12dialect-")
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
