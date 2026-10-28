"""视频处理与剪辑 Agent（V1 + V2）。

V1（对照保留）：LLM 直接生成单条 ffmpeg 命令 + run_ffmpeg 工具执行。

V2（计划模式）：LLM 先探测素材，再通过 submit_plan 工具提交「编辑计划 JSON」，
由 plan_compiler 编译成命令序列执行。见 docs/v2.0-multi-material-editing.md。
"""

from __future__ import annotations

import json
import os

from deepagents import (
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends.local_shell import LocalShellBackend
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.graph.state import CompiledStateGraph

import content_analysis
import ffmpeg_exec
import plan_compiler
import plan_schema
import plan_tools
from model import get_model
from skills import AUDIO_PROMPT_DOC, render_prompt_doc
from creative_skills import render_creative_doc
from workflow import render_workflow_doc

# 仓库根：INPUT/ OUTPUT/ 在根下，LocalShellBackend 也锚定到根
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INPUT_DIR = os.path.join(PROJECT_ROOT, "INPUT")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "OUTPUT")

# 纯音频扩展（list_shots 跳过 / list_music 收录；与 server 的 AUDIO_EXT 同口径）
_AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".opus"}

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
ffmpeg commands. Your job is to build an editing plan with the PLAN TOOLS —
`new_plan` → `add_clips` (shot references) → `set_transition` / `set_audio` /
`set_narration` … → `validate_plan` → `submit()` — every tool call is checked
immediately and answers with the current draft state. Hand-writing the whole
plan JSON via `submit_plan` is a LEGACY path kept only for the `select` /
`workflow` macros (bulk tasks).

## Workflow

1. Use `ls` (or the built-in filesystem tools) to list files in INPUT/.
2. Use `probe_media` on each source you plan to use, to learn its real
   duration / resolution / whether it has audio.
