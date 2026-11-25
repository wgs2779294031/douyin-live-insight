"""Resolve ASR model settings using synthetic directories and an offline SDK stub."""
from pathlib import Path
import os
import sys
from types import ModuleType

import pytest

from scripts import transcribe_backfill as tb


def _synthetic_model(path):
    path.mkdir()
    for name in ("model.bin", "config.json", "tokenizer.json"):
        (path / name).write_bytes(b"synthetic test data; never loaded")
    return path


@pytest.fixture
def sdk(tmp_path, monkeypatch):
    calls, result = [], object()
    module = ModuleType("faster_whisper")

    def model(path, **kwargs):
        calls.append((path, kwargs))
        return result

    module.WhisperModel = model
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    monkeypatch.delenv("WB_ASR_MODEL", raising=False)
    monkeypatch.setattr(tb, "LOCAL_MODEL", tmp_path / "missing-legacy-default")
    monkeypatch.chdir(tmp_path)
    return calls, result


def test_auto_prefers_environment_local_directory_over_legacy(tmp_path, monkeypatch, sdk):
    selected = _synthetic_model(tmp_path / "new-model")
    legacy = _synthetic_model(tmp_path / "legacy-model")
    monkeypatch.setattr(tb, "LOCAL_MODEL", legacy)
    monkeypatch.setenv("WB_ASR_MODEL", str(selected))
    calls, result = sdk
    assert tb.load_model("auto") is result
    assert calls == [(str(selected), {"device": "cpu", "compute_type": "int8", "local_files_only": True})]


@pytest.mark.parametrize("explicit", ["small", "medium", "explicit-local-model", "org/model"])
def test_explicit_cli_model_ignores_invalid_environment(monkeypatch, sdk, explicit):
    monkeypatch.setenv("WB_ASR_MODEL", "")
    calls, result = sdk
    assert tb.load_model(explicit, device="cpu", compute_type="float32") is result
    assert calls == [(explicit, {"device": "cpu", "compute_type": "float32"})]


@pytest.mark.parametrize("bad_kind", ["missing", "file", "blank", "model-name", "url", "unc"])
def test_invalid_environment_never_falls_back_or_calls_sdk(tmp_path, monkeypatch, sdk, bad_kind):
    bad = str(tmp_path / "missing-directory")
    if bad_kind == "file":
        path = tmp_path / "a-file"
        path.write_text("synthetic", encoding="utf-8")
        bad = str(path)
    elif bad_kind == "blank":
        bad = "  "
    elif bad_kind == "model-name":
        bad = "small"
    elif bad_kind == "url":
        bad = "https://example.invalid/model"
    elif bad_kind == "unc":
        bad = r"\\synthetic.invalid\model"
    monkeypatch.setenv("WB_ASR_MODEL", bad)
    with pytest.raises(ValueError, match="WB_ASR_MODEL"):
        tb.load_model("auto")
    assert sdk[0] == []


@pytest.mark.parametrize("missing", ["model.bin", "config.json", "tokenizer.json"])
def test_missing_local_assets_are_configuration_errors(tmp_path, monkeypatch, sdk, missing):
    selected = _synthetic_model(tmp_path / "incomplete-model")
    (selected / missing).unlink()
    monkeypatch.setenv("WB_ASR_MODEL", str(selected))
    with pytest.raises(ValueError, match=missing.replace(".", r"\.")):
        tb.load_model("auto")
    assert sdk[0] == []


def test_environment_sdk_error_does_not_retry_with_download_name(tmp_path, monkeypatch, sdk):
    selected = _synthetic_model(tmp_path / "corrupt-model")
    monkeypatch.setenv("WB_ASR_MODEL", str(selected))
    calls = []

    def failed(path, **kwargs):
        calls.append((path, kwargs))
        raise RuntimeError("synthetic corrupt model")

    monkeypatch.setattr(sys.modules["faster_whisper"], "WhisperModel", failed)
    with pytest.raises(RuntimeError, match="WB_ASR_MODEL"):
        tb.load_model("auto")
    assert calls == [(str(selected), {"device": "cpu", "compute_type": "int8", "local_files_only": True})]


def test_auto_without_environment_keeps_legacy_directory(tmp_path, monkeypatch, sdk):
    legacy = tmp_path / "legacy-default"
    legacy.mkdir()
    monkeypatch.setattr(tb, "LOCAL_MODEL", legacy)
    tb.load_model("auto")
    assert sdk[0] == [(str(legacy), {"device": "cpu", "compute_type": "int8"})]


def test_auto_without_environment_or_legacy_keeps_small_default(sdk):
    tb.load_model("auto")
    assert sdk[0] == [("small", {"device": "cpu", "compute_type": "int8"})]


