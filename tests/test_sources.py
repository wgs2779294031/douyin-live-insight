# -*- coding: utf-8 -*-
"""采集层可替换适配器测试（v2.6 任务书第 7 节验收，全部离线 mock）。

覆盖验收点：
- 验收 2 工厂正确：config/命令行两种方式选型 + 未知 platform 报清晰错误
- 验收 3 B站归一化：DANMU_MSG 产出 record 满足第 3 节 schema
- 验收 4 B站双出口：storage + record_queue 同投；队列满不抛异常不阻塞
- 验收 5 B站全链路：Fake 源 → analyzer 线程 → LLM worker(Fake) → portrait
  落盘 → dashboard 识别 platform=bilibili
- 验收 6 可插拔：FakeAdapter 实现 base 接口即可接入下游
- 验收 7 无 cookie 启动：bilibili_web + cookie 为空不报配置错误
- 验收 9 看板：platform 识别（douyin 回放见 TestDashboardPlatform）

红线：全程 mock 不联网；落盘一律写 pytest tmp_path，绝不触碰 data/ 正式目录。
运行：pytest tests/test_sources.py -v
"""
import json
import os
import queue
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import dashboard  # noqa: E402  scripts/dashboard.py（纯标准库，import 无副作用）
from analyzer import AnalyzerConfig, run_analyzer  # noqa: E402
from config import Config, ConfigError, load_config  # noqa: E402
from llm_service import (  # noqa: E402
    AnchorConfig, LLMConfig, SharedState, run_llm_worker,
)
from sources import SOURCE_PLATFORM, build_adapter  # noqa: E402
from sources.base import SourceAdapter, normalize_record  # noqa: E402
from sources.bilibili_web import BilibiliWebAdapter  # noqa: E402
from sources.douyin_reverse import DouyinReverseAdapter  # noqa: E402
from storage import JsonlStorage  # noqa: E402


# ============================================================ 工具
def today() -> str:
    return time.strftime("%Y-%m-%d")


def make_cfg(**kw) -> Config:
    kw.setdefault("web_rid", "123")
    kw.setdefault("cookie", "")
    return Config(**kw)


def danmu_msg_bytes(text="你好世界", uname="昵称", user_hash="hashABC") -> bytes:
    """构造一条 v1 明文 DANMU_MSG WebSocket 包（16 字节包头 + JSON）。"""
    body = json.dumps({
        "cmd": "DANMU_MSG",
        "info": [
            ["", "", "", "", "", "", "", user_hash],
            text,
            [123, uname],
        ],
    }, ensure_ascii=False).encode("utf-8")
    from sources import bilibili_web as bw
    return bw.pack_header(16 + len(body), 16, 1, 5) + body


class FakeRouter:
    """模拟 LLMRouter：返回固定 chat 响应，probe 为空操作。"""

    def __init__(self, chat_response: str = ""):
        self._response = chat_response
        self.primary = "doubao"
        self.active = "doubao"
        self.active_model = "fake-model"

    @property
    def is_available(self) -> bool:
        return True

    def chat(self, system, user, temperature=0.7, timeout_override=None,
             on_metrics=None, purpose=""):
        return self._response

    def probe_and_maybe_switch(self):
        pass


class FakeAdapter(SourceAdapter):
    """可插拔验证用假源（验收 6）：实现 base 接口，产出固定 record。

    run() 模拟真实 adapter 的"双出口"契约：storage.write + record_queue 投递。
    未注册进工厂 —— 恰好证明未来任意新源只需实现接口即可接入下游。
    """

    def __init__(self, cfg, storage, records, record_queue=None, shared_state=None):
        super().__init__(cfg, storage, record_queue, shared_state)
        self._records = records
        self._count = 0

    def run(self) -> None:
        for rec in self._records:
            self.storage.write(rec)
            if self.record_queue is not None:
                self.record_queue.put_nowait(rec)
            self._count += 1

    def close(self) -> None:
        pass

    @property
    def msg_count(self) -> int:
        return self._count


def bili_records(n=3, room_id="999"):
    """n 条已归一化的 B站 danmaku record（ts 递增、用户不同）。"""
    out = []
    base_ts = int(time.time())
    for i in range(n):
        rec = {"ts": base_ts - (n - i), "type": "danmaku",
               "user_hash": f"hash{i}", "uname": f"用户{i}",
               "text": f"测试弹幕{i}"}
        out.append(normalize_record("bilibili", rec, room_id=room_id))
    return out


