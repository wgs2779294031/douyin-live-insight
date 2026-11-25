"""One admitted worker's bounded, serial discovery and retry loop.

Scheduling metadata is never success evidence. Audio and 7A facts are retained;
the worker only invokes the existing result/projection primitives under their
global writer lock. No model, process or network is created by this module.
"""
from __future__ import annotations

from collections import OrderedDict, deque
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

from scripts import audio_segments as audio
from scripts import transcript_results as results

SCAN_INTERVAL = 10.0
SCAN_ITEMS = 16
SCAN_SECONDS = .1
DAY_CURSORS = 16
DIRECTORY_LIMIT = 1024
SAMPLE_LIMIT = 256
RETRY_LIMIT = 1024
VERIFY_INTERVAL = 300.0
RETRY_DELAYS = (30.0, 120.0, 300.0)
ATTEMPT_LIMIT = 3
BUSY_INTERVAL = 2.0
_SEGMENT = re.compile(r"seg_([0-9]{4,})\.flv\Z")
_DAY = re.compile(r"audio_(.+)_([0-9]{4}-[0-9]{2}-[0-9]{2})\Z")
_SAFE = set("complete projection_pending result_missing attempt_pending attempt_failed attempt_missing "
    "attempt_invalid attempt_lineage_mismatch result_generation_missing predecessors_invalid source_missing "
    "source_changed source_unconfirmed invalid_closure invalid_segment invalid_segment_name linked_path "
    "unsafe_path invalid_file invalid_json invalid_state invalid_data outside_data invalid_folder "
    "record_invalid transcript_result_invalid transcript_attempt_invalid speech_projection_invalid "
    "result_invalid projection_invalid output_changed legacy_conflict too_small rows_invalid decode_unconfirmed "
    "commit_unverified result_too_large source_unconfirmed evidence_blocked asr_failed projection_failed "
    "model_unavailable decode_failed transcription_failed retry_exhausted retry_state_unavailable retry_capacity "
    "scan_unavailable admission_unavailable".split())
_CLEANUP_REASONS = _SAFE | set("audio_deleted cleanup_platform_unsupported cleanup_record_invalid "
    "cleanup_binding_conflict cleanup_handle_unconfirmed cleanup_accept_unconfirmed cleanup_commit_unconfirmed "
    "cleanup_cancel_unconfirmed cleanup_delete_unconfirmed cleanup_delete_failed cleanup_unconfirmed "
    "cleanup_retry_exhausted cleanup_capacity busy".split())


def _reason(value, fallback="evidence_blocked"):
    return value if isinstance(value, str) and value in _SAFE else fallback


def _finite(value, maximum=10 ** 15):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= maximum


