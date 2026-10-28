"""V9.0 理解层离线单测：会话落盘 / plan_diff + 作用域闸门 / edit_plan ops / 意图分类。

运行：.venv/Scripts/python.exe tests/test_v9_understanding.py
全部离线（INTENT_LLM=0 关闭意图 LLM 层），不触网、不跑 ffmpeg。
事故回归：t03/t04 即 2026-10-11 的「演唱会 BGM」与「重启失忆」两个事故。
"""

from __future__ import annotations

import json
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
def t15_intent_proposal_request():
    # 2026-10-15 事故回归：要方案的消息（祈使形态）不得直接开剪。
    # is_proposal_request 命中 → server 给 agent 消息注入「本轮禁止
    # submit_plan」host 指令（结构保证，不靠模型自觉 ask_user）。
    assert intent.is_proposal_request(
        "先别急着剪，给我几个不同风格的替代方案，我选一个再动手")
    assert intent.is_proposal_request("你重新给个替代方案看看")
    assert intent.is_proposal_request("先别动手，我想想")
    assert intent.is_proposal_request("把几个选项列一下")
    # 已选定方案的回复 = 执行授权，不拦（否则用户永远无法启动执行）
    assert not intent.is_proposal_request("用方案C剪，其他不变")
    assert not intent.is_proposal_request("就选方案2")
    assert not intent.is_proposal_request("帮我重新剪好吗")
    assert not intent.is_proposal_request("删掉最后一个片段")
    # 问句形态的方案请求放行为 task（rule-proposal），不再答非所问地汇报现状
    assert intent.classify("有没有别的方案？先别动手") == ("task", "rule-proposal")
    # 普通问句不受影响
    assert intent.classify("你重新剪了嘛")[0] == "status_question"


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
def t12_revision_brief_diff_mode():
    # V12：修订轮 brief 用 diff 工具词汇表（ACTIONS_DOC），draft 已由宿主
    # 载入上一版计划——不再有 edit_plan 一次性 ops 通道。
    import video_agent as va
    old = _mk_plan(3)
    msgs = va.build_revision_brief("剪个vlog", old, "OUTPUT/final.mp4",
                                   ["INPUT/concert.mp4"], "删掉C2")
    body = msgs[0]["content"]
    assert "clip_diff" in body and "validate_plan" in body and "submit()" in body, body[:300]
    assert "C1" in body and "concert.mp4" in body
    assert "完整的新计划" not in body            # 不要求重写整份计划


@t
def t16_retry_feedback_diff_mode():
    # V12：校验失败回传面向 diff 工具（update/delete op + submit()），
    # preamble 保留（修订语义不丢），不再有 ops/submit_plan 双轨岔路。
    import video_agent as va
    plan = _mk_plan(3)
    errs = ["overlays[2] 越界：start_offset(3.9) + duration(1.1) 超出片段 c2 的长度 4.9s。"]
    pre = [{"role": "user", "content": "修订brief…"}]
    msgs = va._retry_feedback("任务", plan, errs, preamble=pre)
    assert msgs[0] is pre[0]                      # preamble 保留（修订语义不丢）
    tail = msgs[-1]["content"]
    assert "diff 工具" in tail and "submit()" in tail, tail[:200]
    assert "从头重建" in tail                     # 明确禁止重写
    # plan is None（agent 没产出任何工具调用）：不炸 + 出口指引
    msgs3 = va._retry_feedback("任务", None, ["未能提取计划"])
    body3 = msgs3[-1]["content"]
    for kw in ("submit()", "submit_plan", "ask_user"):
        assert kw in body3, body3[:200]


