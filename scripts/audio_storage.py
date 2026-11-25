"""Worker-only bounded scan counters and disk sampling; snapshots do no I/O."""
from __future__ import annotations

import copy
from datetime import datetime
import math
from pathlib import Path
import re
import shutil
import stat
import time

from scripts import audio_segments as audio

LOW_SPACE_BYTES = 1024 ** 3
DISK_INTERVAL = 30.0
MAX_BYTES = 2 ** 53 - 1
MAX_COUNT = 10 ** 9
COUNTS = ("audio_bytes", "audio_files", "transcription_pending", "cleanup_pending", "cleanup_failed", "cleanup_blocked")
_DAY = re.compile(r"audio_(.+)_([0-9]{4}-[0-9]{2}-[0-9]{2})\Z")
_SEGMENT = re.compile(r"seg_([0-9]{4,})\.flv\Z")
_REASONS = set("busy audio_deleted cleanup_accept_unconfirmed cleanup_binding_conflict cleanup_cancel_unconfirmed "
    "cleanup_commit_unconfirmed cleanup_delete_failed cleanup_delete_unconfirmed cleanup_handle_unconfirmed "
    "cleanup_platform_unsupported cleanup_record_invalid cleanup_unconfirmed cleanup_retry_exhausted "
    "cleanup_capacity cleanup_retry_state_unavailable source_changed source_missing source_unconfirmed "
    "projection_pending projection_invalid attempt_missing attempt_invalid attempt_failed result_invalid "
    "result_generation_missing legacy_conflict linked_path invalid_file invalid_folder invalid_segment_name "
    "outside_data admission_unavailable".split())


def _integer(value, maximum):
    return type(value) is int and 0 <= value <= maximum


