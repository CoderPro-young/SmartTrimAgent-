"""V12 action 引擎：openshot ApplyJsonDiff 信封语义在 v3 草稿上的实现。

协议（docs/v12-protocol.md §2）：
    {"key": ["clips", {"id": "c1"}], "type": "update", "value": {"end": 9.0}}

- 三动作 insert / update / delete + 我方扩展动词 move（数组序模型的等价物，
  openshot 的 position 更新在我们模型里就是 move）；
- **部分更新**：缺失字段保持原值；显式 null = 清除该字段（openshot 是跳过
  null——我们选清除，让 LLM 能用 {"start": null} 表达「取消自定义裁剪」）；
- **整段替换**：update 里出现 reader / effects 即整段重建（openshot 语义）；
- **批内原子**：任一条错则整批拒绝（逐条错误回传）——比 openshot 的
  「边应用边炸不回滚」安全，因为 draft 在内存、submit 前无副作用；
- **未命中 id 报错并附可用清单**（openshot 静默跳过——刻意反向，防
  「以为改成了」，并让修复闭环一轮到位）；
- applied 日志记录每条的 old_values（撤销预留：将来「撤销上一次修订」=
  反转重放，本期不实现撤销 UI）。

引擎是纯函数集合：不 IO、不改入参。单步轻校验（引用存在/白名单/类型）在
引擎层就地做；跨片段全量规则（转场<片段长、overlay 越界…）仍在
validate_plan/submit 做——片段没搭完时全量规则会误报（V10 同款分层）。

LLM 不直接写信封：五个 diff 工具（plan_tools）把扁平 op 组装成信封后进引擎。
"""

from __future__ import annotations

import copy
import re

import plan_schema
from skills import resolve_effect, v3_effects_menu

# ---- 动作类型（move 为我方扩展）----
ENVELOPE_TYPES = ("insert", "update", "delete", "move")

# ---- v3 各层字段白名单（未知字段零容忍；probe 是宿主侧数据，LLM 不可写）----
CLIP_KEYS_V3 = {"id", "reader", "shot", "start", "end", "duration", "speed",
                "volume", "audio", "effects", "cut_silence", "cut_black"}
CLIP_AUDIO_KEYS_V3 = {"mute", "source", "fade_in", "fade_out", "loop"}
EFFECT_KEYS_V3 = {"id", "type"}
OVERLAY_TEXT_KEYS_V3 = {"id", "type", "at_clip", "start_offset", "duration",
                        "gravity", "text", "font_size", "color", "y_margin"}
OVERLAY_PIP_KEYS_V3 = {"id", "type", "at_clip", "start_offset", "duration",
                       "gravity", "source", "kind", "scale"}
TRANSITION_KEYS_V3 = {"type", "duration"}
OUTPUT_KEYS_V3 = {"filename", "width", "height", "fps"}
AUDIO_KEYS_V3 = {"source", "volume", "fade_in", "fade_out", "loop", "ducking",
                 "original", "snap_beats"}
INTENT_KEYS_V3 = {"_narration", "_captions", "_caption_style", "_title"}

GRAVITIES = plan_schema.POSITIONS
TRANSITIONS = plan_schema.TRANSITIONS


def _err(errors: list, msg: str) -> None:
    errors.append(msg)


def _seg_id(seg) -> str | None:
    """key 段取 id：宽松解析（{"id": X} / {"clip": X} / 裸字符串 "X"）。"""
    if isinstance(seg, str):
        return seg
    if isinstance(seg, dict):
        return seg.get("id") or seg.get("clip")
    return None


def _find_clip(plan: dict, cid):
    for c in plan.get("clips") or []:
        if isinstance(c, dict) and c.get("id") == cid:
            return c
    return None


def _clip_ids(plan: dict) -> list[str]:
    return [c.get("id") for c in (plan.get("clips") or []) if isinstance(c, dict)]


def _ids_suffix(plan: dict) -> str:
    ids = "、".join(str(i) for i in _clip_ids(plan)) or "（无）"
    return f"可用的片段 id：{ids}。"


