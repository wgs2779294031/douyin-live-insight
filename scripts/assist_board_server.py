# -*- coding: utf-8 -*-
"""直播辅助实时看板 · Web 服务（SSE，方案见 docs/直播辅助看板_SSE方案.md）。

架构（采集/分析进程零改动）：
    采集/分析进程照旧逐行落盘 data/*.jsonl
        ↓ 本服务只读 tail（1s 定时轮询 offset，绝不写、不用 fs.watch）
    广播器 1s 生成一帧快照（所有 SSE 连接共享，只序列化一次）
        ↓ 前端 EventSource 订阅 /api/sse
    Web 前端（web/dist 或 scripts/assist_board.html）

数据源（全部只读）：
    danmu_douyin_<tag>_<date>.jsonl   弹幕流（danmaku/enter/like/gift）
    stats_douyin_<tag>_<date>.jsonl   在线人数(room_stats)/累计观看(room_user_seq)
    portrait_<tag>_<date>.jsonl       LLM 画像快照（取最后一行 portrait）
    coldline_<date>.jsonl             冷场事件（多主播共文件，按 room_id 归 tag）
    watch_<tag>.jsonl                 开播探测（live_status/is_live，无日期按文件尾）

鉴权（2026-10-22 起必须）：
    除 /api/login、/api/health、静态资源外，全部要 Cookie 里的签名 token。
    ★ 绝不做「源 IP=127.0.0.1 免登录」：frp 的入站在本机看全是 127.0.0.1，
      任何本地豁免等于全公网豁免。

用法：
    python scripts/assist_board_server.py [--port 8765] [--bind 127.0.0.1]
        [--data <dir>] [--date YYYY-MM-DD]   # 不传 --date = 跟随今天（跨天自动切）
"""
import argparse
import glob
import json
import logging
import math
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import time
from functools import wraps
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import anchor_manager as anchors_mgr            # noqa: E402
import anchor_roster as roster_mod              # noqa: E402
import board_alert                              # noqa: E402
import board_auth as auth                       # noqa: E402
import board_broadcast as broadcast             # noqa: E402
import board_llm                                # noqa: E402
import board_review as review                   # noqa: E402
import llm_pricing                              # noqa: E402
from backfill_tasks import TaskStore, TaskError  # noqa: E402
from watch_anchor_live import classify_env_failure, is_confirmed_offline  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DATA = os.path.join(REPO_ROOT, "data")
DEFAULT_HTML = os.path.join(REPO_ROOT, "scripts", "assist_board.html")
DEFAULT_DIST = os.path.join(REPO_ROOT, "web", "dist")
BOARD_VERSION = "3.8.0-web1"

POLL_INTERVAL = 1.0     # 文件轮询周期（秒）
SSE_INTERVAL = 1.0      # SSE 推送粒度（秒）
PING_EVERY = 15         # 每 N 帧插一个 ping 保活
COLD_ACTIVE_WINDOW = 180    # cold_start 落盘后 N 秒内视为「冷场中」
LIVE_SILENCE_WINDOW = 600   # 无 watch 文件时，stats/danmu 近 N 秒有活动视为开播
RATE_WINDOW_MIN = 5         # 弹幕流速 = 最近 N 分钟均值
RECENT_FEED = 60            # 单 tag 保留条数（快照里带）
ONLINE_SERIES_SEC = 180     # 在线人数序列保留窗口
MAX_FILE_BYTES = 30 * 1024 * 1024   # 新接入文件过大时只读尾部
FEED_BURST = 500            # 单帧最多携带的新增弹幕（防突发刷屏把 payload 撑爆）

_LIKE_RE = re.compile(r"点(?:了)?(\d+)个赞")
_GIFT_RE = re.compile(r"送出\s*(.+?)\s*x(\d+)")
_RE_DATED = re.compile(r"^(danmu_douyin|stats_douyin|portrait)_(.+)_((\d{4}-\d{2}-\d{2}))\.jsonl$")
_RE_WATCH = re.compile(r"^watch_(.+)\.jsonl$")
_RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

PUBLIC_PATHS = {"/api/login", "/api/health"}
STATIC_PREFIXES = ("/assets/",)


