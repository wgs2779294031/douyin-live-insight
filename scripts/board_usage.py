"""Read-only projections of existing LLM metrics; never a budget or billing ledger.

Version-2 attempt timestamps define the local call date. A logical metric may
touch two dates; its record count is therefore not an HTTP-attempt count.
Historical usage without attempt evidence remains explicitly incomplete.
"""
from __future__ import annotations

from datetime import date as Date, datetime
from decimal import Decimal, InvalidOperation, localcontext
import json
import hashlib
from pathlib import Path
import re
import sys

_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

_METRIC_FILE = re.compile(r"^llm_metrics_(\d{4}-\d{2}-\d{2})\.jsonl$")
_TASK_ID = re.compile(r"^bf_([0-9a-f]{64})_[0-9a-f]{32}$")


class UsageError(ValueError):
    code = "invalid_usage_filter"


def _day(value):
    if value is None or value == "":
        return None
    try:
        if not isinstance(value, str) or Date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except (ValueError, TypeError):
        raise UsageError("日期筛选无效。") from None
    return value


def _text(value):
    return value if isinstance(value, str) and len(value) <= 256 and not any(ord(c) < 32 for c in value) else ""


def _tokens(value):
    return value if type(value) is int and value >= 0 else None


def _call_day(value):
    if type(value) not in (int, float):
        return None
    try:
        return datetime.fromtimestamp(value).date().isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def _decimal(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except (ValueError, InvalidOperation):
        return None


def _estimate(item):
    # The sole price implementation belongs to llm_pricing. No config fallback.
    try:
        from llm_pricing import estimate_known_usage
        result = estimate_known_usage(item)
    except Exception:
        return None
    return result if isinstance(result, dict) else None


def _diagnostic(record):
    if record.get("probe") is True:
        return "probe"
    if record.get("test") is True or record.get("purpose") in ("connection_test", "test"):
        return "test"
    return None


def _filters(call_date, tag, task_id, purpose, data_date):
    call_date, data_date = _day(call_date), _day(data_date)
    for value in (tag, purpose):
        if value is not None and (not isinstance(value, str) or not value or not _text(value)):
            raise UsageError("用量筛选无效。")
    if task_id is not None and (not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id)):
        raise UsageError("任务标识无效。")
    return {"kind": "task" if task_id else "room" if tag else "global",
            "call_date": call_date, "data_date": data_date, "room_tag": tag,
            "task_id": task_id, "purpose": purpose, "date_basis": "sdk_attempt_time",
            "coverage_basis": "recorded_metrics_only"}


def _attempts(record):
    values = record.get("sdk_attempt_usage")
    valid = (type(record.get("metrics_version")) is int and record["metrics_version"] == 2 and isinstance(values, list)
             and isinstance(record.get("metric_id"), str)
             and re.fullmatch(r"[0-9a-f]{32}", record["metric_id"]) is not None
             and len(values) <= 3 and all(isinstance(item, dict) for item in values)
             and type(record.get("sdk_attempts")) is int
             and record["sdk_attempts"] == len(values)
             and type(record.get("sent")) is bool
             and record["sent"] == bool(values))
    return values if valid else None


