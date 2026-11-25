"""8B scheduler: only newly generated task bytes, fake ASR and owned native calls."""
import copy
import os
import threading

import pytest

from scripts import audio_cleanup as cleanup
from scripts import audio_segments as audio
from scripts import transcript_results as results
from scripts.asr_sweep import SweepEngine, _sealed

pytestmark = pytest.mark.skipif(os.name != "nt", reason="these generated cleanup fixtures use the accepted Windows primitive")
PAYLOAD = b"generated-8B-source-only" * 256


class Clock:
    def __init__(self):
        self.mono, self.wall = 0.0, 1000.0

    def advance(self, seconds=1):
        self.mono += seconds
        self.wall += seconds


class Storage:
    """API observer only; it never invents facts or performs filesystem work."""
    def __init__(self):
        self.rounds, self.observations, self.outcomes = [], [], []

    def begin_scan(self):
        self.rounds.append({"items": [], "finished": False})

    def observe(self, folder, name, status, *, cleanup_attempts=0, cleanup_exhausted=False):
        item = (folder, name, copy.deepcopy(status), cleanup_attempts, cleanup_exhausted)
        self.observations.append(item)
        self.rounds[-1]["items"].append(item)

    def finish_scan(self, *, complete=True, limited=False):
        self.rounds[-1].update(finished=True, complete=complete, limited=limited)

    def record_cleanup(self, folder, name, outcome):
        self.outcomes.append((folder, name, dict(outcome)))

    def snapshot(self):
        return {"schema": 1, "observed": len(self.observations), "cleanup_results": len(self.outcomes)}


def forbidden(*_args, **_kwargs):
    pytest.fail("unexpected model, ASR or cleanup call")


def recognized(_path):
    return [{"start": 0, "end": 1, "text": "generated 8B transcript"}], 60


def closed(data, tag="A"):
    folder = data / ("audio_%s_2000-01-01" % tag)
    folder.mkdir(exist_ok=True)
    number, source = audio.reserve_segment(data, folder)
    with source.open("xb") as stream:
        stream.write(PAYLOAD)
        stream.flush()
        os.fsync(stream.fileno())
    audio.publish_closed(data, folder, source, number, 100 + number, 160 + number)
    return folder, source


def committed(data, tag="A"):
    folder, source = closed(data, tag)
    assert results.process_segment(data, folder, source.name, recognized, "fake-local-model")["completed_now"]
    assert results.rebuild_projection(data, folder)["projection_ready"]
    return folder, source


def text_bytes(data, folder, source):
    speech, receipt, _ = results.projection_paths(data, folder)
    return {path: path.read_bytes() for path in (results.result_path(data, folder, source.name),
        results.attempt_path(data, folder, source.name), folder / ".segments" / (source.stem + ".json"), speech, receipt)}


def make(data, clock, **kwargs):
    return SweepEngine(data, recognize=kwargs.pop("recognize", forbidden),
        prepare_model=kwargs.pop("prepare_model", forbidden), monotonic=lambda: clock.mono,
        wall_time=lambda: clock.wall, scan_seconds=100, cleanup_enabled=kwargs.pop("cleanup_enabled", True), **kwargs)


def pump(lane, clock, predicate, *, maximum=100, advance=1):
    for _ in range(maximum):
        lane.step()
        if predicate():
            return
        clock.advance(advance)
    assert predicate(), lane.snapshot()


def state(lane):
    return audio.read_json(lane.path)


