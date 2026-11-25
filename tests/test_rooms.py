"""多房间（v3.1）离线测试：配置解析 / 房间级开关 / 落盘分文件 / 运行时隔离。

全部用例零网络、零真实采集：适配器用替身，规则层用真实的 run_analyzer
（这样才能验证「各房间指标不互相污染」这个多房间的核心约束）。
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import room_runtime  # noqa: E402
from analyzer import AnalyzerConfig  # noqa: E402
from config import (  # noqa: E402
    Config,
    ConfigError,
    RoomConfig,
    load_config,
    load_room_configs,
    sanitize_label,
)
from feishu_sink import FeishuSinkConfig  # noqa: E402
from llm_service import LLMConfig  # noqa: E402
from room_runtime import RoomRuntime  # noqa: E402
from storage import JsonlStorage  # noqa: E402


# ================================================================ 夹具
def _write_cfg(tmp_path, payload: dict) -> str:
    p = tmp_path / "config.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(p)


def _record(room_id: str, text: str, ts: int = 1789657000, **extra) -> dict:
    rec = {
        "ts": ts,
        "platform": "douyin",
        "type": "danmaku",
        "user_id": f"u-{room_id}",
        "uname": f"用户{room_id}",
        "text": text,
        "room_id": room_id,
        "extra": {},
    }
    rec.update(extra)
    return rec


class _FakeAdapter:
    """替身适配器：往 record_queue 灌 N 条记录并落盘，然后阻塞到 close()。"""

    def __init__(self, records, storage, record_queue, block=True):
        self.records = list(records)
        self.storage = storage
        self.record_queue = record_queue
        self.msg_count = 0
        self._closed = threading.Event()
        self._block = block

    def run(self):
        for rec in self.records:
            self.storage.write(rec)
            self.msg_count += 1
            try:
                self.record_queue.put_nowait(rec)
            except queue.Full:
                pass
        if self._block:
            # 真实适配器是长连，这里用 Event 模拟「一直跑着」
            self._closed.wait(timeout=30)

    def close(self):
        self._closed.set()


def _nonempty(path) -> bool:
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def _wait_until(pred, timeout: float = 8.0, interval: float = 0.05) -> bool:
    """轮询等到 pred() 为真。

    为什么不用 time.sleep(固定值)：这台机器上并发跑测试时，2.5 秒有时候
    **不够**跨过一个快照窗口（`test_feishu_rows_split_by_room` 偶发失败过），
    而多数时候又白等 2 秒。轮询「条件成立就立刻返回」两头都治。
    超时返回 False 而不抛，让用例自己断言，失败信息里能看到实际文件状态。
    """
    deadline = time.monotonic() + timeout
    while True:
        if pred():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def _install_fake_adapter(monkeypatch, factory):
    """把 room_runtime 里的 build_adapter 换成替身。

    factory 签名统一为 `factory(storage, record_queue) -> adapter`，
    避免每个用例都要记住 build_adapter 的位置参数顺序。
    """
    def _build(source_name, cfg, storage, record_queue=None, shared_state=None,
               **kwargs):  # v3.3 起 build_adapter 会多传 monitor 等旁路参数
        return factory(storage, record_queue)

    monkeypatch.setattr(room_runtime, "build_adapter", _build)


def _make_analyzer_cfg(snapshot_interval: int = 1) -> AnalyzerConfig:
    cfg = AnalyzerConfig()
    # 让规则层尽快吐快照，避免测试等 60 秒
    for name in ("snapshot_interval_seconds",):
        if hasattr(cfg, name):
            setattr(cfg, name, snapshot_interval)
    return cfg


def _make_runtime(room, adapter_factory, tmp_path, llm_cfg=None, feishu_cfg=None,
                  name_in_file=True, index=0):
    """在真实 RoomRuntime 上打替身适配器，其余全用真件。"""
    cfg = Config(web_rid="__placeholder__", output_dir=str(tmp_path))
    rt = RoomRuntime(
        room=room,
        cfg=cfg,
        analyzer_cfg=_make_analyzer_cfg(),
        llm_cfg=llm_cfg or LLMConfig(),
        feishu_cfg=feishu_cfg or FeishuSinkConfig(),
        anchor_cfg=None,
        source_name="douyin_reverse",
        platform="douyin",
        index=index,
        name_in_file=name_in_file,
    )
    created = {}

    def _fake_build(source_name, cfg_, storage, record_queue=None, shared_state=None):
        ad = adapter_factory(room, storage, record_queue)
        created["adapter"] = ad
        created["storage"] = storage
        return ad

    return rt, _fake_build, created


# ================================================================ 1 配置解析
class TestSanitizeLabel:
    def test_keeps_safe_chars(self):
        assert sanitize_label("竞品-就业_v2") == "竞品-就业_v2"

    def test_strips_path_separators(self):
        # 落盘文件名直接用 label，绝不能带路径分隔符
        assert sanitize_label("a/b\\c:d*e") == "a_b_c_d_e"

    def test_empty_falls_back(self):
        assert sanitize_label("", "fallback") == "fallback"
        assert sanitize_label("///", "fallback") == "fallback"

    def test_all_empty_returns_empty(self):
        assert sanitize_label("", "") == ""
        assert sanitize_label(None, None) == ""

    def test_truncates_long_label(self):
        assert len(sanitize_label("x" * 200)) == 40


class TestFileTag:
    def test_label_wins(self):
        assert RoomConfig(room_id="123", label="自家-主播A").file_tag(0) == "自家-主播A"

    def test_digit_room_id_used_directly(self):
        assert RoomConfig(room_id="261378947940").file_tag(0) == "261378947940"

    def test_url_room_id_hashed(self):
        tag = RoomConfig(room_id="https://live.douyin.com/123").file_tag(2)
        assert tag.startswith("room3_")
        assert len(tag) == len("room3_") + 6
        assert "/" not in tag and ":" not in tag

    def test_url_tag_stable_for_same_input(self):
        r = RoomConfig(room_id="https://v.douyin.com/abc/")
        assert r.file_tag(0) == r.file_tag(0)

    def test_label_sanitized_in_tag(self):
        assert RoomConfig(room_id="1", label="a/b").file_tag(0) == "a_b"


class TestLoadRoomConfigs:
    def test_missing_file_returns_empty(self, tmp_path):
        assert load_room_configs(str(tmp_path / "nope.json")) == []

    def test_no_rooms_key_returns_empty(self, tmp_path):
        p = _write_cfg(tmp_path, {"web_rid": "123"})
        assert load_room_configs(p) == []

    def test_empty_rooms_returns_empty(self, tmp_path):
        p = _write_cfg(tmp_path, {"rooms": []})
        assert load_room_configs(p) == []

    def test_string_element_becomes_room(self, tmp_path):
        p = _write_cfg(tmp_path, {"rooms": ["111", "222"]})
        rooms = load_room_configs(p)
        assert [r.room_id for r in rooms] == ["111", "222"]
        # 字符串写法下三个开关都是「继承」
        assert rooms[0].llm is None and rooms[0].feishu is None

    def test_object_element_full(self, tmp_path):
        p = _write_cfg(tmp_path, {"rooms": [{
            "room_id": "111", "label": "自家", "llm": True, "feishu": False,
            "snapshot_interval_seconds": 20, "note": "主播A",
        }]})
        r = load_room_configs(p)[0]
        assert (r.room_id, r.label, r.llm, r.feishu) == ("111", "自家", True, False)
        assert r.snapshot_interval_seconds == 20
        assert r.note == "主播A"

    def test_rooms_not_a_list_raises(self, tmp_path):
        p = _write_cfg(tmp_path, {"rooms": {"room_id": "1"}})
        with pytest.raises(ConfigError, match="必须是数组"):
            load_room_configs(p)

    def test_missing_room_id_raises(self, tmp_path):
        p = _write_cfg(tmp_path, {"rooms": [{"label": "x"}]})
        with pytest.raises(ConfigError, match="缺少 room_id"):
            load_room_configs(p)

    def test_empty_string_element_raises(self, tmp_path):
        p = _write_cfg(tmp_path, {"rooms": ["  "]})
        with pytest.raises(ConfigError, match="不能为空字符串"):
            load_room_configs(p)

    def test_duplicate_room_id_raises(self, tmp_path):
        p = _write_cfg(tmp_path, {"rooms": ["111", "111"]})
        with pytest.raises(ConfigError, match="重复"):
            load_room_configs(p)

    def test_non_bool_switch_raises(self, tmp_path):
        p = _write_cfg(tmp_path, {"rooms": [{"room_id": "1", "llm": "yes"}]})
        with pytest.raises(ConfigError, match="必须是 true / false"):
            load_room_configs(p)

    def test_negative_snapshot_raises(self, tmp_path):
        p = _write_cfg(tmp_path, {"rooms": [{"room_id": "1",
                                            "snapshot_interval_seconds": -1}]})
        with pytest.raises(ConfigError, match="不能为负"):
            load_room_configs(p)

    def test_non_int_snapshot_raises(self, tmp_path):
        p = _write_cfg(tmp_path, {"rooms": [{"room_id": "1",
                                            "snapshot_interval_seconds": "soon"}]})
        with pytest.raises(ConfigError, match="必须是整数"):
            load_room_configs(p)

    def test_max_rooms_default(self):
        assert Config().max_rooms == 5

    def test_max_rooms_must_be_positive(self):
        with pytest.raises(ConfigError, match="max_rooms"):
            Config(web_rid="1", max_rooms=0).validate()


class TestValidateRequireRoom:
    def test_legacy_requires_room(self):
        with pytest.raises(ConfigError, match="web_rid"):
            Config(web_rid="").validate()

    def test_multi_room_skips_room_check(self):
        # 多房间下顶层 web_rid 可以为空（每个房间自带 room_id）
        Config(web_rid="").validate(require_room=False)

    def test_other_checks_still_run(self):
        with pytest.raises(ConfigError, match="heartbeat_interval"):
            Config(web_rid="").validate(require_room=False) if False else \
                Config(web_rid="1", heartbeat_interval=0).validate(require_room=False)


# ================================================================ 2 落盘分文件
class TestStoragePerRoom:
    def test_no_room_keeps_v30_name(self, tmp_path):
        s = JsonlStorage(str(tmp_path), platform="douyin")
        s.write(_record("1", "hi"))
        s.close()
        names = [p.name for p in tmp_path.iterdir()]
        assert names == [f"danmu_douyin_{time.strftime('%Y-%m-%d')}.jsonl"]

    def test_room_in_filename(self, tmp_path):
        s = JsonlStorage(str(tmp_path), platform="douyin", room="竞品A")
        s.write(_record("1", "hi"))
        s.close()
        names = [p.name for p in tmp_path.iterdir()]
        assert names == [f"danmu_douyin_竞品A_{time.strftime('%Y-%m-%d')}.jsonl"]

    def test_bilibili_platform(self, tmp_path):
        s = JsonlStorage(str(tmp_path), platform="bilibili", room="77")
        s.write(_record("77", "hi"))
        s.close()
        names = [p.name for p in tmp_path.iterdir()]
        assert names == [f"danmu_bilibili_77_{time.strftime('%Y-%m-%d')}.jsonl"]

    def test_blank_room_treated_as_none(self, tmp_path):
        s = JsonlStorage(str(tmp_path), platform="douyin", room="   ")
        assert s.room is None

    def test_current_path_none_before_write(self, tmp_path):
        s = JsonlStorage(str(tmp_path), platform="douyin", room="A")
        assert s.current_path is None
        s.write(_record("A", "hi"))
        assert s.current_path and s.current_path.endswith(".jsonl")
        s.close()

    def test_two_rooms_do_not_share_file(self, tmp_path):
        a = JsonlStorage(str(tmp_path), platform="douyin", room="A")
        b = JsonlStorage(str(tmp_path), platform="douyin", room="B")
        a.write(_record("A", "from-A"))
        b.write(_record("B", "from-B"))
        a.close()
        b.close()
        lines_a = [json.loads(x) for x in open(a.current_path, encoding="utf-8")]
        lines_b = [json.loads(x) for x in open(b.current_path, encoding="utf-8")]
        assert [r["text"] for r in lines_a] == ["from-A"]
        assert [r["text"] for r in lines_b] == ["from-B"]


# ================================================================ 3 房间级开关
class TestRoomSwitches:
    def _rt(self, room, tmp_path, llm_cfg=None, feishu_cfg=None, **kw):
        cfg = Config(web_rid="__ph__", output_dir=str(tmp_path))
        return RoomRuntime(
            room=room, cfg=cfg, analyzer_cfg=AnalyzerConfig(),
            llm_cfg=llm_cfg or LLMConfig(), feishu_cfg=feishu_cfg or FeishuSinkConfig(),
            anchor_cfg=None, source_name="douyin_reverse", platform="douyin",
            index=0, **kw,
        )

    def test_inherit_when_none(self, tmp_path):
        llm = LLMConfig()
        llm.enabled = True
        fei = FeishuSinkConfig()
        fei.enabled = True
        rt = self._rt(RoomConfig(room_id="1"), tmp_path, llm_cfg=llm, feishu_cfg=fei)
        # 继承全局：LLM 的 is_active 还要看 api_key，这里只断言 feishu 与 llm 的收窄方向
        assert rt.feishu_enabled is True
        assert isinstance(rt.llm_enabled, bool)

    def test_room_false_narrows_global_true(self, tmp_path):
        llm = LLMConfig()
        llm.enabled = True
        fei = FeishuSinkConfig()
        fei.enabled = True
        rt = self._rt(RoomConfig(room_id="1", llm=False, feishu=False), tmp_path,
                      llm_cfg=llm, feishu_cfg=fei)
        assert rt.llm_enabled is False
        assert rt.feishu_enabled is False

    def test_room_true_cannot_widen_global_false(self, tmp_path, caplog):
        llm = LLMConfig()          # enabled 默认 False
        fei = FeishuSinkConfig()   # enabled 默认 False
        with caplog.at_level("WARNING"):
            rt = self._rt(RoomConfig(room_id="1", llm=True, feishu=True), tmp_path,
                          llm_cfg=llm, feishu_cfg=fei)
        assert rt.llm_enabled is False
        assert rt.feishu_enabled is False
        # 必须留下 WARNING，否则就是「房间说开了、其实没开」的假成功
        text = caplog.text
        assert "llm=true" in text and "feishu=true" in text

    def test_resolve_helpers(self):
        assert RoomConfig(room_id="1").resolve_llm(True) is True
        assert RoomConfig(room_id="1").resolve_llm(False) is False
        assert RoomConfig(room_id="1", llm=False).resolve_llm(True) is False
        assert RoomConfig(room_id="1", feishu=True).resolve_feishu(False) is True
        assert RoomConfig(room_id="1").resolve_feishu(True) is True


# ================================================================ 4 运行时隔离
class TestRuntimeIsolation:
    def test_per_room_private_resources(self, tmp_path):
        cfg = Config(web_rid="__ph__", output_dir=str(tmp_path))
        rts = [
            RoomRuntime(room=RoomConfig(room_id=str(100 + i), label=f"R{i}"),
                        cfg=cfg, analyzer_cfg=AnalyzerConfig(),
                        llm_cfg=LLMConfig(), feishu_cfg=FeishuSinkConfig(),
                        anchor_cfg=None, source_name="douyin_reverse",
                        platform="douyin", index=i)
            for i in range(3)
        ]
        # 队列 / 状态 / 存储 全部是独立对象
        assert len({id(rt.record_queue) for rt in rts}) == 3
        assert len({id(rt.shared_state) for rt in rts}) == 3
        assert len({id(rt.storage) for rt in rts}) == 3
        # 每个房间的 Config 只换了 web_rid，其余共用同一份只读配置值
        assert [rt.cfg.web_rid for rt in rts] == ["100", "101", "102"]
        assert len({id(rt.cfg) for rt in rts}) == 3
        # 互不共享可变容器
        rts[0]._snapshot_fanout.append(lambda s: None)
        assert rts[1]._snapshot_fanout == []

    def test_callbacks_with_no_downstream_still_log_cold_events(self, tmp_path):
        # 零下游时：快照回调为 None（省一次序列化）；但冷场事件**必须**落盘 ——
        # 「冷场次数」不能依赖有没有开 LLM（2026-10-26，冤种小徐 10-25 那次
        # 就是没开 LLM，复盘页冷场恒 0）。
        cfg = Config(web_rid="__ph__", output_dir=str(tmp_path))
        rt = RoomRuntime(room=RoomConfig(room_id="1"), cfg=cfg,
                         analyzer_cfg=AnalyzerConfig(), llm_cfg=LLMConfig(),
                         feishu_cfg=FeishuSinkConfig(), anchor_cfg=None,
                         source_name="douyin_reverse", platform="douyin")
        on_event, on_snapshot = rt._build_callbacks()
        assert on_snapshot is None
        assert on_event is not None
        # 成对发：cold_start 现在是暂存到 cold_end 才写的（要算时长过滤抖动）
        on_event({"event": "cold_start", "ts": 1000, "count": 0, "threshold": 0.5})
        on_event({"event": "cold_end", "ts": 1120, "count": 1, "threshold": 1.0})
        rt._cold_event_log.close()
        p = tmp_path / ("cold_events_%s.jsonl" % time.strftime("%Y-%m-%d"))
        assert p.exists() and p.stat().st_size > 0

    def test_snapshot_interval_takes_min(self, tmp_path):
        cfg = Config(web_rid="__ph__", output_dir=str(tmp_path))
        fei = FeishuSinkConfig()
        fei.snapshot_interval_seconds = 15
        rt = RoomRuntime(room=RoomConfig(room_id="1", snapshot_interval_seconds=5),
                         cfg=cfg, analyzer_cfg=AnalyzerConfig(), llm_cfg=LLMConfig(),
                         feishu_cfg=fei, anchor_cfg=None,
                         source_name="douyin_reverse", platform="douyin")
        rt.feishu_enabled = True
        rt._wire_feishu()
        # 飞书要 15s、房间要 5s → 取 5s（保证每个下游都够用）
        assert rt.snapshot_interval() == 5

    def test_snapshot_interval_default_without_downstream(self, tmp_path):
        cfg = Config(web_rid="__ph__", output_dir=str(tmp_path))
        rt = RoomRuntime(room=RoomConfig(room_id="1"), cfg=cfg,
                         analyzer_cfg=AnalyzerConfig(), llm_cfg=LLMConfig(),
                         feishu_cfg=FeishuSinkConfig(), anchor_cfg=None,
                         source_name="douyin_reverse", platform="douyin")
        assert rt.snapshot_interval() == room_runtime.DEFAULT_SNAPSHOT_INTERVAL


class TestRuntimeEndToEnd:
    """真 run_analyzer + 替身适配器：验证 N 个房间的数据各走各的通道。"""

    def _run_two_rooms(self, tmp_path, monkeypatch, feishu_enabled=True,
                       wait: str = "feishu", wait_timeout: float = 8.0,
                       settle: float = 2.5):
        """起两个房间跑一会儿再停。

        :param wait: "feishu" 等到两个飞书文件都有行；"danmu" 等到弹幕文件各有
                     6 条；None 则固定等 `settle` 秒（验证「什么都没产生」时用，
                     必须真的跨过快照窗口，否则断言不成立）。
        """
        fei = FeishuSinkConfig()
        fei.enabled = feishu_enabled
        fei.output_dir = str(tmp_path)
        fei.snapshot_interval_seconds = 1
        fei.min_interval_seconds = 0.0      # 测试里不控频

        # ★ 替身只装一次：按「存储对象所属房间」分发数据。
        #   若在循环里反复 setattr，后装的会覆盖先装的，两个房间会拿到同一批记录
        #   ——那样测试就永远发现不了「房间之间串数据」的 bug。
        rec_by_tag = {
            "A": [_record("A", f"考研 第{n}条", ts=1789657000 + n) for n in range(6)],
            "B": [_record("B", f"考公 第{n}条", ts=1789657000 + n) for n in range(6)],
        }
        adapters = {}

        def _factory(storage, rq):
            tag = storage.room
            assert tag in rec_by_tag, f"替身拿到未知房间标识 {tag!r}"
            ad = _FakeAdapter(rec_by_tag[tag], storage, rq)
            adapters[tag] = ad
            return ad

        _install_fake_adapter(monkeypatch, _factory)

        rts = []
        for i, rid in enumerate(("A", "B")):
            room = RoomConfig(room_id=rid, label=rid, snapshot_interval_seconds=1)
            cfg = Config(web_rid="__ph__", output_dir=str(tmp_path))
            rts.append(RoomRuntime(
                room=room, cfg=cfg, analyzer_cfg=_make_analyzer_cfg(),
                llm_cfg=LLMConfig(), feishu_cfg=fei, anchor_cfg=None,
                source_name="douyin_reverse", platform="douyin",
                index=i, name_in_file=True,
            ))

        date = time.strftime("%Y-%m-%d")
        feishu_paths = [tmp_path / ("feishu_out_%s_%s.jsonl" % (t, date))
                        for t in ("A", "B")]
        danmu_paths = [tmp_path / ("danmu_douyin_%s_%s.jsonl" % (t, date))
                       for t in ("A", "B")]

        def _ready() -> bool:
            if wait == "feishu":
                return all(_nonempty(p) for p in feishu_paths)
            if wait == "danmu":
                return all(_nonempty(p)
                           and len(p.read_text(encoding="utf-8").splitlines()) >= 6
                           for p in danmu_paths)
            return True

        for rt in rts:
            rt.start()
        try:
            if wait in ("feishu", "danmu"):
                _wait_until(_ready, timeout=wait_timeout)
            else:
                time.sleep(settle)   # 至少跨过一个 1 秒快照窗口
        finally:
            for rt in rts:
                rt.stop()

        return rts, adapters

    def test_records_do_not_cross_rooms(self, tmp_path, monkeypatch):
        # 只验弹幕落盘（写入即 flush），不必等快照窗口
        rts, _ = self._run_two_rooms(tmp_path, monkeypatch, wait="danmu")
        files = {p.name: p for p in tmp_path.glob("danmu_douyin_*.jsonl")}
        assert set(files) == {
            f"danmu_douyin_A_{time.strftime('%Y-%m-%d')}.jsonl",
            f"danmu_douyin_B_{time.strftime('%Y-%m-%d')}.jsonl",
        }
        for tag, word in (("A", "考研"), ("B", "考公")):
            path = tmp_path / f"danmu_douyin_{tag}_{time.strftime('%Y-%m-%d')}.jsonl"
            recs = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
            assert len(recs) == 6
            # ★ 核心断言：A 房间的文件里绝不能出现 B 房间的内容
            assert all(word in r["text"] for r in recs)
            assert all(r["room_id"] == tag for r in recs)

    def test_feishu_rows_split_by_room(self, tmp_path, monkeypatch):
        self._run_two_rooms(tmp_path, monkeypatch)
        files = sorted(p.name for p in tmp_path.glob("feishu_out_*.jsonl"))
        assert files == [f"feishu_out_A_{time.strftime('%Y-%m-%d')}.jsonl",
                         f"feishu_out_B_{time.strftime('%Y-%m-%d')}.jsonl"]
        for tag in ("A", "B"):
            path = tmp_path / f"feishu_out_{tag}_{time.strftime('%Y-%m-%d')}.jsonl"
            rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
            assert rows, "每个房间都应至少落一行"
            assert all(r["room_id"] == tag for r in rows)

    def test_no_feishu_when_disabled(self, tmp_path, monkeypatch):
        # 验证「什么都没有」→ 必须真的等够一个快照窗口，不能用轮询提前退出
        self._run_two_rooms(tmp_path, monkeypatch, feishu_enabled=False, wait=None)
        assert list(tmp_path.glob("feishu_out_*.jsonl")) == []

    def test_single_room_filename_unchanged(self, tmp_path, monkeypatch):
        """legacy 单房间：name_in_file=False → 落盘文件名与 v3.0 完全一致。"""
        fei = FeishuSinkConfig()
        fei.enabled = False
        cfg = Config(web_rid="__ph__", output_dir=str(tmp_path))
        rt = RoomRuntime(
            room=RoomConfig(room_id="261378947940"), cfg=cfg,
            analyzer_cfg=_make_analyzer_cfg(), llm_cfg=LLMConfig(),
            feishu_cfg=fei, anchor_cfg=None, source_name="douyin_reverse",
            platform="douyin", index=0, name_in_file=False,
        )

        def _factory(storage, rq):
            return _FakeAdapter([_record("261378947940", "hi")], storage, rq)

        _install_fake_adapter(monkeypatch, _factory)
        rt.start()
        time.sleep(0.4)
        rt.stop()
        names = [p.name for p in tmp_path.glob("danmu_douyin_*.jsonl")]
        assert names == [f"danmu_douyin_{time.strftime('%Y-%m-%d')}.jsonl"]


class TestRuntimeLifecycle:
    def _rt(self, tmp_path, monkeypatch):
        cfg = Config(web_rid="__ph__", output_dir=str(tmp_path))
        rt = RoomRuntime(room=RoomConfig(room_id="X", label="X"), cfg=cfg,
                         analyzer_cfg=_make_analyzer_cfg(), llm_cfg=LLMConfig(),
                         feishu_cfg=FeishuSinkConfig(), anchor_cfg=None,
                         source_name="douyin_reverse", platform="douyin",
                         index=0, name_in_file=True)
        _install_fake_adapter(monkeypatch,
                              lambda storage, rq: _FakeAdapter([], storage, rq))
        return rt

    def test_start_is_idempotent(self, tmp_path, monkeypatch):
        rt = self._rt(tmp_path, monkeypatch)
        rt.start()
        first_thread = rt._adapter_thread
        rt.start()          # 再调一次不应重复起线程
        assert rt._adapter_thread is first_thread
        rt.stop()

    def test_stop_is_idempotent(self, tmp_path, monkeypatch):
        rt = self._rt(tmp_path, monkeypatch)
        rt.start()
        rt.stop()
        rt.stop()           # 重复 stop 不应抛异常
        assert rt._stopped is True
        assert rt.is_alive() is False

    def test_adapter_error_recorded(self, tmp_path, monkeypatch):
        """采集线程异常必须被记下来——否则房间「看起来在跑」其实已死。"""
        cfg = Config(web_rid="__ph__", output_dir=str(tmp_path))
        rt = RoomRuntime(room=RoomConfig(room_id="X", label="X"), cfg=cfg,
                         analyzer_cfg=_make_analyzer_cfg(), llm_cfg=LLMConfig(),
                         feishu_cfg=FeishuSinkConfig(), anchor_cfg=None,
                         source_name="douyin_reverse", platform="douyin",
                         index=0, name_in_file=True)

        class _Boom:
            msg_count = 0
            def run(self):
                raise RuntimeError("握手失败")
            def close(self):
                pass

        _install_fake_adapter(monkeypatch, lambda storage, rq: _Boom())
        rt.start()
        rt._adapter_thread.join(timeout=2.0)
        assert isinstance(rt.adapter_error, RuntimeError)
        assert "握手失败" in str(rt.adapter_error)
        rt.stop()
