"""真机探针 v2：统计抖音 WSS 实际推送的**全部** method 类型与频次。

为什么要有它
------------
要做「在线人数快照 / 系统事件（福袋·投票·抽奖·商品·开播下播 / PK·连麦）」之前，
必须先知道协议层到底推不推这些信号、频次多少、字段长什么样 —— 不然就是照开源
仓库猜。（现网 parser 只认 4 种 method，其余全部被 `iter_messages` 丢掉。）

v2 相对 v1 的三处增强（2026-09-30 老大质疑「proto 枚举不全」后的复查）：
1. **修掉按房间分桶污染**：v1 用「链式包装 iter_messages」导致后装房间的 wrapped
   会先把消息记进自己的桶、再 yield 给先装房间的 wrapped（两桶都变成并集）。
   v2 改用 **thread-local 当前房间**（每房间一个采集线程，天然隔离）；
2. **未知 method 记 payload 前缀（hex）**：proto 里没有对应类的 method，光有名字
   不够定位，留下前 48 字节便于事后辨认（很多「以为拿不到」的事件其实是被丢弃的
   冷门 method）；
3. **弹幕关键词 + 礼物全字段**：扫描弹幕里的福袋/PK 特征词（带时间戳，用于验证
   A 通道「弹幕间接信号」是否可用），并把 GiftMessage 的
   gift.name / gift.id / diamond_count / combo / action_type / for_linkmic 全部展开
   —— 检验「福袋是不是 GiftMessage 特殊载荷」这一假设。

只输出统计 JSON，**不落业务原始数据流**（弹幕只留关键词命中的文本片段与时间）。

用法
----
    python scripts/probe_methods.py --room <web_rid> [--room <web_rid> ...] \
        --seconds 300 --out data/probe_methods.json

输出结构
--------
    {"started_at": ..., "seconds": ..., "rooms": {
        "<web_rid>": {
            "msg_count": ..., "known_types": {"danmaku": n, ...},
            "keyword_hits": {"<关键词>": [{"offset_s": .., "text": ".."}, ...]},
            "gifts": [{"offset_s":.., "name":.., "id":.., "diamond_count":..,
                        "combo":.., "action_type":.., "for_linkmic":..}, ...],
            "methods": {"<method>": {"count": n, "first_seen_offset": s,
                         "payload_prefix_hex": "…",  # 仅未解码成功的
                         "samples": [{"_decoded": true, "fields": {...}}, ...]}}}}}

解码规则：protobuf 类名与 method 同名（去掉 `Webcast` 前缀），
即 `WebcastRoomStatsMessage` → `protobuf.douyin.RoomStatsMessage`；
没有对应类的只记 method 名、payload 长度与前缀 hex。
"""
from __future__ import annotations

import argparse
import collections
import dataclasses
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import parser as douyin_parser  # noqa: E402  （client 以模块属性引用，运行时可替换）
from client import DanmuClient  # noqa: E402
from config import load_config  # noqa: E402
import protobuf.douyin as douyin_proto  # noqa: E402

