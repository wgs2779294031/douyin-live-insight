# -*- coding: utf-8 -*-
"""进程外健康探针：Windows 计划任务每 5 分钟拉起一次，跑完即退。

为什么要有它
------------
所有「进程内」告警（run_watchdog 的浏览器掉线、watch_anchor_live 的开播/下播、
board_watchdog 的看板看护）都依赖「自己还活着」。2026-10-22 16:45 与 10-23 01:05
两次进程集体消失，这些告警一条都没发出来 —— **监控者本身就是被监控对象，一起死
就一起哑**。

本脚本不常驻、不依赖任何 python 常驻进程：由 Windows 任务计划拉起 → 检查 →
异常发飞书；停写、空转和录音故障按原有流程自愈。持续无效的直播状态探测只告警。

判据
----
`data/watch_<tag>.jsonl` 的 mtime。没在播时 watch_anchor_live 也在写（心跳/探测），
所以「15 分钟没更新」等于真停了，跟有没有在播无关。
持续 10 分钟、至少 3 次探测无效且没有近 3 分钟的实际业务数据时，按一次监控受限事件提醒。

自愈
----
每小时最多尝试一次 `launch_all.py --headed`（尊重名册 llm 开关；会 skip 活着的路）。
不给它无限重试：反复失败时只报警不重启，免得刷屏 + 撞风控。
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import anchor_manager as mgr          # noqa: E402
import anchor_roster as roster_mod    # noqa: E402
import notify                          # noqa: E402
from watch_anchor_live import classify_env_failure, is_confirmed_offline  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STATE_PATH = DATA / "health_probe_state.json"

STALE_SEC = 900            # 15 分钟不更新算停
INVALID_MIN_ROUNDS = 3
INVALID_MIN_SPAN = 600
SILENT_SEC = 180           # 直播中但 stats 超过这么久没写 = 采集空转
ALERT_COOLDOWN = 1800      # 同一路 30 分钟内只报一次
ALERT_RETRY_SEC = 60       # 未送达不占成功冷却；后续探针运行可短周期重试
HEAL_COOLDOWN = 3600       # 每小时最多自愈一次
SILENT_HEAL_GAP = 300      # 静默路串行重启的最小间隔（一次只动一路，防抢浏览器）

# pythonw 下 sys.stdout 是 None，print() 会直接抛 AttributeError 把探针搞死。
# 2026-10-25：计划任务改用 pythonw 直调（不再经 .bat，否则每 5 分钟弹一次 cmd 黑窗），
# 所以这里自己兜住输出，顺手把内容也落到同一个日志里。
if sys.stdout is None:
    try:
        sys.stdout = open(DATA / "health_probe.log", "a",
                          encoding="utf-8", buffering=1)
        sys.stderr = sys.stdout
    except Exception:
        pass


def _load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(st: dict) -> None:
    _save_json(STATE_PATH, st)


def _save_json(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)
    os.replace(tmp, path)


def _fmt(sec: float) -> str:
    if sec >= 86400:
        return "从未生成"
    if sec < 90:
        return "%d 秒" % sec
    if sec < 5400:
        return "%d 分钟" % round(sec / 60)
    return "%.1f 小时" % (sec / 3600)


def probe(roster: dict, now: float | None = None) -> list:
    now = now if now is not None else time.time()
    stale = []
    for a in roster.get("anchors", []):
        if not (a.get("watch") and a.get("enabled")):
            continue
        p = DATA / ("watch_%s.jsonl" % a["tag"])
        try:
            age = now - p.stat().st_mtime
        except OSError:
            age = 86400.0
        if age > STALE_SEC:
            stale.append({"tag": a["tag"], "age": age})
    return stale


def _last_json_line(path: Path, tail_bytes: int = 8192):
    """读文件尾部最后一条能解析的 JSON（大文件不整读）。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


