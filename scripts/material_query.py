"""Bounded, read-only queries over one pinned customer material snapshot.

Order is manifest file order, then byte order within each JSONL file.  It is
not a global time sort.  Only consumed 64 KiB chunks are rehashed; raw DATA is
not revisited.  Store owns snapshot/path/scope checks and publication.
"""
import base64
import copy
from datetime import date as _date, datetime, timedelta
import hashlib
import hmac
import json
import math
import re

try:
    from . import material_common as common
except ImportError:
    import material_common as common


CHUNK_BYTES = 64 * 1024
MAX_DATA_BYTES = 1024 * 1024
MAX_SCAN_LINES = 1000
MAX_SCAN_FILES = 64
MAX_LINE_BYTES = 128 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_CURSOR_BYTES = 2048
STREAMS = ("watch", "room_meta", "stats", "danmu", "events", "speech")
FRESHNESS = "snapshot_at_build_raw_not_rechecked"
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_IDENTITY = {"dev", "ino", "size", "mtime_ns", "nlink"}
_SOURCE_KEYS = {"version", "generated_at", "verified_at", "data_refs", "time_basis",
                "tag_is_speaker", "result_ref", "source_id", "generation",
                "result_sha256", "json_pointer", "off", "audio_state", "cleanup_state", "original_line"}
_GROUP_SOURCE_KEYS = {"version", "generated_at", "verified_at", "data_refs", "time_basis", "tag_is_speaker"}
_COUNT_KEYS = {"canonical_records", "segment_associations", "current_success", "no_speech",
               "audio_cleaned", "projection_pending", "incomplete", "failed", "legacy_unknown",
               "bad_intent", "blocked", "shared_records", "raw_records", "redaction_replacements"}
_SEGMENT_KEYS = {"segment", "result_ref", "closed_ref", "source_id", "generation", "result_sha256",
                 "state", "reason", "outcome", "rows", "canonical_records", "current_result",
                 "projection_ready", "history_usable", "audio_state", "cleanup_state", "source_present"}
_CANONICAL_KEYS = {"stream", "platform", "tag", "ts", "time", "text", "dur", "seg", "off", "view", "redaction", "source"}
_SHARED_KEYS = set(("stream platform tag ts type text room_id seg dur off state method sides products listed_rank_entries "
                    "online total_pv popularity is_hidden display_type display_long content toast prize_name box_name "
                    "status action share_type follow_count roommessagetype fan_type total update_timestamp msg_type "
                    "left_score right_score match_status display_status current_goal_stage final_goal_stage ticket_count "
                    "candidate_num start_time end_time duration winner_count total_score score outer_status side_count "
                    "live_status room_status fans user_count title").split())


