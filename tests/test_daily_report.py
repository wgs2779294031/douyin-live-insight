# -*- coding: utf-8 -*-
"""v3.6 竞品监控日报测试（全离线，零 LLM 调用）。"""

import json
import os

import pytest

import daily_report as dr


# ---------------------------------------------------------------- helpers
def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _danmu(ts, text, typ="danmaku", uid="u1", uname="小明"):
    return {"ts": ts, "type": typ, "user_id": uid, "uname": uname,
            "text": text, "platform": "douyin", "room_id": "r1", "extra": {}}


def _make_dataset(data_dir, tag="教育-测试", date="2026-09-30", base=1790000000):
    os.makedirs(data_dir, exist_ok=True)
    # 弹幕：b0..b2 各 1 条，b3 爆发 12 条
    rows = []
    for i, t in enumerate([1, 2, 3]):
        rows.append(_danmu(base + i * 10, f"普通弹幕{i}"))
    for i in range(12):
        rows.append(_danmu(base + 30, "这个专业能考公吗？" if i % 2 else "老师好厉害",
                           uid=f"u{i}", uname=f"观众{i}"))
    # 进场 / 点赞 / 礼物
    rows.append(_danmu(base + 5, "进入了直播间", typ="enter"))
    rows.append(_danmu(base + 6, "点了5个赞", typ="like"))
    rows.append(_danmu(base + 7, "送出了礼物", typ="gift"))
    # 消费意愿 + 负面
    rows.append(_danmu(base + 40, "课程多少钱？"))
    rows.append(_danmu(base + 41, "太贵了退费"))
    # 冷场：m1..m3 无弹幕（下面补一条 m4）
    rows.append(_danmu(base + 240, "又开始了"))
    _write_jsonl(os.path.join(data_dir, f"danmu_douyin_{tag}_{date}.jsonl"), rows)

    # 人气快照
    stats = [
        {"ts": base, "type": "room_stats", "room_id": "r1", "online": 100, "platform": "douyin"},
        {"ts": base + 10, "type": "room_stats", "room_id": "r1", "online": 200, "platform": "douyin"},
        {"ts": base + 10, "type": "room_user_seq", "room_id": "r1", "online": 200,
         "total_pv": 1000, "popularity": 0, "ranks": [], "platform": "douyin"},
        {"ts": base + 240, "type": "room_user_seq", "room_id": "r1", "online": 210,
         "total_pv": 1400, "popularity": 0, "ranks": [], "platform": "douyin"},
    ]
    _write_jsonl(os.path.join(data_dir, f"stats_douyin_{tag}_{date}.jsonl"), stats)

    # 运营事件
    events = [
        {"ts": base + 20, "type": "social", "room_id": "r1", "user_id": "u9",
         "uname": "路人", "action": 1, "share_type": 0, "follow_count": 100, "platform": "douyin"},
        {"ts": base + 21, "type": "social", "room_id": "r1", "user_id": "u9",
         "uname": "路人", "action": 3, "share_type": 1, "follow_count": 0, "platform": "douyin"},
    ]
    _write_jsonl(os.path.join(data_dir, f"events_douyin_{tag}_{date}.jsonl"), events)

    with open(os.path.join(data_dir, f"room_meta_douyin_{tag}.json"), "w", encoding="utf-8") as f:
        json.dump({"anchor_name": "测试主播", "title": "测试标题", "room_tag": tag}, f,
                  ensure_ascii=False)
    return tag, date, base


# ---------------------------------------------------------------- 工具函数
def test_fmt_num_distinguishes_none_and_zero():
    assert dr._fmt_num(None) == "—"
    assert dr._fmt_num(0) == "0"
    assert dr._fmt_num(1234) == "1,234"


def test_arrow_basic():
    assert dr._arrow(120, 100).startswith("（↑")
    assert dr._arrow(80, 100).startswith("（↓")
    assert dr._arrow(100, 100) == "（持平）"
    assert dr._arrow(None, 100) == ""
    assert dr._arrow(5, 0) == ""


# ---------------------------------------------------------------- 模块④ 提问/话题/情感/消费
def test_questions_clustering():
    texts = ["这个专业能考公吗？", "考公年龄限制多少", "学费多少钱", "今天天气不错"]
    res = dr.analyze_questions(texts)
    cats = {c["category"]: c["count"] for c in res["clusters"]}
    assert res["total_questions"] == 3
    # 打分归类：命中多者优先，同分按类别表先后
    assert cats.get("考公考编") == 2
    assert cats.get("价格购买") == 1
    assert "今天天气不错" not in [e for c in res["clusters"] for e in c["examples"]]


