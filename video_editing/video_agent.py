"""视频处理与剪辑 Agent（V1 + V2）。

V1（对照保留）：LLM 直接生成单条 ffmpeg 命令 + run_ffmpeg 工具执行。

V2（计划模式）：LLM 先探测素材，再通过 submit_plan 工具提交「编辑计划 JSON」，
由 plan_compiler 编译成命令序列执行。见 docs/v2.0-multi-material-editing.md。
"""

from __future__ import annotations

import json
import os

from deepagents import create_deep_agent
from deepagents.backends.local_shell import LocalShellBackend
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.graph.state import CompiledStateGraph

import ffmpeg_exec
import plan_compiler
import plan_schema
from model import get_model
from skills import AUDIO_PROMPT_DOC, render_prompt_doc

# 仓库根：INPUT/ OUTPUT/ 在根下，LocalShellBackend 也锚定到根
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INPUT_DIR = os.path.join(PROJECT_ROOT, "INPUT")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "OUTPUT")

# ---------------------------------------------------------------- LLM 打印 - #

def _print_llm_output(msg: AIMessage) -> None:
    """打印一次 LLM 调用的输出（文本内容 + 工具调用参数）。"""
    text = msg.content or ""
    if isinstance(text, list):  # content blocks 形式
        text = "\n".join(
            (b.get("text", "") if isinstance(b, dict) else str(b)) for b in text
        )
    text = text.strip()
    print("\n" + "=" * 70)
    print("[LLM 输出]")
    if text:
        print(text)
    for tc in msg.tool_calls or []:
        print(f"\n[工具调用] {tc.get('name')}")
        print(json.dumps(tc.get("args"), ensure_ascii=False, indent=2, default=str))


def _invoke_printing_llm(agent: CompiledStateGraph, messages: list) -> dict:
    """流式运行 agent，每产生一次 LLM 输出就打印；返回最终 state。

    用 stream_mode="values"：每个节点跑完都会吐出完整 state，
    只在最后一条消息是新生成的 AIMessage 时打印（按消息 id 去重，
    避免同一条历史消息被反复打印）。

    注意签名：接收的是 **messages 列表**（与 plan_with_retry 的 invoke 约定一致），
    不是任务字符串。2026-09-16 曾因传字符串把 content 包成了列表，
    导致 provider 400（"Only text and image_url are supported"）。
    """
    seen: set = set()
    final: dict = {}
    for state in agent.stream(
        {"messages": messages},
        stream_mode="values",
    ):
        final = state
        msgs = state.get("messages", [])
        if not msgs:
            continue
        last = msgs[-1]
        if not isinstance(last, AIMessage):
            continue
        key = getattr(last, "id", None) or repr(last.content)[:100]
        if key in seen:
            continue
        seen.add(key)
        _print_llm_output(last)
    return final


def make_cli_invoker(agent: CompiledStateGraph | None = None):
    """给 CLI 用的 invoke(messages) 可调用对象（实时打印每次 LLM 输出）。

    抽成工厂函数，是为了让 CLI 与 Web 共用同一个 invoke 约定：
    `invoke(messages: list) -> state`。传错成任务字符串会直接 400，
    所以入口收敛成这一个函数，别再各自拼消息。
    """
    ag = agent or build_video_agent_v2()
    return lambda messages: _invoke_printing_llm(ag, messages)


# ---------------------------------------------------------------- V1 ------- #

SYSTEM_PROMPT = """You are a video processing & editing assistant (V1).

The user describes what they want in natural language (transcode, remux,
resize, filter, concat, etc.), and you translate it into an exact ffmpeg
command line, then execute it with the `run_ffmpeg` tool.

## Working conventions

- Input media lives in the `INPUT/` directory of this project.
- Always write output files into the `OUTPUT/` directory with a new, clear
  filename (e.g. `OUTPUT/sample_1080p.mp4`). Never overwrite inputs.
- List files in `INPUT/` with `ls`/`glob` before planning, so you know the
  exact input filename.
- The machine may NOT have ffmpeg installed. If `run_ffmpeg` reports ffmpeg
  is missing, still give the user the exact command you would run, and explain
  how to install ffmpeg.

Be concise: state the command, run it, and summarize the result.
"""


