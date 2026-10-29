"""V9.0 P1：计划差异（plan_diff）与作用域闸门（check_scope）。

设计契约见 docs/v9.0-understanding-layer.md §3。两个原则：
- 可计算的校验交给规则：指代粒度 × 影响半径都是结构判定；
- 语义校验（改得对不对、目标选没选对）不在这里——归 diff 卡人眼与 ask_user。

纯函数、零 IO、零 LLM：全部可离线测试。
"""
from __future__ import annotations

import re

# 顶层块变化 = 全局影响（与 PLAN_KEYS 同步维护：这里只列「影响整条时间线」的键）
_GLOBAL_KEYS = ("audio", "select", "workflow", "output", "overlays",
                "_narration", "_captions", "_caption_style", "_title")

_TRIM_EPS = 0.05          # trim 对齐容差（beat snap / 微调不应误报增删）
_FULL_WIDTH = str.maketrans("（）０１２３４５６７８９", "()0123456789")

# 用户中文场景词 → 内容卡片英文标签词（翻译表，不是 case 枚举：
# 词表来自卡片本身，缺词只是让闸门退回 none 放行，不产生误拦）
SCENE_GLOSSARY = {
    "演唱会": ("concert", "singing", "performance", "stage", "music"),
    "音乐会": ("concert", "music", "performance"),
    "唱歌": ("singing", "concert", "vocal"),
    "跳舞": ("dance", "dancing"),
    "舞蹈": ("dance", "dancing"),
    "聚会": ("party", "dinner", "gathering"),
    "聚餐": ("dinner", "food", "party"),
    "美食": ("food", "cooking", "meal"),
    "旅行": ("travel", "trip", "outdoor", "landscape"),
    "海边": ("beach", "sea", "ocean"),
    "小孩": ("kids", "children", "child"),
    "孩子": ("kids", "children", "child"),
    "宠物": ("pet", "dog", "cat", "animal"),
}

_GLOBAL_QUANTIFIERS = ("全部", "所有", "整条", "整个", "每一", "全片", "全都", "从头到尾")

_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_int(s: str) -> int | None:
    """「第 X 个」里的 X → int。支持阿拉伯数字与 1~99 的中文写法
    （十 / 十五 / 二十 / 二十三 / 两）。解析不了返回 None。"""
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total, num = 0, 0
    for ch in s:
        if ch in _CN_DIGIT:
            num = _CN_DIGIT[ch]
        elif ch == "十":
            total += (num or 1) * 10
            num = 0
        else:
            return None
    return total + num if total + num > 0 else None


def _r(x) -> float:
    try:
        return round(float(x), 2)
    except (TypeError, ValueError):
        return 0.0


def _trans_of(plan: dict, cid: str):
    for t in plan.get("timeline") or []:
        if t.get("clip") == cid:
            return t.get("transition")
    return None


def _match_clips(old_clips: list, new_clips: list) -> tuple[list, list, list]:
    """同 source + trim 区间重叠 ⇒ 同一逻辑片段（贪心、保序）。

    返回 (matched_pairs, removed_old_idx, added_new_idx)。
    trim 微调（<_TRIM_EPS 级别或区间仍重叠）不会误报增删。
    """
    used = [False] * len(new_clips)
    pairs: list[tuple[int, int]] = []
    removed: list[int] = []
    for i, oc in enumerate(old_clips):
        os_, oe = oc.get("trim_start") or 0, oc.get("trim_end")
        hit = -1
        for j, nc in enumerate(new_clips):
            if used[j] or nc.get("source") != oc.get("source"):
                continue
            ns, ne = nc.get("trim_start") or 0, nc.get("trim_end")
            # 区间重叠判定（ne/oe 为 None 表示「用到素材末尾」，视为 +inf）
            if oe is None or ne is None:
                overlapped = min(oe or 1e9, ne or 1e9) - max(os_, ns) > _TRIM_EPS
            else:
                overlapped = min(oe, ne) - max(os_, ns) > _TRIM_EPS
            if overlapped:
                hit = j
                break
        if hit >= 0:
            used[hit] = True
            pairs.append((i, hit))
        else:
            removed.append(i)
    added = [j for j in range(len(new_clips)) if not used[j]]
    return pairs, removed, added


def _fmt_trans(tr) -> str:
    if not tr:
        return "硬切"
    return f"{tr.get('type')}{_r(tr.get('duration'))}s" if tr.get("duration") else str(tr.get("type"))


