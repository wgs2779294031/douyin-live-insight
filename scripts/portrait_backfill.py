#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""事后批量画像：重放落盘弹幕 → 规则快照 → LLM 画像 → portrait jsonl。

用途：事后按需对指定主播标签/日期的已有弹幕生成画像。
  与实时版**同构**：同一 DanmuAnalyzer.get_snapshot() 快照、同一
  PortraitService（含匿名化/normalize）；新任务行带
  {"ts","task_id","room_tag","purpose","portrait"}，兼容原有画像读取。

要点：
- 走 LLMRouter并沿用既有token账本、门禁和metrics，不据此推断金额；
- 每 --every 秒一个画像点；冷场窗口弹幕数 < --min-danmaku 的点跳过
  （对齐实时版空窗熔断 portrait_min_danmaku）；
- --max-calls限制每个任务的generate调用数，不代表HTTP尝试数或金额；
- 标准CLI/API通过TaskStore按物理tag/日期互斥，普通请求复用耐久任务结果；
- 输入一次读取冻结SHA/范围；--force新建独立run并追加归属行，保留旧输出；
- 旧纯内存backfill_tag helper仅供兼容测试，不是标准接单/成功判定入口；
- 无人设路径（竞品号无 anchor profile，与实时无人设 prompt 逐字符一致）。

用法：
  python scripts/portrait_backfill.py --tag synthetic --date 2026-10-12
  python scripts/portrait_backfill.py --tag synthetic --all-dates --every 300
  python scripts/portrait_backfill.py --tag A --tag B --date 2026-10-12 --force
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
import uuid
from datetime import date as CalendarDate
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import analyzer as az  # noqa: E402
from file_lock import file_lock  # noqa: E402
from llm_service import (  # noqa: E402
    DailyJsonlWriter, LLMConfig, PortraitService, load_llm_config,
)

DEFAULT_EVERY = 120          # 画像点间隔（秒），对齐实时 portrait_interval_seconds
DEFAULT_MIN_DANMAKU = 1      # 冷场窗口弹幕下限，对齐实时 portrait_min_danmaku
DEFAULT_MAX_CALLS = 40       # 单任务generate上限，不代表HTTP尝试数


