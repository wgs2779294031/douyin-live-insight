"""Store composition; native GC only of this run's marked derived toy files."""
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

from scripts import audio_cleanup as cleanup, audio_segments as audio, transcript_results as results
from scripts import material_common as common
from scripts.local_materials import MaterialStore, main

OUT = Path(os.environ.get("DLI_TEST_ROOT", str(Path(__file__).resolve().parents[1] / ".test-state")))
ROOT = Path(__file__).resolve().parents[1]
DAY, TAG = "2026-11-01", "generated-room"
STAMP = datetime(2026, 11, 1, 12, tzinfo=timezone(timedelta(hours=8))).timestamp()


@pytest.fixture
def case(tmp_path, monkeypatch):
    assert tmp_path.resolve().is_relative_to(OUT.resolve())
    data = tmp_path / "generated-data"
    data.mkdir()
    (tmp_path / "owned-task-09A.txt").write_text("Only this task's generated derived material GC", encoding="utf-8")
    context = {"schema": 1, "root": str(ROOT), "instance_id": "generated-materials-instance", "data": str(data),
        "config": str(tmp_path / "config.json"), "roster": str(tmp_path / "roster.json"),
        "dist": str(tmp_path / "dist"), "python": sys.executable, "profile": str(tmp_path / "never-profile"),
        "port": 29001, "cdp_port": 29002}
    roots, operations = [], []
    native = cleanup._disposition_flag
    def scoped(guard, flag):
        assert any(guard.path.absolute().is_relative_to(root) for root in roots)
        assert not guard.path.absolute().is_relative_to(data)
        assert guard.path.suffix != ".flv"
        operations.append((str(guard.path), flag))
        return native(guard, flag)
    monkeypatch.setattr(cleanup, "_disposition_flag", scoped)
    monkeypatch.setattr(cleanup, "cleanup_segment", lambda *_a, **_k: pytest.fail("9A never cleans audio"))
    def store(**options):
        value = MaterialStore(context, **options)
        roots.append(value.root)
        return value
    return type("Case", (), dict(data=data, context=context, store=staticmethod(store), operations=operations, base=tmp_path))()


def empty_builder(*_args, **_kwargs):
    return {"groups": [], "files": [], "catalog": None, "discovery": {"files_checked": 0}}


def source_hashes(data):
    return {p.relative_to(data).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in data.rglob("*") if p.is_file()}


def transcript(case, *, no_speech=False):
    folder = case.data / f"audio_{TAG}_{DAY}"
    folder.mkdir(exist_ok=True)
    number, source = audio.reserve_segment(case.data, folder)
    with source.open("xb") as stream:
        stream.write(b"task09A-generated-not-real-media" * 256)
        stream.flush()
        os.fsync(stream.fileno())
    audio.publish_closed(case.data, folder, source, number, STAMP, STAMP + 60)
    text = "generated 13812345678 https://example.invalid/test"
    rows = [] if no_speech else [{"start": 0, "end": 1, "text": text}]
    assert results.process_segment(case.data, folder, source.name, lambda _: (rows, 60), "fake")['state'] == "completed"
    results.rebuild_projection(case.data, folder)
    return folder, source, text


