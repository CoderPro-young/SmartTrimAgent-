"""v3.0 方言层：openshot 命名的计划 JSON ↔ 编译器内部 v2 形态的双向转换。

协议定位（docs/v12-protocol.md）：字段参考 openshot、格式骨架自有——
- v3 是 LLM 可见 / 会话存储 / 唯一写出形态：reader{type,path}、start/end、
  clip 平级 speed/volume、gravity 九宫格、effects {id,type,参数平铺}；
- v2 是编译器/校验器消费的内部形态（1799 行 plan_compiler 与 plan_schema
  的既有字段名），零改动复用；
- 版本识别只发生在两个边界：入口归一（upgrade）与编译降级（to_internal），
  管线内部永远单一形态（R11 版本口径不一致熔断的教训）。

eq 参数拆合语义：eq 的 brightness/contrast/saturation 相互独立（单滤镜合并
与多滤镜级联输出等价），因此升级时 eq{b,c,s} 拆成 Brightness{b,c} +
Saturation{s}（openshot 字段表的最忠实映射），降级时相邻 eq 系效果合并回
一个 eq——round-trip 精确还原。

本模块是纯函数集合：不 IO、不改入参（一律深拷贝后变换）。
"""

from __future__ import annotations

import copy
import os
import re

import plan_schema
from skills import effects_menu, resolve_effect, v3_effects_menu

V3_SCHEMA_VERSION = "3.0"

# 图片扩展名（kind 判定的唯一依据；v3 clip 不写 kind，降级时按扩展名注入）。
# 以 plan_ops._IMAGE_EXT 为准（历史最全的一份）。
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".jfif", ".webp", ".bmp", ".gif",
             ".tif", ".tiff", ".heic", ".avif"}


# --------------------------------------------------------------------------- #
# 版本识别
# --------------------------------------------------------------------------- #

def version_of(plan: dict) -> str | None:
    """返回计划的 schema 版本（"3.0" / "2.0" / None=未标注，按 v2 处理）。"""
    if not isinstance(plan, dict):
        return None
    v = plan.get("schema_version")
    return str(v) if v else None


def is_v3(plan: dict) -> bool:
    return version_of(plan) == V3_SCHEMA_VERSION


# --------------------------------------------------------------------------- #
# id 分配（v3 的 effects/overlays 主键；升级与 action 引擎共用）
# --------------------------------------------------------------------------- #

def next_effect_id(plan: dict) -> str:
    mx = 0
    for c in plan.get("clips") or []:
        for e in (c.get("effects") or []) if isinstance(c, dict) else []:
            m = re.match(r"^e(\d+)$", str((e or {}).get("id") or ""))
            if m:
                mx = max(mx, int(m.group(1)))
    return f"e{mx + 1}"


def next_overlay_id(plan: dict) -> str:
    mx = 0
    for o in plan.get("overlays") or []:
        m = re.match(r"^o(\d+)$", str((o or {}).get("id") or ""))
        if m:
            mx = max(mx, int(m.group(1)))
    return f"o{mx + 1}"


def _kind_of(path: str) -> str:
    return "image" if os.path.splitext(str(path))[1].lower() in IMAGE_EXT else "video"


# --------------------------------------------------------------------------- #
# 升级：v2 → v3（入口归一：会话载入 / legacy submit_plan / load_plan）
# --------------------------------------------------------------------------- #

def _effect_v2_to_v3(eff: dict) -> list[dict]:
    """v2 effect {name, args} → v3 平铺 typed effect（可能一拆二，见模块 docstring）。"""
    name = eff.get("name")
    args = eff.get("args") or {}
    skill_name, params = resolve_effect(name)
    if skill_name is None:
        # 未知效果名原样透传为 type——校验阶段给出白名单错误（错误信息更有上下文）
        return [{"type": name, **copy.deepcopy(args)}]
    if skill_name == "eq":
        # eq 三参数相互独立：按 openshot 字段表拆成 Brightness{b,c} + Saturation{s}
        parts = []
        bc = {k: copy.deepcopy(args[k])
              for k in ("brightness", "contrast") if k in args}
        if bc:
            parts.append({"type": "Brightness", **bc})
        if "saturation" in args:
            parts.append({"type": "Saturation",
                          "saturation": copy.deepcopy(args["saturation"])})
        return parts or [{"type": "Brightness"}]
    flat = {k: copy.deepcopy(args[k]) for k in params if k in args}
    return [{"type": skill_name, **flat}]


