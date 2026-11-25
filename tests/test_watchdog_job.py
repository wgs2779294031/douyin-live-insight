"""Owned Windows jobs use only synthetic processes and temporary files."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_watchdog as rw
import windows_job as wj


pytestmark = pytest.mark.skipif(os.name != "nt", reason="real Windows job objects")
_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _pin(pid):
    kernel = wj._kernel()
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000 | 0x0001, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return kernel, handle


def _dispose_pins(handles):
    for kernel, handle in handles:
        try:
            if kernel.WaitForSingleObject(handle, 0) != 0:
                kernel.TerminateProcess(handle, 1)
                kernel.WaitForSingleObject(handle, 5000)
        finally:
            kernel.CloseHandle(handle)


def _tree_script(tmp_path):
    worker = tmp_path / "job_tree.py"
    worker.write_text(
        "import os,pathlib,subprocess,sys,time\n"
        "depth=int(sys.argv[1]); root=pathlib.Path(sys.argv[2])\n"
        "if depth < 2:\n"
        "    subprocess.Popen([sys.executable,'-I','-B',__file__,str(depth+1),str(root)],"
        " creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))\n"
        "(root / ('worker_%d.pid' % depth)).write_text(str(os.getpid()),encoding='ascii')\n"
        "while True: time.sleep(60)\n",
        encoding="utf-8")
    return worker


def _ready(proc, tmp_path):
    paths = [tmp_path / ("worker_%d.pid" % depth) for depth in range(3)]
    deadline = time.monotonic() + 12
    while not all(p.exists() and p.stat().st_size for p in paths):
        if proc.poll() is not None:
            out, err = proc.communicate(timeout=3)
            pytest.fail("synthetic owner exited before tree ready: %r %r" % (out, err))
        if time.monotonic() >= deadline:
            pytest.fail("synthetic job tree did not become ready")
        time.sleep(.02)
    return [int(p.read_text(encoding="ascii")) for p in paths]


def _stop_unrelated(proc):
    if proc.poll() is None:
        proc.kill()
    proc.communicate(timeout=5)


def test_job_handle_is_noninheritable_and_structures_match_win64():
    job = wj.WindowsJob()
    try:
        flags = wintypes.DWORD()
        kernel = job._kernel
        kernel.GetHandleInformation.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetHandleInformation.restype = wintypes.BOOL
        assert kernel.GetHandleInformation(job._handle, ctypes.byref(flags))
        assert flags.value & 1 == 0
        assert ctypes.sizeof(wj._THREADENTRY32) == 28
        assert ctypes.sizeof(wj._BASIC_ACCOUNTING_INFORMATION) == 48
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            assert ctypes.sizeof(wj._EXTENDED_LIMIT_INFORMATION) == 144
    finally:
        job.close(timeout=2)
    job.close(timeout=2)


@pytest.mark.parametrize("failure_point", ["assign", "resume"])
def test_failure_before_first_instruction_kills_suspended_child(tmp_path, monkeypatch,
                                                               failure_point):
    marker = tmp_path / "must_not_exist"
    handles = []
    resumed = []

    def fail_assign(self, proc):
        handles.append(_pin(proc.pid))
        raise OSError("synthetic assign failure")

    def fail_resume(pid, kernel):
        handles.append(_pin(pid))
        raise OSError("synthetic resume failure")

    if failure_point == "assign":
        monkeypatch.setattr(wj.WindowsJob, "assign", fail_assign)
        monkeypatch.setattr(wj, "_resume_suspended_process",
                            lambda *_args: resumed.append(True))
    else:
        monkeypatch.setattr(wj, "_resume_suspended_process", fail_resume)
    try:
        with pytest.raises(OSError, match="synthetic"):
            wj.spawn_in_job(
                [sys.executable, "-I", "-B", "-c",
                 "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('ran')",
                 str(marker)], cwd=str(tmp_path), creationflags=_FLAGS)
        assert handles
        assert all(kernel.WaitForSingleObject(handle, 3000) == 0
                   for kernel, handle in handles)
        assert not marker.exists()
        assert not resumed
    finally:
        _dispose_pins(handles)


def test_root_already_exited_still_reaps_descendants_and_preserves_other_process(tmp_path):
    worker = _tree_script(tmp_path)
    proc = rw.start_watch_process(
        [sys.executable, "-I", "-B", str(worker), "0", str(tmp_path)])
    unrelated = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", "import time;time.sleep(60)"],
        creationflags=_FLAGS, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    handles = []
    try:
        pids = _ready(proc, tmp_path)
        handles = [_pin(pid) for pid in pids]
        proc.kill()                 # Kill only watch, intentionally orphaning its children.
        proc.wait(timeout=5)
        assert any(kernel.WaitForSingleObject(handle, 0) != 0
                   for kernel, handle in handles[1:])
        rw.stop_watch_tree(proc, timeout=5)
        assert all(kernel.WaitForSingleObject(handle, 5000) == 0
                   for kernel, handle in handles)
        assert unrelated.poll() is None
        rw.stop_watch_tree(proc, timeout=1)
    finally:
        try:
            rw.stop_watch_tree(proc, timeout=5)
        finally:
            _dispose_pins(handles)
            _stop_unrelated(unrelated)
            proc.wait(timeout=5)


def test_killing_watchdog_owner_closes_job_and_reaps_all_descendants(tmp_path):
    worker = _tree_script(tmp_path)
    manager = tmp_path / "job_owner.py"
    manager.write_text(
        "import pathlib,subprocess,sys,time\n"
        "sys.path.insert(0,sys.argv[1])\n"
        "from windows_job import spawn_in_job\n"
        "proc=spawn_in_job([sys.executable,'-I','-B',sys.argv[2],'0',sys.argv[3]],"
        "cwd=sys.argv[3],creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))\n"
        "while True:time.sleep(60)\n",
        encoding="utf-8")
    owner = subprocess.Popen(
        [sys.executable, "-I", "-B", str(manager), str(Path(wj.__file__).parent),
         str(worker), str(tmp_path)], cwd=str(tmp_path), creationflags=_FLAGS,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    unrelated = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", "import time;time.sleep(60)"],
        creationflags=_FLAGS, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    handles = []
    try:
        handles = [_pin(pid) for pid in _ready(owner, tmp_path)]
        owner.kill()                # No Python finally, no taskkill /T.
        owner.wait(timeout=5)
        assert all(kernel.WaitForSingleObject(handle, 5000) == 0
                   for kernel, handle in handles)
        assert unrelated.poll() is None
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=5)
        _dispose_pins(handles)
        _stop_unrelated(unrelated)


def test_main_closes_each_finished_job_before_next_watch(monkeypatch, tmp_path):
    proc = object()
    started, stopped = [], []
    monkeypatch.setattr(rw, "ROOT", tmp_path)
    monkeypatch.setattr(rw, "_LOG_PATH", None)
    monkeypatch.setattr(rw, "log", lambda *_args: None)
    monkeypatch.setattr(rw, "ensure_browser", lambda *_args: True)
    monkeypatch.setattr(rw.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(rw.sys, "argv", ["run_watchdog", "--sec-uid", "synthetic",
                                       "--tag", "synthetic"])
    monkeypatch.setattr(rw, "_wait_with_port_check", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(rw, "plan_backoff", lambda *_args: (0, 1))
    monkeypatch.setattr(rw.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(rw, "stop_watch_tree", stopped.append)

    def spawn(command):
        started.append(command)
        if len(started) == 1:
            return proc
        assert stopped == [proc]
        raise KeyboardInterrupt

    monkeypatch.setattr(rw, "start_watch_process", spawn)
    assert rw.main() == 0
    assert len(started) == 2 and stopped == [proc]
    assert not (tmp_path / "data" / "watchdog_synthetic.pid").exists()


def test_manager_stop_preserves_browser_child_and_reaps_private_job(tmp_path, monkeypatch):
    """The browser is a child of watchdog, not an unrelated sibling of watchdog."""
    import anchor_manager as am
    worker = _tree_script(tmp_path)
    owner_script = tmp_path / "browser_and_job_owner.py"
    owner_script.write_text(
        "import os,pathlib,subprocess,sys,time\n"
        "sys.path.insert(0,sys.argv[1])\n"
        "import run_watchdog as rw\n"
        "root=pathlib.Path(sys.argv[2])\n"
        "pf=root/'watchdog_synthetic.pid'\n"
        "pf.write_text(str(os.getpid()),encoding='ascii')\n"
        "rw.write_stop_protocol(pf.with_suffix('.protocol.json'))\n"
        "browser=subprocess.Popen([sys.executable,'-I','-B','-c','import time;time.sleep(60)'],"
        "creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))\n"
        "(root/'browser.pid').write_text(str(browser.pid),encoding='ascii')\n"
        "proc=rw.start_watch_process([sys.executable,'-I','-B',sys.argv[3],'0',str(root)])\n"
        "while True:time.sleep(60)\n",
        encoding="utf-8")
    owner = subprocess.Popen(
        [sys.executable, "-I", "-B", str(owner_script), str(Path(rw.__file__).parent),
         str(tmp_path), str(worker)], cwd=str(tmp_path), creationflags=_FLAGS,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    handles, browser_handle = [], None
    try:
        for pid in _ready(owner, tmp_path):
            handles.append(_pin(pid))
        browser_handle = _pin(int((tmp_path / "browser.pid").read_text(encoding="ascii")))
        monkeypatch.setattr(am, "DATA", tmp_path)
        result = am.stop("watch", "synthetic")
        assert result["ok"] and result["state"] == "stopped", result
        owner.communicate(timeout=5)
        assert all(kernel.WaitForSingleObject(handle, 5000) == 0
                   for kernel, handle in handles)
        kernel, handle = browser_handle
        assert kernel.WaitForSingleObject(handle, 0) == 258  # still alive
        assert not (tmp_path / "watchdog_synthetic.pid").exists()
        assert not (tmp_path / "watchdog_synthetic.protocol.json").exists()
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=5)
        _dispose_pins(handles + ([browser_handle] if browser_handle else []))