def plan_diff(old: dict, new: dict) -> dict:
    """两份（raw 未展开）计划 → 结构化差异。diff 卡与作用域闸门共用。

    V12：v3 计划先降级到内部形态再比——同空间比较（R11 不变量），v3 与
    v2 计划（旧会话/宏路径）互比也不会因字段名产生假 diff。
    """
    import plan_dialect
    if plan_dialect.is_v3(old) or plan_dialect.is_v3(new):
        old = plan_dialect.to_internal(old)
        new = plan_dialect.to_internal(new)
    changes: list[dict] = []
    old_clips = old.get("clips") or []
    new_clips = new.get("clips") or []
    pairs, removed, added = _match_clips(old_clips, new_clips)
    touched: list[str] = []

    for i in removed:
        c = old_clips[i]
        touched.append(c.get("id"))
        changes.append({"kind": "remove_clip", "clip": c.get("id"),
                        "detail": f"删除片段 {c.get('id')}（{_short_src(c.get('source'))}）"})
    for j in added:
        c = new_clips[j]
        touched.append(c.get("id"))
        changes.append({"kind": "add_clip", "clip": c.get("id"),
                        "detail": f"新增片段 {c.get('id')}（{_short_src(c.get('source'))} "
                                  f"{_r(c.get('trim_start') or 0)}-{_r(c.get('trim_end') or 0)}s）"})
    for i, j in pairs:
        oc, nc = old_clips[i], new_clips[j]
        bits = []
        ot, nt = ((_r(oc.get("trim_start") or 0), _r(oc.get("trim_end") or 0)),
                  (_r(nc.get("trim_start") or 0), _r(nc.get("trim_end") or 0)))
        if ot != nt:
            bits.append(f"裁剪 {ot[0]}-{ot[1]}s → {nt[0]}-{nt[1]}s")
        oe = {e.get("name") if isinstance(e, dict) else str(e)
              for e in (oc.get("effects") or [])}
        ne = {e.get("name") if isinstance(e, dict) else str(e)
              for e in (nc.get("effects") or [])}
        if oe != ne:
            bits.append(f"效果 {sorted(oe) or '无'} → {sorted(ne) or '无'}")
        oa, na = oc.get("audio") or {}, nc.get("audio") or {}
        if bool(oa.get("mute")) != bool(na.get("mute")):
            bits.append("本段静音" if na.get("mute") else "取消本段静音")
        tr_o, tr_n = _trans_of(old, oc.get("id")), _trans_of(new, nc.get("id"))
        if (tr_o or {}) != (tr_n or {}):
            bits.append(f"转场 {_fmt_trans(tr_o)} → {_fmt_trans(tr_n)}")
        if bits:
            touched.append(nc.get("id"))
            changes.append({"kind": "modify_clip", "clip": nc.get("id"),
                            "detail": f"{nc.get('id')}：" + "；".join(bits)})

    # 顺序变化（matched 集合的相对次序改变）。pairs 按旧序排列；匹配对两侧
    # id 可能不同（手写整份新计划会重新编号，如旧 c4 ↔ 新 c5），所以按
    # 索引秩比较，不能拿新 id 去旧 id 列表里查（id 不一致会直接 ValueError）
    new_seq = [j for _, j in pairs]
    if new_seq != sorted(new_seq):
        by_new = sorted(pairs, key=lambda p: p[1])
        new_rank = {j: r for r, (_, j) in enumerate(by_new)}
        moved = [new_clips[j].get("id")
                 for k, (_, j) in enumerate(pairs) if new_rank[j] != k]
        new_order = [new_clips[j].get("id") for _, j in by_new]
        touched.extend(moved)
        changes.append({"kind": "reorder", "clip": "+".join(moved),
                        "detail": "片段顺序调整：" + " → ".join(new_order)})

    # 转场全局感（2026-10-28 sim-v12-r1 实测）：「所有转场换成 dissolve」这类
    # 合法全局意图只触碰 N-1 个片段（首片段无转场），按 touched≥total 永远判
    # local，与 global 消息互相顶牛造成闸门死循环。全部边界转场都变 ⇒ global。
    boundaries = changed_trans = 0
    for i, j in pairs:
        tr_o = _trans_of(old, old_clips[i].get("id"))
        tr_n = _trans_of(new, new_clips[j].get("id"))
        if tr_o or tr_n:                       # 首片段等无转场的边界不计
            boundaries += 1
            if (tr_o or {}) != (tr_n or {}):
                changed_trans += 1
    if boundaries > 1 and changed_trans == boundaries:
        changes.append({"kind": "transitions_all",
                        "detail": f"全部 {changed_trans} 处转场调整"})

    # 顶层块（全局）
    for key in _GLOBAL_KEYS:
        ov, nv = old.get(key), new.get(key)
        if ov != nv:
            if key == "audio":
                ch = {"kind": "audio_block",
                      "detail": ("移除全局 BGM" if nv is None else
                                 "新增全局 BGM" if ov is None else "调整全局音频设置")}
                if nv is None and ov is not None:
                    ch["side_effect"] = "⚠ 所有片段原声将恢复（V7.7.1：无 BGM 块时默认不再静音原声）"
                elif ov is None and nv is not None and (nv or {}).get("original") != "keep":
                    ch["side_effect"] = "⚠ 所有片段原声将被默认静音（V7.7.1：有 BGM 时保留原声需显式 original=keep）"
                changes.append(ch)
            elif key == "output":
                fn = (nv or {}).get("filename") or (ov or {}).get("filename")
                changes.append({"kind": "output", "detail": f"输出文件改为 {fn}"})
            elif ov is not None or nv is not None:
                changes.append({"kind": "top_level", "detail": f"顶层块 {key} 变化"})

    total = max(len(new_clips), 1)
    uniq_touched = [c for c in dict.fromkeys(touched) if c]
    scope = "global" if (len([c for c in changes if c["kind"] in
                              ("audio_block", "top_level", "transitions_all")]) > 0
                         or len(uniq_touched) >= total) else "local"
    return {
        "scope": scope,
        "touched": uniq_touched,
        "total_clips": len(new_clips),
        "changes": changes,
    }


