"""Task registration is fake; no real startup item/task/service is touched."""
import copy
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import local_autostart as startup

SID = "S-1-5-21-111-222-333-1001"
NS = {"t": startup.NAMESPACE}


@pytest.fixture
def paths(tmp_path):
    root = tmp_path / "客户 软件 $() ` 目录"
    return {"root": str(root), "data": str(root / "客户 数据"),
        "config": str(root / "客户 配置.json"), "roster": str(root / "主播 名册.json"),
        "dist": str(root / "web/dist"), "python": str(root / ".venv/Scripts/python.exe"),
        "port": 18765, "cdp_port": 19222}


def change_xml(snapshot, path, value):
    root = ET.fromstring(snapshot["xml"])
    root.find(path, NS).text = value
    snapshot["xml"] = ET.tostring(root, encoding="unicode")


class FakeScheduler:
    def __init__(self):
        self.tasks, self.writes = {}, []
        self.user = SID

    def current_user(self):
        return self.user

    def read(self, name):
        return copy.deepcopy(self.tasks.get(name))

    def create(self, name, xml, user_sid):
        assert name not in self.tasks, "create must never overwrite an existing task"
        assert user_sid == self.user
        self.tasks[name] = {"xml": xml, "enabled": True}
        self.writes.append(("create", name))
        return self.read(name)

    def set_enabled(self, name, expected_xml, enabled):
        if self.tasks[name]["xml"] != expected_xml:
            return {"conflict": True}
        self.tasks[name]["enabled"] = enabled
        change_xml(self.tasks[name], "t:Settings/t:Enabled", str(enabled).lower())
        self.writes.append(("set_enabled", name, enabled))
        return self.read(name)


def test_plan_is_pure_and_uses_the_same_entry_context(paths, monkeypatch):
    monkeypatch.setattr(startup, "WindowsTaskScheduler", lambda: pytest.fail("plan must not touch OS"))
    result = startup.manage("plan", paths)
    planned = result["plan"]
    assert result["ok"] and result["changed"] is False
    assert planned["command"][:3] == [str(Path(paths["python"]).resolve().with_name("pythonw.exe")),
        str(Path(paths["root"]).resolve() / "scripts/local_app.py"), "run"]
    assert planned["working_directory"] == str(Path(paths["root"]).resolve())
    assert "--open" not in planned["command"] and not planned["opens_page"]
    assert planned["logon_type"] == "InteractiveToken" and not planned["requires_password"]
    assert planned["instance_id"] == startup.instance_id(paths)
    assert not Path(paths["data"]).exists()


def test_instance_identity_canonicalizes_and_isolates_config_data_roots(paths):
    original = startup.instance_id(paths)
    alias = dict(paths, root=str(Path(paths["root"]) / "child/.."))
    assert startup.instance_id(alias) == original
    assert startup.instance_id(dict(paths, port=9999)) == original
    for key in ("root", "config", "data"):
        assert startup.instance_id(dict(paths, **{key: paths[key] + "-other"})) != original
    assert len(original) == 64


@pytest.mark.skipif(os.name != "nt", reason="actual Windows argument parser")
def test_unicode_space_arguments_roundtrip_with_windows_parser(paths):
    planned = startup.plan(paths)
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    count = ctypes.c_int()
    pointer = shell.CommandLineToArgvW(subprocess.list2cmdline(planned["command"]), ctypes.byref(count))
    assert pointer
    try:
        assert [pointer[index] for index in range(count.value)] == planned["command"]
    finally:
        kernel.LocalFree(pointer)
    assert startup.plan(paths)["arguments"] == subprocess.list2cmdline(planned["command"][1:])


def test_task_definition_binds_only_current_user_logon_and_exact_command(paths):
    planned = startup.plan(paths)
    xml = startup.task_xml(planned, SID)
    # Task Scheduler receives Unicode text as a COM BSTR, without a byte encoding declaration.
    assert isinstance(xml, str) and not xml.lstrip().startswith("<?xml")
    root = ET.fromstring(xml)
    assert root.findtext("t:Triggers/t:LogonTrigger/t:UserId", namespaces=NS) == SID
    assert root.findtext("t:Principals/t:Principal/t:UserId", namespaces=NS) == SID
    assert root.findtext("t:Principals/t:Principal/t:LogonType", namespaces=NS) == "InteractiveToken"
    assert root.findtext("t:Principals/t:Principal/t:RunLevel", namespaces=NS) == "LeastPrivilege"
    assert root.findtext("t:Actions/t:Exec/t:WorkingDirectory", namespaces=NS) == paths["root"]
    assert root.findtext("t:Actions/t:Exec/t:Arguments", namespaces=NS) == planned["arguments"]
    assert root.findtext("t:Settings/t:MultipleInstancesPolicy", namespaces=NS) == "IgnoreNew"
    assert root.findtext("t:Settings/t:ExecutionTimeLimit", namespaces=NS) == "PT0S"
    assert "Password" not in xml and "BootTrigger" not in xml


