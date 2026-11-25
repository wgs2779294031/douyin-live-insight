"""Durable, single-resource leases for the existing portrait backfill command.

No scheduler: reading a record never starts work. Unknown runs are reused until
an explicit new force request is accepted. Locks cover only local state I/O.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date as Date
import copy
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid

from file_lock import file_lock
from llm_client import application_control, canonical_llm_source

SCHEMA = 1
LAUNCH_ENV = "DOUYIN_BACKFILL_LAUNCH_TOKEN"
DEFAULT_PARAMETERS = {"every": 120, "min_danmaku": 1, "max_calls": 40}
ACTIVE = {"starting", "running"}
TERMINAL = {"succeeded", "partial", "failed", "blocked", "interrupted", "unknown"}
_TASK = re.compile(r"^bf_([0-9a-f]{64})_([0-9a-f]{32})$")
_REQUEST = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


class TaskError(RuntimeError):
    def __init__(self, code="task_state_unavailable", message="任务状态不可确认，已阻止继续执行。"):
        self.code = code
        super().__init__(message)


class StateWriteError(TaskError):
    pass


class TaskNotFound(TaskError):
    def __init__(self):
        super().__init__("task_not_found", "没有找到对应任务。")


def process_identity(pid):
    """alive/dead/unknown, with an OS creation identity (never a PID alone)."""
    if type(pid) is not int or pid <= 0:
        return {"state": "dead", "pid": pid, "created": None}
    if os.name == "nt":
        try:
            from ctypes import wintypes
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            api.OpenProcess.restype = wintypes.HANDLE
            api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
            api.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = api.OpenProcess(0x1000, False, pid)
            if not handle:
                state = "dead" if ctypes.get_last_error() in (87, 1168) else "unknown"
                return {"state": state, "pid": pid, "created": None}
            try:
                exit_code = wintypes.DWORD()
                stamps = [ctypes.c_ulonglong() for _ in range(4)]
                if not api.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    raise OSError
                if exit_code.value != 259:
                    return {"state": "dead", "pid": pid, "created": None}
                if not api.GetProcessTimes(handle, *(ctypes.byref(value) for value in stamps)):
                    raise OSError
                return {"state": "alive", "pid": pid,
                        "created": stamps[0].value / 1e7 - 11644473600.0}
            finally:
                api.CloseHandle(handle)
        except Exception:
            return {"state": "unknown", "pid": pid, "created": None}
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
        if text[0] == "Z":
            return {"state": "dead", "pid": pid, "created": None}
        boot = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines()
                    if line.startswith("btime "))
        return {"state": "alive", "pid": pid,
                "created": boot + int(text[19]) / os.sysconf("SC_CLK_TCK")}
    except FileNotFoundError:
        return {"state": "dead", "pid": pid, "created": None}
    except Exception:
        return {"state": "unknown", "pid": pid, "created": None}


def process_parent_pid(pid):
    """Read only the selected process' parent (Windows venv redirector support)."""
    if os.name != "nt":
        try:
            return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except Exception:
            return None
    try:
        from ctypes import wintypes
        class BasicInfo(ctypes.Structure):
            _fields_ = [("reserved", ctypes.c_void_p), ("peb", ctypes.c_void_p),
                        ("reserved2", ctypes.c_void_p * 2), ("unique_pid", ctypes.c_size_t),
                        ("parent_pid", ctypes.c_size_t)]
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x1000 | 0x0400, False, pid)
        if not handle:
            return None
        try:
            native = ctypes.WinDLL("ntdll")
            native.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, wintypes.ULONG,
                                                        ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p]
            value = BasicInfo()
            if native.NtQueryInformationProcess(handle, 0, ctypes.byref(value), ctypes.sizeof(value), None) != 0:
                return None
            return int(value.parent_pid) if value.unique_pid == pid else None
        finally:
            api.CloseHandle(handle)
    except Exception:
        return None


def _same_identity(left, right):
    return (isinstance(left, dict) and isinstance(right, dict)
            and left.get("state") == right.get("state") == "alive"
            and left.get("pid") == right.get("pid")
            and type(left.get("created")) in (int, float)
            and type(right.get("created")) in (int, float)
            and left["created"] == right["created"])


def _clone(value):
    return copy.deepcopy(value)