def test_real_builder_store_query_keeps_views_sources_and_data_read_only(case, monkeypatch):
    folder, source, text = transcript(case)
    (case.data / f"danmu_douyin_{TAG}_{DAY}.jsonl").write_bytes(common.encode({"ts": STAMP, "type": "danmaku", "text": text}))
    before = source_hashes(case.data)
    store = case.store()
    result = store.refresh()
    assert result["state"] == "published", result
    canonical = store.query(room=TAG, date=DAY)
    assert canonical["items"][0]["text"] == text
    row = canonical["items"][0]
    assert row["source"]["result_ref"].endswith(".results/seg_0001.json")
    assert row["source"]["json_pointer"] == "/rows/0" and row["source"]["material"]["byte_offset"] == 0
    assert row["source"]["time_basis"] == "writer_wall_clock_plus_offset" and not row["tag_is_speaker"]
    shared = store.query(view="shared", room=TAG, date=DAY)
    assert {r["stream"] for r in shared["items"]} == {"danmu", "speech"}
    assert all("13812345678" not in r.get("text", "") and "https://" not in r.get("text", "") for r in shared["items"])
    assert all(r["source"]["time_basis"] == "stored_record_timestamp_unverified" for r in shared["items"])
    assert all("generation" not in r["source"] for r in shared["items"])
    assert not store.list_groups()["items"][0]["window_is_session"]
    monkeypatch.setattr(common, "write_json", lambda *_a, **_k: pytest.fail("query must not write"))
    monkeypatch.setattr(results, "validate_success", lambda *_a, **_k: pytest.fail("query must not revalidate ASR"))
    monkeypatch.setattr(audio, "fingerprint", lambda *_a, **_k: pytest.fail("query must not hash audio"))
    assert store.query()["items"]
    assert source_hashes(case.data) == before and source.is_file() and not case.operations


def test_empty_publish_repeated_refresh_has_finite_owned_versions(case):
    store = case.store()
    versions = []
    for _ in range(4):
        result = store.refresh(builder=empty_builder)
        assert result["state"] == "published", result
        versions.append(result["version"])
        assert store.query()["items"] == []
        assert len(list((store.root / "versions").iterdir())) <= 2
    assert set(p.name for p in (store.root / "versions").iterdir()) == set(versions[-2:])
    assert case.operations and not list(case.data.iterdir())


def test_failed_build_keeps_current_and_recovers_known_partial_derived_files(case):
    store = case.store()
    prior = store.refresh(builder=empty_builder)["version"]
    first = store.refresh(builder=empty_builder)["version"]
    def interrupted(_data, stage, **_kwargs):
        (stage / "canonical").mkdir()
        (stage / "canonical" / ("a" * 24 + ".jsonl")).write_bytes(b"partial-generated")
        raise RuntimeError("private exception must not escape")
    failed = store.refresh(builder=interrupted)
    assert failed["state"] == "failed" and failed["version"] == first
    assert (store.root / "versions" / prior / ".owner.json").is_file()
    assert len(list((store.root / "versions").iterdir())) == 3
    page = store.query()
    assert page["version"] == first and page["refresh"]["snapshot_stale"]
    assert "private exception" not in json.dumps(page)
    assert store.refresh(builder=empty_builder)["state"] == "published"
    assert len(list((store.root / "versions").iterdir())) == 2


def test_unknown_partial_artifact_blocks_more_versions_and_is_preserved(case):
    store = case.store()
    first = store.refresh(builder=empty_builder)["version"]
    def interrupted(_data, stage, **_kwargs):
        (stage / "unknown.txt").write_text("do not remove", encoding="utf-8")
        raise ValueError("generated")
    assert store.refresh(builder=interrupted)["state"] == "failed"
    before = list((store.root / "versions").iterdir())
    assert store.refresh(builder=empty_builder)["error"] == "material_gc_blocked"
    assert list((store.root / "versions").iterdir()) == before
    assert any((path / "unknown.txt").is_file() for path in before)
    assert store.query()["version"] == first


@pytest.mark.parametrize("after", [False, True])
def test_atomic_pointer_write_failure_distinguishes_precommit_and_committed(case, monkeypatch, after):
    store = case.store()
    first = store.refresh(builder=empty_builder)["version"]
    original = common.write_json
    def fail(path, value, *args, **kwargs):
        if Path(path) == store.root / "current.json":
            if after:
                original(path, value, *args, **kwargs)
            raise PermissionError("generated pointer fault")
        return original(path, value, *args, **kwargs)
    monkeypatch.setattr(common, "write_json", fail)
    result = store.refresh(builder=empty_builder)
    assert result["state"] == ("published" if after else "failed")
    assert (store.query()["version"] != first) is after