def test_fake_registration_enable_disable_check_is_idempotent(paths):
    adapter = FakeScheduler()
    result = startup.manage("status", paths, adapter)
    assert result["ok"] and result["enabled"] is False and result["registered"] is False
    assert not Path(paths["data"]).exists() and adapter.writes == []
    assert startup.manage("disable", paths, adapter)["changed"] is False
    enabled = startup.manage("enable", paths, adapter)
    assert enabled["ok"] and enabled["enabled"] and enabled["changed"]
    assert startup.manage("enable", paths, adapter)["changed"] is False
    assert startup.manage("status", paths, adapter)["enabled"] is True
    disabled = startup.manage("disable", paths, adapter)
    assert disabled["ok"] and not disabled["enabled"] and disabled["changed"]
    assert startup.manage("disable", paths, adapter)["changed"] is False
    assert startup.manage("enable", paths, adapter)["enabled"] is True
    assert [entry[0] for entry in adapter.writes] == ["create", "set_enabled", "set_enabled"]
    assert len(adapter.tasks) == 1


def normalized_readback(xml, account, *, enabled):
    """Mirror the real registered COM XML shape observed on Windows 11."""
    root = ET.fromstring(xml)
    principal = root.find("t:Principals/t:Principal", NS)
    principal.remove(principal.find("t:RunLevel", NS))
    trigger = root.find("t:Triggers/t:LogonTrigger", NS)
    trigger.remove(trigger.find("t:Enabled", NS))
    trigger.find("t:UserId", NS).text = account
    settings = root.find("t:Settings", NS)
    task_enabled = settings.find("t:Enabled", NS)
    if enabled:
        settings.remove(task_enabled)
    else:
        task_enabled.text = "false"
    return {"xml": ET.tostring(root, encoding="unicode"), "enabled": enabled}


def test_normalized_registered_task_keeps_ownership_and_exact_toggle_check(paths, monkeypatch):
    account = r"SYNTHETIC\Current User"
    monkeypatch.setattr(startup.WindowsTaskScheduler, "account_sid",
                        lambda _scheduler, name: SID if name == account else None, raising=False)

    class NormalizingScheduler(FakeScheduler):
        def read(self, name):
            snapshot = super().read(name)
            if snapshot is not None:
                return normalized_readback(snapshot["xml"], account, enabled=snapshot["enabled"])

        def set_enabled(self, name, expected_xml, enabled):
            if self.read(name)["xml"] != expected_xml:
                return {"conflict": True}
            return super().set_enabled(name, self.tasks[name]["xml"], enabled)

    adapter = NormalizingScheduler()
    enabled = startup.manage("enable", paths, adapter)
    assert enabled["ok"] and enabled["registered"] and enabled["enabled"] and enabled["changed"]
    assert startup.manage("status", paths, adapter)["enabled"] is True
    assert startup.manage("enable", paths, adapter)["changed"] is False
    disabled = startup.manage("disable", paths, adapter)
    assert disabled["ok"] and disabled["registered"] and not disabled["enabled"] and disabled["changed"]
    assert startup.manage("status", paths, adapter)["enabled"] is False
    reenabled = startup.manage("enable", paths, adapter)
    assert reenabled["ok"] and reenabled["enabled"] and reenabled["changed"]
    set_enabled = adapter.set_enabled

    def raced_toggle(name, expected_xml, enabled):
        change_xml(adapter.tasks[name], "t:Actions/t:Exec/t:Command", r"C:\other.exe")
        return set_enabled(name, expected_xml, enabled)

    adapter.set_enabled = raced_toggle
    result = startup.manage("disable", paths, adapter)
    assert result["state"] == "ownership_conflict" and not result["changed"]
    assert [entry[0] for entry in adapter.writes] == ["create", "set_enabled", "set_enabled"]


