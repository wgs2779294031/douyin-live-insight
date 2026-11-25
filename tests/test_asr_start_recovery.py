"""Generated protocol files, fake boot/identity and fake owned Jobs only."""
import copy
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
from local_asr import AsrLane
from scripts import asr_protocol as protocol

BOOT = {"schema": 1, "kind": "windows_boot", "installation": "a" * 64, "counter": 10}


class CreationExit(BaseException):
    pass


class Owned:
    def __init__(self, identity):
        self.identity, self.count, self.closed = identity, 1, False
        self.proc = SimpleNamespace(pid=identity["pid"], poll=lambda: None if self.count else 0)
    def active_count(self):
        return self.count
    def close(self):
        assert self.count == 0
        self.closed = True
        return True
    def worker_identity(self, value):
        return self.identity if self.count and all(value.get(key) == self.identity[key] for key in ("pid", "created")) else None


@pytest.fixture
def case(tmp_path, monkeypatch):
    # Every lane below injects a fake boot. Unexpected production reads fail.
    monkeypatch.setattr(protocol, "boot_identity", lambda: pytest.fail("real registry forbidden"))
    data = tmp_path / "data"
    data.mkdir()
    context = {"data": str(data), "root": str(ROOT), "python": sys.executable, "instance_id": "start-recovery-test"}
    clock, wall, generation, boot, spawned = [100.], [2000.], [10.], [dict(BOOT)], []
    def identity(pid):
        if pid == os.getpid():
            return {"state": "alive", "pid": pid, "created": generation[0]}
        for owned in spawned:
            if owned.identity["pid"] == pid:
                return dict(owned.identity) if owned.count else {"state": "dead", "pid": pid}
        return {"state": "dead", "pid": pid}
    def spawn(*_args, **_kwargs):
        owned = Owned({"state": "alive", "pid": 800000 + len(spawned), "created": 500. + len(spawned)})
        spawned.append(owned)
        return owned
    manager = SimpleNamespace(context=context, directory=data / ".local_app", now=lambda: clock[0], stopping=False,
                              child_env=lambda: {}, spawn=spawn)
    def lane(api=None):
        return AsrLane(manager, identity=identity, wall_time=lambda: wall[0], protocol_api=api,
                       boot_identity=lambda: copy.deepcopy(boot[0]))
    return SimpleNamespace(context=context, data=data, clock=clock, wall=wall, generation=generation,
                           boot=boot, spawned=spawned, manager=manager, lane=lane, identity=identity)


def fault_api(phase, operation="readback", *, once=True):
    hits = []
    def write(path, record):
        protocol.write_record(path, record)
        if Path(path).name == "release.json" and record.get("phase") == phase and operation == "write_after" and (not once or not hits):
            hits.append(True)
            raise protocol.ProtocolError("generated write ambiguity")
    def read(path, *args):
        record = protocol.read_record(path, *args)
        if Path(path).name == "release.json" and record and record.get("phase") == phase and operation == "readback" and (not once or not hits):
            hits.append(True)
            raise protocol.ProtocolError("generated readback ambiguity")
        return record
    return SimpleNamespace(paths=protocol.paths, ensure=protocol.ensure, envelope=protocol.envelope,
                           matches=protocol.matches, write_record=write, read_record=read), hits


def ack(case, lane):
    protocol.write_record(lane.paths["ready"], {"schema": 1, "instance_id": case.context["instance_id"],
        "role": "asr:global", "nonce": lane.nonce, "pid": lane.owned.identity["pid"],
        "created": lane.owned.identity["created"], "updated_at": case.wall[0]})


@pytest.mark.parametrize("phase", ["reserved", "creating"])
@pytest.mark.parametrize("operation", ["readback", "write_after"])
def test_no_create_fault_can_retry_and_stop_without_entering_spawn(case, phase, operation):
    api, hits = fault_api(phase, operation)
    lane = case.lane(api)
    lane.tick()
    assert hits == [True] and not case.spawned and lane.owned is None
    assert lane.retry()["ok"] and not case.spawned
    record = protocol.read_record(lane.paths["release"])
    assert record["phase"] == "released" and record["proof"] == "no_spawn" and record["root"] is None
    lane.begin_stop()
    assert lane.drained() and not lane.snapshot()["resource_held"] and not case.spawned


