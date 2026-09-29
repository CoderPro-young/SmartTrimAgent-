# SmartTrimAgent

基于 **deepagents**（LangChain 的 deep agent 框架）构建的**视频处理与剪辑 Agent**——
用自然语言描述你的需求，它自动完成视频的转码、拼接、裁剪、转场、画中画、花字等处理。

核心设计：**LLM 负责"意图"，编译器负责"工程正确性"**。LLM 把用户需求翻译成一份结构化的
「编辑计划 JSON」，再由程序把它编译成确定性的 ffmpeg 命令序列执行并回验产物——避免 LLM
直接手写多输入的 `filter_complex` 时频繁出错。

## 功能特性

- **自然语言驱动**：一句话描述剪辑需求，无需手写 ffmpeg 命令
- **粗剪 · 废段剪除（V5）**：`cut_silence` / `cut_black` ——静音停顿、黑屏废段由
  ffmpeg silencedetect/blackdetect 确定性检测，编译期自动展开成保留片段（可调噪声门限 /
  最短静音 / 保留缓冲）
- **粗剪 · 批量镜头筛选（V5）**：`select` 筛选宏 ——按内容卡标签（场景 / 人数 / 画质 /
  静音比…）+ 时长预算，编译器从卡片确定性生成片段，模型不手抄时间区间
- **智能创作 · 一键成片（V7/V7.2）**：`smart_create` 工作流宏——编译器先确定性
  选材（粗筛废料 → 画质选材 → **最短 1.5s 门槛** → **跨素材轮转装填**，闪帧不入片、
  多素材都出镜），再把**选中的镜头序列**回喂模型**逐镜头写文案**，编译器把文案
  **逐片段绑定**烧录成底部字幕（也可在计划里自带 captions，数量对齐时同样逐段绑定）；
  镜头切分带护栏（<0.5s 碎段并入相邻段、平均粒度 <1.5s 判误切升阈值重试）；
  另有 `one_click_reel`（按关键词出集锦）/ `speech_clean`（口播去静音）内置工作流
- **多素材拼接**：自动把异构素材（mp4 / avi / 图片、不同分辨率 / 编码 / 帧率 / 声道）归一化成统一中间格式再拼接
- **多模态内容理解（V4）**：`analyze_media` 建立镜头级语义索引（人数 / 场景 / 活动 / 情绪 / 画质 +
  V5 静音/黑场信号）——"帮我剪和朋友一起的时光""只留有人的画面"这类语义任务可直接表达；
  结果缓存复用，无 VLM key 时自动降级为纯场景切分；**上传即索引**（V5，后台自动建卡）
- **裁剪与转场**：支持剪掉片段头尾、片段间转场（fade / dissolve / 各种 wipe 等）
- **画中画 + 花字**：在任意素材的指定时间点叠加小窗画面或文字
- **计划校验 + 时间轴数学**：转场重叠导致的总时长缩短、overlay 绝对时间，全部由程序自动计算
- **参数卡片回改 + 审阅报告（V5）**：出片后在网页卡片上直接改 trim / 删片段 / 调转场，
  「重新渲染」不经过大模型；每次出片附「本次做了什么」摘要（剪除明细 / 筛选命中 / 效果清单）
- **产物回验**：用 ffprobe 校验输出时长 / 分辨率 / 编码是否符合预期

## 工作流

```
用户自然语言
   → Agent（LLM）：探测素材 →（语义任务时）analyze_media 建立镜头索引 → 提交「编辑计划 JSON」
   → plan_compiler：校验 → 换算时间轴 → 生成 ffmpeg 命令 → 执行 → 回验
   → OUTPUT/ 产物
```

## 环境要求

- Python 3.13+（推荐用 `py -m venv .venv` 建项目专属环境）
- 一个 LLM 模型接口：默认走 SiliconFlow（OpenAI 兼容），也支持任意 OpenAI / Anthropic 兼容端点
- 一个视觉模型接口（**可选**，V4 内容理解用）：默认复用 SiliconFlow key，零配置；不配则降级为纯场景切分
- ffmpeg（**可选**）：真实渲染需要，未安装时仍可验证「计划 → 命令生成」全链路

## 安装

