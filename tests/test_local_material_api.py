"""9B real loopback requests; only this run's generated owner-derived bytes."""
from contextlib import contextmanager
import copy
import http.client
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlencode
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
import local_app as app
from scripts import audio_cleanup, audio_segments as audio, material_common as common
from scripts import local_material_api as api
from scripts.local_materials import MaterialStore
from test_local_access import local_boards, _bootstrap, _request

OUT = Path(os.environ.get("DLI_TEST_ROOT", str(Path(__file__).resolve().parents[1] / ".test-state")))


def request_id():
    return str(uuid.uuid4())


def empty_builder(*_args, **_kwargs):
    return {"groups": [], "files": [], "catalog": None, "discovery": {"files_checked": 0}}


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(.005)
    assert predicate()


def parsed(item, method, path, body=None, auth=()):
    code, raw, headers = _request(item.port, method, path, body, auth)
    return code, json.loads(raw), headers


def state(item, auth, rid=None, sid=None):
    query = "?" + urlencode({"request_id": rid, "service_id": sid or item.server.materials.service_id}) if rid else ""
    code, body, _ = parsed(item, "GET", "/api/materials/status" + query, auth=auth)
    assert code == 200
    return body


def bytes_under(path):
    return {str(file.relative_to(path)): file.read_bytes() for file in path.rglob("*") if file.is_file()}


def bind_drained(item):
    manager = item.manager
    manager.asr = SimpleNamespace(drained=lambda: True)
    manager.login, manager.manual, manager.roles, manager.browser = SimpleNamespace(busy=False), {}, {}, None
    return lambda: app.RoleManager.drained(manager)


@pytest.fixture
def boards(local_boards, tmp_path, monkeypatch):
    assert tmp_path.resolve().is_relative_to(OUT.resolve())
    roots = []
    native = audio_cleanup._disposition_flag
    def scoped(guard, flag):
        path = guard.path.absolute()
        assert any(path.is_relative_to(root) and (root / "owner.json").is_file() for root in roots), path
        assert path.suffix != ".flv"
        assert path.is_relative_to(tmp_path.absolute())
        return native(guard, flag)
    monkeypatch.setattr(audio_cleanup, "_disposition_flag", scoped)
    monkeypatch.setattr(audio_cleanup, "cleanup_segment", lambda *_a, **_k: pytest.fail("HTTP never cleans audio"))
    def start(*, ready=False, name="generated-http"):
        item = local_boards(name)
        item.materials = item.server.materials
        assert item.materials.store is not None
        roots.append(item.materials.store.root)
        if ready:
            result = item.materials.store.refresh(builder=empty_builder)
            assert result["state"] == "published", result
            item.initial_version = result["version"]
        item.auth = _bootstrap(item.port)
        wait_for(lambda: item.manager.inflight == 0)
        return item
    return start


def raw_request(item, path, raw, auth, *, method="POST", headers=()):
    connection = http.client.HTTPConnection("127.0.0.1", item.port, timeout=3)
    try:
        connection.putrequest(method, path)
        for name, value in list(auth.items()) + list(headers):
            connection.putheader(name, value)
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(len(raw)))
        connection.endheaders(raw)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_auth_and_small_strict_post_before_any_refresh(boards, monkeypatch):
    item = boards()
    calls = []
    monkeypatch.setattr(item.materials.store, "refresh", lambda: calls.append(True))
    for route in ("status", "groups", "query"):
        assert parsed(item, "GET", "/api/materials/" + route)[0] == 401
    for auth in ({}, {"Cookie": item.auth["Cookie"]}, dict(item.auth, Origin="https://outside.invalid"),
                 dict(item.auth, Host="outside.invalid")):
        assert parsed(item, "POST", "/api/materials/refresh", {"request_id": request_id()}, auth)[0] in (401, 403)
    rid = request_id()
    for raw in (b"{}", b"[]", b"null", b'{"request_id":"bad"}',
                json.dumps({"request_id": rid, "force": True}).encode(),
                ('{"request_id":"' + rid + '","request_id":"' + rid + '"}').encode(),
                b'{"request_id":NaN}', b"\xff"):
        code, body = raw_request(item, "/api/materials/refresh", raw, item.auth)
        assert code == 400 and body["code"] == "invalid_refresh_request"
    assert raw_request(item, "/api/materials/refresh", b"x" * 513, item.auth)[0] == 413
    assert parsed(item, "POST", "/api/materials/refresh?force=1", {"request_id": rid}, item.auth)[0] == 400
    assert calls == [] and not item.materials.store.root.exists()
    wait_for(lambda: item.manager.inflight == 0)


