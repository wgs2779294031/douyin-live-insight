"""Regression cases use synthetic data, URLs and child-process mocks only."""
import json
import math
import pathlib
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import assist_board_server as board
import board_review as review
import browser_tools
import gen_example_config as example
import hunt_lottery_ws as hunt
import sweep_lottery_pool as sweep
import scan_luckybox
import decode_frames


def test_peak_uses_full_samples_before_minute_thinning():
    series, peak, pv = review._online_series([
        {"ts": 120, "online": 1000}, {"ts": 121, "online": 1}])
    assert series == [[121, 1]]
    assert peak == 1000


def test_peak_survives_series_tail_limit(monkeypatch):
    monkeypatch.setattr(review, "MAX_SERIES_POINTS", 2)
    series, peak, _ = review._online_series([
        {"ts": 120, "online": 1000}, {"ts": 180, "online": 2},
        {"ts": 240, "online": 3}])
    assert series == [[180, 2], [240, 3]] and peak == 1000


def test_peak_ignores_invalid_counts_and_keeps_latest_timestamp():
    rows = [{"ts": 121, "online": 2}, {"ts": 120, "online": 5}]
    rows += [{"ts": 122, "online": v} for v in (True, -1, math.inf, math.nan, "999")]
    rows += [{"ts": v, "online": 99999} for v in (math.inf, math.nan, None, 0)]
    series, peak, _ = review._online_series(rows)
    assert series == [[121, 2]] and peak == 5


def _feed(store, filename, event):
    store._feed(filename, json.dumps(event, ensure_ascii=False).encode("utf-8"))


def test_coldline_uses_explicit_tag_without_portrait_or_room_mapping(tmp_path):
    store = board.Store(str(tmp_path), "2026-11-04")
    _feed(store, "stats_douyin_A_2026-11-04.jsonl",
          {"type": "room_stats", "ts": 120, "room_id": "platform-room", "online": 1})
    _feed(store, "coldline_2026-11-04.jsonl",
          {"ts": 121, "room_id": "web-room", "room_tag": "A", "lines": ["synthetic"]})
    assert store.tags["A"].cold["lines"] == ["synthetic"]
    assert not store.pending_cold


def test_coldline_explicit_tag_wins_over_stale_mapping(tmp_path):
    store = board.Store(str(tmp_path), "2026-11-04")
    store.room2tag["room"] = "old"
    store.pending_cold["room"] = {"ts": 120}
    _feed(store, "coldline_2026-11-04.jsonl",
          {"ts": 121, "room_id": "room", "room_tag": "A", "lines": ["synthetic"]})
    assert store.tags["A"].cold["ts"] == 121
    assert "old" not in store.tags and not store.pending_cold


def test_coldline_tag_is_enough_even_when_legacy_room_id_missing(tmp_path):
    store = board.Store(str(tmp_path), "2026-11-04")
    _feed(store, "coldline_2026-11-04.jsonl", {"ts": 121, "room_tag": "A"})
    assert "A" in store.tags and not store.pending_cold


def test_capture_paths_are_portable_and_have_explicit_overrides(tmp_path, monkeypatch):
    monkeypatch.delenv("CDP_SCRIPT", raising=False)
    monkeypatch.delenv("WB_CAPTURES_DIR", raising=False)
    monkeypatch.setattr(browser_tools, "ROOT", tmp_path)
    assert browser_tools.cdp_script() == tmp_path / "scripts" / "cdp.mjs"
    assert browser_tools.captures_dir() == tmp_path / "data" / "captures"
    bridge, captures = tmp_path / "custom.mjs", tmp_path / "private-captures"
    captures.mkdir()
    source = captures / "synthetic.json"
    source.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("CDP_SCRIPT", str(bridge))
    monkeypatch.setenv("WB_CAPTURES_DIR", str(captures))
    assert browser_tools.cdp_script() == bridge
    assert scan_luckybox._resolve_capture_path("synthetic.json") == source
    assert decode_frames._resolve("synthetic.json") == source


@pytest.mark.parametrize("module,method", [(sweep, "fetch_feed_once"),
                                           (hunt, "refresh_rooms_from_feed")])
