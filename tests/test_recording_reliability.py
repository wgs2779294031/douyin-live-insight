"""录音配额、在播健康与告警回执；仅合成文件/模拟网络和进程。"""
import json
import os
from pathlib import Path
import sys
import time
from types import ModuleType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import anchor_manager as am
import health_probe as hp
import stream_recorder as sr


@pytest.fixture(autouse=True)
def file_birth_times(tmp_path, monkeypatch):
    """Synthetic FLV fixtures use their simulated clock, not today's file birth."""
    original_stat = Path.stat
    births = {}

    def stat(path, *args, **kwargs):
        info = original_stat(path, *args, **kwargs)
        if path.suffix == ".flv" and path.is_relative_to(tmp_path):
            fields = {key: getattr(info, key) for key in dir(info) if key.startswith("st_")}
            fields["st_birthtime"] = births.get(path, info.st_mtime)
            return SimpleNamespace(**fields)
        return info

    monkeypatch.setattr(Path, "stat", stat)
    return births


def live(sid="session-A", **extra):
    return {"is_live": True, "live_status": 1, "nick": "synthetic",
            "web_rid": "synthetic-room", "room_id_str": sid, **extra}


def offline():
    return {"is_live": False, "live_status": 0, "nick": "synthetic"}


def test_quota_survives_restart_and_same_broadcast(tmp_path):
    path = tmp_path / "quota.json"
    q = sr.SessionQuota(path, 100)
    assert q.observe(live())
    q.finish(100, "quota")
    resumed = sr.SessionQuota(path, 100)
    assert not resumed.observe(live())
    assert not resumed.observe({"is_live": False, "error": "synthetic CDP error"})
    assert not resumed.observe(live())
    assert resumed.state["bytes_used"] == 100
    assert not resumed.state["offline_seen"]


def test_quota_resets_after_confirmed_offline_then_live(tmp_path):
    q = sr.SessionQuota(tmp_path / "quota.json", 100)
    q.observe(live())
    q.finish(100, "quota")
    assert not q.observe(offline())
    assert q.state["exhausted"]
    assert q.observe(live())
    assert q.remaining == 100


def test_quota_resets_for_explicit_new_session_id(tmp_path):
    q = sr.SessionQuota(tmp_path / "quota.json", 100)
    q.observe(live())
    q.finish(100, "quota")
    assert q.observe(live("session-B"))
    assert q.remaining == 100


def test_learning_missing_session_id_does_not_refill_quota(tmp_path):
    q = sr.SessionQuota(tmp_path / "quota.json", 100)
    q.observe(live(None))
    q.finish(100, "quota")
    assert not q.observe(live())
    assert q.state["session_id"] == "session-A"


def test_retry_preserves_consumed_bytes(tmp_path):
    path = tmp_path / "quota.json"
    q = sr.SessionQuota(path, 100)
    q.observe(live())
    q.finish(40, "exception:ConnectionError")
    q = sr.SessionQuota(path, 100)
    assert q.observe(live())
    assert q.remaining == 60
    q.finish(60, "quota")
    assert not q.observe(live())


def test_corrupt_quota_state_does_not_silently_reset(tmp_path):
    path = tmp_path / "quota.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(RuntimeError, match="拒绝重置配额"):
        sr.SessionQuota(path, 100)


