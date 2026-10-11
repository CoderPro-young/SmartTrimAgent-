"""V9.0 P2：修订即操作。apply_ops(last_plan, ops) → 新计划（纯函数）。

设计契约见 docs/v9.0-understanding-layer.md §4。结构保障：
ops 没碰的字段逐字段保留（深拷贝后只改目标）——图片不会悄悄消失，
「局部改动放大成全局」在结构上不合法（全局改动必须显式 set_audio）。
非法 op 全批拒绝，errors 直接回传 plan_with_retry 的重试闭环。
"""
from __future__ import annotations

import copy
import os
import re

import plan_schema

_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".jfif", ".webp", ".bmp", ".gif",
              ".tif", ".tiff", ".heic", ".avif"}

# 给修订 brief 用的词汇表（模型侧唯一可见的 ops 说明）
OPS_DOC = """可用操作（ops，每个是对象，按顺序执行）：
- {"op":"remove_clip","clip":"C11"}                      删除片段
- {"op":"add_clip","source":"INPUT/x.mp4","trim_start":0,"trim_end":5,"after":"C3"}
  新增片段（after 省略 = 追加到末尾；source 必须是 INPUT/ 里存在的素材）
- {"op":"set_clip_trim","clip":"C4","trim_start":44.1,"trim_end":54.1}   只改裁剪区间
- {"op":"move_clip","clip":"C7","after":"C2"}             调整顺序（after 省略 = 移到最前）
- {"op":"mute_clip","clip":"C1","on":true}                静音/取消静音某片段原声
- {"op":"set_transition","clip":"C5","type":"fade","duration":0.4}       改转场（type="none" = 硬切）
- {"op":"set_audio","audio":null}                         删除全局 BGM（必须显式 null）
- {"op":"set_audio","audio":{"source":"MUSIC/x.mp3","volume":0.8,"loop":true}}   设置/替换全局 BGM
规则：只对用户要求的部分下 ops，其余片段一律不碰；ops 表达不了就调用 ask_user，
不要退回重写整份计划。"""


def _err(errors: list, msg: str):
    errors.append(msg)


def _find_clip(plan: dict, cid, errors: list, idx: int):
    for c in plan.get("clips") or []:
        if c.get("id") == cid:
            return c
    _err(errors, f"第 {idx} 个操作：片段 {cid} 不存在（上一版计划里没有这个 id）。")
    return None


def _tl_entry(plan: dict, cid: str):
    for t in plan.get("timeline") or []:
        if t.get("clip") == cid:
            return t
    return None


def _next_id(plan: dict) -> str:
    mx = 0
    for c in plan.get("clips") or []:
        m = re.match(r"^[Cc](\d+)$", str(c.get("id") or ""))
        if m:
            mx = max(mx, int(m.group(1)))
    return f"C{mx + 1}"


def _norm_source(src: str, materials: list[str] | None, errors: list, idx: int) -> str | None:
    s = str(src or "").replace("\\", "/").strip()
    if not s:
        _err(errors, f"第 {idx} 个操作：缺少 source。")
        return None
    if not s.startswith("INPUT/"):
        s = "INPUT/" + s
    if materials is not None:
        base = s.split("/", 1)[1]
        if base not in materials and s not in materials:
            _err(errors, f"第 {idx} 个操作：素材 {s} 不在 INPUT/ 里，不能新增。")
            return None
    return s


def _num_or_none(x):
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "bad"
    return v