def next_clip_id(plan: dict) -> str:
    mx = 0
    for cid in _clip_ids(plan):
        m = re.match(r"^[Cc](\d+)$", str(cid or ""))
        if m:
            mx = max(mx, int(m.group(1)))
    return f"c{mx + 1}"


def _check_keys(value: dict, allowed: set, tag: str) -> list[str]:
    extra = [k for k in value if k not in allowed]
    if not extra:
        return []
    return [f"{tag} 含不支持的字段 {extra}；该层只接受 {sorted(allowed)}。"
            f"多写的字段会被静默忽略（等于没做），因此直接拒绝。"]


def _normalize_effect_part(eff: dict) -> tuple[dict | None, list[str]]:
    """v3 效果对象 {type, 参数平铺} → 规整后的 dict；(None, errors) = 不合法。
    speed 特殊：归位为 clip 平级标量，返回 {"type": "speed", "factor": …} 令牌
    由调用方转写。"""
    errors: list[str] = []
    typ = eff.get("type")
    resolved, params = resolve_effect(typ)
    if resolved is None:
        return None, [f"效果 type {typ!r} 不在白名单 {v3_effects_menu()} 内"
                      f"（收到 {typ!r}）。"]
    # type 保留 LLM 写的 v3 名（Brightness/Saturation/原名均可——v2 名作为
    # 别名受理；内部名映射只发生在方言层降级时，草稿永远是 v3 词汇）
    out = {"type": typ}
    for k, v in eff.items():
        if k in EFFECT_KEYS_V3:
            continue
        if k not in params:
            _err(errors, f"效果 {typ} 不接受参数 {k!r}；只接受 {sorted(params)}。")
            continue
        out[k] = copy.deepcopy(v)
    if errors:
        return None, errors
    return out, []


def _detach_speed(effects: list[dict]) -> tuple[list[dict], float | None]:
    """效果列表里的 speed 归位为标量：(剩余效果, speed|None)。"""
    rest, speed = [], None
    for e in effects:
        if e.get("type") == "speed":
            speed = e.get("factor")
        else:
            rest.append(e)
    return rest, speed


# --------------------------------------------------------------------------- #
# 各路由的实现（返回 (detail, old_values)；错误直接 append 进 errors）
# --------------------------------------------------------------------------- #

