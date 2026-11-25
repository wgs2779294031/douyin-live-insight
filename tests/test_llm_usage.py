"""SDK usage observations: generated config, fake SDK, hard-denied sockets."""
import json
from pathlib import Path
import socket
import ssl  # Import before replacing socket.socket (ssl subclasses it).
import sys
from types import ModuleType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import client  # Preload the application's SSL-dependent imports before the guard.
import llm_client as module
import llm_service


class HTTPFailure(RuntimeError):
    def __init__(self, status):
        self.status_code = status
        super().__init__("synthetic private diagnostic")


def response(prompt=7, completion=3, *, content="synthetic reply", usage=True, malformed=False):
    return SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion) if usage else None,
        choices=[] if malformed else [SimpleNamespace(message=SimpleNamespace(content=content))])


@pytest.fixture
def sdk(monkeypatch):
    state = SimpleNamespace(calls=[], outcomes=[], init=[], prices=[])

    def deny(*args, **kwargs):
        raise AssertionError("network forbidden in SDK usage tests")
    monkeypatch.setattr(socket, "socket", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    class SDK:
        def __init__(self, **kwargs):
            state.init.append(kwargs)
            self.key = kwargs["api_key"]
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, **kwargs):
            state.calls.append({"key": self.key, **kwargs})
            outcome = state.outcomes.pop(0) if state.outcomes else response()
            if callable(outcome):
                outcome = outcome()
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        def close(self):
            pass

    fake = ModuleType("openai")
    fake.OpenAI = SDK
    monkeypatch.setitem(sys.modules, "openai", fake)
    pricing = ModuleType("llm_pricing")
    pricing.price_snapshot = lambda *args: state.prices.pop(0) if state.prices else None
    monkeypatch.setitem(sys.modules, "llm_pricing", pricing)
    return state


def direct(records):
    return module.LLMClient("synthetic-key", "https://synthetic.invalid", "model-a",
                            provider_name="primary", on_metrics=records.append)


def provider(key="synthetic-primary-key", model="model-a"):
    return {"api_key": key, "base_url": "https://synthetic.invalid", "model": model,
            "protocol": "openai_compatible"}


def routed(tmp_path, records, *, providers=None, budget=1000, **kwargs):
    config, data = tmp_path / "config.json", tmp_path / "data"
    data.mkdir(exist_ok=True)
    config.write_text(json.dumps({"llm": {"enabled": True,
        "providers": providers or {"primary": provider()}, "active_provider": "primary",
        "daily_token_budget": budget}}), encoding="utf-8")
    router = llm_service.load_llm_config(str(config)).build_router(
        budget_dir=str(data), control_data_dir=str(data), on_metrics=records.append, **kwargs)
    return router, config, data


@pytest.mark.parametrize("prompt,completion,expected,coverage", [
    (7, 3, (7, 3), "complete"), (0, 0, (0, 0), "complete"),
    (None, None, (None, None), "unknown"),
    (None, 3, (None, 3), "partial"), (7, None, (7, None), "partial"),
    (True, 3, (None, 3), "partial"), (7, False, (7, None), "partial"),
    ("7", 3, (None, 3), "partial"), (7.0, 3, (None, 3), "partial"),
    (-1, 3, (None, 3), "partial"), (float("nan"), 3, (None, 3), "partial"),
    (7, float("inf"), (7, None), "partial"),
])
def test_only_explicit_nonnegative_integer_usage_is_known(sdk, prompt, completion, expected, coverage):
    records = []
    sdk.outcomes.append(response(prompt, completion))
    assert direct(records).chat("system", "synthetic")
    row = records[0]
    assert (row["prompt_tokens"], row["completion_tokens"]) == expected
    assert row["prompt_tokens_known"] is (expected[0] is not None)
    assert row["completion_tokens_known"] is (expected[1] is not None)
    assert row["usage_coverage"] == coverage
    assert row["sdk_attempts"] == len(sdk.calls) == 1 and row["sent"] is True
    assert row["unknown_attempts"] == (coverage != "complete")
    assert row["metrics_version"] == 2 and len(row["metric_id"]) == 32
    assert row["ts"] == row["first_sent_at"] == row["sdk_attempt_usage"][0]["ts"]
    assert row["last_response_at"] >= row["first_sent_at"]
    json.dumps(row, allow_nan=False)
    assert sdk.init[0]["max_retries"] == 0


