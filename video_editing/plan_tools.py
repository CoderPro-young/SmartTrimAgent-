"""计划构建工具集（V12）：按对象拆分的 diff 工具面，协议见 docs/v12-protocol.md。

设计契约：
- LLM 看到的是 8 个写入口（5 个 diff + 3 个设置类）+ 4 个生命周期工具；
  每个工具内部把扁平 op 组装成 action 信封，经 plan_actions.apply_actions
  引擎施加——信封语义全系统只有一份实现；
- 单步轻校验（引用存在/白名单/类型）就地做，错误单步返回 + 全量 draft 摘要
  （反馈回路最短，V10 实测有效的模式原样继承）；跨片段全量规则在
  validate_plan()/submit() 做；
- 批内原子：一次调用里的 ops 要么全成要么全拒（引擎保证）；
- shot 引用 + probe 宿主回填（「引用代替誊抄」）原样继承；
- draft 是 v3.0 计划 JSON（openshot 命名）；submit() 后宿主经方言层降级编译。

旧 V10 动词工具面冻结在 plan_tools_v10.py（回滚开关：video_agent 装配处切换）。
draft 状态挂宿主侧（模块级）：RUN_LOCK 串行保证同时只有一个 run 在构建。
"""

from __future__ import annotations

import copy
import os

from langchain_core.tools import tool

import content_analysis
import plan_actions
import plan_dialect
import plan_schema
from skills import validate_audio, v3_effects_menu

PROJECT_ROOT = "."          # 由 video_agent 装配时注入（测试可覆写）
PROBE_FN = None             # 服务端缓存探针（src->probe dict）；None=现场 ffprobe

# 模块级 draft 状态：{"plan": dict|None, "submitted": bool, "validated": str|None}
_STATE: dict = {"plan": None, "submitted": False, "validated": None}


# ------------------------------------------------------------------ 宿主 API -- #

def reset_draft(plan: dict | None = None) -> None:
    """新 run 初始化；修订轮传 last_plan（v3）作初态——模型在上一版上改。"""
    _STATE.update(plan=plan_dialect.normalize(plan) if plan else None,
                  submitted=False, validated=None)


def take_submitted() -> dict | None:
    """取走已 submit 的 draft 计划（消费型；未提交/已消费返回 None）。"""
    if not _STATE["submitted"] or _STATE["plan"] is None:
        return None
    _STATE["submitted"] = False            # 消费：重试轮需模型再次 submit
    return _STATE["plan"]


def has_draft_activity() -> bool:
    """本轮 run 是否用过构建工具（plan_with_retry 据此选择回传措辞）。"""
    return _STATE["plan"] is not None


# ------------------------------------------------------------------ 内部工具 -- #

def _draft() -> dict | None:
    return _STATE["plan"]


def _bump() -> None:
    _STATE["validated"] = None


def _apply(builder) -> str:
    """diff 工具共用入口：builder 组装信封 → 引擎施加 → 结果文本 + 摘要。"""
    plan = _draft()
    if plan is None:
        return "✗ 还没有 draft——先调用 set_project 初始化。"
    actions, pre_errs = builder(plan)
    if pre_errs:
        return "✗ " + "；".join(pre_errs)
    new_plan, applied, errors = plan_actions.apply_actions(plan, actions)
    if errors:
        return "✗ 整批未应用（批内原子），逐条错误：\n- " + "\n- ".join(errors) \
            + "\n修正后重新调用本工具（已成功的部分不需要重做）。"
    _STATE["plan"] = new_plan
    _bump()
    head = "；".join(a["detail"] for a in applied[:8])
    if len(applied) > 8:
        head += f"（等共 {len(applied)} 条）"
    return f"✓ {head}\n" + _summary(new_plan)


def _shot_span(src: str, shot: int) -> tuple[float, float] | None:
    """从内容卡取第 shot 个镜头（1 起）的 (start, end)。"""
    card = content_analysis.load_cached_card(PROJECT_ROOT, src) if src else None
    shots = (card or {}).get("shots") or []
    if not (isinstance(shot, int) and 1 <= shot <= len(shots)):
        return None
    sh = shots[shot - 1]
    try:
        return float(sh.get("start") or 0.0), float(sh.get("end") or 0.0)
    except (TypeError, ValueError):
        return None


