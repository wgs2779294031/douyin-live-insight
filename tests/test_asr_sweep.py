"""Synthetic closed bytes and fake recognizers: one lane's discovery and retries."""
import json
import os
from pathlib import Path
import threading
from contextlib import contextmanager

import pytest

from scripts import audio_segments as audio
from scripts import transcript_results as results
from scripts.asr_sweep import SweepEngine


class Clock:
    def __init__(self):
        self.mono, self.wall = 0.0, 1000.0
    def advance(self, seconds=1):
        self.mono += seconds
        self.wall += seconds


def closed(data, tag="A", *, size=5000):
    folder = data / f"audio_{tag}_2000-01-01"
    folder.mkdir(exist_ok=True)
    number, path = audio.reserve_segment(data, folder)
    with path.open("xb") as stream:
        stream.write(b"x" * size)
        stream.flush()
        os.fsync(stream.fileno())
    audio.publish_closed(data, folder, path, number, 100 + number, 160 + number)
    return folder, path.name


def recognized(_path):
    return [{"start": 0, "end": 1, "text": "generated utterance"}], 60


def make(data, clock, **kwargs):
    return SweepEngine(data, recognize=kwargs.pop("recognize", recognized), monotonic=lambda: clock.mono,
                       wall_time=lambda: clock.wall, scan_seconds=100, **kwargs)


def pump(engine, clock, predicate, maximum=100, advance=1):
    for _ in range(maximum):
        engine.step()
        if predicate():
            return
        clock.advance(advance)
    assert predicate(), engine.snapshot()


def test_busy_preserves_two_sources_and_no_later_offline_event_is_needed(tmp_path):
    first, second = closed(tmp_path, "A"), closed(tmp_path, "B")
    clock, calls = Clock(), []
    engine = make(tmp_path, clock, recognize=lambda path: (calls.append(path.parent.name) or recognized(path)))
    held, release = threading.Event(), threading.Event()
    def hold():
        with results.writer(tmp_path):
            held.set()
            assert release.wait(10)
    worker = threading.Thread(target=hold)
    worker.start()
    try:
        assert held.wait(2)
        engine.step()
        assert engine.snapshot()["state"] == "busy" and calls == []
        assert not any(folder.joinpath(".results").exists() for folder, _ in (first, second))
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    try:
        clock.advance(2)
        pump(engine, clock, lambda: len(calls) == 2)
        assert set(calls) == {first[0].name, second[0].name}
        assert all(results.validate_success(tmp_path, folder, name)["cleanup_eligible"] for folder, name in (first, second))
        engine.close()
        # No roster is an input, and a fresh worker sees completed facts.
        restarted = make(tmp_path, clock, recognize=lambda _: pytest.fail("completed source was inferred twice"))
        pump(restarted, clock, lambda: restarted.snapshot()["scan"]["last_complete_at"] is not None)
        restarted.close()
    finally:
        engine.close()


def test_projection_only_repairs_without_model_and_keeps_matching_pending(tmp_path):
    folder, name = closed(tmp_path)
    results.process_segment(tmp_path, folder, name, recognized, "old-location")
    original = results.result_path(tmp_path, folder, name).read_bytes()
    intent = results.attempt_path(tmp_path, folder, name).read_bytes()
    clock = Clock()
    engine = make(tmp_path, clock, recognize=lambda _: pytest.fail("projection inferred"),
                  prepare_model=lambda: pytest.fail("projection loaded model"))
    try:
        pump(engine, clock, lambda: results.validate_success(tmp_path, folder, name)["projection_ready"])
        assert results.result_path(tmp_path, folder, name).read_bytes() == original
        assert results.attempt_path(tmp_path, folder, name).read_bytes() == intent
        assert engine.snapshot()["last_completed_at"] is not None
    finally:
        engine.close()


