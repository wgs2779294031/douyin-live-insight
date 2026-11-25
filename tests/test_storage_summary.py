"""Cached storage bridge: generated metadata, fake identities and own loopback."""
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
from local_asr import AsrLane
from scripts import asr_protocol as protocol
from scripts import audio_segments as audio
from test_local_app import make_context
from test_local_access import local_boards, _bootstrap, _request, _state_bytes

BOOT = {"schema": 1, "kind": "windows_boot", "installation": "a" * 64, "counter": 10}


class Owned:
    def __init__(self, pid):
        self.identity = {"state": "alive", "pid": pid, "created": float(pid)}
        self.count, self.closed = 1, False
        self.proc = SimpleNamespace(pid=pid, poll=lambda: None if self.count else 0)
    def active_count(self):
        return self.count
    def close(self):
        assert self.count == 0
        self.closed = True
        return True
    def worker_identity(self, value):
        return self.identity if self.count and all(value.get(key) == self.identity[key] for key in ("pid", "created")) else None


@pytest.fixture
def case(tmp_path):
    context = make_context(tmp_path)
    clock, wall, spawned = [100.], [2000.], []
    def identity(pid):
        if pid == os.getpid():
            return {"state": "alive", "pid": pid, "created": 10.}
        return next((dict(item.identity) if item.count else {"state": "dead", "pid": pid}
            for item in spawned if item.identity["pid"] == pid), {"state": "dead", "pid": pid})
    def spawn(*_args, **_kwargs):
        item = Owned(850000 + len(spawned))
        spawned.append(item)
        return item
    manager = SimpleNamespace(context=context, directory=Path(context["data"]) / ".local_app", stopping=False,
        now=lambda: clock[0], child_env=lambda: {}, spawn=spawn)
    lane = AsrLane(manager, identity=identity, wall_time=lambda: wall[0], boot_identity=lambda: dict(BOOT))
    return SimpleNamespace(lane=lane, context=context, clock=clock, wall=wall, spawned=spawned, manager=manager)


def admitted(case):
    lane = case.lane
    lane.tick()
    protocol.write_record(lane.paths["ready"], {"schema": 1, "instance_id": case.context["instance_id"],
        "role": "asr:global", "nonce": lane.nonce, "pid": lane.owned.identity["pid"],
        "created": lane.owned.identity["created"], "updated_at": case.wall[0]})
    lane.tick()
    assert lane.admitted


def storage(case):
    wall = case.wall[0]
    return {"schema": 1, "snapshot_at": wall, "audio_bytes": 12345, "audio_files": 3,
        "transcription_pending": 1, "cleanup_pending": 1, "cleanup_failed": 0, "cleanup_blocked": 1,
        "scan": {"partial": False, "limited": False, "checked": 3, "started_at": wall - 1,
                 "updated_at": wall, "last_complete_at": wall, "approximate": True, "counts_scope": "scan_round"},
        "disk": {"free_bytes": 2 ** 31, "total_bytes": 2 ** 40, "threshold_bytes": 2 ** 30,
                 "low_space": False, "sampled_at": wall, "error": ""},
        "last_cleanup": {"at": wall, "tag": "generated", "date": "2000-01-01", "segment": "seg_0001.flv",
                         "state": "deleted", "reason": "audio_deleted"}}


def publish(case, value, sequence=1, *, updated=None, binding=None, state="idle"):
    lane = case.lane
    record = dict(protocol.envelope(case.context, lane.nonce, lane.parent, lane.worker, "asr_status", lane.scope),
        sequence=sequence, updated_at=case.wall[0] if updated is None else updated,
        summary={"state": state, "error": "", "counts": {}, "storage": value, "current": {"phase": "cleaning"}})
    if binding:
        record.update(binding)
    protocol.write_record(lane.paths["status"], record)
    lane.tick()
    return lane.snapshot()


def test_initial_storage_unknown_and_partial_scope_is_preserved(case):
    initial = case.lane.snapshot()["storage"]
    assert initial["audio_bytes"] is None and initial["audio_files"] is None
    assert not initial["verified"] and initial["stale"] and initial["scan"]["partial"]
    assert initial["disk"]["low_space"] is None
    admitted(case)
    value = storage(case)
    value["scan"].update(partial=True, limited=True)
    result = publish(case, value)
    safe = result["storage"]
    assert safe["verified"] and not safe["stale"] and safe["audio_bytes"] == 12345
    assert safe["scan"]["partial"] and safe["scan"]["limited"]
    assert safe["scan"]["counts_scope"] == "scan_round" and safe["scan"]["approximate"]
    assert result["current"]["phase"] == "cleaning"


