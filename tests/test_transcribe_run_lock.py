"""The transcriber fails closed and holds its lock through model/task work."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import transcribe_backfill as tb


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_transcriber(tmp_path, monkeypatch):
    tb._release_run_lock()
    monkeypatch.setattr(tb, "DATA", tmp_path)
    yield
    tb._release_run_lock()


def test_other_process_cannot_enter_until_released(tmp_path):
    script = """
import pathlib,sys
from scripts import transcribe_backfill as tb
tb.DATA = pathlib.Path(sys.argv[1])
try:
    sys.exit(0 if tb._acquire_run_lock() else 9)
finally:
    tb._release_run_lock()
"""
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
    def contender():
        return subprocess.run([sys.executable, "-c", script, str(tmp_path)],
                              cwd=str(tmp_path), env=env, capture_output=True, timeout=10)
    assert tb._acquire_run_lock()
    assert contender().returncode == 9
    tb._release_run_lock()
    assert contender().returncode == 0
    assert (tmp_path / ".transcribe.run.lock").exists()


def test_main_lock_io_failure_never_loads_model(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["transcribe_backfill", "--all"])
    monkeypatch.setattr(tb, "list_audio_dirs", lambda *_args: [tmp_path / "audio_X_2000-01-01"])
    class BrokenLock:
        def __init__(self, *_args, **_kwargs):
            pass
        def acquire(self, **_kwargs):
            raise PermissionError("synthetic lock failure")
    monkeypatch.setattr(tb, "FileLock", BrokenLock)
    monkeypatch.setattr(tb, "load_model", lambda *_args: pytest.fail("model loaded without lock"))
    assert tb.main() == 2


def test_model_failure_releases_run_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["transcribe_backfill", "--all"])
    monkeypatch.setattr(tb, "list_audio_dirs", lambda *_args: [tmp_path / "audio_X_2000-01-01"])
    monkeypatch.setattr(tb, "find_ffmpeg", lambda: None)
    from scripts import audio_segments as audio
    folder = tmp_path / "audio_X_2000-01-01"
    folder.mkdir()
    number, path = audio.reserve_segment(tmp_path, folder)
    with path.open("xb") as stream:
        stream.write(b"generated" * 1024)
        stream.flush()
        os.fsync(stream.fileno())
    audio.publish_closed(tmp_path, folder, path, number, 100, 101)
    def fail(*_args):
        assert tb._run_lock is not None
        raise RuntimeError("synthetic model failure")
    monkeypatch.setattr(tb, "load_model", fail)
    assert tb.main() == 1
    assert tb._run_lock is None
    assert tb._acquire_run_lock()


@pytest.mark.parametrize("flag", ["--list", "--verify"])
def test_read_only_modes_do_not_take_run_lock(tmp_path, monkeypatch, flag):
    (tmp_path / "audio_X_2000-01-01").mkdir()
    monkeypatch.setattr(sys, "argv", ["transcribe_backfill", flag])
    monkeypatch.setattr(tb, "list_audio_dirs", lambda *_args: [tmp_path / "audio_X_2000-01-01"])
    monkeypatch.setattr(tb, "verify_speech", lambda *_args: 0)
    monkeypatch.setattr(tb, "_acquire_run_lock", lambda: pytest.fail("read-only mode took lock"))
    assert tb.main() == 0


def test_import_has_no_log_directory_or_stdio_side_effects(tmp_path):
    script = """
import builtins,sys
original_open = builtins.open
def guarded_open(path, mode='r', *args, **kwargs):
    if any(flag in mode for flag in ('w', 'a', '+')):
        raise AssertionError('import attempted file write: ' + str(path))
    return original_open(path, mode, *args, **kwargs)
builtins.open = guarded_open
out, err = sys.stdout, sys.stderr
from scripts import transcribe_backfill
assert sys.stdout is out and sys.stderr is err
"""
    result = subprocess.run([sys.executable, "-c", script], cwd=str(tmp_path),
                            env=dict(os.environ, PYTHONPATH=str(ROOT)),
                            capture_output=True, timeout=10)
    assert result.returncode == 0, (result.stdout, result.stderr)