def _source(value):
    if not isinstance(value, dict):
        _bad()
    result = {key: copy.deepcopy(item) for key, item in value.items() if key in _SOURCE_KEYS}
    for key, item in result.items():
        if item is None:
            continue
        if key in {"result_ref"} and not _relative(item):
            _bad()
        if key == "data_refs" and (not isinstance(item, list) or any(not _relative(ref) for ref in item)):
            _bad()
        if key in {"source_id", "result_sha256"} and (not isinstance(item, str) or not _SHA.fullmatch(item)):
            _bad()
        if key == "generation" and (not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{32}", item)):
            _bad()
        if key == "json_pointer" and (not isinstance(item, str) or not re.fullmatch(r"/rows/(?:0|[1-9][0-9]{0,12})", item)):
            _bad()
        if key in {"generated_at", "verified_at", "off"} and (not _finite(item) or item < 0):
            _bad()
        if key == "version" and (not isinstance(item, str) or not _TOKEN.fullmatch(item)):
            _bad()
        if key == "tag_is_speaker" and item is not False:
            _bad()
        if key == "time_basis" and item not in ("writer_wall_clock_plus_offset", "view_dependent_unverified",
                                                 "stored_record_timestamp_unverified"):
            _bad()
        if key == "audio_state" and item not in ("cleaned", "present", "missing_unconfirmed", "unconfirmed"):
            _bad()
        if key == "cleanup_state" and (not isinstance(item, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", item)):
            _bad()
        if key == "original_line":
            _bad()  # Only unknown/null is supported; never infer raw line numbers.
    return result


def _group_source(value):
    return {key: item for key, item in _source(value).items() if key in _GROUP_SOURCE_KEYS}


def _completeness(value):
    if not isinstance(value, dict):
        _bad()
    result = {}
    if "counts" in value:
        if not isinstance(value["counts"], dict):
            _bad()
        result["counts"] = {}
        for key, item in value["counts"].items():
            if key in _COUNT_KEYS:
                if item is not None and not _integer(item):
                    _bad()
                result["counts"][key] = item
    for key in ("state", "coverage", "raw_freshness", "segments", "inputs_nonatomic",
                "capture_complete", "legacy_speech_unverified", *sorted(_COUNT_KEYS)):
        if key not in value:
            continue
        item = value[key]
        if key == "segments":
            if not isinstance(item, list):
                _bad()
            safe = []
            for segment in item:
                if not isinstance(segment, dict):
                    _bad()
                record = {name: copy.deepcopy(field) for name, field in segment.items() if name in _SEGMENT_KEYS}
                for name, field in record.items():
                    if field is None:
                        continue
                    if name in {"result_ref", "closed_ref"}:
                        if not _relative(field):
                            _bad()
                    elif name in {"rows", "canonical_records"}:
                        if not _integer(field):
                            _bad()
                    elif name in {"current_result", "projection_ready", "history_usable", "source_present"}:
                        if type(field) is not bool:
                            _bad()
                    elif not _text(field, 240):
                        _bad()
                safe.append(record)
            result[key] = safe
        elif key in _COUNT_KEYS:
            if item is not None and not _integer(item):
                _bad()
            result[key] = item
        elif key in {"inputs_nonatomic", "capture_complete", "legacy_speech_unverified"}:
            if item is not None and type(item) is not bool:
                _bad()
            result[key] = item
        elif not _text(item, 64):
            _bad()
        else:
            result[key] = item
    return result


def _redaction(view, value):
    if not isinstance(value, dict):
        _bad()
    expected = ({"policy": "none", "applied": False} if view == "canonical" else
                {"policy": "legacy_url_email_phone", "actual_replacements": None, "original_row": None})
    if any(value.get(key) != item or (isinstance(item, bool) and value.get(key) is not item)
           for key, item in expected.items()):
        _bad()
    return expected


class MaterialQueryError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _bad(reason="material_corrupt"):
    raise MaterialQueryError(reason)


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      allow_nan=False, separators=(",", ":")).encode("utf-8")


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            _bad()
        value[key] = item
    return value


def _decode(raw):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                          parse_constant=lambda _value: _bad())
    except (ValueError, UnicodeError, RecursionError):
        _bad()


def _integer(value, minimum=0, maximum=2**63 - 1):
    return type(value) is int and minimum <= value <= maximum


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _text(value, limit=128):
    return (isinstance(value, str) and 0 < len(value) <= limit
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _day(value):
    try:
        return isinstance(value, str) and _date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _window(group):
    try:
        start, end = (datetime.fromisoformat(group[key]) for key in ("from", "until_exclusive"))
        return (start.utcoffset() == end.utcoffset() == timedelta(hours=8)
                and start.date().isoformat() == group["date"] and start < end)
    except (KeyError, TypeError, ValueError):
        return False


def _relative(value):
    # Common checks the physical components again when opening.  Never accept
    # Windows aliases/ADS even when this module is exercised on another OS.
    return (isinstance(value, str) and 0 < len(value) <= 1024
            and not any(char in value for char in "\\:\x00")
            and all(part and part not in {".", ".."}
                    and part == part.rstrip(" .") for part in value.split("/")))


def _filters(view, room, date, platform, stream=None, time_from=None, time_until=None):
    if not isinstance(view, str) or view not in {"canonical", "shared"}:
        _bad("invalid_filter")
    if room is not None and not _text(room):
        _bad("invalid_filter")
    if date is not None and not _day(date):
        _bad("invalid_filter")
    if platform is not None and (not isinstance(platform, str) or platform not in {"douyin", "bilibili"}):
        _bad("invalid_filter")
    if isinstance(stream, str):
        stream = [stream]
    if stream is not None:
        if (not isinstance(stream, (list, tuple)) or not 1 <= len(stream) <= 6
                or any(not isinstance(item, str) or item not in STREAMS for item in stream)):
            _bad("invalid_filter")
        stream = sorted(set(stream))
    if any(value is not None and not _finite(value) for value in (time_from, time_until)):
        _bad("invalid_filter")
    if time_from is not None and time_until is not None and time_from >= time_until:
        _bad("invalid_filter")
    return {"view": view, "room": room, "date": date, "platform": platform,
            "stream": stream, "time_from": time_from, "time_until": time_until}


def _limit(value):
    if not _integer(value, 1, 200):
        _bad("invalid_limit")
    return value


def _manifest(snapshot):
    value = snapshot.manifest
    try:
        size = len(_json_bytes(value))
    except (TypeError, ValueError, RecursionError):
        _bad()
    if size > MAX_MANIFEST_BYTES:
        _bad("manifest_capacity")
    if (not isinstance(value, dict) or type(value.get("schema")) is not int
            or value["schema"] != 1 or not isinstance(value.get("version"), str)
            or not _TOKEN.fullmatch(value["version"])
            or not isinstance(value.get("files"), list)
            or not isinstance(value.get("groups"), list)
            or not isinstance(value.get("data_scope"), dict)
            or not isinstance(value.get("discovery"), dict)
            or not _finite(value.get("generated_at")) or value["generated_at"] < 0
            or not _finite(value.get("verified_at")) or value["verified_at"] < 0):
        _bad()
    if not isinstance(snapshot.cursor_key, bytes) or len(snapshot.cursor_key) != 32:
        _bad()
    groups = {}
    for group in value["groups"]:
        if (not isinstance(group, dict) or not _text(group.get("id"))
                or group["id"] in groups or not _text(group.get("tag"))
                or not _day(group.get("date")) or group.get("platform") not in ("douyin", "bilibili")
                or not _window(group)
                or group.get("window_is_session") is not False
                or not isinstance(group.get("source"), dict)
                or not isinstance(group.get("completeness"), dict)
                or not isinstance(group.get("views"), dict)
                or set(group["views"]) != {"canonical", "shared"}):
            _bad()
        for info in group["views"].values():
            if (not isinstance(info, dict) or type(info.get("available")) is not bool
                    or not _integer(info.get("records")) or not _text(info.get("status"), 64)
                    or not isinstance(info.get("redaction"), dict)):
                _bad()
        _source(group["source"])
        _completeness(group["completeness"])
        for view, info in group["views"].items():
            _redaction(view, info["redaction"])
        groups[group["id"]] = group
    seen = set()
    for entry in value["files"]:
        if not isinstance(entry, dict):
            _bad()
        required = {"path", "view", "tag", "date", "platform", "group_id", "format",
                    "bytes", "sha256", "identity", "chunks"}
        if not required <= set(entry):
            _bad()
        identity = entry["identity"]
        if (not _relative(entry["path"]) or entry["path"].casefold() in seen
                or entry["view"] not in ("canonical", "shared")
                or not _text(entry["tag"]) or not _day(entry["date"])
                or entry["platform"] not in ("douyin", "bilibili")
                or not _text(entry["group_id"])
                or entry["format"] != {"canonical": "canonical_records", "shared": "shared_projection"}[entry["view"]]
                or not _integer(entry["bytes"]) or not isinstance(entry["sha256"], str)
                or not _SHA.fullmatch(entry["sha256"])
                or not isinstance(identity, dict)
                or set(identity) != _IDENTITY
                # Windows stat uses an unsigned 64-bit volume ID and up to 128 bits for the file ID.
                or not _integer(identity["dev"], maximum=2**64 - 1)
                or not _integer(identity["ino"], maximum=2**128 - 1)
                or not all(_integer(identity[field]) for field in ("size", "mtime_ns", "nlink"))
                or identity["size"] != entry["bytes"]
                or identity["nlink"] != 1
                or not isinstance(entry["chunks"], list)
                or len(entry["chunks"]) != (entry["bytes"] + CHUNK_BYTES - 1) // CHUNK_BYTES
                or any(not isinstance(digest, str) or not _SHA.fullmatch(digest) for digest in entry["chunks"])
                or ("records" in entry and not _integer(entry["records"]))):
            _bad()
        group = groups.get(entry["group_id"])
        if group is None or any(group[key] != entry[key] for key in ("tag", "date", "platform")):
            _bad()
        if "source" in entry:
            _source(entry["source"])
        if "completeness" in entry:
            _completeness(entry["completeness"])
        if "redaction" in entry:
            _redaction(entry["view"], entry["redaction"])
        seen.add(entry["path"].casefold())
    return value


def _filter_digest(filters):
    return hashlib.sha256(_json_bytes(filters)).hexdigest()


def _encode_cursor(snapshot, kind, filters, position):
    value = {"schema": 1, "kind": kind, "version": snapshot.manifest["version"],
             "filters": _filter_digest(filters), "position": position}
    raw = _json_bytes(value)
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=")
    mac = hmac.new(snapshot.cursor_key, raw, hashlib.sha256).hexdigest().encode("ascii")
    return (encoded + b"." + mac).decode("ascii")


def _cursor(snapshot, token, kind, filters):
    if token is None:
        return {"file": 0, "offset": 0} if kind == "query" else {"index": 0}
    try:
        if not isinstance(token, str) or len(token.encode("utf-8")) > MAX_CURSOR_BYTES:
            _bad("invalid_cursor")
        encoded, mac = token.encode("ascii").split(b".")
        if not re.fullmatch(rb"[A-Za-z0-9_-]+", encoded) or not re.fullmatch(rb"[0-9a-f]{64}", mac):
            _bad("invalid_cursor")
        raw = base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4))
        if base64.urlsafe_b64encode(raw).rstrip(b"=") != encoded:
            _bad("invalid_cursor")
        expected = hmac.new(snapshot.cursor_key, raw, hashlib.sha256).hexdigest().encode("ascii")
        if not hmac.compare_digest(mac, expected):
            _bad("invalid_cursor")
        value = _decode(raw)
        if (not isinstance(value, dict) or set(value) != {"schema", "kind", "version", "filters", "position"}
                or type(value["schema"]) is not int or value["schema"] != 1
                or value["kind"] != kind or value["filters"] != _filter_digest(filters)):
            _bad("invalid_cursor")
        if value["version"] != snapshot.manifest["version"]:
            _bad("cursor_expired")
        position = value["position"]
        keys = {"file", "offset"} if kind == "query" else {"index"}
        if not isinstance(position, dict) or set(position) != keys or not all(_integer(n) for n in position.values()):
            _bad("invalid_cursor")
        return position
    except MaterialQueryError as error:
        if error.reason in {"cursor_expired", "invalid_cursor"}:
            raise
        _bad("invalid_cursor")
    except (ValueError, TypeError, UnicodeError):
        _bad("invalid_cursor")


