"""单个直播间的完整运行时（v3.1 多房间）。

为什么需要这个模块
------------------------------------------------
v3.0 及以前，main.py 里是**一套全局资源**：一个 storage、一个 record_queue、
一个 SharedState、一个 analyzer 线程。多房间下这些**全部会被互相污染**：

- `JsonlStorage` 文件名不含 room_id → 多个房间插花写同一个文件
- 单 `SharedState` 被 `client._resolve_room()` 跨房间覆盖 → 人设卡错配
- 单 `analyzer` 吃合并队列 → 各房间的冷场/活跃/高频词**混在一起算，数据全错**

本模块把「一个房间的完整接线」封成一个 `RoomRuntime`，**一个房间一套，
互不共享任何可变状态**。

★ 与「实时台词辅助」的关系（老大 2026-09-29 的要求）
------------------------------------------------
台词辅助（LLM 层）与竞品采集（飞书落盘层）**不是两种模式，而是同一个
analyzer 的两个下游**，所以**不需要第二套代码**。旧静态启动意向示例：

- 对内（自家主播）→ `llm: true`  → 出冷场救场台词 / 观众画像，可另挂 `feishu: true` 留档
- 对外（竞品号）  → `llm: false` → 只落飞书指标（省钱，且竞品不需要秒级实时）

两边可以**同时跑**：进程里每个房间各有一套，互不干扰。

★ 自动分析许可
------------------------------------------------
- 标准应用的 LLM 自动许可由主配置/运行时总控及显式 tags_on 共同决定。
- 自动还须本次房间状态明确在播且当前连接已建立；未知、离线和重连均等待。
- 旧房间 llm 值只是启动意向；补 key 不自动分析，明确许可后可热建 worker。
- 无配置来源的内存集成保留旧静态开关契约。飞书仍沿用房间只能收窄的规则。
"""

from __future__ import annotations

import logging
import json
import os
import queue
import tempfile
import threading
import time
from dataclasses import replace
from typing import Any, Dict, List, Optional

from analyzer import run_analyzer
from config import RoomConfig
from feishu_sink import FeishuSink, run_feishu_worker
from llm_service import DailyJsonlWriter, SharedState, run_llm_worker
from sources import build_adapter
from storage import JsonlStorage

logger = logging.getLogger("danmu")

# 规则层默认快照间隔（无任何下游时的兜底值，与 v2 一致）
DEFAULT_SNAPSHOT_INTERVAL = 60


def _process_created_at():
    """An OS process identity; missing identity is never evidence of a live worker."""
    try:
        if os.name == "nt":
            import ctypes
            values = [ctypes.c_ulonglong() for _ in range(4)]
            kernel = ctypes.windll.kernel32
            if kernel.GetProcessTimes(ctypes.c_void_p(-1),
                                      *(ctypes.byref(value) for value in values)):
                return values[0].value / 1e7 - 11644473600.0
        else:
            with open("/proc/self/stat", encoding="ascii") as source:
                ticks = int(source.read().rsplit(")", 1)[1].split()[19])
            with open("/proc/stat", encoding="ascii") as source:
                boot = next(int(line.split()[1]) for line in source if line.startswith("btime "))
            return boot + ticks / os.sysconf("SC_CLK_TCK")
    except Exception:
        pass
    return None


