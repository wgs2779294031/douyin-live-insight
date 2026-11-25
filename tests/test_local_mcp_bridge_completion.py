"""Completion TTL and unsupported 1xx; own loopback, actual cache getter only.

The cache/tail-delay fixture follows the independent review reproducer.  No
AsrLane constructor, worker, boot/registry lookup, DATA or native job is used.
"""
from contextlib import contextmanager
import copy
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import socket
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
from scripts import local_mcp_bridge as bridge
from scripts.local_asr import AsrLane

WALL = 2000000000.0


def cache_lane(*, asr_age=14.0, stats_age=14.0, disk_age=59.0, dynamic=False):
    start = time.monotonic()
    now = (lambda: time.monotonic() - start) if dynamic else (lambda: 0.0)
    wall = lambda: WALL + now()
    initial = wall()
    at, stats, disk = initial - asr_age, initial - stats_age, initial - disk_age
    lane = object.__new__(AsrLane)
    lane.manager = SimpleNamespace(now=now, stopping=False)
    lane.wall_time = wall
    lane.state, lane.stop_requested = "running", False
    lane._storage_lock = threading.Lock()
    lane._storage_generation = "b" * 24
    lane._storage_invalid_through = {"stats": 0, "disk": 0}
    lane._cache = {
        "state": "running", "resource_held": True, "verified": True, "stale": False, "no_progress": False,
        "snapshot_at": at, "sequence": 4, "generation": "b" * 24,
        "counts": {"pending": None, "projection_pending": 2, "retryable": 0, "blocked": 0, "complete": 1},
        "scan": {"partial": True, "limited": False, "checked": 3, "last_complete_at": None},
        "current": {}, "error": "", "next_condition": "periodic_scan", "next_retry_at": None,
        "last_progress_at": at, "last_completed_at": at,
        "storage": {"schema": 1, "verified": False, "stale": True, "snapshot_at": stats,
            "audio_bytes": 4, "audio_files": 1, "transcription_pending": 0, "cleanup_pending": 1,
            "cleanup_failed": 0, "cleanup_blocked": 0,
            "scan": {"partial": True, "limited": False, "checked": 1, "started_at": stats,
                     "updated_at": stats, "last_complete_at": None},
            "disk": {"verified": False, "stale": True, "free_bytes": 10, "total_bytes": 2000000000,
                     "low_space": True, "threshold_bytes": 1073741824, "sampled_at": disk, "error": ""}, "last_cleanup": None},
        "_worker_state": "idle", "_seen_at": 0, "_initial_age": asr_age, "_progress_at": 0,
        "_storage_receipt": {"stats": {"valid": True, "age": stats_age, "seen": 0, "stamps": (stats,)},
                             "disk": {"valid": True, "age": disk_age, "seen": 0, "stamps": (disk,)}}}
    return lane, wall


@contextmanager
def delayed_cache(delay, *, asr_age=14.85, stats_age=14.85, disk_age=59.85):
    lane, wall = cache_lane(asr_age=asr_age, stats_age=stats_age, disk_age=disk_age, dynamic=True)
    stop = threading.Event()
    served, failures = [], []
    owner = {"state": "alive", "pid": 424242, "created": "fake-completion-created"}
    context = {"instance_id": "task10-completion-fixture"}
    record = {"instance_id": context["instance_id"], "owner": owner, "launch_nonce": "a" * 32,
              "context": context, "state": "waiting", "board_ready": True}
    cookie = "completion_cookie=" + "c" * 43

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *_args):
            pass
        def send(self, value, *, session=False, pause=False):
            raw = json.dumps(value, separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            if session:
                self.send_header("Set-Cookie", cookie + "; HttpOnly; SameSite=Strict; Path=/")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(raw[:-1] if pause else raw)
            self.wfile.flush()
            if pause:
                stop.wait(delay)
                self.wfile.write(raw[-1:])
                self.wfile.flush()
        def do_POST(self):
            try:
                assert self.path == "/api/local/session" and self.rfile.read(2) == b"{}"
                self.send({"mode": "local", "write_token": "unused-fixture-token"}, session=True)
            except BaseException as error:
                failures.append(error)
        def do_GET(self):
            try:
                if self.path == "/api/health":
                    return self.send({"ok": True, "instance_id": context["instance_id"], "owner": owner})
                assert self.path == "/api/asr" and self.headers.get("Cookie") == cookie
                sample = lane.snapshot()
                assert sample["verified"] and sample["storage"]["verified"] and sample["storage"]["disk"]["verified"]
                served.append(copy.deepcopy(sample))
                self.send({"ok": True, "status": sample}, pause=True)
            except BaseException as error:
                failures.append(error)
    server = HTTPServer(("127.0.0.1", 0), Handler)
    context["port"] = server.server_address[1]
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, name="task10-completion-http")
    worker.start()
    reader = object.__new__(bridge.RuntimeBridge)
    reader._valid, reader.context = True, context
    reader._clock, reader._wall = time.monotonic, wall
    reader._cookie = reader._cookie_generation = None
    reader._cookie_name = "completion_cookie"
    def verify(deadline, expected=None):
        deadline.remaining()
        if expected is not None:
            assert expected == reader._generation(record)
        return record
    reader._verify = verify
    try:
        yield reader, lane, served
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        worker.join(2)
        assert not worker.is_alive()
        assert not failures, repr(failures)


