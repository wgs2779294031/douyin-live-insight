"""Only new task-11A synthetic paths and fake DLL handles; no product launch."""
import copy
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from scripts import local_installation as installation
from scripts.local_autostart import instance_id


@pytest.fixture
def installed(tmp_path, monkeypatch):
    assert "task-11A" in str(tmp_path)
    app = tmp_path / "新 安装" / "app"
    app.mkdir(parents=True)
    (app.parent / "new-generated-only.marker").write_text("task11A synthetic", encoding="utf-8")
    executable = app.parent / "runtime/python/python.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"synthetic, never executed")
    executable.with_name("pythonw.exe").write_bytes(b"synthetic, never executed")
    browser = tmp_path / "own-browser.exe"
    browser.write_bytes(b"synthetic, never executed")
    dll = executable.parent / "Lib/site-packages/pywin32_system32"
    dll.mkdir(parents=True)
    value = dict(installation._RELATIVE, schema=1, kind="customer_installation",
        dll_directories=["runtime/python/Lib/site-packages/pywin32_system32"],
        ports={"board": 8765, "cdp": 29222}, browser={"kind": "explicit", "path": str(browser)})
    descriptor = app.parent / "installation.json"
    def write(data=None):
        descriptor.write_text(json.dumps(value if data is None else data), encoding="utf-8")
    write()
    monkeypatch.setattr(installation, "_APP_ROOT", app)
    monkeypatch.setattr(installation, "_ACTIVE", None)
    monkeypatch.setattr(installation, "_DLL_HANDLES", [])
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(sys, "dont_write_bytecode", sys.dont_write_bytecode)
    monkeypatch.setattr(os, "environ", {"SystemRoot": os.environ.get("SystemRoot", "C:/Windows"),
        "TEMP": str(tmp_path), "TMP": str(tmp_path)})
    handles = []
    class Handle:
        closed = False
        def close(self):
            self.closed = True
    def add(directory):
        handle = Handle()
        handles.append((directory, handle))
        return handle
    monkeypatch.setattr(os, "add_dll_directory", add, raising=False)
    return app, value, write, handles


def context_for(layout):
    paths = {name: str(layout[name]) for name in installation._CONTEXT_PATHS}
    return dict(paths, schema=1, instance_id=instance_id(paths), port=layout["port"], cdp_port=layout["cdp_port"])


