"""Synthetic byte streams, temporary outputs, mocked HTTP; no recordings/network/models."""
import json
import os
from pathlib import Path
import sys
from types import ModuleType

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import stream_recorder as sr


def tag(kind, payload, timestamp=0, stream_id=0):
    size = len(payload)
    return (bytes([kind]) + size.to_bytes(3, "big")
            + (timestamp & 0xFFFFFF).to_bytes(3, "big")
            + bytes([timestamp >> 24]) + stream_id.to_bytes(3, "big")
            + payload + (11 + size).to_bytes(4, "big"))


META = tag(18, b"\x02\x00\x0aonMetaData\x08\0\0\0\0\0\0\x09")
SEQ = tag(8, b"\xaf\0\x12\x10")
SEQ48 = tag(8, b"\xaf\0\x11\x90")


def audio(n=1):
    # Payload is synthetic; tests certify container structure, not AAC decoding.
    return tag(8, b"\xaf\x01" + bytes([n]) * 12, n * 21)


def parsed(blob):
    assert blob[:13] == sr.FLV_HEADER
    rows, pos = [], 13
    while pos < len(blob):
        assert pos + 15 <= len(blob)
        size = int.from_bytes(blob[pos + 1:pos + 4], "big")
        end = pos + 15 + size
        assert end <= len(blob)
        assert blob[pos + 8:pos + 11] == b"\0\0\0"
        assert int.from_bytes(blob[end - 4:end], "big") == size + 11
        rows.append((blob[pos], blob[pos + 11:end - 4]))
        pos = end
    return rows


@pytest.fixture
def clock(monkeypatch):
    value = [1000.0]
    monkeypatch.setattr(sr, "now", lambda: value[0])
    return value


def writer(tmp_path, **kwargs):
    return sr.FlvSegWriter(tmp_path, seg_seconds=5, log=lambda *_: None, **kwargs)


@pytest.mark.parametrize("step", [1, 7, 13, 19, 65536])
def test_arbitrary_http_chunk_boundaries_keep_exact_valid_tags(tmp_path, step):
    w = writer(tmp_path)
    stream = sr.FLV_HEADER + META + SEQ + audio(1) + audio(2)
    w.begin_connection()
    for p in range(0, len(stream), step):
        w.feed(stream[p:p + step])
    w.end_connection()
    output = (tmp_path / "seg_0001.flv").read_bytes()
    assert output == stream
    assert len(parsed(output)) == 4
    assert w.total_bytes == len(output)


def test_reconnect_discards_partial_old_tag_and_requires_new_config(tmp_path, clock):
    w = writer(tmp_path)
    w.begin_connection()
    w.feed(sr.FLV_HEADER + META + SEQ + audio(1) + audio(2)[:-4])
    assert w.buf
    w.end_connection()
    assert not w.buf
    clock[0] += 2
    w.begin_connection()
    assert w.init_tags == [] and not w.header_seen
    w.feed(sr.FLV_HEADER[:6])
    w.feed(sr.FLV_HEADER[6:] + META + SEQ48 + audio(3))
    w.close()
    first = parsed((tmp_path / "seg_0001.flv").read_bytes())
    second = parsed((tmp_path / "seg_0002.flv").read_bytes())
    assert first == [(18, META[11:-4]), (8, SEQ[11:-4]), (8, audio(1)[11:-4])]
    assert second == [(18, META[11:-4]), (8, SEQ48[11:-4]), (8, audio(3)[11:-4])]
    assert w.connection_id == 2 and w.valid_audio_packets == 2


def test_second_flv_without_explicit_begin_fails_instead_of_resyncing(tmp_path):
    w = writer(tmp_path)
    w.feed(sr.FLV_HEADER + META + SEQ + audio())
    with pytest.raises(sr.FlvParseError, match="invalid_tag_type"):
        w.feed(sr.FLV_HEADER + META + SEQ + audio(2))
    assert not w.buf
    assert len(parsed((tmp_path / "seg_0001.flv").read_bytes())) == 3
    with pytest.raises(sr.FlvParseError, match="connection_not_started"):
        w.feed(audio(3))


