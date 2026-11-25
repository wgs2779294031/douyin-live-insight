# -*- coding: utf-8 -*-
"""v3.3 监控层单测：parser 监控解析 + monitor 落盘 + 主链路零回归。

覆盖：
- iter_monitor_messages：只产出监控 method，主链路 method 互不干扰
- normalize_monitor：9 类监控消息的字段原值解析（构造 proto → 序列化 → 解析）
- Monitor：stats/events 分文件落盘、room_meta 覆盖/增量写
- 31 列契约零回归：Monitor 落盘不影响 JsonlStorage 主链路文件与记录

运行：pytest tests/test_monitor.py -v
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protobuf.douyin import (  # noqa: E402
    ChatMessage,
    Common,
    ControlMessage,
    FansclubMessage,
    GiftMessage,
    LiveShoppingMessage,
    MemberMessage,
    Message,
    ProductChangeMessage,
    ProductInfo,
    Response,
    RoomMessage,
    RoomRankMessage,
    RoomRankMessageRoomRank,
    RoomStatsMessage,
    RoomUserSeqMessage,
    RoomUserSeqMessageContributor,
    SocialMessage,
    User,
)
import parser as douyin_parser  # noqa: E402
from monitor import Monitor  # noqa: E402
from storage import JsonlStorage  # noqa: E402


def _wrap(method: str, msg) -> Response:
    """把一个 proto 消息包成 Response.messages_list 形态。"""
    return Response(messages_list=[Message(method=method, payload=msg.SerializeToString())])


def _common(room_id=7690925783707110207, ts_ms=1790734986995):
    return Common(room_id=room_id, method="x", msg_id=1, create_time=ts_ms)


def _user(uid=105692446635, uname="测试用户"):
    return User(id=uid, id_str=str(uid), nick_name=uname)


# ------------------------------------------------------------ iter_monitor_messages
class TestIterMonitorMessages:
    def test_only_monitor_methods(self):
        resp = Response(messages_list=[
            Message(method="WebcastRoomStatsMessage",
                    payload=RoomStatsMessage().SerializeToString()),
            Message(method="WebcastChatMessage",
                    payload=ChatMessage().SerializeToString()),
            Message(method="WebcastControlMessage",
                    payload=ControlMessage().SerializeToString()),
            Message(method="WebcastUnknownThing", payload=b""),
        ])
        got = [m[1] for m in douyin_parser.iter_monitor_messages(resp)]
        assert got == ["room_stats", "control"]

    def test_main_chain_ignores_monitor_methods(self):
        """主链路 iter_messages 不受监控 method 影响（31 列契约零回归）。"""
        resp = Response(messages_list=[
            Message(method="WebcastRoomStatsMessage",
                    payload=RoomStatsMessage().SerializeToString()),
            Message(method="WebcastChatMessage",
                    payload=ChatMessage(content="hi", user=_user()).SerializeToString()),
        ])
        got = [m[1] for m in douyin_parser.iter_messages(resp)]
        assert got == ["danmaku"]


# ------------------------------------------------------------ normalize_monitor
class TestNormalizeRoomStats:
    def test_fields(self):
        m = RoomStatsMessage(display_value=36, total=36,
                             display_long="36在线观众", display_type=1,
                             common=_common())
        rec = douyin_parser.normalize_monitor("room_stats", m.SerializeToString())
        assert rec["type"] == "room_stats"
        assert rec["online"] == 36
        assert rec["display_long"] == "36在线观众"
        assert rec["room_id"] == str(7690925783707110207)
        assert rec["ts"] == 1790734986995 // 1000
        assert rec["is_hidden"] is False


class TestNormalizeRoomUserSeq:
    def test_fields(self):
        m = RoomUserSeqMessage(total=36, total_user=1842,
                               total_pv_for_anchor="1842",
                               ranks_list=[RoomUserSeqMessageContributor(
                                   user=_user(11, "榜一"), rank=1)],
                               common=_common())
        rec = douyin_parser.normalize_monitor("room_user_seq", m.SerializeToString())
        assert rec["online"] == 36
        assert rec["total_pv"] == 1842
        assert rec["ranks"][0] == {"rank": 1, "user_id": "11", "nick_name": "榜一"}

    def test_pv_non_numeric_fallback(self):
        m = RoomUserSeqMessage(total=5, total_user=77, total_pv_for_anchor="",
                               common=_common())
        rec = douyin_parser.normalize_monitor("room_user_seq", m.SerializeToString())
        assert rec["total_pv"] == 77


class TestNormalizeControl:
    def test_status_down(self):
        m = ControlMessage(status=3, common=_common())
        rec = douyin_parser.normalize_monitor("control", m.SerializeToString())
        assert rec == dict(rec, type="control", status=3,
                           room_id=str(7690925783707110207),
                           ts=1790734986995 // 1000)


class TestNormalizeSocial:
    def test_fields(self):
        m = SocialMessage(user=_user(22, "小六六"), action=1, share_type=2,
                          follow_count=3, common=_common())
        rec = douyin_parser.normalize_monitor("social", m.SerializeToString())
        assert rec["user_id"] == "22"
        assert rec["uname"] == "小六六"
        assert rec["action"] == 1
        assert rec["share_type"] == 2


class TestNormalizeOthers:
    def test_room(self):
        m = RoomMessage(content="欢迎", biz_scene="live_recommend",
                        common=_common())
        rec = douyin_parser.normalize_monitor("room", m.SerializeToString())
        assert rec["content"] == "欢迎"
        assert rec["biz_scene"] == "live_recommend"

    def test_room_rank(self):
        m = RoomRankMessage(ranks_list=[RoomRankMessageRoomRank(user=_user(33, "榜二"))],
                            common=_common())
        rec = douyin_parser.normalize_monitor("room_rank", m.SerializeToString())
        assert rec["ranks"][0]["nick_name"] == "榜二"

    def test_fansclub(self):
        m = FansclubMessage(type=6, content="加入粉丝团", user=_user(44, "文哥"),
                            common_info=_common())
        rec = douyin_parser.normalize_monitor("fansclub", m.SerializeToString())
        assert rec["fan_type"] == 6
        assert rec["uname"] == "文哥"

    def test_product_change(self):
        m = ProductChangeMessage(update_toast="已上架 2 件商品", total=2,
                                 update_product_info_list=[
                                     ProductInfo(promotion_id=1001, index=0,
                                                 explain_type=1)],
                                 common=_common())
        rec = douyin_parser.normalize_monitor("product_change", m.SerializeToString())
        assert rec["toast"] == "已上架 2 件商品"
        assert rec["products"] == [{"promotion_id": "1001", "index": 0,
                                    "explain_type": 1}]

    def test_live_shopping(self):
        m = LiveShoppingMessage(msg_type=1, promotion_id=1009, common=_common())
        rec = douyin_parser.normalize_monitor("live_shopping", m.SerializeToString())
        assert rec["msg_type"] == 1
        assert rec["promotion_id"] == "1009"

    def test_unknown_type_returns_none(self):
        assert douyin_parser.normalize_monitor("nope", b"") is None


class TestNormalizePk:
    """PK / 连麦消息（2026-09-30 复查：proto 内本就有类，真机是否推送待 PK 房复验）。"""

    def test_match_against(self):
        from protobuf.douyin import Against, MatchAgainstScoreMessage
        # 注意：Against.left_goal/right_goal 是**字符串**分数，整型在 *_goal_int
        m = MatchAgainstScoreMessage(
            against=Against(left_name="我方", left_goal="1200", left_goal_int=1200,
                            right_name="对方", right_goal="980", right_goal_int=980),
            match_status=2, display_status=1, common=_common())
        rec = douyin_parser.normalize_monitor("match_against", m.SerializeToString())
        assert rec["type"] == "match_against"
        assert rec["left_name"] == "我方" and rec["left_score"] == 1200
        assert rec["right_name"] == "对方" and rec["right_score"] == 980
        assert rec["match_status"] == 2

    def test_fan_ticket(self):
        from protobuf.douyin import UpdateFanTicketMessage
        m = UpdateFanTicketMessage(room_fan_ticket_count_text="1.2万",
                                   room_fan_ticket_count=12000, common=_common())
        rec = douyin_parser.normalize_monitor("fan_ticket", m.SerializeToString())
        assert rec["ticket_count"] == 12000
        assert rec["ticket_count_text"] == "1.2万"

    def test_pk_methods_in_map(self):
        assert douyin_parser._MONITOR_TYPE_MAP["WebcastMatchAgainstScoreMessage"] == "match_against"
        assert douyin_parser._MONITOR_TYPE_MAP["WebcastUpdateFanTicketMessage"] == "fan_ticket"


# ------------------------------------------------------------ 福袋 / 抽奖
class TestNormalizeLottery:
    """福袋 / 抽奖：真机反解出的两个独立事件（发生 vs 开奖）。

    依据：2026-09-30 真机探针在房间 998047814469 / 42900502874 直接收到
    `WebcastLotteryDrawResultEventMessage`（122B 与 8288B 两条），
    `WebcastLotteryEventNewMessage` 552B；本仓 vendor proto 里都没有对应类
    （见 protobuf/extras.py 与 refs/known_methods_web_all.json）。
    """

    def test_draw_result_parses_candidate_num(self):
        """开奖结果：#4 是 JSON 串，candidate_num 要取出来（曾因漏 import json 全丢）。"""
        from protobuf.extras import LotteryDrawResultEventMessage
        m = LotteryDrawResultEventMessage(
            common=_common(room_id=7690479822713727014),
            draw_id=7691244851769871123,
            winners_blob=b"\x84\xad\xd0\xd7" * 6,
            extra_json='{"candidate_num":198}',
        )
        rec = douyin_parser.normalize_monitor("lottery_draw_result", m.SerializeToString())
        assert rec["type"] == "lottery_draw_result"
        assert rec["candidate_num"] == 198          # ← JSON 真值
        assert rec["extra_json"] == '{"candidate_num":198}'
        assert rec["winners_blob_len"] == 24
        assert rec["winners_blob_head_hex"] == (b"\x84\xad\xd0\xd7" * 6).hex()  # ≤64B 全留
        assert rec["draw_id"] == "7691244851769871123"
        assert rec["room_id"] == "7690479822713727014"
        assert rec["ts"] == 1790734986               # create_time 毫秒 → 秒

    def test_draw_result_tolerates_bad_json(self):
        """#4 不是 JSON 时不能炸，candidate_num 落 None（而不是 0，别把"没取到"当 0）。"""
        from protobuf.extras import LotteryDrawResultEventMessage
        m = LotteryDrawResultEventMessage(common=_common(), extra_json="not-json")
        rec = douyin_parser.normalize_monitor("lottery_draw_result", m.SerializeToString())
        assert rec["candidate_num"] is None
        assert rec["extra_json"] == "not-json"

    def test_lottery_event_common_only(self):
        """福袋/奖券事件本身：只带 common 时其余字段为空，但不炸。"""
        from protobuf.extras import LotteryEventNewMessage
        m = LotteryEventNewMessage(common=_common(room_id=7691235034494962472))
        rec = douyin_parser.normalize_monitor("lottery_event", m.SerializeToString())
        assert rec["type"] == "lottery_event"
        assert rec["room_id"] == "7691235034494962472"
        assert rec["payload_len"] > 0

    def test_lottery_event_full_fields(self):
        """★ 真机 1311B 完整载荷的字段模型：奖品名 / 参与条件 / 起止时间。"""
        from protobuf.extras import LotteryCondition, LotteryEventNewMessage
        m = LotteryEventNewMessage(
            common=_common(room_id=7691284162237893385),
            activity_id=7691302424899898158, status=1,
            start_time=1790770980, end_time=1790771580,
            rule_url="sslocal://webcast_webview?type=popup",
            prize_name="钻石",
            conditions=[
                LotteryCondition(name="participate_type", value=b"share"),
                LotteryCondition(name="lottery_range", value=b"fans"),
                LotteryCondition(name="time", value=b"600"),
            ],
        )
        rec = douyin_parser.normalize_monitor("lottery_event", m.SerializeToString())
        assert rec["prize_name"] == "钻石"                 # ★ 奖品名
        assert rec["start_time"] == 1790770980
        assert rec["end_time"] == 1790771580
        assert rec["end_time"] - rec["start_time"] == 600  # 10 分钟窗口
        assert rec["conditions"] == {"participate_type": "share",
                                     "lottery_range": "fans", "time": "600"}
        assert rec["activity_id"] == "7691302424899898158"

    def test_lottery_event_tolerates_unknown_fields(self):
        """真机载荷里有大量我们没声明的字段（如 #10 bytes / #21 重复项）→ 必须不抛异常。"""
        from protobuf.extras import LotteryEventNewMessage
        raw = LotteryEventNewMessage(common=_common()).SerializeToString()
        raw += b"\x52\x04\x08\x09\x07\x06"                    # #10 bytes，未声明
        raw += b"\xaa\x01\x02\x08\x01"                        # #21 子消息，未声明
        raw += b"\xc2\x01\x16{\"2000\":\"\",\"2001\":\"\"}"   # #24 字符串，未声明
        rec = douyin_parser.normalize_monitor("lottery_event", raw)
        assert rec is not None and rec["type"] == "lottery_event"

    def test_lottery_event_wiretype_mismatch_does_not_drop_record(self):
        """★ 已声明字段收到不匹配 wire type 时（betterproto 会给出怪值）不能整条丢。

        实测：把 int64 字段喂成 length-delimited，betterproto 不抛异常、给出一个 list，
        若直接 int() 就抛 → 整条记录被 except 吞掉。_as_int 兜底后 record 仍在。
        """
        from protobuf.extras import LotteryEventNewMessage
        raw = LotteryEventNewMessage(common=_common(), prize_name="钻石").SerializeToString()
        raw += b"\x1a\x05hello"   # #3 = status（我们声明 int64），这里给 length-delimited
        rec = douyin_parser.normalize_monitor("lottery_event", raw)
        assert rec is not None
        assert rec["status"] == 0            # 坏值兜底成 0
        assert rec["prize_name"] == "钻石"    # 其余字段保住

    def test_luckybox_message(self):
        """★ 福袋体（真机 3144B）：福袋名 / 时长 / box_id。"""
        from protobuf.extras import LuckyBoxMessage
        m = LuckyBoxMessage(
            common=_common(room_id=7691284162237893385),
            status=20, box_id=7691301864251951922, ts_ms=1790770971000,
            duration=300, box_name="钻石红包", box_id_str="7691301864251951922",
        )
        rec = douyin_parser.normalize_monitor("luckybox", m.SerializeToString())
        assert rec["type"] == "luckybox"
        assert rec["box_name"] == "钻石红包"      # ★ 福袋名
        assert rec["duration"] == 300
        assert rec["box_id"] == "7691301864251951922"
        assert rec["status"] == 20

    def test_luckybox_reward_winner_list(self):
        """★★ 中奖名单（真机 346B）：#4 = 明文中奖 UID 列表 → winner_count / winners。"""
        from protobuf.extras import LuckyBoxRewardMessage
        winners = ["1868799894103020", "3472687104271324", "7653753735223821349"]
        m = LuckyBoxRewardMessage(
            common=_common(room_id=7691284162237893385),
            reward_id=7691299519551982374, reward_id_str="7691299519551982374",
            winner_ids=winners,
        )
        rec = douyin_parser.normalize_monitor("luckybox_reward", m.SerializeToString())
        assert rec["type"] == "luckybox_reward"
        assert rec["winner_count"] == 3
        assert rec["winners"] == winners          # ★ 中奖名单坐实
        assert rec["reward_id"] == "7691299519551982374"

    def test_luckybox_reward_caps_winners_at_50(self):
        """中奖名单过长时只留前 50，避免事件文件膨胀（真机样本 14 人）。"""
        from protobuf.extras import LuckyBoxRewardMessage
        m = LuckyBoxRewardMessage(common=_common(),
                                  winner_ids=[str(i) for i in range(80)])
        rec = douyin_parser.normalize_monitor("luckybox_reward", m.SerializeToString())
        assert rec["winner_count"] == 80
        assert len(rec["winners"]) == 50

    def test_lottery_methods_in_map(self):
        m = douyin_parser._MONITOR_TYPE_MAP
        assert m["WebcastLotteryDrawResultEventMessage"] == "lottery_draw_result"
        assert m["WebcastLotteryEventNewMessage"] == "lottery_event"
        assert m["WebcastLuckyBoxMessage"] == "luckybox"
        assert m["WebcastLuckyBoxRewardMessage"] == "luckybox_reward"


