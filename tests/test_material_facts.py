"""9A builder on owned new records/fake ASR; no audio disposition or model."""
from datetime import datetime
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts import audio_cleanup as cleanup
from scripts import audio_segments as audio
from scripts import material_common as common
from scripts import material_facts as facts
from scripts import transcript_results as results
from scripts.local_data import export_records

OUT = Path(os.environ.get("DLI_TEST_ROOT", str(Path(__file__).resolve().parents[1] / ".test-state")))
STAMP = datetime(2026, 1, 1, 12, tzinfo=export_records.BEIJING).timestamp()
TEXT = "generated https://example.invalid/toy 13800138000 toy@example.invalid"
PAYLOAD = b"generated-9A-not-real-audio" * 256


@pytest.fixture
def case(tmp_path):
    assert tmp_path.resolve().is_relative_to(OUT.resolve())
    assert list(tmp_path.iterdir()) == []
    data, version = tmp_path / "new-data", tmp_path / "new-owned-version"
    data.mkdir()
    version.mkdir()
    (version / "owner-simulation.json").write_bytes(common.encode({"kind": "test-owned-layout", "new_directory": True}))
    return data, version


def closed(data, tag="A", *, start=STAMP, partition="2026-01-01"):
    folder = data / ("audio_%s_%s" % (tag, partition))
    folder.mkdir(exist_ok=True)
    number, source = audio.reserve_segment(data, folder)
    with source.open("xb") as stream:
        stream.write(PAYLOAD)
        stream.flush()
        os.fsync(stream.fileno())
    record = audio.publish_closed(data, folder, source, number, start, start + 60)
    return folder, source, record


def committed(data, tag="A", *, rows=None, projection=True, start=STAMP, partition="2026-01-01"):
    folder, source, record = closed(data, tag, start=start, partition=partition)
    rows = [{"start": 0, "end": 1, "text": TEXT}] if rows is None else rows
    assert results.process_segment(data, folder, source.name, lambda _path: (rows, 60), "fake-9A-model")["completed_now"]
    if projection:
        results.rebuild_projection(data, folder)
    return folder, source, record


def write(data, name, row, *, jsonl=True):
    (data / name).write_bytes(common.encode(row) if jsonl else json.dumps(row, ensure_ascii=False).encode("utf-8"))


def snapshot(data):
    return {path.relative_to(data).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in data.rglob("*") if path.is_file()}


def forbid(*_args, **_kwargs):
    pytest.fail("material building must not repair, recognize, load or clean")


def readonly(monkeypatch):
    monkeypatch.setattr(cleanup, "cleanup_segment", forbid)
    monkeypatch.setattr(cleanup, "_disposition_flag", forbid)
    monkeypatch.setattr(results, "process_segment", forbid)
    monkeypatch.setattr(results, "rebuild_projection", forbid)


def build(case):
    data, version = case
    return facts.build_materials(data, version, material_version="new-test-version", generated_at=STAMP + 100,
                                 expected_scope=audio.data_scope(data))


def rows(version, item):
    return [common.decode(line) for line in (version / item["path"]).read_bytes().splitlines()]


def test_two_views_keep_original_and_shared_redaction_distinct(case, monkeypatch):
    data, version = case
    folder, source, _ = committed(data)
    write(data, "watch_A.jsonl", {"ts": STAMP, "is_live": True, "web_rid": "123", "title": TEXT})
    write(data, "room_meta_douyin_A.json", {"fetched_at": STAMP, "title": TEXT}, jsonl=False)
    write(data, "stats_douyin_A_2026-01-01.jsonl", {"ts": STAMP, "type": "room_stats", "online": 2})
    write(data, "danmu_douyin_A_2026-01-01.jsonl", {"ts": STAMP, "type": "danmaku", "text": TEXT})
    write(data, "events_douyin_A_2026-01-01.jsonl", {"ts": STAMP, "type": "control", "content": TEXT})
    before = snapshot(data)
    readonly(monkeypatch)
    result = build(case)
    assert snapshot(data) == before
    group = result["groups"][0]
    assert group["source"]["time_basis"] == "view_dependent_unverified"
    assert group["window_is_session"] is False and group["source"]["tag_is_speaker"] is False
    canon = next(item for item in result["files"] if item["view"] == "canonical")
    shared = next(item for item in result["files"] if item["view"] == "shared")
    original = rows(version, canon)[0]
    assert original["text"] == TEXT and original["view"] == "canonical"
    assert original["source"]["json_pointer"] == "/rows/0" and original["source"]["original_line"] is None
    loaded, digest = results._load_result(data, folder, source.name)
    assert original["source"]["result_sha256"] == digest and original["source"]["generation"] == loaded["generation"]
    assert all(original[key] == loaded["rows"][0][key] for key in loaded["rows"][0])
    projected = rows(version, shared)
    assert {item["stream"] for item in projected[:-1]} == {"watch", "room_meta", "stats", "danmu", "events", "speech"}
    speech = next(item for item in projected if item["stream"] == "speech")
    assert speech["text"] != TEXT and all(token in speech["text"] for token in ("[链接]", "[电话]", "[邮箱]"))
    assert "source" not in speech and projected[-1]["window_is_session"] is False
    assert canon["source"]["time_basis"] == "writer_wall_clock_plus_offset"
    assert shared["source"]["time_basis"] == "stored_record_timestamp_unverified"
    assert shared["redaction"]["actual_replacements"] is shared["redaction"]["original_row"] is None
    assert result["catalog"]["path"] == "catalog.jsonl" and not result["catalog"]["path"].startswith("exports/")