def _probe_filled_copy(plan: dict) -> dict:
    """全量校验用副本：shot 解析成卡上边界 + 真实探针回填（draft 本体不动）。

    shot 边界来自确定性场景切分，安全；probe 让「整段使用（不写 end）」的
    片段能确定时长——宿主本来就掌握这些事实，模型不必誊抄。
    """
    snap = copy.deepcopy(plan)
    import plan_schema as ps
    fn = PROBE_FN or ps._default_probe_fn(PROJECT_ROOT)
    for c in snap.get("clips") or []:
        if not isinstance(c, dict):
            continue
        shot = c.get("shot")
        if shot is not None:
            span = _shot_span(str((c.get("reader") or {}).get("path") or ""), shot)
            if span:
                c["start"], c["end"] = round(span[0], 3), round(span[1], 3)
            c.pop("shot", None)        # 校验副本里 shot 与 start/end 互斥
        reader = c.get("reader") or {}
        src = reader.get("path")
        if isinstance(src, str) and plan_dialect._kind_of(src) == "video" \
                and "end" not in c and not isinstance(c.get("probe"), dict):
            try:
                info = fn(src) or {}
            except Exception:          # noqa: BLE001 —— 探测失败交给下游校验
                continue
            if isinstance(info, dict) and info.get("ok") and info.get("duration"):
                c["probe"] = {k: info[k] for k in
                              ("duration", "width", "height", "fps",
                               "has_audio", "has_video") if info.get(k) is not None}
    return snap


def _clip_dur_display(c: dict) -> str:
    """摘要用时长（shot 引用从卡上取边界；与编译展开同源）。"""
    s, e = c.get("start"), c.get("end")
    if isinstance(s, (int, float)) and isinstance(e, (int, float)):
        return f"{e - s:g}s"
    span = _shot_span(str((c.get("reader") or {}).get("path") or ""),
                      c.get("shot") or 0) if c.get("shot") else None
    if span:
        return f"{span[1] - span[0]:g}s"
    if plan_dialect._kind_of(str((c.get("reader") or {}).get("path") or "")) == "image" \
            and isinstance(c.get("duration"), (int, float)):
        return f"{c['duration']:g}s"
    return "?"


def _validate_clip_item_v3(item: dict) -> list[str]:
    """insert 单片段就地校验（V10 模式继承）：合成单片段计划跑一遍
    plan_schema 全部单片段规则（素材存在性/shot 引用/trim 互斥/效果参数），
    零规则重复。probe 用占位值（真实探针在 validate/submit 回填）。"""
    synthetic = {"schema_version": "3.0",
                 "output": {"filename": "OUTPUT/_draft_check.mp4",
                            "width": 640, "height": 360},
                 "clips": [dict(copy.deepcopy(item), id="cX")],
                 "timeline": [{"clip": "cX"}]}
    for c in synthetic["clips"]:
        if plan_dialect._kind_of(
                str((c.get("reader") or {}).get("path") or "")) == "video":
            c.setdefault("probe",
                         {"duration": 10.0, "width": 1920, "height": 1080,
                          "fps": 30, "has_audio": True, "has_video": True})
    errs = plan_dialect.validate_any(synthetic, PROJECT_ROOT)
    # 剔除与上下文相关的误报：合成计划里没有转场/时长推导问题
    return [e for e in errs if "cX" in e or "clips[0]" in e]


