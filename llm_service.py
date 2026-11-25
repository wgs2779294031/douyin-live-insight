"""LLM 服务层：观众画像 / 冷场台词 / 商品推荐 三能力 + 独立 worker 线程。

上游：规则层 analyzer.DanmuAnalyzer.get_snapshot() 的快照 dict 与冷场事件 dict。
下游：data/ 下三份按天切分的 JSONL（portrait / coldline / recommend）。

架构（与规则层一致的"队列旁路"模式）：
1. analyzer 线程通过 on_snapshot / on_event 回调把消息 put_nowait 进 LLM 队列，
   队列满直接丢弃，回调本身绝不阻塞；
2. run_llm_worker 在独立 daemon 线程串行消费，同步调用 LLMClient（短超时），
   LLM 慢/挂只影响本线程，采集主循环与规则分析线程不受任何影响；
3. 任何单条消息处理失败都只 warning，不退出 worker。

匿名化红线：发送给模型前 _scrub_userids 递归删除所有 user_id 键；
api_key 只在 LLMClient 内存中，本模块不读取、不打印。

商品（组员供应链选品接口，本阶段预留）数据结构：
    {
        "product_id": str | int,   # 仅落盘对齐用，不发送给模型
        "name": str,               # 商品名
        "category": str,           # 类目（可选）
        "price": str | float,      # 价格（可选）
        "tags": [str],             # 卖点标签（可选）
        "desc": str,               # 一句话描述（可选）
    }
product_list 为空时 RecommendService 直接返回 []，不调用 API。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Union

from file_lock import file_lock
from llm_client import (LLMClient, LLMRouter, MetricsCallback, canonical_llm_source,
                        source_secret_set, validate_llm_metadata, remember_llm_secrets)

logger = logging.getLogger("llm")

# 与 analyzer 一致：默认共用同目录 config.json
DEFAULT_CONFIG_PATH = "config.json"


class LLMConfigError(RuntimeError):
    """LLM 层配置校验失败。"""


# ================================================================ 配置
# 兼容迁移用：旧平铺结构默认值（与 v1 默认对齐）
_LEGACY_DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
_LEGACY_DEFAULT_MODEL = "doubao-seed-2-1-turbo-260628"

# 默认 providers（双 Provider：豆包 + DeepSeek，均空 key 待用户填写）
_DEFAULT_PROVIDERS: Dict[str, Dict[str, str]] = {
    "doubao": {
        "api_key": "",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-seed-2-1-turbo-260628",
    },
    "deepseek": {
        "api_key": "",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
    },
}


@dataclass
class LLMConfig:
    """LLM 层配置（对齐任务书 v2.2 第 1 节 llm 节，providers 嵌套结构）。

    兼容 v1 平铺结构：若 from_dict 检测到旧的 api_key/base_url/model 直接
    出现在 llm 节下，自动迁移为 providers.doubao 并输出 deprecation WARNING。
    """
    # 主配置默认许可；运行时总控可显式覆盖。无凭据时不建立自动 worker。
    enabled: bool = False
    # 当前主 Provider 名（必须出现在 providers 中，否则退化到第一个有 key 的）
    active_provider: str = "doubao"
    # 双 Provider 嵌套：{"doubao": {api_key,base_url,model}, "deepseek": {...}}
    providers: Dict[str, Dict[str, str]] = field(default_factory=lambda: dict(_DEFAULT_PROVIDERS))
    # 业务（画像/推荐）单次请求超时秒数
    timeout_seconds: int = 30
    # 冷场台词独立超时（更短，避免阻塞冷场救场链路）
    coldline_timeout_seconds: int = 5
    # 探测请求超时秒数（ping+max_tokens=1）
    probe_timeout_seconds: int = 5
    # 画像生成周期（秒）：与规则层快照节奏对齐
    portrait_interval_seconds: int = 60
    # 冷场台词开关
    coldline_enabled: bool = True
    # 冷场台词最小间隔（秒）：worker 维护 last_coldline_ts，小于此值跳过
    coldline_min_interval_seconds: int = 120
    # 冷场**持续中**的补词间隔（秒）。0 = 关闭，退回旧行为（只在 cold_start 跃迁那一次出词）。
    # 为什么需要它：analyzer 的 cold_start 只在「进入冷场」时发一次，一场播两小时
    # 一直冷着，主播也只在开头看到 3 句话——台词提示等于摆设（10-25 实测：
    # 研途 16:00 进冷场，全天 coldline jsonl 只有 2 条）。
    # 默认 0 而不是 180：代码默认值保持"与旧行为一致"，由 config.json 显式打开
    # （同 portrait_min_danmaku 的回归开关习惯），免得测试和别处构造的 cfg 被动改行为。
    coldline_repeat_seconds: int = 0
    # 商品推荐开关（组员选品接口未就绪，默认关）
    recommend_enabled: bool = False
    # 候选商品列表 JSON 文件路径（相对运行目录）；为空时不加载商品，推荐链路不触发
    products_file: str = ""
    # metrics 埋点开关；false 不写 data/llm_metrics_*.jsonl
    metrics_enabled: bool = True
    # 备 Provider 状态下探测主的间隔基数（秒）
    provider_probe_interval_seconds: int = 1800
    # 主备切换阈值：连续失败次数达到此值即切换
    provider_failover_threshold: int = 2
    # 失败计数自动归零的空闲时长（秒）
    provider_fail_count_reset_seconds: int = 1800
    # 画像空窗熔断阈值：本次快照冷场窗口内弹幕条数 < 该值时跳过画像 LLM 调用（0 成本）。
    # 默认 1：近 60s 至少 1 条真实弹幕才花钱；设为 0 时等同旧行为（回归开关）。
    portrait_min_danmaku: int = 1
    # 日 token 预算护栏（跨进程文件记账，按 provider 分账本）。
    # 主账 = 豆包：每日免费额度 200 万 token，护栏取 50%（100 万），超限不停用——
    # 按链借道备 provider 继续跑，次日自动重置回主。设 0 = 不设限。
    daily_token_budget: int = 1_000_000
    # 备用 provider 各自的日账（免费额度的 90% 等）。没配的备用不设限
    # （如官方 DeepSeek 按量计费，没有「免费额度百分比」概念）。
    backup_daily_token_budgets: Dict[str, int] = field(default_factory=lambda: {})
    # Bound only by the trusted local loader, never accepted from JSON/HTTP.
    _source_path: str = field(default="", init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # providers 校验与规范化
        if not isinstance(self.providers, dict) or not self.providers:
            self.providers = dict(_DEFAULT_PROVIDERS)
        else:
            norm: Dict[str, Dict[str, str]] = {}
            for name, cfg in self.providers.items():
                if not isinstance(cfg, dict):
                    continue
                norm[name] = {
                    "api_key": str(cfg.get("api_key") or "").strip(),
                    "base_url": str(cfg.get("base_url") or "").strip(),
                    "model": str(cfg.get("model") or "").strip(),
                    "protocol": str(cfg.get("protocol") or "").strip(),
                }
            self.providers = norm or dict(_DEFAULT_PROVIDERS)
        # active_provider 必须在 providers 中（否则退化到第一个 key）
        if self.active_provider not in self.providers:
            self.active_provider = next(iter(self.providers), "doubao")
        # 数值字段校验
        if self.timeout_seconds <= 0:
            raise LLMConfigError(
                f"timeout_seconds 必须为正整数，当前值={self.timeout_seconds}")
        if self.portrait_interval_seconds <= 0:
            raise LLMConfigError(
                "portrait_interval_seconds 必须为正整数，"
                f"当前值={self.portrait_interval_seconds}")
        if self.coldline_timeout_seconds <= 0:
            raise LLMConfigError(
                "coldline_timeout_seconds 必须为正整数，"
                f"当前值={self.coldline_timeout_seconds}")
        if self.probe_timeout_seconds <= 0:
            raise LLMConfigError(
                "probe_timeout_seconds 必须为正整数，"
                f"当前值={self.probe_timeout_seconds}")
        if self.coldline_min_interval_seconds < 0:
            raise LLMConfigError(
                "coldline_min_interval_seconds 不能为负，"
                f"当前值={self.coldline_min_interval_seconds}")
        if self.coldline_repeat_seconds < 0:
            raise LLMConfigError(
                "coldline_repeat_seconds 不能为负（0=关闭冷场续播），"
                f"当前值={self.coldline_repeat_seconds}")
        if self.provider_failover_threshold < 1:
            raise LLMConfigError(
                "provider_failover_threshold 必须 >=1，"
                f"当前值={self.provider_failover_threshold}")
        if self.provider_probe_interval_seconds < 1:
            raise LLMConfigError(
                "provider_probe_interval_seconds 必须 >=1，"
                f"当前值={self.provider_probe_interval_seconds}")
        if self.provider_fail_count_reset_seconds < 1:
            raise LLMConfigError(
                "provider_fail_count_reset_seconds 必须 >=1，"
                f"当前值={self.provider_fail_count_reset_seconds}")
        if self.portrait_min_danmaku < 0:
            raise LLMConfigError(
                f"portrait_min_danmaku 不能为负，当前值={self.portrait_min_danmaku}")
        if self.daily_token_budget < 0:
            raise LLMConfigError(
                f"daily_token_budget 不能为负，当前值={self.daily_token_budget}")
        if not isinstance(self.backup_daily_token_budgets, dict):
            raise LLMConfigError(
                "backup_daily_token_budgets 必须是对象（provider 名 → 日预算）")
        for prov, b in self.backup_daily_token_budgets.items():
            if not isinstance(b, int) or b < 0:
                raise LLMConfigError(
                    f"backup_daily_token_budgets[{prov!r}] 必须为非负整数，"
                    f"当前值={b!r}")

    @property
    def is_active(self) -> bool:
        """该配置快照的默认许可与 key 状态；不代表实时总控或 worker 存活。"""
        if not self.enabled:
            return False
        if not self.providers:
            return False
        return any(
            bool(str(p.get("api_key") or "").strip())
            for p in self.providers.values()
        )

    def build_router(self,
                     on_metrics: Optional[MetricsCallback] = None,
                     budget_dir: str = "data",
                     tag: str = "", provider_override: Optional[str] = None,
                     automatic: bool = False, control_epoch=None,
                     stop_event: Optional[threading.Event] = None,
                     control_data_dir: Optional[str] = None,
                     request_guard: Optional[Callable[[], bool]] = None,
                     on_dispatch: Optional[Callable[..., object]] = None
                     ) -> Optional[LLMRouter]:
        """根据 providers 配置构造 LLMRouter；无可用 provider 返回 None。

        :param budget_dir: 日 token 预算记账目录（采集与日报传同一目录才共用一本账）
        :param tag: 这条链路服务的主播标签，用于闸门 tags_off 的单主播开关
        """
        if not self.providers:
            return None
        return LLMRouter(
            providers_cfg=self.providers,
            active_provider=self.active_provider,
            timeout_seconds=self.timeout_seconds,
            probe_timeout_seconds=self.probe_timeout_seconds,
            failover_threshold=self.provider_failover_threshold,
            probe_interval_seconds=self.provider_probe_interval_seconds,
            fail_count_reset_seconds=self.provider_fail_count_reset_seconds,
            on_metrics=on_metrics,
            daily_token_budget=self.daily_token_budget,
            backup_token_budgets=self.backup_daily_token_budgets,
            budget_dir=budget_dir,
            gate_dir=(control_data_dir if control_data_dir is not None else
                      os.path.join(os.path.dirname(self._source_path), "data")
                      if self._source_path else budget_dir),
            tag=tag,
            config_path=self._source_path or None,
            requested_provider=provider_override,
            automatic=automatic,
            control_epoch=control_epoch,
            stop_event=stop_event,
            request_guard=request_guard,
            on_dispatch=on_dispatch,
        )

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "LLMConfig":
        """从 config.json 的 llm 节加载，只取已知字段，缺省走默认值。

        兼容 v1 平铺结构：检测到 api_key/base_url/model 直接出现在 llm 节下
        （而非 providers 嵌套）时，自动迁移为 providers.doubao 并输出 WARNING。
        """
        if not isinstance(raw, dict):
            raw = {}
        # 兼容旧平铺结构（v1）：api_key/base_url/model 直接在 llm 节下
        is_legacy_flat = any(
            k in raw for k in ("api_key", "base_url", "model")
        ) and "providers" not in raw
        if is_legacy_flat:
            logger.warning(
                "检测到 v1 平铺 llm 配置（api_key/base_url/model 直接在 llm 节下），"
                "自动迁移为 providers.doubao；请尽快更新 config.json 为 providers 嵌套结构"
            )
            legacy_provider = {
                "api_key": str(raw.get("api_key", "")),
                "base_url": str(raw.get("base_url", _LEGACY_DEFAULT_BASE_URL)),
                "model": str(raw.get("model", _LEGACY_DEFAULT_MODEL)),
            }
            raw = {k: v for k, v in raw.items()
                   if k not in ("api_key", "base_url", "model")}
            raw["providers"] = {"doubao": legacy_provider}
            raw.setdefault("active_provider", "doubao")
        known = {k: v for k, v in raw.items()
                 if k in cls.__dataclass_fields__ and cls.__dataclass_fields__[k].init
                 and not k.startswith("_")}
        return cls(**known)


def load_llm_config(path: str = DEFAULT_CONFIG_PATH) -> LLMConfig:
    """读取 config.json 的 llm 节；文件不存在 / 无 llm 节 / 解析失败均用默认值。"""
    path = canonical_llm_source(path)

    def bound(cfg):
        cfg._source_path = path
        return cfg

    if not os.path.exists(path):
        return bound(LLMConfig())
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f) or {}
    except Exception:
        logger.warning("读取 LLM 层配置失败，使用默认值（关闭）")
        return bound(LLMConfig())
    if not isinstance(raw, dict):
        logger.warning("LLM 主配置根节点不是对象，按关闭处理")
        return bound(LLMConfig())
    section = raw.get("llm") or {}
    if not isinstance(section, dict):
        logger.warning("config.json 的 llm 节不是对象，LLM 层按关闭处理")
        return bound(LLMConfig())
    try:
        remember_llm_secrets(section, source_secret_set(path))
        cfg = LLMConfig.from_dict(section)
        if not validate_llm_metadata(cfg.providers, cfg.active_provider,
                                     known_secrets=source_secret_set(path)):
            logger.warning("LLM 配置元数据含凭据，按关闭处理")
            return bound(LLMConfig())
        return bound(cfg)
    except LLMConfigError:
        raise LLMConfigError("LLM 配置字段无效") from None
    except Exception:
        logger.warning("LLM 层配置字段异常，按关闭处理")
        return bound(LLMConfig())


# ================================================================ JSON 解析工具
# 模型偶发返回 ```json ... ``` 包裹或前后带解释文字，做两层容错解析
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$",
                       re.DOTALL | re.IGNORECASE)
# 台词裸文本兜底：按换行/中英文分号切条
_LINE_SPLIT_RE = re.compile(r"[\n；;]+")
# 常见编号前缀："1." "1、" "1:" "① " 等
_INDEX_PREFIX_RE = re.compile(r"^\s*(?:\d+\s*[.、:：)]|[①②③④⑤⑥⑦⑧⑨⑩])\s*")
# 递归脱敏时删除的键名（双保险：即便以后快照新增字段也不泄露 user_id）
_SCRUB_KEYS = {"user_id"}


def _strip_fence(text: str) -> str:
    m = _FENCE_RE.match(text)
    return m.group(1).strip() if m else text.strip()


def _loads_json(text: str) -> Optional[Any]:
    """先整体解析，失败再截取首个 {/[ 到末个 }/] 容错解析。"""
    body = _strip_fence(text)
    try:
        return json.loads(body)
    except Exception:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i, j = body.find(open_c), body.rfind(close_c)
        if i != -1 and j > i:
            try:
                return json.loads(body[i:j + 1])
            except Exception:
                continue
    return None


def _scrub_userids(obj: Any) -> Any:
    """递归删除 dict 中的 user_id 键（发送给 API 前的匿名化双保险）。"""
    if isinstance(obj, dict):
        return {k: _scrub_userids(v) for k, v in obj.items() if k not in _SCRUB_KEYS}
    if isinstance(obj, list):
        return [_scrub_userids(v) for v in obj]
    return obj


def _as_str_list(value: Any, limit: int) -> List[str]:
    """安全转字符串列表：非 list 返空，元素 str() 化后去空白、去重保序、限量。"""
    if not isinstance(value, list):
        return []
    out: List[str] = []
    seen = set()
    for item in value:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _as_question_topics(value: Any, limit: int) -> List[Dict[str, Any]]:
    """观众提问主题归一化：[{topic, count, examples}]；非法结构丢弃。

    兼容两种输入：dict 列表（标准）与纯字符串列表（模型偶尔只给主题名）。
    """
    out: List[Dict[str, Any]] = []
    if not isinstance(value, list):
        return out
    for item in value:
        if isinstance(item, dict):
            topic = str(item.get("topic") or item.get("theme")
                        or item.get("name") or "").strip()
            examples = _as_str_list(item.get("examples") or item.get("questions"), 3)
            try:
                count = int(item.get("count") or 0)
            except (TypeError, ValueError):
                count = 0
            if count <= 0:
                count = len(examples)
        elif isinstance(item, str):
            topic, examples, count = item.strip(), [], 0
        else:
            continue
        if not topic:
            continue
        out.append({"topic": topic, "count": max(0, count), "examples": examples})
        if len(out) >= limit:
            break
    return out


def _to_ratio(value: Any) -> float:
    """情感比例转 float 并 clamp 到 [0,1]；非法值返 0。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if v < 0:
        return 0.0
    if v > 1:
        return 1.0
    return v


