"""Private toy trees and isolated loopback only; no browser or collector."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import local_process as owned
import windows_job as jobs


pytestmark = pytest.mark.skipif(os.name != "nt", reason="customer Windows ownership")


def wait_until(read, *, timeout=10):
    deadline = time.monotonic() + timeout
    while True:
        value = read()
        if value:
            return value
        assert time.monotonic() < deadline, "synthetic resource did not reach expected state"
        time.sleep(.02)


def receipt(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@pytest.fixture
def toy(tmp_path):
    path = tmp_path / "toy.py"
    path.write_text(
        "import json,os,pathlib,socket,subprocess,sys,time\n"
        "sys.path[:0]=[sys.argv[1],str(pathlib.Path(sys.argv[1]).parent)]\n"
        "from backfill_tasks import process_identity\n"
        "mode,output,release=sys.argv[2],pathlib.Path(sys.argv[3]),pathlib.Path(sys.argv[4])\n"
        "value=process_identity(os.getpid())\n"
        "if mode=='orphan':\n"
        " subprocess.Popen([sys.executable,'-X','utf8','-B',__file__,sys.argv[1],'wait',str(output)+'.child',str(release)])\n"
        "if mode=='server':\n"
        " server=socket.socket();server.bind(('127.0.0.1',0));server.listen(1)\n"
        " value['port']=server.getsockname()[1]\n"
        "output.write_text(json.dumps(value),encoding='utf-8')\n"
        "if mode=='orphan':\n"
        " while not pathlib.Path(str(output)+'.child').exists(): time.sleep(.02)\n"
        " os._exit(0)\n"
        "while not release.exists():time.sleep(.02)\n",
        encoding="utf-8", newline="\n")
    return path


def start_toy(tmp_path, toy, mode, name):
    output, release = tmp_path / (name + ".json"), tmp_path / (name + ".release")
    tree = owned.OwnedProcess.start([sys.executable, "-X", "utf8", "-B", str(toy), str(Path(owned.__file__).parent),
        mode, str(output), str(release)], env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8"),
        cwd=tmp_path, log_path=tmp_path / (name + ".log"))
    return tree, output, release


def test_owned_venv_ack_and_normal_close_wait_for_real_exit(tmp_path, toy):
    tree, output, release = start_toy(tmp_path, toy, "wait", "waiting")
    try:
        actual = wait_until(lambda: receipt(output))
        assert tree.worker_identity(actual) == actual
        assert tree.active_count() > 0 and not tree.close()
        assert tree.proc.poll() is None
        release.touch()
        wait_until(lambda: tree.active_count() == 0)
        assert tree.close() and tree.closed and tree.active_count() == 0
        assert tree.close()  # Empty release is idempotent.
    finally:
        assert tree.close(force=True)


def test_dead_root_does_not_hide_live_asr_shaped_descendant(tmp_path, toy):
    tree, output, release = start_toy(tmp_path, toy, "orphan", "parent")
    try:
        child = wait_until(lambda: receipt(Path(str(output) + ".child")))
        wait_until(lambda: tree.proc.poll() is not None)
        assert owned.same_identity(child, owned.process_identity(child["pid"]))
        assert tree.active_count() > 0
        assert not tree.close() and not tree.closed
        assert owned.same_identity(child, owned.process_identity(child["pid"]))
        release.touch()
        wait_until(lambda: tree.active_count() == 0)
        assert tree.close()
    finally:
        assert tree.close(force=True)


def test_force_closes_only_its_job_and_retains_other_listener(tmp_path, toy):
    sibling, sibling_output, _ = start_toy(tmp_path, toy, "server", "separate")
    tree = None
    try:
        other = wait_until(lambda: receipt(sibling_output))
        tree, output, _ = start_toy(tmp_path, toy, "orphan", "owned")
        child = wait_until(lambda: receipt(Path(str(output) + ".child")))
        wait_until(lambda: tree.proc.poll() is not None)
        assert tree.close(force=True, timeout=5)
        assert owned.process_identity(child["pid"])["state"] == "dead"
        assert owned.same_identity(other, owned.process_identity(other["pid"]))
        assert owned.listener_pid(other["port"]) == {"state": "listening", "pids": [other["pid"]]}
    finally:
        if tree is not None:
            assert tree.close(force=True)
        assert sibling.close(force=True)


def test_real_loopback_listener_pid_and_closed_port():
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        server.listen(1)
        assert owned.listener_pid(port) == {"state": "listening", "pids": [os.getpid()]}
    assert owned.listener_pid(port) == {"state": "vacant", "pids": []}


def fake_tree():
    root = {"state": "alive", "pid": 101, "created": 10.0}
    job = SimpleNamespace(active_processes=lambda: 1, release_empty=lambda: False,
                          close=lambda **_kwargs: None)
    proc = SimpleNamespace(pid=101, poll=lambda: None, _watch_job=job)
    tree = owned.OwnedProcess.__new__(owned.OwnedProcess)
    tree.proc, tree.job, tree.identity = proc, job, root
    tree.closed, tree.last_error, tree._uncertain = False, "", False
    return tree


@pytest.mark.parametrize("case", ["root_reused", "root_unknown", "worker_reused", "worker_unknown",
    "wrong_parent", "unknown_parent", "older_child", "invalid_pid", "invalid_created", "nan_created"])
def test_bad_or_unknown_worker_identity_is_never_adopted(monkeypatch, case):
    tree = fake_tree()
    facts = {101: dict(tree.identity), 102: {"state": "alive", "pid": 102, "created": 11.0}}
    claim = {"pid": 102, "created": 11.0}
    parent = 101
    if case == "root_reused":
        facts[101]["created"] = 20.0
    elif case == "root_unknown":
        facts[101]["state"] = "unknown"
    elif case == "worker_reused":
        facts[102]["created"] = 21.0
    elif case == "worker_unknown":
        facts[102]["state"] = "unknown"
    elif case == "wrong_parent":
        parent = 999
    elif case == "unknown_parent":
        parent = None
    elif case == "older_child":
        claim["created"] = facts[102]["created"] = 9.0
    elif case == "invalid_pid":
        claim["pid"] = True
    elif case == "invalid_created":
        claim["created"] = True
    else:
        claim["created"] = float("nan")
    monkeypatch.setattr(owned, "process_identity", lambda pid: facts.get(pid))
    monkeypatch.setattr(owned, "process_parent_pid", lambda _pid: parent)
    assert tree.worker_identity(claim) is None


def test_query_and_force_close_errors_remain_unknown():
    tree = fake_tree()
    def failed():
        raise OSError("synthetic query failure")
    tree.job.active_processes = failed
    assert tree.active_count() is None
    assert not tree.close() and not tree.closed
    def failed_close(**_kwargs):
        raise TimeoutError("synthetic close failure")
    tree.job.close = failed_close
    assert not tree.close(force=True)
    # A lost Job handle must not later turn the same failed stop into success.
    tree.job.active_processes = lambda: 0
    tree.job.close = lambda **_kwargs: None
    assert tree.active_count() is None and not tree.close(force=True)


def test_empty_job_release_does_not_terminate_and_query_failure_keeps_handle(monkeypatch):
    job = jobs.WindowsJob()
    try:
        assert job.active_processes() == 0
        assert job.release_empty() and job._handle is None
        assert job.release_empty()
    finally:
        job.close()
    calls = []
    fake = jobs.WindowsJob.__new__(jobs.WindowsJob)
    fake._handle = 777
    fake._kernel = SimpleNamespace(QueryInformationJobObject=lambda *_args: False,
                                   CloseHandle=lambda handle: calls.append(handle) or True)
    try:
        with pytest.raises(OSError):
            fake.release_empty()
        assert fake._handle == 777 and calls == []
    finally:
        fake._handle = None


def test_listener_failure_and_unknown_owner_are_not_vacant(monkeypatch):
    def failed():
        raise OSError("synthetic read failure")
    monkeypatch.setattr(owned, "_tcp_listener_rows", failed)
    assert owned.listener_pid(12345)["state"] == "unknown"
    monkeypatch.setattr(owned, "_tcp_listener_rows", lambda: [("127.0.0.1", 12345, 0, 2)])
    assert owned.listener_pid(12345)["state"] == "unknown"


def test_wildcard_and_specific_bind_owner_filtering(monkeypatch):
    monkeypatch.setattr(owned, "_tcp_listener_rows", lambda: [
        ("0.0.0.0", 12345, 20, 2), ("127.0.0.2", 12345, 30, 2),
        ("127.0.0.1", 12345, 40, 5), ("127.0.0.1", 54321, 50, 2)])
    assert owned.listener_pid(12345) == {"state": "listening", "pids": [20]}
    assert owned.listener_pid(12345, "0.0.0.0") == {"state": "listening", "pids": [20, 30]}


@pytest.mark.parametrize("port,bind", [(True, "127.0.0.1"), (0, "127.0.0.1"),
    (65536, "127.0.0.1"), ("12345", "127.0.0.1"), (12345, "localhost")])
def test_invalid_listener_query_is_unknown(port, bind):
    assert owned.listener_pid(port, bind)["state"] == "unknown"
