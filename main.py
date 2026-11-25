"""弹幕采集客户端入口（v3.1 多房间并发）。

用法
----
单房间（与 v3.0 完全一致，落盘文件名不变）：
    python main.py                                 # 使用同目录 config.json
    python main.py --config my.json                # 指定配置文件
    python main.py --room 261378947940             # 覆盖配置中的 web_rid（纯数字）
    python main.py --room https://live.douyin.com/261378947940
    python main.py --room https://v.douyin.com/xxxxxx/  # 短链分享链接
    python main.py --room <房间号> --llm           # 启用 LLM 层（需 config.json 填 api_key）
    python main.py --source bilibili_web --room <B站房间号>   # B站游客源（无需 cookie）

多房间（config.json 顶层写 `rooms` 数组，一条命令并发多个直播间）：
    python main.py                                 # 自动识别 rooms，N 个房间同时跑
    不再使用 --room（与 rooms 冲突，会直接报错退出）

★ 为什么需要多房间
------------------------------------------------
竞品监控要同时盯多个直播间。v3.0 及以前一个进程只能盯一个房间——不是配置
问题，是四处**结构性**限制：`JsonlStorage` 文件名不含 room_id、`Config.web_rid`
是单字符串、`SharedState` 会被跨房间覆盖、单个 analyzer 会把各房间指标算在
一起。v3.1 把这四处拆开：每房间一份 `JsonlStorage` / `SharedState` / 队列 /
analyzer 线程，全部封装在 `room_runtime.RoomRuntime` 里。

★ 「实时台词辅助」与「竞品采集」如何并存（老大 2026-09-29 的要求）
------------------------------------------------
**不需要两套模式。** 台词辅助（LLM 层）与竞品采集（飞书落盘层）是**同一个
analyzer 的两个下游**，靠 `rooms[i].llm` / `rooms[i].feishu` 两个房间级开关
区分，同一个进程里可以同时跑：

    "rooms": [
      {"room_id": "<自家主播>", "label": "自家-主播A", "llm": true,  "feishu": true},
      {"room_id": "<竞品号>",   "label": "竞品-就业",   "llm": false, "feishu": true}
    ]

自动分析以运行时总控和显式主播许可为准；旧房间 llm 值仅为启动意向。
飞书房间级开关仍只能收窄全局许可，避免「房间说开了、其实后端
没配」的假成功。

数据源选择
----------
config.json 的 source.platform 或 --source（覆盖配置），取值 douyin_reverse
（抖音 web 逆向，默认）/ bilibili_web（B站网页游客源）。多房间下**所有房间
共用同一个数据源**（竞品池都在抖音，暂不需要混合源）。

输出
----
    单房间  data/danmu_{platform}_YYYY-MM-DD.jsonl
    多房间  data/danmu_{platform}_{房间标识}_YYYY-MM-DD.jsonl

每行一条 JSON（统一事件 schema，见 sources/base.py）：
    {"ts": ..., "platform": "douyin", "type": "danmaku", "user_id": "...",
     "uname": "...", "text": "...", "room_id": "...", "extra": {}}

规则层（analyzer.py）作为旁路独立线程消费同一份 record，不影响落盘。
LLM 层（llm_service.py）默认静默关闭；启用后再增一条独立 daemon 线程消费
快照/冷场事件，慢或失败均不阻塞采集与规则分析。
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import List

from scripts.local_installation import bootstrap_entry
bootstrap_entry()

from analyzer import load_analyzer_config
from config import (
    ConfigError,
    DEFAULT_CONFIG_PATH,
    RoomConfig,
    load_config,
    load_room_configs,
)
from feishu_sink import FeishuConfigError, load_feishu_config
from llm_service import load_anchor_config, load_llm_config
from room_runtime import RoomRuntime
from sources import SOURCE_PLATFORM
from scripts.local_context import (activate_customer_environment, customer_stop_requested,
    load_customer_context)

CUSTOMER = load_customer_context()
activate_customer_environment(CUSTOMER)

logger = logging.getLogger("danmu")

# 多房间启动时的错峰间隔：避免 N 条逆向连接在同一秒抢签名/握手
STAGGER_SECONDS = 1.5

# 运行中每多少秒打一次各房间存活状态
STATUS_INTERVAL_SECONDS = 30.0


def setup_logging() -> None:
    """日志格式：时间 - 级别 - 消息"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="直播弹幕采集客户端（可替换数据源）")
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"配置文件路径（默认 {DEFAULT_CONFIG_PATH}）",
    )
    # 抖音 web_rid 可以是纯数字、完整 URL、或 v.douyin.com 短链；B站源填房间号
    parser.add_argument("--room", type=str, default=None,
                        help="覆盖配置中的房间号（抖音：web_rid/URL/短链；B站：房间号）。"
                             "⚠️ 与 config.json 的 rooms 数组互斥")
    parser.add_argument("--source", type=str, default=None,
                        choices=sorted(SOURCE_PLATFORM),
                        help="采集数据源（覆盖 config.json 的 source.platform）："
                             "douyin_reverse=抖音 web 逆向（默认），bilibili_web=B站网页游客源")
    parser.add_argument("--llm", action="store_true",
                           help="兼容旧启动意向；自动分析仍需全局许可和该主播自动开关")
    return parser.parse_args()


