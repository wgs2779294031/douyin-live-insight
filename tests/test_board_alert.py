# -*- coding: utf-8 -*-
"""采集异常兜底告警单测：判据（不误报「没播」）、冷却、恢复清文件。

不写真实桌面/data：STATE_PATH / LOG_PATH / 桌面目录全部 monkeypatch 到 tmp。
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import board_alert as ba  # noqa: E402

UID = "MS4wLjABAAAA" + "x" * 40
# 时间基准：必须用「现在附近」的 epoch，否则 now-0 可能小于冷却窗口，fresh 恒为空
NOW = 1793000000


def _status(*entries):
    """entries: (tag, state) → 伪造 status_all 的返回。"""
    anchors = []
    for tag, state in entries:
        anchors.append({
            "tag": tag, "state": state, "enabled": True,
            "watch_age_sec": 3600 if state == "stalled" else 10,
            "danmu_age_sec": 3600 if state == "silent" else 10,
        })
    bad = [a for a in anchors if a["state"] in ("stalled", "stopped", "silent")]
    return {"anchors": anchors,
            "health": {"total": len(anchors), "enabled": len(anchors),
                       "ok": len(anchors) - len(bad), "stalled": 0,
                       "stopped": 0, "silent": 0, "problems": []}}


def _patch(tmp_path, monkeypatch):
    monkeypatch.setattr(ba, "DATA", tmp_path)
    monkeypatch.setattr(ba, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(ba, "LOG_PATH", tmp_path / "alerts.log")
    desktop = tmp_path / "Desktop"
    desktop.mkdir(exist_ok=True)
    monkeypatch.setattr(ba, "_desktop_path", lambda: desktop)
    return desktop


def test_no_alert_when_all_ok(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    r = ba.check_and_alert({}, _status(("A", "ok"), ("B", "ok")), now=NOW)
    assert r["bad"] == 0 and r["recovered"] is True
    assert not (tmp_path / "Desktop" / ba.DESKTOP_NAME).exists()


def test_alert_writes_desktop_and_log(tmp_path, monkeypatch):
    d = _patch(tmp_path, monkeypatch)
    r = ba.check_and_alert({}, _status(("A", "stalled"), ("B", "stopped")), now=NOW)
    assert r["bad"] == 2 and r["alerted"] == 2
    f = d / ba.DESKTOP_NAME
    assert f.exists()
    text = f.read_text(encoding="utf-8")
    assert "采集异常 2 路" in text
    assert "A" in text and "B" in text
    assert "launch_all.py --headed" in text        # 无头会被风控拦，这条必须写进提示
    assert (tmp_path / "alerts.log").exists()


def test_disabled_does_not_alert(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    st = _status(("A", "disabled"))
    st["anchors"][0]["enabled"] = False
    r = ba.check_and_alert({}, st, now=NOW)
    assert r["bad"] == 0


def test_cooldown_suppresses_repeat(tmp_path, monkeypatch):
    d = _patch(tmp_path, monkeypatch)
    ba.check_and_alert({}, _status(("A", "stalled")), now=NOW)
    first = (tmp_path / "alerts.log").read_text(encoding="utf-8")
    # 冷却期内再查：不新增日志
    r2 = ba.check_and_alert({}, _status(("A", "stalled")), now=NOW + 100)
    assert r2["alerted"] == 0
    assert (tmp_path / "alerts.log").read_text(encoding="utf-8") == first
    # 冷却过了：重新告警
    r3 = ba.check_and_alert({}, _status(("A", "stalled")), now=NOW + ba.COOLDOWN_SEC + 1)
    assert r3["alerted"] == 1


def test_recovery_removes_desktop_file(tmp_path, monkeypatch):
    d = _patch(tmp_path, monkeypatch)
    ba.check_and_alert({}, _status(("A", "stalled")), now=NOW)
    assert (d / ba.DESKTOP_NAME).exists()
    r = ba.check_and_alert({}, _status(("A", "ok")), now=NOW + 1000)
    assert r["recovered"] is True
    assert not (d / ba.DESKTOP_NAME).exists()
    # 冷却状态也清空 → 下次再坏会立刻告警
    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8")) == {}


def test_silent_also_alerts(tmp_path, monkeypatch):
    """在播但 10 分钟无弹幕 → 也算异常（silent）。"""
    _patch(tmp_path, monkeypatch)
    r = ba.check_and_alert({}, _status(("A", "silent")), now=NOW)
    assert r["bad"] == 1
