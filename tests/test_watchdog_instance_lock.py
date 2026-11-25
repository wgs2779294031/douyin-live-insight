"""Watchdog ownership tests: temporary state, synthetic children, no browser/CDP."""
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_watchdog as rw
from file_lock import FileLock


def _wait_until(predicate, children, timeout=12):
    deadline = time.monotonic() + timeout
    while not predicate():
        for child in children:
            if child.poll() is not None and child.returncode != 0:
                stdout, stderr = child.communicate(timeout=2)
                pytest.fail("synthetic worker exited early: %s\n%s\n%s" %
                            (child.returncode, stdout, stderr))
        if time.monotonic() >= deadline:
            pytest.fail("synthetic watchdog did not reach its barrier")
        time.sleep(.02)


def test_concurrent_watchdog_starts_have_one_owner_and_do_not_replace_pid(tmp_path):
    worker = tmp_path / "synthetic_watchdog_worker.py"
    worker.write_text(
        "import json,os,pathlib,sys,time\n"
        "source=pathlib.Path(sys.argv[1]).resolve()\n"
        "root=pathlib.Path(sys.argv[2]);label=sys.argv[3]\n"
        "def guard(event,args):\n"
        "    if event=='socket.connect':raise AssertionError('network forbidden')\n"
        "    if event=='open' and isinstance(args[0],(str,bytes,os.PathLike)):\n"
        "        p=pathlib.Path(os.fsdecode(args[0])).resolve()\n"
        "        if p==source/'config.json' or p==source/'anchors.json' or source/'data' in p.parents or source/'captures' in p.parents:\n"
        "            raise AssertionError('real private state forbidden')\n"
        "sys.addaudithook(guard)\n"
        "sys.path.insert(0,str(source/'scripts'))\n"
        "import run_watchdog as rw\n"
        "rw.ROOT=root;rw._LOG_PATH=None\n"
        "def event(name):\n"
        "    (root/(name+'_'+str(os.getpid()))).write_text(label)\n"
        "def browser(env):event('browser');return True\n"
        "def protocol(path):path.write_text(json.dumps({'pid':os.getpid(),'synthetic':True}))\n"
        "def wait(proc,env,tag=''):\n"
        "    event('watch')\n"
        "    while not (root/'release').exists():time.sleep(.02)\n"
        "    raise KeyboardInterrupt\n"
        "rw.ensure_browser=browser;rw.write_stop_protocol=protocol\n"
        "rw.start_watch_process=lambda cmd:object()\n"
        "rw.stop_watch_tree=lambda proc:None\n"
        "rw._wait_with_port_check=wait\n"
        "rw.notify.send_text=lambda *a,**k:(_ for _ in ()).throw(AssertionError('alert forbidden'))\n"
        "sys.argv=['synthetic-watchdog','--sec-uid','synthetic','--tag','S']\n"
        "(root/('ready_'+label)).write_text('ready')\n"
        "while not (root/'go').exists():time.sleep(.02)\n"
        "sys.exit(rw.main())\n",
        encoding="utf-8")
    source = Path(rw.__file__).resolve().parents[1]
    args = [sys.executable, "-I", "-B", str(worker), str(source), str(tmp_path)]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    def spawn(label):
        options = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True, creationflags=flags)
        if os.name == "nt":
            from windows_job import spawn_in_job
            return spawn_in_job(args + [label], **options)
        return subprocess.Popen(args + [label], **options)

    children = [spawn(label) for label in ("A", "B")]
    try:
        _wait_until(lambda: all((tmp_path / ("ready_" + label)).exists()
                                for label in ("A", "B")), children)
        (tmp_path / "go").write_text("go")
        # Wait until both starts have either entered the fake watch or exited.
        _wait_until(lambda: len(list(tmp_path.glob("watch_*"))) +
                    sum(child.poll() is not None for child in children) >= 2, [])
        watches = list(tmp_path.glob("watch_*"))
        assert len(watches) == 1, "a duplicate watchdog started a second watch"
        owner_pid = int(watches[0].name.split("_")[1])
        # Windows venv redirectors and their base-Python child can have
        # different PIDs. The worker label binds the logical launch instead.
        owner_index = ("A", "B").index(watches[0].read_text())
        owner = children[owner_index]
        contender = children[1 - owner_index]
        stdout, stderr = contender.communicate(timeout=5)
        assert contender.returncode == 3, (stdout, stderr)
        pidfile = tmp_path / "data" / "watchdog_S.pid"
        protocol = pidfile.with_suffix(".protocol.json")
        assert pidfile.read_text() == str(owner_pid)
        import json
        assert json.loads(protocol.read_text())["pid"] == owner_pid
        assert len(list(tmp_path.glob("browser_*"))) == 1
        lock = Path(str(pidfile) + ".lock")
        inode = lock.stat().st_ino
        time.sleep(.1)
        assert pidfile.read_text() == str(owner_pid)
        assert owner.poll() is None
        (tmp_path / "release").write_text("release")
        stdout, stderr = owner.communicate(timeout=5)
        assert owner.returncode == 0, (stdout, stderr)
        assert not pidfile.exists()
        # A fresh process can own the same stable lock after normal owner exit.
        third = spawn("C")
        children.append(third)
        stdout, stderr = third.communicate(timeout=10)
        assert third.returncode == 0, stderr
        assert len(list(tmp_path.glob("watch_*"))) == 2
        assert lock.exists() and lock.stat().st_ino == inode
    finally:
        (tmp_path / "go").write_text("release test barrier")
        (tmp_path / "release").write_text("release synthetic owners")
        for child in children:
            job = getattr(child, "_watch_job", None)
            if job is not None:
                job.close(timeout=5)  # Includes venv child; synthetic Job only.
            elif child.poll() is None:
                child.kill()
            child.communicate(timeout=5)


