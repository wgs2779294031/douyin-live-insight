"""Synthetic notification settings and transport checks; no real destination."""
import base64
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT.parent), str(ROOT)]
from scripts import notification_settings as settings
from scripts import notify

WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/synthetic-unit-test-id"
SECRET = "synthetic-secret-never-used"


class FakeOpener:
    def __init__(self, responses=None):
        self.requests = []
        self.responses = iter(responses or [{"code": 0}])

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return io.BytesIO(json.dumps(response).encode())


class NotificationTests(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("DLI_NOTIFICATION_TEST_ROOT")
        if base:
            Path(base).mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="notification-", dir=base)
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "config.json"
        self.config = {"feishu": {"enabled": False, "backend": "dry_run", "webhook_url": WEBHOOK},
                       "llm": {"providers": {"untouched": "synthetic-other-secret"}}, "rooms": []}
        self.write(self.config)
        self.api = settings.NotificationSettingsAPI(self.path)

    def write(self, value):
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def save(self, **overrides):
        body = {"action": "save", "enabled": True, "transport": "webhook"}
        body.update(overrides)
        return self.api.post(body)

    def test_read_is_legacy_compatible_and_never_returns_credentials(self):
        code, value = self.api.get()
        self.assertEqual(code, 200)
        self.assertEqual(value["settings"]["source"], "legacy")
        self.assertTrue(value["settings"]["enabled"])
        self.assertTrue(value["settings"]["webhook_configured"])
        self.assertNotIn(WEBHOOK, json.dumps(value))
        self.assertNotIn("synthetic-other-secret", json.dumps(value))

    def test_save_separates_bitable_and_preserves_unrelated_data(self):
        before = self.config["feishu"].copy()
        code, value = self.save(signing_secret=SECRET)
        self.assertEqual(code, 200)
        self.assertTrue(value["ok"])
        saved = settings.read_config(self.path)
        self.assertEqual(saved["feishu"], before)
        self.assertEqual(saved["llm"], self.config["llm"])
        self.assertEqual(saved["notifications"]["feishu"]["signing_secret"], SECRET)
        self.assertNotIn(SECRET, json.dumps(value))

    def test_blank_secret_inputs_preserve_and_explicit_clear_removes(self):
        self.save(signing_secret=SECRET)
        self.save(webhook_url="", signing_secret="")
        self.assertEqual(settings.effective_config(settings.read_config(self.path))["signing_secret"], SECRET)
        self.save(clear_signing_secret=True)
        self.assertEqual(settings.effective_config(settings.read_config(self.path))["signing_secret"], "")

    def test_invalid_clear_and_replace_is_atomic(self):
        before = self.path.read_bytes()
        code, value = self.save(webhook_url=WEBHOOK, clear_webhook=True)
        self.assertEqual((code, value["code"]), (400, "invalid_request"))
        self.assertEqual(self.path.read_bytes(), before)

    def test_explicit_disable_and_clear_never_fall_back(self):
        self.save(enabled=False)
        fake = FakeOpener()
        with patch.object(notify, "_OPENER", fake):
            self.assertFalse(notify.send_text("synthetic", config_path=self.path))
            self.assertEqual(self.api.post({"action": "test"})[1]["code"], "disabled")
        self.assertEqual(fake.requests, [])
        code, value = self.api.post({"action": "clear"})
        self.assertEqual((code, value["code"]), (200, "settings_cleared"))
        self.assertEqual(settings.read_config(self.path)["feishu"]["webhook_url"], WEBHOOK)
        self.assertFalse(value["settings"]["enabled"])
        self.assertFalse(value["settings"]["webhook_configured"])

    def test_unsupported_hosts_redirect_targets_and_invalid_urls_are_rejected(self):
        before = self.path.read_bytes()
        for url in ("http://open.feishu.cn/open-apis/bot/v2/hook/synthetic-id", "https://example.com/a",
                    "https://open.feishu.cn.evil.example/open-apis/bot/v2/hook/synthetic-id",
                    "https://open.feishu.cn@localhost/open-apis/bot/v2/hook/synthetic-id",
                    WEBHOOK + "?credential=secret", WEBHOOK + "#fragment", WEBHOOK.replace(".cn/", ".cn:444/")):
            with self.subTest(url=url):
                code, value = self.save(webhook_url=url)
                self.assertEqual((code, value["code"]), (400, "invalid_webhook"))
                self.assertEqual(self.path.read_bytes(), before)
        self.assertIsNone(notify._NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com"))

    def test_malformed_saved_config_is_not_overwritten(self):
        self.path.write_bytes(b'{"notifications":')
        before = self.path.read_bytes()
        self.assertEqual(self.api.get()[0], 503)
        self.assertEqual(self.save()[1]["code"], "configuration_unavailable")
        self.assertEqual(self.path.read_bytes(), before)

    def test_save_and_get_do_not_send_test_messages(self):
        fake = FakeOpener()
        with patch.object(notify, "_OPENER", fake):
            self.api.get()
            self.save(signing_secret=SECRET)
        self.assertEqual(fake.requests, [])

    def test_explicit_test_sends_saved_destination_and_signature(self):
        self.save(signing_secret=SECRET)
        fake = FakeOpener()
        with patch.object(notify, "_OPENER", fake), patch.object(notify.time, "time", return_value=1700000000):
            code, result = self.api.post({"action": "test"})
        self.assertEqual((code, result["code"], result["delivered"]), (200, "delivered", True))
        self.assertEqual(len(fake.requests), 1)
        request, timeout = fake.requests[0]
        self.assertEqual(request.full_url, WEBHOOK)
        self.assertEqual(timeout, 5)
        payload = json.loads(request.data)
        self.assertEqual(payload["timestamp"], "1700000000")
        expected = base64.b64encode(hmac.new(("1700000000\n" + SECRET).encode(), b"", hashlib.sha256).digest()).decode()
        self.assertEqual(payload["sign"], expected)
        self.assertIn("主动测试", payload["content"]["text"])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(self.api.post({"action": "test", "webhook_url": WEBHOOK})[0], 400)

    def test_remote_and_network_errors_never_echo_sensitive_values(self):
        self.save(signing_secret=SECRET)
        for response, expected in (({"code": 19024, "msg": WEBHOOK + SECRET}, "remote_rejected"),
                                   (ValueError(WEBHOOK + SECRET), "delivery_failed"), ({}, "remote_rejected"),
                                   ({"code": 19024, "StatusCode": 0}, "remote_rejected")):
            fake = FakeOpener([response])
            with patch.object(notify, "_OPENER", fake):
                _, value = self.api.post({"action": "test"})
            self.assertEqual(value["code"], expected)
            self.assertNotIn(WEBHOOK, json.dumps(value))
            self.assertNotIn(SECRET, json.dumps(value))

    def test_legacy_app_request_compatibility(self):
        self.write({"feishu": {"app_id": "synthetic-app", "app_secret": SECRET, "alert_chat_id": "synthetic-chat"}})
        fake = FakeOpener([{"code": 0, "tenant_access_token": "synthetic-token"}, {"code": 0}])
        with patch.object(notify, "_OPENER", fake):
            self.assertTrue(notify.send_text("synthetic message", key="legacy-shape", config_path=self.path))
        self.assertEqual(len(fake.requests), 2)
        request = fake.requests[1][0]
        self.assertIn("receive_id_type=chat_id", request.full_url)
        self.assertEqual(json.loads(request.data)["receive_id"], "synthetic-chat")

    def test_app_send_stops_after_token_if_configuration_changes(self):
        self.write({"feishu": {"app_id": "synthetic-app", "app_secret": SECRET, "alert_open_id": "synthetic-open"}})
        parent = self
        class ChangingOpener(FakeOpener):
            def open(self, request, timeout):
                parent.api.post({"action": "clear"})
                return super().open(request, timeout)
        fake = ChangingOpener([{"code": 0, "tenant_access_token": "synthetic-token"}])
        with patch.object(notify, "_OPENER", fake):
            value = notify.send_result("synthetic", config_path=self.path)
        self.assertEqual(value["code"], "configuration_changed")
        self.assertEqual(len(fake.requests), 1)

    def test_stopping_prevents_active_test_without_network(self):
        api = settings.NotificationSettingsAPI(self.path, stopping=lambda: True)
        fake = FakeOpener()
        with patch.object(notify, "_OPENER", fake):
            self.assertEqual(api.post({"action": "test"})[1]["code"], "stopping")
        self.assertEqual(fake.requests, [])

    def test_boolean_sender_rechecks_caller_permission_after_token(self):
        self.write({"feishu": {"app_id": "synthetic-app", "app_secret": SECRET, "alert_open_id": "synthetic-open"}})
        permitted = {"value": True}
        class StopDuringToken(FakeOpener):
            def open(self, request, timeout):
                permitted["value"] = False
                return super().open(request, timeout)
        fake = StopDuringToken([{"code": 0, "tenant_access_token": "synthetic-token"}])
        with patch.object(notify, "_OPENER", fake):
            self.assertFalse(notify.send_text("synthetic", config_path=self.path,
                                             permitted=lambda: permitted["value"]))
        self.assertEqual(len(fake.requests), 1)

    def test_concurrent_duplicate_does_not_clear_owner_inflight_reservation(self):
        entered, release = threading.Event(), threading.Event()
        class BlockingOpener(FakeOpener):
            def open(self, request, timeout):
                entered.set()
                self_waited = release.wait(3)
                if not self_waited:
                    raise AssertionError("test did not release fake sender")
                return super().open(request, timeout)
        fake = BlockingOpener()
        results = []
        with patch.object(notify, "_OPENER", fake):
            worker = threading.Thread(target=lambda: results.append(notify.send_result("synthetic", key="parallel", config_path=self.path)))
            worker.start()
            self.assertTrue(entered.wait(3))
            try:
                self.assertEqual(notify.send_result("synthetic", key="parallel", config_path=self.path)["code"], "rate_limited")
                self.assertEqual(notify.send_result("synthetic", key="parallel", config_path=self.path)["code"], "rate_limited")
            finally:
                release.set()
                worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["code"], "delivered")
        self.assertEqual(len(fake.requests), 1)

    def test_oversized_remote_response_is_rejected(self):
        class LargeOpener:
            def open(self, request, timeout):
                return io.BytesIO(b" " * (notify.MAX_RESPONSE_BYTES + 2))
        with patch.object(notify, "_OPENER", LargeOpener()):
            self.assertEqual(notify.send_result("synthetic", config_path=self.path)["code"], "remote_rejected")


if __name__ == "__main__":
    unittest.main()
