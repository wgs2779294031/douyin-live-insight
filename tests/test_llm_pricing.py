"""Generated canonical configs; estimate-only writes never invoke a model."""
from copy import deepcopy
import json
from pathlib import Path
import socket
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llm_pricing as pricing


@pytest.fixture
def config(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("pricing must never use a network")
    monkeypatch.setattr(socket, "socket", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"llm": {"enabled": False, "active_provider": "demo",
        "daily_token_budget": 321, "providers": {"demo": {
            "api_key": "synthetic-private-key", "model": "model-a", "base_url": "https://example.invalid"}}},
        "untouched": {"flag": True}}), encoding="utf-8")
    return path


def draft(**fields):
    return dict({"provider": "demo", "model": "model-a", "currency": "CNY",
                 "input_per_million": .125, "output_per_million": 2.5}, **fields)


def test_price_edit_is_independent_and_old_snapshots_remain_exact(config):
    original = json.loads(config.read_text())
    first = pricing.save_prices(config, draft())["entry"]
    snapshot = deepcopy(pricing.price_snapshot(config, "demo", "model-a"))
    estimate = pricing.estimate_known_usage(dict(prompt_tokens=2, completion_tokens=3, price_snapshot=snapshot))
    assert estimate["amount"] == "0.00000775" and estimate["complete"]
    second = pricing.save_prices(config, draft(input_per_million=1, currency="USD"))["entry"]
    assert first["version"] != second["version"] and first["source"] == "user_entered"
    assert pricing.estimate_known_usage(dict(prompt_tokens=2, completion_tokens=3, price_snapshot=snapshot)) == estimate
    current = json.loads(config.read_text())
    current["llm"].pop(pricing.FIELD)
    assert current == original
    assert not list(config.parent.glob("llm_budget*"))
    assert pricing.price_snapshot(config, "demo", "different-model") is None


def test_partial_blank_explicit_zero_and_clear_have_different_meanings(config):
    assert pricing.estimate_known_usage({"prompt_tokens": 3, "completion_tokens": 4}) is None
    partial = pricing.save_prices(config, draft(input_per_million=0, output_per_million=None))["entry"]
    value = pricing.estimate_known_usage(dict(prompt_tokens=3, completion_tokens=4, price_snapshot=partial))
    assert value["amount"] == "0" and value["input_priced"] and not value["output_priced"] and not value["complete"]
    assert pricing.estimate_known_usage(dict(prompt_tokens=None, completion_tokens=4, price_snapshot=partial))["amount"] is None
    cleared = pricing.save_prices(config, draft(input_per_million=None, output_per_million=None, currency=""))
    assert cleared["entry"] is None and cleared["prices"]["entries"] == []
    assert pricing.price_snapshot(config, "demo", "model-a") is None
    assert pricing.estimate_known_usage(dict(prompt_tokens=3, completion_tokens=4, price_snapshot=partial)) == value


@pytest.mark.parametrize("value", [-1, True, False, "1", {}, [], float("nan"), float("inf"), -float("inf"), 10**400])
def test_invalid_numeric_prices_are_rejected_without_writing(config, value):
    before = config.read_bytes()
    with pytest.raises(pricing.PriceError):
        pricing.save_prices(config, draft(input_per_million=value))
    assert config.read_bytes() == before and not Path(str(config) + ".bak").exists()


@pytest.mark.parametrize("fields", [{"currency": "cny"}, {"currency": None}, {"currency": "USD<script>"},
    {"provider": "../escape"}, {"model": "bad\nmodel"}, {"api_key": "not-allowed"},
    {"model": "synthetic-private-key"}])
def test_invalid_metadata_or_secrets_are_not_published(config, fields):
    before = config.read_bytes()
    with pytest.raises(pricing.PriceError):
        pricing.save_prices(config, draft(**fields))
    assert config.read_bytes() == before


def test_current_key_collision_does_not_get_exposed_by_old_price_metadata(config):
    pricing.save_prices(config, draft(model="future-model"))
    raw = json.loads(config.read_text())
    raw["llm"]["providers"]["demo"]["api_key"] = "future-model"
    config.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(pricing.PriceError):
        pricing.read_prices(config)
    assert pricing.price_snapshot(config, "demo", "future-model") is None


def test_malformed_config_and_snapshot_fail_closed(config):
    before = "{"
    config.write_text(before, encoding="utf-8")
    with pytest.raises(pricing.PriceError):
        pricing.save_prices(config, draft())
    assert config.read_text() == before
    assert pricing.price_snapshot(config, "demo", "model-a") is None
    assert pricing.estimate_known_usage({"price_snapshot": {"currency": "CNY"}}) is None


def test_provider_model_mismatch_never_borrows_another_snapshot(config):
    entry = pricing.save_prices(config, draft())["entry"]
    assert pricing.estimate_known_usage(dict(provider="other", prompt_tokens=2, completion_tokens=2,
                                           price_snapshot=entry)) is None


def test_capacity_rejection_preserves_readable_config_but_replacement_and_clear_work(config):
    row = pricing.save_prices(config, draft())["entry"]
    raw = json.loads(config.read_text())
    raw["llm"][pricing.FIELD]["entries"] = [dict(row, model="model-" + str(index)) for index in range(256)]
    config.write_text(json.dumps(raw), encoding="utf-8")
    before = config.read_bytes()
    with pytest.raises(pricing.PriceError):
        pricing.save_prices(config, draft(model="model-new"))
    assert config.read_bytes() == before and len(pricing.read_prices(config)["entries"]) == 256
    pricing.save_prices(config, draft(model="model-0"))
    assert len(pricing.read_prices(config)["entries"]) == 256
    pricing.save_prices(config, draft(model="model-0", currency="", input_per_million=None, output_per_million=None))
    assert len(pricing.read_prices(config)["entries"]) == 255