def _short_src(src) -> str:
    s = str(src or "")
    return s.split("/")[-1][:28]


def msg_scope(msg: str, materials: list[str] | None = None,
              scene_vocab: set[str] | None = None) -> str:
    """用户消息的指代粒度：local（提到具体对象）/ global（全称量词）/ none。

    materials 须按文件名序传入（与素材库 UI 同源）——「第 N 个素材」的 N
    按位置解析（2026-10-29 编号体系）。N 超界视为没提，宁放行给闸门后面的
    ask_user，不误拦。"""
    m = (msg or "").translate(_FULL_WIDTH).lower()
    if not m.strip():
        return "none"
    local = False
    # ① 片段 ID：C3 / c11（不用 \b：CJK 也是 \w，「把C2删掉」没有词边界）
    if re.search(r"(?<![A-Za-z0-9])[Cc]\d{1,3}(?!\d)", m):
        local = True
    # ② 素材文件名 stem（≥4 字符，避免误配短名）
    for mat in materials or []:
        stem = re.split(r"[\\/]", str(mat))[-1]
        stem = stem.rsplit(".", 1)[0]
        if len(stem) >= 4 and stem.lower() in m:
            local = True
    # ③ 场景词：中文词在消息里 且 卡片词表里有对应英文标签
    vocab = {v.lower() for v in (scene_vocab or set())}
    if vocab:
        for zh, ens in SCENE_GLOSSARY.items():
            if zh in m and any(e in vocab for e in ens):
                local = True
    # ④ 素材编号指代：「第 N 个（素材）」。指「第二个片段」时也判 local——
    # 闸门只看粒度冲突，具体指代由 agent 解析
    for tok in re.findall(r"第\s*(\d{1,3}|[零一二两三四五六七八九十]{1,4})\s*个", m):
        n = _cn_to_int(tok)
        if n and materials and 1 <= n <= len(materials):
            local = True
    if local:
        return "local"
    if any(q in m for q in _GLOBAL_QUANTIFIERS):
        return "global"
    return "none"


def check_scope(msg: str, diff: dict, materials: list[str] | None = None,
                scene_vocab: set[str] | None = None) -> dict | None:
    """作用域闸门：消息指代粒度 × diff 影响半径，冲突才拦（返回 question 载荷）。

    materials 须按文件名序传入（编号 = 位置 + 1，与素材库 UI 同源），
    「第 N 个素材」指代据此判 local。签名无信息量（none）时放行——这是
    设计上接受的残留区（见设计文档 §3.3）。
    """
    ms = msg_scope(msg, materials, scene_vocab)
    ds = diff.get("scope")
    if ms == "local" and ds == "global":
        return {
            "type": "question", "kind": "scope_gate",
            "question": ("你的消息里提到了具体对象，但这次修改会影响整条时间线"
                         f"（影响全部 {diff.get('total_clips')} 个片段或全局音频/输出）。"
                         "确认要按全局执行吗？"),
            "options": ["确认全局执行", "不，只改我说的那部分", "取消"],
            "signature": {"msg_scope": ms, "diff_scope": ds},
        }
    if ms == "global" and ds == "local":
        return {
            "type": "question", "kind": "scope_gate",
            "question": (f"你的要求听起来是全局的（全部/整条…），但这次修改只影响到 "
                         f"{len(diff.get('touched') or [])}/{diff.get('total_clips')} 个片段。"
                         "是就要这样，还是其余的也要改？"),
            "options": ["就这样执行", "其余的也要改", "取消"],
            "signature": {"msg_scope": ms, "diff_scope": ds},
        }
    return None
