"""Fallback tests use synthetic browser responses and never contact Douyin."""
import json
import pathlib
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import watch_anchor_live as watch

TAG = "测试主播"
SEC = "fixture-owner"
RID = "123456"
TARGET = "https://www.douyin.com"


def cache(root, rooms):
    path = root / "data" / f"watch_{TAG}.config.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"rooms": rooms}), encoding="utf-8")


def page_result(js, response, http_ok=True):
    node = shutil.which("node") or watch.resolve_node()
    # Exercise the exact page JS including URL construction and identity comparison.
    script = f"""
        global.location = {{origin:'https://www.douyin.com'}};
        global.fetch = async (url,opts) => {{
          if (url.toString() !== 'https://live.douyin.com/webcast/room/web/enter/?aid=6383&device_platform=web&web_rid={RID}'
              || opts.credentials !== 'include' || opts.cache !== 'no-store') throw Error('fetch config');
          return {{ok:{json.dumps(http_ok)}, json:async()=>({json.dumps(response)})}};
        }};
        ({js}).then(x=>console.log(JSON.stringify(x)));
    """
    return subprocess.run([node, "-"], input=script, capture_output=True, text=True,
                          encoding="utf-8", check=True, timeout=8).stdout


def full(status=2, owner=SEC, ident="1912345678987654321"):
    return {"status_code":0, "data":{"data":[{"room_info":{"room":{
        "id_str":ident, "status":status, "user_count":1234, "title":"测试标题"},
        "owner":{"sec_uid":owner, "nickname":TAG}}}]}}


@pytest.mark.parametrize("response,http_ok,reason", [
    (full(), True, "verified_live"), (full(owner="different"), True, "owner_unconfirmed"),
    (full(owner=None), True, "owner_unconfirmed"), (full(status=4), True, "live_unconfirmed"),
    (full(ident="0"), True, "live_unconfirmed"), (full(ident=9312345678987654321), True, "live_unconfirmed"),
    ({"status_code":412, "data":full()["data"]}, True, "api_failure"),
    ({"status_code":0, "data":{"data":[]}}, True, "unconfirmed_room"), (full(),False,"http_failure"),
])
def test_room_enter_checks_real_js_then_watcher_state(monkeypatch, tmp_path, response, http_ok, reason):
    monkeypatch.setattr(watch, "ROOT", tmp_path)
    monkeypatch.setattr(watch, "DATA", tmp_path / "data")
    cache(tmp_path, [{"label":TAG, "room_id":RID}])
    def broken(*args):
        raise RuntimeError("https://sensitive.invalid/private-url")
    monkeypatch.setattr(watch, "poll_once", broken)
    monkeypatch.setattr(watch, "cdp_eval", lambda node,cdp,js,target: page_result(js,response,http_ok))
    row = watch.poll_with_fallback("node","cdp",SEC,TARGET,TAG)
    assert not watch.is_confirmed_offline(row)
    assert "private-url" not in json.dumps(row) and SEC not in json.dumps(row)
    if reason == "verified_live":
        assert row["probe_source"] == "room_enter_fallback" and row["is_live"]
        assert row["room_id_str"] == "1912345678987654321" and row["web_rid"] == RID
        assert row["room_status"] == 2 and watch.classify_env_failure(row) is None
    else:
        assert row["room_enter_result"] == reason and row["is_live"] is False
        assert watch.classify_env_failure(row) is not None


def test_primary_wins_for_both_confirmed_states(monkeypatch):
    monkeypatch.setattr(watch, "cached_web_rid", lambda tag: pytest.fail("unnecessary fallback"))
    for primary in ({"is_live":False,"live_status":0,"api_code":0},
                    {"is_live":True,"live_status":1,"web_rid":RID,"api_code":0}):
        monkeypatch.setattr(watch, "poll_once", lambda *args: primary)
        assert watch.poll_with_fallback("node","cdp",SEC,TARGET,TAG) is primary


def test_no_unique_cached_label_or_non_numeric_room_is_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(watch,"ROOT",tmp_path)
    monkeypatch.setattr(watch,"DATA",tmp_path / "data")
    monkeypatch.setattr(watch,"poll_once",lambda *a: {"is_live":False,"live_status":0,"api_code":403})
    monkeypatch.setattr(watch,"cdp_eval",lambda *a: pytest.fail("no usable cache"))
    for rooms in (None, [], [{"label":"其他","room_id":RID}], [{"label":TAG,"room_id":"https://invalid"}],
                  [{"label":TAG,"room_id":RID}]*2):
        if rooms is not None:
            cache(tmp_path,rooms)
        row = watch.poll_with_fallback("node","cdp",SEC,TARGET,TAG)
        assert row["room_enter_result"] == "no_cached_room" and not watch.is_confirmed_offline(row)


def test_cdp_failure_remains_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(watch,"ROOT",tmp_path)
    monkeypatch.setattr(watch,"DATA",tmp_path / "data")
    cache(tmp_path,[{"label":TAG,"room_id":RID}])
    monkeypatch.setattr(watch,"poll_once",lambda *a: {"is_live":False,"api_msg":"blocked"})
    def failed(*args):
        raise RuntimeError("https://sensitive.invalid/second-private-url")
    monkeypatch.setattr(watch,"cdp_eval",failed)
    row = watch.poll_with_fallback("node","cdp",SEC,TARGET,TAG)
    assert row["room_enter_result"]=="request_failed" and not watch.is_confirmed_offline(row)
    assert watch.classify_env_failure(row) == "auth"
    assert "second-private-url" not in json.dumps(row)