def test_heartbeats_and_disk_samples_never_refresh_statistics_ttl(case):
    admitted(case)
    value = storage(case)
    publish(case, value)
    case.clock[0] += 16
    case.wall[0] += 16
    value["disk"]["sampled_at"] = case.wall[0]
    result = publish(case, value, 2)
    assert result["verified"] and result["storage"]["stale"] and not result["storage"]["verified"]
    assert result["storage"]["disk"]["verified"]
    case.wall[0] = 2005  # Wall rollback cannot make the old observed round fresh.
    result = publish(case, value, 3)
    assert result["storage"]["stale"] and not result["storage"]["verified"]


def test_disk_sample_has_independent_sixty_second_ttl(case):
    admitted(case)
    value = storage(case)
    publish(case, value)
    case.clock[0] += 61
    case.wall[0] += 61
    fresh_stats = storage(case)
    fresh_stats["disk"] = value["disk"]
    result = publish(case, fresh_stats, 2)["storage"]
    assert result["verified"] and not result["disk"]["verified"] and result["disk"]["stale"]
    assert result["disk"]["low_space"] is None


@pytest.mark.parametrize("kind", ["stats", "disk", "envelope"])
def test_future_receipt_cannot_turn_valid_without_new_sequence(case, kind):
    admitted(case)
    value = storage(case)
    if kind == "stats":
        value["snapshot_at"] += 1
        value["scan"]["updated_at"] += 1
    elif kind == "disk":
        value["disk"]["sampled_at"] += 1
    result = publish(case, value, updated=case.wall[0] + 1 if kind == "envelope" else None)
    selected = result["storage"]["disk"] if kind == "disk" else result["storage"]
    assert not selected["verified"]
    case.clock[0] += 2
    case.wall[0] += 2
    case.lane.tick()  # Exactly the same file/sequence must not renew or revalidate.
    selected = case.lane.snapshot()["storage"]
    if kind == "disk":
        selected = selected["disk"]
    assert not selected["verified"]
    result = publish(case, storage(case), 2)["storage"]
    assert result["verified"] and result["disk"]["verified"]


def test_cached_clock_rollback_latches_current_sequence_until_new_valid_receipt(case):
    admitted(case)
    assert publish(case, storage(case))["storage"]["verified"]
    case.wall[0] -= 1
    assert not case.lane.snapshot()["storage"]["verified"]
    assert not case.lane.snapshot()["storage"]["disk"]["verified"]
    case.wall[0] += 2
    assert not case.lane.snapshot()["storage"]["verified"]
    assert not case.lane.snapshot()["storage"]["disk"]["verified"]
    assert publish(case, storage(case), 2)["storage"]["verified"]


@pytest.mark.parametrize("bad", [None, {}, {"schema": 2}, {"schema": True}, {"schema": 1, "audio_bytes": True},
    {"schema": 1, "disk": {"error": {"SECRET": "C:/private"}}, "last_cleanup": {"state": [], "reason": []}}])
def test_missing_bad_storage_is_unknown_and_never_echoes_arbitrary_json(case, bad):
    admitted(case)
    result = publish(case, bad)["storage"]
    assert result["audio_files"] is None and result["scan"]["partial"] and not result["verified"]
    assert result["disk"]["low_space"] is None and "SECRET" not in json.dumps(result) and "private" not in json.dumps(result)


@pytest.mark.parametrize("free,total,raw_low,expected", [(0, 2 ** 40, False, True), (2 ** 30, 2 ** 40, True, False),
    (None, 2 ** 40, False, None), (5, 4, False, None), (-1, 10, False, None), (True, 10, False, None),
    (2 ** 54, 2 ** 55, False, None), (0, 0, False, None)])
def test_low_space_is_derived_only_from_valid_bounded_disk_sample(case, free, total, raw_low, expected):
    admitted(case)
    value = storage(case)
    value["disk"].update(free_bytes=free, total_bytes=total, low_space=raw_low, threshold_bytes=0)
    result = publish(case, value)["storage"]["disk"]
    assert result["low_space"] is expected and result["threshold_bytes"] == 2 ** 30
    assert result["verified"] is (expected is not None)


@pytest.mark.parametrize("binding", [{"nonce": "old-worker"}, {"scope": {"path": "private"}}, {"sequence": 0}])
def test_wrong_old_worker_scope_or_sequence_cannot_publish_storage(case, binding):
    admitted(case)
    result = publish(case, storage(case), binding=binding)
    assert not result["storage"]["verified"] and not result["storage"]["disk"]["verified"]


