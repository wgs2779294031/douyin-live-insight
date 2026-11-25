"""Generated state and fake owned-browser/CDP facts only; no browser/platform I/O."""
import copy
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
import local_app as app
from local_login import LoginGuide
from test_local_app import make_context

UID = "MS4wLjABAAAA" + "x" * 40
ENTRY = {"tag": "generated-anchor", "sec_uid": UID}


class FakeBrowser:
    def __init__(self, pid):
        self.count = 1
        self.closed = False
        self.identity = {"state": "alive", "pid": pid, "created": float(pid)}
        self.proc = SimpleNamespace(pid=pid, poll=lambda: None if self.count else 0)
        self.forced = 0

    def active_count(self):
        return self.count

    def close(self, force=False):
        if force:
            self.forced += 1
            self.count = 0
        if self.count == 0:
            self.closed = True
            return True
        return False


class FakeDriver:
    def __init__(self):
        self.calls = []
        self.state = "channel_offline"
        self.reason = "profile_offline"
        self.hook = None

    def open(self, target_id, marker, guard, remember_target):
        assert guard()
        self.calls.append(("open", target_id, marker))
        if target_id is None:
            assert remember_target("dedicated-target")
        if self.hook:
            self.hook()
        return {"state": "page_opened", "reason": "page_opened", "cookie": "DO-NOT-PUBLISH"}

    def check(self, target_id, sec_uid, guard):
        assert guard() and target_id == "dedicated-target" and sec_uid == UID
        self.calls.append(("check", target_id))
        if self.hook:
            self.hook()
        return {"state": self.state, "reason": self.reason,
            "cookie": "DO-NOT-PUBLISH", "signed_url": "https://example.invalid/?token=DO-NOT-PUBLISH"}


@pytest.fixture
def managed(tmp_path, monkeypatch):
    context = make_context(tmp_path, ports=(18765, 19222))
    registry, spawned, drivers = {}, [], []
    endpoint = {"state": "vacant", "pids": []}
    clock = [1000.0]
    def identity(pid):
        browser = registry.get(pid)
        if browser is not None and browser.count:
            return dict(browser.identity)
        return {"state": "dead", "pid": pid, "created": None}
    monkeypatch.setattr(app, "process_identity", identity)
    monkeypatch.setattr(app, "listener_pid", lambda _port: dict(endpoint))
    import run_watchdog
    monkeypatch.setattr(run_watchdog, "resolve_chrome", lambda _env: "synthetic-browser.exe")
    def spawn(command, **kwargs):
        assert command[0] == "synthetic-browser.exe"
        browser = FakeBrowser(12000 + len(spawned))
        registry[browser.proc.pid] = browser
        spawned.append(browser)
        endpoint.update(state="listening", pids=[browser.proc.pid])
        return browser
    manager = app.RoleManager(context, spawn=spawn, now=lambda: clock[0])
    def factory(_context):
        driver = FakeDriver()
        drivers.append(driver)
        return driver
    manager.login = LoginGuide(manager, driver_factory=factory, wall_time=lambda: 1800000000.0)
    manager.login.prepare_timeout = .1
    def configure(entry=ENTRY):
        Path(context["roster"]).write_text(json.dumps({"version": 1, "anchors": [dict(entry,
            enabled=False, watch=False, record=False, llm=False)]}), encoding="utf-8")
    configure()
    def ready():
        result = manager.login.request("prepare")
        manager.tick({"anchors": []})
        manager.tick({"anchors": []})
        assert result["ok"] and manager.login.snapshot()["browser"]["state"] == "ready"
        return manager.login.request("open")
    return SimpleNamespace(manager=manager, context=context, spawned=spawned, drivers=drivers,
        endpoint=endpoint, clock=clock, configure=configure, ready=ready)