# ================================================================ Prompt
PORTRAIT_SYSTEM = """你是抖音直播间的观众画像分析专家。根据一个统计窗口内的观众弹幕样本与统计数据，提炼结构化观众画像。

只输出一个 JSON 对象，禁止输出 markdown、代码块、注释或任何解释文字。JSON 结构如下：
{
  "interest_topics": ["观众最感兴趣的话题，3-8 个短语"],
  "question_topics": [
    {"topic": "观众提问的业务主题短标签（如 简历 / 面试 / 实习 / 校招 / 考公考研 / 转行 / 年龄学历焦虑 等）",
     "count": 3,
     "examples": ["该主题下观众的原问题，最多 3 条"]}
  ],
  "consumption_intent": {
    "level": "high|medium|low",
    "keywords": ["与购买意愿相关的关键词，如商品名、价格、链接、上车等"]
  },
  "activity_layers": {
    "high": ["高活跃观众昵称，来自活跃榜或频繁发弹幕的人"],
    "mid": ["中活跃观众昵称"],
    "low": ["低活跃观众昵称"]
  },
  "sentiment": {"positive": 0.0, "neutral": 0.0, "negative": 0.0},
  "suggestions": ["给主播的运营或互动建议，1-5 条短句"]
}

硬性要求：
1. sentiment 三项均为 0 到 1 的小数且三项之和必须等于 1（如 0.6/0.3/0.1）；
2. activity_layers 只能使用输入数据中真实出现的昵称，禁止编造；没有可靠依据时对应数组给空数组；
3. 输入信息不足（弹幕极少）时，interest_topics/suggestions 可给空数组，sentiment 向 neutral 倾斜，禁止编造数据；
4. consumption_intent.level 只能是 high、medium、low 三者之一，弹幕中无购买相关信号时给 low；
5. question_topics 只统计**观众真实提出的问题**：主题名用 2-6 字业务短标签（简历/面试/实习/校招/考公考研/转行/年龄学历焦虑/专业选择/薪资待遇 等），
   同一主题合并计数；**禁止把所有提问都归入「其他」这一个主题**——确实无法归类时才用「其他」，且它应是少数；
   无观众提问时给空数组；count 为该主题下的提问条数，examples 为该主题下最多 3 条原问题。"""