@pytest.mark.parametrize("enabled", [False, True], ids=["default-off", "customer-enabled"])
def test_processing_then_cleanup_uses_one_piece_and_final_observation(tmp_path, enabled):
    folder, source = closed(tmp_path)
    clock, storage, loads, calls = Clock(), Storage(), [], []
    options = dict(recognize=lambda path: calls.append(path.name) or recognized(path),
        prepare_model=lambda: loads.append(1), monotonic=lambda: clock.mono, wall_time=lambda: clock.wall,
        storage=storage, scan_seconds=100)
    if enabled:
        options["cleanup_enabled"] = True
    lane = SweepEngine(tmp_path, **options)
    try:
        pump(lane, clock, lambda: lane.snapshot()["counts"]["complete"] == 1)
        assert calls == [source.name] and loads == [1]
        assert source.exists() is (not enabled)
        facts = results.validate_success(tmp_path, folder, source.name)
        assert facts["current_result"] and facts["projection_ready"] and facts["history_usable"]
        assert facts["audio_cleaned"] is enabled
        assert results.attempt_path(tmp_path, folder, source.name).exists()
        assert b"generated 8B transcript" in results.result_path(tmp_path, folder, source.name).read_bytes()
        assert len(storage.observations) == 1
        observed = storage.observations[0]
        assert observed[2]["asr_category"] == "complete" and observed[2]["audio_cleaned"] is enabled
        assert len(storage.outcomes) == int(enabled)
        assert state(lane)["schema"] == 2 and state(lane)["cleanup"] == {}
        assert state(lane)["model"]["attempts"] == 0
        assert not list(tmp_path.rglob("*.tmp"))
    finally:
        lane.close()


def test_existing_success_waits_for_writer_then_cleans_two_sources_without_model(tmp_path, monkeypatch):
    sources = [committed(tmp_path, tag) for tag in ("A", "B")]
    text = {path: value for folder, source in sources for path, value in text_bytes(tmp_path, folder, source).items()}
    calls, storage, clock = [], Storage(), Clock()
    native = cleanup.cleanup_segment
    monkeypatch.setattr(cleanup, "cleanup_segment", lambda *args: calls.append(args[2]) or native(*args))
    lane = make(tmp_path, clock, storage=storage)
    entered, release = threading.Event(), threading.Event()
    def hold():
        with results.writer(tmp_path):
            entered.set()
            assert release.wait(10)
    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert entered.wait(2)
        lane.step()
        assert lane.snapshot()["state"] == "busy" and calls == []
        assert lane._cleanup_entries == {} and not lane.path.exists()
        assert all(source.read_bytes() == PAYLOAD for _, source in sources)
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()
    try:
        clock.advance(2)
        pump(lane, clock, lambda: all(not source.exists() for _, source in sources), advance=2)
        assert len(calls) == 2 and state(lane)["cleanup"] == {}
        assert state(lane)["model"]["attempts"] == state(lane)["model"]["lifetime"] == 0
        assert all(path.read_bytes() == value for path, value in text.items())
        for round_ in storage.rounds:
            keys = [(str(item[0]), item[1]) for item in round_["items"]]
            assert len(keys) == len(set(keys))
    finally:
        lane.close()


def test_pending_absent_is_settled_without_native_mark_or_model(tmp_path, monkeypatch):
    folder, source = committed(tmp_path)
    text = text_bytes(tmp_path, folder, source)
    with monkeypatch.context() as fault:
        fault.setattr(cleanup, "_settle", lambda *_args: (_ for _ in ()).throw(PermissionError("generated post-delete interruption")))
        assert cleanup.cleanup_segment(tmp_path, folder, source.name)["state"] == "pending"
    operation = cleanup.operation_path(tmp_path, folder, source.name)
    assert not source.exists() and audio.read_json(operation)["state"] == "pending"
    monkeypatch.setattr(cleanup, "_set_disposition", forbidden)
    clock = Clock()
    lane = make(tmp_path, clock)
    try:
        pump(lane, clock, lambda: audio.read_json(operation)["state"] == "deleted")
        assert state(lane)["cleanup"] == {} and lane.snapshot()["counts"]["complete"] == 1
        assert all(path.read_bytes() == value for path, value in text.items())
    finally:
        lane.close()


