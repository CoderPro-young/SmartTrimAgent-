"""select 批量筛选宏的纯逻辑（V5 粗剪）：内容卡片 × 筛选条件 → clips。

LLM 只写筛选意图（where 条件 + 预算），本模块确定性地完成「挑哪些镜头」：
杜绝模型手抄多区间 trim 时的漏抄/错抄（文档自认的一号风险），并让卡片里
任何标签维度（场景/人数/画质/静音比…）都成为可筛选条件——用户需求多样化
时不需要新增能力，只需要新的条件组合。

输入是 analyze_media 产出的内容卡片（sidecar 缓存），输出是可直接进
plan_compiler._derive 的 clips/timeline。纯函数、零 IO、可离线测试。
"""

from __future__ import annotations

from signal_detection import overlap_seconds

QUALITY_ORDER = {"good": 0, "ok": 1, "poor": 2}


def _label(shot: dict) -> dict:
    return shot.get("label") or {}


def _text_match(query: str, value: str, tags: list[str]) -> bool:
    """宽松文本匹配：相等或互为子串（VLM 标签「餐厅」vs 用户问「餐厅聚餐」）。"""
    q, v = query.strip(), (value or "").strip()
    if q and v and (q == v or q in v or v in q):
        return True
    return any(q and t and (q == t or q in t or t in q) for t in tags)


def shot_matches(shot: dict, where: dict) -> bool:
    """单个镜头是否满足全部 where 条件（AND 语义；同字段内多值 OR）。"""
    lab = _label(shot)
    tags = [t for t in (lab.get("tags") or []) if t]
    if where.get("tag_any") and not any(
            _text_match(q, "", tags) for q in where["tag_any"]):
        return False
    for field in ("scene", "activity", "mood"):
        values = where.get(f"{field}_any")
        if values and not any(
                _text_match(q, lab.get(field, ""), tags) for q in values):
            return False
    pc = lab.get("person_count", 0)
    if where.get("person_count_min") is not None and pc < where["person_count_min"]:
        return False
    if where.get("person_count_max") is not None and pc > where["person_count_max"]:
        return False
    if where.get("has_children") is True and not lab.get("has_children"):
        return False
    if where.get("has_children") is False and lab.get("has_children"):
        return False
    quality_in = where.get("quality_in")
    if quality_in and lab.get("quality", "ok") not in quality_in:
        return False
    return True


def shot_span(shot: dict) -> tuple[float, float]:
    s = float(shot.get("start") or 0.0)
    e = shot.get("end")
    e = float(e) if e is not None else s + 1.0
    return s, e


def shot_silence_ratio(shot: dict, silences: list | None) -> float:
    """镜头区间的静音占比（卡片 signals.audio 的全片静音区间取重叠）。"""
    if not silences:
        return 0.0
    s, e = shot_span(shot)
    if e <= s:
        return 0.0
    return overlap_seconds(silences, s, e) / (e - s)


def _candidate(shot: dict, source: str, card: dict) -> dict:
    s, e = shot_span(shot)
    lab = _label(shot)
    return {
        "source": source,
        "start": round(s, 3),
        "end": round(e, 3),
        "duration": round(e - s, 3),
        "quality": lab.get("quality", "ok"),
        "label": lab,
        "tag_failed": bool(shot.get("tag_failed")),
    }


def collect_candidates(cards: dict[str, dict], where: dict) -> list[dict]:
    """跨素材收集满足条件的镜头候选（保持 sources 顺序 → 时间顺序）。"""
    out: list[dict] = []
    for source in sorted(cards):
        card = cards[source]
        silences = ((card.get("signals") or {}).get("audio") or {}).get("silences")
        for shot in card.get("shots") or []:
            if shot.get("tag_failed"):
                continue
            if not shot_matches(shot, where):
                continue
            s, e = shot_span(shot)
            dur = e - s
            if where.get("min_duration") is not None and dur < where["min_duration"]:
                continue
            if where.get("max_duration") is not None and dur > where["max_duration"]:
                continue
            if where.get("max_silence_ratio") is not None and \
                    shot_silence_ratio(shot, silences) > where["max_silence_ratio"]:
                continue
            out.append(_candidate(shot, source, card))
    return out


def select_shots(cards: dict[str, dict], select: dict,
                 default_quality_in=("good", "ok")) -> dict:
    """执行筛选，返回 {clips, timeline, picked, skipped_no_budget}。

    budget_seconds 缺省 = 全部命中镜头；装填策略为贪心（放不下的跳过、
    继续尝试更短的），order 支持 as_listed（默认：素材序→时间序）与
    best_first（画质优先→更长优先）。
    """
    where = dict(select.get("where") or {})
    where.setdefault("quality_in", list(default_quality_in))
    cands = collect_candidates(cards, where)

    order = select.get("order", "as_listed")
    if order == "best_first":
        cands.sort(key=lambda c: (QUALITY_ORDER.get(c["quality"], 1), -c["duration"]))

    budget = select.get("budget_seconds")
    picked: list[dict] = []
    used = 0.0
    for c in cands:
        if budget is not None and used + c["duration"] > budget + 1e-6:
            continue
        picked.append(c)
        used = round(used + c["duration"], 3)
        if budget is not None and used >= budget - 1e-6:
            break

    clips: list[dict] = []
    timeline: list[dict] = []
    for i, c in enumerate(picked):
        cid = f"s{i:02d}"
        clips.append({
            "id": cid,
            "source": c["source"],
            "kind": "video",
            "trim_start": c["start"],
            "trim_end": c["end"],
        })
        timeline.append({"clip": cid})
    return {"clips": clips, "timeline": timeline, "picked": picked,
            "matched": len(cands),
            "min_matched_duration": min((c["duration"] for c in cands), default=None)}
