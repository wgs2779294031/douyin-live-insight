# -*- coding: utf-8 -*-
"""
长期观众画像汇总脚本
扫描 data/ 下全部历史 JSONL（弹幕/画像/冷场台词/LLM指标），生成多 Sheet Excel：
  1. 场次总览    - 按天 x 平台聚合：弹幕量 / 画像量 / 台词量 / 调用量 / token / 费用估算
  2. 长期观众画像 - 跨天按 user 聚合：累计发言 / 活跃天数 / 高频词 / 首末活跃
  3. 画像时间线  - portrait 全量明细
  4. 情感趋势    - 按天 x 房间聚合 sentiment 均值
  5. 冷场台词    - coldline 全量明细
  6. LLM 调用    - metrics 全量明细

用法: python summarize.py [--out 输出xlsx路径]
说明: 每次采集结束后重跑一次即可自动累积更新。
"""
import hashlib
import json
import os
import re
import argparse
import collections
from datetime import datetime

import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
DEFAULT_OUT = os.path.join(DATA, "观众画像汇总.xlsx")

# 价格口径（doubao-seed-2.1-turbo，元/百万tokens，仅估算）
PRICE_IN = 3.0
PRICE_OUT = 15.0

STOPWORDS = set("的了是在我有你就他她它们和也不都一个这那好吗吧啊哦嗯呀呢么什么怎么为什么这样那样现在今天明天大家我们你们他们真的觉得感觉可以知道看看看到喜欢主播直播间家人朋友有没有来了来了已经还是就是不是没有自己这个那个什么然后因为所以如果虽然但是其实应该可能大概觉得感觉一直真的非常特别多少分".strip())


def mask_uid(uid: str) -> str:
    """用户 ID 脱敏：取 sha256 前 12 位（稳定不可逆，跨天一致）。

    聚合层仍用原始 uid 在内存中完成，仅输出层调用本函数。
    """
    return hashlib.sha256(str(uid).encode("utf-8")).hexdigest()[:12]


def load_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def ts2str(ts):
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def ts2date(ts):
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
    except Exception:
        return ""