def test_cleanup_budget_survives_restart_own_metadata_and_does_not_starve_new_asr(tmp_path, monkeypatch):
    folder, source = committed(tmp_path, "A")
    canonical = results.result_path(tmp_path, folder, source.name)
    canonical_bytes = canonical.read_bytes()
    clock, storage, attempts, asr, loads = Clock(), Storage(), [], [], []
    native, mark = cleanup.cleanup_segment, cleanup._set_disposition
    def fail_a(guard):
        if guard.path.parent == folder:
            raise PermissionError("generated source A deletion failure")
        mark(guard)
    def call(data, day, name):
        attempts.append((day.name, clock.wall))
        return native(data, day, name)
    monkeypatch.setattr(cleanup, "_set_disposition", fail_a)
    monkeypatch.setattr(cleanup, "cleanup_segment", call)
    options = dict(storage=storage, prepare_model=lambda: loads.append(1),
                   recognize=lambda path: asr.append(path.parent.name) or recognized(path))
    lane = make(tmp_path, clock, **options)
    lane.step()
    assert len(attempts) == 1 and lane.snapshot()["error"] == ""
    first_key = next(iter(state(lane)["cleanup"]))
    operation = cleanup.operation_path(tmp_path, folder, source.name)
    pending_bytes = operation.read_bytes()
    lane.close()
    for path in (operation, canonical):
        info = path.stat()
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))
    lane = make(tmp_path, clock, **options)
    try:
        lane.step()
        assert len(attempts) == 1 and state(lane)["cleanup"][first_key]["attempts"] == 1
        fresh, fresh_source = closed(tmp_path, "B")
        clock.advance(10)
        pump(lane, clock, lambda: not fresh_source.exists())
        assert asr == [fresh.name] and loads == [1]
        assert source.exists() and lane.snapshot()["error"] == ""
        for expected, delay in ((2, 30), (3, 120)):
            clock.advance(delay)
            pump(lane, clock, lambda: sum(day == folder.name for day, _ in attempts) == expected)
            assert state(lane)["cleanup"][first_key]["attempts"] == expected
            lane.close()
            lane = make(tmp_path, clock, **options)
            lane.step()
            assert sum(day == folder.name for day, _ in attempts) == expected
        clock.advance(10000)
        for _ in range(15):
            lane.step()
            clock.advance(10)
        assert sum(day == folder.name for day, _ in attempts) == 3
        assert source.read_bytes() == PAYLOAD and canonical.read_bytes() == canonical_bytes
        assert operation.read_bytes() == pending_bytes and lane.snapshot()["error"] == ""
        assert any(item[3] == 3 and item[4] for item in storage.observations)
        assert lane.retry()
        pump(lane, clock, lambda: sum(day == folder.name for day, _ in attempts) == 4)
        assert state(lane)["cleanup"][first_key]["attempts"] == 1
        assert asr == [fresh.name]
    finally:
        lane.close()


def test_interrupted_cleanup_has_durable_budget_before_callback_and_restart_cooldown(tmp_path, monkeypatch):
    folder, source = committed(tmp_path)
    clock, calls = Clock(), []
    def abort(*_args):
        value = audio.read_json(tmp_path / ".local_app/asr/retry.json")
        assert len(value["cleanup"]) == 1
        record = next(iter(value["cleanup"].values()))
        assert record["attempts"] == record["lifetime"] == 1
        assert record["wait_seconds"] == 30 and record["next_at"] == 1030
        assert value["entries"] == {} and value["model"]["attempts"] == 0
        calls.append(1)
        raise SystemExit("generated worker exit at cleanup boundary")
    monkeypatch.setattr(cleanup, "cleanup_segment", abort)
    lane = make(tmp_path, clock)
    try:
        with pytest.raises(SystemExit):
            lane.step()
    finally:
        lane.close()
    lane = make(tmp_path, clock)
    try:
        lane.step()
        assert calls == [1] and source.read_bytes() == PAYLOAD
        with results.writer(tmp_path):
            pass
    finally:
        lane.close()


@pytest.mark.parametrize("committed_write", [False, True])
def test_unconfirmed_cleanup_budget_never_calls_primitive(tmp_path, monkeypatch, committed_write):
    folder, source = committed(tmp_path)
    clock = Clock()
    lane = make(tmp_path, clock)
    original = audio.atomic_json
    def fail(path, value):
        if path == lane.path:
            if committed_write:
                original(path, value)
            raise PermissionError("generated cleanup budget confirmation failure")
        return original(path, value)
    monkeypatch.setattr(audio, "atomic_json", fail)
    monkeypatch.setattr(cleanup, "cleanup_segment", forbidden)
    try:
        lane.step()
        assert lane.snapshot()["error"] == "retry_state_unavailable"
        assert source.read_bytes() == PAYLOAD
        assert not cleanup.operation_path(tmp_path, folder, source.name).exists()
        if committed_write:
            assert next(iter(state(lane)["cleanup"].values()))["attempts"] == 1
        else:
            assert not lane.path.exists()
        with results.writer(tmp_path):
            pass
    finally:
        lane.close()