def test_questions_theme_split_no_catchall():
    """主题细分：简历/面试/实习/校招/转行/年龄学历焦虑各自成类，且不落「其他」。"""
    texts = ["简历怎么写", "面试要注意什么", "怎么找实习", "校招什么时候开始",
             "30 岁转行来得及吗", "双非学历会被卡吗"]
    res = dr.analyze_questions(texts)
    cats = {c["category"] for c in res["clusters"]}
    assert res["unmatched"] == 0
    assert "其他" not in cats
    for want in ("简历修改", "面试技巧", "实习经验", "校招社招", "转行跨行", "年龄学历焦虑"):
        assert want in cats, f"缺主题 {want}"


def test_question_excludes_pure_punctuation_and_interjection():
    """纯标点 / 单字语气词不是提问——否则它们会灌满「未归类」桶。"""
    for junk in ("？", "？？？", "? ", "啊？", "嗯？", "[流泪]", "。。。"):
        assert dr._is_question(junk) is False, f"{junk!r} 不该判为提问"
    for real in ("灯牌就能上麦了吗？", "怎么找实习", "深圳大学怎么样"):
        assert dr._is_question(real) is True, f"{real!r} 应判为提问"


def test_question_excludes_ba_tail_statement():
    """`吧` 尾多为建议/推测（「15左右吧」），不作疑问标记；吗/呢/？不受影响。"""
    for stmt in ("15左右吧", "直接工作吧", "985吧", "继承家业吧"):
        assert dr._is_question(stmt) is False, f"{stmt!r} 是陈述/建议，不该判为提问"
    for real in ("来得及吗", "联合利华呢", "国庆不放假吗"):
        assert dr._is_question(real) is True, f"{real!r} 应判为提问"


def test_question_new_categories_lianmai_and_employer():
    """新增两类：连麦福利（灯牌/上麦/连线）与企业岗位（公司/事务所/岗位）。"""
    texts = ["灯牌十级了怎么领求职大礼包", "灯牌就能上麦了吗？", "28届能连麦吗",
             "图拉斯这家公司怎么样啊", "不能去四大会计师事务所吗", "这个岗位适合长期发展吗"]
    res = dr.analyze_questions(texts)
    cats = {c["category"] for c in res["clusters"]}
    assert "连麦福利" in cats and "企业与岗位" in cats
    assert res["unmatched"] == 0


def test_question_category_live_chitchat():
    """直播间闲聊（问作息/问主播）单列，不落「其他」；不抢走「岗位」类。"""
    texts = ["徐哥今天下播这么晚吗", "国庆这几天还会直播吗", "国庆不放假吗",
             "你俩加班有加班费吗", "土豆美女是佛山人？",
             "这个岗位适合长期发展吗"]
    res = dr.analyze_questions(texts)
    cats = {c["category"]: c["count"] for c in res["clusters"]}
    assert cats.get("直播闲聊") == 5
    assert cats.get("企业与岗位") == 1


def test_question_examples_prefer_most_frequent():
    """例证取最高频那句，而不是最短那句。"""
    texts = ["随便问题呢"] + ["简历怎么写"] * 5
    res = dr.analyze_questions(texts)
    top = res["clusters"][0]
    assert top["category"] == "简历修改"
    assert top["examples"][0] == "简历怎么写"


def test_question_unmatched_reports_unique_count():
    """未归类要同时给出「总条数」与「去重条数」，便于判断是否长尾。"""
    texts = ["随便说一句呢"] * 3 + ["胡扯一下呢"]
    res = dr.analyze_questions(texts)
    assert res["unmatched"] == 4
    assert res["unmatched_unique"] == 2


def test_topics_merges_portrait_first():
    texts = ["考研 考研 考研", "就业 就业"]
    portrait = {"interest_topics": ["就业前景", "考研规划"]}
    topics = dr.analyze_topics(texts, portrait, n=6)
    assert topics[0] == "就业前景" and topics[1] == "考研规划"
    assert "考研" in topics


def test_sentiment_rule_fallback_and_portrait():
    rule = dr.analyze_sentiment(["谢谢老师", "太坑了退费"], portrait=None)
    assert rule["source"].startswith("规则")
    assert rule["negative"] > 0 and rule["positive"] > 0
    port = dr.analyze_sentiment([], portrait={"sentiment": {"positive": 0.5, "neutral": 0.3, "negative": 0.2}})
    assert port["source"] == "画像(LLM)" and port["positive"] == 0.5


