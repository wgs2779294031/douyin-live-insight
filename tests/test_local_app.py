"""Generated local contexts, real detached Board/toy Jobs, no accounts or model I/O."""
import concurrent.futures
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT)]
import local_app as app
import anchor_manager
from test_llm_lifecycle import sandbox


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_context(tmp_path, ports=None):
    data, dist = tmp_path / "数据 空间", tmp_path / "前端 静态"
    data.mkdir(parents=True)
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>isolated Board</title>", encoding="utf-8")
    config, roster = tmp_path / "主 配置.json", tmp_path / "名册.json"
    config.write_text(json.dumps({"llm": {"enabled": False, "providers": {}}, "rooms": []}), encoding="utf-8")
    roster.write_text('{"version":1,"anchors":[]}', encoding="utf-8")
    port, cdp_port = ports or (free_port(), free_port())
    context = app.context_from_args(SimpleNamespace(root=str(ROOT), data=str(data), config=str(config),
        roster=str(roster), dist=str(dist), python=sys.executable, port=port, cdp_port=cdp_port))
    app.atomic_json(app.runtime_dir(context) / "context.json", context)
    return context


def wait_for(predicate, seconds=12):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError("bounded test wait expired")


def local_headers(port):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
    try:
        connection.request("POST", "/api/local/session", body="{}", headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        result = json.loads(response.read())
        assert response.status == 200
        return {"Cookie": response.getheader("Set-Cookie").split(";", 1)[0],
                "X-Local-Write-Token": result["write_token"]}
    finally:
        connection.close()


def cli(context, action):
    command = [sys.executable, "-X", "utf8", "-B", str(ROOT / "scripts/local_app.py"), action]
    for name in ("root", "data", "config", "roster", "dist", "python", "port", "cdp_port"):
        command += ["--" + name.replace("_", "-"), str(context[name])]
    result = subprocess.run(command, cwd=str(context["data"]), capture_output=True,
        encoding="utf-8", timeout=25, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.stdout.strip(), result.stderr
    return result.returncode, json.loads(result.stdout.splitlines()[-1])


@pytest.mark.parametrize("missing,code", [("python", "python_missing"), ("config", "config_missing"),
    ("roster", "roster_missing"), ("dist", "dist_missing")])
def test_missing_context_is_explicit_and_does_not_launch(tmp_path, missing, code):
    context = make_context(tmp_path)
    context[missing] = str(tmp_path / "missing")
    with pytest.raises(app.AppError, match=code):
        app.check_configuration(context)


def test_empty_roster_and_no_key_do_not_create_watch_or_paid_work(tmp_path):
    context = make_context(tmp_path)
    before = Path(context["config"]).read_bytes()
    config, roster = app.check_configuration(context)
    manager = app.RoleManager(context, command_builder=lambda *_: pytest.fail("empty roster cannot launch"))
    manager.tick(roster)
    assert manager.snapshot()["roles"] == {} and manager.snapshot()["manual_writers"] == 0
    assert Path(context["config"]).read_bytes() == before


@pytest.mark.skipif(os.name != "nt", reason="Windows non-delete-sharing readers")
def test_atomic_publication_waits_for_brief_external_reader(tmp_path):
    path = tmp_path / "owned-state.json"
    app.atomic_json(path, {"value": 1})
    reader = path.open("rb")
    closed = threading.Event()
    def release():
        time.sleep(.1)
        reader.close()
        closed.set()
    worker = threading.Thread(target=release)
    worker.start()
    app.atomic_json(path, {"value": 2})
    worker.join(2)
    assert closed.is_set() and app.read_json(path) == {"value": 2}


@pytest.mark.skipif(os.name != "nt", reason="Windows customer lifecycle")
@pytest.mark.parametrize("callers", [2, 4])
def test_detached_single_board_survives_callers_and_concurrent_start(tmp_path, callers):
    context = make_context(tmp_path)
    source_bytes = {name: Path(context[name]).read_bytes() for name in ("config", "roster")}
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=callers) as workers:
            results = list(workers.map(lambda _: cli(context, "start"), range(callers)))
        assert all(code == 0 for code, _ in results), results
        owners = [value["owner"] for _, value in results]
        assert owners[0] == owners[1] and owners[0]["pid"] != os.getpid()
        connection = http.client.HTTPConnection("127.0.0.1", context["port"], timeout=3)
        connection.request("GET", "/")
        response = connection.getresponse()
        assert response.status == 200 and b"isolated Board" in response.read()
        connection.close()  # Closing the page/client does not stop the app.
        for _ in range(10):
            assert app.status(context)["ok"]
            time.sleep(.03)
        value = app.status(context)
        assert value["ok"] and value["owner"] == owners[0] and value["roles"] == {}
        connection = http.client.HTTPConnection("127.0.0.1", context["port"], timeout=3)
        connection.request("GET", "/api/anchors")
        response = connection.getresponse()
        assert response.status == 401
        response.read()
        connection.close()
        assert cli(context, "recover")[1]["owner"] == owners[0]
    finally:
        code, stopped = cli(context, "stop")
        assert code == 0 and stopped["ok"] and stopped["state"] == "stopped", stopped
        wait_for(lambda: app.process_fact(stopped["owner"]) == "dead")
    assert all(Path(context[name]).read_bytes() == value for name, value in source_bytes.items())
    assert not list(Path(context["data"]).glob("llm_metrics*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows TCP ownership")
def test_foreign_port_is_never_reused_or_stopped(tmp_path):
    context = make_context(tmp_path)
    with socket.socket() as foreign:
        foreign.bind(("127.0.0.1", context["port"]))
        foreign.listen()
        value = app.start(context, wait=.2)
        assert value["state"] == "port_conflict"
        assert foreign.getsockname()[1] == context["port"]
        assert not (app.runtime_dir(context) / "instance.json").exists()


def test_stale_dead_or_unknown_receipts_never_expose_old_running(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    owner = {"state": "alive", "pid": 123, "created": 100.0}
    app.atomic_json(app.runtime_dir(context) / "instance.json", {"context": context,
        "instance_id": context["instance_id"], "owner": owner, "updated_at": time.time(),
        "state": "running", "roles": {"watch:synthetic": {"running": True, "state": "running"}}})
    for fact in ("dead", "unknown", "reused"):
        monkeypatch.setattr(app, "process_fact", lambda _, fact=fact: fact)
        value = app.status(context, probe=False)
        assert value["state"] != "running"
        assert not value["roles"]["watch:synthetic"]["running"]
    changed = dict(context, port=free_port())
    assert app.status(changed)["state"] == "instance_context_conflict"


def test_spawn_exceptions_back_off_and_fuse_until_explicit_recover(tmp_path):
    context = make_context(tmp_path)
    calls, clock = [], [0.0]
    def denied(*args, **kwargs):
        calls.append(True)
        raise OSError("synthetic launch failure")
    manager = app.RoleManager(context, spawn=denied, now=lambda: clock[0], command_builder=lambda *_: ["toy"])
    roster = {"anchors": [{"tag": "synthetic", "enabled": True, "watch": True}]}
    for index in range(20):
        manager.tick(roster)
    assert len(calls) == 1
    for stamp in (3, 8, 20, 100):
        clock[0] = stamp
        manager.tick(roster)
    assert len(calls) == 3
    assert manager.snapshot()["roles"]["watch:synthetic"]["state"] == "restart_limited"
    assert manager.recover()
    manager.tick(roster)
    assert len(calls) == 4


def test_individual_user_stop_survives_reconcile_recover_and_app_restart(tmp_path):
    context = make_context(tmp_path)
    calls = []
    def denied(*args, **kwargs):
        calls.append(True)
        raise OSError("synthetic failure")
    manager = app.RoleManager(context, spawn=denied, command_builder=lambda *_: ["toy"])
    manager.stop_role("watch", "synthetic")
    entry = {"tag": "synthetic", "enabled": True, "watch": True}
    roster = {"anchors": [entry]}
    manager.tick(roster)
    manager.recover()
    manager.tick(roster)
    restarted = app.RoleManager(context, spawn=denied, command_builder=lambda *_: ["toy"])
    restarted.tick(roster)
    assert calls == [] and restarted.suppressed == {"watch:synthetic"}
    restarted.start_role("watch", entry)  # Only an explicit start releases that stop.
    assert calls == [True] and restarted.suppressed == set()


@pytest.mark.parametrize("method,path", [("POST", "/synthetic"), ("PATCH", "/synthetic"),
    ("DELETE", "/synthetic"), ("GET", "/api/review/backfill?task_id=synthetic")])
def test_writing_requests_remain_inflight_during_stop(tmp_path, monkeypatch, method, path):
    import assist_board_server as board
    import anchor_roster
    context = make_context(tmp_path)
    manager = app.RoleManager(context, command_builder=lambda *_: ["unused"])
    entered, release = threading.Event(), threading.Event()
    for module, name in ((anchor_roster, "ROSTER_PATH"), (anchor_manager, "ROOT"),
                         (anchor_manager, "DATA"), (anchor_manager, "PY")):
        monkeypatch.setattr(module, name, getattr(module, name))
    def held(self):
        entered.set()
        release.wait(3)
        return self._json(200, {"synthetic": True})
    monkeypatch.setattr(board.Handler, "do_" + method, held)
    server, thread, broadcaster = app.create_board(context, manager, app.process_identity(os.getpid()))
    headers = local_headers(context["port"])
    def request():
        conn = http.client.HTTPConnection("127.0.0.1", context["port"], timeout=4)
        try:
            conn.request(method, path, body="{}" if method != "GET" else None, headers=headers)
            response = conn.getresponse()
            response.read()
            return response.status
        finally:
            conn.close()
    try:
        with concurrent.futures.ThreadPoolExecutor(1) as workers:
            pending = workers.submit(request)
            assert entered.wait(2)
            manager.begin_stop()
            assert manager.inflight == 1 and not manager.drained()
            release.set()
            assert pending.result() == 200
        assert manager.inflight == 0 and manager.drained()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        broadcaster.stop()
        thread.join(2)
        anchor_manager.configure_customer(None)


def toy_script(tmp_path):
    script = tmp_path / "角色 toy.py"
    script.write_text("""import os,sys,time,subprocess
from pathlib import Path
sys.path[:0] = [sys.argv[1] + '/scripts', sys.argv[1]]
from local_context import load_customer_context,publish_role_ready,customer_stop_requested
ctx = load_customer_context()
identity = publish_role_ready(ctx, sys.argv[2], 'synthetic')
while not customer_stop_requested(ctx, identity): time.sleep(.03)
if sys.argv[2] == 'record':
    subprocess.Popen([sys.executable,'-B','-c',"import time;from pathlib import Path;time.sleep(.8);Path("+repr(sys.argv[3])+").write_text('complete')"])
""", encoding="utf-8")
    return script


@pytest.mark.skipif(os.name != "nt", reason="real owned Windows trees")
@pytest.mark.parametrize("kind", ["watch", "record"])
def test_real_role_ack_idempotent_stop_and_asr_descendant_drain(tmp_path, kind):
    context = make_context(tmp_path)
    script = toy_script(tmp_path)
    complete = tmp_path / "tail-finished.txt"
    builder = lambda kind, entry: [sys.executable, "-B", str(script), str(ROOT), kind, str(complete)]
    manager = app.RoleManager(context, command_builder=builder)
    entry = {"tag": "synthetic", "enabled": True, kind: True}
    roster = {"anchors": [entry]}
    manager.tick(roster)
    def running():
        manager.tick(roster)
        return manager.instance_status(kind, "synthetic")["running"]
    try:
        wait_for(running)
        owned = manager.roles[kind + ":synthetic"]["owned"]
        manager.tick(roster)
        assert manager.roles[kind + ":synthetic"]["owned"] is owned
        manager.begin_stop()
        assert manager.instance_status(kind, "synthetic")["state"] == "stopping"
        if kind == "record":
            time.sleep(.2)
            manager.tick({"anchors": []})
            assert not manager.drained() and not complete.exists()
        def stopped():
            manager.tick({"anchors": []})
            return manager.drained()
        wait_for(stopped)
        if kind == "record":
            assert complete.read_text() == "complete"
    finally:
        for role in manager.roles.values():
            if role["owned"].active_count() != 0:
                role["owned"].close(force=True)


def test_customer_anchor_actions_delegate_without_legacy_pid_operations(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    manager = app.RoleManager(context, command_builder=lambda *_: ["unused"])
    anchor_manager.configure_customer(manager)
    try:
        monkeypatch.setattr(anchor_manager, "read_pid", lambda *_: pytest.fail("legacy PID path forbidden"))
        assert anchor_manager.instance_status("watch", "absent")["running"] is False
        assert anchor_manager.stop("watch", "absent")["state"] == "not_owned"
        manager.begin_stop()
        assert anchor_manager.start("watch", {"tag": "synthetic", "watch": True, "enabled": True})["state"] == "stopping"
    finally:
        anchor_manager.configure_customer(None)


def test_customer_stop_blocks_unsent_automatic_ai_before_main_poll(sandbox, monkeypatch, tmp_path):
    from test_llm_lifecycle import write_gate, snapshot, eventually
    import llm_service
    runtime, config, data = sandbox.runtime(room_llm=True)
    write_gate(data)
    identity = app.process_identity(os.getpid())
    context = make_context(tmp_path / "customer", ports=(18765, 19222))
    context.update(config=str(config), data=str(data))
    context["instance_id"] = app.instance_id(context)
    context["profile"] = str(data / ".local_app/browser-profile")
    context_path = data / ".local_app/context.json"
    stop_path = data / ".local_app/stop.json"
    app.atomic_json(context_path, context)
    monkeypatch.setenv("DY_CUSTOMER_CONTEXT", str(context_path))
    monkeypatch.setenv("DY_CUSTOMER_STOP", str(stop_path))
    assert not runtime._customer_stopping()


    original = llm_service.PortraitService.generate
    observed = []
    def stopping_before_send(self, *args, **kwargs):
        app.atomic_json(stop_path, dict(schema=1, action="stop", instance_id=context["instance_id"],
            pid=identity["pid"], created=identity["created"]))
        observed.append(True)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(llm_service.PortraitService, "generate", stopping_before_send)
    runtime.start()
    eventually(lambda: runtime.llm_status()["state"] == "waiting_input")
    worker = runtime._llm_thread
    runtime._push_llm(snapshot("consumed before stop file"))
    worker.join(2)
    assert observed == [True] and not worker.is_alive()
    assert runtime._customer_stopping()
    assert runtime._llm_control()["reason"] == "application_stopping"
    runtime._push_llm(snapshot("must-not-send"))
    runtime.reconcile_llm()
    eventually(lambda: not runtime.llm_status()["worker_alive"])
    assert sandbox.calls == []
    # A stale/different process' request cannot stop the current instance.
    app.atomic_json(stop_path, dict(schema=1, action="stop", instance_id=context["instance_id"],
        pid=identity["pid"], created=identity["created"] - 1))
    assert not runtime._customer_stopping()


@pytest.mark.parametrize("script,args,expected", [("launch_all.py", ["--data", "生成 路径"],
    ["start", "--data", "生成 路径"]), ("start_recorders.py", ["--list", "--data", "生成 路径"],
    ["status", "--data", "生成 路径"])])
def test_compatibility_launchers_delegate_to_one_entry(monkeypatch, script, args, expected):
    import importlib.util
    import types
    seen = []
    monkeypatch.setitem(sys.modules, "local_app", SimpleNamespace(main=lambda argv: seen.append(argv) or 0))
    roster = types.ModuleType("anchor_roster")
    roster.SEED = []
    roster.ensure = lambda: ({"anchors": []}, False)
    roster.as_legacy_anchors = lambda value: []
    monkeypatch.setitem(sys.modules, "anchor_roster", roster)
    spec = importlib.util.spec_from_file_location("synthetic_compat", ROOT / "scripts" / script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(args) == 0 and seen == [expected]


@pytest.mark.skipif(os.name != "nt", reason="actual Board and Windows toy role")
def test_authenticated_anchor_actions_use_only_this_context_and_keep_explicit_stop(tmp_path, monkeypatch):
    import assist_board_server as board
    import anchor_roster
    context = make_context(tmp_path)
    script = toy_script(tmp_path)
    builder = lambda kind, entry: [sys.executable, "-B", str(script), str(ROOT), kind, str(tmp_path / "tail")]
    manager = app.RoleManager(context, command_builder=builder)
    for module, name in ((anchor_roster, "ROSTER_PATH"), (anchor_manager, "ROOT"),
                         (anchor_manager, "DATA"), (anchor_manager, "PY")):
        monkeypatch.setattr(module, name, getattr(module, name))
    server, thread, broadcaster = app.create_board(context, manager, app.process_identity(os.getpid()))
    headers = local_headers(context["port"])
    def request(path, body):
        conn = http.client.HTTPConnection("127.0.0.1", context["port"], timeout=4)
        try:
            conn.request("POST", path, json.dumps(body), dict(headers, **{"Content-Type": "application/json"}))
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()
    try:
        code, added = request("/api/anchors", {"tag": "synthetic", "sec_uid": "MS4wLjABAAAA" + "x" * 50,
            "watch": True, "record": False, "llm": False, "enabled": False})
        assert code == 200 and added["ok"]
        assert anchor_roster.load(context["roster"])["anchors"][0]["tag"] == "synthetic"
        assert manager.roles == {}
        assert request("/api/anchors/start?tag=synthetic&kind=all", {})[1]["ok"]
        def running():
            manager.tick(anchor_roster.load(context["roster"]))
            return manager.instance_status("watch", "synthetic")["running"]
        wait_for(running)
        owned = manager.roles["watch:synthetic"]["owned"]
        request("/api/anchors/stop?tag=synthetic&kind=watch", {})
        def stopped():
            manager.tick(anchor_roster.load(context["roster"]))
            return owned.active_count() == 0
        wait_for(stopped)
        manager.recover()
        manager.tick(anchor_roster.load(context["roster"]))
        assert manager.roles["watch:synthetic"]["owned"] is owned
        assert owned.active_count() == 0 and "watch:synthetic" in manager.suppressed
        assert json.loads(Path(context["config"]).read_text())["llm"]["enabled"] is False
    finally:
        manager.begin_stop()
        for role in manager.roles.values():
            if role["owned"].active_count() != 0:
                role["owned"].close(force=True)
        server.shutdown()
        server.server_close()
        broadcaster.stop()
        thread.join(2)
        anchor_manager.configure_customer(None)


def test_unacknowledged_owned_start_times_out_and_keeps_stopping_truth(tmp_path):
    context = make_context(tmp_path)
    clock = [0.0]
    class Pending:
        closed = False
        proc = SimpleNamespace(poll=lambda: None)
        def active_count(self):
            return 0 if self.closed else 1
        def worker_identity(self, receipt):
            return None
        def close(self, force=False, **kwargs):
            if force:
                self.closed = True
            return self.closed
    owned = Pending()
    manager = app.RoleManager(context, spawn=lambda *args, **kwargs: owned, now=lambda: clock[0],
        command_builder=lambda *_: ["toy"])
    roster = {"anchors": [{"tag": "synthetic", "enabled": True, "watch": True}]}
    manager.tick(roster)
    manager.begin_stop()
    assert not manager.drained()
    clock[0] = 21
    manager.tick({"anchors": []})
    assert owned.closed and manager.drained()


@pytest.mark.skipif(os.name != "nt", reason="real owned supervisor crash/restart")
def test_crashed_supervisor_recovers_without_touching_unknown_work(tmp_path):
    context = make_context(tmp_path)
    data = Path(context["data"])
    pending = data / "backfill_tasks" / "synthetic-unknown.json"
    pending.parent.mkdir()
    pending.write_text('{"may_have_sent":true,"dispatch_settled":false}', encoding="utf-8")
    ledger = data / "llm_budget_synthetic.json"
    ledger.write_text('{"total":9}', encoding="utf-8")
    progress = data / "asr-progress-synthetic.json"
    progress.write_text('{"done":["synthetic-fragment"]}', encoding="utf-8")
    preserved = {path: path.read_bytes() for path in (pending, ledger, progress, Path(context["config"]), Path(context["roster"]))}
    command = [sys.executable, "-B", str(ROOT / "scripts/local_app.py"), "run"]
    for name in ("root", "data", "config", "roster", "dist", "python", "port", "cdp_port"):
        command += ["--" + name.replace("_", "-"), str(context[name])]
    owned = app.OwnedProcess.start(command, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        cwd=context["root"], log_path=tmp_path / "crash-toy.log")
    restarted = None
    try:
        old = wait_for(lambda: (value if (value := app.status(context)).get("board_ready") and value.get("ok") else None))
        assert owned.worker_identity(old["owner"]) is not None
        assert owned.close(force=True)
        wait_for(lambda: app.process_fact(old["owner"]) == "dead")
        for _ in range(4):
            restarted = app.start(context)
            if restarted.get("ok"):
                break
            assert restarted["state"] in ("starting", "starting_or_identity_unknown", "startup_timeout"), restarted
            time.sleep(.1)
        assert restarted["ok"] and restarted["owner"] != old["owner"], restarted
        assert restarted["roles"] == {} and restarted["manual_writers"] == 0
        assert all(path.read_bytes() == value for path, value in preserved.items())
    finally:
        if not owned.closed:
            owned.close(force=True)
        if restarted and restarted.get("ok"):
            stopped = app.control(context, "stop")
            assert stopped["state"] == "stopped"


@pytest.mark.parametrize("endpoint,expected_attempts", [("vacant", 1), ("listening", 0), ("unknown", 0)])
def test_reused_supervisor_pid_allows_only_lock_and_port_verified_new_launch(tmp_path, monkeypatch, endpoint, expected_attempts):
    context = make_context(tmp_path)
    owner = {"state": "alive", "pid": 999, "created": 100.0}
    app.atomic_json(app.runtime_dir(context) / "instance.json", {"context": context,
        "instance_id": context["instance_id"], "owner": owner, "updated_at": time.time(), "state": "running"})
    monkeypatch.setattr(app, "process_fact", lambda _: "reused")
    monkeypatch.setattr(app, "listener_pid", lambda _: {"state": endpoint, "pids": [123] if endpoint == "listening" else []})
    attempts = []
    def denied(*args, **kwargs):
        attempts.append(True)
        raise OSError("synthetic interpreter failure")
    monkeypatch.setattr(app.subprocess, "Popen", denied)
    value = app.start(context)
    assert len(attempts) == expected_attempts
    assert value["state"] == {"vacant": "python_unavailable", "listening": "port_conflict", "unknown": "port_state_unavailable"}[endpoint]


def test_existing_manual_task_admission_observes_application_stop_without_changing_gate(tmp_path, monkeypatch):
    from backfill_tasks import TaskStore
    context = make_context(tmp_path)
    config = Path(context["config"])
    config.write_text(json.dumps({"llm": {"enabled": True, "daily_token_budget": 0,
        "active_provider": "synthetic", "providers": {"synthetic": {"api_key": "synthetic-only-key",
        "model": "synthetic-model", "base_url": "https://example.invalid/v1"}}}}), encoding="utf-8")
    store = TaskStore(config, context["data"])
    _, run = store.reserve_inline("synthetic", "2026-11-15")
    assert run is not None and run.guard()
    before = config.read_bytes()
    manager = app.RoleManager(context, command_builder=lambda *_: ["unused"])
    monkeypatch.setenv("DY_CUSTOMER_CONTEXT", str(app.runtime_dir(context) / "context.json"))
    monkeypatch.setenv("DY_CUSTOMER_STOP", str(app.runtime_dir(context) / "application-stop.json"))
    manager.begin_stop()
    assert not run.before_send("synthetic", "synthetic-model")
    assert run.stop_reason == "application_stopping"
    assert run.record["dispatch_count"] == 0 and run.record["may_have_sent"] is False
    assert config.read_bytes() == before
    assert not (Path(context["data"]) / "llm_runtime.json").exists()


def test_connection_test_rechecks_application_guard_before_network(tmp_path, monkeypatch):
    import board_llm
    context = make_context(tmp_path)
    Path(context["config"]).write_text('{"llm":{"enabled":true}}', encoding="utf-8")
    monkeypatch.setattr(board_llm, "_provider_http_request", lambda *args: pytest.fail("must not send"))
    value = board_llm.test_provider_connection(context["config"], context["data"], {
        "provider": "synthetic", "protocol": "openai_compatible", "base_url": "https://example.invalid/v1",
        "model": "synthetic", "api_key": "synthetic-not-a-real-key"}, application_guard=lambda: False)
    assert value["code"] == "application_stopping"


def test_unreadable_customer_stop_marker_blocks_ai_admission(tmp_path, monkeypatch):
    import room_runtime
    context = make_context(tmp_path)
    marker = app.runtime_dir(context) / "bad-stop.json"
    marker.write_text('{"broken":', encoding="utf-8")
    monkeypatch.setenv("DY_CUSTOMER_CONTEXT", str(app.runtime_dir(context) / "context.json"))
    monkeypatch.setenv("DY_CUSTOMER_STOP", str(marker))
    runtime = room_runtime.RoomRuntime.__new__(room_runtime.RoomRuntime)
    runtime._llm_cfg = SimpleNamespace(_source_path=context["config"])
    runtime.cfg = SimpleNamespace(output_dir=context["data"])
    assert runtime._customer_stopping() and runtime._llm_control()["reason"] == "application_stopping"


def test_customer_cleanup_keeps_files_open_until_inflight_ai_finishes(sandbox, monkeypatch, tmp_path):
    from test_llm_lifecycle import write_gate, snapshot, eventually
    runtime, config, data = sandbox.runtime()
    write_gate(data)
    context = make_context(tmp_path / "customer", ports=(18765, 19222))
    context.update(config=str(config), data=str(data))
    context["instance_id"] = app.instance_id(context)
    context["profile"] = str(data / ".local_app/browser-profile")
    context_path = app.runtime_dir(context) / "context.json"
    app.atomic_json(context_path, context)
    monkeypatch.setenv("DY_CUSTOMER_CONTEXT", str(context_path))
    entered, release = threading.Event(), threading.Event()
    sandbox.hooks.append(lambda: (entered.set(), release.wait(3)))
    runtime.start()
    eventually(lambda: runtime.llm_status()["state"] == "waiting_input")
    runtime._push_llm(snapshot("entered before shutdown"))
    assert entered.wait(2)
    try:
        runtime.stop(join_timeout=.01)
        assert runtime._llm_thread.is_alive() and not getattr(runtime, "_storage_closed", False)
        release.set()
        runtime.wait_customer_cleanup()
        assert not runtime._llm_thread or not runtime._llm_thread.is_alive()
        assert runtime._storage_closed and len(sandbox.calls) == 1
    finally:
        release.set()


def test_unknown_parent_stop_identity_is_strict_for_ai_only(tmp_path, monkeypatch):
    import scripts.backfill_tasks as tasks
    from local_context import customer_stop_requested
    context = make_context(tmp_path)
    marker = app.runtime_dir(context) / "owner-stop.json"
    app.atomic_json(marker, {"schema": 1, "action": "stop", "instance_id": context["instance_id"],
                            "pid": 777, "created": 12.0})
    monkeypatch.setenv("DY_CUSTOMER_STOP", str(marker))
    monkeypatch.setattr(tasks, "process_identity", lambda pid: {"state": "unknown", "pid": pid, "created": None})
    identity = {"state": "alive", "pid": 888, "created": 15.0}
    assert not customer_stop_requested(context, identity, allow_parent=True)
    with pytest.raises(ValueError):
        customer_stop_requested(context, identity, allow_parent=True, strict=True)


def test_direct_start_never_promotes_unknown_job_from_cached_running(tmp_path):
    context = make_context(tmp_path)
    count, calls = [1], []
    owned = SimpleNamespace(active_count=lambda: count[0])
    manager = app.RoleManager(context, spawn=lambda *args, **kwargs: calls.append(True) or owned,
                              command_builder=lambda *_: ["toy"])
    entry = {"tag": "synthetic", "enabled": True, "watch": True}
    assert manager.start_role("watch", entry)["ok"]
    manager.roles["watch:synthetic"]["state"] = "running"
    count[0] = None
    value = manager.start_role("watch", entry)
    assert not value["ok"] and value["state"] == "identity_unknown" and calls == [True]
    assert manager.roles["watch:synthetic"]["owned"] is owned


@pytest.mark.skipif(os.name != "nt", reason="actual consoleless Windows interpreter")
def test_logon_command_runs_foreground_supervisor_without_a_console(tmp_path):
    from local_autostart import plan
    context = make_context(tmp_path)
    planned = plan(context)
    assert planned["command"][2] == "run"
    if not Path(planned["executable"]).is_file():
        pytest.skip("selected runtime has no pythonw; real enable refuses registration")
    owned = app.OwnedProcess.start(planned["command"], env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        cwd=planned["working_directory"], log_path=tmp_path / "consoleless.log")
    try:
        value = wait_for(lambda: (state if (state := app.status(context)).get("ok") and state.get("board_ready") else None))
        assert owned.worker_identity(value["owner"])
        assert app.control(context, "stop")["state"] == "stopped"
        wait_for(lambda: owned.active_count() == 0)
        assert owned.close()
    finally:
        if not owned.closed:
            owned.close(force=True)


@pytest.mark.parametrize("pid_text", ["live", "malformed"])
def test_legacy_pid_text_never_grants_ownership_or_proves_reuse_by_mtime(tmp_path, pid_text):
    context = make_context(tmp_path)
    marker = Path(context["data"]) / "stream_synthetic.pid"
    marker.write_text(str(os.getpid()) if pid_text == "live" else "unknown", encoding="utf-8")
    os.utime(marker, (1, 1))
    before = marker.read_bytes()
    calls = []
    manager = app.RoleManager(context, command_builder=lambda *_: ["toy"],
        spawn=lambda *args, **kwargs: calls.append(True))
    value = manager.start_role("record", {"tag": "synthetic", "record": True, "enabled": True})
    assert not value["ok"] and value["state"] == "identity_unverified"
    assert calls == [] and marker.read_bytes() == before


@pytest.mark.parametrize("module_name", ["run_watchdog", "watch_anchor_live"])
def test_role_notifications_use_the_same_customer_config(tmp_path, monkeypatch, module_name):
    import importlib
    module = importlib.import_module(module_name)
    context = make_context(tmp_path)
    observed = []
    monkeypatch.setattr(module, "CUSTOMER", context)
    monkeypatch.setattr(module.notify, "send_text", lambda *args, **kwargs: observed.append(kwargs) or True)
    assert module._notify("synthetic local test", key="synthetic")
    assert observed == [{"key": "synthetic", "config_path": context["config"]}]