class ColdEventLog:
    """规则层冷场事件落盘（2026-10-26）。

    为什么需要它
    ------------
    冷场判定一直是 analyzer（规则层）算的，但落盘只有 LLM 层会写
    `coldline_<date>.jsonl`（带救场台词）—— 于是**没开 LLM 的场次冷场次数永远是 0**，
    复盘页看起来像「主播全程没冷过场」（冤种小徐 2026-10-25 就是这么被误读的：
    日志里 15:29/15:31/15:37 三次进冷场，页面上写着 0）。

    这里在规则层出口直接落一份 `cold_events_<date>.jsonl`：零 API 成本、
    零额外进程、与开不开 LLM 无关。cold_end 带上 duration（本场持续秒数）。

    ★ 2026-11-01 审计 P0：原来 cold_start 只在内存里等 cold_end，进程被硬杀
      （10-29 内存耗尽 Id=2004 那次）或主播一直冷到收播时，整段消失 —— 复盘页
      看到的是「这段一直有人说话」。现在两条兜底：达到 min_duration 就立刻落盘
      cold_start；close() 时补写未闭合的那段并标 ``incomplete: true``。
    """

    def __init__(self, output_dir: str, room_tag: Optional[str],
                 room_id: str, date: Optional[str] = None,
                 min_duration: int = 0) -> None:
        # date 只在回填历史时显式传（生产走「今天」）
        self._writer = DailyJsonlWriter(output_dir, "cold_events", date=date)
        # 用文件名那个 tag：复盘页是按文件名里的 tag 反查的
        self._room_tag = room_tag or ""
        self._room_id = room_id or ""
        self._start_ts: Optional[int] = None
        # 短于该秒数的冷场段不落盘（阈值降到 0.5/1.0 后会冒出大量瞬时抖动）
        self._min_duration = max(0, int(min_duration or 0))
        self._pending: Optional[Dict[str, Any]] = None
        # 这段 cold_start 是否已经落过盘（提前落盘 / 正常闭合都要防重复写）
        self._pending_persisted = False
        # ★ 提前落盘用定时器：cold_start 到达 min_duration 的那一刻就写，
        #   不等 cold_end —— 进程被硬杀时 cold_end 永远不会来（2026-11-01 审计 P0）。
        self._timer: Optional[threading.Timer] = None
        # 定时器线程与 analyzer 线程会碰同一批字段；RLock 允许 _flush_pending 内部
        # 再取锁（Lock 会自锁）
        self._lock = threading.RLock()

    def __call__(self, event: Dict[str, Any]) -> None:
        name = event.get("event")
        ts = int(event.get("ts") or time.time())
        rec: Dict[str, Any] = {
            "ts": ts,
            "event": name,
            "room_tag": self._room_tag,
            "room_id": self._room_id,
            "count": event.get("count"),
            "threshold": event.get("threshold"),
        }
        if name == "cold_start":
            # 上一段没闭合就又来一段 → 先收掉它的定时器。否则回填（一天几千段）
            # 会堆出几千个待触发的线程，而且旧 timer 醒来还会把新段当成自己那段。
            self._cancel_timer()
            with self._lock:
                self._start_ts = ts
                # 先暂存：要等 cold_end 算出时长才知道这段够不够长
                self._pending = rec
                self._pending_persisted = False
            # ★ 达到最小持续时长就立刻落盘，不等 cold_end（见类文档 §修法 1）
            if self._min_duration <= 0:
                self._flush_pending()          # 阈值关掉 → 当场就是一段有效冷场
            else:
                self._arm_timer(self._min_duration)
            return
        if name == "cold_end":
            self._cancel_timer()
            with self._lock:
                duration: Optional[int] = None
                if self._start_ts is not None:
                    duration = ts - self._start_ts
                pending = self._pending
                persisted = self._pending_persisted
                self._start_ts = None
                self._pending = None
                self._pending_persisted = False
            if pending is None:
                return          # 没有对应的 start（进程中途启动），丢弃
            if duration is not None:
                if duration < self._min_duration and not persisted:
                    return      # 未落盘的短段照旧过滤；已写 start 则必须闭合
                rec["duration"] = max(0, duration)
            # 提前落过盘的 start 不再写第二遍（否则复盘页冷场次数翻倍）
            if not persisted:
                self._write(pending)
            self._write(rec)
            return
        self._write(rec)

    # ------------------------------------------------------------ 提前落盘
    def _arm_timer(self, seconds: float) -> None:
        t = threading.Timer(seconds, self._flush_pending)
        # daemon：绝不能让一个待触发的定时器把进程退出挂住（pythonw 形态尤其致命）
        t.daemon = True
        self._timer = t
        t.start()

    def _cancel_timer(self) -> None:
        t = self._timer
        self._timer = None
        if t is not None:
            try:
                t.cancel()
            except Exception:  # noqa: BLE001
                pass

    def _flush_pending(self, mark_incomplete: bool = False) -> bool:
        """把暂存的 cold_start 落盘；返回是否真的写成功（失败留给 close 重试）。"""
        with self._lock:
            pending = self._pending
            if pending is None or self._pending_persisted:
                return False
            rec = dict(pending)
            if mark_incomplete:
                rec["incomplete"] = True
            if not self._write(rec):
                return False        # 落盘失败就当没写过，close 还有一次机会
            self._pending_persisted = True
            return True

    def _write(self, rec: Dict[str, Any]) -> bool:
        """落盘单条记录。

        ★ 2026-11-01 审计复查：这里必须持锁。Timer 线程（提前落盘 cold_start）与
          analyzer 线程（cold_end / 普通事件）会并发进来，而 DailyJsonlWriter.write
          本身没有锁（_ensure_file → file.write → flush 三步），两个线程同时写同一个
          句柄就可能写出半行 —— 正是审计 P0-7「跨进程无锁追加」的**线程版本**。
          RLock 可重入，_flush_pending 已经持锁时再进来不会自锁。
        """
        with self._lock:
            try:
                self._writer.write(rec)
                return True
            except Exception as e:  # noqa: BLE001 落盘失败不能影响冷场判定
                logger.warning("[房间 %s] 冷场事件落盘失败，已忽略: %s",
                               self._room_tag, e)
                return False

    def close(self) -> None:
        self._cancel_timer()
        # ★ 未闭合的冷场补写一条，标 incomplete：有记录、能计数，
        #   不会在复盘页伪装成「这段一直有人说话」（2026-11-01 审计 P0）
        self._flush_pending(mark_incomplete=True)
        with self._lock:
            self._pending = None
            self._pending_persisted = False
            self._start_ts = None
            # 关句柄也放进锁里：否则某个 Timer 线程刚拿到锁在写、这边已经把文件关了
            try:
                self._writer.close()
            except Exception:  # noqa: BLE001
                pass