def test_no_create_fault_can_settle_then_launch_and_admit_normally(case):
    api, _ = fault_api("reserved")
    lane = case.lane(api)
    lane.tick()
    lane.tick()  # Settlement is separate; it cannot fall through into Popen.
    assert not case.spawned and not lane.snapshot()["resource_held"]
    case.clock[0] += 3
    lane.tick()
    assert len(case.spawned) == 1
    assert protocol.read_record(lane.paths["release"])["phase"] == "owned"
    ack(case, lane)
    lane.tick()
    assert lane.admitted and protocol.read_record(lane.paths["release"])["phase"] == "admission"
    lane.begin_stop()
    case.spawned[0].count = 0
    assert lane.drained()
    assert protocol.read_record(lane.paths["release"])["proof"] == "job_empty"


def test_no_create_settlement_readback_ambiguity_is_retryable_same_fact(case):
    api, _ = fault_api("reserved")
    lane = case.lane(api)
    lane.tick()
    settle_api, hits = fault_api("released")
    lane.protocol = settle_api
    assert not lane.retry()["ok"] and hits == [True] and not case.spawned
    assert lane.retry()["ok"] and not case.spawned
    lane.begin_stop()
    assert lane.drained()


def test_repeated_settlement_write_failure_does_not_forget_zero_create_stage(case):
    api, _ = fault_api("creating")
    lane = case.lane(api)
    lane.tick()
    writes = []
    def write(path, record):
        if record.get("proof") == "no_spawn" and len(writes) < 3:
            writes.append(True)
            raise protocol.ProtocolError("generated pre-write storage failure")
        protocol.write_record(path, record)
    lane.protocol = SimpleNamespace(**{name: getattr(protocol, name) for name in ("paths", "ensure", "envelope", "matches", "read_record")}, write_record=write)
    for _ in range(3):
        assert not lane.retry()["ok"]
        case.wall[0] += 1
    assert writes == [True] * 3 and not case.spawned
    assert lane.retry()["ok"] and not case.spawned
    lane.begin_stop()
    assert lane.drained() and not lane.snapshot()["resource_held"]


@pytest.mark.parametrize("exception", [RuntimeError, CreationExit])
def test_entered_spawn_exception_cannot_be_cancelled_as_no_create(case, exception):
    calls = []
    def fail(*_a, **_k):
        calls.append(True)
        assert protocol.read_record(protocol.paths(case.context)["release"])["phase"] == "creating"
        raise exception("generated creation entry, no real process")
    case.manager.spawn = fail
    lane = case.lane()
    if exception is CreationExit:
        with pytest.raises(CreationExit):
            lane.tick()
    else:
        lane.tick()
    for _ in range(3):
        lane.tick()
        assert not lane.retry()["ok"]
    lane.begin_stop()
    assert not lane.drained() and len(calls) == 1 and lane.snapshot()["resource_held"]


def create_window(case, phase):
    if phase == "creating":
        def fail(*_a, **_k):
            raise CreationExit("generated exit at creation entry")
        spawn = case.manager.spawn
        case.manager.spawn = fail
        lane = case.lane()
        with pytest.raises(CreationExit):
            lane.tick()
        case.manager.spawn = spawn
    else:
        lane = case.lane()
        lane.tick()
        if phase == "admission":
            ack(case, lane)
            # Actual admission write succeeds, but its outer readback is unknown.
            def read(path, *args):
                record = protocol.read_record(path, *args)
                if Path(path).name == "admission.json":
                    raise protocol.ProtocolError("generated admission ambiguity")
                return record
            lane.protocol = SimpleNamespace(**{name: getattr(protocol, name) for name in ("paths", "ensure", "envelope", "matches", "write_record")}, read_record=read)
            lane.tick()
            assert lane.may_admitted and not lane.admitted
    assert protocol.read_record(lane.paths["release"])["phase"] == phase
    return lane


