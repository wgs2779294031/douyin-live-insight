#!/usr/bin/env python
"""查看 / 清理监控浏览器的标签页（2026-10-27）。

为什么需要这个：
    浏览器是八路共用的一个 Chrome。标签页只增不减的话，每个页面都是一个独立
    renderer 进程，堆到几十个以后浏览器会被系统拖到「端口探不通」，watchdog
    判定卡死 → 杀掉重起。重起本身没错，错在旧页面会被会话快照恢复回来。
    代码里已经堵住了泄漏（capture_stream 抓完就关 + 重启前清快照），
    这个脚本是**手动兜底**：不重启浏览器的前提下，把堆着的页面收掉。

用法：
    python scripts/tabs.py                 # 只列，不动
    python scripts/tabs.py --sweep         # 关掉 URL 完全相同的重复页
    python scripts/tabs.py --close-live    # 关掉所有 live.douyin.com 页
                                            #（只在确认没有主播在录时用）
    python scripts/tabs.py --sessions      # 看看会话快照有多大（不删）
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _profile() -> Path:
    """和 run_watchdog.resolve_profile 保持同一口径（换浏览器就换 profile）。"""
    import os
    p = os.environ.get("WATCH_BROWSER_PROFILE")
    if p:
        return Path(p)
    if os.name == "nt":
        if (os.environ.get("WATCH_BROWSER") or "chrome").lower() == "edge":
            return Path("D:/wb-edge-profile")
        return Path("D:/wb-browser-profile")
    return ROOT / "browser-profile"


PROFILE = _profile()


def _port() -> int:
    import os
    return int(os.environ.get("WATCH_DEBUG_PORT") or 9222)


def _pages() -> list:
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/json/list" % _port(), timeout=5) as r:
            ts = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        print("调试端口不可达（浏览器没起？）：%s" % e)
        return []
    return [t for t in ts if t.get("type") == "page"]


def _close(tid: str) -> bool:
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:%d/json/close/%s" % (_port(), tid), method="PUT")
        urllib.request.urlopen(req, timeout=5).read()
        return True
    except Exception:
        return False


def main() -> int:
    args = sys.argv[1:]
    pages = _pages()
    if not pages:
        print("没有可用页面")
        return 1

    print("当前 %d 个页面：" % len(pages))
    for i, t in enumerate(pages):
        kind = "?"
        u = t.get("url") or ""
        if "live.douyin.com" in u:
            kind = "直播"
        elif "douyin.com" in u:
            kind = "抖音"
        elif u.startswith("chrome://"):
            kind = "内置"
        print("  [%2d] %-4s %s" % (i, kind, u[:96]))

    if "--sessions" in args:
        sd = PROFILE / "Default" / "Sessions"
        if sd.is_dir():
            fs = sorted(sd.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
            print("\n会话快照 %d 个（Chrome 重启时会照着它恢复页面）：" % len(fs))
            for p in fs[:8]:
                print("  %-30s %8.1f KB" % (p.name, p.stat().st_size / 1024))
        else:
            print("\n会话目录不存在：%s" % sd)

    if "--sweep" in args:
        seen, killed = set(), 0
        for t in pages:
            u = t.get("url") or ""
            if not u or u.startswith("chrome://"):
                continue
            if u in seen and _close(t.get("id")):
                killed += 1
            seen.add(u)
        print("\n关掉重复页：%d 个" % killed)

    if "--close-live" in args:
        killed = 0
        for t in pages:
            if "live.douyin.com" in (t.get("url") or "") and _close(t.get("id")):
                killed += 1
        print("\n关掉直播页：%d 个" % killed)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
