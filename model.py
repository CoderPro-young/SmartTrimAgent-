"""Model resolution for the deepagents demo.

Supports several ways to pick a model, in priority order:

1. `DEEPAGENT_MODEL=provider:model`        (explicit, highest priority)
2. `SILICONFLOW_API_KEY` (+ `MODEL_NAME`)  SiliconFlow OpenAI-compatible API
3. OpenAI-compatible:  `OPENAI_API_KEY` (+ optional `OPENAI_BASE_URL`, `OPENAI_MODEL`)
4. Anthropic-compatible: `ANTHROPIC_API_KEY` (+ optional `ANTHROPIC_BASE_URL`, `ANTHROPIC_MODEL`)

Returns a LangChain `BaseChatModel` ready to pass to `create_deep_agent`.
"""

from __future__ import annotations

import os
import time
from typing import Callable

from dotenv import load_dotenv
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.chat_models import generate_from_stream
from langchain_openai import ChatOpenAI

# 加载项目根目录的 .env（AK / LangSmith 等配置，模板见 .env.example）。
# 已存在的环境变量不会被覆盖，所以命令行 export 的值优先于 .env。
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

try:
    # 埋点模块在仓库根；sys.path 无仓库根的独立环境下静默降级为 no-op
    import perf_trace
except ImportError:         # pragma: no cover
    from contextlib import nullcontext
    import types as _types
    perf_trace = _types.ModuleType("perf_trace")
    perf_trace.current = lambda: None
    perf_trace.span = lambda *a, **k: nullcontext()
    perf_trace.event = lambda *a, **k: None
    perf_trace.start = lambda *a, **k: None
    perf_trace.set_ctx = lambda **k: None
    perf_trace.finish_current = lambda *a, **k: None

# ------------------- 超时 / 重试 / 流式（2026-10-28 链测定性后收紧） -------- #
# 历史：2026-10-11 加了单请求超时（180s）+ SDK 重试 2 次；2026-10-28 链测
# （docs/dev-notes-2026-10-28.md §七）实测 541.6s ≈ 3×180——超时其实每次都
# 触发了，是 openai SDK 静默重试把逻辑调用放大成 timeout×(1+retries)，期间
# 上层完全无感。根因：非流式请求的 read timeout 必须 ≥ 整个生成时长（合法
# 大工具轮 30~80s），timeout 不敢压小，挂死请求只能陪它挂满。
#
# 修法（L2）：默认走流式 HTTP（MODEL_STREAMING）。流式下 httpx 的 read
# timeout 按 chunk 间隔生效——挂死请求在首 token 前后 ~MODEL_TIMEOUT 即断，
# 健康的慢生成只要 chunk 持续到达就不受整段预算约束，timeout 才敢压到 60。
# 回滚杠杆：MODEL_STREAMING=0 恢复非流式（timeout 默认值自动回到 180）。
# 注意在 load_dotenv 之后取值，.env 里配的值才能生效。
MODEL_STREAMING = os.environ.get("MODEL_STREAMING", "1") != "0"
MODEL_TIMEOUT = float(os.environ.get("MODEL_TIMEOUT")
                      or (60 if MODEL_STREAMING else 180))
# SDK 静默重试 2→1：多一次重试对瞬时抖动的价值，抵不过「挂死 × 3」的放大；
# 上层 plan_with_retry 本来就会带反馈重跑（2026-10-28 15:01 实测兜得住）。
MODEL_RETRIES = int(os.environ.get("MODEL_RETRIES", "1"))
# VLM 打标（2026-09-24 实测 15~30s/文件）；同样受 3×120=362s×4 并发放大
# （2026-10-28 20:41 实测），与主模型同修。
VLM_TIMEOUT = float(os.environ.get("VLM_TIMEOUT")
                    or (60 if MODEL_STREAMING else 120))
VLM_RETRIES = int(os.environ.get("VLM_RETRIES", "1"))

