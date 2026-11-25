"""Durable result/projection recovery replaces the former append/state contract."""
import json
import os
import sys

import pytest

from scripts import audio_segments as audio
from scripts import transcript_results as results
from scripts import transcribe_backfill as tb


def closed(folder, content=b"0" * 4096):
    number, path = audio.reserve_segment(folder.parent, folder)
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    audio.publish_closed(folder.parent, folder, path, number, 100 + number, 101 + number)
    return path


@pytest.fixture
def recording(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "DATA", tmp_path)
    tb._release_run_lock()
    folder = tmp_path / "audio_X_2000-01-01"
    folder.mkdir()
    closed(folder)
    monkeypatch.setattr(tb, "transcribe_audio", lambda *_args: (
        [{"start": 0, "end": 1, "text": "synthetic"}], 1))
    yield folder
    tb._release_run_lock()


def test_result_replace_failure_preserves_previous_result_and_text(recording, monkeypatch):
    assert tb.transcribe_dir(object(), recording, None, False, "synthetic") == 1
    path = results.result_path(tb.DATA, recording, "seg_0001.flv")
    speech = results.projection_paths(tb.DATA, recording)[0]
    previous, previous_text = path.read_bytes(), speech.read_bytes()
    replace = os.replace
    def fail_result_replace(source, destination):
        if os.fspath(destination) == os.fspath(path):
            raise PermissionError("synthetic result replace failure")
        return replace(source, destination)
    monkeypatch.setattr(results.os, "replace", fail_result_replace)
    with pytest.raises(tb.TranscriptionError):
        tb.transcribe_dir(object(), recording, None, True, "synthetic")
    assert path.read_bytes() == previous and speech.read_bytes() == previous_text
    check = results.validate_success(tb.DATA, recording, "seg_0001.flv")
    assert check["result_committed"] and not check["current_result"] and not check["cleanup_eligible"]
    assert not list(path.parent.glob("*.tmp"))


@pytest.mark.parametrize("bad", ['{"truncated":', '[]'])
def test_unknown_legacy_progress_blocks_transcription(recording, monkeypatch, bad):
    path = recording / ".transcribed.json"
    path.write_text(bad, encoding="utf-8")
    monkeypatch.setattr(tb, "transcribe_audio", lambda *_args: pytest.fail("legacy progress was ignored"))
    with pytest.raises(ValueError, match="legacy_conflict"):
        tb.transcribe_dir(object(), recording, None, False, "synthetic")
    assert path.read_text(encoding="utf-8") == bad


def test_failed_segment_returns_failure_and_can_retry(recording, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["transcribe_backfill", "--all"])
    monkeypatch.setattr(tb, "find_ffmpeg", lambda: None)
    monkeypatch.setattr(tb, "load_model", lambda *_args: object())
    def failed_audio(*_args):
        raise RuntimeError("synthetic decode failure")
    monkeypatch.setattr(tb, "transcribe_audio", failed_audio)
    assert tb.main() == 1 and tb._run_lock is None
    assert not results.validate_success(tb.DATA, recording, "seg_0001.flv")["cleanup_eligible"]
    monkeypatch.setattr(tb, "transcribe_audio", lambda *_args: (
        [{"start": 0, "end": 1, "text": "synthetic"}], 1))
    assert tb.main() == 0
    assert results.validate_success(tb.DATA, recording, "seg_0001.flv")["cleanup_eligible"]
    assert not (recording / ".transcribed.json").exists()


def test_partial_failure_reuses_committed_segment_on_retry(recording, monkeypatch):
    closed(recording, b"1" * 4096)
    monkeypatch.setattr(sys, "argv", ["transcribe_backfill", "--all"])
    monkeypatch.setattr(tb, "find_ffmpeg", lambda: None)
    monkeypatch.setattr(tb, "load_model", lambda *_args: object())
    def first_pass(_model, path, _prompt):
        if path.name == "seg_0002.flv":
            raise RuntimeError("synthetic second segment failure")
        return [{"start": 0, "end": 1, "text": "synthetic"}], 1
    monkeypatch.setattr(tb, "transcribe_audio", first_pass)
    assert tb.main() == 1
    first = results.result_path(tb.DATA, recording, "seg_0001.flv").read_bytes()
    calls = []
    def retry(_model, path, _prompt):
        calls.append(path.name)
        return [{"start": 0, "end": 1, "text": "synthetic"}], 1
    monkeypatch.setattr(tb, "transcribe_audio", retry)
    assert tb.main() == 0 and calls == ["seg_0002.flv"]
    assert results.result_path(tb.DATA, recording, "seg_0001.flv").read_bytes() == first
    rows = [json.loads(line) for line in results.projection_paths(tb.DATA, recording)[0].read_text(encoding="utf-8").splitlines()]
    assert [row["seg"] for row in rows] == ["seg_0001", "seg_0002"]


def test_failed_force_retains_old_output_without_current_success(recording, monkeypatch):
    tb.transcribe_dir(object(), recording, None, False, "synthetic")
    result = results.result_path(tb.DATA, recording, "seg_0001.flv")
    speech = results.projection_paths(tb.DATA, recording)[0]
    old_result, old_text = result.read_bytes(), speech.read_bytes()
    def failed_audio(*_args):
        raise RuntimeError("synthetic force failure")
    monkeypatch.setattr(tb, "transcribe_audio", failed_audio)
    with pytest.raises(tb.TranscriptionError):
        tb.transcribe_dir(object(), recording, None, True, "synthetic")
    assert result.read_bytes() == old_result and speech.read_bytes() == old_text
    check = results.validate_success(tb.DATA, recording, "seg_0001.flv")
    assert check["result_committed"] and not check["current_result"] and not check["cleanup_eligible"]
    monkeypatch.setattr(tb, "transcribe_audio", lambda *_args: (
        [{"start": 0, "end": 1, "text": "synthetic"}], 1))
    assert tb.transcribe_dir(object(), recording, None, False, "synthetic") == 1
    assert len(speech.read_text(encoding="utf-8").splitlines()) == 1


def test_projection_receipt_failure_recovers_without_repeating_inference(recording, monkeypatch):
    speech, receipt, _ = results.projection_paths(tb.DATA, recording)
    atomic = results._atomic
    def fail_receipt(path, value):
        if path == receipt:
            raise PermissionError("synthetic projection receipt failure")
        return atomic(path, value)
    monkeypatch.setattr(results, "_atomic", fail_receipt)
    with pytest.raises(tb.TranscriptionError):
        tb.transcribe_dir(object(), recording, None, False, "synthetic")
    check = results.validate_success(tb.DATA, recording, "seg_0001.flv")
    assert check["result_committed"] and not check["projection_ready"]
    monkeypatch.setattr(results, "_atomic", atomic)
    monkeypatch.setattr(tb, "transcribe_audio", lambda *_args: pytest.fail("result recovery repeated inference"))
    assert tb.transcribe_dir(object(), recording, None, False, "synthetic") == 0
    assert results.validate_success(tb.DATA, recording, "seg_0001.flv")["cleanup_eligible"]
    assert len(speech.read_text(encoding="utf-8").splitlines()) == 1
