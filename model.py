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
