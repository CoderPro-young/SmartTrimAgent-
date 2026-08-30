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
from langchain_core.tools import tool
from langgraph.graph.state import CompiledStateGraph

import ffmpeg_exec
from model import get_model

# 仓库根：INPUT/ OUTPUT/ 在根下，LocalShellBackend 也锚定到根
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INPUT_DIR = os.path.join(PROJECT_ROOT, "INPUT")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "OUTPUT")

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
    return agent.invoke({"messages": [{"role": "user", "content": task}]})


# ---------------------------------------------------------------- V2 ------- #

V2_SYSTEM_PROMPT = """You are a video editing planner (V2). You do NOT write
ffmpeg commands. Your job is to produce an editing plan in JSON and submit it
with the `submit_plan` tool.

## Workflow

1. Use `ls` (or the built-in filesystem tools) to list files in INPUT/.
2. Use `probe_media` on each source you plan to use, to learn its real
   duration / resolution / whether it has audio.
3. Think through the edit, then call `submit_plan` with a valid plan object.

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
                "fps": 30, "has_audio": true}
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

Submit the plan, then stop. Do not write any ffmpeg command.
"""


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


def build_video_agent_v2() -> CompiledStateGraph:
    """V2 agent：probe_media + submit_plan，输出编辑计划而非 ffmpeg 命令。"""
    os.makedirs(INPUT_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    model = get_model()
    backend = LocalShellBackend(root_dir=PROJECT_ROOT)
    return create_deep_agent(
        model=model,
        system_prompt=V2_SYSTEM_PROMPT,
        tools=[probe_media, submit_plan],
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


def run_video_plan_task(task: str) -> dict:
    """运行 v2 计划任务，返回完整 result（含 submit_plan 工具调用）。"""
    agent = build_video_agent_v2()
    return agent.invoke({"messages": [{"role": "user", "content": task}]})

