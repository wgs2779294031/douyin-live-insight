"""Read-only storage observations; generated files and fake capacity only."""
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
from scripts import audio_segments as audio
from scripts.audio_storage import StorageSummary, LOW_SPACE_BYTES


class Clock:
    def __init__(self):
        self.value = 0.0

    def now(self):
        return self.value

    def wall(self):
        return 1800000000.0 + self.value


@pytest.fixture
def generated(tmp_path):
    data = tmp_path / "generated-storage-data"
    data.mkdir()
    folder = data / "audio_generated_2000-01-01"
    folder.mkdir()
    source = folder / "seg_0001.flv"
    source.write_bytes(b"generated" * 512)
    return data, folder, source


def observer(data, clock, usage=None):
    return StorageSummary(data, scope=audio.data_scope(data), monotonic=clock.now, wall_time=clock.wall,
                          disk_usage=usage or (lambda _path: SimpleNamespace(free=2 * LOW_SPACE_BYTES, total=10 * LOW_SPACE_BYTES)))


def text_done(**updates):
    return dict({"current_result": True, "projection_ready": True, "cleanup_eligible": True,
                 "audio_cleaned": False, "history_usable": True, "cleanup_state": "none"}, **updates)


def test_snapshot_and_observation_never_open_or_hash_audio_or_query_disk(generated, monkeypatch):
    data, folder, source = generated
    clock, calls = Clock(), []
    summary = observer(data, clock, lambda path: calls.append(path) or pytest.fail("no disk call from snapshot"))
    initial = summary.snapshot()
    assert initial["audio_bytes"] is None and initial["snapshot_at"] is None and initial["scan"]["partial"]
    real_open = Path.open
    monkeypatch.setattr(Path, "open", lambda path, *a, **k: pytest.fail("source opened") if path == source else real_open(path, *a, **k))
    monkeypatch.setattr(audio, "fingerprint", lambda *_: pytest.fail("audio hashed"))
    summary.begin_scan()
    summary.observe(folder, source.name, {})
    summary.finish_scan()
    result = summary.snapshot()
    assert result["audio_bytes"] == source.stat().st_size and result["audio_files"] == 1
    assert result["transcription_pending"] == 1 and not result["scan"]["partial"]
    result["scan"]["checked"] = 999
    assert summary.snapshot()["scan"]["checked"] == 1 and calls == []


def test_round_aggregates_more_than_the_asr_recent_window(generated):
    data, folder, source = generated
    clock, summary = Clock(), observer(data, Clock())
    source.write_bytes(b"x")
    summary.begin_scan()
    for number in range(1, 258):
        path = folder / ("seg_%04d.flv" % number)
        if number != 1:
            path.write_bytes(b"x")
        summary.observe(folder, path.name, {})
    summary.finish_scan()
    value = summary.snapshot()
    assert value["audio_bytes"] == value["audio_files"] == value["transcription_pending"] == 257
    assert value["scan"]["checked"] == 257 and value["scan"]["counts_scope"] == "scan_round"
    assert not value["scan"]["partial"] and not value["scan"]["limited"] and value["scan"]["approximate"]


@pytest.mark.parametrize("facts,attempts,exhausted,field", [
    (text_done(), 0, False, "cleanup_pending"),
    (text_done(), 1, False, "cleanup_failed"),
    (text_done(), 3, True, "cleanup_failed"),
    (text_done(cleanup_eligible=False, cleanup_state="invalid"), 0, False, "cleanup_blocked"),
    (text_done(cleanup_schedule_reason="cleanup_capacity"), 0, False, "cleanup_blocked"),
    ({}, 0, False, "transcription_pending"),
], ids=["pending", "retry", "exhausted", "bad-operation", "budget-capacity", "unfinished"])
def test_separate_cleanup_counts_do_not_change_transcription_facts(generated, facts, attempts, exhausted, field):
    data, folder, source = generated
    summary = observer(data, Clock())
    summary.begin_scan()
    summary.observe(folder, source.name, facts, cleanup_attempts=attempts, cleanup_exhausted=exhausted)
    value = summary.snapshot()
    assert value[field] == 1
    assert sum(value[key] for key in ("cleanup_pending", "cleanup_failed", "cleanup_blocked", "transcription_pending")) == 1
    assert source.exists()


