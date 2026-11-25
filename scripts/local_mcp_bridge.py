"""Read-only, identity-gated bridge to the one customer loopback application.

No app start/control, file ASR fallback, locks, registry or execution threads.
The only POST is in-memory local-session authentication. Identity/native file
calls count against the deadline but cannot be forcibly preempted by Python.
"""
import copy
import ctypes
from ctypes import wintypes
import hashlib
import http.client
import io
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import time
from urllib.parse import urlencode
import uuid

from backfill_tasks import process_identity
from local_process import listener_pid, same_identity
from scripts import audio_segments as audio

DEADLINE_SECONDS = 5.0
MAX_REQUESTS = 6
MAX_BODY_BYTES = 65536
MAX_HEADER_BYTES = 16384
MAX_WIRE_BYTES = MAX_BODY_BYTES + MAX_HEADER_BYTES
MAX_INSTANCE_BYTES = 65536
INSTANCE_TTL_SECONDS = 10.0
_ROOT = Path(__file__).resolve().parents[1]
_CONTEXT_FIELDS = {"schema", "instance_id", "root", "data", "config", "roster", "dist", "python", "profile", "port", "cdp_port"}
_PATH_FIELDS = {"root", "data", "config", "roster", "dist", "python", "profile"}
_APP_STATES = {"starting", "waiting", "running", "degraded", "stopping", "stopped", "error"}
_ASR_STATES = {"not_started", "starting", "identity_unknown", "admission_unknown", "snapshot_unconfirmed", "idle", "scanning",
               "busy", "processing", "backoff", "dependency_error", "stopping", "stopped", "draining", "storage_error", "failed", "restart_limited"}
_COUNTS = ("pending", "projection_pending", "retryable", "blocked", "complete")
_STORAGE_COUNTS = ("audio_files", "transcription_pending", "cleanup_pending", "cleanup_failed", "cleanup_blocked")
_VERSION = re.compile(r"v_[0-9a-f]{32}\Z")
_TOKEN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SAFE_CODES = {"online", "context_unavailable", "unsupported_platform", "instance_missing", "instance_invalid", "instance_stale",
               "identity_unknown", "identity_mismatch", "listener_unavailable", "runtime_changed", "authentication_unavailable",
               "http_unavailable", "response_invalid", "response_too_large", "deadline_exceeded", "request_limit", "invalid_lookup"}


class _BridgeError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _number(value):
    return value if _finite(value) and value > 0 else None


def _current_at(value, wall, ttl):
    return _number(value) is not None and _finite(wall) and 0 <= wall - value <= ttl


def _count(value, maximum=2**53 - 1):
    return value if type(value) is int and 0 <= value <= maximum else None


def _token(value):
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else ""


def _version(value):
    return value if isinstance(value, str) and _VERSION.fullmatch(value) else None


def _uuid(value):
    try:
        return isinstance(value, str) and len(value) == 36 and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _decode(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise _BridgeError("response_invalid")
            value[key] = item
        return value
    def invalid(_value):
        raise _BridgeError("response_invalid")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid)
        if not isinstance(value, dict):
            raise _BridgeError("response_invalid")
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise _BridgeError("response_invalid") from None


def _signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink


def _regular(info):
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and not getattr(info, "st_file_attributes", 0) & 0x400


class _Deadline:
    def __init__(self, clock):
        self.clock, self.end, self.requests = clock, clock() + DEADLINE_SECONDS, 0

    def remaining(self):
        value = self.end - self.clock()
        if value <= 0:
            raise _BridgeError("deadline_exceeded")
        return value


class _DeadlineRaw(io.RawIOBase):
    """Every socket recv uses time left, including HTTP status/header reads."""
    def __init__(self, sock, deadline):
        super().__init__()
        self.sock, self.deadline = sock, deadline
        self.received, self.header_done, self.tail = 0, False, b""

    def readable(self):
        return True

    def readinto(self, target):
        self.sock.settimeout(self.deadline.remaining())
        length = self.sock.recv_into(memoryview(target)[:min(len(target), MAX_WIRE_BYTES - self.received + 1)])
        raw = bytes(memoryview(target)[:length])
        previous = self.received
        self.received += length
        if self.received > MAX_WIRE_BYTES:
            raise _BridgeError("response_too_large")
        if not self.header_done:
            combined = self.tail + raw
            end = combined.find(b"\r\n\r\n")
            if end >= 0:
                if previous - len(self.tail) + end + 4 > MAX_HEADER_BYTES:
                    raise _BridgeError("response_too_large")
                self.header_done = True
            elif self.received > MAX_HEADER_BYTES:
                raise _BridgeError("response_too_large")
            self.tail = combined[-3:]
        self.deadline.remaining()
        return length


