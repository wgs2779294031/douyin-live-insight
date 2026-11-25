"""飞书落盘层 —— 真数据端到端自检。

为什么需要这个脚本
------------------------------------------------
`tests/test_feishu_sink.py` 用的是**合成快照**，只能验证代码逻辑；
它测不出「快照里的字段到底是什么口径」这类问题。本脚本拿**真实采集的
JSONL** 灌进 `run_analyzer`，走完 main.py 的整条接线：

    run_analyzer → 扇出回调 → FeishuSink.row_from_snapshot → send → dry_run 落盘

逐一校验字段集合 / 类型 / 值域，以及三条关键不变量：

1. **delta 守恒**：首行 `_cum` + 后续各行 `_delta` 之和 == 末行 `_cum`
   （唯一能证明「累计 / 增量两套口径没搞反」的检查）
2. **冷场水位单调不减**（否则 new_cold_events 会重复计数）
3. **active_top_cap 确实透传下来了**（否则 active_users_capped 恒 0，
   会让人误以为 active_users_visible 就是活跃用户总数）

用法（在项目根目录执行）：
    python scripts/check_feishu_rows.py
    python scripts/check_feishu_rows.py data/danmu_douyin_2026-09-17.jsonl
    python scripts/check_feishu_rows.py --keep      # 保留产出的 JSONL 供肉眼查看

全程零网络：forced 成 dry_run 后端，不碰真飞书。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import sys
import threading

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from analyzer import load_analyzer_config, run_analyzer  # noqa: E402
from feishu_sink import FeishuSink, load_feishu_config  # noqa: E402

DEFAULT_DATA = "data/danmu_douyin_2026-09-17.jsonl"
OUT = "data/_check_feishu"

EXPECTED_KEYS = {
    "ts", "date", "room_id", "platform",
    "cum_window_start_ts", "cum_window_sec", "since_prev_sec", "cum_reset",
    "danmaku_cum", "enter_cum", "like_cum", "gift_cum", "total_records_cum",
    "danmaku_delta", "enter_delta", "like_delta", "gift_delta",
    "total_records_delta",
    "is_cold", "cold_window_sec", "cold_count_in_window",
    "cold_threshold_enter", "cold_threshold_exit", "cold_mode",
    "new_cold_events", "latest_cold_ts",
    "active_users_visible", "active_top_cap", "active_users_capped",
    "active_top", "top_words",
}
INT_KEYS = {
    "ts", "cum_window_start_ts", "cum_window_sec", "since_prev_sec", "cum_reset",
    "danmaku_cum", "enter_cum", "like_cum", "gift_cum", "total_records_cum",
    "danmaku_delta", "enter_delta", "like_delta", "gift_delta",
    "total_records_delta",
    "is_cold", "cold_window_sec", "cold_count_in_window",
    "new_cold_events", "latest_cold_ts",
    "active_users_visible", "active_top_cap", "active_users_capped",
}
NONNEG_KEYS = INT_KEYS - {"since_prev_sec", "cum_window_start_ts", "ts"}


def main(data_path: str = DEFAULT_DATA, keep: bool = False) -> int:
    if not os.path.exists(data_path):
        print(f"[SKIP] 找不到数据文件 {data_path}")
        print("       先跑一场采集，或指定已有文件："
              "python scripts/check_feishu_rows.py <file.jsonl>")
        return 2
    n_records = sum(1 for _ in open(data_path, encoding="utf-8"))
    print(f"真实数据：{data_path} 共 {n_records} 条")

    if os.path.isdir(OUT):
        shutil.rmtree(OUT)

    analyzer_cfg = load_analyzer_config("config.json")

    # 刻意把控频与窗口间隔都设为 0，让每一条 record 都产一行，最大化样本数
    fcfg = load_feishu_config("config.json")
    fcfg.enabled = True
    fcfg.backend = "dry_run"
    fcfg.output_dir = OUT
    fcfg.min_interval_seconds = 0.0
    print(f"飞书配置：enabled={fcfg.enabled} backend={fcfg.backend} "
          f"output_dir={fcfg.output_dir} min_interval={fcfg.min_interval_seconds}")

    # 与 main.py 一致：把规则层的 active_top_n 作为截断上限透传给 sink
    sink = FeishuSink(fcfg, room_id="e2e-room", platform="douyin",
                      active_top_cap=analyzer_cfg.active_top_n)
    print(f"规则层 active_top_n={analyzer_cfg.active_top_n}"
          f"（> 透传给 sink 作为 active_top 的截断上限）")
    q: "queue.Queue" = queue.Queue(maxsize=2000)
    rows: list = []

    def on_snapshot(snap: dict) -> None:
        row = sink.row_from_snapshot(snap)
        rows.append(row)
        sink.send(row)          # 走完 main.py 里 _push_feishu 之后的整条发送链路

    th = threading.Thread(
        target=run_analyzer,
        args=(q, analyzer_cfg),
        kwargs={"on_snapshot": on_snapshot, "snapshot_interval_seconds": 0},
        name="e2e-analyzer", daemon=True,
    )
    th.start()

    with open(data_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                q.put(json.loads(line))
    q.put(None)
    th.join(timeout=60)
    assert not th.is_alive(), "analyzer 线程未在超时内退出"

    # ---- 校验 1：条数与字段 ----
    print(f"\n[1] 扇出产出行数 = {len(rows)}（真实 record {n_records} 条）")
    assert rows, "没有产出任何指标行 —— 扇出链路断了"

    bad_keys = [i for i, r in enumerate(rows) if set(r) != EXPECTED_KEYS]
    print(f"[2] 字段集合与预期一致：{'OK' if not bad_keys else f'FAIL @ {bad_keys[:5]}'}")
    assert not bad_keys, f"字段集合不符，首例：{rows[bad_keys[0]]}"

    bad_type = [
        (i, k, type(r[k]).__name__)
        for i, r in enumerate(rows) for k in INT_KEYS
        if not isinstance(r[k], int)
    ]
    print(f"[3] {len(INT_KEYS)} 个整数字段类型正确：{'OK' if not bad_type else f'FAIL {bad_type[:5]}'}")
    assert not bad_type

    # ---- 校验 2：值域 ----
    neg = [
        (i, k, r[k]) for i, r in enumerate(rows) for k in NONNEG_KEYS if r[k] < 0
    ]
    print(f"[4] 非负字段无负值：{'OK' if not neg else f'FAIL {neg[:5]}'}")
    assert not neg

    bad_cold = [i for i, r in enumerate(rows) if r["is_cold"] not in (0, 1)]
    print(f"[5] is_cold ∈ {{0,1}}：{'OK' if not bad_cold else 'FAIL'}")
    assert not bad_cold

    # ---- 校验 3：★ delta 的守恒性（最能证明口径改对了）
    #      首行 _cum + 后续各行 _delta 之和 == 末行 _cum（无重置时）
    print("\n[6] delta 守恒性（首行 cum + Σdelta == 末行 cum）")
    for k in ("danmaku", "enter", "like", "gift", "total_records"):
        recon = rows[0][f"{k}_cum"] + sum(r[f"{k}_delta"] for r in rows[1:])
        last = rows[-1][f"{k}_cum"]
        ok = recon == last
        print(f"    {k:14s} 首行={rows[0][f'{k}_cum']:>6d} + Σdelta="
              f"{sum(r[f'{k}_delta'] for r in rows[1:]):>6d} = {recon:>6d}  "
              f"末行={last:>6d}  {'OK' if ok else 'FAIL'}")
        assert ok, f"{k} 的 delta 与 cum 不守恒"

    n_reset = sum(r["cum_reset"] for r in rows)
    print(f"    重置行数 cum_reset=1 共 {n_reset} 行（真实数据有 4 次时间戳跳跃，"
          f"若 >0 则上面守恒检查仍成立 → 说明重置分支工作正常）")
    assert rows[0]["danmaku_delta"] == 0, "首行 delta 应为 0（无基线）"
    assert rows[0]["since_prev_sec"] == 0, "首行 since_prev_sec 应为 0"

    # ---- 校验 4：冷场水位单调递增 ----
    ws = [r["latest_cold_ts"] for r in rows]
    mono = all(b >= a for a, b in zip(ws, ws[1:]))
    print(f"[7] latest_cold_ts 单调不减（水位不倒退）：{'OK' if mono else 'FAIL'}")
    assert mono, "冷场水位出现倒退，new_cold_events 会重复计数"

    # ---- 校验 5：活跃用户数不得谎报为「总数」
    capped = [r for r in rows if r["active_users_capped"]]
    print(f"[8] active_users_capped=1 的行 {len(capped)}/{len(rows)} 行；"
          f"visible 最大值={max(r['active_users_visible'] for r in rows)}，"
          f"cap={rows[0]['active_top_cap']}")
    assert rows[0]["active_top_cap"] > 0, "active_top_cap 未从 main.py 透传下来"

    # ---- 校验 6：dry_run 真的落到了文件，且行数与内存一致 ----
    produced = sorted(
        os.path.join(OUT, p) for p in os.listdir(OUT) if p.endswith(".jsonl")
    )
    disk_rows = 0
    for p in produced:
        disk_rows += sum(1 for ln in open(p, encoding="utf-8") if ln.strip())
    print(f"[9] dry_run 落盘 {len(produced)} 个文件 / {disk_rows} 行，"
          f"sink.sent={sink.sent} sink.failed={sink.failed}")
    assert disk_rows == sink.sent == len(rows), "落盘行数与发送计数不一致"

    sample = json.loads(open(produced[0], encoding="utf-8").readline())
    assert set(sample) == EXPECTED_KEYS and isinstance(sample["danmaku_cum"], int)

    sink.close()

    # ---- 抽样肉眼检查 ----
    print("\n[10] 抽样（第一条 / 增量最大的窗口）")
    for label, r in (
        ("首行（delta 应为 0）", rows[0]),
        ("danmaku_delta 最大的一条", max(rows[1:], key=lambda x: x["danmaku_delta"])),
    ):
        print(f"  --- {label} ---")
        print("  " + json.dumps(r, ensure_ascii=False)[:320])

    print("\n=== E2E 全部通过 ===")
    if keep:
        print(f"产出保留在 {OUT}/（--keep）")
    else:
        shutil.rmtree(OUT, ignore_errors=True)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="飞书落盘层真数据端到端自检")
    ap.add_argument("data", nargs="?", default=DEFAULT_DATA,
                    help=f"要回放的采集 JSONL（默认 {DEFAULT_DATA}）")
    ap.add_argument("--keep", action="store_true",
                    help="保留 dry_run 产出的 JSONL，便于肉眼查看")
    a = ap.parse_args()
    sys.exit(main(a.data, a.keep))
