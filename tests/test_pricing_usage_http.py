"""Real authenticated loopback Board; price/query paths never construct an SDK."""
from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import socket
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import assist_board_server as board
import board_auth as auth
import llm_pricing

DATE = "2026-11-15"


@pytest.fixture
def server(tmp_path, monkeypatch):
    config, data = tmp_path / "config.json", tmp_path / "data"
    data.mkdir()
    raw = {"llm": {"enabled": False, "daily_token_budget": 100, "active_provider": "demo",
        "providers": {"demo": {"api_key": "synthetic-private-api-secret",
            "model": "model-a", "base_url": "https://example.invalid/v1"}}}, "untouched": True}
    config.write_text(json.dumps(raw), encoding="utf-8")
    gate = data / "llm_runtime.json"
    gate.write_text('{"enabled":false,"pause_generation":9}', encoding="utf-8")
    sdk_calls = []
    module = ModuleType("openai")
    def denied_sdk(*args, **kwargs):
        sdk_calls.append(True)
        raise AssertionError("read/price paths must not construct an SDK")
    module.OpenAI = denied_sdk
    monkeypatch.setitem(sys.modules, "openai", module)
    class H(board.Handler):
        auth_db = {"secret": "synthetic-auth-secret", "users": [{"name": "tester"}]}
        config_path = str(config)
        store = SimpleNamespace(data_dir=str(data), today=lambda: DATE)
        def log_message(self, *_args):
            pass
    httpd = board.ThreadingHTTPServer(("127.0.0.1", 0), H)
    worker = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01})
    worker.start()
    cookie = auth.cookie_header(auth.make_token("tester", H.auth_db["secret"])).split(";", 1)[0]
    connect = socket.create_connection
    def loopback_only(address, *args, **kwargs):
        assert address[0] == "127.0.0.1", "external I/O forbidden"
        return connect(address, *args, **kwargs)
    monkeypatch.setattr(socket, "create_connection", loopback_only)
    def request(method, path, body=None, *, authorized=True, origin=None):
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["Cookie"] = cookie
        if origin:
            headers["Origin"] = origin
        try:
            conn.request(method, path, json.dumps(body) if body is not None else None, headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()
    yield SimpleNamespace(request=request, config=config, data=data, gate=gate, sdk_calls=sdk_calls, handler=H)
    httpd.shutdown()
    httpd.server_close()
    worker.join(2)
    assert not worker.is_alive() and sdk_calls == []


def price(**fields):
    return dict({"provider": "demo", "model": "model-a", "currency": "CNY",
                 "input_per_million": .125, "output_per_million": 2.5}, **fields)


def test_price_save_clear_and_read_do_not_enable_or_change_credentials(server):
    before = json.loads(server.config.read_text())
    gate = server.gate.read_bytes()
    assert server.request("GET", "/api/llm/prices")[1]["entries"] == []
    code, saved = server.request("POST", "/api/llm/prices", price())
    assert code == 200 and saved["config_saved"] and saved["entry"]["source"] == "user_entered"
    assert "synthetic-private-api-secret" not in json.dumps(saved)
    now = json.loads(server.config.read_text())
    now["llm"].pop("cost_estimates")
    assert now == before and server.gate.read_bytes() == gate
    assert not list(server.data.glob("llm_budget*"))
    assert server.request("POST", "/api/llm/prices", price(currency="", input_per_million=None,
                                                             output_per_million=None))[1]["entry"] is None
    assert server.request("GET", "/api/llm/prices")[1]["entries"] == []


@pytest.mark.parametrize("key", [None, ""])
def test_no_key_no_ledger_status_is_readable_and_does_not_claim_zero_cost(server, key):
    raw = json.loads(server.config.read_text())
    if key is None:
        raw["llm"]["providers"]["demo"].pop("api_key")
    else:
        raw["llm"]["providers"]["demo"]["api_key"] = key
    server.config.write_text(json.dumps(raw), encoding="utf-8")
    code, status = server.request("GET", "/api/llm")
    assert code == 200 and status["today"]["total_tokens"] == 0
    assert status["today"]["coverage"]["status"] == "no_records"
    assert status["today"]["cost_cny"] is None and status["today"]["estimates"] == []
    assert status["control"]["allowed"] is False


@pytest.mark.parametrize("amount", [-1, True, "0.5", float("nan"), float("inf"), {}])
def test_bad_prices_have_no_write_or_secret_echo(server, amount):
    before = server.config.read_bytes()
    code, response = server.request("POST", "/api/llm/prices", price(input_per_million=amount))
    assert code == 400 and server.config.read_bytes() == before
    assert "synthetic-private-api-secret" not in json.dumps(response)


@pytest.mark.parametrize("authorized,origin,code", [(False, None, 401),
    (True, "https://cross-site.invalid", 403)])
def test_auth_and_origin_precede_price_write(server, authorized, origin, code):
    before = server.config.read_bytes()
    assert server.request("POST", "/api/llm/prices", price(), authorized=authorized, origin=origin)[0] == code
    assert server.config.read_bytes() == before


@pytest.mark.parametrize("cap", [0, 1, 5000, 100000])
def test_token_cap_is_direct_and_preserves_existing_ledger_and_gate(server, cap):
    ledger = server.data / f"llm_budget_{DATE}.json"
    ledger.write_text(json.dumps({"date": DATE, "total": 7, "budget": 100}), encoding="utf-8")
    before, gate = ledger.read_bytes(), server.gate.read_bytes()
    code, result = server.request("POST", "/api/llm/budget", {"cap_tokens": cap})
    assert code == 200 and result["budget"]["config_cap_tokens"] == cap
    assert result["budget"]["cap_tokens"] == 100 and result["budget"]["used_tokens"] == 7
    assert result["budget"]["pending"] is (cap != 100)
    assert ledger.read_bytes() == before and server.gate.read_bytes() == gate
    assert json.loads(server.config.read_text())["llm"]["daily_token_budget"] == cap


@pytest.mark.parametrize("cap", [True, "10000", 1.5, -1, float("inf")])
def test_invalid_cap_is_rejected_without_truncation(server, cap):
    before = server.config.read_bytes()
    assert server.request("POST", "/api/llm/budget", {"cap_tokens": cap})[0] == 400
    assert server.config.read_bytes() == before


def test_yesterdays_material_today_usage_keeps_saved_price_after_edit(server):
    old = server.request("POST", "/api/llm/prices", price())[1]["entry"]
    stamp = datetime(2026, 11, 15, 12, tzinfo=timezone(timedelta(hours=8))).timestamp()
    task_id = "bf_" + "a" * 64 + "_" + "b" * 32
    record = {"metrics_version": 2, "metric_id": "1" * 32, "ts": stamp,
        "provider": "demo", "model": "model-a", "room_tag": "synthetic", "purpose": "review",
        "data_date": "2026-11-14", "task_id": task_id, "sent": True, "success": True,
        "sdk_attempts": 1, "unknown_attempts": 0, "usage_coverage": "complete",
        "prompt_tokens": 10, "completion_tokens": 2, "prompt_tokens_known": True,
        "completion_tokens_known": True, "sdk_attempt_usage": [{"index": 1, "ts": stamp,
            "response_at": stamp + .5, "prompt_tokens": 10, "completion_tokens": 2,
            "price_snapshot": old}]}
    # Filename is deliberately the material day, never the call-date authority.
    (server.data / "llm_metrics_2026-11-14.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    server.request("POST", "/api/llm/prices", price(input_per_million=999, output_per_million=999))
    code, result = server.request("GET", "/api/llm/usage?call_date=2026-11-15&tag=synthetic")
    assert code == 200 and result["scope"]["call_date"] == DATE
    assert result["totals"]["known_prompt_tokens"] == 10
    assert result["estimates"][0]["amount"] == "0.00000625"
    task = server.request("GET", "/api/llm/usage?task_id=" + task_id)[1]
    assert task["totals"]["known_completion_tokens"] == 2
    yesterday = server.request("GET", "/api/llm/usage?call_date=2026-11-14")[1]
    assert yesterday["totals"]["metric_records"] == 0
    assert server.gate.read_text() == '{"enabled":false,"pause_generation":9}'


@pytest.mark.parametrize("query", ["call_date=2026-02-30", "task_id=../../escape", "purpose=bad!", "extra=1"])
def test_usage_query_rejects_invalid_scope(server, query):
    assert server.request("GET", "/api/llm/usage?" + query)[0] == 400


def usage_record(**fields):
    stamp = datetime(2026, 11, 15, 12).timestamp()
    return dict({"metrics_version": 2, "metric_id": "a" * 32, "ts": stamp,
        "provider": "demo", "model": "model-a", "room_tag": "synthetic-room",
        "purpose": "review", "data_date": "2026-11-14", "sent": True, "success": True,
        "sdk_attempts": 1, "sdk_attempt_usage": [{"ts": stamp, "response_at": stamp + 1,
            "prompt_tokens": 2, "completion_tokens": 1, "price_snapshot": None}]}, **fields)


@pytest.mark.parametrize("route", ["usage", "review"])
@pytest.mark.parametrize("identity", ["provider", "model"])
@pytest.mark.parametrize("source", ["current", "legacy", "remembered"])
def test_public_usage_redacts_all_known_secret_sources(server, route, identity, source):
    from llm_client import canonical_llm_source, source_secret_set
    secret = "synthetic-private-api-secret"
    raw = json.loads(server.config.read_text())
    if source != "current":
        raw["llm"]["providers"]["demo"]["api_key"] = "synthetic-current-other-key"
        if source == "legacy":
            raw["llm"]["key"] = secret
        else:
            source_secret_set(canonical_llm_source(str(server.config))).add(secret)
        server.config.write_text(json.dumps(raw), encoding="utf-8")
    metric = server.data / "llm_metrics_2026-11-15.jsonl"
    metric.write_text(json.dumps(usage_record(**{identity: "prefix-" + secret + "-suffix"})) + "\n",
                      encoding="utf-8")
    before = metric.read_bytes()
    path = ("/api/llm/usage?tag=synthetic-room&data_date=2026-11-14" if route == "usage"
            else "/api/review?tag=synthetic-room&date=2026-11-14")
    code, response = server.request("GET", path)
    assert code == 200 and secret not in json.dumps(response)
    usage = response["llm"] if route == "review" else response
    assert usage["totals"]["known_prompt_tokens"] == 2
    assert usage["by_provider"][0][identity] == ""
    assert metric.read_bytes() == before


def test_cached_review_uses_fresh_secrets_without_mutating_cached_usage(server, monkeypatch):
    from llm_client import canonical_llm_source, source_secret_set
    future_secret = "synthetic-rotated-secret"
    old_secret = "synthetic-private-api-secret"
    metric = server.data / "llm_metrics_2026-11-15.jsonl"
    metric.write_text(json.dumps(usage_record(provider=future_secret, model=old_secret)) + "\n",
                      encoding="utf-8")
    before = metric.read_bytes()
    calls = []
    original_read = board.review.read_usage
    def counted_read(*args, **kwargs):
        calls.append(True)
        return original_read(*args, **kwargs)
    monkeypatch.setattr(board.review, "read_usage", counted_read)
    path = "/api/review?tag=synthetic-room&date=2026-11-14"
    code, first = server.request("GET", path)
    assert code == 200 and future_secret in json.dumps(first) and old_secret not in json.dumps(first)
    cached = board.review.review_tag(str(server.data), "2026-11-14", "synthetic-room")
    assert cached["llm"]["by_provider"][0]["model"] == old_secret
    source_secret_set(canonical_llm_source(str(server.config))).add(old_secret)
    raw = json.loads(server.config.read_text())
    raw["llm"]["providers"]["demo"]["api_key"] = future_secret
    server.config.write_text(json.dumps(raw), encoding="utf-8")
    for _ in range(2):
        code, response = server.request("GET", path)
        assert code == 200
        assert all(secret not in json.dumps(response) for secret in (future_secret, old_secret))
        assert response["llm"]["totals"]["known_prompt_tokens"] == 2
    assert len(calls) == 1
    assert cached is board.review.review_tag(str(server.data), "2026-11-14", "synthetic-room")
    assert cached["llm"]["by_provider"][0]["provider"] == future_secret
    assert metric.read_bytes() == before


@pytest.mark.parametrize("first_route", [None, "/api/llm/usage?tag=synthetic-room&data_date=2026-11-14",
    "/api/review?tag=synthetic-room&date=2026-11-14", "/api/llm"])
@pytest.mark.parametrize("operation", ["rotate", "delete"])
def test_public_reads_and_settings_remember_removed_keys_without_sdk(server, first_route, operation):
    from llm_client import canonical_llm_source, source_secret_set
    old, new = "synthetic-private-api-secret", "synthetic-settings-new-secret"
    metric = server.data / "llm_metrics_2026-11-15.jsonl"
    metric.write_text(json.dumps(usage_record(provider=old, model=new)) + "\n", encoding="utf-8")
    metrics_before, gate_before = metric.read_bytes(), server.gate.read_bytes()
    history = source_secret_set(canonical_llm_source(str(server.config)))
    assert not history  # No loader/SDK or manual history seeding.
    if first_route:
        assert server.request("GET", first_route)[0] == 200
        assert old in history
    if operation == "rotate":
        code, saved = server.request("POST", "/api/llm/settings", {"provider": "demo",
            "protocol": "openai_compatible", "base_url": "https://example.invalid/v1",
            "model": "model-a", "api_key": new})
    else:
        code, saved = server.request("POST", "/api/llm/key/delete", {"provider": "demo"})
    assert code == 200 and saved["config_saved"] and old in history
    if operation == "rotate":
        assert new in history
        # Delete immediately, before any public read could learn the new key.
        assert server.request("POST", "/api/llm/key/delete", {"provider": "demo"})[0] == 200
    entry = json.loads(server.config.read_text())["llm"]["providers"]["demo"]
    assert "api_key" not in entry and "key" not in entry
    for _ in range(2):
        for route in ("/api/llm/usage?tag=synthetic-room&data_date=2026-11-14",
                      "/api/review?tag=synthetic-room&date=2026-11-14"):
            code, response = server.request("GET", route)
            assert code == 200 and old not in json.dumps(response)
            if operation == "rotate":
                assert new not in json.dumps(response)
            else:
                assert new in json.dumps(response)  # Never known in this source.
            usage = response.get("llm", response)
            assert usage["totals"]["known_prompt_tokens"] == 2
    assert metric.read_bytes() == metrics_before and server.gate.read_bytes() == gate_before


def test_public_reads_remember_legacy_aliases_without_loader_or_config_writes(server):
    from llm_client import canonical_llm_source, source_secret_set
    aliases = ("synthetic-legacy-top-key", "synthetic-legacy-provider-key")
    raw = json.loads(server.config.read_text())
    raw["llm"]["key"] = aliases[0]
    raw["llm"]["providers"]["alias"] = {"key": aliases[1]}
    server.config.write_text(json.dumps(raw), encoding="utf-8")
    history = source_secret_set(canonical_llm_source(str(server.config)))
    assert not history
    config_before, gate_before = server.config.read_bytes(), server.gate.read_bytes()
    assert server.request("GET", "/api/llm/usage?tag=synthetic-room")[0] == 200
    assert all(value in history for value in aliases)
    assert server.config.read_bytes() == config_before and server.gate.read_bytes() == gate_before
    raw["llm"].pop("key")
    raw["llm"]["providers"].pop("alias")
    server.config.write_text(json.dumps(raw), encoding="utf-8")
    metric = server.data / "llm_metrics_2026-11-15.jsonl"
    metric.write_text(json.dumps(usage_record(provider=aliases[0], model=aliases[1])) + "\n", encoding="utf-8")
    code, response = server.request("GET", "/api/review?tag=synthetic-room&date=2026-11-14")
    assert code == 200 and all(value not in json.dumps(response) for value in aliases)


def test_known_secrets_stay_in_their_canonical_source(server, tmp_path):
    from llm_client import canonical_llm_source, source_secret_set
    secret_a, secret_b = "synthetic-private-api-secret", "synthetic-other-source-secret"
    config_b = tmp_path / "source-b" / "config.json"
    config_b.parent.mkdir()
    raw = json.loads(server.config.read_text())
    raw["llm"]["providers"]["demo"]["api_key"] = secret_b
    config_b.write_text(json.dumps(raw), encoding="utf-8")
    metric = server.data / "llm_metrics_2026-11-15.jsonl"
    metric.write_text(json.dumps(usage_record(provider=secret_a, model=secret_b)) + "\n", encoding="utf-8")
    route = "/api/review?tag=synthetic-room&date=2026-11-14"
    code, first = server.request("GET", route)
    assert code == 200 and secret_a not in json.dumps(first) and secret_b in json.dumps(first)
    server.handler.config_path = str(config_b)
    code, second = server.request("GET", route)
    assert code == 200 and secret_b not in json.dumps(second) and secret_a in json.dumps(second)
    history_a = source_secret_set(canonical_llm_source(str(server.config)))
    history_b = source_secret_set(canonical_llm_source(str(config_b)))
    assert secret_a in history_a and secret_b not in history_a
    assert secret_b in history_b and secret_a not in history_b
    server.handler.config_path = str(server.config)
    code, third = server.request("GET", route)
    assert code == 200 and secret_a not in json.dumps(third) and secret_b in json.dumps(third)
