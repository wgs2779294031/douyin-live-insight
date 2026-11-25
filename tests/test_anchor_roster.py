# -*- coding: utf-8 -*-
"""名册 + 实例管理单测。全部落 tmp_path，绝不碰真实 data/ 与 anchors.json。"""
import json
import os
import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import anchor_manager as am  # noqa: E402
import anchor_roster as ar  # noqa: E402

UID = "MS4wLjABAAAA" + "x" * 40


@pytest.fixture
def legacy_seed(monkeypatch):
    """Migration-shape regressions use eight explicitly synthetic accounts."""
    rows = [{"tag": f"synthetic_anchor_{index}", "name": f"synthetic_anchor_{index}",
             "sec_uid": "MS4wLjABAAAA" + f"synthetic_{index}_" + "x" * 40,
             "watch": True, "record": index < 4} for index in range(8)]
    monkeypatch.setattr(ar, "SEED", rows)


def _roster(tmp_path):
    r, created = ar.ensure(tmp_path / "anchors.json")
    return r


# ---------------- 名册 ----------------

def test_ensure_seeds_eight(tmp_path, legacy_seed):
    r, created = ar.ensure(tmp_path / "anchors.json")
    assert created is True
    assert len(r["anchors"]) == 8
    assert len(ar.watch_tags(r)) == 8
    assert len(ar.record_tags(r)) == 4
    # 二次调用不重生成
    r2, created2 = ar.ensure(tmp_path / "anchors.json")
    assert created2 is False
    assert r2["anchors"] == r["anchors"]


def test_broken_roster_backed_up_and_reseeded(tmp_path, legacy_seed):
    p = tmp_path / "anchors.json"
    p.write_text("{not json", encoding="utf-8")
    r, fell_back = ar.ensure(p)
    assert fell_back is True
    assert len(r["anchors"]) == 8
    # 坏文件保留证据，不被静默覆盖
    assert list(tmp_path.glob("anchors.json.broken-*"))


def test_duplicate_tag_rejected(tmp_path, legacy_seed):
    p = tmp_path / "anchors.json"
    ar.ensure(p)
    data = json.loads(p.read_text(encoding="utf-8"))
    data["anchors"].append(data["anchors"][0])
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    try:
        ar.load(p)
    except ValueError as e:
        assert "重复" in str(e)
        return
    raise AssertionError("重复 tag 应该报错")


def test_validate_shapes(tmp_path):
    assert ar.validate({"tag": "A", "sec_uid": UID, "watch": True}) == []
    assert ar.validate({"tag": "", "sec_uid": UID})           # 空 tag
    assert ar.validate({"tag": "A/B", "sec_uid": UID})        # 非法字符
    assert ar.validate({"tag": "A", "sec_uid": "abc"})        # sec_uid 形状
    assert ar.validate({"tag": "A", "sec_uid": UID,
                        "watch": False, "record": False})     # 两路全关


def test_upsert_and_remove(tmp_path, legacy_seed):
    r = _roster(tmp_path)
    e = ar.upsert(r, {"tag": "新人", "sec_uid": UID, "watch": True, "record": True})
    assert e["enabled"] is True and e["added_at"]
    assert len(r["anchors"]) == 9
    # 同 tag 再 upsert = 覆盖，不新增
    ar.upsert(r, {"tag": "新人", "sec_uid": UID, "watch": True, "llm": True})
    assert len(r["anchors"]) == 9
    assert ar.find(r, "新人")["llm"] is True
    ar.remove(r, "新人")
    assert ar.find(r, "新人") is None


def test_cannot_remove_last(tmp_path):
    r = {"version": 1, "anchors": [{"tag": "only", "sec_uid": UID, "watch": True}]}
    try:
        ar.remove(r, "only")
    except ValueError:
        return
    raise AssertionError("删最后一个应该报错")


def test_disabled_excluded_from_tags(tmp_path, legacy_seed):
    r = _roster(tmp_path)
    r["anchors"][0]["enabled"] = False
    tags = ar.watch_tags(r)
    assert r["anchors"][0]["tag"] not in tags
    assert len(tags) == 7
    assert len(ar.watch_tags(r, only_enabled=False)) == 8


def test_legacy_anchors_shape(tmp_path, legacy_seed):
    r = _roster(tmp_path)
    legacy = ar.as_legacy_anchors(r)
    assert len(legacy) == 8
    assert all(len(t) == 3 and t[0] == t[2] for t in legacy)


def test_save_creates_bak(tmp_path):
    p = tmp_path / "anchors.json"
    r, _ = ar.ensure(p)
    ar.save(r, p)                     # 第二次写应留 .bak
    assert (tmp_path / "anchors.json.bak").exists()


# ---------------- 实例状态 ----------------

def test_pid_alive_edges():
    assert am.pid_alive(0) is False
    assert am.pid_alive(-1) is False
    assert am.pid_alive(os.getpid()) is True


def test_pidfile_naming():
    assert am.pidfile("watch", "T").name == "watchdog_T.pid"
    assert am.pidfile("record", "T").name == "stream_T.pid"


