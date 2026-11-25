"""Generated state, native short processes and fake SDK only; no live services."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from scripts import backfill_tasks as tasks

ROOT = Path(tasks.__file__).resolve().parents[1]


@pytest.fixture
def setup(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"llm": {"enabled": True, "active_provider": "demo",
        "providers": {"demo": {"api_key": "synthetic-key", "base_url": "https://fake.invalid/v1",
                               "model": "synthetic-model", "protocol": "openai_compatible"}}}}))
    return tasks.TaskStore(config_path=config, data_dir=data), config, data


def reserve(store, request="request-first-0001", **kwargs):
    return store.reserve_inline("synthetic", "2026-11-15", request_id=request, **kwargs)


def test_concurrent_reservation_reuses_and_aliases(setup):
    store, _, _ = setup
    with ThreadPoolExecutor(max_workers=2) as executor:
        rows = list(executor.map(lambda index: reserve(store, f"request-thread-{index:04}"), range(2)))
    assert sum(row[0]["created"] for row in rows) == 1
    assert len({row[0]["task_id"] for row in rows}) == 1
    for index in range(2):
        assert store.status(tag="synthetic", date="2026-11-15", request_id=f"request-thread-{index:04}")["task_id"] == rows[0][0]["task_id"]


def test_normal_terminal_reuse_force_history_and_public_schema(setup):
    store, _, _ = setup
    response, run = reserve(store)
    assert run.guard() and run.before_send("demo", "synthetic-model")
    assert store.status(task_id=run.task_id)["may_have_sent"]
    run.progress(counts={"ok": 1, "calls": 1, "windows_seen": 3, "windows_total": 3},
        input_snapshot={"records": 4, "invalid_records": 1, "ts_min": 1, "ts_max": 2,
                        "captured_at": 3, "sha256": "0" * 64, "bytes": 99, "input_ref": "synthetic.jsonl"},
        output_ref={"path": "portrait.jsonl", "rows": 1, "byte_start": 0, "byte_end": 10},
        current_window_ts=2, processed_ts_end=2, dispatch_settled=True)
    done = run.finish("succeeded", "completed")
    assert done["input"]["event_count"] == 4 and done["input"]["invalid_count"] == 1
    assert done["output"]["append"] is True and done["output"]["task_id"] == run.task_id
    assert done["counts"]["generate_calls"] == 1 and done["counts"]["windows"] == 3
    assert done["processed_ts_end"] == done["current_window_ts"] == 2
    assert done["processed"]["processed_ts_end"] == done["processed"]["current_window_ts"] == 2
    assert reserve(store, "request-ordinary-02")[1] is None
    forced, next_run = reserve(store, "request-force-0002", force=True, parameters={"max_calls": 2})
    assert next_run is not None and forced["task_id"] != response["task_id"]
    assert store.status(task_id=response["task_id"])["state"] == "succeeded"
    next_run.finish("failed", "no_output", dispatch_settled=True)
    assert reserve(store, "request-force-0002", force=True)[0]["task_id"] == next_run.task_id
    with pytest.raises(tasks.TaskError, match="任务已经结束"):
        run.progress(counts={"ok": 99}, output_ref={"path": "must-not-replace"})
    assert store.status(task_id=run.task_id)["counts"]["ok"] == 1
    assert store.status(task_id=run.task_id)["output"]["ref"] == "portrait.jsonl"
    assert len(json.loads(store._path(run.task_id).read_text())["runs"]) == 2


def test_alias_case_parameters_and_resource_mismatch(setup):
    store, _, _ = setup
    first, _ = store.reserve_inline("Synthetic", "2026-11-15", request_id="request-alias-0001")
    second, other = store.reserve_inline("synthetic", "2026-11-15", parameters={"max_calls": 9}, request_id="request-alias-0002")
    assert other is None and second["task_id"] == first["task_id"]
    with pytest.raises(tasks.TaskNotFound):
        store.status(task_id=first["task_id"], tag="other", date="2026-11-15")


def test_internal_ascii_space_tag_reserves_same_resource(setup):
    store, _, _ = setup
    first, run = store.reserve_inline(" Synthetic Room ", "2026-11-15",
                                      request_id="request-space-first")
    second, other = store.reserve_inline("synthetic room", "2026-11-15",
                                         request_id="request-space-second")
    assert run is not None and other is None
    assert first["task_id"] == second["task_id"]
    assert first["task"]["tag"] == "Synthetic Room"
    assert store.status(tag="synthetic room", date="2026-11-15")["task_id"] == run.task_id


@pytest.mark.parametrize("field,value", [("enabled", False), ("timeout_seconds", 31)])
def test_permission_or_credentials_change_is_sticky(setup, field, value):
    store, config, data = setup
    _, run = reserve(store)
    if field == "enabled":
        (data / "llm_runtime.json").write_text('{"enabled":false,"pause_generation":1}')
    else:
        raw = json.loads(config.read_text())
        raw["llm"][field] = value
        config.write_text(json.dumps(raw))
    assert not run.guard() and run.stop_reason
    (data / "llm_runtime.json").write_text('{"enabled":true,"pause_generation":1}')
    assert not run.guard() and not run.before_send()
    assert run.finish("blocked", run.stop_reason, dispatch_settled=True)["state"] == "blocked"


def test_dispatch_marker_write_failure_blocks_and_bad_manifest_is_preserved(setup, monkeypatch):
    store, _, _ = setup
    _, run = reserve(store)
    monkeypatch.setattr(store, "_write", lambda *values: (_ for _ in ()).throw(tasks.StateWriteError()))
    assert run.before_send() is False and run.stop_reason == "task_state_unavailable"
    monkeypatch.undo()
    path = store._path(run.task_id)
    path.write_text('{"broken":')
    with pytest.raises(tasks.TaskError) as error:
        reserve(store, "request-corrupt-0002", force=True)
    assert error.value.code == "task_state_unavailable" and path.read_text() == '{"broken":'
    assert run.guard() is False


def test_unknown_owner_blocks_force_and_pid_reuse_is_not_current_owner(setup):
    store, _, _ = setup
    _, run = reserve(store)
    worker = {"state": "alive", "pid": 456789, "created": 10.0}
    store._update(run.task_id, lambda row: row.update(owner=worker))
    real = store._identity_reader
    store._identity_reader = lambda pid: ({"state": "unknown", "pid": pid, "created": None}
                                         if pid == worker["pid"] else real(pid))
    assert store.status(task_id=run.task_id)["owner_active"] is None
    assert reserve(store, "request-unknown-0002", force=True)[1] is None
    store._identity_reader = lambda pid: ({"state": "alive", "pid": pid, "created": 10.000001}
                                         if pid == worker["pid"] else real(pid))
    status = store.status(task_id=run.task_id)
    assert status["state"] == "interrupted" and status["owner_active"] is False
    assert reserve(store, "request-reusedpid-003", force=True)[1] is not None


def test_legacy_output_is_not_success_and_unowned_pid_blocks_force(setup):
    store, _, data = setup
    output = data / "portrait_synthetic_2026-11-15.jsonl"
    output.write_text('legacy\n')
    result, run = reserve(store)
    assert run is None and result["task"]["state"] == "legacy_unverified"
    assert result["task"]["task_id"] is None
    pid = data / "backfill_synthetic_2026-11-15.pid"
    pid.write_text(str(os.getpid()))
    assert reserve(store, "request-legacy-force", force=True)[1] is None
    assert store.status(tag="synthetic", date="2026-11-15")["can_redo"] is False
    assert output.read_text() == 'legacy\n'


def test_legacy_pid_uses_literal_tag_and_known_dead_can_redo(setup):
    store, _, data = setup
    tag = "synthetic[x]"
    pid = data / f"backfill_{tag}_2026-11-15.pid"
    pid.write_text(str(os.getpid()))
    response, run = store.reserve_inline(tag, "2026-11-15", force=True)
    assert run is None and response["task"]["can_redo"] is False
    real = store._identity_reader
    store._identity_reader = lambda number: ({"state": "dead", "pid": number, "created": None}
                                              if number == 4000000000 else real(number))
    pid.write_text("4000000000")
    assert store.status(tag=tag, date="2026-11-15")["can_redo"] is True


def test_explicit_inline_force_without_request_id_is_new_intent(setup):
    store, _, _ = setup
    _, run = reserve(store)
    run.finish("succeeded", counts={"ok": 1}, dispatch_settled=True)
    response, next_run = store.reserve_inline("synthetic", "2026-11-15", force=True)
    assert next_run is not None and response["task_id"] != run.task_id


@pytest.mark.parametrize("wrong", ["unrelated", "parent_reused"])
def test_correct_token_unrelated_candidate_or_reused_parent_is_not_acked(setup, wrong):
    store, _, _ = setup
    original = store._identity_reader
    now = [time.time()]
    parent = {"state": "alive", "pid": 450001, "created": 10.0}
    child = {"state": "alive", "pid": 450002, "created": 11.0}
    store._now = lambda: now[0]
    store._identity_reader = lambda pid: dict(parent) if pid == parent["pid"] else dict(child) if pid == child["pid"] else original(pid)
    def parent_reader(pid):
        now[0] += 11
        if wrong == "parent_reused":
            parent["created"] += 1
            return parent["pid"]
        return 450003
    store._parent_reader = parent_reader
    def spawn(command, *, env):
        task_id = command[command.index("--task-id") + 1]
        def claim(row):
            assert row["lease"]["token"] == env[tasks.LAUNCH_ENV]
            row["lease"]["candidate"] = dict(child)
        store._update(task_id, claim)
        return type("Process", (), {"pid": parent["pid"]})()
    store._spawn_override = spawn
    response = store.submit("synthetic", "2026-11-15", request_id="request-wrong-parent")
    assert response["started"] is False and response["task"]["state"] == "unknown"
    manifest = json.loads(store._path(response["task_id"]).read_text())
    assert manifest["runs"][0]["lease"]["ack"] is False


def test_spawn_identity_is_not_rebound_to_recycled_parent_on_later_poll(setup):
    store, _, _ = setup
    original_identity, original_update = store._identity_reader, store._update
    now, polls = [time.time()], [0]
    parent = {"state": "alive", "pid": 460001, "created": 20.0}
    child = {"state": "alive", "pid": 460002, "created": 40.0}
    store._now = lambda: now[0]
    store._identity_reader = lambda pid: dict(parent) if pid == parent["pid"] else dict(child) if pid == child["pid"] else original_identity(pid)
    store._parent_reader = lambda pid: parent["pid"]
    def update(task_id, change):
        row = original_update(task_id, change)
        if change.__name__ == "acknowledge":
            polls[0] += 1
            if polls[0] == 1:
                parent["created"] = 30.0
                original_update(task_id, lambda item: item["lease"].update(candidate=dict(child)))
            elif polls[0] == 2:
                now[0] += 11
        return row
    store._update = update
    store._spawn_override = lambda command, **options: type("Process", (), {"pid": parent["pid"]})()
    response = store.submit("synthetic", "2026-11-15", request_id="request-parent-multi-poll")
    assert response["started"] is False and polls[0] >= 2
    assert json.loads(store._path(response["task_id"]).read_text())["runs"][0]["lease"]["ack"] is False


@pytest.mark.parametrize("paused", [False, True])
def test_control_change_after_ack_claim_records_finished_blocked_zero_dispatch(setup, paused):
    store, _, data = setup
    response, pending = store._reserve("synthetic", "2026-11-15", False, None, "request-ack-control", False)
    identity = store._self()
    def ack(row):
        row["lease"].update(ack=True, candidate=identity, expected=identity)
        row["owner"] = identity
    store._update(response["task_id"], ack)
    (data / "llm_runtime.json").write_text(json.dumps({"enabled": not paused, "pause_generation": 1}))
    with pytest.raises(tasks.TaskError):
        store.claim(response["task_id"], pending._token, timeout=.1)
    result = store.status(task_id=response["task_id"])
    assert result["state"] == "blocked" and result["finished_at"] is not None
    assert result["reason"] == ("global_paused" if paused else "control_changed")
    assert result["dispatch_count"] == 0 and not result["may_have_sent"]


@pytest.mark.parametrize("pending", [False, True])
def test_confirmed_dead_owner_classifies_settled_interrupted_or_pending_unknown(setup, pending):
    store, _, _ = setup
    _, run = reserve(store)
    if pending:
        assert run.before_send()
    store._identity_reader = lambda pid: {"state": "dead", "pid": pid, "created": None}
    result = store.status(task_id=run.task_id)
    assert result["state"] == ("unknown" if pending else "interrupted")
    assert result["finished_at"] is not None
    assert store.status(task_id=run.task_id)["finished_at"] == result["finished_at"]


def test_transient_unknown_identity_does_not_seal_inflight_result(setup):
    store, _, _ = setup
    _, run = reserve(store)
    assert run.before_send()
    original = store._identity_reader
    store._identity_reader = lambda pid: {"state": "unknown", "pid": pid, "created": None}
    result = store.status(task_id=run.task_id)
    assert result["state"] == "unknown" and result["finished_at"] is None
    store._identity_reader = original
    run.progress(counts={"ok": 1}, dispatch_settled=True)
    result = run.finish("partial", "completed")
    assert result["state"] == "partial" and result["counts"]["ok"] == 1
    assert result["finished_at"] is not None


def child_env():
    return dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
                PYTHONPATH=os.pathsep.join([str(ROOT), str(ROOT / "scripts")]))


def popen(command, **kwargs):
    return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **kwargs)


def test_two_native_processes_api_cli_reserve_one_resource(setup, tmp_path):
    store, config, data = setup
    helper = tmp_path / "reserve_toy.py"
    helper.write_text('''import json,sys,time
from pathlib import Path
from backfill_tasks import TaskStore
store=TaskStore(config_path=sys.argv[1],data_dir=sys.argv[2])
start,release,result=map(Path,sys.argv[3:6])
until=time.monotonic()+5
while not start.exists() and time.monotonic()<until: time.sleep(.01)
if sys.argv[6]=='api': row,run=store._reserve('synthetic','2026-11-15',False,None,'request-process-api1',False)
else: row,run=store.reserve_inline('synthetic','2026-11-15',force=True,request_id='request-process-cli1')
result.write_text(json.dumps({'created':row['created'],'task_id':row['task_id']}))
while not release.exists() and time.monotonic()<until: time.sleep(.01)
''', encoding="utf-8")
    start, release = tmp_path / "start", tmp_path / "release"
    results = [tmp_path / "api.json", tmp_path / "cli.json"]
    processes = [popen([sys.executable, "-B", str(helper), str(config), str(data), str(start),
                        str(release), str(result), mode], env=child_env())
                 for result, mode in zip(results, ("api", "cli"))]
    try:
        start.write_text("go")
        until = time.monotonic() + 4
        while not all(path.exists() for path in results) and time.monotonic() < until:
            time.sleep(.01)
        assert all(path.exists() for path in results)
        rows = [json.loads(path.read_text()) for path in results]
        assert len({row["task_id"] for row in rows}) == 1
        assert sum(row["created"] for row in rows) == 1
    finally:
        release.write_text("done")
        for process in processes:
            stdout, stderr = process.communicate(timeout=6)
            assert process.returncode == 0, (stdout, stderr)


def test_unacked_real_child_never_gets_dispatch_lease(setup, tmp_path):
    store, config, data = setup
    response, pending = store._reserve("synthetic", "2026-11-15", False, None, "request-unacked-001", False)
    helper = tmp_path / "unacked_toy.py"
    helper.write_text('''import sys
from backfill_tasks import TaskStore,TaskError
store=TaskStore(config_path=sys.argv[1],data_dir=sys.argv[2])
try: store.claim(sys.argv[3],timeout=.1)
except TaskError: raise SystemExit(0)
raise SystemExit(7)
''')
    process = popen([sys.executable, "-B", str(helper), str(config), str(data), response["task_id"]],
                    env=dict(child_env(), **{tasks.LAUNCH_ENV: pending._token}))
    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 0, (stdout, stderr)
    status = store.status(task_id=response["task_id"])
    assert status["state"] == "unknown" and status["dispatch_count"] == 0 and not status["may_have_sent"]
    with pytest.raises(tasks.TaskError):
        store.claim(response["task_id"], pending._token, timeout=0)


@pytest.mark.parametrize("crash", [False, True])
def test_actual_cli_child_ack_fake_sdk_and_crash_unknown(setup, tmp_path, crash):
    store, config, data = setup
    epoch = int(datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp())
    records = [{"ts": epoch, "type": "danmaku", "text": "synthetic first", "room_id": "synthetic-room"},
               {"ts": epoch + 5, "type": "like", "room_id": "synthetic-room"},
               {"ts": epoch + 130, "type": "danmaku", "text": "synthetic next", "room_id": "synthetic-room"},
               {"ts": epoch + 135, "type": "like", "room_id": "synthetic-room"}]
    (data / "danmu_douyin_synthetic_2026-11-15.jsonl").write_text("".join(json.dumps(row) + "\n" for row in records))
    calls = tmp_path / "sdk-calls.jsonl"
    wrapper = tmp_path / "actual_cli_fake.py"
    wrapper.write_text('''import http.client,ssl,socket,sys,os,json,runpy
from pathlib import Path
from types import ModuleType,SimpleNamespace
def deny(*args,**kwargs): raise AssertionError('network forbidden')
socket.socket=socket.create_connection=socket.getaddrinfo=deny
class SDK:
 def __init__(self,api_key,base_url,**kwargs):
  assert api_key=='synthetic-key' and base_url=='https://fake.invalid/v1'
  self.chat=SimpleNamespace(completions=SimpleNamespace(create=self.create))
 def create(self,**kwargs):
  task=sys.argv[sys.argv.index('--task-id')+1]
  data=Path(sys.argv[sys.argv.index('--data-dir')+1])
  manifest=json.loads((data/'backfill_tasks'/(task.split('_')[1]+'.json')).read_text())
  row=next(r for r in manifest['runs'] if r['task_id']==task)
  assert row['may_have_sent'] and not row['dispatch_settled']
  with open(os.environ['REVIEW_CALLS'],'a') as stream: stream.write(json.dumps({'task_id':task})+'\\n')
  if os.environ['REVIEW_CRASH']=='1': os._exit(23)
  return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({'interest_topics':['synthetic']})))],usage=SimpleNamespace(prompt_tokens=2,completion_tokens=1))
 def close(self): pass
fake=ModuleType('openai');fake.OpenAI=SDK;sys.modules['openai']=fake
original=os.environ['REVIEW_ENTRY']
sys.argv=[original]+sys.argv[1:]
runpy.run_path(original,run_name='__main__')
''', encoding="utf-8")
    processes = []
    def spawn(command, *, env):
        assert "--tag" not in command and "--date" not in command
        assert Path(command[command.index("--config") + 1]) == config
        assert Path(command[command.index("--data-dir") + 1]) == data
        assert env[tasks.LAUNCH_ENV] not in command
        process = popen([sys.executable, "-B", str(wrapper), *command[2:]], env=dict(
            env, **child_env(), REVIEW_ENTRY=command[1], REVIEW_CALLS=str(calls), REVIEW_CRASH="1" if crash else "0"))
        processes.append(process)
        return process
    store._spawn_override = spawn
    response = store.submit("synthetic", "2026-11-15", request_id="request-real-cli-01")
    stdout, stderr = processes[0].communicate(timeout=8)
    assert response["created"] and response["started"], (stdout, stderr)
    assert processes[0].returncode == (23 if crash else 0), (stdout, stderr)
    status = store.status(task_id=response["task_id"])
    assert calls.exists() and len(calls.read_text().splitlines()) == 1
    assert status["state"] == ("unknown" if crash else "succeeded")
    again = store.submit("synthetic", "2026-11-15", request_id="request-real-cli-02")
    assert again["reused"] and again["task_id"] == response["task_id"] and len(processes) == 1
    if not crash:
        output = data / "portrait_synthetic_2026-11-15.jsonl"
        assert json.loads(output.read_text().splitlines()[0])["task_id"] == response["task_id"]
        assert status["counts"]["ok"] == 1 and status["output"]["rows"] == 1
