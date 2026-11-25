# -*- coding: utf-8 -*-
"""看门狗重启退避不能只增不减（2026-11-01 审计 P0）。

原实现：

    wait = RESTART_BACKOFF[min(backoff_idx, len(RESTART_BACKOFF) - 1)]
    backoff_idx += 1          # ← 只加，永远没有归零的分支

`fast_exits` 有「活过 FAST_EXIT_SECONDS 就清零」的分支，`backoff_idx` 没有。
后果：一天里子进程因为外部原因退 5 次（浏览器被顶掉、机器休眠、手滑 kill），
之后每次重启都固定等 900 秒 —— 明明子进程随后健康跑了 8 小时，下一次崩溃照样
先躺 15 分钟。八个实例同时处于这状态时，一场直播能白丢一刻钟。

判据与 fast_exits 对齐：存活 ≥ FAST_EXIT_SECONDS 就算健康，退避回到第一档。
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import run_watchdog as rw  # noqa: E402


def test_healthy_run_resets_backoff_to_first_step():
    wait, nxt = rw.plan_backoff(4, alive_seconds=8 * 3600)
    assert wait == rw.RESTART_BACKOFF[0]
    assert nxt == 1                     # 下一次仍是第二档（60s 之后才轮到 120s）


def test_quick_exits_climb_then_cap():
    idx, waits = 0, []
    for _ in range(6):
        wait, idx = rw.plan_backoff(idx, alive_seconds=5.0)
        waits.append(wait)
    assert waits == [60, 120, 240, 480, 900, 900]


def test_healthy_threshold_is_fast_exit_seconds():
    """59 秒不算健康（还是秒退档），60 秒算。"""
    assert rw.plan_backoff(3, alive_seconds=rw.FAST_EXIT_SECONDS - 1)[0] \
        == rw.RESTART_BACKOFF[3]
    assert rw.plan_backoff(3, alive_seconds=float(rw.FAST_EXIT_SECONDS))[0] \
        == rw.RESTART_BACKOFF[0]


def test_reset_only_affects_next_round():
    """健康重置后紧接着又秒退 → 从第二档（120s）开始爬，不是从 900s。"""
    _, idx = rw.plan_backoff(4, alive_seconds=3600.0)
    wait, _ = rw.plan_backoff(idx, alive_seconds=3.0)
    assert wait == rw.RESTART_BACKOFF[1]