def test_strict_get_parameters_and_methods_are_rejected_before_not_ready(boards):
    item = boards()
    bad = [
        ("query", "unknown=1", "invalid_parameters"), ("query", "view=canonical&view=shared", "invalid_parameters"),
        ("groups", "stream=speech", "invalid_parameters"), ("query", "room=", "invalid_parameters"),
        ("query", "view", "invalid_parameters"), ("query", "room=%ff", "invalid_parameters"),
        ("query", "room=%GG", "invalid_parameters"), ("query", "view=both", "invalid_filter"),
        ("query", "date=2026-02-30", "invalid_filter"), ("query", "platform=all", "invalid_filter"),
        ("query", "limit=050", "invalid_limit"), ("query", "limit=201", "invalid_limit"),
        ("query", "limit=true", "invalid_limit"), ("query", "limit=50.0", "invalid_limit"),
        ("query", "time_from=NaN", "invalid_filter"), ("query", "time_until=1e999", "invalid_filter"),
        ("query", "time_from=3&time_until=2", "invalid_filter"), ("query", "cursor=bad", "invalid_cursor"),
        ("query", "&".join(["stream=speech"] * 7), "invalid_parameters"),
        ("status", "request_id=" + request_id(), "invalid_parameters"),
        ("status", "service_id=bad&request_id=" + request_id(), "invalid_parameters"),
    ]
    for route, query, expected in bad:
        code, body, _ = parsed(item, "GET", "/api/materials/" + route + "?" + query, auth=item.auth)
        assert (code, body["code"]) == (400, expected), (route, query, body)
    for method, route in (("GET", "refresh"), ("POST", "status"), ("PATCH", "query"), ("DELETE", "groups")):
        assert parsed(item, method, "/api/materials/" + route, {}, item.auth)[0] == 405
    assert parsed(item, "GET", "/api/materials/nope", auth=item.auth)[0] == 404
    assert not item.materials.store.root.exists()


def test_not_ready_gets_have_zero_initialization_discovery_or_business_effect(boards, monkeypatch):
    from scripts import material_facts, transcript_results as results
    item = boards()
    before = bytes_under(Path(item.context["data"]).parent)
    def forbidden(*_args, **_kwargs):
        raise AssertionError("GET caused initialization/discovery/business action")
    with monkeypatch.context() as marks:
        for function in ("refresh", "_acquire_refresh"):
            marks.setattr(item.materials.store, function, forbidden)
        marks.setattr(common, "write_json", forbidden)
        marks.setattr(material_facts, "build_materials", forbidden)
        marks.setattr(results, "validate_success", forbidden)
        marks.setattr(audio, "fingerprint", forbidden)
        marks.setattr(os, "scandir", forbidden)
        for _ in range(2):
            status = state(item, item.auth)
            assert status["material"]["state"] == "not_ready" and status["operation"] is None
            assert status["material"]["version"] is None
            for route in ("groups", "query"):
                assert parsed(item, "GET", "/api/materials/" + route, auth=item.auth)[0] == 404
    assert before == bytes_under(Path(item.context["data"]).parent)
    assert not item.materials.store.root.exists()


