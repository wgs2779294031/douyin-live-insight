#!/usr/bin/env python
"""福袋大池扫描：先攒一个**大候选池**，再对它狂扫 luckybox —— 解决命中率问题。

为什么需要它
------------
福袋是**脉冲式**的：任意时刻只有极小比例的在播房间在发。实测一次 feed 抓 20 房，
当场扫 luckybox 命中率约 0（2026-09-30：20/20 全空）。但 feed 推荐池**每次请求都换人**
（两次抓包 20 房零重合），所以「多抓几次 feed → 攒大池 → 全池扫」能把命中概率线性拉高。

同时 luckybox 接口**免 cookie/免签名**（HTTP 直连即可），扫一房 ~0.3s，
攒 200 房也才 1 分钟 —— 比「盯 20 房死等」划算得多。

流程
----
1. **攒池**：循环 ``--reloads`` 次（每次先 `cdp.mjs goto` 直播首页，再抓 ``/webcast/feed/``），
   把每次 20 房去重累加。⚠️ 必须先回首页 —— feed 只由首页触发（实测在直播间页 reload 抓到空文件）。
2. **扫池**：对池内每房查 ``/webcast/luckybox/box/list/``（必须用 19 位内部 room_id），
   可 ``--passes`` 多轮。命中即打印 27 字段 box 对象。
3. **取证（可选）**：``--capture-on-hit`` 时对首个命中房抓浏览器侧 WSS 帧并解码，
   看是否出现 ``WebcastLotteryEventNewMessage``。

产出
----
    data/sweep_lottery_<date>.jsonl —— 每房每次扫描结果（含箱对象）
    data/feed-pool_<ts>.json        —— 攒池明细（room_id/web_rid/nick/user_count）

用法
----
    python scripts/sweep_lottery_pool.py --reloads 8 --passes 2 --interval 30 --capture-on-hit
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
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # 便于 import scan_luckybox

from config import load_config  # noqa: E402
from scan_luckybox import parse_feed_capture, query_luckybox  # noqa: E402
from browser_tools import captures_dir, cdp_script, node_binary  # noqa: E402

_REPO = Path(__file__).resolve().parents[1]
_HOME = "https://live.douyin.com/"


def _node_bin() -> str:
    return node_binary()


def _cdp_env() -> Dict[str, str]:
    env = dict(os.environ, CDP_PORT=os.environ.get("CDP_PORT", "9222"))
    env["CDP_TARGET_URL"] = "live.douyin.com"
    env["MSYS_NO_PATHCONV"] = "1"
    return env


def fetch_feed_once(tag: str, wait_ms: int = 12000) -> List[Dict[str, Any]]:
    """回首页 + 抓一次 feed，返回房间清单。失败返回空列表。"""
    caps_dir = captures_dir()
    caps_dir.mkdir(parents=True, exist_ok=True)
    out = caps_dir / f"feed-pool-{tag}.json"
    env = _cdp_env()
    try:
        # ⚠️ 必须先回首页：feed 只由直播首页触发
        subprocess.run([_node_bin(), str(cdp_script()), "goto", _HOME],
                       cwd=str(_REPO), env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=60, creationflags=_NO_WINDOW)
        subprocess.run([_node_bin(), str(cdp_script()), "net", "/webcast/feed/", str(out),
                        str(wait_ms), "reload"],
                       cwd=str(_REPO), env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       timeout=wait_ms / 1000 + 90, creationflags=_NO_WINDOW)
    except Exception as e:
        print(f"  [攒池] feed 抓取异常: {e}", flush=True)
        return []
    try:
        return parse_feed_capture(str(out))
    except Exception as e:
        print(f"  [攒池] 解析异常: {e}", flush=True)
        return []


def build_pool(reloads: int) -> List[Dict[str, Any]]:
    """循环抓 feed，按 id_str 去重累加，返回大候选池（按人气降序）。"""
    pool: Dict[str, Dict[str, Any]] = {}
    for i in range(reloads):
        tag = time.strftime("%H%M%S") + f"-{i}"
        rooms = fetch_feed_once(tag)
        new = 0
        for r in rooms:
            if r["id_str"] not in pool:
                pool[r["id_str"]] = r
                new += 1
        print(f"  [攒池 {i+1}/{reloads}] 本轮 {len(rooms)} 房，新增 {new}，池共 {len(pool)}", flush=True)
        if not rooms:
            print("    ⚠️ 本轮没抓到 feed（浏览器不在首页/未登录？）", flush=True)
    out = sorted(pool.values(), key=lambda r: -(r.get("user_count") or 0))
    return out


def sweep(rooms: List[Dict[str, Any]], passes: int, interval: float,
          cfg, out_path: Path) -> List[Dict[str, Any]]:
    """对池扫 luckybox。返回命中记录列表。"""
    hits: List[Dict[str, Any]] = []
    with open(out_path, "a", encoding="utf-8") as f:
        for p in range(passes):
            for rm in rooms:
                try:
                    d = query_luckybox(rm["id_str"], cfg.user_agent)
                except Exception as e:
                    print(f"  [扫 {p+1}] ERR {rm['web_rid']} {e}", flush=True)
                    continue
                boxes = d.get("data") or []
                extra = d.get("extra") or {}
                rec = {
                    "ts": int(time.time()), "pass": p + 1,
                    "web_rid": rm["web_rid"], "room_id": rm["id_str"],
                    "nick": rm["nick"], "title": rm.get("title", ""),
                    "user_count": rm.get("user_count") or 0,
                    "box_count": len(boxes), "boxes": boxes,
                    "current_round": extra.get("current_round"),
                    "pct": extra.get("pct"),
                    "rush_condition": extra.get("rush_condition"),
                }
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                if boxes:
                    hits.append(rec)
                    print(f"  ★福袋 [扫{p+1}] {rm['web_rid']} {rm['nick'][:14]} "
                          f"boxes={len(boxes)} round={extra.get('current_round')} "
                          f"pct={extra.get('pct')}", flush=True)
                    print(f"     原始: {json.dumps(boxes, ensure_ascii=False)[:800]}", flush=True)
                time.sleep(0.3)
            print(f"  [扫 {p+1}/{passes}] 完，累计命中 {len(hits)}", flush=True)
            if p + 1 < passes:
                time.sleep(interval)
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description="福袋大池扫描（攒池 + 全池扫 luckybox）")
    ap.add_argument("--config", default="data/livetest.config.json")
    ap.add_argument("--reloads", type=int, default=8, help="攒池时抓 feed 的次数（每次约 20 房）")
    ap.add_argument("--passes", type=int, default=1, help="对池扫描的轮数")
    ap.add_argument("--interval", type=float, default=30.0, help="轮间秒数")
    ap.add_argument("--capture-on-hit", action="store_true",
                    help="命中首房后抓 WSS 帧并解码找 WebcastLotteryEventNewMessage")
    ap.add_argument("--capture-ms", type=int, default=120000, help="抓帧时长（毫秒）")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    date = time.strftime("%Y-%m-%d")
    out_path = Path(args.out or f"data/sweep_lottery_{date}.jsonl")

    print(f"=== 阶段 1：攒池（{args.reloads} 次 feed 抓取）===", flush=True)
    rooms = build_pool(args.reloads)
    if not rooms:
        print("池为空 —— 浏览器可能不在首页或未登录。")
        return 1
    print(f"池共 {len(rooms)} 房（去重后）", flush=True)
    pool_dump = _REPO / "data" / f"feed-pool_{time.strftime('%H%M%S')}.json"
    pool_dump.write_text(json.dumps(rooms, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n=== 阶段 2：扫池（{args.passes} 轮）===", flush=True)
    hits = sweep(rooms, args.passes, args.interval, cfg, out_path)

    print(f"\n已写 {out_path}")
    if not hits:
        print(f"未命中在发福袋的房间（池 {len(rooms)} 房 × {args.passes} 轮）。"
              f"福袋脉冲式，可加大 --reloads / --passes 或换时段。")
        return 0

    print(f"命中 {len(hits)} 条。命中房：")
    for h in dict.fromkeys((x["web_rid"], x["nick"]) for x in hits):
        print(f"  · {h[0]} {h[1]}")

    if args.capture_on_hit:
        first = hits[0]
        print(f"\n=== 阶段 3：对首个命中房 {first['web_rid']} 抓 WSS 帧 ===", flush=True)
        try:
            from hunt_lottery_ws import capture_frames  # 复用
            tag = time.strftime("%H%M%S") + "_" + first["web_rid"]
            stats = capture_frames(first["web_rid"], args.capture_ms, tag)
            print("抓帧/解码结论：", json.dumps(stats, ensure_ascii=False)[:800], flush=True)
        except Exception as e:
            print(f"抓帧失败: {e}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
