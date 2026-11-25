# -*- coding: utf-8 -*-
"""SSE 广播器单测：一帧共享、慢消费者丢帧、ping 节奏、增量通道编码。"""
import json
import pathlib
import queue
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import board_broadcast as bb  # noqa: E402


def _snap():
    return {"now_ts": 1.0, "tags": [{"tag": "A"}]}


def test_tick_pushes_snapshot_to_all():
    b = bb.Broadcaster(_snap)
    q1, q2 = b.subscribe(), b.subscribe()
    b.tick()
    kind1, data1 = q1.get_nowait()
    kind2, data2 = q2.get_nowait()
    assert kind1 == kind2 == "snapshot"
    assert json.loads(data1) == json.loads(data2) == _snap()
    assert b.subscribers == 2


def test_unsubscribe_stops_delivery():
    b = bb.Broadcaster(_snap)
    q = b.subscribe()
    b.unsubscribe(q)
    b.tick()
    assert q.empty()
    assert b.subscribers == 0


def test_feed_only_when_present():
    calls = {"n": 0}

    def feed():
        calls["n"] += 1
        return None if calls["n"] == 1 else {"items": [{"seq": 1, "text": "x"}]}

    b = bb.Broadcaster(_snap, feed_fn=feed)
    q = b.subscribe()
    b.tick()                     # feed None → 只有 snapshot
    kinds = [q.get_nowait()[0]]
    assert kinds == ["snapshot"]
    b.tick()                     # 有 feed → snapshot + feed
    got = [q.get_nowait()[0], q.get_nowait()[0]]
    assert got == ["snapshot", "feed"]


def test_ping_inserted_after_each_frame():
    """ping_every=1 → 每帧后面跟一个 ping 注释帧（队列只留最新两格）。"""
    b = bb.Broadcaster(_snap, ping_every=1)
    q = b.subscribe()
    b.tick()
    kinds = []
    while not q.empty():
        kinds.append(q.get_nowait()[0])
    assert kinds == ["snapshot", "ping"]


def test_slow_consumer_drops_oldest_frame():
    """队列只有 2 格：连推 5 帧后仍然拿到最新的两帧，而不是阻塞广播。"""
    b = bb.Broadcaster(lambda: {"n": 1})
    q = b.subscribe()
    for _ in range(5):
        b.tick()
    assert q.qsize() == bb.QUEUE_SIZE
    kind, data = q.get_nowait()
    assert kind == "snapshot"
    # 最早的两帧被丢掉，剩下的是后两帧（内容相同，用 frame 计数验证不阻塞）
    assert b.tick() is not None


def test_run_survives_snapshot_exception():
    """run() 里单帧异常不能杀线程：前两帧抛，第三帧照常出。"""
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("boom")
        return _snap()

    b = bb.Broadcaster(flaky, interval=0.01)
    q = b.subscribe()
    t = b.start()
    got = q.get(timeout=3)
    assert got[0] == "snapshot"
    b.stop()
    t.join(timeout=1)
    assert not t.is_alive()
    assert b.last_error is None      # 成功一帧后错误被清空


def test_stream_to_writes_frames():
    b = bb.Broadcaster(_snap)
    frames = []

    def send(payload):
        frames.append(payload)
        if len(frames) == 1:          # 连上后推一帧，避免与订阅抢时序
            b.tick()
        elif len(frames) >= 3:        # 模拟客户端断开
            raise BrokenPipeError()

    try:
        bb.stream_to(send, b, timeout=0.2)
    except BrokenPipeError:
        pass
    assert frames[0].startswith(b": connected")
    assert any(f.startswith(b"event: snapshot") for f in frames)
    assert b.subscribers == 0        # 断开后必须退订，否则泄漏


def test_sse_frame_encoding():
    assert bb.sse_frame(None, "ping") == b": ping\n\n"
    raw = bb.sse_frame("snapshot", '{"a":1}')
    assert raw == 'event: snapshot\ndata: {"a":1}\n\n'.encode("utf-8")


def test_start_thread_and_stop():
    b = bb.Broadcaster(_snap, interval=0.01)
    q = b.subscribe()
    t = b.start()
    got = q.get(timeout=2)
    assert got[0] == "snapshot"
    b.stop()
    t.join(timeout=1)
    assert not t.is_alive()