def test_actual_refresh_and_query_use_only_generated_data_and_safe_pages(boards):
    item = boards()
    data = Path(item.context["data"])
    text = "task9B <script>alert(1)</script> https://example.invalid 13800138000"
    (data / "danmu_douyin_removed-room_2026-11-01.jsonl").write_bytes(common.encode(
        {"ts": 1793505600, "type": "danmaku", "text": text}))
    (data / "speech_only-room_2026-11-01.jsonl").write_bytes(common.encode(
        {"ts": 1793505600, "text": "historical speech", "seg": "seg_0001", "off": 0, "dur": 1}))
    before = bytes_under(data)
    rid = request_id()
    code, response, _ = parsed(item, "POST", "/api/materials/refresh", {"request_id": rid}, item.auth)
    assert code == 200 and response["operation"]["state"] == "published", response
    assert response["operation"]["request_id"] == rid
    assert response["material"]["state"] == "ready" and response["material"]["version"] == response["operation"]["version"]
    code, groups, _ = parsed(item, "GET", "/api/materials/groups?limit=1", auth=item.auth)
    assert code == 200 and groups["next_cursor"] and groups["items"][0]["window_is_session"] is False
    code, following, _ = parsed(item, "GET", "/api/materials/groups?limit=1&cursor=" + groups["next_cursor"], auth=item.auth)
    assert code == 200 and {group["tag"] for group in groups["items"] + following["items"]} == {"only-room", "removed-room"}
    code, page, _ = parsed(item, "GET", "/api/materials/query?view=shared&stream=danmu&stream=speech", auth=item.auth)
    assert code == 200 and len(page["items"]) == 2
    assert {row["stream"] for row in page["items"]} == {"danmu", "speech"}
    assert "https://example.invalid" not in repr(page) and "13800138000" not in repr(page)
    assert page["version"] == groups["version"] and page["source_freshness"] == "snapshot_at_build_raw_not_rechecked"
    assert before == bytes_under(data)


def test_status_separates_not_ready_ready_corrupt_unavailable_and_calls_finish(boards, monkeypatch):
    item = boards()
    assert state(item, item.auth)["material"]["state"] == "not_ready"
    # A checked owner with no current is not_ready; no lock file is initialized.
    item.materials.store._owner(create=True)
    assert state(item, item.auth)["material"]["state"] == "not_ready"
    assert not (item.materials.store.root / ".refresh.lock").exists()
    assert item.materials.store.refresh(builder=empty_builder)["state"] == "published"
    original, finishes = item.materials.store.snapshot, []
    @contextmanager
    def observed():
        with original() as snapshot:
            finish = snapshot.finish
            def checked():
                finishes.append(True)
                return finish()
            snapshot.finish = checked
            yield snapshot
    def unavailable():
        raise PermissionError("C:/private-material-path")
    with monkeypatch.context() as marks:
        marks.setattr(item.materials.store, "snapshot", observed)
        assert state(item, item.auth)["material"]["state"] == "ready" and finishes == [True]
        marks.setattr(item.materials.store, "_scope", unavailable)
        response = state(item, item.auth)
        assert response["material"]["state"] == "unavailable" and "private" not in repr(response)
    (item.materials.store.root / "current.json").write_bytes(b"broken")
    response = state(item, item.auth)
    assert response["material"]["state"] == "corrupt" and response["material"]["version"] is None
    assert parsed(item, "GET", "/api/materials/query", auth=item.auth)[1]["code"] == "materials_corrupt"


def test_constructor_failure_is_local_and_never_retried_by_get(local_boards, monkeypatch):
    calls = []
    def denied(_context):
        calls.append(True)
        raise PermissionError("D:/private/customer/path")
    monkeypatch.setattr(api, "MaterialStore", denied)
    item = local_boards("failed-material-construction")
    auth = _bootstrap(item.port)
    for _ in range(2):
        body = state(item, auth)
        assert body["material"]["state"] == "unavailable" and "private" not in repr(body)
        assert parsed(item, "GET", "/api/me", auth=auth)[0] == 200
    assert calls == [True]