def test_transcript_only_midnight_rows_use_actual_beijing_days_without_roster(case, monkeypatch):
    data, version = case
    start = datetime(2026, 1, 1, 23, 59, 59, tzinfo=export_records.BEIJING).timestamp()
    committed(data, "removed_room", start=start, rows=[{"start": 0, "end": 1, "text": "before"}, {"start": 2, "end": 3, "text": "after"}])
    readonly(monkeypatch)
    result = build(case)
    assert [item["date"] for item in result["groups"]] == ["2026-01-01", "2026-01-02"]
    canonical = [item for item in result["files"] if item["view"] == "canonical"]
    assert [rows(version, item)[0]["text"] for item in canonical] == ["before", "after"]
    assert [rows(version, item)[0]["source"]["json_pointer"] for item in canonical] == ["/rows/0", "/rows/1"]
    assert len([item for item in result["files"] if item["view"] == "shared"]) == 2
    assert not (data / "roster.json").exists()


@pytest.mark.parametrize("cleaned", [False, True])
def test_no_speech_and_authorized_missing_audio_remain_usable(case, tmp_path, monkeypatch, cleaned):
    data, version = case
    folder, source, closed_record = committed(data, rows=[] if not cleaned else None)
    if cleaned:
        result, digest, ready = results._current_text(data, folder, source.name)
        assert ready
        operation = cleanup.operation_path(data, folder, source.name)
        operation.parent.mkdir()
        value = cleanup._seal({"schema": 1, "kind": "audio_cleanup", "state": "pending", "scope": audio.data_scope(data),
            "relative": closed_record["relative"], "file": source.name, "source": closed_record,
            "generation": result["generation"], "result_sha256": digest})
        audio.atomic_json(operation, value)
        retained = tmp_path / "retained-generated-audio.flv"
        assert source.resolve().is_relative_to(tmp_path.resolve()) and retained.resolve().is_relative_to(tmp_path.resolve())
        source.rename(retained)  # No 9A source deletion or native cleanup.
    before = snapshot(data)
    readonly(monkeypatch)
    result = build(case)
    assert snapshot(data) == before
    group = result["groups"][0]
    canon = next(item for item in result["files"] if item["view"] == "canonical")
    fact = group["completeness"]["segments"][0]
    assert group["views"]["canonical"]["available"]
    if cleaned:
        assert fact["state"] == "cleaned_speech" and fact["audio_state"] == "cleaned" and fact["cleanup_state"] == "pending"
        assert retained.read_bytes() == PAYLOAD and not source.exists() and len(rows(version, canon)) == 1
        assert audio.read_json(operation)["state"] == "pending"
    else:
        assert fact["state"] == "current_no_speech" and canon["records"] == 0 and canon["chunks"] == []
        assert group["views"]["canonical"]["status"] == "no_speech" and source.read_bytes() == PAYLOAD


def test_projection_pending_incomplete_failed_and_bad_intent_are_not_current_exports(case, monkeypatch):
    data, _version = case
    committed(data, "projection", projection=False)
    closed(data, "incomplete")
    folder, source, _ = closed(data, "failed")
    def fail(_path):
        raise RuntimeError("generated ASR failure")
    results.process_segment(data, folder, source.name, fail, "fake")
    folder, source, _ = committed(data, "bad_intent")
    results.attempt_path(data, folder, source.name).write_bytes(b'{"generated-broken-intent":')
    readonly(monkeypatch)
    result = build(case)
    observed = {item["tag"]: item["completeness"]["segments"][0]["state"] for item in result["groups"]}
    assert observed == {"projection": "projection_pending", "incomplete": "incomplete", "failed": "failed", "bad_intent": "bad_intent"}
    assert not any(item["view"] == "canonical" for item in result["files"])
    assert all(item["completeness"]["state"] == "partial" for item in result["groups"])


