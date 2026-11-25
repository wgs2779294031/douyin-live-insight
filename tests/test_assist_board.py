# -*- coding: utf-8 -*-
"""assist_board_server 单测：tmp 构造样例落盘 → 只读 tail → 断言聚合/派生/容错。

全部把 DATA 指向 tmp_path，绝不触碰真实 data/（同 test_dashboard_build 纪律）。
"""
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import assist_board_server as ab  # noqa: E402

TAG = "测试主播A"
DATE = "2026-10-10"
TS = 1791630000  # 2026-10-10 附近任一秒


def _wl(path, rows):
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _mk_store(tmp_path):
    s = ab.Store(str(tmp_path), DATE)
    return s


def _poll(s):
    s.poll()


def test_live_roster_follows_edits_with_history_and_new_anchor(tmp_path):
    roster = tmp_path / "anchors.json"
    tag2, new = "后来停用", "刚刚启用"
    store = ab.Store(str(tmp_path), DATE, roster_path=roster)
    for tag in (TAG, tag2):
        _wl(tmp_path / f"danmu_douyin_{tag}_{DATE}.jsonl",
            [{"type": "danmaku", "text": "历史", "ts": TS}])
    store.poll()
    roster.write_text(json.dumps({"anchors": [{"tag": TAG}, {"tag": tag2}]}), encoding="utf8")
    assert {r["tag"] for r in store.snapshot()["tags"]} == {TAG, tag2}
    roster.write_text(json.dumps({"anchors": [{"tag": tag2, "enabled": False},
                                             {"tag": new, "watch": False, "record": True}]}), encoding="utf8")
    snap = store.snapshot()
    assert snap["roster_error"] is None
    assert [r["tag"] for r in snap["tags"]] == [new]
    assert snap["tags"][0]["last_ts"] == 0
    assert store.take_new_danmu() is None
    assert set(ab.review.available_tags(str(tmp_path), DATE)) == {TAG, tag2}
    # Re-enabling reuses the existing state and filters a mixed batch of new messages.
    roster.write_text(json.dumps({"anchors": [{"tag": TAG}]}), encoding="utf8")
    for tag in (TAG, tag2):
        _wl(tmp_path / f"danmu_douyin_{tag}_{DATE}.jsonl",
            [{"type": "danmaku", "text": "新增", "ts": TS+1}])
    store.poll()
    assert [i["tag"] for i in store.take_new_danmu()["items"]] == [TAG]
    assert [r["tag"] for r in store.snapshot()["tags"]] == [TAG]


def test_roster_empty_corrupt_missing_and_recovered(tmp_path):
    roster = tmp_path / "anchors.json"
    store = ab.Store(str(tmp_path), DATE, roster_path=roster)
    _wl(tmp_path / f"watch_{TAG}.jsonl", [{"ts": TS, "is_live": True}])
    store.poll()
    assert store.snapshot()["tags"] == []
    assert store.snapshot()["roster_error"]
    assert not roster.exists()
    for payload in ("{", '{"anchors":true}', json.dumps({"anchors":[{"tag": TAG,"enabled":"false"}]})):
        roster.write_text(payload, encoding="utf8")
        snap = store.snapshot()
        assert snap["tags"] == [] and snap["roster_error"]
        assert roster.read_text(encoding="utf8") == payload
    roster.write_text('{"anchors":[]}', encoding="utf8")
    snap = store.snapshot()
    assert snap["tags"] == [] and snap["roster_error"] is None
    roster.write_text(json.dumps({"anchors": [{"tag": TAG, "enabled": True}]}), encoding="utf8")
    assert [r["tag"] for r in store.snapshot()["tags"]] == [TAG]


# ---------------- 文件名归类 ----------------

def test_classify():
    assert ab._classify("danmu_douyin_研途-张老师团队_2026-10-10.jsonl") == ("danmu", "研途-张老师团队")
    assert ab._classify("stats_douyin_测试主播A_2026-10-10.jsonl") == ("stats", "测试主播A")
    assert ab._classify("portrait_测试主播A_2026-10-10.jsonl") == ("portrait", "测试主播A")
    assert ab._classify("coldline_2026-10-10.jsonl") == ("coldline", None)
    # 离线 A/B 产物不是线上事件（2026-10-28：曾生成 room_None 占位 tab）
    assert ab._classify("coldline_compare_2026-10-28.jsonl") == (None, None)
    assert ab._classify("watch_测试主播A.jsonl") == ("watch", "测试主播A")
    assert ab._classify("别的文件.jsonl") == (None, None)


# ---------------- danmu 聚合 ----------------

