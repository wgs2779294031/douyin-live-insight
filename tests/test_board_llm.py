# -*- coding: utf-8 -*-
"""LLM 只读状态（board_llm）单测。

重点不是「值对不对」，而是**缺文件时不能炸**：这个接口存在的唯一理由是
设置页不该再出现 404 / 白屏。2026-10-23 第一版就在「今天没有 metrics 文件」
这条路径上抛了 KeyError('total_tokens')，真机 500 —— 所以这里把缺文件
当主用例写。
"""
import json
import pathlib
import sys
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import board_llm  # noqa: E402

DATE = "2026-10-23"


def _write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return path


def _cfg(tmp_path, **llm_over):
    llm = {
        "enabled": False,
        "active_provider": "doubao",
        "daily_token_budget": 1600000,
        "providers": {
            "doubao": {"api_key": "sk-abcdef123456", "model": "doubao-lite",
                       "base_url": "https://ark.example/api/v3"},
            "deepseek": {"api_key": "sk-zzzz9999", "model": "deepseek-chat",
                         "base_url": "https://api.deepseek.com"},
        },
    }
    llm.update(llm_over)
    return _write(tmp_path / "config.json", {"llm": llm})


def test_missing_config_does_not_raise(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    out = board_llm.read_status(str(data), str(tmp_path / "nope.json"), DATE)
    assert out["available"] is False
    assert out["providers"] == []
    assert out["today"]["total_tokens"] == 0
    assert out["errors"]


def test_missing_metrics_file_gives_zeros(tmp_path):
    """真实场景：LLM 关着，今天根本没有 llm_metrics_<date>.jsonl。"""
    cfg = _cfg(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    out = board_llm.read_status(str(data), str(cfg), DATE)
    assert out["available"] is True
    assert out["today"]["calls"] == 0
    assert out["today"]["total_tokens"] == 0
    assert out["errors"] == []


def test_providers_and_key_tail(tmp_path):
    cfg = _cfg(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    out = board_llm.read_status(str(data), str(cfg), DATE)
    names = [p["name"] for p in out["providers"]]
    assert names == ["doubao", "deepseek"]
    doubao = out["providers"][0]
    assert doubao["active"] is True
    assert doubao["key_tail"] == ""  # Status exposes presence only, including short keys.
    assert doubao["has_key"] is True
    # 明文 key 绝不能出现在返回体里（含长度/前缀）
    raw = json.dumps(out, ensure_ascii=False)
    assert "sk-abcdef123456" not in raw
    assert "abcdef" not in raw


def test_metrics_skip_probe_and_shunt_by_provider(tmp_path):
    cfg = _cfg(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    rows = [
        {"provider": "doubao", "success": True, "prompt_tokens": 100, "completion_tokens": 10},
        {"provider": "doubao", "success": False, "prompt_tokens": 50, "completion_tokens": 0},
        {"provider": "deepseek", "success": True, "prompt_tokens": 7, "completion_tokens": 3},
        {"provider": "doubao", "success": True, "prompt_tokens": 999, "completion_tokens": 999,
         "probe": True},
    ]
    # Legacy records still need a timestamp to be assigned to an actual call day.
    for row in rows:
        row["ts"] = datetime.fromisoformat(DATE + "T12:00:00").timestamp()
    p = data / ("llm_metrics_%s.jsonl" % DATE)
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    out = board_llm.read_status(str(data), str(cfg), DATE)
    t = out["today"]
    assert t["calls"] == 3           # 探活那条不算业务调用
    assert t["probe_calls"] == 1
    assert t["failed"] == 1
    assert t["prompt_tokens"] == 157
    assert t["total_tokens"] == 157 + 13
    groups = {row["provider"]: row for row in t["by_provider"]}
    assert groups["doubao"]["metric_records"] == 2
    assert groups["deepseek"]["metric_records"] == 1
    assert t["coverage"]["legacy_records"] == 3 and t["cost_cny"] is None


def test_bad_json_line_is_skipped(tmp_path):
    cfg = _cfg(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    p = data / ("llm_metrics_%s.jsonl" % DATE)
    row = {"provider": "doubao", "prompt_tokens": 5, "completion_tokens": 1,
           "ts": datetime.fromisoformat(DATE + "T12:00:00").timestamp()}
    p.write_text(json.dumps(row) + "\nnot json at all\n\n", encoding="utf-8")
    out = board_llm.read_status(str(data), str(cfg), DATE)
    assert out["today"]["calls"] == 1
    assert out["today"]["total_tokens"] == 6
    assert out["today"]["coverage"]["invalid_records"] == 1
    assert out["today"]["cost_cny"] is None


def test_runtime_gate_overrides_config(tmp_path):
    cfg = _cfg(tmp_path)                      # config: enabled=False
    data = tmp_path / "data"
    data.mkdir()
    _write(data / "llm_runtime.json", {"enabled": True})
    out = board_llm.read_status(str(data), str(cfg), DATE)
    assert out["enabled"] is True
    assert out["enabled_source"] == "runtime"


def test_budget_file_wins_over_config(tmp_path):
    cfg = _cfg(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    # 主账无 provider 后缀（2026-10-23 实测的真实文件名）
    _write(data / ("llm_budget_%s.json" % DATE),
           {"date": DATE, "total": 40000, "budget": 1600000})
    out = board_llm.read_status(str(data), str(cfg), DATE)
    assert out["budget"]["used_tokens"] == 40000
    assert out["budget"]["cap_tokens"] == 1600000
    assert out["budget"]["ratio"] == 0.025


def test_budget_falls_back_to_config_cap(tmp_path):
    cfg = _cfg(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    out = board_llm.read_status(str(data), str(cfg), DATE)
    assert out["budget"]["cap_tokens"] == 1600000
    assert out["budget"]["used_tokens"] == 0
