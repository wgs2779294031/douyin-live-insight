"""Fresh canonical credentials, fake SDK only: no paid or external requests."""
import builtins
import json
import os
import socket
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llm_client as client_module
import llm_service as service


@pytest.fixture
def fake_sdk(monkeypatch):
    calls, instances, hooks = [], [], []
    usage = SimpleNamespace(prompt_tokens=0, completion_tokens=0)
    content = ["synthetic reply"]
    def deny_network(*args, **kwargs):
        raise AssertionError("external network forbidden in fake SDK tests")
    monkeypatch.setattr(socket, "socket", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)

    class SDK:
        def __init__(self, api_key, base_url, **kwargs):
            self.key, self.base_url, self.closed = api_key, base_url, False
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
            instances.append(self)

        def create(self, **kwargs):
            calls.append({"key": self.key, "base_url": self.base_url, **kwargs})
            if hooks:
                hooks.pop(0)()
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content[0]))],
                                   usage=usage)

        def close(self):
            self.closed = True

    module = ModuleType("openai")
    module.OpenAI = SDK
    monkeypatch.setitem(sys.modules, "openai", module)
    monkeypatch.setattr(client_module.time, "sleep", lambda _: None)
    client_module.invalidate_gate_cache()
    return SimpleNamespace(calls=calls, instances=instances, hooks=hooks, usage=usage, content=content)


def provider(key="fake-old-001", model="model-a", base_url="https://old.invalid", protocol="openai_compatible"):
    return {"api_key": key, "model": model, "base_url": base_url, "protocol": protocol}


def save(path, providers, primary="primary"):
    path.write_text(json.dumps({"llm": {"enabled": True, "providers": providers,
                                         "active_provider": primary, "daily_token_budget": 0}}), encoding="utf-8")


def router(path, providers=None):
    if providers is not None:
        save(path, providers)
    data = path.parent / "data"
    data.mkdir(exist_ok=True)
    return service.load_llm_config(str(path)).build_router(budget_dir=str(data))


def test_all_existing_routers_use_changed_content_even_same_mtime_and_size(tmp_path, fake_sdk):
    path = tmp_path / "config.json"
    first = router(path, {"primary": provider()})
    second = router(path)
    assert first.chat("system", "old") and second.chat("system", "old")
    previous = path.stat()
    save(path, {"primary": provider("fake-new-001", "model-b", "https://new.invalid")})
    assert path.stat().st_size == previous.st_size
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    assert first.chat("system", "new") and second.chat("system", "new")
    assert [row["key"] for row in fake_sdk.calls] == ["fake-old-001"] * 2 + ["fake-new-001"] * 2
    assert fake_sdk.calls[-1]["model"] == "model-b"
    assert fake_sdk.calls[-1]["base_url"] == "https://new.invalid"


def test_previously_returned_client_reference_reloads_then_revokes(tmp_path, fake_sdk):
    path = tmp_path / "config.json"
    routed = router(path, {"primary": provider()})
    held = routed.active_client
    assert held.chat("system", "old")
    save(path, {"primary": provider("fake-new-001", "model-b")})
    assert held.chat("system", "new")
    assert fake_sdk.calls[-1]["key"] == "fake-new-001"
    save(path, {"primary": provider("")})
    count = len(fake_sdk.calls)
    assert held.chat("system", "deleted") is None and held.probe() is False
    assert routed.chat("system", "deleted") is None
    assert routed._clients == {} and len(fake_sdk.calls) == count


def test_source_bound_direct_client_and_explicit_static_client_contract(tmp_path, fake_sdk):
    path = tmp_path / "config.json"
    save(path, {"primary": provider("fake-new-001", "model-b")})
    bound = client_module.LLMClient("fake-old-001", "https://old.invalid", "model-a",
                                    provider_name="primary", config_path=str(path))
    static = client_module.LLMClient("fake-static", "https://static.invalid", "static")
    assert bound.chat("system", "bound") and static.chat("system", "static")
    assert fake_sdk.calls[-2]["key"] == "fake-new-001"
    assert fake_sdk.calls[-1]["key"] == "fake-static"


def test_watch_copy_cannot_override_canonical_or_inject_source_path(tmp_path, fake_sdk):
    canonical = tmp_path / "config.json"
    save(canonical, {"primary": provider("fake-main-001")})
    (tmp_path / "data").mkdir()
    copy = tmp_path / "data/watch_synthetic.config.json"
    save(copy, {"primary": provider("fake-copy-001")})
    cfg = service.load_llm_config(str(copy))
    assert cfg._source_path == str(canonical)
    routed = cfg.build_router(budget_dir=str(tmp_path / "data"))
    assert routed.chat("system", "canonical")
    save(canonical, {"primary": provider("fake-next-001")})
    assert routed.chat("system", "canonical changed")
    assert [row["key"] for row in fake_sdk.calls] == ["fake-main-001", "fake-next-001"]
    cfg = service.LLMConfig.from_dict({"_source_path": str(copy), "source_path": str(copy)})
    assert cfg._source_path == ""


