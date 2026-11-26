"""Customer watcher notifications based on bounded, fresh evidence.

This is an in-process detector, not an external whole-machine heartbeat.  It
does not identify a platform cause or perform verification on the user's behalf.
The state machine takes clocks and delivery acknowledgements explicitly so it
can be tested without a browser, a real account, or a notification destination.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import time

from config import sanitize_label

STARTUP_GRACE_SECONDS = 300.0
UNKNOWN_SECONDS = 600.0
UNKNOWN_ROUNDS = 3
EVIDENCE_MAX_AGE_SECONDS = 180.0
RETRY_DELAYS = (60.0, 300.0, 1800.0)
TAIL_BYTES = 16384
BUSINESS_TYPES = {
    "stats": frozenset({"room_stats", "room_user_seq"}),
    "danmu": frozenset({"danmaku", "like", "enter", "gift"}),
}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def fresh_timestamp(value, now, max_age=EVIDENCE_MAX_AGE_SECONDS):
    return (_number(value) and _number(now) and 0 < value <= now
            and now - value <= max_age)


def safe_tag(tag):
    return (isinstance(tag, str) and 0 < len(tag) <= 40
            and not any(char in '/\\:*?"<>|' or ord(char) < 32 for char in tag)
            and not tag.endswith((" ", ".")))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def target_watch_enabled(roster_path, tag, expected_sec_uid=None):
    """True/False only for an unambiguous roster; None means unavailable.

    A missing or malformed roster must not be interpreted as a recovery.  This
    function is read-only and never falls back to a seed or private configuration.
    """
    if not safe_tag(tag):
        return None
    try:
        with Path(roster_path).open("rb") as handle:
            raw = handle.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return None
        doc = json.loads(raw, object_pairs_hook=_unique_object)
        rows = doc.get("anchors") if isinstance(doc, dict) else None
        if not isinstance(rows, list):
            return None
        matches = [row for row in rows if isinstance(row, dict) and row.get("tag") == tag]
        if not matches:
            return False
        if len(matches) != 1:
            return None
        row = matches[0]
        if type(row.get("enabled")) is not bool or type(row.get("watch")) is not bool:
            return None
        if not (row["enabled"] and row["watch"]):
            return False
        if expected_sec_uid is not None:
            if not isinstance(row.get("sec_uid"), str):
                return None
            if row["sec_uid"] != expected_sec_uid:
                return False
        return True
    except (OSError, UnicodeError, ValueError):
        return None


def _tail_records(path):
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            offset = max(0, size - TAIL_BYTES)
            handle.seek(offset)
            data = handle.read(TAIL_BYTES)
    except OSError:
        return
    # Ignore both a cut first record and an unfinished last record.
    for line in reversed(data.split(b"\n")[1 if offset else 0:-1]):
        try:
            row = json.loads(line, object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError):
            continue
        if isinstance(row, dict):
            yield row


def latest_business_timestamp(data_dir, tag, now, *, file_tag=None):
    """Read at most four bounded tails; timestamps and event types are evidence.

    Watch heartbeats, file mtimes, logs, and uncommitted JSON fragments do not
    establish business activity.  The previous local day covers midnight.
    """
    if not safe_tag(tag) or not _number(now):
        return None
    # The collector's filenames and room_tag use RoomConfig.file_tag(), not
    # the human-facing roster tag. A known collector can supply the numeric
    # fallback for labels consisting only of symbols/emoji.
    file_tag = sanitize_label(tag) if file_tag is None else file_tag
    if not safe_tag(file_tag) or sanitize_label(file_tag) != file_tag:
        return None
    labels = {tag, file_tag}
    days = {time.strftime("%Y-%m-%d", time.localtime(t))
            for t in (now, now - EVIDENCE_MAX_AGE_SECONDS)}
    newest = None
    for stream, types in BUSINESS_TYPES.items():
        for day in days:
            path = Path(data_dir) / f"{stream}_douyin_{file_tag}_{day}.jsonl"
            for row in _tail_records(path):
                ts = row.get("ts")
                row_tag, room_tag = row.get("tag", file_tag), row.get("room_tag", file_tag)
                if (row.get("platform", "douyin") == "douyin"
                        and isinstance(row_tag, str) and row_tag in labels
                        and isinstance(room_tag, str) and room_tag in labels
                        and isinstance(row.get("type"), str) and row["type"] in types
                        and fresh_timestamp(ts, now)):
                    newest = max(newest or 0, ts)
    return newest


def business_filename_unique(roster_path, tag, file_tag=None):
    """Do not attribute a shared collector filename to one target.

    This does not rename or migrate data. Windows filenames are case-insensitive;
    a second roster label mapping to the same file tag makes activity ambiguous.
    """
    file_tag = sanitize_label(tag) if file_tag is None else file_tag
    if not safe_tag(file_tag):
        return False
    try:
        with Path(roster_path).open("rb") as handle:
            raw = handle.read(1024 * 1024 + 1)
        doc = json.loads(raw, object_pairs_hook=_unique_object)
        rows = doc.get("anchors") if isinstance(doc, dict) else None
        if len(raw) > 1024 * 1024 or not isinstance(rows, list):
            return False
        for row in rows:
            other = row.get("tag") if isinstance(row, dict) else None
            if not isinstance(other, str):
                return False
            if other != tag and sanitize_label(other).casefold() == file_tag.casefold():
                return False
        return True
    except (OSError, UnicodeError, ValueError):
        return False


@dataclass(frozen=True)
class NotificationEvent:
    action: str
    token: int
    probe: str


class MonitoringAlertState:
    """One watcher/target owner; successful sends deduplicate an incident.

    Failed delivery keeps the incident and recovery pending.  Retries have
    60-second and 5-minute delays; after the third failure, subsequent attempts
    are limited to one per 30 minutes.  A new valid observation is required for
    each recovery attempt.  Disabling/retiring a target never emits recovery.
    """
    def __init__(self, started_at):
        self.started_at = started_at
        self.last_now = started_at
        self.last_probe_ts = None
        self._token = 0
        self._clear_incident()

    def _clear_incident(self):
        self.unknown_since = None
        self.unknown_rounds = 0
        self.first_unknown_epoch = None
        self.last_unknown_epoch = None
        self.warning_sent = False
        self._retry = {action: {"failures": 0, "next_at": 0.0}
                       for action in ("warning", "recovery")}
        self._pending = None
        self.status = "normal"

    def retire(self):
        self.started_at = None
        self.last_probe_ts = None
        self._clear_incident()
        self.status = "inactive"

    def pause(self):
        """An unreadable scope cannot prove a continuous enabled observation."""
        self._pending = None
        if not self.warning_sent:
            self.unknown_since, self.unknown_rounds = None, 0
            self.first_unknown_epoch = self.last_unknown_epoch = None
        self.status = "scope_unavailable"

    def observe(self, *, now, wall_now, probe, probe_ts, business_ts=None, active=True):
        if not active:
            self.retire()
            return None
        if not (_number(now) and _number(wall_now)):
            return None
        if self.started_at is None or now < self.last_now:
            self.started_at = now
            self.last_probe_ts = None
            self._clear_incident()
        self.last_now = now
        # Never reuse an unacknowledged proposal as a second send.
        if self._pending is not None:
            return None
        if not fresh_timestamp(probe_ts, wall_now):
            return None
        if self.last_probe_ts is not None and probe_ts <= self.last_probe_ts:
            return None
        self.last_probe_ts = probe_ts
        confirmed = probe in ("live", "offline")
        if confirmed:
            self.unknown_since, self.unknown_rounds = None, 0
            if not self.warning_sent:
                self._clear_incident()
                return None
            # Older evidence must not resolve a later incident.
            if probe_ts <= self.last_unknown_epoch:
                return None
            action = "recovery"
        else:
            if self.unknown_since is None:
                self.unknown_since = now
                self.first_unknown_epoch = probe_ts
            self.last_unknown_epoch = probe_ts
            self.unknown_rounds += 1
            if self.warning_sent:
                self.status = "warning_sent"
                return None
            if (now - self.started_at < STARTUP_GRACE_SECONDS
                    or now - self.unknown_since < UNKNOWN_SECONDS
                    or self.unknown_rounds < UNKNOWN_ROUNDS
                    or fresh_timestamp(business_ts, wall_now)):
                self.status = "observing"
                return None
            action = "warning"
        self.status = action + "_delivery_pending"
        if now < self._retry[action]["next_at"]:
            return None
        self._token += 1
        event = NotificationEvent(action, self._token, probe if confirmed else "unknown")
        self._pending = event
        return event

    def acknowledge(self, event, delivered, now):
        if event != self._pending:
            return False
        self._pending = None
        if delivered is True:
            if event.action == "recovery":
                self._clear_incident()
            else:
                self.warning_sent = True
                self.status = "warning_sent"
            return True
        retry = self._retry[event.action]
        retry["failures"] += 1
        retry["next_at"] = now + RETRY_DELAYS[min(retry["failures"], len(RETRY_DELAYS)) - 1]
        self.status = event.action + "_delivery_pending"
        return False

    def cancel(self, event):
        """Discard a not-yet-sent proposal after scope/ownership became uncertain."""
        if event == self._pending:
            self._pending = None

    def snapshot(self):
        return {"status": self.status, "unknown_rounds": self.unknown_rounds,
                "warning_delivered": self.warning_sent}


def notification_text(event, tag):
    label = tag if safe_tag(tag) else "当前主播"
    if event.action == "warning":
        return ("[竞品监控][需人工检查] %s：持续至少10分钟、多轮无法确认直播状态，"
                "近3分钟未发现可确认属于此目标的新的人气或互动记录。请检查专用浏览器中的登录/验证、"
                "网络及平台页面；原因尚未确认，中断期间可能缺失资料。" % label)
    state = "在播" if event.probe == "live" else "未在播"
    return ("[竞品监控][探测恢复] %s：新鲜有效响应已重新确认%s。"
            "请在看板确认采集资料继续更新；本通知不表示中断资料已经补齐。" % (label, state))


def deliver_event(state, event, tag, now, send, *, permitted=None):
    """Synchronous bounded sender; a False/exception never becomes success."""
    try:
        options = {"key": "monitoring-%s-%s" % (event.action, tag), "cooldown": 0}
        if permitted is not None:
            options["permitted"] = permitted
        delivered = send(notification_text(event, tag), **options) is True
    except Exception:
        delivered = False
    state.acknowledge(event, delivered, now)
    return delivered
