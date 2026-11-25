#!/usr/bin/env python
"""在播房间轮询筛选器：从 webcast/feed 里筛出「福袋进行中 / 连麦中」的房间。

为什么要它（2026-09-30 复查结论）
--------------------------------
1. ``GET live.douyin.com/webcast/feed/`` **带 cookie 可免签名直接请求**（实测 200 /
   290KB），不必再挂浏览器 —— 盯房可自动化；
   ⚠️ 但**直连只回 ~6 房且没有 owner.web_rid**（浏览器侧才有 20 房 + web_rid），
   所以它的定位是「**看状态**」而不是「**找房**」——要找房走 B2 抓包；
   ⚠️ 更要紧的是：真机实测**各房 ``lottery_finish_time`` 恒为 0**（福袋是脉冲式，
   且推荐池非全站）→ 想抓「正在发福袋的房间」应改用 ``scripts/scan_luckybox.py``
   （luckybox 接口是权威状态源），本脚本只作辅助；
2. feed 的**房间对象本身就带状态字段**，等于免费的房间级探测器：
   - ``lottery_finish_time``（unix 秒）：> now 即**福袋进行中**，附 ``luckymoney_num``
   - ``with_linkmic`` / ``linkmic_display_type`` / ``linker_detail`` / ``linker_map``
     / ``linkmic_layout`` / ``live_type_linkmic``：**连麦 / PK 状态**
   - ``user_count``：在线人数（选房依据）
3. 「PK 是最典型的高能时刻触发器」—— 用它在开播池里蹲到 PK 房，再交给 WSS 探针抓
   method（这才是「PK 到底推什么」的确认路径，理论枚举不作数）。

用法
----
    # 单轮快照
    python scripts/scan_feed.py --rounds 1
    # 蹲 10 轮（每 30 秒一轮），只打命中行
    python scripts/scan_feed.py --rounds 10 --interval 30 --only-hits

输出：``data/feed_scan_<date>.jsonl``（每轮每房一行），命中行同时打到控制台。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config  # noqa: E402

FEED_URL = "https://live.douyin.com/webcast/feed/"
_BASE = {
    "aid": "6383", "app_name": "douyin_web", "live_id": "1", "device_platform": "web",
    "language": "zh-CN", "cookie_enabled": "true", "screen_width": "1536",
    "screen_height": "864", "browser_language": "zh-CN", "browser_platform": "Win32",
    "browser_name": "Chrome", "browser_version": "153.0.0.0", "os_name": "Windows",
    "os_version": "10", "platform": "PC", "channel_id": "0", "count": "20",
    "web_rid": "", "is_draw": "0", "version_code": "170400", "pc_client_type": "1",
}


def fetch_feed(user_agent: str, cookie: str, timeout: int = 12) -> List[Dict[str, Any]]:
    """请求 feed，返回房间字典列表（字段已摊平）。

    ⚠️ 实测（2026-09-30）：**直连** feed 只回 ~6 个房间，且 ``owner.web_rid`` 为空
    （浏览器侧抓包才有 web_rid 和 20 房）。所以这里**不要求 web_rid**，
    以 ``room_id``（id_str，19 位内部房间号）为主键；要给探针用 web_rid 的话，
    得走 B2 抓包那条路（``.browser_bridge/cdp.mjs net``）。
    """
    r = requests.get(FEED_URL, params=_BASE,
                     headers={"User-Agent": user_agent, "cookie": cookie},
                     timeout=timeout)
    r.raise_for_status()
    payload = r.json()
    data = payload.get("data")
    items = data if isinstance(data, list) else (data or {}).get("data") or []
    rooms: List[Dict[str, Any]] = []
    for it in items:
        room = it.get("data") or it
        ow = room.get("owner") or {}
        room_id = str(room.get("id_str") or "")
        if not room_id:
            continue
        rooms.append({
            "web_rid": str(ow.get("web_rid") or ""),   # 直连时为空，别当主键
            "room_id": room_id,
            "nick": ow.get("nickname") or "",
            "title": room.get("title") or "",
            "user_count": room.get("user_count") or 0,
            "lottery_finish_time": room.get("lottery_finish_time") or 0,
            "luckymoney_num": room.get("luckymoney_num") or 0,
            "with_linkmic": bool(room.get("with_linkmic")),
            "linkmic_display_type": room.get("linkmic_display_type") or 0,
            "linkmic_layout": room.get("linkmic_layout") or 0,
            "linker_detail": room.get("linker_detail") or None,
            "room_tag": room.get("room_tag") or None,
            "content_tag": room.get("content_tag") or "",
        })
    return rooms


def judge(room: Dict[str, Any], now: int) -> str:
    marks = []
    lft = room["lottery_finish_time"]
    if lft and lft > now:
        marks.append(f"★福袋进行中(剩{(lft - now) // 60}分,红包{room['luckymoney_num']})")
    if room["with_linkmic"]:
        marks.append("★连麦/PK中")
    return " ".join(marks)


def main() -> int:
    ap = argparse.ArgumentParser(description="feed 轮询筛选（福袋 / 连麦房间）")
    ap.add_argument("--config", default="data/livetest.config.json")
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--only-hits", action="store_true", help="控制台只打命中行")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    out_path = args.out or f"data/feed_scan_{time.strftime('%Y-%m-%d')}.jsonl"
    hits: List[Dict[str, Any]] = []

    with open(out_path, "a", encoding="utf-8") as f:
        for rnd in range(args.rounds):
            try:
                rooms = fetch_feed(cfg.user_agent, cfg.cookie or "")
            except Exception as e:
                print(f"[轮{rnd+1}] feed 请求失败: {e}")
                time.sleep(args.interval)
                continue
            now = int(time.time())
            print(f"\n===== 轮 {rnd+1}：{time.strftime('%H:%M:%S')} 在播 {len(rooms)} 房 =====")
            for rm in sorted(rooms, key=lambda r: -r["user_count"]):
                mark = judge(rm, now)
                rec = dict(rm, ts=now, round=rnd + 1, hit=bool(mark))
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                if mark:
                    hits.append(rec)
                if mark or not args.only_hits:
                    print(f"  {rm['user_count']:>6} {rm['web_rid']:>13} "
                          f"{rm['nick'][:14]:<16} {mark} | {rm['title'][:26]}")
            f.flush()
            if rnd + 1 < args.rounds:
                time.sleep(args.interval)

    print(f"\n已写 {out_path}")
    if hits:
        print(f"命中 {len(hits)} 条：")
        for rm in hits:
            print(f"  · {rm['web_rid']} {rm['nick'][:16]} "
                  f"lft={rm['lottery_finish_time']} linkmic={rm['with_linkmic']}")
    else:
        print("本轮未命中（福袋/PK 都是脉冲式的，需多轮或更长窗口）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
