"""飞书 app 后端（自建应用直写多维表格）离线测试。

全部用例零网络：`api_transport` 一律注入替身，不发起任何真实 HTTP 请求。

重点覆盖三处「只有真机才会撞到、且症状很难查」的行为：
1. **1254291 同一数据表不支持并发写** → 必须被按表串行锁串起来
   （不加锁的症状是「偶发丢行」，不是报错）
2. **99991403 / 429 当月配额用尽** → 必须立刻放弃，不能白重试 max_retries 轮
3. **超时重试的幂等键** → 同一行的 client_token 必须跨重试复用，否则插重复行
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feishu_sink import (  # noqa: E402
    _BACKENDS_IMPLEMENTED,
    _BACKENDS_RESERVED,
    FeishuApiError,
    FeishuConfigError,
    FeishuSink,
    FeishuSinkConfig,
    TenantTokenProvider,
    build_record_payload,
    load_feishu_config,
    new_client_token,
    resolve_table_id,
    row_to_fields,
    table_lock,
)

TOKEN = "t-fake-token-1234"


# ================================================================ 替身
class FakeFeishu:
    """一个假的飞书开放平台：同时充当 token 接口与新增记录接口。"""

    def __init__(self, record_results=None, token_result=None):
        self.token_calls = 0
        self.record_calls = []          # 每次新增记录的 (url, headers, payload)
        self._record_results = list(record_results or [])
        self._token_result = token_result
        self._lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0          # 曾同时进入 record 接口的最大并发数
        self.hold_seconds = 0.0

    def __call__(self, url, headers, payload, timeout):
        if url.endswith("/open-apis/auth/v3/tenant_access_token/internal"):
            self.token_calls += 1
            if self._token_result is not None:
                return self._token_result(payload, self.token_calls)
            return 200, {"code": 0, "msg": "ok",
                         "tenant_access_token": TOKEN, "expire": 7200}

        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.hold_seconds:
                time.sleep(self.hold_seconds)
            with self._lock:
                self.record_calls.append((url, headers, payload))
                idx = len(self.record_calls) - 1
                if idx < len(self._record_results):
                    return self._record_results[idx]
                return 200, {"code": 0, "msg": "ok"}
        finally:
            with self._lock:
                self.in_flight -= 1


def _cfg(**over) -> FeishuSinkConfig:
    base = dict(
        enabled=True,
        backend="app",
        app_id="cli_fake",
        app_secret="secret_fake",
        app_token="bascnFakeAppToken",
        table_id="tblFakeTable",
        min_interval_seconds=0,
        max_retries=2,
        retry_backoff_seconds=0.01,
        snapshot_interval_seconds=1,
        output_dir="data",
    )
    base.update(over)
    return FeishuSinkConfig(**base)


def _row(**over) -> dict:
    row = {
        "ts": 1789657817,
        "date": "2026-09-29",
        "room_id": "73210123456",
        "platform": "douyin",
        "danmaku_cum": 120,
        "danmaku_delta": 12,
        "is_cold": 0,
        "active_users_capped": 1,
        "active_top": "观众A | 观众B",
        "top_words": "考研(8) | 就业(5)",
        "latest_cold_ts": None,
    }
    row.update(over)
    return row


# ================================================================ 1 字段映射
class TestRowToFields:
    def test_int_str_bool_passthrough(self):
        fields = row_to_fields({"a": 1, "b": "x", "c": True, "d": False})
        assert fields == {"a": 1, "b": "x", "c": True, "d": False}
        # bool 是 int 的子类，必须保持 bool —— 被转成 0/1 会让复选框列类型不匹配
        assert isinstance(fields["c"], bool)
        assert isinstance(fields["a"], int) and not isinstance(fields["a"], bool)

    def test_none_skipped_not_written_as_null(self):
        # 不写空值：避免 1254015「字段类型和值不匹配」
        assert row_to_fields({"a": 1, "b": None}) == {"a": 1}

    def test_list_joined_with_pipe(self):
        assert row_to_fields({"words": ["a", "b", "c"]}) == {"words": "a | b | c"}

    def test_tuple_joined_too(self):
        assert row_to_fields({"words": ("a", "b")}) == {"words": "a | b"}

    def test_unknown_type_stringified(self):
        assert row_to_fields({"x": {"k": 1}}) == {"x": "{'k': 1}"}

    def test_field_names_not_renamed(self):
        # 列名必须与行字段名逐字一致 —— 飞书按名字找列，改名等于写到不存在的列
        fields = row_to_fields(_row())
        assert set(fields) == {
            "ts", "date", "room_id", "platform", "danmaku_cum", "danmaku_delta",
            "is_cold", "active_users_capped", "active_top", "top_words",
        }
        assert "latest_cold_ts" not in fields   # 值是 None → 跳过


class TestRecordPayload:
    def test_shape(self):
        payload = build_record_payload(_row(), client_token="tok-1")
        assert set(payload) == {"fields", "client_token"}
        assert payload["client_token"] == "tok-1"
        assert payload["fields"]["room_id"] == "73210123456"

    def test_no_client_token_key_when_absent(self):
        assert set(build_record_payload(_row())) == {"fields"}

    def test_new_client_token_is_uuid4(self):
        pattern = re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        )
        assert pattern.match(new_client_token())
        assert new_client_token() != new_client_token()


# ================================================================ 2 选表
class TestResolveTableId:
    def test_map_hit(self):
        cfg = _cfg(table_id="", table_id_map={"自家": "tblA", "竞品": "tblB"})
        assert resolve_table_id(cfg, "竞品") == "tblB"

    def test_default_fallback(self):
        cfg = _cfg(table_id="tblDefault", table_id_map={"自家": "tblA"})
        assert resolve_table_id(cfg, "没配的房间") == "tblDefault"

    def test_no_default_and_missing_tag_raises(self):
        # ★ 配了 map 但漏了这个房间、又没有兜底 → 必须报错，不能静默跳过
        cfg = _cfg(table_id="", table_id_map={"自家": "tblA"})
        with pytest.raises(FeishuConfigError, match="没有房间"):
            resolve_table_id(cfg, "没配的房间")

    def test_nothing_configured_raises(self):
        with pytest.raises(FeishuConfigError, match="table_id"):
            resolve_table_id(_cfg(table_id="", table_id_map={}), "任意")

    def test_empty_tag_uses_default(self):
        assert resolve_table_id(_cfg(table_id="tblDefault"), None) == "tblDefault"


# ================================================================ 3 令牌
class TestTenantTokenProvider:
    def _provider(self, fake, **over):
        kw = dict(app_id="cli_x", app_secret="sec_x", transport=fake,
                  api_base="https://open.feishu.cn", timeout=1.0,
                  refresh_margin_seconds=300.0)
        kw.update(over)
        return TenantTokenProvider(**kw)

    def test_fetch_and_cache(self):
        fake = FakeFeishu()
        p = self._provider(fake)
        assert p.get() == TOKEN
        assert p.get() == TOKEN
        # 剩余有效期 7200s > 300s 余量 → 第二次不应再请求
        assert fake.token_calls == 1
        assert p.fetch_count == 1

    def test_refresh_when_inside_margin(self):
        # expire=100s < margin 300s → 每次 get() 都会刷新
        fake = FakeFeishu(token_result=lambda payload, n: (
            200, {"code": 0, "msg": "ok", "tenant_access_token": f"t-{n}", "expire": 100}))
        p = self._provider(fake)
        assert p.get() == "t-1"
        assert p.get() == "t-2"
        assert fake.token_calls == 2

    def test_official_semantics_no_wasted_fetch(self):
        """官方：剩余 <30 分钟时调用返回新 token。所以提前 300s 刷新不会白刷。"""
        fake = FakeFeishu(token_result=lambda payload, n: (
            200, {"code": 0, "msg": "ok",
                  "tenant_access_token": "t-new" if n > 1 else "t-first", "expire": 7200}))
        p = self._provider(fake)
        assert p.get() == "t-first"
        p.invalidate()
        assert p.get() == "t-new"
        assert fake.token_calls == 2

    def test_invalidate_forces_refetch(self):
        fake = FakeFeishu()
        p = self._provider(fake)
        p.get()
        p.invalidate()
        p.get()
        assert fake.token_calls == 2

    def test_bad_credentials_not_retryable(self):
        fake = FakeFeishu(token_result=lambda payload, n: (
            200, {"code": 10003, "msg": "app_id or app_secret is invalid"}))
        p = self._provider(fake)
        with pytest.raises(FeishuApiError) as ei:
            p.get()
        assert ei.value.retryable is False     # 凭据错重试无意义
        assert ei.value.code == 10003

    def test_missing_token_in_body_not_retryable(self):
        fake = FakeFeishu(token_result=lambda payload, n: (200, {"code": 0, "msg": "ok"}))
        with pytest.raises(FeishuApiError, match="没有 token"):
            self._provider(fake).get()

    def test_expire_missing_defaults(self):
        fake = FakeFeishu(token_result=lambda payload, n: (
            200, {"code": 0, "msg": "ok", "tenant_access_token": TOKEN}))
        p = self._provider(fake)
        assert p.get() == TOKEN

    def test_http_error_not_retryable(self):
        fake = FakeFeishu(token_result=lambda payload, n: (500, {}))
        with pytest.raises(FeishuApiError) as ei:
            self._provider(fake).get()
        assert ei.value.retryable is False


# ================================================================ 4 sink 真写
class TestAppBackendWrite:
    def test_url_headers_payload(self):
        fake = FakeFeishu()
        sink = FeishuSink(_cfg(), room_id="73210123456", transport=None,
                          api_transport=fake, room_tag="竞品-就业")
        assert sink.table_id == "tblFakeTable"
        assert sink.send(_row()) is True
        assert sink.sent == 1 and sink.failed == 0

        url, headers, payload = fake.record_calls[0]
        assert url == ("https://open.feishu.cn/open-apis/bitable/v1/apps/"
                       "bascnFakeAppToken/tables/tblFakeTable/records")
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        assert headers["Content-Type"].startswith("application/json")
        assert payload["fields"]["room_id"] == "73210123456"
        assert payload["client_token"]           # 幂等键必须带上

    def test_picks_table_by_room_tag(self):
        fake = FakeFeishu()
        cfg = _cfg(table_id="", table_id_map={"A": "tblA", "B": "tblB"})
        sink_a = FeishuSink(cfg, room_tag="A", api_transport=fake)
        sink_b = FeishuSink(cfg, room_tag="B", api_transport=fake)
        assert (sink_a.table_id, sink_b.table_id) == ("tblA", "tblB")
        sink_a.send(_row())
        sink_b.send(_row())
        assert "tblA" in fake.record_calls[0][0]
        assert "tblB" in fake.record_calls[1][0]

    def test_missing_table_id_fails_at_construction(self):
        # 启动即报错，而不是运行时静默丢行
        cfg = _cfg(table_id="", table_id_map={"A": "tblA"})
        with pytest.raises(FeishuConfigError, match="没有房间"):
            FeishuSink(cfg, room_tag="没配的房间", api_transport=FakeFeishu())

    def test_token_is_cached_across_rows(self):
        fake = FakeFeishu()
        sink = FeishuSink(_cfg(), api_transport=fake)
        for _ in range(5):
            sink.send(_row())
        assert len(fake.record_calls) == 5
        assert fake.token_calls == 1           # 5 行只取 1 次 token

    def test_api_error_counts_failed_and_returns_false(self):
        fake = FakeFeishu(record_results=[
            (200, {"code": 1254004, "msg": "WrongTableId"})] * 3)
        sink = FeishuSink(_cfg(), api_transport=fake)
        assert sink.send(_row()) is False
        assert sink.failed == 1 and sink.sent == 0

    def test_never_raises(self):
        def boom(url, headers, payload, timeout):
            raise OSError("网络断了")

        sink = FeishuSink(_cfg(), api_transport=boom)
        assert sink.send(_row()) is False       # 绝不把异常抛给调用方
        assert sink.failed == 1


# ================================================================ 5 配额用尽
class TestQuotaExhausted:
    def test_429_is_not_retried(self):
        fake = FakeFeishu(record_results=[
            (200, {"code": 99991403,
                   "msg": "This month's API call quota has been exceeded"})] * 5)
        sink = FeishuSink(_cfg(max_retries=3), api_transport=fake)
        assert sink.send(_row()) is False
        # ★ 配额用尽重试无用 → 只应调用 1 次，不能白烧 4 次
        assert len(fake.record_calls) == 1
        assert sink.failed == 1

    def test_http_429_treated_as_quota(self):
        fake = FakeFeishu(record_results=[(429, {"code": None, "msg": "rate limited"})] * 5)
        sink = FeishuSink(_cfg(max_retries=3), api_transport=fake)
        assert sink.send(_row()) is False
        assert len(fake.record_calls) == 1

    def test_quota_log_is_actionable(self, caplog):
        fake = FakeFeishu(record_results=[(200, {"code": 99991403, "msg": "exceeded"})])
        sink = FeishuSink(_cfg(), api_transport=fake)
        with caplog.at_level("ERROR"):
            sink.send(_row())
        # 必须说清「怎么办」，而不是只丢一个错误码
        assert "不可重试" in caplog.text


# ================================================================ 6 重试与幂等
class TestRetryAndIdempotency:
    def test_write_conflict_is_retried(self):
        fake = FakeFeishu(record_results=[
            (200, {"code": 1254291, "msg": "Write conflict"}),
            (200, {"code": 0, "msg": "ok"}),
        ])
        sink = FeishuSink(_cfg(max_retries=2), api_transport=fake)
        assert sink.send(_row()) is True
        assert len(fake.record_calls) == 2

    def test_client_token_reused_across_retries(self):
        # ★ 超时重试若换幂等键，就会插出重复行
        fake = FakeFeishu(record_results=[
            (504, {"code": 1255040, "msg": "timeout"}),
            (504, {"code": 1255040, "msg": "timeout"}),
            (200, {"code": 0, "msg": "ok"}),
        ])
        sink = FeishuSink(_cfg(max_retries=3), api_transport=fake)
        assert sink.send(_row()) is True
        tokens = {c[2]["client_token"] for c in fake.record_calls}
        assert len(tokens) == 1, f"重试期间幂等键必须一致，实际有 {len(tokens)} 个"

    def test_different_rows_use_different_tokens(self):
        fake = FakeFeishu()
        sink = FeishuSink(_cfg(), api_transport=fake)
        sink.send(_row())
        sink.send(_row())
        assert (fake.record_calls[0][2]["client_token"]
                != fake.record_calls[1][2]["client_token"])

    def test_server_5xx_retried(self):
        fake = FakeFeishu(record_results=[(500, {}), (200, {"code": 0})])
        sink = FeishuSink(_cfg(max_retries=2), api_transport=fake)
        assert sink.send(_row()) is True
        assert len(fake.record_calls) == 2


# ================================================================ 7 令牌失效自愈
class TestTokenSelfHealing:
    def test_invalid_token_triggers_refetch_then_succeeds(self):
        state = {"n": 0}

        def token_result(payload, n):
            return 200, {"code": 0, "msg": "ok",
                         "tenant_access_token": f"t-{n}", "expire": 7200}

        def record(url, headers, payload, timeout):
            state["n"] += 1
            if state["n"] == 1:
                return 200, {"code": 99991663, "msg": "token expired"}
            return 200, {"code": 0, "msg": "ok"}

        class Tr:
            def __init__(self):
                self.tokens = FakeFeishu(token_result=token_result)

            def __call__(self, url, headers, payload, timeout):
                if url.endswith("tenant_access_token/internal"):
                    return self.tokens(url, headers, payload, timeout)
                return record(url, headers, payload, timeout)

        tr = Tr()
        sink = FeishuSink(_cfg(max_retries=0), api_transport=tr)
        assert sink.send(_row()) is True
        assert tr.tokens.token_calls == 2       # 作废缓存后重新取了 token


# ================================================================ 8 按表串行
class TestTableSerialization:
    def test_same_table_writes_are_serialized(self):
        """1254291：同一数据表不支持并发写。多房间写同一张表必须串起来。"""
        fake = FakeFeishu()
        fake.hold_seconds = 0.05
        cfg = _cfg()
        sinks = [FeishuSink(cfg, room_tag=f"房间{i}", api_transport=fake)
                 for i in range(4)]
        threads = [threading.Thread(target=s.send, args=(_row(),)) for s in sinks]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert len(fake.record_calls) == 4
        # ★ 核心断言：任何时刻进入 record 接口的都只有 1 个
        assert fake.max_in_flight == 1, f"出现并发写（峰值 {fake.max_in_flight}）"

    def test_different_tables_do_not_block_each_other(self):
        # 不同表之间不应互相拖慢：两张表各持一把锁
        assert table_lock("app", "tblA") is not table_lock("app", "tblB")
        assert table_lock("app", "tblA") is table_lock("app", "tblA")

    def test_lock_is_process_global_not_per_sink(self):
        # 两个 sink 实例指向同一张表，必须拿到同一把锁
        fake = FakeFeishu()
        cfg = _cfg()
        a = FeishuSink(cfg, room_tag="A", api_transport=fake)
        b = FeishuSink(cfg, room_tag="B", api_transport=fake)
        assert table_lock(cfg.app_token, a.table_id) is table_lock(cfg.app_token, b.table_id)


# ================================================================ 9 配置
class TestConfig:
    def test_app_is_now_implemented(self):
        assert "app" in _BACKENDS_IMPLEMENTED
        assert "app" not in _BACKENDS_RESERVED

    def test_validate_accepts_complete_app_config(self):
        _cfg().validate()          # 不抛即通过

    def test_validate_rejects_missing_credentials(self):
        with pytest.raises(FeishuConfigError, match="app_id"):
            _cfg(app_secret="").validate()

    def test_validate_rejects_missing_table(self):
        with pytest.raises(FeishuConfigError, match="table_id"):
            _cfg(table_id="", table_id_map={}).validate()

    def test_validate_accepts_table_id_map_only(self):
        _cfg(table_id="", table_id_map={"A": "tblA"}).validate()

    def test_validate_closed_still_noop(self):
        FeishuSinkConfig(enabled=False, backend="app").validate()   # 关闭时不校验

    def test_env_overrides_credentials(self, tmp_path, monkeypatch):
        p = tmp_path / "config.json"
        p.write_text(json.dumps({"feishu": {
            "enabled": True, "backend": "app",
            "app_id": "from_file", "app_secret": "from_file",
            "app_token": "from_file", "table_id": "tblX"}}), encoding="utf-8")
        monkeypatch.setenv("FEISHU_APP_ID", "from_env")
        monkeypatch.setenv("FEISHU_APP_SECRET", "from_env_secret")
        cfg = load_feishu_config(str(p))
        assert cfg.app_id == "from_env"
        assert cfg.app_secret == "from_env_secret"
        assert cfg.app_token == "from_file"      # 未提供环境变量的字段仍读文件

    def test_table_id_map_normalized_to_str(self, tmp_path):
        p = tmp_path / "config.json"
        p.write_text(json.dumps({"feishu": {
            "enabled": True, "backend": "app", "app_id": "a", "app_secret": "b",
            "app_token": "c", "table_id_map": {" 房间A ": " tbl1 "}}}), encoding="utf-8")
        cfg = load_feishu_config(str(p))
        assert cfg.table_id_map == {"房间A": "tbl1"}

    def test_bad_table_id_map_ignored_with_warning(self, tmp_path, caplog):
        p = tmp_path / "config.json"
        p.write_text(json.dumps({"feishu": {
            "enabled": True, "backend": "app", "app_id": "a", "app_secret": "b",
            "app_token": "c", "table_id": "tbl1", "table_id_map": ["not", "a", "dict"]}}),
            encoding="utf-8")
        with caplog.at_level("WARNING"):
            cfg = load_feishu_config(str(p))
        assert cfg.table_id_map == {}
        assert "table_id_map" in caplog.text


# ================================================================ 10 回归
class TestNoRegression:
    def test_dry_run_unaffected(self, tmp_path):
        cfg = _cfg(backend="dry_run", output_dir=str(tmp_path))
        sink = FeishuSink(cfg, room_tag="A")
        assert sink.send(_row()) is True
        path = tmp_path / f"feishu_out_A_{time.strftime('%Y-%m-%d')}.jsonl"
        rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 1 and rows[0]["room_id"] == "73210123456"
        assert sink.table_id == ""            # 非 app 后端不解析数据表

    def test_webhook_backend_still_works(self):
        calls = []

        def transport(url, headers, payload, timeout):
            calls.append((url, headers, payload))
            return 200

        cfg = _cfg(backend="webhook", webhook_url="https://example.com/hook")
        sink = FeishuSink(cfg, transport=transport)
        assert sink.send(_row()) is True
        # webhook 走的是「包一层 row」的老形状，不能被 app 后端改掉
        assert set(calls[0][2]) == {"row"}