def test_consumption_signal():
    res = dr.analyze_consumption(["课程多少钱", "有没有资料", "老师讲得好"])
    assert res["count"] == 2
    assert any("多少钱" in e for e in res["examples"])


# ---------------------------------------------------------------- 模块③ 高能时刻
def test_highlights_sorted_and_clustered():
    session = {"highlights": [
        {"ts": 1000, "kind": "danmaku_burst", "rate_peak_per10s": 5,
         "window_danmaku_count": 3, "window_texts": ["就业怎么样", "考公难吗"]},
        {"ts": 2000, "kind": "both", "rate_peak_per10s": 30, "online_gain": 100,
         "window_danmaku_count": 20, "window_texts": ["考研调剂", "考研复试", "考研国家线"]},
    ]}
    out = dr.build_highlights(session, top_n=2)
    assert out[0]["rate_peak_per10s"] == 30
    # 无分词时整段短语作为一个词元，属预期
    assert any("考研" in k for k in out[0]["keywords"])


# ---------------------------------------------------------------- 模块⑤ 运营动作
def test_ops_timeline_lottery_and_guides_and_cold():
    base = 1790000000
    events = [{"ts": base + 30, "type": "luckybox", "box_name": "钻石红包",
               "participants": 20}]
    danmaku = [
        _danmu(base, "关注主播"), _danmu(base + 1, "加群领资料"),
        _danmu(base + 60, "x"), _danmu(base + 240, "y"),
    ]
    session = {"highlights": []}
    ops = dr.analyze_ops_timeline(events, danmaku, session)
    assert ops["lottery"] and ops["lottery"][0]["source"].startswith("结构化")
    assert "引导关注" in ops["guides"] and "引导加群" in ops["guides"]
    assert ops["pk_teamfight"].startswith("本场景")


def test_ops_lottery_suspect_fallback():
    session = {"highlights": [{"ts": 100, "kind": "lottery_suspect",
                               "keyword_hits": 6, "top_repeated_text": "666"}]}
    ops = dr.analyze_ops_timeline([], [], session)
    assert ops["lottery"][0]["type"] == "lottery_suspect"
    assert "推断" in ops["lottery_source"]


def test_cold_windows_detected():
    base = 1790000000
    danmaku = [_danmu(base + i, f"a{i}") for i in range(10)]          # 第 0 分钟
    danmaku += [_danmu(base + 240 + i, f"b{i}") for i in range(10)]   # 第 4 分钟
    windows = dr._detect_cold_windows(danmaku)
    assert windows, "应检出中间 3 分钟冷场"
    assert windows[0]["duration_min"] >= 2.0


# ---------------------------------------------------------------- 模块⑥ 观众画像
def test_analyze_audience_layers_and_identities():
    danmaku = [
        _danmu(1, "我是考研的", uid="a", uname="甲"),
        _danmu(2, "考研二战", uid="a", uname="甲"),
        _danmu(3, "继续", uid="a", uname="甲"),
        _danmu(4, "在职考公", uid="b", uname="乙"),
    ]
    res = dr.analyze_audience(danmaku)
    assert res["layers"]["high"] == 1
    assert res["top_speakers"][0]["uname"] == "甲" and res["top_speakers"][0]["danmaku"] == 3
    assert res["top_speakers"][0]["uid"] != "a"  # 已脱敏
    labels = [i["label"] for i in res["identities"]]
    assert "考研" in labels


def test_analyze_audience_no_identity_note():
    res = dr.analyze_audience([_danmu(1, "你好")])
    assert res["identities"] == []
    assert "不硬编" in res["identity_note"]


# ---------------------------------------------------------------- 模块① 行
def test_build_row_interaction_rate_and_pv():
    session = {"session": {"duration_seconds": 300, "online_peak": 200, "online_peak_ts": 5,
                           "total_pv_first": 1000, "total_pv_last": 1500,
                           "type_counts": {"danmaku": 50, "like": 20, "gift": 10, "enter": 80},
                           "start_ts": 1790000000, "end_ts": 1790000300}}
    events = [{"type": "social", "action": 1}, {"type": "social", "action": 3}]
    row = dr.build_row(session, events, {"anchor_name": "A", "title": "T"}, "tag1", ["就业"])
    assert row["pv_delta"] == 500
    assert row["follow_total"] == 1 and row["share_total"] == 1
    assert row["interactions"] == 50 + 20 + 10 + 1 + 1
    assert row["interaction_rate"] == round(row["interactions"] / 500 * 100, 2)
    assert row["top_topic"] == "就业"


