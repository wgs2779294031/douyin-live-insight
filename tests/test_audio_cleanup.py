"""Native deletion is confined to this task's newly generated test fragments."""
import json
import os
from pathlib import Path
import threading

import pytest

from scripts import audio_segments as audio
from scripts import audio_cleanup as cleanup
from scripts import transcript_results as results

OUT = Path(os.environ.get("DLI_TEST_ROOT", str(Path(__file__).resolve().parents[1] / ".test-state")))
pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows failclosed primitive")


@pytest.fixture
def day(tmp_path):
    assert tmp_path.resolve().is_relative_to((OUT / "tmp-parent").resolve()), "only this run's generated fragments may be deleted"
    data = tmp_path / "generated-data"
    folder = data / "audio_generated_2000-01-01"
    folder.mkdir(parents=True)
    return data, folder


def prepared(day, *, no_speech=False):
    data, folder = day
    number, path = audio.reserve_segment(data, folder)
    with path.open("xb") as stream:
        stream.write(b"generated-only-" * 512)
        stream.flush()
        os.fsync(stream.fileno())
    audio.publish_closed(data, folder, path, number, 1000, 1060)
    rows = [] if no_speech else [{"start": 0, "end": 1, "text": "generated text"}]
    outcome = results.process_segment(data, folder, path.name, lambda _: (rows, 60), "fake-model")
    assert outcome["state"] == "completed"
    results.rebuild_projection(data, folder)
    return path


def operation(day, path):
    data, folder = day
    result, digest, ready = results._current_text(data, folder, path.name)
    assert ready
    return cleanup.read_operation(data, folder, path.name, result["source"], result["generation"], digest)


def test_native_exact_handle_delete_is_durable_and_idempotent(day):
    data, folder = day
    path = prepared(day)
    before = {item: item.read_bytes() for item in (results.result_path(data, folder, path.name),
        results.attempt_path(data, folder, path.name), *results.projection_paths(data, folder)[:2],
        folder / ".segments" / (path.stem + ".json"), folder / audio.INDEX)}
    outcome = cleanup.cleanup_segment(data, folder, path.name)
    assert outcome == {"state": "deleted", "reason": "audio_deleted", "deleted": True, "deleted_now": True}
    assert not path.exists() and operation(day, path)["state"] == "deleted"
    assert before == {item: item.read_bytes() for item in before}
    assert cleanup.cleanup_segment(data, folder, path.name)["deleted_now"] is False


def test_delete_failure_keeps_exact_pending_then_retries(day, monkeypatch):
    data, folder = day
    path = prepared(day)
    original = cleanup._set_disposition
    def fail(_guard):
        raise PermissionError("generated disposition failure")
    monkeypatch.setattr(cleanup, "_set_disposition", fail)
    outcome = cleanup.cleanup_segment(data, folder, path.name)
    assert outcome["state"] == "pending" and not outcome["deleted"] and path.exists()
    pending = operation(day, path)
    assert pending["state"] == "pending"
    monkeypatch.setattr(cleanup, "_set_disposition", original)
    assert cleanup.cleanup_segment(data, folder, path.name)["deleted_now"]
    assert not path.exists() and operation(day, path)["state"] == "deleted"


@pytest.mark.parametrize("stage", ["before_delete", "after_delete"])
def test_interruptions_recover_without_extra_inference(day, monkeypatch, stage):
    data, folder = day
    path = prepared(day)
    original = cleanup._set_disposition if stage == "before_delete" else cleanup._settle
    def interrupt(*_args):
        raise SystemExit("generated cleanup interruption")
    target = "_set_disposition" if stage == "before_delete" else "_settle"
    monkeypatch.setattr(cleanup, target, interrupt)
    with pytest.raises(SystemExit):
        cleanup.cleanup_segment(data, folder, path.name)
    assert path.exists() is (stage == "before_delete")
    assert operation(day, path)["state"] == "pending"
    monkeypatch.setattr(cleanup, target, original)
    outcome = cleanup.cleanup_segment(data, folder, path.name)
    assert outcome["deleted"] and outcome["deleted_now"] is (stage == "before_delete")
    assert operation(day, path)["state"] == "deleted"