# 显式输出预算（max_tokens）。2026-10-26 事故：不设时 SiliconFlow 默认按模型
# 上限（GLM-4.5V 为 65536）填充，而校验要求 prompt+max_tokens ≤ max_seq_len，
# 输出预算占满整个窗口 → 打标请求 100% 被 400 拒绝。显式设小后 prompt 才有
# 可用空间。打标输出只是几百 token 的 JSON，8192 富余；主模型留更大余量给
# 长计划 + 推理。注意在 load_dotenv 之后取值。
_DISABLE_SET = {"0", "false", "off", "no", ""}
MODEL_MAX_TOKENS = int(os.environ.get("MODEL_MAX_TOKENS", "16384"))
VLM_MAX_TOKENS = int(os.environ.get("VLM_MAX_TOKENS", "8192"))
# VLM thinking 开关（perf 待办 V1）。GLM-4.5V 这类混合推理模型默认开 thinking，
# 打标请求实测会把 63k token 的思考预算烧满（961s/笔还失败，max_tokens 管不住）；
# 打标只要几百 token 的结构化 JSON，默认关。VLM_THINKING=1 可改回开启对照。
VLM_THINKING = os.environ.get("VLM_THINKING", "0") not in _DISABLE_SET


# ----------------------- 流式客户端（L2 核心） ------------------------------ #

# 每个 LLM 请求收到首个 chunk 时触发的回调（L3 可观测：区分「纯挂死」与
# 「生成中」）。回调异常静默吞掉——可观测性钩子绝不影响生成主链路。
_STREAM_HOOKS: list[Callable[[], None]] = []


def add_stream_hook(fn: Callable[[], None]) -> Callable[[], None]:
    """注册首 chunk 回调，返回注销函数。服务端在 agent 期注册/注销。"""
    _STREAM_HOOKS.append(fn)

    def _remove() -> None:
        try:
            _STREAM_HOOKS.remove(fn)
        except ValueError:
            pass

    return _remove


def _prompt_size(messages) -> tuple[int, int]:
    """粗估请求体量：(文本字符数, 图片张数)。base64 图片不计入字符数——
    计入会让 prompt_chars 虚高几个数量级，失去参考价值；张数单列。"""
    chars = 0
    images = 0
    for m in messages or []:
        c = getattr(m, "content", "")
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict):
                    if b.get("type") == "text":
                        chars += len(b.get("text") or "")
                    elif b.get("type") == "image_url":
                        images += 1
                elif isinstance(b, str):
                    chars += len(b)
    return chars, images