@pytest.mark.parametrize("header,reason", [
    (b"garbage!!", "invalid_flv_header"),
    (b"FLV\x02\x05\0\0\0\x09", "invalid_flv_header"),
    (b"FLV\x01\x80\0\0\0\x09", "invalid_flv_header"),
    (b"FLV\x01\x05\0\0\0\0", "invalid_header_length"),
    (b"FLV\x01\x05\0\0\x20\0", "invalid_header_length"),
    (sr.FLV_HEADER[:-1] + b"\x01", "invalid_first_previous_size"),
])
def test_invalid_flv_header_never_creates_output(tmp_path, header, reason):
    w = writer(tmp_path)
    with pytest.raises(sr.FlvParseError, match=reason):
        w.feed(header)
    assert not list(tmp_path.glob("*.flv")) and len(w.buf) == 0


@pytest.mark.parametrize("kind,size", [(8, 8193), (18, 65537), (9, 8 * 1024 * 1024 + 1), (8, 0)])
def test_bogus_length_rejected_from_header_without_waiting_for_body(tmp_path, kind, size):
    w = writer(tmp_path)
    with pytest.raises(sr.FlvParseError, match="invalid_tag_size"):
        w.feed(sr.FLV_HEADER + bytes([kind]) + size.to_bytes(3, "big") + b"\0" * 7)
    assert not w.buf and not list(tmp_path.glob("*.flv"))


@pytest.mark.parametrize("raw,reason", [
    (tag(8, b"\xaf\x01x", stream_id=1), "invalid_stream_id"),
    (audio()[:-1] + b"\xff", "invalid_previous_tag_size"),
    (tag(8, b"\x2f\x01x"), "invalid_aac_tag"),
    (tag(8, b"\xaf\x02x"), "invalid_aac_tag"),
    (tag(8, b"\xaf\x01"), "invalid_aac_tag"),
    (tag(8, b"\xaf\0\0\0"), "invalid_aac_config"),
])
def test_bad_complete_tag_does_not_write_or_advance_audio(tmp_path, raw, reason):
    w = writer(tmp_path)
    w.feed(sr.FLV_HEADER + META + SEQ)
    size = w.total_bytes
    with pytest.raises(sr.FlvParseError, match=reason):
        w.feed(raw)
    assert w.total_bytes == size and w.valid_audio_packets == 0
    assert len(parsed((tmp_path / "seg_0001.flv").read_bytes())) == 2


def test_prior_connection_config_cannot_authorize_new_raw_packet(tmp_path):
    w = writer(tmp_path)
    w.feed(sr.FLV_HEADER + SEQ + audio())
    w.begin_connection()
    with pytest.raises(sr.FlvParseError, match="aac_config_missing"):
        w.feed(sr.FLV_HEADER + audio(2))
    assert w.valid_audio_packets == 1


def test_aac_mono_flags_are_accepted_and_config_is_preserved(tmp_path):
    w = writer(tmp_path)
    mono_config = tag(8, b"\xae\0\x12\x08")
    mono_raw = tag(8, b"\xae\x01synthetic-mono")
    w.feed(sr.FLV_HEADER + mono_config + mono_raw)
    w.close()
    assert w.valid_audio_packets == 1
    assert parsed((tmp_path / "seg_0001.flv").read_bytes()) == [
        (8, mono_config[11:-4]), (8, mono_raw[11:-4])]


def test_non_metadata_script_event_does_not_abort_or_enter_init_cache(tmp_path, clock):
    w = writer(tmp_path)
    cue = tag(18, b"\x02\x00\x0aonCuePoint\x08\0\0\0\0\0\0\x09")
    w.feed(sr.FLV_HEADER + META + SEQ + audio(1) + cue)
    clock[0] += 6
    w.feed(audio(2))
    w.close()
    assert (18, cue[11:-4]) in parsed((tmp_path / "seg_0001.flv").read_bytes())
    assert (18, cue[11:-4]) not in parsed((tmp_path / "seg_0002.flv").read_bytes())
    assert w.init_tags == [META, SEQ]


