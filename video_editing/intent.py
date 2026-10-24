"""V9.0 P3：意图分类车道。规则层零成本先行；LLM 层只救边缘（可关）。

设计契约见 docs/v9.0-understanding-layer.md §5。三条车道：
status_question → 不进规划 agent，直接从会话状态生成事实汇报；
ambiguous       → ask_user；task → 原路径（ops → diff → 闸门）。

规则顺序的判定依据（2026-10-11 三个事故的措辞实测）：
- 祈使标记优先于疑问形态：「帮我重新剪好吗」是请求不是提问；
- 疑问词与动词同现（「为什么把BGM删了？」）按疑问处理——
  它要的是解释，不是再次执行。
"""
from __future__ import annotations

import os
import re

_Q_END = ("？", "?", "吗", "嘛", "呢")
_Q_WORDS = ("做了吗", "做了没", "有没有", "是不是", "为什么", "怎么样", "怎样",
            "啥情况", "完成没", "好了没", "剪好", "搞定了", "多久", "多少",
            "几段", "在哪", "是什么", "啥意思", "可以吗", "行了吗", "啥样")
_IMPERATIVE = ("帮我", "请", "给我", "一下", "过来", "重新弄", "再来一版",
               "导出", "渲染一版")
# 无疑问形态也无祈使标记时的「明显任务动词」——直接按 task，跳过 LLM
_TASK_VERBS = ("删", "去掉", "加上", "添加", "换成", "调整", "剪", "拼接",
               "打码", "加字幕", "配乐", "导出", "渲染", "保留", "改成",
               "换背景", "提速", "倍速", "淡入", "淡出", "重剪")

# 「先给方案再动手」类消息：祈使形态（给我/出几个，classify 判 task）但
# 语义是咨询讨论。2026-10-15 事故：用户要替代方案，agent 直接开剪——
# ask_user 是合法出口而非结构保证，模型瞬时误判就会执行。这里检测命中后
# 由 server 给 agent 消息注入「本轮禁止执行」硬指令（D4：不靠模型诚实）。
_PROPOSAL_WORDS = ("替代方案", "几个方案", "备选", "别的思路", "不同的风格",
                   "不同风格", "几种剪法", "几种方案", "几个选择", "选项",
                   "给个方案", "出个方案", "先给方案", "方案选", "选一个再",
                   "对比一下", "对比一下方案")
_BRAKE_WORDS = ("先别", "别急", "先不剪", "先不要剪", "不要马上",
                "让我想想", "我想想", "先看看", "再决定", "先讨论", "先说思路")
# 已选定方案的回复形态：执行授权，不是再要一轮方案
_PICK_WORDS = ("就用", "选好", "定了", "用方案", "选方案", "按方案",
               "方案a", "方案b", "方案c", "方案d",
               "方案A", "方案B", "方案C", "方案D")


def is_proposal_request(msg: str) -> bool:
    """消息是否在「要方案/踩刹车」——命中则本轮只准 ask_user 出方案。

    「用方案C剪」这类已选定回复不拦（否则用户永远无法启动执行）。
    """
    m = _norm(msg)
    if not m:
        return False
    if any(w in m for w in _PICK_WORDS):
        return False
    return bool(any(w in m for w in _PROPOSAL_WORDS + _BRAKE_WORDS))


def _norm(msg: str) -> str:
    return (msg or "").strip().translate(
        str.maketrans("？（）！", "?()!"))


def classify(msg: str, has_session: bool = False) -> tuple[str, str]:
    """返回 (label, basis)。label ∈ task / status_question / ambiguous。

    basis ∈ rule-question / rule-imperative / rule-verb / rule-default /
    llm / llm-fallback，供观测与测试。
    """
    m = _norm(msg)
    if not m:
        return "task", "rule-default"
    question = m.endswith(_Q_END) or any(w in m for w in _Q_WORDS)
    imperative = any(w in m for w in _IMPERATIVE)
    verb_task = any(w in m for w in _TASK_VERBS)

    if question and imperative:
        return "task", "rule-imperative"      # 「帮我…好吗」是请求
    if question:
        # 「有没有别的方案？先别动手」要的是方案选项，不是现状汇报——
        # 放行为 task（basis=rule-proposal），server 注入点会加禁执行指令
        if is_proposal_request(m):
            return "task", "rule-proposal"
        return "status_question", "rule-question"
    if imperative or verb_task:
        return "task", "rule-verb"

    # 无签名：默认 task；有会话且开启 LLM 时对短消息做一次轻量分类
    if has_session and len(m) <= 40 and os.environ.get("INTENT_LLM", "1") != "0":
        return _llm_classify(m)
    return "task", "rule-default"


