"""Backfill durable-dispatch admission on the real router, fake SDK only."""
import json
import threading
from pathlib import Path

import pytest

from test_llm_config_reload import fake_sdk, provider, save
import llm_service


def build(tmp_path, hook, **kwargs):
    config = tmp_path / "config.json"
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    save(config, {"primary": provider()})
    result = llm_service.load_llm_config(str(config)).build_router(
        budget_dir=str(data), control_data_dir=str(data), tag="synthetic",
        on_dispatch=hook, **kwargs)
    return result, config, data


@pytest.mark.parametrize("mode,reason", [
    ("raise", "task_state_unavailable"), ("false", "task_blocked")])
def test_durable_marker_failure_never_sends_or_retries(tmp_path, fake_sdk, mode, reason):
    marked = []
    def hook(**fields):
        marked.append(fields)
        if mode == "raise":
            raise OSError("synthetic private diagnostic")
        return False
    router, _, _ = build(tmp_path, hook)
    try:
        assert router.chat("system", "synthetic", purpose="review") is None
        assert len(marked) == 1 and fake_sdk.calls == []
        assert router.last_block_reason == reason
    finally:
        router.close()


@pytest.mark.parametrize("change,reason", [
    ("pause", "global_paused"), ("fast_resume", "control_changed"),
    ("credential", "credentials_changed"), ("lease", "task_blocked")])
def test_final_admission_rechecks_after_durable_io(tmp_path, fake_sdk, change, reason):
    allowed, marked = [True], []
    def hook(**fields):
        marked.append(fields)
        if change == "credential":
            save(config, {"primary": provider("synthetic-replacement")})
        elif change == "lease":
            allowed[0] = False
        else:
            (data / "llm_runtime.json").write_text(json.dumps({
                "enabled": change == "fast_resume", "pause_generation": 1,
            }), encoding="utf-8")
    router, config, data = build(tmp_path, hook, request_guard=lambda: allowed[0])
    try:
        assert router.chat("system", "synthetic", purpose="review") is None
        assert len(marked) == 1 and fake_sdk.calls == []
        assert router.last_block_reason == reason
    finally:
        router.close()


def test_each_real_sdk_attempt_has_its_own_marker(tmp_path, fake_sdk):
    marked = []
    router, _, _ = build(tmp_path, lambda **fields: marked.append(fields))
    def timeout():
        raise TimeoutError("synthetic uncertain transport")
    fake_sdk.hooks.extend([timeout, timeout])
    try:
        assert router.chat("system", "synthetic", purpose="review") == "synthetic reply"
        assert len(marked) == len(fake_sdk.calls) == 3
        assert all(row == {"provider": "primary", "model": "model-a"} for row in marked)
        assert router.last_block_reason == ""
    finally:
        router.close()


def test_block_reason_is_per_calling_thread(tmp_path, fake_sdk):
    def hook(**fields):
        if threading.current_thread().name == "blocked":
            return False
    router, _, _ = build(tmp_path, hook)
    results = {}
    def call():
        name = threading.current_thread().name
        answer = router.chat("system", "synthetic", purpose="review")
        results[name] = (answer, router.last_block_reason)
    threads = [threading.Thread(target=call, name=name) for name in ("blocked", "allowed")]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
            assert not thread.is_alive()
        assert results == {"blocked": (None, "task_blocked"),
                           "allowed": ("synthetic reply", "")}
        assert len(fake_sdk.calls) == 1
    finally:
        router.close()


@pytest.mark.parametrize("corrupt,reason", [(False, "daily_budget_exhausted"),
                                          (True, "budget_ledger_error")])
def test_durable_io_cannot_keep_an_expired_budget_approval(tmp_path, fake_sdk, corrupt, reason):
    config, data = tmp_path / "config.json", tmp_path / "data"
    data.mkdir()
    save(config, {"primary": provider()})
    raw = json.loads(config.read_text(encoding="utf-8"))
    raw["llm"]["daily_token_budget"] = 10
    config.write_text(json.dumps(raw), encoding="utf-8")
    marked = []
    def hook(**fields):
        marked.append(fields)
        if corrupt:
            Path(router._budget._path()).write_text("{", encoding="utf-8")
        else:
            router._budget.add(10, 0)
    router = llm_service.load_llm_config(str(config)).build_router(
        budget_dir=str(data), control_data_dir=str(data), on_dispatch=hook)
    try:
        assert router.chat("system", "synthetic", purpose="review") is None
        assert len(marked) == 1 and fake_sdk.calls == []
        assert router.last_block_reason == reason
    finally:
        router.close()