def test_backup_key_swap_and_delete_never_borrow_old_client(tmp_path, fake_sdk):
    path = tmp_path / "config.json"
    routed = router(path, {"primary": provider(), "backup": provider("fake-back-old")})
    held = routed.active_client
    old_backup = routed._clients["backup"]

    class Exhausted:
        budget = last_total = 1
        def check(self):
            return False
    routed._budget = Exhausted()
    save(path, {"primary": provider(), "backup": provider("fake-back-new", "backup-new")})
    assert routed.chat("system", "borrow")
    assert fake_sdk.calls[-1]["key"] == "fake-back-new"
    assert old_backup.chat("system", "held backup")
    assert fake_sdk.calls[-1]["key"] == "fake-back-new"
    save(path, {"primary": provider()})
    count = len(fake_sdk.calls)
    assert routed.chat("system", "backup deleted") is None
    assert old_backup.chat("system", "backup deleted") is None
    assert "backup" not in routed._clients and len(fake_sdk.calls) == count
    assert held.provider_name == "primary"


def test_delete_during_budget_check_blocks_stale_borrow_map(tmp_path, fake_sdk):
    path = tmp_path / "config.json"
    routed = router(path, {"primary": provider(), "backup": provider("fake-back-old")})
    routed.active_client
    class DeleteDuringCheck:
        budget = last_total = 1
        def check(self):
            save(path, {"primary": provider()})
            return False
    routed._budget = DeleteDuringCheck()
    assert routed.chat("system", "must not use revoked fallback") is None
    assert fake_sdk.calls == [] and "backup" not in routed._clients


def test_automatic_probe_uses_new_primary_then_blocks_deleted_primary(tmp_path, fake_sdk):
    path = tmp_path / "config.json"
    routed = router(path, {"primary": provider(), "backup": provider("fake-backup")})
    routed.active_client
    routed.active = "backup"
    routed.last_probe_ts = 0
    save(path, {"primary": provider("fake-new-001", "new-primary"), "backup": provider("fake-backup")})
    routed.probe_and_maybe_switch()
    assert fake_sdk.calls[-1]["messages"] == [{"role": "user", "content": "ping"}]
    assert fake_sdk.calls[-1]["key"] == "fake-new-001" and fake_sdk.calls[-1]["model"] == "new-primary"
    held = routed.active_client
    save(path, {"backup": provider("fake-backup")})
    count = len(fake_sdk.calls)
    routed.probe_and_maybe_switch()
    assert held.probe() is False and len(fake_sdk.calls) == count


@pytest.mark.parametrize("protocol,name,thinking", [
    ("openai_compatible", "custom", False), ("openai_compatible", "doubao", False),
    ("doubao", "custom", True), ("", "doubao", True), ("", "deepseek", False),
])
def test_provider_protocol_controls_chat_and_probe_extensions(tmp_path, fake_sdk, protocol, name, thinking):
    path = tmp_path / "config.json"
    save(path, {name: provider(protocol=protocol)}, primary=name)
    cfg = service.load_llm_config(str(path))
    assert cfg.providers[name]["protocol"] == protocol
    client = cfg.build_router(budget_dir=str(tmp_path)).active_client
    assert client.chat("system", "custom") and client.probe()
    assert all(("extra_body" in call) == thinking for call in fake_sdk.calls)


@pytest.mark.parametrize("failure", ["missing", "malformed", "wrong-root", "bad-section", "permission"])
def test_unreadable_config_revokes_router_and_held_clients_without_secret_log(tmp_path, fake_sdk, monkeypatch, caplog, failure):
    path = tmp_path / "config.json"
    secret = "fake-private-key-never-log"
    routed = router(path, {"primary": provider(secret), "backup": provider("fake-backup")})
    held = routed.active_client
    assert routed.chat("system", "before failure")
    if failure == "missing":
        path.unlink()
    elif failure == "malformed":
        path.write_text('{"llm": "' + secret, encoding="utf-8")
    elif failure == "wrong-root":
        path.write_text('[]', encoding="utf-8")
    elif failure == "bad-section":
        path.write_text('{"llm":[]}', encoding="utf-8")
    else:
        original = builtins.open
        def deny(source, *args, **kwargs):
            if str(source) == str(path):
                raise PermissionError(secret)
            return original(source, *args, **kwargs)
        monkeypatch.setattr(builtins, "open", deny)
    count = len(fake_sdk.calls)
    assert routed.chat("system", "blocked") is None
    routed.probe_and_maybe_switch()
    assert held.chat("system", "held blocked") is None and held.probe() is False
    assert routed._clients == {} and len(fake_sdk.calls) == count
    assert secret not in caplog.text


def test_inflight_request_finishes_pinned_snapshot_then_next_request_reloads(tmp_path, fake_sdk):
    path = tmp_path / "config.json"
    routed = router(path, {"primary": provider()})
    held = routed.active_client
    fake_sdk.hooks.append(lambda: save(path, {"primary": provider("fake-new-001", "model-b")}))
    assert held.chat("system", "in flight")
    assert fake_sdk.calls[-1]["key"] == "fake-old-001" and fake_sdk.calls[-1]["model"] == "model-a"
    assert held.chat("system", "next")
    assert fake_sdk.calls[-1]["key"] == "fake-new-001" and fake_sdk.calls[-1]["model"] == "model-b"