@pytest.mark.parametrize("during", ["model", "recognition"])
def test_stop_before_attempt_or_after_accepted_unit_has_exact_piece_boundary(tmp_path, during):
    first, second = closed(tmp_path), closed(tmp_path)
    clock, stopped, calls = Clock(), {"value": False}, []
    def prepare():
        if during == "model":
            stopped["value"] = True
    def recognize(path):
        calls.append(path.name)
        stopped["value"] = True
        return recognized(path)
    engine = make(tmp_path, clock, prepare_model=prepare, recognize=recognize,
                  should_stop=lambda: stopped["value"])
    try:
        engine.step()
        assert engine.snapshot()["state"] == "stopping"
        assert not results.attempt_path(tmp_path, *second).exists()
        if during == "model":
            assert calls == [] and not results.attempt_path(tmp_path, *first).exists()
        else:
            assert calls == [first[1]]
            assert results.validate_success(tmp_path, *first)["cleanup_eligible"]
        engine.step()
        assert len(calls) <= 1
    finally:
        engine.close()


def test_bad_evidence_and_legacy_do_not_load_model_or_starve_a_good_source(tmp_path):
    broken = closed(tmp_path, "A")
    # A valid closed source with an unparseable accepted-intent file.
    intent = results.attempt_path(tmp_path, *broken)
    intent.parent.mkdir()
    intent.write_bytes(b'{"generated-truncated":')
    legacy = closed(tmp_path, "B")
    speech = tmp_path / "speech_B_2000-01-01.jsonl"
    speech.write_bytes(b'{"unknown-generated-legacy":true}\n')
    small = closed(tmp_path, "C", size=100)
    good = closed(tmp_path, "D")
    clock, calls, loads = Clock(), [], []
    engine = make(tmp_path, clock, prepare_model=lambda: loads.append(1),
                  recognize=lambda path: (calls.append(path.parent.name) or recognized(path)))
    try:
        pump(engine, clock, lambda: results.validate_success(tmp_path, *good)["cleanup_eligible"])
        assert calls == [good[0].name] and loads == [1]
        pump(engine, clock, lambda: engine.snapshot()["scan"]["last_complete_at"] is not None)
        assert engine.snapshot()["counts"]["blocked"] >= 3
        assert intent.read_bytes() == b'{"generated-truncated":'
        assert speech.read_bytes() == b'{"unknown-generated-legacy":true}\n'
        assert all((folder / name).exists() for folder, name in (broken, legacy, small, good))
    finally:
        engine.close()


def test_finite_retry_survives_restart_and_wall_clock_jumps(tmp_path):
    folder, name = closed(tmp_path)
    clock, calls = Clock(), []
    def fail(path):
        calls.append(path.name)
        raise ValueError("generated decode failure")
    engine = make(tmp_path, clock, recognize=fail)
    pump(engine, clock, lambda: len(calls) == 1)
    engine.close()
    clock.wall = 100  # Rollback: restart wait is bounded by the saved 30s.
    engine = make(tmp_path, clock, recognize=fail)
    try:
        for _ in range(10):
            engine.step()
            clock.wall += 100000
        assert len(calls) == 1
        clock.advance(30)
        pump(engine, clock, lambda: len(calls) == 2)
        engine.close()
        clock.wall += 10 ** 8  # Forward jump permits at most the remaining budget.
        engine = make(tmp_path, clock, recognize=fail)
        pump(engine, clock, lambda: len(calls) == 3)
        for _ in range(50):
            clock.advance(1000)
            engine.step()
        assert len(calls) == 3
        assert engine.snapshot()["next_condition"] == "explicit_retry_or_dependency_change"
        assert (folder / name).exists()
        assert engine.retry()
        pump(engine, clock, lambda: len(calls) == 4)
    finally:
        engine.close()


def test_global_model_failure_has_one_budget_and_projection_still_completes(tmp_path):
    sources = [closed(tmp_path, "A"), closed(tmp_path, "B")]
    projected = closed(tmp_path, "C")
    results.process_segment(tmp_path, *projected, recognized, "generated")
    clock, loads = Clock(), []
    def missing():
        loads.append(1)
        raise ValueError("generated missing model private detail")
    engine = make(tmp_path, clock, prepare_model=missing, recognize=lambda _: pytest.fail("no model"))
    try:
        pump(engine, clock, lambda: results.validate_success(tmp_path, *projected)["projection_ready"])
        for _ in range(100):
            clock.advance(30)
            engine.step()
        assert len(loads) == 3
        assert all(not results.attempt_path(tmp_path, *source).exists() for source in sources)
        assert engine.snapshot()["next_condition"] == "explicit_retry_or_dependency_change"
        raw = engine.path.read_text(encoding="utf-8")
        assert "private detail" not in raw
    finally:
        engine.close()


