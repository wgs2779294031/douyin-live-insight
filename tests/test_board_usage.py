"""Synthetic read-only usage projections; no model or network clients."""
from datetime import datetime
import copy
from decimal import Decimal
import json
from pathlib import Path
import sys
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import board_usage as usage
from llm_pricing import estimate_known_usage as real_estimate
from test_llm_usage import sdk, routed, response, provider, HTTPFailure


def stamp(day, hour=12):
    return datetime.fromisoformat(day + f"T{hour:02d}:00:00").timestamp()


def price(currency="CNY", input=3, output=15, version="a" * 32):
    return {"provider": "synthetic", "model": "synthetic-model", "currency": currency,
            "input_per_million": input, "output_per_million": output,
            "version": version, "saved_at": "2026-11-15T00:00:00+00:00", "source": "user_entered"}


def attempt(day="2026-11-15", p=100, c=20, snapshot=None):
    return {"ts": stamp(day), "response_at": stamp(day) + 1, "prompt_tokens": p,
            "completion_tokens": c, "price_snapshot": snapshot}


def record(*attempts, **fields):
    return {"metrics_version": 2, "metric_id": uuid.uuid4().hex,
            "provider": "synthetic", "model": "synthetic-model", "success": True,
            "ts": attempts[0]["ts"] if attempts else stamp("2026-11-15"),
            "sent": bool(attempts), "sdk_attempts": len(attempts),
            "sdk_attempt_usage": list(attempts), "room_tag": "synthetic-room",
            "purpose": "review", "data_date": "2026-11-14", **fields}


