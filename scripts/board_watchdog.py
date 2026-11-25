#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""看板看护器：给 8765 直播辅助看板（assist_board_server.py）配「死了自动拉起」。

为什么需要它（2026-10-21）
--------------------------
六路监控有 run_watchdog 守护所以稳，看板一直「裸奔」——任何一次清场/意外/崩溃
都没人管，于是反复死。本脚本给看板补上同一层守护：探活 → 死了拉起 → 连续失败告警。

与 run_watchdog 的关键差别（都是本机实测逼出来的）
--------------------------------------------------
1. **判活只用端口探活**（GET /api/health 返回 200，免鉴权），**禁止进程枚举**：tasklist / WMI /
   Get-Process 在同一时刻给过 0/4/28 三种结果，完全不可信。
2. **不能上 pythonw**：看板内部有 print，pythonw 下 sys.stdout 为 None 会秒退。
   正确形态 = python.exe + CREATE_NO_WINDOW（六路既有实践，桌面无窗）。
3. **不依赖 schtasks 触发**：schtasks 链路对 board 时好时坏（「run 过期 once 任务」
   报成功但不执行）。本脚本自己长驻，schtasks 只负责把它拉起来。

探活间隔为何取 10s 而不是 60s
------------------------------
验收要求「杀掉 board 后 60s 内恢复」。若探活间隔就是 60s，最坏情况 = 60s 等待 +
启动耗时 ≈ 63s，卡线失败。故探活 10s 一次（本地 GET，成本可忽略），拉起后仍按
60s 窗口复探；日志只在实际异常/恢复时写，不刷屏。

用法
----
    python scripts/board_watchdog.py             # 常驻（正常形态，开机自启走它）
    python scripts/board_watchdog.py --once      # 只探一轮并退出（人工冒烟用，不写 pidfile）
    python scripts/board_watchdog.py --interval 10
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
BOARD_SCRIPT = ROOT / "scripts" / "assist_board_server.py"
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
PIDFILE = DATA / "board_watchdog.pid"
LOG_PATH = DATA / "board_watchdog.log"
BOARD_LOG = DATA / "board_server.log"

BOARD_PORT = 8765
# 2026-10-22：看板加了鉴权，/snapshot 现在返回 401 —— 探活必须改用免鉴权的
# /api/health。否则看护器会把「活着的看板」判定成死，无限重启。
SNAPSHOT_URL = "http://127.0.0.1:%d/api/health" % BOARD_PORT
HEALTH_URL = SNAPSHOT_URL       # 语义别名（老测试仍引用 SNAPSHOT_URL）
PROBE_TIMEOUT = 5.0          # 单次探活超时（秒）。3s 太紧：看板刚起时首个请求
                             # 要抢 GIL + 建连接，实测偶尔超 3s，会被误判成死。
PROBE_EVERY = 10.0           # 探活间隔（秒）
LAUNCH_WAIT = 60.0           # 拉起后等复探的总时长（秒）
LAUNCH_POLL = 2.0            # 复探轮询间隔（秒）
FAIL_LIMIT = 3               # 连续拉起失败多少次告警

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:                          # 告警是尽力而为：notify 挂掉不能让看护瘫痪
    import notify  # noqa: E402
except Exception:             # pragma: no cover - 环境异常兜底
    notify = None

# 恒直连（同 notify）：urllib 默认吃系统代理，老大开 Clash 时探活会假死
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(msg: str) -> None:
    """打控制台 + 追加 data/board_watchdog.log（隐藏运行下文件日志是排查主渠道）。"""
    try:
        print("[看板看护 %s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)
    except Exception:
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg + "\n")
    except Exception:
        pass


def pid_alive(pid: int) -> bool:
    """⚠️ 绝不能用 os.kill(pid, 0) 判活：Windows 上 os.kill 对 0 号信号会直接
    TerminateProcess，把正在跑的实例杀掉（本项目踩过，实现抄 stream_recorder）。"""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False
    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


# ---------------------------------------------------------------- 探活 / 拉起

def board_alive(timeout: float = PROBE_TIMEOUT,
                url: str = SNAPSHOT_URL) -> bool:
    """端口探活：GET /snapshot 返回 200 即活。唯一可信的判活方式。"""
    try:
        with _OPENER.open(url, timeout=timeout) as r:
            return int(getattr(r, "status", None) or r.getcode()) == 200
    except Exception:
        return False


def resolve_python() -> str:
    """跑看板用的解释器：优先仓内 venv（六路同一套依赖），否则当前解释器。"""
    try:
        if VENV_PY.exists():
            return str(VENV_PY)
    except Exception:
        pass
    return sys.executable


