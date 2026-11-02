"""自动化测试数据集驱动：按用例组装填素材、逐轮提交 /api/run、计时、校验成片。

用法：
    python tests/run_dataset.py 用例组1 [用例组2 用例组3]   # 按顺序跑指定组
    python tests/run_dataset.py all                        # 跑全部组

行为：
  1. 每组开跑前调用 load_dataset.py <组> --clean 装填素材（复刻原始环境）
  2. 每组用全新 session_id（dataset-<组>-<时间戳>），逐轮 POST /api/run
  3. 轮内若收到 ask_user 反问且该轮登记了选答文本，自动作为接续消息提交（两段式计同一轮耗时）
  4. 结果（各轮墙钟、出片、回验）落 tests/results/run-<时间戳>.json，进度同步打 stdout
"""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "tests" / "results"

# 轮次文本与 tests/test_dataset.md 逐字一致；ask_user 选答文本来自原始会话
CASES = [
    {
        "id": "用例组1",
        "ref_output": "OUTPUT/草原秋日vlog-r2-r3-r4-r5.mp4",
        "turns": [
            {"text": "用我的素材做一条旅行 vlog：按行程的起承转合挑镜头分组，旁白写具体的地方和感受（不要套路词），挑选合适的背景音乐，30 秒左右"},
            {"text": "时长改为50s，去掉字幕"},
            {"text": "帮我吧转场方式换的炫酷一些，就是那种旋转的加上光影的淡入淡出效果",
             "answer": "圆形旋转扩散 circleopen + dissolve 光影柔化（最接近「旋转+光影」）"},
            {"text": "只用 dissolve 溶解柔化（最贴「光影淡入淡出」，放弃旋转感）"},
            {"text": "换个BGM 那种旅游中的感动的感觉"},
        ],
    },
    {
        "id": "用例组2",
        "ref_output": "OUTPUT/travel_vlog-r2-r3-r4-r5.mp4",
        "turns": [
            {"text": "用我的素材做一条旅行 vlog：按行程的起承转合挑镜头分组，旁白写具体的地方和感受（不要套路词），节奏轻快，配轻快的曲库音乐，60秒左右"},
            {"text": "去掉字幕"},
            {"text": "换个BGM"},
            {"text": "转场方式再修改一下，相邻素材之间使用不同的转场方式"},
            {"text": "把第一个素材片段删除把"},
        ],
    },
    {
        "id": "用例组3",
        "ref_output": "OUTPUT/concert_vlog-r2.mp4",
        "turns": [
            {"text": "一键成片"},
            {"text": "演唱会片段不需要BGM 保留原始的声音"},
        ],
    },
]


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)


def post_run(task: str, session_id: str, timeout: int = 900):
    """POST /api/run，返回 (事件列表, 结局)。结局: done / question / error / stream_broken。"""
    body = json.dumps({"task": task, "session_id": session_id}).encode()
    req = urllib.request.Request(f"{BASE}/api/run", data=body,
                                 headers={"Content-Type": "application/json"})
    events, outcome = [], None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                ev = json.loads(raw)
                events.append(ev)
                t = ev.get("type")
                if t == "done":
                    outcome = "done"
                elif t == "question":
                    outcome = "question"
                elif t == "error":
                    outcome = "error"
    except urllib.error.URLError as e:
        outcome = "stream_broken"
        events.append({"type": "stream_broken", "message": str(e)})
    return events, outcome