COLDLINE_SYSTEM = """你是资深抖音直播运营专家，帮主播在冷场时接住话头、把观众拉回互动。

根据给定的冷场事件、最近话题和最新观众画像，创作 1 到 3 条主播可以直接念出口的话。

要求：
1. 口语化、短句、有亲和力，适合直接朗读，每条不超过 50 个字；
2. **优先接观众正在问的问题**：画像里有 question_topics / interest_topics 时，就着其中一个
   具体问题往下说（例如"刚才有家长问色弱能不能报电子信息"），不要说"扣1""点关注"
   这类和当前话题无关的通用话术 —— 主播正在讲专业，你让他求关注就是打断他；
3. 只有在完全没有可用话题时（刚开播、弹幕只有问候），才用通用暖场 + 互动引导；
4. 不要书面语、不要编号、不要舞台提示，不要出现"画像/数据/观众"这类字眼。

只输出一个 JSON 字符串数组，例如：
["刚才有家长问色弱能不能报电子信息，能报，但这两个方向要避开",
 "说到分数段，我按 600 分上下各给一条路，你们对号入座"]
禁止输出 markdown 或任何解释。"""

RECOMMEND_SYSTEM = """你是电商直播口播文案专家。根据观众画像，为每一个候选商品写一句直播口播推荐理由。

要求：
1. 输出一个 JSON 字符串数组，数组长度和顺序必须与输入商品列表严格一一对应；
2. 每句不超过 40 个字，结合画像中的兴趣话题或消费意向，像主播说话，不要出现"画像/数据/用户"等字眼；
3. 画像信息不足时，就按商品本身的类目和卖点写通用种草理由；
4. 禁止输出 markdown 或任何解释，只输出 JSON 数组。"""


# ================================================================ 主播人设卡（anchor profile，v2.3）
class SharedState:
    """Room identity plus a fail-closed live/connection signal for automatic work."""

    def __init__(self) -> None:
        self.room_id = None  # Optional[str]
        self._live_lock = threading.Lock()
        self._live_status = None
        self._live_connected = False
        self._live_generation = 0
        self._live_connection = 0

    def _live_ready(self):
        return self._live_status == 2 and self._live_connected

    def begin_live_connection(self):
        with self._live_lock:
            if self._live_ready():
                self._live_generation += 1
            self._live_status, self._live_connected = None, False
            self._live_connection += 1
            return self._live_connection

    def set_live_status(self, status, connection):
        with self._live_lock:
            if connection != self._live_connection:
                return
            was_ready = self._live_ready()
            self._live_status = status if type(status) is int else None
            if was_ready and not self._live_ready():
                self._live_generation += 1

    def set_live_connected(self, connected, connection):
        with self._live_lock:
            if connection != self._live_connection:
                return
            was_ready = self._live_ready()
            self._live_connected = connected is True
            if not self._live_connected:
                self._live_status = None
            if was_ready and not self._live_ready():
                self._live_generation += 1

    def automatic_live(self):
        with self._live_lock:
            ready = self._live_ready()
            reason = ("live_ready" if ready else "live_unknown" if self._live_status is None
                      else "not_live" if self._live_status != 2 else "waiting_connection")
            return {"ready": ready, "reason": reason, "generation": self._live_generation}


@dataclass
class AnchorConfig:
    """人设卡配置（config.json 顶层 anchor 节）。"""
    # 默认 false：不加载人设，行为与 v2.2 完全一致（回归红线）
    enabled: bool = False
    # 人设卡目录（相对路径，一份 JSON 一个主播，文件名 = 纯数字 room_id）
    profile_dir: str = "anchor_profiles"

    @classmethod
    def from_dict(cls, raw: Any) -> "AnchorConfig":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            enabled=bool(raw.get("enabled", False)),
            profile_dir=str(raw.get("profile_dir") or "anchor_profiles").strip()
            or "anchor_profiles",
        )


def load_anchor_config(path: str = DEFAULT_CONFIG_PATH) -> AnchorConfig:
    """读取 config.json 顶层 anchor 节；缺失/解析失败均用默认值（关闭）。"""
    if not os.path.exists(path):
        return AnchorConfig()
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f) or {}
    except Exception as e:
        logger.warning("读取 anchor 配置失败，人设功能按关闭处理: %s", e)
        return AnchorConfig()
    return AnchorConfig.from_dict(raw.get("anchor"))


def load_products(path: str) -> List[Dict[str, Any]]:
    """从 JSON 文件加载候选商品列表。

    格式兼容：
      - 顶层对象：{"products": [...]}
      - 顶层数组：[...]

    字段清洗规则：
      - product_id: 缺失时自动补 1 起始序号字符串
      - name: 必填，空/缺失/非字符串跳过整条
      - category/desc: 可选，缺失填 ""，统一转字符串
      - price: 可选，数字或数字字符串保留原样，其他值原样透传
      - tags: 可选，必须为数组，非数组视为 []；元素 str() 化
      - 数组中非对象元素整条跳过

    :param path: JSON 文件路径（相对运行目录）
    :return: 清洗后的商品列表，每个 dict 至少含 product_id/name/category/price/tags/desc 六键
    """
    if not path or not path.strip():
        return []
    path = path.strip()
    if not os.path.exists(path):
        logger.warning("商品文件不存在: %s", path)
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning("商品文件 JSON 解析失败 %s: %s", path, e)
        return []

    # 兼容顶层对象 {"products": [...]} 或顶层数组 [...]
    if isinstance(data, dict):
        products_raw = data.get("products")
        if not isinstance(products_raw, list):
            logger.warning("商品文件顶层对象缺少 products 数组: %s", path)
            return []
    elif isinstance(data, list):
        products_raw = data
    else:
        logger.warning("商品文件顶层既不是对象也不是数组: %s", path)
        return []

    out: List[Dict[str, Any]] = []
    for idx, item in enumerate(products_raw):
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()

        # product_id: 缺失时用 1 起始序号
        product_id = item.get("product_id")
        if product_id is None or product_id == "":
            product_id = str(idx + 1)
        else:
            product_id = str(product_id)

        category = item.get("category")
        category = str(category) if category is not None else ""

        desc = item.get("desc")
        desc = str(desc) if desc is not None else ""

        price = item.get("price")
        # price: 数字或数字字符串保留原样，其他值原样透传
        # 这里不做强制类型转换，直接保留原值

        tags = item.get("tags")
        if isinstance(tags, list):
            tags = [str(t) for t in tags]
        else:
            tags = []

        out.append({
            "product_id": product_id,
            "name": name,
            "category": category,
            "price": price,
            "tags": tags,
            "desc": desc,
        })

    return out


