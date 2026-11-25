"""抖音弹幕协议解析模块。

抖音直播 WebSocket 协议（参考 DouyinLiveWebFetcher/liveMan.py）：
1. 顶层包为 PushFrame（betterproto Message），含 payload_type、log_id、payload 等；
2. payload 通常 gzip 压缩（开头 b'\\x1f\\x8b'），解压后为 Response；
3. Response 含 messages_list，每条 Message 有 method 字段标识业务类型；
4. 按常见 method 分发到对应 betterproto 类解析：
   - WebcastChatMessage   → danmaku（弹幕，必解析）
   - WebcastMemberMessage → enter（进场，加分项）
   - WebcastLikeMessage   → like（点赞，加分项）
   - WebcastGiftMessage   → gift（礼物，加分项）
5. 服务端可能回 need_ack=True，此时需回 ack 包（PushFrame payload_type='ack'、
   payload=internal_ext），否则会被断流。

落盘字段（与 B 站采集器对齐，差异：user_id 而非 uid、type 值 danmaku 而非 danmu）：
    {"ts": int, "type": "danmaku", "user_id": "...", "uname": "...", "text": "..."}
"""

from __future__ import annotations

import gzip
import json
import logging
import time
from typing import Any, Dict, Iterator, Optional, Tuple

from protobuf.douyin import (  # 开源 betterproto 预生成类
    ChatMessage,
    GiftMessage,
    LikeMessage,
    MemberMessage,
    PushFrame,
    Response,
)

logger = logging.getLogger(__name__)

# 业务 method → 落盘 type
_METHOD_TYPE_MAP = {
    "WebcastChatMessage": "danmaku",
    "WebcastMemberMessage": "enter",
    "WebcastLikeMessage": "like",
    "WebcastGiftMessage": "gift",
}


def parse_pushframe(buf: bytes) -> PushFrame:
    """解析顶层 PushFrame。"""
    return PushFrame().parse(buf)


def decompress_payload(pushframe: PushFrame) -> bytes:
    """取 PushFrame.payload 并按需 gzip 解压。"""
    payload = pushframe.payload
    if payload[:2] == b"\x1f\x8b":
        return gzip.decompress(payload)
    return payload


def parse_response(payload_decompressed: bytes) -> Response:
    """解析解压后的 payload 为 Response。"""
    return Response().parse(payload_decompressed)


def iter_messages(response: Response) -> Iterator[Tuple[str, str, bytes]]:
    """遍历 Response.messages_list，产出 (method, msg_type, payload_bytes)。

    method 不在 _METHOD_TYPE_MAP 中的会被跳过。
    """
    for msg in response.messages_list:
        method = msg.method
        msg_type = _METHOD_TYPE_MAP.get(method)
        if msg_type is None:
            continue
        yield method, msg_type, msg.payload


def build_ack(log_id: int, internal_ext: str) -> bytes:
    """构造服务端存活确认 ack 包，用 OPCODE_BINARY 发送。"""
    return PushFrame(
        log_id=log_id,
        payload_type="ack",
        payload=internal_ext.encode("utf-8"),
    ).SerializeToString()


