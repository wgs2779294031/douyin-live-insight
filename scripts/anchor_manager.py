# -*- coding: utf-8 -*-
"""主播实例管理：状态查询 + 热启停（P6）。

职责边界（很重要）
------------------
本模块管「**用户要不要这一路**」（名册 enabled + 进程在不在）。
不管「这一路内部健不健康」—— 那是 `run_watchdog.py` 自己的 30s 体检。

判活纪律
--------
pidfile + `pid_alive()`（ctypes OpenProcess）。**禁止进程枚举**（tasklist/WMI
在同一时刻给过 0/4/28 三种结果）；**绝不用 `os.kill(pid, 0)`**（Windows 上会
真的杀进程）。

并发
----
全局一把锁串行化所有启停；操作前重新读 pidfile（不用进入时的快照），
start 幂等（已在跑就返回 already_running，不报错）—— 前端连点 5 次只会起 1 个。
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PY = ROOT / ".venv" / "Scripts" / "python.exe"

# 录音为什么不能用 PY（2026-10-25 定位）
# --------------------------------------
# stream_recorder 拉流要 curl_cffi，转写要 av + faster_whisper。这三个包**只装在
# Private deployment example omitted; configure this environment explicitly.
# P6 把 record 也接到 PY 之后，每次开播都在 build_session() 里 ModuleNotFoundError，
# 异常又被 finally 吞成 stop_reason=unknown → meta 写一条 0 分片 session、进程退出、
# 计划任务 10 分钟后再拉起 —— 一整天"以为在录，其实 0 字节"（10-25 已发生 10 次）。
# 所以 record 单独探测解释器：谁能 import curl_cffi 用谁。
RECORD_PY_CANDIDATES = [
    os.environ.get("WB_RECORD_PY"),
    sys.executable,
    str(PY),
]
_record_py_cache: str | None = None

WATCHDOG_SCRIPT = "scripts\\run_watchdog.py"
RECORDER_SCRIPT = "scripts\\stream_recorder.py"

WATCH_STALE_SEC = 15 * 60      # watch 探测停写超过这么久 = 采集挂了
DANMU_STALE_SEC = 10 * 60      # 在播但弹幕停这么久 = 采集异常（黄灯）
RECORD_STALE_SEC = 3 * 60      # 默认 60 秒分片，连续三片时长无写入算异常
RECORD_START_GRACE_SEC = 3 * 60
START_SETTLE = 1.2             # 拉起后等 pidfile 落盘的宽限
CDP_URL = "http://127.0.0.1:9222/json/version"
CDP_WAIT_SEC = 12.0            # 起 watch 前等浏览器就绪的上限（见 _wait_cdp）


def _cdp_up(timeout: float = 0.8) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(CDP_URL, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _wait_cdp(limit: float = CDP_WAIT_SEC) -> bool:
    """起 watch 前先等 9222 就绪（2026-10-23 踩过）。

    踩的坑：批量重启 8 路时，8 个 run_watchdog 同时发现 CDP 不通 → 各自去拉
    同一个 profile 的 Chrome → 互相抢，15:42 全线停摆 4 分钟。
    串行「谁先起谁拉浏览器、其余先等」就不会打群架：这里最多等 12s，
    超时也照常拉起（宁可让它自己再试，也不要卡死调用方）。
    """
    deadline = time.time() + max(0.0, limit)
    while time.time() < deadline:
        if _cdp_up():
            return True
        time.sleep(1.0)
    return False


def _any_watch_running() -> bool:
    """有没有别的 watch 在跑（有 = 它会把浏览器拉起来，我们只管等）。"""
    for f in DATA.glob("watchdog_*.pid"):
        try:
            pid = int(f.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        if pid_alive(pid):
            return True
    return False


_LOCK = threading.RLock()
_CUSTOMER_CONTROLLER = None


def configure_customer(controller):
    """Bind this Board's actions to its owning local application only."""
    global _CUSTOMER_CONTROLLER
    _CUSTOMER_CONTROLLER = controller


