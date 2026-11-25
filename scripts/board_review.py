# -*- coding: utf-8 -*-
"""复盘数据聚合：按「日期 + 主播 tag」读 data/*.jsonl，给前端复盘页用。

与 dashboard.py 的分工
----------------------
`dashboard.py` 是**单房间**口径（自家主播，文件名模板写死，不带 tag），
它那套 build_state 不能直接用于本看板的 8 个竞品 tag。本模块按 tag 取文件，
统计口径（条数/逐分钟/在线序列/画像序列）与 dashboard 保持一致，读文件
复用它的 read_jsonl / file_meta / _num，避免两套实现漂移。

性能
----
单日单 tag 弹幕可能上万行（几 MB）。全量读一次约 100~300ms，加 mtime 缓存
（同一天同一 tag 反复请求直接命中）；超过 HUGE 行数时只保留尾部样本，统计
仍走全量。
"""
from __future__ import annotations

import os
import math
from collections import OrderedDict

import dashboard as dash
from board_usage import metrics_fingerprint, read_usage

MAX_SERIES_POINTS = 720      # 在线序列最多 720 点（12 小时 × 每分钟 1 点）
LATEST_N = 200               # 复盘页弹幕流最多给最近 200 条
COLD_N = 50                  # 冷场历史最多 50 条
PORTRAIT_SERIES_N = 30       # 画像序列（情感趋势）最多 30 点
_CACHE_MAX = 24

_cache: "OrderedDict[tuple, dict]" = OrderedDict()


def _paths(data_dir, date, tag):
    return {
        "danmu": os.path.join(data_dir, "danmu_douyin_%s_%s.jsonl" % (tag, date)),
        "stats": os.path.join(data_dir, "stats_douyin_%s_%s.jsonl" % (tag, date)),
        "portrait": os.path.join(data_dir, "portrait_%s_%s.jsonl" % (tag, date)),
        "coldline": os.path.join(data_dir, "coldline_%s.jsonl" % date),
        # 规则层冷场事件（2026-10-26）：analyzer 算出来的翻转，与开不开 LLM 无关
        "cold_events": os.path.join(data_dir, "cold_events_%s.jsonl" % date),
        "metrics": os.path.join(data_dir, "llm_metrics_%s.jsonl" % date),
    }