def _matches_file(entry, filters):
    return (entry["view"] == filters["view"]
            and all(filters[key] is None or filters[key] == entry[field]
                    for key, field in (("room", "tag"), ("date", "date"), ("platform", "platform"))))


class _Budget:
    def __init__(self):
        self.bytes = 0
        self.lines = 0
        self.files = 0

    def room_for_line(self):
        # A boundary check plus a maximum-size line spans at most three chunks.
        return self.bytes <= MAX_DATA_BYTES - 3 * CHUNK_BYTES


class _Reader:
    def __init__(self, stream, entry, budget):
        self.stream, self.entry, self.budget = stream, entry, budget
        self.cache = {}

    def chunk(self, index):
        if index not in self.cache:
            length = min(CHUNK_BYTES, self.entry["bytes"] - index * CHUNK_BYTES)
            if length <= 0:
                _bad()
            if self.budget.bytes + length > MAX_DATA_BYTES:
                _bad("query_budget")
            self.stream.seek(index * CHUNK_BYTES)
            raw = self.stream.read(length)
            self.budget.bytes += len(raw)
            if len(raw) != length or hashlib.sha256(raw).hexdigest() != self.entry["chunks"][index]:
                _bad("material_changed")
            self.cache[index] = raw
        return self.cache[index]

    def boundary(self, offset):
        if offset and self.chunk((offset - 1) // CHUNK_BYTES)[(offset - 1) % CHUNK_BYTES] != 10:
            _bad("invalid_cursor")

    def line(self, offset):
        chunks = []
        size, position = 0, offset
        while position < self.entry["bytes"]:
            raw = self.chunk(position // CHUNK_BYTES)[position % CHUNK_BYTES:]
            end = raw.find(b"\n")
            part = raw if end < 0 else raw[:end + 1]
            size += len(part)
            if size > MAX_LINE_BYTES:
                _bad("material_line_too_large")
            chunks.append(part)
            position += len(part)
            if end >= 0:
                return b"".join(chunks), position
        _bad("material_corrupt")  # Published JSONL always terminates with LF.


def _row(raw, entry):
    value = _decode(raw)
    if (not isinstance(value, dict) or value.get("tag") != entry["tag"]
            or value.get("platform") != entry["platform"]):
        _bad()
    if value.get("stream") == "export_info" and entry["view"] == "shared":
        if type(value.get("schema")) is not int or value["schema"] != 1 or value.get("window_is_session") is not False:
            _bad()
        return None
    if set(value) - (_CANONICAL_KEYS if entry["view"] == "canonical" else _SHARED_KEYS):
        _bad()
    if value.get("stream") not in STREAMS or not _finite(value.get("ts")):
        _bad()
    if entry["view"] == "canonical" and (value["stream"] != "speech" or value.get("view") != "canonical"
                                             or not isinstance(value.get("source"), dict)):
        _bad()
    if entry["view"] == "canonical":
        source = _source(value["source"])
        if any(source.get(key) is None for key in ("result_ref", "source_id", "generation", "result_sha256", "json_pointer")):
            _bad()
        _redaction("canonical", value.get("redaction"))
    return value


def _response(snapshot, items, cursor, budget, limited):
    # This is deliberately last: a switch/GC/change discards the entire page.
    snapshot.finish()
    refresh = {key: copy.deepcopy(snapshot.status[key]) for key in
               ("state", "version", "updated_at", "last_success_at", "error", "snapshot_stale")
               if key in snapshot.status}
    return {"version": snapshot.manifest["version"], "items": items, "refresh": refresh,
            "next_cursor": cursor, "scan": {"bytes": budget.bytes, "lines": budget.lines,
                                               "files": budget.files, "limited": limited,
                                               "complete": cursor is None},
            "source_freshness": FRESHNESS, "window_is_session": False,
            "tag_is_speaker": False, "speaker_separation": False,
            "order": "manifest_file_then_line", "content_verification": "consumed_chunk_sha256"}


def query(snapshot, *, view="canonical", room=None, date=None, platform=None,
          stream=None, time_from=None, time_until=None, cursor=None, limit=50):
    """Return one bounded page; an empty limited page may have next_cursor."""
    filters = _filters(view, room, date, platform, stream, time_from, time_until)
    limit = _limit(limit)
    manifest = _manifest(snapshot)
    position = _cursor(snapshot, cursor, "query", filters)
    files = manifest["files"]
    groups = {group["id"]: group for group in manifest["groups"]}
    index, offset = position["file"], position["offset"]
    if index > len(files) or (index == len(files) and offset):
        _bad("invalid_cursor")
    items, budget = [], _Budget()
    limited = False
    while index < len(files):
        entry = files[index]
        if offset > entry["bytes"] or (offset and not _matches_file(entry, filters)):
            _bad("invalid_cursor")
        if not _matches_file(entry, filters) or offset == entry["bytes"]:
            index, offset = index + 1, 0
            continue
        if budget.files >= MAX_SCAN_FILES or budget.lines >= MAX_SCAN_LINES or not budget.room_for_line():
            limited = True
            break
        budget.files += 1
        with common.open_file(snapshot.root, entry["path"], expected=entry["identity"]) as handle:
            reader = _Reader(handle, entry, budget)
            reader.boundary(offset)
            while offset < entry["bytes"]:
                if budget.lines >= MAX_SCAN_LINES or not budget.room_for_line():
                    limited = True
                    break
                start = offset
                raw, offset = reader.line(offset)
                budget.lines += 1
                value = _row(raw, entry)
                if value is None:
                    if offset != entry["bytes"]:
                        _bad()
                    continue
                if filters["stream"] is not None and value["stream"] not in filters["stream"]:
                    continue
                if filters["time_from"] is not None and value["ts"] < filters["time_from"]:
                    continue
                if filters["time_until"] is not None and value["ts"] >= filters["time_until"]:
                    continue
                item = copy.deepcopy(value)
                group = groups[entry["group_id"]]
                source = _group_source(group["source"])
                if isinstance(entry.get("source"), dict):
                    source.update(_group_source(entry["source"]))
                if view == "canonical":
                    source.update(_source(item["source"]))
                source["time_basis"] = ("writer_wall_clock_plus_offset" if view == "canonical"
                                        else "stored_record_timestamp_unverified")
                source["material"] = {"version": manifest["version"], "path": entry["path"],
                                      "sha256": entry["sha256"], "byte_offset": start}
                source["freshness"] = FRESHNESS
                item["source"] = source
                item["view"] = view
                item["completeness"] = _completeness(entry.get("completeness", group["completeness"]))
                item["redaction"] = _redaction(view, entry.get("redaction", group["views"][view]["redaction"]))
                item["window_is_session"] = False
                item["tag_is_speaker"] = False
                items.append(item)
                if len(items) == limit:
                    break
        if offset == entry["bytes"]:
            index, offset = index + 1, 0
        if limited or len(items) == limit:
            break
    following = (_encode_cursor(snapshot, "query", filters, {"file": index, "offset": offset})
                 if index < len(files) else None)
    return _response(snapshot, items, following, budget, limited)


def list_groups(snapshot, *, view="canonical", room=None, date=None, platform=None,
                cursor=None, limit=50):
    """List bounded manifest groups without opening any payload file."""
    filters = _filters(view, room, date, platform)
    limit = _limit(limit)
    manifest = _manifest(snapshot)
    position = _cursor(snapshot, cursor, "groups", filters)
    index = position["index"]
    groups = manifest["groups"]
    if index > len(groups):
        _bad("invalid_cursor")
    items, budget = [], _Budget()
    while index < len(groups) and budget.lines < MAX_SCAN_LINES and len(items) < limit:
        group = groups[index]
        index += 1
        budget.lines += 1
        if all(filters[key] is None or filters[key] == group[field]
               for key, field in (("room", "tag"), ("date", "date"), ("platform", "platform"))):
            item = {key: copy.deepcopy(group[key]) for key in
                    ("id", "platform", "tag", "date", "from", "until_exclusive",
                     "window_is_session", "source", "completeness") if key in group}
            item["view"] = view
            item["source"] = _group_source(group["source"])
            item["completeness"] = _completeness(group["completeness"])
            info = group["views"][view]
            item["view_info"] = {key: copy.deepcopy(info[key]) for key in ("available", "records", "status")}
            item["view_info"]["redaction"] = _redaction(view, info["redaction"])
            item["source_freshness"] = FRESHNESS
            item["window_is_session"] = False
            item["tag_is_speaker"] = False
            items.append(item)
    following = (_encode_cursor(snapshot, "groups", filters, {"index": index}) if index < len(groups) else None)
    return _response(snapshot, items, following, budget, following is not None and len(items) < limit)
