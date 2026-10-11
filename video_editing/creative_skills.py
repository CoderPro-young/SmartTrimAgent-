"""创作流 skill 注册表（V8.1）：WORKFLOW SKILL 的「注册表化」实现。

V8.0 把 FireRed 式创作流直接写死在 video_agent 的系统提示词模板里；
V8.1 起抽成本注册表——与 skills.py（效果）/ workflow.py（宏）同构的
「一处定义 → 提示词派生」，video_agent 用 {CREATIVE_DOC} 占位符装配。

注册表项 = 路由条件（summary）+ 专属节奏指导（flavor）；七步主流程与
筛选/分组硬规则是全部 skill 共享的公共段（COMMON_STEPS），新 skill =
加一个 CreativeSkill，不改公共段、不改装配代码。

对 FireRed 的差异（记录）：它是 SKILL.md 文件按需加载 + agent 显式选择；
我们是注册表整段常驻提示词——换来零路由失败与零加载机制，代价是常驻
token（当前三 skill 合计 ~1k token，可接受；skill 数量显著变多时再考虑
按需注入）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CreativeSkill:
    name: str                # 计划/报告里标识用
    summary: str             # 路由条件一句话（进 skill 菜单）
    flavor: str = ""         # 专属节奏/文案指导（接在公共段后）


# ------------------------------------------------------------------ 公共段 -- #

COMMON_STEPS = """When the user wants a CREATED piece ("帮我剪个视频", "make a vlog",
"一键成片", "做成带文案的成片"), YOU drive the editing workflow step by step —
the judgment calls (which shots, what order, what text, which music) are YOURS;
timeline math and rendering stay with the deterministic compiler:

1. `ls INPUT/` → `analyze_media` on every video material (cards are usually
   pre-built on upload; missing ones are listed by `list_shots`).
2. Call `list_shots()` — the FULL cross-material shot pool, one line per shot
   (id / source / time range / duration / scene / people / quality / desc).
   Shots marked ⚠ (poor quality, mostly silent, very short) are listed too;
   there is NO pre-culling — you decide.
3. **Filter (your judgment)**: keep the shots that serve the request.
   Hard rules (borrowed from OpenStoryline): if the pool has ≤5 shots, drop
   none; otherwise keep MORE than 80% and at least 5; among near-duplicate
   shots keep the one with the higher `aes_score` (aesthetic 0~1, shown as
   美学x.xx when available; fall back to quality/pacing when absent);
   ⚠ shots are usually dropped unless the content is uniquely valuable.
4. **Group into a narrative (your judgment)**: order groups as
   Hook (grab attention) → Core (the substance) → Vibe (atmosphere) →
   End (closing beat); aggregate same-scene shots and never interleave
   scene A → B → A; each group 2–4 shots / 3–20s unless the material
   forces otherwise.
5. **Write narration (you write it)**: for each group write ONE flowing
   first-person narration paragraph (Chinese, concrete nouns and actions,
   no clichés like 治愈/岁月静好/美好时光, no addressing the label words);
   budget 3–5 characters per second of group duration. Also produce a
   short title for the whole piece.
6. **Pick music (you pick)**: `list_music()` → write the chosen track into
   the top-level `audio` block ({"source": ..., "volume": 1.0, "loop": true}
   — original sound is muted by default when BGM is present). No music
   wanted → omit the `audio` block. Music-driven pacing: add
   `"snap_beats": true` to the audio block and the compiler will align the
   cut points to the music's beats (within ±0.35s, durations permitting).
7. `submit_plan` a HAND-WRITTEN complete plan: `clips` whose trim values are
   the card shot boundaries (may split/merge shots, mind the durations),
   `timeline` with fade transitions, plus these agent-only keys:
   `"_title": "<piece title>"`,
   `"_narration": [{"group_id": "g1", "clip_ids": ["c1","c2"],
                     "text": "<the whole narration paragraph for this group>",
                     "units": ["句一。", "句二。"]}, ...]`
   (`text` = the group's raw narration paragraph; `units` = the same text
   split into subtitle sentences — you may omit `units` and the compiler
   will split `text` deterministically by punctuation). The compiler lays
   the units onto each group's time window proportionally to character
   count — one shot may carry several sentences, a sentence may cross a
   cut point.
   For simple one-line-per-shot captions use `"_captions": [...]` +
   `"_caption_style"` ("bottom" | "credits") instead; `_narration` and
   `_captions` are mutually exclusive. Do NOT write these keys together with
   a `workflow` block.
8. Simple batch/parameter-only tasks may still use the `workflow` macros
   (deterministic fallback) — see the workflow doc below."""


# ------------------------------------------------------------------ 注册表 -- #

GENERAL = CreativeSkill(
    name="general_vlog",
    summary="any one-click creation / vlog / 作品集 when no specific skill "
            "fits (default)",
    flavor="""### Skill: general_vlog (default)
Narration tone: like the person who shot it talking to a friend — concrete,
first-person, no travel-magazine phrasing. Pacing: let the shots breathe;
the Hook group may be a single strong shot."""
)

TRAVEL = CreativeSkill(
    name="travel_vlog",
    summary="旅行 / 出行 / 户外素材（路线、地标、风景、同行人）",
    flavor="""### Skill: travel_vlog
Structure the groups along the trip's own arc (出发 → 在路上 → 到达 →
玩/看 → 收尾), not by scene similarity alone. Hook: the single most
spectacular 2–4s shot (wide vista / motion), even if it breaks chronology.
Narration: places and senses (风、光、味道、声音), first-person past tense;
end with one sentence about wanting to come back, not a moral."""
)

FOOD = CreativeSkill(
    name="food_vlog",
    summary="美食 / 探店 / 做菜过程（食材、烹饪、成品、环境）",
    flavor="""### Skill: food_vlog
Structure around the making: 食材/环境 → 过程（火、翻动、冒气这类动作
特写优先）→ 成品 → 吃/反应。Hook: the most appetizing motion shot
(拉丝、浇汁、冒气) in the first 2 seconds. Narration: verbs and sensory
nouns (切、煸、滋啦、焦香), no 「治愈」「幸福感」; group lengths follow
the cooking beats, shorter cuts during action, longer on the finished dish."""
)

CREATIVE_SKILLS: dict[str, CreativeSkill] = {
    s.name: s for s in (GENERAL, TRAVEL, FOOD)
}


def creative_menu() -> str:
    return "/".join(sorted(CREATIVE_SKILLS))


def render_creative_doc() -> str:
    """派生提示词里的创作流文档（video_agent 的 {CREATIVE_DOC} 占位符）。"""
    lines = ["## Creative workflow skills (V8 — pick ONE by task intent and "
             "drive it yourself)",
             "Available skills (choose by intent; if none fits, use "
             "`general_vlog`):"]
    for s in CREATIVE_SKILLS.values():
        lines.append(f"- `{s.name}`: {s.summary}")
    lines.append("")
    lines.append(COMMON_STEPS)
    lines.append("")
    lines.append("Per-skill notes:")
    for s in CREATIVE_SKILLS.values():
        if s.flavor:
            lines.append(s.flavor)
    return "\n".join(lines)
