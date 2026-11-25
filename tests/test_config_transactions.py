"""Concurrent configuration edits preserve fields and synchronize synthetic copies."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import pytest

from scripts import board_llm
from scripts import watch_anchor_live
from file_lock import file_lock


ROOT = Path(__file__).resolve().parents[1]


def test_concurrent_keys_budget_and_enabled_preserve_all_edits(tmp_path):
    config = tmp_path / "config.json"
    data = tmp_path / "copies"
    data.mkdir()
    base = {"llm": {"enabled": False, "daily_token_budget": 1,
                    "coldline_enabled": True, "providers": {}},
            "untouched": {"source": "synthetic"}}
    config.write_text(json.dumps(base), encoding="utf-8")
    for index in range(2):
        copy = dict(base, room={"id": index})
        (data / ("watch_%d.config.json" % index)).write_text(json.dumps(copy), encoding="utf-8")
    script = """
import pathlib,sys,time
from scripts import board_llm as board
config, data, index = sys.argv[1], sys.argv[2], int(sys.argv[3])
original = board._load_json
def slow_read(path):
    result = original(path)
    if str(path) == config:
        time.sleep(0.004)
    return result
board._load_json = slow_read
for i in range(3):
    if index < 2:
        board.set_key(config, data, 'synthetic_%d_%d' % (index, i), 'synthetic-value')
    elif index == 2:
        board.sync_llm_config(config, data, enabled=True)
    else:
        board.sync_llm_config(config, data, budget=1000)