# 与需求直接对应、必须重点确认的关键 method（就算一次也没来也要在报告里写明「0 次」）
KEY_METHODS = [
    "WebcastRoomStatsMessage",          # 在线人数（display_value / total）
    "WebcastRoomUserSeqMessage",        # 在线人数 / 累计观看 PV（total_pv_for_anchor）
    "WebcastControlMessage",            # 开播 / 下播（开源注释：status=3 下播）
    "WebcastProductChangeMessage",      # 商品上架 / 变更
    "WebcastLiveShoppingMessage",       # 商品讲解状态
    "WebcastRoomMessage",               # 房间信息
    "WebcastRoomRankMessage",           # 房间榜单
    "WebcastFansclubMessage",           # 粉丝团
    "WebcastSocialMessage",             # 关注 / 分享等社交动作
    # —— PK / 连麦（2026-09-30 从 web 前端 client-entry~1.js 提取到平台真实 method 名；
    #    ★ 注意：web 端 PK 走的是 LinkMicBattleMethod 这一套，**不是** MatchAgainstScore）
    "WebcastLinkMicBattleMethod",       # ★ PK 主消息（对战过程/比分）
    "WebcastLinkMicBattleFinishMethod",  # ★ PK 结束
    "WebcastLinkMicArmiesMethod",       # ★ PK 助力（军队）
    "WebcastLinkMicMethod",             # ★ 连麦申请 / 控制
    "WebcastLinkSettingNotifyMessage",  # 连麦权限变更
    "WebcastLinkmicTeamfightMessage",   # 团战
    "WebcastLinkmicTeamfightScoreMessage",  # 团战比分
    "WebcastMatchAgainstScoreMessage",  # 老命名对战比分（proto 有类，web 端列表没有）
    "WebcastUpdateFanTicketMessage",    # PK 粉丝助力票（proto 有类）
    # —— 福袋 / 抽奖（★ 真机已抓到 WebcastLotteryEventNewMessage 552 字节——
    #    「proto 里没有类」不等于「平台不推」，这几个名字都来自平台前端枚举 / 真机）
    "WebcastLuckyBoxMessage",           # ★ 福袋（平台前端枚举里的名字）
    "WebcastLotteryEventNewMessage",    # ★ 真机实测出现过（奖券/福袋事件）
    "WebcastLotteryEventMessage",       # 抽奖事件（旧命名）
    "WebcastPrizeNoticeMessage",        # 奖品发放通知（福袋/抽奖/PK 奖励）
    "WebcastPreviewCjRpMessage",        # 红包 / 福袋即将开奖
    "WebcastPollMessage",               # 投票（验证是否真推）
    # —— 其他高价值冷门 method
    "WebcastScreenChatMessage",         # 屏幕弹幕
    "WebcastNoticeMessage",             # 系统通知
    "WebcastEmojiChatMessage",          # 表情弹幕
    "WebcastLotteryEventMessage",       # 抽奖（重复项保留以防改名）
]

# 弹幕特征词：用于验证「A 通道：用弹幕间接识别福袋/PK」
KEYWORDS = ["福袋", "口令", "截屏", "抽奖", "开奖", "参与", "领", "PK", "连麦", "助力", "扣", "1"]

# 未解码 method 保留的 payload 前缀字节数
PREFIX_BYTES = 48

_tls = threading.local()


def _is_empty(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, (str, list, dict, tuple, bytes)) and len(v) == 0:
        return True
    if isinstance(v, bool) and v is False:
        return True
    if isinstance(v, (int, float)) and v == 0:
        return True
    return False


def _summarize(obj: Any, depth: int = 0) -> Any:
    """递归取非空字段，截断长串，限制深度 —— 只为「看清字段长什么样」。"""
    if depth >= 3:
        return "…"
    if isinstance(obj, bytes):
        return f"<{len(obj)}B>"
    if not (dataclasses.is_dataclass(obj) and not isinstance(obj, type)):
        if isinstance(obj, str) and len(obj) > 60:
            return obj[:57] + "..."
        return obj
    out: Dict[str, Any] = {}
    for f in dataclasses.fields(obj):
        v = getattr(obj, f.name, None)
        if _is_empty(v):
            continue
        if isinstance(v, list):
            out[f.name] = [_summarize(x, depth + 1) for x in v[:3]]
        elif isinstance(v, dict):
            out[f.name] = {str(k)[:30]: _summarize(x, depth + 1) for k, x in list(v.items())[:3]}
        else:
            out[f.name] = _summarize(v, depth + 1)
    return out