def test_old_generation_rollback_is_not_laundered_to_current(case, monkeypatch):
    data, _version = case
    folder, source, _ = committed(data)
    result_path, attempt = results.result_path(data, folder, source.name), results.attempt_path(data, folder, source.name)
    old_result, old_intent = result_path.read_bytes(), attempt.read_bytes()
    results.process_segment(data, folder, source.name, lambda _path: ([{"start": 0, "end": 1, "text": "new generation"}], 60), "fake", force=True)
    results.rebuild_projection(data, folder)
    result_path.write_bytes(old_result)
    attempt.write_bytes(old_intent)
    readonly(monkeypatch)
    result = build(case)
    fact = result["groups"][0]["completeness"]["segments"][0]
    assert fact["state"] == "bad_intent" and not fact["current_result"]
    assert not result["groups"][0]["views"]["canonical"]["available"]


def test_legacy_speech_without_canonical_stays_unverified_shared_only(case, monkeypatch):
    data, version = case
    write(data, "speech_old_room_2026-01-01.jsonl", {"ts": STAMP, "text": TEXT, "tag": "old_room", "seg": "seg_0001", "off": 0, "dur": 1})
    readonly(monkeypatch)
    result = build(case)
    group = result["groups"][0]
    assert group["completeness"]["state"] == "unknown" and group["completeness"]["legacy_speech_unverified"]
    assert not group["views"]["canonical"]["available"] and group["views"]["shared"]["available"]
    assert len(result["files"]) == 1 and rows(version, result["files"][0])[0]["text"] != TEXT


@pytest.mark.parametrize("tag", ["房间🙂", "尾点."])
def test_legal_customer_tag_is_not_silently_lost_when_shared_rejects_it(case, monkeypatch, tag):
    data, version = case
    committed(data, tag)
    readonly(monkeypatch)
    result = build(case)
    group = result["groups"][0]
    assert group["tag"] == tag and group["views"]["canonical"]["available"]
    assert group["views"]["shared"]["status"] == "unsupported_tag" and not group["views"]["shared"]["available"]
    assert result["files"][0]["path"] == "canonical/" + common.group_id("douyin", tag, "2026-01-01") + ".jsonl"
    assert rows(version, result["files"][0])[0]["tag"] == tag


def test_empty_and_unplaced_are_observations_not_offline(case, monkeypatch):
    data, _version = case
    folder = data / "audio_unknown_2026-01-01"
    folder.mkdir()
    (folder / "seg_0001.flv").write_bytes(PAYLOAD)
    readonly(monkeypatch)
    result = build(case)
    assert result["groups"] == result["files"] == [] and result["catalog"] is None
    assert result["discovery"]["unplaced_segments"] and result["discovery"]["no_records_does_not_mean_offline"]


@pytest.mark.parametrize("bad", ["records", "line", "bad-json", "output-capacity"])
def test_builder_limits_or_bad_source_fail_explicitly_without_source_mutation(case, monkeypatch, bad):
    data, _version = case
    if bad == "output-capacity":
        committed(data)
        monkeypatch.setattr(common, "MAX_FILE_BYTES", 64)
    elif bad == "records":
        (data / "watch_A.jsonl").write_bytes(common.encode({"ts": STAMP}) * 2)
        monkeypatch.setattr(facts, "MAX_INPUT_RECORDS", 1)
    elif bad == "line":
        write(data, "watch_A.jsonl", {"ts": STAMP, "title": "x" * common.MAX_LINE_BYTES})
    else:
        (data / "watch_A.jsonl").write_bytes(b'{"generated-bad-json":\n')
    before = snapshot(data)
    readonly(monkeypatch)
    with pytest.raises(common.MaterialError):
        build(case)
    assert snapshot(data) == before


def test_separate_result_read_must_match_validated_generation_and_raw_sha(case, monkeypatch):
    data, _version = case
    committed(data)
    original, calls = results._load_result, []
    def inconsistent(*args):
        value, digest = original(*args)
        calls.append(1)
        return (value, "f" * 64) if len(calls) == 2 else (value, digest)
    readonly(monkeypatch)
    monkeypatch.setattr(results, "_load_result", inconsistent)
    with pytest.raises(common.MaterialError, match="source_changed_during_build"):
        build(case)
    assert len(calls) == 2