def test_config_replacement_rotates_and_replays_only_current_config(tmp_path, clock):
    w = writer(tmp_path)
    w.feed(sr.FLV_HEADER + META + SEQ + audio(1))
    w.feed(SEQ48 + audio(2))
    clock[0] += 6
    w.feed(tag(8, b"\xaf\0\x11\x90", 500))  # redundant config is not accumulated
    w.feed(audio(3))
    w.close()
    assert len(w.segments) == 3
    for n in (2, 3):
        rows = parsed((tmp_path / f"seg_{n:04}.flv").read_bytes())
        assert [d for t, d in rows if t == 8 and d[1] == 0] == [b"\xaf\0\x11\x90"]
    assert len(w.init_tags) == 2
    assert w.total_bytes == sum(p.stat().st_size for p in tmp_path.glob("*.flv"))


def test_rotation_initialization_is_charged_to_quota(tmp_path, clock):
    stream = sr.FLV_HEADER + META + SEQ + audio(1)
    w = writer(tmp_path, quota_bytes=len(stream) * 2)
    w.feed(stream)
    clock[0] += 6
    with pytest.raises(sr.QuotaExceeded):
        w.feed(audio(2))
    w.close()
    assert w.total_bytes == sum(p.stat().st_size for p in tmp_path.glob("*.flv"))
    assert w.total_bytes == len(stream) * 2


def test_progress_only_claims_flushed_raw_aac_not_network_or_metadata(tmp_path, clock):
    w = writer(tmp_path / "audio")
    progress = sr.RecordingProgress(tmp_path / "progress.json", session_id="test-session",
                                   web_rid="test-room", process_created_at=900.0)
    w.progress = progress
    w.begin_connection()
    assert json.loads(progress.path.read_text())["last_valid_audio_at"] is None
    clock[0] += 6
    w.feed(sr.FLV_HEADER + META + SEQ + tag(9, b"synthetic-video"))
    assert json.loads(progress.path.read_text())["valid_audio_packets"] == 0
    clock[0] += 6
    w.feed(audio(0))  # even synthetic zero-valued payload is not a silence alarm
    saved = json.loads(progress.path.read_text())
    assert saved["last_valid_audio_at"] == clock[0]
    assert saved["valid_audio_packets"] == 1 and saved["valid_audio_bytes"] == 12
    assert saved["phase"] == "recording"
    assert len(parsed((w.dir / "seg_0002.flv").read_bytes())) == 3
    clock[0] += 6
    w.begin_connection()
    saved = json.loads(progress.path.read_text())
    assert saved["valid_audio_packets"] == 1 and saved["last_valid_audio_at"] == clock[0] - 6
    assert saved["phase"] == "connecting"
    w.close()


def test_progress_reports_safe_parse_error_and_atomic_failure_keeps_prior(tmp_path, clock, monkeypatch):
    w = writer(tmp_path / "audio")
    w.progress = sr.RecordingProgress(tmp_path / "progress.json", session_id="test-session",
                                     web_rid="test-room", process_created_at=900.0)
    w.begin_connection()
    with pytest.raises(sr.FlvParseError):
        w.feed(b"invalid!!")
    saved = w.progress.path.read_bytes()
    assert json.loads(saved)["parser_error"] == "invalid_flv_header"
    monkeypatch.setattr(sr.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("synthetic")))
    with pytest.raises(OSError):
        w.progress.publish(force=True)
    assert w.progress.path.read_bytes() == saved
    assert not list(tmp_path.glob("*.tmp"))