def test_storage_public_fields_are_bounded_and_cleanup_failure_stays_separate(case):
    admitted(case)
    value = storage(case)
    value.update(audio_bytes=2 ** 54, cleanup_failed=5, private_path="C:/SECRET", nonce="SECRET")
    value["last_cleanup"].update(tag="../SECRET", date="private", segment="C:/SECRET", state="pending", reason="C:/SECRET")
    result = publish(case, value)
    safe = result["storage"]
    assert result["error"] == "" and safe["cleanup_failed"] == 5
    assert safe["audio_bytes"] is None and safe["last_cleanup"]["reason"] == "cleanup_unconfirmed"
    assert "SECRET" not in json.dumps(result) and len(json.dumps(result)) < 4000


@pytest.mark.parametrize("state", ["identity_unknown", "storage_error", "invalid-state"])
def test_unknown_worker_state_does_not_make_cached_storage_current(case, state):
    admitted(case)
    result = publish(case, storage(case), state=state)
    assert not result["storage"]["verified"] and not result["storage"]["disk"]["verified"]


def test_new_worker_generation_clears_old_storage_samples(case):
    admitted(case)
    first = publish(case, storage(case))
    case.lane.owned.count = 0
    case.lane.tick()
    case.clock[0] += 3
    case.wall[0] += 3
    case.lane.tick()
    result = case.lane.snapshot()
    assert result["generation"] != first["generation"] and len(case.spawned) == 2
    assert result["storage"]["audio_bytes"] is None and result["storage"]["audio_files"] is None
    assert not result["storage"]["verified"] and result["storage"]["disk"]["low_space"] is None


def test_repeated_loopback_get_is_cache_only_with_all_lane_io_forbidden(case, local_boards, monkeypatch):
    admitted(case)
    publish(case, storage(case))
    item = local_boards(context=case.context)
    item.manager.asr = case.lane
    auth = _bootstrap(item.port)
    before = _state_bytes(item)
    def forbidden(*_args, **_kwargs):
        pytest.fail("GET must not query/read/scan/sample or create a worker")
    with monkeypatch.context() as patch:
        patch.setattr(protocol, "read_record", forbidden)
        patch.setattr(audio, "data_scope", forbidden)
        patch.setattr(shutil, "disk_usage", forbidden)
        patch.setattr(os, "scandir", forbidden)
        patch.setattr(Path, "open", forbidden)
        patch.setattr(case.manager, "spawn", forbidden)
        for _ in range(5):
            code, raw, _ = _request(item.port, "GET", "/api/asr", headers=auth)
            assert code == 200 and json.loads(raw)["status"]["storage"]["verified"]
    assert _state_bytes(item) == before and len(case.spawned) == 1


def test_slow_worker_metadata_never_blocks_snapshot_or_loopback_get(case, local_boards, monkeypatch):
    admitted(case)
    publish(case, storage(case))
    item = local_boards(context=case.context)
    item.manager.asr = case.lane
    auth = _bootstrap(item.port)
    entered, release = threading.Event(), threading.Event()
    read = protocol.read_record
    def slow(path, *args):
        if Path(path) == case.lane.paths["status"]:
            entered.set()
            assert release.wait(3)
        return read(path, *args)
    monkeypatch.setattr(protocol, "read_record", slow)
    thread = threading.Thread(target=case.lane.tick)
    thread.start()
    try:
        assert entered.wait(1)
        started = time.monotonic()
        code, raw, _ = _request(item.port, "GET", "/api/asr", headers=auth)
        assert code == 200 and json.loads(raw)["status"]["storage"]["verified"]
        assert time.monotonic() - started < .5
        case.lane.begin_stop()
        assert not case.lane.snapshot()["storage"]["verified"]
    finally:
        release.set()
        thread.join(3)
        assert not thread.is_alive()


def test_concurrent_old_get_never_poisons_new_generation_watermark(case):
    from test_storage_watermark_concurrency import assert_old_generation_isolation
    admitted(case)
    first = publish(case, storage(case), 99)
    lane = case.lane
    def set_future():
        case.wall[0] -= 1
    def advance_generation():
        lane.owned.count = 0
        lane.tick()
        case.clock[0] += 3
        lane.tick()
        admitted(case)
        return publish(case, storage(case), 1)
    current = assert_old_generation_isolation(lane, set_future=set_future, advance_generation=advance_generation)
    assert len(case.spawned) == 2
    assert current["sequence"] == 1 and current["generation"] != first["generation"]
    assert current["storage"]["verified"] and current["storage"]["disk"]["verified"]
