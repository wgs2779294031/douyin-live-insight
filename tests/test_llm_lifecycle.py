"""Generated configs, synthetic adapters and fake SDK; no external I/O."""
import json
from pathlib import Path
import queue
import socket
import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llm_client as clients
import llm_service as services
import room_runtime as rooms
from analyzer import AnalyzerConfig
from config import Config, RoomConfig
from feishu_sink import FeishuSinkConfig


def eventually(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def write_config(path, key="synthetic-key", enabled=False):
    path.write_text(json.dumps({"llm": {
        "enabled": enabled, "active_provider": "synthetic",
        "daily_token_budget": 0, "coldline_min_interval_seconds": 0,
        "providers": {"synthetic": {"api_key": key, "base_url": "https://example.invalid/v1",
                                    "model": "synthetic-model", "protocol": "openai_compatible"}},
    }}), encoding="utf-8")


def write_gate(data, enabled=True, tags=None, generation=0, tag_generation=0):
    (data / clients.GATE_FILENAME).write_text(json.dumps({
        "enabled": enabled, "tags_on": ["synthetic"] if tags is None else tags,
        "pause_generation": generation, "tag_pause_generation": {"synthetic": tag_generation},
    }), encoding="utf-8")
    clients.invalidate_gate_cache()


def snapshot(label):
    return {"kind": "snapshot", "snapshot": {"ts": 1, "marker": label,
            "cold_window": {"count": 2, "window_seconds": 60}}}


def confirm_live(shared):
    connection = shared.begin_live_connection()
    shared.set_live_status(2, connection)
    shared.set_live_connected(True, connection)
    return connection


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    # ssl must inherit the real socket class; client imports it through requests.
    import ssl
    import client
    calls, sdk_instances, hooks, runtimes, adapters = [], [], [], [], []
    def denied(*args, **kwargs):
        raise AssertionError("network forbidden in lifecycle tests")
    monkeypatch.setattr(socket, "socket", denied)
    monkeypatch.setattr(socket, "create_connection", denied)

    class SDK:
        def __init__(self, api_key, **kwargs):
            self.key, self.closed = api_key, False
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
            sdk_instances.append(self)
        def create(self, **kwargs):
            calls.append({"key": self.key, **kwargs})
            if hooks:
                hooks.pop(0)()
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content='{"lines":["synthetic line"]}'))],
                usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0))
        def close(self):
            self.closed = True
    module = ModuleType("openai")
    module.OpenAI = SDK
    monkeypatch.setitem(sys.modules, "openai", module)

    class Adapter:
        msg_count = 1
        def __init__(self):
            self.stopped, self.ready = threading.Event(), threading.Event()
        def run(self):
            self.ready.set()
            self.stopped.wait(10)
        def close(self):
            self.stopped.set()
    def adapter(*args, **kwargs):
        result = Adapter()
        adapters.append(result)
        return result
    def analyzer(q, *args, **kwargs):
        while True:
            try:
                if q.get(timeout=0.05) is None:
                    return
            except queue.Empty:
                continue
    monkeypatch.setattr(rooms, "build_adapter", adapter)
    monkeypatch.setattr(rooms, "run_analyzer", analyzer)
    clients.invalidate_gate_cache()

    def runtime(key="synthetic-key", enabled=False, room_llm=False, live=True):
        base = tmp_path / str(len(runtimes))
        base.mkdir()
        data = base / "data"
        data.mkdir()
        path = base / "config.json"
        write_config(path, key, enabled)
        rt = rooms.RoomRuntime(
            room=RoomConfig(room_id="synthetic-room", label="synthetic", llm=room_llm),
            cfg=Config(web_rid="synthetic-room", output_dir=str(data)),
            analyzer_cfg=AnalyzerConfig(), llm_cfg=services.load_llm_config(str(path)),
            feishu_cfg=FeishuSinkConfig(), anchor_cfg=None,
            source_name="synthetic", platform="synthetic")
        if live:
            confirm_live(rt.shared_state)
        runtimes.append(rt)
        return rt, path, data
    yield SimpleNamespace(runtime=runtime, calls=calls, instances=sdk_instances,
                          hooks=hooks, adapters=adapters)
    for rt in runtimes:
        rt.stop(join_timeout=0.3)
    for rt in runtimes:
        if rt._llm_thread:
            rt._llm_thread.join(timeout=1)
        assert not rt.is_alive()
        assert not (rt._llm_thread and rt._llm_thread.is_alive())