def top_words(texts, n=5):
    """轻量高频词：中文连续段 + 英文数字token，去停用词"""
    counter = collections.Counter()
    pat = re.compile(r"[\u4e00-\u9fa5]{2,}|[A-Za-z0-9]{2,}")
    for t in texts:
        if not t:
            continue
        for w in pat.findall(t):
            if w.lower() in STOPWORDS or len(w) < 2:
                continue
            counter[w] += 1
    return "/".join(w for w, _ in counter.most_common(n))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    danmu_files = sorted(glob_("danmu_*.jsonl"))
    portrait_files = sorted(glob_("portrait_*.jsonl"))
    coldline_files = sorted(glob_("coldline_*.jsonl"))
    metrics_files = sorted(glob_("llm_metrics_*.jsonl"))

    # ---------- 1. 场次总览 ----------
    overview = []
    for fp in danmu_files:
        rows = load_jsonl(fp)
        name = os.path.basename(fp)
        day = re.search(r"\d{4}-\d{2}-\d{2}", name)
        day = day.group(0) if day else name
        plat = "抖音" if "douyin" in name else ("B站" if "web" in name else "其他")
        danmaku = sum(1 for r in rows if r.get("type") == "danmaku")
        overview.append({
            "日期": day, "平台": plat, "文件": name,
            "弹幕事件": len(rows), "发言(danmaku)": danmaku,
        })
    pf = {os.path.basename(p): load_jsonl(p) for p in portrait_files}
    cf = {os.path.basename(p): load_jsonl(p) for p in coldline_files}
    mf = {os.path.basename(p): load_jsonl(p) for p in metrics_files}
    for row in overview:
        day = row["日期"]
        row["画像数"] = sum(len(v) for k, v in pf.items() if day in k)
        row["冷场台词批次数"] = sum(len(v) for k, v in cf.items() if day in k)
        calls = [r for k, v in mf.items() if day in k for r in v]
        row["LLM调用数"] = len(calls)
        row["prompt_tokens"] = sum(r.get("prompt_tokens", 0) for r in calls)
        row["completion_tokens"] = sum(r.get("completion_tokens", 0) for r in calls)
        row["总tokens"] = row["prompt_tokens"] + row["completion_tokens"]
        row["费用估算(元)"] = round(
            row["prompt_tokens"] * PRICE_IN / 1e6 + row["completion_tokens"] * PRICE_OUT / 1e6, 3)
    df_overview = pd.DataFrame(overview)

    # ---------- 2. 长期观众画像 ----------
    user_map = collections.defaultdict(lambda: {
        "发言": 0, "事件": 0, "days": set(), "first": None, "last": None,
        "uname": "", "texts": [],
    })
    for fp in danmu_files:
        for r in load_jsonl(fp):
            uid = r.get("user_id") or r.get("user_hash")
            if not uid:
                continue
            u = user_map[uid]
            u["事件"] += 1
            if r.get("type") == "danmaku":
                u["发言"] += 1
                t = r.get("text", "")
                if t:
                    u["texts"].append(t)
            name = r.get("uname") or ""
            if name:
                u["uname"] = name
            d = ts2date(r.get("ts"))
            if d:
                u["days"].add(d)
            ts = r.get("ts")
            if ts:
                if u["first"] is None or ts < u["first"]:
                    u["first"] = ts
                if u["last"] is None or ts > u["last"]:
                    u["last"] = ts
    audience = []
    for uid, u in user_map.items():
        days = len(u["days"])
        span = 1
        if u["first"] and u["last"] and u["last"] > u["first"]:
            span = max(1, int((u["last"] - u["first"]) / 86400) + 1)
        audience.append({
            "用户ID(脱敏)": mask_uid(uid),
            # 昵称可能包含自身 uid（如抖音默认"手机用户{uid}"），输出层一并脱敏
            "最新昵称": u["uname"].replace(uid, mask_uid(uid)) if uid else u["uname"],
            "累计发言": u["发言"],
            "累计事件": u["事件"],
            "活跃天数": days,
            "首次出现": ts2str(u["first"]),
            "最后出现": ts2str(u["last"]),
            "日均发言": round(u["发言"] / span, 1),
            "高频词Top5": top_words(u["texts"]),
        })
    df_audience = pd.DataFrame(audience).sort_values("累计发言", ascending=False)

    # ---------- 3. 画像时间线 ----------
    tl = []
    for fp in portrait_files:
        for r in load_jsonl(fp):
            p = r.get("portrait", r)
            tl.append({
                "时间": ts2str(r.get("ts")),
                "房间ID": r.get("room_id", ""),
                "兴趣话题": "/".join(p.get("interest_topics", [])),
                "消费意向": (p.get("consumption_intent") or {}).get("level", ""),
                "情感-积极": (p.get("sentiment") or {}).get("positive", 0),
                "情感-中性": (p.get("sentiment") or {}).get("neutral", 0),
                "情感-消极": (p.get("sentiment") or {}).get("negative", 0),
                "建议条数": len(p.get("suggestions", [])),
                "高活跃层人数": len((p.get("activity_layers") or {}).get("high", [])),
            })
    df_tl = pd.DataFrame(tl).sort_values("时间")

    # ---------- 4. 情感趋势 ----------
    trend = collections.defaultdict(lambda: {"n": 0, "pos": 0.0, "neu": 0.0, "neg": 0.0})
    for r in df_tl.to_dict("records"):
        key = (str(r["时间"])[:10], r["房间ID"] or "未知")
        trend[key]["n"] += 1
        trend[key]["pos"] += r["情感-积极"]
        trend[key]["neu"] += r["情感-中性"]
        trend[key]["neg"] += r["情感-消极"]
    df_trend = pd.DataFrame([
        {"日期": k[0], "房间ID": k[1], "画像数": v["n"],
         "积极均值": round(v["pos"] / v["n"], 3),
         "中性均值": round(v["neu"] / v["n"], 3),
         "消极均值": round(v["neg"] / v["n"], 3)}
        for k, v in sorted(trend.items())
    ])

    # ---------- 5. 冷场台词 ----------
    cold = []
    for fp in coldline_files:
        for r in load_jsonl(fp):
            lines = r.get("lines", [])
            cold.append({
                "时间": ts2str(r.get("ts")),
                "事件": r.get("event", ""),
                "房间ID": r.get("room_id", ""),
                "台词条数": len(lines),
                "台词内容": "\n".join(lines),
            })
    df_cold = pd.DataFrame(cold).sort_values("时间")

    # ---------- 6. LLM 调用 ----------
    calls = []
    for fp in metrics_files:
        for r in load_jsonl(fp):
            calls.append({
                "时间": ts2str(r.get("ts")),
                "provider": r.get("provider", ""),
                "model": r.get("model", ""),
                "延迟ms": r.get("latency_ms", 0),
                "成功": r.get("success", False),
                "prompt_tokens": r.get("prompt_tokens", 0),
                "completion_tokens": r.get("completion_tokens", 0),
                "probe探测": r.get("probe", False),
                "错误": r.get("error_type", ""),
            })
    df_calls = pd.DataFrame(calls).sort_values("时间")

    # 长数字ID以文本写入，避免Excel科学计数法
    for _df in (df_overview, df_audience, df_tl, df_trend, df_cold, df_calls):
        for _col in _df.columns:
            if "ID" in _col or "房间" in _col:
                _df[_col] = _df[_col].fillna("未知").astype(str).str.replace(r"\.0$", "", regex=True)

    with pd.ExcelWriter(args.out, engine="openpyxl") as w:
        df_overview.to_excel(w, sheet_name="场次总览", index=False)
        df_audience.to_excel(w, sheet_name="长期观众画像", index=False)
        df_tl.to_excel(w, sheet_name="画像时间线", index=False)
        df_trend.to_excel(w, sheet_name="情感趋势", index=False)
        df_cold.to_excel(w, sheet_name="冷场台词", index=False)
        df_calls.to_excel(w, sheet_name="LLM调用", index=False)
        for name, df in [("场次总览", df_overview), ("长期观众画像", df_audience),
                         ("画像时间线", df_tl), ("情感趋势", df_trend),
                         ("冷场台词", df_cold), ("LLM调用", df_calls)]:
            ws = w.sheets[name]
            for col, width in zip("ABCDEFGHIJKLMN", [18] * 14):
                ws.column_dimensions[col].width = 16
            ws.freeze_panes = "A2"

    print("已生成:", args.out)
    print("场次总览 %d 行 | 长期观众画像 %d 人 | 画像时间线 %d 条 | 情感趋势 %d 行 | 冷场台词 %d 条 | LLM调用 %d 条"
          % (len(df_overview), len(df_audience), len(df_tl), len(df_trend), len(df_cold), len(df_calls)))


def glob_(pat):
    import glob
    return glob.glob(os.path.join(DATA, pat))


if __name__ == "__main__":
    main()
