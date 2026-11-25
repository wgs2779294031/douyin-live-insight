"""飞书落盘模块（feishu_sink.py）离线测试。

全部用例零网络、零飞书调用：dry_run 后端写 tmp_path，
webhook 后端注入替身 transport，不发起任何真实 HTTP 请求。
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

from feishu_sink import (  # noqa: E402
    FeishuConfigError,
    FeishuSink,
    FeishuSinkConfig,
    build_row,
    load_feishu_config,
    run_feishu_worker,
)


# ================================================================ 夹具
def _snapshot(**over) -> dict:
    """构造一个形状与 analyzer.get_snapshot() 一致的最小快照。"""
    snap = {
        "ts": 1789657817,
        "window": {"start_ts": 1789657757, "end_ts": 1789657817, "duration_sec": 60},
        "cold_window": {
            "is_cold": False,
            "count": 12,
            "threshold_enter": 11.616025682249157,
            "threshold_exit": 17.42,
            "window_seconds": 60,
            "mode": "adaptive",
        },
        "cold_events": [
            {"event": "cold_start", "ts": 1789657447, "count": 9, "threshold": 9.52},
            {"event": "cold_end", "ts": 1789657454, "count": 12, "threshold": 11.40},
            {"event": "cold_start", "ts": 1789657720, "count": 11, "threshold": 11.61},
        ],
        "active_top": [
            {"user_id": "u1", "uname": "紫色の心情", "recent_count": 18,
             "gift_count": 0, "score": 18.0, "total_count": 77},
            {"user_id": "u2", "uname": "爱吃肉的棒男孩", "recent_count": 11,
             "gift_count": 0, "score": 11.0, "total_count": 70},
        ],
        "type_distribution": {"danmaku": 3582, "enter": 2918, "gift": 624, "like": 3056},
        "top_words": [{"word": "花木兰强吗", "count": 13}, {"word": "帅帅帅", "count": 10}],
        "representative_danmaku": [
            {"uname": "a", "text": "好帅", "ts": 1789657810, "age_sec": 7}
        ],
        "total_records": 10180,
    }
    snap.update(over)
    return snap


def _cfg(**over) -> FeishuSinkConfig:
    """默认给一个「不节流、dry_run」的测试配置，避免用例里 sleep。"""
    base = dict(enabled=True, backend="dry_run", min_interval_seconds=0.0)
    base.update(over)
    return FeishuSinkConfig(**base)


class _FakeResp:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code


class _RecordingTransport:
    """记录所有调用的替身；可按顺序返回不同状态码或抛异常。"""

    def __init__(self, results=None) -> None:
        self.calls = []
        self.results = list(results) if results else [200]

    def __call__(self, url, headers, payload, timeout):
        self.calls.append({"url": url, "headers": dict(headers),
                           "payload": json.loads(json.dumps(payload)), "timeout": timeout})
        idx = min(len(self.calls) - 1, len(self.results) - 1)
        r = self.results[idx]
        if isinstance(r, Exception):
            raise r
        return r


# ================================================================ 配置加载
class TestLoadConfig:
    def test_missing_file_returns_disabled_default(self, tmp_path):
        cfg = load_feishu_config(str(tmp_path / "nope.json"))
        assert cfg.enabled is False and cfg.backend == "dry_run"

    def test_no_feishu_section_returns_default(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text(json.dumps({"web_rid": "123"}), encoding="utf-8")
        assert load_feishu_config(str(p)).enabled is False

    def test_section_not_object_is_ignored(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text(json.dumps({"feishu": ["not", "a", "dict"]}), encoding="utf-8")
        assert load_feishu_config(str(p)).enabled is False

    def test_broken_json_returns_default(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text("{ this is not json", encoding="utf-8")
        assert load_feishu_config(str(p)).enabled is False

    def test_valid_section_loaded(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text(json.dumps({"feishu": {
            "enabled": True, "backend": "webhook",
            "webhook_url": "https://open.feishu.cn/hook/xxx",
            "bearer_token": "tok", "min_interval_seconds": 42,
            "unknown_key_should_be_ignored": 1,
        }}), encoding="utf-8")
        cfg = load_feishu_config(str(p))
        assert cfg.enabled is True
        assert cfg.backend == "webhook"
        assert cfg.webhook_url.endswith("/xxx")
        assert cfg.bearer_token == "tok"
        assert cfg.min_interval_seconds == 42
        assert not hasattr(cfg, "unknown_key_should_be_ignored")


# ================================================================ 校验
class TestValidate:
    def test_disabled_skips_all_checks(self):
        # 关闭时必须放行，保证未启用时零影响
        FeishuSinkConfig(enabled=False, backend="???", timeout_seconds=-1).validate()

    def test_unknown_backend_rejected(self):
        with pytest.raises(FeishuConfigError):
            _cfg(backend="carrier_pigeon").validate()

    def test_app_backend_ok_once_configured(self):
        # app 后端已实现（v3.2）。凭证/表都齐了就应放行，不再是「尚未实现」的保留项。
        _cfg(backend="app", app_id="cli_x", app_secret="s",
             app_token="bascnX", table_id="tblX").validate()

    def test_app_backend_missing_creds_rejected(self):
        # 只写 backend=app 而不给凭证 → 必须报错并说清缺什么，且提示去哪拿
        with pytest.raises(FeishuConfigError) as ei:
            _cfg(backend="app").validate()
        msg = str(ei.value)
        assert "app_id" in msg and "app_secret" in msg and "app_token" in msg
        assert "base" in msg          # 提示 app_token 在 /base/ 后面那段

    def test_webhook_without_url_rejected(self):
        with pytest.raises(FeishuConfigError):
            _cfg(backend="webhook", webhook_url="").validate()

    def test_webhook_with_url_ok(self):
        _cfg(backend="webhook", webhook_url="https://open.feishu.cn/hook/x").validate()

    @pytest.mark.parametrize("field,value", [
        ("timeout_seconds", 0), ("max_retries", -1), ("min_interval_seconds", -1),
    ])
    def test_numeric_bounds(self, field, value):
        with pytest.raises(FeishuConfigError):
            _cfg(**{field: value}).validate()


# ================================================================ 聚合
class TestBuildRow:
    def test_maps_snapshot_fields(self):
        row = build_row(_snapshot(), room_id="834149464025", platform="douyin")
        assert row["ts"] == 1789657817
        assert row["room_id"] == "834149464025"
        assert row["platform"] == "douyin"
        # ★ 累计值带 _cum 后缀：快照的 type_distribution 是全会话累计，
        #   不是「本窗口条数」，字段名必须能看出来
        assert row["danmaku_cum"] == 3582 and row["enter_cum"] == 2918
        assert row["gift_cum"] == 624 and row["like_cum"] == 3056
        assert row["total_records_cum"] == 10180
        assert row["cum_window_sec"] == 60
        assert row["is_cold"] == 0
        assert row["cold_count_in_window"] == 12
        assert row["cold_window_sec"] == 60
        assert row["cold_mode"] == "adaptive"
        # 阈值保留 2 位
        assert row["cold_threshold_enter"] == 11.62
        assert row["cold_threshold_exit"] == 17.42

    def test_field_set_is_stable(self):
        """行的字段集合是「飞书表结构」契约，改动必须是有意识的。

        同时守住口径命名约定：累计带 _cum、增量带 _delta、上限带 _cap。
        """
        expected = {
            "ts", "date", "room_id", "platform",
            "cum_window_start_ts", "cum_window_sec", "since_prev_sec", "cum_reset",
            "danmaku_cum", "enter_cum", "like_cum", "gift_cum", "total_records_cum",
            "danmaku_delta", "enter_delta", "like_delta", "gift_delta",
            "total_records_delta",
            "is_cold", "cold_window_sec", "cold_count_in_window",
            "cold_threshold_enter", "cold_threshold_exit", "cold_mode",
            "new_cold_events", "latest_cold_ts",
            "active_users_visible", "active_top_cap", "active_users_capped",
            "active_top", "top_words",
        }
        assert set(build_row(_snapshot())) == expected

    def test_is_cold_rendered_as_1(self):
        snap = _snapshot(cold_window={"is_cold": True, "count": 3, "mode": "fixed",
                                      "threshold_enter": 3, "threshold_exit": 6})
        assert build_row(snap)["is_cold"] == 1

    def test_top_words_and_active_top_joined_and_limited(self):
        snap = _snapshot(
            top_words=[{"word": f"w{i}", "count": i} for i in range(1, 21)],
            active_top=[{"uname": f"u{i}", "user_id": str(i), "score": 1.0}
                        for i in range(1, 11)],
        )
        row = build_row(snap, top_words_n=3, active_top_n=2)
        assert row["top_words"] == "w1(1) | w2(2) | w3(3)"
        assert row["active_top"] == "u1 | u2"
        # 可见条数 ≠ 活跃用户总数（列表已被规则层截断，本层拿不到总数）
        assert row["active_users_visible"] == 10

    def test_new_cold_events_counts_only_after_watermark(self):
        snap = _snapshot()
        # 水位在第二个事件之后 → 只应数出第 3 个（ts 1789657720）
        assert build_row(snap, prev_cold_watermark=1789657454)["new_cold_events"] == 1
        # 水位为 0 → 全部计入
        assert build_row(snap, prev_cold_watermark=0)["new_cold_events"] == 3
        # 水位最新 → 0
        assert build_row(snap, prev_cold_watermark=1789657720)["new_cold_events"] == 0

    def test_latest_cold_ts_is_max(self):
        assert build_row(_snapshot())["latest_cold_ts"] == 1789657720

    def test_empty_snapshot_does_not_crash(self):
        row = build_row({})
        assert row["room_id"] == "" and row["danmaku_cum"] == 0
        assert row["top_words"] == "" and row["active_top"] == ""
        assert row["new_cold_events"] == 0
        assert row["since_prev_sec"] == 0 and row["cum_reset"] == 0

    def test_missing_sub_dicts_do_not_crash(self):
        row = build_row({"ts": 1, "type_distribution": None, "window": None,
                         "cold_window": None, "cold_events": None})
        assert row["danmaku_cum"] == 0 and row["cum_window_sec"] == 0
        assert row["is_cold"] == 0 and row["cold_window_sec"] == 0


# ================================================================ 累计 / 增量口径
class TestCumulativeAndDelta:
    """v3.0 实测修正：快照计数是累计值，必须同时给 _cum 与 _delta。"""

    def test_first_row_deltas_are_zero(self):
        row = build_row(_snapshot(), prev_cum=None, prev_ts=0)
        assert row["danmaku_delta"] == 0 and row["total_records_delta"] == 0
        assert row["since_prev_sec"] == 0 and row["cum_reset"] == 0

    def test_delta_is_difference_against_prev(self):
        prev = {"danmaku": 3000, "enter": 2900, "like": 3000, "gift": 600,
                "total_records": 9000}
        row = build_row(_snapshot(), prev_cum=prev, prev_ts=1789657757)
        assert row["danmaku_delta"] == 3582 - 3000 == 582
        assert row["enter_delta"] == 2918 - 2900 == 18
        assert row["like_delta"] == 3056 - 3000 == 56
        assert row["gift_delta"] == 624 - 600 == 24
        assert row["total_records_delta"] == 10180 - 9000 == 1180
        assert row["since_prev_sec"] == 1789657817 - 1789657757 == 60
        # 累计值本身仍原样保留
        assert row["danmaku_cum"] == 3582
        assert row["cum_reset"] == 0

    def test_reset_is_detected_and_delta_falls_back_to_current(self):
        """规则层「时间戳跳跃」会清零累计计数 → 不能无条件相减。"""
        prev = {"danmaku": 9999, "enter": 9999, "like": 9999, "gift": 9999,
                "total_records": 99999}
        row = build_row(_snapshot(), prev_cum=prev, prev_ts=1789657757)
        assert row["cum_reset"] == 1
        # 重置后当前值本身就是本段增量
        assert row["danmaku_delta"] == 3582
        assert row["total_records_delta"] == 10180

    def test_no_reset_when_counters_unchanged_or_grown(self):
        prev = {"danmaku": 3582, "enter": 2918, "like": 3056, "gift": 624,
                "total_records": 10180}
        row = build_row(_snapshot(), prev_cum=prev)
        assert row["cum_reset"] == 0
        assert row["danmaku_delta"] == 0

    def test_active_top_cap_marks_truncation(self):
        snap = _snapshot(active_top=[{"uname": f"u{i}", "score": 1.0}
                                     for i in range(5)])
        # 可见 5 条，规则层上限也是 5 → 可能被截断
        capped = build_row(snap, active_top_cap=5)
        assert capped["active_users_visible"] == 5
        assert capped["active_top_cap"] == 5
        assert capped["active_users_capped"] == 1
        # 可见 4 条 < 上限 5 → 未截断，这 4 就是全部
        assert build_row(snap, active_top_cap=9)["active_users_capped"] == 0

    def test_active_top_cap_unknown_never_claims_truncation(self):
        row = build_row(_snapshot(active_top=[{"uname": "u", "score": 1.0}] * 5))
        assert row["active_top_cap"] == 0 and row["active_users_capped"] == 0

    def test_sink_advances_baseline_across_rows(self):
        sink = FeishuSink(_cfg(), room_id="R1", active_top_cap=5)
        r1 = sink.row_from_snapshot(_snapshot())
        r2 = sink.row_from_snapshot(_snapshot(
            ts=1789657877,
            type_distribution={"danmaku": 3600, "enter": 2930, "gift": 630, "like": 3100},
            total_records=10200,
        ))
        assert r1["danmaku_delta"] == 0            # 首行无基线
        assert r2["danmaku_delta"] == 3600 - 3582 == 18
        assert r2["since_prev_sec"] == 60
        assert r2["danmaku_cum"] == 3600          # 累计值仍原样带出
        assert r2["active_top_cap"] == 5

    def test_sink_baseline_is_per_instance(self):
        """一个房间一个实例：两个 sink 的基线互不污染。"""
        a = FeishuSink(_cfg(), room_id="A")
        b = FeishuSink(_cfg(), room_id="B")
        a.row_from_snapshot(_snapshot())          # A 先推一行
        assert b.row_from_snapshot(_snapshot())["danmaku_delta"] == 0


# ================================================================ dry_run 后端
class TestDryRunBackend:
    def test_writes_one_line_per_row(self, tmp_path):
        sink = FeishuSink(_cfg(output_dir=str(tmp_path)), room_id="R1")
        assert sink.send(sink.row_from_snapshot(_snapshot())) is True
        assert sink.send(sink.row_from_snapshot(_snapshot(ts=1789657900))) is True
        sink.close()

        files = sorted(tmp_path.glob("feishu_out_*.jsonl"))
        assert len(files) == 1
        lines = [json.loads(x) for x in files[0].read_text(encoding="utf-8").splitlines()]
        assert len(lines) == 2
        assert lines[0]["room_id"] == "R1"
        assert lines[0]["danmaku_cum"] == 3582
        assert sink.sent == 2 and sink.failed == 0

    def test_disabled_sink_sends_nothing(self, tmp_path):
        sink = FeishuSink(_cfg(enabled=False, output_dir=str(tmp_path)))
        assert sink.send(sink.row_from_snapshot(_snapshot())) is False
        assert not list(tmp_path.glob("feishu_out_*.jsonl"))

    def test_dry_run_never_touches_transport(self, tmp_path):
        t = _RecordingTransport()
        sink = FeishuSink(_cfg(output_dir=str(tmp_path)), transport=t)
        sink.send(sink.row_from_snapshot(_snapshot()))
        assert t.calls == []

    def test_cold_watermark_advances_across_snapshots(self, tmp_path):
        sink = FeishuSink(_cfg(output_dir=str(tmp_path)))
        r1 = sink.row_from_snapshot(_snapshot())                       # 3 个事件
        r2 = sink.row_from_snapshot(_snapshot(ts=1789657900))          # 无新事件
        assert r1["new_cold_events"] == 3
        assert r2["new_cold_events"] == 0
        sink.close()


# ================================================================ webhook 后端
class TestWebhookBackend:
    def test_posts_row_and_bearer_header(self):
        t = _RecordingTransport([200])
        sink = FeishuSink(
            _cfg(backend="webhook", webhook_url="https://open.feishu.cn/hook/abc",
                 bearer_token="secret-token"),
            room_id="R9", transport=t,
        )
        assert sink.send(sink.row_from_snapshot(_snapshot())) is True
        assert len(t.calls) == 1
        c = t.calls[0]
        assert c["url"] == "https://open.feishu.cn/hook/abc"
        assert c["headers"]["Authorization"] == "Bearer secret-token"
        assert c["payload"]["row"]["room_id"] == "R9"
        assert sink.sent == 1

    def test_no_bearer_header_when_token_empty(self):
        t = _RecordingTransport([200])
        sink = FeishuSink(_cfg(backend="webhook", webhook_url="https://x/hook"),
                          transport=t)
        sink.send(sink.row_from_snapshot(_snapshot()))
        assert "Authorization" not in t.calls[0]["headers"]

    def test_retries_then_gives_up(self):
        t = _RecordingTransport([500, 500, 500])
        sink = FeishuSink(
            _cfg(backend="webhook", webhook_url="https://x/hook",
                 max_retries=2, retry_backoff_seconds=0.0),
            transport=t,
        )
        assert sink.send(sink.row_from_snapshot(_snapshot())) is False
        assert len(t.calls) == 3          # 首次 + 2 次重试
        assert sink.failed == 1 and sink.sent == 0

    def test_recovers_on_second_attempt(self):
        t = _RecordingTransport([500, 200])
        sink = FeishuSink(
            _cfg(backend="webhook", webhook_url="https://x/hook",
                 max_retries=2, retry_backoff_seconds=0.0),
            transport=t,
        )
        assert sink.send(sink.row_from_snapshot(_snapshot())) is True
        assert len(t.calls) == 2 and sink.sent == 1 and sink.failed == 0

    def test_transport_exception_is_swallowed(self):
        t = _RecordingTransport([RuntimeError("boom"), 200])
        sink = FeishuSink(
            _cfg(backend="webhook", webhook_url="https://x/hook",
                 max_retries=1, retry_backoff_seconds=0.0),
            transport=t,
        )
        assert sink.send(sink.row_from_snapshot(_snapshot())) is True
        assert sink.failed == 0

    def test_send_never_raises(self):
        t = _RecordingTransport([RuntimeError("always")])
        sink = FeishuSink(
            _cfg(backend="webhook", webhook_url="https://x/hook",
                 max_retries=0, retry_backoff_seconds=0.0),
            transport=t,
        )
        assert sink.send({"ts": 1}) is False   # 不抛异常


# ================================================================ 控频
class TestThrottle:
    def test_min_interval_enforced(self, tmp_path):
        sink = FeishuSink(_cfg(output_dir=str(tmp_path), min_interval_seconds=0.25))
        start = time.time()
        for _ in range(3):
            sink.send(sink.row_from_snapshot(_snapshot()))
        elapsed = time.time() - start
        sink.close()
        # 第 1 次不等待，后 2 次各等约 0.25s
        assert elapsed >= 0.4, f"控频未生效，仅耗时 {elapsed:.3f}s"

    def test_zero_interval_does_not_sleep(self, tmp_path):
        sink = FeishuSink(_cfg(output_dir=str(tmp_path), min_interval_seconds=0.0))
        start = time.time()
        for _ in range(20):
            sink.send(sink.row_from_snapshot(_snapshot()))
        assert time.time() - start < 0.5
        sink.close()


# ================================================================ 旁路 worker
class TestWorker:
    def test_consumes_until_sentinel(self, tmp_path):
        q: "queue.Queue" = queue.Queue()
        sink = FeishuSink(_cfg(output_dir=str(tmp_path)), room_id="R1")
        th = threading.Thread(
            target=run_feishu_worker,
            kwargs={"q": q, "cfg": sink.cfg, "room_id": "R1", "sink": sink},
            daemon=True,
        )
        th.start()
        for _ in range(3):
            q.put(_snapshot())
        q.put(None)
        th.join(timeout=5)
        assert not th.is_alive()
        assert sink.sent == 3

    def test_bad_item_does_not_kill_thread(self, tmp_path):
        q: "queue.Queue" = queue.Queue()
        sink = FeishuSink(_cfg(output_dir=str(tmp_path)))
        th = threading.Thread(
            target=run_feishu_worker,
            kwargs={"q": q, "cfg": sink.cfg, "sink": sink},
            daemon=True,
        )
        th.start()
        q.put("这不是 dict")          # row_from_snapshot 会 AttributeError
        q.put(_snapshot())            # 线程应仍存活并正常处理
        q.put(None)
        th.join(timeout=5)
        assert not th.is_alive()
        assert sink.sent == 1

    def test_stop_event_exits(self, tmp_path):
        q: "queue.Queue" = queue.Queue()
        stop = threading.Event()
        sink = FeishuSink(_cfg(output_dir=str(tmp_path)))
        th = threading.Thread(
            target=run_feishu_worker,
            kwargs={"q": q, "cfg": sink.cfg, "sink": sink, "stop_event": stop},
            daemon=True,
        )
        th.start()
        stop.set()
        th.join(timeout=5)
        assert not th.is_alive()

    def test_full_queue_drops_without_blocking(self, tmp_path):
        """模拟 main.py 的投递逻辑：队列满必须立即丢弃，不阻塞上游。"""
        q: "queue.Queue" = queue.Queue(maxsize=2)
        dropped = 0

        def push(item):
            nonlocal dropped
            try:
                q.put_nowait(item)
            except queue.Full:
                dropped += 1

        for _ in range(10):
            push(_snapshot())
        assert dropped == 8
        assert q.qsize() == 2


# ================================================================ 真实配置回归
class TestProjectConfigRegression:
    def test_project_config_has_feishu_disabled(self):
        """仓库自带 config.json 必须保持 feishu 关闭，避免误触发。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cfg = load_feishu_config(os.path.join(root, "config.json"))
        assert cfg.enabled is False

    def test_example_config_parses_and_is_disabled(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "config.example.json")
        cfg = load_feishu_config(path)
        assert cfg.enabled is False
        cfg.validate()   # 关闭态必须无异常
        raw = json.loads(open(path, encoding="utf-8").read())
        assert "feishu" in raw
