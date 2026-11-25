"""Shared JSONL appends preserve complete Unicode records across processes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

import llm_service
from llm_service import DailyJsonlWriter


ROOT = Path(__file__).resolve().parents[1]


def test_multiple_processes_append_exact_complete_records(tmp_path):
    script = """
import sys
from llm_service import DailyJsonlWriter
writers = [DailyJsonlWriter(sys.argv[1], 'cold_events', date='2000-01-01') for _ in range(2)]
try:
    for i in range(50):
        writers[i % 2].write({'id': sys.argv[2] + ':' + str(i), 'text': '\u4e2d\u6587' * 12000})
finally:
    for writer in writers:
        writer.close()
"""
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    processes = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path), str(i)],
                                 cwd=str(tmp_path), env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for i in range(4)]
    try:
        for proc in processes:
            out, err = proc.communicate(timeout=30)
            assert proc.returncode == 0, (out, err)
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=5)
    rows = [json.loads(line) for line in
            (tmp_path / "cold_events_2000-01-01.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 200
    assert {row["id"] for row in rows} == {"%d:%d" % (p, i) for p in range(4) for i in range(50)}
    assert all(row["text"] == "中文" * 12000 for row in rows)


def test_separate_writer_instances_serialize_threads(tmp_path):
    writers = [DailyJsonlWriter(str(tmp_path), "llm_metrics", date="2000-01-01")
               for _ in range(4)]
    failures = []
    def write(index):
        try:
            for i in range(30):
                writers[index].write({"id": [index, i], "text": "测试" * 5000})
        except BaseException as exc:
            failures.append(exc)
    threads = [threading.Thread(target=write, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    for writer in writers:
        writer.close()
    assert not failures
    rows = [json.loads(line) for line in
            (tmp_path / "llm_metrics_2000-01-01.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 120
    assert len({tuple(row["id"]) for row in rows}) == 120


def test_failed_lock_never_appends_and_writer_can_retry(tmp_path, monkeypatch):
    writer = DailyJsonlWriter(str(tmp_path), "coldline", date="2000-01-01")
    original = llm_service.file_lock
    def fail(*_args, **_kwargs):
        raise PermissionError("synthetic lock failure")
    monkeypatch.setattr(llm_service, "file_lock", fail)
    with pytest.raises(PermissionError):
        writer.write({"id": "rejected"})
    path = tmp_path / "coldline_2000-01-01.jsonl"
    assert path.read_text(encoding="utf-8") == ""
    monkeypatch.setattr(llm_service, "file_lock", original)
    writer.write({"id": "accepted"})
    writer.close()
    assert [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] == [{"id": "accepted"}]


def test_rotation_lock_failure_preserves_old_handle_for_retry(tmp_path, monkeypatch):
    writer = DailyJsonlWriter(str(tmp_path), "coldline", date="2000-01-01")
    writer.write({"id": "old"})
    old_handle = writer._file
    writer._date_override = "2000-01-02"
    original = llm_service.file_lock
    def fail(*_args, **_kwargs):
        raise PermissionError("synthetic old-file lock failure")
    monkeypatch.setattr(llm_service, "file_lock", fail)
    with pytest.raises(PermissionError):
        writer.write({"id": "rejected"})
    assert writer._file is old_handle and not old_handle.closed
    assert writer._current_date == "2000-01-01"
    monkeypatch.setattr(llm_service, "file_lock", original)
    writer.write({"id": "new"})
    writer.close()
    assert json.loads((tmp_path / "coldline_2000-01-01.jsonl").read_text(encoding="utf-8")) == {"id": "old"}
    assert json.loads((tmp_path / "coldline_2000-01-02.jsonl").read_text(encoding="utf-8")) == {"id": "new"}