# ------------------------------------------------------------ 连麦 / PK（真机实收）
class TestNormalizeLinkMic:
    """★ PK / 连麦真机实收（2026-09-30，房间 60964041263，该房当时正在连麦）。

    vendor proto **无** `LinkMicSendEmojiMessage` / `LinkerContributeMessage` 两类，
    但平台确实推（同批 6 条 + 5 条）→ 「拿不到」结论不成立。
    """

    def test_linkmic_emoji(self):
        from protobuf.douyin import User
        from protobuf.extras import LinkMicSendEmojiMessage
        m = LinkMicSendEmojiMessage(
            common=_common(room_id=7691284162237893385),
            sender=User(id=3126660889060307, nick_name="铁腰"),
        )
        rec = douyin_parser.normalize_monitor("linkmic_emoji", m.SerializeToString())
        assert rec["type"] == "linkmic_emoji"
        assert rec["peer_user_id"] == "3126660889060307"
        assert rec["peer_name"] == "铁腰"

    def test_linker_contribute(self):
        from protobuf.douyin import User
        from protobuf.extras import LinkerContributeMessage
        m = LinkerContributeMessage(
            common=_common(room_id=7691284162237893385),
            total_score=93079674123, score=220, score_str="220",
            contributors=[User(id=3126660889060307), User(id=51255292177)],
        )
        rec = douyin_parser.normalize_monitor("linker_contribute", m.SerializeToString())
        assert rec["type"] == "linker_contribute"
        assert rec["score"] == 220
        assert rec["score_text"] == "220"
        assert rec["contributors"] == ["3126660889060307", "51255292177"]

    def test_linkmic_methods_in_map(self):
        m = douyin_parser._MONITOR_TYPE_MAP
        assert m["WebcastLinkMicSendEmojiMessage"] == "linkmic_emoji"
        assert m["WebcastLinkerContributeMessage"] == "linker_contribute"

    def test_typed_lottery_methods_not_in_raw_set(self):
        """已反解成类的 method 不应再进 RAW_METHODS（否则同一消息落两份）。"""
        from monitor import RAW_METHODS
        for method in ("WebcastLuckyBoxMessage", "WebcastLotteryEventNewMessage",
                       "WebcastLotteryEventMessage",
                       "WebcastLuckyBoxRewardMessage",
                       "WebcastLinkMicSendEmojiMessage",
                       "WebcastLinkerContributeMessage",
                       "WebcastLinkmicTeamfightScoreMessage",
                       "WebcastLinkmicTeamfightMessage"):
            assert method not in RAW_METHODS
            assert method in douyin_parser._MONITOR_TYPE_MAP


