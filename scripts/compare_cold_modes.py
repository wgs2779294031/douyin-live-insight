#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""冷场口径横向对比（2026-10-27）。

为什么要有这个脚本
------------------
冷场判定改成什么样，光看参数没意义，必须用**真实弹幕**跑一遍。三种口径：

- `adaptive-old` v2 论文口径，但下限硬编码 max(3)/max(6)
- `adaptive-new` 同一算法，下限改成可配 0.5/1.0（本次修复）
- `relative`     相对自身中位数（0.5 进 / 0.8 出，另行实验）

已知失灵案例（2026-10-26 研途-张老师团队）：平均 1.02 条/分钟、427 分钟里 224 分钟
一条弹幕都没有，而 old 的解除阈值 6 条/60 秒永远达不到 → 单段冷场挂了 121 分钟。

`--min-duration` 模拟生产侧的抖动过滤（`ColdEventLog.min_duration_seconds`）：
短于该秒数的冷场段不计入统计，否则低频场会出现几百段「1 秒冷场」。

用法：
    python scripts/compare_cold_modes.py --date 2026-10-26
    python scripts/compare_cold_modes.py --date 2026-10-26 --min-duration 0
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import analyzer as az  # noqa: E402

MODES: List[Tuple[str, Dict[str, Any]]] = [
    ("adaptive-旧(3/6)", dict(cold_mode="adaptive",
                            cold_floor_enter=3.0, cold_floor_exit=6.0)),
    ("adaptive-新(.5/1)", dict(cold_mode="adaptive",
                             cold_floor_enter=0.5, cold_floor_exit=1.0)),
    ("relative(.5/.8)", dict(cold_mode="relative")),
]


def _danmu_files(data_dir: Path, date: str,
                 tags: Optional[List[str]] = None) -> List[Tuple[str, Path]]:
    out = []
    for p in sorted(glob.glob(str(data_dir / ("danmu_douyin_*_%s.jsonl" % date)))):
        m = re.match(r"^danmu_douyin_(.+)_(\d{4}-\d{2}-\d{2})\.jsonl$", os.path.basename(p))
        if not m:
            continue
        tag = m.group(1)
        if tags and tag not in tags:
            continue
        out.append((tag, Path(p)))
    return out


def _run_mode(path: Path, cfg_kwargs: Dict[str, Any], baseline_minutes: int,
              min_duration: int) -> Dict[str, Any]:
    """用生产同一套 replay_jsonl 跑一遍；时长自己配对（analyzer 的 cold_end 不带 duration）。"""
    cfg = az.AnalyzerConfig(adaptive_baseline_minutes=baseline_minutes, **cfg_kwargs)
    events: List[Dict[str, Any]] = []
    az.replay_jsonl(str(path), cfg, on_event=events.append)

    starts = [e for e in events if e.get("event") == "cold_start"]
    raw: List[int] = []
    cur: Optional[float] = None
    for e in sorted(events, key=lambda x: float(x.get("ts") or 0)):
        if e.get("event") == "cold_start":
            cur = float(e.get("ts") or 0)
        elif e.get("event") == "cold_end" and cur is not None:
            raw.append(max(0, int(float(e.get("ts") or 0) - cur)))
            cur = None
    kept = sorted(d for d in raw if d >= min_duration)
    return {
        "segments_raw": len(starts),
        "segments": len(kept),
        "filtered": len(raw) - len(kept),
        "total_seconds": sum(kept),
        "max_seconds": kept[-1] if kept else 0,
        "median_seconds": kept[len(kept) // 2] if kept else 0,
        "thresholds": sorted({round(float(e.get("threshold") or 0), 1) for e in starts})[:4],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="冷场口径横向对比")
    ap.add_argument("--date", required=True)
    ap.add_argument("--tags", nargs="*", default=None)
    ap.add_argument("--baseline-minutes", type=int, default=15,
                    help="基线窗口（分钟），默认 15")
    ap.add_argument("--min-duration", type=int, default=30,
                    help="短于该秒数的冷场段不计（抖动过滤），0=不过滤")
    ap.add_argument("--data-dir", default=str(REPO_ROOT / "data"))
    args = ap.parse_args()

    files = _danmu_files(Path(args.data_dir), args.date, args.tags)
    if not files:
        print("没有匹配的 danmu 文件", file=sys.stderr)
        return 2

    print("日期 %s | 基线 %d 分钟 | 抖动过滤 ≥%d 秒\n" % (
        args.date, args.baseline_minutes, args.min_duration))
    head = "%-22s %-18s %6s %6s %10s %9s %8s" % (
        "主播", "口径", "段数", "被滤", "总时长", "最长段", "中位段")
    print(head)
    print("-" * len(head))
    for tag, path in files:
        res = {}
        for label, kw in MODES:
            r = _run_mode(path, kw, args.baseline_minutes, args.min_duration)
            res[label] = r
            print("%-22s %-18s %6d %6d %9d秒 %8d秒 %7d秒" % (
                tag, label, r["segments"], r["filtered"],
                r["total_seconds"], r["max_seconds"], r["median_seconds"]))
        old, new = res[MODES[0][0]], res[MODES[1][0]]
        print("%-22s → 最长段 %d→%d 秒，总时长 %d→%d 秒，阈值样本 %s→%s" % (
            "", old["max_seconds"], new["max_seconds"],
            old["total_seconds"], new["total_seconds"],
            old["thresholds"][:2], new["thresholds"][:2]))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
