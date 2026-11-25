"""Shared browser launch serialization, using only temporary roots and fake browsers."""
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_watchdog as rw


def test_no_launch_lock_means_no_browser_cleanup_or_spawn(tmp_path, monkeypatch):
    monkeypatch.setattr(rw, "port_alive", lambda *args, **kwargs: False)
    monkeypatch.setattr(rw, "resolve_chrome", lambda env: "synthetic-browser")
    monkeypatch.setattr(rw, "resolve_profile", lambda env: str(tmp_path / "profile"))
    monkeypatch.setattr(rw, "_acquire_launch_lock", lambda: False)
    monkeypatch.setattr(rw, "_recent_launch", lambda: False)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("non-owner must not clean or launch shared browser")

    monkeypatch.setattr(rw, "_profile_pids", forbidden)
    monkeypatch.setattr(rw.subprocess, "Popen", forbidden)
    monkeypatch.setattr(rw, "_release_launch_lock", forbidden)
    assert rw.ensure_browser({}) is False
    assert calls == []


@pytest.mark.skipif(os.name != "nt", reason="Windows watchdog stop protocol")
def test_stop_protocol_failure_prevents_browser_and_watch_start(tmp_path, monkeypatch):
    monkeypatch.setattr(rw, "ROOT", tmp_path)
    monkeypatch.setattr(rw, "_LOG_PATH", None)
    monkeypatch.setattr(rw.signal, "signal", lambda *args: None)
    monkeypatch.setattr(rw.sys, "argv", ["watchdog", "--sec-uid", "synthetic", "--tag", "S"])
    calls = []

    def fail_protocol(path):
        raise OSError("synthetic protocol write failure")

    monkeypatch.setattr(rw, "write_stop_protocol", fail_protocol)
    monkeypatch.setattr(rw, "ensure_browser", lambda env: calls.append("browser"))
    monkeypatch.setattr(rw, "start_watch_process", lambda cmd: calls.append("watch"))
    with pytest.raises(OSError, match="protocol write failure"):
        rw.main()
    assert calls == []
    assert not (tmp_path / "data" / "watchdog_S.pid").exists()


def test_launch_lock_survives_old_mtime_and_releases_on_owner_exit(tmp_path):
    worker = tmp_path / "launch_lock_worker.py"
    worker.write_text(
        "import pathlib,sys,time\n"
        "sys.path.insert(0,sys.argv[1])\n"
        "import run_watchdog as rw\n"
        "rw.ROOT=pathlib.Path(sys.argv[2])\n"
        "rw.port_alive=lambda **kwargs:False\n"
        "got=rw._acquire_launch_lock(timeout=.2)\n"
        "if sys.argv[3]=='hold':\n"
        "    assert got\n"
        "    (rw.ROOT/'ready').write_text('ready')\n"
        "    while True:time.sleep(60)\n"
        "print('ACQUIRED' if got else 'BUSY',flush=True)\n"
        "if got:rw._release_launch_lock()\n",
        encoding="utf-8")
    args = [sys.executable, "-I", "-B", str(worker), str(Path(rw.__file__).parent), str(tmp_path)]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    owner = subprocess.Popen(args + ["hold"], creationflags=flags,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / "ready").exists():
            assert owner.poll() is None
            if time.monotonic() > deadline:
                pytest.fail("synthetic launch-lock owner not ready")
            time.sleep(.02)
        lock = tmp_path / "data" / ".chrome_launch.lock"
        inode = lock.stat().st_ino
        os.utime(lock, (1, 1))  # old PID/mtime protocol would steal a live lock here
        blocked = subprocess.run(args + ["try"], capture_output=True, text=True,
                                 timeout=5, creationflags=flags)
        assert blocked.returncode == 0, blocked.stderr
        assert blocked.stdout.strip() == "BUSY"
        assert lock.stat().st_ino == inode
        owner.kill()  # terminate only our synthetic owner, with no Python finally
        owner.communicate(timeout=5)
        acquired = subprocess.run(args + ["try"], capture_output=True, text=True,
                                  timeout=5, creationflags=flags)
        assert acquired.returncode == 0, acquired.stderr
        assert acquired.stdout.strip() == "ACQUIRED"
        assert lock.exists() and lock.stat().st_ino == inode
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=5)