def test_sidecar_failure_does_not_prevent_audio_close_and_quota_finalization(tmp_path, clock, monkeypatch):
    w = writer(tmp_path / "audio")
    w.progress = sr.RecordingProgress(tmp_path / "progress.json", session_id="test-session",
                                     web_rid="test-room", process_created_at=900.)
    w.begin_connection()
    saved = w.progress.path.read_bytes()
    replace = sr.os.replace
    def fail_progress(source, destination):
        if os.fspath(destination) == os.fspath(w.progress.path):
            raise OSError("synthetic progress sidecar failure")
        return replace(source, destination)
    monkeypatch.setattr(sr.os, "replace", fail_progress)
    clock[0] += 6
    w.feed(sr.FLV_HEADER + META + SEQ + audio())
    w.close()  # observability failure cannot skip main's following quota/meta work
    assert w.fh is None and len(w.segments) == 1
    assert w.total_bytes == (w.dir / "seg_0001.flv").stat().st_size
    assert w.progress.path.read_bytes() == saved


@pytest.mark.parametrize("pid,created,action,accepted", [
    (None, 900., "stop", True),
    (-1, 900., "stop", False),
    (None, 899., "stop", False),
    (None, 900., "restart", False),
    (None, float("nan"), "stop", False),
])
def test_cooperative_stop_is_bound_to_pid_and_creation(tmp_path, monkeypatch, pid, created, action, accepted):
    monkeypatch.setattr(sr, "DATA", tmp_path)
    request = {"schema": 1, "pid": os.getpid() if pid is None else pid,
               "process_created_at": created, "action": action}
    (tmp_path / "stream_test.stop.json").write_text(json.dumps(request))
    assert sr.stop_requested("test", 900.) is accepted
    assert sr.stop_requested("test", None) is False


def test_stop_interrupts_backoff_without_extra_capture(tmp_path, monkeypatch):
    checks, slept = [], []
    monkeypatch.setattr(sr.time, "sleep", lambda value: slept.append(value))
    def stop():
        checks.append(True)
        return len(checks) == 3
    assert sr.interruptible_wait(60, stop)
    assert slept == [0.5, 0.5]


