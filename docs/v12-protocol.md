# V12 剪辑计划协议 v3.0 规范（字段参考 openshot，格式骨架自有）

> 协议定位：**LLM 学的是我们的工具（自造签名），写出的 JSON 说的是 openshot 的方言（借词），
> 底部行为语义照抄 openshot 验证过的规则**。不是 openshot 协议的实现，无 .osp 互操作；
> 借用的是命名纪律与生产验证过的更新语义。
> 参考来源：`D:\libopenshot\JSON-PROTOCOL.md`（字段）、`ARCHITECTURE.md`（渲染链路）、
> `UI-TO-JSON.md`（操作→JSON 生成链路）。

## 0. 版本与共存边界

- **v3.0**：LLM 可见 / 会话存储 / 唯一写出形态。
- **v2.0**：编译器与校验器消费的内部形态（plan_compiler / plan_schema 的既有字段名）。
- 版本识别只发生在两个边界，管线内部永远单一形态（R11 版本口径不一致熔断的教训）：
  1. **入口边界（归一）**：会话载入 / legacy submit_plan（宏）/ load_plan → `plan_dialect.normalize`（v2→v3）；
  2. **编译边界（降级）**：`compile_plan` 入口 → `plan_dialect.to_internal`（v3→v2）→ 现有管线零改动。
- 旧 v2 会话文件不手工迁移：首次载入即升级，下次落盘自然变 v3。
- `probe` 字段是宿主侧数据，不进 v3 协议（宿主回填）；升级时剥离、round-trip 不保留（设计使然）。

## 1. v3.0 计划 JSON

```json
{
  "schema_version": "3.0",
  "output": {"filename": "OUTPUT/demo.mp4", "width": 1280, "height": 720, "fps": 30},
  "clips": [
    {"id": "c1",
     "reader": {"type": "FFmpegReader", "path": "INPUT/a.mp4"},
     "shot": 3,
     "start": 4.2, "end": 9.0,
     "speed": 0.6,
     "volume": 0.8,
     "audio": {"mute": true},
     "cut_silence": {"noise_db": -35},
     "effects": [{"id": "e1", "type": "Brightness", "brightness": 0.15, "contrast": 1.1}]}
  ],
  "timeline": [
    {"clip": "c1"},
    {"clip": "c2", "transition": {"type": "fade", "duration": 0.5}}
  ],
  "overlays": [
    {"id": "o1", "type": "text", "at_clip": "c1", "start_offset": 0, "duration": 2,
     "gravity": "top", "text": "周末骑行", "font_size": 48, "color": "#FFFFFF"}
  ],
  "audio": {"source": "MUSIC/x.mp3", "volume": 0.3, "loop": true, "original": "mute"},
  "_narration": [{"clip_ids": ["c1"], "text": "旁白原文", "units": ["句一。"]}],
  "_captions": ["一镜一句"], "_caption_style": "bottom", "_title": "标题"
}
```

### 字段映射表（openshot → v3 → 内部 v2 → ffmpeg）

| openshot 概念 | v3.0 | 内部 v2 | ffmpeg 落点 | 差异点 |
|---|---|---|---|---|
| reader{type, path} | `reader{type,path}` | source + kind（按扩展名判定） | -i / -loop 1 | kind 不出现在 v3 |
| start / end（源内窗口） | `start` / `end` | trim_start / trim_end | -ss · trim | 同语义换名 |
| position（贴轴时刻） | **无**（顺序=数组序，时长=推导） | 数组序 | concat/xfade offset | 刻意不引进：防时间戳誊抄与 xfade 累加数学 |
| time⏱ 变速曲线 | `speed` 标量（clip 平级） | effects[].speed{factor} | setpts + atempo 链 | 标量子集；曲线不收 |
| volume⏱ | `volume` 标量（clip 平级） | clip.audio.volume | volume 滤镜 | 标量子集 |
| Brightness{brightness,contrast} | `type:"Brightness"` 参数平铺 | eq（参数子集） | eq= | 参数名一致 |
| Saturation{saturation} | `type:"Saturation"` | eq（参数子集） | eq=saturation= | 与 Brightness 同属 eq，拆/合往返精确还原 |
| gravity 0–8 | `gravity`（九宫格字符串，仅 overlay） | position | overlay=x:y | 字符串枚举非整数 |
| effects{id,type,…} | `{id, type, 参数平铺}` | {name, args} | skills 注册表分发 | id 为 v3 寻址主键 |
| 无对应（我方扩展） | shot / cut_silence / cut_black / audio(BGM) / _narration / _captions / 宏 | 同名 | 各自管线 | 防誊抄引用、意图键、宏 |
| Points 曲线（一切皆动画） | **协议预留**：多点位明确报错 | 只收标量 | 常量滤镜参数 | ffmpeg CLI 只能表达常量 |

