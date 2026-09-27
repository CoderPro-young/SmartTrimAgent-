"""粗筛废料判定（V6 粗剪漏斗第①层）：纯规则、纯函数、零幻觉。

「粗筛 = 扔垃圾」：不判断内容好不好（那是语义层的事），只判断素材
**事实层面是否可用**——全静音、全黑场、画质全差、时长过短。

输入是 analyze_media 的内容卡片 + probe 元数据（/api/inputs 已有），
输出是废料判定（带中文原因）。判定永远只是**建议**——真正的移除由
用户在报告上点「应用」才执行（产品决策：破坏性操作给用户拍板权）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---- 阈值（全部可被调用方覆盖） ----
ALL_SILENT_RATIO = 0.99     # 静音占比 ≥ 此值视为整条无声（Pexels 环境音轨实测 1.0）
ALL_BLACK_RATIO = 0.95      # 黑场覆盖 ≥ 此值视为废片（镜头盖/未开录）
MIN_USABLE_SECONDS = 2.0    # 短于此直接判废（连一个镜头都撑不起来）
POOR_ALL_SHOTS = True       # 所有镜头 quality 都是 poor 才判废（个别差镜头不算）

@dataclass
class JunkVerdict:
    """单个素材的废料判定。"""
    name: str
    junk: bool
    reasons: list[str] = field(default_factory=list)  # 中文原因列表（可多条）
    severity: str = "info"  # junk=True 时 "warn"（建议丢弃）


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def judge_material(name: str, card: dict | None, probe: dict | None, *,
                   all_silent_ratio: float = ALL_SILENT_RATIO,
                   all_black_ratio: float = ALL_BLACK_RATIO,
                   min_usable_seconds: float = MIN_USABLE_SECONDS) -> JunkVerdict:
    """对一个素材做废料判定。

    card  —— analyze_media 的内容卡片（/api/inputs 的 content 字段；None=未索引）
    probe —— ffprobe 元数据（duration/has_audio…）
    判定规则见模块注释；未索引的素材不判废（信息不足宁可放过）。
    """
    reasons: list[str] = []

    duration = None
    if probe and _num(probe.get("duration")):
        duration = float(probe["duration"])

    # ① 过短（probe 层信号，无需卡片）
    if duration is not None and duration < min_usable_seconds:
        reasons.append(f"时长只有 {duration:.1f}s，短于 {min_usable_seconds:g}s 下限")

    if card and not card.get("error"):
        signals = card.get("signals") or {}
        audio = signals.get("audio") or {}
        video = signals.get("video") or {}

        # ② 整条静音（有音轨但几乎无声）
        ratio = audio.get("silence_ratio")
        if ratio is not None and ratio >= all_silent_ratio:
            reasons.append(f"音轨整体无声（静音占比 {ratio:.0%}），剪静音无从下手")

        # ③ 整条黑场
        blacks = video.get("blacks") or []
        if duration and blacks:
            covered = sum(e - s for s, e in blacks if e > s)
            if duration > 0 and covered / duration >= all_black_ratio:
                reasons.append(f"黑场覆盖 {covered:.1f}s/{duration:.1f}s（镜头盖误录或未开录）")

        # ④ 所有镜头画质都差（糊/抖/过曝）
        shots = card.get("shots") or []
        labeled = [s for s in shots if s.get("label")]
        if POOR_ALL_SHOTS and labeled and \
                all((s["label"].get("quality") == "poor") for s in labeled):
            reasons.append(f"全部 {len(labeled)} 个镜头画质差（模糊/抖动/过曝）")

    return JunkVerdict(name=name, junk=bool(reasons), reasons=reasons,
                       severity="warn" if reasons else "info")


def build_cull_report(items: list[dict]) -> dict:
    """对 /api/inputs 的素材列表生成粗筛报告。

    items —— [{name, probe, content, analysis_state, kind}, ...]
    返回 {junk: [verdict...], keep: [names...], stats: {...}}；
    junk 按原因数降序（问题最多的排前面）。
    """
    junk, keep, unindexed, audio = [], [], 0, 0
    for it in items or []:
        if it.get("kind") == "audio":
            audio += 1                 # 纯音频是 BGM 素材，不进粗筛
            continue
        card = it.get("content")
        if not card:
            unindexed += 1
        v = judge_material(it.get("name", "?"), card, it.get("probe"))
        if v.junk:
            junk.append({"name": v.name, "reasons": v.reasons})
        else:
            keep.append(v.name)
    junk.sort(key=lambda j: -len(j["reasons"]))
    return {
        "junk": junk,
        "keep": keep,
        "stats": {
            "total": len(items or []),
            "junk": len(junk),
            "keep": len(keep),
            "unindexed": unindexed,
            "audio": audio,
        },
    }
