"""抖音逆向源适配器：薄封装注册现有 client.DanmuClient（v2.6 任务书第 4 节）。

单一事实源：抖音 web 逆向协议逻辑全部保留在 client.py，本文件零复制，
仅做接口适配（委托）。DanmuClient 的公开形状（run/close/msg_count/构造签名）
与 SourceAdapter 一致，未来协议升级只改 client.py。
"""

from __future__ import annotations

from client import DanmuClient
from sources.base import SourceAdapter


class DouyinReverseAdapter(SourceAdapter):
    """抖音 web 逆向源（现有逻辑搬迁注册）。"""

    platform = "douyin"
    # 抖音 web 逆向源解析全部四类事件（见 parser._METHOD_TYPE_MAP）
    capabilities = {"danmaku": True, "enter": True, "like": True, "gift": True}

    def __init__(self, cfg, storage, record_queue=None, shared_state=None,
                 monitor=None) -> None:
        super().__init__(cfg, storage, record_queue, shared_state, monitor)
        self._client = DanmuClient(
            cfg, storage,
            record_queue=record_queue,
            shared_state=shared_state,
            monitor=monitor,
        )

    def run(self) -> None:
        self._client.run()

    def close(self) -> None:
        self._client.close()

    @property
    def msg_count(self) -> int:
        return self._client.msg_count