def _decode_sample(method: str, payload: bytes) -> Dict[str, Any]:
    name = method[len("Webcast"):] if method.startswith("Webcast") else method
    cls = getattr(douyin_proto, name, None)
    if cls is None:
        # 没有对应类：留下前缀 hex，便于事后人工辨认结构
        return {"_decoded": False, "payload_len": len(payload),
                "payload_prefix_hex": payload[:PREFIX_BYTES].hex()}
    try:
        m = cls().parse(payload)  # type: ignore[union-attr]
        return {"_decoded": True, "fields": _summarize(m)}
    except Exception as e:  # 解析失败也如实记录，别让它看起来像「没信号」
        return {"_decoded": False, "err": str(e)[:100], "payload_len": len(payload),
                "payload_prefix_hex": payload[:PREFIX_BYTES].hex()}


def _scan_keywords(text: str, bucket: Dict[str, list], start: float) -> None:
    """弹幕关键词命中记录（同一关键词每分钟最多留 5 条样本）。"""
    if not text:
        return
    for kw in KEYWORDS:
        if kw in text:
            hits = bucket.setdefault(kw, [])
            if len(hits) < 60:
                hits.append({"offset_s": round(time.time() - start, 1),
                             "text": text[:80]})


def _record_gift(method: str, payload: bytes, gifts: list, start: float) -> None:
    """礼物消息全字段（验证「福袋是不是 GiftMessage 特殊载荷」）。"""
    if method != "WebcastGiftMessage":
        return
    try:
        m = douyin_proto.GiftMessage().parse(payload)
        g = getattr(m, "gift", None)
        gifts.append({
            "offset_s": round(time.time() - start, 1),
            "name": getattr(g, "name", "") or "",
            "gift_id": getattr(g, "id", 0) or 0,
            "diamond_count": getattr(g, "diamond_count", 0) or 0,
            "combo": getattr(m, "combo_count", 0) or 0,
            "repeat_count": getattr(m, "repeat_count", 0) or 0,
            "action_type": getattr(g, "action_type", 0) or 0,
            "for_linkmic": getattr(g, "for_linkmic", False),
            "for_fansclub": getattr(g, "for_fansclub", False),
            "user": getattr(getattr(m, "user", None), "nick_name", "") or "",
        })
    except Exception:
        pass


def _install_recorder(raw_sink: Dict[str, list], raw_limit: int) -> None:
    """全局装一次：按 thread-local 的当前房间分桶（每房间一个采集线程 → 天然隔离）。

    :param raw_sink: {method: [{"offset_s":.., "hex":..}]} —— 未解码 method 的完整载荷
    :param raw_limit: 每个 method 最多留多少条完整载荷
    """
    orig = douyin_parser.iter_messages

    def wrapped(response):
        bucket = getattr(_tls, "bucket", None)
        if bucket is not None:
            start = bucket["_start"]
            for m in getattr(response, "messages_list", None) or []:
                rec = bucket["methods"].get(m.method)
                if rec is None:
                    rec = bucket["methods"][m.method] = {
                        "count": 0,
                        "first_seen_offset": round(time.time() - start, 1),
                        "samples": [],
                    }
                rec["count"] += 1
                if len(rec["samples"]) < 3:
                    rec["samples"].append(_decode_sample(m.method, m.payload))
                # 未解码 method → 留下完整 payload（供离线逆向字段结构）
                name = m.method[len("Webcast"):] if m.method.startswith("Webcast") else m.method
                if getattr(douyin_proto, name, None) is None:
                    lst = raw_sink.setdefault(m.method, [])
                    if len(lst) < raw_limit:
                        lst.append({"offset_s": round(time.time() - start, 1),
                                    "hex": m.payload.hex()})
                # 弹幕关键词（走主链路 method）
                if m.method == "WebcastChatMessage":
                    try:
                        cm = douyin_proto.ChatMessage().parse(m.payload)
                        _scan_keywords(cm.content or "", bucket["keyword_hits"], start)
                    except Exception:
                        pass
                _record_gift(m.method, m.payload, bucket["gifts"], start)
        yield from orig(response)

    douyin_parser.iter_messages = wrapped


def _tag_client(client: DanmuClient, bucket: Dict[str, Any]) -> None:
    """给每个 client 的 _handle_message 打上「当前房间桶」，供 wrapped 读取。"""
    orig = client._handle_message

    def wrapped(ws, message):
        _tls.bucket = bucket
        try:
            return orig(ws, message)
        finally:
            _tls.bucket = None

    client._handle_message = wrapped  # type: ignore[method-assign]


