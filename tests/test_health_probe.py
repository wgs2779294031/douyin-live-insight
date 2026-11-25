# -*- coding: utf-8 -*-
"""进程外探针单测：只测判据（stale 认定），不碰网络/不自愈。

健康探针的价值全在「判得准」：把没播误判成停写会天天半夜报警，把停了漏判
就白做。所以这里只钉死 probe()。
"""
import pathlib
import sys
import json
import os
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import health_probe as hp  # noqa: E402


def _roster(*tags, watch=True, enabled=True):
    return {"anchors": [{"tag": t, "watch": watch, "enabled": enabled,
                         "sec_uid": "MS4wLjABAAAA" + "x" * 40}
                        for t in tags]}


def _mk(tmp_path, tag, age_sec, now):
    p = tmp_path / ("watch_%s.jsonl" % tag)
    p.write_text("{}\n", encoding="utf-8")
    import os
    os.utime(p, (now - age_sec, now - age_sec))
    return p


def test_fresh_files_not_stale(tmp_path, monkeypatch):
    now = 1793000000.0
    monkeypatch.setattr(hp, "DATA", tmp_path)
    _mk(tmp_path, "A", 10, now)
    _mk(tmp_path, "B", hp.STALE_SEC - 1, now)
    assert hp.probe(_roster("A", "B"), now) == []


def test_old_file_is_stale(tmp_path, monkeypatch):
    now = 1793000000.0
    monkeypatch.setattr(hp, "DATA", tmp_path)
    _mk(tmp_path, "A", 10, now)
    _mk(tmp_path, "B", hp.STALE_SEC + 60, now)
    got = hp.probe(_roster("A", "B"), now)
    assert [g["tag"] for g in got] == ["B"]
    assert got[0]["age"] > hp.STALE_SEC


def test_missing_file_counts_as_stale(tmp_path, monkeypatch):
    now = 1793000000.0
    monkeypatch.setattr(hp, "DATA", tmp_path)
    got = hp.probe(_roster("NeverStarted"), now)
    assert len(got) == 1 and got[0]["age"] >= 86400


def test_disabled_or_watch_off_skipped(tmp_path, monkeypatch):
    now = 1793000000.0
    monkeypatch.setattr(hp, "DATA", tmp_path)
    # 两路都没文件，但一路 disabled、一路 watch=false → 都不该报
    r = {"anchors": [
        {"tag": "Off", "watch": True, "enabled": False},
        {"tag": "NoWatch", "watch": False, "enabled": True},
    ]}
    assert hp.probe(r, now) == []


def test_threshold_is_15_minutes():
    """15 分钟是跟 anchor_manager 的 stalled 判据对齐的，别随手改。"""
    assert hp.STALE_SEC == 900


def _watch_samples(folder, tag, now, samples):
    path = folder / f"watch_{tag}.jsonl"
    path.write_text("".join(json.dumps({"ts": now + delta, **row}) + "\n"
                            for delta, row in samples), encoding="utf-8")
    os.utime(path, (now, now))
    return path


OFFLINE = {"api_code": 0, "is_live": False, "live_status": 0, "nick": "sample"}
LIVE = {"api_code": 0, "is_live": True, "live_status": 1, "web_rid": "12"}
BAD = {"is_live": False, "error": "Unexpected end of JSON input"}


def test_nonzero_api_code_overrules_otherwise_confirmed_offline(tmp_path, monkeypatch):
    monkeypatch.setattr(hp, "DATA", tmp_path)
    now = 1_800_000_000
    rejected = {**OFFLINE, "api_code": 2}
    assert hp.classify_env_failure(rejected) is None  # legacy watcher accepts these fields
    _watch_samples(tmp_path, "A", now, [(-600, rejected), (-300, rejected), (0, rejected)])
    invalid, alerts = hp.probe_invalid(_roster("A"), now)
    assert invalid == {"A"}
    assert len(alerts) == 1 and alerts[0]["last_error_kind"] == "channel"


@pytest.mark.parametrize("row", [BAD, {"api_msg": "blocked"}, {"is_live": False},
                                 {"live_status": 1, "is_live": False},
                                 {"room_data_parse_error": "invalid", "live_status": 0}])
