# 计划：VLM 感知层接入（多模态内容理解）—— 调研与设计稿

> 本文承接 [dev-notes-2026-09-21.md](./dev-notes-2026-09-21.md) D5（"根因是感知缺失"）与
> [roadmap-dirty-work-automation.md](./roadmap-dirty-work-automation.md) 远期条目
> （"VLM 语义打标 / 『找有小孩的镜头』，接 SiliconFlow vision 模型"），收敛 2026-09-21
> 晚间关于**多模态模型选型与接入方式**的调研和设计。
>
> **状态：已实施（2026-09-24）**。实现与验收记录见
> [v4.0-multimodal-perception.md](./v4.0-multimodal-perception.md)；§十开放问题中
> 第 1 条（SiliconFlow 实测：GLM-4.6V 未上架，定 `zai-org/GLM-4.5V` 为默认）与
> 第 6 条（验收素材）已随实施闭环，其余转入 v4.0 文档 §七 Phase 4。
> 本文保留为设计依据；架构图源文件 `plan-vlm-perception-layer.drawio`。

---

## 一、背景与动机

**问题**：`probe_media` = `ffprobe -show_format/-show_streams`，只读容器头（时长/分辨率/
编码/有无音轨），从不解码内容。系统对"画面里有什么"一无所知，因此这类任务今天无法表达：

- "帮我剪辑和朋友一起的时光"（需要知道哪些镜头有多人、聚会、欢笑）
- "只保留有人的画面"（时刻 Pro 已被市场验证的 L3 语义筛选）
- "挑最精彩的三段"（高光选择依赖内容信号）

dev-notes D5 已给出诊断与分工准则：**带判断的选择 → 模型 + 可见信号**。缺的就是
"可见信号"里的语义那一半——T9 场景切分（确定性）之后，每个镜头**是什么**仍然不存在。

**目标**：给每个素材建立**镜头级语义索引**（内容卡片），通过独立的 `analyze_media`
工具暴露给规划模型，让它像读时长/分辨率一样读到"0.0–8.2s 餐厅三人聚餐·欢笑"，
然后手写显式 trim 完成语义筛选——复用现有 clips/timeline 计划语言，**不动 schema**。

---

## 二、多模态模型调研（2026-09-21）

### 2.1 候选横向对比

| 模型 | 获取方式 | 视频输入 | 关键能力 | 适配本项目 |
|---|---|---|---|---|
| **GLM-4.6V**（智谱，2025-12 开源） | bigmodel.cn API；SiliconFlow 托管；开源权重（106B 基础 + 9B 轻量） | 原生 `video_url`（URL / base64），最长约 **1 小时**，128K 上下文，原生 Function Call | 同参数规模视觉理解 SOTA，长视频"全局梳理 + 时序细粒度推理" | ★ 与现有 SiliconFlow key 同源；SiliconFlow 侧走抽帧 `image_url` |
| GLM-4.5V（上代） | 同上 | `video_url` | 106B MoE（激活 12B），图像/视频/文档/GUI | 备选 |
| **Qwen3-VL**（阿里） | 百炼 DashScope（qwen3-vl-plus/max）；开源 | 原生 `video_url`（compatible-mode），256K 上下文（可扩 1M） | VideoMMMU 领先，多模态推理强 | 需新 DashScope key |
| Qwen2.5-VL 系列 | SiliconFlow（7B/32B/72B） | SiliconFlow 侧**仅 `image_url` 抽帧** | 成熟稳定，便宜 | ★ 现成可用，作为默认退路 |
| Gemini 3 Pro | Google AI Studio / Vertex | 原生长视频理解 | 商业 API 里视频理解口碑最佳 | 国内网络/计费不便；仅作了解 |

注：`.venv` 里已装 `langchain-google-genai 4.3.7`（项目代码未用），若将来要走 Gemini
路线零安装成本——但非本次推荐。

### 2.2 必须遵守的三个既有约束

