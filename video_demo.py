"""视频处理 Agent demo 入口（V2 计划模式）。

流程：用户自然语言 → agent（探测素材 + submit_plan 提交计划）→ 编译器
（校验→推导→生成命令）→ 展示命令序列 → 执行（ffmpeg 未装则友好提示）→ 回验。

用法：
    export SILICONFLOW_API_KEY=sk-...
    export MODEL_NAME=deepseek-ai/DeepSeek-V4-Flash   # 默认
    python video_demo.py
    python video_demo.py "把 sample.mp4 前 8 秒和 test.png(3秒) 用 fade 拼接，720p"
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plan_compiler
from video_agent import extract_plan, run_video_plan_task  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

DEFAULT_TASK = (
    "把 INPUT/sample.mp4 的前 8 秒和 INPUT/test.png（展示 3 秒）拼接成一段视频，"
    "两个素材之间用 0.5 秒 fade 转场；再在第一个片段开始 1 秒处叠加一个画中画，"
    "内容是 INPUT/logo.png，放在右下角、大小为 1/4，持续 3 秒；"
    "并在第一个片段 0.5 秒处加一行花字“演示”，居中偏上，持续 2 秒。"
    "输出 1280x720、30fps 到 OUTPUT/final.mp4。"
)


def main() -> None:
    task = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TASK
    print("=" * 70)
    print("视频处理 Agent V2 —— LLM 出计划，编译器出命令")
    print("模型:", os.environ.get("MODEL_NAME", "deepseek-ai/DeepSeek-V4-Flash (默认)"))
    print("=" * 70)
    print(f"\n[用户任务] {task}\n")

    # 1) agent 出计划
    print("—— 第 1 步：LLM 探测素材并生成编辑计划 ——")
    result = run_video_plan_task(task)
    plan = extract_plan(result)
    if plan is None:
        print("[失败] 未能从 agent 输出中提取 submit_plan 计划。")
        last = result.get("messages", [])[-1]
        print("agent 最后输出：", str(last.content)[:500] if last else "(空)")
        sys.exit(1)

    print("计划 JSON：")
    print(json.dumps(plan, ensure_ascii=False, indent=2))

    # 2) 编译：校验 → 推导 → 生成命令
    print("\n—— 第 2 步：编译器（校验 → 换算 → 生成命令）——")
    try:
        compiled = plan_compiler.compile_plan(plan, PROJECT_ROOT)
    except plan_compiler.CompileError as exc:
        print("[校验失败]")
        for e in exc.errors:
            print("  -", e)
        sys.exit(2)

    m = compiled.math
    print(f"[换算] 片段时长 d_i = {m['durations']}")
    print(f"[换算] 各片段起点 S_i = {m['starts']}")
    print(f"[换算] 总时长 D = {m['D']}s")
    print(f"[换算] overlay 绝对时间 = {m['overlays']}")
    print("\n生成的命令序列：")
    print(compiled.render_summary())

    # 3) 执行 + 回验（ffmpeg 未装则友好跳过）
    print("\n—— 第 3 步：执行 + 回验 ——")
    plan_compiler.execute(compiled, PROJECT_ROOT)
    for ex in compiled.executes:
        status = "OK" if ex.get("ok") else "跳过/失败"
        note = ex.get("error") or (ex.get("stderr") or "")[:120]
        print(f"  [{status}] {ex['description']} {note}")
    plan_compiler.verify(compiled, PROJECT_ROOT)
    print("  [回验]", compiled.verify)


if __name__ == "__main__":
    main()