@pytest.mark.parametrize("enabled, fields", [
    pytest.param(False, "", id="disabled-omission"),
    pytest.param(True, "<Enabled>false</Enabled>", id="enabled-contradiction"),
    pytest.param(False, "<Enabled>true</Enabled>", id="disabled-contradiction"),
    pytest.param(True, "<Enabled/>", id="explicit-empty"),
    pytest.param(True, "<Enabled> true </Enabled>", id="whitespace"),
    pytest.param(True, "<Enabled>1</Enabled>", id="noncanonical"),
    pytest.param(True, "<Enabled>true</Enabled><Enabled>true</Enabled>", id="duplicate"),
    pytest.param(False, "<Enabled>false</Enabled><Enabled>true</Enabled>", id="duplicate-conflict"),
    pytest.param(True, "<Enabled>true<Other/></Enabled>", id="nested"),
    pytest.param(True, '<Enabled extra="value">true</Enabled>', id="attribute"),
    pytest.param(1, "<Enabled>true</Enabled>", id="nonbool"),
])
def test_invalid_task_enabled_readback_never_authorizes_writes(paths, enabled, fields):
    planned, adapter = startup.plan(paths), FakeScheduler()
    root = ET.fromstring(startup.task_xml(planned, SID))
    settings = root.find("t:Settings", NS)
    settings.remove(settings.find("t:Enabled", NS))
    settings.extend(ET.fromstring(f'<Settings xmlns="{startup.NAMESPACE}">{fields}</Settings>'))
    adapter.tasks[planned["task_name"]] = {
        "xml": ET.tostring(root, encoding="unicode"),
        "enabled": enabled,
    }
    for action in ("status", "enable", "disable"):
        result = startup.manage(action, paths, adapter)
        assert result["state"] == "ownership_conflict" and result["changed"] is False
    assert adapter.writes == []


@pytest.mark.parametrize("section,field,values", [
    ("t:Principals/t:Principal", "RunLevel", [""]),
    ("t:Principals/t:Principal", "RunLevel", ["LeastPrivilege", "HighestAvailable"]),
    ("t:Triggers/t:LogonTrigger", "Enabled", [""]),
    ("t:Triggers/t:LogonTrigger", "Enabled", ["true", "false"]),
    ("t:Triggers/t:LogonTrigger", "UserId", []),
    ("t:Triggers/t:LogonTrigger", "UserId", [""]),
    ("t:Triggers/t:LogonTrigger", "UserId", ["  "]),
    ("t:Triggers/t:LogonTrigger", "UserId", [SID, "S-1-5-21-999-1001"]),
    ("t:Triggers/t:LogonTrigger", "UserId", ["S-1-5-21-999-1001"]),
])
def test_malformed_normalized_fields_never_authorize_task(paths, monkeypatch, section, field, values):
    monkeypatch.setattr(startup.WindowsTaskScheduler, "account_sid",
                        lambda *_args: pytest.fail("invalid XML must not reach account translation"), raising=False)
    planned, adapter = startup.plan(paths), FakeScheduler()
    snapshot = normalized_readback(startup.task_xml(planned, SID), SID, enabled=False)
    root = ET.fromstring(snapshot["xml"])
    parent = root.find(section, NS)
    for element in parent.findall("t:" + field, NS):
        parent.remove(element)
    for value in values:
        ET.SubElement(parent, "{" + startup.NAMESPACE + "}" + field).text = value
    snapshot["xml"] = ET.tostring(root, encoding="unicode")
    adapter.tasks[planned["task_name"]] = snapshot
    assert startup.manage("enable", paths, adapter)["state"] == "ownership_conflict"
    assert adapter.writes == []


@pytest.mark.parametrize("resolved", [None, "S-1-5-21-999-1001", startup.AutostartError("unmapped")])
def test_trigger_account_must_resolve_to_current_user_sid(paths, monkeypatch, resolved):
    account = r"SYNTHETIC\Other Account"
    def lookup(_scheduler, name):
        assert name == account
        if isinstance(resolved, Exception):
            raise resolved
        return resolved

    monkeypatch.setattr(startup.WindowsTaskScheduler, "account_sid", lookup, raising=False)
    planned, adapter = startup.plan(paths), FakeScheduler()
    adapter.tasks[planned["task_name"]] = normalized_readback(startup.task_xml(planned, SID), account, enabled=False)
    assert startup.manage("enable", paths, adapter)["state"] == "ownership_conflict"
    assert adapter.writes == []