def _minute(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M")


class TagState:
    """单个主播 tag 的内存态（当前场次口径，文件按天切名自动跟随）。"""

    def __init__(self, tag):
        self.tag = tag
        self.totals = {"danmaku": 0, "enter": 0, "like": 0, "gift": 0}
        # permin 的 key 用「epoch 分钟」而不是 "HH:MM" 字符串：
        # 字符串跨零点比较 ("23:59" >= "00:05" 为 False) 会把昨天的分钟
        # 当成未来的，导致 rate_per_min 归零。输出时再格式化。
        self.permin = {}                    # epoch_minute -> int 弹幕条数
        self.recent_danmaku = deque(maxlen=RECENT_FEED)
        self.online = None                  # 最新在线人数
        self.total_pv = None                # 最新累计观看
        self.online_series = deque()        # (ts, online)，滚动裁剪
        self.portrait = None                # 最后一行 portrait 整体
        self.cold = None                    # 最新 cold_start 事件
        self.watch_last = None              # watch_<tag>.jsonl 最后一行
        self.last_activity_ts = 0           # 最近一条弹幕/统计，供探测异常时判断是否在播
        self.room_id = None                 # 最近出现的 room_id
        self.last_ts = 0                    # 任一数据源最新落盘时间

    # ---- danmu ----
    def add_danmu(self, ev, seq=0):
        t = ev.get("type")
        ts = ev.get("ts") or int(time.time())
        text = ev.get("text") or ""
        if self.room_id is None or ev.get("room_id"):
            self.room_id = ev.get("room_id") or self.room_id
        self.last_ts = max(self.last_ts, ts)
        self.last_activity_ts = max(self.last_activity_ts, ts)
        if t == "danmaku":
            self.totals["danmaku"] += 1
            k = int(ts) // 60
            self.permin[k] = self.permin.get(k, 0) + 1
            self.recent_danmaku.append({"seq": seq, "ts": ts, "type": "danmaku",
                                        "uname": ev.get("uname") or "匿名", "text": text})
        elif t == "enter":
            self.totals["enter"] += 1
        elif t == "like":
            m = _LIKE_RE.search(text)
            self.totals["like"] += int(m.group(1)) if m else 1
        elif t == "gift":
            self.totals["gift"] += 1
            self.recent_danmaku.append({"seq": seq, "ts": ts, "type": "gift",
                                        "uname": ev.get("uname") or "匿名",
                                        "text": text, "gift": True})

    # ---- stats ----
    def add_stats(self, ev):
        ts = ev.get("ts") or int(time.time())
        online = ev.get("online")
        if online is not None:
            self.online = online
            self.online_series.append((ts, online))
            cut = ts - ONLINE_SERIES_SEC
            while self.online_series and self.online_series[0][0] < cut:
                self.online_series.popleft()
        if ev.get("type") == "room_user_seq" and ev.get("total_pv") is not None:
            self.total_pv = ev["total_pv"]
        self.last_ts = max(self.last_ts, ts)
        self.last_activity_ts = max(self.last_activity_ts, ts)

    # ---- portrait / coldline / watch ----
    def set_portrait(self, ev):
        if isinstance(ev.get("portrait"), dict):
            self.portrait = ev["portrait"]
            self.last_ts = max(self.last_ts, ev.get("ts") or 0)

    def set_cold(self, ev):
        self.cold = {"ts": ev.get("ts"), "lines": ev.get("lines") or [],
                     "anchor_used": ev.get("anchor_used")}
        self.last_ts = max(self.last_ts, ev.get("ts") or 0)

    def set_watch(self, ev):
        self.watch_last = ev

    # ---- 派生指标 ----
    def rate_per_min(self, now_ts):
        """弹幕流速：最近 RATE_WINDOW_MIN 分钟均值（条/分）。"""
        if not self.permin:
            return 0
        cutoff = int(now_ts - RATE_WINDOW_MIN * 60) // 60
        recent = [v for k, v in self.permin.items() if k >= cutoff]
        return int(sum(recent) / RATE_WINDOW_MIN)

    def _probe_state(self, now_ts):
        w = self.watch_last
        if w is None:
            return "missing"
        ts = w.get("ts")
        if type(ts) not in (int, float) or not math.isfinite(ts) or ts <= 0 or ts > now_ts:
            return "error"
        if now_ts - ts > 300:
            return "stale"
        if classify_env_failure(w) is not None or w.get("api_code") not in (None, 0):
            return "error"
        if w.get("is_live") is True and w.get("web_rid"):
            return "live"
        if is_confirmed_offline(w):
            return "offline"
        return "error"

    def live_status(self, now_ts, probe_state=None):
        """保留二元组兼容旧消费者，未知状态由理由及 snapshot.probe_state 区分。"""
        state = probe_state or self._probe_state(now_ts)
        if state in ("live", "offline"):
            return state == "live", "watch探测"
        if self.last_activity_ts and 0 <= now_ts - self.last_activity_ts <= LIVE_SILENCE_WINDOW:
            return True, "数据流活动"
        return False, {"error": "探测异常，状态未知", "stale": "探测已过期，状态未知",
                       "missing": "尚无探测，状态未知"}[state]

    def cold_state(self, now_ts):
        if not self.cold or not self.cold.get("ts"):
            return {"state": "normal", "ts": None, "lines": []}
        dt = now_ts - self.cold["ts"]
        if 0 <= dt <= COLD_ACTIVE_WINDOW:
            return {"state": "cold", "ts": self.cold["ts"], "lines": self.cold["lines"]}
        return {"state": "normal", "ts": self.cold["ts"], "lines": self.cold["lines"]}

    def snapshot(self, now_ts):
        permin = sorted(self.permin)[-60:]
        questions = []
        suggestions = []
        p = self.portrait
        if p:
            questions = sorted(p.get("question_topics") or [],
                               key=lambda q: -(q.get("count") or 0))[:5]
            suggestions = p.get("suggestions") or []
        # 数据停滞（2026-10-21 语义修正）：watch 探测新鲜且明确 is_live=False
        # 时，数据流停 = 主播正常下播，**不是**采集问题（此前误报黄字警告）。
        # 只有「watch 说在播但没数据」或「watch 与数据一起陈旧」才算采集异常。
        probe_state = self._probe_state(now_ts)
        _stale = (bool(self.last_ts)
                  and (now_ts - self.last_ts) > LIVE_SILENCE_WINDOW
                  and probe_state != "offline")
        return {
            "tag": self.tag,
            "room_id": self.room_id,
            "live": list(self.live_status(now_ts, probe_state)),
            "probe_state": probe_state,
            "probe_hint": {"error": "最近一次开播探测失败", "stale": "开播探测已过期",
                           "missing": "尚未收到开播探测"}.get(probe_state),
            "stale": _stale,
            "data_age": (now_ts - self.last_ts) if self.last_ts else None,
            "online": self.online,
            "total_pv": self.total_pv,
            "online_series": list(self.online_series),
            "rate_per_min": self.rate_per_min(now_ts),
            "permin": [{"minute": _minute(k * 60), "danmaku": self.permin[k]}
                       for k in permin],
            "totals": dict(self.totals),
            "cold": self.cold_state(now_ts),
            "portrait_ts": (p or {}).get("portrait_ts"),
            "questions": questions,
            "suggestions": suggestions,
            "consumption_intent": (p or {}).get("consumption_intent"),
            "activity_layers": (p or {}).get("activity_layers"),
            "recent_danmaku": list(self.recent_danmaku),
            "last_ts": self.last_ts,
        }


class Store:
    """只读 tail 引擎 + 聚合状态（线程安全）。"""

    def __init__(self, data_dir, date_str=None, roster_path=None):
        """:param date_str: 固定日期（回放）；None = 跟随今天，跨天自动切换。

        2026-10-22 修：此前 --date 默认取启动当天并固化，过零点后看板仍在
        读昨天的文件，第二天早上看不到当天数据。
        """
        self.data_dir = data_dir
        self.roster_path = roster_path
        self.fixed_date = date_str or None
        self.lock = threading.Lock()
        self.tags = {}              # tag -> TagState
        self.room2tag = {}          # room_id -> tag（coldline 多主播共文件归位用）
        self.pending_cold = {}      # room_id -> 最新 cold_start 事件（等 tag 映射就绪再归位）
        self.tailers = {}           # path -> 已读字节 offset
        self.bufs = {}              # path -> 残行缓冲
        self.errors = deque(maxlen=20)
        self._seq = 0               # 弹幕全局序号（前端按 seq 去重）
        self._new_danmu = deque(maxlen=FEED_BURST)   # 待推送增量
        self._last_day = None

    # ---- 日期 ----
    def today(self):
        return self.fixed_date or datetime.now().strftime("%Y-%m-%d")

    # ---- 文件发现 ----
    def _scan_paths(self):
        d = self.data_dir
        day = self.today()
        if self._last_day not in (None, day):
            # 跨天：老日期的 tailer 不再增长，清掉省内存（tag 状态保留，
            # 因为主播可能跨零点连着播，清空会丢当前场次数据）。
            stale = [p for p in self.tailers
                     if re.search(r"_\d{4}-\d{2}-\d{2}\.jsonl$", p)
                     and not p.endswith("_%s.jsonl" % day)]
            for p in stale:
                self.tailers.pop(p, None)
                self.bufs.pop(p, None)
        self._last_day = day
        dated = glob.glob(os.path.join(d, f"*_{day}.jsonl"))
        watch = glob.glob(os.path.join(d, "watch_*.jsonl"))
        # 注意 _classify 返回 (None, None) 时元组本身为真值，必须按位判
        return {p for p in dated + watch if _classify(os.path.basename(p))[0]}

    # ---- 行分发 ----
    def _feed(self, path, raw):
        try:
            ev = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception as e:
            self.errors.append(f"{os.path.basename(path)}: 解析失败 {e}")
            return
        if not isinstance(ev, dict):    # 合法 JSON 但非对象（如裸字符串）→ 按坏行处理
            self.errors.append(f"{os.path.basename(path)}: 非 dict 行，跳过")
            return
        kind, tag = _classify(os.path.basename(path))
        if kind == "watch":
            with self.lock:
                self.tags.setdefault(tag, TagState(tag)).set_watch(ev)
            return
        if kind not in ("danmu", "stats", "portrait", "coldline"):
            return
        rid = ev.get("room_id")
        if kind == "coldline":
            explicit_tag = str(ev.get("room_tag") or "").strip()
            if explicit_tag:
                # coldline 的 web_rid 可能与 stats 平台 room_id 不同。
                # 写入者已有明确 tag 时直接归属，不必等待画像/弹幕映射。
                with self.lock:
                    self.tags.setdefault(explicit_tag, TagState(explicit_tag)).set_cold(ev)
                    if rid:
                        self.room2tag.setdefault(rid, explicit_tag)
                        self.pending_cold.pop(rid, None)
                return
            # 旧记录没有 tag：先等 room→tag 映射，poll 末尾统一归位。
            if not rid:
                # ★ 2026-10-28：没有 room_id 的冷场行直接丢。离线 A/B 产物
                # （coldline_compare_*.jsonl）就是这种行，收下会变成 room_None 占位 tab。
                return
            with self.lock:
                self.pending_cold[rid] = ev
            return
        if rid and tag:
            with self.lock:
                self.room2tag.setdefault(rid, tag)
        st = self.tags.setdefault(tag, TagState(tag))
        if kind == "danmu":
            with self.lock:
                self._seq += 1
                seq = self._seq
            st.add_danmu(ev, seq)
            if ev.get("type") in ("danmaku", "gift"):
                with self.lock:
                    self._new_danmu.append({
                        "tag": tag, "seq": seq, "ts": ev.get("ts") or int(time.time()),
                        "uname": ev.get("uname") or "匿名", "text": ev.get("text") or "",
                        "gift": ev.get("type") == "gift",
                    })
        elif kind == "stats":
            st.add_stats(ev)
        elif kind == "portrait":
            st.set_portrait(ev)

    # ---- tail ----
    def poll(self):
        try:
            paths = self._scan_paths()
        except Exception as e:
            self.errors.append(f"扫描失败: {e}")
            return
        for path in sorted(paths):
            try:
                size = os.path.getsize(path)
            except OSError as e:
                self.errors.append(f"{os.path.basename(path)}: {e}")
                continue
            if path not in self.tailers:
                offset = 0
                if size > MAX_FILE_BYTES:        # 只关心当前场次，过大只看尾部
                    with open(path, "rb") as f:
                        f.seek(size - MAX_FILE_BYTES)
                        f.readline()
                        offset = f.tell()
                self.tailers[path] = offset
                self.bufs[path] = b""
            offset = self.tailers[path]
            if size < offset:                    # 文件被截断/轮换 → 跳到当前文件尾，避免重复计数
                offset = size
                self.bufs[path] = b""
                self.tailers[path] = size        # ★ 必须写回，否则下轮 offset 仍是旧值会吞新行
            if size > offset:
                try:
                    with open(path, "rb") as f:
                        f.seek(offset)
                        chunk = f.read(size - offset)
                except OSError as e:
                    self.errors.append(f"{os.path.basename(path)}: {e}")
                    continue
                self.tailers[path] = size
                lines = (self.bufs[path] + chunk).split(b"\n")
                self.bufs[path] = lines.pop()    # 残行留到下轮
                for raw in lines:
                    if raw.strip():
                        self._feed(path, raw)
        # pending 冷场事件归位：映射已就绪的进对应 tag，超过 1 小时仍未知的落占位 tag 兜底
        with self.lock:
            for rid, ev in list(self.pending_cold.items()):
                t = self.room2tag.get(rid)
                if t:
                    self.tags.setdefault(t, TagState(t)).set_cold(ev)
                    del self.pending_cold[rid]
                elif ev.get("ts") and time.time() - ev["ts"] > 3600:
                    self.tags.setdefault(f"room_{rid}", TagState(f"room_{rid}")).set_cold(ev)
                    del self.pending_cold[rid]

    def poll_loop(self):
        while True:
            try:
                self.poll()
            except Exception as e:
                self.errors.append(f"poll 异常: {e}")
            time.sleep(POLL_INTERVAL)

    def _live_roster(self):
        """A live view may legitimately have no enabled anchors. Never reseed the roster."""
        if self.roster_path is None:
            return None, None
        try:
            with open(self.roster_path, encoding="utf-8-sig") as f:
                roster = json.load(f)
            entries = roster.get("anchors") if isinstance(roster, dict) else None
            if not isinstance(entries, list):
                raise ValueError("名册缺少 anchors 列表")
            seen, enabled = set(), []
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("tag"), str):
                    raise ValueError("主播条目格式不正确")
                tag = entry["tag"].strip()
                active = entry.get("enabled", True)
                if not tag or tag in seen or not isinstance(active, bool):
                    raise ValueError("主播标签或启用状态不正确")
                seen.add(tag)
                if active:
                    enabled.append(tag)
            return enabled, None
        except (OSError, ValueError):
            return [], "主播名册读取失败，请检查 anchors.json 是否可读、格式是否正确。"

    def take_new_danmu(self):
        """取走本帧新增弹幕（广播器的 feed 通道）。"""
        with self.lock:
            if not self._new_danmu:
                return None
            items = list(self._new_danmu)
            self._new_danmu.clear()
        enabled, _ = self._live_roster()
        if enabled is not None:
            enabled = set(enabled)
            items = [item for item in items if item["tag"] in enabled]
        return {"items": items} if items else None

    def snapshot(self):
        now_ts = time.time()
        enabled, roster_error = self._live_roster()
        with self.lock:
            if enabled is None:
                tags = [st.snapshot(now_ts) for st in self.tags.values()]
            else:
                tags = [(self.tags.get(tag) or TagState(tag)).snapshot(now_ts) for tag in enabled]
        # 排序：开播的在前，再按最近活动
        tags.sort(key=lambda t: (not t["live"][0], -(t["last_ts"] or 0)))
        result = {"now_ts": now_ts, "date": self.today(), "data_dir": self.data_dir,
                  "tags": tags, "errors": list(self.errors), "version": BOARD_VERSION}
        if self.roster_path is not None:
            result["roster_error"] = roster_error
        return result


