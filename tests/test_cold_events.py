# -*- coding: utf-8 -*-
"""规则层冷场事件落盘（2026-10-26）。

背景：冷场判定一直是 analyzer（规则层）算的，但落盘只有 LLM 层写
`coldline_<date>.jsonl`（带救场台词）。于是「没开 LLM 的场次」冷场次数
永远是 0 —— 冤种小徐 2026-10-25 就是这么被误读成「全程没冷过场」的。
这里验的就是这份与 LLM 无关的落盘。
"""
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from room_runtime import ColdEventLog  # noqa: E402


def _read(tmp_path):
    p = tmp_path / ("cold_events_%s.jsonl" % time.strftime("%Y-%m-%d"))
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()
            if x.strip()]


def test_writes_start_and_end_with_duration(tmp_path):
    log = ColdEventLog(str(tmp_path), "测试主播A", "7700")
    log({"event": "cold_start", "ts": 1000, "count": 2, "threshold": 3.0})
    log({"event": "cold_end", "ts": 1090, "count": 6, "threshold": 6.0})
    log.close()

    rows = _read(tmp_path)
    assert [r["event"] for r in rows] == ["cold_start", "cold_end"]
    assert rows[0]["room_tag"] == "测试主播A"
    assert rows[0]["room_id"] == "7700"
    assert rows[0]["count"] == 2
    assert rows[1]["duration"] == 90


def test_orphan_end_is_dropped(tmp_path):
    """进程中途启动时可能只见到 cold_end（没见过 start）。

    这种孤立 end 算不出时长、复盘也不按它计数，直接丢弃 —— 2026-10-27 起
    cold_start 是「暂存到 cold_end 一起写」的，没有 start 就没有可写的对。
    """
    log = ColdEventLog(str(tmp_path), "X", "1")
    log({"event": "cold_end", "ts": 2000})
    log.close()

    p = tmp_path / ("cold_events_%s.jsonl" % time.strftime("%Y-%m-%d"))
    assert not p.exists() or p.read_text(encoding="utf-8").strip() == ""


def test_short_segment_filtered_by_min_duration(tmp_path):
    """低于 min_duration 的抖动段整段不落盘（start/end 都不写）。

    阈值降到 0.5/1.0 后，低频直播间会冒出大量「1 条弹幕进来就解除」的瞬时段
    （2026-10-26 研途回放 57 段里 38 段 < 30 秒），判定照旧但不该污染复盘。
    """
    log = ColdEventLog(str(tmp_path), "低频主播", "9", min_duration=30)
    log({"event": "cold_start", "ts": 1000, "count": 0})
    log({"event": "cold_end", "ts": 1010, "count": 1})      # 10 秒 → 抖动
    log({"event": "cold_start", "ts": 2000, "count": 0})
    log({"event": "cold_end", "ts": 2120, "count": 1})      # 120 秒 → 保留
    log.close()

    rows = _read(tmp_path)
    assert [r["event"] for r in rows] == ["cold_start", "cold_end"]
    assert rows[0]["ts"] == 2000
    assert rows[1]["duration"] == 120


def test_min_duration_zero_keeps_everything(tmp_path):
    log = ColdEventLog(str(tmp_path), "X", "1", min_duration=0)
    log({"event": "cold_start", "ts": 100})
    log({"event": "cold_end", "ts": 101})
    log.close()
    assert len(_read(tmp_path)) == 2


def test_consecutive_starts_keep_each_one(tmp_path):
    """两段连着来（上一段没闭合）→ 两行都落，duration 各算各的。"""
    log = ColdEventLog(str(tmp_path), "X", "1")
    log({"event": "cold_start", "ts": 100})
    log({"event": "cold_end", "ts": 160})
    log({"event": "cold_start", "ts": 200})
    log({"event": "cold_end", "ts": 260})
    log.close()

    rows = _read(tmp_path)
    assert [r.get("duration") for r in rows if r["event"] == "cold_end"] == [60, 60]