def load_anchor_profile(profile_dir: str,
                        room_id: str,
                        room_tag: str = "") -> "tuple[Optional[Dict[str, Any]], Optional[str]]":
    """加载人设卡，查找顺序：`{tag}.json` → `{room_id}.json` → `default.json`。

    ★ 2026-10-28 改：原来**只按 room_id** 找。但抖音的 room_id **每一场直播都变**
      （研途 10-05 是 7693034170179750691、10-28 是 7701563483646167842，
       一天开两场还会出现两个不同 room_id）—— 按 room_id 存人设等于每场都得
      重新丢一份文件，这才是「8 路只做了 1 份人设、开关还关着」的真因。
      主播 tag 是稳定的（`anchors.json` 的主键），所以按 tag 找。
      room_id 只作兼容：老文件、以及将来按场次单独配人设的用法。
    # :return: (profile, error)

    :return: (profile, error)
        - profile 为规范化后的完整人设 dict；None 表示无人设；
        - error 非 None 表示**配置有问题**（目录/文件解析失败）——「这一路没配人设」
          不算 error，属正常路径（无人设 prompt 与 v2.2 逐字符一致）。
        - (None, None) 表示全空字段或没配，等同无人设。
    """
    if not room_tag and not room_id:
        return None, "room_tag 与 room_id 都为空"

    candidates = []
    if room_tag:
        candidates.append(os.path.join(profile_dir, "%s.json" % room_tag))
    if room_id:
        candidates.append(os.path.join(profile_dir, "%s.json" % room_id))

    path = next((p for p in candidates if os.path.exists(p)), None)
    if path is None:
        default_path = os.path.join(profile_dir, "default.json")
        if not os.path.exists(default_path):
            # 目录在、只是这一路没配人设 → 正常（8 路里本来就可能只有几路有）
            if os.path.isdir(profile_dir):
                return None, None
            return None, "人设目录不存在：%s" % profile_dir
        path = default_path
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return None, f"人设 JSON 解析失败 {path}: {type(e).__name__}"
    if not isinstance(data, dict):
        return None, f"人设 JSON 不是对象: {path}"

    def _str_list(value: Any) -> List[str]:
        if not isinstance(value, list):
            return []
        out = []
        for item in value:
            text = str(item).strip()
            if text and text not in out:
                out.append(text)
        return out

    profile = {
        "name": str(data.get("name") or "").strip(),
        "category": str(data.get("category") or "").strip(),
        "style": str(data.get("style") or "").strip(),
        "catchphrases": _str_list(data.get("catchphrases")),
        "fan_call": str(data.get("fan_call") or "").strip(),
        "audience": str(data.get("audience") or "").strip(),
        "interaction_preference": _str_list(data.get("interaction_preference")),
        "forbidden_topics": _str_list(data.get("forbidden_topics")),
    }
    if not (profile["name"] or profile["category"] or profile["style"]
            or profile["catchphrases"]):
        return None, None  # 全空字段 = 等同无人设
    return profile, None


# 注入字段优先级：高 -> 低；超 300 中文字符时从低优先级字段开始裁剪
_INJECT_FIELD_ORDER = ("name", "category", "style", "catchphrases",
                       "fan_call", "audience", "interaction_preference",
                       "forbidden_topics")
_INJECT_MAX_CHARS = 300


def _field_json_len(field: str, value: Any) -> int:
    """字段序列化后的中文字符数（len 即字符数，含标点）。"""
    return len(json.dumps({field: value}, ensure_ascii=False))


def _trim_profile(profile: Dict[str, Any],
                  max_chars: int = _INJECT_MAX_CHARS
                  ) -> "tuple[Dict[str, Any], List[str]]":
    """按优先级裁剪人设卡，保证序列化注入文本 <= max_chars 中文字符。

    规则（任务书 v2.3 第 3.4.7 节）：
    - catchphrases 先截前 3 条；
    - 超限时从当前最低优先级字段开始截断/丢弃，其后更低优先级字段一并裁掉；
    - 返回 (裁剪后人设 dict, 被裁字段名列表)；调用方负责 WARNING 留痕。
    注意：裁剪只影响 prompt 注入文本；禁区过滤/口头禅校验用完整版数据。
    """
    if not isinstance(profile, dict):
        return {}, []
    trimmed_fields: List[str] = []
    kept: Dict[str, Any] = {}
    total = 0
    for field in _INJECT_FIELD_ORDER:
        value = profile.get(field)
        if field == "catchphrases" and isinstance(value, list) and len(value) > 3:
            value = value[:3]
            trimmed_fields.append("catchphrases(保留前3条)")
        if value is None or value == "" or value == []:
            continue
        seg_len = _field_json_len(field, value)
        if total + seg_len <= max_chars:
            kept[field] = value
            total += seg_len
            continue
        # 需要截断/丢弃该字段及其后所有更低优先级字段
        budget = max_chars - total
        if isinstance(value, list):
            trunc: List[Any] = []
            for item in value:
                if _field_json_len(field, trunc + [item]) <= budget:
                    trunc.append(item)
                else:
                    break
            if trunc:
                kept[field] = trunc
            trimmed_fields.append(field)
        else:
            overhead = _field_json_len(field, "")
            avail = budget - overhead
            if avail >= 2:
                trunc_text = str(value)[:avail]
                kept[field] = trunc_text
            trimmed_fields.append(field)
        break  # 更低优先级字段全部放弃
    # 记录因超限被整段放弃的字段（在被截断字段之后）
    stopped = False
    for field in _INJECT_FIELD_ORDER:
        if field in kept or field in trimmed_fields:
            if field in trimmed_fields:
                stopped = True
            continue
        if stopped and profile.get(field):
            trimmed_fields.append(field)
    return kept, trimmed_fields


# 禁区过滤的否定前缀：命中词前 2 字符内以这些字结尾时视为否定语境
_NEG_PREFIX = ("不", "没", "无", "别", "勿")


def _filter_forbidden(lines: List[str],
                      forbidden_topics: List[str]) -> "tuple[List[str], int]":
    """禁区话题硬过滤（不信任 LLM 自觉），带否定前缀豁免。

    :return: (保留的台词, 丢弃条数)；否定语境豁免不计入丢弃数。
    边界行为：禁区词在句首（prefix 空串）正常丢弃；
    禁区词前是标点（非否定字）正常丢弃。
    """
    kept: List[str] = []
    dropped = 0
    for line in lines:
        hit = []
        for kw in forbidden_topics:
            if not kw or kw not in line:
                continue
            idx = line.find(kw)
            prefix = line[max(0, idx - 2):idx]
            if prefix and prefix[-1] in _NEG_PREFIX:
                logger.warning(
                    "[人设过滤] 禁区词'%s'为否定语境，保留待复核: %s",
                    kw, line[:30])
                continue
            hit.append(kw)
        if hit:
            logger.warning("[人设过滤] 台词命中禁区%s，已丢弃: %s", hit, line[:30])
            dropped += 1
            continue
        kept.append(line)
    return kept, dropped


def _check_catchphrase(lines: List[str], catchphrases: List[str],
                       fan_call: str) -> int:
    """口头禅/粉丝称呼后处理校验（软约束，只统计不惩罚）。

    :return: 命中口头禅或粉丝称呼的台词条数
    """
    hits = 0
    for line in lines:
        if any(cp and cp in line for cp in catchphrases) or \
                (fan_call and fan_call in line):
            hits += 1
        else:
            logger.warning("[人设校验] 台词未含口头禅/粉丝称呼: %s", line[:30])
    return hits


def _is_empty_profile(profile: Optional[Dict[str, Any]]) -> bool:
    """人设是否为空（None 或关键四字段全空）——等同无人设模式。"""
    if not isinstance(profile, dict):
        return True
    return not (profile.get("name") or profile.get("category")
                or profile.get("style") or profile.get("catchphrases"))


def _build_coldline_system(profile: Dict[str, Any]) -> str:
    """有人设时的冷场台词 system prompt（任务书 v2.3 第 4.1 节模板 + 2026-10-28 修订）。

    ★ 2026-10-28 改：补「先接观众正在问的问题」。
      原模板只有「每条必须含口头禅」这一条硬要求，模型于是把 1-3 条全写成
      "家人们扣1""刚进来的点点关注"，主播正在讲的题一个字都没接。无人设路径
      10-25 已修（COLDLINE_SYSTEM 第 2 条 + _portrait_block），但人设用的是
      另一套模板，漏了同一条规则 —— 今天人设开关一打开，扣1 话术就会跟着回来。

    :param profile: 裁剪后的注入用人设（_trim_profile 产物）
    """
    desc = []
    if profile.get("category"):
        desc.append(f"主播是{profile['category']}主播")
    if profile.get("style"):
        desc.append(f"风格{profile['style']}")
    if profile.get("catchphrases"):
        desc.append("口头禅："
                    + json.dumps(profile["catchphrases"], ensure_ascii=False))
    if profile.get("fan_call"):
        desc.append(f"对粉丝的称呼是\"{profile['fan_call']}\"")
    if profile.get("audience"):
        desc.append(f"受众主要是{profile['audience']}")
    if profile.get("interaction_preference"):
        desc.append("偏好的互动方式："
                    + json.dumps(profile["interaction_preference"],
                                 ensure_ascii=False))
    head = f"你是主播\"{profile.get('name') or ''}\"的直播间救场助手。"
    if desc:
        head += "，".join(desc) + "。"
    rules = [
        "**优先接观众正在问的问题**：user 里有「观众最近在问」时，就着其中一条往下说"
        "（能引用原话最好，例如\"刚才有家长问色弱能不能报电子信息\"）；这时不要说"
        "\"扣1\"\"点关注\"\"点亮左上角\"这类跟当前话题无关的通用话术 —— 主播正在讲专业，"
        "你让他求关注就是打断他；",
        "只有在完全没有可用问题时（刚开播、弹幕只有问候），才用通用暖场 + 互动引导；",
        "口头禅或粉丝称呼在 1-3 条里自然带入至少一处即可，不必每条都塞，更不许生硬堆砌；",
        "口语化、短句、主播可直接照读，每条不超过 45 个字（写长了会被截断，"
        "主播念不完整）；不要书面语、不要编号、不要舞台提示；"
        "台词里不要出现\"画像/数据/观众\"这类字眼，说\"家长/同学/有人\"；",
    ]
    if profile.get("forbidden_topics"):
        rules.append("绝对不得提及以下话题或表述："
                     + json.dumps(profile["forbidden_topics"], ensure_ascii=False) + "；")
    rules.append("只输出JSON：{\"lines\": [\"台词1\", \"台词2\"]}")
    parts = [head, "任务：冷场时生成1-3条救场台词。", "硬要求："]
    parts += ["%d. %s" % (i, r) for i, r in enumerate(rules, 1)]
    return "\n".join(parts)