def test_restart_anchor_stops_then_starts(monkeypatch):
    """重启 = 先停再起，且**不碰 enabled**（重启 ≠ 停用）。

    2026-10-28：老大在主播页找不到「启动」——名册 enabled=true 时只渲染「停止」，
    而进程其实早死了，只能去命令行。这条保证重启走的是「杀进程 + 拉起」，
    不会把名册开关改成 false（否则开机自启就不管它了）。
    """
    calls = []
    monkeypatch.setattr(am, "stop_anchor",
                        lambda tag, entry, kinds: calls.append(("stop", tag)) or {"watch": {"ok": True}})
    monkeypatch.setattr(am, "start_anchor",
                        lambda entry, kinds: calls.append(("start", entry["tag"])) or {"watch": {"ok": True}})

    entry = {"tag": "X", "watch": True, "record": False, "enabled": True}
    res = am.restart_anchor("X", entry, ("watch",))

    assert calls == [("stop", "X"), ("start", "X")]
    assert entry["enabled"] is True
    assert res["started"]["watch"]["ok"] is True


def test_state_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(am, "DATA", tmp_path)
    st = am.anchor_status({"tag": "X", "watch": True, "record": False, "enabled": False})
    assert st["state"] == "disabled"


def test_state_stopped_when_no_process(tmp_path, monkeypatch):
    monkeypatch.setattr(am, "DATA", tmp_path)
    st = am.anchor_status({"tag": "X", "watch": True, "record": False, "enabled": True})
    assert st["state"] == "stopped"
    assert st["watch_running"] is False


def test_state_stalled_when_watch_file_old(tmp_path, monkeypatch):
    """★ 2026-10-22 16:45 八路全停就是这种：进程在，但探测文件不再更新。"""
    monkeypatch.setattr(am, "DATA", tmp_path)
    monkeypatch.setattr(am, "instance_status",
                        lambda kind, tag: {"kind": kind, "pid": 123, "running": True})
    f = tmp_path / "watch_X.jsonl"
    f.write_text('{"ts": 1, "is_live": false}\n', encoding="utf-8")
    old = time.time() - (am.WATCH_STALE_SEC + 60)
    os.utime(f, (old, old))
    st = am.anchor_status({"tag": "X", "watch": True, "record": False, "enabled": True})
    assert st["state"] == "stalled"
    assert st["watch_age_sec"] > am.WATCH_STALE_SEC


def test_state_ok_when_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(am, "DATA", tmp_path)
    monkeypatch.setattr(am, "instance_status",
                        lambda kind, tag: {"kind": kind, "pid": 123, "running": True})
    (tmp_path / "watch_X.jsonl").write_text('{"ts": 1, "is_live": false}\n', encoding="utf-8")
    st = am.anchor_status({"tag": "X", "watch": True, "record": False, "enabled": True})
    assert st["state"] == "ok"


def test_health_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(am, "DATA", tmp_path)
    monkeypatch.setattr(am, "instance_status",
                        lambda kind, tag: {"kind": kind, "pid": 1, "running": True})
    f = tmp_path / "watch_A.jsonl"
    f.write_text('{"ts":1}\n', encoding="utf-8")
    old = time.time() - (am.WATCH_STALE_SEC + 60)
    os.utime(f, (old, old))
    (tmp_path / "watch_B.jsonl").write_text('{"ts":1}\n', encoding="utf-8")
    roster = {"anchors": [
        {"tag": "A", "watch": True, "enabled": True},
        {"tag": "B", "watch": True, "enabled": True},
        {"tag": "C", "watch": True, "enabled": False},
    ]}
    h = am.status_all(roster)["health"]
    assert h["total"] == 3
    assert h["enabled"] == 2
    assert h["stalled"] == 1
    assert h["problems"][0]["tag"] == "A"


def test_start_rejects_when_watch_flag_off(tmp_path, monkeypatch):
    monkeypatch.setattr(am, "DATA", tmp_path)
    out = am.start("watch", {"tag": "X", "sec_uid": UID, "watch": False})
    assert out["ok"] is False and "已关闭" in out["error"]


def test_stop_not_running_is_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(am, "DATA", tmp_path)
    out = am.stop("watch", "X")
    assert out["ok"] is True and out["state"] == "not_running"


def test_customer_default_roster_starts_empty(tmp_path):
    assert ar.SEED == []
    path = tmp_path / "anchors.json"
    roster, created = ar.ensure(path)
    assert created is True and roster["anchors"] == []
    assert ar.watch_tags(roster) == ar.record_tags(roster) == []
    second, created = ar.ensure(path)
    assert created is False and second["anchors"] == []


def test_existing_empty_customer_roster_is_valid_in_strict_mode(tmp_path, monkeypatch):
    path = tmp_path / "anchors.json"
    before = b'{"version":1,"anchors":[]}'
    path.write_bytes(before)
    monkeypatch.setenv("WB_REQUIRE_EXISTING_STATE", "1")
    roster, created = ar.ensure(path)
    assert created is False and roster["anchors"] == []
    assert path.read_bytes() == before


def test_corrupt_customer_default_roster_fails_closed(tmp_path):
    assert ar.SEED == []
    path = tmp_path / "anchors.json"
    before = b'{broken-customer-roster'
    path.write_bytes(before)
    with pytest.raises(ValueError):
        ar.ensure(path)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("anchors.json.broken-*"))


@pytest.mark.parametrize("entries", [["not-an-anchor"], [
    {"tag": "synthetic", "sec_uid": UID, "enabled": "false"},
]])
def test_malformed_customer_roster_preserves_original(tmp_path, entries):
    assert ar.SEED == []
    path = tmp_path / "anchors.json"
    before = json.dumps({"version": 1, "anchors": entries}).encode("utf-8")
    path.write_bytes(before)
    with pytest.raises(ValueError):
        ar.ensure(path)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("anchors.json.broken-*"))
