"""Real owned loopback transports; short action budgets, no browser or platform."""
import base64
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import socket
import socketserver
import struct
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT)]
import login_browser as bridge
import local_app as app
from local_login import LoginGuide
from test_local_access import local_boards, _bootstrap, _request

UID = "MS4wLjABAAAA" + "d" * 44
TARGET = "deadline-owned-page"
BUDGET = .45


def wait_for(predicate, seconds=1.0):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.005)
    assert predicate(), "bounded resource-cleanup wait expired"


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True

    def process_request_thread(self, request, client_address):
        with self.lock:
            self.handlers.append(threading.current_thread())
        return super().process_request_thread(request, client_address)


class Handler(socketserver.BaseRequestHandler):
    def read_exact(self, count):
        data = b""
        while len(data) < count:
            part = self.request.recv(count - len(data))
            if not part:
                raise EOFError
            data += part
        return data

    def drip(self, chunks, seconds):
        self.server.blocked.set()
        for chunk in chunks:
            self.request.sendall(chunk)
            if self.server.done.wait(seconds / len(chunks)):
                return

    def wait_closed(self):
        while not self.server.done.is_set():
            try:
                if not self.request.recv(1024):
                    return True
            except socket.timeout:
                continue
        return False

    def handle(self):
        self.request.settimeout(.1)
        path = None
        closed = False
        try:
            incoming = b""
            while b"\r\n\r\n" not in incoming:
                incoming += self.read_exact(1)
            lines = incoming.decode("ascii").split("\r\n")
            path = lines[0].split()[1]
            with self.server.lock:
                self.server.paths.append(path)
            mode = self.server.mode
            if path == "/sentinel":
                self.request.sendall(b"ready")
                while not self.server.done.is_set():
                    try:
                        value = self.request.recv(1)
                    except socket.timeout:
                        continue
                    if not value:
                        closed = True
                        return
                    self.request.sendall(value)
                return
            if path == "/json/list":
                body = json.dumps([{"id": TARGET, "type": "page", "url": bridge.LOGIN_URL,
                    "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.server.server_address[1]}/devtools/page/{TARGET}"}]).encode()
                self.request.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\n")
                if mode in ("http_headers", "combined"):
                    duration = .18 if mode == "combined" else 1.1
                    self.drip([f"X-Synthetic-{i}: value\r\n".encode() for i in range(40)], duration)
                if mode == "http_chunked":
                    self.request.sendall(b"Transfer-Encoding: chunked\r\n\r\n")
                    # Each chunk-size line and chunk is valid. read1 internally
                    # combines framing reads and must retain the same deadline.
                    self.drip([b"1\r\n" + bytes((value,)) + b"\r\n" for value in body[:40]], 1.1)
                    remainder = body[40:]
                    self.request.sendall(f"{len(remainder):x}\r\n".encode() + remainder + b"\r\n0\r\n\r\n")
                else:
                    self.request.sendall(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n")
                    if self.server.before_body:
                        self.server.before_body()
                    self.request.sendall(body)
                closed = self.wait_closed()
                return
            assert path == f"/devtools/page/{TARGET}"
            headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
            accept = base64.b64encode(hashlib.sha1((headers["Sec-WebSocket-Key"]
                + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
            self.request.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n")
            if mode in ("ws_headers", "combined"):
                duration = .18 if mode == "combined" else 1.1
                self.drip([f"X-Synthetic-{i}: value\r\n".encode() for i in range(40)], duration)
            self.request.sendall(b"\r\n")
            first = self.read_exact(2)
            size = first[1] & 127
            if size == 126:
                size = struct.unpack("!H", self.read_exact(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self.read_exact(8))[0]
            mask = self.read_exact(4) if first[1] & 128 else b""
            payload = self.read_exact(size)
            if mask:
                payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            command = json.loads(payload)
            assert command["method"] == "Runtime.evaluate"
            with self.server.lock:
                self.server.commands.append(command["method"])
            body = json.dumps({"id": command["id"], "result": {
                "result": {"value": {"state": "channel_offline"}}}}).encode()
            assert len(body) < 126
            self.request.sendall(bytes((0x81, len(body))))
            if mode in ("ws_frame", "combined"):
                duration = .18 if mode == "combined" else 1.1
                self.drip([bytes((value,)) for value in body[:40]], duration)
                self.request.sendall(body[40:])
            else:
                self.request.sendall(body)
            closed = self.wait_closed()
        except (ConnectionError, EOFError):
            closed = True
        except OSError as error:
            # A peer shutdown can be reported as WSAECONNABORTED/RESET or EPIPE.
            if isinstance(error, socket.timeout):
                self.server.errors.append("unexpected_idle_timeout")
            else:
                closed = True
        except Exception as error:
            self.server.errors.append(type(error).__name__)
        finally:
            if closed and path is not None:
                with self.server.lock:
                    self.server.closed_paths.append(path)


@contextmanager
def cdp_server(mode="normal", before_body=None):
    server = Server(("127.0.0.1", 0), Handler)
    server.mode, server.before_body = mode, before_body
    server.done, server.blocked, server.lock = threading.Event(), threading.Event(), threading.Lock()
    server.handlers, server.paths, server.commands, server.closed_paths, server.errors = [], [], [], [], []
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
    worker.start()
    try:
        yield server
    finally:
        server.done.set()
        server.shutdown()
        server.server_close()
        worker.join(1)
        assert not worker.is_alive()
        for handler in server.handlers:
            handler.join(1)
            assert not handler.is_alive()
        assert server.errors == []


@pytest.fixture
def deadline_timers(monkeypatch):
    timers = []
    actual_timer = threading.Timer
    def tracked_timer(*args, **kwargs):
        timer = actual_timer(*args, **kwargs)
        timers.append(timer)
        return timer
    monkeypatch.setattr(threading, "Timer", tracked_timer)
    monkeypatch.setattr(bridge, "_ACTION_SECONDS", BUDGET)
    yield timers
    assert all(not timer.is_alive() for timer in timers), "an action returned with its timer alive"


def driver_for(server):
    driver = bridge.LoginBrowser({"cdp_port": server.server_address[1]})
    driver._owned_targets.add(TARGET)
    return driver


def assert_released(server, timers):
    assert timers and all(not timer.is_alive() for timer in timers)
    wait_for(lambda: len(server.closed_paths) == len(server.paths))


@pytest.mark.parametrize("mode", ["normal", "http_headers", "http_chunked", "ws_headers", "ws_frame", "combined"])
def test_actual_transport_uses_one_absolute_deadline(mode, deadline_timers):
    with cdp_server(mode) as server:
        started = time.monotonic()
        result = driver_for(server).check(TARGET, UID, lambda: True)
        elapsed = time.monotonic() - started
        if mode == "normal":
            assert result == {"state": "channel_offline", "reason": "profile_offline"}
            assert elapsed < BUDGET
        else:
            assert result == {"state": "unknown", "reason": "timeout"}
            assert elapsed <= BUDGET + .25, (mode, elapsed)
            assert elapsed >= BUDGET - .08, (mode, elapsed)
        assert_released(server, deadline_timers)


def test_owner_loss_after_http_closes_connection_and_prevents_websocket(deadline_timers):
    owner = {"valid": True}
    with cdp_server(before_body=lambda: owner.update(valid=False)) as server:
        result = driver_for(server).check(TARGET, UID, lambda: owner["valid"])
        assert result == {"state": "unknown", "reason": "ownership_changed"}
        assert server.paths == ["/json/list"] and server.commands == []
        assert_released(server, deadline_timers)


def test_deadline_does_not_close_another_actions_connection(deadline_timers):
    with cdp_server("http_headers") as server:
        sentinel = socket.create_connection(server.server_address, timeout=1)
        other = bridge._Action(lambda: True)
        other.deadline = time.monotonic() + 2
        try:
            other.own(sentinel)
            sentinel.sendall(b"GET /sentinel HTTP/1.1\r\nHost: generated\r\n\r\n")
            assert sentinel.recv(5) == b"ready"
            result = driver_for(server).check(TARGET, UID, lambda: True)
            assert result == {"state": "unknown", "reason": "timeout"}
            other.check()
            sentinel.sendall(b"x")
            assert sentinel.recv(1) == b"x"
        finally:
            other.close()
            sentinel.close()
        assert_released(server, deadline_timers)


def test_late_socket_registration_is_closed_without_starting_a_timer(deadline_timers):
    action = bridge._Action(lambda: True)
    action.deadline = time.monotonic() - 1
    stream, peer = socket.socketpair()
    peer.settimeout(1)
    try:
        with pytest.raises(bridge._Failure, match="timeout"):
            action.own(stream)
        assert stream.fileno() == -1
        assert peer.recv(1) == b""
        assert deadline_timers == []
    finally:
        action.close()
        stream.close()
        peer.close()


def test_final_cleanup_joins_an_expiry_callback_already_in_progress(deadline_timers):
    entered, second_abort, release, returned = (threading.Event() for _ in range(4))
    class Stream:
        def shutdown(self, _how):
            if not entered.is_set():
                entered.set()
                assert release.wait(2), "test did not release the timer callback"
            else:
                second_abort.set()
        def close(self):
            pass
    action = bridge._Action(lambda: True)
    action.deadline = time.monotonic() + .04
    action.own(Stream())
    worker = threading.Thread(target=lambda: (action.close(), returned.set()))
    try:
        assert entered.wait(1)
        worker.start()
        assert second_abort.wait(1)
        assert not returned.wait(.04), "cleanup returned before its running callback finished"
    finally:
        release.set()
        if worker.ident is not None:
            worker.join(1)
            assert not worker.is_alive()
        action.close()
    assert returned.is_set()
    assert all(not timer.is_alive() for timer in deadline_timers)


def test_stop_keeps_real_login_http_inflight_until_transport_cleanup(local_boards, deadline_timers):
    with cdp_server("ws_frame") as server:
        item = local_boards("deadline-stop")
        owner = ("generated-owner", server.server_address[1])
        manager = item.manager
        manager.context = dict(item.context, cdp_port=server.server_address[1])
        manager.now = time.monotonic
        manager.login_owner = lambda: owner
        manager.login_browser_status = lambda: {"state": "stopping" if manager.stopping else "ready", "reason": "generated"}
        manager.roles, manager.manual = {}, {}
        closes = []
        manager.browser = SimpleNamespace(close=lambda force=False: closes.append(force) or True)
        guide = manager.login = LoginGuide(manager)
        guide._owner, guide._target_id, guide._driver = owner, TARGET, driver_for(server)
        tag = "generated-deadline-anchor"
        Path(item.context["roster"]).write_text(json.dumps({"version": 1, "anchors": [
            {"tag": tag, "sec_uid": UID, "enabled": False, "watch": False, "record": False, "llm": False}]}), encoding="utf-8")
        auth = _bootstrap(item.port)
        results = []
        worker = threading.Thread(target=lambda: results.append(_request(item.port, "POST",
            "/api/browser-login", {"action": "check", "tag": tag}, auth)))
        worker.start()
        try:
            assert server.blocked.wait(1)
            assert guide.busy and manager.inflight == 1
            manager.stopping = True
            assert not app.RoleManager.drained(manager) and closes == []
            assert _request(item.port, "POST", "/api/browser-login", {"action": "prepare"}, auth)[0] == 503
            assert manager.inflight == 1 and guide.busy
            worker.join(BUDGET + 1)
            assert not worker.is_alive() and results[0][0] == 200
            wait_for(lambda: manager.inflight == 0)
            assert guide.busy is False
            channel = json.loads(results[0][1])["status"]["channel"]
            assert channel["state"] == "unknown" and channel["reason"] == "application_stopping"
            assert_released(server, deadline_timers)
            assert app.RoleManager.drained(manager) and closes == [True]
            assert manager.browser is None
        finally:
            worker.join(2)
            assert not worker.is_alive()
