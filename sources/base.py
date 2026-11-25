r"""采集层可替换适配器：抽象接口 + 统一事件归一化（v2.6 任务书第 3/4 节）。

架构约定：
- 下游（analyzer 规则层 / llm_service LLM层 / dashboard 看板）只消费统一格式的
  标准事件，不感知数据来源；
- 新增数据源 = 新增一个 adapter 文件并在 sources/__init__.py 注册，下游零改动。

统一事件 Schema（所有 adapter 投递给 record_queue、以及经 JsonlStorage 落盘的
record，统一为以下 8 个字段）::

    {
      "ts": 1789564843,                # int, 秒级时间戳
      "platform": "douyin",            # "douyin" / "bilibili"
      "type": "danmaku",               # danmaku/enter/like/gift（B站源本期仅 danmaku）
      "user_id": "...",                # 平台内用户稳定标识字符串；B站游客源填 user_hash
      "uname": "昵称",
      "text": "弹幕正文",
      "room_id": "...",                # 平台内部真实房间号字符串（拿不到则填启动参数）
      "extra": {}                      # 平台特有字段，无则 {}
    """

from __future__ import annotations

import abc
from typing import Any, Dict, Optional

# platform 取值（本期枚举；未来接入官方源时在此扩充）
PLATFORM_DOUYIN = "douyin"
PLATFORM_BILIBILI = "bilibili"

# 统一事件类型枚举（capabilities 的固定键集合，缺一不可）
EVENT_TYPES = ("danmaku", "enter", "like", "gift")


class SourceAdapter(abc.ABC):
    """数据源适配器抽象基类。

    实现约定：
    - 构造函数接收 (cfg, storage, record_queue, shared_state=None)；
    - run() 阻塞运行（签名与原 DanmuClient.run 一致），断线自动重连；
    - close() 优雅退出，幂等可重复调用；
    - msg_count 只读属性，返回本次运行累计收到的消息条数；
    - 每条消息必须做"双出口"：storage.write(record) 落盘 +
      record_queue.put_nowait(record) 投递下游（队列满丢弃，绝不阻塞采集）；
    - **capabilities 为子类必须覆盖的抽象类属性**：声明本源能产出哪些事件
      类型，键固定为 EVENT_TYPES 即 ("danmaku", "enter", "like", "gift")
      四个，值必须为 bool，四键必须齐全；缺失任何一键或保留基类空 dict
      均视为实现错误（由 tests/test_contract.py 契约测试强制失败）。
      看板据此做能力降级显示，下游不得据其改变处理逻辑。
    """

    # 平台标识（子类必须覆盖）：取值见 PLATFORM_DOUYIN / PLATFORM_BILIBILI
    platform: str = ""

    # 能力声明（子类必须覆盖）：键固定为 danmaku/enter/like/gift，值为 bool；
    # 基类保留空 dict 仅作占位，未覆盖即契约测试失败。
    capabilities: dict = {}

    def __init__(self, cfg, storage, record_queue=None, shared_state=None,
                 monitor=None) -> None:
        self.cfg = cfg
        self.storage = storage
        # 下游旁路队列：可选；提供则每条 record 落盘后投递（满则丢弃）
        self.record_queue = record_queue
        # 可选共享容器：房间号解析成功后写 shared_state.room_id（LLM 人设卡用）
        self.shared_state = shared_state
        # v3.3 监控层旁路：可选 Monitor（抖音源接线；B站源接收但不使用）
        self.monitor = monitor

    @abc.abstractmethod
    def run(self) -> None:
        """阻塞运行采集主循环。"""

    @abc.abstractmethod
    def close(self) -> None:
        """优雅退出（停止重连/关闭连接）。"""

    @property
    @abc.abstractmethod
    def msg_count(self) -> int:
        """本次运行累计接收消息数。"""


def normalize_record(platform: str, record: Dict[str, Any],
                     room_id: Any = "",
                     extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把各平台原始 record 归一化成统一事件 schema（第 3 节契约的唯一实现）。

    规则：
    - 各 adapter 不得私自拼字段，一律经本函数归一化；
    - 抖音源：现网 5 字段 + 仅新增 platform/room_id/extra 三个字段；
    - B站源：parse_danmaku 产出 user_hash → user_id 取其值（user_hash 本身即
      匿名哈希，符合匿名化红线），原值同时放入 extra.user_hash；user_hash 键
      不保留在顶层；
    - room_id 统一转字符串（拿不到则由调用方填启动参数）。

    :param platform: 平台标识（"douyin" / "bilibili"）
    :param record: 平台原始 record（至少含 ts/type/uname/text，用户标识字段
                   为 user_id 或 user_hash）
    :param room_id: 平台内部真实房间号（int/str 均可，拿不到填启动参数）
    :param extra: 额外平台特有字段（无则 {}）
    :return: 归一化后的新 dict（不修改入参）
    """
    out = dict(record)
    out["platform"] = platform
    out["room_id"] = str(room_id) if room_id is not None else ""
    out["extra"] = dict(extra) if extra else {}

    # 用户标识归一：user_hash → user_id（B站游客源），并保留原值到 extra
    user_hash = out.pop("user_hash", None)
    if user_hash is not None:
        out["user_id"] = str(out.get("user_id") or user_hash)
        out["extra"].setdefault("user_hash", user_hash)
    out["user_id"] = str(out.get("user_id") or "")
    return out


# ================================================================ 未来官方源接入点（本期不实现）
# 任务书 v2.6 第 8 节：本期不实现任何官方 API adapter，仅留接口与接入说明。
#
# 1) B站直播开放平台（open-live OAuth，wss://broadcast-ext.msg.dailymt.com/websocket/v2）
#    接入方式：新建 sources/bilibili_openlive.py，实现 SourceAdapter：
#      - __init__ 读 config 新增的 openlive app_id/access_token 配置；
#      - run() 用官方 SDK 鉴权包连 WSS，把 OpenLiveDanmakuData 消息映射为
#        normalize_record(PLATFORM_BILIBILI, {...}, room_id=...) 后双出口投递；
#      - 在 sources/__init__.py 注册 name="bilibili_openlive" 即可，下游零改动。
# 2) 抖音直播小玩法网关（官方消息推送）
#    接入方式：新建 sources/douyin_openlive.py，同样实现 SourceAdapter，把
#    官方推送的 WebcastChatMessage 等映射为 normalize_record(PLATFORM_DOUYIN,
#    {...}, room_id=...)；client.py 的 web 逆向协议保持不变，两者可并存。
