"""Synthetic files and recognition only: commit boundaries, not real ASR/media."""
import json
import os
from pathlib import Path
import threading

import pytest

from scripts import audio_segments as audio
from scripts import transcript_results as results


@pytest.fixture
def day(tmp_path):
    folder = tmp_path / "audio_generated_2000-01-01"
    folder.mkdir()
    return tmp_path, folder


def closed(day, *, start=1000, payload=b"a" * 4096):
    data, folder = day
    number, path = audio.reserve_segment(data, folder)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    audio.publish_closed(data, folder, path, number, start, start + 60)
    return path.name


def recognize(_path):
    return [{"start": 0, "end": 1, "text": "generated speech"}], 60


def committed(day, name=None):
    data, folder = day
    name = name or closed(day)
    result = results.process_segment(data, folder, name, recognize, "generated-model")
    assert result["state"] == "completed" and result["result_committed"]
    return name


def never(_path):
    pytest.fail("durable result was unnecessarily inferred again")


def test_committed_result_is_authority_without_progress_and_after_model_move(day):
    data, folder = day
    name = committed(day)
    assert not (folder / ".transcribed.json").exists()
    assert results.validate_success(data, folder, name)["reason"] == "projection_pending"
    assert results.process_segment(data, folder, name, never, "different-install/model")["state"] == "reused"
    results.rebuild_projection(data, folder)
    status = results.validate_success(data, folder, name)
    assert status["result_committed"] and status["projection_ready"] and status["cleanup_eligible"]


def test_projection_preserves_segment_order_offsets_and_legitimate_duplicates(day):
    data, folder = day
    first = closed(day, start=2000)
    raw = [{"start": 2, "end": 3, "text": "later"},
           {"start": 1, "end": 2, "text": "same"}, {"start": 1, "end": 2, "text": "same"}]
    results.process_segment(data, folder, first, lambda _: (raw, 60), "model")
    results.rebuild_projection(data, folder)
    second = closed(day, start=1000)  # Wall clock moved backwards; sequence did not.
    committed(day, second)
    results.rebuild_projection(data, folder)
    speech, _, _ = results.projection_paths(data, folder)
    rows = [json.loads(line) for line in speech.read_text(encoding="utf-8").splitlines()]
    assert [row["seg"] for row in rows] == [Path(first).stem] * 3 + [Path(second).stem]
    assert [row["off"] for row in rows[:3]] == [1, 1, 2]
    assert [row["text"] for row in rows[:3]] == ["same", "same", "later"]
    assert set(rows[0]) == {"ts", "time", "text", "dur", "tag", "seg", "off"}
    assert results.validate_success(data, folder, first)["cleanup_eligible"]
    assert results.validate_success(data, folder, second)["cleanup_eligible"]


@pytest.mark.parametrize("boundary", ["fsync", "replace"])
def test_result_commit_failure_preserves_facts_and_retry_has_no_duplicate(day, monkeypatch, boundary):
    data, folder = day
    name = closed(day)
    path = results.result_path(data, folder, name)
    actual_replace, actual_fsync = results.os.replace, results.os.fsync
    count = {"fsync": 0}
    def replace(source, target):
        if Path(target) == path:
            raise PermissionError("generated result replace failure")
        return actual_replace(source, target)
    def fsync(fd):
        count["fsync"] += 1
        if count["fsync"] == 2:  # pending attempt then the result's data fsync
            raise PermissionError("generated result fsync failure")
        return actual_fsync(fd)
    with monkeypatch.context() as patch:
        patch.setattr(results.os, "replace" if boundary == "replace" else "fsync", replace if boundary == "replace" else fsync)
        failed = results.process_segment(data, folder, name, recognize, "model")
    assert failed["state"] == "failed" and not failed["result_committed"]
    assert not path.exists()
    assert not list(folder.rglob("*.tmp"))
    assert (folder / name).exists()
    committed(day, name)
    results.rebuild_projection(data, folder)
    speech, _, _ = results.projection_paths(data, folder)
    assert len(speech.read_text(encoding="utf-8").splitlines()) == 1


