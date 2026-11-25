"""Application permissions, generated configuration and fake SDK only."""
import json
from pathlib import Path
import sys
import threading
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import board_llm
import llm_client as client_module
import llm_service
from test_llm_config_reload import fake_sdk, provider, router, save


def test_global_permission_and_independent_automatic_scope(tmp_path, fake_sdk):
    path = tmp_path / "config.json"
    routed = router(path, {"primary": provider()})
    data = tmp_path / "data"
    automatic = llm_service.load_llm_config(path).build_router(tag="a", automatic=True, request_guard=lambda: True)
    manual = llm_service.load_llm_config(path).build_router(tag="a")
    assert automatic.chat("s", "automatic defaults off") is None
    assert manual.chat("s", "manual defaults independent")
    board_llm.write_gate(data, tag_off=("a", False))
    assert automatic.chat("s", "explicitly enabled automatic")
    board_llm.write_gate(data, tag_off=("a", True))
    assert manual.chat("s", "manual still allowed")
    board_llm.write_gate(data, enabled=False)
    count = len(fake_sdk.calls)
    assert routed.chat("s", "total pause") is None
    assert manual.chat("s", "paused manual") is None
    assert automatic.chat("s", "paused auto") is None
    assert len(fake_sdk.calls) == count


def test_held_source_client_waiting_for_send_lock_is_cancelled_by_pause_cycle(tmp_path, fake_sdk):
    path = tmp_path / "config.json"
    held = router(path, {"primary": provider()}).active_client
    entered, done = threading.Event(), threading.Event()
    result = []
    original = client_module.application_control
    def observe(*args, **kwargs):
        value = original(*args, **kwargs)
        if threading.current_thread().name == "synthetic-control-waiter":
            entered.set()
        return value
    # This patch only observes the actual admission snapshot before lock wait.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(client_module, "application_control", observe)
        def call():
            try:
                result.append(held.chat("s", "queued before pause"))
            finally:
                done.set()
        with held._request_lock:
            thread = threading.Thread(target=call, name="synthetic-control-waiter")
            thread.start()
            assert entered.wait(2)
            board_llm.write_gate(tmp_path / "data", enabled=False)
            board_llm.write_gate(tmp_path / "data", enabled=True)
        thread.join(2)
        assert done.is_set() and not thread.is_alive()
    assert result == [None] and fake_sdk.calls == []
    assert held.chat("s", "independent new call")


def test_total_pause_blocks_retry_and_held_probe(tmp_path, fake_sdk):
    path = tmp_path / "config.json"
    routed = router(path, {"primary": provider()})
    held = routed.active_client
    class TemporaryFailure(RuntimeError):
        status_code = 500
    def first_failure():
        board_llm.write_gate(tmp_path / "data", enabled=False)
        raise TemporaryFailure("synthetic")
    fake_sdk.hooks.append(first_failure)
    assert routed.chat("s", "no retry after pause") is None
    assert len(fake_sdk.calls) == 1
    assert held.probe() is False
    assert len(fake_sdk.calls) == 1


def test_source_automatic_without_live_guard_cannot_send(tmp_path, fake_sdk):
    path = tmp_path / "config.json"
    save(path, {"primary": provider()})
    board_llm.write_gate(tmp_path / "data", enabled=True, tag_off=("a", False))
    automatic = llm_service.load_llm_config(path).build_router(tag="a", automatic=True)
    assert automatic.chat("s", "missing live evidence") is None
    assert fake_sdk.calls == []
    assert llm_service.load_llm_config(path).build_router(tag="a").chat("s", "manual independent")


def test_pause_cycle_during_final_credentials_read_prevents_sdk_admission(tmp_path, fake_sdk, monkeypatch):
    path = tmp_path / "config.json"
    held = router(path, {"primary": provider()}).active_client
    original = client_module._read_request_config
    reads = []
    def read(*args, **kwargs):
        value = original(*args, **kwargs)
        reads.append(True)
        if len(reads) == 3:
            board_llm.write_gate(tmp_path / "data", enabled=False)
            board_llm.write_gate(tmp_path / "data", enabled=True)
        return value
    monkeypatch.setattr(client_module, "_read_request_config", read)
    assert held.chat("s", "last credential read changes gate") is None
    assert len(reads) == 3 and fake_sdk.calls == []
    assert held.chat("s", "independent next request")
    assert len(fake_sdk.calls) == 1


