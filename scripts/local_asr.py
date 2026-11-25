"""One application-owned ASR lane; manager-side metadata and resource control only."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import threading
import time

from backfill_tasks import process_identity
from local_process import same_identity
from scripts import audio_segments as audio
from scripts import asr_protocol as lifecycle_protocol

_COUNTS = ("pending", "projection_pending", "retryable", "blocked", "complete")
_WORKER_STATES = {"starting", "identity_unknown", "idle", "scanning", "busy", "processing", "backoff", "dependency_error", "stopping", "storage_error"}
_PHASES = {"idle", "scan", "scanning", "checking", "waiting", "writer_busy", "loading_model", "model_loading",
           "recognizing", "transcribing", "processing", "committing", "projecting", "projection", "backoff", "stopping", "cleaning"}
_ERRORS = {"", "busy", "writer_busy", "worker_error", "dependency_error", "dependency_missing", "model_unavailable",
    "model_load_failed", "retry_state_unavailable", "retry_state_invalid", "retry_exhausted", "retry_capacity",
    "storage_error", "source_missing", "source_changed", "source_unconfirmed", "linked_path", "invalid_file",
    "invalid_closure", "invalid_segment", "invalid_segment_name", "invalid_folder", "outside_data", "too_small",
    "legacy_conflict", "result_missing", "result_invalid", "result_generation_missing", "record_invalid",
    "attempt_missing", "attempt_invalid", "attempt_failed", "attempt_pending", "attempt_lineage_mismatch",
    "predecessors_invalid", "decode_failed", "decode_unconfirmed", "transcription_failed", "projection_pending",
    "projection_failed", "projection_invalid", "projection_unverified", "result_too_large", "output_changed",
    "commit_unverified", "unsafe_path", "invalid_json", "state_too_large", "state_changed", "admission_unavailable",
    "status_unavailable", "scan_unavailable", "asr_failed", "evidence_blocked", "budget_exhausted"}
_CONDITIONS = {"periodic_scan", "writer_release", "after_backoff", "explicit_retry_or_dependency_change",
               "repair_evidence", "waiting_admission", "stop_after_current", "none"}
_STORAGE_COUNTS = ("audio_files", "transcription_pending", "cleanup_pending", "cleanup_failed", "cleanup_blocked")
_CLEANUP_REASONS = _ERRORS | {"audio_deleted", "cleanup_platform_unsupported", "cleanup_record_invalid",
    "cleanup_binding_conflict", "cleanup_handle_unconfirmed", "cleanup_accept_unconfirmed", "cleanup_commit_unconfirmed",
    "cleanup_cancel_unconfirmed", "cleanup_delete_unconfirmed", "cleanup_delete_failed", "cleanup_unconfirmed",
    "cleanup_retry_exhausted", "cleanup_capacity"}
_DISK_ERRORS = {"", "disk_unconfirmed", "disk_unavailable", "disk_sample_invalid", "disk_scope_changed"}
_LOW_SPACE_BYTES = 1073741824


def _unsigned(value, maximum=2 ** 53 - 1):
    return value if type(value) is int and 0 <= value <= maximum else None


def _enum(value, allowed, fallback):
    return value if isinstance(value, str) and value in allowed else fallback


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _stamp(value, wall, *, future=2):
    return value if _number(value) and 0 < value <= wall + future else None


def _storage_summary(raw, wall):
    raw = raw if isinstance(raw, dict) and type(raw.get("schema")) is int and raw["schema"] == 1 else {}
    scan = raw.get("scan") if isinstance(raw.get("scan"), dict) else {}
    disk = raw.get("disk") if isinstance(raw.get("disk"), dict) else {}
    counts = {key: _unsigned(raw.get(key), 1000000000) for key in _STORAGE_COUNTS}
    snapshot_at, updated = _stamp(raw.get("snapshot_at"), wall, future=0), _stamp(scan.get("updated_at"), wall, future=0)
    started, complete = _stamp(scan.get("started_at"), wall, future=0), _stamp(scan.get("last_complete_at"), wall, future=0)
    if started is not None and updated is not None and started > updated:
        started = None
    if complete is not None and updated is not None and complete > updated:
        complete = None
    free, total = _unsigned(disk.get("free_bytes")), _unsigned(disk.get("total_bytes"))
    error = _enum(disk.get("error"), _DISK_ERRORS, "disk_unavailable")
    sampled = _stamp(disk.get("sampled_at"), wall, future=0)
    if free is None or total is None or total == 0 or free > total:
        free = total = None
        error = error or "disk_sample_invalid"
    last = raw.get("last_cleanup")
    safe_last = None
    if (isinstance(last, dict) and _enum(last.get("state"), {"deleted", "pending", "kept", "busy"}, None) is not None
            and _stamp(last.get("at"), wall, future=0) is not None):
        current = _summary_current(last)
        safe_last = {"at": last["at"], **{key: current[key] for key in ("tag", "date", "segment")},
            "state": last["state"], "reason": _enum(last.get("reason"), _CLEANUP_REASONS, "cleanup_unconfirmed")}
    return {"schema": 1, "verified": False, "stale": True, "snapshot_at": snapshot_at,
        "audio_bytes": _unsigned(raw.get("audio_bytes")), **counts,
        "scan": {"partial": scan.get("partial") is not False or snapshot_at is None or updated != snapshot_at
                            or started is None or scan.get("limited") is True or _unsigned(scan.get("checked"), 1000000000) is None
                            or any(value is None for value in counts.values()) or _unsigned(raw.get("audio_bytes")) is None,
                 "limited": scan.get("limited") is True, "checked": _unsigned(scan.get("checked"), 1000000000),
                 "started_at": started, "updated_at": updated, "last_complete_at": complete,
                 "approximate": True, "counts_scope": "scan_round"},
        "disk": {"free_bytes": free, "total_bytes": total, "threshold_bytes": _LOW_SPACE_BYTES,
                 "low_space": free < _LOW_SPACE_BYTES if free is not None and sampled is not None and not error else None,
                 "sampled_at": sampled, "error": error, "verified": False, "stale": True}, "last_cleanup": safe_last}


def _summary_current(current):
    safe = {"tag": "", "date": "", "segment": "", "phase": "", "attempt": None}
    tag = current.get("tag")
    if isinstance(tag, str) and len(tag) <= 40 and not any(c in '/\\:*?"<>|' or ord(c) < 32 for c in tag):
        safe["tag"] = tag
    if isinstance(current.get("date"), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", current["date"]):
        safe["date"] = current["date"]
    if isinstance(current.get("segment"), str) and re.fullmatch(r"seg_[0-9]{4,}(?:\.flv)?", current["segment"]):
        safe["segment"] = current["segment"][:64]
    if isinstance(current.get("phase"), str) and current["phase"] in _PHASES:
        safe["phase"] = current["phase"]
    if type(current.get("attempt")) is int and 0 <= current["attempt"] <= 1000000:
        safe["attempt"] = current["attempt"]
    return safe


def _summary(raw, wall):
    raw = raw if isinstance(raw, dict) else {}
    counts = raw.get("counts") if isinstance(raw.get("counts"), dict) else {}
    scan = raw.get("scan") if isinstance(raw.get("scan"), dict) else {}
    current = raw.get("current") if isinstance(raw.get("current"), dict) else {}
    safe_current = {"tag": "", "date": "", "segment": "", "phase": "", "attempt": None}
    tag = current.get("tag")
    if isinstance(tag, str) and len(tag) <= 40 and not any(c in '/\\:*?"<>|' or ord(c) < 32 for c in tag):
        safe_current["tag"] = tag
    if isinstance(current.get("date"), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", current["date"]):
        safe_current["date"] = current["date"]
    if isinstance(current.get("segment"), str) and re.fullmatch(r"seg_[0-9]{4,}(?:\.flv)?", current["segment"]):
        safe_current["segment"] = current["segment"][:64]
    if isinstance(current.get("phase"), str) and current["phase"] in _PHASES:
        safe_current["phase"] = current["phase"]
    if type(current.get("attempt")) is int and 0 <= current["attempt"] <= 1000000:
        safe_current["attempt"] = current["attempt"]
    return {"state": _enum(raw.get("state"), _WORKER_STATES, "snapshot_unconfirmed"),
        "counts": {key: counts.get(key) if type(counts.get(key)) is int and 0 <= counts[key] <= 1000000000 else None for key in _COUNTS},
        "scan": {"partial": scan.get("partial") is not False, "limited": scan.get("limited") is True,
                 "checked": scan.get("checked") if type(scan.get("checked")) is int and 0 <= scan["checked"] <= 1000000000 else 0,
                 "last_complete_at": _stamp(scan.get("last_complete_at"), wall),
                 "counts_scope": "recent_window", "sample_limit": 256},
        "current": safe_current, "error": _enum(raw.get("error"), _ERRORS, "worker_error"),
        "next_condition": _enum(raw.get("next_condition"), _CONDITIONS, "periodic_scan"),
        "next_retry_at": _stamp(raw.get("next_retry_at"), wall, future=3600), "last_progress_at": _stamp(raw.get("last_progress_at"), wall),
        "last_completed_at": _stamp(raw.get("last_completed_at"), wall), "storage": _storage_summary(raw.get("storage"), wall)}


class AsrLane:
    SNAPSHOT_TTL = 15.0
    NO_PROGRESS_SECONDS = 120.0
    START_LIMIT = 3

    def __init__(self, manager, *, identity=process_identity, wall_time=time.time, protocol_api=None, boot_identity=None):
        if protocol_api is None:
            from scripts import asr_protocol as protocol_api
        self.manager, self.context = manager, manager.context
        self.protocol, self.identity, self.wall_time = protocol_api, identity, wall_time
        try:
            boot = (boot_identity or getattr(protocol_api, "boot_identity", lambda: None))()
        except (OSError, ValueError):
            boot = None
        self.boot = copy.deepcopy(boot) if lifecycle_protocol.valid_boot_identity(boot) else None
        self.paths = {key: Path(value) for key, value in protocol_api.paths(self.context).items()}
        self._lock = threading.Lock()
        self._storage_lock = threading.Lock()
        self.owned = None
        self.parent = self.worker = None
        self.nonce = ""
        self._reset_storage()
        self.failures = 0
        self.retry_at = 0.0
        self.started_at = 0.0
        self.stop_requested = False
        self.stop_written = False
        self.may_admitted = False
        self.admitted = False
        self.control_sequence = 0
        self.status_sequence = 0
        self._last_status = None
        self._seen_at = 0.0
        self._initial_age = 0.0
        self._progress_at = 0.0
        self._progress_marker = None
        self._stop_retry = 0.0
        self._unowned_hold = False
        self._prior_records = None
        self._release_record = None
        self._reservation = None
        self._prior_release = None
        self._launch_records = []
        self._create_entered = False
        self._no_spawn_pending = False
        self._no_spawn_record = None
        self._owned_published = False
        self._admission_published = False
        self._worker_summary = _summary({}, wall_time())
        self.scope = None
        self.state, self.error = "not_started", ""
        try:
            self.scope = protocol_api.ensure(self.context)
        except (OSError, ValueError):
            self.state, self.error = "storage_error", "runtime_unavailable"
        self._cache = {}
        self._publish()

    def _binding(self, kind):
        return self.protocol.envelope(self.context, self.nonce, self.parent, self.worker, kind, scope=self.scope)

    def _reset_storage(self):
        generation = hashlib.sha256(self.nonce.encode()).hexdigest()[:24] if self.nonce else ""
        with self._storage_lock:
            if generation == getattr(self, "_storage_generation", None):
                return
            self._storage_generation = generation
            self._storage_invalid_through = {"stats": 0, "disk": 0}
        self._storage_observations = {}
        self._storage_receipt = {}

    def _storage_watermarks(self, generation, *, future=(), sequence=0):
        # Only generation and two in-memory integers share this short lock.
        # Never acquire the lane/tick lock or perform I/O while holding it.
        with self._storage_lock:
            if generation != self._storage_generation:
                return None
            for kind in future:
                self._storage_invalid_through[kind] = max(self._storage_invalid_through[kind], sequence)
            return dict(self._storage_invalid_through)

    def _receive_storage(self, raw, wall, envelope_at):
        generation = self._storage_generation
        raw = raw if isinstance(raw, dict) and type(raw.get("schema")) is int and raw["schema"] == 1 else {}
        safe = _storage_summary(raw, wall)
        scan = raw.get("scan") if isinstance(raw.get("scan"), dict) else {}
        disk = raw.get("disk") if isinstance(raw.get("disk"), dict) else {}
        last = raw.get("last_cleanup") if isinstance(raw.get("last_cleanup"), dict) else {}
        stamps = {"stats": [raw.get("snapshot_at"), scan.get("started_at"), scan.get("updated_at"),
                            scan.get("last_complete_at"), last.get("at")], "disk": [disk.get("sampled_at")]}
        valid = {"stats": safe["snapshot_at"] is not None and safe["scan"]["updated_at"] == safe["snapshot_at"],
                 "disk": safe["disk"]["sampled_at"] is not None and safe["disk"]["free_bytes"] is not None and not safe["disk"]["error"]}
        receipt, rejected = {}, []
        for kind in ("stats", "disk"):
            marker = raw.get("snapshot_at") if kind == "stats" else disk.get("sampled_at")
            marker = marker if _number(marker) and marker > 0 else None
            old = self._storage_observations.get(kind, {})
            if marker is not None and marker != old.get("marker"):
                old = {"marker": marker, "seen": self.manager.now(), "age": max(0.0, wall - marker)}
                self._storage_observations[kind] = old
            future = envelope_at > wall or any(_number(stamp) and stamp > wall for stamp in stamps[kind])
            if future:
                rejected.append(kind)
            receipt[kind] = dict(old, valid=bool(valid[kind]) and not future,
                stamps=tuple(stamp for stamp in stamps[kind] if _number(stamp) and stamp > 0))
        self._storage_watermarks(generation, future=rejected, sequence=self.status_sequence)
        self._storage_receipt = receipt
        return safe

    def _snapshot_storage(self, result, receipt):
        storage = result["storage"]
        wall, now = self.wall_time(), self.manager.now()
        futures = {kind: (result["snapshot_at"] is not None and result["snapshot_at"] > wall
                         or any(stamp > wall for stamp in receipt.get(kind, {}).get("stamps", ())))
                   for kind in ("stats", "disk")}
        invalid_through = self._storage_watermarks(result["generation"],
            future=tuple(kind for kind in futures if futures[kind]), sequence=result["sequence"])
        parent_current = (result["verified"] and not result["stale"] and invalid_through is not None
            and result["state"] in {"idle", "scanning", "busy", "processing", "backoff", "dependency_error"})
        for kind, target, ttl in (("stats", storage, 15.0), ("disk", storage["disk"], 60.0)):
            sample = receipt.get(kind, {})
            future = futures[kind]
            age = sample.get("age", 0) + max(0.0, now - sample.get("seen", now))
            current = (parent_current and sample.get("valid") is True and not future
                       and result["sequence"] > invalid_through[kind] and age <= ttl)
            target.update(verified=bool(current), stale=not bool(current))
            if kind == "disk" and not current:
                target["low_space"] = None

    def _publish(self):
        self._cache = dict(copy.deepcopy(self._worker_summary), state=self.state, error=self.error or self._worker_summary["error"],
            resource_held=self.owned is not None or self._unowned_hold, verified=self.admitted and self.state == "running",
            stale=self.state != "running" or not bool(self._last_status), no_progress=False,
            snapshot_at=self._last_status.get("updated_at") if self._last_status else None,
            sequence=self.status_sequence,
            generation=hashlib.sha256(self.nonce.encode()).hexdigest()[:24] if self.nonce else "",
            _worker_state=self._worker_summary["state"], _seen_at=self._seen_at, _initial_age=self._initial_age,
            _progress_at=self._progress_at, _storage_receipt=copy.deepcopy(self._storage_receipt))
        if self.state in ("starting", "not_started"):
            self._cache["next_condition"] = "waiting_admission"
        elif self.state in ("identity_unknown", "admission_unknown", "snapshot_unconfirmed"):
            self._cache["next_condition"] = "repair_evidence"
        elif self.state == "restart_limited":
            self._cache["next_condition"] = "explicit_retry_or_dependency_change"
        elif self.state in ("backoff", "failed"):
            self._cache["next_condition"] = "after_backoff"
        elif self.state in ("stopping", "draining"):
            self._cache["next_condition"] = "stop_after_current"
        elif self.state == "stopped":
            self._cache["next_condition"] = "none"

    def snapshot(self):
        # Never wait for metadata I/O, a scanner or a native model call.
        result = copy.deepcopy(self._cache)
        storage_receipt = result.pop("_storage_receipt")
        state = result.pop("_worker_state")
        seen, initial = result.pop("_seen_at"), result.pop("_initial_age")
        progress = result.pop("_progress_at")
        if result["verified"]:
            age = initial + max(0.0, self.manager.now() - seen)
            result["stale"] = not result["snapshot_at"] or age > self.SNAPSHOT_TTL
            if result["stale"]:
                result.update(state="snapshot_unconfirmed", verified=False)
            else:
                result["state"] = state
            result["no_progress"] = state == "processing" and self.manager.now() - progress >= self.NO_PROGRESS_SECONDS
        if self.state in ("failed", "backoff", "restart_limited") and self.retry_at:
            result["next_retry_at"] = self.wall_time() + max(0.0, self.retry_at - self.manager.now())
        if self.stop_requested or self.manager.stopping:
            if result["state"] not in ("identity_unknown", "admission_unknown"):
                result["state"] = "stopping" if result["resource_held"] else "stopped"
                result["next_condition"] = "stop_after_current" if result["resource_held"] else "none"
            result.update(verified=False, stale=True)
        self._snapshot_storage(result, storage_receipt)
        return result

    def _count(self):
        try:
            return self.owned.active_count()
        except Exception:
            return None

    def _release_empty(self):
        try:
            if not self.owned.close():
                return False
            if not self.scope_current() or not self._fresh_parent():
                return False
            if self._release_record is None:
                records = self._records()
                fingerprints = self._fingerprints(records)
                for key, value in records.items():
                    if (value is not None and value["nonce"] != self.nonce
                            and fingerprints[key] != (self._prior_records or {}).get(key)):
                        raise ValueError("changed boot evidence")
                    if value is not None and value["nonce"] == self.nonce:
                        claim = dict(value, state="alive") if key == "ready" else value["worker"]
                        if (self.worker is not None and not same_identity(claim, self.worker)
                                or key != "ready" and not same_identity(value["parent"], self.parent)):
                            raise ValueError("wrong boot binding")
                root = self.owned.identity
                if not same_identity(root, root):
                    raise ValueError("root identity unavailable")
                identities = [root] + ([self.worker] if self.worker is not None else [])
                for key, value in records.items():
                    if value is not None:
                        identities.append(dict(value, state="alive") if key == "ready" else value["worker"])
                if not all(self._identity_ended(identity) for identity in identities):
                    raise ValueError("old identity unresolved")
                self._release_record = {"schema": 2, "kind": "asr_release", "role": "asr:global",
                    "instance_id": self.context["instance_id"], "scope": self.scope,
                    "nonce": self.nonce, "parent": self.parent, "root": root, "worker": self.worker,
                    "empty": True, "records": fingerprints, "updated_at": self.wall_time(),
                    "phase": "released", "proof": "job_empty", "boot": self.boot}
            self.protocol.write_record(self.paths["release"], self._release_record)
            if (not self.scope_current() or not self._fresh_parent()
                    or self.protocol.read_record(self.paths["release"]) != self._release_record
                    or self._fingerprints(self._records()) != self._release_record["records"]):
                return False
            return True
        except (OSError, ValueError):
            return False

    def _records(self):
        records = {key: self.protocol.read_record(self.paths[key]) for key in ("ready", "status", "admission")}
        for key, value in records.items():
            if value is None:
                continue
            if (type(value.get("schema")) is not int or value["schema"] != 1
                    or value.get("instance_id") != self.context["instance_id"] or value.get("role") != "asr:global"
                    or not isinstance(value.get("nonce"), str) or not 0 < len(value["nonce"]) <= 128):
                raise ValueError("invalid boot evidence")
            if key == "ready":
                identity = dict(value, state="alive")
                if not same_identity(identity, identity) or _stamp(value.get("updated_at"), self.wall_time(), future=math.inf) is None:
                    raise ValueError("invalid ready identity")
            elif (value.get("kind") != "asr_" + key or value.get("scope") != self.scope
                    or not same_identity(value.get("parent"), value.get("parent"))
                    or not same_identity(value.get("worker"), value.get("worker"))):
                raise ValueError("invalid boot envelope")
            if key == "status" and (type(value.get("sequence")) is not int or not 1 <= value["sequence"] <= 2 ** 53 - 1
                    or _stamp(value.get("updated_at"), self.wall_time(), future=math.inf) is None or not isinstance(value.get("summary"), dict)):
                raise ValueError("invalid boot status")
        return records

    @staticmethod
    def _fingerprints(records):
        return {key: hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest() if value is not None else None
                for key, value in records.items()}

    def _identity_ended(self, identity):
        actual = self.identity(identity["pid"])
        return (isinstance(actual, dict) and (actual.get("state") == "dead"
            or same_identity(actual, actual) and not same_identity(identity, actual)))

    def _resource_write(self, record):
        if not self.scope_current() or not self._fresh_parent():
            raise ValueError("resource owner unavailable")
        self.protocol.write_record(self.paths["release"], record)
        if (not self.scope_current() or not self._fresh_parent()
                or self.protocol.read_record(self.paths["release"]) != record):
            raise ValueError("resource readback unconfirmed")

    def _phase(self, phase):
        record = dict(self._reservation, phase=phase, updated_at=self.wall_time(),
                      root=self.owned.identity if self.owned is not None else None,
                      worker=self.worker)
        self._launch_records.append(record)
        self._launch_records = self._launch_records[-3:]
        self._resource_write(record)
        self._reservation = record

    def _settle_no_spawn(self):
        """Only this control flow knows creation was never entered."""
        if not self._no_spawn_pending or self._create_entered or self.owned is not None:
            return False
        try:
            current = self.protocol.read_record(self.paths["release"])
            if current != self._prior_release and current not in self._launch_records and current != self._no_spawn_record:
                raise ValueError("resource evidence changed")
            fingerprints = self._fingerprints(self._records())
            if fingerprints != self._prior_records:
                raise ValueError("boot records changed")
            if self._no_spawn_record is None:
                self._no_spawn_record = dict(self._reservation, phase="released", proof="no_spawn", empty=True,
                    root=None, worker=None, records=fingerprints, updated_at=self.wall_time())
            record = self._no_spawn_record
            self._resource_write(record)
            self._prior_release = record
            self._no_spawn_pending = self._unowned_hold = False
            self.failures += 1
            self.retry_at = self.manager.now() + min(30, 2 ** self.failures)
            self.state, self.error = "restart_limited" if self.failures >= self.START_LIMIT else "failed", "resource_reservation_unconfirmed"
            return True
        except (OSError, ValueError):
            self._unowned_hold = True
            self.state, self.error = "identity_unknown", "resource_reservation_unconfirmed"
            return False

    def _lifecycle_records_match(self, release, records, fingerprints):
        if not isinstance(release.get("records"), dict) or set(release["records"]) != {"ready", "status", "admission"}:
            return False
        for key, value in records.items():
            if fingerprints[key] == release["records"][key]:
                continue
            if value is None or value["nonce"] != release["nonce"]:
                return False
            claim = dict(value, state="alive") if key == "ready" else value["worker"]
            if (key != "ready" and not same_identity(value["parent"], release["parent"])
                    or release.get("worker") is not None and not same_identity(claim, release["worker"])
                    or key == "admission" and release.get("phase") != "admission"):
                return False
        return True

    def _old_resources_clear(self):
        """PID files cannot prove an abandoned private Job was empty."""
        try:
            if not self.scope_current():
                raise ValueError("data scope changed")
            release = self.protocol.read_record(self.paths["release"])
            advanced = isinstance(release, dict) and lifecycle_protocol.boot_advanced(release.get("boot"), self.boot)
            records = self._records()
            fingerprints = self._fingerprints(records)
            if release is None and all(value is None for value in records.values()):
                self._prior_records, self._prior_release, self._unowned_hold = fingerprints, None, False
                return True
            required = {"schema", "kind", "role", "instance_id", "scope", "nonce", "parent", "root", "worker", "empty", "records", "updated_at"}
            if (not isinstance(release, dict) or not required <= set(release)
                    or type(release.get("schema")) is not int or release["schema"] not in (1, 2)
                    or release.get("kind") != "asr_release" or release.get("role") != "asr:global"
                    or release.get("instance_id") != self.context["instance_id"] or release.get("scope") != self.scope
                    or not isinstance(release.get("nonce"), str) or not 0 < len(release["nonce"]) <= 128
                    or not same_identity(release.get("parent"), release.get("parent"))
                    or release.get("root") is not None and not same_identity(release["root"], release["root"])
                    or release.get("worker") is not None and not same_identity(release["worker"], release["worker"])
                    or not isinstance(release.get("records"), dict) or set(release["records"]) != {"ready", "status", "admission"}
                    or any(value is not None and (not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None)
                           for value in release["records"].values())
                    or _stamp(release.get("updated_at"), self.wall_time(), future=math.inf) is None):
                raise ValueError("release unconfirmed")
            version = release["schema"]
            phase, proof = release.get("phase"), release.get("proof")
            if version == 2:
                if (not {"phase", "proof", "boot"} <= set(release)
                        or release["boot"] is not None and not lifecycle_protocol.valid_boot_identity(release["boot"])
                        or not isinstance(proof, str)
                        or not isinstance(phase, str) or phase not in {"reserved", "creating", "owned", "admission", "released"}
                        or phase == "released" and release.get("empty") is not True
                        or phase != "released" and release.get("empty") is not False
                        or phase in {"reserved", "creating"} and (release.get("root") is not None or release.get("worker") is not None)
                        or phase in {"owned", "admission"} and not same_identity(release.get("root"), release.get("root"))
                        or phase == "admission" and not same_identity(release.get("worker"), release.get("worker"))):
                    raise ValueError("invalid resource phase")
                if phase != "released":
                    no_spawn = (phase == "reserved" and self._identity_ended(release["parent"])
                                and release.get("records") == fingerprints)
                    if not no_spawn and not (advanced and self._lifecycle_records_match(release, records, fingerprints)):
                        raise ValueError("unreleased resource")
                    parent = self.identity(os.getpid())
                    if not same_identity(parent, parent):
                        raise ValueError("recovery owner unavailable")
                    self.parent = {key: parent[key] for key in ("state", "pid", "created")}
                    release = dict(release, phase="released", empty=True, proof="no_spawn" if no_spawn else "boot_advanced",
                                   released_boot=self.boot, released_by=self.parent, records=fingerprints, updated_at=self.wall_time())
                    self._resource_write(release)
                    phase, proof = release["phase"], release["proof"]
                if proof == "no_spawn":
                    if release.get("root") is not None or release.get("worker") is not None:
                        raise ValueError("invalid no-spawn proof")
                elif proof == "boot_advanced":
                    if (not lifecycle_protocol.boot_advanced(release.get("boot"), release.get("released_boot"))
                            or not lifecycle_protocol.valid_boot_identity(self.boot)
                            or self.boot["installation"] != release["released_boot"]["installation"]
                            or self.boot["counter"] < release["released_boot"]["counter"]):
                        raise ValueError("invalid new-boot proof")
                elif proof != "job_empty" or not same_identity(release.get("root"), release.get("root")):
                    raise ValueError("invalid empty proof")
            elif release.get("empty") is not True or not same_identity(release.get("root"), release.get("root")):
                raise ValueError("legacy release unconfirmed")
            if release.get("records") != fingerprints:
                raise ValueError("changed released records")
            identities = [release["root"]] if release.get("root") is not None else []
            if release["worker"] is not None:
                identities.append(release["worker"])
            for key, value in records.items():
                if value is not None:
                    claim = dict(value, state="alive") if key == "ready" else value["worker"]
                    if same_identity(claim, release["worker"]) and (value["nonce"] != release["nonce"]
                            or key != "ready" and not same_identity(value["parent"], release["parent"])):
                        raise ValueError("release boot mismatch")
                    identities.append(claim)
            if proof != "boot_advanced" and not advanced and not all(self._identity_ended(identity) for identity in identities):
                raise ValueError("previous identity unresolved")
            self._prior_records, self._prior_release, self._unowned_hold = fingerprints, release, False
            return True
        except (OSError, ValueError):
            self._unowned_hold = True
            self.state, self.error = "identity_unknown", "previous_resource_unconfirmed"
            return False

    def _fresh_parent(self):
        return self.parent is not None and same_identity(self.parent, self.identity(os.getpid()))

    def scope_current(self):
        try:
            return self.scope is not None and audio.data_scope(self.context["data"]) == self.scope
        except (OSError, ValueError):
            return False

    def _fresh_worker(self):
        return self.worker is not None and same_identity(self.worker, self.identity(self.worker["pid"]))

    def _start(self):
        if not self._old_resources_clear():
            return
        parent = self.identity(os.getpid())
        if not same_identity(parent, parent):
            self.state, self.error = "identity_unknown", "parent_identity_unavailable"
            return
        self.parent = {key: parent[key] for key in ("state", "pid", "created")}
        self.nonce = secrets.token_hex(24)
        self._reset_storage()
        # New launch generations never inherit a previous worker's counters or
        # timestamps, including when the next Popen itself fails.
        self.worker = None
        self.started_at = self.manager.now()
        self.stop_written = self.may_admitted = self.admitted = False
        self.control_sequence = self.status_sequence = 0
        self._last_status = None
        self._seen_at = self._initial_age = self._progress_at = 0.0
        self._progress_marker = None
        self._release_record = None
        self._launch_records = []
        self._create_entered = False
        self._no_spawn_pending = False
        self._no_spawn_record = None
        self._owned_published = self._admission_published = False
        self._worker_summary = _summary({}, self.wall_time())
        env = self.manager.child_env()
        env.update(DY_CUSTOMER_ROLE_NONCE=self.nonce, DY_ASR_PARENT_PID=str(parent["pid"]),
                   DY_ASR_PARENT_CREATED=str(parent["created"]), DY_CUSTOMER_STOP=str(self.paths["stop"]),
                   DY_ASR_SCOPE=json.dumps(self.scope, ensure_ascii=False, separators=(",", ":")),
                   PYTHONIOENCODING="utf-8")
        if not self.scope_current():
            self.state, self.error = "identity_unknown", "data_scope_changed"
            return
        self._reservation = {"schema": 2, "kind": "asr_release", "role": "asr:global",
            "instance_id": self.context["instance_id"], "scope": self.scope, "nonce": self.nonce,
            "parent": self.parent, "root": None, "worker": None, "empty": False,
            "records": self._prior_records, "updated_at": self.wall_time(), "phase": "reserved", "proof": "", "boot": self.boot}
        self._unowned_hold = True
        self._no_spawn_pending = True
        try:
            self._phase("reserved")
            self._phase("creating")
        except (OSError, ValueError):
            self.state, self.error = "identity_unknown", "resource_reservation_unconfirmed"
            return
        self._create_entered, self._no_spawn_pending = True, False
        try:
            self.owned = self.manager.spawn([self.context["python"], "-B", str(Path(self.context["root"]) / "scripts/asr_worker.py")],
                env=env, cwd=self.context["root"], log_path=self.manager.directory / "logs/asr.log")
            if (self.owned is None or not same_identity(getattr(self.owned, "identity", None), getattr(self.owned, "identity", None))
                    or not callable(getattr(self.owned, "active_count", None)) or not callable(getattr(self.owned, "close", None))):
                self.owned = None
                raise ValueError("owned process unavailable")
        except Exception:
            self.failures += 1
            # An exception may follow a process creation without returning its
            # handle. Only a held Job can establish the later empty-tree fact.
            self.state, self.error = "identity_unknown", "worker_start_unconfirmed"
            return
        self._unowned_hold = False
        try:
            self._phase("owned")
            self._owned_published = True
        except (OSError, ValueError):
            self.state, self.error = "identity_unknown", "resource_reservation_unconfirmed"
            return
        self.state, self.error = "starting", ""

    def _read_ready(self):
        ready = self.protocol.read_record(self.paths["ready"])
        valid = (isinstance(ready, dict) and type(ready.get("schema")) is int and ready["schema"] == 1
                 and ready.get("instance_id") == self.context["instance_id"] and ready.get("role") == "asr:global"
                 and ready.get("nonce") == self.nonce)
        return self.owned.worker_identity(ready) if valid else None

    def _read_status(self):
        value = self.protocol.read_record(self.paths["status"])
        if value is None:
            return
        if not self.scope_current():
            raise ValueError("data scope changed")
        if not self.protocol.matches(value, self.context, self.nonce, self.parent, self.worker, "asr_status", scope=self.scope):
            raise ValueError("wrong status binding")
        sequence, updated = value.get("sequence"), value.get("updated_at")
        if type(sequence) is not int or not 1 <= sequence <= 2 ** 53 - 1 or _stamp(updated, self.wall_time()) is None:
            raise ValueError("invalid status")
        if sequence < self.status_sequence or sequence == self.status_sequence and value != self._last_status:
            raise ValueError("old status")
        if sequence == self.status_sequence:
            return
        summary = _summary(value.get("summary"), self.wall_time())
        self.status_sequence, self._last_status = sequence, value
        summary["storage"] = self._receive_storage(value.get("summary", {}).get("storage")
            if isinstance(value.get("summary"), dict) else None, self.wall_time(), updated)
        self._seen_at = self.manager.now()
        self._initial_age = max(0.0, self.wall_time() - updated)
        marker = summary["last_progress_at"]
        if marker != self._progress_marker:
            self._progress_marker = marker
            self._progress_at = self.manager.now() - max(0.0, self.wall_time() - marker) if marker else self.manager.now()
        self._worker_summary = summary

    def _stop(self):
        self.stop_requested = True
        self.state = "stopping"
        if not self.owned:
            self._old_resources_clear()
            return
        if not self.scope_current():
            self.state, self.error = "identity_unknown", "data_scope_changed"
            return
        if self.stop_written or self.manager.now() < self._stop_retry:
            return
        if not self._fresh_parent() or not self._fresh_worker():
            self.error = "stop_identity_unavailable"
            return
        if not self.scope_current():
            self.state, self.error = "identity_unknown", "data_scope_changed"
            return
        try:
            self.protocol.write_record(self.paths["stop"], dict(self._binding("asr_stop"), action="stop"))
            self.stop_written = True
        except (OSError, ValueError):
            self.error = "stop_request_unavailable"
            self._stop_retry = self.manager.now() + 2

    def tick(self):
        if not self._lock.acquire(blocking=False):
            return
        try:
            if self.owned is not None:
                count = self._count()
                if count is None:
                    self.state, self.error = "identity_unknown", "process_tree_state_unavailable"
                    return
                if count == 0:
                    if not self._release_empty():
                        self.state, self.error = "identity_unknown", "process_tree_close_unconfirmed"
                        return
                    self.owned = None
                    self.admitted = False
                    if self.stop_requested or self.manager.stopping:
                        self.state = "stopped"
                        return
                    self.failures += 1
                    self.retry_at = self.manager.now() + min(30, 2 ** self.failures)
                    self.state, self.error = "restart_limited" if self.failures >= self.START_LIMIT else "backoff", "worker_exited"
                    return
                if not self.scope_current():
                    self.state, self.error = "identity_unknown", "data_scope_changed"
                    return
                if self.stop_requested or self.manager.stopping:
                    self._stop()
                    return
                if self.owned.proc.poll() is not None:
                    self.state, self.error = "draining", "root_exited_descendants_held"
                    return
                if not self._fresh_parent():
                    self.state, self.error = "identity_unknown", "parent_identity_unavailable"
                    return
                if not self._owned_published:
                    try:
                        self._phase("owned")
                        self._owned_published = True
                    except (OSError, ValueError):
                        self.state, self.error = "identity_unknown", "resource_reservation_unconfirmed"
                        return
                try:
                    worker = self._read_ready()
                except (OSError, ValueError):
                    worker = None
                if worker is None:
                    unknown = self.worker or self.may_admitted or self.manager.now() - self.started_at > 20
                    self.state, self.error = "identity_unknown" if unknown else "starting", "worker_ack_unconfirmed"
                    return
                if self.worker is not None and not same_identity(self.worker, worker):
                    self.state, self.error = "identity_unknown", "worker_identity_unavailable"
                    return
                self.worker = {key: worker[key] for key in ("state", "pid", "created")}
                if not self.admitted:
                    if self.may_admitted:
                        self.state, self.error = "admission_unknown", "admission_unconfirmed"
                        return
                    if self.manager.stopping:
                        self._stop()
                        return
                    if not self.scope_current():
                        self.state, self.error = "identity_unknown", "data_scope_changed"
                        return
                    if not self._admission_published:
                        try:
                            self._phase("admission")
                            self._admission_published = True
                        except (OSError, ValueError):
                            self.state, self.error = "identity_unknown", "resource_reservation_unconfirmed"
                            return
                    self.may_admitted = True
                    admission = self._binding("asr_admission")
                    try:
                        self.protocol.write_record(self.paths["admission"], admission)
                        if self.protocol.read_record(self.paths["admission"]) != admission:
                            raise ValueError("admission readback unconfirmed")
                    except (OSError, ValueError):
                        self.state, self.error = "admission_unknown", "admission_unconfirmed"
                        return
                    self.admitted = True
                    self._progress_at = self.manager.now()
                else:
                    try:
                        if self.protocol.read_record(self.paths["admission"]) != self._binding("asr_admission"):
                            raise ValueError("admission continuity unconfirmed")
                    except (OSError, ValueError):
                        self.admitted = False
                        self.state, self.error = "admission_unknown", "admission_unconfirmed"
                        return
                self.state, self.error = "running", ""
                try:
                    self._read_status()
                except (OSError, ValueError):
                    if not self.scope_current():
                        self.state, self.error = "identity_unknown", "data_scope_changed"
                    else:
                        self.state, self.error = "snapshot_unconfirmed", "snapshot_unconfirmed"
                return
            if self._no_spawn_pending:
                self._settle_no_spawn()
                return
            if self.stop_requested or self.manager.stopping:
                if self._old_resources_clear():
                    self.state = "stopped"
                return
            if self.scope is None:
                self.state, self.error = "storage_error", "runtime_unavailable"
                return
            if not self.scope_current():
                self.state, self.error = "identity_unknown", "data_scope_changed"
                return
            if self.failures >= self.START_LIMIT:
                self.state = "restart_limited"
                return
            if self.manager.now() < self.retry_at:
                self.state = "backoff"
                return
            self._start()
        finally:
            self._publish()
            self._lock.release()

    def begin_stop(self):
        self.stop_requested = True
        if self._lock.acquire(blocking=False):
            try:
                self._stop()
            finally:
                self._publish()
                self._lock.release()

    def drained(self):
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if self.owned is None:
                if self._no_spawn_pending:
                    return self._settle_no_spawn()
                return self._old_resources_clear()
            if self._count() != 0:
                return False
            if not self._release_empty():
                self.state, self.error = "identity_unknown", "resource_release_unconfirmed"
                return False
            self.owned = None
            self.admitted = False
            self.state = "stopped"
            return True
        finally:
            self._publish()
            self._lock.release()

    def retry(self):
        with self._lock:
            if self.stop_requested or self.manager.stopping:
                return {"ok": False, "status": self.snapshot()}
            if self.scope is not None and not self.scope_current():
                self.state, self.error = "identity_unknown", "data_scope_changed"
                self._publish()
                return {"ok": False, "status": self.snapshot()}
            if self.owned is None:
                if self._no_spawn_pending and not self._settle_no_spawn():
                    self._publish()
                    return {"ok": False, "status": self.snapshot()}
                if self.scope is None:
                    try:
                        self.scope = self.protocol.ensure(self.context)
                    except (OSError, ValueError):
                        self.state, self.error = "storage_error", "runtime_unavailable"
                        self._publish()
                        return {"ok": False, "status": self.snapshot()}
                if not self._old_resources_clear():
                    self._publish()
                    return {"ok": False, "status": self.snapshot()}
                self.failures, self.retry_at = 0, 0.0
                self.state, self.error = "not_started", ""
                self._publish()
                return {"ok": True, "status": self.snapshot()}
            if (self.state != "running" or not self.admitted or self._count() in (None, 0)
                    or not self._fresh_parent() or not self._fresh_worker() or self.owned.proc.poll() is not None):
                return {"ok": False, "status": self.snapshot()}
            try:
                if not same_identity(self.worker, self._read_ready()):
                    return {"ok": False, "status": self.snapshot()}
                if self.protocol.read_record(self.paths["admission"]) != self._binding("asr_admission"):
                    self.admitted = False
                    self.state, self.error = "admission_unknown", "admission_unconfirmed"
                    self._publish()
                    return {"ok": False, "status": self.snapshot()}
            except (OSError, ValueError):
                return {"ok": False, "status": self.snapshot()}
            try:
                if not self.scope_current():
                    self.state, self.error = "identity_unknown", "data_scope_changed"
                    self._publish()
                    return {"ok": False, "status": self.snapshot()}
                self.control_sequence += 1
                self.protocol.write_record(self.paths["control"], dict(self._binding("asr_control"), action="retry", sequence=self.control_sequence))
            except (OSError, ValueError):
                self.error = "control_unconfirmed"
                self._publish()
                return {"ok": False, "status": self.snapshot()}
            self.failures, self.retry_at, self.error = 0, 0.0, ""
            self._publish()
            return {"ok": True, "status": self.snapshot()}