@pytest.mark.parametrize("stop", [True, None], ids=["stopping", "unconfirmed-admission"])
def test_stop_or_unknown_admission_before_scan_accepts_no_cleanup(tmp_path, monkeypatch, stop):
    folder, source = committed(tmp_path)
    monkeypatch.setattr(cleanup, "cleanup_segment", forbidden)
    lane = make(tmp_path, Clock(), should_stop=lambda: stop)
    try:
        lane.step()
        assert source.read_bytes() == PAYLOAD and not lane.path.exists()
        assert not cleanup.operation_path(tmp_path, folder, source.name).exists()
    finally:
        lane.close()


def test_stop_at_cleaning_stage_has_no_native_call_or_budget_acceptance(tmp_path, monkeypatch):
    _folder, source = committed(tmp_path)
    stopped = [False]
    def publish(summary):
        if (summary.get("current") or {}).get("phase") == "cleaning":
            stopped[0] = True
    monkeypatch.setattr(cleanup, "cleanup_segment", forbidden)
    lane = make(tmp_path, Clock(), should_stop=lambda: stopped[0], publish=publish)
    try:
        lane.step()
        assert lane.snapshot()["state"] == "stopping" and source.read_bytes() == PAYLOAD
        assert lane._cleanup_entries == {} and not lane.path.exists()
    finally:
        lane.close()


def test_stop_during_accepted_recognition_finishes_text_but_defers_cleanup(tmp_path, monkeypatch):
    folder, source = closed(tmp_path)
    stopped = [False]
    def recognize(path):
        stopped[0] = True
        return recognized(path)
    monkeypatch.setattr(cleanup, "cleanup_segment", forbidden)
    lane = make(tmp_path, Clock(), recognize=recognize, prepare_model=lambda: None, should_stop=lambda: stopped[0])
    try:
        lane.step()
        assert lane.snapshot()["state"] == "stopping"
        assert results.validate_success(tmp_path, folder, source.name)["cleanup_eligible"]
        assert source.read_bytes() == PAYLOAD and state(lane)["cleanup"] == {}
    finally:
        lane.close()


def test_accepted_cleanup_finishes_naturally_but_no_next_piece_starts(tmp_path, monkeypatch):
    first, second = committed(tmp_path, "A"), committed(tmp_path, "B")
    stopped, calls = [False], []
    native = cleanup.cleanup_segment
    def run(*args):
        calls.append(args[1].name)
        stopped[0] = True
        return native(*args)
    monkeypatch.setattr(cleanup, "cleanup_segment", run)
    lane = make(tmp_path, Clock(), should_stop=lambda: stopped[0])
    try:
        lane.step()
        lane.step()
        assert calls == [first[0].name] and not first[1].exists() and second[1].read_bytes() == PAYLOAD
        assert lane.snapshot()["state"] == "stopping" and state(lane)["cleanup"] == {}
    finally:
        lane.close()


@pytest.mark.parametrize("refund_unconfirmed", [False, True])
def test_primitive_busy_refunds_budget_or_fails_closed_on_uncertain_refund(tmp_path, monkeypatch, refund_unconfirmed):
    _folder, source = committed(tmp_path)
    calls, writes, clock, storage = [], [], Clock(), Storage()
    monkeypatch.setattr(cleanup, "cleanup_segment", lambda *_args: calls.append(1) or {"state": "busy", "reason": "busy"})
    lane = make(tmp_path, clock, storage=storage)
    original = audio.atomic_json
    def save(path, value):
        if path == lane.path:
            writes.append(1)
            if refund_unconfirmed and len(writes) == 2:
                raise PermissionError("generated busy refund uncertainty")
        return original(path, value)
    monkeypatch.setattr(audio, "atomic_json", save)
    try:
        lane.step()
        assert calls == [1] and source.read_bytes() == PAYLOAD
        if refund_unconfirmed:
            assert lane.snapshot()["error"] == "retry_state_unavailable"
            assert next(iter(state(lane)["cleanup"].values()))["attempts"] == 1
        else:
            assert state(lane)["cleanup"] == {} and lane.snapshot()["error"] == ""
            assert storage.outcomes[-1][2]["state"] == "busy"
    finally:
        lane.close()