@tool
def run_ffmpeg(command: str) -> str:
    """Execute an ffmpeg command line and return its output.

    Args:
        command: The full ffmpeg command line, e.g.
            `ffmpeg -i INPUT/sample.mp4 -c copy OUTPUT/sample.avi`
    """
    import shlex
    args = shlex.split(command)
    if not args:
        return "Error: empty command."
    if os.path.basename(args[0]) not in ("ffmpeg", "ffmpeg.exe"):
        return (
            f"Error: command must start with `ffmpeg`, got `{args[0]}`. "
            "Do not wrap with shell redirection or && chains."
        )
    res = ffmpeg_exec.run(args, cwd=PROJECT_ROOT)
    if not res["ok"] and res.get("error"):
        return f"{res['error']}\n原命令：{command}"
    out = (res.get("stdout") or "")[-4000:]
    err = (res.get("stderr") or "")[-4000:]
    return f"exit={res['returncode']}\n[stdout]\n{out}\n[stderr]\n{err}"


def build_video_agent() -> CompiledStateGraph:
    """V1 agent（对照保留）。"""
    os.makedirs(INPUT_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    model = get_model()
    backend = LocalShellBackend(root_dir=PROJECT_ROOT)
    return create_deep_agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[run_ffmpeg],
        backend=backend,
        name="video-processing-agent-v1",
    )


def run_video_task(task: str) -> dict:
    """Run one natural-language video task; return the full result state."""
    agent = build_video_agent()
    return _invoke_printing_llm(agent, task)


# ---------------------------------------------------------------- V2 ------- #