def launch_board(python_exe: Optional[str] = None,
                 log_path: Optional[Path] = None) -> subprocess.Popen:
    """隐藏拉起看板：python.exe + CREATE_NO_WINDOW（pythonw 会秒退，见模块头）。"""
    exe = python_exe or resolve_python()
    path = Path(log_path) if log_path else BOARD_LOG
    try:
        fh: object = open(path, "ab", buffering=0)   # 看板只有启动横幅，留档即可
    except Exception:
        fh = subprocess.DEVNULL
    try:
        proc = subprocess.Popen(
            [exe, str(BOARD_SCRIPT)],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL, stdout=fh, stderr=fh,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    finally:
        try:
            if hasattr(fh, "close"):
                fh.close()                            # Popen 已复制句柄给子进程
        except Exception:
            pass
    return proc


def wait_until_alive(seconds: float = LAUNCH_WAIT, interval: float = LAUNCH_POLL,
                     probe: Callable[[], bool] = board_alive) -> bool:
    """拉起后复探：到期前探到 200 返回 True。"""
    deadline = time.time() + seconds
    while True:
        if probe():
            return True
        if time.time() >= deadline:
            return False
        time.sleep(interval)


# ---------------------------------------------------------------- 告警 / 防双开

def alert_human(text: str, key: str = "board-watch",
                cooldown: float = 1800.0) -> bool:
    """飞书告警（webhook 通道现成）；通道不可用时只落日志，绝不抛。"""
    if notify is None:
        log("告警通道不可用（notify 未加载），仅落日志：%s" % text)
        return False
    try:
        return bool(notify.send_text(text, key=key, cooldown=cooldown))
    except Exception as e:
        log("告警发送失败：%s" % e)
        return False


def _read_pid(path: Path) -> int:
    try:
        return int(Path(path).read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def acquire_pidfile(path: Path = PIDFILE, alive: Callable[[int], bool] = pid_alive) -> bool:
    """True=本实例拿到看守权；False=已有活着的看护器（防双开）。"""
    pid = _read_pid(path)
    if pid and pid != os.getpid() and alive(pid):
        return False
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(str(os.getpid()), encoding="utf-8")
    return True


# ---------------------------------------------------------------- 决策核心

class Guard:
    """一轮 = 探活 → 死了拉起 → 复探 → 计数/告警。纯逻辑，依赖全可注入（便于单测）。"""

    def __init__(self, probe: Callable[[], bool] = board_alive,
                 launch: Callable[[], object] = launch_board,
                 wait: Callable[[], bool] = wait_until_alive,
                 alert: Callable[..., bool] = alert_human,
                 fail_limit: int = FAIL_LIMIT):
        self.probe = probe
        self.launch = launch
        self.wait = wait
        self.alert = alert
        self.fail_limit = fail_limit
        self.fails = 0              # 连续「拉起后仍死」的次数
        self.revives = 0            # 累计复活次数（便于日志/排障）

    def _alert(self, text: str, key: str) -> bool:
        """告警是尽力而为：通道抛异常绝不能把看护循环打崩。"""
        try:
            return bool(self.alert(text, key=key))
        except Exception as e:
            log("告警调用失败（忽略，继续看护）：%s" % e)
            return False

    def tick(self) -> str:
        """返回 alive / revived / revive_failed / need_human。"""
        if self.probe():
            if self.fails:
                log("看板已恢复，连续失败计数 %d 清零" % self.fails)
                self.fails = 0
            return "alive"

        log("探活失败：%s 无响应 → 拉起看板" % SNAPSHOT_URL)
        try:
            self.launch()
        except Exception as e:
            log("拉起失败：%s" % e)

        if self.wait():
            self.revives += 1
            self.fails = 0
            log("看板已复活（累计第 %d 次），%s 恢复 200" % (self.revives, SNAPSHOT_URL))
            self._alert("[看板] 8765 曾掉线，看护器已自动拉起（本轮无需人工）",
                        "board-relaunch")
            return "revived"

        self.fails += 1
        log("拉起后复探 %.0fs 仍失败（连续 %d/%d）"
            % (LAUNCH_WAIT, self.fails, self.fail_limit))
        if self.fails >= self.fail_limit:
            self._alert("[看板] 看护拉起 3 次仍失败，需人工介入", "board-watch")
            log("已告警：连续 %d 次拉起无效；计数清零后继续重试" % self.fails)
            self.fails = 0
            return "need_human"
        return "revive_failed"


# ---------------------------------------------------------------- main

def _rotate_log(max_bytes: int = 5 * 1024 * 1024) -> None:
    """日志超 5MB 轮转成 .old，防止无限膨胀（同 run_watchdog）。"""
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > max_bytes:
            LOG_PATH.replace(LOG_PATH.with_suffix(".log.old"))
    except Exception:
        pass


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="看板看护：端口探活 + 死了自动拉起")
    ap.add_argument("--once", action="store_true",
                    help="只探一轮就退出（人工冒烟；不写 pidfile、不常驻）")
    ap.add_argument("--interval", type=float, default=PROBE_EVERY,
                    help="探活间隔秒数（默认 %.0f）" % PROBE_EVERY)
    args = ap.parse_args(argv)

    DATA.mkdir(parents=True, exist_ok=True)
    _rotate_log()

    # 显式传依赖（而不是吃 Guard 的默认参数）：调用时解析，便于测试与热替换
    guard = Guard(probe=board_alive, launch=launch_board,
                  wait=wait_until_alive, alert=alert_human)
    if args.once:
        action = guard.tick()
        print("once: %s（board_alive=%s）" % (action, board_alive()))
        return 0 if action in ("alive", "revived") else 1

    if not acquire_pidfile():
        log("已有看护器在跑（pid=%d），本实例退出（防双开）" % _read_pid(PIDFILE))
        return 0

    def _terminate(signum, frame):
        raise KeyboardInterrupt

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _terminate)
        except Exception:
            pass

    log("启动：探活 %s（每 %.0fs / 超时 %.0fs）｜拉起后复探 %.0fs｜连续 %d 次失败告警｜pid=%d"
        % (SNAPSHOT_URL, args.interval, PROBE_TIMEOUT, LAUNCH_WAIT, FAIL_LIMIT, os.getpid()))
    try:
        while True:
            guard.tick()
            time.sleep(max(1.0, args.interval))
    except KeyboardInterrupt:
        log("收到退出信号，退出（看板进程不随之退出）")
        return 0
    finally:
        try:
            PIDFILE.unlink(missing_ok=True)     # 退出即清，防双开靠它
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