def _apply_clips(work: dict, act: dict, idx: int, errors: list,
                 applied: list) -> None:
    typ = act["type"]
    key = act["key"]
    value = act.get("value")

    if len(key) == 1:                                   # ["clips"] → insert
        if typ != "insert":
            _err(errors, f"第 {idx} 个操作：key ['clips'] 只支持 insert"
                         f"（update/delete 需要指到具体片段："
                         f"['clips', {{'id': …}}]）。")
            return
        if not isinstance(value, dict):
            _err(errors, f"第 {idx} 个操作：insert 片段的 value 必须是对象"
                         f"（至少含 reader: {{'type': 'FFmpegReader', 'path': …}}）。")
            return
        after = value.pop("after", None)
        item = {k: copy.deepcopy(v) for k, v in value.items()}
        errs = _check_keys(item, CLIP_KEYS_V3, "insert 片段")
        errors.extend(errs)
        if errs:
            return
        cid = item.get("id")
        if cid is None:
            cid = next_clip_id(work)
            item["id"] = cid
        elif _find_clip(work, cid) is not None:
            _err(errors, f"第 {idx} 个操作：片段 id {cid} 已存在（insert 用于新建；"
                         f"改现有片段请用 update）。{_ids_suffix(work)}")
            return
        reader = item.get("reader")
        if not (isinstance(reader, dict) and isinstance(reader.get("path"), str)
                and reader["path"]) and item.get("shot") is None:
            _err(errors, f"第 {idx} 个操作：新片段需要 reader{{type, path}}"
                         f"或 shot 编号引用。")
            return
        effects = item.pop("effects", None)
        if effects is not None:
            if not isinstance(effects, list):
                _err(errors, f"第 {idx} 个操作：effects 必须是数组。")
                return
            norm = []
            for j, eff in enumerate(effects):
                if not isinstance(eff, dict):
                    _err(errors, f"第 {idx} 个操作：effects[{j}] 必须是对象。")
                    return
                e, e_errs = _normalize_effect_part(eff)
                if e_errs:
                    errors.extend(f"第 {idx} 个操作（effects[{j}]）：{m}" for m in e_errs)
                    return
                norm.append(e)
            norm, spd = _detach_speed(norm)
            if spd is not None:
                item["speed"] = spd
            if norm:
                eid_n = _max_effect_id(work)
                for e in norm:
                    eid_n += 1
                    e["id"] = f"e{eid_n}"
                item["effects"] = norm
        ca = item.get("audio")
        if isinstance(ca, dict):
            if "volume" in ca:
                _err(errors, f"第 {idx} 个操作：clip.audio 里不写 volume——"
                             f"音量是 clip 平级字段：{{'volume': 0.8}}。")
                return
            errs = _check_keys(ca, CLIP_AUDIO_KEYS_V3, "clip.audio")
            errors.extend(errs)
            if errs:
                return
        pos = _insert_pos(work, after, errors, idx)
        if pos is None:
            return
        work.setdefault("clips", []).insert(pos, item)
        work.setdefault("timeline", []).insert(
            pos, {"clip": item["id"], "transition": None})
        applied.append({"op": "insert", "target": f"clips/{item['id']}",
                        "detail": f"新增 {item['id']}（"
                                  f"{str((item.get('reader') or {}).get('path', '?')).rsplit('/', 1)[-1]}"
                                  f"{' #' + str(item['shot']) if item.get('shot') else ''}）",
                        "old_values": None})
        return

    if len(key) == 2:                                   # ["clips", {id}]
        cid = _seg_id(key[1])
        c = _find_clip(work, cid) if cid is not None else None
        if c is None:
            _err(errors, f"第 {idx} 个操作：片段 {cid} 不存在。{_ids_suffix(work)}")
            return
        if typ == "delete":
            old = copy.deepcopy(c)
            work["clips"] = [x for x in work["clips"] if x is not c]
            work["timeline"] = [t for t in (work.get("timeline") or [])
                                if t.get("clip") != cid]
            n_ov = len(work.get("overlays") or [])
            work["overlays"] = [o for o in (work.get("overlays") or [])
                                if o.get("at_clip") != cid]
            dropped = n_ov - len(work.get("overlays") or [])
            # 旁白组引用级联清理（2026-10-29 sim-v12-a A3 实测：删片段后
            # 引用它的旁白组让计划过不了校验，模型只能停下来问用户）
            narr_note = ""
            narr = work.get("_narration")
            if isinstance(narr, list):
                kept = []
                n_groups_before = len(narr)
                for g in narr:
                    if isinstance(g, dict) and isinstance(g.get("clip_ids"), list):
                        g = {**g, "clip_ids": [x for x in g["clip_ids"] if x != cid]}
                        if not g["clip_ids"]:
                            continue          # 只覆盖被删片段的组整个移除
                    kept.append(g)
                if len(kept) != n_groups_before:
                    if kept:
                        work["_narration"] = kept
                    else:
                        work.pop("_narration", None)
                    narr_note = f"；旁白组 {n_groups_before}→{len(kept)}（移除引用 {cid} 的组）"
            applied.append({"op": "delete", "target": f"clips/{cid}",
                            "detail": f"删除 {cid}"
                                      f"{'（连带移除挂载文字/画中画）' if dropped else ''}"
                                      f"{narr_note}",
                            "old_values": {"clip": old}})
            return
        if typ != "update":
            _err(errors, f"第 {idx} 个操作：片段 {cid} 上只支持 update/delete。")
            return
        if not isinstance(value, dict) or not value:
            _err(errors, f"第 {idx} 个操作：update 的 value 必须是非空对象"
                         f"（只带要改的字段）。")
            return
        errs = _check_keys(value, CLIP_KEYS_V3, f"update {cid}")
        errors.extend(errs)
        if errs:
            return
        old_values: dict = {}
        for k, v in value.items():
            old_values[k] = copy.deepcopy(c.get(k)) if k in c else None
            if k == "effects":
                # 整段替换（openshot 语义），id 缺失的自动补
                new_effects = []
                if v is not None:
                    for j, eff in enumerate(v if isinstance(v, list) else []):
                        if not isinstance(eff, dict):
                            errors.append(f"第 {idx} 个操作：effects[{j}] 必须是对象。")
                            break
                        e, e_errs = _normalize_effect_part(eff)
                        if e_errs:
                            errors.extend(f"第 {idx} 个操作（effects[{j}]）：{m}"
                                          for m in e_errs)
                            break
                        new_effects.append(e)
                    else:
                        new_effects, spd = _detach_speed(new_effects)
                        if spd is not None:
                            c["speed"] = spd
                        eid_n = _max_effect_id(work)
                        for e in new_effects:
                            eid_n += 1
                            e.setdefault("id", f"e{eid_n}")
                        if new_effects:
                            c["effects"] = new_effects
                        elif not spd:
                            c.pop("effects", None)
                else:
                    c.pop("effects", None)
                continue
            if k == "audio":
                if v is None:
                    c.pop("audio", None)
                    continue
                if not isinstance(v, dict):
                    _err(errors, f"第 {idx} 个操作：audio 必须是对象。")
                    continue
                if "volume" in v:
                    _err(errors, f"第 {idx} 个操作：clip.audio 里不写 volume——"
                                 f"音量是 clip 平级字段。")
                    continue
                ca = dict(c.get("audio") or {})
                for ak, av in v.items():
                    if av is None:
                        ca.pop(ak, None)
                    else:
                        ca[ak] = copy.deepcopy(av)
                if ca:
                    c["audio"] = ca
                else:
                    c.pop("audio", None)
                continue
            if v is None:
                c.pop(k, None)                 # 显式 null = 清除字段
            else:
                c[k] = copy.deepcopy(v)
        applied.append({"op": "update", "target": f"clips/{cid}",
                        "detail": f"更新 {cid}：{', '.join(value)}",
                        "old_values": old_values})
        return

    # ["clips", {id}, "effects"] / ["clips", {id}, "effects", {eid}] —— clip 内效果
    if len(key) in (3, 4) and key[2] == "effects":
        cid = _seg_id(key[1])
        c = _find_clip(work, cid) if cid is not None else None
        if c is None:
            _err(errors, f"第 {idx} 个操作：片段 {cid} 不存在。{_ids_suffix(work)}")
            return
        eid = _seg_id(key[3]) if len(key) == 4 else None
        effects = c.setdefault("effects", []) if typ == "insert" \
            else (c.get("effects") or [])
        target = next((e for e in effects
                       if isinstance(e, dict) and e.get("id") == eid), None) \
            if eid is not None else None
        if typ == "insert":
            if len(key) == 4:
                _err(errors, f"第 {idx} 个操作：insert 效果用三段 key "
                             f"['clips', {{'id': …}}, 'effects']（id 由宿主分配）。")
                return
            if not isinstance(value, dict):
                _err(errors, f"第 {idx} 个操作：insert 效果的 value 必须是对象"
                             f"（至少含 type）。")
                return
            e, e_errs = _normalize_effect_part(value)
            if e_errs:
                errors.extend(f"第 {idx} 个操作：{m}" for m in e_errs)
                return
            rest, spd = _detach_speed([e])
            if spd is not None:
                c["speed"] = spd
                applied.append({"op": "insert", "target": f"clips/{cid}/speed",
                                "detail": f"{cid} 变速设为 {spd}（speed 是 clip 平级标量）",
                                "old_values": {"speed": c.get("speed")}})
                return
            if not rest:
                return
            eid_n = _max_effect_id(work) + 1
            rest[0]["id"] = f"e{eid_n}"
            c.setdefault("effects", []).append(rest[0])
            applied.append({"op": "insert", "target": f"clips/{cid}/effects/e{eid_n}",
                            "detail": f"{cid} 挂效果 {rest[0]['type']}（e{eid_n}）",
                            "old_values": None})
            return
        if target is None:
            have = [e.get("id") for e in effects if isinstance(e, dict)]
            _err(errors, f"第 {idx} 个操作：效果 {eid} 不在片段 {cid} 上"
                         f"（现有：{have or '无'}）。")
            return
        if typ == "delete":
            old = copy.deepcopy(target)
            c["effects"] = [e for e in effects if e is not target]
            if not c["effects"]:
                c.pop("effects", None)
            applied.append({"op": "delete", "target": f"clips/{cid}/effects/{eid}",
                            "detail": f"移除 {cid} 的效果 {target.get('type')}（{eid}）",
                            "old_values": {"effect": old}})
            return
        if typ == "update":
            if not isinstance(value, dict) or not value:
                _err(errors, f"第 {idx} 个操作：update 效果的 value 必须是非空对象。")
                return
            old = copy.deepcopy(target)
            new_type = value.get("type")
            if new_type is not None and new_type != target.get("type"):
                # 换类型 = 同 id 整体重建（type 不可部分更新）
                e, e_errs = _normalize_effect_part(dict(value, id=eid))
                if e_errs:
                    errors.extend(f"第 {idx} 个操作：{m}" for m in e_errs)
                    return
                rest, spd = _detach_speed([e])
                if spd is not None or not rest:
                    _err(errors, f"第 {idx} 个操作：效果不能改成 speed——"
                                 f"变速是 clip 平级标量，用 clips/{cid} 的 update。")
                    return
                target.clear()
                target.update(rest[0])
            else:
                merged = {k: v for k, v in target.items()
                          if k not in ("id", "type")}
                for k, v in value.items():
                    if k in ("id", "type"):
                        continue
                    if v is None:
                        merged.pop(k, None)
                    else:
                        merged[k] = v
                e, e_errs = _normalize_effect_part(dict(merged, type=target["type"]))
                if e_errs:
                    errors.extend(f"第 {idx} 个操作：{m}" for m in e_errs)
                    return
                keep_id = target["id"]
                target.clear()
                target.update(e)
                target["id"] = keep_id
            applied.append({"op": "update", "target": f"clips/{cid}/effects/{eid}",
                            "detail": f"更新 {cid} 的效果 {eid}",
                            "old_values": {"effect": old}})
            return
        _err(errors, f"第 {idx} 个操作：效果上只支持 insert/update/delete。")
        return

    _err(errors, f"第 {idx} 个操作：clips 的 key 形态不认识：{key}。")


