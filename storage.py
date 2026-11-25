"""消息落盘模块：JSONL，按日期切分文件。

文件命名（v2.6 起按 platform 区分，抖音保持原命名不变；v3.1 起支持按房间分文件）：
    单房间（room=None，与 v3.0 及以前完全一致）
        抖音    {output_dir}/danmu_douyin_YYYY-MM-DD.jsonl
        B站     {output_dir}/danmu_bilibili_YYYY-MM-DD.jsonl
    多房间（room="<房间标识>"）
        抖音    {output_dir}/danmu_douyin_<room>_YYYY-MM-DD.jsonl
        B站     {output_dir}/danmu_bilibili_<room>_YYYY-MM-DD.jsonl

★ room 传的是**已安全化的短标识**（见 config.RoomConfig.file_tag），不是完整
  room_id/URL —— 多房间下若文件名不含房间标识，多个房间会**插花写同一个文件**，
  且读回来的记录无法区分来源。

每行一条 JSON 记录。跨天时自动切到新文件；每条写入后立即 flush，保证异常退出不丢消息。

记录字段（v2.6 统一事件 schema，见 sources/base.normalize_record）：
    {"ts": ..., "platform": "douyin", "type": "danmaku", "user_id": "...",
     "uname": "...", "text": "...", "room_id": "...", "extra": {}}
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class JsonlStorage:
    def __init__(self, output_dir: str, platform: str = "douyin",
                 room: Optional[str] = None) -> None:
        self.output_dir = output_dir
        # 落盘平台标识：抖音 "douyin"（默认，与 v2.5 命名一致）、B站 "bilibili"
        self.platform = platform
        # v3.1 多房间：房间标识（已安全化）。None → 沿用 v3.0 旧文件名（零回归）
        self.room = (str(room).strip() or None) if room is not None else None
        os.makedirs(self.output_dir, exist_ok=True)
        self._file = None
        self._current_date: Optional[str] = None
        self._log_path: Optional[str] = None

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d", time.localtime())

    def _path_for(self, date_str: str) -> str:
        if self.room:
            name = f"danmu_{self.platform}_{self.room}_{date_str}.jsonl"
        else:
            name = f"danmu_{self.platform}_{date_str}.jsonl"
        return os.path.join(self.output_dir, name)

    @property
    def current_path(self) -> Optional[str]:
        """当前文件路径（尚未写过则为 None）。多房间下便于日志/自检定位。"""
        return self._log_path

    def _ensure_file(self) -> None:
        """确保当前文件句柄对应当天日期，跨天则切文件。

        ★ 顺序必须是「先 open 成功 → 再换 _file/_current_date」（2026-11-01 审计 P0）。
          原实现先 close 旧句柄、先改 _current_date，最后才 open；open 一旦失败
          （磁盘满 / 目录被删 / 权限变更），下一次 write 会因
          `today == self._current_date` 直接返回、**永不重试轮转**，然后往已关闭的
          句柄里写 → 异常被上层吞掉 → 该进程余下数据全部静默丢失。
          现在失败时旧句柄原封不动，异常如实抛给调用方，下一次写入自己重试。
        """
        today = self._today()
        if self._file is not None and today == self._current_date:
            return
        path = self._path_for(today)
        new_file = open(path, "a", encoding="utf-8")   # 失败就抛，旧状态不变
        old_file = self._file
        self._file = new_file
        self._current_date = today
        self._log_path = path
        if old_file is not None:
            try:
                old_file.close()
            except Exception as e:  # noqa: BLE001 旧句柄关不掉不影响新文件
                logger.warning("关闭旧落盘文件失败（不影响新文件）: %s", e)
            logger.info("跨天切分，已切换落盘文件到 %s", path)

    def write(self, record: Dict[str, Any]) -> None:
        """写入一条记录（dict → 一行 JSON）。

        统一字段（v2.6 schema）：ts / platform / type / user_id / uname / text
        / room_id / extra；历史 record（无新三字段）也能原样写入，不报错。
        """
        self._ensure_file()
        line = json.dumps(record, ensure_ascii=False)
        self._file.write(line + "\n")
        self._file.flush()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
            self._current_date = None
