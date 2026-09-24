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
    )
    if base:
        kwargs["base_url"] = base
    return ChatOpenAI(**kwargs)
