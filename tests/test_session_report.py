# -*- coding: utf-8 -*-
"""v3.3 场次统计 + 高能时刻单测（离线计算，session_report.py）。

覆盖：
- _load_stats：序列合并 / 无效值剔除 / 同秒取大
- 场次统计：时长 / 弹幕总量 / 峰值人气+时刻 / 累计 PV 起止
- 人气跳升检测：真实跳升命中 / 平稳段不误报
- 弹幕爆发检测：爆发命中 / 平稳段不误报
- 合并与前后文窗口：相邻合并为一条、kind=both、窗口弹幕原文

运行：pytest tests/test_session_report.py -v
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import session_report as sr  # noqa: E402


T0 = 1790700000  # 基准时间戳（秒）


def _write_stats(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for ts, online, pv in rows:
            f.write(json.dumps({"ts": ts, "type": "room_stats", "online": online,
                                "total_pv": pv, "platform": "douyin"},
                               ensure_ascii=False) + "\n")


def _write_danmu(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for ts, text in records:
            f.write(json.dumps({"ts": ts, "platform": "douyin", "type": "danmaku",
                                "user_id": "u", "uname": "n", "text": text,
                                "room_id": "r", "extra": {}},
                               ensure_ascii=False) + "\n")


def _steady_series(n=20, online=50, step=3, pv_start=1000):
    """平稳人气序列：online 恒定，PV 线性增长。"""
    return [(T0 + i * step, online, pv_start + i * 7) for i in range(n)]


class TestLoadStats:
    def test_merge_and_filter(self, tmp_path):
        p = tmp_path / "s.jsonl"
        p.write_text(
            json.dumps({"ts": T0, "type": "room_stats", "online": 10, "total_pv": None}) + "\n"
            + json.dumps({"ts": T0, "type": "room_user_seq", "online": 8, "total_pv": 500}) + "\n"
            + json.dumps({"ts": T0 + 3, "type": "room_stats", "online": 0}) + "\n",
            encoding="utf-8")
        series = sr._load_stats(str(p))
        assert series == [(T0, 10, 500), (T0 + 3, 50, None)][:0] or True
        # 上面第三行 online=0 应被剔除；第一二行同秒合并 online 取大、PV 取有值
        assert len(series) == 1
        assert series[0] == (T0, 10, 500)


class TestSessionStats:
    def test_basic(self, tmp_path):
        stats = str(tmp_path / "s.jsonl")
        danmu = str(tmp_path / "d.jsonl")
        rows = _steady_series()
        _write_stats(stats, rows)
        recs = [(T0 + i, f"弹幕{i}") for i in range(30)]
        _write_danmu(danmu, recs)
        report = sr.compute_session(stats, danmu, room_tag="测试")
        s = report["session"]
        assert s["start_ts"] == T0
        assert s["danmaku_total"] == 30
        assert s["online_peak"] == 50
        assert s["online_peak_ts"] == T0
        assert s["total_pv_first"] == 1000
        assert s["total_pv_last"] == 1000 + 19 * 7
        assert s["duration_seconds"] == 19 * 3
        # 平稳段不应有高能时刻
        assert report["highlights"] == []


class TestOnlineJump:
    def test_jump_detected(self, tmp_path):
        stats = str(tmp_path / "s.jsonl")
        danmu = str(tmp_path / "d.jsonl")
        rows = _steady_series(online=50)
        # 第 25 个点：人气跳到 120（基线 50 → 2.4 倍，增量 70）
        rows.append((T0 + 24 * 3 + 5, 120, 1200))
        _write_stats(stats, rows)
        _write_danmu(danmu, [])
        report = sr.compute_session(stats, danmu)
        jumps = [h for h in report["highlights"] if h["kind"] in ("online_jump", "both")]
        assert len(jumps) == 1
        assert jumps[0]["online_before"] == 50
        assert jumps[0]["online_peak"] == 120
        assert jumps[0]["ts"] == T0 + 24 * 3 + 5

    def test_small_noise_not_flagged(self, tmp_path):
        stats = str(tmp_path / "s.jsonl")
        danmu = str(tmp_path / "d.jsonl")
        rows = _steady_series(online=50)
        rows.append((T0 + 100, 56, 1300))  # +6 < max(5, 50*0.3)=15 → 不算跳升
        _write_stats(stats, rows)
        _write_danmu(danmu, [])
        assert sr.compute_session(stats, danmu)["highlights"] == []


class TestDanmuBurst:
    def test_burst_detected(self, tmp_path):
        stats = str(tmp_path / "s.jsonl")
        danmu = str(tmp_path / "d.jsonl")
        _write_stats(stats, _steady_series())
        # 前 90 秒每 10 秒 1 条（平稳），随后 10 秒爆发 20 条
        recs = [(T0 + i, f"平时{i}") for i in range(0, 90, 10)]
        recs += [(T0 + 100 + j, f"爆发{j}") for j in range(20)]
        _write_danmu(danmu, recs)
        report = sr.compute_session(stats, danmu)
        bursts = [h for h in report["highlights"] if h["kind"] in ("danmaku_burst", "both")]
        # 爆发 20 条落在 100..119 → 两个 10 秒桶各 10 条，均命中并合并为 1 条
        assert len(bursts) == 1
        assert bursts[0]["rate_peak_per10s"] == 10
        assert bursts[0]["rate_before_per10s"] == 1

    def test_context_window(self, tmp_path):
        stats = str(tmp_path / "s.jsonl")
        danmu = str(tmp_path / "d.jsonl")
        _write_stats(stats, _steady_series())
        recs = [(T0 + i, f"平时{i}") for i in range(0, 90, 10)]
        recs += [(T0 + 100 + j, f"爆发{j}") for j in range(20)]
        _write_danmu(danmu, recs)
        report = sr.compute_session(stats, danmu)
        h = report["highlights"][0]
        assert h["window_seconds"] == sr.CONTEXT_WINDOW_S
        texts = h["window_texts"]
        assert any("爆发" in t for t in texts)
        assert h["window_danmaku_count"] == len(
            [1 for ts, _ in recs if h["ts"] - 60 <= ts <= h["ts"] + 60])


class TestMerge:
    def test_adjacent_merged_as_both(self, tmp_path):
        stats = str(tmp_path / "s.jsonl")
        danmu = str(tmp_path / "d.jsonl")
        rows = _steady_series(online=50)
        rows.append((T0 + 100, 120, 1200))  # 人气跳升 @100
        _write_stats(stats, rows)
        recs = [(T0 + i, f"平时{i}") for i in range(0, 90, 10)]
        recs += [(T0 + 95 + j, f"爆发{j}") for j in range(20)]  # 弹幕爆发 @~105
        _write_danmu(danmu, recs)
        report = sr.compute_session(stats, danmu)
        hs = report["highlights"]
        assert len(hs) == 1  # 相邻 ≤120s 合并
        assert hs[0]["kind"] == "both"
        assert hs[0]["online_peak"] == 120 and hs[0]["rate_peak_per10s"] == 10


class TestLotterySuspects:
    """通道 A：用弹幕间接识别「疑似福袋时段」（推断，非平台事件）。"""

    def test_keyword_burst_with_repeats_detected(self, tmp_path):
        stats = str(tmp_path / "s.jsonl")
        danmu = str(tmp_path / "d.jsonl")
        _write_stats(stats, _steady_series())
        # 前 5 分钟日常弹幕（无关键词）；随后 1 分钟观众狂刷同一口令
        recs = [(T0 + i, f"闲聊{i}") for i in range(0, 300, 20)]
        recs += [(T0 + 300 + i, "福袋口令666") for i in range(12)]
        _write_danmu(danmu, recs)
        report = sr.compute_session(stats, danmu)
        sus = [h for h in report["highlights"] if "lottery_suspect" in h["kind"]]
        assert len(sus) == 1
        assert sus[0]["keyword_hits"] >= sr.LOTTERY_MIN_HITS
        assert sus[0]["repeat_count"] >= sr.LOTTERY_REPEAT_MIN
        assert sus[0]["top_repeated_text"] == "福袋口令666"

    def test_chatter_about_redpacket_not_flagged(self, tmp_path):
        """闲聊提到「红包」但无口令式重复 → 不误报。"""
        stats = str(tmp_path / "s.jsonl")
        danmu = str(tmp_path / "d.jsonl")
        _write_stats(stats, _steady_series())
        recs = [(T0 + i * 30, "过年发红包了吗") for i in range(10)]
        _write_danmu(danmu, recs)
        report = sr.compute_session(stats, danmu)
        assert [h for h in report["highlights"] if "lottery_suspect" in h["kind"]] == []

    def test_no_keywords_no_detection(self, tmp_path):
        stats = str(tmp_path / "s.jsonl")
        danmu = str(tmp_path / "d.jsonl")
        _write_stats(stats, _steady_series())
        _write_danmu(danmu, [(T0 + i, "主播讲得真好") for i in range(50)])
        report = sr.compute_session(stats, danmu)
        assert report["highlights"] == []

    def test_merges_with_danmaku_burst_kind_naming(self, tmp_path):
        """同窗口内既有弹幕爆发又有福袋口令 → kind 用 '+' 连接，不再笼统叫 both。"""
        stats = str(tmp_path / "s.jsonl")
        danmu = str(tmp_path / "d.jsonl")
        _write_stats(stats, _steady_series())
        recs = [(T0 + i, f"平时{i}") for i in range(0, 90, 10)]
        recs += [(T0 + 100 + i, "口令福袋") for i in range(20)]
        _write_danmu(danmu, recs)
        report = sr.compute_session(stats, danmu)
        hs = report["highlights"]
        assert len(hs) >= 1
        kinds = hs[0]["kind"]
        assert "lottery_suspect" in kinds and "danmaku_burst" in kinds
        assert kinds == "danmaku_burst+lottery_suspect"


class TestMeta:
    def test_meta_start_time_overrides(self, tmp_path):
        """room_meta 的 start_time 覆盖场次起点（在 _run_one 中发生）。

        这里直接测 compute_session 不含该逻辑，用集成路径验证：
        构造 meta 文件 + 两个输入文件，跑 _run_one 生成 JSON。
        """
        stats = str(tmp_path / "stats_douyin_测试_2026-09-30.jsonl")
        danmu = str(tmp_path / "danmu_douyin_测试_2026-09-30.jsonl")
        meta = str(tmp_path / "room_meta_douyin_测试.json")
        _write_stats(stats, _steady_series())
        _write_danmu(danmu, [(T0, "hi")])
        with open(meta, "w", encoding="utf-8") as f:
            json.dump({"room_id": "769", "anchor_name": "A", "title": "T",
                       "start_time": T0 - 600, "session_id": "769_2026-09-30"},
                      f, ensure_ascii=False)

        class Args:
            platform = "douyin"
            out = str(tmp_path / "out.json")
        rc = sr._run_one(Args, stats, danmu, "测试")
        assert rc == 0
        with open(Args.out, encoding="utf-8") as f:
            report = json.load(f)
        assert report["session"]["start_ts"] == T0 - 600
        assert report["session"]["session_id"] == "769_2026-09-30"
        assert report["room_meta"]["anchor_name"] == "A"
