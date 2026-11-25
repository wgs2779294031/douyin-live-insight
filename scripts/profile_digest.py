# -*- coding: utf-8 -*-
"""从主播原话转写里提「语言指纹」，供人工/模型写人设卡。

为什么需要它
------------
人设卡（`anchor_profiles/<room_id>.json`）注入 prompt 有 **300 中文字符** 硬上限
（`llm_service._INJECT_MAX_CHARS`），而且 `catchphrases` 只保留前 3 条、
冷场台词**每条必须含至少一个口头禅或粉丝称呼**。所以人设必须写得很紧，
字段值要来自主播真说的话 —— 否则出来的是「扣1/点关注」那种通用话术。

用法：
    python scripts/profile_digest.py                  # 全部主播
    python scripts/profile_digest.py --tag 框框看升学
    python scripts/profile_digest.py --out data/profile_digest.md

输出：每个主播一节，含时长/句数、称呼语、口癖候选（n-gram）、高频话题词、
均匀采样原话。零 API 成本。
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

# 称呼语候选：这些词原样出现在句子里才算「他真的这么叫观众」
ADDRESS = ["同学们", "同学", "家长朋友们", "家长", "各位家长", "各位", "大家",
           "朋友们", "宝子", "宝宝", "家人们", "兄弟", "姐妹们", "老铁",
           "学弟学妹", "学弟学妹们", "孩子们", "小伙子", "姑娘"]

# 口癖候选：主持人常用的过渡/确认/强调小词
TICS = ["对吧", "是不是", "对不对", "你知道", "你知道吗", "我跟你说", "说实话",
        "说句实话", "说白了", "记住了", "记好了", "注意", "重点来了", "划重点",
        "一定要", "千万别", "不要", "就是说", "然后", "其实", "问题是",
        "第一个", "第二个", "第三个", "最后", "举个例子", "比如说", "你看",
        "你想想", "想清楚", "我再说一遍", "听懂了吗", "有没有", "能不能",
        "好不好", "行不行", "是不是这个道理", "对吧同学们"]

# 话题词（教育/就业赛道）
TOPICS = ["考研", "考公", "考编", "就业", "实习", "校招", "秋招", "春招", "简历",
          "面试", "笔试", "调剂", "复试", "初试", "分数线", "国家线", "专业",
          "学校", "院校", "985", "211", "双一流", "本科", "专科", "研究生", "博士",
          "保研", "留学", "志愿", "填报", "选专业", "跨考", "二战", "上岸",
          "编制", "事业单位", "国企", "央企", "私企", "大厂", "银行", "教师编",
          "薪资", "待遇", "年薪", "五险一金", "户口", "落户", "签三方",
          "家长", "孩子", "规划", "赛道", "信息差", "避坑", "踩坑", "干货"]

STOP_HEAD = set("的了是我你他她它们这那就都也很还又不是在有和跟把被让给对说个一下么呢啊吧嘛")


def _sentence_split(text: str):
    return [s.strip() for s in re.split(r"[。！？!?；;\n]+", text) if s.strip()]


def _load_speech(paths):
    rows = []
    for p in paths:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                t = str(r.get("text") or "").strip()
                if len(t) < 2:
                    continue
                rows.append(r)
    rows.sort(key=lambda r: float(r.get("ts") or 0))
    return rows


def _room_ids():
    """tag -> 该 tag 在弹幕里出现最多的平台 room_id（人设文件名要用它）。"""
    out = {}
    for p in glob.glob(str(DATA / "danmu_douyin_*.jsonl")):
        base = os.path.basename(p)
        tag = base[len("danmu_douyin_"):-len("_%s.jsonl" % base[-16:-6])]
        c = collections.Counter()
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    rid = str(r.get("room_id") or "").strip()
                    if rid.isdigit() and len(rid) > 8:
                        c[rid] += 1
        except OSError:
            continue
        if c:
            out.setdefault(tag, c.most_common(1)[0][0])
    return out


def _tag_texts() -> dict:
    """tag -> 全部原话拼成的一段文本（算跨主播特异性用）。"""
    out = {}
    for tag, paths in _speech_by_tag().items():
        rows = _load_speech(sorted(paths))
        if rows:
            out[tag] = "\n".join(str(r.get("text") or "") for r in rows)
    return out


def specificity_table(texts: dict, min_chars: int = 2000) -> dict:
    """词 -> {tag: 特异性}。特异性 = 本主播每万字频率 ÷ 其他主播的中位频率。

    为什么需要（2026-10-28）：digest 原来只报**绝对次数**，所以「不要×200」
    「其实×150」这类**所有主播都说的通用词**会排在前面。实测让 doubao / deepseek
    照绝对次数写人设卡，两个模型都把「不要」「一定要」选成了口癖 —— 它们分不清
    「这个人特有的说法」和「大家都会的填充词」。特异性就是这条判据的算术形式：
    ≥2 才是这个人独有的，≈1 说明全行业都这么说。
    """
    tags = [t for t, c in texts.items() if len(c) >= min_chars]
    if len(tags) < 3:
        return {}
    table = {}
    for w in set(TICS) | set(ADDRESS):
        rate = {t: texts[t].count(w) * 10000.0 / max(1, len(texts[t])) for t in tags}
        for t in tags:
            others = sorted(rate[o] for o in tags if o != t)
            base = others[len(others) // 2]
            table.setdefault(w, {})[t] = (rate[t] / base) if base > 0 else rate[t]
    return table


def digest(tag: str, rows, room_id: str, spec: dict = None) -> str:
    texts = [str(r.get("text") or "").strip() for r in rows]
    dur = sum(float(r.get("dur") or 0) for r in rows)
    days = sorted({str(r.get("time") or "")[:10] for r in rows if r.get("time")})
    sentences = []
    for t in texts:
        sentences.extend(_sentence_split(t))
    sentences = [s for s in sentences if 3 <= len(s) <= 60]

    addr = collections.Counter()
    for w in ADDRESS:
        n = sum(t.count(w) for t in texts)
        if n:
            addr[w] = n
    tics = collections.Counter()
    for w in TICS:
        n = sum(t.count(w) for t in texts)
        if n:
            tics[w] = n
    topics = collections.Counter()
    for w in TOPICS:
        n = sum(t.count(w) for t in texts)
        if n:
            topics[w] = n

    # 句首 2~4 字口头短语（他开口的方式）
    head = collections.Counter()
    for s in sentences:
        h = s[:4]
        if len(h) >= 2 and h[0] not in STOP_HEAD:
            head[h] += 1
    # 句尾 3 字（他的收尾习惯）
    tail = collections.Counter()
    for s in sentences:
        if len(s) >= 6:
            tail[s[-3:]] += 1

    # 均匀采样：把句子按时间切成 N 段，每段取中间一句
    N = 18
    picks = []
    if sentences:
        step = max(1, len(sentences) // N)
        for i in range(0, len(sentences), step):
            picks.append(sentences[i])
        picks = picks[:N]

    lines = []
    lines.append("### %s" % tag)
    lines.append("")
    lines.append("- room_id（人设文件名）：`%s`" % (room_id or "（未在弹幕里找到）"))
    lines.append("- 素材：%d 段 / 约 %.0f 分钟 / 日期 %s" %
                 (len(rows), dur / 60.0, ", ".join(days)))
    lines.append("- 句数：%d（3~60 字）" % len(sentences))
    lines.append("")
    lines.append("**他怎么叫观众**（次数）：")
    lines.append("  " + ("、".join("%s×%d" % (k, v) for k, v in addr.most_common(8)) or "（无）"))
    lines.append("")
    lines.append("**口癖/过渡词**（次数）：")
    lines.append("  " + ("、".join("%s×%d" % (k, v) for k, v in tics.most_common(14)) or "（无）"))
    lines.append("")
    if spec:
        cand = []
        for w, per_tag in spec.items():
            s = per_tag.get(tag, 0.0)
            n = sum(t.count(w) for t in texts)
            if s >= 1.5 and n >= 3:
                cand.append((w, s, n))
        cand.sort(key=lambda x: -x[1])
        lines.append("**候选口癖（特异性 = 本主播每万字频率 ÷ 其他主播中位频率；"
                     "≥2 才算他特有的，≈1 是全行业通用词，别写进人设卡）**：")
        lines.append("  " + ("、".join("%s×%d（×%.1f）" % (w, n, s)
                                       for w, s, n in cand[:12]) or "（无）"))
        lines.append("")
    lines.append("**高频话题词**：")
    lines.append("  " + ("、".join("%s×%d" % (k, v) for k, v in topics.most_common(16)) or "（无）"))
    lines.append("")
    lines.append("**句首习惯**（开口头 2~4 字）：")
    lines.append("  " + "、".join("%s×%d" % (k, v) for k, v in head.most_common(10)))
    lines.append("")
    lines.append("**句尾习惯**（末 3 字）：")
    lines.append("  " + "、".join("%s×%d" % (k, v) for k, v in tail.most_common(8)))
    lines.append("")
    lines.append("**原话采样**（按时段均匀取）：")
    for s in picks:
        lines.append("  - %s" % s)
    lines.append("")
    return "\n".join(lines)


def _speech_by_tag() -> dict:
    """tag -> [speech_*.jsonl]，文件名约定 `speech_<tag>_<YYYY-MM-DD>.jsonl`。"""
    files = collections.defaultdict(list)
    for p in glob.glob(str(DATA / "speech_*.jsonl")):
        base = os.path.basename(p)
        tag = base[len("speech_"):-len("_%s.jsonl" % base[-16:-6])]
        files[tag].append(p)
    return files


def _count_word(rows, word: str) -> int:
    return sum(str(r.get("text") or "").count(word) for r in rows)


def _minutes(rows) -> float:
    return sum(float(r.get("dur") or 0) for r in rows) / 60.0


# 素材量阈值（分钟）—— 写一张卡要的是「他说了多少话」，不是「等了多少天」
READY_THIN_MIN = 15.0     # 以下只能写薄卡：称呼 + 1~2 个高频口癖
READY_OK_MIN = 60.0       # 够写一张能用的卡
READY_SOLID_MIN = 180.0   # 多场覆盖，够写稳的卡


def _material_hint(minutes: float) -> str:
    if minutes >= READY_SOLID_MIN:
        return "素材够，可写稳卡"
    if minutes >= READY_OK_MIN:
        return "可写卡"
    if minutes >= READY_THIN_MIN:
        return "只能写薄卡（再攒一些更好）"
    return "素材太少，先别写"


def _roster_tags(roster_path=None) -> list:
    """名册里的 tag（读不到就返回空，不影响体检）。"""
    p = Path(roster_path) if roster_path else ROOT / "anchors.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return [str(a["tag"]) for a in data.get("anchors", [])
                if isinstance(a, dict) and a.get("tag")]
    except Exception:  # noqa: BLE001
        return []


def _card_block(tag: str, card_path, files, min_new_minutes: float = 10.0):
    """一张卡的体检段。返回 (lines, stale)；stale=True 表示该重写。"""
    out = ["### %s" % tag]
    try:
        card = json.loads(card_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        out.append("- **读不出来**：%s" % e)
        return out, False
    cps = [c for c in (card.get("catchphrases") or []) if c]
    fan = card.get("fan_call") or ""
    all_paths = sorted(files.get(tag, []))
    card_mtime = card_path.stat().st_mtime
    new_paths = [p for p in all_paths if os.path.getmtime(p) > card_mtime]
    all_rows = _load_speech(all_paths)
    new_rows = _load_speech(new_paths)
    if not all_rows:
        out.append("- 没有转写素材（卡里的内容无法核对）")
        return out, False

    days = sorted({str(r.get("time") or "")[:10] for r in all_rows if r.get("time")})
    cp_all = "、".join("%s×%d" % (c, _count_word(all_rows, c)) for c in cps) or "（卡里没写）"
    cp_new = ("、".join("%s×%d" % (c, _count_word(new_rows, c)) for c in cps)
              or "（无）") if new_rows else "（无新素材）"
    new_min = _minutes(new_rows)
    out.append("- 卡：`%s`（改于 %s）" % (card_path.name,
                 time.strftime("%m-%d %H:%M", time.localtime(card_mtime))))
    out.append("- 素材：%d 段 / %.0f 分钟 / %d 天" % (len(all_rows), _minutes(all_rows), len(days)))
    out.append("- 口头禅（全部素材）：%s；称呼「%s」×%d" % (cp_all, fan, _count_word(all_rows, fan)))
    out.append("- **卡后新素材**：%d 段 / %.0f 分钟；口头禅 %s；称呼×%d" % (
        len(new_rows), new_min, cp_new, _count_word(new_rows, fan)))
    if not new_rows or new_min < min_new_minutes:
        out.append("- 判定：**无法验证**（卡后素材不足 %.0f 分钟，继续攒）" % min_new_minutes)
        return out, False
    hits = sum(_count_word(new_rows, c) for c in cps) + _count_word(new_rows, fan)
    rate = hits / new_min
    if rate < 0.3:
        out.append("- 判定：**⚠️ 该重写**（%.2f 次/分钟，主播可能换了讲法）" % rate)
        return out, True
    out.append("- 判定：正常（%.2f 次/分钟）" % rate)
    return out, False


def check_cards(cards_dir, min_new_minutes: float = 10.0) -> "tuple[str, bool]":
    """人设卡保鲜检查：卡写完**以后**主播的说话方式变了没有。

    为什么需要：卡是静态 JSON，运行时不读转写（llm_service 只在首次加载时读卡）。
    新一场的转写不会回灌人设 —— 卡只会越来越旧，而且没有任何地方会告诉你它旧了。
    这里不重写卡，只回答「该不该重写」。

    判定（只看**卡 mtime 之后**新增的素材，之前的素材是写卡时的依据，不算）：
    - 无新素材 / 新素材 < min_new_minutes 分钟 → 「无法验证」，继续攒；
    - 新素材够，但卡里的口头禅+称呼合计 < 0.3 次/分钟 → 「⚠️ 该重写」；
    - 否则 → 「正常」。

    :return: (markdown, 是否有卡被判该重写)
    """
    files = _speech_by_tag()
    out = ["# 人设卡保鲜检查", "",
           "> 卡写完之后的转写才算「新素材」。卡里的口头禅/称呼在新素材里不再出现，"
           "说明卡过时了（主播换了讲法）。", ""]
    stale = False
    for card_path in sorted(Path(cards_dir).glob("*.json")):
        tag = card_path.stem
        if tag == "default":
            continue
        lines, s = _card_block(tag, card_path, files, min_new_minutes)
        out.extend(lines)
        out.append("")
        stale = stale or s
    return "\n".join(out), stale


def status_report(cards_dir, roster_path=None, min_new_minutes: float = 10.0):
    """一张表回答三件事：谁该写卡、谁的卡该重写、谁还没素材。

    换赛道 / 加新主播时跑这个，不用逐个问「素材够不够」。
    没卡的分三类（按素材分钟数），有卡的复用保鲜判定。

    :return: (markdown, 被判该重写的 tag 列表)
    """
    files = _speech_by_tag()
    cards = {p.stem: p for p in Path(cards_dir).glob("*.json") if p.stem != "default"}
    roster = _roster_tags(roster_path)

    out = ["# 人设卡状态", "",
           "> 写卡看的是**他说了多少分钟**，不是等了几天："
           "≥%.0f 分钟能写薄卡，≥%.0f 分钟够用，≥%.0f 分钟算稳。" % (
               READY_THIN_MIN, READY_OK_MIN, READY_SOLID_MIN), ""]

    out.append("## 还没建卡")
    missing = sorted(set(list(files) + roster) - set(cards))
    if not missing:
        out.append("- （无）")
    for tag in missing:
        rows = _load_speech(sorted(files.get(tag, [])))
        if not rows:
            out.append("- **%s**：没有转写素材（开播后自动攒；名册里 record=false 的永远没有）" % tag)
            continue
        days = len({str(r.get("time") or "")[:10] for r in rows if r.get("time")})
        mins = _minutes(rows)
        out.append("- **%s**：%d 段 / %.0f 分钟 / %d 天 → **%s**" % (
            tag, len(rows), mins, days, _material_hint(mins)))
    out.append("")

    out.append("## 已有卡")
    stale = []
    for tag in sorted(cards):
        lines, s = _card_block(tag, cards[tag], files, min_new_minutes)
        out.extend(lines)
        out.append("")
        if s:
            stale.append(tag)

    out.append("## 结论")
    if stale:
        out.append("- ⚠️ 该重写：%s" % "、".join(stale))
    else:
        out.append("- 没有卡被判过时")
    ready = [t for t in missing
             if _minutes(_load_speech(sorted(files.get(t, [])))) >= READY_THIN_MIN]
    if ready:
        out.append("- 可以开写：%s" % "、".join(ready))
    return "\n".join(out), stale


def main() -> int:
    ap = argparse.ArgumentParser(description="主播原话 → 语言指纹（写人设卡用）")
    ap.add_argument("--tag", help="只跑一个主播")
    ap.add_argument("--out", default=str(DATA / "profile_digest.md"))
    ap.add_argument("--check", action="store_true",
                    help="不生成指纹，改查现有 人设卡 是否过时（卡后新素材里口头禅还在不在）")
    ap.add_argument("--status", action="store_true",
                    help="一张表：谁该写卡（按素材分钟数）、谁的卡该重写、谁还没素材")
    ap.add_argument("--roster", default=str(ROOT / "anchors.json"),
                    help="名册路径（--status 用它找「名册里有但没卡」的主播）")
    ap.add_argument("--cards", default=str(ROOT / "anchor_profiles"),
                    help="人设卡目录（默认 anchor_profiles/）")
    args = ap.parse_args()

    if args.status:
        text, stale = status_report(args.cards, args.roster)
        print(text)
        return 1 if stale else 0

    if args.check:
        text, stale = check_cards(args.cards)
        print(text)
        return 1 if stale else 0

    files = _speech_by_tag()
    rids = _room_ids()
    tags = sorted(files) if not args.tag else [args.tag]
    # 特异性要拿"全体主播"做分母，所以先算一遍全量文本（再只输出 --tag 那一段）
    all_texts = _tag_texts()
    spec = specificity_table(all_texts)
    if not spec:
        print("⚠️ 样本主播不足 3 个（或文本太短），本次不输出特异性", file=sys.stderr)
    chunks = []
    for tag in tags:
        if tag not in files:
            print("没有转写：%s" % tag, file=sys.stderr)
            continue
        rows = _load_speech(sorted(files[tag]))
        if not rows:
            print("转写为空：%s" % tag, file=sys.stderr)
            continue
        chunks.append(digest(tag, rows, rids.get(tag, ""), spec))

    text = "# 主播语言指纹（供写人设卡）\n\n" + "\n".join(chunks)
    Path(args.out).write_text(text, encoding="utf-8")
    print("已写出 %s（%d 个主播 / %d 字符）" % (args.out, len(chunks), len(text)))
    for tag in tags:
        if tag in rids:
            print("  %-24s room_id=%s" % (tag, rids[tag]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
