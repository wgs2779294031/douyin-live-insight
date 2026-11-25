# -*- coding: utf-8 -*-
"""运行时闸门（data/llm_runtime.json）单测，零真实 API 调用。

这个闸门是 P5「秒生效」的根：`config.json` 只能管进程启动时要不要 LLM，
而六路 watch 各有 config 副本、改主配置传不过去 —— 所以开关必须落在
「每次调用前读一个共享文件」上。这里验的就是这条路径。
"""
import json

import pytest

import llm_client
from llm_client import LLMRouter


_PROVIDERS = {
    "doubao": {"api_key": "test-key", "base_url": "https://example.invalid",
               "model": "m1"},
    "deepseek": {"api_key": "test-key-2", "base_url": "https://example.invalid",
                 "model": "m2"},
}


class _Stub:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def chat(self, system, user, temperature=0.7,
             timeout_override=None, on_metrics=None, purpose=""):
        self.calls.append((system, user))
        return self.result


@pytest.fixture(autouse=True)
def _clear_gate_cache():
    """模块级缓存会跨用例串味儿，每个用例前后都清一次。"""
    def reset():
        llm_client._gate_cache.update(
            {"path": "", "mtime": 0.0, "ts": 0.0, "value": {}})
    reset()
    yield
    reset()


def _gate(tmp_path, payload):
    p = tmp_path / llm_client.GATE_FILENAME
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _router(tmp_path, gate_dir=None):
    r = LLMRouter(_PROVIDERS, "doubao", budget_dir=str(tmp_path),
                  gate_dir=str(gate_dir if gate_dir is not None else tmp_path))
    r._clients["doubao"] = _Stub("from-doubao")
    r._clients["deepseek"] = _Stub("from-deepseek")
    return r


def test_no_gate_file_keeps_old_behaviour(tmp_path):
    """没有闸门文件 = 不改变任何既有行为（config 说了算）。"""
    r = _router(tmp_path)
    assert r.chat("s", "u") == "from-doubao"


def test_gate_off_skips_call_entirely(tmp_path):
    _gate(tmp_path, {"enabled": False})
    r = _router(tmp_path)
    assert r.chat("s", "u") is None
    assert r._clients["doubao"].calls == []      # 一个请求都没发


def test_gate_on_allows_call(tmp_path):
    _gate(tmp_path, {"enabled": True})
    r = _router(tmp_path)
    assert r.chat("s", "u") == "from-doubao"


def test_gate_can_override_config_off(tmp_path):
    """config 层是关的、闸门说开 —— 以闸门为准（闸门是运行时权威）。"""
    _gate(tmp_path, {"enabled": True})
    r = _router(tmp_path)
    r.chat("s", "u")
    assert r._clients["doubao"].calls == [("s", "u")]


def test_gate_switches_provider_immediately(tmp_path):
    _gate(tmp_path, {"enabled": True, "active_provider": "deepseek"})
    r = _router(tmp_path)
    assert r.chat("s", "u") == "from-deepseek"
    assert r.active == "deepseek"


def test_unknown_provider_in_gate_is_ignored(tmp_path):
    _gate(tmp_path, {"enabled": True, "active_provider": "不存在的"})
    r = _router(tmp_path)
    assert r.chat("s", "u") == "from-doubao"


def test_legacy_nested_shape_is_tolerated(tmp_path):
    """容忍 {"llm": {"enabled": false}} 这种嵌套写法，别因为格式不同就失效。"""
    _gate(tmp_path, {"llm": {"enabled": False}})
    r = _router(tmp_path)
    assert r.chat("s", "u") is None


def test_broken_json_closes_gate(tmp_path):
    """已有闸门损坏不能按缺省放行，关闭调用等待修复。"""
    (tmp_path / llm_client.GATE_FILENAME).write_text("{oops", encoding="utf-8")
    r = _router(tmp_path)
    assert r.chat("s", "u") is None
    assert r._clients["doubao"].calls == []


def test_gate_change_takes_effect_after_ttl(tmp_path, monkeypatch):
    """版本变更立即生效，TTL 过后即使版本不变也重新确认可读。"""
    p = _gate(tmp_path, {"enabled": True})
    monkeypatch.setattr(llm_client, "GATE_CACHE_TTL", 0.0)
    r = _router(tmp_path)
    assert r.chat("s", "u") == "from-doubao"

    p.write_text(json.dumps({"enabled": False}), encoding="utf-8")
    assert r.chat("s", "u") is None              # TTL=0 → 立刻读到新值


def test_gate_disabled_by_default_without_gate_dir(tmp_path):
    """没传 gate_dir（临时脚本 / 单测直接 new）→ 完全不读闸门。"""
    _gate(tmp_path, {"enabled": False})
    r = LLMRouter(_PROVIDERS, "doubao", budget_dir=str(tmp_path))
    r._clients["doubao"] = _Stub("ok")
    assert r.chat("s", "u") == "ok"


# ---------------- 用途维度（2026-10-25） ----------------
# 实时台词辅助与对外复盘建议是两个消费者，以前只能同开同关。

def test_purpose_switch_blocks_only_that_purpose(tmp_path):
    """purposes.review=false：复盘调用被拦，实时照常。"""
    _gate(tmp_path, {"enabled": True,
                     "purposes": {"realtime": True, "review": False}})
    r = _router(tmp_path)
    stub = r._clients["doubao"]
    assert r.chat("s", "u", purpose="realtime") == "from-doubao"
    assert len(stub.calls) == 1
    assert r.chat("s", "u", purpose="review") is None
    assert len(stub.calls) == 1, "被闸门拦下的调用不该打到 client"


def test_purpose_unset_passes_through(tmp_path):
    """闸门没写 purposes → 不限制任何用途（旧行为）。"""
    _gate(tmp_path, {"enabled": True})
    r = _router(tmp_path)
    assert r.chat("s", "u", purpose="review") == "from-doubao"
    assert r.chat("s", "u", purpose="realtime") == "from-doubao"


def test_global_off_beats_purpose_on(tmp_path):
    """enabled=false 与 purposes 是与关系：用途开着也调不动。"""
    _gate(tmp_path, {"enabled": False, "purposes": {"realtime": True}})
    r = _router(tmp_path)
    assert r.chat("s", "u", purpose="realtime") is None


def test_purpose_only_affects_named_one(tmp_path):
    """只关 realtime 时，review 仍可调。"""
    _gate(tmp_path, {"enabled": True, "purposes": {"realtime": False}})
    r = _router(tmp_path)
    assert r.chat("s", "u", purpose="realtime") is None
    assert r.chat("s", "u", purpose="review") == "from-doubao"
    assert r.chat("s", "u") == "from-doubao", "不传 purpose 的调用不受用途开关影响"