def upgrade_v2_to_v3(plan: dict) -> dict:
    """v2 计划 → v3 计划。纯函数；未知版本原样按 v2 处理。"""
    if not isinstance(plan, dict):
        return copy.deepcopy(plan)
    out = copy.deepcopy(plan)
    out["schema_version"] = V3_SCHEMA_VERSION

    # output.resolution → 拍平
    res = (out.get("output") or {}).pop("resolution", None)
    if isinstance(res, dict):
        out["output"] = {"width": res.get("width"), "height": res.get("height"),
                         **{k: v for k, v in (out.get("output") or {}).items()
                            if k != "resolution"}}

    # clips：source/kind → reader；trim → start/end；effects 平铺；speed/volume 提升
    ei = 0
    for c in out.get("clips") or []:
        if not isinstance(c, dict):
            continue
        src = c.pop("source", None)
        c.pop("kind", None)
        if src is not None:
            c["reader"] = {"type": "FFmpegReader", "path": src}
        if "trim_start" in c:
            c["start"] = c.pop("trim_start")
        if "trim_end" in c:
            c["end"] = c.pop("trim_end")
        # effects：name/args → typed 平铺 + 全局 id
        new_effects = []
        for eff in c.get("effects") or []:
            if isinstance(eff, dict) and eff.get("name") == "speed":
                # speed 是 v3 的 clip 平级标量（openshot time 的标量子集）
                if "speed" not in c:
                    c["speed"] = (eff.get("args") or {}).get("factor")
                continue
            for part in (_effect_v2_to_v3(eff) if isinstance(eff, dict) else [eff]):
                ei += 1
                part.setdefault("id", f"e{ei}")
                new_effects.append(part)
        if new_effects:
            c["effects"] = new_effects
        # clip.audio：volume 提升为平级标量，其余（mute/source/fade/loop）留原位
        ca = c.get("audio")
        if isinstance(ca, dict) and "volume" in ca:
            c.setdefault("volume", ca.pop("volume"))
            if not ca:
                c.pop("audio", None)
        c.pop("probe", None)   # probe 是宿主侧数据，不进 v3 协议（宿主回填）

    # overlays：position → gravity（openshot 九宫格词汇）+ 全局 id
    oi = 0
    for o in out.get("overlays") or []:
        if not isinstance(o, dict):
            continue
        if "position" in o:
            o["gravity"] = o.pop("position")
        oi += 1
        o.setdefault("id", f"o{oi}")

    return out


# --------------------------------------------------------------------------- #
# 降级：v3 → v2（编译边界：编译器/校验器/Tier2 消费的内部形态）
# --------------------------------------------------------------------------- #

def _effect_v3_to_v2(eff: dict) -> dict | None:
    """v3 typed effect → v2 {name, args}（speed 由调用方收走，不会走到这里）。"""
    typ = eff.get("type")
    resolved, params = resolve_effect(typ)
    args = {k: copy.deepcopy(eff[k]) for k in params if k in eff}
    if resolved is None:
        # 未知 type 透传，由 v2 校验给白名单错误
        out = {"name": typ}
        if args:
            out["args"] = args
        return out
    out = {"name": resolved}
    if args:
        out["args"] = args
    return out


def _merge_adjacent_eq(effects: list[dict]) -> list[dict]:
    """相邻 eq 系效果合并为一个 eq（参数并集）。升级拆分的逆变换，
    也是 LLM 分别写 Brightness + Saturation 的等价归一。"""
    out: list[dict] = []
    for e in effects:
        prev = out[-1] if out else None
        if (prev is not None and e["name"] == "eq" and prev["name"] == "eq"
                and not (set(prev["args"]) & set(e["args"]))):
            prev["args"] = {**prev["args"], **e["args"]}
        else:
            out.append(e)
    return out


