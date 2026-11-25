"""Concurrent state changes use synthetic files and never touch the real roster/data."""
import json
import pathlib
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import anchor_roster as ar
import assist_board_server as board
import board_llm


def _roster(path, llm=True):
    ar.save({"version": 1, "anchors": [
        {"tag": tag, "sec_uid": "MS4wLjABAAAA" + "x" * 40,
         "watch": True, "record": True, "llm": llm, "enabled": True}
        for tag in ("A", "B")
    ]}, path)


def _handler(path, data, tag, body=None):
    h = board.Handler.__new__(board.Handler)
    h.roster_path = path
    h.store = SimpleNamespace(data_dir=str(data))
    h._query = lambda: {"tag": tag, "kind": "all"}
    h._auth_user = lambda: "synthetic-test"
    h._read_json = lambda: body
    h._process_llm_flag = lambda tag: True
    h._json = lambda code, body, *args: (code, body)
    # Unit handlers have no socket; real bounded rejection is covered over HTTP.
    h._reject_json = h._json
    return h


_UID = "MS4wLjABAAAA" + "x_-a" * 10
_PROFILE = f"https://www.douyin.com/user/{_UID}"


@pytest.mark.parametrize("value", [
    _UID, _UID + "?from_tab_name=live",
    _PROFILE + "/?from_tab_name=live#profile",
    "复制这段分享文字\n" + _PROFILE + "，打开抖音查看。",
    "来看看 " + f"https://www.iesdouyin.com/share/user/{_UID}/?from=share",
])
def test_add_anchor_canonicalizes_only_new_entry(tmp_path, value):
    path = tmp_path / "anchors.json"
    _roster(path)
    old = ar.load(path)
    old["anchors"][0]["sec_uid"] += "?from_tab_name=keep"
    ar.save(old, path)
    h = _handler(path, tmp_path, "new", {"tag": "new", "sec_uid": value})
    h.path = "/api/anchors"
    h._csrf_ok = lambda: True
    status, reply = h.do_POST()
    assert status == 200 and reply["anchor"]["sec_uid"] == _UID
    stored = json.loads(path.read_text("utf-8"))["anchors"]
    assert stored[-1]["sec_uid"] == _UID
    assert stored[0]["sec_uid"] == old["anchors"][0]["sec_uid"]


@pytest.mark.parametrize("value", [
    "https://v.douyin.com/XyZ/", "https://live.douyin.com/12345",
    f"https://www.douyin.com/video/{_UID}", f"http://localhost/user/{_UID}",
    f"https://www.douyin.com.evil.test/user/{_UID}",
    f"https://evil.test@www.douyin.com/user/{_UID}",
    "https://other.test/?url=" + _PROFILE, _PROFILE + " " + _PROFILE,
    _PROFILE + "?other=" + _UID + "B", _UID + "?other=" + _UID + "B",
    f"file://www.douyin.com/user/{_UID}", "普通文字 " + _UID,
])
def test_add_anchor_rejects_bad_input_before_writing(tmp_path, value):
    path = tmp_path / "anchors.json"
    _roster(path)
    before = path.read_bytes()
    h = _handler(path, tmp_path, "new", {"tag": "new", "sec_uid": value})
    h.path = "/api/anchors"
    h._csrf_ok = lambda: True
    assert h.do_POST()[0] == 400
    assert path.read_bytes() == before


@pytest.mark.parametrize("value", [_UID + "=", _UID + "==", "MS4wLjABAAAA" + "a" * 26 + "==",
                                   "MS4wLjABAAAA" + "a" * 242 + "=="])
def test_anchor_uid_preserves_url_safe_base64_padding_and_bounds(value):
    assert ar.parse_anchor_uid(value) == value
    assert ar.parse_anchor_uid("https://www.douyin.com/user/" + value + "?from_tab_name=live") == value


@pytest.mark.parametrize("value", [_UID + "===", _UID + "=abc", "MS4wLjABAAAA" + "a" * 245])
def test_anchor_uid_rejects_bad_padding_and_oversize(value):
    with pytest.raises(ValueError):
        ar.parse_anchor_uid(value)


def _children(commands):
    procs = [subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, encoding="utf-8",
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
             for cmd in commands]
    try:
        for proc in procs:
            out, err = proc.communicate(timeout=20)
            assert proc.returncode == 0, (out, err)
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


def test_roster_read_modify_replace_is_a_process_transaction(tmp_path):
    path = tmp_path / "anchors.json"
    _roster(path)
    worker = r'''
import sys,time
sys.path.insert(0,sys.argv[1])
import anchor_roster as ar
path,tag=sys.argv[2:]
for i in range(6):
    def change(roster):
        item=ar.find(roster,tag)
        count=int(item.get("note") or 0)
        time.sleep(.02)
        item["note"]=str(count+1)
    ar.update(change,path)
'''
    _children([[sys.executable, "-X", "utf8", "-c", worker,
                str(ROOT / "scripts"), str(path), tag]
               for tag in ("A", "A", "B", "B")])
    roster = ar.load(path)
    assert ar.find(roster, "A")["note"] == "12"
    assert ar.find(roster, "B")["note"] == "12"


