# -*- coding: utf-8 -*-
"""采集异常兜底告警：写桌面文件 + 本地日志（不依赖飞书/邮件）。

为什么需要它
------------
2026-10-22 16:44 起八路监控全停，到 23:47 才被人发现 —— 整整 7.5 小时。
当时的三个"告警通道"全是哑的：飞书 `backend=dry_run` 推不出去、看板没人打开、
桌面没有任何提示文件。

飞书那条路已确认走不通（豆包账号应用被平台策略拒，`code=21001`），所以这里做
最不依赖外部服务的一层：**只要看板进程还活着，异常就会落到桌面**。

判据沿用 anchor_manager 的状态机：`stalled`（进程在但探测停写）与
`stopped`（该在跑却没进程）才算异常；`disabled`（用户主动停用）和
`ok`（没播但探测在写）都不报。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STATE_PATH = DATA / "board_alert_state.json"
LOG_PATH = DATA / "board_alerts.log"

COOLDOWN_SEC = 1800          # 同一路 30 分钟内只提醒一次，避免刷屏
DESKTOP_NAME = "采集异常.txt"


def _desktop_path() -> Path | None:
    try:
        d = Path(os.environ.get("USERPROFILE", "")) / "Desktop"
        return d if d.is_dir() else None
    except Exception:
        return None


def _load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(STATE_PATH) + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


def _append_log(text: str) -> None:
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(text.rstrip() + "\n")
    except OSError:
        pass


def _fmt_age(sec) -> str:
    if sec is None:
        return "无数据"
    if sec < 90:
        return "%d 秒" % sec
    if sec < 5400:
        return "%d 分钟" % round(sec / 60)
    return "%.1f 小时" % (sec / 3600)


def check_and_alert(roster: dict, status: dict | None = None,
                    now: float | None = None) -> dict:
    """检查一次；有问题就落桌面 + 日志。返回本轮的判定结果（便于测试）。"""
    import anchor_manager as mgr

    now = now if now is not None else time.time()
    status = status or mgr.status_all(roster)
    health = status["health"]
    bad = [a for a in status["anchors"]
           if a["enabled"] and a["state"] in ("stalled", "stopped", "silent")]

    state = _load_state()
    fresh = []
    for a in bad:
        last = float(state.get(a["tag"], 0) or 0)
        if now - last >= COOLDOWN_SEC:
            fresh.append(a)

    desktop = _desktop_path()
    dfile = (desktop / DESKTOP_NAME) if desktop else None

    if not bad:
        # 全部恢复：清桌面提示 + 清冷却
        if dfile and dfile.exists():
            try:
                dfile.unlink()
            except OSError:
                pass
            _append_log("[%s] 全部恢复正常，桌面提示已清除" %
                        time.strftime("%Y-%m-%d %H:%M:%S"))
        if state:
            _save_state({})
        return {"bad": 0, "alerted": 0, "recovered": True}

    # 有异常：只在有新告警（或桌面文件不存在）时重写，避免每 5 分钟改 mtime
    if fresh or (dfile and not dfile.exists()):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        lines = ["[%s] 采集异常 %d 路（共 %d 路启用）" % (ts, len(bad), health["enabled"]), ""]
        for a in bad:
            why = {"stalled": "探测 %s 前就没再更新" % _fmt_age(a["watch_age_sec"]),
                   "stopped": "进程未运行",
                   "silent": "在播但弹幕 %s 无新行" % _fmt_age(a["danmu_age_sec"])}[a["state"]]
            lines.append("- %s：%s" % (a["tag"], why))
        lines += [
            "",
            "处置：",
            "  1) 打开看板 https://weiguoshen.cn:8443 → 主播页 → 该行右侧点「启动」",
            "     （名册 enabled=true 只代表「开机自启会拉起它」，进程死了仍要点一下）",
            "  2) 或在命令行跑：python scripts\\launch_all.py --headed",
            "     （无头会被抖音风控拦，必须带 --headed）",
            "",
            "本文件由看板自动维护，恢复正常后会自动删除。",
        ]
        text = "\n".join(lines)
        try:
            if dfile:
                desktop.mkdir(parents=True, exist_ok=True)
                dfile.write_text(text, encoding="utf-8")
        except OSError:
            pass
        _append_log(text)
        for a in fresh:
            state[a["tag"]] = now
        _save_state(state)

    return {"bad": len(bad), "alerted": len(fresh), "recovered": False,
            "tags": [a["tag"] for a in bad]}