def test_pull_loop_resets_each_http_response_including_truncated_eof(tmp_path, clock, monkeypatch):
    quit_flag = [False]
    class Response:
        status_code = 200
        def __init__(self, second=False):
            self.second, self.closed = second, False
        def iter_content(self, _size):
            if not self.second:
                yield sr.FLV_HEADER + META + SEQ + audio(1) + audio(2)[:-5]
            else:
                yield sr.FLV_HEADER + META + SEQ48 + audio(3)
                quit_flag[0] = True
                yield audio(4)
        def close(self):
            self.closed = True
    responses = [Response(), Response(True)]
    pending = iter(responses)
    class Session:
        def get(self, *args, **kwargs):
            return next(pending)
    cap = {"playerFlvUrls": ["https://synthetic.invalid/x?expire=9999999999"]}
    monkeypatch.setattr(sr, "build_session", lambda *_: Session())
    monkeypatch.setattr(sr, "capture_stream", lambda *_: cap)
    monkeypatch.setattr(sr, "read_watch_state", lambda *_: None)
    monkeypatch.setattr(sr.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    w = writer(tmp_path)
    assert sr.pull_loop("test", "test-room", w, lambda *_: None, 30, 30, 0,
                        "synthetic-node", cap, quit_flag) == "signal"
    assert all(r.closed for r in responses)
    assert w.connection_id == 2 and w.valid_audio_packets == 2 and not w.buf
    assert len(list(tmp_path.glob("*.flv"))) == 2
    for p in tmp_path.glob("*.flv"):
        rows = parsed(p.read_bytes())
        assert len([d for t, d in rows if t == 8 and d[1] == 1]) == 1


def test_main_publishes_new_code_identity_before_first_audio(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "DATA", tmp_path)
    monkeypatch.setattr(sr.sys, "argv", ["recorder", "--tag", "synthetic", "--once"])
    monkeypatch.setattr(sr.signal, "signal", lambda *_: None)
    monkeypatch.setattr(sr, "resolve_node", lambda: "synthetic-node")
    monkeypatch.setattr(sr, "_process_created_at", lambda *_: 900.)
    monkeypatch.setattr(sr, "Log", lambda *_: lambda *_: None)
    monkeypatch.setitem(sys.modules, "curl_cffi", ModuleType("curl_cffi"))
    def state(_tag):
        ready = json.loads((tmp_path / "stream_synthetic.progress.json").read_text())
        assert ready["implementation"] == "flv_connection_v2"
        assert ready["pid"] == os.getpid() and ready["process_created_at"] == 900.
        assert ready["last_valid_audio_at"] is None
        return None
    monkeypatch.setattr(sr, "read_watch_state", state)
    assert sr.main() == 2
    assert not (tmp_path / "stream_synthetic.pid").exists()


def test_main_same_session_retry_keeps_recent_audio_and_accounts_all_files(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(sr, "DATA", tmp_path)
    monkeypatch.setattr(sr.sys, "argv", ["recorder", "--tag", "synthetic"])
    monkeypatch.setattr(sr.signal, "signal", lambda *_: None)
    monkeypatch.setattr(sr, "resolve_node", lambda: "synthetic-node")
    monkeypatch.setattr(sr, "_process_created_at", lambda *_: 900.)
    monkeypatch.setattr(sr, "Log", lambda *_: lambda *_: None)
    monkeypatch.setitem(sys.modules, "curl_cffi", ModuleType("curl_cffi"))
    monkeypatch.setattr(sr.time, "sleep", lambda *_: None)
    monkeypatch.setattr(sr, "capture_stream", lambda *_: {"synthetic": True})
    monkeypatch.setattr(sr, "read_watch_state", lambda *_: {
        "is_live": True, "live_status": 1, "nick": "synthetic",
        "web_rid": "test-room", "room_id_str": "test-session"})
    attempts = []
    def pull(_tag, _rid, w, *_args):
        attempts.append(w)
        if len(attempts) == 2:
            assert w.valid_audio_packets == 1 and w.last_valid_audio_at == 1000.
            clock[0] += 1
        w.feed(sr.FLV_HEADER + META + SEQ + audio(len(attempts)))
        if len(attempts) == 1:
            raise RuntimeError("synthetic transient failure")
        return "signal"
    monkeypatch.setattr(sr, "pull_loop", pull)
    assert sr.main() == 0
    assert len(attempts) == 2
    progress = json.loads((tmp_path / "stream_synthetic.progress.json").read_text())
    assert progress["valid_audio_packets"] == 2 and progress["last_valid_audio_at"] == 1001.
    quota = json.loads((tmp_path / "stream_synthetic.state.json").read_text())
    files = list(tmp_path.glob("audio_*/*.flv"))
    assert len(files) == 2
    assert quota["bytes_used"] == sum(p.stat().st_size for p in files)


def test_video_trickle_without_audio_hits_progress_deadline(tmp_path, clock, monkeypatch):
    w = writer(tmp_path)
    class Response:
        status_code = 200
        closed = False
        def iter_content(self, _size):
            yield sr.FLV_HEADER + META + SEQ
            clock[0] += 121
            yield tag(9, b"synthetic-video")
        def close(self):
            self.closed = True
    response = Response()
    class Session:
        def get(self, *args, **kwargs):
            return response
    cap = {"playerFlvUrls": ["https://synthetic.invalid/x?expire=9999999999"]}
    monkeypatch.setattr(sr, "build_session", lambda *_: Session())
    monkeypatch.setattr(sr, "capture_stream", lambda *_: pytest.fail("stop should precede recapture"))
    monkeypatch.setattr(sr, "read_watch_state", lambda *_: None)
    w.stop_check = lambda: w.parser_error == "audio_stalled"
    assert sr.pull_loop("test", "test-room", w, lambda *_: None, 30, 30, 0,
                        "synthetic-node", cap, [False]) == "signal"
    assert response.closed and w.parser_error == "audio_stalled"
    assert w.valid_audio_packets == 0 and w.last_valid_audio_at is None