class RoomRuntime:
    """一个直播间的运行时装配：采集 → 规则层 → 各下游（LLM / 飞书）。

    生命周期：`__init__` 只装配不启动；`start()` 起线程；`stop()` 优雅收尾。
    两个方法都幂等（重复调用不报错、不重复起线程）。
    """

    def __init__(
        self,
        room: RoomConfig,
        cfg,
        analyzer_cfg,
        llm_cfg,
        feishu_cfg,
        anchor_cfg,
        source_name: str,
        platform: str,
        index: int = 0,
        name_in_file: bool = True,
    ) -> None:
        """:param name_in_file: 落盘文件名是否带房间标识。单房间（legacy）传
            False 以保持与 v3.0 完全一致的文件名（历史数据与脚本可续用）。
        """
        self.room = room
        self.source_name = source_name
        self.platform = platform
        self.index = index
        # 日志/落盘用的房间标识（已安全化）
        self.tag = room.file_tag(index)
        self.file_room: Optional[str] = self.tag if name_in_file else None

        # ★ 每房间一份 Config：只换 web_rid，其余（cookie/heartbeat/签名）共用。
        #   adapter / client 读的是 cfg.web_rid，因此**下游零改动**。
        self.cfg = replace(cfg, web_rid=room.room_id)

        # LLM 启动意向仅供内存兼容；标准应用在 reconcile 时读实时许可。
        self.llm_enabled = bool(llm_cfg.is_active) and room.llm is not False
        self.feishu_enabled = bool(feishu_cfg.enabled) and room.feishu is not False
        if room.llm is True and not llm_cfg.is_active:
            logger.warning(
                "[房间 %s] rooms 里要求 llm=true，但全局 LLM 层未启用"
                "（llm.enabled=false 或所有 provider 的 api_key 为空）→ 启动时等待运行时许可与凭据",
                self.tag,
            )
        if room.feishu is True and not feishu_cfg.enabled:
            logger.warning(
                "[房间 %s] rooms 里要求 feishu=true，但全局 feishu 层未启用"
                "（feishu.enabled=false）→ 该房间不落飞书",
                self.tag,
            )

        # —— 房间私有资源（★ 一个房间一套，绝不共享）
        self.storage = JsonlStorage(cfg.output_dir, platform=platform, room=self.file_room)
        self.record_queue: "queue.Queue" = queue.Queue(maxsize=1000)
        self.shared_state = SharedState()

        # v3.3 竞品监控层（仅抖音源）：人气快照/系统事件/房间元数据独立落盘，
        # 与主链路（31 列契约）完全独立；B站源暂不接（协议未实现）
        self.monitor = None
        if platform == "douyin":
            from monitor import Monitor
            self.monitor = Monitor(cfg.output_dir, platform="douyin",
                                   room_tag=self.file_room or self.tag)

        self._analyzer_cfg = analyzer_cfg
        self._llm_cfg = llm_cfg
        self._feishu_cfg = feishu_cfg
        self._anchor_cfg = anchor_cfg

        # 扇出：规则层只看到 _on_event / _on_snapshot 两个回调，内部再展开给各下游
        self._snapshot_fanout: List[Any] = []
        self._event_fanout: List[Any] = []
        self._snapshot_intervals: List[int] = []

        # ★ 冷场事件落盘**始终挂上**（2026-10-26）：它不属于任何可选下游，
        #   是「冷场次数」这个数字的唯一可靠来源（LLM 层写的那份只在开 LLM 时有）。
        self._cold_event_log = ColdEventLog(
            cfg.output_dir, self.file_room or self.tag, self.cfg.web_rid,
            min_duration=getattr(self._analyzer_cfg, "cold_min_duration_seconds", 0))
        self._event_fanout.append(self._cold_event_log)

        self._llm_queue: Optional["queue.Queue"] = None
        self._llm_thread: Optional[threading.Thread] = None
        self._llm_lock = threading.RLock()
        self._llm_stop_event: Optional[threading.Event] = None
        self._llm_epoch = None
        self._llm_live_generation = None
        self._llm_generation = 0
        self._llm_wired = False
        self._llm_state = "waiting_collector"
        self._llm_reason = "not_started"
        self._llm_next_start_at = 0.0
        self._process_created_at = _process_created_at()
        self._collection_done = False
        self._feishu_queue: Optional["queue.Queue"] = None
        self._feishu_thread: Optional[threading.Thread] = None
        self._analyzer_thread: Optional[threading.Thread] = None
        self._adapter_thread: Optional[threading.Thread] = None

        self._adapter = None
        self._started = False
        self._stopped = False
        # 采集线程异常退出时置位（供 main.py 看板/日志判断该房间是否还活着）
        self.adapter_error: Optional[BaseException] = None

    # ============================================================ 装配（不启动）
    def _wire_llm(self) -> None:
        """Install one stable fanout; worker generations never add more callbacks."""
        if self._llm_wired:
            return
        self._llm_wired = True

        def _llm_on_event(event: dict) -> None:
            # 仅"冷场进入"需要救场台词，cold_end 不推给 LLM
            if event.get("event") == "cold_start":
                self._push_llm({"kind": "event", "event": event})

        def _llm_on_snapshot(snapshot: dict) -> None:
            self._push_llm({"kind": "snapshot", "snapshot": snapshot})

        self._event_fanout.append(_llm_on_event)
        self._snapshot_fanout.append(_llm_on_snapshot)
        self._snapshot_intervals.append(self._llm_cfg.portrait_interval_seconds)

    def _customer_stopping(self):
        if not os.environ.get("DY_CUSTOMER_CONTEXT"):
            return False
        try:
            from scripts.local_context import load_customer_context, customer_stop_requested
            from pathlib import Path
            context = load_customer_context()
            source = getattr(getattr(self, "_llm_cfg", None), "_source_path", "")
            if (not source or Path(source).resolve() != Path(context["config"]).resolve()
                    or Path(self.cfg.output_dir).resolve() != Path(context["data"]).resolve()):
                return True
            return customer_stop_requested(context, allow_parent=True, strict=True)
        except (OSError, ValueError, TypeError):
            return True  # An unreadable application boundary cannot admit AI work.

    def _llm_control(self):
        if self._customer_stopping():
            return {"allowed": False, "reason": "application_stopping", "epoch": None}
        if not self._llm_cfg._source_path:
            # Explicit in-memory integrations keep the old static contract.
            return {"allowed": self.llm_enabled, "reason": "legacy_memory_config", "epoch": None}
        from llm_client import application_control
        return application_control(config_path=self._llm_cfg._source_path,
                                   data_dir=self.cfg.output_dir, tag=self.tag, automatic=True)

    def _automatic_live(self):
        if not self._llm_cfg._source_path:
            return {"ready": True, "reason": "legacy_memory_config", "generation": None}
        return self.shared_state.automatic_live()

    def _push_llm(self, item: dict) -> None:
        control = self._llm_control()
        live = self._automatic_live()
        with self._llm_lock:
            if (self._stopped or not control["allowed"] or control["epoch"] != self._llm_epoch
                    or not live["ready"] or live["generation"] != self._llm_live_generation
                    or self._llm_stop_event is None or self._llm_stop_event.is_set()
                    or self._llm_thread is None or not self._llm_thread.is_alive()):
                return
            item = dict(item, _llm_epoch=self._llm_epoch, _llm_generation=self._llm_generation,
                        _llm_live_generation=self._llm_live_generation)
            try:
                self._llm_queue.put_nowait(item)
            except queue.Full:
                pass

    def _stop_llm_locked(self, reason):
        if self._llm_stop_event is not None:
            self._llm_stop_event.set()
        if self._llm_queue is not None:
            while True:
                try:
                    self._llm_queue.get_nowait()
                except queue.Empty:
                    break
            self._llm_queue.put_nowait(None)
        if self._llm_state != "error":
            self._llm_state, self._llm_reason = "stopping", reason

    def _publish_llm_locked(self):
        thread = self._llm_thread
        live = self._automatic_live()
        record = {
            "pid": os.getpid(), "process_created_at": self._process_created_at,
            "observed_at": time.time(), "room_tag": self.tag,
            "generation": self._llm_generation, "epoch": self._llm_epoch,
            "live_ready": live["ready"], "live_generation": live["generation"],
            "worker_live_generation": self._llm_live_generation,
            "thread_ident": thread.ident if thread else None,
            "thread_native_id": thread.native_id if thread else None,
            "worker_alive": bool(thread and thread.is_alive()),
            "collector_alive": self.is_alive() and not self._collection_done,
            "state": self._llm_state, "reason": self._llm_reason,
        }
        temporary = None
        try:
            os.makedirs(self.cfg.output_dir, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".llm-worker-", suffix=".tmp", dir=self.cfg.output_dir)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False)
            os.replace(temporary, os.path.join(self.cfg.output_dir, "llm_worker_%s.json" % self.tag))
        except OSError:
            logger.debug("LLM worker 状态写入失败（忽略）")
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def llm_status(self):
        with self._llm_lock:
            return {"state": self._llm_state, "reason": self._llm_reason,
                    "generation": self._llm_generation,
                    "worker_alive": bool(self._llm_thread and self._llm_thread.is_alive())}

    def reconcile_llm(self) -> None:
        """Called by the existing main loop; never wait for a retiring worker here."""
        from llm_service import load_llm_config, load_products
        control = self._llm_control()
        live = self._automatic_live()
        config_error = False
        try:
            fresh = load_llm_config(self._llm_cfg._source_path) if self._llm_cfg._source_path else self._llm_cfg
            ready = any(all(str(row.get(key) or "").strip() for key in ("api_key", "base_url", "model"))
                        for row in fresh.providers.values())
        except Exception:
            fresh, ready, config_error = None, False, True
        with self._llm_lock:
            thread = self._llm_thread
            alive = bool(thread and thread.is_alive())
            collecting = self._started and not self._stopped and self.is_alive() and not self._collection_done
            if alive and (not collecting or not control["allowed"] or not ready
                          or not live["ready"] or live["generation"] != self._llm_live_generation
                          or control["epoch"] != self._llm_epoch):
                reason = ("collection_stopped" if not collecting else control["reason"]
                          if not control["allowed"] else "configuration_unavailable" if config_error
                          else "no_credentials" if not ready else live["reason"] if not live["ready"]
                          else "live_generation_changed" if live["generation"] != self._llm_live_generation
                          else "control_epoch_changed")
                self._stop_llm_locked(reason)
            if alive:
                self._publish_llm_locked()
                return  # No second generation while the first still owns resources.
            self._llm_thread = None
            self._llm_queue = None
            self._llm_stop_event = None
            if not collecting:
                if self._llm_state != "error":
                    self._llm_state = "stopped" if self._stopped else "waiting_collector"
                    self._llm_reason = "collection_stopped" if self._stopped else "collector_not_running"
            elif config_error or control["reason"] == "control_unavailable":
                self._llm_state, self._llm_reason = "error", "configuration_unavailable"
            elif not control["allowed"]:
                if self._llm_state != "error":
                    self._llm_state, self._llm_reason = "waiting_permission", control["reason"]
            else:
                try:
                    if not ready:
                        self._llm_state, self._llm_reason = "no_key", "no_credentials"
                    elif not live["ready"]:
                        self._llm_state, self._llm_reason = "waiting_live", live["reason"]
                    elif time.monotonic() < self._llm_next_start_at:
                        self._llm_state, self._llm_reason = "error", "worker_retry_wait"
                    else:
                        products = load_products(fresh.products_file)
                        self._llm_generation += 1
                        generation = self._llm_generation
                        self._llm_epoch = list(control["epoch"]) if control["epoch"] is not None else None
                        self._llm_live_generation = live["generation"]
                        self._llm_queue = queue.Queue(maxsize=200)
                        self._llm_stop_event = threading.Event()
                        q, stopped, epoch = self._llm_queue, self._llm_stop_event, self._llm_epoch
                        live_generation = self._llm_live_generation
                        def request_allowed():
                            current = self._automatic_live()
                            return (not self._customer_stopping() and not stopped.is_set() and current["ready"]
                                    and current["generation"] == live_generation)
                        def state_changed(state):
                            with self._llm_lock:
                                if generation != self._llm_generation:
                                    return
                                if stopped.is_set() and state not in ("stopped", "error"):
                                    return
                                self._llm_state, self._llm_reason = state, "worker_" + state
                                if state == "error":
                                    self._llm_next_start_at = time.monotonic() + 5.0
                                self._publish_llm_locked()
                        def run_generation():
                            try:
                                run_llm_worker(q, fresh, self.cfg.output_dir, products=products,
                                               room_id=self.cfg.web_rid, room_tag=self.tag,
                                               shared_state=self.shared_state, anchor_cfg=self._anchor_cfg,
                                               stop_event=stopped, control_epoch=epoch,
                                               worker_generation=generation, live_generation=live_generation,
                                               request_guard=request_allowed, on_state=state_changed)
                            except Exception:
                                state_changed("error")
                            finally:
                                with self._llm_lock:
                                    if self._llm_state != "error":
                                        state_changed("stopped")
                        self._llm_state, self._llm_reason = "starting", "worker_starting"
                        self._llm_thread = threading.Thread(target=run_generation,
                                                           name=f"llm-{self.tag}-{generation}", daemon=True)
                        self._llm_thread.start()
                except Exception:
                    self._llm_state, self._llm_reason = "error", "configuration_unavailable"
                    self._llm_next_start_at = time.monotonic() + 5.0
            self._publish_llm_locked()

    def _wire_feishu(self) -> None:
        """为「竞品数据落盘」挂上飞书下游（对外房间）。"""
        self._feishu_queue = queue.Queue(maxsize=self._feishu_cfg.queue_maxsize)

        def _push_feishu(snapshot: dict) -> None:
            try:
                self._feishu_queue.put_nowait(snapshot)
            except queue.Full:
                pass

        self._snapshot_fanout.append(_push_feishu)
        self._snapshot_intervals.append(self._feishu_cfg.snapshot_interval_seconds)

        # ★ 在**主线程**构造 sink（而不是让 worker 线程内部懒构造）：
        #   app 后端下 sink 构造期要解析 table_id，配漏了会抛 FeishuConfigError。
        #   若在 worker 线程里抛，线程会静默死掉 → 「房间在跑但一行都没落」，
        #   这正是最难查的一类故障。放主线程抛，start() 直接失败，启动即报错。
        sink = FeishuSink(
            self._feishu_cfg,
            room_id=self.cfg.web_rid,
            platform=self.platform,
            active_top_cap=self._analyzer_cfg.active_top_n,
            room_tag=self.file_room,
        )

        self._feishu_thread = threading.Thread(
            target=run_feishu_worker,
            kwargs={
                "q": self._feishu_queue,
                "cfg": self._feishu_cfg,
                "room_id": self.cfg.web_rid,
                "platform": self.platform,
                "active_top_cap": self._analyzer_cfg.active_top_n,
                "room_tag": self.file_room,
                "sink": sink,
            },
            name=f"feishu-{self.tag}",
            daemon=True,
        )

    def _build_callbacks(self) -> tuple:
        """组合扇出回调。

        与 v2 的差别（2026-10-26）：**冷场事件始终有下游**（规则层落盘），
        所以 on_event 不再可能为 None；快照仍然无下游时给 None（省一次序列化）。
        """
        def _on_event(event: dict) -> None:
            for fn in self._event_fanout:
                try:
                    fn(event)
                except Exception as e:
                    logger.warning("[房间 %s] 冷场事件下游回调异常，已忽略: %s",
                                   self.tag, e)

        def _on_snapshot(snapshot: dict) -> None:
            for fn in self._snapshot_fanout:
                try:
                    fn(snapshot)
                except Exception as e:
                    logger.warning("[房间 %s] 周期快照下游回调异常，已忽略: %s",
                                   self.tag, e)

        on_event = _on_event if self._event_fanout else None
        on_snapshot = _on_snapshot if self._snapshot_fanout else None
        return on_event, on_snapshot

    def snapshot_interval(self) -> int:
        """规则层推送快照的间隔 = 所有约束的**最小值**（保证每个下游都够用）。

        房间级 `snapshot_interval_seconds` 只是再加一条约束；下游各自的节流
        （LLM 在途判定 / 飞书 min_interval）仍在各自线程里做。
        """
        candidates = [i for i in self._snapshot_intervals if i and i > 0]
        if self.room.snapshot_interval_seconds > 0:
            candidates.append(self.room.snapshot_interval_seconds)
        if not candidates:
            return DEFAULT_SNAPSHOT_INTERVAL
        return min(candidates)

    # ============================================================ 启动
    def start(self) -> None:
        with self._llm_lock:
            if self._started or self._stopped:
                return
            self._started = True

        if self.llm_enabled or self._llm_cfg._source_path:
            self._wire_llm()
        if self.feishu_enabled:
            self._wire_feishu()

        on_event, on_snapshot = self._build_callbacks()
        snap_interval = self.snapshot_interval()

        self._analyzer_thread = threading.Thread(
            target=run_analyzer,
            args=(self.record_queue, self._analyzer_cfg),
            kwargs={
                "on_event": on_event,
                "on_snapshot": on_snapshot,
                "snapshot_interval_seconds": snap_interval,
            },
            name=f"analyzer-{self.tag}",
            daemon=True,
        )
        self._analyzer_thread.start()

        # 采集适配器也放进独立线程：多房间下必须并发跑 N 条长连
        self._adapter = build_adapter(
            self.source_name, self.cfg, self.storage,
            record_queue=self.record_queue, shared_state=self.shared_state,
            monitor=self.monitor,
        )
        self._adapter_thread = threading.Thread(
            target=self._run_adapter, name=f"adapter-{self.tag}", daemon=True
        )
        self._adapter_thread.start()

        self.reconcile_llm()
        if self._feishu_thread is not None:
            self._feishu_thread.start()

        logger.info(
            "[房间 %s] 已启动 %s room_id=%s 落盘=%s 规则快照=%ss LLM=%s 飞书=%s 监控=%s",
            self.tag, self.source_name, self.room.room_id,
            self.storage.current_path or "(待写入)", snap_interval,
            self.llm_status()["state"], self.feishu_enabled, self.monitor is not None,
        )
        if self._llm_thread is not None:
            logger.info(
                "[房间 %s] LLM 层已启用 active_provider=%s providers=%s 画像间隔=%ss "
                "冷场台词=%s(最小间隔=%ss) 推荐=%s metrics=%s",
                self.tag, self._llm_cfg.active_provider,
                list(self._llm_cfg.providers.keys()),
                self._llm_cfg.portrait_interval_seconds, self._llm_cfg.coldline_enabled,
                self._llm_cfg.coldline_min_interval_seconds,
                self._llm_cfg.recommend_enabled, self._llm_cfg.metrics_enabled,
            )
        if self._feishu_thread is not None:
            logger.info("[房间 %s] 飞书落盘已启用 backend=%s 最小发送间隔=%ss",
                        self.tag, self._feishu_cfg.backend,
                        self._feishu_cfg.min_interval_seconds)

    def _run_adapter(self) -> None:
        """采集线程体：异常绝不能静默吞掉（否则房间"看起来在跑"其实已死）。"""
        try:
            self._adapter.run()
        except Exception as e:  # noqa: BLE001 - 兜底，必须记下来
            self.adapter_error = e
            logger.error("[房间 %s] 采集线程异常退出: %s", self.tag, e, exc_info=True)
        finally:
            self._collection_done = True
            self.reconcile_llm()

    # ============================================================ 停止
    def stop(self, join_timeout: float = 3.0) -> None:
        """优雅收尾：先停采集（不再产数据）→ 再依次排空各下游 → 关文件。

        幂等：重复调用安全。任何一步失败都只记日志，不影响其他房间收尾。
        """
        with self._llm_lock:
            if self._stopped:
                return
            self._stopped = True
            self._stop_llm_locked("collection_stopped")
            llm_thread = self._llm_thread

        # 1) 先停采集，避免边收边停
        if self._adapter is not None:
            try:
                self._adapter.close()
            except Exception as e:
                logger.warning("[房间 %s] 关闭采集适配器异常: %s", self.tag, e)
        if self._adapter_thread is not None:
            self._adapter_thread.join(timeout=join_timeout)

        # 2) 规则层
        self._drain(self.record_queue, self._analyzer_thread, "analyzer", join_timeout)

        # 3) 下游（LLM / 飞书）；每条落盘均已 flush，join 超时也由 daemon 兜底
        if llm_thread is not None:
            llm_thread.join(timeout=max(0.0, join_timeout))
        self.reconcile_llm()
        self._drain(self._feishu_queue, self._feishu_thread, "feishu", join_timeout)

        if os.environ.get("DY_CUSTOMER_CONTEXT"):
            pending = [thread for thread in (self._adapter_thread, self._analyzer_thread,
                llm_thread, self._feishu_thread) if thread is not None and thread.is_alive()]
            if pending:
                self._customer_cleanup_threads = pending
                return  # The application stays stopping and keeps writer resources.
        self._finish_storage()

    def wait_customer_cleanup(self):
        """Wait in the owned process; the controlling CLI remains bounded."""
        for thread in getattr(self, "_customer_cleanup_threads", []):
            while thread.is_alive():
                thread.join(timeout=.2)
        if hasattr(self, "_customer_cleanup_threads"):
            self._finish_storage()
            self._customer_cleanup_threads = []

    def _finish_storage(self):
        if getattr(self, "_storage_closed", False):
            return
        self._storage_closed = True
        # 4) 关落盘文件（监控层文件一并关）
        try:
            self.storage.close()
        except Exception as e:
            logger.warning("[房间 %s] 关闭落盘文件异常: %s", self.tag, e)
        try:
            self._cold_event_log.close()
        except Exception as e:
            logger.warning("[房间 %s] 关闭冷场事件文件异常: %s", self.tag, e)
        if self.monitor is not None:
            try:
                self.monitor.close()
            except Exception as e:
                logger.warning("[房间 %s] 关闭监控层文件异常: %s", self.tag, e)

        msg_count = getattr(self._adapter, "msg_count", 0)
        mon_stats = getattr(self.monitor, "stats_count", 0) if self.monitor else 0
        mon_events = getattr(self.monitor, "events_count", 0) if self.monitor else 0
        if self.monitor is not None:
            logger.info("[房间 %s] 已停止，本次共落盘消息 %s 条（监控层：快照 %s 条 / 事件 %s 条）",
                        self.tag, msg_count, mon_stats, mon_events)
        else:
            logger.info("[房间 %s] 已停止，本次共落盘消息 %s 条", self.tag, msg_count)

    @staticmethod
    def _drain(q: Optional["queue.Queue"], th: Optional[threading.Thread],
               name: str, timeout: float) -> None:
        if q is None or th is None:
            return
        try:
            q.put_nowait(None)  # 哨兵
        except queue.Full:
            # 队列满说明下游已积压，直接等它把现有条目处理完并超时退出
            pass
        th.join(timeout=timeout)

    def is_alive(self) -> bool:
        """该房间的采集线程是否还在跑（供监控/看板判断）。"""
        return bool(self._adapter_thread and self._adapter_thread.is_alive())

    def msg_count(self) -> int:
        return int(getattr(self._adapter, "msg_count", 0))
