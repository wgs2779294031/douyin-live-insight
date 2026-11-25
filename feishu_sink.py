"""飞书落盘模块（v3.0）：把「窗口聚合指标」推送到飞书/多维表格。

为什么不是逐条弹幕
------------------------------------------------
弹幕峰值可达几十条/秒，而飞书侧无论走「多维表格自动化 webhook」还是
「开放平台 API」，都有频率与次数限制——逐条写必然被限流，且很快撞表格
记录数上限（多维表格单表记录数量级有限）。

因此本模块只落 **按窗口聚合的指标行**：一行 = 一个房间的一个时间窗。
原始弹幕的全量数据仍由 storage.JsonlStorage 落本地 JSONL，二者分层、
互不替代——本地是原始资产，飞书是分析视图。

三种后端（backend）
------------------------------------------------
dry_run  本地 JSONL，零网络。默认值，开发与自测用，不产生任何飞书调用。
webhook  飞书多维表格「自动化 → 接收到 webhook 时」流程的唯一地址。
         只需一个 URL（可选 Bearer token），**不需要自建应用 / app_id / app_secret**。
         ⚠️ 官方限制（见飞书帮助中心「使用自动化流程的 webhook 触发」）：
           - 请求体 ≤ 4 MB
           - 整个多维表格 50 次/秒；单个流程 5 次/秒
           - 未开启「凭证校验」的流程仅 1 次/秒
           - 自动化运行次数（与工作流共用）基础版 200 次/月、
             专业版 50,000 次/月、旗舰版 500,000 次/月
app      飞书开放平台**自建应用**直写多维表格 API。适合高频场景，也是
         唯一能绕开「自动化运行次数」限制的路。凭证 `app_id` / `app_secret`
         换 `tenant_access_token`（有效期 2 小时，本模块自动缓存刷新），
         再调 `POST /open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records`
         新增一行。接口频率 50 次/秒。

         接这个后端之前，飞书侧必须先做完（缺一步就是 403）：
           1. 个人飞书号需先「升级为团队」（免费、一人也可、无需营业执照），
              否则开发者后台不让建自建应用
           2. 建「企业自建应用」→ 开通权限 **查看、评论、编辑和管理多维表格**
           3. ★ 在多维表格页面「...」→「更多」→「添加文档应用」把该应用加进来。
              **顺序坑：应用必须先已开通至少一个多维表格 API 权限，否则这一步
              在搜索框里根本搜不到这个应用。**
           4. 记下 `app_id` / `app_secret`，以及多维表格的 `app_token` 与 `table_id`

         ⚠️ 两个只在真机上才撞得到的坑（都在下面代码里有对应处理）：
           - **1254291 Write conflict：同一个数据表不支持并发写** → 多房间同时写
             同一张表必然撞。本模块用**进程级按表串行锁**解决；更稳的做法是
             **一房间一张表**（同一个多维表格里建多张表，授权只需做一次）。
           - **99991403 / HTTP 429 配额用尽** → 重试无用，直接判定失败并告警，
             不要白白重试三轮。

旁路原则（与 LLM 层完全一致）
------------------------------------------------
队列 + 独立 daemon 线程；队列满直接丢弃。飞书慢、超时或挂掉，绝不允许
阻塞采集主循环与规则分析线程。发送失败按 max_retries 重试，仍失败则计入
failed 并丢弃该行（不重排、不阻塞后续行）。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "config.json"

# 已实现的后端（"app" 在 v3.2 实现）
_BACKENDS_IMPLEMENTED = ("dry_run", "webhook", "app")
# 预留但尚未实现的后端；填了会在 validate 里明确报错，避免静默假成功
_BACKENDS_RESERVED: tuple = ()

# ---- 飞书开放平台（自建应用）路径 ----
_TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
_RECORD_PATH = "/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"

# ---- 飞书错误码（只列本模块需要区别对待的）----
# 配额用尽：当月 API 调用量已超上限 → 重试无用，必须显式失败 + 告警
_ERR_QUOTA_EXHAUSTED = 99991403
# 同一数据表并发写冲突 → 串行锁 + 重试可解
_ERR_WRITE_CONFLICT = 1254291
# 请求过快 → 退避重试可解
_ERR_TOO_MANY_REQUEST = 1254290
# 请求超时 → 重试可解
_ERR_TIMEOUT = 1255040
# 令牌失效相关 → 作废缓存令牌后重试可解
_ERR_TOKEN_INVALID = (99991663, 99991661, 99991664)


class FeishuConfigError(RuntimeError):
    """飞书落盘配置缺失或非法。"""


class FeishuApiError(RuntimeError):
    """飞书开放平台接口返回非 0 code 或非 2xx。

    :param retryable: False 表示「重试也没用」（如当月配额用尽），
        send() 会立刻放弃而不是白重试 max_retries 轮。
    """

    def __init__(self, message: str, code: Optional[int] = None,
                 http_status: Optional[int] = None, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.retryable = retryable


# ================================================================ 按表串行锁
# ★ 飞书 1254291 明确写着「同一个数据表(table) 不支持并发调用写接口」。
#   多房间下每个房间一套 FeishuSink + 独立线程，若它们指向同一张表就会撞。
#   这里用**进程级**的按表锁，把所有写同一张表的调用串起来 —— 不加锁的话
#   表现为「偶发丢行」，比直接报错难查得多。
_TABLE_LOCKS: Dict[str, threading.Lock] = {}
_TABLE_LOCKS_GUARD = threading.Lock()


def table_lock(app_token: str, table_id: str) -> threading.Lock:
    """取（并复用）某个数据表的进程级写锁。"""
    key = f"{app_token}/{table_id}"
    with _TABLE_LOCKS_GUARD:
        lock = _TABLE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _TABLE_LOCKS[key] = lock
        return lock


# ================================================================ 配置
@dataclass
class FeishuSinkConfig:
    """飞书落盘配置（config.json 的 feishu 节）。默认关闭，开启前零影响。"""

    enabled: bool = False
    # dry_run | webhook（app 预留未实现）
    backend: str = "dry_run"
    # 多维表格自动化 webhook 地址（backend=webhook 时必填）
    webhook_url: str = ""
    # webhook 流程若开了「凭证校验(Bearer token)」则填这里；未开留空
    bearer_token: str = ""
    # dry_run 落盘目录（与采集数据同目录，便于一起归档）
    output_dir: str = "data"
    # 单次 HTTP 超时（秒）
    timeout_seconds: float = 5.0
    # 失败重试次数（不含首次）
    max_retries: int = 2
    # 重试退避基数（秒），第 n 次重试等待 backoff * n
    retry_backoff_seconds: float = 1.5
    # ★ 控频：两次实际发送之间的最小间隔（秒）。
    # 默认 300s（5 分钟一行），对应「基础版 200 次/月」这类额度很紧的套餐；
    # 走自建应用 API 或更高套餐时可调小。
    min_interval_seconds: float = 300.0
    # 仅当 LLM 层未启用时，规则层推送快照的间隔（秒）。
    # LLM 层启用时取两者较小值，保证两边都够用。
    snapshot_interval_seconds: int = 60
    # 旁路队列容量：满即丢弃（保证不阻塞上游）
    queue_maxsize: int = 200
    # 聚合进指标行的条数
    top_words_n: int = 10
    active_top_n: int = 5

    # ---- app 后端（自建应用直写多维表格）----
    # 凭证。★ 优先从环境变量 FEISHU_APP_ID / FEISHU_APP_SECRET 读，
    #     config.json 作为兜底（config.json 已在 .gitignore 内，不会入库）。
    app_id: str = ""
    app_secret: str = ""
    # 多维表格的 app_token：表格 URL 形如 https://xxx.feishu.cn/base/<app_token>?table=<table_id>
    app_token: str = ""
    # 默认数据表 table_id。单房间时填这个即可。
    table_id: str = ""
    # 多房间：房间标识（rooms[i].label 安全化后）→ table_id。
    # ★ 建议一房间一张表：飞书明确限制「同一张表不支持并发写」(1254291)，
    #   一表一房间天然规避；且授权是按多维表格（app_token）做的，加一次就够。
    table_id_map: Dict[str, str] = field(default_factory=dict)
    # 开放平台域名（私有化/海外版可改）
    api_base: str = "https://open.feishu.cn"
    # token 提前刷新余量（秒）。官方 token 有效 2 小时；剩余 <30 分钟时调用
    # 会返回新 token，所以 300s 这个值必然落在「会发新 token」的区间，不会白刷。
    token_refresh_margin_seconds: float = 300.0

    def validate(self) -> None:
        """开启时校验；关闭时直接返回（保证未启用时零影响）。"""
        if not self.enabled:
            return
        backend = str(self.backend).strip()
        if backend in _BACKENDS_RESERVED:
            raise FeishuConfigError(
                f"feishu.backend={backend!r} 尚未实现（v3.0 仅支持 "
                f"{list(_BACKENDS_IMPLEMENTED)}）。高频场景请改走自建应用 API，"
                f"见 feishu_sink 模块文档。"
            )
        if backend not in _BACKENDS_IMPLEMENTED:
            raise FeishuConfigError(
                f"未知 feishu.backend {self.backend!r}，可选："
                f"{list(_BACKENDS_IMPLEMENTED)}（另有预留 {list(_BACKENDS_RESERVED)}）"
            )
        if backend == "webhook" and not str(self.webhook_url).strip():
            raise FeishuConfigError(
                "feishu.backend=webhook 时必须填 feishu.webhook_url"
                "（多维表格 → 自动化 → 接收到 webhook 时 → 复制地址）"
            )
        if backend == "app":
            missing = [k for k in ("app_id", "app_secret", "app_token")
                       if not str(getattr(self, k)).strip()]
            if missing:
                raise FeishuConfigError(
                    f"feishu.backend=app 缺少必填项：{missing}。"
                    f"app_id/app_secret 在开发者后台「凭证与基础信息」取；"
                    f"app_token 是多维表格 URL 里 /base/ 后面那段"
                    f"（也可用环境变量 FEISHU_APP_ID / FEISHU_APP_SECRET 提供凭证）"
                )
            if not str(self.table_id).strip() and not self.table_id_map:
                raise FeishuConfigError(
                    "feishu.backend=app 必须填 feishu.table_id（单房间），"
                    "或 feishu.table_id_map（多房间：房间标识 → table_id）"
                )
            if not str(self.api_base).startswith(("http://", "https://")):
                raise FeishuConfigError("feishu.api_base 必须是 http(s) 地址")
        if self.timeout_seconds <= 0:
            raise FeishuConfigError("feishu.timeout_seconds 必须为正数")
        if self.max_retries < 0:
            raise FeishuConfigError("feishu.max_retries 不能为负")
        if self.min_interval_seconds < 0:
            raise FeishuConfigError("feishu.min_interval_seconds 不能为负")
        if self.token_refresh_margin_seconds < 0:
            raise FeishuConfigError("feishu.token_refresh_margin_seconds 不能为负")


def load_feishu_config(path: str = DEFAULT_CONFIG_PATH) -> FeishuSinkConfig:
    """从 config.json 的 feishu 节加载配置。

    文件不存在 / 无 feishu 节 / 节不是对象 → 返回默认配置（enabled=False）。
    未知键忽略（与 llm/anchor 节同风格，文件多写键不报错）。

    ★ 凭证可用环境变量覆盖：`FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_APP_TOKEN`
    （config.json 已在 .gitignore 内，但环境变量是更干净的隔离方式）。
    """
    raw: Dict[str, Any] = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            section = data.get("feishu")
            if isinstance(section, dict):
                raw = dict(section)
            elif section is not None:
                logger.warning("config 的 feishu 节不是对象，已忽略（用默认值）")
        except (OSError, ValueError) as e:
            logger.warning("读取 feishu 配置失败，用默认值: %s", e)

    for env_name, field_name in (("FEISHU_APP_ID", "app_id"),
                                 ("FEISHU_APP_SECRET", "app_secret"),
                                 ("FEISHU_APP_TOKEN", "app_token")):
        env_val = os.environ.get(env_name)
        if env_val:
            raw[field_name] = env_val

    # table_id_map 必须是 str→str；脏数据只告警不炸（多写键不报错是本节的风格）
    if "table_id_map" in raw:
        mapping = raw["table_id_map"]
        if mapping is None:
            raw.pop("table_id_map")
        elif isinstance(mapping, dict):
            raw["table_id_map"] = {str(k).strip(): str(v).strip()
                                   for k, v in mapping.items()}
        else:
            logger.warning("feishu.table_id_map 不是对象，已忽略")
            raw.pop("table_id_map")

    known = {k: v for k, v in raw.items() if k in FeishuSinkConfig.__dataclass_fields__}
    return FeishuSinkConfig(**known)


# ================================================================ 聚合
# ★ 口径说明（v3.0 实测修正，改字段前务必先读）
# 1) analyzer.get_snapshot() 的 type_distribution / total_records 是
#    **本次采集以来的累计值**（只有「时间戳跳跃」重置窗口时才会清零），
#    不是「本窗口条数」。故本模块同时输出 _cum（累计）与 _delta（相对上
#    一行）两套字段 —— 想看「这段时间窗发生了什么」用 _delta。
# 2) active_top 已被规则层截断到 analyzer.active_top_n（默认 5），
#    所以 len(active_top) 是「可见条数」而非「活跃用户总数」。真实总数
#    本层拿不到（规则层不暴露），故输出 active_users_visible +
#    active_top_cap + active_users_capped，由调用方自行判断是否被截断。
# 3) cold_* 系列来自 cold_window（默认 60 秒滚动窗），与第 1 条的累计
#    区间**不是同一个窗口** —— 故单独输出 cold_window_sec。
_CUM_KEYS = ("danmaku", "enter", "like", "gift", "total_records")


def build_row(
    snapshot: Dict[str, Any],
    room_id: Optional[str] = None,
    platform: str = "douyin",
    top_words_n: int = 10,
    active_top_n: int = 5,
    prev_cold_watermark: int = 0,
    prev_cum: Optional[Dict[str, int]] = None,
    prev_ts: int = 0,
    active_top_cap: int = 0,
) -> Dict[str, Any]:
    """把 AnalyzerConfig.get_snapshot() 的实时快照压成一行飞书指标。

    设计约束：字段名用英文扁平键（便于多维表格建列 / 自动化字段映射），
    只取**平台可核算的原值**，不做任何推断或评分；**字段名必须自带口径**
    —— 累计带 `_cum`、增量带 `_delta`、配置上限带 `_cap`。

    :param prev_cold_watermark: 上一次已上报的冷场事件时间戳上限；
        用于计算「本行新增冷场次数」（快照里的 cold_events 是累计列表，
        不能直接用 len，因为它可能被截断）。
    :param prev_cum: 上一行的累计计数（danmaku/enter/like/gift/total_records）；
        为 None 表示这是第一行，所有 _delta 记 0。
    :param prev_ts: 上一行的 ts；用于算 since_prev_sec。
    :param active_top_cap: 规则层的 active_top_n（活跃用户列表被截断的上限）；
        传 0 表示未知，此时 active_users_capped 恒为 0。
    :return: 扁平 dict。
    """
    dist = snapshot.get("type_distribution") or {}
    win = snapshot.get("window") or {}
    cold = snapshot.get("cold_window") or {}
    cold_events = snapshot.get("cold_events") or []
    top_words = snapshot.get("top_words") or []
    active_top = snapshot.get("active_top") or []

    ts = int(snapshot.get("ts") or 0)
    latest_cold_ts = max(
        (int(e.get("ts", 0)) for e in cold_events if isinstance(e, dict)), default=0
    )
    new_cold_events = sum(
        1
        for e in cold_events
        if isinstance(e, dict) and int(e.get("ts", 0)) > int(prev_cold_watermark)
    )

    # —— 累计计数 + 相对上一行的增量。累计值会被规则层「时间戳跳跃」清零，
    #    故不能无条件相减：变小即视为重置，此时当前值本身就是本段增量。
    cum: Dict[str, int] = {
        "danmaku": int(dist.get("danmaku") or 0),
        "enter": int(dist.get("enter") or 0),
        "like": int(dist.get("like") or 0),
        "gift": int(dist.get("gift") or 0),
        "total_records": int(snapshot.get("total_records") or 0),
    }
    prev = prev_cum or {}
    deltas: Dict[str, int] = {}
    cum_reset = 0
    for k in _CUM_KEYS:
        p = prev.get(k)
        if p is None:
            deltas[k] = 0
        elif cum[k] < p:
            cum_reset = 1
            deltas[k] = cum[k]
        else:
            deltas[k] = cum[k] - p

    n_active = len(active_top)
    cap = int(active_top_cap or 0)

    row: Dict[str, Any] = {
        # —— 身份与时间
        "ts": ts,
        "date": time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "",
        "room_id": str(room_id) if room_id else "",
        "platform": platform,
        # 累计区间：自本次采集开始（或自上次「时间戳跳跃」重置）起
        "cum_window_start_ts": int(win.get("start_ts") or 0),
        "cum_window_sec": int(win.get("duration_sec") or 0),
        # 本行与上一行的间隔（首行 0）
        "since_prev_sec": (ts - int(prev_ts)) if prev_ts else 0,
        # 1 = 累计计数被规则层重置过，本行 _delta 从 0 重新起算
        "cum_reset": cum_reset,
        # —— 四类事件：累计值
        "danmaku_cum": cum["danmaku"],
        "enter_cum": cum["enter"],
        "like_cum": cum["like"],
        "gift_cum": cum["gift"],
        "total_records_cum": cum["total_records"],
        # —— 四类事件：相对上一行的增量 ★「这段时间窗发生了什么」看这一组
        "danmaku_delta": deltas["danmaku"],
        "enter_delta": deltas["enter"],
        "like_delta": deltas["like"],
        "gift_delta": deltas["gift"],
        "total_records_delta": deltas["total_records"],
        # —— 冷场（注意：滚动窗 = cold_window_sec，不是上面的累计区间）
        "is_cold": 1 if cold.get("is_cold") else 0,
        "cold_window_sec": int(cold.get("window_seconds") or 0),
        "cold_count_in_window": int(cold.get("count") or 0),
        "cold_threshold_enter": round(float(cold.get("threshold_enter") or 0.0), 2),
        "cold_threshold_exit": round(float(cold.get("threshold_exit") or 0.0), 2),
        "cold_mode": str(cold.get("mode") or ""),
        "new_cold_events": new_cold_events,
        "latest_cold_ts": latest_cold_ts,
        # —— 活跃用户：列表已被规则层截断，故只报「可见条数」+ 上限 + 截断标记
        "active_users_visible": n_active,
        "active_top_cap": cap,
        "active_users_capped": 1 if (cap > 0 and n_active >= cap) else 0,
        "active_top": " | ".join(
            str(u.get("uname") or "") for u in active_top[:active_top_n]
        ),
        # —— 高频词（文本化，避免多维表格多值字段类型限制）
        "top_words": " | ".join(
            f"{w.get('word')}({w.get('count')})"
            for w in top_words[:top_words_n]
            if isinstance(w, dict)
        ),
    }
    return row


# ================================================================ 发送
def _default_transport(
    url: str, headers: Dict[str, str], payload: Dict[str, Any], timeout: float
) -> int:
    """默认 HTTP 传输：返回状态码。测试可注入替身，避免任何真实网络调用。"""
    import requests  # 延迟导入：dry_run 后端无网络依赖

    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    return int(resp.status_code)


def _default_api_transport(
    url: str, headers: Dict[str, str], payload: Dict[str, Any], timeout: float
) -> Tuple[int, Dict[str, Any]]:
    """默认 HTTP 传输（开放平台）：返回 (状态码, 解析后的响应体)。

    与 `_default_transport` 分开是**故意的**：webhook 只需状态码，它的替身签名
    已被既有测试固定，不能改。这里多要一个响应体（飞书把错误码放在 body 里，
    只看 HTTP 200 会把「配额用尽」当成功）。

    响应体不是 JSON（如网关 5xx 返回 HTML）时返回空 dict，由调用方按状态码判断。
    """
    import requests  # 延迟导入：dry_run 后端无网络依赖

    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 - 非 JSON 响应，交给调用方按状态码处理
        body = {}
    if not isinstance(body, dict):
        body = {}
    return int(resp.status_code), body


# ================================================================ app 后端原语
class TenantTokenProvider:
    """缓存 `tenant_access_token`，到期前自动刷新。线程安全。

    官方说明：token 最长有效 2 小时；**剩余有效期 < 30 分钟时调用会返回新
    token，≥ 30 分钟时返回原有的**。所以提前 `refresh_margin` 秒（默认 300s）
    刷新，必然落在「会发新 token」的区间，不会白刷一轮。

    :param transport: 与 `_default_api_transport` 同签名的传输函数（测试注入替身）。
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        transport: Callable[[str, Dict[str, str], Dict[str, Any], float],
                            Tuple[int, Dict[str, Any]]],
        api_base: str = "https://open.feishu.cn",
        timeout: float = 5.0,
        refresh_margin_seconds: float = 300.0,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.refresh_margin = float(refresh_margin_seconds)
        self._transport = transport
        self._lock = threading.Lock()
        self._token = ""
        self._expire_at = 0.0
        # 统计：取了几次 token（用于排查「是不是每行都在换 token」）
        self.fetch_count = 0

    def get(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expire_at - self.refresh_margin:
                return self._token
            return self._fetch_locked()

    def invalidate(self) -> None:
        """作废当前 token（收到「令牌失效」错误码时调用），下次 get() 会重新取。"""
        with self._lock:
            self._token = ""
            self._expire_at = 0.0

    def _fetch_locked(self) -> str:
        url = f"{self.api_base}{_TOKEN_PATH}"
        headers = {"Content-Type": "application/json; charset=utf-8"}
        payload = {"app_id": self.app_id, "app_secret": self.app_secret}
        status, body = self._transport(url, headers, payload, self.timeout)
        code = body.get("code")
        if status != 200 or code != 0:
            # 凭证错误是配置问题，重试无用 → retryable=False
            raise FeishuApiError(
                f"获取 tenant_access_token 失败：HTTP {status} code={code} "
                f"msg={body.get('msg')!r}（检查 app_id / app_secret）",
                code=code if isinstance(code, int) else None,
                http_status=status,
                retryable=False,
            )
        token = str(body.get("tenant_access_token") or "")
        if not token:
            raise FeishuApiError("获取 tenant_access_token 成功但响应里没有 token",
                                 code=code, http_status=status, retryable=False)
        # expire 是「多少秒后过期」（通常 7200）
        expire_in = body.get("expire")
        try:
            expire_in = float(expire_in)
        except (TypeError, ValueError):
            expire_in = 7200.0
        self._token = token
        self._expire_at = time.time() + max(expire_in, 60.0)
        self.fetch_count += 1
        logger.info("已获取 tenant_access_token（%s 秒后过期，第 %d 次获取）",
                    int(expire_in), self.fetch_count)
        return token


def resolve_table_id(cfg: FeishuSinkConfig, room_tag: Optional[str] = None) -> str:
    """决定这个房间写哪张数据表。

    优先 `table_id_map[room_tag]`，其次默认 `table_id`。
    **两者都没有时直接抛错，不静默跳过** —— 「配了但没生效」是最难查的一类
    故障，宁可启动就炸。
    """
    mapping = cfg.table_id_map or {}
    tag = str(room_tag).strip() if room_tag else ""
    if tag and tag in mapping:
        return str(mapping[tag]).strip()
    default = str(cfg.table_id or "").strip()
    if default:
        return default
    if mapping and tag:
        raise FeishuConfigError(
            f"feishu.table_id_map 里没有房间 {tag!r} 对应的 table_id，"
            f"且未配兜底的 feishu.table_id → 该房间无法写入。"
            f"已配置的房间：{sorted(mapping)}"
        )
    raise FeishuConfigError(
        "feishu.backend=app 需要 feishu.table_id（单房间）"
        "或 feishu.table_id_map（多房间：房间标识 → table_id）"
    )


def row_to_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    """把指标行转成多维表格的 `fields`。

    只做**类型对齐**，不改语义、不改列名、不补默认值：
    - int / float / bool → 原样（对应数字列、复选框列）
    - str → 原样（对应文本列）
    - list / tuple → `" | "` 拼接（与 webhook 后端一致，规避多值字段类型限制）
    - None → **跳过该字段**（不写空值，避免「字段类型和值不匹配」1254015）
    """
    fields: Dict[str, Any] = {}
    for key, value in row.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            fields[key] = " | ".join(str(v) for v in value)
        elif isinstance(value, bool):
            # bool 是 int 的子类，必须放在 int 判断之前（保持 true/false）
            fields[key] = value
        elif isinstance(value, (int, float, str)):
            fields[key] = value
        else:
            fields[key] = str(value)
    return fields


def build_record_payload(row: Dict[str, Any],
                         client_token: Optional[str] = None) -> Dict[str, Any]:
    """构造新增记录的请求体。

    :param client_token: uuidv4 幂等键。**同一行重试时传同一个值**，
        这样「请求超时但其实写成功了」的重试不会插出重复行。
    """
    payload: Dict[str, Any] = {"fields": row_to_fields(row)}
    if client_token:
        payload["client_token"] = client_token
    return payload


def new_client_token() -> str:
    return str(uuid.uuid4())


class FeishuSink:
    """一个房间一个实例（room_id 绑定），负责把指标行送到飞书。

    线程安全：send() 内部用锁串行化，并强制 min_interval_seconds 控频。
    """

    def __init__(
        self,
        cfg: FeishuSinkConfig,
        room_id: Optional[str] = None,
        platform: str = "douyin",
        transport: Optional[Callable[[str, Dict[str, str], Dict[str, Any], float], int]] = None,
        active_top_cap: int = 0,
        room_tag: Optional[str] = None,
        api_transport: Optional[Callable[[str, Dict[str, str], Dict[str, Any], float],
                                         Tuple[int, Dict[str, Any]]]] = None,
    ) -> None:
        self.cfg = cfg
        self.room_id = room_id
        self.platform = platform
        # v3.1 多房间：已安全化的房间标识，仅用于拆分 dry_run 的本地文件名。
        # 单房间（legacy）传 None → 文件名与 v3.0 完全一致。**不进 webhook 载荷**
        # （载荷里的房间标识是 room_id 原值，不能被安全化后的短标识替代）。
        self.room_tag = (str(room_tag).strip() or None) if room_tag is not None else None
        # 规则层 active_top_n（用于标注 active_top 是否被截断）；0 = 未知
        self.active_top_cap = int(active_top_cap or 0)
        self._transport = transport or _default_transport
        self._api_transport = api_transport or _default_api_transport
        self._lock = threading.Lock()
        self._last_send_ts = 0.0
        self._cold_watermark = 0
        # 上一行的累计计数与 ts（用于算 _delta / since_prev_sec）
        self._prev_cum: Optional[Dict[str, int]] = None
        self._prev_ts = 0
        self._file = None
        self._current_date: Optional[str] = None
        # ---- app 后端：目标数据表 + 令牌提供者（构造期解析，缺失即抛错）----
        self.table_id = ""
        self._tokens: Optional[TenantTokenProvider] = None
        if str(self.cfg.backend).strip() == "app":
            self.table_id = resolve_table_id(self.cfg, self.room_tag)
            self._tokens = TenantTokenProvider(
                app_id=self.cfg.app_id,
                app_secret=self.cfg.app_secret,
                transport=self._api_transport,
                api_base=self.cfg.api_base,
                timeout=self.cfg.timeout_seconds,
                refresh_margin_seconds=self.cfg.token_refresh_margin_seconds,
            )
        # 统计（收盘时可读）
        self.sent = 0
        self.failed = 0
        self.skipped = 0

    # ---------- 聚合入口 ----------
    def row_from_snapshot(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """把快照压成指标行，并推进冷场水位与累计基线。

        推进水位保证 `new_cold_events` 不重不漏；推进累计基线保证
        `*_delta` 是「相对上一行」的增量。
        """
        row = build_row(
            snapshot,
            room_id=self.room_id,
            platform=self.platform,
            top_words_n=self.cfg.top_words_n,
            active_top_n=self.cfg.active_top_n,
            prev_cold_watermark=self._cold_watermark,
            prev_cum=self._prev_cum,
            prev_ts=self._prev_ts,
            active_top_cap=self.active_top_cap,
        )
        if row["latest_cold_ts"] > self._cold_watermark:
            self._cold_watermark = row["latest_cold_ts"]
        self._prev_cum = {k: row[f"{k}_cum"] for k in _CUM_KEYS}
        self._prev_ts = row["ts"]
        return row

    # ---------- 发送 ----------
    def send(self, row: Dict[str, Any]) -> bool:
        """发送一行指标。含控频与重试；返回是否成功。never raises。"""
        if not self.cfg.enabled:
            return False
        with self._lock:
            self._wait_for_slot()
            # 幂等键：**同一行在重试之间复用**。请求超时但服务端其实写成功了，
            # 重试就不会插出重复行（client_token 是官方的幂等机制）。
            client_token = new_client_token()
            for attempt in range(self.cfg.max_retries + 1):
                try:
                    self._deliver(row, client_token=client_token)
                    self._last_send_ts = time.time()
                    self.sent += 1
                    return True
                except Exception as e:
                    # retryable=False 表示「重试也没用」（如当月配额用尽、凭证错），
                    # 不必白等 max_retries 轮退避
                    if attempt < self.cfg.max_retries and getattr(e, "retryable", True):
                        delay = self.cfg.retry_backoff_seconds * (attempt + 1)
                        logger.warning(
                            "飞书发送失败（第 %d/%d 次），%.1fs 后重试: %s",
                            attempt + 1, self.cfg.max_retries + 1, delay, e,
                        )
                        time.sleep(delay)
                    else:
                        self.failed += 1
                        if getattr(e, "retryable", True):
                            logger.error("飞书发送最终失败，已丢弃该行: %s", e)
                        else:
                            logger.error("飞书发送失败且不可重试，已丢弃该行: %s", e)
                        break
            return False

    def _wait_for_slot(self) -> None:
        """控频：距上次实际发送不足 min_interval_seconds 则等待。

        ⚠️ 判据必须是「有没有发送过」（_last_send_ts > 0），**不能用
        elapsed > 0** —— Windows 计时器精度约 15.6ms，同一 tick 内两次调用
        会算出 elapsed 恰好为 0，用 elapsed > 0 判断会让控频被静默跳过。
        """
        gap = self.cfg.min_interval_seconds
        if gap <= 0 or self._last_send_ts <= 0:
            return
        elapsed = time.time() - self._last_send_ts
        if elapsed < gap:
            time.sleep(gap - elapsed)

    def _deliver(self, row: Dict[str, Any],
                 client_token: Optional[str] = None) -> None:
        backend = str(self.cfg.backend).strip()
        if backend == "dry_run":
            self._write_dry_run(row)
            return
        if backend == "webhook":
            headers = {"Content-Type": "application/json; charset=utf-8"}
            token = str(self.cfg.bearer_token).strip()
            if token:
                headers["Authorization"] = f"Bearer {token}"
            status = self._transport(
                self.cfg.webhook_url, headers, {"row": row}, self.cfg.timeout_seconds
            )
            if not (200 <= int(status) < 300):
                raise RuntimeError(f"webhook 返回非 2xx：HTTP {status}")
            return
        if backend == "app":
            self._deliver_app(row, client_token=client_token)
            return
        raise RuntimeError(f"未实现的 backend：{self.cfg.backend!r}")

    # ---------- app 后端：自建应用直写多维表格 ----------
    def _deliver_app(self, row: Dict[str, Any],
                     client_token: Optional[str] = None) -> None:
        """调 `POST /bitable/v1/apps/{app_token}/tables/{table_id}/records` 新增一行。

        三重防护（都是真机上会撞到的）：
        1. **进程级按表串行锁** —— 飞书 1254291「同一数据表不支持并发写」，
           多房间写同一张表时必须串起来，否则表现为偶发丢行；
        2. **幂等键跨重试复用** —— 超时重试不插重复行；
        3. **令牌失效自动重取** —— 收到 99991663 之类先作废缓存再重试一次。
        """
        if self._tokens is None:  # pragma: no cover - 构造期已保证
            raise FeishuApiError("app 后端未初始化令牌提供者", retryable=False)
        url = f"{self.cfg.api_base.rstrip('/')}{_RECORD_PATH.format(app_token=self.cfg.app_token, table_id=self.table_id)}"
        payload = build_record_payload(row, client_token=client_token)

        with table_lock(self.cfg.app_token, self.table_id):
            for token_attempt in range(2):   # 最多因令牌失效重取一次
                token = self._tokens.get()
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                }
                status, body = self._api_transport(
                    url, headers, payload, self.cfg.timeout_seconds
                )
                code = body.get("code")
                if status == 200 and code == 0:
                    return
                msg = body.get("msg")
                if code in _ERR_TOKEN_INVALID and token_attempt == 0:
                    logger.warning("飞书令牌失效（code=%s），作废缓存后重试一次", code)
                    self._tokens.invalidate()
                    continue
                # 配额用尽：重试无意义 → 标记不可重试
                if code == _ERR_QUOTA_EXHAUSTED or status == 429:
                    raise FeishuApiError(
                        f"飞书当月 API 调用量已用尽（HTTP {status} code={code} msg={msg!r}）。"
                        f"基础免费版上限约 10,000 次/月，需等次月 1 号刷新或升级版本。"
                        f"★ 落盘会持续失败，建议先调大 feishu.min_interval_seconds 或关闭该层",
                        code=code if isinstance(code, int) else None,
                        http_status=status,
                        retryable=False,
                    )
                raise FeishuApiError(
                    f"新增记录失败：HTTP {status} code={code} msg={msg!r}",
                    code=code if isinstance(code, int) else None,
                    http_status=status,
                    retryable=(code in (_ERR_WRITE_CONFLICT, _ERR_TOO_MANY_REQUEST,
                                         _ERR_TIMEOUT) or status >= 500),
                )
        raise FeishuApiError("新增记录失败：令牌连续失效", retryable=False)

    def _write_dry_run(self, row: Dict[str, Any]) -> None:
        today = time.strftime("%Y-%m-%d", time.localtime())
        if self._file is None or today != self._current_date:
            if self._file is not None:
                self._file.close()
            os.makedirs(self.cfg.output_dir, exist_ok=True)
            # 多房间时按房间拆文件，否则 N 个房间的行会插花写同一文件
            # （行里虽有 room_id 可区分，但人肉排查时拆开更直接）。
            name = (f"feishu_out_{self.room_tag}_{today}.jsonl"
                    if self.room_tag else f"feishu_out_{today}.jsonl")
            path = os.path.join(self.cfg.output_dir, name)
            # 注意：dry_run 只写本地，不 flush 到网络；写完 flush 防异常退出丢行
            self._file = open(path, "a", encoding="utf-8")
            self._current_date = today
        self._file.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._file.flush()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
            self._current_date = None
        logger.info(
            "飞书落盘已结束 backend=%s sent=%d failed=%d skipped=%d room_id=%s",
            self.cfg.backend, self.sent, self.failed, self.skipped, self.room_id or "-",
        )


# ================================================================ 旁路 worker
def run_feishu_worker(
    q: "queue.Queue",
    cfg: FeishuSinkConfig,
    room_id: Optional[str] = None,
    platform: str = "douyin",
    sink: Optional[FeishuSink] = None,
    stop_event: Optional[threading.Event] = None,
    active_top_cap: int = 0,
    room_tag: Optional[str] = None,
    api_transport: Optional[Callable[[str, Dict[str, str], Dict[str, Any], float],
                                     Tuple[int, Dict[str, Any]]]] = None,
) -> None:
    """独立线程消费快照队列并推送飞书。

    队列里放的是 **analyzer 快照 dict**（不是指标行）——聚合成行在 worker
    线程里做，保证采集/规则线程的调用开销最小。
    收到 None 视为退出哨兵。任何异常都不允许把线程打死。

    :param active_top_cap: 规则层 active_top_n，透传给 FeishuSink（见 build_row）。
    :param room_tag: v3.1 多房间下已安全化的房间标识；既要拆 dry_run 本地文件，
        app 后端下也用它查 `table_id_map` 选表。
    :param api_transport: app 后端的 HTTP 传输（测试注入替身）。
    """
    sink = sink or FeishuSink(
        cfg, room_id=room_id, platform=platform, active_top_cap=active_top_cap,
        room_tag=room_tag, api_transport=api_transport,
    )
    logger.info(
        "飞书落盘线程已启动 backend=%s room_id=%s 最小发送间隔=%ss",
        cfg.backend, room_id or "-", cfg.min_interval_seconds,
    )
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        try:
            item = q.get(timeout=1.0)
        except queue.Empty:
            continue
        if item is None:
            break
        try:
            sink.send(sink.row_from_snapshot(item))
        except Exception as e:  # 兜底：绝不让单行异常终结线程
            logger.warning("飞书落盘处理快照异常，已忽略: %s", e)
    sink.close()


def make_worker_kwargs(
    q: "queue.Queue", cfg: FeishuSinkConfig, room_id: Optional[str] = None,
    platform: str = "douyin", active_top_cap: int = 0,
    room_tag: Optional[str] = None,
    api_transport: Optional[Callable[..., Tuple[int, Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """构造 threading.Thread(target=run_feishu_worker, ...) 的 kwargs（供 main.py 接线）。"""
    return {
        "q": q,
        "cfg": cfg,
        "room_id": room_id,
        "platform": platform,
        "active_top_cap": active_top_cap,
        "room_tag": room_tag,
        "api_transport": api_transport,
    }
