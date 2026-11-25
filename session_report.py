r"""v3.3 竞品监控规则层：场次统计 + 高能时刻标记（离线计算）。

为什么离线算（而不是采集中实时算）
------------------------------------------------
高能时刻定义（老大需求书）：「人气 + 弹幕异常跳升点，含前后文窗口」——
「前后文窗口」天然要求跳升点**两侧**的数据都已发生；场次统计（时长/弹幕总量/
峰值人气/峰值时刻）是结算性数据。两者都适合直播结束后从落盘文件一次算完：
零运行时风险、31 列主链路零改动、算法可反复调参重算。

输入（监控采集层产物，见 monitor.py）：
    stats_douyin_<tag>_<date>.jsonl   人气快照（room_stats / room_user_seq）
    danmu_douyin_<tag>_<date>.jsonl   主链路事件（danmaku/enter/like/gift）

输出：
    session_<tag>_<date>.json
        session:    时长 / 弹幕总量 / 各类事件计数 / 峰值人气+时刻 / 累计观看PV起止
        highlights: 跳升点列表（人气跳升 / 弹幕爆发 / 双重），每个含前后文窗口

高能时刻判定（原值比值 + 绝对下限，避免小房间噪声；全部参数可调）：
    人气跳升：当前在线 >= 前基线(此前 8 个快照的中位数) × jump_ratio
              且增量 >= max(min_jump, 基线 × jump_min_ratio)
    弹幕爆发：10 秒桶弹幕数 >= max(min_burst, 前 6 桶中位数 × burst_ratio)
    相邻 120 秒内合并，保留峰值。

用法：
    python session_report.py --tag 升学-人大周老师 --date 2026-09-30 [--dir data]
    python session_report.py --stats <path> --danmu <path> [--out <path>]
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import statistics
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("danmu")

# —— 可调参数（默认值）
ONLINE_BASELINE_N = 8       # 人气基线窗口：此前 N 个快照
ONLINE_JUMP_RATIO = 1.5     # 人气跳升：峰值 >= 基线 × 1.5
ONLINE_JUMP_MIN = 5         # 人气跳升绝对下限（人）
ONLINE_JUMP_MIN_RATIO = 0.3  # 且增量 >= 基线 × 0.3
DANMU_BUCKET_S = 10         # 弹幕速率桶宽（秒）
DANMU_BASELINE_N = 6        # 弹幕基线：此前 N 个桶（60 秒）
DANMU_BURST_RATIO = 3.0     # 弹幕爆发：桶计数 >= 基线中位数 × 3
DANMU_BURST_MIN = 8         # 弹幕爆发绝对下限（条/桶）
MERGE_WINDOW_S = 120        # 相邻跳升合并窗口（秒）
CONTEXT_WINDOW_S = 60       # 前后文窗口半径（秒）
CONTEXT_MAX_TEXTS = 10      # 每个跳升点前后文弹幕最多保留条数

# —— 通道 A：疑似福袋时段（弹幕间接信号，2026-09-30 立；同日真机复核后**降级为兜底**）
# ⚠️ 更正：先前「proto 没类 → WSS 拿不到结构化福袋事件」**不成立**。真机已实收
#    WebcastLuckyBoxMessage / WebcastLotteryEventNewMessage / WebcastLuckyBoxRewardMessage
#    / WebcastLotteryDrawResultEventMessage（见 protobuf/extras.py）—— 结构化事件**拿得到**。
# 通道 A 现已降级为**兜底**：当①②③（WSS 事件 / luckybox XHR / 攒池扫描）都没采到时，
# 才用「关键词密度 + 短文本重复度」从弹幕推断「疑似福袋时段」。它是**推断**非精确事件。
LOTTERY_KEYWORDS = ["福袋", "口令", "截屏", "抽奖", "开奖", "抢福袋", "领福袋", "中奖", "红包"]
LOTTERY_WINDOW_S = 60       # 滑窗宽度（秒）
LOTTERY_BASELINE_N = 6      # 基线：此前 N 个窗口
LOTTERY_MIN_HITS = 5        # 绝对下限：窗口内关键词命中最少条数
LOTTERY_RATIO = 3.0         # 且 >= 基线中位数 × 3
LOTTERY_REPEAT_MIN = 4      # 窗口内「同一短文本重复」次数下限（口令弹幕的强特征）


def _load_jsonl(path: str) -> List[Dict[str, Any]]:
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _load_stats(path: str) -> List[Tuple[int, int, Optional[int]]]:
    """读人气快照 → [(ts, online, total_pv|None)]，按 ts 排序。"""
    rows: Dict[int, Tuple[int, Optional[int]]] = {}
    for rec in _load_jsonl(path):
        ts = int(rec.get("ts") or 0)
        online = int(rec.get("online") or 0)
        if online <= 0:
            continue  # is_hidden / 0 值快照不进序列
        pv = rec.get("total_pv")
        pv = int(pv) if isinstance(pv, (int, float)) else None
        prev = rows.get(ts)
        # 同秒取 online 更大者；total_pv 取有值者
        rows[ts] = (max(online, prev[0]) if prev else online,
                    pv if pv is not None else (prev[1] if prev else None))
    return sorted((ts, v[0], v[1]) for ts, v in rows.items())


def _danmu_buckets(danmaku_ts: List[int], bucket_s: int) -> List[Tuple[int, int]]:
    """弹幕时间戳 → [(桶起点 ts, 条数)]。"""
    if not danmaku_ts:
        return []
    t0 = (min(danmaku_ts) // bucket_s) * bucket_s
    buckets: Dict[int, int] = {}
    for ts in danmaku_ts:
        b = ((ts - t0) // bucket_s) * bucket_s + t0
        buckets[b] = buckets.get(b, 0) + 1
    return sorted(buckets.items())


def _detect_online_jumps(series: List[Tuple[int, int, Optional[int]]]) -> List[Dict[str, Any]]:
    """人气跳升点检测。"""
    jumps = []
    for i in range(1, len(series)):
        ts, online, _ = series[i]
        base_slice = [s[1] for s in series[max(0, i - ONLINE_BASELINE_N):i]]
        baseline = statistics.median(base_slice) if base_slice else online
        if baseline <= 0:
            continue
        gain = online - baseline
        if (online >= baseline * ONLINE_JUMP_RATIO
                and gain >= max(ONLINE_JUMP_MIN, baseline * ONLINE_JUMP_MIN_RATIO)):
            jumps.append({
                "ts": ts,
                "kind": "online_jump",
                "online_before": int(baseline),
                "online_peak": online,
                "online_gain": int(gain),
            })
    return jumps


def _detect_danmu_bursts(buckets: List[Tuple[int, int]]) -> List[Dict[str, Any]]:
    """弹幕爆发点检测（桶中心时刻标记）。"""
    bursts = []
    for i in range(1, len(buckets)):
        b_ts, count = buckets[i]
        base_slice = [c for _, c in buckets[max(0, i - DANMU_BASELINE_N):i]]
        baseline = statistics.median(base_slice) if base_slice else count
        gain = count - baseline
        if (count >= max(DANMU_BURST_MIN, baseline * DANMU_BURST_RATIO)
                and gain >= DANMU_BURST_MIN // 2):
            bursts.append({
                "ts": b_ts + DANMU_BUCKET_S // 2,
                "kind": "danmaku_burst",
                "rate_before_per10s": int(baseline),
                "rate_peak_per10s": count,
            })
    return bursts


def _detect_lottery_suspects(danmaku_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """通道 A：用弹幕间接识别「疑似福袋时段」（推断，非精确事件）。

    判据（两个都要满足，避免把日常提到「红包」的闲聊误判）：
    1. 60 秒滑窗内命中 LOTTERY_KEYWORDS 的弹幕 >= max(LOTTERY_MIN_HITS, 基线中位数×3)；
    2. 该窗口内存在**同一短文本重复 >= LOTTERY_REPEAT_MIN 次**（口令弹幕的强特征）。

    输出：{ts, kind: "lottery_suspect", keyword_hits, top_repeated_text, repeat_count,
           window_seconds, window_texts}
    """
    recs = [r for r in danmaku_records if r.get("ts")]
    if not recs:
        return []
    recs.sort(key=lambda r: r["ts"])
    # 按 60s 滑窗（步长 15s）统计
    t_start, t_end = recs[0]["ts"], recs[-1]["ts"]
    step = 15
    windows: List[Tuple[int, int, int, Dict[str, int]]] = []  # (win_start, hits, repeat, texts)
    win_start = t_start
    while win_start <= t_end:
        win_end = win_start + LOTTERY_WINDOW_S
        hits = 0
        texts: Dict[str, int] = {}
        for r in recs:
            ts = r["ts"]
            if ts < win_start:
                continue
            if ts >= win_end:
                break
            text = (r.get("text") or "").strip()
            if any(k in text for k in LOTTERY_KEYWORDS):
                hits += 1
                if 0 < len(text) <= 12:          # 口令一般很短
                    texts[text] = texts.get(text, 0) + 1
        repeat = max(texts.values(), default=0)
        windows.append((win_start, hits, repeat, texts))
        win_start += step
    if not windows:
        return []

    suspects: List[Dict[str, Any]] = []
    for i, (ws, hits, repeat, texts) in enumerate(windows):
        base_slice = [w[1] for w in windows[max(0, i - LOTTERY_BASELINE_N):i]]
        baseline = statistics.median(base_slice) if base_slice else 0
        if (hits >= max(LOTTERY_MIN_HITS, baseline * LOTTERY_RATIO)
                and repeat >= LOTTERY_REPEAT_MIN):
            top_text = max(texts.items(), key=lambda kv: kv[1])[0] if texts else ""
            suspects.append({
                "ts": ws + LOTTERY_WINDOW_S // 2,
                "kind": "lottery_suspect",
                "keyword_hits": hits,
                "baseline_hits": int(baseline),
                "top_repeated_text": top_text,
                "repeat_count": repeat,
            })
    # 相邻窗口去重：30s 内只留命中最多的一条
    deduped: List[Dict[str, Any]] = []
    for s in suspects:
        if deduped and s["ts"] - deduped[-1]["ts"] <= 30:
            if s["keyword_hits"] > deduped[-1]["keyword_hits"]:
                deduped[-1] = s
            continue
        deduped.append(s)
    return deduped


def _merge_highlights(jumps: List[Dict[str, Any]],
                      bursts: List[Dict[str, Any]],
                      suspects: Optional[List[Dict[str, Any]]] = None
                      ) -> List[Dict[str, Any]]:
    """各类跳升按时间相邻合并（≤ MERGE_WINDOW_S）。

    kind 命名：单类原样；{online_jump, danmaku_burst} 两类合并 → "both"（历史行为）；
    出现第三类（lottery_suspect）时用 "+" 连接全部类别名。
    """
    all_pts = [(p["ts"], p) for p in (jumps + bursts + (suspects or []))]
    all_pts.sort(key=lambda x: x[0])
    groups: List[List[Dict[str, Any]]] = []
    for ts, p in all_pts:
        if groups and ts - groups[-1][-1]["ts"] <= MERGE_WINDOW_S:
            groups[-1].append(p)
        else:
            groups.append([p])
    merged = []
    for g in groups:
        kinds = sorted({p["kind"] for p in g})
        if kinds == ["danmaku_burst", "online_jump"]:
            kind_name = "both"
        else:
            kind_name = "+".join(kinds)
        item: Dict[str, Any] = {
            "ts": min(p["ts"] for p in g),
            "kind": kind_name,
        }
        for p in g:
            for k, v in p.items():
                if k in ("ts", "kind"):
                    continue
                # 同名冲突（都含 ts 之外的数值）取更大峰值
                if k not in item or (isinstance(v, (int, float))
                                     and isinstance(item[k], (int, float))
                                     and v > item[k]):
                    item[k] = v
        merged.append(item)
    return merged


def _attach_context(highlights: List[Dict[str, Any]],
                    danmaku_records: List[Dict[str, Any]]) -> None:
    """给每个跳升点附前后文窗口（±CONTEXT_WINDOW_S 的弹幕原文）。"""
    by_ts = sorted(danmaku_records, key=lambda r: r.get("ts", 0))
    for h in highlights:
        lo, hi = h["ts"] - CONTEXT_WINDOW_S, h["ts"] + CONTEXT_WINDOW_S
        texts = [r.get("text", "") for r in by_ts if lo <= r.get("ts", 0) <= hi]
        h["window_seconds"] = CONTEXT_WINDOW_S
        h["window_danmaku_count"] = len(texts)
        h["window_texts"] = texts[:CONTEXT_MAX_TEXTS]


def compute_session(stats_path: str, danmu_path: str,
                    room_tag: str = "") -> Dict[str, Any]:
    """主计算：读两个 jsonl → 场次统计 + 高能时刻。纯函数（不含 IO 输出）。"""
    series = _load_stats(stats_path)
    events = _load_jsonl(danmu_path)
    danmaku = [e for e in events if e.get("type") == "danmaku"]
    danmaku_ts = [int(e["ts"]) for e in danmaku if e.get("ts")]

    # —— 场次统计（全部为落盘原值聚合，无推断）
    all_ts = ([s[0] for s in series] + [int(e.get("ts") or 0) for e in events])
    all_ts = [t for t in all_ts if t > 0]
    type_counts: Dict[str, int] = {}
    for e in events:
        t = e.get("type") or "?"
        type_counts[t] = type_counts.get(t, 0) + 1

    pvs = [s[2] for s in series if s[2] is not None]
    session: Dict[str, Any] = {
        "session_id": None,  # 由调用方/元数据补
        "room_tag": room_tag,
        "start_ts": min(all_ts) if all_ts else 0,
        "end_ts": max(all_ts) if all_ts else 0,
        "event_total": len(events),
        "type_counts": type_counts,
        "danmaku_total": type_counts.get("danmaku", 0),
    }
    if session["start_ts"] and session["end_ts"]:
        session["duration_seconds"] = session["end_ts"] - session["start_ts"]
    if series:
        peak = max(series, key=lambda s: s[1])
        session["online_peak"] = peak[1]
        session["online_peak_ts"] = peak[0]
        session["online_first"] = series[0][1]
        session["online_last"] = series[-1][1]
        session["online_samples"] = len(series)
    if pvs:
        session["total_pv_first"] = pvs[0]
        session["total_pv_last"] = pvs[-1]
        session["total_pv_max"] = max(pvs)

    # —— 高能时刻
    jumps = _detect_online_jumps(series)
    bursts = _detect_danmu_bursts(_danmu_buckets(danmaku_ts, DANMU_BUCKET_S))
    suspects = _detect_lottery_suspects(danmaku)   # 通道 A：疑似福袋时段（推断）
    highlights = _merge_highlights(jumps, bursts, suspects)
    _attach_context(highlights, danmaku)

    return {
        "generated_at": int(time.time()),
        "inputs": {"stats": stats_path, "danmu": danmu_path},
        "session": session,
        "highlights": highlights,
        "params": {
            "online_baseline_n": ONLINE_BASELINE_N,
            "online_jump_ratio": ONLINE_JUMP_RATIO,
            "online_jump_min": ONLINE_JUMP_MIN,
            "danmu_bucket_s": DANMU_BUCKET_S,
            "danmu_burst_ratio": DANMU_BURST_RATIO,
            "danmu_burst_min": DANMU_BURST_MIN,
            "merge_window_s": MERGE_WINDOW_S,
            "context_window_s": CONTEXT_WINDOW_S,
            "lottery_keywords": LOTTERY_KEYWORDS,
            "lottery_window_s": LOTTERY_WINDOW_S,
            "lottery_min_hits": LOTTERY_MIN_HITS,
            "lottery_ratio": LOTTERY_RATIO,
            "lottery_repeat_min": LOTTERY_REPEAT_MIN,
        },
        "data_boundary": {
            "gmv": "—（WSS 推流拿不到 GMV；竞品号的 GMV 要抖店后台权限，属平台侧能力边界）",
            "replay": "—（直播流/录播未接）",
            "start_time": "优先 room_meta 的 start_time；否则近似为场次首条消息",
            "lottery": "福袋共**四级**来源（prior 越靠前越权威）："
                       "①**福袋结构化事件**走 WSS —— 真机 2026-09-30 实收 "
                       "WebcastLuckyBoxMessage(type=luckybox，含福袋名/时长)、"
                       "WebcastLotteryEventNewMessage(type=lottery_event，含奖品名/参与条件/起止)、"
                       "WebcastLuckyBoxRewardMessage(type=luckybox_reward，★含**中奖 UID 名单**)、"
                       "WebcastLotteryDrawResultEventMessage(type=lottery_draw_result，含 candidate_num)；"
                       "②在发福袋的实时状态走房间页 XHR /webcast/luckybox/box/list/"
                       "（scripts/scan_luckybox.py；免签名，但必须用 19 位内部 room_id）；"
                       "③找「正在发福袋的房」用 scripts/sweep_lottery_pool.py"
                       "（多抓几次 feed 攒大池再扫，单批 20 房命中率≈0，攒池后实测 160 房命中 3 房）；"
                       "④本报告的 kind=lottery_suspect 是**弹幕推断**（通道 A），非平台事件 —— "
                       "只有①②都没采到时才用它兜底",
            "pk_linkmic": "★ 真机已坐实**平台确实推**（2026-09-30，房间 60964041263）："
                          "实收 WebcastLinkMicSendEmojiMessage(type=linkmic_emoji) 与 "
                          "WebcastLinkerContributeMessage(type=linker_contribute，含贡献分)。"
                          "注意这两类 **vendor proto 里没有**，故此前「0 次」曾被误读为「拿不到」，"
                          "实为「没遇到」；proto 内另有 WebcastMatchAgainstScoreMessage / "
                          "UpdateFanTicketMessage（比分/助力票）可用。",
        },
    }


def discover_inputs(output_dir: str, platform: str, tag: str, date: str
                    ) -> Tuple[Optional[str], Optional[str]]:
    """按 tag+date 自动发现 stats / danmu 文件路径。"""
    stats = os.path.join(output_dir, f"stats_{platform}_{tag}_{date}.jsonl")
    danmu = os.path.join(output_dir, f"danmu_{platform}_{tag}_{date}.jsonl")
    return (stats if os.path.exists(stats) else None,
            danmu if os.path.exists(danmu) else None)


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(levelname)s - %(message)s")
    ap = argparse.ArgumentParser(description="场次统计 + 高能时刻（离线）")
    ap.add_argument("--dir", default="data", help="落盘目录（默认 data）")
    ap.add_argument("--platform", default="douyin")
    ap.add_argument("--tag", default=None, help="房间标识（多房间文件名中的 tag）")
    ap.add_argument("--date", default=time.strftime("%Y-%m-%d"))
    ap.add_argument("--stats", default=None, help="直接指定 stats jsonl（优先于 --tag 自动发现）")
    ap.add_argument("--danmu", default=None, help="直接指定 danmu jsonl")
    ap.add_argument("--out", default=None, help="输出 JSON 路径（默认 session_<tag>_<date>.json）")
    args = ap.parse_args()

    stats_path, danmu_path = args.stats, args.danmu
    if not (stats_path and danmu_path):
        if not args.tag:
            # 无 --tag：扫描目录里所有 stats 文件，逐个生成
            pattern = os.path.join(args.dir, f"stats_{args.platform}_*_{args.date}.jsonl")
            files = sorted(glob.glob(pattern))
            if not files:
                logger.error("目录 %s 下找不到 stats_%s_*_%s.jsonl", args.dir,
                             args.platform, args.date)
                return 1
            rc = 0
            for fp in files:
                name = os.path.basename(fp)[len(f"stats_{args.platform}_"):-len(f"_{args.date}.jsonl")]
                rc = rc or _run_one(args, fp, None, name)
            return rc
        stats_path, danmu_path = discover_inputs(args.dir, args.platform, args.tag, args.date)
    return _run_one(args, stats_path, danmu_path, args.tag)


def _run_one(args, stats_path: Optional[str], danmu_path: Optional[str],
             tag: str) -> int:
    if not stats_path or not os.path.exists(stats_path):
        logger.error("找不到 stats 文件：%s", stats_path)
        return 1
    if not danmu_path or not os.path.exists(danmu_path):
        # 从 stats 文件名推导同目录同名 danmu 文件
        base = os.path.basename(stats_path)
        if base.startswith("stats_"):
            candidate = os.path.join(os.path.dirname(stats_path),
                                     "danmu_" + base[len("stats_"):])
            if os.path.exists(candidate):
                danmu_path = candidate
    if not danmu_path or not os.path.exists(danmu_path):
        logger.error("找不到 danmu 文件：%s", danmu_path)
        return 1
    report = compute_session(stats_path, danmu_path, room_tag=tag)
    # 补房间元数据（若有）
    meta_path = os.path.join(os.path.dirname(stats_path),
                             f"room_meta_{args.platform}_{tag}.json")
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            report["room_meta"] = meta
            start = int(meta.get("start_time") or 0)
            if not start:
                # 开播真实时刻两个来源都不给 → 用本场首条消息时间近似
                start = int(meta.get("first_msg_ts") or 0)
            if start:
                report["session"]["start_ts"] = start
                if report["session"].get("end_ts"):
                    report["session"]["duration_seconds"] = (
                        report["session"]["end_ts"] - start)
            report["session"]["session_id"] = meta.get("session_id") or report["session"]["session_id"]
        except Exception as e:
            logger.warning("读取房间元数据失败: %s", e)
    if not report["session"].get("session_id"):
        report["session"]["session_id"] = f"{tag}_{args.date}"

    out = args.out or os.path.join(
        os.path.dirname(stats_path), f"session_{tag}_{args.date}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    logger.info("已生成 %s（高能时刻 %d 个，弹幕 %d 条，峰值在线 %s）",
                out, len(report["highlights"]),
                report["session"].get("danmaku_total", 0),
                report["session"].get("online_peak", "—"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