def test_no_key_start_and_save_does_not_enable_until_global_and_tag_allow(sandbox):
    rt, path, data = sandbox.runtime(key="", room_llm=False)
    rt.start()
    collector = rt._adapter_thread
    write_config(path, "synthetic-new-key")
    rt.reconcile_llm()
    assert not rt.llm_status()["worker_alive"] and sandbox.calls == []
    write_gate(data, enabled=True, tags=[])
    rt.reconcile_llm()
    assert not rt.llm_status()["worker_alive"]
    write_gate(data)
    rt.reconcile_llm()
    eventually(lambda: rt.llm_status()["state"] == "waiting_input")
    assert rt._adapter_thread is collector and collector.is_alive()
    rt._push_llm(snapshot("new event"))
    eventually(lambda: len(sandbox.calls) == 1)
    assert sandbox.calls[0]["key"] == "synthetic-new-key"


def test_permission_without_credentials_is_waiting_not_running(sandbox):
    rt, path, data = sandbox.runtime(key="")
    write_gate(data)
    rt.start()
    assert rt.llm_status()["state"] == "no_key" and rt._llm_thread is None
    assert rt.is_alive() and sandbox.calls == []


def test_deleting_all_keys_retires_worker_then_save_starts_fresh_generation(sandbox):
    rt, path, data = sandbox.runtime()
    write_gate(data)
    rt.start()
    eventually(lambda: rt.llm_status()["state"] == "waiting_input")
    first = rt._llm_thread
    write_config(path, "")
    rt.reconcile_llm()
    first.join(2)
    assert not first.is_alive()
    rt.reconcile_llm()
    assert rt.llm_status()["state"] == "no_key"
    rt._push_llm(snapshot("must not queue without key"))
    assert sandbox.calls == []
    write_config(path, "synthetic-restored-key")
    rt.reconcile_llm()
    eventually(lambda: rt.llm_status()["state"] == "waiting_input")
    assert rt._llm_generation == 2 and rt._llm_thread is not first
    rt._push_llm(snapshot("fresh key generation"))
    eventually(lambda: len(sandbox.calls) == 1)
    assert sandbox.calls[0]["key"] == "synthetic-restored-key"