def test_native_guards_block_source_and_metadata_replacement(day, monkeypatch):
    data, folder = day
    path = prepared(day)
    original = cleanup._set_disposition
    checked = []
    def verify(guard):
        for target in (path, folder, data, results.result_path(data, folder, path.name),
                       results.attempt_path(data, folder, path.name), cleanup.operation_path(data, folder, path.name)):
            with pytest.raises(PermissionError):
                target.rename(target.with_name(target.name + ".moved"))
            checked.append(target.name)
        with pytest.raises(PermissionError):
            with path.open("wb") as stream:
                stream.write(b"must-never-truncate")
        original(guard)
    monkeypatch.setattr(cleanup, "_set_disposition", verify)
    outcome = cleanup.cleanup_segment(data, folder, path.name)
    assert outcome["deleted"] and len(checked) == 6


def test_accept_write_unconfirmed_never_deletes_and_valid_pending_can_retry(day, monkeypatch):
    data, folder = day
    path = prepared(day)
    write = cleanup._write_operation
    dispose = cleanup._set_disposition
    calls = []
    def uncertain(target, value):
        write(target, value)
        raise PermissionError("generated pending write readback uncertainty")
    monkeypatch.setattr(cleanup, "_write_operation", uncertain)
    monkeypatch.setattr(cleanup, "_set_disposition", lambda _guard: calls.append(True))
    outcome = cleanup.cleanup_segment(data, folder, path.name)
    assert outcome["reason"] == "cleanup_accept_unconfirmed" and not outcome["deleted_now"]
    assert path.exists() and calls == [] and operation(day, path)["state"] == "pending"
    monkeypatch.setattr(cleanup, "_write_operation", write)
    monkeypatch.setattr(cleanup, "_set_disposition", dispose)
    assert cleanup.cleanup_segment(data, folder, path.name)["deleted"]


def test_same_name_replacement_before_handle_open_is_preserved(day, monkeypatch):
    data, folder = day
    path = prepared(day)
    original = cleanup._Guard.__enter__
    entered = []
    payload = path.read_bytes()
    stamp = path.stat().st_mtime_ns
    def enter(guard):
        if guard.delete:
            entered.append(True)
            path.rename(path.with_suffix(".retained"))
            path.write_bytes(payload)
            os.utime(path, ns=(stamp, stamp))
        return original(guard)
    monkeypatch.setattr(cleanup._Guard, "__enter__", enter)
    outcome = cleanup.cleanup_segment(data, folder, path.name)
    assert entered == [True] and outcome["reason"] == "source_changed" and not outcome["deleted_now"]
    assert path.read_bytes() == payload and path.with_suffix(".retained").read_bytes() == payload


def test_settlement_failure_preserves_missing_source_pending_for_recovery(day, monkeypatch):
    data, folder = day
    path = prepared(day)
    write = cleanup._write_operation
    def fail(target, value):
        if value["state"] == "deleted":
            raise PermissionError("generated settlement failure")
        write(target, value)
    monkeypatch.setattr(cleanup, "_write_operation", fail)
    outcome = cleanup.cleanup_segment(data, folder, path.name)
    assert outcome["state"] == "pending" and outcome["deleted"] and outcome["deleted_now"]
    assert not path.exists() and operation(day, path)["state"] == "pending"
    monkeypatch.setattr(cleanup, "_write_operation", write)
    outcome = cleanup.cleanup_segment(data, folder, path.name)
    assert outcome["deleted"] and not outcome["deleted_now"] and operation(day, path)["state"] == "deleted"


@pytest.mark.parametrize("fault", ["checksum", "generation", "state"])
def test_bad_or_conflicting_accepted_operation_is_not_overwritten(day, monkeypatch, fault):
    data, folder = day
    path = prepared(day)
    def fail(_guard):
        raise PermissionError("generated delete failure")
    monkeypatch.setattr(cleanup, "_set_disposition", fail)
    assert cleanup.cleanup_segment(data, folder, path.name)["state"] == "pending"
    target = cleanup.operation_path(data, folder, path.name)
    record = json.loads(target.read_text(encoding="utf-8"))
    if fault == "checksum":
        record["checksum"] = "0" * 64
    else:
        record["generation" if fault == "generation" else "state"] = "b" * 32 if fault == "generation" else "invalid"
        record = cleanup._seal({key: value for key, value in record.items() if key != "checksum"})
    audio.atomic_json(target, record)
    retained = target.read_bytes()
    outcome = cleanup.cleanup_segment(data, folder, path.name)
    assert outcome["state"] == "kept" and not outcome["deleted_now"] and path.exists()
    assert target.read_bytes() == retained


