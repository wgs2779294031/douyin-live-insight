"""Generated context, fake dependencies and a short owned Python child only."""
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
import local_context as context_api
import run_watchdog as watchdog
import watch_anchor_live as watch
import stream_recorder as recorder
import transcribe_backfill as transcribe
from scripts.backfill_tasks import process_identity


@pytest.fixture
def customer(tmp_path, monkeypatch):
    data = tmp_path / "客户 数据"
    data.mkdir()
    raw = {"schema": 1, "root": str(ROOT), "config": str(tmp_path / "主 配置.json"),
        "data": str(data), "roster": str(tmp_path / "名册.json"), "dist": str(tmp_path / "dist"),
        "python": sys.executable, "port": 18975, "cdp_port": 18976,
        "profile": str(tmp_path / "专有 浏览器"), "instance_id": "synthetic_instance_123"}
    Path(raw["config"]).write_text(json.dumps({"rooms": [], "output_dir": "ignored", "llm": {"enabled": False}}), encoding="utf-8")
    path = tmp_path / "context.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setenv("DY_CUSTOMER_CONTEXT", str(path))
    monkeypatch.setenv("DY_CUSTOMER_STOP", str(tmp_path / "stop.json"))
    monkeypatch.setenv("DY_CUSTOMER_ROLE_NONCE", "synthetic-role-nonce")
    for module in (watchdog, watch, recorder, transcribe):
        monkeypatch.setattr(module, "CUSTOMER", raw)
        monkeypatch.setattr(module, "DATA", data)
    return SimpleNamespace(raw=raw, path=path, data=data,
        stop=Path(os.environ["DY_CUSTOMER_STOP"]))


def request_stop(customer, identity=None, **changes):
    identity = identity or process_identity(os.getpid())
    body = dict(schema=1, action="stop", instance_id=customer.raw["instance_id"],
                pid=identity["pid"], created=identity["created"])
    body.update(changes)
    customer.stop.write_text(json.dumps(body), encoding="utf-8")


def test_explicit_context_and_environment_do_not_use_seller_paths(customer, monkeypatch):
    loaded = context_api.load_customer_context()
    assert loaded == customer.raw
    for name in ("CDP_PORT", "WATCH_DEBUG_PORT", "WATCH_BROWSER_PROFILE", "DOUYIN_OUTPUT_DIR", "WB_RECORD_PY", "HF_HOME"):
        monkeypatch.setenv(name, "previous")
    context_api.activate_customer_environment(loaded)
    assert os.environ["CDP_PORT"] == os.environ["WATCH_DEBUG_PORT"] == str(loaded["cdp_port"])
    assert os.environ["DOUYIN_OUTPUT_DIR"] == loaded["data"]
    assert os.environ["WB_RECORD_PY"] == loaded["python"]
    assert os.environ["WATCH_BROWSER_PROFILE"] == loaded["profile"]
    assert Path(os.environ["HF_HOME"]).is_relative_to(customer.data)


@pytest.mark.parametrize("field,bad", [("root", "relative"), ("root", "C:/unowned-code"),
    ("data", None), ("port", True), ("cdp_port", -1), ("schema", True), ("instance_id", "../bad")])
def test_bad_context_fails_without_legacy_fallback(customer, field, bad):
    raw = dict(customer.raw, **{field: bad})
    customer.path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        context_api.load_customer_context()


def test_no_customer_environment_preserves_legacy_entry(monkeypatch):
    monkeypatch.delenv("DY_CUSTOMER_CONTEXT", raising=False)
    assert context_api.load_customer_context() is None


def test_role_ready_is_exact_identity_and_nonce(customer):
    identity = context_api.publish_role_ready(customer.raw, "watch", "合成主播")
    receipt = json.loads(context_api.role_receipt_path(customer.raw, "watch", "合成主播").read_text())
    assert identity == process_identity(os.getpid())
    assert receipt["pid"] == identity["pid"] and receipt["created"] == identity["created"]
    assert receipt["role"] == "watch:合成主播" and receipt["nonce"] == "synthetic-role-nonce"


@pytest.mark.parametrize("mutation", [{}, {"pid": 1}, {"created": 0}, {"created": True},
    {"instance_id": "another-instance"}, {"schema": True}])
