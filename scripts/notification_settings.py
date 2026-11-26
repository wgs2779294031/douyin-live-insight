"""Private notification configuration and a secret-free local settings API.

This is independent of the legacy ``feishu`` Bitable settings. An explicit new
disabled setting must never fall back to an older configured transport.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import urlsplit

from file_lock import file_lock

MAX_CONFIG_BYTES = 2 * 1024 * 1024
MAX_SECRET_LENGTH = 4096
WEBHOOK_PATH = re.compile(r"/open-apis/bot/v2/hook/[A-Za-z0-9_-]{8,256}\Z")
MESSAGES = {
    "settings_saved": "通知配置已保存；保存本身不会发送消息。",
    "settings_cleared": "事件通知已关闭，通知专用地址和签名已清除。",
    "delivered": "飞书已接受这条测试消息，请在配置的接收会话中确认。",
    "disabled": "事件通知已关闭。",
    "not_configured": "尚未配置可用的飞书通知通道。",
    "rate_limited": "本次消息正在冷却期内，未重复发送。",
    "invalid_request": "通知设置字段不正确，请重新检查。",
    "invalid_webhook": "请填写飞书官方群自定义机器人的 HTTPS Webhook 地址。",
    "invalid_secret": "签名密钥格式不正确。",
    "legacy_app_unavailable": "尚无可沿用的飞书自建应用和接收目标。",
    "configuration_unavailable": "本地通知配置暂时无法读取，原文件已保留。",
    "storage_failed": "通知配置保存未完成，请刷新核对后重试。",
    "delivery_failed": "发送未确认，请先检查接收会话和网络，不要盲目重复发送。",
    "remote_rejected": "飞书未接受消息，请检查机器人关键词、签名、接收对象及权限。",
    "test_busy": "另一次测试通知正在发送，请等待结果。",
    "stopping": "应用正在停止，本次没有发送通知。",
    "configuration_changed": "通知配置已变化，本次没有继续发送，请重新确认。",
}
_test_locks = {}
_test_guard = threading.Lock()


class NotificationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _text(value, code="invalid_request"):
    if not isinstance(value, str) or len(value) > MAX_SECRET_LENGTH or any(ord(c) < 32 for c in value):
        raise NotificationError(code)
    return value.strip()


def validate_webhook(value):
    value = _text(value, "invalid_webhook")
    if not value:
        return ""
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or url.hostname != "open.feishu.cn" or url.port not in (None, 443)
                or url.username is not None or url.password is not None or url.query or url.fragment
                or not WEBHOOK_PATH.fullmatch(url.path)):
            raise ValueError
    except ValueError:
        raise NotificationError("invalid_webhook") from None
    return value


def read_config(config_path):
    try:
        path = Path(config_path)
        if path.is_symlink():
            raise ValueError
        with path.open("rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
        value = json.loads(raw.decode("utf-8-sig"))
        if len(raw) > MAX_CONFIG_BYTES or not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError, TypeError):
        raise NotificationError("configuration_unavailable") from None


def effective_config(config):
    """Return private effective transport data; never send this object to a client."""
    old = config.get("feishu", {})
    if not isinstance(old, dict):
        old = {}
    old_text = lambda key: old.get(key, "") if isinstance(old.get(key, ""), str) else ""
    app = {key: old_text(key).strip() for key in ("app_id", "app_secret", "alert_chat_id", "alert_open_id")}
    app["api_base"] = old_text("api_base").strip() or "https://open.feishu.cn"
    app_available = bool(app["app_id"] and app["app_secret"] and (app["alert_chat_id"] or app["alert_open_id"]))
    notifications = config.get("notifications", {})
    if not isinstance(notifications, dict):
        raise NotificationError("configuration_unavailable")
    if "feishu" in notifications:
        saved = notifications["feishu"]
        if (not isinstance(saved, dict) or type(saved.get("enabled")) is not bool
                or saved.get("transport") not in ("webhook", "legacy_app")
                or not isinstance(saved.get("webhook_url", ""), str)
                or not isinstance(saved.get("signing_secret", ""), str)):
            raise NotificationError("configuration_unavailable")
        return dict(app, enabled=saved["enabled"], transport=saved["transport"], source="explicit",
                    webhook_url=saved.get("webhook_url", ""), signing_secret=saved.get("signing_secret", ""),
                    legacy_app_available=app_available)
    webhook = old_text("webhook_url").strip()
    enabled = bool(webhook or app_available)
    return dict(app, enabled=enabled, transport="webhook" if webhook or not app_available else "legacy_app",
                source="legacy" if enabled else "unconfigured", webhook_url=webhook, signing_secret="",
                legacy_app_available=app_available)


def public_settings(config):
    saved = effective_config(config)
    transport = saved["transport"]
    return {"enabled": saved["enabled"], "transport": transport, "source": saved["source"],
            "webhook_configured": bool(saved["webhook_url"]),
            "signing_secret_configured": bool(saved["signing_secret"]),
            "legacy_app_available": saved["legacy_app_available"],
            "destination_hint": "已配置的飞书群自定义机器人" if transport == "webhook" else "已有飞书自建应用的接收会话"}


def _atomic_write(path, value):
    path = Path(path)
    if path.is_symlink() or not path.parent.is_dir():
        raise OSError("unsafe_configuration_path")
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, allow_nan=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_settings(config_path, body):
    allowed = {"action", "enabled", "transport", "webhook_url", "signing_secret", "clear_webhook", "clear_signing_secret"}
    if (not isinstance(body, dict) or set(body) - allowed or body.get("action") != "save"
            or type(body.get("enabled")) is not bool or body.get("transport") not in ("webhook", "legacy_app")
            or any(name in body and type(body[name]) is not bool for name in ("clear_webhook", "clear_signing_secret"))):
        raise NotificationError("invalid_request")
    replacements = {}
    for name, clear, code in (("webhook_url", "clear_webhook", "invalid_webhook"),
                              ("signing_secret", "clear_signing_secret", "invalid_secret")):
        value = _text(body.get(name, ""), code)
        if value and body.get(clear):
            raise NotificationError("invalid_request")
        replacements[name] = "" if body.get(clear) else value
    with file_lock(config_path):
        config = read_config(config_path)
        current = effective_config(config)
        saved = {key: current[key] for key in ("webhook_url", "signing_secret")}
        for name, clear in (("webhook_url", "clear_webhook"), ("signing_secret", "clear_signing_secret")):
            if replacements[name] or body.get(clear):
                saved[name] = replacements[name]
        saved.update(enabled=body["enabled"], transport=body["transport"])
        if saved["webhook_url"]:
            saved["webhook_url"] = validate_webhook(saved["webhook_url"])
        if saved["enabled"]:
            if saved["transport"] == "webhook" and not saved["webhook_url"]:
                raise NotificationError("not_configured")
            if saved["transport"] == "legacy_app" and not current["legacy_app_available"]:
                raise NotificationError("legacy_app_unavailable")
        config = copy.deepcopy(config)
        config.setdefault("notifications", {})["feishu"] = saved
        try:
            _atomic_write(config_path, config)
        except (OSError, ValueError, TypeError):
            raise NotificationError("storage_failed") from None
        return public_settings(config)


def clear_settings(config_path):
    with file_lock(config_path):
        config = read_config(config_path)
        effective_config(config)  # Refuse malformed saved structures instead of overwriting them.
        config.setdefault("notifications", {})["feishu"] = {
            "enabled": False, "transport": "webhook", "webhook_url": "", "signing_secret": ""}
        try:
            _atomic_write(config_path, config)
        except (OSError, ValueError, TypeError):
            raise NotificationError("storage_failed") from None
        return public_settings(config)


def result(code, *, settings=None):
    out = {"ok": code in ("settings_saved", "settings_cleared", "delivered"), "code": code,
           "message": MESSAGES[code], "delivered": code == "delivered"}
    if settings is not None:
        out["settings"] = settings
    return out


class NotificationSettingsAPI:
    def __init__(self, config_path, *, stopping=lambda: False):
        self.config_path, self.stopping = str(config_path), stopping
        identity = os.path.normcase(os.path.realpath(self.config_path))
        with _test_guard:
            self.test_lock = _test_locks.setdefault(identity, threading.Lock())

    def get(self):
        try:
            return 200, {"ok": True, "settings": public_settings(read_config(self.config_path))}
        except NotificationError as error:
            return 503, result(error.code)

    def post(self, body):
        try:
            if not isinstance(body, dict):
                raise NotificationError("invalid_request")
            action = body.get("action")
            if action == "save":
                settings = save_settings(self.config_path, body)
                return 200, result("settings_saved", settings=settings)
            if action == "clear" and set(body) == {"action"}:
                return 200, result("settings_cleared", settings=clear_settings(self.config_path))
            if action != "test" or set(body) != {"action"}:
                raise NotificationError("invalid_request")
            if not self.test_lock.acquire(blocking=False):
                return 200, result("test_busy")
            try:
                if self.stopping():
                    return 200, result("stopping")
                from scripts import notify
                sent = notify.send_result("[竞品监控] 告警通道测试：这条消息由本地看板主动测试发送。",
                                          key="manual-settings-test", cooldown=10,
                                          config_path=self.config_path, permitted=lambda: not self.stopping())
                try:
                    settings = public_settings(read_config(self.config_path))
                except NotificationError:
                    settings = None  # Preserve the known send result if a later status read fails.
                return 200, result(sent["code"], settings=settings)
            finally:
                self.test_lock.release()
        except NotificationError as error:
            status = 503 if error.code in ("configuration_unavailable", "storage_failed") else 400
            return status, result(error.code)
        except (OSError, TimeoutError):
            return 503, result("storage_failed")
