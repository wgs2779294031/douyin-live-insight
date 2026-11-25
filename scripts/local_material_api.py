"""Customer HTTP material adapter: bounded reads and synchronous single flight.

The caller's existing mutation/inflight guard owns the whole POST.  This module
does not create execution threads, initialize storage on GET, or persist HTTP
request receipts.  MaterialStore remains the only publisher/CLI lock owner.
"""
from collections import OrderedDict
import copy
from datetime import date
import json
import math
import re
import stat
import threading
import time
from urllib.parse import parse_qsl
import uuid

from scripts import material_common as common
from scripts.local_materials import MaterialStore


ROUTES = {"/api/materials/status", "/api/materials/groups", "/api/materials/query", "/api/materials/refresh"}
MAX_BODY_BYTES = 512
MAX_QUERY_BYTES = 8192
TERMINAL_LIMIT = 32
_VERSION = re.compile(r"v_[0-9a-f]{32}\Z")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
_STREAMS = {"watch", "room_meta", "stats", "danmu", "events", "speech"}
_SAFE_CODES = {
    "material_build_failed", "material_build_invalid", "material_busy", "material_gc_blocked",
    "material_gc_pending", "material_diagnostic_unavailable", "material_publish_unconfirmed",
    "material_capacity", "discovery_capacity", "data_scope_changed", "material_scope_changed",
    "material_path_unconfirmed", "material_changed", "material_file_unconfirmed", "material_unavailable",
    "material_line_too_large", "material_truncated_line", "material_record_invalid", "material_checksum_mismatch",
    "shared_projection_failed", "evidence_unconfirmed", "source_changed_during_build", "source_identity_mismatch",
    "source_line_too_large", "source_record_invalid", "source_time_unconfirmed", "material_refresh_unconfirmed",
    "material_owner_unconfirmed", "material_pointer_invalid", "material_version_invalid", "material_lock_unconfirmed",
    "material_gc_cancel_unconfirmed", "material_unknown_artifact", "materials_unavailable",
}
_CORRUPT = {"material_corrupt", "material_record_invalid", "material_checksum_mismatch", "material_pointer_invalid",
            "material_version_invalid", "material_owner_unconfirmed", "material_file_unconfirmed",
            "material_line_too_large", "material_truncated_line", "manifest_capacity"}
_CHANGED = {"material_changed", "material_version_changed", "material_scope_changed", "data_scope_changed"}


class MaterialAPIError(ValueError):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)


def error_body(code):
    messages = {
        "invalid_parameters": "材料请求参数无效。", "invalid_filter": "材料筛选条件无效。",
        "invalid_limit": "每页条数须为1至200的整数。", "invalid_cursor": "材料续页位置无效，请从头读取。",
        "invalid_refresh_request": "更新资料请求须仅包含有效请求UUID。",
        "cursor_expired": "材料版本已变化，请从头读取。", "material_changed": "材料已变化，请从头读取。",
        "material_version_changed": "材料版本已变化，请从头读取。",
        "material_scope_changed": "材料目录身份已变化，暂不可读取。", "data_scope_changed": "材料目录身份已变化，暂不可读取。",
        "materials_not_ready": "尚未建立可读取的材料，请手动更新资料。",
        "materials_unavailable": "本实例材料暂不可用。", "materials_corrupt": "材料凭据或内容未通过核验。",
        "materials_busy": "已有资料更新正在进行；本次新请求未登记或执行。",
        "materials_stopping": "应用正在停止，请只读查询已接纳更新的结果。",
        "material_method_not_allowed": "不支持此材料操作。", "material_route_not_found": "材料接口不存在。",
    }
    return {"ok": False, "code": code, "error": messages.get(code, "本实例材料暂不可用。")}


def _uuid(value):
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _stamp(value):
    try:
        return value if type(value) in (int, float) and math.isfinite(value) and value > 0 else None
    except OverflowError:
        return None


def _version(value):
    return value if isinstance(value, str) and _VERSION.fullmatch(value) else None


def _code(value, fallback):
    return value if isinstance(value, str) and value in _SAFE_CODES else fallback


def _query(raw, allowed, *, streams=False):
    try:
        if (not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_QUERY_BYTES
                or re.search(r"%(?![0-9a-fA-F]{2})", raw)):
            raise ValueError
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=16,
                          encoding="utf-8", errors="strict")
        values = {}
        for key, value in pairs:
            if key not in allowed or not value:
                raise ValueError
            if key == "stream" and streams:
                values.setdefault(key, []).append(value)
                if len(values[key]) > 6:
                    raise ValueError
            elif key in values:
                raise ValueError
            else:
                values[key] = value
        return values
    except (ValueError, UnicodeError):
        raise MaterialAPIError(400, "invalid_parameters") from None


