# -*- coding: utf-8 -*-
"""dashboard_build 单测：tmp 构造两天样例数据 → 生成 → 断言统计/容错/抽稀。

全部通过 monkeypatch 把 dashboard_build.DATA 指向 tmp_path、
ANCHORS 指向测试名册，绝不触碰真实 data/。
"""
import json
import pathlib
import sys
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import anchor_roster  # noqa: E402

# launch_all builds its legacy view at import. Collection must not consult or
# repair the default roster; every test below supplies its own synthetic view.
with patch.object(anchor_roster, "ensure", return_value=({"anchors": []}, False)):
    import dashboard_build as db  # noqa: E402

TAG = "测试主播A"
# 日期相对今天动态生成：dates_for 有 7 天滚动窗口，硬编码日期会随时间滚出
# 窗口导致 anchor 被过滤、测试集体失败（2026-10-14 实证）。固定取昨天/前天。
from datetime import date as _date, datetime as _dt, timedelta as _td
TODAY = _date.today()
DAY1 = (TODAY - _td(days=2)).isoformat()
DAY2 = (TODAY - _td(days=1)).isoformat()
TS1 = int(_dt.strptime(DAY1, "%Y-%m-%d").replace(hour=20).timestamp())
TS2 = int(_dt.strptime(DAY2, "%Y-%m-%d").replace(hour=20).timestamp())


def _patch_env(monkeypatch, tmp_path, anchors=None):
    monkeypatch.setattr(db, "DATA", tmp_path)
    monkeypatch.setattr(db, "ANCHORS", anchors or [("t1", "x", TAG)])
    monkeypatch.setattr(db, "OUT", tmp_path / "dashboard.html")


def _wl(path, rows):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                    encoding="utf-8")


def _mk_days(tmp_path, tag=TAG):
    """两天完整样例：danmu/stats/portrait/coldline/room_meta/watch/llm_budget。"""
    # danmu 两天：day1 三条，day2 两条（含一条坏行）
    _wl(tmp_path / ("danmu_douyin_%s_%s.jsonl" % (tag, DAY1)), [
        {"ts": TS1, "type": "danmaku", "text": "问题一"},
        {"ts": TS1 + 60, "type": "like"},
        {"ts": TS1 + 3600, "type": "gift"},
    ])
    p2 = tmp_path / ("danmu_douyin_%s_%s.jsonl" % (tag, DAY2))
    p2.write_text(
        json.dumps({"ts": TS2, "type": "danmaku", "text": "问题二"},
                   ensure_ascii=False) + "\n"
        + "{broken json\n"
        + json.dumps({"ts": TS2 + 120, "type": "enter"}) + "\n",
        encoding="utf-8")
    # stats 两天：每分钟多点（抽稀验证用 day2）
    seq1 = [[TS1 + i * 10, 50 + i] for i in range(30)]  # 5 分钟、10s 一点
    _wl(tmp_path / ("stats_douyin_%s_%s.jsonl" % (tag, DAY1)),
        [{"ts": t, "type": "room_stats", "online": v} for t, v in seq1]
        + [{"ts": TS1, "type": "room_user_seq", "total_pv": 1234}])
    seq2 = [[TS2 + i * 10, 80 + i] for i in range(30)]
    _wl(tmp_path / ("stats_douyin_%s_%s.jsonl" % (tag, DAY2)),
        [{"ts": t, "type": "room_stats", "online": v} for t, v in seq2]
        + [{"ts": TS2, "type": "room_user_seq", "total_pv": 2000}])
    # portrait 两天（末行=全量快照）
    _wl(tmp_path / ("portrait_%s_%s.jsonl" % (tag, DAY1)), [
        {"ts": TS1, "portrait": {"interest_topics": ["旧主题"],
                                 "question_topics": [{"topic": "分数线", "count": 1,
                                                      "examples": ["多少分"]}]}}])
    _wl(tmp_path / ("portrait_%s_%s.jsonl" % (tag, DAY2)), [
        {"ts": TS2, "portrait": {"interest_topics": []}},
        {"ts": TS2 + 100, "portrait": {
            "interest_topics": ["考研院校"],
            "question_topics": [{"topic": "复试线", "count": 3, "examples": ["复试多少分"]}],
            "sentiment": {"positive": 0.5, "neutral": 0.5, "negative": 0.0},
            "consumption_intent": {"level": "mid"},
            "activity_layers": {"high": 5, "mid": 10, "low": 20},
            "suggestions": ["多讲分数线"]}}])
    # coldline（按天，room_id 区分）
    _wl(tmp_path / ("coldline_%s.jsonl" % DAY2), [
        {"ts": TS2, "room_id": "R1", "event": "cold_start",
         "lines": ["家人们扣个1", "备用句", "第三句"]},
        {"ts": TS2 + 50, "room_id": "R999", "event": "cold_start",
         "lines": ["别的主播的"]},
    ])
    # room_meta + watch + llm_budget
    (tmp_path / ("room_meta_douyin_%s.json" % tag)).write_text(
        json.dumps({"room_id": "R1", "title": "今晚讲择校", "web_rid": "123",
                    "start_time": "20:00"}, ensure_ascii=False),
        encoding="utf-8")
    _wl(tmp_path / ("watch_%s.jsonl" % tag), [
        {"ts": TS2, "is_live": False, "nick": "A老师", "fans": 5555},
        {"ts": TS2 + 10, "is_live": True, "nick": "A老师", "fans": 5560},
    ])
    (tmp_path / ("llm_budget_%s.json" % DAY2)).write_text(
        json.dumps({"date": DAY2, "total": 18690, "budget": 1600000}),
        encoding="utf-8")