def test_gate_concurrent_process_updates_preserve_every_tag(tmp_path):
    worker = r'''
import sys
sys.path.insert(0,sys.argv[1])
import board_llm
for i in range(5):
    board_llm.write_gate(sys.argv[2],tag_off=(sys.argv[3]+str(i),True),actor="test")
'''
    _children([[sys.executable, "-X", "utf8", "-c", worker,
                str(ROOT / "scripts"), str(tmp_path), str(i)] for i in range(4)])
    assert set(board_llm.read_tags_off(str(tmp_path))) == {
        str(p) + str(i) for p in range(4) for i in range(5)}


def test_http_changes_to_different_anchors_do_not_overwrite(tmp_path, monkeypatch):
    path, data = tmp_path / "anchors.json", tmp_path / "runtime"
    _roster(path)
    original_load = ar.load

    def slow_load(path):
        roster = original_load(path)
        time.sleep(.04)  # Force overlap if the lock covers only the save.
        return roster

    monkeypatch.setattr(ar, "load", slow_load)
    monkeypatch.setattr(board.anchors_mgr, "stop", lambda *args: {"ok": True})
    handlers = [_handler(path, data, tag) for tag in ("A", "B")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda h: h._anchor_patch({"llm": False}), handlers))
    assert all(code == 200 for code, _ in results)
    assert all(not a["llm"] for a in ar.load(path)["anchors"])
    assert set(board_llm.read_tags_off(str(data))) == {"A", "B"}


def test_same_anchor_off_then_on_keeps_gate_and_roster_consistent(tmp_path, monkeypatch):
    path, data = tmp_path / "anchors.json", tmp_path / "runtime"
    _roster(path)
    off_started, release_off = threading.Event(), threading.Event()
    real_write = board_llm.write_gate

    def pause_off(*args, **kwargs):
        if kwargs.get("tag_off") == ("A", True):
            off_started.set()
            assert release_off.wait(5)
        return real_write(*args, **kwargs)

    monkeypatch.setattr(board_llm, "write_gate", pause_off)
    with ThreadPoolExecutor(max_workers=2) as pool:
        off = pool.submit(_handler(path, data, "A")._anchor_patch, {"llm": False})
        try:
            assert off_started.wait(5)
            on = pool.submit(_handler(path, data, "A")._anchor_patch, {"llm": True})
            time.sleep(.04)
        finally:
            release_off.set()
        assert off.result(timeout=5)[0] == on.result(timeout=5)[0] == 200
    assert ar.find(ar.load(path), "A")["llm"] is True
    assert "A" not in board_llm.read_tags_off(str(data))


@pytest.mark.parametrize("enable", [False, True])
def test_failed_roster_save_does_not_open_runtime_gate(tmp_path, enable):
    path, data = tmp_path / "anchors.json", tmp_path / "runtime"
    _roster(path, llm=not enable)
    if enable:
        board_llm.write_gate(str(data), tag_off=("A", True))
    h = _handler(path, data, "A")

    def fail_save(roster):
        raise OSError("synthetic disk failure")

    h._save_roster = fail_save
    with pytest.raises(OSError, match="synthetic"):
        h._anchor_patch({"llm": enable})
    assert "A" in board_llm.read_tags_off(str(data))
    assert ar.find(ar.load(path), "A")["llm"] is (not enable)


def test_delete_and_patch_do_not_resurrect_deleted_anchor(tmp_path, monkeypatch):
    path, data = tmp_path / "anchors.json", tmp_path / "runtime"
    _roster(path)
    monkeypatch.setattr(board.anchors_mgr, "stop_anchor", lambda *args:
                        {"watch": {"ok": True}, "record": {"ok": True}})
    h_delete, h_patch = _handler(path, data, "B"), _handler(path, data, "A")
    with ThreadPoolExecutor(max_workers=2) as pool:
        removed = pool.submit(h_delete._anchor_delete, {"tag": "B"})
        patched = pool.submit(h_patch._anchor_patch, {"llm": False})
        assert removed.result(timeout=5)[0] == patched.result(timeout=5)[0] == 200
    roster = ar.load(path)
    assert ar.find(roster, "B") is None
    assert ar.find(roster, "A")["llm"] is False


def test_roster_replace_failure_preserves_original_and_cleans_own_temp(tmp_path, monkeypatch):
    path = tmp_path / "anchors.json"
    _roster(path)
    original = path.read_bytes()
    names = []

    def fail_replace(src, dst):
        names.append(str(src))
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(ar.os, "replace", fail_replace)
    for _ in range(2):
        with pytest.raises(OSError, match="synthetic"):
            ar.update(lambda r: ar.find(r, "A").update(llm=False), path)
    assert path.read_bytes() == original
    assert len(set(names)) == 2
    assert not list(tmp_path.glob("*.tmp"))


def test_bad_gate_is_not_silently_replaced_or_unblocked(tmp_path):
    path = tmp_path / "llm_runtime.json"
    path.write_text('{"tags_off": [', encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="闸门"):
        board_llm.write_gate(str(tmp_path), tag_off=("A", False))
    assert path.read_bytes() == before


def test_unauthenticated_patch_does_not_enter_write_transaction(tmp_path):
    h = _handler(tmp_path / "anchors.json", tmp_path / "runtime", "A")
    h.path = "/api/anchors?tag=A"
    h._csrf_ok = lambda: False
    assert h.do_PATCH()[0] == 403
    assert not list(tmp_path.iterdir())