def _filters(route, raw):
    allowed = {"view", "room", "date", "platform", "cursor", "limit"}
    if route == "/api/materials/query":
        allowed.update(("stream", "time_from", "time_until"))
    values = _query(raw, allowed, streams=route.endswith("/query"))
    if values.get("view", "canonical") not in {"canonical", "shared"}:
        raise MaterialAPIError(400, "invalid_filter")
    if "room" in values and (len(values["room"]) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in values["room"])):
        raise MaterialAPIError(400, "invalid_filter")
    if "platform" in values and values["platform"] not in {"douyin", "bilibili"}:
        raise MaterialAPIError(400, "invalid_filter")
    if "date" in values:
        try:
            valid = date.fromisoformat(values["date"]).isoformat() == values["date"]
        except ValueError:
            valid = False
        if not valid:
            raise MaterialAPIError(400, "invalid_filter")
    if "stream" in values and any(value not in _STREAMS for value in values["stream"]):
        raise MaterialAPIError(400, "invalid_filter")
    if "limit" in values:
        value = values["limit"]
        if not re.fullmatch(r"[1-9][0-9]{0,2}", value) or not 1 <= int(value) <= 200:
            raise MaterialAPIError(400, "invalid_limit")
        values["limit"] = int(value)
    if "cursor" in values and (len(values["cursor"]) > 2048 or not values["cursor"].isascii()
                               or not re.fullmatch(r"[A-Za-z0-9_-]+\.[0-9a-f]{64}", values["cursor"])):
        raise MaterialAPIError(400, "invalid_cursor")
    for key in ("time_from", "time_until"):
        if key in values:
            value = values[key]
            if len(value) > 64 or not _NUMBER.fullmatch(value):
                raise MaterialAPIError(400, "invalid_filter")
            number = float(value)
            if not math.isfinite(number):
                raise MaterialAPIError(400, "invalid_filter")
            values[key] = number
    if "time_from" in values and "time_until" in values and values["time_from"] >= values["time_until"]:
        raise MaterialAPIError(400, "invalid_filter")
    return values


def _request_id(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    try:
        if not isinstance(raw, bytes) or len(raw) > MAX_BODY_BYTES:
            raise ValueError
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                           parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict) or set(value) != {"request_id"} or not _uuid(value["request_id"]):
            raise ValueError
        return value["request_id"]
    except (ValueError, UnicodeError, RecursionError):
        raise MaterialAPIError(400, "invalid_refresh_request") from None