def test_stop_requires_exact_role_identity(customer, mutation):
    request_stop(customer, **mutation)
    assert context_api.customer_stop_requested(customer.raw) is (not mutation)


def test_real_child_recognizes_only_verified_parent_stop(customer):
    request_stop(customer)
    code = "import json,local_context as c; x=c.load_customer_context(); print(json.dumps([c.customer_stop_requested(x),c.customer_stop_requested(x,allow_parent=True)]))"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(ROOT), str(ROOT / "scripts"))), PYTHONDONTWRITEBYTECODE="1")
    child = subprocess.Popen([sys.executable, "-X", "utf8", "-B", "-c", code],
        env=env, cwd=customer.data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        stdout, stderr = child.communicate(timeout=15)
        assert child.returncode == 0, stderr
        assert json.loads(stdout) == [False, True]
        assert child.poll() == 0
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)


@pytest.mark.parametrize("changes", [{}, {"instance_id": "wrong"}, {"created": 11},
    {"port": 9}, {"profile": "relative"}])
def test_browser_receipt_requires_same_instance_and_exact_live_identity(customer, changes):
    value = dict(schema=1, instance_id=customer.raw["instance_id"], pid=123, created=10,
                 port=customer.raw["cdp_port"], profile=customer.raw["profile"])
    value.update(changes)
    path = customer.data / ".local_app/browser-owner.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    assert context_api.browser_owner_valid(customer.raw,
        lambda pid: dict(state="alive", pid=pid, created=10),
        lambda port: dict(state="listening", pids=[123])) is (not changes)
    assert not context_api.browser_owner_valid(customer.raw,
        lambda pid: dict(state="unknown", pid=pid, created=None),
        lambda port: dict(state="listening", pids=[123]))


@pytest.mark.parametrize("listener", [dict(state="vacant", pids=[]),
    dict(state="unknown", pids=[]), dict(state="listening", pids=[456]),
    dict(state="listening", pids=[123, 456])])
def test_live_browser_receipt_does_not_authorize_foreign_or_unknown_listener(customer, listener):
    path = customer.data / ".local_app/browser-owner.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(dict(schema=1, instance_id=customer.raw["instance_id"], pid=123,
        created=10, port=customer.raw["cdp_port"], profile=customer.raw["profile"])), encoding="utf-8")
    assert not context_api.browser_owner_valid(customer.raw,
        lambda pid: dict(state="alive", pid=pid, created=10), lambda port: listener)


@pytest.mark.parametrize("owned", [False, True])
def test_watchdog_never_manages_unknown_browser_on_200(customer, monkeypatch, owned):
    monkeypatch.setattr(watchdog, "browser_owner_valid", lambda ctx: owned)
    monkeypatch.setattr(watchdog, "port_alive", lambda: True)
    for name in ("sweep_duplicate_tabs", "_profile_pids", "resolve_chrome"):
        monkeypatch.setattr(watchdog, name, lambda *args: pytest.fail("customer role must not manage browsers"))
    assert watchdog.ensure_browser() is owned


def test_watchdog_stop_waits_for_owned_child_instead_of_terminating(customer, monkeypatch):
    request_stop(customer)
    monkeypatch.setattr(watchdog, "_CUSTOMER_IDENTITY", process_identity(os.getpid()))
    finished = threading.Event()
    child = SimpleNamespace(poll=lambda: 0 if finished.is_set() else None, returncode=0)
    timer = threading.Timer(.1, finished.set)
    timer.start()
    try:
        assert watchdog._wait_with_port_check(child, {}, check_seconds=.01) == 0
    finally:
        timer.join()