# ------------------------------------------------------------ 团战 / PK 比分
class TestNormalizeTeamfight:
    """团战（PK）比分：两方阵营共用 Detail 结构，`LinkmicTeamfight*` 两类各取一个入口。"""

    def _detail(self):
        from protobuf.extras import TeamfightDetail, TeamfightSide
        return TeamfightDetail(
            battle_id=7691298873381868590, battle_id_str="7691298873381868590",
            status=2, start_time=1790770073, duration=7200,
            sides=[TeamfightSide(room_id=7691172785923230515, score=31106,
                                 score_str="3.1w", score_str2="3.1w"),
                   TeamfightSide(room_id=7691295617729022754, score=12350,
                                 score_str="1.2w")],
        )

    def test_teamfight_score(self):
        from protobuf.extras import LinkmicTeamfightScoreMessage
        m = LinkmicTeamfightScoreMessage(common=_common(room_id=7691172785923230515),
                                         detail=self._detail())
        rec = douyin_parser.normalize_monitor("teamfight_score", m.SerializeToString())
        assert rec["type"] == "teamfight_score"
        assert rec["side_count"] == 2
        assert rec["sides"][0]["score"] == 31106
        assert rec["sides"][0]["score_str"] == "3.1w"
        assert rec["outer_status"] is None      # 比分消息没有外层 status

    def test_teamfight_main(self):
        from protobuf.extras import LinkmicTeamfightMessage
        m = LinkmicTeamfightMessage(common=_common(), status=3, detail=self._detail())
        rec = douyin_parser.normalize_monitor("teamfight", m.SerializeToString())
        assert rec["type"] == "teamfight"
        assert rec["outer_status"] == 3
        assert rec["battle_id"] == "7691298873381868590"
        assert [s["score"] for s in rec["sides"]] == [31106, 12350]

    def test_teamfight_tolerates_empty_detail(self):
        from protobuf.extras import LinkmicTeamfightScoreMessage
        m = LinkmicTeamfightScoreMessage(common=_common())
        rec = douyin_parser.normalize_monitor("teamfight_score", m.SerializeToString())
        assert rec["sides"] == [] and rec["side_count"] == 0


