# -*- coding: utf-8 -*-
"""看板 HTTP 层鉴权测试：真起 server，验公开/受限路径、登录、Cookie、CSRF、限流。

纪律：数据目录与用户库全部落 tmp_path，绝不碰真实 data/。
"""
import contextlib
import http.client
import json
import pathlib
import sys
import threading
import time
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import assist_board_server as ab  # noqa: E402
import board_auth as auth          # noqa: E402
import board_broadcast as bb       # noqa: E402

DATE = "2026-10-10"
HTML = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "assist_board.html"


@contextlib.contextmanager
def running_board(tmp_path, max_fails=8):
    store = ab.Store(str(tmp_path), DATE)
    db, plain = auth.ensure_db(str(tmp_path / "board_users.json"))
    bc = bb.Broadcaster(store.snapshot)

    saved = (ab.Handler.store, ab.Handler.broadcaster, ab.Handler.auth_db,
             ab.Handler.throttle, ab.Handler.html_path, ab.Handler.dist_dir,
             ab.Handler.started_at, ab.Handler.users_path, ab.Handler.config_path)
    ab.Handler.store = store
    ab.Handler.broadcaster = bc
    ab.Handler.auth_db = db
    ab.Handler.throttle = auth.LoginThrottle(max_fails=max_fails, window=60)
    ab.Handler.html_path = str(HTML)
    ab.Handler.dist_dir = None
    ab.Handler.started_at = 0.0
    # ★ 必须指向 tmp：否则 /api/passwd 会去改真实 data/board_users.json 的密码
    ab.Handler.users_path = str(tmp_path / "board_users.json")
    # ★ 同理：/api/llm 会读 config.json（里面有真实 api_key，只出尾 4 位也不该读）
    ab.Handler.config_path = str(tmp_path / "config.json")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), ab.Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield port, plain, db
    finally:
        srv.shutdown()
        srv.server_close()
        # Handler 是同一个类，类属性（auth_db/users_path/store）在测试间共享：
        # 等已启动的 handler 线程收尾，否则它会串到下一个测试的类属性上（偶发 403/401 错乱）。
        time.sleep(0.15)
        (ab.Handler.store, ab.Handler.broadcaster, ab.Handler.auth_db,
         ab.Handler.throttle, ab.Handler.html_path, ab.Handler.dist_dir,
         ab.Handler.started_at, ab.Handler.users_path, ab.Handler.config_path) = saved


