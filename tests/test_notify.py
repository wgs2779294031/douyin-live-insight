# -*- coding: utf-8 -*-
"""notify.send_text 的单测（纯逻辑：缺配置/限频/请求体构造）。

2026-10-07 起实现走模块内 _OPENER（恒直连、绕过系统代理），patch 点随之
改为 notify._OPENER——patch urllib.request.urlopen 拦不到它。
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import notify  # noqa: E402


class _FakeOpener:
    """替身 opener：记录请求并按 responder 返回响应。"""

    def __init__(self, responder):
        self.responder = responder
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        return self.responder(req)


def _write_cfg(tmp_path, app_id="cli_x", secret="s", chat_id="oc_1"):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"feishu": {
        "app_id": app_id, "app_secret": secret, "alert_chat_id": chat_id,
    }}), encoding="utf-8")
    return str(p)


def _token_responder(captured_body=None):
    """返回一个 responder：token 请求回 code=0，发消息请求回 code=0。"""

    class R:
        def __init__(self, body):
            self._b = body

        def read(self):
            return self._b

    def responder(req):
        if "tenant_access_token" in req.full_url:
            return R(b'{"code":0,"tenant_access_token":"tok"}')
        return R(b'{"code":0}')

    return responder


def test_send_skips_when_not_configured(tmp_path, monkeypatch):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"feishu": {}}), encoding="utf-8")
    calls = []

    def boom(req):
        calls.append(req)
        raise AssertionError("缺凭据不应发出任何请求")

    monkeypatch.setattr(notify, "_OPENER", _FakeOpener(boom))
    assert notify.send_text("x", config_path=str(p)) is False
    assert calls == []                      # 缺凭据不发任何请求


def test_send_rate_limited_per_key(tmp_path, monkeypatch):
    p = _write_cfg(tmp_path)
    monkeypatch.setattr(notify, "_OPENER",
                        _FakeOpener(_token_responder()))
    assert notify.send_text("a", key="k1", cooldown=600, config_path=str(p)) is True
    assert notify.send_text("a", key="k1", cooldown=600, config_path=str(p)) is False
    assert notify.send_text("b", key="k2", cooldown=600, config_path=str(p)) is True


def test_send_request_shape(tmp_path, monkeypatch):
    p = _write_cfg(tmp_path)
    fake = _FakeOpener(_token_responder())
    monkeypatch.setattr(notify, "_OPENER", fake)

    assert notify.send_text("测试告警", config_path=str(p)) is True
    assert len(fake.requests) == 2          # 一次 token + 一次发消息
    msg_req = fake.requests[1]
    assert "receive_id_type=chat_id" in msg_req.full_url
    body = json.loads(msg_req.data.decode())
    assert body["receive_id"] == "oc_1"
    assert body["msg_type"] == "text"
    assert "测试" in json.loads(body["content"])["text"]