def test_schema1_migrates_and_model_exhaustion_does_not_block_old_cleanup(tmp_path):
    _folder, source = committed(tmp_path)
    clock = Clock()
    lane = make(tmp_path, clock)
    old_model = dict(lane._model, attempts=3, lifetime=3, wait_seconds=300, next_at=1300, last_error="model_unavailable")
    legacy = {"schema": 1, "kind": "asr_retry", "scope": lane.scope, "saved_at": clock.wall,
              "entries": {}, "model": old_model, "last_completed_at": None}
    lane.path.parent.mkdir(parents=True)
    audio.atomic_json(lane.path, _sealed(legacy))
    lane.close()
    lane = make(tmp_path, clock)
    try:
        lane.step()
        assert not source.exists()
        assert state(lane)["schema"] == 2 and state(lane)["cleanup"] == {}
        assert state(lane)["model"] == old_model
    finally:
        lane.close()


@pytest.mark.parametrize("bad", ["missing", "not-object", "wrong-identity-key"])
def test_malformed_schema2_cleanup_budget_is_retained_and_never_cleans(tmp_path, monkeypatch, bad):
    _folder, source = committed(tmp_path)
    lane = make(tmp_path, Clock())
    value = {"schema": 2, "kind": "asr_retry", "scope": lane.scope, "saved_at": 1000,
             "entries": {}, "model": lane._model, "last_completed_at": None}
    if bad != "missing":
        value["cleanup"] = [] if bad == "not-object" else {"invalid-generated-key": {}}
    lane.path.parent.mkdir(parents=True)
    audio.atomic_json(lane.path, _sealed(value))
    original = lane.path.read_bytes()
    lane.close()
    monkeypatch.setattr(cleanup, "cleanup_segment", forbidden)
    lane = make(tmp_path, Clock())
    try:
        lane.step()
        assert lane.snapshot()["error"] == "retry_state_unavailable" and not lane.retry()
        assert lane.path.read_bytes() == original and source.read_bytes() == PAYLOAD
    finally:
        lane.close()


def test_bad_and_incomplete_sources_are_retained_without_cleanup(tmp_path, monkeypatch):
    sources = []
    unclosed = tmp_path / "audio_unclosed_2000-01-01"
    unclosed.mkdir()
    (unclosed / "seg_0001.flv").write_bytes(PAYLOAD)
    sources.append(unclosed / "seg_0001.flv")
    _day, source = closed(tmp_path, "untranscribed")
    sources.append(source)
    day, source = closed(tmp_path, "failed")
    def fail(_path):
        raise RuntimeError("generated ASR failure")
    assert results.process_segment(tmp_path, day, source.name, fail, "fake")["state"] == "failed"
    sources.append(source)
    day, source = committed(tmp_path, "bad_operation")
    operation = cleanup.operation_path(tmp_path, day, source.name)
    operation.parent.mkdir()
    operation.write_bytes(b'{"generated-bad-cleanup":')
    sources.append(source)
    day, source = committed(tmp_path, "bad_projection")
    results.projection_paths(tmp_path, day)[1].write_bytes(b'{"generated-bad-receipt":')
    sources.append(source)
    day, source = committed(tmp_path, "replaced")
    retained = tmp_path / "retained-generated-original.flv"
    assert source.resolve().is_relative_to(tmp_path.resolve()) and retained.resolve().is_relative_to(tmp_path.resolve())
    source.rename(retained)
    source.write_bytes(b"replacement must remain")
    sources.append(source)
    before = {path: path.read_bytes() for path in sources}
    def missing_model():
        raise ValueError("generated unavailable model")
    monkeypatch.setattr(cleanup, "cleanup_segment", forbidden)
    clock = Clock()
    lane = make(tmp_path, clock, prepare_model=missing_model)
    try:
        pump(lane, clock, lambda: lane.snapshot()["scan"]["last_complete_at"] is not None)
        assert all(path.read_bytes() == value for path, value in before.items())
        assert retained.read_bytes() == PAYLOAD and operation.read_bytes() == b'{"generated-bad-cleanup":'
        assert lane._cleanup_entries == {}
    finally:
        lane.close()