def test_quota_save_failure_is_propagated(tmp_path, monkeypatch):
    q = sr.SessionQuota(tmp_path / "quota.json", 100)
    q.observe(live())

    def fail_replace(*args):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(sr.os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic replace failure"):
        q.finish(100, "quota")
    assert list(tmp_path.glob("*.tmp")) == []


def test_main_does_not_create_new_writer_after_quota_in_same_live(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "DATA", tmp_path)
    monkeypatch.setattr(sr.sys, "argv", ["recorder", "--tag", "synthetic", "--quota-mb", "1"])
    monkeypatch.setattr(sr.signal, "signal", lambda *args: None)
    monkeypatch.setattr(sr, "Log", lambda path: lambda message: None)
    monkeypatch.setattr(sr, "resolve_node", lambda: "synthetic-node")
    monkeypatch.setitem(sys.modules, "curl_cffi", ModuleType("curl_cffi"))
    monkeypatch.setattr(sr.time, "sleep", lambda seconds: None)
    rows = iter([live(), live(), {"is_live": False, "error": "synthetic"},
                 live(), offline(), live("session-B")])
    seen = []

    def state(tag):
        value = next(rows)
        seen.append(value)
        return value

    monkeypatch.setattr(sr, "read_watch_state", state)
    captures = []
    monkeypatch.setattr(sr, "capture_stream",
                        lambda *args: captures.append(len(seen)) or {"synthetic": True})

    class Writer:
        def __init__(self, out, seg, quota, keep, log):
            self.total_bytes, self.quota_bytes, self.segments = 0, quota, []

        def close(self):
            pass

    monkeypatch.setattr(sr, "FlvSegWriter", Writer)
    pulls = []

    def pull(tag, rid, writer, *args):
        pulls.append(writer)
        writer.total_bytes = writer.quota_bytes if len(pulls) == 1 else 1
        return "quota" if len(pulls) == 1 else "signal"

    monkeypatch.setattr(sr, "pull_loop", pull)
    assert sr.main() == 0
    assert captures == [1, 6]
    assert len(pulls) == 2
    assert not (tmp_path / "stream_synthetic.pid").exists()


def _recording_setup(tmp_path, monkeypatch, *, running=True, is_live=True, record=True):
    stamp = 1793800000.0
    monkeypatch.setattr(am, "DATA", tmp_path)
    monkeypatch.setattr(am.time, "time", lambda: stamp)
    monkeypatch.setattr(am, "instance_status", lambda kind, tag:
                        {"pid": 123, "running": True if kind == "watch" else running})
    row = live(ts=stamp) if is_live else {**offline(), "ts": stamp}
    watch_path = tmp_path / "watch_X.jsonl"
    watch_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    os.utime(watch_path, (stamp, stamp))
    return stamp, {"tag": "X", "watch": True, "record": record, "enabled": True}


def _recorder_state(tmp_path, stamp, *, since=None, exhausted=False, sid="session-A"):
    value = {"session_id": sid, "web_rid": "synthetic-room", "exhausted": exhausted,
             "phase": "quota" if exhausted else "starting", "offline_seen": False,
             "live_since": stamp - 1000 if since is None else since}
    (tmp_path / "stream_X.state.json").write_text(json.dumps(value), encoding="utf-8")


def _audio_tag(payload, timestamp=0, kind=8):
    header = (bytes([kind]) + len(payload).to_bytes(3, "big")
              + (timestamp & 0xffffff).to_bytes(3, "big") + bytes([timestamp >> 24])
              + bytes(3))
    return header + payload + (11 + len(payload)).to_bytes(4, "big")


def _silent_flv():
    # AAC-LC 44.1 kHz stereo setup followed by an encoded silence packet.
    return (sr.FLV_HEADER + _audio_tag(b"\xaf\x00\x12\x10")
            + _audio_tag(b"\xaf\x01\x21\x10\x04\x60\x8c\x1c", 23))


def _fragment(tmp_path, stamp, payload, number=1):
    day = time.strftime("%Y-%m-%d", time.localtime(stamp))
    directory = tmp_path / ("audio_X_%s" % day)
    directory.mkdir(exist_ok=True)
    path = directory / ("seg_%04d.flv" % number)
    path.write_bytes(payload)
    os.utime(path, (stamp, stamp))
    return path


def _progress(tmp_path, stamp, monkeypatch, **changes):
    created = stamp - 1000
    monkeypatch.setattr(am, "process_create_time", lambda pid: created)
    row = {"schema": 1, "pid": 123, "process_created_at": created, "updated_at": stamp,
           "session_id": "session-A", "web_rid": "synthetic-room", "phase": "recording",
           "last_valid_audio_at": stamp - 5, "valid_audio_packets": 100,
           "valid_audio_bytes": 1000, "parser_error": None}
    row.update(changes)
    (tmp_path / "stream_X.progress.json").write_text(json.dumps(row), encoding="utf-8")


def test_recording_exit_is_part_of_anchor_health(tmp_path, monkeypatch):
    stamp, entry = _recording_setup(tmp_path, monkeypatch, running=False)
    st = am.anchor_status(entry)
    assert st["state"] == "stopped"
    assert st["record_state"] == "stopped"
    assert am._health_of([st])["problems"][0]["record_state"] == "stopped"


@pytest.mark.parametrize("is_live,record", [(False, True), (True, False)])
def test_recording_not_required_offline_or_unconfigured(tmp_path, monkeypatch, is_live, record):
    _, entry = _recording_setup(tmp_path, monkeypatch, running=False,
                               is_live=is_live, record=record)
    assert am.anchor_status(entry)["state"] == "ok"


def test_probe_error_with_false_live_flag_is_unknown(tmp_path, monkeypatch):
    stamp, entry = _recording_setup(tmp_path, monkeypatch, running=False)
    (tmp_path / "watch_X.jsonl").write_text(
        json.dumps({"ts": stamp, "is_live": False, "error": "synthetic CDP failure"}) + "\n",
        encoding="utf-8")
    assert am.recording_health(entry)["state"] == "unknown"


def test_incomplete_live_response_is_unknown_for_recording_health(tmp_path, monkeypatch):
    stamp, entry = _recording_setup(tmp_path, monkeypatch, running=False)
    row = {"ts": stamp, "is_live": False, "live_status": 1, "nick": "synthetic",
           "web_rid": ""}
    (tmp_path / "watch_X.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert am.recording_health(entry)["state"] == "unknown"


def test_record_only_missing_watcher_does_not_hide_exited_recorder(tmp_path, monkeypatch):
    monkeypatch.setattr(am, "DATA", tmp_path)
    monkeypatch.setattr(am, "instance_status", lambda kind, tag:
                        {"pid": 0, "running": False})
    entry = {"tag": "X", "enabled": True, "watch": False, "record": True}
    st = am.anchor_status(entry)
    assert st["state"] == st["record_state"] == "stopped"
    assert am._health_of([st])["stopped"] == 1


def test_quota_pause_is_normal_but_new_session_requires_recorder(tmp_path, monkeypatch):
    stamp, entry = _recording_setup(tmp_path, monkeypatch, running=False)
    _recorder_state(tmp_path, stamp, exhausted=True)
    assert am.anchor_status(entry)["record_state"] == "quota"
    assert am.anchor_status(entry)["state"] == "ok"
    (tmp_path / "watch_X.jsonl").write_text(json.dumps(live("session-B", ts=stamp)) + "\n",
                                           encoding="utf-8")
    os.utime(tmp_path / "watch_X.jsonl", (stamp, stamp))
    assert am.anchor_status(entry)["record_state"] == "stopped"


def test_new_recording_gets_grace_then_missing_segments_are_stalled(tmp_path, monkeypatch):
    stamp, entry = _recording_setup(tmp_path, monkeypatch)
    _recorder_state(tmp_path, stamp, since=stamp - 10)
    assert am.anchor_status(entry)["record_state"] == "starting"
    _recorder_state(tmp_path, stamp, since=stamp - am.RECORD_START_GRACE_SEC - 1)
    assert am.anchor_status(entry)["state"] == "stalled"


def test_old_recording_segment_is_stalled(tmp_path, monkeypatch):
    stamp, entry = _recording_setup(tmp_path, monkeypatch)
    _recorder_state(tmp_path, stamp)
    day = time.strftime("%Y-%m-%d", time.localtime(stamp))
    directory = tmp_path / ("audio_X_%s" % day)
    directory.mkdir()
    segment = directory / "seg_0001.flv"
    segment.write_bytes(_silent_flv())
    old = stamp - am.RECORD_STALE_SEC - 1
    os.utime(segment, (old, old))
    assert am.anchor_status(entry)["record_state"] == "stalled"


def test_fresh_previous_day_segment_remains_healthy(tmp_path, monkeypatch):
    stamp, entry = _recording_setup(tmp_path, monkeypatch)
    _recorder_state(tmp_path, stamp)
    day = time.strftime("%Y-%m-%d", time.localtime(stamp - 86400))
    directory = tmp_path / ("audio_X_%s" % day)
    directory.mkdir()
    segment = directory / "seg_0001.flv"
    segment.write_bytes(_silent_flv())
    os.utime(segment, (stamp - 10, stamp - 10))
    assert am.anchor_status(entry)["record_state"] == "ok"


def test_new_broadcast_uses_first_session_observation_for_starting_grace(tmp_path, monkeypatch):
    stamp, entry = _recording_setup(tmp_path, monkeypatch)
    _recorder_state(tmp_path, stamp, sid="old-session")
    pidfile = am.pidfile("record", "X")
    pidfile.write_text("123", encoding="ascii")
    os.utime(pidfile, (stamp - 1000, stamp - 1000))
    rows = [live("old-session", ts=stamp - 500), live("new-session", ts=stamp - 10)]
    watch = tmp_path / "watch_X.jsonl"
    watch.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    assert am.recording_health(entry, now=stamp)["state"] == "starting"
    with watch.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(live("new-session", ts=stamp + 200)) + "\n")
    assert am.recording_health(entry, now=stamp + 200)["state"] == "stalled"


def test_truncated_high_frequency_window_does_not_refresh_starting_grace(tmp_path, monkeypatch):
    stamp, entry = _recording_setup(tmp_path, monkeypatch)
    _recorder_state(tmp_path, stamp, sid="old-session")
    row = live("new-session", ts=stamp - 1, padding="x" * 1000)
    watch = tmp_path / "watch_X.jsonl"
    watch.write_text((json.dumps(row) + "\n") * 100, encoding="utf-8")
    assert am.recording_health(entry, now=stamp)["state"] == "unknown"


def _probe_setup(tmp_path, monkeypatch):
    clock = [10000.0]
    monkeypatch.setattr(hp, "DATA", tmp_path)
    monkeypatch.setattr(hp, "STATE_PATH", tmp_path / "health-state.json")
    monkeypatch.setattr(hp.time, "time", lambda: clock[0])
    monkeypatch.setattr(hp, "_beat", lambda text: None)
    monkeypatch.setattr(hp.roster_mod, "ensure", lambda: ({"anchors": []}, False))
    monkeypatch.setattr(hp, "probe", lambda *args: [{"tag": "X", "age": 1000}])
    monkeypatch.setattr(hp, "probe_silent", lambda *args: [])
    monkeypatch.setattr(hp, "probe_recording", lambda *args: [])
    hp._save_state({"last_heal": clock[0]})

    def forbidden_heal(*args, **kwargs):
        raise AssertionError("test must not self-heal real services")

    monkeypatch.setattr(hp.subprocess, "run", forbidden_heal)
    return clock


def test_failed_alert_retries_without_success_cooldown(tmp_path, monkeypatch, capsys):
    clock = _probe_setup(tmp_path, monkeypatch)
    deliveries = iter([False, True])
    calls = []

    def send(text, **kwargs):
        calls.append(kwargs)
        return next(deliveries)

    monkeypatch.setattr(hp.notify, "send_text", send)
    assert hp.main() == 1
    state = hp._load_state()
    assert state["alerted"] == {}
    assert state["alert_failed"] == {"X": clock[0]}
    assert state["last_delivery"]["status"] == "failed"
    assert "已推送" not in capsys.readouterr().out
    clock[0] += hp.ALERT_RETRY_SEC - 1
    hp.main()
    assert len(calls) == 1
    clock[0] += 1
    hp.main()
    assert len(calls) == 2
    state = hp._load_state()
    assert state["alerted"] == {"X": clock[0]}
    assert state["alert_failed"] == {}
    assert state["last_delivery"]["status"] == "sent"
    assert all(c["cooldown"] == 0 for c in calls)
    clock[0] += hp.ALERT_RETRY_SEC
    hp.main()
    assert len(calls) == 2  # a successful send still earns its full cooldown


def test_alert_exception_is_recorded_as_failure(tmp_path, monkeypatch):
    clock = _probe_setup(tmp_path, monkeypatch)

    def fail_send(*args, **kwargs):
        raise RuntimeError("synthetic send failure")

    monkeypatch.setattr(hp.notify, "send_text", fail_send)
    assert hp.main() == 1
    assert hp._load_state()["last_delivery"]["status"] == "failed"


def test_recovery_clears_failed_delivery_state(tmp_path, monkeypatch):
    _probe_setup(tmp_path, monkeypatch)
    hp._save_state({"alert_failed": {"X": 10000}, "last_delivery": {"status": "failed"}})
    monkeypatch.setattr(hp, "probe", lambda *args: [])
    assert hp.main() == 0
    assert hp._load_state() == {}


def test_recording_probe_reports_only_actual_recording_failures(monkeypatch):
    statuses = {"A": "stopped", "B": "stalled", "C": "quota", "D": "idle"}
    monkeypatch.setattr(hp.mgr, "recording_health", lambda entry, now:
                        {"state": statuses[entry["tag"]], "age_sec": 500})
    got = hp.probe_recording({"anchors": [{"tag": tag} for tag in statuses]}, now=10000)
    assert [(x["tag"], x["kind"]) for x in got] == [("A", "录音退出"), ("B", "录音停写")]

def test_growing_invalid_fragment_never_renews_recording_health(tmp_path, monkeypatch):
    stamp, entry = _recording_setup(tmp_path, monkeypatch)
    _recorder_state(tmp_path, stamp)
    path = _fragment(tmp_path, stamp, _silent_flv() + b"broken")
    for extra in (b"more bad bytes", b"yet more bad bytes"):
        with path.open("ab") as fh:
            fh.write(extra)
        os.utime(path, (stamp, stamp))
        status = am.recording_health(entry, now=stamp)
        assert status["state"] == "stalled" and status["reason"] == "invalid_audio"


def test_encoded_silence_is_valid_audio_progress(tmp_path, monkeypatch):
    stamp, entry = _recording_setup(tmp_path, monkeypatch)
    _recorder_state(tmp_path, stamp)
    _fragment(tmp_path, stamp - 1, _silent_flv())
    assert am.recording_health(entry, now=stamp)["state"] == "ok"


@pytest.mark.parametrize("payload", [
    sr.FLV_HEADER + _audio_tag(b"\xaf\x01\x21\x10"),  # Missing decoder configuration.
    _silent_flv() + _audio_tag(b"\xaf\x02bad"),        # Invalid AAC packet kind.
    _silent_flv()[:-1] + b"\xff",                     # Wrong PreviousTagSize.
    sr.FLV_HEADER + _audio_tag(b"\xaf\x00\x00\x00") + _audio_tag(b"\xaf\x01abc"),
])
def test_invalid_aac_envelope_is_rejected(tmp_path, payload):
    path = tmp_path / "fragment.flv"
    path.write_bytes(payload)
    assert am._flv_has_recent_aac(path) is False


def test_bad_initialization_after_valid_aac_setup_cannot_hide_behind_valid_tail(tmp_path, monkeypatch):
    stamp, entry = _recording_setup(tmp_path, monkeypatch)
    _recorder_state(tmp_path, stamp)
    sequence = _audio_tag(b"\xaf\x00\x12\x10")
    broken_script = _audio_tag(b"x" * 65536, kind=18)[:-4] + bytes(4)
    tail = b"".join(_audio_tag(b"\xaf\x01\x21\x10\x04\x60\x8c\x1c", n * 23)
                    for n in range(40))
    path = _fragment(tmp_path, stamp, sr.FLV_HEADER + sequence + broken_script + tail)
    assert am._flv_has_recent_aac(path) is False
    status = am.recording_health(entry, now=stamp)
    assert status["state"] == "stalled" and status["reason"] == "invalid_audio"


def test_partial_open_fragment_uses_recent_closed_audio(tmp_path, monkeypatch):
    stamp, entry = _recording_setup(tmp_path, monkeypatch)
    _recorder_state(tmp_path, stamp)
    _fragment(tmp_path, stamp - 61, _silent_flv(), 1)
    _fragment(tmp_path, stamp, _silent_flv()[:-5], 2)
    status = am.recording_health(entry, now=stamp)
    assert status["state"] == "ok" and status["age_sec"] == 61


def test_legacy_check_skips_payload_and_bounds_reads(tmp_path, monkeypatch):
    path = tmp_path / "fragment.flv"
    raw = _audio_tag(b"\xaf\x01" + b"\0" * 100000, 23)
    path.write_bytes(sr.FLV_HEADER + _audio_tag(b"\xaf\x00\x12\x10") + raw * 20)
    original_open = Path.open
    consumed = []

    class ObservedFile:
        def __init__(self, fh):
            self.fh = fh
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.fh.close()
        def __getattr__(self, name):
            return getattr(self.fh, name)
        def read(self, count=-1):
            assert count >= 0
            value = self.fh.read(count)
            consumed.append(len(value))
            return value

    monkeypatch.setattr(Path, "open", lambda path, *a, **kw: ObservedFile(original_open(path, *a, **kw)))
    assert am._flv_has_recent_aac(path)
    assert sum(consumed) < 2000


def test_current_worker_valid_audio_receipt_is_authoritative(tmp_path, monkeypatch):
    stamp, entry = _recording_setup(tmp_path, monkeypatch)
    _recorder_state(tmp_path, stamp)
    _progress(tmp_path, stamp, monkeypatch)
    monkeypatch.setattr(am, "_recent_recording_fragments", lambda *_: pytest.fail("receipt avoids audio reads"))
    assert am.recording_health(entry, now=stamp) == {"state": "ok", "age_sec": 5, "reason": "audio_progress"}


def test_network_and_file_growth_cannot_mask_stale_audio_receipt(tmp_path, monkeypatch):
    stamp, entry = _recording_setup(tmp_path, monkeypatch)
    _recorder_state(tmp_path, stamp)
    _fragment(tmp_path, stamp, _silent_flv())
    _progress(tmp_path, stamp, monkeypatch, last_valid_audio_at=stamp-300, parser_error="invalid_tag")
    status = am.recording_health(entry, now=stamp)
    assert status == {"state": "stalled", "age_sec": 300, "reason": "invalid_audio"}


@pytest.mark.parametrize("changes", [
    {"pid": 456}, {"process_created_at": 1}, {"session_id": "other"},
    {"web_rid": "other"}, {"updated_at": float("inf")},
    {"last_valid_audio_at": float("nan")}, {"valid_audio_packets": 0},
])
def test_invalid_receipt_cannot_mask_missing_valid_audio(tmp_path, monkeypatch, changes):
    stamp, entry = _recording_setup(tmp_path, monkeypatch)
    _recorder_state(tmp_path, stamp)
    _progress(tmp_path, stamp, monkeypatch, **changes)
    assert am.recording_health(entry, now=stamp)["state"] == "stalled"


def test_new_broadcast_has_grace_but_no_audio_eventually_stalls(tmp_path, monkeypatch):
    stamp, entry = _recording_setup(tmp_path, monkeypatch)
    _recorder_state(tmp_path, stamp, since=stamp-20)
    _progress(tmp_path, stamp, monkeypatch, last_valid_audio_at=None, valid_audio_packets=0,
              process_created_at=stamp-20)
    monkeypatch.setattr(am, "process_create_time", lambda pid: stamp-20)
    assert am.recording_health(entry, now=stamp)["state"] == "starting"
    # The observer's clock advances, and retrying cannot reset the broadcast start.
    watch = tmp_path / "watch_X.jsonl"
    watch.write_text(json.dumps(live(ts=stamp+200))+"\n", encoding="utf-8")
    assert am.recording_health(entry, now=stamp+200)["state"] == "stalled"


def test_startup_receipt_with_no_room_does_not_restart_broadcast_grace(tmp_path, monkeypatch):
    stamp, entry = _recording_setup(tmp_path, monkeypatch)
    _recorder_state(tmp_path, stamp)
    _progress(tmp_path, stamp, monkeypatch, phase="starting", session_id="", web_rid="",
              last_valid_audio_at=None, valid_audio_packets=0, process_created_at=stamp-20)
    monkeypatch.setattr(am, "process_create_time", lambda pid: stamp-20)
    status = am.recording_health(entry, now=stamp)
    assert status["state"] == "stalled" and status["age_sec"] == 1000
    assert am.recording_health(entry, now=stamp+200)["state"] == "stalled"


def _starting_progress(tmp_path, stamp, monkeypatch, *, created=None, blank=False, pid=123):
    created = stamp - 5 if created is None else created
    _progress(tmp_path, stamp, monkeypatch, pid=pid, phase="starting",
              session_id="" if blank else "session-A",
              web_rid="" if blank else "synthetic-room",
              last_valid_audio_at=None, valid_audio_packets=0, process_created_at=created)
    monkeypatch.setattr(am, "process_create_time", lambda _: created)


@pytest.mark.parametrize("blank", [False, True])
def test_same_broadcast_repeated_worker_replacement_cannot_clear_failure(tmp_path, monkeypatch, blank):
    stamp, entry = _recording_setup(tmp_path, monkeypatch)
    _recorder_state(tmp_path, stamp)
    for pid, elapsed in ((123, 0), (456, 180), (789, 360)):
        now = stamp + elapsed
        (tmp_path / "watch_X.jsonl").write_text(json.dumps(live(ts=now))+"\n", encoding="utf-8")
        _starting_progress(tmp_path, now, monkeypatch, blank=blank, pid=pid)
        monkeypatch.setattr(am, "instance_status", lambda *_: {"pid": pid, "running": True})
        status = am.recording_health(entry, now=now)
        assert status["state"] == "stalled" and status["age_sec"] == 1000 + elapsed
        faults = hp.probe_recording({"anchors": [entry]}, now=now)
        assert len(faults) == 1 and faults[0]["kind"] == "录音停写"


def test_blank_startup_receipt_preserves_genuine_new_broadcast_grace(tmp_path, monkeypatch):
    stamp, entry = _recording_setup(tmp_path, monkeypatch)
    _recorder_state(tmp_path, stamp, since=stamp-10)
    _starting_progress(tmp_path, stamp, monkeypatch, blank=True)
    assert am.recording_health(entry, now=stamp)["state"] == "starting"
    assert am.recording_health(entry, now=stamp+171)["state"] == "stalled"


@pytest.mark.parametrize("blank", [False, True])
def test_restart_uses_recent_closed_same_broadcast_audio_without_extending_it(tmp_path, monkeypatch, blank):
    stamp, entry = _recording_setup(tmp_path, monkeypatch)
    _recorder_state(tmp_path, stamp)
    _fragment(tmp_path, stamp-60, _silent_flv())
    _starting_progress(tmp_path, stamp, monkeypatch, blank=blank)
    status = am.recording_health(entry, now=stamp)
    assert status["state"] == "ok" and status["age_sec"] == 60
    status = am.recording_health(entry, now=stamp+121)
    assert status["state"] == "stalled" and status["age_sec"] == 181


def test_restarted_worker_cannot_borrow_audio_from_before_this_broadcast(tmp_path, monkeypatch):
    stamp, entry = _recording_setup(tmp_path, monkeypatch)
    _recorder_state(tmp_path, stamp, since=stamp-10)
    _fragment(tmp_path, stamp-20, _silent_flv())
    _starting_progress(tmp_path, stamp, monkeypatch)
    assert am.recording_health(entry, now=stamp)["state"] == "starting"
    status = am.recording_health(entry, now=stamp+171)
    assert status["state"] == "stalled" and status["age_sec"] == 181


@pytest.mark.parametrize("receipt", [False, True])
def test_old_broadcast_file_flushed_after_new_start_is_not_recovery(
        tmp_path, monkeypatch, file_birth_times, receipt):
    stamp, entry = _recording_setup(tmp_path, monkeypatch)
    _recorder_state(tmp_path, stamp, since=stamp-300)
    path = _fragment(tmp_path, stamp-10, _silent_flv())
    file_birth_times[path] = stamp-500
    if receipt:
        _starting_progress(tmp_path, stamp, monkeypatch)
    status = am.recording_health(entry, now=stamp)
    assert status["state"] == "stalled" and status["age_sec"] == 300


@pytest.mark.parametrize("scope", [{"session_id": "old-session"}, {"web_rid": "old-room"}])
def test_identified_old_scope_worker_cannot_claim_new_scope_through_files(tmp_path, monkeypatch, scope):
    stamp, entry = _recording_setup(tmp_path, monkeypatch)
    _recorder_state(tmp_path, stamp, since=stamp-300)
    # The old worker can still create well-formed files after watch sees a new room.
    _fragment(tmp_path, stamp-10, _silent_flv())
    _progress(tmp_path, stamp, monkeypatch, **scope)
    status = am.recording_health(entry, now=stamp)
    assert status["state"] == "stalled" and status["age_sec"] == 300


def test_empty_receipt_cannot_claim_current_worker_file_growth_as_audio(tmp_path, monkeypatch):
    stamp, entry = _recording_setup(tmp_path, monkeypatch)
    _recorder_state(tmp_path, stamp)
    _fragment(tmp_path, stamp-1, _silent_flv())
    _starting_progress(tmp_path, stamp, monkeypatch)
    assert am.recording_health(entry, now=stamp)["state"] == "stalled"


@pytest.mark.parametrize("changes", [
    None, {"web_rid": "other-room"}, {"session_id": "other-session"},
    {"session_id": ""}, {"offline_seen": True}, {"live_since": float("nan")},
    {"live_since": float("inf")}, {"live_since": True}, {"live_since": "100"},
    {"live_since": 1793800100.0},
])
def test_untrusted_state_uses_continuous_watch_history_not_process_birth(tmp_path, monkeypatch, changes):
    stamp, entry = _recording_setup(tmp_path, monkeypatch)
    if changes is not None:
        _recorder_state(tmp_path, stamp, since=stamp-1)
        path = tmp_path / "stream_X.state.json"
        row = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**row, **changes}), encoding="utf-8")
    rows = [live("other-session", ts=stamp-500), live(ts=stamp-300), live(ts=stamp)]
    (tmp_path / "watch_X.jsonl").write_text(
        "".join(json.dumps(row)+"\n" for row in rows), encoding="utf-8")
    _starting_progress(tmp_path, stamp, monkeypatch, blank=True)
    status = am.recording_health(entry, now=stamp)
    assert status["state"] == "stalled" and status["age_sec"] == 300