@pytest.mark.parametrize("state,expected", [("pending", 1), ("deleted", 0)])
def test_authorized_absent_source_counts_settlement_without_audio_bytes(generated, state, expected):
    data, folder, source = generated
    retained = source.with_suffix(".retained")
    source.rename(retained)
    summary = observer(data, Clock())
    summary.begin_scan()
    summary.observe(folder, source.name, text_done(audio_cleaned=True, cleanup_eligible=False, cleanup_state=state))
    summary.finish_scan()
    value = summary.snapshot()
    assert value["cleanup_pending"] == expected and value["audio_bytes"] == value["audio_files"] == 0
    assert retained.read_bytes() == b"generated" * 512


def test_partial_limited_and_growing_sources_are_not_a_live_global_total(generated):
    data, folder, source = generated
    clock = Clock()
    summary = observer(data, clock)
    summary.begin_scan()
    summary.observe(folder, source.name, {})
    summary.finish_scan()
    first = summary.snapshot()
    source.write_bytes(b"growth" * 2000)
    assert summary.snapshot()["audio_bytes"] == first["audio_bytes"]
    clock.value += 10
    summary.begin_scan()
    assert summary.snapshot()["scan"]["partial"] and summary.snapshot()["scan"]["last_complete_at"] == first["snapshot_at"]
    summary.observe(folder, source.name, {})
    summary.finish_scan(complete=True, limited=True)
    value = summary.snapshot()
    assert value["audio_bytes"] == 12000 and value["scan"]["partial"] and value["scan"]["limited"]
    assert value["scan"]["last_complete_at"] == first["snapshot_at"]


def test_linked_source_is_excluded_and_range_stays_partial(generated):
    data, folder, source = generated
    os.link(source, folder / "retained-new-hardlink")
    summary = observer(data, Clock())
    summary.begin_scan()
    summary.observe(folder, source.name, text_done())
    summary.finish_scan()
    value = summary.snapshot()
    assert value["audio_bytes"] == 0 and value["cleanup_blocked"] == 1
    assert value["scan"]["partial"] and value["scan"]["limited"] and value["scan"]["last_complete_at"] is None


def test_disk_sampling_is_throttled_and_does_not_renew_scan_snapshot(generated):
    data, folder, source = generated
    clock, calls = Clock(), []
    def usage(path):
        calls.append(path)
        return SimpleNamespace(free=0, total=10 * LOW_SPACE_BYTES)
    summary = observer(data, clock, usage)
    summary.begin_scan()
    summary.observe(folder, source.name, {})
    stamp = summary.snapshot()["snapshot_at"]
    clock.value += 20
    summary.poll_disk()
    disk = summary.snapshot()["disk"]
    assert disk["low_space"] is True and disk["sampled_at"] == clock.wall()
    assert summary.snapshot()["snapshot_at"] == stamp
    for _ in range(3):
        summary.snapshot()
        summary.poll_disk()
    assert len(calls) == 1
    clock.value += 30
    summary.poll_disk()
    assert len(calls) == 2


@pytest.mark.parametrize("free,total", [(True, 10), (-1, 10), (11, 10), (0, 0), (2**53, 2**53)])
def test_bad_capacity_is_unknown_instead_of_safe_space(generated, free, total):
    data = generated[0]
    summary = observer(data, Clock(), lambda _: SimpleNamespace(free=free, total=total))
    summary.poll_disk()
    value = summary.snapshot()["disk"]
    assert value["free_bytes"] is None and value["low_space"] is None and value["error"] == "disk_unconfirmed"


def test_low_space_boundary_and_error_are_explicit(generated):
    data = generated[0]
    summary = observer(data, Clock(), lambda _: SimpleNamespace(free=LOW_SPACE_BYTES, total=2 * LOW_SPACE_BYTES))
    summary.poll_disk()
    assert summary.snapshot()["disk"]["low_space"] is False
    def denied(_):
        raise OSError("generated private path is not public")
    failed = observer(data, Clock(), denied)
    failed.poll_disk()
    assert failed.snapshot()["disk"]["error"] == "disk_unconfirmed"


def test_replaced_scope_does_not_query_a_different_disk(generated):
    data, folder, source = generated
    summary = observer(data, Clock(), lambda _: pytest.fail("different namespace queried"))
    data.rename(data.with_name("retained-old-root"))
    data.mkdir()
    summary.poll_disk()
    assert summary.snapshot()["disk"]["free_bytes"] is None


def test_recent_result_is_safe_and_does_not_claim_freed_bytes(generated):
    data, folder, source = generated
    summary = observer(data, Clock())
    summary.record_cleanup(folder, source.name, {"state": [], "reason": {"private": "path"}, "deleted_now": True})
    result = summary.snapshot()["last_cleanup"]
    assert result["state"] == "pending" and result["reason"] == "cleanup_unconfirmed"
    assert "deleted_now" not in result and "bytes_freed" not in result