# —— v3.3 监控层：method → 监控 type 映射（与主链路 _METHOD_TYPE_MAP 完全独立，
#    互不影响；主链路 31 列契约 / danmu_*.jsonl 落盘零改动）
_MONITOR_TYPE_MAP = {
    "WebcastRoomStatsMessage": "room_stats",
    "WebcastRoomUserSeqMessage": "room_user_seq",
    "WebcastControlMessage": "control",
    "WebcastRoomMessage": "room",
    "WebcastSocialMessage": "social",
    "WebcastRoomRankMessage": "room_rank",
    "WebcastFansclubMessage": "fansclub",
    "WebcastProductChangeMessage": "product_change",
    "WebcastLiveShoppingMessage": "live_shopping",
    # —— PK / 连麦（2026-09-30 复查：proto 里本就有 MatchAgainstScoreMessage /
    #    UpdateFanTicketMessage，上届真机 0 次只是因为房间当时没在 PK）
    "WebcastMatchAgainstScoreMessage": "match_against",
    "WebcastUpdateFanTicketMessage": "fan_ticket",
    # —— 福袋 / 抽奖（★ 真机反解：平台确实推，只是 vendor proto 里没类；
    #    类定义见 protobuf/extras.py，字段语义为推测）
    "WebcastLotteryDrawResultEventMessage": "lottery_draw_result",
    # 福袋/奖券事件本身（真机 1311B 完整载荷：奖品名 / 参与条件 / 起止时间）
    "WebcastLotteryEventNewMessage": "lottery_event",
    "WebcastLotteryEventMessage": "lottery_event",
    "WebcastPreviewCjRpMessage": "lottery_event",
    # 福袋体（真机 3144B：福袋名「钻石红包」/ 时长 / box_id）
    "WebcastLuckyBoxMessage": "luckybox",
    # 福袋中奖名单（真机 346B：#4 = 明文中奖 UID 列表）★「中奖名单」坐实
    "WebcastLuckyBoxRewardMessage": "luckybox_reward",
    # —— 连麦 / PK（★ 决定性证据：vendor proto 无此类，但平台确实推）
    "WebcastLinkMicSendEmojiMessage": "linkmic_emoji",
    "WebcastLinkerContributeMessage": "linker_contribute",
    # 团战（PK）★ 真机实收比分（90s 10 条），补上 v3.4 缺的「PK 比分真机样本」
    "WebcastLinkmicTeamfightScoreMessage": "teamfight_score",
    "WebcastLinkmicTeamfightMessage": "teamfight",
}


def iter_monitor_messages(response: Response) -> Iterator[Tuple[str, str, bytes]]:
    """遍历 Response.messages_list，产出监控层消息 (method, monitor_type, payload)。

    与 iter_messages 互斥独立：主链路只认 4 类业务消息，本函数只认监控类。
    """
    for msg in response.messages_list:
        monitor_type = _MONITOR_TYPE_MAP.get(msg.method)
        if monitor_type is None:
            continue
        yield msg.method, monitor_type, msg.payload