@t
def t17_guard_overwrite_preview_suffix():
    # 2026-10-24 回归：base_output 存实际产物名（带 _preview）后，计划原名
    # 的同名覆盖检测失效——修订轮悄悄覆盖上一版成片（simtest4 实测）。
    import video_agent as va
    got = []
    plan = {"output": {"filename": "OUTPUT/slideshow.mp4"}}
    # preview 档：实际产物名匹配 → 改名
    va._guard_overwrite(plan, "OUTPUT/slideshow_preview.mp4", "-r2",
                        "preview", lambda ev: got.append(ev))
    assert plan["output"]["filename"] == "OUTPUT/slideshow-r2.mp4", plan
    assert got and "改名" in got[0]["text"]
    # 原名直接匹配（final 档）→ 改名
    p2 = {"output": {"filename": "OUTPUT/final.mp4"}}
    va._guard_overwrite(p2, "OUTPUT/final.mp4", "-r2", "final",
                        lambda ev: None)
    assert p2["output"]["filename"] == "OUTPUT/final-r2.mp4"
    # 不同名 → 不动
    p3 = {"output": {"filename": "OUTPUT/other.mp4"}}
    va._guard_overwrite(p3, "OUTPUT/final_preview.mp4", "-r2", "preview",
                        lambda ev: None)
    assert p3["output"]["filename"] == "OUTPUT/other.mp4"
    # 2026-10-28 盲区回归：输出名撞「更早历史产物」（非上一版）也要改名
    p4 = {"output": {"filename": "OUTPUT/grassland_travel.mp4"}}
    va._guard_overwrite(p4, "OUTPUT/grassland_travel-r2_preview.mp4", "-r3",
                        "preview", lambda ev: None,
                        history_outputs=["OUTPUT/grassland_travel.mp4"])
    assert p4["output"]["filename"] == "OUTPUT/grassland_travel-r3.mp4", p4


@t
def t18_answer_brief_anchored():
    # 2026-10-28 simtest10 事故回归：问答接续 brief 必须带会话基线锚定——
    # 无锚时用户答「重写计划」会让模型换素材从零创作。
    import video_agent as va
    q = {"type": "question", "question": "加标题在 ops 里表达不了，怎么处理？",
         "options": ["重写计划加标题", "跳过"]}
    # 无基线：兼容旧行为（只有问题+回答，无锚定段）
    msgs = va.build_answer_brief(q, "重写计划加标题")
    assert "当前会话基线" not in msgs[0]["content"]
    # 有基线：片段表 + 硬约束 + 原任务/成片名
    msgs2 = va.build_answer_brief(
        q, "选第二个：重写计划补上「未完待续」字幕",
        base_plan=_mk_plan(3), base_output="OUTPUT/f.mp4",
        materials=["INPUT/concert.mp4"], session_task="剪个vlog")
    body = msgs2[0]["content"]
    assert "当前会话基线" in body and "剪个vlog" in body
    assert "concert.mp4" in body and "OUTPUT/f.mp4" in body
    assert "不得更换素材集" in body and "不得重建整个作品" in body
    assert "底稿" in body                     # 重写以片段表为底稿


@t
def t13_edit_plan_retired():
    # V12：edit_plan 通道退役——工具不再注册、提取函数已删除；
    # 修订语义由 reset_draft(base_plan) + diff 工具承接（见 t12/t16）。
    import video_agent as va
    assert not hasattr(va, "extract_ops")
    assert not hasattr(va, "edit_plan")


@t
def t14b_diff_transitions_all_global():
    # 2026-10-28 sim-v12-r1 实测回归：所有边界转场都变 ⇒ global——
    # 「所有转场换成 X」只触碰 N-1 个片段（首片段无转场），旧口径
    # 永远 local，与 global 消息互相顶牛造成闸门死循环。
    import plan_diff as pdiff
    old = {"clips": [
        {"id": "C1", "source": "INPUT/a.mp4", "trim_start": 0, "trim_end": 8},
        {"id": "C2", "source": "INPUT/a.mp4", "trim_start": 40, "trim_end": 44},
        {"id": "C3", "source": "INPUT/b.mp4", "trim_start": 0, "trim_end": 5}],
        "timeline": [{"clip": "C1"},
                     {"clip": "C2", "transition": {"type": "fade", "duration": 0.5}},
                     {"clip": "C3", "transition": {"type": "fade", "duration": 0.5}}]}
    new = {"clips": old["clips"],
           "timeline": [{"clip": "C1"},
                        {"clip": "C2", "transition": {"type": "dissolve", "duration": 0.5}},
                        {"clip": "C3", "transition": {"type": "dissolve", "duration": 0.5}}]}
    d = pdiff.plan_diff(old, new)
    assert d["scope"] == "global", d
    assert any(c["kind"] == "transitions_all" for c in d["changes"]), d
    # 部分转场变化不升级 global（2 处只改 1 处）
    part = {"clips": old["clips"],
            "timeline": [{"clip": "C1"},
                         {"clip": "C2", "transition": {"type": "dissolve", "duration": 0.5}},
                         {"clip": "C3", "transition": {"type": "fade", "duration": 0.5}}]}
    assert pdiff.plan_diff(old, part)["scope"] == "local"