# ---------------------------------------------------------------- 模块⑦ LLM
def test_llm_disabled_zero_cost():
    s = dr.LlmSummarizer(client=None)
    out = s.summarize({"x": 1})
    assert out["enabled"] is False and out["cost_yuan"] == 0.0 and not out["ok"]


class _FakeClient:
    def chat(self, system, user, on_metrics=None, **kw):
        if on_metrics:
            on_metrics({"provider": "fake", "model": "fake-1", "success": True,
                        "prompt_tokens": 100, "completion_tokens": 20, "error_type": ""})
        return "1. 跟进「考公年龄」选题\n2. 观众担心 30 岁转行来不及\n3. 学他做福袋口令引导\n"


def test_llm_legacy_usage_has_no_estimate_without_snapshot():
    s = dr.LlmSummarizer(client=_FakeClient())
    out = s.summarize({"x": 1})
    assert out["enabled"] and out["ok"]
    assert out["cost_yuan"] is None
    assert out["usage"]["coverage"]["legacy_records"] == 1
    assert out["usage"]["estimates"] == []
    assert len(out["lines"]) == 3
    assert "考公年龄" in out["lines"][0]


class _RouterLikeClient:
    """模拟 LLMRouter：chat() **不接受** on_metrics，用量只走构造时注入的 sink。"""

    def __init__(self, emit):
        self.emit = emit

    def chat(self, system, user, purpose=""):  # 故意不接收 on_metrics
        self.emit({"provider": "fake-router", "model": "fake-r",
                   "success": True, "prompt_tokens": 100,
                   "completion_tokens": 20, "error_type": ""})
        return "结论A\n结论B"


def test_llm_router_style_cost_recorded():
    """覆盖真实 LLMRouter 路径：per-call on_metrics 触发 TypeError → 走构造时 sink 记费。"""
    s = dr.LlmSummarizer(client=None)
    s.client = _RouterLikeClient(s._on_metrics)
    out = s.summarize({"x": 1})
    assert out["ok"] and out["provider"] == "fake-router"
    assert out["cost_yuan"] is None
    assert out["usage"]["coverage"]["legacy_records"] == 1
    assert out["lines"] == ["结论A", "结论B"]


# ---------------------------------------------------------------- 发现 + 端到端
def test_discover_bundles(tmp_path):
    tag, date, _ = _make_dataset(str(tmp_path))
    bundles = dr.discover_bundles(str(tmp_path), date)
    assert [b["tag"] for b in bundles] == [tag]
    b = bundles[0]
    assert b["stats_path"] and b["danmu_path"] and b["meta_path"]
    assert b["events_path"]


def test_end_to_end_reports_written(tmp_path):
    tag, date, _ = _make_dataset(str(tmp_path))
    bundles = dr.discover_bundles(str(tmp_path), date)
    llm = dr.LlmSummarizer(client=None)
    sections = [dr.build_session_section(b, llm) for b in bundles]
    rows = [s["row"] for s in sections]
    out = dr.write_reports(str(tmp_path), date, sections, rows, None)

    assert os.path.exists(out["combined"])
    assert len(out["per"]) == 1 and os.path.exists(out["per"][0])
    text = open(out["combined"], encoding="utf-8").read()
    # 关键模块都在
    for kw in ["总览", "单场概览", "高能时刻", "观众话题与高频提问", "运营动作时间线",
               "观众画像", "自动结论"]:
        assert kw in text
    assert "LLM 成本" in text and "未调用模型" in text

    idx = json.load(open(out["index"], encoding="utf-8"))
    assert idx["date"] == date and tag in idx["rows"]
    assert idx["llm_total"]["cost_yuan"] == 0.0


def test_missing_lottery_shows_none_not_zero(tmp_path):
    tag, date, _ = _make_dataset(str(tmp_path))
    section = dr.build_session_section(
        dr.discover_bundles(str(tmp_path), date)[0], dr.LlmSummarizer(client=None))
    md = dr.render_session_md(section)
    assert "福袋/抽奖**：无" in md
    assert "PK/团战**：本场景" in md


