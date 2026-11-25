# -*- coding: utf-8 -*-
"""board_watchdog 单测（2026-10-21）：端口探活判活、隐藏拉起、连续失败告警、防双开。

纪律同 test_assist_board：全部指向 tmp_path，绝不写真实 data/
（autouse fixture 把 LOG_PATH 也钉到 tmp，tick 内部落日志不会污染真实目录）。
"""
import subprocess
import sys
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import board_watchdog as bw  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_log(tmp_path, monkeypatch):
    """log() 走模块级 LOG_PATH（调用时读），钉到 tmp 即不碰真实 data/。"""
    monkeypatch.setattr(bw, "LOG_PATH", tmp_path / "board_watchdog.log")


# ---------------------------------------------------------------- 探活

class _Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, []

    def open(self, url, timeout=None):
        self.calls.append((url, timeout))
        if self.exc:
            raise self.exc
        return self.resp


def test_board_alive_true_on_200(monkeypatch):
    op = _Opener(_Resp(200))
    monkeypatch.setattr(bw, "_OPENER", op)
    assert bw.board_alive() is True
    assert op.calls and op.calls[0][0] == bw.SNAPSHOT_URL


def test_board_alive_false_on_refused(monkeypatch):
    monkeypatch.setattr(bw, "_OPENER", _Opener(exc=urllib.error.URLError("refused")))
    assert bw.board_alive() is False


def test_board_alive_false_on_non_200(monkeypatch):
    monkeypatch.setattr(bw, "_OPENER", _Opener(_Resp(503)))
    assert bw.board_alive() is False


def test_board_alive_true_against_real_http():
    """真起一个只回 200 的 server：证明探活链路（含绕代理）真的能拿到 200。"""
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        assert bw.board_alive(timeout=2.0,
                              url="http://127.0.0.1:%d/snapshot" % port) is True
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------- 拉起

def test_launch_board_hidden_python(monkeypatch, tmp_path):
    seen = {}

    def fake_popen(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return object()

    monkeypatch.setattr(bw.subprocess, "Popen", fake_popen)
    bw.launch_board(log_path=tmp_path / "board.log")

    cmd = seen["cmd"]
    assert cmd[1] == str(bw.BOARD_SCRIPT)
    assert cmd[0].lower().endswith("python.exe") or cmd[0] == sys.executable
    assert seen["kw"]["cwd"] == str(bw.ROOT)
    # 红线：长驻看板禁止 pythonw，必须 CREATE_NO_WINDOW 隐藏
    assert seen["kw"]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert "pythonw" not in cmd[0].lower()


def test_launch_board_tolerates_bad_log_path(monkeypatch, tmp_path):
    monkeypatch.setattr(bw.subprocess, "Popen",
                        lambda cmd, **kw: object())
    # 日志目录不存在 → 回落 DEVNULL，不能因此拉不起来
    bad = tmp_path / "no_such_dir" / "x.log"
    assert bw.launch_board(log_path=bad) is not None


# ---------------------------------------------------------------- 复探

def test_wait_until_alive_returns_true(monkeypatch):
    seq = iter([False, False, True])
    assert bw.wait_until_alive(seconds=5, interval=0.01,
                               probe=lambda: next(seq)) is True


def test_wait_until_alive_times_out():
    assert bw.wait_until_alive(seconds=0.05, interval=0.01,
                               probe=lambda: False) is False


# ---------------------------------------------------------------- 决策核心

def _guard(probes, waits=True, alerts=None):
    """probes: 每次 probe 依次返回的布尔值（末项常驻）。"""
    it = iter(probes)
    last = {"v": probes[-1] if probes else False}

    def probe():
        try:
            last["v"] = next(it)
        except StopIteration:
            pass
        return last["v"]

    calls = alerts if alerts is not None else []
    g = bw.Guard(probe=probe, launch=lambda: calls.append("launch"),
                 wait=lambda: waits,
                 alert=lambda text, key="board-watch": calls.append((key, text)))
    return g, calls


def test_guard_alive_does_not_launch():
    g, calls = _guard([True])
    assert g.tick() == "alive"
    assert calls == []


def test_guard_revives_when_dead():
    g, calls = _guard([False, True], waits=True)
    assert g.tick() == "revived"
    assert calls[0] == "launch"
    assert calls[1][0] == "board-relaunch"
    assert g.revives == 1 and g.fails == 0


def test_guard_alerts_after_three_failures():
    g, calls = _guard([False], waits=False)
    actions = [g.tick() for _ in range(3)]
    assert actions == ["revive_failed", "revive_failed", "need_human"]
    alerts = [c for c in calls if isinstance(c, tuple)]
    assert len(alerts) == 1
    assert alerts[0][0] == "board-watch"
    assert "3 次仍失败" in alerts[0][1]
    assert calls.count("launch") == 3
    # 告警后计数清零：下一轮重新累计，不会每轮刷屏
    assert g.fails == 0
    assert g.tick() == "revive_failed"


def test_guard_alert_survives_exception():
    """告警通道抛异常不能把 tick 打崩（监控先活，告警尽力）。"""
    def boom(*a, **kw):
        raise RuntimeError("webhook down")

    g = bw.Guard(probe=lambda: False, launch=lambda: None, wait=lambda: False,
                 alert=boom, fail_limit=1)
    assert g.tick() == "need_human"


# ---------------------------------------------------------------- pid / 防双开

def test_pid_alive_does_not_kill():
    import os
    assert bw.pid_alive(os.getpid()) is True
    assert bw.pid_alive(999999) is False
    assert bw.pid_alive(0) is False


def test_pidfile_blocks_second_instance(tmp_path):
    pf = tmp_path / "board_watchdog.pid"
    pf.write_text("4242", encoding="utf-8")
    assert bw.acquire_pidfile(pf, alive=lambda pid: pid == 4242) is False


def test_pidfile_rewritten_when_stale(tmp_path):
    pf = tmp_path / "board_watchdog.pid"
    pf.write_text("4242", encoding="utf-8")
    assert bw.acquire_pidfile(pf, alive=lambda pid: False) is True
    import os
    assert int(pf.read_text(encoding="utf-8")) == os.getpid()


def test_pidfile_written_when_missing(tmp_path):
    pf = tmp_path / "sub" / "board_watchdog.pid"
    assert bw.acquire_pidfile(pf, alive=lambda pid: True) is True
    assert pf.exists()


def test_once_mode_exits_zero_when_alive(monkeypatch, tmp_path):
    """--once 冒烟：活着返回 0，且不写 pidfile（不跟常驻实例抢）。"""
    monkeypatch.setattr(bw, "DATA", tmp_path)
    monkeypatch.setattr(bw, "PIDFILE", tmp_path / "board_watchdog.pid")
    monkeypatch.setattr(bw, "board_alive", lambda *a, **kw: True)
    assert bw.main(["--once"]) == 0
    assert not (tmp_path / "board_watchdog.pid").exists()


def test_once_mode_returns_1_when_launch_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(bw, "DATA", tmp_path)
    monkeypatch.setattr(bw, "PIDFILE", tmp_path / "board_watchdog.pid")
    monkeypatch.setattr(bw, "board_alive", lambda *a, **kw: False)
    monkeypatch.setattr(bw, "launch_board", lambda *a, **kw: None)
    monkeypatch.setattr(bw, "wait_until_alive", lambda *a, **kw: False)
    assert bw.main(["--once"]) == 1