def test_feed_helpers_use_bundled_bridge_and_utf8(module, method, tmp_path, monkeypatch):
    monkeypatch.setenv("WB_CAPTURES_DIR", str(tmp_path))
    monkeypatch.setenv("NODE_BIN", "synthetic-node")
    monkeypatch.delenv("CDP_SCRIPT", raising=False)
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if args[2] == "net":
            pathlib.Path(args[4]).write_text("[]", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="中文诊断", stderr="")

    monkeypatch.setattr(module.subprocess, "run", run)
    assert getattr(module, method)("test") == []
    assert len(calls) == 2
    for args, kwargs in calls:
        assert args[0] == "synthetic-node"
        assert pathlib.Path(args[1]) == ROOT / "scripts" / "cdp.mjs"
        assert kwargs["encoding"] == "utf-8" and kwargs["errors"] == "replace"


def test_node_override_does_not_probe_path(monkeypatch):
    monkeypatch.setenv("NODE_BIN", "explicit-node")
    monkeypatch.setattr(browser_tools.shutil, "which", lambda *a: pytest.fail("unexpected PATH probe"))
    assert browser_tools.node_binary() == "explicit-node"


def test_scrubber_clears_webhooks_nested_credentials_and_url_access_values():
    original = {"feishu": {"webhook_url": "https://example.invalid/hook/synthetic"},
                "headers": {"Authorization": "Bearer synthetic"},
                "auth": "synthetic", "nested": [{"session_id": {"value": "synthetic"}}],
                "base_url": "https://u:p@example.invalid/v1?api_key=synthetic&version=1#synthetic"}
    clean = example.scrub(original)
    assert clean["feishu"]["webhook_url"] == ""
    assert clean["nested"][0]["session_id"] == ""
    assert clean["base_url"] == "https://example.invalid/v1?api_key=&version=1"
    assert not example.validate_example(clean)
    assert original["auth"] == "synthetic"


def test_validator_is_independent_of_scrubber_and_never_echoes_values(tmp_path, monkeypatch, capsys):
    src, dst = tmp_path / "in.json", tmp_path / "out.json"
    sentinel = "SENTINEL_MUST_NOT_BE_PRINTED"
    src.write_text(json.dumps({"feishu": {"webhook_url": sentinel}}), encoding="utf-8")
    dst.write_text("preserved", encoding="utf-8")
    monkeypatch.setattr(example, "scrub", lambda obj: obj)  # Simulate a missed scrubber rule.
    monkeypatch.setattr(sys, "argv", ["gen", "--src", str(src), "--dst", str(dst)])
    assert example.main() == 1
    assert dst.read_text(encoding="utf-8") == "preserved"
    captured = capsys.readouterr()
    assert sentinel not in captured.out + captured.err
    assert "webhook_url" in captured.err


def test_url_whitespace_cannot_bypass_scrubbing_or_validation():
    unsafe = {"base_url": " \t https://u:p@example.invalid/v1?api_key=synthetic "}
    assert example.validate_example(unsafe)
    clean = example.scrub(unsafe)
    assert clean["base_url"] == "https://example.invalid/v1?api_key="
    assert not example.validate_example(clean)


def test_generator_rejects_overwriting_source(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.json"
    path.write_text('{"api_key": "synthetic"}', encoding="utf-8")
    original = path.read_bytes()
    monkeypatch.setattr(sys, "argv", ["gen", "--src", str(path), "--dst", str(path)])
    assert example.main() == 1 and path.read_bytes() == original


def test_generator_produces_valid_synthetic_template(tmp_path, monkeypatch):
    src, dst = tmp_path / "in.json", tmp_path / "out.json"
    src.write_text(json.dumps({"feishu": {"webhook_url": "synthetic"},
                               "anchor": {"profile_dir": "anchor_profiles"}}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["gen", "--src", str(src), "--dst", str(dst)])
    assert example.main() == 0
    out = json.loads(dst.read_text(encoding="utf-8"))
    assert out["anchor"]["profile_dir"] == "anchor_profiles.example"
    assert not example.validate_example(out)
