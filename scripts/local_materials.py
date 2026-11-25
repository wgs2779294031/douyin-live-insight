"""Explicit customer-local material publication and read-only query facade."""
from contextlib import contextmanager, ExitStack
import hashlib
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time
from types import SimpleNamespace

from scripts import audio_cleanup, audio_segments as audio
from scripts import material_common as common

MaterialError = common.MaterialError
_VERSION = re.compile(r"v_[0-9a-f]{32}\Z")
_HEX = re.compile(r"[0-9a-f]{64}\Z")
MAX_TREE_ENTRIES = 16384
MAX_GC_BYTES = 2 * 1024 * 1024 * 1024
_ERRORS = {"", "material_build_failed", "material_gc_blocked", "material_publish_unconfirmed",
    "material_capacity", "material_scope_changed", "material_path_unconfirmed", "material_changed",
    "material_file_unconfirmed", "material_unavailable", "material_line_too_large", "material_truncated_line",
    "material_record_invalid", "material_checksum_mismatch", "shared_projection_failed", "evidence_unconfirmed", "material_gc_pending"}


def _stamp(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        return False


class MaterialStore:
    def __init__(self, context, *, derived_root=None, wall_time=time.time):
        if (not isinstance(context, dict) or type(context.get("schema")) is not int or context["schema"] != 1
                or not isinstance(context.get("instance_id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", context["instance_id"])
                or not isinstance(context.get("data"), str) or not isinstance(context.get("root"), str)
                or Path(context.get("root", "")) != Path(__file__).resolve().parents[1]):
            raise MaterialError("customer_context_required")
        self.context, self.instance_id, self.wall = dict(context), context["instance_id"], wall_time
        self.data = common.local_path(context.get("data", ""))
        if self.data.parent == self.data:
            raise MaterialError("data_volume_root")
        suffix = hashlib.sha256(self.instance_id.encode()).hexdigest()[:12]
        default = self.data.with_name(self.data.name[:80] + ".materials-" + suffix)
        if derived_root is not None and not isinstance(derived_root, (str, os.PathLike)):
            raise MaterialError("local_path_required")
        self.root = common.local_path(default if derived_root is None else derived_root)
        if self.root == self.data or self.root in self.data.parents or self.data in self.root.parents:
            raise MaterialError("material_path_overlap")
        if self.root.parent != self.data.parent:
            raise MaterialError("material_sibling_required")
        for key in ("profile", "config", "roster"):
            if context.get(key):
                if not isinstance(context[key], (str, os.PathLike)):
                    raise MaterialError("local_path_required")
                protected = common.local_path(context[key])
                if protected == self.root or protected in self.root.parents or self.root in protected.parents:
                    raise MaterialError("material_path_overlap")
        self.scope = audio.data_scope(self.data)
        self.parent_scope = audio.data_scope(self.data.parent)

    def _scope(self):
        if audio.data_scope(self.data) != self.scope or audio.data_scope(self.data.parent) != self.parent_scope:
            raise MaterialError("material_scope_changed")

    def _owner(self, *, create=False):
        self._scope()
        if not self.root.exists():
            if not create:
                raise MaterialError("materials_not_ready")
            self.root.mkdir()
            owner = common.seal({"schema": 1, "kind": "local_materials_owner", "instance_id": self.instance_id,
                "owner_id": secrets.token_hex(16), "cursor_key": secrets.token_hex(32),
                "data_scope": self.scope, "derived_scope": audio.data_scope(self.root)})
            with common.guard_directory(self.root):
                common.write_json(self.root / "owner.json", owner)
                (self.root / "versions").mkdir()
        with common.guard_directory(self.root):
            owner = common.checked(common.read_json(self.root / "owner.json", 65536))
            if (type(owner.get("schema")) is not int or owner["schema"] != 1 or owner.get("kind") != "local_materials_owner"
                    or owner.get("instance_id") != self.instance_id or owner.get("data_scope") != self.scope
                    or owner.get("derived_scope") != audio.data_scope(self.root)
                    or not re.fullmatch(r"[0-9a-f]{32}", str(owner.get("owner_id", "")))
                    or not _HEX.fullmatch(str(owner.get("cursor_key", "")))):
                raise MaterialError("material_owner_unconfirmed")
            if create and not (self.root / "versions").exists():
                (self.root / "versions").mkdir()
            if create and not (self.root / "gc").exists():
                (self.root / "gc").mkdir()
        return owner

    def _pointer(self, owner, *, missing=False):
        path = self.root / "current.json"
        if missing and not path.exists():
            return None
        value = common.checked(common.read_json(path, 65536))
        if (type(value.get("schema")) is not int or value["schema"] != 1 or value.get("kind") != "material_current"
                or value.get("owner_id") != owner["owner_id"] or value.get("data_scope") != self.scope
                or not _VERSION.fullmatch(str(value.get("version", "")))
                or not _HEX.fullmatch(str(value.get("manifest_sha256", "")))
                or value.get("previous") is not None and not _VERSION.fullmatch(str(value["previous"]))
                or not _stamp(value.get("generated_at"))):
            raise MaterialError("material_pointer_invalid")
        return value

    def _version_owner(self, owner, version):
        if not _VERSION.fullmatch(version):
            raise MaterialError("material_version_invalid")
        path = self.root / "versions" / version
        value = common.checked(common.read_json(path / ".owner.json", 65536))
        self._check_version_owner(owner, version, value, audio.data_scope(path))
        return value

    def _check_version_owner(self, owner, version, value, scope):
        common.checked(value)
        if (type(value.get("schema")) is not int or value["schema"] != 1 or value.get("kind") != "material_version"
                or value.get("version") != version or value.get("owner_id") != owner["owner_id"]
                or value.get("data_scope") != self.scope or value.get("version_scope") != scope
                or value.get("layout") != "material-v1" or value.get("phase") not in {"building", "sealed"}
                or value.get("phase") == "sealed" and not _HEX.fullmatch(str(value.get("manifest_sha256", "")))):
            raise MaterialError("material_owner_unconfirmed")

    def _receipt(self, owner, version, scope=None):
        value = common.checked(common.read_json(self.root / "gc" / (version + ".json")))
        if (type(value.get("schema")) is not int or value["schema"] != 1 or value.get("kind") != "material_gc"
                or value.get("version") != version or value.get("owner_id") != owner["owner_id"]
                or value.get("data_scope") != self.scope or scope is not None and value.get("version_scope") != scope
                or not isinstance(value.get("files"), dict) or not isinstance(value.get("directories"), dict)
                or len(value["files"]) + len(value["directories"]) > MAX_TREE_ENTRIES):
            raise MaterialError("material_owner_unconfirmed")
        self._check_version_owner(owner, version, value.get("version_owner"), value.get("version_scope"))
        for name, digest in value["files"].items():
            if not self._layout(common.relative(name).as_posix(), False) or not _HEX.fullmatch(str(digest)):
                raise MaterialError("material_owner_unconfirmed")
        for name, identity in value["directories"].items():
            if (not self._layout(common.relative(name).as_posix(), True) or not isinstance(identity, dict)
                    or set(identity) != {"dev", "ino"} or any(type(item) is not int for item in identity.values())):
                raise MaterialError("material_owner_unconfirmed")
        return value

    @staticmethod
    def _layout(name, directory):
        if directory:
            return bool(re.fullmatch(r"(?:canonical|exports|exports/数据|exports/数据/[0-9]{4}-[0-9]{2}-[0-9]{2}|exports/\.nas-export-[a-z0-9_]+)", name))
        return (name in {".owner.json", "manifest.json", "catalog.jsonl"}
                or bool(re.fullmatch(r"canonical/[0-9a-f]{24}\.jsonl", name))
                or bool(re.fullmatch(r"exports/数据/[0-9]{4}-[0-9]{2}-[0-9]{2}/(?:douyin|bilibili)_[^/]+\.jsonl", name))
                or bool(re.fullmatch(r"exports/\.nas-export-[a-z0-9_]+/records\.jsonl", name))
                or bool(re.fullmatch(r"(?:tmp[a-z0-9_]{8}|(?:manifest|catalog|\.owner)\.json\.[a-z0-9_]+\.tmp)", name)))

    def _prune(self, owner, version):
        path = self.root / "versions" / version
        with common.directory_chain(self.root, path), ExitStack() as leases:
            receipt_path = self.root / "gc" / (version + ".json")
            receipt = None
            if receipt_path.exists():
                receipt = self._receipt(owner, version, audio.data_scope(path))
                marker = receipt["version_owner"]
            else:
                marker = self._version_owner(owner, version)
            expected = None
            if marker.get("phase") == "sealed" and receipt is None:
                manifest = common.checked(common.read_json(path / "manifest.json", expected_sha=marker["manifest_sha256"]))
                expected = {entry["path"]: entry["sha256"] for entry in manifest["files"]}
                if manifest.get("catalog") is not None:
                    expected["catalog.jsonl"] = manifest["catalog"]["sha256"]
                expected.update({"manifest.json": marker["manifest_sha256"],
                                 ".owner.json": hashlib.sha256(common.encode(marker)).hexdigest()})
            if receipt is not None:
                expected = receipt["files"]
            allowed_dirs = None
            if expected is not None:
                allowed_dirs = {"canonical", "exports", "exports/数据"}
                for name in expected:
                    allowed_dirs.update(item.as_posix() for item in common.relative(name).parents if item.as_posix() != ".")
            if receipt is not None:
                allowed_dirs = set(receipt["directories"])
            directories, files, pending = [], [], [path]
            inspected = 0
            while pending:
                parent = pending.pop()
                with common.directory_chain(path, parent), os.scandir(parent) as entries:
                    for entry in entries:
                        inspected += 1
                        if inspected > MAX_TREE_ENTRIES:
                            raise MaterialError("material_capacity")
                        leaf = parent / entry.name
                        common.local_path(leaf)
                        info = leaf.lstat()
                        is_dir = stat.S_ISDIR(info.st_mode)
                        name = leaf.relative_to(path).as_posix()
                        if (not self._layout(name, is_dir) or getattr(info, "st_file_attributes", 0) & 0x400
                                or not is_dir and expected is not None and name not in expected
                                or is_dir and allowed_dirs is not None and name not in allowed_dirs):
                            raise MaterialError("material_unknown_artifact")
                        if is_dir:
                            lease = leases.enter_context(common.guard_directory(leaf))
                            held = os.fstat(lease.fd)
                            if (held.st_dev, held.st_ino) != (info.st_dev, info.st_ino):
                                raise MaterialError("material_changed")
                            physical = {"dev": info.st_dev, "ino": info.st_ino}
                            if receipt is not None and receipt["directories"][name] != physical:
                                raise MaterialError("material_changed")
                            directories.append((leaf, physical))
                            pending.append(leaf)
                        else:
                            common.identity(leaf)
                            files.append(name)
            # A bounded per-version physical GC receipt survives removal of
            # the in-directory marker. It is not ASR success evidence.
            hashes, hashed_bytes = {}, 0
            for name in files:
                digest = hashlib.sha256()
                with common.open_file(path, name) as stream:
                    size = common.identity(stream.fileno())["size"]
                    hashed_bytes += size
                    if size > common.MAX_FILE_BYTES or hashed_bytes > MAX_GC_BYTES:
                        raise MaterialError("material_capacity")
                    while raw := stream.read(common.CHUNK_BYTES):
                        digest.update(raw)
                hashes[name] = digest.hexdigest()
                if expected is not None and hashes[name] != expected[name]:
                    raise MaterialError("material_changed")
            if receipt is None:
                receipt = common.seal({"schema": 1, "kind": "material_gc", "version": version,
                    "owner_id": owner["owner_id"], "data_scope": self.scope, "version_scope": audio.data_scope(path),
                    "version_owner": marker, "files": hashes,
                    "directories": {leaf.relative_to(path).as_posix(): scope for leaf, scope in directories}})
                common.write_json(receipt_path, receipt)
            # Validate the complete layout before any deletion. Owner is last.
            for name in sorted(set(files) - {".owner.json"}):
                common.delete_owned_file(path, name, hashes[name])
            leases.close()
            for directory, scope in sorted(directories, key=lambda item: len(item[0].parts), reverse=True):
                common.delete_empty_directory(path, directory.relative_to(path).as_posix(), scope)
            scope = audio.data_scope(path)
        common.delete_empty_directory(self.root / "versions", version, scope, owner_sha=hashes.get(".owner.json"))
        common.delete_owned_file(self.root / "gc", receipt_path.name, hashlib.sha256(common.encode(receipt)).hexdigest())

    def _gc_receipts(self, owner, kept):
        with common.guard_directory(self.root / "gc"), os.scandir(self.root / "gc") as entries:
            names = [entry.name for entry in entries]
        if len(names) > 3:
            raise MaterialError("material_capacity")
        for name in names:
            if not re.fullmatch(r"v_[0-9a-f]{32}\.json", name):
                raise MaterialError("material_unknown_artifact")
            version = name[:-5]
            if version in kept:
                raise MaterialError("material_owner_unconfirmed")
            if not (self.root / "versions" / version).exists():
                path = self.root / "gc" / name
                receipt = self._receipt(owner, version)
                common.delete_owned_file(self.root / "gc", name, hashlib.sha256(common.encode(receipt)).hexdigest())

    def _diagnostic(self, state, version, error="", *, last_success_at=None):
        error = error if isinstance(error, str) and error in _ERRORS else "material_build_failed"
        value = common.seal({"schema": 1, "kind": "material_refresh", "data_scope": self.scope,
            "state": state, "version": version, "updated_at": self.wall(), "last_success_at": last_success_at, "error": error})
        common.write_json(self.root / "refresh.json", value, 65536)
        return value

    def _metadata_temps(self):
        # Permanent lock sidecars and unknown user files are never removed.
        inspected = 0
        with os.scandir(self.root) as entries:
            for entry in entries:
                inspected += 1
                if inspected > 128:
                    raise MaterialError("material_capacity")
                if re.fullmatch(r"(?:owner|current|refresh)\.json\.[a-z0-9_]{8}\.tmp", entry.name):
                    digest = hashlib.sha256()
                    with common.open_file(self.root, entry.name) as stream:
                        if common.identity(stream.fileno())["size"] > 65536:
                            raise MaterialError("material_capacity")
                        while raw := stream.read(common.CHUNK_BYTES):
                            digest.update(raw)
                    common.delete_owned_file(self.root, entry.name, digest.hexdigest())

    def _validate_built(self, owner, version, path, manifest):
        from scripts.material_query import _manifest
        _manifest(SimpleNamespace(manifest=manifest, cursor_key=bytes.fromhex(owner["cursor_key"])))
        for entry in manifest["files"]:
            relative = common.relative(entry["path"]).as_posix()
            if not self._layout(relative, False) or common.identity(path / relative) != entry["identity"]:
                raise MaterialError("material_changed")
        catalog = manifest.get("catalog")
        if catalog is not None and (catalog.get("path") != "catalog.jsonl"
                or common.identity(path / "catalog.jsonl") != catalog.get("identity")):
            raise MaterialError("material_changed")

    def _acquire_refresh(self):
        # A permanent, never-replaced resource. Exact Windows handle opening
        # refuses reparse points and pins this inode across processes. No mark
        # or deletion is performed on the lock file.
        path = self.root / ".refresh.lock"
        try:
            with path.open("xb") as stream:
                stream.write(b"0")
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            pass
        guard = audio_cleanup._Guard(path, delete=True)
        try:
            guard.__enter__()
        except OSError as error:
            if getattr(error, "winerror", None) in (32, 33):
                return None
            raise MaterialError("material_lock_unconfirmed") from None
        try:
            if common.identity(guard.fd)["size"] != 1 or os.read(guard.fd, 2) != b"0":
                raise MaterialError("material_lock_unconfirmed")
            return guard
        except BaseException:
            guard.close()
            raise

    def refresh(self, *, builder=None):
        if builder is None:
            from scripts.material_facts import build_materials
            builder = build_materials
        with common.guard_directory(self.data.parent), common.guard_directory(self.data):
            owner = self._owner(create=True)
            with common.guard_directory(self.root), common.guard_directory(self.root / "versions"), common.guard_directory(self.root / "gc"):
                lock = self._acquire_refresh()
                if lock is None:
                    return {"state": "busy", "version": None, "error": "material_busy"}
                try:
                    self._scope()
                    pointer = self._pointer(owner, missing=True)
                    previous = pointer["version"] if pointer else None
                    kept = {value for value in (previous, pointer.get("previous") if pointer else None) if value}
                    if previous:
                        with common.directory_chain(self.root, self.root / "versions" / previous):
                            marker = self._version_owner(owner, previous)
                            if marker.get("phase") != "sealed" or marker.get("manifest_sha256") != pointer["manifest_sha256"]:
                                raise MaterialError("material_owner_unconfirmed")
                            common.checked(common.read_json(self.root / "versions" / previous / "manifest.json",
                                                            expected_sha=pointer["manifest_sha256"]))
                    try:
                        self._metadata_temps()
                        self._gc_receipts(owner, kept)
                        versions = sorted(item.name for item in (self.root / "versions").iterdir())
                        if len(versions) > 3:
                            raise MaterialError("material_capacity")
                        for version in versions:
                            if version not in kept:
                                self._prune(owner, version)
                    except (OSError, ValueError):
                        self._diagnostic("failed", previous, "material_gc_blocked")
                        return {"state": "failed", "version": previous, "error": "material_gc_blocked"}
                    generated = self.wall()
                    version = "v_" + secrets.token_hex(16)
                    path = self.root / "versions" / version
                    path.mkdir()
                    marker = common.seal({"schema": 1, "kind": "material_version", "version": version,
                        "owner_id": owner["owner_id"], "data_scope": self.scope,
                        "version_scope": audio.data_scope(path), "layout": "material-v1", "phase": "building"})
                    with common.guard_directory(path):
                        common.write_json(path / ".owner.json", marker, 65536)
                        self._diagnostic("building", previous, last_success_at=pointer.get("generated_at") if pointer else None)
                        committed = False
                        try:
                            built = builder(self.data, path, material_version=version, generated_at=generated, expected_scope=self.scope)
                            self._scope()
                            manifest = common.seal(dict(built, schema=1, kind="local_materials", version=version,
                                data_scope=self.scope, generated_at=generated, verified_at=generated,
                                raw_freshness="snapshot_at_build_raw_not_rechecked"))
                            self._validate_built(owner, version, path, manifest)
                            digest = common.write_json(path / "manifest.json", manifest)
                            marker = common.seal(dict({key: value for key, value in marker.items() if key != "checksum"},
                                                     phase="sealed", manifest_sha256=digest))
                            common.write_json(path / ".owner.json", marker, 65536)
                            current = common.seal({"schema": 1, "kind": "material_current", "owner_id": owner["owner_id"],
                                "data_scope": self.scope, "version": version, "previous": previous,
                                "manifest_sha256": digest, "generated_at": generated})
                            try:
                                common.write_json(self.root / "current.json", current, 65536)
                                committed = True
                            except (OSError, ValueError):
                                try:
                                    committed = self._pointer(owner) == current
                                except (OSError, ValueError):
                                    return {"state": "unconfirmed", "version": None, "error": "material_publish_unconfirmed"}
                                if not committed:
                                    raise MaterialError("material_publish_unconfirmed")
                        except Exception as error:
                            reason = error.reason if isinstance(error, MaterialError) else "material_build_failed"
                            try:
                                self._diagnostic("failed", previous, reason, last_success_at=pointer.get("generated_at") if pointer else None)
                            except (OSError, ValueError):
                                pass
                            return {"state": "failed", "version": previous, "error": reason}
                        finally:
                            self._scope()
                    warning = ""
                    for retired in kept - {previous}:
                        try:
                            self._prune(owner, retired)
                        except (OSError, ValueError):
                            warning = "material_gc_pending"
                    try:
                        self._diagnostic("success", version, warning, last_success_at=generated)
                    except (OSError, ValueError):
                        warning = "material_diagnostic_unavailable"
                    return {"state": "published", "version": version, "previous": previous, "error": "", "warning": warning}
                finally:
                    lock.close()

    @contextmanager
    def snapshot(self):
        with ExitStack() as stack:
            stack.enter_context(common.guard_directory(self.data.parent))
            self._scope()
            stack.enter_context(common.guard_directory(self.root))
            owner_identity = common.identity(self.root / "owner.json")
            owner = self._owner()
            if common.identity(self.root / "owner.json") != owner_identity:
                raise MaterialError("material_changed")
            pointer = self._pointer(owner)
            path = self.root / "versions" / pointer["version"]
            stack.enter_context(common.directory_chain(self.root, path))
            marker = self._version_owner(owner, pointer["version"])
            if marker.get("phase") != "sealed" or marker.get("manifest_sha256") != pointer["manifest_sha256"]:
                raise MaterialError("material_owner_unconfirmed")
            manifest_identity = common.identity(path / "manifest.json")
            manifest = common.checked(common.read_json(path / "manifest.json", expected_sha=pointer["manifest_sha256"]))
            if (manifest.get("version") != pointer["version"] or manifest.get("data_scope") != self.scope
                    or manifest.get("kind") != "local_materials" or type(manifest.get("schema")) is not int or manifest["schema"] != 1):
                raise MaterialError("material_record_invalid")
            status = {"state": "unconfirmed", "version": pointer["version"], "updated_at": None,
                      "last_success_at": pointer.get("generated_at"), "error": "material_refresh_unconfirmed", "snapshot_stale": True}
            try:
                diagnostic = common.checked(common.read_json(self.root / "refresh.json", 65536))
                if (type(diagnostic.get("schema")) is int and diagnostic["schema"] == 1
                        and diagnostic.get("kind") == "material_refresh" and diagnostic.get("data_scope") == self.scope
                        and diagnostic.get("version") == pointer["version"] and diagnostic.get("state") in {"success", "failed"}
                        and _stamp(diagnostic.get("updated_at"))
                        and (diagnostic.get("last_success_at") is None or _stamp(diagnostic["last_success_at"]))
                        and isinstance(diagnostic.get("error"), str) and diagnostic["error"] in _ERRORS):
                    status.update({key: diagnostic.get(key) for key in ("state", "version", "updated_at", "last_success_at", "error")})
                    status["snapshot_stale"] = diagnostic["state"] != "success"
            except (OSError, ValueError):
                pass
            def finish():
                self._scope()
                if common.identity(self.root / "owner.json") != owner_identity:
                    raise MaterialError("material_changed")
                latest = self._pointer(owner)
                if latest["version"] != pointer["version"] or latest["manifest_sha256"] != pointer["manifest_sha256"]:
                    raise MaterialError("material_version_changed")
                if self._version_owner(owner, pointer["version"]) != marker or common.identity(path / "manifest.json") != manifest_identity:
                    raise MaterialError("material_changed")
            yield SimpleNamespace(root=path, manifest=manifest, status=status, cursor_key=bytes.fromhex(owner["cursor_key"]), finish=finish)

    def query(self, **filters):
        from scripts.material_query import query
        with self.snapshot() as snapshot:
            return query(snapshot, **filters)

    def list_groups(self, **filters):
        from scripts.material_query import list_groups
        with self.snapshot() as snapshot:
            return list_groups(snapshot, **filters)


def main(argv=None):
    import argparse
    import json
    from scripts.local_context import load_customer_context
    from scripts.material_query import MaterialQueryError
    parser = argparse.ArgumentParser(description="Explicit local customer materials; reading never refreshes.")
    parser.add_argument("--context", default=os.environ.get("DY_CUSTOMER_CONTEXT"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("refresh", help="Explicitly build and publish local materials")
    for name in ("list", "query"):
        command = commands.add_parser(name)
        command.add_argument("--view", choices=("canonical", "shared"), default="canonical")
        command.add_argument("--room")
        command.add_argument("--date")
        command.add_argument("--platform", choices=("douyin", "bilibili"))
        command.add_argument("--limit", type=int, default=50)
        command.add_argument("--cursor")
        if name == "query":
            command.add_argument("--stream", action="append", choices=("watch", "room_meta", "stats", "danmu", "events", "speech"))
            command.add_argument("--from", dest="time_from", type=float)
            command.add_argument("--until", dest="time_until", type=float)
    options = vars(parser.parse_args(argv))
    source, command = options.pop("context"), options.pop("command")
    try:
        if not source:
            raise MaterialError("customer_context_required")
        # Reject remote path strings before the general context loader resolves
        # them; this entry point is local and never probes NAS paths.
        raw = common.read_json(common.local_path(source), 65536)
        for key in ("root", "data", "config", "roster", "dist", "python", "profile"):
            common.local_path(raw.get(key, ""))
        context = load_customer_context(source)
        store = MaterialStore(context)
        result = store.refresh() if command == "refresh" else getattr(store, "list_groups" if command == "list" else "query")(**options)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 1 if command == "refresh" and result["state"] != "published" else 0
    except (MaterialError, MaterialQueryError) as error:
        print(json.dumps({"error": error.reason}, ensure_ascii=False))
        return 1
    except (OSError, ValueError, TypeError):
        print(json.dumps({"error": "customer_materials_unavailable"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