def _tail_records(path, limit):
    """Yield committed records, newest first, including a barrier for unreadable records."""
    try:
        with path.open("rb") as f:
            size = f.seek(0, os.SEEK_END)
            offset = max(0, size - limit)
            f.seek(offset)
            data = f.read(size - offset)
    except FileNotFoundError:
        return
    except OSError:
        yield None
        return
    lines = data.split(b"\n")[1 if offset else 0:-1]
    for line in reversed(lines):
        if line.strip():
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                row = None
            yield row if isinstance(row, dict) else None


def _usable_ts(value, now):
    return type(value) in (int, float) and math.isfinite(value) and 0 < value <= now


PRIMARY_CODES = {"profile_blocked_or_empty", "profile_unconfirmed", "profile_request_failed"}
FALLBACK_CODES = {"no_cached_room", "unexpected_origin", "http_failure", "api_failure",
                  "unconfirmed_room", "owner_unconfirmed", "live_unconfirmed", "request_failed"}


def _watch_report(tag, now):
    info = {"probe": "missing", "ts": None, "primary": "unknown", "fallback": "not_reported",
            "rounds": 0, "span": 0, "kind": None}
    previous = None
    for row in _tail_records(DATA / ("watch_%s.jsonl" % tag), 131072):
        ts = row.get("ts") if row else None
        if not _usable_ts(ts, now):
            if previous is None:
                info["probe"] = "unreadable"
            break
        if previous is None:
            info["ts"] = ts
            if now - ts > STALE_SEC:
                info["probe"] = "stale"
                break
        elif not 0 < previous - ts <= STALE_SEC:
            break
        kind = classify_env_failure(row)
        if kind is None and row.get("api_code") not in (None, 0):
            kind = "channel"
        if kind is None:
            if row.get("is_live") is True and row.get("web_rid"):
                probe = "live"
            elif is_confirmed_offline(row):
                probe = "offline"
            else:
                probe, kind = "unknown", "channel"
        else:
            probe = "unknown"
        if previous is None:
            # Values in a watch row may be upstream text. Only emit locally known labels.
            primary = row.get("profile_error")
            fallback = row.get("room_enter_result")
            info["primary"] = primary if isinstance(primary, str) and primary in PRIMARY_CODES else (
                "unconfirmed" if kind else "ok")
            if isinstance(fallback, str):
                info["fallback"] = fallback if fallback in FALLBACK_CODES else "other"
            if probe == "live" and row.get("probe_source") == "room_enter_fallback":
                info["fallback"] = "verified_live"
            info["probe"], info["kind"] = probe, kind
        if kind is None:
            break
        previous = ts
        info["rounds"] += 1
        info["span"] = info["ts"] - ts
        if info["rounds"] >= INVALID_MIN_ROUNDS and info["span"] >= INVALID_MIN_SPAN:
            break
    return info


def probe_invalid(roster: dict, now: float):
    """Compatibility view for predicate callers; delivery also checks business evidence."""
    unknown, sustained = set(), []
    for a in roster.get("anchors", []):
        if a.get("watch") and a.get("enabled"):
            tag = a["tag"]
            row = _watch_report(tag, now)
            if row["probe"] == "unknown":
                unknown.add(tag)
                if row["rounds"] >= INVALID_MIN_ROUNDS and row["span"] >= INVALID_MIN_SPAN:
                    sustained.append({"tag": tag, "kind": "探测无效", "rounds": row["rounds"],
                                      "span": row["span"], "last_ts": row["ts"], "last_error_kind": row["kind"]})
    return unknown, sustained


BUSINESS_TYPES = {"stats": {"room_stats", "room_user_seq"}, "danmu": {"danmaku", "like", "enter", "gift"}}


def _business_age(tag, stream, now):
    days = {time.strftime("%Y-%m-%d", time.localtime(t)) for t in (now, now - SILENT_SEC)}
    newest = None
    for day in days:
        path = DATA / f"{stream}_douyin_{tag}_{day}.jsonl"
        for row in _tail_records(path, 16384):
            if (row and row.get("platform", "douyin") == "douyin"
                    and row.get("tag", tag) == tag and row.get("room_tag", tag) == tag
                    and isinstance(row.get("type"), str) and row["type"] in BUSINESS_TYPES[stream]
                    and _usable_ts(row.get("ts"), now)):
                newest = max(newest or 0, row["ts"])
    return None if newest is None else now - newest