1. **端点内容块限制**：SiliconFlow 实测只接受 `text` 与 `image_url` 内容块
   （video_agent.py:59/387 的 400 记录）→ SiliconFlow 路线上 VLM 只能吃**抽帧多图**，
   不能传整段视频；原生 `video_url` 只有 bigmodel / DashScope 才有。
2. **永不重放消息历史**（v3.1 D1 铁律）→ 图片绝不能进入 agent 主循环的消息流；
   VLM 结果只能以**文本信号**（内容卡片）形式交给规划模型。
3. **服务端真相优先于模型自述**（Preflight Tier 2 原则）→ 信号由确定性工具算出，
   模型只读不算。

### 2.3 来源

- 智谱开放文档（GLM-4.5V / GLM-4.6V，含 video_url 与 base64 调用示例）：docs.bigmodel.cn
- GLM-4.6V 发布介绍：z.ai（2025-12；1 小时视频 / 128K / Function Call）
- SiliconFlow 视觉语言模型文档与模型中心：docs.siliconflow.cn / siliconflow.cn
- 阿里云百炼《视觉理解》（qwen3-vl 系列 video_url 输入）：help.aliyun.com
- 2026 开源 VLM 综述（Qwen3-VL 256K 上下文）：bentoml.com；VLM API 指南：mixpeek.com

---

## 三、选型决策（2026-09-21 已确认，评审时可推翻）

### D1 · 路线：SiliconFlow 抽帧多图

ffmpeg 抽关键帧 → `image_url`（base64）批量送 VLM 打标。默认模型 GLM-4.6V
（SiliconFlow 上的确切 model ID 待实测确认，见 §十-1；退路 `Qwen/Qwen2.5-VL-32B-Instruct`）。

- **为什么不选原生 video_url（bigmodel GLM-4.6V / 百炼 qwen3-vl）**：时间定位与长视频
  理解更好，但要引入第二个供应商、第二套计费；抽帧路线零新依赖、复用现有 key、
  帧数即成本上限（roadmap"按帧配额计费"原设计）。架构上留 `VLM_*` 配置缝，
  将来切原生视频路线只换客户端实现，不动感知层。

### D2 · 范围：感知层完整链路，MVP 不做 auto_select 宏

交付到"规划模型读卡片手写 trim 计划"为止（schema 不动）。`auto_select{criteria,budget}`
编译器宏（dev-notes D1 宏层）留到验证卡片质量后再做——模型手写 trim 本来就是保底通道。

### D3 · 触发：独立工具 `analyze_media`，模型按需调用（2026-09-24 评审修订）

**原方案**是内嵌 `probe_media`（探测即索引，感知无条件发生）；**评审改为独立工具**，
理由：纯机械任务（截取/拼接/加字幕/已知区间的处理）不该付 VLM 的 token 成本，
由模型判断任务是否涉及内容理解、按需调用 `analyze_media(path)`。

代价是"要不要感知"的决策交回模型，需配套防护（§4.5）。缓存仍是 sidecar
`TMP/probe/<hash>.json`（T11）；**标签 schema 固定、与任务无关**，保证按文件缓存
可复用（不能按问题定制标签，否则缓存失效）。"上传即索引"（后台预分析）仍是后续优化。

---

## 四、架构设计

### 4.1 核心原则（延续"信号是确定性工具算出来的，让模型看见"）

1. **时间由确定性信号产生，语义由 VLM 产生**：镜头边界全部来自 ffmpeg 场景检测
   （`select='gt(scene,0.3)'`），VLM 只给每个镜头打语义标签、**不报时间戳**——
   从结构上杜绝"VLM 幻觉时间戳还能通过全部校验"（dev-notes D5 点名的语义幻觉实例）。
2. **图片不进 agent 主循环**：VLM 调用是感知模块内部的独立一次性请求（每批帧一个），
   结果以文本卡片由 `analyze_media` 工具返回。agent 消息流里永远只有文本。
3. **优雅降级**：无 `VLM_API_KEY` → 卡片只含场景切分（L1 信号，链路仍完整可用）；
   VLM 某批失败 → 该批镜头标 `tag_failed`，不中断整体；场景检测失败 → 均匀采样兜底。
