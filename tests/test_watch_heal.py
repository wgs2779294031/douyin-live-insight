# -*- coding: utf-8 -*-
"""通道自愈 heal_channel 的单测（2026-10-05 实测场景：假 douyin.com 标签页）。"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import watch_anchor_live as w  # noqa: E402


def _fake_cdp_eval(monkeypatch, payload):
    calls = []

    def fake_eval(node, cdp, js, target_url, timeout=40):
        calls.append(js)
        if payload is None:
            raise RuntimeError("cdp eval 失败")
        return payload
    monkeypatch.setattr(w, "cdp_eval", fake_eval)
    return calls


def test_heal_skips_when_page_healthy(monkeypatch):
    """页面真加载了 douyin.com（href 同域 + complete + 有标题）→ 不动，返回 None。"""
    _fake_cdp_eval(monkeypatch, json.dumps({
        "href": "https://www.douyin.com/jingxuan", "ready": "complete",
        "title": "抖音精选电脑版"}))
    ran = []

    class FakeCompleted:
        returncode = 0
        stdout = '{"url": "https://www.douyin.com"}'

    monkeypatch.setattr(w.subprocess, "run",
                        lambda *a, **k: ran.append(a) or FakeCompleted())
    assert w.heal_channel("node", "cdp.mjs", "douyin.com") is None
    assert ran == []            # 健康页面不应触发 goto


def test_heal_navigates_when_blank(monkeypatch):
    """假 douyin.com（location 是 about:blank）→ 调 goto 修复，返回动作描述。"""
    _fake_cdp_eval(monkeypatch, json.dumps({
        "href": "about:blank", "ready": "complete", "title": ""}))
    ran = []

    class FakeCompleted:
        returncode = 0
        stdout = '{"url": "https://www.douyin.com", "title": "抖音"}'

    monkeypatch.setattr(w.subprocess, "run",
                        lambda *a, **k: ran.append(a) or FakeCompleted())
    out = w.heal_channel("node", "cdp.mjs", "douyin.com")
    assert out and "重新导航" in out
    assert ran and ran[0][0][1] == "cdp.mjs" and ran[0][0][2] == "goto"


def test_heal_reports_failure_when_goto_fails(monkeypatch):
    """goto 也失败（如浏览器整个没起）→ 返回失败描述，不抛异常。"""
    _fake_cdp_eval(monkeypatch, json.dumps({
        "href": "about:blank", "ready": "complete", "title": ""}))

    class FakeFailed:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(w.subprocess, "run", lambda *a, **k: FakeFailed())
    out = w.heal_channel("node", "cdp.mjs", "douyin.com")
    assert out and "未成功" in out


def test_heal_navigates_when_eval_unreachable(monkeypatch):
    """eval 本身不可达（端口死等）→ 也尝试 goto，不抛异常。"""
    _fake_cdp_eval(monkeypatch, None)
    ran = []

    class FakeCompleted:
        returncode = 0
        stdout = '{"url": "https://www.douyin.com"}'

    monkeypatch.setattr(w.subprocess, "run",
                        lambda *a, **k: ran.append(a) or FakeCompleted())
    out = w.heal_channel("node", "cdp.mjs", "douyin.com")
    assert out and "重新导航" in out