def _conflicting_metric_ids(records):
    """Exact duplicate payloads dedupe; conflicting identities are untrusted."""
    signatures, conflicts = {}, set()
    for record in records:
        if not isinstance(record, dict):
            continue
        metric_id = record.get("metric_id")
        if not isinstance(metric_id, str) or not re.fullmatch(r"[0-9a-f]{32}", metric_id):
            continue
        try:
            payload = json.dumps(record, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
            signature = hashlib.sha256(payload.encode("utf-8")).digest()
        except (TypeError, ValueError):
            # Non-JSON payloads cannot be accepted as evidence for this identity.
            conflicts.add(metric_id)
            continue
        if metric_id in signatures and signatures[metric_id] != signature:
            conflicts.add(metric_id)
        signatures.setdefault(metric_id, signature)
    return conflicts


def aggregate_records(records, *, call_date=None, tag=None, task_id=None, purpose=None,
                      data_date=None, include_probes=False, invalid_records=0,
                      source_complete=True):
    """Aggregate recorded evidence only, with no model/config/ledger writes.

    ``calls`` is retained for old clients and means metric records, never task
    generate count or actual HTTP count. Known token components can be partial.
    """
    scope = _filters(call_date, tag, task_id, purpose, data_date)
    if type(include_probes) is not bool:
        raise UsageError("用量筛选无效。")
    scope["include_probes"] = include_probes
    records = list(records)
    conflicting_ids = _conflicting_metric_ids(records)
    reported_conflicts = set()
    totals = {"metric_records": 0, "business_records": 0, "probe_records": 0,
              "test_records": 0, "not_sent_records": 0, "failed_records": 0,
              "successful_records": 0, "latency_ms_total": 0, "latency_records": 0,
              "sdk_attempts": 0, "known_prompt_tokens": 0, "known_completion_tokens": 0,
              "known_total_tokens": 0}
    coverage = {"status": "no_records", "known_usage_records": 0,
                "unknown_usage_records": 0, "legacy_records": 0,
                "invalid_records": max(0, invalid_records), "conflicting_records": 0,
                "unattributed_records": 0,
                "undated_records": 0, "uncovered_dispatches": 0, "pending_task": False,
                "source_complete": source_complete is True}
    diagnostics = {"probe_records": 0, "test_records": 0, "excluded": not include_probes,
                   "connection_tests_included": False, "connection_tests_status": "not_recorded",
                   "daily_report_included": False, "daily_report_status": "report_local_only"}
    prices, dates, providers = {}, {}, {}
    unknown_price_components = 0
    known_components = 0
    legacy_token_components = 0
    seen_ids = set()

    for record in records:
        if not isinstance(record, dict):
            coverage["invalid_records"] += 1
            continue
        ownership = (("room_tag", tag), ("task_id", task_id),
                     ("purpose", purpose), ("data_date", data_date))
        if any(expected is not None and record.get(key) and record.get(key) != expected
               for key, expected in ownership):
            continue
        attempts = _attempts(record)
        legacy = attempts is None
        if attempts and any(_call_day(item.get("ts")) is None for item in attempts):
            coverage["undated_records"] += 1
        selected = ([item for item in attempts if not call_date or _call_day(item.get("ts")) == call_date]
                    if attempts is not None else [])
        no_send = attempts == []
        record_day = _call_day(record.get("ts"))
        if (legacy or no_send) and record_day is None:
            coverage["undated_records"] += 1
        if call_date and not selected and not (no_send and record_day == call_date):
            if not legacy or record_day != call_date:
                continue
        # Missing ownership is a coverage gap, never a reason to use global totals.
        missing = any(expected is not None and not record.get(key) for key, expected in ownership)
        diagnostic = _diagnostic(record)
        if diagnostic and not include_probes:
            if not missing:
                diagnostics[diagnostic + "_records"] += 1
            continue
        if missing:
            coverage["unattributed_records"] += 1
            continue
        if any(expected is not None and record.get(key) != expected for key, expected in ownership):
            continue
        metric_id = record.get("metric_id")
        if isinstance(metric_id, str) and metric_id in conflicting_ids:
            # Count each conflicting logical identity once and exclude every
            # variant, so record order cannot choose the known/unknown winner.
            if metric_id not in reported_conflicts:
                reported_conflicts.add(metric_id)
                coverage["invalid_records"] += 1
                coverage["conflicting_records"] += 1
            continue
        if isinstance(metric_id, str) and 8 <= len(metric_id) <= 128:
            if metric_id in seen_ids:
                continue
            seen_ids.add(metric_id)
        if diagnostic:
            diagnostics[diagnostic + "_records"] += 1
            if not include_probes:
                continue
        totals["metric_records"] += 1
        totals[(diagnostic + "_records") if diagnostic else "business_records"] += 1
        totals["failed_records"] += record.get("success") is False
        totals["successful_records"] += record.get("success") is True
        latency = _tokens(record.get("latency_ms"))
        if latency is not None:
            totals["latency_ms_total"] += latency
            totals["latency_records"] += 1
        pkey = (_text(record.get("provider")) or "unknown", _text(record.get("model")) or "unknown")
        provider = providers.setdefault(pkey, {"provider": pkey[0], "model": pkey[1],
            "metric_records": 0, "sdk_attempts": 0, "known_prompt_tokens": 0,
            "known_completion_tokens": 0})
        provider["metric_records"] += 1
        if no_send:
            totals["not_sent_records"] += 1
            coverage["known_usage_records"] += 1
            continue
        if legacy:
            coverage["legacy_records"] += 1
            coverage["unknown_usage_records"] += 1
            # Old integers are reported partial usage, not proof of complete billing.
            for old, new in (("prompt_tokens", "known_prompt_tokens"),
                             ("completion_tokens", "known_completion_tokens")):
                value = _tokens(record.get(old))
                if value is not None:
                    totals[new] += value
                    provider[new] += value
                    legacy_token_components += 1
            # Even an old price-shaped field is not a versioned send snapshot.
            unknown_price_components += 2
            continue
        fully_known = True
        for attempt in selected:
            day = _call_day(attempt.get("ts"))
            if day is None:
                fully_known = False
            day_total = dates.setdefault(day or "unknown", {"call_date": day,
                "sdk_attempts": 0, "known_prompt_tokens": 0, "known_completion_tokens": 0})
            totals["sdk_attempts"] += 1
            provider["sdk_attempts"] += 1
            day_total["sdk_attempts"] += 1
            for old, new in (("prompt_tokens", "known_prompt_tokens"),
                             ("completion_tokens", "known_completion_tokens")):
                value = _tokens(attempt.get(old))
                if value is None:
                    fully_known = False
                else:
                    known_components += 1
                    totals[new] += value
                    provider[new] += value
                    day_total[new] += value
            estimate = _estimate(dict(attempt, provider=record.get("provider"), model=record.get("model")))
            currency = estimate.get("currency") if estimate else None
            if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3,8}", currency):
                unknown_price_components += 2
                continue
            amount = _decimal(estimate.get("amount"))
            if amount is None:
                unknown_price_components += 2
                continue
            group = prices.setdefault(currency, {"currency": currency, "amount": Decimal(0),
                "priced_components": 0, "unknown_price_components": 0,
                "price_versions": set(), "price_snapshots": {}})
            with localcontext() as context:
                context.prec = 80
                group["amount"] += amount
            for name in ("input", "output"):
                if estimate.get(name + "_priced") is True:
                    group["priced_components"] += 1
                else:
                    group["unknown_price_components"] += 1
                    unknown_price_components += 1
            version = _text(estimate.get("version"))
            if version:
                group["price_versions"].add(version)
                group["price_snapshots"][version] = {"version": version,
                    "source": "user_entered" if estimate.get("source") == "user_entered" else "unknown",
                    "saved_at": _text(estimate.get("saved_at")) or None}
        coverage["known_usage_records" if fully_known else "unknown_usage_records"] += 1

    estimates = []
    gaps = (coverage["unknown_usage_records"] + coverage["invalid_records"]
            + coverage["unattributed_records"] + coverage["undated_records"])
    if totals["metric_records"]:
        coverage["status"] = ("complete" if not gaps and source_complete else
                              "partial" if known_components or legacy_token_components else "unknown")
    elif coverage["invalid_records"] or coverage["unattributed_records"] or coverage["undated_records"] or not source_complete:
        coverage["status"] = "unknown"
    for currency, group in sorted(prices.items()):
        group["amount"] = format(group["amount"], "f")
        group["price_versions"] = sorted(group["price_versions"])
        group["price_snapshots"] = [group["price_snapshots"][key] for key in sorted(group["price_snapshots"])]
        group["partial"] = bool(gaps or unknown_price_components or not source_complete)
        estimates.append(group)
    coverage["unknown_price_components"] = unknown_price_components
    coverage["price_complete"] = bool(totals["metric_records"] and not unknown_price_components and not gaps and source_complete)
    totals["known_total_tokens"] = totals["known_prompt_tokens"] + totals["known_completion_tokens"]
    result = {"scope": scope, "totals": totals, "coverage": coverage, "estimates": estimates,
              "diagnostics": diagnostics, "by_call_date": [dates[key] for key in sorted(dates)],
              "by_provider": list(providers.values()),
              "calls": totals["metric_records"], "prompt_tokens": totals["known_prompt_tokens"],
              "completion_tokens": totals["known_completion_tokens"],
              "total_tokens": totals["known_total_tokens"], "cost_cny": None,
              "note": "仅汇总已记录指标；模型记录数不等于画像点或HTTP尝试数，缺失记录与usage不代表免费。"}
    if len(estimates) == 1 and estimates[0]["currency"] == "CNY" and not estimates[0]["partial"]:
        result["cost_cny"] = float(estimates[0]["amount"])
    return result