def test_actual_getter_tail_delay_loses_current_at_completion():
    with delayed_cache(.35) as (reader, lane, served):
        result = reader.health()
        manager_now = lane.snapshot()
        assert served and result["runtime"]["verified"]
        assert served[0]["verified"] and served[0]["storage"]["verified"] and served[0]["storage"]["disk"]["verified"]
        sample = result["asr"]
        assert not manager_now["verified"] and not sample["verified"] and sample["stale"]
        assert not sample["storage"]["verified"] and sample["storage"]["stale"]
        assert not sample["storage"]["disk"]["verified"] and sample["storage"]["disk"]["stale"]
        assert sample["storage"]["disk"]["low_space"] is None
        assert sample["resource_held"] is True and sample["counts"] == served[0]["counts"]
        assert sample["sequence"] == served[0]["sequence"] and sample["generation"] == served[0]["generation"]


def test_actual_getter_without_tail_delay_stays_current():
    with delayed_cache(0) as (reader, lane, served):
        result = reader.health()
        assert served and result["runtime"]["verified"] and lane.snapshot()["verified"]
        assert result["asr"]["verified"] and result["asr"]["storage"]["verified"]
        assert result["asr"]["storage"]["disk"]["verified"] and result["asr"]["storage"]["disk"]["low_space"] is True


@pytest.mark.parametrize("extra,current", [(1.0, True), (1.000001, False)])
def test_completion_exact_fifteen_sixty_thresholds_and_history(extra, current):
    lane, wall = cache_lane()
    source = lane.snapshot()
    assert source["verified"] and source["storage"]["verified"] and source["storage"]["disk"]["verified"]
    result = bridge._asr({"ok": True, "status": source}, wall() + extra)
    assert result["verified"] is current and result["stale"] is not current
    assert result["storage"]["verified"] is current and result["storage"]["disk"]["verified"] is current
    assert result["storage"]["disk"]["low_space"] is (True if current else None)
    assert result["resource_held"] == source["resource_held"] and result["counts"] == source["counts"]
    assert result["counts"]["pending"] is None and result["sequence"] == 4 and result["generation"] == "b" * 24
    assert result["storage"]["audio_bytes"] == source["storage"]["audio_bytes"]
    assert result["storage"]["disk"]["free_bytes"] == source["storage"]["disk"]["free_bytes"]


@pytest.mark.parametrize("stats_age,disk_age,stats_current,disk_current", [(14.0, 50.0, False, True), (1.0, 59.0, True, False)])
def test_statistics_and_disk_expire_independently(stats_age, disk_age, stats_current, disk_current):
    lane, wall = cache_lane(asr_age=1.0, stats_age=stats_age, disk_age=disk_age)
    source = lane.snapshot()
    result = bridge._asr({"ok": True, "status": source}, wall() + 1.1)
    assert result["verified"] is True
    assert result["storage"]["verified"] is stats_current
    assert result["storage"]["disk"]["verified"] is disk_current
    assert result["storage"]["disk"]["low_space"] is (True if disk_current else None)


@pytest.mark.parametrize("rejected", ["asr_verified", "asr_stale", "stats_verified", "stats_stale", "disk_verified", "disk_stale", "manager_watermark"])
def test_completion_never_promotes_a_manager_rejection(rejected):
    lane, wall = cache_lane(asr_age=1, stats_age=1, disk_age=1)
    if rejected == "manager_watermark":
        lane._storage_invalid_through = {"stats": 4, "disk": 4}
    source = lane.snapshot()
    targets = {"asr": source, "stats": source["storage"], "disk": source["storage"]["disk"]}
    if rejected != "manager_watermark":
        kind, field = rejected.split("_")
        targets[kind]["verified" if field == "verified" else "stale"] = field != "verified"
    result = bridge._asr({"ok": True, "status": source}, wall() + 1)
    if rejected.startswith("asr"):
        assert result["verified"] is False and result["storage"]["verified"] is False
        assert result["storage"]["disk"]["verified"] is False
    elif rejected.startswith("stats"):
        assert result["storage"]["verified"] is False
    else:
        assert result["storage"]["disk"]["verified"] is False
        assert result["storage"]["disk"]["low_space"] is None
    if rejected == "manager_watermark":
        assert result["storage"]["verified"] is False and lane._storage_invalid_through == {"stats": 4, "disk": 4}


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), True, "not-time", WALL + 100])
def test_invalid_or_future_sample_times_only_downgrade(value):
    lane, wall = cache_lane(asr_age=1, stats_age=1, disk_age=1)
    source = lane.snapshot()
    source["snapshot_at"] = value
    source["storage"]["snapshot_at"] = source["storage"]["scan"]["updated_at"] = value
    source["storage"]["disk"]["sampled_at"] = value
    result = bridge._asr({"ok": True, "status": source}, wall())
    assert not result["verified"] and not result["storage"]["verified"] and not result["storage"]["disk"]["verified"]
    assert result["storage"]["disk"]["low_space"] is None and result["counts"]["pending"] is None


