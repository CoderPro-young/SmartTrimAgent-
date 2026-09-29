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
    return _reject_reason(shot, where) is None


def _reject_reason(shot: dict, where: dict) -> str | None:
    """返回第一个不满足条件的原因（中文，进筛选报告）；全满足返回 None。"""
    lab = _label(shot)
    tags = [t for t in (lab.get("tags") or []) if t]
    # text_any：跨字段 OR（场景/活动/氛围/标签任一命中即可）——
    # 给「关键词」语义用；单字段 _any 之间仍是 AND
    text_any = where.get("text_any")
    if text_any and not any(
            _text_match(q, lab.get(f, "") or "", tags)
            for q in text_any for f in ("scene", "activity", "mood")):
        return f"场景/活动/氛围/标签均不匹配「{('、'.join(map(str, text_any)))[:32]}」"
    for field, label in (("scene", "场景"), ("activity", "活动"), ("mood", "氛围")):
        values = where.get(f"{field}_any")
        if values and not any(
                _text_match(q, lab.get(field, ""), tags) for q in values):
            return f"{label}不匹配（要求 {('、'.join(map(str, values)))[:40]}）"
    if where.get("tag_any") and not any(
            _text_match(q, "", tags) for q in where["tag_any"]):
        return f"标签不含 {('、'.join(map(str, where['tag_any'])))[:40]}"
    pc = lab.get("person_count", 0)
    if where.get("person_count_min") is not None and pc < where["person_count_min"]:
        return f"人数 {pc} 少于 {where['person_count_min']}"
    if where.get("person_count_max") is not None and pc > where["person_count_max"]:
        return f"人数 {pc} 超过 {where['person_count_max']}"
    if where.get("has_children") is True and not lab.get("has_children"):
        return "画面无儿童"
    if where.get("has_children") is False and lab.get("has_children"):
        return "画面有儿童（要求排除）"
    quality_in = where.get("quality_in")
    if quality_in and lab.get("quality", "ok") not in quality_in:
        return f"画质 {lab.get('quality', 'ok')} 不在 {('、'.join(quality_in))}"
    return None


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


def collect_candidates(cards: dict[str, dict], where: dict) -> tuple[list[dict], list[dict]]:
    """跨素材收集满足条件的镜头候选 + 被拒镜头（带中文原因，进筛选报告）。

    返回 (candidates, rejected)，均保持 sources 顺序 → 时间顺序。
    """
    out: list[dict] = []
    rejected: list[dict] = []
    for source in sorted(cards):
        card = cards[source]
        silences = ((card.get("signals") or {}).get("audio") or {}).get("silences")
        for shot in card.get("shots") or []:
            if shot.get("tag_failed"):
                rejected.append({**_candidate(shot, source, card), "reason": "打标失败，无法判断"})
                continue
            reason = _reject_reason(shot, where)
            if reason is None:
                s, e = shot_span(shot)
                dur = e - s
                if where.get("min_duration") is not None and dur < where["min_duration"]:
                    reason = f"时长 {dur:.1f}s 短于 {where['min_duration']}s"
                elif where.get("max_duration") is not None and dur > where["max_duration"]:
                    reason = f"时长 {dur:.1f}s 超过 {where['max_duration']}s"
                elif where.get("max_silence_ratio") is not None and \
                        shot_silence_ratio(shot, silences) > where["max_silence_ratio"]:
                    reason = f"镜头内静音占比 {shot_silence_ratio(shot, silences):.0%} " \
                             f"超过 {where['max_silence_ratio']:.0%}"
            if reason is not None:
                rejected.append({**_candidate(shot, source, card), "reason": reason})
                continue
            out.append(_candidate(shot, source, card))
    return out, rejected


def select_shots(cards: dict[str, dict], select: dict,
                 default_quality_in=("good", "ok")) -> dict:
    """执行筛选，返回 {clips, timeline, picked, skipped_no_budget}。

    budget_seconds 缺省 = 全部命中镜头。装填策略：
    - best_first + 有预算 → 跨素材轮转（V7.2）：每条素材轮流贡献镜头，
      避免单条素材的长镜头占满预算把其他来源全部挤出；装不下的直接标拒
      （预算只减不增），同来源更短的后续镜头仍有机会进片。
    - 其余（as_listed / 无预算）→ 顺序贪心：放不下的跳过、继续尝试更短的。
    order 支持 as_listed（默认：素材序→时间序）与 best_first（画质优先→更长优先）。
    """
    where = dict(select.get("where") or {})
    where.setdefault("quality_in", list(default_quality_in))
    cands, rejected = collect_candidates(cards, where)

    order = select.get("order", "as_listed")
    if order == "best_first":
        cands.sort(key=lambda c: (QUALITY_ORDER.get(c["quality"], 1), -c["duration"]))

    budget = select.get("budget_seconds")
    picked: list[dict] = []
    used = 0.0
    if order == "best_first" and budget is not None:
        # 跨素材轮转装填（V7.2）：每条素材轮流贡献镜头。全局贪心会被单条
        # 素材的长镜头占满预算（实测：两条 11.7s 占满 25s，其余素材全部
        # 跳过，最后靠 0.08s 碎片凑数）——轮转让多素材任务每个来源都出镜。
        # 预算只减不增：本轮装不下的镜头以后也装不下，直接标拒；
        # 同来源更短的后续镜头仍有机会在后续轮次进片。
        queues: dict[str, list[dict]] = {}
        for c in cands:                      # cands 已按 画质→时长 排序
            queues.setdefault(c["source"], []).append(c)
        names = sorted(queues)
        while any(queues.values()):
            for name in names:
                q = queues.get(name)
                if not q:
                    continue
                c = q[0]
                if used + c["duration"] > budget + 1e-6:
                    rejected.append({**c, "reason": f"装不进 {budget:g}s 预算（已用 {used:g}s）"})
                    q.pop(0)
                    continue
                picked.append(q.pop(0))
                used = round(used + c["duration"], 3)
                if used >= budget - 1e-6:
                    queues.clear()
                    break
    else:
        for c in cands:
            if budget is not None and used + c["duration"] > budget + 1e-6:
                rejected.append({**c, "reason": f"装不进 {budget}s 预算（已用 {used:g}s）"})
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
            "min_matched_duration": min((c["duration"] for c in cands), default=None),
            "rejected": rejected}