class _CountingStorage:
    """DanmuClient 对 storage 的唯一调用是 `write(record)`。"""

    def __init__(self) -> None:
        self.n = 0
        self.types: collections.Counter = collections.Counter()

    def write(self, record: Dict[str, Any]) -> None:
        self.n += 1
        self.types[record.get("type")] += 1


def main() -> int:
    ap = argparse.ArgumentParser(description="抖音 WSS method 类型探针 v2")
    ap.add_argument("--room", action="append", required=True,
                    help="web_rid（可多次传，多房间并行探）")
    ap.add_argument("--config", default="data/livetest.config.json")
    ap.add_argument("--seconds", type=int, default=300)
    ap.add_argument("--out", default="data/probe_methods.json")
    ap.add_argument("--raw-limit", type=int, default=20,
                    help="每个「proto 无对应类」的 method 最多留多少条完整载荷")
    args = ap.parse_args()

    raw_sink: Dict[str, list] = {}
    _install_recorder(raw_sink, args.raw_limit)

    runtimes = []
    for room in args.room:
        cfg = load_config(args.config, room_override=room)
        storage = _CountingStorage()
        bucket: Dict[str, Any] = {"methods": {}, "keyword_hits": {}, "gifts": [],
                                  "_start": time.time()}
        client = DanmuClient(cfg, storage)
        _tag_client(client, bucket)
        th = threading.Thread(target=client.run, name=f"probe-{room}", daemon=True)
        th.start()
        runtimes.append({"room": room, "client": client, "thread": th,
                         "storage": storage, "bucket": bucket, "t0": time.time()})
        time.sleep(1.5)  # 错峰，与 main.py 的 STAGGER 一致
        print(f"[probe] 已启动 {room}", flush=True)

    time.sleep(args.seconds)
    for r in runtimes:
        r["client"].close()
    for r in runtimes:
        r["thread"].join(timeout=10)

    out: Dict[str, Any] = {
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "seconds": args.seconds,
        "rooms": {},
        "raw_unknown_methods": {k: v for k, v in raw_sink.items()},
    }
    for r in runtimes:
        b = r["bucket"]
        out["rooms"][r["room"]] = {
            "msg_count": r["storage"].n,
            "known_types": dict(r["storage"].types),
            "keyword_hits": b["keyword_hits"],
            "gifts": b["gifts"],
            "methods": b["methods"],
        }

    # ---- 控制台摘要：按频次排序 + 关键 method 的 0 次也要写明 ----
    for room, data in out["rooms"].items():
        print(f"\n===== 房间 {room}：落盘 {data['msg_count']} 条 "
              f"known_types={data['known_types']} =====")
        items = sorted(data["methods"].items(), key=lambda kv: -kv[1]["count"])
        for name, rec in items:
            mark = " ★" if name in KEY_METHODS else ""
            print(f"  {rec['count']:>7}  {name}{mark}")
        missing = [k for k in KEY_METHODS if k not in data["methods"]]
        if missing:
            print(f"  —— 本次 0 次的关键 method（{len(missing)} 个）：")
            for m in missing:
                print(f"       · {m}")
        kw = {k: len(v) for k, v in data["keyword_hits"].items()}
        if kw:
            print(f"  —— 弹幕关键词命中：{kw}")
        if data["gifts"]:
            names = collections.Counter(g["name"] for g in data["gifts"])
            print(f"  —— 礼物种类：{dict(names)}")

    if raw_sink:
        print("\n===== 未解码 method（proto 无类）完整载荷已存 =====")
        for name, lst in raw_sink.items():
            print(f"  {name}: {len(lst)} 条，首条 hex 前 60 字节：{lst[0]['hex'][:120]}")
        print("  （离线结构解析见 scripts/pb_decode.py）")

    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    print(f"\n已写出 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
