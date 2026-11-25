# -*- coding: utf-8 -*-
"""stream_recorder 单测：FLV 分片器（tag 边界切片 / 头部重放 / 视频轨剔除 / 配额 / 编号接续）。

全部在 tmp_path 里跑，绝不触碰真实 data/（同 test_assist_board 纪律）。
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import stream_recorder as sr  # noqa: E402

FLV_HDR = b"FLV\x01\x05\x00\x00\x00\x09\x00\x00\x00\x00"


def _tag(ttype: int, data: bytes, ts: int = 0) -> bytes:
    size = len(data)
    return (bytes([ttype]) + size.to_bytes(3, "big")
            + (ts & 0xFFFFFF).to_bytes(3, "big") + bytes([(ts >> 24) & 0xFF])
            + b"\x00\x00\x00" + data + (11 + size).to_bytes(4, "big"))


def _script_tag() -> bytes:
    return _tag(sr.TAG_SCRIPT, b"\x02\x00\x0aonMetaData")


def _aac_seq_tag() -> bytes:
    return _tag(sr.TAG_AUDIO, b"\xaf\x00\x12\x10")


def _aac_raw(i: int) -> bytes:
    return _tag(sr.TAG_AUDIO, b"\xaf\x01" + bytes([i % 256]) * 10, ts=i * 20)


def _video_tag(i: int) -> bytes:
    return _tag(sr.TAG_VIDEO, b"\x17\x01" + bytes([i % 256]) * 20, ts=i * 20)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _mk(tmp_path, seg_seconds=5, quota_mb=10, keep_video=False):
    return sr.FlvSegWriter(tmp_path, seg_seconds=seg_seconds,
                           quota_bytes=int(quota_mb * 1048576),
                           keep_video=keep_video, log=lambda *_: None)


def test_split_by_tag_boundary_and_replay_init(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(sr, "now", clock)
    w = _mk(tmp_path, seg_seconds=5)
    stream = FLV_HDR + _script_tag() + _aac_seq_tag()
    for i in range(3):
        stream += _aac_raw(i)
    # 喂 2.5 秒的数据 → 还是第 1 片
    w.feed(stream)
    clock.t += 2.5
    for i in range(3, 6):
        w.feed(_aac_raw(i))
    assert not (tmp_path / "seg_0002.flv").exists()
    # 再推过 5 秒线 → 切第 2 片
    clock.t += 3.0
    w.feed(_aac_raw(6))
    w.feed(_aac_raw(7))
    w.close()

    assert (tmp_path / "seg_0001.flv").exists()
    assert (tmp_path / "seg_0002.flv").exists()
    seg2 = (tmp_path / "seg_0002.flv").read_bytes()
    # 第 2 片必须自带 FLV 头 + 元数据 + AAC 序列头（否则 ffmpeg 解不出）
    assert seg2.startswith(FLV_HDR)
    assert b"\x02\x00\x0aonMetaData" in seg2
    assert b"\xaf\x00\x12\x10" in seg2
    assert b"\xaf\x01" in seg2


def test_video_tags_stripped_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "now", Clock())
    w = _mk(tmp_path, seg_seconds=600)
    w.feed(FLV_HDR + _script_tag() + _aac_seq_tag() + _video_tag(0) + _aac_raw(0))
    w.close()
    data = (tmp_path / "seg_0001.flv").read_bytes()
    assert b"\x17\x01" not in data          # 视频 tag 被剔除
    assert b"\xaf\x01" in data              # 音频留下
    assert w.segments[0]["tags"] == 3       # 元数据 + AAC 序列头 + 1 个音频帧


def test_keep_video_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "now", Clock())
    w = _mk(tmp_path, seg_seconds=600, keep_video=True)
    w.feed(FLV_HDR + _script_tag() + _aac_seq_tag() + _video_tag(0) + _aac_raw(0))
    w.close()
    assert b"\x17\x01" in (tmp_path / "seg_0001.flv").read_bytes()


def test_quota_exceeded(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "now", Clock())
    w = sr.FlvSegWriter(tmp_path, seg_seconds=600, quota_bytes=50, log=lambda *_: None)
    try:
        w.feed(FLV_HDR + _script_tag() + _aac_seq_tag() + _aac_raw(0))
        raise AssertionError("应当抛 QuotaExceeded")
    except sr.QuotaExceeded:
        pass
    finally:
        w.close()


def test_segment_numbering_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "now", Clock())
    (tmp_path / "seg_0007.flv").write_bytes(b"x")
    w = _mk(tmp_path, seg_seconds=600)
    w.feed(FLV_HDR + _script_tag() + _aac_seq_tag() + _aac_raw(0))
    w.close()
    assert (tmp_path / "seg_0008.flv").exists()
    assert (tmp_path / "seg_0007.flv").read_bytes() == b"x"   # 不覆盖旧片


def test_truncated_tail_is_buffered(tmp_path, monkeypatch):
    """半截 tag 不能丢：留在缓冲区等下一批字节（文件可能已开，但那半截不能落盘）。"""
    monkeypatch.setattr(sr, "now", Clock())
    w = _mk(tmp_path, seg_seconds=600)
    stream = FLV_HDR + _script_tag() + _aac_seq_tag() + _aac_raw(0)
    w.feed(stream[:-6])
    size_before = (tmp_path / "seg_0001.flv").stat().st_size
    assert size_before < len(stream)      # 尾部 6 字节还没落
    w.feed(stream[-6:])
    w.close()
    assert (tmp_path / "seg_0001.flv").stat().st_size == len(stream)


def test_read_watch_state(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "DATA", tmp_path)
    p = tmp_path / "watch_测试主播.jsonl"
    p.write_text('{"ts":1,"is_live":true,"web_rid":"123"}\n', encoding="utf-8")
    st = sr.read_watch_state("测试主播")
    assert st and st["web_rid"] == "123"
    assert sr.read_watch_state("不存在") is None


def test_write_meta_merges_sessions(tmp_path):
    s1 = {"session_start_ts": 1, "stop_reason": "offline", "segments": [
        {"file": "seg_0001.flv", "bytes": 1048576}]}
    s2 = {"session_start_ts": 2, "stop_reason": "quota", "segments": [
        {"file": "seg_0002.flv", "bytes": 1048576}]}
    sr.write_meta(tmp_path, s1)
    sr.write_meta(tmp_path, s2)
    meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert [s["file"] for s in meta["segments"]] == ["seg_0001.flv", "seg_0002.flv"]
    assert len(meta["sessions"]) == 2
    assert meta["sessions"][1]["stop_reason"] == "quota"
    assert meta["total_mb_all"] == 2.0


def test_pid_alive_does_not_kill(tmp_path, monkeypatch):
    """⭐ 回归：判活不能用 os.kill(pid,0)（Windows 会 TerminateProcess）。"""
    import os
    assert sr.pid_alive(os.getpid()) is True
    assert sr.pid_alive(999999) is False


def test_pick_stream_url_prefers_lowest_quality():
    cap = {"flvPullUrl": {"FULL_HD1": "u-uhd", "HD1": "u-hd", "SD1": "u-sd1", "SD2": "u-sd2"},
           "playerFlvUrls": ["u-player"]}
    assert sr.pick_stream_url(cap) == ("u-sd1", "SD1")
    assert sr.pick_stream_url(cap, "HD1") == ("u-hd", "HD1")


def test_pick_stream_url_falls_back_to_player():
    assert sr.pick_stream_url({"playerFlvUrls": ["u-player"]}) == ("u-player", "player")
    assert sr.pick_stream_url({}) == (None, None)


def test_url_expire_ts():
    assert sr.url_expire_ts("http://x/a.flv?expire=1793007558&sign=abc") == 1793007558
    assert sr.url_expire_ts("http://x/a.flv?sign=abc&volcTime=1793007558") == 1793007558
    assert sr.url_expire_ts("http://x/a.flv?expire=123") is None      # 明显不是时间戳
    assert sr.url_expire_ts("http://x/a.flv") is None
    assert sr.url_expire_ts("") is None
