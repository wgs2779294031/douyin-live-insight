# -*- coding: utf-8 -*-
"""房间元数据合并：低质量来源不得覆盖较优值（2026-10-01 实测坑）。

背景：直播结束后断线重连，`webcast/room/web/enter/` 取不到 → 退到「房间页 HTML 正则」，
该正则会误抓页面上任意一个 nickname（实测把「冤种小徐」写成了「かみ」）。
修法：`client._prefer_richer_meta` 按来源质量排序，低质量不覆盖。
"""

import sys

import pytest

import client as C
import monitor as M


def test_fallback_does_not_overwrite_enter_meta():
    new = {"source": "room_page_html", "anchor_name": "かみ", "title": ""}
    prev = {"source": "webcast/room/web/enter", "anchor_name": "冤种小徐",
            "title": "进来聊聊怎么找工作"}
    out = C._prefer_richer_meta(new, prev)
    assert out["anchor_name"] == "冤种小徐"
    assert out["title"] == "进来聊聊怎么找工作"
    assert out["source"] == "webcast/room/web/enter"


def test_better_source_overwrites():
    new = {"source": "webcast/room/web/enter", "anchor_name": "正确名", "title": "新标题"}
    prev = {"source": "room_page_html", "anchor_name": "かみ", "title": "旧"}
    out = C._prefer_richer_meta(new, prev)
    assert out["anchor_name"] == "正确名" and out["title"] == "新标题"


def test_no_prev_keeps_new():
    out = C._prefer_richer_meta(
        {"source": "room_page_html", "anchor_name": "かみ", "title": "t"}, {})
    assert out["anchor_name"] == "かみ"


def test_equal_rank_keeps_new_value():
    """同级别来源：不合并（是否为空由上层决定），避免出现「半新半旧」的字段。"""
    new = {"source": "room_page_html", "anchor_name": "", "title": "新"}
    prev = {"source": "room_page_html", "anchor_name": "旧名", "title": "旧"}
    out = C._prefer_richer_meta(new, prev)
    assert out["anchor_name"] == "" and out["title"] == "新"


def test_source_rank_order_is_monotonic():
    ranks = [C.META_SOURCE_RANK[k] for k in ("fallback", "room_page_html",
                                             "webcast/room/web/enter")]
    assert ranks == sorted(ranks)


def test_monitor_read_room_meta_roundtrip(tmp_path):
    m = M.Monitor(output_dir=str(tmp_path), platform="douyin", room_tag="t1")
    assert m.read_room_meta() == {}
    m.write_room_meta({"anchor_name": "甲", "title": "标题",
                       "source": "webcast/room/web/enter"})
    got = m.read_room_meta()
    assert got["anchor_name"] == "甲" and got["room_tag"] == "t1"


def test_monitor_read_room_meta_tolerates_broken_json(tmp_path):
    m = M.Monitor(output_dir=str(tmp_path), platform="douyin", room_tag="t2")
    with open(m._meta_path, "w", encoding="utf-8") as f:
        f.write("{ not json")
    assert m.read_room_meta() == {}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