def put_context(layout, value):
    path = layout["context_file"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_resolve_and_bootstrap_are_read_only_and_keep_dll_handles(installed):
    app, _value, _write, handles = installed
    before = sorted(str(path.relative_to(app.parent)) for path in app.parent.rglob("*"))
    layout = installation.load_installation()
    assert not layout["state"].exists()
    assert layout["root"] == app and layout["python"].name == "python.exe"
    assert installation.bootstrap_installed() == layout
    assert installation.bootstrap_installed() == layout
    assert len(handles) == 1 and not handles[0][1].closed
    assert installation._DLL_HANDLES == [handles[0][1]]
    assert sys.dont_write_bytecode is True
    assert sorted(str(path.relative_to(app.parent)) for path in app.parent.rglob("*")) == before


def test_python_base_directory_is_a_valid_owned_dll_directory(installed):
    _app, value, write, handles = installed
    value["dll_directories"].insert(0, "runtime/python")
    write()
    layout = installation.bootstrap_installed()
    assert layout["dll_directories"][0] == layout["python"].parent
    assert len(handles) == 2 and all(not handle.closed for _path, handle in handles)


@pytest.mark.parametrize("change", [
    {"schema": True}, {"kind": "other"}, {"extra": 1}, {"app": "app/v2"},
    {"state": "../state"}, {"node": "C:/seller/node.exe"}, {"model": "https://invalid/model"},
    {"ports": {"board": 8765, "cdp": 8765}}, {"ports": {"board": True, "cdp": 29222}},
    {"browser": {"kind": "system-edge", "extra": 1}},
    {"dll_directories": ["runtime/python/../outside"]},
    {"dll_directories": ["runtime/python/C:/outside"]},
    {"dll_directories": ["runtime/ffmpeg/bin"]},
])
def test_invalid_description_never_falls_back(installed, change):
    app, value, write, _handles = installed
    write(dict(value, **change))
    with pytest.raises(installation.InstallationError):
        installation.load_installation()
    assert not (app.parent / "state").exists()


def test_duplicate_description_keys_rejected(installed):
    app, _value, _write, _handles = installed
    path = app.parent / "installation.json"
    path.write_text(path.read_text().replace('"schema": 1', '"schema": 1, "schema": 1'), encoding="utf-8")
    with pytest.raises(installation.InstallationError):
        installation.load_installation()


def test_hardlinked_descriptor_rejected_without_deleting_alias(installed):
    app, _value, _write, _handles = installed
    alias = app.parent / "descriptor-alias-retained.json"
    os.link(app.parent / "installation.json", alias)
    with pytest.raises(installation.InstallationError):
        installation.load_installation()
    assert alias.exists()


@pytest.mark.parametrize("pythonw", [False, True])
def test_real_python_and_pythonw_identity_allowed(installed, monkeypatch, pythonw):
    layout = installation.load_installation()
    monkeypatch.setattr(sys, "executable", str(layout["python"].with_name("pythonw.exe") if pythonw else layout["python"]))
    assert installation.bootstrap_installed() is not None


def test_other_interpreter_rejected_before_environment_or_dll(installed, monkeypatch):
    _app, _value, _write, handles = installed
    monkeypatch.setattr(sys, "executable", str(Path(sys.executable).with_name("foreign.exe")))
    before = dict(os.environ)
    with pytest.raises(installation.InstallationError, match="installation_python_mismatch"):
        installation.bootstrap_installed()
    assert dict(os.environ) == before and not handles


def test_partial_dll_registration_closes_only_new_handles(installed, monkeypatch):
    app, value, write, _handles = installed
    second = app.parent / "runtime/python/Lib/site-packages/second-owned-dlls"
    second.mkdir()
    value["dll_directories"].append("runtime/python/Lib/site-packages/second-owned-dlls")
    write()
    class Handle:
        closed = False
        def close(self):
            self.closed = True
    first = Handle()
    calls = []
    def add(path):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("synthetic DLL failure")
        return first
    monkeypatch.setattr(os, "add_dll_directory", add)
    before = dict(os.environ)
    with pytest.raises(installation.InstallationError, match="installation_dll_unavailable"):
        installation.bootstrap_installed()
    assert first.closed and installation._ACTIVE is None and not installation._DLL_HANDLES
    assert dict(os.environ) == before


def test_poisoned_environment_replaced_but_network_settings_kept(installed):
    layout = installation.load_installation()
    os.environ.update(PYTHONPATH="seller-modules", PYTHONHOME="seller-python", NODE_OPTIONS="--require foreign",
        NODE_PATH="seller-node", WATCH_NODE="seller-node", NODE_BIN="seller-node", FFMPEG_PATH="seller-ffmpeg",
        WB_ASR_MODEL="seller-model", WATCH_CHROME="seller-browser", DY_CUSTOMER_CONTEXT="seller-context",
        HTTPS_PROXY="http://customer-proxy.invalid:8080", REQUESTS_CA_BUNDLE="customer-ca")
    installation.bootstrap_installed()
    for key in ("PYTHONPATH", "PYTHONHOME", "NODE_OPTIONS", "NODE_PATH", "DY_CUSTOMER_CONTEXT"):
        assert key not in os.environ
    assert os.environ["WATCH_NODE"] == os.environ["NODE_BIN"] == str(layout["node"])
    assert os.environ["FFMPEG_PATH"] == str(layout["ffmpeg"])
    assert os.environ["WB_ASR_MODEL"] == str(layout["model"])
    assert os.environ["HF_HOME"] == str(layout["cache"] / "hf")
    assert os.environ["HTTPS_PROXY"] == "http://customer-proxy.invalid:8080"
    assert os.environ["REQUESTS_CA_BUNDLE"] == "customer-ca"
    assert os.environ["PATH"].split(os.pathsep)[0] == str(layout["node"].parent)


def test_mixed_case_child_dictionary_cannot_reintroduce_windows_overrides(installed):
    layout = installation.load_installation()
    context = context_for(layout)
    env = installation.installed_child_environment(context, {"Node_Options": "--require foreign", "pythonPath": "foreign",
        "path": "seller-node", "watch_node": "seller-node", "hF_hOmE": "seller-cache", "SYSTEMROOT": "C:/Windows",
        "hF_hUb_CaChE": "seller-hub", "huggingface_hub_cache": "seller-legacy-hub", "Transformers_Cache": "seller-transformers",
        "hF_assets_cache": "seller-assets", "Huggingface_Assets_Cache": "seller-legacy-assets", "hf_xet_cache": "seller-xet",
        "https_proxy": "http://customer.invalid"})
    assert not any(key.upper() in {"NODE_OPTIONS", "PYTHONPATH"} for key in env)
    assert [key for key in env if key.upper() == "PATH"] == ["PATH"]
    assert [key for key in env if key.upper() == "WATCH_NODE"] == ["WATCH_NODE"]
    assert env["WATCH_NODE"] == str(layout["node"])
    assert env["HF_HOME"] == str(layout["cache"] / "hf")
    for key, suffix in (("HF_HUB_CACHE", "hub"), ("HUGGINGFACE_HUB_CACHE", "hub"), ("TRANSFORMERS_CACHE", "transformers"),
                        ("HF_ASSETS_CACHE", "assets"), ("HUGGINGFACE_ASSETS_CACHE", "assets"), ("HF_XET_CACHE", "xet")):
        assert [name for name in env if name.upper() == key] == [key]
        assert env[key] == str(layout["cache"] / "hf" / suffix)
    assert env["https_proxy"] == "http://customer.invalid"


def test_current_context_preserved_and_parent_binding_checks_contents(installed):
    layout = installation.load_installation()
    context = context_for(layout)
    put_context(layout, context)
    os.environ["DY_CUSTOMER_CONTEXT"] = str(layout["context_file"])
    installation.bootstrap_installed()
    assert os.environ["DY_CUSTOMER_CONTEXT"] == str(layout["context_file"])
    child = installation.installed_child_environment(context)
    assert child["DY_CUSTOMER_CONTEXT"] == str(layout["context_file"])
    installation.bind_customer_context(context)
    wrong = dict(context, instance_id="f" * 64)
    put_context(layout, wrong)
    with pytest.raises(installation.InstallationError, match="installation_context_mismatch"):
        installation.bind_customer_context(context)
    assert "DY_CUSTOMER_CONTEXT" not in installation.installed_child_environment(context)


def test_first_boot_does_not_set_missing_context_and_assets_do_not_kill_reads(installed):
    layout = installation.bootstrap_installed()
    for key in ("node", "ffmpeg", "model"):
        assert not layout[key].exists()
    assert "DY_CUSTOMER_CONTEXT" not in os.environ and not layout["context_file"].exists()
    installation.validate_installed_context(context_for(layout))


def test_context_from_another_state_is_rejected(installed):
    layout = installation.load_installation()
    context = context_for(layout)
    context["data"] = str(layout["state"] / "other-data")
    with pytest.raises(installation.InstallationError, match="installation_context_mismatch"):
        installation.installed_child_environment(context)


def test_installed_cli_rejects_remote_path_before_resolving_it(installed, monkeypatch):
    from scripts import local_app
    layout = installation.load_installation()
    monkeypatch.setattr(local_app, "bootstrap_installed", lambda: layout)
    monkeypatch.setattr(local_app, "ROOT", layout["root"])
    args = SimpleNamespace(root=str(layout["root"]), config=None, data="\\\\synthetic-server\\must-not-probe",
        roster=None, dist=None, python=None, port=None, cdp_port=None)
    original = Path.resolve
    def guarded(path, *extra, **kwargs):
        assert not str(path).startswith("\\\\synthetic-server"), "must reject before any remote resolution"
        return original(path, *extra, **kwargs)
    monkeypatch.setattr(Path, "resolve", guarded)
    with pytest.raises(local_app.AppError, match="installation_context_mismatch"):
        local_app.context_from_args(args)
    args.data = None
    assert local_app.context_from_args(args) == context_for(layout)
    monkeypatch.setattr(local_app, "bootstrap_installed", lambda: None)
    with pytest.raises(local_app.AppError, match="invalid_port"):
        local_app.context_from_args(args)  # Legacy explicit None was invalid before installation support.


def test_absent_description_keeps_legacy_environment(installed):
    app, _value, _write, handles = installed
    (app.parent / "installation.json").rename(app.parent / "retained-description.json")
    os.environ["WATCH_NODE"] = "legacy-explicit"
    before = dict(os.environ)
    assert installation.bootstrap_installed() is None
    assert installation.installed_child_environment({}) == before
    assert dict(os.environ) == before and not handles


def test_installed_text_streams_reconfigure_utf8_preserving_raw_buffers(installed, monkeypatch):
    incoming = io.BytesIO("合成输入".encode("utf-8"))
    outgoing, diagnostic = io.BytesIO(), io.BytesIO()
    streams = [io.TextIOWrapper(incoming, encoding="gbk"), io.TextIOWrapper(outgoing, encoding="gbk"),
               io.TextIOWrapper(diagnostic, encoding="gbk")]
    with monkeypatch.context() as patch:
        for name, stream in zip(("stdin", "stdout", "stderr"), streams):
            patch.setattr(sys, name, stream)
        installation.bootstrap_installed()
        assert sys.stdin.read() == "合成输入"
        sys.stdout.write("生成中文✓"); sys.stdout.flush()
        sys.stderr.write("生成诊断✓"); sys.stderr.flush()
        # Repeating bootstrap after stdin was read must not reconfigure it again.
        installation.bootstrap_installed()
    assert outgoing.getvalue() == "生成中文✓".encode("utf-8")
    assert diagnostic.getvalue() == "生成诊断✓".encode("utf-8")
    assert [stream.buffer for stream in streams] == [incoming, outgoing, diagnostic]
    assert [stream.errors for stream in streams] == ["strict", "replace", "replace"]
    for stream in streams:
        stream.close()


def test_pythonw_none_standard_streams_are_skipped(installed, monkeypatch):
    with monkeypatch.context() as patch:
        for name in ("stdin", "stdout", "stderr"):
            patch.setattr(sys, name, None)
        result = installation.bootstrap_installed()
    assert result is not None


def test_legacy_text_stream_configuration_is_unchanged(installed, monkeypatch):
    app, _value, _write, _handles = installed
    (app.parent / "installation.json").rename(app.parent / "retained-description.json")
    calls = []
    class ExistingStream:
        def reconfigure(self, **kwargs):
            calls.append(kwargs)
    with monkeypatch.context() as patch:
        for name in ("stdin", "stdout", "stderr"):
            patch.setattr(sys, name, ExistingStream())
        result = installation.bootstrap_installed()
    assert result is None and calls == []
