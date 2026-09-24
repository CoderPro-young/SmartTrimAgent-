"""视频处理 Agent demo 入口（V2 计划模式）。

流程：用户自然语言 → agent（探测素材 + submit_plan 提交计划）→ 编译器
（校验→推导→生成命令）→ 展示命令序列 → 执行（ffmpeg 未装则友好提示）→ 回验。

用法：
    配置写进 .env（模板 .env.example）：SILICONFLOW_API_KEY / MODEL_NAME / LangSmith
    python video_editing/video_demo.py            # 默认演示（需要 INPUT/ 里有示例素材）
    python video_editing/video_demo.py "把 INPUT/my.mp4 前 8 秒和 INPUT/pic.png(3秒) 用 fade 拼接，720p"
"""

from __future__ import annotations

import json
import os
import sys

# 项目根目录（model.py / .env / INPUT / OUTPUT 都在根下）+ 自身目录（同目录模块平铺导入）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import content_analysis
import plan_compiler
from video_agent import make_cli_invoker, plan_with_retry  # noqa: E402


def _cli_progress(ev: dict) -> None:
    """感知层（V4）进度打到终端，让「分析中」可见。"""
    stage = ev.get("stage")
    if stage == "start":
        print(f"[感知] 开始分析 {ev.get('file')}（{ev.get('kind')}）：场景切分 + 抽帧…")
    elif stage == "sample":
        print(f"[感知] {ev.get('file')}：抽帧完成（{ev.get('shots')} 个镜头），VLM 打标中…")
    elif stage == "tag":
        print(f"[感知] {ev.get('file')}：VLM 打标 {ev.get('done')}/{ev.get('total')} 批")
    elif stage == "done":
        cached = "（缓存命中，零计算）" if ev.get("cached") else ""
        print(f"[感知] {ev.get('file')}：索引完成{cached} —— {ev.get('summary') or ''}")

DEFAULT_TASK = (
    "把 INPUT/sample.mp4 的前 8 秒和 INPUT/test.png（展示 3 秒）拼接成一段视频，"
    "两个素材之间用 0.5 秒 fade 转场；再在第一个片段开始 1 秒处叠加一个画中画，"
    "内容是 INPUT/logo.png，放在右下角、大小为 1/4，持续 3 秒；"
    "并在第一个片段 0.5 秒处加一行花字“演示”，居中偏上，持续 2 秒。"
    "输出 1280x720、30fps 到 OUTPUT/final.mp4。"
)
# 不带参数运行时需要预置的素材：INPUT/ 默认是空的，缺了就给出提示而不是让 agent 空转
DEFAULT_INPUTS = ["INPUT/sample.mp4", "INPUT/test.png", "INPUT/logo.png"]


def _cli_emit(event: dict, verbose: bool) -> None:
    """把 plan_with_retry 的事件打到终端（verbose 只控制流程细节，错误始终打印）。"""
    typ = event.get("type")
    if typ == "attempt":
        if verbose:
            print(f"\n—— 第 1 步：LLM 出计划（第 {event['n']}/{event['max']} 次尝试）——")
    elif typ == "plan":
        if verbose:
            print("计划 JSON：")
            print(json.dumps(event["plan"], ensure_ascii=False, indent=2))
    elif typ == "compile_error":
        print("[校验失败] 编译器报错：")
        for e in event["errors"]:
            print("  -", e)
    elif typ == "status":
        print(f"[重试] {event['text']}")
    elif typ == "error":
        print(f"[失败] {event.get('message')}")


def main() -> None:
    """三步流程入口：LLM 出计划 → 编译器出命令 → 执行 + 回验。"""
    # 其余打印的总开关：默认只打印每次 LLM 的输出；
    # 需要看完整流程时设 DEMO_VERBOSE=1。
    verbose = os.environ.get("DEMO_VERBOSE") == "1"
    content_analysis.set_progress_hook(_cli_progress)
    if len(sys.argv) > 1:
        task = sys.argv[1]
    else:
        # INPUT/ 默认是空的（素材由用户自己放），所以先检查默认演示任务的素材在不在
        missing = [p for p in DEFAULT_INPUTS
                   if not os.path.isfile(os.path.join(PROJECT_ROOT, p))]
        if missing:
            print("默认演示任务需要这些素材，但 INPUT/ 里没有：")
            for p in missing:
                print("  -", p)
            print("\n请把自己的视频/图片放进 INPUT/ 后重跑，或直接传入任务，例如：")
            print('  python video_editing/video_demo.py '
                  '"把 INPUT/我的视频.mp4 转成 720p，输出到 OUTPUT/out.mp4"')
            print("\n（示例素材在 git 历史里，需要时用 `git checkout -- INPUT/` 取回）")
            sys.exit(2)
        task = DEFAULT_TASK
    if verbose:
        print("=" * 70)
        print("视频处理 Agent V2 —— LLM 出计划，编译器出命令")
        print("模型:", os.environ.get("MODEL_NAME", "deepseek-ai/DeepSeek-V4-Flash (默认)"))
        print("=" * 70)
        print(f"\n[用户任务] {task}\n")

    # 1) agent 出计划 + 2) 编译（校验失败会把错误回传模型重出，最多 MAX_PLAN_RETRIES 次）
    #    invoke 约定：invoke(messages: list) -> state（与 Web 端一致）
    compiled, _result = plan_with_retry(
        task,
        PROJECT_ROOT,
        invoke=make_cli_invoker(),
        emit=lambda ev: _cli_emit(ev, verbose),
    )
    if compiled is None:
        sys.exit(2)

    if verbose:
        print("\n—— 第 2 步：编译器（校验 → 换算 → 生成命令）——")
        m = compiled.math
        print(f"[换算] 片段时长 d_i = {m['durations']}")
        print(f"[换算] 各片段起点 S_i = {m['starts']}")
        print(f"[换算] 总时长 D = {m['D']}s")
        print(f"[换算] overlay 绝对时间 = {m['overlays']}")
        print("\n生成的命令序列：")
        print(compiled.render_summary())

        # 3) 执行 + 回验（ffmpeg 未装则友好跳过）
        print("\n—— 第 3 步：执行 + 回验 ——")
    try:
        plan_compiler.execute(compiled, PROJECT_ROOT)
    except plan_compiler.CompileError as exc:
        # dry-run 语法预检失败：命令一条都没真跑，错误可直接展示/回传
        print("\n[dry-run 预检失败] 已停止执行：")
        for e in exc.errors:
            print(" -", e)
        sys.exit(2)
    if verbose:
        for pc in compiled.prechecks:
            if pc.get("note"):
                print(f"  [预检跳过] {pc.get('description') or ''} {pc['note']}")
        for ex in compiled.executes:
            status = "OK" if ex.get("ok") else "跳过/失败"
            note = ex.get("error") or (ex.get("stderr") or "")[:120]
            print(f"  [{status}] {ex['description']} {note}")
    plan_compiler.verify(compiled, PROJECT_ROOT)
    if verbose:
        print("  [回验]", compiled.verify)


if __name__ == "__main__":
    main()