def test_empty_roster_prepare_uses_existing_browser_path_and_no_other_intent(managed):
    item, manager = managed, managed.manager
    Path(item.context["roster"]).write_text('{"anchors":[]}', encoding="utf-8")
    manager.suppressed.add("watch:user-stopped")
    manager.launch_failures["watch:other"] = {"failures": 3, "retry_at": 9000}
    original = copy.deepcopy(manager.launch_failures), set(manager.suppressed)
    before = Path(item.context["config"]).read_bytes(), Path(item.context["roster"]).read_bytes()
    for _ in range(3):
        assert manager.login.snapshot()["browser"]["state"] == "not_prepared"
    assert item.spawned == item.drivers == []
    assert item.ready()["status"]["channel"]["state"] == "page_opened"
    assert len(item.spawned) == 1 and manager.roles == {}
    manager.login.request("open")
    assert len(item.spawned) == 1 and item.drivers[0].calls[1][1] == "dedicated-target"
    assert original == (manager.launch_failures, manager.suppressed)
    assert before == (Path(item.context["config"]).read_bytes(), Path(item.context["roster"]).read_bytes())
    assert manager.login.request("check")["status"]["channel"]["state"] == "need_target"
    assert len(item.drivers[0].calls) == 2


def test_manual_browser_close_does_not_restart_without_new_intent(managed):
    managed.ready()
    manager = managed.manager
    first = manager.browser
    first.count = 0
    managed.endpoint.update(state="vacant", pids=[])
    manager.tick({"anchors": []})
    for _ in range(5):
        managed.clock[0] += 100
        manager.tick({"anchors": []})
    assert len(managed.spawned) == 1 and manager.browser is None and not manager.browser_manual
    assert manager.login.snapshot()["browser"]["state"] == "closed"
    managed.ready()
    assert len(managed.spawned) == 2 and manager.browser is not first


def test_prepare_waits_one_minute_for_the_owned_browser_listener(managed):
    manager = managed.manager
    assert manager.login.request("prepare")["ok"]
    manager.tick({"anchors": []})
    browser = managed.spawned[0]
    started = managed.clock[0]
    managed.endpoint.update(state="vacant", pids=[])

    for elapsed in (25, 60):
        managed.clock[0] = started + elapsed
        manager.tick({"anchors": []})
        assert manager.login_browser_status() == {
            "state": "starting", "reason": "browser_waiting_for_owned_port",
        }
        assert manager.browser is browser and browser.forced == 0

    managed.clock[0] = started + 60.1
    manager.tick({"anchors": []})
    assert manager.login_browser_status() == {
        "state": "failed", "reason": "browser_start_timeout",
    }
    assert manager.browser is None and browser.closed and browser.forced == 1
    assert len(managed.spawned) == 1


@pytest.mark.parametrize("failure,state", [("missing", "missing"), ("conflict", "conflict"), ("unknown", "identity_unknown")])
def test_prepare_failure_is_explicit_and_does_not_call_cdp(managed, monkeypatch, failure, state):
    manager = managed.manager
    if failure == "missing":
        import run_watchdog
        monkeypatch.setattr(run_watchdog, "resolve_chrome", lambda _env: None)
    else:
        managed.endpoint.update(state="listening" if failure == "conflict" else "unknown", pids=[998877])
    manager.login.request("prepare")
    manager.tick({"anchors": []})
    assert manager.login.snapshot()["browser"]["state"] == state
    assert managed.spawned == managed.drivers == []
    assert not manager.login.request("check", ENTRY)["ok"]
    assert managed.drivers == []


@pytest.mark.parametrize("state,reason", [("channel_live", "profile_live"), ("channel_offline", "profile_offline"),
    ("restricted", "profile_restricted"), ("unknown", "profile_unconfirmed")])
def test_safe_channel_states_are_not_login_or_collection_claims(managed, state, reason):
    assert managed.ready()["status"]["busy"] is False
    driver = managed.drivers[0]
    driver.state, driver.reason = state, reason
    result = managed.manager.login.request("check", ENTRY)
    assert result["status"]["busy"] is False
    status = result["status"]
    assert status["channel"] == {"state": state, "reason": reason, "target_tag": ENTRY["tag"],
        "checked_at": 1800000000.0, "source": "owned_browser_profile_api"}
    assert status["login_confirmed"] is status["collection_recovered"] is False
    assert "DO-NOT-PUBLISH" not in json.dumps(result) and UID not in json.dumps(result)


