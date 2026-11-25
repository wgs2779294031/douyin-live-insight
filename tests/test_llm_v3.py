# -*- coding: utf-8 -*-
"""LLM 服务层回归测试（v2.3 人设卡 + v2.2 回归红线）。

全部用例使用 Mock LLMClient / FakeRouter，**不发起任何真实 API 调用**，
不产生任何费用。落盘写入 pytest tmp_path，绝不触碰 data/ 正式目录。

运行：pytest tests/test_llm_v3.py -v
依赖：pytest（pip install pytest）
"""
import json
import os
import queue
import threading
from unittest.mock import patch, MagicMock

import pytest

# 将项目根加入 sys.path，保证 `import llm_service` 可用
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_service as L
from llm_service import (
    PORTRAIT_SYSTEM, COLDLINE_SYSTEM,
    AnchorConfig, SharedState, LLMConfig,
    PortraitService, ColdlineService,
    load_anchor_profile, _trim_profile, _filter_forbidden,
    _check_catchphrase, _is_empty_profile, _build_coldline_system,
)


# ============================================================ Mock 工具
class FakeLLMClient:
    """模拟 LLMClient/LLMRouter 的 chat 接口，记录调用并返回预设响应。"""

    def __init__(self, response: str = "", responses=None):
        # response: 单次固定返回；responses: 按调用顺序依次返回（用完回退到 response）
        self._responses = list(responses) if responses else []
        self._default = response
        self.calls = []  # (system, user, temperature, timeout_override)

    def chat(self, system, user, temperature=0.7, timeout_override=None,
             on_metrics=None, purpose=""):
        self.calls.append((system, user, temperature, timeout_override))
        if self._responses:
            return self._responses.pop(0)
        return self._default

    @property
    def last_system(self) -> str:
        return self.calls[-1][0] if self.calls else ""


class FakeRouter:
    """模拟 LLMRouter，供 run_llm_worker 使用。"""

    def __init__(self, chat_response: str = "", chat_responses=None):
        self._client = FakeLLMClient(chat_response, chat_responses)
        self.primary = "doubao"
        self.active = "doubao"
        self.active_model = "fake-model"

    @property
    def is_available(self) -> bool:
        return True

    def probe_and_maybe_switch(self):
        pass

    def chat(self, system, user, temperature=0.7, timeout_override=None,
             purpose=""):
        return self._client.chat(system, user, temperature, timeout_override)

    @property
    def calls(self):
        return self._client.calls


