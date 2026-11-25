"""Local material bounds and guarded files; no jobs, network or audio processing."""
from contextlib import contextmanager, ExitStack
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat

from scripts import audio_cleanup, audio_segments as audio

CHUNK_BYTES = 65536
MAX_LINE_BYTES = 131072
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024


class MaterialError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def seal(value):
    return dict(value, checksum=hashlib.sha256(encode(value)).hexdigest())


def checked(value):
    if not isinstance(value, dict) or value.get("checksum") != hashlib.sha256(encode({key: item for key, item in value.items() if key != "checksum"})).hexdigest():
        raise MaterialError("material_record_invalid")
    return value


def group_id(platform, tag, day):
    return hashlib.sha256(encode([platform, tag, day])).hexdigest()[:24]


def relative(name):
    if (not isinstance(name, str) or not name or len(name) > 240 or "\\" in name or ":" in name
            or any(ord(char) < 32 for char in name)):
        raise MaterialError("material_path_invalid")
    path = PurePosixPath(name)
    if path.is_absolute() or path.as_posix() != name or any(part in ("", ".", "..") or part != part.rstrip(" .") for part in path.parts):
        raise MaterialError("material_path_invalid")
    return path


def local_path(path):
    value = str(path)
    if not Path(path).is_absolute() or value.startswith(("\\\\", "//")):
        raise MaterialError("local_path_required")
    try:
        audio._no_links(Path(path))
    except (OSError, ValueError):
        raise MaterialError("material_path_unconfirmed") from None
    return Path(path).absolute()


def identity(path):
    if isinstance(path, int):
        info = os.fstat(path)
    else:
        path = local_path(path)
        info = audio._regular(path)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or getattr(info, "st_file_attributes", 0) & 0x400:
        raise MaterialError("material_file_unconfirmed")
    return {"dev": info.st_dev, "ino": info.st_ino, "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "nlink": info.st_nlink}


class _DirectoryGuard(audio_cleanup._Guard):
    def __init__(self, path):
        super().__init__(path, directory=True)

    def __enter__(self):
        import msvcrt
        audio._no_links(self.path)
        before = audio.data_scope(self.path)
        self.native = audio_cleanup._Windows()
        # FILE_LIST_DIRECTORY is read access for sharing checks. Attribute-only
        # handles do not provide this namespace lease on Windows.
        handle = self.native.api.CreateFileW(audio_cleanup._extended(self.path), 0x81, 0x3,
            None, 3, 0x200000 | 0x2000000, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            self.fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            self.native.api.CloseHandle(handle)
            raise
        try:
            info = os.fstat(self.fd)
            if (not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                    or (info.st_dev, info.st_ino) != (before["dev"], before["ino"])
                    or self.native.final_path(self.handle) != os.path.normcase(str(self.path.resolve(strict=True)))):
                raise MaterialError("material_path_unconfirmed")
            self.signature = audio._signature(info)
            self.expected_path = self.native.final_path(self.handle)
            return self
        except BaseException:
            self.close()
            raise


@contextmanager
def guard_directory(path):
    path = local_path(path)
    if os.name != "nt":
        raise MaterialError("material_platform_unsupported")
    try:
        with _DirectoryGuard(path) as guard:
            yield guard
            guard.verify()
    except (OSError, audio_cleanup.CleanupError, audio.SegmentError):
        raise MaterialError("material_path_unconfirmed") from None


@contextmanager
def directory_chain(base, child):
    base, child = local_path(base), local_path(child)
    try:
        parts = child.relative_to(base).parts
    except ValueError:
        raise MaterialError("material_path_invalid") from None
    with ExitStack() as stack:
        stack.enter_context(guard_directory(base))
        path = base
        for part in parts:
            path = path / part
            stack.enter_context(guard_directory(path))
        yield


@contextmanager
def open_file(base, name, expected=None):
    path = Path(base).joinpath(*relative(name).parts)
    try:
        with directory_chain(base, path.parent), audio_cleanup._Guard(path) as guard:
            before = identity(guard.fd)
            if expected is not None and before != expected:
                raise MaterialError("material_changed")
            with os.fdopen(os.dup(guard.fd), "rb") as stream:
                yield stream
            if identity(guard.fd) != before:
                raise MaterialError("material_changed")
            guard.verify()
    except MaterialError:
        raise
    except (OSError, audio_cleanup.CleanupError, audio.SegmentError):
        raise MaterialError("material_unavailable") from None


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise MaterialError("material_record_invalid")
        value[key] = item
    return value


def decode(raw):
    def invalid(_value):
        raise MaterialError("material_record_invalid")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=invalid)
    except (UnicodeError, ValueError) as error:
        raise MaterialError("material_record_invalid") from error


def read_json(path, limit=MAX_MANIFEST_BYTES, expected_sha=None):
    path = Path(path)
    with open_file(path.parent, path.name) as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise MaterialError("material_capacity")
    if expected_sha is not None and hashlib.sha256(raw).hexdigest() != expected_sha:
        raise MaterialError("material_checksum_mismatch")
    value = decode(raw)
    if not isinstance(value, dict):
        raise MaterialError("material_record_invalid")
    return value


def write_json(path, value, limit=MAX_MANIFEST_BYTES):
    raw = encode(value)
    if len(raw) > limit:
        raise MaterialError("material_capacity")
    # Caller pins the owned parent and serializes publication.
    audio.atomic_json(path, value)
    return hashlib.sha256(raw).hexdigest()


def describe_file(base, name):
    digest, chunks, length = hashlib.sha256(), [], 0
    tail = 0
    with open_file(base, name) as stream:
        before = identity(stream.fileno())
        if before["size"] > MAX_FILE_BYTES:
            raise MaterialError("material_capacity")
        while raw := stream.read(CHUNK_BYTES):
            length += len(raw)
            digest.update(raw)
            chunks.append(hashlib.sha256(raw).hexdigest())
            for part in raw.splitlines(keepends=True):
                tail += len(part)
                if tail > MAX_LINE_BYTES:
                    raise MaterialError("material_line_too_large")
                if part.endswith(b"\n"):
                    tail = 0
        if tail:
            raise MaterialError("material_truncated_line")
    return {"path": str(relative(name)), "bytes": length, "sha256": digest.hexdigest(),
            "identity": before, "chunks": chunks}


def delete_owned_file(base, name, expected_sha):
    """Only the Store's validated owner/layout GC calls this, never DATA."""
    path = Path(base).joinpath(*relative(name).parts)
    may_mark = False
    with directory_chain(base, path.parent), audio_cleanup._Guard(path, delete=True) as guard:
        before = identity(guard.fd)
        digest = hashlib.sha256()
        remaining = before["size"]
        if remaining > 512 * 1024 * 1024:
            raise MaterialError("material_capacity")
        while remaining:
            raw = os.read(guard.fd, min(CHUNK_BYTES, remaining))
            if not raw:
                raise MaterialError("material_changed")
            remaining -= len(raw)
            digest.update(raw)
        if os.read(guard.fd, 1) or identity(guard.fd) != before or digest.hexdigest() != expected_sha:
            raise MaterialError("material_changed")
        try:
            # TRUE may take effect and then raise before returning. Retain
            # cancellation responsibility until postmark identity is checked.
            may_mark = True
            audio_cleanup._disposition_flag(guard, True)
            current = os.fstat(guard.fd)
            if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_nlink) != (
                    before["dev"], before["ino"], before["size"], before["mtime_ns"], 0):
                raise MaterialError("material_changed")
            may_mark = False  # Closing the exact held file completes this GC.
        finally:
            if may_mark:
                try:
                    audio_cleanup._disposition_flag(guard, False)
                except BaseException as error:
                    # The caller retains its physical GC receipt; do not claim
                    # that a failed/unknown cancellation preserved this leaf.
                    raise MaterialError("material_gc_cancel_unconfirmed") from error