def diagnose(roster, now):
    result = {}
    for a in roster.get("anchors", []):
        if not (a.get("enabled") and a.get("watch")):
            continue
        tag = a["tag"]
        row = _watch_report(tag, now)
        ages = {k: _business_age(tag, k, now) for k in BUSINESS_TYPES}
        row["stats_age_sec"], row["interaction_age_sec"] = ages["stats"], ages["danmu"]
        row["evidence"] = [k for k, age in ages.items() if age is not None and age <= SILENT_SEC]
        row["activity"] = "recent" if row["evidence"] else "no_recent_evidence"
        level = "normal" if row["probe"] in ("live", "offline") else "observe"
        if level == "observe" and row["evidence"]:
            level = "degraded_data_active"
        elif row["rounds"] >= INVALID_MIN_ROUNDS and row["span"] >= INVALID_MIN_SPAN:
            level = "missed_start_risk"
        row["level"] = level
        result[tag] = row
    return result


def probe_silent(roster: dict, now: float | None = None) -> list:
    """直播中但采集空转：watch 说在播，stats 却几分钟没写。

    为什么需要这一层（2026-10-29 事故）：watch_anchor_live 曾有「采集进程死了一次性
    标志不再重生」的缺陷，而此时另外三层全部判「健康」——
      - run_watchdog：只看 watch_anchor_live 进程活没活 → 活着；
      - 本探针的 probe()：只看 watch_<tag>.jsonl mtime → 探测每 60 秒照常刷新；
      - 看板主播页：只看进程 → 正常。
    只有实时页按 danmu 落盘才看得出停滞（红条），其余全绿。

    判据用 **stats 落盘**而不是 danmu：stats 是采集器每 30 秒发的统计包，
    跟直播间有没有人说话无关（弹幕稀疏的场次用 danmu 会误报）。

    :return: [{"tag", "age", "online"}, ...]，age = stats 最后写入距今秒数
    """
    now = now if now is not None else time.time()
    # 日期从 now 推（不是 time.strftime 默认取当前）—— 逻辑时间与文件名必须同一个时点
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    silent = []
    for a in roster.get("anchors", []):
        if not (a.get("watch") and a.get("enabled")):
            continue
        tag = a["tag"]
        last = _last_json_line(DATA / ("watch_%s.jsonl" % tag))
        if not isinstance(last, dict) or not last.get("is_live") or not _usable_ts(last.get("ts"), now):
            continue
        # 探测行本身太旧 → 那是「停写」，归 probe() 管，这里不重复报
        if now - float(last.get("ts") or 0) > STALE_SEC:
            continue
        stats_path = DATA / ("stats_douyin_%s_%s.jsonl" % (tag, day))
        try:
            age = now - stats_path.stat().st_mtime
        except OSError:
            age = 86400.0
        if age > SILENT_SEC:
            silent.append({"tag": tag, "age": age,
                           "online": last.get("user_count")})
    return silent


def probe_recording(roster: dict, now: float | None = None) -> list:
    """录音已配置且确认在播时，检查进程及分片；配额停录不当故障。"""
    failed = []
    for entry in roster.get("anchors", []):
        status = mgr.recording_health(entry, now=now)
        if status["state"] in ("stopped", "stalled"):
            failed.append({"tag": entry["tag"],
                           "age": status["age_sec"] or 0,
                           "kind": ("录音退出" if status["state"] == "stopped" else
                                    "录音格式异常" if status.get("reason") == "invalid_audio" else "录音停写")})
    return failed