def _sealed(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    return dict(value, checksum=hashlib.sha256(raw).hexdigest())


def _stat(path, *, links=False):
    try:
        value = path.lstat()
        token = [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_mode]
        return token + [value.st_nlink] if links else token
    except FileNotFoundError:
        return None
    except OSError:
        return "unavailable"


class _DayCursor:
    def __init__(self, folder):
        self.folder, self.phase, self.iterator = folder, 0, None
        self.yielded = False
        self.position, self.skipping = 0, 0
        self.replaying = False

    def close(self):
        if self.iterator is not None:
            self.iterator.close()
            self.iterator = None

    def next(self, limit=32, *, deadline=None, now=time.monotonic):
        # Primary closure facts, unconfirmed raw sources, then result-only
        # orphans. Existence guards avoid an unbounded de-duplication set.
        roots = [self.folder / ".segments", self.folder, self.folder / ".results"]
        audio.safe_folder(self.folder.parent, self.folder)
        inspected = 0
        while self.phase < len(roots) and inspected < limit:
            if deadline is not None and now() >= deadline:
                return None
            if self.iterator is None:
                try:
                    self.iterator = os.scandir(roots[self.phase])
                    self.skipping = self.position
                    self.replaying = bool(self.skipping)
                except FileNotFoundError:
                    self.phase += 1
                    self.position = 0
                    continue
            try:
                entry = next(self.iterator)
            except StopIteration:
                self.close()
                self.phase += 1
                self.position, self.skipping = 0, 0
                self.replaying = False
                continue
            name = entry.name
            inspected += 1
            if self.skipping:
                self.skipping -= 1
                continue
            self.replaying = False
            self.position += 1
            if self.phase == 0 and name.startswith("seg_") and name.endswith(".json"):
                self.yielded = True
                return name[:-5] + ".flv"
            if self.phase == 1 and name.startswith("seg_") and name.endswith(".flv"):
                if not (self.folder / ".segments" / (name[:-4] + ".json")).exists():
                    self.yielded = True
                    return name
            if (self.phase == 2 and name.startswith("seg_") and name.endswith(".json")
                    and not name.endswith(".attempt.json")):
                candidate = name[:-5] + ".flv"
                if not (self.folder / ".segments" / name).exists() and not (self.folder / candidate).exists():
                    self.yielded = True
                    return candidate
            if self.phase == 2 and name.startswith("seg_") and name.endswith(".attempt.json"):
                stem = name[:-13]
                candidate = stem + ".flv"
                if (not (self.folder / ".results" / (stem + ".json")).exists()
                        and not (self.folder / ".segments" / (stem + ".json")).exists()
                        and not (self.folder / candidate).exists()):
                    self.yielded = True
                    return candidate
        if self.phase >= len(roots):
            raise StopIteration
        return None


class SweepEngine:
    def __init__(self, data, *, recognize, prepare_model=lambda: None, should_stop=lambda: False,
                 publish=lambda _summary: None, monotonic=time.monotonic, wall_time=time.time,
                 model_name="auto", state_path=None, dependency_token=lambda: None,
                 cleanup_enabled=False, storage=None,
                 scan_interval=SCAN_INTERVAL, scan_items=SCAN_ITEMS, scan_seconds=SCAN_SECONDS,
                 day_cursors=DAY_CURSORS, sample_limit=SAMPLE_LIMIT, retry_limit=RETRY_LIMIT,
                 verify_interval=VERIFY_INTERVAL, retry_delays=RETRY_DELAYS, attempt_limit=ATTEMPT_LIMIT):
        self.data = Path(data).absolute()
        self.scope = audio.data_scope(self.data)
        self.recognize, self.prepare_model, self.should_stop, self.publish = recognize, prepare_model, should_stop, publish
        self.now, self.wall, self.model_name = monotonic, wall_time, model_name
        self.dependency_token = dependency_token
        if type(cleanup_enabled) is not bool:
            raise ValueError("invalid cleanup setting")
        self.cleanup_enabled, self.storage = cleanup_enabled, storage
        self.scan_interval, self.scan_items, self.scan_seconds = float(scan_interval), int(scan_items), float(scan_seconds)
        self.day_limit, self.sample_limit, self.retry_limit = int(day_cursors), int(sample_limit), int(retry_limit)
        self.verify_interval, self.delays, self.attempt_limit = float(verify_interval), tuple(retry_delays), int(attempt_limit)
        if (self.scan_interval <= 0 or not 1 <= self.scan_items <= 256 or self.scan_seconds <= 0
                or not 1 <= self.day_limit <= 64 or not 1 <= self.sample_limit <= 1024
                or not 1 <= self.retry_limit <= 4096 or self.verify_interval <= 0
                or not 1 <= self.attempt_limit <= 10 or not self.delays
                or any(not _finite(delay, 3600) or delay <= 0 for delay in self.delays)):
            raise ValueError("invalid sweep limits")
        self.path = Path(state_path) if state_path is not None else self.data / ".local_app/asr/retry.json"
        self.path = self.path.absolute()
        audio._no_links(self.path.parent)
        if not self.path.resolve().is_relative_to(self.data.resolve()):
            raise ValueError("retry state outside data")
        self._root, self._days, self._waiting, self._root_done = None, deque(), deque(), False
        self._scan_evicted = False
        self._storage_scan_open, self._storage_limited = False, False
        self._cache, self._epoch, self._checked = OrderedDict(), 0, 0
        self._partial, self._limited, self._next_scan = True, False, 0.0
        self._last_complete, self._last_progress, self._last_completed = None, None, None
        self._current, self._state, self._error = None, "idle", ""
        self._busy_until, self._model_ready, self._storage_bad = 0.0, False, False
        self._scope_bad = False
        self._entries, self._due, self._model_due = {}, {}, 0.0
        self._cleanup_entries, self._cleanup_due = {}, {}
        self._model = self._empty_model()
        self._load_state()

    def _signature(self):
        raw = json.dumps(self.dependency_token(), sort_keys=True, default=str, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()

    def _empty_model(self):
        return {"attempts": 0, "lifetime": 0, "wait_seconds": 0, "next_at": 0,
                "last_error": "", "signature": self._signature()}

    def _load_state(self):
        try:
            self._require_scope()
            value = audio.read_json(self.path)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            self._storage_bad = True
            return
        try:
            payload = {key: item for key, item in value.items() if key != "checksum"}
            if (_sealed(payload) != value or type(value.get("schema")) is not int or value["schema"] not in (1, 2) or value.get("kind") != "asr_retry"
                    or value.get("scope") != self.scope or not _finite(value.get("saved_at"))
                    or not isinstance(value.get("entries"), dict) or len(value["entries"]) > self.retry_limit):
                raise ValueError
            for key, entry in value["entries"].items():
                if not isinstance(key, str) or len(key) > 260 or not isinstance(entry, dict):
                    raise ValueError
                self._validate_entry(entry)
            model = value["model"]
            self._validate_entry(model, model=True)
            cleanup = value["cleanup"] if value["schema"] == 2 else value.get("cleanup", {})
            if not isinstance(cleanup, dict) or len(cleanup) > self.retry_limit:
                raise ValueError
            for key, entry in cleanup.items():
                self._validate_cleanup(entry)
                if key != self._cleanup_key(entry):
                    raise ValueError
            self._entries, self._model = value["entries"], model
            self._cleanup_entries = cleanup
            completed = value.get("last_completed_at")
            if completed is not None and not _finite(completed):
                raise ValueError
            self._last_completed = completed
            saved = value["saved_at"]
            self._due = {key: self.now() + self._remaining(entry, saved) for key, entry in self._entries.items()}
            self._model_due = self.now() + self._remaining(model, saved)
            self._cleanup_due = {key: self.now() + self._remaining(entry, saved) for key, entry in cleanup.items()}
            self._storage_bad = False
        except (ValueError, KeyError, TypeError):
            self._storage_bad = True

    def _validate_entry(self, entry, *, model=False):
        if (type(entry.get("attempts")) is not int or not 0 <= entry["attempts"] <= self.attempt_limit
                or type(entry.get("lifetime")) is not int or not 0 <= entry["lifetime"] <= 2 ** 53 - 1
                or not _finite(entry.get("wait_seconds"), max(self.delays)) or not _finite(entry.get("next_at"))
                or entry.get("last_error", "") not in _SAFE | {""}):
            raise ValueError
        if model:
            if not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("signature", ""))):
                raise ValueError
        elif (type(entry.get("projection_attempts")) is not int
              or not 0 <= entry["projection_attempts"] <= self.attempt_limit
              or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("source_token", "")))):
            raise ValueError

    def _remaining(self, entry, saved):
        # Clock rollback cannot create an unbounded wait. Forward jumps can
        # permit one retry; the persistent finite attempt budget still applies.
        if self.wall() < saved:
            return entry["wait_seconds"]
        return min(entry["wait_seconds"], max(0.0, entry["next_at"] - self.wall()))

    def _cleanup_key(self, status):
        fields = (status.get("source_id"), status.get("generation"), status.get("result_sha256"))
        if not all(isinstance(value, str) and re.fullmatch(pattern, value)
                   for value, pattern in zip(fields, (r"[0-9a-f]{64}", r"[0-9a-f]{32}", r"[0-9a-f]{64}"))):
            return None
        raw = json.dumps([self.scope, *fields], sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _validate_cleanup(self, entry):
        if (not isinstance(entry, dict) or set(entry) != {"source_id", "generation", "result_sha256",
                "attempts", "lifetime", "wait_seconds", "next_at", "last_error"}
                or self._cleanup_key(entry) is None
                or type(entry.get("attempts")) is not int or not 0 <= entry["attempts"] <= self.attempt_limit
                or type(entry.get("lifetime")) is not int or not 0 <= entry["lifetime"] <= 2 ** 53 - 1
                or not _finite(entry.get("wait_seconds"), max(self.delays)) or not _finite(entry.get("next_at"))
                or entry.get("last_error") not in _CLEANUP_REASONS | {""}):
            raise ValueError("cleanup state unavailable")

    def _save_state(self):
        if self._storage_bad:
            raise ValueError("retry state unavailable")
        try:
            self._require_scope()
            audio._no_links(self.path.parent)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            value = _sealed({"schema": 2, "kind": "asr_retry", "scope": self.scope,
                             "saved_at": self.wall(), "entries": self._entries, "model": self._model,
                             "cleanup": self._cleanup_entries,
                             "last_completed_at": self._last_completed})
            audio.atomic_json(self.path, value)
        except (OSError, ValueError):
            self._storage_bad = True
            raise

    def _require_scope(self):
        try:
            if audio.data_scope(self.data) != self.scope:
                raise ValueError("data scope changed")
        except (OSError, ValueError):
            self._scope_bad = True
            raise ValueError("data scope unavailable") from None

    def _stop(self):
        try:
            self._require_scope()
            stopped = self.should_stop() is not False
        except Exception:
            self._error, self._state = "admission_unavailable", "identity_unknown"
            return True
        if stopped:
            self._state = "stopping"
        return stopped

    def _emit(self):
        self.publish(self.snapshot())

    def _stage(self, folder, name, phase, attempt=0):
        match = _DAY.fullmatch(folder.name)
        self._current = {"tag": (match[1] if match else "")[:40], "date": match[2] if match else "",
                         "segment": name[:64], "phase": phase, "attempt": attempt}
        self._last_progress = self.wall()
        self._state, self._error = "processing", ""
        self._emit()

    def _tokens(self, folder, name):
        self._require_scope()
        audio.safe_folder(self.data, folder)
        stem = Path(name).stem
        source = [_stat(folder / name), _stat(folder / ".segments" / (stem + ".json"))]
        other = [_stat(folder / ".results" / (stem + ".json")), _stat(folder / ".results" / (stem + ".attempt.json")),
                 _stat(folder / ".speech-projection.json"), _stat(folder / ".speech-projection.pending.json"),
                 _stat(folder / ".transcribed.json"), _stat(folder / ".transcribed.json.rebuild.pending"),
                 _stat(folder / ".cleanup", links=True), _stat(folder / ".cleanup" / (stem + ".json"), links=True)]
        match = _DAY.fullmatch(folder.name)
        if match:
            other.append(_stat(self.data / f"speech_{match[1]}_{match[2]}.jsonl"))
        digest = lambda value: hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()
        return digest(source + other), digest(source)

    def _remember(self, key, folder, name, category, reason, token, source_token, status=None):
        if key not in self._cache and len(self._cache) >= self.sample_limit:
            self._cache.popitem(last=False)
            self._limited = True
        self._cache[key] = {"folder": folder, "name": name, "category": category, "reason": reason,
                            "token": token, "source_token": source_token, "checked": self.now(), "epoch": self._epoch,
                            "status": dict(status) if status is not None else {"name": name, "result_committed": False,
                                "current_result": False, "projection_ready": False, "cleanup_eligible": False,
                                "reusable": False, "audio_cleaned": False, "history_usable": False,
                                "cleanup_state": "unconfirmed", "cleanup_reason": "", "reason": reason,
                                "source_id": None, "generation": None, "result_sha256": None}}
        self._cache.move_to_end(key)
        return self._cache[key]

    def _probe(self, folder, name, *, fresh=False):
        key = folder.name + "/" + name
        if len(key) > 260:
            return key, self._remember(key, folder, name, "blocked", "invalid_folder", "", "")
        token, source_token = self._tokens(folder, name)
        old = self._cache.get(key)
        if not fresh and old and old["token"] == token and self.now() - old["checked"] < self.verify_interval:
            old["epoch"] = self._epoch
            return key, old
        category, reason, status = "blocked", "evidence_blocked", None
        if not _SEGMENT.fullmatch(name) or name != "seg_%04d.flv" % int(_SEGMENT.fullmatch(name)[1]):
            reason = "invalid_segment_name"
        else:
            previous = self._current, self._state, self._error
            self._stage(folder, name, "checking")
            try:
                try:
                    results._projection_owned(self.data, folder)
                except (OSError, ValueError) as error:
                    reason = _reason(getattr(error, "reason", "evidence_blocked"))
                    return key, self._remember(key, folder, name, category, reason, token, source_token)
                status = results.validate_success(self.data, folder, name)
                reason = _reason(status.get("reason"))
                if (status["current_result"] and status.get("audio_cleaned") is True
                        and status.get("history_usable") is True):
                    category, reason = "complete", "complete"
                elif (status["current_result"] and status.get("audio_cleaned") is True
                        and not status["projection_ready"]
                        and status.get("cleanup_state") in {"pending", "deleted"}
                        and not status.get("cleanup_reason")):
                    category, reason = "projection_pending", "projection_pending"
                elif status["current_result"] and status["reusable"]:
                    category = "complete" if status["projection_ready"] else "projection_pending"
                elif reason in {"result_missing", "attempt_pending", "attempt_failed"}:
                    try:
                        closed = audio.verify_closed(self.data, folder, name)
                        intent = results._attempt(self.data, folder, name)
                        if closed["size"] < 4096:
                            reason = "too_small"
                        else:
                            category = "retryable" if intent is not None else "pending"
                    except (OSError, ValueError) as error:
                        reason = _reason(getattr(error, "reason", "source_unconfirmed"), "source_unconfirmed")
            finally:
                self._current, self._state, self._error = previous
                self._last_progress = self.wall()
        entry = self._remember(key, folder, name, category, reason, token, source_token, status)
        if category == "complete" and key in self._entries:
            self._entries.pop(key)
            self._due.pop(key, None)
        return key, entry

    def _begin_scan(self):
        self._root = os.scandir(self.data)
        self._root_done, self._partial, self._limited = False, True, False
        self._scan_evicted = False
        self._epoch += 1
        self._checked = 0
        self._storage_limited = False
        self._storage_scan_open = True
        if self.storage is not None:
            self.storage.begin_scan()

    def _fill_days(self, deadline):
        inspected = 0
        while (not self._root_done and len(self._days) < self.day_limit and inspected < self.scan_items
               and self.now() < deadline):
            try:
                entry = next(self._root)
            except StopIteration:
                self._root.close()
                self._root, self._root_done = None, True
                break
            inspected += 1
            if not _DAY.fullmatch(entry.name):
                continue
            path = self.data / entry.name
            try:
                audio.safe_folder(self.data, path)
            except (OSError, ValueError):
                self._storage_limited = True
                self._remember(entry.name, path, "", "blocked", "linked_path", "", "")
                continue
            self._days.append(_DayCursor(path))
        if self._root_done:
            while self._waiting and len(self._days) < self.day_limit:
                self._days.append(self._waiting.popleft())

    def _candidate(self, deadline):
        self._fill_days(deadline)
        count = len(self._days)
        while self._days and count and self.now() < deadline:
            day = self._days.popleft()
            count -= 1
            try:
                name = day.next(deadline=deadline, now=self.now)
            except StopIteration:
                day.close()
                if not day.yielded:
                    match = _DAY.fullmatch(day.folder.name)
                    speech = self.data / f"speech_{match[1]}_{match[2]}.jsonl"
                    material = [speech, day.folder / ".transcribed.json", day.folder / ".transcribed.json.rebuild.pending",
                                day.folder / ".speech-projection.json", day.folder / ".speech-projection.pending.json"]
                    if any(path.exists() or path.is_symlink() for path in material):
                        self._remember(day.folder.name, day.folder, "", "blocked", "legacy_conflict", "", "")
                self._fill_days(deadline)
                continue
            except (OSError, ValueError):
                self._storage_limited = True
                day.close()
                self._remember(day.folder.name, day.folder, "", "blocked", "scan_unavailable", "", "")
                continue
            # Root discovery gets a slot after a source quantum, rather than
            # waiting for the first long directories to finish. Suspended
            # cursors retain only phase/ordinal; replay also has an item budget.
            if day.replaying or self._root_done and not self._waiting:
                self._days.append(day)
            else:
                day.close()
                if len(self._waiting) >= DIRECTORY_LIMIT:
                    self._waiting.popleft().close()
                    self._limited = self._scan_evicted = True
                self._waiting.append(day)
                self._fill_days(deadline)
            if name is not None:
                return day.folder, name
        if self._root_done and not self._days and not self._waiting:
            self._partial = self._scan_evicted
            if not self._scan_evicted:
                self._last_complete = self.wall()
            self._next_scan = self.now() + self.scan_interval
            for key in list(self._cache):
                if self._cache[key]["epoch"] != self._epoch:
                    self._cache.pop(key)
            self._finish_storage_scan(complete=not self._scan_evicted)
        return None

    def _finish_storage_scan(self, *, complete):
        if self._storage_scan_open:
            self._storage_scan_open = False
            if self.storage is not None:
                self.storage.finish_scan(complete=complete, limited=self._storage_limited or self._scan_evicted)

    def _observe_candidate(self, entry):
        if self.storage is None or self._scope_bad:
            return
        status = dict(entry["status"], asr_category=entry["category"], asr_reason=entry["reason"])
        key = self._cleanup_key(status)
        record = self._cleanup_entries.get(key)
        if self.cleanup_enabled and self._needs_cleanup(entry) and key is None:
            status["cleanup_schedule_reason"] = "cleanup_binding_conflict"
        elif (self.cleanup_enabled and self._needs_cleanup(entry) and record is None
                and len(self._cleanup_entries) >= self.retry_limit):
            status["cleanup_schedule_reason"] = "cleanup_capacity"
        self.storage.observe(entry["folder"], entry["name"], status,
            cleanup_attempts=record["attempts"] if record else 0,
            cleanup_exhausted=bool(record and record["attempts"] >= self.attempt_limit))

    def _schedule(self, key, record, error, *, projection=False):
        field = "projection_attempts" if projection else "attempts"
        record["last_error"] = _reason(error, "projection_failed" if projection else "asr_failed")
        delay = self.delays[min(max(record[field] - 1, 0), len(self.delays) - 1)]
        record.update(wait_seconds=delay, next_at=self.wall() + delay)
        self._due[key] = self.now() + delay
        self._save_state()

    def _record(self, key, source_token):
        record = self._entries.get(key)
        if record is None or record["source_token"] != source_token:
            if record is None and len(self._entries) >= self.retry_limit:
                raise ValueError("retry capacity")
            record = {"attempts": 0, "projection_attempts": 0, "lifetime": (record or {}).get("lifetime", 0),
                      "wait_seconds": 0, "next_at": 0, "last_error": "", "source_token": source_token}
            self._entries[key] = record
            self._due.pop(key, None)
        return record

    def _save_model_wait(self):
        delay = self.delays[min(self._model["attempts"] - 1, len(self.delays) - 1)]
        self._model.update(last_error="model_unavailable", wait_seconds=delay, next_at=self.wall() + delay)
        self._model_due = self.now() + delay
        self._save_state()

    @staticmethod
    def _confirmed_deleted(entry):
        status = entry["status"]
        return (status.get("current_result") is True and status.get("audio_cleaned") is True
                and status.get("history_usable") is True and status.get("cleanup_state") == "deleted")

    @staticmethod
    def _needs_cleanup(entry):
        status = entry["status"]
        return (entry["category"] == "complete" and status.get("current_result") is True
                and status.get("projection_ready") is True
                and (status.get("cleanup_eligible") is True
                     or status.get("audio_cleaned") is True and status.get("history_usable") is True
                     and status.get("cleanup_state") == "pending" and not status.get("cleanup_reason")))

    def _cleanup_ready(self, entry):
        if not self.cleanup_enabled:
            return False
        key = self._cleanup_key(entry["status"])
        if key is None:
            return False
        record = self._cleanup_entries.get(key)
        if self._confirmed_deleted(entry):
            return record is not None
        return (self._needs_cleanup(entry) and (record is None or record["attempts"] < self.attempt_limit
                and self.now() >= self._cleanup_due.get(key, 0)))

    def _save_cleanup_wait(self, key, record, reason):
        delay = self.delays[min(record["attempts"] - 1, len(self.delays) - 1)]
        record.update(last_error=reason, wait_seconds=delay, next_at=self.wall() + delay)
        self._cleanup_due[key] = self.now() + delay
        self._save_state()

    def _record_cleanup_outcome(self, folder, name, outcome):
        if self.storage is not None:
            self.storage.record_cleanup(folder, name, outcome)

    def _restore_cleanup_reservation(self, key, previous, deadline):
        if previous is None:
            self._cleanup_entries.pop(key, None)
            self._cleanup_due.pop(key, None)
        else:
            self._cleanup_entries[key] = previous
            self._cleanup_due[key] = deadline
        self._save_state()

    def _cleanup_locked(self, entry):
        """Caller holds the one writer. Scheduling never grants deletion."""
        if not self.cleanup_enabled or self._stop():
            return entry
        folder, name = entry["folder"], entry["name"]
        key = self._cleanup_key(entry["status"])
        if key is None:
            return entry
        record = self._cleanup_entries.get(key)
        if self._confirmed_deleted(entry):
            if record is not None:
                self._cleanup_entries.pop(key)
                self._cleanup_due.pop(key, None)
                self._save_state()
            return entry
        if not self._cleanup_ready(entry):
            return entry
        if record is None and len(self._cleanup_entries) >= self.retry_limit:
            self._record_cleanup_outcome(folder, name, {"state": "kept", "reason": "cleanup_capacity",
                                                       "deleted": False, "deleted_now": False})
            return entry
        previous, deadline = copy.deepcopy(record), self._cleanup_due.get(key, 0.0)
        if record is None:
            record = {field: entry["status"][field] for field in ("source_id", "generation", "result_sha256")}
            record.update(attempts=0, lifetime=0, wait_seconds=0, next_at=0, last_error="")
        self._stage(folder, name, "cleaning", record["attempts"] + 1)
        if self._stop():
            return entry
        self._cleanup_entries[key] = record
        record["attempts"] += 1
        record["lifetime"] += 1
        # Accept this call durably before native code, including its bounded
        # next condition if the worker exits before the call returns.
        self._save_cleanup_wait(key, record, "cleanup_unconfirmed")
        if self._stop():
            self._restore_cleanup_reservation(key, previous, deadline)
            return entry
        try:
            from scripts import audio_cleanup
            raw = audio_cleanup.cleanup_segment(self.data, folder, name)
        except Exception as error:
            raw = {"state": "pending", "reason": getattr(error, "reason", "cleanup_unconfirmed")}
        state = raw.get("state") if isinstance(raw, dict) else None
        reason = raw.get("reason") if isinstance(raw, dict) else None
        outcome = {"state": state if isinstance(state, str) and state in {"deleted", "pending", "kept", "busy"} else "pending",
                   "reason": reason if isinstance(reason, str) and reason in _CLEANUP_REASONS else "cleanup_unconfirmed",
                   "deleted": isinstance(raw, dict) and raw.get("deleted") is True,
                   "deleted_now": isinstance(raw, dict) and raw.get("deleted_now") is True}
        if outcome["state"] == "busy":
            # The primitive's busy result guarantees zero accepted operation.
            self._restore_cleanup_reservation(key, previous, deadline)
            self._busy_until = self.now() + BUSY_INTERVAL
        else:
            _, entry = self._probe(folder, name, fresh=True)
            if outcome["state"] == "deleted" and self._confirmed_deleted(entry):
                self._cleanup_entries.pop(key, None)
                self._cleanup_due.pop(key, None)
                self._save_state()
            else:
                if outcome["state"] == "deleted":
                    outcome.update(state="pending", reason="cleanup_commit_unconfirmed")
                self._save_cleanup_wait(key, record, outcome["reason"])
        self._last_progress = self.wall()
        self._record_cleanup_outcome(folder, name, outcome)
        return entry

    def _cleanup_unit(self, entry):
        folder, name = entry["folder"], entry["name"]
        try:
            with results.writer(self.data):
                if self._stop():
                    return
                _, entry = self._probe(folder, name, fresh=True)
                self._cleanup_locked(entry)
                self._current = None
                if not self._stop():
                    self._state, self._error = "idle", ""
        except results.WriterBusy:
            self._busy_until = self.now() + BUSY_INTERVAL
            self._state, self._error = "busy", ""
        except (OSError, ValueError):
            self._current = None
            self._state, self._error = (("identity_unknown", "admission_unavailable") if self._scope_bad
                else ("dependency_error", "retry_state_unavailable") if self._storage_bad
                else ("idle", ""))

    def _unit(self, key, entry):
        folder, name = entry["folder"], entry["name"]
        try:
            with results.writer(self.data):
                if self._stop():
                    return
                key, entry = self._probe(folder, name, fresh=True)
                kind = entry["category"]
                if kind not in {"pending", "retryable", "projection_pending"}:
                    self._cleanup_locked(entry)
                    return
                previous = self._entries.get(key)
                matching = previous is not None and previous["source_token"] == entry["source_token"]
                record = previous if matching else {"attempts": 0, "projection_attempts": 0}
                projection = kind == "projection_pending"
                field = "projection_attempts" if projection else "attempts"
                if record[field] >= self.attempt_limit or matching and self.now() < self._due.get(key, 0):
                    self._state, self._error = "backoff", "retry_exhausted" if record[field] >= self.attempt_limit else record["last_error"]
                    return
                if not projection and not self._model_ready:
                    signature = self._signature()
                    if signature != self._model["signature"]:
                        lifetime = self._model["lifetime"]
                        remaining = min(max(self._model_due - self.now(), BUSY_INTERVAL), max(self.delays))
                        self._model = self._empty_model()
                        self._model.update(signature=signature, lifetime=lifetime, wait_seconds=remaining,
                                           next_at=self.wall() + remaining, last_error="model_unavailable")
                        self._model_due = self.now() + remaining
                        self._save_state()
                    if self._model["attempts"] >= self.attempt_limit or self.now() < self._model_due:
                        self._state, self._error = "dependency_error", "model_unavailable"
                        return
                    self._stage(folder, name, "model_loading", self._model["attempts"] + 1)
                    if self._stop():
                        return
                    # Accept the load durably before entering native code. An
                    # unhandled exit keeps this budget and cooldown on restart.
                    self._model["attempts"] += 1
                    self._model["lifetime"] += 1
                    self._save_model_wait()
                    if self._stop():
                        return
                    try:
                        self.prepare_model()
                    except Exception:
                        # This is the same accepted load, with its cooldown
                        # measured from the handled failure rather than twice.
                        self._save_model_wait()
                        self._state, self._error = "dependency_error", "model_unavailable"
                        return
                    lifetime = self._model["lifetime"]
                    self._model = self._empty_model()
                    self._model["lifetime"] = lifetime
                    self._model_due = 0.0
                    # Confirm success settlement before permitting a source
                    # attempt; clean restarts do not consume the failure limit.
                    self._save_state()
                    self._model_ready = True
                if self._stop():
                    return
                record = self._record(key, entry["source_token"])
                self._stage(folder, name, "projection" if projection else "recognizing", record[field] + 1)
                if self._stop():
                    return
                if projection:
                    record[field] += 1
                    self._save_state()
                    try:
                        outcome = results.rebuild_projection(self.data, folder)
                        if not outcome["projection_ready"]:
                            raise results.ResultError("projection_failed")
                    except (OSError, ValueError) as error:
                        self._schedule(key, record, getattr(error, "reason", "projection_failed"), projection=True)
                else:
                    def recognize(path):
                        record["attempts"] += 1
                        record["lifetime"] += 1
                        self._save_state()
                        return self.recognize(path)
                    outcome = results.process_segment(self.data, folder, name, recognize, self.model_name)
                    if outcome["state"] not in {"completed", "reused"}:
                        self._schedule(key, record, outcome.get("reason", "asr_failed"))
                    else:
                        try:
                            results.rebuild_projection(self.data, folder)
                        except (OSError, ValueError):
                            record["projection_attempts"] += 1
                            self._schedule(key, record, "projection_failed", projection=True)
                # Once accepted, source processing and its projection can finish
                # naturally even if stop appeared during recognition.
                _, latest = self._probe(folder, name, fresh=True)
                if latest["category"] == "complete":
                    self._last_completed, self._last_progress = self.wall(), self.wall()
                    self._save_state()
                    if not self._stop():
                        latest = self._cleanup_locked(latest)
                self._current = None
                if self._stop():
                    self._state = "stopping"
                elif latest["category"] == "complete":
                    self._state, self._error = "idle", ""
                else:
                    self._state, self._error = "backoff", record["last_error"]
        except results.WriterBusy:
            self._busy_until = self.now() + BUSY_INTERVAL
            self._state, self._error = "busy", ""
        except (OSError, ValueError) as error:
            if self._scope_bad:
                self._current = None
                self._state, self._error = "identity_unknown", "admission_unavailable"
            else:
                self._error = "retry_state_unavailable" if self._storage_bad else _reason(getattr(error, "reason", "retry_capacity"))
                self._state = "dependency_error" if self._storage_bad else "backoff"

    def step(self):
        if self._stop():
            self._emit()
            return 0.0
        if self._storage_bad:
            self._state, self._error = "dependency_error", "retry_state_unavailable"
            self._emit()
            return self.scan_interval
        if self.now() < self._busy_until:
            self._emit()
            return min(self.scan_interval, self._busy_until - self.now())
        if self._root is None and not self._days and not self._waiting and self._root_done and self.now() < self._next_scan:
            self._emit()
            return min(self.scan_interval, self._next_scan - self.now())
        started = self.now()
        try:
            if self._root is None and not self._days and not self._waiting:
                self._begin_scan()
            self._state, self._error = "scanning", ""
            for _ in range(self.scan_items):
                if self._stop() or self.now() - started >= self.scan_seconds:
                    break
                candidate = self._candidate(started + self.scan_seconds)
                if candidate is None:
                    break
                self._checked += 1
                key, entry = self._probe(*candidate)
                performed = False
                if self._cleanup_ready(entry):
                    self._cleanup_unit(entry)
                    performed = True
                elif entry["category"] in {"pending", "retryable", "projection_pending"}:
                    field = "projection_attempts" if entry["category"] == "projection_pending" else "attempts"
                    record = self._entries.get(key)
                    due = (not record or record["source_token"] != entry["source_token"]
                           or record[field] < self.attempt_limit and self.now() >= self._due.get(key, 0))
                    if due:
                        self._unit(key, entry)
                        performed = True
                self._observe_candidate(self._cache.get(key, entry))
                if performed:
                    break
            if self._state == "scanning":
                self._state = "backoff" if any(item["category"] == "retryable" for item in self._cache.values()) else "idle"
        except (OSError, ValueError) as error:
            self._state, self._error = "dependency_error", _reason(getattr(error, "reason", "scan_unavailable"), "scan_unavailable")
        self._emit()
        return 0.0 if self._partial else max(0.0, min(self.scan_interval, self._next_scan - self.now()))

    def retry(self):
        try:
            self._require_scope()
        except ValueError:
            self._state, self._error = "identity_unknown", "admission_unavailable"
            self._emit()
            return False
        if self._storage_bad:
            self._load_state()
            if self._storage_bad:
                self._state, self._error = "dependency_error", "retry_state_unavailable"
                self._emit()
                return False
        for entry in self._entries.values():
            entry.update(attempts=0, projection_attempts=0, wait_seconds=0, next_at=0)
        for entry in self._cleanup_entries.values():
            entry.update(attempts=0, wait_seconds=0, next_at=0, last_error="")
        lifetime = self._model["lifetime"]
        self._model = self._empty_model()
        self._model["lifetime"] = lifetime
        self._due, self._model_due, self._busy_until, self._next_scan = {}, 0.0, 0.0, 0.0
        self._cleanup_due = {}
        self._cache.clear()
        self._save_state()
        self._state, self._error = "idle", ""
        self._emit()
        return True

    def snapshot(self):
        counts = {name: 0 for name in ("pending", "projection_pending", "retryable", "blocked", "complete")}
        for entry in self._cache.values():
            counts[entry["category"]] += 1
        deadlines = []
        exhausted = False
        for key, deadline in self._due.items():
            entry = self._entries.get(key)
            category = self._cache.get(key, {}).get("category")
            field = "projection_attempts" if category == "projection_pending" else "attempts"
            if entry and entry[field] >= self.attempt_limit:
                exhausted = True
            elif deadline > self.now():
                deadlines.append(deadline)
        if self._model_due > self.now() and self._model["attempts"] < self.attempt_limit:
            deadlines.append(self._model_due)
        next_at = self.wall() + min(deadlines) - self.now() if deadlines else None
        summary = {"state": self._state, "counts": counts,
                "scan": {"partial": self._partial or self._limited, "limited": self._limited,
                         "checked": self._checked, "last_complete_at": self._last_complete,
                         "counts_scope": "recent_window", "sample_limit": self.sample_limit},
                "current": copy.deepcopy(self._current), "error": self._error, "next_retry_at": next_at,
                "last_progress_at": self._last_progress, "last_completed_at": self._last_completed,
                "next_condition": "explicit_retry_or_dependency_change" if self._model["attempts"] >= self.attempt_limit or exhausted
                                  else "periodic_scan",
                "defaults": {"scan_interval": self.scan_interval, "scan_items": self.scan_items,
                             "retry_delays": list(self.delays), "attempt_limit": self.attempt_limit,
                             "verify_interval": self.verify_interval, "day_cursors": self.day_limit,
                             "directory_limit": DIRECTORY_LIMIT}}
        if self.storage is not None:
            summary["storage"] = self.storage.snapshot()
        return summary

    def close(self):
        if self._root is not None:
            self._root.close()
            self._root = None
        while self._days:
            self._days.popleft().close()
        while self._waiting:
            self._waiting.popleft().close()
        # Statistics cannot prevent owned enumeration handles from closing.
        self._finish_storage_scan(complete=False)