def _summary(plan: dict) -> str:
    """draft 紧凑状态摘要（get_plan/各工具返回值共用）。"""
    out = plan.get("output") or {}
    lines = [f"draft「{out.get('filename', '?')}」"
             f" {out.get('width', '?')}x{out.get('height', '?')}"
             f" @{out.get('fps', 30)}"]
    trans = {t.get("clip"): t.get("transition")
             for t in plan.get("timeline") or []}
    total = 0.0
    for c in plan.get("clips") or []:
        tr = trans.get(c.get("id"))
        tr_s = "硬切" if not tr else f"{tr.get('type')}{tr.get('duration')}s"
        dur = _clip_dur_display(c)
        try:
            total += float(dur.rstrip("s"))
        except ValueError:
            pass
        tr_dur = (tr or {}).get("duration") or 0
        total -= tr_dur if isinstance(tr_dur, (int, float)) else 0
        src_s = str((c.get("reader") or {}).get("path", "")).rsplit("/", 1)[-1]
        ref = f"#{c['shot']}" if c.get("shot") else (
            f"{c['start']:g}-{c['end']:g}s"
            if isinstance(c.get("start"), (int, float))
            and isinstance(c.get("end"), (int, float)) else "整段")
        eff = ",".join(
            (e.get("type", "?") + (f"({e['id']})" if e.get("id") else ""))
            if isinstance(e, dict) else str(e)
            for e in (c.get("effects") or [])) or "-"
        speed = f" speed={c['speed']:g}" if c.get("speed") is not None else ""
        vol = f" vol={c['volume']:g}" if c.get("volume") is not None else ""
        mute = " [静音]" if (c.get("audio") or {}).get("mute") else ""
        lines.append(f"  {c.get('id')}  {src_s} {ref}  {dur}  转场:{tr_s}"
                     f"  效果:{eff}{speed}{vol}{mute}")
    n_trans = sum(1 for t in (plan.get("timeline") or [])
                  if t.get("transition"))
    lines.append(f"共 {len(plan.get('clips') or [])} 片段 / 约 {round(total, 1)}s"
                 f"（{n_trans} 个转场）")
    audio = plan.get("audio")
    if audio:
        lines.append(f"BGM: {str(audio.get('source', '')).rsplit('/', 1)[-1]}"
                     f" 音量{audio.get('volume', 0.3)}"
                     f"{' 卡点' if audio.get('snap_beats') else ''}")
    else:
        lines.append("BGM: 无")
    ovs = plan.get("overlays") or []
    if ovs:
        lines.append(f"文字/画中画 {len(ovs)} 条：" + "、".join(
            f"[{o.get('id')}] {str(o.get('text') or o.get('source') or '')[:12]}"
            for o in ovs))
    narr = plan.get("_narration")
    if narr:
        lines.append(f"旁白 {len(narr)} 组（title={plan.get('_title') or '无'}）")
    elif plan.get("_captions"):
        lines.append(f"字幕 {len(plan['_captions'])} 句"
                     f"（style={plan.get('_caption_style') or 'bottom'}）")
    if _STATE["validated"]:
        lines.append(f"上次试算：{_STATE['validated']}")
    return "\n".join(lines)


# ------------------------------------------------------------------- 工具 -- #

@tool
def set_project(filename: str, width: int, height: int, fps: int = 30) -> str:
    """Initialize (or update) the draft's output spec. Call this FIRST on a new
    task. If a draft already exists (revision turn), only the given fields
    change — everything else is preserved.

    Args:
        filename: output file name, e.g. "OUTPUT/my_video.mp4" (OUTPUT/ prefix).
        width: canvas width px (positive even number; vertical video use 720).
        height: canvas height px (positive even number; vertical video use 1280).
        fps: output fps, default 30.
    """
    plan = _draft()
    if plan is None:
        if not (isinstance(width, int) and width > 0 and width % 2 == 0
                and isinstance(height, int) and height > 0 and height % 2 == 0):
            return f"✗ width/height 必须是正偶数（收到 {width}x{height}）。"
        fn = filename if filename.startswith("OUTPUT/") else f"OUTPUT/{filename}"
        _STATE.update(
            plan={"schema_version": "3.0",
                  "output": {"filename": fn, "width": width, "height": height,
                             "fps": fps},
                  "clips": [], "timeline": [], "overlays": []},
            submitted=False, validated=None)
        return ("✓ draft 已初始化。下一步：clip_diff 批量 insert 片段（优先用 "
                "shot 编号引用 list_shots 的镜头），然后 timeline_diff / "
                "audio_diff / set_narration，最后 validate_plan → submit。\n"
                + _summary(_draft()))
    return _apply(lambda _p: ([{"key": ["output"], "type": "update",
                               "value": {"filename": filename, "width": width,
                                         "height": height, "fps": fps}}], []))


