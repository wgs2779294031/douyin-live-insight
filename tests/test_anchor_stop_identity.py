"""停止实例必须验证 PID 归属；所有 taskkill 均模拟，不操作真实进程。"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import anchor_manager as am


def _pidfile(tmp_path, monkeypatch, kind="watch"):
    monkeypatch.setattr(am, "DATA", tmp_path)
    monkeypatch.setattr(am.time, "sleep", lambda seconds: None)
    path = am.pidfile(kind, "synthetic")
    path.write_text("12345", encoding="utf-8")
    os.utime(path, (1000, 1000))
    if kind == "watch":
        path.with_suffix(".protocol.json").write_text(
            json.dumps({"protocol": "owned-watch-job-v1", "pid": 12345, "created": 999}),
            encoding="utf-8")
    return path


@pytest.mark.parametrize("kind", ["watch", "record"])
@pytest.mark.parametrize("created", [1001, 2000])
def test_stop_never_kills_reused_pid_from_stale_pidfile(tmp_path, monkeypatch, kind, created):
    path = _pidfile(tmp_path, monkeypatch, kind)
    monkeypatch.setattr(am, "pid_alive", lambda pid: True)
    monkeypatch.setattr(am, "process_create_time", lambda pid: created)
    calls = []
    monkeypatch.setattr(am, "_run_hidden", lambda cmd: calls.append(cmd))
    result = am.stop(kind, "synthetic")
    assert calls == []
    assert result == {"ok": True, "state": "not_running"}
    assert not path.exists()


def test_stop_refuses_unknown_creation_time_without_discarding_identity(tmp_path, monkeypatch):
    path = _pidfile(tmp_path, monkeypatch)
    monkeypatch.setattr(am, "pid_alive", lambda pid: True)
    monkeypatch.setattr(am, "process_create_time", lambda pid: None)
    calls = []
    monkeypatch.setattr(am, "_run_hidden", lambda cmd: calls.append(cmd))
    result = am.stop("watch", "synthetic")
    assert calls == []
    assert result["ok"] is False
    assert result["state"] == "identity_unverified"
    assert path.exists()


def test_stop_refuses_unreadable_identity_timestamp(tmp_path, monkeypatch):
    path = _pidfile(tmp_path, monkeypatch)
    monkeypatch.setattr(am, "pid_alive", lambda pid: True)
    monkeypatch.setattr(am, "process_create_time", lambda pid: 999)
    real_stat = Path.stat

    def stat(self, *args, **kwargs):
        if self == path:
            raise PermissionError("synthetic identity access denied")
        return real_stat(self, *args, **kwargs)

    calls = []
    monkeypatch.setattr(am, "_run_hidden", lambda cmd: calls.append(cmd))
    with monkeypatch.context() as identity_patch:
        identity_patch.setattr(Path, "stat", stat)
        result = am.stop("watch", "synthetic")
    assert calls == []
    assert result["ok"] is False
    assert result["state"] == "identity_unverified"
    assert path.exists()


def test_stop_kills_only_verified_owned_tree(tmp_path, monkeypatch):
    path = _pidfile(tmp_path, monkeypatch)
    alive = [True]
    monkeypatch.setattr(am, "pid_alive", lambda pid: alive[0])
    monkeypatch.setattr(am, "process_create_time", lambda pid: 999)
    calls = []

    def kill(cmd):
        calls.append(cmd)
        alive[0] = False
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(am, "_run_hidden", kill)
    result = am.stop("watch", "synthetic")
    assert calls == [["taskkill", "/F", "/PID", "12345"]]
    assert result["ok"] is True and result["state"] == "stopped"
    assert not path.exists()


@pytest.mark.parametrize("marker", [None, {"protocol": "owned-watch-job-v1", "pid": 12345,
                                          "created": 998}])
def test_legacy_or_wrong_identity_protocol_is_not_guessed(tmp_path, monkeypatch, marker):
    path = _pidfile(tmp_path, monkeypatch)
    if marker is None:
        path.with_suffix(".protocol.json").unlink()
    else:
        path.with_suffix(".protocol.json").write_text(json.dumps(marker), encoding="utf-8")
    monkeypatch.setattr(am, "pid_alive", lambda pid: True)
    monkeypatch.setattr(am, "process_create_time", lambda pid: 999)
    calls = []
    monkeypatch.setattr(am, "_run_hidden", lambda cmd: calls.append(cmd))
    result = am.stop("watch", "synthetic")
    assert calls == []
    assert not result["ok"] and result["state"] == "legacy_cleanup_required"
    assert path.exists()


def test_record_stop_still_targets_its_owned_tree(tmp_path, monkeypatch):
    _pidfile(tmp_path, monkeypatch, "record")
    alive = [True]
    monkeypatch.setattr(am, "pid_alive", lambda pid: alive[0])
    monkeypatch.setattr(am, "process_create_time", lambda pid: 999)
    calls = []

    def kill(cmd):
        calls.append(cmd)
        alive[0] = False
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(am, "_run_hidden", kill)
    assert am.stop("record", "synthetic")["ok"]
    assert calls == [["taskkill", "/F", "/T", "/PID", "12345"]]


def test_restart_does_not_claim_success_or_spawn_after_legacy_stop_refusal(tmp_path, monkeypatch):
    path = _pidfile(tmp_path, monkeypatch)
    path.with_suffix(".protocol.json").unlink()
    monkeypatch.setattr(am, "pid_alive", lambda pid: True)
    monkeypatch.setattr(am, "process_create_time", lambda pid: 999)

    def forbidden_start(*args, **kwargs):
        raise AssertionError("failed stop must not become an apparently successful restart")

    monkeypatch.setattr(am, "start_anchor", forbidden_start)
    result = am.restart_anchor("synthetic", {"tag": "synthetic", "watch": True}, ("watch",))
    assert not result["started"]["watch"]["ok"]
    assert result["started"]["watch"]["state"] == "restart_blocked"


def test_reused_pid_after_successful_stop_is_not_treated_as_owned_running(tmp_path, monkeypatch):
    path = _pidfile(tmp_path, monkeypatch)
    creation = [999]
    monkeypatch.setattr(am, "pid_alive", lambda pid: True)
    monkeypatch.setattr(am, "process_create_time", lambda pid: creation[0])

    def kill(cmd):
        creation[0] = 2000  # original exited; its numeric PID now belongs elsewhere
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(am, "_run_hidden", kill)
    result = am.stop("watch", "synthetic")
    assert result["ok"] is True and result["state"] == "stopped"
    assert not path.exists()