class StreamingChatOpenAI(ChatOpenAI):
    """streaming=True 时让 invoke()/ainvoke() 内部也走流式 HTTP。

    当前 langchain-core 1.6.1 的 _generate_with_cache 已经会按实例属性
    streaming=True 把 .invoke() 路由到 _stream（_should_stream 的 affirmative
    分支），但该路径不转发 run_manager（token 回调不触发），且老版本
    langchain-openai 的 _generate 无流式分支——子类的 _generate 覆盖保证
    任何版本/路径下「streaming=True ⇒ .invoke() 走流式并聚合」，聚合用
    generate_from_stream（与 .stream() 同一条 chunk 合并逻辑，tool_calls
    增量照常聚合）。_stream 上的首 chunk hook 是 L3 可观测的唯一咽喉点。
    """

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if not self.streaming:
            tr = perf_trace.current()
            if tr is None:
                return super()._generate(messages, stop=stop,
                                         run_manager=run_manager, **kwargs)
            chars, images = _prompt_size(messages)
            with tr.span("llm", kind="llm", streaming=False,
                         model=getattr(self, "model_name", "") or "",
                         prompt_chars=chars, n_images=images) as s:
                res = super()._generate(messages, stop=stop,
                                        run_manager=run_manager, **kwargs)
                if s is not None:
                    try:
                        um = getattr(res.generations[0].message,
                                     "usage_metadata", None)
                        if um:
                            s.meta["usage"] = um
                    except (IndexError, AttributeError):
                        pass
                return res
        return generate_from_stream(iter(self._stream(
            messages, stop=stop, run_manager=run_manager, **kwargs)))

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        if not self.streaming:
            return await super()._agenerate(messages, stop=stop,
                                            run_manager=run_manager, **kwargs)
        chunks = [chunk async for chunk in self._astream(
            messages, stop=stop, run_manager=run_manager, **kwargs)]
        return generate_from_stream(iter(chunks))

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        """带埋点的流式：一条逻辑请求一条 llm span（TTFT/块数/输出量/usage）。

        streaming=True 时 .invoke() 经 _generate → generate_from_stream 也走
        这里，所以埋这一个生成器即覆盖全部同步路径。_agenerate/_astream 不埋
        （生产链路全同步；异步调用时无 trace 侧画像，属可接受盲区）。
        """
        tr = perf_trace.current()
        if tr is None:
            yield from self._stream_untracked(messages, stop, run_manager,
                                              **kwargs)
            return
        chars, images = _prompt_size(messages)
        with tr.span("llm", kind="llm", streaming=True,
                     model=getattr(self, "model_name", "") or "",
                     prompt_chars=chars, n_images=images) as s:
            n_chunks = 0
            out_chars = 0
            usage = None
            try:
                for chunk in self._stream_untracked(messages, stop, run_manager,
                                                    **kwargs):
                    if n_chunks == 0 and s is not None:
                        # 首 chunk = TTFT（相对 span 起点即请求发起点）
                        s.meta["ttft_s"] = round(time.perf_counter() - s.t0, 3)
                    n_chunks += 1
                    # 流式产出的是 ChatGenerationChunk，文本在 .message 上
                    # （2026-11-01 实测：直接读 .content 会 AttributeError）
                    msg = getattr(chunk, "message", None)
                    c = getattr(msg, "content", "") or ""
                    if isinstance(c, str):
                        out_chars += len(c)
                    elif isinstance(c, list):
                        out_chars += sum(len(b.get("text") or "")
                                         for b in c if isinstance(b, dict))
                    um = getattr(msg, "usage_metadata", None)
                    if um:
                        usage = um
                    yield chunk
            finally:
                if s is not None:
                    s.meta["n_chunks"] = n_chunks
                    s.meta["out_chars"] = out_chars
                    if usage:
                        s.meta["usage"] = usage

    def _stream_untracked(self, messages, stop=None, run_manager=None, **kwargs):
        """原 _stream 逻辑（首 chunk hook 链），埋点壳的内层。"""
        first = True
        for chunk in super()._stream(messages, stop=stop,
                                     run_manager=run_manager, **kwargs):
            if first:
                first = False
                for hook in list(_STREAM_HOOKS):
                    try:
                        hook()
                    except Exception:
                        pass
            yield chunk


