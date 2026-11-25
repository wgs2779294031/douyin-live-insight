# -*- coding: utf-8 -*-
"""探测异常不能被当成「已下播」（2026-11-01 审计 P0）。

原实现（watch_anchor_live.main 轮询循环）：

    try:
        rec = poll_once(...)
    except Exception as e:
        rec = {..., "error": str(e), "is_live": False}   # ← 兜底造出来的
    ...
    elif proc is not None and args.start:
        offline_streak += 1
        if offline_streak >= 2:
            break          # ← 走 finally，把采集进程 terminate 掉

于是在浏览器掉线 / 登录态被风控 / CDP 端口暂时没响应时，两轮（约 2 分钟）就把一个
**正在正常收弹幕**的采集器杀掉，整场数据断在这里；而 watch 子进程本身还活着、
看门狗看它活着判健康 —— 「进程活着 + 直播中 + 零数据」再来一遍。

判据只认「有效响应里明确未在播」：环境异常时探测结果无效，既不累加 offline_streak
也不动采集器。
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import watch_anchor_live as wal  # noqa: E402


# ============================================================ 一、纯函数判据
def _live():
    return {"ts": 1, "live_status": 1, "room_id_str": "7691567198174743346",
            "nick": "某主播", "web_rid": "719712320231", "room_status": 2,
            "is_live": True}


def _offline():
    return {"ts": 1, "live_status": 0, "room_id_str": "0", "nick": "某主播",
            "web_rid": None, "room_status": None, "is_live": False}


def _probe_error():
    return {"ts": 1, "error": "cdp eval 失败（浏览器没起 / 没有可定位的标签页？）",
            "is_live": False}


def test_probe_error_is_not_confirmed_offline():
    """探测抛异常 → 兜底 rec 是 is_live=False，但这不是「下播」。"""
    assert wal.is_confirmed_offline(_probe_error()) is False


def test_probe_error_stays_false_even_if_kind_passed_in():
    """调用方已经把 kind 算出来了（main 循环就是这样）：仍然不是下播。"""
    rec = _probe_error()
    assert wal.is_confirmed_offline(rec, "channel") is False
    assert wal.is_confirmed_offline(rec, "auth") is False


def test_auth_blocked_is_not_confirmed_offline():
    rec = {"ts": 1, "api_msg": "blocked", "live_status": None, "nick": None,
           "room_id_str": None, "web_rid": None, "is_live": False}
    assert wal.is_confirmed_offline(rec) is False


def test_empty_shell_profile_is_not_confirmed_offline():
    rec = {"ts": 1, "live_status": None, "nick": None, "room_id_str": None,
           "web_rid": None, "is_live": False}
    assert wal.is_confirmed_offline(rec) is False


def test_normal_offline_is_confirmed():
    """有效响应 + live_status=0 + 有昵称 → 这才是真下播。"""
    assert wal.is_confirmed_offline(_offline()) is True


def test_live_is_not_offline():
    assert wal.is_confirmed_offline(_live()) is False


# ============================================================ 二、主循环行为
class FakeProc:
    """采集子进程替身：poll() 恒返回 None（活着）。"""

    def __init__(self):
        self.pid = 424242
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def _run_main(monkeypatch, tmp_path, poll, rounds, *, messages=None, rows=None, started=None):
    """跑 main()，把一切外部动作替换成替身，返回 poll 被调用的次数。"""
    started = started or FakeProc()
    messages = [] if messages is None else messages
    rows = [] if rows is None else rows
    calls = {"n": 0}

    def fake_poll(*args, **kwargs):
        calls["n"] += 1
        return poll(calls["n"])

    monkeypatch.setattr(wal, "poll_once", fake_poll)
    monkeypatch.setattr(wal, "_append", lambda _path, row: rows.append(dict(row)))
    monkeypatch.setattr(wal.notify, "send_text",
                        lambda text, key, cooldown: messages.append((key, text)) or True)
    monkeypatch.setattr(wal, "heal_channel", lambda *a, **k: None)
    monkeypatch.setattr(wal.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(wal, "make_config", lambda *a, **k: tmp_path / "cfg.json")
    monkeypatch.setattr(wal, "start_collector", lambda *a, **k: started)
    monkeypatch.setattr(wal, "rebuild_dashboard", lambda *a, **k: True)
    monkeypatch.setattr(sys, "argv", ["watch_anchor_live.py", "--sec-uid", "x",
                                      "--tag", "t", "--start",
                                      "--interval", "0",
                                      "--max-rounds", str(rounds)])
    rc = wal.main()
    assert rc == 0
    return calls["n"], started


def test_probe_errors_do_not_end_the_session(monkeypatch, tmp_path):
    """1 轮开播 + 连续 3 轮探测异常 → 4 轮必须全部跑完，不许中途判定下播。"""

    def poll(n):
        if n == 1:
            return _live()
        raise RuntimeError("cdp eval 失败（浏览器没起 / 没有可定位的标签页？）")

    rounds, _ = _run_main(monkeypatch, tmp_path, poll, rounds=4)
    assert rounds == 4


def test_real_offline_still_stops_after_two_rounds(monkeypatch, tmp_path):
    """对照组：真下播（有效响应 + 未在播）仍然两轮就收工，别把修复做成永不停。"""

    def poll(n):
        return _live() if n == 1 else _offline()

    rounds, started = _run_main(monkeypatch, tmp_path, poll, rounds=6)
    assert rounds == 3          # 第 1 轮开播 + 第 2、3 轮未播 → 第 3 轮收工
    assert started.terminated is True   # 正常收尾仍然会终止采集器


def test_reused_fallback_room_never_ends_an_existing_collector(monkeypatch, tmp_path):
    # First obtain a valid live state, then repeatedly fail both probes.
    # All four rounds must run; an offline interpretation would break on round 3.
    monkeypatch.setattr(wal, "cached_web_rid", lambda tag: "123")
    monkeypatch.setattr(wal, "cdp_eval", lambda *args: '{"reason":"owner_unconfirmed"}')
    def poll(n):
        if n == 1:
            return _live()
        return {"live_status": 0, "is_live": False, "api_code": 403}
    rounds, _ = _run_main(monkeypatch, tmp_path, poll, rounds=4)
    assert rounds == 4


def test_probe_loss_and_return_are_local_but_live_and_offline_are_sent(monkeypatch, tmp_path, capsys):
    messages, rows = [], []
    def poll(n):
        if n in (1, 5):
            return _live()
        if n in (6, 7):
            return _offline()
        raise RuntimeError("blocked")
    rounds, proc = _run_main(monkeypatch, tmp_path, poll, 7, messages=messages, rows=rows)
    assert rounds == 7 and proc.terminated
    assert [r.get("probe_transition") for r in rows] == [
        None, None, None, "sustained_unknown", "confirmed_again", None, None]
    assert [k for k, _ in messages] == ["live-t", "offline-t"]
    assert "采集进程已启动" in messages[0][1] and "已确认下播" in messages[1][1]
    output = capsys.readouterr().out
    assert "未能确认主播开播状态" in output and "扫码" not in output


def test_collector_exit_and_fuse_still_notify(monkeypatch, tmp_path):
    messages = []
    proc = FakeProc()
    proc.returncode = 1
    _run_main(monkeypatch, tmp_path, lambda n: _live(), wal.COLLECTOR_FUSE + 1,
              messages=messages, started=proc)
    keys = [k for k, _ in messages]
    assert keys == ["live-t"] + ["collector-restart-t"]*(wal.COLLECTOR_FUSE-1) + ["collector-fuse-t"]
    assert all("准备第" in msg and "已自动重启" not in msg for _, msg in messages[1:-1])
    assert "暂停自动重启" in messages[-1][1]


def test_maintenance_resume_only_suppresses_same_live_room(monkeypatch, tmp_path):
    for resume, expected in ((_live()["room_id_str"], ["offline-t"]),
                             ("different-room", ["live-t", "offline-t"])):
        messages = []
        monkeypatch.setenv("WATCH_RESUME_ROOM_ID", resume)
        rounds, proc = _run_main(monkeypatch, tmp_path, lambda n: _live() if n == 1 else _offline(),
                                 3, messages=messages)
        assert rounds == 3 and proc.terminated
        assert [k for k, _ in messages] == expected