def test_instance_lock_io_error_does_not_touch_existing_pid_or_start(tmp_path, monkeypatch):
    monkeypatch.setattr(rw, "ROOT", tmp_path)
    monkeypatch.setattr(rw, "_LOG_PATH", None)
    monkeypatch.setattr(rw.sys, "argv", ["watchdog", "--sec-uid", "synthetic", "--tag", "S"])
    pidfile = tmp_path / "data" / "watchdog_S.pid"
    pidfile.parent.mkdir()
    pidfile.write_text("synthetic-prior-owner")
    calls = []

    class BrokenLock:
        def __init__(self, path):
            assert path == pidfile

        def acquire(self, blocking):
            assert blocking is False
            raise OSError("synthetic lock permission failure")

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("no owner means no start or protocol publication")

    monkeypatch.setattr(rw, "FileLock", BrokenLock)
    monkeypatch.setattr(rw, "write_stop_protocol", forbidden)
    monkeypatch.setattr(rw, "ensure_browser", forbidden)
    monkeypatch.setattr(rw, "start_watch_process", forbidden)
    with pytest.raises(OSError, match="lock permission"):
        rw.main()
    assert pidfile.read_text() == "synthetic-prior-owner"
    assert calls == []


def test_startup_exception_releases_instance_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(rw, "ROOT", tmp_path)
    monkeypatch.setattr(rw, "_LOG_PATH", None)
    monkeypatch.setattr(rw.signal, "signal", lambda *args: None)
    monkeypatch.setattr(rw.sys, "argv", ["watchdog", "--sec-uid", "synthetic", "--tag", "S"])
    monkeypatch.setattr(rw, "write_stop_protocol", lambda path: None)
    monkeypatch.setattr(rw, "ensure_browser", lambda env: (_ for _ in ()).throw(OSError("synthetic startup failure")))
    with pytest.raises(OSError, match="startup failure"):
        rw.main()
    pidfile = tmp_path / "data" / "watchdog_S.pid"
    assert not pidfile.exists()
    lock = FileLock(pidfile)
    assert lock.acquire(blocking=False)
    lock.release()
    assert Path(str(pidfile) + ".lock").exists()
