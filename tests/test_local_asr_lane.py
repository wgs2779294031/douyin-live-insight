"""Only generated metadata, fake Jobs and one exact owned toy process tree."""
import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
import local_app as app
import local_process as process
from local_asr import AsrLane
from scripts import asr_protocol as protocol
from test_local_app import make_context


class Job:
    def __init__(self):
        self.count, self.forced = 1, 0
    def active_processes(self):
        return self.count
    def release_empty(self):
        return self.count == 0
    def close(self, timeout=15):
        self.forced += 1
        raise AssertionError("ASR manager must not force close its lane")


@pytest.fixture
def lane(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    clock, wall, registry, spawned = [100.0], [2000.0], {}, []
    parent = {"state": "alive", "pid": os.getpid(), "created": 10.0}
    registry[parent["pid"]] = parent
    def identity(pid):
        actual = registry.get(pid, {"state": "dead", "pid": pid})
        for owned, _, _ in spawned:
            if (owned.job.count == 0 and (pid == owned.proc.pid or pid == owned.proc.pid + 1)
                    and actual.get("created") == float(pid)):
                return {"state": "dead", "pid": pid}
        return dict(actual)
    monkeypatch.setattr(process, "process_identity", identity)
    monkeypatch.setattr(process, "process_parent_pid", lambda pid: pid - 1)
    def spawn(command, **kwargs):
        pid = parent["pid"] + 1000 + len(spawned) * 2
        registry[pid] = {"state": "alive", "pid": pid, "created": float(pid)}
        proc = SimpleNamespace(pid=pid, _watch_job=Job(), exit_code=None)
        proc.poll = lambda: proc.exit_code
        owned = process.OwnedProcess(proc)
        spawned.append((owned, command, kwargs))
        return owned
    manager = SimpleNamespace(context=context, directory=app.runtime_dir(context), stopping=False,
        now=lambda: clock[0], spawn=spawn, child_env=lambda: {"DY_CUSTOMER_CONTEXT": str(app.runtime_dir(context) / "context.json")})
    value = AsrLane(manager, identity=identity, wall_time=lambda: wall[0])
    return SimpleNamespace(lane=value, manager=manager, context=context, clock=clock, wall=wall,
        registry=registry, spawned=spawned, identity=identity)


def ack(item, *, redirect=False, nonce=None):
    lane = item.lane
    root = lane.owned.proc.pid
    pid = root + 1 if redirect else root
    if redirect:
        item.registry[pid] = {"state": "alive", "pid": pid, "created": float(pid)}
    worker = dict(item.registry[pid])
    lane.paths["ready"].parent.mkdir(exist_ok=True)
    protocol.write_record(lane.paths["ready"], {"schema": 1, "instance_id": item.context["instance_id"],
        "role": "asr:global", "nonce": lane.nonce if nonce is None else nonce,
        "pid": pid, "created": worker["created"], "updated_at": item.wall[0]})
    return worker


def status(item, sequence=1, **changes):
    lane = item.lane
    summary = {"state": "idle", "counts": {key: 0 for key in ("pending", "projection_pending", "retryable", "blocked", "complete")},
        "scan": {"partial": False, "limited": False, "checked": 2, "last_complete_at": item.wall[0]},
        "current": {}, "error": "", "last_progress_at": item.wall[0], "last_completed_at": None}
    summary.update(changes)
    record = dict(protocol.envelope(item.context, lane.nonce, lane.parent, lane.worker, "asr_status", lane.scope),
        sequence=sequence, updated_at=item.wall[0], summary=summary)
    protocol.write_record(lane.paths["status"], record)
    return record


def admitted(item, **kwargs):
    item.lane.tick()
    worker = ack(item, **kwargs)
    item.lane.tick()
    assert item.lane.admitted
    return worker


def test_lane_is_singleton_and_independent_of_roster_and_get(lane):
    value = lane.lane
    for _ in range(3):
        assert value.snapshot()["counts"]["pending"] is None
    assert lane.spawned == []
    value.tick()
    assert len(lane.spawned) == 1 and value.snapshot()["resource_held"]
    assert lane.spawned[0][1] == [lane.context["python"], "-B", str(ROOT / "scripts/asr_worker.py")]
    assert lane.spawned[0][2]["env"]["DY_ASR_PARENT_PID"] == str(os.getpid())
    assert json.loads(lane.spawned[0][2]["env"]["DY_ASR_SCOPE"]) == value.scope
    for _ in range(5):
        value.tick()
    assert len(lane.spawned) == 1 and not value.drained()
    assert not lane.spawned[0][0].job.forced


def test_venv_direct_child_ack_is_admitted_and_scope_bound(lane):
    worker = admitted(lane, redirect=True)
    value = lane.lane
    admission = protocol.read_record(value.paths["admission"])
    assert admission["worker"] == worker and admission["parent"] == value.parent
    assert admission["scope"] == value.scope and admission["nonce"] == value.nonce
    status(lane)
    value.tick()
    snap = value.snapshot()
    assert snap["state"] == "idle" and snap["verified"] and not snap["stale"]
    assert value.nonce not in json.dumps(snap) and str(lane.context["data"]) not in json.dumps(snap)


@pytest.mark.parametrize("fault", ["nonce", "pid_reused", "unknown_tree"])
def test_unverified_resources_never_admit_or_spawn_again(lane, fault):
    value = lane.lane
    value.tick()
    ack(lane, nonce="previous-boot" if fault == "nonce" else None)
    if fault == "pid_reused":
        lane.registry[value.owned.proc.pid]["created"] += 1
    if fault == "unknown_tree":
        value.owned.job.count = None
    lane.clock[0] += 1000
    for _ in range(3):
        value.tick()
    assert not value.admitted and not value.paths["admission"].exists()
    assert value.snapshot()["resource_held"] and not value.snapshot()["verified"]
    assert not value.drained() and len(lane.spawned) == 1 and value.owned.job.forced == 0


def test_admission_write_then_error_is_unknown_until_tree_exits(lane, monkeypatch):
    value = lane.lane
    value.tick()
    ack(lane)
    write = protocol.write_record
    def uncertain(path, record):
        write(path, record)
        if path == value.paths["admission"]:
            raise PermissionError("generated readback ambiguity")
    monkeypatch.setattr(protocol, "write_record", uncertain)
    value.tick()
    assert value.may_admitted and not value.admitted and value.state == "admission_unknown"
    for _ in range(3):
        value.tick()
        assert not value.retry()["ok"]
    assert len(lane.spawned) == 1 and not value.drained() and value.owned.job.forced == 0
    lane.manager.stopping = True
    value.begin_stop()
    assert not value.drained()
    value.owned.job.count = 0
    value.tick()
    assert value.drained() and value.state == "stopped"


def test_missing_accepted_admission_does_not_renew_authorization(lane):
    value = lane.lane
    admitted(lane)
    status(lane)
    value.tick()
    assert value.snapshot()["verified"]
    path = value.paths["admission"]
    path.rename(path.with_suffix(".retained"))
    value.tick()
    assert value.state == "admission_unknown" and not value.snapshot()["verified"]
    assert not value.retry()["ok"] and len(lane.spawned) == 1
    assert not value.drained() and not value.paths["admission"].exists()


@pytest.mark.parametrize("active", [False, True])
def test_replaced_data_root_does_not_receive_old_lane_records_or_new_worker(lane, active):
    value = lane.lane
    if active:
        admitted(lane)
        status(lane)
        value.tick()
    original_scope = dict(value.scope)
    root = Path(lane.context["data"])
    root.rename(root.with_name(root.name + " retained"))
    root.mkdir()
    value.tick()
    assert value.scope == original_scope and value.state == "identity_unknown"
    assert not value.snapshot()["verified"] and not value.retry()["ok"]
    value.begin_stop()
    assert list(root.iterdir()) == []
    assert len(lane.spawned) == int(active)
    if active:
        assert value.snapshot()["resource_held"] and not value.drained()
        value.owned.job.count = 0
    assert not value.drained()  # Scope changed: the empty resource proof cannot be published.


def test_root_exit_with_live_descendant_or_unknown_count_never_releases(lane):
    value = lane.lane
    admitted(lane)
    owned = value.owned
    owned.proc.exit_code = 0
    owned.job.count = 1
    value.tick()
    assert value.state == "draining" and not value.drained()
    owned.job.count = None
    value.tick()
    assert value.state == "identity_unknown" and not value.drained()
    assert len(lane.spawned) == 1 and owned.job.forced == 0
    lane.manager.stopping = True
    owned.job.count = 0
    assert value.drained() and owned.closed


@pytest.mark.parametrize("failure", ["false", "raise"])
def test_empty_tree_release_failure_stays_owned_and_blocks_restart(lane, failure):
    value = lane.lane
    value.tick()
    owned = value.owned
    owned.proc.exit_code, owned.job.count = 0, 0
    def release():
        if failure == "raise":
            raise OSError("generated Job close query failure")
        return False
    owned.job.release_empty = release
    for _ in range(2):
        value.tick()
    assert value.state == "identity_unknown" and value.owned is owned
    assert not value.drained() and len(lane.spawned) == 1 and owned.job.forced == 0


def test_stop_write_failure_does_not_release_or_admit_more_work(lane, monkeypatch):
    value = lane.lane
    admitted(lane)
    write = protocol.write_record
    def fail(path, record):
        if path == value.paths["stop"]:
            raise PermissionError("generated stop write failure")
        return write(path, record)
    monkeypatch.setattr(protocol, "write_record", fail)
    lane.manager.stopping = True
    value.begin_stop()
    assert value.state == "stopping" and value.error == "stop_request_unavailable"
    assert value.snapshot()["resource_held"] and not value.drained() and not value.retry()["ok"]
    lane.clock[0] += 3
    value.tick()
    assert len(lane.spawned) == 1 and value.owned.job.forced == 0
    monkeypatch.setattr(protocol, "write_record", write)
    lane.clock[0] += 3
    value.tick()
    stop = protocol.read_record(value.paths["stop"])
    assert stop["worker"] == value.worker and stop["action"] == "stop"
    value.owned.job.count = 0
    assert value.drained()


def test_repeated_confirmed_worker_exit_is_limited_and_retry_only_resets_asr(lane):
    value = lane.lane
    calls = []
    lane.manager.roles = {"record:kept": {"failures": 9}}
    lane.manager.browser_failures = 7
    for _ in range(10):
        value.tick()
        if value.owned is not None:
            calls.append(1)
            value.owned.proc.exit_code, value.owned.job.count = 0, 0
            value.tick()
        lane.clock[0] += 31
    assert len(calls) == 3 and value.state == "restart_limited"
    assert value.retry()["ok"] and value.failures == 0
    assert lane.manager.roles["record:kept"]["failures"] == 9 and lane.manager.browser_failures == 7


def test_failed_new_launch_does_not_mix_the_previous_boot_snapshot(lane):
    value = lane.lane
    admitted(lane)
    status(lane, counts={"complete": 2})
    value.tick()
    previous = value.snapshot()["generation"]
    value.owned.proc.exit_code, value.owned.job.count = 0, 0
    value.tick()
    lane.clock[0] += 31
    def fail(*_args, **_kwargs):
        raise OSError("generated next launch failure")
    lane.manager.spawn = fail
    value.tick()
    current = value.snapshot()
    assert current["generation"] != previous and current["counts"]["complete"] is None
    assert current["snapshot_at"] is None and not current["verified"] and current["resource_held"]
    assert value.state == "identity_unknown" and not value.retry()["ok"]


def successor(item):
    return AsrLane(item.manager, identity=item.identity, wall_time=lambda: item.wall[0])


@pytest.mark.parametrize("fact", ["alive", "unknown", "dead", "reused", "bad", "conflict"])
def test_previous_boot_without_empty_job_proof_never_starts_or_gets_overwritten(lane, fact):
    value = lane.lane
    worker = admitted(lane)
    status(lane)
    root = value.owned.proc.pid
    if fact in ("unknown", "dead"):
        lane.registry[root] = {"state": fact, "pid": root}
    elif fact == "reused":
        lane.registry[root]["created"] += 1
    elif fact == "bad":
        value.paths["status"].write_text("broken generated JSON", encoding="utf-8")
    elif fact == "conflict":
        record = protocol.read_record(value.paths["admission"])
        record["nonce"] = "another-unreleased-boot"
        protocol.write_record(value.paths["admission"], record)
    retained = {key: value.paths[key].read_bytes() for key in ("ready", "status", "admission", "release")}
    fresh = successor(lane)
    for _ in range(3):
        fresh.tick()
        assert not fresh.retry()["ok"]
    fresh.begin_stop()
    assert not fresh.drained() and fresh.owned is None
    assert fresh.snapshot()["resource_held"] and fresh.state == "identity_unknown"
    assert len(lane.spawned) == 1 and value.owned.job.forced == 0
    assert retained == {key: value.paths[key].read_bytes() for key in retained}
    assert worker["pid"] == root


@pytest.mark.parametrize("redirect", [False, True])
def test_matching_empty_release_allows_clean_manager_restart(lane, redirect):
    value = lane.lane
    admitted(lane, redirect=redirect)
    status(lane)
    owned, nonce = value.owned, value.nonce
    owned.proc.exit_code, owned.job.count = 0, 0
    value.begin_stop()
    assert value.drained() and owned.closed
    release = protocol.read_record(value.paths["release"])
    assert release["empty"] is True and release["root"] == owned.identity
    assert release["worker"] == value.worker and release["nonce"] == nonce
    fresh = successor(lane)
    fresh.tick()
    assert len(lane.spawned) == 2 and fresh.nonce != nonce and fresh.owned is not None
    reservation = protocol.read_record(value.paths["release"])
    assert reservation["empty"] is False and reservation["nonce"] == fresh.nonce


def test_same_manager_ackless_exit_preserves_old_records_and_can_restart_cleanly(lane):
    value = lane.lane
    admitted(lane)
    status(lane)
    retained = {key: value.paths[key].read_bytes() for key in ("ready", "status", "admission")}
    value.owned.proc.exit_code, value.owned.job.count = 0, 0
    value.tick()
    lane.clock[0] += 31
    value.tick()
    assert len(lane.spawned) == 2
    # The second boot never publishes ACK/status; its exact Job still proves empty.
    value.owned.proc.exit_code, value.owned.job.count = 0, 0
    value.begin_stop()
    assert value.drained()
    assert retained == {key: value.paths[key].read_bytes() for key in retained}
    fresh = successor(lane)
    fresh.tick()
    assert len(lane.spawned) == 3 and fresh.owned is not None


@pytest.mark.parametrize("fault", ["write_after", "readback", "changed_record", "unknown_identity"])
def test_empty_release_ambiguity_retains_closed_owned_until_confirmed(lane, monkeypatch, fault):
    value = lane.lane
    admitted(lane)
    status(lane)
    owned = value.owned
    owned.proc.exit_code, owned.job.count = 0, 0
    write, read = protocol.write_record, protocol.read_record
    real_identity = value.identity
    if fault == "unknown_identity":
        value.identity = lambda pid: {"state": "unknown", "pid": pid} if pid == owned.proc.pid else real_identity(pid)
    def uncertain_write(path, record):
        write(path, record)
        if path == value.paths["release"] and record.get("empty") is True and fault == "write_after":
            raise PermissionError("generated post-write ambiguity")
    def uncertain_read(path, *args, **kwargs):
        record = read(path, *args, **kwargs)
        if path == value.paths["release"] and record.get("empty") is True and fault == "readback":
            raise PermissionError("generated readback ambiguity")
        return record
    monkeypatch.setattr(protocol, "write_record", uncertain_write)
    monkeypatch.setattr(protocol, "read_record", uncertain_read)
    if fault == "changed_record":
        record = read(value.paths["admission"])
        record["nonce"] = "unrelated-boot"
        write(value.paths["admission"], record)
    value.tick()
    assert owned.closed and value.owned is owned and value.snapshot()["resource_held"]
    assert value.state == "identity_unknown" and not value.drained() and not value.retry()["ok"]
    assert len(lane.spawned) == 1 and owned.job.forced == 0
    if fault == "changed_record":
        record["nonce"] = value.nonce
        write(value.paths["admission"], record)
    value.identity = real_identity
    monkeypatch.setattr(protocol, "write_record", write)
    monkeypatch.setattr(protocol, "read_record", read)
    assert value.drained() and value.owned is None


@pytest.mark.parametrize("fault", ["write_after", "readback", "spawn_after_creation"])
def test_launch_reservation_uncertainty_blocks_without_fabricating_empty(lane, monkeypatch, fault):
    value = lane.lane
    write, read = protocol.write_record, protocol.read_record
    calls = []
    def uncertain_write(path, record):
        write(path, record)
        if path == value.paths["release"] and fault == "write_after":
            raise PermissionError("generated reservation write ambiguity")
    def uncertain_read(path, *args, **kwargs):
        record = read(path, *args, **kwargs)
        if path == value.paths["release"] and record is not None and fault == "readback":
            raise PermissionError("generated reservation readback ambiguity")
        return record
    def uncertain_spawn(*args, **kwargs):
        calls.append(1)
        assert read(value.paths["release"])["empty"] is False
        raise OSError("simulated creation followed by lost handle; no actual process")
    monkeypatch.setattr(protocol, "write_record", uncertain_write)
    monkeypatch.setattr(protocol, "read_record", uncertain_read)
    lane.manager.spawn = uncertain_spawn
    for _ in range(4):
        value.tick()
        lane.clock[0] += 31
        assert not value.retry()["ok"]
    assert len(calls) == int(fault == "spawn_after_creation") and lane.spawned == []
    assert value.snapshot()["resource_held"] and not value.drained() and value.owned is None
    assert read(value.paths["release"])["empty"] is False


@pytest.mark.parametrize("fault", ["fingerprint", "scope", "nonce", "alive_root", "unknown_worker"])
def test_release_cannot_authorize_changed_evidence_or_unresolved_identity(lane, fault):
    value = lane.lane
    admitted(lane)
    status(lane)
    owned = value.owned
    owned.proc.exit_code, owned.job.count = 0, 0
    assert value.drained()
    release = protocol.read_record(value.paths["release"])
    if fault == "fingerprint":
        record = protocol.read_record(value.paths["status"])
        record["sequence"] += 1
        protocol.write_record(value.paths["status"], record)
    elif fault == "scope":
        release["scope"] = {"different": "generated"}
    elif fault == "nonce":
        release["nonce"] = "another-boot"
    elif fault == "alive_root":
        owned.job.count = 1
    else:
        lane.registry[owned.proc.pid] = {"state": "unknown", "pid": owned.proc.pid}
    protocol.write_record(value.paths["release"], release)
    fresh = successor(lane)
    fresh.tick()
    assert len(lane.spawned) == 1 and fresh.owned is None and fresh.snapshot()["resource_held"]
    assert not fresh.retry()["ok"] and not fresh.drained()


def test_status_sequence_ttl_and_real_progress_are_independent(lane):
    value = lane.lane
    admitted(lane)
    status(lane, state="processing", current={"tag": "generated", "date": "2000-01-01", "segment": "seg_0001.flv", "phase": "recognizing"})
    value.tick()
    assert value.snapshot()["verified"]
    lane.clock[0] += 16
    lane.wall[0] += 16
    value.tick()  # Re-reading the same heartbeat does not renew snapshot TTL.
    assert value.snapshot()["stale"] and not value.snapshot()["verified"]
    lane.clock[0] += 105
    lane.wall[0] += 105
    status(lane, 2, state="processing", last_progress_at=2000., current={"phase": "recognizing"})
    value.tick()
    snap = value.snapshot()
    assert snap["verified"] and not snap["stale"] and snap["no_progress"]
    assert snap["resource_held"] and value.owned.job.forced == 0
    status(lane, 1)
    value.tick()
    assert not value.snapshot()["verified"] and value.state == "snapshot_unconfirmed"


def test_bounded_future_retry_time_is_visible_but_future_status_is_not(lane):
    value = lane.lane
    admitted(lane)
    record = status(lane, state="backoff", next_retry_at=lane.wall[0] + 300)
    value.tick()
    assert value.snapshot()["next_retry_at"] == lane.wall[0] + 300
    record.update(sequence=2, updated_at=lane.wall[0] + 100)
    protocol.write_record(value.paths["status"], record)
    value.tick()
    assert not value.snapshot()["verified"] and value.state == "snapshot_unconfirmed"


def test_snapshot_is_bounded_safe_and_does_not_wait_for_metadata_io(lane, monkeypatch):
    value = lane.lane
    admitted(lane)
    status(lane, error="SECRET-PATH-TOKEN", current={"tag": "../private", "segment": "C:/private", "phase": "SECRET"},
           arbitrary="SECRET", transcript=["SECRET"] * 100)
    value.tick()
    raw = json.dumps(value.snapshot())
    assert "SECRET" not in raw and "private" not in raw and len(raw) < 2500
    entered, release = threading.Event(), threading.Event()
    read = protocol.read_record
    def slow(path, *args, **kwargs):
        if path == value.paths["ready"]:
            entered.set()
            assert release.wait(2)
        return read(path, *args, **kwargs)
    monkeypatch.setattr(protocol, "read_record", slow)
    worker = threading.Thread(target=value.tick)
    worker.start()
    try:
        assert entered.wait(1)
        started = time.monotonic()
        assert value.snapshot()["resource_held"]
        assert time.monotonic() - started < .1
        value.begin_stop()  # It cannot wait on this metadata reader either.
        assert value.snapshot()["state"] == "stopping" and not value.snapshot()["verified"]
    finally:
        release.set()
        worker.join(2)
        assert not worker.is_alive()


def test_role_manager_empty_roster_still_owns_lane_and_stop_drains(lane, monkeypatch):
    monkeypatch.setattr(app, "process_identity", lane.identity)
    manager = app.RoleManager(lane.context, spawn=lane.manager.spawn, now=lane.manager.now)
    manager.tick({"anchors": []})
    assert manager.roles == {} and manager.asr.owned is not None
    assert manager.snapshot()["asr"]["resource_held"]
    manager.begin_stop()
    assert not manager.drained()
    manager.asr.owned.job.count = 0
    manager.tick({"anchors": []})
    assert manager.drained()


def test_application_status_does_not_revive_asr_from_a_dead_or_stale_parent(lane, monkeypatch):
    owner = {"state": "alive", "pid": 999, "created": 10.}
    path = app.runtime_dir(lane.context) / "instance.json"
    value = {"owner": owner, "context": lane.context, "instance_id": lane.context["instance_id"],
             "state": "running", "updated_at": time.time(), "roles": {},
             "asr": {"state": "processing", "verified": True, "stale": False, "resource_held": True,
                     "counts": {"pending": 0, "complete": 2}}}
    app.atomic_json(path, value)
    monkeypatch.setattr(app, "process_fact", lambda _owner: "dead")
    old = app.status(lane.context, probe=False)
    assert old["state"] == "exited" and old["asr"]["state"] == "snapshot_unconfirmed"
    assert not old["asr"]["verified"] and old["asr"]["stale"] and old["asr"]["counts"]["pending"] is None
    monkeypatch.setattr(app, "process_fact", lambda _owner: "alive")
    value["updated_at"] = time.time() - 100
    app.atomic_json(path, value)
    assert not app.status(lane.context, probe=False)["asr"]["verified"]


@pytest.mark.skipif(os.name != "nt", reason="uses the actual Windows private Job")
def test_exact_owned_toy_descendant_keeps_lane_occupied_after_root_exit(tmp_path):
    context = make_context(tmp_path)
    directory = Path(context["data"])
    ready, go = directory / "toy-ready", directory / "toy-exit"
    script = """import pathlib,subprocess,sys,time
ready,go=map(pathlib.Path,sys.argv[1:])
ready.write_text('ready',encoding='utf-8')
while not go.exists(): time.sleep(.02)
subprocess.Popen([sys.executable,'-c','import time; time.sleep(2.5)'],creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
"""
    owned = process.OwnedProcess.start([context["python"], "-B", "-c", script, str(ready), str(go)],
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), cwd=directory, log_path=directory / "toy.log")
    try:
        started = time.monotonic()
        while not ready.exists() and time.monotonic() - started < 5:
            time.sleep(.02)
        assert ready.exists()
        manager = SimpleNamespace(context=context, directory=app.runtime_dir(context), stopping=False,
            now=time.monotonic, spawn=lambda *_args, **_kwargs: owned,
            child_env=lambda: {"DY_CUSTOMER_CONTEXT": str(app.runtime_dir(context) / "context.json")})
        value = AsrLane(manager)
        value.tick()
        go.write_text("exit", encoding="utf-8")
        until = time.monotonic() + 4
        while owned.proc.poll() is None and time.monotonic() < until:
            time.sleep(.02)
        assert owned.proc.poll() is not None and owned.active_count() > 0
        value.tick()
        assert value.state == "draining" and value.snapshot()["resource_held"] and not value.drained()
        manager.stopping = True
        value.begin_stop()
        until = time.monotonic() + 5
        while owned.active_count() != 0 and time.monotonic() < until:
            time.sleep(.02)
        assert owned.active_count() == 0 and value.drained() and owned.closed
    finally:
        if not owned.closed:
            assert owned.close(force=True), "only this exact test Job may be force-cleaned"