def test_changing_dependency_files_does_not_bypass_existing_global_cooldown(tmp_path):
    closed(tmp_path, "A")
    closed(tmp_path, "B")
    clock, loads, version = Clock(), [], [0]
    def missing():
        loads.append(version[0])
        raise ValueError("generated unavailable model")
    engine = make(tmp_path, clock, prepare_model=missing, dependency_token=lambda: version[0])
    try:
        pump(engine, clock, lambda: len(loads) == 1)
        for _ in range(20):
            version[0] += 1
            clock.advance(.2)
            engine.step()
        assert len(loads) == 1
        clock.advance(30)
        pump(engine, clock, lambda: len(loads) == 2)
        assert all(not (folder / ".results").exists() for folder in tmp_path.glob("audio_*"))
    finally:
        engine.close()


def test_bad_retry_metadata_is_retained_across_restart_and_explicit_retry(tmp_path):
    source = closed(tmp_path)
    path = tmp_path / ".local_app/asr/retry.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"bad-generated-state":')
    clock = Clock()
    for _ in range(2):
        engine = make(tmp_path, clock, prepare_model=lambda: pytest.fail("bad metadata loaded model"))
        try:
            engine.step()
            assert engine.snapshot()["error"] == "retry_state_unavailable"
            assert not engine.retry()
            assert path.read_bytes() == b'{"bad-generated-state":'
            assert not results.attempt_path(tmp_path, *source).exists()
        finally:
            engine.close()


def test_round_robin_and_bounded_summary_do_not_hide_partial_scan(tmp_path):
    for _ in range(6):
        closed(tmp_path, "A", size=100)
    good = closed(tmp_path, "B")
    clock, calls = Clock(), []
    engine = make(tmp_path, clock, scan_items=1, day_cursors=2, sample_limit=2,
                  recognize=lambda path: (calls.append(path.parent.name) or recognized(path)))
    try:
        pump(engine, clock, lambda: calls, maximum=6)
        assert calls == [good[0].name]
        pump(engine, clock, lambda: engine.snapshot()["scan"]["last_complete_at"] is not None)
        snapshot = engine.snapshot()
        assert snapshot["scan"]["partial"] and snapshot["scan"]["limited"]
        assert snapshot["scan"]["counts_scope"] == "recent_window"
        assert sum(snapshot["counts"].values()) <= 2
    finally:
        engine.close()


def test_one_open_day_cursor_does_not_hold_back_the_next_long_directory(tmp_path):
    for _ in range(20):
        closed(tmp_path, "A", size=100)
    good = closed(tmp_path, "B")
    clock, calls = Clock(), []
    engine = make(tmp_path, clock, scan_items=1, day_cursors=1,
                  recognize=lambda path: (calls.append(path.parent.name) or recognized(path)))
    try:
        pump(engine, clock, lambda: calls, maximum=6)
        assert calls == [good[0].name]
        assert engine.snapshot()["scan"]["partial"]
    finally:
        engine.close()


def test_cursor_replay_exact_budget_boundary_still_reaches_later_sources(tmp_path):
    first, second = closed(tmp_path, "A"), closed(tmp_path, "B")
    for index in range(60):
        (first[0] / ".segments" / f"readme-{index:03d}.txt").write_bytes(b"generated unrelated sidecar")
    clock, calls = Clock(), []
    engine = make(tmp_path, clock, scan_items=1, day_cursors=1,
                  recognize=lambda path: (calls.append(path.parent.name) or recognized(path)))
    try:
        pump(engine, clock, lambda: len(calls) == 2, maximum=30)
        assert set(calls) == {first[0].name, second[0].name}
        assert all(results.validate_success(tmp_path, *source)["cleanup_eligible"] for source in (first, second))
    finally:
        engine.close()


