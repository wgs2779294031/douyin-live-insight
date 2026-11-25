"""9A revision GC on new registered derived JSON only; never DATA/audio."""
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[1]
OUT = Path(os.environ.get("DLI_TEST_ROOT", str(SOURCE / ".test-state"))).resolve()
sys.path.insert(0, str(SOURCE))
AUDIT = {"network": 0, "process": 0, "registry": 0}
OBS, GUARDS, REGISTERED = [], [], []


def audit(event, _args):
    kind = ("network" if event in {"socket.connect", "socket.bind", "socket.getaddrinfo"}
            else "process" if event in {"subprocess.Popen", "os.system", "os.spawn"}
            else "registry" if event.startswith("winreg.") else None)
    if kind:
        AUDIT[kind] += 1
        raise AssertionError("forbidden external effect")


sys.addaudithook(audit)
from scripts import audio_cleanup as cleanup, audio_segments as audio, material_common as common
from scripts.local_materials import MaterialStore


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def empty_builder(*_args, **_kwargs):
    return {"groups": [], "files": [], "catalog": None, "discovery": {"files_checked": 0}}


@pytest.fixture
def case(tmp_path, monkeypatch, request):
    assert tmp_path.resolve().is_relative_to((OUT / "tmp-parent").resolve())
    assert list(tmp_path.iterdir()) == []
    data = tmp_path / "new-original-data"
    data.mkdir()
    raw = data / "generated-original.jsonl"
    raw.write_bytes(b'{"generated_original":"must remain unchanged"}\n')
    before = sha(raw.read_bytes())
    context = {"schema": 1, "root": str(SOURCE), "instance_id": "gc-cancel-revision1",
        "data": str(data), "config": str(tmp_path / "unused-config.json"),
        "roster": str(tmp_path / "unused-roster.json"), "profile": str(tmp_path / "never-profile")}
    store = MaterialStore(context)
    assert not store.root.exists() and store.root.absolute().is_relative_to(tmp_path.resolve())
    assert not store.root.absolute().is_relative_to(data.resolve())
    owner = store._owner(create=True)
    registered = store.root.resolve(strict=True)
    assert owner["derived_scope"] == audio.data_scope(registered)
    registration = {"test": request.node.name, "root": str(registered), "owner_id": owner["owner_id"],
        "scope": owner["derived_scope"], "data": str(data.resolve()), "data_sha256_before": before}
    REGISTERED.append(registration)
    c = SimpleNamespace(data=data, raw=raw, base=tmp_path, store=store, owner=owner, hook=None,
        operations=[], native_events=[], authorized={}, aliases=set(), errors=[])
    original_flag = cleanup._disposition_flag

    def scoped(guard, value):
        assert type(value) is bool
        assert audio.data_scope(registered) == owner["derived_scope"]
        if value:
            path = guard.path.resolve(strict=True)
            assert path.is_relative_to(registered) and path.is_relative_to((OUT / "tmp-parent").resolve())
            assert not path.is_relative_to(data.resolve()) and path not in c.aliases
            assert path.suffix.lower() not in {".flv", ".wav", ".mp3", ".aac", ".m4a", ".flac", ".ogg"}
            c.authorized[id(guard)] = (guard, str(path))
        else:
            assert id(guard) in c.authorized and c.authorized[id(guard)][0] is guard
            path = Path(c.authorized[id(guard)][1])
        c.operations.append({"path": str(path), "value": value})

        def native():
            original_flag(guard, value)
            c.native_events.append({"path": str(path), "value": value, "nlink": os.fstat(guard.fd).st_nlink})
        return c.hook(guard, value, native) if c.hook is not None else native()

    monkeypatch.setattr(cleanup, "_disposition_flag", scoped)
    for cls in (cleanup._Guard, common._DirectoryGuard):
        original_enter = cls.__enter__
        def enter(guard, original=original_enter):
            value = original(guard)
            GUARDS.append(guard)
            return value
        monkeypatch.setattr(cls, "__enter__", enter)
    yield c
    registration["data_sha256_after"] = sha(raw.read_bytes())
    assert registration["data_sha256_after"] == before
    assert all(guard.fd is None for guard in GUARDS)
    OBS.append({"test": request.node.name, "operations": c.operations, "native_events": c.native_events,
                "data_unchanged": True, "all_guard_fds_closed": True})


