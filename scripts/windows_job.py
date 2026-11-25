"""Windows ownership for one suspended watch and all of its descendants.

The job handle is private and non-inheritable. Its final close kills the entire
job, including when the watchdog itself is terminated without running finally.
Only a newly created suspended process is assigned; no user process is adopted.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import math
import os
import subprocess
import time


CREATE_SUSPENDED = 0x00000004
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_HANDLE_FLAG_INHERIT = 0x00000001
_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002
_THREAD_QUERY_LIMITED_INFORMATION = 0x0800
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_MAX_DWORD = 0xFFFFFFFF


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class _THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


def _kernel():
    if os.name != "nt":
        raise NotImplementedError("watch jobs require Windows")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        "SetHandleInformation": ([wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD], wintypes.BOOL),
        "SetInformationJobObject": (
            [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        "QueryInformationJobObject": (
            [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
             ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "CreateToolhelp32Snapshot": ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
        "Thread32First": ([wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32)], wintypes.BOOL),
        "Thread32Next": ([wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32)], wintypes.BOOL),
        "OpenThread": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "GetProcessIdOfThread": ([wintypes.HANDLE], wintypes.DWORD),
        "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(kernel, name)
        fn.argtypes = args
        fn.restype = result
    return kernel


def _failed(operation):
    error = ctypes.get_last_error()
    return OSError(error, "%s: %s" % (operation, ctypes.FormatError(error)))


class WindowsJob:
    """A private job for exactly one owned watch tree; close is idempotent."""

    def __init__(self):
        self._kernel = _kernel()
        self._handle = self._kernel.CreateJobObjectW(None, None)
        if not self._handle:
            raise _failed("CreateJobObjectW")
        try:
            if not self._kernel.SetHandleInformation(self._handle, _HANDLE_FLAG_INHERIT, 0):
                raise _failed("SetHandleInformation")
            limits = _EXTENDED_LIMIT_INFORMATION()
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self._kernel.SetInformationJobObject(
                    self._handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                    ctypes.byref(limits), ctypes.sizeof(limits)):
                raise _failed("SetInformationJobObject")
        except BaseException:
            self._kernel.CloseHandle(self._handle)
            self._handle = None
            raise

    def assign(self, proc):
        # Popen retains its own process handle, so the PID remains associated
        # with this owned process while we open a handle with assignment rights.
        process = self._kernel.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, proc.pid)
        if not process:
            raise _failed("OpenProcess")
        try:
            if not self._kernel.AssignProcessToJobObject(self._handle, process):
                raise _failed("AssignProcessToJobObject")
        finally:
            self._kernel.CloseHandle(process)

    def active_processes(self):
        """Count the whole owned tree, even after its original root exits."""
        if self._handle is None:
            return 0
        info = _BASIC_ACCOUNTING_INFORMATION()
        if not self._kernel.QueryInformationJobObject(
                self._handle, _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                ctypes.byref(info), ctypes.sizeof(info), None):
            raise _failed("QueryInformationJobObject")
        return int(info.ActiveProcesses)

    def release_empty(self):
        """Release without terminating only after all owned descendants exit."""
        if self._handle is None:
            return True
        if self.active_processes() != 0:
            return False
        handle = self._handle
        if not self._kernel.CloseHandle(handle):
            raise _failed("CloseHandle(job)")
        self._handle = None
        return True

    def close(self, timeout=15.0):
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("job close timeout must be finite and nonnegative")
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            # Do not wait for root liveness: it may already have exited while
            # descendants continue running. Reap them before starting a new tree.
            if not self._kernel.TerminateJobObject(handle, 1):
                raise _failed("TerminateJobObject")
            deadline = time.monotonic() + timeout
            while True:
                info = _BASIC_ACCOUNTING_INFORMATION()
                if not self._kernel.QueryInformationJobObject(
                        handle, _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                        ctypes.byref(info), ctypes.sizeof(info), None):
                    raise _failed("QueryInformationJobObject")
                if info.ActiveProcesses == 0:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("owned watch job still has %d process(es)"
                                       % info.ActiveProcesses)
                time.sleep(min(0.02, remaining))
        finally:
            # This final close also enforces KILL_ON_JOB_CLOSE if termination or
            # accounting above failed. Never retain an unmanaged job handle.
            if not self._kernel.CloseHandle(handle):
                raise _failed("CloseHandle(job)")

    def __del__(self):
        handle = getattr(self, "_handle", None)
        if handle is not None:
            self._handle = None
            try:
                self._kernel.CloseHandle(handle)
            except Exception:
                pass


def _resume_suspended_process(pid, kernel):
    snapshot = kernel.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if snapshot == _INVALID_HANDLE_VALUE:
        raise _failed("CreateToolhelp32Snapshot")
    try:
        entry = _THREADENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        threads = []
        found = kernel.Thread32First(snapshot, ctypes.byref(entry))
        while found:
            if entry.th32OwnerProcessID == pid:
                threads.append(entry.th32ThreadID)
            entry.dwSize = ctypes.sizeof(entry)
            found = kernel.Thread32Next(snapshot, ctypes.byref(entry))
        # No program code has run yet: a freshly created suspended process has
        # only its primary thread. Ambiguous snapshots must fail closed.
        if len(threads) != 1:
            raise RuntimeError("cannot identify suspended watch primary thread: pid=%s count=%s"
                               % (pid, len(threads)))
    finally:
        kernel.CloseHandle(snapshot)
    thread = kernel.OpenThread(_THREAD_SUSPEND_RESUME | _THREAD_QUERY_LIMITED_INFORMATION,
                               False, threads[0])
    if not thread:
        raise _failed("OpenThread")
    try:
        if kernel.GetProcessIdOfThread(thread) != pid:
            raise RuntimeError("suspended watch thread ownership changed")
        previous_count = kernel.ResumeThread(thread)
        if previous_count == _MAX_DWORD:
            raise _failed("ResumeThread")
        if previous_count != 1:
            raise RuntimeError("unexpected suspended watch thread count: %s" % previous_count)
    finally:
        kernel.CloseHandle(thread)


def spawn_in_job(command, **kwargs):
    """Create a watch suspended, assign it, then permit its first instruction.

    Assignment/resume failure terminates the owned suspended process and raises.
    No already-running process is ever returned outside the job protocol.
    """
    job = WindowsJob()
    proc = None
    try:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_SUSPENDED
        proc = subprocess.Popen(command, **kwargs)
        job.assign(proc)
        proc._watch_job = job
        _resume_suspended_process(proc.pid, job._kernel)
        return proc
    except BaseException:
        try:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
        finally:
            job.close(timeout=10)
        raise