### eq 拆/合语义

`eq` 的 brightness/contrast/saturation 相互独立（单滤镜合并与多滤镜级联输出等价），
因此：升级时 `eq{b,c,s}` → `Brightness{b,c}` + `Saturation{s}`（相邻放置）；
降级时相邻 eq 系效果合并回一个 `eq`（参数并集）——round-trip 精确还原。
LLM 分别写 Brightness + Saturation 也被合并归一，产出滤镜等价。

### 显式不对齐声明（与 openshot 的差异）

1. **position/layer 自由布局不给 LLM 写**——顺序=数组序、时长=推导（防幻觉设计）；
2. **keyframe 曲线协议预留**——v3 只收标量，`{"Points":[...]}` 多点位明确报错；
3. **渲染模型不对齐**——编译期命令批处理 vs GetFrame 帧服务，协议层不受影响；
4. **BGM = 全局 audio 块**——openshot 无此概念（其 BGM 是 clip）；
5. **未命中 id 报错并附可用清单**——openshot 静默跳过；我们防「以为改成了」并利于修复闭环；
6. **不学它的冗余字段组**——它修剪要 start/end/duration/position 四字段一组，我们只有
   start/end 是权威（duration 推导、position 不给写），冗余一致性负担从结构上消失；
7. **不学它的「边应用边炸不回滚」**——draft 在内存、submit 前无副作用，批内原子；
8. **不学它的 undo 记账字段**——`old_values`/`transaction` 引擎不读；我们的变更审计由
   plan_diff 承担，action 引擎的 applied 日志记 old_values 仅作将来撤销的预留。

## 2. action 协议（引擎层，plan_actions.py）

信封语义取自 openshot `ApplyJsonDiff`，LLM 通过按对象拆分的 diff 工具使用它
（工具内组装信封，LLM 不直接写信封）：

```json
{"key": ["clips", {"id": "c1"}], "type": "update", "value": {"end": 9.0}}
```

| 字段 | 取值 | 语义 |
|---|---|---|
| `key` | 寻址路径数组 | key[0] 路由对象类型，后续段按 id 定位 |
| `type` | `insert` / `update` / `delete` | 现场创建 / 部分覆盖 / 按 id 销毁 |
| `value` | 新数据 | **部分更新**：缺失字段保持原值；`reader`/`effects` 出现即整段替换 |

**key 路由表**：

| key 形态 | 路由 |
|---|---|
| `["clips"]` / `["clips",{"id":…}]` | clip 本身（reader/start/end/speed/volume/audio/shot…） |
| `["clips",{"id":…},"effects",{"id":…}]` | clip 内效果实例 |
| `["timeline",{"clip":…},"transition"]` | 前置转场（type+duration） |
| `["overlays"]` / `["overlays",{"id":…}]` | 文字/画中画 |
| `["audio"]` | 全局 BGM |
| `["output"]` | 输出规格 |
| `["_narration"]` | 旁白组 |

**与 openshot 的行为差异**：批内原子（一条错整批拒绝逐条回传；openshot 逐条独立应用、
异常点之前不回滚）；未命中 id 报错附可用 id 清单；`old_values` 记入 applied 日志
（撤销预留，本期不实现撤销 UI）。

**复合操作分解配方**（源自 openshot-qt UI 代码的操作→action 模式库）：