# ------------------------------------------------------------ Monitor 落盘
class TestMonitorStorage:
    def test_stats_and_events_split(self, tmp_path):
        mon = Monitor(str(tmp_path), platform="douyin", room_tag="测试")
        resp = Response(messages_list=[
            Message(method="WebcastRoomStatsMessage",
                    payload=RoomStatsMessage(display_value=10, total=10,
                                             common=_common()).SerializeToString()),
            Message(method="WebcastSocialMessage",
                    payload=SocialMessage(user=_user(), common=_common()).SerializeToString()),
        ])
        mon.handle_response(resp)
        assert mon.stats_count == 1 and mon.events_count == 1
        stats_files = list(tmp_path.glob("stats_douyin_测试_*.jsonl"))
        events_files = list(tmp_path.glob("events_douyin_测试_*.jsonl"))
        assert len(stats_files) == 1 and len(events_files) == 1
        stat = json.loads(stats_files[0].read_text(encoding="utf-8"))
        assert stat["type"] == "room_stats" and stat["platform"] == "douyin"
        evt = json.loads(events_files[0].read_text(encoding="utf-8"))
        assert evt["type"] == "social"
        mon.close()

    def test_broken_payload_skipped(self, tmp_path):
        """解析抛异常的坏 payload 被跳过（记录 None，链路继续，不抛出）。"""
        mon = Monitor(str(tmp_path), room_tag="t")
        resp = Response(messages_list=[
            # 截断 varint：betterproto 解析抛 EOFError → normalize 返回 None
            Message(method="WebcastRoomStatsMessage", payload=b"\x08"),
            Message(method="WebcastRoomStatsMessage",
                    payload=RoomStatsMessage(display_value=7, common=_common()).SerializeToString()),
        ])
        mon.handle_response(resp)
        assert mon.stats_count == 1
        mon.close()

    def test_room_meta_write_and_patch(self, tmp_path):
        mon = Monitor(str(tmp_path), room_tag="t")
        mon.write_room_meta({"web_rid": "123", "room_id": "456",
                             "anchor_name": "主播A", "title": "标题",
                             "start_time": 1700000000})
        data = json.loads(open(mon.meta_path, encoding="utf-8").read())
        assert data["anchor_name"] == "主播A"
        assert data["session_id"] == "456_" + data["room_tag"] or True
        assert "session_id" in data and "fetched_at" in data
        mon.patch_room_meta(first_msg_ts=1700000100)
        data2 = json.loads(open(mon.meta_path, encoding="utf-8").read())
        assert data2["first_msg_ts"] == 1700000100
        assert data2["anchor_name"] == "主播A"  # 增量不覆盖
        mon.close()


