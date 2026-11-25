# -*- coding: utf-8 -*-
"""冷场阈值口径单测（2026-10-27）。

背景：`adaptive` 模式的 `mean - 1.5σ` 本来是合理统计口径，但结果被
`max(3.0, ...)` / `max(6.0, ...)` 两个硬编码下限兜住，对低频直播间等于退回固定阈值。
2026-10-26 实测「研途-张老师团队」：436 条弹幕 / 427 分钟 / 224 分钟零弹幕，
平均 1.02 条/分钟 —— 解除阈值 6 条/60 秒永远达不到，单段冷场挂了 121 分钟
（真值 77.8 分钟：16:23 起连续 77.8 分钟一条弹幕都没有）。

这里锁住三件事：
1. 下限可配，且能低到匹配低频场；
2. relative 模式按自身中位数判，不受绝对量级影响；
3. 两种模式在样本不足时都回退到配置里的固定阈值（冷启动阶段有判定）。
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import analyzer as az  # noqa: E402


class _Stub:
    """只带 thresholds() 需要的最小字段，省得建整个 DanmuAnalyzer。"""

    def __init__(self, cfg: az.AnalyzerConfig, history):
        self.cfg = cfg
        self._rate_history = list(history)


def test_adaptive_floor_is_configurable():
    """旧默认 3.0/6.0 → 低频场永远解除不了；新默认 0.5/1.0 才能匹配 1 条/分钟。"""
    cfg_old = az.AnalyzerConfig(cold_mode="adaptive",
                                cold_floor_enter=3.0, cold_floor_exit=6.0)
    cfg_new = az.AnalyzerConfig(cold_mode="adaptive")
    # 均值 1 条/60s、方差极小 → mean-1.5σ 接近 1，被下限兜住
    hist = [1, 2, 1, 1, 2, 1, 1, 1]

    old = az.AdaptiveBaselineDetector(3, 6, 3, cfg_old.cold_floor_enter,
                                      cfg_old.cold_floor_exit)
    new = az.AdaptiveBaselineDetector(3, 6, 3, cfg_new.cold_floor_enter,
                                      cfg_new.cold_floor_exit)

    assert old.thresholds(_Stub(cfg_old, hist)) == (3.0, 6.0)   # 老行为
    e_new, x_new = new.thresholds(_Stub(cfg_new, hist))
    assert e_new < 3.0 and x_new < 6.0
    assert x_new > e_new


def test_adaptive_ignores_floor_when_volatile():
    """高频场：σ 大时阈值仍由 mean-1.5σ 决定，不该被下限抬起来。"""
    cfg = az.AnalyzerConfig(cold_mode="adaptive")
    hist = [10, 40, 20, 55, 15, 30, 25, 35]
    det = az.AdaptiveBaselineDetector(3, 6, 3, cfg.cold_floor_enter, cfg.cold_floor_exit)
    e, x = det.thresholds(_Stub(cfg, hist))
    assert e > cfg.cold_floor_enter
    assert x > e


def test_relative_uses_median_not_absolute():
    """relative 模式：同样「掉一半」，低频场和高频场应各自按自己的量级判。"""
    cfg = az.AnalyzerConfig(cold_mode="relative")
    det = az.RelativeBaselineDetector(cfg.cold_relative_enter, cfg.cold_relative_exit,
                                      cfg.adaptive_min_samples, cfg.cold_absolute_floor)

    low = _Stub(cfg, [1, 0, 2, 1, 1, 0, 1])          # 中位数 1
    high = _Stub(cfg, [20, 30, 25, 22, 28, 26, 24])  # 中位数 25

    e_low, x_low = det.thresholds(low)
    e_high, x_high = det.thresholds(high)
    # 低频场被绝对下限托到 1.0（0 条才算冷），高频场按比例
    assert e_low == pytest.approx(cfg.cold_absolute_floor)
    assert e_high > 10
    assert x_high > e_high


def test_relative_floor_prevents_never_cold():
    """整场没人说话（history 全 0）时不能算出 enter=0 —— 否则 0 条永远不 < 0。"""
    cfg = az.AnalyzerConfig(cold_mode="relative")
    det = az.RelativeBaselineDetector(cfg.cold_relative_enter, cfg.cold_relative_exit,
                                      cfg.adaptive_min_samples, cfg.cold_absolute_floor)
    stub = _Stub(cfg, [0, 0, 0, 0, 0])
    e, x = det.thresholds(stub)
    assert e > 0
    assert x > e


def test_cold_start_falls_back_to_fixed_thresholds():
    """样本不足时两种模式都回退到配置里的固定阈值（开播头几分钟要有判定）。"""
    cfg = az.AnalyzerConfig(cold_mode="adaptive", adaptive_min_samples=5)
    stub = _Stub(cfg, [4, 5])          # 只有 2 个样本 < 5
    adaptive = az.AdaptiveBaselineDetector(
        cfg.cold_enter_threshold, cfg.cold_exit_threshold, cfg.adaptive_min_samples,
        cfg.cold_floor_enter, cfg.cold_floor_exit)
    relative = az.RelativeBaselineDetector(
        cfg.cold_relative_enter, cfg.cold_relative_exit,
        cfg.adaptive_min_samples, cfg.cold_absolute_floor)
    assert adaptive.thresholds(stub) == (3.0, 6.0)
    assert relative.thresholds(stub) == (3.0, 6.0)


def test_invalid_relative_ratio_rejected():
    with pytest.raises(az.AnalyzerConfigError):
        az.AnalyzerConfig(cold_relative_enter=0.9, cold_relative_exit=0.5)


def test_unknown_cold_mode_rejected():
    with pytest.raises(az.AnalyzerConfigError):
        az.AnalyzerConfig(cold_mode="magic")
