# -*- coding: utf-8 -*-
"""transcribe_backfill 单测：目录名解析 + speech jsonl 体检（不碰模型、不碰真实 data/）。"""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import transcribe_backfill as tb  # noqa: E402


def test_dir_tag_date_roundtrip():
    d = pathlib.Path("D:/x/data/audio_研途-张老师团队_2026-10-19")
    assert tb.dir_tag_date(d) == ("研途-张老师团队", "2026-10-19")


def test_list_audio_dirs_filters(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "DATA", tmp_path)
    (tmp_path / "audio_A_2026-10-19").mkdir()
    (tmp_path / "audio_B_2026-10-19").mkdir()
    (tmp_path / "audio_C_2026-10-18").mkdir()
    (tmp_path / "speech_X_2026-10-19.jsonl").write_text("", encoding="utf-8")
    got = [p.name for p in tb.list_audio_dirs()]
    assert got == ["audio_A_2026-10-19", "audio_B_2026-10-19", "audio_C_2026-10-18"]
    assert [p.name for p in tb.list_audio_dirs(tag="A")] == ["audio_A_2026-10-19"]
    assert [p.name for p in tb.list_audio_dirs(date="2026-10-18")] == ["audio_C_2026-10-18"]


def test_verify_does_not_promote_legacy_speech_format_to_success(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "DATA", tmp_path)
    (tmp_path / "audio_A_2026-10-19").mkdir()
    rows = [{"ts": 100.0, "time": "t1", "text": "第一句", "dur": 2.0, "tag": "A", "seg": "seg_0001"},
            {"ts": 102.0, "time": "t2", "text": "第二句", "dur": 1.0, "tag": "A", "seg": "seg_0001"}]
    f = tmp_path / "speech_A_2026-10-19.jsonl"
    f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    assert tb.verify_speech("A", "2026-10-19") == 1
    assert f.read_text(encoding="utf-8").count("\n") == 2


def test_verify_speech_detects_missing_field(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "DATA", tmp_path)
    f = tmp_path / "speech_A_2026-10-19.jsonl"
    f.write_text('{"ts": 1, "text": "缺字段"}\n', encoding="utf-8")
    assert tb.verify_speech("A", "2026-10-19") == 1


def test_read_only_empty_day_does_not_hide_unknown_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "DATA", tmp_path)
    folder = tmp_path / "audio_A_2026-10-19"
    folder.mkdir()
    progress = folder / ".transcribed.json"
    progress.write_bytes(b'{"legacy-truncated":')
    monkeypatch.setattr(tb, "_acquire_run_lock", lambda: pytest.fail("read-only took writer"))
    monkeypatch.setattr(sys, "argv", ["transcribe_backfill", "--list"])
    assert tb.main() == 1
    assert tb.verify_speech("A", "2026-10-19") == 1
    assert progress.read_bytes() == b'{"legacy-truncated":'