def test_existing_hardlink_keeps_audio_without_accepting_cleanup(day):
    data, folder = day
    path = prepared(day)
    linked = folder / "generated-linked.flv"
    os.link(path, linked)
    outcome = cleanup.cleanup_segment(data, folder, path.name)
    assert outcome["state"] == "kept" and not outcome["deleted_now"] and path.exists() and linked.exists()
    assert not cleanup.operation_path(data, folder, path.name).exists()


def test_writer_busy_creates_no_cleanup_intent(day):
    data, folder = day
    path = prepared(day)
    entered, release = threading.Event(), threading.Event()
    def hold():
        with results.writer(data):
            entered.set()
            assert release.wait(10)
    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert entered.wait(2)
        assert cleanup.cleanup_segment(data, folder, path.name)["state"] == "busy"
        assert path.exists() and not cleanup.operation_path(data, folder, path.name).exists()
    finally:
        release.set()
        thread.join(2)
        assert not thread.is_alive()


def test_successful_disposition_waits_for_last_attribute_reader_before_marking_deleted(day):
    import msvcrt
    data, folder = day
    path = prepared(day)
    native = cleanup._Windows()
    handle = native.api.CreateFileW(cleanup._extended(path), 0x80, 0x5, None, 3, 0x200000, None)
    assert handle not in (None, cleanup.ctypes.c_void_p(-1).value)
    fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    try:
        outcome = cleanup.cleanup_segment(data, folder, path.name)
        assert outcome["state"] == "pending" and outcome["deleted_now"] and not outcome["deleted"]
        assert operation(day, path)["state"] == "pending"
    finally:
        os.close(fd)
    assert not path.exists()
    outcome = cleanup.cleanup_segment(data, folder, path.name)
    assert outcome["deleted"] and not outcome["deleted_now"] and operation(day, path)["state"] == "deleted"


def test_hardlink_before_mark_is_detected_and_marking_cancelled(day, monkeypatch):
    data, folder = day
    path = prepared(day)
    original = cleanup._set_disposition
    linked = folder / "late-hardlink.flv"
    def link_then_mark(guard):
        os.link(path, linked)
        original(guard)
    monkeypatch.setattr(cleanup, "_set_disposition", link_then_mark)
    outcome = cleanup.cleanup_segment(data, folder, path.name)
    assert outcome["state"] == "pending" and not outcome["deleted"] and not outcome["deleted_now"]
    assert path.exists() and linked.read_bytes() == path.read_bytes()
    assert operation(day, path)["state"] == "pending"


def test_marked_source_refuses_new_hardlink(day, monkeypatch):
    data, folder = day
    path = prepared(day)
    original = cleanup._verify_marked
    checked = []
    def compete(guard):
        with pytest.raises(PermissionError):
            os.link(path, folder / "cannot-link-marked.flv")
        checked.append(True)
        original(guard)
    monkeypatch.setattr(cleanup, "_verify_marked", compete)
    assert cleanup.cleanup_segment(data, folder, path.name)["deleted"] and checked == [True]
    assert not (folder / "cannot-link-marked.flv").exists()


@pytest.mark.parametrize("failure", [SystemExit, RuntimeError])
def test_exception_after_true_cancels_before_handle_close(day, monkeypatch, failure):
    data, folder = day
    path = prepared(day)
    flag = cleanup._disposition_flag
    def mark_then_interrupt(guard, value):
        flag(guard, value)
        if value:
            raise failure("generated exception after successful native TRUE")
    monkeypatch.setattr(cleanup, "_disposition_flag", mark_then_interrupt)
    with pytest.raises(failure):
        cleanup.cleanup_segment(data, folder, path.name)
    assert path.exists() and operation(day, path)["state"] == "pending"