| 用户语义 | action 组合 |
|---|---|
| 切割 c2@35s | `clip_diff([{op:"update",id:"c2",end:35},{op:"insert",reader:<c2同源>,start:35,…}])` 一批两 op |
| 修剪左边缘 | `clip_diff([{op:"update",id:"c2",start:32}])`（只有 start/end 是权威） |
| 换 BGM | `audio_diff([{op:"set",…}])` |
| 加转场 | `timeline_diff([{op:"transition",clip:"c2",type:"fade",duration:0.5}])` |
| 删片段连带挂载文字 | `clip_diff([{op:"delete",id:"c3"}])`（级联，宿主自动清 overlay） |

## 3. LLM 工具面（18 个）

```
写入口（8）：clip_diff / effect_diff / overlay_diff / timeline_diff /
            audio_diff / set_project / set_narration / set_captions
生命周期（4）：load_plan / get_plan / validate_plan / submit
感知（4）：probe_media / analyze_media / list_shots / list_music
出口/宏（2）：ask_user / submit_plan（仅 select/workflow 宏专用）
```

- 五个 diff 工具的 action 形态（扁平，update 字段直接拍平在 op 对象上）：
  - `clip_diff`：`{"op":"update","id":"c1","end":9.0}` / `{"op":"insert","reader":{…},"shot":3,"after":"c2"}` / `{"op":"delete","id":"c3"}`
  - `effect_diff`：`{"op":"insert","clip":"c1","type":"Brightness","brightness":0.2}` / update/delete 按 id；`type:"speed"` 自动归位为 clip.speed
  - `overlay_diff`：insert（text/pip 两型）/ update / delete——id 寻址（消灭 v2 下标漂移）
  - `timeline_diff`：move（after 省略=最前）/ transition（13 种 + none，首段无转场校验）
  - `audio_diff`：set（全 8 参）/ clear
- 退役：17 个动词工具（代码冻结保留一版作回滚开关）、`edit_plan(ops)` 修订词表。
- 旧机制全部继承：单步校验就地报错、错误附可用 id 清单、shot 解析副本做全量校验、
  probe 宿主回填、零容忍未知字段、批内原子。

### 数据分层（借鉴 openshot TimelineSync 过滤 files/markers/settings）

| 层 | 内容 | 去向 |
|---|---|---|
| 进协议 | clips/timeline/overlays/audio/output/_narration/_captions | v3 计划 → 编译 |
| 宿主侧 | probe、内容卡缓存、感知工具输出、applied 日志 | 会话/内存，不进计划 |
| 会话层 | task/turn/history/pending_question/pending_plan/last_output | 会话文件，不进编译 |

## 4. 效果能力（只改名，能力不变）

| v3 type | 内部 skill | 参数 |
|---|---|---|
| Brightness | eq | brightness(-1~1)、contrast(-1000~1000) |
| Saturation | eq | saturation(0~3)；0=黑白 |
| hflip / vflip | 同名 | 无 |
| transpose | 同名 | dir 0~3 |
| delogo | 同名 | region [x,y,w,h]（源像素，留 1px 边距） |
| face_mosaic | 同名（PREPASS） | mode/block_size/score_threshold/target |
| （speed 不是效果） | speed | clip 平级 `speed` 标量 0.25~4.0 |

v2 名（eq/hflip/…）继续受理为别名；`resolve_effect` 是唯一的名字解析点。
后续独立一期（不在 v12 范围）：直映射效果扩充——ChromaKey→chromakey、Blur→boxblur、
Sharpen→unsharp、Crop→crop、ParametricEQ→equalizer 等；跟踪系/自绘系/3D 系
（Tracker/Stabilizer/Caption/LensFlare…）ffmpeg 后端不收，写了明确报错。

## 5. 两轮工作流

- **首轮**：set_project → clip_diff 批量 insert → effect/overlay/timeline/audio_diff
  → validate_plan → submit → 编译渲染。
- **修订轮**：宿主把上一版 v3 计划载入草稿（reset_draft(last_plan)，模型不重写整份）
  → 同一套 diff 工具只改要求的部分 → validate_plan → submit → plan_diff 作用域闸门
  → 编译（内容寻址缓存只重编码被改片段）。