def test_result_replaced_before_readback_failure_can_be_verified_without_reinference(day, monkeypatch):
    data, folder = day
    name = closed(day)
    path, read = results.result_path(data, folder, name), results._read
    failed = []
    def fail_once(target, *args):
        if target == path and path.exists() and not failed:
            failed.append(True)
            raise PermissionError("generated post-replace verification failure")
        return read(target, *args)
    with monkeypatch.context() as patch:
        patch.setattr(results, "_read", fail_once)
        outcome = results.process_segment(data, folder, name, recognize, "model")
    assert outcome["state"] == "failed"
    assert results.validate_success(data, folder, name)["result_committed"]
    assert results.process_segment(data, folder, name, never, "model")["state"] == "reused"
    results.rebuild_projection(data, folder)
    assert results.validate_success(data, folder, name)["cleanup_eligible"]


def test_projection_replace_failure_preserves_old_output_and_new_result(day, monkeypatch):
    data, folder = day
    first = committed(day)
    results.rebuild_projection(data, folder)
    speech, _, _ = results.projection_paths(data, folder)
    previous = speech.read_bytes()
    second = committed(day)
    replace = results.os.replace
    def fail(source, target):
        if Path(target) == speech:
            raise PermissionError("generated projection replace failure")
        return replace(source, target)
    with monkeypatch.context() as patch:
        patch.setattr(results.os, "replace", fail)
        with pytest.raises(PermissionError):
            results.rebuild_projection(data, folder)
    assert speech.read_bytes() == previous
    assert results.validate_success(data, folder, second)["result_committed"]
    assert not results.validate_success(data, folder, second)["projection_ready"]
    assert results.process_segment(data, folder, second, never, "model")["state"] == "reused"
    results.rebuild_projection(data, folder)
    assert all(results.validate_success(data, folder, name)["cleanup_eligible"] for name in (first, second))
    assert not list(data.glob("*.tmp"))


def test_projection_receipt_failure_keeps_recoverable_owned_output(day, monkeypatch):
    data, folder = day
    name = committed(day)
    speech, receipt, pending = results.projection_paths(data, folder)
    replace = results.os.replace
    def fail(source, target):
        if Path(target) == receipt:
            raise PermissionError("generated receipt failure")
        return replace(source, target)
    with monkeypatch.context() as patch:
        patch.setattr(results.os, "replace", fail)
        with pytest.raises(PermissionError):
            results.rebuild_projection(data, folder)
    assert speech.exists() and pending.exists() and not receipt.exists()
    assert not results.validate_success(data, folder, name)["cleanup_eligible"]
    assert results.process_segment(data, folder, name, never, "model")["state"] == "reused"
    results.rebuild_projection(data, folder)
    assert results.validate_success(data, folder, name)["cleanup_eligible"]
    assert not pending.exists()


@pytest.mark.parametrize("damage", ["projection_truncated", "projection_missing", "result_truncated", "result_replaced", "source_replaced"])
def test_damaged_source_result_or_output_never_gives_cleanup_eligibility(day, damage):
    data, folder = day
    name = committed(day)
    results.rebuild_projection(data, folder)
    speech, _, _ = results.projection_paths(data, folder)
    path = results.result_path(data, folder, name)
    if damage == "projection_truncated":
        speech.write_bytes(speech.read_bytes()[:10])
    elif damage == "projection_missing":
        speech.rename(speech.with_suffix(".retained"))
    elif damage == "result_truncated":
        path.write_bytes(path.read_bytes()[:10])
    elif damage == "result_replaced":
        value = json.loads(path.read_bytes())
        value["rows"][0]["text"] = "replacement with stale checksum"
        path.write_text(json.dumps(value), encoding="utf-8")
    else:
        source = folder / name
        before, payload = source.stat(), source.read_bytes()
        source.rename(source.with_suffix(".retained"))
        source.write_bytes(payload)
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert not results.validate_success(data, folder, name)["cleanup_eligible"]
    if damage.startswith("projection"):
        assert results.process_segment(data, folder, name, never, "model")["state"] == "reused"
        results.rebuild_projection(data, folder)
        assert results.validate_success(data, folder, name)["cleanup_eligible"]


