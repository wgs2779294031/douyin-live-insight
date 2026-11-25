"""Per-segment cleanup facts; only an exact Windows source handle can delete."""
from __future__ import annotations

from contextlib import ExitStack
import ctypes
from ctypes import wintypes
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from scripts import audio_segments as audio

_KEYS = {"schema", "kind", "state", "scope", "relative", "file", "source", "generation", "result_sha256", "checksum"}
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_GENERATION = re.compile(r"[0-9a-f]{32}\Z")
_MAX_OPERATION = 65536


class CleanupError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _payload(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")).encode("utf-8")


def _seal(value):
    return dict(value, checksum=hashlib.sha256(_payload(value)).hexdigest())


def operation_path(data, folder, name):
    folder = audio.safe_folder(data, folder)
    audio._number(name)
    match = re.fullmatch(r"audio_(.+)_([0-9]{4}-[0-9]{2}-[0-9]{2})", folder.name)
    if match is None:
        raise CleanupError("invalid_folder")
    try:
        datetime.strptime(match[2], "%Y-%m-%d")
    except ValueError as error:
        raise CleanupError("invalid_folder") from error
    directory = folder / ".cleanup"
    audio._no_links(directory)
    if directory.exists() and not directory.is_dir():
        raise CleanupError("cleanup_record_invalid")
    return directory / (Path(name).stem + ".json")


def read_operation(data, folder, name, closed, generation, result_sha256):
    """Pure read/binding checks; this never grants transcript success."""
    path = operation_path(data, folder, name)
    if (not isinstance(generation, str) or _GENERATION.fullmatch(generation) is None
            or not isinstance(result_sha256, str) or _HEX.fullmatch(result_sha256) is None
            or audio.verify_closed(data, folder, name, require_source=False) != closed):
        raise CleanupError("cleanup_binding_conflict")
    try:
        if audio._regular(path).st_size > _MAX_OPERATION:
            raise CleanupError("cleanup_record_invalid")
        value = audio.read_json(path)
    except FileNotFoundError:
        return None
    except (ValueError, OSError) as error:
        raise CleanupError("cleanup_record_invalid") from error
    if (set(value) != _KEYS or type(value.get("schema")) is not int or value["schema"] != 1
            or value.get("kind") != "audio_cleanup" or value.get("state") not in ("pending", "deleted")
            or not isinstance(value.get("checksum"), str) or _HEX.fullmatch(value["checksum"]) is None):
        raise CleanupError("cleanup_record_invalid")
    payload = {key: item for key, item in value.items() if key != "checksum"}
    if value["checksum"] != hashlib.sha256(_payload(payload)).hexdigest():
        raise CleanupError("cleanup_record_invalid")
    if (value["scope"] != audio.data_scope(data) or value["relative"] != closed["relative"]
            or value["file"] != name or value["source"] != closed or value["generation"] != generation
            or value["result_sha256"] != result_sha256):
        raise CleanupError("cleanup_binding_conflict")
    return value


def _write_operation(path, value):
    if len(_payload(value)) + 1 > _MAX_OPERATION:
        raise CleanupError("cleanup_record_invalid")
    audio.atomic_json(path, value)


class _Disposition(ctypes.Structure):
    _fields_ = [("DeleteFile", ctypes.c_ubyte)]  # Native BOOLEAN, not a 4-byte BOOL.


class _Windows:
    def __init__(self):
        if os.name != "nt":
            raise CleanupError("cleanup_platform_unsupported")
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self.api.CreateFileW.restype = wintypes.HANDLE
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.api.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        self.api.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        self.api.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.SetFileInformationByHandle.restype = wintypes.BOOL

    def final_path(self, handle):
        buffer = ctypes.create_unicode_buffer(32768)
        size = self.api.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if not 0 < size < len(buffer):
            raise CleanupError("cleanup_handle_unconfirmed")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return os.path.normcase(value)


def _extended(path):
    value = str(Path(path).absolute())
    if value.startswith("\\\\?\\"):
        return value
    return "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value


class _Guard:
    def __init__(self, path, *, directory=False, delete=False):
        self.path, self.directory, self.delete = Path(path), directory, delete
        self.native, self.fd = None, None
        self.disposition_set = False
        self.may_mark = False
        self.mark_unconfirmed = False

    def __enter__(self):
        import msvcrt
        audio._no_links(self.path)
        before = self.path.stat(follow_symlinks=False)
        self.native = _Windows()
        access = 0x80 if self.directory else 0x80000000  # READ_ATTRIBUTES / GENERIC_READ
        access |= 0x10000 if self.delete else 0  # DELETE
        sharing = 0 if self.delete else 0x1 | (0x2 if self.directory else 0)
        flags = 0x200000 | (0x2000000 if self.directory else 0)  # OPEN_REPARSE_POINT / BACKUP_SEMANTICS
        handle = self.native.api.CreateFileW(_extended(self.path), access, sharing, None, 3, flags, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            self.fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            self.native.api.CloseHandle(handle)
            raise
        try:
            current = os.fstat(self.fd)
            if (getattr(current, "st_file_attributes", 0) & 0x400
                    or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
                    or (stat.S_ISDIR(current.st_mode) if self.directory else stat.S_ISREG(current.st_mode) and current.st_nlink == 1) is not True
                    or self.native.final_path(self.handle) != os.path.normcase(str(self.path.resolve(strict=True)))):
                raise CleanupError("cleanup_handle_unconfirmed")
            self.signature = audio._signature(current)
            self.expected_path = self.native.final_path(self.handle)
            return self
        except BaseException:
            self.close()
            raise

    @property
    def handle(self):
        import msvcrt
        return msvcrt.get_osfhandle(self.fd)

    def close(self):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            os.close(fd)

    def verify(self):
        current = os.fstat(self.fd)
        if self.directory:
            valid = (current.st_dev, current.st_ino) == self.signature[:2]
        else:
            valid = audio._signature(current) == self.signature and current.st_nlink == 1
        if not valid or self.native.final_path(self.handle) != self.expected_path:
            raise CleanupError("cleanup_handle_unconfirmed")

    def __exit__(self, *_args):
        self.close()


def _fingerprint_handle(guard, *, marked=False):
    before = os.fstat(guard.fd)
    digest = hashlib.sha256()
    os.lseek(guard.fd, 0, os.SEEK_SET)
    remaining = before.st_size
    while remaining:
        chunk = os.read(guard.fd, min(1024 * 1024, remaining))
        if not chunk:
            raise CleanupError("source_changed")
        digest.update(chunk)
        remaining -= len(chunk)
    if os.read(guard.fd, 1) or audio._signature(os.fstat(guard.fd)) != audio._signature(before):
        raise CleanupError("source_changed")
    if marked:
        if before.st_nlink != 0 or os.fstat(guard.fd).st_nlink != 0:
            raise CleanupError("source_changed")
    else:
        current = audio._regular(guard.path)
        if audio._signature(current) != audio._signature(before):
            raise CleanupError("source_changed")
    return {"dev": before.st_dev, "ino": before.st_ino, "size": before.st_size,
            "mtime_ns": before.st_mtime_ns, "sha256": digest.hexdigest()}


def _verify_source(guard, closed):
    if any(closed.get(key) != value for key, value in _fingerprint_handle(guard).items()):
        raise CleanupError("source_changed")
    guard.closed_source = closed


def _disposition_flag(guard, value):
    disposition = _Disposition(int(value))
    if not guard.native.api.SetFileInformationByHandle(guard.handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
        raise ctypes.WinError(ctypes.get_last_error())
    guard.disposition_set = value
    if not value:
        guard.may_mark = False


def _verify_marked(guard):
    # A marked name is excluded from NTFS's link count. Any remaining link
    # appeared before marking; marking itself prevents a later hardlink open.
    if any(guard.closed_source.get(key) != value for key, value in _fingerprint_handle(guard, marked=True).items()):
        raise CleanupError("source_changed")
    for evidence in guard.evidence_guards:
        evidence.verify()
    guard.final_check()


def _set_disposition(guard):
    for evidence in guard.evidence_guards:
        evidence.verify()
    _disposition_flag(guard, True)
    try:
        _verify_marked(guard)
    except BaseException:
        try:
            _disposition_flag(guard, False)
        except OSError as error:
            raise CleanupError("cleanup_cancel_unconfirmed") from error
        raise


def _present(path):
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _out(state, reason, *, deleted=False, deleted_now=False):
    return {"state": state, "reason": reason, "deleted": deleted, "deleted_now": deleted_now}


def _facts(results, data, folder, name, closed, generation, digest):
    result, current_digest, ready = results._current_text(data, folder, name)
    if result["source"] != closed or result["generation"] != generation or current_digest != digest:
        raise CleanupError("cleanup_binding_conflict")
    if not ready:
        raise CleanupError("projection_pending")


def _settle(data, folder, name, closed, generation, digest, pending):
    source = Path(folder) / name
    if _present(source) or read_operation(data, folder, name, closed, generation, digest) != pending:
        raise CleanupError("cleanup_binding_conflict")
    value = _seal(dict({key: item for key, item in pending.items() if key != "checksum"}, state="deleted"))
    _write_operation(operation_path(data, folder, name), value)
    if read_operation(data, folder, name, closed, generation, digest) != value:
        raise CleanupError("cleanup_commit_unconfirmed")
    if _present(source):
        raise CleanupError("cleanup_binding_conflict")


def cleanup_segment(data, folder, name):
    """One writer, one durable intent, one exact-handle deletion; no scheduler."""
    if os.name != "nt":
        return _out("kept", "cleanup_platform_unsupported")
    from scripts import transcript_results as results
    accepted, deleted_now, missing, source_guard = False, False, False, None
    try:
        with results.writer(data):
            path = operation_path(data, folder, name)
            folder, source = path.parent.parent, path.parent.parent / name
            closed = audio.verify_closed(data, folder, name, require_source=False)
            result, digest, ready = results._current_text(data, folder, name)
            generation = result["generation"]
            if result["source"] != closed or not ready:
                raise CleanupError("projection_pending")
            operation = read_operation(data, folder, name, closed, generation, digest)
            present = _present(source)
            if operation is None and not present:
                return _out("kept", "source_missing")
            if operation is not None and operation["state"] == "deleted" and present:
                return _out("kept", "cleanup_binding_conflict")
            if present and not results.validate_success(data, folder, name)["cleanup_eligible"]:
                return _out("kept", "source_unconfirmed")
            with ExitStack() as guards:
                evidence_guards = []
                for directory in (Path(data), folder, folder / ".segments", folder / ".results"):
                    evidence_guards.append(guards.enter_context(_Guard(directory, directory=True)))
                path.parent.mkdir(exist_ok=True)
                evidence_guards.append(guards.enter_context(_Guard(path.parent, directory=True)))
                speech, projection, projection_pending = results.projection_paths(data, folder)
                metadata = [folder / ".segments" / (Path(name).stem + ".json"),
                            results.result_path(data, folder, name), results.attempt_path(data, folder, name), projection, speech]
                if _present(projection_pending):
                    metadata.append(projection_pending)
                for record_path in metadata:
                    evidence_guards.append(guards.enter_context(_Guard(record_path)))
                _facts(results, data, folder, name, closed, generation, digest)
                source_guard = guards.enter_context(_Guard(source, delete=True)) if present else None
                if source_guard is not None:
                    _verify_source(source_guard, closed)
                if operation is None:
                    operation = _seal({"schema": 1, "kind": "audio_cleanup", "state": "pending",
                        "scope": audio.data_scope(data), "relative": closed["relative"], "file": name,
                        "source": closed, "generation": generation, "result_sha256": digest})
                    try:
                        _write_operation(path, operation)
                        if read_operation(data, folder, name, closed, generation, digest) != operation:
                            raise CleanupError("cleanup_accept_unconfirmed")
                    except (OSError, ValueError):
                        return _out("pending", "cleanup_accept_unconfirmed")
                operation_guard = guards.enter_context(_Guard(path))
                evidence_guards.append(operation_guard)
                if read_operation(data, folder, name, closed, generation, digest) != operation:
                    raise CleanupError("cleanup_binding_conflict")
                accepted = True
                if operation["state"] == "deleted":
                    if _present(source):
                        return _out("kept", "cleanup_binding_conflict")
                    return _out("deleted", "audio_deleted", deleted=True)
                _facts(results, data, folder, name, closed, generation, digest)
                if source_guard is not None:
                    _verify_source(source_guard, closed)
                    source_guard.evidence_guards = evidence_guards
                    def final_check():
                        _facts(results, data, folder, name, closed, generation, digest)
                        if read_operation(data, folder, name, closed, generation, digest) != operation:
                            raise CleanupError("cleanup_binding_conflict")
                    source_guard.final_check = final_check
                    source_guard.may_mark = True
                    try:
                        _set_disposition(source_guard)
                    except BaseException:
                        try:
                            _disposition_flag(source_guard, False)
                        except (OSError, ValueError) as error:
                            source_guard.mark_unconfirmed = True
                            raise CleanupError("cleanup_cancel_unconfirmed") from error
                        raise
                    deleted_now = True
                    source_guard.close()
                missing = not _present(source)
                if not missing:
                    return _out("pending", "cleanup_delete_unconfirmed", deleted_now=deleted_now)
                _facts(results, data, folder, name, closed, generation, digest)
                operation_guard.close()
                _settle(data, folder, name, closed, generation, digest, operation)
                return _out("deleted", "audio_deleted", deleted=True, deleted_now=deleted_now)
    except results.WriterBusy:
        return _out("busy", "busy")
    except (ValueError, OSError) as error:
        deleted_now = deleted_now or bool(source_guard and (source_guard.disposition_set or source_guard.mark_unconfirmed))
        if accepted:
            try:
                missing = not _present(Path(folder) / name)
            except OSError:
                missing = False
        reason = getattr(error, "reason", "cleanup_delete_failed" if accepted else "cleanup_unconfirmed")
        if not isinstance(reason, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason) is None:
            reason = "cleanup_unconfirmed"
        return _out("pending" if accepted else "kept", reason, deleted=missing, deleted_now=deleted_now)
