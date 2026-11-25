"""ASR routes through actual loopback permission and stop/inflight boundaries."""
import json
import threading
import time

import pytest

from test_local_access import local_boards, _bootstrap, _request, _state_bytes


class Lane:
    def __init__(self, manager):
        self.manager = manager
        self.reads, self.calls = 0, []
        self.entered, self.release = threading.Event(), threading.Event()
        self.hold = False
    def snapshot(self):
        self.reads += 1
        return {"state": "stopping" if self.manager.stopping else "idle", "resource_held": True,
                "verified": True, "stale": False, "counts": {"pending": 0}, "generation": "generated"}
    def retry(self):
        self.calls.append(self.manager.inflight)
        self.entered.set()
        if self.hold:
            assert self.release.wait(3)
        return {"ok": not self.manager.stopping, "status": self.snapshot()}


def prepared(local_boards):
    item = local_boards()
    item.manager.asr = Lane(item.manager)
    auth = _bootstrap(item.port)
    drained(item)
    return item, item.manager.asr, auth


def drained(item):
    until = time.monotonic() + 1
    while item.manager.inflight and time.monotonic() < until:
        time.sleep(.005)
    assert item.manager.inflight == 0


def test_asr_get_is_read_only_and_retry_keeps_local_authorization(local_boards):
    item, lane, auth = prepared(local_boards)
    before = _state_bytes(item)
    assert _request(item.port, "GET", "/api/asr")[0] == 401 and lane.reads == 0
    for _ in range(2):
        code, raw, _ = _request(item.port, "GET", "/api/asr", headers={"Cookie": auth["Cookie"]})
        assert code == 200 and json.loads(raw)["status"]["state"] == "idle"
    assert lane.reads == 2 and lane.calls == [] and _state_bytes(item) == before
    for headers in ({}, {"Cookie": auth["Cookie"]},
                    dict(auth, Origin="https://external.invalid"), dict(auth, Host="external.invalid")):
        assert _request(item.port, "POST", "/api/asr", {"action": "retry"}, headers)[0] in (401, 403)
        assert lane.calls == []
    assert _request(item.port, "POST", "/api/asr", {"action": "retry"}, auth)[0] == 200
    drained(item)
    assert lane.calls == [1] and item.manager.inflight == 0 and _state_bytes(item) == before


@pytest.mark.parametrize("body", [{}, {"action": "force"}, {"action": "pause"},
    {"action": "retry", "path": "outside"}, {"action": "retry", "model": "outside"}, []])
def test_asr_retry_accepts_no_paths_model_or_other_actions(local_boards, body):
    item, lane, auth = prepared(local_boards)
    before = _state_bytes(item)
    assert _request(item.port, "POST", "/api/asr", body, auth)[0] == 400
    drained(item)
    assert lane.calls == [] and item.manager.inflight == 0 and _state_bytes(item) == before


def test_asr_write_inflight_and_stop_keep_get_fast_without_replay(local_boards):
    item, lane, auth = prepared(local_boards)
    lane.hold = True
    results = []
    worker = threading.Thread(target=lambda: results.append(_request(item.port, "POST", "/api/asr", {"action": "retry"}, auth)))
    worker.start()
    try:
        assert lane.entered.wait(1) and item.manager.inflight == 1
        item.manager.stopping = True
        assert _request(item.port, "POST", "/api/asr", {"action": "retry"}, auth)[0] == 503
        started = time.monotonic()
        code, raw, _ = _request(item.port, "GET", "/api/asr", headers=auth)
        assert code == 200 and json.loads(raw)["status"]["state"] == "stopping"
        assert time.monotonic() - started < .5 and lane.calls == [1]
    finally:
        lane.release.set()
        worker.join(2)
        assert not worker.is_alive()
    until = time.monotonic() + 1
    while item.manager.inflight and time.monotonic() < until:
        time.sleep(.005)
    assert item.manager.inflight == 0 and lane.calls == [1] and results[0][0] == 200


def test_asr_body_limit_does_not_dispatch(local_boards):
    item, lane, auth = prepared(local_boards)
    assert _request(item.port, "POST", "/api/asr", {"action": "retry", "extra": "x" * 70000}, auth)[0] == 413
    until = time.monotonic() + 1
    while item.manager.inflight and time.monotonic() < until:
        time.sleep(.005)
    assert lane.calls == [] and item.manager.inflight == 0