def generated_building(c):
    version = "v_" + "a" * 32
    stage = c.store.root / "versions" / version
    assert not stage.exists()
    stage.mkdir()
    marker = common.seal({"schema": 1, "kind": "material_version", "version": version,
        "owner_id": c.owner["owner_id"], "data_scope": c.store.scope,
        "version_scope": audio.data_scope(stage), "layout": "material-v1", "phase": "building"})
    common.write_json(stage / ".owner.json", marker, 65536)
    (stage / "canonical").mkdir()
    leaf = stage / "canonical" / ("b" * 24 + ".jsonl")
    leaf.write_bytes(b'{"generated_derived":true}\n')
    return version, stage, leaf


def leaf_values(c, leaf, *, native=False):
    return [item["value"] for item in (c.native_events if native else c.operations) if item["path"] == str(leaf)]


def assert_original_context(error, original):
    assert isinstance(error, common.MaterialError) and error.reason == "material_path_unconfirmed"
    seen = set()
    current = error
    while current is not None and id(current) not in seen:
        if current is original:
            return
        seen.add(id(current))
        current = current.__context__
    pytest.fail("guard normalization lost the original injected OSError context")


def test_true_pre_native_oserror_still_attempts_false_and_preserves_leaf(case):
    c = case
    version, _stage, leaf = generated_building(c)
    original = leaf.read_bytes()
    failure = OSError("generated before native TRUE")
    def fail_before_true(guard, value, native):
        if guard.path == leaf and value:
            raise failure
        return native()
    c.hook = fail_before_true
    with pytest.raises(common.MaterialError) as caught:
        c.store._prune(c.owner, version)
    assert_original_context(caught.value, failure)
    assert leaf.read_bytes() == original and leaf.stat().st_nlink == 1
    assert leaf_values(c, leaf) == [True, False] and leaf_values(c, leaf, native=True) == [False]
    assert (c.store.root / "gc" / (version + ".json")).is_file()


@pytest.mark.parametrize("error_type", [OSError, SystemExit, KeyboardInterrupt])
def test_post_true_late_alias_interrupt_is_cancelled_before_guard_closes(case, error_type):
    c = case
    version, _stage, leaf = generated_building(c)
    original = leaf.read_bytes()
    alias = c.base / "unregistered-derived-alias.json"
    c.aliases.add(alias)
    failure = error_type("generated native TRUE succeeded before helper return")
    def interrupt_after_true(guard, value, native):
        if guard.path == leaf and value:
            os.link(leaf, alias)
            native()
            assert os.fstat(guard.fd).st_nlink == 1
            raise failure
        return native()
    c.hook = interrupt_after_true
    with pytest.raises(common.MaterialError if error_type is OSError else error_type) as caught:
        c.store._prune(c.owner, version)
    if error_type is OSError:
        assert_original_context(caught.value, failure)
    else:
        assert caught.value is failure
    assert leaf_values(c, leaf) == [True, False]
    assert leaf_values(c, leaf, native=True) == [True, False]
    assert leaf.read_bytes() == alias.read_bytes() == original and leaf.stat().st_nlink == 2
    assert (c.store.root / "gc" / (version + ".json")).is_file()


def test_postmark_fstat_interrupt_retains_cancellation_responsibility(case, monkeypatch):
    c = case
    version, _stage, leaf = generated_building(c)
    original, original_fstat, armed = leaf.read_bytes(), os.fstat, {"fd": None, "fired": False}
    failure = SystemExit("generated before postmark fstat verification")
    def arm_after_true(guard, value, native):
        result = native()
        if guard.path == leaf and value:
            armed["fd"] = guard.fd
        return result
    def interrupted_fstat(fd):
        if fd == armed["fd"] and not armed["fired"]:
            armed["fired"] = True
            raise failure
        return original_fstat(fd)
    c.hook = arm_after_true
    monkeypatch.setattr(common.os, "fstat", interrupted_fstat)
    with pytest.raises(SystemExit) as caught:
        c.store._prune(c.owner, version)
    assert caught.value is failure and armed["fired"] and leaf.read_bytes() == original
    assert leaf_values(c, leaf, native=True) == [True, False] and leaf.stat().st_nlink == 1


