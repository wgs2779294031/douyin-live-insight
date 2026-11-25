# -*- coding: utf-8 -*-
"""B站采集器纯函数单测（自 danmu-collect/tests/test_web_client.py 移植，v2.6）。

适配点（任务书 v2.6 第 5 节）：
- 被测模块 web_client → sources.bilibili_web（协议纯函数零改动）；
- 退避重置用例改用 BilibiliWebAdapter 新构造签名 (cfg, storage, ...)，
  storage 用 JsonlStorage(tmp_path)（不再自己写文件）。

覆盖（10 个用例，与原项目一一对应）：
- unpack_packets：version=1 明文 / version=2 zlib 嵌套 / version=3 brotli 嵌套
- parse_danmaku：正常 / 非 DANMU_MSG / 损坏 JSON / 缺字段 info
- brotli 未安装：_HAS_BROTLI=False 时 version=3 包跳过不抛异常
- 退避重置：_on_open 连接成功后 self._backoff 回到 1

运行：pytest tests/test_web_client.py -v
"""
import json
import os
import sys
import zlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sources import bilibili_web as web_client  # noqa: E402  适配新路径
from storage import JsonlStorage  # noqa: E402


# ------------------------------------------------------------ 构造工具
def danmu_json(text="你好世界", uname="昵称", user_hash="hashABC"):
    return json.dumps({
        "cmd": "DANMU_MSG",
        "info": [
            ["", "", "", "", "", "", "", user_hash],
            text,
            [123, uname],
        ],
    }, ensure_ascii=False).encode("utf-8")


def wrap_packet(payload, version, operation=5):
    """16 字节包头 + payload。"""
    return web_client.pack_header(16 + len(payload), 16, version, operation) + payload


def make_collector(tmp_path, room_id=123):
    """按 v2.6 适配器签名构造 BilibiliWebAdapter（cfg 用桩对象，不读配置）。"""
    cfg = SimpleNamespace(web_rid=str(room_id), heartbeat_interval=30)
    storage = JsonlStorage(str(tmp_path), platform="bilibili")
    return web_client.BilibiliWebAdapter(cfg, storage, record_queue=None,
                                         shared_state=None)


# ------------------------------------------------------------ unpack_packets
class TestUnpackPackets:
    def test_version1_plain(self):
        pkt = wrap_packet(danmu_json(), version=1)
        result = web_client.unpack_packets(pkt)
        assert len(result) == 1
        op, body = result[0]
        assert op == 5
        rec = web_client.parse_danmaku(body)
        assert rec["text"] == "你好世界"
        assert rec["uname"] == "昵称"
        assert rec["user_hash"] == "hashABC"

    def test_version2_zlib_nested(self):
        inner = wrap_packet(danmu_json(text="zlib内容"), version=0)
        pkt = wrap_packet(zlib.compress(inner), version=2)
        result = web_client.unpack_packets(pkt)
        assert len(result) == 1
        op, body = result[0]
        assert op == 5
        rec = web_client.parse_danmaku(body)
        assert rec["text"] == "zlib内容"

    def test_version3_brotli_nested(self):
        brotli = pytest.importorskip("brotli", reason="未安装 brotli，跳过该用例")
        inner = wrap_packet(danmu_json(text="brotli内容"), version=0)
        pkt = wrap_packet(brotli.compress(inner), version=3)
        result = web_client.unpack_packets(pkt)
        assert len(result) == 1
        op, body = result[0]
        assert op == 5
        rec = web_client.parse_danmaku(body)
        assert rec["text"] == "brotli内容"

    def test_version3_skipped_when_brotli_missing(self, monkeypatch):
        # brotli 未安装场景：v3 包直接跳过，返回空列表，不抛异常
        monkeypatch.setattr(web_client, "_HAS_BROTLI", False)
        pkt = wrap_packet(b"fake-compressed-bytes", version=3)
        result = web_client.unpack_packets(pkt)
        assert result == []