# ============================================================ 验收 2：工厂
class TestFactory:
    def test_build_douyin_reverse(self, tmp_path):
        storage = JsonlStorage(str(tmp_path), platform="douyin")
        adapter = build_adapter("douyin_reverse", make_cfg(), storage)
        assert isinstance(adapter, DouyinReverseAdapter)
        assert isinstance(adapter, SourceAdapter)
        assert adapter.platform == "douyin"

    def test_build_bilibili_web(self, tmp_path):
        storage = JsonlStorage(str(tmp_path), platform="bilibili")
        adapter = build_adapter("bilibili_web", make_cfg(), storage)
        assert isinstance(adapter, BilibiliWebAdapter)
        assert isinstance(adapter, SourceAdapter)
        assert adapter.platform == "bilibili"

    def test_unknown_name_raises_clear_error(self, tmp_path):
        storage = JsonlStorage(str(tmp_path), platform="douyin")
        with pytest.raises(ValueError, match="未知数据源"):
            build_adapter("douyin_official", make_cfg(), storage)

    def test_source_platform_map(self):
        assert SOURCE_PLATFORM == {"douyin_reverse": "douyin",
                                   "bilibili_web": "bilibili"}

    def test_config_selects_adapter(self, tmp_path):
        """config.json 的 source.platform 能选型（命令行 --source 由 main 覆盖
        cfg.source 后走同一 build_adapter，此处验配到 Config 的链路）。"""
        path = tmp_path / "config.json"
        path.write_text(json.dumps({
            "source": {"platform": "bilibili_web"}, "web_rid": "456",
        }, ensure_ascii=False), encoding="utf-8")
        cfg = load_config(str(path))
        assert cfg.source == "bilibili_web"
        storage = JsonlStorage(str(tmp_path), platform=SOURCE_PLATFORM[cfg.source])
        adapter = build_adapter(cfg.source, cfg, storage)
        assert isinstance(adapter, BilibiliWebAdapter)


# ============================================================ 验收 3：归一化 schema
class TestNormalizeRecord:
    def test_douyin_fields_exact_plus_three(self):
        """抖音：现网 5 字段完全一致 + 仅新增 platform/room_id/extra。"""
        base = {"ts": 1789564843, "type": "danmaku",
                "user_id": "u1", "uname": "昵称", "text": "hello"}
        out = normalize_record("douyin", base, room_id="42")
        assert set(out.keys()) == {
            "ts", "type", "user_id", "uname", "text", "platform", "room_id", "extra"}
        assert out["platform"] == "douyin"
        assert out["room_id"] == "42"
        assert out["extra"] == {}
        # 原 5 字段值不变
        for k, v in base.items():
            assert out[k] == v
        # 不修改入参
        assert "platform" not in base

    def test_bilibili_user_hash_maps_to_user_id(self):
        """B站：user_id 取 user_hash 值，原值保留在 extra.user_hash。"""
        rec = {"ts": 1, "type": "danmaku", "user_hash": "hashABC",
               "uname": "昵称", "text": "内容"}
        out = normalize_record("bilibili", rec, room_id="7")
        assert out["platform"] == "bilibili"
        assert out["user_id"] == "hashABC"
        assert out["extra"] == {"user_hash": "hashABC"}
        assert "user_hash" not in out
        assert out["type"] == "danmaku"

    def test_room_id_coerced_to_str(self):
        out = normalize_record("douyin", {"ts": 1, "type": "danmaku",
                                          "user_id": "u", "uname": "n", "text": "t"},
                               room_id=12345)
        assert out["room_id"] == "12345"


