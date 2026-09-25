"""V5 真实素材测试驱动：POST /api/run 跑一轮自然语言任务，打印事件时间线。

用法：.venv/Scripts/python.exe tests/e2e_driver.py "<任务文本>" [session_id]
"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"


def run_task(task: str, session_id: str, timeout: int = 420):
    t0 = time.time()
    req = urllib.request.Request(
        f"{BASE}/api/run",
        data=json.dumps({"task": task, "session_id": session_id}).encode(),
        headers={"Content-Type": "application/json"})
    summary = {"tools": [], "attempts": 0, "plan": None, "exec_plan": None,
               "execs": [], "verify": None, "done": None, "errors": [],
               "questions": [], "hallucinations": []}
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            ev = json.loads(raw)
            t = ev.get("type")
            el = time.time() - t0
            if t == "attempt":
                summary["attempts"] += 1
                print(f"[{el:6.1f}s] 第 {ev['n']}/{ev['max']} 轮尝试")
            elif t == "llm":
                for tc in ev.get("tool_calls") or []:
                    name = tc.get("name")
                    args = tc.get("args") or {}
                    brief = {k: args[k] for k in list(args)[:2]}
                    summary["tools"].append(name)
                    print(f"[{el:6.1f}s] 工具 {name} {json.dumps(brief, ensure_ascii=False)[:110]}")
            elif t == "plan":
                summary["plan"] = ev["plan"]
                print(f"[{el:6.1f}s] 计划提交: clips={len(ev['plan'].get('clips') or [])}"
                      f" select={'有' if ev['plan'].get('select') else '无'}"
                      f" cut={sum(1 for c in ev['plan'].get('clips') or [] if c.get('cut_silence') or c.get('cut_black'))}")
            elif t == "exec_plan":
                summary["exec_plan"] = ev["plan"]
                clips = ev["plan"].get("clips") or []
                print(f"[{el:6.1f}s] 展开后: {len(clips)} 个片段 "
                      + ", ".join(f"{c['id']}({c.get('trim_start')}-{c.get('trim_end')}s)" for c in clips[:6]))
            elif t == "exec":
                summary["execs"].append(ev["ok"])
                mark = "✓" if ev["ok"] else "✗"
                print(f"[{el:6.1f}s] 执行 {mark} {ev['description']}")
                if not ev["ok"]:
                    print("        stderr:", (ev.get("stderr_tail") or ev.get("error") or "")[:300])
            elif t == "verify":
                summary["verify"] = ev["verify"]
                print(f"[{el:6.1f}s] 回验 {ev['verify']}")
            elif t == "done":
                summary["done"] = ev
                rep = ev.get("report") or {}
                print(f"[{el:6.1f}s] 出片 {ev['output']} ok={ev['ok']}")
                cuts = rep.get("cuts") or []
                if cuts:
                    print(f"        报告: 剪除 {rep.get('removed_total_seconds')}s/"
                          f"{sum(c['removed_segments'] for c in cuts)} 段")
                if rep.get("select"):
                    print(f"        报告: 筛选命中 {rep.get('selected_shots')} 镜头 / {rep['select'].get('total_seconds')}s")
            elif t == "question":
                summary["questions"].append(ev)
                print(f"[{el:6.1f}s] 反问: {ev.get('question','')[:120]}")
                print(f"        选项: {ev.get('options')}")
            elif t == "compile_error":
                summary["errors"] += ev.get("errors") or []
                print(f"[{el:6.1f}s] 编译错误: " + "; ".join(ev.get("errors") or [])[:300])
            elif t == "error":
                summary["errors"].append(ev.get("message"))
                print(f"[{el:6.1f}s] 错误: {ev.get('message','')[:300]}")
            elif t == "hallucination":
                summary["hallucinations"].append(ev)
                print(f"[{el:6.1f}s] 幻觉标记({ev.get('signal')}): {ev.get('evidence','')[:100]}")
            elif t == "status":
                print(f"[{el:6.1f}s] · {ev.get('text','')[:80]}")
    print(f"\n===== 汇总（{time.time()-t0:.1f}s）=====")
    print(f"工具调用: {summary['tools']}")
    print(f"尝试轮数: {summary['attempts']} | 反问: {len(summary['questions'])}"
          f" | 编译错误: {len(summary['errors'])} | 幻觉标记: {len(summary['hallucinations'])}")
    if summary["done"]:
        print(f"产物: {summary['done']['output']} | 回验: {summary['verify']}")
    elif summary["questions"]:
        print("结局: 等用户回答反问")
    else:
        print("结局: 未出片", summary["errors"][-1:] if summary["errors"] else "")


if __name__ == "__main__":
    run_task(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else f"e2e-{int(time.time())}")