def _max_effect_id(plan: dict) -> int:
    mx = 0
    for c in plan.get("clips") or []:
        for e in (c.get("effects") or []) if isinstance(c, dict) else []:
            m = re.match(r"^e(\d+)$", str((e or {}).get("id") or ""))
            if m:
                mx = max(mx, int(m.group(1)))
    return mx


def _insert_pos(work: dict, after, errors: list, idx: int) -> int | None:
    if after is None:
        return len(work.get("clips") or [])
    ref = _find_clip(work, after)
    if ref is None:
        _err(errors, f"第 {idx} 个操作：after={after} 不存在。{_ids_suffix(work)}")
        return None
    return next(i for i, x in enumerate(work["clips"]) if x is ref) + 1


def _apply_timeline(work: dict, act: dict, idx: int, errors: list,
                    applied: list) -> None:
    typ, key, value = act["type"], act["key"], act.get("value")
    tl = work.setdefault("timeline", [])

    if len(key) == 1:
        _err(errors, f"第 {idx} 个操作：timeline 由片段顺序自动维护——"
                     f"用 move 调顺序、transition 设转场，不直接写 timeline 数组。")
        return
    cid = _seg_id(key[1])
    pos = next((i for i, t in enumerate(tl) if t.get("clip") == cid), None)
    if pos is None:
        _err(errors, f"第 {idx} 个操作：片段 {cid} 不在时间轴里。{_ids_suffix(work)}")
        return

    if len(key) == 2:
        if typ != "move":
            _err(errors, f"第 {idx} 个操作：timeline 条目只支持 move"
                         f"（转场请指到 ['timeline', {{'clip': …}}, 'transition']）。")
            return
        after = (value or {}).get("after")
        if after == cid:
            _err(errors, f"第 {idx} 个操作：不能把片段移动到自己后面。")
            return
        entry = tl.pop(pos)
        ci = next(i for i, x in enumerate(work["clips"]) if x.get("id") == cid)
        clip_entry = work["clips"].pop(ci)
        if after is None:
            tl.insert(0, entry)
            work["clips"].insert(0, clip_entry)
        else:
            tpos = next((i for i, t in enumerate(tl) if t.get("clip") == after), None)
            if tpos is None:
                tl.insert(pos, entry)           # 还原
                work["clips"].insert(ci, clip_entry)
                _err(errors, f"第 {idx} 个操作：after={after} 不在时间轴里。"
                             f"{_ids_suffix(work)}")
                return
            tl.insert(tpos + 1, entry)
            cpos = next(i for i, x in enumerate(work["clips"])
                        if x.get("id") == after)
            work["clips"].insert(cpos + 1, clip_entry)
        applied.append({"op": "move", "target": f"timeline/{cid}",
                        "detail": f"{cid} 移动到 {after or '最前'} 之后",
                        "old_values": {"position": pos}})
        return

    if len(key) == 3 and key[2] == "transition":
        if typ == "move":
            _err(errors, f"第 {idx} 个操作：转场不支持 move。")
            return
        if pos == 0 and typ in ("insert", "update"):
            _err(errors, f"第 {idx} 个操作：{cid} 是首个片段，没有前置转场"
                         f"（首段即硬切）。")
            return
        if typ == "delete":
            old = copy.deepcopy(tl[pos].get("transition"))
            tl[pos]["transition"] = None
            applied.append({"op": "delete", "target": f"timeline/{cid}/transition",
                            "detail": f"{cid} 前置转场改为硬切",
                            "old_values": {"transition": old}})
            return
        if not isinstance(value, dict):
            _err(errors, f"第 {idx} 个操作：transition 的 value 必须是对象"
                         f"{{'type': …, 'duration': …}}。")
            return
        errs = _check_keys(value, TRANSITION_KEYS_V3, "transition")
        errors.extend(errs)
        if errs:
            return
        ttype = value.get("type")
        if ttype is not None and ttype not in TRANSITIONS:
            _err(errors, f"第 {idx} 个操作：转场类型 {ttype!r} 不在白名单 "
                         f"{sorted(TRANSITIONS)} 内。")
            return
        old = copy.deepcopy(tl[pos].get("transition"))
        merged = dict(old or {})
        for k, v in value.items():
            if v is None:
                merged.pop(k, None)
            else:
                merged[k] = v
        if "type" not in merged:
            _err(errors, f"第 {idx} 个操作：转场缺少 type。")
            return
        if not (isinstance(merged.get("duration"), (int, float))
                and merged["duration"] > 0):
            _err(errors, f"第 {idx} 个操作：转场 duration 必须为正秒数。")
            return
        tl[pos]["transition"] = merged
        applied.append({"op": typ, "target": f"timeline/{cid}/transition",
                        "detail": f"{cid} 前置转场 = {merged['type']} "
                                  f"{merged['duration']}s",
                        "old_values": {"transition": old}})
        return

    _err(errors, f"第 {idx} 个操作：timeline 的 key 形态不认识：{key}。")