class _ResponseSocket:
    def __init__(self, sock, deadline):
        self.sock, self.deadline = sock, deadline

    def makefile(self, *_args, **_kwargs):
        return io.BufferedReader(_DeadlineRaw(self.sock, self.deadline))


class _FinalResponse(http.client.HTTPResponse):
    def _read_status(self):
        result = super()._read_status()
        if 100 <= result[1] < 200:
            raise _BridgeError("response_invalid")
        return result


def _current(value):
    value = value if isinstance(value, dict) else {}
    tag = value.get("tag")
    return {"tag": tag if isinstance(tag, str) and len(tag) <= 40 and not any(char in '/\\:*?"<>|' or ord(char) < 32 for char in tag) else "",
            "date": value.get("date") if isinstance(value.get("date"), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value["date"]) else "",
            "segment": value.get("segment") if isinstance(value.get("segment"), str) and re.fullmatch(r"seg_[0-9]{4,}(?:\.flv)?", value["segment"]) and len(value["segment"]) <= 64 else "",
            "phase": _token(value.get("phase")), "attempt": _count(value.get("attempt"), 1000000)}


def _storage(raw, parent_current, wall):
    if not isinstance(raw, dict) or type(raw.get("schema")) is not int or raw["schema"] != 1:
        return None
    scan, disk = raw.get("scan"), raw.get("disk")
    if not isinstance(scan, dict) or not isinstance(disk, dict):
        raise _BridgeError("response_invalid")
    snapshot_at, updated, sampled = _number(raw.get("snapshot_at")), _number(scan.get("updated_at")), _number(disk.get("sampled_at"))
    verified = (parent_current and raw.get("verified") is True and raw.get("stale") is False
                and _current_at(snapshot_at, wall, 15.0) and updated == snapshot_at)
    disk_current = (parent_current and disk.get("verified") is True and disk.get("stale") is False
                    and _current_at(sampled, wall, 60.0))
    free, total = _count(disk.get("free_bytes")), _count(disk.get("total_bytes"))
    if free is None or total is None or total == 0 or free > total:
        free = total = None
        disk_current = False
    if _token(disk.get("error")) or disk.get("threshold_bytes") != 1073741824:
        disk_current = False
    values = {key: _count(raw.get(key), 1000000000) for key in _STORAGE_COUNTS}
    last = raw.get("last_cleanup")
    last = ({"at": _number(last.get("at")), **{key: value for key, value in _current(last).items() if key in {"tag", "date", "segment"}},
             "state": last["state"], "reason": _token(last.get("reason"))}
            if isinstance(last, dict) and last.get("state") in ("deleted", "pending", "kept", "busy") else None)
    return {"schema": 1, "verified": bool(verified), "stale": not bool(verified), "snapshot_at": snapshot_at,
            "audio_bytes": _count(raw.get("audio_bytes")), **values,
            "scan": {"partial": scan.get("partial") is not False or any(value is None for value in values.values()) or _count(raw.get("audio_bytes")) is None,
                     "limited": scan.get("limited") is True,
                     "checked": _count(scan.get("checked"), 1000000000), "started_at": _number(scan.get("started_at")),
                     "updated_at": _number(scan.get("updated_at")), "last_complete_at": _number(scan.get("last_complete_at")),
                     "approximate": True, "counts_scope": "scan_round"},
            "disk": {"verified": bool(disk_current), "stale": not bool(disk_current), "free_bytes": free, "total_bytes": total,
                     "threshold_bytes": 1073741824, "low_space": disk.get("low_space") if disk_current and type(disk.get("low_space")) is bool else None,
                     "sampled_at": sampled, "error": _token(disk.get("error"))}, "last_cleanup": last}


