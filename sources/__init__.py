"""采集层数据源注册表与工厂（v2.6 任务书第 4 节 / v2.6.1 capabilities）。

用法（main.py 接线）::

    from sources import SOURCE_PLATFORM, build_adapter, get_capabilities

    adapter = build_adapter("bilibili_web", cfg, storage, record_queue, shared_state)
    adapter.run()
    caps = get_capabilities("bilibili_web")  # 看板能力降级用

新增数据源：实现 sources/base.SourceAdapter 接口后，在 build_adapter 中
注册一个分支即可，下游（analyzer/llm_service/dashboard）零改动。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from sources.base import SourceAdapter

# 工厂 name → 落盘平台标识（JsonlStorage 用，决定 danmu_{platform}_*.jsonl 文件名）
SOURCE_PLATFORM = {
    "douyin_reverse": "douyin",
    "bilibili_web": "bilibili",
}

# 落盘 platform 标识 → 工厂 name（看板从落盘记录的 platform 反查能力用）。
# 当前 platform 与源一一对应；未来若出现同平台多源（能力不同），看板需改为
# 记录源 name，本反查表届时按实际需求扩展。
PLATFORM_SOURCE = {v: k for k, v in SOURCE_PLATFORM.items()}

__all__ = [
    "SourceAdapter", "SOURCE_PLATFORM", "PLATFORM_SOURCE",
    "build_adapter", "get_capabilities", "get_capabilities_by_platform",
]


def build_adapter(name: str, cfg, storage,
                  record_queue: Optional["queue.Queue"] = None,
                  shared_state: Any = None,
                  monitor: Any = None) -> SourceAdapter:
    """按 name 构造数据源适配器；未知 name 抛 ValueError（启动即报清晰错误）。

    :param monitor: v3.3 监控层旁路（可选 Monitor 实例）；抖音源接线，
        B站源接收但暂不使用（B站监控层协议未实现）。
    """
    if name == "douyin_reverse":
        # 函数内延迟导入：避免未用的源（及其依赖）在启动时被加载
        from sources.douyin_reverse import DouyinReverseAdapter
        return DouyinReverseAdapter(cfg, storage, record_queue=record_queue,
                                    shared_state=shared_state, monitor=monitor)
    if name == "bilibili_web":
        from sources.bilibili_web import BilibiliWebAdapter
        return BilibiliWebAdapter(cfg, storage, record_queue=record_queue,
                                  shared_state=shared_state)
    raise ValueError(
        f"未知数据源 {name!r}，可选值：{sorted(SOURCE_PLATFORM)}（config.json 的 "
        f"source.platform 或命令行 --source）"
    )


def _adapter_class(name: str):
    """按工厂 name 取适配器类（延迟导入，未知 name 抛 ValueError）。"""
    if name == "douyin_reverse":
        from sources.douyin_reverse import DouyinReverseAdapter
        return DouyinReverseAdapter
    if name == "bilibili_web":
        from sources.bilibili_web import BilibiliWebAdapter
        return BilibiliWebAdapter
    raise ValueError(
        f"未知数据源 {name!r}，可选值：{sorted(SOURCE_PLATFORM)}（config.json 的 "
        f"source.platform 或命令行 --source）"
    )


def get_capabilities(name: str) -> Dict[str, bool]:
    """按工厂 name 查询源能力声明（不实例化 adapter）。

    :return: {"danmaku": bool, "enter": bool, "like": bool, "gift": bool}
             的副本（调用方可安全修改）；未知 name 抛 ValueError（与
             build_adapter 同口径）。
    """
    return dict(_adapter_class(name).capabilities)


def get_capabilities_by_platform(platform: str) -> Dict[str, bool]:
    """按落盘记录中的 platform 标识反查能力（看板用，实现从简）。

    未知 platform 抛 ValueError；调用方（看板）对未知值做容错，不得打挂页面。
    """
    name = PLATFORM_SOURCE.get(platform)
    if name is None:
        raise ValueError(f"未知 platform 标识 {platform!r}，"
                         f"已知：{sorted(PLATFORM_SOURCE)}")
    return get_capabilities(name)
