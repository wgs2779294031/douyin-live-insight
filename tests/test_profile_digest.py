# -*- coding: utf-8 -*-
"""profile_digest 的 --check（人设卡保鲜）单测：不碰真实 data/ 与真实卡。"""
import json
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import profile_digest as pd  # noqa: E402


def _card(tmp_path, tag, catchphrases=("对不对",), fan="家长", age_sec=3600):
    cards = tmp_path / "cards"
    cards.mkdir(exist_ok=True)
    p = cards / ("%s.json" % tag)
    p.write_text(json.dumps({
        "name": tag, "category": "升学规划", "style": "结构化",
        "catchphrases": list(catchphrases), "fan_call": fan,
        "audience": "家长", "interaction_preference": [], "forbidden_topics": [],
    }, ensure_ascii=False), encoding="utf-8")
    old = time.time() - age_sec
    os.utime(p, (old, old))
    return p


def _speech(tmp_path, tag, date, texts, dur=6.0):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    p = data / ("speech_%s_%s.jsonl" % (tag, date))
    with p.open("w", encoding="utf-8") as f:
        for i, t in enumerate(texts):
            f.write(json.dumps({"ts": 1.0 + i, "time": "%s 20:00:00" % date,
                                "text": t, "dur": dur, "tag": tag},
                               ensure_ascii=False) + "\n")
    return p


def _run(tmp_path, monkeypatch, tag, min_new_minutes=10.0):
    monkeypatch.setattr(pd, "DATA", tmp_path / "data")
    return pd.check_cards(tmp_path / "cards", min_new_minutes=min_new_minutes)


def test_no_new_material_is_unverifiable(tmp_path, monkeypatch):
    _card(tmp_path, "A", age_sec=86400)
    _speech(tmp_path, "A", "2026-10-01", ["对不对，这个我说过"])   # 早于卡 → 不算新素材
    text, stale = _run(tmp_path, monkeypatch, "A")
    assert "无法验证" in text
    assert stale is False


def test_fresh_card_keeps_hitting(tmp_path, monkeypatch):
    _card(tmp_path, "B", age_sec=3600)
    _speech(tmp_path, "B", "2026-10-28",
            ["对不对，家长看这里"] * 200, dur=6.0)               # 20 分钟，命中 200 次
    text, stale = _run(tmp_path, monkeypatch, "B")
    assert "判定：正常" in text
    assert stale is False


def test_drifted_card_flagged(tmp_path, monkeypatch):
    _card(tmp_path, "C", age_sec=3600)
    _speech(tmp_path, "C", "2026-10-28",
            ["这个方案很直接", "我再说一遍结论"] * 200)           # 20 分钟，零命中
    text, stale = _run(tmp_path, monkeypatch, "C")
    assert "该重写" in text
    assert stale is True


def test_too_little_new_material(tmp_path, monkeypatch):
    _card(tmp_path, "D", age_sec=3600)
    _speech(tmp_path, "D", "2026-10-28", ["对不对"] * 5)         # 0.5 分钟
    text, stale = _run(tmp_path, monkeypatch, "D")
    assert "无法验证" in text
    assert "素材不足" in text
    assert stale is False


def test_broken_card_does_not_crash(tmp_path, monkeypatch):
    cards = tmp_path / "cards"
    cards.mkdir()
    (cards / "E.json").write_text("{这不是 json", encoding="utf-8")
    text, stale = _run(tmp_path, monkeypatch, "E")
    assert "读不出来" in text
    assert stale is False


def test_default_card_skipped(tmp_path, monkeypatch):
    _card(tmp_path, "default", catchphrases=(), fan="")
    text, _ = _run(tmp_path, monkeypatch, "default")
    assert "### default" not in text


def test_card_without_material_reported(tmp_path, monkeypatch):
    _card(tmp_path, "F", age_sec=60)
    text, stale = _run(tmp_path, monkeypatch, "F")
    assert "没有转写素材" in text
    assert stale is False


# ---------------------------------------------------------------- --status
def _roster(tmp_path, tags):
    p = tmp_path / "anchors.json"
    p.write_text(json.dumps({"version": 1, "anchors": [{"tag": t} for t in tags]},
                            ensure_ascii=False), encoding="utf-8")
    return p


def test_status_lists_missing_card_with_material(tmp_path, monkeypatch):
    _card(tmp_path, "已有卡", age_sec=60)
    _speech(tmp_path, "已有卡", "2026-10-01", ["对不对"])
    _speech(tmp_path, "新主播", "2026-10-28", ["这个专业没问题"] * 700)   # 70 分钟
    monkeypatch.setattr(pd, "DATA", tmp_path / "data")
    text, _ = pd.status_report(tmp_path / "cards", _roster(tmp_path, ["新主播"]))
    assert "## 还没建卡" in text
    assert "新主播" in text and "可写卡" in text
    assert "可以开写：新主播" in text


def test_status_marks_thin_material(tmp_path, monkeypatch):
    _card(tmp_path, "A", age_sec=60)
    _speech(tmp_path, "A", "2026-10-01", ["对不对"])
    _speech(tmp_path, "薄", "2026-10-28", ["嗯嗯"] * 200)                  # 20 分钟
    monkeypatch.setattr(pd, "DATA", tmp_path / "data")
    text, _ = pd.status_report(tmp_path / "cards", _roster(tmp_path, []))
    assert "只能写薄卡" in text


def test_status_no_material_from_roster(tmp_path, monkeypatch):
    _card(tmp_path, "A", age_sec=60)
    _speech(tmp_path, "A", "2026-10-01", ["对不对"])
    monkeypatch.setattr(pd, "DATA", tmp_path / "data")
    text, _ = pd.status_report(tmp_path / "cards", _roster(tmp_path, ["袁军"]))
    assert "**袁军**：没有转写素材" in text
    assert "可以开写" not in text


def test_status_reports_stale_card(tmp_path, monkeypatch):
    _card(tmp_path, "C", age_sec=3600)
    _speech(tmp_path, "C", "2026-10-28", ["这个方案很直接"] * 200)
    monkeypatch.setattr(pd, "DATA", tmp_path / "data")
    text, stale = pd.status_report(tmp_path / "cards", _roster(tmp_path, []))
    assert stale == ["C"]
    assert "该重写：C" in text