def _resolve_rooms(cfg, args):
    """决定本次要跑哪些房间，返回 `(rooms, is_multi)`。

    - config.json 有 `rooms` → 多房间模式（`is_multi=True`，落盘文件名带房间标识）
    - 没有 → legacy 单房间（web_rid / --room），`is_multi=False`，文件名与 v3.0 一致

    抛 ConfigError 表示配置层面就不该启动。
    """
    room_cfgs = load_room_configs(args.config)

    if room_cfgs and args.room:
        raise ConfigError(
            "config.json 里已配置 rooms 数组，不能再传 --room"
            "（--room 是单房间模式的入口，两者同时给会互相覆盖）"
        )

    if room_cfgs:
        if len(room_cfgs) > cfg.max_rooms:
            raise ConfigError(
                f"rooms 配置了 {len(room_cfgs)} 个房间，超过 max_rooms={cfg.max_rooms}。"
                f"每个房间是一条独立的逆向长连，暴露面随房间数线性上升；"
                f"确实要更多请显式调大 max_rooms"
            )
        # 多房间下顶层 web_rid 允许为空 → 跳过该项校验
        cfg.validate(require_room=False)
        for i, room in enumerate(room_cfgs):
            if not room.label and not str(room.room_id).strip().isdigit():
                logger.warning(
                    "rooms[%d] 的 room_id 不是纯数字（%s）且未填 label，"
                    "落盘文件名只能用 room%d_<hash>，建议补 label 以便人读",
                    i, room.room_id, i + 1,
                )
        return room_cfgs, True

    # legacy 单房间
    cfg.validate()
    return [RoomConfig(room_id=cfg.web_rid, label="")], False


def _wait_until_done(runtimes: List[RoomRuntime]) -> None:
    """主线程等所有房间结束；每 STATUS_INTERVAL_SECONDS 打一次存活状态。

    房间级别的结束（直播结束 / 连接被断且不再重连）都是正常现象，逐个记日志；
    **全部结束**后返回，让上层走正常收尾（不挂死等 Ctrl+C）。
    """
    finished: set = set()
    last_status = time.monotonic()
    while True:
        if CUSTOMER and customer_stop_requested(CUSTOMER, allow_parent=True):
            return
        time.sleep(1.0)
        for rt in runtimes:
            rt.reconcile_llm()
        if time.monotonic() - last_status < STATUS_INTERVAL_SECONDS:
            if any(rt.is_alive() for rt in runtimes):
                continue
        last_status = time.monotonic()
        alive = []
        for rt in runtimes:
            if rt.is_alive():
                alive.append(rt.tag)
                continue
            if rt.tag in finished:
                continue
            finished.add(rt.tag)
            if rt.adapter_error is not None:
                logger.warning("[房间 %s] 采集线程已退出（异常：%s）",
                               rt.tag, rt.adapter_error)
            else:
                logger.info("[房间 %s] 采集线程已正常结束", rt.tag)
        logger.info("运行中房间 %d/%d：%s",
                    len(alive), len(runtimes), ", ".join(alive) or "（无）")
        if not alive:
            logger.info("所有房间均已结束，准备退出")
            return