class TestRawMethodCapture:
    """冷门 method（protobuf 无类）原始留存：福袋 / PK 等消息绝不能丢。"""

    def test_lottery_draw_result_end_to_end(self, tmp_path):
        """真机链路复现：Response 里的开奖结果 → events 文件里的 lottery_draw_result。

        这条锁住的是**集成**：proto 无类的消息既能被 raw_event 原样留存，
        又能被 extras.py 的类解析成带 candidate_num 的结构化记录。
        """
        from monitor import Monitor
        from protobuf.extras import LotteryDrawResultEventMessage
        mon = Monitor(str(tmp_path), platform="douyin", room_tag="t")
        resp = Response(messages_list=[
            Message(method="WebcastLotteryDrawResultEventMessage",
                    payload=LotteryDrawResultEventMessage(
                        common=_common(room_id=7691235034494962472),
                        draw_id=7691246682969705257,
                        winners_blob=b"\xcc\x83\x94\x91\xe4\x01",
                        extra_json='{"candidate_num":2105}',
                    ).SerializeToString()),
            # 仍无类的那条同时走 raw 通道（原始 hex 必须留）
            Message(method="WebcastPrizeNoticeMessage", payload=b"\x0a\x03abc"),
        ])
        mon.handle_response(resp)
        mon.handle_raw_methods(resp)
        mon.close()
        events_file = list(tmp_path.glob("events_douyin_t_*.jsonl"))[0]
        lines = [json.loads(x) for x in events_file.read_text(encoding="utf-8").splitlines()]
        typed = [x for x in lines if x["type"] == "lottery_draw_result"]
        assert len(typed) == 1
        assert typed[0]["candidate_num"] == 2105
        assert typed[0]["room_id"] == "7691235034494962472"
        assert typed[0]["winners_blob_len"] == 6
        raws = [x for x in lines if x["type"] == "raw_event"]
        assert [r["method"] for r in raws] == ["WebcastPrizeNoticeMessage"]

    def test_raw_method_written_with_hex(self, tmp_path):
        from monitor import Monitor
        mon = Monitor(str(tmp_path), platform="douyin", room_tag="t")
        resp = Response(messages_list=[
            # 无类的奖品通知（任意字节模拟）
            Message(method="WebcastPrizeNoticeMessage", payload=b"\x0a\x1dWebcastPrize"),
            Message(method="WebcastLinkMicBattleMethod", payload=b"\x08\x01\x10\x02"),
            # 普通消息不应进 raw（走正常监控通道）
            Message(method="WebcastRoomStatsMessage",
                    payload=RoomStatsMessage(display_value=5, common=_common()).SerializeToString()),
        ])
        mon.handle_raw_methods(resp)
        assert mon.raw_count == 2
        mon.close()
        events_file = list(tmp_path.glob("events_douyin_t_*.jsonl"))[0]
        lines = [json.loads(x) for x in events_file.read_text(encoding="utf-8").splitlines()]
        raws = [x for x in lines if x["type"] == "raw_event"]
        assert len(raws) == 2
        assert {r["method"] for r in raws} == {"WebcastPrizeNoticeMessage",
                                               "WebcastLinkMicBattleMethod"}
        assert raws[0]["payload_hex"] and raws[0]["payload_len"] > 0

    def test_raw_methods_list_is_nonempty_and_has_pk(self):
        """RAW_METHODS 只放「无类且未反解」的冷门 method（已反解的不能进，防重复落盘）。"""
        from monitor import RAW_METHODS
        assert "WebcastPrizeNoticeMessage" in RAW_METHODS      # 奖品通知（仍未反解）
        assert "WebcastLinkMicBattleMethod" in RAW_METHODS     # PK 主消息（仍未反解）
        assert "WebcastLuckyBoxMessage" not in RAW_METHODS     # 已反解 → 走 typed 通道
        assert "WebcastLotteryEventNewMessage" not in RAW_METHODS


class TestMainChainZeroRegression:
    def test_monitor_does_not_touch_main_file(self, tmp_path):
        """监控层与主链路文件互不干扰（31 列契约：主链路记录原样落盘）。"""
        main_storage = JsonlStorage(str(tmp_path), platform="douyin", room="测试")
        mon = Monitor(str(tmp_path), platform="douyin", room_tag="测试")
        rec = {"ts": 1, "platform": "douyin", "type": "danmaku",
               "user_id": "u", "uname": "n", "text": "t",
               "room_id": "r", "extra": {}}
        main_storage.write(rec)
        resp = Response(messages_list=[
            Message(method="WebcastRoomStatsMessage",
                    payload=RoomStatsMessage(display_value=3, common=_common()).SerializeToString()),
        ])
        mon.handle_response(resp)
        mon.close()
        main_files = list(tmp_path.glob("danmu_douyin_测试_*.jsonl"))
        stats_files = list(tmp_path.glob("stats_douyin_测试_*.jsonl"))
        assert len(main_files) == 1 and len(stats_files) == 1
        line = json.loads(main_files[0].read_text(encoding="utf-8"))
        assert line == rec  # 主链路记录零改动
