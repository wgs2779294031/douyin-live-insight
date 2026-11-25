"""Only newly spawned local-app resources are owned; PID files cannot confer it."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import math
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys

_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from backfill_tasks import process_identity, process_parent_pid
from windows_job import spawn_in_job


def same_identity(left, right):
    return (isinstance(left, dict) and isinstance(right, dict)
            and left.get("state") == right.get("state") == "alive"
            and type(left.get("pid")) is int and left["pid"] > 0
            and type(right.get("pid")) is int
            and left["pid"] == right.get("pid")
            and type(left.get("created")) in (int, float)
            and math.isfinite(left["created"]) and left["created"] > 0
            and type(right.get("created")) in (int, float)
            and left["created"] == right["created"])


class OwnedProcess:
    """A held Popen handle plus its private, non-inherited Windows Job.

    A normal close never interrupts a remaining ASR descendant. State-query or
    force-close errors remain unknown instead of being converted into stopped.
    """

    def __init__(self, proc):
        self.proc = proc
        self.job = proc._watch_job
        self.identity = process_identity(proc.pid)
        self.closed = False
        self.last_error = ""
        self._uncertain = False

    @classmethod
    def start(cls, command, *, env, cwd, log_path):
        if os.name != "nt":
            raise NotImplementedError("local-app owned trees require Windows Jobs")
        if not isinstance(command, (list, tuple)) or not command or any(
                not isinstance(item, str) or not item or "\0" in item for item in command):
            raise ValueError("command must be an argument list")
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab", buffering=0) as output:
            proc = spawn_in_job(list(command), cwd=str(cwd), env=dict(env),
                stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                close_fds=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            return cls(proc)
        except BaseException:
            proc._watch_job.close(timeout=10)
            raise

    def active_count(self):
        if self._uncertain:
            return None
        if self.closed:
            return 0
        try:
            count = self.job.active_processes()
            if type(count) is not int or count < 0:
                raise ValueError
            return count
        except Exception:
            self.last_error = "process_tree_state_unavailable"
            return None

    def worker_identity(self, receipt):
        """Validate ACK process facts; the caller also checks its private nonce.

        Windows venv launchers can redirect to one direct child. No unrelated
        process, later PID occupant, or unverifiable parent is adopted.
        """
        if self.closed or self._uncertain or not isinstance(receipt, dict):
            return None
        pid, created = receipt.get("pid"), receipt.get("created")
        if (type(pid) is not int or pid <= 0 or type(created) not in (int, float)
                or not math.isfinite(created) or created <= 0):
            return None
        root = process_identity(self.proc.pid)
        if self.proc.poll() is not None or not same_identity(self.identity, root):
            return None
        actual = process_identity(pid)
        claimed = {"state": "alive", "pid": pid, "created": created}
        if not same_identity(claimed, actual):
            return None
        if pid != self.proc.pid and (created < self.identity["created"]
                or process_parent_pid(pid) != self.proc.pid):
            return None
        return actual

    def close(self, force=False, *, timeout=15.0):
        if self._uncertain:
            return False
        if self.closed:
            return True
        if not force and self.active_count() != 0:
            return False
        try:
            if force:
                self.job.close(timeout=timeout)
            elif not self.job.release_empty():
                return False
            self.closed = True
            self.proc.poll()
            return True
        except Exception:
            self.last_error = "process_tree_close_unconfirmed"
            # WindowsJob.close closes the handle even on a failed termination
            # query. Losing that handle must not manufacture an empty-tree fact.
            if force:
                self._uncertain = True
            return False


class _TCP_ROW(ctypes.Structure):
    _fields_ = [(name, wintypes.DWORD) for name in (
        "state", "local_address", "local_port", "remote_address", "remote_port", "pid")]


def _tcp_listener_rows():
    # GetExtendedTcpTable, AF_INET + TCP_TABLE_OWNER_PID_LISTENER (3).
    # https://learn.microsoft.com/windows/win32/api/iphlpapi/nf-iphlpapi-getextendedtcptable
    api = ctypes.WinDLL("iphlpapi", use_last_error=True).GetExtendedTcpTable
    api.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                    wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
    api.restype = wintypes.DWORD
    size = wintypes.DWORD()
    code = api(None, ctypes.byref(size), False, socket.AF_INET, 3, 0)
    for _ in range(4):
        if code != 122 or not 4 <= size.value <= 32 * 1024 * 1024:
            raise OSError("TCP listener table unavailable")
        buffer = ctypes.create_string_buffer(size.value)
        code = api(buffer, ctypes.byref(size), False, socket.AF_INET, 3, 0)
        if code == 122:
            continue
        if code:
            raise OSError("TCP listener table unavailable")
        count = wintypes.DWORD.from_buffer_copy(buffer.raw[:4]).value
        row_size = ctypes.sizeof(_TCP_ROW)
        if 4 + count * row_size > min(size.value, len(buffer)):
            raise ValueError("invalid TCP listener table size")
        rows = []
        for index in range(count):
            row = _TCP_ROW.from_buffer_copy(buffer, 4 + index * row_size)
            rows.append((socket.inet_ntoa(struct.pack("=I", row.local_address)),
                         socket.ntohs(row.local_port & 0xffff), int(row.pid), int(row.state)))
        return rows
    raise OSError("TCP listener table changed repeatedly")


def listener_pid(port, bind="127.0.0.1"):
    """Read IPv4 listener owners without connecting or interpreting HTTP 200.

    Wildcard listeners also conflict with a concrete bind. Only Windows IPv4
    endpoints are supported by this local application; failures are unknown.
    """
    if type(port) is not int or not 1 <= port <= 65535 or not isinstance(bind, str):
        return {"state": "unknown", "pids": [], "error": "invalid_endpoint"}
    try:
        socket.inet_pton(socket.AF_INET, bind)
    except (OSError, ValueError):
        return {"state": "unknown", "pids": [], "error": "invalid_endpoint"}
    if os.name != "nt":
        return {"state": "unknown", "pids": [], "error": "unsupported_platform"}
    try:
        rows = _tcp_listener_rows()
        matched = [pid for address, row_port, pid, state in rows
            if state == 2 and row_port == port
            and (bind == "0.0.0.0" or address in (bind, "0.0.0.0"))]
        if any(type(pid) is not int or pid <= 0 for pid in matched):
            return {"state": "unknown", "pids": [], "error": "listener_owner_unavailable"}
        pids = sorted(set(matched))
        return {"state": "listening" if pids else "vacant", "pids": pids}
    except Exception:
        return {"state": "unknown", "pids": [], "error": "tcp_table_unavailable"}
