"""Explicit customer role paths and ownership receipts, without global services."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time

_ROOT = Path(__file__).resolve().parents[1]
_PATHS = {"root", "config", "data", "roster", "dist", "python", "profile"}
_FIELDS = _PATHS | {"schema", "port", "cdp_port", "instance_id"}


def _read_json(path, limit):
    with Path(path).open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("customer context or receipt exceeds limit")
    return json.loads(raw)


def load_customer_context(path=None):
    from scripts.local_installation import bootstrap_installed, validate_installed_context
    installed = bootstrap_installed()
    value = os.environ.get("DY_CUSTOMER_CONTEXT") if path is None else path
    if not value:
        if installed is not None:
            raise ValueError("installed customer context required")
        return None
    source = Path(value)
    if not source.is_absolute():
        raise ValueError("customer context path must be absolute")
    raw = _read_json(source, 65536)
    if (not isinstance(raw, dict) or set(raw) != _FIELDS
            or type(raw.get("schema")) is not int or raw["schema"] != 1):
        raise ValueError("invalid customer context fields")
    for name in _PATHS:
        value = raw[name]
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise ValueError("customer paths must be explicit and absolute")
        raw[name] = str(Path(value).resolve())
    if Path(raw["root"]) != _ROOT.resolve():
        raise ValueError("customer application root does not own this code")
    if (not isinstance(raw["instance_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", raw["instance_id"])):
        raise ValueError("invalid customer instance identity")
    if any(type(raw[name]) is not int or not 1 <= raw[name] <= 65535
           for name in ("port", "cdp_port")) or raw["port"] == raw["cdp_port"]:
        raise ValueError("invalid customer ports")
    validate_installed_context(raw)
    return raw


def activate_customer_environment(context):
    from scripts.local_installation import bootstrap_installed, validate_installed_context
    installed = bootstrap_installed()
    if not context:
        return
    validate_installed_context(context)
    os.environ.update(CDP_PORT=str(context["cdp_port"]),
        WATCH_DEBUG_PORT=str(context["cdp_port"]), WATCH_BROWSER_PROFILE=context["profile"],
        DOUYIN_OUTPUT_DIR=context["data"], WB_RECORD_PY=context["python"],
        HF_HOME=str(installed["cache"] / "hf") if installed else str(Path(context["data"]) / "cache" / "hf"))


def role_receipt_path(context, kind, tag):
    validate_role_tag(tag)
    role = kind + ":" + tag
    return Path(context["data"]) / ".local_app" / "roles" / (hashlib.sha256(role.encode("utf-8")).hexdigest() + ".json")


def validate_role_tag(tag):
    if (not isinstance(tag, str) or not tag or len(tag) > 40
            or any(c in '/\\:*?"<>|' or ord(c) < 32 for c in tag)):
        raise ValueError("invalid customer role tag")
    return tag


def publish_role_ready(context, kind, tag):
    if not context:
        return None
    from scripts.backfill_tasks import process_identity
    identity = process_identity(os.getpid())
    nonce = os.environ.get("DY_CUSTOMER_ROLE_NONCE", "")
    if identity.get("state") != "alive" or not nonce or len(nonce) > 128:
        raise RuntimeError("customer role ownership unavailable")
    path = role_receipt_path(context, kind, tag)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {"schema": 1, "instance_id": context["instance_id"], "role": kind + ":" + tag,
        "nonce": nonce, "pid": identity["pid"], "created": identity["created"], "updated_at": time.time()}
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return identity


def customer_stop_requested(context, identity=None, *, allow_parent=False, strict=False):
    if not context:
        return False
    path = os.environ.get("DY_CUSTOMER_STOP")
    if not path:
        return False
    try:
        if identity is None:
            from scripts.backfill_tasks import process_identity
            identity = process_identity(os.getpid())
        value = _read_json(path, 4096)
        valid = (identity.get("state") == "alive" and type(value.get("schema")) is int
            and value["schema"] == 1 and value.get("action") == "stop"
            and value.get("instance_id") == context["instance_id"]
            and type(value.get("pid")) is int and value["pid"] > 0
            and type(value.get("created")) in (int, float)
            and math.isfinite(value["created"]))
        if not valid:
            if strict and (not isinstance(value.get("instance_id"), str)
                           or value.get("instance_id") == context["instance_id"]):
                raise ValueError("customer stop state unavailable")
            return False
        if value["pid"] == identity["pid"] and value["created"] == identity["created"]:
            return True
        if not allow_parent:
            return False
        from scripts.backfill_tasks import process_identity, process_parent_pid
        owner = process_identity(value["pid"])
        if strict and owner.get("state") == "unknown":
            raise ValueError("customer stop owner unavailable")
        if (owner.get("state") != "alive" or owner.get("created") != value["created"]):
            return False
        child = identity
        for _ in range(4):
            parent_pid = process_parent_pid(child["pid"])
            if type(parent_pid) is not int or parent_pid <= 0:
                if strict:
                    raise ValueError("customer stop ancestry unavailable")
                return False
            parent = process_identity(parent_pid)
            if strict and parent.get("state") == "unknown":
                raise ValueError("customer stop ancestry unavailable")
            if parent.get("state") != "alive" or parent["created"] > child["created"]:
                return False
            if parent_pid == owner["pid"]:
                return parent["created"] == owner["created"] and process_identity(owner["pid"]) == owner
            child = parent
        return False
    except FileNotFoundError:
        return False
    except (OSError, ValueError, TypeError, AttributeError):
        if strict:
            raise ValueError("customer stop state unavailable") from None
        return False


def browser_owner_valid(context, identity_reader=None, listener_reader=None):
    if not context:
        return False
    try:
        if identity_reader is None:
            from scripts.backfill_tasks import process_identity
            identity_reader = process_identity
        if listener_reader is None:
            from scripts.local_process import listener_pid
            listener_reader = listener_pid
        value = _read_json(Path(context["data"]) / ".local_app" / "browser-owner.json", 16384)
        if (type(value.get("schema")) is not int or value["schema"] != 1
                or value.get("instance_id") != context["instance_id"]
                or type(value.get("pid")) is not int or value["pid"] <= 0
                or type(value.get("created")) not in (int, float) or not math.isfinite(value["created"])
                or type(value.get("port")) is not int or value["port"] != context["cdp_port"]
                or not isinstance(value.get("profile"), str)
                or not Path(value["profile"]).is_absolute()
                or Path(value["profile"]).resolve() != Path(context["profile"]).resolve()):
            return False
        current = identity_reader(value["pid"])
        if (current.get("state") != "alive" or current.get("pid") != value["pid"]
                or current.get("created") != value["created"]):
            return False
        listener = listener_reader(context["cdp_port"])
        return (listener.get("state") == "listening" and listener.get("pids") == [value["pid"]]
                and identity_reader(value["pid"]) == current)
    except (OSError, ValueError, TypeError, AttributeError):
        return False
