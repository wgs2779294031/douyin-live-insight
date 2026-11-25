# -*- coding: utf-8 -*-
"""鉴权层单测：密码 / token / cookie / 用户库 / 限流。

不碰真实 data/：所有用户库都落在 tmp_path。
"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import board_auth as auth  # noqa: E402


# ---------------- 密码 ----------------

def test_hash_and_verify():
    salt, h = auth.hash_password("pa55word")
    assert auth.verify_password("pa55word", salt, h) is True
    assert auth.verify_password("wrong", salt, h) is False
    # 同一密码两次哈希盐不同 → 结果不同
    salt2, h2 = auth.hash_password("pa55word")
    assert h != h2


def test_hash_rejects_empty():
    try:
        auth.hash_password("")
    except ValueError:
        return
    raise AssertionError("空密码应抛 ValueError")


def test_verify_password_bad_salt():
    assert auth.verify_password("x", "not-hex", "00") is False


# ---------------- token ----------------

def test_token_roundtrip():
    tok = auth.make_token("admin", "s3cret")
    assert isinstance(tok, str) and "|" not in tok       # 已被 base64 包住
    assert auth.verify_token(tok, "s3cret") == "admin"


def test_token_wrong_secret():
    tok = auth.make_token("admin", "s3cret")
    assert auth.verify_token(tok, "other") is None


def test_token_expired():
    tok = auth.make_token("admin", "s3cret", ttl=10, now=1000)
    assert auth.verify_token(tok, "s3cret", now=1005) == "admin"
    assert auth.verify_token(tok, "s3cret", now=1011) is None


def test_token_tampered():
    tok = auth.make_token("admin", "s3cret")
    assert auth.verify_token(tok[:-2] + "zz", "s3cret") is None
    assert auth.verify_token("garbage", "s3cret") is None
    assert auth.verify_token("", "s3cret") is None


def test_token_rename_breaks_signature():
    """把用户名字段换掉必须验签失败（否则等于提权）。"""
    tok = auth.make_token("admin", "s3cret")
    raw = auth._b64d(tok).decode()
    user, exp, sig = raw.rsplit("|", 2)
    forged = auth._b64e(("hacker|%s|%s" % (exp, sig)).encode())
    assert auth.verify_token(forged, "s3cret") is None


# ---------------- cookie ----------------

def test_parse_cookies():
    got = auth.parse_cookies("a=1; wb_token=abc.def; b=2")
    assert got["wb_token"] == "abc.def"
    assert auth.parse_cookies(None) == {}
    assert auth.parse_cookies("") == {}


def test_cookie_header_flags():
    h = auth.cookie_header("tok", max_age=60)
    assert "wb_token=tok" in h
    assert "HttpOnly" in h and "SameSite=Lax" in h and "Max-Age=60" in h
    assert "Max-Age=0" in auth.clear_cookie_header()


# ---------------- 用户库 ----------------

def test_ensure_db_creates_and_is_idempotent(tmp_path):
    p = str(tmp_path / "board_users.json")
    db, plain = auth.ensure_db(p)
    assert plain and len(plain) >= 10
    assert db["secret"] and db["users"][0]["name"] == "admin"
    # 初始密码文件
    assert (tmp_path / "board_initial_password.txt").exists()
    # 二次调用不再重生成
    db2, plain2 = auth.ensure_db(p)
    assert plain2 is None
    assert db2["secret"] == db["secret"]


def test_check_login(tmp_path):
    p = str(tmp_path / "u.json")
    db, plain = auth.ensure_db(p)
    assert auth.check_login(db, "admin", plain) is True
    assert auth.check_login(db, "admin", "nope") is False
    assert auth.check_login(db, "ghost", plain) is False


def test_add_set_remove_user(tmp_path):
    p = str(tmp_path / "u.json")
    auth.ensure_db(p)
    auth.add_user(p, "xiaoyu", "pw123456", role="member")
    db = auth.load_db(p)
    assert auth.check_login(db, "xiaoyu", "pw123456")
    assert auth.find_user(db, "xiaoyu")["role"] == "member"

    auth.set_password(p, "xiaoyu", "newpw999")
    db = auth.load_db(p)
    assert auth.check_login(db, "xiaoyu", "newpw999")
    assert not auth.check_login(db, "xiaoyu", "pw123456")

    auth.remove_user(p, "xiaoyu")
    assert auth.find_user(auth.load_db(p), "xiaoyu") is None


def test_cannot_remove_last_user(tmp_path):
    p = str(tmp_path / "u.json")
    auth.ensure_db(p)
    try:
        auth.remove_user(p, "admin")
    except ValueError:
        return
    raise AssertionError("删最后一个用户应被拒绝")


def test_load_db_rejects_broken(tmp_path):
    p = tmp_path / "u.json"
    p.write_text("{not json", encoding="utf-8")
    assert auth.load_db(str(p)) is None
    p.write_text('{"users": []}', encoding="utf-8")
    assert auth.load_db(str(p)) is None


# ---------------- 请求鉴权 ----------------

def test_user_from_headers(tmp_path):
    p = str(tmp_path / "u.json")
    db, _ = auth.ensure_db(p)
    tok = auth.make_token("admin", db["secret"])
    assert auth.user_from_headers({"Cookie": "wb_token=" + tok}, db) == "admin"
    assert auth.user_from_headers({"Cookie": "wb_token=bad"}, db) is None
    assert auth.user_from_headers({}, db) is None
    assert auth.user_from_headers({"Cookie": "wb_token=" + tok}, None) is None


def test_token_invalid_after_user_removed(tmp_path):
    p = str(tmp_path / "u.json")
    db, _ = auth.ensure_db(p)
    auth.add_user(p, "temp", "pw123456")
    db = auth.load_db(p)
    tok = auth.make_token("temp", db["secret"])
    assert auth.user_from_headers({"Cookie": "wb_token=" + tok}, db) == "temp"
    auth.remove_user(p, "temp")
    db = auth.load_db(p)
    assert auth.user_from_headers({"Cookie": "wb_token=" + tok}, db) is None


# ---------------- 限流 ----------------

def test_throttle_blocks_after_threshold():
    th = auth.LoginThrottle(max_fails=3, window=60)
    assert not th.blocked("1.1.1.1", now=1000)
    for i in range(3):
        th.fail("1.1.1.1", now=1000 + i)
    assert th.blocked("1.1.1.1", now=1003)
    assert not th.blocked("2.2.2.2", now=1003)      # 别的 IP 不受影响
    # 窗口过了自动解封
    assert not th.blocked("1.1.1.1", now=1065)


def test_throttle_reset():
    th = auth.LoginThrottle(max_fails=2, window=60)
    th.fail("1.1.1.1", now=1000)
    th.fail("1.1.1.1", now=1001)
    assert th.blocked("1.1.1.1", now=1002)
    th.reset("1.1.1.1")
    assert not th.blocked("1.1.1.1", now=1002)