def get_model() -> BaseChatModel:
    """按优先级解析模型配置，返回可直接传给 create_deep_agent 的模型实例。"""

    # 1) 显式指定 provider:model，如 DEEPAGENT_MODEL=openai:gpt-4o-mini
    explicit = os.environ.get("DEEPAGENT_MODEL")
    if explicit:
        from langchain.chat_models import init_chat_model

        # 超时/重试必须带上——init_chat_model 不会继承下面的默认值，漏了
        # 这条路径就完全裸奔（2026-10-28 补的洞）。timeout/max_retries 是
        # ChatOpenAI / ChatAnthropic 共有的构造参数。流式语义因供应商而异，
        # 显式路径不启用（MODEL_STREAMING 只作用于本项目默认两条路线）。
        return init_chat_model(explicit, timeout=MODEL_TIMEOUT,
                               max_retries=MODEL_RETRIES)

    # 2) SiliconFlow（硅基流动）— OpenAI 兼容端点，本项目的默认路线
    sf_key = os.environ.get("SILICONFLOW_API_KEY")
    if sf_key:
        return StreamingChatOpenAI(
            model=os.environ.get("MODEL_NAME", "deepseek-ai/DeepSeek-V4-Flash"),
            api_key=sf_key,
            base_url=os.environ.get(
                "SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"
            ),
            temperature=0,
            timeout=MODEL_TIMEOUT,
            max_retries=MODEL_RETRIES,
            streaming=MODEL_STREAMING,
            # 注意：ChatOpenAI 的 max_tokens 参数（含 model_kwargs 里的同名键）
            # 都会被 langchain-openai 改写成 max_completion_tokens 发出，而
            # SiliconFlow 不认后者（静默忽略、仍按模型上限填 max_tokens → 400）。
            # extra_body 走 openai SDK 层直接合并进请求体，绕过参数改写
            # （2026-10-26 事故，真机验证）。
            extra_body={"max_tokens": MODEL_MAX_TOKENS},
        )

    # 3) 任意 OpenAI 兼容端点（OpenAI / DeepSeek / 本地 vLLM 等）
    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key:
        return StreamingChatOpenAI(
            model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            api_key=openai_key,
            base_url=os.environ.get("OPENAI_BASE_URL"),
            temperature=0,
            timeout=MODEL_TIMEOUT,
            max_retries=MODEL_RETRIES,
            streaming=MODEL_STREAMING,
        )

    # 4) Anthropic 兼容端点（Claude / DashScope Qwen 等）
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if anthropic_key:
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
            api_key=anthropic_key,
            base_url=os.environ.get("ANTHROPIC_BASE_URL"),
            temperature=0,
        )

    raise RuntimeError(
        "No model configured. Set SILICONFLOW_API_KEY / OPENAI_API_KEY / "
        "ANTHROPIC_API_KEY, or DEEPAGENT_MODEL=provider:model. See .env.example."
    )


# ----------------------- V4 感知层：VLM 客户端 ----------------------------- #

# 2026-09-24 实测 SiliconFlow /v1/models：GLM-4.6V 未上架。对两段真实素材做
# A/B 打标（TMP/_vlm_realtest.py，记录见 docs/v4.0-multimodal-perception.md）：
#   zai-org/GLM-4.5V        场景/活动识别更准（溯溪/户外溪流），延迟 ~15-30s/文件
#   Qwen/Qwen3-VL-30B-A3B   tags 更丰富但场景偶有偏差，更快更便宜
# 打标精度直接影响"和朋友一起的时光"类筛选的一次通过率，默认取 GLM-4.5V。
DEFAULT_VLM_MODEL = "zai-org/GLM-4.5V"


def get_vlm_model():
    """V4 感知层专用 VLM（抽帧多图打标）。与规划模型完全独立，可单独换供应商。

    优先级：VLM_API_KEY（独立供应商，需配 VLM_BASE_URL）> 复用 SiliconFlow。
    两边都没有 key 时返回 None —— content_analysis 据此降级为纯 L1 信号
    （只有场景切分，无语义标签），链路不报错。

    约束：VLM 请求只是 content_analysis 内部的一次性调用（每批帧一个请求），
    图片永远不进入 agent 主循环的消息流（v3.1 D1 铁律：永不重放消息历史）。
    """
    if os.environ.get("VLM_API_KEY"):
        key = os.environ["VLM_API_KEY"]
        base = os.environ.get("VLM_BASE_URL")   # 独立供应商必须显式给端点
    elif os.environ.get("SILICONFLOW_API_KEY"):
        key = os.environ["SILICONFLOW_API_KEY"]
        base = os.environ.get("VLM_BASE_URL") or os.environ.get(
            "SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"
        )
    else:
        return None

    kwargs = dict(
        model=os.environ.get("VLM_MODEL", DEFAULT_VLM_MODEL),
        api_key=key,
        temperature=0,
        max_retries=VLM_RETRIES,
        timeout=VLM_TIMEOUT,
        streaming=MODEL_STREAMING,
        # 同 get_model：max_tokens 参数/model_kwargs 都会被改写成
        # max_completion_tokens，SiliconFlow 不认；extra_body 直接进请求体。
        # enable_thinking 走同一通道（perf 待办 V1：打标关 thinking）。
        extra_body={"max_tokens": VLM_MAX_TOKENS, "enable_thinking": VLM_THINKING},
    )
    if base:
        kwargs["base_url"] = base
    return StreamingChatOpenAI(**kwargs)
