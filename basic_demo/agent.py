"""DeepAgents demo — build a runnable coding agent.

This wraps `deepagents.create_deep_agent`, which by default gives the agent a
built-in tool suite:

  ls, read_file, write_file, edit_file, glob, grep   -> filesystem tools
  execute                                            -> run shell commands (sandbox backend)
  task                                               -> delegate to subagents

We add a focused system prompt so the agent behaves like a small coding
assistant, and expose a single `run(task)` helper used by `main.py`.
"""

from __future__ import annotations

import os

from deepagents import create_deep_agent
from deepagents.backends.local_shell import LocalShellBackend
from langgraph.graph.state import CompiledStateGraph

from model import get_model

SYSTEM_PROMPT = """You are a focused coding agent demo.

You help the user accomplish concrete file/code tasks inside the working
directory. Prefer using your filesystem tools (write_file, read_file, ls,
glob, grep) to actually create and inspect files rather than only describing
what to do.

Keep responses concise. When a task is done, summarize what you produced in a
short bullet list.
"""


def build_agent(working_dir: str | None = None) -> CompiledStateGraph:
    """Construct and return a compiled deep agent.

    A `LocalShellBackend` rooted at `working_dir` is used so that file writes
    and shell commands actually touch the real filesystem under that directory
    (the default `StateBackend` only keeps files in ephemeral in-memory state).
    With `virtual_mode=True` (default), an absolute path like `/app.py` maps to
    `{working_dir}/app.py`.
    """
    model = get_model()
    backend = LocalShellBackend(root_dir=working_dir) if working_dir else LocalShellBackend()
    agent = create_deep_agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        backend=backend,
        name="demo-coding-agent",
    )
    return agent


def run(task: str, working_dir: str | None = None) -> str:
    """Run a single task through the agent and return its final answer."""
    agent = build_agent(working_dir)
    result = agent.invoke({"messages": [{"role": "user", "content": task}]})
    messages = result.get("messages", [])
    # The last message is the agent's final reply.
    return messages[-1].content if messages else ""
