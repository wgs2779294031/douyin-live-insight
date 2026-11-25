# -*- coding: utf-8 -*-
r"""v3.6 竞品监控日报（离线生成）。

面向「就业辅导机构客户」：每天直播结束后，从已落盘的场次数据自动产出一份
竞品主播监控日报，回答四个问题——
    竞品昨天播得怎么样 / 哪个时刻最火 / 观众在关心什么 / 他做了什么运营动作。

设计原则
--------
1. **口径一致**：场次统计直接复用 `session_report.compute_session`（不另起炉灶），
   事件计数同样来自落盘原值；本模块只做「聚合 + 归类 + 呈现」。
2. **离线、零运行时风险**：只读 data/ 下已落盘文件；不接实时链路、不改 31 列契约。
3. **成本受控**：LLM **只做「每场一次聚合总结」**（模块⑦），**禁止按弹幕逐条调用**；
   单场已记录用量与发送时单价快照估算落进分场与总览，缺口不补零。
4. **容错显式**：主播当天没播 / 没福袋 / 数据缺失，对应模块**显式标「无」**，
   不报错、不编造。区分「无数据（—）」与「确实为 0」。
5. **就业辅导场景关闭 PK/团战模块**（本场景不适用）。

输入（每主播 / 每天一组，均来自既有监控采集层）
    session_<tag>_<date>.json       场次统计 + 高能时刻（session_report 产物）
    danmu_<tag>_<date>.jsonl        主链路事件（danmaku/enter/like/gift）
    events_<tag>_<date>.jsonl       运营事件（social/room_rank/fansclub/福袋等）
    room_meta_<tag>.json            主播名 / 直播标题

输出（默认 <dir>/reports_<date>/）
    竞品监控日报_<date>.md          单文件总览（= 横向对比表 + 各主播分场）
    <tag>_分场_<date>.md            每主播一份独立分场
    report_index_<date>.json        机器可读摘要（供转飞书表格 / 次日涨跌对比）

用法：
    python daily_report.py --date 2026-09-30
    python daily_report.py --date 2026-09-30 --tag 教育-辰哥英语 --tag 高流量-锋哥说
    python daily_report.py --date 2026-09-30 --llm        # 启用 LLM 自动结论（真实计费）
"""

from __future__ import annotations

import argparse
import collections
import glob
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# 复用既有规则层口径（场次统计 + 高能时刻），避免两套算法漂移
from session_report import compute_session, discover_inputs
from scripts.board_usage import aggregate_records, merge_usage_summaries, usage_text

logger = logging.getLogger("danmu")

# ================================================================ 文本归类字典
STOPWORDS = set(
    "的了是在我有你就他她它们和也不都一个这那好吗吧啊哦嗯呀呢么什么怎么为什么"
    "这样那样现在今天明天大家我们你们他们真的觉得感觉可以知道看看看到喜欢主播"
    "直播间家人朋友有没有来了已经还是就是不是没有自己这个那个然后因为所以如果"
    "虽然但是其实应该可能大概一直非常特别哈哈哈哈哈嘿嘿谢谢感谢老师嗯嗯嗯"
)

# 观众提问聚类（按「命中词数」打分归类，命中多者优先；同分按本表先后；
# **零命中才落「其他」** —— 严禁把全部提问塞进兜底类）
CATEGORY_RULES: List[Tuple[str, List[str]]] = [
    ("简历修改", ["简历", "写简历", "改简历", "简历模板", "投简历", "简历优化", "简历怎么写"]),
    ("面试技巧", ["面试", "面经", "群面", "单面", "终面", "hr面", "面试技巧", "面试题"]),
    ("实习经验", ["实习", "找实习", "实习经历", "日常实习", "暑期实习", "实习经历"]),
    ("校招社招", ["校招", "社招", "秋招", "春招", "提前批", "补录", "内推", "应届"]),
    ("考公考编", ["考公", "公务员", "事业编", "考编", "国考", "省考", "选调", "编制", "体制"]),
    ("考研升学", ["考研", "研究生", "读研", "调剂", "初试", "保研", "专硕", "学硕", "上岸"]),
    ("转行跨行", ["转行", "跨行", "换行", "转岗", "换个行业", "不做本专业", "半路转"]),
    ("年龄学历焦虑", ["年龄", "几岁", "多岁", "大龄", "30岁", "35岁", "岁数", "太晚",
                  "来得及", "学历", "双非", "第一学历", "卡学历", "专升本", "大专", "不好找工作"]),
    ("专业选择", ["专业", "选专业", "学什么", "跨专业", "转专业", "对口"]),
    ("院校层次", ["学校", "院校", "大学", "985", "211", "双一流", "一本", "二本",
                "三本", "专科", "本科", "学院", "名校",
                # 常见院校简称（弹幕里几乎只写简称，不收会把「院校对比」整类漏掉）
                "上财", "暨大", "深大", "华师", "华科", "华五", "中大", "复旦",
                "清华", "北大", "华政", "两财一贸", "末流985", "末流211"]),
    ("薪资待遇", ["工资", "薪资", "收入", "待遇", "前景", "月薪", "年薪", "好就业", "坑不坑", "值不值"]),
    ("价格购买", ["价格", "多少钱", "收费", "费用", "报名", "课程", "资料", "链接",
                "怎么买", "优惠", "下单", "付费", "购买", "咨询费"]),
    ("政策条件", ["政策", "条件", "要求", "资格", "报名条件", "限制", "符合"]),
    ("志愿升学", ["高考", "志愿", "填报", "分数", "录取", "单招", "复读", "职高", "中专", "分数线"]),
    # 直播间特有的两类：问「怎么连麦/怎么领福利」与问「某公司/某岗位怎么样」，
    # 就业/升学类直播几乎每场都有，不归入它们就会全落「其他」。
    ("连麦福利", ["连麦", "上麦", "连线", "连上", "麦", "灯牌", "礼包", "粉丝团", "加团"]),
    ("企业与岗位", ["公司", "事务所", "国企", "央国企", "大厂", "这家", "岗位", "职位",
                "甲方", "乙方", "了解不了解", "值得去", "适合长期"]),
    # 与求职无关的直播间互动（问主播/问作息/夸主播）。这类在每个直播间都稳定出现，
    # 若不单列就会全落「其他」而掩盖上面的主题分布。
    ("直播闲聊", ["开播", "下播", "停播", "播吗", "加班", "放假", "美女", "帅哥",
                "声音", "主播", "直播间"]),
]

# 问候语 / 口头禅（不得进入「高频话题」——它们只是礼貌或复读，不带业务信息）
GREETING_PET_PHRASES = {
    "老师好", "老师们好", "老师好呀", "老师好老师好", "主播好", "大家好", "上午好", "中午好",
    "下午好", "晚上好", "早安", "晚安", "在吗", "来了", "来了来了", "打卡", "签到",
    "哈喽", "hello", "hi", "嗨", "拜拜", "再见", "对对对", "对对", "是的", "对啊",
    "嗯嗯", "哦哦", "好的", "收到", "谢谢", "谢谢老师", "感谢", "辛苦了", "加油",
    "棒棒哒", "优秀", "厉害", "厉害了", "牛逼", "好帅", "帅哥", "美女", "哈哈", "哈哈哈",
    "呵呵", "666", "支持", "点赞", "爱了", "真棒", "好棒", "知道了", "了解", "明白",
    "好可爱", "太好啦", "不错", "真好", "笑死", "没错", "确实", "真的吗",
}

# 情感（无画像时规则兜底）
NEG_WORDS = ["退费", "退款", "骗", "垃圾", "听不懂", "没用", "不值", "套路", "坑",
             "恶心", "太差", "假的", "割韭菜", "废话", "浪费时间", "取关"]
POS_WORDS = ["谢谢", "感谢", "厉害", "学到了", "有用", "支持", "加油", "喜欢", "真好",
             "受益", "讲得好", "有帮助", "涨知识"]

# 消费意愿信号
CONSUME_WORDS = ["价格", "多少钱", "收费", "费用", "报名", "报名费", "课程", "资料",
                 "链接", "怎么买", "优惠", "下单", "付费", "购买", "咨询", "领取", "私信"]

# 引导动作（主播话术侧，从弹幕/事件间接观察）
GUIDE_GROUPS: List[Tuple[str, List[str]]] = [
    ("引导关注", ["关注", "点关注", "关注一下", "加关注"]),
    ("引导加群", ["加群", "进群", "粉丝群", "社群", "扣1", "扣 1", "扣个1"]),
    ("引导领资料", ["领资料", "资料", "领取", "评论区", "置顶", "主页", "私信", "留言"]),
]