def downgrade_v3_to_v2(plan: dict) -> dict:
    """v3 计划 → v2 内部形态。纯函数；非 v3 输入原样深拷贝返回。"""
    if not is_v3(plan):
        return copy.deepcopy(plan)
    out = copy.deepcopy(plan)
    out["schema_version"] = "2.0"

    out_f = out.get("output")
    if isinstance(out_f, dict):
        width, height = out_f.pop("width", None), out_f.pop("height", None)
        if width is not None or height is not None:
            out_f["resolution"] = {"width": width, "height": height}

    for c in out.get("clips") or []:
        if not isinstance(c, dict):
            continue
        reader = c.pop("reader", None)
        src = (reader or {}).get("path") if isinstance(reader, dict) else None
        if src is not None:
            c["source"] = src
            c.setdefault("kind", _kind_of(src))
        if "start" in c:
            c["trim_start"] = c.pop("start")
        if "end" in c:
            c["trim_end"] = c.pop("end")
        speed = c.pop("speed", None)
        effects = []
        for eff in c.get("effects") or []:
            if not isinstance(eff, dict):
                effects.append(eff)
                continue
            if eff.get("type") == "speed":
                # v3 里变速只住在 clip.speed；effect 形态出现即归位（标量优先）
                if speed is None:
                    speed = eff.get("factor")
                continue
            v2e = _effect_v3_to_v2(eff)
            if v2e is not None:
                effects.append(v2e)
        if speed is not None:
            effects.insert(0, {"name": "speed", "args": {"factor": speed}})
        if effects:
            c["effects"] = _merge_adjacent_eq(effects)
        # clip.volume + clip.audio → v2 clip.audio（volume 并回对象内）
        vol = c.pop("volume", None)
        ca = c.get("audio")
        if vol is not None or isinstance(ca, dict):
            merged = dict(ca) if isinstance(ca, dict) else {}
            if vol is not None:
                merged["volume"] = vol
            c["audio"] = merged

    for o in out.get("overlays") or []:
        if not isinstance(o, dict):
            continue
        if "gravity" in o:
            o["position"] = o.pop("gravity")
        o.pop("id", None)          # v2 overlay 无 id 主键（v3 专属寻址）

    return out


# --------------------------------------------------------------------------- #
# 归一与校验入口
# --------------------------------------------------------------------------- #

def normalize(plan: dict) -> dict:
    """入口归一：任何版本读入 → v3。写出永远 v3。"""
    if is_v3(plan):
        return copy.deepcopy(plan)
    return upgrade_v2_to_v3(plan)


def to_internal(plan: dict) -> dict:
    """编译边界降级：v3 → v2 内部形态（非 v3 原样返回副本）。"""
    return downgrade_v3_to_v2(plan)


# 校验错误信息里的 v2 字段名 → v3 名（展示层翻译，让 LLM 看到它写的词汇）
_MSG_REWRITES = [
    (re.compile(r"\btrim_start\b"), "start"),
    (re.compile(r"\btrim_end\b"), "end"),
    (re.compile(r"(clips\[\d+\])\.source"), r"\1.reader.path"),
    (re.compile(r"output\.resolution"), "output 的 width/height"),
    (re.compile(r"effects\[(\d+)\]\.args\.factor"), r"（clip.speed，effects[\1]）"),
    (re.compile(r"effects\[(\d+)\]\.args\."), r"effects[\1] 的参数 "),
    (re.compile(r"effects\[(\d+)\]\.name"), r"effects[\1].type"),
]


def _rewrite_msgs(errors: list[str]) -> list[str]:
    # v2 效果白名单字面量 → v3 菜单（错误信息与 LLM 写的词汇一致）
    menu_v2, menu_v3 = str(effects_menu()), str(v3_effects_menu())
    out = []
    for e in errors:
        e = e.replace(menu_v2, menu_v3)
        for pat, repl in _MSG_REWRITES:
            e = pat.sub(repl, e)
        out.append(e)
    return out


def validate_any(plan: dict, project_root: str) -> list[str]:
    """任意版本计划的校验：降级到 v2 跑既有 validate_plan，零新规则。"""
    errors = plan_schema.validate_plan(to_internal(plan), project_root)
    return _rewrite_msgs(errors) if is_v3(plan) else errors


def check_material_fit_any(plan: dict, project_root: str, probe_fn=None) -> list:
    """Tier2 素材匹配：同样走降级（它读 source/trim/position 等内部字段）。"""
    errs = plan_schema.check_material_fit(to_internal(plan), project_root, probe_fn)
    if is_v3(plan):
        for m in errs:
            m.tag = _rewrite_msgs([m.tag])[0]
    return errs