def test_danmu_like_gift(tmp_path):
    _wl(tmp_path / f"danmu_douyin_{TAG}_{DATE}.jsonl", [
        {"ts": TS, "type": "danmaku", "uname": "甲***", "text": "多少分能报", "room_id": "r1"},
        {"ts": TS + 1, "type": "like", "text": "点了3个赞", "room_id": "r1"},
        {"ts": TS + 2, "type": "like", "text": "赞了", "room_id": "r1"},
        {"ts": TS + 3, "type": "gift", "uname": "乙***", "text": "送出 小心心 x5", "room_id": "r1"},
        {"ts": TS + 4, "type": "enter", "text": "进入了直播间", "room_id": "r1"},
        "坏行不是JSON",
    ])
    s = _mk_store(tmp_path)
    _poll(s)
    st = s.tags[TAG]
    assert st.totals == {"danmaku": 1, "enter": 1, "like": 4, "gift": 1}
    assert st.recent_danmaku[-1]["text"] == "送出 小心心 x5"
    assert st.recent_danmaku[-1]["gift"] is True
    assert st.room_id == "r1"
    # 坏行进 errors，不抛异常（裸字符串被 _wl 引号包裹 → 合法 JSON 但非 dict）
    assert any("非 dict" in e or "解析失败" in e for e in s.errors)


# ---------------- stats：在线/PV/序列 ----------------

def test_stats_online_pv_series(tmp_path):
    _wl(tmp_path / f"stats_douyin_{TAG}_{DATE}.jsonl", [
        {"ts": TS, "type": "room_stats", "room_id": "r1", "online": 100},
        {"ts": TS + 10, "type": "room_user_seq", "room_id": "r1", "online": 120, "total_pv": 5000},
        {"ts": TS + 20, "type": "room_stats", "room_id": "r1", "online": 90},
    ])
    s = _mk_store(tmp_path)
    _poll(s)
    st = s.tags[TAG]
    assert st.online == 90
    assert st.total_pv == 5000
    assert st.online_series[-1] == (TS + 20, 90)
    assert len(st.online_series) == 3


# ---------------- portrait：取最后一行 + 坏行容错 ----------------

def test_portrait_last_wins(tmp_path):
    p = tmp_path / f"portrait_{TAG}_{DATE}.jsonl"
    _wl(p, [
        {"ts": TS, "room_id": "r1", "room_tag": TAG,
         "portrait": {"portrait_ts": TS, "question_topics": [{"topic": "旧", "count": 1}],
                      "suggestions": ["旧建议"]}},
    ])
    # 追加坏行 + 新快照 → 以新快照为准
    with open(p, "a", encoding="utf-8") as f:
        f.write("{'坏的'\n")
    _wl(p, [{"ts": TS + 60, "room_id": "r1", "room_tag": TAG,
             "portrait": {"portrait_ts": TS + 60, "question_topics": [{"topic": "新", "count": 2}],
                          "suggestions": ["新建议"]}}])
    s = _mk_store(tmp_path)
    _poll(s)
    snap = s.tags[TAG].snapshot(TS + 120)
    assert snap["portrait_ts"] == TS + 60
    assert snap["questions"][0]["topic"] == "新"
    assert snap["suggestions"] == ["新建议"]


# ---------------- coldline：按 room_id 归 tag ----------------

def test_coldline_room_mapping(tmp_path):
    _wl(tmp_path / f"stats_douyin_{TAG}_{DATE}.jsonl",
        [{"ts": TS, "type": "room_stats", "room_id": "r1", "online": 10}])
    _wl(tmp_path / ("coldline_%s.jsonl" % DATE), [
        {"ts": TS, "room_id": "r1", "event": "cold_start", "lines": ["台词A"]},
        {"ts": TS + 1, "room_id": "r_unknown", "event": "cold_start", "lines": ["台词B"]},
    ])
    s = _mk_store(tmp_path)
    _poll(s)
    assert s.room2tag["r1"] == TAG
    cold = s.tags[TAG].cold_state(TS + 10)
    assert cold["state"] == "cold"
    assert cold["lines"] == ["台词A"]
    # 未知房间：TS 早于 1 小时兜底阈值 → 直接落占位 tag，pending 清空
    assert s.tags["room_r_unknown"].cold["lines"] == ["台词B"]


def test_coldline_without_room_id_ignored(tmp_path):
    """没有 room_id 的冷场行不能进看板 —— 否则变成 room_None 占位 tab。

    真实来历：`compare_coldline.py` 的离线产物 `coldline_compare_<date>.jsonl`
    没有 room_id 字段，旧逻辑 pending_cold[None] → 1 小时后兜底成 `room_None`。
    """
    _wl(tmp_path / f"stats_douyin_{TAG}_{DATE}.jsonl",
        [{"ts": TS, "type": "room_stats", "room_id": "r1", "online": 10}])
    _wl(tmp_path / ("coldline_%s.jsonl" % DATE), [
        {"ts": TS - 7200, "event": "cold_start", "lines": ["没房间号的老行"]},
    ])
    s = _mk_store(tmp_path)
    _poll(s)
    assert "room_None" not in s.tags
    assert s.pending_cold == {}