def test_failure_expiry_and_owner_loss_never_restore_old_channel_green(managed):
    managed.ready()
    guide = managed.manager.login
    assert guide.request("check", ENTRY)["status"]["channel"]["state"] == "channel_offline"
    managed.clock[0] += guide.evidence_ttl + 1
    assert guide.snapshot()["channel"]["state"] == "expired"
    driver = managed.drivers[0]
    driver.state, driver.reason = "unknown", "SECRET-UNSAFE-REASON"
    failed = guide.request("check", ENTRY)
    assert failed["status"]["channel"]["state"] == "unknown"
    assert failed["status"]["channel"]["reason"] == "action_failed"
    driver.state, driver.reason = "restricted", "profile_restricted"
    guide.request("check", ENTRY)
    managed.endpoint.update(state="unknown", pids=[])
    invalid = guide.snapshot()["channel"]
    assert invalid["state"] == "unknown" and invalid["checked_at"] is None and invalid["source"] == "none"
    managed.endpoint.update(state="listening", pids=[managed.manager.browser.proc.pid])
    assert guide.snapshot()["channel"]["state"] == "unknown"


@pytest.mark.parametrize("change", ["generation", "target", "stop"])
def test_late_success_cannot_publish_after_owner_target_or_stop_changes(managed, change):
    managed.ready()
    manager, driver = managed.manager, managed.drivers[0]
    def changed():
        if change == "generation":
            manager.browser_generation = "new-generation"
        elif change == "target":
            managed.configure(dict(ENTRY, sec_uid=UID + "y"))
        else:
            manager.begin_stop()
    driver.hook = changed
    result = manager.login.request("check", ENTRY)
    assert not result["ok"] and result["status"]["channel"]["state"] == "unknown"
    assert result["status"]["channel"]["checked_at"] is None and not result["status"]["busy"]


def test_inflight_action_deduplicates_without_holding_manager_lock_or_early_drain(managed):
    managed.ready()
    manager, driver = managed.manager, managed.drivers[0]
    entered, release = threading.Event(), threading.Event()
    def blocked():
        entered.set()
        assert release.wait(3)
    driver.hook = blocked
    results = []
    thread = threading.Thread(target=lambda: results.append(manager.login.request("check", ENTRY)))
    thread.start()
    try:
        assert entered.wait(2)
        duplicate = manager.login.request("open")
        assert not duplicate["ok"] and duplicate["status"]["busy"]
        manager.tick({"anchors": []})
        manager.begin_stop()
        assert manager.login.busy and not manager.drained()
        assert not manager.login.request("prepare")["ok"]
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive() and not results[0]["status"]["busy"]
    assert results[0]["status"]["channel"]["state"] == "unknown" and manager.drained()


def test_foreign_or_reused_browser_identity_has_zero_guide_operations(managed, monkeypatch):
    managed.ready()
    manager = managed.manager
    calls = list(managed.drivers[0].calls)
    monkeypatch.setattr(app, "process_identity", lambda pid: {"state": "alive", "pid": pid, "created": 1.0})
    assert manager.login.snapshot()["browser"]["state"] == "identity_unknown"
    assert not manager.login.request("open")["ok"]
    assert not manager.login.request("check", ENTRY)["ok"]
    assert managed.drivers[0].calls == calls and len(managed.spawned) == 1


def test_check_target_uses_same_roster_normalization_without_changing_source(managed):
    managed.ready()
    manager = managed.manager
    managed.configure({"tag": "  " + ENTRY["tag"] + " ", "sec_uid": " " + UID + "  "})
    before = Path(managed.context["roster"]).read_bytes()
    result = manager.login.request("check", ENTRY)
    assert result["status"]["channel"]["state"] == "channel_offline"
    assert Path(managed.context["roster"]).read_bytes() == before
    Path(managed.context["roster"]).write_text(json.dumps({"anchors": [ENTRY,
        {"tag": " " + ENTRY["tag"] + " ", "sec_uid": UID}]}), encoding="utf-8")
    assert manager.login.snapshot()["channel"]["state"] == "unknown"
    assert manager.login.snapshot()["channel"]["checked_at"] is None


@pytest.mark.parametrize("endpoint,state", [({"state": "unknown", "pids": []}, "identity_unknown"),
    ({"state": "listening", "pids": [998877]}, "conflict")])
def test_prepared_browser_losing_port_ownership_is_not_a_ready_channel(managed, endpoint, state):
    managed.ready()
    manager = managed.manager
    calls = list(managed.drivers[0].calls)
    managed.endpoint.update(endpoint)
    assert manager.login.snapshot()["browser"]["state"] == state
    assert not manager.login.request("check", ENTRY)["ok"]
    assert managed.drivers[0].calls == calls and len(managed.spawned) == 1