def _asr(value, wall):
    raw = value.get("status")
    if value.get("ok") is not True or not isinstance(raw, dict) or raw.get("state") not in _ASR_STATES:
        raise _BridgeError("response_invalid")
    if any(type(raw.get(key)) is not bool for key in ("resource_held", "verified", "stale", "no_progress")):
        raise _BridgeError("response_invalid")
    generation = raw.get("generation")
    if not isinstance(generation, str) or (generation and not re.fullmatch(r"[0-9a-f]{24}", generation)):
        raise _BridgeError("response_invalid")
    counts, scan = raw.get("counts"), raw.get("scan")
    if not isinstance(counts, dict) or not isinstance(scan, dict):
        raise _BridgeError("response_invalid")
    snapshot_at = _number(raw.get("snapshot_at"))
    current = raw["verified"] and not raw["stale"]
    # Completion can only downgrade the manager's claim, never promote a stale
    # or rejected generation/sequence or create another sequence watermark.
    current = current and _current_at(snapshot_at, wall, 15.0)
    return {"state": raw["state"], "resource_held": raw["resource_held"], "verified": bool(current), "stale": not bool(current),
            "no_progress": raw["no_progress"], "snapshot_at": snapshot_at, "sequence": _count(raw.get("sequence")), "generation": generation,
            "counts": {key: _count(counts.get(key), 1000000000) for key in _COUNTS},
            "scan": {"partial": scan.get("partial") is not False, "limited": scan.get("limited") is True,
                     "checked": _count(scan.get("checked"), 1000000000), "last_complete_at": _number(scan.get("last_complete_at")),
                     "counts_scope": "recent_window", "sample_limit": 256},
            "current": _current(raw.get("current")), "error": _token(raw.get("error")), "next_condition": _token(raw.get("next_condition")),
            "next_retry_at": _number(raw.get("next_retry_at")), "last_progress_at": _number(raw.get("last_progress_at")),
            "last_completed_at": _number(raw.get("last_completed_at")), "storage": _storage(raw.get("storage"), current, wall)}


def _material_http(raw):
    if (raw.get("ok") is not True or not _uuid(raw.get("service_id")) or raw.get("active_id") is not None and not _uuid(raw["active_id"])
            or raw.get("lookup") not in ("latest", "found", "unknown", "service_changed") or type(raw.get("stopping")) is not bool):
        raise _BridgeError("response_invalid")
    material, operation = raw.get("material"), raw.get("operation")
    if not isinstance(material, dict) or material.get("state") not in ("ready", "not_ready", "unavailable", "corrupt"):
        raise _BridgeError("response_invalid")
    version = _version(material.get("version"))
    if material["state"] == "ready" and version is None:
        raise _BridgeError("response_invalid")
    safe_operation = None
    if operation is not None:
        if (not isinstance(operation, dict) or operation.get("service_id") != raw["service_id"] or not _uuid(operation.get("request_id"))
                or operation.get("state") not in ("running", "published", "failed", "unconfirmed", "busy")):
            raise _BridgeError("response_invalid")
        if operation["state"] == "published" and _version(operation.get("version")) is None:
            raise _BridgeError("response_invalid")
        safe_operation = {"service_id": operation["service_id"], "request_id": operation["request_id"], "state": operation["state"],
                          "version": None if operation["state"] in ("unconfirmed", "busy", "running") else _version(operation.get("version")),
                          "previous": _version(operation.get("previous")), "code": _token(operation.get("code")) or None,
                          "warning": _token(operation.get("warning")) or None, "started_at": _number(operation.get("started_at")),
                          "finished_at": _number(operation.get("finished_at"))}
    if (raw["lookup"] in ("unknown", "service_changed") and safe_operation is not None
            or raw["lookup"] == "found" and safe_operation is None):
        raise _BridgeError("response_invalid")
    diagnostic = material.get("refresh")
    diagnostic = ({"state": _token(diagnostic.get("state")), "version": _version(diagnostic.get("version")),
                   "updated_at": _number(diagnostic.get("updated_at")), "last_success_at": _number(diagnostic.get("last_success_at")),
                   "error": _token(diagnostic.get("error")) or None, "snapshot_stale": diagnostic.get("snapshot_stale") is not False}
                  if isinstance(diagnostic, dict) else None)
    return {"ok": True, "service_id": raw["service_id"], "active_id": raw.get("active_id"), "operation": safe_operation,
            "lookup": raw["lookup"], "stopping": raw["stopping"],
            "retention": {"scope": "http_instance", "terminal_limit": 32, "persistent": False, "ttl_seconds": None},
            "material": {"state": material["state"], "code": _token(material.get("code")) or None, "version": version,
                         "generated_at": _number(material.get("generated_at")), "verified_at": _number(material.get("verified_at")),
                         "refresh": diagnostic}}