def test_legacy_modules_keep_exact_accepted_bytes():
    root = Path(__file__).resolve().parents[1]
    expected = {"scripts/local_data/export_records.py": "7ba722838420e9a8ec728bcb3212b0cf2a2ebfe63dc3caae1179d54302b638bd",
                "scripts/local_data/build_index.py": "2541143c6f5f6df355d09fd1bf0e82c16d0caca379ebd6879ccbcf2e24f22fbe"}
    assert all(hashlib.sha256((root / name).read_bytes()).hexdigest() == digest for name, digest in expected.items())


@pytest.mark.parametrize("location", ["record", "generated_at"])
def test_extreme_integer_timestamp_has_safe_material_error(case, monkeypatch, location):
    data, version = case
    readonly(monkeypatch)
    if location == "record":
        write(data, "watch_A.jsonl", {"ts": 10 ** 400})
        with pytest.raises(common.MaterialError, match="source_time_unconfirmed"):
            build(case)
    else:
        with pytest.raises(common.MaterialError, match="material_build_invalid"):
            facts.build_materials(data, version, material_version="new-test-version", generated_at=10 ** 400,
                                  expected_scope=audio.data_scope(data))


def test_output_fault_closes_all_streams_and_retains_first_error(case, monkeypatch):
    _data, version = case
    (version / "canonical").mkdir()
    closed_streams = []
    class GeneratedStream:
        def __init__(self, name):
            self.name = name
        def flush(self):
            if self.name == "first":
                raise OSError("generated-first-flush")
        def fileno(self):
            return 12345
        def close(self):
            closed_streams.append(self.name)
            if self.name == "first":
                raise OSError("generated-first-close")
    monkeypatch.setattr(facts.os, "fsync", lambda _fd: None)
    output = facts._Canonical(version)
    output.handles.update(first=GeneratedStream("first"), second=GeneratedStream("second"))
    with pytest.raises(OSError, match="generated-first-flush"):
        output.close()
    assert closed_streams == ["first", "second"] and not output.handles
    output.close()
    # The bounded LRU eviction follows the same flush/close path.
    output.handles.update((str(index), GeneratedStream("first" if index == 0 else str(index))) for index in range(16))
    with pytest.raises(OSError, match="generated-first-flush"):
        output.emit({"id": "generated-safe-group"}, {"text": "generated row"})
    assert closed_streams[-1] == "first" and len(output.handles) == 15
    output.close()


@pytest.mark.parametrize("platform", ["douyin", "bilibili"])
def test_bounded_shared_wrapper_matches_original_publish_bytes(case, tmp_path, monkeypatch, platform):
    data, version = case
    if platform == "douyin":
        committed(data)
        write(data, "watch_A.jsonl", {"ts": STAMP, "is_live": True, "web_rid": "123", "title": TEXT})
    write(data, "room_meta_%s_A.json" % platform, {"fetched_at": STAMP, "title": TEXT}, jsonl=False)
    write(data, "stats_%s_A_2026-01-01.jsonl" % platform, {"ts": STAMP, "type": "room_stats", "online": 2, "ranks": [1]})
    write(data, "danmu_%s_A_2026-01-01.jsonl" % platform, {"ts": STAMP, "type": "gift", "text": TEXT})
    write(data, "events_%s_A_2026-01-01.jsonl" % platform, {"ts": STAMP, "type": "raw_event", "method": "ToyMethod",
        "content": TEXT, "sides": [{"score": 2}], "products": [{"promotion_id": "12", "index": 0}], "ranks": [1, 2]})
    # A producer's adjacent file may contain this day's timestamp.
    write(data, "danmu_%s_A_2025-12-31.jsonl" % platform, {"ts": STAMP + 1, "type": "danmaku", "text": "spillover"})
    reference = tmp_path / "new-reference-only" / "数据"
    reference.mkdir(parents=True)
    original_publish = export_records.publish
    expected = original_publish(data, reference, "A", datetime.fromtimestamp(STAMP, export_records.BEIJING).date(), platform)
    expected_bytes = (reference / "2026-01-01" / expected["file"]).read_bytes()
    before = snapshot(data)
    readonly(monkeypatch)
    monkeypatch.setattr(export_records, "publish", forbid)
    result = build(case)
    shared = next(item for item in result["files"] if item["view"] == "shared")
    assert (version / shared["path"]).read_bytes() == expected_bytes
    assert snapshot(data) == before and not list(version.rglob(".nas-export-*"))


