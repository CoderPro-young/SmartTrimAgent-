# DeepAgents demo (under "Agent 应用" scene)

基于本机已安装的 **deepagents**（`pip` 包，v0.7.7）——LangChain 的 deep agent 框架，
自带工具套件（文件系统 + shell `execute` + 子 Agent）与规划/记忆中间件。

当前包含两个 Agent：

| Agent | 文件 | 能力 |
|---|---|---|
| 编码 Agent（基础 demo） | `main.py` / `agent.py` | 用 `write_file`/`read_file` 完成文件/代码任务 |
| **视频处理与剪辑 Agent（V2）** | `video_demo.py` / `video_agent.py` / `plan_compiler.py` / `plan_schema.py` / `ffmpeg_exec.py` | 自然语言 → 编辑计划 JSON → 编译器出 ffmpeg 命令 → 执行回验 |

## 视频处理与剪辑 Agent（V2）

LLM 产出「编辑计划 JSON」，`plan_compiler` 编译成 ffmpeg 命令序列（归一化 → 拼接/转场/叠加）
并执行回验。支持多素材拼接、裁剪、转场、画中画、花字，素材可异构（mp4/avi/图片）。

```bash
# 1) 配置模型（SiliconFlow，默认 deepseek-ai/DeepSeek-V4-Flash）
export SILICONFLOW_API_KEY=sk-...
export MODEL_NAME=deepseek-ai/DeepSeek-V4-Flash

# 2) 运行默认任务（多素材拼接 + 转场 + 画中画 + 花字）
python video_demo.py

# 或自定义自然语言指令
python video_demo.py "把 sample.mp4 前 8 秒和 test.png(3秒) 用 fade 拼接，720p"
```

目录约定：`INPUT/`（素材：`sample.mp4`、`test.png`、`logo.png`）、`OUTPUT/`（产物）、
`TMP/`（归一化中间件，可缓存）。

> ffmpeg 安装：Windows `winget install ffmpeg` / macOS `brew install ffmpeg` /
> Ubuntu `sudo apt install ffmpeg`。本机未装时，计划、换算、命令生成均可验证，
> 真实渲染需装好后重跑。

技术方案文档：
- [docs/v2.0-overview.md](docs/v2.0-overview.md) —— **整体方案（workflow + 模块职责，简明）**
- [docs/v2.0-multi-material-editing.md](docs/v2.0-multi-material-editing.md) —— V2 详细设计（schema / 时间轴数学 / 扩展点）
- [docs/v1.0-architecture.md](docs/v1.0-architecture.md) —— V1 单命令版（对照）

## 编码 Agent（基础 demo）

`main.py` 构建编码 Agent，任务：创建 `hello_deepagent.py` 并读回，演示内置
`write_file` / `read_file` 工具。

```bash
export SILICONFLOW_API_KEY=sk-...
export MODEL_NAME=deepseek-ai/DeepSeek-V4-Flash
python main.py
```

## 模型配置

模型解析优先级（`model.py`）：显式 `DEEPAGENT_MODEL=provider:model` >
`SILICONFLOW_API_KEY`(+`MODEL_NAME`，默认 `deepseek-ai/DeepSeek-V4-Flash`) >
`OPENAI_API_KEY`(+`OPENAI_BASE_URL`/`OPENAI_MODEL`) > `ANTHROPIC_API_KEY`。
完整模板见 `.env.example`。

> 模型选型备忘：`Qwen/Qwen2.5-7B-Instruct` 免费但 **tool-calling 不可靠**
> （生成畸形工具参数）；`Qwen/Qwen2.5-72B-Instruct`、`deepseek-ai/DeepSeek-V4-Flash`
> 工具调用稳定。SiliconFlow 免费模型需账户有余额/实名，否则返回
> `402 insufficient balance`。

## 依赖

```bash
pip install "deepagents==0.7.7" langchain-openai langchain-anthropic
```

## 关键实现细节

- `agent.py` / `video_agent.py` 均传入 **`LocalShellBackend(root_dir=...)`**：
  默认 `StateBackend` 是内存态、文件不落盘；`LocalShellBackend` 让文件写入与
  shell 命令真实作用于磁盘目录。
- 编译器两条渲染路径：无转场走 concat `-c copy`（快速、零损耗）；有转场走
  filter_complex 链式 xfade（重编码）。时间轴数学（xfade offset、overlay 绝对时间、
  总时长 D）由 `plan_compiler._derive` 程序计算，LLM 不碰绝对秒数。
- ffmpeg 未安装时，`ffmpeg_exec` 返回可读错误 + 命令原文，验证不受阻。