def test_missing_usage_is_unknown_even_with_successful_content(sdk):
    records = []
    sdk.outcomes.append(response(usage=False))
    assert direct(records).chat("system", "synthetic")
    assert records[0]["success"] is True
    assert records[0]["prompt_tokens"] is records[0]["completion_tokens"] is None
    assert records[0]["usage_coverage"] == "unknown"


def test_missing_usage_field_is_independently_unknown(sdk):
    records = []
    result = response()
    result.usage = SimpleNamespace(prompt_tokens=4)
    sdk.outcomes.append(result)
    assert direct(records).chat("system", "synthetic")
    assert records[0]["prompt_tokens"] == 4 and records[0]["completion_tokens"] is None
    assert records[0]["usage_coverage"] == "partial"


def test_retry_success_keeps_unknown_attempts_and_single_known_ledger_delta(tmp_path, sdk):
    records = []
    router, _, _ = routed(tmp_path, records)
    sdk.outcomes.extend([TimeoutError(), TimeoutError(), response(11, 2)])
    assert router.chat("system", "synthetic", purpose="review")
    row = records[0]
    assert len(records) == 1 and row["sdk_attempts"] == 3 and row["unknown_attempts"] == 2
    assert row["success"] and row["usage_coverage"] == "partial"
    assert row["prompt_tokens"] == 11 and row["completion_tokens"] == 2
    assert not row["prompt_tokens_known"] and not row["completion_tokens_known"]
    assert [a["response_at"] is None for a in row["sdk_attempt_usage"]] == [True, True, False]
    assert router._budget.check() and router._budget.last_total == 13
    assert row["ledger_date"] == router._budget.last_day and row["purpose"] == "review"


def test_usage_precedes_choices_parse_and_all_known_replies_charge_once(tmp_path, sdk):
    records = []
    router, _, _ = routed(tmp_path, records)
    sdk.outcomes.extend([response(2, 1, malformed=True), response(5, 3)])
    assert router.chat("system", "synthetic")
    row = records[0]
    assert row["sdk_attempts"] == 2 and row["unknown_attempts"] == 0
    assert row["usage_coverage"] == "complete"
    assert (row["prompt_tokens"], row["completion_tokens"]) == (7, 4)
    assert router._budget.check() and router._budget.last_total == 11


@pytest.mark.parametrize("content,error", [("not valid portrait JSON", ""), ("", "empty_content")])
def test_business_output_failure_does_not_erase_returned_usage(tmp_path, sdk, content, error):
    records = []
    router, _, _ = routed(tmp_path, records)
    sdk.outcomes.append(response(4, 2, content=content))
    text = router.chat("system", "synthetic", purpose="portrait")
    if content:
        with pytest.raises(ValueError):
            json.loads(text)
    else:
        assert text is None
    assert records[0]["error_type"] == error
    assert records[0]["usage_coverage"] == "complete"
    assert router._budget.check() and router._budget.last_total == 6


@pytest.mark.parametrize("failure,attempts,response_received", [
    (HTTPFailure(401), 1, True), (TimeoutError(), 3, False)])
def test_known_http_failure_and_transport_unknown_both_have_unknown_usage(sdk, failure, attempts, response_received):
    records = []
    sdk.outcomes.extend([failure] * attempts)
    assert direct(records).chat("system", "synthetic") is None
    row = records[0]
    assert row["sdk_attempts"] == row["unknown_attempts"] == attempts
    assert row["usage_coverage"] == "unknown" and row["prompt_tokens"] is None
    assert all((a["response_at"] is not None) == response_received for a in row["sdk_attempt_usage"])
    if response_received:
        assert row["error_type"].endswith("_401")  # 3B1 known-result settlement unchanged.


