"""Fake CDP transports and the actual fixed page expression; no platform access."""
import copy
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import login_browser as bridge

UID = "MS4wLjABAAAA" + "x" * 44
MARKER = "synthetic_login_marker_0123456789"
SECRET = "synthetic-private-cookie-and-signed-url"


@pytest.fixture
def cdp(monkeypatch):
    import websocket
    env = SimpleNamespace(port=19222, targets=[{"id": "monitor", "type": "page",
        "url": "https://live.douyin.com/123", "webSocketDebuggerUrl": "ws://127.0.0.1:19222/devtools/page/monitor"}],
        http=[], wires=[], streams=[], commands=[], timeline=[], created=0, remember=[], responses=[],
        value={"state": "channel_offline"}, guard=True, lose_create=False, navigate_error=False,
        after_http=None, after_command=None, ws_timeouts=[], ws_status=101,
        version_ws="ws://127.0.0.1:19222/devtools/browser/synthetic-browser")

    class Response:
        status = 200
        fp = None
        def __init__(self, payload):
            self.payload, self.closed = payload, False
            env.responses.append(self)
        def read1(self, count):
            chunk, self.payload = self.payload[:count], self.payload[count:]
            return chunk
        def close(self):
            self.closed = True

    class Stream:
        def __init__(self):
            self.closed = False
            env.streams.append(self)
        def shutdown(self, how):
            assert how == socket.SHUT_RDWR
        def close(self):
            self.closed = True

    class HTTP:
        def __init__(self, host, port, timeout):
            assert host == "127.0.0.1" and port == env.port and 0 < timeout <= 2
            self.closed = False
            self.sock = None
        def connect(self):
            self.sock = Stream()
        def request(self, method, path, headers):
            assert method == "GET" and headers == {"Connection": "close"}
            self.path = path
            env.http.append(path)
            env.timeline.append(("http", path))
            if env.after_http:
                env.after_http(path)
        def getresponse(self):
            if self.path == "/json/list":
                return Response(json.dumps(env.targets).encode())
            if self.path == "/json/version":
                return Response(json.dumps({"webSocketDebuggerUrl": env.version_ws}).encode())
            assert self.path.startswith("/json/activate/")
            return Response(b"Target activated")
        def close(self):
            self.closed = True
            if self.sock is not None:
                self.sock.close()

    class Wire:
        def __init__(self, url):
            self.url, self.closed = url, False
            self.target = url.rsplit("/", 1)[-1]
            env.wires.append(self)
        def settimeout(self, timeout):
            assert 0 < timeout <= 8
            env.ws_timeouts.append(timeout)
        def getstatus(self):
            return env.ws_status
        def send(self, raw):
            request = json.loads(raw)
            method, parameters = request["method"], request["params"]
            env.commands.append((self.target, method, copy.deepcopy(parameters)))
            env.timeline.append(("cdp", method))
            result = {}
            if method == "Target.createTarget":
                env.created += 1
                target_id = "owned-login-" + str(env.created)
                env.targets.append({"id": target_id, "type": "page", "url": parameters["url"],
                    "webSocketDebuggerUrl": f"ws://127.0.0.1:{env.port}/devtools/page/{target_id}"})
                result = {"targetId": target_id}
                if env.lose_create:
                    env.lose_create = False
                    self.timeout = True
            elif method == "Page.enable":
                assert parameters == {}
            elif method == "Page.navigate":
                assert self.target != "monitor" and parameters == {"url": bridge.LOGIN_URL}
                if env.navigate_error:
                    result = {"errorText": SECRET}
                else:
                    next(row for row in env.targets if row["id"] == self.target)["url"] = parameters["url"]
            elif method == "Runtime.evaluate":
                assert self.target != "monitor" and parameters["returnByValue"] and parameters["awaitPromise"]
                result = {"result": {"value": env.value}}
            else:
                raise AssertionError(method)
            self.response = json.dumps({"id": request["id"], "result": result})
            if env.after_command:
                env.after_command(method)
        def recv(self):
            if getattr(self, "timeout", False):
                raise TimeoutError(SECRET)
            return self.response
        def shutdown(self):
            self.closed = True

    def connect(url, **kwargs):
        assert kwargs["suppress_origin"] is True and kwargs["http_no_proxy"] == ["127.0.0.1", "localhost"]
        assert kwargs["enable_multithread"] is True
        assert kwargs["redirect_limit"] == 0
        assert kwargs["socket"] in env.streams
        return Wire(url)

    def connect_stream(address, *, timeout):
        assert address == ("127.0.0.1", env.port) and 0 < timeout <= 2
        return Stream()

    monkeypatch.setattr(bridge.http.client, "HTTPConnection", HTTP)
    monkeypatch.setattr(websocket, "create_connection", connect)
    monkeypatch.setattr(socket, "create_connection", connect_stream)
    env.driver = bridge.LoginBrowser({"cdp_port": env.port})
    env.open = lambda target=None: env.driver.open(target, MARKER, lambda: env.guard, env.remember.append)
    yield env
    assert all(wire.closed for wire in env.wires)
    assert all(response.closed for response in env.responses)
    assert all(stream.closed for stream in env.streams)


