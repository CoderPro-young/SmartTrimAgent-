"""SmartTrimAgent Web 入口（零第三方依赖，只用标准库）。

    .venv/Scripts/python.exe web/server.py [--port 8000] [--host 127.0.0.1]

浏览器打开 http://127.0.0.1:8000

路由：
    GET  /                → 单页前端（web/index.html）
    GET  /api/health      → ffmpeg/ffprobe 可用性 + 当前模型
    GET  /api/inputs      → 列出 INPUT/ 下素材（?probe=1 附带 ffprobe 信息）
    GET  /api/outputs     → 列出 OUTPUT/ 下已有产物
    POST /api/run         → 运行一次任务，NDJSON 流式返回事件
    GET  /media/<path>    → 预览产物（限制在 OUTPUT/ 内，支持 Range 拖动进度）
    GET  /input/<name>    → 预览原始素材（限制在 INPUT/ 内，同样支持 Range）

设计上与 video_demo.py 保持一致的三步流程
（agent 出计划 → 编译器出命令 → 执行 + 回验），
区别是把过程作为结构化事件流吐给前端，而不是打印到控制台。

额外加了一个 CLI 里没有的能力：计划校验失败时，把编译器的中文错误
回传给 LLM 让它重出计划（plan_schema 的设计意图），默认最多重试 2 次。
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import queue
import re
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote

# ---- 路径准备（与 video_demo.py 同款：根目录 + video_editing 平铺导入）----
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "video_editing"))

import ffmpeg_exec  # noqa: E402
import plan_compiler  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from skills import SKILLS  # noqa: E402
from video_agent import (  # noqa: E402
    build_answer_brief,
    build_revision_brief,
    build_video_agent_v2,
    plan_with_retry,
)

MAX_RETRIES = 2          # 计划校验失败后回传 LLM 重出的次数上限
RUN_LOCK = threading.Lock()  # TMP/ 是共享的，同一时刻只允许一次运行
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "OUTPUT")
INPUT_DIR = os.path.join(PROJECT_ROOT, "INPUT")
LEDGER_PATH = os.path.join(PROJECT_ROOT, "TMP", "uploads.json")
MAX_UPLOAD = 500 * 1024 * 1024   # 单文件上限，可用 --max-upload-mb 覆盖

# 多轮会话（v3.1 P0）：单用户本地工具，进程内 dict 足够；
# 「启动新任务」= 前端换新 session_id，旧会话惰性 GC，无需删除接口。
SESSIONS: dict[str, dict] = {}
SESSION_MAX_TURNS = 5        # 每会话最多保留几轮历史（控制 token）
_SESSION_LOCK = threading.Lock()

# 上传白名单。视频/图片可作 clip；纯音频只能作 BGM（顶层 audio.source），
# 不能进 clips —— plan_schema 会拦下没有视频轨的 clip。
VIDEO_EXT = {
    ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".flv", ".wmv",
    ".mpg", ".mpeg", ".m2ts", ".mts", ".ts", ".3gp", ".ogv", ".rmvb", ".asf",
}
IMAGE_EXT = {
    ".png", ".jpg", ".jpeg", ".jfif", ".webp", ".bmp", ".gif",
    ".tif", ".tiff", ".heic", ".avif",
}
AUDIO_EXT = {
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".oga",
    ".wma", ".opus", ".aiff", ".aif", ".amr",
}
ALLOWED_EXT = VIDEO_EXT | IMAGE_EXT | AUDIO_EXT

# ffprobe 结果缓存（key: 绝对路径 -> (mtime, size, info)），避免每次刷新页面都重探
_PROBE_CACHE: dict[str, tuple[float, int, dict]] = {}


def _guess_kind(name: str) -> str | None:
    ext = os.path.splitext(name)[1].lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in IMAGE_EXT:
        return "image"
    if ext in AUDIO_EXT:
        return "audio"
    return None


def _list_materials() -> list[str]:
    """INPUT/ 下可用于剪辑的素材名（跳过 .part 之类的隐藏临时文件）。"""
    if not os.path.isdir(INPUT_DIR):
        return []
    return sorted(
        n for n in os.listdir(INPUT_DIR)
        if not n.startswith(".") and _guess_kind(n) and os.path.isfile(os.path.join(INPUT_DIR, n))
    )


def _safe_name(raw: str) -> str | None:
    """把用户给的文件名收敛成一个安全的单段文件名。"""
    name = os.path.basename(unquote(raw or "").replace("\\", "/")).strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if not name or name in (".", ".."):
        return None
    stem, ext = os.path.splitext(name)
    if not stem:
        return None
    return stem[:80] + ext[:12].lower()


def _load_ledger() -> list[str]:
    """记录哪些 INPUT/ 文件是「网页上传」的——只有这些才允许在界面上删除，
    避免误删 git 里的示例素材。"""
    try:
        with open(LEDGER_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return [x for x in data if isinstance(x, str)]
    except (OSError, json.JSONDecodeError):
        return []


def _save_ledger(names: list[str]) -> None:
    os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
    with open(LEDGER_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(set(names)), f, ensure_ascii=False, indent=2)


def _probe_file(full_path: str) -> dict:
    """用 ffprobe 校验并提取素材信息；带 mtime 缓存。"""
    try:
        st = os.stat(full_path)
    except OSError:
        return {"ok": False, "error": "文件不存在"}
    cached = _PROBE_CACHE.get(full_path)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]

    info = _probe_uncached(full_path)
    _PROBE_CACHE[full_path] = (st.st_mtime, st.st_size, info)
    return info


def _probe_uncached(full_path: str) -> dict:
    res = ffmpeg_exec.probe(full_path)
    if not res.get("available"):
        return {"ok": False, "error": "ffprobe 不可用，无法校验素材（请先安装 ffmpeg）。"}
    data = res.get("data")
    if not data:
        reason = (res.get("error") or "无法识别该文件").strip().splitlines()
        return {"ok": False, "error": f"ffmpeg 解不开这个文件：{reason[-1][:160] if reason else ''}"}

    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None and audio is None:
        return {"ok": False, "error": "文件里既没有视频/图像轨，也没有音频轨。"}

    if video is None:
        # 纯音频：只允许作为 BGM（顶层 audio.source）；不能当 clip（plan_schema 会拦）
        raw = (data.get("format") or {}).get("duration") or audio.get("duration")
        try:
            dur = round(float(raw), 3)
        except (TypeError, ValueError):
            dur = None
        return {
            "ok": True,
            "kind": "audio",
            "duration": dur,
            "width": None,
            "height": None,
            "codec": audio.get("codec_name"),
            "fps": None,
            "has_audio": True,
            "has_video": False,
            "channels": audio.get("channels"),
            "sample_rate": audio.get("sample_rate"),
        }

    duration = None
    raw_dur = (data.get("format") or {}).get("duration") or video.get("duration")
    try:
        duration = round(float(raw_dur), 3)
    except (TypeError, ValueError):
        duration = None      # 静态图片没有时长，正常

    fps = None
    num, _, den = (video.get("r_frame_rate") or "").partition("/")
    try:
        if float(den):
            fps = round(float(num) / float(den), 2)
    except (TypeError, ValueError):
        pass

    return {
        "ok": True,
        "duration": duration,
        "width": video.get("width"),
        "height": video.get("height"),
        "codec": video.get("codec_name"),
        "fps": fps,
        "has_audio": audio is not None,
        "has_video": True,
    }



# --------------------------------------------------------------------------- #
# 事件流：把 agent 过程转成结构化事件
# --------------------------------------------------------------------------- #

def _retire(full: str, name: str) -> tuple[str, str]:
    """把素材移出 INPUT/。

    优先真删；若被环境拦下（某些沙箱/安全策略会把删除重定向到回收站，回收站
    不可用时直接报错），退化成一个改名操作——移到 TMP/removed/。改名不受那套
    机制限制，而且可恢复，效果上同样让素材从 agent 视野里消失。

    返回 (mode, detail)，mode ∈ {"deleted", "moved"}；彻底失败抛原异常。
    """
    try:
        os.remove(full)
        return "deleted", ""
    except Exception as first_exc:   # 宽捕获：safe-delete 钩子的异常未必是 OSError
        pass

    trash = os.path.join(PROJECT_ROOT, "TMP", "removed")
    os.makedirs(trash, exist_ok=True)
    target = os.path.join(trash, name)
    if os.path.exists(target):
        stem, ext = os.path.splitext(name)
        target = os.path.join(trash, f"{stem}-{int(time.time())}{ext}")
    try:
        os.replace(full, target)
    except Exception as exc2:
        # 降级也失败：把两次异常都记进服务日志，便于定位是哪种拦截
        sys.stderr.write(f"[retire] 真删失败: {type(first_exc).__name__}: {first_exc}\n")
        sys.stderr.write(f"[retire] 降级移动也失败: {type(exc2).__name__}: {exc2}\n")
        sys.stderr.flush()
        raise first_exc
    return "moved", os.path.relpath(target, PROJECT_ROOT).replace("\\", "/")


def _retire_quietly(path: str) -> None:
    """丢弃上传失败的临时文件（同理：真删不行就挪进 TMP/removed/）。"""
    try:
        _retire(path, os.path.basename(path))
    except Exception:
        pass


def _msg_text(msg: AIMessage) -> str:
    """取 AIMessage 的文本内容（兼容 content blocks 列表形式）。"""
    content = msg.content or ""
    if isinstance(content, list):
        return "\n".join(
            (b.get("text", "") if isinstance(b, dict) else str(b)) for b in content
        )
    return str(content)


def _stream_agent(agent, messages: list, emit, seen: set | None = None,
                  on_llm=None) -> dict:
    """流式跑 agent，每产生一条新的 AIMessage 就 emit 一次事件。

    按消息 id 去重，避免同一条历史消息被重复 emit。
    `seen` 可由调用方跨多次调用传入——否则重试时历史消息会被重新流出来、
    在界面上重复显示一遍。
    `on_llm(text, tool_calls)` 可选：每条新 AIMessage 的回调（L1 幻觉扫描用）。
    """
    if seen is None:
        seen = set()
    final: dict = {}
    for state in agent.stream({"messages": messages}, stream_mode="values"):
        final = state
        msgs = state.get("messages", [])
        if not msgs:
            continue
        last = msgs[-1]
        if not isinstance(last, AIMessage):
            continue
        key = getattr(last, "id", None) or repr(last.content)[:100]
        if key in seen:
            continue
        seen.add(key)
        emit({
            "type": "llm",
            "text": _msg_text(last).strip(),
            "tool_calls": [
                {"name": tc.get("name"), "args": tc.get("args")}
                for tc in (last.tool_calls or [])
            ],
        })
        if on_llm:
            on_llm(
                _msg_text(last).strip(),
                [{"name": tc.get("name"), "args": tc.get("args")}
                 for tc in (last.tool_calls or [])],
            )
    return final


# ------------------------------------------------------- 多轮会话 / 闸门 ---- #

def _get_session(session_id: str | None, task: str) -> tuple[dict, bool]:
    """取会话；不存在则新建（无状态兜底：未知 id 当第一轮）。

    返回 (session, created)。不带 session_id 的请求每次都拿到独立临时会话
    （等价于旧行为：无修订上下文）。
    """
    if not session_id:
        return ({"task": task, "turn": 0, "last_plan": None, "last_output": None,
                 "last_verify": None, "pending_question": None, "history": []}, True)
    with _SESSION_LOCK:
        session = SESSIONS.get(session_id)
        if session is None:
            session = {"task": task, "turn": 0, "last_plan": None, "last_output": None,
                       "last_verify": None, "pending_question": None, "history": []}
            SESSIONS[session_id] = session
            while len(SESSIONS) > 20:      # 惰性上限，防内存膨胀
                SESSIONS.pop(next(iter(SESSIONS)), None)
            return session, True
        return session, False


_GATE_CHITCHAT = {
    "哈哈", "哈哈哈", "呵呵", "在吗", "你好", "您好", "hi", "hello",
    "测试", "test", "111", "。。。", "。。", "??", "???",
}
_GATE_EDIT_HINTS = {
    "剪", "裁", "拼", "接", "转", "加", "去", "换", "调", "码", "镜像", "倍速",
    "分辨率", "1080", "720", "4k", "封面", "字幕", "花字", "音乐", "bgm", "音轨",
    "声音", "导出", "输出", "打码", "马赛克", "画中画", "水印", "淡入", "淡出",
}


def _intent_gate(task: str) -> dict | None:
    """规则闸门：只拦「一眼没意图」的高置信度负例，其余放行给 agent + ask_user。

    判定（全部满足才拦）：短（≤6 字）+ 不含任何剪辑关键词 + 命中闲聊词表
    或去标点后 ≤2 字。误伤代价低——拦错只是多问一次，agent 侧照常兜底。
    """
    t = (task or "").strip()
    if not t:
        return None
    core = re.sub(r"[\W_]+", "", t, flags=re.UNICODE)
    if len(t) <= 6 and not any(k in t.lower() for k in _GATE_EDIT_HINTS) \
            and (t in _GATE_CHITCHAT or len(core) <= 2):
        return {
            "type": "question", "kind": "intent_gate",
            "question": "这条消息我没看出要做什么剪辑处理。你想对素材做什么？",
            "options": ["拼接片段", "裁剪片段", "加字幕 / 花字", "加转场",
                        "给人脸打码", "调整输出分辨率"],
        }
    return None


def _l1_scan(text: str, tool_calls: list, existing: set) -> list[dict]:
    """L1 幻觉扫描（启发式，只标记不拦截）。

    信号：① 文本/工具参数引用了 INPUT/ 里不存在的文件；
    ② 文本声称使用了能力注册表之外的效果名。
    """
    signals: list[dict] = []
    blob = text or ""
    for tc in tool_calls or []:
        blob += " " + json.dumps(tc.get("args") or {}, ensure_ascii=False, default=str)
    for ref in set(re.findall(r"INPUT/[A-Za-z0-9_\-.\u4e00-\u9fff]+", blob)):
        name = ref[len("INPUT/"):]
        if name and name not in existing:
            signals.append({
                "signal": "missing_source_ref",
                "evidence": f"提到了素材 {ref}，但 INPUT/ 里没有这个文件。",
            })
    known = set(SKILLS) | {
        "拼接", "裁剪", "转场", "字幕", "花字", "画中画", "水印", "打码", "马赛克",
        "镜像", "翻转", "变速", "调色", "淡入", "淡出", "背景音乐", "混音",
    }
    for claim in re.findall(r"(?:加了|添加|应用了|使用了|做了|用了)\s*([A-Za-z_][A-Za-z0-9_]*)", text or ""):
        if claim not in known:
            signals.append({
                "signal": "unknown_effect_claim",
                "evidence": f"声称使用了「{claim}」，但它不在能力注册表里。",
            })
    return signals


def _run_pipeline(task: str, emit, session_id: str | None = None) -> None:
    """完整流程：出计划 → Preflight → 编译 → 执行 → 回验。所有进度通过 emit 上报。

    多轮（v3.1）：同一 session_id 内，每条消息按「问答接续 > 修订 > 首轮」
    优先级构造上下文；只有前端点「启动新任务」换新 id 才重置。
    """
    if not task:
        emit({"type": "error", "message": "请先描述剪辑需求（可以先上传素材，再写一句需求）。"})
        return

    materials = _list_materials()
    if not materials:
        emit({
            "type": "error",
            "message": "INPUT/ 里还没有素材。请先在左侧拖入或选择视频/图片，再提交需求。",
        })
        return
    emit({"type": "materials", "names": materials})

    session, _created = _get_session(session_id, task)

    # 规则闸门（零成本拦「一眼没意图」；question 也是 pending_question）
    gate = _intent_gate(task)
    if gate:
        emit(gate)
        session["pending_question"] = gate
        return

    # 消息构造三优先级（纯文本，遵守「不重放消息历史」约束）
    pending = session.get("pending_question")
    base_plan = session.get("last_plan")
    base_output = session.get("last_output")
    if pending:
        messages = build_answer_brief(pending, task)
    elif base_plan is not None:
        messages = build_revision_brief(
            session["task"], base_plan, base_output,
            ["INPUT/" + m for m in materials], task,
        )
    else:
        messages = None          # plan_with_retry 内部用 task 单条

    agent = build_video_agent_v2()
    seen: set = set()
    existing = set(materials)

    def on_llm(text: str, tool_calls: list) -> None:
        for sig in _l1_scan(text, tool_calls, existing):
            emit({"type": "hallucination", "level": "warn", **sig})

    probe_fn = lambda src: _probe_file(os.path.join(PROJECT_ROOT, src))  # noqa: E731
    suffix = f"-r{(session.get('turn') or 0) + 1}"

    # 拦截 question 事件写入会话（plan_with_retry 不感知会话，用包裹 emit 传递）
    question_holder: dict = {}

    def tracking_emit(ev) -> None:
        if ev and ev.get("type") == "question":
            question_holder["q"] = ev
        emit(ev)

    # 重试闭环（含 Preflight 三分流）已下沉到 video_agent.plan_with_retry，
    # CLI 与 Web 共用同一套语义；这里只负责把事件转成 NDJSON。
    compiled, _result = plan_with_retry(
        task,
        PROJECT_ROOT,
        invoke=lambda m: _stream_agent(agent, m, tracking_emit, seen, on_llm=on_llm),
        emit=tracking_emit,
        max_retries=MAX_RETRIES,
        messages=messages,
        base_output=base_output,
        output_suffix=suffix if base_output else None,
        probe_fn=probe_fn,
    )
    if compiled is None:
        # question / 放弃类返回：事件已 emit，last_plan 不动；
        # 若本轮产生过 question，记为待答，等用户下一条消息接续
        if question_holder:
            session["pending_question"] = question_holder["q"]
        return

    # 成功提交新计划 → 更新会话（问答接续完成，pending_question 清除）
    session["last_plan"] = compiled.plan
    session["last_output"] = compiled.plan["output"]["filename"]
    session["pending_question"] = None
    session["turn"] = (session.get("turn") or 0) + 1
    history = session.setdefault("history", [])
    history.append({"task": task, "output": session["last_output"]})
    del history[:-SESSION_MAX_TURNS]

    emit({"type": "math", "math": compiled.math})
    emit({
        "type": "commands",
        "commands": [
            {"stage": c.stage, "description": c.description, "line": c.line}
            for c in compiled.commands
        ],
    })

    emit({"type": "status", "text": "开始执行命令（归一化 → 渲染）…"})
    plan_compiler.execute(compiled, PROJECT_ROOT)
    for ex in compiled.executes:
        ok = bool(ex.get("ok"))
        emit({
            "type": "exec",
            "description": ex.get("description"),
            "ok": ok,
            "returncode": ex.get("returncode"),
            "error": ex.get("error"),
            # 失败时把 stderr 尾巴带上，便于在前端直接定位
            "stderr_tail": "" if ok else (ex.get("stderr") or "")[-1500:],
        })

    plan_compiler.verify(compiled, PROJECT_ROOT)
    session["last_verify"] = compiled.verify
    emit({"type": "verify", "verify": compiled.verify})

    out = compiled.plan["output"]["filename"]
    emit({
        "type": "done",
        "output": out,
        "url": "/media/" + out.replace("\\", "/"),
        "ok": bool((compiled.verify or {}).get("ok")),
    })


def _safe_run(task: str, emit, session_id: str | None = None) -> None:
    """包一层异常兜底：任何未预期异常都要作为事件上报，不能让前端空等。"""
    acquired = RUN_LOCK.acquire(timeout=1)
    if not acquired:
        emit({"type": "error", "message": "已有任务正在运行，请等它结束再提交。"})
        emit(None)
        return
    try:
        _run_pipeline(task, emit, session_id)
    except Exception as exc:  # noqa: BLE001
        emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        emit({"type": "traceback", "text": traceback.format_exc()[-2000:]})
    finally:
        RUN_LOCK.release()
        emit(None)  # 结束哨兵


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "SmartTrimAgent"
    protocol_version = "HTTP/1.0"   # 用「连接关闭」界定流式响应体，无需 chunked

    # ---- 工具 ----
    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, full: str) -> None:
        """发送文件，支持 Range（<video> 拖动进度条需要 206）。"""
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        size = os.path.getsize(full)
        start, end = 0, size - 1

        rng = self.headers.get("Range")
        status = 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                elif m.group(2):           # 后缀写法 bytes=-N
                    start = max(0, size - int(m.group(2)))
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                end = min(end, size - 1)
                status = 206

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(full, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _fail(self, status: int, message: str) -> None:
        self._send_json({"ok": False, "error": message}, status)

    def _drain_body(self, limit: int = 2 * 1024 ** 3) -> None:
        """读掉并丢弃还没消费的请求体。

        提前拒绝（413/415）时必须先排空再回响应：否则客户端还在上传、服务端已经
        关连接，浏览器 / urllib 拿到的是 ConnectionAborted，而不是我们精心写的
        「文件超过上限」这类 JSON 提示。排空量由客户端声明的 Content-Length 界定。
        """
        try:
            remaining = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return
        remaining = min(remaining, limit)
        while remaining > 0:
            try:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
            except OSError:
                return
            if not chunk:
                return
            remaining -= len(chunk)

    def _reject(self, status: int, message: str) -> None:
        """提前拒绝：先排空请求体，再回错误。"""
        self._drain_body()
        self._fail(status, message)

    def _handle_upload(self, raw_name: str) -> None:
        """接收裸二进制请求体（文件名走 query），写入 INPUT/。

        没有用 multipart/form-data：标准库解析 multipart 又长又容易出错，
        而前端 `fetch(url, {body: file})` 直接发裸体更简单、更快。
        归属不按会话记账：进白名单（TMP/uploads.json）即视为本任务素材，
        「启动新任务」时白名单整体清空。
        """
        name = _safe_name(raw_name)
        if not name:
            self._reject(400, "文件名不合法。")
            return
        kind = _guess_kind(name)
        if kind is None:
            ext = os.path.splitext(name)[1] or "(无扩展名)"
            self._reject(415, f"不支持的格式 {ext}；支持：{'、'.join(sorted(ALLOWED_EXT))}")
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._reject(400, "空文件。")
            return
        if length > MAX_UPLOAD:
            self._reject(413, f"文件超过上限 {MAX_UPLOAD // (1024 * 1024)}MB。")
            return

        os.makedirs(INPUT_DIR, exist_ok=True)
        ledger = _load_ledger()

        # 重名处理：只有「本身上传来的」才允许覆盖（方便反复替换同一素材），
        # 碰到示例素材等非上传文件则自动加序号，绝不覆盖。
        target_name, target = name, os.path.join(INPUT_DIR, name)
        if os.path.exists(target) and name not in ledger:
            stem, ext = os.path.splitext(name)
            for i in range(1, 1000):
                cand = f"{stem}-{i}{ext}"
                if not os.path.exists(os.path.join(INPUT_DIR, cand)):
                    target_name, target = cand, os.path.join(INPUT_DIR, cand)
                    break

        # 先落临时文件（保留扩展名，让 ffprobe 按内容识别），校验通过再原子改名；
        # 任何一步失败都丢弃临时文件，不留下半个素材。
        _, ext = os.path.splitext(target_name)
        part = os.path.join(INPUT_DIR, f".upload-{uuid.uuid4().hex[:10]}{ext}")
        written = 0
        try:
            with open(part, "wb") as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
                    remaining -= len(chunk)
        except OSError as exc:
            _retire_quietly(part)
            self._fail(500, f"写入失败：{exc}")
            return

        if written != length:
            _retire_quietly(part)
            self._fail(400, f"上传中断：只收到 {written}/{length} 字节。")
            return

        info = _probe_file(part)
        if not info.get("ok"):
            _retire_quietly(part)
            self._fail(422, info.get("error") or "素材校验失败。")
            return

        try:
            os.replace(part, target)
        except OSError as exc:
            _retire_quietly(part)
            self._fail(500, f"保存失败：{exc}")
            return

        _PROBE_CACHE.pop(part, None)
        _PROBE_CACHE[target] = (os.path.getmtime(target), os.path.getsize(target), info)
        _save_ledger(ledger + [target_name])

        self._send_json({
            "ok": True,
            "name": target_name,
            "path": "INPUT/" + target_name,
            "kind": kind,
            "size": os.path.getsize(target),
            "renamed": target_name != name,
            "probe": info,
        })

    def _handle_session_reset(self, payload: dict) -> None:
        """「启动新任务」：清掉全部网页上传的素材，然后废弃会话。

        归属不按会话记账：登记只存进程内存（SESSIONS）的话，服务一重启就丢，
        旧任务的素材便永远清不掉——而 INPUT/ 对 agent 是全局可见的，必须清得掉。
        因此以 TMP/uploads.json 白名单为准，一次清空其中全部文件；
        手动放进 INPUT/ 的文件与示例素材不在白名单内，不受影响。
        单个素材清不掉（被锁等）不阻断其余，errors 里逐条说明。
        """
        sid = (payload.get("session_id") or "").strip()
        if not sid:
            self._fail(400, "缺少 session_id。")
            return
        if RUN_LOCK.locked():
            self._fail(409, "有任务正在运行，等它结束再启动新任务。")
            return
        with _SESSION_LOCK:
            SESSIONS.pop(sid, None)
        cleared: list[str] = []
        errors: list[str] = []
        for name in _load_ledger():
            full = self._resolve_under(INPUT_DIR, name)
            if not full:
                continue
            try:
                _retire(full, name)
                cleared.append(name)
            except Exception as exc:     # 宽捕获：一个失败不拖垮整组
                errors.append(f"{name}: {exc}")
            _PROBE_CACHE.pop(full, None)
        if cleared:
            gone = set(cleared)
            _save_ledger([n for n in _load_ledger() if n not in gone])
        self._send_json({"ok": True, "cleared": cleared, "errors": errors})

    def _handle_delete(self, payload: dict) -> None:
        """移除素材：只允许动网页上传过的（白名单在 TMP/uploads.json）。

        真删还是移到 TMP/removed/ 由 _retire 决定，响应里的 mode 会告诉前端。
        """
        name = _safe_name(payload.get("name") or "")
        if not name:
            self._fail(400, "文件名不合法。")
            return
        ledger = _load_ledger()
        if name not in ledger:
            self._fail(403, "只能删除网页上传的素材；示例素材受保护。")
            return
        full = self._resolve_under(INPUT_DIR, name)
        if not full:
            self._fail(404, "文件不存在。")
            return
        try:
            mode, detail = _retire(full, name)
        except Exception as exc:     # 宽捕获：钩子异常类型不确定（见 _retire 注释）
            self._fail(500, f"移除失败：{exc}")
            return
        _PROBE_CACHE.pop(full, None)
        _save_ledger([n for n in ledger if n != name])
        self._send_json({"ok": True, "name": name, "mode": mode, "detail": detail})

    def _serve_media(self) -> None:
        """只暴露 OUTPUT/ 下的单个文件。

        必须锚定 OUTPUT_DIR 而不是 PROJECT_ROOT：锚到项目根的话
        `/media/.env` 会把 .env（含真实 API Key）直接吐给浏览器。
        因此这里只接受「无子目录的单段文件名」，并额外兼容 OUTPUT/ 前缀。
        """
        rel = unquote(self.path[len("/media/"):].split("?", 1)[0]).replace("\\", "/")
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if parts and parts[0] == "OUTPUT":   # 兼容 /media/OUTPUT/xxx.mp4
            parts = parts[1:]
        if len(parts) != 1:                  # 挡掉子目录与 ..
            self._send_bytes(b"404 not found", "text/plain; charset=utf-8", 404)
            return
        full = self._resolve_under(OUTPUT_DIR, parts[0])
        if not full:
            self._send_bytes(b"404 not found", "text/plain; charset=utf-8", 404)
            return
        self._send_file(full)

    def _serve_input(self) -> None:
        """只暴露 INPUT/ 下的单个素材文件（供前端预览原始素材）。

        与 _serve_media 同理且同样必须锚定 INPUT_DIR：锚到项目根的话
        `/input/../.env` 会把带 API Key 的 .env 吐出去。因此这里同样只接受
        「无子目录的单段文件名」，并兼容 /input/INPUT/xxx 这种带前缀的写法。
        """
        rel = unquote(self.path[len("/input/"):].split("?", 1)[0]).replace("\\", "/")
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if parts and parts[0] == "INPUT":    # 兼容 /input/INPUT/xxx.mp4
            parts = parts[1:]
        if len(parts) != 1:                  # 挡掉子目录与 ..
            self._send_bytes(b"404 not found", "text/plain; charset=utf-8", 404)
            return
        full = self._resolve_under(INPUT_DIR, parts[0])
        if not full:
            self._send_bytes(b"404 not found", "text/plain; charset=utf-8", 404)
            return
        self._send_file(full)

    def _resolve_under(self, base: str, rel: str) -> str | None:
        """把 rel 安全地解析到 base 之下；越界返回 None。"""
        root = os.path.realpath(base)
        full = os.path.realpath(os.path.join(root, rel))
        if full != root and not full.startswith(root + os.sep):
            return None
        return full if os.path.isfile(full) else None

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]

        if path in ("/", "/index.html"):
            full = os.path.join(WEB_DIR, "index.html")
            if not os.path.isfile(full):
                self._send_bytes(b"index.html not found", "text/plain; charset=utf-8", 500)
                return
            with open(full, "rb") as f:
                self._send_bytes(f.read(), "text/html; charset=utf-8")
            return

        if path == "/api/health":
            ff = ffmpeg_exec.which("ffmpeg")
            version = ""
            if ff:
                res = ffmpeg_exec.run(["ffmpeg", "-hide_banner", "-version"])
                first = (res.get("stdout") or "").splitlines()
                version = first[0][:60] if first else ""
            self._send_json({
                "ffmpeg": ff,
                "ffprobe": ffmpeg_exec.which("ffprobe"),
                "ffmpeg_version": version,
                "model": os.environ.get("MODEL_NAME", "deepseek-ai/DeepSeek-V4-Flash (默认)"),
                "busy": RUN_LOCK.locked(),
                "materials": _list_materials(),
                "upload": {
                    "max_mb": MAX_UPLOAD // (1024 * 1024),
                    "exts": sorted(ALLOWED_EXT),
                },
            })
            return

        if path == "/api/inputs":
            with_probe = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "").get("probe")
            ledger = set(_load_ledger())
            items = []
            if os.path.isdir(INPUT_DIR):
                for name in sorted(os.listdir(INPUT_DIR)):
                    if name.startswith("."):
                        continue          # 上传中的 .part 临时文件
                    kind = _guess_kind(name)
                    full = os.path.join(INPUT_DIR, name)
                    if kind is None or not os.path.isfile(full):
                        continue
                    item = {
                        "name": name,
                        "path": "INPUT/" + name,
                        "url": "/input/" + quote(name),
                        "kind": kind,
                        "size": os.path.getsize(full),
                        "mtime": os.path.getmtime(full),
                        "uploaded": name in ledger,
                    }
                    if with_probe:
                        item["probe"] = _probe_file(full)
                    items.append(item)
            items.sort(key=lambda x: (not x["uploaded"], -x["mtime"]))
            self._send_json({"inputs": items})
            return

        if path == "/api/outputs":
            items = []
            if os.path.isdir(OUTPUT_DIR):
                for name in sorted(os.listdir(OUTPUT_DIR)):
                    full = os.path.join(OUTPUT_DIR, name)
                    if os.path.isfile(full) and os.path.splitext(name)[1].lower() in VIDEO_EXT:
                        items.append({
                            "name": name,
                            "url": "/media/OUTPUT/" + name,
                            "size": os.path.getsize(full),
                            "mtime": os.path.getmtime(full),
                        })
            items.sort(key=lambda x: x["mtime"], reverse=True)
            self._send_json({"outputs": items})
            return

        if path.startswith("/media/"):
            self._serve_media()
            return

        if path.startswith("/input/"):
            self._serve_input()
            return

        self._send_bytes(b"404 not found", "text/plain; charset=utf-8", 404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]

        # 上传走裸二进制请求体，文件名在 query 上（不走 multipart）
        if path == "/api/upload":
            qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            self._handle_upload((qs.get("name") or [""])[0])
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send_json({"error": "请求体不是合法 JSON"}, 400)
            return

        if path == "/api/delete":
            self._handle_delete(payload)
            return

        if path == "/api/session/reset":
            self._handle_session_reset(payload)
            return

        if path != "/api/run":
            self._send_bytes(b"404 not found", "text/plain; charset=utf-8", 404)
            return

        task = (payload.get("task") or "").strip()
        session_id = (payload.get("session_id") or "").strip() or None
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        events: queue.Queue = queue.Queue()

        def emit(ev) -> None:
            events.put(ev)

        threading.Thread(target=_safe_run, args=(task, emit, session_id), daemon=True).start()

        while True:
            ev = events.get()
            if ev is None:
                break
            try:
                self.wfile.write((json.dumps(ev, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return  # 前端关掉了页面

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[web] %s - %s\n" % (self.address_string(), fmt % args))
        sys.stderr.flush()


def main() -> None:
    global MAX_UPLOAD
    ap = argparse.ArgumentParser(description="SmartTrimAgent Web 入口")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--max-upload-mb", type=int, default=MAX_UPLOAD // (1024 * 1024),
                    help="单个上传素材的大小上限（MB），默认 500")
    args = ap.parse_args()
    MAX_UPLOAD = max(1, args.max_upload_mb) * 1024 * 1024

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(INPUT_DIR, exist_ok=True)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    ff = ffmpeg_exec.which("ffmpeg") or "未找到（渲染会失败，请先装 ffmpeg）"
    print(f"SmartTrimAgent Web  →  http://{args.host}:{args.port}")
    print(f"  ffmpeg  : {ff}")
    print(f"  模型    : {os.environ.get('MODEL_NAME', 'deepseek-ai/DeepSeek-V4-Flash (默认)')}")
    print(f"  素材目录: {INPUT_DIR}（上传上限 {MAX_UPLOAD // (1024 * 1024)}MB/个）")
    print("  Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        httpd.server_close()


if __name__ == "__main__":
    main()
