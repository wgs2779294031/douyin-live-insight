"""Gate/roster faults use temporary synthetic files and offline client stubs."""
import json
import logging
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import llm_client
from llm_client import LLMRouter
import anchor_roster as ar
import assist_board_server as board


class StubClient:
    def __init__(self):
        self.calls = []

    def chat(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return "synthetic response"


@pytest.fixture(autouse=True)
def clean_gate_cache():
    llm_client.invalidate_gate_cache()
    yield
    llm_client.invalidate_gate_cache()


def router(tmp_path):
    r = LLMRouter({"doubao": {"api_key": "synthetic-test-only", "model": "stub",
                                "base_url": "https://example.invalid"}},
                  "doubao", budget_dir=str(tmp_path), gate_dir=str(tmp_path))
    r._clients["doubao"] = StubClient()
    return r


@pytest.mark.parametrize("body", [b"{broken", b"[]", b"null", b"\xff"])
def test_invalid_existing_gate_blocks_router_call(tmp_path, body, caplog):
    (tmp_path / llm_client.GATE_FILENAME).write_bytes(body)
    r = router(tmp_path)
    with caplog.at_level(logging.WARNING):
        assert r.chat("system", "synthetic user") is None
    assert r._clients["doubao"].calls == []
    assert any("闸门" in rec.getMessage() for rec in caplog.records)


def test_missing_gate_keeps_configuration_default(tmp_path):
    assert llm_client.runtime_gate(str(tmp_path)) == {}
    r = router(tmp_path)
    assert r.chat("system", "synthetic user") == "synthetic response"
    assert len(r._clients["doubao"].calls) == 1


def test_stat_failure_cannot_reuse_an_enabled_cache(tmp_path, monkeypatch):
    path = tmp_path / llm_client.GATE_FILENAME
    path.write_text('{"enabled": true}', encoding="utf-8")
    monkeypatch.setattr(llm_client, "GATE_CACHE_TTL", 3600)
    r = router(tmp_path)
    assert r.chat("system", "first") == "synthetic response"
    original_stat = os.stat

    def denied(file, *args, **kwargs):
        if isinstance(file, (str, bytes, os.PathLike)) and os.fspath(file) == str(path):
            raise PermissionError("synthetic stat failure")
        return original_stat(file, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(llm_client.os, "stat", denied)
        assert r.chat("system", "must be denied") is None
    assert len(r._clients["doubao"].calls) == 1
    # A recovered valid file must not remain stuck in the failure cache.
    assert r.chat("system", "recovered") == "synthetic response"


@pytest.mark.parametrize("cached", [False, True])
def test_read_failure_closes_gate_and_can_recover_without_mtime_change(tmp_path, monkeypatch,
                                                                    cached):
    path = tmp_path / llm_client.GATE_FILENAME
    path.write_text('{"enabled": true}', encoding="utf-8")
    r = router(tmp_path)
    if cached:
        assert r.chat("system", "cached") == "synthetic response"
        monkeypatch.setattr(llm_client, "GATE_CACHE_TTL", 0)
    real_open = open

    def denied(file, *args, **kwargs):
        if os.fspath(file) == str(path):
            raise PermissionError("synthetic read failure")
        return real_open(file, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(llm_client, "open", denied, raising=False)
        assert r.chat("system", "must be denied") is None
    assert len(r._clients["doubao"].calls) == int(cached)
    assert r.chat("system", "recovered") == "synthetic response"


def test_repaired_gate_mtime_replaces_closed_error_cache(tmp_path, monkeypatch):
    path = tmp_path / llm_client.GATE_FILENAME
    path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(llm_client, "GATE_CACHE_TTL", 3600)
    r = router(tmp_path)
    assert r.chat("system", "must be denied") is None
    previous = path.stat()
    path.write_text('{"enabled": true}', encoding="utf-8")
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns + 10_000_000))
    assert r.chat("system", "recovered") == "synthetic response"


def _seed(monkeypatch):
    monkeypatch.setattr(ar, "SEED", [{"tag": "synthetic seed", "name": "seed",
                                    "sec_uid": "MS4wLjABAAAA" + "x" * 40,
                                    "watch": True, "record": False}])


@pytest.mark.parametrize("error", [PermissionError, OSError])
def test_temporary_roster_read_error_preserves_original_and_raises(tmp_path, monkeypatch, error):
    _seed(monkeypatch)
    path = tmp_path / "anchors.json"
    original = b'{"version":1,"anchors":[{"tag":"user choice","enabled":false}]}'
    path.write_bytes(original)

    def denied(*args, **kwargs):
        raise error("synthetic roster read failure")

    monkeypatch.setattr(ar, "load", denied)
    with pytest.raises(error, match="synthetic roster read failure"):
        ar.ensure(path)
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("anchors.json.broken-*"))


def test_missing_roster_still_initializes_seed(tmp_path, monkeypatch):
    _seed(monkeypatch)
    result, created = ar.ensure(tmp_path / "anchors.json")
    assert created is True
    assert result["anchors"][0]["tag"] == "synthetic seed"