# ---------------- 底层 ----------------

def _flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def pid_alive(pid: int) -> bool:
    """⚠️ 不能用 os.kill(pid, 0)：Windows 上 os.kill 对 0 号信号会直接杀进程。"""
    if pid <= 0:
        return False
    k32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == 259      # STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def process_create_time(pid: int) -> float | None:
    """取进程创建时间（unix 秒）；拿不到返回 None。"""
    if pid <= 0:
        return None
    k32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        created, exited, kernel, user = (ctypes.c_ulonglong(0) for _ in range(4))
        if not k32.GetProcessTimes(h, ctypes.byref(created), ctypes.byref(exited),
                                   ctypes.byref(kernel), ctypes.byref(user)):
            return None
        # FILETIME：自 1601-01-01 起的 100ns 计数
        return created.value / 1e7 - 11644473600.0
    except Exception:
        return None
    finally:
        k32.CloseHandle(h)


def pid_owns_pidfile(pid: int, pf: Path, skew: float = 5.0,
                     max_gap: float = 300.0, *, strict: bool = False) -> bool:
    """判断「pid 活着」是不是**当初写这个 pidfile 的那个进程**。

    为什么需要：Windows 会复用 PID。2026-10-25 实测 4 路录音里 2 路的 pid 被
    系统回收给了 WorkBuddy.exe 和 editor_sdk.exe —— 只看 pid 存活就会一直判
    「已在跑」，死掉的录音永远不会被拉起（日志停在两小时前，页面还显示正常）。

    判据：进程创建时间不晚于 pidfile 写入时间太多（复用者必然是更晚启动的），
    且写入时间距创建不超过 max_gap（pidfile 是进程启动时立刻写的）。
    strict 用于停止等破坏性操作：不容许晚于 pidfile，身份读取失败则抛错拒绝操作。
    """
    if not pid or not pid_alive(pid):
        return False
    ct = process_create_time(pid)
    if ct is None:
        if strict:
            raise RuntimeError("无法核实进程创建时间，拒绝终止该 PID")
        return True                      # 拿不到创建时间就退回旧行为（宁可误判为活着）
    try:
        mt = pf.stat().st_mtime
    except OSError:
        if strict:
            raise RuntimeError("无法核实 pidfile 时间，拒绝终止该 PID")
        return True
    # 只读判活保留既有时间容差；破坏性停止不能把新于 pidfile 的进程当成原实例。
    if ct > mt + (0.0 if strict else skew):
        return False                     # 进程比 pidfile 新 → 不是它（pid 被复用）
    if mt - ct > max_gap:
        return False                     # 进程比 pidfile 老太多 → 也不是它
    return True


def pidfile(kind: str, tag: str) -> Path:
    """watch -> watchdog_<tag>.pid；record -> stream_<tag>.pid（沿用既有命名）。"""
    name = "watchdog_%s.pid" % tag if kind == "watch" else "stream_%s.pid" % tag
    return DATA / name