def _apply_overlays(work: dict, act: dict, idx: int, errors: list,
                    applied: list) -> None:
    typ, key, value = act["type"], act["key"], act.get("value")
    ovs = work.setdefault("overlays", [])

    if len(key) == 1:
        if typ != "insert":
            _err(errors, f"第 {idx} 个操作：key ['overlays'] 只支持 insert"
                         f"（update/delete 需要指到具体 overlay："
                         f"['overlays', {{'id': …}}]）。")
            return
        if not isinstance(value, dict):
            _err(errors, f"第 {idx} 个操作：insert overlay 的 value 必须是对象。")
            return
        item = {k: copy.deepcopy(v) for k, v in value.items()}
        allowed = OVERLAY_PIP_KEYS_V3 if item.get("type") == "pip" \
            else OVERLAY_TEXT_KEYS_V3
        errs = _check_keys(item, allowed, f"overlay({item.get('type')})")
        errors.extend(errs)
        if errs:
            return
        if item.get("type") not in ("text", "pip"):
            _err(errors, f"第 {idx} 个操作：overlay.type 必须是 text 或 pip。")
            return
        at = item.get("at_clip")
        if _find_clip(work, at) is None:
            _err(errors, f"第 {idx} 个操作：overlay.at_clip={at} 不存在。"
                         f"{_ids_suffix(work)}")
            return
        if not (isinstance(item.get("start_offset"), (int, float))
                and item["start_offset"] >= 0):
            _err(errors, f"第 {idx} 个操作：start_offset 必须是非负数"
                         f"（相对裁剪后片段内时间）。")
            return
        if not (isinstance(item.get("duration"), (int, float))
                and item["duration"] > 0):
            _err(errors, f"第 {idx} 个操作：duration 必须为正数（秒）。")
            return
        g = item.get("gravity")
        if g is not None and g not in GRAVITIES:
            _err(errors, f"第 {idx} 个操作：gravity 必须是九宫格之一 {sorted(GRAVITIES)}。")
            return
        if item["type"] == "text" and not str(item.get("text") or "").strip():
            _err(errors, f"第 {idx} 个操作：text overlay 的 text 不能为空。")
            return
        mx = 0
        for o in ovs:
            m = re.match(r"^o(\d+)$", str((o or {}).get("id") or ""))
            if m:
                mx = max(mx, int(m.group(1)))
        item.setdefault("id", f"o{mx + 1}")
        ovs.append(item)
        applied.append({"op": "insert", "target": f"overlays/{item['id']}",
                        "detail": f"新增 overlay {item['id']}"
                                  f"（{item['type']} → {at}）",
                        "old_values": None})
        return

    if len(key) == 2:
        oid = _seg_id(key[1])
        target = next((o for o in ovs if isinstance(o, dict) and o.get("id") == oid),
                      None)
        if target is None:
            have = [o.get("id") for o in ovs if isinstance(o, dict)]
            _err(errors, f"第 {idx} 个操作：overlay {oid} 不存在"
                         f"（现有：{have or '无'}）。")
            return
        if typ == "delete":
            old = copy.deepcopy(target)
            ovs.remove(target)
            applied.append({"op": "delete", "target": f"overlays/{oid}",
                            "detail": f"移除 overlay {oid}",
                            "old_values": {"overlay": old}})
            return
        if typ != "update":
            _err(errors, f"第 {idx} 个操作：overlay 只支持 insert/update/delete。")
            return
        if not isinstance(value, dict) or not value:
            _err(errors, f"第 {idx} 个操作：update overlay 的 value 必须是非空对象。")
            return
        if "type" in value and value["type"] != target.get("type"):
            _err(errors, f"第 {idx} 个操作：overlay.type 不可改"
                         f"（删除后重建）。")
            return
        allowed = OVERLAY_PIP_KEYS_V3 if target.get("type") == "pip" \
            else OVERLAY_TEXT_KEYS_V3
        errs = _check_keys(value, allowed, f"update overlay {oid}")
        errors.extend(errs)
        if errs:
            return
        old = copy.deepcopy(target)
        for k, v in value.items():
            if v is None and k != "type":
                target.pop(k, None)
            else:
                target[k] = copy.deepcopy(v)
        applied.append({"op": "update", "target": f"overlays/{oid}",
                        "detail": f"更新 overlay {oid}：{', '.join(value)}",
                        "old_values": {"overlay": old}})
        return

    _err(errors, f"第 {idx} 个操作：overlays 的 key 形态不认识：{key}。")