def test_corrupt_roster_keeps_existing_fallback_and_logs_in_http_reader(tmp_path, monkeypatch,
                                                                     caplog):
    _seed(monkeypatch)
    path = tmp_path / "anchors.json"
    path.write_text("{synthetic broken", encoding="utf-8")
    handler = board.Handler.__new__(board.Handler)
    handler.roster_path = path
    with caplog.at_level(logging.WARNING):
        result = handler._roster()
    assert result["anchors"][0]["tag"] == "synthetic seed"
    backups = list(tmp_path.glob("anchors.json.broken-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{synthetic broken"
    assert json.loads(path.read_text(encoding="utf-8"))["anchors"][0]["tag"] == "synthetic seed"
    assert any("名册" in rec.getMessage() and "重建" in rec.getMessage()
               for rec in caplog.records)


def _http_handler(tmp_path):
    path = tmp_path / "anchors.json"
    anchors = [{"tag": tag, "name": tag, "sec_uid": "MS4wLjABAAAA" + "x" * 40,
                "enabled": True, "watch": True, "record": True, "llm": True}
               for tag in ("synthetic a", "synthetic b")]
    ar.save({"version": 1, "anchors": anchors}, path)
    h = board.Handler.__new__(board.Handler)
    h.roster_path = path
    h.store = SimpleNamespace(data_dir=str(tmp_path / "runtime"))
    h._query = lambda: {"tag": "synthetic a"}
    h._auth_user = lambda: "synthetic-user"
    h._json = lambda status, payload: (status, payload)
    h._process_llm_flag = lambda tag: True
    return h, path


def _refused_stop():
    return {"ok": False, "state": "legacy_cleanup_required",
            "error": "synthetic unverified process protocol"}


def test_delete_refused_stop_keeps_roster_and_reports_failure(tmp_path, monkeypatch):
    h, path = _http_handler(tmp_path)
    original = path.read_bytes()
    stopped = {"watch": _refused_stop(), "record": {"ok": True, "state": "stopped"}}
    monkeypatch.setattr(board.anchors_mgr, "stop_anchor", lambda *a, **k: stopped)
    status, payload = h._anchor_delete({"tag": "synthetic a"})
    assert status == 409
    assert payload["ok"] is False
    assert payload["state"] == "stop_failed"
    assert payload["error"]
    assert payload["stopped"] == stopped
    assert path.read_bytes() == original
    gate_path = Path(h.store.data_dir) / llm_client.GATE_FILENAME
    assert "synthetic a" in json.loads(gate_path.read_text(encoding="utf-8"))["tags_off"]


def test_successful_delete_still_saves_removal(tmp_path, monkeypatch):
    h, path = _http_handler(tmp_path)
    monkeypatch.setattr(board.anchors_mgr, "stop_anchor", lambda *a, **k:
                        {"watch": {"ok": True}, "record": {"ok": True}})
    status, payload = h._anchor_delete({"tag": "synthetic a"})
    assert status == 200 and payload["ok"] is True
    assert ar.find(ar.load(path), "synthetic a") is None


@pytest.mark.parametrize("result", [
    {"ok": False, "stopped": {}, "started": {}},
    {"stopped": {"watch": _refused_stop()}, "started": {}},
    {"stopped": {"watch": _refused_stop()},
     "started": {"watch": {"ok": False, "state": "restart_blocked"}}},
])
def test_restart_failure_cannot_be_reported_as_success(tmp_path, monkeypatch, result):
    h, path = _http_handler(tmp_path)
    original = path.read_bytes()
    monkeypatch.setattr(board.anchors_mgr, "restart_anchor", lambda *a, **k: result)
    status, payload = h._anchor_restart()
    assert status == 409
    assert payload["ok"] is False
    assert payload["result"] == result
    assert path.read_bytes() == original


def test_successful_restart_preserves_existing_response(tmp_path, monkeypatch):
    h, path = _http_handler(tmp_path)
    result = {"stopped": {"watch": {"ok": True}},
              "started": {"watch": {"ok": True, "state": "started"}}}
    monkeypatch.setattr(board.anchors_mgr, "restart_anchor", lambda *a, **k: result)
    status, payload = h._anchor_restart()
    assert status == 200 and payload["ok"] is True


@pytest.mark.parametrize("kind", ["watch", "record"])
def test_patch_refused_auto_stop_reports_partial_and_keeps_saved_close(tmp_path, monkeypatch,
                                                                     kind):
    h, path = _http_handler(tmp_path)
    stopped = _refused_stop()
    monkeypatch.setattr(board.anchors_mgr, "stop", lambda *a, **k: stopped)
    status, payload = h._anchor_patch({kind: False, "llm": False})
    assert status == 409
    assert payload["ok"] is False
    assert payload["state"] == "partial"
    assert payload["error"]
    assert payload["auto_stopped"] == {kind: stopped}
    entry = ar.find(ar.load(path), "synthetic a")
    assert entry[kind] is False and entry["llm"] is False
    gate_path = Path(h.store.data_dir) / llm_client.GATE_FILENAME
    assert "synthetic a" in json.loads(gate_path.read_text(encoding="utf-8"))["tags_off"]


def test_successful_patch_auto_stop_is_still_success(tmp_path, monkeypatch):
    h, path = _http_handler(tmp_path)
    monkeypatch.setattr(board.anchors_mgr, "stop", lambda *a, **k:
                        {"ok": True, "state": "stopped"})
    status, payload = h._anchor_patch({"watch": False})
    assert status == 200 and payload["ok"] is True
    assert ar.find(ar.load(path), "synthetic a")["watch"] is False