def test_room_change_with_same_session_id_uses_current_room_start(tmp_path, monkeypatch):
    stamp, entry = _recording_setup(tmp_path, monkeypatch)
    _recorder_state(tmp_path, stamp)
    rows = [live(ts=stamp-500), live(ts=stamp-10, web_rid="new-room")]
    (tmp_path / "watch_X.jsonl").write_text(
        "".join(json.dumps(row)+"\n" for row in rows), encoding="utf-8")
    _fragment(tmp_path, stamp-20, _silent_flv())
    _starting_progress(tmp_path, stamp, monkeypatch, blank=True)
    status = am.recording_health(entry, now=stamp)
    assert status["state"] == "starting" and status["age_sec"] == 10
    assert am.recording_health(entry, now=stamp+171)["state"] == "stalled"


def test_confirmed_offline_boundary_starts_new_grace_and_excludes_old_audio(tmp_path, monkeypatch):
    stamp, entry = _recording_setup(tmp_path, monkeypatch)
    _recorder_state(tmp_path, stamp)
    path = tmp_path / "stream_X.state.json"
    row = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**row, "offline_seen": True}), encoding="utf-8")
    rows = [live(ts=stamp-500), {**offline(), "ts": stamp-20}, live(ts=stamp-10)]
    (tmp_path / "watch_X.jsonl").write_text(
        "".join(json.dumps(row)+"\n" for row in rows), encoding="utf-8")
    _fragment(tmp_path, stamp-25, _silent_flv())
    _starting_progress(tmp_path, stamp, monkeypatch, blank=True)
    status = am.recording_health(entry, now=stamp)
    assert status["state"] == "starting" and status["age_sec"] == 10


