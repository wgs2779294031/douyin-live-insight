"""用项目自己的聚合代码，从真实弹幕生成「飞书建表字段」与「指标行」两份 JSON。

为什么不用手写字段清单
------------------------------------------------
列清单手抄必错（本项目实测：手写「33 列」，真实只有 31 列）。
所以这里**从 `build_row({})` 推导列名与类型**，与落盘契约永远同步。

输出
------------------------------------------------
argv[1] = 字段数组 JSON（喂给 `lark-cli base +base-create --fields`）
argv[2] = 批量建记录 JSON（喂给 `lark-cli base +record-batch-create --json`）
"""

from __future__ import annotations

import io
import json
import queue
import sys
import threading
import time

sys.path.insert(0, ".")

from analyzer import load_analyzer_config, run_analyzer  # noqa: E402
from feishu_sink import (  # noqa: E402
    FeishuSink,
    build_row,
    load_feishu_config,
    row_to_fields,
)

DATA = "data/danmu_douyin_2026-09-17.jsonl"
LIMIT = 6          # 只取前若干行，够验证契约即可


def field_type(value) -> str:
    if isinstance(value, bool):
        return "checkbox"
    if isinstance(value, (int, float)):
        return "number"
    return "text"


def main(out_fields: str, out_records: str) -> int:
    # ---- 1. 列清单：由代码推导，不手抄
    sample = row_to_fields(build_row({}, room_id="x", platform="douyin"))
    fields = [{"name": k, "type": field_type(v)} for k, v in sample.items()]
    io.open(out_fields, "w", encoding="utf-8").write(
        json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
    )
    n_text = sum(1 for f in fields if f["type"] == "text")
    n_num = sum(1 for f in fields if f["type"] == "number")
    print(f"[字段] 推出 {len(fields)} 列（文本 {n_text} / 数字 {n_num}）-> {out_fields}")

    # ---- 2. 真实指标行：回放真弹幕，按 60 秒窗口产行（= 真跑采集时的口径）
    cfg = load_analyzer_config("config.json")
    fcfg = load_feishu_config("config.json")
    fcfg.enabled, fcfg.backend, fcfg.output_dir = True, "dry_run", "data/_probe"
    fcfg.min_interval_seconds = 0.0
    sink = FeishuSink(fcfg, room_id="73210123456", platform="douyin",
                      active_top_cap=cfg.active_top_n)

    rows: list = []
    q: "queue.Queue" = queue.Queue(maxsize=20000)

    def on_snapshot(snap: dict) -> None:
        if len(rows) < LIMIT:
            rows.append(sink.row_from_snapshot(snap))

    # ★ 快照定时用的是**墙上时钟**，不是 record 里的 ts。所以「一口气灌完」
    #   会让窗口一次都不开（实测 0 行）。这里按真实时间轴节流：每 CHUNK 条
    #   歇 CHUNK_SLEEP 秒，让 1 秒窗口真的开合。
    CHUNK, CHUNK_SLEEP = 1800, 1.2
    th = threading.Thread(
        target=run_analyzer, args=(q, cfg),
        kwargs={"on_snapshot": on_snapshot, "snapshot_interval_seconds": 1},
        name="probe", daemon=True,
    )
    th.start()
    n = 0
    with io.open(DATA, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            q.put(json.loads(line))
            n += 1
            if n % CHUNK == 0:
                time.sleep(CHUNK_SLEEP)
    q.put(None)
    th.join(timeout=120)
    sink.close()

    recs = [row_to_fields(r) for r in rows]
    # 契约自检：每条记录的字段集合必须与列清单完全一致（少一列飞书就整条报错）
    want = {f["name"] for f in fields}
    for i, r in enumerate(recs):
        assert set(r) == want, f"第 {i} 行字段集与列清单不一致：{set(r) ^ want}"

    payload = {"create_records": recs}
    io.open(out_records, "w", encoding="utf-8").write(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )
    print(f"[记录] 真实弹幕回放产出 {len(recs)} 条指标行，"
          f"字段集 100% 对齐 {len(want)} 列 -> {out_records}")
    for r in recs[:3]:
        print(f"       ts={r['ts']} danmaku_cum={r['danmaku_cum']} "
              f"danmaku_delta={r['danmaku_delta']} is_cold={r['is_cold']} "
              f"active_top_cap={r['active_top_cap']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
