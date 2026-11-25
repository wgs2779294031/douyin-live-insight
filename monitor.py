r"""v3.3 竞品监控采集层：人气快照 + 系统事件 + 房间元数据 独立落盘。

为什么单独一层（老大 2026-09-30 产品双目的需求书）
------------------------------------------------
产品两个目的共用一个数据底座：对内实时辅助（原链路）+ 竞品监控（新增）。
竞品监控需要的在线人数快照 / 系统事件 / 房间元数据**结构与主链路不同**，
不能塞进 5 字段 normalize / 8 字段统一 schema —— 硬塞会破坏 31 列契约。

落盘文件（与主链路 danmu_*.jsonl 完全独立，31 列契约零改动）：
    stats_douyin_<tag>_<date>.jsonl   高频人气快照（room_stats / room_user_seq）
    events_douyin_<tag>_<date>.jsonl  低频系统事件（social/room_rank/fansclub/
                                      control/room/product_change/live_shopping）
    room_meta_douyin_<tag>.json       房间元数据（主播名/标题/开播时间/场次标识）

数据来源（2026-09-30 真机探针 data/probe_methods.json 实测，150s/2 房间）：
    RoomStatsMessage    57 次  → 在线人数（display_value/total）
    RoomUserSeqMessage  54 次  → 在线人数 + 累计观看 PV（total_pv_for_anchor）+ 在线榜
    SocialMessage       16 次  → 关注/分享
    RoomRankMessage      6 次  → 榜单变动
    RoomMessage / FansclubMessage 各 4 次
    ControlMessage       0 次（只在开播/下播边界出现，中途加入拿不到开播时刻）
    ProductChange / LiveShopping 0 次（需电商直播间）
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional

import parser as douyin_parser

logger = logging.getLogger("danmu")

# 写入 stats 文件的监控 type（高频）；其余写 events 文件（低频）
_STATS_TYPES = {"room_stats", "room_user_seq"}

# ★ 冷门 method 原始留存清单（2026-09-30 从抖音 web 前端 client-entry~1.js 提取 +
#   真机实测确认平台会推、但本仓 proto 里**没有对应类**的消息）。
#   这些消息不能丢：原样落盘 method + payload（hex），等补上类定义后即可回解。
#   依据：refs/known_methods_web_all.json（62 个平台 method 名，本仓 proto 只覆盖 12 个）
RAW_METHODS = {
    # ⚠️ 注意：**已被 parser._MONITOR_TYPE_MAP 收录成「有类」的 method 不要放这里**，
    #    否则同一条消息会既落 typed 事件、又落 raw_event（重复）。
    #    2026-09-30 真机反解后，以下已移出本集合（改由 protobuf/extras.py 的类解析）：
    #      WebcastLuckyBoxMessage / WebcastLotteryEventNewMessage / WebcastLotteryEventMessage
    #    下列仍是「proto 无类 & 尚未反解」的冷门 method —— 原样留 hex，供离线反解：
    "WebcastPrizeNoticeMessage",         # 奖品发放通知（福袋/抽奖/PK 奖励）
    "WebcastPreviewCjRpMessage",         # 红包 / 福袋即将开奖
    "WebcastLinkMicBattleMethod",        # PK 主消息
    "WebcastLinkMicBattleFinishMethod",  # PK 结束
    "WebcastLinkMicArmiesMethod",        # PK 助力
    "WebcastLinkMicMethod",              # 连麦申请 / 控制
    "WebcastLinkSettingNotifyMessage",   # 连麦权限变更
    "WebcastScreenChatMessage",          # 屏幕弹幕
    "WebcastNoticeMessage",              # 系统通知
    # —— 2026-09-30 真机新遇到、**未反解**的连麦一族（先原样留 hex，理由见各行）：
    # 连麦「彩蛋」（385B）：**结构自相矛盾** —— 同一逻辑字段 room_id 在 `#3.#2.#1` 里是
    #   varint(7691172785923230515)，在 `#3.#2.#3` 里却是 length-delimited('7691172785923230515')
    #   → 两块**不是同一个 message 类型**，据此无法安全建模（硬建模等于赌）；且彩蛋语义
    #   （#4/#5 里 4/60/5690 与 150/60/5630 到底是门槛还是进度）无第二样本佐证 → 暂缓。
    "WebcastLinkmicProfitEggMessage",
    # 连麦房间大状态（真机单条 58KB）：值得单独一轮取样，本轮不滥建模。
    "WebcastLinkMessage",
    # 展厅点亮聊天（969B）：是模板聊天（'exhibition_lighten_chat_message_linkmic_audience_v2'）。
    "WebcastExhibitionChatMessage",
    # ⚠️ WebcastLinkmicTeamfightMessage / …TeamfightScoreMessage 已反解成类 → 移出本集合
}


def _summarize_payload(payload: bytes, max_bytes: int = 512) -> str:
    """原始 payload → hex 字符串（截断到 max_bytes，标注是否截断）。"""
    if payload is None:
        return ""
    head = payload[:max_bytes]
    suffix = "" if len(payload) <= max_bytes else "..."
    return head.hex() + suffix


class _DateJsonl:
    """按日期切分的 JSONL 追加写文件（与 storage.JsonlStorage 同款模式，
    但文件名前缀不同，且互不共享状态）。每条写入即 flush。"""

    def __init__(self, output_dir: str, name_prefix: str) -> None:
        self.output_dir = output_dir
        self.name_prefix = name_prefix
        os.makedirs(output_dir, exist_ok=True)
        self._file = None
        self._current_date: Optional[str] = None
        self._path: Optional[str] = None

    def _path_for(self, date_str: str) -> str:
        return os.path.join(self.output_dir, f"{self.name_prefix}_{date_str}.jsonl")

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d", time.localtime())

    def _ensure_file(self) -> None:
        """确保当前文件句柄对应当天日期，跨天则切文件。

        ★ 与 storage.JsonlStorage._ensure_file / llm_service.DailyJsonlWriter
          同源同修（2026-11-01 审计 P0）：先 open 成功再换 _file/_current_date，
          open 失败时旧句柄保持可用、下一次写入自己重试。原实现先 close 再 open，
          失败后 _current_date 已是新日期 → 永不重试，句柄已关闭 → 每条都抛
          ValueError 被旁路吞掉 → 监控层静默停写（stats/events 全部丢失）。
        """
        today = self._today()
        if self._file is not None and today == self._current_date:
            return
        path = self._path_for(today)
        new_file = open(path, "a", encoding="utf-8")   # 失败就抛，旧状态不变
        old_file = self._file
        self._file = new_file
        self._current_date = today
        self._path = path
        if old_file is not None:
            try:
                old_file.close()
            except Exception as e:  # noqa: BLE001 旧句柄关不掉不影响新文件
                logger.warning("关闭旧监控落盘文件失败（不影响新文件）: %s", e)

    def write(self, record: Dict[str, Any]) -> None:
        self._ensure_file()
        self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._file.flush()

    @property
    def current_path(self) -> Optional[str]:
        return self._path

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
            self._current_date = None


class Monitor:
    """一个房间一份的监控落盘器。挂到 DanmuClient 上作为旁路，绝不影响主链路。"""

    def __init__(self, output_dir: str, platform: str = "douyin",
                 room_tag: str = "") -> None:
        self.platform = platform
        self.room_tag = room_tag or "room"
        self._stats = _DateJsonl(output_dir, f"stats_{platform}_{self.room_tag}")
        self._events = _DateJsonl(output_dir, f"events_{platform}_{self.room_tag}")
        self._meta_path = os.path.join(
            output_dir, f"room_meta_{platform}_{self.room_tag}.json")
        # 计数（供运行日志/自检）
        self.stats_count = 0
        self.events_count = 0
        self.raw_count = 0

    # ============================================================ 消息处理
    def handle_response(self, response) -> None:
        """吃一个 Response，解析其中的监控消息并落盘。由 client 旁路调用，
        异常由调用方兜底（不影响主链路）。"""
        for method, monitor_type, payload in douyin_parser.iter_monitor_messages(response):
            record = douyin_parser.normalize_monitor(monitor_type, payload)
            if record is None:
                continue
            record["platform"] = self.platform
            if monitor_type in _STATS_TYPES:
                self._stats.write(record)
                self.stats_count += 1
            else:
                self._events.write(record)
                self.events_count += 1

    def handle_raw_methods(self, response) -> None:
        """冷门 method 原始留存：平台会推但本仓 proto 无类的消息（福袋 / PK 等）。

        ⚠️ 这类消息**进不了 normalize_monitor**（没有类可解），但绝不能丢 ——
        原样落盘 method + payload hex，等补上类定义后可离线回解（scripts/pb_decode.py）。
        """
        if not RAW_METHODS:
            return
        for msg in getattr(response, "messages_list", None) or []:
            if msg.method not in RAW_METHODS:
                continue
            ts = int(time.time())
            self._events.write({
                "ts": ts,
                "type": "raw_event",
                "platform": self.platform,
                "method": msg.method,
                "payload_len": len(msg.payload or b""),
                "payload_hex": _summarize_payload(msg.payload or b""),
                "note": "proto 无对应类；hex 可离线用 scripts/pb_decode.py 反解",
            })
            self.raw_count += 1

    # ============================================================ 房间元数据
    def write_room_meta(self, meta: Dict[str, Any]) -> None:
        """覆盖写房间元数据 JSON（主播名/标题/开播时间/场次标识）。"""
        meta = dict(meta)
        meta.setdefault("platform", self.platform)
        meta.setdefault("room_tag", self.room_tag)
        # 场次标识：日期 + 房间号（开播真实边界拿不到时的稳定标识）
        meta.setdefault("session_id", f"{meta.get('room_id', '')}_{time.strftime('%Y-%m-%d')}")
        meta["fetched_at"] = int(time.time())
        tmp = self._meta_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._meta_path)

    def read_room_meta(self) -> Dict[str, Any]:
        """读回已落盘的房间元数据（不存在/损坏返回 {}）。

        用途：``client._fetch_room_info`` 在重连时判断「本次拿到的元数据质量是否更低」，
        避免用兜底值（房间页 HTML 正则）覆盖上一次 enter 接口拿到的好值。
        """
        if not os.path.exists(self._meta_path):
            return {}
        try:
            with open(self._meta_path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception as e:
            logger.warning("读取房间元数据失败（按空处理）: %s", e)
            return {}

    def patch_room_meta(self, **kwargs) -> None:
        """增量补写元数据（如首条消息时间）。文件不存在则先建骨架。"""
        meta: Dict[str, Any] = {}
        if os.path.exists(self._meta_path):
            try:
                with open(self._meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception as e:
                logger.warning("读取房间元数据失败，将覆盖: %s", e)
        meta.update(kwargs)
        self.write_room_meta(meta)

    @property
    def meta_path(self) -> str:
        return self._meta_path

    @property
    def stats_path(self) -> Optional[str]:
        return self._stats.current_path

    @property
    def events_path(self) -> Optional[str]:
        return self._events.current_path

    def close(self) -> None:
        self._stats.close()
        self._events.close()