@pytest.mark.parametrize("path,value", [
    ("t:RegistrationInfo/t:Description", "foreign"),
    ("t:Actions/t:Exec/t:Command", "C:\\other\\python.exe"),
    ("t:Actions/t:Exec/t:Arguments", "different arguments"),
    ("t:Actions/t:Exec/t:WorkingDirectory", "C:\\other"),
    ("t:Principals/t:Principal/t:UserId", "S-1-5-21-999-1001"),
    ("t:Principals/t:Principal/t:LogonType", "Password"),
    ("t:Principals/t:Principal/t:RunLevel", "HighestAvailable"),
    ("t:Triggers/t:LogonTrigger/t:UserId", "S-1-5-21-999-1001"),
    ("t:Triggers/t:LogonTrigger/t:Enabled", "false"),
    ("t:Settings/t:MultipleInstancesPolicy", "Parallel"),
    ("t:Settings/t:Hidden", "false"),
    ("t:Settings/t:ExecutionTimeLimit", "PT1M"),
    ("t:Settings/t:Enabled", "false"),
])
def test_foreign_or_modified_task_is_never_overwritten_or_disabled(paths, path, value):
    adapter = FakeScheduler()
    planned = startup.plan(paths)
    original = {"xml": startup.task_xml(planned, SID), "enabled": True}
    change_xml(original, path, value)
    adapter.tasks[planned["task_name"]] = original
    before = copy.deepcopy(adapter.tasks)
    for action in ("status", "enable", "disable"):
        result = startup.manage(action, paths, adapter)
        assert not result["ok"] and result["state"] == "ownership_conflict"
        assert result["changed"] is False
    assert adapter.tasks == before and adapter.writes == []


@pytest.mark.parametrize("section", ["Actions", "Triggers", "Principals"])
def test_extra_task_action_trigger_or_principal_is_a_conflict(paths, section):
    planned, adapter = startup.plan(paths), FakeScheduler()
    root = ET.fromstring(startup.task_xml(planned, SID))
    collection = root.find("t:" + section, NS)
    collection.append(copy.deepcopy(collection[0]))
    adapter.tasks[planned["task_name"]] = {"xml": ET.tostring(root, encoding="unicode"), "enabled": True}
    assert startup.manage("disable", paths, adapter)["state"] == "ownership_conflict"
    assert adapter.writes == []


def test_changed_port_has_same_instance_but_conflicting_old_task_binding(paths):
    adapter = FakeScheduler()
    assert startup.manage("enable", paths, adapter)["ok"]
    before = copy.deepcopy(adapter.tasks)
    changed = dict(paths, port=18766)
    assert startup.plan(paths)["task_name"] == startup.plan(changed)["task_name"]
    assert startup.manage("enable", changed, adapter)["state"] == "ownership_conflict"
    assert adapter.tasks == before and len(adapter.writes) == 1


def test_race_after_read_is_rejected_without_overwriting(paths):
    class Race(FakeScheduler):
        def set_enabled(self, name, expected_xml, enabled):
            change_xml(self.tasks[name], "t:RegistrationInfo/t:Description", "new foreign task")
            return super().set_enabled(name, expected_xml, enabled)
    adapter = Race()
    assert startup.manage("enable", paths, adapter)["ok"]
    result = startup.manage("disable", paths, adapter)
    assert result["state"] == "ownership_conflict" and result["changed"] is False
    assert adapter.tasks[result["task_name"]]["enabled"] is True
    assert len(adapter.writes) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell adapter transport")