def main() -> int:
    setup_logging()
    args = parse_args()

    # ------------------------------------------------------------------ 配置
    try:
        cfg = load_config(args.config, room_override=args.room)
        if CUSTOMER:
            cfg.output_dir = CUSTOMER["data"]
        if args.source:
            cfg.source = args.source
    except FileNotFoundError:
        logger.error("找不到配置文件：%s", args.config)
        return 1
    except Exception as e:
        logger.error("加载配置失败：%s", e)
        return 1

    try:
        rooms, multi = _resolve_rooms(cfg, args)
    except ConfigError as e:
        logger.error("配置错误：%s", e)
        return 1

    # v2.6 可替换适配器：按 config/命令行选数据源；未知 source 启动即报清晰错误
    source_name = cfg.source
    platform = SOURCE_PLATFORM.get(source_name)
    if platform is None:
        logger.error("未知数据源 %r，可选值：%s（config.json 的 source.platform "
                     "或命令行 --source）", source_name, sorted(SOURCE_PLATFORM))
        return 1

    # 规则层 / LLM 层 / 飞书层 / 人设卡：**全局各一份**，房间共享只读配置
    analyzer_cfg = load_analyzer_config(args.config)

    anchor_cfg = load_anchor_config(args.config)
    if anchor_cfg.enabled:
        for room in rooms:
            if room.room_id and not str(room.room_id).strip().isdigit():
                logger.warning(
                    "anchor.enabled=true 但房间 %s 的 room_id 非纯数字，"
                    "建议传纯数字 room_id，否则人设可能不生效（惰性加载已兜底短链/URL）",
                    room.label or room.room_id,
                )

    llm_cfg = load_llm_config(CUSTOMER["config"] if CUSTOMER else args.config)
    if llm_cfg.enabled and not llm_cfg.is_active:
        logger.warning("llm.enabled=true 但 providers 中所有 api_key 为空，自动分析等待后补配置")

    try:
        feishu_cfg = load_feishu_config(args.config)
        feishu_cfg.validate()
    except FeishuConfigError as e:
        logger.error("飞书落盘配置错误：%s", e)
        return 1

    # ------------------------------------------------------------------ 装配
    # 落盘文件名是否带房间标识：单房间（legacy）不带，保持与 v3.0 完全一致，
    # 历史数据与既有脚本可无缝续用；多房间必须带，否则各房间会插花写同一文件。
    name_in_file = multi

    runtimes: List[RoomRuntime] = []
    for i, room in enumerate(rooms):
        runtimes.append(RoomRuntime(
            room=room,
            cfg=cfg,
            analyzer_cfg=analyzer_cfg,
            llm_cfg=llm_cfg,
            feishu_cfg=feishu_cfg,
            anchor_cfg=anchor_cfg,
            source_name=source_name,
            platform=platform,
            index=i,
            name_in_file=name_in_file,
        ))

    logger.info(
        "启动弹幕采集 source=%s platform=%s 模式=%s 房间数=%d output_dir=%s 心跳间隔=%ss",
        source_name, platform, "多房间" if multi else "单房间",
        len(runtimes), cfg.output_dir, cfg.heartbeat_interval,
    )
    for rt in runtimes:
        logger.info("  · %s", rt.room.describe())

    # ------------------------------------------------------------------ 运行
    started: List[RoomRuntime] = []
    try:
        for i, rt in enumerate(runtimes):
            if CUSTOMER and customer_stop_requested(CUSTOMER, allow_parent=True):
                raise KeyboardInterrupt
            try:
                rt.start()
            except Exception as e:
                # 装配期失败（如 feishu.backend=app 但某房间没配 table_id）
                # → 立刻停掉已启动的房间并整体退出。**不静默跳过这个房间**：
                # 「配了但没生效」比直接报错难查得多。
                logger.error("[房间 %s] 启动失败：%s", rt.tag, e)
                logger.error("已中止启动，正在收尾已运行的其他房间...")
                for done in reversed(started):
                    done.stop()
                return 1
            started.append(rt)
            if i + 1 < len(runtimes):
                time.sleep(STAGGER_SECONDS)
        if len(started) > 1:
            logger.info("全部 %d 个房间已启动（错峰 %.1fs），按 Ctrl+C 退出",
                        len(started), STAGGER_SECONDS)
        _wait_until_done(started)
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，准备优雅退出...")
    finally:
        # 逆序停止：先起的后停，保证日志顺序可读
        for rt in reversed(started):
            rt.stop()
        if CUSTOMER:
            for rt in reversed(started):
                rt.wait_customer_cleanup()
        total = sum(rt.msg_count() for rt in started)
        if len(started) > 1:
            logger.info("已退出，%d 个房间共落盘消息 %s 条，明细：", len(started), total)
            for rt in started:
                logger.info("  · [%s] %s 条", rt.tag, rt.msg_count())
        else:
            logger.info("已退出，本次共落盘消息 %s 条", total)

    return 0


if __name__ == "__main__":
    sys.exit(main())
