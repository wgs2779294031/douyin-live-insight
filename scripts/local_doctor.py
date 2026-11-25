"""External standard-library checks; target imports are a separate gated action."""
from __future__ import annotations

import argparse
import contextlib
import ctypes
from ctypes import wintypes
import importlib
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import stat
import sys
import uuid

NATIVE_IMPORTS = (
    "numpy", "av", "ctranslate2", "faster_whisper", "onnxruntime", "tokenizers",
    "pydantic_core", "cryptography.hazmat.bindings._rust", "curl_cffi", "py_mini_racer",
    "win32api", "win32file", "win32event", "win32process", "pythoncom", "pywintypes",
)
VC_URL = "https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170"
EDGE_URL = "https://www.microsoft.com/edge/download"


class DoctorError(ValueError):
    pass


def _result(ok, code, facts=None, not_checked=None, next_steps=None, exit_code=None):
    return dict(ok=bool(ok), code=code, facts=facts or {}, not_checked=not_checked or [],
                next_steps=next_steps or [], exit=(0 if ok else 1) if exit_code is None else exit_code)


def _target(value):
    text = os.fspath(value)
    path = Path(text)
    if (not text or not path.is_absolute() or text.startswith(("\\", "/"))
            or "://" in text or any(ord(char) < 32 for char in text)
            or any(char in ':<>"|?*' for char in text[2:])
            or ".." in path.parts):
        raise DoctorError("invalid_target")
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise DoctorError("invalid_target")
    return path.absolute()


def _regular(path, *, single_link=True):
    try:
        info = Path(path).lstat()
        return (stat.S_ISREG(info.st_mode) and (not single_link or info.st_nlink == 1)
                and not getattr(info, "st_file_attributes", 0) & 0x400)
    except OSError:
        return False


class _Disposition(ctypes.Structure):
    _fields_ = [("DeleteFile", ctypes.c_ubyte)]


def _kernel():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    api.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    api.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    api.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    api.SetFileInformationByHandle.restype = wintypes.BOOL
    return api


def _final_path(api, handle):
    buffer = ctypes.create_unicode_buffer(32768)
    count = api.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
    if not 0 < count < len(buffer):
        raise DoctorError("state_probe_unconfirmed")
    value = buffer.value
    if value.startswith("\\\\?\\"):
        value = value[4:]
    return os.path.normcase(value)