def test_shared_reread_ignores_appended_oversize_tail_and_marks_partial(case, monkeypatch):
    data, version = case
    source = data / "watch_A.jsonl"
    initial = common.encode({"ts": STAMP, "title": "initial generated row"})
    source.write_bytes(initial)
    monkeypatch.setattr(facts, "MAX_INPUT_BYTES", 512)
    real_shared, real_open, read_lengths = facts._shared_projection, Path.open, []
    class Counted:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def fileno(self):
            return self.stream.fileno()
        def readline(self, amount):
            raw = self.stream.readline(amount)
            read_lengths.append(len(raw))
            return raw
    def tracked_open(path, mode="r", *args, **kwargs):
        stream = real_open(path, mode, *args, **kwargs)
        return Counted(stream) if path == source and mode == "rb" else stream
    def appended(*args):
        with real_open(source, "ab") as stream:
            stream.write(b"generated-uncommitted-tail" * common.MAX_LINE_BYTES)
        return real_shared(*args)
    readonly(monkeypatch)
    monkeypatch.setattr(Path, "open", tracked_open)
    monkeypatch.setattr(facts, "_shared_projection", appended)
    result = build(case)
    shared = next(item for item in result["files"] if item["view"] == "shared")
    assert rows(version, shared)[0]["title"] == "initial generated row"
    assert sum(read_lengths) == 2 * len(initial) and source.stat().st_size > common.MAX_LINE_BYTES
    assert result["discovery"]["append_observed"] and result["groups"][0]["completeness"]["state"] == "partial"
    assert result["discovery"]["budgeted_input_bytes"] == 2 * len(initial)


def test_shared_repeated_reads_consume_input_budget(case, monkeypatch):
    data, _version = case
    raw = b"".join(common.encode({"ts": STAMP + offset * 86400, "title": "generated"}) for offset in range(3))
    (data / "watch_A.jsonl").write_bytes(raw)
    monkeypatch.setattr(facts, "MAX_INPUT_BYTES", len(raw) * 2)
    readonly(monkeypatch)
    with pytest.raises(common.MaterialError, match="discovery_capacity"):
        build(case)
    assert (data / "watch_A.jsonl").read_bytes() == raw


def test_created_output_directories_are_pinned_but_allow_child_writes(case, monkeypatch):
    data, version = case
    committed(data)
    original, checked = common.guard_directory, set()
    wanted = {"canonical", "exports", "exports/数据", "exports/数据/2026-01-01"}
    @contextmanager
    def watched(path):
        with original(path) as guard:
            if Path(path).is_relative_to(version):
                relative = Path(path).relative_to(version).as_posix()
                if relative in wanted and relative not in checked:
                    retained = Path(path).with_name(Path(path).name + ".rename-retained")
                    with pytest.raises(OSError):
                        Path(path).rename(retained)
                    assert Path(path).is_dir() and not retained.exists()
                    (Path(path) / ".generated-guard-probe.txt").write_bytes(b"new child allowed")
                    checked.add(relative)
            yield guard
    readonly(monkeypatch)
    monkeypatch.setattr(common, "guard_directory", watched)
    result = build(case)
    assert checked == wanted and result["catalog"] is not None


@pytest.mark.parametrize("replacement", ["other_file", "hardlink"])
def test_canonical_reopen_rejects_replaced_or_linked_leaf_without_writing(case, tmp_path, monkeypatch, replacement):
    _data, version = case
    (version / "canonical").mkdir()
    output = facts._Canonical(version)
    relative = output.ensure({"id": "new-group"})
    path = version / relative
    retained = path.with_name("retained-generated-original.jsonl")
    path.rename(retained)
    other = tmp_path / "new-other-leaf.txt"
    other.write_bytes(b"generated-other-no-write")
    if replacement == "hardlink":
        os.link(other, path)
    else:
        path.write_bytes(b"generated-replacement-no-write")
    before = path.read_bytes()
    original_open, flags_seen = facts.os.open, []
    def no_create(name, flags, *args, **kwargs):
        if Path(name) == path:
            flags_seen.append(flags)
            assert not flags & os.O_CREAT
        return original_open(name, flags, *args, **kwargs)
    monkeypatch.setattr(facts.os, "open", no_create)
    with pytest.raises(common.MaterialError):
        output.emit({"id": "new-group"}, {"text": "must not be written"})
    output.close()
    assert flags_seen and path.read_bytes() == before and retained.read_bytes() == b""
    assert other.read_bytes() == b"generated-other-no-write"


