# -*- coding: utf-8 -*-
"""采集层适配器契约测试（v2.6.1 补丁任务书缺口 2：防漂移机制）。

设计要点（任务书强制约束）：
1. 参数化列表自动派生自工厂注册表 SOURCE_PLATFORM.keys()——不允许手写固定
   平台名单；新源一旦注册即自动进入契约测试；
2. FIXTURES 夹具登记与注册表强一致：注册了源但没提供夹具的直接失败
   （test_fixtures_cover_factory_registry），这是"新 adapter 绕过契约即报错"
   的落地点；
3. 对每个注册源统一断言归一化结果（必需字段 / platform 映射 / type 枚举 /
   user_id 非空 / JSON 可序列化）与 capabilities 声明（四键齐全、值为 bool）。

夹具形态（FIXTURES[name]）：
- {"kind": "ws_packet", "data": bytes, "room_id": str}
  B站：DANMU_MSG 明文 WebSocket 包，直接喂真实 adapter 的 _on_message，
  从 record_queue 取归一化后的 record（走完整 adapter 归一化路径，离线）；
- {"kind": "raw_record", "data": dict, "room_id": str}
  抖音：协议解析（parser.normalize）之后、统一归一化之前的原始 record dict
  （最小可归一化样例），经 sources.base.normalize_record 归一化——与
  client.py 在 record 构造处的调用完全相同（v2.6 唯一接入点）。

另含 v2.6.1 看板能力降级断言（验收第 3 条）。全程离线 mock，不联网，
落盘写 tmp_path。运行：pytest tests/test_contract.py -v
"""
import json
import os
import queue
import sys
import time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import dashboard  # noqa: E402  scripts/dashboard.py
from config import Config  # noqa: E402
from sources import (  # noqa: E402
    SOURCE_PLATFORM, build_adapter, get_capabilities,
    get_capabilities_by_platform,
)
from sources.base import EVENT_TYPES, normalize_record  # noqa: E402
from sources import bilibili_web as bw  # noqa: E402
from storage import JsonlStorage  # noqa: E402

# 统一事件 schema 必需字段（第 3 节契约）
REQUIRED_FIELDS = {"ts", "platform", "type", "user_id", "uname",
                   "text", "room_id", "extra"}


# ============================================================ 夹具登记
def _bili_danmu_packet(text="契约测试弹幕", uname="B站昵称",
                       user_hash="contractHash001") -> bytes:
    """构造 v1 明文 DANMU_MSG WebSocket 包（16 字节包头 + JSON）。"""
    body = json.dumps({
        "cmd": "DANMU_MSG",
        "info": [
            ["", "", "", "", "", "", "", user_hash],
            text,
            [123, uname],
        ],
    }, ensure_ascii=False).encode("utf-8")
    return bw.pack_header(16 + len(body), 16, 1, 5) + body


# Protocol fixture fields below are entirely synthetic.
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


_BILI_ENTER_PB = _fixture_enter()
_BILI_GIFT_PB = _fixture_gift(102, "合成送礼用户", "合成礼物")


def _bili_enter_packet() -> bytes:
    'INTERACT_WORD_V2 合成 pb WebSocket 包（uid=101，昵称=合成进场用户）。'
    body = json.dumps({"cmd": "INTERACT_WORD_V2", "data": {"pb": _BILI_ENTER_PB}},
                      ensure_ascii=False).encode("utf-8")
    return bw.pack_header(16 + len(body), 16, 1, 5) + body


def _bili_gift_packet() -> bytes:
    'SEND_GIFT_V2 付费礼物合成 pb WebSocket 包（uid=102，粉丝团灯牌 x1）。'
    body = json.dumps({"cmd": "SEND_GIFT_V2", "data": {"pb": _BILI_GIFT_PB}},
                      ensure_ascii=False).encode("utf-8")
    return bw.pack_header(16 + len(body), 16, 1, 5) + body


def _bili_like_packet() -> bytes:
    'LIKE_INFO_V3_CLICK 明文 WebSocket 包（uid=103，昵称=合成点赞用户）。'
    body = json.dumps({
        "cmd": "LIKE_INFO_V3_CLICK",
        "data": {"uid": 103, "uname": '合成点赞用户',
                 "like_text": "为主播点赞了"},
    }, ensure_ascii=False).encode("utf-8")
    return bw.pack_header(16 + len(body), 16, 1, 5) + body


