"""One customer-owned Windows application: Board, existing watch/record and ASR.

CLI callers detach from the application. The supervisor keeps exact process
handles and private Jobs; normal shutdown waits for writers instead of killing
an unverified PID or deleting their durable work.
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.local_installation import (bootstrap_installed, bootstrap_entry, installed_child_environment,
    validate_installed_context, bind_customer_context, installed_launcher_command)
bootstrap_entry()
from file_lock import FileLock
from backfill_tasks import process_identity, process_parent_pid
from local_autostart import instance_id, manage as autostart_manage
from local_process import OwnedProcess, listener_pid, same_identity


BROWSER_START_TIMEOUT_SECONDS = 60


class AppError(ValueError):
    pass


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise AppError("runtime_path_unavailable")
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        with FileLock(path):
            if path.is_symlink():
                raise AppError("runtime_path_unavailable")
            deadline = time.monotonic() + 2.0
            while True:
                try:
                    os.replace(temporary, path)
                    break
                except PermissionError:
                    # Windows readers outside this protocol may briefly hold a
                    # non-delete-sharing handle. A status read must not crash us.
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.02)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    try:
        with FileLock(path):
            if path.is_symlink() or path.stat().st_size > 1024 * 1024:
                raise AppError("state_unavailable")
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        raise AppError("state_unavailable") from None


def context_from_args(args):
    installed = bootstrap_installed()
    if installed:
        # Refuse foreign/remote CLI paths before resolve can probe them.
        for name in ("root", "config", "data", "roster", "dist", "python"):
            value = getattr(args, name)
            if value is None:
                continue
            text = str(value)
            candidate = Path(text)
            if (not candidate.is_absolute() or text.startswith(("\\\\", "//")) or "://" in text
                    or ".." in candidate.parts or any(ord(char) < 32 for char in text)
                    or os.path.normcase(os.path.normpath(text)) != os.path.normcase(str(installed[name]))):
                raise AppError("installation_context_mismatch")
    root = Path(args.root).expanduser().resolve()
    if root != ROOT:
        raise AppError("application_root_mismatch")
    context = {"root": str(root)}
    defaults = {"config": root / "config.json", "data": root / "data",
                "roster": root / "anchors.json", "dist": root / "web/dist",
                "python": Path(sys.executable)}
    if installed:
        defaults.update({name: installed[name] for name in defaults})
    for name, default in defaults.items():
        context[name] = str(Path(getattr(args, name) or default).expanduser().resolve())
    for name in ("port", "cdp_port"):
        value = getattr(args, name)
        if value is None and installed:
            value = installed[name]
        if type(value) is not int or not 1 <= value <= 65535:
            raise AppError("invalid_port")
        context[name] = value
    if context["port"] == context["cdp_port"]:
        raise AppError("port_conflict")
    if re.fullmatch(r"watch_.+\.config\.json", Path(context["config"]).name):
        raise AppError("canonical_config_required")
    context.update(schema=1, instance_id=instance_id(context),
                   profile=str(Path(context["data"]) / ".local_app/browser-profile"))
    validate_installed_context(context)
    return context


def runtime_dir(context):
    path = Path(context["data"]) / ".local_app"
    if path.is_symlink():
        raise AppError("runtime_path_unavailable")
    return path


def check_configuration(context):
    if not Path(context["python"]).is_file():
        raise AppError("python_missing")
    if not (Path(context["dist"]) / "index.html").is_file():
        raise AppError("dist_missing")
    if not Path(context["config"]).is_file():
        raise AppError("config_missing")
    config = read_json(context["config"])
    if config is None:
        raise AppError("config_missing")
    if not Path(context["roster"]).is_file():
        raise AppError("roster_missing")
    import anchor_roster
    try:
        with anchor_roster.transaction(context["roster"]):
            roster = anchor_roster.load(context["roster"])
            if any(anchor_roster.validate(entry) for entry in roster["anchors"]):
                raise ValueError
    except (OSError, ValueError, TypeError):
        raise AppError("roster_invalid") from None
    return config, roster


def process_fact(receipt):
    if not isinstance(receipt, dict) or type(receipt.get("pid")) is not int:
        return "unknown"
    actual = process_identity(receipt["pid"])
    if actual["state"] == "dead":
        return "dead"
    if actual["state"] != "alive":
        return "unknown"
    return "alive" if same_identity(receipt, actual) else "reused"


def status(context, *, probe=True):
    value = read_json(runtime_dir(context) / "instance.json")
    if value is None:
        return {"ok": True, "state": "stopped", "instance_id": context["instance_id"]}
    if value.get("instance_id") != context["instance_id"]:
        return {"ok": False, "state": "instance_conflict"}
    if value.get("context") != context:
        return {"ok": False, "state": "instance_context_conflict"}
    fact = process_fact(value.get("owner"))
    result = copy.deepcopy(value)
    result["process_state"] = fact
    if fact == "dead" or fact == "reused" and value.get("state") == "stopped":
        result.update(ok=value.get("state") == "stopped", state=("stopped" if value.get("state") == "stopped" else "exited"))
        for role in result.get("roles", {}).values():
            role.update(state="stopped" if value.get("state") == "stopped" else "unknown", running=False)
    elif fact != "alive" or type(value.get("updated_at")) not in (int, float) or time.time() - value["updated_at"] > 10:
        result.update(ok=False, state="identity_unknown" if fact == "unknown" else "stale_instance")
        for role in result.get("roles", {}).values():
            role.update(state="unknown", running=False)
    elif probe and value.get("board_ready"):
        endpoint = listener_pid(context["port"])
        if endpoint["state"] != "listening" or endpoint["pids"] != [value["owner"]["pid"]]:
            result.update(ok=False, state="board_identity_unavailable")
        else:
            try:
                # A local service must never redirect this identity probe elsewhere.
                class NoRedirect(urllib.request.HTTPRedirectHandler):
                    def redirect_request(self, *unused):
                        return None
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
                with opener.open("http://127.0.0.1:%s/api/health" % context["port"], timeout=.8) as response:
                    health = json.loads(response.read(16384))
                if health.get("instance_id") != context["instance_id"] or health.get("owner") != value["owner"]:
                    raise ValueError
            except Exception:
                result.update(ok=False, state="board_unavailable")
    asr = result.get("asr")
    if isinstance(asr, dict) and (fact != "alive" or type(value.get("updated_at")) not in (int, float)
                                  or not 0 <= time.time() - value["updated_at"] <= 10):
        asr.update(state="stopped" if value.get("state") == "stopped" else "snapshot_unconfirmed",
                   verified=False, stale=True)
        if isinstance(asr.get("counts"), dict):
            asr["counts"] = dict.fromkeys(asr["counts"], None)
    return result


class RoleManager:
    def __init__(self, context, *, command_builder=None, spawn=OwnedProcess.start, now=time.monotonic):
        self.context = context
        self.directory = runtime_dir(context)
        self.lock = threading.RLock()
        self.spawn = spawn
        self.command_builder = command_builder
        self.now = now
        self.roles, self.manual = {}, []
        self.launch_failures = {}
        self.stopping = False
        self.inflight = 0
        self.browser = None
        self.browser_failures = 0
        self.browser_retry = 0.0
        self.browser_started = 0.0
        self.browser_manual = False
        self.browser_generation = ""
        self.browser_error = ""
        self.last_error = ""
        self.suppressed = set()
        self.intent_error = False
        from local_login import LoginGuide
        self.login = LoginGuide(self)
        from local_asr import AsrLane
        self.asr = AsrLane(self, identity=process_identity)
        try:
            intent = read_json(self.directory / "role-intents.json", {})
            if intent:
                values = intent.get("suppressed")
                if (intent.get("instance_id") != context["instance_id"] or not isinstance(values, list)
                        or any(not isinstance(key, str) or not re.fullmatch(r"(?:watch|record):[^/\\\x00-\x1f]{1,40}", key) for key in values)):
                    raise AppError("role_intent_unavailable")
                self.suppressed = set(values)
        except AppError:
            self.intent_error = True
            self.last_error = "role_intent_unavailable"

    def save_intents(self):
        try:
            atomic_json(self.directory / "role-intents.json", {"schema": 1,
                "instance_id": self.context["instance_id"], "suppressed": sorted(self.suppressed)})
        except (AppError, OSError):
            self.intent_error = True
            raise AppError("role_intent_unavailable") from None

    def child_env(self):
        env = dict(installed_child_environment(self.context), DY_CUSTOMER_CONTEXT=str(self.directory / "context.json"),
                   PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
        for name in ("DY_CUSTOMER_STOP", "DY_CUSTOMER_ROLE_NONCE"):
            env.pop(name, None)
        return env

    def command(self, kind, entry):
        if self.command_builder:
            return self.command_builder(kind, entry)
        root = Path(self.context["root"])
        command = [self.context["python"], "-B", str(root / "scripts" / (
            "run_watchdog.py" if kind == "watch" else "stream_recorder.py")), "--tag", entry["tag"]]
        if kind == "watch":
            command += ["--sec-uid", entry["sec_uid"], "--start", "--python", self.context["python"]]
            if entry.get("llm"):
                command.append("--llm")
        return command

    def start_role(self, kind, entry, *, explicit=True):
        from local_context import role_receipt_path
        key = kind + ":" + entry["tag"]
        with self.lock:
            if self.stopping:
                return {"ok": False, "state": "stopping"}
            if self.intent_error:
                return {"ok": False, "state": "role_intent_unavailable"}
            if kind not in ("watch", "record") or not entry.get("enabled") or not entry.get(kind):
                return {"ok": False, "state": "disabled"}
            if explicit and key in self.suppressed:
                self.suppressed.remove(key)
                self.save_intents()
            if key in self.suppressed:
                return {"ok": False, "state": "disabled_by_user"}
            if not self.command_builder and kind == "record" and importlib.util.find_spec("curl_cffi") is None:
                self.last_error = "record_dependency_missing_curl_cffi"
                return {"ok": False, "state": "dependency_missing", "dependency": "curl_cffi"}
            failed = self.launch_failures.get(key, {"failures": 0, "retry_at": 0})
            if failed["failures"] >= 3 or self.now() < failed["retry_at"]:
                return {"ok": False, "state": "restart_limited"}
            existing = self.roles.get(key)
            if existing:
                count = existing["owned"].active_count()
                if count is None:
                    existing["state"] = "identity_unknown"
                    return {"ok": False, "state": "identity_unknown", "pid": 0}
                if count != 0:
                    return {"ok": existing["state"] not in ("stopping", "draining", "identity_unknown"), "state": existing["state"],
                            "pid": (existing.get("worker") or {}).get("pid", 0)}
                if self.now() < existing["retry_at"] or existing["failures"] >= 3:
                    return {"ok": False, "state": "restart_limited"}
                if not existing["owned"].close():
                    return {"ok": False, "state": "identity_unknown"}
            failures = existing["failures"] if existing else failed["failures"]
            legacy = Path(self.context["data"]) / (("watchdog_" if kind == "watch" else "stream_") + entry["tag"] + ".pid")
            if legacy.exists():
                try:
                    if legacy.is_symlink() or legacy.stat().st_size > 64:
                        raise ValueError
                    previous = process_identity(int(legacy.read_text(encoding="utf-8").strip()))
                    if previous["state"] != "dead":
                        self.last_error = "existing_role_not_owned"
                        return {"ok": False, "state": "identity_unverified"}
                except (OSError, ValueError, KeyError):
                    self.last_error = "existing_role_identity_unavailable"
                    return {"ok": False, "state": "identity_unverified"}
            nonce = secrets.token_hex(24)
            stop_path = self.directory / "stops" / (Path(role_receipt_path(self.context, kind, entry["tag"])).stem + ".json")
            env = self.child_env()
            env.update(DY_CUSTOMER_ROLE_NONCE=nonce, DY_CUSTOMER_STOP=str(stop_path))
            try:
                owned = self.spawn(self.command(kind, entry), env=env, cwd=self.context["root"],
                                   log_path=self.directory / "logs" / (stop_path.stem + ".log"))
            except Exception:
                self.last_error = "role_start_failed"
                failures += 1
                self.launch_failures[key] = {"failures": failures, "retry_at": self.now() + min(30, 2 ** failures)}
                return {"ok": False, "state": "failed", "error": self.last_error}
            self.launch_failures.pop(key, None)
            self.roles[key] = {"owned": owned, "kind": kind, "tag": entry["tag"], "nonce": nonce,
                "stop_path": stop_path, "receipt": Path(role_receipt_path(self.context, kind, entry["tag"])),
                "worker": None, "state": "starting", "started": self.now(), "failures": failures,
                "retry_at": 0, "stop_requested": False}
            return {"ok": True, "state": "starting", "pid": 0}

    def stop_role(self, kind, tag, *, suppress=True):
        from local_context import validate_role_tag
        if kind not in ("watch", "record"):
            return {"ok": False, "state": "invalid_role"}
        validate_role_tag(tag)
        with self.lock:
            key = kind + ":" + tag
            if suppress and not self.intent_error and key not in self.suppressed:
                self.suppressed.add(key)
                self.save_intents()
            role = self.roles.get(key)
            if not role:
                # Historical PID files never grant this supervisor ownership.
                return {"ok": True, "state": "not_owned", "pid": 0}
            role["stop_requested"] = True
            role["state"] = "stopping"
            worker = role.get("worker")
            if worker and same_identity(worker, process_identity(worker["pid"])):
                atomic_json(role["stop_path"], {"schema": 1, "action": "stop",
                    "instance_id": self.context["instance_id"], "pid": worker["pid"], "created": worker["created"]})
            if role["owned"].active_count() == 0 and role["owned"].close():
                role["state"] = "stopped"
                return {"ok": True, "state": "stopped", "pid": 0}
            return {"ok": False, "state": "stopping", "pid": (worker or {}).get("pid", 0)}

    def instance_status(self, kind, tag):
        with self.lock:
            role = self.roles.get(kind + ":" + tag)
            worker = role.get("worker") if role else None
            verified = bool(worker and same_identity(worker, process_identity(worker["pid"])))
            return {"kind": kind, "pid": worker["pid"] if verified else 0,
                    "running": verified and role["state"] == "running", "stale_pid": 0,
                    "state": role["state"] if role else "stopped"}

    def spawn_manual(self, command, *, env):
        with self.lock:
            if self.stopping:
                raise AppError("application_stopping")
            env = dict(env, DY_CUSTOMER_CONTEXT=str(self.directory / "context.json"),
                       DY_CUSTOMER_STOP=str(self.directory / "application-stop.json"))
            owned = self.spawn(command, env=env, cwd=self.context["root"], log_path=self.directory / "manual.log")
            self.manual.append(owned)
            return owned.proc

    def _browser_tick(self, wanted):
        role_wanted = wanted
        wanted = wanted or self.browser_manual
        if self.command_builder:  # Tests inject toy roles; no browser launch.
            return
        if self.browser:
            count = self.browser.active_count()
            if count == 0 and self.browser.close():
                self.browser = None
                self.browser_manual = False
                wanted = role_wanted
                self.browser_error = "browser_closed"
                self.browser_failures += 1
                self.browser_retry = self.now() + min(30, 2 ** self.browser_failures)
            elif self.browser.proc.poll() is None and same_identity(self.browser.identity, process_identity(self.browser.proc.pid)):
                endpoint = listener_pid(self.context["cdp_port"])
                if endpoint["state"] == "listening" and endpoint["pids"] == [self.browser.proc.pid]:
                    atomic_json(self.directory / "browser-owner.json", {"schema": 1,
                        "instance_id": self.context["instance_id"], "pid": self.browser.proc.pid,
                        "created": self.browser.identity["created"], "port": self.context["cdp_port"],
                        "profile": self.context["profile"]})
                    self.browser_error = ""
                    if self.last_error.startswith("browser_"):
                        self.last_error = ""
                else:
                    atomic_json(self.directory / "browser-owner.json", {"schema": 1, "verified": False})
                    self.last_error = "browser_waiting_for_owned_port"
                    self.browser_error = self.last_error
                    if self.now() - self.browser_started > BROWSER_START_TIMEOUT_SECONDS:
                        self.last_error = "browser_start_timeout"
                        self.browser_error = self.last_error
                        if self.browser.close(force=True):
                            self.browser = None
                            self.browser_manual = False
                            self.browser_failures += 1
                            self.browser_retry = self.now() + min(30, 2 ** self.browser_failures)
                return
            else:
                return  # Unknown descendants cannot authorize a second browser.
        if not wanted or self.stopping or self.browser_failures >= 3 or self.now() < self.browser_retry:
            return
        endpoint = listener_pid(self.context["cdp_port"])
        if endpoint["state"] != "vacant":
            self.last_error = "browser_port_conflict" if endpoint["state"] == "listening" else "browser_identity_unavailable"
            self.browser_error = self.last_error
            self.browser_manual = False
            return
        import run_watchdog
        env = self.child_env()
        env.update(WATCH_BROWSER_PROFILE=self.context["profile"], WATCH_DEBUG_PORT=str(self.context["cdp_port"]),
                   CDP_PORT=str(self.context["cdp_port"]))
        executable = run_watchdog.resolve_chrome(env)
        if not executable:
            self.last_error = "browser_missing"
            self.browser_error = self.last_error
            self.browser_manual = False
            return
        command = [executable, "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=" + str(self.context["cdp_port"]),
            "--user-data-dir=" + self.context["profile"], "--no-first-run", "--no-default-browser-check", "about:blank"]
        try:
            self.browser = self.spawn(command, env=env, cwd=self.context["root"], log_path=self.directory / "browser.log")
            self.browser_started = self.now()
            self.browser_generation = secrets.token_hex(24)
            self.browser_error = ""
        except Exception:
            self.browser_failures += 1
            self.browser_retry = self.now() + min(30, 2 ** self.browser_failures)
            self.last_error = "browser_start_failed"
            self.browser_error = self.last_error
            self.browser_manual = False

    def login_owner(self):
        """Current exact owned browser; no HTTP/CDP probe or process adoption."""
        from local_context import browser_owner_valid
        with self.lock:
            browser = self.browser
            if self.stopping or browser is None or not self.browser_generation:
                return None
            if browser.active_count() in (0, None) or browser.proc.poll() is not None:
                return None
            if not same_identity(browser.identity, process_identity(browser.proc.pid)):
                return None
            endpoint = listener_pid(self.context["cdp_port"])
            if endpoint.get("state") != "listening" or endpoint.get("pids") != [browser.proc.pid]:
                return None
            if not browser_owner_valid(self.context, identity_reader=process_identity, listener_reader=listener_pid):
                return None
            return (self.browser_generation, browser.identity["pid"], browser.identity["created"])

    def login_browser_status(self):
        with self.lock:
            if self.stopping:
                return {"state": "stopping", "reason": "application_stopping"}
            if self.login_owner() is not None:
                return {"state": "ready", "reason": "owned_browser_ready"}
            if self.browser is not None:
                count = self.browser.active_count()
                if count == 0:
                    return {"state": "closed", "reason": "browser_closed"}
                if (count is None or self.browser.proc.poll() is not None
                        or not same_identity(self.browser.identity, process_identity(self.browser.proc.pid))):
                    return {"state": "identity_unknown", "reason": "browser_identity_unavailable"}
                endpoint = listener_pid(self.context["cdp_port"])
                if endpoint.get("state") == "unknown":
                    return {"state": "identity_unknown", "reason": "browser_identity_unavailable"}
                if endpoint.get("state") == "listening" and endpoint.get("pids") != [self.browser.proc.pid]:
                    return {"state": "conflict", "reason": "browser_port_conflict"}
                return {"state": "starting", "reason": "browser_waiting_for_owned_port"}
            states = {"browser_missing": "missing", "browser_port_conflict": "conflict", "browser_identity_unavailable": "identity_unknown",
                "browser_closed": "closed", "browser_start_failed": "failed", "browser_start_timeout": "failed"}
            if self.browser_error in states:
                return {"state": states[self.browser_error], "reason": self.browser_error}
            if self.browser_failures >= 3:
                return {"state": "failed", "reason": "browser_restart_limited"}
            return {"state": "starting" if self.browser_manual else "not_prepared",
                    "reason": "prepare_requested" if self.browser_manual else "not_prepared"}

    def request_login_browser(self):
        with self.lock:
            if self.stopping:
                return False
            if self.login_owner() is not None:
                return True
            if self.browser is not None:
                count = self.browser.active_count()
                if count == 0 and self.browser.close():
                    self.browser = None
                elif (count is None or self.browser.proc.poll() is not None
                        or not same_identity(self.browser.identity, process_identity(self.browser.proc.pid))):
                    self.browser_error = "browser_identity_unavailable"
                    return False
            self.browser_manual = True
            self.browser_failures, self.browser_retry = 0, 0.0
            self.browser_error = ""
            if self.last_error.startswith("browser_"):
                self.last_error = ""
            return True

    def tick(self, roster):
        # ASR owns its metadata/Job state independently of the short role lock.
        # It never scans audio or loads a model in this supervisor process.
        self.asr.tick()
        with self.lock:
            desired = {(kind, entry["tag"]): entry for entry in roster["anchors"]
                if entry.get("enabled") for kind in ("watch", "record")
                if entry.get(kind) and kind + ":" + entry["tag"] not in self.suppressed and not self.intent_error}
            for role in list(self.roles.values()):
                if self.stopping or (role["kind"], role["tag"]) not in desired:
                    self.stop_role(role["kind"], role["tag"], suppress=False)
                count = role["owned"].active_count()
                if count == 0:
                    if role["owned"].close():
                        if role["state"] not in ("stopped", "failed"):
                            role["failures"] += 1
                            role["retry_at"] = self.now() + min(30, 2 ** role["failures"])
                        role["state"] = "stopped" if role["stop_requested"] else "failed"
                    continue
                if count is None:
                    role["state"] = "identity_unknown"
                    continue
                try:
                    receipt = read_json(role["receipt"], {})
                    valid = (receipt.get("instance_id") == self.context["instance_id"]
                        and receipt.get("role") == role["kind"] + ":" + role["tag"]
                        and receipt.get("nonce") == role["nonce"])
                    worker = role["owned"].worker_identity(receipt) if valid else None
                except AppError:
                    worker = None
                if worker:
                    role["worker"] = worker
                    if role["stop_requested"]:
                        self.stop_role(role["kind"], role["tag"], suppress=False)
                    else:
                        role["state"] = "running"
                elif role.get("worker"):
                    role["state"] = "stopping" if role["stop_requested"] else "draining"
                elif not role.get("worker") and role["state"] not in ("failed", "stopped") and self.now() - role["started"] > 20:
                    # Before an ACK there is no verified worker to ask to stop.
                    # The private Job still proves every resource was ours.
                    role["owned"].close(force=True)
                    role["state"] = "failed"
                    role["failures"] += 1
                    role["retry_at"] = self.now() + min(30, 2 ** role["failures"])
            self.manual = [owned for owned in self.manual if not (owned.active_count() == 0 and owned.close())]
            self._browser_tick(bool(desired))
            if not self.stopping:
                for (kind, tag), entry in desired.items():
                    self.start_role(kind, entry, explicit=False)

    def begin_stop(self):
        with self.lock:
            self.stopping = True
            owner = process_identity(os.getpid())
            if owner["state"] == "alive":
                try:
                    atomic_json(self.directory / "application-stop.json", {"schema": 1, "action": "stop",
                        "instance_id": self.context["instance_id"], "pid": owner["pid"], "created": owner["created"]})
                except (AppError, OSError):
                    self.last_error = "stop_request_unavailable"
            for role in self.roles.values():
                try:
                    self.stop_role(role["kind"], role["tag"], suppress=False)
                except (AppError, OSError):
                    role["state"] = "stopping"
                    self.last_error = "stop_request_unavailable"
        self.asr.begin_stop()

    def recover(self):
        with self.lock:
            if self.stopping:
                return False
            for role in self.roles.values():
                if role["owned"].active_count() == 0:
                    role.update(failures=0, retry_at=0)
            self.browser_failures, self.browser_retry = 0, 0
            self.launch_failures.clear()
            self.last_error = ""
            return True

    def drained(self):
        if not self.asr.drained():
            return False
        with self.lock:
            if self.inflight or self.login.busy or self.manual or any(role["owned"].active_count() != 0 for role in self.roles.values()):
                return False
            if self.browser:
                if not self.browser.close(force=True):
                    return False
                self.browser = None
            return True

    def snapshot(self):
        with self.lock:
            roles = {key: {"state": value["state"], "failures": value["failures"],
                "pid": (value.get("worker") or {}).get("pid", 0), "running": value["state"] == "running",
                "collection_health": "not_confirmed"}
                for key, value in self.roles.items()}
            roles.update({key: {"state": "restart_limited" if value["failures"] >= 3 else "failed",
                "failures": value["failures"], "pid": 0, "running": False, "collection_health": "unknown"}
                for key, value in self.launch_failures.items()})
            return {"roles": roles, "manual_writers": len(self.manual), "inflight_requests": self.inflight,
                    "last_error": self.last_error, "stopping": self.stopping,
                    "suppressed": sorted(self.suppressed), "intent_error": self.intent_error,
                    "asr": self.asr.snapshot()}


def create_board(context, manager, owner):
    import assist_board_server as board
    import anchor_roster
    import anchor_manager
    from http.server import ThreadingHTTPServer
    from backfill_tasks import TaskStore
    from local_access import LocalAccess
    from scripts.local_material_api import MaterialAPI, MAX_BODY_BYTES
    from scripts.notification_settings import NotificationSettingsAPI
    anchor_roster.ROSTER_PATH = Path(context["roster"])
    anchor_manager.DATA, anchor_manager.ROOT = Path(context["data"]), Path(context["root"])
    anchor_manager.PY = Path(context["python"])
    anchor_manager.configure_customer(manager)
    class LocalStore(board.Store):
        def _live_roster(self):
            with anchor_roster.transaction(context["roster"]):
                return super()._live_roster()
    store = LocalStore(context["data"], roster_path=context["roster"])
    access = LocalAccess(context["instance_id"] + ":" + str(context["port"]))
    materials = MaterialAPI(context, stopping=lambda: manager.stopping)
    notifications = NotificationSettingsAPI(context["config"], stopping=lambda: manager.stopping)
    broadcaster = board.broadcast.Broadcaster(store.snapshot, interval=board.SSE_INTERVAL,
        ping_every=board.PING_EVERY, feed_fn=store.take_new_danmu)

    class Handler(board.Handler):
        config_path = context["config"]
        roster_path = context["roster"]
        dist_dir = context["dist"]
        users_path = str(Path(context["data"]) / "board_users.json")
        html_path = str(Path(context["root"]) / "scripts/assist_board.html")
        started_at = time.time()
        auth_db = None
        throttle = None

        def _send(self, code, ctype, body, extra_headers=None):
            headers = {"X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'",
                       "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}
            headers.update(extra_headers or {})
            return super()._send(code, ctype, body, headers)

        def _reject_json(self, code, obj):
            if (code == 503 and self.path.split("?", 1)[0] == "/api/materials/refresh"
                    and manager.stopping and isinstance(obj, dict) and set(obj) == {"error"}):
                from scripts.local_material_api import error_body
                obj = error_body("materials_stopping")
            return super()._reject_json(code, obj)

        def _auth_user(self):
            return "本机用户" if access.authenticated(self) else None

        def _csrf_ok(self):
            return access.source_ok(self) and access.write_allowed(self)

        def _serve_index(self):
            # A missing dist must never fall back to the legacy password page.
            try:
                path = self._dist_index()
                if not path:
                    raise FileNotFoundError
                return self._send(200, "text/html; charset=utf-8", Path(path).read_bytes())
            except OSError:
                return self._json(503, {"error": "本地看板前端不可用，请检查应用安装。"})

        def _local_request(self, callback, *, mutation=False):
            if not access.source_ok(self):
                return self._reject_json(403, {"code": "local_origin", "error": "仅允许本机同源访问。"})
            path = self.path.split("?", 1)[0]
            if path == "/legacy" or path.startswith("/legacy/"):
                return self._reject_json(410, {"error": "本地模式请使用新版看板。"})
            if path in ("/api/login", "/api/logout", "/api/passwd"):
                return self._reject_json(404, {"error": "本地模式无需看板账号密码。"})
            if path == "/api/access" and self.command == "GET":
                return self._json(200, {"mode": "local"})
            if path == "/api/local/session" and self.command == "POST":
                def bootstrap():
                    try:
                        self._request_body()  # Keep the existing bounded body/transport rules.
                    except board.RequestBodyError as error:
                        return self._reject_json(error.code, {"error": error.message})
                    body, cookie = access.bootstrap(self)
                    return self._json(200, body, {"Set-Cookie": cookie})
                return self._mutation(bootstrap)
            if self.command == "GET" and (path == "/api/health" or
                    not (path.startswith("/api/") or path in ("/snapshot", "/sse"))):
                if path == "/api/health" or path.startswith(board.STATIC_PREFIXES) or path == "/favicon.ico":
                    return callback()
                return self._serve_index()
            if not access.authenticated(self):
                return self._reject_json(401, {"code": "local_session_required", "error": "本地会话已失效，请重新连接。"})
            if mutation and not access.write_allowed(self):
                return self._reject_json(403, {"code": "local_token_required", "error": "操作凭证已失效，请重新连接后再次操作。"})
            if path == "/api/me" and self.command == "GET":
                return self._json(200, {"mode": "local", "user": "本机用户", "role": "local"})
            if path == "/api/browser-login":
                if self.command == "GET":
                    return self._browser_login()
                if self.command == "POST":
                    return self._mutation(self._browser_login)
                return self._reject_json(405, {"error": "不支持此登录辅助操作。"})
            if path == "/api/notifications/feishu":
                if self.command == "GET":
                    code, body = notifications.get()
                    return self._json(code, body)
                if self.command == "POST":
                    return self._mutation(self._notifications)
                return self._reject_json(405, {"code": "invalid_request", "error": "不支持此通知操作。"})
            if path == "/api/asr":
                if self.command == "GET":
                    return self._asr_status()
                if self.command == "POST":
                    return self._mutation(self._asr_status)
                return self._reject_json(405, {"error": "不支持此转写操作。"})
            if path == "/api/materials" or path.startswith("/api/materials/"):
                if path == "/api/materials/refresh" and self.command == "POST":
                    return self._mutation(self._materials)
                return self._materials()
            return self._mutation(callback) if mutation else callback()

        def _materials(self):
            from scripts.local_material_api import ROUTES, error_body
            path, _, query = self.path.partition("?")
            if path not in ROUTES:
                return self._reject_json(404, error_body("material_route_not_found"))
            if self.command == "GET" and path != "/api/materials/refresh":
                code, body = materials.get(path, query)
                return self._json(code, body)
            if self.command != "POST" or path != "/api/materials/refresh":
                return self._reject_json(405, error_body("material_method_not_allowed"))
            try:
                if self._declared_length() > MAX_BODY_BYTES:
                    return self._reject_json(413, error_body("invalid_refresh_request"))
                raw = self._request_body()
            except board.RequestBodyError as error:
                return self._reject_json(error.code, error_body("invalid_refresh_request"))
            code, body = materials.post(query, raw)
            return self._json(code, body)

        def _notifications(self):
            try:
                if self._declared_length() > 16384:
                    return self._reject_json(413, {"ok": False, "code": "invalid_request", "delivered": False})
                body = self._read_json()
            except board.RequestBodyError:
                return self._reject_json(400, {"ok": False, "code": "invalid_request", "delivered": False})
            code, body = notifications.post(body)
            return self._json(code, body)

        def _asr_status(self):
            lane = getattr(manager, "asr", None)
            if lane is None:
                return self._reject_json(503, {"code": "asr_unavailable", "error": "本实例转写状态尚不可用。"})
            if self.command == "GET":
                return self._json(200, {"ok": True, "status": lane.snapshot()})
            try:
                body = self._read_json()
            except board.RequestBodyError as error:
                return self._reject_json(error.code, {"error": error.message})
            if not isinstance(body, dict) or set(body) != {"action"} or body.get("action") != "retry":
                return self._reject_json(400, {"code": "invalid_asr_action", "error": "请选择重新检查或重试可重试失败。"})
            return self._json(200, lane.retry())

        def _browser_login(self):
            guide = getattr(manager, "login", None)
            if guide is None:
                return self._reject_json(503, {"code": "login_guide_unavailable", "error": "本实例登录辅助尚不可用。"})
            if self.command == "GET":
                return self._json(200, {"ok": True, "status": guide.snapshot()})
            try:
                body = self._read_json()
            except board.RequestBodyError as error:
                return self._reject_json(error.code, {"error": error.message})
            if (not isinstance(body, dict) or set(body) - {"action", "tag"}
                    or body.get("action") not in ("prepare", "open", "check")
                    or body["action"] != "check" and "tag" in body):
                return self._reject_json(400, {"code": "invalid_login_action", "error": "登录辅助参数无效。"})
            action, entry = body["action"], None
            if action == "check" and body.get("tag"):
                tag = body["tag"]
                if (not isinstance(tag, str) or not 1 <= len(tag) <= 40
                        or any(c in '/\\:*?"<>|' or ord(c) < 32 for c in tag)):
                    return self._reject_json(400, {"code": "invalid_login_target", "error": "请选择已登记的主播。"})
                try:
                    rows = [row for row in self._roster()["anchors"] if row.get("tag") == tag]
                    if len(rows) != 1 or anchor_roster.parse_anchor_uid(rows[0]["sec_uid"]) != rows[0]["sec_uid"]:
                        return self._reject_json(400, {"code": "invalid_login_target", "error": "所选主播尚无可检查的抖音通道。"})
                    entry = {"tag": tag, "sec_uid": rows[0]["sec_uid"]}
                except (OSError, ValueError, TypeError, KeyError):
                    return self._reject_json(503, {"code": "roster_unavailable", "error": "主播名册暂不可读，请重新确认。"})
            elif action == "check" and "tag" in body and not isinstance(body["tag"], str):
                return self._reject_json(400, {"code": "invalid_login_target", "error": "请选择已登记的主播。"})
            return self._json(200, guide.request(action, entry))

        def _health(self):
            return dict(super()._health(), instance_id=context["instance_id"], owner=owner)

        def _backfill_store(self):
            return TaskStore(context["config"], context["data"], python_exe=context["python"], spawn=manager.spawn_manual)

        def _llm_model_settings(self, action):
            if action != "test":
                return super()._llm_model_settings(action)
            body = self._read_json()
            if not isinstance(body, dict):
                return self._reject_json(400, {"error": "请求体必须是 JSON 对象"})
            result = board.board_llm.test_provider_connection(context["config"], context["data"], body,
                application_guard=lambda: not manager.stopping)
            return self._json(200, result)

        def _roster(self):
            with anchor_roster.transaction(context["roster"]):
                return anchor_roster.load(context["roster"])

        def _mutation(self, callback):
            with manager.lock:
                if manager.stopping:
                    return self._reject_json(503, {"error": "应用正在停止，请等待写入任务收尾。"})
                manager.inflight += 1
            try:
                return callback()
            finally:
                with manager.lock:
                    manager.inflight -= 1

        def do_POST(self):
            return self._local_request(super().do_POST, mutation=True)

        def do_PATCH(self):
            return self._local_request(super().do_PATCH, mutation=True)

        def do_DELETE(self):
            return self._local_request(super().do_DELETE, mutation=True)

        def do_GET(self):
            # This GET may reconcile its durable task receipt. SSE remains an
            # unbounded read stream and must not hold the shutdown counter.
            return self._local_request(super().do_GET,
                mutation=self.path.split("?", 1)[0] == "/api/review/backfill")

        def do_HEAD(self):
            return self._local_request(lambda: self._json(405, {"error": "method not allowed"}), mutation=True)

        def do_OPTIONS(self):
            return self._local_request(lambda: self._json(405, {"error": "method not allowed"}), mutation=True)

    Handler.store, Handler.broadcaster = store, broadcaster

    class ExclusiveServer(ThreadingHTTPServer):
        allow_reuse_address = False
        daemon_threads = True
        def server_bind(self):
            if os.name == "nt":
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()

    server = ExclusiveServer(("127.0.0.1", context["port"]), Handler)
    server.materials = materials
    broadcaster.start()
    threading.Thread(target=store.poll_loop, daemon=True, name="local-board-data").start()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .1}, daemon=True)
    thread.start()
    return server, thread, broadcaster


def supervise(context):
    directory = runtime_dir(context)
    directory.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None:
        sys.stdout = (directory / "app.log").open("a", encoding="utf-8", buffering=1)
    if sys.stderr is None:
        sys.stderr = (directory / "app.log").open("a", encoding="utf-8", buffering=1)
    guard = FileLock(directory / "run")
    if not guard.acquire(blocking=False):
        return 3
    owner = process_identity(os.getpid())
    if owner["state"] != "alive":
        guard.release()
        return 4
    manager = RoleManager(context)
    server = thread = broadcaster = None
    nonce = os.environ.get("DY_LOCAL_LAUNCH_NONCE", "")
    def publish(state, error="", error_type="", dependency=""):
        atomic_json(directory / "instance.json", dict(manager.snapshot(), schema=1, owner=owner,
            instance_id=context["instance_id"], context=context, launch_nonce=nonce, updated_at=time.time(),
            state=state, ok=state in ("waiting", "running", "stopped"), error=error, error_type=error_type,
            dependency=dependency, board_ready=server is not None))
    try:
        atomic_json(directory / "context.json", context)
        bind_customer_context(context, directory / "context.json")
        from scripts.local_context import activate_customer_environment
        activate_customer_environment(context)
        publish("starting")
        _config, roster = check_configuration(context)
        server, thread, broadcaster = create_board(context, manager, owner)
        last_recover = None
        while True:
            request = read_json(directory / "control.json", {})
            if request.get("instance_id") == context["instance_id"] and request.get("owner") == owner:
                if request.get("action") == "stop":
                    manager.begin_stop()
                elif request.get("action") == "recover" and request.get("nonce") != last_recover:
                    manager.recover()
                    last_recover = request.get("nonce")
            try:
                _config, roster = check_configuration(context)
                manager.last_error = ""
            except AppError as error:
                manager.last_error = str(error)
                # Bad/missing state cannot authorize new work; stop current roles.
                roster = {"anchors": []}
            manager.tick(roster)
            if manager.stopping and manager.drained():
                server.shutdown()
                server.server_close()
                server = None
                broadcaster.stop()
                thread.join(2)
                publish("stopped")
                return 0
            snapshot = manager.snapshot()
            state = "stopping" if manager.stopping else "waiting" if not snapshot["roles"] else "running"
            if not manager.stopping and (manager.intent_error or manager.last_error or any(r["state"] in ("failed", "draining", "restart_limited", "identity_unknown") for r in snapshot["roles"].values())):
                state = "degraded"
            if not thread.is_alive():
                manager.begin_stop()
                manager.last_error = "board_thread_exited"
                state = "stopping"
            publish(state)
            time.sleep(.2)
    except (KeyboardInterrupt, Exception) as error:
        manager.begin_stop()
        # Keep this process and all ownership evidence while writers drain.
        while not manager.drained():
            try:
                manager.tick({"anchors": []})
            except (AppError, OSError):
                manager.last_error = "stop_request_unavailable"
            try:
                publish("stopping", "startup_failed" if server is None else "application_error", type(error).__name__)
            except (AppError, OSError):
                pass  # Retain Job handles and writers even when state storage fails.
            time.sleep(.2)
        if server:
            server.shutdown()
            server.server_close()
            server = None
        if broadcaster:
            broadcaster.stop()
        publish("error", str(error) if isinstance(error, AppError) else "dependency_missing" if isinstance(error, ModuleNotFoundError) else "startup_failed",
                type(error).__name__, (error.name or "") if isinstance(error, ModuleNotFoundError) else "")
        return 1
    finally:
        guard.release()


def start(context, *, wait=15.0):
    directory = runtime_dir(context)
    directory.mkdir(parents=True, exist_ok=True)
    with FileLock(directory / "commands"):
        current = status(context)
        if current["state"] in ("instance_conflict", "instance_context_conflict", "identity_unknown", "board_identity_unavailable", "stale_instance"):
            if current.get("process_state") != "reused":
                return dict(current, ok=False)
            # A different creation identity proves the old supervisor exited.
            # It grants no ownership of the reused PID: only lock/port checks
            # below may admit a newly spawned application.
        if current.get("process_state") == "alive":
            if current["state"] == "stopped":
                return dict(current, ok=False, state="stopping")
            return current
        guard = FileLock(directory / "run")
        if not guard.acquire(blocking=False):
            deadline = time.monotonic() + 2.0
            acquired = False
            if current.get("process_state") in ("dead", "reused"):
                while time.monotonic() < deadline:
                    time.sleep(.02)
                    if guard.acquire(blocking=False):
                        acquired = True
                        break
            if not acquired:
                return {"ok": False, "state": "starting_or_identity_unknown"}
        guard.release()
        check_configuration(context)
        endpoint = listener_pid(context["port"])
        if endpoint["state"] != "vacant":
            return {"ok": False, "state": "port_conflict" if endpoint["state"] == "listening" else "port_state_unavailable"}
        nonce = secrets.token_hex(24)
        command = installed_launcher_command(context)
        if command is None:
            command = [context["python"], "-B", str(ROOT / "scripts/local_app.py"), "run"]
            for name in ("root", "config", "data", "roster", "dist", "python", "port", "cdp_port"):
                command += ["--" + name.replace("_", "-"), str(context[name])]
        env = dict(installed_child_environment(context), DY_LOCAL_LAUNCH_NONCE=nonce, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
        with (directory / "app.log").open("ab", buffering=0) as output:
            try:
                proc = subprocess.Popen(command, cwd=context["root"], env=env, stdin=subprocess.DEVNULL,
                    stdout=output, stderr=output, close_fds=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            except OSError:
                return {"ok": False, "state": "python_unavailable" if command[0] == context["python"] else "launcher_unavailable"}
        launched = process_identity(proc.pid)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            current = status(context)
            owner = current.get("owner", {})
            if (current.get("launch_nonce") == nonce and process_fact(owner) == "alive"
                    and (owner.get("pid") == proc.pid or process_parent_pid(owner.get("pid", 0)) == proc.pid)
                    and launched["state"] == "alive" and owner["created"] >= launched["created"]):
                if current.get("board_ready") or current.get("state") == "error":
                    return current
            if proc.poll() is not None:
                return dict(current, ok=False, state="startup_failed")
            time.sleep(.1)
        return {"ok": False, "state": "startup_timeout", "instance_id": context["instance_id"],
                "note": "后台状态待确认；重新查询，不重复创建实例。"}


def control(context, action, *, wait=5.0):
    directory = runtime_dir(context)
    with FileLock(directory / "commands"):
        current = status(context)
        if current.get("process_state") != "alive" or not same_identity(current.get("owner"), process_identity(current["owner"]["pid"])):
            return current if action == "stop" else start(context, wait=wait)
        if action == "recover" and current.get("stopping"):
            return current
        atomic_json(directory / "control.json", {"schema": 1, "action": action,
            "instance_id": context["instance_id"], "owner": current["owner"], "nonce": secrets.token_hex(16)})
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        current = status(context)
        if action == "recover" or current["state"] in ("stopped", "exited", "error"):
            return current
        time.sleep(.1)
    return status(context)


def main(argv=None):
    installed = bootstrap_installed()
    parser = argparse.ArgumentParser(description="客户本地应用启动/停止/状态/恢复与登录后自启")
    parser.add_argument("action", choices=("start", "stop", "status", "recover", "run", "autostart"))
    parser.add_argument("autostart_action", nargs="?", choices=("plan", "status", "enable", "disable"))
    parser.add_argument("--root", default=str(ROOT))
    for name in ("config", "data", "roster", "dist", "python"):
        parser.add_argument("--" + name)
    parser.add_argument("--port", type=int, default=installed["port"] if installed else 8765)
    parser.add_argument("--cdp-port", type=int, default=installed["cdp_port"] if installed else 29222)
    parser.add_argument("--open", action="store_true", help="本次成功启动后打开看板；后台恢复不打开")
    args = parser.parse_args(argv)
    try:
        context = context_from_args(args)
        if args.action == "run":
            return supervise(context)
        if args.action == "autostart":
            value = autostart_manage(args.autostart_action or "plan", context)
        elif args.action == "start":
            value = start(context)
        elif args.action == "status":
            value = status(context)
        else:
            value = control(context, args.action)
        if args.action == "start" and args.open and value.get("ok") and value.get("board_ready"):
            import webbrowser
            webbrowser.open("http://127.0.0.1:%s/" % context["port"])
        print(json.dumps(value, ensure_ascii=False))
        return 0 if value.get("ok", value.get("state") == "stopped") else 1
    except (AppError, OSError, ValueError) as error:
        print(json.dumps({"ok": False, "state": str(error) if isinstance(error, AppError) else "application_state_unavailable",
                          "error_type": type(error).__name__}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