def test_single_flight_same_uuid_and_prior_terminal_during_next_active(boards, monkeypatch):
    item = boards(ready=True)
    entered, release, calls, result, errors = threading.Event(), threading.Event(), [], [], []
    def hold():
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        return {"state": "published", "version": item.initial_version}
    monkeypatch.setattr(item.materials.store, "refresh", hold)
    first, other = request_id(), request_id()
    def post():
        try:
            result.append(parsed(item, "POST", "/api/materials/refresh", {"request_id": first}, item.auth))
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=post)
    worker.start()
    try:
        assert entered.wait(2)
        duplicate = parsed(item, "POST", "/api/materials/refresh", {"request_id": first}, item.auth)
        assert duplicate[0] == 200 and duplicate[1]["operation"]["state"] == "running"
        different = parsed(item, "POST", "/api/materials/refresh", {"request_id": other}, item.auth)
        assert different[0] == 409 and different[1]["code"] == "materials_busy"
        assert different[1]["request_registered"] is False and len(calls) == 1
        assert state(item, item.auth, other)["lookup"] == "unknown"
    finally:
        release.set()
        worker.join(3)
        assert not worker.is_alive()
    assert not errors and result[0][1]["operation"]["state"] == "published"
    assert parsed(item, "POST", "/api/materials/refresh", {"request_id": first}, item.auth)[1]["operation"]["state"] == "published"
    assert len(calls) == 1
    entered.clear()
    release.clear()
    first, prior = other, first
    worker = threading.Thread(target=post)
    worker.start()
    try:
        assert entered.wait(2)
        lookup = state(item, item.auth, prior)
        assert lookup["active_id"] == first and lookup["operation"]["request_id"] == prior
        assert lookup["operation"]["state"] == "published"
    finally:
        release.set()
        worker.join(3)
        assert not worker.is_alive()
    assert not errors and len(calls) == 2


def test_retention_is_bounded_same_service_and_unknown_is_not_failure(boards, monkeypatch):
    item = boards(ready=True)
    calls = []
    def busy():
        calls.append(True)
        return {"state": "busy", "error": "material_busy"}
    monkeypatch.setattr(item.materials.store, "refresh", busy)
    ids = [request_id() for _ in range(33)]
    for rid in ids:
        assert parsed(item, "POST", "/api/materials/refresh", {"request_id": rid}, item.auth)[1]["operation"]["state"] == "busy"
    assert len(item.materials._terminal) == 32
    lookup = state(item, item.auth, ids[0])
    assert lookup["lookup"] == "unknown" and lookup["operation"] is None
    old_service = state(item, item.auth, ids[-1], request_id())
    assert old_service["lookup"] == "service_changed" and old_service["operation"] is None
    assert old_service["material"]["state"] == "ready"
    assert parsed(item, "POST", "/api/materials/refresh", {"request_id": ids[-1]}, item.auth)[1]["operation"]["state"] == "busy"
    assert len(calls) == 33 and lookup["retention"]["ttl_seconds"] is None


def test_real_cli_lock_busy_is_retained_without_starting_a_build(boards, monkeypatch):
    from scripts import material_facts
    item = boards(ready=True)
    monkeypatch.setattr(material_facts, "build_materials", lambda *_a, **_k: pytest.fail("CLI busy must not build"))
    lock = item.materials.store._acquire_refresh()
    assert lock is not None
    rid = request_id()
    try:
        response = parsed(item, "POST", "/api/materials/refresh", {"request_id": rid}, item.auth)
        assert response[0] == 200 and response[1]["operation"]["state"] == "busy"
        assert response[1]["operation"]["code"] == "material_busy"
        assert response[1]["material"]["version"] == item.initial_version
    finally:
        lock.close()
    assert parsed(item, "POST", "/api/materials/refresh", {"request_id": rid}, item.auth)[1]["operation"]["state"] == "busy"


def test_published_warning_failed_old_and_unconfirmed_current_are_separate(boards, monkeypatch):
    item = boards(ready=True)
    values = [
        {"state": "published", "version": item.initial_version, "warning": "material_gc_pending"},
        {"state": "failed", "version": item.initial_version, "error": "discovery_capacity"},
        {"state": "unconfirmed", "version": item.initial_version, "error": "material_publish_unconfirmed"},
        {"state": "failed", "error": "C:/do-not-leak/private"},
    ]
    for value in values:
        monkeypatch.setattr(item.materials.store, "refresh", lambda value=value: copy.deepcopy(value))
        response = parsed(item, "POST", "/api/materials/refresh", {"request_id": request_id()}, item.auth)[1]
        receipt = response["operation"]
        assert receipt["state"] == value["state"] and response["material"]["version"] == item.initial_version
        assert "do-not-leak" not in repr(response)
        if value["state"] == "published":
            assert receipt["warning"] == "material_gc_pending" and receipt["code"] is None
        if value["state"] == "unconfirmed":
            assert receipt["version"] is None
        if value.get("error") == "discovery_capacity":
            assert receipt["code"] == "discovery_capacity"


