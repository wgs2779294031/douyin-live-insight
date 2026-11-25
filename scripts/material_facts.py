"""Explicit local material build; never query, repair, transcribe or clean DATA."""
from collections import OrderedDict
from contextlib import ExitStack, redirect_stdout
from datetime import date, datetime, time, timedelta
import io
import json
import math
import os
from pathlib import Path
import re

from scripts import audio_segments as audio
from scripts import material_common as common
from scripts import transcript_results as results
from scripts.local_context import validate_role_tag
from scripts.local_data import build_index, export_records

MAX_INPUT_FILES = 4096
MAX_DISCOVERY_ENTRIES = 16384
MAX_INPUT_BYTES = 256 * 1024 * 1024
MAX_INPUT_RECORDS = 500000
MAX_GROUPS = 1024
_DAY = re.compile(r"audio_(.+)_([0-9]{4}-[0-9]{2}-[0-9]{2})\Z")
_SEGMENT = re.compile(r"(seg_[0-9]{4,})(?:\.attempt)?\.json\Z")
_DATED = re.compile(r"(stats|danmu|events)_(douyin|bilibili)_(.+)_([0-9]{4}-[0-9]{2}-[0-9]{2})\.jsonl\Z")
_SPEECH = re.compile(r"speech_(.+)_([0-9]{4}-[0-9]{2}-[0-9]{2})\.jsonl\Z")
_STATES = ("current_speech", "current_no_speech", "cleaned_speech", "cleaned_no_speech",
           "projection_pending", "incomplete", "failed", "legacy_unknown", "bad_intent", "blocked")
_CANONICAL_REDACTION = {"policy": "none", "applied": False}
_SHARED_REDACTION = {"policy": "legacy_url_email_phone", "actual_replacements": None, "original_row": None}


def _safe_reason(value, fallback="evidence_unconfirmed"):
    return value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) else fallback


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _day(stamp):
    if not _finite(stamp):
        raise common.MaterialError("source_time_unconfirmed")
    try:
        return datetime.fromtimestamp(stamp, export_records.BEIJING).date().isoformat()
    except (ValueError, OverflowError, OSError) as error:
        raise common.MaterialError("source_time_unconfirmed") from error


class _Inputs:
    def __init__(self, data):
        self.data, self.files, self.bytes, self.records = data, {}, 0, 0
        self.appended, self.entries, self.raw_reads = False, 0, set()

    def add(self, path, *, raw=False, sound=False):
        path = Path(path)
        if path in self.files:
            return self.files[path][0]
        try:
            info = common.identity(path)
        except FileNotFoundError:
            return None
        if len(self.files) >= MAX_INPUT_FILES:
            raise common.MaterialError("discovery_capacity")
        self.files[path] = (info, raw, sound)
        if not sound:
            self.charge(info["size"])
        return info

    def charge(self, amount):
        self.bytes += amount
        if self.bytes > MAX_INPUT_BYTES:
            raise common.MaterialError("discovery_capacity")

    def record(self, amount=1):
        self.records += amount
        if self.records > MAX_INPUT_RECORDS:
            raise common.MaterialError("discovery_capacity")

    def finish(self):
        for path, (before, raw, sound) in self.files.items():
            if sound:
                continue  # Audio validity belongs to the checked 7A/8A facts.
            try:
                current = common.identity(path)
            except (ValueError, OSError) as error:
                raise common.MaterialError("source_changed_during_build") from error
            if current == before:
                continue
            if (raw and current["dev"] == before["dev"] and current["ino"] == before["ino"]
                    and current["nlink"] == 1 and current["size"] > before["size"]):
                self.appended = True
                continue
            raise common.MaterialError("source_changed_during_build")


def _entries(path, inputs):
    found = []
    with os.scandir(path) as entries:
        for entry in entries:
            inputs.entries += 1
            if inputs.entries > MAX_DISCOVERY_ENTRIES:
                raise common.MaterialError("discovery_capacity")
            found.append(Path(entry.path))
    return sorted(found, key=lambda item: item.name)