def usage_text(usage):
    """Shared plain-text estimate wording for existing report/health exports."""
    if not isinstance(usage, dict):
        return "用量与估算费用未知"
    estimates = usage.get("estimates") or []
    if not estimates:
        return "无估价依据；已记录用量覆盖" + ("完整" if usage.get("coverage", {}).get("status") == "complete" else "不完整或未知")
    parts = [f"{item['currency']} {item['amount']}" + ("（仅已知部分）" if item.get("partial") else "")
             for item in estimates]
    return "按发送时用户单价快照估算：" + "；".join(parts) + "（非提供商账单）"


def merge_usage_summaries(summaries):
    """Sum disjoint report sections, never re-price or mix currencies."""
    summaries = list(summaries)
    result = aggregate_records([])
    result["scope"]["kind"] = "report"
    result["scope"]["coverage_basis"] = "report_local_metrics_only"
    prices = {}
    for summary in summaries:
        for key in result["totals"]:
            result["totals"][key] += summary.get("totals", {}).get(key, 0)
        for key in ("known_usage_records", "unknown_usage_records", "legacy_records", "invalid_records", "conflicting_records",
                    "unattributed_records", "undated_records", "uncovered_dispatches", "unknown_price_components"):
            result["coverage"][key] += summary.get("coverage", {}).get(key, 0)
        for estimate in summary.get("estimates", []):
            currency = estimate["currency"]
            group = prices.setdefault(currency, dict(estimate, amount=Decimal(0), priced_components=0,
                unknown_price_components=0, price_versions=set(), price_snapshots={}))
            with localcontext() as context:
                context.prec = 80
                group["amount"] += Decimal(estimate["amount"])
            group["partial"] = group["partial"] or estimate["partial"]
            group["priced_components"] += estimate["priced_components"]
            group["unknown_price_components"] += estimate["unknown_price_components"]
            group["price_versions"].update(estimate["price_versions"])
            group["price_snapshots"].update({item["version"]: item for item in estimate["price_snapshots"]})
    complete = bool(summaries) and all(item["coverage"]["status"] == "complete" for item in summaries)
    result["coverage"]["status"] = "complete" if complete else "partial" if summaries else "no_records"
    result["coverage"]["price_complete"] = bool(summaries) and all(item["coverage"]["price_complete"] for item in summaries)
    for currency, group in sorted(prices.items()):
        group["amount"] = format(group["amount"], "f")
        group["price_versions"] = sorted(group["price_versions"])
        group["price_snapshots"] = [group["price_snapshots"][key] for key in sorted(group["price_snapshots"])]
        group["partial"] = group["partial"] or not result["coverage"]["price_complete"]
        result["estimates"].append(group)
    result["calls"] = result["totals"]["metric_records"]
    result["prompt_tokens"] = result["totals"]["known_prompt_tokens"]
    result["completion_tokens"] = result["totals"]["known_completion_tokens"]
    result["total_tokens"] = result["totals"]["known_total_tokens"]
    if len(result["estimates"]) == 1 and result["estimates"][0]["currency"] == "CNY" and not result["estimates"][0]["partial"]:
        result["cost_cny"] = float(result["estimates"][0]["amount"])
    return result


