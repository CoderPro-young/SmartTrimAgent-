"""把指定用例组的素材装填进 INPUT/。

用法：
    python tests/load_dataset.py 用例组1            # 增量装填（INPUT/ 里已有的其他文件保留，会列出来提醒）
    python tests/load_dataset.py 用例组1 --clean    # 先清空 INPUT/ 再装填（复刻该组原始运行环境，推荐）
    python tests/load_dataset.py 用例组1 --dry-run  # 只检查素材仓是否齐全，不动 INPUT/

素材仓：tests/dataset_materials/<用例组>/，用例清单见 tests/test_dataset.md。
"""

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VAULT = Path(__file__).resolve().parent / "dataset_materials"
INPUT = ROOT / "INPUT"

# 各组素材清单（与 tests/test_dataset.md 保持一致）
CASES = {
    "用例组1": [
        "微信视频2026-10-05_175011_493.mp4",
        "微信视频2026-10-31_090008_305.mp4",
        "微信视频2026-10-31_090121_768.mp4",
        "微信图片_20260926094000_610_33.jpg",
        "微信图片_20260927165328_928_33.jpg",
        "微信图片_20260927165340_929_33.jpg",
    ],
    "用例组2": [
        "微信视频2026-09-26_100901_938.mp4",
        "微信视频2026-09-26_100944_860.mp4",
        "微信视频2026-09-26_100919_300.mp4",
        "微信视频2026-09-26_101240_737.mp4",
        "微信图片_20260928224256_1561_33.jpg",
        "微信视频2026-10-30_092543_209.mp4",
        "微信视频2026-10-31_090121_768.mp4",
        "微信图片_20260928224504_1572_33.jpg",
    ],
    "用例组3": [
        "微信视频2026-10-30_092543_209.mp4",
        "63a1568957d8261f3dc30de18a2a157c-00.00.00.000-00.01.27.200.mp4",
        "微信视频2026-09-26_101230_833.mp4",
        "微信图片_20260925225926_550_33.jpg",
        "微信图片_20260926094000_610_33.jpg",
    ],
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("case", choices=sorted(CASES), help="要装填的用例组")
    ap.add_argument("--clean", action="store_true",
                    help="装填前清空 INPUT/（.gitkeep 除外）。素材编号按文件名序生成，"
                         "INPUT/ 里残留无关文件会让编号和原始轮次对不上，忠实回放建议加 --clean")
    ap.add_argument("--dry-run", action="store_true", help="只检查，不落盘")
    args = ap.parse_args()

    files = CASES[args.case]
    src_dir = VAULT / args.case
    missing = [f for f in files if not (src_dir / f).is_file()]
    if missing:
        sys.exit(f"素材仓 {src_dir} 缺文件，先补齐再装填：\n  " + "\n  ".join(missing))
    print(f"素材仓齐全：{src_dir}（{len(files)} 个文件）")

    INPUT.mkdir(exist_ok=True)
    if args.clean and not args.dry_run:
        removed = 0
        for p in INPUT.iterdir():
            if p.name == ".gitkeep":
                continue
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            removed += 1
        print(f"已清空 INPUT/（移除 {removed} 项）")

    for f in files:
        if args.dry_run:
            print(f"[dry-run] {f} -> INPUT/")
        else:
            shutil.copy2(src_dir / f, INPUT / f)
            print(f"装填 {f}")

    if not args.dry_run:
        extra = sorted(p.name for p in INPUT.iterdir()
                       if p.name != ".gitkeep" and p.name not in files)
        if extra:
            print("\n⚠ INPUT/ 还有本组之外的文件（会影响素材编号与回放保真度，建议 --clean 重装）：")
            for name in extra:
                print(f"  {name}")


if __name__ == "__main__":
    main()
