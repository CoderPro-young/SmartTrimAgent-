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

from dotenv import load_dotenv
from langchain_core.language_models import BaseChatModel

# 加载项目根目录的 .env（AK / LangSmith 等配置，模板见 .env.example）。
# 已存在的环境变量不会被覆盖，所以命令行 export 的值优先于 .env。
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# 主模型单次请求超时（秒）与 SDK 级重试次数，可用环境变量覆盖。
# 2026-10-11 线上实测：不设超时的话，一次挂起的请求能无声吊 10+ 分钟（前端零反馈）；
# 超时后由 openai SDK 指数退避自动重试，重试耗尽才把异常抛给上层 plan_with_retry。
# 注意在 load_dotenv 之后取值，.env 里配的 MODEL_TIMEOUT 才能生效。
MODEL_TIMEOUT = float(os.environ.get("MODEL_TIMEOUT", "180"))
MODEL_RETRIES = int(os.environ.get("MODEL_RETRIES", "2"))

# 显式输出预算（max_tokens）。2026-10-26 事故：不设时 SiliconFlow 默认按模型
# 上限（GLM-4.5V 为 65536）填充，而校验要求 prompt+max_tokens ≤ max_seq_len，
# 输出预算占满整个窗口 → 打标请求 100% 被 400 拒绝。显式设小后 prompt 才有
# 可用空间。打标输出只是几百 token 的 JSON，8192 富余；主模型留更大余量给
# 长计划 + 推理。注意在 load_dotenv 之后取值。
MODEL_MAX_TOKENS = int(os.environ.get("MODEL_MAX_TOKENS", "16384"))
VLM_MAX_TOKENS = int(os.environ.get("VLM_MAX_TOKENS", "8192"))


def get_model() -> BaseChatModel:
    """按优先级解析模型配置，返回可直接传给 create_deep_agent 的模型实例。"""

    # 1) 显式指定 provider:model，如 DEEPAGENT_MODEL=openai:gpt-4o-mini
    explicit = os.environ.get("DEEPAGENT_MODEL")
    if explicit:
        from langchain.chat_models import init_chat_model

        return init_chat_model(explicit)

    # 2) SiliconFlow（硅基流动）— OpenAI 兼容端点，本项目的默认路线
    sf_key = os.environ.get("SILICONFLOW_API_KEY")
    if sf_key:
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=os.environ.get("MODEL_NAME", "deepseek-ai/DeepSeek-V4-Flash"),
            api_key=sf_key,
            base_url=os.environ.get(
                "SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"
            ),
            temperature=0,
            timeout=MODEL_TIMEOUT,
            max_retries=MODEL_RETRIES,
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
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            api_key=openai_key,
            base_url=os.environ.get("OPENAI_BASE_URL"),
            temperature=0,
            timeout=MODEL_TIMEOUT,
            max_retries=MODEL_RETRIES,
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

    from langchain_openai import ChatOpenAI

    kwargs = dict(
        model=os.environ.get("VLM_MODEL", DEFAULT_VLM_MODEL),
        api_key=key,
        temperature=0,
        max_retries=2,
        timeout=120,
        # 同 get_model：max_tokens 参数/model_kwargs 都会被改写成
        # max_completion_tokens，SiliconFlow 不认；extra_body 直接进请求体。
        extra_body={"max_tokens": VLM_MAX_TOKENS},
    )
    if base:
        kwargs["base_url"] = base
    return ChatOpenAI(**kwargs)