3. If the request depends on WHAT IS IN the footage (finding / picking /
   filtering by content — e.g. "moments with friends", "only the parts with
   people", "the best three segments", "scenes with kids"), call
   `analyze_media` on each involved source and READ the content card it
   returns: shot-level time ranges with semantic labels.
   - NEVER invent trim ranges for a content-based selection without a
     content card. If `analyze_media` returns an error, or the card shows
     no matching shots, call `ask_user` with the real facts instead.
   - The content card is a read-only signal. Do not copy its fields into
     the plan — clips keep exactly the schema fields below.
   - Shot boundaries in the card come from deterministic scene detection,
     so they are safe to use as trim values.
   - **Bulk selection (V5)**: when picking MANY shots from one or more
     sources into a reel/highlight (e.g. "collect all dinner-party shots
     into ~30s"), do NOT hand-write dozens of trims. Instead submit a
     top-level `select` block (criteria + budget); the compiler reads the
     content cards deterministically and generates the clips. The host
     builds content cards automatically when materials are uploaded, so
     you normally just `ls INPUT/` and write the `select` block WITHOUT
     calling analyze_media first (pulling every full card wastes context).
     Only if the compiler reports a missing content index should you call
     `analyze_media` on that source and resubmit. `select` replaces
     `clips`/`timeline`/`overlays` (mutually exclusive).
4. **Removing dead segments (V5)**: for "cut out the silences / pauses /
   dead air / black screens", add `cut_silence` (or `cut_black`) to the
   clip instead of enumerating trims. The compiler detects the wasted
   intervals deterministically and expands the clip into kept segments.
   Works only on video clips whose material has an audio track (silence).
   BEFORE submitting, call `analyze_media` on the source and read
   `signals.audio`: if `silence_ratio` is ≈1.0 the whole track is silent —
   cutting silence is meaningless; do NOT submit, call `ask_user` and
   explain the real facts instead. (`silences: null` = no audio track at all.)
5. **Feasibility self-check**: compare the user's request against the probed
   facts (duration range / audio track / resolution / how many files exist).
   If every part of the request can be satisfied by the real materials,
   continue to step 6; otherwise go to step 7.
6. Build the plan with the plan tools and submit: `new_plan` → `add_clips`
   (ONE call for the whole sequence, shot references preferred) →
   `set_transition` / `set_audio` / `set_narration` / `add_text` →
   `validate_plan` → `submit`. Call `get_plan()` any time to see the current
   draft. For the `select` / `workflow` macros (bulk/parameter-only tasks),
   use the legacy `submit_plan` tool instead.
7. Only when the self-check fails — or the request is ambiguous / has no
   actionable operation / asks for a capability you don't have — call
   `ask_user` instead: state the real facts you probed and offer concrete
   options the user can pick from.

## Material content rule (hard)

ALL knowledge about what media files contain MUST come from `probe_media`,
`analyze_media`, `list_shots` and `list_music`. NEVER call `read_file` (or
any other filesystem tool) on files under INPUT/ or MUSIC/ — your planner
model is text-only, so image/video/audio bytes in a tool result are rejected
by the API with a hard 400 and the whole turn is lost. `ls` on directories
is fine (file names only). If an analyze_media card shows `tag_partial`,
`degradations` or missing labels, plan with the time boundaries, signals and
whatever labels exist — or call `ask_user` and say which source lacks
semantic labels. Do NOT try to look at the media yourself.

{CREATIVE_DOC}

## When to ask instead of submit

Call `ask_user` INSTEAD of `submit_plan` when ANY of these holds:
- The message contains no actionable editing operation (e.g. "haha", "test").
- The user asks for OPTIONS / ALTERNATIVES before any editing (e.g. "give me
  a few different styles to choose from", "don't cut yet — show me what you
  would do"). Present 2-4 concrete alternatives (structure, pacing, BGM
  direction, what differs from the previous version) via `ask_user` and wait.
  Do NOT submit() in the same turn: the user's pick in the NEXT message
  is what authorizes execution.
- The message asks ABOUT previous work or the conversation itself rather than
  requesting a NEW edit (e.g. "did you re-edit it?", "why did you keep that
  shot?", "how long is the result?"). It may mention editing words, but it
  wants information, not execution. Do NOT run it as a modification request:
  call `ask_user`, report the facts you were given (previous plan, output
  file, clip count, duration), and ask whether any change is wanted.
- The target material is ambiguous ("that video" while INPUT/ holds several).
- The request needs a capability that is not in the effect list — say so
  honestly and offer the closest available alternative.
- A content-based request where the content card shows no matching shots
  (e.g. "moments with friends" but the card says no multi-person shots) —
  report what the card actually contains and offer alternatives.
- The probed facts show the request exceeds what the material can do
  (e.g. "the first 50 seconds" but the clip is only 12.259s) — do NOT
  hard-clamp and do NOT fabricate probe numbers to make it fit; report the
  real duration and offer options (use everything / switch material / pick
  a shorter range).

`ask_user` and `submit()` are mutually exclusive — pick exactly one per turn.

## Probe field

Do NOT write the `probe` field into clips — the host re-probes every source
itself and fills `probe` in after you submit (hand-written values are simply
overwritten). Call `probe_media` to LEARN the facts (duration, resolution,
audio) for your own decisions. `trim_end` may be omitted when you want to
use the whole clip.

## Plan shape (reference — build it with the plan tools)

You normally do NOT hand-write this JSON: the plan tools build it for you
(`new_plan`/`add_clips`/… create exactly this shape). It is documented here
so you know what the draft looks like; the `select`/`workflow` macros and
the legacy `submit_plan` path still submit it directly.

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
      "shot": 3,
      "cut_silence": {"noise_db": -35, "min_silence": 0.4,
                      "keep_padding": 0.15, "min_keep": 0.3}
    },
    {
      "id": "c2",
      "source": "INPUT/b.mp4",
      "kind": "video",
      "trim_start": 12,
      "trim_end": 18.5
    },
    {
      "id": "c3",
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

- `"shot": N` = the N-th shot of that source as shown by `list_shots`
  (`INPUT/a.mp4#03`); the compiler copies the exact card shot boundaries into
  trim_start/trim_end. PREFER shot references over hand-copied timestamps —
  transcription errors are the #1 failure. Write explicit `trim_start` /
  `trim_end` only when splitting or merging shots. Never write `probe` (the
  host fills it); omit `trim_end` to use the whole clip.

## select macro (V5 bulk selection — replaces clips/timeline/overlays)

When the task is "pick matching shots from the materials into a reel", submit
this instead of `clips`/`timeline`/`overlays` (audio BGM block is still allowed):

{
  "schema_version": "2.0",
  "output": {"filename": "OUTPUT/friends.mp4",
             "resolution": {"width": 1280, "height": 720}, "fps": 30},
  "select": {
    "sources": ["INPUT/a.mp4", "INPUT/b.mp4"],
    "where": {
      "scene_any": ["聚会", "餐厅"],
      "activity_any": ["聚餐", "合影"],
      "person_count_min": 2,
      "has_children": false,
      "quality_in": ["good", "ok"],
      "min_duration": 1.5,
      "max_duration": 12,
      "max_silence_ratio": 0.6,
      "tag_any": ["举杯"]
    },
    "budget_seconds": 30,
    "order": "as_listed"
  }
}

- All `where` fields are optional; matching is AND across fields (OR within a
  list). Text fields match loosely (substring either way, incl. shot tags).
- `budget_seconds` omitted = take all matches; `order`:
  "as_listed" (source order then time, default) | "best_first" (quality then
  longer). Selected shots are joined with hard cuts.
- Every source MUST already have a content card (call `analyze_media` first);
  otherwise the compiler rejects the plan.

## Rules

- `clips` only lists materials that enter the timeline (main track).
  overlay/PiP sources are referenced directly in `overlays`.
- Every clip MUST have a video track. An audio-only file cannot be a clip —
  it goes in the top-level `audio` object instead (see below).
- `cut_silence` / `cut_black` (optional clip fields, V5): the compiler
  detects wasted intervals and expands the clip into kept segments (hard
  cuts in between; the clip's own transition lands on the first kept
  segment). Only for video clips; `cut_silence` requires the material to
  have an audio track. Do NOT hand-write trims for silence removal.
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

{WORKFLOW_DOC}

{AUDIO_DOC}

Submit the plan, then stop. Do not write any ffmpeg command.
"""

# 能力清单从 skills 注册表派生 —— 注册表加一条，提示词自动多一条，
# 不会出现"提示词与白名单脱节导致模型猜 effect 名"的旧坑。
V2_SYSTEM_PROMPT = (
    V2_SYSTEM_PROMPT_TEMPLATE
    .replace("{EFFECTS_DOC}", render_prompt_doc())
    .replace("{WORKFLOW_DOC}", render_workflow_doc())
    .replace("{CREATIVE_DOC}", render_creative_doc())
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
def analyze_media(path: str) -> str:
    """Analyze the CONTENT of a media file and return a content card (JSON).

    The card lists shots with deterministic time ranges and semantic labels
    per shot (person_count / has_children / scene / activity / mood / tags /
    quality). Results are cached in TMP/probe/, so calling twice is free.

    Call this BEFORE writing trims whenever the task depends on what appears
    in the footage (e.g. "moments with friends", "keep only segments with
    people", "pick the best parts"). Time ranges come from scene detection
    and are safe to use as trim_start/trim_end. Do NOT invent content-based
    trim ranges without this card.

    Args:
        path: Path relative to the project root, e.g. `INPUT/sample.mp4`.
    """
    # 感知层（V4）：时间由 ffmpeg 场景切分产生，语义由 VLM 产生；
    # 图片只在本工具内部的一次性请求里出现，永远不进 agent 消息流。
    card = content_analysis.analyze_media(path, PROJECT_ROOT)
    return json.dumps(card, ensure_ascii=False)


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
    """Ask the user a question — to clarify, or to answer a status question.

    Use this INSTEAD of submit_plan when: the request contains no actionable
    editing operation; the message asks about previous work / status instead
    of requesting an edit (then report the known facts in `question` —
    previous output, clip count, duration — and ask if any change is wanted);
    the target material is ambiguous; the request needs a
    capability that is not available; or probe_media shows the request exceeds
    what the material can actually do.

    Args:
        question: The clarifying question, in the user's language.
        options: Optional short candidate answers the user can pick from.
    """
    return "问题已提交给用户，等待答复。"


@tool
def edit_plan(ops: list) -> str:
    """Apply edit operations to the PREVIOUS plan (revision turns only).

    This is the ONLY way to modify an existing edit in a revision turn —
    never rewrite the whole plan. Each op is a small object; untouched
    fields are preserved verbatim by the host. If what the user wants
    cannot be expressed with the ops vocabulary, call `ask_user` instead
    of improvising.

    Args:
        ops: Ordered list of operation objects (vocabulary is documented
            in the revision brief you received).
    """
    return "操作已接收，宿主将对上一版计划应用这些操作并编译。"


@tool
def list_shots() -> str:
    """List EVERY shot across ALL indexed materials as your candidate pool
    (for creative/one-click tasks; V8.0 — no culling, no pre-filtering).

    One line per shot: shot_id, source, start-end, duration, scene/activity,
    person_count, quality, silence_ratio, desc. Shots flagged ⚠ (poor quality
    / mostly silent / very short) are shown TOO — judging them is YOUR job.
    Time ranges are the same card boundaries analyze_media returns, safe to
    use as trim_start/trim_end.

    Call analyze_media first for any material listed as missing, then call
    this again. No arguments — it always covers all of INPUT/.
    """
    lines, missing = [], []
    total_shots, total_seconds = 0, 0.0
    for name in sorted(os.listdir(INPUT_DIR)):
        rel = f"INPUT/{name}"
        full = os.path.join(INPUT_DIR, name)
        if not os.path.isfile(full) or name.startswith("."):
            continue
        if os.path.splitext(name)[1].lower() in _AUDIO_EXTS:
            continue                      # 纯音频走 list_music
        card = content_analysis.load_cached_card(PROJECT_ROOT, rel)
        if not card or card.get("error") or not card.get("shots"):
            missing.append(rel)
            continue
        ratio = ((card.get("signals") or {}).get("audio") or {}) \
            .get("silence_ratio")
        for i, sh in enumerate(card["shots"], 1):
            lab = sh.get("label") or {}
            s, e = sh.get("start") or 0.0, sh.get("end")
            dur = round((e - s), 3) if isinstance(e, (int, float)) else None
            flags = []
            if lab.get("quality") == "poor":
                flags.append("画质差")
            if isinstance(ratio, (int, float)) and ratio >= 0.8:
                flags.append(f"高静音{ratio:.0%}")
            if dur is not None and dur < 1.5:
                flags.append("过短")
            mark = " ⚠" + "/".join(flags) if flags else ""
            desc = str(lab.get("desc") or "").strip()
            desc = (desc[:40] + "…") if len(desc) > 40 else desc
            aes = lab.get("aes_score")
            aes_s = f"  美学{aes}" if isinstance(aes, (int, float)) else ""
            lines.append(
                f"{rel}#{i:02d}  {s:.3f}-{e if e is not None else '?'}s"
                f"  {dur}s  {lab.get('scene') or '?'}/{lab.get('activity') or '?'}"
                f"  {lab.get('person_count', 0)}人  {lab.get('quality') or '?'}"
                f"{aes_s}{mark}  {desc}")
            total_shots += 1
            total_seconds += dur or 0.0
    if not lines and not missing:
        return "INPUT/ 里没有任何视频素材。"
    srcs = {l.split("#", 1)[0] for l in lines}
    head = (f"共 {total_shots} 个镜头 / 合计 {round(total_seconds, 1)}s"
            f"（来自 {len(srcs)} 个素材）。"
            if lines else "没有已建卡的镜头。")
    body = "\n".join(lines)
    tail = ""
    if missing:
        tail = ("\n还没有内容索引（先对它们调用 analyze_media）："
                + "、".join(missing))
    return head + "\n" + body + tail


@tool
def list_music() -> str:
    """List BGM candidates for the `audio` block (built-in library first,
    then user-uploaded audio in INPUT/).

    One line per track: source path, title, genre, mood tags, duration.
    Pick ONE and write it into the plan's top-level audio block
    ({"source": "MUSIC/...", "volume": 1.0, "loop": true}); omit the audio
    block entirely when the user does not want music.
    """
    import workflow as wf_mod
    lines = []
    try:
        with open(os.path.join(PROJECT_ROOT, wf_mod.MUSIC_DIR, "meta.json"),
                  encoding="utf-8") as f:
            entries = json.load(f)
        for e in entries:
            tags = "/".join(str(t) for t in (e.get("tags") or [])[:5])
            lines.append(
                f"{e.get('file')}  {e.get('duration', '?')}s"
                f"  {e.get('title') or '?'}  {e.get('genre') or '?'}"
                + (f"  {tags}" if tags else ""))
    except (OSError, ValueError):
        pass                           # 曲库缺失不致命——INPUT/ 音频仍可列
    for name in sorted(os.listdir(INPUT_DIR)):
        rel = f"INPUT/{name}"
        if os.path.splitext(name)[1].lower() not in _AUDIO_EXTS:
            continue
        if not os.path.isfile(os.path.join(INPUT_DIR, name)):
            continue
        meta = ffmpeg_exec.probe(rel)
        dur = meta.get("duration")
        lines.append(f"{rel}  {round(dur, 1) if dur else '?'}s  (用户上传)")
    return "\n".join(lines) or "没有可用配乐候选（MUSIC/ 与 INPUT/ 均无音频）。"


# 宿主强制：媒体字节永不进 agent 消息流（v3.1 D1 铁律的兜底，不靠模型自觉）。
# 2026-10-26 事故：VLM 打标部分失败后，agent 用内置 read_file 直接读 INPUT 下的
# jpg，整图 base64 进入 ToolMessage，下一轮被纯文本主模型 400 拒绝、整轮作废。
# 领域信息一律走 probe/analyze/list_shots/list_music，文件系统工具（read/write/
# edit/glob/grep/execute）对本 agent 无正当用途，全部排除；`ls` 保留（提示词
# 工作流第 1 步在用，只返回文件名）。注册是合并语义（excluded_tools 取并集），
# 不覆盖框架默认；即便模型幻觉出被排除工具的调用，也会在执行边界被中间件以
# 文本拒绝，不会再把二进制塞进下一轮请求。
register_harness_profile(
    "openai",
    HarnessProfile(excluded_tools=frozenset({
        "read_file", "write_file", "edit_file", "glob", "grep", "execute",
    })),
)


def build_video_agent_v2() -> CompiledStateGraph:
    """V2+V4+V8+V10 agent：感知/曲库工具 + 计划构建工具集 + ask_user。

    V10 起首轮计划通过 plan_tools 的组合操作构建（new_plan → add_clips →
    … → submit），submit_plan 保留为兼容路径（宏/旧提示词）。
    """
    os.makedirs(INPUT_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    model = get_model()
    backend = LocalShellBackend(root_dir=PROJECT_ROOT)
    plan_tools.PROJECT_ROOT = PROJECT_ROOT
    return create_deep_agent(
        model=model,
        system_prompt=V2_SYSTEM_PROMPT,
        tools=[probe_media, analyze_media, list_shots, list_music,
               submit_plan, ask_user, edit_plan, *plan_tools.PLAN_TOOLS],
        backend=backend,
        name="video-editing-planner-v2",
    )


def _autofill_probe(plan: dict, project_root: str, probe_fn=None) -> int:
    """宿主用真实探针回填 clips[].probe（「引用代替誊抄」2026-10-28）。

    对应 OpenAI function-calling 指南「不要让模型填你已知的参数」：素材事实
    宿主本来就会 probe（服务端缓存/现场 ffprobe），模型手抄 probe 再靠
    probe_mismatch 交叉核验属于双重浪费，抄错/编造的空间也在这里。回填一律
    覆盖模型写的值——誊抄错误从结构上消失，probe 字段对模型变为可不写。
    只回填 video 片段（图片走 kind+duration，probe 对它无意义且本机 ffmpeg
    会把静图探成 0.04s 的怪值）。回填失败（素材缺失等）保持原样，交给
    下游「素材不存在」校验给出自救信息。返回回填个数。
    """
    filled = 0
    import plan_schema
    fn = probe_fn or plan_schema._default_probe_fn(project_root)
    for c in plan.get("clips") or []:
        if c.get("kind") != "video":
            continue
        src = c.get("source")
        if not isinstance(src, str):
            continue
        try:
            info = fn(src) or {}
        except Exception:                      # noqa: BLE001 —— 探测失败交给下游校验
            continue
        if not (isinstance(info, dict) and info.get("ok") and info.get("duration")):
            continue
        c["probe"] = {k: info[k] for k in
                      ("duration", "width", "height", "fps", "has_audio", "has_video")
                      if info.get(k) is not None}
        filled += 1
    return filled


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


def extract_ops(result: dict) -> list | None:
    """从 agent 结果里提取 edit_plan 调用的 ops 列表（无则 None）。"""
    for msg in result.get("messages", []):
        for tc in (getattr(msg, "tool_calls", None) or []):
            if tc.get("name") == "edit_plan":
                args = tc.get("args") or {}
                ops = args.get("ops")
                if isinstance(ops, list):
                    return ops
                return None
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
                    preamble: list | None = None, ops_mode: bool = False,
                    base_plan: dict | None = None,
                    tool_mode: bool = False) -> list:
    """构造「回传重出」的消息列表。

    刻意**不重放原始消息历史**，用纯文本重建「原任务 + 上次计划 + 错误 + 要求」。
    原因（都是实测踩出来的）：
      ① provider 对消息内容有格式限制，原样回放历史里的 AIMessage/ToolMessage
         可能被拒（SiliconFlow 报过 400 "Only text and image_url are supported"）；
      ② 把上一版计划留在上下文里，模型容易照抄自己的错误答案；
         显式用文字给出「上次的计划」，反而能精确表达「保留它、只改这几处」。

    preamble：首轮上下文的纯文本消息（修订 brief / 问答接续 brief）；
    提供时用它替换默认的「原任务」单条消息，保证重试不会丢掉修订语义。
    ops_mode：修订轮 ops 失败时，要求模型修正 ops 重新调用 edit_plan，
    而不是退回重写整份计划（V9 P2）。
    """
    if plan is None:
        if ops_mode:
            # 2026-10-24 实测：ops 表达不了的要求（如加标题/overlay 文字层）
            # 若只许修 ops，模型无路可走输出纯文本 → 提取失败死循环。给出
            # 与校验失败路径一致的三出口岔路。
            body = ("上一次没有产生可执行的操作或计划。如果用户的要求能用 "
                    "edit_plan ops 表达（片段增删/裁剪/顺序/转场/静音/BGM），"
                    "修正后重新调用 edit_plan；如果 ops 表达不了（如加标题、"
                    "overlay 文字层、效果参数），本轮改用 submit_plan 提交完整"
                    "的编辑计划；如果想向用户提问或汇报，调用 ask_user——"
                    "三种都算完成任务。\n\n"
                    "失败详情：\n- " + "\n- ".join(errors))
        else:
            body = ("上一次尝试没有产生可编译的计划。请重新完成这个任务：如果用户的消息"
                    "是剪辑/修改要求，调用 submit_plan 提交完整的编辑计划；"
                    "如果你其实想向用户提问或汇报情况（比如这条消息并不是修改要求，"
                    "而是在询问上一轮做了什么），就调用 ask_user——两种都算完成任务"
                    "（若上次是调用出错，忽略它重新尝试即可）。\n\n"
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
    if plan is not None and ops_mode:
        # 2026-10-24 熔断回归：修订轮校验失败（如 base_plan 带来的
        # overlay 存量越界）若仍机械要求 submit_plan，会与修订 brief 的
        # 「只允许 edit_plan」矛盾，模型两头不是输出纯文本耗尽重试。
        # 给出明确岔路：ops 能修的修 ops；ops 覆盖不到的字段本轮例外
        # 允许 submit_plan 提交修正后的完整计划。
        instruction = (
            "要求：只修正上述问题，其余部分保持不变。若问题出在 ops 能"
            "表达的范围（片段增删/裁剪/顺序/转场/静音/BGM），修正 ops 后"
            "重新调用 edit_plan；若问题在 ops 覆盖不到的字段（如 overlay "
            "文字层、效果参数），本轮改用 submit_plan 提交修正后的完整"
            "计划——这是修订轮唯一允许 submit_plan 的例外。"
        )
    if tool_mode and not ops_mode:
        # V10 工具化构建：反馈面向计划工具而非 submit_plan
        if plan is not None:
            instruction = (
                "要求：用对应的计划工具只修正上述问题（set_clip / "
                "set_transition / set_audio / set_narration / add_text …，"
                "get_plan() 可查看当前 draft），其余内容保持不变，修正后"
                "重新调用 submit()。不要 new_plan 从头重建。"
            )
        else:
            instruction = (
                "要求：用计划工具构建计划（new_plan → add_clips(shot 引用) → "
                "set_transition / set_audio / set_narration → validate_plan → "
                "submit），不要用 submit_plan 手写整份 JSON。"
            )
    if ops_mode and base_plan is not None:
        # 例外出口的底稿：修订 brief 只给片段表（压缩上下文），但 submit_plan
        # 需要完整计划——没有底稿模型不敢凭空重写（2026-10-24 实测：两轮
        # 纯文本解释「ops 表达不了」后短路熔断）。低频例外路径，正确性优先。
        body += ("\n\n上一版完整计划 JSON（submit_plan 的底稿——只修改与本次"
                 "需求相关的字段，其余逐字段保持原样）：\n"
                 + json.dumps(base_plan, ensure_ascii=False, indent=2))
    head = preamble if preamble is not None else [{"role": "user", "content": task}]
    return head + [{"role": "user", "content": body + "\n\n" + instruction}]


# ---------------------------------------------------- 会话上下文（纯文本） ---- #

def _plan_table_rows(base_plan: dict) -> list[str]:
    """计划的紧凑片段表（修订 brief / 问答接续 brief 共用的锚定渲染）。"""
    trans = {t.get("clip"): t.get("transition")
             for t in base_plan.get("timeline") or []}
    rows = []
    for c in base_plan.get("clips") or []:
        ts = c.get("trim_start") or 0
        te = c.get("trim_end")
        te_s = f"{te:g}" if isinstance(te, (int, float)) else "末尾"
        tr = trans.get(c.get("id"))
        tr_s = "硬切" if not tr else f"{tr.get('type')} {tr.get('duration')}s"
        eff = ",".join(e.get("name", "?") if isinstance(e, dict) else str(e)
                       for e in (c.get("effects") or [])) or "-"
        mute = " [已静音]" if (c.get("audio") or {}).get("mute") else ""
        rows.append(f"  {c.get('id')}  {str(c.get('source')).rsplit('/', 1)[-1]}"
                    f"  {ts:g}-{te_s}s  转场:{tr_s}  效果:{eff}{mute}")
    return rows


def build_revision_brief(task: str, base_plan: dict, base_output: str,
                         materials: list[str], new_task: str) -> list:
    """修订轮上下文（V9 ops 模式）：片段表 + ops 词汇表 + 本次要求。

    修订轮不再让模型重写整份计划 JSON——只允许 edit_plan(ops)。
    片段表给到 id/素材/裁剪/转场/效果，足够下 ops；完整 JSON 不再
    重放（压缩上下文，也杜绝「顺手改了没让改的字段」）。
    """
    import plan_ops
    listing = "、".join(materials) if materials else "（INPUT/ 为空）"
    rows = _plan_table_rows(base_plan)
    audio = base_plan.get("audio")
    audio_s = json.dumps(audio, ensure_ascii=False) if audio else "（无全局 BGM，原声直通）"
    body = (
        "用户在上一版成片的基础上提出了新的要求。\n\n"
        f"最初的需求：{task}\n"
        f"上一版已产出的成片：{base_output}\n"
        "上一版计划的片段表（id 可直接用于 ops）：\n" + "\n".join(rows)
        + f"\n顶层音频块（BGM）：{audio_s}\n"
        f"当前 INPUT/ 里实际可用的素材：{listing}\n\n"
        f"用户这次的修改要求：{new_task}\n\n"
        + plan_ops.OPS_DOC + "\n\n"
        "提交方式：调用 edit_plan(ops) 工具提交操作列表——这是修订轮唯一合法的"
        "执行出口；ops 表达不了就不要硬凑，改用 ask_user 说明。\n"
        "动手前先判断消息性质：如果它不是修改要求，而是在询问/确认上一轮的情况"
        "（「你重新剪了吗」「成片多长」），调用 ask_user 如实汇报，不要执行。\n"
        "如果用户提到了具体对象（某个片段/素材/场景），只对那些片段下 ops；"
        "不确定指代（比如「背景音乐」指全局 BGM 还是某视频自带的声音）就先 "
        "ask_user 确认，不要猜。"
    )
    return [{"role": "user", "content": body}]


def build_answer_brief(pending: dict, answer: str,
                       base_plan: dict | None = None,
                       base_output: str | None = None,
                       materials: list[str] | None = None,
                       session_task: str | None = None) -> list:
    """问答接续上下文：「你问过什么 + 用户怎么答」+ 会话基线锚定。

    2026-10-28 实测（simtest10 重启后接续）：brief 只有问题+回答时，用户
    答「重写计划补字幕」会让模型无锚可依——创作流把整个素材池当新任务，
    换素材重建 9 片段。修复：有 last_plan 时附片段表与硬约束（只做用户
    要求的改动、素材不得更换），「重写」以它为底稿而非从零创作。
    """
    body = (
        "你刚才向用户提出了一个问题：\n"
        f"{pending.get('question') or ''}\n"
        f"用户的回答是：{answer}\n\n"
    )
    if base_plan is not None:
        rows = _plan_table_rows(base_plan)
        audio = base_plan.get("audio")
        audio_s = json.dumps(audio, ensure_ascii=False) if audio \
            else "（无全局 BGM，原声直通）"
        listing = "、".join(materials) if materials else "（未知）"
        body += (
            "当前会话基线——用户的「修正/重写」一律以此为底稿，"
            "只做用户要求的那几处改动：\n"
            f"最初的需求：{session_task or '（未知）'}\n"
            f"当前版成片：{base_output or '（未知）'}\n"
            "当前计划的片段表：\n" + "\n".join(rows) + "\n"
            f"全局 BGM：{audio_s}\n"
            f"可用素材：{listing}\n\n"
            "硬约束：不得更换素材集（除非用户明确要求换素材）；"
            "不得重建整个作品；修不了的点如实说明。\n\n"
        )
    body += (
        "请据此继续：能用 edit_plan ops 表达就用 ops 修正后重新调用"
        " edit_plan；ops 表达不了（文字标题/overlay/效果参数）时，以上面的"
        "片段表为底稿重写完整计划并调用 submit_plan；"
        "如果用户的回答与问题无关，先判断它是新的剪辑要求还是仅仅在询问情况——"
        "是要求就按修订处理；只是询问（如「剪好了吗」）就调用 ask_user 如实"
        "汇报，不要提交新计划；"
        "如果用户是要你先说明做法或替代方案而不是马上执行"
        "（如「说说你的方案」「给我一个替代方案」「先别动手」），"
        "必须调用 ask_user 把方案、改动范围和副作用讲清楚，等用户确认后再提交——"
        "不要在同一次回复里既解释又直接剪辑。"
    )
    return [{"role": "user", "content": body}]


# ---- Preflight Tier 2 分流用的选项模板 ----

_MISMATCH_OPTIONS = {
    "over_range": ["用全部可用时长", "换别的素材", "改范围（说一个更短的）"],
    "empty_range": ["从头开始用全部", "换别的素材", "改范围"],
    "no_audio_track": ["换成有声音的素材", "去掉 ducking，只保留画面"],
    "cut_no_audio": ["去掉剪静音，直接用原素材", "换有声音的素材"],
    "bgm_too_short": ["自动循环这首 BGM", "换一首更长的", "接受音乐中途结束"],
    "pip_too_short": ["缩短画中画时长", "换一个素材"],
    "source_missing": ["换别的素材"],
    "mute_vs_clip_audio": ["去掉 original=mute（BGM 与原声/换声混音）",
                           "删掉逐段换声，成片只要 BGM"],
}


def _mismatch_options(mtype: str) -> list[str]:
    return _MISMATCH_OPTIONS.get(mtype, ["换一个素材", "调整要求"])


def _guard_overwrite(plan: dict, base_output: str | None,
                     output_suffix: str | None, quality: str, emit,
                     history_outputs: list[str] | None = None) -> None:
    """D4：修订/重建轮不覆盖本会话任何已产出成片（后端强制，不靠模型自觉）。

    口径归一（2026-10-24）：base_output 是上一版**实际产物名**（preview 档
    带 _preview 后缀），而计划里写的是原名——按本档实际产物名比较。
    历史盲区（2026-10-28 simtest9 实测）：只比「上一版」时，模型把输出名
    改回更早的产物名（如初版名）会静默覆盖旧成片——比对范围扩到会话
    history 里的全部产物名。
    """
    if not (base_output and output_suffix):
        return
    taken = {base_output}
    for h in history_outputs or []:
        if isinstance(h, str):
            taken.add(h)
            stem, ext = os.path.splitext(h)
            if ext:
                taken.add(f"{stem}_preview{ext}")   # 档位口径归一
    fn = (plan.get("output") or {}).get("filename")
    fn_real = plan_compiler.output_filename(
        plan, {"preview": quality == "preview"})
    if fn in taken or fn_real in taken:
        stem, ext = os.path.splitext(fn)
        plan["output"]["filename"] = f"{stem}{output_suffix}{ext}"
        emit({"type": "status",
              "text": f"为避免覆盖本会话已有成片，输出已自动改名为 "
                      f"{plan['output']['filename']}"})


def plan_with_retry(task: str, project_root: str, invoke, emit,
                    max_retries: int = MAX_PLAN_RETRIES, *,
                    messages: list | None = None,
                    base_output: str | None = None,
                    output_suffix: str | None = None,
                    probe_fn=None,
                    quality: str = "final",
                    base_plan: dict | None = None,
                    apply_ops_fn=None,
                    history_outputs: list[str] | None = None):
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
      base_plan      上一版计划；与 apply_ops_fn 同给时，修订轮走 edit_plan(ops) 路径
      apply_ops_fn   plan_ops.apply_ops；agent 调了 edit_plan 就由宿主确定性施加
    """
    first_messages: list = list(messages) if messages else [{"role": "user", "content": task}]
    messages = first_messages
    result: dict = {}
    last_errors: list[str] | None = None
    ops_mode = base_plan is not None and apply_ops_fn is not None
    # V10 首轮工具化构建：新 run 初始化 draft（修订轮 ops 模式不用）；
    # 重试不重置——模型只需修正出问题的部分再 submit
    tool_mode = not ops_mode
    if tool_mode:
        plan_tools.reset_draft()

    for attempt in range(max_retries + 1):
        emit({"type": "attempt", "n": attempt + 1, "max": max_retries + 1})

        # ① 调模型（异常也转入统一的失败处理）
        plan: dict | None = None
        errors: list[str] | None = None
        try:
            result = invoke(messages)
            ops = extract_ops(result) if ops_mode else None
            if ops is not None:
                # V9 P2：修订轮 edit_plan(ops) → 宿主确定性施加（不重写整份计划）
                plan, applied, op_errors = apply_ops_fn(base_plan, ops)
                if op_errors:
                    plan = None
                    errors = op_errors
                    emit({"type": "compile_error", "errors": errors})
                else:
                    summary = "；".join(a["detail"] for a in applied[:8])
                    emit({"type": "status", "text": f"已应用 {len(applied)} 个编辑操作：{summary}"})
            else:
                # V10：draft 工具构建优先（submit() 提交）；模型仍调
                # submit_plan 时走 legacy 一次性路径（宏/兼容）
                plan = plan_tools.take_submitted()
                if plan is None:
                    plan = extract_plan(result)
                if plan is not None:
                    # probe 自动回填（「引用代替誊抄」）：真实探针覆盖模型手抄值
                    n_probe = _autofill_probe(plan, project_root, probe_fn)
                    if n_probe:
                        emit({"type": "status",
                              "text": f"已用真实探针回填 {n_probe} 个片段的 probe 字段。"})
        except Exception as exc:
            plan = None
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
                _guard_overwrite(plan, base_output, output_suffix, quality,
                                 emit, history_outputs=history_outputs)
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
                        return plan_compiler.compile_plan(
                            plan, project_root, quality=quality), result
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
        messages = _retry_feedback(task, plan, errors, preamble=first_messages,
                                   ops_mode=ops_mode, base_plan=base_plan,
                                   tool_mode=tool_mode)

    return None, result