def metrics_fingerprint(data_dir):
    """All call-date files participate, including today's backfill of old data."""
    result = []
    try:
        for path in sorted(Path(data_dir).glob("llm_metrics_*.jsonl")):
            if _METRIC_FILE.fullmatch(path.name):
                try:
                    stat = path.stat()
                    result.append((path.name, stat.st_mtime_ns, stat.st_size))
                except OSError:
                    result.append((path.name, None, None))
    except OSError:
        return (("unavailable", None, None),)
    return tuple(result)


def _task_coverage(result, data_dir, task_id, records):
    if not task_id:
        return
    path = Path(data_dir) / "backfill_tasks" / (_TASK_ID.fullmatch(task_id).group(1) + ".json")
    try:
        if path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError
        manifest = json.loads(path.read_text(encoding="utf-8-sig"))
        runs = manifest["runs"]
        if (not isinstance(runs, list)
                or any(not isinstance(row, dict) or not isinstance(row.get("task_id"), str)
                       for row in runs)):
            raise ValueError
        matches = [row for row in runs if row["task_id"] == task_id]
        if len(matches) != 1:
            raise ValueError
        record = matches[0]
        markers = record.get("dispatch_count")
        sent, settled = record.get("may_have_sent"), record.get("dispatch_settled")
        tag, data_date = record.get("tag"), _day(record.get("data_date"))
        if (type(markers) is not int or markers < 0
                or type(sent) is not bool or type(settled) is not bool
                or sent != bool(markers)
                or not _text(tag) or data_date is None):
            raise ValueError
        # Only the same task's material ownership can cover its durable markers.
        # Call-date filtering is deliberately absent: a task can span midnight.
        all_task_attempts, seen_ids = 0, set()
        conflicting_ids = _conflicting_metric_ids(records)
        for metric in records:
            if metric.get("task_id") != task_id:
                continue
            if (metric.get("room_tag") != tag or metric.get("data_date") != data_date
                    or metric.get("purpose") != "review"):
                raise ValueError
            metric_id = metric.get("metric_id")
            if isinstance(metric_id, str) and (metric_id in conflicting_ids or metric_id in seen_ids):
                continue
            if isinstance(metric_id, str):
                seen_ids.add(metric_id)
            attempts = _attempts(metric)
            if attempts is not None:
                all_task_attempts += len(attempts)
        if all_task_attempts > markers:
            raise ValueError
        # Marker writes precede actual SDK sends, so this is a conservative gap,
        # explicitly not an additional HTTP count or token estimate.
        uncovered = max(0, markers - all_task_attempts)
        pending = sent and not settled
    except (OSError, ValueError, KeyError, TypeError, AttributeError, StopIteration):
        result["coverage"]["task_state_unavailable"] = True
        uncovered, pending = 0, True
    result["coverage"].update(uncovered_dispatches=uncovered, pending_task=pending)
    if uncovered or pending:
        result["coverage"]["status"] = "partial" if result["totals"]["metric_records"] else "unknown"
        result["coverage"]["price_complete"] = False
        result["cost_cny"] = None
        for group in result["estimates"]:
            group["partial"] = True


