"""Virtual clocks, owned temporary fixtures, and fake notification senders only."""
from __future__ import annotations

import json
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from scripts.monitoring_alerts import (MonitoringAlertState, deliver_event,
    latest_business_timestamp, notification_text, target_watch_enabled)
from config import RoomConfig

# Never inherit a real installed customer context while importing the source.
with patch.dict(os.environ, {k: v for k, v in os.environ.items()
                            if not k.startswith("DY_CUSTOMER")}, clear=True):
    import watch_anchor_live as watcher

EPOCH = 1_700_000_000.0


def observe(state, tick, probe="unknown", **changes):
    values = {"now": float(tick), "wall_now": EPOCH + tick,
              "probe": probe, "probe_ts": EPOCH + tick}
    values.update(changes)
    return state.observe(**values)


def warning(state):
    assert observe(state, 0) is None
    assert observe(state, 300) is None
    event = observe(state, 600)
    assert event is not None and event.action == "warning"
    return event


class StateTests(unittest.TestCase):
    def test_short_transient_and_confirmed_offline_do_not_warn(self):
        state = MonitoringAlertState(0)
        for tick in (0, 60, 120):
            self.assertIsNone(observe(state, tick))
        self.assertIsNone(observe(state, 180, "offline"))
        self.assertIsNone(observe(state, 1800, "offline"))
        self.assertEqual(state.snapshot()["status"], "normal")

    def test_requires_both_duration_and_three_distinct_rounds(self):
        state = MonitoringAlertState(0)
        self.assertIsNone(observe(state, 0))
        self.assertIsNone(observe(state, 600))
        # A duplicate/replayed sample is not a third observation.
        self.assertIsNone(observe(state, 601, probe_ts=EPOCH + 600))
        self.assertEqual(state.unknown_rounds, 2)
        self.assertEqual(observe(state, 602).action, "warning")

    def test_successful_warning_deduplicates_until_actual_recovery(self):
        state = MonitoringAlertState(0)
        event = warning(state)
        state.acknowledge(event, True, 600)
        for tick in (601, 900, 7200):
            self.assertIsNone(observe(state, tick))
        recovered = observe(state, 7260, "live")
        self.assertEqual(recovered.action, "recovery")
        state.acknowledge(recovered, True, 7260)
        self.assertIsNone(observe(state, 7320, "live"))
        self.assertFalse(state.warning_sent)

    def test_active_business_evidence_suppresses_warning_but_not_forever(self):
        state = MonitoringAlertState(0)
        observe(state, 0)
        observe(state, 300)
        self.assertIsNone(observe(state, 600, business_ts=EPOCH + 590))
        self.assertIsNone(observe(state, 800, business_ts=EPOCH + 790))
        self.assertEqual(observe(state, 1000, business_ts=EPOCH + 790).action, "warning")

    def test_old_future_or_boolean_business_timestamp_cannot_suppress(self):
        for value in (EPOCH, EPOCH + 100000, True, float("nan")):
            state = MonitoringAlertState(0)
            observe(state, 0)
            observe(state, 300)
            self.assertIsNotNone(observe(state, 600, business_ts=value))

    def test_business_only_is_not_a_login_or_probe_recovery(self):
        state = MonitoringAlertState(0)
        event = warning(state)
        state.acknowledge(event, True, 600)
        self.assertIsNone(observe(state, 700, business_ts=EPOCH + 700))
        self.assertTrue(state.warning_sent)

    def test_recovery_needs_new_fresh_valid_evidence(self):
        state = MonitoringAlertState(0)
        event = warning(state)
        state.acknowledge(event, True, 600)
        self.assertIsNone(observe(state, 620, "live", probe_ts=EPOCH + 590))
        self.assertIsNone(observe(state, 630, "live", probe_ts=EPOCH + 631))
        self.assertIsNone(observe(state, 800, "live", probe_ts=EPOCH + 610))
        event = observe(state, 810, "offline")
        self.assertEqual(event.action, "recovery")
        self.assertEqual(event.probe, "offline")

    def test_warning_failure_is_not_marked_sent_and_can_retry(self):
        state = MonitoringAlertState(0)
        event = warning(state)
        state.acknowledge(event, False, 600)
        self.assertFalse(state.warning_sent)
        self.assertIsNone(observe(state, 659))
        retry = observe(state, 660)
        self.assertEqual(retry.action, "warning")
        self.assertTrue(state.acknowledge(retry, True, 660))
        self.assertTrue(state.warning_sent)

    def test_unsent_warning_does_not_generate_fake_recovery(self):
        state = MonitoringAlertState(0)
        event = warning(state)
        state.acknowledge(event, False, 600)
        self.assertIsNone(observe(state, 660, "offline"))
        self.assertEqual(state.status, "normal")

    def test_recovery_failure_remains_pending_with_bounded_retries(self):
        state = MonitoringAlertState(0)
        event = warning(state)
        state.acknowledge(event, True, 600)
        event = observe(state, 620, "live")
        state.acknowledge(event, False, 620)
        self.assertTrue(state.warning_sent)
        self.assertIsNone(observe(state, 679, "live"))
        event = observe(state, 680, "offline")
        state.acknowledge(event, False, 680)
        self.assertIsNone(observe(state, 979, "offline"))
        event = observe(state, 980, "offline")
        state.acknowledge(event, False, 980)
        self.assertIsNone(observe(state, 2779, "offline"))
        event = observe(state, 2780, "offline")
        self.assertEqual(event.action, "recovery")
        state.acknowledge(event, True, 2780)
        self.assertFalse(state.warning_sent)

    def test_disabled_target_and_stale_ack_cannot_emit_recovery(self):
        state = MonitoringAlertState(0)
        event = warning(state)
        self.assertIsNone(observe(state, 610, active=False))
        self.assertFalse(state.acknowledge(event, True, 610))
        self.assertEqual(state.status, "inactive")
        self.assertIsNone(observe(state, 1200))
        self.assertIsNone(observe(state, 1500))
        self.assertEqual(observe(state, 1800).action, "warning")

    def test_backward_monotonic_clock_restarts_graceful_observation(self):
        state = MonitoringAlertState(0)
        observe(state, 100)
        observe(state, 500)
        self.assertIsNone(observe(state, 200))
        self.assertEqual(state.unknown_rounds, 1)
        self.assertIsNone(observe(state, 799))
        self.assertEqual(observe(state, 800).action, "warning")

    def test_unavailable_scope_breaks_continuous_unknown_accumulation(self):
        state = MonitoringAlertState(0)
        observe(state, 0)
        observe(state, 300)
        state.pause()
        self.assertIsNone(observe(state, 600))
        self.assertIsNone(observe(state, 900))
        self.assertEqual(observe(state, 1200).action, "warning")

    def test_sender_exception_or_non_bool_success_does_not_acknowledge_success(self):
        state = MonitoringAlertState(0)
        event = warning(state)
        def fail(*args, **kwargs):
            raise OSError("synthetic failure")
        self.assertFalse(deliver_event(state, event, "demo", 600, fail))
        event = observe(state, 660)
        self.assertFalse(deliver_event(state, event, "demo", 660, lambda *a, **kw: "true"))
        self.assertFalse(state.warning_sent)

    def test_messages_do_not_claim_risk_control_or_missing_data_recovery(self):
        state = MonitoringAlertState(0)
        event = warning(state)
        text = notification_text(event, "demo")
        self.assertIn("原因尚未确认", text)
        self.assertNotIn("已被风控", text)
        state.acknowledge(event, True, 600)
        text = notification_text(observe(state, 620, "live"), "demo")
        self.assertIn("不表示中断资料已经补齐", text)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        parent = os.environ.get("DLI_TEST_ROOT")
        if parent:
            Path(parent).mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="monitor-alert-", dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)

    def write_stream(self, stream, tag, now, rows, *, trailing=b""):
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        path = self.work / f"{stream}_douyin_{tag}_{day}.jsonl"
        path.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in rows) + trailing)
        return path

    def test_semantic_rows_not_mtime_establish_business_evidence(self):
        now = EPOCH
        self.write_stream("stats", "demo", now, [
            {"ts": now, "type": "heartbeat"},
            {"ts": now - 300, "type": "room_stats"},
            {"ts": now + 1, "type": "room_stats"},
            {"ts": now, "type": "room_stats", "tag": "another"},
            {"ts": now, "type": "room_stats", "platform": "other"},
            {"ts": now, "type": "room_stats", "tag": []},
            {"ts": now, "type": "room_stats", "room_tag": {}},
            {"ts": True, "type": "room_stats"},
        ], trailing=b'{"ts":1700000000,"type":"room_stats"}')
        self.assertIsNone(latest_business_timestamp(self.work, "demo", now))
        self.write_stream("danmu", "demo", now, [{"ts": now - 2, "type": "danmaku"}])
        self.assertEqual(latest_business_timestamp(self.work, "demo", now), now - 2)

    def test_midnight_and_bounded_tail(self):
        midnight = time.mktime((2026, 11, 27, 0, 0, 30, 0, 0, -1))
        self.write_stream("stats", "demo", midnight - 60,
                          [{"ts": midnight - 60, "type": "room_stats"}])
        self.assertEqual(latest_business_timestamp(self.work, "demo", midnight), midnight - 60)
        self.assertIsNone(latest_business_timestamp(self.work, "../outside", midnight))

    def test_actual_collector_file_tag_mapping_for_spaces_symbols_and_emoji(self):
        for label in ("my room", "主播 · A", "💙"):
            file_tag = RoomConfig(room_id="12345678", label=label).file_tag()
            self.write_stream("stats", file_tag, EPOCH,
                [{"ts": EPOCH - 1, "type": "room_stats", "room_tag": file_tag}])
            self.assertEqual(latest_business_timestamp(self.work, label, EPOCH, file_tag=file_tag), EPOCH - 1)
            if label != "💙":
                self.assertEqual(latest_business_timestamp(self.work, label, EPOCH), EPOCH - 1)

    def test_same_tag_replacement_is_not_the_old_watch_target(self):
        customer = self.customer()
        path = Path(customer["roster"])
        self.assertTrue(target_watch_enabled(path, "demo", "synthetic"))
        self.assertFalse(target_watch_enabled(path, "demo", "different-uid"))
        state = MonitoringAlertState(0)
        observe(state, 0)
        observe(state, 300)
        path.write_text(json.dumps({"anchors": [
            {"tag": "demo", "sec_uid": "replacement", "enabled": True, "watch": True}]}))
        with patch.object(watcher, "CUSTOMER", customer), \
             patch.object(watcher, "DATA", self.work), \
             patch.object(watcher, "customer_stop_requested", return_value=False), \
             patch.object(watcher, "_notify") as send:
            result = watcher._customer_monitoring_step(state, "demo", {"ts": EPOCH + 600}, "channel",
                sec_uid="synthetic", now=600, wall_now=EPOCH + 600)
            send.assert_not_called()
            self.assertEqual(result["status"], "inactive")

    def test_ambiguous_collector_filename_is_not_business_suppression_evidence(self):
        tag = "A B"
        file_tag = RoomConfig(room_id="12345678", label=tag).file_tag()
        stream = self.write_stream("stats", file_tag, EPOCH + 600,
            [{"ts": EPOCH + 600, "type": "room_stats", "room_tag": file_tag}])
        original_stream = stream.read_bytes()
        for colliding in (False, True):
            customer = self.customer()
            rows = [{"tag": tag, "sec_uid": "synthetic", "enabled": True, "watch": True}]
            if colliding:
                rows.append({"tag": "a_b", "sec_uid": "other", "enabled": True, "watch": True})
            Path(customer["roster"]).write_text(json.dumps({"anchors": rows}))
            original_roster = Path(customer["roster"]).read_bytes()
            state = MonitoringAlertState(0)
            observe(state, 0)
            observe(state, 300)
            sent = []
            with patch.object(watcher, "CUSTOMER", customer), \
                 patch.object(watcher, "DATA", self.work), \
                 patch.object(watcher, "customer_stop_requested", return_value=False), \
                 patch.object(watcher, "_notify", side_effect=lambda text, **kw: sent.append(text) or True):
                watcher._customer_monitoring_step(state, tag, {"ts": EPOCH + 600}, "channel",
                    sec_uid="synthetic", file_tag=file_tag, now=600, wall_now=EPOCH + 600)
            self.assertEqual(len(sent), 1 if colliding else 0)
            self.assertEqual(stream.read_bytes(), original_stream)
            self.assertEqual(Path(customer["roster"]).read_bytes(), original_roster)

    def test_roster_requires_explicit_unambiguous_enabled_watch_target(self):
        path = self.work / "roster.json"
        self.assertIsNone(target_watch_enabled(path, "demo"))
        for enabled, watch in ((False, True), (True, False), (False, False), (True, True)):
            path.write_text(json.dumps({"anchors": [{"tag": "demo", "enabled": enabled, "watch": watch}]}))
            self.assertIs(target_watch_enabled(path, "demo"), enabled and watch)
        self.assertFalse(target_watch_enabled(path, "removed"))
        path.write_text(json.dumps({"anchors": [{"tag": "demo", "enabled": "true", "watch": True}]}))
        self.assertIsNone(target_watch_enabled(path, "demo"))
        path.write_text(json.dumps({"anchors": [{"tag": "demo"}, {"tag": "demo"}]}))
        self.assertIsNone(target_watch_enabled(path, "demo"))
        path.write_text('{"anchors":[{"tag":"demo","enabled":false,"enabled":true,"watch":true}]}')
        self.assertIsNone(target_watch_enabled(path, "demo"))

    def customer(self):
        roster = self.work / "roster.json"
        roster.write_text(json.dumps({"anchors": [{"tag": "demo", "sec_uid": "synthetic", "enabled": True, "watch": True}]}))
        return {"roster": str(roster), "config": str(self.work / "unused-config.json")}

    def test_customer_hook_delivers_only_sustained_unknown_then_fresh_recovery(self):
        state = MonitoringAlertState(0)
        sent = []
        with patch.object(watcher, "CUSTOMER", self.customer()), \
             patch.object(watcher, "DATA", self.work), \
             patch.object(watcher, "customer_stop_requested", return_value=False), \
             patch.object(watcher, "_notify", side_effect=lambda text, **kw: sent.append((text, kw)) or True):
            for tick in (0, 300, 600):
                watcher._customer_monitoring_step(state, "demo", {"ts": EPOCH + tick}, "channel",
                                                  sec_uid="synthetic", now=tick, wall_now=EPOCH + tick)
            self.assertEqual(len(sent), 1)
            result = watcher._customer_monitoring_step(state, "demo",
                {"ts": EPOCH + 660, "live_status": 0, "is_live": False, "nick": "demo"}, None,
                sec_uid="synthetic", now=660, wall_now=EPOCH + 660)
            self.assertEqual(len(sent), 2)
            self.assertIn("探测恢复", sent[1][0])
            self.assertEqual(sent[0][1]["cooldown"], 0)
            self.assertEqual(result["status"], "normal")

    def test_user_stop_during_slow_query_suppresses_pending_send(self):
        state = MonitoringAlertState(0)
        observe(state, 0)
        observe(state, 300)
        with patch.object(watcher, "CUSTOMER", self.customer()), \
             patch.object(watcher, "DATA", self.work), \
             patch.object(watcher, "customer_stop_requested", side_effect=[False, True]), \
             patch.object(watcher, "_notify") as send:
            result = watcher._customer_monitoring_step(state, "demo", {"ts": EPOCH + 600}, "channel",
                                                      sec_uid="synthetic", now=600, wall_now=EPOCH + 600)
            send.assert_not_called()
            self.assertEqual(result["status"], "inactive")

    def test_disabled_or_unavailable_scope_during_proposal_suppresses_send(self):
        for current in (False, None):
            state = MonitoringAlertState(0)
            observe(state, 0)
            observe(state, 300)
            with patch.object(watcher, "CUSTOMER", self.customer()), \
                 patch.object(watcher, "DATA", self.work), \
                 patch.object(watcher, "customer_stop_requested", return_value=False), \
                 patch.object(watcher, "target_watch_enabled", side_effect=[True, current]), \
                 patch.object(watcher, "_notify") as send:
                watcher._customer_monitoring_step(state, "demo", {"ts": EPOCH + 600}, "channel",
                                                  sec_uid="synthetic", now=600, wall_now=EPOCH + 600)
                send.assert_not_called()
                self.assertIsNone(state._pending)

    def test_real_main_loop_wires_warning_recovery_and_normal_exit_cleanup(self):
        clock = {"tick": 0.0, "round": 0}
        sent, rows = [], []
        def fake_poll(*args):
            clock["round"] += 1
            if clock["round"] == 6:
                return {"ts": EPOCH + clock["tick"], "live_status": 0,
                        "is_live": False, "nick": "demo", "api_code": 0}
            return {"ts": EPOCH + clock["tick"], "error": "synthetic channel unavailable",
                    "is_live": False}
        def sleep(delay):
            clock["tick"] += delay
        args = ["watch", "--sec-uid", "synthetic", "--tag", "demo",
                "--node", "synthetic-node", "--cdp", "synthetic-cdp", "--max-rounds", "6"]
        with patch.object(watcher, "CUSTOMER", self.customer()), \
             patch.object(watcher, "DATA", self.work), \
             patch.object(watcher, "customer_stop_requested", return_value=False), \
             patch.object(watcher, "installed_layout", return_value=None), \
             patch.object(watcher, "poll_with_fallback", side_effect=fake_poll), \
             patch.object(watcher, "heal_channel", return_value=None), \
             patch.object(watcher, "_append", side_effect=lambda _path, row: rows.append(dict(row))), \
             patch.object(watcher, "_notify", side_effect=lambda text, **kw: sent.append((text, kw)) or True), \
             patch.object(watcher.time, "monotonic", side_effect=lambda: clock["tick"]), \
             patch.object(watcher.time, "time", side_effect=lambda: EPOCH + clock["tick"]), \
             patch.object(watcher.time, "sleep", side_effect=sleep), \
             patch.object(sys, "argv", args), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(watcher.main(), 0)
        self.assertEqual(len(sent), 2)
        self.assertIn("需人工检查", sent[0][0])
        self.assertIn("探测恢复", sent[1][0])
        self.assertEqual(rows[4]["monitoring_alert"]["status"], "warning_sent")
        self.assertEqual(rows[5]["monitoring_alert"]["status"], "normal")

    def test_main_stop_after_poll_prevents_notification_and_row_publication(self):
        clock = {"tick": 0.0, "round": 0}
        rows, sent = [], []
        def fake_poll(*args):
            clock["round"] += 1
            clock["tick"] = clock["round"] * 600.0
            return {"ts": EPOCH + clock["tick"], "error": "synthetic channel unavailable",
                    "is_live": False}
        def sleep(delay):
            clock["tick"] += delay
        args = ["watch", "--sec-uid", "synthetic", "--tag", "demo",
                "--node", "synthetic-node", "--cdp", "synthetic-cdp", "--max-rounds", "3"]
        with patch.object(watcher, "CUSTOMER", self.customer()), \
             patch.object(watcher, "DATA", self.work), \
             patch.object(watcher, "customer_stop_requested", side_effect=lambda *a, **kw: clock["round"] >= 3), \
             patch.object(watcher, "installed_layout", return_value=None), \
             patch.object(watcher, "poll_with_fallback", side_effect=fake_poll), \
             patch.object(watcher, "heal_channel", return_value=None), \
             patch.object(watcher, "_append", side_effect=lambda _path, row: rows.append(dict(row))), \
             patch.object(watcher, "_notify", side_effect=lambda text, **kw: sent.append(text) or True), \
             patch.object(watcher.time, "monotonic", side_effect=lambda: clock["tick"]), \
             patch.object(watcher.time, "time", side_effect=lambda: EPOCH + clock["tick"]), \
             patch.object(watcher.time, "sleep", side_effect=sleep), \
             patch.object(sys, "argv", args), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(watcher.main(), 0)
        self.assertEqual(sent, [])
        self.assertEqual(len(rows), 2)

    def test_delivery_guard_rechecks_stop_or_disabled_scope_after_token_phase(self):
        for transition in ("stop", "disable", "replace"):
            state = MonitoringAlertState(0)
            observe(state, 0)
            observe(state, 300)
            customer = self.customer()
            flags = {"stopped": False, "strict_checks": 0, "post_attempts": 0}
            def stop(_context, **kwargs):
                flags["strict_checks"] += int(kwargs.get("strict", False))
                return flags["stopped"]
            def token_then_message(text, *, permitted, **kwargs):
                self.assertTrue(permitted())
                # Stand in for token acquisition. The real sender also checks
                # this guard immediately before its next network phase.
                if transition == "stop":
                    flags["stopped"] = True
                elif transition == "replace":
                    Path(customer["roster"]).write_text(json.dumps({"anchors": [
                        {"tag": "demo", "sec_uid": "replacement", "enabled": True, "watch": True}]}))
                else:
                    Path(customer["roster"]).write_text(json.dumps({"anchors": [
                        {"tag": "demo", "enabled": False, "watch": True}]}))
                if not permitted():
                    return False
                flags["post_attempts"] += 1
                return True
            with patch.object(watcher, "CUSTOMER", customer), \
                 patch.object(watcher, "DATA", self.work), \
                 patch.object(watcher, "customer_stop_requested", side_effect=stop), \
                 patch.object(watcher, "_notify", side_effect=token_then_message):
                result = watcher._customer_monitoring_step(state, "demo", {"ts": EPOCH + 600}, "channel",
                                                          sec_uid="synthetic", now=600, wall_now=EPOCH + 600)
            self.assertEqual(flags["post_attempts"], 0)
            self.assertEqual(flags["strict_checks"], 2)
            self.assertEqual(result["status"], "inactive")
            self.assertFalse(state.warning_sent)


if __name__ == "__main__":
    unittest.main()