@tool
def clip_diff(ops: list) -> str:
    """Add / modify / remove CLIPS on the draft (batch: one call for 1..N ops,
    atomic — one bad op rejects the whole batch). Each op is a flat dict:

      {"op": "insert", "reader": {"type": "FFmpegReader", "path": "INPUT/a.mp4"},
       "shot": 3, "after": "c2"}          ← PREFERRED: reference the N-th shot
        from list_shots; exact boundaries are filled by the host — no timestamp
        copying. "after" omitted = append at end.
      {"op": "insert", "reader": {"type": "ImageReader", "path": "INPUT/img.jpg"},
       "duration": 3}                     ← still image, show for 3s
      {"op": "insert", "reader": {...}, "start": 12, "end": 18.5}  ← custom cut
        (only when splitting/merging shots; mutually exclusive with "shot")
      {"op": "update", "id": "c2", "end": 9.0}          ← partial: only listed
        fields change; "shot": 5 re-references a shot; "start": null clears it
      {"op": "delete", "id": "c3"}       ← also removes overlays attached to c3

    Optional per-clip fields: "speed" (0.25..4), "volume" (0..1], "audio":
    {"mute": true} or {"source": "INPUT/…", "fade_in": …, "loop": …},
    "effects": [{"type": "Brightness", "brightness": 0.2}, …], "cut_silence".
    Do NOT write "probe" — the host fills it. kind is auto-detected from the
    file extension.
    """
    def builder(plan: dict):
        actions = []
        pre_errs = []
        for i, op in enumerate(ops if isinstance(ops, list) else [], 1):
            if not isinstance(op, dict):
                return None, ["clip_diff 的每个 op 必须是对象。"]
            kind = op.get("op")
            if kind == "insert":
                value = {k: v for k, v in op.items() if k != "op"}
                errs = _validate_clip_item_v3(
                    {k: v for k, v in value.items() if k != "after"})
                if errs:
                    pre_errs.append(f"clip_diff 第 {i} 个 op："
                                    + "；".join(errs[:2]))
                    continue
                actions.append({"key": ["clips"], "type": "insert",
                                "value": value})
            elif kind in ("update", "delete"):
                cid = op.get("id")
                if cid is None:
                    return None, [f"clip_diff {kind} 缺少 id。"]
                value = {k: v for k, v in op.items()
                         if k not in ("op", "id")}
                actions.append({"key": ["clips", {"id": cid}], "type": kind,
                                "value": value or None})
            else:
                return None, [f"clip_diff 不支持 op={kind!r}"
                              f"（insert/update/delete）。"]
        return actions, pre_errs
    return _apply(builder)


@tool
def effect_diff(ops: list) -> str:
    """Attach / modify / remove EFFECTS on clips (batch, atomic). Effect types:
    Brightness, Saturation, hflip, vflip, transpose, delogo, face_mosaic.
    (Speed is NOT an effect — set the clip's "speed" via clip_diff.)

      {"op": "insert", "clip": "c2", "type": "Brightness",
       "brightness": 0.15, "contrast": 1.1}
      {"op": "insert", "clip": "c2", "type": "face_mosaic",
       "mode": "mosaic", "score_threshold": 0.4}
      {"op": "update", "id": "e1", "brightness": 0.25}   ← partial by effect id
      {"op": "delete", "id": "e1"}
    """
    def builder(_plan: dict):
        actions = []
        for op in ops if isinstance(ops, list) else []:
            if not isinstance(op, dict):
                return None, ["effect_diff 的每个 op 必须是对象。"]
            kind = op.get("op")
            if kind == "insert":
                clip = op.get("clip")
                if clip is None:
                    return None, ["effect_diff insert 缺少 clip（挂在哪个片段）。"]
                value = {k: v for k, v in op.items()
                         if k not in ("op", "clip")}
                actions.append({"key": ["clips", {"id": clip}, "effects"],
                                "type": "insert", "value": value})
            elif kind in ("update", "delete"):
                eid = op.get("id")
                if eid is None:
                    return None, [f"effect_diff {kind} 缺少效果 id。"]
                # 效果 id 全局唯一：宿主定位它挂在哪个片段
                owner = next((c.get("id") for c in (_plan.get("clips") or [])
                              for e in (c.get("effects") or [])
                              if isinstance(e, dict) and e.get("id") == eid), None)
                if owner is None and kind == "update":
                    ids = [e.get("id") for c in (_plan.get("clips") or [])
                           for e in (c.get("effects") or []) if isinstance(e, dict)]
                    return None, [f"效果 {eid} 不存在（现有：{ids or '无'}）。"]
                value = {k: v for k, v in op.items()
                         if k not in ("op", "id", "clip")}
                actions.append({"key": ["clips", {"id": owner or op.get("clip")},
                                        "effects", {"id": eid}],
                                "type": kind, "value": value or None})
            else:
                return None, [f"effect_diff 不支持 op={kind!r}。"]
        return actions, []
    return _apply(builder)