def read_usage(data_dir, call_date=None, *, tag=None, task_id=None, purpose=None,
               data_date=None, include_probes=False):
    """Read existing metrics and (only for task lookup) conservative lease evidence."""
    _filters(call_date, tag, task_id, purpose, data_date)
    records, invalid, complete = [], 0, True
    root = Path(data_dir)
    try:
        paths = sorted(root.glob("llm_metrics_*.jsonl"))
        for path in paths:
            if not _METRIC_FILE.fullmatch(path.name):
                continue
            if path.is_symlink():
                complete = False
                continue
            try:
                with path.open(encoding="utf-8-sig", errors="strict") as stream:
                    for line in stream:
                        if not line.strip():
                            continue
                        try:
                            if len(line) > 1024 * 1024:
                                raise ValueError
                            item = json.loads(line)
                            if not isinstance(item, dict):
                                raise ValueError
                            records.append(item)
                        except (ValueError, TypeError):
                            invalid += 1
            except (OSError, UnicodeError):
                complete = False
    except OSError:
        complete = False
    result = aggregate_records(records, call_date=call_date, tag=tag, task_id=task_id,
        purpose=purpose, data_date=data_date, include_probes=include_probes,
        invalid_records=invalid, source_complete=complete)
    _task_coverage(result, data_dir, task_id, records)
    return result
