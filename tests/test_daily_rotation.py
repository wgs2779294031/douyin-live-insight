# -*- coding: utf-8 -*-
"""跨日轮转失败后不能静默停写（2026-11-01 审计 P0，三处同源）。

三处是同一个模式的三份拷贝：
    storage.JsonlStorage._ensure_file
    monitor._DateJsonl.write
    llm_service.DailyJsonlWriter._ensure_file

原实现的顺序是「先 close 旧句柄 → 改 _current_date → 再 open 新文件」。open 一旦
失败（磁盘满 / 目录被误删 / 权限变更 / 杀软锁文件），前两件事已经发生、新句柄
没有 —— 于是：

1. 下一次 write 走 `today == self._current_date` 直接返回，**永不重试轮转**；
2. 紧接着 `self._file.write(...)` 打在已关闭的句柄上抛 ValueError，被上层
   （Monitor 旁路 / LLM worker 的兜底 except）吞掉 → 该进程余下全部数据静默丢失，
   而落盘计数、日志、看板一切正常。

修法：先 open 成功，再替换 _file 与 _current_date；失败时旧句柄原封不动，
下一次 write 会自己重新尝试轮转（异常如实抛给调用方，不吞）。
"""
import builtins
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm_service import DailyJsonlWriter  # noqa: E402
from monitor import _DateJsonl  # noqa: E402
from storage import JsonlStorage  # noqa: E402


@pytest.fixture
def clock(monkeypatch):
    """接管 time.strftime 的 %Y-%m-%d —— 三处轮转判据都走这一条。"""
    real = time.strftime
    state = {"date": "2026-11-01"}

    def fake(fmt, *args):
        if fmt == "%Y-%m-%d":
            return state["date"]
        return real(fmt, *args)

    monkeypatch.setattr(time, "strftime", fake)
    return state


def _patch_open(monkeypatch, marker):
    """让「路径含 marker 的 open」抛 OSError（模拟磁盘满 / 权限变更）。

    返回开关 dict：``state["on"] = False`` 即恢复真实行为。
    """
    real_open = builtins.open
    state = {"on": True}

    def fake_open(file, *args, **kwargs):
        if state["on"] and marker in str(file):
            raise OSError(28, "No space left on device")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    return state


def _jsonl(path):
    assert path.exists(), "落盘文件不存在：%s" % path
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()
            if x.strip()]


# ============================================================ 一、跨日正常切分
def test_storage_rotates_on_new_day(tmp_path, clock):
    st = JsonlStorage(str(tmp_path))
    st.write({"n": 1})
    clock["date"] = "2026-11-02"
    st.write({"n": 2})
    st.close()

    assert _jsonl(tmp_path / "danmu_douyin_2026-11-01.jsonl") == [{"n": 1}]
    assert _jsonl(tmp_path / "danmu_douyin_2026-11-02.jsonl") == [{"n": 2}]


def test_monitor_rotates_on_new_day(tmp_path, clock):
    w = _DateJsonl(str(tmp_path), "stats_douyin_t")
    w.write({"n": 1})
    clock["date"] = "2026-11-02"
    w.write({"n": 2})
    w.close()

    assert _jsonl(tmp_path / "stats_douyin_t_2026-11-01.jsonl") == [{"n": 1}]
    assert _jsonl(tmp_path / "stats_douyin_t_2026-11-02.jsonl") == [{"n": 2}]


def test_llm_writer_rotates_on_new_day(tmp_path, clock):
    w = DailyJsonlWriter(str(tmp_path), "coldline")
    w.write({"n": 1})
    clock["date"] = "2026-11-02"
    w.write({"n": 2})
    w.close()

    assert _jsonl(tmp_path / "coldline_2026-11-01.jsonl") == [{"n": 1}]
    assert _jsonl(tmp_path / "coldline_2026-11-02.jsonl") == [{"n": 2}]


# ============================================================ 二、轮转失败可重试
def test_storage_rotation_failure_keeps_old_handle(tmp_path, clock, monkeypatch):
    st = JsonlStorage(str(tmp_path))
    st.write({"n": 1})
    old_handle, old_date = st._file, st._current_date
    assert old_date == "2026-11-01"

    clock["date"] = "2026-11-02"
    fail = _patch_open(monkeypatch, "2026-11-02")

    with pytest.raises(OSError):
        st.write({"n": 2})

    # 旧句柄既没被关（还能继续写），日期也没被提前改掉（否则永不重试）
    assert st._file is old_handle
    assert not old_handle.closed
    assert st._current_date == "2026-11-01"

    # 磁盘恢复后，下一条写入自己重试轮转，不需要重建对象
    fail["on"] = False
    st.write({"n": 3})
    st.close()

    assert _jsonl(tmp_path / "danmu_douyin_2026-11-01.jsonl") == [{"n": 1}]
    assert _jsonl(tmp_path / "danmu_douyin_2026-11-02.jsonl") == [{"n": 3}]


def test_monitor_rotation_failure_keeps_old_handle(tmp_path, clock, monkeypatch):
    w = _DateJsonl(str(tmp_path), "stats_douyin_t")
    w.write({"n": 1})
    old_handle, old_date = w._file, w._current_date

    clock["date"] = "2026-11-02"
    fail = _patch_open(monkeypatch, "2026-11-02")

    with pytest.raises(OSError):
        w.write({"n": 2})

    assert w._file is old_handle
    assert not old_handle.closed
    assert w._current_date == "2026-11-01"

    fail["on"] = False
    w.write({"n": 3})
    w.close()

    assert _jsonl(tmp_path / "stats_douyin_t_2026-11-01.jsonl") == [{"n": 1}]
    assert _jsonl(tmp_path / "stats_douyin_t_2026-11-02.jsonl") == [{"n": 3}]


def test_llm_writer_rotation_failure_keeps_old_handle(tmp_path, clock, monkeypatch):
    w = DailyJsonlWriter(str(tmp_path), "coldline")
    w.write({"n": 1})
    old_handle = w._file

    clock["date"] = "2026-11-02"
    fail = _patch_open(monkeypatch, "2026-11-02")

    with pytest.raises(OSError):
        w.write({"n": 2})

    assert w._file is old_handle
    assert not old_handle.closed
    assert w._current_date == "2026-11-01"

    fail["on"] = False
    w.write({"n": 3})
    w.close()

    assert _jsonl(tmp_path / "coldline_2026-11-01.jsonl") == [{"n": 1}]
    assert _jsonl(tmp_path / "coldline_2026-11-02.jsonl") == [{"n": 3}]
