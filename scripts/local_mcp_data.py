"""Read-only MCP domain adapter; no SDK, publisher, lifecycle or file writes."""
import json
import math
from pathlib import Path
import re
import stat
import uuid

from scripts import material_common as common
from scripts.local_context import load_customer_context
from scripts.local_materials import MaterialStore
from scripts.material_query import _filters, _limit

MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
_VERSION = re.compile(r"v_[0-9a-f]{32}\Z")
_MESSAGES = {
    "invalid_context": "需要明确且通过核验的本地客户context。",
    "invalid_arguments": "只读工具参数无效。",
    "invalid_filter": "材料筛选条件无效。", "invalid_limit": "每页条数须为1至200的整数。",
    "invalid_cursor": "材料续页位置无效，请从头读取。", "cursor_expired": "材料版本已变化，请从头读取。",
    "material_changed": "材料已变化，请从头读取。", "material_version_changed": "材料版本已变化，请从头读取。",
    "material_scope_changed": "材料目录身份已变化，当前不可读取。",
    "materials_not_ready": "尚未建立资料；请先在本地复盘页明确更新，MCP不会代为整理。",
    "materials_corrupt": "材料凭据或内容未通过核验。", "materials_unavailable": "材料暂不可读取。",
    "runtime_unavailable": "当前应用或运行回执不可核定，不表示积压为零。",
    "result_too_large": "结果超过MCP输出上限，请降低limit或缩小筛选范围；没有返回截断页。",
    "result_encoding_failed": "结果未能安全序列化，没有返回部分内容。",
}
_CORRUPT = {"material_record_invalid", "material_checksum_mismatch", "material_pointer_invalid",
    "material_version_invalid", "material_owner_unconfirmed", "material_file_unconfirmed",
    "material_line_too_large", "material_truncated_line", "material_corrupt"}
_DIRECT = {"invalid_filter", "invalid_limit", "invalid_cursor", "cursor_expired", "material_changed",
           "material_version_changed", "material_scope_changed"}
_REFRESH_CODES = {"", "material_build_failed", "material_gc_blocked", "material_publish_unconfirmed",
    "material_capacity", "material_scope_changed", "material_path_unconfirmed", "material_changed",
    "material_file_unconfirmed", "material_unavailable", "material_line_too_large", "material_truncated_line",
    "material_record_invalid", "material_checksum_mismatch", "shared_projection_failed", "evidence_unconfirmed",
    "material_gc_pending", "material_refresh_unconfirmed"}


def error_result(code):
    code = code if code in _MESSAGES else "materials_unavailable"
    return {"ok": False, "code": code, "error": _MESSAGES[code], "data": None, "content_is_untrusted": True}


def _failure(error):
    reason = getattr(error, "reason", None)
    if isinstance(reason, str):
        if reason in _DIRECT:
            return reason
        if reason in _CORRUPT:
            return "materials_corrupt"
    return "materials_unavailable"


def bounded_result(value):
    """Measure compact UTF-8 payload without allocating a second large JSON.

    The SDK entry separately measures actual ToolResult and final wire frames.
    Large pages fail as a whole; neither rows nor cursors are silently dropped.
    """
    size = 0
    try:
        encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        for part in encoder.iterencode(value):
            size += len(part.encode("utf-8"))
            if size > MAX_PAYLOAD_BYTES:
                return error_result("result_too_large")
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        return error_result("result_encoding_failed")
    return value


def _success(data):
    return bounded_result({"ok": True, "code": None, "error": None, "data": data, "content_is_untrusted": True})


def _stamp(value):
    try:
        return value if type(value) in (int, float) and value > 0 and math.isfinite(value) else None
    except OverflowError:
        return None


def _uuid(value):
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