def test_cold_window_expiry(tmp_path):
    _wl(tmp_path / f"danmu_douyin_{TAG}_{DATE}.jsonl",
        [{"ts": TS, "type": "danmaku", "text": "x", "room_id": "r1"}])
    _wl(tmp_path / ("coldline_%s.jsonl" % DATE),
        [{"ts": TS, "room_id": "r1", "event": "cold_start", "lines": ["台词"]}])
    s = _mk_store(tmp_path)
    _poll(s)
    # 窗口内=冷场，窗外=正常但保留台词
    assert s.tags[TAG].cold_state(TS + 100)["state"] == "cold"
    assert s.tags[TAG].cold_state(TS + ab.COLD_ACTIVE_WINDOW + 1)["state"] == "normal"


# ---------------- watch：开播状态 ----------------

def test_watch_live_and_silence_fallback(tmp_path):
    _wl(tmp_path / f"watch_{TAG}.jsonl",
        [{"ts": TS, "is_live": True, "live_status": 2, "web_rid": "x"}])
    s = _mk_store(tmp_path)
    _poll(s)
    st = s.tags[TAG]
    live, src = st.live_status(TS + 10)
    assert live is True and src == "watch探测"
    # 过时的开播探测不能证明现在已下播。
    live, src = st.live_status(TS + 400)
    assert live is False and "未知" in src
    assert st.snapshot(TS + 400)["probe_state"] == "stale"
    # 无 watch 文件时靠数据流活动兜底
    _wl(tmp_path / f"stats_douyin_{TAG}_{DATE}.jsonl",
        [{"ts": TS + 500, "type": "room_stats", "room_id": "r1", "online": 5}])
    _poll(s)
    live, src = st.live_status(TS + 505)
    assert live is True and src == "数据流活动"


def test_invalid_watch_is_unknown_and_does_not_hide_stale_data():
    state = ab.TagState(TAG)
    assert state.snapshot(TS)["probe_state"] == "missing"
    assert state.snapshot(TS)["live"][0] is False
    state.add_stats({"ts": TS, "type": "room_stats", "online": 5})
    now = TS + 700
    for probe in ({"is_live": False, "error": "private-value"},
                  {"is_live": False, "api_msg": "blocked", "live_status": 0},
                  {"is_live": False}, {"is_live": False, "live_status": 1},
                  {"is_live": False, "live_status": 0, "api_code": 403},
                  {"is_live": True, "live_status": 1, "web_rid": ""},
                  {"is_live": False, "room_data_parse_error": "private-value", "live_status": 0}):
        state.set_watch({"ts": now, **probe})
        snap = state.snapshot(now)
        assert snap["probe_state"] == "error" and snap["probe_hint"]
        assert snap["live"][0] is False and "未知" in snap["live"][1]
        assert snap["stale"] is True
        assert "private-value" not in json.dumps(snap, ensure_ascii=False)
    # 画像更新不能单独当作实时互动。
    state.set_portrait({"ts": now, "portrait": {}})
    assert state.live_status(now)[0] is False
    state.add_danmu({"ts": now, "type": "danmaku", "text": "新消息"})
    assert state.snapshot(now)["live"] == [True, "数据流活动"]
    assert state.snapshot(now)["probe_state"] == "error"
    # 确认下播优先于之前收到的互动。
    state.set_watch({"ts": now, "is_live": False, "live_status": 0})
    snap = state.snapshot(now)
    assert snap["live"] == [False, "watch探测"] and snap["probe_state"] == "offline"
    assert snap["probe_hint"] is None and snap["stale"] is False


# ---------------- tail：增量 / 截断重置 ----------------

