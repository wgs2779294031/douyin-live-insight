"""Generated files only: accepted intents and exact generation succession."""
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import audio_segments as audio
from scripts import transcript_results as results


@pytest.fixture
def day(tmp_path):
    folder = tmp_path / "audio_generation_2000-01-01"
    folder.mkdir()
    return tmp_path, folder


def closed(day):
    data, folder = day
    number, path = audio.reserve_segment(data, folder)
    with path.open("xb") as stream:
        stream.write(b"generated-not-media" * 300)
        stream.flush()
        os.fsync(stream.fileno())
    audio.publish_closed(data, folder, path, number, 1000 + number, 1060 + number)
    return path.name


def process(day, name, text, *, force=False, calls=None):
    def recognize(path):
        if calls is not None:
            calls.append(path.name)
        return [{"start": 0, "end": 1, "text": text}], 60
    return results.process_segment(*day, name, recognize, "generated-model", force=force)


def ready(day, text="generation-A"):
    name = closed(day)
    outcome = process(day, name, text)
    assert outcome["state"] == "completed"
    results.rebuild_projection(*day)
    assert results.validate_success(*day, name)["cleanup_eligible"]
    return name, outcome


def failed(_path):
    raise ValueError("generated recognizer failure")


def never(_path):
    pytest.fail("recovery unexpectedly inferred again")


def write_sealed(path, value):
    payload = {key: item for key, item in value.items() if key != "checksum"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False).encode("utf-8")
    sealed = dict(payload, checksum=hashlib.sha256(canonical).hexdigest())
    path.write_text(json.dumps(sealed, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False) + "\n", encoding="utf-8")


def snapshot_pair(day, name):
    return (results.result_path(*day, name).read_bytes(),
            results.attempt_path(*day, name).read_bytes())


def restore_pair(day, name, saved):
    result, attempt = results.result_path(*day, name), results.attempt_path(*day, name)
    result.with_suffix(".new-retained").write_bytes(result.read_bytes())
    attempt.with_suffix(".new-retained").write_bytes(attempt.read_bytes())
    result.write_bytes(saved[0])
    attempt.write_bytes(saved[1])


@pytest.mark.parametrize("fault", ["missing", "corrupt"])
def test_failed_force_with_lost_or_bad_intent_keeps_only_historical_success(day, fault):
    name, _ = ready(day)
    speech, _, _ = results.projection_paths(*day)
    before = speech.read_bytes()
    assert results.process_segment(*day, name, failed, "new-model", force=True)["state"] == "failed"
    intent = results.attempt_path(*day, name)
    intent.rename(intent.with_suffix(".retained"))
    if fault == "corrupt":
        intent.write_bytes(b'{"generated-truncated":')
    status = results.validate_success(*day, name)
    assert status["result_committed"]
    assert not status["current_result"] and not status["reusable"] and not status["cleanup_eligible"]
    assert speech.read_bytes() == before and (day[1] / name).exists()
    if fault == "corrupt":
        with pytest.raises(results.ResultError):
            results.process_segment(*day, name, never, "new-model")
        assert intent.read_bytes() == b'{"generated-truncated":'


def test_ordinary_retry_after_missing_accepted_intent_does_not_reuse_older_generation(day):
    name, old = ready(day)
    assert results.process_segment(*day, name, failed, "new-model", force=True)["state"] == "failed"
    intent = results.attempt_path(*day, name)
    intent.rename(intent.with_suffix(".retained"))
    calls = []
    retried = process(day, name, "newly-retried", calls=calls)
    assert retried["state"] == "completed" and retried["inferred"] and calls == [name]
    assert retried["generation"] != old["generation"]
    results.rebuild_projection(*day)
    assert results.validate_success(*day, name)["cleanup_eligible"]
    assert len(results.projection_paths(*day)[0].read_bytes().splitlines()) == 1


def test_missing_successor_fields_allow_same_generation_but_cannot_authorize_rollback(day):
    name = closed(day)
    assert process(day, name, "legacy-generation-A")["state"] == "completed"
    for path in (results.result_path(*day, name), results.attempt_path(*day, name)):
        value = json.loads(path.read_bytes())
        value.pop("supersedes", None)
        write_sealed(path, value)
    results.rebuild_projection(*day)
    assert results.validate_success(*day, name)["cleanup_eligible"]
    old = snapshot_pair(day, name)
    assert process(day, name, "new-generation-B", force=True)["state"] == "completed"
    results.rebuild_projection(*day)
    speech, receipt, _ = results.projection_paths(*day)
    published = speech.read_bytes(), receipt.read_bytes()
    restore_pair(day, name, old)
    with pytest.raises(results.ResultError):
        results.rebuild_projection(*day)
    assert (speech.read_bytes(), receipt.read_bytes()) == published
    assert not results.validate_success(*day, name)["cleanup_eligible"]


