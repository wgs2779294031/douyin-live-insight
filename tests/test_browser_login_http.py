"""New login routes through the actual local HTTP/session/stop boundary."""
import json
from pathlib import Path
from types import ModuleType
import sys
import threading
import time

import pytest

from test_local_access import local_boards, _request, _bootstrap, _state_bytes


class Guide:
    def __init__(self, manager):
        self.manager = manager
        self.reads, self.calls = 0, []
        self.entered, self.release = threading.Event(), threading.Event()
        self.hold = False

    def snapshot(self):
        self.reads += 1
        return {"browser": {"state": "stopping" if self.manager.stopping else "not_prepared", "reason": ""},
            "channel": {"state": "not_checked", "reason": "", "target_tag": "", "checked_at": None, "source": ""},
            "busy": False, "action": "", "action_id": 0,
            "login_confirmed": False, "collection_recovered": False}

    def request(self, action, entry=None):
        self.calls.append((action, entry, self.manager.inflight))
        if self.hold:
            self.entered.set()
            assert self.release.wait(3)
        return {"ok": not self.manager.stopping, "status": self.snapshot()}


@pytest.fixture(autouse=True)
def no_sdk(monkeypatch):
    calls = []
    module = ModuleType("openai")
    def denied(*_args, **_kwargs):
        calls.append(1)
        raise AssertionError("model forbidden in login HTTP tests")
    module.OpenAI = denied
    monkeypatch.setitem(sys.modules, "openai", module)
    yield
    assert calls == []


def prepared(local_boards):
    item = local_boards()
    item.manager.login = Guide(item.manager)
    return item, item.manager.login, _bootstrap(item.port)


def test_status_is_read_only_and_all_new_actions_keep_local_permissions(local_boards):
    item, guide, auth = prepared(local_boards)
    before = _state_bytes(item)
    assert _request(item.port, "GET", "/api/browser-login")[0] == 401
    assert guide.reads == 0
    for _ in range(3):
        code, raw, _ = _request(item.port, "GET", "/api/browser-login", headers={"Cookie": auth["Cookie"]})
        assert code == 200 and json.loads(raw)["status"]["login_confirmed"] is False
    assert guide.reads == 3 and guide.calls == []
    for action in ("prepare", "open", "check"):
        for headers in ({}, {"Cookie": auth["Cookie"]},
            {**auth, "Origin": "https://third-party.invalid"},
            {**auth, "X-Local-Write-Token": "wrong"}):
            assert _request(item.port, "POST", "/api/browser-login", {"action": action}, headers)[0] in (401, 403)
            assert guide.calls == [] and item.manager.inflight == 0
    assert _state_bytes(item) == before
    assert _request(item.port, "POST", "/api/browser-login", {"action": "prepare"}, auth)[0] == 200
    assert guide.calls == [("prepare", None, 1)] and item.manager.inflight == 0
    assert _state_bytes(item) == before


@pytest.mark.parametrize("body", [None, [], {"action": "eval", "script": "private-payload"},
    {"action": "open", "url": "https://third-party.invalid/"},
    {"action": "check", "sec_uid": "private-id"}, {"action": "check", "tag": 123},
    {"action": "prepare", "extra": "ignored-is-not-allowed"}])
def test_login_route_is_not_an_arbitrary_url_or_script_proxy(local_boards, body):
    item, guide, auth = prepared(local_boards)
    before = _state_bytes(item)
    assert _request(item.port, "POST", "/api/browser-login", body, auth)[0] == 400
    assert guide.calls == [] and _state_bytes(item) == before and item.manager.inflight == 0


def test_check_uses_only_selected_roster_identity_without_enabling_any_role(local_boards):
    item, guide, auth = prepared(local_boards)
    tag, uid = "合成关闭目标", "MS4wLjABAAAA" + "x" * 50
    roster = {"version": 1, "anchors": [{"tag": tag, "name": "synthetic", "sec_uid": uid,
        "watch": False, "record": False, "llm": False, "enabled": False}]}
    Path(item.context["roster"]).write_text(json.dumps(roster), encoding="utf-8")
    before = _state_bytes(item)
    assert _request(item.port, "POST", "/api/browser-login", {"action": "check", "tag": "不存在"}, auth)[0] in (400, 404)
    assert guide.calls == []
    assert _request(item.port, "POST", "/api/browser-login", {"action": "check", "tag": tag}, auth)[0] == 200
    assert guide.calls == [("check", {"tag": tag, "sec_uid": uid}, 1)]
    assert _state_bytes(item) == before and item.manager.inflight == 0


def test_login_request_drains_with_application_stop_and_cannot_start_new_actions(local_boards):
    item, guide, auth = prepared(local_boards)
    guide.hold = True
    results = []
    worker = threading.Thread(target=lambda: results.append(_request(item.port, "POST", "/api/browser-login", {"action": "open"}, auth)))
    worker.start()
    try:
        assert guide.entered.wait(2) and item.manager.inflight == 1
        item.manager.stopping = True
        assert _request(item.port, "POST", "/api/browser-login", {"action": "prepare"}, auth)[0] == 503
        assert len(guide.calls) == 1 and item.manager.inflight == 1
        assert _request(item.port, "GET", "/api/browser-login", headers=auth)[0] == 200
    finally:
        guide.release.set()
        worker.join(3)
    assert not worker.is_alive() and results[0][0] == 200
    assert item.manager.inflight == 0 and len(guide.calls) == 1


def test_login_body_rejection_happens_before_any_guide_action(local_boards):
    item, guide, auth = prepared(local_boards)
    before = _state_bytes(item)
    assert _request(item.port, "POST", "/api/browser-login", {"padding": "x" * (70 * 1024)}, auth)[0] == 413
    # Rejections deliberately reply before bounded upload draining finishes.
    # The accepted handler remains in-flight until that transport work ends.
    assert guide.calls == []
    deadline = time.monotonic() + 3
    while item.manager.inflight and time.monotonic() < deadline:
        time.sleep(.01)
    assert guide.calls == [] and item.manager.inflight == 0 and _state_bytes(item) == before
