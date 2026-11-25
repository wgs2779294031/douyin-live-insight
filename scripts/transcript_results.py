"""Durable per-segment facts and a recoverable compatibility speech projection.

All mutation uses the existing data/.transcribe.run lock. A result is the only
success authority; attempt files only prevent an older generation masquerading
as a pending/failed force attempt. Projection metadata is reconstructible.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import time

from file_lock import FileLock
from scripts import audio_segments as audio

_NAME = re.compile(r"seg_([0-9]{4,})\.flv\Z")
_GENERATION = re.compile(r"[0-9a-f]{32}\Z")
_MAX_RESULT_BYTES = 16 * 1024 * 1024


class ResultError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class WriterBusy(RuntimeError):
    pass


@contextmanager
def writer(data):
    """One global writer, including direct calls and projection-only recovery."""
    scope = audio.data_scope(data)
    lock = FileLock(Path(scope["path"]) / ".transcribe.run", timeout=0)
    if not lock.acquire(blocking=False):
        raise WriterBusy("busy")
    try:
        yield
    finally:
        lock.release()


def _locked(function):
    @wraps(function)
    def wrapped(data, *args, **kwargs):
        with writer(data):
            return function(data, *args, **kwargs)
    return wrapped


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _seal(value):
    return dict(value, checksum=hashlib.sha256(_bytes(value)).hexdigest())


def _supersedes(value):
    # Old candidate records have no cross-generation authority by default.
    entries = value.get("supersedes", [])
    if not isinstance(entries, list) or len(entries) > 3:
        raise ResultError("predecessors_invalid")
    pairs = set()
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) != {"generation", "result_sha256"}
                or not isinstance(entry["generation"], str) or not _GENERATION.fullmatch(entry["generation"])
                or not isinstance(entry["result_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", entry["result_sha256"])):
            raise ResultError("predecessors_invalid")
        pairs.add((entry["generation"], entry["result_sha256"]))
    if len(pairs) != len(entries):
        raise ResultError("predecessors_invalid")
    return pairs


def _authorization(result, attempt):
    if attempt is None:
        raise ResultError("attempt_missing")
    if attempt["generation"] != result["generation"] or attempt["state"] != "pending":
        raise ResultError("attempt_" + attempt["state"])
    if _supersedes(result) != _supersedes(attempt):
        raise ResultError("attempt_lineage_mismatch")
    return _supersedes(result)


def _check_lineage(result, digest, attempt, proof, *, require_current):
    source_id = result["source"]["source_id"]
    observed = {(entry["generation"], entry["result_sha256"])
                for identity, entry in (proof["_required_generations"] if proof else [])
                if identity == source_id}
    replacing = observed - {(result["generation"], digest)}
    if require_current or replacing:
        try:
            allowed = _authorization(result, attempt)
        except ResultError as error:
            if replacing and not require_current:
                raise ResultError("result_generation_missing") from error
            raise
        if not replacing.issubset(allowed):
            raise ResultError("result_generation_missing")


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ResultError("record_invalid")
        value[key] = item
    return value


def _invalid_constant(_value):
    raise ResultError("record_invalid")


def _decode(raw):
    return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)


def _check_record(value, kind):
    if not isinstance(value, dict):
        raise ResultError(kind + "_invalid")
    payload = {key: item for key, item in value.items() if key != "checksum"}
    try:
        valid = (type(value.get("schema")) is int and value["schema"] == 1 and value.get("kind") == kind
                 and value.get("checksum") == hashlib.sha256(_bytes(payload)).hexdigest())
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ResultError(kind + "_invalid")
    return value


def _regular(path, *, missing=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return None
        raise
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or path.is_symlink()
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise ResultError("unsafe_path")
    return info


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _read(path, limit=_MAX_RESULT_BYTES):
    before = _regular(path)
    with path.open("rb") as stream:
        if _identity(before) != _identity(os.fstat(stream.fileno())):
            raise ResultError("output_changed")
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ResultError("result_too_large")
        if _identity(before) != _identity(os.fstat(stream.fileno())):
            raise ResultError("output_changed")
    if _identity(before) != _identity(_regular(path)):
        raise ResultError("output_changed")
    return raw


def _json(path):
    try:
        return _decode(_read(path))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ResultError("record_invalid") from error


def _atomic(path, value):
    """One self-contained commit, not a separate result/success receipt pair."""
    raw = _bytes(value) + b"\n"
    if len(raw) > _MAX_RESULT_BYTES:
        raise ResultError("result_too_large")
    _regular(path, missing=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _regular(path, missing=True)
        os.replace(temporary, path)
        if _read(path) != raw:
            raise ResultError("commit_unverified")
    finally:
        temporary.unlink(missing_ok=True)


def _paths(data, folder, name=None, *, create=False):
    folder = audio.safe_folder(data, folder)
    result_dir = folder / ".results"
    if result_dir.exists() or result_dir.is_symlink():
        info = result_dir.lstat()
        if (not stat.S_ISDIR(info.st_mode) or result_dir.is_symlink()
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise ResultError("unsafe_path")
    elif create:
        result_dir.mkdir()
    if name is not None and (not isinstance(name, str) or not _NAME.fullmatch(name)):
        raise ResultError("invalid_segment")
    tag, date = folder.name[len("audio_"):-11], folder.name[-10:]
    datetime.strptime(date, "%Y-%m-%d")
    if not folder.name.startswith("audio_") or not tag:
        raise ResultError("invalid_folder")
    return folder, result_dir, tag, date


def result_path(data, folder, name):
    return _paths(data, folder, name)[1] / (Path(name).stem + ".json")


def attempt_path(data, folder, name):
    return _paths(data, folder, name)[1] / (Path(name).stem + ".attempt.json")


def projection_paths(data, folder):
    folder, _, tag, date = _paths(data, folder)
    return Path(audio.data_scope(data)["path"]) / f"speech_{tag}_{date}.jsonl", folder / ".speech-projection.json", folder / ".speech-projection.pending.json"


def _finite(value, *, positive=False):
    return (type(value) in (int, float) and math.isfinite(value)
            and (value > 0 if positive else value >= 0))


def _make_rows(folder, name, closed, raw_rows):
    _, _, tag, _ = _paths(Path(folder).parent, folder, name)
    if not isinstance(raw_rows, (list, tuple)):
        raise ResultError("rows_invalid")
    rows = []
    for row in raw_rows:
        if not isinstance(row, dict) or not isinstance(row.get("text"), str) or not row["text"].strip():
            raise ResultError("rows_invalid")
        start, end = row.get("start"), row.get("end")
        if not _finite(start) or not _finite(end) or end < start:
            raise ResultError("rows_invalid")
        absolute = float(closed["start_ts"]) + start
        rows.append({"ts": round(absolute, 3), "time": datetime.fromtimestamp(absolute).strftime("%Y-%m-%d %H:%M:%S"),
                     "text": row["text"], "dur": round(end - start, 2), "tag": tag,
                     "seg": Path(name).stem, "off": start})
    # Stable sort preserves legitimate repeated utterances and equal offsets.
    return sorted(rows, key=lambda row: row["off"])


def _load_result(data, folder, name):
    path = result_path(data, folder, name)
    raw = _read(path)
    try:
        result = _check_record(_decode(raw), "transcript_result")
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ResultError("result_invalid") from error
    closed = audio.verify_closed(data, folder, name, require_source=False)
    _, _, tag, _ = _paths(data, folder, name)
    rows = result.get("rows")
    if (result.get("source") != closed or not _GENERATION.fullmatch(str(result.get("generation", "")))
            or not isinstance(result.get("model"), str) or not result["model"]
            or not _finite(result.get("audio_seconds"), positive=True)
            or not _finite(result.get("committed_at")) or not isinstance(rows, list)
            or result.get("outcome") != ("speech" if rows else "no_speech")):
        raise ResultError("result_invalid")
    last_offset = -1
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"ts", "time", "text", "dur", "tag", "seg", "off"}
                or not _finite(row.get("off")) or not _finite(row.get("dur")) or not _finite(row.get("ts"))
                or not isinstance(row.get("text"), str) or not row["text"].strip()
                or row["tag"] != tag or row["seg"] != Path(name).stem or row["off"] < last_offset):
            raise ResultError("result_invalid")
        absolute = float(closed["start_ts"]) + row["off"]
        if (row["ts"] != round(absolute, 3)
                or row["time"] != datetime.fromtimestamp(absolute).strftime("%Y-%m-%d %H:%M:%S")):
            raise ResultError("result_invalid")
        last_offset = row["off"]
    _supersedes(result)
    return result, hashlib.sha256(raw).hexdigest()


def _attempt(data, folder, name):
    path = attempt_path(data, folder, name)
    try:
        value = _check_record(_json(path), "transcript_attempt")
    except FileNotFoundError:
        return None
    closed = audio.verify_closed(data, folder, name, require_source=False)
    if (value.get("scope") != audio.data_scope(data) or value.get("source_id") != closed["source_id"]
            or not _GENERATION.fullmatch(str(value.get("generation", "")))
            or value.get("state") not in {"pending", "failed"}):
        raise ResultError("attempt_invalid")
    _supersedes(value)
    return value


def _load_projection(data, folder, path):
    value = _check_record(_json(path), "speech_projection")
    speech, _, _ = projection_paths(data, folder)
    if (value.get("scope") != audio.data_scope(data) or value.get("speech") != speech.name
            or not isinstance(value.get("coverage"), dict) or type(value.get("rows")) is not int or value["rows"] < 0
            or type(value.get("size")) is not int or value["size"] < 0
            or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("sha256", "")))):
        raise ResultError("projection_invalid")
    for source_id, entry in value["coverage"].items():
        if (not re.fullmatch(r"[0-9a-f]{64}", source_id) or not isinstance(entry, dict)
                or not _GENERATION.fullmatch(str(entry.get("generation", "")))
                or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("result_sha256", "")))):
            raise ResultError("projection_invalid")
    return value


def _file_digest(path):
    before = _regular(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        if _identity(before) != _identity(os.fstat(stream.fileno())):
            raise ResultError("output_changed")
        remaining = before.st_size
        while remaining:
            chunk = stream.read(min(65536, remaining))
            if not chunk:
                raise ResultError("output_changed")
            digest.update(chunk)
            remaining -= len(chunk)
        if stream.read(1):
            raise ResultError("output_changed")
        if _identity(before) != _identity(os.fstat(stream.fileno())):
            raise ResultError("output_changed")
    if _identity(before) != _identity(_regular(path)):
        raise ResultError("output_changed")
    return before.st_size, digest.hexdigest()


def _legacy_guard(data, folder):
    folder = audio.safe_folder(data, folder)
    # This implementation never writes the old size/mtime progress format.
    # Its presence cannot be silently reinterpreted as a new durable fact.
    for legacy in (folder / ".transcribed.json", folder / ".transcribed.json.rebuild.pending"):
        if legacy.exists() or legacy.is_symlink():
            raise ResultError("legacy_conflict")


def _projection_owned(data, folder):
    _legacy_guard(data, folder)
    speech, receipt, pending = projection_paths(data, folder)
    proof = None
    required_coverage = {}
    required_generations = []
    for path in (receipt, pending):
        try:
            candidate = _load_projection(data, folder, path)
        except FileNotFoundError:
            continue
        # A later output may have reached disk before its receipt replacement.
        # An old receipt must not hide that pending projection's added sources.
        required_coverage.update(candidate["coverage"])
        required_generations.extend(candidate["coverage"].items())
        if path == receipt:
            proof = candidate
        elif proof is None:
            proof = candidate
    if _regular(speech, missing=True) is not None and proof is None:
        raise ResultError("legacy_conflict")
    # An unfinished first projection cannot claim arbitrary pre-existing bytes.
    if proof is not None and not receipt.exists() and speech.exists():
        if _file_digest(speech) != (proof["size"], proof["sha256"]):
            raise ResultError("legacy_conflict")
    return dict(proof, coverage=required_coverage, _required_generations=required_generations) if proof is not None else None


def _current_text_for_result(data, folder, name, result, digest):
    """Current accepted text only; never opens or grants existence of the source."""
    _legacy_guard(data, folder)
    attempt = _attempt(data, folder, name)
    _check_lineage(result, digest, attempt, _projection_owned(data, folder), require_current=True)
    speech, receipt, _ = projection_paths(data, folder)
    try:
        projection = _load_projection(data, folder, receipt)
        entry = projection["coverage"].get(result["source"]["source_id"])
        return (entry == {"generation": result["generation"], "result_sha256": digest}
                and _file_digest(speech) == (projection["size"], projection["sha256"]))
    except (ValueError, OSError):
        return False


def _current_text(data, folder, name):
    """Metadata-only guard for the cleanup caller holding the exact source handle.

    This returns no source-valid or deletion permission flag. The caller must
    independently verify its held source identity/content and cleanup operation.
    """
    result, digest = _load_result(data, folder, name)
    return result, digest, _current_text_for_result(data, folder, name, result, digest)


def validate_success(data, folder, name):
    """Read-only facts; eligibility never performs audio deletion."""
    status = {"name": name, "result_committed": False, "current_result": False,
              "projection_ready": False, "cleanup_eligible": False, "reusable": False,
              "reason": "result_missing", "generation": None,
              "source_id": None, "result_sha256": None,
              "audio_cleaned": False, "history_usable": False,
              "cleanup_state": "unconfirmed", "cleanup_reason": ""}
    try:
        result, digest = _load_result(data, folder, name)
        status.update(result_committed=True, generation=result["generation"],
                      source_id=result["source"]["source_id"], result_sha256=digest)
        projection_ready = _current_text_for_result(data, folder, name, result, digest)
        status["current_result"] = True
        source_valid = True
        source_absent = False
        try:
            audio.verify_closed(data, folder, name)
        except (ValueError, OSError) as error:
            source_valid = False
            status["reason"] = getattr(error, "reason", "source_missing" if isinstance(error, FileNotFoundError) else "source_unconfirmed")
            # Only an absent leaf in the still-valid data/day namespace can be
            # explained by a cleanup intent. Links, access errors and replaced
            # namespaces remain unconfirmed, regardless of a cleanup record.
            checked_folder = audio.safe_folder(data, folder)
            try:
                (checked_folder / name).lstat()
            except FileNotFoundError:
                source_absent = True
        status["reusable"] = source_valid
        status["projection_ready"] = projection_ready
        from scripts import audio_cleanup as cleanup
        try:
            operation = cleanup.read_operation(data, folder, name, result["source"], result["generation"], digest)
            status["cleanup_state"] = operation["state"] if operation is not None else "none"
            if operation is not None:
                if source_absent:
                    status["audio_cleaned"] = True
                elif not source_valid or operation["state"] == "deleted":
                    status["cleanup_state"] = "conflict"
                    status["cleanup_reason"] = "cleanup_source_changed" if not source_valid else "cleanup_source_present"
        except (ValueError, OSError) as error:
            reason = getattr(error, "reason", "cleanup_invalid")
            status["cleanup_state"] = "conflict" if reason in {"cleanup_conflict", "cleanup_binding_conflict"} else "invalid"
            status["cleanup_reason"] = reason
        status["history_usable"] = (source_valid or status["audio_cleaned"]) and projection_ready
        status["cleanup_eligible"] = (source_valid and projection_ready
                                      and status["cleanup_state"] in {"none", "pending"})
        if source_valid:
            status["reason"] = "complete" if status["projection_ready"] else "projection_pending"
        elif status["audio_cleaned"]:
            status["reason"] = "audio_cleaned" if projection_ready else "projection_pending"
    except FileNotFoundError:
        pass
    except (ValueError, OSError) as error:
        status["reason"] = getattr(error, "reason", "result_invalid")
    return status


@_locked
def begin_attempt(data, folder, name):
    closed = audio.verify_closed(data, folder, name)
    proof = _projection_owned(data, folder)
    _paths(data, folder, name, create=True)
    predecessors = {(entry["generation"], entry["result_sha256"])
                    for source_id, entry in (proof["_required_generations"] if proof else [])
                    if source_id == closed["source_id"]}
    path = result_path(data, folder, name)
    if path.exists():
        previous, digest = _load_result(data, folder, name)  # Never replace corrupt facts silently.
        predecessors.add((previous["generation"], digest))
    _attempt(data, folder, name)
    # Sorting only canonicalizes serialization; it never decides generation age.
    supersedes = [{"generation": generation, "result_sha256": digest}
                  for generation, digest in sorted(predecessors)]
    value = _seal({"schema": 1, "kind": "transcript_attempt", "scope": audio.data_scope(data),
                   "source_id": closed["source_id"], "generation": secrets.token_hex(16),
                   "state": "pending", "started_at": time.time(), "reason": "requested", "supersedes": supersedes})
    _supersedes(value)
    _atomic(attempt_path(data, folder, name), value)
    return value


@_locked
def fail_attempt(data, folder, name, generation, reason="transcription_failed"):
    value = _attempt(data, folder, name)
    if value is None or value["generation"] != generation:
        raise ResultError("attempt_changed")
    try:
        result, _ = _load_result(data, folder, name)
    except (ValueError, OSError):
        result = None
    if result is not None and result["generation"] == generation:
        return  # A committed self-contained result outranks a late status error.
    payload = {key: item for key, item in value.items() if key != "checksum"}
    payload.update(state="failed", reason=reason)
    _atomic(attempt_path(data, folder, name), _seal(payload))


@_locked
def commit_result(data, folder, name, closed, generation, raw_rows, audio_seconds, model_name):
    if audio.verify_closed(data, folder, name) != closed:
        raise ResultError("source_changed")
    attempt = _attempt(data, folder, name)
    if attempt is None or attempt["generation"] != generation or attempt["state"] != "pending":
        raise ResultError("attempt_changed")
    if not _finite(audio_seconds, positive=True) or not isinstance(model_name, str) or not model_name:
        raise ResultError("decode_unconfirmed")
    rows = _make_rows(folder, name, closed, raw_rows)
    value = _seal({"schema": 1, "kind": "transcript_result", "source": closed,
                   "generation": generation, "model": model_name, "committed_at": time.time(),
                   "audio_seconds": audio_seconds, "outcome": "speech" if rows else "no_speech", "rows": rows,
                   "supersedes": attempt.get("supersedes", [])})
    # The source is checked once more immediately before committing the fact.
    if audio.verify_closed(data, folder, name) != closed:
        raise ResultError("source_changed")
    _atomic(result_path(data, folder, name), value)
    result, _ = _load_result(data, folder, name)
    if result != value:
        raise ResultError("commit_unverified")
    if audio.verify_closed(data, folder, name) != closed:
        raise ResultError("source_changed")
    return result


@_locked
def process_segment(data, folder, name, recognize, model_name, force=False):
    status = validate_success(data, folder, name)
    if not force and (status["reusable"] or (status["audio_cleaned"] and status["history_usable"] and status["current_result"])):
        return dict(status, state="reused", completed_now=False, inferred=False)
    try:
        closed = audio.verify_closed(data, folder, name)
    except (ValueError, OSError) as error:
        return dict(status, state="source_unconfirmed", completed_now=False, inferred=False,
                    reason=getattr(error, "reason", "source_unconfirmed"))
    if closed["size"] < 4096:
        return dict(status, state="too_small", completed_now=False, inferred=False, cleanup_eligible=False, reason="too_small")
    attempt = begin_attempt(data, folder, name)
    try:
        rows, audio_seconds = recognize(Path(folder) / name)
        commit_result(data, folder, name, closed, attempt["generation"], rows, audio_seconds, model_name)
    except Exception as error:
        try:
            fail_attempt(data, folder, name, attempt["generation"])
        except (ValueError, OSError):
            pass  # The pending attempt still blocks any older success.
        status = validate_success(data, folder, name)
        return dict(status, state="failed", completed_now=False, inferred=True,
                    reason=getattr(error, "reason", "transcription_failed"))
    return dict(validate_success(data, folder, name), state="completed", completed_now=True, inferred=True)


@_locked
def rebuild_projection(data, folder):
    folder, result_dir, _, _ = _paths(data, folder)
    previous = _projection_owned(data, folder)
    speech, receipt, pending = projection_paths(data, folder)
    paths = sorted((path for path in result_dir.glob("seg_*.json") if re.fullmatch(r"seg_[0-9]{4,}\.json", path.name)),
                   key=lambda path: int(path.stem[4:]))
    if not paths:
        return {"projection_ready": False, "reason": "result_missing", "rows": 0}
    fd, name = tempfile.mkstemp(prefix=speech.name + ".", suffix=".tmp", dir=speech.parent)
    temporary = Path(name)
    coverage, digest, size, row_count = {}, hashlib.sha256(), 0, 0
    try:
        with os.fdopen(fd, "wb") as stream:
            for path in paths:
                result, result_digest = _load_result(data, folder, path.stem + ".flv")
                source_id = result["source"]["source_id"]
                coverage[source_id] = {"generation": result["generation"], "result_sha256": result_digest}
                attempt = _attempt(data, folder, path.stem + ".flv")
                _check_lineage(result, result_digest, attempt, previous, require_current=False)
                for row in result["rows"]:
                    raw = _bytes(row) + b"\n"
                    stream.write(raw)
                    digest.update(raw)
                    size += len(raw)
                    row_count += 1
            stream.flush()
            os.fsync(stream.fileno())
        if previous is not None and not set(previous["coverage"]).issubset(coverage):
            # A missing/corrupt fact cannot turn recovery into text deletion.
            raise ResultError("result_missing")
        value = _seal({"schema": 1, "kind": "speech_projection", "scope": audio.data_scope(data),
                       "speech": speech.name, "coverage": coverage, "size": size,
                       "sha256": digest.hexdigest(), "rows": row_count})
        # This is projection provenance for recovery, never a segment success.
        _atomic(pending, value)
        _regular(speech, missing=True)
        os.replace(temporary, speech)
        _atomic(receipt, value)
        if _file_digest(speech) != (size, digest.hexdigest()):
            raise ResultError("projection_unverified")
        try:
            pending.unlink()
        except OSError:
            pass  # A verified receipt/current output already supersedes intent.
        return {"projection_ready": True, "reason": "complete", "rows": row_count}
    finally:
        temporary.unlink(missing_ok=True)


def directory_status(data, folder):
    """Discover facts without obtaining a writer or loading a model."""
    folder, result_dir, _, _ = _paths(data, folder)
    discovered = {item["name"]: item for item in audio.discover(data, folder)}
    for path in result_dir.glob("seg_*.json"):
        if re.fullmatch(r"seg_[0-9]{4,}\.json", path.name):
            discovered.setdefault(path.stem + ".flv", {"name": path.stem + ".flv", "state": "source_unconfirmed", "reason": "source_missing"})
    statuses = []
    for name in sorted(discovered, key=lambda item: (int(_NAME.fullmatch(item)[1]) if _NAME.fullmatch(item) else float("inf"), item)):
        status = validate_success(data, folder, name)
        if status["current_result"]:
            state = "completed" if status["projection_ready"] else "projection_pending"
        elif status["result_committed"]:
            state = status["reason"]
        else:
            state = discovered[name]["state"]
        statuses.append(dict(discovered[name], **status, state=state))
    return statuses


discover = directory_status
