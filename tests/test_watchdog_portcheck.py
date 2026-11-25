# -*- coding: utf-8 -*-
"""run_watchdog._wait_with_port_check 的单测（2026-10-05 补：子进程运行中也要体检浏览器）。"""
import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import run_watchdog as rw  # noqa: E402


def test_clear_crash_flag_rewrites_preferences(tmp_path, capsys):
    """Crashed 标记被改写为 Normal，且日志说明动作。"""
    prefs_dir = tmp_path / "Default"
    prefs_dir.mkdir()
    prefs = prefs_dir / "Preferences"
    prefs.write_text(json.dumps({"profile": {"exit_type": "Crashed"}}), encoding="utf-8")
    rw.clear_crash_flag(str(tmp_path))
    d = json.loads(prefs.read_text(encoding="utf-8"))
    assert d["profile"]["exit_type"] == "Normal"
    assert d["profile"]["exited_cleanly"] is True


def test_clear_crash_flag_silent_when_no_file(tmp_path):
    """profile 目录还没有 Preferences（首启）→ 不报错。"""
    rw.clear_crash_flag(str(tmp_path))          # 不应抛异常


class FakeProc:
    """wait() 前两次抛 TimeoutExpired，第三次返回退出码 7。"""

    def __init__(self):
        self.calls = 0

    def wait(self, timeout=None):
        self.calls += 1
        if self.calls <= 2:
            raise subprocess.TimeoutExpired(cmd="watch", timeout=timeout)
        return 7


def test_port_checked_while_child_running(monkeypatch):
    proc = FakeProc()
    state = {"checks": 0, "restarts": 0}

    def fake_port_alive():
        state["checks"] += 1
        return state["checks"] > 1    # 第一次体检发现端口死，之后恢复

    def fake_ensure(env):
        state["restarts"] += 1
        return True

    monkeypatch.setattr(rw, "port_alive", fake_port_alive)
    monkeypatch.setattr(rw, "ensure_browser", fake_ensure)
    monkeypatch.setattr(rw, "_alert_browser_drop", lambda *args, **kwargs: False)
    rc = rw._wait_with_port_check(proc, {}, check_seconds=0.01)
    assert rc == 7
    assert state["restarts"] == 1     # 端口死时被重拉了一次


def test_no_restart_when_port_alive(monkeypatch):
    proc = FakeProc()
    state = {"restarts": 0}

    def fake_port_alive():
        return True

    def fake_ensure(env):
        state["restarts"] += 1

    monkeypatch.setattr(rw, "port_alive", fake_port_alive)
    monkeypatch.setattr(rw, "ensure_browser", fake_ensure)
    rc = rw._wait_with_port_check(proc, {}, check_seconds=0.01)
    assert rc == 7
    assert state["restarts"] == 0     # 端口一直活着就不动浏览器