4. **缓存复用零重算**：`TMP/probe/<hash>.json`，键 = name+size+mtime（与 `_probe_file`
   同策略），对应 roadmap 北极星"复用零重算"。

### 4.2 数据流

```
工作流（V2_SYSTEM_PROMPT，语义任务走 2.5，机械任务跳过）
  1. ls INPUT/
  2. probe_media("INPUT/a.mp4")          # 容器元数据：必做、便宜、零改动
  2.5 任务涉及内容理解（找/挑/筛选/高光/"和朋友一起"类）？
       └─ 是 ──> analyze_media("INPUT/a.mp4")     # 新工具，agent 主动调用
                   ├─ sidecar 命中？ ── 是 ──> 直接返回卡片
                   ├─ ① 场景切分：ffmpeg select='gt(scene,0.3)' + showinfo
                   │      → 镜头边界列表；超帧预算则均匀下采样；每镜头取代表帧
                   │      → 缩至长边 ~640px / JPEG q80 / base64
                   ├─ ② VLM 打标：每批 ~6 帧，严格 JSON 输出
                   │      → {person_count, has_children, scene, activity, mood, tags[], quality, usable}
                   └─ ③ 合成内容卡片 → 写 sidecar → 返回（文本）
  3. 可行性自检（现有）
  4. submit_plan：模型读卡片 + 用户意图 → 挑镜头区间 → 手写 trim_start/trim_end
  ⇒ 之后全链复用：Tier 2 校验 → 编译 → 渲染 → 回验
```

### 4.3 内容卡片 schema（v1）

```json
{
  "schema_version": 1,
  "source": "INPUT/a.mp4",
  "generated_at": "2026-09-21T22:00:00",
  "vlm_model": "THUDM/GLM-4.6V",
  "frame_budget": 24,
  "shots": [
    {
      "start": 0.0, "end": 8.2,
      "person_count": 3, "has_children": false,
      "scene": "餐厅", "activity": "聚餐", "mood": "欢笑",
      "tags": ["多人", "举杯", "室内"],
      "quality": "good", "usable": true
    }
  ],
  "summary": "生日聚会：餐厅多人聚餐 + 户外合影"
}
```

- 图片素材：无场景检测，单帧单镜头（`start=0`），同样进卡片（overlay/PiP 素材也需要语义）。
- 卡片字段是**给模型读的信号**，不是计划语言的一部分——计划 schema 保持封闭，
  模型不得把卡片字段誊抄进 plan（提示词里写明）。

### 4.4 与 roadmap 任务编号的关系

| 任务 | 关系 |
|---|---|
| T9 场景切分 | **被本计划吸收**：§4.2 ① 即 T9 的解析器落地，完成后在 roadmap 勾选 |
| T11 sidecar 缓存 | **由本计划落地**：`TMP/probe/<hash>.json` 即其原设计 |
| T10 静音区间 | **不涉及**：音频信号独立，仍按 dev-notes 顺序单独做（将来同样写进 sidecar） |
| 远期"VLM 语义打标" | 本计划即其提前落地；"高光集锦/多素材智能选择/身份识别"仍后置 |

### 4.5 独立工具路线的配套约束（2026-09-24 评审新增）

把"要不要感知"交回模型后，防幻觉的关键从结构保证退到了提示词约束，需要补三道：

1. **提示词硬规则**：任务涉及内容理解 → 必须先 `analyze_media` 再动笔；**没有卡片
   不得凭空写语义相关的 trim 区间**（这是 dev-notes D5 点名的最危险幻觉——编造的
   时间戳能通过现有全部校验）；宁可 `ask_user`。
2. **标签 schema 与任务无关**：卡片字段固定，不按用户问题定制——否则缓存从按文件
   失效成按问题失效，"复用零重算"崩塌。任务相关性由规划模型读卡片时自己判断。