def test_storage_observes_every_final_candidate_once_beyond_asr_sample_limit(tmp_path, monkeypatch):
    sources = [committed(tmp_path, tag) for tag in ("A", "B")]
    monkeypatch.setattr(cleanup, "cleanup_segment", forbidden)
    clock, storage = Clock(), Storage()
    lane = make(tmp_path, clock, cleanup_enabled=False, storage=storage, sample_limit=1, scan_items=1)
    try:
        pump(lane, clock, lambda: storage.rounds and storage.rounds[-1]["finished"])
        round_ = storage.rounds[-1]
        assert round_["complete"] and not round_["limited"] and len(round_["items"]) == 2
        assert len({(item[0], item[1]) for item in round_["items"]}) == 2
        assert all(item[2]["current_result"] and item[2]["projection_ready"] for item in round_["items"])
        assert lane.snapshot()["scan"]["limited"] and sum(lane.snapshot()["counts"].values()) == 1
        assert all(source.read_bytes() == PAYLOAD for _, source in sources)
    finally:
        lane.close()


def test_observer_exception_cannot_bypass_native_writer_or_scan_handle_close(tmp_path):
    folder, source = committed(tmp_path)
    class BrokenStorage(Storage):
        def record_cleanup(self, *_args):
            raise RuntimeError("generated observer result failure")
        def finish_scan(self, **_kwargs):
            raise RuntimeError("generated observer finish failure")
    lane = make(tmp_path, Clock(), storage=BrokenStorage())
    try:
        with pytest.raises(RuntimeError, match="observer result"):
            lane.step()
        assert not source.exists() and results.validate_success(tmp_path, folder, source.name)["history_usable"]
        with results.writer(tmp_path):
            pass
    finally:
        with pytest.raises(RuntimeError, match="observer finish"):
            lane.close()
    assert lane._root is None and not lane._days and not lane._waiting


def test_cleanup_capacity_blocks_only_cleanup_while_new_asr_can_complete(tmp_path, monkeypatch):
    first, second = committed(tmp_path, "A"), closed(tmp_path, "B")
    calls, asr, storage, clock = [], [], Storage(), Clock()
    monkeypatch.setattr(cleanup, "cleanup_segment", lambda _data, folder, _name:
        calls.append(folder.name) or {"state": "pending", "reason": "cleanup_delete_failed"})
    lane = make(tmp_path, clock, storage=storage, retry_limit=1, prepare_model=lambda: None,
                recognize=lambda path: asr.append(path.parent.name) or recognized(path))
    try:
        pump(lane, clock, lambda: bool(asr))
        assert calls == [first[0].name] and asr == [second[0].name]
        assert results.validate_success(tmp_path, second[0], second[1].name)["cleanup_eligible"]
        assert first[1].read_bytes() == second[1].read_bytes() == PAYLOAD
        assert len(state(lane)["cleanup"]) == 1 and lane.snapshot()["error"] == ""
        assert storage.outcomes[-1][2]["reason"] == "cleanup_capacity"
        assert storage.observations[-1][2]["cleanup_schedule_reason"] == "cleanup_capacity"
    finally:
        lane.close()


@pytest.fixture(autouse=True)
def require_owned_task_08b_directory(tmp_path):
    # Pure appended guard: existing behavioral assertions above are unchanged.
    from pathlib import Path
    allowed = Path(os.environ.get("DLI_TEST_ROOT", str(Path(__file__).resolve().parents[1] / ".test-state"))).resolve()
    assert tmp_path.resolve().is_relative_to(allowed), "cleanup fixtures require the explicit task-08B runner"
