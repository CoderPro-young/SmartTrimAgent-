"""等待可观测性（heartbeat）+ 主模型超时重试 离线测试。

背景（2026-10-11 线上实测）：一次 SiliconFlow 瞬时故障让任务静默 10+ 分钟，
前端零反馈形似卡死。修复分两层：
  1) server._stream_ndjson 静默期由 _heartbeat_pump 补 heartbeat 事件，
     前端据此显示「已等待 Xs」；
  2) model.get_model() 给主模型 ChatOpenAI 显式 timeout / max_retries，
     超时由 openai SDK 指数退避重试，耗尽才抛给上层 plan_with_retry。
"""
import importlib.util
import os
import queue
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def _load(name, rel, env=None):
    """按指定环境变量加载一个顶层模块（load_dotenv 不覆盖已存在的变量）。"""
    saved = {k: os.environ.get(k) for k in (env or {})}
    os.environ.update({k: v for k, v in (env or {}).items()})
    try:
        spec = importlib.util.spec_from_file_location(name, ROOT / rel)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main():
    print("== heartbeat 泵 ==")
    server = _load("st_server_wait", "web/server.py")

    # t0 回归：_stream_ndjson 必须仍是 Handler 的方法（方法/模块级误移会让类结构断裂）
    check("t0 _stream_ndjson 仍是 Handler 方法",
          hasattr(server.Handler, "_stream_ndjson"))

    # t1 静默期按周期发 heartbeat，idle 递增
    q: queue.Queue = queue.Queue()
    stop = threading.Event()
    last_ts = [time.monotonic()]
    pump = threading.Thread(target=server._heartbeat_pump,
                            args=(q, stop, last_ts, 0.05), daemon=True)
    pump.start()
    time.sleep(0.18)
    beats = []
    while not q.empty():
        ev = q.get_nowait()
        if ev.get("type") == "heartbeat":
            beats.append(ev["idle"])
    stop.set()
    check("t1 静默期发出多条 heartbeat", len(beats) >= 2, f"beats={beats}")
    check("t1b idle 单调不减", all(b2 >= b1 for b1, b2 in zip(beats, beats[1:])),
          f"beats={beats}")

    # t2 真实事件刷新 last_ts 后，idle 从小值重新计
    q2: queue.Queue = queue.Queue()
    stop2 = threading.Event()
    last_ts2 = [time.monotonic() - 10]          # 模拟上一事件已是 10s 前
    emit = lambda ev: (last_ts2.__setitem__(0, time.monotonic()), q2.put(ev))
    pump2 = threading.Thread(target=server._heartbeat_pump,
                             args=(q2, stop2, last_ts2, 0.05), daemon=True)
    pump2.start()
    time.sleep(0.12)                            # 先漏出一条大 idle 的心跳
    emit({"type": "llm"})                       # 真实事件到达
    time.sleep(0.12)                            # 下一条心跳应从小 idle 重新计
    stop2.set()
    got = [q2.get_nowait() for _ in range(q2.qsize())]
    beats2 = [e["idle"] for e in got if e.get("type") == "heartbeat"]
    check("t2 事件后 idle 重新计时（不带上累积）",
          len(beats2) >= 2 and beats2[-1] < 10 and beats2[0] > 5,
          f"beats={beats2}")

    # t3 stop 置位后泵及时退出（join 等待调度，避免 stop 后立即 is_alive 的竞态）
    pump.join(timeout=2)
    pump2.join(timeout=2)
    check("t3 stop 后泵退出", not pump.is_alive() and not pump2.is_alive())

    print("== 主模型超时/重试 ==")
    model = _load("st_model_wait", "model.py",
                  env={"SILICONFLOW_API_KEY": "test-key", "MODEL_TIMEOUT": "7.5"})
    from langchain_openai import ChatOpenAI
    m = model.get_model()
    check("t4 返回 ChatOpenAI", isinstance(m, ChatOpenAI))
    check("t5 MODEL_TIMEOUT 环境变量生效", float(m.request_timeout) == 7.5,
          f"request_timeout={m.request_timeout}")
    check("t6 max_retries 默认 2", m.max_retries == 2, f"max_retries={m.max_retries}")

    model2 = _load("st_model_wait2", "model.py",
                   env={"SILICONFLOW_API_KEY": "test-key"})   # 不配则用默认 180s
    m2 = model2.get_model()
    check("t7 默认超时 180s", float(m2.request_timeout) == 180.0,
          f"request_timeout={m2.request_timeout}")

    print(f"\n通过 {PASS} / 失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
