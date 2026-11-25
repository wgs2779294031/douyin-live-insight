#!/usr/bin/env python
"""解码「浏览器侧 WSS 原始帧」抓包（``cdp.mjs wsframes`` 的产物）。

为什么要有它
------------
公开 webcast proto 只是**子集**，判断「平台到底推不推某个事件」不能靠枚举，
只能靠真机抓帧。两条路都通：
- **本仓库自带客户端**（``scripts/probe_methods.py``）——自己的连接、自己的签名；
- **浏览器侧**（``.browser_bridge/cdp.mjs wsframes``）——用你自己的登录态连接，
  经 CDP 把服务端推下来的二进制帧原样落盘。

本脚本吃后者的 jsonl：base64 → ``PushFrame`` → gzip → ``Response`` → 逐条 Message，
统计 method 频次，并把「主链路 4 类之外」的 method 的**完整 payload hex** 落盘
（配合 ``scripts/pb_decode.py`` 还原字段树；proto 里没有对应类的 method 也能解）。

用法
----
    # 1) 抓帧（浏览器已连到目标直播间）
    node cdp.mjs wsframes captures/ws.jsonl 180000 "im/push" "https://live.douyin.com/<web_rid>"
    # 2) 解码
    python scripts/decode_frames.py captures/ws.jsonl --out data/ws_decoded.json

输出
----
    {"frames": n, "messages": n, "methods": {"Webcast...": count},
     "raw": {"<未知 method>": [{"len": .., "hex": ".."}, ...]}}
"""
from __future__ import annotations

import argparse
import base64
import collections
import gzip
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from protobuf.douyin import PushFrame, Response  # noqa: E402
from browser_tools import resolve_capture  # noqa: E402

# 主链路已稳定解析的 method（其余都是「值得看一眼」的）
_MAIN_CHAIN = {
    "WebcastChatMessage", "WebcastLikeMessage", "WebcastMemberMessage",
    "WebcastGiftMessage", "WebcastSocialMessage", "WebcastControlMessage",
    "WebcastRoomMessage", "WebcastRoomStatsMessage", "WebcastRoomUserSeqMessage",
    "WebcastFansclubMessage",
}


def _resolve(path: str) -> Path:
    return resolve_capture(path)


def main() -> int:
    ap = argparse.ArgumentParser(description="解码浏览器侧 WSS 原始帧")
    ap.add_argument("jsonl", help="cdp.mjs wsframes 产出的 jsonl")
    ap.add_argument("--out", default=None, help="输出统计 JSON（默认不写）")
    ap.add_argument("--max-raw", type=int, default=5,
                    help="每个未知 method 最多保留几条完整 payload hex")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    path = _resolve(args.jsonl)
    methods: collections.Counter = collections.Counter()
    raw: Dict[str, List[Dict[str, Any]]] = {}
    n_frames = n_msgs = n_bad = 0

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        f = json.loads(line)
        if f.get("opcode") != 2:          # 2 = binary；1 = text（心跳/ack 多为 text）
            continue
        n_frames += 1
        try:
            pf = PushFrame().parse(base64.b64decode(f["payloadData"]))
        except Exception:
            n_bad += 1
            continue
        payload = pf.payload
        if payload[:2] == b"\x1f\x8b":
            try:
                payload = gzip.decompress(payload)
            except Exception:
                n_bad += 1
                continue
        try:
            resp = Response().parse(payload)
        except Exception:
            n_bad += 1
            continue
        for m in resp.messages_list:
            n_msgs += 1
            methods[m.method] += 1
            if m.method in _MAIN_CHAIN:
                continue
            bucket = raw.setdefault(m.method, [])
            if len(bucket) < args.max_raw:
                bucket.append({"len": len(m.payload), "hex": m.payload.hex()})

    out = {
        "file": str(path),
        "frames": n_frames,
        "messages": n_msgs,
        "parse_failed_frames": n_bad,
        "methods": dict(methods.most_common()),
        "raw": raw,
    }
    if args.out:
        Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
    if not args.quiet:
        print(f"帧 {n_frames}（解析失败 {n_bad}）→ 消息 {n_msgs}")
        print("method 频次：")
        for name, cnt in methods.most_common():
            mark = "" if name in _MAIN_CHAIN else "  ★非主链路"
            print(f"  {cnt:>6}  {name}{mark}")
        if raw:
            print("非主链路 method 完整载荷已留：")
            for name, items in raw.items():
                print(f"  · {name}（{len(items)} 条，首条 {items[0]['len']}B）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
