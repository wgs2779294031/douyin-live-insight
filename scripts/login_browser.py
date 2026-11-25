"""Bounded CDP actions for one dedicated customer login page, without accounts."""
from __future__ import annotations

import http.client
import json
import re
import socket
import threading
import time
from urllib.parse import quote, urlsplit

LOGIN_URL = "https://www.douyin.com/"
LOGIN_ORIGIN = "https://www.douyin.com"
PROFILE_API = ("https://www.douyin.com/aweme/v1/web/user/profile/other/"
               "?device_platform=webapp&aid=6383&channel=channel_pc_web"
               "&publish_video_strategy_type=2&source=channel_pc_web"
               "&sec_user_id={sec}&update_version_code=170400")
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_MARKER = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_UID = re.compile(r"MS4wLjABAAAA[A-Za-z0-9_-]+={0,2}\Z")
_STATES = {"channel_live": "profile_live", "channel_offline": "profile_offline",
           "restricted": "profile_restricted"}
_UNKNOWN_REASONS = {"unexpected_origin", "target_mismatch", "profile_unconfirmed", "timeout"}
_REASONS = _UNKNOWN_REASONS | {"ownership_changed", "cdp_unavailable", "dependency_missing",
    "invalid_target", "target_missing", "target_ambiguous"}
_ACTION_SECONDS = 8.0


class _Failure(RuntimeError):
    pass


class _Action:
    def __init__(self, guard):
        self.guard = guard
        self.deadline = time.monotonic() + _ACTION_SECONDS
        self._lock = threading.Lock()
        self._sockets = set()
        self._timer = None
        self._closed = False

    @staticmethod
    def _abort(stream):
        # HTTP makefile() can keep the descriptor alive after socket.close().
        # shutdown, even on that closed socket object, interrupts its reader.
        try:
            stream.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            stream.close()
        except OSError:
            pass

    def _expire(self):
        with self._lock:
            streams = tuple(self._sockets)
        for stream in streams:
            self._abort(stream)

    def own(self, stream):
        # Keep the exact object until action cleanup; never look up a descriptor
        # or close a browser/global transport. Late connects are closed as well.
        with self._lock:
            expired = self._closed or time.monotonic() >= self.deadline
            if not expired:
                self._sockets.add(stream)
                if self._timer is None:
                    self._timer = threading.Timer(self.deadline - time.monotonic(), self._expire)
                    self._timer.daemon = True
                    self._timer.start()
        if expired:
            self._abort(stream)
            raise _Failure("timeout")
        return stream

    def close(self):
        with self._lock:
            self._closed = True
            streams, self._sockets = tuple(self._sockets), set()
            timer = self._timer
            if timer is not None:
                timer.cancel()
        for stream in streams:
            self._abort(stream)
        if timer is not None:
            timer.join()  # No cancellation thread may outlive a returned action.

    def failure(self, error):
        # An interrupted library read may report EOF/OSError rather than timeout.
        if time.monotonic() >= self.deadline:
            return _Failure("timeout")
        return error

    def check(self):
        if self.guard() is not True:
            raise _Failure("ownership_changed")
        if time.monotonic() >= self.deadline:
            raise _Failure("timeout")

    def timeout(self, maximum=2.0):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise _Failure("timeout")
        return min(maximum, remaining)


class _Socket:
    def __init__(self, connection, action):
        self.connection, self.action, self.sequence = connection, action, 0

    def send(self, method, parameters):
        self.action.check()
        self.sequence += 1
        self.connection.settimeout(self.action.timeout())
        self.connection.send(json.dumps({"id": self.sequence, "method": method, "params": parameters}))
        for _ in range(64):
            self.connection.settimeout(self.action.timeout(8.0))
            raw = self.connection.recv()
            self.action.timeout()
            if not isinstance(raw, (str, bytes)) or len(raw) > 128 * 1024:
                raise _Failure("cdp_unavailable")
            message = json.loads(raw)
            if isinstance(message, dict) and message.get("id") == self.sequence:
                self.action.check()
                if "error" in message or not isinstance(message.get("result"), dict):
                    raise _Failure("cdp_unavailable")
                return message["result"]
        raise _Failure("cdp_unavailable")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.connection.shutdown()  # Close only this CDP socket, without a close handshake wait.