def test_cancel_failure_is_unknown_without_claiming_zero_deletion(day, monkeypatch):
    data, folder = day
    path = prepared(day)
    linked = folder / "retained-hardlink.flv"
    dispose, flag = cleanup._set_disposition, cleanup._disposition_flag
    def compete(guard):
        os.link(path, linked)
        dispose(guard)
    def fail_cancel(guard, value):
        if not value:
            raise PermissionError("generated cancellation uncertainty")
        flag(guard, value)
    monkeypatch.setattr(cleanup, "_set_disposition", compete)
    monkeypatch.setattr(cleanup, "_disposition_flag", fail_cancel)
    outcome = cleanup.cleanup_segment(data, folder, path.name)
    assert outcome["state"] == "pending" and outcome["reason"] == "cleanup_cancel_unconfirmed"
    assert outcome["deleted_now"] and linked.exists()
    assert json.loads(cleanup.operation_path(data, folder, path.name).read_text(encoding="utf-8"))["state"] == "pending"


def test_metadata_hardlink_at_mark_cancels_source_deletion(day, monkeypatch):
    data, folder = day
    path = prepared(day)
    flag = cleanup._disposition_flag
    def compete(guard, value):
        if value:
            os.link(results.result_path(data, folder, path.name), folder / "late-result-link.json")
        flag(guard, value)
    monkeypatch.setattr(cleanup, "_disposition_flag", compete)
    outcome = cleanup.cleanup_segment(data, folder, path.name)
    assert outcome["state"] == "pending" and not outcome["deleted_now"] and path.exists()


def test_new_projection_evidence_at_mark_cancels_source_deletion(day, monkeypatch):
    data, folder = day
    path = prepared(day)
    flag = cleanup._disposition_flag
    def compete(guard, value):
        if value:
            results.projection_paths(data, folder)[2].write_text("broken generated evidence", encoding="utf-8")
        flag(guard, value)
    monkeypatch.setattr(cleanup, "_disposition_flag", compete)
    outcome = cleanup.cleanup_segment(data, folder, path.name)
    assert outcome["state"] == "pending" and not outcome["deleted_now"] and path.exists()


def test_deleted_record_rechecks_recreated_source_name(day, monkeypatch):
    data, folder = day
    path = prepared(day)
    assert cleanup.cleanup_segment(data, folder, path.name)["deleted"]
    enter = cleanup._Guard.__enter__
    target = cleanup.operation_path(data, folder, path.name)
    def compete(guard):
        if guard.path == target:
            path.write_bytes(b"new-replacement-must-remain")
        return enter(guard)
    monkeypatch.setattr(cleanup._Guard, "__enter__", compete)
    outcome = cleanup.cleanup_segment(data, folder, path.name)
    assert outcome["state"] == "kept" and not outcome["deleted"] and not outcome["deleted_now"]
    assert path.read_bytes() == b"new-replacement-must-remain"


@pytest.mark.parametrize("when", ["before", "after"])
def test_settlement_recreated_name_is_never_deleted_or_claimed_absent(day, monkeypatch, when):
    data, folder = day
    path = prepared(day)
    settle, write = cleanup._settle, cleanup._write_operation
    def compete(*args):
        if when == "before":
            path.write_bytes(b"new-replacement")
        return settle(*args)
    def write_then_recreate(target, value):
        write(target, value)
        if value["state"] == "deleted" and when == "after":
            path.write_bytes(b"new-replacement")
    monkeypatch.setattr(cleanup, "_settle", compete)
    monkeypatch.setattr(cleanup, "_write_operation", write_then_recreate)
    outcome = cleanup.cleanup_segment(data, folder, path.name)
    assert outcome["state"] == "pending" and not outcome["deleted"] and outcome["deleted_now"]
    assert path.read_bytes() == b"new-replacement"


