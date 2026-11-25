"""Own bounded threads; no reentrant dict callbacks, native process or audio."""
import copy
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
import local_asr as lane_api
from scripts import asr_protocol as protocol


class ThreadGroup:
    def __init__(self):
        self.threads, self.releases, self.errors, self.results = [], [], [], {}
    def start(self, name, callback):
        done = threading.Event()
        def run():
            try:
                self.results[name] = callback()
            except BaseException as error:
                self.errors.append((name, error))
            finally:
                done.set()
        thread = threading.Thread(target=run, name=name)
        self.threads.append(thread)
        thread.start()
        return done
    def close(self):
        for event in self.releases:
            event.set()
        for thread in self.threads:
            thread.join(4)
        assert all(not thread.is_alive() for thread in self.threads), "own thread did not join"
        if self.errors:
            name, error = self.errors[0]
            raise AssertionError("own thread failed: " + name) from error


@contextmanager
def own_threads():
    group = ThreadGroup()
    try:
        yield group
    finally:
        group.close()


class ObservedLock:
    """Real independent mutex with a bounded test-only acquire and attempt hook."""
    def __init__(self, raw, attempted):
        self.raw, self.attempted = raw, attempted
    def __enter__(self):
        if threading.current_thread().name == "wm-new":
            self.attempted.set()
        assert self.raw.acquire(timeout=3), "watermark mutex acquire timed out"
        return self
    def __exit__(self, *_args):
        self.raw.release()
    def acquire(self, *args, **kwargs):
        return self.raw.acquire(*args, **kwargs)
    def release(self):
        self.raw.release()
    def locked(self):
        return self.raw.locked()


def sample(stamp):
    return {"schema": 1, "snapshot_at": stamp, "audio_bytes": 5, "audio_files": 1,
        "transcription_pending": 0, "cleanup_pending": 0, "cleanup_failed": 0, "cleanup_blocked": 0,
        "scan": {"partial": False, "limited": False, "checked": 1, "started_at": stamp,
                 "updated_at": stamp, "last_complete_at": stamp, "approximate": True, "counts_scope": "scan_round"},
        "disk": {"free_bytes": 2 ** 31, "total_bytes": 2 ** 40, "sampled_at": stamp, "error": ""},
        "last_cleanup": None}


@pytest.fixture
def watermark_case(tmp_path):
    data = tmp_path / "new-data"
    data.mkdir()
    wall, mono, record = [2000.], [100.], [None]
    context = {"data": str(data), "root": str(ROOT), "python": sys.executable, "instance_id": "watermark-threads-test"}
    api = SimpleNamespace(paths=protocol.paths, ensure=protocol.ensure, envelope=protocol.envelope,
        read_record=lambda _path: copy.deepcopy(record[0]), matches=protocol.matches)
    manager = SimpleNamespace(context=context, stopping=False, now=lambda: mono[0])
    lane = lane_api.AsrLane(manager, wall_time=lambda: wall[0], protocol_api=api, boot_identity=lambda: None)
    lane.nonce = "test-current-generation"
    lane.parent, lane.worker = {"state": "alive", "pid": 900001, "created": 1.}, {"state": "alive", "pid": 900002, "created": 2.}
    lane._reset_storage()
    lane.state, lane.admitted, lane.owned = "running", True, object()
    observations = []
    def receive(sequence, stamp, *, storage_stamp=None):
        raw = sample(stamp if storage_stamp is None else storage_stamp)
        value = protocol.envelope(context, lane.nonce, lane.parent, lane.worker, "asr_status", scope=lane.scope)
        value.update(sequence=sequence, updated_at=stamp, summary={"state": "idle", "error": "", "storage": raw})
        record[0] = value
        lane._read_status()
        lane._publish()
    value = SimpleNamespace(lane=lane, wall=wall, mono=mono, receive=receive, observations=observations)
    yield value
    (tmp_path / "observations.json").write_text(json.dumps(observations, indent=2), encoding="utf-8")


