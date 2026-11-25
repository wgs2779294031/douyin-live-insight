"""Fresh generated records and own native loopback/PID; no installation or SDK."""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
from scripts import local_mcp_bridge as bridge
from scripts import audio_segments as audio

OUT = Path(os.environ.get("DLI_TEST_ROOT", str(Path(__file__).resolve().parents[1] / ".test-state")))
NONCE_OUT = ROOT.parent / "task-12/mcp-launch-nonce"
VERSION = "v_" + "a" * 32


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def asr_snapshot():
    now = time.time()
    return {"ok": True, "status": {"state": "idle", "resource_held": True, "verified": True, "stale": False,
        "no_progress": False, "snapshot_at": now, "sequence": 4, "generation": "b" * 24,
        "counts": {"pending": None, "projection_pending": 2, "retryable": 1, "blocked": 0, "complete": 7},
        "scan": {"partial": True, "limited": True, "checked": 16, "last_complete_at": None,
                 "counts_scope": "recent_window", "sample_limit": 256},
        "current": {"tag": "generated-room", "date": "2026-11-16", "segment": "seg_0001", "phase": "idle", "attempt": None},
        "error": "", "next_condition": "periodic_scan", "next_retry_at": None, "last_progress_at": now, "last_completed_at": now,
        "storage": {"schema": 1, "verified": True, "stale": False, "snapshot_at": now,
            "audio_bytes": 200, "audio_files": 2, "transcription_pending": 1, "cleanup_pending": 1, "cleanup_failed": 0, "cleanup_blocked": 0,
            "scan": {"partial": True, "limited": True, "checked": 2, "started_at": now - 1, "updated_at": now,
                     "last_complete_at": None, "approximate": True, "counts_scope": "scan_round"},
            "disk": {"verified": True, "stale": False, "free_bytes": 100, "total_bytes": 2 * 1073741824,
                     "low_space": True, "threshold_bytes": 1073741824, "sampled_at": now, "error": ""}, "last_cleanup": None}}}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def process_request_thread(self, *args):
        self.threads.append(threading.current_thread())
        return super().process_request_thread(*args)