@pytest.mark.parametrize("case", ["disable", "enable", "prewrite-change", "contradictory-xml", "foreign-readback"])
def test_windows_toggle_reads_fresh_task_and_validates_readback(paths, monkeypatch, case):
    planned = startup.plan(paths)
    name = planned["task_name"]
    enabled = case == "enable"
    initial_xml = startup.task_xml(planned, SID)
    before = normalized_readback(initial_xml, SID, enabled=not enabled)
    fresh = normalized_readback(initial_xml, SID, enabled=enabled)
    if case == "contradictory-xml":
        change_xml(fresh, "t:Settings/t:Enabled", "true")
    if case == "foreign-readback":
        change_xml(fresh, "t:Actions/t:Exec/t:Command", r"C:\other.exe")
    current_xml = before["xml"] + "\n" if case == "prewrite-change" else before["xml"]

    # Run the actual adapter script with an in-memory task folder replacing COM.
    connection = r"""    $service = New-Object -ComObject Schedule.Service
    $service.Connect()
    $folder = $service.GetFolder('\')"""
    fixture = r"""
    $script:fixture = $request
    $script:lookups = 0
    $script:writes = 0
    $folder = [pscustomobject]@{}
    $folder | Add-Member -MemberType ScriptMethod -Name GetTask -Value {
        param([string]$name)
        if ($name -cne $script:fixture.fixture_name) { throw 'wrong task name' }
        $script:lookups++
        if ($script:lookups -eq 2 -and $script:writes -eq 1) {
            return [pscustomobject]@{Xml=$script:fixture.fresh_xml; Enabled=$script:fixture.enabled}
        }
        if ($script:lookups -ne 1) { throw 'unexpected read before assignment' }
        $cached = [pscustomobject]@{Xml=$script:fixture.current_xml}
        $cached | Add-Member -MemberType ScriptProperty -Name Enabled -Value {
            if ($script:writes -eq 1) { return $script:fixture.enabled }
            return -not $script:fixture.enabled
        } -SecondValue {
            param($value)
            if ($script:writes -ne 0 -or $script:lookups -ne 1 -or
                $script:fixture.current_xml -cne $script:fixture.expected_xml -or
                $value -ne $script:fixture.enabled) { throw 'unexpected assignment' }
            $script:writes++
        }
        return $cached
    }
"""
    assert startup._POWERSHELL.count(connection) == 1
    script = startup._POWERSHELL.replace(connection, fixture)
    assert "-ComObject Schedule.Service" not in script
    monkeypatch.setattr(startup, "_POWERSHELL", script)
    responses = []

    class InMemoryScheduler(startup.WindowsTaskScheduler):
        def current_user(self):
            return SID

        def read(self, task_name):
            assert task_name == name
            return before.copy()

        def _invoke(self, action, **values):
            assert action == "set_enabled"
            result = super()._invoke(action, **values, fixture_name=name,
                                     current_xml=current_xml, fresh_xml=fresh["xml"])
            responses.append(result)
            return result

    result = startup.manage("enable" if enabled else "disable", paths, InMemoryScheduler())
    if case == "prewrite-change":
        assert responses == [{"conflict": True}]
        assert result["state"] == "ownership_conflict" and result["changed"] is False
    else:
        assert responses == [fresh], result
        if case in {"contradictory-xml", "foreign-readback"}:
            assert result["state"] == "state_unconfirmed" and result["changed"] is None
        else:
            assert result["ok"] and result["changed"] and result["enabled"] is enabled


def test_mutation_error_reports_unknown_and_does_not_claim_disabled(paths):
    class LateError(FakeScheduler):
        def create(self, *args):
            super().create(*args)
            raise OSError("reply lost after registration")
    adapter = LateError()
    result = startup.manage("enable", paths, adapter)
    assert not result["ok"] and result["state"] == "unavailable"
    assert result["enabled"] is None and result["changed"] is None
    assert startup.manage("status", paths, adapter)["enabled"] is True


def test_create_race_never_replaces_the_new_foreign_registration(paths):
    class Race(FakeScheduler):
        def create(self, name, xml, user_sid):
            self.tasks[name] = {"xml": "foreign", "enabled": True}
            raise OSError("exclusive create found an existing task")
    adapter = Race()
    result = startup.manage("enable", paths, adapter)
    assert not result["ok"]
    assert adapter.tasks[result["task_name"]] == {"xml": "foreign", "enabled": True}
    assert adapter.writes == []


def test_adapter_transport_uses_static_script_and_stdin_json_only(paths, monkeypatch):
    calls = []
    monkeypatch.setattr(Path, "is_file", lambda _path: True)
    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"missing":true}')
    monkeypatch.setattr(startup.subprocess, "run", fake_run)
    adapter = startup.WindowsTaskScheduler()
    assert adapter.read("test-name") is None
    command, options = calls[0]
    assert command[-2] == "-EncodedCommand"
    import base64
    assert base64.b64decode(command[-1]).decode("utf-16-le") == startup._POWERSHELL
    assert json.loads(options["input"]) == {"action": "read", "name": "test-name"}
    assert options["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert "RegisterTaskDefinition([string]$request.name, $definition, 2," in startup._POWERSHELL
    assert "$request.expected_xml" in startup._POWERSHELL
    assert "-NonInteractive" in command and options["timeout"] == 20


@pytest.mark.parametrize("key,value", [("port", True), ("port", 0), ("cdp_port", 65536),
    ("root", "relative/path"), ("config", "bad\npath"), ("data", 'C:\\bad"path')])
def test_invalid_plan_context_never_calls_adapter(paths, key, value):
    class Denied:
        def current_user(self):
            pytest.fail("invalid context must never call OS")
    result = startup.manage("enable", dict(paths, **{key: value}), Denied())
    assert result == {"ok": False, "state": "invalid_context", "changed": False}


def test_invalid_account_never_registers_another_user(paths):
    adapter = FakeScheduler()
    adapter.user = "DOMAIN\\some-other-user"
    result = startup.manage("enable", paths, adapter)
    assert not result["ok"] and adapter.writes == []