def test_failed_diagnostic_after_commit_never_rolls_back_current(case, monkeypatch):
    store = case.store()
    first = store.refresh(builder=empty_builder)["version"]
    original = store._diagnostic
    def fail(state, *args, **kwargs):
        if state == "success":
            raise PermissionError("generated diagnostic fault")
        return original(state, *args, **kwargs)
    monkeypatch.setattr(store, "_diagnostic", fail)
    result = store.refresh(builder=empty_builder)
    assert result["state"] == "published" and result["warning"]
    assert store.query()["version"] != first
    assert store.query()["refresh"]["state"] == "unconfirmed"


def test_system_exit_keeps_old_version_readable_without_claiming_success(case):
    store = case.store()
    first = store.refresh(builder=empty_builder)["version"]
    def stop(*_args, **_kwargs):
        raise SystemExit("generated interruption")
    with pytest.raises(SystemExit):
        store.refresh(builder=stop)
    page = store.query()
    assert page["version"] == first and page["refresh"]["snapshot_stale"]
    assert store.refresh(builder=empty_builder)["state"] == "published"


def test_unknown_existing_root_and_overlapping_paths_are_rejected(case):
    default = case.store().root
    default.mkdir()
    (default / "foreign.txt").write_text("preserve", encoding="utf-8")
    with pytest.raises(common.MaterialError):
        case.store().refresh(builder=empty_builder)
    assert (default / "foreign.txt").read_text(encoding="utf-8") == "preserve"
    for root in (case.data, case.data.parent, case.data / "nested"):
        with pytest.raises(common.MaterialError):
            case.store(derived_root=root)


def test_data_scope_replacement_is_rejected_and_original_retained(case):
    store = case.store()
    store.refresh(builder=empty_builder)
    case.data.rename(case.base / "retained-source-data")
    case.data.mkdir()
    with pytest.raises(common.MaterialError, match="scope_changed"):
        store.query()
    assert not list(case.data.iterdir())


def test_owner_mismatch_and_corrupt_diagnostic_do_not_leak_or_select_old(case):
    store = case.store()
    version = store.refresh(builder=empty_builder)["version"]
    diagnostic = common.seal({"schema": 1, "kind": "material_refresh", "data_scope": store.scope,
        "state": "failed", "version": version, "updated_at": STAMP, "last_success_at": None, "error": "SECRET:C:/private"})
    common.write_json(store.root / "refresh.json", diagnostic)
    page = store.query()
    assert "SECRET" not in json.dumps(page) and page["refresh"]["state"] == "unconfirmed"
    owner = common.read_json(store.root / "owner.json")
    owner["instance_id"] = "other-instance"
    common.write_json(store.root / "owner.json", common.seal({k: v for k, v in owner.items() if k != "checksum"}))
    with pytest.raises(common.MaterialError):
        store.query()


def test_version_switch_invalidates_existing_cursor_and_mid_page_snapshot(case):
    transcript(case)
    store = case.store()
    assert store.refresh()["state"] == "published"
    with store.snapshot() as snapshot:
        assert store.refresh()["state"] == "published"
        with pytest.raises(common.MaterialError, match="version_changed"):
            snapshot.finish()


def test_cli_loads_explicit_context_and_read_commands_do_not_refresh(case, capsys):
    store = case.store()
    store.refresh(builder=empty_builder)
    source = case.base / "context.json"
    source.write_bytes(common.encode(case.context))
    assert main(["--context", str(source), "query"]) == 0
    page = json.loads(capsys.readouterr().out)
    assert page["items"] == []
    assert main(["--context", str(source), "query", "--limit", "0"]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "invalid_limit"}
    assert not Path(case.context["profile"]).exists()


def test_real_derived_payload_gc_preserves_all_original_sources(case):
    transcript(case)
    before = source_hashes(case.data)
    store = case.store()
    versions = []
    for _ in range(3):
        outcome = store.refresh()
        assert outcome["state"] == "published", outcome
        versions.append(outcome["version"])
        assert store.query()["items"]
    assert not (store.root / "versions" / versions[0]).exists()
    assert len(list((store.root / "versions").iterdir())) == 2
    assert source_hashes(case.data) == before and case.operations


