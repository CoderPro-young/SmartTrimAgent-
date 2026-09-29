"""web/server._retire 离线测试：删除失败的降级链与异常透传。

背景（线上实测）：`except Exception as first_exc:` 的变量在块结束即被 Python
删除，降级路径再引用它直接 UnboundLocalError，把真正的删除失败原因（Windows
文件被占用）掩盖掉。这里离线锁死三条路径的行为。
"""
import importlib.util
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("st_server", ROOT / "web" / "server.py")
server = importlib.util.module_from_spec(spec)
sys.modules["st_server"] = server
spec.loader.exec_module(server)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


class _Ctx:
    """临时替换 os.remove / os.replace / time.sleep，退出时还原。"""

    def __init__(self, remove_exc=None, replace_exc=None):
        self.remove_exc = remove_exc
        self.replace_exc = replace_exc
        self._orig = (os.remove, os.replace, time.sleep)

    def __enter__(self):
        real_sleep = time.sleep

        def _remove(p):
            if self.remove_exc:
                raise self.remove_exc
            return self._orig[0](p)

        def _replace(a, b):
            if self.replace_exc:
                raise self.replace_exc
            return self._orig[1](a, b)

        os.remove, os.replace = _remove, _replace
        time.sleep = lambda *_: None          # 测试不等重试间隔
        return self

    def __exit__(self, *a):
        os.remove, os.replace, time.sleep = self._orig


def _make_tmp(name):
    INPUT_DIR = ROOT / "INPUT"
    INPUT_DIR.mkdir(exist_ok=True)
    full = INPUT_DIR / name
    full.write_bytes(b"x" * 8)
    return str(full), name


def main():
    print("== _retire 降级链 ==")

    # t1 正常路径：真删成功
    full, name = _make_tmp("retire_ok.bin")
    try:
        mode, detail = server._retire(full, name)
        check("t1 真删成功返回 deleted", mode == "deleted" and not os.path.exists(full), f"{mode}/{detail}")
    finally:
        if os.path.exists(full):
            os.remove(full)

    # t2 真删被拦（沙箱钩子抛非 OSError）、降级移动成功 → moved 且落在 TMP/removed/
    full, name = _make_tmp("retire_moved.bin")
    try:
        with _Ctx(remove_exc=RuntimeError("safe-delete hook refused")):
            mode, detail = server._retire(full, name)
        target = ROOT / "TMP" / "removed" / name
        check("t2 降级移动返回 moved", mode == "moved", f"{mode}/{detail}")
        check("t2 文件落在 TMP/removed/", target.exists() and not os.path.exists(full), detail)
        os.remove(str(target))
    finally:
        if os.path.exists(full):
            os.remove(full)

    # t3 文件被占用（Windows PermissionError）：真删重试后降级，移动成功
    full, name = _make_tmp("retire_locked.bin")
    try:
        with _Ctx(remove_exc=PermissionError(32, "being used by another process")):
            mode, detail = server._retire(full, name)
        check("t3 占用时降级移动成功", mode == "moved", f"{mode}/{detail}")
        os.remove(str(ROOT / "TMP" / "removed" / name))
    finally:
        if os.path.exists(full):
            os.remove(full)

    # t4 两条路都失败：抛 RuntimeError，消息含两次异常原文，且不再出现
    #    UnboundLocalError('first_exc')——这是本次线上事故的根因
    full, name = _make_tmp("retire_doomed.bin")
    try:
        with _Ctx(remove_exc=PermissionError(32, "锁定A"),
                  replace_exc=PermissionError(5, "锁定B")):
            try:
                server._retire(full, name)
                raised = None
            except RuntimeError as exc:
                raised = exc
        check("t4 彻底失败抛 RuntimeError", raised is not None,
              f"raised={type(raised).__name__}" if raised else "未抛")
        if raised:
            msg = str(raised)
            check("t4 消息含真删失败原文", "锁定A" in msg, msg)
            check("t4 消息含降级失败原文", "锁定B" in msg, msg)
            check("t4 不再是 UnboundLocalError", "first_exc" not in msg and "UnboundLocal" not in msg, msg)
    finally:
        if os.path.exists(full):
            os.remove(full)

    print(f"\n_retire 离线测试：{PASS}/{PASS + FAIL} 项通过")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