def _req(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        h = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        return r.status, r.read(), dict(r.getheaders())
    finally:
        conn.close()


def _login(port, password, username="admin", headers=None):
    return _req(port, "POST", "/api/login",
                {"username": username, "password": password}, headers)


def _cookie_of(headers):
    raw = headers.get("Set-Cookie") or ""
    return raw.split(";", 1)[0]


def test_logged_in_form_saves_profile_uid_over_http(tmp_path, monkeypatch):
    path = tmp_path / "anchors.json"
    monkeypatch.setattr(ab.Handler, "roster_path", str(path))
    uid = "MS4wLjABAAAA" + "T" * 40
    with running_board(tmp_path) as (port, password, _db):
        status, _, headers = _login(port, password)
        assert status == 200
        headers = {"Cookie": _cookie_of(headers), "Origin": f"http://127.0.0.1:{port}"}
        form = {"tag": "fixture", "sec_uid": f"分享主页 https://www.douyin.com/user/{uid}?from_tab_name=l"}
        status, result, _ = _req(port, "POST", "/api/anchors", form, headers)
        assert status == 200 and json.loads(result)["anchor"]["sec_uid"] == uid
        before = path.read_bytes()
        assert json.loads(before)["anchors"][-1]["sec_uid"] == uid
        form["sec_uid"] = "https://v.douyin.com/example/"
        assert _req(port, "POST", "/api/anchors", form, headers)[0] == 400
        assert path.read_bytes() == before


# ---------------- 公开 / 受限 ----------------

def test_health_is_public(tmp_path):
    with running_board(tmp_path) as (port, _pw, _db):
        code, body, _ = _req(port, "GET", "/api/health")
        assert code == 200
        doc = json.loads(body)
        assert doc["ok"] is True and doc["version"]


def test_index_is_public_shell(tmp_path):
    """HTML 外壳可公开（不含数据）；数据接口才要鉴权。"""
    with running_board(tmp_path) as (port, _pw, _db):
        code, body, _ = _req(port, "GET", "/")
        assert code == 200
        assert b"<html" in body.lower() or b"<!doctype" in body.lower()


def test_live_requires_auth(tmp_path):
    with running_board(tmp_path) as (port, _pw, _db):
        assert _req(port, "GET", "/api/live")[0] == 401


def test_snapshot_legacy_path_also_locked(tmp_path):
    with running_board(tmp_path) as (port, _pw, _db):
        assert _req(port, "GET", "/snapshot")[0] == 401


def test_sse_requires_auth(tmp_path):
    with running_board(tmp_path) as (port, _pw, _db):
        assert _req(port, "GET", "/api/sse")[0] == 401
        assert _req(port, "GET", "/sse")[0] == 401


def test_static_404_is_json(tmp_path):
    with running_board(tmp_path) as (port, _pw, _db):
        assert _req(port, "GET", "/assets/x.js")[0] == 404


# ---------------- 登录 ----------------

def test_login_wrong_password(tmp_path):
    with running_board(tmp_path) as (port, _pw, _db):
        code, body, headers = _login(port, "wrong")
        assert code == 401
        assert "Set-Cookie" not in headers


def test_login_then_access_with_cookie(tmp_path):
    with running_board(tmp_path) as (port, pw, _db):
        code, body, headers = _login(port, pw)
        assert code == 200
        assert json.loads(body)["user"] == "admin"
        cookie = _cookie_of(headers)
        assert cookie.startswith("wb_token=")
        assert "HttpOnly" in headers["Set-Cookie"]

        code, body, _ = _req(port, "GET", "/api/live", headers={"Cookie": cookie})
        assert code == 200
        doc = json.loads(body)
        assert "tags" in doc and doc["date"] == DATE

        code, body, _ = _req(port, "GET", "/api/me", headers={"Cookie": cookie})
        assert code == 200
        assert json.loads(body)["user"] == "admin"


def test_login_default_username(tmp_path):
    """只传 password 时按 admin 登录（前端可省略用户名）。"""
    with running_board(tmp_path) as (port, pw, _db):
        code, _, _ = _req(port, "POST", "/api/login", {"password": pw})
        assert code == 200


def test_login_bad_body(tmp_path):
    with running_board(tmp_path) as (port, _pw, _db):
        assert _req(port, "POST", "/api/login", {"username": "admin"})[0] == 400


def test_login_csrf_origin_mismatch(tmp_path):
    with running_board(tmp_path) as (port, pw, _db):
        code, _, _ = _login(port, pw, headers={"Origin": "http://evil.example"})
        assert code == 403


def test_login_origin_match_ok(tmp_path):
    with running_board(tmp_path) as (port, pw, _db):
        code, _, _ = _login(port, pw, headers={"Origin": "http://127.0.0.1:%d" % port})
        assert code == 200


def test_logout_clears_cookie(tmp_path):
    with running_board(tmp_path) as (port, pw, _db):
        _, _, headers = _login(port, pw)
        cookie = _cookie_of(headers)
        code, _, h2 = _req(port, "POST", "/api/logout", headers={"Cookie": cookie})
        assert code == 200
        assert "Max-Age=0" in h2["Set-Cookie"]


# ---------------- 限流 ----------------

def test_login_throttled_after_repeated_failures(tmp_path):
    with running_board(tmp_path, max_fails=3) as (port, pw, _db):
        for _ in range(3):
            assert _login(port, "wrong")[0] == 401
        code, body, _ = _login(port, pw)
        assert code == 429
        assert "频繁" in json.loads(body)["error"]


def test_write_api_requires_auth(tmp_path):
    with running_board(tmp_path) as (port, _pw, _db):
        assert _req(port, "POST", "/api/logout")[0] == 401
        assert _req(port, "POST", "/api/anything")[0] == 401


# ---------------- 改密码 ----------------

def test_passwd_requires_auth(tmp_path):
    with running_board(tmp_path) as (port, _pw, _db):
        assert _req(port, "POST", "/api/passwd", {"old": "x", "new": "yyyyyy"})[0] == 401


def test_passwd_changes_and_old_rejected(tmp_path):
    with running_board(tmp_path) as (port, pw, _db):
        _, _, h = _login(port, pw)
        code, body, _ = _req(port, "POST", "/api/passwd",
                             {"old": pw, "new": "newpass123"},
                             headers={"Cookie": _cookie_of(h)})
        assert code == 200 and json.loads(body)["ok"] is True
        assert _login(port, "newpass123")[0] == 200     # 新密码可用
        assert _login(port, pw)[0] == 401               # 旧密码立即失效


def test_passwd_rejects_wrong_old(tmp_path):
    with running_board(tmp_path) as (port, pw, _db):
        _, _, h = _login(port, pw)
        code, body, _ = _req(port, "POST", "/api/passwd",
                             {"old": "nope", "new": "newpass123"},
                             headers={"Cookie": _cookie_of(h)})
        assert code == 400
        assert "当前密码" in json.loads(body)["error"]


def test_passwd_rejects_short(tmp_path):
    with running_board(tmp_path) as (port, pw, _db):
        _, _, h = _login(port, pw)
        code, _, _ = _req(port, "POST", "/api/passwd",
                          {"old": pw, "new": "123"},
                          headers={"Cookie": _cookie_of(h)})
        assert code == 400


def test_passwd_does_not_touch_real_users_file(tmp_path):
    """回归：跑测试绝不能改真实 data/board_users.json 的密码。"""
    real = pathlib.Path(auth.DEFAULT_USERS_PATH)
    before = real.read_bytes() if real.exists() else None
    with running_board(tmp_path) as (port, pw, _db):
        _, _, h = _login(port, pw)
        _req(port, "POST", "/api/passwd", {"old": pw, "new": "whatever123"},
             headers={"Cookie": _cookie_of(h)})
    after = real.read_bytes() if real.exists() else None
    assert before == after


# ---------------- LLM 只读状态（P5 前半截）----------------

def test_llm_requires_auth(tmp_path):
    with running_board(tmp_path) as (port, _pw, _db):
        assert _req(port, "GET", "/api/llm")[0] == 401


def test_llm_returns_status_without_config(tmp_path):
    """设置页调这个接口，配置缺失时必须 200 + 空值，不能 500。"""
    with running_board(tmp_path) as (port, pw, _db):
        cookie = _cookie_of(_login(port, pw)[2])
        code, body, _ = _req(port, "GET", "/api/llm", headers={"Cookie": cookie})
        assert code == 200
        doc = json.loads(body)
        assert doc["available"] is False
        assert doc["providers"] == []
        assert doc["today"]["total_tokens"] == 0
        assert doc["errors"]


# ---------------- P5：LLM 闸门（写）----------------

def test_llm_gate_requires_auth(tmp_path):
    with running_board(tmp_path) as (port, _pw, _db):
        assert _req(port, "POST", "/api/llm/gate", {"enabled": True})[0] == 401


def test_llm_gate_rejects_cross_site(tmp_path):
    with running_board(tmp_path) as (port, pw, _db):
        cookie = _cookie_of(_login(port, pw)[2])
        code, _, _ = _req(port, "POST", "/api/llm/gate", {"enabled": True},
                          headers={"Cookie": cookie, "Origin": "https://evil.example"})
        assert code == 403


def test_llm_gate_writes_gate_file(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"llm": {"enabled": False, "providers": {}}}), encoding="utf-8")
    with running_board(tmp_path) as (port, pw, _db):
        cookie = _cookie_of(_login(port, pw)[2])
        code, body, _ = _req(port, "POST", "/api/llm/gate", {"enabled": True},
                             headers={"Cookie": cookie})
        assert code == 200
        doc = json.loads(body)
        assert doc["enabled"] is True and doc["enabled_source"] == "runtime"
        assert doc.get("hint")
        gate = json.loads((tmp_path / "llm_runtime.json").read_text(encoding="utf-8"))
        assert gate["enabled"] is True and gate["updated_by"] == "admin"


