"""DeepAgents demo — entry point.

Usage:
    # 1) 把 .env.example 复制为 .env，填入模型 AK（如 SILICONFLOW_API_KEY）
    # 2) Run
    python main.py

The demo sends one task to the agent: create a small Python script and read it
back, showcasing the built-in filesystem tools (write_file / read_file).
"""

from __future__ import annotations

import os
import sys

# So `python main.py` works regardless of the current directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import build_agent, run  # noqa: E402

DEMO_TASK = (
    "Use the write_file tool with a RELATIVE path 'hello_deepagent.py' (no "
    "leading slash) to create a file in the current working directory that "
    "prints 'Hello from deepagent!'. Then use read_file on the same relative "
    "path to show its contents and confirm it was written."
)


def main() -> None:
    """跑一个内置演示任务：让 agent 写一个 hello 脚本再读回来。"""
    working_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    os.makedirs(working_dir, exist_ok=True)

    print("=" * 60)
    print("DeepAgents demo — coding agent")
    print("Working directory:", working_dir)
    print("=" * 60)

    # Build once so we can show it compiles, then run the task.
    try:
        agent = build_agent(working_dir)
    except RuntimeError as exc:
        print(f"\n[config] {exc}\n")
        print("Quick start:")
        print("  cp .env.example .env   # 填入 SILICONFLOW_API_KEY 等配置")
        print("  python main.py")
        print("\nSee .env.example for all options.")
        sys.exit(2)

    print(f"[ok] agent compiled: {type(agent).__name__}\n")

    print(f"[task] {DEMO_TASK}\n")
    answer = run(DEMO_TASK, working_dir=working_dir)

    print("-" * 60)
    print("Agent final answer:")
    print(answer)
    print("-" * 60)


if __name__ == "__main__":
    main()