# 每个工厂注册源必须在此登记一个原始消息样例，否则
# test_fixtures_cover_factory_registry 立即失败（防漂移强制约束）
FIXTURES = {
    "douyin_reverse": {
        # client.py parser.normalize 产出、接入统一归一化前的原始 record
        "kind": "raw_record",
        "room_id": "73210123456",
        "data": {
            "ts": 1789564843, "type": "danmaku",
            "user_id": "7001234567890123", "uname": "抖音昵称",
            "text": "主播晚上好",
        },
    },
    "bilibili_web": {
        # 真实 WebSocket op5 包，喂 BilibiliWebAdapter._on_message
        "kind": "ws_packet",
        "room_id": "21452505",
        "data": _bili_danmu_packet(),
        # v2.7.1：四类事件契约夹具（合成 pb 结构，遍历断言 type/user_id/可序列化）
        "extra_cases": [
            {"name": "enter", "packet": _bili_enter_packet(), "expected_type": "enter"},
            {"name": "gift",  "packet": _bili_gift_packet(),  "expected_type": "gift"},
            {"name": "like",  "packet": _bili_like_packet(),  "expected_type": "like"},
        ],
    },
}


def test_fixtures_cover_factory_registry():
    """工厂注册表与夹具登记表必须完全一致——注册源即强制带夹具。"""
    assert set(FIXTURES.keys()) == set(SOURCE_PLATFORM.keys()), (
        "存在已注册但未提供契约夹具的数据源："
        f"{set(SOURCE_PLATFORM.keys()) - set(FIXTURES.keys())}；"
        "新增 adapter 必须在 FIXTURES 登记原始消息样例"
    )


# ============================================================ 归一化执行
def produce_record(adapter, fixture):
    """按夹具形态走 adapter 的真实归一化路径，返回统一 record。

    - ws_packet：真实 adapter._on_message（解包→parse_danmaku→
      normalize_record→双出口），从 record_queue 取回；
    - raw_record：经 sources.base.normalize_record（与抖音 client.py 接入点
      同一函数、同一调用），room_id 取夹具声明值。
    """
    kind = fixture["kind"]
    if kind == "ws_packet":
        adapter._on_message(MagicMock(), fixture["data"])
        return adapter.record_queue.get_nowait()
    if kind == "raw_record":
        return normalize_record(adapter.platform, fixture["data"],
                                room_id=fixture["room_id"])
    raise AssertionError(f"未知夹具类型 {kind!r}，请在 produce_record 增补")


# ============================================================ 每源契约（自动派生参数化）
@pytest.mark.parametrize("name", list(SOURCE_PLATFORM.keys()))
class TestAdapterContract:
    def _build(self, name, tmp_path):
        fixture = FIXTURES[name]
        cfg = Config(source=name, web_rid=fixture["room_id"], cookie="")
        storage = JsonlStorage(str(tmp_path), platform=SOURCE_PLATFORM[name])
        adapter = build_adapter(name, cfg, storage, record_queue=queue.Queue())
        return adapter, fixture

    def test_normalized_record_schema(self, name, tmp_path):
        adapter, fixture = self._build(name, tmp_path)
        record = produce_record(adapter, fixture)

        # 必需字段齐全
        assert REQUIRED_FIELDS <= set(record.keys()), (
            f"缺字段：{REQUIRED_FIELDS - set(record.keys())}")
        # platform 与工厂映射一致
        assert record["platform"] == SOURCE_PLATFORM[name]
        # type 在四事件枚举内
        assert record["type"] in EVENT_TYPES
        # user_id 非空字符串
        assert isinstance(record["user_id"], str) and record["user_id"]
        # room_id 为字符串
        assert isinstance(record["room_id"], str) and record["room_id"]
        # JSON 可序列化（落盘/看板/LLM 全链路依赖）
        encoded = json.dumps(record, ensure_ascii=False)
        assert json.loads(encoded)["platform"] == SOURCE_PLATFORM[name]

    def test_capabilities_declared(self, name, tmp_path):
        adapter, _ = self._build(name, tmp_path)
        caps = adapter.capabilities
        # 四键齐全且值为 bool；未覆盖基类空 dict 会在这里失败
        assert set(caps.keys()) == set(EVENT_TYPES)
        assert all(isinstance(v, bool) for v in caps.values())

    def test_get_capabilities_matches_adapter(self, name, tmp_path):
        """查询函数结果与 adapter 实例声明一致（返回副本，不泄漏内部 dict）。"""
        adapter, _ = self._build(name, tmp_path)
        caps = get_capabilities(name)
        assert caps == dict(adapter.capabilities)
        caps["danmaku"] = "tampered"
        assert get_capabilities(name)["danmaku"] is True


def test_get_capabilities_unknown_raises():
    with pytest.raises(ValueError, match="未知数据源"):
        get_capabilities("not_registered_source")
    with pytest.raises(ValueError, match="未知 platform"):
        get_capabilities_by_platform("not_a_platform")


