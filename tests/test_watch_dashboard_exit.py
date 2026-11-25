"""The normal exit build must finish (or time out) before the owned Job closes."""
import pathlib
import subprocess
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import watch_anchor_live as watch


def test_dashboard_rebuild_waits_with_a_deadline(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(watch.subprocess, "run", run)
    assert watch.rebuild_dashboard(timeout=7) is True
    assert calls[0][1]["timeout"] == 7
    assert calls[0][1]["encoding"] == "utf-8"
    assert pathlib.Path(calls[0][0][1]).name == "dashboard_build.py"


def test_dashboard_failure_does_not_claim_completion(monkeypatch, capsys):
    monkeypatch.setattr(watch.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=3))
    assert watch.rebuild_dashboard() is False
    assert "重算完成" not in capsys.readouterr().out


def test_dashboard_timeout_is_bounded_and_not_fatal(monkeypatch):
    def run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
    monkeypatch.setattr(watch.subprocess, "run", run)
    assert watch.rebuild_dashboard(timeout=.1) is False
