#!/usr/bin/env python
"""福袋「真机触发实验」：找到**正在发福袋**的房间 → 立刻抓该房间浏览器侧 WSS 帧。

为什么要这么绕
--------------
公开 webcast proto 里**没有**福袋消息类（我们这份 68 类是子集），所以判断
「福袋事件平台到底推不推」只能靠真机取证。但福袋是**脉冲式**的：随便挑个房间蹲
十分钟大概率空手。于是分两步：

1. **定位**：``GET /webcast/luckybox/box/list/``（免签名，但必须用 **19 位内部
   room_id**）轮询在播房间，谁 ``data`` 非空谁就在发福袋 —— 这是「触发时刻」的
   确定性信号；
2. **取证**：把浏览器导航到那个房间，用 CDP 抓它自己的 WSS 二进制帧
   （``cdp.mjs wsframes``），再用 ``scripts/decode_frames.py`` 解码，看服务端
   实际推了哪些 method（重点找 ``WebcastLotteryEventNewMessage``）。

⚠️ 全程只读：内部房间号来自 CDP 抓的 feed 响应；浏览器侧只做「导航 + 监听网络」，
   不点击、不参与、不使用任何账号凭据。

用法
----
    python scripts/hunt_lottery_ws.py --feed-capture feed-hunt-1.json \
        --rounds 12 --interval 15 --capture-ms 150000

产出
----
    data/hunt_lottery_<date>.json  —— 每轮扫描结果 + 命中后抓帧/解码结论
    命中即抓帧；抓到含 ``WebcastLotteryEventNewMessage`` 的帧会写
    data/lottery_frame_<ts>.jsonl（原始帧）与 data/lottery_frame_<ts>.decoded.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess# 子进程统一不开控制台窗口：父进程若是无控制台形态（pythonw/NO_WINDOW），
# 控制台子进程会被 Windows 新开一个黑框闪出来（2026-10-19/20 反复踩）。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # 便于 import scan_luckybox

from config import load_config  # noqa: E402
from scan_luckybox import parse_feed_capture, query_luckybox  # noqa: E402
from browser_tools import captures_dir, cdp_script, node_binary  # noqa: E402

_REPO = Path(__file__).resolve().parents[1]


def _node_bin() -> str:
    return node_binary()


def capture_frames(web_rid: str, wait_ms: int, tag: str) -> Dict[str, Any]:
    """导航到房间 + 抓 WSS 帧 + 解码。返回解码统计。"""
    caps_dir = captures_dir()
    caps_dir.mkdir(parents=True, exist_ok=True)
    raw = caps_dir / f"ws-lottery-{tag}.jsonl"
    env = dict(os.environ, CDP_PORT=os.environ.get("CDP_PORT", "9222"),
               CDP_TARGET_URL="live.douyin.com")
    url = f"https://live.douyin.com/{web_rid}"
    print(f"[抓帧] 导航 {url}，抓 {wait_ms} ms 的 WSS 帧 ...", flush=True)
    r = subprocess.run([_node_bin(), str(cdp_script()), "wsframes", str(raw),
                        str(wait_ms), "im/push", url],
                       cwd=str(_REPO), env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace",
                       timeout=wait_ms / 1000 + 120, creationflags=_NO_WINDOW)
    if r.returncode != 0:
        return {"ok": False, "err": (r.stderr or r.stdout)[-500:]}
    print((r.stdout or "").strip()[-300:], flush=True)

    dec_out = _REPO / "data" / f"lottery_frame_{tag}.decoded.json"
    d = subprocess.run([sys.executable, str(_REPO / "scripts" / "decode_frames.py"),
                        str(raw), "--out", str(dec_out), "--max-raw", "8"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       cwd=str(_REPO), env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                       creationflags=_NO_WINDOW)
    print((d.stdout or "").strip()[-1500:], flush=True)
    stats: Dict[str, Any] = {"ok": True, "raw_file": str(raw), "decoded_file": str(dec_out)}
    try:
        stats["methods"] = json.loads(dec_out.read_text(encoding="utf-8"))["methods"]
    except Exception as e:
        stats["decode_err"] = str(e)
    stats["got_lottery"] = "WebcastLotteryEventNewMessage" in (stats.get("methods") or {})
    return stats


def refresh_rooms_from_feed(tag: str) -> List[Dict[str, Any]]:
    """经 CDP 重抓一次 feed，返回新的房间清单。

    ⚠️ 为什么必须refresh：**feed 是推荐池，每次请求都换人**（实测两份抓包 20 房零重合）。
    固定一份清单蹲福袋，命中率取决于那批房间的类型 —— 游戏/带货房常发，
    生活/助眠/聊天房基本不发。所以长蹲要**定期换房源**。
    """
    caps_dir = captures_dir()
    caps_dir.mkdir(parents=True, exist_ok=True)
    out = caps_dir / f"feed-hunt-{tag}.json"
    env = dict(os.environ, CDP_PORT=os.environ.get("CDP_PORT", "9222"))
    env["CDP_TARGET_URL"] = "live.douyin.com"
    try:
        # ⚠️ 先回直播首页再抓：feed 只由**首页**触发，
        # 呆在某个直播间页里 reload 是抓不到 /webcast/feed/ 的（实测抓到 2 字节空文件）。
        subprocess.run([_node_bin(), str(cdp_script()), "goto", "https://live.douyin.com/"],
                       cwd=str(_REPO), env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60,
                       creationflags=_NO_WINDOW)
        subprocess.run([_node_bin(), str(cdp_script()), "net", "/webcast/feed/", str(out),
                        "12000", "reload"],
                       cwd=str(_REPO), env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=90,
                       creationflags=_NO_WINDOW)
    except Exception as e:
        print(f"[refresh] feed 重抓失败: {e}", flush=True)
        return []
    try:
        rooms = parse_feed_capture(str(out))
    except Exception as e:
        print(f"[refresh] 解析失败: {e}", flush=True)
        return []
    if not rooms:
        print("[refresh] 本次没抓到 feed（页面不在首页？）→ 沿用旧清单", flush=True)
        return []
    # 高流量优先（人多的房更可能发福袋）
    rooms.sort(key=lambda r: -(r.get("user_count") or 0))
    print(f"[refresh] 新清单 {len(rooms)} 房", flush=True)
    return rooms


def main() -> int:
    ap = argparse.ArgumentParser(description="福袋真机触发实验（定位 + 抓帧）")
    ap.add_argument("--feed-capture", required=True, help="CDP 抓的 feed 响应 JSON")
    ap.add_argument("--config", default="data/livetest.config.json")
    ap.add_argument("--rounds", type=int, default=12)
    ap.add_argument("--interval", type=float, default=15.0)
    ap.add_argument("--capture-ms", type=int, default=150000,
                    help="命中后抓帧时长（毫秒，默认 150s）")
    ap.add_argument("--max-captures", type=int, default=1, help="最多抓几次")
    ap.add_argument("--refresh-every", type=int, default=0,
                    help="每 N 轮经 CDP 重抓 feed 换房源（0=不刷新）")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    rooms = parse_feed_capture(args.feed_capture)
    if not rooms:
        print("抓包里没有解析出房间")
        return 1
    rooms.sort(key=lambda r: -(r.get("user_count") or 0))
    out_path = Path(args.out or f"data/hunt_lottery_{time.strftime('%Y-%m-%d')}.json")
    print(f"待扫 {len(rooms)} 房 × {args.rounds} 轮，间隔 {args.interval}s", flush=True)

    log: List[Dict[str, Any]] = []
    n_captured = 0
    for rnd in range(args.rounds):
        if args.refresh_every and rnd and rnd % args.refresh_every == 0:
            fresh = refresh_rooms_from_feed(time.strftime("%H%M"))
            if fresh:
                rooms = fresh
        for rm in rooms:
            try:
                d = query_luckybox(rm["id_str"], cfg.user_agent)
            except Exception as e:
                log.append({"round": rnd + 1, "web_rid": rm["web_rid"], "err": str(e)})
                continue
            boxes = d.get("data") or []
            if not boxes:
                continue
            extra = d.get("extra") or {}
            print(f"★福袋 [轮{rnd+1}] {rm['web_rid']} {rm['nick'][:14]} "
                  f"boxes={len(boxes)} round={extra.get('current_round')} "
                  f"pct={extra.get('pct')}", flush=True)
            rec = {"round": rnd + 1, "ts": int(time.time()),
                   "web_rid": rm["web_rid"], "room_id": rm["id_str"],
                   "nick": rm["nick"], "boxes": boxes, "extra": extra}
            if n_captured < args.max_captures:
                tag = time.strftime("%H%M%S") + "_" + rm["web_rid"]
                rec["capture"] = capture_frames(rm["web_rid"], args.capture_ms, tag)
                n_captured += 1
            log.append(rec)
            out_path.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
            if n_captured >= args.max_captures:
                print("已达到抓帧上限，结束。")
                return 0
            time.sleep(0.3)
        if rnd + 1 < args.rounds:
            time.sleep(args.interval)

    out_path.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"未在窗口内命中福袋房（福袋是脉冲式的，换时段或加大 --rounds）；明细 {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
