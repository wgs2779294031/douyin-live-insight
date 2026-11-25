# -*- coding: utf-8 -*-
"""profile_draft 的机械体检单测：不调 LLM，只验「错卡能不能被抓住」。"""
import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import profile_draft as pdr  # noqa: E402

DIGEST = """### 测主播
- 素材：100 段 / 60 分钟 / 1 天
**候选口癖（特异性 = 本主播每万字频率 ÷ 其他主播中位频率）**：
  听懂了吗×64（×21.7）、好不好×234（×5.6）、第一个×147（×1.8）
"""


def test_cli_provider_override_keeps_bound_config_source(tmp_path, monkeypatch):
    import json
    import llm_service
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'llm': {'enabled': False, 'active_provider': 'primary',
                                        'providers': {'primary': {}, 'custom': {}}}}), encoding='utf-8')
    monkeypatch.setattr(pdr, 'ROOT', tmp_path)
    monkeypatch.setattr(pdr, 'read_digest', lambda *_args: DIGEST)
    monkeypatch.setattr(sys, 'argv', ['profile_draft', '--tag', 'synthetic', '--provider', 'custom'])
    seen = []
    def build(cfg, *, provider_override=None):
        seen.append((cfg._source_path, provider_override, cfg.active_provider))
        return SimpleNamespace(chat=lambda *_args, **_kwargs: None)
    monkeypatch.setattr(llm_service.LLMConfig, 'build_router', build)
    assert pdr.main() == 2
    assert seen == [(str(config.resolve()), 'custom', 'primary')]


def _card(**kw):
    base = {
        "name": "测", "category": "升学规划", "style": "条理清晰",
        "catchphrases": ["好不好", "听懂了吗"], "fan_call": "家长",
        "audience": "高考家长", "interaction_preference": ["弹幕提问"],
        "forbidden_topics": ["保过"],
    }
    base.update(kw)
    return base


def test_good_card_passes():
    issues, card = pdr.check_card(_card(), DIGEST)
    assert issues == []
    assert card["forbidden_topics"] == ["保过"]


def test_fan_call_with_comma_is_caught():
    issues, _ = pdr.check_card(_card(fan_call="家长、各位家长"), DIGEST)
    assert any("单个称呼" in i for i in issues)


def test_fan_call_in_catchphrases_is_caught():
    issues, _ = pdr.check_card(_card(catchphrases=["好不好", "家长"]), DIGEST)
    assert any("称呼词" in i for i in issues)


def test_generic_catchphrase_not_in_candidates_is_caught():
    """「不要」这类通用词不在候选行里 → 抓出来。"""
    issues, _ = pdr.check_card(_card(catchphrases=["不要", "好不好"]), DIGEST)
    assert any("不在候选里" in i for i in issues)


def test_placeholder_forbidden_is_replaced():
    issues, card = pdr.check_card(_card(forbidden_topics=["无明确禁忌话题"]), DIGEST)
    assert any("占位词" in i for i in issues)
    assert "保过" in card["forbidden_topics"]


def test_string_interaction_preference_fixed():
    issues, card = pdr.check_card(_card(interaction_preference="弹幕提问"), DIGEST)
    assert any("不是数组" in i for i in issues)
    assert card["interaction_preference"] == ["弹幕提问"]


def test_overlong_card_flags_forbidden_trim():
    """超 300 字时 forbidden_topics 会被裁 → 必须单独警告（合规闸门失效）。"""
    issues, _ = pdr.check_card(
        _card(style="很长的风格描述" * 40), DIGEST)
    assert any("被裁掉字段" in i for i in issues)
    assert any("合规闸门失效" in i for i in issues)


# ---------------------------------------------------------------- --apply
def _official(tmp_path, tag, **kw):
    p = tmp_path / "cards" / ("%s.json" % tag)
    p.parent.mkdir(exist_ok=True)
    p.write_text(__import__("json").dumps(_card(**kw), ensure_ascii=False), encoding="utf-8")
    return p