@tool
def overlay_diff(ops: list) -> str:
    """Add / modify / remove TEXT or PICTURE-IN-PICTURE overlays (batch,
    atomic, addressed by id — no index drift).

      {"op": "insert", "type": "text", "at_clip": "c1", "text": "周末骑行",
       "duration": 2, "gravity": "top", "font_size": 48, "color": "#FFFFFF"}
      {"op": "insert", "type": "pip", "at_clip": "c1", "source": "INPUT/b.mp4",
       "kind": "video", "duration": 4, "gravity": "bottom-right", "scale": 0.25}
      {"op": "update", "id": "o1", "text": "新文案", "duration": 3}
      {"op": "delete", "id": "o1"}

    start_offset (default 0) counts WITHIN the target clip (0 = when that clip
    first appears); gravity is one of top-left/top/top-right/left/center/
    right/bottom-left/bottom/bottom-right.
    """
    def builder(_plan: dict):
        actions = []
        for op in ops if isinstance(ops, list) else []:
            if not isinstance(op, dict):
                return None, ["overlay_diff 的每个 op 必须是对象。"]
            kind = op.get("op")
            if kind == "insert":
                value = {k: v for k, v in op.items() if k != "op"}
                actions.append({"key": ["overlays"], "type": "insert",
                                "value": value})
            elif kind in ("update", "delete"):
                oid = op.get("id")
                if oid is None:
                    return None, [f"overlay_diff {kind} 缺少 id。"]
                value = {k: v for k, v in op.items() if k not in ("op", "id")}
                actions.append({"key": ["overlays", {"id": oid}], "type": kind,
                                "value": value or None})
            else:
                return None, [f"overlay_diff 不支持 op={kind!r}。"]
        return actions, []
    return _apply(builder)


@tool
def timeline_diff(ops: list) -> str:
    """Reorder clips or set transitions (batch, atomic).

      {"op": "move", "clip": "c3", "after": "c1"}   ← after omitted = move to FRONT
      {"op": "transition", "clip": "c2", "type": "fade", "duration": 0.5}
        ← the transition INTO c2 (hangs on the later clip; first clip has none)
        type: fade/dissolve/wipeleft/wiperight/wipeup/wipedown/slideleft/
        slideright/slideup/slidedown/circleopen/circleclose; "none" = hard cut
      {"op": "transition", "clip": "c2", "type": "none"}   ← back to hard cut
    """
    def builder(_plan: dict):
        actions = []
        for op in ops if isinstance(ops, list) else []:
            if not isinstance(op, dict):
                return None, ["timeline_diff 的每个 op 必须是对象。"]
            kind = op.get("op")
            clip = op.get("clip")
            if clip is None:
                return None, ["timeline_diff 的 op 缺少 clip。"]
            if kind == "move":
                actions.append({"key": ["timeline", {"clip": clip}],
                                "type": "move",
                                "value": {"after": op.get("after")}})
            elif kind == "transition":
                ttype = str(op.get("type") or "")
                if ttype in ("", "none", "hard"):
                    actions.append({"key": ["timeline", {"clip": clip},
                                            "transition"],
                                    "type": "delete", "value": None})
                else:
                    actions.append({"key": ["timeline", {"clip": clip},
                                            "transition"],
                                    "type": "update",
                                    "value": {"type": ttype,
                                              "duration": op.get("duration", 0.5)}})
            else:
                return None, [f"timeline_diff 不支持 op={kind!r}（move/transition）。"]
        return actions, []
    return _apply(builder)