def write(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_optional_prices_zero_and_currency_separation():
    result = usage.aggregate_records([record(attempt(snapshot=price())),
        record(attempt(snapshot=price("USD", .1, .2, "b" * 32))),
        record(attempt(snapshot=price("USDT", 1, 2, "c" * 32)))])
    assert result["totals"]["sdk_attempts"] == 3
    assert result["prompt_tokens"] == 300 and result["completion_tokens"] == 60
    assert {item["currency"]: Decimal(item["amount"]) for item in result["estimates"]} == {
        "CNY": Decimal(".0006"), "USD": Decimal(".000014"), "USDT": Decimal(".00014")}
    assert result["cost_cny"] is None
    assert result["estimates"][0]["price_snapshots"][0]["saved_at"] == "2026-11-15T00:00:00+00:00"
    no_price = usage.aggregate_records([record(attempt())])
    assert no_price["estimates"] == [] and no_price["cost_cny"] is None
    zero = usage.aggregate_records([record(attempt(snapshot=price(input=0, output=0)))])
    assert Decimal(zero["estimates"][0]["amount"]) == 0 and zero["cost_cny"] == 0
    assert zero["coverage"]["price_complete"] is True


def test_price_snapshot_never_uses_current_config(monkeypatch):
    import llm_pricing
    monkeypatch.setattr(llm_pricing, "price_snapshot", lambda *a, **k: pytest.fail("current price read"))
    old = record(attempt(snapshot=price(input=1, output=2)))
    new = record(attempt(snapshot=price(input=2, output=4, version="b" * 32)))
    assert Decimal(usage.aggregate_records([old])["estimates"][0]["amount"]) == Decimal(".00014")
    result = usage.aggregate_records([old, new])
    assert Decimal(result["estimates"][0]["amount"]) == Decimal(".00042")
    assert result["estimates"][0]["price_versions"] == ["a" * 32, "b" * 32]


@pytest.mark.parametrize("bad", [None, True, -1, 1.5, "2"])
def test_invalid_or_missing_component_is_unknown_not_free(bad):
    result = usage.aggregate_records([record(attempt(p=bad, snapshot=price()))])
    assert result["prompt_tokens"] == 0 and result["completion_tokens"] == 20
    assert result["coverage"]["unknown_usage_records"] == 1
    assert result["coverage"]["status"] == "partial"
    assert result["cost_cny"] is None
    assert result["estimates"][0]["partial"] is True
    assert Decimal(result["estimates"][0]["amount"]) == Decimal(".0003")


def test_partial_price_and_legacy_reported_tokens():
    partial = usage.aggregate_records([record(attempt(snapshot=price(output=None)))])
    assert Decimal(partial["estimates"][0]["amount"]) == Decimal(".0003")
    assert partial["estimates"][0]["partial"] is True
    legacy = usage.aggregate_records([{"ts": stamp("2026-11-15"), "prompt_tokens": 99,
        "completion_tokens": 20, "price_snapshot": price(), "provider": "synthetic"}])
    assert legacy["prompt_tokens"] == 99 and legacy["coverage"]["legacy_records"] == 1
    assert legacy["coverage"]["status"] == "partial"
    assert legacy["estimates"] == [] and legacy["cost_cny"] is None


def test_retry_success_keeps_fee_gap_and_record_attempt_units_separate():
    result = usage.aggregate_records([record(attempt(p=None, c=None, snapshot=price()),
        attempt(p=100, c=20, snapshot=price()), success=True)])
    assert result["calls"] == 1 and result["totals"]["sdk_attempts"] == 2
    assert result["prompt_tokens"] == 100 and result["completion_tokens"] == 20
    assert result["coverage"]["unknown_usage_records"] == 1
    assert result["estimates"][0]["partial"] is True and result["cost_cny"] is None


def test_actual_attempt_dates_cross_midnight_and_material_date(tmp_path):
    row = record(attempt("2026-11-15", p=10, c=1, snapshot=price()),
                 attempt("2026-11-16", p=20, c=2, snapshot=price()))
    # Filename is the write day, not the first SDK attempt day.
    write(tmp_path / "llm_metrics_2026-11-16.jsonl", [row])
    first = usage.read_usage(tmp_path, "2026-11-15", tag="synthetic-room", data_date="2026-11-14")
    second = usage.read_usage(tmp_path, "2026-11-16")
    assert first["prompt_tokens"] == 10 and second["prompt_tokens"] == 20
    assert first["calls"] == second["calls"] == 1
    assert first["totals"]["sdk_attempts"] == second["totals"]["sdk_attempts"] == 1
    all_days = usage.read_usage(tmp_path, tag="synthetic-room", data_date="2026-11-14")
    assert all_days["calls"] == 1 and all_days["totals"]["sdk_attempts"] == 2
    assert len(all_days["by_call_date"]) == 2


def test_room_purpose_filters_never_fall_back_to_global_and_dedupe():
    one = record(attempt(snapshot=price()))
    other = record(attempt(), room_tag="other-room", purpose="realtime")
    missing = record(attempt())
    missing.pop("room_tag")
    result = usage.aggregate_records([one, copy.deepcopy(one), other, missing], tag="synthetic-room", purpose="review")
    assert result["calls"] == 1 and result["prompt_tokens"] == 100
    assert result["coverage"]["unattributed_records"] == 1
    assert result["scope"]["kind"] == "room" and result["estimates"][0]["partial"] is True


@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_metric_identity_cannot_hide_unknown_usage(reverse):
    known = record(attempt(p=10, c=2, snapshot=price()))
    unknown = copy.deepcopy(known)
    unknown["sdk_attempt_usage"][0].update(prompt_tokens=None, completion_tokens=None)
    rows = [unknown, known] if reverse else [known, unknown]
    result = usage.aggregate_records(rows)
    assert result == usage.aggregate_records(list(reversed(rows)))
    assert result["coverage"]["conflicting_records"] == 1
    assert result["coverage"]["invalid_records"] == 1
    assert result["coverage"]["status"] == "unknown"
    assert result["coverage"]["price_complete"] is False
    assert result["calls"] == result["totals"]["sdk_attempts"] == 0
    assert result["prompt_tokens"] == result["completion_tokens"] == 0
    assert result["estimates"] == [] and result["cost_cny"] is None
    duplicate = usage.aggregate_records([known, copy.deepcopy(known)])
    assert duplicate["calls"] == duplicate["totals"]["sdk_attempts"] == 1
    assert duplicate["prompt_tokens"] == 10 and duplicate["completion_tokens"] == 2
    assert duplicate["coverage"]["status"] == "complete"
    assert duplicate["coverage"]["conflicting_records"] == 0


def test_bad_date_from_known_other_room_cannot_pollute_selected_scope():
    malformed = record(attempt(), room_tag="other-room", ts=None)
    malformed["sdk_attempt_usage"][0]["ts"] = None
    result = usage.aggregate_records([record(attempt(snapshot=price())), malformed],
                                    call_date="2026-11-15", tag="synthetic-room")
    assert result["coverage"]["status"] == "complete"
    assert result["coverage"]["undated_records"] == 0


def test_probes_independent_tests_not_recorded_and_no_send_explicit():
    business = record(attempt(snapshot=price()))
    probe = record(attempt(snapshot=price()), probe=True)
    nosend = record(success=False)
    result = usage.aggregate_records([business, probe, nosend])
    assert result["totals"]["sdk_attempts"] == 1 and result["calls"] == 2
    assert result["diagnostics"]["probe_records"] == 1
    assert result["diagnostics"]["connection_tests_included"] is False
    assert result["totals"]["not_sent_records"] == 1
    assert usage.aggregate_records([business, probe], include_probes=True)["totals"]["sdk_attempts"] == 2


def test_empty_bad_and_legacy_rows_never_assume_zero_cost(tmp_path):
    empty = usage.read_usage(tmp_path)
    assert empty["coverage"]["status"] == "no_records" and empty["cost_cny"] is None
    path = tmp_path / "llm_metrics_2026-11-15.jsonl"
    path.write_text('bad\n[]\n', encoding="utf-8")
    before = path.read_bytes()
    result = usage.read_usage(tmp_path)
    assert result["coverage"]["invalid_records"] == 2 and result["coverage"]["status"] == "unknown"
    assert result["cost_cny"] is None and path.read_bytes() == before


def test_crashed_task_uncovered_marker_is_read_only_and_not_extra_http(tmp_path):
    task_id = "bf_" + "a" * 64 + "_" + "b" * 32
    directory = tmp_path / "backfill_tasks"
    directory.mkdir()
    manifest = directory / ("a" * 64 + ".json")
    manifest.write_text(json.dumps({"runs": [{"task_id": task_id, "dispatch_count": 2,
        "tag": "synthetic-room", "data_date": "2026-11-14",
        "may_have_sent": True, "dispatch_settled": False}]}), encoding="utf-8")
    before = manifest.read_bytes()
    result = usage.read_usage(tmp_path, task_id=task_id)
    assert result["totals"]["sdk_attempts"] == 0
    assert result["coverage"]["uncovered_dispatches"] == 2
    assert result["coverage"]["pending_task"] is True and result["coverage"]["status"] == "unknown"
    assert manifest.read_bytes() == before and result["cost_cny"] is None


def test_date_filtered_task_does_not_mark_other_day_attempts_uncovered(tmp_path):
    task_id = "bf_" + "a" * 64 + "_" + "b" * 32
    write(tmp_path / "llm_metrics_2026-11-16.jsonl", [record(
        attempt("2026-11-15", snapshot=price()), attempt("2026-11-16", snapshot=price()), task_id=task_id)])
    directory = tmp_path / "backfill_tasks"
    directory.mkdir()
    (directory / ("a" * 64 + ".json")).write_text(json.dumps({"runs": [{"task_id": task_id,
        "tag": "synthetic-room", "data_date": "2026-11-14",
        "dispatch_count": 2, "may_have_sent": True, "dispatch_settled": True}]}), encoding="utf-8")
    result = usage.read_usage(tmp_path, "2026-11-15", task_id=task_id)
    assert result["totals"]["sdk_attempts"] == 1 and result["coverage"]["uncovered_dispatches"] == 0


def task_projection(tmp_path, *, sent=True, settled=True):
    from backfill_tasks import TaskStore
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"llm": {"enabled": True, "active_provider": "synthetic",
        "providers": {"synthetic": {"api_key": "synthetic-private-key", "model": "synthetic-model",
            "base_url": "https://example.invalid/v1"}}}}), encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    store = TaskStore(config, data)
    _, run = store.reserve_inline("synthetic-room", "2026-11-14")
    assert run is not None
    if sent:
        assert run.before_send("synthetic", "synthetic-model")
        run.progress(dispatch_settled=settled)
    elif not settled:
        run.progress(dispatch_settled=False)
    rows = [record(attempt(snapshot=price()), task_id=run.task_id)] if sent else [record(task_id=run.task_id)]
    write(data / "llm_metrics_2026-11-15.jsonl", rows)
    return data, store._path(run.task_id), run.task_id