def test_disconnect_holds_real_inflight_drain_and_never_holds_manager_long_lock(boards, monkeypatch):
    item = boards(ready=True)
    drained = bind_drained(item)
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    original = item.materials.store.refresh
    def builder(*args, **kwargs):
        entered.set()
        assert release.wait(4)
        done.set()
        return empty_builder(*args, **kwargs)
    monkeypatch.setattr(item.materials.store, "refresh", lambda: original(builder=builder))
    rid = request_id()
    raw = json.dumps({"request_id": rid}).encode()
    connection = http.client.HTTPConnection("127.0.0.1", item.port, timeout=2)
    connection.request("POST", "/api/materials/refresh", raw, {**item.auth, "Content-Type": "application/json"})
    lock_seen = threading.Event()
    contender = None
    try:
        assert entered.wait(2) and item.manager.inflight == 1 and not drained()
        connection.close()  # Work belongs to the accepted server request.
        def lock_probe():
            with item.manager.lock:
                lock_seen.set()
        contender = threading.Thread(target=lock_probe)
        contender.start()
        assert lock_seen.wait(.5), "refresh held manager.lock over the build"
        with item.manager.lock:
            item.manager.stopping = True
        assert not drained() and not done.is_set()
        stopped = parsed(item, "POST", "/api/materials/refresh", {"request_id": request_id()}, item.auth)
        assert stopped[0] == 503 and stopped[1]["code"] == "materials_stopping"
        cached_post = parsed(item, "POST", "/api/materials/refresh", {"request_id": rid}, item.auth)
        assert cached_post[0] == 503 and cached_post[1]["code"] == "materials_stopping"
        before = time.monotonic()
        observed = state(item, item.auth, rid)
        assert time.monotonic() - before < .75
        assert observed["operation"]["state"] == "running" and observed["material"]["version"] == item.initial_version
        assert parsed(item, "GET", "/api/materials/groups", auth=item.auth)[0] == 200
        assert item.manager.inflight == 1 and not drained()
    finally:
        connection.close()
        release.set()
        if contender:
            contender.join(2)
            assert not contender.is_alive()
        wait_for(lambda: item.manager.inflight == 0, 3)
    assert done.is_set() and drained()
    final = state(item, item.auth, rid)
    assert final["operation"]["state"] == "published" and final["active_id"] is None


@pytest.mark.parametrize("window", ["refresh", "outcome"])
def test_baseexception_unwinds_terminal_before_releasing_original_mutation(boards, monkeypatch, window):
    item = boards(ready=True)
    drained = bind_drained(item)
    rid, observed = request_id(), []
    if window == "refresh":
        def interrupted():
            assert item.manager.inflight == 1 and not drained()
            raise KeyboardInterrupt("generated interrupt")
        monkeypatch.setattr(item.materials.store, "refresh", interrupted)
    else:
        monkeypatch.setattr(item.materials.store, "refresh", lambda: {"state": "published", "version": item.initial_version})
        def interrupted(_value):
            assert item.manager.inflight == 1 and not drained()
            raise SystemExit("generated helper-return interruption")
        monkeypatch.setattr(item.materials, "_outcome", interrupted)
    handler = SimpleNamespace(_reject_json=lambda *_a, **_k: pytest.fail("not stopping"))
    def run():
        try:
            item.server.RequestHandlerClass._mutation(handler, lambda: item.materials.post("", json.dumps({"request_id": rid}).encode()))
        except BaseException as error:
            observed.append(type(error))
    worker = threading.Thread(target=run)
    worker.start()
    worker.join(2)
    assert not worker.is_alive() and observed == [KeyboardInterrupt if window == "refresh" else SystemExit]
    assert item.manager.inflight == 0 and drained()
    receipt = state(item, item.auth, rid)
    assert receipt["active_id"] is None and receipt["operation"]["state"] == "unconfirmed"
    assert receipt["operation"]["version"] is None
