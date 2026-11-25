# -*- coding: utf-8 -*-
"""日 token 预算护栏测试（DailyTokenBudget + LLMRouter 集成，零真实 API 调用）。"""

import json

from llm_client import DailyTokenBudget, LLMRouter


_PROVIDERS = {
    "doubao": {"api_key": "test-key", "base_url": "https://example.invalid",
               "model": "test-model"},
}


def test_add_accumulates_and_persists_across_instances(tmp_path):
    b1 = DailyTokenBudget(1600000, str(tmp_path))
    b1.add(100, 20)
    b1.add(30, 0)
    # 新实例（模拟日报进程）读同一本账
    b2 = DailyTokenBudget(1600000, str(tmp_path))
    assert b2.check() is True
    assert b2.last_total == 150


def test_check_fails_after_budget_exhausted(tmp_path):
    b = DailyTokenBudget(100, str(tmp_path))
    assert b.check() is True
    b.add(60, 50)
    assert b.check() is False
    assert b.last_total == 110


def test_zero_budget_disables_and_writes_nothing(tmp_path):
    b = DailyTokenBudget(0, str(tmp_path))
    assert b.check() is True
    b.add(999, 999)
    assert list(tmp_path.iterdir()) == []   # 不写任何记账文件


def test_router_skips_call_when_budget_exhausted(tmp_path):
    """单 provider 预算用满后 chat 直接返回 None，且不发起任何网络调用。"""
    DailyTokenBudget(1, str(tmp_path)).add(1, 0)   # 预填：已用 1/1
    seen = []
    router = LLMRouter(_PROVIDERS, "doubao",
                       on_metrics=seen.append,
                       daily_token_budget=1, budget_dir=str(tmp_path))
    assert router.chat("sys", "user") is None
    # metrics 里能看到「因预算跳过」，便于在 llm_metrics 里统计
    assert any(r.get("error_type") == "daily_budget_exhausted" for r in seen)


_TWO_PROVIDERS = {
    "doubao": {"api_key": "test-key", "base_url": "https://example.invalid",
               "model": "test-model"},
    "deepseek": {"api_key": "test-key-2", "base_url": "https://example.invalid",
                 "model": "test-model-2"},
}


class _StubClient:
    """替身 LLMClient：chat 返回预设值，记录调用参数。"""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def chat(self, system, user, temperature=0.7,
             timeout_override=None, on_metrics=None, purpose=""):
        self.calls.append((system, user))
        return self.result


def test_router_borrows_backup_when_budget_exhausted(tmp_path):
    """主超护栏 → 借道备用发出调用并返回其结果；active 不切换。"""
    DailyTokenBudget(1, str(tmp_path)).add(1, 0)   # 预填：已用 1/1
    stub = _StubClient("borrowed-ok")
    router = LLMRouter(_TWO_PROVIDERS, "doubao",
                       daily_token_budget=1, budget_dir=str(tmp_path))
    router._clients["deepseek"] = stub
    assert router.chat("sys", "user") == "borrowed-ok"
    assert stub.calls == [("sys", "user")]
    assert router.active == "doubao"     # 借道不动切换状态机


def test_router_falls_back_to_skip_when_borrow_fails(tmp_path):
    """主超护栏 + 借道也失败 → 返回 None，记 daily_budget_exhausted。"""
    DailyTokenBudget(1, str(tmp_path)).add(1, 0)
    seen = []
    router = LLMRouter(_TWO_PROVIDERS, "doubao", on_metrics=seen.append,
                       daily_token_budget=1, budget_dir=str(tmp_path))
    router._clients["deepseek"] = _StubClient(None)
    assert router.chat("sys", "user") is None
    assert any(r.get("error_type") == "daily_budget_exhausted" for r in seen)


def test_probe_does_not_switch_back_while_budget_exhausted(tmp_path):
    """主 API 通但护栏超限：探测成功也不切回，且退避不翻倍。"""
    DailyTokenBudget(1, str(tmp_path)).add(1, 0)
    router = LLMRouter(_TWO_PROVIDERS, "doubao",
                       daily_token_budget=1, budget_dir=str(tmp_path))
    router.active = "deepseek"            # 模拟此前已切到备
    router.probe_backoff = 1800

    class _P:
        def probe(self, probe_timeout=5, on_metrics=None):
            return True

    router._clients["doubao"] = _P()
    router.probe_and_maybe_switch()
    assert router.active == "deepseek"    # 超账期间不切回
    assert router.probe_backoff == 1800   # 退避不翻倍

    # 账本重置（删当日账，check 归零）后探测成功即切回
    import os
    os.remove(str(tmp_path / ("llm_budget_%s.json" % __import__("time").strftime("%Y-%m-%d"))))
    router.last_probe_ts = 0.0
    router.probe_and_maybe_switch()
    assert router.active == "doubao"


_THREE_PROVIDERS = dict(_TWO_PROVIDERS, deepseek_official={
    "api_key": "test-key-3", "base_url": "https://example.invalid",
    "model": "test-model-3"})


def test_three_level_chain_parses_in_config_order(tmp_path):
    """三级链按 config 顺序解析：doubao -> deepseek -> deepseek_official。"""
    router = LLMRouter(_THREE_PROVIDERS, "doubao",
                       daily_token_budget=0, budget_dir=str(tmp_path))
    assert router.backup_chain == ["deepseek", "deepseek_official"]
    assert router.backup == "deepseek"


class _FailClient(_StubClient):
    """恒失败的替身 client。"""

    def chat(self, system, user, temperature=0.7,
             timeout_override=None, on_metrics=None, purpose=""):
        return None