def test_tail_increment_and_truncate(tmp_path):
    f = tmp_path / f"danmu_douyin_{TAG}_{DATE}.jsonl"
    _wl(f, [{"ts": TS, "type": "danmaku", "text": "一", "room_id": "r1"}])
    s = _mk_store(tmp_path)
    _poll(s)
    assert s.tags[TAG].totals["danmaku"] == 1
    # 追加半行 → 增量读只收完整行
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": TS + 1, "type": "danmaku", "text": "二", "room_id": "r1"},
                            ensure_ascii=False)[:20])
    _poll(s)
    assert s.tags[TAG].totals["danmaku"] == 1
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": TS + 1, "type": "danmaku", "text": "二", "room_id": "r1"},
                            ensure_ascii=False)[20:] + "\n")
    _poll(s)
    assert s.tags[TAG].totals["danmaku"] == 2
    # 文件被截断（size < offset）→ 跳到文件尾：不崩、不重复计数
    with open(f, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": TS + 9, "type": "danmaku", "text": "三", "room_id": "r1"},
                            ensure_ascii=False) + "\n")
    _poll(s)
    assert s.tags[TAG].totals["danmaku"] == 2
    # 截断后的下一轮起，新追加的行照常增量读取
    _wl(f, [{"ts": TS + 10, "type": "danmaku", "text": "四", "room_id": "r1"}])
    _poll(s)
    assert s.tags[TAG].totals["danmaku"] == 3


# ---------------- snapshot 形状 ----------------

def test_snapshot_shape_and_order(tmp_path):
    _wl(tmp_path / f"danmu_douyin_{TAG}_{DATE}.jsonl",
        [{"ts": TS, "type": "danmaku", "text": "x", "room_id": "r1"}])
    _wl(tmp_path / ("watch_%s.jsonl" % TAG),
        [{"ts": TS, "is_live": False}])
    s = _mk_store(tmp_path)
    _poll(s)
    snap = s.snapshot()
    assert snap["date"] == DATE
    assert len(snap["tags"]) == 1
    t = snap["tags"][0]
    for k in ("tag", "live", "online", "total_pv", "online_series", "rate_per_min",
              "permin", "totals", "cold", "questions", "suggestions", "recent_danmaku"):
        assert k in t, k
    assert t["live"][0] is False  # watch 探测未开播
    assert t["rate_per_min"] == 0


# ---------------- 数据停滞（stale）三态 ----------------

def test_stale_when_data_silence_exceeds_window(tmp_path):
    """有过数据但超静默窗口 → stale=True：语义是「采集疑似未运行」而非未开播。"""
    import time as _t
    _wl(tmp_path / f"danmu_douyin_{TAG}_{DATE}.jsonl",
        [{"ts": TS, "type": "danmaku", "text": "x", "room_id": "r1"}])
    _wl(tmp_path / ("watch_%s.jsonl" % TAG),
        [{"ts": TS, "is_live": True}])
    s = _mk_store(tmp_path)
    _poll(s)
    t = s.tags[TAG].snapshot(TS + 60)
    assert t["stale"] is False          # 60s < 600s 静默窗口
    # 快照时刻推到 TS+700：watch 与数据流全部陈旧 → 数据停滞
    t2 = s.tags[TAG].snapshot(TS + 700)
    assert t2["stale"] is True
    assert t2["live"][0] is False
    assert t2["data_age"] == 700


def test_stale_false_when_never_had_data(tmp_path):
    """从无任何数据的 tag → 不建脏 tag（自然不存在 stale=True 的空房间）。"""
    s = _mk_store(tmp_path)
    _poll(s)
    assert TAG not in s.tags


def test_stale_false_when_watch_says_offline(tmp_path):
    """watch 探测新鲜且 is_live=False（正常下播）→ 即使数据陈旧也不算 stale。"""
    _wl(tmp_path / f"danmu_douyin_{TAG}_{DATE}.jsonl",
        [{"ts": TS, "type": "danmaku", "text": "x", "room_id": "r1"}])
    _wl(tmp_path / ("watch_%s.jsonl" % TAG),
        [{"ts": TS, "is_live": True},
         {"ts": TS + 700, "is_live": False, "live_status": 0}])   # 700s 后明确下播
    s = _mk_store(tmp_path)
    _poll(s)
    t = s.tags[TAG].snapshot(TS + 800)
    # 数据已陈旧（>600s），但 watch 明确说下播了 → 正常未开播，非采集异常
    assert t["stale"] is False
    assert t["live"][0] is False


def test_stale_true_when_live_but_no_data(tmp_path):
    """watch 说在播但数据流停 → 在播没数据 = 采集异常，stale=True。"""
    _wl(tmp_path / f"danmu_douyin_{TAG}_{DATE}.jsonl",
        [{"ts": TS, "type": "danmaku", "text": "x", "room_id": "r1"}])
    _wl(tmp_path / ("watch_%s.jsonl" % TAG),
        [{"ts": TS, "is_live": True},
         {"ts": TS + 700, "is_live": True, "live_status": 1, "web_rid": "test"}])
    s = _mk_store(tmp_path)
    _poll(s)
    t = s.tags[TAG].snapshot(TS + 800)
    assert t["stale"] is True
    assert t["live"] == [True, "watch探测"] and t["probe_state"] == "live"
