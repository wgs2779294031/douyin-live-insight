"""Real thread/process contention against temporary files; no application data."""
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

import file_lock as locks
from file_lock import FileLock, file_lock


ROOT = Path(__file__).resolve().parents[1]


def _holder(tmp_path, resource):
    ready = tmp_path / "ready"
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    proc = subprocess.Popen(
        [sys.executable, "-c", "import pathlib,sys,time; from file_lock import FileLock; "
         "lock=FileLock(sys.argv[1]); lock.acquire(); "
         "pathlib.Path(sys.argv[2]).write_text('ready'); time.sleep(30)",
         str(resource), str(ready)], cwd=str(tmp_path), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 10
    while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if not ready.exists():
        proc.kill()
        out, err = proc.communicate(timeout=5)
        pytest.fail("holder did not acquire lock: %r %r" % (out, err))
    return proc


def test_distinct_instances_and_canonical_aliases_serialize_threads(tmp_path):
    resource = tmp_path / "state.json"
    results = []
    with file_lock(resource):
        # Nested application helpers can lock the same resource without deadlock.
        with file_lock(tmp_path / "unused" / ".." / resource.name):
            pass
        def compete():
            candidate = FileLock(resource, timeout=0.05)
            results.append(candidate.acquire(blocking=False))
            try:
                candidate.acquire()
            except TimeoutError:
                results.append("timeout")
        thread = threading.Thread(target=compete)
        thread.start()
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert results == [False, "timeout"]
    with file_lock(resource):
        pass
    assert Path(str(resource) + ".lock").exists()


def test_process_contention_timeout_and_crash_release(tmp_path):
    resource = tmp_path / "state.json"
    proc = _holder(tmp_path, resource)
    try:
        candidate = FileLock(resource, timeout=0.05)
        assert candidate.acquire(blocking=False) is False
        with pytest.raises(TimeoutError):
            candidate.acquire()
    finally:
        proc.kill()
        proc.communicate(timeout=5)
    # An ungraceful exit releases the kernel lock, even though its file remains.
    with file_lock(resource, timeout=1):
        pass


def test_io_failure_does_not_enter_or_poison_thread_lock(tmp_path, monkeypatch):
    resource = tmp_path / "state.json"
    original = locks._try_os_lock
    def fail(_handle):
        raise PermissionError("synthetic lock failure")
    monkeypatch.setattr(locks, "_try_os_lock", fail)
    with pytest.raises(PermissionError):
        with file_lock(resource):
            pytest.fail("entered without an OS lock")
    monkeypatch.setattr(locks, "_try_os_lock", original)
    results = []
    def retry():
        try:
            with file_lock(resource, timeout=0.2):
                results.append("acquired")
        except BaseException as exc:
            results.append(exc)
    thread = threading.Thread(target=retry)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert results == ["acquired"]


def test_exception_releases_and_legacy_sidecar_is_not_deleted(tmp_path):
    resource = tmp_path / ".transcribe.run"
    sidecar = Path(str(resource) + ".lock")
    sidecar.write_text("12345", encoding="utf-8")
    with pytest.raises(ValueError):
        with file_lock(resource):
            raise ValueError("synthetic work failure")
    assert sidecar.read_text(encoding="utf-8") == "12345"
    with file_lock(resource):
        pass