def test_insufficient_watch_history_cannot_use_new_pid_or_future_since(tmp_path, monkeypatch):
    stamp, entry = _recording_setup(tmp_path, monkeypatch)
    _recorder_state(tmp_path, stamp, since=stamp+100)
    row = live(ts=stamp-1, padding="x"*1000)
    (tmp_path / "watch_X.jsonl").write_text((json.dumps(row)+"\n")*100, encoding="utf-8")
    _starting_progress(tmp_path, stamp, monkeypatch, blank=True)
    assert am.recording_health(entry, now=stamp)["state"] == "unknown"


def test_current_valid_audio_remains_evidence_when_session_start_is_unavailable(tmp_path, monkeypatch):
    stamp, entry = _recording_setup(tmp_path, monkeypatch)
    row = live(ts=stamp-1, padding="x"*1000)
    (tmp_path / "watch_X.jsonl").write_text((json.dumps(row)+"\n")*100, encoding="utf-8")
    _progress(tmp_path, stamp, monkeypatch)
    assert am.recording_health(entry, now=stamp) == {
        "state": "ok", "age_sec": 5, "reason": "audio_progress"}


def test_recording_format_alert_retries_without_starting_watchers(tmp_path, monkeypatch):
    clock = _probe_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(hp, "probe", lambda *a: [])
    monkeypatch.setattr(hp, "probe_recording", lambda *a: [{"tag": "X", "age": 600, "kind": "录音格式异常"}])
    hp._save_state({})
    messages = []
    def deliver(text, **kwargs):
        messages.append(text)
        return len(messages) > 1
    monkeypatch.setattr(hp.notify, "send_text", deliver)
    assert hp.main() == 1
    assert "未见有效音频包（录音格式异常）" in messages[0]
    assert "last_heal" not in hp._load_state()
    clock[0] += hp.ALERT_RETRY_SEC
    assert hp.main() == 1 and len(messages) == 2
    clock[0] += hp.ALERT_RETRY_SEC
    assert hp.main() == 1 and len(messages) == 2