def test_pinned_old_reader_blocks_gc_without_losing_owner_and_later_recovers(case):
    store = case.store()
    first = store.refresh(builder=empty_builder)["version"]
    with store.snapshot() as old:
        second = store.refresh(builder=empty_builder)["version"]
        outcome = store.refresh(builder=empty_builder)
        assert outcome["state"] == "published" and outcome["warning"] == "material_gc_pending"
        assert (store.root / "versions" / first / ".owner.json").is_file()
        assert store.query()["version"] == outcome["version"] != second
        assert store.refresh(builder=empty_builder)["error"] == "material_gc_blocked"
        assert len(list((store.root / "versions").iterdir())) == 3
        with pytest.raises(common.MaterialError, match="version_changed"):
            old.finish()
    assert store.refresh(builder=empty_builder)["state"] == "published"
    assert not (store.root / "versions" / first).exists()


def test_linked_owner_is_rejected_without_any_gc(case):
    store = case.store()
    store.refresh(builder=empty_builder)
    os.link(store.root / "owner.json", case.base / "retained-owner-link.json")
    with pytest.raises(common.MaterialError):
        store.refresh(builder=empty_builder)
    assert not case.operations
    assert (store.root / "owner.json").is_file()


@pytest.mark.parametrize("directory", [False, True])
def test_sealed_gc_preserves_unmanifested_valid_shaped_artifact(case, directory):
    store = case.store()
    first = store.refresh(builder=empty_builder)["version"]
    retired = store.root / "versions" / first
    if directory:
        foreign = retired / "exports" / "数据" / "2099-01-01"
        foreign.mkdir(parents=True)
    else:
        foreign = retired / "canonical" / ("f" * 24 + ".jsonl")
        foreign.parent.mkdir()
        foreign.write_bytes(b"generated unknown must remain\n")
    store.refresh(builder=empty_builder)
    outcome = store.refresh(builder=empty_builder)
    assert outcome["state"] == "published" and outcome["warning"] == "material_gc_pending"
    assert foreign.exists() and (retired / ".owner.json").is_file()
    assert store.refresh(builder=empty_builder)["error"] == "material_gc_blocked"
    assert foreign.exists()


def test_gc_receipt_recovers_after_inner_owner_removed_before_directory_mark(case, monkeypatch):
    store = case.store()
    first = store.refresh(builder=empty_builder)["version"]
    retired = store.root / "versions" / first
    store.refresh(builder=empty_builder)
    native = cleanup._disposition_flag
    injected = []
    def fail(guard, flag):
        if guard.directory and guard.path == retired and flag and not injected:
            injected.append(True)
            assert not (retired / ".owner.json").exists()
            raise PermissionError("generated interruption after owner close")
        return native(guard, flag)
    monkeypatch.setattr(cleanup, "_disposition_flag", fail)
    result = store.refresh(builder=empty_builder)
    assert result["state"] == "published" and result["warning"] == "material_gc_pending"
    assert injected and retired.is_dir() and not (retired / ".owner.json").exists()
    receipt = store.root / "gc" / (first + ".json")
    assert receipt.is_file()
    assert store.refresh(builder=empty_builder)["state"] == "published"
    assert not retired.exists() and not receipt.exists()


def test_pointer_write_and_readback_unknown_never_claims_old_current(case, monkeypatch):
    store = case.store()
    first = store.refresh(builder=empty_builder)["version"]
    original_write, original_pointer = common.write_json, store._pointer
    injected = []
    def fail_write(path, value, *args, **kwargs):
        result = original_write(path, value, *args, **kwargs)
        if Path(path) == store.root / "current.json":
            injected.append(True)
            raise PermissionError("generated committed fault")
        return result
    def fail_read(*args, **kwargs):
        if injected:
            raise PermissionError("generated readback fault")
        return original_pointer(*args, **kwargs)
    monkeypatch.setattr(common, "write_json", fail_write)
    monkeypatch.setattr(store, "_pointer", fail_read)
    assert store.refresh(builder=empty_builder) == {"state": "unconfirmed", "version": None, "error": "material_publish_unconfirmed"}
    monkeypatch.setattr(store, "_pointer", original_pointer)
    assert store.query()["version"] != first


