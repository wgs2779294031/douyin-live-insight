# -*- coding: utf-8 -*-
"""福袋大池扫描脚本单测：攒池去重 + 扫池落盘/命中统计（不触网、不起浏览器）。

运行：pytest tests/test_sweep_lottery_pool.py -v
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import sweep_lottery_pool as sw  # noqa: E402


def _room(web_rid, id_str, nick="主播", user_count=0, title="测试直播间"):
    return {"web_rid": web_rid, "id_str": id_str, "nick": nick,
            "title": title, "user_count": user_count}


class TestBuildPool:
    def test_dedup_by_id_str_and_sort_by_user_count(self, monkeypatch):
        """多轮 feed 里同一 room_id 只留一份；池按人气降序。"""
        batches = [
            [_room("a", "1001", "甲", 10), _room("b", "1002", "乙", 500)],
            # 1001 重复出现（应去重），1003 新
            [_room("a", "1001", "甲", 999), _room("c", "1003", "丙", 50)],
        ]
        calls = {"n": 0}

        def fake_fetch(tag, wait_ms=12000):
            b = batches[calls["n"]]
            calls["n"] += 1
            return b

        monkeypatch.setattr(sw, "fetch_feed_once", fake_fetch)
        pool = sw.build_pool(reloads=2)
        assert [r["id_str"] for r in pool] == ["1002", "1003", "1001"]  # 按人气降序
        assert len(pool) == 3  # 1001 去重

    def test_keeps_first_seen_when_duplicate(self, monkeypatch):
        batches = [
            [_room("a", "1001", "甲首见", 10)],
            [_room("a", "1001", "甲再见", 999)],
        ]
        calls = {"n": 0}

        def fake_fetch(tag, wait_ms=12000):
            b = batches[calls["n"]]
            calls["n"] += 1
            return b

        monkeypatch.setattr(sw, "fetch_feed_once", fake_fetch)
        pool = sw.build_pool(reloads=2)
        assert pool[0]["nick"] == "甲首见"  # 先见优先

    def test_empty_batches_ok(self, monkeypatch):
        monkeypatch.setattr(sw, "fetch_feed_once", lambda tag, wait_ms=12000: [])
        assert sw.build_pool(reloads=3) == []


class TestSweep:
    def _cfg(self):
        class _C:
            user_agent = "UA-test"
        return _C()

    def test_hit_recorded_and_jsonl_written(self, monkeypatch, tmp_path):
        rooms = [_room("a", "1001", "甲", 10), _room("b", "1002", "乙", 500)]
        box = {"box_id_str": "999", "box_type": 1, "diamond_count": 100}

        def fake_query(room_id, ua, timeout=8):
            if room_id == "1002":
                return {"data": [box], "extra": {"current_round": 2, "pct": 37}}
            return {"data": [], "extra": {"current_round": 0}}

        monkeypatch.setattr(sw, "query_luckybox", fake_query)
        monkeypatch.setattr(sw.time, "sleep", lambda *_: None)
        out = tmp_path / "sweep.jsonl"
        hits = sw.sweep(rooms, passes=1, interval=0, cfg=self._cfg(), out_path=out)

        assert len(hits) == 1
        assert hits[0]["web_rid"] == "b"
        assert hits[0]["box_count"] == 1
        assert hits[0]["current_round"] == 2
        lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines() if x.strip()]
        assert len(lines) == 2  # 未命中也要落盘
        assert {x["web_rid"] for x in lines} == {"a", "b"}

    def test_no_hit_returns_empty_but_writes_all(self, monkeypatch, tmp_path):
        rooms = [_room("a", "1001"), _room("b", "1002")]
        monkeypatch.setattr(sw, "query_luckybox",
                            lambda room_id, ua, timeout=8: {"data": [], "extra": {}})
        monkeypatch.setattr(sw.time, "sleep", lambda *_: None)
        out = tmp_path / "sweep.jsonl"
        hits = sw.sweep(rooms, passes=2, interval=0, cfg=self._cfg(), out_path=out)
        assert hits == []
        lines = out.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 4  # 2 房 × 2 轮

    def test_query_error_does_not_break_sweep(self, monkeypatch, tmp_path):
        rooms = [_room("a", "1001"), _room("b", "1002")]
        calls = {"n": 0}

        def flaky(room_id, ua, timeout=8):
            calls["n"] += 1
            if room_id == "1001":
                raise RuntimeError("网络抖动")
            return {"data": [], "extra": {}}

        monkeypatch.setattr(sw, "query_luckybox", flaky)
        monkeypatch.setattr(sw.time, "sleep", lambda *_: None)
        out = tmp_path / "sweep.jsonl"
        hits = sw.sweep(rooms, passes=1, interval=0, cfg=self._cfg(), out_path=out)
        assert hits == []
        # 出错的房不落盘，成功的落盘 → 只有 b 一行
        lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines() if x.strip()]
        assert len(lines) == 1 and lines[0]["web_rid"] == "b"