def test_explicit_runtime_allow_overrides_canonical_pause_without_changing_it(tmp_path, fake_sdk):
    path = tmp_path / "config.json"
    save(path, {"primary": provider()})
    value = json.loads(path.read_text())
    value["llm"]["enabled"] = False
    path.write_text(json.dumps(value))
    routed = llm_service.load_llm_config(path).build_router()
    assert routed.chat("s", "paused") is None
    board_llm.write_gate(tmp_path / "data", enabled=True)
    assert routed.chat("s", "explicitly allowed")
    assert json.loads(path.read_text())["llm"]["enabled"] is False


@pytest.mark.parametrize("invalid", ['{"enabled":"false"}', '[]', '{broken'])
def test_invalid_gate_cannot_enable_source_bound_calls(tmp_path, fake_sdk, invalid):
    path = tmp_path / "config.json"
    routed = router(path, {"primary": provider()})
    (tmp_path / "data/llm_runtime.json").write_text(invalid)
    assert routed.chat("s", "invalid control") is None
    assert client_module.application_control(path)["reason"] == "control_unavailable"
    assert fake_sdk.calls == []


def test_active_connection_test_pause_does_not_create_worker_or_model_request(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    save(path, {"primary": provider()})
    data = tmp_path / "data"
    board_llm.write_gate(data, enabled=False)
    monkeypatch.setattr(board_llm.threading, "Thread", lambda *a, **k: pytest.fail("paused test started worker"))
    result = board_llm.test_provider_connection(path, data, {
        "provider": "primary", "protocol": "openai_compatible", "base_url": "https://example.invalid",
        "model": "synthetic", "api_key": "synthetic"})
    assert result["code"] == "global_paused"
    assert board_llm.delete_provider_key(path, data, {"provider": "primary"})["ok"]


def test_cli_config_off_persists_pause_and_config_on_does_not_resume(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    save(path, {"primary": provider()})
    monkeypatch.setattr(board_llm, "__file__", str(tmp_path / "scripts/board_llm.py"))
    assert board_llm._main(["config-off"]) == 0
    paused = client_module.application_control(path)
    assert paused["allowed"] is False and paused["epoch"][0] == 1
    assert board_llm._main(["config-on"]) == 0
    assert client_module.application_control(path) == paused


@pytest.mark.parametrize("change", ["stale", "wrong_pid", "wrong_room", "secret_reason", "bad_epoch", "old_epoch", "old_live"])
def test_worker_status_requires_fresh_matching_identity_and_safe_reason(tmp_path, change):
    path = tmp_path / "config.json"
    save(path, {"primary": provider()})
    data = tmp_path / "data"
    board_llm.write_gate(data, tag_off=("a", False))
    now = time.time()
    record = {"room_tag": "a", "pid": 1234, "process_created_at": now - 10, "observed_at": now,
              "worker_alive": True, "collector_alive": True, "thread_ident": 10,
              "thread_native_id": 11, "generation": 1, "epoch": [0, 0, 0],
              "live_ready": True, "live_generation": 1, "worker_live_generation": 1,
              "state": "waiting_input", "reason": "worker_waiting_input"}
    if change == "stale": record["observed_at"] = now - 20
    if change == "wrong_room": record["room_tag"] = "other"
    if change == "secret_reason": record["reason"] = "synthetic-private-key"
    if change == "bad_epoch": record["epoch"] = ["synthetic-private-key"]
    if change == "old_epoch": record["epoch"] = [0, 1, 0]
    if change == "old_live": record["live_generation"] = 2
    (data / "llm_worker_a.json").write_text(json.dumps(record))
    result = board_llm.automatic_status(data, path, "a", is_live=True,
        identity_reader=lambda _: None if change == "wrong_pid" else now - 10)
    if change == "secret_reason":
        assert result["worker_state"]["state"] == "waiting_input"
        assert "synthetic-private-key" not in json.dumps(result)
    elif change == "old_epoch":
        assert result["worker_state"]["state"] == "stopping"
        assert result["worker_state"]["reason"] == "control_epoch_changed"
    elif change == "old_live":
        assert result["worker_state"]["state"] == "stopping"
        assert result["worker_state"]["reason"] == "live_generation_changed"
    else:
        assert result["worker_state"]["state"] == "unknown"
        assert result["worker_state"]["worker_alive"] is False