def _portrait_block(latest_portrait: Optional[Dict[str, Any]]) -> str:
    """把画像里「观众在问什么」拎成独立区块，拼在 user prompt 末尾。

    2026-10-25 加：整段画像 JSON 塞进去时模型基本只盯着 top_words，出来全是
    "扣1/点关注"这类通用话术（框框讲着"色弱能不能报电子信息"，系统让他"点亮左上角
    关注"，主播当然不用）。问题原话放到显眼位置才压得住。
    """
    if not latest_portrait:
        return ""
    out = "\n最新画像：" + json.dumps(latest_portrait, ensure_ascii=False)
    q_lines = []
    for q in (latest_portrait.get("question_topics") or [])[:4]:
        if not isinstance(q, dict):
            continue
        topic = str(q.get("topic") or "").strip()
        ex = [str(x) for x in (q.get("examples") or [])[:2] if x]
        if topic or ex:
            q_lines.append("- %s%s" % (
                topic, ("（观众原话：%s）" % "；".join(ex)) if ex else ""))
    if q_lines:
        out += ("\n观众最近在问（**优先接着这些说**，能引用原话更好）：\n"
                + "\n".join(q_lines))
    return out


def _build_coldline_user(cold_event: Dict[str, Any],
                         recent_topics: Dict[str, Any],
                         latest_portrait: Optional[Dict[str, Any]]) -> str:
    """有人设时的冷场台词 user prompt（三段式，含可选画像）。"""
    base = (
        f"当前冷场事件：{json.dumps(cold_event, ensure_ascii=False)}\n"
        f"最近话题：{json.dumps(recent_topics, ensure_ascii=False)}"
    )
    if not latest_portrait:
        return base + "\n最新画像（可选，可能为空）：（暂无）"
    return base + _portrait_block(latest_portrait)


# 冷场台词单条硬上限（字数）；prompt 里让模型写 45 字以内，这里兜底防超长
_LINE_MAX_CHARS = 50
# 收口语义位置：优先在这些符号处断开，避免把一句话砍成半截
_CLAMP_CUT_CHARS = "。！？!?；;，,、 "