# ============================================================ v2.7：B站进场/礼物契约夹具
@pytest.mark.parametrize("case",
                         FIXTURES["bilibili_web"]["extra_cases"],
                         ids=[c["name"] for c in FIXTURES["bilibili_web"]["extra_cases"]])
def test_bilibili_extra_fixtures(case, tmp_path):
    """B站 enter/gift 夹具：走真实 _on_message 归一化路径，断言 type/user_id/可序列化。"""
    fixture = FIXTURES["bilibili_web"]
    cfg = Config(source="bilibili_web", web_rid=fixture["room_id"], cookie="")
    storage = JsonlStorage(str(tmp_path), platform="bilibili")
    adapter = build_adapter("bilibili_web", cfg, storage, record_queue=queue.Queue())
    adapter._on_message(MagicMock(), case["packet"])
    record = adapter.record_queue.get_nowait()
    assert record["type"] == case["expected_type"]
    assert record["platform"] == "bilibili"
    assert record["type"] in EVENT_TYPES
    assert isinstance(record["user_id"], str) and record["user_id"]
    assert isinstance(record["room_id"], str) and record["room_id"]
    # JSON 可序列化（落盘/看板/LLM 全链路依赖）
    encoded = json.dumps(record, ensure_ascii=False)
    assert json.loads(encoded)["type"] == case["expected_type"]


def test_only_enter_no_danmaku_cold_count_zero(tmp_path):
    """v2.7：补 enter 后冷场判定仍只由 danmaku 驱动——只喂 enter 时 count==0。
    type_distribution 能统计到 enter（活跃分层礼物加权链路依赖）。"""
    from analyzer import DanmuAnalyzer, AnalyzerConfig
    az = DanmuAnalyzer(AnalyzerConfig())
    now = int(time.time())
    for i in range(5):
        az.feed({"ts": now + i, "type": "enter", "user_id": f"u{i}",
                 "uname": f"用户{i}", "text": "进入直播间"})
    snap = az.get_snapshot()
    assert snap["cold_window"]["count"] == 0, "冷场计数只由 danmaku 驱动，enter 不计入"
    assert snap["type_distribution"].get("enter", 0) >= 5
    # 礼物加权链路：active_top 字段存在（recent_gift 经 gift_weight 加权）
    assert "active_top" in snap


# ============================================================ 验收 3：看板能力降级
def _write_platform_file(data_dir, platform, n, with_types=None):
    """用统一归一化路径造 n 条当日 record 落盘（不联网）。"""
    storage = JsonlStorage(str(data_dir), platform=platform)
    now = int(time.time())
    for i in range(n):
        raw = {"ts": now - (n - i), "type": "danmaku",
               "user_id": f"hash{i}", "uname": f"用户{i}",
               "text": f"看板契约弹幕{i}"}
        storage.write(normalize_record(platform, raw, room_id="999"))
    return storage


class TestDashboardCapabilityDegradation:
    def test_bilibili_all_capabilities(self, tmp_path):
        """B站回放：v2.7.1 四项全 True（含 LIKE_INFO_V3_CLICK 点赞流）。"""
        data_dir = tmp_path / "data_bili"
        _write_platform_file(data_dir, "bilibili", 2)
        state = dashboard.build_state(str(data_dir), time.strftime("%Y-%m-%d"))
        assert state["platform"] == "bilibili"
        assert state["danmu"]["capabilities"] == {
            "danmaku": True, "enter": True, "like": True, "gift": True}
        # 数字口径不受能力标记影响
        assert state["danmu"]["total"] == 2
        assert state["danmu"]["counts"]["danmaku"] == 2

    def test_douyin_all_supported_display_unchanged(self, tmp_path):
        """抖音回放：四项能力全 True（前端继续显示数字，口径不变）。"""
        data_dir = tmp_path / "data_dy"
        _write_platform_file(data_dir, "douyin", 2)
        state = dashboard.build_state(str(data_dir), time.strftime("%Y-%m-%d"))
        assert state["platform"] == "douyin"
        assert state["danmu"]["capabilities"] == {
            "danmaku": True, "enter": True, "like": True, "gift": True}
        assert state["danmu"]["total"] == 2

    def test_no_data_fail_open(self, tmp_path):
        """无任何落盘文件：capabilities={}，前端缺省按数字显示，不报错。"""
        state = dashboard.build_state(str(tmp_path), time.strftime("%Y-%m-%d"))
        assert state["danmu"]["capabilities"] == {}
        assert state["danmu"]["total"] == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
