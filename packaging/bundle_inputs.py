"""Read-only validation shared by the public desktop and archive builders."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import zipfile

BLOCK = 1024 * 1024
MAX_MANIFEST = 8 * BLOCK
MAX_FILE = 2 * 1024 * BLOCK
MAX_TOTAL = 8 * 1024 * BLOCK
MAX_ENTRIES = 40000
HEX = re.compile(r"[0-9a-f]{64}\Z")
RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
            *(f"{prefix}{n}" for prefix in ("COM", "LPT") for n in "123456789¹²³")}


class PackagingError(ValueError):
    """A bounded error code suitable for a JSON command result."""


def require(condition, code):
    if not condition:
        raise PackagingError(code)


def hex_digest(value):
    require(isinstance(value, str) and HEX.fullmatch(value) is not None, "invalid_sha256")
    return value


def signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def ordinary(path, *, directory=False, allow_hardlinks=False):
    """Inspect before resolving, so symlinks/junctions are never followed."""
    path = Path(os.path.abspath(os.fspath(path)))
    require(not str(path).startswith(("\\\\", "//")), "local_path_required")
    for part in (path, *path.parents):
        info = part.lstat()
        require(not stat.S_ISLNK(info.st_mode)
                and not getattr(info, "st_file_attributes", 0) & 0x400, "linked_input")
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) if directory else
            stat.S_ISREG(info.st_mode) and (allow_hardlinks or info.st_nlink == 1), "ordinary_path_required")
    return path


def fresh_path(path):
    path = Path(os.path.abspath(os.fspath(path)))
    ordinary(path.parent, directory=True)
    try:
        path.lstat()
    except FileNotFoundError:
        return path
    raise PackagingError("output_already_exists")


def disjoint(left, right):
    left, right = Path(left), Path(right)
    require(not left.is_relative_to(right) and not right.is_relative_to(left), "overlapping_paths")


def digest_file(path, *, allow_hardlinks=False):
    path = ordinary(path, allow_hardlinks=allow_hardlinks)
    before = signature(path.stat())
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        require(signature(os.fstat(stream.fileno())) == before, "input_changed")
        for data in iter(lambda: stream.read(BLOCK), b""):
            checksum.update(data)
        require(signature(os.fstat(stream.fileno())) == before, "input_changed")
    require(signature(path.stat()) == before, "input_changed")
    return checksum.hexdigest()


def read_file(path, *, limit=MAX_MANIFEST):
    path = ordinary(path)
    before = signature(path.stat())
    require(before[2] <= limit, "input_capacity")
    with path.open("rb") as stream:
        require(signature(os.fstat(stream.fileno())) == before, "input_changed")
        raw = stream.read(limit + 1)
        require(len(raw) == before[2]
                and signature(os.fstat(stream.fileno())) == before, "input_changed")
    require(signature(path.stat()) == before, "input_changed")
    return raw


def unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, "duplicate_json_key")
        value[key] = item
    return value


def read_json_bytes(raw):
    try:
        return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(PackagingError("invalid_json")))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PackagingError("invalid_json") from error


def relative_name(name):
    require(isinstance(name, str) and 0 < len(name) <= 1024
            and not name.startswith("/") and "\\" not in name, "unsafe_member_path")
    parts = name.split("/")
    require(all(part and part not in (".", "..") and len(part) <= 255
                and not part.endswith((" ", "."))
                and part.split(".")[0].rstrip(" ").upper() not in RESERVED
                and not any(ord(char) < 32 or char in ':<>"|?*' for char in part)
                for part in parts), "unsafe_member_path")
    return name


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


class ValidatedBundle:
    """Validate an explicitly trusted ZIP without extracting or executing it.

    Its original manifest bytes, version and channel are preserved. SHA-256
    verifies the caller's chosen trust anchor; this is not publisher signing.
    """
    def __init__(self, path, expected_sha256):
        self.path = ordinary(path)
        self.expected_sha256 = hex_digest(expected_sha256)
        self.handle = None
        self.zip = None

    def __enter__(self):
        try:
            self.before = signature(self.path.stat())
            self.handle = self.path.open("rb")
            require(signature(os.fstat(self.handle.fileno())) == self.before, "input_changed")
            checksum = hashlib.sha256()
            for data in iter(lambda: self.handle.read(BLOCK), b""):
                checksum.update(data)
            require(checksum.hexdigest() == self.expected_sha256, "bundle_sha256_mismatch")
            self.stable()
            self.handle.seek(0)
            self.zip = zipfile.ZipFile(self.handle)
            self._validate()
            self.stable()
            return self
        except BaseException:
            self.close()
            raise

    def stable(self):
        require(signature(os.fstat(self.handle.fileno())) == self.before
                and signature(self.path.stat()) == self.before, "input_changed")

    def _validate(self):
        infos = self.zip.infolist()
        require(0 < len(infos) <= MAX_ENTRIES, "bundle_capacity")
        seen, files, directories = set(), {}, set()
        total = 0
        for info in infos:
            is_dir = info.is_dir()
            name = relative_name(info.filename[:-1] if is_dir else info.filename)
            require(info.filename == name + ("/" if is_dir else ""), "unsafe_member_path")
            require(name.casefold() not in seen, "duplicate_member")
            seen.add(name.casefold())
            kind = stat.S_IFMT(info.external_attr >> 16)
            require(kind in (0, stat.S_IFREG, stat.S_IFDIR)
                    and not (info.external_attr & 0x400), "linked_or_special_member")
            require(not info.flag_bits & 1 and info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED),
                    "unsupported_zip_member")
            require(0 <= info.file_size <= MAX_FILE, "bundle_capacity")
            total += info.file_size
            require(total <= MAX_TOTAL, "bundle_capacity")
            if is_dir:
                require(info.file_size == 0 and kind != stat.S_IFREG, "invalid_directory_member")
                directories.add(name)
            else:
                require(kind != stat.S_IFDIR, "invalid_file_member")
                files[name] = info
        for name in (*files, *directories):
            parts = name.split("/")
            require(all("/".join(parts[:n]) in directories for n in range(1, len(parts))),
                    "missing_directory_member")
        require("bundle.manifest.json" in files and files["bundle.manifest.json"].file_size <= MAX_MANIFEST,
                "bundle_manifest_missing")
        self.manifest_raw = self.zip.read("bundle.manifest.json")
        self.manifest = read_json_bytes(self.manifest_raw)
        manifest = self.manifest
        require(isinstance(manifest, dict) and type(manifest.get("schema")) is int
                and manifest["schema"] == 1 and manifest.get("kind") == "customer_bundle",
                "bundle_manifest_invalid")
        require(isinstance(manifest.get("version"), str) and isinstance(manifest.get("channel"), str),
                "bundle_manifest_invalid")
        rows, dirs = manifest.get("files"), manifest.get("directories")
        require(isinstance(rows, list) and isinstance(dirs, list), "bundle_manifest_invalid")
        declared, aliases = {}, set()
        for row in rows:
            require(isinstance(row, dict) and set(row) == {"path", "bytes", "sha256"}, "invalid_manifest_row")
            name = relative_name(row["path"])
            require(name != "bundle.manifest.json" and name.casefold() not in aliases, "duplicate_manifest_path")
            aliases.add(name.casefold())
            require(type(row["bytes"]) is int and 0 <= row["bytes"] <= MAX_FILE, "invalid_manifest_row")
            hex_digest(row["sha256"])
            declared[name] = row
        normalized = [relative_name(name) for name in dirs]
        require(len(normalized) == len(set(name.casefold() for name in normalized)), "duplicate_manifest_path")
        require(set(files) == set(declared) | {"bundle.manifest.json"}
                and directories == set(normalized), "bundle_inventory_mismatch")
        self.manifest_sha256 = hashlib.sha256(self.manifest_raw).hexdigest()
        self.expected_files = dict(declared)
        self.expected_files["bundle.manifest.json"] = {
            "path": "bundle.manifest.json", "bytes": len(self.manifest_raw), "sha256": self.manifest_sha256}
        for name, row in self.expected_files.items():
            require(files[name].file_size == row["bytes"], "member_size_mismatch")
            checksum = hashlib.sha256()
            with self.zip.open(name) as member:
                for block in iter(lambda: member.read(BLOCK), b""):
                    checksum.update(block)
            require(checksum.hexdigest() == row["sha256"], "member_sha256_mismatch")
        require("CustomerSetup.exe" in declared, "setup_missing")
        with self.zip.open("CustomerSetup.exe") as setup:
            require(setup.read(2) == b"MZ", "setup_not_windows_executable")
        self.setup_sha256 = declared["CustomerSetup.exe"]["sha256"]
        self.files, self.directories, self.infos = files, directories, infos

    def close(self):
        if self.zip is not None:
            self.zip.close()
        if self.handle is not None:
            self.handle.close()

    def __exit__(self, kind, value, traceback):
        try:
            if kind is None:
                self.stable()
        finally:
            self.close()
