"""In-memory sessions for the loopback-only customer Board, without accounts."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import threading
import time
from urllib.parse import urlsplit


class LocalAccess:
    ttl = 8 * 60 * 60
    max_sessions = 32

    def __init__(self, namespace):
        suffix = hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:16]
        self.cookie_name = "dy_local_" + suffix
        self._sessions = {}
        self._lock = threading.RLock()

    @staticmethod
    def _one(headers, name, maximum=512):
        values = headers.get_all(name, [])
        if len(values) != 1 or not isinstance(values[0], str) or len(values[0]) > maximum:
            return None
        return values[0]

    def _authority(self, request):
        port = request.server.server_address[1]
        host = self._one(request.headers, "Host")
        for alias in ("127.0.0.1", "localhost"):
            if host == f"{alias}:{port}" or (port == 80 and host == alias):
                return f"{alias}:{port}"
        return None

    def source_ok(self, request):
        """Check actual peer, unambiguous authority and browser metadata first."""
        try:
            if not ipaddress.ip_address(request.client_address[0]).is_loopback:
                return False
            target = urlsplit(request.path)
            if (not request.path.startswith("/") or request.path.startswith("//")
                    or target.scheme or target.netloc or target.fragment or "#" in request.path
                    or ";" in target.path or any(ord(char) < 32 or ord(char) == 127 for char in request.path)):
                return False
            port = request.server.server_address[1]
            authority = self._authority(request)
            if authority is None:
                return False
            # This local entry has no reverse-proxy mode. Never inherit its trust.
            if any(name.lower() == "forwarded" or name.lower().startswith("x-forwarded-")
                   for name in request.headers.keys()):
                return False
            if request.headers.get_all("Origin", []):
                origins = {"http://" + authority}
                if port == 80:
                    origins.add("http://" + authority[:-3])
                if self._one(request.headers, "Origin") not in origins:
                    return False
            if request.headers.get_all("Sec-Fetch-Site", []):
                if self._one(request.headers, "Sec-Fetch-Site") not in ("same-origin", "none"):
                    return False
            return len(request.headers.get_all("Cookie", [])) <= 1
        except (ValueError, TypeError, IndexError, AttributeError):
            return False

    def _cookie(self, request):
        raw = self._one(request.headers, "Cookie", 8192)
        if raw is None:
            return None
        values = []
        for part in raw.split(";"):
            name, separator, value = part.strip().partition("=")
            if separator and name == self.cookie_name:
                values.append(value)
        return values[0] if len(values) == 1 and len(values[0]) <= 128 else None

    def _current(self, request):
        session = self._sessions.get(self._cookie(request))
        if (session and session["expires"] > time.monotonic()
                and session["host"] == self._authority(request)):
            return session
        return None

    def authenticated(self, request):
        with self._lock:
            return self._current(request) is not None

    def write_allowed(self, request):
        supplied = self._one(request.headers, "X-Local-Write-Token", 128)
        if supplied is None or not supplied.isascii():
            return False
        with self._lock:
            session = self._current(request)
            return bool(session and hmac.compare_digest(session["write_token"], supplied))

    def bootstrap(self, request):
        with self._lock:
            now = time.monotonic()
            self._sessions = {key: value for key, value in self._sessions.items() if value["expires"] > now}
            key = self._cookie(request)
            session = self._current(request)
            if session is None:
                while len(self._sessions) >= self.max_sessions:
                    self._sessions.pop(next(iter(self._sessions)))
                key = secrets.token_urlsafe(32)
                session = {"write_token": secrets.token_urlsafe(32), "expires": now + self.ttl,
                           "host": self._authority(request)}
                self._sessions[key] = session
            return ({"mode": "local", "user": "本机用户", "role": "local", "write_token": session["write_token"]},
                    f"{self.cookie_name}={key}; HttpOnly; SameSite=Strict; Path=/")