def test_three_fresh_invalid_samples_are_not_offline(tmp_path, monkeypatch, row):
    monkeypatch.setattr(hp, "DATA", tmp_path)
    now = 1_800_000_000
    path = _watch_samples(tmp_path, "A", now, [(-600, row), (-300, row), (0, row)])
    with path.open("ab") as stream:
        stream.write(b'{"ts":')  # writer is still appending its next sample
    invalid, alerts = hp.probe_invalid(_roster("A"), now)
    assert invalid == {"A"}
    assert [(a["kind"], a["rounds"], a["span"]) for a in alerts] == [("探测无效", 3, 600)]


@pytest.mark.parametrize("samples", [
    [(0, BAD)], [(-60, BAD), (-30, BAD), (0, BAD)],
    [(-200, BAD), (-150, OFFLINE), (-60, BAD), (0, BAD)],
    [(-300, BAD), (-200, BAD), (-100, BAD), (0, OFFLINE)],
    [(-300, BAD), (-200, BAD), (-100, BAD), (0, LIVE)],
    [(-2000, BAD), (-60, BAD), (0, BAD)], [(-1021, BAD), (-961, BAD), (-901, BAD)],
])
def test_transient_recovered_or_stale_samples_do_not_alert(tmp_path, monkeypatch, samples):
    monkeypatch.setattr(hp, "DATA", tmp_path)
    now = 1_800_000_000
    _watch_samples(tmp_path, "A", now, samples)
    assert hp.probe_invalid(_roster("A"), now)[1] == []