class RuntimeBridge:
    def __init__(self, context, *, identity_reader=None, listener_reader=None, monotonic=time.monotonic, wall_time=time.time):
        self._clock, self._wall = monotonic, wall_time
        self._identity, self._listener = identity_reader or process_identity, listener_reader or listener_pid
        self._cookie = self._cookie_generation = None
        self.context, self.scope, self._valid = None, None, False
        try:
            if (not isinstance(context, dict) or set(context) != _CONTEXT_FIELDS or type(context.get("schema")) is not int or context["schema"] != 1
                    or not isinstance(context.get("instance_id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", context["instance_id"])
                    or any(not isinstance(context[key], str) or not Path(context[key]).is_absolute() for key in _PATH_FIELDS)
                    or Path(context["root"]).resolve() != _ROOT.resolve() or type(context.get("port")) is not int or not 1 <= context["port"] <= 65535
                    or type(context.get("cdp_port")) is not int or not 1 <= context["cdp_port"] <= 65535 or context["cdp_port"] == context["port"]
                    or str(context["data"]).startswith(("\\\\", "//"))):
                return
            self.context = copy.deepcopy(context)
            self.data = Path(context["data"])
            self.path = self.data / ".local_app" / "instance.json"
            self.scope = audio.data_scope(self.data)
            namespace = context["instance_id"] + ":" + str(context["port"])
            self._cookie_name = "dy_local_" + hashlib.sha256(namespace.encode()).hexdigest()[:16]
            self._valid = True
        except Exception:
            pass

    def _scope(self, deadline):
        deadline.remaining()
        if audio.data_scope(self.data) != self.scope:
            raise _BridgeError("runtime_changed")
        deadline.remaining()

    def _read_file(self, deadline):
        if os.name != "nt":
            raise _BridgeError("unsupported_platform")
        import msvcrt
        audio._no_links(self.path)
        before = self.path.lstat()
        if not _regular(before) or before.st_size > MAX_INSTANCE_BYTES:
            raise _BridgeError("instance_invalid")
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        api.CreateFileW.restype = wintypes.HANDLE
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        api.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        name = str(self.path.absolute())
        handle = api.CreateFileW("\\\\?\\" + name, 0x80000000, 0x7, None, 3, 0x200000, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            raise _BridgeError("instance_invalid")
        fd = None
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            handle = None
            initial = os.fstat(fd)
            if not _regular(initial) or _signature(initial) != _signature(before):
                raise _BridgeError("runtime_changed")
            raw = os.read(fd, MAX_INSTANCE_BYTES + 1)
            deadline.remaining()
            final = os.fstat(fd)
            current = self.path.lstat()
            native_handle = msvcrt.get_osfhandle(fd)
            buffer = ctypes.create_unicode_buffer(32768)
            length = api.GetFinalPathNameByHandleW(native_handle, buffer, len(buffer), 0)
            actual = buffer.value[4:] if buffer.value.startswith("\\\\?\\") else buffer.value
            if (not 0 < length < len(buffer) or os.path.normcase(actual) != os.path.normcase(str(self.path.resolve(strict=True)))
                    or not _regular(final) or not _regular(current) or _signature(initial) != _signature(final)
                    or _signature(final) != _signature(current) or len(raw) != final.st_size or len(raw) > MAX_INSTANCE_BYTES):
                raise _BridgeError("runtime_changed")
            return raw
        finally:
            if fd is not None:
                os.close(fd)
            elif handle is not None:
                api.CloseHandle(handle)

    def _record(self, deadline):
        self._scope(deadline)
        value = None
        for attempt in range(2):
            try:
                value = _decode(self._read_file(deadline))
                break
            except _BridgeError as error:
                if error.code != "runtime_changed" or attempt:
                    raise
        self._scope(deadline)
        wall = self._wall()
        owner = value.get("owner")
        nonce = value.get("launch_nonce")
        if (type(value.get("schema")) is not int or value["schema"] != 1 or value.get("instance_id") != self.context["instance_id"]
                or value.get("context") != self.context or value.get("state") not in _APP_STATES or type(value.get("board_ready")) is not bool
                or not isinstance(nonce, str) or (nonce and not re.fullmatch(r"(?:[0-9a-f]{32}|[0-9a-f]{48})", nonce))
                or not isinstance(owner, dict) or set(owner) != {"state", "pid", "created"}
                or owner.get("state") != "alive" or type(owner.get("pid")) is not int or owner["pid"] <= 0 or _number(owner.get("created")) is None):
            raise _BridgeError("instance_invalid")
        if not _finite(wall) or _number(value.get("updated_at")) is None or not 0 <= wall - value["updated_at"] <= INSTANCE_TTL_SECONDS:
            raise _BridgeError("instance_stale")
        return value

    @staticmethod
    def _generation(record):
        return (record["instance_id"], record["owner"]["pid"], record["owner"]["created"], record["launch_nonce"],
                json.dumps(record["context"], ensure_ascii=False, sort_keys=True, separators=(",", ":")))

    def _verify(self, deadline, generation=None):
        record = self._record(deadline)
        if generation is not None and self._generation(record) != generation:
            raise _BridgeError("runtime_changed")
        fact = self._identity(record["owner"]["pid"])
        deadline.remaining()
        if not same_identity(record["owner"], fact):
            raise _BridgeError("identity_unknown" if fact.get("state") == "unknown" else "identity_mismatch")
        endpoint = self._listener(self.context["port"])
        deadline.remaining()
        if endpoint.get("state") != "listening" or endpoint.get("pids") != [record["owner"]["pid"]]:
            raise _BridgeError("listener_unavailable")
        if not record["board_ready"]:
            raise _BridgeError("http_unavailable")
        return record

    def _request(self, deadline, method, path, *, cookie=False):
        if deadline.requests >= MAX_REQUESTS:
            raise _BridgeError("request_limit")
        if not ((method == "GET" and (path in ("/api/health", "/api/asr", "/api/materials/status")
                or re.fullmatch(r"/api/materials/status\?request_id=[0-9a-f-]{36}&service_id=[0-9a-f-]{36}", path)))
                or method == "POST" and path == "/api/local/session"):
            raise _BridgeError("http_unavailable")
        deadline.requests += 1
        sock, response = socket.socket(socket.AF_INET, socket.SOCK_STREAM), None
        try:
            sock.settimeout(deadline.remaining())
            sock.connect(("127.0.0.1", self.context["port"]))
            headers = [f"{method} {path} HTTP/1.1", "Host: 127.0.0.1:" + str(self.context["port"]), "Connection: close", "Accept: application/json"]
            if cookie:
                headers.append("Cookie: " + self._cookie)
            body = b"{}" if method == "POST" else b""
            if body:
                headers.extend(("Content-Type: application/json", "Content-Length: 2"))
            sock.settimeout(deadline.remaining())
            sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body)
            response = _FinalResponse(_ResponseSocket(sock, deadline), method=method)
            response.begin()
            deadline.remaining()
            lengths = response.headers.get_all("Content-Length", [])
            if (len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,8}", lengths[0]) or response.headers.get_all("Transfer-Encoding", [])):
                raise _BridgeError("response_invalid")
            length = int(lengths[0])
            if length > MAX_BODY_BYTES:
                raise _BridgeError("response_too_large")
            raw = bytearray()
            while len(raw) < length:
                deadline.remaining()
                chunk = response.read1(min(8192, length - len(raw)))
                if not chunk:
                    raise _BridgeError("response_invalid")
                raw.extend(chunk)
            deadline.remaining()
            return response.status, bytes(raw), response.headers
        finally:
            if response is not None:
                response.close()
            sock.close()

    def _bootstrap(self, deadline, generation):
        self._verify(deadline, generation)
        code, raw, headers = self._request(deadline, "POST", "/api/local/session")
        self._verify(deadline, generation)
        if code != 200 or _decode(raw).get("mode") != "local":
            raise _BridgeError("authentication_unavailable")
        values = headers.get_all("Set-Cookie", [])
        if len(values) != 1:
            raise _BridgeError("authentication_unavailable")
        match = re.fullmatch(re.escape(self._cookie_name) + r"=([A-Za-z0-9_-]{32,128});.*", values[0])
        if not match:
            raise _BridgeError("authentication_unavailable")
        self._cookie = self._cookie_name + "=" + match[1]
        self._cookie_generation = generation

    def _read_target(self, deadline, generation, path):
        if self._cookie_generation != generation or self._cookie is None:
            self._cookie = self._cookie_generation = None
            self._bootstrap(deadline, generation)
        for attempt in range(2):
            self._verify(deadline, generation)
            code, raw, _headers = self._request(deadline, "GET", path, cookie=True)
            self._verify(deadline, generation)
            if code == 401 and not attempt:
                self._cookie = self._cookie_generation = None
                self._bootstrap(deadline, generation)
                continue
            if code != 200:
                raise _BridgeError("authentication_unavailable" if code in (401, 403) else "http_unavailable")
            return _decode(raw)
        raise _BridgeError("authentication_unavailable")

    def _runtime(self, code, observed=None):
        return {"state": "online" if code == "online" else "unknown", "verified": code == "online",
                "code": code if code in _SAFE_CODES else "http_unavailable", "observed_at": _number(observed)}

    def _call(self, path, kind, lookup=None):
        unknown = {"schema": 1, "runtime": self._runtime("context_unavailable" if not self._valid else "http_unavailable")}
        unknown.update({"application": None, "asr": None} if kind == "health" else {"http": None})
        if kind == "health":
            unknown["coverage"] = {"asr_counts": "recent_window", "sample_limit": 256, "storage_counts": "scan_round",
                                   "platform_login": "not_checked", "collection_completeness": "unknown", "raw_data_checked": False}
        if not self._valid:
            return unknown
        try:
            deadline = _Deadline(self._clock)
            record = self._verify(deadline)
            generation = self._generation(record)
            code, raw, _headers = self._request(deadline, "GET", "/api/health")
            health = _decode(raw)
            self._verify(deadline, generation)
            if code != 200 or health.get("ok") is not True or health.get("instance_id") != self.context["instance_id"] or health.get("owner") != record["owner"]:
                raise _BridgeError("identity_mismatch")
            value = self._read_target(deadline, generation, path)
            record = self._verify(deadline, generation)
            wall = self._wall()
            result = _asr(value, wall) if kind == "health" else _material_http(value)
            if kind == "status":
                if lookup is None and result["lookup"] != "latest":
                    raise _BridgeError("response_invalid")
                if lookup is not None:
                    request_id, service_id = lookup
                    if result["service_id"] != service_id:
                        if result["lookup"] != "service_changed" or result["operation"] is not None:
                            raise _BridgeError("response_invalid")
                    elif (result["lookup"] not in ("found", "unknown")
                          or result["operation"] is not None and result["operation"]["request_id"] != request_id):
                        raise _BridgeError("response_invalid")
            deadline.remaining()
            unknown["runtime"] = self._runtime("online", wall)
            if kind == "health":
                unknown.update(application={"state": record["state"], "board_ready": record["board_ready"]}, asr=result)
            else:
                unknown["http"] = result
            return unknown
        except Exception as error:
            self._cookie = self._cookie_generation = None
            code = error.code if isinstance(error, _BridgeError) else "instance_missing" if isinstance(error, FileNotFoundError) else "deadline_exceeded" if isinstance(error, TimeoutError) else "http_unavailable"
            unknown["runtime"] = self._runtime(code)
            return unknown

    def health(self):
        return self._call("/api/asr", "health")

    def status(self, request_id=None, service_id=None):
        if (request_id is None) != (service_id is None) or request_id is not None and (not _uuid(request_id) or not _uuid(service_id)):
            return {"schema": 1, "runtime": self._runtime("invalid_lookup"), "http": None}
        path = "/api/materials/status"
        if request_id is not None:
            path += "?" + urlencode({"request_id": request_id, "service_id": service_id})
        return self._call(path, "status", (request_id, service_id) if request_id is not None else None)
