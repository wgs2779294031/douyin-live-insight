#!/usr/bin/env python
"""福袋状态扫描器：批量查在播房间的福袋（luckybox）状态。

为什么要它（2026-09-30 复查结论）
--------------------------------
公开 webcast proto 里没有福袋/投票消息类，但**结构化观察不等于 WSS 事件**：
房间页自己会请求 ``GET /webcast/luckybox/box/list/``（浏览器 XHR 实测抓到），
一个 ``data`` 数组 = 当前在发的福袋，``extra.current_round/pct/round_target``
= 抢福袋进度。**无需 cookie、无需 a_bogus 也能拿到 200**（实测）。

⚠️ 参数必须用**内部 room_id（19 位）**——传 web_rid 会得到一个「无房间上下文」
的空结构（``data: []`` + ``extra`` 全 0），**看起来像"没福袋"，其实是参数错了**。
这是本接口最容易踩的坑，扫描器因此强制要求 room_id 映射。

用法
----
    # 1) 先经 B2 通道抓一次 feed（cdp.mjs net "webcast" captures/x.json）
    # 2) 扫描（自动从抓包里解析 web_rid -> id_str）
    python scripts/scan_luckybox.py --feed-capture ../captures/x.json --rounds 5 --interval 20

输出：命中房间打印细则；全程写 data/luckybox_scan_<date>.jsonl（含未命中记录）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config  # noqa: E402
from browser_tools import resolve_capture  # noqa: E402

LUCKYBOX_URL = "https://live.douyin.com/webcast/luckybox/box/list/"
_BASE_PARAMS = {
    "aid": "6383", "app_name": "douyin_web", "live_id": "1", "device_platform": "web",
    "language": "zh-CN", "cookie_enabled": "true", "screen_width": "1536",
    "screen_height": "864", "browser_language": "zh-CN", "browser_platform": "Win32",
    "browser_name": "Chrome", "browser_version": "153.0.0.0",
    "os_name": "Windows", "os_version": "10", "platform": "PC",
}


def _resolve_capture_path(path: str) -> Path:
    """完整路径/cwd 或显式抓包目录；不依赖另一台机器的 WorkBuddy 安装。"""
    return resolve_capture(path)


def parse_feed_capture(path: str) -> List[Dict[str, Any]]:
    """从 CDP net 抓包里解析 feed。

    返回每房：``web_rid`` / ``id_str``（19 位内部 room_id）/ ``nick`` / ``title``
    / ``user_count``，外加 PK 线索 ``with_linkmic`` / ``linkmic_display_type``
    / ``linker_play_modes`` / ``linker_map``（⚠️ 作「配置」读，实测各房常为 0/False）。
    """
    caps = json.loads(_resolve_capture_path(path).read_text(encoding="utf-8"))
    rooms: List[Dict[str, Any]] = []
    for c in caps:
        if "/webcast/feed/?" not in c.get("url", ""):
            continue
        if len(c.get("body") or "") < 100000:
            continue
        data = json.loads(c["body"])
        items = data.get("data") if isinstance(data.get("data"), list) else \
            (data.get("data") or {}).get("data")
        for it in items or []:
            room = it.get("data") or it
            ow = room.get("owner") or {}
            if not ow.get("web_rid") or not room.get("id_str"):
                continue
            linker = room.get("linker_detail") or {}
            rooms.append({
                "web_rid": str(ow["web_rid"]),
                "id_str": str(room["id_str"]),
                "nick": ow.get("nickname") or "",
                "title": room.get("title") or "",
                "user_count": room.get("user_count") or 0,
                # PK/连麦状态（feed 自带；⚠️ 作「配置」读，实测各房常为 0/False）
                "with_linkmic": bool(room.get("with_linkmic")),
                "linkmic_display_type": room.get("linkmic_display_type") or 0,
                "linker_play_modes": linker.get("linker_play_modes") or [],
                "linker_map": room.get("linker_map") or {},
            })
    return rooms


def query_luckybox(room_id: str, user_agent: str, timeout: int = 8) -> Dict[str, Any]:
    p = dict(_BASE_PARAMS)
    p["room_id"] = str(room_id)
    r = requests.get(LUCKYBOX_URL, params=p, headers={"User-Agent": user_agent},
                     timeout=timeout)
    r.raise_for_status()
    return r.json()


def main() -> int:
    ap = argparse.ArgumentParser(description="福袋状态扫描器（luckybox/box/list）")
    ap.add_argument("--feed-capture", required=True,
                    help="CDP net 抓包 JSON（含 /webcast/feed/ 响应）")
    ap.add_argument("--config", default="data/livetest.config.json")
    ap.add_argument("--rounds", type=int, default=1, help="轮询轮数")
    ap.add_argument("--interval", type=float, default=20.0, help="轮间秒数")
    ap.add_argument("--out", default=None, help="输出 jsonl（默认 data/luckybox_scan_<date>.jsonl）")
    args = ap.parse_args()

    cfg = load_config(args.config)
    rooms = parse_feed_capture(args.feed_capture)
    if not rooms:
        print("抓包里没有解析出房间（确认是 cdp.mjs net 抓的 feed 响应）")
        return 1
    print(f"待扫房间 {len(rooms)} 个，轮数 {args.rounds}，间隔 {args.interval}s")

    out_path = args.out or f"data/luckybox_scan_{time.strftime('%Y-%m-%d')}.jsonl"
    hits_total: List[Tuple[str, str]] = []
    with open(out_path, "a", encoding="utf-8") as f:
        for rnd in range(args.rounds):
            for rm in rooms:
                try:
                    d = query_luckybox(rm["id_str"], cfg.user_agent)
                except Exception as e:
                    print(f"  [轮{rnd+1}] ERR {rm['web_rid']} {e}")
                    continue
                boxes = d.get("data") or []
                extra = d.get("extra") or {}
                rec = {
                    "ts": int(time.time()), "round": rnd + 1,
                    "web_rid": rm["web_rid"], "room_id": rm["id_str"],
                    "nick": rm["nick"], "title": rm["title"],
                    "user_count": rm["user_count"],
                    "box_count": len(boxes),
                    "current_round": extra.get("current_round"),
                    "pct": extra.get("pct"),
                    "rush_condition": extra.get("rush_condition"),
                    "boxes": boxes,
                }
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                if boxes:
                    hits_total.append((rm["web_rid"], rm["nick"]))
                    print(f"  ★福袋 [轮{rnd+1}] {rm['web_rid']} {rm['nick'][:14]} "
                          f"boxes={len(boxes)} pct={extra.get('pct')}")
                    print(f"     原始: {json.dumps(boxes, ensure_ascii=False)[:600]}")
                time.sleep(0.4)
            if rnd + 1 < args.rounds:
                time.sleep(args.interval)

    print(f"\n已写 {out_path}")
    if hits_total:
        print("命中福袋房：")
        for web_rid, nick in dict.fromkeys(hits_total):
            print(f"  · {web_rid} {nick}")
    else:
        print("本轮未捕获到在发福袋的房间（福袋是脉冲式的，需多轮/挑对时段）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
