# -*- coding: utf-8 -*-
"""Bounded Feishu event notifications, separate from Bitable synchronization.

``notifications.feishu`` is authoritative when present. Only its absence permits
the historical ``feishu`` webhook/app fallback. The public boolean send_text API
is retained for existing watch/recorder callers.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.notification_settings import (NotificationError, effective_config,
                                           read_config, validate_webhook)

MAX_RESPONSE_BYTES = 65536
REQUEST_TIMEOUT = 5.0
_last_sent = {}
_inflight = set()
_send_lock = threading.Lock()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Do not depend on the user's system proxy; do not redirect private webhook or
# bearer-token requests to another endpoint.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _response_json(request):
    response = _OPENER.open(request, timeout=REQUEST_TIMEOUT)
    try:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    finally:
        close = getattr(response, "close", None)
        if close is not None:
            close()
    if len(raw) > MAX_RESPONSE_BYTES:
        raise NotificationError("remote_rejected")
    try:
        result = json.loads(raw.decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, UnicodeError):
        raise NotificationError("remote_rejected") from None


def _request(url, value, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    return urllib.request.Request(url, data=json.dumps(value, ensure_ascii=False).encode("utf-8"),
                                  headers=headers)


def _accepted(value, *, webhook=False):
    name = "code" if "code" in value or not webhook else "StatusCode"
    return type(value.get(name)) is int and value[name] == 0


def _official_app_base(value):
    if value.rstrip("/") != "https://open.feishu.cn":
        raise NotificationError("not_configured")
    return "https://open.feishu.cn"


def _get_token(app_id, app_secret, base, timeout=REQUEST_TIMEOUT):
    # timeout is retained for older callers; all requests use the fixed bound.
    base = _official_app_base(base)
    value = _response_json(_request(base + "/open-apis/auth/v3/tenant_access_token/internal",
                                   {"app_id": app_id, "app_secret": app_secret}))
    token = value.get("tenant_access_token")
    if not _accepted(value) or not isinstance(token, str) or not token or len(token) > 16384:
        raise NotificationError("remote_rejected")
    return token


def _load_feishu_cfg(config_path=None):
    saved = effective_config(read_config(config_path or ROOT / "config.json"))
    return (saved["app_id"], saved["app_secret"], saved["api_base"],
            saved["alert_chat_id"], saved["alert_open_id"])


def list_chats(config_path=None):
    """Legacy development helper; customer settings do not enumerate private chats."""
    try:
        app_id, secret, base, _, _ = _load_feishu_cfg(config_path)
        if not app_id or not secret:
            return "尚未配置飞书自建应用。"
        base = _official_app_base(base)
        token = _get_token(app_id, secret, base)
        request = urllib.request.Request(base + "/open-apis/im/v1/chats?page_size=50",
                                         headers={"Authorization": "Bearer " + token})
        value = _response_json(request)
        if not _accepted(value):
            return "飞书未接受会话列表请求，请检查应用权限。"
        items = (value.get("data") or {}).get("items") or []
        return "\n".join(str(item.get("chat_id", "")) + "  " + str(item.get("name") or "(单聊)")
                         for item in items[:50] if isinstance(item, dict)) or "应用尚未加入接收会话。"
    except Exception:
        return "会话列表暂不可用，请检查本地配置及飞书权限。"


def _fingerprint(saved):
    return hashlib.sha256(json.dumps(saved, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def send_result(text, key=None, cooldown=600.0, config_path=None, *, permitted=lambda: True):
    """Return only safe fixed codes; never return or log a credential/remote error."""
    identity = None
    reserved = False
    try:
        path = str(config_path or ROOT / "config.json")
        saved = effective_config(read_config(path))
        if not saved["enabled"]:
            return {"code": "disabled", "delivered": False}
        if not isinstance(text, str) or not text or len(text) > 12000:
            raise NotificationError("invalid_request")
        fingerprint = _fingerprint(saved)
        if saved["transport"] == "webhook":
            url = validate_webhook(saved["webhook_url"])
            if not url:
                raise NotificationError("not_configured")
        elif not saved["legacy_app_available"]:
            raise NotificationError("not_configured")
        else:
            url = _official_app_base(saved["api_base"])
        identity = (os.path.normcase(os.path.realpath(path)), fingerprint, str(key or text[:40]))
        now = time.monotonic()
        with _send_lock:
            if identity in _inflight or now - _last_sent.get(identity, float("-inf")) < max(0.0, float(cooldown)):
                return {"code": "rate_limited", "delivered": False}
            _inflight.add(identity)
            reserved = True

        def check_permitted():
            if not permitted():
                raise NotificationError("stopping")
            if _fingerprint(effective_config(read_config(path))) != fingerprint:
                raise NotificationError("configuration_changed")

        check_permitted()
        if saved["transport"] == "webhook":
            value = {"msg_type": "text", "content": {"text": text}}
            secret = saved["signing_secret"]
            if secret:
                timestamp = str(int(time.time()))
                value.update(timestamp=timestamp, sign=base64.b64encode(hmac.new(
                    (timestamp + "\n" + secret).encode("utf-8"), b"", hashlib.sha256).digest()).decode("ascii"))
            response = _response_json(_request(url, value))
            accepted = _accepted(response, webhook=True)
        else:
            token = _get_token(saved["app_id"], saved["app_secret"], url)
            check_permitted()
            target, target_type = ((saved["alert_chat_id"], "chat_id") if saved["alert_chat_id"]
                                   else (saved["alert_open_id"], "open_id"))
            response = _response_json(_request(url + "/open-apis/im/v1/messages?receive_id_type=" + target_type,
                {"receive_id": target, "msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)}, token))
            accepted = _accepted(response)
        if not accepted:
            raise NotificationError("remote_rejected")
        with _send_lock:
            _last_sent[identity] = time.monotonic()
        return {"code": "delivered", "delivered": True}
    except NotificationError as error:
        return {"code": error.code, "delivered": False}
    except urllib.error.HTTPError:
        return {"code": "remote_rejected", "delivered": False}
    except Exception:
        return {"code": "delivery_failed", "delivered": False}
    finally:
        if reserved:
            with _send_lock:
                _inflight.discard(identity)


def send_text(text, key=None, cooldown=600.0, config_path=None, *, permitted=lambda: True):
    sent = send_result(text, key, cooldown, config_path, permitted=permitted)
    if sent["code"] not in ("delivered", "disabled", "rate_limited"):
        print("[告警] 飞书通知未确认：" + sent["code"], flush=True)
    return sent["delivered"]


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "chats"
    if action == "chats":
        print(list_chats())
    elif action == "test":
        sent = send_result("[竞品监控] 告警通道测试：这条消息由命令行主动测试发送。", key="manual-test")
        print("已发送" if sent["delivered"] else "未发出：" + sent["code"])