def test_source_changed_during_inference_cannot_commit_success(day):
    data, folder = day
    name = closed(day)
    def replace_source(path):
        info = path.stat()
        path.write_bytes(b"b" * 4096)
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        return recognize(path)
    failed = results.process_segment(data, folder, name, replace_source, "model")
    assert failed["state"] == "failed" and not failed["result_committed"]
    assert (folder / name).exists()


def test_missing_audio_preserves_result_and_text_but_cannot_redo_or_cleanup(day):
    data, folder = day
    name = committed(day)
    results.rebuild_projection(data, folder)
    speech, _, _ = results.projection_paths(data, folder)
    previous = speech.read_bytes()
    (folder / name).rename(folder / (name + ".retained"))
    status = results.validate_success(data, folder, name)
    assert status["result_committed"] and status["projection_ready"] and not status["cleanup_eligible"]
    assert results.process_segment(data, folder, name, never, "model", force=True)["state"] == "source_unconfirmed"
    results.rebuild_projection(data, folder)
    assert speech.read_bytes() == previous


def test_valid_no_speech_is_success_but_small_or_undecoded_is_not(day):
    data, folder = day
    zero = closed(day)
    assert results.process_segment(data, folder, zero, lambda _: ([], 12), "model")["state"] == "completed"
    small = closed(day, payload=b"generated small")
    assert results.process_segment(data, folder, small, never, "model")["state"] == "too_small"
    invalid = closed(day)
    assert results.process_segment(data, folder, invalid, lambda _: ([], 0), "model")["state"] == "failed"
    results.rebuild_projection(data, folder)
    assert results.validate_success(data, folder, zero)["cleanup_eligible"]
    value = json.loads(results.result_path(data, folder, zero).read_bytes())
    assert value["outcome"] == "no_speech" and value["rows"] == []
    assert not results.validate_success(data, folder, small)["result_committed"]
    assert not results.validate_success(data, folder, invalid)["result_committed"]


@pytest.mark.parametrize("legacy", ["speech", "progress", "bad_progress"])
def test_unknown_legacy_material_is_preserved_and_blocks_write(day, legacy):
    data, folder = day
    name = closed(day)
    speech, _, _ = results.projection_paths(data, folder)
    path = speech if legacy == "speech" else folder / ".transcribed.json"
    payload = b'{"unknown":"old"}\n' if legacy != "bad_progress" else b'{"truncated"'
    path.write_bytes(payload)
    with pytest.raises(results.ResultError, match="legacy_conflict"):
        results.process_segment(data, folder, name, never, "model", force=True)
    assert path.read_bytes() == payload and (folder / name).exists()


def test_force_failure_preserves_old_unique_result_and_speech_but_not_current_success(day):
    data, folder = day
    name = committed(day)
    results.rebuild_projection(data, folder)
    speech, _, _ = results.projection_paths(data, folder)
    result_path = results.result_path(data, folder, name)
    previous_result, previous_speech = result_path.read_bytes(), speech.read_bytes()
    def failed(_path):
        raise RuntimeError("generated forced recognition failure")
    outcome = results.process_segment(data, folder, name, failed, "new-model", force=True)
    assert outcome["state"] == "failed" and outcome["result_committed"] and not outcome["current_result"]
    assert result_path.read_bytes() == previous_result and speech.read_bytes() == previous_speech
    assert not results.validate_success(data, folder, name)["cleanup_eligible"]
    next_result = results.process_segment(data, folder, name, recognize, "new-model")
    assert next_result["state"] == "completed" and next_result["generation"] != outcome["generation"]
    results.rebuild_projection(data, folder)
    assert results.validate_success(data, folder, name)["cleanup_eligible"]


def test_global_writer_protects_direct_processing_force_and_projection(day):
    data, folder = day
    name = committed(day)
    held, release, failures = threading.Event(), threading.Event(), []
    def holder():
        with results.writer(data):
            held.set()
            assert release.wait(3)
    worker = threading.Thread(target=holder)
    worker.start()
    try:
        assert held.wait(1)
        for write in (lambda: results.process_segment(data, folder, name, never, "model", force=True),
                      lambda: results.rebuild_projection(data, folder)):
            with pytest.raises(results.WriterBusy):
                write()
        assert results.validate_success(data, folder, name)["result_committed"]
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive() and failures == []