@pytest.mark.parametrize("phase", ["creating", "owned", "admission"])
def test_same_boot_lost_handle_windows_stay_held(case, phase):
    old = create_window(case, phase)
    retained = old.paths["release"].read_bytes()
    case.generation[0] += 10
    fresh = case.lane()
    calls = len(case.spawned)
    for _ in range(3):
        fresh.tick()
        assert not fresh.retry()["ok"]
    fresh.begin_stop()
    assert not fresh.drained() and fresh.snapshot()["resource_held"]
    assert len(case.spawned) == calls and old.paths["release"].read_bytes() == retained


@pytest.mark.parametrize("phase", ["creating", "owned", "admission"])
def test_strict_new_boot_recovers_creation_and_admission_windows(case, phase):
    old = create_window(case, phase)
    case.generation[0] += 10
    case.boot[0]["counter"] += 1
    for owned in case.spawned:
        owned.count = 0  # The simulated new OS boot ended all old fake processes.
    case.wall[0] -= 100  # OS boot proof does not depend on wall-clock direction.
    fresh = case.lane()
    assert fresh.retry()["ok"]
    proof = protocol.read_record(old.paths["release"])
    assert proof["phase"] == "released" and proof["proof"] == "boot_advanced"
    assert protocol.boot_advanced(proof["boot"], proof["released_boot"])
    before = len(case.spawned)
    fresh.tick()
    assert len(case.spawned) == before + 1 and fresh.owned is not None
    # An ACK-less new worker may leave the old boot's future-dated metadata in
    # place. Resource closure depends on its Job, never on wall time catching up.
    fresh.begin_stop()
    fresh.owned.count = 0
    assert fresh.drained() and not fresh.snapshot()["resource_held"]
    final = protocol.read_record(fresh.paths["release"])
    assert final["proof"] == "job_empty"


@pytest.mark.parametrize("change", ["same", "rollback", "different_installation", "none", "invalid"])
def test_unproven_boot_change_never_releases_unknown_tree(case, change):
    old = create_window(case, "creating")
    case.generation[0] += 10
    if change == "rollback":
        case.boot[0]["counter"] -= 1
    elif change == "different_installation":
        case.boot[0].update(installation="b" * 64, counter=11)
    elif change == "none":
        case.boot[0] = None
    elif change == "invalid":
        case.boot[0] = {"kind": "windows_boot", "counter": 11}
    fresh = case.lane()
    fresh.tick()
    assert not fresh.retry()["ok"] and not fresh.drained() and fresh.snapshot()["resource_held"]
    assert not case.spawned and protocol.read_record(old.paths["release"])["empty"] is False


@pytest.mark.parametrize("old_parent", ["alive", "ended"])
def test_reserved_phase_is_durable_zero_create_only_after_old_parent_ended(case, old_parent):
    api, _ = fault_api("reserved")
    old = case.lane(api)
    old.tick()
    if old_parent == "ended":
        case.generation[0] += 10
    fresh = case.lane()
    assert fresh.retry()["ok"] is (old_parent == "ended")
    assert not case.spawned


@pytest.mark.parametrize("field", ["root", "worker", "boot", "phase", "records"])
def test_missing_schema2_fields_cannot_escape_or_authorize_recovery(case, field):
    old = create_window(case, "creating")
    record = protocol.read_record(old.paths["release"])
    del record[field]
    protocol.write_record(old.paths["release"], record)
    case.boot[0]["counter"] += 1
    fresh = case.lane()
    fresh.tick()
    assert not fresh.retry()["ok"] and not fresh.drained() and not case.spawned


def test_new_boot_does_not_override_conflicting_records_or_replaced_scope(case):
    old = create_window(case, "owned")
    ack(case, old)
    record = protocol.read_record(old.paths["ready"])
    record["nonce"] = "unrelated-resource"
    protocol.write_record(old.paths["ready"], record)
    case.boot[0]["counter"] += 1
    fresh = case.lane()
    assert not fresh.retry()["ok"] and len(case.spawned) == 1
    case.data.rename(case.data.with_name("retained-data"))
    case.data.mkdir()
    fresh.tick()
    assert not fresh.retry()["ok"] and not fresh.drained()
    assert list(case.data.iterdir()) == [] and len(case.spawned) == 1