V2_SYSTEM_PROMPT_TEMPLATE = """You are a video editing planner (V2). You do NOT write
ffmpeg commands. Your job is to produce an editing plan in JSON and submit it
with the `submit_plan` tool.

## Workflow

1. Use `ls` (or the built-in filesystem tools) to list files in INPUT/.
2. Use `probe_media` on each source you plan to use, to learn its real
   duration / resolution / whether it has audio.
3. **Feasibility self-check**: compare the user's request against the probed
   facts (duration range / audio track / resolution / how many files exist).
   If every part of the request can be satisfied by the real materials,
   continue to step 4; otherwise go to step 5.
4. Call `submit_plan` with a valid plan object.
5. Only when the self-check fails — or the request is ambiguous / has no
   actionable operation / asks for a capability you don't have — call
   `ask_user` instead: state the real facts you probed and offer concrete
   options the user can pick from.

## When to ask instead of submit

Call `ask_user` INSTEAD of `submit_plan` when ANY of these holds:
- The message contains no actionable editing operation (e.g. "haha", "test").
- The target material is ambiguous ("that video" while INPUT/ holds several).
- The request needs a capability that is not in the effect list — say so
  honestly and offer the closest available alternative.
- The probed facts show the request exceeds what the material can do
  (e.g. "the first 50 seconds" but the clip is only 12.259s) — do NOT
  hard-clamp and do NOT fabricate probe numbers to make it fit; report the
  real duration and offer options (use everything / switch material / pick
  a shorter range).

`ask_user` and `submit_plan` are mutually exclusive — pick exactly one per turn.

## Probe truthfulness

The `probe` field you write into each clip must match what `probe_media`
actually returned for that file. The host cross-checks it against its own
re-probe of the material: fabricated probe data is rejected and sent back.

## Plan schema (submit exactly this shape)

{
  "schema_version": "2.0",
  "output": {
    "filename": "OUTPUT/final.mp4",
    "resolution": {"width": 1280, "height": 720},
    "fps": 30
  },
  "clips": [
    {
      "id": "c1",
      "source": "INPUT/a.mp4",
      "kind": "video",
      "trim_start": 0,
      "trim_end": 8,
      "probe": {"duration": 10.0, "width": 1920, "height": 1080,
                "fps": 30, "has_audio": true, "has_video": true}
    },
    {
      "id": "c2",
      "source": "INPUT/img.png",
      "kind": "image",
      "duration": 3
    }
  ],
  "timeline": [
    {"clip": "c1"},
    {"clip": "c2", "transition": {"type": "fade", "duration": 0.5}}
  ],
  "overlays": [
    {"type": "pip", "source": "INPUT/logo.png", "kind": "image",
     "at_clip": "c1", "start_offset": 1, "duration": 3,
     "position": "bottom-right", "scale": 0.25},
    {"type": "text", "text": "Hello", "at_clip": "c1",
     "start_offset": 0.5, "duration": 2,
     "font_size": 48, "color": "#FFFFFF", "position": "center"}
  ]
}

## Rules

- `clips` only lists materials that enter the timeline (main track).
  overlay/PiP sources are referenced directly in `overlays`.
- Every clip MUST have a video track. An audio-only file cannot be a clip —
  it goes in the top-level `audio` object instead (see below).
- Times are RELATIVE: `start_offset` counts from the START of the trimmed
  clip segment (0 = when that clip first appears on the timeline). You never
  compute absolute seconds — the compiler does that.
- `trim_start`/`trim_end` are positions in the ORIGINAL source. If ffprobe is
  unavailable and you cannot know the real duration, you MUST set an explicit
  `trim_end` for video clips (no null) so the duration is known.
- transition.type must be one of:
  fade, dissolve, wipeleft, wiperight, wipeup, wipedown,
  slideleft, slideright, slideup, slidedown, circleopen, circleclose
- A transition hangs on the timeline item of the SECOND clip (the one being
  transitioned INTO).
- overlays: position is one of top-left/top/top-right/left/center/right/
  bottom-left/bottom/bottom-right. pip.scale in (0,1].
- All source paths start with INPUT/; output filename starts with OUTPUT/.
- Per-clip filters go in that clip's `effects` array. The ONLY valid `name`
  values are exactly these (anything else is rejected by the compiler):

{EFFECTS_DOC}

{AUDIO_DOC}

Submit the plan, then stop. Do not write any ffmpeg command.
"""

# 能力清单从 skills 注册表派生 —— 注册表加一条，提示词自动多一条，
# 不会出现"提示词与白名单脱节导致模型猜 effect 名"的旧坑。
V2_SYSTEM_PROMPT = (
    V2_SYSTEM_PROMPT_TEMPLATE
    .replace("{EFFECTS_DOC}", render_prompt_doc())
    .replace("{AUDIO_DOC}", AUDIO_PROMPT_DOC)
)


@tool
def probe_media(path: str) -> str:
    """Probe a media file with ffprobe and return its metadata as JSON.

    Args:
        path: Path relative to the project root, e.g. `INPUT/sample.mp4`.
    """
    return json.dumps(ffmpeg_exec.probe(path), ensure_ascii=False)


@tool
def submit_plan(plan: dict) -> str:
    """Submit an editing plan (JSON object) for compilation and execution.

    Args:
        plan: The editing plan object following the V2 schema.
    """
    # 交由宿主提取；这里只回执。基础校验放在 plan_compiler。
    return "计划已接收，等待编译器校验与执行。"


@tool
def ask_user(question: str, options: list[str] | None = None) -> str:
    """Ask the user to clarify an ambiguous, unsupported, or infeasible request.

    Use this INSTEAD of submit_plan when: the request contains no actionable
    editing operation; the target material is ambiguous; the request needs a
    capability that is not available; or probe_media shows the request exceeds
    what the material can actually do.

    Args:
        question: The clarifying question, in the user's language.
        options: Optional short candidate answers the user can pick from.
    """
    return "问题已提交给用户，等待答复。"