```bash
# 1) 创建并激活项目专属虚拟环境（推荐，避免多套 Python 混淆）
py -m venv .venv
.venv\Scripts\activate           # PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate

# 2) 安装依赖
pip install -r requirements.txt
```

> 不建虚拟环境时，直接 `pip install "deepagents==0.7.11" langchain-openai langchain-anthropic python-dotenv` 也可以。

## 快速开始

> **`INPUT/` 默认是空的**——素材由你自己提供（Web 界面拖拽上传，或手动放进 `INPUT/`）。
> 想看仓库原始的演示素材，用 `git checkout -- INPUT/` 取回（`sample.mp4` / `test.png` / `logo.png`）。

```bash
# 1) 配置：复制模板为 .env，填入模型 AK（.env 已被 gitignore，不会提交）
cp .env.example .env

# 2) 把自己的素材放进 INPUT/，然后用自然语言下任务
python video_editing/video_demo.py "把 INPUT/my.mp4 转成 720p，画面水平镜像，输出到 OUTPUT/out.mp4"
python video_editing/video_demo.py "把 INPUT/a.mp4 和 INPUT/b.mp4 拼接，中间加 0.5 秒 dissolve 转场"
```

不带参数运行 `python video_editing/video_demo.py` 会走「默认演示任务」（多素材拼接 + 转场 +
画中画 + 花字），它需要上面那三个示例素材；缺失时脚本会直接提示，不会让 agent 空转。

> 安装 ffmpeg 后即可真实渲染：Windows `winget install ffmpeg` / macOS `brew install ffmpeg` /
> Ubuntu `sudo apt install ffmpeg`。

## Web 入口

不想敲命令行的话，项目自带一个零第三方依赖的 Web 界面（只用 Python 标准库）：

```bash
.venv\Scripts\python.exe web/server.py            # 默认 http://127.0.0.1:8000
.venv\Scripts\python.exe web/server.py --port 9000 --max-upload-mb 2000
```

**素材全部从界面上传**（不预置任何默认素材）。输入区顶部可切换两种模式（V7.1，选择会记住）：
**✂️ 粗剪模式**（默认，筛镜头 / 剪静音 / 去黑屏等做减法的任务模板）与
**✨ 智能创作**（一键成片 / Vlog / 作品展示等模板，引导模型走 smart_create 出带文案字幕的成片）。
模式只影响模板与引导文案，任务自然语言里带什么意图就走什么链路。左边输入自然语言需求
（上方有任务模板一键填入），右边三个标签页：

- **素材库（V5，默认页）** —— 素材列表带**缩略图**与索引状态徽章；**上传即索引**：
  落盘后后台自动建立内容卡（含静音/黑场信号），不用等任务触发；
  **镜头库**：跨素材聚合全部镜头（缩略图卡 + 标签），按 有人/无人/画质/场景 筛选，
  点击镜头即在预览区**区间回放**（直接播原文件，播放钳制在镜头起止，不切片）
- **成品** —— 内置播放器直接预览 + 下载，附 ffprobe 回验结果（实际时长 vs 预期时长）；
  **审阅报告**（本次剪除多少静音 / 筛选命中哪些镜头 / 用了什么效果）；
  **只读时间线**（成片结构胶片条：片段缩略格宽∝时长、转场标记，点击回看源片段）
- **流水线（高级）** —— **参数卡片**：出片后的计划渲染成人类可读卡片（trim 可改、效果可删、
  片段可删/排序、转场与输出可调；JSON 折叠进「高级」），改完点「按修改重新渲染」
  走 `/api/replan` **不经过大模型**；另有时间轴换算 / ffmpeg 命令序列 / 执行日志 / 历史产物

素材区的几个约定：

- 上传落盘在 `INPUT/`（平铺），这样 agent 用 `ls INPUT/` 就能直接看到，不需要额外提示
- 一个素材都没有时，「运行」会被禁用并提示先上传；后端也会拦一道，不会让 agent 空转
- 与已存在的文件重名：自己上传过的直接覆盖（方便反复替换同一素材），非上传文件则自动加序号，
  绝不覆盖
- 素材名的「×」只能移除**网页上传过**的（白名单记在 `TMP/uploads.json`）；
  若运行环境禁止删除（回收站不可用），服务会自动退化为移动到 `TMP/removed/`，并在界面上说明