def test_repeated_start_and_reconcile_do_not_duplicate_workers_or_fanout(sandbox):
    rt, path, data = sandbox.runtime()
    write_gate(data)
    threads = [threading.Thread(target=rt.start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
    eventually(lambda: rt.llm_status()["state"] == "waiting_input")
    first = rt._llm_thread
    for _ in range(5):
        rt.reconcile_llm()
    assert rt._llm_thread is first and rt._llm_generation == 1
    assert len(sandbox.adapters) == 1
    assert len(rt._event_fanout) == 2 and len(rt._snapshot_fanout) == 1


def test_stop_discards_queue_and_fast_off_on_waits_for_old_worker(sandbox):
    rt, path, data = sandbox.runtime()
    write_gate(data)
    rt.start()
    eventually(lambda: rt.llm_status()["state"] == "waiting_input")
    entered, release = threading.Event(), threading.Event()
    def inflight():
        entered.set()
        assert release.wait(3)
    sandbox.hooks.append(inflight)
    rt._push_llm(snapshot("in flight"))
    assert entered.wait(2)
    old_thread, old_queue = rt._llm_thread, rt._llm_queue
    rt._push_llm(snapshot("must discard"))
    write_gate(data, enabled=False, generation=1)
    rt.reconcile_llm()
    assert rt._llm_stop_event.is_set()
    rt._push_llm(snapshot("paused must reject"))
    write_gate(data, generation=1)
    rt.reconcile_llm()
    assert rt._llm_thread is old_thread and rt._llm_generation == 1
    assert rt.llm_status()["state"] == "stopping"
    release.set()
    old_thread.join(2)
    assert not old_thread.is_alive()
    rt.reconcile_llm()
    eventually(lambda: rt.llm_status()["state"] == "waiting_input")
    assert rt._llm_queue is not old_queue and rt._llm_generation == 2
    assert len(sandbox.calls) == 1
    rt._push_llm(snapshot("fresh generation"))
    eventually(lambda: len(sandbox.calls) == 2)
    assert "must discard" not in sandbox.calls[-1]["messages"][-1]["content"]


def test_old_epoch_is_discarded_even_if_pause_was_between_reconcile_ticks(sandbox):
    rt, path, data = sandbox.runtime()
    write_gate(data)
    rt.start()
    eventually(lambda: rt.llm_status()["state"] == "waiting_input")
    old_epoch = list(rt._llm_epoch)
    write_gate(data, tag_generation=1)
    rt._llm_queue.put(dict(snapshot("old epoch"), _llm_epoch=old_epoch))
    old_thread = rt._llm_thread
    old_thread.join(2)
    assert not old_thread.is_alive() and sandbox.calls == []
    rt.reconcile_llm()
    eventually(lambda: rt.llm_status()["state"] == "waiting_input")
    rt._llm_queue.put(dict(snapshot("stale manually queued"), _llm_epoch=old_epoch))
    rt._push_llm(snapshot("current epoch"))
    eventually(lambda: len(sandbox.calls) == 1)
    assert "current epoch" in sandbox.calls[0]["messages"][-1]["content"]


def test_consume_to_chat_pause_cannot_adopt_new_epoch(sandbox, monkeypatch):
    rt, path, data = sandbox.runtime()
    write_gate(data)
    original = services.PortraitService.generate
    changed = []
    def paused_before_chat(self, *args, **kwargs):
        if not changed:
            write_gate(data, generation=1)
            changed.append(True)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(services.PortraitService, "generate", paused_before_chat)
    rt.start()
    eventually(lambda: rt.llm_status()["state"] == "waiting_input")
    old_worker = rt._llm_thread
    rt._push_llm(snapshot("consumed under old epoch"))
    old_worker.join(2)
    assert changed == [True] and not old_worker.is_alive() and sandbox.calls == []
    rt.reconcile_llm()
    eventually(lambda: rt.llm_status()["state"] == "waiting_input")
    rt._push_llm(snapshot("new allowed epoch"))
    eventually(lambda: len(sandbox.calls) == 1)


def test_new_worker_does_not_reuse_prior_snapshot_or_portrait(sandbox):
    rt, path, data = sandbox.runtime()
    write_gate(data)
    rt.start()
    eventually(lambda: rt.llm_status()["state"] == "waiting_input")
    old = snapshot("prior generation")
    old["snapshot"]["top_words"] = [{"word": "synthetic-old-topic"}]
    old["snapshot"]["representative_danmaku"] = [{"uname": "synthetic", "text": "synthetic-old-danmaku"}]
    rt._push_llm(old)
    eventually(lambda: len(sandbox.calls) == 1 and rt.llm_status()["state"] == "waiting_input")
    old_worker = rt._llm_thread
    write_gate(data, enabled=False, generation=1)
    rt.reconcile_llm()
    old_worker.join(2)
    write_gate(data, generation=1)
    rt.reconcile_llm()
    eventually(lambda: rt.llm_status()["state"] == "waiting_input")
    rt._push_llm({"kind": "event", "event": {"event": "cold_start", "ts": 2}})
    eventually(lambda: len(sandbox.calls) == 2)
    payload = sandbox.calls[-1]["messages"][-1]["content"]
    assert "synthetic-old-topic" not in payload and "synthetic-old-danmaku" not in payload


def test_local_stop_event_blocks_unsent_http_retries(sandbox, monkeypatch):
    rt, path, data = sandbox.runtime()
    write_gate(data)
    rt.start()
    eventually(lambda: rt.llm_status()["state"] == "waiting_input")
    worker = rt._llm_thread
    class Transient(RuntimeError):
        status_code = 500
    def stop_during_http():
        rt._llm_stop_event.set()
        raise Transient("synthetic in-flight failure")
    monkeypatch.setattr(clients, "_RETRY_BACKOFF_BASE", 0)
    sandbox.hooks.append(stop_during_http)
    rt._push_llm(snapshot("only issued HTTP may finish"))
    worker.join(2)
    assert not worker.is_alive() and len(sandbox.calls) == 1 and rt.is_alive()


def test_shutdown_wait_is_bounded_then_thread_closes_sdk_after_http(sandbox):
    rt, path, data = sandbox.runtime()
    write_gate(data)
    rt.start()
    eventually(lambda: rt.llm_status()["state"] == "waiting_input")
    entered, release = threading.Event(), threading.Event()
    def inflight():
        entered.set()
        assert release.wait(3)
    sandbox.hooks.append(inflight)
    rt._push_llm(snapshot("issued may finish"))
    assert entered.wait(2)
    worker = rt._llm_thread
    started = time.monotonic()
    rt.stop(join_timeout=0.05)
    assert time.monotonic() - started < 0.8
    assert not rt.is_alive() and worker.is_alive()
    rt._push_llm(snapshot("after stop"))
    release.set()
    worker.join(2)
    assert not worker.is_alive() and len(sandbox.calls) == 1
    assert all(instance.closed for instance in sandbox.instances)


def test_worker_sidecar_has_actual_process_thread_identity_and_no_secret(sandbox):
    rt, path, data = sandbox.runtime()
    write_gate(data)
    rt.start()
    eventually(lambda: rt.llm_status()["state"] == "waiting_input")
    record = json.loads((data / "llm_worker_synthetic.json").read_text(encoding="utf-8"))
    assert record["worker_alive"] and record["collector_alive"]
    assert record["process_created_at"] is not None
    assert record["thread_ident"] == rt._llm_thread.ident
    assert record["thread_native_id"] == rt._llm_thread.native_id
    assert record["generation"] == 1 and record["state"] == "waiting_input"
    assert "synthetic-key" not in json.dumps(record)
    (data / "observed_running_worker.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def test_unstarted_or_finished_collection_never_claims_running_worker(sandbox):
    rt, path, data = sandbox.runtime()
    write_gate(data)
    rt.reconcile_llm()
    assert rt.llm_status()["state"] == "waiting_collector" and rt._llm_thread is None
    rt.start()
    eventually(lambda: rt.llm_status()["state"] == "waiting_input")
    worker = rt._llm_thread
    rt._adapter.close()
    rt._adapter_thread.join(2)
    worker.join(2)
    rt.reconcile_llm()
    assert rt.llm_status()["state"] == "waiting_collector"
    assert not rt.llm_status()["worker_alive"]


def test_main_cli_retains_only_old_start_hint(monkeypatch):
    import main
    monkeypatch.setattr(sys, "argv", ["main.py", "--llm"])
    args = main.parse_args()
    assert args.llm is True
    assert not hasattr(args, "no_llm")


@pytest.mark.parametrize("status,connected", [(None, False), (0, True), (2, False)])
def test_unknown_offline_or_unconnected_rooms_reject_automatic_heartbeat_events(sandbox, status, connected):
    rt, path, data = sandbox.runtime(live=False)
    token = rt.shared_state.begin_live_connection()
    rt.shared_state.set_live_status(status, token)
    if connected:
        rt.shared_state.set_live_connected(True, token)
    write_gate(data)
    rt.start()
    rt._push_llm(snapshot("synthetic idle heartbeat"))
    rt._push_llm({"kind": "event", "event": {"event": "cold_start", "ts": 1}})
    assert rt.llm_status()["state"] == "waiting_live"
    assert rt._llm_thread is None and rt.is_alive() and sandbox.calls == []


def test_live_loss_and_fast_reconnect_retire_old_generation_without_reconcile_gap(sandbox):
    rt, path, data = sandbox.runtime()
    write_gate(data)
    rt.start()
    eventually(lambda: rt.llm_status()["state"] == "waiting_input")
    entered, release = threading.Event(), threading.Event()
    def inflight():
        entered.set()
        assert release.wait(3)
    sandbox.hooks.append(inflight)
    rt._push_llm(snapshot("already issued live request"))
    assert entered.wait(2)
    old_worker = rt._llm_thread
    rt._push_llm(snapshot("old live queue"))
    prior = rt.shared_state.automatic_live()["generation"]
    confirm_live(rt.shared_state)  # reconnect is deliberately faster than reconcile
    assert rt.shared_state.automatic_live()["generation"] > prior
    rt.reconcile_llm()
    assert rt._llm_thread is old_worker and rt._llm_generation == 1
    release.set()
    old_worker.join(2)
    assert not old_worker.is_alive() and len(sandbox.calls) == 1
    rt.reconcile_llm()
    eventually(lambda: rt.llm_status()["state"] == "waiting_input")
    rt._push_llm(snapshot("new confirmed live generation"))
    eventually(lambda: len(sandbox.calls) == 2)
    assert "old live queue" not in sandbox.calls[-1]["messages"][-1]["content"]


def test_consume_to_sdk_live_loss_rejects_http_even_before_next_reconcile(sandbox, monkeypatch):
    rt, path, data = sandbox.runtime()
    write_gate(data)
    original = services.PortraitService.generate
    def disconnected_before_chat(self, *args, **kwargs):
        rt.shared_state.begin_live_connection()
        return original(self, *args, **kwargs)
    monkeypatch.setattr(services.PortraitService, "generate", disconnected_before_chat)
    rt.start()
    eventually(lambda: rt.llm_status()["state"] == "waiting_input")
    worker = rt._llm_thread
    rt._push_llm(snapshot("live when consumed but no longer live"))
    worker.join(2)
    assert not worker.is_alive() and sandbox.calls == []
    rt.reconcile_llm()
    assert rt.llm_status()["state"] == "waiting_live" and rt.is_alive()


def test_live_generation_change_inside_http_blocks_unsent_retry(sandbox, monkeypatch):
    rt, path, data = sandbox.runtime()
    write_gate(data)
    rt.start()
    eventually(lambda: rt.llm_status()["state"] == "waiting_input")
    worker = rt._llm_thread
    class Transient(RuntimeError):
        status_code = 500
    def reconnect_during_http():
        confirm_live(rt.shared_state)
        raise Transient("synthetic old connection failure")
    monkeypatch.setattr(clients, "_RETRY_BACKOFF_BASE", 0)
    sandbox.hooks.append(reconnect_during_http)
    rt._push_llm(snapshot("only old issued HTTP"))
    worker.join(2)
    assert not worker.is_alive() and len(sandbox.calls) == 1 and rt.is_alive()
    rt.reconcile_llm()
    eventually(lambda: rt.llm_status()["state"] == "waiting_input")
    rt._push_llm(snapshot("new connection generation"))
    eventually(lambda: len(sandbox.calls) == 2)


def test_source_bound_worker_without_live_guard_fails_closed(sandbox):
    rt, path, data = sandbox.runtime()
    write_gate(data)
    q = queue.Queue()
    q.put(dict(snapshot("missing trusted live guard"), _llm_epoch=[0, 0, 0]))
    services.run_llm_worker(q, services.load_llm_config(str(path)), output_dir=str(data),
                            room_tag="synthetic", control_epoch=[0, 0, 0])
    assert sandbox.calls == [] and sandbox.instances == []


@pytest.mark.parametrize("status,returned_id,expected", [(None, "123", False), (0, "123", False),
                                                       (2, "999", False), (2, "123", True)])
def test_client_requires_fresh_matching_enter_and_current_ws_open(sandbox, monkeypatch, status, returned_id, expected):
    import client as web_client
    shared = services.SharedState()
    monitor = SimpleNamespace(read_room_meta=lambda: {
        "room_status": 2, "room_id": "123", "anchor_name": "synthetic old",
        "title": "synthetic old", "source": "webcast/room/web/enter"},
        write_room_meta=lambda _: None)
    collector = web_client.DanmuClient(Config(web_rid="123"), SimpleNamespace(),
                                      shared_state=shared, monitor=monitor)
    collector._room_id = "123"
    collector._live_connection = shared.begin_live_connection()
    response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"data": {"data": [{
        "id_str": returned_id, "status": status, "title": "synthetic fresh",
        "owner": {"nickname": "synthetic fresh"}}]}})
    monkeypatch.setattr(web_client.requests, "get", lambda *a, **kw: response)
    collector._fetch_room_info("123", "123")
    assert not shared.automatic_live()["ready"]
    observed = []
    class WS:
        def __init__(self, *args, **kwargs):
            self.callbacks = kwargs
        def run_forever(self, **kwargs):
            self.callbacks["on_open"](self)
            observed.append(shared.automatic_live()["ready"])
            self.callbacks["on_error"](self, "synthetic disconnect")
            assert not shared.automatic_live()["ready"]
            # The same connection cannot regain permission from its old metadata.
            self.callbacks["on_open"](self)
            assert not shared.automatic_live()["ready"]
    monkeypatch.setattr(web_client.websocket, "WebSocketApp", WS)
    monkeypatch.setattr(collector, "_start_thread", lambda *args: None)
    collector._serve("wss://example.invalid", "")
    assert observed == [expected] and not shared.automatic_live()["ready"]


def test_shared_live_tokens_and_generations_reject_late_old_callbacks():
    shared = services.SharedState()
    first = confirm_live(shared)
    original_generation = shared.automatic_live()["generation"]
    shared.set_live_connected(False, first)
    assert shared.automatic_live()["generation"] == original_generation + 1
    second = shared.begin_live_connection()
    shared.set_live_connected(True, second)
    shared.set_live_status(2, first)
    assert not shared.automatic_live()["ready"]
    shared.set_live_status(2, second)
    assert shared.automatic_live()["ready"]
    shared.set_live_connected(False, first)
    shared.set_live_status(3, first)
    assert shared.automatic_live()["ready"]


def test_client_resolution_failure_revokes_previous_live_without_new_probe(sandbox):
    import client as web_client
    shared = services.SharedState()
    confirm_live(shared)
    prior = shared.automatic_live()["generation"]
    collector = web_client.DanmuClient(Config(web_rid="synthetic-invalid-room"),
                                      SimpleNamespace(), shared_state=shared)
    with pytest.raises(RuntimeError):
        collector._resolve_room()
    assert not shared.automatic_live()["ready"]
    assert shared.automatic_live()["generation"] == prior + 1


def test_control_offline_with_ws_open_revokes_automatic_and_ignores_old_scope(sandbox, monkeypatch):
    import client as web_client
    rt, path, data = sandbox.runtime(live=False)
    token = confirm_live(rt.shared_state)
    collector = web_client.DanmuClient(Config(web_rid="123"), SimpleNamespace(), shared_state=rt.shared_state)
    collector._room_id, collector._live_connection = "123", token
    write_gate(data)
    rt.start()
    eventually(lambda: rt.llm_status()["state"] == "waiting_input")
    worker = rt._llm_thread
    control = {"status": 3, "room_id": "999"}
    monkeypatch.setattr(web_client, "douyin_parser", SimpleNamespace(
        parse_pushframe=lambda _: SimpleNamespace(), decompress_payload=lambda _: b"synthetic",
        parse_response=lambda _: SimpleNamespace(need_ack=False), iter_messages=lambda _: [],
        iter_monitor_messages=lambda _: [("WebcastControlMessage", "control", b"synthetic")],
        normalize_monitor=lambda *a: control))
    collector._handle_message(SimpleNamespace(), b"synthetic", connection=token)
    assert rt.shared_state.automatic_live()["ready"]
    before = rt.shared_state.automatic_live()["generation"]
    control["room_id"] = "123"
    collector._handle_message(SimpleNamespace(), b"synthetic", connection=token)
    assert not rt.shared_state.automatic_live()["ready"]
    assert rt.shared_state.automatic_live()["generation"] == before + 1
    rt._push_llm({"kind": "event", "event": {"event": "cold_start", "ts": 1}})
    worker.join(2)
    assert not worker.is_alive() and sandbox.calls == [] and rt.is_alive()
    new_token = confirm_live(rt.shared_state)
    collector._live_connection = new_token
    collector._handle_message(SimpleNamespace(), b"synthetic", connection=token)
    assert rt.shared_state.automatic_live()["ready"]  # late old WS cannot revoke the new one
    collector.close()
    assert not rt.shared_state.automatic_live()["ready"]


def test_late_old_heartbeat_failure_cannot_revoke_new_connection(sandbox, monkeypatch):
    import client as web_client
    import protobuf.douyin as protocol
    shared = services.SharedState()
    old_connection = confirm_live(shared)
    collector = web_client.DanmuClient(Config(web_rid="123"), SimpleNamespace(), shared_state=shared)
    collector._live_connection = old_connection
    sockets, targets, sends = [], [], []
    class WS:
        def __init__(self, *args, **callbacks):
            self.callbacks = callbacks
            sockets.append(self)
        def run_forever(self, **kwargs):
            pass  # Hold the old on_open callback until a newer connection exists.
        def send(self, *args, **kwargs):
            sends.append(True)
            raise RuntimeError("synthetic delayed heartbeat failure")
    monkeypatch.setattr(web_client.websocket, "WebSocketApp", WS)
    monkeypatch.setattr(collector, "_start_thread", lambda target, name: targets.append(target))
    collector._serve("wss://example.invalid", "")
    new_connection = confirm_live(shared)
    collector._live_connection = new_connection
    collector._ws = sockets[0]  # Keep the existing dynamic WS send behavior.
    collector._stop_heartbeat = SimpleNamespace(wait=lambda _: False)
    monkeypatch.setattr(protocol, "PushFrame", lambda **kwargs: SimpleNamespace(
        SerializeToString=lambda: b"synthetic heartbeat"))
    current_generation = shared.automatic_live()["generation"]
    # Both callback delivery and the resulting heartbeat start after reconnect.
    sockets[0].callbacks["on_open"](sockets[0])
    assert len(targets) == 1 and shared.automatic_live()["ready"]
    targets[0]()
    assert shared.automatic_live()["ready"]
    assert shared.automatic_live()["generation"] == current_generation
    collector._heartbeat_loop()  # Compatible direct use captures the current token.
    assert not shared.automatic_live()["ready"]
    assert shared.automatic_live()["generation"] == current_generation + 1
    assert len(sends) == 2


def cleanup_case(monkeypatch, tmp_path, failures=(), available=True, notification_failures=()):
    events, states = [], []
    notification_failures = list(notification_failures)
    private_error = "synthetic-private-close-secret"
    class Writer:
        def __init__(self, output_dir, prefix):
            self.prefix = prefix
        def close(self):
            events.append("close:" + self.prefix)
            if self.prefix in failures:
                raise OSError(28, private_error)
    class Router:
        is_available = available
        primary = active = "synthetic"
        active_model = "synthetic-model"
        def probe_and_maybe_switch(self):
            pass
        def close(self):
            events.append("close:router")
            if "router" in failures:
                raise OSError(private_error)
    def notify(state):
        states.append(state)
        if state in notification_failures:
            notification_failures.remove(state)
            raise OSError(private_error)
    cfg = services.LLMConfig(metrics_enabled=True)
    cfg.build_router = lambda **kwargs: Router()
    monkeypatch.setattr(services, "DailyJsonlWriter", Writer)
    def run():
        q = queue.Queue()
        q.put(None)
        return services.run_llm_worker(q, cfg, output_dir=str(tmp_path),
                                       room_tag="synthetic", on_state=notify)
    return SimpleNamespace(run=run, events=events, states=states, private_error=private_error)


def test_cleanup_one_writer_failure_still_attempts_all_resources_and_reports_error(sandbox, monkeypatch, tmp_path, caplog):
    case = cleanup_case(monkeypatch, tmp_path, failures={"portrait_synthetic"})
    assert case.run() is None
    assert case.events == ["close:portrait_synthetic", "close:coldline", "close:recommend",
                           "close:llm_metrics", "close:router"]
    assert case.states[-1] == "error" and "stopped" not in case.states
    assert case.private_error not in caplog.text


def test_cleanup_multiple_errors_do_not_skip_metrics_router_or_final_error(sandbox, monkeypatch, tmp_path, caplog):
    case = cleanup_case(monkeypatch, tmp_path,
                        failures={"portrait_synthetic", "coldline", "llm_metrics", "router"},
                        notification_failures=["error"])
    assert case.run() is None
    assert case.events == ["close:portrait_synthetic", "close:coldline", "close:recommend",
                           "close:llm_metrics", "close:router"]
    assert case.states[-2:] == ["error", "error"] and "stopped" not in case.states
    assert case.private_error not in caplog.text


def test_unavailable_router_metrics_close_error_still_closes_router_and_notifies(sandbox, monkeypatch, tmp_path, caplog):
    case = cleanup_case(monkeypatch, tmp_path, failures={"llm_metrics", "router"}, available=False)
    assert case.run() is None
    assert case.events == ["close:llm_metrics", "close:router"]
    assert case.states == ["error"] and case.private_error not in caplog.text


def test_final_stopped_notification_failure_falls_back_to_error(sandbox, monkeypatch, tmp_path, caplog):
    case = cleanup_case(monkeypatch, tmp_path, notification_failures=["stopped"])
    assert case.run() is None
    assert case.events[-1] == "close:router"
    assert case.states[-2:] == ["stopped", "error"]
    assert case.private_error not in caplog.text
    assert "资源清理或最终状态通知未全部成功" in caplog.text


def test_all_final_notification_failures_raise_only_fixed_safe_error(sandbox, monkeypatch, tmp_path, caplog):
    case = cleanup_case(monkeypatch, tmp_path, notification_failures=["stopped", "error"])
    with pytest.raises(RuntimeError, match="LLM worker 最终状态通知失败") as observed:
        case.run()
    assert case.events[-1] == "close:router" and case.states[-1] == "error"
    assert case.private_error not in caplog.text and case.private_error not in str(observed.value)


def test_stop_cleanup_error_survives_thread_exit_and_reconcile(sandbox, monkeypatch, caplog):
    rt, path, data = sandbox.runtime()
    write_gate(data)
    original = services.DailyJsonlWriter
    private_error = "synthetic-private-stop-cleanup-secret"
    closed = []
    class FailingPortrait(original):
        def close(self):
            closed.append(self.prefix)
            super().close()
            if self.prefix == "portrait_synthetic":
                raise OSError(28, private_error)
    monkeypatch.setattr(services, "DailyJsonlWriter", FailingPortrait)
    rt.start()
    eventually(lambda: rt.llm_status()["state"] == "waiting_input")
    worker = rt._llm_thread
    rt.stop(join_timeout=0.3)
    worker.join(2)
    assert not worker.is_alive() and not rt.is_alive()
    assert set(closed) == {"portrait_synthetic", "coldline", "recommend", "llm_metrics"}
    assert all(instance.closed for instance in sandbox.instances)
    assert rt.llm_status()["state"] == "error"
    rt.reconcile_llm()
    assert rt.llm_status()["state"] == "error"
    assert private_error not in caplog.text