def _raw_rows(path, is_json, inputs):
    before = inputs.add(path, raw=True)
    if before is None:
        return
    if path in inputs.raw_reads:
        inputs.charge(before["size"])
    inputs.raw_reads.add(path)
    with path.open("rb") as stream:
        current = common.identity(stream.fileno())
        if current != before:
            if (current["dev"] != before["dev"] or current["ino"] != before["ino"]
                    or current["size"] <= before["size"]):
                raise common.MaterialError("source_changed_during_build")
            inputs.appended = True
        remaining = before["size"]
        while remaining:
            raw = stream.read(min(remaining, common.MAX_LINE_BYTES + 1)) if is_json else stream.readline(min(remaining, common.MAX_LINE_BYTES + 1))
            if not raw:
                raise common.MaterialError("source_changed_during_build")
            remaining -= len(raw)
            if len(raw) > common.MAX_LINE_BYTES or is_json and remaining:
                raise common.MaterialError("source_line_too_large")
            if not is_json and not raw.endswith(b"\n"):
                if remaining:
                    raise common.MaterialError("source_line_too_large")
                break  # The existing publisher also excludes an unfinished tail.
            if not raw.strip():
                continue
            value = common.decode(raw)
            if not isinstance(value, dict):
                raise common.MaterialError("source_record_invalid")
            inputs.record()
            yield value


def _output_identity(stream, path, expected=None):
    common.local_path(path)
    current = common.identity(stream if isinstance(stream, int) else stream.fileno())
    if (expected is not None and (current["dev"], current["ino"]) != (expected["dev"], expected["ino"])):
        raise common.MaterialError("material_changed")
    if common.identity(path) != current:
        raise common.MaterialError("material_changed")
    return current


class _Canonical:
    def __init__(self, root):
        self.root, self.handles, self.sizes, self.counts, self.identities = root, OrderedDict(), {}, {}, {}

    def ensure(self, group):
        name = "canonical/" + group["id"] + ".jsonl"
        if name not in self.sizes:
            with (self.root / name).open("xb") as stream:
                created = _output_identity(stream, self.root / name)
                stream.flush()
                os.fsync(stream.fileno())
                _output_identity(stream, self.root / name, created)
            self.sizes[name], self.counts[name] = 0, 0
            self.identities[name] = created
        return name

    @staticmethod
    def _finish(stream, path=None, expected=None):
        failure = None
        try:
            if path is not None:
                _output_identity(stream, path, expected)
            stream.flush()
            os.fsync(stream.fileno())
            if path is not None:
                _output_identity(stream, path, expected)
        except (OSError, ValueError) as error:
            failure = error
        finally:
            try:
                stream.close()
            except (OSError, ValueError) as error:
                if failure is None:
                    failure = error
        if failure is not None:
            raise failure

    def emit(self, group, row):
        name = self.ensure(group)
        raw = common.encode(row)
        if len(raw) > common.MAX_LINE_BYTES or self.sizes[name] + len(raw) > common.MAX_FILE_BYTES:
            raise common.MaterialError("material_capacity")
        if name not in self.handles:
            if len(self.handles) >= 16:
                _name, old = self.handles.popitem(last=False)
                self._finish(old, self.root / _name if _name in self.identities else None, self.identities.get(_name))
            path = self.root / name
            common.local_path(path)
            descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0))
            try:
                current = _output_identity(descriptor, path, self.identities[name])
                if current["size"] != self.sizes[name]:
                    raise common.MaterialError("material_changed")
                self.handles[name] = os.fdopen(descriptor, "ab")
                descriptor = None
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        self.handles.move_to_end(name)
        _output_identity(self.handles[name], self.root / name, self.identities[name])
        self.handles[name].write(raw)
        self.sizes[name] += len(raw)
        self.counts[name] += 1

    def close(self):
        failure = None
        try:
            for name, stream in self.handles.items():
                try:
                    self._finish(stream, self.root / name if name in self.identities else None, self.identities.get(name))
                except (OSError, ValueError) as error:
                    if failure is None:
                        failure = error
        finally:
            self.handles.clear()
        if failure is not None:
            raise failure