@pytest.mark.parametrize("wall", [None, float("nan"), float("inf"), True, "not-time"])
def test_completion_clock_unavailable_never_claims_current(wall):
    lane, _wall = cache_lane(asr_age=1, stats_age=1, disk_age=1)
    result = bridge._asr({"ok": True, "status": lane.snapshot()}, wall)
    assert not result["verified"] and not result["storage"]["verified"] and not result["storage"]["disk"]["verified"]
    assert result["storage"]["disk"]["low_space"] is None


def test_real_manager_future_rejection_stays_rejected_after_wall_recovers_same_sequence():
    lane, _wall = cache_lane(asr_age=1, stats_age=1, disk_age=1)
    wall = [WALL]
    lane.wall_time = lambda: wall[0]
    future = WALL + 1
    lane._cache["storage"]["snapshot_at"] = future
    lane._cache["storage"]["scan"]["updated_at"] = future
    lane._cache["storage"]["disk"]["sampled_at"] = future
    for kind in ("stats", "disk"):
        lane._cache["_storage_receipt"][kind]["stamps"] = (future,)
    rejected = lane.snapshot()
    assert lane._storage_invalid_through == {"stats": 4, "disk": 4}
    assert not rejected["storage"]["verified"] and not rejected["storage"]["disk"]["verified"]
    wall[0] += 2
    recovered_clock = lane.snapshot()
    result = bridge._asr({"ok": True, "status": recovered_clock}, wall[0])
    assert result["verified"] and result["sequence"] == 4
    assert not result["storage"]["verified"] and not result["storage"]["disk"]["verified"]
    assert result["storage"]["disk"]["low_space"] is None and lane._storage_invalid_through == {"stats": 4, "disk": 4}


@contextmanager
def response_server(payload):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(2)
    failures, requests = [], []
    def serve():
        try:
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(2)
                raw = b""
                while b"\r\n\r\n" not in raw:
                    part = conn.recv(4096)
                    assert part
                    raw += part
                    assert len(raw) <= 8192
                requests.append(raw.split(b"\r\n", 1)[0])
                conn.sendall(payload)
        except BaseException as error:
            failures.append(error)
    worker = threading.Thread(target=serve, name="task10-completion-raw")
    worker.start()
    try:
        yield listener.getsockname()[1], requests
    finally:
        listener.close()
        worker.join(3)
        assert not worker.is_alive()
        assert not failures, repr(failures)


def request(payload):
    with response_server(payload) as (port, requests):
        reader = object.__new__(bridge.RuntimeBridge)
        reader.context, reader._cookie = {"port": port}, None
        try:
            return reader._request(bridge._Deadline(time.monotonic), "GET", "/api/health")
        finally:
            assert requests == [b"GET /api/health HTTP/1.1"]


def final_response(extra=b""):
    return b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n" + extra + b"\r\n{}"


@pytest.mark.parametrize("status", [100, 101, 102, 103, 199])
def test_all_unsupported_informational_responses_are_rejected_before_begin_skips(status):
    with pytest.raises(bridge._BridgeError) as caught:
        request(f"HTTP/1.1 {status} Unsupported\r\n\r\n".encode() + final_response())
    assert caught.value.code == "response_invalid"


def test_interim_cannot_hide_large_final_header():
    with pytest.raises(bridge._BridgeError) as caught:
        request(b"HTTP/1.1 100 Continue\r\n\r\n" + final_response(b"X-Fill: " + b"x" * 17000 + b"\r\n"))
    assert caught.value.code == "response_invalid"


@pytest.mark.parametrize("extra,allowed", [(0, True), (1, False)])
def test_supported_final_header_exact_budget(extra, allowed):
    overhead = len(final_response(b"X-Fill: \r\n")) - 2
    payload = final_response(b"X-Fill: " + b"x" * (bridge.MAX_HEADER_BYTES - overhead + extra) + b"\r\n")
    if allowed:
        code, raw, _headers = request(payload)
        assert (code, raw) == (200, b"{}")
    else:
        with pytest.raises(bridge._BridgeError) as caught:
            request(payload)
        assert caught.value.code == "response_too_large"
