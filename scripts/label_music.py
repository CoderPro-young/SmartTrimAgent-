"""MUSIC/ 曲库离线打标（V7.5.3）：给选曲提供氛围/场景语义信号。

两条通道（--provider）：
- tags   抓取 Mixkit 分类页里每首曲子的**人工标注标签**（Uplifting/Travel/
         Acoustic Guitar…，JSON-LD 与条目卡片里都有），写回 meta.tags。
         零模型零密钥，数据来自曲库发布方，是当前最可信的语义来源。
- omni   音频理解大模型真听感打标（FireRed omni_bgm_label 同款思路），
         需 DASHSCOPE_API_KEY（qwen3-omni-flash）——能听出 tags 覆盖不了的
         细腻情绪，配置 key 后 --provider omni --force 即可升级。

注意：不做「本地 DSP 推断 mood」——实测 RMS 能量/响度密度/起始率三个
客观指标都无法区分感知能量（最安静的 ambient 起始率最高），硬推会产出
误导性标签；感知语义交给人工标注（tags）或音频大模型（omni）。

用法（项目根目录）：
    .venv/Scripts/python.exe scripts/label_music.py                 # 抓人工标签
    .venv/Scripts/python.exe scripts/label_music.py --provider omni --force
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
META_PATH = os.path.join(PROJECT_ROOT, "MUSIC", "meta.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

DEFAULT_OMNI_MODEL = os.environ.get("MUSIC_LABEL_MODEL", "qwen3-omni-flash")
EXCERPT_SECONDS = 90          # 听感判断不需要整首
EXCERPT_KBPS = 32

# 启发式通道的遗留字段（曾试过 DSP 推断 mood，已废弃，抓标签时清掉）
STALE_KEYS = ("mood", "scene", "energy", "bpm", "description", "label_source")

OMNI_PROMPT = """你是一个音乐分析专家。请听这段音乐（摘录），只输出一个 JSON 对象：
{
  "mood": [""],        // 情绪，从 ["欢快","轻松","治愈","平静","浪漫","伤感","激昂","紧张","史诗","酷","俏皮","怀旧"] 中选 1~3 个最贴切的
  "scene": [""],       // 适用场景，从 ["旅游","Vlog","美食","亲子","日常","健身","商务","科技感","夜生活","咖啡馆","自然风光"] 中选 1~3 个
  "energy": "",        // 能量：low / mid / high 三选一
  "description": ""    // 一句话中文描述整体听感与最适配的视频类型
}
不要输出 JSON 以外的任何内容。"""


# ----------------------------------------------------------- tags 通道 ---- #

def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "ignore")


def scrape_tags(page_url: str, track_id: str) -> list[str]:
    """在分类页上找到该曲目的条目卡片，收它的人工标签。

    卡片定位用播放器属性精确匹配——页头内嵌的全站数据块里也有 /music/<id>/
    字样，宽匹配会抓到全站标签表（160 个），那就不是这首曲子的标签了。
    标签 = 卡片内的 genre/mood/instrument/tag 锚文本（Mixkit 编辑标注）。
    """
    needle = (f'data-audio-player-preview-url-value='
              f'"https://assets.mixkit.co/music/{track_id}/')
    html = fetch(page_url)
    for card in html.split('<div class="item-grid__item">'):
        if needle not in card:
            continue
        tags = re.findall(
            r'href="/free-stock-music/(?:genre|mood|instrument|tag)/[^"]*"'
            r'[^>]*>\s*([^<]+?)\s*<', card)
        return tags[:10]
    raise LookupError(f"分类页 {page_url} 里没找到曲目 id={track_id} 的条目卡片")


def track_id_of(entry: dict) -> str | None:
    src = entry.get("source_url") or ""
    m = re.search(r"/music/(\d+)/", src)
    return m.group(1) if m else None


# ----------------------------------------------------------- omni 通道 ---- #

def _resolve_omni() -> tuple[str, str, str] | None:
    """omni 通道端点解析（FireRed 同款 DashScope 兼容模式）→ (key, base, model)。"""
    def env(k):
        return os.environ.get(k, "").strip()

    if not env("DASHSCOPE_API_KEY"):
        try:
            from dotenv import load_dotenv
            load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
        except ImportError:
            pass
    if not env("DASHSCOPE_API_KEY"):
        return None
    base = env("DASHSCOPE_BASE_URL") or \
        "https://dashscope.aliyuncs.com/compatible-mode/v1"
    return env("DASHSCOPE_API_KEY"), base, DEFAULT_OMNI_MODEL


def make_excerpt(path: str, tmpdir: str) -> str | None:
    """ffmpeg 截取 90s 单声道低码率摘录 → base64；无 ffmpeg/失败返回 None。"""
    small = os.path.join(tmpdir, "excerpt.mp3")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "quiet", "-t", str(EXCERPT_SECONDS),
             "-i", path, "-ac", "1", "-ar", "16000", "-b:a", f"{EXCERPT_KBPS}k",
             small],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with open(small, "rb") as f:
            return base64.b64encode(f.read()).decode()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


def omni_label(api_key: str, base_url: str, model: str, audio_b64: str) -> dict:
    payload = json.dumps({
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "input_audio",
                 "input_audio": {"data": audio_b64, "format": "mp3"}},
                {"type": "text", "text": OMNI_PROMPT},
            ],
        }],
        "modalities": ["text"],
        "temperature": 0.1,
        "max_tokens": 512,
    }).encode()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions", data=payload,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.load(resp)
    text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    s, e = text.find("{"), text.rfind("}")
    if s < 0 or e <= s:
        raise ValueError(f"回复不含 JSON：{text[:120]!r}")
    labels = json.loads(text[s:e + 1])
    for k in ("mood", "scene"):
        if not isinstance(labels.get(k), list) or not labels[k]:
            raise ValueError(f"标签字段 {k} 缺失或不是非空数组：{labels}")
    labels["label_source"] = f"omni:{model}"
    return labels


# --------------------------------------------------------------- 主流程 ---- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="已有标签也重新标注")
    ap.add_argument("--provider", choices=("tags", "omni"), default="tags")
    ap.add_argument("--sleep", type=float, default=0.8, help="逐首间隔秒数")
    args = ap.parse_args()

    omni = None
    if args.provider == "omni":
        omni = _resolve_omni()
        if omni is None:
            raise SystemExit("--provider omni 需要 DASHSCOPE_API_KEY（.env 或环境变量）")

    with open(META_PATH, encoding="utf-8") as f:
        meta = json.load(f)

    todo = [e for e in meta if args.force or not e.get("tags")]
    print(f"曲库 {len(meta)} 首，通道 {args.provider}"
          + (f"（{omni[2]}）" if omni else "") + f"，待标注 {len(todo)}")
    failed = 0
    for i, entry in enumerate(todo, 1):
        rel = entry["file"]
        print(f"[{i}/{len(todo)}] {rel} ...", flush=True)
        try:
            if args.provider == "tags":
                for k in STALE_KEYS:            # 清掉废弃通道的遗留标签
                    entry.pop(k, None)
                tid = track_id_of(entry)
                if not tid:
                    raise LookupError("meta 里没有 source_url，无法定位条目")
                entry["tags"] = scrape_tags(entry["category_page"], tid)
            else:
                full = os.path.join(PROJECT_ROOT, rel)
                with tempfile.TemporaryDirectory() as tmpdir:
                    audio_b64 = make_excerpt(full, tmpdir)
                if audio_b64 is None:
                    raise RuntimeError("ffmpeg 不可用，无法截取摘录")
                entry.update(omni_label(omni[0], omni[1], omni[2], audio_b64))
                time.sleep(args.sleep)
            with open(META_PATH, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=1)
            print(f"    tags={entry.get('tags') or entry.get('mood')}")
        except Exception as exc:  # noqa: BLE001 —— 单首失败不中断整批
            failed += 1
            print(f"    FAIL: {type(exc).__name__}: {exc}")

    print(f"完成：{len(todo) - failed}/{len(todo)} 标注成功，已写回 {META_PATH}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
