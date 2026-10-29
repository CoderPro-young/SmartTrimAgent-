# LLM 请求耗时优化：过程全记录

时间跨度：2026-10-28 晚 ~ 10-29 凌晨
范围：**只优化了一个版本——LLM 请求层（超时/流式）**。预填充、输出速度、轮次数量等其他瓶颈本轮未动（见「当前瓶颈」与「待办」）。
代码：`model.py` / `web/server.py` / `web/static/js/app.js` / `.env.example`，测试 `tests/test_model_streaming_timeout.py`（新增）+ `tests/test_v8_wait_heartbeat.py`（更新）。**截至本文未提交**。

---

## 一、问题发现（2026-10-28 晚，链路实测）

sim-1028-v10a 五连发压测（一键成片 + 多轮自然语言修订），事件级计时
（`TMP/_timed_run.py` → `_timed_last_run.jsonl`）：

| 轮 | 指令 | 耗时 | 结果 |
|---|---|---|---|
| 1 | 一键成片（72s 草原 vlog） | **716.5s** | 成功，但 **541.6s（75.6%）耗在一次 LLM 请求挂死** |
| 2 | 换抒情 BGM | 31.6s | 失败（R11 展开版存档缺陷，当晚另修） |
| 3/3b | 开头加大标题 | 344s | 成功 |
| 4 | 状态问询 | 0s | 零 LLM 直答 |

关键事实：会话总计 1092s，**一次挂死就占 49.6%**。挂死期间客户端零反馈、
零干预手段；重试闭环吸收后同样的活 128s 就干完了——等待与工作量无关。

同晚 VLM 打标路径出现同构问题：**362s × 4 并发挂死**，仍被重试闭环兜住。

## 二、诊断与纠正

### 第一版定性（错）

「LLM 请求无客户端级超时」——开发笔记 §七 原话。**复核代码后推翻**。

### 修正后的真因

`model.py` 自 2026-10-11（commit 1219042）起就有 `MODEL_TIMEOUT=180` +
`max_retries=2`。实测 541.6s ≈ **3 × 180s**：单请求 180s 超时其实每次都
触发了，是 **openai SDK 超时后静默重试 2 次、每次重新挂满 180s**，三层叠完
才把异常抛给上层。VLM 同机制：3 × 120s = 362s。

### 结构性冲突（为什么不能只调参数）

非流式请求的 read timeout 必须 ≥ **整段生成时长**，而合法的大工具轮实测
30~80s（批量 add_clips 2500+ tok、ask_user 长思考 71s）。timeout 压小会
误杀健康请求，不压小就陪挂死请求耗满——「挂死检测」和「长合法生成」共用
一个预算，参数层面无解。

## 三、修复设计（V1，已实施）

| 层 | 改动 | 效果 |
|---|---|---|
| **L2 核心** | 默认流式 HTTP（`MODEL_STREAMING=1`）。流式下 read timeout **按 chunk 间隔计**——挂死在首 token 前 ~60s 即断；健康的慢生成只要 token 持续到达就不受限 | timeout 才敢 180→60 |
| **L1 收紧** | `MODEL_RETRIES` 2→1；VLM `VLM_TIMEOUT=60`/`VLM_RETRIES=1` | 最坏逻辑调用 542s → **~125s**（2×60+退避） |
| **补洞** | `DEEPAGENT_MODEL` 显式路径的 `init_chat_model` 补传 timeout/max_retries（此前完全裸奔） | 堵住第三条路径 |
| **L3 可观测** | 首 chunk 触发 hook → server emit `llm_first_token` → 前端等待条区分「模型响应中」/「模型生成中（token 持续到达）」 | 挂死与慢生成可分辨 |

**实现要点**（踩过的坑）：
- langchain-openai **1.6.0** 的 `_generate` 已无流式分支（`streaming` 参数
  只对调用方直接 `.stream()` 生效）；langchain-core **1.6.1** 的
  `_should_stream` 虽会按实例属性把 `.invoke()` 路由到 `_stream`，但该路径
  不转发 run_manager。因此写了 `StreamingChatOpenAI` 子类：
  `_generate`/`_agenerate` 用 `generate_from_stream` 聚合（与 `.stream()`
  同一条 chunk 合并逻辑，tool_calls 增量照常聚合），`_stream` 上挂首 chunk
  hook——保证任何版本/路径下行为一致。
- hook 是模块级列表（服务端 RUN_LOCK 串行保证同一时刻只有一个 pipeline，
  与 `content_analysis.set_progress_hook` 同款先例）；注册/注销用
  `try/finally`，回调异常静默吞掉（可观测性钩子绝不影响主链路）。
- **回滚杆**：`.env` 设 `MODEL_STREAMING=0` 一键回非流式，timeout 默认值
  自动回 180/120，无需改代码。

## 四、验证

### 单测（tests/test_model_streaming_timeout.py，7 项全绿）

本地 stub 服务验证机制本身：
- **挂死 stub**（收请求永不回包）：timeout=2s 时 2s 预算内抛超时；
  max_retries=1 时总耗时 ≈ 2×2s+退避——重试放大器受控；
- **SSE stub**：`.invoke()` 流式聚合后 content + tool_calls 增量参数与
  整段回包等价（agent 链路正确性闸门）；`streaming=False` 回滚分支同测；
- **hook**：每请求恰好一次、注销后不再触发、回调抛异常不影响生成。