@pytest.mark.parametrize("block", ["budget", "marker"])
def test_unsent_block_has_zero_sdk_attempts_without_fake_free_tokens(tmp_path, sdk, block):
    records = []
    router, _, _ = routed(tmp_path, records, budget=1,
                           **({"on_dispatch": lambda **_: False} if block == "marker" else {}))
    if block == "budget":
        router._budget.add(1, 0)
    assert router.chat("system", "synthetic") is None
    row = records[-1]
    assert sdk.calls == [] and not row["sent"] and row["sdk_attempts"] == row["unknown_attempts"] == 0
    assert row["usage_coverage"] == "not_sent" and row["sdk_attempt_usage"] == []
    assert row["prompt_tokens"] is row["completion_tokens"] is None


def test_partial_known_component_uses_original_ledger_without_inventing_metrics_zero(tmp_path, sdk):
    records = []
    router, _, _ = routed(tmp_path, records)
    sdk.outcomes.append(response(None, 9))
    assert router.chat("system", "synthetic")
    assert records[0]["prompt_tokens"] is None and records[0]["completion_tokens"] == 9
    assert router._budget.check() and router._budget.last_total == 9


def test_borrow_records_keep_each_provider_and_selected_account(tmp_path, sdk):
    records = []
    providers = {"primary": provider(), "a": provider("synthetic-a-key", "model-a"),
                 "b": provider("synthetic-b-key", "model-b")}
    router, _, data = routed(tmp_path, records, providers=providers, budget=1)
    router._budget.add(1, 0)
    account = module.DailyTokenBudget(100, str(data), name="b")
    router._backup_budgets["b"] = account
    sdk.outcomes.extend([HTTPFailure(401), TimeoutError(), response(8, 2)])
    assert router.chat("system", "synthetic", purpose="review")
    a, b = records
    assert (a["provider"], b["provider"], b["model"]) == ("a", "b", "model-b")
    assert [a["sdk_attempts"], b["sdk_attempts"]] == [1, 2]
    assert [a["unknown_attempts"], b["unknown_attempts"]] == [1, 1]
    assert len({a["metric_id"], b["metric_id"]}) == 2
    assert a["error_type"].endswith("_401") and b["success"]
    assert account.check() and account.last_total == 10
    assert router._budget.check() is False and router._budget.last_total == 1


def test_probe_tracks_usage_but_remains_excluded_from_original_budget(tmp_path, sdk):
    records = []
    router, _, _ = routed(tmp_path, records)
    plan = router._selection("probe")
    assert router._selected_call(plan, probe=True)
    row = records[0]
    assert row["probe"] and row["sdk_attempts"] == 1 and row["usage_coverage"] == "complete"
    assert row["prompt_tokens"] == 7 and row["completion_tokens"] == 3
    assert router._budget.check() and router._budget.last_total == 0


def test_attempt_dates_prices_and_identity_are_send_snapshots(tmp_path, sdk, monkeypatch):
    records = []
    router, _, _ = routed(tmp_path, records)
    clock = [2000000000.0]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    # Synthetic UTC midnight boundary; aggregation can choose its documented local zone.
    first = {"currency": "CNY", "input_per_million": 1, "output_per_million": 2,
             "version": "one", "source": "user_entered"}
    second = {**first, "version": "two", "input_per_million": 3}
    sdk.prices.extend([first, second])
    def first_timeout():
        clock[0] += 86400
        raise TimeoutError()
    sdk.outcomes.extend([first_timeout, response(5, 4)])
    assert router.chat("system", "synthetic")
    row = records[0]
    assert row["ts"] == 2000000000.0 and row["provider"] == "primary" and row["model"] == "model-a"
    assert [a["ts"] for a in row["sdk_attempt_usage"]] == [2000000000.0, 2000086400.0]
    assert [a["price_snapshot"]["version"] for a in row["sdk_attempt_usage"]] == ["one", "two"]
    assert row["last_response_at"] == 2000086400.0