def apply_ops(base_plan: dict, ops, materials: list[str] | None = None):
    """对上一版计划施加编辑操作。返回 (new_plan, applied, errors)。

    errors 非空 ⇒ 全批拒绝（new_plan 返回 None），由重试闭环回传给模型。
    """
    errors: list[str] = []
    applied: list[dict] = []
    if not isinstance(base_plan, dict):
        return None, [], ["上一版计划缺失，无法施加编辑操作。"]
    if not isinstance(ops, list) or not ops:
        return None, [], ["edit_plan.ops 必须是非空的操作数组。"]

    plan = copy.deepcopy(base_plan)
    plan.setdefault("clips", [])
    plan.setdefault("timeline", [])

    for idx, op in enumerate(ops, 1):
        if not isinstance(op, dict) or not op.get("op"):
            _err(errors, f"第 {idx} 个操作：必须是含 op 字段的对象。")
            continue
        kind = op.get("op")

        if kind == "remove_clip":
            cid = op.get("clip")
            c = _find_clip(plan, cid, errors, idx)
            if c is None:
                continue
            plan["clips"] = [x for x in plan["clips"] if x.get("id") != cid]
            plan["timeline"] = [t for t in plan["timeline"] if t.get("clip") != cid]
            applied.append({"op": kind, "detail": f"删除 {cid}（{os.path.basename(str(c.get('source')))}）"})

        elif kind == "add_clip":
            src = _norm_source(op.get("source"), materials, errors, idx)
            if src is None:
                continue
            ts = _num_or_none(op.get("trim_start"))
            te = _num_or_none(op.get("trim_end"))
            if ts == "bad" or te == "bad" or (ts is not None and ts < 0) or \
                    (te is not None and ts is not None and te <= ts):
                _err(errors, f"第 {idx} 个操作：trim_start/trim_end 必须是合法秒数且 end > start。")
                continue
            ext = os.path.splitext(src)[1].lower()
            cid = _next_id(plan)
            clip = {"id": cid, "source": src,
                    "kind": "image" if ext in _IMAGE_EXT else "video"}
            if ts is not None:
                clip["trim_start"] = ts
            if te is not None:
                clip["trim_end"] = te
            after = op.get("after")
            if after is not None:
                ref = _find_clip(plan, after, errors, idx)
                if ref is None:
                    continue
                pos = next(i for i, x in enumerate(plan["clips"]) if x.get("id") == after) + 1
            else:
                pos = len(plan["clips"])
            plan["clips"].insert(pos, clip)
            plan["timeline"].insert(pos, {"clip": cid, "transition": None})
            applied.append({"op": kind, "detail": f"新增 {cid}（{os.path.basename(src)}）"})

        elif kind == "set_clip_trim":
            c = _find_clip(plan, op.get("clip"), errors, idx)
            if c is None:
                continue
            ts = _num_or_none(op.get("trim_start"))
            te = _num_or_none(op.get("trim_end"))
            if ts == "bad" or te == "bad":
                _err(errors, f"第 {idx} 个操作：trim 必须是数值。")
                continue
            if ts is not None:
                c["trim_start"] = ts
            if te is not None:
                c["trim_end"] = te
            applied.append({"op": kind, "detail": f"{c.get('id')} 裁剪改为 "
                                                  f"{ts if ts is not None else c.get('trim_start', 0)}"
                                                  f"-{te if te is not None else c.get('trim_end', '末尾')}s"})

        elif kind == "move_clip":
            cid = op.get("clip")
            if _find_clip(plan, cid, errors, idx) is None:
                continue
            clip = next(x for x in plan["clips"] if x.get("id") == cid)
            entry = _tl_entry(plan, cid)
            plan["clips"] = [x for x in plan["clips"] if x.get("id") != cid]
            plan["timeline"] = [t for t in plan["timeline"] if t.get("clip") != cid]
            after = op.get("after")
            if after:
                if _find_clip(plan, after, errors, idx) is None:
                    continue
                pos = next(i for i, x in enumerate(plan["clips"]) if x.get("id") == after) + 1
            else:
                pos = 0
            plan["clips"].insert(pos, clip)
            plan["timeline"].insert(pos, entry or {"clip": cid, "transition": None})
            applied.append({"op": kind, "detail": f"{cid} 移动到 {after or '最前'} 之后"})

        elif kind == "mute_clip":
            c = _find_clip(plan, op.get("clip"), errors, idx)
            if c is None:
                continue
            on = bool(op.get("on", True))
            if on:
                c["audio"] = {**(c.get("audio") or {}), "mute": True}
            else:
                a = c.get("audio") or {}
                a.pop("mute", None)
                if a:
                    c["audio"] = a
                else:
                    c.pop("audio", None)
            applied.append({"op": kind, "detail": f"{c.get('id')} {'静音' if on else '取消静音'}"})

        elif kind == "set_transition":
            cid = op.get("clip")
            if _find_clip(plan, cid, errors, idx) is None:
                continue
            entry = _tl_entry(plan, cid)
            if entry is None:
                _err(errors, f"第 {idx} 个操作：{cid} 没有时间轴条目。")
                continue
            ttype = op.get("type")
            dur = _num_or_none(op.get("duration"))
            if dur == "bad" or (dur is not None and dur <= 0):
                _err(errors, f"第 {idx} 个操作：duration 必须为正秒数。")
                continue
            if ttype in (None, "", "none", "hard"):
                entry["transition"] = None
            else:
                if ttype not in plan_schema.TRANSITIONS:
                    _err(errors, f"第 {idx} 个操作：转场 {ttype} 不在白名单 "
                                 f"{sorted(plan_schema.TRANSITIONS)} 内。")
                    continue
                entry["transition"] = {"type": ttype,
                                       "duration": dur if dur is not None else 0.3}
            applied.append({"op": kind, "detail": f"{cid} 转场改为 "
                                                  f"{'硬切' if entry['transition'] is None else _fmt(entry['transition'])}"})

        elif kind == "set_audio":
            audio = op.get("audio", "unset")
            if audio is None:
                if "audio" in plan:
                    del plan["audio"]
                applied.append({"op": kind, "detail": "删除全局 BGM"})
            elif isinstance(audio, dict):
                plan["audio"] = copy.deepcopy(audio)
                applied.append({"op": kind,
                                "detail": f"设置全局 BGM：{audio.get('source', '?')}"})
            else:
                _err(errors, f"第 {idx} 个操作：set_audio 的 audio 必须是对象或 null。")

        else:
            _err(errors, f"第 {idx} 个操作：未知操作 {kind}。")

    if errors:
        return None, applied, errors
    return plan, applied, []


def _fmt(tr: dict) -> str:
    return f"{tr.get('type')} {tr.get('duration')}s"
