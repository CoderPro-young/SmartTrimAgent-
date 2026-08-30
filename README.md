# SmartTrimAgent

基于 **deepagents**（LangChain 的 deep agent 框架）构建的**视频处理与剪辑 Agent**——
用自然语言描述你的需求，它自动完成视频的转码、拼接、裁剪、转场、画中画、花字等处理。

核心设计：**LLM 负责"意图"，编译器负责"工程正确性"**。LLM 把用户需求翻译成一份结构化的
「编辑计划 JSON」，再由程序把它编译成确定性的 ffmpeg 命令序列执行并回验产物——避免 LLM
直接手写多输入的 `filter_complex` 时频繁出错。

## 功能特性

- **自然语言驱动**：一句话描述剪辑需求，无需手写 ffmpeg 命令
- **多素材拼接**：自动把异构素材（mp4 / avi / 图片、不同分辨率 / 编码 / 帧率 / 声道）归一化成统一中间格式再拼接
- **裁剪与转场**：支持剪掉片段头尾、片段间转场（fade / dissolve / 各种 wipe 等）
- **画中画 + 花字**：在任意素材的指定时间点叠加小窗画面或文字
- **计划校验 + 时间轴数学**：转场重叠导致的总时长缩短、overlay 绝对时间，全部由程序自动计算
- **产物回验**：用 ffprobe 校验输出时长 / 分辨率 / 编码是否符合预期

## 工作流

```
用户自然语言
   → Agent（LLM）：探测素材 → 提交「编辑计划 JSON」
   → plan_compiler：校验 → 换算时间轴 → 生成 ffmpeg 命令 → 执行 → 回验
   → OUTPUT/ 产物
```

## 环境要求

- Python 3.13+（依赖 `deepagents`）
- 一个 LLM 模型接口：默认走 SiliconFlow（OpenAI 兼容），也支持任意 OpenAI / Anthropic 兼容端点
- ffmpeg（**可选**）：真实渲染需要，未安装时仍可验证「计划 → 命令生成」全链路

## 安装

```bash
pip install "deepagents==0.7.11" langchain-openai langchain-anthropic python-dotenv
```

## 快速开始

```bash
# 1) 配置：复制模板为 .env，填入模型 AK（.env 已被 gitignore，不会提交）
cp .env.example .env

# 2) 运行默认演示（多素材拼接 + 转场 + 画中画 + 花字）
python video_demo.py

# 3) 或用自然语言自定义任务
python video_demo.py "把 sample.mp4 前 8 秒和 test.png(3秒) 用 fade 转场拼接，输出 720p"
```

> 安装 ffmpeg 后即可真实渲染：Windows `winget install ffmpeg` / macOS `brew install ffmpeg` /
> Ubuntu `sudo apt install ffmpeg`。

## 使用示例

```bash
python video_demo.py "把 INPUT/sample.mp4 转成 720p，水平镜像"
python video_demo.py "把 a.mp4 和 b.avi 拼接，中间加 0.5 秒 dissolve 转场"
python video_demo.py "在 sample.mp4 第 3 秒加一个右下角画中画 logo.png，持续 5 秒"
python video_demo.py "把 sample.mp4 前 5 秒剪掉，剩下部分加一行花字"
```

运行过程会打印：LLM 生成的编辑计划 JSON、编译器换算出的时间轴（片段时长 / 起点 / 总时长）、
以及完整的 ffmpeg 命令序列，方便验证与调试。

## 目录结构

```
deepagent-demo/
├── video_demo.py        # 演示入口：任务 → 计划 → 编译 → 命令展示 → 执行回验
├── video_agent.py       # Agent 构建：probe_media / submit_plan 工具 + 计划 schema 提示词
├── plan_schema.py       # 编辑计划 JSON 的校验（白名单 + 语义规则）
├── plan_compiler.py     # 编译器：校验 → 换算 → 生成命令 → 执行 → 回验
├── ffmpeg_exec.py       # ffmpeg/ffprobe 执行器（未安装时返回可读错误）
├── model.py             # 模型解析（默认 deepseek-ai/DeepSeek-V4-Flash）
├── main.py / agent.py   # 基础编码 Agent demo（对照）
├── INPUT/               # 素材（sample.mp4 / test.png / logo.png）
├── OUTPUT/              # 产物（运行时生成）
├── TMP/                 # 归一化中间件（运行时生成，可缓存）
└── docs/                # 技术方案文档
```

## 模型与追踪配置

所有配置通过 `.env` 导入（`model.py` 启动时用 python-dotenv 加载，命令行 export 的值优先）。
模型解析优先级（`model.py`）：显式 `DEEPAGENT_MODEL=provider:model` >
`SILICONFLOW_API_KEY`(+`MODEL_NAME`) > `OPENAI_API_KEY`(+`OPENAI_BASE_URL`/`OPENAI_MODEL`) >
`ANTHROPIC_API_KEY`(+`ANTHROPIC_BASE_URL`/`ANTHROPIC_MODEL`)。

可选开启 LangSmith 追踪，在 `.env` 里加：

```
LANGSMITH_TRACING=true
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
LANGSMITH_API_KEY=lsv2_pt_xxxxxxxx
LANGSMITH_PROJECT=DeepAgentDemo
```

打开 [smith.langchain.com](https://smith.langchain.com) 对应项目即可看到每次运行的完整
调用链（工具调用、prompt、token 用量）。

> 模型选型：`Qwen/Qwen2.5-7B-Instruct` 免费但工具调用不稳定；推荐
> `deepseek-ai/DeepSeek-V4-Flash` 或 `Qwen/Qwen2.5-72B-Instruct`。

## 技术文档

- [docs/v2.0-overview.md](docs/v2.0-overview.md) —— 整体方案（workflow + 模块职责，简明）
- [docs/v2.0-multi-material-editing.md](docs/v2.0-multi-material-editing.md) —— v2 详细设计（schema / 时间轴数学 / 扩展点）
- [docs/v1.0-architecture.md](docs/v1.0-architecture.md) —— v1 单命令版（对照）

## 已知限制 / 路线图

- 转场处音频当前为硬切（v2.1 计划加入 acrossfade 交叉淡变）
- 画布策略当前为 pad 黑边，blur-fill / crop-fill 留 v2.1
- 计划 schema 已预留三个扩展点（clip 效果 / overlay 区域 / timeline 布局块），新增能力无需重构