# 身份标签（从弹幕推断；推断不出**不硬编**）
IDENTITY_RULES: List[Tuple[str, List[str]]] = [
    ("考研", ["考研", "研究生", "读研", "二战", "上岸"]),
    ("考公", ["考公", "公务员", "事业编", "考编", "国考", "省考"]),
    ("在职", ["在职", "上班", "工作党", "职场"]),
    ("大三大四", ["大三", "大四", "应届"]),
    ("高三", ["高三", "高考生"]),
    ("大专/专科", ["大专", "专科", "职高", "中专"]),
    ("家长", ["家长", "孩子", "我妈", "我爸", "我儿子", "我女儿"]),
    ("跨考/转行", ["跨考", "跨专业", "转行"]),
]

# 福袋 / 抽奖相关事件类型（monitor 落盘 type 原值）
LOTTERY_EVENT_TYPES = {"lottery_event", "luckybox", "luckybox_reward", "lottery_draw_result"}

_QMARK_RE = re.compile(r"[?？]")
# 疑问语气助词：**只认 吗/呢/嘛**。`吧` 在弹幕里绝大多数是建议或推测
# （「15左右吧」「直接工作吧」「985吧」），把它们当问句会同时虚高问句数与污染主题聚类，
# 故 `吧` 不作疑问标记——除非同句另有 ? / 吗 / 呢 / 疑问词（由下面 return 逻辑覆盖）。
_QMODAL_RE = re.compile(r"(吗|呢|嘛)\s*$")
_QWORDS = ["怎么", "如何", "能不能", "可不可以", "是不是", "要不要", "多少", "怎么办",
           "哪些", "哪个", "哪里", "什么时候", "什么", "有没有", "请问", "想问", "求解",
           "求问", "咋", "能报", "够不够", "来得及", "行不行", "对吗", "求教"]
_TOKEN_RE = re.compile(r"[\u4e00-\u9fa5]{2,}|[A-Za-z0-9]{2,}")
# 抖音弹幕里的表情/挂件文本形如「[流泪]」「【鼓掌】」→ 分词前先剔除，避免污染话题
_EMOJI_BRACKET_RE = re.compile(r"[\[【][^\]】]{1,8}[\]】]")


