# -*- coding: utf-8 -*-
"""复盘聚合单测：data/ 用 tmp 构造，绝不碰真实落盘。"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import board_review as br  # noqa: E402

TAG = "测试主播A"
DATE = "2026-10-10"
TS = 1791630000


def _wl(path, rows):
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _mk(tmp_path):
    (tmp_path / ("danmu_douyin_%s_%s.jsonl" % (TAG, DATE))).write_text("", encoding="utf-8")
    _wl(tmp_path / ("danmu_douyin_%s_%s.jsonl" % (TAG, DATE)), [
        {"ts": TS, "type": "danmaku", "uname": "甲", "text": "hi", "room_id": "r1"},
        {"ts": TS + 10, "type": "enter", "text": "进入了", "room_id": "r1"},
        {"ts": TS + 70, "type": "danmaku", "uname": "乙", "text": "yo", "room_id": "r1"},
        {"ts": TS + 130, "type": "like", "text": "点了3个赞", "room_id": "r1"},
    ])
    _wl(tmp_path / ("stats_douyin_%s_%s.jsonl" % (TAG, DATE)), [
        {"ts": TS + 5, "type": "room_stats", "room_id": "r1", "online": 100},
        {"ts": TS + 15, "type": "room_stats", "room_id": "r1", "online": 140},
        {"ts": TS + 80, "type": "room_user_seq", "room_id": "r1", "online": 90, "total_pv": 5000},
    ])
    _wl(tmp_path / ("coldline_%s.jsonl" % DATE), [
        {"ts": TS + 30, "room_id": "r1", "event": "cold_start", "lines": ["台词A"]},
        {"ts": TS + 40, "room_id": "rX", "event": "cold_start", "lines": ["别人家的"]},
    ])
    return br.review_tag(str(tmp_path), DATE, TAG)


def test_counts_and_permin(tmp_path):
    d = _mk(tmp_path)
    assert d["danmu"]["total"] == 4
    assert d["danmu"]["counts"] == {"danmaku": 2, "enter": 1, "like": 1}
    # TS 与 TS+70 同属一分钟？TS%60 未知，所以只断言「有 1~2 个桶且总数=2」
    permin = d["danmu"]["permin"]
    assert 1 <= len(permin) <= 2
    assert sum(x["danmaku"] for x in permin) == 2
    assert all(x["ts"] % 60 == 0 for x in permin)


def test_online_series_and_peak(tmp_path):
    d = _mk(tmp_path)
    # 每分钟取最后一点：TS+5/TS+15 同分钟取 140；TS+80 单独一分钟取 90
    assert d["stats"]["peak_online"] == 140
    assert d["stats"]["total_pv"] == 5000
    assert len(d["stats"]["online_series"]) == 2


def test_coldline_filtered_by_room(tmp_path):
    d = _mk(tmp_path)
    events = d["coldline"]["events"]
    assert d["coldline"]["filter"] == "by_room_id"
    assert len(events) == 1
    assert events[0]["lines"] == ["台词A"]
    # 没有规则层文件（老场次）→ 退回旧口径：source=llm，数 cold_start
    assert d["coldline"]["source"] == "llm"
    assert d["coldline"]["count"] == 1


def test_cold_events_rules_take_priority(tmp_path):
    """有 cold_events_*.jsonl 就用它算次数/时长，与开不开 LLM 无关。"""
    _mk(tmp_path)          # 建基础文件（含一条 coldline，模拟「同一天还开了 LLM」）
    _wl(tmp_path / ("cold_events_%s.jsonl" % DATE), [
        {"ts": TS + 20, "event": "cold_start", "room_tag": TAG,
         "room_id": "r1", "count": 2, "threshold": 3.0},
        {"ts": TS + 140, "event": "cold_end", "room_tag": TAG,
         "room_id": "r1", "count": 6, "threshold": 6.0, "duration": 120},
        {"ts": TS + 200, "event": "cold_start", "room_tag": TAG,
         "room_id": "r1", "count": 1, "threshold": 3.0},
        {"ts": TS + 260, "event": "cold_start", "room_tag": "别人家",
         "room_id": "r1", "count": 9, "threshold": 3.0},
    ])
    d = br.review_tag(str(tmp_path), DATE, TAG)
    cl = d["coldline"]
    assert cl["source"] == "rules"
    assert cl["count"] == 2                    # 第二段未闭合，也算一段
    assert cl["total_seconds"] == 120          # 未闭合那段贡献 0
    assert len(cl["sessions"]) == 2
    assert cl["sessions"][0]["duration"] == 120


def test_latest_danmu_capped(tmp_path):
    d = _mk(tmp_path)
    assert len(d["danmu"]["latest"]) == 4
    assert d["danmu"]["latest"][-1]["text"] == "点了3个赞"


def test_cache_hit(tmp_path):
    d1 = _mk(tmp_path)
    d2 = br.review_tag(str(tmp_path), DATE, TAG)
    assert d1 == d2
    assert (str(tmp_path), DATE, TAG) in br._cache


def test_empty_day(tmp_path):
    d = br.review_tag(str(tmp_path), DATE, TAG)
    assert d["empty"] is True
    assert d["danmu"]["total"] == 0
    assert d["stats"]["peak_online"] is None


def test_available_dates_and_tags(tmp_path):
    _mk(tmp_path)
    assert DATE in br.available_dates(str(tmp_path))
    assert TAG in br.available_tags(str(tmp_path))
    assert TAG in br.available_tags(str(tmp_path), DATE)
    assert br.available_tags(str(tmp_path), "2026-01-01") == []


def test_dates_sorted_desc(tmp_path):
    for d in ("2026-10-08", "2026-10-10", "2026-10-09"):
        _wl(tmp_path / ("danmu_douyin_x_%s.jsonl" % d), [{"ts": TS, "type": "danmaku"}])
    got = br.available_dates(str(tmp_path))
    assert got[:3] == ["2026-10-10", "2026-10-09", "2026-10-08"]
