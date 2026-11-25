"""Explicit, bounded login guidance for the current owned customer browser."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import secrets
import time


_CHECK_STATES = {"channel_live", "channel_offline", "restricted", "unknown"}
_REASONS = {"ownership_changed", "cdp_unavailable", "timeout", "target_missing", "unexpected_origin",
    "invalid_target", "profile_unconfirmed", "profile_restricted", "profile_live", "profile_offline",
    "page_opened", "dependency_missing", "action_failed", "target_ambiguous", "target_mismatch"}


class LoginGuide:
    evidence_ttl = 60.0
    prepare_timeout = 5.0

    def __init__(self, manager, *, driver_factory=None, wall_time=time.time):
        self.manager = manager
        self.driver_factory = driver_factory
        self.wall_time = wall_time
        self.busy = False
        self.action = ""
        self.action_id = 0
        self._owner = None
        self._target_id = None
        self._marker = secrets.token_hex(24)
        self._driver = None
        self._expires = 0.0
        self._check_target = None
        self._channel = self._empty("not_checked", "not_checked")

    @staticmethod
    def _empty(state, reason, tag=""):
        return {"state": state, "reason": reason, "target_tag": tag, "checked_at": None, "source": "none"}

    def _invalidate(self, state="unknown", reason="browser_changed", tag=""):
        self._channel = self._empty(state, reason, tag)
        self._expires = 0.0

    def _synchronize(self):
        owner = self.manager.login_owner()
        if owner is not None and self._owner is not None and owner != self._owner:
            self._owner, self._target_id, self._driver = None, None, None
            self._marker = secrets.token_hex(24)
            self._invalidate()
        elif owner is None and (self._channel["checked_at"] is not None
                                or self._channel["state"] == "page_opened"):
            self._invalidate(reason="application_stopping" if self.manager.stopping else "browser_unverified")
        if self._channel["checked_at"] is not None and not self._target_matches():
            self._invalidate(reason="target_changed")
        if self._channel["state"] in ("channel_live", "channel_offline") and self.manager.now() >= self._expires:
            self._channel["state"], self._channel["reason"] = "expired", "evidence_expired"
        return owner

    def _target_matches(self):
        if self._check_target is None:
            return False
        try:
            # Roster writers replace atomically. A bounded direct read avoids
            # holding manager.lock while waiting for the roster's writer lock.
            with Path(self.manager.context["roster"]).open("rb") as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                return False
            roster = json.loads(raw.decode("utf-8-sig"))
            tag, uid = self._check_target
            rows = [row for row in roster["anchors"] if isinstance(row, dict)
                    and str(row.get("tag") or "").strip() == tag]
            return len(rows) == 1 and str(rows[0].get("sec_uid") or "").strip() == uid
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            return False

    def snapshot(self):
        with self.manager.lock:
            self._synchronize()
            return {"browser": self.manager.login_browser_status(), "channel": copy.deepcopy(self._channel),
                "busy": self.busy, "action": self.action, "action_id": self.action_id,
                "login_confirmed": False, "collection_recovered": False}

    def _response(self, ok=False, *, finished=False):
        status = self.snapshot()
        if finished:
            # The action still owns its slot until finally, but the completed
            # response must not leave the client displaying an active operation.
            status["busy"] = False
        return {"ok": bool(ok), "status": status}

    def _guard(self, action_id, owner):
        with self.manager.lock:
            return (not self.manager.stopping and self.busy and self.action_id == action_id
                    and self.manager.login_owner() == owner
                    and (self.action != "check" or self._target_matches()))

    def _wait_owner(self):
        until = time.monotonic() + self.prepare_timeout
        while True:
            with self.manager.lock:
                if self.manager.stopping:
                    return None
                owner = self.manager.login_owner()
                if owner is not None:
                    return owner
                if self.manager.login_browser_status()["state"] in ("missing", "conflict", "failed", "identity_unknown", "closed"):
                    return None
            if time.monotonic() >= until:
                return None
            time.sleep(.05)

    def _bind(self, owner):
        if self._owner != owner:
            self._owner, self._target_id, self._driver = owner, None, None
            self._marker = secrets.token_hex(24)
        if self._driver is None:
            if self.driver_factory is None:
                from login_browser import LoginBrowser
                self._driver = LoginBrowser(self.manager.context)
            else:
                self._driver = self.driver_factory(self.manager.context)
        return self._driver

    def request(self, action, entry=None):
        # HTTP validates the roster/body first; this layer still accepts no URL,
        # arbitrary script or caller-provided CDP target.
        with self.manager.lock:
            if self.manager.stopping:
                self._invalidate(reason="application_stopping")
                return self._response()
            if action not in ("prepare", "open", "check"):
                self._invalidate(reason="invalid_action")
                return self._response()
            if self.busy:
                return self._response()  # One accepted action owns the browser at a time.
            self.action_id += 1
            sequence = self.action_id
            self.action = action
            self._check_target = None
            tag = entry.get("tag", "") if isinstance(entry, dict) else ""
            uid = entry.get("sec_uid", "") if isinstance(entry, dict) else ""
            if action == "check" and (not isinstance(tag, str) or not tag or not isinstance(uid, str)
                    or not 40 <= len(uid) <= 256 or not re.fullmatch(r"MS4wLjABAAAA[A-Za-z0-9_-]+={0,2}", uid)):
                self._invalidate("need_target", "target_required")
                return self._response()
            if action == "check":
                self._check_target = (tag, uid)
            self._invalidate("checking" if action == "check" else "not_checked",
                             "checking" if action == "check" else "prepare_requested", tag if action == "check" else "")
            if action in ("prepare", "open"):
                accepted = self.manager.request_login_browser()
                if action == "prepare" or not accepted:
                    return self._response(accepted)
            self.busy = True

        owner = None
        try:
            owner = self._wait_owner() if action == "open" else self.manager.login_owner()
            with self.manager.lock:
                if owner is None or not self._guard(sequence, owner):
                    self._invalidate(reason="application_stopping" if self.manager.stopping else "browser_not_ready", tag=tag)
                    return self._response(finished=True)
                driver = self._bind(owner)
                target_id, marker = self._target_id, self._marker
                if action == "check" and not target_id:
                    self._invalidate(reason="login_page_required", tag=tag)
                    return self._response(finished=True)

            def guard():
                return self._guard(sequence, owner)

            def remember_target(value):
                if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
                    return False
                with self.manager.lock:
                    if not self._guard(sequence, owner):
                        return False
                    self._target_id = value
                    return True

            if action == "open":
                result = driver.open(target_id, marker, guard, remember_target)
            else:
                result = driver.check(target_id, uid, guard)
            with self.manager.lock:
                if not self._guard(sequence, owner):
                    self._invalidate(reason="application_stopping" if self.manager.stopping else "browser_changed", tag=tag)
                    return self._response(finished=True)
                result = result if isinstance(result, dict) else {}
                state = result.get("state")
                reason = result.get("reason")
                if reason not in _REASONS:
                    reason = "action_failed"
                if action == "open":
                    if state != "page_opened":
                        state = "restricted" if state == "restricted" else "unknown"
                    self._channel = self._empty(state, reason)
                else:
                    state = state if state in _CHECK_STATES else "unknown"
                    self._channel = {"state": state, "reason": reason, "target_tag": tag,
                        "checked_at": self.wall_time(), "source": "owned_browser_profile_api"}
                    self._expires = self.manager.now() + self.evidence_ttl
                return self._response(state in ("page_opened", "channel_live", "channel_offline"), finished=True)
        except Exception:
            with self.manager.lock:
                self._invalidate(reason="application_stopping" if self.manager.stopping else "action_failed", tag=tag)
                return self._response(finished=True)
        finally:
            with self.manager.lock:
                self.busy = False