def test_no_data_still_writes_report(tmp_path):
    out = dr.write_reports(str(tmp_path), "2026-01-01", [], [], None)
    text = open(out["combined"], encoding="utf-8").read()
    assert "本日无任何竞品场次数据" in text


def test_recompute_when_session_file_absent(tmp_path):
    """无 session_*.json 时按统一口径现算，不应报错。"""
    tag, date, _ = _make_dataset(str(tmp_path))
    bundles = dr.discover_bundles(str(tmp_path), date)
    assert bundles[0]["session_path"] is None
    section = dr.build_session_section(bundles[0], dr.LlmSummarizer(client=None))
    assert section["overview"]["duration_text"] != "—"
    assert section["row"]["danmaku_total"] >= 14


def test_topics_filters_emoji_and_digits():
    topics = dr.analyze_topics(["听哭了[流泪]", "青青7799 来了", "就业前景怎么样"], n=5)
    assert "流泪" not in topics
    assert "7799" not in topics


def test_norm_counts_defaults_zero():
    c = dr._norm_counts({"danmaku": 5})
    assert c["danmaku"] == 5 and c["gift"] == 0 and c["enter"] == 0


def test_overview_gift_zero_not_dash():
    """核心事件计数缺失应为 0（非「—」），避免把真 0 显示成无数据。"""
    session = {"session": {"type_counts": {"danmaku": 5, "enter": 2, "like": 1},
                           "start_ts": 1790000000, "end_ts": 1790000300}}
    ov = dr.build_overview(session, None)
    assert ov["type_counts"]["gift"] == 0
    assert dr._fmt_num(ov["type_counts"]["gift"]) == "0"


# ---------------------------------------------------------------- v3.6.1 质量修复
def test_topics_filter_greetings_and_pet_phrases():
    texts = ["老师好", "上午好", "对对对", "就业前景怎么样", "就业前景怎么样"]
    topics = dr.analyze_topics(texts, n=5)
    assert "老师好" not in topics and "上午好" not in topics and "对对对" not in topics
    assert any("就业前景" in t for t in topics)


def test_questions_from_portrait_path():
    """有画像 question_topics 时改走画像，不再走规则。"""
    portrait = {"question_topics": [
        {"topic": "简历", "count": 2, "examples": ["简历怎么写"]},
        {"topic": "转行", "count": 1, "examples": ["30岁转行来得及吗"]},
    ]}
    res = dr.analyze_questions(["随便一句"], portrait=portrait)
    assert res["source"] == "画像(LLM)"
    assert res["total_questions"] == 3
    assert res["clusters"][0]["category"] == "简历"      # 按 count 降序
    assert res["unmatched"] == 0


def test_concentration_flags_solo_speaker():
    """辰哥场景：9 条弹幕全出自 1 人 → 极高集中 + 疑似自己人撑场。"""
    danmaku = [_danmu(i, f"第{i}条", uid="u1", uname="我***") for i in range(9)]
    c = dr.analyze_concentration(danmaku)
    assert c["speakers"] == 1 and c["total_danmaku"] == 9
    assert c["level"] == "极高"
    assert c["warning"] and "疑似自己人" in c["warning"]


def test_concentration_normal_room_no_warning():
    danmaku = []
    for uid in ["a", "b", "c", "d", "e"]:
        danmaku += [_danmu(1, "x", uid=uid, uname=uid), _danmu(2, "y", uid=uid, uname=uid)]
    c = dr.analyze_concentration(danmaku)
    assert c["speakers"] == 5 and c["level"] == "低" and c["warning"] == ""


def test_parse_conclusion_strips_markdown():
    raw = "- **跟进**`考公年龄`选题\n2. 观众不知` 道 30 岁转行来得及吗"
    lines = dr._parse_conclusion(raw, 3)
    assert lines[0] == "跟进考公年龄选题"
    assert "不知道" in lines[1]              # 反引号+空格未把词切断
    joined = "".join(lines)
    assert "`" not in joined and "*" not in joined