@pytest.fixture
def service(tmp_path, monkeypatch):
    assert any(tmp_path.resolve().is_relative_to(root.resolve()) for root in (OUT, NONCE_OUT))
    data = tmp_path / "new-task10-data"
    runtime = data / ".local_app"
    runtime.mkdir(parents=True)
    owner = bridge.process_identity(os.getpid())
    assert owner["state"] == "alive"
    stop = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *_args):
            pass
        def send(self, code, body, *, cookie=False):
            raw = body if isinstance(body, bytes) else encoded(body)
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            if cookie:
                self.send_header("Set-Cookie", server.cookie + "; HttpOnly; SameSite=Strict; Path=/")
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
        def do_POST(self):
            server.requests.append(("POST", self.path, self.headers.get("Cookie")))
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            assert self.path == "/api/local/session" and raw == b"{}"
            if server.on_bootstrap:
                server.on_bootstrap()
            self.send(server.bootstrap_code, {"mode": "local", "write_token": "fixture-write-token-never-output"}, cookie=server.bootstrap_code == 200)
        def do_GET(self):
            server.requests.append(("GET", self.path, self.headers.get("Cookie")))
            path = urlsplit(self.path).path
            if path == "/api/health":
                if server.health_mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "http://outside.invalid/private")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    self.close_connection = True
                    return
                if server.health_mode == "header_drip":
                    self.close_connection = True
                    try:
                        for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}":
                            if stop.wait(.3):
                                return
                            self.wfile.write(bytes([byte])); self.wfile.flush()
                    except OSError:
                        pass
                    return
                if server.health_mode == "large_header":
                    self.send_response(200)
                    self.send_header("X-Generated", "x" * bridge.MAX_HEADER_BYTES)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.close_connection = True
                    return
                if server.on_health:
                    server.on_health()
                return self.send(200, server.health_raw if server.health_raw is not None else
                                 {"ok": True, "instance_id": server.context["instance_id"], "owner": server.health_owner})
            if self.headers.get("Cookie") != server.cookie:
                return self.send(401, {"code": "local_session_required"})
            if server.always_401 or server.expire_once:
                server.expire_once = False
                return self.send(401, {"code": "local_session_required"})
            if path == "/api/asr":
                if server.on_target:
                    server.on_target()
                if server.body_drip:
                    raw = encoded(server.asr)
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.close_connection = True
                    try:
                        for byte in raw:
                            if stop.wait(.2):
                                return
                            self.wfile.write(bytes([byte])); self.wfile.flush()
                    except OSError:
                        pass
                    return
                return self.send(200, server.asr_raw if server.asr_raw is not None else server.asr)
            assert path == "/api/materials/status"
            lookup = parse_qs(urlsplit(self.path).query)
            result = copy.deepcopy(server.materials)
            if lookup:
                sid, rid = lookup["service_id"][0], lookup["request_id"][0]
                if sid != result["service_id"]:
                    result.update(lookup="service_changed", operation=None)
                elif not result["operation"] or result["operation"]["request_id"] != rid:
                    result.update(lookup="unknown", operation=None)
                else:
                    result["lookup"] = "found"
            return self.send(200, result)
    server = Server(("127.0.0.1", 0), Handler)
    server.threads, server.requests = [], []
    port = server.server_address[1]
    server.context = {"schema": 1, "instance_id": "task10-bridge-instance", "root": str(ROOT), "data": str(data),
        "config": str(tmp_path / "never-config.json"), "roster": str(tmp_path / "never-roster.json"), "dist": str(tmp_path / "never-dist"),
        "python": sys.executable, "profile": str(tmp_path / "never-profile"), "port": port, "cdp_port": 1 if port != 1 else 2}
    namespace = server.context["instance_id"] + ":" + str(port)
    import hashlib
    server.cookie = "dy_local_" + hashlib.sha256(namespace.encode()).hexdigest()[:16] + "=" + "c" * 43
    server.owner, server.health_owner = owner, copy.deepcopy(owner)
    server.path, server.data = runtime / "instance.json", data
    server.record_value = {"schema": 1, "instance_id": server.context["instance_id"], "context": server.context, "owner": owner,
                           "launch_nonce": "a" * 32, "updated_at": time.time(), "state": "waiting", "board_ready": True}
    def record(**changes):
        server.record_value.update(changes)
        temporary = server.path.with_suffix(".new-json")
        temporary.write_bytes(encoded(server.record_value))
        os.replace(temporary, server.path)
    server.record = record
    record()
    server.asr, server.asr_raw, server.health_raw = asr_snapshot(), None, None
    server.on_target = server.on_health = server.on_bootstrap = None
    server.health_mode, server.bootstrap_code = "", 200
    server.expire_once = server.always_401 = server.body_drip = False
    sid, rid = str(uuid.uuid4()), str(uuid.uuid4())
    server.materials = {"ok": True, "service_id": sid, "active_id": None, "lookup": "latest", "stopping": False,
        "operation": {"service_id": sid, "request_id": rid, "state": "unconfirmed", "version": None, "previous": None,
                      "code": "material_publish_unconfirmed", "warning": None, "started_at": time.time(), "finished_at": None},
        "retention": {"scope": "http_instance", "terminal_limit": 32, "persistent": False, "ttl_seconds": None},
        "material": {"state": "ready", "code": None, "version": VERSION, "generated_at": time.time(), "verified_at": time.time(),
                     "refresh": {"state": "success", "version": VERSION, "updated_at": time.time(), "last_success_at": time.time(), "error": None, "snapshot_stale": False}}}
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, name="task10-own-loopback")
    thread.start()
    def forbidden(*_args, **_kwargs):
        raise AssertionError("bridge used a process/registry/business write")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(audio, "atomic_json", forbidden)
    monkeypatch.setattr(audio, "fingerprint", forbidden)
    if os.name == "nt":
        import winreg
        monkeypatch.setattr(winreg, "OpenKey", forbidden)
    try:
        yield server
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive()
        for worker in server.threads:
            worker.join(2)
            assert not worker.is_alive()


def new_bridge(service, **options):
    return bridge.RuntimeBridge(service.context, **options)