@tool
def audio_diff(ops: list) -> str:
    """Set / replace / clear the BACKGROUND MUSIC (tracks from list_music).

      {"op": "set", "source": "MUSIC/x.mp3", "volume": 1.0, "loop": true}
      {"op": "set", "source": "MUSIC/x.mp3", "original": "keep", "ducking": true}
      {"op": "clear"}    ← remove BGM (original sound only)

    volume (0,1] default 0.3; BY DEFAULT the music REPLACES the clips' original
    audio (original="mute") so use ~1.0. original="keep" mixes both — only then
    may you add ducking=true. snap_beats=true aligns cut points to the beat.
    """
    def builder(_plan: dict):
        actions = []
        for op in ops if isinstance(ops, list) else []:
            if not isinstance(op, dict):
                return None, ["audio_diff 的每个 op 必须是对象。"]
            kind = op.get("op")
            if kind == "clear":
                actions.append({"key": ["audio"], "type": "delete", "value": None})
            elif kind == "set":
                value = {k: v for k, v in op.items() if k != "op"}
                audio = {**(_plan.get("audio") or {}), **value}
                errs = validate_audio(audio, "audio", PROJECT_ROOT)
                if errs:
                    return None, errs
                actions.append({"key": ["audio"], "type": "update",
                                "value": value})
            else:
                return None, [f"audio_diff 不支持 op={kind!r}（set/clear）。"]
        return actions, []
    return _apply(builder)


@tool
def set_narration(groups: list, title: str | None = None) -> str:
    """Set narration (旁白制): groups cover ALL clips in narrative order.
    Each group: {"clip_ids": ["c1","c2"], "text": "整段旁白原文",
                 "units": ["句一。", "句二。"]} — units optional (compiler
    splits text by punctuation). Subtitle timing is computed by the compiler
    from each group's time window — you never write timestamps.
    Pass an empty groups list to remove narration.
    """
    plan = _draft()
    if plan is None:
        return "✗ 还没有 draft。"
    ids = {c.get("id") for c in plan.get("clips") or []}
    errs = []
    if groups:
        for i, g in enumerate(groups if isinstance(groups, list) else []):
            if not isinstance(g, dict) or not g.get("clip_ids"):
                errs.append(f"groups[{i}] 缺 clip_ids")
                continue
            bad = [x for x in g["clip_ids"] if x not in ids]
            if bad:
                errs.append(f"groups[{i}].clip_ids 引用不存在的片段 {bad}")
    if errs:
        return "✗ " + "；".join(errs)
    actions = [{"key": ["_narration"], "type": "update" if groups else "delete",
                "value": groups or None}]
    if title is not None:
        actions.append({"key": ["_title"], "type": "update", "value": title})
    elif groups:
        actions.append({"key": ["_title"], "type": "delete", "value": None})
    # 旁白与简单字幕互斥（引擎外的块级约束，就地检查）
    if groups and plan.get("_captions"):
        actions.append({"key": ["_captions"], "type": "delete", "value": None})
        actions.append({"key": ["_caption_style"], "type": "delete", "value": None})
    return _apply(lambda _p: (actions, []))


@tool
def set_captions(lines: list, style: str = "bottom") -> str:
    """Simple captions: one short line per clip, in timeline order.
    style: "bottom" (subtitle bar) or "credits" (centered ending credits).
    Pass an empty list to remove captions."""
    plan = _draft()
    if plan is None:
        return "✗ 还没有 draft。"
    if not (isinstance(lines, list) and all(
            isinstance(x, str) and x.strip() for x in lines)):
        return "✗ lines 必须是非空字符串数组（或空数组=移除）。"
    actions = [{"key": ["_captions"], "type": "update" if lines else "delete",
                "value": lines or None},
               {"key": ["_caption_style"], "type": "update", "value": style}]
    if lines and plan.get("_narration"):
        actions.append({"key": ["_narration"], "type": "delete", "value": None})
        actions.append({"key": ["_title"], "type": "delete", "value": None})
    return _apply(lambda _p: (actions, []))


@tool
def load_plan(plan: dict) -> str:
    """Load a COMPLETE plan JSON as the new draft (full replace — like opening
    a project file). Use only when the host explicitly provides a plan to
    continue from, or the select/workflow macro result needs hand-tweaks.
    The plan is upgraded to schema v3 automatically if needed.
    """
    if not isinstance(plan, dict):
        return "✗ plan 必须是 JSON 对象。"
    v3 = plan_dialect.normalize(plan)
    _STATE.update(plan=v3, submitted=False, validated=None)
    return "✓ 已载入计划（全量替换）。\n" + _summary(v3)


