# -*- coding: utf-8 -*-
"""冷场开始不能在内存里等 cold_end（2026-11-01 审计 P0）。

原实现（room_runtime.ColdEventLog）：

    if name == "cold_start":
        self._pending = rec        # ← 只存内存，一个字节都没落盘
        return
    if name == "cold_end":
        ... 算 duration，不达标就 return，达标才把 pending 一起写

于是「冷场开始」这件事的可观测性完全依赖于进程能活到 cold_end。实际失效形态：

- 进程被硬杀（10-29 事故：内存耗尽 Id=2004，系统直接收走进程）→ 正在冷的那一段
  整段消失，复盘页看到的是「这段一直有人说话」；
- 主播一直冷到收播 → analyzer 不再产 cold_end → 收播那一段永远不记；
- 冷场跨过午夜 → 落盘日期还归在 cold_end 那天。

修法两条：
1. 达到 min_duration 后**立刻**持久化 cold_start（不等 cold_end）；
2. close() 时把还没闭合的那段补写出来，标 ``incomplete: true``（没有 cold_end，
   时长未知）—— 有记录、能计数，不会伪装成「没冷过场」。
"""
import json
import os
import pathlib
import sys
import threading
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from room_runtime import ColdEventLog  # noqa: E402


def _rows(tmp_path):
    p = tmp_path / ("cold_events_%s.jsonl" % time.strftime("%Y-%m-%d"))
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()
            if x.strip()]


def test_zero_min_duration_persists_start_immediately(tmp_path):
    """min_duration=0（阈值关掉）→ cold_start 当场落盘，不等 cold_end。"""
    log = ColdEventLog(str(tmp_path), "X", "1", min_duration=0)
    log({"event": "cold_start", "ts": 100, "count": 0})

    rows = _rows(tmp_path)
    assert [r["event"] for r in rows] == ["cold_start"]
    assert "incomplete" not in rows[0]

    log({"event": "cold_end", "ts": 160})
    log.close()
    rows = _rows(tmp_path)
    assert [r["event"] for r in rows] == ["cold_start", "cold_end"]
    assert rows[1]["duration"] == 60


def test_start_persisted_once_min_duration_reached(tmp_path):
    """min_duration=1 秒 → 不到阈值不写，到了就自己写出来（不必等 cold_end）。"""
    log = ColdEventLog(str(tmp_path), "X", "1", min_duration=1)
    log({"event": "cold_start", "ts": 1000, "count": 0})
    assert _rows(tmp_path) == []          # 刚开始，还算不上「一段冷场」

    deadline = time.time() + 3.0
    while time.time() < deadline and not _rows(tmp_path):
        time.sleep(0.05)

    rows = _rows(tmp_path)
    assert [r["event"] for r in rows] == ["cold_start"]
    assert rows[0]["ts"] == 1000
    assert "incomplete" not in rows[0]
    log.close()


def test_close_writes_unclosed_start_as_incomplete(tmp_path):
    """没等到 cold_end 就收工 → 补写一条并标 incomplete（不再是整段消失）。"""
    log = ColdEventLog(str(tmp_path), "X", "1", min_duration=30)
    log({"event": "cold_start", "ts": 1000, "count": 2, "threshold": 3.0})
    assert _rows(tmp_path) == []
    log.close()

    rows = _rows(tmp_path)
    assert [r["event"] for r in rows] == ["cold_start"]
    assert rows[0]["incomplete"] is True
    assert rows[0]["ts"] == 1000
    assert rows[0]["count"] == 2
    assert "duration" not in rows[0]      # 没有结束时刻，时长确实未知


def test_close_after_closed_segment_adds_nothing(tmp_path):
    """正常闭合后再 close → 不能多补一条（否则冷场次数翻倍）。"""
    log = ColdEventLog(str(tmp_path), "X", "1", min_duration=30)
    log({"event": "cold_start", "ts": 1000})
    log({"event": "cold_end", "ts": 1100})
    log.close()

    rows = _rows(tmp_path)
    assert [r["event"] for r in rows] == ["cold_start", "cold_end"]
    assert "incomplete" not in rows[0]
    assert rows[1]["duration"] == 100


def test_close_is_idempotent(tmp_path):
    """RoomRuntime.stop() 与测试/异常路径都可能重复 close。"""
    log = ColdEventLog(str(tmp_path), "X", "1", min_duration=30)
    log({"event": "cold_start", "ts": 1000})
    log.close()
    log.close()

    assert len(_rows(tmp_path)) == 1


def test_orphan_end_still_dropped(tmp_path):
    """进程中途启动只见到 cold_end → 仍然丢弃（原行为不变）。"""
    log = ColdEventLog(str(tmp_path), "X", "1")
    log({"event": "cold_end", "ts": 2000})
    log.close()
    assert _rows(tmp_path) == []