def test_writer_progress_is_consumed_by_real_recording_health(tmp_path, monkeypatch):
    stamp, entry = _recording_setup(tmp_path, monkeypatch)
    _recorder_state(tmp_path, stamp)
    monkeypatch.setattr(am, "instance_status", lambda *_: {"pid": os.getpid(), "running": True})
    monkeypatch.setattr(am, "process_create_time", lambda pid: stamp-1000)
    progress = sr.RecordingProgress(tmp_path / "stream_X.progress.json",
                                   session_id="session-A", web_rid="synthetic-room",
                                   process_created_at=stamp-1000)
    writer = sr.FlvSegWriter(tmp_path / "audio", log=lambda *_: None)
    writer.progress = progress
    writer.begin_connection()
    writer.feed(_silent_flv())
    progress.publish(writer, force=True)
    assert am.recording_health(entry, now=stamp)["state"] == "ok"
    # Parser failure has no authority to refresh the last valid audio timestamp.
    monkeypatch.setattr(am.time, "time", lambda: stamp+181)
    with pytest.raises(sr.FlvParseError):
        writer.feed(b"\x00" * 15)
    (tmp_path / "watch_X.jsonl").write_text(json.dumps(live(ts=stamp+181))+"\n", encoding="utf-8")
    status = am.recording_health(entry, now=stamp+181)
    assert status["state"] == "stalled" and status["reason"] == "invalid_audio"
    writer.close()
