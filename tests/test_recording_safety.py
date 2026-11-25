"""Atomic metadata, dependency diagnostics and optional PID skip use only tmp/mocks."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import stream_recorder as recorder
import watch_anchor_live as watch


def session(file="seg_0001.flv"):
    return {"segments": [{"file": file, "bytes": 12}], "session_start": "synthetic",
            "session_end": "synthetic", "stop_reason": "signal", "web_rid": "synthetic"}


@pytest.mark.parametrize("raw", [b'{broken', b'[]', b'{"segments":"broken"}'])
def test_bad_meta_is_preserved_and_refused(tmp_path, raw):
    path = tmp_path / "meta.json"
    path.write_bytes(raw)
    with pytest.raises(RuntimeError):
        recorder.write_meta(tmp_path, session())
    assert path.read_bytes() == raw


def test_failed_meta_replace_keeps_original_and_cleans_own_temp(tmp_path, monkeypatch):
    recorder.write_meta(tmp_path, session())
    path = tmp_path / "meta.json"
    original = path.read_bytes()
    def fail(*_args):
        raise PermissionError("synthetic metadata replace failure")
    monkeypatch.setattr(recorder.os, "replace", fail)
    with pytest.raises(PermissionError):
        recorder.write_meta(tmp_path, session("seg_0002.flv"))
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


def test_first_meta_and_next_session_preserve_both_segments(tmp_path):
    recorder.write_meta(tmp_path, session())
    recorder.write_meta(tmp_path, session("seg_0002.flv"))
    saved = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert len(saved["sessions"]) == 2
    assert [row["file"] for row in saved["segments"]] == ["seg_0001.flv", "seg_0002.flv"]


def test_resolve_node_has_portable_missing_dependency_fallback(monkeypatch):
    monkeypatch.setattr(watch.shutil, "which", lambda *_args: None)
    monkeypatch.setattr(watch.Path, "exists", lambda *_args: False)
    assert watch.resolve_node({}) == "node"


def test_missing_node_is_explicit_configuration_failure(monkeypatch):
    def missing(*_args, **_kwargs):
        raise FileNotFoundError("synthetic missing executable")
    monkeypatch.setattr(watch.subprocess, "run", missing)
    monkeypatch.setattr(watch, "_append", lambda *_args: None)
    monkeypatch.setattr(watch, "rebuild_dashboard", lambda: None)
    monkeypatch.setattr(watch, "heal_channel", lambda *_args: pytest.fail("dependency treated as browser problem"))
    monkeypatch.setattr(watch.notify, "send_text", lambda *_args, **_kwargs: pytest.fail("unexpected real alert path"))
    monkeypatch.setattr(sys, "argv", ["watch", "--sec-uid", "synthetic", "--once",
                                      "--node", "synthetic-node", "--cdp", "synthetic-cdp"])
    assert watch.main() == 2


@pytest.mark.parametrize("owns", [False, True])
def test_transcribe_skip_requires_pid_identity(tmp_path, monkeypatch, owns):
    monkeypatch.setattr(recorder, "DATA", tmp_path)
    monkeypatch.setattr(recorder, "ROOT", tmp_path)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "transcribe_backfill.py").write_text("# synthetic only", encoding="utf-8")
    (tmp_path / ".tr_synthetic_2000-01-01.lock").write_text("123", encoding="utf-8")
    monkeypatch.setattr(recorder, "pid_alive", lambda *_args: True)
    checked, spawned = [], []
    def ownership(pid, path, **kwargs):
        checked.append(kwargs)
        return owns
    monkeypatch.setattr(recorder, "pid_owns_file", ownership)
    monkeypatch.setattr(recorder.subprocess, "Popen",
                        lambda *_args, **_kwargs: spawned.append(True) or SimpleNamespace(pid=456))
    recorder.spawn_transcribe("synthetic", "2000-01-01", lambda *_args: None)
    assert checked and checked[0]["require_identity"] is True
    assert len(spawned) == (0 if owns else 1)
