# -*- coding: utf-8 -*-
"""SSE 广播器：一秒一帧，所有连接共享同一份快照。

为什么需要它（从「本机工具」变「多人 Web 应用」的关键改造）：
    旧实现里每个 SSE 连接各自调用 store.snapshot()，1 个人没事；
    10 个同事 = 每秒 10 次全量遍历 + 序列化。而且每个人拿到的
    now_ts 不同，同一时刻看到的数字对不上。

    改成全局 1s 一帧：只序列化一次，所有订阅者收同一份字节。

通道设计：
    event: snapshot  轻量帧（KPI/画像/冷场/最近若干条弹幕）
    event: feed      增量帧（这一秒新增的弹幕，按 seq 去重）
    comment: ping    保活（每 ping_every 帧插一次，frp/代理不会掐断）

慢消费者（网络卡）不阻塞广播：队列满时丢最旧帧。
"""
from __future__ import annotations

import json
import queue
import threading
import time

QUEUE_SIZE = 2      # 只保留最新两帧：慢客户端拿到的是「稍微旧的现在」，不是积压


class Broadcaster:
    def __init__(self, snapshot_fn, interval: float = 1.0,
                 ping_every: int = 15, feed_fn=None):
        """
        :param snapshot_fn: 无参可调用，返回 dict（一帧快照）
        :param interval: 帧间隔秒
        :param ping_every: 每 N 帧插一个 ping（0 = 不插）
        :param feed_fn: 无参可调用，返回「这一秒的新增弹幕」list 或 None
        """
        self.snapshot_fn = snapshot_fn
        self.feed_fn = feed_fn
        self.interval = interval
        self.ping_every = ping_every
        self._subs: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self._frames = 0
        self._stop = threading.Event()
        self._last_error = None
        self._thread: threading.Thread | None = None

    # ---------- 订阅 ----------
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=QUEUE_SIZE)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    @property
    def subscribers(self) -> int:
        with self._lock:
            return len(self._subs)

    # ---------- 广播 ----------
    def _push(self, item) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(item)
            except queue.Full:
                # 丢最旧的一帧再塞：宁可少一帧，也不要让整个广播被一个慢客户端拖住
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(item)
                except queue.Full:
                    pass

    @staticmethod
    def _dumps(obj) -> str:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))

    def tick(self, now: float | None = None) -> dict:
        """生成并广播一帧；返回快照 dict（测试可直接调）。"""
        snap = self.snapshot_fn()
        self._push(("snapshot", self._dumps(snap)))
        if self.feed_fn is not None:
            try:
                feed = self.feed_fn()
            except Exception:
                feed = None
            if feed:
                self._push(("feed", self._dumps(feed)))
        self._frames += 1
        if self.ping_every and self._frames % self.ping_every == 0:
            self._push(("ping", ""))
        return snap

    # ---------- 生命周期 ----------
    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
                self._last_error = None
            except Exception as e:      # 单帧失败不能杀线程
                self._last_error = repr(e)
            self._stop.wait(self.interval)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run, name="board-broadcast", daemon=True)
        t.start()
        self._thread = t
        return t

    def stop(self) -> None:
        self._stop.set()

    @property
    def last_error(self) -> str | None:
        return self._last_error


def sse_frame(event: str | None, data: str) -> bytes:
    """把 (event, data) 编码成 SSE 报文；event=None 表示注释帧。"""
    if event is None:
        return b": " + (data or "ping").encode("utf-8") + b"\n\n"
    return ("event: %s\ndata: %s\n\n" % (event, data)).encode("utf-8")


def stream_to(send, broadcaster: Broadcaster, timeout: float = 30.0) -> None:
    """把广播队列写进 SSE 连接。send() 抛异常即视为客户端断开。

    独立成函数（而不是塞在 Handler 里）是为了可单测：传入假 send 就能验。
    """
    q = broadcaster.subscribe()
    try:
        send(sse_frame(None, "connected"))      # 先吐一帧注释，让代理立即建立流
        while True:
            try:
                kind, data = q.get(timeout=timeout)
            except queue.Empty:
                send(sse_frame(None, "ping"))
                continue
            if kind == "ping":
                send(sse_frame(None, "ping"))
            elif kind == "snapshot":
                send(sse_frame("snapshot", data))
            else:
                send(sse_frame("feed", data))
    finally:
        broadcaster.unsubscribe(q)
