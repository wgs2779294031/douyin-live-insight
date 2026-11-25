"""Resolve recording CDN hosts through the active physical uplink's own DNS.

No adapter, route, DNS setting, URL, Host header or TLS policy is changed. The
session remains owned by the caller, which must close this wrapper after use.
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urldefrag, urljoin, urlsplit


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_FAKE_V4 = ipaddress.ip_network("198.18.0.0/15")
_REDIRECTS = {301, 302, 303, 307, 308}
DNS_PROVIDER_REASONS = frozenset({
    "uplink_dns_query_failed", "uplink_dns_query_timeout", "uplink_dns_reply_invalid",
})


class StreamNetworkError(RuntimeError):
    """Fixed, safe reason; never include resolver output or signed URLs."""

    def __init__(self, reason, *, code=None, response=None, dns_reason=None):
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.response = response
        self.dns_reason = dns_reason


@dataclass(frozen=True)
class AddressRecord:
    address: str
    ttl: float = 0


def _public_address(value):
    try:
        address = ipaddress.ip_address(value)
    except (ValueError, TypeError):
        return None
    if (not address.is_global or address.is_multicast or address.is_reserved
            or address.is_unspecified or address.is_loopback or address.is_link_local):
        return None
    v4 = address if address.version == 4 else address.ipv4_mapped
    if v4 is not None and (v4 in _FAKE_V4 or not v4.is_global):
        return None
    return str(address)


def _host_name(value):
    try:
        name = value.encode("idna").decode("ascii").lower()
    except (AttributeError, UnicodeError):
        raise StreamNetworkError("invalid_stream_host") from None
    labels = name.rstrip(".").split(".")
    if (len(name) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", x)
                               for x in labels)):
        raise StreamNetworkError("invalid_stream_host")
    return name


class WindowsDnsProvider:
    """Query only DNS servers already attached to connected default uplinks."""

    def __init__(self, *, runner=subprocess.run, timeout=15):
        if not 0 < timeout <= 15:
            raise ValueError("dns_timeout_out_of_range")
        self.runner, self.timeout = runner, timeout

    def resolve(self, hostname):
        hostname = _host_name(hostname)
        scripts_dir = os.path.dirname(os.path.abspath(__file__))
        worker = os.path.join(scripts_dir, "stream_dns_worker.py")
        command = [sys.executable, "-B", "-E", "-s", worker, hostname]
        timed_out = False
        try:
            try:
                result = self.runner(command, cwd=os.path.dirname(scripts_dir), capture_output=True,
                                     timeout=self.timeout, creationflags=NO_WINDOW)
            except subprocess.TimeoutExpired as error:
                timed_out = True
                raw = error.stdout or b""
            else:
                if result.returncode:
                    raise StreamNetworkError("uplink_dns_query_failed")
                raw = result.stdout

            if not isinstance(raw, (bytes, str)):
                raise TypeError("invalid_dns_output_type")
            if len(raw) > 65536:
                raise StreamNetworkError("uplink_dns_reply_invalid")
            if timed_out:
                # Keep only complete output from lookups that finished before the deadline.
                newline = b"\n" if isinstance(raw, bytes) else "\n"
                raw = raw.rpartition(newline)[0]
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8-sig")
            if not raw.strip():
                raise ValueError("empty_dns_output")

            records = []
            for line in raw.splitlines():
                payload = json.loads(line)
                batch = payload.get("records") if isinstance(payload, dict) else None
                if not isinstance(batch, list):
                    raise StreamNetworkError("uplink_dns_reply_invalid")
                records.extend(AddressRecord(row.get("address"), row.get("ttl", 0))
                               for row in batch if isinstance(row, dict))
            if timed_out and not any(_public_address(record.address) for record in records):
                raise StreamNetworkError("uplink_dns_query_timeout")
            return records
        except StreamNetworkError:
            if timed_out:
                raise StreamNetworkError("uplink_dns_query_timeout") from None
            raise
        except (OSError, UnicodeError, ValueError, TypeError):
            reason = "uplink_dns_query_timeout" if timed_out else "uplink_dns_query_failed"
            raise StreamNetworkError(reason) from None


class _Response:
    def __init__(self, raw, owner):
        self._raw, self._owner, self._closed = raw, owner, False

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._owner._responses.discard(self)
        try:
            self._raw.close()
        except Exception:
            raise StreamNetworkError("stream_response_close_failed") from None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class DirectDnsSession:
    """Small synchronous GET wrapper; redirects are resolved one host at a time."""

    def __init__(self, base, provider=None, *, resolve_option=None, proxy_option=None, clock=time.monotonic,
                 max_redirects=5, max_cache_ttl=60):
        if type(max_redirects) is not int or not 0 <= max_redirects <= 10:
            raise ValueError("redirect_limit_out_of_range")
        if not 0 < max_cache_ttl <= 60:
            raise ValueError("dns_cache_ttl_out_of_range")
        if resolve_option is None or proxy_option is None:
            from curl_cffi.const import CurlOpt
            if resolve_option is None:
                resolve_option = CurlOpt.RESOLVE
            if proxy_option is None:
                proxy_option = CurlOpt.PROXY
        self._base, self.provider = base, provider or WindowsDnsProvider()
        self._resolve_option, self._clock = resolve_option, clock
        self._proxy_option = proxy_option
        self.max_redirects, self.max_cache_ttl = max_redirects, max_cache_ttl
        self._cache, self._responses, self._closed = {}, set(), False
        self._lock = threading.RLock()

    def _addresses(self, hostname):
        cached = self._cache.get(hostname)
        if cached is not None and cached[0] > self._clock():
            return cached[1]
        self._cache.pop(hostname, None)
        try:
            resolver = getattr(self.provider, "resolve", self.provider)
            records = resolver(hostname)
        except StreamNetworkError as error:
            detail = error.reason
            if not isinstance(detail, str) or detail not in DNS_PROVIDER_REASONS:
                detail = None
            raise StreamNetworkError("uplink_dns_resolution_failed", dns_reason=detail) from None
        except Exception:
            raise StreamNetworkError("uplink_dns_resolution_failed") from None
        if not isinstance(records, (list, tuple)):
            raise StreamNetworkError("uplink_dns_reply_invalid")
        addresses, ttls = [], []
        for row in records:
            if isinstance(row, dict):
                row = AddressRecord(row.get("address"), row.get("ttl", 0))
            if not isinstance(row, AddressRecord):
                continue
            address = _public_address(row.address)
            if address is None:
                continue
            ttl = row.ttl
            if type(ttl) not in (int, float) or not math.isfinite(ttl) or ttl < 0:
                ttl = 0
            if address not in addresses:
                addresses.append(address)
            ttls.append(ttl)
        if not addresses:
            raise StreamNetworkError("uplink_dns_no_public_address")
        ttl = min([self.max_cache_ttl, *ttls])
        result = tuple(addresses)
        if ttl > 0:
            if len(self._cache) >= 128:
                self._cache.pop(next(iter(self._cache)))
            self._cache[hostname] = (self._clock() + ttl, result)
        return result

    def _mapping(self, url):
        try:
            if not isinstance(url, str) or any(ord(char) <= 32 for char in url):
                raise ValueError()
            parts = urlsplit(url)
            if (parts.scheme not in ("http", "https") or not parts.hostname
                    or parts.username is not None or parts.password is not None):
                raise ValueError()
            hostname = parts.hostname
            port = parts.port
            if port is None:
                port = 443 if parts.scheme == "https" else 80
            if not 1 <= port <= 65535 or "%" in hostname:
                raise ValueError()
        except (ValueError, TypeError):
            raise StreamNetworkError("invalid_stream_url") from None
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            # curl also accepts legacy IPv4 forms such as 127.1 or 0x7f000001.
            # They must not bypass the literal check by posing as DNS names.
            number = r"(?:0x[0-9a-f]+|[0-9]+)"
            if re.fullmatch(number + r"(?:\." + number + r"){0,3}\.?", hostname, re.I):
                raise StreamNetworkError("noncanonical_stream_ip")
            hostname = _host_name(hostname)
            addresses = self._addresses(hostname)
            formatted = ["[" + value + "]" if ":" in value else value for value in addresses]
            return [hostname + ":" + str(port) + ":" + ",".join(formatted)]
        if _public_address(hostname) is None:
            raise StreamNetworkError("stream_ip_not_public")
        return None

    def _get_once(self, url, kwargs):
        mapping = self._mapping(url)
        original = getattr(self._base, "curl_options", None)
        options = dict(original or {})
        # libcurl otherwise inherits proxy environment variables. This wrapper
        # deliberately uses the physical direct uplink, without changing them.
        options[self._proxy_option] = ""
        if mapping is None:
            options.pop(self._resolve_option, None)
        else:
            options[self._resolve_option] = mapping
        self._base.curl_options = options
        try:
            return self._base.get(url, **kwargs)
        except Exception as error:
            response = getattr(error, "response", None)
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            code = getattr(error, "code", None)
            raise StreamNetworkError("stream_connect_failed", code=code,
                                     response=response) from None
        finally:
            self._base.curl_options = original

    def get(self, url, **kwargs):
        with self._lock:
            if self._closed:
                raise StreamNetworkError("stream_session_closed")
            kwargs = dict(kwargs)
            kwargs["allow_redirects"] = False
            kwargs.pop("max_redirects", None)
            seen, redirects = set(), 0
            while True:
                try:
                    key = urldefrag(url)[0] if isinstance(url, str) else None
                except (ValueError, TypeError):
                    raise StreamNetworkError("invalid_stream_url") from None
                if key in seen:
                    raise StreamNetworkError("stream_redirect_loop")
                seen.add(key)
                raw = self._get_once(url, kwargs)
                if raw.status_code not in _REDIRECTS:
                    response = _Response(raw, self)
                    self._responses.add(response)
                    return response
                try:
                    location = raw.headers.get("Location") or raw.headers.get("location")
                    if not isinstance(location, str) or not location:
                        raise StreamNetworkError("stream_redirect_location_missing")
                    if redirects >= self.max_redirects:
                        raise StreamNetworkError("stream_redirect_limit")
                    following = urljoin(url, location)
                finally:
                    try:
                        raw.close()
                    except Exception:
                        raise StreamNetworkError("stream_response_close_failed") from None
                url, redirects = following, redirects + 1

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            failed = False
            for response in tuple(self._responses):
                try:
                    response.close()
                except Exception:
                    failed = True
            try:
                self._base.close()
            except Exception:
                failed = True
            self._cache.clear()
            if failed:
                raise StreamNetworkError("stream_session_close_failed")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def wrap_session(base, provider=None, **kwargs):
    """Transfer ownership of a raw curl_cffi Session to the direct-DNS wrapper."""
    try:
        return DirectDnsSession(base, provider, **kwargs)
    except Exception:
        try:
            base.close()
        except Exception:
            pass
        raise