def test_shared_new_writer_rejects_multi_link_before_first_write(case, monkeypatch):
    data, version = case
    write(data, "watch_A.jsonl", {"ts": STAMP, "title": "generated"})
    original, linked = Path.open, []
    target = version / "exports/数据/2026-01-01/douyin_A.jsonl"
    def linked_open(path, mode="r", *args, **kwargs):
        stream = original(path, mode, *args, **kwargs)
        if path == target and mode == "xb":
            retained = path.with_name("generated-linked-output.txt")
            os.link(path, retained)
            linked.append(retained)
        return stream
    readonly(monkeypatch)
    monkeypatch.setattr(Path, "open", linked_open)
    with pytest.raises(common.MaterialError):
        build(case)
    assert linked and target.read_bytes() == linked[0].read_bytes() == b""
    # Writer was closed on failure; this rename affects only new derived output.
    target.rename(target.with_name("retained-generated-rejected.jsonl"))


def test_shared_projection_failure_closes_new_partial_output(case, monkeypatch):
    data, version = case
    raw = common.encode({"ts": STAMP, "title": "one"}) + common.encode({"ts": STAMP + 1, "title": "two"})
    (data / "watch_A.jsonl").write_bytes(raw)
    original, calls = export_records.project, []
    def fail_second(*args):
        calls.append(1)
        if len(calls) == 2:
            raise ValueError("generated-projection-fault")
        return original(*args)
    readonly(monkeypatch)
    monkeypatch.setattr(export_records, "project", fail_second)
    with pytest.raises(common.MaterialError, match="shared_projection_failed"):
        build(case)
    target = version / "exports/数据/2026-01-01/douyin_A.jsonl"
    retained = target.with_name("retained-generated-partial.jsonl")
    target.rename(retained)
    assert len(retained.read_bytes().splitlines()) == 1 and (data / "watch_A.jsonl").read_bytes() == raw


@pytest.mark.parametrize("replacement", ["missing", "other_file", "same_inode_append"])
def test_catalog_sync_reopen_does_not_create_or_accept_replaced_leaf(case, monkeypatch, replacement):
    data, version = case
    raw = common.encode({"ts": STAMP, "title": "generated catalog input"})
    (data / "watch_A.jsonl").write_bytes(raw)
    target, retained = version / "catalog.jsonl", version / "retained-generated-catalog.jsonl"
    real_open, real_path_open, real_describe, observed = facts.os.open, Path.open, common.describe_file, []
    def substitute():
        target.rename(retained)
        assert retained.read_bytes()
        if replacement == "other_file":
            target.write_bytes(b"generated-new-leaf-not-catalog")
    def guarded_open(path, flags, *args, **kwargs):
        if Path(path) == target and flags & os.O_WRONLY:
            assert not flags & os.O_CREAT
            if replacement != "same_inode_append":
                substitute()
            observed.append(flags)
        return real_open(path, flags, *args, **kwargs)
    def legacy_open(path, mode="r", *args, **kwargs):
        if path == target and mode == "ab":
            # This branch makes the former append-open implementation fail the
            # missing-catalog assertion by recreating a zero-byte leaf.
            substitute()
        return real_path_open(path, mode, *args, **kwargs)
    def appended_describe(base, name):
        if name == "catalog.jsonl" and replacement == "same_inode_append":
            before = common.identity(target)
            with real_path_open(target, "ab") as stream:
                stream.write(common.encode({"generated_catalog_append": True}))
            after = common.identity(target)
            assert (before["dev"], before["ino"]) == (after["dev"], after["ino"]) and after["size"] > before["size"]
        return real_describe(base, name)
    readonly(monkeypatch)
    monkeypatch.setattr(facts.os, "open", guarded_open)
    monkeypatch.setattr(Path, "open", legacy_open)
    monkeypatch.setattr(common, "describe_file", appended_describe)
    with pytest.raises(common.MaterialError):
        build(case)
    assert observed and (data / "watch_A.jsonl").read_bytes() == raw
    if replacement == "missing":
        assert retained.read_bytes() and not target.exists()
    elif replacement == "other_file":
        assert retained.read_bytes()
        assert target.read_bytes() == b"generated-new-leaf-not-catalog"
        target.rename(target.with_name("retained-generated-replacement.jsonl"))
    else:
        assert not retained.exists() and common.decode(target.read_bytes().splitlines()[-1]) == {"generated_catalog_append": True}