def test_real_native_pid_listener_identity_and_safe_cache_read(service):
    assert bridge.listener_pid(service.context["port"])["pids"] == [os.getpid()]
    reader = new_bridge(service)
    result = reader.health()
    assert result["runtime"]["verified"] is True and result["runtime"]["state"] == "online", result
    assert result["asr"]["counts"]["pending"] is None and result["asr"]["scan"]["partial"] is True
    assert result["asr"]["scan"]["counts_scope"] == "recent_window" and result["asr"]["scan"]["sample_limit"] == 256
    assert result["asr"]["storage"]["scan"]["counts_scope"] == "scan_round"
    assert result["asr"]["storage"]["disk"]["low_space"] is True
    assert result["coverage"]["collection_completeness"] == "unknown" and result["coverage"]["platform_login"] == "not_checked"
    assert [(method, path) for method, path, _cookie in service.requests] == [
        ("GET", "/api/health"), ("POST", "/api/local/session"), ("GET", "/api/asr")]
    assert service.requests[0][2] is None and service.requests[1][2] is None
    assert "fixture-write-token" not in repr(result) and "dy_local_" not in repr(result)
    assert str(service.data) not in repr(result) and "launch_nonce" not in repr(result)
    reader.health()
    assert sum(method == "POST" for method, _, _ in service.requests) == 1


def test_health_accepts_nonce_generated_by_start(service, monkeypatch):
    from scripts import local_app as app

    launched = []

    def spawn(_command, **options):
        nonce = options["env"]["DY_LOCAL_LAUNCH_NONCE"]
        launched.append(nonce)
        service.record(launch_nonce=nonce, updated_at=time.time())
        return SimpleNamespace(pid=service.owner["pid"], poll=lambda: None)

    def status(_context):
        if not launched:
            return {"ok": True, "state": "stopped"}
        return dict(service.record_value, ok=True, process_state="alive")

    # Run the real start nonce generator. Substitute only the workload launch
    # and preflight so the owned loopback server stands in for the supervisor.
    with monkeypatch.context() as patch:
        patch.setattr(app, "status", status)
        patch.setattr(app, "check_configuration", lambda _context: None)
        patch.setattr(app, "listener_pid", lambda _port: {"state": "vacant"})
        patch.setattr(app, "installed_launcher_command", lambda _context: None)
        patch.setattr(app, "installed_child_environment", lambda _context: {})
        patch.setattr(app.subprocess, "Popen", spawn)
        started = app.start(service.context, wait=1.0)
    assert started["ok"] and len(launched) == 1
    assert started["launch_nonce"] == launched[0] and len(launched[0]) == 48
    result = new_bridge(service).health()
    assert result["runtime"]["verified"] and result["runtime"]["code"] == "online", result


def test_health_accepts_empty_nonce_from_direct_run(service):
    service.record(launch_nonce="")
    result = new_bridge(service).health()
    assert result["runtime"]["verified"] and result["runtime"]["code"] == "online", result


@pytest.mark.parametrize("nonce", [None, "a" * 31, "a" * 33, "a" * 47, "a" * 49, "A" * 48, "a" * 47 + "g", "a" * 48 + "\n"])
def test_health_rejects_malformed_launch_nonce_before_http(service, nonce):
    service.record(launch_nonce=nonce)
    result = new_bridge(service).health()
    assert result["runtime"]["code"] == "instance_invalid" and result["runtime"]["verified"] is False
    assert service.requests == []


@pytest.mark.parametrize("bad", ["pid", "listener", "health", "context", "instance", "old", "future"])
def test_unknown_identity_never_bootstraps_or_sends_cookie(service, bad):
    options = {}
    if bad == "pid":
        options["identity_reader"] = lambda pid: dict(service.owner, created=service.owner["created"] + 1)
    elif bad == "listener":
        options["listener_reader"] = lambda port: {"state": "listening", "pids": [1]}
    elif bad == "health":
        service.health_owner["created"] += 1
    elif bad == "context":
        altered = dict(service.context, port=service.context["port"] + 1)
        service.record(context=altered)
    elif bad == "instance":
        service.record(instance_id="different-instance")
    elif bad == "old":
        service.record(updated_at=time.time() - 30)
    else:
        service.record(updated_at=time.time() + 30)
    result = new_bridge(service, **options).health()
    assert result["runtime"]["state"] == "unknown" and result["asr"] is None
    assert all(method == "GET" and path == "/api/health" and cookie is None for method, path, cookie in service.requests)


@pytest.mark.parametrize("window", ["health", "bootstrap", "target"])
def test_generation_change_is_rejected_and_cookie_is_discarded(service, window):
    callback = lambda: service.record(launch_nonce="d" * 48, updated_at=time.time())
    setattr(service, "on_" + window, callback)
    reader = new_bridge(service)
    result = reader.health()
    assert result["runtime"]["code"] == "runtime_changed" and result["asr"] is None
    assert reader._cookie is None and reader._cookie_generation is None
    if window == "health":
        assert len(service.requests) == 1
    elif window == "bootstrap":
        assert all(path != "/api/asr" for _, path, _ in service.requests)