def test_llm_gate_intent_cannot_claim_permission_with_missing_config(tmp_path):
    with running_board(tmp_path) as (port, pw, _db):
        cookie = _cookie_of(_login(port, pw)[2])
        code, body, _ = _req(port, "POST", "/api/llm/gate", {"enabled": True}, headers={"Cookie": cookie})
        assert code == 200
        result = json.loads(body)
        assert result["enabled"] is False
        assert result["control"]["reason"] == "control_unavailable"


def test_llm_provider_rejects_unknown(tmp_path):
    with running_board(tmp_path) as (port, pw, _db):
        cookie = _cookie_of(_login(port, pw)[2])
        sentinel = "synthetic-key-pasted-into-provider"
        code, body, _ = _req(port, "POST", "/api/llm/provider", {"provider": sentinel},
                              headers={"Cookie": cookie})
        assert code == 400
        assert sentinel.encode() not in body
        assert json.loads(body)["error"] == "没有这个提供商，请从已配置列表选择。"


def test_legacy_serves_old_page(tmp_path):
    """P4 尾款：/legacy 必须吐旧单页，不能被 SPA 回退吃成新前端。"""
    with running_board(tmp_path) as (port, pw, _db):
        cookie = _cookie_of(_login(port, pw)[2])
        code, body, _ = _req(port, "GET", "/legacy", headers={"Cookie": cookie})
        assert code == 200
        assert body == HTML.read_bytes()
