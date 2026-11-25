"""Budget accounting uses temporary synthetic ledgers and real child processes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

import llm_client
from llm_client import BudgetLedgerError, DailyTokenBudget, LLMRouter
from file_lock import file_lock


ROOT = Path(__file__).resolve().parents[1]


def test_process_updates_preserve_every_usage_increment(tmp_path):
    script = """
import sys
from llm_client import DailyTokenBudget
budget = DailyTokenBudget(1000000, sys.argv[1])
for i in range(12):
    budget.add(2, 3)
    assert budget.check()
"""
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
    children = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path)],
                                cwd=str(tmp_path), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                for _ in range(4)]
    try:
        for child in children:
            out, err = child.communicate(timeout=30)
            assert child.returncode == 0, err.decode("utf-8", errors="replace")
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
    fresh = DailyTokenBudget(1000000, str(tmp_path))
    assert fresh.check() and fresh.last_total == 4 * 12 * 5
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("payload", [
    b'{"date":', b'[]', b'{"total": 0}',
    b'{"date": "wrong-day", "total": 1}',
    b'{"total": -1}', b'{"total": true}', b'{"total": "1"}',
])
def test_existing_invalid_ledger_never_becomes_zero_or_gets_overwritten(tmp_path, payload):
    budget = DailyTokenBudget(100, str(tmp_path))
    path = Path(budget._path())
    if payload.startswith(b'{"total":') and payload != b'{"total": 0}':
        obj = json.loads(payload)
        obj["date"] = time.strftime("%Y-%m-%d")
        payload = json.dumps(obj).encode("utf-8")
    path.write_bytes(payload)
    with pytest.raises(BudgetLedgerError):
        budget.check()
    with pytest.raises(BudgetLedgerError):
        budget.add(1, 1)
    assert path.read_bytes() == payload


def test_failed_replace_preserves_old_ledger_and_pending_usage_evidence(tmp_path, monkeypatch):
    budget = DailyTokenBudget(100, str(tmp_path))
    budget.add(5, 0)
    path = Path(budget._path())
    original = path.read_bytes()
    def fail(*_args):
        raise PermissionError("synthetic replace failure")
    monkeypatch.setattr(llm_client.os, "replace", fail)
    with pytest.raises(BudgetLedgerError):
        budget.add(7, 3)
    assert path.read_bytes() == original
    pending = list(tmp_path.glob(path.name + ".*.tmp"))
    assert len(pending) == 1
    assert json.loads(pending[0].read_text(encoding="utf-8"))["total"] == 15
    # A new process/instance must not rely on the surviving old total of five.
    with pytest.raises(BudgetLedgerError):
        DailyTokenBudget(100, str(tmp_path)).check()


def test_lock_deadline_preserves_total_and_delta_then_allows_explicit_recovery(tmp_path, monkeypatch):
    budget = DailyTokenBudget(100, str(tmp_path))
    budget.add(5, 0)
    path = Path(budget._path())
    ready, release = threading.Event(), threading.Event()
    def hold():
        with file_lock(path):
            ready.set()
            assert release.wait(timeout=5)
    holder = threading.Thread(target=hold)
    holder.start()
    assert ready.wait(timeout=2)
    monkeypatch.setattr(llm_client, "file_lock", lambda resource: file_lock(resource, timeout=0.03))
    try:
        with pytest.raises(BudgetLedgerError) as failure:
            budget.add(7, 3)
        assert isinstance(failure.value.__cause__, TimeoutError)
    finally:
        release.set()
        holder.join(timeout=2)
    assert not holder.is_alive()
    assert json.loads(path.read_text(encoding="utf-8"))["total"] == 5
    pending = list(tmp_path.glob(path.name + ".failed.*.tmp"))
    assert len(pending) == 1
    delta = json.loads(pending[0].read_text(encoding="utf-8"))
    assert delta["unaccounted_prompt_tokens"] + delta["unaccounted_completion_tokens"] == 10
    with pytest.raises(BudgetLedgerError):
        DailyTokenBudget(100, str(tmp_path)).check()
    # Synthetic operator reconciliation: retain the recorded delta in the ledger
    # before clearing its evidence. A new worker can then resume without a reset.
    with file_lock(path, timeout=0.2):
        reconciled = json.loads(path.read_text(encoding="utf-8"))
        reconciled["total"] += 10
        path.write_text(json.dumps(reconciled), encoding="utf-8")
        pending[0].unlink()
    fresh = DailyTokenBudget(100, str(tmp_path))
    assert fresh.check() and fresh.last_total == 15


def test_unknown_ledger_blocks_router_without_borrowing_backup(tmp_path, monkeypatch):
    seen = []
    class Client:
        def __init__(self, **kwargs):
            self.model = kwargs["model"]
        def chat(self, *_args, **_kwargs):
            pytest.fail("a provider was called with unknown budget usage")
        def _emit_metrics(self, **kwargs):
            kwargs["on_metrics"]({"error_type": kwargs["error_type"]})
    monkeypatch.setattr(llm_client, "LLMClient", Client)
    providers = {name: {"api_key": "synthetic", "base_url": "https://example.invalid",
                        "model": "synthetic"} for name in ("primary", "backup")}
    router = LLMRouter(providers, "primary", daily_token_budget=100,
                       budget_dir=str(tmp_path), on_metrics=seen.append)
    Path(router._budget._path()).write_bytes(b'{"broken":')
    assert router.chat("synthetic", "synthetic") is None
    assert router.active == "primary" and router.consecutive_failures == 0
    assert any(row.get("error_type") == "budget_ledger_error" for row in seen)


def test_failed_accounting_is_visible_and_keeps_outer_metrics(tmp_path, caplog):
    seen = []
    router = LLMRouter({}, "", on_metrics=seen.append)
    class BrokenBudget:
        def add(self, *_args):
            raise BudgetLedgerError("synthetic unavailable ledger")
    router._budget = BrokenBudget()
    rec = {"prompt_tokens": 1, "completion_tokens": 2, "provider": ""}
    router._on_metrics(rec)
    assert seen == [rec]
    assert "synthetic unavailable ledger" in caplog.text
