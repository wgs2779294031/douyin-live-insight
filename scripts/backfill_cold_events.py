#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""回填历史冷场事件（2026-10-26）。

为什么需要
----------
`cold_events_<date>.jsonl` 是 2026-10-26 才开始写的。在那之前「冷场次数」只有
LLM 层那份 `coldline_<date>.jsonl` —— 没开 LLM 的场次恒为 0（冤种小徐 10-25
就被误读成「全程没冷过场」，实际日志里冷过三次）。

这里用落盘的弹幕把 analyzer 重放一遍，按**原日期**补出规则层的 cold_start /
cold_end。与实时版是同一套判定（同一 AnalyzerConfig、同一 DanmuAnalyzer），
所以补出来的数字等同于「当时规则层一直在跑」。零 API 成本。

用法：
  python scripts/backfill_cold_events.py --tag 就业-冤种小徐 --date 2026-10-25
  python scripts/backfill_cold_events.py --all-dates
  python scripts/backfill_cold_events.py --all-tags --all-dates --force
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import analyzer as az  # noqa: E402
from room_runtime import ColdEventLog  # noqa: E402

RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _today_str() -> str:
    import time as _t
    return _t.strftime("%Y-%m-%d")


def _first_room_id(path: Path) -> str:
    """从弹幕文件里取平台房间号（第一行有 room_id 的）。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for _, line in zip(range(200), f):
                line = line.strip()
                if not line:
                    continue
                try:
                    rid = json.loads(line).get("room_id")
                except ValueError:
                    continue
                if rid:
                    return str(rid)
    except OSError:
        pass
    return ""


def _already_has(out: Path, tag: str) -> bool:
    """该文件里已经有这个 tag 的行？（json.dumps 的分隔符固定，直接找子串）"""
    if not out.exists():
        return False
    try:
        txt = out.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return ('"room_tag": "%s"' % tag) in txt


def backfill_one(data_dir: Path, tag: str, date: str,
                 force: bool = False) -> Dict[str, Any]:
    out = data_dir / ("cold_events_%s.jsonl" % date)
    danmu = data_dir / ("danmu_douyin_%s_%s.jsonl" % (tag, date))
    if not danmu.exists():
        return {"status": "no_danmu", "tag": tag, "date": date, "sessions": 0}
    if not force and _already_has(out, tag):
        return {"status": "skipped", "tag": tag, "date": date, "sessions": 0}

    room_id = _first_room_id(danmu)
    # ★ 用 config.json 里的规则层配置，别用 dataclass 默认值：
    #   阈值口径（cold_floor_enter/exit、基线窗口、抖动过滤）都在配置里，
    #   回填必须与生产一致，否则复盘页的新旧数字对不上。
    cfg = az.load_analyzer_config()
    log = ColdEventLog(str(data_dir), tag, room_id, date=date,
                       min_duration=getattr(cfg, "cold_min_duration_seconds", 0))
    try:
        az.replay_jsonl(str(danmu), cfg, on_event=log)
    finally:
        log.close()

    # 数一下写进去多少段（cold_start 行数）
    sessions = 0
    if out.exists():
        with open(out, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("room_tag") == tag and rec.get("event") == "cold_start":
                    sessions += 1
    return {"status": "done", "tag": tag, "date": date,
            "sessions": sessions, "room_id": room_id}


def _all_tags(data_dir: Path) -> List[str]:
    tags = set()
    for p in glob.glob(os.path.join(str(data_dir), "danmu_douyin_*.jsonl")):
        name = os.path.basename(p)[len("danmu_douyin_"):-len(".jsonl")]
        m = re.match(r"^(.+)_(\d{4}-\d{2}-\d{2})$", name)
        if m:
            tags.add(m.group(1))
    return sorted(tags)


def _all_dates(data_dir: Path, tag: Optional[str] = None) -> List[str]:
    pat = "danmu_douyin_%s_*.jsonl" % (tag if tag else "*")
    dates = set()
    for p in glob.glob(os.path.join(str(data_dir), pat)):
        m = re.search(r"_(\d{4}-\d{2}-\d{2})\.jsonl$", os.path.basename(p))
        if m:
            dates.add(m.group(1))
    return sorted(dates)


def main() -> int:
    ap = argparse.ArgumentParser(description="回填历史冷场事件（规则层重放）")
    ap.add_argument("--tag", action="append", default=[],
                    help="主播标签（可多次；不给则配合 --all-tags）")
    ap.add_argument("--date", action="append", default=[],
                    help="日期 YYYY-MM-DD（可多次；不给则用 --all-dates）")
    ap.add_argument("--all-tags", action="store_true", help="所有有弹幕的 tag")
    ap.add_argument("--all-dates", action="store_true", help="该 tag 所有有弹幕的日期")
    ap.add_argument("--force", action="store_true",
                    help="已有记录也重跑（会追加重复行，慎用）")
    ap.add_argument("--skip-today", action="store_true",
                    help="跳过今天（文件正被采集进程追加写，移不走也覆盖不了）")
    ap.add_argument("--data-dir", default=str(REPO_ROOT / "data"))
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    tags = list(args.tag) or (_all_tags(data_dir) if args.all_tags else [])
    if not tags:
        print("没有指定 tag（用 --tag 或 --all-tags）", file=sys.stderr)
        return 2

    total = {"done": 0, "skipped": 0, "no_danmu": 0, "sessions": 0}
    for tag in tags:
        dates = list(args.date) or (_all_dates(data_dir, tag) if args.all_dates else [])
        if not dates:
            print("[%s] 没有指定日期（用 --date 或 --all-dates）" % tag, file=sys.stderr)
            continue
        for date in dates:
            if not RE_DATE.match(date):
                print("[%s %s] 日期格式不对，跳过" % (tag, date), file=sys.stderr)
                continue
            if args.skip_today and date == _today_str():
                continue
            r = backfill_one(data_dir, tag, date, force=args.force)
            total[r["status"]] = total.get(r["status"], 0) + 1
            total["sessions"] += r["sessions"]
            print("[%s %s] %s 冷场 %d 段 room_id=%s"
                  % (tag, date, r["status"], r["sessions"], r.get("room_id", "")))
    print("汇总：写入 %d 个 (tag,date)，跳过 %d，无弹幕 %d，共 %d 段冷场"
          % (total["done"], total["skipped"], total["no_danmu"], total["sessions"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