def _apply_block(work: dict, act: dict, idx: int, errors: list,
                 applied: list) -> None:
    """块级 key：audio / output / _narration 等意图键。"""
    typ, key, value = act["type"], act["key"], act.get("value")
    name = key[0]

    if name == "audio":
        if typ == "delete":
            old = copy.deepcopy(work.get("audio"))
            work.pop("audio", None)
            applied.append({"op": "delete", "target": "audio",
                            "detail": "删除全局 BGM（原声直通）",
                            "old_values": {"audio": old}})
            return
        if typ in ("insert", "update"):
            if not isinstance(value, dict) or not value:
                _err(errors, f"第 {idx} 个操作：audio 的 value 必须是非空对象。")
                return
            errs = _check_keys(value, AUDIO_KEYS_V3, "audio")
            errors.extend(errs)
            if errs:
                return
            old = copy.deepcopy(work.get("audio"))
            merged = dict(old or {})
            for k, v in value.items():
                if v is None:
                    merged.pop(k, None)
                else:
                    merged[k] = copy.deepcopy(v)
            if merged:
                work["audio"] = merged
            else:
                work.pop("audio", None)
            applied.append({"op": "update", "target": "audio",
                            "detail": f"更新 BGM：{', '.join(value)}",
                            "old_values": {"audio": old}})
            return
        _err(errors, f"第 {idx} 个操作：audio 只支持 insert/update/delete。")
        return

    if name == "output":
        if typ != "update":
            _err(errors, f"第 {idx} 个操作：output 只支持 update。")
            return
        if not isinstance(value, dict) or not value:
            _err(errors, f"第 {idx} 个操作：output 的 value 必须是非空对象"
                         f"（filename/width/height/fps）。")
            return
        errs = _check_keys(value, OUTPUT_KEYS_V3, "output")
        errors.extend(errs)
        if errs:
            return
        out = work.setdefault("output", {})
        old = copy.deepcopy(out)
        for k, v in value.items():
            if v is None:
                out.pop(k, None)
            else:
                out[k] = copy.deepcopy(v)
        applied.append({"op": "update", "target": "output",
                        "detail": f"更新输出规格：{', '.join(value)}",
                        "old_values": {"output": old}})
        return

    if name in INTENT_KEYS_V3:
        if typ == "delete":
            old = copy.deepcopy(work.get(name))
            work.pop(name, None)
            applied.append({"op": "delete", "target": name,
                            "detail": f"清除 {name}",
                            "old_values": {name: old}})
            return
        if typ == "update":
            old = copy.deepcopy(work.get(name))
            work[name] = copy.deepcopy(value)
            applied.append({"op": "update", "target": name,
                            "detail": f"设置 {name}",
                            "old_values": {name: old}})
            return
        _err(errors, f"第 {idx} 个操作：{name} 只支持 update/delete（整体替换）。")
        return

    _err(errors, f"第 {idx} 个操作：不认识的 key[0]：{name}。"
                 f"支持 clips / timeline / overlays / audio / output / "
                 f"{' / '.join(sorted(INTENT_KEYS_V3))}。")