def _llm_classify(m: str) -> tuple[str, str]:
    """单次小调用：task / question / ambiguous 三选一。任何异常退回 task。"""
    try:
        from model import get_model
        prompt = (
            "判断下面这条消息发给视频剪辑 agent 时的意图类别，只输出一个小写单词：\n"
            "task（提出或修改剪辑需求）/ question（询问之前做了什么、结果如何、"
            "为什么）/ ambiguous（分不清是要求还是提问）。\n"
            f"消息：{m}\n只输出单词本身。"
        )
        out = str(get_model().invoke(prompt).content or "").strip().lower()
        label = re.findall(r"task|question|ambiguous", out)
        if label:
            return (label[0], "llm") if label[0] != "question" else ("status_question", "llm")
    except Exception:                          # noqa: BLE001 —— 离线/断网兜底
        pass
    return "task", "llm-fallback"


def is_confirm(msg: str) -> bool:
    """pending_plan 的放行判定：用户对「确认全局执行」类问题的肯定回答。"""
    m = _norm(msg)
    if not m or any(w in m for w in ("取消", "不算", "别", "不了", "重来")):
        return False
    return bool(re.search(r"确认|确定|执行|继续|全局|是的?|对|好的?|嗯|ok|yes|go", m.lower()))


def build_status_answer(session: dict) -> str:
    """从会话状态生成事实汇报（status_question 车道的直答文本）。"""
    turn = session.get("turn") or 0
    out = session.get("last_output")
    plan = session.get("last_plan") or {}
    clips = plan.get("clips") or []
    lines: list[str] = []
    if not clips:
        return "这个会话里还没有产出过计划。你可以直接把需求发给我。"
    srcs = sorted({str(c.get("source", "")).rsplit("/", 1)[-1] for c in clips})
    dur = 0.0
    for c in clips:
        try:
            ts = float(c.get("trim_start") or 0)
            te = float(c.get("trim_end") or 0)
            dur += (te - ts) if te > ts else 0
        except (TypeError, ValueError):
            pass
    lines.append(f"当前会话已进行 {turn} 轮；上一版成片是 {out}，"
                 f"共 {len(clips)} 个片段、约 {round(dur, 1)}s，"
                 f"用了 {len(srcs)} 个素材（{('、'.join(srcs[:3]))}{'…' if len(srcs) > 3 else ''}）。")
    audio = plan.get("audio") or {}
    if audio.get("source"):
        name = str(audio["source"]).rsplit("/", 1)[-1]
        vol = audio.get("volume")
        vol_part = f"，音量 {vol}" if vol is not None and vol != 1 else ""
        lines.append(f"BGM 用的 {name}{vol_part}。")
    exp = session.get("last_expansions") or {}
    bits = []
    if exp.get("select"):
        bits.append(f"筛选命中 {len(exp['select'].get('picked') or [])} 个镜头")
    if exp.get("cuts"):
        removed = round(sum(c.get("removed_seconds", 0) for c in exp["cuts"]), 1)
        bits.append(f"剪除废段 {removed}s")
    if exp.get("beats") and exp["beats"].get("snapped"):
        bits.append(f"节拍卡点 {exp['beats']['snapped']} 处")
    if exp.get("captions") and exp["captions"].get("title"):
        bits.append(f"标题「{exp['captions']['title']}」")
    if bits:
        lines.append("上一版用到的处理：" + "；".join(bits) + "。")
    hist = session.get("history") or []
    if len(hist) >= 2:
        lines.append(f"更早还产出过：{'、'.join(h.get('output', '?') for h in hist[:-1][-2:])}。")
    lines.append("需要我基于上一版继续修改，还是重新开始？")
    return "\n".join(lines)
