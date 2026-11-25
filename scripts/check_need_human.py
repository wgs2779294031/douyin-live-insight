# -*- coding: utf-8 -*-
"""体检：浏览器是否被抖音风控拦在「验证码中间页 / 登录页」，需要人工介入。

为什么要有它
------------
监控默认无头跑（桌面零打扰），但一旦被风控拦成验证码页，人是看不见的 ——
只会表现为「探测一直失败」，而日志里只是普通 error，很容易被当成网络抖动。
这个脚本把「需要人」这件事变成桌面上的一个文件，老大一眼能看到。

做法（故意极简）
----------------
只问 CDP 的 HTTP 接口 `127.0.0.1:9222/json/list`，读每个标签的 title。
不发 WebSocket、不起 node 子进程、不碰抖音页面 —— 零依赖、零副作用。

用法
----
    python scripts/check_need_human.py            # 检查一次，必要时写桌面文件
    python scripts/check_need_human.py --quiet    # 安静模式（给计划任务用）

退出码：0 正常；2 需要人工；1 连不上浏览器
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

CDP = "http://127.0.0.1:9222/json/list"
DESKTOP = Path(os.path.expanduser("~")) / "Desktop"
FLAG = DESKTOP / "需要人工过验证码.txt"
LOG = Path(__file__).resolve().parents[1] / "data" / "check_human.log"

# 抖音风控的几种落地页标题（实测 / 常见）
BAD_WORDS = ("验证码中间页", "验证", "登录", "安全验证", "访问过于频繁")


def _log(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def list_targets(timeout: float = 5.0):
    req = urllib.request.Request(CDP, headers={"Host": "127.0.0.1:9222"})
    # 本机回环，别走代理（本机有 Clash 时 urllib 会走系统代理，连不上）
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    try:
        targets = list_targets()
    except Exception as e:
        # 浏览器没起来不算「需要人工」，交给 watchdog
        FLAG.unlink(missing_ok=True)
        if not args.quiet:
            print("[check] 连不上 9222：%s" % str(e)[:80])
        _log("连不上 9222：%s" % str(e)[:80])
        return 1

    pages = [t for t in targets if t.get("type") == "page"]
    bad = [t for t in pages if any(w in (t.get("title") or "") for w in BAD_WORDS)]

    if not bad:
        if FLAG.exists():
            FLAG.unlink()
            print("[check] 验证码已解除，桌面提示文件已删")
            _log("解除：桌面提示文件已删")
        elif not args.quiet:
            print("[check] 正常（%d 个标签，无验证码）" % len(pages))
        return 0

    lines = [
        "抖音把浏览器拦住了，需要人工过一下验证码。",
        "",
        "一条命令搞定（跑完去点弹出的浏览器窗口）：",
        "",
        "【PowerShell】（蓝色窗口）",
        r"  cd D:\毕设\douyin-collect",
        r"  .venv\Scripts\python.exe scripts\launch_all.py --fix-captcha --browser edge",
        "",
        "【cmd】（黑色窗口）",
        r"  cd /d D:\毕设\douyin-collect",
        r"  .venv\Scripts\python.exe scripts\launch_all.py --fix-captcha --browser edge",
        "",
        "→ 会先关掉旧实例和旧浏览器，再弹出一个**不会被最小化**的浏览器窗口，",
        "  在里面过验证码 / 扫码即可，处理完它自己继续跑，不需要再执行别的命令。",
        "",
        "⚠️ 只跑 --kill 是没用的：浏览器不关的话会被原样复用，还是那个被拦的实例。",
        "",
        "影响：处理前，开播探测和录流都会失败（内容补不回来）。",
        "",
        "被拦的标签页：",
    ]
    for t in bad:
        lines.append("  - %s  %s" % (t.get("title"), (t.get("url") or "")[:110]))
    lines += ["", "检查时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S")]

    DESKTOP.mkdir(parents=True, exist_ok=True)
    FLAG.write_text("\n".join(lines), encoding="utf-8")
    print("[check] 需要人工：%d 个标签被拦 -> %s" % (len(bad), FLAG))
    _log("需要人工：%d 个标签被拦（%s）"
         % (len(bad), "; ".join((t.get("title") or "") for t in bad)))
    return 2


if __name__ == "__main__":
    sys.exit(main())