def test_real_customer_watchdog_waits_without_browser_and_duplicate_cannot_replace_ack(customer):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(ROOT), str(ROOT / "scripts"))),
               PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    command = [sys.executable, "-X", "utf8", "-B", str(ROOT / "scripts/run_watchdog.py"),
               "--sec-uid", "synthetic-not-an-account", "--tag", "合成主播"]
    def spawn(nonce):
        options = dict(cwd=customer.data, env=dict(env, DY_CUSTOMER_ROLE_NONCE=nonce),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if os.name == "nt":
            from windows_job import spawn_in_job
            return spawn_in_job(command, **options)
        return subprocess.Popen(command, **options)
    children = []
    first = spawn("first-owned-nonce")
    children.append(first)
    try:
        receipt_path = context_api.role_receipt_path(customer.raw, "watch", "合成主播")
        deadline = time.monotonic() + 10
        while not receipt_path.exists() and first.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert receipt_path.exists()
        receipt_bytes = receipt_path.read_bytes()
        receipt = json.loads(receipt_bytes)
        assert receipt["nonce"] == "first-owned-nonce"
        assert first.poll() is None and not Path(customer.raw["profile"]).exists()
        second = spawn("second-duplicate-nonce")
        children.append(second)
        output, errors = second.communicate(timeout=10)
        assert second.returncode == 3, (output, errors)
        assert receipt_path.read_bytes() == receipt_bytes
        request_stop(customer, receipt)
        output, errors = first.communicate(timeout=10)
        assert first.returncode == 0, (output, errors)
        assert not Path(customer.raw["profile"]).exists()
        assert not (customer.data / "watchdog_合成主播.pid").exists()
    finally:
        for child in children:
            job = getattr(child, "_watch_job", None)
            if job is not None:
                job.close(timeout=5)
            elif child.poll() is None:
                child.terminate()
            child.wait(timeout=5)


def test_watch_configs_and_collector_use_same_explicit_context(customer, monkeypatch):
    before = Path(customer.raw["config"]).read_bytes()
    path = watch.make_config("1234", "合成主播", False, Path(customer.raw["config"]))
    assert path.parent == customer.data
    generated = json.loads(path.read_text())
    assert generated["output_dir"] == customer.raw["data"]
    assert generated["rooms"] == [{"room_id": "1234", "label": "合成主播", "llm": False, "feishu": False}]
    assert Path(customer.raw["config"]).read_bytes() == before
    seen = []
    monkeypatch.setattr(watch.subprocess, "Popen", lambda command, **kwargs: seen.append((command, kwargs)) or SimpleNamespace(pid=123))
    watch.start_collector(path, False)
    assert seen[0][0][:2] == [customer.raw["python"], str(ROOT / "main.py")]
    assert seen[0][0][-1] == str(path)
    seen[0][1]["stdout"].close()


def test_recorder_does_not_start_new_asr_while_stopping(customer, monkeypatch):
    request_stop(customer)
    monkeypatch.setattr(recorder, "_CUSTOMER_IDENTITY", process_identity(os.getpid()))
    monkeypatch.setattr(recorder.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("must not spawn while stopping"))
    recorder.spawn_transcribe("合成主播", "2026-11-15", lambda _: None)
    assert not list(customer.data.glob(".tr_*"))


def test_customer_closed_audio_waits_for_managed_asr_without_new_child(customer, monkeypatch):
    children = []
    monkeypatch.setattr(recorder, "_TRANSCRIBE_CHILDREN", children)
    monkeypatch.setattr(recorder.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("customer handoff must use the managed ASR lane"))
    monkeypatch.setattr(recorder, "pid_owns_file", lambda *args, **kwargs: pytest.fail("customer handoff must not consult the legacy tag lock"))
    recorder.spawn_transcribe("合成主播", "2026-11-15", lambda _: None)
    assert recorder._TRANSCRIBE_CHILDREN is children and children == []
    assert not list(customer.data.glob(".tr_*"))
    assert not list(customer.data.glob("transcribe_*.log"))


def test_customer_asr_auto_model_never_uses_legacy_or_download(customer, tmp_path, monkeypatch):
    monkeypatch.delenv("WB_ASR_MODEL", raising=False)
    monkeypatch.setattr(transcribe, "LOCAL_MODEL", tmp_path / "missing-model")
    with pytest.raises((ValueError, FileNotFoundError)):
        transcribe.load_model("auto")


def test_collector_stop_checks_before_reconciling_llm(customer, monkeypatch):
    import main
    monkeypatch.setattr(main, "CUSTOMER", customer.raw)
    request_stop(customer)
    runtime = SimpleNamespace(reconcile_llm=lambda: pytest.fail("stopped application must not reconcile new work"))
    main._wait_until_done([runtime])