def capture_snapshot(lane):
    return lane.snapshot(), copy.deepcopy(lane._cache["_storage_receipt"])


@pytest.mark.parametrize("kind", ["stats", "disk"])
@pytest.mark.parametrize("paths", [("get", "get"), ("get", "receive"), ("receive", "get")])
def test_same_generation_watermarks_are_monotone_across_real_threads(watermark_case, kind, paths):
    c, lane = watermark_case, watermark_case.lane
    c.receive(1, 2000.)
    old_result, old_receipt = capture_snapshot(lane)
    c.wall[0] = 2001.
    c.receive(2, 2001.)
    c.wall[0] = 1999.
    old_read, release, new_attempt = threading.Event(), threading.Event(), threading.Event()
    writes = []
    class PauseOnRead(dict):
        paused = False
        def __getitem__(self, key):
            value = super().__getitem__(key)
            if key == kind and threading.current_thread().name == "wm-old" and not self.paused:
                self.paused = True
                old_read.set()
                assert release.wait(3), "old critical-section pause timed out"
            return value
        def __setitem__(self, key, value):
            writes.append((threading.current_thread().name, key, value))
            super().__setitem__(key, value)
    lane._storage_invalid_through = PauseOnRead(stats=0, disk=0)
    lane._storage_lock = ObservedLock(lane._storage_lock, new_attempt)
    def old_write():
        if paths[0] == "get":
            return lane._snapshot_storage(old_result, old_receipt)
        # Drive the actual receipt stage, without publishing an older cache.
        # This tests the lower-sequence writer path, not out-of-order ticks.
        lane.status_sequence = 1
        return lane._receive_storage(sample(2000.), 1999., 2000.)
    def new_write():
        if paths[1] == "get":
            return lane.snapshot()
        lane.status_sequence = 2
        return lane._receive_storage(sample(2001.), 1999., 2001.)
    with own_threads() as group:
        group.releases.append(release)
        old_done = group.start("wm-old", old_write)
        assert old_read.wait(2)
        new_done = group.start("wm-new", new_write)
        assert new_attempt.wait(2), "new real thread never attempted the watermark lock"
        assert not new_done.wait(.05), "new writer bypassed the held watermark mutex"
        release.set()
        assert old_done.wait(3) and new_done.wait(3)
    lane.status_sequence = 2
    watermarks = lane._storage_watermarks(lane._storage_generation)
    c.observations.append({"paths": paths, "kind": kind, "watermarks": watermarks, "writes": writes, "threads_joined": True})
    assert watermarks == {"stats": 2, "disk": 2}
    c.wall[0] = 2002.
    current = lane.snapshot()
    assert current["sequence"] == 2 and not current["storage"]["verified"] and not current["storage"]["disk"]["verified"]
    c.receive(2, 2001.)  # Same original receipt is still insufficient.
    assert not lane.snapshot()["storage"]["verified"]
    c.receive(3, 2002.)
    current = lane.snapshot()
    assert current["storage"]["verified"] and current["storage"]["disk"]["verified"]


def assert_old_generation_isolation(lane, *, set_future, advance_generation):
    """Reusable bounded real-thread check; does not import the legacy test module."""
    old_result, old_receipt = capture_snapshot(lane)
    assert old_result["storage"]["verified"]
    original_clock = lane.wall_time
    paused, release = threading.Event(), threading.Event()
    def clock():
        value = original_clock()
        if threading.current_thread().name == "wm-old-generation":
            paused.set()
            assert release.wait(10), "old generation clock pause timed out"
        return value
    lane.wall_time = clock
    try:
        set_future()
        with own_threads() as group:
            group.releases.append(release)
            old_done = group.start("wm-old-generation", lambda: lane._snapshot_storage(old_result, old_receipt))
            assert paused.wait(2)
            new_done = group.start("wm-new-generation", advance_generation)
            assert new_done.wait(8), "reset/receive was blocked by a clock callback outside the critical section"
            release.set()
            assert old_done.wait(3)
        current = lane.snapshot()
        assert current["generation"] != old_result["generation"] and current["sequence"] == 1
        assert not old_result["storage"]["verified"]
        assert lane._storage_watermarks(current["generation"]) == {"stats": 0, "disk": 0}
        assert current["storage"]["verified"] and current["storage"]["disk"]["verified"]
        return current
    finally:
        release.set()
        lane.wall_time = original_clock