@tool
def get_plan() -> str:
    """Show the current draft: clip table (id / source / range / duration /
    transition / effects with ids), BGM, overlays with ids, narration,
    estimated total length and last validation result. Call this whenever you
    are unsure what the draft looks like so far."""
    plan = _draft()
    if plan is None:
        return "还没有 draft（先 set_project 初始化）。"
    return _summary(plan)


@tool
def validate_plan() -> str:
    """Run FULL validation on the draft (cross-clip rules: transitions vs
    clip lengths, overlay bounds, narration shapes...). Always call this
    before submit; fix reported issues with the diff tools, then submit."""
    plan = _draft()
    if plan is None:
        return "✗ 还没有 draft。"
    errs = plan_dialect.validate_any(_probe_filled_copy(plan), PROJECT_ROOT)
    if errs:
        _STATE["validated"] = f"✗ {len(errs)} 处问题"
        return "试算发现以下问题（用工具修正后再 submit）：\n- " \
            + "\n- ".join(errs)
    _STATE["validated"] = "✓ 通过"
    return "✓ 试算通过，可以 submit。"


@tool
def submit() -> str:
    """Submit the draft for compilation and rendering. Runs full validation
    first; on success the host compiles (dry-run → render → verify) and the
    draft becomes the session's current plan. Ask via ask_user instead if the
    request cannot be fulfilled."""
    plan = _draft()
    if plan is None:
        return "✗ 还没有 draft。"
    if not plan.get("clips"):
        return "✗ draft 里还没有片段（先 clip_diff insert）。"
    errs = plan_dialect.validate_any(_probe_filled_copy(plan), PROJECT_ROOT)
    if errs:
        _STATE["validated"] = f"✗ {len(errs)} 处问题"
        return "✗ submit 被拦下（未提交），先用工具修正：\n- " \
            + "\n- ".join(errs)
    _STATE["submitted"] = True
    _STATE["validated"] = "✓ 通过（已提交）"
    return "✓ 已提交编译执行。\n" + _summary(plan)


PLAN_TOOLS = [set_project, clip_diff, effect_diff, overlay_diff,
              timeline_diff, audio_diff, set_narration, set_captions,
              load_plan, get_plan, validate_plan, submit]

# 修订轮 brief 用的词汇表（模型侧唯一的 op 语法说明；与各 diff 工具 docstring 同语义）
ACTIONS_DOC = """修订工具与 op 语法（draft 已载入上一版计划，直接改，不要 set_project 重建）：
五个 diff 工具，每个接受一组 op（数组、批内原子——一条错整批拒）：

clip_diff(ops)   片段增删改
- {"op":"insert","reader":{"type":"FFmpegReader","path":"INPUT/a.mp4"},"shot":3,"after":"c2"}
  新增（after 省略=追加到末尾；shot 编号引用 list_shots 的镜头，或写 start/end 自定义裁剪，二选一）
- {"op":"update","id":"c2","end":9.0}      只改传入字段；"start":null 表示清除该字段
- {"op":"delete","id":"c3"}                删除（连它挂载的文字/画中画一起清）
  片段可选字段：speed(0.25~4)/volume((0,1])/audio:{"mute":true}/cut_silence
effect_diff(ops) 效果（Brightness/Saturation/hflip/vflip/transpose/delogo/face_mosaic）
- {"op":"insert","clip":"c2","type":"Brightness","brightness":0.15}
- {"op":"update","id":"e1","brightness":0.25}   / {"op":"delete","id":"e1"}
overlay_diff(ops) 文字/画中画（按 id 寻址）
- {"op":"insert","type":"text","at_clip":"c1","text":"标题","duration":2,"gravity":"top"}
- {"op":"update","id":"o1","text":"新文案","duration":3}   / {"op":"delete","id":"o1"}
timeline_diff(ops) 顺序与转场
- {"op":"move","clip":"c3","after":"c1"}   （after 省略=移到最前）
- {"op":"transition","clip":"c2","type":"fade","duration":0.5}   （type:"none"=硬切）
audio_diff(ops) 全局 BGM
- {"op":"set","source":"MUSIC/x.mp3","volume":1.0,"loop":true}   / {"op":"clear"}
set_narration(groups, title) / set_captions(lines, style)   文案（互斥）

改完 validate_plan() 试算 → submit() 提交。规则：只对用户要求的部分下 op；
clip id / overlay id / 效果 id 一律从片段表或 get_plan() 里逐字引用。"""