def test_portrait_per_room_drives_report(tmp_path):
    """离线 mock：按房间画像文件被日报读取，话题/提问改走画像。"""
    tag, date, _ = _make_dataset(str(tmp_path))
    mock = tmp_path / "mock_portrait"
    mock.mkdir()
    _write_jsonl(str(mock / f"portrait_{tag}_{date}.jsonl"), [{
        "ts": 1, "room_id": "r1", "room_tag": tag,
        "portrait": {
            "portrait_ts": 1,
            "interest_topics": ["初中英语语法", "完形填空"],
            "question_topics": [
                {"topic": "错题整理", "count": 2, "examples": ["阅读理解错了也需要弄到错题本吗"]},
                {"topic": "学习方法", "count": 1, "examples": ["错题怎么抄"]},
            ],
            "consumption_intent": {"level": "low", "keywords": []},
            "activity_layers": {"high": [], "mid": [], "low": []},
            "sentiment": {"positive": 0.1, "neutral": 0.85, "negative": 0.05},
            "suggestions": [],
        },
    }])
    b = dr.discover_bundles(str(tmp_path), date)[0]
    pop, note = dr.load_portrait_for(b, date, "douyin", str(mock), [str(tmp_path)])
    assert pop and "按房间" in note
    sec = dr.build_session_section(b, dr.LlmSummarizer(client=None), pop, note)
    assert sec["topics"][0] == "初中英语语法"
    assert sec["questions"]["source"] == "画像(LLM)"
    assert any(c["category"] == "错题整理" for c in sec["questions"]["clusters"])
    assert "来源：画像(LLM)" in dr.render_session_md(sec)


def test_portrait_found_by_room_tag_in_global_file(tmp_path):
    """回退路径：全局 portrait_<date>.jsonl 里按 room_tag 过滤命中。"""
    tag, date, _ = _make_dataset(str(tmp_path))
    _write_jsonl(os.path.join(str(tmp_path), f"portrait_{date}.jsonl"), [{
        "ts": 1, "room_id": "r9", "room_tag": tag,
        "portrait": {"interest_topics": ["就业路径"], "sentiment": {"positive": 1, "neutral": 0, "negative": 0}},
    }])
    b = dr.discover_bundles(str(tmp_path), date)[0]
    pop, note = dr.load_portrait_for(b, date, "douyin", None, [str(tmp_path)])
    assert pop and pop["interest_topics"] == ["就业路径"]


def test_overview_shows_concentration_column(tmp_path):
    tag, date, _ = _make_dataset(str(tmp_path))
    b = dr.discover_bundles(str(tmp_path), date)[0]
    sec = dr.build_session_section(b, dr.LlmSummarizer(client=None))
    md = dr.render_overview_md([sec["row"]], date, None,
                               {"cost_yuan": 0, "calls": 0, "prompt_tokens": 0,
                                "completion_tokens": 0})
    assert "集中度" in md


# ---------------------------------------------------------------- 占位 user_id（2026-10-01 实测坑）
def test_concentration_placeholder_uid_falls_back_to_uname():
    """平台下发的 user_id 恒为占位值 111111 时，必须按昵称统计，
    否则会把整场判成「1 人发言 → 疑似自己人撑场」（误报）。"""
    danmaku = [_danmu(i, f"第{i}条", uid="111111", uname=f"观众{i % 40}") for i in range(120)]
    c = dr.analyze_concentration(danmaku)
    assert c["speakers"] == 40          # 按昵称去重后的真实发言人数，不是 1
    assert c["level"] == "低"
    assert c["warning"] == ""           # 不误报「疑似自己人撑场」
    assert "昵称" in c["key_source"]


def test_audience_placeholder_uid_counts_by_uname():
    danmaku = [_danmu(i, "x", uid="111111", uname=f"观众{i % 10}") for i in range(30)]
    a = dr.analyze_audience(danmaku)
    assert a["speaker_count"] == 10
    assert "昵称" in a["speaker_key_source"]
    assert a["top_speakers"][0]["uid"] == "(昵称)"   # 不给假 hash


def test_concentration_fallback_warning_carries_caveat():
    """昵称兜底 + 真的高集中 → 告警仍成立，但必须带上口径说明。"""
    danmaku = [_danmu(i, f"第{i}条", uid="111111", uname="同一个人") for i in range(9)]
    c = dr.analyze_concentration(danmaku)
    assert c["speakers"] == 1 and c["level"] == "极高"
    assert "疑似自己人" in c["warning"] and "昵称" in c["warning"]


def test_speaker_real_uid_still_preferred_over_uname():
    """user_id 正常时必须用 user_id，不受昵称影响（同名不同人不合并）。"""
    danmaku = [_danmu(1, "a", uid="1001", uname="同名"),
               _danmu(2, "b", uid="1002", uname="同名")]
    a = dr.analyze_audience(danmaku)
    assert a["speaker_count"] == 2
    assert "平台 user_id" in a["speaker_key_source"]