3. **兜底观察项**：实测统计"语义任务漏调 analyze_media"的发生率；若不可忽略，
   仿 web 端 `_l1_scan` 加 warn 级扫描（特征：多段细粒度 trim + 未调用过 analyze_media），
   先靠提示词，不动结构。

---

## 五、分阶段实施方案

### Phase 1 · VLM 配置 + 感知层核心模块

**1a. `model.py` 新增 `get_vlm_model() -> BaseChatModel | None`**

- 环境变量：`VLM_MODEL`（默认待实测的 GLM-4.6V SiliconFlow ID）、
  `VLM_BASE_URL`（默认 `SILICONFLOW_BASE_URL`）、`VLM_API_KEY`（默认回退 `SILICONFLOW_API_KEY`）
- 无任何 key → 返回 `None`（调用方降级为纯 L1 信号）
- `.env.example` 补三个变量 + `VLM_FRAME_BUDGET`（默认 24 帧/文件）

**1b. 新增 `video_editing/content_analysis.py`**

- `sample_keyframes(path, project_root, budget) -> list[shot]`：场景检测 + showinfo 解析
  + 代表帧抽取 + 下采样 + 压缩编码
- `tag_shots(shots, vlm_model, on_progress) -> list[shot_card]`：批处理打标，JSON 解析失败
  重试一次，批失败不熔断
- `analyze_media(path, project_root, probe_meta, on_progress) -> content_card`：编排 + sidecar
  读写（命中即返）
- 模块级 `on_progress` 回调钩子（供 Web 订阅进度）

**1c. 离线单元测试**（新建 `tests/test_content_analysis.py`，mock VLM/ffmpeg 不联网）

showinfo 解析、超预算下采样、缓存命中/失效、坏 JSON 容错、无 key 降级、图片单帧路径。

### Phase 2 · `analyze_media` 工具 + 提示词

- `video_agent.py` 新增 `@tool analyze_media(path) -> str`：调
  `content_analysis.analyze_media`，返回卡片 JSON（或降级原因 `vlm_unavailable` /
  `analysis_failed`）；**`probe_media` 保持纯容器探测，零改动**
- `V2_SYSTEM_PROMPT` 工作流插入 step 2.5（§4.5 的三条约束写进提示词）：
  - 判断任务是否涉及内容理解 → 是则对涉及的每个素材调用 `analyze_media`
  - 语义筛选类任务 → 按卡片选镜头区间，写显式 `trim_start/trim_end`
  - 卡片显示无匹配镜头（如全程无多人画面）→ 走 `ask_user` 报事实给选项
  - 未分析不得凭空写语义 trim；卡片字段不得誊抄进计划
- CLI 端到端验证：`python video_editing/video_demo.py "帮我剪出和朋友一起的时光"`

### Phase 3 · Web 可视化

- `web/server.py`：注册进度回调 → 新 NDJSON 事件 `analysis`（stage/file/done/total）；
  `/api/inputs?probe=1` 读 sidecar 附卡片摘要（零成本）
- `web/index.html`：右栏新增「内容索引」卡片（每素材镜头表：时间区间 + 人数 + 场景/
  活动/情绪标签 + 质量徽章）；分析中显示进度（"VLM 打标 4/6 批"）

### Phase 4 · 后续迭代（不在本期）

`auto_select` 编译器宏 → 上传即索引（后台预分析）→ 原生 `video_url` 路线
（bigmodel/DashScope，换客户端实现即可）→ 人脸聚类/同伴身份识别（"这是小明"级）→
高光评分集锦。

---

## 六、涉及文件

| 文件 | 动作 |
|---|---|
| `video_editing/content_analysis.py` | 新增（感知层核心） |
| `tests/test_content_analysis.py` | 新增（离线单测） |
| `model.py` | 修改（+`get_vlm_model`） |
| `video_editing/video_agent.py` | 修改（+`analyze_media` 工具、提示词 step 2.5） |
| `web/server.py` | 修改（analysis 事件） |
| `web/index.html` | 修改（内容索引卡片） |
| `.env.example` | 修改（VLM_* 配置） |

