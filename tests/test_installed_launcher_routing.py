"""Synthetic launcher routing only; no native process or startup task is created."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from scripts import local_installation as installation
from scripts import local_app as app
from scripts import local_autostart as autostart


@pytest.fixture
def installed(tmp_path, monkeypatch):
    assert "task-11B1" in str(tmp_path)
    target = tmp_path / "新 客户 $() ` 安装"
    target.mkdir()
    (target / "new-generated-only.marker").write_text("task11B1 synthetic", encoding="utf-8")
    state, root = target / "state", target / "app"
    layout = dict(install=target, root=root, data=state / "data", config=state / "config.json",
        roster=state / "anchors.json", dist=root / "web/dist", python=target / "runtime/python/python.exe",
        profile=state / "data/.local_app/browser-profile", port=18765, cdp_port=19222)
    monkeypatch.setattr(installation, "_ACTIVE", layout)
    return layout, installation._expected_context(layout)


def test_installed_plan_targets_stable_launcher_and_binds_exact_context(installed):
    layout, context = installed
    result = autostart.manage("plan", context)
    command = installation.installed_launcher_command(context)
    assert result["ok"] and result["changed"] is False
    assert result["plan"]["command"] == command
    assert command[:4] == [str(layout["install"] / "entry/CustomerLauncherW.exe"), "run", "--target", str(layout["install"])]
    assert dict(zip(command[4::2], command[5::2])) == {
        "--" + name.replace("_", "-"): str(context[name])
        for name in ("root", "config", "data", "roster", "dist", "python", "port", "cdp_port")}
    assert result["plan"]["arguments"] == subprocess.list2cmdline(command[1:])
    marker = json.loads(result["plan"]["owner_marker"])
    assert marker["binding"] == hashlib.sha256(json.dumps(command, ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest()
    assert not layout["data"].exists() and not layout["config"].exists()


def test_legacy_plan_keeps_original_direct_command(installed, monkeypatch):
    _layout, context = installed
    monkeypatch.setattr(installation, "_ACTIVE", None)
    monkeypatch.setattr(installation, "load_installation", lambda: None)
    assert installation.installed_launcher_command(context) is None
    expected = [str(Path(context["python"]).with_name("pythonw.exe")), str(Path(context["root"]) / "scripts/local_app.py"), "run"]
    for name in ("root", "config", "data", "roster", "dist", "python", "port", "cdp_port"):
        expected += ["--" + name.replace("_", "-"), str(context[name])]
    assert autostart.plan(context)["command"] == expected


@pytest.mark.parametrize("name", ["root", "config", "data", "roster", "dist", "python", "port", "cdp_port"])
def test_foreign_installed_context_cannot_route_through_its_launcher(installed, name):
    layout, context = installed
    altered = dict(context)
    altered[name] = altered[name] + 1 if isinstance(altered[name], int) else altered[name] + "-foreign"
    with pytest.raises(installation.InstallationError, match="installation_context_mismatch"):
        installation.installed_launcher_command(altered)
    result = autostart.manage("plan", altered)
    assert result == {"ok": False, "state": "invalid_context", "changed": False}
    assert not layout["data"].exists()


def test_missing_launcher_is_reported_without_python_fallback(installed):
    _layout, context = installed
    result = autostart.manage("enable", context)
    assert result == {"ok": False, "state": "launcher_missing", "changed": False}


@pytest.mark.parametrize("fault", [None, "parent", "nonce", "created"])
def test_start_preserves_nonce_direct_parent_and_creation_checks(installed, monkeypatch, fault):
    layout, context = installed
    spawned = []
    class Lock:
        def __init__(self, *_args, **_kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def acquire(self, **_kwargs): return True
        def release(self): pass
    def spawn(command, **kwargs):
        spawned.append((command, kwargs))
        assert kwargs["close_fds"] is True
        assert kwargs["env"]["DY_LOCAL_LAUNCH_NONCE"]
        return SimpleNamespace(pid=88, poll=lambda: None)
    def status(_context):
        if not spawned:
            return {"ok": True, "state": "stopped"}
        nonce = spawned[0][1]["env"]["DY_LOCAL_LAUNCH_NONCE"]
        return {"ok": True, "state": "waiting", "process_state": "alive", "board_ready": True,
            "owner": {"pid": 99, "created": 9 if fault == "created" else 11},
            "launch_nonce": "wrong" if fault == "nonce" else nonce}
    clock = iter(number / 10 for number in range(100))
    monkeypatch.setattr(app, "FileLock", Lock)
    monkeypatch.setattr(app, "status", status)
    monkeypatch.setattr(app, "check_configuration", lambda _context: None)
    monkeypatch.setattr(app, "listener_pid", lambda _port: {"state": "vacant"})
    monkeypatch.setattr(app, "installed_child_environment", lambda _context: {})
    monkeypatch.setattr(app.subprocess, "Popen", spawn)
    monkeypatch.setattr(app, "process_identity", lambda pid: {"pid": pid, "created": 10, "state": "alive"})
    monkeypatch.setattr(app, "process_fact", lambda _owner: "alive")
    monkeypatch.setattr(app, "process_parent_pid", lambda _pid: 777 if fault == "parent" else 88)
    monkeypatch.setattr(app.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(app.time, "sleep", lambda _seconds: None)
    result = app.start(context, wait=.5)
    assert len(spawned) == 1
    assert spawned[0][0] == installation.installed_launcher_command(context)
    assert result["state"] == ("waiting" if fault is None else "startup_timeout")
    assert result["ok"] is (fault is None)
    assert (layout["data"] / ".local_app/app.log").is_file()
