"""Launcher regressions using synthetic rosters and bounded local processes only."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import time
import types
from unittest.mock import Mock

import pytest


LAUNCH_PATH = Path(__file__).resolve().parents[1] / "scripts" / "launch_all.py"


@pytest.fixture
def launcher(monkeypatch):
    # Importing launch_all bootstraps ANCHORS. Never import the actual roster here.
    roster = types.ModuleType("anchor_roster")
    roster.SEED = [{"tag": "synthetic", "sec_uid": "synthetic-id"}]
    roster.ensure = lambda: ({"anchors": []}, False)
    roster.as_legacy_anchors = lambda _value: []
    monkeypatch.setitem(sys.modules, "anchor_roster", roster)
    monkeypatch.setattr(sys, "path", sys.path[:])
    spec = importlib.util.spec_from_file_location("synthetic_launch_all", LAUNCH_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, roster


def _bootstrap_case(roster, case):
    if case == "ensure-error":
        roster.ensure = Mock(side_effect=OSError("synthetic unreadable roster"))
    elif case == "legacy-error":
        roster.as_legacy_anchors = Mock(side_effect=ValueError("synthetic bad roster"))
    else:
        roster.as_legacy_anchors = lambda _value: []


@pytest.mark.parametrize("case", ["ensure-error", "legacy-error", "empty"])
def test_required_bootstrap_never_returns_seed(launcher, monkeypatch, case):
    module, roster = launcher
    monkeypatch.setenv("WB_REQUIRE_EXISTING_STATE", "1")
    _bootstrap_case(roster, case)
    assert module._bootstrap_anchors() == []


@pytest.mark.parametrize("case", ["ensure-error", "legacy-error", "empty"])
def test_legacy_bootstrap_keeps_seed_fallback(launcher, monkeypatch, case):
    module, roster = launcher
    monkeypatch.delenv("WB_REQUIRE_EXISTING_STATE", raising=False)
    _bootstrap_case(roster, case)
    assert module._bootstrap_anchors() == [("synthetic", "synthetic-id", "synthetic")]


def test_required_bootstrap_keeps_valid_roster(launcher, monkeypatch):
    module, roster = launcher
    monkeypatch.setenv("WB_REQUIRE_EXISTING_STATE", "1")
    expected = [("existing", "existing-id", "existing")]
    roster.as_legacy_anchors = lambda _value: expected
    assert module._bootstrap_anchors() == expected


def test_launch_disconnects_all_parent_stdio(launcher, monkeypatch, tmp_path):
    module, _roster = launcher
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "instance_running", lambda _tag: False)
    popen = Mock()
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    module.launch("synthetic", "synthetic-id", "synthetic", llm=False)
    kwargs = popen.call_args.kwargs
    assert {name: kwargs.get(name) for name in ("stdin", "stdout", "stderr")} == {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }


@pytest.mark.skipif(sys.platform != "win32", reason="Windows pipe inheritance regression")
def test_captured_launcher_returns_while_watchdog_survives(tmp_path):
    ready = tmp_path / "ready"
    heartbeat = tmp_path / "heartbeat"
    release = tmp_path / "release"
    finished = tmp_path / "finished"
    worker = tmp_path / "synthetic_watchdog.py"
    worker.write_text(
        "import time\nfrom pathlib import Path\n"
        f"ready = Path({str(ready)!r})\n"
        f"heartbeat = Path({str(heartbeat)!r})\n"
        f"release = Path({str(release)!r})\n"
        f"finished = Path({str(finished)!r})\n"
        "ready.write_text('ready', encoding='ascii')\n"
        "deadline = time.monotonic() + 20\n"
        "count = 0\n"
        "while not release.exists() and time.monotonic() < deadline:\n"
        "    count += 1\n"
        "    heartbeat.write_text(str(count), encoding='ascii')\n"
        "    time.sleep(0.05)\n"
        "finished.write_text('finished', encoding='ascii')\n",
        encoding="utf-8",
    )
    # The short launcher imports only its own module plus a synthetic roster.
    code = (
        "import importlib.util, sys, types\nfrom pathlib import Path\n"
        "roster = types.ModuleType('anchor_roster')\n"
        "roster.SEED = []\n"
        "roster.ensure = lambda: ({'anchors': []}, False)\n"
        "roster.as_legacy_anchors = lambda value: []\n"
        "sys.modules['anchor_roster'] = roster\n"
        f"spec = importlib.util.spec_from_file_location('synthetic_launch', {str(LAUNCH_PATH)!r})\n"
        "module = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(module)\n"
        f"module.ROOT = Path({str(tmp_path)!r})\n"
        "module.PY = sys.executable\n"
        f"module.SCRIPT = {str(worker)!r}\n"
        "module.instance_running = lambda tag: False\n"
        "module.launch('synthetic', 'synthetic-id', 'synthetic', llm=False)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=str(tmp_path), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "synthetic watchdog did not start"
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            pytest.fail("captured launcher still waits for its long-lived watchdog's pipes")
        assert process.returncode == 0, (stdout, stderr)
        assert not finished.exists()
        before = heartbeat.read_text(encoding="ascii") if heartbeat.exists() else ""
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if heartbeat.exists() and heartbeat.read_text(encoding="ascii") not in ("", before):
                break
            time.sleep(0.02)
        else:
            pytest.fail("synthetic watchdog stopped when its launcher exited")
    finally:
        # Graceful release plus a hard worker deadline; no PID-file-based killing.
        release.touch()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
        deadline = time.monotonic() + 5
        while ready.exists() and not finished.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not ready.exists() or finished.exists(), "synthetic watchdog did not cleanly exit"
