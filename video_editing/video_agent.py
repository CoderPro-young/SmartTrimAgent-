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
import plan_dialect
import plan_schema
import plan_tools
from model import get_model
from skills import (AUDIO_PROMPT_DOC, render_prompt_doc_v3)
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

V2_SYSTEM_PROMPT_TEMPLATE = """You are a video editing planner (V12). You do NOT write
ffmpeg commands and you do NOT hand-write plan JSON. Your job is to build an
editing plan by calling the PLAN TOOLS — every call is checked immediately and
answers with the current draft state. The draft is a v3 editing-plan JSON
(openshot-style field names); you never see or write it whole.

## Tools (the only write path)

  set_project(filename, width, height, fps)   ← start here; also updates spec
  clip_diff(ops)        add / modify / remove CLIPS   (batch, atomic)
  effect_diff(ops)      attach / modify / remove effects per clip
  overlay_diff(ops)     text / picture-in-picture overlays (addressed by id)
  timeline_diff(ops)    reorder clips (move) and transitions
  audio_diff(ops)       set / clear the background music
  set_narration(groups, title) / set_captions(lines, style)
  load_plan(plan)       full replace (only when the host hands you a plan)
  get_plan()            inspect the draft — call whenever unsure
  validate_plan()       full trial validation
  submit()              commit for compilation & rendering

Each op is a small flat dict, e.g. clip_diff:
  [{"op":"insert","reader":{"type":"FFmpegReader","path":"INPUT/a.mp4"},"shot":3},
   {"op":"insert","reader":{"type":"FFmpegReader","path":"INPUT/b.mp4"},"shot":1}]
or a revision: [{"op":"update","id":"c2","end":9.0}].
A batch is ATOMIC: one bad op rejects the whole call — fix and resend; already
applied batches must NOT be resent. Every tool answer ends with the full draft
summary, so you always know the current state.

## Workflow

1. Use `ls` to list files in INPUT/.
2. Use `probe_media` on each source you plan to use, to learn its real
   duration / resolution / whether it has audio.
3. If the request depends on WHAT IS IN the footage (finding / picking /
   filtering by content — e.g. "moments with friends", "only the parts with
   people", "the best three segments"), call `analyze_media` and READ the
   content card, or just read `list_shots` output (one line per shot).
   - NEVER invent start/end ranges for a content-based selection without a
     content card. If no matching shots exist, call `ask_user` with the real
     facts instead.
   - **Prefer shot references**: insert clips with "shot": N — the host copies
     the exact card boundaries. Transcription of timestamps is the #1 failure;
     write explicit "start"/"end" only when splitting or merging shots.
   - **Bulk selection (V5)**: when picking MANY shots into a reel (~30s of
     "all dinner-party shots"), do NOT enumerate clips. Submit a top-level
     `select` block via the legacy `submit_plan` tool instead; the compiler
     reads the content cards deterministically. `select`/`workflow` macros are
     the ONLY plans you hand-write.
4. **Removing dead segments (V5)**: for "cut out the silences / black
   screens", put `cut_silence` (or `cut_black`) on the clip in clip_diff
   instead of enumerating ranges. BEFORE submitting, read `signals.audio` of
   the source (analyze_media): if `silence_ratio` ≈ 1.0 the whole track is
   silent — do NOT submit; call `ask_user` with the real facts.
5. **Feasibility self-check**: compare the request against the probed facts
   (duration range / audio track / resolution / how many files exist). If
   every part can be satisfied, continue; otherwise `ask_user` (see below).
6. Build the plan with the tools and submit:
   set_project → clip_diff (ONE call for the whole sequence) →
   timeline_diff (transitions) / effect_diff / overlay_diff / audio_diff /
   set_narration → validate_plan → submit. `get_plan()` any time.
7. REVISION turns (the host preloaded the previous plan as your draft): do NOT
   rebuild from scratch and do NOT call set_project again unless the output
   spec changes. Touch ONLY what the user asked for with update/delete ops;
   get_plan() first if the brief does not include the full clip table.
   "start": null clears a field (e.g. drop a custom cut to re-reference a shot).

## Composite operations (recipe — one batch each)

- Split clip c2 at 35s: ONE clip_diff call:
  [{"op":"update","id":"c2","end":35},
   {"op":"insert","reader":<same reader as c2>,"start":35,"end":<c2 end>,"after":"c2"}]
- Trim the left edge: [{"op":"update","id":"c2","start":32}]
- Re-reference a shot (drop a custom cut): [{"op":"update","id":"c2","shot":5,"start":null,"end":null}]
- Change BGM: audio_diff([{"op":"set","source":"MUSIC/x.mp3","volume":1.0}])
- Add a transition INTO c2: timeline_diff([{"op":"transition","clip":"c2","type":"fade","duration":0.5}])

## Material content rule (hard)

ALL knowledge about what media files contain MUST come from `probe_media`,
`analyze_media`, `list_shots` and `list_music`. NEVER call `read_file` (or any
filesystem tool) on files under INPUT/ or MUSIC/ — your planner model is
text-only, so media bytes in a tool result are rejected by the API with a hard
400 and the whole turn is lost. `ls` on directories is fine. Do NOT try to
look at the media yourself.

{CREATIVE_DOC}

## When to ask instead of submit

Call `ask_user` INSTEAD of submitting when ANY of these holds:
- The message contains no actionable editing operation (e.g. "haha", "test").
- The user asks for OPTIONS / ALTERNATIVES before any editing ("give me a few
  styles", "don't cut yet"). Present 2-4 concrete alternatives via `ask_user`
  and wait. Do NOT submit() in the same turn.
- The message asks ABOUT previous work rather than requesting a NEW edit —
  report the facts you were given and ask whether any change is wanted.
- The target material is ambiguous ("that video" while INPUT/ holds several).
- The request needs a capability that is not in the effect list — say so
  honestly and offer the closest alternative.
- The probed facts show the request exceeds the material ("the first 50
  seconds" but the clip is 12s) — do NOT hard-clamp and do NOT fabricate
  numbers; report the real duration and offer options.

`ask_user` and `submit()` are mutually exclusive — pick exactly one per turn.

## Probe field

Do NOT write "probe" anywhere — the host re-probes every source itself. Call
`probe_media` to LEARN the facts for your own decisions. "end" may be omitted
to use the whole clip (the host knows the real duration).

## Draft shape (reference — you build it via the tools, never by hand)

{
  "schema_version": "3.0",
  "output": {"filename": "OUTPUT/final.mp4", "width": 1280, "height": 720, "fps": 30},
  "clips": [
    {"id": "c1", "reader": {"type": "FFmpegReader", "path": "INPUT/a.mp4"},
     "shot": 3,
     "cut_silence": {"noise_db": -35, "min_silence": 0.4, "keep_padding": 0.15}},
    {"id": "c2", "reader": {"type": "FFmpegReader", "path": "INPUT/b.mp4"},
     "start": 12, "end": 18.5, "speed": 0.5, "volume": 0.8,
     "effects": [{"id": "e1", "type": "Brightness", "brightness": 0.15}]},
    {"id": "c3", "reader": {"type": "ImageReader", "path": "INPUT/img.png"},
     "duration": 3}
  ],
  "timeline": [
    {"clip": "c1"},
    {"clip": "c2", "transition": {"type": "fade", "duration": 0.5}}
  ],
  "overlays": [
    {"id": "o1", "type": "pip", "source": "INPUT/logo.png", "kind": "image",
     "at_clip": "c1", "start_offset": 1, "duration": 3,
     "gravity": "bottom-right", "scale": 0.25},
    {"id": "o2", "type": "text", "text": "Hello", "at_clip": "c1",
     "start_offset": 0.5, "duration": 2, "gravity": "center",
     "font_size": 48, "color": "#FFFFFF"}
  ],
  "audio": {"source": "MUSIC/x.mp3", "volume": 1.0, "loop": true}
}

- Clip order = timeline order (the arrays stay in sync automatically; there is
  no absolute "position" field — that is by design, you never compute seconds
  on the timeline).
- "shot": N = the N-th shot of that source (`INPUT/a.mp4#03` in list_shots);
  mutually exclusive with "start"/"end".
- Times are RELATIVE: overlay "start_offset" counts from the START of the
  trimmed clip segment. "start"/"end" are positions in the ORIGINAL source.
- A transition hangs on the timeline item of the SECOND clip (the one being
  transitioned INTO). transition.type: fade, dissolve, wipeleft, wiperight,
  wipeup, wipedown, slideleft, slideright, slideup, slidedown, circleopen,
  circleclose.
- Per-clip effects go in that clip's `effects` array — you attach them with
  effect_diff. The ONLY valid `type` values:

{EFFECTS_DOC}

## select macro (V5 bulk selection — legacy submit_plan only)

When the task is "pick matching shots from the materials into a reel", do NOT
enumerate clips: submit via the legacy `submit_plan` tool a plan containing a
top-level `select` block INSTEAD of clips/timeline/overlays:

{
  "schema_version": "3.0",
  "output": {"filename": "OUTPUT/friends.mp4", "width": 1280, "height": 720},
  "select": {
    "sources": ["INPUT/a.mp4", "INPUT/b.mp4"],
    "where": {"scene_any": ["聚会", "餐厅"], "person_count_min": 2,
              "quality_in": ["good", "ok"], "min_duration": 1.5,
              "max_silence_ratio": 0.6},
    "budget_seconds": 30, "order": "as_listed"
  }
}

- All `where` fields are optional; matching is AND across fields (OR within a
  list). `budget_seconds` omitted = take all matches; order: "as_listed" |
  "best_first". Every source MUST already have a content card.
- The `workflow` macro (one-click reel / speech clean / smart_create) is
  submitted the same way — see its doc below.

{WORKFLOW_DOC}

{AUDIO_DOC}

Submit with submit(), then stop. Do not write any ffmpeg command.
"""

