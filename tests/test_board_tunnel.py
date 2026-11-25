# -*- coding: utf-8 -*-
"""公网隧道脚本单测：命令拼装 / pidfile 防双开 / 判活边界。

不真起 ssh（测试环境不应联网），只验可测的纯逻辑。
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import board_tunnel as bt  # noqa: E402


def test_build_cmd_contains_reverse_forward():
    cmd = bt.build_cmd(host="1.2.3.4", remote_port=18765, local_port=8765,
                       key=pathlib.Path("/tmp/k"))
    assert cmd[0] == "ssh"
    assert "-N" in cmd
    i = cmd.index("-R")
    assert cmd[i + 1] == "18765:127.0.0.1:8765"
    assert cmd[-1] == "root@1.2.3.4"
    assert str(pathlib.Path("/tmp/k")) in cmd


def test_build_cmd_is_noninteractive():
    """必须 BatchMode + ExitOnForwardFailure：无窗口运行时不能弹密码、不能假装活着。"""
    cmd = bt.build_cmd()
    joined = " ".join(cmd)
    assert "BatchMode=yes" in joined
    assert "ExitOnForwardFailure=yes" in joined
    assert "ServerAliveInterval=30" in joined


def test_pid_alive_rejects_bad_pid():
    assert bt.pid_alive(0) is False
    assert bt.pid_alive(-5) is False


def test_pid_alive_self():
    import os
    assert bt.pid_alive(os.getpid()) is True


def test_acquire_pidfile_blocks_second_instance(tmp_path, monkeypatch):
    pidfile = tmp_path / "t.pid"
    monkeypatch.setattr(bt, "PIDFILE", pidfile)
    monkeypatch.setattr(bt, "DATA", tmp_path)
    # 首次进入：拿到锁
    assert bt.acquire_pidfile() is True
    assert pidfile.read_text(encoding="utf-8").strip() == str(__import__("os").getpid())
    # 再进一次：同 pid 视为自己，允许（幂等）
    assert bt.acquire_pidfile() is True


def test_acquire_pidfile_replaces_stale(tmp_path, monkeypatch):
    """pidfile 里是死进程 → 直接接管，不能让残留把自启锁死。"""
    pidfile = tmp_path / "t.pid"
    pidfile.write_text("999999", encoding="utf-8")
    monkeypatch.setattr(bt, "PIDFILE", pidfile)
    monkeypatch.setattr(bt, "DATA", tmp_path)
    monkeypatch.setattr(bt, "pid_alive", lambda pid: False)
    assert bt.acquire_pidfile() is True