def _profile_script(sec_uid):
    # The existing watcher uses this profile API and live_status=0/room evidence.
    # Project only a coarse channel verdict inside the page; never return raw data.
    return r"""(async () => {
      const expected = %s, api = %s, origin = %s;
      if (location.origin !== origin) return {state:'unknown', reason:'unexpected_origin'};
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 1800);
      try {
        const response = await fetch(api, {credentials:'include', cache:'no-store',
          redirect:'error', signal:controller.signal});
        if ([401,403].includes(response.status)) return {state:'restricted'};
        if (!response.ok) return {state:'unknown', reason:'profile_unconfirmed'};
        const data = await response.json();
        if (!data || typeof data !== 'object') return {state:'unknown', reason:'profile_unconfirmed'};
        if (data.status_code !== 0) return {state:'restricted'};
        const user = data.user;
        if (!user || typeof user !== 'object' || Object.keys(user).length === 0)
          return {state:'restricted'};
        if (user.sec_uid !== expected) return {state:'unknown', reason:'target_mismatch'};
        let room = user.room_data;
        if (typeof room === 'string') {
          try { room = JSON.parse(room); }
          catch (_) { return {state:'unknown', reason:'profile_unconfirmed'}; }
        }
        if (room != null && (typeof room !== 'object' || Array.isArray(room)))
          return {state:'unknown', reason:'profile_unconfirmed'};
        if (room?.owner?.sec_uid && room.owner.sec_uid !== expected)
          return {state:'unknown', reason:'target_mismatch'};
        if (user.live_status === 0 && room?.status !== 2) return {state:'channel_offline'};
        const rid = room?.owner?.web_rid;
        if (user.live_status === 1 && typeof rid === 'string' && /^[1-9][0-9]*$/.test(rid))
          return {state:'channel_live'};
        return {state:'unknown', reason:'profile_unconfirmed'};
      } catch (error) {
        return {state:'unknown', reason:error?.name === 'AbortError' ? 'timeout' : 'profile_unconfirmed'};
      } finally { clearTimeout(timer); }
    })()""" % (json.dumps(sec_uid), json.dumps(PROFILE_API.format(sec=quote(sec_uid, safe=""))),
                json.dumps(LOGIN_ORIGIN))


