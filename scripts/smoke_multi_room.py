"""多房间（v3.1）离线冒烟：真跑 main.main()，只把采集适配器换成替身。

为什么需要它
------------------------------------------------
单测（tests/test_rooms.py）测的是**零件**：配置解析、开关求值、存储分文件、
RoomRuntime 的装配。但「main.py 的多房间编排」是另一层——房间列表怎么来、
启动顺序、错峰、结束判定、收尾顺序。这层只有真跑一次 main() 才验证得到。

★ 全程零网络、零成本：替身适配器直接往 record_queue 灌数据并落盘。
★ 覆盖的真实链路：main → RoomRuntime → 真 analyzer（规则层）→ 真
  feishu_sink（dry_run 后端）→ 本地文件。

用法
----
    ./.venv/Scripts/python.exe scripts/smoke_multi_room.py
    ./.venv/Scripts/python.exe scripts/smoke_multi_room.py --keep   # 保留产物目录
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as main_mod  # noqa: E402
import room_runtime  # noqa: E402

# 每个房间灌多少条弹幕（够触发规则层快照即可）
RECORDS_PER_ROOM = 8
# 替身适配器「活着」多久（秒）：要长于一个快照窗口，才能落出指标行
ALIVE_SECONDS = 2.2
# 冒烟里把状态巡检间隔压到 0.5s（默认 30s 太慢）
SMOKE_STATUS_INTERVAL = 0.5

ROOMS = [
    {"room_id": "100001", "label": "自家-主播A", "llm": False, "feishu": True},
    {"room_id": "200002", "label": "竞品-就业", "llm": False, "feishu": True},
]
TEXTS = {"100001": "考研怎么选", "200002": "考公怎么报"}


class _FakeAdapter:
    """替身采集器：灌记录 → 落盘 → 阻塞 ALIVE_SECONDS → 结束。"""

    def __init__(self, room_id, storage, record_queue):
        self.room_id = room_id
        self.storage = storage
        self.record_queue = record_queue
        self.msg_count = 0
        self._closed = threading.Event()

    def run(self):
        base = 1789657000
        for n in range(RECORDS_PER_ROOM):
            rec = {
                "ts": base + n * 3,
                "platform": "douyin",
                "type": "danmaku",
                "user_id": f"{self.room_id}-u{n}",
                "uname": f"观众{n}",
                "text": f"{TEXTS[self.room_id]} 第{n}条",
                "room_id": self.room_id,
                "extra": {},
            }
            self.storage.write(rec)
            self.msg_count += 1
            try:
                self.record_queue.put_nowait(rec)
            except Exception:
                pass
        self._closed.wait(timeout=ALIVE_SECONDS)

    def close(self):
        self._closed.set()


def _install_fake_adapter() -> None:
    def _build(source_name, cfg, storage, record_queue=None, shared_state=None):
        rid = storage.room
        # 落盘文件名里是 label（安全化后），映射回 room_id
        for r in ROOMS:
            if cfg.web_rid == r["room_id"]:
                return _FakeAdapter(r["room_id"], storage, record_queue)
        raise AssertionError(f"替身拿到未知房间 {rid!r} / web_rid={cfg.web_rid!r}")

    room_runtime.build_adapter = _build


def _make_config(workdir: str) -> str:
    payload = {
        "source": {"platform": "douyin_reverse"},
        "output_dir": os.path.join(workdir, "data"),
        "heartbeat_interval": 30,
        "max_rooms": 5,
        "rooms": ROOMS,
        "cold_window_seconds": 60,
        "snapshot_interval_seconds": 60,
        "feishu": {
            "enabled": True,
            "backend": "dry_run",
            "output_dir": os.path.join(workdir, "data"),
            "snapshot_interval_seconds": 1,
            "min_interval_seconds": 0,
            "queue_maxsize": 200,
        },
    }
    path = os.path.join(workdir, "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="多房间离线冒烟（零网络）")
    ap.add_argument("--keep", action="store_true", help="保留产物目录以便人工检查")
    args = ap.parse_args()

    global _WORKDIR
    _WORKDIR = tempfile.mkdtemp(prefix="smoke_multiroom_")
    workdir = _WORKDIR
    cfg_path = _make_config(workdir)
    data_dir = os.path.join(workdir, "data")
    today = time.strftime("%Y-%m-%d")

    print(f"[冒烟] 工作目录 {workdir}")
    _install_fake_adapter()
    main_mod.STATUS_INTERVAL_SECONDS = SMOKE_STATUS_INTERVAL
    main_mod.STAGGER_SECONDS = 0.2

    sys.argv = ["main.py", "--config", cfg_path]
    t0 = time.time()
    rc = main_mod.main()
    cost = time.time() - t0

    problems = []
    print(f"\n[冒烟] main() 返回 {rc}，耗时 {cost:.1f}s")

    # ---- 1 退出码
    if rc != 0:
        problems.append(f"main() 返回 {rc}，期望 0")

    # ---- 2 每个房间一份弹幕文件，且内容不串
    for room in ROOMS:
        tag = room["label"]
        path = os.path.join(data_dir, f"danmu_douyin_{tag}_{today}.jsonl")
        if not os.path.exists(path):
            problems.append(f"缺少落盘文件 {os.path.basename(path)}")
            continue
        with open(path, encoding="utf-8") as f:
            recs = [json.loads(x) for x in f if x.strip()]
        if len(recs) != RECORDS_PER_ROOM:
            problems.append(f"{tag}: 落盘 {len(recs)} 条，期望 {RECORDS_PER_ROOM}")
        wrong = [r for r in recs if r["room_id"] != room["room_id"]]
        if wrong:
            problems.append(f"{tag}: 混入其他房间的记录 {len(wrong)} 条")
        if any(TEXTS[room["room_id"]] not in r["text"] for r in recs):
            problems.append(f"{tag}: 文本内容不属于本房间")
        print(f"[冒烟] {tag}: {len(recs)} 条弹幕，room_id 全部匹配={not wrong}")

    # ---- 3 每个房间一份飞书指标行，且 room_id 不串
    for room in ROOMS:
        tag = room["label"]
        path = os.path.join(data_dir, f"feishu_out_{tag}_{today}.jsonl")
        if not os.path.exists(path):
            problems.append(f"缺少飞书指标文件 {os.path.basename(path)}")
            continue
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(x) for x in f if x.strip()]
        if not rows:
            problems.append(f"{tag}: 飞书指标行为空")
        bad = [r for r in rows if r.get("room_id") != room["room_id"]]
        if bad:
            problems.append(f"{tag}: 飞书行混入其他房间 {len(bad)} 行")
        # 口径不变量：首行 _cum + Σ_delta == 末行 _cum
        for key in ("danmaku", "enter", "like", "gift", "total_records"):
            ck, dk = f"{key}_cum", f"{key}_delta"
            if not rows or ck not in rows[0]:
                continue
            expect = rows[0][ck] + sum(int(r.get(dk, 0)) for r in rows)
            if expect != rows[-1][ck]:
                problems.append(
                    f"{tag}: {key} 口径不守恒（首行{rows[0][ck]} + Σdelta = "
                    f"{expect}，末行{rows[-1][ck]}）"
                )
        print(f"[冒烟] {tag}: {len(rows)} 行飞书指标，room_id 全部匹配={not bad}")

    # ---- 4 不应出现无房间标识的旧文件名（会意味着串写）
    stray = [n for n in os.listdir(data_dir)
             if n.startswith("danmu_douyin_") and n.count("_") == 2]
    if stray:
        problems.append(f"多房间下出现了不带房间标识的文件（疑似串写）：{stray}")

    # ---- 5 legacy 单房间文件名零回归
    print("[冒烟] 另测：legacy 单房间（rooms 缺省）文件名不带房间标识")
    legacy_dir = os.path.join(workdir, "legacy")
    os.makedirs(legacy_dir, exist_ok=True)
    legacy_cfg = os.path.join(legacy_dir, "config.json")
    with open(legacy_cfg, "w", encoding="utf-8") as f:
        json.dump({
            "source": {"platform": "douyin_reverse"},
            "web_rid": "100001",
            "output_dir": os.path.join(legacy_dir, "data"),
        }, f, ensure_ascii=False)
    sys.argv = ["main.py", "--config", legacy_cfg]
    rc2 = main_mod.main()
    legacy_file = os.path.join(legacy_dir, "data", f"danmu_douyin_{today}.jsonl")
    if rc2 != 0 or not os.path.exists(legacy_file):
        problems.append("legacy 单房间路径未产出旧命名文件（零回归被破坏）")
    else:
        print(f"[冒烟] legacy: {os.path.basename(legacy_file)} ✓")

    print()
    if problems:
        print(f"✗ 冒烟未通过，{len(problems)} 个问题：")
        for p in problems:
            print(f"   · {p}")
        return 1
    print("✓ 冒烟全部通过（2 房间并发、数据不串、口径守恒、单房间文件名未变）")
    return 0


if __name__ == "__main__":
    _keep = "--keep" in sys.argv
    _WORKDIR = None
    try:
        code = main()
    finally:
        if _WORKDIR and not _keep:
            shutil.rmtree(_WORKDIR, ignore_errors=True)
        elif _WORKDIR:
            print(f"[冒烟] 产物保留在 {_WORKDIR}")
    sys.exit(code)