class ReadService:
    def __init__(self, context, *, bridge_factory=None):
        self.context = dict(context)
        self.store = MaterialStore(context)
        if bridge_factory is None:
            from scripts.local_mcp_bridge import RuntimeBridge
            bridge_factory = RuntimeBridge
        self.bridge = bridge_factory(context)

    @classmethod
    def from_context_file(cls, path):
        """Validate an explicit immutable startup input; never activate roles."""
        try:
            source = common.local_path(path)
            # Keep this configured startup file stable while the existing
            # validator rereads it. No original DATA/profile contents are read.
            with common.open_file(source.parent, source.name) as stream:
                raw = stream.read(65537)
                if len(raw) > 65536:
                    raise ValueError
                value = common.decode(raw)
                if not isinstance(value, dict):
                    raise ValueError
                for key in ("root", "data", "config", "roster", "dist", "python", "profile"):
                    common.local_path(value.get(key, ""))
                context = load_customer_context(source)
            return cls(context)
        except Exception:
            raise common.MaterialError("invalid_context") from None

    def _not_ready(self):
        try:
            info = self.store.root.lstat()
        except FileNotFoundError:
            self.store._scope()
            return True
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise common.MaterialError("material_owner_unconfirmed")
        with common.guard_directory(self.store.root):
            try:
                (self.store.root / "owner.json").lstat()
            except FileNotFoundError:
                raise common.MaterialError("material_owner_unconfirmed") from None
            owner = self.store._owner(create=False)
            return self.store._pointer(owner, missing=True) is None

    def _read(self, operation):
        try:
            if self._not_ready():
                return error_result("materials_not_ready")
            return _success(operation())
        except Exception as error:
            return error_result(_failure(error))

    def groups(self, *, view="canonical", room=None, date=None, platform=None, cursor=None, limit=50):
        try:
            _filters(view, room, date, platform)
            _limit(limit)
        except Exception as error:
            return error_result(_failure(error))
        return self._read(lambda: self.store.list_groups(view=view, room=room, date=date,
            platform=platform, cursor=cursor, limit=limit))

    def query(self, *, view="canonical", room=None, date=None, platform=None, stream=None,
              time_from=None, time_until=None, cursor=None, limit=50):
        try:
            _filters(view, room, date, platform, stream, time_from, time_until)
            _limit(limit)
        except Exception as error:
            return error_result(_failure(error))
        return self._read(lambda: self.store.query(view=view, room=room, date=date, platform=platform,
            stream=stream, time_from=time_from, time_until=time_until, cursor=cursor, limit=limit))

    def _material(self):
        empty = {"version": None, "generated_at": None, "verified_at": None, "refresh": None}
        try:
            if self._not_ready():
                return dict(empty, state="not_ready", code="materials_not_ready")
            with self.store.snapshot() as snapshot:
                manifest, diagnostic = snapshot.manifest, snapshot.status
                version = manifest.get("version")
                if not isinstance(version, str) or not _VERSION.fullmatch(version):
                    raise common.MaterialError("material_record_invalid")
                refresh = {key: diagnostic.get(key) for key in ("state", "version", "updated_at", "last_success_at", "snapshot_stale")}
                refresh["error"] = diagnostic.get("error") if diagnostic.get("error") in _REFRESH_CODES else "material_refresh_unconfirmed"
                result = {"state": "ready", "code": None, "version": version,
                    "generated_at": _stamp(manifest.get("generated_at")), "verified_at": _stamp(manifest.get("verified_at")),
                    "refresh": refresh, "source_freshness": "snapshot_at_build_raw_not_rechecked"}
                snapshot.finish()
                return result
        except Exception as error:
            code = _failure(error)
            return dict(empty, state="corrupt" if code == "materials_corrupt" else "unavailable", code=code)

    def status(self, *, request_id=None, service_id=None):
        if ((request_id is None) != (service_id is None)
                or request_id is not None and (not _uuid(request_id) or not _uuid(service_id))):
            return error_result("invalid_arguments")
        # No material read lease spans the runtime HTTP work. These are two
        # observations; neither current version nor timestamps settle a UUID.
        material = self._material()
        try:
            remote = self.bridge.status(request_id=request_id, service_id=service_id)
            data = {"schema": 1, "material": material, "runtime": remote["runtime"], "http": remote["http"],
                    "operation_inference": "only_explicit_http_receipt"}
            return _success(data)
        except Exception:
            return _success({"schema": 1, "material": material, "runtime": {"state": "unknown", "verified": False,
                "code": "runtime_unavailable", "observed_at": None}, "http": None,
                "operation_inference": "only_explicit_http_receipt"})

    def health(self):
        try:
            return _success(self.bridge.health())
        except Exception:
            return error_result("runtime_unavailable")
