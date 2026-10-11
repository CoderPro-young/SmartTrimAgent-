"""V9.0 理解层离线单测：会话落盘 / plan_diff + 作用域闸门 / edit_plan ops / 意图分类。

运行：.venv/Scripts/python.exe tests/test_v9_understanding.py
全部离线（INTENT_LLM=0 关闭意图 LLM 层），不触网、不跑 ffmpeg。
事故回归：t03/t04 即 2026-10-11 的「演唱会 BGM」与「重启失忆」两个事故。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

os.environ["INTENT_LLM"] = "0"          # 测试一律走规则层

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "video_editing"))

import intent            # noqa: E402
import plan_diff         # noqa: E402
import plan_ops          # noqa: E402

TESTS = []


def t(fn):
    TESTS.append(fn)
    return fn


# ------------------------------------------------------ 测试计划样例 ------- #
# 形态对齐真实产物（final-r2：8 片段 + BGM + fade 转场）
def _mk_plan(n=3, with_audio=True):
    clips, timeline = [], []
    for i in range(1, n + 1):
        src = ("INPUT/concert.mp4" if i <= n - 1 else "INPUT/dance.mp4")
        clips.append({"id": f"C{i}", "source": src,
                      "trim_start": i * 10.0, "trim_end": i * 10.0 + 5.0})
        timeline.append({"clip": f"C{i}",
                         "transition": None if i == 1 else {"type": "fade", "duration": 0.4}})
    plan = {"clips": clips, "timeline": timeline,
            "output": {"filename": "OUTPUT/final.mp4", "resolution": {"width": 1280, "height": 720}, "fps": 30}}
    if with_audio:
        plan["audio"] = {"source": "MUSIC/pop.mp3", "volume": 0.8}
    return plan


MATERIALS = ["concert.mp4", "dance.mp4"]
VOCAB = {"concert", "dance"}


# ------------------------------------------------------ P1：plan_diff ---- #
@t
def t01_diff_local_vs_global():
    old = _mk_plan(3)
    rm, _, errs = plan_ops.apply_ops(old, [{"op": "remove_clip", "clip": "C2"}])
    assert not errs, errs
    d = plan_diff.plan_diff(old, rm)
    assert d["scope"] == "local" and d["touched"] == ["C2"], d
    kinds = [c["kind"] for c in d["changes"]]
    assert "remove_clip" in kinds

    g, _, errs2 = plan_ops.apply_ops(old, [{"op": "set_audio", "audio": None}])
    assert not errs2
    d2 = plan_diff.plan_diff(old, g)
    assert d2["scope"] == "global", d2
    audio_ch = next(c for c in d2["changes"] if c["kind"] == "audio_block")
    assert "原声将恢复" in audio_ch.get("side_effect", ""), audio_ch


@t
def t02_diff_image_removal_always_reported():
    old = _mk_plan(3)
    old["clips"][2] = {"id": "C3", "source": "INPUT/photo.jpg",
                       "trim_start": 0, "trim_end": 3.0, "kind": "image"}
    rm, _, errs = plan_ops.apply_ops(old, [{"op": "remove_clip", "clip": "C3"}])
    assert not errs
    d = plan_diff.plan_diff(old, rm)
    assert any("photo.jpg" in c["detail"] for c in d["changes"]), d
    assert d["scope"] == "local"


@t
def t03_gate_concert_case():          # 事故回归：局部指代 × 全局 diff → 拦
    old = _mk_plan(3)
    g, _, _ = plan_ops.apply_ops(old, [{"op": "set_audio", "audio": None}])
    d = plan_diff.plan_diff(old, g)
    q = plan_diff.check_scope("去掉演唱会的背景音乐", d,
                              materials=MATERIALS, scene_vocab=VOCAB)
    assert q and q["kind"] == "scope_gate" and "确认" in q["question"], q
    assert q["options"][0] == "确认全局执行"

    b, _, _ = plan_ops.apply_ops(old, [{"op": "mute_clip", "clip": "C1", "on": True}])
    d2 = plan_diff.plan_diff(old, b)
    assert plan_diff.check_scope("去掉演唱会的背景音乐", d2,
                                 materials=MATERIALS, scene_vocab=VOCAB) is None


@t
def t04_gate_other_signatures():
    old = _mk_plan(3)
    g, _, _ = plan_ops.apply_ops(old, [{"op": "set_audio", "audio": None}])
    d = plan_diff.plan_diff(old, g)
    # 全局量词 × 全局 diff = 一致 → 放行
    assert plan_diff.check_scope("整条片不要配乐", d) is None
    # 无签名 → 放行（设计上接受的残留区）
    assert plan_diff.check_scope("随便处理下", d) is None
    # 全局量词 × 局部 diff → 拦（做少了）
    b, _, _ = plan_ops.apply_ops(old, [{"op": "mute_clip", "clip": "C1", "on": True}])
    dl = plan_diff.plan_diff(old, b)
    q = plan_diff.check_scope("所有片段都静音", dl)
    assert q and "只影响" in q["question"], q
    # 片段 ID 指代（不用场景词表）
    assert plan_diff.msg_scope("把C2删掉") == "local"
    assert plan_diff.msg_scope("把 C2 删掉") == "local"


# ------------------------------------------------------ P2：apply_ops ---- #
@t
def t05_ops_untouched_fields_verbatim():
    old = _mk_plan(3)
    old["clips"][0]["effects"] = [{"name": "mirror"}]
    old["clips"][0]["probe"] = {"duration": 87.2, "has_audio": True}
    new, applied, errs = plan_ops.apply_ops(old, [{"op": "remove_clip", "clip": "C2"}])
    assert not errs and len(applied) == 1, (errs, applied)
    ids = [c["id"] for c in new["clips"]]
    assert ids == ["C1", "C3"]
    # 未提及的字段逐字节保留（事故回归：图片/效果不会悄悄消失）
    assert new["clips"][0]["effects"] == [{"name": "mirror"}]
    assert new["clips"][0]["probe"] == {"duration": 87.2, "has_audio": True}
    assert new["audio"] == old["audio"]
    assert [t_["clip"] for t_ in new["timeline"]] == ["C1", "C3"]


@t
def t06_ops_validation_errors():
    old = _mk_plan(3)
    _, _, errs = plan_ops.apply_ops(old, [{"op": "remove_clip", "clip": "C99"}])
    assert errs and "不存在" in errs[0]
    _, _, errs2 = plan_ops.apply_ops(old, [{"op": "add_clip", "source": "INPUT/nope.mp4"}],
                                     materials=MATERIALS)
    assert errs2 and "不在 INPUT" in errs2[0]
    _, _, errs3 = plan_ops.apply_ops(old, [{"op": "set_transition", "clip": "C2",
                                            "type": "spin"}])
    assert errs3 and "白名单" in errs3[0]
    _, _, errs4 = plan_ops.apply_ops(old, "not-a-list")
    assert errs4


@t
def t07_ops_add_and_transition_and_reorder():
    old = _mk_plan(3)
    new, applied, errs = plan_ops.apply_ops(old, [
        {"op": "add_clip", "source": "INPUT/dance.mp4", "trim_start": 0,
         "trim_end": 4, "after": "C1"},
        {"op": "set_transition", "clip": "C2", "type": "dissolve", "duration": 0.5},
        {"op": "move_clip", "clip": "C3"},
        {"op": "set_audio", "audio": None},
    ], materials=MATERIALS)
    assert not errs, errs
    ids = [c["id"] for c in new["clips"]]
    assert ids == ["C3", "C1", "C4", "C2"], ids     # C3 移到最前；新片段 C4 在 C1 后；C2 未提及原地不动
    tr = next(t_ for t_ in new["timeline"] if t_["clip"] == "C2")["transition"]
    assert tr == {"type": "dissolve", "duration": 0.5}
    assert "audio" not in new and len(applied) == 4


# ------------------------------------------------------ P3：意图分类 ---- #
@t
def t08_intent_rules():
    assert intent.classify("你重新剪了嘛")[0] == "status_question"    # 事故回归
    assert intent.classify("剪好了没？")[0] == "status_question"
    assert intent.classify("为什么把BGM删了？")[0] == "status_question"
    assert intent.classify("帮我重新剪好吗")[0] == "task"            # 祈使优先
    assert intent.classify("删掉最后一个片段")[0] == "task"
    assert intent.classify("重新剪一下")[0] == "task"
    assert intent.classify("把字幕调成黄色")[0] == "task"
    # classify 不再产 ambiguous（LLM 关闭时保守放行为 task）
    assert intent.classify("背景音乐")[0] == "task"


@t
def t09_intent_confirm():
    assert intent.is_confirm("确认全局执行") is True
    assert intent.is_confirm("执行") is True
    assert intent.is_confirm("取消") is False
    assert intent.is_confirm("不，只改那部分") is False


@t
def t10_status_answer():
    s = {"turn": 2, "last_output": "OUTPUT/final.mp4", "last_plan": _mk_plan(3),
         "last_expansions": {"select": {"picked": [1, 2, 3]},
                             "cuts": [{"removed_seconds": 2.5}]},
         "history": [{"output": "OUTPUT/x.mp4"}]}
    text = intent.build_status_answer(s)
    assert "final.mp4" in text and "3 个片段" in text and "筛选命中 3" in text, text
    assert "继续修改" in text
    assert "还没有产出过计划" in intent.build_status_answer({"turn": 0})


# ------------------------------------------------------ P0：会话落盘 ---- #
@t
def t11_session_persistence():
    sys.path.insert(0, os.path.join(PROJECT_ROOT, "web"))
    import server
    tmp = tempfile.mkdtemp()
    try:
        server.SESSIONS_DIR = tmp
        server.SESSIONS.clear()
        sid = "test-sid-1"
        sess, restored = server._get_session(sid, "剪个vlog")
        assert restored is False                    # 全新会话
        sess["turn"] = 1
        sess["last_plan"] = _mk_plan(3)
        sess["last_output"] = "OUTPUT/final.mp4"
        sess["last_expansions"] = {"select": {"picked": [{"s": 1}]}}
        server._save_session(sid)
        # 模拟重启：清空内存，同 sid 再取 → 从盘恢复
        server.SESSIONS.clear()
        sess2, restored2 = server._get_session(sid, "继续")
        assert restored2 is True and sess2["_from_disk"] is True
        assert sess2["turn"] == 1
        assert sess2["last_plan"]["clips"][0]["id"] == "C1"
        assert sess2["last_expansions"]["select"]["picked"] == [{"s": 1}]
        # pending_plan 不落盘（进程内暂存）
        sess2["pending_plan"] = object()
        server._save_session(sid)
        server.SESSIONS.clear()
        sess3, _ = server._get_session(sid, "继续")
        assert "pending_plan" not in sess3
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------ 修订 brief ------- #
@t
def t12_revision_brief_ops_mode():
    import video_agent as va
    old = _mk_plan(3)
    msgs = va.build_revision_brief("剪个vlog", old, "OUTPUT/final.mp4",
                                   ["INPUT/concert.mp4"], "删掉C2")
    body = msgs[0]["content"]
    assert "edit_plan" in body and "remove_clip" in body, body[:200]
    assert "C1" in body and "concert.mp4" in body
    assert "完整的新计划" not in body            # 不再要求重写整份计划


@t
def t13_extract_ops():
    import video_agent as va

    class FakeMsg:
        def __init__(self, calls):
            self.tool_calls = calls

    result = {"messages": [FakeMsg([{"name": "edit_plan",
                                     "args": {"ops": [{"op": "remove_clip", "clip": "C2"}]}}])]}
    assert va.extract_ops(result) == [{"op": "remove_clip", "clip": "C2"}]
    assert va.extract_ops({"messages": [FakeMsg([])]}) is None


def main():
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\nV9.0 理解层离线测试：{len(TESTS) - failed}/{len(TESTS)} 项通过")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