def test_apply_blocks_fact_field_change(tmp_path):
    """事实字段（fan_call/口癖/禁区）不许机器擅改 —— 必须 --force。"""
    p = _official(tmp_path, "A")
    ok, msg, fact = pdr.apply_card(p, _card(catchphrases=["好不好"]))
    assert ok is False and "需 --force" in msg
    assert fact[0][0] == "catchphrases"
    # 卡没被改
    assert "听懂了吗" in p.read_text(encoding="utf-8")


def test_apply_allows_description_only_change(tmp_path):
    """只改 style/audience（描述层）→ 直接放行，并留备份。"""
    p = _official(tmp_path, "B")
    ok, msg, fact = pdr.apply_card(p, _card(style="口语化，强调专业适配"))
    assert ok is True and fact == []
    assert "强调专业适配" in p.read_text(encoding="utf-8")
    assert list((tmp_path / "cards" / "_backup").glob("B_*.json"))


def test_apply_force_writes_fact_change(tmp_path):
    p = _official(tmp_path, "C")
    ok, _, fact = pdr.apply_card(p, _card(fan_call="同学"), force=True)
    assert ok is True and fact[0][0] == "fan_call"
    assert "同学" in p.read_text(encoding="utf-8")


def test_apply_new_card_needs_no_force(tmp_path):
    """首次建卡没有旧事实可保护，不该被 --force 卡住。"""
    p = tmp_path / "cards" / "D.json"
    p.parent.mkdir()
    ok, _, fact = pdr.apply_card(p, _card())
    assert ok is True
    assert p.exists()


def test_diff_cards_reports_only_changes():
    d = pdr.diff_cards({"a": 1, "b": 2}, {"a": 1, "b": 3, "c": 4})
    assert set(k for k, _, _ in d) == {"b", "c"}


# ---------------------------------------------------------------- --apply
def _official(tmp_path, tag, **kw):
    p = tmp_path / "cards" / ("%s.json" % tag)
    p.parent.mkdir(exist_ok=True)
    p.write_text(__import__("json").dumps(_card(**kw), ensure_ascii=False), encoding="utf-8")
    return p


def test_apply_blocks_fact_field_change(tmp_path):
    """事实字段（fan_call/口癖/禁区）不许机器擅改 —— 必须 --force。"""
    p = _official(tmp_path, "A")
    ok, msg, fact = pdr.apply_card(p, _card(catchphrases=["好不好"]))
    assert ok is False and "需 --force" in msg
    assert fact[0][0] == "catchphrases"
    # 卡没被改
    assert "听懂了吗" in p.read_text(encoding="utf-8")


def test_apply_allows_description_only_change(tmp_path):
    """只改 style/audience（描述层）→ 直接放行，并留备份。"""
    p = _official(tmp_path, "B")
    ok, msg, fact = pdr.apply_card(p, _card(style="口语化，强调专业适配"))
    assert ok is True and fact == []
    assert "强调专业适配" in p.read_text(encoding="utf-8")
    assert list((tmp_path / "cards" / "_backup").glob("B_*.json"))


def test_apply_force_writes_fact_change(tmp_path):
    p = _official(tmp_path, "C")
    ok, _, fact = pdr.apply_card(p, _card(fan_call="同学"), force=True)
    assert ok is True and fact[0][0] == "fan_call"
    assert "同学" in p.read_text(encoding="utf-8")


def test_apply_new_card_needs_no_force(tmp_path):
    """首次建卡没有旧事实可保护，不该被 --force 卡住。"""
    p = tmp_path / "cards" / "D.json"
    p.parent.mkdir()
    ok, _, fact = pdr.apply_card(p, _card())
    assert ok is True
    assert p.exists()


def test_diff_cards_reports_only_changes():
    d = pdr.diff_cards({"a": 1, "b": 2}, {"a": 1, "b": 3, "c": 4})
    assert set(k for k, _, _ in d) == {"b", "c"}