def test_legitimate_successors_do_not_depend_on_clock_or_random_generation_order(day, monkeypatch):
    clock = [4000.0]
    generations = iter(character * 32 for character in "fedcba9876543210")
    monkeypatch.setattr(results, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(results, "secrets", SimpleNamespace(token_hex=lambda _count: next(generations)))
    first, oldest = ready(day)
    second = closed(day)
    clock[0] = 3000.0
    assert process(day, second, "stable-second")["state"] == "completed"
    results.rebuild_projection(*day)
    clock[0] = 2000.0
    middle = process(day, first, "generation-2", force=True)
    results.rebuild_projection(*day)
    clock[0] = 1000.0
    newest = process(day, first, "generation-3", force=True)
    results.rebuild_projection(*day)
    assert len({oldest["generation"], middle["generation"], newest["generation"]}) == 3
    rows = [json.loads(raw) for raw in results.projection_paths(*day)[0].read_bytes().splitlines()]
    assert [row["text"] for row in rows] == ["generation-3", "stable-second"]
    assert all(results.validate_success(*day, name)["cleanup_eligible"] for name in (first, second))


def test_retry_and_pending_projection_recovery_preserve_other_segment_without_inference(day, monkeypatch):
    first, old = ready(day)
    second = closed(day)
    assert process(day, second, "other-segment")["state"] == "completed"
    results.rebuild_projection(*day)
    assert results.process_segment(*day, first, failed, "failed-force", force=True)["state"] == "failed"
    calls = []
    retried = process(day, first, "retry-current", calls=calls)
    assert retried["generation"] != old["generation"] and calls == [first]
    speech, receipt, pending = results.projection_paths(*day)
    replace = results.os.replace
    def fail_receipt(source, target):
        if Path(target) == receipt:
            raise PermissionError("generated receipt replacement failure")
        return replace(source, target)
    with monkeypatch.context() as patch:
        patch.setattr(results.os, "replace", fail_receipt)
        with pytest.raises(PermissionError):
            results.rebuild_projection(*day)
    assert pending.exists()
    reused = results.process_segment(*day, first, never, "moved-model")
    assert reused["state"] == "reused" and not reused["inferred"]
    results.rebuild_projection(*day)
    rows = [json.loads(raw) for raw in speech.read_bytes().splitlines()]
    assert [row["text"] for row in rows] == ["retry-current", "other-segment"]
    assert all(results.validate_success(*day, name)["cleanup_eligible"] for name in (first, second))
    assert not list(day[1].rglob("*.tmp"))


def test_old_pair_cannot_overwrite_a_new_generation_published_before_receipt_commit(day, monkeypatch):
    name, _ = ready(day)
    old = snapshot_pair(day, name)
    assert process(day, name, "NEW-pending-generation", force=True)["state"] == "completed"
    speech, receipt, pending = results.projection_paths(*day)
    replace = results.os.replace
    def fail_receipt(source, target):
        if Path(target) == receipt:
            raise PermissionError("generated receipt replacement failure")
        return replace(source, target)
    with monkeypatch.context() as patch:
        patch.setattr(results.os, "replace", fail_receipt)
        with pytest.raises(PermissionError):
            results.rebuild_projection(*day)
    assert pending.exists()
    published = speech.read_bytes(), receipt.read_bytes(), pending.read_bytes()
    restore_pair(day, name, old)
    with pytest.raises(results.ResultError):
        results.rebuild_projection(*day)
    assert (speech.read_bytes(), receipt.read_bytes(), pending.read_bytes()) == published
    assert not results.validate_success(*day, name)["cleanup_eligible"]
    assert not list(day[1].rglob("*.tmp"))


def test_attempt_authorization_must_match_its_committed_result(day):
    name, _ = ready(day)
    assert process(day, name, "next-generation", force=True)["state"] == "completed"
    path = results.attempt_path(*day, name)
    attempt = json.loads(path.read_bytes())
    assert attempt["supersedes"]
    attempt["supersedes"] = []
    write_sealed(path, attempt)
    speech, receipt, _ = results.projection_paths(*day)
    before = speech.read_bytes(), receipt.read_bytes()
    status = results.validate_success(*day, name)
    assert status["result_committed"] and not status["current_result"] and not status["cleanup_eligible"]
    with pytest.raises(results.ResultError):
        results.rebuild_projection(*day)
    assert (speech.read_bytes(), receipt.read_bytes()) == before


@pytest.mark.parametrize("boundary", ["before", "after"])
def test_intent_atomic_acceptance_boundary_preserves_old_result_and_cleans_temporary_files(day, monkeypatch, boundary):
    name, _ = ready(day)
    result_path, intent = results.result_path(*day, name), results.attempt_path(*day, name)
    speech, receipt, _ = results.projection_paths(*day)
    before = result_path.read_bytes(), speech.read_bytes(), receipt.read_bytes()
    replace = results.os.replace
    def fail_intent(source, target):
        if Path(target) == intent:
            if boundary == "after":
                replace(source, target)
            raise PermissionError("generated intent replacement boundary")
        return replace(source, target)
    calls = []
    with monkeypatch.context() as patch:
        patch.setattr(results.os, "replace", fail_intent)
        with pytest.raises(PermissionError):
            process(day, name, "never-inferred", force=True, calls=calls)
    assert calls == []
    assert (result_path.read_bytes(), speech.read_bytes(), receipt.read_bytes()) == before
    status = results.validate_success(*day, name)
    assert status["result_committed"]
    assert status["current_result"] is (boundary == "before")
    assert status["cleanup_eligible"] is (boundary == "before")
    assert not list(day[1].rglob("*.tmp"))
    assert (day[1] / name).exists()