def delete_empty_directory(base, name, expected_scope, *, owner_sha=None):
    """Remove the exact owned directory; optional final owner leaf goes last.

    Acquiring DELETE before removing its marker refuses pinned query readers.
    This function does not follow paths out of the supplied owned namespace.
    """
    path = Path(base).joinpath(*relative(name).parts)
    with directory_chain(base, path):
        before = audio.data_scope(path)
        if (before["dev"], before["ino"]) != (expected_scope["dev"], expected_scope["ino"]):
            raise MaterialError("material_changed")
        with os.scandir(path) as entries:
            names = sorted(entry.name for entry in entries)
        if names != ([".owner.json"] if owner_sha is not None else []):
            raise MaterialError("material_unknown_artifact")
    # Listing while holding a share-zero directory DELETE handle conflicts
    # with Windows enumeration. Reopen the same identity exclusively only
    # after this guarded listing; SetDisposition still refuses nonempty dirs.
    with directory_chain(base, path.parent), audio_cleanup._Guard(path, directory=True, delete=True) as guard:
        info = os.fstat(guard.fd)
        if (info.st_dev, info.st_ino) != (expected_scope["dev"], expected_scope["ino"]):
            raise MaterialError("material_changed")
        if owner_sha is not None:
            # The parent directory's DELETE handle is already pinned. Opening
            # another parent read guard would conflict with our own handle.
            with audio_cleanup._Guard(path / ".owner.json", delete=True) as owner:
                before = identity(owner.fd)
                if before["size"] > 65536:
                    raise MaterialError("material_owner_unconfirmed")
                raw = os.read(owner.fd, 65537)
                if hashlib.sha256(raw).hexdigest() != owner_sha or identity(owner.fd) != before:
                    raise MaterialError("material_owner_unconfirmed")
                audio_cleanup._disposition_flag(owner, True)
        audio_cleanup._disposition_flag(guard, True)