# ================================================================ 基础工具
def _load_jsonl(path: Optional[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not path or not os.path.exists(path):
        return out
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


def _load_json(path: Optional[str]) -> Optional[Dict[str, Any]]:
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def mask_uid(uid: Any) -> str:
    """用户 ID 脱敏：sha256 前 12 位（稳定不可逆，与 summarize.py 口径一致）。"""
    return hashlib.sha256(str(uid).encode("utf-8")).hexdigest()[:12]


# ★ 平台占位/空 user_id（2026-10-01 实测）：抖音在部分房间下发「隐私化」用户，
#   `user_id` 恒为 "111111"（全部弹幕同值）。此时若仍按 user_id 聚合，
#   会把整场判成「1 人发言 → 疑似自己人撑场」，是**误报**。
#   正确做法：退回昵称（uname，实测同场有 253 个不同昵称）并在报告里标注口径。
_PLACEHOLDER_UIDS = {"", "0", "1", "111111", "1111111", "11111111111"}

_NICKNAME_KEY_NOTE = ("昵称（平台未返回真实 user_id，按昵称统计；同名/同掩码可能并成一人）")
_UID_KEY_NOTE = "平台 user_id"


def resolve_speaker(d: Dict[str, Any]) -> Tuple[str, str, bool]:
    """把一条弹幕归到一个「发言人键」。返回 ``(key, uname, 是否昵称兜底)``。

    - `user_id` 可用 → 直接用（key = user_id，兜底位 False）
    - `user_id` 是占位/空 → 退回昵称（key = ``"n:" + uname``，兜底位 True）
    - 两者都空 → ``("", "", False)``，该条不计入发言人（不是「匿名观众」）
    """
    uid = str(d.get("user_id") or d.get("user_hash") or "").strip()
    uname = str(d.get("uname") or "").strip()
    if uid and uid not in _PLACEHOLDER_UIDS:
        return uid, uname, False
    if uname:
        return "n:" + uname, uname, True
    return "", "", False


def speaker_key_note(used_fallback: bool) -> str:
    """说话人统计口径说明（进报告，避免读者把数字当成平台原值）。"""
    return _NICKNAME_KEY_NOTE if used_fallback else _UID_KEY_NOTE


def ts2clock(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S")
    except Exception:
        return "—"


def ts2min(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%H:%M")
    except Exception:
        return "—"


def _has_substance(text: str) -> bool:
    """是否有实义内容：剔除表情挂件后，至少含一个 >=2 字的中文词或 >=2 位字母数字。

    「？」「啊？」「？？？」这类只有标点/单字语气词的弹幕不是提问，不能计入问句聚类。
    """
    stripped = _EMOJI_BRACKET_RE.sub(" ", text or "")
    return bool(_TOKEN_RE.search(stripped))


def _is_question(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if not _has_substance(t):
        return False
    if _QMARK_RE.search(t) or _QMODAL_RE.search(t):
        return True
    return any(w in t for w in _QWORDS)


def _is_greeting(w: str) -> bool:
    """问候语 / 口头禅判定：整词命中，或包含长度 >=3 的问候短语。"""
    if w in GREETING_PET_PHRASES:
        return True
    return any(len(g) >= 3 and g in w for g in GREETING_PET_PHRASES)


def _tokens(texts: List[str]) -> collections.Counter:
    counter: collections.Counter = collections.Counter()
    for t in texts:
        if not t:
            continue
        t = _EMOJI_BRACKET_RE.sub(" ", t)   # 剔除 [流泪] 类表情文本
        for w in _TOKEN_RE.findall(t):
            if len(w) < 2 or len(w) > 12 or w.isdigit():
                continue          # 去停用词 / 去过长整句 / 去纯数字噪声
            if w.lower() in STOPWORDS or _is_greeting(w):
                continue          # 去停用词 / 去问候语口头禅（老师好/上午好/对对对…）
            counter[w] += 1
    return counter


_CORE_EVENT_TYPES = ("danmaku", "enter", "like", "gift")


def _norm_counts(raw: Dict[str, Any]) -> Dict[str, int]:
    """事件计数归一化：核心类型缺失即 0（该场文件完整，缺失=真 0，非「无数据」）。"""
    out = {k: int(raw.get(k, 0) or 0) for k in _CORE_EVENT_TYPES}
    for k, v in raw.items():
        if k not in out:
            out[k] = int(v or 0)
    return out


def _fmt_num(value: Any) -> str:
    """数值格式化：整数千分位；缺失（None）→「—」（区分「无数据」与「为 0」）。"""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        value = round(value, 3)
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer()):
        return f"{int(value):,}"
    return str(value)


def _arrow(cur: Any, prev: Any) -> str:
    """涨跌箭头（vs 前日）。任一缺失返回空串。"""
    try:
        c, p = float(cur), float(prev)
    except (TypeError, ValueError):
        return ""
    if p == 0:
        return ""
    delta = c - p
    if abs(delta) < 1e-9:
        return "（持平）"
    pct = delta / abs(p) * 100
    mark = "↑" if delta > 0 else "↓"
    return f"（{mark}{abs(pct):.0f}%）"


# ================================================================ 模块 ① 分场基础指标
def build_row(session: Dict[str, Any], events: List[Dict[str, Any]],
              meta: Optional[Dict[str, Any]], tag: str,
              topics: List[str]) -> Dict[str, Any]:
    """单场横向对比行（总览表一行）。口径全部来自落盘原值聚合。"""
    s = session.get("session") or {}
    tc = s.get("type_counts") or {}
    danmaku = int(tc.get("danmaku", 0) or 0)
    like = int(tc.get("like", 0) or 0)
    gift = int(tc.get("gift", 0) or 0)
    enter = int(tc.get("enter", 0) or 0)

    follows = sum(1 for e in events if e.get("type") == "social" and int(e.get("action") or 0) == 1)
    shares = sum(1 for e in events if e.get("type") == "social" and int(e.get("action") or 0) in (2, 3))
    lottery_n = sum(1 for e in events if e.get("type") in LOTTERY_EVENT_TYPES)

    pv_first = s.get("total_pv_first")
    pv_last = s.get("total_pv_last")
    pv_delta = (int(pv_last) - int(pv_first)) if (isinstance(pv_first, int)
                                                  and isinstance(pv_last, int)) else None

    interactions = danmaku + like + gift + follows + shares
    if pv_delta and pv_delta > 0:
        interaction_rate = round(interactions / pv_delta * 100, 2)
    else:
        interaction_rate = None

    return {
        "tag": tag,
        "anchor_name": (meta or {}).get("anchor_name") or tag,
        "title": (meta or {}).get("title") or "",
        "duration_seconds": s.get("duration_seconds"),
        "start_ts": s.get("start_ts"),
        "end_ts": s.get("end_ts"),
        "online_peak": s.get("online_peak"),
        "online_peak_ts": s.get("online_peak_ts"),
        "pv_first": pv_first,
        "pv_last": pv_last,
        "pv_delta": pv_delta,
        "danmaku_total": danmaku,
        "like_total": like,
        "gift_total": gift,
        "enter_total": enter,
        "follow_total": follows,
        "share_total": shares,
        "interactions": interactions,
        "interaction_rate": interaction_rate,
        "lottery_count": lottery_n,
        "top_topic": topics[0] if topics else None,
        # 次日涨跌对比所需的分母指标
        "_cmp": {
            "duration_seconds": s.get("duration_seconds"),
            "online_peak": s.get("online_peak"),
            "danmaku_total": danmaku,
            "pv_delta": pv_delta,
            "interaction_rate": interaction_rate,
        },
    }


# ================================================================ 模块 ② 单场概览
def build_overview(session: Dict[str, Any], meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    s = session.get("session") or {}
    dur = s.get("duration_seconds")
    return {
        "anchor_name": (meta or {}).get("anchor_name") or s.get("room_tag"),
        "title": (meta or {}).get("title") or "（无标题）",
        "date": datetime.fromtimestamp(s.get("start_ts") or time.time()).strftime("%Y-%m-%d"),
        "time_range": f"{ts2clock(s.get('start_ts'))} - {ts2clock(s.get('end_ts'))}",
        "duration_text": f"{int(dur // 60)} 分 {int(dur % 60)} 秒" if isinstance(dur, int) else "—",
        "online_peak": s.get("online_peak"),
        "online_peak_clock": ts2clock(s.get("online_peak_ts")),
        "pv_first": s.get("total_pv_first"),
        "pv_last": s.get("total_pv_last"),
        "pv_delta": None,
        "type_counts": _norm_counts(s.get("type_counts") or {}),
        "event_total": s.get("event_total"),
    }


# ================================================================ 模块 ③ 高能时刻 TOP3
def _cluster_texts(texts: List[str], max_reps: int = 2) -> Dict[str, Any]:
    """把一窗弹幕聚成「几句话」：高频关键词 + 少量代表短句（不堆原文）。"""
    counter = _tokens(texts)
    keywords = [w for w, _ in counter.most_common(4)]
    reps: List[str] = []
    seen = set()
    for t in texts:
        t = (t or "").strip()
        if not (3 <= len(t) <= 28) or t in seen:
            continue
        seen.add(t)
        reps.append(t)
        if len(reps) >= max_reps:
            break
    return {"keywords": keywords, "examples": reps}


def _highlight_intensity(h: Dict[str, Any]) -> float:
    return float(max(int(h.get("rate_peak_per10s") or 0),
                     int(h.get("online_gain") or 0) / 4.0,
                     int(h.get("keyword_hits") or 0)))


def build_highlights(session: Dict[str, Any], top_n: int = 3) -> List[Dict[str, Any]]:
    """高能时刻 TOP3：按强度排序，附「窗口观众在刷什么」的聚类结果。"""
    hl = list(session.get("highlights") or [])
    hl.sort(key=_highlight_intensity, reverse=True)
    out = []
    for h in hl[:top_n]:
        cluster = _cluster_texts(h.get("window_texts") or [])
        out.append({
            "clock": ts2clock(h.get("ts")),
            "kind": h.get("kind"),
            "online_gain": h.get("online_gain"),
            "online_peak": h.get("online_peak"),
            "rate_peak_per10s": h.get("rate_peak_per10s"),
            "window_danmaku_count": h.get("window_danmaku_count"),
            "keywords": cluster["keywords"],
            "examples": cluster["examples"],
        })
    return out


# ================================================================ 模块 ④ 观众话题与高频提问
def analyze_topics(danmaku_texts: List[str], portrait: Optional[Dict[str, Any]] = None,
                   n: int = 8) -> List[str]:
    """高频话题 TOP：**优先复用 LLM 画像 interest_topics**，再补弹幕高频词。

    两个来源都过滤问候语/口头禅（老师好 / 上午好 / 对对对 …），去重保序。
    """
    topics: List[str] = []
    seen = set()

    def _push(x: str) -> None:
        x = (x or "").strip()
        if not x or x in seen or _is_greeting(x):
            return
        seen.add(x)
        topics.append(x)

    if isinstance(portrait, dict):
        for t in (portrait.get("interest_topics") or []):
            _push(str(t))
            if len(topics) >= n:
                return topics[:n]
    for w, _ in _tokens(danmaku_texts).most_common(n * 2):
        _push(w)
        if len(topics) >= n:
            break
    return topics[:n]


def _classify_question(text: str) -> Optional[str]:
    """按命中关键词数打分归类（命中多者优先；同分按 CATEGORY_RULES 顺序）。"""
    best: Optional[str] = None
    best_hits = 0
    for name, keys in CATEGORY_RULES:
        hits = sum(1 for k in keys if k in text)
        if hits > best_hits:
            best_hits, best = hits, name
    return best


def analyze_questions(danmaku_texts: List[str],
                      portrait: Optional[Dict[str, Any]] = None,
                      max_examples: int = 3) -> Dict[str, Any]:
    """观众提问聚类：**优先用 LLM 画像的 question_topics**；无画像时规则兜底。

    规则路径按主题打分归类（简历/面试/实习/校招/考公考研/转行/年龄学历焦虑…），
    **零命中才落「其他」**，绝不用「其他」兜底全部。
    """
    # —— 路径 1：画像（LLM）
    if isinstance(portrait, dict) and portrait.get("question_topics"):
        clusters = []
        for it in portrait["question_topics"]:
            if not isinstance(it, dict):
                continue
            topic = str(it.get("topic") or "").strip()
            if not topic:
                continue
            clusters.append({
                "category": topic,
                "count": int(it.get("count") or 0),
                "examples": [str(e) for e in (it.get("examples") or [])][:max_examples],
            })
        if clusters:
            clusters.sort(key=lambda c: c["count"], reverse=True)
            return {"source": "画像(LLM)", "total_questions": sum(c["count"] for c in clusters),
                    "clusters": clusters, "unmatched": 0}

    # —— 路径 2：规则兜底
    buckets: Dict[str, List[str]] = collections.defaultdict(list)
    total_q = 0
    for t in danmaku_texts:
        if not _is_question(t):
            continue
        total_q += 1
        text = (t or "").strip()
        cat = _classify_question(text) or "其他"
        buckets[cat].append(text)

    clusters = []
    for name, items in sorted(buckets.items(), key=lambda kv: len(kv[1]), reverse=True):
        seen = set()
        examples = []
        # 例证取「出现次数最多」再取「最短」——最高频的那句最能代表这一类，
        # 按长度升序会让「？」之类的碎片句子抢到例证位。
        for t, _n in collections.Counter(items).most_common():
            if t in seen:
                continue
            seen.add(t)
            examples.append(t)
            if len(examples) >= max_examples:
                break
        clusters.append({"category": name, "count": len(items), "examples": examples})
    unmatched = buckets.get("其他", [])
    return {"source": "规则推断（无画像）", "total_questions": total_q,
            "clusters": clusters, "unmatched": len(unmatched),
            "unmatched_unique": len(set(unmatched))}


def analyze_sentiment(danmaku_texts: List[str],
                      portrait: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """情感分布：优先用画像 sentiment；否则规则兜底（并标注为规则推断）。"""
    if isinstance(portrait, dict) and isinstance(portrait.get("sentiment"), dict):
        s = portrait["sentiment"]
        return {"source": "画像(LLM)", "negative": s.get("negative"),
                "neutral": s.get("neutral"), "positive": s.get("positive"),
                "negative_examples": []}
    pos = neg = 0
    neg_examples = []
    for t in danmaku_texts:
        if any(w in t for w in POS_WORDS):
            pos += 1
        if any(w in t for w in NEG_WORDS):
            neg += 1
            if len(neg_examples) < 5 and 2 <= len(t) <= 30:
                neg_examples.append(t.strip())
    total = max(1, len(danmaku_texts))
    return {"source": "规则推断（无画像）",
            "negative": round(neg / total, 3),
            "neutral": round(max(0, total - pos - neg) / total, 3),
            "positive": round(pos / total, 3),
            "negative_examples": neg_examples}


def analyze_consumption(danmaku_texts: List[str]) -> Dict[str, Any]:
    hits = [t.strip() for t in danmaku_texts if any(w in t for w in CONSUME_WORDS)]
    examples = []
    seen = set()
    for t in sorted(hits, key=lambda x: len(x)):
        if t in seen or not (2 <= len(t) <= 30):
            continue
        seen.add(t)
        examples.append(t)
        if len(examples) >= 5:
            break
    return {"count": len(hits), "examples": examples}


# ================================================================ 模块 ⑤ 运营动作时间线
def analyze_ops_timeline(events: List[Dict[str, Any]], danmaku: List[Dict[str, Any]],
                         session: Dict[str, Any]) -> Dict[str, Any]:
    """运营动作时间线：福袋 / 引导动作 / 冷场时段。（就业辅导场景**不含 PK/团战**。）"""
    # —— 福袋（优先结构化事件，缺失时用高能时刻里的弹幕推断兜底）
    lottery = []
    for e in events:
        if e.get("type") not in LOTTERY_EVENT_TYPES:
            continue
        prize = e.get("prize_name") or e.get("box_name") or e.get("reward_name") or "—"
        item = {
            "clock": ts2clock(e.get("ts")),
            "type": e.get("type"),
            "prize": prize,
            "source": "结构化事件(WSS)",
        }
        cond = e.get("conditions")
        if isinstance(cond, dict) and cond:
            item["conditions"] = cond
        for k_src, k_dst in (("participants", "participants"),
                             ("winner_count", "winner_count"),
                             ("winners", "winners"), ("candidate_num", "candidate_num")):
            if e.get(k_src) not in (None, "", []):
                item[k_dst] = e.get(k_src)
        lottery.append(item)
    lottery_source = "结构化事件(WSS)" if lottery else None
    if not lottery:
        for h in (session.get("highlights") or []):
            if "lottery_suspect" in str(h.get("kind") or ""):
                lottery.append({
                    "clock": ts2clock(h.get("ts")),
                    "type": "lottery_suspect",
                    "prize": "—",
                    "keyword_hits": h.get("keyword_hits"),
                    "top_repeated_text": h.get("top_repeated_text"),
                    "source": "弹幕推断（兜底）",
                })
        if lottery:
            lottery_source = "弹幕推断（兜底，非平台事件）"

    # —— 引导动作（主播话术侧，间接观察）
    guides: Dict[str, Dict[str, Any]] = {}
    for name, keys in GUIDE_GROUPS:
        buckets: collections.Counter = collections.Counter()
        for d in danmaku:
            text = d.get("text") or ""
            if any(k in text for k in keys):
                buckets[ts2min(d.get("ts"))] += 1
        total = sum(buckets.values())
        if total:
            peak_min, peak_n = buckets.most_common(1)[0]
            guides[name] = {"count": total, "peak_minute": peak_min, "peak_count": peak_n}

    # —— 冷场时段（弹幕密度低于基线的连续窗口，竞品弱项）
    cold = _detect_cold_windows(danmaku)

    return {
        "lottery": lottery,
        "lottery_source": lottery_source,
        "guides": guides,
        "cold_windows": cold,
        "pk_teamfight": "本场景（就业辅导）不适用，已关闭",
    }


def _detect_cold_windows(danmaku: List[Dict[str, Any]], bucket_s: int = 60,
                         ratio: float = 0.3, min_len_s: int = 120,
                         max_out: int = 5) -> List[Dict[str, Any]]:
    ts_list = sorted(int(d.get("ts")) for d in danmaku if d.get("ts"))
    if not ts_list:
        return []
    t0 = (ts_list[0] // bucket_s) * bucket_s
    buckets: collections.Counter = collections.Counter()
    for ts in ts_list:
        buckets[((ts - t0) // bucket_s) * bucket_s + t0] += 1
    if not buckets:
        return []
    counts = sorted(buckets.values())
    baseline = counts[len(counts) // 2]  # 中位数
    threshold = max(1.0, baseline * ratio)

    # 只考察场次覆盖的连续桶（无弹幕的桶 = 0 条，也要算进冷场）
    start, end = min(buckets), max(buckets)
    windows = []
    run_start = None
    b = start
    while b <= end:
        cold = buckets.get(b, 0) <= threshold
        if cold and run_start is None:
            run_start = b
        elif not cold and run_start is not None:
            if b - run_start >= min_len_s:
                windows.append((run_start, b))
            run_start = None
        b += bucket_s
    if run_start is not None and (end + bucket_s) - run_start >= min_len_s:
        windows.append((run_start, end + bucket_s))

    out = []
    for ws, we in windows:
        seg = [x for x in ts_list if ws <= x < we]
        out.append({
            "start": ts2clock(ws),
            "end": ts2clock(we - 1),
            "duration_min": round((we - ws) / 60, 1),
            "danmaku_count": len(seg),
        })
        if len(out) >= max_out:
            break
    return out


# ================================================================ 模块 ⑥ 观众画像
def analyze_audience(danmaku: List[Dict[str, Any]], top_speakers: int = 5) -> Dict[str, Any]:
    """活跃分层 + 高频发言观众 + 身份标签（推断不出**不硬编**）。"""
    per_user: Dict[str, int] = collections.Counter()
    uname_map: Dict[str, str] = {}
    texts = []
    fb_hits = 0
    for d in danmaku:
        if d.get("type") != "danmaku":
            continue
        key, uname, fb = resolve_speaker(d)
        text = d.get("text") or ""
        if key:
            per_user[key] += 1
            if uname:
                uname_map[key] = uname
            if fb:
                fb_hits += 1
        if text:
            texts.append(text)

    levels = {"high": [], "mid": [], "low": []}
    for key, n in per_user.items():
        if n >= 3:
            levels["high"].append(key)
        elif n == 2:
            levels["mid"].append(key)
        else:
            levels["low"].append(key)

    speakers = []
    for key, n in per_user.most_common(top_speakers):
        if n < 2:
            continue
        # 昵称兜底时给「(昵称)」标记，不给假 hash，避免读者当成平台 user_id
        speakers.append({"uid": "(昵称)" if key.startswith("n:") else mask_uid(key),
                         "uname": uname_map.get(key, ""), "danmaku": n})

    identities = []
    joined = "\n".join(texts)
    for name, keys in IDENTITY_RULES:
        c = sum(joined.count(k) for k in keys)
        if c:
            identities.append({"label": name, "hit": c})
    identities.sort(key=lambda x: x["hit"], reverse=True)

    return {
        "speaker_count": len(per_user),
        "danmaku_speakers": sum(1 for n in per_user.values()),
        "speaker_key_source": speaker_key_note(fb_hits > 0),
        "layers": {"high": len(levels["high"]), "mid": len(levels["mid"]),
                   "low": len(levels["low"])},
        "top_speakers": speakers,
        "identities": identities[:6],
        "identity_note": "" if identities else "未能从弹幕推断身份标签（保持原值，不硬编）",
    }


def analyze_concentration(danmaku: List[Dict[str, Any]],
                          suspect_share: float = 0.6,
                          suspect_total: int = 4) -> Dict[str, Any]:
    """互动集中度 / 疑似自己人撑场。

    判据（可调）：高集中 = 单场弹幕 >= suspect_total 且（发言人 <=2 或 头名占比 >= suspect_share）。
    典型：整场弹幕全出自 1 人 → 极高集中，判「疑似自己人撑场」。
    """
    per_user: collections.Counter = collections.Counter()
    uname_map: Dict[str, str] = {}
    fb_hits = 0
    for d in danmaku:
        if d.get("type") != "danmaku":
            continue
        key, uname, fb = resolve_speaker(d)
        if not key:
            continue
        per_user[key] += 1
        if uname:
            uname_map[key] = uname
        if fb:
            fb_hits += 1
    total = sum(per_user.values())
    speakers = len(per_user)
    used_fallback = fb_hits > 0
    if total == 0:
        return {"total_danmaku": 0, "speakers": 0, "top_share": None,
                "level": "—", "top_uname": "", "warning": "",
                "key_source": speaker_key_note(False)}
    top_key, top_n = per_user.most_common(1)[0]
    share = round(top_n / total, 3)
    if speakers == 1 or share >= 0.8:
        level = "极高"
    elif share >= suspect_share or speakers <= 2:
        level = "高"
    elif share >= 0.4:
        level = "中"
    else:
        level = "低"
    warning = ""
    if total >= suspect_total and (speakers <= 2 or share >= suspect_share):
        # 昵称兜底时，掩码昵称可能把不同人并成一人 → 结论必须带口径说明，别当成事实
        caveat = ("（注：平台未返回真实 user_id，本项按昵称统计；同名/同掩码可能被并成一人，"
                  "占比较真实值偏高，判读需谨慎）") if used_fallback else ""
        warning = (f"互动高度集中：{uname_map.get(top_key) or '(匿名)'} 一人占 {share * 100:.0f}%"
                   f"（{top_n}/{total} 条，全场仅 {speakers} 人发言）—— 疑似自己人/内部人撑场，"
                   f"非真实观众互动，评估人气时需打折。{caveat}")
    return {"total_danmaku": total, "speakers": speakers, "top_share": share,
            "level": level, "top_uname": uname_map.get(top_key, ""), "warning": warning,
            "key_source": speaker_key_note(used_fallback)}


# ================================================================ 模块 ⑦ LLM 自动结论（每场一次）
class LlmSummarizer:
    """每场一次聚合总结。client=None 时安全降级为「未启用」，不产生任何费用。

    真实 client 为 llm_service 的 LLMRouter（OpenAI 兼容），用量经 on_metrics 捕获。
    """

    SYSTEM = (
        "你是就业辅导机构的内容运营分析师。用户会给你一场竞品直播的结构化统计与"
        "观众的弹幕归类结果。请只输出 3 条以内的中文要点，每行一条，不要编号、不要"
        "解释、不要客套，覆盖：①今日最值得跟进的选题 ②观众痛点一句话 ③可借鉴的运营动作。"
        "信息不足时宁少勿编。"
    )

    def __init__(self, client: Any = None, price_in: Optional[float] = None,
                 price_out: Optional[float] = None, max_lines: int = 3) -> None:
        self.client = client
        # Legacy arguments are retained for callers, but cannot manufacture a
        # historical send-time price snapshot.
        self.max_lines = max_lines
        # 用量捕获：LLMClient 走「每次调用回调」，LLMRouter 只支持「构造时回调」
        # 两路都写进同一个 _records，由 summarize() 每次调用前清空
        self._records: List[Dict[str, Any]] = []

    def _on_metrics(self, record: Dict[str, Any]) -> None:
        if isinstance(record, dict):
            self._records.append(record)

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def summarize(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        base = {"enabled": self.enabled, "ok": False, "provider": "", "model": "",
                "prompt_tokens": 0, "completion_tokens": 0, "cost_yuan": 0.0 if not self.enabled else None,
                "usage": aggregate_records([]),
                "lines": [], "error": ""}
        if not self.enabled:
            base["error"] = "LLM 未启用（--llm 且 config 已配置 key 才启用）"
            return base
        user = json.dumps(payload, ensure_ascii=False)[:6000]
        self._records.clear()
        text = None
        try:
            # 普通 client（含测试替身）支持每次调用回调
            text = self.client.chat(self.SYSTEM, user, on_metrics=self._on_metrics,
                                    purpose="review")
        except TypeError:
            # LLMRouter 不接受 on_metrics —— 用量经构造时注入的 _on_metrics 捕获；
            # purpose="review" 让闸门可以「只关实时、留复盘」（2026-10-25）
            try:
                text = self.client.chat(self.SYSTEM, user, purpose="review")
            except Exception:  # noqa: BLE001
                base["error"] = "模型请求未完成，用量覆盖可能不完整"
        except Exception:  # noqa: BLE001
            base["error"] = "模型请求未完成，用量覆盖可能不完整"

        records = self._records
        usage = aggregate_records(records)
        usage["scope"]["kind"] = "report_section"
        usage["scope"]["coverage_basis"] = "report_local_metrics_only"
        base["usage"] = usage
        base["prompt_tokens"] = usage["prompt_tokens"]
        base["completion_tokens"] = usage["completion_tokens"]
        base["cost_yuan"] = usage["cost_cny"]
        if records:
            base["provider"] = records[-1].get("provider", "")
            base["model"] = records[-1].get("model", "")
            base["ok"] = bool(records[-1].get("success"))
        if text:
            base["ok"] = True
            base["lines"] = _parse_conclusion(text, self.max_lines)
        elif not base["error"]:
            base["error"] = "空响应"
        return base


_MD_EMPH_RE = re.compile(r"[*_~]+")
_MD_HEAD_RE = re.compile(r"^\s*#{1,6}\s*")
_INDEX_RE = re.compile(r"^\s*(?:[-*•+]|\d+\s*[.、:：)]|[①②③④⑤⑥⑦⑧⑨⑩])\s*")
# 中文之间残留的空白（多为模型插入的反引号/空格把词切断，如「不知` 道」→「不知道」）
_CJK_SPACE_RE = re.compile(r"(?<=[\u4e00-\u9fa5])\s+(?=[\u4e00-\u9fa5])")


def _parse_conclusion(text: str, max_lines: int) -> List[str]:
    """把 LLM 结论清洗成纯文本行：去反引号 / 强调符 / 标题 / 项目符号编号，
    并消掉中文之间被 markdown 切断的空白（「不知` 道」→「不知道」）。"""
    out = []
    for raw in re.split(r"[\n\r]+", text or ""):
        line = raw.replace("`", "")            # 反引号（行内代码）→ 直接删除
        line = _MD_HEAD_RE.sub("", line)        # 标题井号
        line = _INDEX_RE.sub("", line)          # 项目符号 / 序号
        line = _MD_EMPH_RE.sub("", line)        # **加粗** / *斜体* / ~~删除线~~
        line = re.sub(r"\s+", " ", line).strip()
        line = _CJK_SPACE_RE.sub("", line)      # 消掉中文之间残留空格
        if line:
            out.append(line)
        if len(out) >= max_lines:
            break
    return out


def build_llm_summarizer(enabled: bool = False,
                         config_path: Optional[str] = None) -> LlmSummarizer:
    """按需装配真实 LLM（enabled 且 config 有 provider key）；否则返回降级实例（零费用）。

    日报是独立人工消费者：命令行 --llm 仅表示请求模型分析，实际发送仍受
    canonical总许可/运行时暂停约束；主播自动实况开关不限制此人工用途。
    """
    if not enabled:
        return LlmSummarizer(client=None)
    try:
        from llm_service import load_llm_config, DEFAULT_CONFIG_PATH
        cfg = load_llm_config(config_path or DEFAULT_CONFIG_PATH)
        has_key = any(str((p or {}).get("api_key") or "").strip()
                      for p in cfg.providers.values())
        if not has_key:
            logger.warning("--llm 已开，但 config 未配置任何 provider key，自动结论降级（零费用）")
            return LlmSummarizer(client=None)
        s = LlmSummarizer(client=None)
        # 关键：router 的用量只在**构造时**回调，故在此注入 sink
        s.client = cfg.build_router(on_metrics=s._on_metrics)
        if s.client is None:
            logger.warning("LLM router 装配为空，自动结论降级")
        return s
    except Exception as e:  # noqa: BLE001
        logger.warning("装配 LLM 失败，自动结论降级：%s", e)
        return LlmSummarizer(client=None)


# ================================================================ 发现 + 装配
def _tag_from_filename(name: str, prefix: str, suffix: str) -> Optional[str]:
    if name.startswith(prefix) and name.endswith(suffix):
        return name[len(prefix):-len(suffix)]
    return None


def discover_bundles(data_dir: str, date: str, platform: str = "douyin",
                     tags: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """发现当天的所有主播场次。优先按 session_*.json，其次按 danmu 文件。"""
    found: Dict[str, Dict[str, Any]] = {}

    def _ensure(tag: str) -> Dict[str, Any]:
        return found.setdefault(tag, {"tag": tag})

    # 1) stats 文件是「一场必有」的锚点
    for fp in sorted(glob.glob(os.path.join(data_dir, f"stats_{platform}_*_{date}.jsonl"))):
        tag = _tag_from_filename(os.path.basename(fp), f"stats_{platform}_",
                                 f"_{date}.jsonl")
        if not tag:
            continue
        b = _ensure(tag)
        b["stats_path"] = fp

    # 2) 补齐其余文件
    for fp in sorted(glob.glob(os.path.join(data_dir, f"danmu_{platform}_*_{date}.jsonl"))):
        tag = _tag_from_filename(os.path.basename(fp), f"danmu_{platform}_",
                                 f"_{date}.jsonl")
        if not tag:
            continue
        _ensure(tag)["danmu_path"] = fp

    for tag, b in found.items():
        stats, danmu = discover_inputs(data_dir, platform, tag, date)
        b.setdefault("stats_path", stats)
        b.setdefault("danmu_path", danmu)
        cand = {
            "events_path": os.path.join(data_dir, f"events_{platform}_{tag}_{date}.jsonl"),
            "session_path": os.path.join(data_dir, f"session_{tag}_{date}.json"),
            "meta_path": os.path.join(data_dir, f"room_meta_{platform}_{tag}.json"),
        }
        for k, v in cand.items():
            b[k] = v if os.path.exists(v) else None

    if tags:
        found = {t: found[t] for t in tags if t in found}
    return [found[t] for t in sorted(found)]


def build_session_section(bundle: Dict[str, Any], llm: LlmSummarizer,
                          portrait: Optional[Dict[str, Any]] = None,
                          portrait_note: str = "") -> Dict[str, Any]:
    """把一个主播的当天数据装配成分场分析结果（纯数据，不含渲染）。

    portrait 为该房间当天的 LLM 画像（按 room_tag/room_id 归属）；有则话题与提问
    聚类**走画像**，无则规则兜底（并在报告里标注来源）。
    """
    tag = bundle["tag"]
    danmu_records = _load_jsonl(bundle.get("danmu_path"))
    events = _load_jsonl(bundle.get("events_path"))
    meta = _load_json(bundle.get("meta_path"))

    session = _load_json(bundle.get("session_path"))
    if session is None:
        # 无 session 文件时按统一口径现算
        if bundle.get("stats_path") and bundle.get("danmu_path"):
            session = compute_session(bundle["stats_path"], bundle["danmu_path"],
                                      room_tag=tag)
        else:
            session = {"session": {}, "highlights": [], "missing": True}

    danmaku = [d for d in danmu_records if d.get("type") == "danmaku"]
    danmaku_texts = [d.get("text") or "" for d in danmaku]

    topics = analyze_topics(danmaku_texts, portrait)
    row = build_row(session, events, meta, tag, topics)
    overview = build_overview(session, meta)
    overview["pv_delta"] = row["pv_delta"]
    conc = analyze_concentration(danmaku)
    row["concentration_level"] = conc["level"]
    row["concentration_warning"] = conc["warning"]

    section = {
        "tag": tag,
        "row": row,
        "overview": overview,
        "highlights": build_highlights(session),
        "topics": topics,
        "questions": analyze_questions(danmaku_texts, portrait),
        "sentiment": analyze_sentiment(danmaku_texts, portrait),
        "consumption": analyze_consumption(danmaku_texts),
        "ops": analyze_ops_timeline(events, danmaku, session),
        "audience": analyze_audience(danmaku),
        "concentration": conc,
        "portrait_source": portrait_note or ("画像(LLM)" if portrait else "无画像（话题/提问/情感走规则）"),
        "data_flags": _data_flags(bundle, danmu_records, events, session),
    }

    # 模块⑦：每场一次 LLM（信息不足时也给，最多 3 行；未启用则标注）
    llm_payload = {
        "主播": row["anchor_name"],
        "标题": row["title"],
        "场次": overview,
        "高频话题": topics,
        "提问聚类": section["questions"]["clusters"][:5],
        "情感": {k: section["sentiment"][k] for k in ("positive", "neutral", "negative")},
        "消费意愿条数": section["consumption"]["count"],
        "互动集中度": section["concentration"],
        "运营动作": {"福袋次数": row["lottery_count"],
                     "引导关注": section["ops"]["guides"].get("引导关注", {}).get("count", 0),
                     "冷场时段数": len(section["ops"]["cold_windows"])},
        "高能时刻": [{"时刻": h["clock"], "关键词": h["keywords"]} for h in section["highlights"]],
    }
    section["llm"] = llm.summarize(llm_payload)
    return section


def _data_flags(bundle: Dict[str, Any], danmu: List[Dict[str, Any]],
                events: List[Dict[str, Any]], session: Dict[str, Any]) -> Dict[str, Any]:
    """数据边界：区分「无数据（—）」与「确实为 0」。"""
    return {
        "has_danmu": bool(danmu),
        "has_events": bool(events),
        "has_session": not session.get("missing"),
        "has_meta": bool(bundle.get("meta_path")),
        "no_lottery": not any(e.get("type") in LOTTERY_EVENT_TYPES for e in events),
        "note": "GMV 拿不到（平台侧）；PK/团战本场景关闭",
    }


# ================================================================ 渲染
def render_overview_md(rows: List[Dict[str, Any]], date: str,
                       prev: Optional[Dict[str, Any]], llm_total: Dict[str, Any]) -> str:
    order = ["tag", "anchor_name", "duration_min", "online_peak", "pv_delta",
             "danmaku_total", "interaction_rate", "lottery_count", "top_topic"]
    header = ["主播", "时长(分)", "峰值在线", "本场PV增量", "弹幕量",
              "互动率", "福袋", "集中度", "最火话题"]
    lines = [f"# 竞品监控日报 · 总览（{date}）", ""]
    lines.append("> 口径：与场次统计一致；互动率 = （弹幕+点赞+礼物+关注+分享）÷ 本场PV增量。"
                 "「—」表示无数据，不等同于 0。涨跌为 vs 前日。")
    lines.append("")
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "---|" * len(header))

    prev_map = (prev or {}).get("rows", {}) if isinstance(prev, dict) else {}
    for r in rows:
        dur_min = round(r["duration_seconds"] / 60, 1) if r.get("duration_seconds") else None
        pr = prev_map.get(r["tag"], {})
        cmp_ = r.get("_cmp") or {}
        cells = [
            r["anchor_name"],
            _fmt_num(dur_min),
            _fmt_num(r["online_peak"]) + _arrow(r["online_peak"], pr.get("online_peak")),
            _fmt_num(r["pv_delta"]) + _arrow(r["pv_delta"], pr.get("pv_delta")),
            _fmt_num(r["danmaku_total"]) + _arrow(r["danmaku_total"], pr.get("danmaku_total")),
            (f"{r['interaction_rate']}%" if r["interaction_rate"] is not None else "—")
            + _arrow(cmp_.get("interaction_rate"), pr.get("interaction_rate")),
            _fmt_num(r["lottery_count"]),
            (str(r.get("concentration_level") or "—")
             + ("⚠️" if r.get("concentration_warning") else "")),
            r["top_topic"] or "—",
        ]
        lines.append("| " + " | ".join(str(c) for c in cells) + " |")

    lines.append("")
    if not rows:
        lines.append("**本日无任何竞品场次数据（无数据，非 0）。**")
    lines.append("**LLM 成本（本报告合计）**：" + usage_text(llm_total.get("usage")) + " "
                 f"（{llm_total.get('calls', 0)} 个指标记录，"
                 f"prompt {llm_total.get('prompt_tokens', 0)} / "
                 f"completion {llm_total.get('completion_tokens', 0)} tokens）")
    lines.append("")
    return "\n".join(lines)


def render_session_md(section: Dict[str, Any]) -> str:
    row = section["row"]
    ov = section["overview"]
    L: List[str] = []
    L.append(f"# 竞品分场 · {row['anchor_name']}（{ov['date']}）")
    L.append("")
    L.append(f"**直播标题**：{ov['title']}")
    L.append("")

    # ② 单场概览
    L.append("## ① 单场概览")
    tc = ov["type_counts"]
    L.append(f"- 时段：{ov['time_range']}（时长 {ov['duration_text']}）")
    L.append(f"- 峰值在线：{_fmt_num(ov['online_peak'])} 人（{ov['online_peak_clock']}）")
    L.append(f"- 累计观看 PV：{_fmt_num(ov['pv_first'])} → {_fmt_num(ov['pv_last'])}"
             f"（本场增量 {_fmt_num(ov['pv_delta'])}）")
    L.append(f"- 互动计数：弹幕 {_fmt_num(tc.get('danmaku'))} / 进场 {_fmt_num(tc.get('enter'))} "
             f"/ 点赞 {_fmt_num(tc.get('like'))} / 礼物 {_fmt_num(tc.get('gift'))}")
    L.append(f"- 关注 {_fmt_num(row['follow_total'])} / 分享 {_fmt_num(row['share_total'])}"
             f"｜互动率 {row['interaction_rate'] if row['interaction_rate'] is not None else '—'}"
             f"{'%' if row['interaction_rate'] is not None else ''}")
    L.append("")

    # ③ 高能时刻 TOP3
    L.append("## ② 高能时刻 TOP3")
    if section["highlights"]:
        for i, h in enumerate(section["highlights"], 1):
            desc = []
            if h.get("online_gain"):
                desc.append(f"人气 +{_fmt_num(h['online_gain'])}（至 {_fmt_num(h['online_peak'])}）")
            if h.get("rate_peak_per10s"):
                desc.append(f"弹幕 {_fmt_num(h['rate_peak_per10s'])} 条/10 秒")
            if h.get("keyword_hits"):
                desc.append(f"关键词命中 {_fmt_num(h['keyword_hits'])}")
            L.append(f"{i}. **{h['clock']}**（{h.get('kind')}）：{'；'.join(desc) or '—'}")
            if h["keywords"]:
                L.append(f"   - 窗口观众聚焦：{' / '.join(h['keywords'])}")
            if h["examples"]:
                L.append(f"   - 代表弹幕：{'；'.join('「%s」' % e for e in h['examples'])}")
    else:
        L.append("- 无（本场未检出人气跳升 / 弹幕爆发；无数据，非 0）")
    L.append("")

    # ④ 观众话题与高频提问
    L.append("## ③ 观众话题与高频提问")
    L.append(f"> 来源：{section['portrait_source']}；已过滤问候语/口头禅（老师好/上午好/对对对…）。")
    L.append("**高频话题 TOP**：" + ("、".join(section["topics"]) or "—"))
    q = section["questions"]
    L.append(f"\n**观众提问聚类**（来源：{q['source']}；共识别 "
             f"{_fmt_num(q['total_questions'])} 条问句）：")
    if q["clusters"]:
        for c in q["clusters"]:
            L.append(f"- **{c['category']}**（{c['count']} 条）："
                     + "；".join("「%s」" % e for e in c["examples"]))
    else:
        L.append("- 无（本场无观众提问，或弹幕量过少；无数据，非 0）")
    if q.get("unmatched"):
        pct = 100.0 * q["unmatched"] / max(1, q["total_questions"])
        L.append(f"- 未归类 {q['unmatched']} 条 / 共 {q['total_questions']} 条"
                 f"（占 {pct:.0f}%；去重后 {q.get('unmatched_unique', q['unmatched'])} 条，"
                 f"多为只说一次的个人情况咨询——共性主题已单列成类，此处不再归类，"
                 f"以免为单条个案硬造类别）")
    s = section["sentiment"]
    L.append(f"\n**情感分布**（来源：{s['source']}）："
             f"积极 {_fmt_num(s['positive'])} / 中性 {_fmt_num(s['neutral'])} "
             f"/ 消极 {_fmt_num(s['negative'])}")
    if s.get("negative_examples"):
        L.append("- 负面弹幕：" + "；".join("「%s」" % e for e in s["negative_examples"]))
    cons = section["consumption"]
    L.append(f"\n**消费意愿信号**：{_fmt_num(cons['count'])} 条"
             + (f"｜{'；'.join('「%s」' % e for e in cons['examples'])}" if cons["examples"] else ""))
    L.append("")

    # ⑤ 运营动作时间线
    L.append("## ④ 运营动作时间线")
    ops = section["ops"]
    if ops["lottery"]:
        L.append(f"- **福袋/抽奖**（来源：{ops['lottery_source']}）")
        for it in ops["lottery"]:
            extra = ""
            if it.get("participants") is not None:
                extra += f"，参与 {_fmt_num(it['participants'])}"
            if it.get("winner_count") is not None:
                extra += f"，中奖 {_fmt_num(it['winner_count'])} 人"
            if it.get("top_repeated_text"):
                extra += f"，口令「{it['top_repeated_text']}」"
            L.append(f"  - {it['clock']}｜{it.get('prize', '—')}{extra}")
    else:
        L.append("- **福袋/抽奖**：无（当次未采集到福袋事件，也未见弹幕口令；无数据，非 0）")
    if ops["guides"]:
        L.append("- **引导动作**：")
        for name, g in ops["guides"].items():
            L.append(f"  - {name}：{_fmt_num(g['count'])} 次（峰值 {g['peak_minute']}，"
                     f"{_fmt_num(g['peak_count'])} 次）")
    else:
        L.append("- **引导动作**：无（未观察到关注/加群/领资料类话术；无数据，非 0）")
    if ops["cold_windows"]:
        L.append("- **冷场时段（竞品弱项）**：")
        for w in ops["cold_windows"]:
            L.append(f"  - {w['start']} - {w['end']}（{w['duration_min']} 分，"
                     f"弹幕 {_fmt_num(w['danmaku_count'])} 条）")
    else:
        L.append("- **冷场时段**：无")
    L.append(f"- **PK/团战**：{ops['pk_teamfight']}")
    L.append("")

    # ⑥ 观众画像
    L.append("## ⑤ 观众画像")
    a = section["audience"]
    L.append(f"- 发言观众数：{_fmt_num(a['speaker_count'])}｜活跃分层："
             f"高 {_fmt_num(a['layers']['high'])} / 中 {_fmt_num(a['layers']['mid'])} "
             f"/ 低 {_fmt_num(a['layers']['low'])}"
             f"（说话人口径：{a.get('speaker_key_source', '平台 user_id')}）")
    if a["top_speakers"]:
        def _sp_txt(sp):
            # uid 为 "(昵称)" 表示昵称兜底口径，别写成 "小***((昵称))"
            tail = f" {sp['danmaku']} 条" if sp["uid"] == "(昵称)" \
                else f"({sp['uid']}) {sp['danmaku']} 条"
            return f"{sp['uname'] or '(匿名)'}{tail}"
        L.append("- 高频发言观众：" + "；".join(_sp_txt(sp) for sp in a["top_speakers"]))
    if a["identities"]:
        L.append("- 身份标签（弹幕推断）：" + "、".join(
            f"{i['label']}×{i['hit']}" for i in a["identities"]))
    else:
        L.append(f"- 身份标签：{a['identity_note']}")
    # 互动集中度 / 疑似自己人撑场
    c = section["concentration"]
    if c["speakers"]:
        share_txt = "—" if c["top_share"] is None else f"{c['top_share'] * 100:.0f}%"
        L.append(f"- 互动集中度：**{c['level']}**（头名 {c['top_uname'] or '(匿名)'} "
                 f"占 {share_txt}，全场 {c['speakers']} 人发言 / {c['total_danmaku']} 条弹幕）")
    if c["warning"]:
        L.append(f"- ⚠️ **{c['warning']}**")
    L.append("")

    # ⑦ 自动结论
    L.append("## ⑥ 自动结论（LLM，每场一次）")
    llm = section["llm"]
    if llm["ok"] and llm["lines"]:
        for line in llm["lines"]:
            L.append(f"- {line}")
    else:
        L.append(f"- （未生成：{llm.get('error') or '无输出'}）")
    L.append("- 本场 LLM 估算：" + (usage_text(llm.get("usage")) if llm["enabled"] else "未调用模型")
             + f"（{llm.get('provider') or '—'}/{llm.get('model') or '—'}，"
             f"prompt {llm['prompt_tokens']} / completion {llm['completion_tokens']} tokens）")
    L.append("")

    # 数据边界
    flags = section["data_flags"]
    L.append("## 数据边界")
    miss = []
    if not flags["has_meta"]:
        miss.append("房间元数据")
    if not flags["has_session"]:
        miss.append("session 预计算结果（已现算）")
    if not flags["has_danmu"]:
        miss.append("弹幕文件")
    if not flags["has_events"]:
        miss.append("运营事件文件")
    L.append("- 缺失：" + ("、".join(miss) if miss else "无"))
    L.append(f"- 说明：{flags['note']}；「—」= 无数据，不等同于 0。")
    L.append("")
    return "\n".join(L)


def render_combined_md(date: str, rows: List[Dict[str, Any]],
                       sections: List[Dict[str, Any]],
                       prev: Optional[Dict[str, Any]],
                       llm_total: Dict[str, Any]) -> str:
    parts = [render_overview_md(rows, date, prev, llm_total)]
    parts.append("\n---\n")
    parts.append(f"# 分场明细（共 {len(sections)} 个主播）\n")
    for sec in sections:
        parts.append(render_session_md(sec))
        parts.append("\n---\n")
    return "\n".join(parts)


# ================================================================ 落盘
def _safe_tag(tag: str) -> str:
    return re.sub(r"[\\/:*?\"<>|]", "_", tag)


def write_reports(data_dir: str, date: str, sections: List[Dict[str, Any]],
                  rows: List[Dict[str, Any]], prev: Optional[Dict[str, Any]],
                  out_dir: Optional[str] = None) -> Dict[str, str]:
    out_dir = out_dir or os.path.join(data_dir, f"reports_{date}")
    os.makedirs(out_dir, exist_ok=True)

    report_usage = merge_usage_summaries(s["llm"]["usage"] for s in sections if s["llm"]["enabled"])
    llm_total = {
        "calls": report_usage["calls"],
        "prompt_tokens": sum(s["llm"]["prompt_tokens"] for s in sections),
        "completion_tokens": sum(s["llm"]["completion_tokens"] for s in sections),
        "cost_yuan": report_usage["cost_cny"] if any(s["llm"]["enabled"] for s in sections) else 0.0,
        "usage": report_usage,
    }

    combined = render_combined_md(date, rows, sections, prev, llm_total)
    combined_path = os.path.join(out_dir, f"竞品监控日报_{date}.md")
    with open(combined_path, "w", encoding="utf-8") as f:
        f.write(combined)

    per_paths = []
    for sec in sections:
        p = os.path.join(out_dir, f"{_safe_tag(sec['tag'])}_分场_{date}.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write(render_session_md(sec))
        per_paths.append(p)

    rows_idx: Dict[str, Dict[str, Any]] = {}
    for s in sections:
        r = s["row"]
        rows_idx[r["tag"]] = {
            **r.get("_cmp", {}),
            "anchor_name": r.get("anchor_name"),
            "top_topic": r.get("top_topic"),
            "interaction_rate": r.get("interaction_rate"),
            "lottery_count": r.get("lottery_count"),
            "concentration_level": r.get("concentration_level"),
            "concentration_warning": bool(r.get("concentration_warning")),
            "portrait_source": s.get("portrait_source"),
            "questions_source": (s.get("questions") or {}).get("source"),
        }
    index = {
        "date": date,
        "generated_at": int(time.time()),
        "llm_total": llm_total,
        "rows": rows_idx,
        "anchors": [{"tag": r["tag"], "anchor_name": r["anchor_name"]} for r in rows],
    }
    index_path = os.path.join(out_dir, f"report_index_{date}.json")
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    return {"combined": combined_path, "index": index_path, "per": per_paths}


def _load_prev_index(data_dir: str, date: str, out_dir: Optional[str] = None
                     ) -> Optional[Dict[str, Any]]:
    """读前一天的 report_index（用于涨跌对比）；无则返回 None。"""
    try:
        d = datetime.strptime(date, "%Y-%m-%d")
        prev_date = (d.timestamp() - 86400)
        prev_date = datetime.fromtimestamp(prev_date).strftime("%Y-%m-%d")
    except Exception:
        return None
    candidates = [
        os.path.join(out_dir or os.path.join(data_dir, f"reports_{prev_date}"),
                     f"report_index_{prev_date}.json"),
        os.path.join(data_dir, f"reports_{prev_date}", f"report_index_{prev_date}.json"),
    ]
    for c in candidates:
        idx = _load_json(c)
        if idx:
            return idx
    return None


def _latest_portrait_in(path: str, tag: Optional[str] = None,
                        room_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """取一个 portrait jsonl 里匹配 tag/room_id 的最后一条画像 dict。"""
    latest: Optional[Dict[str, Any]] = None
    for r in _load_jsonl(path):
        if not isinstance(r, dict):
            continue
        if tag and r.get("room_tag") and str(r["room_tag"]) != str(tag):
            continue
        if room_id and r.get("room_id") and str(r["room_id"]) != str(room_id):
            continue
        p = r.get("portrait")
        if isinstance(p, dict):
            latest = p
        elif "interest_topics" in r:          # 兼容裸画像记录
            latest = r
    return latest


def load_portrait_for(bundle: Dict[str, Any], date: str, platform: str = "douyin",
                      portrait_dir: Optional[str] = None,
                      search_dirs: Optional[List[str]] = None
                      ) -> Tuple[Optional[Dict[str, Any]], str]:
    """按 room_tag / room_id 找到该房间当天的 LLM 画像。返回 (portrait|None, note)。

    匹配顺序：① `portrait_<tag>_<date>.jsonl`（按房间独立落盘）；
    ② 任意 `portrait_*<date>*.jsonl` 内按 `room_tag` 或 `room_id`(=web_rid) 过滤。
    """
    tag = bundle["tag"]
    meta = _load_json(bundle.get("meta_path"))
    room_id = (meta or {}).get("web_rid")
    dirs: List[str] = []
    for d in [portrait_dir] + list(search_dirs or []):
        if d and d not in dirs:
            dirs.append(d)

    for d in dirs:                                   # ① 按房间独立文件
        p = os.path.join(d, f"portrait_{tag}_{date}.jsonl")
        if os.path.exists(p):
            pop = _latest_portrait_in(p)
            if pop:
                return pop, f"画像(按房间) {os.path.basename(p)}"
    for d in dirs:                                   # ② 文件内按 tag / room_id 过滤
        for p in sorted(glob.glob(os.path.join(d, f"portrait_*{date}*.jsonl"))):
            pop = _latest_portrait_in(p, tag=tag) or (
                _latest_portrait_in(p, room_id=room_id) if room_id else None)
            if pop:
                return pop, f"画像 {os.path.basename(p)}"
    return None, "无（当天无该房间画像 → 话题/提问/情感走规则兜底）"


# ================================================================ CLI
def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(levelname)s - %(message)s")
    ap = argparse.ArgumentParser(description="竞品监控日报（离线生成）")
    ap.add_argument("--dir", default="data", help="落盘目录（默认 data）")
    ap.add_argument("--date", default=time.strftime("%Y-%m-%d"))
    ap.add_argument("--platform", default="douyin")
    ap.add_argument("--tag", action="append", default=None,
                    help="只生成指定 tag（可重复）；缺省=当天全部")
    ap.add_argument("--out-dir", default=None, help="输出目录（默认 <dir>/reports_<date>）")
    ap.add_argument("--portrait-dir", default=None,
                    help="画像目录（默认与 --dir 同；离线 mock 时指向 mock 目录）")
    ap.add_argument("--llm", action="store_true",
                    help="启用 LLM 自动结论（每场一次，真实计费）")
    ap.add_argument("--config", default=None, help="LLM 配置（config.json 路径）")
    args = ap.parse_args(argv)

    bundles = discover_bundles(args.dir, args.date, args.platform, args.tag)
    if not bundles:
        logger.error("目录 %s 下找不到 %s 在 %s 的任何场次数据", args.dir, args.platform, args.date)
        # 仍产出一份「无数据」报告，避免调用方报错
        out = write_reports(args.dir, args.date, [], [], _load_prev_index(args.dir, args.date), args.out_dir)
        logger.info("已生成空报告：%s", out["combined"])
        return 0

    llm = build_llm_summarizer(args.llm, args.config)

    sections = []
    for b in bundles:
        try:
            portrait, pnote = load_portrait_for(
                b, args.date, args.platform, args.portrait_dir, [args.dir])
            logger.info("[%s] %s", b.get("tag"), pnote)
            sections.append(build_session_section(b, llm, portrait, pnote))
        except Exception as e:  # noqa: BLE001
            logger.exception("装配 %s 分场失败：%s", b.get("tag"), e)
    sections.sort(key=lambda s: (s["row"]["online_peak"] or 0), reverse=True)
    rows = [s["row"] for s in sections]

    prev = _load_prev_index(args.dir, args.date, args.out_dir)
    out = write_reports(args.dir, args.date, sections, rows, prev, args.out_dir)
    logger.info("已生成总览：%s", out["combined"])
    logger.info("分场 %d 份：%s", len(out["per"]), out["index"])
    logger.info("LLM：%s", "启用（每场一次）" if llm.enabled else "未启用（零费用）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
