"""ffmpeg / ffprobe 执行器。

供 plan_compiler 与 video_agent 复用：检测工具是否可用，执行命令并捕获输出。
本机未装 ffmpeg 时，run 返回明确的"未安装"错误（命令仍完整保留）。
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import threading

FFMPEG_TIMEOUT = 600

# 正在运行的 ffmpeg/ffprobe 子进程登记（取消按钮用：/api/cancel 时全部终止）。
# RUN_LOCK 保证同一时刻只有一个「任务」，但后台内容分析也会起 ffmpeg，
# 因此用集合 + 锁，而不是单例句柄。
_PROC_LOCK = threading.Lock()
_PROCS: set[subprocess.Popen] = set()


def kill_all() -> int:
    """终止当前登记的所有子进程，返回杀掉的数量（用于「取消任务」）。"""
    with _PROC_LOCK:
        procs = list(_PROCS)
        _PROCS.clear()
    n = 0
    for p in procs:
        try:
            if p.poll() is None:
                p.kill()
                n += 1
        except Exception:
            pass
    return n


def _run_tracked(argv: list[str], timeout: int, cwd: str | None) -> dict:
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, cwd=cwd)
    with _PROC_LOCK:
        _PROCS.add(proc)
    try:
        out, err = proc.communicate(timeout=timeout)
        returncode = proc.returncode
        timed_out = False
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        returncode = proc.returncode
        timed_out = True
    finally:
        with _PROC_LOCK:
            _PROCS.discard(proc)
    if timed_out:
        return {"ok": False, "returncode": returncode,
                "stdout": out or "", "stderr": err or "",
                "error": f"命令超时（>{timeout}s）", "command": argv}
    return {"ok": returncode == 0, "returncode": returncode,
            "stdout": out or "", "stderr": err or "", "command": argv}


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
    进程被 kill_all() 终止时 returncode 非零（前端表现为该命令失败/取消）。
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
        return _run_tracked(argv, timeout, cwd)
    except OSError as exc:
        return {"ok": False, "returncode": None, "error": f"启动失败：{exc}",
                "command": cmd}


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
    try:
        return {"path": path, "available": True, "data": json.loads(res["stdout"])}
    except json.JSONDecodeError:
        return {"path": path, "available": True, "error": "ffprobe 输出解析失败", "raw": res["stdout"][:500]}