def test_open_owns_one_target_and_never_navigates_monitor(cdp):
    original = copy.deepcopy(cdp.targets[0])
    first = cdp.open()
    assert first["state"] == "page_opened" and cdp.created == 1
    assert cdp.remember == [first["target_id"]]
    assert [method for _, method, _ in cdp.commands] == ["Target.createTarget", "Page.enable", "Page.navigate"]
    assert cdp.open(first["target_id"])["state"] == "page_opened" and cdp.created == 1
    assert cdp.targets[0] == original
    assert all(target != "monitor" for target, _, _ in cdp.commands)


def test_unknown_existing_target_cannot_be_adopted(cdp):
    assert cdp.open("monitor") == {"state": "unknown", "reason": "invalid_target"}
    assert cdp.driver.check("monitor", UID, lambda: True) == {"state": "unknown", "reason": "target_missing"}
    assert cdp.http == cdp.commands == []


def test_open_activates_its_owned_target_before_navigation(cdp):
    result = cdp.open()
    activated = cdp.timeline.index(("http", "/json/activate/" + result["target_id"]))
    enabled = cdp.timeline.index(("cdp", "Page.enable"))
    navigated = cdp.timeline.index(("cdp", "Page.navigate"))
    assert activated < enabled < navigated
    assert all(not path.endswith("/monitor") for path in cdp.http)


def test_invalid_owner_blocks_all_cdp_and_lost_owner_blocks_next_command(cdp):
    cdp.guard = False
    assert cdp.open()["reason"] == "ownership_changed"
    assert cdp.driver.check("monitor", UID, lambda: False)["reason"] == "ownership_changed"
    assert cdp.http == cdp.commands == []
    cdp.guard = True
    cdp.after_http = lambda _path: setattr(cdp, "guard", False)
    assert cdp.open()["reason"] == "ownership_changed"
    assert cdp.commands == []


def test_create_ack_lost_recovers_only_its_exact_marker(cdp):
    cdp.lose_create = True
    assert cdp.open() == {"state": "unknown", "reason": "timeout"}
    assert cdp.created == 1 and cdp.remember == []
    result = cdp.open()
    assert result["state"] == "page_opened" and cdp.created == 1
    assert cdp.remember == [result["target_id"]]


def test_target_is_remembered_before_navigation_failure_and_error_is_safe(cdp):
    cdp.navigate_error = True
    result = cdp.open()
    assert result == {"state": "unknown", "reason": "cdp_unavailable"}
    assert len(cdp.remember) == cdp.created == 1 and SECRET not in json.dumps(result)
    cdp.navigate_error = False
    assert cdp.open(cdp.remember[0])["state"] == "page_opened" and cdp.created == 1