# ------------------------------------------------------------ parse_danmaku
class TestParseDanmaku:
    def test_normal_danmu_msg(self):
        rec = web_client.parse_danmaku(danmu_json())
        assert rec is not None
        assert rec["type"] == "danmaku"
        assert rec["text"] == "你好世界"

    def test_non_danmu_cmd_returns_none(self):
        body = json.dumps({"cmd": "SEND_GIFT", "data": {}}).encode()
        assert web_client.parse_danmaku(body) is None

    def test_corrupt_json_returns_none(self):
        assert web_client.parse_danmaku(b"{broken json!!!") is None

    def test_missing_info_fields_graceful(self):
        # 只有 cmd 没有 info：不抛异常，返回字段为空字符串
        body = json.dumps({"cmd": "DANMU_MSG"}).encode()
        rec = web_client.parse_danmaku(body)
        assert rec is not None
        assert rec["text"] == ""
        assert rec["uname"] == ""
        assert rec["user_hash"] == ""


# ------------------------------------------------------------ parse_event（v2.7.1：合成 pb 夹具）
# Protocol fixture fields below are entirely synthetic.
# 老版明文 INTERACT_WORD / SEND_GIFT 仅用于验证回退路径。

# Synthetic wire-format fixtures, generated from declared fields only.
# They contain no captured payload, account, avatar, room or cookie.
def _fixture_varint(value):
    result = bytearray()
    while value > 0x7f:
        result.append((value & 0x7f) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def _fixture_uint(field, value):
    return _fixture_varint(field << 3) + _fixture_varint(value)


def _fixture_blob(field, value):
    return _fixture_varint((field << 3) | 2) + _fixture_varint(len(value)) + value


def _fixture_text(field, value):
    return _fixture_blob(field, value.encode("utf-8"))


def _fixture_b64(value):
    import base64
    return base64.b64encode(value).decode("ascii")


def _fixture_enter():
    return _fixture_b64(_fixture_uint(1, 101)
                        + _fixture_text(2, "合成进场用户")
                        + _fixture_uint(7, 1700000000))


def _fixture_gift(uid, name, gift):
    body = _fixture_text(2, name)
    if uid is not None:
        body = _fixture_uint(1, uid) + body
    gift_body = (_fixture_uint(1, 1) + _fixture_text(2, gift)
                 + _fixture_uint(3, 1) + _fixture_uint(10, 1700000000))
    return _fixture_b64(body + _fixture_blob(10, gift_body))


ENTER_PB = _fixture_enter()

GIFT_FREE_PB = _fixture_gift(None, "合成匿名用户", "人气票")

GIFT_PAID_PB = _fixture_gift(102, "合成送礼用户", "粉丝团灯牌")


def enter_v2_raw():
    return json.dumps({"cmd": "INTERACT_WORD_V2", "data": {"pb": ENTER_PB}},
                      ensure_ascii=False).encode()


def gift_free_raw():
    return json.dumps({"cmd": "SEND_GIFT_V2", "data": {"pb": GIFT_FREE_PB}},
                      ensure_ascii=False).encode()


def gift_paid_raw():
    return json.dumps({"cmd": "SEND_GIFT_V2", "data": {"pb": GIFT_PAID_PB}},
                      ensure_ascii=False).encode()


def like_raw(uid, uname):
    return json.dumps({
        "cmd": "LIKE_INFO_V3_CLICK",
        "data": {"uid": uid, "uname": uname, "like_text": "为主播点赞了"},
    }, ensure_ascii=False).encode()


class TestParseEvent:
    def test_parse_event_enter_v2_pb(self):
        'INTERACT_WORD_V2 合成 pb：uid/uname/ts 从 pb 解码。'
        rec = web_client.parse_event(enter_v2_raw())
        assert rec is not None
        assert rec["type"] == "enter"
        assert rec["user_hash"] == '101'
        assert rec["uname"] == '合成进场用户'
        assert rec["ts"] == 1700000000
        assert rec["text"] == "进入直播间"

    def test_parse_event_gift_paid_v2_pb(self):
        'SEND_GIFT_V2 付费礼物合成 pb：uid/uname/gift_name/num 从 pb 解码。'
        rec = web_client.parse_event(gift_paid_raw())
        assert rec is not None
        assert rec["type"] == "gift"
        assert rec["user_hash"] == '102'
        assert rec["uname"] == '合成送礼用户'
        assert "粉丝团灯牌" in rec["text"]
        assert rec["text"] == "赠送 粉丝团灯牌 x1"

    def test_parse_event_gift_free_anonymous_v2_pb(self):
        """免费匿名礼物：user_id 为空属预期正确，gift_name/num 正常，不返 None。"""
        rec = web_client.parse_event(gift_free_raw())
        assert rec is not None
        assert rec["type"] == "gift"
        assert rec["user_hash"] == ""
        assert rec["uname"] == '合成匿名用户'
        assert "人气票" in rec["text"]
        assert rec["text"] == "赠送 人气票 x1"

    def test_parse_event_like_plain(self):
        """LIKE_INFO_V3_CLICK 明文 JSON：uid/uname/text。"""
        rec = web_client.parse_event(like_raw(103, '合成点赞用户'))
        assert rec is not None
        assert rec["type"] == "like"
        assert rec["user_hash"] == '103'
        assert rec["uname"] == '合成点赞用户'
        assert rec["text"] == "为主播点赞了"

    def test_parse_event_like_uinfo_fallback(self):
        """like 缺 data.uname 时回退 data.uinfo.base.name。"""
        body = json.dumps({
            "cmd": "LIKE_INFO_V3_CLICK",
            "data": {"uid": 999, "like_text": "点赞",
                     "uinfo": {"base": {"name": "回退昵称"}}},
        }).encode()
        rec = web_client.parse_event(body)
        assert rec["uname"] == "回退昵称"

    def test_parse_event_legacy_plaintext_fallback(self):
        """老版 INTERACT_WORD / SEND_GIFT 无 pb 时回退明文字段。"""
        e = web_client.parse_event(json.dumps({
            "cmd": "INTERACT_WORD",
            "data": {"uid": 123, "uname": "老版进场", "timestamp": 1700000000},
        }).encode())
        assert e["type"] == "enter" and e["user_hash"] == "123" and e["uname"] == "老版进场"
        g = web_client.parse_event(json.dumps({
            "cmd": "SEND_GIFT",
            "data": {"uid": 456, "uname": "老版送礼", "giftName": "辣条", "num": 2},
        }).encode())
        assert g["type"] == "gift" and g["user_hash"] == "456"
        assert g["text"] == "赠送 辣条 x2"

    def test_parse_event_entry_effect_ignored(self):
        body = json.dumps({"cmd": "ENTRY_EFFECT", "data": {}}).encode()
        assert web_client.parse_event(body) is None

    def test_parse_event_system_cmd_ignored(self):
        # 系统消息白名单内的 cmd 均返 None（不刷 WARNING）
        for cmd in ("WATCHED_CHANGE", "ONLINE_RANK_COUNT", "NOTICE_MSG",
                    "ROOM_REAL_TIME_MESSAGE_UPDATE",
                    "LIKE_INFO_V3_UPDATE", "DM_INTERACTION", "HOT_ROOM_NOTIFY"):
            body = json.dumps({"cmd": cmd, "data": {}}).encode()
            assert web_client.parse_event(body) is None, f"{cmd} 应被忽略"

    def test_parse_event_unknown_cmd_no_crash(self):
        body = json.dumps({"cmd": "SOME_NEW_CMD_XYZ", "data": {}}).encode()
        assert web_client.parse_event(body) is None

    def test_parse_event_corrupt_json_returns_none(self):
        assert web_client.parse_event(b"{broken json!!!") is None

    def test_parse_event_danmu_still_routes(self):
        rec = web_client.parse_event(danmu_json(text="主入口兼容"))
        assert rec is not None and rec["type"] == "danmaku"
        assert rec["text"] == "主入口兼容"


# ------------------------------------------------------------ _decode_pb 容错（v2.7.1）
class TestDecodePbRobustness:
    def test_decode_pb_truncated_no_raise(self):
        # 截断 bytes 不抛异常，返回已解析部分
        import base64 as _b64
        full = _b64.b64decode(ENTER_PB)
        assert web_client._decode_pb(full[:max(1, len(full) // 2)]) is not None  # 不抛

    def test_decode_pb_garbage_no_raise(self):
        assert web_client._decode_pb(b"\xff\xfe\xfd\xfc") is not None

    def test_decode_pb_empty(self):
        assert web_client._decode_pb(b"") == {}

    def test_decode_pb_invalid_b64_returns_empty(self):
        # 非法 base64 → 空 dict，不崩
        assert web_client._b64_decode_pb("!!!not-base64!!!") == {}


# ------------------------------------------------------------ wire2 嵌套段类型防护（v2.7.2）
def _pb_varint(n):
    """最小化 varint 编码（手工 protobuf bytes 夹具用）。"""
    out = b""
    while n > 0x7F:
        out += bytes([n & 0x7F | 0x80])
        n >>= 7
    return out + bytes([n])


def _pb_field(num, wire, payload):
    return _pb_varint((num << 3) | wire) + payload


def _b64enc(b):
    import base64 as _b64
    return _b64.b64encode(b).decode()


# 一个"非文本嵌套段"：seg 可 UTF-8 解为 "0*"，但无字母/CJK，_looks_like_text
# 判 False，_decode_pb 将其递归为 dict {6: [(0, 42)]}——真机脏昵称的最小复现。
_NESTED_SEG = b"\x30\x2a"


class TestPbTextSafety:
    """wire2 字段为嵌套 message(dict) 时不得把 dict repr 当文本落盘。"""

    def test_enter_nested_f2_yields_empty_uname(self):
        pb = (_pb_field(1, 0, _pb_varint(101))
              + _pb_field(2, 2, _pb_varint(len(_NESTED_SEG)) + _NESTED_SEG)
              + _pb_field(7, 0, _pb_varint(1700000000)))
        body = json.dumps({"cmd": "INTERACT_WORD_V2",
                           "data": {"pb": _b64enc(pb)}}).encode()
        rec = web_client.parse_event(body)
        assert rec is not None
        assert rec["type"] == "enter"
        assert rec["user_hash"] == '101'  # wire0 uid 不受影响
        assert rec["uname"] == ""
        assert rec["text"] == "进入直播间"
        # 落盘字段中不得出现 dict repr 痕迹
        assert "{" not in rec["uname"] and "[(" not in rec["uname"]
        assert "{" not in rec["text"] and "[(" not in rec["text"]

    def test_enter_normal_f2_uname_preserved(self):
        """对照：f2 为正常 UTF-8 中文昵称时不受误伤。"""
        seg = '合成进场用户'.encode("utf-8")
        pb = (_pb_field(1, 0, _pb_varint(12345))
              + _pb_field(2, 2, _pb_varint(len(seg)) + seg)
              + _pb_field(7, 0, _pb_varint(1700000000)))
        body = json.dumps({"cmd": "INTERACT_WORD_V2",
                           "data": {"pb": _b64enc(pb)}}).encode()
        rec = web_client.parse_event(body)
        assert rec["uname"] == '合成进场用户'

    def test_enter_masked_nickname_preserved(self):
        """打码昵称（合法 str 含星号）必须原样保留，不得误判为空。"""
        seg = "紫***".encode("utf-8")
        pb = (_pb_field(1, 0, _pb_varint(777))
              + _pb_field(2, 2, _pb_varint(len(seg)) + seg))
        body = json.dumps({"cmd": "INTERACT_WORD_V2",
                           "data": {"pb": _b64enc(pb)}}).encode()
        rec = web_client.parse_event(body)
        assert rec["uname"] == "紫***"

    def test_gift_nested_uname_and_gift_name_fallback(self):
        """f2 昵称嵌套段 → uname 空；f10.f2 礼物名嵌套段 → 回退'礼物'；
        wire0 uid 仍正确，text 无 dict repr。"""
        inner = (_pb_field(1, 0, _pb_varint(34358))
                 + _pb_field(2, 2, _pb_varint(len(_NESTED_SEG)) + _NESTED_SEG)
                 + _pb_field(3, 0, _pb_varint(1)))
        pb = (_pb_field(1, 0, _pb_varint(102))
              + _pb_field(2, 2, _pb_varint(len(_NESTED_SEG)) + _NESTED_SEG)
              + _pb_field(10, 2, _pb_varint(len(inner)) + inner))
        body = json.dumps({"cmd": "SEND_GIFT_V2",
                           "data": {"pb": _b64enc(pb)}}).encode()
        rec = web_client.parse_event(body)
        assert rec is not None
        assert rec["type"] == "gift"
        assert rec["user_hash"] == '102'
        assert rec["uname"] == ""
        assert rec["text"] == "赠送 礼物 x1"
        assert "{" not in rec["uname"] and "[(" not in rec["uname"]
        assert "{" not in rec["text"] and "[(" not in rec["text"]

    def test_legacy_plaintext_non_str_uname_no_repr(self):
        """老版明文分支：uname 非 str（异常结构）时置空，不写 repr。"""
        body = json.dumps({"cmd": "INTERACT_WORD",
                           "data": {"uid": 555, "uname": {"x": 1},
                                    "timestamp": 1700000000}}).encode()
        rec = web_client.parse_event(body)
        assert rec["uname"] == ""
        assert rec["user_hash"] == "555"


# ------------------------------------------------------------ 退避重置
class TestBackoffReset:
    def test_on_open_resets_backoff_to_one(self, tmp_path):
        collector = make_collector(tmp_path)
        # 模拟历史抖动已把退避推高
        collector._backoff = 8
        ws = MagicMock()
        collector._on_open(ws)
        assert collector._backoff == 1
        assert collector.last_reconnect_success is not None

    def test_backoff_grows_after_failure_then_resets(self, tmp_path):
        """退避在连接失败路径翻倍，连接成功后重置（纯逻辑验证，不联网）。"""
        collector = make_collector(tmp_path)
        assert collector._backoff == 1
        collector._backoff = min(collector._backoff * 2, web_client.MAX_BACKOFF)
        assert collector._backoff == 2
        collector._backoff = min(collector._backoff * 2, web_client.MAX_BACKOFF)
        assert collector._backoff == 4
        # 模拟重连成功
        ws = MagicMock()
        collector._on_open(ws)
        assert collector._backoff == 1


# ------------------------------------------------------------ op8 认证回复校验（v2.7 任务三）
def _op8_packet(body_bytes, version=1):
    """构造 op=8 认证回复 ws 包。"""
    return wrap_packet(body_bytes, version=version, operation=8)


class TestOp8AuthReply:
    def test_op8_code0_no_close(self, tmp_path):
        """code=0：放行不 close，_auth_ok_logged 置 True。"""
        collector = make_collector(tmp_path)
        ws = MagicMock()
        pkt = _op8_packet(json.dumps({"code": 0}).encode())
        collector._on_message(ws, pkt)
        ws.close.assert_not_called()
        assert collector._auth_ok_logged is True

    def test_op8_code0_logs_only_once(self, tmp_path):
        """连续两次 code=0：不 close，_auth_ok_logged 幂等（INFO 仅一次）。"""
        collector = make_collector(tmp_path)
        ws = MagicMock()
        pkt = _op8_packet(json.dumps({"code": 0}).encode())
        collector._on_message(ws, pkt)
        collector._on_message(ws, pkt)
        ws.close.assert_not_called()
        assert collector._auth_ok_logged is True

    def test_op8_code_neg101_triggers_close(self, tmp_path):
        """code=-101（token 失效典型码）：主动 close，走重连。"""
        collector = make_collector(tmp_path)
        ws = MagicMock()
        pkt = _op8_packet(json.dumps(
            {"code": -101, "message": "account denied"}).encode())
        collector._on_message(ws, pkt)
        ws.close.assert_called_once()
        assert collector._auth_ok_logged is False

    def test_op8_invalid_json_triggers_close(self, tmp_path):
        """认证回复非合法 JSON：主动 close，全程无未捕获异常。"""
        collector = make_collector(tmp_path)
        ws = MagicMock()
        pkt = _op8_packet(b"{broken auth reply!!!")
        collector._on_message(ws, pkt)
        ws.close.assert_called_once()
        assert collector._auth_ok_logged is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