def read_pid(kind: str, tag: str) -> int:
    try:
        return int(pidfile(kind, tag).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _clear_pidfile(kind: str, tag: str) -> None:
    try:
        pidfile(kind, tag).unlink(missing_ok=True)
    except OSError:
        pass


def _file_age(path: Path):
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def _run_hidden(cmd) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", creationflags=_flags())


# ---------------- 状态 ----------------

def instance_status(kind: str, tag: str) -> dict:
    if _CUSTOMER_CONTROLLER is not None:
        return _CUSTOMER_CONTROLLER.instance_status(kind, tag)
    pid = read_pid(kind, tag)
    pf = pidfile(kind, tag)
    # 不能只看 pid 存活：Windows 会复用 PID（2026-10-25 两路录音的 pid 被
    # WorkBuddy / editor_sdk 占用，导致「死掉的进程」永远显示正常、也不会被重启）
    alive = bool(pid) and pid_owns_pidfile(pid, pf)
    stale = pid if (pid and not alive and pid_alive(pid)) else 0
    return {"kind": kind, "pid": pid if alive else 0, "running": alive,
            "stale_pid": stale}


def _watch_session_start(tag: str, session_id: str, stamp: float,
                         web_rid: str = "") -> float | None:
    """有界恢复当前连续场次首次观察时间，不用每轮新 ts 重置宽限。"""
    from watch_anchor_live import is_confirmed_offline
    try:
        with open(DATA / ("watch_%s.jsonl" % tag), "rb") as fh:
            fh.seek(0, os.SEEK_END)
            offset = max(0, fh.tell() - 65536)
            fh.seek(offset)
            raw = fh.read(65536).decode("utf-8", "replace")
    except OSError:
        return None
    lines = raw.splitlines()
    if offset:
        lines = lines[1:]  # 尾窗第一行可能被截断，不能据此证明场次边界。
    earliest, boundary = None, offset == 0
    for line in reversed(lines):
        try:
            row = json.loads(line)
            if not isinstance(row, dict) or row.get("error"):
                continue
            ts = row.get("ts")
            if not _valid_progress_time(ts, stamp):
                continue
            sid = str(row.get("room_id_str") or row.get("room_id") or "")
            room_matches = not web_rid or str(row.get("web_rid") or "") == web_rid
            if row.get("is_live") is True and sid == session_id and room_matches:
                earliest = min(earliest or ts, ts)
            elif is_confirmed_offline(row) or (row.get("is_live") is True and sid):
                boundary = True
                break
        except (ValueError, TypeError):
            continue
    # 未见边界且尾窗不足一个宽限周期时，只能说证据不足，不能滑窗永远 starting。
    if earliest is not None and (boundary or stamp - earliest > RECORD_START_GRACE_SEC):
        return earliest
    return None


def _valid_progress_time(value, stamp):
    return type(value) in (int, float) and math.isfinite(value) and 0 < value <= stamp


def _record_audio_progress(tag, instance, watch, stamp):
    """Read only a current worker's progress; old PID/session receipts cannot keep it green."""
    path = DATA / ("stream_%s.progress.json" % tag)
    try:
        with path.open("rb") as fh:
            raw = fh.read(16385)
        if len(raw) > 16384:
            return None
        progress = json.loads(raw)
        created = process_create_time(instance["pid"])
        if (progress.get("schema") != 1 or progress.get("pid") != instance["pid"]
                or created is None
                or not _valid_progress_time(progress.get("process_created_at"), stamp)
                or abs(progress["process_created_at"] - created) > 0.001
                or not _valid_progress_time(progress.get("updated_at"), stamp)
                or progress["updated_at"] < created):
            return None
        session_id = str(watch.get("room_id_str") or watch.get("room_id") or "")
        last_audio = progress.get("last_valid_audio_at")
        if last_audio is not None and (
                not _valid_progress_time(last_audio, progress["updated_at"]) or last_audio < created):
            return None
        packets = progress.get("valid_audio_packets", 0)
        if type(packets) is not int or packets < 0 or (last_audio is not None and packets == 0):
            return None
        initializing = (progress.get("phase") == "starting" and not progress.get("web_rid")
                        and not progress.get("session_id") and last_audio is None and packets == 0)
        if not initializing and (
                str(progress.get("web_rid") or "") != str(watch.get("web_rid") or "")
                or (session_id and str(progress.get("session_id") or "") != session_id)):
            # This is an identified worker for another scope, not an old recorder
            # without receipts. Its newly-created files cannot prove this live room.
            return {"last_audio": None, "created": created, "invalid_audio": False,
                    "scope_mismatch": True}
        return {"last_audio": last_audio, "created": created,
                "invalid_audio": bool(progress.get("parser_error"))}
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return None


def _flv_has_recent_aac(path):
    """Bounded legacy check: AAC setup at the front and complete recent tags at EOF.

    Read headers and short AAC prefixes only, skipping media payloads. An unfinished
    current fragment can fall back to the previous fragment. This checks packet
    structure, not decoded speech or loudness; encoded silence remains audio.
    """
    try:
        with path.open("rb", buffering=0) as fh:
            size = os.fstat(fh.fileno()).st_size
            header = fh.read(9)
            if len(header) != 9 or header[:4] != b"FLV\x01" or header[4] & ~5:
                return False
            offset = int.from_bytes(header[5:9], "big")
            if not 9 <= offset <= 4096:
                return False
            fh.seek(offset)
            if fh.read(4) != b"\0\0\0\0":
                return False
            first_tag = offset + 4

            def tag_at(position):
                fh.seek(position)
                head = fh.read(11)
                if len(head) != 11 or head[0] not in (8, 9, 18) or head[8:11] != b"\0\0\0":
                    return None
                length = int.from_bytes(head[1:4], "big")
                end = position + 11 + length + 4
                if end > size:
                    return None
                prefix = fh.read(min(length, 8))
                fh.seek(end - 4)
                if int.from_bytes(fh.read(4), "big") != length + 11:
                    return None
                timestamp = int.from_bytes(head[4:7], "big") | head[7] << 24
                return head[0], prefix, length, end, timestamp

            position = first_tag
            configured = False
            first_audio = False
            for _ in range(32):
                tag = tag_at(position)
                if tag is None:
                    return False
                kind, prefix, length, position, _ = tag
                if kind == 8:
                    if len(prefix) < 2 or prefix[0] >> 4 != 10:
                        return False
                    if prefix[1] == 0:
                        if len(prefix) < 4:
                            return False
                        audio_type = prefix[2] >> 3
                        frequency = ((prefix[2] & 7) << 1) | (prefix[3] >> 7)
                        channels = (prefix[3] >> 3) & 15
                        configured = (audio_type not in (0, 31) and frequency <= 12 and channels <= 7)
                        if not configured:
                            return False
                    elif prefix[1] == 1 and length > 2 and configured:
                        first_audio = True
                        break
                    else:
                        return False
                if position >= size:
                    break
            if not first_audio:
                return False

            position, last_timestamp, audio_packets = size, None, 0
            for _ in range(16):
                if position == first_tag:
                    return audio_packets > 0
                if position < first_tag + 15:
                    return False
                fh.seek(position - 4)
                tag_size = int.from_bytes(fh.read(4), "big")
                start = position - 4 - tag_size
                if tag_size < 11 or start < first_tag:
                    return False
                tag = tag_at(start)
                if tag is None or tag[3] != position:
                    return False
                kind, prefix, length, _, timestamp = tag
                if last_timestamp is not None and timestamp > last_timestamp:
                    return False
                last_timestamp = timestamp
                if kind == 8:
                    if len(prefix) < 2 or prefix[0] >> 4 != 10 or prefix[1] not in (0, 1):
                        return False
                    if prefix[1] == 1:
                        if length <= 2:
                            return False
                        audio_packets += 1
                position = start
            return audio_packets > 0
    except (OSError, ValueError):
        return False


def _recent_recording_fragments(tag, stamp, since):
    # Enumerate filenames only. Read metadata for at most two files per directory.
    import heapq
    candidates = []
    for day in {time.strftime("%Y-%m-%d", time.localtime(stamp)),
                time.strftime("%Y-%m-%d", time.localtime(stamp - 86400))}:
        directory = DATA / ("audio_%s_%s" % (tag, day))
        names = (p for p in directory.glob("seg_*.flv") if p.stem[4:].isdigit())
        for path in heapq.nlargest(2, names, key=lambda p: int(p.stem[4:])):
            try:
                info = path.stat()
                # A previous broadcast's buffered file can be flushed after the
                # new broadcast starts. mtime alone does not establish ownership.
                born = getattr(info, "st_birthtime", None)
                if born is None and os.name == "nt":
                    born = info.st_ctime  # Windows Python before st_birthtime.
                if (info.st_size and _valid_progress_time(born, stamp)
                        and since <= born <= info.st_mtime <= stamp):
                    candidates.append((info.st_mtime, path))
            except OSError:
                pass
    return sorted(candidates, reverse=True)[:2]

def recording_health(entry: dict, now: float | None = None,
                     instance: dict | None = None) -> dict:
    """只在可信且新鲜的在播状态下要求录音；配额暂停是正常状态。"""
    stamp = time.time() if now is None else now
    result = {"state": "disabled", "age_sec": None}
    if not entry.get("enabled", True) or not entry.get("record"):
        return result
    tag = entry["tag"]
    instance = instance_status("record", tag) if instance is None else instance
    # 仅录音路没有独立 watch 可替它蹲守，配置要求 recorder 自身常驻。
    if not entry.get("watch") and not instance["running"]:
        return {"state": "stopped", "age_sec": None}
    watch = _last_watch_state(tag)
    if not watch:
        return {"state": "unknown", "age_sec": None}
    if watch.get("error") or str(watch.get("api_msg") or "").lower() == "blocked":
        return {"state": "unknown", "age_sec": None}
    try:
        if stamp - float(watch.get("ts") or 0) > WATCH_STALE_SEC:
            return {"state": "unknown", "age_sec": None}
    except (TypeError, ValueError):
        return {"state": "unknown", "age_sec": None}
    if watch.get("is_live") is not True:
        from watch_anchor_live import is_confirmed_offline
        return {"state": "idle" if is_confirmed_offline(watch) else "unknown",
                "age_sec": None}
    try:
        state = json.loads((DATA / ("stream_%s.state.json" % tag)).read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    sid = str(watch.get("room_id_str") or watch.get("room_id") or "")
    old_sid = str(state.get("session_id") or "")
    same_session = not (sid and old_sid and sid != old_sid)
    same_room = (bool(watch.get("web_rid")) and
                 str(watch["web_rid"]) == str(state.get("web_rid") or ""))
    if (state.get("exhausted") is True and state.get("phase") == "quota"
            and not state.get("offline_seen") and same_session and same_room):
        return {"state": "quota", "age_sec": None}
    if not instance["running"]:
        return {"state": "stopped", "age_sec": None}
    # The grace belongs to a broadcast, never to a replacement process. A startup
    # receipt can have no room yet, and PID/receipt creation must not clear an
    # existing no-audio incident. Do not borrow another room/session's start time.
    since = state.get("live_since")
    if not (sid and sid == old_sid and same_room and not state.get("offline_seen")
            and _valid_progress_time(since, stamp)):
        since = (_watch_session_start(tag, sid, stamp, str(watch.get("web_rid") or ""))
                 if sid and watch.get("web_rid") else None)
    progress = _record_audio_progress(tag, instance, watch, stamp)
    if progress is not None:
        latest = progress["last_audio"]
        if latest is not None and since is not None and latest < since:
            latest = None
        if latest is not None:
            age = stamp - latest
            return {"state": "stalled" if age > RECORD_STALE_SEC else "ok",
                    "age_sec": int(max(0, age)),
                    "reason": "invalid_audio" if progress["invalid_audio"] else "audio_progress"}
    if since is None:
        return {"state": "unknown", "age_sec": None}
    latest = None
    fragments = ([] if progress and progress.get("scope_mismatch")
                 else _recent_recording_fragments(tag, stamp, since))
    invalid_fragment = bool(progress and progress["invalid_audio"])
    for modified, path in fragments:
        # For a new worker without audio, allow only already-written same-session
        # audio as a restart bridge. Current network/file growth is not a receipt.
        if progress is not None and modified > progress["created"]:
            continue
        if _flv_has_recent_aac(path):
            latest = modified
            break
        invalid_fragment = True
    if latest is not None:
        age = max(0, stamp - latest)
        return {"state": "stalled" if age > RECORD_STALE_SEC else "ok",
                "age_sec": int(age),
                "reason": "invalid_audio" if invalid_fragment else "audio_progress"}
    age = max(0, stamp - since)
    return {"state": "starting" if age <= RECORD_START_GRACE_SEC else "stalled",
            "age_sec": int(age), "reason": "invalid_audio" if invalid_fragment else "no_audio"}


def anchor_status(entry: dict) -> dict:
    """单路状态 + 数据新鲜度（给前端表格用）。"""
    tag = entry["tag"]
    w = instance_status("watch", tag)
    r = instance_status("record", tag)
    w_age = _file_age(DATA / ("watch_%s.jsonl" % tag))
    d_age = _file_age(DATA / ("danmu_douyin_%s_%s.jsonl" % (tag, _today())))
    is_live = _last_is_live(tag)
    recording = recording_health(entry, instance=r)

    # 状态机（设计 §五）：深夜没播 ≠ 采集挂了
    if not entry.get("enabled", True):
        state = "disabled"
    elif entry.get("watch") and not w["running"]:
        state = "stopped"
    elif entry.get("watch") and w_age is not None and w_age > WATCH_STALE_SEC:
        state = "stalled"          # ★ 2026-10-22 16:45 八路全停就是这种
    elif entry.get("watch") and w_age is None:
        state = "unknown"          # 刚加的主播还没探测
    elif recording["state"] in ("stopped", "stalled"):
        state = recording["state"]
    elif is_live and d_age is not None and d_age > DANMU_STALE_SEC:
        state = "silent"           # 在播但弹幕停了
    else:
        state = "ok"

    return {
        "tag": tag,
        "name": entry.get("name") or tag,
        "sec_uid": entry.get("sec_uid", ""),
        "watch": bool(entry.get("watch")),
        "record": bool(entry.get("record")),
        "llm": bool(entry.get("llm")),
        "enabled": bool(entry.get("enabled", True)),
        "note": entry.get("note", ""),
        "added_at": entry.get("added_at", ""),
        "watch_pid": w["pid"],
        "record_pid": r["pid"],
        "watch_running": w["running"],
        "record_running": r["running"],
        "record_state": recording["state"],
        "record_age_sec": recording["age_sec"],
        "is_live": is_live,
        "watch_age_sec": int(w_age) if w_age is not None else None,
        "danmu_age_sec": int(d_age) if d_age is not None else None,
        "state": state,
    }


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _last_watch_state(tag: str):
    """读最后一条完整探测记录（None = 读不到）。"""
    p = DATA / ("watch_%s.jsonl" % tag)
    try:
        size = p.stat().st_size
        with open(p, "rb") as f:
            f.seek(max(0, size - 2000))
            lines = [l for l in f.read().decode("utf-8", "replace").splitlines() if l.strip()]
        if not lines:
            return None
        for line in reversed(lines):
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    return row
            except ValueError:
                continue
    except (OSError, ValueError):
        return None


def _last_is_live(tag: str):
    row = _last_watch_state(tag)
    return row.get("is_live") if row else None


def status_all(roster: dict) -> dict:
    items = [anchor_status(a) for a in roster.get("anchors") or []]
    return {"anchors": items, "health": _health_of(items)}


def _health_of(items: list) -> dict:
    enabled = [x for x in items if x["enabled"]]
    bad = [x for x in enabled if x["state"] in ("stalled", "stopped", "silent")]
    return {
        "total": len(items),
        "enabled": len(enabled),
        "ok": len([x for x in enabled if x["state"] in ("ok", "unknown")]),
        "stalled": len([x for x in enabled if x["state"] == "stalled"]),
        "stopped": len([x for x in enabled if x["state"] == "stopped"]),
        "silent": len([x for x in enabled if x["state"] == "silent"]),
        "problems": [{"tag": x["tag"], "state": x["state"],
                      "watch_age_sec": x["watch_age_sec"],
                      "record_state": x.get("record_state"),
                      "record_age_sec": x.get("record_age_sec")} for x in bad],
    }


# ---------------- 启停 ----------------

def _spawn_watch(entry: dict) -> subprocess.Popen:
    cmd = [str(PY), WATCHDOG_SCRIPT, "--sec-uid", entry["sec_uid"],
           "--tag", entry["tag"], "--start"]
    if entry.get("llm"):
        cmd.append("--llm")
    return subprocess.Popen(cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=_flags())


def _resolve_record_python() -> str:
    """挑一个能 import curl_cffi 的解释器给 recorder 用；一个都找不到返回 ""。

    故意**不退回 .venv** —— .venv 没有 curl_cffi，用它起 recorder 必然 0 分片，
    等于"起了个假的"（2026-10-25 一整天就是被这种静默失败吃掉的）。宁可明确报错。
    探测结果进程内缓存。
    """
    global _record_py_cache
    if _record_py_cache is not None:
        return _record_py_cache
    for cand in RECORD_PY_CANDIDATES:
        if not cand:
            continue
        p = Path(cand)
        if not p.exists():
            continue
        try:
            r = subprocess.run([str(p), "-c", "import curl_cffi"],
                               capture_output=True, timeout=60,
                               creationflags=_flags())
            if r.returncode == 0:
                _record_py_cache = str(p)
                return _record_py_cache
        except Exception:
            continue
    _record_py_cache = ""
    return _record_py_cache


def _spawn_record(entry: dict) -> subprocess.Popen:
    py = _resolve_record_python()
    if not py:
        raise RuntimeError(
            "找不到能 import curl_cffi 的 Python，录音无法启动。候选=%s；"
            "设环境变量 WB_RECORD_PY 或补装 curl_cffi。"
            % [c for c in RECORD_PY_CANDIDATES if c])
    cmd = [py, RECORDER_SCRIPT, "--tag", entry["tag"]]
    return subprocess.Popen(cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=_flags())


def start(kind: str, entry: dict) -> dict:
    """拉起一路。幂等：已在跑直接返回 already_running。"""
    if _CUSTOMER_CONTROLLER is not None:
        return _CUSTOMER_CONTROLLER.start_role(kind, entry)
    tag = entry["tag"]
    with _LOCK:
        st = instance_status(kind, tag)
        if st["running"]:
            return {"ok": True, "state": "already_running", "pid": st["pid"]}
        if kind == "watch" and not entry.get("sec_uid"):
            return {"ok": False, "state": "error", "error": "名册里 sec_uid 为空"}
        if kind == "watch" and not entry.get("watch"):
            return {"ok": False, "state": "error", "error": "这一路的 watch 已关闭"}
        if kind == "record" and not entry.get("record"):
            return {"ok": False, "state": "error", "error": "这一路的 record 已关闭"}
        if kind == "watch" and not _cdp_up() and _any_watch_running():
            # 别的路在跑 → 它会负责拉浏览器，我们等它起来，别同时抢 profile；
            # 一路都没跑 → 我们自己就是第一个，直接起（它会去拉 Chrome）。
            _wait_cdp()
        _clear_pidfile(kind, tag)        # 清掉可能残留的死 pid
        try:
            proc = _spawn_watch(entry) if kind == "watch" else _spawn_record(entry)
        except Exception as e:
            return {"ok": False, "state": "error", "error": str(e)[:300]}
        time.sleep(START_SETTLE)
        pid = read_pid(kind, tag) or proc.pid
        alive = pid_alive(pid)
        return {"ok": bool(alive), "state": "started" if alive else "failed",
                "pid": pid if alive else 0,
                "error": "" if alive else "拉起后 pidfile 未落盘/进程秒退，看 data/*.log"}


def stop(kind: str, tag: str) -> dict:
    """停一路；新 watchdog 由私有 Job 清子树，不能 /T 误杀共享浏览器。"""
    if _CUSTOMER_CONTROLLER is not None:
        return _CUSTOMER_CONTROLLER.stop_role(kind, tag)
    with _LOCK:
        pid = read_pid(kind, tag)
        if not pid or not pid_alive(pid):
            _clear_pidfile(kind, tag)
            return {"ok": True, "state": "not_running"}
        pf = pidfile(kind, tag)
        try:
            owned = pid_owns_pidfile(pid, pf, strict=True)
        except RuntimeError as exc:
            # 状态查询可以保守当作存活；破坏性操作必须确认归属，未知不能杀也不删证据。
            return {"ok": False, "state": "identity_unverified", "pid": pid,
                    "error": str(exc)}
        if not owned:
            _clear_pidfile(kind, tag)
            return {"ok": True, "state": "not_running"}
        if kind == "watch":
            try:
                marker = json.loads(pf.with_suffix(".protocol.json").read_text(encoding="utf-8"))
                verified_job = (marker.get("protocol") == "owned-watch-job-v1"
                                and marker.get("pid") == pid
                                and marker.get("created") == process_create_time(pid))
            except (OSError, ValueError, AttributeError):
                verified_job = False
            if not verified_job:
                return {"ok": False, "state": "legacy_cleanup_required", "pid": pid,
                        "error": "旧版或无法验证 Job 协议；首次切换须在维护窗口完整清理旧实例，"
                                 "拒绝用 /T 误杀共享浏览器或仅杀根遗留采集进程"}
            command = ["taskkill", "/F", "/PID", str(pid)]
        else:
            command = ["taskkill", "/F", "/T", "/PID", str(pid)]
        _run_hidden(command)
        time.sleep(0.6)
        try:
            still = pid_owns_pidfile(pid, pf, strict=True)
        except RuntimeError as exc:
            return {"ok": False, "state": "identity_unverified", "pid": pid,
                    "error": str(exc)}
        if not still:
            _clear_pidfile(kind, tag)     # taskkill 不会让进程走 finally
            if kind == "watch":
                try:
                    pf.with_suffix(".protocol.json").unlink(missing_ok=True)
                except OSError:
                    pass
        return {"ok": not still, "state": "stopped" if not still else "still_running",
                "pid": pid}


def start_anchor(entry: dict, kinds=("watch", "record")) -> dict:
    out = {}
    for k in kinds:
        if k == "watch" and not entry.get("watch"):
            continue
        if k == "record" and not entry.get("record"):
            continue
        out[k] = start(k, entry)
    return out


def stop_anchor(tag: str, entry: dict, kinds=("watch", "record")) -> dict:
    out = {}
    for k in kinds:
        out[k] = stop(k, tag)
    return out


def restart_anchor(tag: str, entry: dict, kinds=("watch", "record")) -> dict:
    """先停再起。**不动 enabled** —— 重启不等于停用。

    2026-10-28：老大在主播页找不到「启动」（名册 enabled=true 时只有「停止」），
    而进程其实早就死了 —— 想恢复只能去命令行。加这条路给页面的「重启」按钮用。
    stop() 内部会 taskkill /T 并等 0.6s 复查，start() 又带幂等判活，
    所以这里不需要额外 sleep。
    """
    stopped = stop_anchor(tag, entry, kinds)
    if any(not result.get("ok") for result in stopped.values()):
        # 看板按 started 的 ok 判定重启结果；不得把拒绝停止伪装成 already_running 成功。
        return {"stopped": stopped,
                "started": {kind: {"ok": False, "state": "restart_blocked",
                                   "error": "停止未完成，拒绝继续启动；先处理停止结果"}
                            for kind in stopped}}
    started = start_anchor(entry, kinds)
    return {"stopped": stopped, "started": started}
