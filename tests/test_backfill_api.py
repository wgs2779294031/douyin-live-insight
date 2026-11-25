"""Real authenticated Board HTTP and durable store; no collector or SDK sends."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import socket
import sys
import threading
from types import SimpleNamespace
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import assist_board_server as board
import board_auth as auth
from backfill_tasks import TaskStore


@pytest.fixture
def api(tmp_path, monkeypatch):
    config, data = tmp_path / "config.json", tmp_path / "data"
    data.mkdir()
    config.write_text(json.dumps({"llm": {
        "enabled": False, "active_provider": "synthetic", "daily_token_budget": 0,
        "providers": {"synthetic": {"api_key": "synthetic-not-a-real-key",
            "base_url": "https://model.invalid/v1", "model": "synthetic-model",
            "protocol": "openai_compatible"}}}}), encoding="utf-8")
    spawns = []
    def spawn(command, **options):
        spawns.append((command, options))
        raise OSError("synthetic launch interruption")
    store = TaskStore(config_path=str(config), data_dir=str(data), spawn=spawn)
    class H(board.Handler):
        auth_db = {"secret": "synthetic-board-secret", "users": [{"name": "tester"}]}
        config_path = str(config)
        store = SimpleNamespace(data_dir=str(data), today=lambda: "2026-11-15")
        def _backfill_store(self):
            return store
        def log_message(self, *_args):
            pass
    server = board.ThreadingHTTPServer(("127.0.0.1", 0), H)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
    worker.start()
    token = auth.make_token("tester", H.auth_db["secret"])
    cookie = auth.cookie_header(token).split(";", 1)[0]
    original_connect = socket.create_connection
    def only_loopback(address, *args, **kwargs):
        assert address[0] == "127.0.0.1", "external network forbidden"
        return original_connect(address, *args, **kwargs)
    monkeypatch.setattr(socket, "create_connection", only_loopback)
    def request(method, body=None, query="", authorized=True, origin=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["Cookie"] = cookie
        if origin:
            headers["Origin"] = origin
        try:
            connection.request(method, "/api/review/backfill" + query,
                               json.dumps(body) if body is not None else None, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()
    yield SimpleNamespace(request=request, config=config, data=data, spawns=spawns, store=store)
    server.shutdown()
    server.server_close()
    worker.join(2)
    assert not worker.is_alive()


def body(**values):
    return dict({"tag": "synthetic", "date": "2026-11-15", "force": False,
                 "request_id": str(uuid.uuid4())}, **values)


def test_concurrent_submissions_and_readonly_recovery_share_one_durable_run(api):
    requests = [body(), body()]
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda item: api.request("POST", item), requests))
    assert [code for code, _ in replies] == [200, 200]
    task_id = replies[0][1]["task_id"]
    assert task_id == replies[1][1]["task_id"]
    assert sorted(item["created"] for _, item in replies) == [False, True]
    assert api.spawns == []
    for submitted in requests:
        code, task = api.request("GET", query="?tag=synthetic&date=2026-11-15&request_id="
                                + submitted["request_id"])
        assert code == 200 and task["task_id"] == task_id and task["state"] == "blocked"
    assert api.request("GET", query="?task_id=" + task_id)[1]["task_id"] == task_id


def test_enable_does_not_rerun_old_task_and_force_uuid_is_idempotent(api):
    old = api.request("POST", body())[1]["task_id"]
    raw = json.loads(api.config.read_text(encoding="utf-8"))
    raw["llm"]["enabled"] = True
    api.config.write_text(json.dumps(raw), encoding="utf-8")
    assert api.request("POST", body())[1]["task_id"] == old
    redo = body(force=True)
    first, second = api.request("POST", redo), api.request("POST", redo)
    assert first[0] == second[0] == 200
    assert first[1]["task_id"] == second[1]["task_id"] != old
    assert first[1]["task"]["state"] == "unknown"
    assert len(api.spawns) == 1
    command, options = api.spawns[0]
    assert "--task-id" in command and "--config" in command and "--data-dir" in command
    assert "--tag" not in command and "--date" not in command
    assert "DOUYIN_BACKFILL_LAUNCH_TOKEN" in options["env"]
    assert options["env"]["DOUYIN_BACKFILL_LAUNCH_TOKEN"] not in json.dumps(first)
    assert api.request("GET", query="?task_id=" + first[1]["task_id"])[0] == 200
    assert len(api.spawns) == 1


@pytest.mark.parametrize("changes", [{"force": "false"}, {"force": 1},
    {"tag": "../outside"}, {"date": "2026-02-30"}, {"request_id": "bad"},
    {"extra": True}])
def test_invalid_request_has_no_launch(api, changes):
    assert api.request("POST", body(**changes))[0] == 400
    assert api.spawns == []
    assert not list((api.data / "backfill_tasks").glob("*.json"))


@pytest.mark.parametrize("authorized,origin,code", [(False, None, 401),
    (True, "https://cross-site.invalid", 403)])
def test_existing_auth_and_origin_gates_precede_task_submission(api, authorized, origin, code):
    assert api.request("POST", body(), authorized=authorized, origin=origin)[0] == code
    assert api.spawns == []
    assert not list((api.data / "backfill_tasks").glob("*.json"))


def test_legacy_and_missing_receipts_are_readonly(api):
    output = api.data / "portrait_synthetic_2026-11-15.jsonl"
    output.write_text('{"portrait":{"synthetic":true}}\n', encoding="utf-8")
    code, old = api.request("GET", query="?tag=synthetic&date=2026-11-15")
    assert code == 200 and old["state"] == "legacy_unverified" and old["task_id"] is None
    assert api.request("POST", body())[1]["task_id"] is None
    assert api.spawns == []
    assert not list((api.data / "backfill_tasks").glob("*.json"))


def test_unknown_id_resource_mismatch_and_corrupt_state_never_reexecute(api):
    task_id = api.request("POST", body())[1]["task_id"]
    assert api.request("GET", query="?task_id=" + task_id + "&tag=another&date=2026-11-15")[0] == 404
    missing = task_id[:-32] + "0" * 32
    assert api.request("GET", query="?task_id=" + missing)[0] == 404
    path = next((api.data / "backfill_tasks").glob("*.json"))
    path.write_text("{", encoding="utf-8")
    assert api.request("GET", query="?task_id=" + task_id)[0] == 503
    assert api.request("POST", body(force=True))[0] == 503
    assert path.read_text(encoding="utf-8") == "{" and api.spawns == []
