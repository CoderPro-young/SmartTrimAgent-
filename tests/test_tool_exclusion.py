"""2026-10-26 事故修复的离线单测。

事故链：未设 max_tokens → SiliconFlow 按模型上限填充（prompt+65536>65536）→
GLM-4.5V 打标 100% 被 400 → 2 素材缺语义标签 → agent 用内置 read_file 读
INPUT 下的 jpg → 整图 base64 进 ToolMessage → 纯文本主模型 400、整轮作废。

覆盖两处修复：
1. model.py 两个工厂显式 max_tokens（env 可调）；
2. HarnessProfile 排除文件系统内置工具（read/write/edit/glob/grep/execute，
   `ls` 保留）——媒体字节永不进 agent 消息流的宿主强制兜底。

运行：.venv/Scripts/python.exe tests/test_tool_exclusion.py
"""

from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "video_editing"))

_EXCLUDED = {"read_file", "write_file", "edit_file", "glob", "grep", "execute"}


def t01_max_tokens_constants():
    import model
    assert model.VLM_MAX_TOKENS == int(os.environ.get("VLM_MAX_TOKENS", "8192"))
    assert model.MODEL_MAX_TOKENS == int(os.environ.get("MODEL_MAX_TOKENS", "16384"))


def t02_model_factories_set_max_tokens():
    # 断言线上载荷：langchain-openai 会把 max_tokens 参数（含 model_kwargs 同名键）
    # 改写成 max_completion_tokens，SiliconFlow 不认后者——所以工厂必须走
    # extra_body（openai SDK 层合并进请求体，绕过参数改写）。
    import model
    from langchain_core.messages import HumanMessage
    if not os.environ.get("SILICONFLOW_API_KEY"):
        print("  跳过（无 SILICONFLOW_API_KEY）")
        return

    def _wire_max_tokens(m):
        payload = m._get_request_payload([HumanMessage("hi")], stop=None)
        assert "max_completion_tokens" not in payload, \
            f"不应出现被 SiliconFlow 忽略的 max_completion_tokens: {payload.get('max_completion_tokens')}"
        return (payload.get("extra_body") or {}).get("max_tokens")

    main = model.get_model()
    got = _wire_max_tokens(main)
    assert got == model.MODEL_MAX_TOKENS, f"主模型载荷 max_tokens={got}"
    vlm = model.get_vlm_model()
    assert vlm is not None
    got = _wire_max_tokens(vlm)
    assert got == model.VLM_MAX_TOKENS, f"VLM 载荷 max_tokens={got}"


def t03_harness_profile_excludes_fs_tools():
    import video_agent  # noqa: F401 —— 导入即完成 profile 注册
    from deepagents.profiles.harness.harness_profiles import _harness_profile_for_model
    from model import get_model
    if not os.environ.get("SILICONFLOW_API_KEY"):
        print("  跳过（无 SILICONFLOW_API_KEY）")
        return
    # create_deep_agent 构建时走的就是这个查找（graph.py 内部同款调用）
    prof = _harness_profile_for_model(get_model(), None)
    missing = _EXCLUDED - set(prof.excluded_tools)
    assert not missing, f"profile 未排除: {missing}"
    assert "ls" not in prof.excluded_tools, "ls 应保留"


def t04_agent_builds_and_domain_tools_disjoint():
    import plan_tools
    import video_agent as va
    va.build_video_agent_v2()   # 构建成功即可，不发请求
    names = {t.name for t in plan_tools.PLAN_TOOLS}
    clash = names & _EXCLUDED
    assert not clash, f"领域工具与被排除内置工具重名: {clash}"


if __name__ == "__main__":
    t01_max_tokens_constants()
    print("ok  t01_max_tokens_constants")
    t02_model_factories_set_max_tokens()
    print("ok  t02_model_factories_set_max_tokens")
    t03_harness_profile_excludes_fs_tools()
    print("ok  t03_harness_profile_excludes_fs_tools")
    t04_agent_builds_and_domain_tools_disjoint()
    print("ok  t04_agent_builds_and_domain_tools_disjoint")
    print("ALL PASS")