def test_guard_lost_after_creation_does_not_navigate(cdp):
    cdp.after_command = lambda method: setattr(cdp, "guard", False) if method == "Target.createTarget" else None
    assert cdp.open()["reason"] == "ownership_changed"
    assert [method for _, method, _ in cdp.commands] == ["Target.createTarget"]
    assert cdp.remember == []


@pytest.mark.parametrize("bad_url", ["ws://third-party.invalid:19222/devtools/browser/id",
    "ws://127.0.0.1:1/devtools/browser/id", "ws://user@127.0.0.1:19222/devtools/browser/id",
    "wss://127.0.0.1:19222/devtools/browser/id", "ws://127.0.0.1:19222/devtools/browser/id?x=1",
    "ws://127.0.0.1:19222/devtools/page/id", "ws://127.0.0.1:19222/devtools/browser/id/other"])
def test_browser_websocket_authority_is_not_a_proxy(cdp, bad_url):
    cdp.version_ws = bad_url
    assert cdp.open()["reason"] == "cdp_unavailable"
    assert cdp.wires == [] and cdp.created == 0


def test_check_is_single_fixed_evaluate_without_navigation_and_redacts_payload(cdp):
    opened = cdp.open()
    cdp.commands.clear()
    cdp.value = {"state": "channel_offline", "cookie": SECRET, "url": SECRET, "reason": SECRET}
    result = cdp.driver.check(opened["target_id"], UID, lambda: True)
    assert result == {"state": "channel_offline", "reason": "profile_offline"}
    assert [method for _, method, _ in cdp.commands] == ["Runtime.evaluate"]
    assert SECRET not in json.dumps(result) and "logged_in" not in result


def test_check_refuses_foreign_origin_missing_target_and_invalid_identity(cdp):
    target_id = cdp.open()["target_id"]
    cdp.commands.clear()
    row = next(row for row in cdp.targets if row["id"] == target_id)
    row["url"] = "https://www.douyin.com.attacker.invalid/"
    assert cdp.driver.check(target_id, UID, lambda: True)["reason"] == "unexpected_origin"
    assert cdp.commands == []
    cdp.http.clear()
    assert cdp.driver.check(target_id, "bad" + SECRET, lambda: True)["reason"] == "invalid_target"
    assert cdp.http == cdp.commands == []
    cdp.targets.remove(row)
    assert cdp.driver.check(target_id, UID, lambda: True)["reason"] == "target_missing"


