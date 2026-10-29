"""2026-10-28 链测定性后的 L1/L2 修复单测。

背景（docs/dev-notes-2026-10-28.md §七，TMP/_timed_last_run.jsonl 实证）：
非流式 180s read timeout × (1+2 次 SDK 静默重试) = 541.6s 纯等待，占轮 1
（716.5s）的 75.6%；VLM 路径同机制 3×120s=362s×4 并发放大。修复四件：
1. 默认流式 + MODEL_TIMEOUT 60（流式下 read timeout 按 chunk 间隔计，
   挂死在首 token 前即断，慢生成不受整段预算约束）；
2. MODEL_RETRIES 2→1、VLM_TIMEOUT/VLM_RETRIES 同步收紧；
3. StreamingChatOpenAI 子类：langchain-openai 1.6.0 的 _generate 已无流式
   分支（streaming 参数只对 .stream() 调用方生效），子类恢复「.invoke()
   内部消费流并聚合」的旧行为，deepagents 图内部调用才能吃到按 chunk 计
   的超时；首 chunk 触发 _STREAM_HOOKS（L3 可观测）；
4. init_chat_model 显式路径补传 timeout/max_retries（此前完全裸奔）。

本地挂死 stub（http.server 收下请求永不回包）验证机制本身，SSE stub 验证
流式聚合后 .invoke() 的 content/tool_calls 正确——这是 L2 的正确性闸门。

运行：.venv/Scripts/python.exe tests/test_model_streaming_timeout.py
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from langchain_core.messages import HumanMessage

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "video_editing"))

# 本地 stub 必须绕过系统代理（httpx trust_env 会把 127.0.0.1 也发给代理）
_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
               "NO_PROXY", "no_proxy")


@contextlib.contextmanager
def _no_proxy():
    saved = {k: os.environ.get(k) for k in _PROXY_KEYS}
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def model_env(**env):
    """临时改 env → reload(model) → 用完还原再 reload（常量在 import 期取值）。"""
    import importlib
    import model

    keys = {"MODEL_STREAMING", "MODEL_TIMEOUT", "MODEL_RETRIES",
            "VLM_TIMEOUT", "VLM_RETRIES", "DEEPAGENT_MODEL",
            "SILICONFLOW_API_KEY"}
    saved = {k: os.environ.get(k) for k in keys}
    for k, v in env.items():
        os.environ[k] = v
    try:
        importlib.reload(model)
        yield model
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(model)


# --------------------------- 本地 HTTP stub -------------------------------- #

class _StubHandler(BaseHTTPRequestHandler):
    """按请求体里的 stream 标志分流：True → SSE 流；False → 整段 JSON。
    mode="hang" 的实例永不回包（超时机制验证）。"""

    mode = "sse"          # "sse" | "json" | "hang"

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw or b"{}")
        except Exception:
            return {}

    def do_POST(self):
        body = self._read_body()
        if self.mode == "hang":
            time.sleep(180)                      # 永不回包；daemon 线程收尾
            return
        if body.get("stream"):
            self._send_sse()
        else:
            self._send_json()

    def _send_json(self):
        payload = {"id": "stub", "object": "chat.completion", "created": 0,
                   "model": "stub", "choices": [{"index": 0,
                   "message": {"role": "assistant", "content": "非流式回包"},
                   "finish_reason": "stop"}], "usage": {}}
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_sse(self):
        def _delta(d, finish=None):
            return {"choices": [{"index": 0, "delta": d, "finish_reason": finish}]}

        chunks = [
            _delta({"role": "assistant", "content": "你好"}),
            _delta({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                    "function": {"name": "add_clips",
                                                 "arguments": ""}}]}),
            _delta({"tool_calls": [{"index": 0,
                                    "function": {"arguments": '{"items":'}}]}),
            _delta({"tool_calls": [{"index": 0,
                                    "function": {"arguments": "[]}"}}]}),
            _delta({}, finish="tool_calls"),
        ]
        out = []
        for c in chunks:
            c.update(id="stub", object="chat.completion.chunk",
                     created=0, model="stub")
            out.append(f"data: {json.dumps(c, ensure_ascii=False)}\n\n".encode())
        out.append(b"data: [DONE]\n\n")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for part in out:
            self.wfile.write(part)
            self.wfile.flush()

    def log_message(self, *args):
        pass


def _serve(mode: str) -> tuple[ThreadingHTTPServer, str]:
    handler = type(f"_H_{mode}", (_StubHandler,), {"mode": mode})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1"


def _client(model_mod, base_url: str, **kw):
    params = dict(model="stub-model", api_key="stub-key", base_url=base_url,
                  temperature=0, timeout=10, max_retries=0, streaming=True)
    params.update(kw)
    return model_mod.StreamingChatOpenAI(**params)


# ------------------------------ 配置接线 ------------------------------------ #

def t01_defaults_streaming_60s_1retry():
    with model_env(SILICONFLOW_API_KEY="stub") as model:
        assert model.MODEL_STREAMING is True
        assert model.MODEL_TIMEOUT == 60.0 and model.MODEL_RETRIES == 1
        assert model.VLM_TIMEOUT == 60.0 and model.VLM_RETRIES == 1
        m = model.get_model()
        assert isinstance(m, model.StreamingChatOpenAI)
        assert m.streaming is True
        assert m.request_timeout == 60.0 and m.max_retries == 1
        v = model.get_vlm_model()
        assert v is not None and v.streaming is True
        assert v.request_timeout == 60.0 and v.max_retries == 1


def t02_rollback_model_streaming_0():
    with model_env(SILICONFLOW_API_KEY="stub", MODEL_STREAMING="0") as model:
        # 回滚杆：非流式语义下 timeout 默认值必须自动回到「整段生成时长」预算
        assert model.MODEL_TIMEOUT == 180.0
        assert model.VLM_TIMEOUT == 120.0
        assert model.get_model().streaming is False


def t03_env_overrides_respected():
    with model_env(SILICONFLOW_API_KEY="stub", MODEL_TIMEOUT="999",
                   MODEL_RETRIES="7", VLM_TIMEOUT="123", VLM_RETRIES="0") as model:
        m = model.get_model()
        assert m.request_timeout == 999.0 and m.max_retries == 7
        v = model.get_vlm_model()
        assert v.request_timeout == 123.0 and v.max_retries == 0


def t04_init_chat_model_path_passes_timeout():
    with model_env(SILICONFLOW_API_KEY="stub",
                   DEEPAGENT_MODEL="openai:gpt-4o-mini") as model:
        import langchain.chat_models as lcm
        captured = {}

        def _fake(model_str, **kwargs):
            captured["model"], captured["kwargs"] = model_str, kwargs
            return object()

        orig = lcm.init_chat_model
        lcm.init_chat_model = _fake
        try:
            model.get_model()
        finally:
            lcm.init_chat_model = orig
        # 补洞断言：显式路径必须带上超时/重试（此前完全裸奔）
        assert captured["model"] == "openai:gpt-4o-mini"
        assert captured["kwargs"].get("timeout") == model.MODEL_TIMEOUT
        assert captured["kwargs"].get("max_retries") == model.MODEL_RETRIES


# ------------------------------ 机制验证 ------------------------------------ #

def t05_hang_times_out_within_budget():
    """挂死 stub：流式 read timeout 在预算内触发（对照 2026-10-28 的 541.6s）。"""
    import model
    srv, base = _serve("hang")
    try:
        with _no_proxy():
            # 单次：timeout=2s，零重试 → ~2s 必须抛 Timeout
            c = _client(model, base, timeout=2, max_retries=0)
            t0 = time.perf_counter()
            try:
                c.invoke([HumanMessage("hi")])
                raise AssertionError("挂死请求竟然成功返回")
            except Exception as exc:
                assert "Timeout" in type(exc).__name__, \
                    f"应为超时异常，实为 {type(exc).__name__}: {exc}"
            took = time.perf_counter() - t0
            assert 1.5 <= took <= 8, f"单次超时耗时 {took:.1f}s 超出预算"

            # 重试放大器：max_retries=1 → 2 次尝试 ≈ 2×2s+退避，仍远小于 3×180s
            c = _client(model, base, timeout=2, max_retries=1)
            t0 = time.perf_counter()
            try:
                c.invoke([HumanMessage("hi")])
                raise AssertionError("挂死请求竟然成功返回")
            except Exception as exc:
                assert "Timeout" in type(exc).__name__
            took = time.perf_counter() - t0
            assert 3.5 <= took <= 15, f"重试后总耗时 {took:.1f}s 超出预算"
    finally:
        srv.shutdown()


def t06_streaming_invoke_aggregates_tool_calls():
    """SSE stub：.invoke() 流式聚合后 content/tool_calls 与整段回包等价
    （langchain-openai 1.6.0 需要子类恢复该行为，这是 agent 链路的正确性闸门）。"""
    import model
    srv, base = _serve("sse")
    try:
        with _no_proxy():
            msg = _client(model, base).invoke([HumanMessage("hi")])
            assert msg.content == "你好", f"content 聚合错: {msg.content!r}"
            assert len(msg.tool_calls) == 1, f"tool_calls 数量错: {msg.tool_calls}"
            tc = msg.tool_calls[0]
            assert tc["name"] == "add_clips" and tc["id"] == "call_1"
            assert tc["args"] == {"items": []}, f"增量参数聚合错: {tc['args']}"

            # 回滚杆：streaming=False 走原生非流式分支，同样能拿到结果
            msg2 = _client(model, base, streaming=False).invoke(
                [HumanMessage("hi")])
            assert msg2.content == "非流式回包"
    finally:
        srv.shutdown()


def t07_stream_hook_fires_once_per_request():
    import model
    srv, base = _serve("sse")
    fired = []
    remove = model.add_stream_hook(lambda: fired.append(1))
    try:
        with _no_proxy():
            _client(model, base).invoke([HumanMessage("hi")])
            assert len(fired) == 1, f"首 chunk 回调应恰好一次，实为 {len(fired)}"
            _client(model, base).invoke([HumanMessage("hi")])
            assert len(fired) == 2, "每个请求的首 chunk 各触发一次"
    finally:
        remove()
    # 注销后不再触发；回调抛异常不影响生成主链路
    remove2 = model.add_stream_hook(
        lambda: (_ for _ in ()).throw(RuntimeError("hook boom")))
    try:
        with _no_proxy():
            _client(model, base).invoke([HumanMessage("hi")])
            assert len(fired) == 2, "已注销的回调不应再触发"
    finally:
        remove2()
        srv.shutdown()


if __name__ == "__main__":
    for name, fn in sorted((k, v) for k, v in globals().items()
                           if k.startswith("t") and callable(v)):
        fn()
        print(f"ok  {name}")
    print("ALL PASS")
