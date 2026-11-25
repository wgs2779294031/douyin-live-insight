"""配置模块：抖音弹幕采集客户端。

加载优先级：环境变量 > config.json > 代码内默认值。
抖音网页版走游客身份，无需 AppKey/Secret 等密钥；但 web_rid 必须非空。

环境变量前缀统一为 DOUYIN_，例如 DOUYIN_WEB_RID、DOUYIN_HEARTBEAT_INTERVAL。

v3.1 多房间：顶层可配 `rooms` 数组，让同一进程并发监控多个直播间
（对内自家主播 + 对外竞品）。**不配 `rooms` 时行为与 v3.0 完全一致。**
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "config.json"

# 多房间默认上限。每个房间 = 一条独立的逆向 WSS 长连，暴露面随房间数线性
# 上升（风控角度不该一上来铺满竞品池），故默认压到 5。
MAX_ROOMS_DEFAULT = 5

# 环境变量名与配置字段的映射
_ENV_MAP = {
    "web_rid": "DOUYIN_WEB_RID",
    "output_dir": "DOUYIN_OUTPUT_DIR",
    "heartbeat_interval": "DOUYIN_HEARTBEAT_INTERVAL",
    "sign_backend": "DOUYIN_SIGN_BACKEND",
    "user_agent": "DOUYIN_USER_AGENT",
    "api_base": "DOUYIN_API_BASE",
    "www_base": "DOUYIN_WWW_BASE",
    "wss_host": "DOUYIN_WSS_HOST",
}

_INT_FIELDS = {"heartbeat_interval", "max_rooms"}


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


@dataclass
class Config:
    # 采集源适配器（v2.6）：douyin_reverse=抖音 web 逆向（默认，行为同 v2.5）、
    # bilibili_web=B站网页游客源。--source 命令行可覆盖。
    # bilibili_web 为游客模式，无需 cookie；web_rid 字段复用为 B站房间号。
    source: str = "douyin_reverse"
    # 直播间标识：可以是 web_rid（如 261378947940）、完整 URL 或 v.douyin.com 短链
    # --room 命令行参数可覆盖；bilibili_web 源下填 B站房间号（纯数字或短号）
    web_rid: str = ""
    # 消息落盘目录
    output_dir: str = "data"
    # WebSocket 心跳间隔（秒）；任务书要求 30s，开源原版为 5s
    # TODO(联调): 若 30s 被服务端踢线，先降到 15s 再 5s
    heartbeat_interval: int = 30
    # 签名 JS 执行后端：auto / mini_racer / execjs
    sign_backend: str = "auto"
    # 浏览器 UA（游客身份伪装）
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0"
    )
    # 抖音直播网页 base，用于解析真实 room_id
    api_base: str = "https://live.douyin.com"
    # 抖音主页 base，用于取 __ac_nonce / __ac_signature
    www_base: str = "https://www.douyin.com"
    # 弹幕 WebSocket 服务器 host
    wss_host: str = "webcast100-ws-web-lq.douyin.com"
    # 登录态 Cookie（可选但推荐）：游客只能收进场/点赞/礼物，聊天弹幕需登录态
    cookie: str = ""
    # v3.1 多房间上限：`rooms` 数组长度不得超过此值（默认 5）。
    # 每个房间是一条独立逆向 WSS 长连，房间越多暴露面越大，故设上限。
    max_rooms: int = MAX_ROOMS_DEFAULT

    def validate(self, require_room: bool = True) -> None:
        """启动前校验：web_rid 必填、心跳间隔必须为正。

        v2.6 采集源规则：
        - bilibili_web（B站游客模式）不得要求 cookie 存在——本函数本就不校验
          cookie，cookie 仅为 douyin_reverse 的可选推荐项；
        - douyin_reverse 维持 v2.5 全部校验，行为完全一致。

        :param require_room: v3.1 多房间模式下每个房间自带 room_id，顶层
            web_rid 可以为空 → 此时传 False 跳过该项校验，其余校验照旧。
            默认 True，保持 v3.0 及以前的调用行为不变。
        """
        if require_room and not str(self.web_rid).strip():
            if self.source == "bilibili_web":
                raise ConfigError(
                    "bilibili_web 源需要填入 B站直播间房间号"
                    "（config.json 的 web_rid、--room 命令行参数或环境变量 DOUYIN_WEB_RID）"
                )
            raise ConfigError(
                "请先填入抖音直播间 web_rid（config.json 的 web_rid，"
                "或 --room 命令行参数，或环境变量 DOUYIN_WEB_RID）；"
                "或改用顶层 rooms 数组配置多个房间"
            )
        if self.heartbeat_interval <= 0:
            raise ConfigError("heartbeat_interval 必须为正整数")
        if self.max_rooms <= 0:
            raise ConfigError("max_rooms 必须为正整数")


# ================================================================ 多房间（v3.1）
# 文件名安全化：rooms 里的 label 会直接进落盘文件名，必须去掉路径分隔符
# 等非法字符（room_id 允许填完整 URL / v.douyin.com 短链，含 `:/`）。
_LABEL_UNSAFE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff_-]+")
_LABEL_MAX_LEN = 40


def sanitize_label(raw: str, fallback: str = "") -> str:
    """把 label 归一成安全的文件名片段；为空则回退 fallback；仍为空返回 ""。"""
    for candidate in (raw, fallback):
        s = _LABEL_UNSAFE.sub("_", str(candidate or "").strip()).strip("_-")
        if s:
            return s[:_LABEL_MAX_LEN]
    return ""


@dataclass
class RoomConfig:
    """一个被监控的直播间（config.json 顶层 `rooms` 数组的元素）。

    ★ 为什么要房间级开关（老大 2026-09-29 的要求）
    -------------------------------------------------
    「对我们自己公司主播的实时台词辅助要保留」。台词辅助（LLM 层）与竞品
    采集（飞书落盘层）**不是两种模式，而是同一个分析器的两个下游**：

    - 对内（自家主播）→ `llm: true`（出冷场救场台词 / 观众画像），可选 `feishu: true` 留档
    - 对外（竞品号）  → `llm: false`（省钱，且竞品不需要秒级实时），`feishu: true`

    两边可以同时跑，靠 `rooms` 里的布尔开关区分，**不需要第二套模式代码**。
    """

    # 必填：纯数字 room_id / 完整 URL / v.douyin.com 短链（口径同 web_rid）
    room_id: str = ""
    # 可选：用于日志与落盘文件名。留空则自动取 room_id（并做文件名安全化）
    label: str = ""
    # 房间级下游开关。None = 继承全局 llm.enabled / feishu.enabled
    llm: Optional[bool] = None
    feishu: Optional[bool] = None
    # 房间级快照间隔（秒）。0 = 继承默认。实时辅助要短，竞品可以长。
    snapshot_interval_seconds: int = 0
    # 备注：只进日志，便于人读（如「竞品-就业赛道」）
    note: str = ""

    def resolve_llm(self, default: bool) -> bool:
        return default if self.llm is None else bool(self.llm)

    def resolve_feishu(self, default: bool) -> bool:
        return default if self.feishu is None else bool(self.feishu)

    def file_tag(self, index: int = 0) -> str:
        """落盘文件名里用的房间标识（已安全化，稳定且唯一）。

        - 有 label → 用 label
        - 无 label 但 room_id 是纯数字 → 直接用（最可读）
        - 无 label 且 room_id 是 URL/短链 → `room{i+1}_{sha1前6位}`
          （不直接用 URL，否则文件名会带一堆下划线；sha1 只保证**不同 URL 不会
          撞名**——`i+1` 会随数组顺序变，所以同一房间挪了位置文件名也会变，
          这是「没填 label」的代价，main.py 会 WARNING 提示补 label）
        """
        s = sanitize_label(self.label)
        if s:
            return s
        rid = str(self.room_id or "").strip()
        if rid.isdigit():
            return rid
        digest = hashlib.sha1(rid.encode("utf-8")).hexdigest()[:6]
        return f"room{index + 1}_{digest}"

    def describe(self) -> str:
        parts = [f"room_id={self.room_id}"]
        if self.label:
            parts.append(f"label={self.label}")
        if self.note:
            parts.append(f"note={self.note}")
        parts.append(f"llm={'inherit' if self.llm is None else self.llm}")
        parts.append(f"feishu={'inherit' if self.feishu is None else self.feishu}")
        if self.snapshot_interval_seconds:
            parts.append(f"snapshot={self.snapshot_interval_seconds}s")
        return " ".join(parts)


def _coerce_optional_bool(value: Any, where: str) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{where} 必须是 true / false（或不填以继承全局开关）")


def _coerce_room(raw: Any, index: int) -> RoomConfig:
    """把 rooms 数组的一个元素归一成 RoomConfig；非法即抛 ConfigError。"""
    where = f"config.json 的 rooms[{index}]"
    if isinstance(raw, str):
        rid = raw.strip()
        if not rid:
            raise ConfigError(f"{where} 不能为空字符串")
        return RoomConfig(room_id=rid)
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} 必须是对象或字符串，实际是 {type(raw).__name__}")

    rid = str(raw.get("room_id") or "").strip()
    if not rid:
        raise ConfigError(
            f"{where} 缺少 room_id（纯数字直播间号 / 完整 URL / v.douyin.com 短链）"
        )
    snap = raw.get("snapshot_interval_seconds") or 0
    try:
        snap = int(snap)
    except (TypeError, ValueError):
        raise ConfigError(f"{where} 的 snapshot_interval_seconds 必须是整数")
    if snap < 0:
        raise ConfigError(f"{where} 的 snapshot_interval_seconds 不能为负")

    return RoomConfig(
        room_id=rid,
        label=str(raw.get("label") or "").strip(),
        llm=_coerce_optional_bool(raw.get("llm"), f"{where} 的 llm"),
        feishu=_coerce_optional_bool(raw.get("feishu"), f"{where} 的 feishu"),
        snapshot_interval_seconds=snap,
        note=str(raw.get("note") or "").strip(),
    )


def load_room_configs(path: str = DEFAULT_CONFIG_PATH) -> List[RoomConfig]:
    """读取 config.json 顶层 `rooms` 数组。

    - 文件不存在 / 无 `rooms` 键 / `rooms` 为空数组 → 返回 `[]`，
      调用方据此走 **legacy 单房间**路径（web_rid + --room），行为与 v3.0 一致。
    - `rooms` 非数组、或元素非法 → 抛 ConfigError（启动即报错，不静默退化）。
    """
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f) or {}
    except (OSError, ValueError) as e:
        logger.warning("读取多房间配置失败，按未配置处理: %s", e)
        return []

    items = raw.get("rooms")
    if items is None:
        return []
    if not isinstance(items, list):
        raise ConfigError("config.json 的 rooms 必须是数组（每个元素是一个直播间）")

    rooms: List[RoomConfig] = []
    for i, item in enumerate(items):
        rooms.append(_coerce_room(item, i))

    seen: Dict[str, int] = {}
    for i, r in enumerate(rooms):
        key = r.room_id
        if key in seen:
            raise ConfigError(
                f"config.json 的 rooms 里 room_id={key!r} 重复"
                f"（第 {seen[key]} 个与第 {i} 个），多房间不许监控同一房间"
            )
        seen[key] = i
    return rooms



def _coerce(field: str, value):
    """环境变量读出来的都是字符串，整数字段做转换。"""
    if field in _INT_FIELDS and isinstance(value, str):
        value = value.strip()
        if value == "":
            return None
        return int(value)
    return value


def load_config(
    path: str = DEFAULT_CONFIG_PATH,
    room_override: Optional[str] = None,
) -> Config:
    """读取配置：先读 config.json（若存在），再用环境变量覆盖，最后应用命令行参数。"""
    raw: dict = {}

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f) or {}

    # v2.6 采集源配置：source 节（与 llm/anchor 平级）取 source.platform；
    # 也容忍直接把 source 写成字符串。未知取值由 sources.build_adapter 报错。
    src = raw.get("source")
    if isinstance(src, dict) and src.get("platform"):
        raw["source"] = str(src["platform"]).strip()
    elif isinstance(src, str) and src.strip():
        raw["source"] = src.strip()

    for field, env_name in _ENV_MAP.items():
        env_val = os.environ.get(env_name)
        if env_val is not None and env_val != "":
            raw[field] = _coerce(field, env_val)

    if room_override is not None:
        raw["web_rid"] = room_override

    # 只取 Config 认识的字段，避免文件里多写键导致报错
    known = {k: v for k, v in raw.items() if k in Config.__dataclass_fields__}
    return Config(**known)