def _stamp(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value > 0 else None


class StorageSummary:
    """Constant-memory, non-atomic aggregates from one bounded traversal.

    The engine reports each candidate's final observation exactly once. No
    paths, transcript content, success authority or cleanup budget are stored.
    """
    def __init__(self, data, *, scope=None, monotonic=time.monotonic, wall_time=time.time, disk_usage=None):
        self.data = Path(data).absolute()
        self.scope = audio.data_scope(self.data) if scope is None else copy.deepcopy(scope)
        self.now, self.wall = monotonic, wall_time
        self.disk_usage = disk_usage or shutil.disk_usage
        self._next_disk = 0.0
        self._counts = dict.fromkeys(COUNTS, None)
        self._scan = {"partial": True, "limited": False, "checked": 0, "started_at": None,
                      "updated_at": None, "last_complete_at": None, "approximate": True, "counts_scope": "scan_round"}
        self._disk = {"free_bytes": None, "total_bytes": None, "low_space": None,
                      "threshold_bytes": LOW_SPACE_BYTES, "sampled_at": None, "error": "disk_unconfirmed"}
        self._last_cleanup = None

    def _scope_current(self):
        try:
            return audio.data_scope(self.data) == self.scope
        except (ValueError, OSError):
            return False

    def _limited(self):
        self._scan.update(partial=True, limited=True)

    def begin_scan(self):
        self._counts = dict.fromkeys(COUNTS, 0)
        self._scan.update(partial=True, limited=False, checked=0,
                          started_at=_stamp(self.wall()), updated_at=_stamp(self.wall()))
        if not self._scope_current():
            self._limited()

    def _source(self, folder, name):
        """Cheap canonical-leaf metadata only; never open/hash source bytes."""
        try:
            if not self._scope_current():
                raise ValueError("scope changed")
            folder = audio.safe_folder(self.data, folder)
            match, segment = _DAY.fullmatch(folder.name), _SEGMENT.fullmatch(name)
            if not match or not segment or name != "seg_%04d.flv" % audio._number(name):
                raise ValueError("noncanonical source")
            datetime.strptime(match[2], "%Y-%m-%d")
            source = folder / name
            audio._no_links(source)
            try:
                info = source.lstat()
            except FileNotFoundError:
                if not self._scope_current():
                    raise ValueError("scope changed")
                return False, 0, True
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or getattr(info, "st_file_attributes", 0) & 0x400
                    or not _integer(info.st_size, MAX_BYTES) or not self._scope_current()):
                raise ValueError("unconfirmed source")
            return True, info.st_size, True
        except (ValueError, OSError):
            return None, 0, False

    def _add(self, name, amount):
        previous = self._counts[name]
        maximum = MAX_BYTES if name == "audio_bytes" else MAX_COUNT
        if previous is None or not _integer(previous + amount, maximum):
            self._counts[name] = None
            self._limited()
        else:
            self._counts[name] = previous + amount

    def observe(self, folder, name, status, *, cleanup_attempts=0, cleanup_exhausted=False):
        # One call per discovered candidate, after its possible processing unit.
        if self._counts["audio_bytes"] is None and self._scan["started_at"] is None:
            self.begin_scan()
        present, size, confirmed = self._source(folder, name)
        if not confirmed:
            self._limited()
        if self._scan["checked"] < MAX_COUNT:
            self._scan["checked"] += 1
        else:
            self._limited()
        if present is True:
            self._add("audio_bytes", size)
            self._add("audio_files", 1)
        status = status if isinstance(status, dict) else {}
        text_done = status.get("current_result") is True and status.get("projection_ready") is True
        cleaned = (status.get("audio_cleaned") is True and status.get("history_usable") is True
                   and status.get("cleanup_state") == "deleted")
        eligible = (status.get("cleanup_eligible") is True and present is True and confirmed
                    or status.get("audio_cleaned") is True and status.get("history_usable") is True
                    and status.get("cleanup_state") == "pending" and present is False and confirmed)
        if text_done:
            if cleaned and present is False and confirmed:
                pass
            elif (isinstance(status.get("cleanup_schedule_reason"), str)
                  and status["cleanup_schedule_reason"] in {"cleanup_capacity", "cleanup_retry_state_unavailable"}):
                self._add("cleanup_blocked", 1)
            elif eligible:
                failed = (_integer(cleanup_attempts, MAX_COUNT) and cleanup_attempts > 0) or cleanup_exhausted is True
                self._add("cleanup_failed" if failed else "cleanup_pending", 1)
            else:
                self._add("cleanup_blocked", 1)
        elif present is not False:
            self._add("transcription_pending", 1)
        self._scan["updated_at"] = _stamp(self.wall())

    def finish_scan(self, complete=True, limited=False):
        self._scan["limited"] = self._scan["limited"] or limited is True
        self._scan["partial"] = complete is not True or self._scan["limited"]
        self._scan["updated_at"] = _stamp(self.wall())
        if not self._scan["partial"]:
            self._scan["last_complete_at"] = self._scan["updated_at"]

    def record_cleanup(self, folder, name, outcome):
        match = _DAY.fullmatch(Path(folder).name)
        tag, day = (match[1], match[2]) if match else ("", "")
        if len(tag) > 40 or any(character in '/\\:*?"<>|' or ord(character) < 32 for character in tag):
            tag = ""
        if not isinstance(name, str) or not _SEGMENT.fullmatch(name) or len(name) > 64:
            name = ""
        outcome = outcome if isinstance(outcome, dict) else {}
        state = outcome.get("state")
        state = state if isinstance(state, str) and state in {"deleted", "pending", "kept", "busy"} else "pending"
        reason = outcome.get("reason")
        reason = reason if isinstance(reason, str) and reason in _REASONS else "cleanup_unconfirmed"
        self._last_cleanup = {"at": _stamp(self.wall()), "tag": tag, "date": day, "segment": name,
                              "state": state, "reason": reason}

    def poll_disk(self):
        """Only the admitted worker calls this; never called by snapshot()."""
        if self.now() < self._next_disk:
            return
        self._next_disk = self.now() + DISK_INTERVAL
        try:
            if not self._scope_current():
                raise ValueError("scope changed")
            usage = self.disk_usage(self.data)
            free, total = usage.free, usage.total
            if (not _integer(free, MAX_BYTES) or not _integer(total, MAX_BYTES)
                    or total == 0 or free > total or not self._scope_current()):
                raise ValueError("invalid usage")
            self._disk = {"free_bytes": free, "total_bytes": total, "low_space": free < LOW_SPACE_BYTES,
                          "threshold_bytes": LOW_SPACE_BYTES, "sampled_at": _stamp(self.wall()), "error": ""}
        except (OSError, ValueError, AttributeError, TypeError):
            self._disk = {"free_bytes": None, "total_bytes": None, "low_space": None,
                          "threshold_bytes": LOW_SPACE_BYTES, "sampled_at": None, "error": "disk_unconfirmed"}

    def snapshot(self):
        return copy.deepcopy(dict(self._counts, schema=1, snapshot_at=self._scan["updated_at"],
                                  scan=self._scan, disk=self._disk, last_cleanup=self._last_cleanup))
