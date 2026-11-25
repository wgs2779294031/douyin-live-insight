# -*- coding: utf-8 -*-
"""watch_anchor_live.classify_env_failure 的判定测试（纯函数，零网络）。"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import watch_anchor_live as wal  # noqa: E402


def _ok_live():
    return {"live_status": 1, "room_id_str": "7691567198174743346",
            "nick": "冤种小徐", "web_rid": "719712320231", "is_live": True}


def _ok_offline():
    return {"live_status": 0, "room_id_str": "0", "nick": "冤种小徐",
            "web_rid": None, "is_live": False}


def test_normal_states_are_not_failures():
    assert wal.classify_env_failure(_ok_live()) is None
    assert wal.classify_env_failure(_ok_offline()) is None


def test_blocked_api_msg_is_auth():
    rec = _ok_offline() | {"api_msg": "blocked", "api_code": -1,
                           "live_status": None, "nick": None, "room_id_str": None}
    assert wal.classify_env_failure(rec) == "auth"


def test_cdp_error_is_channel():
    assert wal.classify_env_failure(
        {"error": "cdp eval 失败（浏览器没起 / 没有可定位的标签页？）"}) == "channel"


def test_fetch_error_containing_blocked_is_auth():
    assert wal.classify_env_failure({"error": "页面 fetch 失败：…blocked…"}) == "auth"


def test_empty_shell_profile_is_auth():
    """接口返回空壳（三要素全空）→ 未登录/风控，不是「正常未开播」。"""
    rec = {"live_status": None, "room_id_str": None, "nick": None,
           "web_rid": None, "is_live": False, "api_msg": None}
    assert wal.classify_env_failure(rec) == "auth"
