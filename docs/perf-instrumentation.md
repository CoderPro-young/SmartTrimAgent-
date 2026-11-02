# 全链路耗时埋点（perf trace）使用说明

> 2026-11-01 落地。目的：性能优化阶段不用再开会话/翻 LangSmith 才能看一次
> 任务的耗时分布——每次任务自动落盘一份报告，打开即看瓶颈。

## 看什么

每次任务（`/api/run`、`/api/replan`）和每次上传预热结束，自动写两个文件：

```
TMP/perf/1101_143025_073_06f4b9fb_t2_run.json   完整数据（瀑布分析用）
TMP/perf/1101_143025_073_06f4b9fb_t2_run.txt    人读摘要（观察瓶颈看这个）
```

文件名：`月日_时分秒_毫秒_会话前8位_t轮次_origin`，`origin ∈ run / replan / prewarm`。

服务端控制台同时打印同样的摘要；NDJSON 流末尾会发一条
`{"type":"perf","summary":...}` 事件（前端忽略，`TMP/_timed_run.py` 会记时间戳）。

## txt 摘要怎么读

```
== perf [run] sid=06f4b9fb turn=2 total=462.3s (2026-11-01 14:38:07) ==
task: 把这两个素材剪成一条 30 秒的朋友圈视频
stages (总耗时 / 独占):
  agent_loop                     380.2s /      12.1s
  agent_attempt#1                375.0s /       9.8s
    vlm_tag_shots                160.3s /       2.1s
  ...
LLM ×4  total 210.5s  ttft avg 8.2s  slowest: llm 88.3s
VLM ×6  total 150.0s  slowest: vlm_batch#3 41.0s
ffmpeg ×9 total 15.2s  slowest: ffmpeg 8.1s
slowest spans:
     88.3s  [llm] llm  {...}
```

- **stages 表**：按开始时间排序；`总耗时` 是该环节的墙钟时长，`独占`
  扣掉了期间发生的 LLM/VLM/ffmpeg 时间（并发子 span 按合并区间扣，不重复）。
  独占时间大的环节 = 自身逻辑慢（如 `_derive` 数学、场景切分的 ffmpeg 之外的
  纯计算基本不会大）；独占接近 0 = 时间都花在下面的子项里。
- **LLM 行**：`ttft avg` 是首 token 平均延迟（等排队/预填充），与总时长分开
  看——TTFT 大是排队/网络，总时长大是生成慢。meta 里有 `prompt_chars`、
  `n_images`、`n_chunks`、`usage`（供应商给了就记）。
- **VLM 行**：每批帧一次请求一条 span，`attempt=2` 表示失败重试的那次；
  `vlm_tag_shots` stage 的独占时间能看出「打标请求之外还等了什么」（如
  并发排队）。
- **ffmpeg 行**：全部子进程（归一化/渲染/预检/回验/抽帧/缩略图）统一在这里，
  `by_tool` 区分 ffmpeg/ffprobe。
- **slowest spans**：全 trace 最慢的 10 条明细，meta 带 command/模型/批号。

## 典型诊断口径

| 现象 | 看哪里 |
|---|---|
| 新任务提交后转圈很久 | run 报告的 `agent_attempt#1` 内 `vlm_tag_shots`：命中预热未完成+任务内重复分析（对照同时刻 prewarm 报告的时间轴重叠，即台账 §1 双路挤兑） |
| LLM 等很久没动静 | llm span 的 `ttft_s` 大而 `n_chunks` 小 → 排队/挂死；`n_chunks` 大 → 慢生成（对照 72~78 tok/s 基线） |
| 渲染阶段慢 | `execute` → `exec_render` 的 ffmpeg spans（单条命令明细） |
| 编译意外慢 | `compile_plan` → `compile_expand` 里嵌的 llm spans（宏路径的旁白/文案/BGM 调用） |

## 实现速查

- 模块：仓库根 `perf_trace.py`（flat span 列表 + contextvars 流动；无活动
  trace 时全部调用 no-op，绝不影响主链路）。
- 咽喉点：`model.py StreamingChatOpenAI._stream/_generate`（主模型 + VLM 全部
  LLM 请求）、`ffmpeg_exec._run_tracked`（全部 ffmpeg/ffprobe 子进程）。
- 环节 span：`web/server.py _run_pipeline`（materials/session/gate/classify/
  brief/agent_build/agent_loop/plan_diff/scope_gate/commit）、
  `video_agent.plan_with_retry`（每次 attempt + preflight）、
  `plan_compiler.compile_plan/execute`（编译子阶段、precheck、三批命令）、
  `content_analysis.analyze_media/tag_shots`（分析 + 打标）。
- 线程池不继承 contextvars：`content_analysis.tag_shots` 与
  `plan_compiler._execute_impl.run_batch` 的 submit 点显式
  `contextvars.copy_context()` 传播，新增并行段要照做，否则 span 断链。
- 上传预热：`server._bg_analyze` 在真正开工后开独立 trace（`origin=prewarm`），
  与 run 报告按时间戳（epoch）对齐即可看双路挤兑。
- 开关：`PERF_TRACE=0` 完全禁用（.env）。