def test_snapshot_finish_rejects_manifest_change_and_current_building_marker(case):
    store = case.store()
    version = store.refresh(builder=empty_builder)["version"]
    folder = store.root / "versions" / version
    with store.snapshot() as snapshot:
        with (folder / "manifest.json").open("ab") as stream:
            stream.write(b" ")
        with pytest.raises(common.MaterialError, match="material_changed"):
            snapshot.finish()
    marker = common.read_json(folder / ".owner.json")
    marker = {key: value for key, value in marker.items() if key != "checksum"}
    marker["phase"] = "building"
    common.write_json(folder / ".owner.json", common.seal(marker))
    with pytest.raises(common.MaterialError, match="owner_unconfirmed"):
        store.query()


def test_junction_derived_root_is_rejected_without_reading_target(case):
    import subprocess
    store = case.store()
    target = case.base / "generated-junction-target"
    target.mkdir()
    (target / "preserve.txt").write_text("generated only", encoding="utf-8")
    assert not any(char in str(store.root) + str(target) for char in '&|<>^%!"\r\n')
    command = ["cmd.exe", "/d", "/c", "mklink", "/J", str(store.root), str(target)]
    result = subprocess.run(command, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert store.root.is_junction()
    with pytest.raises(common.MaterialError):
        case.store()
    assert (target / "preserve.txt").read_text(encoding="utf-8") == "generated only"
    assert not case.operations


def test_gc_holds_descendant_directory_lease_through_leaf_delete(case, monkeypatch):
    store = case.store()
    current = store.refresh(builder=empty_builder)["version"]
    def partial(_data, stage, **_kwargs):
        (stage / "canonical").mkdir()
        (stage / "canonical" / ("b" * 24 + ".jsonl")).write_bytes(b"generated owned partial\n")
        raise RuntimeError("generated partial")
    assert store.refresh(builder=partial)["state"] == "failed"
    stage = next(path for path in (store.root / "versions").iterdir() if path.name != current)
    native_file, native_dir = common.delete_owned_file, common.delete_empty_directory
    blocked, released = [], []
    def file_delete(base, name, digest):
        if Path(base) == stage and name.startswith("canonical/"):
            with pytest.raises(PermissionError):
                (stage / "canonical").rename(stage / "retained-lease-probe")
            blocked.append(True)
        return native_file(base, name, digest)
    def dir_delete(base, name, scope, **options):
        if Path(base) == stage and name == "canonical":
            source, temporary = stage / name, stage / "retained-lease-probe"
            assert source.absolute().is_relative_to(store.root) and temporary.absolute().is_relative_to(store.root)
            source.rename(temporary)
            temporary.rename(source)
            released.append(True)
        return native_dir(base, name, scope, **options)
    monkeypatch.setattr(common, "delete_owned_file", file_delete)
    monkeypatch.setattr(common, "delete_empty_directory", dir_delete)
    assert store.refresh(builder=empty_builder)["state"] == "published"
    assert blocked == released == [True] and not stage.exists()


def test_refresh_lock_refuses_second_process_without_starting_builder(case):
    import subprocess
    store = case.store()
    context_file = case.base / "generated-context.json"
    context_file.write_bytes(common.encode(case.context))
    outcomes = []
    def builder(*args, **kwargs):
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-m", "scripts.local_materials",
            "--context", str(context_file), "refresh"], cwd=ROOT, env=environment, capture_output=True, timeout=15)
        assert result.returncode == 1, result.stderr
        outcomes.append(json.loads(result.stdout))
        return empty_builder(*args, **kwargs)
    assert store.refresh(builder=builder)["state"] == "published"
    assert outcomes == [{"state": "busy", "version": None, "error": "material_busy"}]
    assert len(list((store.root / "versions").iterdir())) == 1