def _shared_projection(data, root, target, inputs, directories):
    """Original six-stream bytes, through the builder's bounded source reader."""
    tag, platform, day = target["tag"], target["platform"], date.fromisoformat(target["date"])
    if (not re.fullmatch(r"[\w\u3400-\u9fff .()（）·-]{1,95}", tag)
            or tag != tag.strip(" .") or platform not in {"douyin", "bilibili"}):
        return {"status": "unsupported_tag"}
    beginning = datetime.combine(day, time.min, export_records.BEIJING)
    finish = beginning + timedelta(days=1)
    counts = {kind: 0 for kind in export_records.NUMERIC}
    present = {kind: False for kind in export_records.NUMERIC}
    output, length, created = None, 0, None
    relative = "exports/数据/" + day.isoformat() + "/" + platform + "_" + tag + ".jsonl"

    def emit(value):
        nonlocal output, length, created
        # Keep legacy field insertion order/encoding; common.encode sorts keys.
        raw = (json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(raw) > common.MAX_LINE_BYTES or length + len(raw) > common.MAX_FILE_BYTES:
            raise common.MaterialError("material_capacity")
        if output is None:
            directory = root / "exports/数据" / day.isoformat()
            directory.mkdir(exist_ok=True)
            directories.enter_context(common.guard_directory(directory))
            output = (root / relative).open("xb")
            created = _output_identity(output, root / relative)
        _output_identity(output, root / relative, created)
        output.write(raw)
        length += len(raw)

    try:
        for kind, path in export_records.sources(data, tag, platform, day):
            if inputs.add(path, raw=True) is None:
                continue
            ref = path.relative_to(data).as_posix()
            if ref not in target["source"]["data_refs"]:
                target["source"]["data_refs"].append(ref)
            if kind in {"watch", "room_meta"} or day.isoformat() in path.stem[-10:]:
                present[kind] = True
            for row in _raw_rows(path, kind == "room_meta", inputs):
                stamp = row.get("fetched_at" if kind == "room_meta" else "ts")
                if not _finite(stamp):
                    raise common.MaterialError("source_time_unconfirmed")
                if not beginning.timestamp() <= stamp < finish.timestamp():
                    continue
                if row.get("platform", platform) != platform or row.get("tag", tag) != tag:
                    raise common.MaterialError("source_identity_mismatch")
                emit(export_records.project(kind, row, stamp, tag, platform))
                counts[kind] += 1
                present[kind] = True
        if output is None:
            return {"status": "empty"}
        emit({"stream": "export_info", "schema": 1, "platform": platform, "tag": tag,
              "from": beginning.isoformat(), "until_exclusive": finish.isoformat(),
              "counts": counts, "source_present": present, "window_is_session": False})
        return {"status": "available", "relative": relative, "records": sum(counts.values()), "identity": created}
    except common.MaterialError:
        raise
    except (ValueError, OverflowError) as error:
        raise common.MaterialError("shared_projection_failed") from error
    finally:
        if output is not None:
            _Canonical._finish(output, root / relative, created)

def _segment_state(status, result):
    if status.get("material_intent_bad"):
        return "bad_intent"
    if result is not None and status.get("current_result") and status.get("history_usable"):
        return ("cleaned_" if status.get("audio_cleaned") else "current_") + ("no_speech" if result["outcome"] == "no_speech" else "speech")
    reason = status.get("reason", "")
    if reason == "attempt_pending":
        return "incomplete"
    if reason.startswith("attempt_") or reason in {"result_generation_missing", "predecessors_invalid"}:
        return "failed" if reason == "attempt_failed" else "bad_intent"
    if status.get("current_result") and not status.get("projection_ready"):
        return "projection_pending"
    if reason == "result_missing":
        return "incomplete"
    if reason == "legacy_conflict":
        return "legacy_unknown"
    return "blocked"


def build_materials(data, version_root, *, material_version, generated_at, expected_scope=None):
    """Build into one new, caller-owned version directory; return manifest parts."""
    data, root = common.local_path(data), common.local_path(version_root)
    scope = audio.data_scope(data)
    if expected_scope is not None and scope != expected_scope:
        raise common.MaterialError("data_scope_changed")
    if (data == root or data in root.parents or root in data.parents
            or not isinstance(material_version, str) or not material_version or len(material_version) > 128
            or not _finite(generated_at) or generated_at <= 0):
        raise common.MaterialError("material_build_invalid")
    if not root.is_dir():
        raise common.MaterialError("material_path_unconfirmed")
    directories = ExitStack()
    groups, inputs, canonical = {}, _Inputs(data), _Canonical(root)
    unplaced, audio_days, segment_total = [], [], 0

    def check_scope():
        if audio.data_scope(data) != scope:
            raise common.MaterialError("data_scope_changed")

    def group(platform, tag, day):
        validate_role_tag(tag)
        key = (platform, tag, day)
        if key not in groups:
            if len(groups) >= MAX_GROUPS:
                raise common.MaterialError("discovery_capacity")
            midnight = datetime.combine(date.fromisoformat(day), time.min, export_records.BEIJING)
            groups[key] = {"id": common.group_id(*key), "platform": platform, "tag": tag, "date": day,
                "from": midnight.isoformat(), "until_exclusive": (midnight + timedelta(days=1)).isoformat(),
                "window_is_session": False,
                "source": {"version": material_version, "generated_at": generated_at, "verified_at": generated_at,
                    "data_refs": [], "time_basis": "view_dependent_unverified", "tag_is_speaker": False,
                    "notes": ["date_window_is_not_a_session", "tag_is_not_a_speaker",
                              "canonical_writer_clock_shared_stored_timestamp_both_unverified"]},
                "completeness": {"state": "unknown", "coverage": "observed_material_only",
                    "raw_freshness": "snapshot_at_build_raw_not_rechecked", "inputs_nonatomic": True,
                    "capture_complete": None, "legacy_speech_unverified": False, "segments": [],
                    "counts": {"canonical_records": 0, "segment_associations": 0, "current_success": 0,
                        "no_speech": 0, "audio_cleaned": 0, "projection_pending": 0, "incomplete": 0,
                        "failed": 0, "legacy_unknown": 0, "bad_intent": 0, "blocked": 0,
                        "shared_records": 0, "raw_records": None, "redaction_replacements": None}},
                "views": {"canonical": {"available": False, "records": 0, "status": "unavailable", "redaction": dict(_CANONICAL_REDACTION)},
                          "shared": {"available": False, "records": 0, "status": "empty", "redaction": dict(_SHARED_REDACTION)}}}
        return groups[key]

    def add_ref(target, path):
        ref = path.relative_to(data).as_posix()
        if ref not in target["source"]["data_refs"]:
            target["source"]["data_refs"].append(ref)

    try:
        directories.enter_context(common.guard_directory(data))
        directories.enter_context(common.guard_directory(root))
        for directory in (root / "canonical", root / "exports", root / "exports/数据"):
            directory.mkdir(exist_ok=True)
            directories.enter_context(common.guard_directory(directory))
        # Only recognized direct children: no roster, browser/model recursion or NAS.
        for entry in _entries(data, inputs):
            check_scope()
            match = _DAY.fullmatch(entry.name)
            if match:
                validate_role_tag(match[1])
                date.fromisoformat(match[2])
                audio.safe_folder(data, entry)
                audio_days.append((entry, match[1]))
                continue
            kind = platform = tag = None
            is_json = False
            match = _DATED.fullmatch(entry.name)
            if match:
                kind, platform, tag = match[1], match[2], match[3]
                date.fromisoformat(match[4])
            elif match := _SPEECH.fullmatch(entry.name):
                kind, platform, tag = "speech", "douyin", match[1]
                date.fromisoformat(match[2])
            elif entry.name.startswith("watch_") and entry.name.endswith(".jsonl"):
                kind, platform, tag = "watch", "douyin", entry.name[6:-6]
            elif match := re.fullmatch(r"room_meta_(douyin|bilibili)_(.+)\.json", entry.name):
                kind, platform, tag, is_json = "room_meta", match[1], match[2], True
            if kind is None:
                continue
            validate_role_tag(tag)
            found = False
            for row in _raw_rows(entry, is_json, inputs):
                if row.get("tag", tag) != tag or row.get("platform", platform) != platform:
                    raise common.MaterialError("source_identity_mismatch")
                target = group(platform, tag, _day(row.get("fetched_at" if is_json else "ts")))
                add_ref(target, entry)
                if kind == "speech":
                    target["completeness"]["legacy_speech_unverified"] = True
                found = True
            if not found:
                unplaced.append({"source_ref": entry.relative_to(data).as_posix(), "tag": tag,
                                 "reason": "no_committed_timestamped_rows"})

        for folder, tag in audio_days:
            with common.guard_directory(folder):
                names = set()
                for path in _entries(folder, inputs):
                    if re.fullmatch(r"seg_[0-9]{4,}\.flv", path.name):
                        inputs.add(path, sound=True)
                        names.add(path.name)
                for dirname in (".segments", ".results", ".cleanup"):
                    directory = folder / dirname
                    if not directory.exists() and not directory.is_symlink() and not getattr(directory, "is_junction", lambda: False)():
                        continue
                    common.local_path(directory)
                    for path in _entries(directory, inputs):
                        if match := _SEGMENT.fullmatch(path.name):
                            inputs.add(path)
                            names.add(match[1] + ".flv")
                for name in (".speech-projection.json", ".speech-projection.pending.json", ".transcribed.json", ".transcribed.json.rebuild.pending"):
                    path = folder / name
                    if path.exists():
                        inputs.add(path)
                for name in sorted(names, key=lambda item: int(item[4:-4])):
                    check_scope()
                    segment_total += 1
                    result, digest, closed = None, None, None
                    source = folder / name
                    if (folder / ".results" / (source.stem + ".json")).exists():
                        info = inputs.add(source, sound=True)
                        if info is not None:
                            inputs.charge(2 * info["size"])
                    status = results.validate_success(data, folder, name)
                    try:
                        result, digest = results._load_result(data, folder, name)
                        closed = result["source"]
                    except (ValueError, OSError):
                        try:
                            closed = audio.verify_closed(data, folder, name, require_source=False)
                        except (ValueError, OSError):
                            pass
                    # A failed attempt with no result is distinct from never accepted.
                    if status.get("reason") == "result_missing":
                        try:
                            intent = results._attempt(data, folder, name)
                            if intent is not None:
                                status = dict(status, reason="attempt_" + intent["state"])
                        except (ValueError, OSError) as error:
                            status = dict(status, reason=_safe_reason(getattr(error, "reason", None)), material_intent_bad=True)
                    elif result is not None and not status.get("current_result"):
                        try:
                            if results._attempt(data, folder, name) is None:
                                status = dict(status, reason="attempt_missing")
                        except (ValueError, OSError) as error:
                            status = dict(status, reason=_safe_reason(getattr(error, "reason", None)), material_intent_bad=True)
                    eligible = bool(result is not None and status.get("current_result") and status.get("history_usable"))
                    if result is None and status.get("current_result") and status.get("history_usable"):
                        raise common.MaterialError("source_changed_during_build")
                    if eligible:
                        if (status.get("source_id"), status.get("generation"), status.get("result_sha256")) != (closed["source_id"], result["generation"], digest):
                            raise common.MaterialError("source_changed_during_build")
                        again = results.validate_success(data, folder, name)
                        if (not again.get("current_result") or not again.get("history_usable")
                                or (again.get("source_id"), again.get("generation"), again.get("result_sha256"))
                                != (closed["source_id"], result["generation"], digest)):
                            raise common.MaterialError("source_changed_during_build")
                        status = again
                    if closed is None:
                        unplaced.append({"source_ref": folder.name + "/" + name, "tag": tag,
                                         "reason": _safe_reason(status.get("reason"))})
                        continue
                    row_groups = {}
                    if eligible and result["rows"]:
                        for index, row in enumerate(result["rows"]):
                            inputs.record()
                            target = group("douyin", tag, _day(row["ts"]))
                            row_groups.setdefault(target["id"], (target, []))[1].append((index, row))
                    else:
                        target = group("douyin", tag, _day(closed["start_ts"]))
                        row_groups[target["id"]] = (target, [])
                    current_state = _segment_state(status, result)
                    for target, rows in row_groups.values():
                        add_ref(target, folder / ".segments" / (source.stem + ".json"))
                        result_ref = folder.name + "/.results/" + source.stem + ".json" if result is not None else None
                        if result_ref:
                            add_ref(target, folder / ".results" / (source.stem + ".json"))
                        try:
                            source.lstat()
                            present = True
                        except FileNotFoundError:
                            present = False
                        except OSError:
                            present = None
                        audio_state = "cleaned" if status.get("audio_cleaned") else "present" if status.get("reusable") else "missing_unconfirmed" if present is False else "unconfirmed"
                        fact = {"segment": name, "result_ref": result_ref,
                            "closed_ref": folder.name + "/.segments/" + source.stem + ".json",
                            "source_id": closed["source_id"], "generation": result["generation"] if result is not None else None,
                            "result_sha256": digest, "state": current_state, "reason": _safe_reason(status.get("reason")),
                            "outcome": result["outcome"] if result is not None else None,
                            "rows": len(result["rows"]) if result is not None else None, "canonical_records": len(rows),
                            "current_result": status.get("current_result") is True, "projection_ready": status.get("projection_ready") is True,
                            "history_usable": status.get("history_usable") is True, "audio_state": audio_state,
                            "cleanup_state": status.get("cleanup_state", "unconfirmed"), "source_present": present}
                        target["completeness"]["segments"].append(fact)
                        if eligible:
                            canonical.ensure(target)
                            target["views"]["canonical"]["available"] = True
                        for index, row in rows:
                            source_ref = {"result_ref": result_ref, "source_id": closed["source_id"],
                                "generation": result["generation"], "result_sha256": digest, "json_pointer": "/rows/" + str(index),
                                "off": row["off"], "audio_state": audio_state, "cleanup_state": status.get("cleanup_state"),
                                "verified_at": generated_at, "original_line": None,
                                "time_basis": "writer_wall_clock_plus_offset"}
                            canonical.emit(target, dict(row, stream="speech", platform="douyin", source=source_ref,
                                                        view="canonical", redaction=dict(_CANONICAL_REDACTION)))
        canonical.close()
        files = []
        for target in sorted(groups.values(), key=lambda item: (item["date"], item["platform"], item["tag"])):
            check_scope()
            view = target["views"]["canonical"]
            if view["available"]:
                relative = "canonical/" + target["id"] + ".jsonl"
                item = common.describe_file(root, relative)
                if (item["identity"]["dev"], item["identity"]["ino"]) != (canonical.identities[relative]["dev"], canonical.identities[relative]["ino"]):
                    raise common.MaterialError("material_changed")
                item.update(records=canonical.counts[relative], view="canonical", tag=target["tag"], date=target["date"],
                    platform=target["platform"], group_id=target["id"], format="canonical_records",
                    source=dict(target["source"], time_basis="writer_wall_clock_plus_offset"), redaction=dict(_CANONICAL_REDACTION))
                files.append(item)
                view.update(records=item["records"], status="available" if item["records"] else "no_speech")
            shared = target["views"]["shared"]
            info = _shared_projection(data, root, target, inputs, directories)
            shared["status"] = info["status"]
            if info["status"] == "available":
                relative = info["relative"]
                item = common.describe_file(root, relative)
                if (item["identity"]["dev"], item["identity"]["ino"]) != (info["identity"]["dev"], info["identity"]["ino"]):
                    raise common.MaterialError("material_changed")
                item.update(records=info["records"], view="shared", tag=target["tag"], date=target["date"],
                    platform=target["platform"], group_id=target["id"], format="shared_projection",
                    source=dict(target["source"], time_basis="stored_record_timestamp_unverified"), redaction=dict(_SHARED_REDACTION))
                files.append(item)
                shared.update(available=True, records=item["records"], status="available")
            counts = target["completeness"]["counts"]
            facts = target["completeness"]["segments"]
            counts.update(canonical_records=view["records"], segment_associations=len(facts), shared_records=shared["records"])
            for fact in facts:
                if fact["state"] in {"current_speech", "current_no_speech", "cleaned_speech", "cleaned_no_speech"}:
                    counts["current_success"] += 1
                elif fact["state"] in counts:
                    counts[fact["state"]] += 1
                counts["no_speech"] += fact["outcome"] == "no_speech" and fact["history_usable"]
                counts["audio_cleaned"] += fact["audio_state"] == "cleaned"
            partial = any(fact["state"] not in {"current_speech", "current_no_speech", "cleaned_speech", "cleaned_no_speech"} for fact in facts)
            target["completeness"]["state"] = "partial" if partial else "current_at_build" if facts else "unknown"
            target["source"]["data_refs"].sort()
            for item in files:
                if item["group_id"] == target["id"]:
                    item["completeness"] = dict(target["completeness"], segments=[])
        catalog = None
        if any(item["view"] == "shared" for item in files):
            with redirect_stdout(io.StringIO()):
                build_index.build(root / "exports", root / "catalog.jsonl")
            catalog_path = root / "catalog.jsonl"
            catalog_identity = common.identity(catalog_path)
            descriptor = os.open(catalog_path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0))
            try:
                _output_identity(descriptor, catalog_path, catalog_identity)
                os.fsync(descriptor)
                _output_identity(descriptor, catalog_path, catalog_identity)
            finally:
                os.close(descriptor)
            catalog = common.describe_file(root, "catalog.jsonl")
            if catalog["identity"] != catalog_identity:
                raise common.MaterialError("material_changed")
            catalog["records"] = sum(item["view"] == "shared" for item in files)
        inputs.finish()
        check_scope()
        if inputs.appended:
            for target in groups.values():
                target["completeness"]["state"] = "partial"
            for item in files:
                item["completeness"]["state"] = "partial"
        return {"groups": sorted(groups.values(), key=lambda item: (item["date"], item["platform"], item["tag"])),
            "files": sorted(files, key=lambda item: (item["view"], item["date"], item["platform"], item["tag"], item["path"])),
            "catalog": catalog,
            "discovery": {"files_checked": len(inputs.files), "directory_entries_checked": inputs.entries, "segments_found": segment_total,
                "records_examined": inputs.records, "budgeted_input_bytes": inputs.bytes, "inputs_nonatomic": True,
                "append_observed": inputs.appended, "unplaced_segments": unplaced,
                "limits": {"files": MAX_INPUT_FILES, "entries": MAX_DISCOVERY_ENTRIES, "bytes": MAX_INPUT_BYTES, "records": MAX_INPUT_RECORDS, "groups": MAX_GROUPS},
                "no_records_does_not_mean_offline": True}}
    except common.MaterialError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise common.MaterialError("material_build_failed") from error
    finally:
        try:
            canonical.close()
        finally:
            directories.close()
