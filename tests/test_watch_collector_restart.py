# -*- coding: utf-8 -*-
"""采集进程自愈的判定测试（plan_collector 纯函数，零网络零子进程）。

背景（2026-10-29）：watch_anchor_live 以前用一次性 `started` 标志，main.py 被外部
杀掉后再也不会重起。可观测表现极隐蔽 —— 探测每 60 秒都成功、日志每轮都打
「★开播」、看门狗只看本进程活没活，于是「进程活着 + 直播中 + 零数据」能静默挂着，
直到有人发现实时页的红条。
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import watch_anchor_live as wal  # noqa: E402


NOW = 1_000_000.0


def _plan(**kw):
    base = dict(started=True, rc=None, alive_seconds=600.0, restarts=0,
                fast_exits=0, paused_until=0.0, now=NOW)
    base.update(kw)
    return wal.plan_collector(**base)


def test_never_started_is_start():
    """第一次开播 → start（不是 restart，别发「采集中断」告警）。"""
    action, restarts, fast, paused = _plan(started=False, rc=None, alive_seconds=0.0)
    assert action == "start"
    assert restarts == 0 and fast == 0 and paused == 0.0


def test_alive_does_nothing():
    action, restarts, _, _ = _plan(rc=None, alive_seconds=30.0)
    assert action == "alive"
    assert restarts == 0


def test_alive_long_enough_clears_fast_exits():
    """活着且跑够久 → 把陈年的「刚起就退」计数清掉，否则一次坏运气会一直压着。"""
    action, _, fast, _ = _plan(rc=None, alive_seconds=900.0, fast_exits=3)
    assert action == "alive"
    assert fast == 0


def test_alive_not_long_enough_keeps_counter():
    _, _, fast, _ = _plan(rc=None, alive_seconds=60.0, fast_exits=3)
    assert fast == 3


def test_exited_after_healthy_run_restarts_and_clears_counter():
    """跑了 20 分钟才死（外部杀） → 重启，且把「刚起就退」计数清零。"""
    action, restarts, fast, paused = _plan(rc=1, alive_seconds=1200.0, fast_exits=2)
    assert action == "restart"
    assert restarts == 1 and fast == 0 and paused == 0.0


def test_fast_exit_accumulates():
    _, restarts, fast, paused = _plan(rc=1, alive_seconds=30.0, fast_exits=1)
    assert (restarts, fast, paused) == (1, 2, 0.0)


def test_fuse_after_consecutive_fast_exits():
    """连续 4 次刚起就退 → 熔断，冷却 900 秒，计数归零（不是永久放弃）。"""
    action, restarts, fast, paused = _plan(rc=1, alive_seconds=5.0, fast_exits=3)
    assert action == "fuse"
    assert restarts == 1
    assert fast == 0
    assert paused == NOW + wal.COLLECTOR_PAUSE_SECONDS


def test_paused_does_not_restart():
    action, restarts, _, paused = _plan(rc=1, alive_seconds=5.0,
                                       paused_until=NOW + 100.0)
    assert action == "paused"
    assert restarts == 0
    assert paused == NOW + 100.0


def test_pause_expires_and_retries():
    """冷却到点后自动再试 —— 不永久放弃（配置修好/环境恢复就能自己回来）。"""
    action, _, _, _ = _plan(rc=1, alive_seconds=1200.0, paused_until=NOW - 1.0)
    assert action == "restart"


def test_fuse_threshold_is_reachable_from_zero():
    """从零开始数，恰好第 FUSE 次刚起就退才熔断。"""
    fast = 0
    actions = []
    for _ in range(wal.COLLECTOR_FUSE):
        action, _, fast, _ = _plan(rc=1, alive_seconds=1.0, fast_exits=fast)
        actions.append(action)
    assert actions[:-1] == ["restart"] * (wal.COLLECTOR_FUSE - 1)
    assert actions[-1] == "fuse"
