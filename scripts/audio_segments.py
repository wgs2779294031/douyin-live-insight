"""Durable segment allocation and writer-published closure facts.

The counter is never removed by audio cleanup. A missing closure is unconfirmed,
not a claim that a file has stopped growing. No function here deletes audio.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile

from file_lock import file_lock

INDEX = ".segment-index.json"
CLOSED = ".segments"
_NAME = re.compile(r"seg_([0-9]+)\.flv\Z")
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_MAX_NUMBER = 2 ** 53 - 1
_MAX_JSON = 4 * 1024 * 1024


class SegmentError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _linked(path):
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def _no_links(path):
    for current in (path, *path.parents):
        if _linked(current):
            raise SegmentError("linked_path")


def _regular(path):
    _no_links(path)
    current = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
        raise SegmentError("invalid_file")
    return current


def data_scope(data):
    root = Path(data).absolute()
    _no_links(root)
    current = root.stat(follow_symlinks=False)
    if not stat.S_ISDIR(current.st_mode):
        raise SegmentError("invalid_data")
    if current.st_dev < 0 or current.st_ino <= 0:
        raise SegmentError("data_identity_unavailable")
    return {"path": os.path.normcase(str(root.resolve(strict=True))),
            "dev": current.st_dev, "ino": current.st_ino}


def safe_folder(data, folder):
    root = Path(data).absolute()
    data_scope(root)
    candidate = Path(folder).absolute()
    _no_links(candidate)
    if candidate.parent.resolve(strict=True) != root.resolve(strict=True):
        raise SegmentError("outside_data")
    if not candidate.is_dir():
        raise SegmentError("invalid_folder")
    return candidate


def _signature(current):
    return current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns


def fingerprint(path):
    path = Path(path)
    before = _regular(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        if _signature(os.fstat(stream.fileno())) != _signature(before):
            raise SegmentError("source_changed")
        remaining = before.st_size
        while remaining:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                raise SegmentError("source_changed")
            digest.update(chunk)
            remaining -= len(chunk)
        if stream.read(1):
            raise SegmentError("source_changed")
        if _signature(os.fstat(stream.fileno())) != _signature(before):
            raise SegmentError("source_changed")
    after = _regular(path)
    if _signature(after) != _signature(before):
        raise SegmentError("source_changed")
    return {"size": before.st_size, "mtime_ns": before.st_mtime_ns,
            "sha256": digest.hexdigest(), "dev": before.st_dev, "ino": before.st_ino}


def _invalid_constant(_value):
    raise SegmentError("invalid_json")


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise SegmentError("invalid_json")
        value[key] = item
    return value


def read_json(path):
    path = Path(path)
    before = _regular(path)
    if before.st_size > _MAX_JSON:
        raise SegmentError("state_too_large")
    with path.open("rb") as stream:
        if _signature(os.fstat(stream.fileno())) != _signature(before):
            raise SegmentError("state_changed")
        raw = stream.read(_MAX_JSON + 1)
        if _signature(os.fstat(stream.fileno())) != _signature(before):
            raise SegmentError("state_changed")
    if len(raw) > _MAX_JSON or _signature(_regular(path)) != _signature(before):
        raise SegmentError("state_changed")
    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=_invalid_constant,
                           object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise SegmentError("invalid_json") from error
    if not isinstance(value, dict):
        raise SegmentError("invalid_state")
    return value


def atomic_json(path, value):
    path = Path(path)
    _no_links(path.parent)
    if path.exists() or path.is_symlink():
        _regular(path)
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":")) + "\n").encode("utf-8")
    if len(raw) > _MAX_JSON:
        raise SegmentError("state_too_large")
    fd, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists() or path.is_symlink():
            _regular(path)
        os.replace(temporary, path)
        if read_json(path) != value:
            raise SegmentError("commit_unverified")
    finally:
        temporary.unlink(missing_ok=True)


def _number(name):
    match = _NAME.fullmatch(name)
    if (match is None or not 1 <= int(match[1]) <= _MAX_NUMBER
            or name != "seg_%04d.flv" % int(match[1])):
        raise SegmentError("invalid_segment_name")
    return int(match[1])


def _source_id(scope, relative, number):
    raw = json.dumps([scope, relative, number], ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _same_scope(value, expected):
    return (isinstance(value, dict) and set(value) == {"path", "dev", "ino"}
            and isinstance(value["path"], str) and type(value["dev"]) is int
            and type(value["ino"]) is int and value == expected)


def _safe_lock(path):
    _no_links(path)
    if path.exists():
        _regular(path)


def _closure_path(folder, name):
    return folder / CLOSED / (Path(name).stem + ".json")


def verify_closed(data, folder, name, require_source=True):
    folder = safe_folder(data, folder)
    number = _number(name)
    if name != "seg_%04d.flv" % number:
        raise SegmentError("invalid_segment_name")
    record = read_json(_closure_path(folder, name))
    scope = data_scope(data)
    relative = folder.name + "/" + name
    if (type(record.get("schema")) is not int or record["schema"] != 1 or record.get("kind") != "closed"
            or not _same_scope(record.get("scope"), scope) or record.get("relative") != relative
            or type(record.get("number")) is not int or record["number"] != number or record.get("file") != name
            or record.get("source_id") != _source_id(scope, relative, number)
            or type(record.get("size")) is not int or record["size"] < 0
            or type(record.get("mtime_ns")) is not int or record["mtime_ns"] < 0
            or type(record.get("dev")) is not int or record["dev"] < 0
            or type(record.get("ino")) is not int or record["ino"] <= 0
            or not isinstance(record.get("sha256"), str) or not _HEX.fullmatch(record["sha256"])
            or record.get("time_source") != "writer_wall_clock"
            or not isinstance(record.get("details"), dict)):
        raise SegmentError("invalid_closure")
    for key in ("start_ts", "end_ts"):
        if type(record.get(key)) not in (int, float) or not math.isfinite(record[key]):
            raise SegmentError("invalid_closure")
    if require_source:
        current = fingerprint(folder / name)
        if any(current[key] != record[key] for key in current):
            raise SegmentError("source_changed")
    return record


def _last_reserved(data, folder):
    path = folder / INDEX
    try:
        state = read_json(path)
    except FileNotFoundError:
        return 0
    if (type(state.get("schema")) is not int or state["schema"] != 1 or state.get("kind") != "segment_counter"
            or not _same_scope(state.get("scope"), data_scope(data)) or state.get("folder") != folder.name
            or type(state.get("last")) is not int or not 0 <= state["last"] <= _MAX_NUMBER):
        raise SegmentError("invalid_counter")
    return state["last"]


def reserve_segment(data, folder):
    folder = safe_folder(data, folder)
    index_path = folder / INDEX
    _safe_lock(index_path.with_name(index_path.name + ".lock"))
    with file_lock(index_path):
        maximum = _last_reserved(data, folder)
        for path in folder.glob("seg_*.flv"):
            _regular(path)
            maximum = max(maximum, _number(path.name))
        closed = folder / CLOSED
        if closed.exists() or closed.is_symlink():
            _no_links(closed)
            if not closed.is_dir():
                raise SegmentError("invalid_closure_directory")
            for path in closed.glob("*.json"):
                name = path.stem + ".flv"
                record = verify_closed(data, folder, name, require_source=False)
                maximum = max(maximum, record["number"])
        for filename in ("meta.json", ".transcribed.json"):
            try:
                old = read_json(folder / filename)
            except FileNotFoundError:
                continue
            if filename == "meta.json":
                segments = old.get("segments", [])
                sessions = old.get("sessions", [])
                if (not isinstance(segments, list) or not isinstance(sessions, list)
                        or any(not isinstance(row, dict) or "file" not in row for row in segments)
                        or any(not isinstance(row, dict) for row in sessions)):
                    raise SegmentError("invalid_legacy_meta")
                names = [row["file"] for row in segments]
            else:
                if any(not isinstance(row, dict) for row in old.values()):
                    raise SegmentError("invalid_legacy_progress")
                names = old
            for name in names:
                if not isinstance(name, str):
                    raise SegmentError("invalid_segment_name")
                maximum = max(maximum, _number(name))
        if maximum >= _MAX_NUMBER:
            raise SegmentError("counter_exhausted")
        number = maximum + 1
        atomic_json(index_path, {"schema": 1, "kind": "segment_counter", "scope": data_scope(data),
                                 "folder": folder.name, "last": number})
    return number, folder / ("seg_%04d.flv" % number)


def publish_closed(data, folder, path, index, start_ts, end_ts, **details):
    folder = safe_folder(data, folder)
    path = Path(path).absolute()
    if type(index) is not int or not 1 <= index <= _MAX_NUMBER:
        raise SegmentError("invalid_segment_path")
    if path.parent != folder or path.name != "seg_%04d.flv" % index or _number(path.name) != index:
        raise SegmentError("invalid_segment_path")
    if index > _last_reserved(data, folder):
        raise SegmentError("unreserved_segment")
    for value in (start_ts, end_ts):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise SegmentError("invalid_source_time")
    scope = data_scope(data)
    relative = folder.name + "/" + path.name
    record = {"schema": 1, "kind": "closed", "scope": scope,
              "source_id": _source_id(scope, relative, index), "relative": relative,
              "number": index, "file": path.name, **fingerprint(path),
              "start_ts": start_ts, "end_ts": end_ts,
              "time_source": "writer_wall_clock", "details": details}
    directory = folder / CLOSED
    directory.mkdir(exist_ok=True)
    _no_links(directory)
    destination = _closure_path(folder, path.name)
    _safe_lock(destination.with_name(destination.name + ".lock"))
    with file_lock(destination):
        if destination.exists():
            if verify_closed(data, folder, path.name) != record:
                raise SegmentError("closure_conflict")
        else:
            atomic_json(destination, record)
        if verify_closed(data, folder, path.name) != record:
            raise SegmentError("commit_unverified")
    return record


def discover(data, folder):
    folder = safe_folder(data, folder)
    names = {path.name for path in folder.glob("seg_*.flv")}
    closed = folder / CLOSED
    if closed.exists() or closed.is_symlink():
        _no_links(closed)
        if not closed.is_dir():
            raise SegmentError("invalid_closure_directory")
        names |= {path.stem + ".flv" for path in closed.glob("*.json")}
    results = []
    for name in sorted(names):
        try:
            record = verify_closed(data, folder, name)
        except (OSError, ValueError) as error:
            reason = getattr(error, "reason", "source_unconfirmed")
            results.append({"name": name, "state": "source_unconfirmed", "reason": reason})
        else:
            results.append({"name": name, "state": "closed", "reason": "closed", "closed": record})
    def order(row):
        try:
            number = _number(row["name"])
        except SegmentError:
            number = _MAX_NUMBER + 1
        return number, row["name"]
    return sorted(results, key=order)
