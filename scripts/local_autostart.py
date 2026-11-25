"""Current-user logon startup for one customer instance.

Planning is pure. Only explicit enable/disable calls use Task Scheduler; the
development tests inject a fake adapter and never register a real task.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from file_lock import FileLock

NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
OWNER = "douyin-customer-logon-v1"
_PATH_KEYS = ("root", "config", "data", "roster", "dist", "python")
ET.register_namespace("", NAMESPACE)


class AutostartError(ValueError):
    pass


def _canonical(value):
    if not isinstance(value, (str, os.PathLike)):
        raise AutostartError("invalid_path")
    text = os.fspath(value)
    if not text or any(ord(char) < 32 for char in text) or '"' in text:
        raise AutostartError("invalid_path")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise AutostartError("absolute_path_required")
    return str(path.resolve())


def instance_id(paths: dict) -> str:
    """The app and logon task share this physical config/data/root identity."""
    try:
        identity = {name: os.path.normcase(_canonical(paths[name]))
                    for name in ("root", "data", "config")}
    except (KeyError, TypeError):
        raise AutostartError("missing_context") from None
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode("ascii")).hexdigest()


def plan(paths: dict) -> dict:
    """Build the exact command and ownership marker without touching the OS."""
    if not isinstance(paths, dict):
        raise AutostartError("invalid_context")
    try:
        context = {name: _canonical(paths[name]) for name in _PATH_KEYS}
        for name in ("port", "cdp_port"):
            value = paths[name]
            if type(value) is not int or not 1 <= value <= 65535:
                raise AutostartError("invalid_port")
            context[name] = value
    except KeyError:
        raise AutostartError("missing_context") from None
    identity = instance_id(context)
    script = str(Path(context["root"]) / "scripts" / "local_app.py")
    # The logon action itself stays alive as the supervisor. It does not depend
    # on Task Scheduler retaining a detached descendant after an action exits.
    from scripts.local_installation import InstallationError, installed_launcher_command
    try:
        command = installed_launcher_command(context)
    except InstallationError:
        raise AutostartError("invalid_context") from None
    if command is None:
        arguments = [script, "run"]
        for name in ("root", "config", "data", "roster", "dist", "python", "port", "cdp_port"):
            arguments.extend(["--" + name.replace("_", "-"), str(context[name])])
        executable = Path(context["python"]).with_name("pythonw.exe")
        command = [str(executable), *arguments]
    else:
        executable, arguments = Path(command[0]), command[1:]
    binding = hashlib.sha256(json.dumps(command, ensure_ascii=True,
        separators=(",", ":")).encode("ascii")).hexdigest()
    marker = json.dumps({"owner": OWNER, "instance_id": identity, "binding": binding},
                        sort_keys=True, separators=(",", ":"))
    return {"schema": 1, "instance_id": identity,
        "task_name": "DouyinLocalApp-" + identity[:24], "owner_marker": marker,
        "command": command, "executable": str(executable),
        "arguments": subprocess.list2cmdline(arguments), "working_directory": context["root"],
        "context": context, "trigger": "current_user_logon", "logon_type": "InteractiveToken",
        "opens_page": False, "requires_password": False}


def task_xml(planned: dict, user_sid: str) -> str:
    if not isinstance(user_sid, str) or not re.fullmatch(r"S-\d-(?:\d+-)+\d+", user_sid):
        raise AutostartError("current_user_unavailable")
    def node(parent, name, text=None, **attributes):
        element = ET.SubElement(parent, "{" + NAMESPACE + "}" + name, attributes)
        element.text = text
        return element
    task = ET.Element("{" + NAMESPACE + "}Task", {"version": "1.2"})
    info = node(task, "RegistrationInfo")
    node(info, "Description", planned["owner_marker"])
    triggers = node(task, "Triggers")
    trigger = node(triggers, "LogonTrigger")
    node(trigger, "Enabled", "true")
    node(trigger, "UserId", user_sid)
    principals = node(task, "Principals")
    principal = node(principals, "Principal", id="CurrentUser")
    node(principal, "UserId", user_sid)
    node(principal, "LogonType", "InteractiveToken")
    node(principal, "RunLevel", "LeastPrivilege")
    settings = node(task, "Settings")
    for name, value in (("MultipleInstancesPolicy", "IgnoreNew"),
                        ("DisallowStartIfOnBatteries", "false"),
                        ("StopIfGoingOnBatteries", "false"), ("StartWhenAvailable", "true"),
                        ("Enabled", "true"), ("Hidden", "true"), ("ExecutionTimeLimit", "PT0S")):
        node(settings, name, value)
    actions = node(task, "Actions", Context="CurrentUser")
    action = node(actions, "Exec")
    node(action, "Command", planned["executable"])
    node(action, "Arguments", planned["arguments"])
    node(action, "WorkingDirectory", planned["working_directory"])
    # XmlText receives a Unicode BSTR, so omit the byte encoding declaration.
    return ET.tostring(task, encoding="unicode", xml_declaration=False)


def _has_default(element, name, value, ns):
    fields = element.findall("t:" + name, ns)
    return not fields or (len(fields) == 1 and len(fields[0]) == 0 and fields[0].text == value)


def _task_enabled_matches(root, enabled, ns):
    if type(enabled) is not bool:
        return False
    fields = root.findall("t:Settings/t:Enabled", ns)
    if not fields:
        return enabled  # Task Scheduler omits the default true on enabled tasks.
    return (len(fields) == 1 and len(fields[0]) == 0 and not fields[0].attrib
            and fields[0].text == ("true" if enabled else "false"))


def _trigger_user_matches(trigger, user_sid, ns):
    fields = trigger.findall("t:UserId", ns)
    if len(fields) != 1 or len(fields[0]) != 0:
        return False
    account = fields[0].text
    if not account or not account.strip() or any(ord(char) < 32 for char in account):
        return False
    if account == user_sid:
        return True
    if re.fullmatch(r"S-\d-(?:\d+-)+\d+", account, re.IGNORECASE):
        return False
    return WindowsTaskScheduler().account_sid(account) == user_sid


def _owned(snapshot, planned, user_sid):
    """A matching name/description alone never proves task ownership."""
    try:
        root = ET.fromstring(snapshot["xml"])
        ns = {"t": NAMESPACE}
        def text(path):
            return root.findtext(path, namespaces=ns)
        principals = root.findall("t:Principals/t:Principal", ns)
        triggers = root.findall("t:Triggers/*", ns)
        actions = root.findall("t:Actions/*", ns)
        return (root.tag == "{" + NAMESPACE + "}Task"
            and text("t:RegistrationInfo/t:Description") == planned["owner_marker"]
            and len(principals) == len(triggers) == len(actions) == 1
            and text("t:Principals/t:Principal/t:UserId") == user_sid
            and text("t:Principals/t:Principal/t:LogonType") == "InteractiveToken"
            # Task Scheduler uses these defaults when the elements are omitted.
            and _has_default(principals[0], "RunLevel", "LeastPrivilege", ns)
            and triggers[0].tag == "{" + NAMESPACE + "}LogonTrigger"
            and _has_default(triggers[0], "Enabled", "true", ns)
            and actions[0].tag == "{" + NAMESPACE + "}Exec"
            and text("t:Actions/t:Exec/t:Command") == planned["executable"]
            and text("t:Actions/t:Exec/t:Arguments") == planned["arguments"]
            and text("t:Actions/t:Exec/t:WorkingDirectory") == planned["working_directory"]
            and text("t:Settings/t:MultipleInstancesPolicy") == "IgnoreNew"
            and text("t:Settings/t:Hidden") == "true"
            and text("t:Settings/t:ExecutionTimeLimit") == "PT0S"
            and _task_enabled_matches(root, snapshot.get("enabled"), ns)
            and _trigger_user_matches(triggers[0], user_sid, ns))
    except (AutostartError, OSError, KeyError, TypeError, ET.ParseError, AttributeError):
        return False


# This script is static; paths/arguments/XML travel as JSON through stdin.
# Creation uses TASK_CREATE (2), never CREATE_OR_UPDATE. Existing tasks are only
# toggled after the exact prior XML is read back again in the same invocation.
_POWERSHELL = r"""
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
try {
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    if ($request.action -eq 'sid') {
        @{sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value} | ConvertTo-Json -Compress
        exit 0
    }
    if ($request.action -eq 'account_sid') {
        $account = [Security.Principal.NTAccount]::new([string]$request.account)
        @{sid=$account.Translate([Security.Principal.SecurityIdentifier]).Value} | ConvertTo-Json -Compress
        exit 0
    }
    $service = New-Object -ComObject Schedule.Service
    $service.Connect()
    $folder = $service.GetFolder('\')
    if ($request.action -eq 'create') {
        $definition = $service.NewTask(0)
        $definition.XmlText = [string]$request.xml
        $null = $folder.RegisterTaskDefinition([string]$request.name, $definition, 2,
            [string]$request.sid, $null, 3, $null)
    }
    $task = $null
    try { $task = $folder.GetTask([string]$request.name) }
    catch {
        $exception = $_.Exception
        $missing = $false
        while ($null -ne $exception) {
            if ($exception.HResult -eq -2147024894) { $missing = $true }
            $exception = $exception.InnerException
        }
        if ($missing) {
            @{missing=$true} | ConvertTo-Json -Compress
            exit 0
        }
        throw
    }
    if ($request.action -eq 'set_enabled') {
        if ([string]$task.Xml -cne [string]$request.expected_xml) {
            @{conflict=$true} | ConvertTo-Json -Compress
            exit 0
        }
        $task.Enabled = [bool]$request.enabled
        $task = $folder.GetTask([string]$request.name)
    }
    @{xml=[string]$task.Xml; enabled=[bool]$task.Enabled} | ConvertTo-Json -Compress
} catch {
    @{error='scheduler_unavailable'} | ConvertTo-Json -Compress
    exit 1
}
"""


class WindowsTaskScheduler:
    """OS adapter; construction and planning do not call Windows."""
    def _invoke(self, action, **values):
        if os.name != "nt":
            raise AutostartError("windows_required")
        executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        if not executable.is_file():
            raise AutostartError("scheduler_unavailable")
        encoded = base64.b64encode(_POWERSHELL.encode("utf-16-le")).decode("ascii")
        try:
            result = subprocess.run([str(executable), "-NoLogo", "-NoProfile", "-NonInteractive",
                "-EncodedCommand", encoded], input=json.dumps(dict(action=action, **values), ensure_ascii=True),
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                errors="replace", timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            value = json.loads(result.stdout.lstrip("\ufeff"))
            if result.returncode or not isinstance(value, dict) or value.get("error"):
                raise AutostartError("scheduler_unavailable")
            return value
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise AutostartError("scheduler_unavailable") from None

    def current_user(self):
        return self._invoke("sid")["sid"]

    def account_sid(self, account):
        return self._invoke("account_sid", account=account).get("sid")

    def read(self, name):
        value = self._invoke("read", name=name)
        return None if value.get("missing") is True else value

    def create(self, name, xml, user_sid):
        return self._invoke("create", name=name, xml=xml, sid=user_sid)

    def set_enabled(self, name, expected_xml, enabled):
        return self._invoke("set_enabled", name=name, expected_xml=expected_xml, enabled=enabled)


def manage(action, paths, adapter=None):
    """Inspect or explicitly enable/disable only this exact current-user task."""
    try:
        planned = plan(paths)
    except AutostartError:
        return {"ok": False, "state": "invalid_context", "changed": False}
    if action == "plan":
        return {"ok": True, "state": "planned", "changed": False, "plan": planned}
    if action not in {"status", "enable", "disable"}:
        return {"ok": False, "state": "invalid_action", "changed": False}
    if action == "enable" and adapter is None and not Path(planned["executable"]).is_file():
        missing = "launcher_missing" if Path(planned["executable"]).name == "CustomerLauncherW.exe" else "pythonw_missing"
        return {"ok": False, "state": missing, "changed": False}
    adapter = adapter or WindowsTaskScheduler()
    try:
        if action == "status":
            return _manage(action, planned, adapter)
        lock = Path(planned["context"]["data"]) / "local_app_autostart"
        with FileLock(lock, timeout=10):
            return _manage(action, planned, adapter)
    except (AutostartError, OSError, TimeoutError, KeyError, TypeError):
        return {"ok": False, "state": "unavailable",
                "changed": None if action in {"enable", "disable"} else False,
                "enabled": None,
                "task_name": planned["task_name"]}


def _manage(action, planned, adapter):
    sid = adapter.current_user()
    # Validate the selected account before any mutating adapter call.
    xml = task_xml(planned, sid)
    existing = adapter.read(planned["task_name"])
    if existing is not None and not _owned(existing, planned, sid):
        return {"ok": False, "state": "ownership_conflict", "changed": False,
                "task_name": planned["task_name"]}
    changed = False
    if existing is None:
        if action != "enable":
            return {"ok": True, "state": "disabled", "enabled": False,
                    "registered": False, "changed": False, "task_name": planned["task_name"]}
        existing = adapter.create(planned["task_name"], xml, sid)
        changed = True
    elif action != "status" and existing["enabled"] != (action == "enable"):
        existing = adapter.set_enabled(planned["task_name"], existing["xml"], action == "enable")
        if isinstance(existing, dict) and existing.get("conflict") is True:
            return {"ok": False, "state": "ownership_conflict", "changed": False,
                    "task_name": planned["task_name"]}
        changed = True
    if not _owned(existing, planned, sid):
        return {"ok": False, "state": "state_unconfirmed", "changed": None,
                "task_name": planned["task_name"]}
    expected = None if action == "status" else action == "enable"
    if expected is not None and existing["enabled"] is not expected:
        return {"ok": False, "state": "state_unconfirmed", "changed": changed,
                "task_name": planned["task_name"]}
    return {"ok": True, "state": "enabled" if existing["enabled"] else "disabled",
            "enabled": existing["enabled"], "registered": True, "changed": changed,
            "task_name": planned["task_name"]}
