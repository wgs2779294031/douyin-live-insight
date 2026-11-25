# -*- coding: utf-8 -*-
"""客户CLI现统一委托 local_app.py start，参数以该入口 --help 为准。

以下旧辅助函数保留兼容导入；客户CLI不执行裸PID清场/旧单路拉起。
历史实现说明：手动/开机拉起全部主播监控实例。

2026-10-07 起改为**隐藏运行形态**（经历过三次「窗口被当垃圾关掉」全停后定案）：
  - 看门狗进程 CREATE_NO_WINDOW 隐藏运行，桌面不再有任何黑窗；
  - 排查靠 data/watchdog_<标签>.log（watchdog 自带文件日志）；
  - 防双开靠 data/watchdog_<标签>.pid + PID 存活检查（不再依赖窗口标题）。

2026-10-20 修订：Chrome 默认改**有头**——headless 指纹触发抖音风控（搜索页/
主页出验证码且无人能处理，扫码也无窗口可显示），且运营侧 B2 抓数与本监控
共用同一 Chrome 实例，无头会连带把 B2 卡死。Chrome 窗口可最小化，被误关由
watchdog 30s 体检重拉自愈。云端 Docker 仍强制 WATCH_HEADLESS=1 不受影响。

用法：
  python scripts/launch_all.py            # 拉起全部（已活着的跳过）
  python scripts/launch_all.py --kill     # 先按 pidfile 杀掉全部旧实例再拉起
  python scripts/launch_all.py --headless # 本次强制无头 Chrome（无人值守场景）

注意：名册改动需与 start_watch_autostart.bat 保持同步（bat 现在只调本脚本）。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
DATA = ROOT / "data"
SCRIPT = "scripts\\run_watchdog.py"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import anchor_roster  # noqa: E402

# 2026-10-23（P6）：名册 anchors.json 成为唯一真相源，ANCHORS 变成「读名册的兼容视图」
# —— dashboard_build.py 仍按 (实例名, sec_uid, tag) 三元组消费它，所以保留这个名字和形状。
# 默认模式下名册缺失/损坏时回退到 SEED；required-state 模式不暴露种子视图。
def _bootstrap_anchors():
    try:
        roster, _created = anchor_roster.ensure()
        legacy = anchor_roster.as_legacy_anchors(roster)
        if legacy:
            return legacy
    except Exception:
        pass
    if os.environ.get("WB_REQUIRE_EXISTING_STATE") == "1":
        return []
    return [(a["tag"], a["sec_uid"], a["tag"]) for a in anchor_roster.SEED]

ANCHORS = _bootstrap_anchors() if __name__ != "__main__" else []


def _no_window_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run_hidden(cmd) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          creationflags=_no_window_flags())


def pidfile_path(name: str) -> Path:
    # 2026-10-14：统一用中文 tag——run_watchdog 写的是 watchdog_<tag>.pid
    # （见 run_watchdog.py），此处曾用英文实例名导致防双开永远查空（重复
    # 跑 launch_all 会双开）。调用处的 name 参数即 tag，语义不变。
    return DATA / ("watchdog_%s.pid" % name)


def instance_running(name: str) -> bool:
    """pidfile 里的 PID 还活着（且是 python）→ 视为实例在跑。"""
    pf = pidfile_path(name)
    try:
        pid = int(pf.read_text(encoding="utf-8").strip())
    except Exception:
        return False
    r = _run_hidden(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV"])
    return "python.exe" in (r.stdout or "")


def kill_instance(name: str) -> None:
    """按 pidfile 杀整棵进程树（/T 连带 watch 子进程），并清 pidfile。"""
    pf = pidfile_path(name)
    try:
        pid = int(pf.read_text(encoding="utf-8").strip())
    except Exception:
        return
    _run_hidden(["taskkill", "/F", "/T", "/PID", str(pid)])
    try:
        pf.unlink()
    except Exception:
        pass


def launch(name: str, sec_uid: str, tag: str, headless: bool = False,
           llm: bool = True) -> None:
    if instance_running(tag):
        print("[skip] %s 已在运行（pidfile 活）" % tag)
        return
    env = os.environ.copy()
    # ★ 2026-10-28：默认**有头**，与文件头注释、SOP、老大的要求一致。
    #   这一行以前写的是 `if not headed: env.setdefault("WATCH_HEADLESS","1")`
    #   —— 即**默认无头**，和上面那段注释完全相反。机器 10-27 前没重启过，
    #   八路都是手动 `--headed` 起的，所以这个矛盾一直没暴露；10-28 早上
    #   开机自启（bat 只传 --no-llm --browser edge，没有 --headed）起了
    #   **无头 Edge** → 抖音直接给验证码中间页 → need_human 弹「需要人工过验证码」
    #   而老大根本看不到浏览器窗口。
    #   用 pop 而不是 setdefault：父进程环境里残留的 WATCH_HEADLESS=1 也要清掉，
    #   否则「在某个设过变量的终端里启动」又会退回无头。
    if headless:
        env["WATCH_HEADLESS"] = "1"
    else:
        env.pop("WATCH_HEADLESS", None)
    # 2026-10-14：--no-llm 纯采集模式（竞品六路零 token 常驻，事后画像走
    # portrait_backfill.py 按需补），默认仍带 --llm（实时辅助路径不变）
    cmd = [str(PY), SCRIPT, "--sec-uid", sec_uid, "--tag", tag, "--start"]
    if llm:
        cmd.append("--llm")
    subprocess.Popen(
        cmd,
        cwd=str(ROOT), env=env,
        # Watchdogs have their own file logs. Do not retain a health caller's pipes.
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=_no_window_flags(),
    )
    print("[ok] 已拉起 %s（%s%s）"
          % (name, tag, "，无头（会被风控抓验证码）" if headless else "，有头"))


def kill_browser() -> int:
    """关掉监控用的那个浏览器，返回杀掉的主进程数。

    ★ 按 profile 认人（不是按进程名），所以不会误伤老大自己开的 Chrome/Edge。
    过验证码、换浏览器之前必须先做这一步 —— `ensure_browser` 只看端口通不通，
    旧浏览器还活着它就直接复用，重启多少次都是那个被拦的无头实例。
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import run_watchdog as rw
        profile = rw.resolve_profile()
        pids = rw._profile_pids(profile)
    except Exception as e:  # noqa: BLE001 诊断类操作，失败不阻断主流程
        print("[fix] 查旧浏览器进程失败（忽略）：%s" % e)
        return 0
    for pid in pids:
        _run_hidden(["taskkill", "/PID", str(pid), "/T", "/F"])
    if pids:
        time.sleep(3)          # 等 profile 锁释放，否则新实例会撞 SingletonLock
    return len(pids)


