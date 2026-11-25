# -*- coding: utf-8 -*-
"""闸门 / 配置写入单测（P5 写的那半截）。

这里验的是「写坏会出大事」的那几个点：原子替换、备份、只动 llm 段、副本同步。
不验 HTTP（在 test_board_server_auth.py 里）。
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import board_llm  # noqa: E402


def _cfg(path, llm_enabled=False, key="sk-old-1111", extra_top=True):
    obj = {"llm": {"enabled": llm_enabled,
                   "active_provider": "doubao",
                   "daily_token_budget": 1000,
                   "providers": {"doubao": {"api_key": key, "model": "m",
                                            "base_url": "https://x.invalid"}}}}
    if extra_top:
        obj["feishu"] = {"webhook_url": "https://hook.invalid", "app_id": "cli_x"}
        obj["room"] = {"id": "123"}
    obj["llm"].setdefault("coldline_enabled", True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _tree(tmp_path):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    for tag in ("甲", "乙"):
        _cfg(data / ("watch_%s.config.json" % tag))
    return data


def test_write_gate_creates_file_with_meta(tmp_path):
    data = _tree(tmp_path)
    out = board_llm.write_gate(str(data), enabled=True, active_provider="doubao",
                               actor="test")
    assert out["enabled"] is True and out["updated_by"] == "test" and out["updated_at"]
    saved = json.loads((data / "llm_runtime.json").read_text(encoding="utf-8"))
    assert saved["enabled"] is True
    assert not (data / "llm_runtime.json.tmp").exists()   # 原子写不留 tmp


def test_write_gate_keeps_previous_keys(tmp_path):
    data = _tree(tmp_path)
    board_llm.write_gate(str(data), enabled=True)
    board_llm.write_gate(str(data), active_provider="deepseek")
    saved = json.loads((data / "llm_runtime.json").read_text(encoding="utf-8"))
    assert saved["enabled"] is True                 # 第二次没传 enabled，不能丢
    assert saved["active_provider"] == "deepseek"


def test_sync_config_updates_main_and_copies(tmp_path):
    data = _tree(tmp_path)
    cfg = _cfg(tmp_path / "config.json", llm_enabled=False)
    r = board_llm.sync_llm_config(str(cfg), str(data), enabled=True)

    assert r["enabled"] is True and r["count"] == 2
    main = json.loads(cfg.read_text(encoding="utf-8"))
    assert main["llm"]["enabled"] is True
    assert main["feishu"]["app_id"] == "cli_x"        # llm 段以外的内容不许动
    for tag in ("甲", "乙"):
        sub = json.loads((data / ("watch_%s.config.json" % tag)).read_text(encoding="utf-8"))
        assert sub["llm"]["enabled"] is True


def test_sync_config_backs_up_before_overwrite(tmp_path):
    data = _tree(tmp_path)
    cfg = _cfg(tmp_path / "config.json", llm_enabled=False, key="sk-original")
    board_llm.sync_llm_config(str(cfg), str(data), enabled=True)
    bak = tmp_path / "config.json.bak"
    assert bak.exists()
    assert json.loads(bak.read_text(encoding="utf-8"))["llm"]["enabled"] is False


def test_sync_config_without_copies(tmp_path):
    data = _tree(tmp_path)
    cfg = _cfg(tmp_path / "config.json")
    r = board_llm.sync_llm_config(str(cfg), str(data), enabled=True, sync_copies=False)
    assert r["count"] == 0
    sub = json.loads((data / "watch_甲.config.json").read_text(encoding="utf-8"))
    assert sub["llm"]["enabled"] is False


def test_set_key_updates_everywhere_and_does_not_leak(tmp_path):
    data = _tree(tmp_path)
    cfg = _cfg(tmp_path / "config.json")
    r = board_llm.set_key(str(cfg), str(data), "doubao", "sk-brand-new-9999")
    assert r["count"] == 2
    main = json.loads(cfg.read_text(encoding="utf-8"))
    assert main["llm"]["providers"]["doubao"]["api_key"] == "sk-brand-new-9999"
    sub = json.loads((data / "watch_甲.config.json").read_text(encoding="utf-8"))
    assert sub["llm"]["providers"]["doubao"]["api_key"] == "sk-brand-new-9999"

    # 读接口只回尾 4 位
    st = board_llm.read_status(str(data), str(cfg), "2026-10-23")
    assert "sk-brand-new-9999" not in json.dumps(st, ensure_ascii=False)
    assert st["providers"][0]["key_tail"] == ""  # Never return key substrings.


def test_read_status_reports_runtime_source_after_write(tmp_path):
    data = _tree(tmp_path)
    cfg = _cfg(tmp_path / "config.json", llm_enabled=True)
    board_llm.write_gate(str(data), enabled=False)
    st = board_llm.read_status(str(data), str(cfg), "2026-10-23")
    assert st["enabled"] is False and st["enabled_source"] == "runtime"