def normalize_monitor(msg_type: str, payload: bytes) -> Optional[Dict[str, Any]]:
    """把监控层消息 payload 解析成落盘 dict（字段为平台原值，不做推断）。

    :return: dict 或 None（解析失败返回 None，调用方忽略）
    """
    # 延迟导入：监控 proto 类只在监控路径加载，主链路零感知
    from protobuf.douyin import (
        ControlMessage,
        FansclubMessage,
        LiveShoppingMessage,
        ProductChangeMessage,
        RoomMessage,
        RoomRankMessage,
        RoomStatsMessage,
        RoomUserSeqMessage,
        SocialMessage,
    )

    def _msg_ts(m) -> int:
        """服务端毫秒时间戳 → 秒；取不到回退本地时钟。"""
        common = getattr(m, "common", None) or getattr(m, "common_info", None)
        raw = getattr(common, "create_time", 0) or getattr(common, "create_timestamp", 0) \
            if common is not None else 0
        return int(raw // 1000) if raw else int(time.time())

    def _common_field(m, name, default=""):
        common = getattr(m, "common", None) or getattr(m, "common_info", None)
        if common is None:
            return default
        val = getattr(common, name, default)
        return default if val is None else val

    def _as_int(v, default: int = 0) -> int:
        """宽容取整：betterproto 在**字段 wire type 与声明不符**时会给出怪值
        （实测把 int64 字段喂成 length-delimited，会得到 list），直接 int() 会抛
        并让整条记录被丢弃。这里统一兜底，保住记录其余字段。"""
        if v is None:
            return default
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, int):
            return v
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    try:
        if msg_type == "room_stats":
            m = RoomStatsMessage().parse(payload)
            online = m.display_value or m.total or 0
            return {
                "ts": _msg_ts(m),
                "type": "room_stats",
                "room_id": str(_common_field(m, "room_id", "")),
                "online": int(online),
                "display_long": m.display_long or "",
                "is_hidden": bool(m.is_hidden),
                "display_type": int(m.display_type),
            }
        if msg_type == "room_user_seq":
            m = RoomUserSeqMessage().parse(payload)
            pv_raw = getattr(m, "total_pv_for_anchor", "") or ""
            try:
                total_pv = int(pv_raw)
            except (TypeError, ValueError):
                total_pv = int(getattr(m, "total_user", 0) or 0)
            ranks = []
            for r in (m.ranks_list or [])[:10]:
                u = getattr(r, "user", None)
                if u is None:
                    continue
                ranks.append({
                    "rank": int(getattr(r, "rank", 0) or 0),
                    "user_id": str(getattr(u, "id_str", "") or getattr(u, "id", "") or ""),
                    "nick_name": getattr(u, "nick_name", "") or "",
                })
            return {
                "ts": _msg_ts(m),
                "type": "room_user_seq",
                "room_id": str(_common_field(m, "room_id", "")),
                "online": int(m.total or 0),
                "total_pv": total_pv,
                "popularity": int(getattr(m, "popularity", 0) or 0),
                "ranks": ranks,
            }
        if msg_type == "control":
            m = ControlMessage().parse(payload)
            # status=3 下播（开源注释）；开播/管理类其他值原样落盘不猜语义
            return {
                "ts": _msg_ts(m),
                "type": "control",
                "room_id": str(_common_field(m, "room_id", "")),
                "status": int(m.status),
            }
        if msg_type == "room":
            m = RoomMessage().parse(payload)
            return {
                "ts": _msg_ts(m),
                "type": "room",
                "room_id": str(_common_field(m, "room_id", "")),
                "content": (m.content or "").strip(),
                "biz_scene": m.biz_scene or "",
                "roommessagetype": int(m.roommessagetype or 0),
            }
        if msg_type == "social":
            m = SocialMessage().parse(payload)
            u = getattr(m, "user", None)
            return {
                "ts": _msg_ts(m),
                "type": "social",
                "room_id": str(_common_field(m, "room_id", "")),
                "user_id": str(getattr(u, "id_str", "") or getattr(u, "id", "") or "") if u else "",
                "uname": (getattr(u, "nick_name", "") or "") if u else "",
                "action": int(m.action or 0),
                "share_type": int(m.share_type or 0),
                "follow_count": int(m.follow_count or 0),
            }
        if msg_type == "room_rank":
            m = RoomRankMessage().parse(payload)
            ranks = []
            for r in (m.ranks_list or [])[:10]:
                u = getattr(r, "user", None)
                if u is None:
                    continue
                ranks.append({
                    "user_id": str(getattr(u, "id_str", "") or getattr(u, "id", "") or ""),
                    "nick_name": getattr(u, "nick_name", "") or "",
                })
            return {
                "ts": _msg_ts(m),
                "type": "room_rank",
                "room_id": str(_common_field(m, "room_id", "")),
                "ranks": ranks,
            }
        if msg_type == "fansclub":
            m = FansclubMessage().parse(payload)
            u = getattr(m, "user", None)
            return {
                "ts": _msg_ts(m),
                "type": "fansclub",
                "room_id": str(_common_field(m, "room_id", "")),
                "user_id": str(getattr(u, "id_str", "") or getattr(u, "id", "") or "") if u else "",
                "uname": (getattr(u, "nick_name", "") or "") if u else "",
                "fan_type": int(m.type or 0),
                "content": (m.content or "").strip(),
            }
        if msg_type == "product_change":
            m = ProductChangeMessage().parse(payload)
            products = []
            for p in (m.update_product_info_list or [])[:20]:
                pid = getattr(p, "promotion_id", 0)
                products.append({
                    "promotion_id": str(pid) if pid is not None else "",
                    "index": int(getattr(p, "index", 0) or 0),
                    "explain_type": int(getattr(p, "explain_type", 0) or 0),
                })
            return {
                "ts": _msg_ts(m),
                "type": "product_change",
                "room_id": str(_common_field(m, "room_id", "")),
                "update_timestamp": int(m.update_timestamp or 0),
                "toast": (m.update_toast or "").strip(),
                "products": products,
                "total": int(m.total or 0),
            }
        if msg_type == "live_shopping":
            m = LiveShoppingMessage().parse(payload)
            pid = getattr(m, "promotion_id", 0)
            return {
                "ts": _msg_ts(m),
                "type": "live_shopping",
                "room_id": str(_common_field(m, "room_id", "")),
                "msg_type": int(m.msg_type or 0),
                "promotion_id": str(pid) if pid is not None else "",
            }
        if msg_type == "match_against":
            # PK 对战比分（竞品监控里最典型的高能时刻触发器）
            # 注意：Against.left_goal/right_goal 是**字符串**分数，另有 *_goal_int 整型
            from protobuf.douyin import MatchAgainstScoreMessage

            def _score(int_val, str_val) -> int:
                try:
                    v = int(int_val or 0)
                except (TypeError, ValueError):
                    v = 0
                if v:
                    return v
                try:
                    return int(str(str_val or "").strip())
                except (TypeError, ValueError):
                    return 0

            m = MatchAgainstScoreMessage().parse(payload)
            a = getattr(m, "against", None)
            return {
                "ts": _msg_ts(m),
                "type": "match_against",
                "room_id": str(_common_field(m, "room_id", "")),
                "left_name": (getattr(a, "left_name", "") or "") if a else "",
                "left_score": _score(getattr(a, "left_goal_int", 0), getattr(a, "left_goal", "")) if a else 0,
                "right_name": (getattr(a, "right_name", "") or "") if a else "",
                "right_score": _score(getattr(a, "right_goal_int", 0), getattr(a, "right_goal", "")) if a else 0,
                "match_status": int(getattr(m, "match_status", 0) or 0),
                "display_status": int(getattr(m, "display_status", 0) or 0),
                "current_goal_stage": int(getattr(a, "current_goal_stage", 0) or 0) if a else 0,
                "final_goal_stage": int(getattr(a, "final_goal_stage", 0) or 0) if a else 0,
            }
        if msg_type == "fan_ticket":
            from protobuf.douyin import UpdateFanTicketMessage
            m = UpdateFanTicketMessage().parse(payload)
            return {
                "ts": _msg_ts(m),
                "type": "fan_ticket",
                "room_id": str(_common_field(m, "room_id", "")),
                "ticket_count_text": (getattr(m, "room_fan_ticket_count_text", "") or ""),
                "ticket_count": int(getattr(m, "room_fan_ticket_count", 0) or 0),
            }
        if msg_type == "lottery_draw_result":
            # ★ 福袋 / 抽奖开奖结果事件（真机反解，类定义在 protobuf/extras.py）
            from protobuf.extras import LotteryDrawResultEventMessage
            m = LotteryDrawResultEventMessage().parse(payload)
            extra_raw = getattr(m, "extra_json", "") or ""
            candidate_num = None
            try:
                candidate_num = int((json.loads(extra_raw) or {}).get("candidate_num"))
            except Exception:
                candidate_num = None
            return {
                "ts": _msg_ts(m),
                "type": "lottery_draw_result",
                "room_id": str(_common_field(m, "room_id", "")),
                "draw_id": str(getattr(m, "draw_id", 0) or 0),
                "winners_blob_len": len(m.winners_blob or b""),
                # 只留前 64 字节：整块动辄 8KB，全留会把 events 文件撑爆；
                # 头部足够判断它是不是 packed varint / 嵌套 message（语义待坐实，引用须标「推断」）
                "winners_blob_head_hex": (m.winners_blob or b"")[:64].hex(),
                "extra_json": extra_raw,
                "candidate_num": candidate_num,   # 参与人数（JSON 里的真值）
                "source": "proto/extras.LotteryDrawResultEventMessage",
            }
        if msg_type == "lottery_event":
            # 福袋 / 奖券事件本身（method 可能有好几个：LotteryEventNew/LotteryEvent/PreviewCjRp）
            # 真机完整样本 1311B（房间 60964041263）：#18=奖品名「钻石」、#11=参与条件、
            # #4/#5=起止时间（差 600s）。旧样本只有 48B 前缀，故曾只声明 common 一层。
            from protobuf.extras import LotteryEventNewMessage
            m = LotteryEventNewMessage().parse(payload)
            conds: Dict[str, str] = {}
            for c in (getattr(m, "conditions", None) or []):
                k = getattr(c, "name", "") or ""
                if not k:
                    continue
                try:
                    conds[k] = (getattr(c, "value", b"") or b"").decode("utf-8", "replace")
                except Exception:
                    conds[k] = ""
            return {
                "ts": _msg_ts(m),
                "type": "lottery_event",
                "room_id": str(_common_field(m, "room_id", "")),
                "activity_id": str(getattr(m, "activity_id", 0) or 0),
                "status": _as_int(getattr(m, "status", 0)),
                "start_time": _as_int(getattr(m, "start_time", 0)),   # unix 秒
                "end_time": _as_int(getattr(m, "end_time", 0)),       # unix 秒
                "prize_name": (getattr(m, "prize_name", "") or ""),    # ★ 奖品名
                "conditions": conds,                                    # ★ 参与条件（键值）
                "rule_url": (getattr(m, "rule_url", "") or "")[:300],
                "payload_len": len(payload or b""),
                "source": "proto/extras.LotteryEventNewMessage",
            }
        if msg_type == "luckybox":
            # 福袋体推送（真机 3144B）：福袋名 / 时长 / box_id
            from protobuf.extras import LuckyBoxMessage
            m = LuckyBoxMessage().parse(payload)
            return {
                "ts": _msg_ts(m),
                "type": "luckybox",
                "room_id": str(_common_field(m, "room_id", "")),
                "box_id": str(getattr(m, "box_id_str", "") or getattr(m, "box_id", 0) or 0),
                "box_name": (getattr(m, "box_name", "") or ""),   # ★ 福袋名（真机「钻石红包」）
                "status": _as_int(getattr(m, "status", 0)),
                "duration": _as_int(getattr(m, "duration", 0)),   # 秒
                "source": "proto/extras.LuckyBoxMessage",
            }
        if msg_type == "luckybox_reward":
            # ★ 福袋中奖名单（真机 346B）：#4 = 明文中奖用户 UID 列表
            from protobuf.extras import LuckyBoxRewardMessage
            m = LuckyBoxRewardMessage().parse(payload)
            winners = list(getattr(m, "winner_ids", None) or [])
            return {
                "ts": _msg_ts(m),
                "type": "luckybox_reward",
                "room_id": str(_common_field(m, "room_id", "")),
                "reward_id": str(getattr(m, "reward_id_str", "") or getattr(m, "reward_id", 0) or 0),
                "winner_count": len(winners),      # ★ 中奖人数
                "winners": winners[:50],           # ★ 中奖用户 UID（超 50 只留前 50）
                "source": "proto/extras.LuckyBoxRewardMessage",
            }
        if msg_type == "linkmic_emoji":
            # 连麦 / PK 中的表情互动（★ 证明平台确实推 LinkMic 一族）
            from protobuf.extras import LinkMicSendEmojiMessage
            m = LinkMicSendEmojiMessage().parse(payload)
            u = getattr(m, "sender", None)
            return {
                "ts": _msg_ts(m),
                "type": "linkmic_emoji",
                "room_id": str(_common_field(m, "room_id", "")),
                "peer_user_id": str(getattr(u, "id", "") or "") if u else "",
                "peer_name": (getattr(u, "nick_name", "") or "") if u else "",
                "source": "proto/extras.LinkMicSendEmojiMessage",
            }
        if msg_type == "linker_contribute":
            # 连麦 / PK 贡献分（★ 同上：平台确实推）
            from protobuf.extras import LinkerContributeMessage
            m = LinkerContributeMessage().parse(payload)
            contributors = []
            for u in (getattr(m, "contributors", None) or [])[:20]:
                contributors.append(str(getattr(u, "id", "") or ""))
            return {
                "ts": _msg_ts(m),
                "type": "linker_contribute",
                "room_id": str(_common_field(m, "room_id", "")),
                "total_score": _as_int(getattr(m, "total_score", 0)),
                "score": _as_int(getattr(m, "score", 0)),
                "score_text": (getattr(m, "score_str", "") or ""),
                "contributors": [c for c in contributors if c][:20],
                "source": "proto/extras.LinkerContributeMessage",
            }
        if msg_type in ("teamfight_score", "teamfight"):
            # ★ 团战（PK）比分：真机实收（房间 30474723465 正在团战，90s 10 条比分）
            # 两条消息共用同一个 Detail 结构（#9 = 两方阵营的比分）
            def _sides(detail) -> list:
                out = []
                for s in (getattr(detail, "sides", None) or []):
                    out.append({
                        "room_id": str(getattr(s, "room_id", 0) or 0),
                        "score": _as_int(getattr(s, "score", 0)),
                        "score_str": (getattr(s, "score_str", "") or ""),
                    })
                return out

            if msg_type == "teamfight_score":
                from protobuf.extras import LinkmicTeamfightScoreMessage as _TF
                m = _TF().parse(payload)
                detail = getattr(m, "detail", None)
                outer_status = None
            else:
                from protobuf.extras import LinkmicTeamfightMessage as _TF
                m = _TF().parse(payload)
                detail = getattr(m, "detail", None)
                outer_status = _as_int(getattr(m, "status", 0))
            sides = _sides(detail) if detail is not None else []
            return {
                "ts": _msg_ts(m),
                "type": msg_type,
                "room_id": str(_common_field(m, "room_id", "")),
                "battle_id": str(getattr(detail, "battle_id", 0) or 0) if detail else "",
                "status": _as_int(getattr(detail, "status", 0)) if detail else 0,
                "outer_status": outer_status,
                "start_time": _as_int(getattr(detail, "start_time", 0)) if detail else 0,
                "duration": _as_int(getattr(detail, "duration", 0)) if detail else 0,
                "sides": sides,          # ★ 两方比分（room_id / score / score_str）
                "side_count": len(sides),
                "source": "proto/extras." + (
                    "LinkmicTeamfightScoreMessage" if msg_type == "teamfight_score"
                    else "LinkmicTeamfightMessage"),
            }
        return None
    except Exception as e:
        logger.warning("解析监控消息 %s 失败: %s（payload 前 80 字节: %r）",
                       msg_type, e, payload[:80])
        return None


