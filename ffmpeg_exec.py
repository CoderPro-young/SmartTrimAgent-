"""ffmpeg / ffprobe 执行器。

供 plan_compiler 与 video_agent 复用：检测工具是否可用，执行命令并捕获输出。
本机未装 ffmpeg 时，run 返回明确的"未安装"错误（命令仍完整保留）。
"""

from __future__ import annotations

import shlex
import shutil
import subprocess

FFMPEG_TIMEOUT = 600


def which(name: str) -> str | None:
    """返回可执行文件的完整路径，找不到返回 None。"""
    return shutil.which(name)


def ffmpeg_available() -> bool:
    return which("ffmpeg") is not None


def ffprobe_available() -> bool:
    return which("ffprobe") is not None


def run(cmd: str | list[str], timeout: int = FFMPEG_TIMEOUT, cwd: str | None = None) -> dict:
    """执行命令，返回 {ok, returncode, stdout, stderr, command}。

    ffmpeg/ffprobe 不在 PATH 时返回 ok=False 且 error 说明"未安装"。
    """
    argv = shlex.split(cmd) if isinstance(cmd, str) else list(cmd)
    if not argv:
        return {"ok": False, "error": "空命令", "command": cmd}
    tool = argv[0]
    if shutil.which(tool) is None:
        return {
            "ok": False,
            "returncode": None,
            "error": (
                f"`{tool}` 未安装（PATH 查找失败），命令未执行。"
                f"请安装 ffmpeg（含 ffprobe）：Windows `winget install ffmpeg`，"
                f"安装后重跑即可真实渲染。"
            ),
            "command": cmd,
        }
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, cwd=cwd
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
            "command": cmd,
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "returncode": None,
            "error": f"命令超时（>{timeout}s）",
            "command": cmd,
        }


def probe(path: str) -> dict:
    """ffprobe 获取素材元数据；不可用/失败时返回标记 note 的最小结果。"""
    if not ffprobe_available():
        return {
            "path": path,
            "available": False,
            "note": "ffprobe 未安装，无法探测真实时长/分辨率/编码；"
                    "video 素材请在计划里显式给出 trim_end。",
        }
    res = run(
        [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", path,
        ]
    )
    if not res["ok"]:
        return {"path": path, "available": True, "error": res.get("stderr") or res.get("error")}
    import json
    try:
        return {"path": path, "available": True, "data": json.loads(res["stdout"])}
    except json.JSONDecodeError:
        return {"path": path, "available": True, "error": "ffprobe 输出解析失败", "raw": res["stdout"][:500]}