def test_unchanged_completed_source_is_not_rehashed_each_scan(tmp_path, monkeypatch):
    source = closed(tmp_path)
    results.process_segment(tmp_path, *source, recognized, "generated")
    results.rebuild_projection(tmp_path, source[0])
    clock, fingerprints = Clock(), []
    original = audio.fingerprint
    def fingerprint(path):
        fingerprints.append(path)
        return original(path)
    monkeypatch.setattr(audio, "fingerprint", fingerprint)
    engine = make(tmp_path, clock, recognize=lambda _: pytest.fail("completed source rerun"))
    try:
        pump(engine, clock, lambda: engine.snapshot()["scan"]["last_complete_at"] is not None)
        first = len(fingerprints)
        assert first > 0
        for _ in range(20):
            clock.advance(10)
            engine.step()
        assert len(fingerprints) == first
    finally:
        engine.close()


def test_checking_phase_is_published_before_expensive_source_verification(tmp_path, monkeypatch):
    source = closed(tmp_path)
    clock, summaries, seen = Clock(), [], []
    original = audio.verify_closed
    def verify(*args, **kwargs):
        if not seen:
            assert summaries and summaries[-1]["current"]["phase"] == "checking"
            assert summaries[-1]["state"] == "processing"
            seen.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(audio, "verify_closed", verify)
    engine = make(tmp_path, clock, publish=summaries.append, should_stop=lambda: False)
    try:
        engine.step()
        assert seen
        assert results.validate_success(tmp_path, *source)["cleanup_eligible"]
    finally:
        engine.close()


def test_retry_storage_failure_stops_before_fake_recognition_and_releases_writer(tmp_path, monkeypatch):
    source = closed(tmp_path)
    clock, calls = Clock(), []
    engine = make(tmp_path, clock, recognize=lambda path: (calls.append(path.name) or recognized(path)))
    original = audio.atomic_json
    def fail_retry(path, value):
        if Path(path) == engine.path:
            raise PermissionError("generated retry-state write failure")
        return original(path, value)
    monkeypatch.setattr(audio, "atomic_json", fail_retry)
    try:
        engine.step()
        assert calls == []
        assert engine.snapshot()["error"] == "retry_state_unavailable"
        with results.writer(tmp_path):
            pass
        assert (source[0] / source[1]).exists()
        assert not list(engine.path.parent.glob("*.tmp"))
    finally:
        engine.close()


def test_replaced_physical_data_scope_stops_before_discovery_or_model(tmp_path):
    data, retained = tmp_path / "data", tmp_path / "retained-data"
    data.mkdir()
    source = closed(data)
    clock = Clock()
    engine = make(data, clock, prepare_model=lambda: pytest.fail("changed scope loaded a model"))
    # Both resolved move targets are explicitly inside this generated test root.
    assert data.resolve().is_relative_to(tmp_path.resolve())
    assert retained.resolve().is_relative_to(tmp_path.resolve())
    data.rename(retained)
    data.mkdir()
    try:
        engine.step()
        assert engine.snapshot()["state"] == "identity_unknown"
        assert engine.snapshot()["error"] == "admission_unavailable"
        assert (retained / source[0].name / source[1]).exists()
        assert not any(data.iterdir())
    finally:
        engine.close()


def test_scope_changed_during_recognition_never_writes_retry_into_new_root(tmp_path, monkeypatch):
    data, retained = tmp_path / "data", tmp_path / "retained-data"
    data.mkdir()
    source = closed(data)
    clock = Clock()
    @contextmanager
    def fault_writer(_data):
        # This filesystem denies renaming a directory while its real writer
        # sidecar is held. Isolate the namespace fault from that OS protection;
        # separate tests above exercise the actual writer and busy contract.
        yield
    monkeypatch.setattr(results, "writer", fault_writer)
    def recognize(path):
        assert path == source[0] / source[1]
        assert data.resolve().is_relative_to(tmp_path.resolve())
        assert retained.resolve().is_relative_to(tmp_path.resolve())
        engine.close()  # Release only this test's directory enumeration handles.
        data.rename(retained)
        data.mkdir()
        return recognized(path)
    engine = make(data, clock, recognize=recognize)
    try:
        engine.step()
        assert engine.snapshot()["state"] == "identity_unknown"
        assert engine.snapshot()["error"] == "admission_unavailable"
        assert (retained / source[0].name / source[1]).exists()
        assert not any(data.iterdir()), "old-scope scheduler wrote to replacement data"
        assert not engine.retry()
        assert not any(data.iterdir())
    finally:
        engine.close()
