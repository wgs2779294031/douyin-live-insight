# -*- coding: utf-8 -*-
"""浏览器掉线告警的「去刷屏」测试。

背景（2026-10-29 老大截图）：飞书群连着刷
`[竞品监控] 浏览器掉线，看门狗已自动重拉（若频繁出现说明 Chrome 不稳定…）`
——八路 watchdog 是**八个进程**，而 notify 的 cooldown 是进程内字典，
同一件事每路各报一条；文案还写死 Chrome，实际跑的是 Edge，排查方向被带偏。

改后：跨进程共享计数，窗口内累计到阈值才喊一次，其余只写日志。
"""

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import run_watchdog as rw  # noqa: E402


def _setup(tmp_path, monkeypatch, sent, browser="edge"):
    monkeypatch.setattr(rw, "_BROWSER_ALERT_FILE", tmp_path / "alert.json")
    monkeypatch.setattr(rw, "browser_name", lambda env=None: browser)
    monkeypatch.setattr(rw.notify, "send_text",
                        lambda text, **kw: sent.append(text) or True)


def _state(tmp_path):
    return json.loads((tmp_path / "alert.json").read_text(encoding="utf-8"))


def test_first_two_drops_are_silent(tmp_path, monkeypatch):
    """偶发掉线自动重拉成功，不该叫人（叫了人只会去关告警）。"""
    sent = []
    _setup(tmp_path, monkeypatch, sent)
    assert rw._alert_browser_drop({}, "A") is False
    assert rw._alert_browser_drop({}, "B") is False
    assert sent == []
    assert _state(tmp_path)["count"] == 2


def test_third_drop_alerts_then_resets(tmp_path, monkeypatch):
    sent = []
    _setup(tmp_path, monkeypatch, sent)
    for _ in range(rw.BROWSER_ALERT_THRESHOLD - 1):
        rw._alert_browser_drop({}, "A")
    assert rw._alert_browser_drop({}, "A") is True
    assert len(sent) == 1
    # 发完清零：下一次掉线重新从 1 数，不会连着喊
    st = _state(tmp_path)
    assert st["count"] == 0
    assert st["last_alert"] > 0
    assert rw._alert_browser_drop({}, "A") is False
    assert len(sent) == 1


def test_alert_text_names_real_browser(tmp_path, monkeypatch):
    """文案必须写实际浏览器 —— 写死 Chrome 会把人带去看错进程。"""
    sent = []
    _setup(tmp_path, monkeypatch, sent, browser="edge")
    for _ in range(rw.BROWSER_ALERT_THRESHOLD):
        rw._alert_browser_drop({}, "A")
    assert "Edge" in sent[0]
    assert "Chrome" not in sent[0]


def test_alert_text_chrome_when_chrome(tmp_path, monkeypatch):
    sent = []
    _setup(tmp_path, monkeypatch, sent, browser="chrome")
    for _ in range(rw.BROWSER_ALERT_THRESHOLD):
        rw._alert_browser_drop({}, "A")
    assert "Chrome" in sent[0]


def test_window_expiry_restarts_count(tmp_path, monkeypatch):
    """半小时前的 5 次不算数 —— 老计数不能压着，否则一次抖动就触发告警。"""
    sent = []
    _setup(tmp_path, monkeypatch, sent)
    (tmp_path / "alert.json").write_text(json.dumps({
        "window_start": time.time() - rw.BROWSER_ALERT_WINDOW - 600,
        "count": 5, "last_alert": 0.0}), encoding="utf-8")
    assert rw._alert_browser_drop({}, "A") is False
    assert _state(tmp_path)["count"] == 1
    assert sent == []


def test_alert_respects_last_alert_cooldown(tmp_path, monkeypatch):
    """刚报过（窗口内）→ 即使又攒够次数也不再喊，避免「恢复-再爆」抖动刷屏。"""
    sent = []
    _setup(tmp_path, monkeypatch, sent)
    (tmp_path / "alert.json").write_text(json.dumps({
        "window_start": time.time() - 60, "count": 0,
        "last_alert": time.time() - 60}), encoding="utf-8")
    for _ in range(rw.BROWSER_ALERT_THRESHOLD):
        rw._alert_browser_drop({}, "A")
    assert sent == []


def test_corrupt_state_file_does_not_crash(tmp_path, monkeypatch):
    sent = []
    _setup(tmp_path, monkeypatch, sent)
    (tmp_path / "alert.json").write_text("not json", encoding="utf-8")
    assert rw._alert_browser_drop({}, "A") is False
    assert _state(tmp_path)["count"] == 1