def _clamp_line(text: str, limit: int = _LINE_MAX_CHARS) -> str:
    """把一条台词压到 limit 字以内，**优先在标点处收口**。

    2026-10-28 改：原来直接 `text[:50]`，把「…弹幕报上你的本科」这种句子砍在
    第 50 个字上 —— 主播照着念就是半句话。现在超长时回退到最近的标点（至少留
    一半长度才认），实在没有标点才硬截。
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(ch) for ch in _CLAMP_CUT_CHARS)
    if cut >= limit // 2:
        return head[:cut].rstrip("，,、；; ")
    return head


# 画像建议层注入段（有人设时追加到 PORTRAIT_SYSTEM 末尾；category/style 为主）
_PORTRAIT_ANCHOR_SUFFIX = (
    "\n请结合主播定位（{category}，风格{style}）让 suggestions "
    "更贴合该主播的实际直播方式。\n"
    "但 interest_topics、question_topics、consumption_intent、sentiment、activity_layers "
    "五个字段必须严格基于本次弹幕证据生成，不得因主播人设而偏移或脑补。"
)


# ================================================================ 三个 Service
class PortraitService:
    """观众画像提炼：snapshot -> 结构齐整的画像 dict（失败返 None）。

    v2.3：人设只允许影响 suggestions（建议层）；无人设时 prompt 与 v2.2
    逐字符一致。profile 传完整版人设 dict，注入前内部走 _trim_profile。
    """

    def __init__(self, client: "Union[LLMClient, LLMRouter]",
                 purpose: str = "realtime") -> None:
        self.client = client
        # 用途闸门标签（llm_runtime.json → purposes）。
        # 实时链路 = "realtime"；事后批处理（scripts/portrait_backfill.py）传 ""，
        # 表示不受 purposes 限制 —— 那是人手动发起的一次性分析，本身就有
        # --max-calls 单日封顶和日护栏兜底，不该因为「实时层关着」跑不了。
        self.purpose = purpose

    def generate(self, snapshot: Dict[str, Any],
                 profile: Optional[Dict[str, Any]] = None
                 ) -> Optional[Dict[str, Any]]:
        # 匿名化双保险：递归剔除 user_id（代表性弹幕本就不含 user_id，
        # active_top 里含 user_id，必须删除）
        payload = _scrub_userids(snapshot)
        system = PORTRAIT_SYSTEM
        if not _is_empty_profile(profile):
            inject, _ = _trim_profile(profile)
            system = PORTRAIT_SYSTEM + _PORTRAIT_ANCHOR_SUFFIX.format(
                category=inject.get("category") or "综合",
                style=inject.get("style") or "自然",
            )
        user = (
            "以下是抖音直播间一个统计窗口的实时快照（已匿名化，不含任何用户ID）。"
            "请按约定 schema 提炼观众画像，只输出 JSON：\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        raw = self.client.chat(system, user, temperature=0.3,
                               purpose=self.purpose)
        if raw is None:
            return None
        data = _loads_json(raw)
        if not isinstance(data, dict):
            logger.warning("[画像] 模型返回无法解析为 JSON 对象，已丢弃本次结果")
            return None
        return self._normalize(data, int(snapshot.get("ts") or time.time()))

    @staticmethod
    def _normalize(data: Dict[str, Any], portrait_ts: int) -> Dict[str, Any]:
        """补齐/纠正 schema 字段，保证落盘字段齐全、类型合法。"""
        ci = data.get("consumption_intent")
        ci = ci if isinstance(ci, dict) else {}
        level = str(ci.get("level", "low")).strip().lower()
        if level not in ("high", "medium", "low"):
            level = "low"

        al = data.get("activity_layers")
        al = al if isinstance(al, dict) else {}

        # 情感比例：clamp 后归一化，保证三项之和为 1；全 0 时按完全中性处理
        pos = _to_ratio((data.get("sentiment") or {}).get("positive")
                        if isinstance(data.get("sentiment"), dict) else 0)
        neu = _to_ratio((data.get("sentiment") or {}).get("neutral")
                        if isinstance(data.get("sentiment"), dict) else 0)
        neg = _to_ratio((data.get("sentiment") or {}).get("negative")
                        if isinstance(data.get("sentiment"), dict) else 0)
        total = pos + neu + neg
        if total > 0:
            sentiment = {
                "positive": round(pos / total, 3),
                "neutral": round(neu / total, 3),
                "negative": round(neg / total, 3),
            }
            # 修正 round 后的舍入误差，挂到 neutral 上
            drift = round(1 - sum(sentiment.values()), 3)
            sentiment["neutral"] = round(sentiment["neutral"] + drift, 3)
        else:
            # v2.2 第 6 节：和=0 时填默认 {0.33, 0.34, 0.33}（保证两 provider
            # 输出语义一致，而非 v1 的完全中性偏置）
            sentiment = {"positive": 0.33, "neutral": 0.34, "negative": 0.33}

        return {
            "portrait_ts": portrait_ts,
            "interest_topics": _as_str_list(data.get("interest_topics"), 8),
            "question_topics": _as_question_topics(data.get("question_topics"), 10),
            "consumption_intent": {
                "level": level,
                "keywords": _as_str_list(ci.get("keywords"), 10),
            },
            "activity_layers": {
                "high": _as_str_list(al.get("high"), 10),
                "mid": _as_str_list(al.get("mid"), 10),
                "low": _as_str_list(al.get("low"), 10),
            },
            "sentiment": sentiment,
            "suggestions": _as_str_list(data.get("suggestions"), 5),
        }


class ColdlineService:
    """冷场救场台词：cold_start 事件 + 近期话题 -> 1~3 条台词。

    v2.3 人设版：
    - profile 为完整人设 dict（非空）时，system 注入人设（300 字裁剪后）、
      user 改三段式（含可选最新画像）；返回台词先做禁区硬过滤
      （否定前缀豁免），再做口头禅校验（软约束只统计）；
    - profile 为 None/空 时，system+user 与 v2.2 逐字符一致（回归红线），
      不做过滤/校验。

    :return: (lines, stats)；stats 含 anchor_used/anchor_name/
             catchphrase_hit_count/forbidden_filtered_count
    防抖（最小间隔）由 run_llm_worker 维护 last_coldline_ts，本类保持无状态。
    """

    def __init__(self, client: "Union[LLMClient, LLMRouter]",
                 timeout_seconds: int = 5) -> None:
        self.client = client
        self.timeout_seconds = timeout_seconds

    def generate(self, cold_event: Dict[str, Any],
                 recent_topics: Dict[str, Any],
                 profile: Optional[Dict[str, Any]] = None,
                 latest_portrait: Optional[Dict[str, Any]] = None,
                 ) -> "tuple[List[str], Dict[str, Any]]":
        empty_stats = {"anchor_used": False, "anchor_name": "",
                       "catchphrase_hit_count": 0,
                       "forbidden_filtered_count": 0}
        if _is_empty_profile(profile):
            # v2.2 原样路径。**2026-10-25 起这里不再是逐字符一致**：原先 user 里
            # 只有 cold_event + recent_topics，模型只能靠 top_words 编"扣1/点关注"，
            # 而画像里明明有"观众在问色弱能不能报电子信息"这种可直接接话的素材
            # （老大："台词提示是核心卖点，但实际使用频率不高"）。现在把画像里
            # 的问题原话拼上去；没有画像时与旧版一字不差。
            user = json.dumps({
                "cold_event": cold_event,
                "recent_topics": recent_topics,
            }, ensure_ascii=False) + _portrait_block(latest_portrait)
            raw = self.client.chat(
                COLDLINE_SYSTEM, user, temperature=0.8,
                timeout_override=self.timeout_seconds, purpose="realtime",
            )
            if raw is None:
                return [], dict(empty_stats)
            return self._parse_lines(raw), dict(empty_stats)

        # ---- 有人设路径 ----
        inject, _ = _trim_profile(profile)
        system = _build_coldline_system(inject)
        user = _build_coldline_user(cold_event, recent_topics, latest_portrait)
        raw = self.client.chat(
            system, user, temperature=0.8,
            timeout_override=self.timeout_seconds, purpose="realtime",
        )
        lines = self._parse_lines(raw) if raw else []
        # 禁区硬过滤（用完整版 forbidden_topics，不依赖注入裁剪结果）
        forbidden = [kw for kw in (profile.get("forbidden_topics") or []) if kw]
        filtered_count = 0
        if forbidden:
            before = len(lines)
            lines, filtered_count = _filter_forbidden(lines, forbidden)
        # 口头禅校验（软约束：不丢弃，只统计；用完整版 catchphrases/fan_call）
        hits = _check_catchphrase(
            lines,
            [cp for cp in (profile.get("catchphrases") or []) if cp],
            profile.get("fan_call") or "",
        )
        return lines, {
            "anchor_used": True,
            "anchor_name": str(profile.get("name") or ""),
            "catchphrase_hit_count": hits,
            "forbidden_filtered_count": filtered_count,
        }

    @staticmethod
    def _parse_lines(raw: str) -> List[str]:
        lines: List[str] = []
        parsed = _loads_json(raw)
        if isinstance(parsed, dict) and isinstance(parsed.get("lines"), list):
            parsed = parsed["lines"]  # v2.3 有人设版输出 {"lines": [...]}
        if isinstance(parsed, list):
            candidates = [str(x) for x in parsed]
        else:
            # 兜底：模型没给 JSON 数组，按行/分号拆裸文本
            candidates = _LINE_SPLIT_RE.split(_strip_fence(raw))
        for item in candidates:
            text = _INDEX_PREFIX_RE.sub("", str(item)).strip().strip("\"'“”‘’")
            if not text:
                continue
            lines.append(_clamp_line(text))
            if len(lines) >= 3:
                break
        return lines


class RecommendService:
    """商品推荐理由：画像 + 候选商品 -> 与商品等长对齐的理由字符串列表。

    product_list 为空时直接返回 [] 且不调用 API（任务书 3.2）。
    第 i 个理由对应第 i 个商品；模型漏给/给空时该位置为空串，由上层过滤落盘。
    """

    def __init__(self, client: LLMClient) -> None:
        self.client = client

    def generate(self, portrait: Dict[str, Any],
                 product_list: List[Dict[str, Any]]) -> List[str]:
        if not product_list:
            return []
        # 只发商品营销字段给模型，product_id 不出模型（仅落盘对齐用）
        items_for_llm = []
        for p in product_list:
            items_for_llm.append({
                "name": str(p.get("name") or ""),
                "category": str(p.get("category") or ""),
                "price": p.get("price", ""),
                "tags": p.get("tags") if isinstance(p.get("tags"), list) else [],
                "desc": str(p.get("desc") or ""),
            })
        user = (
            "观众画像（仅供你理解观众，不要在理由里提及画像/数据字眼）：\n"
            + json.dumps(portrait, ensure_ascii=False)
            + f"\n候选商品共 {len(items_for_llm)} 个（输出数组必须与此顺序一一对应）：\n"
            + json.dumps(items_for_llm, ensure_ascii=False)
        )
        raw = self.client.chat(RECOMMEND_SYSTEM, user, temperature=0.7,
                               purpose="realtime")
        if raw is None:
            return []
        parsed = _loads_json(raw)
        if not isinstance(parsed, list):
            logger.warning("[推荐] 模型返回无法解析为 JSON 数组，本次跳过")
            return []
        reasons: List[str] = []
        for i in range(len(product_list)):
            reason = ""
            if i < len(parsed):
                reason = _INDEX_PREFIX_RE.sub("", str(parsed[i])).strip().strip("\"'“”‘’")
            reasons.append(reason[:40])
        return reasons


# ================================================================ 落盘
class DailyJsonlWriter:
    """按天切分的 JSONL 写入器（风格对齐 storage.JsonlStorage）。

    文件名：{output_dir}/{prefix}_YYYY-MM-DD.jsonl；每条写入后立即 flush。
    """

    def __init__(self, output_dir: str, prefix: str,
                 date: Optional[str] = None) -> None:
        self.output_dir = output_dir
        self.prefix = prefix
        # date: 强制写到指定日期（回填历史时用，如 scripts/backfill_cold_events.py）。
        # 不传 = 按当前系统日期切分（生产行为不变）。
        self._date_override = date or None
        os.makedirs(output_dir, exist_ok=True)
        self._file = None
        self._current_date: Optional[str] = None
        self._lock = threading.RLock()

    def _today(self) -> str:
        if self._date_override:
            return self._date_override
        return time.strftime("%Y-%m-%d", time.localtime())

    def _path_for(self, date_str: str) -> str:
        return os.path.join(self.output_dir, f"{self.prefix}_{date_str}.jsonl")

    def _ensure_file(self) -> None:
        """确保当前文件句柄对应当天日期，跨天则切文件。

        ★ 与 storage.JsonlStorage._ensure_file / monitor._DateJsonl._ensure_file
          同源同修（2026-11-01 审计 P0）：先 open 成功再换 _file/_current_date。
          原实现先 close 旧句柄再 open，open 失败后 _current_date 已是新日期 →
          永不重试，且句柄已关闭 → 之后每条 LLM 落盘都抛 ValueError 被 worker
          兜底吞掉，界面看着在跑、coldline 却整天没有新行。
        """
        today = self._today()
        if self._file is not None and today == self._current_date:
            return
        path = self._path_for(today)
        new_file = open(path, "a", encoding="utf-8")   # 失败就抛，旧状态不变
        old_file = self._file
        old_date = self._current_date
        if old_file is not None:
            try:
                with file_lock(self._path_for(old_date)):
                    old_file.close()
            except BaseException:
                # A failed flush may still hold buffered bytes. Keep its handle
                # reachable for a locked retry, rather than flushing in __del__.
                new_file.close()
                raise
        self._file = new_file
        self._current_date = today
        logger.info("LLM 落盘文件: %s", path)

    def write(self, record: Dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._lock:
            self._ensure_file()
            # Separate rooms/backfills use separate writer objects and processes.
            # flush must finish before another writer can append its whole line.
            with file_lock(self._path_for(self._current_date)):
                self._file.write(line)
                self._file.flush()

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                # A previous failed flush may have left buffered bytes to close.
                with file_lock(self._path_for(self._current_date)):
                    self._file.close()
                self._file = None
                self._current_date = None


# ================================================================ worker 线程
# LLM 队列消息协议：
#   {"kind": "snapshot", "snapshot": <analyzer.get_snapshot() 结果>}
#   {"kind": "event",    "event":    <cold_start/cold_end 事件 dict>}
#   None 为退出哨兵
def _extract_topics(snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """从最近一次快照提取冷场话题上下文（高频词 + 最新 10 条代表性弹幕）。"""
    if not snapshot:
        return {"top_words": [], "recent_danmaku": []}
    top_words = [
        w.get("word") for w in (snapshot.get("top_words") or [])[:10]
        if isinstance(w, dict) and w.get("word")
    ]
    recent_danmaku = [
        {"uname": d.get("uname", ""), "text": d.get("text", "")}
        for d in (snapshot.get("representative_danmaku") or [])[-10:]
        if isinstance(d, dict)
    ]
    return {"top_words": top_words, "recent_danmaku": recent_danmaku}


def run_llm_worker(q: "queue.Queue", cfg: LLMConfig,
                   output_dir: str = "data",
                   products: Optional[List[Dict[str, Any]]] = None,
                   room_id: str = "",
                   room_tag: str = "",
                   shared_state: Optional[SharedState] = None,
                   anchor_cfg: Optional[AnchorConfig] = None,
                   stop_event: Optional[threading.Event] = None,
                   control_epoch=None,
                   worker_generation=None,
                   live_generation=None,
                   request_guard: Optional[Callable[[], bool]] = None,
                   on_state: Optional[Callable[[str], None]] = None) -> None:
    """LLM worker 线程入口：消费 snapshot/event 消息，调模型并落盘。

    :param q: 消息队列；None 为退出哨兵
    :param cfg: 已加载的 LLMConfig；自动许可与代际在消费及发送前分别校验
    :param output_dir: 落盘目录（与采集落盘同目录）
    :param products: 候选商品列表（组员选品接口预留，当前可传 []）
    :param room_id: 房间号（main.py 传入 cfg.web_rid 原始值），写入每条落盘 record
    :param room_tag: 房间文件标识（与 stats/events 同名同 tag）；非空时画像按
                     房间独立落盘 portrait_<room_tag>_<date>.jsonl，并写入 record
    :param shared_state: 跨线程共享容器（client._resolve_room 解析后写 room_id），
                         人设卡惰性加载依据；None 时人设功能禁用
    :param anchor_cfg: 人设卡配置；enabled=false 时行为与 v2.2 完全一致
    :param stop_event: 可选停止信号
    :param request_guard: 标准应用必须绑定当前在播连接及其代际；未知状态拒绝自动调用
    """
    products = products or []
    anchor_cfg = anchor_cfg or AnchorConfig()
    source_path = cfg._source_path

    def notify(state):
        if on_state is not None:
            try:
                on_state(state)
            except Exception:
                logger.warning("LLM 生命周期状态通知失败")
                return False
        return True

    def notify_final(state):
        if notify(state):
            return True
        if notify("error"):
            return False
        raise RuntimeError("LLM worker 最终状态通知失败") from None

    def control_current():
        if stop_event is not None and stop_event.is_set():
            return False
        if not source_path:
            return True  # Trusted in-memory callers retain their static contract.
        try:
            if request_guard is None or request_guard() is not True:
                return False
        except Exception:
            return False
        from llm_client import application_control
        control = application_control(config_path=source_path, data_dir=output_dir,
                                      tag=room_tag, automatic=True)
        return control["allowed"] and control["epoch"] == control_epoch

    if source_path and control_epoch is None:
        from llm_client import application_control
        control_epoch = application_control(config_path=source_path, data_dir=output_dir,
                                            tag=room_tag, automatic=True)["epoch"]
    if not control_current():
        notify_final("stopped")
        return

    # metrics writer：独立前缀 llm_metrics，仅 metrics_enabled 时落盘
    metrics_writer: Optional[DailyJsonlWriter] = None
    if cfg.metrics_enabled:
        metrics_writer = DailyJsonlWriter(output_dir, "llm_metrics")

    # metrics 延迟落盘机制：冷场调用的 metrics 行需要附加 catchphrase_hit_count /
    # forbidden_filtered_count（调用结束后才可知），故 ctx 激活期间 record 进
    # pending，调用结束统一 flush；其他调用（画像/推荐/探测）直接写。
    metrics_ctx_active = False
    metrics_pending: List[Dict[str, Any]] = []
    metric_data_date: Optional[str] = None

    def source_date(value):
        if not isinstance(value, dict) or type(value.get("ts")) not in (int, float):
            return None
        try:
            return time.strftime("%Y-%m-%d", time.localtime(value["ts"]))
        except (ValueError, OverflowError, OSError):
            return None

    def _write_metrics(record: Dict[str, Any]) -> None:
        if metrics_writer is None:
            return
        # 归属键：以前 metrics 行不带 room_tag，事后只能看到「今天 120 次调用」
        # 却分不清是哪一路花的钱（2026-10-25 排查冤种小徐无画像时卡住）。
        if room_tag and "room_tag" not in record:
            record["room_tag"] = room_tag
        if not record.get("probe"):
            if not record.get("purpose"):
                record["purpose"] = "realtime"
            if metric_data_date is not None:
                record.setdefault("data_date", metric_data_date)
        try:
            metrics_writer.write(record)
        except Exception as e:
            logger.debug("metrics 落盘异常（忽略）: %s", type(e).__name__)

    def _on_metrics(record: Dict[str, Any]) -> None:
        if metrics_ctx_active:
            metrics_pending.append(record)
        else:
            _write_metrics(record)

    def _flush_metrics(extra: Optional[Dict[str, Any]] = None) -> None:
        for rec in metrics_pending:
            if extra:
                rec.update(extra)
            _write_metrics(rec)
        metrics_pending.clear()

    router = cfg.build_router(on_metrics=_on_metrics, tag=room_tag, automatic=True,
                             control_epoch=control_epoch, stop_event=stop_event,
                             control_data_dir=output_dir, request_guard=request_guard)
    def close_resource(resource, label, optional=False):
        if resource is None:
            return True
        try:
            close = getattr(resource, "close", None)
            if optional and not callable(close):
                return True  # Trusted legacy in-memory router compatibility.
            close()
        except Exception:
            logger.error("LLM worker 资源清理失败（%s）", label)
            return False
        return True

    def close_router():
        return close_resource(router, "router", optional=True)
    if router is None or not router.is_available:
        logger.error(
            "LLM worker 启动失败：无可用 provider（请检查 config.json 的 "
            "llm.providers 是否填了 api_key）"
        )
        close_resource(metrics_writer, "metrics")
        close_router()
        notify_final("error")
        return

    portrait_svc = PortraitService(router)
    coldline_svc = ColdlineService(
        router, timeout_seconds=cfg.coldline_timeout_seconds)
    recommend_svc = RecommendService(router)

    # 画像按房间独立落盘（room_tag 非空时）；冷场/推荐仍为全局单文件，保持既有行为
    portrait_prefix = f"portrait_{room_tag}" if room_tag else "portrait"
    writers = {
        "portrait": DailyJsonlWriter(output_dir, portrait_prefix),
        "coldline": DailyJsonlWriter(output_dir, "coldline"),
        "recommend": DailyJsonlWriter(output_dir, "recommend"),
    }
    last_snapshot: Optional[Dict[str, Any]] = None
    last_portrait: Optional[Dict[str, Any]] = None
    # 冷场台词防抖：上次成功产出台词的处理时刻；<coldline_min_interval_seconds 跳过
    last_coldline_ts: float = 0.0

    # ---- 人设卡惰性加载状态 ----
    loaded_room: Optional[str] = None      # 已成功加载的 room_id
    loaded_profile: Optional[Dict[str, Any]] = None  # 完整版人设（过滤/校验用）
    anchor_failed_room: Optional[str] = None  # 加载失败的 room（缓存失败防刷屏）
    warned_room_pending = False            # room_id 未解析的 WARNING 只打一次/轮
    # 口头禅连续零命中降级（仅 anchor_used=true 轮次计数；重启 main.py 重置）
    consecutive_zero_hits = 0
    session_anchor_disabled = False        # 达 5 次后本场剩余时间不再注入

    def _ensure_profile() -> Optional[Dict[str, Any]]:
        """惰性加载人设卡：按 tag 找（room_id 兜底），缓存加载结果。

        ★ 2026-10-28：不再等 `shared_state.room_id`。tag 是配置里就有的，
          开播第一秒就能加载；只有既没 tag 又没解析出 room_id 才降级。
        """
        nonlocal loaded_room, loaded_profile, anchor_failed_room, \
            warned_room_pending
        if not anchor_cfg.enabled or session_anchor_disabled:
            return None
        rid = shared_state.room_id if shared_state is not None else None
        key = room_tag or rid
        if not key:
            if not warned_room_pending:
                logger.warning(
                    "[人设] tag 与 room_id 都为空，本轮降级无人设")
                warned_room_pending = True
            return None
        warned_room_pending = False
        if loaded_room == key:
            return loaded_profile
        if anchor_failed_room == key:
            return None
        profile, err = load_anchor_profile(anchor_cfg.profile_dir, rid or "", room_tag)
        if err:
            logger.warning("[人设] 加载失败（本场不再重试）: %s", err)
            anchor_failed_room = key
            return None
        loaded_room = key
        if profile is None:
            # 没配/config 空模板等全空字段：等同无人设（正常路径，不打 WARNING）
            logger.info("[人设] tag=%s 未配置人设卡，按无人设模式运行", key)
            loaded_profile = None
        else:
            loaded_profile = profile
            # 注入长度预检：超 300 中文字符时 WARNING 记录被裁字段
            _, trimmed_fields = _trim_profile(profile)
            if trimmed_fields:
                logger.warning(
                    "[人设] 注入文本超 %s 字，已裁剪字段: %s",
                    _INJECT_MAX_CHARS, ",".join(trimmed_fields))
            logger.info(
                "[人设] 已加载人设 name=%s tag=%s room_id=%s category=%s",
                profile["name"], key, rid or "-", profile["category"])
        return loaded_profile

    logger.info(
        "LLM worker 已启动 primary=%s active=%s model=%s timeout=%ss "
        "coldline_timeout=%ss 画像间隔=%ss 冷场台词=%s(最小间隔=%ss) "
        "推荐=%s metrics=%s 人设=%s(profile_dir=%s) room_id=%s 商品数=%s",
        router.primary or "(无)", router.active or "(无)", router.active_model,
        cfg.timeout_seconds, cfg.coldline_timeout_seconds,
        cfg.portrait_interval_seconds, cfg.coldline_enabled,
        cfg.coldline_min_interval_seconds, cfg.recommend_enabled,
        cfg.metrics_enabled,
        "on" if anchor_cfg.enabled else "off", anchor_cfg.profile_dir,
        room_id or "(空)", len(products),
    )

    def handle_snapshot(snapshot: Dict[str, Any]) -> None:
        nonlocal last_snapshot, last_portrait, metric_data_date
        last_snapshot = snapshot
        metric_data_date = source_date(snapshot)
        cw = snapshot.get("cold_window") or {}
        # 冷场续播必须放在画像熔断之前：冷场恰恰是弹幕最少的场景，
        # 放到后面会被 `_cw_count < portrait_min_danmaku` 的 return 直接吃掉。
        _maybe_coldline_repeat(cw)
        # 空窗熔断：冷场窗口内弹幕条数 < 阈值时跳过画像 LLM 调用（0 成本）。
        # last_snapshot 已更新（冷场台词依赖它），熔断不影响冷场台词路径。
        _cw_count = int(cw.get("count") or 0)
        _cw_sec = int(cw.get("window_seconds") or cfg.portrait_interval_seconds)
        if _cw_count < cfg.portrait_min_danmaku:
            logger.info(
                "[画像] 近%ss 弹幕 %s < 阈值 %s，跳过本次画像调用（0 成本）",
                _cw_sec, _cw_count, cfg.portrait_min_danmaku)
            return
        profile = _ensure_profile()
        portrait = portrait_svc.generate(snapshot, profile=profile)
        if portrait is None:
            logger.warning("[画像] 本次画像生成失败或 API 降级，跳过落盘")
            return
        last_portrait = portrait
        writers["portrait"].write({
            "ts": int(time.time()),
            "room_id": room_id,
            "room_tag": room_tag,
            "portrait": portrait,
        })
        logger.info(
            "[画像] 已产出并落盘：兴趣=%s 消费意向=%s 情感=%s 建议=%s条",
            "/".join(portrait["interest_topics"][:5]) or "(无)",
            portrait["consumption_intent"]["level"],
            portrait["sentiment"], len(portrait["suggestions"]),
        )
        # 推荐：画像更新后，开关打开且存在候选商品时才调用
        if cfg.recommend_enabled and products:
            reasons = recommend_svc.generate(portrait, products)
            items = [
                {"product_id": str(p.get("product_id", idx + 1)), "reason": reason}
                for idx, (p, reason) in enumerate(zip(products, reasons))
                if reason
            ]
            if items:
                writers["recommend"].write({
                    "ts": int(time.time()),
                    "room_id": room_id,
                    "items": items,
                })
                logger.info("[推荐] 已产出 %s 条商品推荐理由并落盘", len(items))

    def _produce_coldline(event: Dict[str, Any], event_name: str) -> None:
        """生成一组冷场台词并落盘。

        :param event_name: 落盘 record 的 event 字段。
            "cold_start" = 进入冷场那一次的原始事件；
            "cold_continue" = 冷场持续中的补词（2026-10-25 新增，见 _maybe_coldline_repeat）。
            分开写是为了让复盘页按 cold_start 统计"冷场场次"时不被补词撑大。
        """
        nonlocal last_coldline_ts, consecutive_zero_hits, session_anchor_disabled, \
            metrics_ctx_active, metric_data_date
        metric_data_date = source_date(event) or source_date(last_snapshot)
        profile = _ensure_profile()
        topics = _extract_topics(last_snapshot)
        # 冷场 metrics 行需附加人设计数字段，激活延迟落盘
        metrics_ctx_active = True
        try:
            lines, stats = coldline_svc.generate(
                event, topics, profile=profile, latest_portrait=last_portrait)
        finally:
            metrics_ctx_active = False
        anchor_used = bool(stats["anchor_used"])
        # 口头禅连续零命中降级：仅 anchor_used=true 轮次计数
        if anchor_used:
            if stats["catchphrase_hit_count"] > 0:
                consecutive_zero_hits = 0
            else:
                consecutive_zero_hits += 1
                if consecutive_zero_hits >= 5:
                    session_anchor_disabled = True
                    logger.warning(
                        "[人设] 连续 %s 轮口头禅零命中，本场剩余时间不再注入人设"
                        "（可能是口头禅过于特殊导致 LLM 难以命中，"
                        "建议运营调整人设卡）", consecutive_zero_hits)
        writers["coldline"].write({
            "ts": int(time.time()),
            "room_id": room_id,
            # room_tag 是复盘页唯一可靠的归属键：danmu/stats 落的是平台房间号
            # （如 7700495043394439936），coldline 落的是 web_rid（如 277671499254），
            # 两者不相等 → 复盘页按 room_id 过滤冷场会永远是 0（2026-10-25 定位）。
            "room_tag": room_tag,
            "event": event_name,
            "lines": lines,
            "anchor_used": anchor_used,
            "anchor_name": stats["anchor_name"],
            "catchphrase_hit_count": stats["catchphrase_hit_count"],
            "forbidden_filtered_count": stats["forbidden_filtered_count"],
        })
        # metrics 冷场行附加人设计数（论文"人设注入有效性"量化数据源）
        _flush_metrics({
            "catchphrase_hit_count": stats["catchphrase_hit_count"],
            "forbidden_filtered_count": stats["forbidden_filtered_count"],
        })
        if lines:
            # 仅成功产出才更新 last_coldline_ts（防抖只锁成功产出，
            # 降级时下次冷场事件可立即重试）
            last_coldline_ts = time.time()
            for idx, line in enumerate(lines, 1):
                logger.info("[冷场台词]%s %s) %s",
                            "" if event_name == "cold_start" else "(续播)",
                            idx, line)
        else:
            logger.warning("[冷场台词] API 降级/过滤后无台词产出（仅落盘空记录）")

    def _maybe_coldline_repeat(cw: Dict[str, Any]) -> None:
        """冷场**持续中**按 coldline_repeat_seconds 补词（2026-10-25 新增）。

        背景：analyzer 的 cold_start 只在「进入冷场」跃迁时发一次。一场两小时
        一直冷着的直播，主播只会在开头收到 3 句话，之后提示全无 —— 交接文档里
        "台词像摆设"就是这个（实测研途 16:00 进冷场、全天只有 2 条 coldline）。
        这里用快照里的 cold_window.is_cold 当"仍在冷场"的依据，按间隔补词；
        事件语义不动（cold_events / 复盘统计口径保持原样）。
        """
        nonlocal last_coldline_ts
        if cfg.coldline_repeat_seconds <= 0 or not cfg.coldline_enabled:
            return
        if not cw.get("is_cold"):
            return
        now = time.time()
        if last_coldline_ts and now - last_coldline_ts < cfg.coldline_repeat_seconds:
            return
        event = {
            "event": "cold_start",
            "ts": int(now),
            "count": int(cw.get("count") or 0),
            "threshold": float(cw.get("threshold_enter") or 0),
        }
        logger.info(
            "[冷场续播] 仍在冷场（近%ss 弹幕 %s 条），距上组台词 %ss ≥ %ss，补一组",
            int(cw.get("window_seconds") or 0), event["count"],
            int(now - last_coldline_ts) if last_coldline_ts else -1,
            cfg.coldline_repeat_seconds)
        _produce_coldline(event, "cold_continue")

    def handle_event(event: Dict[str, Any]) -> None:
        nonlocal last_coldline_ts
        if event.get("event") != "cold_start":
            return
        if not cfg.coldline_enabled:
            return
        # 防抖：距上次成功产出台词不足最小间隔，跳过本次冷场事件
        now = time.time()
        if last_coldline_ts and \
                now - last_coldline_ts < cfg.coldline_min_interval_seconds:
            logger.debug(
                "[冷场台词] 距上次产出 %ss < 最小间隔 %ss，跳过本次冷场事件",
                int(now - last_coldline_ts), cfg.coldline_min_interval_seconds,
            )
            return
        _produce_coldline(event, "cold_start")

    try:
        while True:
            if not control_current():
                break
            notify("waiting_input")
            # 周期性探测主 provider 是否恢复（仅在 active=backup 时实际触发）
            try:
                router.probe_and_maybe_switch()
            except Exception as e:
                logger.debug("probe 异常（忽略）: %s", type(e).__name__)
            try:
                item = q.get(timeout=0.25)
            except queue.Empty:
                continue
            if item is None:
                break
            if not control_current():
                break
            if source_path and item.get("_llm_epoch") != control_epoch:
                continue
            if worker_generation is not None and item.get("_llm_generation") != worker_generation:
                continue
            if live_generation is not None and item.get("_llm_live_generation") != live_generation:
                continue
            notify("running")
            # 单条消息异常不允许杀掉 worker（红线：不影响采集链路）
            try:
                kind = item.get("kind")
                if kind == "snapshot":
                    handle_snapshot(item.get("snapshot") or {})
                elif kind == "event":
                    handle_event(item.get("event") or {})
                else:
                    logger.warning("LLM 队列收到未知消息类型，已丢弃: %s", kind)
            except Exception as e:
                logger.warning("处理 LLM 消息异常，已跳过: %s", e)
    finally:
        cleanup_failed = False
        for name, writer in writers.items():
            if not close_resource(writer, name):
                cleanup_failed = True
        if not close_resource(metrics_writer, "metrics"):
            cleanup_failed = True
        if not close_router():
            cleanup_failed = True
        notification_ok = False
        try:
            notification_ok = notify_final("error" if cleanup_failed else "stopped")
        finally:
            if cleanup_failed or not notification_ok:
                logger.error("LLM worker 线程已退出，资源清理或最终状态通知未全部成功")
            else:
                logger.info("LLM worker 线程已退出")