def _classify(filename):
    """返回 (kind, tag或None)。kind ∈ danmu/stats/portrait/coldline/watch/None。"""
    if filename.startswith("coldline_compare_"):
        # 离线 A/B 对比产物（compare_coldline.py），不是线上事件 → 不进看板
        return None, None
    if filename.startswith("coldline_") and filename.endswith(".jsonl"):
        return "coldline", None
    m = _RE_WATCH.match(filename)
    if m:
        return "watch", m.group(1)
    m = _RE_DATED.match(filename)
    if m:
        kind = {"danmu_douyin": "danmu", "stats_douyin": "stats",
                "portrait": "portrait"}[m.group(1)]
        return kind, m.group(2)
    return None, None


# ---------------- HTTP ----------------

class RequestBodyError(ValueError):
    """Fixed public errors; never include request bytes or parser exceptions."""
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)

def _roster_transaction(method):
    """所有名册写请求共用读改写锁，包含同请求中的运行时 LLM 闸门更新。"""
    @wraps(method)
    def serialized(self, *args, **kwargs):
        with roster_mod.transaction(getattr(self, "roster_path", None)):
            return method(self, *args, **kwargs)
    return serialized


class Handler(BaseHTTPRequestHandler):
    max_json_body = 64 * 1024
    max_rejection_drain = 256 * 1024
    body_read_timeout = 2.0
    rejection_drain_timeout = 2.0
    roster_path = None
    store = None
    broadcaster = None
    auth_db = None
    users_path = auth.DEFAULT_USERS_PATH
    config_path = os.path.join(REPO_ROOT, "config.json")
    throttle = None
    html_path = DEFAULT_HTML
    dist_dir = None
    started_at = 0.0

    server_version = "AssistBoard/" + BOARD_VERSION

    def log_message(self, fmt, *args):
        pass

    # ---- 基础输出 ----
    def _send(self, code, ctype, body, extra_headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _json(self, code, obj, extra_headers=None):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False), extra_headers)

    def _redirect(self, location):
        self._send(302, "text/plain; charset=utf-8", b"", {"Location": location})

    # ---- 鉴权 ----
    def _auth_user(self):
        return auth.user_from_headers(self.headers, self.auth_db)

    def _deny(self, path):
        # API 类路径（含历史 /snapshot、/sse）一律回 401 JSON，不重定向：
        # 前端和探活脚本要能区分「没权限」和「地址不存在」。
        if path.startswith("/api/") or path in ("/snapshot", "/sse"):
            if self.command in ("POST", "PATCH", "DELETE"):
                return self._reject_json(401, {"error": "auth required"})
            self._json(401, {"error": "auth required"})
        else:
            self._redirect("/login")

    def _csrf_ok(self):
        """写接口的 Origin 校验（SameSite=Lax 之外的第二道）。

        2026-10-22 修：只比**主机名**，不比端口。原因是反代链路下
        浏览器发 `Origin: https://weiguoshen.cn:8443`，而 nginx 转给后端的
        Host 头可能是 `weiguoshen.cn`（`$host` 不带端口），逐字比会误判
        403「bad origin」——老大登录时就撞上了。同端口差异在我们的部署里
        由 nginx 保证，跨站仍然会被 hostname 差异挡住。
        """
        origin = self.headers.get("Origin")
        if not origin:
            return True     # 非浏览器客户端（curl / 脚本）
        o = urlparse(origin)
        if not o.hostname:
            return False
        candidates = set()
        for raw in (self.headers.get("Host"),
                    self.headers.get("X-Forwarded-Host")):
            if raw:
                h = urlparse("//" + raw.split(",")[0].strip())
                if h.hostname:
                    candidates.add(h.hostname.lower())
        return o.hostname.lower() in candidates

    def _declared_length(self):
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) > 1 or self.headers.get("Transfer-Encoding"):
            raise RequestBodyError(400, "请求体长度或传输方式不受支持")
        raw = lengths[0].strip() if lengths else "0"
        if not re.fullmatch(r"[0-9]{1,12}", raw):
            raise RequestBodyError(400, "请求体长度无效")
        return int(raw)

    def _request_body(self):
        if hasattr(self, "_body_bytes"):
            return self._body_bytes
        n = self._declared_length()
        if self.headers.get("Expect"):
            raise RequestBodyError(417, "不支持此请求体预发送方式")
        if n > self.max_json_body:
            raise RequestBodyError(413, "请求体过大")
        self._body_consumed = 0
        parts = []
        deadline = time.monotonic() + self.body_read_timeout
        previous = self.connection.gettimeout()
        try:
            while self._body_consumed < n:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RequestBodyError(408, "读取请求体超时")
                self.connection.settimeout(remaining)
                chunk = self.rfile.read1(min(8192, n - self._body_consumed))
                if not chunk:
                    raise RequestBodyError(400, "请求体不完整")
                self._body_consumed += len(chunk)
                parts.append(chunk)
        except TimeoutError:
            raise RequestBodyError(408, "读取请求体超时") from None
        finally:
            self.connection.settimeout(previous)
        self._body_bytes = b"".join(parts)
        return self._body_bytes

    def _read_json(self):
        try:
            return json.loads(self._request_body().decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            if isinstance(exc, RequestBodyError):
                raise
            return None

    def _reject_json(self, code, obj):
        """Reply first, half-close writes, then drain bounded untrusted input.

        Authentication/CSRF rejection runs no JSON parser or business action.
        A client may still be sending its body when it receives the response;
        keeping reads open briefly prevents closing across that valid upload.
        Arbitrarily late or unbounded peer input is deliberately not awaited.
        """
        self.close_connection = True
        self._json(code, obj, {"Connection": "close"})
        try:
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
        except OSError:
            return
        try:
            target = max(0, self._declared_length() - getattr(self, "_body_consumed", 0))
        except RequestBodyError:
            target = self.max_rejection_drain
        target = min(target, self.max_rejection_drain)
        deadline = time.monotonic() + self.rejection_drain_timeout
        previous = self.connection.gettimeout()
        try:
            while target:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.connection.settimeout(remaining)
                chunk = self.rfile.read1(min(8192, target))
                if not chunk:
                    break
                target -= len(chunk)
        except OSError:
            pass
        finally:
            self.connection.settimeout(previous)

    # ---- 静态 ----
    def _dist_index(self):
        if self.dist_dir:
            p = os.path.join(self.dist_dir, "index.html")
            if os.path.isfile(p):
                return p
        return None

    def _serve_index(self):
        path = self._dist_index() or self.html_path
        try:
            with open(path, "rb") as f:
                body = f.read()
            self._send(200, "text/html; charset=utf-8", body)
        except OSError:
            self._send(500, "text/plain; charset=utf-8",
                       "前端入口缺失（web/dist/index.html 或 scripts/assist_board.html）")

    def _serve_legacy(self):
        """旧版单页（scripts/assist_board.html）。

        新前端上线后仍留一个月兜底：新页面出问题时同事能一键回旧页，
        而 SPA 回退会把任何未知路径都吃成新前端，所以要显式吐这个文件。
        """
        try:
            with open(self.html_path, "rb") as f:
                body = f.read()
        except OSError:
            return self._json(404, {"error": "旧版页面不存在"})
        self._send(200, "text/html; charset=utf-8", body)

    def _serve_static(self, path):
        root = os.path.abspath(self.dist_dir or "")
        target = os.path.abspath(os.path.join(root, path.lstrip("/")))
        if not root or not target.startswith(root + os.sep) or not os.path.isfile(target):
            return self._json(404, {"error": "not found"})
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        try:
            with open(target, "rb") as f:
                body = f.read()
        except OSError:
            return self._json(404, {"error": "not found"})
        # 构建产物文件名带 hash → 可长缓存；index.html 不走这条路径
        self._send(200, ctype, body,
                   {"Cache-Control": "public, max-age=31536000, immutable"})

    # ---- GET ----
    def do_GET(self):
        try:
            path = urlparse(self.path).path
            if path in ("/", "/index.html", "/login"):
                return self._serve_index()
            if path.startswith(STATIC_PREFIXES) or path == "/favicon.ico":
                return self._serve_static(path)
            if path == "/api/access":
                return self._json(200, {"mode": "account"})
            if path == "/api/health":
                return self._json(200, self._health())
            user = self._auth_user()
            if path not in PUBLIC_PATHS and user is None:
                return self._deny(path)
            if path == "/api/me":
                rec = auth.find_user(self.auth_db or {}, user) or {}
                return self._json(200, {"user": user,
                                        "role": rec.get("role", "member")})
            if path == "/api/review":
                return self._review()
            if path == "/api/review/dates":
                return self._json(200, {"dates": review.available_dates(self.store.data_dir)})
            if path == "/api/review/tags":
                q = self._query()
                d = q.get("date", "")
                tags = review.available_tags(self.store.data_dir, d)
                # 名册全量一起给：复盘下拉此前只列「当天有落盘」的 tag，没开播的
                # 主播直接不出现，看起来像「这个号没在监控」。2026-10-25 老大
                # 就是据此问「为什么复盘只有四个」——另外四路当天没开播。
                # 分成两组显示，才能区分「今天没播」和「压根没登记」。
                all_tags = []
                try:
                    all_tags = [a["tag"] for a in (self._roster().get("anchors") or [])
                                if a.get("tag") and a.get("enabled")]
                except Exception:  # noqa: BLE001 名册读不出来时退回旧行为
                    all_tags = list(tags)
                return self._json(200, {"tags": tags, "all_tags": all_tags})
            if path == "/api/review/backfill":
                return self._backfill_read()
            if path == "/api/anchors/health":
                return self._json(200, anchors_mgr.status_all(self._roster())["health"])
            if path == "/api/llm":
                # 只读状态（P5 写的部分还没做）。不抛异常：设置页不能因此白屏。
                return self._json(200, board_llm.read_status(
                    self.store.data_dir, self.config_path, self.store.today()))
            if path == "/api/llm/prices":
                return self._llm_prices()
            if path == "/api/llm/usage":
                return self._llm_usage()
            if path in ("/legacy", "/legacy/"):
                # P4 尾款：旧版单页兜底。方案书要求「保留一个月」，但新前端的
                # SPA 回退会把 /legacy 也吃成新页面 —— 所以这里显式吐旧 HTML。
                return self._serve_legacy()
            if path == "/api/anchors":
                data = anchors_mgr.status_all(self._roster())
                # 单主播 LLM 开关有两个面：名册 llm（下次拉起带不带 --llm）
                # 与闸门 tags_off（现在花不花钱）。两个都回给前端，
                # 免得出现「开关是开的但其实一直没调用」。
                try:
                    off = set(board_llm.read_tags_off(self.store.data_dir))
                except Exception:  # noqa: BLE001
                    off = set()
                for a in data.get("anchors") or []:
                    a["llm_gate_off"] = str(a.get("tag")) in off
                    # 2026-10-29：名册 llm=true 但进程是 --no-llm 起的（开机自启就是），
                    # 页面只显示名册意愿 → 实时页两栏永远空着却没人知道。这里把
                    # 进程真实状态一起回给前端，由前端在 LLM 列打提示。
                    a["process_llm"] = self._process_llm_flag(str(a.get("tag")))
                    a.update(board_llm.automatic_status(self.store.data_dir, self.config_path,
                                                       str(a.get("tag")), is_live=a.get("is_live")))
                return self._json(200, data)
            if path in ("/api/live", "/snapshot"):
                return self._json(200, self.store.snapshot())
            if path in ("/api/sse", "/sse"):
                return self._handle_sse()
            # SPA 路由回退：/review、/anchors 这类前端路由刷新时也要拿到 index.html，
            # 否则 F5 直接 404。API 类路径仍然严格 404。
            if path.startswith("/api/") or path in ("/snapshot", "/sse"):
                return self._json(404, {"error": "not found"})
            return self._serve_index()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            try:
                self._json(500, {"error": "服务处理失败，请稍后重试"})
            except Exception:
                pass

    # ---- POST ----
    def do_POST(self):
        try:
            path = urlparse(self.path).path
            if not self._csrf_ok():
                return self._reject_json(403, {"error": "bad origin"})
            if path != "/api/login" and self._auth_user() is None:
                return self._deny(path)
            if path == "/api/login":
                return self._login()
            if path == "/api/logout":
                return self._json(200, {"ok": True},
                                  {"Set-Cookie": auth.clear_cookie_header()})
            if path == "/api/anchors":
                return self._anchor_add()
            if path == "/api/anchors/start":
                return self._anchor_start()
            if path == "/api/anchors/stop":
                return self._anchor_stop()
            if path == "/api/anchors/restart":
                return self._anchor_restart()
            if path == "/api/passwd":
                return self._passwd()
            if path == "/api/llm/gate":
                return self._llm_gate()
            if path == "/api/llm/provider":
                return self._llm_provider()
            if path == "/api/llm/budget":
                return self._llm_budget()
            if path == "/api/llm/settings":
                return self._llm_model_settings("save")
            if path == "/api/llm/prices":
                return self._llm_prices(saving=True)
            if path == "/api/llm/key/delete":
                return self._llm_model_settings("delete")
            if path == "/api/llm/test":
                return self._llm_model_settings("test")
            if path == "/api/review/backfill":
                return self._backfill_start()
            return self._json(404, {"error": "not found"})
        except RequestBodyError as exc:
            return self._reject_json(exc.code, {"error": exc.message})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            try:
                self._json(500, {"error": "服务处理失败，请稍后重试"})
            except Exception:
                pass

    # ---- PATCH（改名册开关）----
    def do_PATCH(self):
        try:
            path = urlparse(self.path).path
            if not self._csrf_ok():
                return self._reject_json(403, {"error": "bad origin"})
            if self._auth_user() is None:
                return self._deny(path)
            if path != "/api/anchors":
                return self._json(404, {"error": "not found"})
            body = self._read_json()
            if not isinstance(body, dict):
                return self._json(400, {"error": "请求体不是 JSON"})
            return self._anchor_patch(body)
        except RequestBodyError as exc:
            return self._reject_json(exc.code, {"error": exc.message})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            try:
                self._json(500, {"error": "服务处理失败，请稍后重试"})
            except Exception:
                pass

    # ---- DELETE（删主播，要求二次确认）----
    def do_DELETE(self):
        try:
            path = urlparse(self.path).path
            if not self._csrf_ok():
                return self._reject_json(403, {"error": "bad origin"})
            if self._auth_user() is None:
                return self._deny(path)
            if path != "/api/anchors":
                return self._json(404, {"error": "not found"})
            q = self._query()
            tag = (q.get("tag") or "").strip()
            if not tag:
                return self._json(400, {"error": "缺少 tag"})
            if (q.get("confirm") or "") != tag:
                return self._json(400, {"error": "二次确认不一致：confirm 必须等于 tag"})
            return self._anchor_delete(q)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            try:
                self._json(500, {"error": "服务处理失败，请稍后重试"})
            except Exception:
                pass

    # ---- 业务 ----
    def _query(self):
        q = parse_qs(urlparse(self.path).query)
        return {k: v[0] for k, v in q.items() if v}

    # ---- 名册（P6）----
    def _roster(self):
        """每次都重新读盘：名册可能被另一个请求/launch_all 改过。"""
        roster, rebuilt = roster_mod.ensure(self.roster_path or roster_mod.ROSTER_PATH)
        if rebuilt:
            logging.getLogger(__name__).warning("主播名册缺失或损坏，已使用种子重建，请核对主播配置")
        return roster

    def _save_roster(self, roster):
        roster_mod.save(roster, self.roster_path or roster_mod.ROSTER_PATH)

    @_roster_transaction
    def _anchor_patch(self, body):
        roster, entry, sent = self._anchor_entry(self._query())
        if sent:
            return
        if "llm" in body and type(body["llm"]) is not bool:
            return self._json(400, {"error": "自动实况分析开关必须是布尔值"})
        for k in ("watch", "record", "llm"):
            if k in body:
                entry[k] = bool(body[k])
        errs = roster_mod.validate(entry)
        if errs:
            return self._json(400, {"error": "；".join(errs)})
        gate = {}
        # 关闭先落运行时闸门；名册保存失败也不能继续花钱。
        # 开启反过来，名册保存成功后才放开闸门。
        if "llm" in body and not entry["llm"]:
            gate = board_llm.write_gate(self.store.data_dir, tag_off=(entry["tag"], True),
                                        actor=self._auth_user() or "board")
        self._save_roster(roster)
        if "llm" in body and entry["llm"]:
            gate = board_llm.write_gate(self.store.data_dir, tag_off=(entry["tag"], False),
                                        actor=self._auth_user() or "board")
        auto = {}
        if body.get("watch") is False:
            auto["watch"] = anchors_mgr.stop("watch", entry["tag"])
        if body.get("record") is False:
            auto["record"] = anchors_mgr.stop("record", entry["tag"])
        stop_failed = any(not result.get("ok") for result in auto.values())
        resp = {"ok": not stop_failed, "anchor": entry, "auto_stopped": auto}
        if stop_failed:
            resp.update({"state": "partial", "error": "配置已保存，但进程停止失败，请处理停止结果",
                         "note": "已保存配置与当前 LLM 闸门保持生效；仍运行的进程需要后续处理"})
        if "llm" in body:
            resp["tags_off"] = gate.get("tags_off") or []
            resp["process_llm"] = self._process_llm_flag(entry["tag"])
            resp.update(board_llm.automatic_status(self.store.data_dir, self.config_path, entry["tag"]))
            resp["hint"] = ("自动实况分析已允许；仍需总控制允许、有效密钥和运行中的采集。"
                            if entry["llm"] else "自动实况分析已关闭；未发送的自动调用停止，基础采集与人工分析不受此开关影响。")
        return self._json(409 if stop_failed else 200, resp)

    @_roster_transaction
    def _anchor_delete(self, q):
        roster, entry, sent = self._anchor_entry(q)
        if sent:
            return
        tag = entry["tag"]
        try:
            removed = roster_mod.remove(roster, tag)
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        board_llm.write_gate(self.store.data_dir, tag_off=(tag, True),
                             actor=self._auth_user() or "board")
        stopped = anchors_mgr.stop_anchor(tag, entry)
        if any(not result.get("ok") for result in stopped.values()):
            return self._json(409, {"ok": False, "state": "stop_failed",
                                    "error": "进程停止失败，未删除名册条目",
                                    "stopped": stopped,
                                    "note": "该路 LLM 闸门已关闭；处理停止结果后可重试删除"})
        self._save_roster(roster)
        return self._json(200, {"ok": True, "removed": removed, "stopped": stopped,
                                "note": "历史 jsonl 保留在 data/ 下，未删除"})

    def _anchor_entry(self, q):
        """返回 (roster, entry, sent)。sent=True 表示已经回过响应，调用方直接 return。"""
        tag = (q.get("tag") or "").strip()
        if not tag:
            self._json(400, {"error": "缺少 tag"})
            return None, None, True
        roster = self._roster()
        entry = roster_mod.find(roster, tag)
        if not entry:
            self._json(404, {"error": "名册里没有：%s" % tag})
            return None, None, True
        return roster, entry, False

    def _process_llm_flag(self, tag):
        """这一路的采集进程是以 --llm 起的吗？（True/False，查不到给 None）

        读 `data/watch_<tag>.config.json` 的 rooms[0].llm —— watch_anchor_live
        每次开播前用 --llm 的值重写这个副本，所以它就是「进程实际带了什么参数」。
        """
        p = os.path.join(self.store.data_dir, "watch_%s.config.json" % tag)
        try:
            with open(p, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            return None
        rooms = cfg.get("rooms") or []
        if rooms and isinstance(rooms[0], dict):
            return bool(rooms[0].get("llm"))
        return None

    @_roster_transaction
    def _anchor_add(self):
        body = self._read_json()
        if not isinstance(body, dict):
            return self._json(400, {"error": "请求体不是 JSON"})
        if "llm" in body and type(body["llm"]) is not bool:
            return self._json(400, {"error": "自动实况分析开关必须是布尔值"})
        roster = self._roster()
        try:
            entry = roster_mod.upsert(roster, body)
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        if not entry["llm"]:
            board_llm.write_gate(self.store.data_dir, tag_off=(entry["tag"], True),
                                 actor=self._auth_user() or "board")
        self._save_roster(roster)
        if entry["llm"]:
            board_llm.write_gate(self.store.data_dir, tag_off=(entry["tag"], False),
                                 actor=self._auth_user() or "board")
        return self._json(200, {"ok": True, "anchor": entry,
                                "note": ("已入名册，将按已启用的功能运行。" if getattr(anchors_mgr, "_CUSTOMER_CONTROLLER", None) is not None
                                         else "已入名册，点「启动」才真正拉起采集")})

    @_roster_transaction
    def _anchor_start(self):
        q = self._query()
        roster, entry, sent = self._anchor_entry(q)
        if sent:
            return
        kind = q.get("kind", "all")
        if kind == "watch" and not entry.get("watch"):
            return self._json(400, {"error": "名册里这一路的 watch 已关闭，先打开 watch"})
        if kind == "record" and not entry.get("record"):
            return self._json(400, {"error": "名册里这一路的 record 已关闭，先打开 record"})
        kinds = ("watch", "record") if kind == "all" else (kind,)
        if kind == "all":
            entry["enabled"] = True       # 先写意图，再起进程
            self._save_roster(roster)
        res = anchors_mgr.start_anchor(entry, kinds)
        return self._json(200, {"ok": all(v.get("ok") for v in res.values()),
                                "result": res, "enabled": entry.get("enabled")})

    @_roster_transaction
    def _anchor_stop(self):
        q = self._query()
        roster, entry, sent = self._anchor_entry(q)
        if sent:
            return
        kind = q.get("kind", "all")
        persist = (q.get("persist") or "").lower() in ("1", "true", "yes")
        kinds = ("watch", "record") if kind == "all" else (kind,)
        # ★ D6：整路停用必须先写 enabled=false，否则开机自启又把它拉回来
        if persist or kind == "all":
            entry["enabled"] = False
            self._save_roster(roster)
        res = anchors_mgr.stop_anchor(entry["tag"], entry, kinds)
        return self._json(200, {"ok": all(v.get("ok") for v in res.values()),
                                "result": res, "enabled": entry.get("enabled")})

    @_roster_transaction
    def _anchor_restart(self):
        """重启一路：先杀进程再拉起，**不动 enabled**（重启 ≠ 停用）。"""
        q = self._query()
        roster, entry, sent = self._anchor_entry(q)
        if sent:
            return
        res = anchors_mgr.restart_anchor(entry["tag"], entry, ("watch", "record"))
        started = res.get("started") or {}
        stopped = res.get("stopped") or {}
        ok = (res.get("ok", True) and not any(not v.get("ok") for v in stopped.values())
              and bool(started) and all(v.get("ok") for v in started.values()))
        return self._json(200 if ok else 409, {"ok": bool(ok),
                                "result": res, "enabled": entry.get("enabled")})

    def _passwd(self):
        """改密码：验当前密码 → 写新哈希 → 热更新内存用户库。

        无状态 token 不会因改密码自动失效（签名密钥没变），所以返回里提示
        「建议退出重登」；真要强制全体掉线得换 secret，那是另一回事。
        """
        user = self._auth_user()
        body = self._read_json()
        if not isinstance(body, dict):
            return self._json(400, {"error": "请求体不是 JSON"})
        old = body.get("old") or ""
        new = (body.get("new") or "").strip()
        if len(new) < 6:
            return self._json(400, {"error": "新密码至少 6 位"})
        if not auth.check_login(self.auth_db or {}, user, old):
            return self._json(400, {"error": "当前密码不对"})
        path = getattr(type(self), "users_path", auth.DEFAULT_USERS_PATH)
        try:
            auth.set_password(path, user, new)
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        newdb = auth.load_db(path)
        if newdb:                       # 热更新，否则旧密码还能登到进程重启
            type(self).auth_db = newdb
            self.auth_db = newdb
        return self._json(200, {"ok": True, "note": "已修改，建议退出后重新登录"})

    def _review(self):
        """复盘：单日单主播的历史聚合（带 mtime 缓存，见 board_review）。"""
        q = self._query()
        date = (q.get("date") or self.store.today()).strip()
        tag = (q.get("tag") or "").strip()
        if not _RE_DATE.match(date):
            return self._json(400, {"error": "date 格式应为 YYYY-MM-DD"})
        if not tag:
            return self._json(400, {"error": "缺少 tag"})
        try:
            data = review.review_tag(self.store.data_dir, date, tag)
        except Exception as e:
            return self._json(500, {"error": "review failed", "detail": repr(e)})
        # Cached aggregates stay private/raw; each response uses fresh secrets.
        data = {**data, "llm": self._public_llm_usage(data["llm"])}
        return self._json(200, data)

    # ---- 按需补画像（2026-10-25）----
    # 老大的原始设想：LLM 是「根据弹幕提炼」的，不必实时跑，需要时再提炼。
    # 那就要有一个入口，在复盘页上点一下，对某天某主播重放弹幕补一份画像。
    # 复用 scripts/portrait_backfill.py（与实时版同构：同一 analyzer 快照、
    # 同一 PortraitService），这里只负责起进程 + 报进度。
    def _backfill_store(self):
        return TaskStore(config_path=self.config_path, data_dir=self.store.data_dir,
                         python_exe=sys.executable)

    def _backfill_error(self, error):
        code = getattr(error, "code", "task_state_unavailable")
        status = 404 if code == "task_not_found" else 400 if code.startswith("invalid_") else 503
        return self._json(status, {"ok": False, "code": code,
                                  "error": "任务参数无效。" if status == 400 else
                                  "任务状态待确认，请只读重新查询；不要重复提交。"})

    def _backfill_read(self):
        q = self._query()
        try:
            task = self._backfill_store().status(
                task_id=q.get("task_id") or None, tag=q.get("tag") or None,
                date=q.get("date") or None, request_id=q.get("request_id") or None)
            return self._json(200, task)
        except TaskError as exc:
            return self._backfill_error(exc)

    def _backfill_start(self):
        body = self._read_json()
        if (not isinstance(body, dict) or set(body) - {"tag", "date", "force", "request_id"}
                or not isinstance(body.get("tag"), str)
                or not isinstance(body.get("date"), str)
                or type(body.get("force", False)) is not bool
                or not isinstance(body.get("request_id"), str)):
            return self._json(400, {"ok": False, "code": "invalid_request",
                                    "error": "任务参数无效。"})
        try:
            response = self._backfill_store().submit(
                tag=body["tag"], date=body["date"], force=body.get("force", False),
                request_id=body["request_id"])
            return self._json(200, response)
        except TaskError as exc:
            return self._backfill_error(exc)

    def _health(self):
        return {"ok": True, "version": BOARD_VERSION,
                "uptime": int(time.time() - (self.started_at or time.time())),
                "subscribers": self.broadcaster.subscribers if self.broadcaster else 0}

    # ---- P5：LLM 运行时闸门（写）----
    def _llm_model_settings(self, action):
        body = self._read_json()
        if not isinstance(body, dict):
            return self._reject_json(400, {"error": "请求体必须是 JSON 对象"})
        funcs = {"save": board_llm.save_provider_config,
                 "delete": board_llm.delete_provider_key,
                 "test": board_llm.test_provider_connection}
        result = funcs[action](self.config_path, self.store.data_dir, body)
        if action == "test":
            code = 200  # An upstream 401 must not log the user out of the board.
        elif result.get("code") == "invalid":
            code = 400
        else:
            code = 200 if result.get("ok") else 503
        return self._json(code, result)

    def _llm_gate(self):
        """开关 LLM。只写运行时闸门（秒生效），不重启任何进程。"""
        body = self._read_json()
        if not isinstance(body, dict) or type(body.get("enabled")) is not bool:
            return self._json(400, {"error": "enabled必须是布尔值"})
        enabled = body["enabled"]
        board_llm.write_gate(self.store.data_dir, enabled=enabled,
                             actor=self._auth_user() or "board")
        st = self._llm_status()
        st["hint"] = ("已允许新的内置AI调用；各主播自动实况仍需单独开启，等待采集和有效密钥。"
                      if enabled else "内置AI调用已暂停；未发送请求停止，已发HTTP可收尾，基础采集不受影响。")
        if enabled and not st.get("control", {}).get("allowed"):
            st["hint"] = "许可意图已保存，但控制状态暂不可确认；模型请求仍会阻断。"
        return self._json(200, st)

    def _llm_provider(self):
        """切 active_provider。同样只写闸门，调用链下次调用即走新 provider。"""
        body = self._read_json()
        name = str((body or {}).get("provider") or "").strip() if isinstance(body, dict) else ""
        st = self._llm_status()
        names = [p["name"] for p in st.get("providers") or []]
        if name not in names:
            return self._json(400, {"error": "没有这个提供商，请从已配置列表选择。",
                                    "providers": names})
        board_llm.write_gate(self.store.data_dir, active_provider=name,
                             actor=self._auth_user() or "board")
        st = self._llm_status()
        st["hint"] = "已切到 %s：下一次调用就生效。" % name
        return self._json(200, st)

    def _llm_status(self):
        return board_llm.read_status(self.store.data_dir, self.config_path,
                                     self.store.today())

    def _llm_prices(self, saving=False):
        try:
            value = (llm_pricing.save_prices(self.config_path, self._read_json()) if saving
                     else llm_pricing.read_prices(self.config_path))
            return self._json(200, value)
        except llm_pricing.PriceError as error:
            status = 400 if error.code == "invalid_price" else 503
            return self._json(status, {"ok": False, "code": error.code,
                "config_saved": None if error.code == "price_storage_error" else False,
                "error": "估算单价无效或状态待确认，请重新读取核对。"})

    def _public_llm_usage(self, value):
        secrets = board_llm._provider_secrets(board_llm._load_json(self.config_path))
        secrets = board_llm._remember_provider_secrets(self.config_path, secrets)
        return board_llm._public_status(value, secrets)

    def _llm_usage(self):
        from datetime import date as CalendarDate
        import board_usage
        q = self._query()
        try:
            if set(q) - {"call_date", "data_date", "tag", "task_id", "purpose"}:
                raise ValueError
            for key in ("call_date", "data_date"):
                if q.get(key) and CalendarDate.fromisoformat(q[key]).isoformat() != q[key]:
                    raise ValueError
            if q.get("task_id") and not re.fullmatch(r"bf_[0-9a-f]{64}_[0-9a-f]{32}", q["task_id"]):
                raise ValueError
            if q.get("tag") and (len(q["tag"]) > 100 or any(ord(c) < 32 for c in q["tag"])):
                raise ValueError
            if q.get("purpose") and not re.fullmatch(r"[a-z_]{1,64}", q["purpose"]):
                raise ValueError
            call_date = q.get("call_date") or (None if any(q.get(k) for k in ("tag", "task_id", "data_date"))
                                               else self.store.today())
            value = board_usage.read_usage(self.store.data_dir, call_date,
                tag=q.get("tag") or None, task_id=q.get("task_id") or None,
                purpose=q.get("purpose") or None, data_date=q.get("data_date") or None)
            return self._json(200, self._public_llm_usage(value))
        except ValueError:
            return self._json(400, {"ok": False, "error": "用量查询参数无效。"})

    def _llm_budget(self):
        """改日 token 护栏（2026-10-25，老大要求前端可改）。

        写 config.json + 同步全部 watch 副本。**这是启动期配置**：账本进程启动时
        读一次，改完要重启该路 watch 才按新值算；但闸门/界面上显示的数字立刻更新。
        """
        body = self._read_json()
        if not isinstance(body, dict):
            return self._json(400, {"error": "空请求"})
        n = body.get("cap_tokens", body.get("budget"))
        if type(n) is not int or not 0 <= n <= 2**53 - 1:
            return self._json(400, {"error": "cap_tokens 必须是非负安全整数（0 = 不设限）"})
        r = board_llm.sync_llm_config(self.config_path, self.store.data_dir,
                                      budget=n)
        st = self._llm_status()
        st["hint"] = ("日护栏已改为 %s tok，同步 %d 份 watch 副本。"
                      "账本在进程启动时读一次 —— 想立刻按新值算，重启对应 watch。"
                      % (r["budget"], r["count"]))
        return self._json(200, st)

    def _login(self):
        ip = self.client_address[0] if self.client_address else "?"
        if self.throttle and self.throttle.blocked(ip):
            return self._json(429, {"error": "尝试过于频繁，请 1 分钟后再试"})
        body = self._read_json()
        if not isinstance(body, dict):
            return self._json(400, {"error": "请求体不是 JSON"})
        name = (body.get("username") or "admin").strip() or "admin"
        pwd = body.get("password") or ""
        if not isinstance(pwd, str) or not pwd:
            return self._json(400, {"error": "缺少密码"})
        if not auth.check_login(self.auth_db or {}, name, pwd):
            if self.throttle:
                self.throttle.fail(ip)
            return self._json(401, {"error": "用户名或密码不对"})
        if self.throttle:
            self.throttle.reset(ip)
        token = auth.make_token(name, self.auth_db["secret"])
        rec = auth.find_user(self.auth_db, name) or {}
        self._json(200, {"ok": True, "user": name, "role": rec.get("role", "member")},
                   {"Set-Cookie": auth.cookie_header(token)})

    def _handle_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")   # nginx 反代必须，否则流被缓冲
        self.end_headers()
        self.close_connection = True

        def send(payload: bytes):
            self.wfile.write(payload)
            self.wfile.flush()

        try:
            broadcast.stream_to(send, self.broadcaster)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--bind", default=os.environ.get("BOARD_BIND", "127.0.0.1"),
                    help="监听地址：本机 127.0.0.1；走 frp 穿透时改 0.0.0.0")
    ap.add_argument("--data", default=DEFAULT_DATA)
    ap.add_argument("--date", default="", help="回放日期 YYYY-MM-DD；留空=跟随今天（跨天自动切）")
    ap.add_argument("--html", default=DEFAULT_HTML)
    ap.add_argument("--dist", default=DEFAULT_DIST, help="前端构建产物目录（存在则优先）")
    ap.add_argument("--users", default=auth.DEFAULT_USERS_PATH)
    args = ap.parse_args()

    store = Store(args.data, args.date or None, roster_path=None if args.date else roster_mod.ROSTER_PATH)
    # ★ 不要在这里同步 store.poll()（2026-10-22 现场踩过）：
    # 首扫要读当日所有 jsonl（研途单日 1.3 万行），实测把启动拖到 3s+，
    # 而看护器探活超时正好 3s → 判死 → 重复拉起 → 多个实例抢同一端口
    # （Windows 的 SO_REUSEADDR 允许重复绑）→ 互相拖慢 → 雪崩。
    # 先起服务，让 poll_loop 在后台把数据灌进来（1s 内就能出第一帧）。

    db, plain = auth.ensure_db(args.users)
    if plain:
        print("=" * 60)
        print("  首次启动，已生成登录账号")
        print("  用户名: admin")
        print("  密码  : %s" % plain)
        print("  同时写入 data/board_initial_password.txt")
        print("  改密码: python scripts/board_auth.py passwd admin")
        print("=" * 60)

    bc = broadcast.Broadcaster(store.snapshot, interval=SSE_INTERVAL,
                               ping_every=PING_EVERY,
                               feed_fn=store.take_new_danmu)
    bc.start()
    threading.Thread(target=store.poll_loop, daemon=True).start()

    # 采集异常兜底告警（飞书 dry_run/策略拒 → 至少落到桌面，别再一次 7 小时没人知道）
    def _alert_loop():
        while True:
            try:
                board_alert.check_and_alert(roster_mod.ensure()[0])
            except Exception:
                pass
            time.sleep(300)

    threading.Thread(target=_alert_loop, daemon=True, name="board-alert").start()

    Handler.store = store
    Handler.broadcaster = bc
    Handler.auth_db = db
    Handler.users_path = args.users
    Handler.throttle = auth.LoginThrottle()
    Handler.html_path = args.html
    Handler.dist_dir = args.dist
    Handler.started_at = time.time()

    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    use_dist = os.path.isfile(os.path.join(args.dist, "index.html"))
    print("[直播辅助看板] http://%s:%d  数据源=%s  日期=%s  前端=%s"
          % (args.bind, args.port, args.data, store.today(),
             "dist" if use_dist else "assist_board.html"))
    print("  已载入 %d 个 tag  已注册 %d 个用户"
          % (len(store.tags), len(db.get("users") or [])))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