def test_old_generation_thread_cannot_poison_new_generation(watermark_case):
    c, lane = watermark_case, watermark_case.lane
    c.receive(99, 2000.)
    def set_future():
        c.wall[0] = 1999.
    def advance():
        lane.nonce = "test-next-generation"
        lane._reset_storage()
        lane.status_sequence, lane._last_status = 0, None
        c.receive(1, 1999.)
    result = assert_old_generation_isolation(lane, set_future=set_future, advance_generation=advance)
    c.observations.append({"old_generation_isolated": True, "new_generation": result["generation"], "threads_joined": True})


def test_same_generation_reset_preserves_rejections_and_new_generation_rejects_old_writes(watermark_case):
    c, lane = watermark_case, watermark_case.lane
    c.receive(1, 2000.)
    generation = lane._storage_generation
    assert lane._storage_watermarks(generation, future=("stats", "disk"), sequence=2) == {"stats": 2, "disk": 2}
    observations, receipt = lane._storage_observations, lane._storage_receipt
    lane._reset_storage()
    assert lane._storage_watermarks(generation) == {"stats": 2, "disk": 2}
    assert lane._storage_observations is observations and lane._storage_receipt is receipt
    lane.nonce = "next-reset-generation"
    lane._reset_storage()
    assert lane._storage_generation != generation
    assert lane._storage_watermarks(generation, future=("stats", "disk"), sequence=99) is None
    assert lane._storage_watermarks(lane._storage_generation) == {"stats": 0, "disk": 0}
    assert lane._storage_observations == {} and lane._storage_receipt == {}


def test_clock_and_storage_parser_callbacks_run_outside_watermark_lock(watermark_case, monkeypatch):
    c, lane = watermark_case, watermark_case.lane
    observed = []
    def assert_free(label):
        assert lane._storage_lock.acquire(blocking=False), label + " ran inside watermark mutex"
        lane._storage_lock.release()
        observed.append(label)
    wall, now, parser = lane.wall_time, lane.manager.now, lane_api._storage_summary
    def clock():
        assert_free("clock")
        return wall()
    def monotonic():
        assert_free("monotonic")
        return now()
    def parse(*args):
        assert_free("parser")
        return parser(*args)
    monkeypatch.setattr(lane, "wall_time", clock)
    monkeypatch.setattr(lane.manager, "now", monotonic)
    monkeypatch.setattr(lane_api, "_storage_summary", parse)
    c.receive(1, 2000.)
    assert lane.snapshot()["storage"]["verified"] and set(observed) == {"clock", "monotonic", "parser"}


def test_blocked_protocol_read_does_not_hold_watermark_or_get_lock(watermark_case, monkeypatch):
    c, lane = watermark_case, watermark_case.lane
    c.receive(1, 2000.)
    entered, release = threading.Event(), threading.Event()
    read = lane.protocol.read_record
    def slow(path):
        entered.set()
        assert release.wait(3), "protocol pause timed out"
        return read(path)
    monkeypatch.setattr(lane.protocol, "read_record", slow)
    with own_threads() as group:
        group.releases.append(release)
        writer_done = group.start("wm-slow-receive", lane._read_status)
        assert entered.wait(2)
        getter_done = group.start("wm-fast-get", lane.snapshot)
        assert getter_done.wait(1) and not writer_done.is_set()
        assert group.results["wm-fast-get"]["storage"]["verified"]
        release.set()
        assert writer_done.wait(3)