def _fingerprint(paths):
    out = []
    for k in ("danmu", "stats", "portrait", "coldline", "cold_events", "metrics"):
        p = paths[k]
        try:
            st = os.stat(p)
            out.append((k, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((k, 0, 0))
    return tuple(out)


def _permin_from_danmu(rows):
    """逐分钟弹幕量（只算 danmaku，和实时板的 rate_per_min 同口径）。"""
    buckets = {}
    for r in rows:
        if r.get("type") != "danmaku":
            continue
        ts = dash._num(r.get("ts"))
        if not ts:
            continue
        k = int(ts) // 60
        buckets[k] = buckets.get(k, 0) + 1
    return [{"ts": k * 60, "danmaku": v} for k, v in sorted(buckets.items())]


def _online_series(rows):
    """峰值从完整有效采样计算，分钟抽稀和展示截尾不改变统计。"""
    by_min = {}
    pv = None
    peak = None
    for r in rows:
        ts = dash._num(r.get("ts"))
        online = r.get("online")
        if (isinstance(ts, (int, float)) and math.isfinite(ts) and ts > 0
                and isinstance(online, (int, float)) and not isinstance(online, bool)
                and math.isfinite(online) and online >= 0):
            value = int(online)
            peak = value if peak is None else max(peak, value)
            point = (int(ts), value)
            minute = int(ts) // 60
            if minute not in by_min or point[0] >= by_min[minute][0]:
                by_min[minute] = point
        if r.get("type") == "room_user_seq" and r.get("total_pv") is not None:
            pv = r["total_pv"]
    series = [list(by_min[k]) for k in sorted(by_min)][-MAX_SERIES_POINTS:]
    return series, peak, pv


def _room_id_of(danmu_rows, stats_rows, portrait_rows):
    """从数据里推断 room_id（用于把 coldline 事件归到本 tag）。"""
    for rows in (portrait_rows, stats_rows, danmu_rows):
        for r in reversed(rows):
            rid = r.get("room_id")
            if rid:
                return str(rid)
    return ""


def _watch_web_rid(data_dir: str, tag: str) -> str:
    """读 data/watch_<tag>.jsonl 最后一行的 web_rid（主页号）。

    为什么要它：danmu/stats 落的是平台房间号（7700495043394439936），而 coldline
    落的是 web_rid（277671499254）—— 两个号不相等，光按 room_id 过滤冷场永远为 0
    （2026-10-25 定位）。watch jsonl 里正好两个都有，用它当桥。
    """
    import json as _json
    p = os.path.join(data_dir, "watch_%s.jsonl" % tag)
    if not os.path.exists(p):
        return ""
    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            # 未开播的行 web_rid 是 null，所以要往回翻到最近一条有值的
            # （一场直播可能几百行，256KB 足够覆盖，不够就放弃）
            f.seek(max(0, size - 262144))
            tail = f.read().decode("utf-8", "replace").strip().splitlines()
        for line in reversed(tail):
            line = line.strip()
            if not line:
                continue
            try:
                d = _json.loads(line)
            except ValueError:
                continue
            if d.get("web_rid"):
                return str(d["web_rid"])
    except OSError:
        return ""
    return ""


def review_tag(data_dir: str, date: str, tag: str) -> dict:
    """单日单主播的复盘数据（带 mtime 缓存）。"""
    paths = _paths(data_dir, date, tag)
    key = (data_dir, date, tag)
    fp = (_fingerprint(paths), metrics_fingerprint(data_dir))
    hit = _cache.get(key)
    if hit and hit["fp"] == fp:
        _cache.move_to_end(key)
        return hit["data"]

    danmu_rows = dash.read_jsonl(paths["danmu"]) or []
    stats_rows = dash.read_jsonl(paths["stats"]) or []
    portrait_rows = dash.read_jsonl(paths["portrait"]) or []
    cold_rows = dash.read_jsonl(paths["coldline"]) or []

    room_id = _room_id_of(danmu_rows, stats_rows, portrait_rows)

    # 弹幕
    counts = {}
    for r in danmu_rows:
        t = r.get("type") or ""
        counts[t] = counts.get(t, 0) + 1
    latest = [{"ts": r.get("ts"), "type": r.get("type", ""),
               "uname": r.get("uname", ""), "text": r.get("text", "")}
              for r in danmu_rows[-LATEST_N:]]
    # 互动流单独给一份（只 danmaku/gift）。为什么后端就要切：一场 4000+ 事件里
    # 95% 是「进入直播间/点赞」，直接对最后 N 条做前端筛选会筛出一条不剩
    # （2026-10-25 老大反馈"满屏进入了直播间"就是复盘页这个 feed）。
    interactive = [r for r in danmu_rows if r.get("type") in ("danmaku", "gift")]
    latest_interactive = [{"ts": r.get("ts"), "type": r.get("type", ""),
                           "uname": r.get("uname", ""), "text": r.get("text", "")}
                          for r in interactive[-LATEST_N:]]

    # 在线
    online_series, peak_online, total_pv = _online_series(stats_rows)

    # 画像
    latest_portrait = None
    portrait_series = []
    if portrait_rows:
        last = portrait_rows[-1]
        latest_portrait = {"ts": last.get("ts"), "room_id": last.get("room_id", ""),
                           "portrait": last.get("portrait", {}) or {}}
        for r in portrait_rows[-PORTRAIT_SERIES_N:]:
            p = r.get("portrait", {}) or {}
            portrait_series.append({"ts": r.get("ts"),
                                    "portrait_ts": p.get("portrait_ts"),
                                    "sentiment": p.get("sentiment", {}) or {},
                                    "interest_topics": p.get("interest_topics", []) or []})

    # 冷场：有 room_id 就严格过滤，没有就全给（并标记 how）
    # 含 cold_continue（冷场持续中的补词，2026-10-25 新增）：只取 cold_start 会把
    # 一场里大部分的救场台词挡在复盘页外面。
    cold_all = [r for r in cold_rows
                if r.get("event") in ("cold_start", "cold_continue")] or cold_rows
    web_rid = _watch_web_rid(data_dir, tag)

    def _mine(r):
        # 三条路任一命中就算本场：room_tag（新数据）> web_rid（历史 coldline）
        # > 平台房间号（老口径）
        if r.get("room_tag"):
            return r.get("room_tag") == tag
        rid = str(r.get("room_id") or "")
        return bool(rid) and (rid == web_rid or rid == room_id)

    if room_id or web_rid:
        cold_events = [r for r in cold_all if _mine(r)]
        cold_filter = "by_room_id"
    else:
        cold_events = cold_all
        cold_filter = "all_rooms"
    cold_events = cold_events[-COLD_N:]

    # 规则层冷场场次（2026-10-26）：analyzer 的 cold_start/cold_end 直接落盘，
    # 与开不开 LLM 无关。以前只有 LLM 层写 coldline → 没开 LLM 的场次
    # 「冷场次数」永远是 0（冤种小徐 10-25 那次就是这么被误读的）。
    rules_rows = dash.read_jsonl(paths["cold_events"]) or []
    cold_sessions = []
    cur = None
    for r in sorted((x for x in rules_rows if _mine(x)),
                    key=lambda x: dash._num(x.get("ts"))):
        ev = r.get("event")
        if ev == "cold_start":
            if cur is not None:      # 上一条没闭合（进程被杀/直接下播）
                cold_sessions.append(cur)
            cur = {"start": r.get("ts"), "end": None, "duration": None,
                   "count": r.get("count"), "threshold": r.get("threshold")}
        elif ev == "cold_end" and cur is not None:
            cur["end"] = r.get("ts")
            dur = r.get("duration")
            if dur is None:
                dur = dash._num(r.get("ts")) - dash._num(cur.get("start"))
            cur["duration"] = int(dur)
            cold_sessions.append(cur)
            cur = None
    if cur is not None:
        cold_sessions.append(cur)

    cold_total_seconds = sum(int(s.get("duration") or 0) for s in cold_sessions)
    # 没有规则层文件（10-26 之前的历史场次）→ 退回旧口径：数 cold_start
    if cold_sessions:
        cold_count = len(cold_sessions)
        cold_source = "rules"
    else:
        cold_count = len([r for r in cold_events if r.get("event") == "cold_start"])
        cold_source = "llm" if cold_events else "none"

    # Material date is independent of the actual SDK call date. Missing room
    # ownership is reported as incomplete, never replaced with global totals.
    usage = read_usage(data_dir, tag=tag, data_date=date)

    data = {
        "date": date,
        "tag": tag,
        "room_id": room_id,
        "danmu": {
            "total": len(danmu_rows),
            "danmaku_count": counts.get("danmaku", 0),
            "counts": counts,
            "permin": _permin_from_danmu(danmu_rows),
            "latest": latest,
            "latest_interactive": latest_interactive,
        },
        "stats": {
            "online_series": online_series,
            "peak_online": peak_online,
            "total_pv": total_pv,
            "points": len(stats_rows),
        },
        "portrait": {"latest": latest_portrait, "series": portrait_series},
        "coldline": {
            "events": cold_events,                       # 台词事件（LLM 层）
            "filter": cold_filter,
            "count": cold_count,                          # 冷场次数（规则层优先）
            "total_seconds": cold_total_seconds,
            "source": cold_source,                        # rules | llm | none
            "sessions": cold_sessions[-COLD_N:],          # 规则层场次（起止+时长）
        },
        "llm": usage,
        "files": {k: dash.file_meta(paths[k])
                  for k in ("danmu", "stats", "portrait", "coldline", "cold_events")},
        "empty": not (danmu_rows or stats_rows or portrait_rows),
    }

    _cache[key] = {"fp": fp, "data": data}
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)
    return data


def available_dates(data_dir: str, limit: int = 60) -> list:
    """data/ 下出现过的日期（倒序）。文件名里就带日期，不用读内容。"""
    import re
    import glob
    pat = re.compile(r"_(\d{4}-\d{2}-\d{2})\.jsonl$")
    days = set()
    for p in glob.glob(os.path.join(data_dir, "*_*.jsonl")):
        m = pat.search(os.path.basename(p))
        if m:
            days.add(m.group(1))
    return sorted(days, reverse=True)[:limit]


def available_tags(data_dir: str, date: str = "") -> list:
    """有数据的主播 tag 列表。给了日期就只列当天有落盘的。"""
    import re
    import glob
    suffixes = ["danmu_douyin_", "stats_douyin_", "portrait_"]
    tags = set()
    for p in glob.glob(os.path.join(data_dir, "*.jsonl")):
        name = os.path.basename(p)
        for suf in suffixes:
            if name.startswith(suf) and name.endswith(".jsonl"):
                rest = name[len(suf):-len(".jsonl")]
                m = re.match(r"^(.+)_(\d{4}-\d{2}-\d{2})$", rest)
                if m:
                    if not date or m.group(2) == date:
                        tags.add(m.group(1))
    return sorted(tags)