@contextlib.contextmanager
def _directory_leases(path, api):
    """Pin existing ordinary ancestors; never create or enumerate state content."""
    import msvcrt
    fds = []
    try:
        for directory in reversed((path, *path.parents)):
            before = directory.lstat()
            if not stat.S_ISDIR(before.st_mode) or getattr(before, "st_file_attributes", 0) & 0x400:
                raise DoctorError("state_probe_unconfirmed")
            handle = api.CreateFileW(str(directory), 0x81, 3, None, 3, 0x2200000, None)
            if handle in (None, ctypes.c_void_p(-1).value):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            except BaseException:
                api.CloseHandle(handle)
                raise
            fds.append(fd)
            opened = os.fstat(fd)
            if ((opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                    or not stat.S_ISDIR(opened.st_mode)
                    or getattr(opened, "st_file_attributes", 0) & 0x400
                    or _final_path(api, handle) != os.path.normcase(str(directory))):
                raise DoctorError("state_probe_unconfirmed")
        yield
    finally:
        first_error = None
        for fd in reversed(fds):
            try:
                os.close(fd)
            except OSError as error:
                first_error = first_error or error
        if first_error is not None:
            raise first_error


def _state_probe(state):
    """Only a freshly created owned leaf is ever marked for deletion."""
    path = Path(state)
    if not path.is_dir():
        return {"state": "missing", "created_directories": False}
    if os.name != "nt":
        return {"state": "not_checked", "reason": "windows_required"}
    import msvcrt
    name = ".doctor-" + uuid.uuid4().hex + ".tmp"
    probe, fd, created, may_mark = path / name, None, False, False
    try:
        api = _kernel()
        with _directory_leases(path, api):
            handle = api.CreateFileW(str(probe), 0xC0010000, 0, None, 1, 0x200100, None)
            if handle in (None, ctypes.c_void_p(-1).value):
                raise ctypes.WinError(ctypes.get_last_error())
            created = True
            try:
                fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
            except BaseException:
                api.CloseHandle(handle)
                raise
            try:
                before = os.fstat(fd)
                expected = os.path.normcase(str(probe))
                if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                        or getattr(before, "st_file_attributes", 0) & 0x400
                        or _final_path(api, handle) != expected):
                    raise DoctorError("state_probe_unconfirmed")
                payload = b"customer doctor owned write probe\n"
                if os.write(fd, payload) != len(payload):
                    raise DoctorError("state_probe_unconfirmed")
                os.fsync(fd)
                os.lseek(fd, 0, os.SEEK_SET)
                if os.read(fd, len(payload) + 1) != payload:
                    raise DoctorError("state_probe_unconfirmed")
                after = os.fstat(fd)
                if ((after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
                        or after.st_nlink != 1 or after.st_size != len(payload)
                        or _final_path(api, handle) != expected):
                    raise DoctorError("state_probe_unconfirmed")
                mark = _Disposition(1)
                may_mark = True
                if not api.SetFileInformationByHandle(handle, 4, ctypes.byref(mark), ctypes.sizeof(mark)):
                    raise ctypes.WinError(ctypes.get_last_error())
                if os.fstat(fd).st_nlink != 0 or _final_path(api, handle) != expected:
                    raise DoctorError("state_probe_unconfirmed")
                may_mark = False
            finally:
                try:
                    if may_mark:
                        cancel = _Disposition(0)
                        if not api.SetFileInformationByHandle(handle, 4, ctypes.byref(cancel), ctypes.sizeof(cancel)):
                            raise DoctorError("state_probe_cleanup_unconfirmed")
                finally:
                    closing, fd = fd, None
                    os.close(closing)
            try:
                probe.lstat()
            except FileNotFoundError:
                return {"state": "writable", "write_fsync_readback": True, "probe_removed": True}
            raise DoctorError("state_probe_cleanup_unconfirmed")
    except (OSError, DoctorError) as error:
        return {"state": "unconfirmed", "reason": str(error) if isinstance(error, DoctorError) else "state_probe_unconfirmed",
                "probe_created": created, "probe_name": name if created else None,
                "probe_removed": None, "cleanup_state": "unconfirmed"}


def _platform_fact():
    if os.name != "nt":
        return {"state": "unsupported", "system": platform.system(), "machine": platform.machine()}
    build = sys.getwindowsversion().build
    machine = platform.machine().upper()
    return {"state": "supported_os_floor" if machine in {"AMD64", "X86_64"} and build >= 19045 else "unsupported",
            "machine": machine, "windows_build": build, "cpu_native_abi": "not_checked"}


def _vc_fact():
    if os.name != "nt":
        return {"state": "not_checked", "abi_verified": False}
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    api.GetSystemDirectoryW.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    count = api.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < count < len(buffer):
        return {"state": "unconfirmed", "abi_verified": False}
    missing = [name for name in ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll")
               if not _regular(Path(buffer.value) / name, single_link=False)]
    return {"state": "missing" if missing else "files_present_hint", "missing": missing, "abi_verified": False}


def _browser_fact(layout):
    browser = layout.get("browser_executable")
    choice = layout.get("browser")
    if browser is not None:
        present = _regular(browser, single_link=False)
    elif isinstance(choice, dict) and choice.get("kind") == "explicit":
        present = _regular(choice.get("path", ""), single_link=False)
    else:
        present = any(_regular(Path(os.environ.get(key, fallback)) / "Microsoft/Edge/Application/msedge.exe", single_link=False)
            for key, fallback in (("ProgramFiles(x86)", "C:/Program Files (x86)"), ("ProgramFiles", "C:/Program Files")))
    return {"state": "file_present_hint" if present else "missing", "collection_available": "not_checked",
            "board_materials_require_browser": False}


def _port_fact(port):
    if type(port) is not int or not 1 <= port <= 65535:
        return {"state": "unconfirmed"}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            connection.bind(("127.0.0.1", port))
        return {"state": "bind_available_at_check"}
    except OSError as error:
        return {"state": "in_use" if getattr(error, "winerror", None) == 10048 or error.errno in {48, 98, 10048} else "unconfirmed",
                "listener_owner": "not_checked"}


def _doctor(target, *, deep=False, inspect_install=None):
    """Run outside target runtime. Inspection authorizes the sole owned write probe."""
    not_checked = ["native_imports", "model_load", "real_collection", "paid_api", "all_cpu_variants",
                   "clean_host_support", "os_logon", "host_mcp_registration", "global_resource_drain"]
    if not deep:
        not_checked.extend(["full_payload_hash", "full_target_inventory"])
    if type(deep) is not bool:
        return _result(False, "invalid_parameters", not_checked=not_checked, exit_code=2)
    try:
        path = _target(target)
        if not path.exists():
            return _result(False, "not_installed", not_checked=not_checked,
                next_steps=["Use the trusted bundle CustomerSetup.exe install with an explicit target."], exit_code=2)
        if inspect_install is None:
            if deep:
                from customer_bundle import inspect_install
                checked = inspect_install(path, deep=True)
            else:
                from customer_bundle import inspect_install_quick
                checked = inspect_install_quick(path)
        else:
            checked = inspect_install(path, deep=bool(deep))
        layout, owner = checked["layout"], checked["owner"]
        state = Path(layout["state"])
        if _target(state) != path / "state" or _target(layout["root"]) != path / "app":
            raise DoctorError("installation_unconfirmed")
    except (OSError, ValueError, KeyError, TypeError, ImportError) as error:
        component = getattr(error, "doctor_component", None)
        issue = getattr(error, "code", None)
        known = {"installation_template", "board_dist", "python", "pythonw", "node", "ffmpeg",
                 "model_files", "launcher", "application_code", "private_python_dependencies"}
        if component in known and issue in {"installation_component_missing", "installation_component_manifest_missing"}:
            facts = {"components": {component: False if issue == "installation_component_missing" else None},
                "component_issue": {"component": component, "code": issue},
                "integrity": {"mode": "quick_component_diagnostic", "completed": False}}
            return _result(False, "installation_unconfirmed", facts, not_checked,
                ["Private component " + component + " or its manifest entry is missing; keep owned state unchanged and verify the trusted package. Do not fall back to PATH."])
        return _result(False, "installation_unconfirmed", not_checked=not_checked,
            next_steps=["Check the target and its owned installation/manager-cache facts; keep unknown files unchanged."])
    inspection = checked.get("integrity", {})
    quick = inspection.get("scope") == "doctor_key_metadata_and_complete_cache_hashes"
    integrity = {"mode": "quick_metadata" if quick else "deep_hash" if deep else "bounded_metadata"}
    if quick:
        integrity.update({name:inspection[name] for name in (
            "scope", "files_checked", "target_files_checked", "cache_files_checked",
            "target_inventory_checked", "target_hashes_checked", "cache_inventory_checked",
            "cache_hashes_checked", "template_hash_checked")})
    facts = {"binding": {"verified": True, "install_id": owner.get("install_id"),
                "version": owner.get("version"), "channel": owner.get("channel"),
                "manifest_sha256": owner.get("bundle_manifest_sha256")},
             "integrity": integrity,
             "platform": _platform_fact(), "vc_runtime": _vc_fact(), "browser": _browser_fact(layout),
             "llm": {"required_for_base": False, "credentials_read": False}}
    components = {name: _regular(layout[name]) for name in ("python", "pythonw", "node", "ffmpeg")}
    components["board_dist"] = _regular(Path(layout["dist"]) / "index.html")
    components["model_files"] = all(_regular(Path(layout["model"]) / name)
        for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt"))
    facts["components"] = components
    facts["ports"] = {"board": _port_fact(layout["port"]), "cdp": _port_fact(layout["cdp_port"])}
    facts["state"] = _state_probe(state)
    try:
        usage = shutil.disk_usage(state)
        facts["disk"] = {"free_bytes": usage.free, "total_bytes": usage.total,
            "low_space": usage.free < 1024 ** 3, "warning_threshold_bytes": 1024 ** 3,
            "installation_capacity_guarantee": False}
    except OSError:
        facts["disk"] = {"state": "unconfirmed", "free_bytes": None, "total_bytes": None}
    steps = []
    if facts["vc_runtime"]["state"] != "files_present_hint":
        steps.append("Check the Microsoft x64 VC runtime prerequisite: " + VC_URL)
    if facts["browser"]["state"] == "missing":
        steps.append("Install/select Edge before collection; board/materials do not require it: " + EDGE_URL)
    if any(not present for present in components.values()):
        steps.append("Keep the installation unchanged and verify missing private components; do not fall back to PATH.")
    if any(value["state"] != "bind_available_at_check" for value in facts["ports"].values()):
        steps.append("Query status/gate-status to identify existing owners; a used port alone is not a conflict or drain proof.")
    steps.append("For explicit native-import checks use CustomerLauncher.exe doctor-imports --target <target>.")
    if facts["platform"]["state"] == "unsupported":
        return _result(False, "platform_unsupported", facts, not_checked, steps)
    if facts["state"]["state"] != "writable":
        steps.insert(0, "Check the existing owned state directory permissions; preserve any unconfirmed doctor probe.")
        return _result(False, "state_unwritable", facts, not_checked, steps)
    if facts["disk"].get("low_space"):
        steps.append("Provide space for audio and derived data; this warning is not the install/staging minimum.")
    limited = (not all(components.values()) or facts["browser"]["state"] == "missing"
               or facts["vc_runtime"]["state"] != "files_present_hint" or facts["disk"].get("low_space", True))
    return _result(True, "checks_passed_limited" if limited else "checks_passed", facts, not_checked, steps)


def doctor(target, *, deep=False, inspect_install=None):
    try:
        return _doctor(target, deep=deep, inspect_install=inspect_install)
    except Exception:
        return _result(False, "doctor_unconfirmed", not_checked=["installation_readiness"],
            next_steps=["Keep the installation unchanged and retry from its verified external manager."])


def _import_results(importer):
    results = []
    for name in NATIVE_IMPORTS:
        try:
            importer(name)
        except Exception:
            results.append({"module": name, "ok": False, "code": "native_import_failed"})
        else:
            results.append({"module": name, "ok": True})
    return results


def _flush_streams(streams):
    """Drain each saved wrapper while its standard fd is still discarded."""
    seen, complete = set(), True
    for stream in streams:
        if stream is None or id(stream) in seen:
            continue
        seen.add(id(stream))
        try:
            stream.flush()
        except (OSError, ValueError):
            complete = False
    return complete


def doctor_imports(target, *, importer=importlib.import_module):
    """The native launcher must admit this private process before it maps Python."""
    try:
        from scripts.local_installation import bootstrap_installed
        layout = bootstrap_installed()
        if layout is None or _target(target) != layout["install"] or Path(__file__).resolve().parents[1] != layout["root"]:
            raise DoctorError("installation_unconfirmed")
    except (OSError, ValueError, ImportError):
        return _result(False, "installation_unconfirmed", exit_code=78)
    # Suppress native as well as Python import chatter; emit only the final JSON.
    saved, results, complete = [], None, True
    original = (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__)
    try:
        if not _flush_streams(original):
            raise OSError("stdio_flush_unconfirmed")
        with open(os.devnull, "w", encoding="utf-8") as discard:
            for descriptor in (1, 2):
                saved.append((descriptor, os.dup(descriptor)))
                os.dup2(discard.fileno(), descriptor)
            with contextlib.redirect_stdout(discard), contextlib.redirect_stderr(discard):
                try:
                    results = _import_results(importer)
                finally:
                    # A library may retain the original TextIOWrapper instead
                    # of consulting sys.stdout. Drain it before restoring fds.
                    drained = _flush_streams(original)
                    try:
                        discard.flush()
                    except (OSError, ValueError):
                        drained = False
                    if not drained:
                        raise OSError("stdio_flush_unconfirmed")
    except (OSError, ValueError):
        complete = False
    finally:
        for descriptor, copy in reversed(saved):
            try:
                os.dup2(copy, descriptor)
            except OSError:
                complete = False
            finally:
                try:
                    os.close(copy)
                except OSError:
                    complete = False
    if not complete or results is None:
        return _result(False, "native_imports_unconfirmed")
    passed = all(row["ok"] for row in results)
    return _result(passed, "native_imports_passed" if passed else "native_imports_failed",
        {"imports": results, "model_loaded": False, "collection_started": False, "paid_api_called": False},
        ["model_inference", "real_collection", "all_cpu_variants", "clean_host_support"])


def _emit_json(value, output_fd=None):
    text = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
    if output_fd is None:
        print(text, end="")
        return
    remaining = memoryview(text.encode("utf-8"))
    while remaining:
        written = os.write(output_fd, remaining)
        if written <= 0:
            raise OSError("doctor_output_unavailable")
        remaining = remaining[written:]


def _parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("doctor", "doctor-imports"), default="doctor")
    parser.add_argument("--target", required=True)
    parser.add_argument("--deep", action="store_true", help="Explicit complete installed-file hash validation")
    return parser.parse_args(argv)


def main(argv=None, *, _output_fd=None, _parsed=None):
    args = _parse_arguments(argv) if _parsed is None else _parsed
    if args.action == "doctor-imports" and args.deep:
        value = _result(False, "invalid_parameters", exit_code=2)
    else:
        value = doctor(args.target, deep=args.deep) if args.action == "doctor" else doctor_imports(args.target)
    _emit_json(value, _output_fd)
    return value["exit"]


def _cli_main(argv=None):
    """Process entry only: keep late cached-wrapper flushes off protocol stdout."""
    # Help and argument errors retain their normal visible argparse behavior.
    args = _parse_arguments(argv)
    protocol = None
    try:
        protocol = os.dup(1)
        os.set_inheritable(protocol, False)
        with open(os.devnull, "wb", buffering=0) as discard:
            for descriptor in (1, 2):
                os.dup2(discard.fileno(), descriptor)
            try:
                return main(_output_fd=protocol, _parsed=args)
            except Exception:
                value = _result(False, "doctor_cli_unconfirmed")
                _emit_json(value, protocol)
                return value["exit"]
    except (OSError, ValueError):
        return 1  # A missing/broken physical output channel cannot carry JSON.
    finally:
        # This helper is only the dedicated CLI process. Do not restore fd1/2:
        # shutdown or retained wrappers must continue writing to the sink.
        sys.stdout = sys.stderr = None
        if protocol is not None:
            try:
                os.close(protocol)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(_cli_main())