@pytest.mark.parametrize("cancel_stage", ["before_native_false", "after_native_false"])
def test_cancel_failure_blocks_this_refresh_retains_receipt_then_can_recover(case, monkeypatch, cancel_stage):
    c = case
    first = c.store.refresh(builder=empty_builder)
    assert first["state"] == "published"
    pointer_before = (c.store.root / "current.json").read_bytes()
    version, stage, leaf = generated_building(c)
    original = leaf.read_bytes()
    alias = c.base / "unregistered-derived-alias.json"
    c.aliases.add(alias)
    cancel_error = (OSError("generated before native FALSE") if cancel_stage == "before_native_false"
                    else KeyboardInterrupt("generated native FALSE succeeded before helper return"))
    def uncertain_cancel(guard, value, native):
        if guard.path == leaf and value:
            os.link(leaf, alias)
            native()
            raise SystemExit("generated after native TRUE")
        if guard.path == leaf and not value:
            if cancel_stage == "after_native_false":
                native()
            raise cancel_error
        return native()
    c.hook = uncertain_cancel
    real_delete, failures, builders = common.delete_owned_file, [], []
    def record_error(*args, **kwargs):
        try:
            return real_delete(*args, **kwargs)
        except common.MaterialError as error:
            failures.append(error)
            raise
    def must_not_build(*_args, **_kwargs):
        builders.append(True)
        raise AssertionError("pre-GC cancellation failure must not begin a new stage")
    monkeypatch.setattr(common, "delete_owned_file", record_error)
    versions_before = {path.name for path in (c.store.root / "versions").iterdir()}
    blocked = c.store.refresh(builder=must_not_build)
    assert blocked == {"state": "failed", "version": first["version"], "error": "material_gc_blocked"}
    assert not builders and {path.name for path in (c.store.root / "versions").iterdir()} == versions_before
    assert (c.store.root / "current.json").read_bytes() == pointer_before
    assert c.store.query()["version"] == first["version"]
    assert len(failures) == 1 and failures[0].reason == "material_gc_cancel_unconfirmed"
    assert failures[0].__cause__ is cancel_error
    receipt = c.store.root / "gc" / (version + ".json")
    saved = common.checked(common.read_json(receipt))
    assert saved["files"][leaf.relative_to(stage).as_posix()] == sha(original)
    assert alias.read_bytes() == original
    if cancel_stage == "before_native_false":
        assert leaf_values(c, leaf, native=True) == [True] and not leaf.exists()
    else:
        assert leaf_values(c, leaf, native=True) == [True, False]
        assert leaf.read_bytes() == original and leaf.stat().st_nlink == 2
    OBS.append({"test": cancel_stage, "cancel_state": "unconfirmed", "cause": type(cancel_error).__name__,
        "leaf_exists_at_block": leaf.exists(), "alias_sha256": sha(alias.read_bytes()),
        "original_sha256": sha(original), "gc_receipt_retained": True, "no_preservation_promise": True})
    # Keep every unknown alias. A present multi-link owned leaf is renamed out
    # of the pending version, preserving it, so receipt recovery can recheck.
    retained = c.base / "retained-cancelled-owned-leaf.jsonl"
    if leaf.exists():
        leaf.rename(retained)
        assert retained.read_bytes() == original
    c.hook = None
    recovered = c.store.refresh(builder=empty_builder)
    assert recovered["state"] == "published" and recovered["version"] != first["version"]
    assert not stage.exists() and not receipt.exists() and alias.read_bytes() == original
    assert (c.store.root / "versions" / first["version"]).is_dir()
    assert not builders and len(list((c.store.root / "versions").iterdir())) == 2


def test_normal_postmark_verified_gc_completes_without_false(case):
    c = case
    version, stage, leaf = generated_building(c)
    c.store._prune(c.owner, version)
    assert not leaf.exists() and not stage.exists() and not (c.store.root / "gc" / (version + ".json")).exists()
    assert leaf_values(c, leaf, native=True) == [True]
    assert c.operations and all(item["value"] is True for item in c.operations)


@pytest.fixture(scope="session", autouse=True)
def evidence(request):
    base = Path(request.config.option.basetemp)
    assert base.is_absolute() and base.parent.resolve() == (OUT / "tmp-parent").resolve()
    yield
    payload = {"audit": AUDIT, "registered_owner_derived_roots": REGISTERED, "observations": OBS,
        "native_guards": len(GUARDS), "all_guard_fds_closed": all(guard.fd is None for guard in GUARDS),
        "scope": "new registered derived JSON only; original DATA SHA unchanged; aliases and fixtures retained"}
    (OUT / (base.name + "-observations.json")).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert AUDIT == {"network": 0, "process": 0, "registry": 0}
    assert all(guard.fd is None for guard in GUARDS)