def test_build_two_days_and_content(monkeypatch, tmp_path):
    _patch_env(monkeypatch, tmp_path)
    _mk_days(tmp_path)
    out = db.build()
    assert out.exists()
    html = out.read_text(encoding="utf-8")
    assert TAG in html
    assert DAY1 in html and DAY2 in html
    assert "复试线" in html          # portrait 末行的 question_topics
    assert "考研院校" in html        # interest_topics
    assert "家人们扣个1" in html     # 冷场台词
    assert "今晚讲择校" in html
    assert "<script>" in html
    # 坏行被跳过：day2 total = 2（1 danmaku + 1 enter）
    assert '"total":2' in html.replace(" ", "")
    # watch 末行状态：在播 + fans 5560
    assert '"is_live":true' in html.replace(" ", "")
    assert "5560" in html
    # llm 账
    assert "1600000" in html


def test_overview_and_dates(monkeypatch, tmp_path):
    _patch_env(monkeypatch, tmp_path)
    _mk_days(tmp_path)
    payload = db.build_payload()
    a = payload["anchors"][0]
    assert a["dates"] == [DAY1, DAY2]
    assert len(a["overview"]) == 2
    ov = {r["date"]: r for r in a["overview"]}
    assert ov[DAY1]["total"] == 3 and ov[DAY1]["gift"] == 1
    assert ov[DAY2]["total"] == 2
    assert ov[DAY1]["online_peak"] == 79   # 50+29
    assert ov[DAY1]["total_pv"] == 1234
    assert ov[DAY2]["cold_count"] == 1     # 只算本主播 room_id R1
    # llm 只在有文件的日子
    assert payload["llm"].get(DAY2, {}).get("total") == 18690


def test_downsample_one_point_per_minute():
    base = 960  # 60 的整数倍，桶对齐
    series = [[base + i * 10, i] for i in range(30)]  # 290s 跨度，10s 一点
    out = db.downsample(series, per_minute=1)
    assert len(out) == 5               # 960..1250 落在 5 个分钟桶
    assert out[0] == [base, 5]         # 桶起点对齐，每桶保留最后一个点（i=0..5）
    assert out[-1] == [base + 240, 29]  # 末桶 i=24..29
    assert db.downsample([], per_minute=1) == []


def test_bad_and_missing_files_tolerated(monkeypatch, tmp_path):
    _patch_env(monkeypatch, tmp_path)
    # 完全无数据：不炸、anchors 过滤为空（无数据号不进板）
    payload = db.build_payload()
    assert payload["anchors"] == []
    out = db.build()
    assert out.exists()
    # 存在但全坏行：也不炸（danmu 计数全 0 但仍进板）
    (tmp_path / ("danmu_douyin_%s_%s.jsonl" % (TAG, DAY1))).write_text(
        "{oops\n{oops\n", encoding="utf-8")
    payload2 = db.build_payload()
    assert payload2["anchors"][0]["dates"] == [DAY1]
    dd = payload2["anchors"][0]["days_data"][DAY1]
    assert dd["danmu"]["counts"]["total"] == 0


def test_sync_desktop_copy(monkeypatch, tmp_path):
    """桌面已有 dashboard.html 副本时，build() 同步覆盖它；无副本则不动。"""
    fake_home = tmp_path / "home"
    desk = fake_home / "Desktop"
    desk.mkdir(parents=True)
    old = desk / "dashboard.html"
    old.write_text("OLD", encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    _patch_env(monkeypatch, tmp_path)
    _mk_days(tmp_path)
    db.build()
    assert "OLD" not in old.read_text(encoding="utf-8")
    assert TAG in old.read_text(encoding="utf-8")   # 与正式看板同内容
    # 无副本的主机：不创建新文件
    fake_home2 = tmp_path / "home2"
    (fake_home2 / "Desktop").mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(fake_home2))
    db.build()
    assert not (fake_home2 / "Desktop" / "dashboard.html").exists()


def test_empty_anchor_filtered(monkeypatch, tmp_path):
    """近 7 天无采集数据的号不进板（2026-10-08 规则）。"""
    _patch_env(monkeypatch, tmp_path,
               anchors=[("t1", "x", TAG), ("t2", "y", "没播过的号")])
    _mk_days(tmp_path)
    payload = db.build_payload()
    tags = [a["tag"] for a in payload["anchors"]]
    assert TAG in tags and "没播过的号" not in tags


def test_all_empty_payload_renders(monkeypatch, tmp_path):
    """全部无数据：payload.anchors 为空但 HTML 仍生成（前端有空态）。"""
    _patch_env(monkeypatch, tmp_path)
    out = db.build()
    html = out.read_text(encoding="utf-8")
    assert "近 7 天无任何主播开播数据" in html


def test_excludes_other_anchor_coldline(monkeypatch, tmp_path):
    """coldline 按天共享文件：别的主播(room_id 不同)的冷场不得算进来。"""
    _patch_env(monkeypatch, tmp_path, anchors=[("t1", "x", TAG), ("t2", "y", "测试主播B")])
    _mk_days(tmp_path)
    # B 主播也需有数据才进板（无数据号被过滤）
    (tmp_path / ("room_meta_douyin_测试主播B.json")).write_text(
        json.dumps({"room_id": "R2"}), encoding="utf-8")
    _wl(tmp_path / ("danmu_douyin_测试主播B_%s.jsonl" % DAY2),
        [{"ts": TS2, "type": "danmaku", "text": "B的弹幕"}])
    payload = db.build_payload()
    by_tag = {a["tag"]: a for a in payload["anchors"]}
    a_day = by_tag[TAG]["days_data"][DAY2]
    b_day = by_tag["测试主播B"]["days_data"][DAY2]
    assert a_day["cold"]["count"] == 1          # R1 的冷场归 A
    assert not b_day.get("cold")                # R1/R999 都不归 B