class LoginBrowser:
    def __init__(self, context):
        self.port = context["cdp_port"]
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("invalid CDP port")
        self._owned_targets = set()

    def _http(self, path, action):
        action.check()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=action.timeout())
        response = None
        try:
            connection.connect()
            action.own(connection.sock)
            connection.request("GET", path, headers={"Connection": "close"})
            response = connection.getresponse()
            if response.status != 200:
                raise _Failure("cdp_unavailable")
            chunks, size = [], 0
            while True:
                # HTTPConnection may detach its socket after Connection: close.
                stream = getattr(getattr(response.fp, "raw", None), "_sock", None)
                if stream is not None:
                    stream.settimeout(action.timeout())
                else:
                    action.timeout()
                chunk = response.read1(min(8192, 256 * 1024 + 1 - size))
                action.timeout()
                size += len(chunk)
                if size > 256 * 1024:
                    raise _Failure("cdp_unavailable")
                if not chunk:
                    break
                chunks.append(chunk)
            action.check()
            return b"".join(chunks)
        finally:
            if response is not None:
                response.close()
            connection.close()

    def _json(self, path, action):
        return json.loads(self._http(path, action))

    def _socket(self, url, action, *, kind, target_id=None):
        action.check()
        parsed = urlsplit(url)
        expected = f"127.0.0.1:{self.port}"
        prefix = "/devtools/" + kind + "/"
        suffix = parsed.path.removeprefix(prefix)
        if (parsed.scheme != "ws" or parsed.netloc != expected or parsed.query or parsed.fragment
                or not parsed.path.startswith(prefix) or not _ID.fullmatch(suffix)
                or target_id is not None and suffix != target_id):
            raise _Failure("cdp_unavailable")
        import websocket
        # Own the transport before websocket-client starts its blocking handshake.
        stream = action.own(socket.create_connection(("127.0.0.1", self.port), timeout=action.timeout()))
        connection = websocket.create_connection(url, socket=stream, timeout=action.timeout(),
            suppress_origin=True, enable_multithread=True,
            http_no_proxy=["127.0.0.1", "localhost"], redirect_limit=0)
        try:
            if connection.getstatus() != 101:
                raise _Failure("cdp_unavailable")
            action.check()
        except BaseException:
            connection.shutdown()
            raise
        return _Socket(connection, action)

    def _targets(self, action):
        targets = self._json("/json/list", action)
        if not isinstance(targets, list) or len(targets) > 128:
            raise _Failure("cdp_unavailable")
        return [target for target in targets if isinstance(target, dict) and target.get("type") == "page"]

    @staticmethod
    def _failure(error):
        if isinstance(error, _Failure):
            reason = str(error) if str(error) in _REASONS else "cdp_unavailable"
        elif isinstance(error, ImportError):
            reason = "dependency_missing"
        elif isinstance(error, TimeoutError) or type(error).__name__ == "WebSocketTimeoutException":
            reason = "timeout"
        else:
            reason = "cdp_unavailable"
        return {"state": "unknown", "reason": reason}

    def open(self, target_id, marker, guard, remember_target):
        action = _Action(guard)
        try:
            action.check()
            if not isinstance(marker, str) or not _MARKER.fullmatch(marker):
                raise _Failure("invalid_target")
            if target_id is not None and (not isinstance(target_id, str) or not _ID.fullmatch(target_id)
                                          or target_id not in self._owned_targets):
                raise _Failure("invalid_target")
            targets = self._targets(action)
            target = next((row for row in targets if row.get("id") == target_id), None) if target_id else None
            marker_url = "about:blank#local-login-" + marker
            if target is None:
                matches = [row for row in targets if row.get("url") == marker_url]
                if len(matches) > 1:
                    raise _Failure("target_ambiguous")
                target = matches[0] if matches else None
                if target:
                    target_id = target.get("id")
                else:
                    version = self._json("/json/version", action)
                    with self._socket(version.get("webSocketDebuggerUrl", ""), action, kind="browser") as browser:
                        result = browser.send("Target.createTarget", {"url": marker_url})
                    target_id = result.get("targetId")
                if not isinstance(target_id, str) or not _ID.fullmatch(target_id):
                    raise _Failure("cdp_unavailable")
                self._owned_targets.add(target_id)
                action.check()
                if remember_target(target_id) is False:
                    raise _Failure("ownership_changed")
                action.check()
            # Activate only our recorded page before navigation: an initially
            # backgrounded browser may otherwise defer its first HTTP navigation.
            self._http("/json/activate/" + target_id, action)
            with self._socket(f"ws://127.0.0.1:{self.port}/devtools/page/{target_id}", action,
                              kind="page", target_id=target_id) as page:
                page.send("Page.enable", {})
                result = page.send("Page.navigate", {"url": LOGIN_URL})
                if result.get("errorText"):
                    raise _Failure("cdp_unavailable")
            action.check()
            return {"state": "page_opened", "reason": "page_opened", "target_id": target_id}
        except Exception as error:
            return self._failure(action.failure(error))
        finally:
            action.close()

    def check(self, target_id, sec_uid, guard):
        action = _Action(guard)
        try:
            action.check()
            if (not isinstance(target_id, str) or not _ID.fullmatch(target_id)
                    or target_id not in self._owned_targets):
                raise _Failure("target_missing")
            if not isinstance(sec_uid, str) or not 40 <= len(sec_uid) <= 256 or not _UID.fullmatch(sec_uid):
                raise _Failure("invalid_target")
            target = next((row for row in self._targets(action) if row.get("id") == target_id), None)
            if target is None:
                raise _Failure("target_missing")
            expected_origin = LOGIN_ORIGIN
            current = urlsplit(target.get("url", ""))
            if current.scheme + "://" + current.netloc != expected_origin:
                raise _Failure("unexpected_origin")
            with self._socket(target.get("webSocketDebuggerUrl", ""), action,
                              kind="page", target_id=target_id) as page:
                result = page.send("Runtime.evaluate", {"expression": _profile_script(sec_uid),
                    "returnByValue": True, "awaitPromise": True, "userGesture": True})
            action.check()
            if result.get("exceptionDetails"):
                raise _Failure("cdp_unavailable")
            value = (result.get("result") or {}).get("value")
            if not isinstance(value, dict):
                raise _Failure("profile_unconfirmed")
            state = value.get("state")
            if state in _STATES:
                return {"state": state, "reason": _STATES[state]}
            reason = value.get("reason")
            return {"state": "unknown", "reason": reason if reason in _UNKNOWN_REASONS else "profile_unconfirmed"}
        except Exception as error:
            return self._failure(action.failure(error))
        finally:
            action.close()