def normalize(msg_type: str, payload: bytes) -> Optional[Dict[str, Any]]:
    """把原始消息 payload 解析后统一成落盘结构。

    :return: dict 或 None（解析失败时返回 None，调用方忽略）
    """
    ts: int = int(time.time())
    user_id: Any = ""
    uname: Any = ""
    text: Optional[str] = ""

    try:
        if msg_type == "danmaku":
            m = ChatMessage().parse(payload)
            # betterproto 字段：user.id / user.nick_name / content
            user_id = str(getattr(m.user, "id", "") or "")
            uname = getattr(m.user, "nick_name", "") or ""
            text = m.content or ""
            # common.create_timestamp 为毫秒级，优先用服务端时间
            if getattr(m.common, "create_timestamp", 0):
                ts = int(m.common.create_timestamp // 1000)
        elif msg_type == "enter":
            m = MemberMessage().parse(payload)
            user_id = str(getattr(m.user, "id", "") or "")
            uname = getattr(m.user, "nick_name", "") or ""
            action = "进入了直播间"
            text = action
        elif msg_type == "like":
            m = LikeMessage().parse(payload)
            uname = getattr(m.user, "nick_name", "") or ""
            user_id = str(getattr(m.user, "id", "") or "")
            text = f"点了{m.count}个赞"
        elif msg_type == "gift":
            m = GiftMessage().parse(payload)
            uname = getattr(m.user, "nick_name", "") or ""
            user_id = str(getattr(m.user, "id", "") or "")
            gift_name = getattr(getattr(m, "gift", None), "name", "") or ""
            combo = getattr(m, "combo_count", 0) or 0
            text = f"送出 {gift_name} x{combo}" if combo else f"送出 {gift_name}"
        else:
            return None
    except Exception as e:
        logger.warning("解析 %s 消息失败: %s（payload 前 80 字节: %r）", msg_type, e, payload[:80])
        return None

    return {
        "ts": ts,
        "type": msg_type,
        "user_id": user_id,
        "uname": uname,
        "text": text,
    }