另有 `test_v8_wait_heartbeat` 更新为新默认（t6 retries=1、t7 60s 流式、
t7b 回滚 180s 非流式），10/10。全量回归 22 pass / 2 fail，两个失败均为
并行 V12 重构所致（`test_v10` 的 `new_plan` 已被挪走），与本修复无关。

### 端到端（worktree 隔离环境）

V12 并行会话把主树 `plan_with_retry` 签名改到半迁移、8000 起不来，改用
worktree（`D:\projects\deepagent-demo-wt`，分支 `test/llm-timeout-v10` =
V10 基线 a9e774f + 仅本修复）复测。环境坑两条，已记档：
- INPUT/TMP 用 junction 会被 deepagents 沙箱拒绝（junction 被解析成主树
  真实路径，判「outside root」）——INPUT 需 `cp -p` 实体拷贝（保 mtime，
  内容卡缓存键=文件名+大小+mtime 才能命中）；MUSIC 漏拷会让 set_audio
  循环失败（防线正确转 ask_user）。

| 场景 | 结果 |
|---|---|
| **拥堵窗口**（sim-wt-l2b，午夜） | 55 请求**零 OpenAITimeoutError**；8 次 provider 预填充挂死**每次被 60s 闸掐断自动恢复**（事件间隔 69.9s = 60 击杀 + 退避 + 8s 重试后正常），对照修复前单次 541s |
| **干净窗口**（sim-wt-l2c） | **462.2s 完整成片，97.52s，verify 通过**；25 请求 TTFT 中位 1.2s，零击杀零挂死 |
| 主树冒烟（sim-1028-l2a） | 34 请求全部流式跑通，最大单请求沉默 57.0s；最终熔断于 V12 schema 漂移（与本修复无关） |

### 修复前后对比（同任务「一键成片 + 配乐」，同素材）

| 指标 | 修复前（10-28 15:00） | 修复后（10-29 00:30，provider 更拥堵） |
|---|---|---|
| 总耗时 | 716.5s | **462.2s** |
| 灾难挂死 | 单请求 541.6s 纯等待 | **零（60s 封顶自动恢复）** |
| 最坏逻辑调用 | ~542s（180×3） | ~125s（60×2+退避） |
| 成片 | 72.5s verify 通过 | 97.52s verify 通过 |

## 五、当前瓶颈（462.2s 拆解）

| 块 | 耗时 | 占比 |
|---|---|---|
| 等首 token（TTFT，25 请求合计） | 227s | 49% |
| 生成期（模型吐字） | 219s | 47% |
| ffmpeg 渲染 + 回验 | 15s | 3% |

三个根因（性质不同，解法不同）：

1. **上下文逐轮全量重发 → 预填充越来越贵**（TTFT 227s）：TTFT 中位仅
   1.2s，但 10 个请求 >10s、尾部 38.6/30.6/22.2s——agent 循环每轮重发全部
   历史（13.7k→38k），provider 预填充随轮次线性变贵。
2. **三只生成期大象共 170s（占生成期 78%）**：89.1s 一轮只调了
   `list_shots`（思考阶段烧掉几千推理 token）；56.8s `new_plan`（计划
   schema 结构化输出）；24.2s `set_narration`（旁白文本）。
3. **输出速度 17~36 tok/s**：供应商物理上限，只能少生成。

## 六、待办

### 阶段 2 杠杆（按预期收益排，均未动工）

1. **验证 SiliconFlow 前缀缓存命中**（最大杠杆，针对根因 1）：agent 循环
   每轮重发的前缀完全相同，若命中自动前缀缓存，38k 预填充 → ~1k 增量，
   227s TTFT 理论可压到 60s 内。判别方法：看 TTFT 是否随轮次增大反而下降。
2. **思考预算封顶**（针对 89s 单轮）：若 DeepSeek-V4-Flash 支持 thinking
   budget 参数，给工具循环轮设上限，单轮最多省 ~70s。
3. **上下文摘要化**（dev-notes §七 瓶颈 3 的既有观察项）：旧轮工具结果换
   摘要，与杠杆 1 叠加。
4. **轮次数量**：V12 粗粒度动词方向，正在另一分支进行。

### 收尾与运维

- [ ] **提交本修复**：主树 5 文件未提交；`server.py` 与 V12 未提交编辑
  纠缠，等 V12 会话收敛后并入（worktree 分支 `test/llm-timeout-v10` 留有
  完整干净版本可对照/搬运）。
- [ ] **重启 8000 服务**：主树当前起不来（V12 半迁移），V12 收敛后重启
  才吃到本修复（Python 模块进程启动时加载）。
- [ ] **清理 .env 的 LangSmith key**：已失效（401 Invalid token），每轮
  LLM 调用仍拖着死 tracer 的重试开销；换新 key 或清掉。
- [ ] worktree 去留决定（验证已完成，可删可留作对照）。
- [ ] 观察项（未修）：内容卡缓存对 mtime 敏感（素材挪动即 944s 全量重
  分析，可改 content hash 键控）。

### 明确不做

- 输出速度（17~36 tok/s）：供应商侧，客户端无解。
- 渲染（15s / 3%）：非瓶颈，不动。

---

数据来源：`TMP/_timed_last_run.jsonl`（事件级到达时刻）、`TMP/_l2_wt_round1*.log`、
`TMP/run_errors.log`、`docs/dev-notes-2026-10-28.md` §七/八/九。