def test_price_io_precedes_final_guard_and_cannot_bypass_pause(tmp_path, sdk, monkeypatch):
    records = []
    router, _, data = routed(tmp_path, records)
    def paused_price(*_):
        (data / "llm_runtime.json").write_text(json.dumps({"enabled": False}), encoding="utf-8")
        return {"currency": "CNY", "input_per_million": 0, "output_per_million": 0}
    monkeypatch.setattr(sys.modules["llm_pricing"], "price_snapshot", paused_price)
    assert router.chat("system", "synthetic") is None
    assert sdk.calls == [] and records[-1]["sdk_attempts"] == 0


def test_cancellation_after_sent_timeout_retains_attempt_unknown(tmp_path, sdk):
    records = []
    router, config, _ = routed(tmp_path, records)
    def deleted_key():
        raw = json.loads(config.read_text(encoding="utf-8"))
        raw["llm"]["providers"]["primary"]["api_key"] = ""
        config.write_text(json.dumps(raw), encoding="utf-8")
        raise TimeoutError()
    sdk.outcomes.append(deleted_key)
    assert router.chat("system", "synthetic") is None
    row = records[0]
    assert len(sdk.calls) == 1 and row["sdk_attempts"] == row["unknown_attempts"] == 1
    assert row["error_type"] == "send_cancelled" and row["usage_coverage"] == "unknown"


def test_inflight_snapshot_does_not_bypass_newly_known_secret_redaction(tmp_path, sdk):
    records = []
    router, _, _ = routed(tmp_path, records)
    sdk.prices.append({"provider": "primary", "model": "model-a", "currency": "CNY",
                       "input_per_million": 1, "version": "synthetic"})
    def metadata_now_matches_secret():
        router._known_secrets.add("primary")
        return response(4, 2)
    sdk.outcomes.append(metadata_now_matches_secret)
    assert router.chat("system", "synthetic")
    row = records[0]
    assert row["provider"] == "" and row["sdk_attempt_usage"][0]["price_snapshot"] is None
    assert '"primary"' not in json.dumps(row)
    assert router._budget.check() and router._budget.last_total == 6


@pytest.mark.parametrize("location", ["body", "usage"])
@pytest.mark.parametrize("probe", [False, True])
def test_http_error_explicit_usage_is_retained_without_raw_body_or_double_accounting(tmp_path, sdk, caplog, location, probe):
    records = []
    router, _, _ = routed(tmp_path, records)
    failure = HTTPFailure(401)
    failure.body = {"private": "synthetic-body-must-not-escape"}
    if location == "body":
        failure.body["usage"] = {"prompt_tokens": 5, "completion_tokens": 2}
    else:
        failure.usage = SimpleNamespace(prompt_tokens=5, completion_tokens=2)
    sdk.outcomes.append(failure)
    if probe:
        assert not router._selected_call(router._selection(), probe=True)
    else:
        assert router.chat("system", "synthetic") is None
    row = records[0]
    assert len(sdk.calls) == len(records) == row["sdk_attempts"] == 1
    assert (row["prompt_tokens"], row["completion_tokens"]) == (5, 2)
    assert row["usage_coverage"] == "complete" and row["unknown_attempts"] == 0
    assert row["last_response_at"] is not None and row["success"] is False
    assert router._budget.check() and router._budget.last_total == (0 if probe else 7)
    assert "synthetic-body-must-not-escape" not in json.dumps(row) + caplog.text


@pytest.mark.parametrize("probe", [False, True])
def test_http_error_bad_usage_keeps_only_valid_component_unknown(tmp_path, sdk, probe):
    records = []
    router, _, _ = routed(tmp_path, records)
    failure = HTTPFailure(403)
    failure.body = {"usage": {"prompt_tokens": "5", "completion_tokens": 2}}
    sdk.outcomes.append(failure)
    if probe:
        assert not router._selected_call(router._selection(), probe=True)
    else:
        assert router.chat("system", "synthetic") is None
    row = records[0]
    assert row["prompt_tokens"] is None and row["completion_tokens"] == 2
    assert row["unknown_attempts"] == 1 and row["usage_coverage"] == "partial"
    assert len(sdk.calls) == row["sdk_attempts"] == 1
    assert router._budget.check() and router._budget.last_total == (0 if probe else 2)