def test_short_segment_still_filtered(tmp_path):
    """抖动段（< min_duration）照旧整段不写 —— 不能被「提前落盘」变成脏数据。"""
    log = ColdEventLog(str(tmp_path), "X", "1", min_duration=30)
    log({"event": "cold_start", "ts": 1000})
    log({"event": "cold_end", "ts": 1005})
    log.close()
    assert _rows(tmp_path) == []


@pytest.mark.parametrize("end_ts, duration", [(1029, 29), (999, 0)])
def test_persisted_start_is_closed_when_event_clock_is_short(
        tmp_path, monkeypatch, end_ts, duration):
    """Timer 已落开始，事件时间略短于阈值时仍要写结束，避免孤儿冷场。"""
    callbacks = []

    class ManualTimer:
        def __init__(self, seconds, callback):
            callbacks.append(callback)

        def start(self):
            pass

        def cancel(self):
            pass

    monkeypatch.setattr("room_runtime.threading.Timer", ManualTimer)
    log = ColdEventLog(str(tmp_path), "X", "1", min_duration=30)
    log({"event": "cold_start", "ts": 1000})
    callbacks[0]()  # wall-clock timer fires before delayed event-time end arrives
    assert [r["event"] for r in _rows(tmp_path)] == ["cold_start"]

    log({"event": "cold_end", "ts": end_ts})
    log.close()
    rows = _rows(tmp_path)
    assert [r["event"] for r in rows] == ["cold_start", "cold_end"]
    assert rows[1]["duration"] == duration


def test_consecutive_starts_persist_each_once(tmp_path):
    """上一段没闭合又来一段 → 两段各落一条，不重复。"""
    log = ColdEventLog(str(tmp_path), "X", "1", min_duration=0)
    log({"event": "cold_start", "ts": 100})
    log({"event": "cold_start", "ts": 200})
    log.close()

    assert [r["ts"] for r in _rows(tmp_path)] == [100, 200]


def test_consecutive_starts_keep_only_latest_pending(tmp_path):
    """阈值 >0 时后一段覆盖前一段的暂存（原行为），close 补写的是最后那段。

    同时验证旧定时器被收掉：回填一天几千段时不能堆出几千个待触发线程。
    """
    log = ColdEventLog(str(tmp_path), "X", "1", min_duration=30)
    log({"event": "cold_start", "ts": 100})
    log({"event": "cold_start", "ts": 200})
    log.close()

    rows = _rows(tmp_path)
    assert [r["ts"] for r in rows] == [200]
    assert rows[0]["incomplete"] is True


# ---------------------------------------------------------------- 并发写串行化
class _SlowWriter:
    """记录"同时有几个写者进来"的假 writer —— 真 writer 没有锁，用它暴露串行性。"""

    def __init__(self):
        self._g = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.count = 0

    def write(self, rec):
        with self._g:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.002)          # 故意留窗口：没锁的话别的线程会挤进来
        with self._g:
            self.active -= 1
            self.count += 1

    def close(self):
        pass


def _make_log(tmp_path):
    return ColdEventLog(str(tmp_path), "T", "7700000000000000000")


def test_concurrent_write_is_serialised(tmp_path):
    """Timer 线程（提前落盘）与 analyzer 线程（cold_end）会并发调 _write，
    而 DailyJsonlWriter.write 内部没有锁 —— 必须在 ColdEventLog 这层串行化，
    否则同一句柄上两个线程各写半行，就是审计 P0-7 的线程版本。"""
    log = _make_log(tmp_path)
    slow = _SlowWriter()
    log._writer = slow

    def worker(n):
        for _ in range(20):
            log._write({"ts": n, "event": "cold_start", "room_tag": "T"})

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert slow.count == 120, "有写入丢失（线程异常吞掉了）"
    assert slow.max_active == 1, "同一时刻有 %d 个线程在写同一句柄" % slow.max_active


def test_flush_pending_inside_lock_does_not_deadlock(tmp_path):
    """_flush_pending 已持锁，内部再调 _write 拿同一把 RLock —— 必须可重入。"""
    log = _make_log(tmp_path)
    log({"event": "cold_start", "ts": 1000, "threshold": 1.0})
    log._flush_pending()
    # 没有卡死就是通过；顺带确认只写了一行
    path = tmp_path / ("cold_events_%s.jsonl" % time.strftime("%Y-%m-%d"))
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_close_holds_lock_while_closing_file(tmp_path):
    """close() 关句柄也要在锁内：否则 Timer 刚拿到锁在写、这边把文件关了。"""
    log = _make_log(tmp_path)
    slow = _SlowWriter()
    log._writer = slow
    done = []

    def bg():
        log._write({"event": "x"})
        done.append(True)

    t = threading.Thread(target=bg, daemon=True)
    t.start()
    log.close()
    t.join(timeout=10)
    assert done == [True]