class MaterialAPI:
    def __init__(self, context, *, stopping=lambda: False, store_factory=None, wall_time=time.time):
        self.service_id = str(uuid.uuid4())
        self._lock, self._active, self._terminal = threading.Lock(), None, OrderedDict()
        self._stopping, self._wall = stopping, wall_time
        try:
            self.store = (store_factory or MaterialStore)(context)
        except Exception:
            self.store = None  # Materials fail locally; the rest of Board stays usable.

    def _now(self):
        try:
            return _stamp(self._wall())
        except Exception:
            return None

    def _not_ready(self):
        if self.store is None:
            raise MaterialAPIError(503, "materials_unavailable")
        try:
            info = self.store.root.lstat()
        except FileNotFoundError:
            self.store._scope()  # Stat-only DATA binding; no discovery or initialization.
            return True
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise MaterialAPIError(503, "materials_corrupt")
        # Only a checked owner plus absent current counts as not_ready.  These
        # create=False/private reads do not create owner, lock, or work dirs.
        with common.guard_directory(self.store.root):
            self.store._scope()
            try:
                (self.store.root / "owner.json").lstat()
            except FileNotFoundError:
                raise MaterialAPIError(503, "materials_corrupt") from None
            owner = self.store._owner(create=False)
            return self.store._pointer(owner, missing=True) is None

    @staticmethod
    def _failure(error):
        if isinstance(error, MaterialAPIError):
            return error.status, error.code
        reason = getattr(error, "reason", None)
        if not isinstance(reason, str):
            return 503, "materials_unavailable"
        if reason in {"invalid_filter", "invalid_limit", "invalid_cursor"}:
            return 400, reason
        if reason == "cursor_expired" or reason in _CHANGED:
            return 409, reason
        if reason in _CORRUPT:
            return 503, "materials_corrupt"
        if reason == "materials_not_ready":
            return 404, "materials_not_ready"
        return 503, "materials_unavailable"

    def _material(self):
        empty = {"version": None, "generated_at": None, "verified_at": None, "refresh": None}
        try:
            if self._not_ready():
                return dict(empty, state="not_ready", code="materials_not_ready")
            with self.store.snapshot() as snapshot:
                manifest, diagnostic = snapshot.manifest, snapshot.status
                version = _version(manifest.get("version"))
                if version is None:
                    raise MaterialAPIError(503, "materials_corrupt")
                refresh = {"state": diagnostic.get("state") if diagnostic.get("state") in {"success", "failed", "unconfirmed"} else "unconfirmed",
                           "version": _version(diagnostic.get("version")), "updated_at": _stamp(diagnostic.get("updated_at")),
                           "last_success_at": _stamp(diagnostic.get("last_success_at")),
                           "error": _code(diagnostic.get("error"), "material_refresh_unconfirmed") if diagnostic.get("error") else None,
                           "snapshot_stale": diagnostic.get("snapshot_stale") is not False}
                result = {"state": "ready", "code": None, "version": version,
                          "generated_at": _stamp(manifest.get("generated_at")), "verified_at": _stamp(manifest.get("verified_at")),
                          "refresh": refresh, "source_freshness": "snapshot_at_build_raw_not_rechecked"}
                snapshot.finish()
                return result
        except Exception as error:
            _, code = self._failure(error)
            state = "corrupt" if code == "materials_corrupt" else "not_ready" if code == "materials_not_ready" else "unavailable"
            return dict(empty, state=state, code=code)

    def _receipt(self, request_id=None, service_id=None):
        with self._lock:
            active_id = self._active["request_id"] if self._active else None
            if service_id is not None and service_id != self.service_id:
                return active_id, None, "service_changed"
            if request_id is None:
                result = self._active or (next(reversed(self._terminal.values())) if self._terminal else None)
                return active_id, copy.deepcopy(result), "latest"
            result = self._active if active_id == request_id else self._terminal.get(request_id)
            return active_id, copy.deepcopy(result), "found" if result else "unknown"

    def status(self, request_id=None, service_id=None):
        active, receipt, lookup = self._receipt(request_id, service_id)
        return {"ok": True, "service_id": self.service_id, "active_id": active, "operation": receipt, "lookup": lookup,
                "retention": {"scope": "http_instance", "terminal_limit": TERMINAL_LIMIT, "persistent": False, "ttl_seconds": None},
                "material": self._material(), "stopping": bool(self._stopping())}

    def get(self, route, raw_query):
        try:
            if route == "/api/materials/status":
                values = _query(raw_query, {"request_id", "service_id"})
                if values and (set(values) != {"request_id", "service_id"} or not all(_uuid(value) for value in values.values())):
                    raise MaterialAPIError(400, "invalid_parameters")
                return 200, self.status(**values)
            filters = _filters(route, raw_query)
            if self._not_ready():
                raise MaterialAPIError(404, "materials_not_ready")
            method = self.store.list_groups if route == "/api/materials/groups" else self.store.query
            return 200, method(**filters)
        except Exception as error:
            status, code = self._failure(error)
            return status, error_body(code)

    def _outcome(self, value):
        if not isinstance(value, dict) or value.get("state") not in {"published", "failed", "unconfirmed", "busy"}:
            value = {"state": "unconfirmed"}
        state = value["state"]
        version = _version(value.get("version"))
        if state == "published" and version is None:
            state = "unconfirmed"
        if state in {"unconfirmed", "busy"}:
            version = None
        code = None if state == "published" else _code(value.get("error"),
                   "material_busy" if state == "busy" else "material_publish_unconfirmed" if state == "unconfirmed" else "material_build_failed")
        return {"state": state, "version": version, "previous": _version(value.get("previous")), "code": code,
                "warning": _code(value.get("warning"), None), "finished_at": self._now()}

    def post(self, raw_query, raw_body):
        try:
            _query(raw_query, set())
            request_id = _request_id(raw_body)
        except MaterialAPIError as error:
            return error.status, error_body(error.code)
        outcome = {"state": "unconfirmed", "version": None, "previous": None,
                   "code": "material_publish_unconfirmed", "warning": None, "finished_at": None}
        started_at = self._now()
        with self._lock:
            active_id = self._active["request_id"] if self._active else None
            known = active_id == request_id or request_id in self._terminal
            if active_id is not None and not known:
                body = dict(error_body("materials_busy"), service_id=self.service_id, active_id=active_id,
                            request_registered=False)
                return 409, body
            if not known:
                self._active = {"service_id": self.service_id, "request_id": request_id, "state": "running",
                                "version": None, "previous": None, "code": None, "warning": None,
                                "started_at": started_at, "finished_at": None}
        if known:
            return 200, self.status(request_id, self.service_id)
        try:
            value = (self.store.refresh() if self.store is not None else
                     {"state": "failed", "error": "materials_unavailable"})
            outcome = self._outcome(value)
        except BaseException as error:
            # The store stack has unwound before the finally records terminal
            # state. A response/connection failure never owns the work lifetime.
            if not isinstance(error, Exception):
                raise
        finally:
            with self._lock:
                terminal = dict(self._active, **outcome)
                self._terminal[request_id] = terminal
                while len(self._terminal) > TERMINAL_LIMIT:
                    self._terminal.popitem(last=False)
                self._active = None
        return 200, self.status(request_id, self.service_id)