def run_turn(turn: dict, session_id: str) -> dict:
    """跑一轮（含 ask_user 接续），返回该轮计时与结局摘要。"""
    record = {"text": turn["text"], "seconds": 0.0, "outcome": None,
              "output": None, "verify": None, "asked": None, "errors": []}
    t0 = time.time()
    events, outcome = post_run(turn["text"], session_id)
    for ev in events:
        if ev.get("type") == "question":
            record["asked"] = {"question": (ev.get("question") or "")[:200],
                               "options": ev.get("options")}
        elif ev.get("type") in ("error", "compile_error"):
            record["errors"].append(str(ev)[:200])
        elif ev.get("type") == "done":
            record["output"] = ev.get("output")
        elif ev.get("type") == "verify":
            record["verify"] = ev.get("verify")

    if outcome == "question" and turn.get("answer"):
        log(f"    反问: {record['asked']['question'][:80]}")
        log(f"    自动选答: {turn['answer']}")
        ans_events, ans_outcome = post_run(turn["answer"], session_id)
        events += ans_events
        outcome = ans_outcome
        for ev in ans_events:
            if ev.get("type") == "done":
                record["output"] = ev.get("output")
            elif ev.get("type") == "verify":
                record["verify"] = ev.get("verify")
            elif ev.get("type") in ("error", "compile_error"):
                record["errors"].append(str(ev)[:200])

    record["seconds"] = round(time.time() - t0, 1)
    record["outcome"] = outcome
    return record


def health_busy() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE}/api/health", timeout=10) as r:
            return bool(json.load(r).get("busy"))
    except Exception:
        return True  # 服务无响应按 busy 处理，避免叠发


def run_case(case: dict) -> dict:
    cid = case["id"]
    log(f"===== {cid} 开始：装填素材 =====")
    subprocess.run([sys.executable, str(ROOT / "tests" / "load_dataset.py"), cid, "--clean"],
                   check=True, cwd=str(ROOT))

    session_id = f"dataset-{cid}-{time.strftime('%m%d-%H%M%S')}"
    log(f"会话: {session_id}")
    case_result = {"id": cid, "session_id": session_id, "turns": [], "ok": True}

    for i, turn in enumerate(case["turns"], 1):
        while health_busy():
            log(f"  服务忙，等 30s 后重试（第 {i} 轮）")
            time.sleep(30)
        log(f"  第 {i}/{len(case['turns'])} 轮: {turn['text'][:60]}")
        rec = run_turn(turn, session_id)
        case_result["turns"].append(rec)
        log(f"    -> {rec['outcome']}，耗时 {rec['seconds']}s"
            + (f"，出片 {rec['output']}" if rec["output"] else "")
            + (f"，回验 {rec['verify']}" if rec["verify"] else ""))
        if rec["outcome"] != "done":
            case_result["ok"] = False
            if rec["asked"]:
                log(f"    ⚠ 未登记选答的反问，停在该组: {rec['asked']['question']}")
            break

    case_result["total_seconds"] = round(sum(t["seconds"] for t in case_result["turns"]), 1)
    final_out = next((t["output"] for t in reversed(case_result["turns"]) if t["output"]), None)
    case_result["final_output"] = final_out
    if final_out:
        p = ROOT / final_out
        case_result["output_exists"] = p.is_file()
        case_result["output_size_mb"] = round(p.stat().st_size / 1e6, 1) if p.is_file() else None
        if case["ref_output"] and final_out != case["ref_output"]:
            log(f"    ℹ 输出文件名与参考不同: {final_out}（参考 {case['ref_output']}）")
    log(f"===== {cid} 结束: ok={case_result['ok']}，总耗时 {case_result['total_seconds']}s =====\n")
    return case_result


def main() -> None:
    wanted = sys.argv[1:]
    if not wanted or wanted == ["all"]:
        cases = CASES
    else:
        cases = [c for c in CASES if c["id"] in wanted]
        unknown = set(wanted) - {c["id"] for c in CASES}
        if unknown:
            sys.exit(f"未知用例组: {unknown}")

    RESULTS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_path = RESULTS / f"run-{stamp}.json"
    report = {"started": stamp, "cases": []}
    for case in cases:
        report["cases"].append(run_case(case))
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    log("===== 汇总 =====")
    for c in report["cases"]:
        log(f"{c['id']}: ok={c['ok']} 总耗时={c.get('total_seconds')}s "
            f"出片={c.get('final_output')} 存在={c.get('output_exists')}")
        for i, t in enumerate(c["turns"], 1):
            log(f"  第{i}轮 {t['seconds']}s -> {t['outcome']}")
    log(f"结果文件: {out_path}")


if __name__ == "__main__":
    main()
