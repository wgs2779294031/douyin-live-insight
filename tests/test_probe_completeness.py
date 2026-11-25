"""Synthetic live responses with incomplete room fields never confirm offline."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import stream_recorder as recorder
import watch_anchor_live as watch


def live():
    return {"ts": 100, "live_status": 1, "room_id_str": "same-session",
            "nick": "synthetic", "web_rid": "synthetic-room", "room_status": 2,
            "is_live": True}


@pytest.mark.parametrize("room_data", [None, "{broken", {}, {"owner": {}},
                                      {"owner": {"web_rid": ""}, "status": 2}])
def test_live_profile_missing_room_details_is_unknown_not_offline(monkeypatch, room_data):
    payload = {"ok": True, "live_status": 1, "room_id_str": "same-session",
               "nick": "synthetic", "room_data": room_data}
    monkeypatch.setattr(watch, "cdp_eval", lambda *_args: json.dumps(payload))
    row = watch.poll_once("synthetic-node", "synthetic-cdp", "synthetic", None)
    assert row["is_live"] is False
    assert watch.classify_env_failure(row) == "channel"
    assert watch.is_confirmed_offline(row) is False


@pytest.mark.parametrize("row", [
    {"nick": "synthetic", "room_id_str": "same-session", "is_live": False},
    {"nick": "synthetic", "live_status": 1, "is_live": False},
    {"nick": "synthetic", "live_status": 0, "room_data_parse_error": "synthetic", "is_live": False},
])
def test_missing_or_unparseable_live_status_is_never_confirmed_offline(row):
    assert watch.is_confirmed_offline(row) is False


def test_explicit_normal_offline_remains_confirmed():
    row = {"nick": "synthetic", "live_status": 0, "room_id_str": "0", "is_live": False}
    assert watch.classify_env_failure(row) is None
    assert watch.is_confirmed_offline(row) is True


def test_incomplete_live_response_does_not_mark_offline_or_refill_quota(tmp_path):
    quota = recorder.SessionQuota(tmp_path / "quota.json", 100)
    assert quota.observe(live())
    quota.finish(100, "quota")
    incomplete = dict(live(), web_rid=None, is_live=False)
    assert not quota.observe(incomplete)
    assert quota.state["offline_seen"] is False
    assert not quota.observe(live())
    assert quota.state["bytes_used"] == 100


def test_pull_loop_keeps_stream_on_incomplete_live_status(monkeypatch):
    quit_flag = [False]
    calls = []
    class Response:
        status_code = 200
        def iter_content(self, _size):
            yield b"synthetic"
            yield b"synthetic-next"
        def close(self):
            pass
    class Session:
        def get(self, *_args, **_kwargs):
            calls.append("get")
            return Response()
    class Writer:
        seg_seconds, quota_bytes = 60, 1000
        def feed(self, _chunk):
            quit_flag[0] = True
    monkeypatch.setattr(recorder, "pick_stream_url", lambda *_args: ("synthetic-url", "ld"))
    monkeypatch.setattr(recorder, "build_session", lambda *_args: Session())
    monkeypatch.setattr(recorder, "read_watch_state", lambda *_args: dict(live(), web_rid=None, is_live=False))
    monkeypatch.setattr(recorder, "now", lambda: 100.0)
    monkeypatch.setattr(recorder, "url_expire_ts", lambda *_args: 100000.0)
    monkeypatch.setattr(recorder, "capture_stream", lambda *_args: pytest.fail("unexpected recapture"))
    result = recorder.pull_loop("synthetic", "synthetic-room", Writer(), lambda *_args: None,
                                30, 30, 0, "synthetic-node", {}, quit_flag)
    assert result == "signal" and calls == ["get"]


def test_watch_does_not_stop_after_two_incomplete_live_responses(tmp_path, monkeypatch):
    rows = iter([live()] + [dict(live(), web_rid=None, is_live=False)] * 3)
    calls, notices = [], []
    class Process:
        pid, returncode = 123456, None
        def poll(self):
            return None
        def terminate(self):
            pass
        def wait(self, **_kwargs):
            return 0
    def poll(*_args):
        calls.append("poll")
        return next(rows)
    monkeypatch.setattr(watch, "poll_once", poll)
    monkeypatch.setattr(watch, "_append", lambda *_args: None)
    monkeypatch.setattr(watch, "make_config", lambda *_args: tmp_path / "synthetic.json")
    monkeypatch.setattr(watch, "start_collector", lambda *_args: Process())
    monkeypatch.setattr(watch, "rebuild_dashboard", lambda: None)
    monkeypatch.setattr(watch, "heal_channel", lambda *_args: None)
    monkeypatch.setattr(watch.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(watch.notify, "send_text", lambda message, **_kwargs: notices.append(message))
    monkeypatch.setattr(sys, "argv", ["watch", "--sec-uid", "synthetic", "--tag", "synthetic",
                                      "--start", "--node", "synthetic-node", "--cdp", "synthetic-cdp",
                                      "--max-rounds", "4", "--interval", "0"])
    assert watch.main() == 0
    assert len(calls) == 4
    assert not any("已下播" in notice for notice in notices)