# 能力清单从 skills 注册表派生 —— 注册表加一条，提示词自动多一条，
# 不会出现"提示词与白名单脱节导致模型猜 effect 名"的旧坑。
V2_SYSTEM_PROMPT = (
    V2_SYSTEM_PROMPT_TEMPLATE
    .replace("{EFFECTS_DOC}", render_prompt_doc_v3())
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


# edit_plan(ops)（V9 修订词表）已在 V12 退役：修订轮由宿主把上一版计划载入
# draft（reset_draft(base_plan)），与首轮共用同一套 diff 工具。plan_ops.py
# 冻结保留（纯函数 + 测试仍在）。


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
    """V12 agent：感知/曲库工具 + 按对象拆分的 diff 工具面 + ask_user。

    V12 起计划通过 plan_tools 的 diff 工具构建（clip_diff/effect_diff/… →
    submit），draft 是 v3 计划 JSON；submit_plan 保留为 legacy 路径
    （select/workflow 宏）。V10 动词工具面冻结在 plan_tools_v10（回滚开关：
    把 plan_tools 换回 plan_tools_v10 并恢复旧 V2_SYSTEM_PROMPT 即可）。
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
               submit_plan, ask_user, *plan_tools.PLAN_TOOLS],
        backend=backend,
        name="video-editing-planner-v2",
    )


def _autofill_probe(plan: dict, project_root: str, probe_fn=None) -> int:
    """宿主用真实探针回填 clips[].probe（「引用代替誊抄」2026-10-28）。

    对应 OpenAI function-calling 指南「不要让模型填你已知的参数」：素材事实
    宿主本来就会 probe（服务端缓存/现场 ffprobe），模型手抄 probe 再靠
    probe_mismatch 交叉核验属于双重浪费，抄错/编造的空间也在这里。回填一律
    覆盖模型写的值——誊抄错误从结构上消失，probe 字段对模型变为可不写。
    probe 是宿主侧数据，不进 v3 协议（引擎层 LLM 写不进来），但编译/校验
    需要——回填到内部形态消费的位置（v3 clip 上带 probe 字段，方言层降级
    时透传给 v2 校验，会话存储无碍）。只回填 video 片段（图片 probe 无意义
    且本机 ffmpeg 会把静图探成 0.04s 的怪值）。回填失败（素材缺失等）保持
    原样，交给下游「素材不存在」校验给出自救信息。返回回填个数。
    """
    filled = 0
    import plan_schema
    fn = probe_fn or plan_schema._default_probe_fn(project_root)
    for c in plan.get("clips") or []:
        if not isinstance(c, dict):
            continue
        src = c.get("source") or (c.get("reader") or {}).get("path")
        if not isinstance(src, str):
            continue
        kind = c.get("kind") or plan_dialect._kind_of(src)
        if kind != "video":
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
                    preamble: list | None = None,
                    tool_mode: bool = True) -> list:
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
        body = ("上一次尝试没有产生可执行的计划或提问。请重新完成这个任务：如果用户的消息"
                "是剪辑/修改要求，用 diff 工具把草稿改好后调用 submit() 提交"
                "（select/workflow 宏任务用 submit_plan）；"
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
        "如果某个效果当前不支持，请如实说明做不到，不要臆造字段。"
    )
    if tool_mode:
        # V12 diff 工具构建：反馈面向 diff 工具而非 submit_plan
        if plan is not None:
            instruction = (
                "要求：用对应的 diff 工具只修正上述问题（clip_diff / "
                "effect_diff / overlay_diff / timeline_diff / audio_diff 的 "
                "update/delete op，get_plan() 可查看当前 draft），其余内容"
                "保持不变，修正后重新调用 submit()。不要 set_project 从头"
                "重建。"
            )
        else:
            instruction = (
                "要求：用 diff 工具构建计划（set_project → clip_diff 批量 "
                "insert（shot 引用）→ timeline_diff / effect_diff / "
                "overlay_diff / audio_diff / set_narration → validate_plan → "
                "submit），不要用 submit_plan 手写整份 JSON。"
            )
    head = preamble if preamble is not None else [{"role": "user", "content": task}]
    return head + [{"role": "user", "content": body + "\n\n" + instruction}]


# ---------------------------------------------------- 会话上下文（纯文本） ---- #

def _plan_table_rows(base_plan: dict) -> list[str]:
    """计划的紧凑片段表（修订 brief / 问答接续 brief 共用的锚定渲染）。

    V12：输入可能是 v3（新会话）或 v2（旧会话未升级路径），先归一到 v3 再渲染，
    表格词汇与 LLM 写的 diff 工具参数一致。
    """
    base_plan = plan_dialect.normalize(base_plan)
    trans = {t.get("clip"): t.get("transition")
             for t in base_plan.get("timeline") or []}
    rows = []
    for c in base_plan.get("clips") or []:
        s = c.get("start")
        e = c.get("end")
        rng = f"{s:g}-{e:g}s" if isinstance(s, (int, float)) \
            and isinstance(e, (int, float)) else \
            (f"#{c['shot']}" if c.get("shot") else "整段")
        tr = trans.get(c.get("id"))
        tr_s = "硬切" if not tr else f"{tr.get('type')} {tr.get('duration')}s"
        eff = ",".join(e.get("type", "?") if isinstance(e, dict) else str(e)
                       for e in (c.get("effects") or [])) or "-"
        speed = f" speed={c['speed']:g}" if c.get("speed") is not None else ""
        mute = " [已静音]" if (c.get("audio") or {}).get("mute") else ""
        vol = f" vol={c['volume']:g}" if c.get("volume") is not None else ""
        rows.append(f"  {c.get('id')}  {str((c.get('reader') or {}).get('path')).rsplit('/', 1)[-1]}"
                    f"  {rng}  转场:{tr_s}  效果:{eff}{speed}{vol}{mute}")
    return rows


def build_revision_brief(task: str, base_plan: dict, base_output: str,
                         materials: list[str], new_task: str) -> list:
    """修订轮上下文（V12 diff 模式）：片段表 + diff 工具词汇表 + 本次要求。

    修订轮不让模型重写整份计划——宿主已把上一版计划载入 draft（plan_with_retry
    的 reset_draft(base_plan)），模型用同一套 diff 工具只改要求的部分。
    片段表给到 id/素材/裁剪/转场/效果，足够下 op；完整 JSON 不再重放。
    """
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
        + plan_tools.ACTIONS_DOC + "\n\n"
        "提交方式：改完调用 validate_plan() → submit()。这是修订轮的执行出口。\n"
        "动手前先判断消息性质：如果它不是修改要求，而是在询问/确认上一轮的情况"
        "（「你重新剪了吗」「成片多长」），调用 ask_user 如实汇报，不要执行。\n"
        "如果用户提到了具体对象（某个片段/素材/场景），只对那些片段下 op；"
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
        "请据此继续：draft 里已有当前计划（宿主已载入），用 diff 工具只改"
        "需要的部分，改完 validate_plan() → submit()；"
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
      base_plan      上一版计划（v3）：修订轮把它载入 draft 作初态——模型在同一
                     套 diff 工具上只改要求的部分（V12：edit_plan(ops) 通道退役）

    （V9 的 ops_mode / apply_ops_fn 已删除：修订轮与首轮共用 diff 工具路径，
    plan_ops.py 冻结保留。）
    """
    first_messages: list = list(messages) if messages else [{"role": "user", "content": task}]
    messages = first_messages
    result: dict = {}
    last_errors: list[str] | None = None
    # V12 diff 工具构建：新 run 初始化 draft；修订轮传入上一版计划作初态
    #（模型在上一版上改，不重建）；重试不重置——模型只需修正出问题的部分再 submit
    plan_tools.reset_draft(base_plan)

    for attempt in range(max_retries + 1):
        emit({"type": "attempt", "n": attempt + 1, "max": max_retries + 1})

        # ① 调模型（异常也转入统一的失败处理）
        plan: dict | None = None
        errors: list[str] | None = None
        try:
            result = invoke(messages)
            # V12：draft 工具构建（submit() 提交）；模型仍调 submit_plan 时
            # 走 legacy 一次性路径（select/workflow 宏）
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
                errors = ["你没有提交可执行的计划：用 diff 工具构建草稿后调用 "
                          "submit() 提交（或 select/workflow 宏任务用 submit_plan）。"]
                emit({"type": "error", "message": "未能从 agent 输出中提取计划。"})
            else:
                # D4：修订轮不覆盖上一版产物（后端强制，不靠模型自觉）
                _guard_overwrite(plan, base_output, output_suffix, quality,
                                 emit, history_outputs=history_outputs)
                # Preflight Tier 2：素材现实 × 计划要求（v3 走方言层降级）
                mismatches = plan_dialect.check_material_fit_any(
                    plan, project_root, probe_fn=probe_fn)
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
        messages = _retry_feedback(task, plan, errors, preamble=first_messages)

    return None, result