# ============================================================ 验收 3/4：B站 adapter
class TestBilibiliAdapter:
    def _make(self, tmp_path, record_queue=None, shared_state=None):
        storage = JsonlStorage(str(tmp_path), platform="bilibili")
        return BilibiliWebAdapter(make_cfg(), storage,
                                  record_queue=record_queue,
                                  shared_state=shared_state), storage

    def test_schema_and_dual_outlet(self, tmp_path):
        """验收 3+4：DANMU_MSG → record 进 storage 和 record_queue，schema 合规。"""
        q = queue.Queue()
        adapter, storage = self._make(tmp_path, record_queue=q)
        pkt = danmu_msg_bytes()
        adapter._on_message(MagicMock(), pkt)  # mock ws 喂入

        rec = q.get_nowait()
        assert q.empty()
        # 第 3 节 schema
        assert set(rec.keys()) == {
            "ts", "platform", "type", "user_id", "uname", "text",
            "room_id", "extra"}
        assert rec["platform"] == "bilibili"
        assert rec["user_id"] == "hashABC"
        assert rec["type"] == "danmaku"
        assert rec["text"] == "你好世界"
        assert rec["room_id"] == "123"  # 启动参数房间号
        # 落盘双出口：danmu_bilibili_*.jsonl 有且只有这一条
        path = os.path.join(str(tmp_path), f"danmu_bilibili_{today()}.jsonl")
        with open(path, "r", encoding="utf-8") as f:
            lines = [json.loads(l) for l in f if l.strip()]
        assert len(lines) == 1
        assert lines[0]["user_id"] == "hashABC"
        assert adapter.msg_count == 1

    def test_queue_full_drops_silently(self, tmp_path):
        """验收 4：队列满时不抛异常、不阻塞；落盘照常。"""
        q = queue.Queue(maxsize=1)
        q.put_nowait("filler")
        adapter, storage = self._make(tmp_path, record_queue=q)
        adapter._on_message(MagicMock(), danmu_msg_bytes())  # 不应抛 queue.Full
        assert q.get_nowait() == "filler"
        assert q.empty()
        path = os.path.join(str(tmp_path), f"danmu_bilibili_{today()}.jsonl")
        assert os.path.exists(path)  # 落盘未受队列影响
        assert adapter.msg_count == 1

    def test_record_queue_none_ok(self, tmp_path):
        """record_queue 为 None 时仅落盘，不报错（与抖音源同语义）。"""
        adapter, storage = self._make(tmp_path)
        adapter._on_message(MagicMock(), danmu_msg_bytes())
        assert adapter.msg_count == 1

    def test_resolve_room_writes_shared_state(self, tmp_path, monkeypatch):
        """短号解析成功后写 shared_state.room_id；shared_state 为 None 时跳过。"""
        from sources import bilibili_web as bw
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"data": {"room_id": 999}}
        monkeypatch.setattr(bw.requests, "get",
                            lambda *a, **k: fake_resp)

        shared = SharedState()
        adapter, _ = self._make(tmp_path, shared_state=shared)
        adapter.resolve_room_id()
        assert str(adapter.room_id) == "999"
        assert shared.room_id == "999"

        adapter2, _ = self._make(tmp_path, shared_state=None)
        adapter2.resolve_room_id()  # 不应抛异常
        assert str(adapter2.room_id) == "999"


# ============================================================ 验收 7：无 cookie 启动
class TestNoCookieBilibili:
    def test_bilibili_empty_cookie_validates(self):
        cfg = Config(source="bilibili_web", web_rid="6", cookie="")
        cfg.validate()  # 不抛异常即通过（游客模式不需要 cookie）

    def test_douyin_empty_web_rid_still_raises(self):
        cfg = Config(source="douyin_reverse", web_rid="")
        with pytest.raises(ConfigError, match="web_rid"):
            cfg.validate()

    def test_invalid_source_rejected_at_startup(self, tmp_path):
        """非法 platform：SOURCE_PLATFORM 查不到 → main 报清晰错误并退出。"""
        cfg = Config(source="douyin_official", web_rid="1")
        assert SOURCE_PLATFORM.get(cfg.source) is None
        with pytest.raises(ValueError, match="未知数据源"):
            build_adapter(cfg.source, cfg,
                          JsonlStorage(str(tmp_path), platform="douyin"))