@pytest.mark.xfail(strict=True, reason="Native probe01/02: legacy os.replace rejects an open destination even with share7; historical assumption, not reader's long lease")
def test_mutable_record_share_delete_allows_atomic_heartbeat_replace(service, monkeypatch):
    reader = new_bridge(service)
    initial = service.path.stat().st_ino
    entered, replaced = threading.Event(), threading.Event()
    errors, observations = [], []
    original = os.read
    once = False
    def read(fd, size):
        nonlocal once
        if not once and size == bridge.MAX_INSTANCE_BYTES + 1 and os.fstat(fd).st_ino == initial:
            once = True
            entered.set()
            assert replaced.wait(2)
        return original(fd, size)
    def writer():
        try:
            assert entered.wait(2)
            service.record(updated_at=time.time())
            observations.append(service.path.stat().st_ino != initial)
        except BaseException as error:
            errors.append(error)
        finally:
            replaced.set()
    worker = threading.Thread(target=writer)
    monkeypatch.setattr(os, "read", read)
    worker.start()
    try:
        result = reader.health()
    finally:
        replaced.set()
        worker.join(2)
        assert not worker.is_alive()
    assert not errors and observations == [True]
    assert result["runtime"]["verified"] is True, result


def test_cookie_rebuild_once_on_401_and_no_business_post(service):
    reader = new_bridge(service)
    service.expire_once = True
    assert reader.health()["runtime"]["verified"] is True
    assert len(service.requests) == 5
    assert [path for method, path, _ in service.requests if method == "POST"] == ["/api/local/session"] * 2
    service.requests.clear()
    service.always_401 = True
    result = reader.health()
    assert result["runtime"]["code"] == "authentication_unavailable" and result["asr"] is None
    assert len(service.requests) == 4  # Existing cookie, one rebuild, one retry.
    assert reader._cookie is None


def test_failed_bootstrap_or_redirect_never_uses_unknown_credentials(service):
    service.bootstrap_code = 503
    assert new_bridge(service).health()["runtime"]["code"] == "authentication_unavailable"
    assert [(method, path) for method, path, _ in service.requests] == [("GET", "/api/health"), ("POST", "/api/local/session")]
    service.requests.clear()
    service.health_mode = "redirect"
    result = new_bridge(service).health()
    assert result["runtime"]["state"] == "unknown" and len(service.requests) == 1


@pytest.mark.parametrize("where", ["header", "body"])
def test_absolute_five_second_deadline_stops_header_or_body_drip(service, where):
    if where == "header":
        service.health_mode = "header_drip"
    else:
        service.body_drip = True
    reader = new_bridge(service)
    start = time.monotonic()
    result = reader.health()
    elapsed = time.monotonic() - start
    assert result["runtime"]["code"] == "deadline_exceeded", result
    assert 4.5 <= elapsed < 6.5, elapsed
    assert reader._cookie is None


@pytest.mark.parametrize("where", ["body", "header"])
def test_response_caps_are_enforced_without_echo(service, where):
    if where == "body":
        service.asr_raw = b"x" * (bridge.MAX_BODY_BYTES + 1)
    else:
        service.health_mode = "large_header"
    result = new_bridge(service).health()
    assert result["runtime"]["code"] == "response_too_large" and result["asr"] is None


def test_old_future_partial_null_snapshot_never_becomes_current_by_bridge(service):
    service.asr["status"].update(verified=False, stale=True, snapshot_at=time.time() - 1000)
    service.asr["status"]["storage"].update(verified=False, stale=True)
    service.asr["status"]["storage"]["disk"].update(verified=False, stale=True, low_space=None, free_bytes=None, total_bytes=None)
    result = new_bridge(service).health()["asr"]
    assert result["verified"] is False and result["stale"] is True and result["counts"]["pending"] is None
    assert result["storage"]["scan"]["partial"] is True and result["storage"]["disk"]["low_space"] is None
    service.asr["status"].update(verified=True, stale=False, snapshot_at=time.time() + 1000)
    result = new_bridge(service).health()["asr"]
    assert result["verified"] is False and result["storage"]["disk"]["low_space"] is None