def _valid_identity(value):
    return (value is None or isinstance(value, dict) and value.get("state") == "alive"
            and type(value.get("pid")) is int and value["pid"] > 0
            and type(value.get("created")) in (int, float) and math.isfinite(value["created"])
            and value["created"] > 0)


def _valid_epoch(value):
    return isinstance(value, list) and len(value) == 3 and all(type(item) is int and item >= 0 for item in value)


def _parameters(value):
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - set(DEFAULT_PARAMETERS):
        raise TaskError("invalid_parameters", "画像参数无效。")
    result = dict(DEFAULT_PARAMETERS, **value)
    limits = {"every": (1, 86400), "min_danmaku": (0, 100000), "max_calls": (1, 10000)}
    if any(type(result[key]) is not int or not low <= result[key] <= high
           for key, (low, high) in limits.items()):
        raise TaskError("invalid_parameters", "画像参数无效。")
    return result


class TaskStore:
    def __init__(self, config_path, data_dir, *, python_exe=None, spawn=None,
                 identity_reader=None, parent_reader=None, now=None):
        self.config_path = canonical_llm_source(str(config_path))
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.directory = self.data_dir / "backfill_tasks"
        self.python_exe = str(python_exe or sys.executable)
        self._spawn_override = spawn
        self._identity_reader = identity_reader or process_identity
        self._parent_reader = parent_reader or process_parent_pid
        self._now = now or time.time

    def _identity(self, pid):
        try:
            value = self._identity_reader(pid)
            if (isinstance(value, dict) and value.get("state") in ("alive", "dead", "unknown")
                    and (value.get("state") != "alive" or type(value.get("created")) in (int, float)
                         and math.isfinite(value["created"]))):
                return dict(value, pid=pid)
        except Exception:
            pass
        return {"state": "unknown", "pid": pid, "created": None}

    def _self(self):
        identity = self._identity(os.getpid())
        if identity["state"] != "alive":
            raise TaskError("identity_unavailable", "无法确认当前执行进程身份。")
        return identity

    def _candidate_belongs(self, candidate, launch):
        if not candidate or not _same_identity(self._identity(candidate["pid"]), candidate):
            return False
        if not _same_identity(self._identity(launch["pid"]), launch):
            return False
        if candidate["pid"] == launch["pid"]:
            return _same_identity(candidate, launch)
        if candidate["created"] < launch["created"]:
            return False
        try:
            parent = self._parent_reader(candidate["pid"])
        except Exception:
            return False
        return (parent == launch["pid"]
                and _same_identity(self._identity(candidate["pid"]), candidate)
                and _same_identity(self._identity(launch["pid"]), launch))

    def _spawn_identity(self, process):
        """Bind the original Popen process once; never adopt a recycled PID."""
        poll = getattr(process, "poll", None)
        if callable(poll) and poll() is not None:
            return {"state": "dead", "pid": process.pid, "created": None}
        handle = getattr(process, "_handle", None)
        if os.name == "nt" and handle is not None:
            try:
                from ctypes import wintypes
                api = ctypes.WinDLL("kernel32", use_last_error=True)
                api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
                api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
                code = wintypes.DWORD()
                stamps = [ctypes.c_ulonglong() for _ in range(4)]
                original = wintypes.HANDLE(int(handle))  # Borrowed; Popen retains ownership.
                if not api.GetExitCodeProcess(original, ctypes.byref(code)):
                    raise OSError
                if code.value != 259:
                    return {"state": "dead", "pid": process.pid, "created": None}
                if not api.GetProcessTimes(original, *(ctypes.byref(value) for value in stamps)):
                    raise OSError
                return {"state": "alive", "pid": process.pid,
                        "created": stamps[0].value / 1e7 - 11644473600.0}
            except Exception:
                return {"state": "unknown", "pid": process.pid, "created": None}
        return self._identity(process.pid)

    def _resource(self, tag, date):
        if (not isinstance(tag, str) or not 1 <= len(tag.strip()) <= 100
                or re.search(r'[\x00-\x1f<>:"/\\|?*]', tag.strip())
                or tag.strip() in (".", "..") or tag.strip().endswith((".", " "))):
            raise TaskError("invalid_tag", "主播标识无效。")
        tag = unicodedata.normalize("NFC", tag.strip())
        if tag.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}:
            raise TaskError("invalid_tag", "主播标识无效。")
        try:
            if not isinstance(date, str) or Date.fromisoformat(date).isoformat() != date:
                raise ValueError
        except (TypeError, ValueError):
            raise TaskError("invalid_date", "日期必须是有效的YYYY-MM-DD。") from None
        key = {"data_root": os.path.normcase(str(self.data_dir)), "tag_key": tag.casefold(),
               "data_date": date}
        digest = hashlib.sha256(json.dumps(key, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return tag, date, digest, key

    def _path(self, task_id):
        match = _TASK.fullmatch(task_id) if isinstance(task_id, str) else None
        if match is None:
            raise TaskError("invalid_task_id", "任务标识无效。")
        return self.directory / (match.group(1) + ".json")

    def _read(self, path):
        try:
            if path.is_symlink():
                raise ValueError
            content = path.read_bytes()
        except FileNotFoundError:
            return None
        except Exception:
            raise TaskError() from None
        try:
            if len(content) > 8 * 1024 * 1024:
                raise ValueError
            value = json.loads(content)
            if (not isinstance(value, dict) or value.get("schema") != SCHEMA
                    or value.get("kind") != "portrait_backfill"
                    or not isinstance(value.get("resource"), dict)
                    or value["resource"].get("digest") != path.stem
                    or value["resource"].get("data_root") != os.path.normcase(str(self.data_dir))
                    or not isinstance(value.get("runs"), list)
                    or not isinstance(value.get("requests"), dict)):
                raise ValueError
            ids = set()
            for run in value["runs"]:
                task_id = run.get("task_id") if isinstance(run, dict) else None
                lease = run.get("lease", {}) if isinstance(run, dict) else {}
                if (not isinstance(task_id, str) or not _TASK.fullmatch(task_id)
                        or self._path(task_id) != path or task_id in ids
                        or run.get("status") not in ACTIVE | TERMINAL
                        or not isinstance(run.get("lease"), dict)
                        or not isinstance(run.get("counts"), dict)
                        or any(not isinstance(key, str) or type(item) is not int or item < 0
                               for key, item in run["counts"].items())
                        or not _valid_epoch(run.get("control_epoch"))
                        or type(run.get("may_have_sent")) is not bool
                        or type(run.get("dispatch_settled")) is not bool
                        or type(run.get("dispatch_count")) is not int or run["dispatch_count"] < 0
                        or not _valid_identity(run.get("owner"))
                        or any(type(lease.get(flag)) is not bool for flag in ("ack", "revoked", "released"))
                        or not isinstance(lease.get("token"), str) or not 32 <= len(lease["token"]) <= 128
                        or type(lease.get("deadline")) not in (int, float) or not math.isfinite(lease["deadline"])
                        or any(not _valid_identity(lease.get(key)) for key in ("launcher", "candidate", "expected"))
                        or not isinstance(run.get("tag"), str)
                        or self._resource(run["tag"], run.get("data_date"))[2] != path.stem
                        or run.get("input") is not None and not isinstance(run["input"], dict)
                        or run.get("output") is not None and not isinstance(run["output"], (dict, str))
                        or run.get("credential_fingerprint") is not None and not re.fullmatch(r"[0-9a-f]{64}", run["credential_fingerprint"])
                        or _parameters(run.get("parameters")) != run.get("parameters")):
                    raise ValueError
                ids.add(task_id)
            if any(not _REQUEST.fullmatch(key) or value not in ids for key, value in value["requests"].items()):
                raise ValueError
            return value
        except Exception:
            raise TaskError() from None

    def _write(self, path, value):
        temporary = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            if self.directory.resolve() != self.directory or path.is_symlink():
                raise OSError
            content = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
            if len(content) > 8 * 1024 * 1024:
                raise ValueError
            fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=self.directory)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
            if path.read_bytes() != content:
                raise OSError
            if os.name != "nt":
                directory_fd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except Exception:
            raise StateWriteError() from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    @contextmanager
    def _locked(self, path):
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with file_lock(path):
                yield
        except TaskError:
            raise
        except Exception:
            raise TaskError() from None

    def _credentials(self):
        try:
            from llm_service import load_llm_config
            cfg = load_llm_config(self.config_path)
            rows = {name: {key: str(row.get(key) or "").strip()
                           for key in ("api_key", "base_url", "model", "protocol")}
                    for name, row in cfg.providers.items() if isinstance(row, dict)}
            if not any(all(row[key] for key in ("api_key", "base_url", "model")) for row in rows.values()):
                return None
            return hashlib.sha256(json.dumps({"providers": rows, "active": cfg.active_provider,
                                            "timeout_seconds": cfg.timeout_seconds,
                                            "probe_timeout_seconds": cfg.probe_timeout_seconds},
                                            sort_keys=True).encode()).hexdigest()
        except Exception:
            return None

    def _owner_state(self, run):
        lease = run["lease"]
        if lease.get("released") or lease.get("revoked") and not lease.get("ack"):
            return "dead"
        owner = run.get("owner") or lease.get("candidate") or lease.get("launcher")
        if not isinstance(owner, dict) or type(owner.get("pid")) is not int:
            return "unknown"
        actual = self._identity(owner["pid"])
        if actual["state"] == "dead":
            return "dead"
        if actual["state"] == "alive" and not _same_identity(actual, owner):
            return "dead"  # This PID belongs to a different process.
        return "alive" if _same_identity(actual, owner) else "unknown"

    def _reconcile(self, manifest):
        changed = False
        for run in manifest["runs"]:
            if run["status"] not in ACTIVE and not (run["status"] == "unknown" and run.get("reason") == "owner_identity_unknown"):
                continue
            lease = run["lease"]
            owner_state = self._owner_state(run)
            if not lease.get("ack") and (owner_state == "dead" or self._now() > lease["deadline"]):
                lease.update(revoked=True, released=True)
                run.update(status="unknown", reason="startup_ack_timeout", finished_at=self._now())
                changed = True
            elif lease.get("ack") and owner_state != "alive":
                known_interruption = owner_state == "dead" and run.get("dispatch_settled") is True
                state = "interrupted" if known_interruption else "unknown"
                reason = ("owner_exited" if known_interruption else "owner_exited_unknown"
                          if owner_state == "dead" else "owner_identity_unknown")
                finished = self._now() if owner_state == "dead" else None
                if (run["status"] != state or run.get("reason") != reason
                        or owner_state == "dead" and run.get("finished_at") is None
                        or owner_state == "unknown" and run.get("finished_at") is not None):
                    run.update(status=state, reason=reason, finished_at=finished)
                    changed = True
        return changed

    def _public(self, run, tag=None):
        state = self._owner_state(run)
        raw_counts = run["counts"]
        counts = dict(raw_counts)
        counts.update(windows=raw_counts.get("windows_seen", raw_counts.get("windows", 0)),
                      generate_calls=raw_counts.get("calls", 0))
        for name in ("windows_total", "ok", "fail", "skipped", "unknown"):
            counts.setdefault(name, 0)
        raw_input, raw_output = run.get("input"), run.get("output")
        processed = _clone(run.get("processed")) if isinstance(run.get("processed"), dict) else {}
        for name in ("current_window_ts", "processed_ts_end"):
            if name in run:
                processed[name] = _clone(run[name])
        input_ref = None if raw_input is None else {
            "event_count": raw_input.get("records", raw_input.get("event_count", 0)),
            "invalid_count": raw_input.get("invalid_records", raw_input.get("invalid_count", 0)),
            "start_ts": raw_input.get("ts_min", raw_input.get("start_ts")),
            "end_ts": raw_input.get("ts_max", raw_input.get("end_ts")),
            "captured_at": raw_input.get("captured_at"), "sha256": raw_input.get("sha256"),
            "bytes": raw_input.get("bytes"), "ref": raw_input.get("input_ref", raw_input.get("ref"))}
        if isinstance(raw_output, str):
            raw_output = {"path": raw_output}
        output_ref = None if raw_output is None else {
            "ref": raw_output.get("path", raw_output.get("ref")), "task_id": run["task_id"],
            "rows": raw_output.get("rows", 0), "byte_start": raw_output.get("byte_start"),
            "byte_end": raw_output.get("byte_end"), "append": True}
        return {"task_id": run["task_id"], "type": "portrait_backfill", "tag": tag or run["tag"],
                "room_tag": tag or run["tag"], "purpose": "review",
                "data_date": run["data_date"], "state": run["status"], "status": run["status"],
                "parameters": _clone(run["parameters"]), "counts": counts,
                "input": input_ref, "output": output_ref,
                "submitted_at": run.get("submitted_at"), "started_at": run.get("started_at"),
                "finished_at": run.get("finished_at"), "reason": run.get("reason", ""),
                "processed": processed, "may_have_sent": bool(run.get("may_have_sent")),
                "current_window_ts": run.get("current_window_ts"),
                "processed_ts_end": run.get("processed_ts_end"),
                "dispatch_count": run.get("dispatch_count", 0),
                "can_redo": run["status"] not in ACTIVE and state == "dead",
                "owner_active": True if state == "alive" else False if state == "dead" else None}

    def _response(self, run, *, created=False, started=False, tag=None):
        public = self._public(run, tag)
        return {"ok": True, "created": created, "started": started, "reused": not created,
                "task_id": run["task_id"], "task": public, "status": public,
                "hint": "任务已登记；请按任务标识读取状态。" if created else "已复用已有任务，没有新增模型执行。"}

    def _legacy(self, tag, date):
        output = self.data_dir / f"portrait_{tag}_{date}.jsonl"
        try:
            nonempty = output.is_file() and output.stat().st_size > 0
            pid_path = self.data_dir / f"backfill_{tag}_{date}.pid"
            log_path = self.data_dir / f"backfill_{tag}_{date}.log"
            old_owner, old_log = pid_path.exists(), log_path.exists()
            owner_uncertain = old_owner
            if old_owner:
                try:
                    raw = pid_path.read_bytes()
                    text = raw.decode("utf-8-sig").strip()
                    if len(raw) <= 64 and re.fullmatch(r"[0-9]{1,10}", text):
                        owner_uncertain = self._identity(int(text))["state"] != "dead"
                except (OSError, ValueError, UnicodeError):
                    pass
        except OSError:
            raise TaskError() from None
        if not nonempty and not old_owner and not old_log:
            return None
        return {"task_id": None, "type": "portrait_backfill", "tag": tag, "data_date": date,
                "state": "legacy_unverified", "status": "legacy_unverified", "counts": {},
                "input": None, "output": {"path": output.name} if nonempty else None,
                "reason": "legacy_unverified", "can_redo": not owner_uncertain,
                "owner_active": None if owner_uncertain else False}

    def _reserve(self, tag, date, force, parameters, request_id, inline):
        tag, date, digest, resource = self._resource(tag, date)
        parameters = _parameters(parameters)
        if type(force) is not bool:
            raise TaskError("invalid_force", "重做开关必须是布尔值。")
        if request_id is None and not force:
            request_id = uuid.uuid4().hex
        if not isinstance(request_id, str) or not _REQUEST.fullmatch(request_id):
            raise TaskError("invalid_request_id", "请提供有效的唯一提交标识。")
        path = self.directory / (digest + ".json")
        identity = self._self()
        with self._locked(path):
            manifest = self._read(path)
            if manifest is None:
                legacy = self._legacy(tag, date)
                if legacy and (not force or legacy["owner_active"] is None):
                    return {"ok": True, "created": False, "started": False, "reused": True,
                            "task_id": None, "task": legacy, "status": legacy,
                            "hint": "仅有历史记录，完整性未确认；没有执行模型。"}, None
                manifest = {"schema": SCHEMA, "kind": "portrait_backfill",
                            "resource": dict(resource, digest=digest), "runs": [], "requests": {}}
            self._reconcile(manifest)
            mapped = manifest["requests"].get(request_id)
            if mapped:
                run = next(row for row in manifest["runs"] if row["task_id"] == mapped)
                self._write(path, manifest)
                return self._response(run, tag=tag), None
            occupied = next((row for row in reversed(manifest["runs"])
                             if row["status"] in ACTIVE or self._owner_state(row) != "dead"), None)
            run = occupied or (manifest["runs"][-1] if manifest["runs"] and not force else None)
            if run is not None:
                manifest["requests"][request_id] = run["task_id"]
                self._write(path, manifest)
                return self._response(run, tag=tag), None
            control = application_control(self.config_path, str(self.data_dir), purpose="review")
            fingerprint = self._credentials()
            reason = "" if control["allowed"] and fingerprint else control["reason"] if not control["allowed"] else "no_credentials"
            token, now = secrets.token_urlsafe(32), self._now()
            run = {"task_id": f"bf_{digest}_{uuid.uuid4().hex}", "tag": tag, "data_date": date,
                   "parameters": parameters, "status": "blocked" if reason else "running" if inline else "starting",
                   "reason": reason, "submitted_at": now, "started_at": now if inline and not reason else None,
                   "finished_at": now if reason else None, "config_path": self.config_path,
                   "control_epoch": list(control["epoch"]), "credential_fingerprint": fingerprint,
                   "counts": {"ok": 0, "fail": 0, "skipped": 0, "windows": 0, "calls": 0},
                   "input": None, "output": None, "processed": 0, "may_have_sent": False,
                   "dispatch_count": 0, "dispatch_settled": True,
                   "owner": identity if inline else None,
                   "lease": {"token": token, "launcher": identity, "candidate": identity if inline else None,
                             "expected": identity if inline else None, "ack": inline,
                             "revoked": bool(reason), "released": bool(reason), "deadline": now + 10}}
            manifest["runs"].append(run)
            manifest["requests"][request_id] = run["task_id"]
            self._write(path, manifest)
            return self._response(run, created=True, started=inline and not reason), (TaskRun(self, run, token) if not reason else None)

    def reserve_inline(self, tag, date, force=False, parameters=None, request_id=None):
        if request_id is None:
            request_id = uuid.uuid4().hex  # A new explicit CLI invocation is a new intent.
        return self._reserve(tag, date, force, parameters, request_id, True)

    def status(self, task_id=None, tag=None, date=None, request_id=None):
        selected_tag = None
        if tag is not None or date is not None:
            selected_tag, date, digest, _ = self._resource(tag, date)
            path = self.directory / (digest + ".json")
            if task_id and self._path(task_id) != path:
                raise TaskNotFound()
        elif task_id:
            path = self._path(task_id)
        else:
            raise TaskError("invalid_resource", "请提供主播与日期或任务标识。")
        if request_id is not None and (not isinstance(request_id, str) or not _REQUEST.fullmatch(request_id)):
            raise TaskError("invalid_request_id", "提交标识无效。")
        with self._locked(path):
            manifest = self._read(path)
            if manifest is None:
                if task_id:
                    raise TaskNotFound()
                return self._legacy(selected_tag, date) or {"task_id": None, "tag": selected_tag,
                    "data_date": date, "state": "not_requested", "status": "not_requested",
                    "counts": {}, "input": None, "output": None, "can_redo": False, "owner_active": False}
            if self._reconcile(manifest):
                self._write(path, manifest)
            wanted = task_id or manifest["requests"].get(request_id) if request_id else task_id
            if wanted:
                run = next((row for row in manifest["runs"] if row["task_id"] == wanted), None)
                if run is None:
                    raise TaskNotFound()
            elif request_id:
                return {"task_id": None, "tag": selected_tag or manifest["runs"][-1]["tag"],
                        "data_date": manifest["resource"]["data_date"], "state": "not_requested",
                        "status": "not_requested", "counts": {}, "input": None, "output": None,
                        "can_redo": False, "owner_active": False}
            elif manifest["runs"]:
                run = manifest["runs"][-1]
            else:
                raise TaskNotFound()
            return self._public(run, selected_tag)

    def _update(self, task_id, change):
        path = self._path(task_id)
        with self._locked(path):
            manifest = self._read(path)
            if manifest is None:
                raise TaskNotFound()
            run = next((row for row in manifest["runs"] if row["task_id"] == task_id), None)
            if run is None:
                raise TaskNotFound()
            change(run)
            self._write(path, manifest)
            return _clone(run)

    def claim(self, task_id, launch_token=None, timeout=10):
        token = launch_token or os.environ.get(LAUNCH_ENV, "")
        if not isinstance(token, str) or not token:
            raise TaskError("invalid_launch_token", "执行租约无效。")
        identity = self._self()
        until = time.monotonic() + max(0, min(float(timeout), 10))
        while True:
            def candidate(run):
                lease = run["lease"]
                if (lease.get("token") != token or lease.get("revoked") or lease.get("released")
                        or run["status"] != "starting" or self._now() > lease["deadline"]):
                    raise TaskError("task_blocked", "启动租约已失效，未执行模型。")
                if lease.get("candidate") and not _same_identity(lease["candidate"], identity):
                    raise TaskError("task_owned", "任务已由另一执行器持有。")
                lease["candidate"] = identity
                if lease.get("ack"):
                    if not _same_identity(lease.get("expected"), identity):
                        raise TaskError("task_owned", "子进程身份未确认。")
                    run.update(owner=identity, status="running", started_at=self._now())
            run = self._update(task_id, candidate)
            if run["status"] == "running":
                result = TaskRun(self, run, token)
                if not result.guard():
                    result.finish("blocked", result.stop_reason, dispatch_settled=True)
                    raise TaskError("task_blocked", "任务许可已变化，未执行模型。")
                return result
            if time.monotonic() >= until:
                self._abort_start(task_id, token, "startup_ack_timeout")
                raise TaskError("task_blocked", "父子进程交接未确认，未执行模型。")
            time.sleep(.02)

    def _abort_start(self, task_id, token, reason):
        def abort(run):
            if run["lease"].get("token") == token and run["status"] == "starting":
                run["lease"].update(revoked=True, released=True)
                run.update(status="unknown", reason=reason, finished_at=self._now())
        return self._update(task_id, abort)

    def submit(self, tag, date, force=False, request_id=None, parameters=None):
        response, pending = self._reserve(tag, date, force, parameters, request_id, False)
        if pending is None:
            return response
        task_id, token = pending.task_id, pending._token
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", **{LAUNCH_ENV: token})
        command = [self.python_exe, str(Path(__file__).resolve().parent / "portrait_backfill.py"),
                   "--task-id", task_id,
                   "--config", self.config_path, "--data-dir", str(self.data_dir)]
        try:
            if self._spawn_override is not None:
                process = self._spawn_override(command, env=env)
            else:
                with open(self.directory / (task_id + ".log"), "ab") as log:
                    process = subprocess.Popen(command, env=env, cwd=str(Path(__file__).resolve().parent.parent),
                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            launch_identity = self._spawn_identity(process)
            if launch_identity["state"] != "alive":
                raise TaskError("task_blocked", "执行进程身份不可确认。")
            until = time.monotonic() + 10
            while time.monotonic() < until:
                child = launch_identity
                acked = [False]
                def acknowledge(run):
                    lease = run["lease"]
                    if (lease.get("token") != token or lease.get("revoked")
                            or run["status"] != "starting" or self._now() > lease["deadline"]):
                        raise TaskError("task_blocked", "启动交接已失效。")
                    if child["state"] == "dead":
                        raise TaskError("task_blocked", "执行进程已退出。")
                    if child["state"] == "alive":
                        candidate = lease.get("candidate")
                        belongs = self._candidate_belongs(candidate, child)
                        lease["spawn_process"] = child
                        if belongs:
                            lease["expected"] = candidate
                            lease["ack"] = True
                            run["owner"] = candidate
                            acked[0] = True
                run = self._update(task_id, acknowledge)
                if acked[0]:
                    return self._response(run, created=True, started=True, tag=tag)
                time.sleep(.02)
            run = self._abort_start(task_id, token, "startup_ack_timeout")
        except Exception:
            try:
                run = self._abort_start(task_id, token, "startup_unknown")
            except TaskError:
                raise StateWriteError() from None
        return self._response(run, created=True, tag=tag)


class TaskRun:
    def __init__(self, store, record, token):
        self.store, self.task_id, self._token = store, record["task_id"], token
        self._record = _clone(record)
        self.stop_reason = ""

    @property
    def record(self):
        return _clone(self._record)

    @property
    def last_block_reason(self):
        return self.stop_reason

    def public(self):
        return self.store.status(task_id=self.task_id)

    def _owned(self, run):
        lease = run["lease"]
        if (lease.get("token") != self._token or not lease.get("ack")
                or lease.get("revoked") or not _same_identity(run.get("owner"), self.store._self())):
            raise TaskError("task_owned", "任务执行身份已失效。")

    def _permission(self, run):
        self._owned(run)
        if os.environ.get("DY_CUSTOMER_CONTEXT"):
            try:
                from scripts.local_context import load_customer_context, customer_stop_requested
                context = load_customer_context()
                if (Path(context["config"]).resolve() != Path(self.store.config_path).resolve()
                        or Path(context["data"]).resolve() != self.store.data_dir.resolve()):
                    return "application_context_changed"
                if customer_stop_requested(context, allow_parent=True, strict=True):
                    return "application_stopping"
            except (OSError, ValueError, TypeError):
                return "application_context_unavailable"
        if run["status"] != "running" or run["lease"].get("released"):
            return "task_blocked"
        control = application_control(self.store.config_path, str(self.store.data_dir), purpose="review")
        if not control["allowed"]:
            return control["reason"]
        if list(control["epoch"]) != run["control_epoch"]:
            return "control_changed"
        if self.store._credentials() != run["credential_fingerprint"]:
            return "credentials_changed"
        return ""

    def guard(self):
        if self.stop_reason:
            return False
        try:
            path = self.store._path(self.task_id)
            with self.store._locked(path):
                manifest = self.store._read(path)
                if manifest is None:
                    raise TaskNotFound()
                run = next((row for row in manifest["runs"] if row["task_id"] == self.task_id), None)
                if run is None:
                    raise TaskNotFound()
                reason = self._permission(run)
                if reason:
                    self.stop_reason = reason
                    if run["status"] in ACTIVE:
                        run.update(status="blocked", reason=reason)
                        self.store._write(path, manifest)
                self._record = _clone(run)
            return not self.stop_reason
        except TaskError as error:
            self.stop_reason = error.code
            return False

    def before_send(self, provider="", model=""):
        if not self.guard():
            return False
        try:
            def mark(run):
                reason = self._permission(run)
                if reason:
                    self.stop_reason = reason
                    raise TaskError("task_blocked")
                run.update(may_have_sent=True, dispatch_settled=False,
                           dispatch_count=run["dispatch_count"] + 1,
                           last_dispatch_at=self.store._now())
            self._record = self.store._update(self.task_id, mark)
            return True
        except TaskError as error:
            self.stop_reason = self.stop_reason or error.code
            return False

    def _fields(self, run, counts=None, input_snapshot=None, output_ref=None,
                dispatch_settled=None, processed=None, **values):
        if counts is not None:
            if not isinstance(counts, dict) or any(type(value) is not int or value < 0 for value in counts.values()):
                raise TaskError("invalid_progress", "任务计数无效。")
            run["counts"].update(counts)
        if input_snapshot is not None:
            run["input"] = _clone(input_snapshot)
        if output_ref is not None:
            run["output"] = _clone(output_ref)
        if processed is not None:
            run["processed"] = _clone(processed)
        for name in ("current_window_ts", "processed_ts_end"):
            if name in values:
                run[name] = _clone(values[name])
        if dispatch_settled is not None:
            if type(dispatch_settled) is not bool:
                raise TaskError("invalid_progress", "任务进度无效。")
            run["dispatch_settled"] = dispatch_settled

    def progress(self, **values):
        try:
            def progress(run):
                self._owned(run)
                if run.get("finished_at") is not None or run["lease"].get("released"):
                    raise TaskError("task_finished", "任务已经结束，不能改写历史进度。")
                self._fields(run, **values)
            self._record = self.store._update(self.task_id, progress)
            return self.record
        except TaskError as error:
            self.stop_reason = self.stop_reason or error.code
            raise

    def finish(self, state=None, reason="", **values):
        aliases = {"done": "succeeded", "success": "succeeded", "paused": "blocked"}
        state = aliases.get(state, state)
        if state not in TERMINAL:
            raise TaskError("invalid_state", "任务终态无效。")
        if reason and not re.fullmatch(r"[a-z_]{1,64}", reason):
            raise TaskError("invalid_reason", "任务结束原因无效。")
        try:
            def finish(run):
                self._owned(run)  # Finishing does not require current model permission.
                if run.get("finished_at") is not None:
                    return
                self._fields(run, **values)
                chosen = "unknown" if self.stop_reason == "task_state_unavailable" else state
                run.update(status=chosen, reason=reason or self.stop_reason or "completed",
                           finished_at=self.store._now())
                if chosen != "unknown" and run.get("dispatch_settled"):
                    run["lease"]["released"] = True
            self._record = self.store._update(self.task_id, finish)
            return self.store._public(self._record)
        except TaskError as error:
            self.stop_reason = self.stop_reason or error.code
            raise


BackfillTasks = TaskStore
