# 第三方项目引用说明（Third-Party Notices）

SmartTrimAgent 在设计与实现中参考了以下开源项目。除注明外，本项目**未复制
其源代码**，引用的是公开的设计思想 / 算法流程 / 参数语义；个别直接借鉴实现
模式之处已在代码注释与下表「引用内容」中逐条标明。

| 项目 | 许可证 | 仓库 | 引用内容 | 引用位置 |
|---|---|---|---|---|
| [FireRed-OpenStoryline](https://github.com/FireRedTeam/FireRed-OpenStoryline)（小红书 FireRedTeam） | Apache-2.0 | github.com/FireRedTeam/FireRed-OpenStoryline | WORKFLOW SKILL（主流程技能）概念与「加载→切镜头→理解→筛选→分组→时间线→渲染」流程编排（V8.0 进一步借鉴其**agent 逐步执行形态**与 `prompts/tasks/filter_clips|group_clips` 的筛选/分组规则——≤5 不删、保留 >80%、Hook→Core→Vibe→End、场景聚合、单组 2-4 镜，规则以中文改写进本项目提示词，未复制原文）；`split_shots` 的 TransNetV2 镜头边界检测方案（V7.9 借鉴：以「复核」形态引入，解码参数与采纳/回退策略见 `docs/v7.9-transnet-rescue.md`）；`speech_rough_cut` 的 ASR 逐句判废思路（T14 规划参考）；其《性能瓶颈分析与优化建议》驱动的并发优化（串行 VLM → 并发、渲染层避免逐帧合成）；`select_bgm` 的「候选召回 → LLM 终选一首，失败降级候选第一条」选曲思路与 `omni_bgm_label` 的音频大模型听音打标思路（V7.5 自动配乐，无向量召回的轻量版） | `video_editing/workflow.py`（模块头注释）、`video_editing/video_agent.py`（Creative workflow 提示词段）、`video_editing/transnet.py`、`content_analysis._detect_boundaries`、`scripts/label_music.py`、`docs/v7.0-oneclick-and-perf.md`、`docs/v7.5-auto-bgm.md`、`docs/v7.9-transnet-rescue.md`、`docs/v8.0-agent-workflow.md` |
| [transnetv2_pytorch](https://github.com/allenday/transnetv2_pytorch)（allenday；模型原作者 [lovak/TransNetV2](https://github.com/lovak/TransNetV2)） | MIT | github.com/allenday/transnetv2_pytorch | **运行时依赖（可选，V7.9）**：TransNetV2 镜头边界检测模型的 PyTorch 移植（含官方权重文件）。pip 安装使用，未复制/修改其源码 | `video_editing/transnet.py`、`requirements.txt`（注释项） |
| [auto-editor](https://github.com/WyattBlue/auto-editor)（WyattBlue） | Unlicense | github.com/WyattBlue/auto-editor | 静音剪除的区间→片段转换（margin/min-cut/min-clip 参数语义 = 本项目 keep_padding/min_keep）、`word:`/`subtitle:` 把转写当信号流的思路（T14 规划参考）、多目标时间线导出（EDL 形态参考） | `video_editing/signal_detection.py`、`timeline_export.py`、`docs/v5.0-roughcut.md` |
| [LosslessCut](https://github.com/mifi/losslesscut)（mifi） | GPL-2.0 | github.com/mifi/losslesscut | 仅参考其检测/交互的**设计模式**（silencedetect stderr 解析、-ss 前置快速抽帧、原文件直放+区间钳制），未复制任何代码 | `signal_detection.py`、`web/static/js/materials.js` 有界预览注释 |

## 合规说明

- FireRed-OpenStoryline 为 **Apache-2.0**：引用其思想并注明来源即符合该协议
  对衍生作品的署名要求；如未来复制其**代码或提示词原文**，将按协议附注
  LICENSE 副本与修改说明。
- transnetv2_pytorch 为 **MIT**：作为可选运行时依赖经 pip 分发使用（含其
  自带的权重文件），MIT 允许随本项目分发/使用；署名保留于本表与
  `requirements.txt`。
- auto-editor 为 **Unlicense**（公共领域贡献），无署名义务，本仓库仍自愿标注。
- LosslessCut 为 **GPL-2.0**：仅思想层面的引用不构成衍生作品；本项目与之
  无代码共享，若未来引入其代码需整体考虑 GPL 传染性（当前规避）。