def test_status_keeps_old_service_unknown_and_operation_independent_of_current(service):
    reader = new_bridge(service)
    raw = service.materials
    sid, rid = raw["service_id"], raw["operation"]["request_id"]
    result = reader.status(rid, sid)
    assert result["http"]["lookup"] == "found" and result["http"]["operation"]["state"] == "unconfirmed"
    assert result["http"]["operation"]["version"] is None and result["http"]["material"]["version"] == VERSION
    unknown = reader.status(str(uuid.uuid4()), sid)
    assert unknown["http"]["lookup"] == "unknown" and unknown["http"]["operation"] is None
    changed = reader.status(rid, str(uuid.uuid4()))
    assert changed["http"]["lookup"] == "service_changed" and changed["http"]["operation"] is None
    assert changed["http"]["material"]["version"] == VERSION
    assert all(method == "GET" or path == "/api/local/session" for method, path, _ in service.requests)


def test_invalid_lookup_and_offline_have_no_file_asr_fallback(service):
    reader = new_bridge(service)
    assert reader.status("bad", None)["runtime"]["code"] == "invalid_lookup"
    assert not service.requests
    retained = service.path.with_name("retained-instance.json")
    service.path.rename(retained)
    result = reader.health()
    assert result["runtime"]["state"] == "unknown" and result["asr"] is None
    assert reader.status()["http"] is None and not service.requests
    assert retained.exists()


def test_scope_changed_during_target_rejects_all_current_claims(service):
    reader = new_bridge(service)
    retained = service.data.with_name("retained-generated-data")
    def replace_scope():
        service.data.rename(retained)
        service.data.mkdir()
    service.on_target = replace_scope
    result = reader.health()
    assert result["runtime"]["code"] == "runtime_changed" and result["asr"] is None
    assert retained.is_dir()


def test_request_budget_and_native_time_are_part_of_whole_deadline(service, monkeypatch):
    monkeypatch.setattr(bridge, "MAX_REQUESTS", 2)
    result = new_bridge(service).health()
    assert result["runtime"]["code"] == "request_limit" and len(service.requests) == 2
    monkeypatch.setattr(bridge, "MAX_REQUESTS", 6)
    monkeypatch.setattr(bridge, "DEADLINE_SECONDS", .05)
    service.requests.clear()
    def delayed(pid):
        time.sleep(.1)
        return bridge.process_identity(pid)
    start = time.monotonic()
    result = new_bridge(service, identity_reader=delayed).health()
    assert result["runtime"]["code"] == "deadline_exceeded" and not service.requests
    assert time.monotonic() - start >= .1  # Native call cannot be hard preempted.


def test_real_publisher_retry_succeeds_after_short_read_before_http(service, monkeypatch):
    """Use the unchanged supervisor writer's actual 20ms/2s retry path."""
    import local_app as application
    reader = new_bridge(service)
    initial = service.path.stat().st_ino
    entered, release_read, published = threading.Event(), threading.Event(), threading.Event()
    errors, replace_errors = [], []
    original_read, original_replace = os.read, os.replace
    once = False
    def read(fd, size):
        nonlocal once
        if not once and size == bridge.MAX_INSTANCE_BYTES + 1 and os.fstat(fd).st_ino == initial:
            once = True
            entered.set()
            assert release_read.wait(2)
        return original_read(fd, size)
    def replace(source, target):
        try:
            return original_replace(source, target)
        except PermissionError as error:
            if Path(target) == service.path:
                replace_errors.append(error.winerror)
                release_read.set()
            raise
    def writer():
        try:
            assert entered.wait(2)
            value = dict(service.record_value, updated_at=time.time())
            application.atomic_json(service.path, value)
        except BaseException as error:
            errors.append(error)
        finally:
            published.set()
            release_read.set()
    def health_after_publish():
        assert published.wait(2)
        assert not errors
        assert service.path.stat().st_ino != initial
    service.on_health = health_after_publish
    monkeypatch.setattr(os, "read", read)
    monkeypatch.setattr(os, "replace", replace)
    worker = threading.Thread(target=writer)
    worker.start()
    try:
        result = reader.health()
    finally:
        release_read.set()
        worker.join(3)
        assert not worker.is_alive()
    assert not errors and replace_errors and replace_errors[0] == 5
    assert result["runtime"]["verified"] is True, result
    assert service.path.stat().st_ino != initial
    assert any(path == "/api/asr" for _, path, _ in service.requests)
