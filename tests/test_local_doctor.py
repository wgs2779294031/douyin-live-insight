"""New task-11B1 fixtures only; native probe never handles business files."""
import copy
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import local_doctor as doctor
from scripts import local_installation as installation
from scripts import customer_bundle as bundle


@pytest.fixture
def installed(tmp_path, monkeypatch):
    assert "task-11B1" in str(tmp_path)
    target = tmp_path / "新 客户 空间"
    target.mkdir()
    (target / "new-generated-only.marker").write_text("task11B1 generated fixture", encoding="utf-8")
    state, app = target / "state", target / "app"
    state.mkdir()
    layout = dict(state=state, root=app, python=target / "runtime/python/python.exe",
        pythonw=target / "runtime/python/pythonw.exe", node=target / "runtime/node/node.exe",
        ffmpeg=target / "runtime/ffmpeg/ffmpeg.exe", model=target / "assets/model", dist=app / "web/dist",
        port=18765, cdp_port=19222, browser={"kind": "explicit", "path": str(target / "missing-browser.exe")},
        browser_executable=None)
    for path in [*(layout[name] for name in ("python", "pythonw", "node", "ffmpeg")),
                 layout["dist"] / "index.html", *(layout["model"] / name for name in
                    ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt"))]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic never executed or loaded")
    checked = dict(owner={"install_id": "c63c6fd8-52e0-4e5f-9fa4-6cd4c0473f4c", "version": "synthetic",
        "channel": "local-candidate", "bundle_manifest_sha256": "a" * 64}, manifest={}, layout=layout, integrity={})
    calls = []
    def inspect(path, deep=False):
        calls.append((path, deep))
        return checked
    monkeypatch.setattr(doctor, "_platform_fact", lambda: {"state": "supported_os_floor", "cpu_native_abi": "not_checked"})
    monkeypatch.setattr(doctor, "_vc_fact", lambda: {"state": "files_present_hint", "abi_verified": False})
    monkeypatch.setattr(doctor, "_port_fact", lambda _port: {"state": "bind_available_at_check"})
    return target, checked, inspect, calls


def test_uninstalled_never_inspects_or_creates_target(tmp_path):
    target = tmp_path / "never-created"
    def denied(*_args, **_kwargs):
        pytest.fail("missing target must not import or inspect installed code")
    result = doctor.doctor(target, inspect_install=denied)
    assert not result["ok"] and result["code"] == "not_installed" and result["exit"] == 2
    assert not target.exists()


def test_failed_owner_inspection_never_probes_state(installed, monkeypatch):
    target, _checked, _inspect, _calls = installed
    before = sorted(str(path.relative_to(target)) for path in target.rglob("*"))
    def invalid(*_args, **_kwargs):
        raise ValueError("synthetic PRIVATE-CREDENTIAL must not leak")
    monkeypatch.setattr(doctor, "_state_probe", lambda _state: pytest.fail("owner validation must precede writing"))
    result = doctor.doctor(target, inspect_install=invalid)
    assert result["code"] == "installation_unconfirmed" and not result["ok"]
    assert "PRIVATE-CREDENTIAL" not in json.dumps(result)
    assert sorted(str(path.relative_to(target)) for path in target.rglob("*")) == before


@pytest.mark.parametrize("value", [r"\\server\share", r"\\?\D:\target", r"D:\target:stream", r"D:\a\..\target"])
def test_invalid_target_is_rejected_before_inspection(value):
    result = doctor.doctor(value, inspect_install=lambda *_args, **_kwargs: pytest.fail("invalid target lookup"))
    assert result["code"] == "installation_unconfirmed" and not result["ok"]


def test_light_doctor_does_not_need_browser_or_optional_key(installed, monkeypatch):
    target, _checked, inspect, calls = installed
    monkeypatch.setattr(doctor, "_state_probe", lambda _state: {"state": "writable", "probe_removed": True})
    result = doctor.doctor(target, inspect_install=inspect)
    assert result["ok"] and result["code"] == "checks_passed_limited"
    assert calls == [(target, False)]
    assert result["facts"]["browser"]["state"] == "missing"
    assert result["facts"]["llm"] == {"required_for_base": False, "credentials_read": False}
    assert result["facts"]["vc_runtime"]["abi_verified"] is False
    assert "full_payload_hash" in result["not_checked"] and "native_imports" in result["not_checked"]
    assert set(result) == {"ok", "code", "facts", "not_checked", "next_steps", "exit"}


def test_deep_is_explicit_forwarded_hash_mode_not_native_loading(installed, monkeypatch):
    target, _checked, inspect, calls = installed
    monkeypatch.setattr(doctor, "_state_probe", lambda _state: {"state": "writable"})
    monkeypatch.setattr(doctor, "_import_results", lambda *_args: pytest.fail("deep hash must not load native modules"))
    result = doctor.doctor(target, deep=True, inspect_install=inspect)
    assert result["ok"] and calls == [(target, True)]
    assert result["facts"]["integrity"]["mode"] == "deep_hash"
    assert "full_payload_hash" not in result["not_checked"]
    assert doctor.doctor(target, deep="true", inspect_install=inspect)["code"] == "invalid_parameters"
    assert len(calls) == 1


def test_missing_state_never_creates_it(installed, monkeypatch):
    target, checked, inspect, _calls = installed
    checked["layout"]["state"].rmdir()  # only this fixture's empty generated state
    monkeypatch.setattr(doctor, "_kernel", lambda: pytest.fail("missing state must not invoke write API"))
    result = doctor.doctor(target, inspect_install=inspect)
    assert result["code"] == "state_unwritable" and result["facts"]["state"]["state"] == "missing"
    assert not checked["layout"]["state"].exists()


def test_bad_layout_cannot_direct_probe_to_foreign_state(installed, tmp_path, monkeypatch):
    target, checked, inspect, _calls = installed
    checked["layout"]["state"] = tmp_path / "foreign-state"
    monkeypatch.setattr(doctor, "_state_probe", lambda *_args: pytest.fail("foreign state must not be touched"))
    assert doctor.doctor(target, inspect_install=inspect)["code"] == "installation_unconfirmed"
    assert not checked["layout"]["state"].exists()


def test_unexpected_inspector_failure_stays_safe_json(installed):
    target, _checked, _inspect, _calls = installed
    def failed(*_args, **_kwargs):
        raise RuntimeError("synthetic-private-value")
    result = doctor.doctor(target, inspect_install=failed)
    assert result["code"] == "doctor_unconfirmed" and not result["ok"]
    assert "synthetic-private-value" not in json.dumps(result)


@pytest.mark.skipif(os.name != "nt", reason="actual Windows owned DELETE-handle micro probe")
def test_native_probe_removes_only_its_new_leaf(installed):
    _target, checked, _inspect, _calls = installed
    state = checked["layout"]["state"]
    sentinel = state / "fixture-existing.txt"
    sentinel.write_bytes(b"owned fixture must remain byte-identical")
    before = sentinel.read_bytes()
    result = doctor._state_probe(state)
    assert result == {"state": "writable", "write_fsync_readback": True, "probe_removed": True}
    assert sentinel.read_bytes() == before and list(state.iterdir()) == [sentinel]


@pytest.mark.skipif(os.name != "nt", reason="actual Windows probe retains unconfirmed own leaf")
def test_native_probe_identity_rejection_retains_generated_leaf(installed, monkeypatch):
    _target, checked, _inspect, _calls = installed
    original = doctor._final_path
    def changed(api, handle):
        value = original(api, handle)
        return value + "-different" if value.endswith(".tmp") else value
    monkeypatch.setattr(doctor, "_final_path", changed)
    result = doctor._state_probe(checked["layout"]["state"])
    assert result["state"] == "unconfirmed" and result["probe_created"]
    retained = checked["layout"]["state"] / result["probe_name"]
    assert retained.is_file() and retained.read_bytes() == b""


@pytest.mark.skipif(os.name != "nt", reason="actual Windows probe interrupted after mark")
def test_native_probe_may_mark_cancels_on_interruption(installed, monkeypatch):
    _target, checked, _inspect, _calls = installed
    native = doctor._kernel()
    actions = []
    interruption = SystemExit("synthetic owned probe interruption")
    class Api:
        def __getattr__(self, name): return getattr(native, name)
        def SetFileInformationByHandle(self, handle, kind, pointer, size):
            flag = ctypes.cast(pointer, ctypes.POINTER(doctor._Disposition)).contents.DeleteFile
            actions.append(flag)
            result = native.SetFileInformationByHandle(handle, kind, pointer, size)
            if flag and result:
                raise interruption
            return result
    monkeypatch.setattr(doctor, "_kernel", lambda: Api())
    with pytest.raises(SystemExit) as caught:
        doctor._state_probe(checked["layout"]["state"])
    assert caught.value is interruption and actions == [1, 0]
    leaves = list(checked["layout"]["state"].glob(".doctor-*.tmp"))
    assert len(leaves) == 1 and leaves[0].read_bytes() == b"customer doctor owned write probe\n"


def test_gated_import_action_uses_only_whitelist_and_hides_chatter(installed, monkeypatch, capfd):
    target, checked, _inspect, _calls = installed
    layout = dict(checked["layout"], install=target)
    monkeypatch.setattr(installation, "bootstrap_installed", lambda: layout)
    monkeypatch.setattr(doctor, "__file__", str(layout["root"] / "scripts/local_doctor.py"))
    seen = []
    def fake_import(name):
        seen.append(name)
        os.write(1, b"synthetic native stdout chatter\n")
        print("synthetic buffered Python stdout chatter", end="")
        if name == "av":
            raise ImportError("synthetic-private-import-path")
        return object()
    result = doctor.doctor_imports(target, importer=fake_import)
    output = capfd.readouterr()
    assert output.out == "" and output.err == ""
    assert seen == list(doctor.NATIVE_IMPORTS) and len(seen) == 16
    assert result["code"] == "native_imports_failed" and not result["ok"]
    assert "synthetic-private-import-path" not in json.dumps(result)
    assert result["facts"]["model_loaded"] is False and result["facts"]["paid_api_called"] is False


def test_cli_default_and_explicit_doctor_emit_one_json(installed, monkeypatch, capsys):
    target, _checked, _inspect, _calls = installed
    result = doctor._result(True, "synthetic_checked")
    seen = []
    def fake_doctor(value, *, deep=False):
        seen.append((value, deep))
        return result
    monkeypatch.setattr(doctor, "doctor", fake_doctor)
    for args in (("--target", str(target)), ("doctor", "--target", str(target), "--deep")):
        assert doctor.main(args) == 0
        assert json.loads(capsys.readouterr().out) == result
    assert seen == [(str(target), False), (str(target), True)]


@pytest.mark.skipif(os.name != "nt", reason="actual Windows failed cancellation is explicitly unknown")
def test_native_probe_cancel_failure_never_claims_retained_or_removed(installed, monkeypatch):
    _target, checked, _inspect, _calls = installed
    native = doctor._kernel()
    actions = []
    class Api:
        def __getattr__(self, name): return getattr(native, name)
        def SetFileInformationByHandle(self, handle, kind, pointer, size):
            flag = ctypes.cast(pointer, ctypes.POINTER(doctor._Disposition)).contents.DeleteFile
            actions.append(flag)
            if flag:
                assert native.SetFileInformationByHandle(handle, kind, pointer, size)
            return 0  # TRUE may have acted; FALSE is deliberately unconfirmed.
    monkeypatch.setattr(doctor, "_kernel", lambda: Api())
    result = doctor._state_probe(checked["layout"]["state"])
    assert result["state"] == "unconfirmed" and result["reason"] == "state_probe_cleanup_unconfirmed"
    assert result["probe_removed"] is None and result["cleanup_state"] == "unconfirmed"
    assert "unknown_leaf_preserved" not in result and actions == [1, 0]
    assert not (checked["layout"]["state"] / result["probe_name"]).exists()


@pytest.mark.skipif(os.name != "nt", reason="actual Windows failed fsync retains only generated probe")
def test_native_probe_fsync_failure_is_not_writable(installed, monkeypatch):
    _target, checked, _inspect, _calls = installed
    def denied(_fd): raise OSError("synthetic fsync failure")
    monkeypatch.setattr(doctor.os, "fsync", denied)
    result = doctor._state_probe(checked["layout"]["state"])
    assert result["state"] == "unconfirmed" and result["probe_removed"] is None
    assert (checked["layout"]["state"] / result["probe_name"]).read_bytes() == b"customer doctor owned write probe\n"


def test_missing_private_components_do_not_fall_back_to_development_path(installed, monkeypatch):
    target, checked, inspect, _calls = installed
    for name in ("node", "ffmpeg"):
        path = checked["layout"][name]
        path.rename(path.with_suffix(".preserved"))
    model = checked["layout"]["model"] / "model.bin"
    model.rename(model.with_suffix(".preserved"))
    monkeypatch.setenv("PATH", str(target / "invented-development-tools"))
    monkeypatch.setattr(doctor, "_state_probe", lambda _state: {"state": "writable"})
    monkeypatch.setattr(doctor, "_vc_fact", lambda: {"state": "missing", "abi_verified": False})
    result = doctor.doctor(target, inspect_install=inspect)
    assert result["ok"] and result["code"] == "checks_passed_limited"
    assert all(result["facts"]["components"][name] is False for name in ("node", "ffmpeg", "model_files"))
    assert result["facts"]["components"]["board_dist"] is True
    assert result["facts"]["vc_runtime"] == {"state": "missing", "abi_verified": False}


@pytest.fixture
def quick_target(tmp_path, monkeypatch):
    """Construct tiny owned facts directly: no installer or executable is run."""
    if os.name != "nt":
        pytest.skip("real Windows directory/FileId guards")
    assert "task-11B1" in str(tmp_path)
    (tmp_path / "quick-generated-only.marker").write_bytes(b"new synthetic metadata only")
    target = tmp_path / "quick-target"
    target.mkdir()
    install_id = "c63c6fd8-52e0-4e5f-9fa4-6cd4c0473f4c"
    cache = tmp_path / (".quick-target.manager-" + install_id)
    descriptor = dict(installation._RELATIVE, schema=1, kind="customer_installation",
        browser={"kind": "explicit", "path": str(tmp_path / "missing-edge.exe")},
        dll_directories=["runtime/python"], ports={"board": 18765, "cdp": 19222})
    target_files = {name: ("synthetic never execute: " + name).encode() for name in bundle.DOCTOR_KEY_FILES}
    target_files[bundle.DOCTOR_TEMPLATE] = bundle.encode(descriptor)
    target_files["installation.json"] = bundle.encode(descriptor)
    target_files["payload-manifest.json"] = b"{}\n"
    target_files["notices/LICENSE.txt"] = b"synthetic notice a\n"
    for number in range(12):
        target_files[f"app/extra-blocks/row-{number}.txt"] = b"listed inventory outside quick scope\n"
    manager_files = {name: b"synthetic cache never execute" for name in (
        "manager/python/python.exe", "manager/python/python314._pth", "manager/code/customer_bundle.py",
        "manager/code/customer_install.py", "manager/code/local_doctor.py")}
    manager_files["manager/python/python314._pth"] = b"python314.zip\n.\n../code\n"
    entries = {}
    for name, raw in target_files.items():
        entries[name if name.startswith("entry/") else "payload/" + name] = (raw, "entry" if name.startswith("entry/") else "product")
    for name, raw in manager_files.items():
        entries[name] = (raw, "manager")
    entries["AGENT_INSTALL.md"] = (b"synthetic owned guide\n", "guide")
    entries["CustomerSetup.exe"] = (target_files["entry/CustomerLauncher.exe"], "bootstrap")
    directories = sorted({parent.as_posix() for name in entries for parent in Path(name).parents if parent.as_posix() != "."})
    rows = [dict(path=name, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(), component=component)
            for name, (raw, component) in sorted(entries.items())]
    manifest = dict(schema=1, kind="customer_bundle", version="0.1.0-synthetic", channel="local-candidate",
                    state_compatibility=1, files=rows, directories=directories)
    manifest_raw = bundle.encode(manifest)
    digest = hashlib.sha256(manifest_raw).hexdigest()
    def write(path, raw):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    for name, raw in target_files.items():
        write(target / name, raw)
    write(target / "AGENT_INSTALL.md", entries["AGENT_INSTALL.md"][0])
    for name, raw in manager_files.items():
        write(cache / name, raw)
    (target / "state/data").mkdir(parents=True)
    (target / ".gate").mkdir()
    for name in ("admission.lock", "code.lease", "maintenance.json", "spawn-ledger.json"):
        write(target / ".gate" / name, b"synthetic mutable gate fact\n")
    with bundle.guard_path(target) as identity:
        owner = dict(schema=1, kind="customer_installation_owner", install_id=install_id,
            target=str(target), target_identity=identity, version=manifest["version"], channel=manifest["channel"],
            bundle_manifest_sha256=digest, manager_cache=str(cache), manager_manifest_sha256=digest)
    intent = dict(schema=1, kind="customer_first_install_intent", phase="installed", install_id=install_id,
        target=str(target), target_identity=identity, stage_identity=identity, bundle_manifest_sha256=digest, code=None)
    intent_path = target.parent / ".quick-target.install-intent.json"
    write(target / "owner.json", bundle.encode(owner))
    write(target / "installed.manifest.json", manifest_raw)
    write(intent_path, bundle.encode(intent))
    write(cache / "bundle.manifest.json", manifest_raw)
    write(cache / "cache-owner.json", bundle.encode(dict(schema=1, kind="customer_manager_cache_owner",
        install_id=install_id, target=str(target), target_identity=identity, bundle_manifest_sha256=digest)))
    monkeypatch.setitem(sys.modules, "customer_bundle", bundle)
    monkeypatch.setattr(doctor, "_platform_fact", lambda: {"state": "supported_os_floor"})
    monkeypatch.setattr(doctor, "_vc_fact", lambda: {"state": "files_present_hint", "abi_verified": False})
    monkeypatch.setattr(doctor, "_port_fact", lambda _port: {"state": "bind_available_at_check"})
    monkeypatch.setattr(doctor, "_state_probe", lambda _state: {"state": "writable", "probe_removed": True})
    return dict(target=target, cache=cache, owner=owner, intent=intent_path, manifest=manifest,
                manager_files=manager_files, target_files=target_files)


def test_quick_target_inventory_is_never_scanned_and_counts_are_partial(quick_target, monkeypatch):
    target, cache = quick_target["target"], quick_target["cache"]
    (target / "unlisted-extra.txt").write_bytes(b"new owned extra deliberately outside quick scope")
    original_scan, original_check = bundle.scan_tree, bundle.check_file
    checked = []
    def scan(path, **kwargs):
        assert Path(path) == cache, "quick must never enumerate target inventory"
        return original_scan(path, **kwargs)
    def check(path, row, *, deep):
        checked.append((Path(path), deep))
        return original_check(path, row, deep=deep)
    monkeypatch.setattr(bundle, "scan_tree", scan)
    monkeypatch.setattr(bundle, "check_file", check)
    result = bundle.inspect_install_quick(target)
    facts = result["integrity"]
    assert facts["scope"] == "doctor_key_metadata_and_complete_cache_hashes"
    assert facts["target_inventory_checked"] is False and facts["target_hashes_checked"] is False
    assert facts["cache_inventory_checked"] is True and facts["cache_hashes_checked"] is True
    assert facts["template_hash_checked"] is True
    assert facts["target_files_checked"] == len(bundle.DOCTOR_KEY_FILES)
    assert facts["cache_files_checked"] == len(quick_target["manager_files"])
    assert facts["files_checked"] == len(bundle.DOCTOR_KEY_FILES) + len(quick_target["manager_files"])
    assert facts["files_checked"] < len(quick_target["manifest"]["files"])
    assert all(deep for path, deep in checked if path.is_relative_to(cache))
    assert all(not deep for path, deep in checked if path.is_relative_to(target))
    assert len([path for path, _deep in checked if path.is_relative_to(target)]) == len(bundle.DOCTOR_KEY_FILES) - 1


@pytest.mark.parametrize("deep", [False, True])
def test_quick_does_not_weaken_default_or_deep_unknown_inventory_rejection(quick_target, deep):
    target = quick_target["target"]
    (target / "app/unlisted-owned-extra.txt").write_bytes(b"new unlisted synthetic")
    assert bundle.inspect_install_quick(target)["integrity"]["target_inventory_checked"] is False
    with pytest.raises(bundle.BundleError, match="installation_unlisted_path"):
        bundle.inspect_install(target, deep=deep)


def test_quick_is_metadata_only_but_deep_rejects_same_size_target_hash_mismatch(quick_target):
    target = quick_target["target"]
    path = target / "notices/LICENSE.txt"
    before = path.read_bytes()
    path.write_bytes(before.replace(b"notice a", b"notice b"))
    assert path.stat().st_size == len(before)
    assert bundle.inspect_install_quick(target)["integrity"]["target_hashes_checked"] is False
    with pytest.raises(bundle.BundleError, match="bundle_integrity_mismatch"):
        bundle.inspect_install(target, deep=True)


def test_quick_still_hashes_every_cache_file(quick_target):
    path = quick_target["cache"] / "manager/code/customer_install.py"
    raw = path.read_bytes()
    path.write_bytes(b"X" * len(raw))
    with pytest.raises(bundle.BundleError, match="bundle_integrity_mismatch"):
        bundle.inspect_install_quick(quick_target["target"])


@pytest.mark.parametrize("fact", ["owner", "intent", "cache_owner", "template"])
def test_quick_invalid_authority_cannot_authorize_doctor_probe(quick_target, monkeypatch, fact):
    target, cache = quick_target["target"], quick_target["cache"]
    paths = dict(owner=target / "owner.json", intent=quick_target["intent"],
        cache_owner=cache / "cache-owner.json", template=target / bundle.DOCTOR_TEMPLATE)
    path = paths[fact]
    value = json.loads(path.read_bytes())
    if fact == "template":
        before = path.read_bytes()
        value["ports"]["board"] += 1  # Same size, and descriptor comparison ignores template ports.
        raw = bundle.encode(value)
        assert len(raw) == len(before)
    elif fact == "owner":
        value["target_identity"]["file_id"] = "0" * 32
        raw = bundle.encode(value)
    elif fact == "intent":
        value["phase"] = "ready"
        raw = bundle.encode(value)
    else:
        value["install_id"] = "00000000-0000-4000-8000-000000000000"
        raw = bundle.encode(value)
    path.write_bytes(raw)
    monkeypatch.setattr(doctor, "_state_probe", lambda *_args: pytest.fail("invalid authority reached write probe"))
    result = doctor.doctor(target)
    assert not result["ok"] and result["code"] == "installation_unconfirmed"
    assert not list((target / "state").glob(".doctor-*"))


@pytest.mark.parametrize("name,component", [
    ("entry/CustomerLauncher.exe", "launcher"), ("runtime/python/pythonw.exe", "pythonw"),
    ("runtime/node/node.exe", "node"), ("runtime/ffmpeg/ffmpeg.exe", "ffmpeg"),
    ("assets/models/faster-whisper-small/model.bin", "model_files")])
def test_quick_missing_key_component_rejected_before_probe(quick_target, monkeypatch, name, component):
    target = quick_target["target"]
    path = target / name
    path.rename(path.with_name(path.name + ".retained"))
    monkeypatch.setattr(doctor, "_state_probe", lambda *_args: pytest.fail("missing component reached write probe"))
    result = doctor.doctor(target)
    assert not result["ok"] and result["code"] == "installation_unconfirmed"
    assert result["facts"]["components"] == {component: False}
    assert result["facts"]["component_issue"] == {"component": component, "code": "installation_component_missing"}
    assert result["facts"]["integrity"] == {"mode": "quick_component_diagnostic", "completed": False}
    assert "Do not fall back to PATH" in result["next_steps"][0]


def test_quick_permission_failure_is_not_laundered_to_missing(quick_target, monkeypatch):
    target = quick_target["target"]
    original = bundle.check_file
    def denied(path, row, *, deep):
        if path == target / "runtime/node/node.exe":
            raise PermissionError("synthetic private path must not leak")
        return original(path, row, deep=deep)
    monkeypatch.setattr(bundle, "check_file", denied)
    monkeypatch.setattr(doctor, "_state_probe", lambda *_args: pytest.fail("permission failure reached probe"))
    result = doctor.doctor(target)
    assert not result["ok"] and result["code"] == "installation_unconfirmed"
    assert "component_issue" not in result["facts"]
    assert "synthetic private path" not in json.dumps(result)


def test_quick_doctor_default_route_scope_and_deep_route_remain_distinct(quick_target, monkeypatch):
    target = quick_target["target"]
    quick, full = bundle.inspect_install_quick, bundle.inspect_install
    calls = []
    def quick_spy(path):
        calls.append(("quick", path))
        return quick(path)
    def full_spy(path, deep=False):
        calls.append(("full", path, deep))
        return full(path, deep=deep)
    monkeypatch.setattr(bundle, "inspect_install_quick", quick_spy)
    monkeypatch.setattr(bundle, "inspect_install", full_spy)
    ordinary = doctor.doctor(target)
    assert ordinary["ok"] and calls == [("quick", target)]
    assert ordinary["facts"]["integrity"]["mode"] == "quick_metadata"
    assert ordinary["facts"]["integrity"]["target_inventory_checked"] is False
    assert "full_target_inventory" in ordinary["not_checked"] and "full_payload_hash" in ordinary["not_checked"]
    deep = doctor.doctor(target, deep=True)
    assert deep["ok"] and calls[-1] == ("full", target, True)
    assert deep["facts"]["integrity"]["mode"] == "deep_hash"
    assert "full_target_inventory" not in deep["not_checked"] and "full_payload_hash" not in deep["not_checked"]


def test_quick_uninstalled_still_creates_nothing(tmp_path, monkeypatch):
    target = tmp_path / "never-created-quick"
    monkeypatch.setattr(bundle, "inspect_install_quick", lambda *_args: pytest.fail("missing target lookup"))
    monkeypatch.setattr(doctor, "_state_probe", lambda *_args: pytest.fail("missing target probe"))
    assert doctor.doctor(target)["code"] == "not_installed"
    assert not target.exists()


REV1_CLI_PROBE = r'''
import errno,json,os,sys
from pathlib import Path
from types import ModuleType
case,source,own=sys.argv[1],Path(sys.argv[2]),Path(sys.argv[3])
sys.path.insert(0,str(source))
from scripts import local_doctor as doctor
target=own/'never-created-target'
assert not target.exists()
bootstrap=ModuleType('scripts.local_installation')
bootstrap.bootstrap_installed=lambda: None if case=='bootstrap_invalid' else {'install':target,'root':source}
sys.modules['scripts.local_installation']=bootstrap
held_out,held_err=sys.stdout,sys.stderr
for stream in (held_out,held_err):stream.reconfigure(line_buffering=False,write_through=False)
state={'started':False,'out_flushes':0,'err_flushes':0,'dup_calls':0,'writes':0}
seen,duplicates,inheritable=[] ,[],[]
real_dup,real_write=doctor.os.dup,doctor.os.write
def duplicate(fd):
 state['dup_calls']+=1
 if case=='fd_error' and state['dup_calls']==2:raise OSError('GENERATED_PRIVATE_DUP_ERROR')
 result=real_dup(fd);duplicates.append(result);inheritable.append(os.get_inheritable(result));return result
doctor.os.dup=duplicate
class Proxy:
 def __init__(self,stream,label,fail=False):self.stream,self.label,self.fail=stream,label,fail
 def write(self,value):return self.stream.write(value)
 def flush(self):
  state[self.label+'_flushes']+=1
  if self.fail and state['started']:raise OSError('GENERATED_PRIVATE_FLUSH_ERROR')
  return self.stream.flush()
 def __getattr__(self,name):return getattr(self.stream,name)
if case=='none_success':sys.stdout=sys.stderr=None
if case=='flush_error':
 sys.stdout=Proxy(held_out,'out',True);sys.stderr=Proxy(held_err,'err')
def importer(name):
 assert isinstance(name,str)
 seen.append(name)
 if name==doctor.NATIVE_IMPORTS[0]:
  state['started']=True
  if case in ('ordinary_success','mixed_success','none_success','flush_error'):print('GENERATED_PRIVATE_PRINT',end='')
  if case in ('cached_stdout_failure','mixed_success','none_success','flush_error'):held_out.write('GENERATED_PRIVATE_CACHED_OUT')
  if case in ('cached_stderr_failure','mixed_success','none_success','flush_error'):held_err.write('GENERATED_PRIVATE_CACHED_ERR')
  if case in ('mixed_success','none_success','flush_error'):
   os.write(1,b'GENERATED_PRIVATE_FD1');os.write(2,b'GENERATED_PRIVATE_FD2')
 if name=='av' and case in ('cached_stdout_failure','cached_stderr_failure'):
  raise ImportError('GENERATED_PRIVATE_IMPORT_ERROR')
 return object()
original=doctor.doctor_imports
doctor.doctor_imports=lambda value:original(value,importer=importer)
action='doctor-imports'
if case=='short_write_unicode':
 action='doctor'
 doctor.doctor=lambda *_args,**_kwargs:doctor._result(True,'synthetic_protocol',{'text':'中文🐟'})
 def short_write(fd,data):
  state['writes']+=1
  return real_write(fd,memoryview(data)[:3])
 doctor.os.write=short_write
exit_code=doctor._cli_main([action,'--target',str(target)])
closed=[]
for fd in duplicates:
 try:os.fstat(fd)
 except OSError as error:closed.append(error.errno==errno.EBADF)
 else:closed.append(False)
record={'case':case,'cli_return_exit':exit_code,'seen':seen,'state':state,'all_private_duplicates_closed':all(closed),
 'protocol_duplicate_noninheritable':bool(inheritable) and not inheritable[0],
 'target_never_created':not target.exists(),
 'scope':'Production private CLI function with fake bootstrap/importer; real OS fd, std TextIOWrapper buffering and controlled fault proxy. No installed target/native modules.'}
(own/'probe-record.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
assert all(closed) and record['protocol_duplicate_noninheritable'] and record['target_never_created']
if case=='flush_error':assert state['err_flushes']>=2
raise SystemExit(exit_code)
'''


@pytest.mark.parametrize("case,code,exit_code", [
    ("ordinary_success", "native_imports_passed", 0),
    ("cached_stdout_failure", "native_imports_failed", 1),
    ("cached_stderr_failure", "native_imports_failed", 1),
    ("mixed_success", "native_imports_passed", 0),
    ("none_success", "native_imports_passed", 0),
    ("flush_error", "native_imports_unconfirmed", 1),
    ("fd_error", "native_imports_unconfirmed", 1),
    ("bootstrap_invalid", "installation_unconfirmed", 78),
    ("short_write_unicode", "synthetic_protocol", 0),
])
def test_rev1_cli_stdio_single_json_and_real_exit(tmp_path, case, code, exit_code):
    task = ROOT.parent / "task-11B1"
    assert tmp_path.resolve().is_relative_to(task.resolve())
    own = tmp_path / case
    own.mkdir()
    (own / "generated-only.marker").write_bytes(b"revision1 synthetic CLI only")
    probe = own / "probe.py"
    probe.write_text(REV1_CLI_PROBE, encoding="utf-8")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", PYTHONIOENCODING="utf8")
    env.pop("DY_CUSTOMER_CONTEXT", None)
    command = [sys.executable, "-B", "-X", "utf8", str(probe), case, str(ROOT), str(own)]
    process = subprocess.Popen(command, cwd=own, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()  # Only this test's held short child; fake importer cannot spawn.
        stdout, stderr = process.communicate(timeout=5)
    (own / "stdout.raw").write_bytes(stdout)
    (own / "stderr.raw").write_bytes(stderr)
    value = None
    try: value = json.loads(stdout)
    except (ValueError, UnicodeError): pass
    record = dict(case=case, command=command, pid=process.pid, returncode=process.returncode,
        waited=True, timed_out=timed_out, parsed_single_json=isinstance(value, dict),
        actual_cli_exit_matches_payload=isinstance(value, dict) and value.get("exit")==process.returncode,
        stdout_sha256=hashlib.sha256(stdout).hexdigest(), stderr_sha256=hashlib.sha256(stderr).hexdigest(),
        scope="Private production CLI function path; fake bootstrap/importer, real standard fds and buffer; not installed/native import validation.")
    (own / "parent-record.json").write_text(json.dumps(record,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    index = os.environ.get("DOCTOR_STDIO_EVIDENCE_INDEX")
    if index:
        index = Path(index)
        assert index.resolve().is_relative_to((task / "revision-1/doctor").resolve())
        with index.open("a",encoding="utf-8") as stream:
            stream.write(json.dumps({"case":case,"files":[str(own/name) for name in (
                "probe.py","stdout.raw","stderr.raw","parent-record.json","probe-record.json")]},ensure_ascii=False)+"\n")
    assert not timed_out and process.returncode == exit_code
    assert isinstance(value,dict) and set(value)=={"ok","code","facts","not_checked","next_steps","exit"}
    assert value["code"]==code and value["exit"]==process.returncode
    assert b"GENERATED_PRIVATE" not in stdout and stderr==b""
    child = json.loads((own/"probe-record.json").read_text(encoding="utf-8"))
    assert child["all_private_duplicates_closed"] and child["protocol_duplicate_noninheritable"]
    assert child["target_never_created"] and not (own/"never-created-target").exists()
    if case=="short_write_unicode":
        assert value["facts"]["text"]=="中文🐟" and child["state"]["writes"]>1
    elif case in {"bootstrap_invalid","fd_error"}:
        assert child["seen"]==[]
    else:
        assert child["seen"]==list(doctor.NATIVE_IMPORTS)


def test_rev1_flush_attempts_all_distinct_original_wrappers():
    calls=[]
    class Stream:
        def __init__(self,label,fail=False):self.label,self.fail=label,fail
        def flush(self):
            calls.append(self.label)
            if self.fail:raise OSError("generated safe flush error")
    first,second=Stream("stdout",True),Stream("stderr")
    assert doctor._flush_streams((first,None,second,first)) is False
    assert calls==["stdout","stderr"]


def test_rev1_function_api_none_streams_returns_result(installed, monkeypatch):
    target,checked,_inspect,_calls=installed
    layout=dict(checked["layout"],install=target)
    monkeypatch.setattr(installation,"bootstrap_installed",lambda:layout)
    monkeypatch.setattr(doctor,"__file__",str(layout["root"] / "scripts/local_doctor.py"))
    monkeypatch.setattr(sys,"stdout",None)
    monkeypatch.setattr(sys,"stderr",None)
    value=doctor.doctor_imports(target,importer=lambda _name:object())
    assert value["ok"] and value["code"]=="native_imports_passed" and value["exit"]==0
    assert set(value)=={"ok","code","facts","not_checked","next_steps","exit"}


@pytest.mark.parametrize("case,arguments,exit_code", [
    ("help", ["--help"], 0),
    ("invalid", ["doctor", "--target", "never-created-relative", "--invalid-generated-flag"], 2),
])
def test_rev1_cli_help_and_invalid_arguments_remain_visible(tmp_path, case, arguments, exit_code):
    task = ROOT.parent / "task-11B1"
    assert tmp_path.resolve().is_relative_to(task.resolve())
    command = [sys.executable,"-B","-X","utf8",str(ROOT/"scripts/local_doctor.py"),*arguments]
    env = dict(os.environ,PYTHONDONTWRITEBYTECODE="1")
    env.pop("DY_CUSTOMER_CONTEXT",None)
    result = subprocess.run(command,cwd=tmp_path,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10)
    (tmp_path/"stdout.raw").write_bytes(result.stdout)
    (tmp_path/"stderr.raw").write_bytes(result.stderr)
    record = dict(case=case,command=command,returncode=result.returncode,waited=True,
        scope="Actual source CLI argument parser only; no bootstrap, target or importer reached.",
        stdout_sha256=hashlib.sha256(result.stdout).hexdigest(),stderr_sha256=hashlib.sha256(result.stderr).hexdigest())
    (tmp_path/"parent-record.json").write_text(json.dumps(record,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    index=os.environ.get("DOCTOR_STDIO_EVIDENCE_INDEX")
    if index:
        index=Path(index)
        assert index.resolve().is_relative_to((task/"revision-1/doctor").resolve())
        with index.open("a",encoding="utf-8") as stream:
            stream.write(json.dumps({"case":case,"files":[str(tmp_path/name) for name in (
                "stdout.raw","stderr.raw","parent-record.json")]},ensure_ascii=False)+"\n")
    assert result.returncode==exit_code
    if case=="help":
        assert b"usage:" in result.stdout and b"doctor-imports" in result.stdout and result.stderr==b""
    else:
        assert result.stdout==b"" and b"unrecognized arguments" in result.stderr
    assert not (tmp_path/"never-created-relative").exists()