def test_read_only_status_includes_invalid_and_open_sources_without_filesystem_mutation(day):
    data, folder = day
    name = closed(day)
    number, path = audio.reserve_segment(data, folder)
    path.write_bytes(b"unclosed" * 1024)
    # Number allocation correctly refuses an invalid legacy name. Introduce it
    # afterwards to exercise read-only discovery, not allocation bootstrap.
    (folder / "seg_bad.flv").write_bytes(b"generated")
    before = {path.relative_to(data).as_posix(): path.read_bytes() for path in data.rglob("*") if path.is_file()}
    status = results.directory_status(data, folder)
    after = {path.relative_to(data).as_posix(): path.read_bytes() for path in data.rglob("*") if path.is_file()}
    assert before == after
    assert {item["name"] for item in status} == {"seg_bad.flv", name, path.name}
    assert all(not item["cleanup_eligible"] for item in status)


def test_missing_result_does_not_drop_previously_projected_text(day):
    data, folder = day
    first, second = committed(day), committed(day)
    results.rebuild_projection(data, folder)
    speech, _, _ = results.projection_paths(data, folder)
    previous = speech.read_bytes()
    path = results.result_path(data, folder, first)
    path.rename(path.with_suffix(".retained"))
    with pytest.raises(results.ResultError, match="result_missing"):
        results.rebuild_projection(data, folder)
    assert speech.read_bytes() == previous
    assert results.validate_success(data, folder, second)["result_committed"]


def test_old_receipt_cannot_hide_new_pending_coverage_after_receipt_failure(day, monkeypatch):
    data, folder = day
    first = committed(day)
    results.rebuild_projection(data, folder)
    second = committed(day)
    speech, receipt, pending = results.projection_paths(data, folder)
    replace = results.os.replace
    def fail(source, target):
        if Path(target) == receipt:
            raise PermissionError("generated second receipt replacement failure")
        return replace(source, target)
    with monkeypatch.context() as patch:
        patch.setattr(results.os, "replace", fail)
        with pytest.raises(PermissionError):
            results.rebuild_projection(data, folder)
    # Receipt says A while pending and actual speech contain AB.
    assert pending.exists()
    previous = speech.read_bytes()
    assert len(previous.splitlines()) == 2
    path = results.result_path(data, folder, second)
    path.rename(path.with_suffix(".retained"))
    with pytest.raises(results.ResultError, match="result_missing"):
        results.rebuild_projection(data, folder)
    assert speech.read_bytes() == previous
    assert results.validate_success(data, folder, first)["result_committed"]


def test_replaced_old_result_cannot_roll_back_a_published_force_generation(day, monkeypatch):
    data, folder = day
    name = committed(day)
    results.rebuild_projection(data, folder)
    path = results.result_path(data, folder, name)
    old_result = path.read_bytes()
    outcome = results.process_segment(data, folder, name,
        lambda _: ([{"start": 0, "end": 1, "text": "new generated generation"}], 60), "new-model", force=True)
    assert outcome["state"] == "completed"
    speech, receipt, pending = results.projection_paths(data, folder)
    replace = results.os.replace
    def fail(source, target):
        if Path(target) == receipt:
            raise PermissionError("generated force receipt replacement failure")
        return replace(source, target)
    with monkeypatch.context() as patch:
        patch.setattr(results.os, "replace", fail)
        with pytest.raises(PermissionError):
            results.rebuild_projection(data, folder)
    assert pending.exists()
    published = speech.read_bytes()
    path.write_bytes(old_result)  # Valid old checksum; the newer attempt remains.
    with pytest.raises(results.ResultError, match="result_generation_missing"):
        results.rebuild_projection(data, folder)
    assert speech.read_bytes() == published
    assert not results.validate_success(data, folder, name)["cleanup_eligible"]