# ============================================================ 验收 6：可插拔
class TestFakeAdapterPluggable:
    def test_fake_adapter_downstream_consumes(self, tmp_path):
        """实现 SourceAdapter 接口的任意新源接入后，下游 analyzer 正常消费。"""
        records = []
        base_ts = int(time.time())
        for i in range(3):
            rec = {"ts": base_ts - i, "type": "danmaku", "user_id": f"u{i}",
                   "uname": f"用户{i}", "text": f"hello{i}"}
            records.append(normalize_record("fake_official", rec, room_id="1"))

        storage = JsonlStorage(str(tmp_path), platform="douyin")
        q = queue.Queue()
        adapter = FakeAdapter(make_cfg(), storage, records, record_queue=q)
        snapshots = []
        t = threading.Thread(target=run_analyzer, args=(q, AnalyzerConfig()),
                             kwargs={"on_snapshot": snapshots.append,
                                     "snapshot_interval_seconds": 0},
                             daemon=True)
        t.start()
        adapter.run()
        q.put_nowait(None)
        t.join(timeout=5.0)
        assert not t.is_alive()
        assert adapter.msg_count == 3
        # 下游真实消费成功：快照里可见 3 条 danmaku 计数
        assert snapshots and snapshots[-1]["type_distribution"]["danmaku"] == 3
        # 落盘同样生效
        path = os.path.join(str(tmp_path), f"danmu_douyin_{today()}.jsonl")
        with open(path, "r", encoding="utf-8") as f:
            assert len([l for l in f if l.strip()]) == 3


# ============================================================ 验收 5/9：B站全链路 + 看板
PORTRAIT_JSON = json.dumps({
    "interest_topics": ["测试话题"], "consumption_intent": {"level": "low"},
    "activity_layers": {}, "sentiment": {"positive": 0.2, "neutral": 0.6,
                                          "negative": 0.2},
    "suggestions": [],
})


class TestBiliFullChain:
    def test_bili_records_reach_analyzer_llm_dashboard(self, tmp_path):
        """验收 5+9：B站 record → 同一 analyzer 线程 → LLM worker(Fake) →
        portrait 落盘 → dashboard.build_state 识别 platform=bilibili。"""
        data_dir = tmp_path / "data"
        storage = JsonlStorage(str(data_dir), platform="bilibili")
        records = bili_records(3, room_id="999")

        # Fake 源按真实 adapter 契约双出口投递（离线复现 bilibili_web 行为）
        q = queue.Queue()
        source = FakeAdapter(make_cfg(), storage, records, record_queue=q)

        snapshots = []
        analyzer_thread = threading.Thread(
            target=run_analyzer, args=(q, AnalyzerConfig()),
            kwargs={"on_snapshot": snapshots.append,
                    "snapshot_interval_seconds": 0},
            daemon=True)
        analyzer_thread.start()
        source.run()
        q.put_nowait(None)
        analyzer_thread.join(timeout=5.0)
        assert not analyzer_thread.is_alive()
        assert snapshots and snapshots[-1]["total_records"] == 3

        # LLM worker：FakeRouter 产出画像，落盘进同一 data 目录（与 main 接线一致）
        llm_q = queue.Queue()
        llm_q.put({"kind": "snapshot", "snapshot": snapshots[-1]})
        llm_q.put(None)
        with patch.object(LLMConfig, "build_router",
                          return_value=FakeRouter(PORTRAIT_JSON)):
            run_llm_worker(
                llm_q, LLMConfig(enabled=True,
                                 providers={"doubao": {"api_key": "k"}}),
                str(data_dir), products=[], room_id="999",
                shared_state=SharedState(), anchor_cfg=AnchorConfig())
        portrait_path = data_dir / f"portrait_{today()}.jsonl"
        assert portrait_path.exists()
        with open(portrait_path, "r", encoding="utf-8") as f:
            portrait_rows = [json.loads(l) for l in f if l.strip()]
        assert portrait_rows and portrait_rows[-1]["room_id"] == "999"

        # 看板识别：platform=bilibili、画像/弹幕数字口径正常
        state = dashboard.build_state(str(data_dir), today())
        assert state["platform"] == "bilibili"
        assert state["danmu"]["total"] == 3
        assert state["danmu"]["counts"]["danmaku"] == 3
        assert state["portrait"]["latest"]["room_id"] == "999"

    def test_douyin_replay_shows_douyin(self, tmp_path):
        """验收 9 对照组：抖音落盘回放 → platform=douyin。"""
        data_dir = tmp_path / "data"
        storage = JsonlStorage(str(data_dir), platform="douyin")
        base_ts = int(time.time())
        for i in range(2):
            rec = {"ts": base_ts - i, "type": "danmaku", "user_id": f"u{i}",
                   "uname": f"用户{i}", "text": f"hello{i}"}
            storage.write(normalize_record("douyin", rec, room_id="10001"))
        state = dashboard.build_state(str(data_dir), today())
        assert state["platform"] == "douyin"
        assert state["danmu"]["total"] == 2


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
