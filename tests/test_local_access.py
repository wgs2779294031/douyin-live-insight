"""Actual loopback HTTP, generated state and intercepted business effects only."""
import http.client
from email.message import Message
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
import local_app as app
import assist_board_server as board
import anchor_manager
import anchor_roster
import local_access as access_api


def _free_port():
    with socket.socket() as stream:
        stream.bind(("127.0.0.1", 0))
        return stream.getsockname()[1]


def _request(port, method, path, body=None, headers=()):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    pairs = list(headers.items()) if isinstance(headers, dict) else list(headers)
    if not any(name.lower() == "host" for name, _ in pairs):
        pairs.append(("Host", f"127.0.0.1:{port}"))
    raw = json.dumps(body).encode("utf-8") if body is not None else b""
    try:
        connection.putrequest(method, path, skip_host=True)
        for name, value in pairs:
            connection.putheader(name, value)
        if body is not None:
            connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(len(raw)))
        connection.endheaders(raw)
        response = connection.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        connection.close()


def _bootstrap(port, headers=()):
    status, raw, response = _request(port, "POST", "/api/local/session", {}, headers)
    assert status == 200, (status, raw)
    result = json.loads(raw)
    assert result["mode"] == "local" and result["user"] and result["role"]
    assert isinstance(result["write_token"], str) and len(result["write_token"]) >= 32
    cookie = response["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie
    assert "Access-Control-Allow-Origin" not in response
    return {"Cookie": cookie.split(";", 1)[0], "X-Local-Write-Token": result["write_token"]}


@pytest.fixture
def local_boards(tmp_path, monkeypatch):
    # create_board owns its real HTTP/broadcast threads. Its unrelated data poll
    # waits on this fixture event, so no historical data or process is inspected.
    poll_stop = threading.Event()
    poll_threads = []
    def controlled_poll(_self):
        poll_threads.append(threading.current_thread())
        poll_stop.wait(10)
    monkeypatch.setattr(board.Store, "poll_loop", controlled_poll)
    monkeypatch.setattr(board, "SSE_INTERVAL", .02)
    for module, fields in ((anchor_roster, ("ROSTER_PATH",)),
        (anchor_manager, ("DATA", "ROOT", "PY", "_CUSTOMER_CONTROLLER"))):
        for field in fields:
            monkeypatch.setattr(module, field, getattr(module, field))
    active = []

    def start(name="generated", context=None, account=None):
        if context is None:
            base = tmp_path / name
            base.mkdir()
            data, dist = base / "数据", base / "静态"
            data.mkdir()
            dist.mkdir()
            (dist / "index.html").write_text("<!doctype html><title>generated</title>", encoding="utf-8")
            config, roster = base / "主配置.json", base / "名册.json"
            config.write_text('{"rooms":[],"llm":{"enabled":false,"providers":{}}}', encoding="utf-8")
            roster.write_text('{"version":1,"anchors":[]}', encoding="utf-8")
            receipts = data / "backfill_tasks"
            receipts.mkdir()
            (receipts / "synthetic-existing-state.json").write_text('{"sentinel":"unchanged"}', encoding="utf-8")
            context = app.context_from_args(SimpleNamespace(root=str(ROOT), data=str(data),
                config=str(config), roster=str(roster), dist=str(dist), python=sys.executable,
                port=_free_port(), cdp_port=_free_port()))
        if account is not None:
            (Path(context["data"]) / "board_users.json").write_bytes(account)
        manager = SimpleNamespace(lock=threading.RLock(), stopping=False, inflight=0,
            spawn_manual=lambda *_args, **_kwargs: pytest.fail("must not spawn a manual process"))
        server, worker, broadcaster = app.create_board(context, manager, {"pid": os.getpid(), "created": 1})
        item = SimpleNamespace(context=context, manager=manager, server=server, worker=worker,
            broadcaster=broadcaster, port=server.server_address[1], closed=False)
        active.append(item)
        return item

    def close(item):
        if item.closed:
            return
        item.closed = True
        item.broadcaster.stop()
        item.server.shutdown()
        item.server.server_close()
        item.worker.join(2)
        assert not item.worker.is_alive()
        if item.broadcaster._thread:
            item.broadcaster._thread.join(2)
            assert not item.broadcaster._thread.is_alive()

    start.close = close
    yield start
    poll_stop.set()
    for item in reversed(active):
        close(item)
    for worker in poll_threads:
        worker.join(2)
        assert not worker.is_alive()


def _state_bytes(item):
    return {str(path): path.read_bytes() for path in (
        Path(item.context["config"]), Path(item.context["roster"]),
        *Path(item.context["data"]).rglob("*.json")) if path.is_file()}


def test_local_mode_needs_no_account_and_does_not_touch_existing_users(local_boards):
    missing = local_boards("without-account")
    assert not (Path(missing.context["data"]) / "board_users.json").exists()
    status, body, _ = _request(missing.port, "GET", "/api/access")
    assert status == 200 and json.loads(body) == {"mode": "local"}
    auth = _bootstrap(missing.port)
    assert _request(missing.port, "GET", "/api/me", headers=auth)[0] == 200
    assert not (Path(missing.context["data"]) / "board_users.json").exists()
    existing = local_boards("with-account", account=b'{"users":"synthetic-do-not-read"}\n')
    before = (Path(existing.context["data"]) / "board_users.json").read_bytes()
    _bootstrap(existing.port)
    assert (Path(existing.context["data"]) / "board_users.json").read_bytes() == before


def test_cli_bootstrap_safe_reads_and_token_is_not_public(local_boards):
    item = local_boards()
    assert _request(item.port, "GET", "/api/live")[0] == 401
    auth = _bootstrap(item.port)  # Intentionally no Origin or browser headers.
    for route in ("/api/me", "/api/live", "/snapshot", "/api/health", "/api/access"):
        status, raw, response = _request(item.port, "GET", route, headers={"Cookie": auth["Cookie"]})
        assert status == 200, (route, status, raw)
        assert auth["X-Local-Write-Token"].encode() not in raw
        assert "Access-Control-Allow-Origin" not in response
    assert _request(item.port, "GET", "/api/live", headers={**auth, "Sec-Fetch-Site": "same-origin"})[0] == 200
    for route in ("/live", "/review", "/anchors", "/settings", "/login"):
        assert _request(item.port, "GET", route)[0] == 200


@pytest.mark.parametrize("headers", [
    {"Origin": "https://third-party.invalid"}, {"Origin": "null"},
    {"Origin": "http://127.0.0.1:1"}, {"Origin": "http://localhost:1"},
    {"Origin": "http://127.0.0.1:1/path"},
    {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
    {"Host": "third-party.invalid:8765"}, {"Host": "127.0.0.1:1"},
    {"Host": "127.0.0.1"}, {"Host": "127.0.0.1:8765, localhost:8765"},
    [("Host", "127.0.0.1:1"), ("Host", "localhost:1")],
])
def test_bootstrap_rejects_host_source_and_duplicate_host(local_boards, headers):
    item = local_boards()
    before = _state_bytes(item)
    status, raw, response = _request(item.port, "POST", "/api/local/session", {}, headers)
    assert status in (400, 403), (status, raw)
    assert "Set-Cookie" not in response and "write_token" not in raw.decode("utf-8")
    assert _state_bytes(item) == before
    assert item.manager.inflight == 0


def test_exact_origin_port_and_localhost_alias(local_boards):
    item = local_boards()
    auth = _bootstrap(item.port, {"Origin": f"http://127.0.0.1:{item.port}", "Sec-Fetch-Site": "same-origin"})
    assert _request(item.port, "GET", "/api/live", headers=auth)[0] == 200
    alias = _bootstrap(item.port, {"Host": f"localhost:{item.port}", "Origin": f"http://localhost:{item.port}"})
    assert _request(item.port, "GET", "/api/live", headers={**alias, "Host": f"localhost:{item.port}"})[0] == 200
    # X-Forwarded-* never repairs a false Host or Origin.
    for extra in ({"Host": "third-party.invalid", "X-Forwarded-Host": f"127.0.0.1:{item.port}"},
        {"Origin": "https://third-party.invalid", "X-Forwarded-Host": "third-party.invalid"}):
        assert _request(item.port, "GET", "/api/live", headers={**auth, **extra})[0] == 403
    for duplicated in ([("Host", f"127.0.0.1:{item.port}")] * 2,
        [("Origin", f"http://127.0.0.1:{item.port}")] * 2):
        assert _request(item.port, "POST", "/api/local/session", {}, duplicated)[0] in (400, 403)


MUTATIONS = [
    ("POST", "/api/anchors", "_anchor_add"),
    ("POST", "/api/anchors/start", "_anchor_start"),
    ("POST", "/api/anchors/stop", "_anchor_stop"),
    ("POST", "/api/anchors/restart", "_anchor_restart"),
    ("PATCH", "/api/anchors?tag=synthetic", "_anchor_patch"),
    ("DELETE", "/api/anchors?tag=synthetic&confirm=synthetic", "_anchor_delete"),
    ("POST", "/api/llm/gate", "_llm_gate"),
    ("POST", "/api/llm/provider", "_llm_provider"),
    ("POST", "/api/llm/budget", "_llm_budget"),
    ("POST", "/api/llm/settings", "_llm_model_settings"),
    ("POST", "/api/llm/key/delete", "_llm_model_settings"),
    ("POST", "/api/llm/test", "_llm_model_settings"),
    ("POST", "/api/llm/prices", "_llm_prices"),
    ("POST", "/api/review/backfill", "_backfill_start"),
    ("GET", "/api/review/backfill?tag=synthetic&date=2026-11-15", "_backfill_read"),
]


@pytest.mark.parametrize("method,route,callback", MUTATIONS)
def test_each_mutating_route_requires_cookie_and_write_token(local_boards, monkeypatch, method, route, callback):
    item = local_boards()
    auth = _bootstrap(item.port)
    marker = Path(item.context["data"]) / "business-effects.jsonl"
    # Intercept at the business handler, after the real production route dispatch.
    def effect(handler, *_args, **_kwargs):
        with marker.open("a", encoding="utf-8") as stream:
            stream.write("synthetic-side-effect\n")
        return handler._json(200, {"ok": True})
    monkeypatch.setattr(item.server.RequestHandlerClass, callback, effect)
    before = _state_bytes(item)
    variants = [None, {"Cookie": auth["Cookie"]},
        {"Cookie": auth["Cookie"], "X-Local-Write-Token": "synthetic-wrong"},
        {"X-Local-Write-Token": auth["X-Local-Write-Token"]},
        {**auth, "Origin": "https://third-party.invalid"}]
    for headers in variants:
        status, raw, _ = _request(item.port, method, route, {} if method != "GET" else None, headers or {})
        assert status in (401, 403), (method, route, status, raw)
        assert not marker.exists() and _state_bytes(item) == before
        assert item.manager.inflight == 0
    # A token in a URL is not a substitute for the protected header.
    query = "&" if "?" in route else "?"
    status, _, _ = _request(item.port, method, route + query + "write_token=" + auth["X-Local-Write-Token"],
        {} if method != "GET" else None, {"Cookie": auth["Cookie"]})
    assert status == 403 and not marker.exists()
    status, raw, _ = _request(item.port, method, route, {} if method != "GET" else None,
        {**auth, "Origin": f"http://127.0.0.1:{item.port}", "Sec-Fetch-Site": "same-origin"})
    assert status == 200, (method, route, status, raw)
    assert marker.read_text(encoding="utf-8").splitlines() == ["synthetic-side-effect"]
    assert _state_bytes(item) == before and item.manager.inflight == 0


def test_no_origin_cli_can_write_only_after_bootstrap(local_boards, monkeypatch):
    item = local_boards()
    calls = []
    def effect(handler):
        calls.append(1)
        return handler._json(200, {"ok": True})
    monkeypatch.setattr(item.server.RequestHandlerClass, "_llm_gate", effect)
    assert _request(item.port, "POST", "/api/llm/gate", {})[0] == 401
    assert calls == []
    auth = _bootstrap(item.port)
    assert _request(item.port, "POST", "/api/llm/gate", {}, auth)[0] == 200
    assert calls == [1]


def test_other_instance_and_previous_boot_credentials_fail(local_boards, monkeypatch):
    first, second = local_boards("first"), local_boards("second")
    first_auth, second_auth = _bootstrap(first.port), _bootstrap(second.port)
    calls = []
    def effect(handler):
        calls.append(1)
        return handler._json(200, {"ok": True})
    monkeypatch.setattr(second.server.RequestHandlerClass, "_llm_gate", effect)
    assert _request(second.port, "POST", "/api/llm/gate", {}, first_auth)[0] in (401, 403)
    assert _request(second.port, "POST", "/api/llm/gate", {},
        {**second_auth, "X-Local-Write-Token": first_auth["X-Local-Write-Token"]})[0] == 403
    assert calls == []
    context = first.context
    local_boards.close(first)
    restarted = local_boards(context=context)
    monkeypatch.setattr(restarted.server.RequestHandlerClass, "_llm_gate", effect)
    assert _request(restarted.port, "POST", "/api/llm/gate", {}, first_auth)[0] in (401, 403)
    fresh_auth = _bootstrap(restarted.port)
    assert fresh_auth["X-Local-Write-Token"] != first_auth["X-Local-Write-Token"]
    assert _request(restarted.port, "POST", "/api/llm/gate", {}, fresh_auth)[0] == 200
    assert calls == [1]


def test_sse_uses_http_only_cookie_without_token_in_url(local_boards):
    item = local_boards()
    assert _request(item.port, "GET", "/api/sse")[0] == 401
    auth = _bootstrap(item.port)
    connection = http.client.HTTPConnection("127.0.0.1", item.port, timeout=3)
    response = None
    try:
        connection.request("GET", "/api/sse", headers={"Cookie": auth["Cookie"]})
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type").startswith("text/event-stream")
        assert response.fp.readline().startswith(b": ")
        assert item.manager.inflight == 0
    finally:
        if response is not None:
            response.close()
        connection.close()
    deadline = time.monotonic() + 3
    while item.broadcaster.subscribers and time.monotonic() < deadline:
        item.broadcaster._push(("ping", ""))
        time.sleep(.02)
    assert item.broadcaster.subscribers == 0


def test_stop_gate_still_blocks_new_controls_and_reconcile_get(local_boards, monkeypatch):
    item = local_boards()
    auth = _bootstrap(item.port)
    calls = []
    def effect(handler, *_args, **_kwargs):
        calls.append(1)
        return handler._json(200, {"ok": True})
    monkeypatch.setattr(item.server.RequestHandlerClass, "_llm_gate", effect)
    monkeypatch.setattr(item.server.RequestHandlerClass, "_backfill_read", effect)
    item.manager.stopping = True
    for method, route in (("POST", "/api/llm/gate"), ("GET", "/api/review/backfill")):
        assert _request(item.port, method, route, {} if method == "POST" else None, auth)[0] == 503
        assert _request(item.port, method, route, {} if method == "POST" else None)[0] in (401, 403)
    assert calls == [] and item.manager.inflight == 0
    assert _request(item.port, "GET", "/api/live", headers=auth)[0] == 200


def test_preexisting_control_is_counted_until_it_finishes_after_stop(local_boards, monkeypatch):
    item = local_boards()
    auth = _bootstrap(item.port)
    entered, release = threading.Event(), threading.Event()
    calls, results = [], []
    def effect(handler):
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return handler._json(200, {"ok": True})
    monkeypatch.setattr(item.server.RequestHandlerClass, "_llm_gate", effect)
    client = threading.Thread(target=lambda: results.append(_request(item.port, "POST", "/api/llm/gate", {}, auth)))
    client.start()
    try:
        assert entered.wait(2) and item.manager.inflight == 1
        item.manager.stopping = True
        assert _request(item.port, "POST", "/api/llm/gate", {}, auth)[0] == 503
        assert calls == [1] and item.manager.inflight == 1
    finally:
        release.set()
        client.join(3)
    assert not client.is_alive() and results[0][0] == 200 and item.manager.inflight == 0


def test_ambiguous_cookie_or_token_and_non_ascii_token_are_rejected(local_boards, monkeypatch):
    item = local_boards()
    auth = _bootstrap(item.port)
    calls = []
    def effect(handler):
        calls.append(1)
        return handler._json(200, {"ok": True})
    monkeypatch.setattr(item.server.RequestHandlerClass, "_llm_gate", effect)
    variants = [
        [("Cookie", auth["Cookie"]), ("Cookie", auth["Cookie"]), ("X-Local-Write-Token", auth["X-Local-Write-Token"])],
        [("Cookie", auth["Cookie"]), ("X-Local-Write-Token", auth["X-Local-Write-Token"]),
         ("X-Local-Write-Token", auth["X-Local-Write-Token"])],
        {**auth, "X-Local-Write-Token": "\xff"},
    ]
    for headers in variants:
        assert _request(item.port, "POST", "/api/llm/gate", {}, headers)[0] == 403
        assert calls == [] and item.manager.inflight == 0


def test_parameterized_paths_cannot_bypass_write_or_legacy_gate(local_boards, monkeypatch):
    item = local_boards()
    auth = _bootstrap(item.port)
    calls = []
    def effect(handler):
        calls.append(1)
        return handler._json(200, {"ok": True})
    monkeypatch.setattr(item.server.RequestHandlerClass, "_backfill_read", effect)
    assert _request(item.port, "GET", "/api/review/backfill;tail",
        headers={"Cookie": auth["Cookie"]})[0] in (400, 403)
    assert calls == []
    for route in ("/legacy;tail", "/legacy/;tail"):
        assert _request(item.port, "GET", route, headers=auth)[0] in (400, 403, 404, 410)
    for route in ("/api/login;tail", "/api/logout;tail", "/api/passwd;tail"):
        assert _request(item.port, "POST", route, {}, auth)[0] in (400, 403, 404, 410)


def test_bootstrap_preserves_bounded_body_and_transport_rejection(local_boards):
    item = local_boards()
    before = _state_bytes(item)
    for body, headers, expected in (({"padding": "x" * (70 * 1024)}, {}, 413),
        ({}, {"Content-Length": "2"}, 400), ({}, {"Transfer-Encoding": "chunked"}, 400)):
        status, _, response = _request(item.port, "POST", "/api/local/session", body, headers)
        assert status == expected and "Set-Cookie" not in response
    assert _state_bytes(item) == before and item.manager.inflight == 0


def test_local_legacy_and_password_endpoints_are_disabled(local_boards):
    item = local_boards()
    auth = _bootstrap(item.port)
    for route in ("/legacy", "/legacy/"):
        status, raw, response = _request(item.port, "GET", route, headers=auth)
        assert status in (404, 410)
        assert "Location" not in response and b"type=\"password\"" not in raw
    for route in ("/api/login", "/api/logout", "/api/passwd"):
        assert _request(item.port, "POST", route, {}, auth)[0] in (404, 410)
    assert not (Path(item.context["data"]) / "board_users.json").exists()


def test_standalone_board_keeps_account_mode_and_login(tmp_path):
    from test_board_server_auth import running_board
    with running_board(tmp_path) as (port, password, _db):
        status, raw, _ = _request(port, "GET", "/api/access")
        assert status == 200 and json.loads(raw) == {"mode": "account"}
        assert _request(port, "GET", "/api/live")[0] == 401
        status, _, response = _request(port, "POST", "/api/login", {"username": "admin", "password": password})
        assert status == 200
        cookie = response["Set-Cookie"].split(";", 1)[0]
        assert _request(port, "GET", "/api/me", headers={"Cookie": cookie})[0] == 200


def _authority_request(host, origin=None, port=80):
    headers = Message()
    headers["Host"] = host
    if origin is not None:
        headers["Origin"] = origin
    headers["Sec-Fetch-Site"] = "same-origin"
    return SimpleNamespace(path="/api/access", client_address=("127.0.0.1", 1),
        server=SimpleNamespace(server_address=("127.0.0.1", port)), headers=headers)


@pytest.mark.parametrize("alias", ["127.0.0.1", "localhost"])
def test_default_http_authority_and_session_use_effective_port(tmp_path, alias):
    from test_local_app import make_context
    context = make_context(tmp_path / "generated-only", ports=(80, 19222))
    assert context["port"] == 80  # Parsed only: no socket is opened or probed on 80.
    access = access_api.LocalAccess("synthetic-default-http")
    observations = []
    for explicit in (False, True):
        host = alias + (":80" if explicit else "")
        alternate = alias + ("" if explicit else ":80")
        for origin in (None, "http://" + alias, "http://" + alias + ":80"):
            request = _authority_request(host, origin)
            assert access.source_ok(request)
            body, cookie = access.bootstrap(request)
            resumed = _authority_request(alternate, origin)
            resumed.headers["Cookie"] = cookie.split(";", 1)[0]
            resumed.headers["X-Local-Write-Token"] = body["write_token"]
            assert access.source_ok(resumed) and access.authenticated(resumed) and access.write_allowed(resumed)
            fresh, fresh_cookie = access.bootstrap(resumed)
            assert fresh["write_token"] == body["write_token"] and fresh_cookie == cookie
            other = _authority_request("localhost" if alias == "127.0.0.1" else "127.0.0.1")
            other.headers["Cookie"] = resumed.headers["Cookie"]
            other.headers["X-Local-Write-Token"] = body["write_token"]
            assert access.source_ok(other) and not access.authenticated(other) and not access.write_allowed(other)
            observations.append({"host": host, "origin": origin, "alternate_host": alternate,
                "source_allowed": True, "same_session_and_write_permission": True, "other_alias_denied": True})
    (tmp_path / "authority-observation.json").write_text(json.dumps({"real_port_bound": False,
        "context_port": context["port"], "observations": observations}, indent=2), encoding="utf-8")


@pytest.mark.parametrize("alias", ["127.0.0.1", "localhost"])
def test_nondefault_authority_still_requires_explicit_port(alias):
    access = access_api.LocalAccess("synthetic-nondefault-http")
    host = alias + ":8765"
    for origin in (None, "http://" + host):
        assert access.source_ok(_authority_request(host, origin, 8765))
    assert not access.source_ok(_authority_request(alias, port=8765))
    assert not access.source_ok(_authority_request(alias + ":80", port=8765))
    assert not access.source_ok(_authority_request(host, "http://" + alias, 8765))
    assert not access.source_ok(_authority_request(host, "http://" + alias + ":80", 8765))


@pytest.mark.parametrize("host,origin", [
    ("127.0.0.1:81", None), ("localhost:080", None), ("127.0.0.1:", None),
    ("localhost:80:80", None), ("localhost.", None), ("LOCALHOST", None),
    ("2130706433", None), ("127.0.0.2", None), ("third-party.invalid", None),
    ("user@localhost", None), ("localhost/", None), ("localhost?x", None),
    ("localhost#x", None), ("localhost,127.0.0.1", None), (" localhost", None), ("localhost ", None),
    ("localhost", "https://localhost"), ("localhost:80", "https://localhost:80"),
    ("localhost", "http://localhost:81"), ("localhost", "http://127.0.0.1"),
    ("127.0.0.1:80", "http://localhost:80"), ("localhost", "http://localhost:080"),
    ("localhost", "http://localhost:"), ("localhost", "http://localhost/"),
    ("localhost", "http://localhost?x"), ("localhost", "http://localhost#x"),
    ("localhost", "http://user@localhost"), ("localhost", "null"), ("localhost", ""),
])
def test_authority_normalization_rejects_wrong_or_malformed_origins(host, origin):
    access = access_api.LocalAccess("synthetic-reject-authority")
    assert not access.source_ok(_authority_request(host, origin))