def _snapshot_input(data_dir: Path, tag: str, date: str):
    """Freeze one exact byte read; later appends cannot extend this execution."""
    name = f"danmu_douyin_{tag}_{date}.jsonl"
    try:
        raw = (data_dir / name).read_bytes()
        missing = False
    except FileNotFoundError:
        raw, missing = b"", True
    records, invalid = [], 0
    for line in raw.decode("utf-8-sig", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            invalid += 1
            continue
        ts = record.get("ts") if isinstance(record, dict) else None
        if type(ts) not in (int, float) or not math.isfinite(ts):
            invalid += 1
            continue
        records.append(record)
    records.sort(key=lambda item: item["ts"])
    return records, {
        "input_ref": name, "captured_at": time.time(), "missing": missing,
        "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
        "records": len(records), "invalid_records": invalid,
        "ts_min": records[0]["ts"] if records else None,
        "ts_max": records[-1]["ts"] if records else None,
    }


def _replay_windows(records, every):
    analyzer = az.DanmuAnalyzer(az.AnalyzerConfig())
    last_snapshot = None
    for record in records:
        try:
            analyzer.feed(record)
        except Exception:
            continue
        ts = record["ts"]
        if last_snapshot is None:
            last_snapshot = ts
        elif ts - last_snapshot >= every:
            last_snapshot = ts
            yield ts, analyzer.get_snapshot()


def _dates_for_tag(data_dir, tag):
    prefix = f"danmu_douyin_{tag}_"
    dates = []
    for path in data_dir.glob("danmu_douyin_*.jsonl"):
        if not path.stem.startswith(prefix):
            continue
        suffix = path.stem[len(prefix):]
        try:
            if CalendarDate.fromisoformat(suffix).isoformat() == suffix:
                dates.append(suffix)
        except ValueError:
            continue
    return dates


def _read_records(path: Path) -> List[Dict[str, Any]]:
    """读弹幕 jsonl，按 ts 升序；坏行跳过。"""
    records: List[Dict[str, Any]] = []
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and isinstance(rec.get("ts"), (int, float)):
            records.append(rec)
    records.sort(key=lambda r: r["ts"])
    return records


def backfill_tag(data_dir: Path, tag: str, date: str, svc: PortraitService,
                 every: int = DEFAULT_EVERY,
                 min_danmaku: int = DEFAULT_MIN_DANMAKU,
                 max_calls: int = DEFAULT_MAX_CALLS,
                 force: bool = False) -> Dict[str, Any]:
    """Legacy pure in-memory helper; standard entry points use execute_task.

    :return: 汇总 {"status": skipped|done|no_danmu, "windows", "ok",
                   "fail", "out_path"}
    """
    out_path = data_dir / f"portrait_{tag}_{date}.jsonl"
    if not force and out_path.exists() and out_path.stat().st_size > 0:
        return {"status": "skipped", "windows": 0, "ok": 0, "fail": 0,
                "out_path": str(out_path)}

    records = _read_records(data_dir / f"danmu_douyin_{tag}_{date}.jsonl")
    if not records:
        return {"status": "no_danmu", "windows": 0, "ok": 0, "fail": 0,
                "out_path": str(out_path)}

    cfg = az.AnalyzerConfig()
    az_analyzer = az.DanmuAnalyzer(cfg)
    last_snap_ts: Optional[float] = None
    calls = 0
    ok = fail = windows = 0

    # 逐点落盘 + 逐点打日志（2026-10-25）：
    # 原实现把行攒在内存里、最后一次性写，跑 40 次调用要好几分钟，中途一看
    # 日志是空的、文件也不存在，等于「点了按钮什么也看不到」；真挂了还全丢。
    # 顺带把进度打出来，前端能拿日志末行当状态。
    with open(out_path, "a", encoding="utf-8") as fout:
        for rec in records:
            try:
                az_analyzer.feed(rec)
            except Exception:
                continue  # 单条坏记录不影响整体重放
            ts = rec["ts"]
            if last_snap_ts is None:
                last_snap_ts = ts          # 首点只记起点，对齐实时「首窗不推」
                continue
            if ts - last_snap_ts < every:
                continue
            windows += 1
            last_snap_ts = ts
            if calls >= max_calls:
                break
            snap = az_analyzer.get_snapshot()
            cw = int((snap.get("cold_window") or {}).get("count") or 0)
            if cw < min_danmaku:
                continue                   # 空窗熔断，对齐实时
            calls += 1
            print(f"[{tag} {date}] 第 {calls}/{max_calls} 次调用中…", flush=True)
            portrait = svc.generate(snap, profile=None)
            if portrait is None:
                fail += 1
                print(f"[{tag} {date}] 第 {calls} 次失败", flush=True)
                continue
            ok += 1
            fout.write(json.dumps({
                "ts": int(snap.get("ts") or ts),
                "room_tag": tag,
                "portrait": portrait,
            }, ensure_ascii=False) + "\n")
            fout.flush()
            print(f"[{tag} {date}] 第 {ok} 个画像点已落盘（调用 {calls}/{max_calls}）",
                  flush=True)

    # 行已逐点写入（见上面的 with open）—— 这里不再重复写一遍
    return {"status": "done", "windows": windows, "ok": ok, "fail": fail,
            "out_path": str(out_path)}


class _TaskAbort(RuntimeError):
    def __init__(self, state, reason):
        super().__init__(reason)
        self.state, self.reason = state, reason


_BLOCK_REASONS = {
    "global_paused", "purpose_paused", "control_changed", "control_epoch_changed", "credentials_changed",
    "client_unavailable", "daily_budget_exhausted", "budget_ledger_error", "task_blocked",
    "application_stopping",
}


def _credentials(cfg):
    return tuple(sorted((name, *(str(row.get(key) or "") for key in
                        ("api_key", "base_url", "model", "protocol")))
                        for name, row in cfg.providers.items()))


def _data_path(data_dir, name):
    base = Path(data_dir).resolve()
    path = base / name
    if Path(name).name != name or "\\" in name or path.resolve().parent != base:
        raise _TaskAbort("failed", "invalid_scope")
    return path


def _append_task_output(path, record):
    encoded = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    with file_lock(str(path)):
        with path.open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell():
                stream.seek(-1, os.SEEK_END)
                tail = stream.read(1)
                stream.seek(0, os.SEEK_END)
                if tail != b"\n":
                    stream.write(b"\n")
            start = stream.tell()
            if stream.write(encoded) != len(encoded):
                raise OSError("backfill output incomplete")
            stream.flush()
            os.fsync(stream.fileno())
            return start, stream.tell()


def execute_task(run, data_dir: Path, config_path: str) -> Dict[str, Any]:
    """Execute one claimed run; TaskStore remains the durable authority."""
    record = dict(run.record)
    task_id, tag, date = record["task_id"], record["tag"], record["data_date"]
    parameters = record.get("parameters") or {}
    counts = {"windows_total": 0, "windows_seen": 0, "calls": 0,
              "ok": 0, "fail": 0, "skipped": 0, "unknown": 0}
    output_ref = {"path": f"portrait_{tag}_{date}.jsonl", "task_id": task_id,
                  "rows": 0, "byte_start": None, "byte_end": None}
    captured = None
    router = metrics_writer = None
    context = {"reason": "", "pending": False, "sent": False,
               "signature": None, "stages": []}
    state, reason = "failed", "execution_failed"
    processed = {}

    def stop_reason():
        return (getattr(run, "stop_reason", "") or context["reason"]
                or getattr(router, "last_block_reason", "") or "task_blocked")

    def guarded():
        if context["reason"]:
            return False
        try:
            if not run.guard():
                return False
            if context["signature"] is not None:
                fresh = load_llm_config(config_path)
                if _credentials(fresh) != context["signature"]:
                    context["reason"] = "credentials_changed"
                    return False
            return True
        except Exception:
            context["reason"] = "task_state_unavailable"
            return False

    def fields():
        return {"counts": dict(counts), "input_snapshot": captured,
                "output_ref": dict(output_ref), "processed": dict(processed)}

    def progress(settled=False):
        try:
            run.progress(dispatch_settled=settled, **fields())
            if settled:
                context["pending"] = False
        except Exception:
            context["reason"] = "task_state_unavailable"
            raise _TaskAbort("unknown", "task_state_unavailable") from None

    def dispatch(provider, model):
        if not guarded():
            raise RuntimeError("task_blocked")
        try:
            stages = context["stages"]
            if not stages or (stages[-1]["provider"], stages[-1]["model"]) != (provider, model):
                stages.append({"provider": provider, "model": model, "observed": False})
            context["pending"] = True  # A failed marker write can itself be uncertain.
            if run.before_send(provider, model) is False:
                raise RuntimeError("task_blocked")
            context["sent"] = True
        except Exception:
            context["reason"] = getattr(run, "stop_reason", "") or "task_state_unavailable"
            raise RuntimeError("task_state_unavailable") from None

    def metrics(rec):
        error_type = str(rec.get("error_type") or "")
        stages = context["stages"]
        # SDK callbacks are synchronous: retries share this provider/model stage,
        # and a later provider cannot settle an earlier provider's unknown result.
        if (stages and (rec.get("provider"), rec.get("model"))
                == (stages[-1]["provider"], stages[-1]["model"])
                and not rec.get("probe") and (rec.get("success") is True
                or error_type in ("empty_content", "credential_in_response")
                or error_type.rsplit("_", 1)[-1] in ("400", "401", "403", "404", "422"))):
            stages[-1]["observed"] = True
        item = dict(rec, task_id=task_id, room_tag=tag, purpose="review", data_date=date)
        try:
            metrics_writer.write(item)
        except Exception:
            context["reason"] = "task_state_unavailable"
            raise RuntimeError("task_state_unavailable") from None

    try:
        every = int(parameters.get("every", DEFAULT_EVERY))
        minimum = int(parameters.get("min_danmaku", DEFAULT_MIN_DANMAKU))
        maximum = int(parameters.get("max_calls", DEFAULT_MAX_CALLS))
        if every <= 0 or minimum < 0 or maximum <= 0:
            raise _TaskAbort("failed", "invalid_parameters")
        output_path = _data_path(data_dir, output_ref["path"])
        _data_path(data_dir, f"danmu_douyin_{tag}_{date}.jsonl")
        if not guarded():
            raise _TaskAbort("blocked", stop_reason())
        cfg = load_llm_config(config_path)
        context["signature"] = _credentials(cfg)
        records, captured = _snapshot_input(Path(data_dir), tag, date)
        windows = list(_replay_windows(records, every))
        counts["windows_total"] = len(windows)
        progress()
        if not records:
            raise _TaskAbort("failed", "no_data")
        metrics_writer = DailyJsonlWriter(str(data_dir), "llm_metrics")
        router = cfg.build_router(on_metrics=metrics, budget_dir=str(data_dir), tag=tag,
                                  automatic=False, control_epoch=record["control_epoch"],
                                  request_guard=guarded, control_data_dir=str(data_dir),
                                  on_dispatch=dispatch)
        if router is None or not router.is_available:
            raise _TaskAbort("blocked", "client_unavailable")
        svc = PortraitService(router, purpose="review")
        for ts, snapshot in windows:
            if not guarded():
                raise _TaskAbort("unknown" if stop_reason() == "task_state_unavailable" else "blocked", stop_reason())
            if counts["calls"] >= maximum:
                state, reason = ("partial" if counts["ok"] else "failed"), "call_limit"
                break
            counts["windows_seen"] += 1
            processed["current_window_ts"] = ts
            if int((snapshot.get("cold_window") or {}).get("count") or 0) < minimum:
                counts["skipped"] += 1
                processed["processed_ts_end"] = ts
                progress()
                continue
            progress()
            counts["calls"] += 1
            context["stages"] = []
            try:
                portrait = svc.generate(snapshot, profile=None)
            except Exception:
                portrait = None
            allowed = guarded()
            blocked = (stop_reason() if not allowed else getattr(router, "last_block_reason", ""))
            unresolved = context["pending"] and any(not stage["observed"] for stage in context["stages"])
            if portrait is None and unresolved:
                counts["unknown"] += 1
                progress()
                raise _TaskAbort("unknown", "task_state_unavailable" if context["reason"]
                                 == "task_state_unavailable" else "request_result_unknown")
            if portrait is not None:
                try:
                    start, end = _append_task_output(output_path, {
                        "ts": int(snapshot.get("ts") or ts), "room_tag": tag,
                        "task_id": task_id, "purpose": "review", "portrait": portrait,
                    })
                except Exception:
                    counts["unknown"] += 1
                    raise _TaskAbort("unknown", "output_write_failed") from None
                counts["ok"] += 1
                output_ref["rows"] += 1
                if output_ref["byte_start"] is None:
                    output_ref["byte_start"] = start
                output_ref["byte_end"] = end
            elif not blocked:
                counts["fail"] += 1
            processed["processed_ts_end"] = ts
            if unresolved:
                counts["unknown"] += 1
                progress()
                raise _TaskAbort("unknown", "task_state_unavailable" if context["reason"]
                                 == "task_state_unavailable" else "request_result_unknown")
            progress(settled=context["reason"] != "task_state_unavailable"
                     and blocked != "task_state_unavailable")
            if blocked:
                raise _TaskAbort("blocked" if blocked in _BLOCK_REASONS else "unknown", blocked)
        else:
            state = "partial" if counts["ok"] and counts["fail"] else "failed" if counts["fail"] else "succeeded"
            reason = "completed_with_failures" if counts["fail"] else "no_eligible_windows" if not counts["calls"] else "completed"
    except _TaskAbort as error:
        state, reason = error.state, error.reason
    except Exception:
        state, reason = ("unknown" if context["sent"] else "failed"), "execution_failed"
    finally:
        for resource in (metrics_writer, router):
            close = getattr(resource, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    state, reason = "unknown", "cleanup_failed"
        try:
            run.finish(state, reason=reason, dispatch_settled=not context["pending"], **fields())
        except Exception:
            state, reason = "unknown", "task_state_unavailable"
            try:
                run.finish(state, reason=reason, dispatch_settled=False, **fields())
            except Exception:
                return {"task_id": task_id, "state": state, "status": state, "reason": reason}
    try:
        return run.public()
    except Exception:
        return {"task_id": task_id, "state": "unknown", "status": "unknown",
                "reason": "task_state_unavailable"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="事后批量画像（弹幕重放→LLM→portrait jsonl）")
    ap.add_argument("--task-id", help=argparse.SUPPRESS)
    ap.add_argument("--tag", action="append",
                    help="主播标签（可多次）")
    ap.add_argument("--date", action="append",
                    help="日期 YYYY-MM-DD（可多次；与 --all-dates 二选一）")
    ap.add_argument("--all-dates", action="store_true",
                    help="自动扫描该 tag 全部有弹幕数据的日期")
    ap.add_argument("--every", type=int,
                    help=f"画像点间隔秒（默认 {DEFAULT_EVERY}，对齐实时）")
    ap.add_argument("--min-danmaku", type=int,
                    help=f"窗口弹幕下限（默认 {DEFAULT_MIN_DANMAKU}，对齐实时熔断）")
    ap.add_argument("--max-calls", type=int,
                    help=f"单任务generate调用封顶（默认 {DEFAULT_MAX_CALLS}）")
    ap.add_argument("--force", action="store_true",
                    help="明确新建独立run并追加结果，保留旧输出和历史任务")
    ap.add_argument("--data-dir", default=str(REPO_ROOT / "data"))
    ap.add_argument("--config", default=str(REPO_ROOT / "config.json"))
    args = ap.parse_args(argv)
    if args.task_id and (args.tag or args.date or args.all_dates or args.force
                         or any(value is not None for value in (args.every, args.min_danmaku, args.max_calls))):
        ap.error("子任务只允许从任务记录读取处理范围与参数")
    if not args.task_id and (not args.tag or bool(args.date) == bool(args.all_dates)):
        ap.error("请提供 --tag，并选择 --date 或 --all-dates")
    from backfill_tasks import TaskStore
    data_dir, config_path = Path(args.data_dir).resolve(), str(Path(args.config).resolve())
    try:
        store = TaskStore(data_dir=str(data_dir), config_path=config_path)
        if args.task_id:
            run = store.claim(args.task_id, timeout=10)
            result = execute_task(run, data_dir, config_path)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return 0 if result.get("state", result.get("status")) == "succeeded" else 2
        code = 0
        parameters = {"every": args.every if args.every is not None else DEFAULT_EVERY,
                      "min_danmaku": args.min_danmaku if args.min_danmaku is not None else DEFAULT_MIN_DANMAKU,
                      "max_calls": args.max_calls if args.max_calls is not None else DEFAULT_MAX_CALLS}
        seen = set()
        for tag in dict.fromkeys(item.strip() for item in args.tag):
            dates = list(args.date or [])
            if args.all_dates:
                dates = _dates_for_tag(data_dir, tag)
            for date in sorted(set(item.strip() for item in dates)):
                physical = os.path.normcase(str(data_dir / f"portrait_{tag}_{date}.jsonl"))
                if physical in seen:
                    continue
                seen.add(physical)
                response, run = store.reserve_inline(tag, date, force=args.force, parameters=parameters,
                                                     request_id=uuid.uuid4().hex if args.force else None)
                if run is not None:
                    result = execute_task(run, data_dir, config_path)
                    response = dict(response, task=result, status=result.get("state", result.get("status")))
                print(json.dumps(response, ensure_ascii=False), flush=True)
                status = (response.get("task") or {}).get("state", response.get("status"))
                if status not in ("succeeded", "running", "launching", "queued"):
                    code = 2
        return code
    except Exception:
        print(json.dumps({"ok": False, "state": "unknown", "reason": "task_state_unavailable"}), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