- 单文件上限默认 500MB，可用 `--max-upload-mb` 调整

比命令行版多一个能力：**计划校验失败时会把编译器的中文错误回传给 LLM 重新出计划**（最多 2 次），
这也是 `plan_schema` 设计里的原意。

> 服务默认只监听 `127.0.0.1`。媒体路由被限制在 `OUTPUT/` 目录内，不会把 `.env` 之类
> 项目文件暴露出去（上传接口也只写 `INPUT/`）；若要改成 `0.0.0.0` 对外提供服务，请自行加鉴权。

### 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | ffmpeg/ffprobe 状态、当前模型、上传策略 |
| GET | `/api/inputs?probe=1` | 列出 `INPUT/` 素材（探测信息、内容卡、索引状态） |
| GET | `/api/outputs` | 列出 `OUTPUT/` 已有产物 |
| POST | `/api/run` | 运行任务，NDJSON 事件流 |
| POST | `/api/replan` | 按编辑后的计划直接编译执行（不过 LLM），NDJSON 事件流 |
| POST | `/api/cancel` | 取消当前任务（终止 ffmpeg + 阶段检查点） |
| POST | `/api/upload?name=<文件名>` | 上传素材，请求体为裸二进制（上传后自动建索引） |
| POST | `/api/delete` | 移除素材，`{"name": "..."}` |
| GET | `/media/OUTPUT/<名字>` | 预览产物（支持 Range） |
| GET | `/input/<名字>` | 预览原始素材（支持 Range） |
| GET | `/thumb/<名字>?t=<秒>&h=<高>` | 素材任意时刻缩略图（-ss 快速抽帧，带缓存） |
| GET | `/probe-frame/<帧文件名>` | 内容卡镜头代表帧（白名单路由） |
| GET | `/static/<路径>` | 前端 ES modules（零构建） |

## 使用示例

素材换成你自己的（`INPUT/` 下的文件名）：

```bash
python video_editing/video_demo.py "把 INPUT/my.mp4 转成 720p，水平镜像"
python video_editing/video_demo.py "把 INPUT/a.mp4 和 INPUT/b.avi 拼接，中间加 0.5 秒 dissolve 转场"
python video_editing/video_demo.py "在 INPUT/my.mp4 第 3 秒加一个右下角画中画 INPUT/logo.png，持续 5 秒"
python video_editing/video_demo.py "把 INPUT/my.mp4 前 5 秒剪掉，剩下部分加一行花字「开头」"
python video_editing/video_demo.py "给 INPUT/my.mp4 里的人脸打码，输出到 OUTPUT/blur.mp4"
python video_editing/video_demo.py "帮我剪出和朋友一起的时光：从 INPUT/ 里挑多人聚在一起的镜头，拼成一段 15 秒左右的视频"
```

Web 界面里更省事：**点素材名**把 `INPUT/xxx` 插进输入框，再**点下面的短语**（转成 720p / 水平镜像 /
加 0.5 秒 fade 转场 / 加画中画 / 加花字 / 给人脸打码 …）拼成一句话。

运行过程会打印：LLM 生成的编辑计划 JSON、编译器换算出的时间轴（片段时长 / 起点 / 总时长）、
以及完整的 ffmpeg 命令序列，方便验证与调试。

## 目录结构