@pytest.mark.parametrize("stage", ["unclosed", "not_transcribed", "failed"])
def test_unconfirmed_work_is_retained_without_cleanup_acceptance(day, monkeypatch, stage):
    data, folder = day
    number, path = audio.reserve_segment(data, folder)
    path.write_bytes(b"new-unconfirmed-test-segment" * 200)
    if stage != "unclosed":
        audio.publish_closed(data, folder, path, number, 1000, 1060)
    if stage == "failed":
        attempt = results.begin_attempt(data, folder, path.name)
        results.fail_attempt(data, folder, path.name, attempt["generation"], "transcription_failed")
    monkeypatch.setattr(cleanup, "_set_disposition", lambda _guard: pytest.fail("unconfirmed audio must not be marked"))
    outcome = cleanup.cleanup_segment(data, folder, path.name)
    assert outcome["state"] == "kept" and not outcome["deleted"] and not outcome["deleted_now"]
    assert path.exists() and not cleanup.operation_path(data, folder, path.name).exists()


def test_outside_data_folder_is_never_touched(day, monkeypatch):
    data, folder = day
    outside_data = data.parent / "new-outside-data"
    outside = outside_data / folder.name
    outside.mkdir(parents=True)
    path = prepared((outside_data, outside))
    retained = path.read_bytes()
    monkeypatch.setattr(cleanup, "_set_disposition", lambda _guard: pytest.fail("outside source must not be marked"))
    outcome = cleanup.cleanup_segment(data, outside, path.name)
    assert outcome["state"] == "kept" and not outcome["deleted_now"]
    assert path.read_bytes() == retained


@pytest.mark.parametrize("target", ["source", "cleanup_directory"])
def test_native_symlink_is_refused(day, monkeypatch, target):
    data, folder = day
    path = prepared(day)
    if target == "source":
        retained = path.with_suffix(".retained")
        path.rename(retained)
        try:
            os.symlink(retained.name, path)
        except OSError:
            pytest.skip("native symlink privilege unavailable; no privilege setting changed")
    else:
        retained = data / "new-linked-directory"
        retained.mkdir()
        try:
            os.symlink(retained, folder / ".cleanup", target_is_directory=True)
        except OSError:
            pytest.skip("native symlink privilege unavailable; no privilege setting changed")
    monkeypatch.setattr(cleanup, "_set_disposition", lambda _guard: pytest.fail("linked path must not be marked"))
    outcome = cleanup.cleanup_segment(data, folder, path.name)
    assert outcome["state"] == "kept" and not outcome["deleted_now"]
    assert retained.exists()


def test_operation_read_is_pure_and_never_recurses_into_result_validation(day, monkeypatch):
    data, folder = day
    path = prepared(day)
    result, digest, _ = results._current_text(data, folder, path.name)
    monkeypatch.setattr(results, "validate_success", lambda *_args: pytest.fail("read_operation must not validate results"))
    monkeypatch.setattr(results, "_current_text", lambda *_args: pytest.fail("read_operation must not load results"))
    assert cleanup.read_operation(data, folder, path.name, result["source"], result["generation"], digest) is None
    assert not (folder / ".cleanup").exists() and path.exists()


def test_non_windows_is_failclosed_without_any_file_action(day, monkeypatch):
    from types import SimpleNamespace
    data, folder = day
    path = prepared(day)
    monkeypatch.setattr(cleanup, "os", SimpleNamespace(name="posix"))
    outcome = cleanup.cleanup_segment(data, folder, path.name)
    assert outcome == {"state": "kept", "reason": "cleanup_platform_unsupported", "deleted": False, "deleted_now": False}
    assert path.exists()


def test_native_junction_cleanup_directory_is_refused(day, monkeypatch):
    import _winapi
    data, folder = day
    path = prepared(day)
    target = data.parent / "generated-junction-target"
    target.mkdir()
    directory = folder / ".cleanup"
    _winapi.CreateJunction(str(target), str(directory))
    assert directory.is_junction()
    monkeypatch.setattr(cleanup, "_set_disposition", lambda _guard: pytest.fail("junction metadata must not authorize deletion"))
    outcome = cleanup.cleanup_segment(data, folder, path.name)
    assert outcome["state"] == "kept" and not outcome["deleted_now"] and path.exists()
    assert list(target.iterdir()) == []