class HealthRun:
    def __init__(self, tmp_path, monkeypatch, *tags):
        self.path = tmp_path
        self.now = 1_800_000_000
        self.tags = tags
        self.send = Mock(return_value=True)
        self.launch, self.restart = Mock(), Mock()
        monkeypatch.setattr(hp, "DATA", tmp_path)
        monkeypatch.setattr(hp, "STATE_PATH", tmp_path / "health-state.json")
        monkeypatch.setattr(hp.time, "time", lambda: self.now)
        monkeypatch.setattr(hp.roster_mod, "ensure", lambda: (_roster(*self.tags), False))
        monkeypatch.setattr(hp.notify, "send_text", self.send)
        monkeypatch.setattr(hp.subprocess, "run", self.launch)
        monkeypatch.setattr(hp.mgr, "restart_anchor", self.restart)

    def invalid(self, tag, span=600):
        _watch_samples(self.path, tag, self.now, [(-span, BAD), (-span//2, BAD), (0, BAD)])

    def business(self, tag, ts, stream="stats", **fields):
        day = hp.time.strftime("%Y-%m-%d", hp.time.localtime(ts))
        path = self.path / f"{stream}_douyin_{tag}_{day}.jsonl"
        row = {"ts": ts, "tag": tag, "type": "room_stats" if stream == "stats" else "danmaku", **fields}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")

    def good(self, tag):
        _watch_samples(self.path, tag, self.now, [(0, OFFLINE)])

    def diagnosis(self):
        return json.loads((self.path / "health_probe_diagnosis.json").read_bytes())


def test_shared_failure_changes_members_without_repeat_and_recovers_once(tmp_path, monkeypatch):
    h = HealthRun(tmp_path, monkeypatch, "A", "B")
    h.invalid("A")
    h.good("B")
    assert hp.main() == 1
    assert "可能漏采" in h.send.call_args.args[0]
    assert "扫码" not in h.send.call_args.args[0]
    h.now += 3601
    h.invalid("A")
    h.invalid("B")
    assert hp.main() == 1 and h.send.call_count == 1
    assert h.diagnosis()["incident"]["unresolved"] == ["A", "B"]
    h.good("A")
    assert hp.main() == 1 and h.send.call_count == 1
    h.good("B")
    assert hp.main() == 0 and h.send.call_count == 2
    assert "已确认下播" in h.send.call_args.args[0]
    assert hp.main() == 0 and h.send.call_count == 2
    h.launch.assert_not_called()
    h.restart.assert_not_called()


def test_unsupported_status_observed_then_persistent_loss_notified(tmp_path, monkeypatch):
    h = HealthRun(tmp_path, monkeypatch, "A")
    h.invalid("A", 120)
    assert hp.main() == 0 and h.send.call_count == 0
    h.invalid("A")
    h.business("A", h.now-10)
    assert hp.main() == 0 and h.send.call_count == 0
    row = h.diagnosis()["tags"]["A"]
    assert (row["level"],row["activity"],row["evidence"],row["stats_age_sec"]) == (
        "degraded_data_active","recent",["stats"],10)
    h.now += 181
    assert hp.main() == 1 and hp.main() == 1 and h.send.call_count == 1
    assert h.diagnosis()["tags"]["A"]["level"] == "missed_start_risk"
    h.business("A", h.now-1, "danmu")
    assert hp.main() == 0 and h.send.call_count == 2
    assert "已有新业务数据" in h.send.call_args.args[0]
    assert "探测已恢复" not in h.send.call_args.args[0]
    assert hp.main() == 0 and h.send.call_count == 2


def test_only_real_escalation_reopens_delivery_during_same_incident(tmp_path, monkeypatch):
    h = HealthRun(tmp_path, monkeypatch, "A", "B")
    for t in h.tags: h.invalid(t)
    h.business("B", h.now)
    hp.main()
    assert h.send.call_count == 1
    h.now += 181
    hp.main()
    assert h.send.call_count == 2 and "B" in h.send.call_args.args[0]
    h.business("B", h.now)
    hp.main()
    h.now += 181
    hp.main()
    assert h.send.call_count == 2


def test_confirmed_live_writer_loses_coverage_during_old_incident(tmp_path, monkeypatch):
    old = ["A", "B", "C", "D", "E"]
    h = HealthRun(tmp_path, monkeypatch, *old, "live")
    for tag in old:
        h.invalid(tag)
    _watch_samples(tmp_path, "live", h.now, [(0,LIVE)])
    h.business("live", h.now-1)
    os.utime(next(tmp_path.glob("stats_douyin_live_*.jsonl")), (h.now-1, h.now-1))
    hp._save_state({"invalid_probes": {t:{} for t in old},
                    "alerted": {"invalid:"+t:h.now-2000 for t in old}})
    hp.main()
    assert h.send.call_count == 0 and hp._load_state()["probe_had_data"] == ["live"]
    # First subsequent observation: sustained invalid and data already stale.
    h.now += 720
    for tag in h.tags:
        h.invalid(tag)
    assert hp.main() == 1 and h.send.call_count == 1
    assert "live" in h.send.call_args.args[0]
    assert all(tag not in h.send.call_args.args[0] for tag in old)
    assert hp._load_state()["last_delivery"]["tags"] == ["live"]
    h.now += 1900
    for tag in h.tags:
        h.invalid(tag)
    assert hp.main() == 1 and h.send.call_count == 1
    h.good("live")
    hp.main()
    assert "live" not in hp._load_state().get("probe_had_data", [])


def test_unknown_or_removed_member_never_generates_false_recovery(tmp_path, monkeypatch):
    h = HealthRun(tmp_path, monkeypatch, "A", "B")
    h.invalid("A")
    h.invalid("B")
    hp.main()
    h.good("A")
    (tmp_path/"watch_B.jsonl").write_bytes(b"broken complete record\n")
    os.utime(tmp_path/"watch_B.jsonl", (h.now, h.now))
    hp.main()
    assert h.diagnosis()["tags"]["B"]["probe"] == "unreadable"
    assert h.diagnosis()["incident"]["unresolved"] == ["B"]
    h.tags = ["A"]
    assert hp.main() == 0 and h.send.call_count == 1
    assert h.diagnosis()["incident"] == {"status": "scope_changed", "retired": ["B"]}
    assert "probe_incident" not in hp._load_state()
    assert hp.main() == 0 and h.send.call_count == 1


def test_disabling_member_keeps_remaining_risk_and_later_reports_only_verified_room(tmp_path, monkeypatch):
    h = HealthRun(tmp_path, monkeypatch, "A", "B")
    roster = _roster("A", "B")
    monkeypatch.setattr(hp.roster_mod, "ensure", lambda: (roster, False))
    h.invalid("A")
    h.invalid("B")
    assert hp.main() == 1 and h.send.call_count == 1
    roster["anchors"][1]["enabled"] = False
    assert hp.main() == 1 and h.send.call_count == 1
    incident = h.diagnosis()["incident"]
    assert incident["unresolved"] == ["A"] and incident["retired"] == ["B"]
    h.good("A")
    assert hp.main() == 0 and h.send.call_count == 2
    assert "已确认下播：A" in h.send.call_args.args[0] and "B" not in h.send.call_args.args[0]
    assert h.diagnosis()["incident"]["retired"] == ["B"]
    assert "probe_incident" not in hp._load_state()


def test_seed_fallback_cannot_retire_incident_member(tmp_path, monkeypatch):
    h = HealthRun(tmp_path, monkeypatch, "A")
    h.invalid("A")
    assert hp.main() == 1
    monkeypatch.setattr(hp.roster_mod, "ensure", lambda: (_roster(), True))
    assert hp.main() == 1 and h.send.call_count == 1
    assert h.diagnosis()["incident"]["unresolved"] == ["A"]


def test_failed_warning_and_recovery_retry_without_repeat_after_success(tmp_path, monkeypatch):
    h = HealthRun(tmp_path, monkeypatch, "A")
    h.send.side_effect = [False, True, False, True]
    h.invalid("A")
    hp.main()
    assert hp._load_state()["last_delivery"]["status"] == "failed"
    h.now += hp.ALERT_RETRY_SEC-1
    hp.main()
    assert h.send.call_count == 1
    h.now += 1
    hp.main()
    assert hp._load_state()["last_delivery"]["status"] == "sent"
    hp.main()
    h.good("A")
    assert hp.main() == 1
    assert h.diagnosis()["incident"]["action"] == "recovery"
    hp.main()
    assert h.send.call_count == 3
    h.now += hp.ALERT_RETRY_SEC
    assert hp.main() == 0 and h.send.call_count == 4


def test_cross_midnight_valid_times_only_and_safe_diagnostic(tmp_path, monkeypatch):
    h = HealthRun(tmp_path, monkeypatch, "A")
    now = hp.time.localtime(h.now)
    h.now = int(hp.time.mktime((now.tm_year, now.tm_mon, now.tm_mday,0,0,30,0,0,-1)))
    secret = "https://invalid.example/path?credential=mock-secret"
    _watch_samples(tmp_path,"A",h.now,[(t,{"error":secret,"profile_error":secret,
                                          "room_enter_result":secret,"sec_uid":secret}) for t in (-600,-300,0)])
    h.business("A", h.now+20)    # future ts
    h.business("A", h.now-600)
    h.business("A", h.now-50, "danmu", text=secret)
    hp.main()
    row = h.diagnosis()["tags"]["A"]
    assert row["evidence"] == ["danmu"] and row["interaction_age_sec"] == 50
    assert row["stats_age_sec"] == 600
    assert secret not in str(h.diagnosis())
    assert not h.send.called


def test_old_delivered_incident_does_not_send_again_on_upgrade(tmp_path, monkeypatch):
    h = HealthRun(tmp_path, monkeypatch, "A")
    h.invalid("A")
    hp._save_state({"invalid_probes":{"A":{"kind":"探测无效"}},
                    "alerted":{"invalid:A": h.now-5000}})
    assert hp.main() == 1 and h.send.call_count == 0
    assert hp._load_state()["probe_incident"]["notified"]
    h.good("A")
    assert hp.main() == 0 and h.send.call_count == 1


def test_preview_produces_example_without_writes_or_notification(tmp_path, monkeypatch):
    h = HealthRun(tmp_path, monkeypatch, "A")
    h.invalid("A")
    state = {"alerted": {"invalid:A": h.now-100}, "invalid_probes": {"A": {}}}
    unchanged = json.dumps(state)
    before = set(tmp_path.iterdir())
    report = hp.preview_probe(_roster("A"), h.now, state=state)
    assert report["would_send"] == [] and "可能漏采" in report["warning_example"]
    preview = hp.preview_probe(_roster("A"), h.now, state={})
    assert preview["would_send"] == [report["warning_example"]]
    assert json.dumps(state) == unchanged and set(tmp_path.iterdir()) == before
    h.send.assert_not_called()


def test_older_valid_sample_cannot_acknowledge_current_incident(tmp_path, monkeypatch):
    h = HealthRun(tmp_path, monkeypatch, "A")
    h.invalid("A")
    hp.main()
    _watch_samples(tmp_path,"A",h.now,[(-60,OFFLINE)])
    assert hp.main() == 1 and h.send.call_count == 1


# ---------------------------------------------------------------- 空转（probe_silent）
def _watch_row(tmp_path, tag, is_live, now, ts=None):
    """写一条探测记录（watch_<tag>.jsonl 的最后一行）。"""
    import json
    import os
    p = tmp_path / ("watch_%s.jsonl" % tag)
    p.write_text(json.dumps({"ts": int(ts if ts is not None else now),
                             "is_live": is_live, "user_count": 120},
                            ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def _stats(tmp_path, tag, age_sec, now):
    import os
    import time as _t
    day = _t.strftime("%Y-%m-%d", _t.localtime(now))
    p = tmp_path / ("stats_douyin_%s_%s.jsonl" % (tag, day))
    p.write_text("{}\n", encoding="utf-8")
    os.utime(p, (now - age_sec, now - age_sec))
    return p


def test_silent_when_live_but_stats_old(tmp_path, monkeypatch):
    """在播 + stats 几分钟没写 = 采集空转（2026-10-29 那种静默停滞）。"""
    now = 1793000000.0
    monkeypatch.setattr(hp, "DATA", tmp_path)
    _watch_row(tmp_path, "A", True, now)
    _stats(tmp_path, "A", hp.SILENT_SEC + 60, now)
    got = hp.probe_silent(_roster("A"), now)
    assert [g["tag"] for g in got] == ["A"]
    assert got[0]["online"] == 120


def test_not_silent_when_stats_fresh(tmp_path, monkeypatch):
    now = 1793000000.0
    monkeypatch.setattr(hp, "DATA", tmp_path)
    _watch_row(tmp_path, "A", True, now)
    _stats(tmp_path, "A", 30, now)
    assert hp.probe_silent(_roster("A"), now) == []


def test_offline_is_not_silent(tmp_path, monkeypatch):
    """没在播的时候 stats 本来就不写，不能报空转（否则半夜天天误报）。"""
    now = 1793000000.0
    monkeypatch.setattr(hp, "DATA", tmp_path)
    _watch_row(tmp_path, "A", False, now)
    _stats(tmp_path, "A", 3600, now)
    assert hp.probe_silent(_roster("A"), now) == []


def test_stale_watch_not_reported_as_silent(tmp_path, monkeypatch):
    """探测行本身太旧 → 那是「停写」，归 probe()，别在空转里重复报一遍。"""
    now = 1793000000.0
    monkeypatch.setattr(hp, "DATA", tmp_path)
    _watch_row(tmp_path, "A", True, now, ts=now - hp.STALE_SEC - 60)
    _stats(tmp_path, "A", 3600, now)
    assert hp.probe_silent(_roster("A"), now) == []


def test_missing_stats_is_silent(tmp_path, monkeypatch):
    """在播但今天根本没 stats 文件（采集从没起来过）→ 也要报。"""
    now = 1793000000.0
    monkeypatch.setattr(hp, "DATA", tmp_path)
    _watch_row(tmp_path, "A", True, now)
    got = hp.probe_silent(_roster("A"), now)
    assert len(got) == 1 and got[0]["age"] >= 86400


def test_silent_skips_disabled(tmp_path, monkeypatch):
    now = 1793000000.0
    monkeypatch.setattr(hp, "DATA", tmp_path)
    r = {"anchors": [{"tag": "Off", "watch": False, "enabled": True}]}
    assert hp.probe_silent(r, now) == []


def test_last_json_line_tolerates_half_line(tmp_path):
    """尾部可能是半行（正在写）→ 要往前找回最后一条完整 JSON。"""
    p = tmp_path / "x.jsonl"
    p.write_text('{"ts": 1, "is_live": true}\n{"ts": 2, "is_li', encoding="utf-8")
    assert hp._last_json_line(p) == {"ts": 1, "is_live": True}


# ---------------------------------------------------------------- 空转（probe_silent）
def _watch_row(tmp_path, tag, is_live, now, ts=None):
    """写一条探测记录（watch_<tag>.jsonl 的最后一行）。"""
    import json
    import os
    p = tmp_path / ("watch_%s.jsonl" % tag)
    p.write_text(json.dumps({"ts": int(ts if ts is not None else now),
                             "is_live": is_live, "user_count": 120},
                            ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def _stats(tmp_path, tag, age_sec, now):
    import os
    import time as _t
    day = _t.strftime("%Y-%m-%d", _t.localtime(now))
    p = tmp_path / ("stats_douyin_%s_%s.jsonl" % (tag, day))
    p.write_text("{}\n", encoding="utf-8")
    os.utime(p, (now - age_sec, now - age_sec))
    return p


def test_silent_when_live_but_stats_old(tmp_path, monkeypatch):
    """在播 + stats 几分钟没写 = 采集空转（2026-10-29 那种静默停滞）。"""
    now = 1793000000.0
    monkeypatch.setattr(hp, "DATA", tmp_path)
    _watch_row(tmp_path, "A", True, now)
    _stats(tmp_path, "A", hp.SILENT_SEC + 60, now)
    got = hp.probe_silent(_roster("A"), now)
    assert [g["tag"] for g in got] == ["A"]
    assert got[0]["online"] == 120


def test_not_silent_when_stats_fresh(tmp_path, monkeypatch):
    now = 1793000000.0
    monkeypatch.setattr(hp, "DATA", tmp_path)
    _watch_row(tmp_path, "A", True, now)
    _stats(tmp_path, "A", 30, now)
    assert hp.probe_silent(_roster("A"), now) == []


def test_offline_is_not_silent(tmp_path, monkeypatch):
    """没在播的时候 stats 本来就不写，不能报空转（否则半夜天天误报）。"""
    now = 1793000000.0
    monkeypatch.setattr(hp, "DATA", tmp_path)
    _watch_row(tmp_path, "A", False, now)
    _stats(tmp_path, "A", 3600, now)
    assert hp.probe_silent(_roster("A"), now) == []


def test_stale_watch_not_reported_as_silent(tmp_path, monkeypatch):
    """探测行本身太旧 → 那是「停写」，归 probe()，别在空转里重复报一遍。"""
    now = 1793000000.0
    monkeypatch.setattr(hp, "DATA", tmp_path)
    _watch_row(tmp_path, "A", True, now, ts=now - hp.STALE_SEC - 60)
    _stats(tmp_path, "A", 3600, now)
    assert hp.probe_silent(_roster("A"), now) == []


def test_missing_stats_is_silent(tmp_path, monkeypatch):
    """在播但今天根本没 stats 文件（采集从没起来过）→ 也要报。"""
    now = 1793000000.0
    monkeypatch.setattr(hp, "DATA", tmp_path)
    _watch_row(tmp_path, "A", True, now)
    got = hp.probe_silent(_roster("A"), now)
    assert len(got) == 1 and got[0]["age"] >= 86400


def test_silent_skips_disabled(tmp_path, monkeypatch):
    now = 1793000000.0
    monkeypatch.setattr(hp, "DATA", tmp_path)
    r = {"anchors": [{"tag": "Off", "watch": False, "enabled": True}]}
    assert hp.probe_silent(r, now) == []


def test_last_json_line_tolerates_half_line(tmp_path):
    """尾部可能是半行（正在写）→ 要往前找回最后一条完整 JSON。"""
    p = tmp_path / "x.jsonl"
    p.write_text('{"ts": 1, "is_live": true}\n{"ts": 2, "is_li', encoding="utf-8")
    assert hp._last_json_line(p) == {"ts": 1, "is_live": True}