def test_total_deadline_is_bounded_across_transport_steps(cdp, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(bridge, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    cdp.after_http = lambda _path: clock.__setitem__(0, clock[0] + 8.0)
    assert cdp.open() == {"state": "unknown", "reason": "timeout"}
    assert cdp.commands == []


def test_websocket_response_wait_uses_remaining_total_budget(cdp, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(bridge, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    cdp.after_command = lambda _method: clock.__setitem__(0, clock[0] + 1.0)
    assert cdp.open()["state"] == "page_opened"
    assert any(2 < value <= 8 for value in cdp.ws_timeouts)


def test_websocket_steps_cannot_extend_the_action_deadline(cdp, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(bridge, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    cdp.after_command = lambda method: clock.__setitem__(0, 8.0) if method == "Page.enable" else None
    assert cdp.open() == {"state": "unknown", "reason": "timeout"}
    assert [method for _, method, _ in cdp.commands] == ["Target.createTarget", "Page.enable"]


def test_owner_lost_on_enabled_response_cannot_dispatch_navigation(cdp):
    cdp.after_command = lambda method: setattr(cdp, "guard", False) if method == "Page.enable" else None
    assert cdp.open()["reason"] == "ownership_changed"
    assert [method for _, method, _ in cdp.commands] == ["Target.createTarget", "Page.enable"]


def test_deadline_reads_do_not_repeat_expensive_ownership_checks(cdp):
    calls = []
    def guard():
        calls.append(1)
        return True
    assert cdp.driver.open(None, MARKER, guard, cdp.remember.append)["state"] == "page_opened"
    # A cold real run spent over half its 8s budget in 51 redundant OS checks.
    # Keep the current fresh before/after checks without charging body chunks again.
    assert 12 <= len(calls) <= 24


def test_websocket_redirect_response_is_never_accepted_or_followed(cdp):
    cdp.ws_status = 302
    assert cdp.open() == {"state": "unknown", "reason": "cdp_unavailable"}
    assert cdp.created == 0 and cdp.commands == []


@pytest.mark.parametrize("fixture,expected", [
    ({"user": {"live_status": 0}}, {"state": "channel_offline"}),
    ({"user": {"live_status": 1, "room_data": {"owner": {"web_rid": "123"}, "status": 2}}}, {"state": "channel_live"}),
    ({"user": {"live_status": 1}}, {"state": "unknown", "reason": "profile_unconfirmed"}),
    ({"user": {"live_status": 0, "room_data": {"status": 2}}}, {"state": "unknown", "reason": "profile_unconfirmed"}),
    ({"user": {"live_status": False}}, {"state": "unknown", "reason": "profile_unconfirmed"}),
    ({"user": {"live_status": 0, "sec_uid": "other"}}, {"state": "unknown", "reason": "target_mismatch"}),
    ({"user": {"live_status": 0, "room_data": "not-json"}}, {"state": "unknown", "reason": "profile_unconfirmed"}),
    ({"status_code": 1}, {"state": "restricted"}),
    ({"user": None}, {"state": "restricted"}),
    ({"http_status": 403}, {"state": "restricted"}),
    ({"http_status": 500}, {"state": "unknown", "reason": "profile_unconfirmed"}),
    ({"throw_name": "AbortError"}, {"state": "unknown", "reason": "timeout"}),
    ({"origin": "https://third-party.invalid"}, {"state": "unknown", "reason": "unexpected_origin"}),
])
def test_actual_profile_expression_projects_only_coarse_channel_evidence(fixture, expected):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required only to execute the actual fixed JS expression in a fake VM")
    user = {"sec_uid": UID, "nickname": SECRET, "cookie": SECRET, "stream_url": SECRET}
    if isinstance(fixture.get("user", {}), dict):
        user.update(fixture.get("user", {}))
    elif "user" in fixture:
        user = fixture["user"]
    value = {"status_code": fixture.get("status_code", 0), "user": user}
    script = """import vm from 'node:vm';
const input = JSON.parse(process.argv[1]); let calls = 0;
const ctx = {location:{origin:input.origin}, AbortController, setTimeout, clearTimeout,
  fetch:async (url, options) => { calls++;
    if (url !== input.api || options.redirect !== 'error' || options.credentials !== 'include') throw Error('bad fixed request');
    if(input.throw_name) { const e=Error(input.secret);e.name=input.throw_name;throw e; }
    return {status:input.status,ok:input.status===200,json:async()=>input.value}; }};
const result=await vm.runInNewContext(input.expression,ctx);
console.log(JSON.stringify({result,calls}));"""
    payload = {"expression": bridge._profile_script(UID), "origin": fixture.get("origin", "https://www.douyin.com"),
        "status": fixture.get("http_status", 200), "throw_name": fixture.get("throw_name"), "secret": SECRET,
        "value": value, "api": bridge.PROFILE_API.format(sec=UID)}
    result = subprocess.run([node, "--input-type=module", "-e", script, json.dumps(payload)],
        capture_output=True, text=True, encoding="utf-8", timeout=4,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["result"] == expected
    assert observed["calls"] == (0 if fixture.get("origin") else 1)
    assert SECRET not in result.stdout and UID not in result.stdout