def test_loader_and_sdk_errors_never_log_their_raw_secret(tmp_path, fake_sdk, monkeypatch, caplog):
    path = tmp_path / "config.json"
    secret = "fake-private-key-never-log"
    save(path, {"primary": provider(secret)})
    client = router(path).active_client
    class Failure(RuntimeError):
        status_code = 401
    fake_sdk.hooks.append(lambda: (_ for _ in ()).throw(Failure(secret)))
    assert client.chat("system", "no secret in exception logs") is None
    monkeypatch.setattr(service.json, "load", lambda *a, **k: (_ for _ in ()).throw(ValueError(secret)))
    assert service.load_llm_config(str(path)).is_active is False
    assert secret not in caplog.text


def test_reload_does_not_wait_on_inflight_client_with_router_metrics_callback(tmp_path, fake_sdk):
    path = tmp_path / "config.json"
    save(path, {"primary": provider()})
    observed = []
    routed = service.load_llm_config(str(path)).build_router(
        budget_dir=str(tmp_path), on_metrics=lambda _: observed.append(routed.active_model))
    held = routed.active_client
    old_sdk = held._client
    entered, release, reloaded = threading.Event(), threading.Event(), threading.Event()
    errors = []
    def sending():
        entered.set()
        assert release.wait(3)
    fake_sdk.hooks.append(sending)
    def old_request():
        try:
            assert held.chat("system", "already issued")
        except BaseException as error:
            errors.append(type(error).__name__)
    first = threading.Thread(target=old_request, daemon=True)
    first.start()
    assert entered.wait(2)
    save(path, {"primary": provider("fake-new-001", "model-b")})
    def reload_route():
        try:
            assert routed.active_model == "model-b"
        except BaseException as error:
            errors.append(type(error).__name__)
        finally:
            reloaded.set()
    second = threading.Thread(target=reload_route, daemon=True)
    second.start()
    finished_without_waiting = reloaded.wait(2)
    was_closed_while_sending = old_sdk.closed
    release.set()
    first.join(2)
    second.join(2)
    assert finished_without_waiting and not was_closed_while_sending
    assert not first.is_alive() and not second.is_alive() and errors == []
    assert observed == ["model-b"] and old_sdk.closed
    assert held.chat("system", "next request")
    assert fake_sdk.calls[-1]["key"] == "fake-new-001"


def test_issued_request_accounts_to_send_time_primary_after_route_changes(tmp_path, fake_sdk):
    path = tmp_path / "config.json"
    routed = router(path, {"primary": provider(), "backup": provider("fake-backup")})
    routed.active_client
    class Account:
        def __init__(self):
            self.entries = []
        def check(self):
            return True
        def add(self, prompt, completion):
            self.entries.append((prompt, completion))
    primary, backup = Account(), Account()
    routed._budget = primary
    routed._backup_budgets = {"backup": backup}
    fake_sdk.usage.prompt_tokens, fake_sdk.usage.completion_tokens = 7, 3
    def change_during_send():
        save(path, {"backup": provider("fake-backup")})
        assert routed.active_model == "model-a"
    fake_sdk.hooks.append(change_during_send)
    assert routed.chat("system", "issued before primary removal")
    assert routed.primary == "backup"
    assert primary.entries == [(7, 3)] and backup.entries == []


def test_router_send_never_holds_route_lock_while_waiting_on_held_client(tmp_path, fake_sdk, monkeypatch):
    path = tmp_path / "config.json"
    save(path, {"primary": provider()})
    routed = service.load_llm_config(str(path)).build_router(
        budget_dir=str(tmp_path), on_metrics=lambda _: routed.active_model)
    held = routed.active_client
    entered, release, routed_waiting = threading.Event(), threading.Event(), threading.Event()
    errors = []
    def sending():
        entered.set()
        assert release.wait(3)
    fake_sdk.hooks.append(sending)
    original_metrics = routed._selection_metrics
    def metrics(account, purpose=""):
        routed_waiting.set()
        return original_metrics(account, purpose)
    monkeypatch.setattr(routed, "_selection_metrics", metrics)
    def invoke(function):
        try:
            assert function("system", "synthetic concurrent request")
        except BaseException as error:
            errors.append(type(error).__name__)
    first = threading.Thread(target=invoke, args=(held.chat,), daemon=True)
    second = threading.Thread(target=invoke, args=(routed.chat,), daemon=True)
    first.start()
    assert entered.wait(2)
    second.start()
    assert routed_waiting.wait(2)
    release.set()
    first.join(2)
    second.join(2)
    assert not first.is_alive() and not second.is_alive() and errors == []
    assert len(fake_sdk.calls) == 2