## 七、验收标准

1. 离线单测全绿（mock，不联网）
2. e2e：3 段真实素材（多人聚会 / 单人风景 / 混合），任务"帮我剪出和朋友一起的时光"
   → agent 基于卡片选中多人镜头 → 编译执行 → verify 通过
3. 无 `VLM_API_KEY` 时全链路仍可用（降级为容器元数据 + 场景切分）
4. 同一素材第二次运行零重算（sidecar 命中，日志可证）
5. 单文件分析成本受硬约束：帧数 ≤ 预算（默认 24），帧分辨率 ≤ 640px 长边

## 八、风险与对策

| 风险 | 对策 |
|---|---|
| SiliconFlow 多图上限 / 每图 token 规则未实测 | 批次大小可配置；批失败自动减半重试；上线前先跑实测脚本（§十-1） |
| VLM 时间定位误差 | 设计上 VLM 不报时间；镜头边界全来自确定性场景检测 |
| 抽帧密度低导致镜头语义代表性差 | 每镜头取代表帧而非全片均匀抽帧；长镜头（>10s）可取 2 帧（待评审定） |
| 标签质量不达预期（人数/活动识别错） | MVP 先人工评估（e2e 验收）；不达预期换模型只是改 `VLM_MODEL` 一个环境变量 |
| **模型跳过 `analyze_media` 直接编语义 trim**（独立工具路线的主要新风险） | 提示词硬规则（先分析后动笔，宁可 ask_user）；实测发生率，必要时加 L1 式 warn 扫描（§4.5-3） |
| "朋友"是通用语义（多人/聚会），不是身份识别 | 明确边界：特定人物识别（参考照片 + 人脸聚类）在 Phase 4；卡片 `person_count`/`tags` 已预留线索 |
| 分析耗时（24 帧 × 批 6 = 4 次请求）落在首次 probe_media | 进度事件可见；缓存后零成本；必要时帧预算调小 |

## 九、成本估算框架（单价待实测，评审时补）

每素材每首次分析 ≈ `帧预算 × 每帧token数`（输入） + `镜头数 × ~80token`（输出）。
按 24 帧、每帧 1–2k token 估：**单文件单次 ≈ 25–50k 输入 token**；缓存命中后为零。
具体单价以 SiliconFlow 模型中心为准（§十-1 实测时一并记录），不在此臆造数字。

## 十、待讨论问题（2026-09-22 评审拍板）

1. **SiliconFlow 实测**：GLM-4.6V 是否已上线 / 确切 model ID / 多图上限 / 每图 token 规则 /
   单价——建议评审前跑一个 10 分钟实测脚本（抽 6 帧打标，看质量与账单）
2. **默认参数**：帧预算 24、批大小 6、长边 640px、场景阈值 0.3 是否合适（素材典型时长
   是几分钟的手机视频还是几十分钟？决定预算量级）
3. **排序**：本计划 vs dev-notes 第二批（T10 静音 → T12 trim_silence）谁先？
   感知层已吸收 T9/T11，若先行则第二批顺延
4. **提示词策略**：卡片以完整 JSON 返回给模型，还是压缩成每镜头一行的表格文本
   （省 token、防誊抄，但丢结构）——倾向后者，待定
5. **后续优先级**：Phase 4 里 auto_select 宏、上传即索引、身份识别的先后
6. **验收素材**：用哪些真实素材做 e2e（需要至少一段多人聚会的素材）

---

## 附：调研来源链接

- GLM-4.5V / GLM-4.6V 官方文档：https://docs.bigmodel.cn
- GLM-4.6V 发布：https://z.ai/blog （2025-12）
- SiliconFlow VLM 文档：https://docs.siliconflow.cn ；模型中心：https://siliconflow.cn
- 阿里云百炼视觉理解（Qwen3-VL video_url）：https://help.aliyun.com
- 2026 开源 VLM 综述：https://www.bentoml.com ；VLM API 指南：https://mixpeek.com