def _write_profile(tmp_path, room_id, profile_dict):
    """在 tmp_path 下写 {room_id}.json 人设卡。"""
    path = tmp_path / f"{room_id}.json"
    path.write_text(json.dumps(profile_dict, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _make_profile(**overrides):
    base = {
        "name": "测试主播",
        "category": "游戏",
        "style": "搞笑",
        "catchphrases": ["家人们", "宝子们"],
        "fan_call": "家人们",
        "audience": "年轻人",
        "interaction_preference": ["扣1", "投票"],
        "forbidden_topics": ["政治", "低俗"],
    }
    base.update(overrides)
    return base


# ============================================================ 1. 回归红线
class TestRegressionRedline:
    """anchor.enabled=false / profile 为空时，prompt 与 v2.2 逐字符一致。"""

    def test_portrait_system_verbatim_when_no_profile(self):
        client = FakeLLMClient(response=json.dumps({
            "interest_topics": [], "consumption_intent": {"level": "low"},
            "activity_layers": {}, "sentiment": {"positive": 0, "neutral": 0, "negative": 0},
            "suggestions": [],
        }))
        svc = PortraitService(client)
        svc.generate({"ts": 1, "top_words": []}, profile=None)
        assert client.last_system == PORTRAIT_SYSTEM

    def test_coldline_system_verbatim_when_no_profile(self):
        client = FakeLLMClient(response='["家人们扣个1"]')
        svc = ColdlineService(client)
        lines, stats = svc.generate({"event": "cold_start"}, {"top_words": []}, profile=None)
        assert client.last_system == COLDLINE_SYSTEM
        assert stats["anchor_used"] is False
        assert stats["anchor_name"] == ""

    def test_empty_profile_dict_treated_as_no_persona(self):
        client = FakeLLMClient(response='["x"]')
        svc = ColdlineService(client)
        lines, stats = svc.generate({}, {}, profile={})
        assert client.last_system == COLDLINE_SYSTEM
        assert stats["anchor_used"] is False


# ============================================================ 2. 人设注入
class TestAnchorInjection:
    def test_system_prompt_contains_persona_fields(self):
        client = FakeLLMClient(response='{"lines": ["家人们好"]}')
        svc = ColdlineService(client)
        profile = _make_profile(name="晚晴", catchphrases=["宝子们"])
        svc.generate({}, {}, profile=profile)
        sys_prompt = client.last_system
        assert "晚晴" in sys_prompt
        assert "宝子们" in sys_prompt

    def test_portrait_suffix_injected_with_category_style(self):
        client = FakeLLMClient(response=json.dumps({
            "interest_topics": [], "consumption_intent": {"level": "low"},
            "activity_layers": {}, "sentiment": {"positive": 0.3, "neutral": 0.4, "negative": 0.3},
            "suggestions": [],
        }))
        svc = PortraitService(client)
        profile = _make_profile(category="美妆", style="温柔")
        svc.generate({"ts": 1}, profile=profile)
        assert client.last_system.startswith(PORTRAIT_SYSTEM)
        assert "美妆" in client.last_system
        assert "温柔" in client.last_system


# ============================================================ 3. 禁区过滤
class TestForbiddenFilter:
    def test_line_without_negation_prefix_is_dropped(self):
        lines = ["我们来聊政治话题", "正常台词"]
        kept, dropped = _filter_forbidden(lines, ["政治"])
        assert kept == ["正常台词"]
        assert dropped == 1

    def test_negation_prefix_exempts(self):
        # 实现语义：禁区词紧邻前一个字符为否定字时豁免
        for neg in ["不", "没", "无", "别", "勿"]:
            line = f"我们{neg}政治"
            kept, dropped = _filter_forbidden([line], ["政治"])
            assert kept == [line], f"否定前缀'{neg}'应豁免"
            assert dropped == 0

    def test_all_lines_hit_returns_empty_no_exception(self):
        lines = ["政治话题", "低俗内容"]
        kept, dropped = _filter_forbidden(lines, ["政治", "低俗"])
        assert kept == []
        assert dropped == 2

    def test_forbidden_filter_applied_in_coldline_pipeline(self):
        # 模型返回含禁区词的台词，经硬过滤后被丢弃
        client = FakeLLMClient(response='{"lines": ["今天聊政治", "家人们扣1"]}')
        svc = ColdlineService(client)
        profile = _make_profile(forbidden_topics=["政治"], catchphrases=["家人们"])
        lines, stats = svc.generate({}, {}, profile=profile)
        assert "今天聊政治" not in lines
        assert "家人们扣1" in lines
        assert stats["forbidden_filtered_count"] == 1


# ============================================================ 4. 口头禅校验 + 连续 5 次零命中降级
class TestCatchphraseDegradation:
    def test_hit_count_correct(self):
        profile = _make_profile(catchphrases=["家人们"], fan_call="宝子")
        # 两条含口头禅/粉丝称呼，一条不含
        lines = ["家人们好", "宝子们扣1", "大家好"]
        hits = _check_catchphrase(lines, profile["catchphrases"], profile["fan_call"])
        assert hits == 2

    def test_consecutive_five_zero_hits_disables_anchor(self, tmp_path):
        """连续 5 轮 catchphrase_hit_count=0 → session_anchor_disabled=True。"""
        profile_dir = tmp_path / "profiles"
        profile_dir.mkdir()
        _write_profile(profile_dir, "1001", _make_profile(catchphrases=["家人们"]))

        shared = SharedState()
        shared.room_id = "1001"
        anchor_cfg = AnchorConfig(enabled=True, profile_dir=str(profile_dir))
        cfg = LLMConfig(enabled=True, providers={"doubao": {"api_key": "k"}},
                        coldline_min_interval_seconds=0)

        # 6 轮冷场：前 5 轮台词不含"家人们"，第 6 轮验证 anchor_used=False
        responses = ['{"lines": ["大家好"]}'] * 5 + ['{"lines": ["大家好"]}']
        router = FakeRouter(chat_responses=responses)

        q = queue.Queue()
        for _ in range(6):
            q.put({"kind": "event", "event": {"event": "cold_start", "ts": 1}})
        q.put(None)

        with patch.object(LLMConfig, "build_router", return_value=router):
            L.run_llm_worker(q, cfg, output_dir=str(tmp_path / "out"),
                             room_id="1001", shared_state=shared,
                             anchor_cfg=anchor_cfg)

        # 读取 coldline 落盘，验证 anchor_used 变化
        cold_path = tmp_path / "out" / f"coldline_{L.time.strftime('%Y-%m-%d')}.jsonl"
        records = [json.loads(line) for line in cold_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(records) == 6
        # 前 5 轮 anchor_used=True（人设已注入但零命中），第 6 轮 anchor_used=False
        for i in range(5):
            assert records[i]["anchor_used"] is True, f"第{i}轮应为True"
            assert records[i]["catchphrase_hit_count"] == 0
        assert records[5]["anchor_used"] is False

    def test_hit_resets_consecutive_counter(self, tmp_path):
        profile_dir = tmp_path / "profiles"
        profile_dir.mkdir()
        _write_profile(profile_dir, "1001", _make_profile(catchphrases=["家人们"]))

        shared = SharedState()
        shared.room_id = "1001"
        anchor_cfg = AnchorConfig(enabled=True, profile_dir=str(profile_dir))
        cfg = LLMConfig(enabled=True, providers={"doubao": {"api_key": "k"}},
                        coldline_min_interval_seconds=0)

        # 4 次零命中 + 1 次命中 + 1 次零命中 → 不会触发降级（第6次仍 anchor_used=True）
        responses = [
            '{"lines": ["大家好"]}', '{"lines": ["大家好"]}',
            '{"lines": ["大家好"]}', '{"lines": ["大家好"]}',
            '{"lines": ["家人们好"]}',  # 命中，归零
            '{"lines": ["大家好"]}',     # 又零命中，但连续计数=1
        ]
        router = FakeRouter(chat_responses=responses)
        q = queue.Queue()
        for _ in range(6):
            q.put({"kind": "event", "event": {"event": "cold_start", "ts": 1}})
        q.put(None)
        with patch.object(LLMConfig, "build_router", return_value=router):
            L.run_llm_worker(q, cfg, output_dir=str(tmp_path / "out"),
                             room_id="1001", shared_state=shared,
                             anchor_cfg=anchor_cfg)
        cold_path = tmp_path / "out" / f"coldline_{L.time.strftime('%Y-%m-%d')}.jsonl"
        records = [json.loads(line) for line in cold_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert all(r["anchor_used"] is True for r in records)


# ============================================================ 5. 目录缺失/损坏降级
class TestProfileLoadDegradation:
    def test_missing_dir_returns_error(self, tmp_path):
        profile, err = load_anchor_profile(str(tmp_path / "no_such_dir"), "1001")
        assert profile is None
        assert err is not None

    def test_corrupt_json_returns_error(self, tmp_path):
        bad = tmp_path / "1001.json"
        bad.write_text("{not valid json", encoding="utf-8")
        profile, err = load_anchor_profile(str(tmp_path), "1001")
        assert profile is None
        assert "解析失败" in err

    def test_worker_warns_once_on_missing_profile(self, tmp_path, caplog):
        """加载失败对同一 room 只 WARNING 一次，worker 不退出。"""
        import logging
        shared = SharedState()
        shared.room_id = "9999"  # 无对应人设文件，且无 default.json
        anchor_cfg = AnchorConfig(enabled=True, profile_dir=str(tmp_path / "empty"))
        cfg = LLMConfig(enabled=True, providers={"doubao": {"api_key": "k"}})
        router = FakeRouter(chat_response='{"lines": ["x"]}')
        q = queue.Queue()
        for _ in range(3):  # 3 次冷场事件
            q.put({"kind": "event", "event": {"event": "cold_start", "ts": 1}})
        q.put(None)
        with caplog.at_level(logging.WARNING, logger="llm"):
            with patch.object(LLMConfig, "build_router", return_value=router):
                L.run_llm_worker(q, cfg, output_dir=str(tmp_path / "out"),
                                 room_id="9999", shared_state=shared,
                                 anchor_cfg=anchor_cfg)
        # 加载失败 WARNING 只出现一次（cached in anchor_failed_room）
        fail_warnings = [r for r in caplog.records if "加载失败" in r.getMessage()]
        assert len(fail_warnings) == 1


# ============================================================ 6. default 兜底
class TestDefaultFallback:
    def test_room_id_not_matched_uses_default(self, tmp_path):
        default = tmp_path / "default.json"
        default.write_text(json.dumps(_make_profile(name="默认主播")), encoding="utf-8")
        profile, err = load_anchor_profile(str(tmp_path), "8888")  # 8888.json 不存在
        assert err is None
        assert profile is not None
        assert profile["name"] == "默认主播"

    def test_all_empty_default_treated_as_no_persona(self, tmp_path):
        default = tmp_path / "default.json"
        default.write_text(json.dumps({"name": "", "category": "", "style": "",
                                       "catchphrases": []}), encoding="utf-8")
        profile, err = load_anchor_profile(str(tmp_path), "8888")
        assert profile is None  # 全空字段 = 无人设
        assert err is None


# ============================================================ 7.5 tag 优先（2026-10-28）
class TestTagFirstLookup:
    """人设按主播 tag 找，room_id 只作兼容。

    为什么必须这样：抖音的 room_id **每场直播都变**（研途 10-05=7693…、10-28=7701…），
    按 room_id 存人设等于每场都要重丢一份文件 —— 这正是「8 路只做了 1 份人设」的真因。
    """

    def test_tag_wins_over_room_id(self, tmp_path):
        _write_profile(tmp_path, "主播甲", _make_profile(name="按tag的"))
        _write_profile(tmp_path, "7001", _make_profile(name="按room_id的"))
        prof, err = load_anchor_profile(str(tmp_path), "7001", "主播甲")
        assert err is None
        assert prof is not None
        assert prof["name"] == "按tag的"

    def test_falls_back_to_room_id_when_tag_missing(self, tmp_path):
        _write_profile(tmp_path, "7001", _make_profile(name="按room_id的"))
        prof, err = load_anchor_profile(str(tmp_path), "7001", "没有这个主播")
        assert err is None
        assert prof["name"] == "按room_id的"

    def test_no_profile_for_this_anchor_is_not_an_error(self, tmp_path):
        """目录在、只是这一路没配人设 → (None, None)，不该刷 WARNING。"""
        _write_profile(tmp_path, "主播甲", _make_profile(name="别人的"))
        prof, err = load_anchor_profile(str(tmp_path), "", "主播乙")
        assert prof is None
        assert err is None

    def test_worker_loads_by_tag_before_room_resolved(self, tmp_path, caplog):
        """room_id 还没解析出来时也要能加载人设（tag 是配置里就有的）。"""
        import logging
        profile_dir = tmp_path / "profiles"
        profile_dir.mkdir()
        _write_profile(profile_dir, "主播甲",
                       _make_profile(name="主播甲", catchphrases=["家人们"]))

        anchor_cfg = AnchorConfig(enabled=True, profile_dir=str(profile_dir))
        cfg = LLMConfig(enabled=True, providers={"doubao": {"api_key": "k"}},
                        coldline_min_interval_seconds=0)
        router = FakeRouter(chat_response='{"lines": ["家人们别走啊"]}')
        q = queue.Queue()
        q.put({"kind": "event", "event": {"event": "cold_start", "ts": 1}})
        q.put(None)
        with caplog.at_level(logging.INFO, logger="llm"):
            with patch.object(LLMConfig, "build_router", return_value=router):
                # shared_state 传 None：模拟 client 还没解析出 room_id
                L.run_llm_worker(q, cfg, output_dir=str(tmp_path / "out"),
                                 room_id="", room_tag="主播甲",
                                 shared_state=None, anchor_cfg=anchor_cfg)

        assert any("已加载人设" in r.getMessage() for r in caplog.records)
        cold_path = tmp_path / "out" / f"coldline_{L.time.strftime('%Y-%m-%d')}.jsonl"
        records = [json.loads(line) for line in cold_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert records and records[0]["anchor_used"] is True


# ============================================================ 7. 多主播隔离
class TestMultiAnchorIsolation:
    def test_two_room_ids_load_own_profile(self, tmp_path):
        _write_profile(tmp_path, "1001", _make_profile(name="主播A"))
        _write_profile(tmp_path, "2002", _make_profile(name="主播B"))
        p1, _ = load_anchor_profile(str(tmp_path), "1001")
        p2, _ = load_anchor_profile(str(tmp_path), "2002")
        assert p1["name"] == "主播A"
        assert p2["name"] == "主播B"

    def test_worker_anchor_name_correct(self, tmp_path):
        _write_profile(tmp_path, "1001", _make_profile(name="主播A"))
        shared = SharedState()
        shared.room_id = "1001"
        anchor_cfg = AnchorConfig(enabled=True, profile_dir=str(tmp_path))
        cfg = LLMConfig(enabled=True, providers={"doubao": {"api_key": "k"}})
        router = FakeRouter(chat_response='{"lines": ["家人们好"]}')
        q = queue.Queue()
        q.put({"kind": "event", "event": {"event": "cold_start", "ts": 1}})
        q.put(None)
        with patch.object(LLMConfig, "build_router", return_value=router):
            L.run_llm_worker(q, cfg, output_dir=str(tmp_path / "out"),
                             room_id="1001", shared_state=shared,
                             anchor_cfg=anchor_cfg)
        cold_path = tmp_path / "out" / f"coldline_{L.time.strftime('%Y-%m-%d')}.jsonl"
        rec = json.loads(cold_path.read_text(encoding="utf-8").splitlines()[0])
        assert rec["anchor_name"] == "主播A"
        assert rec["anchor_used"] is True


# ============================================================ 8. 300 字裁剪
class TestTrimProfile:
    def test_trim_drops_low_priority_fields(self):
        # 构造一个超大 profile（远超 300 字）
        big = _make_profile(
            name="主播" * 50,
            category="游戏" * 50,
            style="搞笑" * 50,
            catchphrases=["家人们" * 20] * 10,
        )
        kept, trimmed = _trim_profile(big, max_chars=300)
        assert trimmed, "应有被裁字段"
        # 序列化后总长度 <= 300
        total = len(json.dumps(kept, ensure_ascii=False))
        assert total <= 300
        # 低优先级字段（forbidden_topics）一定被裁
        assert "forbidden_topics" in trimmed

    def test_catchphrases_truncated_to_three(self):
        big = _make_profile(catchphrases=["p1", "p2", "p3", "p4", "p5"])
        kept, trimmed = _trim_profile(big, max_chars=300)
        assert len(kept.get("catchphrases", [])) <= 3

    def test_small_profile_not_trimmed(self):
        small = _make_profile(name="小", category="游戏", style="温柔",
                              catchphrases=["家人们"])
        kept, trimmed = _trim_profile(small, max_chars=300)
        assert trimmed == []
        assert kept.get("name") == "小"


# ============================================================ 9. room_id 惰性加载
class TestLazyRoomIdLoading:
    def test_shared_state_none_degrades_to_no_persona(self, tmp_path):
        """shared_state 为 None → 人设功能禁用，anchor_used=False。"""
        _write_profile(tmp_path, "1001", _make_profile(name="主播A"))
        anchor_cfg = AnchorConfig(enabled=True, profile_dir=str(tmp_path))
        cfg = LLMConfig(enabled=True, providers={"doubao": {"api_key": "k"}})
        router = FakeRouter(chat_response='{"lines": ["x"]}')
        q = queue.Queue()
        q.put({"kind": "event", "event": {"event": "cold_start", "ts": 1}})
        q.put(None)
        with patch.object(LLMConfig, "build_router", return_value=router):
            L.run_llm_worker(q, cfg, output_dir=str(tmp_path / "out"),
                             room_id="1001", shared_state=None,
                             anchor_cfg=anchor_cfg)
        cold_path = tmp_path / "out" / f"coldline_{L.time.strftime('%Y-%m-%d')}.jsonl"
        rec = json.loads(cold_path.read_text(encoding="utf-8").splitlines()[0])
        assert rec["anchor_used"] is False

    def test_room_id_resolved_after_delay_loads_profile(self, tmp_path):
        """先 None（降级），写入 room_id 后命中 profile。"""
        _write_profile(tmp_path, "1001", _make_profile(name="主播A"))
        shared = SharedState()
        shared.room_id = None  # 初始未解析
        anchor_cfg = AnchorConfig(enabled=True, profile_dir=str(tmp_path))
        cfg = LLMConfig(enabled=True, providers={"doubao": {"api_key": "k"}})
        router = FakeRouter(chat_response='{"lines": ["x"]}')
        q = queue.Queue()
        # 第一轮：room_id=None
        q.put({"kind": "event", "event": {"event": "cold_start", "ts": 1}})
        # 模拟 client 解析到 room_id
        q.put({"kind": "event", "event": {"event": "cold_start", "ts": 2}})
        q.put(None)
        with patch.object(LLMConfig, "build_router", return_value=router):
            L.run_llm_worker(q, cfg, output_dir=str(tmp_path / "out"),
                             room_id="1001", shared_state=shared,
                             anchor_cfg=anchor_cfg)
        cold_path = tmp_path / "out" / f"coldline_{L.time.strftime('%Y-%m-%d')}.jsonl"
        lines = [l for l in cold_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        rec1 = json.loads(lines[0])
        assert rec1["anchor_used"] is False  # room_id 未解析
        # 第二轮前设置 room_id（模拟跨线程写入）
        shared.room_id = "1001"
        # 重跑一次验证命中
        router2 = FakeRouter(chat_response='{"lines": ["x"]}')
        q2 = queue.Queue()
        q2.put({"kind": "event", "event": {"event": "cold_start", "ts": 3}})
        q2.put(None)
        with patch.object(LLMConfig, "build_router", return_value=router2):
            L.run_llm_worker(q2, cfg, output_dir=str(tmp_path / "out2"),
                             room_id="1001", shared_state=shared,
                             anchor_cfg=anchor_cfg)
        cold_path2 = tmp_path / "out2" / f"coldline_{L.time.strftime('%Y-%m-%d')}.jsonl"
        rec3 = json.loads(cold_path2.read_text(encoding="utf-8").splitlines()[0])
        assert rec3["anchor_used"] is True
        assert rec3["anchor_name"] == "主播A"


# ============================================================ 8. 画像空窗熔断（v2.7 任务一）
class TestPortraitMelt:
    """冷场窗口弹幕数 < portrait_min_danmaku 时跳过画像 LLM 调用（0 成本）。
    冷场台词路径不受影响。"""

    _PORTRAIT_RESP = json.dumps({
        "interest_topics": [], "consumption_intent": {"level": "low"},
        "activity_layers": {}, "sentiment": {"positive": 0, "neutral": 0, "negative": 0},
        "suggestions": [],
    })

    def _snap(self, count):
        return {"kind": "snapshot", "snapshot": {
            "ts": 1, "top_words": [],
            "cold_window": {"count": count, "window_seconds": 60,
                            "is_cold": count == 0}}}

    def _run(self, q, cfg, tmp_path, router):
        with patch.object(LLMConfig, "build_router", return_value=router):
            L.run_llm_worker(q, cfg, output_dir=str(tmp_path / "out"),
                             room_id="1001", shared_state=None,
                             anchor_cfg=AnchorConfig())

    def test_melt_zero_danmaku_no_llm_call(self, tmp_path):
        """count=0 阈值=1：连续 3 快照，FakeRouter 调用 0 次，无 portrait 落盘。"""
        cfg = LLMConfig(enabled=True, providers={"doubao": {"api_key": "k"}},
                        portrait_min_danmaku=1)
        router = FakeRouter(chat_response=self._PORTRAIT_RESP)
        q = queue.Queue()
        for _ in range(3):
            q.put(self._snap(0))
        q.put(None)
        self._run(q, cfg, tmp_path, router)
        assert len(router.calls) == 0, "熔断期不应调用 LLM"
        assert list((tmp_path / "out").glob("portrait_*.jsonl")) == []

    def test_melt_threshold_met_normal_call(self, tmp_path):
        """count=1 阈值=1：正常调用并落盘。"""
        cfg = LLMConfig(enabled=True, providers={"doubao": {"api_key": "k"}},
                        portrait_min_danmaku=1)
        router = FakeRouter(chat_response=self._PORTRAIT_RESP)
        q = queue.Queue()
        q.put(self._snap(1))
        q.put(None)
        self._run(q, cfg, tmp_path, router)
        assert len(router.calls) == 1
        files = list((tmp_path / "out").glob("portrait_*.jsonl"))
        assert len(files) == 1

    def test_melt_threshold_zero_regression(self, tmp_path):
        """阈值=0：任意快照都调用，等同旧行为（回归开关）。"""
        cfg = LLMConfig(enabled=True, providers={"doubao": {"api_key": "k"}},
                        portrait_min_danmaku=0)
        router = FakeRouter(chat_response=self._PORTRAIT_RESP)
        q = queue.Queue()
        q.put(self._snap(0))
        q.put(None)
        self._run(q, cfg, tmp_path, router)
        assert len(router.calls) == 1
        assert list((tmp_path / "out").glob("portrait_*.jsonl"))

    def test_melt_does_not_block_coldline(self, tmp_path):
        """熔断期塞 cold_start：画像 0 调用，冷场台词仍生成（路径独立）。"""
        cfg = LLMConfig(enabled=True, providers={"doubao": {"api_key": "k"}},
                        portrait_min_danmaku=1, coldline_min_interval_seconds=0)
        # 画像熔断不调用；唯一 LLM 调用来自冷场台词，返回合法 list
        router = FakeRouter(chat_response='["家人们扣1"]')
        q = queue.Queue()
        q.put(self._snap(0))  # 熔断，但 last_snapshot 已更新
        q.put({"kind": "event", "event": {"event": "cold_start", "ts": 1}})
        q.put(None)
        self._run(q, cfg, tmp_path, router)
        cold_files = list((tmp_path / "out").glob("coldline_*.jsonl"))
        assert len(cold_files) == 1
        rec = json.loads(cold_files[0].read_text(encoding="utf-8").splitlines()[0])
        assert rec["lines"], "冷场台词应正常产出"
        assert list((tmp_path / "out").glob("portrait_*.jsonl")) == [], "熔断期无画像落盘"

    def test_melt_missing_cold_window_field(self, tmp_path):
        """snapshot 缺 cold_window 字段：按 0 处理，不抛异常。"""
        cfg = LLMConfig(enabled=True, providers={"doubao": {"api_key": "k"}},
                        portrait_min_danmaku=1)
        router = FakeRouter(chat_response=self._PORTRAIT_RESP)
        q = queue.Queue()
        q.put({"kind": "snapshot", "snapshot": {"ts": 1, "top_words": []}})
        q.put(None)
        self._run(q, cfg, tmp_path, router)
        assert len(router.calls) == 0
        assert list((tmp_path / "out").glob("portrait_*.jsonl")) == []


# ============================================================ 10. 画像 question_topics + 按房间落盘（v3.6.1）
class TestPortraitQuestionTopics:
    """画像新增 question_topics 字段的归一化（离线，零 API 调用）。"""

    def test_normalize_standard(self):
        norm = PortraitService._normalize({
            "interest_topics": ["就业"],
            "question_topics": [
                {"topic": "简历", "count": 3, "examples": ["简历怎么写", "简历模板"]},
                {"topic": "面试"},
                "转行",
                {"no_topic": True},
                123,
            ],
        }, 123)
        qt = norm["question_topics"]
        assert qt[0] == {"topic": "简历", "count": 3,
                         "examples": ["简历怎么写", "简历模板"]}
        assert qt[1]["topic"] == "面试" and qt[1]["count"] == 0   # count 缺省补 0
        assert any(x["topic"] == "转行" for x in qt)             # 纯字符串兼容
        assert all(x["topic"] for x in qt)                       # 丢弃无 topic 项

    def test_missing_is_empty_list(self):
        norm = PortraitService._normalize({"interest_topics": []}, 1)
        assert norm["question_topics"] == []

    def test_capped_at_ten(self):
        data = {"question_topics": [{"topic": f"T{i}", "count": i} for i in range(20)]}
        assert len(PortraitService._normalize(data, 1)["question_topics"]) == 10


class TestPortraitPerRoomFile:
    """room_tag 非空时画像按房间独立落盘，并写入 room_tag（v3.6.1）。"""

    _RESP = json.dumps({
        "interest_topics": ["错题整理"],
        "question_topics": [{"topic": "错题整理", "count": 1, "examples": ["错题怎么抄"]}],
        "consumption_intent": {"level": "low"},
        "activity_layers": {}, "sentiment": {"positive": 0, "neutral": 1, "negative": 0},
        "suggestions": [],
    })

    def _run(self, q, cfg, tmp_path, router, room_tag):
        with patch.object(LLMConfig, "build_router", return_value=router):
            L.run_llm_worker(q, cfg, output_dir=str(tmp_path / "out"),
                             room_id="34679796879", room_tag=room_tag,
                             shared_state=None, anchor_cfg=AnchorConfig())

    def _snap(self):
        return {"kind": "snapshot", "snapshot": {
            "ts": 1, "top_words": [],
            "cold_window": {"count": 1, "window_seconds": 60, "is_cold": False}}}

    def test_written_per_room_with_tag(self, tmp_path):
        cfg = LLMConfig(enabled=True, providers={"doubao": {"api_key": "k"}})
        router = FakeRouter(chat_response=self._RESP)
        q = queue.Queue()
        q.put(self._snap())
        q.put(None)
        self._run(q, cfg, tmp_path, router, room_tag="教育-辰哥英语")
        today = L.time.strftime("%Y-%m-%d")
        p = tmp_path / "out" / f"portrait_教育-辰哥英语_{today}.jsonl"
        assert p.exists(), "画像应按房间独立落盘"
        rec = json.loads(p.read_text(encoding="utf-8").strip())
        assert rec["room_tag"] == "教育-辰哥英语"
        assert rec["room_id"] == "34679796879"
        assert rec["portrait"]["question_topics"][0]["topic"] == "错题整理"

    def test_global_filename_when_no_tag_regression(self, tmp_path):
        cfg = LLMConfig(enabled=True, providers={"doubao": {"api_key": "k"}})
        router = FakeRouter(chat_response=self._RESP)
        q = queue.Queue()
        q.put(self._snap())
        q.put(None)
        self._run(q, cfg, tmp_path, router, room_tag="")
        today = L.time.strftime("%Y-%m-%d")
        assert (tmp_path / "out" / f"portrait_{today}.jsonl").exists()


class TestColdlineRepeat:
    """冷场**持续中**的补词（2026-10-25 新增 coldline_repeat_seconds）。

    旧行为：cold_start 只在「进入冷场」跃迁时发一次 → 主播整场只看到开头那 3 句，
    之后提示全无（实测研途 16:00 进冷场、全天只有 2 条 coldline）。新行为：
    快照里 is_cold 仍为真且距上组台词够久，就补一组，落盘 event=cold_continue
    （不撑大复盘页按 cold_start 统计的"冷场场次"）。
    """

    _RESP = '["家人们扣1", "刚进来的点点关注"]'

    def _snap(self, count=0, is_cold=True):
        return {"kind": "snapshot", "snapshot": {
            "ts": 1, "top_words": [],
            "cold_window": {"count": count, "window_seconds": 60,
                            "is_cold": is_cold, "threshold_enter": 3.0}}}

    def _run(self, q, cfg, tmp_path, router):
        with patch.object(LLMConfig, "build_router", return_value=router):
            L.run_llm_worker(q, cfg, output_dir=str(tmp_path / "out"),
                             room_id="1001", shared_state=None,
                             anchor_cfg=AnchorConfig())

    def _records(self, tmp_path):
        files = list((tmp_path / "out").glob("coldline_*.jsonl"))
        if not files:
            return []
        return [json.loads(x) for x in
                files[0].read_text(encoding="utf-8").strip().splitlines() if x]

    def test_repeat_fires_when_interval_elapsed(self, tmp_path, monkeypatch):
        """时钟每次读表 +100s、间隔 60s → 连续 3 个冷场快照各补一组。"""
        state = {"t": 1000.0}

        def _tick():
            state["t"] += 100
            return state["t"]
        monkeypatch.setattr(L.time, "time", _tick)

        cfg = LLMConfig(enabled=True, providers={"doubao": {"api_key": "k"}},
                        portrait_min_danmaku=1, coldline_repeat_seconds=60)
        router = FakeRouter(chat_response=self._RESP)
        q = queue.Queue()
        for _ in range(3):
            q.put(self._snap())
        q.put(None)
        self._run(q, cfg, tmp_path, router)

        recs = self._records(tmp_path)
        assert len(recs) == 3
        assert {r["event"] for r in recs} == {"cold_continue"}
        assert all(r["lines"] for r in recs), "每组都该有台词"
        # 画像仍被熔断（count=0 < 1），只有冷场台词在花钱
        assert list((tmp_path / "out").glob("portrait_*.jsonl")) == []

    def test_repeat_skipped_when_too_soon(self, tmp_path, monkeypatch):
        """间隔 1000s 远大于时钟步长 → 只补首组，后续快照跳过。"""
        state = {"t": 1000.0}

        def _tick():
            state["t"] += 100
            return state["t"]
        monkeypatch.setattr(L.time, "time", _tick)

        cfg = LLMConfig(enabled=True, providers={"doubao": {"api_key": "k"}},
                        portrait_min_danmaku=1, coldline_repeat_seconds=1000)
        router = FakeRouter(chat_response=self._RESP)
        q = queue.Queue()
        for _ in range(3):
            q.put(self._snap())
        q.put(None)
        self._run(q, cfg, tmp_path, router)

        assert len(self._records(tmp_path)) == 1
        assert len(router.calls) == 1

    def test_repeat_off_by_default(self, tmp_path):
        """默认 repeat=0 = 关闭：冷场快照不再产生任何台词（旧行为回归）。"""
        cfg = LLMConfig(enabled=True, providers={"doubao": {"api_key": "k"}},
                        portrait_min_danmaku=1)
        assert cfg.coldline_repeat_seconds == 0
        router = FakeRouter(chat_response=self._RESP)
        q = queue.Queue()
        q.put(self._snap())
        q.put(None)
        self._run(q, cfg, tmp_path, router)

        assert router.calls == []
        assert self._records(tmp_path) == []

    def test_no_repeat_when_not_cold(self, tmp_path, monkeypatch):
        """非冷场快照即使间隔够也不补。"""
        state = {"t": 1000.0}

        def _tick():
            state["t"] += 100
            return state["t"]
        monkeypatch.setattr(L.time, "time", _tick)

        cfg = LLMConfig(enabled=True, providers={"doubao": {"api_key": "k"}},
                        portrait_min_danmaku=1, coldline_repeat_seconds=60)
        router = FakeRouter(chat_response=self._RESP)
        q = queue.Queue()
        q.put(self._snap(count=5, is_cold=False))
        q.put(None)
        self._run(q, cfg, tmp_path, router)

        assert self._records(tmp_path) == []


class TestAnchorPromptParity:
    """人设版 system prompt 不能漏掉无人设版的「接观众问题」规则（2026-10-28）。

    背景：10-25 给无人设路径加了「优先接观众正在问的问题」+ `_portrait_block`，
    但人设走的是另一套模板 `_build_coldline_system`，只写了「每条必须含口头禅」
    —— 人设开关一打开，台词又变回「家人们扣1」。
    """

    def test_anchor_system_prioritizes_audience_questions(self):
        text = _build_coldline_system(_make_profile())
        assert "优先接观众正在问的问题" in text
        assert "扣1" in text  # 作为反面例子被点名，而不是被鼓励
        # 规则 1 必须是「接问题」，不能被口头禅挤到后面
        first_rule = [x for x in text.splitlines() if x.startswith("1. ")][0]
        assert "观众" in first_rule

    def test_anchor_system_caps_line_length(self):
        text = _build_coldline_system(_make_profile())
        assert "不超过 45 个字" in text

    def test_clamp_line_cuts_at_punctuation(self):
        """超长台词不能砍出半句话（原来 text[:50] 会把句子砍在第 50 个字上）。"""
        text = "家长们刚进直播间别着急划走，不管你是大二准备保研、大三要考研，" \
               "还是现在还没确定方向，弹幕报上你的本科院校和专业"
        out = L._clamp_line(text)
        assert len(out) <= 50
        assert not out.endswith("本科")          # 不是硬切
        assert out.endswith("方向") or out.endswith("考研")

    def test_clamp_line_hard_cuts_without_punctuation(self):
        out = L._clamp_line("啊" * 80)
        assert len(out) == 50

    def test_clamp_line_keeps_short_line(self):
        assert L._clamp_line("各位家长，这个问题我说清楚。") == "各位家长，这个问题我说清楚。"

    def test_forbidden_rule_numbering_stays_sequential(self):
        text = _build_coldline_system(_make_profile(forbidden_topics=["政治"]))
        nums = [line.split(".", 1)[0] for line in text.splitlines()
                if line[:2].rstrip(".").isdigit() and line[1:3].startswith(". ")]
        assert nums == [str(i) for i in range(1, len(nums) + 1)]

    def test_trimmed_profile_still_keeps_rules(self):
        """300 字裁剪只动人设字段，规则段（含第 1 条）必须完整保留。"""
        inject, _ = _trim_profile(_make_profile(
            style="很长的风格描述" * 40, catchphrases=["甲", "乙", "丙", "丁"]))
        text = _build_coldline_system(inject)
        assert "优先接观众正在问的问题" in text
        assert "只输出JSON" in text
        assert len(inject.get("catchphrases") or []) <= 3


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