def test_failure_walks_down_the_chain_then_wraps(tmp_path):
    """连续失败沿链前进：doubao->deepseek->deepseek_official，链尾回主。"""
    router = LLMRouter(_THREE_PROVIDERS, "doubao",
                       daily_token_budget=0, budget_dir=str(tmp_path))
    for n in ("doubao", "deepseek", "deepseek_official"):
        router._clients[n] = _FailClient(None)
    assert router._other_provider() == "deepseek"
    router.active = "deepseek"
    assert router._other_provider() == "deepseek_official"
    router.active = "deepseek_official"
    assert router._other_provider() == "doubao"     # 链尾回主


def test_budget_borrow_walks_chain_to_official(tmp_path):
    """主超护栏：先借方舟，失败再借官方，官方成功则返回其结果。"""
    DailyTokenBudget(1, str(tmp_path)).add(1, 0)
    router = LLMRouter(_THREE_PROVIDERS, "doubao",
                       daily_token_budget=1, budget_dir=str(tmp_path))
    ark = _StubClient(None)               # 方舟挂
    off = _StubClient("official-ok")      # 官方活着
    router._clients["deepseek"] = ark
    router._clients["deepseek_official"] = off
    assert router.chat("sys", "user") == "official-ok"
    assert ark.calls == [("sys", "user")]
    assert off.calls == [("sys", "user")]
    assert router.active == "doubao"      # 借道不动状态机


def test_router_accounting_via_metrics_callback(tmp_path):
    """记账走 metrics 回调流：带 token 用量的记录进账本（probe 不计），且原样转发。"""
    seen = []
    router = LLMRouter(_PROVIDERS, "doubao", on_metrics=seen.append,
                       daily_token_budget=1000, budget_dir=str(tmp_path))
    # 模拟 LLMClient.chat 成功后发出的 metrics 记录（经 router 包装后的回调）
    router._on_metrics({"success": True, "prompt_tokens": 300, "completion_tokens": 50})
    router._on_metrics({"success": True, "probe": True, "prompt_tokens": 1})
    b = DailyTokenBudget(1000, str(tmp_path))
    assert b.check() is True          # 350 < 1000
    assert b.last_total == 350
    assert len(seen) == 2             # 两条记录都原样转发给真正的 metrics 回调


def test_config_field_wires_from_dict():
    from llm_service import LLMConfig
    cfg = LLMConfig.from_dict({"enabled": True, "daily_token_budget": 500})
    assert cfg.daily_token_budget == 500
    assert LLMConfig.from_dict({}).daily_token_budget == 1_000_000  # 默认 50%
    cfg2 = LLMConfig.from_dict(
        {"backup_daily_token_budgets": {"deepseek": 1800000}})
    assert cfg2.backup_daily_token_budgets == {"deepseek": 1800000}


def test_split_budgets_per_provider(tmp_path):
    """分账本：主调用记账进主账（旧文件名），借道记账进备用自己的账本。"""
    router = LLMRouter(_TWO_PROVIDERS, "doubao", on_metrics=lambda r: None,
                       daily_token_budget=1000, budget_dir=str(tmp_path),
                       backup_token_budgets={"deepseek": 2000})
    # 主调用 300 token → 主账（llm_budget_<date>.json）
    router._on_metrics({"success": True, "provider": "doubao",
                        "prompt_tokens": 300, "completion_tokens": 0})
    # 借道调用 500 token → deepseek 账（llm_budget_deepseek_<date>.json）
    router._on_metrics({"success": True, "provider": "deepseek",
                        "prompt_tokens": 500, "completion_tokens": 0})
    import time
    day = time.strftime("%Y-%m-%d")
    import json, os
    main = json.load(open(tmp_path / ("llm_budget_%s.json" % day),
                          encoding="utf-8"))
    ds = json.load(open(tmp_path / ("llm_budget_deepseek_%s.json" % day),
                        encoding="utf-8"))
    assert main["total"] == 300 and main["budget"] == 1000
    assert ds["total"] == 500 and ds["budget"] == 2000


def test_borrow_skips_backup_over_its_own_budget(tmp_path):
    """主超账 + 方舟备用也超自己的账 → 跳过它借道官方；全超则跳过本次。"""
    DailyTokenBudget(1, str(tmp_path)).add(1, 0)          # 主账满
    router = LLMRouter(_THREE_PROVIDERS, "doubao",
                       daily_token_budget=1, budget_dir=str(tmp_path),
                       backup_token_budgets={"deepseek": 1})
    DailyTokenBudget(1, str(tmp_path), name="deepseek").add(1, 0)  # 方舟账也满
    off = _StubClient("official-ok")
    ark = _StubClient("ark-ok")
    router._clients["deepseek"] = ark
    router._clients["deepseek_official"] = off
    assert router.chat("sys", "user") == "official-ok"    # 越过满账的方舟
    assert ark.calls == []
    assert off.calls == [("sys", "user")]

    # 全链账满：主满 + 备满 → 本次跳过
    router2 = LLMRouter(_TWO_PROVIDERS, "doubao",
                        daily_token_budget=1, budget_dir=str(tmp_path),
                        backup_token_budgets={"deepseek": 1})
    router2._clients["deepseek"] = _StubClient("x")
    assert router2.chat("sys", "user") is None


def test_budget_file_is_atomically_valid_json(tmp_path):
    b = DailyTokenBudget(100, str(tmp_path))
    b.add(10, 5)
    path = tmp_path / ("llm_budget_%s.json" % __import__("time").strftime("%Y-%m-%d"))
    d = json.loads(path.read_text(encoding="utf-8"))
    assert d["total"] == 15 and d["budget"] == 100
    assert not list(tmp_path.glob("*.tmp"))   # 原子替换后无残留