@pytest.mark.parametrize("spelling", ["absolute", "existing-relative", "dot-relative", "parent-relative", "home-relative"])
def test_explicit_local_directory_is_offline_and_wins_over_bad_environment(tmp_path, monkeypatch, sdk, spelling):
    selected = _synthetic_model(tmp_path / "model")
    value = str(selected)
    if spelling == "existing-relative":
        value = "model"
    elif spelling == "dot-relative":
        value = "./model"
    elif spelling == "parent-relative":
        work = tmp_path / "work"
        work.mkdir()
        monkeypatch.chdir(work)
        value = "../model"
    elif spelling == "home-relative":
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        value = "~/model"
    monkeypatch.setenv("WB_ASR_MODEL", "https://example.invalid/must-not-be-used")
    assert tb.load_model(value) is sdk[1]
    assert sdk[0] == [(str(Path(value).expanduser()),
                       {"device": "cpu", "compute_type": "int8", "local_files_only": True})]


@pytest.mark.parametrize("asset", ["model.bin", "config.json", "tokenizer.json"])
@pytest.mark.parametrize("damage", ["missing", "empty"])
def test_explicit_local_assets_must_be_complete_before_sdk(tmp_path, sdk, asset, damage):
    selected = _synthetic_model(tmp_path / "incomplete")
    if damage == "missing":
        (selected / asset).unlink()
    else:
        (selected / asset).write_bytes(b"")
    with pytest.raises(ValueError, match="--model"):
        tb.load_model(str(selected))
    assert sdk[0] == []


@pytest.mark.parametrize("spelling", ["absolute", "dot-relative", "parent-relative", "home-relative", "backslash-relative", "file"])
def test_invalid_explicit_local_path_is_never_treated_as_download_id(tmp_path, monkeypatch, sdk, spelling):
    value = str(tmp_path / "missing")
    if spelling == "dot-relative":
        value = "./missing"
    elif spelling == "parent-relative":
        value = "../missing"
    elif spelling == "home-relative":
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        value = "~/missing"
    elif spelling == "backslash-relative":
        value = r".\missing"
    elif spelling == "file":
        path = tmp_path / "not-directory"
        path.write_text("synthetic", encoding="utf-8")
        value = str(path)
    with pytest.raises(ValueError, match="--model"):
        tb.load_model(value)
    assert sdk[0] == []


def test_explicit_posix_absolute_missing_path_is_never_a_model_id(monkeypatch, sdk):
    # Exercise POSIX spelling on Windows without probing a real root directory.
    monkeypatch.setattr(Path, "is_dir", lambda _self: False)
    with pytest.raises(ValueError, match="--model"):
        tb.load_model("/synthetic-absent-model")
    assert sdk[0] == []


@pytest.mark.parametrize("argument", ["auto", "explicit"])
def test_local_expanduser_failure_is_a_configuration_error(monkeypatch, sdk, argument):
    monkeypatch.setenv("WB_ASR_MODEL", "~/synthetic")
    def fail(_self):
        raise RuntimeError("synthetic unknown home directory")
    monkeypatch.setattr(Path, "expanduser", fail)
    with pytest.raises(ValueError, match="WB_ASR_MODEL" if argument == "auto" else "--model"):
        tb.load_model("auto" if argument == "auto" else "~/synthetic")
    assert sdk[0] == []


@pytest.mark.skipif(os.name != "nt", reason="Windows drive spelling")
@pytest.mark.parametrize("argument", ["auto", "explicit"])
def test_windows_double_slash_drive_is_a_local_directory(tmp_path, monkeypatch, sdk, argument):
    selected = _synthetic_model(tmp_path / "model")
    value = selected.as_posix().replace(":/", "://", 1)
    monkeypatch.setenv("WB_ASR_MODEL", value)
    assert tb.load_model("auto" if argument == "auto" else value) is sdk[1]
    assert sdk[0] == [(str(Path(value)), {"device": "cpu", "compute_type": "int8", "local_files_only": True})]


def test_explicit_local_sdk_error_cannot_fall_back_to_download_name(tmp_path, monkeypatch, sdk):
    selected = _synthetic_model(tmp_path / "corrupt-model")
    calls = []
    def failed(path, **kwargs):
        calls.append((path, kwargs))
        raise RuntimeError("synthetic local load failure")
    monkeypatch.setattr(sys.modules["faster_whisper"], "WhisperModel", failed)
    with pytest.raises(RuntimeError, match="--model"):
        tb.load_model(str(selected))
    assert calls == [(str(selected), {"device": "cpu", "compute_type": "int8", "local_files_only": True})]