@pytest.mark.parametrize("sent,settled,status", [(False, True, "complete"), (False, False, "complete"),
    (True, True, "complete"), (True, False, "partial")])
def test_real_task_send_states_remain_read_only(tmp_path, sent, settled, status):
    data, path, task_id = task_projection(tmp_path, sent=sent, settled=settled)
    before = {file: file.read_bytes() for file in data.rglob("*") if file.is_file()}
    result = usage.read_usage(data, task_id=task_id)
    assert result["coverage"]["status"] == status
    assert result["coverage"]["pending_task"] is (sent and not settled)
    assert not result["coverage"].get("task_state_unavailable")
    assert result["coverage"]["uncovered_dispatches"] == 0
    assert result["prompt_tokens"] == (100 if sent else 0)
    assert before == {file: file.read_bytes() for file in data.rglob("*") if file.is_file()}


@pytest.mark.parametrize("field,bad", [(field, bad)
    for field in ("may_have_sent", "dispatch_settled") for bad in ("true", None, 1)]
    + [("dispatch_count", bad) for bad in (True, -1, "1")])
def test_bad_task_send_types_preserve_known_usage_and_unknown_gap(tmp_path, field, bad):
    data, path, task_id = task_projection(tmp_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["runs"][0][field] = bad
    path.write_text(json.dumps(manifest), encoding="utf-8")
    before = {file: file.read_bytes() for file in data.rglob("*") if file.is_file()}
    result = usage.read_usage(data, task_id=task_id)
    assert result["coverage"]["status"] == "partial"
    assert result["coverage"]["task_state_unavailable"] is True
    assert result["coverage"]["pending_task"] is True
    assert result["coverage"]["price_complete"] is False and result["cost_cny"] is None
    assert result["calls"] == 1 and result["prompt_tokens"] == 100 and result["completion_tokens"] == 20
    assert result["estimates"][0]["partial"] is True
    assert before == {file: file.read_bytes() for file in data.rglob("*") if file.is_file()}


@pytest.mark.parametrize("mutation", ["duplicate", "missing", "bad_row", "bad_runs",
    "missing_tag", "bad_date", "sent_without_marker", "marker_without_sent"])
def test_unconfirmed_matching_task_evidence_is_unknown(tmp_path, mutation):
    data, path, task_id = task_projection(tmp_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    run = manifest["runs"][0]
    if mutation == "duplicate":
        manifest["runs"].append(copy.deepcopy(run))
    elif mutation == "missing":
        manifest["runs"] = []
    elif mutation == "bad_row":
        manifest["runs"].append(None)
    elif mutation == "bad_runs":
        manifest["runs"] = {"not": "a list"}
    elif mutation == "missing_tag":
        run.pop("tag")
    elif mutation == "bad_date":
        run["data_date"] = 20261114
    elif mutation == "sent_without_marker":
        run["dispatch_count"] = 0
    else:
        run["may_have_sent"] = False
    path.write_text(json.dumps(manifest), encoding="utf-8")
    before = path.read_bytes()
    result = usage.read_usage(data, task_id=task_id)
    assert result["coverage"]["task_state_unavailable"] is True
    assert result["coverage"]["pending_task"] is True and result["coverage"]["status"] == "partial"
    assert result["prompt_tokens"] == 100 and result["completion_tokens"] == 20
    assert path.read_bytes() == before


@pytest.mark.parametrize("field,bad", [("room_tag", "other-room"), ("data_date", "2026-11-13"),
    ("purpose", "realtime"), ("room_tag", None)])
def test_mismatched_metric_ownership_cannot_confirm_task_coverage(tmp_path, field, bad):
    data, path, task_id = task_projection(tmp_path)
    metrics_path = data / "llm_metrics_2026-11-15.jsonl"
    rows = [json.loads(line) for line in metrics_path.read_text(encoding="utf-8").splitlines()]
    rows.append(record(attempt(p=2, c=1), task_id=task_id, **{field: bad}))
    write(metrics_path, rows)
    before = {file: file.read_bytes() for file in data.rglob("*") if file.is_file()}
    result = usage.read_usage(data, task_id=task_id)
    assert result["coverage"]["task_state_unavailable"] is True
    assert result["coverage"]["pending_task"] is True and result["coverage"]["status"] == "partial"
    assert result["prompt_tokens"] >= 100 and result["completion_tokens"] >= 20
    assert result["coverage"]["price_complete"] is False
    assert before == {file: file.read_bytes() for file in data.rglob("*") if file.is_file()}


@pytest.mark.parametrize("markers,attempts", [(0, 1), (1, 2)])
def test_more_attempts_than_task_markers_preserve_usage_and_unknown_gap(tmp_path, markers, attempts):
    data, path, task_id = task_projection(tmp_path, sent=bool(markers))
    metrics_path = data / "llm_metrics_2026-11-15.jsonl"
    lawful = metrics_path.read_bytes()
    manifest_before = path.read_bytes()
    assert usage.read_usage(data, task_id=task_id)["coverage"]["status"] == "complete"
    write(metrics_path, [record(*[attempt(snapshot=price()) for _ in range(attempts)], task_id=task_id)])
    before = {file: file.read_bytes() for file in data.rglob("*") if file.is_file()}
    result = usage.read_usage(data, task_id=task_id)
    assert result["coverage"]["status"] == "partial"
    assert result["coverage"]["task_state_unavailable"] is True
    assert result["coverage"]["pending_task"] is True
    assert result["prompt_tokens"] == 100 * attempts and result["completion_tokens"] == 20 * attempts
    assert result["cost_cny"] is None and result["estimates"][0]["partial"] is True
    assert before == {file: file.read_bytes() for file in data.rglob("*") if file.is_file()}
    metrics_path.write_bytes(lawful)
    confirmed = usage.read_usage(data, task_id=task_id)
    assert confirmed["coverage"]["status"] == "complete"
    assert confirmed["coverage"]["pending_task"] is False
    assert not confirmed["coverage"].get("task_state_unavailable")
    assert path.read_bytes() == manifest_before


def test_review_old_data_cache_includes_new_call_day_metrics(tmp_path):
    import board_review
    first = board_review.review_tag(str(tmp_path), "2026-11-14", "synthetic-room")
    assert first["llm"]["calls"] == 0
    write(tmp_path / "llm_metrics_2026-11-16.jsonl", [record(attempt("2026-11-16", snapshot=price()))])
    second = board_review.review_tag(str(tmp_path), "2026-11-14", "synthetic-room")
    assert second["llm"]["calls"] == 1 and second["llm"]["scope"]["data_date"] == "2026-11-14"


def test_report_merge_keeps_mixed_currency_and_partial_unknown():
    first = usage.aggregate_records([record(attempt(snapshot=price()))])
    second = usage.aggregate_records([record(attempt(snapshot=price("USD", 1, 2, "b" * 32)))])
    combined = usage.merge_usage_summaries([first, second])
    assert combined["cost_cny"] is None and len(combined["estimates"]) == 2
    assert combined["prompt_tokens"] == 200
    assert "CNY" in usage.usage_text(combined) and "USD" in usage.usage_text(combined)


def test_excluded_probe_does_not_create_business_ownership_gap():
    probe = record(attempt(), probe=True)
    probe.pop("room_tag")
    result = usage.aggregate_records([record(attempt(snapshot=price())), probe], tag="synthetic-room")
    assert result["coverage"]["status"] == "complete"
    assert result["coverage"]["unattributed_records"] == 0


def test_missing_metric_identity_is_legacy_and_malformed_task_is_unknown(tmp_path):
    row = record(attempt(snapshot=price()))
    row.pop("metric_id")
    assert usage.aggregate_records([row])["coverage"]["legacy_records"] == 1
    task_id = "bf_" + "a" * 64 + "_" + "b" * 32
    (tmp_path / "backfill_tasks").mkdir()
    (tmp_path / "backfill_tasks" / ("a" * 64 + ".json")).write_text('{"runs":[null]}', encoding="utf-8")
    result = usage.read_usage(tmp_path, task_id=task_id)
    assert result["coverage"]["task_state_unavailable"] is True
    assert result["coverage"]["status"] == "unknown"


def test_legacy_dashboard_and_health_use_shared_unknown_wording(tmp_path, monkeypatch):
    import dashboard
    import health_check
    monkeypatch.setattr(health_check, "_today", lambda: "2026-11-15")
    result = health_check.check_llm_cost(str(tmp_path))
    assert result["cost_yuan"] is None and "0 元" not in result["detail"]
    assert result["status"] == health_check.STATUS_WARN
    write(tmp_path / "llm_metrics_2026-11-16.jsonl", [record(attempt("2026-11-15", snapshot=price()))])
    metrics, _ = dashboard.build_metrics(str(tmp_path), "2026-11-15")
    assert metrics["calls"] == 1 and metrics["prompt_tokens"] == 100
    assert metrics["cost_cny"] == pytest.approx(.0006)


def test_daily_report_uses_only_recorded_snapshot_for_its_local_estimate():
    import daily_report
    class Fake:
        def chat(self, *args, on_metrics=None, **kwargs):
            on_metrics(record(attempt(snapshot=price())))
            return "not valid generated material"
    result = daily_report.LlmSummarizer(Fake()).summarize({"synthetic": True})
    assert result["cost_yuan"] == pytest.approx(.0006)
    assert result["usage"]["scope"]["coverage_basis"] == "report_local_metrics_only"


def test_real_sdk_retry_projection_keeps_original_ledger_once(tmp_path, sdk, monkeypatch):
    monkeypatch.setattr(sys.modules["llm_pricing"], "estimate_known_usage", real_estimate, raising=False)
    snapshot = dict(price(input=1, output=2), provider="primary", model="model-a")
    sdk.prices.extend([snapshot, snapshot])
    sdk.outcomes.extend([TimeoutError(), response(11, 2)])
    records = []
    router, _, data = routed(tmp_path, records)
    try:
        assert router.chat("synthetic system", "synthetic question", purpose="review")
        before = {path: path.read_bytes() for path in data.glob("llm_budget_*.json")}
        result = usage.aggregate_records(records)
        assert result["calls"] == 1 and result["totals"]["sdk_attempts"] == 2
        assert result["prompt_tokens"] == 11 and result["completion_tokens"] == 2
        assert result["coverage"]["status"] == "partial"
        assert Decimal(result["estimates"][0]["amount"]) == Decimal(".000015")
        assert result["estimates"][0]["partial"] is True
        assert router._budget.check() and router._budget.last_total == 13
        assert before and all(path.read_bytes() == content for path, content in before.items())
    finally:
        router.close()


def test_real_sdk_borrow_records_are_not_extra_usage_or_budget(tmp_path, sdk, monkeypatch):
    monkeypatch.setattr(sys.modules["llm_pricing"], "estimate_known_usage", real_estimate, raising=False)
    records = []
    router, _, data = routed(tmp_path, records, budget=1, providers={
        "primary": provider(), "known": provider("synthetic-known-key", "model-b"),
        "unknown": provider("synthetic-unknown-key", "model-c")})
    router._budget.add(1, 0)
    sdk.outcomes.extend([HTTPFailure(401), TimeoutError(), TimeoutError(), TimeoutError()])
    try:
        assert router.chat("synthetic system", "synthetic question", purpose="review") is None
        before = {path: path.read_bytes() for path in data.glob("llm_budget_*.json")}
        result = usage.aggregate_records(records)
        assert len(sdk.calls) == result["totals"]["sdk_attempts"] == 4
        assert result["totals"]["not_sent_records"] == 1
        assert result["coverage"]["unknown_usage_records"] == 2
        assert result["coverage"]["status"] == "unknown"
        assert result["estimates"] == [] and result["cost_cny"] is None
        assert all(path.read_bytes() == content for path, content in before.items())
    finally:
        router.close()
