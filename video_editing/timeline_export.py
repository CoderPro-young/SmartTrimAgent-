"""粗剪时间线导出（V6 粗剪漏斗第③层）：计划 → 精剪软件可导入的格式。

粗剪的下一站往往是精剪软件（剪映专业版/Premiere/Resolve 都认 EDL）。
本模块把编译后的计划+时间轴数学映射成两种格式：

- EDL（CMX3600）：业界最通用的交换格式；视频轨的源入出点/录制入出点
  逐片段写出，剪映专业版/Premiere/Resolve/FCP 均可导入。转场在 EDL 里
  以注释行标注（视觉提示，导入后按硬切处理——粗剪语义下够用）。
- CSV：人类可读的剪辑表（源/入点/出点/时长/时间线位置），给用户核对
  或导入表格。

实现参考 auto-editor（src/exports/）：EDL 行格式
  `001  AX V C  00:00:00:00 00:00:08:10 00:00:00:00 00:00:08:10`
（事件号 · 素材通道 · 轨 · 切换类型 · 源入 · 源出 · 录入 · 录出）。
"""

from __future__ import annotations

import csv
import io


def _tc(seconds: float, fps: int) -> str:
    """秒 → HH:MM:SS:FF 时间码（帧取整，负值钳到 0）。"""
    s = max(0.0, float(seconds or 0))
    total = int(round(s * fps))
    f = total % fps
    sec = total // fps
    return f"{sec // 3600:02d}:{(sec // 60) % 60:02d}:{sec % 60:02d}:{f:02d}"


def _safe_title(name: str, limit: int = 70) -> str:
    """EDL TITLE 行只留安全字符。"""
    return "".join(c for c in name if c.isalnum() or c in " ._-")[:limit] or "roughcut"


def to_edl(plan: dict, math: dict) -> str:
    """计划 + 编译数学 → CMX3600 EDL 文本。"""
    fps = int(plan.get("output", {}).get("fps") or math.get("fps") or 30)
    out_name = (plan.get("output", {}).get("filename") or "OUTPUT/roughcut.mp4").split("/")[-1]
    lines = [f"TITLE: {_safe_title(out_name.rsplit('.', 1)[0])}", "FCM: NON-DROP FRAME", ""]

    by_id = {c.get("id"): c for c in plan.get("clips") or []}
    for i, cid in enumerate(math.get("order") or []):
        clip = by_id.get(cid)
        if not clip:
            continue
        d = math["durations"].get(cid) or 0
        s = math["starts"].get(cid) or 0
        src_in = clip.get("trim_start") or 0
        src_out = (clip.get("trim_end") if clip.get("trim_end") is not None
                   else src_in + d)
        lines.append(
            f"{i + 1:03d}  AX V C        "
            f"{_tc(src_in, fps)} {_tc(src_out, fps)} "
            f"{_tc(s, fps)} {_tc(s + d, fps)}"
        )
        lines.append(f"* FROM CLIP NAME: {clip.get('source', '')}")
        effects = [e.get("name") for e in (clip.get("effects") or []) if isinstance(e, dict)]
        if effects:
            lines.append(f"* EFFECTS: {', '.join(effects)}")
        tr = (math.get("transitions") or {}).get(i)  # 挂在当前项上 = 与上一段的转场
        if tr:
            lines.append(f"* TRANSITION IN: {tr.get('type')} {tr.get('duration')}s")
        lines.append("")
    return "\n".join(lines)


def to_csv(plan: dict, math: dict) -> str:
    """计划 + 编译数学 → 剪辑表 CSV（UTF-8 BOM，Excel 直接打开不乱码）。"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["#", "素材", "源入点(s)", "源出点(s)", "片段时长(s)",
                "时间线起点(s)", "转场(进入)", "效果"])
    by_id = {c.get("id"): c for c in plan.get("clips") or []}
    for i, cid in enumerate(math.get("order") or []):
        clip = by_id.get(cid) or {}
        d = math["durations"].get(cid) or 0
        src_in = clip.get("trim_start") or 0
        src_out = (clip.get("trim_end") if clip.get("trim_end") is not None
                   else round(src_in + d, 3))
        tr = (math.get("transitions") or {}).get(i)
        effects = ", ".join(e.get("name", "") for e in (clip.get("effects") or [])
                            if isinstance(e, dict))
        w.writerow([i + 1, clip.get("source", ""), src_in, src_out, d,
                    math["starts"].get(cid) or 0,
                    f"{tr.get('type')} {tr.get('duration')}s" if tr else "硬切",
                    effects])
    return "\ufeff" + buf.getvalue()


def export_files(plan: dict, math: dict, out_path: str) -> list[str]:
    """把 EDL/CSV 写到成片同目录同名（仅扩展名不同），返回相对路径列表。

    out_path —— 成片路径（OUTPUT/xxx.mp4）；导出 OUTPUT/xxx.edl 与 OUTPUT/xxx.csv。
    """
    import os
    stem = out_path.rsplit(".", 1)[0]
    written = []
    for ext, content in ((".edl", to_edl(plan, math)), (".csv", to_csv(plan, math))):
        p = stem + ext
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        written.append(p)
    return written