def _beat(text: str) -> None:
    """每次运行留一行心跳：证明「计划任务真的把 python 跑起来了」。

    不靠 stdout 重定向 —— pythonw 无控制台时 stdout 句柄不可靠，而且 bat 里的
    `start` 是异步的，任务返回 0 并不代表 python 成功。
    """
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        with open(DATA / "health_probe_beat.log", "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
    except OSError:
        pass


def _coverage_warning(risk):
    return ("[竞品监控][监控受限] %s 持续无法可靠判断直播状态，且近 3 分钟未发现新的人气或互动记录。"
            "可能漏采新开播，请核对这些直播间。" % "、".join(sorted(risk)))


def _handle_probe_incident(tags, state, now, send=None, *, scope_trusted=True):
    """One delivery owner for coverage of enabled rooms, across repeated scheduled runs."""
    risk = {tag for tag, row in tags.items() if row["level"] == "missed_start_risk"}
    covered = {tag for tag, row in tags.items() if row["evidence"]}
    confirmed = {tag for tag, row in tags.items() if row["probe"] in ("live", "offline")}
    # A 3-minute scheduled run may miss the intermediate degraded-but-still-writing
    # phase. Preserve actual data evidence across live/unknown samples until offline.
    had_data = (set(state.get("probe_had_data", [])) | covered).intersection(tags)
    had_data.difference_update(tag for tag in confirmed if tags[tag]["probe"] == "offline")
    if had_data:
        state["probe_had_data"] = sorted(had_data)
    else:
        state.pop("probe_had_data", None)

    incident = state.get("probe_incident") or {}
    legacy = set(state.pop("invalid_probes", {}))
    sent = state.get("alerted") or {}
    failed = state.get("alert_failed") or {}
    # Carry forward actual legacy send successes, even if the new 10-minute test
    # has not accumulated yet. Do not infer delivery from a watcher retry counter.
    legacy_sent = {tag for tag in legacy if sent.get("invalid:" + tag)}
    if legacy and not incident:
        incident = {"affected": sorted(legacy), "notified": bool(legacy_sent),
                    "last_failed_at": max((failed.get("invalid:" + tag, 0) for tag in legacy), default=0)}
    for table in (sent, failed):
        for key in list(table):
            if key.startswith("invalid:"):
                del table[key]

    if not risk and not incident:
        return {"status": "clear"}
    affected = set(incident.get("affected", [])) | risk
    # A verified roster change retires coverage; it is not evidence of recovery.
    removed = affected.difference(tags) if scope_trusted else set()
    affected -= removed
    retired = (set(incident.get("retired", [])) | removed) - affected
    scope = {"retired": sorted(retired)} if retired else {}
    incident.update(scope)
    if not retired:
        incident.pop("retired", None)
    incident["affected"] = sorted(affected)
    incident.setdefault("opened_at", now)
    incident.setdefault("notified", False)
    # A fresh timestamp from before the first risky observation cannot prove recovery
    # after a rotation, truncated history, or a replacement with an older watch file.
    since = incident.setdefault("first_risk_ts", {})
    for tag in removed:
        since.pop(tag, None)
    for tag in risk:
        since.setdefault(tag, tags[tag]["ts"])
    confirmed = {tag for tag in confirmed if tags[tag]["ts"] >= since.get(tag, 0)}
    covered = {tag for tag in covered if any(now - tags[tag][field] >= since.get(tag, 0)
               for field in ("stats_age_sec", "interaction_age_sec")
               if tags[tag][field] is not None and tags[tag][field] <= SILENT_SEC)}
    escalated = set(incident.get("escalated", [])) - removed
    if removed:
        incident["escalated"] = sorted(escalated)
    escalation = (risk & had_data) - escalated
    unresolved = affected - (confirmed | covered)
    incident["unresolved"] = sorted(unresolved)
    state["probe_incident"] = incident

    if not unresolved:
        if removed:
            state.pop("probe_incident")
            return {"status": "scope_changed", **scope}
        if not incident["notified"]:
            state.pop("probe_incident")
            return {"status": "clear"}
        action = "recovery"
    elif risk and (not incident["notified"] or escalation):
        action = "warning"
    else:
        return {"status": "open", "unresolved": sorted(unresolved), "notified": incident["notified"], **scope}

    if now - incident.get("last_failed_at", 0) < ALERT_RETRY_SEC:
        return {"status": "delivery_pending", "action": action, "unresolved": sorted(unresolved), **scope}
    if action == "recovery":
        message_tags = affected
        live = sorted(tag for tag in affected if tags[tag]["probe"] == "live")
        offline = sorted(tag for tag in affected if tags[tag]["probe"] == "offline")
        data_only = sorted(affected - confirmed)
        details = [label + "：" + "、".join(names) for label, names in
                   (("已确认在播", live), ("已确认下播", offline), ("已有新业务数据，直播状态待确认", data_only)) if names]
        text = "[竞品监控][监控状态更新] " + "；".join(details)
    else:
        message_tags = escalation if incident["notified"] else risk
        text = _coverage_warning(message_tags)
    try:
        deliver = notify.send_text if send is None else send
        delivered = deliver(text, key="probe_coverage_" + action, cooldown=0) is True
    except Exception:
        delivered = False
    state["last_delivery"] = {"status": "sent" if delivered else "failed", "ts": now,
                              "tags": sorted(message_tags)}
    if not delivered:
        incident["last_failed_at"] = now
        return {"status": "delivery_pending", "action": action, "unresolved": sorted(unresolved), **scope}
    if action == "recovery":
        state.pop("probe_incident")
        return {"status": "resolved", "notified": True, **scope}
    incident.update(notified=True, last_failed_at=0, escalated=sorted(escalated | escalation))
    return {"status": "open", "notified": True, "unresolved": sorted(unresolved), **scope}


def preview_probe(roster, now=None, state=None):
    """Read current evidence and simulate probe-only notification with an in-memory acknowledgment."""
    from copy import deepcopy
    now = time.time() if now is None else now
    tags = diagnose(roster, now)
    messages = []
    def capture(text, **kwargs):
        messages.append(text)
        return True
    snapshot = _load_state() if state is None else deepcopy(state)
    incident = _handle_probe_incident(tags, snapshot, now, send=capture)
    risk = [tag for tag, row in tags.items() if row["level"] == "missed_start_risk"]
    return {"schema": 1, "generated_at": now, "tags": tags, "incident": incident,
            "would_send": messages, "warning_example": _coverage_warning(risk) if risk else None}


def main(argv=None) -> int:
    _beat("start")
    roster, fell = roster_mod.ensure()
    if fell:
        print("[warn] anchors.json 缺失/损坏，已用种子重建")

    now = time.time()
    stale = probe(roster, now)
    diagnostics = diagnose(roster, now)
    inconclusive = {tag for tag, row in diagnostics.items() if row["probe"] not in ("live", "offline")}
    # An invalid status row cannot establish a live room or justify a restart.
    silent = [item for item in probe_silent(roster, now) if item["tag"] not in inconclusive]
    recording = [item for item in probe_recording(roster, now) if item["tag"] not in inconclusive]
    state = _load_state()
    incident = _handle_probe_incident(diagnostics, state, now, scope_trusted=not fell)
    snapshot = {"schema": 1, "generated_at": now, "tags": diagnostics, "incident": incident,
                "stalled": [s["tag"] for s in stale], "silent": [s["tag"] for s in silent],
                "recording_faults": [s["tag"] for s in recording]}
    _save_json(DATA / "health_probe_diagnosis.json", snapshot)
    if not stale and not silent and not recording:
        state = {k: v for k, v in state.items() if k in ("probe_incident", "probe_had_data")
                 or (k == "last_delivery" and "probe_incident" in state)}
        _save_state(state)
        if "probe_incident" in state:
            print("[warn] 开播监控受限，事件状态：%s" % incident["status"])
            return 1
        print("[observe] %d 路直播状态待确认" % len(inconclusive) if inconclusive else "[ok] 全部正常")
        return 0

    # ---- 空转自愈：串行，一次只重启一路（八路同时重启会抢同一个浏览器）----
    silent_healed = ""
    if silent and now - float(state.get("last_silent_heal", 0) or 0) >= SILENT_HEAL_GAP:
        target = silent[0]["tag"]
        ent = next((a for a in roster.get("anchors", []) if a.get("tag") == target), None)
        if ent is not None:
            try:
                result = mgr.restart_anchor(target, ent, kinds=("watch",))
                if result.get("started", {}).get("watch", {}).get("ok"):
                    silent_healed = target
                    print("[heal] 已重新启动监控进程：%s" % target)
            except Exception as e:      # noqa: BLE001
                print("[warn] 重启空转路失败 %s: %r" % (target, e))
        state["last_silent_heal"] = now

    # ---- 自愈（每小时最多一次）----
    healed = False
    heal_err = ""
    attempted = bool(stale or silent) and now - float(state.get("last_heal", 0) or 0) >= HEAL_COOLDOWN
    if attempted:
        try:
            # 不再传 --no-llm（2026-10-23）：LLM 已开，自愈必须尊重名册的 llm 开关。
            # 原来固定 --no-llm，16:12 自愈一次就把「赵主任」那路的 LLM 悄悄关掉了，
            # 而且进程会一直带着 --no-llm 跑到下次手动重启 —— 属于「恢复了采集、
            # 但改了另一个配置」的隐性损失。
            r = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "launch_all.py"), "--headed"],
                cwd=str(ROOT), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=300)
            healed = r.returncode == 0
            if not healed:
                heal_err = (r.stderr or r.stdout or "")[-200:]
        except Exception as e:
            heal_err = repr(e)
        state["last_heal"] = now

    # ---- 告警（同 tag 30 分钟冷却）----
    last_alerts = state.get("alerted") or {}
    failed_alerts = state.get("alert_failed") or {}
    items = ([{"tag": s["tag"], "age": s["age"], "kind": "停写"} for s in stale]
             + [{"tag": s["tag"], "age": s["age"], "kind": "空转"} for s in silent]
             + recording)
    fresh = [s for s in items
             if now - float(last_alerts.get(s["tag"], 0) or 0) >= ALERT_COOLDOWN
             and now - float(failed_alerts.get(s["tag"], 0) or 0) >= ALERT_RETRY_SEC]

    if fresh:
        head = []
        if stale:
            head.append("采集停写 %d 路" % len(stale))
        if silent:
            head.append("直播中但空转 %d 路" % len(silent))
        if recording:
            head.append("录音异常 %d 路" % len(recording))
        lines = ["[竞品监控][需人工处理] " + "、".join(head) + "（进程外探针发现）", ""]
        for s in fresh:
            detail = ("录音进程未运行" if s["kind"] == "录音退出" else
                      _fmt(s["age"]) + ("未见有效音频包（录音格式异常）" if s["kind"] == "录音格式异常" else
                      "未见有效音频更新" if s["kind"] == "录音停写" else
                      "没更新" if s["kind"] == "停写" else "无数据（在播）"))
            lines.append("- %s：%s" % (s["tag"], detail))
        lines.append("")
        if silent_healed:
            lines.append("空转自愈：已重启 %s" % silent_healed)
        if stale and attempted:
            lines.append("停写自愈：%s%s" % ("已尝试" if healed else "尝试失败",
                                            ("（%s）" % heal_err) if heal_err else ""))
        lines.append("看板 https://weiguoshen.cn:8443 → 主播页")
        tags = sorted({s["tag"] for s in fresh})
        try:
            delivered = notify.send_text("\n".join(lines), key="probe_" + ",".join(tags),
                                         cooldown=0) is True
        except Exception:
            delivered = False
        for key in tags:
            if delivered:
                last_alerts[key] = now
                failed_alerts.pop(key, None)
            else:
                failed_alerts[key] = now
        state["alerted"], state["alert_failed"] = last_alerts, failed_alerts
        state["last_delivery"] = {"status": "sent" if delivered else "failed",
                                  "ts": now, "tags": tags}
        if delivered:
            print("[alert] 停写 %d / 空转 %d / 录音异常 %d，已推送" % (len(stale), len(silent), len(recording)))
        else:
            print("[warn] 告警未推送；%ds 后允许下次探针重试，不占用成功冷却"
                  % ALERT_RETRY_SEC)
    else:
        print("[warn] 停写 %d / 空转 %d / 录音异常 %d，等待重试或冷却" % (len(stale), len(silent), len(recording)))

    _save_state(state)
    return 1


if __name__ == "__main__":
    sys.exit(main())