@t
def t14c_diff_no_substantive_change():
    # 无实质变化（仅 D4 自动改名的 output 差异）→ changes 只有 output，
    # server 侧据此跳过闸门（防「已是目标状态」的原样提交触发 global×local 死循环）
    import plan_diff as pdiff
    plan = {"clips": [{"id": "C1", "source": "INPUT/a.mp4",
                       "trim_start": 0, "trim_end": 8}],
            "timeline": [{"clip": "C1"}], "output": {"filename": "OUTPUT/a.mp4"}}
    renamed = json.loads(json.dumps(plan))
    renamed["output"]["filename"] = "OUTPUT/a-r2.mp4"
    d = pdiff.plan_diff(plan, renamed)
    kinds = [c["kind"] for c in d["changes"]]
    assert kinds == ["output"], d
    assert d["touched"] == []


@t
def t14_diff_reorder_renumbered_ids():
    """事故回归（2026-10-13）：手写整份新计划会重新编号，匹配对两侧 id 不同，
    旧实现拿新 id 去旧 id 列表 .index() 直接 ValueError 崩掉整轮修订；
    且 id 稳定时旧比较序列恒等，reorder 从来不会被报出来。"""
    old = {"clips": [
        {"id": "C1", "source": "INPUT/concert.mp4", "trim_start": 0, "trim_end": 8},
        {"id": "C2", "source": "INPUT/concert.mp4", "trim_start": 40.72, "trim_end": 44.12},
        {"id": "C3", "source": "INPUT/concert.mp4", "trim_start": 31.8, "trim_end": 33.52},
        {"id": "C4", "source": "INPUT/dance.mp4", "trim_start": 0, "trim_end": 5}],
        "timeline": [{"clip": c["id"]} for c in _mk_plan(4)["clips"]],
        "output": {"filename": "OUTPUT/final.mp4"}}
    # 新计划：重新编号（c1..c5）+ 31.8/40.72 两段次序对调 + 新增一张图片
    new = {"clips": [
        {"id": "c1", "source": "INPUT/concert.mp4", "trim_start": 0, "trim_end": 8},
        {"id": "c2", "source": "INPUT/concert.mp4", "trim_start": 31.8, "trim_end": 40.72},
        {"id": "c3", "source": "INPUT/concert.mp4", "trim_start": 40.72, "trim_end": 54.12},
        {"id": "c4", "source": "INPUT/dance.mp4", "trim_start": 0, "trim_end": 5},
        {"id": "c5", "source": "INPUT/poster.jpg", "kind": "image", "duration": 4}],
        "timeline": [{"clip": c["id"]} for c in _mk_plan(5)["clips"]],
        "output": {"filename": "OUTPUT/final.mp4"}}
    d = plan_diff.plan_diff(old, new)          # 旧实现在此 ValueError: 'c3' is not in list
    kinds = [c["kind"] for c in d["changes"]]
    assert "reorder" in kinds, d
    ro = next(c for c in d["changes"] if c["kind"] == "reorder")
    assert set(ro["clip"].split("+")) == {"c2", "c3"}, ro   # 对调的两段被点名
    assert "c1" not in ro["clip"] and "c4" not in ro["clip"]
    assert "add_clip" in kinds                                # 图片新增如实上报
    assert all("c5" == c.get("clip") or c["kind"] != "modify_clip"
               for c in d["changes"] if c["kind"] == "add_clip")

    # id 稳定（ops move_clip 路径）：同样要能报出 reorder
    base = _mk_plan(3)
    moved_plan, _, errs = plan_ops.apply_ops(
        base, [{"op": "move_clip", "clip": "C3", "after": "C1"}])
    assert not errs, errs
    d2 = plan_diff.plan_diff(base, moved_plan)
    assert any(c["kind"] == "reorder" for c in d2["changes"]), d2


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
