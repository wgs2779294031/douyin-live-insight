"""Actual customer worker/engine; deletion only of this run's owned toy bytes."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_asr_worker import (context, identities, binding, run_fake, Clock,
                             PARENT, WORKER, NONCE, no_computation)
from scripts import asr_protocol as protocol
from scripts import audio_segments as audio
from scripts import audio_cleanup as cleanup
from scripts import audio_storage as storage
from scripts import transcript_results as results

OUT = Path(os.environ.get("DLI_TEST_ROOT", str(Path(__file__).resolve().parents[1] / ".test-state")))
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows native cleanup integration")


@pytest.mark.parametrize("old_success", [False, True])
def test_actual_customer_worker_cleans_and_retains_text(context, identities, monkeypatch, old_success):
    data = Path(context["data"])
    assert data.resolve().is_relative_to(OUT.resolve())
    marker = data / "owned-by-task-08B.txt"
    marker.write_text("new synthetic fragments only", encoding="utf-8")
    folder = data / "audio_generated_2000-01-01"
    folder.mkdir()
    number, source = audio.reserve_segment(data, folder)
    payload = b"task-08B-worker-generated-only" * 256
    with source.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    audio.publish_closed(data, folder, source, number, 100, 160)
    rows = [{"start": 0, "end": 1, "text": "task 08B durable text"}]
    if old_success:
        assert results.process_segment(data, folder, source.name,
            lambda _: (rows, 60), "generated-model")["state"] == "completed"
        results.rebuild_projection(data, folder)
    preserved, events, calls = {}, [], []
    native = cleanup.cleanup_segment

    def owned_cleanup(actual_data, actual_folder, actual_name):
        assert (Path(actual_data), Path(actual_folder), actual_name) == (data, folder, source.name)
        assert marker.read_text(encoding="utf-8") == "new synthetic fragments only"
        assert protocol.permission(context, NONCE, PARENT, WORKER) == "admitted"
        calls.append("native")
        return native(actual_data, actual_folder, actual_name)

    monkeypatch.setattr(cleanup, "cleanup_segment", owned_cleanup)
    disk_calls = []
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda path:
        (disk_calls.append(Path(path)) or SimpleNamespace(free=2 ** 31, total=2 ** 33)))

    class Adapter:
        model_name = "generated-model"

        def __init__(self, _context):
            events.append("adapter")

        def signature(self):
            return ["missing-local-model-but-stable"]

        def prepare(self):
            assert not old_success, "existing successful text needs no model"
            events.append("prepare")

        def recognize(self, path):
            assert not old_success and path == source and path.read_bytes() == payload
            events.append("recognize")
            return rows, 60

    paths = protocol.paths(context)
    protocol.ensure(context)
    protocol.write_record(paths["admission"], binding(context, "asr_admission"))
    permission_calls = 0

    def permission(*args):
        nonlocal permission_calls
        permission_calls += 1
        assert permission_calls < 240
        facts = results.validate_success(data, folder, source.name)
        if facts.get("current_result") and facts.get("projection_ready") and not preserved:
            evidence = [results.result_path(data, folder, source.name),
                results.attempt_path(data, folder, source.name),
                *results.projection_paths(data, folder)[:2],
                folder / ".segments" / (source.stem + ".json"), folder / audio.INDEX]
            preserved.update({path: path.read_bytes() for path in evidence})
        if facts.get("audio_cleaned") and facts.get("cleanup_state") == "deleted":
            if not paths["stop"].exists():
                protocol.write_record(paths["stop"], dict(binding(context, "asr_stop"), action="stop"))
        return protocol.permission(*args)

    assert run_fake(context, Clock(), permission_fn=permission, adapter_factory=Adapter) == 0
    assert events == (["adapter"] if old_success else ["adapter", "prepare", "recognize"])
    assert calls == ["native"] and not source.exists()
    assert disk_calls == [data]
    facts = results.validate_success(data, folder, source.name)
    assert facts["history_usable"] and facts["audio_cleaned"] and facts["cleanup_state"] == "deleted"
    assert preserved and preserved == {path: path.read_bytes() for path in preserved}
    summary = protocol.read_record(paths["status"])["summary"]
    assert summary["state"] == "stopping" and summary["counts"]["complete"] == 1
    assert summary["storage"]["last_cleanup"]["state"] == "deleted"
    assert marker.is_file() and paths["stop"].is_file()


@pytest.mark.parametrize("admission", ["waiting", "unknown"])
def test_unadmitted_worker_constructs_no_storage_or_cleanup(context, monkeypatch, admission):
    assert Path(context["data"]).resolve().is_relative_to(OUT.resolve())
    monkeypatch.setattr(storage, "StorageSummary", no_computation)
    monkeypatch.setattr(cleanup, "cleanup_segment", no_computation)
    clock = Clock()
    assert run_fake(context, clock, permission_fn=lambda *_:
        "stop" if clock.value >= .4 else admission,
        adapter_factory=no_computation, engine_factory=no_computation) == 0
    assert not protocol.paths(context)["retry"].exists()
    assert not list(Path(context["data"]).glob("audio_*"))
