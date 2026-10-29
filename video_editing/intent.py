"""V9.0 P3：意图分类车道。规则层零成本先行；LLM 层只救边缘（可关）。

设计契约见 docs/v9.0-understanding-layer.md §5。2026-10-29 改向后只剩两条
真分支（ambiguous 车道撤销，见 classify docstring）：
status_question → 不进规划 agent，直接从会话状态生成事实汇报；
task            → 放行原路径（agent 侧 brief/ask_user 兜底一切拿不准）。

规则顺序的判定依据（2026-10-11 三个事故的措辞实测）：
- 祈使标记优先于疑问形态：「帮我重新剪好吗」是请求不是提问；
- 疑问形态只有命中状态词表才直答——混入任务动词（「可以把X加进去吗」）
  的疑问是要求，一律放行（agent 的 brief 有「询问类先 ask_user」护栏）。
"""
from __future__ import annotations

import os
import re

from plan_schema import clip_duration

_Q_END = ("？", "?", "吗", "嘛", "呢")
_Q_WORDS = ("做了吗", "做了没", "有没有", "是不是", "为什么", "怎么样", "怎样",
            "啥情况", "完成没", "好了没", "剪好", "搞定了", "多久", "多少",
            "几段", "在哪", "是什么", "啥意思", "可以吗", "行了吗", "啥样")
_IMPERATIVE = ("帮我", "请", "给我", "一下", "过来", "重新弄", "再来一版",
               "导出", "渲染一版")
# 无疑问形态也无祈使标记时的「明显任务动词」——直接按 task，跳过 LLM。
# 2026-10-29 事故词表扩充：修改要求常以否定式/抱怨式出现（「不要字幕」
# 「第二个素材没被用上」「怎么没加进去」），全句无「剪」字也会被漏判——
# 分类不了就丢在门口。宁放行（agent ask_user 兜底）不误丢。
_TASK_VERBS = ("删", "删掉", "去掉", "不要", "加上", "添加", "加进", "加入",
               "换成", "调整", "剪", "拼接",
               "打码", "加字幕", "配乐", "导出", "渲染", "保留", "改成",
               "换背景", "提速", "倍速", "淡入", "淡出", "重剪",
               "没用上", "没被用", "没用", "漏了", "被使用", "没有用到", "用不上")

# 高置信状态问句词表：问的是「上一版做得怎么样/在哪/多长」。直答只认
# 「疑问形态 + 这里命中 + 去掉状态词后无任务动词」三条件同时成立。
# 复合短语（剪好了没…）要整体在表里——去词时才不会被「好了没」截断出
# 孤立的「剪」误判成任务动词（_pure_status 按长度降序去词）。
_STATUS_WORDS = ("多长", "几段", "多少", "多久", "几秒", "时长", "在哪",
                 "好了没", "好了吗", "完成没", "完成了吗", "做了吗", "做了没",
                 "做完了没", "做完了吗", "剪好了没", "剪好了吗", "剪好没",
                 "剪好了", "搞定了没", "搞定了吗",
                 "搞定", "剪好", "啥样", "怎么样", "怎样", "啥情况",
                 "什么情况", "进度", "上一版", "成片")

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
    """返回 (label, basis)。label ∈ task / status_question。

    ambiguous 车道已撤销（2026-10-29 一键成片后两次修改要求被它拦在门口
    静默丢弃）：两类错误代价不对称——闲聊误放进 agent 只多跑一轮（ask_user
    出口现成），修改要求误拦则彻底丢失。所以除高置信状态问句外一律放行。

    basis ∈ rule-question / rule-imperative / rule-verb / rule-proposal /
    rule-default / llm / llm-fallback，供观测与测试。
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
        # 直答边界收紧：状态词命中、且去掉状态词后不再有任务动词才算
        # 「明确状态问句」。「剪好了没」的「剪」属于「剪好」不算任务动词；
        # 「可以把第二个素材加进去吗」混入真实请求 → 放行
        if _pure_status(m):
            return "status_question", "rule-question"
        if verb_task:
            return "task", "rule-verb"
        # 纯疑问但拿不准（如「然后呢」）→ 放行，agent ask_user 如实回答
    if imperative or verb_task:
        return "task", "rule-verb"

    # 无签名：默认 task；有会话且开启 LLM 时对短消息做一次轻量分类
    if has_session and len(m) <= 40 and os.environ.get("INTENT_LLM", "1") != "0":
        return _llm_classify(m)
    return "task", "rule-default"


def _pure_status(m: str) -> bool:
    """疑问形态消息是否为高置信状态问句（status_question 直答的充分条件）。

    状态词表命中是必要条件；再把命中的状态词从消息里挖掉，剩余部分如果
    还带任务动词（「顺便把字幕去掉」），说明是混合消息，放行不直答。
    """
    if not any(w in m for w in _STATUS_WORDS):
        return False
    rest = m
    for w in sorted(_STATUS_WORDS, key=len, reverse=True):   # 长词优先，防重叠截断
        rest = rest.replace(w, "")
    return not any(v in rest for v in _TASK_VERBS)


def _llm_classify(m: str) -> tuple[str, str]:
    """单次小调用：只认 question（询问先前结果）直答，其余一律放行 task。

    2026-10-29 复现实测：同一句「我的第二个素材没有被使用到」连测 4 次在
    三分类间掷骰子（无固定温度 + 「明确要求 + 反问式投诉」句式天然模糊）。
    二分类收敛 + temperature=0 + 明确样例（抱怨素材没用上 = 修改需求）。
    任何异常退回 task（放行）。
    """
    try:
        from model import get_model
        prompt = (
            "判断下面这条发给视频剪辑 agent 的消息，只输出一个小写单词：\n"
            "question —— 询问之前做没做、结果如何、多长、在哪、为什么，"
            "只是要一个答复；\n"
            "task —— 其余一切：提出或修改剪辑需求。注意：抱怨某个素材或"
            "效果「没有被用上 / 漏了 / 不要了」、质问「怎么没把X加进去」，"
            "哪怕句式像提问，都是修改需求，属于 task。\n"
            f"消息：{m}\n只输出 question 或 task。"
        )
        model = get_model()
        try:
            model = model.bind(temperature=0)   # 显式路径（DEEPAGENT_MODEL）默认温度不为 0
        except Exception:                      # noqa: BLE001 —— 个别模型不支持 bind 时保持构造值
            pass
        out = str(model.invoke(prompt).content or "").strip().lower()
        label = re.findall(r"question|task", out)
        if label:
            return ("status_question", "llm") if label[0] == "question" \
                else ("task", "llm")
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
    import plan_dialect
    turn = session.get("turn") or 0
    out = session.get("last_output")
    # 降到内部形态消费（v3/v2 都认）：clip_duration 等读取的是 v2 字段
    plan = plan_dialect.to_internal(session.get("last_plan") or {})
    clips = plan.get("clips") or []
    lines: list[str] = []
    if not clips:
        return "这个会话里还没有产出过计划。你可以直接把需求发给我。"
    srcs = sorted({str(c.get("source", "")).rsplit("/", 1)[-1] for c in clips})
    # 时长口径统一走 plan_schema.clip_duration（image=duration / video=trim）：
    # 手工只累计 trim 会把图片片段算成 0（2026-10-28 实测 71.5s vs 成片 74.95s）
    dur = sum(d for d in (clip_duration(c) for c in clips) if d)
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
