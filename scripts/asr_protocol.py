"""Small file protocol for the one customer ASR lane, never a task ledger."""
from __future__ import annotations

import json
import hashlib
import math
import os
from pathlib import Path
import stat
import uuid

from scripts import audio_segments as audio
from scripts.backfill_tasks import process_identity, process_parent_pid
from scripts.local_context import role_receipt_path
from scripts.local_process import same_identity

ROLE = "asr:global"
MAX_RECORD = 65536


class ProtocolError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def valid_boot_identity(value):
    return (isinstance(value, dict) and set(value) == {"schema", "kind", "installation", "counter"}
            and type(value.get("schema")) is int and value["schema"] == 1
            and value.get("kind") == "windows_boot"
            and isinstance(value.get("installation"), str) and len(value["installation"]) == 64
            and all(character in "0123456789abcdef" for character in value["installation"])
            and type(value.get("counter")) is int and 0 <= value["counter"] <= 0xffffffff)


def boot_advanced(previous, current):
    """Only a strictly later boot on the same Windows installation proves exit."""
    return (valid_boot_identity(previous) and valid_boot_identity(current)
            and previous["installation"] == current["installation"]
            and current["counter"] > previous["counter"])


def boot_identity():
    """Read only the documented Windows boot counter; never infer it from time.

    Microsoft documents BootId as incrementing on every successful boot:
    https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/oem-hvci-enablement
    MachineGuid is hashed locally so counters from different installations
    cannot be compared. Missing/unsupported evidence has no time-based fallback.
    """
    try:
        import winreg
        access = winreg.KEY_QUERY_VALUE | winreg.KEY_WOW64_64KEY
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters",
                0, access) as key:
            counter, kind = winreg.QueryValueEx(key, "BootId")
        if kind != winreg.REG_DWORD or type(counter) is not int or not 0 <= counter <= 0xffffffff:
            return None
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0, access) as key:
            machine, kind = winreg.QueryValueEx(key, "MachineGuid")
        if kind != winreg.REG_SZ or not isinstance(machine, str):
            return None
        machine = uuid.UUID(machine)
        if not machine.int:
            return None
        installation = hashlib.sha256(str(machine).encode("ascii")).hexdigest()
        return {"schema": 1, "kind": "windows_boot", "installation": installation, "counter": counter}
    except (ImportError, OSError, ValueError, AttributeError, TypeError):
        return None


def paths(context):
    directory = Path(context["data"]) / ".local_app" / "asr"
    value = {key: directory / (key + ".json")
             for key in ("admission", "stop", "status", "control", "retry", "release")}
    value.update(directory=directory, ready=Path(role_receipt_path(context, "asr", "global")))
    return value


def ensure(context, expected_scope=None):
    scope = audio.data_scope(context["data"])
    if expected_scope is not None and scope != expected_scope:
        raise ProtocolError("data_scope_changed")
    for path in (Path(context["data"]) / ".local_app", paths(context)["directory"], paths(context)["ready"].parent):
        if audio.data_scope(context["data"]) != scope:
            raise ProtocolError("data_scope_changed")
        audio._no_links(path.parent)
        path.mkdir(exist_ok=True)
        audio._no_links(path)
        if not stat.S_ISDIR(path.lstat().st_mode):
            raise ProtocolError("runtime_path_unavailable")
    return scope


def read_record(path, limit=MAX_RECORD):
    try:
        path = Path(path)
        info = audio._regular(path)
        if info.st_size > limit:
            raise ProtocolError("record_too_large")
        signature = audio._signature(info)
        with path.open("rb") as stream:
            if audio._signature(os.fstat(stream.fileno())) != signature:
                raise ProtocolError("record_unavailable")
            raw = stream.read(limit + 1)
            if len(raw) > limit or audio._signature(os.fstat(stream.fileno())) != signature:
                raise ProtocolError("record_unavailable")
        if audio._signature(audio._regular(path)) != signature:
            raise ProtocolError("record_unavailable")
        value = json.loads(raw, object_pairs_hook=_unique, parse_constant=_invalid)
        if not isinstance(value, dict):
            raise ProtocolError("record_unavailable")
        return value
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        if isinstance(error, ProtocolError):
            raise
        raise ProtocolError("record_unavailable") from error


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ProtocolError("record_unavailable")
        value[key] = item
    return value


def _invalid(_value):
    raise ProtocolError("record_unavailable")


