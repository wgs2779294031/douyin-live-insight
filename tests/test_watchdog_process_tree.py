"""仅使用临时替身进程验证看门狗回收，不连接浏览器或真实采集。"""
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_watchdog as rw


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows process tree cleanup")


class FakeWatch:
    pid = 12345

    def __init__(self, alive=True):
        self.alive = alive

    def poll(self):
        return None if self.alive else 0

    def wait(self, timeout):
        if self.alive:
            raise subprocess.TimeoutExpired("synthetic-watch", timeout)
        return 0

    def terminate(self):
        raise AssertionError("Windows must clean the tree before killing its root")

    kill = terminate


def test_windows_tree_cleanup_runs_before_direct_termination(monkeypatch):
    proc = FakeWatch()
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        proc.alive = False
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(rw.subprocess, "run", run)
    rw.stop_watch_tree(proc, timeout=3)
    assert calls[0][0] == ["taskkill", "/PID", "12345", "/T", "/F"]
    assert calls[0][1]["timeout"] == 3
    rw.stop_watch_tree(proc, timeout=3)
    assert len(calls) == 1  # repeat cleanup does not target a potentially reused PID


@pytest.mark.parametrize("failure", ["returncode", "timeout", "missing"])
def test_tree_cleanup_failure_is_not_reported_as_success(monkeypatch, failure):
    proc = FakeWatch()

    def run(cmd, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        if failure == "missing":
            raise FileNotFoundError("synthetic missing taskkill")
        return subprocess.CompletedProcess(cmd, 1)

    monkeypatch.setattr(rw.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="回收 watch 进程树"):
        rw.stop_watch_tree(proc, timeout=0.1)
    assert proc.alive


def test_cleanup_accepts_root_exiting_during_taskkill(monkeypatch):
    proc = FakeWatch()

    def run(cmd, **kwargs):
        proc.alive = False
        return subprocess.CompletedProcess(cmd, 128)

    monkeypatch.setattr(rw.subprocess, "run", run)
    rw.stop_watch_tree(proc)


@pytest.mark.parametrize("failure", [KeyboardInterrupt, RuntimeError])
def test_main_cleans_owned_child_in_finally(monkeypatch, tmp_path, failure):
    proc = FakeWatch()
    cleaned = []
    monkeypatch.setattr(rw, "ROOT", tmp_path)
    monkeypatch.setattr(rw, "_LOG_PATH", None)
    monkeypatch.setattr(rw, "log", lambda msg: None)
    monkeypatch.setattr(rw, "ensure_browser", lambda env: True)
    monkeypatch.setattr(rw.signal, "signal", lambda *args: None)
    monkeypatch.setattr(rw.sys, "argv", ["run_watchdog", "--sec-uid", "synthetic",
                                        "--tag", "synthetic"])
    monkeypatch.setattr(rw, "start_watch_process", lambda *args, **kwargs: proc)

    def wait(*args, **kwargs):
        raise failure("synthetic stop")

    monkeypatch.setattr(rw, "_wait_with_port_check", wait)
    monkeypatch.setattr(rw, "stop_watch_tree", cleaned.append)
    if failure is KeyboardInterrupt:
        assert rw.main() == 0
    else:
        with pytest.raises(RuntimeError, match="synthetic stop"):
            rw.main()
    assert cleaned == [proc]
    assert not (tmp_path / "data" / "watchdog_synthetic.pid").exists()


def _open_process(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.WaitForSingleObject.restype = ctypes.c_ulong
    kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel.TerminateProcess.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    # Keep the original process object pinned, avoiding PID reuse in cleanup/asserts.
    handle = kernel.OpenProcess(0x00100000 | 0x0001, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return kernel, handle


def test_real_windows_three_level_tree_removed_and_other_process_survives(tmp_path):
    worker = tmp_path / "synthetic_tree.py"
    worker.write_text(
        "import os, pathlib, subprocess, sys, time\n"
        "depth = int(sys.argv[1])\n"
        "root = pathlib.Path(sys.argv[2])\n"
        "if depth < 2:\n"
        "    subprocess.Popen([sys.executable, '-I', '-B', __file__, str(depth+1), str(root)], "
        "creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))\n"
        "(root / ('worker_%d.pid' % depth)).write_text(str(os.getpid()), encoding='ascii')\n"
        "while True: time.sleep(60)\n",
        encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen([sys.executable, "-I", "-B", str(worker), "0", str(tmp_path)],
                            creationflags=flags)
    unrelated = subprocess.Popen([sys.executable, "-I", "-B", "-c",
                                  "import time; time.sleep(60)"], creationflags=flags)
    handles = []
    try:
        deadline = time.monotonic() + 10
        paths = [tmp_path / ("worker_%d.pid" % n) for n in range(3)]
        while not all(p.exists() and p.stat().st_size for p in paths):
            assert proc.poll() is None
            if time.monotonic() >= deadline:
                pytest.fail("synthetic tree did not become ready")
            time.sleep(0.02)
        pids = [int(p.read_text(encoding="ascii")) for p in paths]
        for pid in pids:
            handles.append(_open_process(pid))
        assert unrelated.poll() is None
        rw.stop_watch_tree(proc, timeout=8)
        for kernel, handle in handles:
            assert kernel.WaitForSingleObject(handle, 5000) == 0
        assert unrelated.poll() is None
        rw.stop_watch_tree(proc, timeout=1)  # already exited is harmless
    finally:
        # These handles refer only to our synthetic processes, never real services.
        if proc.poll() is None:
            try:
                rw.stop_watch_tree(proc, timeout=5)
            except RuntimeError:
                proc.kill()
        for kernel, handle in handles:
            if kernel.WaitForSingleObject(handle, 0) != 0:
                kernel.TerminateProcess(handle, 1)
            kernel.CloseHandle(handle)
        if unrelated.poll() is None:
            unrelated.terminate()
        unrelated.wait(timeout=5)
        proc.wait(timeout=5)