def test_trusted_provider_override_survives_reload_but_not_key_revocation(tmp_path, fake_sdk):
    path = tmp_path / "config.json"
    save(path, {"primary": provider(), "backup": provider("fake-back-old")})
    routed = service.load_llm_config(str(path)).build_router(
        budget_dir=str(tmp_path), provider_override="backup")
    held = routed.active_client
    assert routed.chat("system", "selected backup")
    assert fake_sdk.calls[-1]["key"] == "fake-back-old"
    save(path, {"primary": provider(), "backup": provider("fake-back-new", "backup-b")})
    assert routed.chat("system", "changed selected key")
    assert routed.primary == "backup" and fake_sdk.calls[-1]["key"] == "fake-back-new"
    count = len(fake_sdk.calls)
    save(path, {"primary": provider()})
    assert routed.chat("system", "selected provider removed") is None
    assert held.probe() is False and len(fake_sdk.calls) == count
    assert service.LLMConfig.from_dict({"provider_override": "backup", "requested_provider": "backup"})._source_path == ""


def test_newly_keyed_backup_keeps_preconfigured_budget_and_ledger(tmp_path, fake_sdk):
    path = tmp_path / "config.json"
    save(path, {"primary": provider(), "backup": provider("")})
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["llm"]["backup_daily_token_budgets"] = {"backup": 1}
    path.write_text(json.dumps(raw), encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    routed = service.load_llm_config(str(path)).build_router(budget_dir=str(data))
    assert "backup" not in routed._clients and "backup" not in routed._backup_budgets
    client_module.DailyTokenBudget(1, str(data), name="backup").add(1, 0)
    save(path, {"primary": provider(), "backup": provider("fake-back-new")})
    routed.active_client
    ledger = routed._backup_budgets["backup"]
    class PrimaryExhausted:
        budget = last_total = 1
        def check(self):
            return False
    routed._budget = PrimaryExhausted()
    assert routed.chat("system", "backup already capped") is None
    assert fake_sdk.calls == [] and ledger.last_total == 1
    save(path, {"primary": provider(), "backup": provider("fake-next-key")})
    routed.active_client
    assert routed._backup_budgets["backup"] is ledger


def configured_backup_budget_router(tmp_path, limit):
    path = tmp_path / "config.json"
    save(path, {"primary": provider(), "backup": provider("fake-backup")})
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["llm"]["backup_daily_token_budgets"] = {"backup": limit}
    path.write_text(json.dumps(raw), encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    return path, data, service.load_llm_config(str(path)).build_router(budget_dir=str(data))


def test_removing_primary_cannot_promote_backup_out_of_its_budget(tmp_path, fake_sdk):
    path, data, routed = configured_backup_budget_router(tmp_path, 1)
    ledger = routed._backup_budgets["backup"]
    ledger.add(1, 0)
    save(path, {"backup": provider("fake-backup")})
    assert routed.chat("system", "old backup remains capped") is None
    assert routed.primary == "backup" and routed._budget_primary_provider == "primary"
    assert routed._backup_budgets["backup"] is ledger and fake_sdk.calls == []


def test_promoted_backup_keeps_usage_on_original_provider_ledger(tmp_path, fake_sdk):
    path, data, routed = configured_backup_budget_router(tmp_path, 20)
    class OldPrimaryAccount:
        def __init__(self):
            self.entries = []
        def check(self):
            return True
        def add(self, prompt, completion):
            self.entries.append((prompt, completion))
    old_primary = OldPrimaryAccount()
    routed._budget = old_primary
    ledger = routed._backup_budgets["backup"]
    fake_sdk.usage.prompt_tokens, fake_sdk.usage.completion_tokens = 2, 3
    save(path, {"backup": provider("fake-backup")})
    assert routed.chat("system", "promoted backup")
    assert old_primary.entries == []
    assert ledger.check() and ledger.last_total == 5
    assert routed._backup_budgets["backup"] is ledger


@pytest.mark.parametrize("field", ["model", "base_url", "protocol", "provider_name", "active_provider"])
def test_cross_provider_secret_metadata_blocks_initial_loader_and_constructor(tmp_path, fake_sdk, caplog, field):
    path = tmp_path / "config.json"
    secret = "fake-backup-private-long"
    providers = {"primary": provider(), "backup": provider(secret)}
    active = "primary"
    if field == "provider_name":
        providers[secret] = providers.pop("primary")
        active = secret
    elif field == "active_provider":
        active = secret
        providers[secret] = provider()
    else:
        providers["primary"][field] = "prefix-" + secret + "-suffix"
    save(path, providers, active)
    cfg = service.load_llm_config(str(path))
    assert not cfg.is_active
    routed = cfg.build_router(budget_dir=str(tmp_path))
    assert routed.chat("system", "unsafe metadata") is None
    direct = client_module.LLMRouter(providers, active, config_path=str(path))
    assert direct.chat("system", "unsafe direct constructor") is None
    held = client_module.LLMClient("fake-old-001", "https://old.invalid", "model-a",
                                   provider_name=active, config_path=str(path))
    assert held.chat("system", "unsafe held metadata") is None
    assert fake_sdk.calls == [] and fake_sdk.instances == [] and secret not in caplog.text


def test_later_metadata_cannot_contain_previously_known_removed_key(tmp_path, fake_sdk, caplog):
    path = tmp_path / "config.json"
    previous_key = "fake-previous-secret-long"
    routed = router(path, {"primary": provider(previous_key)})
    held = routed.active_client
    assert routed.chat("system", "before unsafe edit")
    count = len(fake_sdk.calls)
    save(path, {"primary": provider("fake-new-001", "prefix-" + previous_key)})
    assert routed.chat("system", "unsafe new metadata") is None
    assert held.probe() is False and len(fake_sdk.calls) == count
    assert previous_key not in caplog.text


def test_short_secret_metadata_rule_is_exact_match(tmp_path, fake_sdk, caplog):
    path = tmp_path / "config.json"
    save(path, {"primary": provider(model="tiny"), "backup": provider("tiny")})
    assert not service.load_llm_config(str(path)).is_active
    save(path, {"primary": provider(model="tiny-model"), "backup": provider("tiny")})
    cfg = service.load_llm_config(str(path))
    assert cfg.is_active
    assert cfg.build_router(budget_dir=str(tmp_path)).chat("system", "allowed metadata")


def test_secret_returned_by_fake_sdk_is_not_exposed_but_usage_is_kept(tmp_path, fake_sdk, caplog):
    path = tmp_path / "config.json"
    key = "fake-private-secret-long"
    records = []
    save(path, {"primary": provider(key)})
    routed = service.load_llm_config(str(path)).build_router(budget_dir=str(tmp_path), on_metrics=records.append)
    fake_sdk.content[0] = "upstream echoed " + key
    fake_sdk.usage.prompt_tokens, fake_sdk.usage.completion_tokens = 4, 2
    assert routed.chat("system", "do not return credential") is None
    assert records[-1]["error_type"] == "credential_in_response"
    assert records[-1]["prompt_tokens"] == 4 and records[-1]["completion_tokens"] == 2
    assert key not in caplog.text and key not in json.dumps(records)


@pytest.mark.parametrize("key", ["short", "secret", "seven77"])
def test_five_to_seven_character_secrets_block_metadata_substrings(tmp_path, fake_sdk, caplog, key):
    path = tmp_path / "config.json"
    save(path, {"primary": provider(model="prefix-" + key), "backup": provider(key)})
    assert not service.load_llm_config(str(path)).is_active
    assert key not in caplog.text and fake_sdk.instances == []


def test_url_metadata_checks_unquoted_short_secrets_as_substrings(tmp_path, fake_sdk, caplog):
    path = tmp_path / "config.json"
    save(path, {"primary": provider(base_url="https://example.invalid/%74iny/"),
                "backup": provider("tiny")})
    assert not service.load_llm_config(str(path)).is_active
    assert fake_sdk.instances == [] and "tiny" not in caplog.text


@pytest.mark.parametrize("legacy", ["provider_alias", "flat_api_key", "flat_alias"])
def test_legacy_secret_alias_is_redacted_but_never_activated(tmp_path, fake_sdk, caplog, legacy):
    path = tmp_path / "config.json"
    secret = "fake-legacy-private-long"
    raw = {"enabled": True, "active_provider": "primary", "providers": {"primary": provider(model=secret)}}
    if legacy == "provider_alias":
        raw["providers"]["legacy"] = {"key": secret, "model": "legacy", "base_url": "https://old.invalid"}
    else:
        raw["api_key" if legacy == "flat_api_key" else "key"] = secret
    path.write_text(json.dumps({"llm": raw}), encoding="utf-8")
    assert not service.load_llm_config(str(path)).is_active
    assert fake_sdk.instances == [] and secret not in caplog.text
    raw["providers"]["primary"]["model"] = "safe-model"
    path.write_text(json.dumps({"llm": raw}), encoding="utf-8")
    routed = service.load_llm_config(str(path)).build_router(budget_dir=str(tmp_path))
    assert routed.chat("system", "legacy alias stays inactive")
    assert "legacy" not in routed._clients
    assert all(call["key"] != secret for call in fake_sdk.calls)


def test_existing_empty_router_keeps_declared_primary_budget_identity(tmp_path, fake_sdk):
    path = tmp_path / "config.json"
    save(path, {"primary": provider("")})
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["llm"]["daily_token_budget"] = 1
    path.write_text(json.dumps(raw), encoding="utf-8")
    routed = service.load_llm_config(str(path)).build_router(budget_dir=str(tmp_path))
    assert not routed.is_available and routed._budget_primary_provider == "primary"
    save(path, {"primary": provider()})
    fake_sdk.usage.prompt_tokens = 1
    assert routed.chat("system", "existing object only, no worker created")
    assert routed.chat("system", "budget still capped") is None
    assert len(fake_sdk.calls) == 1


class Account:
    budget = 10
    last_total = 0

    def __init__(self, allowed=True):
        self.allowed, self.entries = allowed, []

    def check(self):
        return self.allowed

    def add(self, prompt, completion):
        self.entries.append((prompt, completion))


def test_budget_check_canonical_reload_reselects_client_and_account(tmp_path, fake_sdk):
    path = tmp_path / "config.json"
    providers = {"primary": provider(), "backup": provider("fake-backup")}
    routed = router(path, providers)
    backup = Account()
    routed._backup_budgets["backup"] = backup

    class ExhaustedAndSwitch(Account):
        def check(self):
            save(path, providers, primary="backup")
            assert routed.active_model == "model-a"  # performs the real reload
            return False

    primary = ExhaustedAndSwitch(False)
    routed._budget = primary
    fake_sdk.usage.prompt_tokens, fake_sdk.usage.completion_tokens = 3, 2
    assert routed.chat("system", "reselect after budget I/O")
    assert [call["key"] for call in fake_sdk.calls] == ["fake-backup"]
    assert primary.entries == [] and backup.entries == [(3, 2)]


def test_route_change_after_initial_budget_check_is_checked_before_sdk(tmp_path, fake_sdk, monkeypatch):
    path = tmp_path / "config.json"
    providers = {"primary": provider(), "backup": provider("fake-backup")}
    routed = router(path, providers)
    primary, backup = Account(), Account(False)
    routed._budget, routed._backup_budgets["backup"] = primary, backup
    original = routed._selected_call

    def change(plan, *args, **kwargs):
        save(path, providers, primary="backup")
        assert routed.active_model == "model-a"
        return original(plan, *args, **kwargs)

    monkeypatch.setattr(routed, "_selected_call", change)
    assert routed.chat("system", "changed just before SDK") is None
    assert fake_sdk.calls == [] and primary.entries == backup.entries == []


@pytest.mark.parametrize("selected", ["primary", "backup"])
def test_explicit_provider_budget_cannot_borrow_any_other_account(tmp_path, fake_sdk, selected):
    path = tmp_path / "config.json"
    save(path, {"primary": provider(), "backup": provider("fake-backup")})
    routed = service.load_llm_config(str(path)).build_router(
        budget_dir=str(tmp_path), provider_override=selected)
    primary, backup = Account(selected != "primary"), Account(selected != "backup")
    routed._budget, routed._backup_budgets["backup"] = primary, backup
    assert routed._budget_primary_provider == "primary"
    for _ in range(routed.failover_threshold + 1):
        assert routed.chat("system", "only the requested account") is None
    assert routed.active_client.provider_name == selected
    assert routed.active == selected and routed._other_provider() is None
    assert fake_sdk.calls == []


def test_trusted_override_wins_gate_provider_but_obeys_gate_stop(tmp_path, fake_sdk):
    path = tmp_path / "config.json"
    save(path, {"primary": provider(), "backup": provider("fake-backup")})
    routed = service.load_llm_config(str(path)).build_router(
        budget_dir=str(tmp_path), provider_override="backup", control_data_dir=str(tmp_path))
    gate = tmp_path / client_module.GATE_FILENAME
    gate.write_text(json.dumps({"enabled": True, "active_provider": "primary"}), encoding="utf-8")
    assert routed.chat("system", "explicit target")
    assert [call["key"] for call in fake_sdk.calls] == ["fake-backup"]
    gate.write_text(json.dumps({"enabled": False, "active_provider": "primary"}), encoding="utf-8")
    client_module.invalidate_gate_cache()
    assert routed.chat("system", "stopped") is None and routed.active == "backup"
    assert len(fake_sdk.calls) == 1


def test_explicit_override_failure_keeps_selected_client_between_calls(tmp_path, fake_sdk):
    path = tmp_path / "config.json"
    save(path, {"primary": provider(), "backup": provider("fake-backup")})
    routed = service.load_llm_config(str(path)).build_router(
        budget_dir=str(tmp_path), provider_override="backup")
    class Fatal(RuntimeError):
        status_code = 401
    for _ in range(routed.failover_threshold + 1):
        fake_sdk.hooks.append(lambda: (_ for _ in ()).throw(Fatal("synthetic")))
        assert routed.chat("system", "selected fails") is None
        assert routed.active_client.provider_name == "backup" and routed.active == "backup"
    assert all(call["key"] == "fake-backup" for call in fake_sdk.calls)


@pytest.mark.parametrize("replacement", ["", "fake-new-001"])
@pytest.mark.parametrize("during", ["http", "retry_sleep"])
@pytest.mark.parametrize("held_client", [False, True])
def test_each_unsent_retry_rechecks_credentials(tmp_path, fake_sdk, monkeypatch, replacement, during, held_client):
    path = tmp_path / "config.json"
    routed = router(path, {"primary": provider()})
    target = routed.active_client if held_client else routed
    class Transient(RuntimeError):
        status_code = 500
    def revoke():
        save(path, {"primary": provider(replacement)})
    def fail():
        if during == "http":
            revoke()
        raise Transient("synthetic transient failure")
    fake_sdk.hooks.append(fail)
    if during == "retry_sleep":
        monkeypatch.setattr(client_module.time, "sleep", lambda _: revoke())
    assert target.chat("system", "retry must be cancelled") is None
    assert [call["key"] for call in fake_sdk.calls] == ["fake-old-001"]
    if replacement:
        assert target.chat("system", "next logical request uses new key")
        assert [call["key"] for call in fake_sdk.calls] == ["fake-old-001", replacement]


@pytest.mark.parametrize("dispatch", ["chat", "borrow", "probe"])
def test_revoked_selected_client_cannot_send_first_http(tmp_path, fake_sdk, monkeypatch, dispatch):
    path = tmp_path / "config.json"
    providers = {"primary": provider(), "backup": provider("fake-backup")}
    routed = router(path, providers)
    routed.active_client
    if dispatch == "borrow":
        routed._budget = Account(False)
    elif dispatch == "probe":
        routed.active = "backup"
        routed.last_probe_ts = 0
    original = routed._selected_call
    def revoke(plan, *args, **kwargs):
        changed = {name: dict(value) for name, value in providers.items()}
        changed[plan["name"]]["api_key"] = ""
        save(path, changed)
        return original(plan, *args, **kwargs)
    monkeypatch.setattr(routed, "_selected_call", revoke)
    if dispatch == "probe":
        assert routed.probe_and_maybe_switch() is None
    else:
        assert routed.chat("system", "selected then revoked") is None
    assert fake_sdk.calls == []


def test_probe_selection_source_race_cancels_without_escaping(tmp_path, fake_sdk, monkeypatch):
    path = tmp_path / "config.json"
    providers = {"primary": provider(), "backup": provider("fake-backup")}
    routed = router(path, providers)
    routed.active_client
    routed.active, routed.last_probe_ts = "backup", 0
    original = routed._selection
    def changed(*args, **kwargs):
        if kwargs.get("expected") is not None:
            save(path, providers, primary="backup")
        return original(*args, **kwargs)
    monkeypatch.setattr(routed, "_selection", changed)
    assert routed.probe_and_maybe_switch() is None and fake_sdk.calls == []


def test_scrubbed_provider_metrics_still_charge_exact_selected_ledger(tmp_path, fake_sdk):
    path = tmp_path / "config.json"
    save(path, {"primary": provider(), "backup": provider("fake-backup")})
    records = []
    routed = service.load_llm_config(str(path)).build_router(
        budget_dir=str(tmp_path), on_metrics=records.append, provider_override="backup")
    assert routed._budget_primary_provider == "primary"
    primary, backup = Account(), Account()
    routed._budget, routed._backup_budgets["backup"] = primary, backup
    fake_sdk.usage.prompt_tokens, fake_sdk.usage.completion_tokens = 4, 3
    def secret_now_matches_public_name():
        save(path, {"primary": provider("backup"), "backup": provider("fake-backup")}, primary="backup")
        assert routed.active_model == ""  # metadata validation fails closed and remembers the key
    fake_sdk.hooks.append(secret_now_matches_public_name)
    assert routed.chat("system", "already issued backup finishes")
    assert records[-1]["provider"] == ""
    assert primary.entries == [] and backup.entries == [(4, 3)]


@pytest.mark.parametrize("target", ["a", "b"])
@pytest.mark.parametrize("replacement", ["", "synthetic-backup-new-key"])
def test_last_failed_borrow_http_rotation_cannot_restart_logical_call(tmp_path, fake_sdk, target, replacement):
    path = tmp_path / "config.json"
    providers = {"primary": provider(), "a": provider("synthetic-a-old-key"),
                 "b": provider("synthetic-b-old-key")}
    routed = router(path, providers)
    routed._budget = Account(False)
    accounts = {name: Account() for name in ("a", "b")}
    routed._backup_budgets = accounts
    old_key = providers[target]["api_key"]
    rotated = []
    class Transient(RuntimeError):
        status_code = 500
    def fail_old_keys():
        key = fake_sdk.calls[-1]["key"]
        if key not in ("synthetic-a-old-key", "synthetic-b-old-key"):
            return
        if key == old_key and sum(c["key"] == key for c in fake_sdk.calls) == client_module._MAX_ATTEMPTS:
            providers[target]["api_key"] = replacement
            save(path, providers)
            rotated.append(True)
        raise Transient("synthetic borrowed failure")
    fake_sdk.hooks.extend([fail_old_keys] * (client_module._MAX_ATTEMPTS * 4))
    assert routed.chat("system", "one logical task") is None
    expected = ["synthetic-a-old-key"] * client_module._MAX_ATTEMPTS
    if target == "b":
        expected += ["synthetic-b-old-key"] * client_module._MAX_ATTEMPTS
    assert rotated == [True] and [c["key"] for c in fake_sdk.calls] == expected
    if replacement:
        fake_sdk.usage.prompt_tokens, fake_sdk.usage.completion_tokens = 2, 1
        assert routed.chat("system", "new logical task may use new key")
        assert fake_sdk.calls[-1]["key"] == replacement
        assert accounts[target].entries == [(2, 1)]


def test_failed_borrow_without_config_change_can_continue_to_next_candidate(tmp_path, fake_sdk):
    path = tmp_path / "config.json"
    routed = router(path, {"primary": provider(), "a": provider("synthetic-a-old-key"),
                           "b": provider("synthetic-b-key")})
    routed._budget = Account(False)
    primary, a, b = routed._budget, Account(), Account()
    routed._backup_budgets = {"a": a, "b": b}
    class Transient(RuntimeError):
        status_code = 500
    def fail():
        raise Transient("synthetic unchanged failure")
    fake_sdk.hooks.extend([fail] * client_module._MAX_ATTEMPTS)
    fake_sdk.usage.prompt_tokens, fake_sdk.usage.completion_tokens = 2, 1
    assert routed.chat("system", "ordinary borrowing remains available")
    assert [c["key"] for c in fake_sdk.calls] == ["synthetic-a-old-key"] * client_module._MAX_ATTEMPTS + ["synthetic-b-key"]
    assert primary.entries == a.entries == [] and b.entries == [(2, 1)]


def test_earlier_borrow_http_keeps_issued_state_when_next_selection_changes(tmp_path, fake_sdk, monkeypatch):
    path = tmp_path / "config.json"
    providers = {"primary": provider(), "a": provider("synthetic-a-old-key"),
                 "b": provider("synthetic-b-key")}
    routed = router(path, providers)
    routed._budget = Account(False)
    class Transient(RuntimeError):
        status_code = 500
    def fail():
        raise Transient("synthetic borrowed failure")
    fake_sdk.hooks.extend([fail] * client_module._MAX_ATTEMPTS)
    original = routed._selection
    changed = []
    def selection(*args, **kwargs):
        if kwargs.get("provider") == "b" and not changed:
            providers["a"]["api_key"] = "synthetic-a-new-key"
            save(path, providers)
            changed.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(routed, "_selection", selection)
    assert routed.chat("system", "no restart after earlier candidate sent") is None
    assert changed == [True]
    assert [c["key"] for c in fake_sdk.calls] == ["synthetic-a-old-key"] * client_module._MAX_ATTEMPTS


def test_cancelled_final_guard_without_http_still_allows_bounded_reselection(tmp_path, fake_sdk, monkeypatch):
    path = tmp_path / "config.json"
    providers = {"primary": provider(), "backup": provider("synthetic-backup-key")}
    routed = router(path, providers)
    held = routed.active_client
    original = held._before_http_send
    def cancel_after_approval(guard, issued):
        original(guard, issued)
        save(path, providers, primary="backup")
        raise client_module._DispatchCancelled("route_changed", issued)
    monkeypatch.setattr(held, "_before_http_send", cancel_after_approval)
    assert routed.chat("system", "not yet sent may reselect")
    assert [c["key"] for c in fake_sdk.calls] == ["synthetic-backup-key"]


@pytest.mark.parametrize("no_send", ["empty", "unavailable", "final_guard"])
def test_dispatch_marker_requires_actual_sdk_admission(tmp_path, fake_sdk, monkeypatch, no_send):
    path = tmp_path / "config.json"
    routed = router(path, {"primary": provider()})
    plan = routed._selection()
    state = {"issued": False}
    if no_send == "unavailable":
        plan["client"]._client = None
        monkeypatch.setattr(plan["client"], "_initialize_sdk", lambda: None)
    if no_send == "final_guard":
        def cancel(*args):
            raise client_module._DispatchCancelled("route_changed")
        monkeypatch.setattr(plan["client"], "_before_http_send", cancel)
        with pytest.raises(client_module._DispatchCancelled):
            routed._selected_call(plan, "system", "synthetic", dispatch_state=state)
    else:
        assert routed._selected_call(plan, "" if no_send == "empty" else "system",
                                     "synthetic", dispatch_state=state) is None
    assert state == {"issued": False} and fake_sdk.calls == []


def test_logical_dispatch_state_is_isolated_between_concurrent_calls(tmp_path, fake_sdk):
    path = tmp_path / "config.json"
    providers = {"primary": provider(), "b": provider("synthetic-b-key"),
                 "c": provider("synthetic-c-key")}
    routed = router(path, providers)
    routed._budget = Account()
    entered, release = threading.Event(), threading.Event()
    results, errors = [], []
    def sending():
        entered.set()
        assert release.wait(3)
    fake_sdk.hooks.append(sending)
    def first_call():
        try:
            results.append(routed.chat("system", "first already issued"))
        except BaseException as error:
            errors.append(type(error).__name__)
    thread = threading.Thread(target=first_call, daemon=True)
    thread.start()
    assert entered.wait(2)
    save(path, providers, primary="b")
    class SwitchBeforeSecondSend(Account):
        def check(self):
            save(path, providers, primary="c")
            return True
    routed._backup_budgets = {"b": SwitchBeforeSecondSend(), "c": Account()}
    try:
        assert routed.chat("system", "second not yet issued may reselect")
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive() and errors == [] and results == ["synthetic reply"]
    assert [c["key"] for c in fake_sdk.calls] == ["fake-old-001", "synthetic-c-key"]
