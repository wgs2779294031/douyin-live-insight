"""MCP domain reads of new generated material only; writes happen in setup."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from scripts import audio_cleanup as cleanup, audio_segments as audio, material_common as common
from scripts import transcript_results as results
from scripts.local_mcp_data import ReadService, bounded_result, MAX_PAYLOAD_BYTES

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT.parent / "task-10/data/tmp-parent"
STAMP = datetime(2001, 1, 1, 12, tzinfo=timezone(timedelta(hours=8))).timestamp()
DAY, TAG = "2001-01-01", "generated-only"
TEXT = '合成原文 13812345678 https://example.invalid/ <script>not-instructions</script>'
AUDIT = {"network": 0, "process": 0, "registry": 0}


def audit(event, _args):
    key = ("network" if event in {"socket.connect", "socket.bind", "socket.getaddrinfo"}
           else "process" if event in {"subprocess.Popen", "os.system", "os.spawn"}
           else "registry" if event.startswith("winreg.") else None)
    if key:
        AUDIT[key] += 1
        raise AssertionError("domain reads have no runtime network/process/registry effects")


sys.addaudithook(audit)


class OfflineBridge:
    def __init__(self, context):
        self.calls = []

    def status(self, **lookup):
        self.calls.append(("status", lookup))
        return {"schema": 1, "runtime": {"state": "unknown", "verified": False,
                "code": "runtime_unavailable", "observed_at": None}, "http": None}

    def health(self):
        self.calls.append(("health", {}))
        return {"schema": 1, "runtime": {"state": "unknown", "verified": False,
            "code": "runtime_unavailable", "observed_at": None}, "asr": None,
            "coverage": {"asr_counts": "recent_window", "sample_limit": 256, "storage_counts": "scan_round"}}


@pytest.fixture
def case(tmp_path, monkeypatch):
    assert tmp_path.resolve().is_relative_to(OUT.resolve()) and not list(tmp_path.iterdir())
    data = tmp_path / "generated-data"
    data.mkdir()
    context = {"schema": 1, "root": str(ROOT), "instance_id": "mcp-domain-generated-instance",
        "data": str(data), "config": str(tmp_path / "unused-config.json"), "roster": str(tmp_path / "unused-roster.json"),
        "dist": str(tmp_path / "unused-dist"), "python": sys.executable, "profile": str(tmp_path / "never-profile"),
        "port": 29411, "cdp_port": 29412}
    service = ReadService(context, bridge_factory=OfflineBridge)
    native, actions = cleanup._disposition_flag, []
    def scoped(guard, flag):
        path = guard.path.absolute()
        assert path.is_relative_to(service.store.root) and path.is_relative_to(tmp_path)
        assert not path.is_relative_to(data) and path.suffix.lower() not in {".flv", ".wav", ".mp3", ".aac"}
        actions.append((str(path), flag))
        return native(guard, flag)
    monkeypatch.setattr(cleanup, "_disposition_flag", scoped)
    yield SimpleNamespace(base=tmp_path, data=data, context=context, service=service, actions=actions)
    assert not Path(context["profile"]).exists()
    assert AUDIT == {"network": 0, "process": 0, "registry": 0}


def hashes(case):
    answer = {}
    for base in (case.data, case.service.store.root):
        if base.exists():
            for path in base.rglob("*"):
                if path.is_file():
                    answer[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return answer


def prepare(case, *, no_speech=False):
    folder = case.data / f"audio_{TAG}_{DAY}"
    folder.mkdir()
    number, source = audio.reserve_segment(case.data, folder)
    source.write_bytes(b"mcp-generated-toy-not-real-media" * 256)
    audio.publish_closed(case.data, folder, source, number, STAMP, STAMP + 30)
    segments = [] if no_speech else [{"start": index, "end": index + 1, "text": TEXT + " #" + str(index)} for index in range(3)]
    assert results.process_segment(case.data, folder, source.name, lambda _source: (segments, 30), "fake")["state"] == "completed"
    results.rebuild_projection(case.data, folder)
    assert case.service.store.refresh()["state"] == "published"
    return source


@contextmanager
def forbid_writes(case, monkeypatch):
    before = hashes(case)
    with monkeypatch.context() as patch:
        def forbidden(*_args, **_kwargs):
            pytest.fail("MCP reads must not publish, hash audio, transcribe, project or clean")
        for owner, name in [(common, "write_json"), (cleanup, "_disposition_flag"), (cleanup, "cleanup_segment"),
            (audio, "fingerprint"), (results, "validate_success"), (results, "process_segment"), (results, "rebuild_projection")]:
            patch.setattr(owner, name, forbidden)
        patch.setattr(case.service.store, "refresh", forbidden)
        yield
    assert hashes(case) == before


def test_empty_materials_are_not_ready_without_initialization(case, monkeypatch):
    with forbid_writes(case, monkeypatch):
        assert case.service.groups()["code"] == "materials_not_ready"
        assert case.service.query()["code"] == "materials_not_ready"
        status = case.service.status()
        assert status["ok"] and status["data"]["material"]["state"] == "not_ready"
        assert status["data"]["runtime"]["state"] == "unknown" and status["data"]["http"] is None
    assert not case.service.store.root.exists() and not case.actions


def test_actual_views_and_stable_cursor_keep_sources_and_have_no_business_writes(case, monkeypatch):
    source = prepare(case)
    with forbid_writes(case, monkeypatch):
        groups = case.service.groups(limit=1)
        assert groups["ok"] and groups["data"]["items"][0]["window_is_session"] is False
        pages, cursor = [], None
        while True:
            result = case.service.query(limit=1, cursor=cursor, room=TAG)
            assert result["ok"]
            pages.extend(result["data"]["items"])
            cursor = result["data"]["next_cursor"]
            if cursor is None:
                break
        assert len(pages) == 3 and [row["source"]["json_pointer"] for row in pages] == ["/rows/0", "/rows/1", "/rows/2"]
        assert all("13812345678" in row["text"] and row["view"] == "canonical" for row in pages)
        assert all(row["source"]["material"]["sha256"] and row["source"]["generation"] for row in pages)
        shared = case.service.query(view="shared", room=TAG)
        assert shared["ok"] and shared["data"]["items"]
        assert all("13812345678" not in row.get("text", "") and "https://" not in row.get("text", "") for row in shared["data"]["items"])
        assert not case.service.bridge.calls
    assert source.exists() and not case.actions


def test_zero_row_success_still_lists_a_group(case, monkeypatch):
    prepare(case, no_speech=True)
    with forbid_writes(case, monkeypatch):
        group = case.service.groups()["data"]["items"][0]
        assert group["view_info"]["status"] == "no_speech" and group["view_info"]["records"] == 0
        page = case.service.query()
        assert page["ok"] and page["data"]["items"] == []


def test_new_version_invalidates_old_cursor_without_mcp_refresh(case):
    prepare(case)
    first = case.service.query(limit=1)
    assert first["ok"] and first["data"]["next_cursor"]
    # A separate explicit fixture refresh simulates the local UI, not a tool.
    assert case.service.store.refresh()["state"] == "published"
    result = case.service.query(limit=1, cursor=first["data"]["next_cursor"])
    assert not result["ok"] and result["code"] == "cursor_expired" and result["data"] is None


def test_material_status_finishes_and_does_not_expose_physical_paths_or_infer_uuid(case, monkeypatch):
    prepare(case)
    original, finished = case.service.store.snapshot, []
    @contextmanager
    def tracked():
        with original() as snapshot:
            finish = snapshot.finish
            def final():
                finished.append(True)
                return finish()
            snapshot.finish = final
            yield snapshot
    monkeypatch.setattr(case.service.store, "snapshot", tracked)
    request_id, service_id = "a" * 8 + "-aaaa-4aaa-aaaa-" + "a" * 12, "b" * 8 + "-bbbb-4bbb-bbbb-" + "b" * 12
    with forbid_writes(case, monkeypatch):
        result = case.service.status(request_id=request_id, service_id=service_id)
    assert result["ok"] and finished == [True]
    assert result["data"]["material"]["state"] == "ready" and result["data"]["runtime"]["state"] == "unknown"
    assert result["data"]["http"] is None and result["data"]["operation_inference"] == "only_explicit_http_receipt"
    assert case.service.bridge.calls == [("status", {"request_id": request_id, "service_id": service_id})]
    assert str(case.data) not in json.dumps(result, ensure_ascii=False) and "data_scope" not in json.dumps(result)


def test_bad_existing_owner_is_corrupt_and_never_repaired(case, monkeypatch):
    case.service.store.root.mkdir()
    foreign = case.service.store.root / "preserve.json"
    foreign.write_bytes(b'{"unknown":true}\n')
    with forbid_writes(case, monkeypatch):
        result = case.service.query()
        status = case.service.status()
    assert not result["ok"] and result["code"] == "materials_corrupt"
    assert status["data"]["material"]["state"] == "corrupt"
    assert foreign.read_bytes() == b'{"unknown":true}\n' and not (case.service.store.root / "owner.json").exists()


@pytest.mark.parametrize("filters,code", [({"limit": True}, "invalid_limit"), ({"limit": 201}, "invalid_limit"),
    ({"view": "raw"}, "invalid_filter"), ({"stream": "retry"}, "invalid_filter")])
def test_invalid_parameters_rejected_before_any_material_read(case, filters, code):
    assert case.service.query(**filters)["code"] == code
    assert not case.service.store.root.exists() and not case.service.bridge.calls


def test_uuid_pair_required_and_runtime_unavailable_is_not_zero(case):
    assert case.service.status(request_id="0" * 36)["code"] == "invalid_arguments"
    result = case.service.health()
    assert result["ok"] and result["data"]["asr"] is None and result["data"]["runtime"]["verified"] is False


def test_payload_limit_fails_whole_result_without_losing_a_cursor_silently():
    raw = {"ok": True, "data": {"items": [{"text": "生成" * MAX_PAYLOAD_BYTES}], "next_cursor": "must-not-drop"}}
    result = bounded_result(raw)
    assert result["ok"] is False and result["code"] == "result_too_large" and result["data"] is None


def test_encoding_and_private_exception_have_only_safe_errors(case, monkeypatch):
    assert bounded_result({"data": float("inf")})["code"] == "result_encoding_failed"
    monkeypatch.setattr(case.service.bridge, "health", lambda: (_ for _ in ()).throw(RuntimeError("SECRET:C:/private/key")))
    assert case.service.health()["code"] == "runtime_unavailable"
    assert "SECRET" not in json.dumps(case.service.health())