"""
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
    children = [subprocess.Popen([sys.executable, "-c", script, str(config), str(data), str(i)],
                                cwd=str(tmp_path), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                for i in range(4)]
    try:
        for child in children:
            out, err = child.communicate(timeout=30)
            assert child.returncode == 0, err.decode("utf-8", errors="replace")
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
    saved = json.loads(config.read_text(encoding="utf-8"))
    assert saved["llm"]["enabled"] is True
    assert saved["llm"]["daily_token_budget"] == 1000
    assert saved["llm"]["coldline_enabled"] is True
    assert set(saved["llm"]["providers"]) == {"synthetic_%d_%d" % (p, i)
                                               for p in range(2) for i in range(3)}
    assert saved["untouched"] == {"source": "synthetic"}
    for index in range(2):
        sub = json.loads((data / ("watch_%d.config.json" % index)).read_text(encoding="utf-8"))
        assert sub["llm"] == saved["llm"]
        assert sub["room"] == {"id": index}
    assert not list(tmp_path.rglob("*.tmp"))


def test_set_key_preserves_malformed_original_and_does_not_publish_copies(tmp_path):
    config = tmp_path / "config.json"
    config.write_bytes(b'{"llm":')
    data = tmp_path / "copies"
    data.mkdir()
    copy = data / "watch_0.config.json"
    copy.write_text('{"unchanged": true}', encoding="utf-8")
    with pytest.raises(RuntimeError):
        board_llm.set_key(config, str(data), "synthetic", "synthetic-value")
    assert config.read_bytes() == b'{"llm":'
    assert copy.read_text(encoding="utf-8") == '{"unchanged": true}'


def test_config_deadline_does_not_write_and_next_edit_can_resume(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    before = '{"llm": {"enabled": false, "daily_token_budget": 1}, "untouched": true}'
    config.write_text(before, encoding="utf-8")
    ready, release = threading.Event(), threading.Event()
    def hold():
        with file_lock(config):
            ready.set()
            assert release.wait(timeout=5)
    holder = threading.Thread(target=hold)
    holder.start()
    assert ready.wait(timeout=2)
    monkeypatch.setattr(board_llm, "file_lock", lambda path: file_lock(path, timeout=0.03))
    try:
        with pytest.raises(TimeoutError):
            board_llm.sync_llm_config(config, str(tmp_path / "copies"), budget=999)
    finally:
        release.set()
        holder.join(timeout=2)
    assert not holder.is_alive()
    assert config.read_text(encoding="utf-8") == before
    board_llm.sync_llm_config(config, str(tmp_path / "copies"), budget=999)
    saved = json.loads(config.read_text(encoding="utf-8"))
    assert saved["llm"] == {"enabled": False, "daily_token_budget": 999}
    assert saved["untouched"] is True


def test_room_config_waits_for_main_edit_then_reads_fresh_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(watch_anchor_live, "ROOT", tmp_path)
    config = tmp_path / "config.json"
    config.write_text('{"llm": {"daily_token_budget": 1}, "web_rid": "old"}', encoding="utf-8")
    started = threading.Event()
    failures = []
    def generate():
        started.set()
        try:
            watch_anchor_live.make_config("synthetic-room", "synthetic", False, config)
        except BaseException as exc:
            failures.append(exc)
    with file_lock(config):
        worker = threading.Thread(target=generate)
        worker.start()
        assert started.wait(timeout=2)
        board_llm.sync_llm_config(config, str(tmp_path / "data"), budget=1234)
    worker.join(timeout=5)
    assert not worker.is_alive() and not failures
    derived = json.loads((tmp_path / "data" / "watch_synthetic.config.json").read_text(encoding="utf-8"))
    assert derived["llm"]["daily_token_budget"] == 1234
    assert derived["rooms"] == [{"room_id": "synthetic-room", "label": "synthetic",
                                  "llm": False, "feishu": False}]
    assert "web_rid" not in derived


def test_room_config_failed_replace_preserves_existing_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(watch_anchor_live, "ROOT", tmp_path)
    config = tmp_path / "config.json"
    config.write_text('{"llm": {"enabled": false}}', encoding="utf-8")
    derived = watch_anchor_live.make_config("old-room", "synthetic", False, config)
    before = derived.read_bytes()
    def fail(*_args):
        raise PermissionError("synthetic failed replace")
    monkeypatch.setattr(watch_anchor_live.os, "replace", fail)
    with pytest.raises(PermissionError):
        watch_anchor_live.make_config("new-room", "synthetic", False, config)
    assert derived.read_bytes() == before
    assert not list(derived.parent.glob("*.tmp"))


def _sync_tree(tmp_path):
    config = tmp_path / "config.json"
    config.write_text('{"llm": {"enabled": false}, "main": true}', encoding="utf-8")
    data = tmp_path / "copies"
    data.mkdir()
    copies = [data / ("watch_%s.config.json" % name) for name in ("a", "z")]
    for copy in copies:
        copy.write_text('{"llm": {"enabled": false}, "room": "synthetic"}', encoding="utf-8")
    return config, data, copies


def test_backup_failure_refuses_main_and_copy_writes_and_cleans_temp(tmp_path, monkeypatch):
    config, data, copies = _sync_tree(tmp_path)
    before = {path: path.read_bytes() for path in [config] + copies}
    def fail(*_args, **_kwargs):
        raise PermissionError("synthetic backup denied")
    monkeypatch.setattr(shutil, "copy2", fail)
    with pytest.raises(PermissionError, match="synthetic backup"):
        board_llm.sync_llm_config(config, str(data), enabled=True)
    assert all(path.read_bytes() == original for path, original in before.items())
    assert not list(tmp_path.rglob("*.tmp"))


@pytest.mark.parametrize("failure", ["malformed", "unreadable"])
def test_every_copy_is_prevalidated_before_main_or_good_copy_changes(tmp_path, monkeypatch, failure):
    config, data, copies = _sync_tree(tmp_path)
    bad = copies[-1]
    if failure == "malformed":
        bad.write_bytes(b'{"broken":')
    else:
        original_load = board_llm._load_json
        monkeypatch.setattr(board_llm, "_load_json",
                            lambda path: None if Path(path) == bad else original_load(path))
    before = {path: path.read_bytes() for path in [config] + copies}
    with pytest.raises(RuntimeError, match="watch"):
        board_llm.sync_llm_config(config, str(data), enabled=True)
    assert all(path.read_bytes() == original for path, original in before.items())
    assert not Path(str(config) + ".bak").exists()
    assert not list(tmp_path.rglob("*.tmp"))


def test_waiting_for_any_copy_lock_does_not_publish_main_early(tmp_path, monkeypatch):
    config, data, copies = _sync_tree(tmp_path)
    before = config.read_bytes()
    ready, release, attempted = threading.Event(), threading.Event(), threading.Event()
    failures = []
    def hold_copy():
        with file_lock(copies[-1]):
            ready.set()
            assert release.wait(timeout=5)
    holder = threading.Thread(target=hold_copy)
    holder.start()
    assert ready.wait(timeout=2)
    original_lock = board_llm.file_lock
    def observed_lock(path, **kwargs):
        if Path(path) == copies[-1]:
            attempted.set()
        return original_lock(path, **kwargs)
    monkeypatch.setattr(board_llm, "file_lock", observed_lock)
    def synchronize():
        try:
            board_llm.sync_llm_config(config, str(data), enabled=True)
        except BaseException as exc:
            failures.append(exc)
    worker = threading.Thread(target=synchronize)
    worker.start()
    try:
        assert attempted.wait(timeout=2)
        assert config.read_bytes() == before
        assert not Path(str(config) + ".bak").exists()
    finally:
        release.set()
        holder.join(timeout=2)
        worker.join(timeout=5)
    assert not holder.is_alive() and not worker.is_alive() and not failures
    assert json.loads(config.read_text(encoding="utf-8"))["llm"]["enabled"] is True


def test_publication_io_failure_is_explicit_and_retry_finishes_sync(tmp_path, monkeypatch):
    config, data, copies = _sync_tree(tmp_path)
    original_write = board_llm._atomic_write_json
    def fail_second_copy(path, obj, backup=False):
        if Path(path) == copies[-1]:
            raise PermissionError("synthetic copy publication failed")
        return original_write(path, obj, backup=backup)
    monkeypatch.setattr(board_llm, "_atomic_write_json", fail_second_copy)
    with pytest.raises(PermissionError, match="synthetic copy publication"):
        board_llm.sync_llm_config(config, str(data), enabled=True)
    assert json.loads(config.read_text(encoding="utf-8"))["llm"]["enabled"] is True
    assert json.loads(copies[0].read_text(encoding="utf-8"))["llm"]["enabled"] is True
    assert json.loads(copies[-1].read_text(encoding="utf-8"))["llm"]["enabled"] is False
    monkeypatch.setattr(board_llm, "_atomic_write_json", original_write)
    assert board_llm.sync_llm_config(config, str(data), enabled=True)["count"] == 2
    assert all(json.loads(path.read_text(encoding="utf-8"))["llm"]["enabled"]
               for path in copies)
