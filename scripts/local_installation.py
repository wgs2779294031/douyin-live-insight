"""Read-only installed layout and per-process binding; absent layout is legacy."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys

_APP_ROOT = Path(__file__).resolve().parents[1]
_ACTIVE = None
_DLL_HANDLES = []
_RELATIVE = {
    "app": "app", "state": "state", "python": "runtime/python/python.exe",
    "node": "runtime/node/node.exe", "ffmpeg": "runtime/ffmpeg/ffmpeg.exe",
    "model": "assets/models/faster-whisper-small", "cache": "state/cache",
}
_FIELDS = set(_RELATIVE) | {"schema", "kind", "browser", "dll_directories", "ports"}
_CONTEXT_PATHS = ("root", "config", "data", "roster", "dist", "python", "profile")


class InstallationError(ValueError):
    def __init__(self, code="installed_runtime_unavailable"):
        self.code = code
        super().__init__(code)


def _no_links(path):
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise InstallationError("installation_path_unavailable")


def _absolute(value):
    if (not isinstance(value, (str, Path)) or not str(value)
            or str(value).startswith(("\\\\", "//")) or "://" in str(value)
            or any(ord(char) < 32 for char in str(value))):
        raise InstallationError("installation_path_unavailable")
    path = Path(value)
    if not path.is_absolute():
        raise InstallationError("installation_path_unavailable")
    _no_links(path)
    return path.resolve()


def _same(left, right):
    return os.path.normcase(str(_absolute(left))) == os.path.normcase(str(_absolute(right)))


def _decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise InstallationError("installation_invalid")
            result[key] = value
        return result
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
            parse_constant=lambda _value: (_ for _ in ()).throw(InstallationError("installation_invalid")))
    except (UnicodeError, ValueError, RecursionError):
        raise InstallationError("installation_invalid") from None


def _signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink


def _read(path):
    _no_links(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 65536:
        raise InstallationError("installation_invalid")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(65537)
        after = os.fstat(stream.fileno())
    _no_links(path)
    if (len(raw) > 65536 or _signature(before) != _signature(opened)
            or _signature(opened) != _signature(after) or _signature(after) != _signature(path.lstat())):
        raise InstallationError("installation_invalid")
    return _decode(raw)


def _browser(choice):
    if choice == {"kind": "system-edge"}:
        candidates = [Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
                      Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Microsoft/Edge/Application/msedge.exe"]
    elif isinstance(choice, dict) and set(choice) == {"kind", "path"} and choice["kind"] == "explicit":
        candidates = [_absolute(choice["path"])]
    else:
        raise InstallationError("installation_invalid")
    for candidate in candidates:
        candidate = _absolute(candidate)
        if candidate.is_file():
            return candidate
    return None


def load_installation(app_root=None):
    """Resolve a descriptor without creating state or checking this interpreter."""
    root = _absolute(_APP_ROOT if app_root is None else app_root)
    install = root.parent
    descriptor = install / "installation.json"
    try:
        descriptor.lstat()
    except FileNotFoundError:
        return None
    value = _read(descriptor)
    if (not isinstance(value, dict) or set(value) != _FIELDS
            or type(value.get("schema")) is not int or value["schema"] != 1
            or value.get("kind") != "customer_installation"
            or any(value.get(key) != relative for key, relative in _RELATIVE.items())):
        raise InstallationError("installation_invalid")
    paths = {key: _absolute(install / relative) for key, relative in _RELATIVE.items()}
    if not _same(paths["app"], root):
        raise InstallationError("installation_root_mismatch")
    ports = value["ports"]
    if (not isinstance(ports, dict) or set(ports) != {"board", "cdp"}
            or any(type(port) is not int or not 1 <= port <= 65535 for port in ports.values())
            or ports["board"] == ports["cdp"]):
        raise InstallationError("installation_invalid")
    directories = value["dll_directories"]
    if not isinstance(directories, list) or len(directories) > 64:
        raise InstallationError("installation_invalid")
    dlls = []
    for relative in directories:
        if (not isinstance(relative, str) or not (relative == "runtime/python" or relative.startswith("runtime/python/"))
                or "\\" in relative or ":" in relative or any(part in ("", ".", "..") for part in relative.split("/"))):
            raise InstallationError("installation_path_unavailable")
        directory = _absolute(install / relative)
        if not directory.is_relative_to(paths["python"].parent) or not directory.is_dir() or directory in dlls:
            raise InstallationError("installation_path_unavailable")
        dlls.append(directory)
    state = paths["state"]
    layout = dict(paths, install=install, root=root, descriptor=descriptor,
        config=_absolute(state / "config.json"), roster=_absolute(state / "anchors.json"),
        data=_absolute(state / "data"), dist=_absolute(root / "web/dist"),
        profile=_absolute(state / "data/.local_app/browser-profile"),
        context_file=_absolute(state / "data/.local_app/context.json"),
        port=ports["board"], cdp_port=ports["cdp"], dll_directories=tuple(dlls),
        browser_executable=_browser(value["browser"]))
    return layout


def installed_layout():
    return _ACTIVE if _ACTIVE is not None else load_installation()


def installed_launcher_command(context, action="run", *, windowed=True):
    """Route installed application entry through the pre-runtime native lease."""
    layout = installed_layout()
    if layout is None:
        return None
    if action != "run" or not isinstance(context, dict):
        raise InstallationError("installation_context_mismatch")
    for name in ("root", "config", "data", "roster", "dist", "python"):
        if name not in context or not _same(context[name], layout[name]):
            raise InstallationError("installation_context_mismatch")
    if any(type(context.get(name)) is not int or context[name] != layout[name]
           for name in ("port", "cdp_port")):
        raise InstallationError("installation_context_mismatch")
    executable = _absolute(layout["install"] / "entry" /
                           ("CustomerLauncherW.exe" if windowed else "CustomerLauncher.exe"))
    command = [str(executable), action, "--target", str(layout["install"])]
    for name in ("root", "config", "data", "roster", "dist", "python", "port", "cdp_port"):
        command.extend(["--" + name.replace("_", "-"), str(context[name])])
    return command


def _expected_context(layout):
    paths = {key: str(layout[key]) for key in _CONTEXT_PATHS}
    identity = {key: os.path.normcase(paths[key]) for key in ("root", "data", "config")}
    instance = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True).encode("ascii")).hexdigest()
    return dict(paths, schema=1, instance_id=instance, port=layout["port"], cdp_port=layout["cdp_port"])


def _context_valid(context, layout):
    expected = _expected_context(layout)
    return (isinstance(context, dict) and set(context) == set(expected)
        and type(context.get("schema")) is int and context["schema"] == 1
        and all(isinstance(context.get(key), str) and _same(context[key], expected[key]) for key in _CONTEXT_PATHS)
        and all(type(context.get(key)) is int and context[key] == expected[key] for key in ("port", "cdp_port"))
        and context.get("instance_id") == expected["instance_id"])


def validate_installed_context(context):
    layout = installed_layout()
    if layout is not None and not _context_valid(context, layout):
        raise InstallationError("installation_context_mismatch")


def _environment(layout, base):
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in base.items()):
        raise InstallationError("installation_environment_invalid")
    env = dict(base)
    remove = {"PYTHONHOME", "PYTHONPATH", "PYTHONUSERBASE", "PYTHONSTARTUP", "PYTHONINSPECT", "NODE_OPTIONS", "NODE_PATH", "CDP_TARGET", "CDP_TARGET_URL"}
    owned = {"WATCH_NODE", "NODE_BIN", "WATCH_CDP", "CDP_SCRIPT", "WB_ASR_MODEL", "FFMPEG_PATH", "HF_HOME", "WATCH_CHROME",
             "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "TRANSFORMERS_CACHE", "HF_ASSETS_CACHE", "HUGGINGFACE_ASSETS_CACHE", "HF_XET_CACHE",
             "WATCH_BROWSER", "CDP_PORT", "WATCH_DEBUG_PORT", "WATCH_BROWSER_PROFILE", "DOUYIN_OUTPUT_DIR", "WB_RECORD_PY",
             "PYTHONUTF8", "PYTHONDONTWRITEBYTECODE", "PATH", "DY_CUSTOMER_CONTEXT"}
    for key in tuple(env):
        if key.upper() in remove | owned:
            env.pop(key)
    system_values = {value for key, value in base.items() if key.upper() == "SYSTEMROOT"}
    if len(system_values) > 1:
        raise InstallationError("installation_environment_invalid")
    system = Path(next(iter(system_values), "C:/Windows"))
    env.update(WATCH_NODE=str(layout["node"]), NODE_BIN=str(layout["node"]),
        WATCH_CDP=str(layout["root"] / "scripts/cdp.mjs"), CDP_SCRIPT=str(layout["root"] / "scripts/cdp.mjs"),
        WB_ASR_MODEL=str(layout["model"]), FFMPEG_PATH=str(layout["ffmpeg"]), HF_HOME=str(layout["cache"] / "hf"),
        HF_HUB_CACHE=str(layout["cache"] / "hf/hub"), HUGGINGFACE_HUB_CACHE=str(layout["cache"] / "hf/hub"),
        TRANSFORMERS_CACHE=str(layout["cache"] / "hf/transformers"),
        HF_ASSETS_CACHE=str(layout["cache"] / "hf/assets"), HUGGINGFACE_ASSETS_CACHE=str(layout["cache"] / "hf/assets"),
        HF_XET_CACHE=str(layout["cache"] / "hf/xet"),
        WATCH_CHROME=str(layout["browser_executable"] or ""), WATCH_BROWSER="edge",
        CDP_PORT=str(layout["cdp_port"]), WATCH_DEBUG_PORT=str(layout["cdp_port"]),
        WATCH_BROWSER_PROFILE=str(layout["profile"]), DOUYIN_OUTPUT_DIR=str(layout["data"]), WB_RECORD_PY=str(layout["python"]),
        PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1",
        PATH=os.pathsep.join(map(str, (layout["node"].parent, layout["python"].parent, system / "System32", system))))
    sources = {value for key, value in base.items() if key.upper() == "DY_CUSTOMER_CONTEXT"}
    source = next(iter(sources)) if len(sources) == 1 else None
    valid = False
    if source:
        try:
            valid = _same(source, layout["context_file"]) and _context_valid(_read(layout["context_file"]), layout)
        except (OSError, ValueError):
            pass
    if valid:
        env["DY_CUSTOMER_CONTEXT"] = str(layout["context_file"])
    return env


def bootstrap_installed():
    """Bind one process before business imports; no filesystem writes."""
    global _ACTIVE
    layout = installed_layout()
    if layout is None:
        return None
    allowed = (layout["python"], layout["python"].with_name("pythonw.exe"))
    if not any(_same(sys.executable, candidate) for candidate in allowed):
        raise InstallationError("installation_python_mismatch")
    if _ACTIVE is None:
        handles = []
        try:
            for directory in layout["dll_directories"]:
                handles.append(os.add_dll_directory(str(directory)))
        except (AttributeError, OSError):
            for handle in handles:
                handle.close()
            raise InstallationError("installation_dll_unavailable") from None
        _DLL_HANDLES.extend(handles)
        _ACTIVE = layout
    cleaned = _environment(layout, os.environ)
    for key in tuple(os.environ):
        if key not in cleaned:
            os.environ.pop(key, None)
    os.environ.update(cleaned)
    sys.dont_write_bytecode = True
    _configure_text_streams()
    return layout


def _configure_text_streams():
    # The isolated runtime ignores PYTHONUTF8 at startup. Reconfigure only the
    # existing text wrappers; keep raw buffers and the MCP private pipes intact.
    for name, errors in (("stdin", "strict"), ("stdout", "replace"), ("stderr", "replace")):
        stream = getattr(sys, name)
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        encoding = str(getattr(stream, "encoding", "")).lower().replace("_", "-")
        if encoding in ("utf-8", "utf8") and getattr(stream, "errors", None) == errors:
            continue
        try:
            reconfigure(encoding="utf-8", errors=errors)
        except (OSError, ValueError):
            raise InstallationError("installation_stdio_unavailable") from None


def installed_child_environment(context, base=None):
    layout = installed_layout()
    env = dict(os.environ if base is None else base)
    if layout is None:
        return env
    validate_installed_context(context)
    return _environment(layout, env)


def bootstrap_entry():
    """Safe ordinary script startup; only the private site wrapper uses _exit."""
    try:
        return bootstrap_installed()
    except (InstallationError, OSError):
        try:
            os.write(2, b"installed_runtime_unavailable\n")
        except OSError:
            pass
        raise SystemExit(78) from None


def bind_customer_context(context, source=None):
    layout = installed_layout()
    if layout is None:
        return
    validate_installed_context(context)
    path = layout["context_file"] if source is None else _absolute(source)
    if not _same(path, layout["context_file"]) or not _context_valid(_read(path), layout):
        raise InstallationError("installation_context_mismatch")
    os.environ["DY_CUSTOMER_CONTEXT"] = str(path)