def _legacy_main() -> int:
    ap = argparse.ArgumentParser(description="手动/开机拉起全部主播监控实例（隐藏形态）")
    ap.add_argument("--kill", action="store_true", help="先杀掉全部旧实例再拉起")
    ap.add_argument("--headed", action="store_true",
                    help="（兼容旧用法：现在默认就是有头，本参数无副作用）")
    ap.add_argument("--headless", action="store_true",
                    help="本次强制无头（无人值守/云端）。⚠️ 桌面场景别用：无头指纹"
                         "会被抖音给验证码中间页，而且人看不到窗口、没法过验证码")
    ap.add_argument("--no-llm", action="store_true",
                    help="纯采集模式：不带 --llm（零 token，事后画像用 "
                         "portrait_backfill.py 按需补）")
    ap.add_argument("--fix-captcha", action="store_true",
                    help="人工过验证码专用：杀掉全部实例**和浏览器**，重新拉起"
                         "**不最小化**的有头窗口，然后人在窗口里过验证码。"
                         "（只 --kill 不行：浏览器没关的话会被原样复用，还是那个被拦的实例）")
    ap.add_argument("--browser", choices=["chrome", "edge"], default=None,
                    help="切监控浏览器（2026-10-27）：本机 Chrome 是 32 位"
                         "（2 GB 地址空间上限），Edge 是 64 位。exe 与 profile 一起换。"
                         "★ 换之前必须先关掉旧浏览器，否则新实例会复用旧的那一个。")
    args = ap.parse_args()

    if args.browser:
        # 子进程继承 os.environ，所以在这里设一次就够了（watch 和 record 都会带上）
        os.environ["WATCH_BROWSER"] = args.browser
        print("[browser] 本次使用 %s（profile 随之切换）" % args.browser)

    if not PY.exists():
        print("venv python 不存在：%s" % PY)
        return 1
    DATA.mkdir(parents=True, exist_ok=True)

    roster, fell_back = anchor_roster.ensure()
    if fell_back:
        print("[warn] anchors.json 缺失或损坏，已用内置种子重建"
              "（原文件若存在已改名 .broken-<时间戳>）")

    entries = [a for a in roster["anchors"] if a.get("watch")]
    if args.kill or args.fix_captcha:
        # --kill 连「已停用」的也杀：用户刚把 enabled 改 false 时进程可能还活着，
        # 只杀 enabled 会留下孤儿。
        for a in entries:
            kill_instance(a["tag"])

    if args.fix_captcha:
        print("[fix] 全部实例已停，接着关浏览器（不关的话新实例会复用被拦的那个）...")
        n = kill_browser()
        print("[fix] 已关闭浏览器主进程 %d 个" % n)
        # 保留窗口给人工操作：ensure_browser 拉起后不最小化
        os.environ["WATCH_NO_MINIMIZE"] = "1"
        print("[fix] 接下来拉起的浏览器窗口**不会最小化**，请在弹出的窗口里过验证码/扫码。")
        print("[fix] 处理完它自己会继续跑，不用再执行别的命令。")

    started = skipped = 0
    # 2026-10-29：--no-llm 会把「名册里 llm=true」的路也压成纯采集，而主播页那一列
    # 显示的是**名册意愿**不是进程状态 —— 于是页面写着「LLM 开」，实时页的
    # 「观众在问什么 / LLM 建议」两栏却永远是空的（当天就是这样查了半天）。
    if args.no_llm:
        would = [a["tag"] for a in entries
                 if a.get("llm") and a.get("enabled", True)]
        if would:
            print("[warn] --no-llm 本次不启动 LLM 层，但名册里这些路 llm=true：%s"
                  % "、".join(would))
            print("[warn] 实时页的「观众在问什么 / LLM 给主播的建议」会一直空白。"
                  "要 LLM 就去掉 --no-llm。")
    for a in entries:
        if not a.get("enabled", True):
            print("[skip] %s 名册里已停用（enabled=false）" % a["tag"])
            skipped += 1
            continue
        launch(a["tag"], a["sec_uid"], a["tag"], headless=args.headless,
               llm=bool(a.get("llm")) and not args.no_llm)
        started += 1
    print("[roster] 尝试拉起 %d 路，跳过停用 %d 路" % (started, skipped))
    return 0


def main(argv=None) -> int:
    """Compatibility CLI: all customer starts use the single local application."""
    from local_app import main as application_main
    return application_main(["start", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    sys.exit(main())
