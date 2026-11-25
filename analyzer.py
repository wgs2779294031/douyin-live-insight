"""规则统计层：抖音弹幕实时分析模块（v2 升级版）。

职责：
1. 冷场检测：迟滞双阈值 + 滑动基线自适应。窗口内 danmaku 条数 < 进入阈值
   → 冷场；>= 解除阈值 → 解除；之间维持现状防抖。fixed 模式回退为 v1 单阈值
   语义（enter=exit）便于回归与论文对比。
2. 用户活跃度：按 user_id 维度累计发言总数 + 最近 N 分钟发言数 + 礼物数，
   按加权分 score = recent_danmaku*1.0 + recent_gift*gift_weight 输出 Top N。
   游客 user_id="111111" 跳过活跃度统计（仍计入消息类型分布）。
3. 基础统计：消息类型分布累计、弹幕文本高频词 Top N（简单分词 + 停用词表，
   过滤纯表情/标点/短词）。
4. 代表性弹幕：保留最近 N 条 danmaku（uname/text/ts），不含 user_id。
5. 冷场事件历史：保留最近 20 次 cold_start/cold_end 翻转事件。
6. 时间戳跳跃检测：相邻两条 record |ts 差|>300s 视为回放/直播断档，
   WARNING 后重置所有 deque，避免脏数据污染统计。
7. 输出：冷场/解除事件、活跃快照、统计快照统一走 logging.INFO；
   内部保留实时指标 dict（get_snapshot），供后续 LLM 层 / API 层取用，不落盘。

设计原则：
- 标准库优先（queue/threading/collections/time/re/json/statistics）。
- 分析函数与采集器解耦：DanmuAnalyzer 只吃 record dict + 配置，可独立用于
  回放（replay_jsonl）或 FastAPI 等任意入口，不依赖 client.py / storage.py。
- run_analyzer 为队列消费入口（与采集线程旁路），replay_jsonl 为历史回放入口。
- get_snapshot 只读：不修改任何 deque，剪枝统一在 feed 中执行。

CLI：
    python analyzer.py --replay data/danmu_douyin_2026-09-16.jsonl
    python analyzer.py --replay data/xxx.jsonl --cold-mode fixed      # v1 单阈值回归
    python analyzer.py --replay data/xxx.jsonl --cold-mode adaptive   # 自适应基线
    python analyzer.py --replay data/xxx.jsonl --realtime             # 按记录 ts 节流回放
    python analyzer.py --replay data/xxx.jsonl --snapshot             # 输出最终指标 JSON
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import re
import statistics
import threading
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger("analyzer")

# ================================================================ 配置
# 默认配置路径：与采集器 config.json 同目录共用
DEFAULT_CONFIG_PATH = "config.json"


class AnalyzerConfigError(RuntimeError):
    """规则层配置校验失败。"""


@dataclass
class AnalyzerConfig:
    """规则统计层配置（v2 字段对齐任务书五节）。

    向后兼容：若旧 config.json 残留 `cold_threshold` 字段，from_dict 会自动
    映射为 cold_enter_threshold 并打印 deprecation 警告，方便老配置平滑升级。
    """
    # 冷场滑动窗口大小（秒）
    cold_window_seconds: int = 60
    # 冷场判定模式：
    #   fixed    = 单阈值回归 v1
    #   adaptive = 均值-1.5×标准差 + 硬编码下限 3/6（v2 论文口径，低频直播间会失灵）
    #   relative = 相对自身基线（中位数 × 比例），2026-10-27 新增
    cold_mode: str = "adaptive"
    # 冷场进入阈值：窗口内 danmaku 条数 < 该值 → 冷场
    cold_enter_threshold: int = 3
    # 冷场解除阈值：窗口内 danmaku 条数 >= 该值 → 解除（仅 adaptive 模式生效，
    # fixed 模式下被忽略，thresholds() 返回 (enter, enter) 保持 v1 行为）
    cold_exit_threshold: int = 6
    # ---- relative 模式参数（2026-10-27）----
    # 低于基线的这个比例 → 判冷场；回到基线的这个比例 → 解除。
    # 为什么不用绝对条数：2026-10-26 实测「研途-张老师团队」真实弹幕约 1.5 条/分钟，
    # 而 adaptive 的解除阈值被 max(6.0, ...) 锁死在 6 条/60 秒 —— 它永远达不到，
    # 一段冷场挂了 43 分钟才等到偶然攒够 6 条。
    cold_relative_enter: float = 0.5
    cold_relative_exit: float = 0.8
    # 绝对下限：基线趋近 0（整场没人说话）时不能让阈值也变成 0，
    # 否则「0 条」永远不满足 count < 0，冷场永远不触发。
    cold_absolute_floor: float = 1.0
    # 自适应基线窗口（分钟）：采样该时长内的窗口计数序列做均值/标准差（adaptive）
    # 或中位数（relative）。relative 建议 15，太短会被冷场期自己污染。
    adaptive_baseline_minutes: int = 5
    # 自适应冷启动所需最少样本数：样本不足时回退到固定 (enter, exit) 阈值
    adaptive_min_samples: int = 3
    # adaptive 模式的绝对下限（2026-10-27 从硬编码 3.0/6.0 改成可配）。
    # 为什么：`mean - 1.5σ` 本身是合理的统计口径，但结果被 `max(3.0, ...)` 和
    # `max(6.0, ...)` 兜住，对低频直播间等于退回固定阈值。2026-10-26 实测
    # 「研途-张老师团队」平均 1.02 条/分钟（427 分钟里 224 分钟一条弹幕都没有），
    # 解除阈值 6 条/60 秒永远达不到 → 单段冷场挂了 121 分钟。
    # 降到 0.5/1.0 后：0 条判冷、1 条即解除，与它的真实量级匹配。
    cold_floor_enter: float = 0.5
    cold_floor_exit: float = 1.0
    # 冷场最短时长（秒）：短于它的冷场段**不落盘、不计入复盘**（2026-10-27）。
    # 阈值降到 0.5/1.0 后会天然产生大量「1 条弹幕进来就解除」的瞬时抖动
    # （2026-10-26 研途回放 57 段里有 38 段 < 30 秒）。判定照旧，只是不记。
    cold_min_duration_seconds: int = 30
    # 用户活跃度统计窗口（分钟）
    activity_window_minutes: int = 10
    # 礼物活跃度加权系数：score = recent_danmaku*1.0 + recent_gift*gift_weight
    gift_weight: float = 2.0
    # 活跃用户 Top N
    active_top_n: int = 10
    # 统计 / 活跃快照输出间隔（秒）
    snapshot_interval_seconds: int = 60
    # 高频词 Top N
    top_words_n: int = 20
    # 代表性弹幕保留条数（最近 N 条 danmaku，不含 user_id）
    recent_danmaku_maxlen: int = 200
    # 停用词文件路径（可选，每行一个词；空则仅使用内置停用词表）
    stopwords_file: str = ""

    def __post_init__(self) -> None:
        # 校验 cold_mode
        if self.cold_mode not in ("fixed", "adaptive", "relative"):
            raise AnalyzerConfigError(
                f"cold_mode 必须为 fixed / adaptive / relative，当前值={self.cold_mode!r}")
        # 解除阈值必须大于进入阈值（迟滞区间非负）
        if self.cold_exit_threshold <= self.cold_enter_threshold:
            raise AnalyzerConfigError(
                f"cold_exit_threshold({self.cold_exit_threshold}) 必须大于 "
                f"cold_enter_threshold({self.cold_enter_threshold})")
        # relative 模式的比例必须 ordered 且为正
        if self.cold_relative_enter <= 0 or self.cold_relative_exit <= 0:
            raise AnalyzerConfigError(
                f"cold_relative_enter/exit 必须 > 0，当前 "
                f"{self.cold_relative_enter}/{self.cold_relative_exit}")
        if self.cold_relative_exit <= self.cold_relative_enter:
            raise AnalyzerConfigError(
                f"cold_relative_exit({self.cold_relative_exit}) 必须大于 "
                f"cold_relative_enter({self.cold_relative_enter})")
        # 自适应最小样本数至少 1（statistics.stdev 需 >=2，但 cold_start 阶段
        # 我们用 <min_samples 判断，min_samples>=3 才能保证 stdev 不报错）
        if self.adaptive_min_samples < 1:
            raise AnalyzerConfigError(
                f"adaptive_min_samples 必须 >=1，当前值={self.adaptive_min_samples}")
        # 数值非负校验
        for name in ("cold_window_seconds", "activity_window_minutes",
                     "snapshot_interval_seconds", "top_words_n",
                     "recent_danmaku_maxlen", "adaptive_baseline_minutes"):
            if getattr(self, name) < 0:
                raise AnalyzerConfigError(f"{name} 不能为负数，当前值={getattr(self, name)}")
        if self.gift_weight < 0:
            raise AnalyzerConfigError(f"gift_weight 不能为负数，当前值={self.gift_weight}")

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "AnalyzerConfig":
        """从 dict 加载：筛选已知字段 + cold_threshold 兼容映射。

        若 raw 中存在旧字段 `cold_threshold` 而无 `cold_enter_threshold`，
        自动把 cold_threshold 映射为 cold_enter_threshold 并打 deprecation 警告，
        保证 v1 配置文件无需改动即可平滑升级到 v2。
        """
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        # 兼容读法：旧 cold_threshold 自动映射为 cold_enter_threshold
        if "cold_threshold" in raw and "cold_enter_threshold" not in raw:
            legacy_val = raw["cold_threshold"]
            logger.warning(
                "[deprecation] 检测到旧字段 cold_threshold=%s，已映射为 cold_enter_threshold；"
                "建议改用新字段名（fixed 模式下 exit 字段被忽略，回退单阈值语义）",
                legacy_val,
            )
            known["cold_enter_threshold"] = legacy_val
        return cls(**known)


def load_analyzer_config(path: str = DEFAULT_CONFIG_PATH) -> AnalyzerConfig:
    """读取 config.json 中的规则层字段；文件不存在或字段缺失则用默认值。

    加载方式仿照 config.py：先读 JSON，再筛选已知字段，缺省走 dataclass 默认值。
    独立读取而不复用 config.load_config：避免污染采集器 Config 的字段定义，
    也满足"analyzer 不依赖采集器内部对象"的验收要求。
    """
    if not os.path.exists(path):
        return AnalyzerConfig()
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f) or {}
    except Exception as e:
        logger.warning("读取规则层配置失败，使用默认值: %s", e)
        return AnalyzerConfig()
    return AnalyzerConfig.from_dict(raw)


# ================================================================ 停用词
# 内置停用词：直播间高频但无信息量的词，分词后过滤掉避免污染 Top 词频
_STOPWORDS_DEFAULT: frozenset = frozenset({
    "主播", "直播", "一下", "这个", "那个", "什么", "怎么", "我们", "你们",
    "他们", "可以", "没有", "不是", "就是", "怎么", "出来", "过来", "回去",
    "哈哈", "哈哈哈", "哈哈哈哈", "666", "233", "2333", "111", "222", "333",
    "好的", "可以", "知道", "觉得", "感觉", "现在", "已经", "时间", "今天",
    "昨天", "明天", "觉得", "看到", "看到", "刚才", "其实", "一下", "看了",
})


def load_stopwords(cfg: AnalyzerConfig) -> frozenset:
    """加载停用词表：内置 + 可选外部文件（每行一个词）。

    文件不存在或读取失败时仅用内置停用词表并 warning，不抛异常。
    """
    words = set(_STOPWORDS_DEFAULT)
    if cfg.stopwords_file:
        if not os.path.exists(cfg.stopwords_file):
            logger.warning("停用词文件不存在，仅使用内置停用词表: %s", cfg.stopwords_file)
        else:
            try:
                with open(cfg.stopwords_file, "r", encoding="utf-8") as f:
                    for line in f:
                        w = line.strip()
                        if w:
                            words.add(w)
                logger.info("加载停用词文件成功，合计 %s 个停用词: %s",
                            len(words), cfg.stopwords_file)
            except Exception as e:
                logger.warning("读取停用词文件失败，仅使用内置停用词表: %s (%s)",
                               cfg.stopwords_file, e)
    return frozenset(words)


# ================================================================ 高频词分词
# 简单分词：按空白 / 中英文标点切分。不引入 jieba，对齐任务书"简单分词"要求。
# 过滤规则（任务书 3.3）：纯表情 [xxx] 形式、纯标点、长度 < 2 的字词、停用词。
_TOKEN_SPLIT_RE = re.compile(r"[\s，。！？!?.,;；:：、~～\-—\(\)（）【】「」\"'`]+")
# 整段表情符号 [doge] [微笑] 等：先剥离再做分词，避免把"捂脸"拆出来当词
_EMOTE_INLINE_RE = re.compile(r"\[[^\[\]]*\]")
# 纯标点（含纯符号组合）
_PUNCT_RE = re.compile(r"^[\W_]+$", re.UNICODE)


def tokenize(text: str, stopwords: Optional[frozenset] = None) -> List[str]:
    """对一条弹幕文本做简单分词，返回过滤后的 token 列表。

    :param text: 弹幕原文
    :param stopwords: 停用词集合；为 None 时不过滤停用词（仅过滤表情/标点/短词）
    """
    if not text:
        return []
    # 先剥离所有 [xxx] 表情符号整段，再切词
    text = _EMOTE_INLINE_RE.sub(" ", text)
    out: List[str] = []
    for tok in _TOKEN_SPLIT_RE.split(text):
        if not tok or len(tok) < 2:
            continue
        if _PUNCT_RE.match(tok):
            continue
        if stopwords and tok in stopwords:
            continue
        out.append(tok)
    return out


# ================================================================ 冷场检测器
class ColdDetector:
    """冷场阈值计算抽象基类。

    thresholds(analyzer) 返回 (enter_threshold, exit_threshold)：
    - 当窗口内 danmaku 条数 < enter_threshold → 判定进入冷场
    - 当窗口内 danmaku 条数 >= exit_threshold → 判定解除冷场
    - 之间维持现状（迟滞防抖）
    """

    def thresholds(self, analyzer: "DanmuAnalyzer") -> Tuple[float, float]:
        raise NotImplementedError


class FixedThresholdDetector(ColdDetector):
    """固定阈值检测器（v1 单阈值语义回归）。

    thresholds() 返回 (cold_enter_threshold, cold_enter_threshold) 即单阈值
    语义：count < enter → 冷场，count >= enter → 解除。
    cold_exit_threshold 在 fixed 模式下被忽略。

    设计原因：v2 任务书要求 fixed 模式回归与 v1 完全一致。v1 的 cold_threshold
    是单阈值，count<threshold 进入、count>=threshold 解除。为保持完全一致的
    行为，fixed 模式不应用迟滞，exit 字段仅用于 adaptive 模式与配置校验。
    """

    def __init__(self, enter: int) -> None:
        self._enter = float(enter)

    def thresholds(self, analyzer: "DanmuAnalyzer") -> Tuple[float, float]:
        return (self._enter, self._enter)


class AdaptiveBaselineDetector(ColdDetector):
    """自适应基线检测器（论文核心创新点）。

    冷启动阶段（len(rate_history) < adaptive_min_samples）：
        返回 (cold_enter_threshold, cold_exit_threshold) 固定值
    样本充足后：
        基于 rate_history 的均值/标准差计算自适应阈值：
            adaptive = max(3, mean - 1.5 * stdev)
            return (adaptive, max(6, adaptive * 1.5))
        绝对下限保护避免误判极端冷场，3/6 与默认配置一致。

    rate_history 由 DanmuAnalyzer._maybe_sample_rate 采样填充，
    每 60s 取一次当前窗口内 danmaku 计数，maxlen=adaptive_baseline_minutes。
    """

    def __init__(self, enter: int, exit_t: int, min_samples: int,
                 floor_enter: float = 3.0, floor_exit: float = 6.0) -> None:
        self._enter = float(enter)
        self._exit = float(exit_t)
        self._min_samples = int(min_samples)
        self._floor_enter = float(floor_enter)
        self._floor_exit = float(floor_exit)

    def thresholds(self, analyzer: "DanmuAnalyzer") -> Tuple[float, float]:
        rate_history = analyzer._rate_history
        if len(rate_history) < self._min_samples:
            # 冷启动：返回固定阈值（与默认配置一致）
            return (self._enter, self._exit)
        mean_val = statistics.fmean(rate_history)
        stdev_val = statistics.pstdev(rate_history)  # 总体标准差，单实例样本
        adaptive = max(self._floor_enter, mean_val - 1.5 * stdev_val)
        exit_adaptive = max(self._floor_exit, adaptive * 1.5)
        return (adaptive, exit_adaptive)


class RelativeBaselineDetector(ColdDetector):
    """相对自身基线检测器（2026-10-27 新增，解决低频直播间失灵）。

    为什么需要
    ----------
    `AdaptiveBaselineDetector` 的自适应结果被两个**硬编码绝对下限**兜住：
    `max(3.0, ...)` 与 `max(6.0, ...)`。对互动量大的直播间没问题，但对
    「研途-张老师团队」这种真实弹幕约 1.5 条/分钟的场次：

    - 进入阈值被抬到 3 → 几乎每个 60 秒窗口都 < 3 → 一开播就判冷场；
    - 解除阈值被抬到 6 → 它平均 1.5 条，**永远达不到**；
    - 结果：2026-10-26 一场 20 段冷场累计 206 分钟，最长一段挂了 43 分钟。

    判定方式
    --------
    取最近 `adaptive_baseline_minutes` 个采样点的**中位数**作为基线
    （中位数不被进人潮/爆点拉高，也不像均值那样被冷场期自己带偏）：

        enter = max(cold_absolute_floor, baseline * cold_relative_enter)
        exit  = max(enter + 0.5,        baseline * cold_relative_exit)

    默认 0.5 / 0.8：掉到平常一半算冷，回到八成算解除。
    绝对下限 `cold_absolute_floor` 防止基线趋 0 时「0 条 < 0」恒假（冷场永不触发）。

    冷启动（样本 < adaptive_min_samples）沿用配置里的固定 enter/exit，
    保证开播头几分钟仍有判定。
    """

    def __init__(self, enter_ratio: float, exit_ratio: float, min_samples: int,
                 floor: float) -> None:
        self._enter_ratio = float(enter_ratio)
        self._exit_ratio = float(exit_ratio)
        self._min_samples = int(min_samples)
        self._floor = float(floor)

    def thresholds(self, analyzer: "DanmuAnalyzer") -> Tuple[float, float]:
        rate_history = analyzer._rate_history
        if len(rate_history) < self._min_samples:
            return (float(analyzer.cfg.cold_enter_threshold),
                    float(analyzer.cfg.cold_exit_threshold))
        baseline = float(statistics.median(rate_history))
        enter = max(self._floor, baseline * self._enter_ratio)
        exit_t = max(enter + 0.5, baseline * self._exit_ratio)
        return (enter, exit_t)


# ================================================================ 核心分析器
class DanmuAnalyzer:
    """弹幕规则分析器（v2）。

    时间基准：调用方传入 now_ts（epoch 秒）推进逻辑。
    - feed(record) 内部用 record.ts 作为 now_ts，更新窗口/活跃/词频，并触发冷场判定。
    - tick(now_ts) 仅推进冷场状态机，不更新统计；用于无新消息时也能检测冷场。

    线程安全：内部状态未加锁，单线程消费。多线程访问需通过 get_snapshot 外加锁
    或仅由消费线程持有该实例。

    回调：on_event(event_dict) 在冷场状态翻转时同步调用，异常被 try/except
    包住避免影响主流程。main.py 不注册回调，仅 replay_jsonl / 测试场景使用。
    """

    # 抖音弹幕中游客 user_id 固定为 "111111"（系统匿名占位），不参与活跃度统计
    _GUEST_USER_ID = "111111"
    # 时间戳跳跃阈值：相邻两条 record |ts 差| > 该值视为断档
    _TS_JUMP_THRESHOLD = 300.0

    def __init__(self, cfg: AnalyzerConfig,
                 on_event: Optional[Callable[[Dict[str, Any]], None]] = None) -> None:
        self.cfg = cfg
        self._on_event = on_event
        # 停用词表（启动时加载一次，feed 内查询用）
        self._stopwords: frozenset = load_stopwords(cfg)

        # 冷场检测器：根据 cold_mode 选择 fixed / adaptive
        if cfg.cold_mode == "fixed":
            self._detector: ColdDetector = FixedThresholdDetector(cfg.cold_enter_threshold)
        elif cfg.cold_mode == "adaptive":
            self._detector = AdaptiveBaselineDetector(
                cfg.cold_enter_threshold, cfg.cold_exit_threshold, cfg.adaptive_min_samples,
                floor_enter=cfg.cold_floor_enter, floor_exit=cfg.cold_floor_exit)
        elif cfg.cold_mode == "relative":
            self._detector = RelativeBaselineDetector(
                cfg.cold_relative_enter, cfg.cold_relative_exit,
                cfg.adaptive_min_samples, cfg.cold_absolute_floor)
        else:  # __post_init__ 已校验，兜底防御
            raise AnalyzerConfigError(f"未知 cold_mode={cfg.cold_mode!r}")

        # 近 cold_window_seconds 内 danmaku 的 ts 列表（用于冷场计数）
        self._danmaku_ts: Deque[int] = deque()
        # 自适应基线采样历史：每 60s 采一次窗口内 danmaku 计数
        # maxlen=adaptive_baseline_minutes 即 5 个样本 = 5 分钟
        rate_maxlen = max(cfg.adaptive_baseline_minutes, 2)
        self._rate_history: Deque[int] = deque(maxlen=rate_maxlen)
        # 上次采样时间（epoch 秒）
        self._last_rate_sample_ts: float = 0.0

        # 消息类型累计计数
        self._type_counts: Counter = Counter()
        # 每个 user_id 累计发言总数（仅 danmaku 计入"发言"）
        self._user_total: Dict[str, int] = defaultdict(int)
        # 每个 user_id 最近发言 ts 列表（用于最近 N 分钟计数），普通 dict 不自动创建
        self._user_recent: Dict[str, Deque[int]] = {}
        # 每个 user_id 最近送礼 ts 列表（用于最近 N 分钟礼物计数）
        self._user_gift_recent: Dict[str, Deque[int]] = {}
        # user_id → 最新 uname（兜底展示）
        self._user_uname: Dict[str, str] = {}
        # 词频累计
        self._word_counts: Counter = Counter()

        # 代表性弹幕：最近 N 条 danmaku（不含 user_id），用于 LLM 层取上下文
        self._recent_danmaku: Deque[Dict[str, Any]] = deque(maxlen=cfg.recent_danmaku_maxlen)
        # 冷场事件历史：最近 20 次 cold_start/cold_end 翻转
        self._cold_events: Deque[Dict[str, Any]] = deque(maxlen=20)

        # 当前冷场状态（初始未知 None，等首次判定后置 True/False）
        self._is_cold: Optional[bool] = None
        # 上次快照输出的时间（epoch 秒）
        self._last_snapshot_ts: float = 0.0
        # 上次推进的时间（用于快照间隔判断）
        self._last_tick_ts: float = 0.0
        # 上次喂入的 record ts（用于时间戳跳跃检测）
        self._last_record_ts: float = 0.0
        # 窗口起点（首条 record 或最近一次 ts_jump 重置后的 ts）
        self._window_start_ts: float = 0.0
        # 累计喂入记录总数
        self.total_records: int = 0
        # 周期剪枝计数器（每 100 条全量剪枝一次）
        self._prune_counter: int = 0

    # ----- 时间戳跳跃检测 -----
    def _detect_ts_jump(self, ts: float) -> None:
        """检测 record 间时间戳跳跃，超过阈值则 WARNING 并重置窗口 deque。

        用于回放/直播断档场景：避免陈旧数据污染后续统计。
        在 feed 开头调用，先检测再更新 _last_record_ts。

        重置范围（对齐任务书 9 节，仅清空"窗口"类 deque）：
        - _danmaku_ts（冷场窗口）
        - _user_recent（活跃窗口-弹幕）
        - _user_gift_recent（活跃窗口-礼物）
        - _rate_history（速率历史）

        保留：
        - _is_cold 状态机延续（避免 fixed 模式回归时产生额外 cold_start）
        - _cold_events 事件历史（LLM 层需要冷场翻转历史作上下文）
        - _recent_danmaku 代表性弹幕（maxlen=200 自然滚动，不强制清空）
        - _last_snapshot_ts 节奏延续（避免清空后立刻刷屏）
        """
        if not self._last_record_ts:
            # 首条 record：仅记录起点，不判定
            return
        diff = abs(ts - self._last_record_ts)
        if diff > self._TS_JUMP_THRESHOLD:
            logger.warning(
                "[时间戳跳跃] |ts 差|=%.1fs（>%ss），重置窗口 deque ts=%s",
                diff, int(self._TS_JUMP_THRESHOLD), int(ts),
            )
            self._danmaku_ts.clear()
            self._user_recent.clear()
            self._user_gift_recent.clear()
            self._rate_history.clear()
            # 速率采样基准重置，让跳跃后立即采一次新基线
            self._last_rate_sample_ts = 0.0
            # 窗口起点重置为跳跃后时间，duration_sec 重新计算
            self._window_start_ts = ts

    # ----- 剪枝 -----
    def _prune_cold_window(self, now_ts: float) -> None:
        """弹出冷场窗口外的 danmaku ts。"""
        cutoff = now_ts - self.cfg.cold_window_seconds
        while self._danmaku_ts and self._danmaku_ts[0] < cutoff:
            self._danmaku_ts.popleft()

    def _prune_user(self, uid: str, now_ts: float) -> None:
        """弹出指定用户活跃窗口外的 ts。"""
        cutoff = now_ts - self.cfg.activity_window_minutes * 60
        recent = self._user_recent.get(uid)
        if recent:
            while recent and recent[0] < cutoff:
                recent.popleft()
        gift = self._user_gift_recent.get(uid)
        if gift:
            while gift and gift[0] < cutoff:
                gift.popleft()

    def _prune_all_users(self, now_ts: float) -> None:
        """全量剪枝：遍历所有 user_id，弹出活跃窗口外的 ts。

        由 feed 周期触发（每 100 条），避免单个用户 deque 无限增长。
        """
        cutoff = now_ts - self.cfg.activity_window_minutes * 60
        for uid, recent in list(self._user_recent.items()):
            while recent and recent[0] < cutoff:
                recent.popleft()
            if not recent:
                # 已无活跃记录，清理避免 dict 无限增长
                self._user_recent.pop(uid, None)
                self._user_uname.pop(uid, None)
                # _user_total 保留（历史发言总数仍有意义）；gift 一并清理
                gift = self._user_gift_recent.pop(uid, None)
                if not self._user_total.get(uid) and not gift:
                    self._user_total.pop(uid, None)
        for uid, gift in list(self._user_gift_recent.items()):
            while gift and gift[0] < cutoff:
                gift.popleft()
            if not gift and uid not in self._user_recent:
                self._user_gift_recent.pop(uid, None)

    # ----- 自适应基线采样 -----
    def _log_adaptive_threshold(self) -> None:
        """输出 [自适应] 日志：样本数、均值、标准差、当前阈值。"""
        rate_history = self._rate_history
        mean_val = statistics.fmean(rate_history)
        stdev_val = statistics.pstdev(rate_history)
        enter_t, exit_t = self._detector.thresholds(self)
        logger.info(
            "[自适应] 样本=%s mean=%.2f stdev=%.2f 阈值(进入=%.2f 解除=%.2f)",
            len(rate_history), mean_val, stdev_val, enter_t, exit_t,
        )

    def _maybe_sample_rate(self, now_ts: float) -> None:
        """每 60s 采样一次当前窗口内 danmaku 计数，加入 rate_history。

        自适应模式下采样后若样本数足够，输出 [自适应] 日志（mean/stdev/阈值）。
        """
        if self._last_rate_sample_ts and now_ts - self._last_rate_sample_ts < 60:
            return
        # 采样前先剪枝，保证取的是当前窗口的计数
        self._prune_cold_window(now_ts)
        self._rate_history.append(len(self._danmaku_ts))
        self._last_rate_sample_ts = now_ts
        # 仅 adaptive / relative 模式 + 样本充足时输出 [自适应] 日志
        if (self.cfg.cold_mode in ("adaptive", "relative")
                and len(self._rate_history) >= self.cfg.adaptive_min_samples):
            self._log_adaptive_threshold()

    # ----- 冷场检测 -----
    def _check_cold(self, now_ts: float) -> None:
        """根据当前窗口内 danmaku 条数判定冷场状态变化，输出事件。

        迟滞双阈值：
        - count < enter_threshold → 状态置 True（冷场）
        - count >= exit_threshold → 状态置 False（解除）
        - 之间维持现状（防抖）
        fixed 模式下 enter=exit，回退到 v1 单阈值语义。
        """
        self._prune_cold_window(now_ts)
        count = len(self._danmaku_ts)
        enter_t, exit_t = self._detector.thresholds(self)
        if count < enter_t:
            if self._is_cold is not True:
                # None → True（首次判定为冷场）或 False → True（由正常进入冷场）
                logger.info(
                    "[冷场] 近%ss 弹幕仅 %s 条（进入阈值 %s），直播间进入冷场状态 ts=%s",
                    self.cfg.cold_window_seconds, count, enter_t, int(now_ts),
                )
                self._is_cold = True
                self._record_cold_event("cold_start", now_ts, count, enter_t)
        elif count >= exit_t:
            if self._is_cold is True:
                logger.info(
                    "[冷场解除] 近%ss 弹幕 %s 条（解除阈值 %s），冷场结束 ts=%s",
                    self.cfg.cold_window_seconds, count, exit_t, int(now_ts),
                )
                self._is_cold = False
                self._record_cold_event("cold_end", now_ts, count, exit_t)
            elif self._is_cold is None:
                # 首次判定即正常，不输出事件，仅置状态
                self._is_cold = False
        # 之间维持现状（迟滞防抖，不输出事件）

    def _record_cold_event(self, event_name: str, now_ts: float,
                           count: int, threshold: float) -> None:
        """记录冷场翻转事件到 history，并触发回调。"""
        event = {
            "event": event_name,
            "ts": int(now_ts),
            "count": int(count),
            "threshold": float(threshold),
        }
        self._cold_events.append(event)
        self._fire_event(event)

    def _fire_event(self, event: Dict[str, Any]) -> None:
        """触发外部回调；异常被 try/except 包住避免影响主流程。"""
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception as e:
            logger.warning("冷场事件回调抛异常，已忽略: %s event=%r", e, event)

    def tick(self, now_ts: float) -> None:
        """推进时间（无新消息时调用），仅触发冷场判定。

        主要用于实时模式 queue.get 超时或回放模式两条 record 之间的时间跳跃。
        tick 不更新统计（不影响 rate_history 采样、不增加 total_records），
        但会推进冷场状态机与快照输出。
        """
        # tick 也算一次"时间流逝"，参与 rate_history 采样（避免长时间无消息时
        # adaptive 阈值不更新）
        self._maybe_sample_rate(now_ts)
        self._check_cold(now_ts)
        self._last_tick_ts = now_ts
        self._maybe_snapshot(now_ts)

    # ----- 主喂入入口 -----
    def feed(self, record: Dict[str, Any]) -> None:
        """喂入一条 record，更新所有统计并触发冷场判定。"""
        ts = int(record.get("ts") or 0)
        msg_type = record.get("type") or ""
        # 兼容 B站（user_hash）与抖音（user_id）双字段；user_hash 本身为匿名哈希，符合匿名化红线
        user_id = str(record.get("user_id") or record.get("user_hash") or "")
        uname = record.get("uname") or ""
        text = record.get("text") or ""

        # 1. 时间戳跳跃检测（在更新任何状态前）
        self._detect_ts_jump(ts)
        self._last_record_ts = ts
        if self._window_start_ts == 0.0:
            self._window_start_ts = ts

        # 2. 基础计数（消息类型分布始终计入，无论是否游客）
        self.total_records += 1
        self._type_counts[msg_type] += 1

        # 3. 游客 111111 跳过活跃度统计（仍计入消息类型分布）
        if user_id == self._GUEST_USER_ID:
            user_id = ""

        # 4. 仅 danmaku 计入冷场窗口、用户发言、词频、代表性弹幕
        if msg_type == "danmaku":
            self._danmaku_ts.append(ts)
            # 代表性弹幕（不含 user_id，供 LLM 层取上下文）
            self._recent_danmaku.append({
                "uname": uname,
                "text": text,
                "ts": ts,
            })
            if user_id:
                self._user_total[user_id] += 1
                # 普通字典，避免 defaultdict 自动创建空条目
                if user_id not in self._user_recent:
                    self._user_recent[user_id] = deque()
                self._user_recent[user_id].append(ts)
                self._prune_user(user_id, ts)
                if uname:
                    self._user_uname[user_id] = uname
            for tok in tokenize(text, self._stopwords):
                self._word_counts[tok] += 1

        # 5. 礼物计入活跃度（type=gift，按 user_id 维度）
        if msg_type == "gift" and user_id:
            if user_id not in self._user_gift_recent:
                self._user_gift_recent[user_id] = deque()
            self._user_gift_recent[user_id].append(ts)
            self._prune_user(user_id, ts)
            if uname:
                self._user_uname[user_id] = uname

        # 6. 自适应基线采样（每次 feed 都尝试，60s 内只采一次）
        self._maybe_sample_rate(ts)

        # 7. 推进冷场状态（用 record.ts 作为时间基准）
        self._check_cold(ts)
        self._last_tick_ts = ts
        self._maybe_snapshot(ts)

        # 8. 周期剪枝（每 100 条全量剪枝一次，避免 deque 无限增长）
        self._prune_counter += 1
        if self._prune_counter >= 100:
            self._prune_counter = 0
            self._prune_all_users(ts)

    # ----- 快照输出 -----
    def _maybe_snapshot(self, now_ts: float) -> None:
        """按 snapshot_interval_seconds 周期输出活跃快照 + 统计快照。"""
        if self._last_snapshot_ts == 0.0:
            # 首次：记录起点但不立即输出（避免启动瞬间刷屏）
            self._last_snapshot_ts = now_ts
            return
        if now_ts - self._last_snapshot_ts < self.cfg.snapshot_interval_seconds:
            return
        self._log_activity_snapshot(now_ts)
        self._log_stats_snapshot(now_ts)
        self._last_snapshot_ts = now_ts

    def _log_activity_snapshot(self, now_ts: float) -> None:
        """输出活跃用户 Top N 到日志（含 score / gift_count）。"""
        top = self._compute_active_top(now_ts)
        if not top:
            logger.info("[活跃快照] 近%s分钟无活跃用户 ts=%s",
                        self.cfg.activity_window_minutes, int(now_ts))
            return
        parts = [
            f"{i+1}.{u['uname'] or u['user_id']}"
            f"(发言{u['recent_count']}/礼{u['gift_count']}/分{u['score']:.1f}/总{u['total_count']})"
            for i, u in enumerate(top)
        ]
        logger.info("[活跃快照] 近%s分钟 Top%s: %s ts=%s",
                    self.cfg.activity_window_minutes, len(top), " ".join(parts), int(now_ts))

    def _log_stats_snapshot(self, now_ts: float) -> None:
        """输出消息类型分布 + 高频词到日志。"""
        dist = " ".join(f"{k}={v}" for k, v in sorted(self._type_counts.items()))
        top_words = self._word_counts.most_common(self.cfg.top_words_n)
        words_str = " ".join(f"{w}({c})" for w, c in top_words)
        logger.info("[统计快照] 类型分布: %s | 高频词Top%s: %s | 累计记录=%s ts=%s",
                    dist or "(无)", self.cfg.top_words_n, words_str or "(无)",
                    self.total_records, int(now_ts))

    # ----- 计算（供 get_snapshot / 日志共用，只读） -----
    def _compute_active_top(self, now_ts: float) -> List[Dict[str, Any]]:
        """计算当前活跃用户 Top N（按加权 score 排序），只读不改 deque。

        score = recent_danmaku * 1.0 + recent_gift * gift_weight
        其中 recent_* 为最近 activity_window_minutes 内的计数。
        """
        cutoff = now_ts - self.cfg.activity_window_minutes * 60
        rows: List[Dict[str, Any]] = []
        # 合并 danmaku 用户和 gift 用户的 uid 集合
        all_uids = set(self._user_recent.keys()) | set(self._user_gift_recent.keys())
        for uid in all_uids:
            recent_deque = self._user_recent.get(uid)
            # 只读统计：不修改 deque，用 sum(1 for t in recent if t >= cutoff)
            recent_count = sum(1 for t in (recent_deque or ()) if t >= cutoff)
            gift_deque = self._user_gift_recent.get(uid)
            gift_count = sum(1 for t in (gift_deque or ()) if t >= cutoff)
            if recent_count == 0 and gift_count == 0:
                continue
            total_count = self._user_total.get(uid, 0)
            score = recent_count * 1.0 + gift_count * self.cfg.gift_weight
            rows.append({
                "user_id": uid,
                "uname": self._user_uname.get(uid, ""),
                "recent_count": recent_count,
                "gift_count": gift_count,
                "score": round(score, 2),
                "total_count": total_count,
            })
        rows.sort(key=lambda r: (r["score"], r["total_count"]), reverse=True)
        return rows[: self.cfg.active_top_n]

    def get_snapshot(self) -> Dict[str, Any]:
        """返回当前实时指标 dict（只读，供 LLM 层 / API 层取用，不落盘）。

        重要：本方法不调用任何 _prune_* / clear / append，完全只读。
        剪枝统一在 feed 中执行，保证调用方在任意线程读取都不会影响内部状态。
        """
        now_ts = self._last_tick_ts or time.time()
        # 冷场计数只读统计：不调 _prune_cold_window，直接 sum 窗口内 ts
        cold_cutoff = now_ts - self.cfg.cold_window_seconds
        cold_count = sum(1 for t in self._danmaku_ts if t >= cold_cutoff)
        # 阈值取最近一次 detector 计算结果（仅 adaptive 模式有意义）
        enter_t, exit_t = self._detector.thresholds(self)
        top = self._compute_active_top(now_ts)
        top_words = self._word_counts.most_common(self.cfg.top_words_n)
        # 代表性弹幕附 age_sec（相对 now_ts 的年龄，便于 LLM 判断时效）
        rep_danmaku = [
            {
                "uname": d["uname"],
                "text": d["text"],
                "ts": d["ts"],
                "age_sec": max(0, int(now_ts - d["ts"])),
            }
            for d in self._recent_danmaku
        ]
        # 取最近 50 条代表性弹幕（任务书要求）
        if len(rep_danmaku) > 50:
            # 取末尾 50 条（最新的）
            rep_danmaku = rep_danmaku[-50:]
        return {
            "ts": int(now_ts),
            "window": {
                "start_ts": int(self._window_start_ts) if self._window_start_ts else 0,
                "end_ts": int(now_ts),
                "duration_sec": int(now_ts - self._window_start_ts) if self._window_start_ts else 0,
            },
            "cold_window": {
                "is_cold": bool(self._is_cold) if self._is_cold is not None else False,
                "count": cold_count,
                "threshold_enter": enter_t,
                "threshold_exit": exit_t,
                "window_seconds": self.cfg.cold_window_seconds,
                "mode": self.cfg.cold_mode,
            },
            "cold_events": list(self._cold_events),
            "active_top": top,
            "type_distribution": dict(self._type_counts),
            "top_words": [{"word": w, "count": c} for w, c in top_words],
            "representative_danmaku": rep_danmaku,
            "total_records": self.total_records,
        }


# ================================================================ 队列消费入口（实时旁路）
def run_analyzer(q: "queue.Queue", cfg: AnalyzerConfig,
                 stop_event: Optional[threading.Event] = None,
                 on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
                 on_snapshot: Optional[Callable[[Dict[str, Any]], None]] = None,
                 snapshot_interval_seconds: int = 60) -> None:
    """独立线程消费采集器推入的 record 队列，运行规则分析。

    :param q: queue.Queue 实例，maxsize 由调用方设定；收到 None 视为退出哨兵
    :param cfg: AnalyzerConfig
    :param stop_event: 可选外部停止信号；为 None 时仅靠队列 None 哨兵退出
    :param on_event: 可选冷场事件回调（cold_start/cold_end 翻转时同步触发）
    :param on_snapshot: 可选周期快照回调，按 snapshot_interval_seconds 节流
                        （真实时间），把 get_snapshot() 结果推给 LLM 层；
                        首次调用只记起点不推送。为 None 时行为与 v2 完全一致
    :param snapshot_interval_seconds: on_snapshot 推送间隔（秒）
    """
    analyzer = DanmuAnalyzer(cfg, on_event=on_event)
    logger.info(
        "规则分析线程已启动 cold_mode=%s cold_window=%ss enter=%s exit=%s snapshot=%ss",
        cfg.cold_mode, cfg.cold_window_seconds, cfg.cold_enter_threshold,
        cfg.cold_exit_threshold, snapshot_interval_seconds,
    )
    last_snapshot_push_ts: float = 0.0

    def _maybe_push_snapshot(now_ts: float) -> None:
        """真实时间节流推送周期快照；回调异常不允许影响分析主循环。"""
        nonlocal last_snapshot_push_ts
        if on_snapshot is None:
            return
        if last_snapshot_push_ts == 0.0:
            # 首次只记起点不推送，避免开播瞬间产出空快照
            last_snapshot_push_ts = now_ts
            return
        if now_ts - last_snapshot_push_ts < snapshot_interval_seconds:
            return
        last_snapshot_push_ts = now_ts
        try:
            on_snapshot(analyzer.get_snapshot())
        except Exception as e:
            logger.warning("周期快照回调异常，已忽略: %s", e)

    while True:
        if stop_event is not None and stop_event.is_set():
            break
        try:
            record = q.get(timeout=1.0)
        except queue.Empty:
            # 无新消息：用真实时间推进冷场检测（长静默也能触发冷场）
            now_ts = time.time()
            analyzer.tick(now_ts)
            _maybe_push_snapshot(now_ts)
            continue
        if record is None:
            # 哨兵：采集主线程通知退出
            break
        try:
            analyzer.feed(record)
        except Exception as e:
            logger.warning("分析 record 失败: %s record=%r", e, record)
        _maybe_push_snapshot(time.time())
    logger.info("规则分析线程已退出，共处理 %s 条记录", analyzer.total_records)


# ================================================================ 历史回放入口
def replay_jsonl(path: str, cfg: AnalyzerConfig, realtime: bool = False,
                  on_event: Optional[Callable[[Dict[str, Any]], None]] = None
                  ) -> DanmuAnalyzer:
    """从历史 JSONL 回放数据，复现冷场 / 解除事件与统计结果。

    :param path: JSONL 文件路径，每行一条 record dict
    :param cfg: AnalyzerConfig
    :param realtime: True 则按记录间 ts 差节流回放（接近真实节奏）；
                     False 则全速回放，仅靠 tick 推进冷场判定
    :param on_event: 可选冷场事件回调
    :return: 最终的 DanmuAnalyzer 实例（可调 get_snapshot 查看结果）
    """
    analyzer = DanmuAnalyzer(cfg, on_event=on_event)
    logger.info("回放开始 file=%s realtime=%s cold_mode=%s",
                path, realtime, cfg.cold_mode)
    count = 0
    last_ts: Optional[int] = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except Exception as e:
                logger.warning("跳过非 JSON 行: %s (%s)", line[:60], e)
                continue
            ts = int(record.get("ts") or 0)
            # 节流：若开启 realtime 且与上一条有时间差，按差值 sleep 模拟实时
            if realtime and last_ts is not None and ts > last_ts:
                # 单条之间最多 sleep 5 秒，避免回放过慢
                time.sleep(min(ts - last_ts, 5.0))
            # 先推进冷场判定（模拟"两条记录之间的时间流逝"）
            analyzer.tick(ts)
            analyzer.feed(record)
            last_ts = ts
            count += 1
    # 回放结束后输出最终快照
    final_ts = last_ts or int(time.time())
    analyzer._log_activity_snapshot(final_ts)
    analyzer._log_stats_snapshot(final_ts)
    logger.info("回放结束 共处理 %s 条记录", count)
    return analyzer


# ================================================================ CLI
def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抖音弹幕规则统计层（v2）")
    parser.add_argument("--replay", metavar="JSONL_PATH",
                        help="从历史 JSONL 回放验证规则逻辑")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                        help=f"配置文件路径（默认 {DEFAULT_CONFIG_PATH}）")
    parser.add_argument("--realtime", action="store_true",
                        help="回放时按记录 ts 节流模拟实时（默认全速回放）")
    parser.add_argument("--snapshot", action="store_true",
                        help="回放结束后输出最终实时指标 JSON（get_snapshot 结果）")
    parser.add_argument(
        "--cold-mode", choices=["fixed", "adaptive"], default=None,
        help="覆盖配置中的 cold_mode（fixed=v1 单阈值回归；adaptive=自适应基线）"
             "；不指定则用 config.json 中的值",
    )
    return parser.parse_args()


def main() -> int:
    _setup_logging()
    args = _parse_args()
    cfg = load_analyzer_config(args.config)
    # CLI 覆盖 cold_mode（便于论文对比实验，不修改 config.json）
    if args.cold_mode is not None and args.cold_mode != cfg.cold_mode:
        logger.info("[CLI 覆盖] cold_mode: %s → %s", cfg.cold_mode, args.cold_mode)
        # 直接改字段：__post_init__ 已确保 exit>enter，cold_mode 仅影响 detector 选择
        cfg.cold_mode = args.cold_mode

    if args.replay:
        if not os.path.exists(args.replay):
            logger.error("回放文件不存在: %s", args.replay)
            return 1
        analyzer = replay_jsonl(args.replay, cfg, realtime=args.realtime)
        if args.snapshot:
            print(json.dumps(analyzer.get_snapshot(), ensure_ascii=False, indent=2))
        return 0

    # 无 --replay 时仅打印配置说明，避免误启动空跑
    logger.info("analyzer.py 当前为分析模块，无 --replay 时不独立运行。")
    logger.info("实时模式由 main.py 自动起 daemon 线程调用 run_analyzer。")
    logger.info("回放测试: python analyzer.py --replay data/danmu_douyin_YYYY-MM-DD.jsonl")
    logger.info("对比实验: python analyzer.py --replay data/xxx.jsonl --cold-mode fixed|adaptive")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