def build_video_agent_v2() -> CompiledStateGraph:
    """V2 agent：probe_media + submit_plan + ask_user，输出编辑计划而非 ffmpeg 命令。"""
    os.makedirs(INPUT_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    model = get_model()
    backend = LocalShellBackend(root_dir=PROJECT_ROOT)
    return create_deep_agent(
        model=model,
        system_prompt=V2_SYSTEM_PROMPT,
        tools=[probe_media, submit_plan, ask_user],
        backend=backend,
        name="video-editing-planner-v2",
    )


def extract_plan(result: dict) -> dict | None:
    """从 agent 结果里提取 submit_plan 提交的计划对象。

    submit_plan 的参数名是 `plan`，所以工具调用的 args 形如 {"plan": {...}}；
    这里做解包（兼容直接传 plan 对象的情况）。
    """
    for msg in result.get("messages", []):
        tool_calls = getattr(msg, "tool_calls", None) or []
        for tc in tool_calls:
            if tc.get("name") == "submit_plan":
                args = tc.get("args") or {}
                if isinstance(args, dict) and "plan" in args and isinstance(args["plan"], dict):
                    return args["plan"]
                return args if isinstance(args, dict) else None
    return None


def extract_ask(result: dict) -> dict | None:
    """从 agent 结果里提取 ask_user 调用（question + options）。"""
    for msg in result.get("messages", []):
        for tc in (getattr(msg, "tool_calls", None) or []):
            if tc.get("name") == "ask_user":
                args = tc.get("args") or {}
                if isinstance(args, dict):
                    return {
                        "question": args.get("question") or "",
                        "options": args.get("options") or None,
                    }
    return None


def run_video_plan_task(task: str) -> dict:
    """运行 v2 计划任务，返回完整 result（含 submit_plan 工具调用）。

    单轮调用（不重试）；带重试的入口见 plan_with_retry。
    """
    agent = build_video_agent_v2()
    return _invoke_printing_llm(agent, [{"role": "user", "content": task}])


# ------------------------------------------------------- 计划重试闭环 ------- #

MAX_PLAN_RETRIES = 2


def _retry_feedback(task: str, plan: dict | None, errors: list[str],
                    preamble: list | None = None) -> list:
    """构造「回传重出」的消息列表。

    刻意**不重放原始消息历史**，用纯文本重建「原任务 + 上次计划 + 错误 + 要求」。
    原因（都是实测踩出来的）：
      ① provider 对消息内容有格式限制，原样回放历史里的 AIMessage/ToolMessage
         可能被拒（SiliconFlow 报过 400 "Only text and image_url are supported"）；
      ② 把上一版计划留在上下文里，模型容易照抄自己的错误答案；
         显式用文字给出「上次的计划」，反而能精确表达「保留它、只改这几处」。

    preamble：首轮上下文的纯文本消息（修订 brief / 问答接续 brief）；
    提供时用它替换默认的「原任务」单条消息，保证重试不会丢掉修订语义。
    """
    if plan is None:
        body = ("上一次尝试没有产生可编译的计划。请重新完成这个任务，并调用 submit_plan "
                "提交完整的编辑计划（若上次是调用出错，忽略它重新尝试即可）。\n\n"
                "失败详情：\n- " + "\n- ".join(errors))
    else:
        body = (
            "你上一次提交的计划（JSON）如下：\n"
            + json.dumps(plan, ensure_ascii=False, indent=2)
            + "\n\n它没有通过编译器校验，错误如下：\n- "
            + "\n- ".join(errors)
        )
    instruction = (
        "要求：只修正上述问题，其余部分保持不变；不要新增计划里没有声明的字段或效果。"
        "如果某个效果当前不支持，请如实说明做不到，不要臆造字段。修正后请重新调用 submit_plan。"
    )
    head = preamble if preamble is not None else [{"role": "user", "content": task}]
    return head + [{"role": "user", "content": body + "\n\n" + instruction}]


# ---------------------------------------------------- 会话上下文（纯文本） ---- #

def build_revision_brief(task: str, base_plan: dict, base_output: str,
                         materials: list[str], new_task: str) -> list:
    """修订轮上下文：最初需求 + 上一版计划/产物 + 当前素材 + 本次修改要求。"""
    listing = "、".join(materials) if materials else "（INPUT/ 为空）"
    body = (
        "用户在上一版成片的基础上提出了新的要求。\n\n"
        f"最初的需求：{task}\n"
        f"上一版已产出的成片：{base_output}\n"
        "上一版提交的计划（JSON）：\n"
        + json.dumps(base_plan, ensure_ascii=False, indent=2)
        + f"\n\n当前 INPUT/ 里实际可用的素材：{listing}\n\n"
        f"用户这次的修改要求：{new_task}\n\n"
        "要求：在上一版计划的基础上做最小必要修改，只改用户要求的部分，"
        "其余字段原样保留。输出完整的新计划（不是补丁），并重新调用 submit_plan。"
        "如果你不确定用户指的是哪一处，先调用 ask_user 确认，不要猜。"
    )
    return [{"role": "user", "content": body}]


def build_answer_brief(pending: dict, answer: str) -> list:
    """问答接续上下文：把「你问过什么 + 用户怎么答」交给模型。"""
    body = (
        "你刚才向用户提出了一个问题：\n"
        f"{pending.get('question') or ''}\n"
        f"用户的回答是：{answer}\n\n"
        "请据此继续：如果这是素材不匹配的确认（用户从选项里选了处理方式），"
        "按用户的选择修正计划并重新调用 submit_plan；"
        "如果用户的回答与问题无关，就把它当作一条新的要求来处理。"
    )
    return [{"role": "user", "content": body}]


# ---- Preflight Tier 2 分流用的选项模板 ----

_MISMATCH_OPTIONS = {
    "over_range": ["用全部可用时长", "换别的素材", "改范围（说一个更短的）"],
    "empty_range": ["从头开始用全部", "换别的素材", "改范围"],
    "no_audio_track": ["换成有声音的素材", "去掉 ducking，只保留画面"],
    "bgm_too_short": ["自动循环这首 BGM", "换一首更长的", "接受音乐中途结束"],
    "pip_too_short": ["缩短画中画时长", "换一个素材"],
    "source_missing": ["换别的素材"],
}


def _mismatch_options(mtype: str) -> list[str]:
    return _MISMATCH_OPTIONS.get(mtype, ["换一个素材", "调整要求"])


def plan_with_retry(task: str, project_root: str, invoke, emit,
                    max_retries: int = MAX_PLAN_RETRIES, *,
                    messages: list | None = None,
                    base_output: str | None = None,
                    output_suffix: str | None = None,
                    probe_fn=None):
    """出计划 → Preflight 匹配校验 → 编译；失败按「谁能修」三分流。

    invoke(messages) -> result_state   调用方决定「怎么跑 agent」（打印 / 流式上报）
    emit(event: dict)                  调用方决定「事件去哪」（终端 / NDJSON）

    三个出口（详见 docs/v3.1-multi-turn-interaction.md §5.3b）：
      ① agent 自己调了 ask_user            → emit question，立即返回（不重试）
      ② Tier 2 素材不匹配且用户可决        → emit question（事实+选项），立即返回（零重试）
      ③ 模型修得了的错（schema/编造 probe）→ 错误回传重出（≤ max_retries 次）

    新增可选参数（全部 keyword-only，CLI 旧调用不受影响）：
      messages       预构造的首轮上下文（修订 brief / 问答接续 brief）；None = task 单条
      base_output    上一版产物路径；output_suffix 提供时，新计划同名会自动改名（D4 防覆盖）
      probe_fn       服务端缓存的真实探针（src->probe dict）；None = 现场 ffprobe
    """
    first_messages: list = list(messages) if messages else [{"role": "user", "content": task}]
    messages = first_messages
    result: dict = {}
    last_errors: list[str] | None = None

    for attempt in range(max_retries + 1):
        emit({"type": "attempt", "n": attempt + 1, "max": max_retries + 1})

        # ① 调模型（异常也转入统一的失败处理）
        plan: dict | None = None
        try:
            result = invoke(messages)
            plan = extract_plan(result)
            errors: list[str] | None = None
        except Exception as exc:
            errors = [f"调用模型失败（{type(exc).__name__}）：{str(exc)[:300]}"]
            emit({"type": "error", "message": errors[0]})

        # ② 三分流：ask_user / 素材不匹配 / 编译
        if errors is None:
            ask = extract_ask(result)
            if ask is not None and plan is not None:
                emit({"type": "hallucination", "level": "warn",
                      "signal": "both_ask_and_plan",
                      "evidence": "同一轮同时调用了 ask_user 与 submit_plan，已按 ask_user 处理。"})
            if ask is not None:
                # 出口 ①：agent 自己反问（Tier 1 可行性自检的出口）
                emit({"type": "question", "kind": "ask_user",
                      "question": ask["question"], "options": ask["options"]})
                return None, result
            if plan is None:
                errors = ["你没有调用 submit_plan 工具。请把完整编辑计划放进 "
                          "submit_plan 的 plan 参数里提交。"]
                emit({"type": "error", "message": "未能从 agent 输出中提取 submit_plan 计划。"})
            else:
                # D4：修订轮不覆盖上一版产物（后端强制，不靠模型自觉）
                if base_output and output_suffix:
                    fn = (plan.get("output") or {}).get("filename")
                    if fn == base_output:
                        stem, ext = os.path.splitext(fn)
                        plan["output"]["filename"] = f"{stem}{output_suffix}{ext}"
                        emit({"type": "status",
                              "text": f"为避免覆盖上一版成片，输出已自动改名为 "
                                      f"{plan['output']['filename']}"})
                # Preflight Tier 2：素材现实 × 计划要求
                mismatches = plan_schema.check_material_fit(plan, project_root,
                                                            probe_fn=probe_fn)
                for w in (m for m in mismatches if m.level == "warn"):
                    emit({"type": "hallucination", "level": "warn",
                          "signal": w.type, "evidence": w.message})
                blocks = [m for m in mismatches
                          if m.level == "block" and m.user_decidable]
                if blocks:
                    # 出口 ②：用户可决的素材不匹配 → 直接问用户，零重试
                    emit({"type": "question", "kind": "material_mismatch",
                          "question": blocks[0].message,
                          "options": _mismatch_options(blocks[0].type),
                          "all": [b.message for b in blocks]})
                    return None, result
                errors = [m.message for m in mismatches
                          if m.level == "block" and not m.user_decidable]
                if errors:
                    # probe_mismatch 等：模型修得了，进重试
                    emit({"type": "compile_error", "errors": errors})
                else:
                    emit({"type": "plan", "plan": plan})
                    try:
                        return plan_compiler.compile_plan(plan, project_root), result
                    except plan_compiler.CompileError as exc:
                        errors = exc.errors
                        emit({"type": "compile_error", "errors": errors})

        # ③ 停止条件
        if last_errors is not None and set(errors) == set(last_errors):
            emit({
                "type": "error",
                "message": f"模型连续两次给出相同的 {len(errors)} 条错误，已停止重试"
                           f"（重试不会改变结果）：\n- " + "\n- ".join(errors),
            })
            return None, result

        if attempt >= max_retries:
            emit({
                "type": "error",
                "message": f"连续 {max_retries + 1} 次未能得到可执行计划，已放弃：\n- "
                           + "\n- ".join(errors),
            })
            return None, result

        # ④ 回传重试（preamble 保住修订/问答语义）
        last_errors = errors
        emit({
            "type": "status",
            "text": f"把 {len(errors)} 条错误回传给模型重出计划（第 {attempt + 2} 次）…",
        })
        messages = _retry_feedback(task, plan, errors, preamble=first_messages)

    return None, result

