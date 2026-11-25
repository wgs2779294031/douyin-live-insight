# -*- coding: utf-8 -*-
"""客户CLI委托 local_app.py start；--list委托status，不再独立拉录音。

以下为兼容导入保留的旧辅助函数说明：按名册拉起录音路。

为什么要有它
------------
`D:\\wb-bin\\start_recorders.bat` 原来把 4 个 tag 写死在 bat 里，与
`anchors.json` 是两套名单 —— 在界面上把某路 `record` 关掉，重启电脑它
又被 bat 拉回来了。现在 bat 只调本脚本，名单唯一来源 = anchors.json。

用法：
    python scripts/start_recorders.py            # 按名册起（已在跑的跳过）
    python scripts/start_recorders.py --list     # 只列会起哪些，不真起
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import anchor_manager as mgr      # noqa: E402
import anchor_roster as roster_mod  # noqa: E402


def _legacy_main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="按名册拉起 recorder")
    ap.add_argument("--list", action="store_true", help="只列名单，不启动")
    args = ap.parse_args(argv)

    roster, fell_back = roster_mod.ensure()
    if fell_back:
        print("[warn] anchors.json 缺失或损坏，已用内置种子重建（原文件备份为 .broken-*）")

    targets = [a for a in roster["anchors"] if a.get("record") and a.get("enabled")]
    if args.list:
        for a in targets:
            st = mgr.instance_status("record", a["tag"])
            print("%-24s %s" % (a["tag"], "在跑 pid=%d" % st["pid"] if st["running"] else "未跑"))
        print("共 %d 路" % len(targets))
        return 0

    started = skipped = failed = 0
    for a in targets:
        res = mgr.start("record", a)
        if res["state"] == "already_running":
            print("[skip] %s 已在跑 pid=%s" % (a["tag"], res.get("pid")))
            skipped += 1
        elif res["ok"]:
            print("[ok] %s 已拉起 pid=%s" % (a["tag"], res.get("pid")))
            started += 1
        else:
            print("[fail] %s %s" % (a["tag"], res.get("error") or res.get("state")))
            failed += 1
    print("[recorder] 起 %d / 跳过 %d / 失败 %d" % (started, skipped, failed))
    return 0 if failed == 0 else 1


def main(argv=None) -> int:
    """Compatibility CLI; never create an independent customer recording tree."""
    from local_app import main as application_main
    arguments = list(sys.argv[1:] if argv is None else argv)
    action = "status" if "--list" in arguments else "start"
    return application_main([action, *(argument for argument in arguments if argument != "--list")])


if __name__ == "__main__":
    sys.exit(main())
