#!/usr/bin/env python
"""看门狗：把「蹲开播」变成开机即蹲、崩了自动拉起的常驻任务。

为什么需要它（2026-10-04 移植准备）
----------------------------------
watch_anchor_live.py 本身已能长驻，但它崩了/机器重启后没人拉起，且它依赖
一个带调试端口的浏览器。本脚本补上「守护」这一层：

1. 确保 Chrome（调试端口 9222）活着——没有就自动启动（路径自动探测）；
2. 拉起 watch_anchor_live.py 子进程并透传参数（--sec-uid/--tag/--start/--llm...）；
3. 子进程退出后**退避重启**（60s → 120s → ... 封顶 900s）；若子进程刚启动就秒退
   （存活 <60s）连续 5 次，判定为配置类故障，停止重启并显式告警，避免死循环；
4. 每 30s 顺带体检 9222 端口，挂了自动重启浏览器（不打断子进程——
   watch 对浏览器中断自带「暂停喊人 + 300s 重试」逻辑）；
5. Ctrl+C / 终止信号 → 结束子进程并退出。

用法
----
    python scripts/run_watchdog.py --sec-uid <MS4w...> --tag <标签> --start --llm
环境变量
----
    WATCH_CHROME           chrome 可执行文件路径（默认自动探测）
    WATCH_BROWSER_PROFILE  浏览器 user-data-dir（Windows 默认 D:/wb-browser-profile，
                           其他平台默认 <仓库>/browser-profile）
    WATCH_NODE / WATCH_CDP 透传给 watch 脚本（见 watch_anchor_live.resolve_*）
    WATCH_HEADLESS=1       无头启动浏览器（云端/Docker 用；扫码需另用 CDP 截图）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.local_installation import bootstrap_entry, installed_layout
bootstrap_entry()
from file_lock import FileLock  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import notify  # noqa: E402  告警推送（缺凭据/失败时静默）
from scripts.local_context import (activate_customer_environment, browser_owner_valid,
    customer_stop_requested, load_customer_context, publish_role_ready, validate_role_tag)

CUSTOMER = load_customer_context()
activate_customer_environment(CUSTOMER)
DATA = Path(CUSTOMER["data"]) if CUSTOMER else ROOT / "data"


def _notify(*args, **kwargs):
    if CUSTOMER:
        kwargs["config_path"] = CUSTOMER["config"]
    return notify.send_text(*args, **kwargs)

_CUSTOMER_IDENTITY = None

RESTART_BACKOFF = [60, 120, 240, 480, 900]   # 子进程退出后的重启退避（秒）
FAST_EXIT_SECONDS = 60                        # 存活短于该值视为「秒退」
FAST_EXIT_LIMIT = 5                           # 连续秒退次数上限
PORT_CHECK_SECONDS = 30                       # 浏览器体检间隔
DEBUG_PORT = CUSTOMER["cdp_port"] if CUSTOMER else 9222

_LOG_PATH: Optional[Path] = None              # 文件日志路径（main 里按 tag 定）

# 浏览器候选（2026-10-27：新增 --browser 切换）
#   本机 Chrome 装在 Program Files (x86)，PE Machine=0x014C → **32 位**，
#   用户态地址空间只有 2 GB。八路采集 + 长时间直播把主进程顶到上限后，
#   调试端口就探不通、被判定卡死重启，形成「占满→重启→又占满」的循环。
#   Edge 在同一个目录但 PE Machine=0x8664（64 位），系统自带、不用装东西，
#   用 WATCH_BROWSER=edge 切过去即可（exe 和 profile 一起换，见 resolve_profile）。
_BROWSER_CANDIDATES: Dict[str, List[str]] = {
    "chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ],
    "edge": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ],
}
_CHROME_CANDIDATES = _BROWSER_CANDIDATES["chrome"]


def log(msg: str) -> None:
    """打控制台 + 追加 data/watchdog_<tag>.log（2026-10-07 起看门狗隐藏运行，
    控制台没人看，文件日志是排查主渠道；写失败不影响主流程）。"""
    try:
        # pythonw（零窗口形态）下 sys.stdout 为 None，print 会抛——文件日志才是主渠道
        print("[看门狗 %s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)
    except Exception:
        pass
    if _LOG_PATH is not None:
        try:
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg + "\n")
        except Exception:
            pass


def browser_name(env: Optional[Dict[str, str]] = None) -> str:
    """当前用哪个浏览器：edge（默认，2026-10-28 起）| chrome。

    ★ 默认从 chrome 改成 edge 的原因：本机 Chrome 是 32 位（PE 0x014C），
      用户态地址空间 2 GB，八路跑一天就顶到上限 → 端口探不通 → 判卡死重启
      → 又占满。Edge 是 64 位，没有这个上限。

    ★ 为什么必须写进默认值而不是只靠 `--browser edge` 参数：
      2026-10-28 老大照桌面告警去命令行跑 `launch_all.py --headed`（没带
      --browser），当天就把浏览器切回了 Chrome —— 参数式切换只要有人手敲一次
      就会退回去。默认值才能保证「任何入口起的都是 Edge」。

    WATCH_BROWSER=chrome 仍是显式回滚手段。
    """
    env = os.environ if env is None else env
    explicit = (env.get("WATCH_BROWSER") or "").strip().lower()
    if explicit in ("chrome", "edge"):
        return explicit
    # 没显式指定：有 Edge 就用 Edge，没有才退回 Chrome（云/Linux 上没有 Edge）
    if any(Path(p).exists() for p in _BROWSER_CANDIDATES.get("edge", [])):
        return "edge"
    return "chrome"


def resolve_chrome(env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """定位浏览器可执行文件：WATCH_CHROME 显式指定 → PATH → WATCH_BROWSER 对应清单。"""
    env = os.environ if env is None else env
    installed = installed_layout()
    if installed is not None:
        selected = installed["browser_executable"]
        return str(selected) if selected is not None and selected.is_file() else None
    explicit = env.get("WATCH_CHROME") or ""
    if explicit:
        return explicit if Path(explicit).exists() else None
    cand: List[str] = [shutil.which("google-chrome") or "", shutil.which("chromium") or "", shutil.which("chrome") or ""]
    if browser_name(env) == "edge":
        cand += _BROWSER_CANDIDATES["edge"]
    else:
        cand += _BROWSER_CANDIDATES["chrome"]
        local = env.get("LOCALAPPDATA")
        if local:
            cand.append(str(Path(local) / "Google/Chrome/Application/chrome.exe"))
    for c in cand:
        if c and Path(c).exists():
            return c
    return None


def resolve_profile(env: Optional[Dict[str, str]] = None) -> str:
    """浏览器 user-data-dir：环境变量 → 按浏览器分别默认。

    Windows 沿用历史目录（登录态在那里，本机零迁移）；其他平台（云/Linux）
    默认放仓库内 browser-profile/（随迁移一起拷贝，注意不要提交进 git）。

    ★ Edge 必须用自己的 profile 目录：两个浏览器不能同时开同一个 user-data-dir
    （撞 SingletonLock），而且各自会往里写自己的东西。切浏览器 = 换 exe + 换 profile，
    所以这两个函数必须成对读同一个 WATCH_BROWSER。
    """
    env = os.environ if env is None else env
    if env.get("WATCH_BROWSER_PROFILE"):
        return env["WATCH_BROWSER_PROFILE"]
    if os.name == "nt":
        if browser_name(env) == "edge":
            return "D:/wb-edge-profile"
        return "D:/wb-browser-profile"
    return str(ROOT / "browser-profile")


def port_alive(timeout: float = 3.0) -> bool:
    """调试端口是否可访问。"""
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/json/version" % DEBUG_PORT, timeout=timeout):
            return True
    except Exception:
        return False


def clear_crash_flag(profile: str) -> None:
    """清除 Chrome profile 的崩溃标记（2026-10-05 真机坑）。

    Chrome 非正常关闭（直接关机/强杀）后，Preferences 里 profile.exit_type=Crashed
    → 下次启动走「崩溃恢复」：弹『要恢复页面吗』气泡 + 标签页可能停在假 douyin.com
    或卡死加载中，页面 JS 主线程排队阻塞 → 所有 CDP evaluate 40s 超时，六实例全挂。
    启动浏览器前把标记改成 Normal，Chrome 就当上次正常退出，不走恢复流程。
    文件不存在/损坏时静默跳过（新 profile 首启没有该文件属正常）。
    """
    prefs = Path(profile) / "Default" / "Preferences"
    try:
        d = json.loads(prefs.read_text(encoding="utf-8"))
        prof = d.setdefault("profile", {})
        changed = prof.get("exit_type") != "Normal" or prof.get("exited_cleanly") is not True
        prof["exit_type"] = "Normal"
        prof["exited_cleanly"] = True
        if changed:
            prefs.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            log("已清除浏览器崩溃恢复标记（%s）" % prefs)
    except FileNotFoundError:
        pass
    except Exception as e:
        log("清除崩溃标记失败（不影响启动）：%s" % e)


def clear_session_files(profile: str) -> int:
    """删掉 Chrome 的会话快照，**阻止重启后把上一轮的标签页恢复回来**（2026-10-27）。

    老大提的循环：「越重启占得越多，重启又不去掉之前的，占满又重启」。
    支点就在这里 —— `Default/Sessions/` 下的 Session_* / Tabs_* 是 Chrome 用来
    「恢复上次页面」的快照，Chrome 一旦非正常退出（被 taskkill 就算），
    下次启动会照着它把那些页面全拉回来，于是新浏览器一睁眼就背着上一轮的全部
    renderer 进程，很快又到上限、又被判卡死。

    只在**拉起新浏览器之前**调（端口已通时不动，浏览器运行中这些文件被占用）。
    返回删除个数。删不掉不算失败：文件被锁就跳过，Chrome 起来后行为照旧。
    """
    sd = Path(profile) / "Default" / "Sessions"
    if not sd.is_dir():
        return 0
    n = 0
    for p in sd.iterdir():
        if not p.is_file():
            continue
        if not (p.name.startswith("Session_") or p.name.startswith("Tabs_")):
            continue
        try:
            p.unlink()
            n += 1
        except Exception:
            pass
    if n:
        log("已清空浏览器会话快照 %d 个（重启后不再恢复上一轮标签页）" % n)
    return n


def _proc_name(env: Optional[Dict[str, str]] = None) -> str:
    """当前浏览器的进程名。换 Edge 后必须跟着变 —— 否则找旧实例永远找不到。"""
    return "msedge.exe" if browser_name(env) == "edge" else "chrome.exe"


def _main_procs(name: str) -> list:
    """列出该进程名的**主进程** [(pid, cmdline), ...]。

    ★ 2026-10-28：以前用 `wmic process ... get ProcessId,CommandLine /format:csv`，
    实测这台机器上 csv 只吐出 `Node,ProcessId` 两列、**CommandLine 整列不见了**，
    于是按 profile 匹配永远为 0 ……
      - `ensure_browser` 的「先清残留」分支从来清不掉东西；
      - `kill_browser()`（过验证码要关旧浏览器）报「已关闭 0 个」，
        旧的无头实例还在，新实例一看端口通就直接复用。
    现在优先走 CIM，wmic 只作后备。
    """
    rows: list = []
    try:
        ps = (
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
            "Get-CimInstance Win32_Process -Filter \"Name='%s'\" | "
            "Where-Object { $_.CommandLine -and $_.CommandLine -notlike '*--type=*' } | "
            "ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }" % name
        )
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        for line in (out or "").splitlines():
            line = line.strip()
            if "|" not in line:
                continue
            pid_s, cmd = line.split("|", 1)
            if pid_s.strip().isdigit() and cmd.strip():
                rows.append((int(pid_s.strip()), cmd))
    except Exception:
        rows = []
    if rows:
        return rows
    # 后备：wmic（老系统；输出列是否完整不保证，仅作兜底）
    try:
        out = subprocess.run(["wmic", "process", "where", "name='%s'" % name,
                              "get", "ProcessId,CommandLine", "/format:csv"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=20).stdout
        for line in (out or "").splitlines():
            if "user-data-dir" not in line.lower():
                continue
            parts = line.strip().split(",")
            for p in parts:
                if p.strip().isdigit():
                    rows.append((int(p.strip()), line))
                    break
    except Exception:
        pass
    return rows


def _profile_pids(profile: str, env: Optional[Dict[str, str]] = None) -> list:
    """用我们这个 profile 起的浏览器**主进程**。

    2026-10-23 真机：调试端口挂了，但同 profile 的 Chrome 还在（卡死状态）。
    此时再拉一个新的会撞 profile 锁 → 一个个都「未响应」，任务栏堆出六个
    「Google Chrome 未响应，是否立即重新启动？」。所以先认人，再动手。

    ★ 2026-10-27：进程名不能写死 chrome.exe。切到 Edge 后会一个都匹配不到，
      于是「卡死的旧实例清不掉 → 新实例撞 SingletonLock → 永远拉不起来」，
      而且日志里看起来一切正常（只是反复「已启动浏览器」但端口不通）。
    """
    key = profile.replace("\\", "/").lower()
    pids = []
    for pid, cmd in _main_procs(_proc_name(env)):
        if key not in cmd.replace("\\", "/").lower():
            continue
        pids.append(pid)
    return pids


_LAUNCH_LOCKS = threading.local()


def _acquire_launch_lock(timeout: float = 60.0) -> bool:
    """跨进程只让一个去拉浏览器（八路 watchdog 同时发现端口不通时会打群架）。"""
    directory = Path(CUSTOMER["data"]) if CUSTOMER else ROOT / "data"
    lock = FileLock(directory / ".chrome_launch", timeout=timeout)
    deadline = time.monotonic() + timeout
    while True:
        try:
            if lock.acquire(blocking=False):
                stack = getattr(_LAUNCH_LOCKS, "stack", [])
                stack.append(lock)
                _LAUNCH_LOCKS.stack = stack
                return True
        except OSError as exc:
            log("浏览器启动锁不可用：%s" % type(exc).__name__)
            return False
        if port_alive(timeout=0.8) or time.monotonic() >= deadline:
            return False
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))


def _release_launch_lock() -> None:
    stack = getattr(_LAUNCH_LOCKS, "stack", [])
    if stack:
        stack.pop().release()  # 稳定 sidecar 绝不 unlink；进程硬退出由内核释放锁。


CHROME_MARK = DATA / ".chrome_last_launch"
NEWBORN_GRACE = 150.0      # 刚起过的 Chrome 给 2.5 分钟初始化，别当尸体清掉
# 端口探测宽限（2026-10-27）：CDP 在内存吃紧时会短暂无响应，单次失败就杀 Chrome
# 会把活着的当尸体 —— 2026-10-27 17:35 就是这么把正常浏览器清掉重起的。
PORT_PROBE_TRIES = 3
PORT_PROBE_GAP = 5.0

# 标签页巡检：同一 URL 的重复 tab 直接关掉（2026-10-27）
_TAB_SWEEP_MARK = DATA / ".tab_sweep_last"
TAB_SWEEP_INTERVAL = 900.0      # 15 分钟内最多巡检一次，8 路都会调进来


def _list_pages() -> list:
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/json/list" % DEBUG_PORT, timeout=3) as r:
            return [t for t in json.loads(r.read().decode("utf-8", "replace"))
                    if t.get("type") == "page"]
    except Exception:
        return []


def sweep_duplicate_tabs(min_gap: float = TAB_SWEEP_INTERVAL) -> int:
    """关掉 URL 完全相同的多余标签页，顺便把当前 tab 数写进日志。

    这是「浏览器越跑越大」的兜底，不是主力修复（主力是 capture_stream 抓完就关）。
    只关**同一个 URL** 的重复项 —— 8 路同时开播时本来就该有 8 个不同的直播间 tab，
    那种不能动；而同一个直播间出现 2 个页面，一定是漏下来的孤儿。
    """
    try:
        now = time.time()
        if _TAB_SWEEP_MARK.exists() and now - _TAB_SWEEP_MARK.stat().st_mtime < min_gap:
            return 0
        _TAB_SWEEP_MARK.parent.mkdir(parents=True, exist_ok=True)
        _TAB_SWEEP_MARK.write_text(str(int(now)), encoding="utf-8")
    except Exception:
        pass

    pages = _list_pages()
    if not pages:
        return 0
    seen, killed = {}, 0
    for t in pages:
        url = t.get("url") or ""
        if not url or url.startswith("chrome://"):
            continue
        if url in seen:
            try:
                req = urllib.request.Request(
                    "http://127.0.0.1:%d/json/close/%s" % (DEBUG_PORT, t.get("id")),
                    method="PUT")
                urllib.request.urlopen(req, timeout=3).read()
                killed += 1
            except Exception:
                pass
        else:
            seen[url] = t.get("id")
    if killed:
        log("标签页巡检：关掉 %d 个重复页（当前剩 %d 个）" % (killed, len(seen)))
    elif len(seen) > 12:
        # 不主动杀（可能是 8 路同时在抓），只留个刻度供排查
        log("标签页巡检：当前 %d 个页面，偏多，留意是否有泄漏" % len(seen))
    return killed


def port_alive_with_grace(tries: int = PORT_PROBE_TRIES,
                          gap: float = PORT_PROBE_GAP) -> bool:
    """连续失败 `tries` 次才认定调试端口真的不通（约 10 秒宽限）。"""
    for i in range(max(1, tries)):
        if port_alive():
            return True
        if i < tries - 1:
            time.sleep(gap)
    return False


def minimize_windows_of(pid: int) -> int:
    """把该进程的可见顶层窗口最小化，返回最小化个数（2026-10-27）。

    老大要「有头」（headless 会触发抖音风控）但不想被窗口打扰。Chrome 没有
    `--start-minimized`，只能在起来之后用 Win32 收起来。只在 ensure_browser
    拉**新**浏览器时调一次，不是每次开播都调。

    ⚠️ 64 位下必须声明 argtypes，否则 HWND 会被截成 32 位导致最小化错窗口。
    """
    if os.name != "nt":
        return 0
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]

    SW_MINIMIZE = 6
    found: List[int] = []

    def _cb(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    try:
        user32.EnumWindows(WNDENUMPROC(_cb), 0)
        for hwnd in found:
            user32.ShowWindow(hwnd, SW_MINIMIZE)
    except Exception as e:  # noqa: BLE001 最小化失败不影响采集
        log("最小化浏览器窗口失败（忽略）：%s" % e)
        return 0
    return len(found)


def _recent_launch(sec: float = NEWBORN_GRACE) -> bool:
    try:
        return time.time() - CHROME_MARK.stat().st_mtime < sec
    except OSError:
        return False


def _stamp_launch() -> None:
    try:
        CHROME_MARK.write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        pass


_MISMATCH_WARN_TS = 0.0


def _warn_browser_mismatch(env: Optional[Dict[str, str]] = None) -> None:
    """端口通、但上面跑的不是配置的那个浏览器时喊一声（30 分钟最多一次）。

    2026-10-28：`ensure_browser` 只要 `port_alive()` 就直接复用，于是
    「配置写成 edge、实际连的还是 Chrome」这种状态**完全没有痕迹** ——
    老大把浏览器跑回 32 位 Chrome 后，日志里一句提示都没有。这里补一句。
    """
    global _MISMATCH_WARN_TS
    now = time.time()
    if now - _MISMATCH_WARN_TS < 1800:
        return
    want = browser_name(env)
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/json/version" % DEBUG_PORT,
                                    timeout=4) as r:
            v = json.loads(r.read().decode("utf-8", "replace"))
        ua = "%s %s" % (v.get("Browser") or "", v.get("User-Agent") or "")
    except Exception:
        return
    if "Edg/" in ua or "Edg " in ua:
        got = "edge"
    elif "Chrome/" in ua:
        got = "chrome"
    else:
        return
    if got == want:
        return
    _MISMATCH_WARN_TS = now
    log("调试端口 %d 上是 %s，与配置的 %s 不一致 —— 换浏览器必须先关掉旧浏览器，"
        "否则 ensure_browser 会一直复用旧的（本路继续用 %s）"
        % (DEBUG_PORT, got, want, got))


def ensure_browser(env: Optional[Dict[str, str]] = None) -> bool:
    """确保带调试端口的浏览器在跑；不在则启动。返回是否可用。

    2026-10-23 加两道闸（那天任务栏堆了六个「未响应」框）：
    1. 全局启动锁：八路同时发现端口不通时，只让一个去拉，其余等。
    2. 先清残留：端口不通但同 profile 的浏览器还在 = 卡死的旧实例，
       不清掉就再拉只会撞 profile 锁，越拉越多个窗口。
    """
    if CUSTOMER:
        # The application owns one shared browser. Roles never adopt, launch,
        # enumerate, kill, or sweep a browser merely because a port responds.
        return browser_owner_valid(CUSTOMER) and port_alive()
    if port_alive():
        # 浏览器已在跑：顺手做一次低频巡检（15 分钟一次），清掉重复的孤儿标签页。
        # 放在这里是因为八路每轮都会走到，天然形成节流，不需要额外定时器。
        try:
            sweep_duplicate_tabs()
        except Exception as e:      # 巡检永远不能影响主流程
            log("标签页巡检异常（忽略）：%s" % e)
        try:
            _warn_browser_mismatch(env)
        except Exception:
            pass
        return True
    chrome = resolve_chrome(env)
    if not chrome:
        log("未找到 %s（设 WATCH_CHROME 指定路径，或 WATCH_BROWSER=edge 切浏览器），"
            "无法自动拉起浏览器" % browser_name(env))
        return False
    profile = resolve_profile(env)
    Path(profile).mkdir(parents=True, exist_ok=True)

    got = _acquire_launch_lock()
    if not got:
        return port_alive()  # 未取得锁不得清理旧浏览器或另起一个。
    try:
        if port_alive():
            return True                      # 等锁期间别人已经拉好了
        if _recent_launch():
            # 刚刚才起过浏览器：它可能还在初始化（登录态/首屏很慢）。这时把"还没
            # 就绪"当成"卡死"去杀，八路就会互相把对方刚拉起来的 Chrome 掐掉，
            # 于是屏幕上不断弹新的抖音窗口（2026-10-25 13:48 就是这么滚起来的）。
            for _ in range(int(NEWBORN_GRACE)):
                time.sleep(1)
                if port_alive():
                    return True
            log("浏览器起了 %.0fs 仍不通，按卡死处理" % NEWBORN_GRACE)
        stale = _profile_pids(profile, env)
        if stale:
            # ★ 杀之前先确认是真的死：CDP 在内存吃紧时会短暂无响应
            #   （2026-10-27 整机可用内存只剩 2.2 GB，17:35 把正常浏览器清掉了）
            if port_alive_with_grace():
                log("调试端口短暂无响应但已恢复，不清浏览器")
                return True
            log("调试端口 %d 次探测均不通，判定卡死 → 清掉重起：%s"
                % (PORT_PROBE_TRIES, stale))
            for pid in stale:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True)
            time.sleep(3)
        clear_crash_flag(profile)
        # ★ 2026-10-27：杀完旧实例后把会话快照也清掉，否则新浏览器一启动就把
        #   上一轮那堆标签页全恢复回来 —— 内存立刻又从高位起步，很快再被判定
        #   卡死，形成「占满→重启→又占满」的死循环（老大反馈的就是这个）。
        clear_session_files(profile)
        # --disable-features 只能传一条（多条会互相覆盖），所以按浏览器拼起来
        features = ["CalculateNativeWinOcclusion"]
        if browser_name(env) == "edge":
            # Edge 的「睡眠标签页」会把后台页冻住 → CDP evaluate 直接超时，
            # 表现得和「端口卡死」一模一样。侧边栏 / 首次登录引导也会抢焦点。
            features += ["msSleepingTabs", "msEdgeSidebarV2", "msImplicitSignin",
                         "msEdgeIdentityFeature"]
        cmd = [chrome,
               "--remote-debugging-port=%d" % DEBUG_PORT,
               "--user-data-dir=%s" % profile,
               "--no-first-run", "--no-default-browser-check",
               "--hide-crash-restore-bubble", "--disable-session-crashed-bubble",
               # 免疫系统代理（2026-10-07 真机坑）：老大开 Clash 系统代理后，Chrome 把
               # douyin 流量也绕进代理，代理一抖全监控转圈。抖音是国内站，恒直连。
               "--no-proxy-server",
               # 2026-10-23：八路共用一个 Chrome，任一标签卡住都会让浏览器主线程停下，
               # 弹「Google Chrome 未响应，是否立即重新启动？」。关掉挂起监视。
               "--disable-hang-monitor",
               # 后台标签不要被降频：降频会让 CDP 调用偶尔超时，很像「卡死」。
               "--disable-backgrounding-occluded-windows",
               "--disable-renderer-backgrounding",
               # 2026-10-27：窗口最小化后 Chrome 会把定时器降到 1 次/分钟
               # （最小化 = occluded），页面里的轮询就停了 —— 这是「最小化后漏数据」
               # 的经典成因。三个参数一起关掉节流与遮挡检测。
               "--disable-background-timer-throttling",
               "--disable-features=" + ",".join(features),
               "--disable-ipc-flooding-protection",
               # 直播页自动播放视频是主要卡顿来源；我们只要 DOM/接口，不要画面和声音。
               "--mute-audio",
               "--autoplay-policy=document-user-activation-required",
               "https://www.douyin.com"]
        if (env or os.environ).get("WATCH_HEADLESS") == "1":
            cmd.insert(1, "--headless=new")
        try:
            # detach：浏览器独立于本进程存活
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            _stamp_launch()          # 给后来者一个"刚起过"的信号，别再杀再起
            log("已启动浏览器：%s（browser=%s profile=%s）"
                % (chrome, browser_name(env), profile))
        except Exception as e:
            log("浏览器启动失败：%s" % e)
            return False
        for _ in range(15):            # 最多等 15s
            time.sleep(1)
            if port_alive():
                # 端口就绪说明窗口已建好，收起来别打扰老大（只有拉新浏览器时才走这里）。
                # ★ 人工过验证码时例外：WATCH_NO_MINIMIZE=1 保留窗口给老大操作，
                #   否则他按提示去点浏览器，看到的只有任务栏一个最小化图标。
                if (env or os.environ).get("WATCH_NO_MINIMIZE") == "1":
                    log("窗口保持可见（WATCH_NO_MINIMIZE=1，等人工过验证码）")
                    return True
                n = minimize_windows_of(proc.pid)
                if n:
                    log("已最小化 %d 个浏览器窗口" % n)
                return True
        log("浏览器已启动但 %d 端口未就绪" % DEBUG_PORT)
        return False
    finally:
        if got:
            _release_launch_lock()


def build_watch_cmd(python_exe: str, args: argparse.Namespace) -> List[str]:
    cmd = [python_exe, *(["-B"] if installed_layout() is not None else []), str(ROOT / "scripts" / "watch_anchor_live.py"),
           "--sec-uid", args.sec_uid, "--tag", args.tag]
    if args.start:
        cmd.append("--start")
    if args.llm:
        cmd.append("--llm")
    if args.interval:
        cmd += ["--interval", str(args.interval)]
    if args.max_rounds:
        cmd += ["--max-rounds", str(args.max_rounds)]
    return cmd


_BROWSER_ALERT_FILE = DATA / ".browser_alert.json"
BROWSER_ALERT_WINDOW = 1800.0     # 计数窗口（秒）
BROWSER_ALERT_THRESHOLD = 3       # 窗口内第 N 次掉线才喊人


def _alert_browser_drop(env: Dict[str, str], tag: str = "") -> bool:
    """浏览器掉线告警：只在「窗口内反复掉线」时喊一次，八路共享计数。

    为什么不能直接 notify（2026-10-29 老大截图：飞书群连刷「浏览器掉线」）：
    八路 watchdog 是**八个进程**，而 notify 的 cooldown 是进程内字典 →
    同一件事每路各报一条，看起来像八台机器同时崩。而且偶发掉线自动重拉成功，
    本来就不该叫人，报了只会让人去关告警。

    判据：窗口内累计次数 >= BROWSER_ALERT_THRESHOLD 才发；发完清零。
    跨进程共享一个 json（小竞态最多多报一条，比每条都报好得多）。
    """
    now = time.time()
    try:
        st = json.loads(_BROWSER_ALERT_FILE.read_text(encoding="utf-8"))
        if not isinstance(st, dict):
            st = {}
    except (OSError, ValueError):
        st = {}

    window_start = float(st.get("window_start") or 0)
    if now - window_start > BROWSER_ALERT_WINDOW:
        window_start, count = now, 0
    else:
        count = int(st.get("count") or 0)
    count += 1

    last_alert = float(st.get("last_alert") or 0)
    fired = False
    if count >= BROWSER_ALERT_THRESHOLD and now - last_alert > BROWSER_ALERT_WINDOW:
        bname = "Edge" if browser_name(env) == "edge" else "Chrome"
        _notify(
            "[竞品监控][需人工查看] 浏览器 %d 分钟内掉线 %d 次，看门狗每次都自动重拉，"
            "但这个频率不正常（%s）。常见原因：内存吃紧被系统结束、标签页泄漏。"
            % (round(BROWSER_ALERT_WINDOW / 60), count, bname),
            key="browser-restart", cooldown=BROWSER_ALERT_WINDOW)
        fired = True
        window_start, count, last_alert = now, 0, now

    try:
        _BROWSER_ALERT_FILE.write_text(
            json.dumps({"window_start": window_start, "count": count,
                        "last_alert": last_alert, "ts": now, "tag": tag}),
            encoding="utf-8")
    except OSError:
        pass
    return fired


def _wait_with_port_check(proc: subprocess.Popen, env: Dict[str, str],
                          check_seconds: float = PORT_CHECK_SECONDS,
                          tag: str = "") -> int:
    """等子进程退出，期间每隔 check_seconds 体检浏览器端口（2026-10-05 补缺口）。

    原实现只在子进程退出后的退避等待期体检——watch 子进程「不放弃重试」意味着它
    几乎不退出，浏览器死了没人重拉，六实例只会干等喊人（10-05 真机踩过）。
    返回子进程退出码；体检是附带动作，不影响等待。
    """
    next_browser_check = time.monotonic() + check_seconds
    while True:
        if CUSTOMER and customer_stop_requested(CUSTOMER, _CUSTOMER_IDENTITY):
            # The exact ancestor request is visible to watch/main as well.
            # Keep this owner and its Job alive until normal child cleanup ends.
            while proc.poll() is None:
                time.sleep(.25)
            return proc.returncode
        try:
            return proc.wait(timeout=min(check_seconds, .5) if CUSTOMER else check_seconds)
        except subprocess.TimeoutExpired:
            pass
        if CUSTOMER:
            if time.monotonic() >= next_browser_check:
                # The application restores its browser. The existing watch
                # remains contained and its CDP boundary rejects unknown owners.
                ensure_browser(env)
                next_browser_check = time.monotonic() + check_seconds
            continue
        if not port_alive():
            log("浏览器掉线（子进程运行中体检），尝试重新拉起")
            ensure_browser(env)
            # 不每路都发：八进程各报一条等于刷屏（见 _alert_browser_drop）
            if _alert_browser_drop(env, tag):
                log("已推送「浏览器反复掉线」告警（窗口内第 %d 次）"
                    % BROWSER_ALERT_THRESHOLD)


def plan_backoff(backoff_idx: int, alive_seconds: float,
                 table: Optional[List[int]] = None,
                 healthy_seconds: Optional[float] = None) -> tuple:
    """子进程退出后该等多久再拉起（纯函数，零 IO，供单测）。

    ★ 为什么要有「健康就归零」（2026-11-01 审计 P0）：原实现 ``backoff_idx``
      只增不减 —— 一天里子进程退 5 次（浏览器被顶掉 / 机器休眠 / 手滑 kill），
      之后就**永远**停在 900 秒档，哪怕它随后健康跑了 8 小时。八个实例同时处在这
      状态时，一场直播白丢一刻钟。判据与 ``fast_exits`` 对齐：存活 ≥
      FAST_EXIT_SECONDS 就算健康，退避回到第一档。

    :return: (等待秒数, 下一轮的 backoff_idx)
    """
    table = RESTART_BACKOFF if table is None else table
    healthy = FAST_EXIT_SECONDS if healthy_seconds is None else healthy_seconds
    if alive_seconds >= healthy:
        backoff_idx = 0
    wait = table[min(backoff_idx, len(table) - 1)]
    return wait, backoff_idx + 1


def start_watch_process(command):
    """Windows 在执行第一条指令前纳入本轮私有 Job；浏览器不在此 Job 内。"""
    kwargs = {"cwd": str(ROOT),
              "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    if os.name == "nt":
        from windows_job import spawn_in_job
        return spawn_in_job(command, **kwargs)
    return subprocess.Popen(command, **kwargs)


def write_stop_protocol(path: Path) -> None:
    """声明本进程使用私有子 Job，允许管理器仅停止 watchdog 本体。"""
    from anchor_manager import process_create_time
    pid = os.getpid()
    created = process_create_time(pid)
    if created is None:
        raise RuntimeError("无法核实 watchdog 身份，拒绝发布停止协议")
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"protocol": "owned-watch-job-v1", "pid": pid,
                       "created": created}, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def stop_watch_tree(proc: Optional[subprocess.Popen], timeout: float = 15.0) -> None:
    """回收本次拉起的 watch；Windows 必须在根进程退出前结束整棵子树。

    先 terminate() 会使 taskkill /T 失去查找 main.py 后代的根，因此 Windows
    分支只对本次拥有的存活 PID 使用 taskkill，不按进程名称清理。回收失败要
    明确报错，不能把仅杀掉 watch 当作成功。根已退出时不再追杀旧 PID，以免
    PID 被无关进程重用。标准启动入口已使用 Job，根先死/看门狗硬杀也会回收；
    taskkill 分支仅兼容没有 Job 的外部调用者。
    """
    if proc is None:
        return
    job = getattr(proc, "_watch_job", None)
    if job is not None:
        # 根 watch 已退出也必须关闭 Job：它可能留下仍活着的 main 后代。
        job.close(timeout=timeout)
        proc.wait(timeout=timeout)
        return
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            if proc.poll() is None:
                raise RuntimeError("无法回收 watch 进程树 pid=%s" % proc.pid) from exc
        else:
            if result.returncode != 0 and proc.poll() is None:
                raise RuntimeError("回收 watch 进程树失败 pid=%s rc=%s"
                                   % (proc.pid, result.returncode))
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("回收后 watch 仍未退出 pid=%s" % proc.pid) from exc
    else:
        # 保留其他平台现有的直接子进程回收行为。
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=timeout)


def main() -> int:
    ap = argparse.ArgumentParser(description="蹲开播看门狗：守护浏览器 + watch 子进程")
    ap.add_argument("--sec-uid", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--start", action="store_true", help="透传给 watch：开播自动采集")
    ap.add_argument("--llm", action="store_true", help="透传给 watch：启用 LLM 层")
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--max-rounds", type=int, default=0)
    ap.add_argument("--python", default=sys.executable,
                    help="跑 watch 用的 python（默认当前解释器）")
    args = ap.parse_args()
    if CUSTOMER:
        args.python = CUSTOMER["python"]
        validate_role_tag(args.tag)

    data_dir = Path(CUSTOMER["data"]) if CUSTOMER else ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    pidfile = data_dir / ("watchdog_%s.pid" % args.tag)
    # launch_all、健康探针和面板可能同时拉起同一路。PID 文本不是互斥锁；
    # 先持有稳定 sidecar，直到子树/身份文件清理完成才允许下一位 owner。
    instance_lock = FileLock(pidfile)
    if not instance_lock.acquire(blocking=False):
        log("同一路看门狗已运行，拒绝重复启动：%s" % args.tag)
        return 3
    try:
        if CUSTOMER:
            global _CUSTOMER_IDENTITY
            _CUSTOMER_IDENTITY = publish_role_ready(CUSTOMER, "watch", args.tag)
        return _run_owned_watchdog(args, data_dir, pidfile)
    finally:
        instance_lock.release()  # 不删除 .pid.lock；硬退出时由操作系统释放。


def _run_owned_watchdog(args, data_dir: Path, pidfile: Path) -> int:
    env = os.environ
    proc: Optional[subprocess.Popen] = None
    backoff_idx = 0
    fast_exits = 0

    # 文件日志 + pidfile（2026-10-07 隐藏运行形态：控制台不存在了，
    # 排查靠 data/watchdog_<tag>.log；防双开由外层全生命周期 FileLock 负责）
    global _LOG_PATH
    _LOG_PATH = data_dir / ("watchdog_%s.log" % args.tag)
    try:                                    # 日志超 5MB 轮转成 .old，防止无限膨胀
        if _LOG_PATH.exists() and _LOG_PATH.stat().st_size > 5 * 1024 * 1024:
            _LOG_PATH.replace(_LOG_PATH.with_suffix(".log.old"))
    except Exception:
        pass
    pidfile.write_text(str(os.getpid()), encoding="utf-8")
    protocol_path = pidfile.with_suffix(".protocol.json")

    def _terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _terminate)
    try:
        signal.signal(signal.SIGTERM, _terminate)
    except Exception:
        pass

    try:
        if os.name == "nt":
            write_stop_protocol(protocol_path)  # 发布失败时不启动浏览器或采集子树。
        log("启动：tag=%s start=%s llm=%s python=%s"
            % (args.tag, args.start, args.llm, args.python))
        if not ensure_browser(env) and not CUSTOMER:
            log("⚠️ 浏览器暂不可用，继续拉起 watch（watch 侧会暂停喊人重试）")
        while True:
            if CUSTOMER:
                while not ensure_browser(env):
                    _customer_wait(1)
                _customer_wait(0)
            cmd = build_watch_cmd(args.python, args)
            log("拉起子进程：%s" % " ".join(cmd))
            started_at = time.time()
            proc = start_watch_process(cmd)
            try:
                rc = _wait_with_port_check(proc, env, tag=args.tag)
            finally:
                # 每场/异常退出都先清空本轮 Job，再允许退避或重拉。不能覆盖
                # proc 后才清理，否则根已死时的孤儿采集会与下一轮并发。
                stop_watch_tree(proc)
                proc = None
            alive = time.time() - started_at
            log("子进程退出 rc=%s（存活 %.0fs）" % (rc, alive))

            if alive < FAST_EXIT_SECONDS:
                fast_exits += 1
                if fast_exits >= FAST_EXIT_LIMIT:
                    log("⛔ 连续 %d 次秒退，判定为配置类故障（sec_uid 错 / 参数错 / "
                        "python 环境坏），停止重启。请人工排查后重新运行。"
                        % fast_exits)
                    _notify("[竞品监控][需人工处理] 看门狗熔断：%s 连续 %d 次"
                                     "秒退（配置类故障），已停止重启，请人工排查"
                                     % (args.tag, fast_exits),
                                     key="fuse-%s" % args.tag, cooldown=3600.0)
                    return 2
            else:
                fast_exits = 0

            wait, backoff_idx = plan_backoff(backoff_idx, alive)
            log("%.0fs 后重启（第 %d 次退避）" % (wait, backoff_idx))
            if CUSTOMER:
                _customer_wait(wait)
                continue
            for _ in range(int(wait // PORT_CHECK_SECONDS)):
                time.sleep(PORT_CHECK_SECONDS)
                if not port_alive():
                    log("浏览器掉线，尝试重新拉起")
                    ensure_browser(env)
            time.sleep(wait % PORT_CHECK_SECONDS)
    except KeyboardInterrupt:
        log("收到退出信号，回收 watch 进程树并退出")
        return 0
    finally:
        try:
            if proc is not None:
                stop_watch_tree(proc)
        finally:
            try:
                pidfile.unlink(missing_ok=True)     # 防双开 pidfile：退出即清
            except Exception:
                pass
            try:
                marker = json.loads(protocol_path.read_text(encoding="utf-8"))
                if marker.get("pid") == os.getpid():
                    protocol_path.unlink()
            except (OSError, ValueError, AttributeError):
                pass


def _customer_wait(seconds):
    deadline = time.monotonic() + seconds
    while True:
        if customer_stop_requested(CUSTOMER, _CUSTOMER_IDENTITY):
            raise KeyboardInterrupt
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(.25, remaining))


if __name__ == "__main__":
    sys.exit(main())