def apply_actions(plan: dict, actions) -> tuple[dict | None, list[dict], list[str]]:
    """对 v3 计划施加一组 action。返回 (new_plan|None, applied, errors)。

    批内原子：errors 非空 ⇒ new_plan 为 None（工作副本丢弃，入参不动）。
    applied 每条含 old_values（撤销预留）。
    """
    errors: list[dict] = []
    applied: list[dict] = []
    if not isinstance(plan, dict):
        return None, [], ["计划草稿缺失，无法施加操作。"]
    if not isinstance(actions, list) or not actions:
        return None, [], ["actions 必须是非空数组。"]

    work = copy.deepcopy(plan)
    for idx, act in enumerate(actions, 1):
        if not isinstance(act, dict):
            _err(errors, f"第 {idx} 个操作：必须是对象（key/type/value 信封）。")
            continue
        key = act.get("key")
        typ = act.get("type")
        if not isinstance(key, list) or not key or not isinstance(key[0], str):
            _err(errors, f"第 {idx} 个操作：key 必须是非空数组，"
                         f"如 ['clips', {{'id': 'c1'}}]。")
            continue
        if typ not in ENVELOPE_TYPES:
            _err(errors, f"第 {idx} 个操作：type 必须是 {ENVELOPE_TYPES} 之一"
                         f"（收到 {typ!r}）。")
            continue
        route = key[0]
        if route == "clips":
            _apply_clips(work, act, idx, errors, applied)
        elif route == "timeline":
            _apply_timeline(work, act, idx, errors, applied)
        elif route == "overlays":
            _apply_overlays(work, act, idx, errors, applied)
        else:
            _apply_block(work, act, idx, errors, applied)

    if errors:
        return None, applied, errors
    return work, applied, []
