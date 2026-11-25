# -*- coding: utf-8 -*-
"""黑窗探针：抓「到底是哪个 node 进程弹了控制台窗口」。

背景
----
老大反复看到 `...\\node.exe` 的黑框闪一下又关。已修 stream_recorder 的 capture_stream
（漏了 CREATE_NO_WINDOW），但还偶发。当前所有 node 进程 MainWindowHandle 都是 0，
说明窗口是瞬时的 —— 必须蹲守抓现场。

做法
----
每 2 秒枚举一次 node.exe：谁有可见窗口，就把 PID / 父进程 / 完整命令行 / 窗口标题
写进日志。抓到的行直接指出是哪个脚本起的。

    python scripts/find_black_window.py --minutes 30
"""
from __future__ import annotations

import argparse
import ctypes
import csv
import io
import subprocess
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "_cap" / "blackwin.log"
u32 = ctypes.windll.user32


def windows_of(pid: int):
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(h, _):
        p = ctypes.c_ulong()
        u32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid and u32.IsWindowVisible(h):
            buf = ctypes.create_unicode_buffer(260)
            u32.GetWindowTextW(h, buf, 260)
            out.append(buf.value)
        return True

    u32.EnumWindows(cb, 0)
    return out


def all_visible_windows():
    """枚举**所有**可见顶层窗口：返回 (hwnd, pid, title)。

    为什么不只查 node 进程：控制台窗口的归属有时挂在 conhost 上，
    按 node 的 PID 查会漏（2026-10-20 实测：25 分钟一次都没抓到，但人确实看见了）。
    -> 改成先扫全部窗口，再按标题里是否含 node.exe / cmd.exe 来筛。
    """
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(h, _):
        if not u32.IsWindowVisible(h):
            return True
        pid = ctypes.c_ulong()
        u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        buf = ctypes.create_unicode_buffer(300)
        u32.GetWindowTextW(h, buf, 300)
        out.append((int(h), pid.value, buf.value))
        return True

    u32.EnumWindows(cb, 0)
    return out


def node_pids():
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq node.exe", "/FO", "CSV", "/NH"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    pids = []
    for row in csv.reader(io.StringIO(r.stdout or "")):
        if len(row) >= 2 and row[0].lower().startswith("node"):
            try:
                pids.append(int(row[1]))
            except Exception:
                pass
    return pids


def detail(pid: int) -> str:
    r = subprocess.run(["wmic", "process", "where", "processid=%d" % pid,
                        "get", "ParentProcessId,CommandLine", "/format:list"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    parts = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    return " | ".join(parts)[:400]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--interval", type=float, default=2.0)
    args = ap.parse_args()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + args.minutes * 60
    seen = set()
    hits = 0
    with open(OUT, "a", encoding="utf-8") as f:
        f.write("\n===== probe start %s (%.0f min) =====\n"
                % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), args.minutes))
        while time.time() < deadline:
            suspects = []
            for _hwnd, pid, title in all_visible_windows():
                low = (title or "").lower()
                if ("node.exe" in low or low.startswith("c:")
                        or "cmd.exe" in low or "命令提示符" in title):
                    suspects.append((pid, title))
            for pid, title in suspects:
                key = (pid, title)
                line = "[%s] 窗口 pid=%d 标题=%r  %s" % (
                    datetime.now().strftime("%H:%M:%S"), pid, title, detail(pid))
                f.write(line + "\n")
                f.flush()
                if key not in seen:
                    seen.add(key)
                    hits += 1
                    print(line[:260], flush=True)
            time.sleep(args.interval)
        f.write("===== probe end %s，命中 %d =====\n"
                % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), hits))
    print("完成，命中 %d 次，日志 %s" % (hits, OUT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
