#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""看板公网隧道：SSH 反向端口转发（零新二进制，不碰 Windows Defender）。

为什么不是 frp（2026-10-22 实测）
--------------------------------
frp 的 Windows 二进制被 Windows Defender 判成「病毒或潜在的垃圾软件」并直接
锁文件：`Get-FileHash` 报「无法读取文件…文件包含病毒或潜在的垃圾软件」，
`Expand-Archive` 同样失败。要跑就得把 `D:\\wb-bin\\frp` 加进 Defender 排除
项 —— 那是改本机安全策略，不值当。Windows 自带 OpenSSH 能做同样的反向隧道。

链路
----
    本机 127.0.0.1:8765（看板，只听本机）
        ↑ ssh -R 18765:127.0.0.1:8765 root@ECS
    ECS 127.0.0.1:18765（只有本机能访问，GatewayPorts 默认 no）
        ↑ nginx 8443 TLS（复用 weiguoshen.cn 证书）→ https://weiguoshen.cn:8443
    同事浏览器

判活与自愈
----------
直接持有 ssh 的 Popen 句柄用 poll() 判活，**不做进程枚举**（本机 tasklist/WMI
在同一时刻给过 0/4/28 三种结果）。断了 5s 后重连，日志落 data/board_tunnel.log。

用法
----
    python scripts/board_tunnel.py              # 常驻（开机自启走它）
    python scripts/board_tunnel.py --once       # 只探一次（看当前是否在跑）
"""
from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PIDFILE = DATA / "board_tunnel.pid"
LOG_PATH = DATA / "board_tunnel.log"
KEY = Path(os.path.expanduser("~")) / ".ssh" / "id_ed25519_board"

REMOTE_HOST = "47.86.41.209"
REMOTE_PORT = 18765          # ECS 上的回环端口（nginx 8443 反代到这里）
LOCAL_PORT = 8765            # 本机看板
RETRY_SECONDS = 5.0


def log(msg: str) -> None:
    line = "[隧道 %s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    """⚠️ 绝不能用 os.kill(pid, 0)：Windows 上 os.kill 对 0 号信号会直接杀进程。"""
    if pid <= 0:
        return False
    k32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259       # STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def build_cmd(host: str = REMOTE_HOST, remote_port: int = REMOTE_PORT,
              local_port: int = LOCAL_PORT, key: Path = KEY) -> list:
    return [
        "ssh", "-N",
        "-i", str(key),
        "-o", "BatchMode=yes",              # 只用 key，绝不弹密码（无窗口时会卡死）
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "TCPKeepAlive=yes",
        "-o", "ExitOnForwardFailure=yes",   # 端口被占就退出，别假装活着
        "-R", "%d:127.0.0.1:%d" % (remote_port, local_port),
        "root@%s" % host,
    ]


def acquire_pidfile() -> bool:
    DATA.mkdir(parents=True, exist_ok=True)
    if PIDFILE.exists():
        try:
            old = int(PIDFILE.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            old = 0
        if old and old != os.getpid() and pid_alive(old):
            return False
    PIDFILE.write_text(str(os.getpid()), encoding="utf-8")
    return True


def run_forever(host: str, remote_port: int, local_port: int) -> int:
    if not acquire_pidfile():
        log("已有隧道看护在跑（pidfile 活），本实例退出")
        return 0
    if not KEY.exists():
        log("找不到私钥 %s —— 先在 ECS 的 authorized_keys 里登记公钥" % KEY)
        return 2
    log("启动：本机 %d -> %s:%d（每 %ds 检查一次子进程）"
        % (local_port, host, remote_port, int(RETRY_SECONDS)))
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        while True:
            try:
                fh = open(LOG_PATH, "ab", buffering=0)
            except OSError:
                fh = subprocess.DEVNULL
            try:
                proc = subprocess.Popen(
                    build_cmd(host, remote_port, local_port),
                    stdin=subprocess.DEVNULL, stdout=fh, stderr=fh,
                    creationflags=flags)
            finally:
                try:
                    if hasattr(fh, "close"):
                        fh.close()
                except Exception:
                    pass
            log("ssh 已起 pid=%d" % proc.pid)
            rc = proc.wait()
            log("ssh 退出 rc=%s，%.0fs 后重连" % (rc, RETRY_SECONDS))
            time.sleep(RETRY_SECONDS)
    finally:
        try:
            PIDFILE.unlink(missing_ok=True)
        except OSError:
            pass
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="看板 SSH 反向隧道（常驻，断了自动重连）")
    ap.add_argument("--host", default=REMOTE_HOST)
    ap.add_argument("--remote-port", type=int, default=REMOTE_PORT)
    ap.add_argument("--local-port", type=int, default=LOCAL_PORT)
    ap.add_argument("--once", action="store_true", help="只报告当前状态，不常驻")
    args = ap.parse_args(argv)

    if args.once:
        alive = False
        if PIDFILE.exists():
            try:
                pid = int(PIDFILE.read_text(encoding="utf-8").strip())
                alive = pid_alive(pid)
            except (OSError, ValueError):
                alive = False
        log("状态：%s（pidfile=%s）" % ("在跑" if alive else "未运行", PIDFILE))
        return 0 if alive else 1

    return run_forever(args.host, args.remote_port, args.local_port)


if __name__ == "__main__":
    sys.exit(main())