def write_record(path, value):
    # audio.atomic_json adds the final newline and verifies its exact JSON value.
    if len(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")) + 1 > MAX_RECORD:
        raise ProtocolError("record_too_large")
    audio.atomic_json(path, value)


def identity_valid(value):
    return (isinstance(value, dict) and value.get("state") == "alive"
            and type(value.get("pid")) is int and value["pid"] > 0
            and type(value.get("created")) in (int, float)
            and math.isfinite(value["created"]) and value["created"] > 0)


def envelope(context, nonce, parent, worker, kind, scope=None):
    if not identity_valid(parent) or not identity_valid(worker) or not isinstance(nonce, str) or not nonce:
        raise ProtocolError("identity_unavailable")
    claim = lambda identity: {key: identity[key] for key in ("state", "pid", "created")}
    return {"schema": 1, "kind": kind, "instance_id": context["instance_id"], "role": ROLE,
            "nonce": nonce, "parent": claim(parent), "worker": claim(worker),
            "scope": audio.data_scope(context["data"]) if scope is None else dict(scope)}


def valid_envelope(value, kind):
    return (isinstance(value, dict) and type(value.get("schema")) is int and value["schema"] == 1
            and value.get("kind") == kind and value.get("role") == ROLE
            and isinstance(value.get("instance_id"), str) and bool(value["instance_id"])
            and isinstance(value.get("nonce"), str) and 0 < len(value["nonce"]) <= 128
            and isinstance(value.get("scope"), dict) and identity_valid(value.get("parent"))
            and identity_valid(value.get("worker")))


def matches(value, context, nonce, parent, worker, kind, scope=None):
    return (valid_envelope(value, kind)
            and value.get("instance_id") == context["instance_id"] and value.get("nonce") == nonce
            and value.get("scope") == (audio.data_scope(context["data"]) if scope is None else scope)
            and same_identity(value.get("parent"), parent) and same_identity(value.get("worker"), worker))


def parent_state(parent, worker):
    """Require the exact live parent within the current worker's actual ancestry."""
    if not identity_valid(parent) or not identity_valid(worker):
        return "unknown"
    actual_parent = process_identity(parent["pid"])
    if not isinstance(actual_parent, dict) or actual_parent.get("state") == "unknown":
        return "unknown"
    if actual_parent.get("state") != "dead" and not identity_valid(actual_parent):
        return "unknown"
    if not same_identity(parent, actual_parent):
        return "stop"
    if not same_identity(worker, process_identity(worker["pid"])):
        return "unknown"
    current = worker
    for _ in range(4):
        pid = process_parent_pid(current["pid"])
        if type(pid) is not int or pid <= 0:
            return "unknown"
        actual = process_identity(pid)
        if not identity_valid(actual) or actual["created"] > current["created"]:
            return "unknown"
        if pid == parent["pid"]:
            return "alive" if same_identity(actual, parent) else "stop"
        current = actual
    return "unknown"


def permission(context, nonce, parent, worker, scope=None):
    """No admission/unknown stop evidence permits new work; accepted work drains."""
    owner = parent_state(parent, worker)
    if owner != "alive":
        return owner
    try:
        scope = audio.data_scope(context["data"]) if scope is None else scope
        if audio.data_scope(context["data"]) != scope:
            return "unknown"
        app_stop = read_record(Path(context["data"]) / ".local_app/application-stop.json", 4096)
        if app_stop is not None:
            if (type(app_stop.get("schema")) is not int or app_stop["schema"] != 1
                    or app_stop.get("action") != "stop" or not isinstance(app_stop.get("instance_id"), str)
                    or not app_stop["instance_id"] or not identity_valid({"state": "alive",
                        "pid": app_stop.get("pid"), "created": app_stop.get("created")})):
                return "unknown"
            if (app_stop.get("instance_id") == context["instance_id"]
                    and app_stop.get("pid") == parent["pid"] and app_stop.get("created") == parent["created"]):
                return "stop"
        specific = read_record(paths(context)["stop"], 8192)
        if specific is not None:
            if not valid_envelope(specific, "asr_stop") or specific.get("action") != "stop":
                return "unknown"
            if matches(specific, context, nonce, parent, worker, "asr_stop", scope):
                return "stop"
            if specific["instance_id"] == context["instance_id"] and specific["nonce"] == nonce:
                return "unknown"
        admission = read_record(paths(context)["admission"])
        if matches(admission, context, nonce, parent, worker, "asr_admission", scope):
            return "admitted"
        if admission is None:
            return "waiting"
        if valid_envelope(admission, "asr_admission") and admission["nonce"] != nonce:
            return "waiting"
        return "unknown"
    except (OSError, ValueError):
        return "unknown"


def parent_from_environment():
    try:
        value = {"state": "alive", "pid": int(os.environ["DY_ASR_PARENT_PID"]),
                 "created": float(os.environ["DY_ASR_PARENT_CREATED"])}
        if not identity_valid(value):
            raise ValueError
        return value
    except (KeyError, ValueError, OverflowError) as error:
        raise ProtocolError("parent_identity_unavailable") from error


def scope_from_environment():
    try:
        raw = os.environ["DY_ASR_SCOPE"]
        if len(raw.encode("utf-8")) > MAX_RECORD:
            raise ValueError
        scope = json.loads(raw, object_pairs_hook=_unique, parse_constant=_invalid)
        if (not isinstance(scope, dict) or set(scope) != {"path", "dev", "ino"}
                or not isinstance(scope["path"], str) or not scope["path"]
                or type(scope["dev"]) is not int or scope["dev"] < 0
                or type(scope["ino"]) is not int or scope["ino"] <= 0):
            raise ValueError
        return scope
    except (KeyError, ValueError, UnicodeError) as error:
        raise ProtocolError("data_scope_unavailable") from error