```
deepagent-demo/
├── video_editing/           # 视频剪辑 Agent（项目主体）
│   ├── video_demo.py        # 演示入口：任务 → 计划 → 编译 → 命令展示 → 执行回验
│   ├── video_agent.py       # Agent 构建：probe_media / analyze_media / submit_plan 工具 + 提示词
│   ├── content_analysis.py  # V4 感知层：场景切分 + VLM 打标 + 内容卡片 + sidecar 缓存
│   ├── plan_schema.py       # 编辑计划 JSON 的校验（白名单 + 语义规则）
│   ├── plan_compiler.py     # 编译器：校验 → 换算 → 生成命令 → 执行 → 回验
│   ├── face_mosaic.py       # 人脸检测打码（v3.0，OpenCV YuNet）
│   └── ffmpeg_exec.py       # ffmpeg/ffprobe 执行器（未安装时返回可读错误）
├── web/                     # Web 入口（标准库 HTTP server + 零构建模块化前端）
│   ├── server.py            # 路由：/api/* · /static/* · /thumb/* · /probe-frame/* · /media/* · /input/*
│   ├── index.html           # 布局骨架（素材库 / 成品 / 流水线 三标签）
│   └── static/              # ES modules（app/api/chat/materials/shots/pipeline/timeline/result/templates）
│   └── index.html           # 对话 + 素材上传 + 流水线检视 UI（NDJSON 事件流）
├── basic_demo/              # 基础编码 Agent demo（deepagents 最简用法对照）
│   ├── main.py              # 入口：演示写/读文件任务
│   └── agent.py             # 最简 deep agent 构建
├── tests/                   # 离线单测（content_analysis 等，全 mock 不联网）
├── model.py                 # 共用：加载 .env + 按优先级解析模型（含 V4 的 get_vlm_model）
├── INPUT/                   # 素材目录（默认空；Web 上传或手动放入）
├── OUTPUT/                  # 产物（运行时生成）
├── TMP/                     # 归一化中间件（运行时生成，可缓存）
└── docs/                    # 技术方案文档
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

### V4 感知层的 VLM 配置（可选）

内容理解（`analyze_media`）默认复用上面的 SiliconFlow key，零配置即可用。想单独调整时：

```
VLM_MODEL=zai-org/GLM-4.5V          # 打标默认（实测更准）；备选 Qwen/Qwen3-VL-30B-A3B-Instruct
VLM_FRAME_BUDGET=24                  # 每素材抽帧上限（成本闸，约几分钱/文件·一次性）
VLM_BATCH=6                          # 每次请求带几帧
# 换独立供应商时再配：
# VLM_API_KEY=...                    # 配了它就不再复用 SiliconFlow
# VLM_BASE_URL=...                   # 独立供应商必须显式给 OpenAI 兼容端点
```

详见 [docs/v4.0-multimodal-perception.md](docs/v4.0-multimodal-perception.md)。

## 技术文档

- [docs/v7.2-alignment-guards.md](docs/v7.2-alignment-guards.md) —— V7.2 感知粒度护栏 + 装填来源均衡 + 文案选材后对齐（实测驱动的迭代修复）
- [docs/v7.1-smart-create.md](docs/v7.1-smart-create.md) —— V7.1 双模式前端 + 智能创作（smart_create / 文案铺字幕烧录 / 无音轨现场探测修复）
- [docs/v7.0-oneclick-and-perf.md](docs/v7.0-oneclick-and-perf.md) —— V7 一键成片 workflow 宏 + 性能优化（VLM 并发 / 归一化内容寻址缓存）
- [docs/v6.0-roughcut-loop.md](docs/v6.0-roughcut-loop.md) —— V6 粗剪减法漏斗（粗筛报告/一键应用 / EDL·CSV 导出 / 筛选报告）
- [docs/v5.0-roughcut.md](docs/v5.0-roughcut.md) —— V5 粗剪内核与媒体化前端（静音/黑场信号 / select 宏 / 镜头库 / 参数卡片回改）
- [docs/v4.0-multimodal-perception.md](docs/v4.0-multimodal-perception.md) —— V4 多模态感知层（analyze_media / 内容卡片 / 实测记录）
- [docs/v3.1-multi-turn-interaction.md](docs/v3.1-multi-turn-interaction.md) —— 多轮交互与会话设计（V3.1）
- [docs/v2.0-overview.md](docs/v2.0-overview.md) —— 整体方案（workflow + 模块职责，简明）
- [docs/v2.0-multi-material-editing.md](docs/v2.0-multi-material-editing.md) —— v2 详细设计（schema / 时间轴数学 / 扩展点）
- [docs/v1.0-architecture.md](docs/v1.0-architecture.md) —— v1 单命令版（对照）

## 已知限制 / 路线图

- 语义筛选依赖 VLM 标签质量（静音/黑场信号已进卡片；笑声/欢呼声等语义级音频信号待 ASR，二期）
- 场景切分阈值 0.3 / 长镜头粒度 12s 为经验值，真实素材漏切或碎切时再调
- 画布策略当前为 pad 黑边，blur-fill / crop-fill 留 v2.1
- 计划 schema 已预留三个扩展点（clip 效果 / overlay 区域 / timeline 布局块），新增能力无需重构
