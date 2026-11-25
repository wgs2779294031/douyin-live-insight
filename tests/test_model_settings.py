"""Provider settings and bounded connection checks, using synthetic state only."""
import contextlib
import copy
from datetime import datetime
import http.server
import json
from pathlib import Path
import sys
import threading
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import board_llm as settings


KEY = "synthetic-key-do-not-return"


@pytest.fixture
def tree(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    config = tmp_path / "config.json"
    value = {"room": {"id": "synthetic"}, "other": {"keep": True},
             "llm": {"enabled": False, "active_provider": "other", "daily_token_budget": 91,
                     "providers": {"demo": {"api_key": KEY, "model": "old-model",
                                              "base_url": "https://example.invalid/v1"},
                                   "other": {"api_key": "other-synthetic-key"}}}}
    config.write_text(json.dumps(value), encoding="utf-8")
    for index in range(2):
        sub = copy.deepcopy(value)
        sub["room"]["id"] = str(index)
        sub["llm"]["enabled"] = bool(index)
        sub["llm"]["daily_token_budget"] = 100 + index
        (data / ("watch_%d.config.json" % index)).write_text(json.dumps(sub), encoding="utf-8")
    gate = data / "llm_runtime.json"
    gate.write_bytes(b'{"enabled":false,"active_provider":"other"}')
    return config, data, value


def draft(base_url="https://example.invalid/v1", key="", protocol="openai_compatible"):
    return {"provider": "demo", "protocol": protocol, "base_url": base_url,
            "model": "new-model", "api_key": key}


@pytest.fixture
def allow_model_calls(tree):
    """Connection/privacy cases explicitly opt in under the application total control."""
    config, data, _ = tree
    settings.write_gate(data, enabled=True)


def read(path):
    return json.loads(path.read_bytes())


def test_save_preserves_key_controls_and_other_settings(tree):
    config, data, before = tree
    gate = (data / "llm_runtime.json").read_bytes()
    copies = {p: read(p) for p in data.glob("watch_*.config.json")}
    result = settings.save_provider_config(config, data, draft())
    assert result["ok"] and result["config_saved"]
    assert result["sync"] == {"total": 2, "synced": 2, "failed": 0}
    after = read(config)
    assert after["room"] == before["room"] and after["other"] == before["other"]
    assert after["llm"]["providers"]["demo"]["api_key"] == KEY
    assert after["llm"]["providers"]["demo"]["protocol"] == "openai_compatible"
    assert after["llm"]["enabled"] is False and after["llm"]["active_provider"] == "other"
    assert read(config.with_suffix(".json.bak")) == before
    for path, old in copies.items():
        current = read(path)
        assert current["room"] == old["room"]
        assert current["llm"]["enabled"] == old["llm"]["enabled"]
        assert current["llm"]["daily_token_budget"] == old["llm"]["daily_token_budget"]
        assert current["llm"]["providers"]["demo"] == after["llm"]["providers"]["demo"]
    assert (data / "llm_runtime.json").read_bytes() == gate
    assert KEY not in json.dumps(result)


def test_replacement_and_explicit_deletion_never_return_keys(tree):
    config, data, _ = tree
    new_key = "new-synthetic-secret"
    result = settings.save_provider_config(config, data, draft(key=new_key))
    assert result["ok"] and read(config)["llm"]["providers"]["demo"]["api_key"] == new_key
    assert new_key not in json.dumps(result) and KEY not in json.dumps(result)
    result = settings.delete_provider_key(config, data, {"provider": "demo"})
    assert result["ok"] and result["sync"]["synced"] == 2
    for path in [config, *data.glob("watch_*.config.json")]:
        entry = read(path)["llm"]["providers"]["demo"]
        assert "api_key" not in entry and "key" not in entry
        assert entry["model"] == "new-model"
        assert read(path)["llm"]["providers"]["other"]["api_key"] == "other-synthetic-key"


@pytest.mark.parametrize("key", ["a", "abcd", "12345", KEY])
def test_status_never_exposes_any_key_tail(tree, key):
    config, data, value = tree
    value["llm"]["providers"]["demo"]["api_key"] = key
    config.write_text(json.dumps(value), encoding="utf-8")
    status = settings.read_status(str(data), str(config), "2026-11-14")
    assert status["providers"][0]["key_tail"] == ""
    assert status["providers"][0]["has_key"] is True
    assert status["providers"][0]["protocol"] == "openai_compatible"
    assert "api_key" not in status["providers"][0]


@pytest.mark.parametrize("url", [
    "https://user:password@example.invalid/v1", "https://example.invalid/v1?api_key=secret",
    "https://example.invalid/v1#secret", "http://example.invalid/v1",
    "https://example.invalid/\nsecret", "https://example.invalid/%0asecret",
    "https://example.invalid:99999/v1", "https://example.invalid/v1?",
])
def test_invalid_urls_are_rejected_without_saving_or_echo(tree, url):
    config, data, value = tree
    original = config.read_bytes()
    result = settings.save_provider_config(config, data, draft(base_url=url))
    assert not result["ok"] and result["code"] == "invalid" and not result["config_saved"]
    assert result["error"] == result["message"]
    assert config.read_bytes() == original
    value["llm"]["providers"]["demo"]["base_url"] = url
    config.write_text(json.dumps(value), encoding="utf-8")
    public = settings.read_status(str(data), str(config), "2026-11-14")
    assert public["providers"][0]["base_url"] == ""


@pytest.mark.parametrize("change", [{"provider": "bad/provider"}, {"protocol": "unknown"},
                                     {"model": "bad\nmodel"}, {"model": "x" * 257},
                                     {"api_key": "key\nheader"}, {"enabled": True}])
def test_invalid_fields_never_mutate_config(tree, change):
    config, data, _ = tree
    before = config.read_bytes()
    result = settings.save_provider_config(config, data, {**draft(), **change})
    assert result["code"] == "invalid" and config.read_bytes() == before


def test_main_write_failure_preserves_original_and_reports_unsaved(tree, monkeypatch):
    config, data, _ = tree
    before = config.read_bytes()
    original = settings._atomic_write_json

    def fail(path, value, backup=False):
        if Path(path) == config:
            raise OSError(KEY + str(config))
        return original(path, value, backup=backup)

    monkeypatch.setattr(settings, "_atomic_write_json", fail)
    result = settings.save_provider_config(config, data, draft())
    assert not result["ok"] and not result["config_saved"]
    assert result["sync"] == {"total": 2, "synced": 0, "failed": 2}
    assert config.read_bytes() == before
    assert KEY not in json.dumps(result) and str(config) not in json.dumps(result)


def test_partial_copy_failure_reports_real_progress(tree, monkeypatch):
    config, data, _ = tree
    bad = data / "watch_0.config.json"
    before = bad.read_bytes()
    original = settings._atomic_write_json

    def fail(path, value, backup=False):
        if Path(path) == bad:
            raise OSError(KEY + str(bad))
        return original(path, value, backup=backup)

    monkeypatch.setattr(settings, "_atomic_write_json", fail)
    result = settings.save_provider_config(config, data, draft())
    assert not result["ok"] and result["config_saved"] and result["code"] == "partial_sync"
    assert result["sync"] == {"total": 2, "synced": 1, "failed": 1}
    assert bad.read_bytes() == before
    assert read(config)["llm"]["providers"]["demo"]["model"] == "new-model"
    assert KEY not in json.dumps(result) and str(bad) not in json.dumps(result)


def test_corrupt_copy_is_preserved_but_valid_copy_syncs(tree):
    config, data, _ = tree
    bad = data / "watch_0.config.json"
    bad.write_bytes(b"{broken synthetic config")
    result = settings.save_provider_config(config, data, draft())
    assert result["config_saved"] and not result["ok"]
    assert result["sync"] == {"total": 2, "synced": 1, "failed": 1}
    assert bad.read_bytes() == b"{broken synthetic config"


def test_delete_partial_is_explicit_and_removes_legacy_alias(tree, monkeypatch):
    config, data, value = tree
    value["llm"]["providers"]["demo"]["key"] = "legacy-synthetic-key"
    config.write_text(json.dumps(value), encoding="utf-8")
    bad = data / "watch_0.config.json"
    original = settings._atomic_write_json

    def fail(path, obj, backup=False):
        if Path(path) == bad:
            raise OSError("synthetic deletion failure")
        return original(path, obj, backup=backup)

    monkeypatch.setattr(settings, "_atomic_write_json", fail)
    result = settings.delete_provider_key(config, data, {"provider": "demo"})
    assert result["config_saved"] and not result["ok"]
    assert result["sync"] == {"total": 2, "synced": 1, "failed": 1}
    assert "api_key" not in read(config)["llm"]["providers"]["demo"]
    assert "key" not in read(config)["llm"]["providers"]["demo"]
    assert read(bad)["llm"]["providers"]["demo"]["api_key"] == KEY


def test_concurrent_provider_saves_do_not_lose_canonical_or_copy_updates(tree):
    config, data, _ = tree
    start = threading.Barrier(3)
    results = []

    def save(name):
        body = {**draft(key="synthetic-" + name), "provider": name}
        start.wait(timeout=2)
        results.append(settings.save_provider_config(config, data, body))

    threads = [threading.Thread(target=save, args=(name,)) for name in ("first", "second")]
    for thread in threads:
        thread.start()
    start.wait(timeout=2)
    for thread in threads:
        thread.join(timeout=3)
    assert len(results) == 2 and all(result["ok"] for result in results)
    for path in [config, *data.glob("watch_*.config.json")]:
        providers = read(path)["llm"]["providers"]
        assert providers["first"]["api_key"] == "synthetic-first"
        assert providers["second"]["api_key"] == "synthetic-second"


def test_key_cannot_be_embedded_in_saved_endpoint_path(tree):
    config, data, _ = tree
    before = config.read_bytes()
    result = settings.save_provider_config(config, data,
                                          draft("https://example.invalid/" + KEY))
    assert result["code"] == "invalid" and config.read_bytes() == before
    assert KEY not in json.dumps(result)


@pytest.mark.parametrize("field,key,value", [
    ("provider", KEY, KEY), ("model", KEY, "prefix-" + KEY + "-suffix"),
    ("model", "abcd", "abcd"),
])
@pytest.mark.usefixtures("allow_model_calls")
def test_secret_in_public_identity_is_rejected_for_save_and_test(tree, monkeypatch, field, key, value):
    config, data, _ = tree
    before = config.read_bytes()
    body = {**draft(key=key), field: value}
    monkeypatch.setattr(settings, "_provider_http_request", lambda *args: pytest.fail("must not request"))
    saved = settings.save_provider_config(config, data, body)
    tested = settings.test_provider_connection(config, data, body)
    assert saved["code"] == "invalid" and tested["code"] == "invalid_config"
    assert config.read_bytes() == before
    assert key not in json.dumps(saved) and key not in json.dumps(tested)


@pytest.mark.usefixtures("allow_model_calls")
def test_saved_key_cannot_be_embedded_in_unsaved_draft_model(tree, monkeypatch):
    config, data, _ = tree
    body = {**draft(), "model": "prefix-" + KEY}
    monkeypatch.setattr(settings, "_provider_http_request", lambda *args: pytest.fail("must not request"))
    assert settings.save_provider_config(config, data, body)["code"] == "invalid"
    assert settings.test_provider_connection(config, data, body)["code"] == "invalid_config"


def test_empty_input_migrates_legacy_key_without_losing_secret(tree):
    config, data, value = tree
    entry = value["llm"]["providers"]["demo"]
    entry["key"] = entry.pop("api_key")
    config.write_text(json.dumps(value), encoding="utf-8")
    result = settings.save_provider_config(config, data, draft())
    assert result["ok"]
    for path in [config, *data.glob("watch_*.config.json")]:
        entry = read(path)["llm"]["providers"]["demo"]
        assert entry["api_key"] == KEY and "key" not in entry
    assert KEY not in json.dumps(result)


@pytest.mark.parametrize("field", ["provider", "model", "base_url"])
@pytest.mark.usefixtures("allow_model_calls")
def test_other_provider_secret_cannot_enter_draft_public_fields(tree, monkeypatch, field):
    config, data, _ = tree
    other_key = "other-synthetic-key"
    value = "https://example.invalid/" + other_key if field == "base_url" else other_key
    body = {**draft(), field: value}
    before = config.read_bytes()
    monkeypatch.setattr(settings, "_provider_http_request", lambda *args: pytest.fail("must not send"))
    saved = settings.save_provider_config(config, data, body)
    tested = settings.test_provider_connection(config, data, body)
    assert saved["code"] == "invalid" and tested["code"] == "invalid_config"
    assert config.read_bytes() == before
    assert other_key not in json.dumps(saved) and other_key not in json.dumps(tested)


def test_status_redacts_all_known_keys_from_dynamic_and_legacy_metadata(tree):
    config, data, value = tree
    other_key = value["llm"]["providers"]["other"]["api_key"]
    value["llm"]["active_provider"] = other_key
    value["llm"]["api_key"] = "legacy-flat-synthetic-secret"
    value["llm"]["providers"]["demo"].update(
        model=other_key, base_url="https://example.invalid/" + other_key,
        label="prefix-legacy-flat-synthetic-secret", owner=other_key)
    value["llm"]["providers"][other_key] = {"model": "m", "api_key": KEY}
    config.write_text(json.dumps(value), encoding="utf-8")
    stamp = datetime(2026, 11, 15, 12).timestamp()
    (data / "llm_metrics_2026-11-15.jsonl").write_text(
        json.dumps({"provider": other_key, "model": KEY, "prompt_tokens": 1, "ts": stamp}) + "\n",
        encoding="utf-8")
    status = settings.read_status(str(data), str(config), "2026-11-15")
    public = json.dumps(status)
    assert other_key not in public and "legacy-flat-synthetic-secret" not in public
    assert KEY not in public and status["active_provider"] == ""
    assert status["providers"][0]["model"] == status["providers"][0]["base_url"] == ""
    assert status["today"]["total_tokens"] == 1
    today = status["today"]
    assert today["totals"]["metric_records"] == 1
    assert today["totals"]["known_prompt_tokens"] == 1
    assert len(today["by_provider"]) == 1
    group = today["by_provider"][0]
    assert group["metric_records"] == 1 and group["known_prompt_tokens"] == 1
    assert group["provider"] == group["model"] == ""


@pytest.mark.parametrize("file_day", ["2026-11-15", "2026-11-16"])
def test_status_undated_metrics_remain_unknown_regardless_of_filename(tree, file_day):
    import board_usage
    config, data, _ = tree
    metrics = data / ("llm_metrics_" + file_day + ".jsonl")
    metrics.write_text(json.dumps({"provider": "demo", "prompt_tokens": 1}) + "\n", encoding="utf-8")
    before = metrics.read_bytes()
    today = settings.read_status(str(data), str(config), "2026-11-15")["today"]
    assert today["total_tokens"] == today["totals"]["metric_records"] == 0
    assert today["by_provider"] == []
    assert today["coverage"]["undated_records"] == 1
    assert today["coverage"]["status"] == "unknown" and today["cost_cny"] is None
    all_dates = board_usage.read_usage(data)
    assert all_dates["totals"]["metric_records"] == 1
    assert all_dates["totals"]["known_prompt_tokens"] == 1
    assert all_dates["coverage"]["legacy_records"] == 1
    assert all_dates["coverage"]["status"] == "partial"
    assert metrics.read_bytes() == before


@pytest.mark.usefixtures("allow_model_calls")
def test_new_key_collision_with_existing_other_metadata_is_rejected(tree, monkeypatch):
    config, data, value = tree
    new_key = "new-secret-must-not-become-public"
    value["llm"]["providers"]["other"]["model"] = new_key
    config.write_text(json.dumps(value), encoding="utf-8")
    before = config.read_bytes()
    monkeypatch.setattr(settings, "_provider_http_request", lambda *args: pytest.fail("must not send"))
    body = draft(key=new_key)
    assert settings.save_provider_config(config, data, body)["code"] == "invalid"
    assert settings.test_provider_connection(config, data, body)["code"] == "invalid_config"
    assert config.read_bytes() == before


def test_retired_key_cannot_become_unmasked_metadata_after_edit(tree):
    config, data, value = tree
    value["llm"]["providers"]["other"]["model"] = KEY
    config.write_text(json.dumps(value), encoding="utf-8")
    before = config.read_bytes()
    assert settings.save_provider_config(config, data, draft(key="replacement-synthetic-key"))["code"] == "invalid"
    assert settings.delete_provider_key(config, data, {"provider": "demo"})["code"] == "invalid"
    assert config.read_bytes() == before
    assert KEY not in json.dumps(settings.read_status(str(data), str(config), "2026-11-15"))


@pytest.mark.parametrize("short_key", ["x", "abcd"])
@pytest.mark.usefixtures("allow_model_calls")
def test_cross_provider_short_key_exact_metadata_is_hidden(tree, monkeypatch, short_key):
    config, data, value = tree
    value["llm"]["providers"]["other"]["api_key"] = short_key
    value["llm"]["providers"]["demo"]["model"] = short_key
    value["llm"]["active_provider"] = short_key
    config.write_text(json.dumps(value), encoding="utf-8")
    status = settings.read_status(str(data), str(config), "2026-11-15")
    assert status["active_provider"] == "" and status["providers"][0]["model"] == ""
    assert all(provider["key_tail"] == "" for provider in status["providers"])
    monkeypatch.setattr(settings, "_provider_http_request", lambda *args: pytest.fail("must not send"))
    body = {**draft(), "model": short_key}
    assert settings.save_provider_config(config, data, body)["code"] == "invalid"
    assert settings.test_provider_connection(config, data, body)["code"] == "invalid_config"


@contextlib.contextmanager
def fake_provider(status=200, delay=0, slow=False):
    observed = []

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            observed.append({"path": self.path, "body": json.loads(body),
                             "authorization": self.headers.get("Authorization")})
            time.sleep(delay)
            payload = json.dumps({"choices": [{"message": {"content": KEY}}],
                                  "usage": {"prompt_tokens": 3, "completion_tokens": 1,
                                            "total_tokens": 4, "private": KEY}}).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                if status == 302:
                    self.send_header("Location", "/redirect-do-not-follow")
                self.end_headers()
                if slow:
                    for char in payload:
                        self.wfile.write(bytes([char]))
                        self.wfile.flush()
                        time.sleep(0.03)
                else:
                    self.wfile.write(payload)
            except OSError:
                pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d/v1" % server.server_port, observed
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@pytest.mark.parametrize("protocol", ["doubao", "openai_compatible"])
@pytest.mark.usefixtures("allow_model_calls")
def test_active_draft_uses_saved_key_one_request_and_no_state_writes(tree, protocol):
    config, data, _ = tree
    before = {p: p.read_bytes() for p in [config, *data.iterdir()] if p.is_file()}
    with fake_provider() as (url, observed):
        result = settings.test_provider_connection(config, data, draft(url, protocol=protocol))
    assert result == {"ok": True, "code": "ok", "message": "连接测试成功。", "transport": "direct",
                      "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}}
    assert len(observed) == 1 and observed[0]["path"] == "/v1/chat/completions"
    assert observed[0]["authorization"] == "Bearer " + KEY
    assert observed[0]["body"]["max_tokens"] <= 8
    assert observed[0]["body"]["stream"] is False
    assert ("thinking" in observed[0]["body"]) == (protocol == "doubao")
    assert all(path.read_bytes() == raw for path, raw in before.items())
    assert KEY not in json.dumps(result)


@pytest.mark.parametrize("status,code", [(401, "auth_failed"), (403, "auth_failed"),
                                         (429, "rate_limited"), (404, "model_not_found"),
                                         (500, "upstream_error"), (302, "upstream_error")])
@pytest.mark.usefixtures("allow_model_calls")
def test_upstream_failure_is_fixed_and_never_retried(tree, status, code):
    config, data, _ = tree
    with fake_provider(status=status) as (url, observed):
        result = settings.test_provider_connection(config, data, draft(url))
    assert not result["ok"] and result["code"] == code
    assert len(observed) == 1 and KEY not in json.dumps(result)


@pytest.mark.usefixtures("allow_model_calls")
def test_slow_drip_has_wall_clock_deadline(tree, monkeypatch):
    config, data, _ = tree
    monkeypatch.setattr(settings, "_PROVIDER_TEST_TIMEOUT", 0.15)
    with fake_provider(slow=True) as (url, observed):
        started = time.monotonic()
        result = settings.test_provider_connection(config, data, draft(url))
        elapsed = time.monotonic() - started
    assert result["code"] == "timeout" and elapsed < 0.65
    assert len(observed) == 1


@pytest.mark.usefixtures("allow_model_calls")
def test_busy_blocks_duplicate_even_while_timed_out_worker_unwinds(tree, monkeypatch):
    config, data, _ = tree
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(settings, "_PROVIDER_TEST_TIMEOUT", 0.08)
    calls = []

    def blocked(*args):
        calls.append(1)
        entered.set()
        release.wait(timeout=2)
        return settings._test_result("ok")

    monkeypatch.setattr(settings, "_provider_http_request", blocked)
    result = []
    thread = threading.Thread(target=lambda: result.append(
        settings.test_provider_connection(config, data, draft())))
    thread.start()
    assert entered.wait(timeout=1)
    assert settings.test_provider_connection(config, data, draft())["code"] == "busy"
    thread.join(timeout=1)
    assert result[0]["code"] == "timeout"
    assert settings.test_provider_connection(config, data, draft())["code"] == "busy"
    assert len(calls) == 1
    release.set()


@pytest.mark.usefixtures("allow_model_calls")
def test_late_connect_never_sends_a_post_after_deadline(tree, monkeypatch):
    config, data, _ = tree
    release, closed = threading.Event(), threading.Event()
    sent = []

    class Connection:
        sock = None

        def __init__(self, *args, **kwargs):
            pass

        def connect(self):
            release.wait(timeout=2)

        def request(self, *args, **kwargs):
            sent.append(1)

        def close(self):
            closed.set()

    monkeypatch.setattr(settings.http.client, "HTTPSConnection", Connection)
    monkeypatch.setattr(settings, "_PROVIDER_TEST_TIMEOUT", 0.05)
    result = settings.test_provider_connection(config, data, draft())
    assert result["code"] == "timeout"
    release.set()
    assert closed.wait(timeout=1)
    assert sent == []


@pytest.mark.usefixtures("allow_model_calls")
def test_missing_key_and_bad_draft_do_not_start_network(tree, monkeypatch):
    config, data, value = tree
    value["llm"]["providers"]["demo"].pop("api_key")
    config.write_text(json.dumps(value), encoding="utf-8")
    monkeypatch.setattr(settings, "_provider_http_request", lambda *args: pytest.fail("no request"))
    assert settings.test_provider_connection(config, data, draft())["code"] == "missing_key"
    assert settings.test_provider_connection(config, data, {"provider": "demo"})["code"] == "invalid_config"
