#!/usr/bin/env python
"""直播间**音频**录流器 —— 主播开播自动落盘 flv 分片（主播原话工程化 · 第 1 步）。

为什么这么写（2026-10-16 / 10-19 实测结论，勿改成"页面内 fetch"）
------------------------------------------------------------------
1. 流地址：`enter_room` 响应里的 `stream_url.flv_pull_url`（uhd/hd/ld/sd 全清晰度，带
   expire+sign）**配合浏览器全量 cookie + curl_cffi impersonate=chrome 可直连 200**（10-19 实测四档全过）。
   播放器自己拉的 URL 只作兜底 —— 2026-10-19 实测有房间的播放器改走 WebRTC（拉 `*.sdp`），
   这时按 `.flv` 抓会一条都抓不到（研途-张老师团队就是这样）。
2. 拉流：douyin cookies 全量 + `curl_cffi` impersonate='chrome'。
3. FLV 分片：按 **FLV tag 边界**切（不是按字节瞎切），每片重放「文件头 + 元数据 + AAC 序列头」，
   否则中间段没有 AAC AudioSpecificConfig，ffmpeg 解不出来。
4. 只录音频 → 默认挑最低清（视频 tag 丢掉，音频码率四档都在 15~18KB/s，但总带宽差 5 倍）。

用法
----
    # 常驻蹲守（开播即录，下播停）—— 生产用法
    python scripts/stream_recorder.py --tag 框框看升学

    # 只看当前状态，不录
    python scripts/stream_recorder.py --tag 框框看升学 --once

    # 限时试录（验收用）
    python scripts/stream_recorder.py --tag 研途-张老师团队 --max-minutes 5

产物
----
    data/audio_<tag>_<YYYY-MM-DD>/seg_0001.flv ...   # 默认 60s 一片，音频轨 only
    data/audio_<tag>_<YYYY-MM-DD>/meta.json          # 每片的绝对起止时间（转写要用）
    data/stream_<tag>.log                            # 事件日志（验收只看它 + pidfile）
    data/stream_<tag>.pid                            # 防双开

纪律
----
- 单路单场配额默认 1GB（--quota-mb），超了停录。
- 每 30 分钟重抓 URL+cookie（签名/会话自愈）；403/断流退避重抓，连续 3 次失败退出。
- 只读公开直播间音频，内部竞品分析用途；不做任何账号动作。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.local_installation import bootstrap_entry, installed_layout
bootstrap_entry()

from watch_anchor_live import classify_env_failure, is_confirmed_offline
from file_lock import FileLock, file_lock
from scripts import audio_segments
from scripts.local_context import (activate_customer_environment, browser_owner_valid, customer_stop_requested,
    load_customer_context, publish_role_ready, validate_role_tag)

ROOT = Path(__file__).resolve().parents[1]
CUSTOMER = load_customer_context()
activate_customer_environment(CUSTOMER)
DATA = Path(CUSTOMER["data"]) if CUSTOMER else ROOT / "data"
_CUSTOMER_IDENTITY = None
_TRANSCRIBE_CHILDREN = []

# 浏览器调试端口（与 run_watchdog.DEBUG_PORT / capture_stream.mjs 的 CDP_PORT 对齐）
CDP_PORT = int(os.environ.get("WATCH_DEBUG_PORT") or os.environ.get("CDP_PORT") or 9222)
CAPTURE_MJS = ROOT / "scripts" / "capture_stream.mjs"

FLV_HEADER = b"FLV\x01\x05\x00\x00\x00\x09" + b"\x00\x00\x00\x00"  # flags=audio+video
TAG_AUDIO, TAG_VIDEO, TAG_SCRIPT = 8, 9, 18

# 录流器本体是无窗（pythonw）跑的，但它 spawn 的 node / ffmpeg 是控制台程序：
# 不显式加这个标志，Windows 就给每个子进程新开一个黑框——闪一下又自动关（2026-10-19 老大反馈）。
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ------------------------------------------------------------------ 工具
def now() -> float:
    return time.time()


def fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


# 本机遗留的 node 位置（移植时靠 WATCH_NODE 或 PATH 覆盖，见 resolve_node）
_NODE_FALLBACKS = []


def resolve_node() -> str:
    """定位 node：WATCH_NODE → 本机遗留位置 → PATH（shutil.which）→ 裸名兜底。

    ★ 2026-11-01 审计 P0：原来在候选列表里直接写
      ``os.popen("where node").read().strip().splitlines()[0]``。列表元素是**先全部
      求值再赋值**的，所以 `where node` 一旦没有输出（PATH 里没有 node / nvm 未激活 /
      PATH 被 launcher 截断），`splitlines()[0]` 直接 IndexError，`resolve_node()`
      整个炸在录制启动路径上 —— 一台没配 node 的机器连「找不到 node」都报不出来。
      改用 shutil.which：无输出就是 None，天然可判空，也不再拉一个 cmd 子进程。
    """
    installed = installed_layout()
    if installed is not None:
        selected = installed["node"]
        if not selected.is_file():
            raise RuntimeError("客户安装缺少固定的 Node.js")
        return str(selected)
    # ★ 逐个按顺序探测，**不要**把 which 写进列表字面量：列表元素先全部求值，
    #   那样即使 WATCH_NODE 已经命中也会白探一次 PATH（原 bug 的同款形态）。
    for c in (os.environ.get("WATCH_NODE"), *([] if CUSTOMER else _NODE_FALLBACKS)):
        if c and Path(c).exists():
            return c
    which_node = shutil.which("node")
    if which_node and Path(which_node).exists():
        return which_node
    return "node"


class Log:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, msg: str) -> None:
        line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        try:
            print(line, flush=True)
        except Exception:
            pass


def pid_alive(pid: int) -> bool:
    """⚠️ 绝不能用 os.kill(pid, 0) 判活：Windows 上 os.kill 对 0 号信号会直接
    TerminateProcess，把正在跑的实例杀掉（本项目踩过）。"""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False
    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


# ------------------------------------------------------------------ 开播状态
def read_watch_state(tag: str) -> Optional[Dict[str, Any]]:
    """读 data/watch_<tag>.jsonl 最后一行（watch_anchor_live.py 每轮一行）。"""
    p = DATA / ("watch_%s.jsonl" % tag)
    if not p.exists():
        return None
    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail = f.read().decode("utf-8", "replace").strip().splitlines()
        for line in reversed(tail):
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except Exception:
                continue
    except Exception:
        return None
    return None


class SessionQuota:
    """跨重试/进程重启保留同一场配额；只有可信的新场次才清零。"""

    def __init__(self, path: Path, limit: int):
        self.path, self.limit = path, limit
        try:
            self.state = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(self.state, dict) or not isinstance(self.state.get("bytes_used", 0), int):
                raise ValueError("invalid quota state")
        except FileNotFoundError:
            self.state = {}
        except (OSError, ValueError) as exc:
            raise RuntimeError("录音配额状态无法读取，拒绝重置配额：%s" % path.name) from exc

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False,
                                             dir=self.path.parent, prefix=self.path.name + ".",
                                             suffix=".tmp") as fh:
                name = fh.name
                json.dump(self.state, fh, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(name, self.path)
        finally:
            if name and os.path.exists(name):
                os.unlink(name)

    def observe(self, st: dict) -> bool:
        if classify_env_failure(st) is not None:
            return False
        if is_confirmed_offline(st):
            if self.state and not self.state.get("offline_seen"):
                self.state["offline_seen"] = True
                self._save()
            return False
        if not st.get("is_live") or not st.get("web_rid"):
            return False
        sid = str(st.get("room_id_str") or st.get("room_id") or "")
        old_sid = str(self.state.get("session_id") or "")
        new_session = (not self.state or self.state.get("offline_seen")
                       or (sid and old_sid and sid != old_sid)
                       or str(st["web_rid"]) != self.state.get("web_rid"))
        if new_session:
            self.state = {"session_id": sid, "web_rid": str(st["web_rid"]),
                          "bytes_used": 0, "exhausted": False, "offline_seen": False,
                          "live_since": now(), "phase": "starting"}
            self._save()
        elif sid and not old_sid:
            self.state["session_id"] = sid
            self._save()
        return not self.state.get("exhausted") and self.remaining > 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - max(0, int(self.state.get("bytes_used", 0))))

    def finish(self, added_bytes: int, reason: str):
        self.state["bytes_used"] = max(0, int(self.state.get("bytes_used", 0))) + added_bytes
        self.state["exhausted"] = reason == "quota" or self.remaining == 0
        self.state["phase"] = "quota" if self.state["exhausted"] else reason
        if reason == "offline":
            self.state["offline_seen"] = True
        self._save()  # 保存失败必须退出，不能装作配额已持久化后继续重试。


# ------------------------------------------------------------------ 抓流地址
def _close_tab_for(web_rid: str) -> None:
    """关掉这个直播间残留的标签页（2026-10-27）。

    capture_stream.mjs 带 --close-tab 会自己关，但超时被 kill 时走不到那一步；
    这里按 url 兜底扫一遍，避免一个没关掉的 tab 永久占着一个 renderer 进程。
    """
    if CUSTOMER:
        return  # URL guesses do not establish ownership of a customer's tab.
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/json/list" % CDP_PORT, timeout=3) as r:
            targets = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return
    for t in targets:
        if t.get("type") != "page":
            continue
        if str(web_rid) not in (t.get("url") or ""):
            continue
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:%d/json/close/%s" % (CDP_PORT, t.get("id")),
                method="PUT")
            urllib.request.urlopen(req, timeout=3).read()
        except Exception:
            pass


def _page_ids() -> set:
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/json/list" % CDP_PORT, timeout=3) as r:
            ts = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return set()
    return {t.get("id") for t in ts if t.get("type") == "page"}


def _close_orphan_pages(before: set) -> int:
    """关掉这次抓流新产生、但已经不在直播间的页面（2026-10-27）。

    `_close_tab_for` 按 web_rid 匹配，可直播间一结束/被风控，reload 会把页面
    跳到 www.douyin.com/jingxuan —— 那时 URL 里已经没有 web_rid，扫不到。
    所以再做一次「前后快照 diff」：本次新冒出来、URL 又不是直播间的页面，
    一定是这次抓流留下的孤儿，关掉绝不会碰到别的路正在用的 tab。
    """
    if CUSTOMER or not before:
        return 0
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/json/list" % CDP_PORT, timeout=3) as r:
            ts = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return 0
    killed = 0
    for t in ts:
        if t.get("type") != "page" or t.get("id") in before:
            continue
        u = t.get("url") or ""
        if "douyin.com" not in u or "live.douyin.com" in u:
            continue          # 只清理「跳到普通抖音页」的那种残留
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:%d/json/close/%s" % (CDP_PORT, t.get("id")),
                method="PUT")
            urllib.request.urlopen(req, timeout=3).read()
            killed += 1
        except Exception:
            pass
    return killed


def capture_stream(web_rid: str, out_json: Path, node: str, wait_ms: int = 20000) -> Optional[Dict[str, Any]]:
    if CUSTOMER:
        if (customer_stop_requested(CUSTOMER, _CUSTOMER_IDENTITY)
                or not browser_owner_valid(CUSTOMER)):
            return None
        out_json.parent.mkdir(parents=True, exist_ok=True)
        return _capture_stream_inner(web_rid, out_json, node, wait_ms)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    before = _page_ids()
    try:
        return _capture_stream_inner(web_rid, out_json, node, wait_ms)
    finally:
        # 不管抓成没过都扫一遍：mjs 自己会关，但它异常退出（reload 超时之类）时
        # 走不到收尾那行，tab 就留在浏览器里了。这里按 url + 快照 diff 双保险。
        _close_tab_for(web_rid)
        _close_orphan_pages(before)


def _capture_stream_inner(web_rid: str, out_json: Path, node: str,
                          wait_ms: int) -> Optional[Dict[str, Any]]:
    try:
        cp = subprocess.run(
            # --close-tab 实际已不是必需（mjs 现在默认就关），留着兼容旧口径。
            # 以前不传这个参数 → 每抓一次在浏览器里留一个 renderer，八路各
            # 30 分钟一次，一天能堆几十个，直到端口探不通被判卡死重启。
            [node, str(CAPTURE_MJS), "--web-rid", str(web_rid), "--out", str(out_json),
             "--wait", str(wait_ms), "--close-tab"],
            cwd=str(ROOT), capture_output=True, timeout=90,
            encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return None
    if not out_json.exists():
        return None
    try:
        payload = json.loads(out_json.read_text(encoding="utf-8"))
    except Exception:
        return None
    if cp.returncode != 0 or not (payload.get("flvPullUrl") or payload.get("playerFlvUrls")):
        return None
    return payload


def _windows_curl_ca_path(path: str) -> bytes:
    # The bundled PEM's relative path omits the Unicode part of the install root.
    # Curl opens narrow paths with the Windows ANSI code page, even in Python UTF-8 mode.
    try:
        path = os.path.relpath(path)
    except ValueError:
        pass  # Preserve a configured CA on a different drive.
    encoded = path.encode("mbcs")
    if encoded.decode("mbcs") != path:
        raise UnicodeError("CA path is not representable in the Windows ANSI code page")
    return encoded


def build_session(cap: Dict[str, Any]):
    """把 capture 字典变成 curl_cffi Session（cookies 按 douyin 域注入）。"""
    from curl_cffi import requests as creq
    s = creq.Session(impersonate="chrome")
    if sys.platform == "win32" and installed_layout() is not None:
        from curl_cffi import CurlOpt
        from curl_cffi.curl import DEFAULT_CACERT

        # Keep the same trust bundle selected by curl_cffi before supplying its path as bytes.
        ca_file = s.verify if isinstance(s.verify, str) else DEFAULT_CACERT
        try:
            ca_path = _windows_curl_ca_path(ca_file)
        except UnicodeError:
            s.close()
            raise RuntimeError("recorder_ca_path_unrepresentable") from None
        s.verify = True
        s.curl_options.update({CurlOpt.CAINFO: ca_path, CurlOpt.PROXY_CAINFO: ca_path})
    jar = {c["name"]: c["value"] for c in cap.get("cookies", []) if c.get("name")}
    for k, v in jar.items():
        try:
            s.cookies.set(k, v, domain=".douyin.com")
        except Exception:
            pass
    if jar.get("ttwid"):
        try:
            s.cookies.set("ttwid", jar["ttwid"], domain=".douyincdn.com")
        except Exception:
            pass
    # Direct media requests must use real addresses, rather than a proxy's
    # fake-IP DNS answers that cannot be reached through the normal route.
    from stream_network import wrap_session
    return wrap_session(s)


# 只录音频 → 越低清越省带宽（实测音频码率四档都在 15~18KB/s，视频差 5 倍）
QUALITY_ORDER = ["SD1", "SD2", "LD1", "HD1", "FULL_HD1"]


def pick_stream_url(cap: Dict[str, Any], prefer: str = "auto"):
    """优先 enter_room 的 flv_pull_url（带签名，实测可直连）；兜底播放器 flv URL。"""
    flv = cap.get("flvPullUrl") or {}
    if prefer and prefer != "auto":
        for k in (prefer, prefer.upper()):
            if k in flv:
                return flv[k], k
    for k in QUALITY_ORDER:
        if k in flv:
            return flv[k], k
    if flv:
        k = sorted(flv)[0]
        return flv[k], k
    urls = cap.get("playerFlvUrls") or []
    if urls:
        return sorted(urls, key=len)[0], "player"
    return None, None


def url_expire_ts(u: str) -> Optional[int]:
    """从拉流地址里抠出签名到期时间（秒）。抠不到返回 None。

    enter_room 给的四档地址都带 expire（2026-10-19 实测：剩余约 7 天），
    播放器 URL 带 expire/volcTime。有了它就不用"每 30 分钟盲重抓"——
    盲重抓要 reload 直播间页面，是风控最敏感的动作。
    """
    if not u:
        return None
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
    except Exception:
        return None
    for key in ("expire", "volcTime", "exp_hr"):
        v = q.get(key)
        if v:
            try:
                n = int(v[0])
            except Exception:
                continue
            if n > 1_000_000_000:
                return n
    return None


# ------------------------------------------------------------------ FLV 分片器
class FlvParseError(ValueError):
    """Safe fixed reason only; never includes stream bytes or signed URLs."""


class ProgressWriteError(OSError):
    """An observability write failed after the actual audio was flushed."""


class RecordingProgress:
    """Private, atomic evidence of flushed AAC packets, owned by one process/session."""

    INTERVAL_SEC = 5.0

    def __init__(self, path: Path, *, session_id: str, web_rid: str,
                 process_created_at: Optional[float] = None):
        self.path = path
        self.state = {
            "schema": 1, "implementation": "flv_connection_v2", "pid": os.getpid(),
            "process_created_at": (process_created_at if process_created_at is not None
                                   else _process_created_at(os.getpid())),
            "session_id": session_id, "web_rid": web_rid,
            "updated_at": 0.0, "phase": "connecting", "connection_id": 0,
            "last_valid_audio_at": None, "valid_audio_packets": 0,
            "valid_audio_bytes": 0, "parser_error": None,
        }
        self._saved_at = None

    def publish(self, writer=None, *, phase=None, force=False):
        stamp = now()
        if not force and self._saved_at is not None and stamp - self._saved_at < self.INTERVAL_SEC:
            return
        state = dict(self.state)
        if writer is not None:
            # No sidecar may claim unflushed buffered output is already available.
            if writer.fh is not None:
                writer.fh.flush()
            state.update(connection_id=writer.connection_id,
                         last_valid_audio_at=writer.last_valid_audio_at,
                         valid_audio_packets=writer.valid_audio_packets,
                         valid_audio_bytes=writer.valid_audio_bytes,
                         parser_error=writer.parser_error)
        if phase is not None:
            state["phase"] = phase
        state["updated_at"] = stamp
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            raise ProgressWriteError("recording_progress_write_failed") from exc
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)
        self.state, self._saved_at = state, stamp


def stop_requested(tag: str, process_created_at: Optional[float]) -> bool:
    """Cooperative maintenance stop; a stale/reused PID request cannot stop us."""
    if process_created_at is None:
        return False
    try:
        with (DATA / ("stream_%s.stop.json" % tag)).open("rb") as fh:
            raw = fh.read(4097)
        if len(raw) > 4096:
            return False
        request = json.loads(raw)
        created = request.get("process_created_at")
        return (request.get("schema") == 1 and request.get("action") == "stop"
                and type(request.get("pid")) is int and request["pid"] == os.getpid()
                and type(created) in (int, float) and math.isfinite(created)
                and abs(created - process_created_at) < 0.000001)
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def interruptible_wait(seconds: float, should_stop) -> bool:
    """Return true on stop; polling does not require signals to interrupt sleep."""
    remaining = max(0.0, seconds)
    while remaining > 0:
        if should_stop():
            return True
        step = min(0.5, remaining)
        time.sleep(step)
        remaining -= step
    return should_stop()


class FlvSegWriter:
    """按 FLV tag 边界切片；每片重放 文件头 + 元数据 + AAC 序列头。

    - 默认**丢弃视频 tag**（只要音频，磁盘省 3~4 倍；--keep-video 可保留）
    - 片长按**墙钟**算（不是 tag 时间戳：中途进流时 tag ts 不从 0 开始）
    """

    def __init__(self, out_dir: Path, seg_seconds: int = 60, quota_bytes: int = 1024 ** 3,
                 keep_video: bool = False, log: Optional[Log] = None):
        self.dir = out_dir
        # Check the existing physical data parent before creating any directory.
        # mkdir(parents=True) could otherwise follow a redirected data ancestor.
        audio_segments.data_scope(self.dir.parent)
        self.dir.mkdir(exist_ok=True)
        audio_segments.safe_folder(self.dir.parent, self.dir)
        self.seg_seconds = max(5, seg_seconds)
        self.quota_bytes = quota_bytes
        self.keep_video = keep_video
        self.log = log or (lambda *_: None)
        self.buf = bytearray()
        self.header_seen = False
        self.init_tags: List[bytes] = []
        self.fh = None
        self.seg_index = 0  # Allocation consults the durable counter at creation.
        self.seg_start_wall: Optional[float] = None
        self.seg_start_tagms: Optional[int] = None
        self.seg_bytes = 0
        self.total_bytes = 0
        self.segments: List[Dict[str, Any]] = []
        self._tags_in_seg = 0
        self._script_init = None
        self._aac_init = None
        self._aac_config = None
        self.connection_id = 0
        self.connection_active = False
        self.connection_started_at = None
        self.connection_audio_at = None
        self.last_valid_audio_at = None
        self.valid_audio_packets = 0
        self.valid_audio_bytes = 0
        self.parser_error = None
        self.progress = None
        self.stop_check = None
        self.closure_errors = []

    # These caps reject a bogus multi-megabyte AUDIO length before buffering its body.
    MAX_TAG_BYTES = {TAG_AUDIO: 8192, TAG_SCRIPT: 64 * 1024, TAG_VIDEO: 8 * 1024 * 1024}
    MAX_BUFFER_BYTES = max(MAX_TAG_BYTES.values()) + 15

    def begin_connection(self) -> None:
        """Start a fresh HTTP byte stream; never feed a second FLV header as a tag.

        Close the old segment and discard its partial tag and initialization. New
        connections must provide their own file header and AAC configuration.
        Within this connection, rotations replay only the current init tags.
        """
        self._close_seg()
        self.buf.clear()
        self.header_seen = False
        self.init_tags = []
        self._script_init = self._aac_init = self._aac_config = None
        self.connection_id += 1
        self.connection_active = True
        self.connection_started_at = now()
        self.connection_audio_at = None
        self._publish(phase="connecting", force=True)

    def end_connection(self, phase="reconnecting") -> None:
        self._close_seg()
        self.buf.clear()
        self.header_seen = False
        self.connection_active = False
        self._publish(phase=phase, force=True)

    def _publish(self, *, phase=None, force=False):
        if self.progress is not None:
            if phase is not None:
                self._progress_phase = phase
            phase = getattr(self, "_progress_phase", None)
            failed_at = getattr(self, "_progress_failed_at", None)
            if not force and failed_at is not None and now() - failed_at < self.progress.INTERVAL_SEC:
                return
            try:
                self.progress.publish(self, phase=phase, force=force)
                self._progress_failed_at = None
            except ProgressWriteError:
                # A sidecar outage must not skip the caller's quota/meta finalizer.
                # Actual audio flush/write errors are deliberately not caught here.
                self._progress_failed_at = now()
                self.log("⚠️ 录音进度状态写入失败，保留音频并继续正常结算")

    def _invalid(self, reason):
        self.parser_error = reason
        self.buf.clear()
        self._close_seg()
        self.connection_active = False
        self._publish(phase="error", force=True)
        raise FlvParseError(reason)

    # -- 内部 --------------------------------------------------------
    def _rotate(self) -> None:
        self._close_seg()
        self.seg_index, path = audio_segments.reserve_segment(self.dir.parent, self.dir)
        name = path.name
        self.fh = open(path, "xb")
        self.seg_start_wall = now()
        self._cur_name = name
        self.fh.write(FLV_HEADER)
        for t in self.init_tags:
            self.fh.write(t)
        self.seg_bytes = len(FLV_HEADER) + sum(len(t) for t in self.init_tags)
        self.total_bytes += self.seg_bytes
        self._tags_in_seg = 0

    def _close_seg(self) -> None:
        if self.fh is None:
            return
        handle, self.fh = self.fh, None
        try:
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
        except Exception:
            handle.close()
            raise
        end_ts = now()
        segment = {
            "file": self._cur_name,
            "start_ts": round(self.seg_start_wall, 3),
            "start_time": fmt_ts(self.seg_start_wall),
            "end_ts": round(end_ts, 3),
            "bytes": self.seg_bytes,
            "tags": self._tags_in_seg,
        }
        self.segments.append(segment)
        try:
            audio_segments.publish_closed(self.dir.parent, self.dir, self.dir / self._cur_name,
                self.seg_index, self.seg_start_wall, end_ts, tags=self._tags_in_seg,
                connection_id=self.connection_id)
            segment["closure_state"] = "closed"
        except (OSError, ValueError) as error:
            # Do not skip quota/meta/finalization if only the closure sidecar failed.
            # Absence of a verified closure keeps this audio unconfirmed for ASR.
            reason = getattr(error, "reason", type(error).__name__)
            segment.update(closure_state="source_unconfirmed", closure_reason=reason)
            self.closure_errors.append({"file": self._cur_name, "reason": reason})
            self.log("⚠️ 分片闭合记录发布失败，音频保留且来源未确认：%s (%s)" % (self._cur_name, reason))

    def _need_rotate(self) -> bool:
        if self.fh is None:
            return True
        return (now() - (self.seg_start_wall or now())) >= self.seg_seconds

    def _remember_init(self, raw: bytes, ttype: int, data: bytes) -> None:
        if ttype == TAG_SCRIPT and self._valid_metadata(data):
            self._script_init = raw
        elif ttype == TAG_AUDIO and data[1] == 0:
            self._aac_init, self._aac_config = raw, data[2:]
        self.init_tags = [t for t in (self._script_init, self._aac_init) if t is not None]

    @staticmethod
    def _valid_metadata(data: bytes) -> bool:
        # Only the bounded metadata event is initialization, not arbitrary script
        # payloads accidentally found inside a damaged byte stream.
        def amf_string(offset):
            if len(data) < offset + 3 or data[offset] != 2:
                return None, offset
            length = int.from_bytes(data[offset + 1:offset + 3], "big")
            end = offset + 3 + length
            if not 1 <= length <= 128 or end > len(data):
                return None, offset
            return data[offset + 3:end], end
        name, end = amf_string(0)
        if name == b"@setDataFrame":
            name, end = amf_string(end)
        return name == b"onMetaData"

    @staticmethod
    def _valid_aac_config(config: bytes) -> bool:
        if not 2 <= len(config) <= 64:
            return False
        object_type = config[0] >> 3
        frequency_index = ((config[0] & 7) << 1) | (config[1] >> 7)
        channel_config = (config[1] >> 3) & 15
        # Reject reserved values; retain ordinary AAC LC, HE and other defined profiles.
        if object_type not in (1, 2, 3, 4, 5, 6, 7, 17, 19, 20, 21, 22, 23, 29):
            return False
        if frequency_index in (13, 14) or channel_config > 7:
            return False
        if frequency_index == 15:
            if len(config) < 5:
                return False
            explicit_rate = (int.from_bytes(config[:5], "big") >> 7) & 0xFFFFFF
            if not 1 <= explicit_rate <= 384000:
                return False
        return True

    # -- 对外 --------------------------------------------------------
    def feed(self, chunk: bytes) -> None:
        # Preserve the one-stream helper API, but all subsequent streams require
        # explicit begin_connection(), including after an error or close.
        if self.connection_id == 0:
            self.begin_connection()
        if not self.connection_active:
            raise FlvParseError("connection_not_started")
        self.buf += chunk
        if len(self.buf) > self.MAX_BUFFER_BYTES:
            self._invalid("buffer_limit")
        if not self.header_seen:
            if len(self.buf) < 9:
                return
            if self.buf[:3] != b"FLV" or self.buf[3] != 1 or self.buf[4] & ~5:
                self._invalid("invalid_flv_header")
            hdr_len = int.from_bytes(self.buf[5:9], "big")
            if not 9 <= hdr_len <= 4096:
                self._invalid("invalid_header_length")
            if len(self.buf) < hdr_len + 4:
                return
            if self.buf[hdr_len:hdr_len + 4] != b"\0\0\0\0":
                self._invalid("invalid_first_previous_size")
            del self.buf[:hdr_len + 4]
            self.header_seen = True
        while True:
            if len(self.buf) < 11:
                self._publish()
                return
            ttype = self.buf[0]
            if ttype not in (TAG_AUDIO, TAG_VIDEO, TAG_SCRIPT):
                self._invalid("invalid_tag_type")
            size = int.from_bytes(self.buf[1:4], "big")
            if not 1 <= size <= self.MAX_TAG_BYTES[ttype]:
                self._invalid("invalid_tag_size")
            if self.buf[8:11] != b"\0\0\0":
                self._invalid("invalid_stream_id")
            tagms = int.from_bytes(self.buf[4:7], "big") | (self.buf[7] << 24)
            total = 11 + size + 4
            if len(self.buf) < total:
                self._publish()
                return
            if int.from_bytes(self.buf[total - 4:total], "big") != 11 + size:
                self._invalid("invalid_previous_tag_size")
            raw = bytes(self.buf[:total])
            data = raw[11:11 + size]
            del self.buf[:total]
            is_audio = False
            if ttype == TAG_AUDIO:
                if len(data) < 3 or data[0] >> 4 != 10 or data[1] not in (0, 1):
                    self._invalid("invalid_aac_tag")
                if data[1] == 0:
                    if not self._valid_aac_config(data[2:]):
                        self._invalid("invalid_aac_config")
                    if data[2:] == self._aac_config:
                        # No need to append identical initialization, even when
                        # timestamps differ. Rotation replays the cached one once.
                        continue
                    if self._aac_config is not None and data[2:] != self._aac_config:
                        # A configuration change starts a new independently decodable
                        # segment; never replay the obsolete sequence header into it.
                        self._close_seg()
                        self._aac_init = self._aac_config = None
                        self.init_tags = [self._script_init] if self._script_init else []
                else:
                    if self._aac_config is None:
                        self._invalid("aac_config_missing")
                    is_audio = True
            if ttype != TAG_VIDEO or self.keep_video:
                # 先决定是否换片（换片会重放 init_tags），再登记 init tag —— 顺序不能反，
                # 否则首片会把本条 tag 既当 init 重放一遍、又被写一遍（重复 28 字节）。
                if self._need_rotate():
                    self._rotate()
                    self.seg_start_tagms = tagms
                self._remember_init(raw, ttype, data)
                self.fh.write(raw)
                self.seg_bytes += len(raw)
                self.total_bytes += len(raw)
                self._tags_in_seg += 1
                if is_audio:
                    self.last_valid_audio_at = self.connection_audio_at = now()
                    self.valid_audio_packets += 1
                    self.valid_audio_bytes += len(data) - 2
                    self.parser_error = None
                    self._publish(phase="recording")
            if self.total_bytes >= self.quota_bytes:
                raise QuotaExceeded()

    def close(self) -> None:
        self.end_connection("stopped")


class QuotaExceeded(Exception):
    pass


def write_meta(out_dir: Path, session_meta: Dict[str, Any]) -> None:
    """meta.json 按天累积（同日多场不能互相覆盖）。"""
    mp = out_dir / "meta.json"
    with file_lock(mp):
        try:
            old = json.loads(mp.read_text(encoding="utf-8"))
        except FileNotFoundError:
            old = {}
        except (OSError, ValueError) as exc:
            raise RuntimeError("录音 meta.json 无法读取，保留原文件并拒绝覆盖") from exc
        if not isinstance(old, dict) or not isinstance(old.get("segments", []), list) \
                or not isinstance(old.get("sessions", []), list) \
                or any(not isinstance(s, dict) or not s.get("file")
                       for s in old.get("segments", [])):
            raise RuntimeError("录音 meta.json 格式损坏，保留原文件并拒绝覆盖")
        segs = {s["file"]: s for s in old.get("segments", [])}
        for s in session_meta.get("segments", []):
            segs[s["file"]] = s
        sessions = list(old.get("sessions", []))
        sessions.append({k: session_meta.get(k) for k in
                         ("session_start", "session_start_ts", "session_end",
                          "stop_reason", "web_rid", "session_mb")})
        merged = dict(session_meta)
        merged["segments"] = sorted(segs.values(), key=lambda x: x.get("file", ""))
        merged["sessions"] = sessions
        merged["total_mb_all"] = round(sum(float(s.get("bytes", 0)) for s in segs.values()) / 1048576, 2)
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(prefix="meta.json.", suffix=".tmp", dir=out_dir)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(merged, fh, ensure_ascii=False, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, mp)
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass


def spawn_transcribe(tag: str, day: str, log: Log) -> None:
    """下播即转：起一个后台转写子进程，不阻塞守护循环。

    - 只转这一 tag 这一天，且 transcribe_backfill 本身幂等（已转的分片跳过）
    - 用 lock 文件防重入：下播即转 和 02:30 定时任务 撞上时不会两个进程写同一个 jsonl
    - 分片在 write_meta 之前已经 close()，所以此刻转到的都是完整片
    """
    if CUSTOMER:
        # The application-owned singleton discovers durable closed facts even
        # without another offline event. Never bypass its admission or stop gate.
        log("闭合分片已保留，由应用受管转写补扫；录音进程不另启ASR")
        return
    lock = DATA / (".tr_%s_%s.lock" % (tag, day))
    if lock.exists():
        try:
            if pid_owns_file(int(lock.read_text().strip()), lock, require_identity=True):
                log("⏭ 该 tag 今天已有转写进程在跑，跳过")
                return
        except Exception:
            pass
    script = ROOT / "scripts" / "transcribe_backfill.py"
    if not script.exists():
        log("⚠️ 找不到 transcribe_backfill.py，跳过转写")
        return
    try:
        out = open(DATA / ("transcribe_%s.log" % tag), "a", encoding="utf-8")
        # ★ 必须显式给 utf-8：父进程是无窗口的 pythonw，环境里没有 PYTHONIOENCODING，
        #   子进程会按 locale(cp936) 写 stdout → 打印 `▶` 时 UnicodeEncodeError，
        #   一场直播的转写就这么静默失败（2026-10-26 实测：一天 5 场 643MB 全没转）。
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        if CUSTOMER:
            env.pop("DY_CUSTOMER_STOP", None)
            env.pop("DY_CUSTOMER_ROLE_NONCE", None)
        proc = subprocess.Popen(
            [CUSTOMER["python"] if CUSTOMER else sys.executable, str(script), "--tag", tag, "--date", day],
            cwd=str(ROOT), stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=NO_WINDOW, env=env,
        )
        out.close()  # Popen owns its copied handle; the recorder need not retain it.
        if CUSTOMER:
            _TRANSCRIBE_CHILDREN[:] = [child for child in _TRANSCRIBE_CHILDREN if child.poll() is None]
            _TRANSCRIBE_CHILDREN.append(proc)
        lock.write_text(str(proc.pid), encoding="utf-8")
        log("📝 已触发转写 pid=%d → data/speech_%s_%s.jsonl（日志 data/transcribe_%s.log）"
            % (proc.pid, tag, day, tag))
    except Exception as e:
        log("⚠️ 触发转写失败：%s" % str(e)[:120])


# ------------------------------------------------------------------ 拉流
def pull_loop(tag: str, web_rid: str, writer: FlvSegWriter, log: Log,
              refresh_min: int, poll_sec: int, max_minutes: float,
              node: str, cap: Dict[str, Any], quit_flag: List[bool],
              quality: str = "auto", refresh_margin_sec: int = 900) -> str:
    """Own the current HTTP session through replacements and every exit path."""
    current = [None]

    def close_session(session):
        close = getattr(session, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                log("⚠️ 录音网络会话清理失败")

    def new_session(captured):
        replacement = build_session(captured)
        previous, current[0] = current[0], replacement
        close_session(previous)
        return replacement

    try:
        return _pull_loop_impl(tag, web_rid, writer, log, refresh_min, poll_sec,
                               max_minutes, node, cap, quit_flag, quality,
                               refresh_margin_sec, new_session)
    finally:
        close_session(current[0])


_STREAM_REQUEST_REASONS = frozenset({
    "invalid_stream_host", "invalid_stream_url", "noncanonical_stream_ip",
    "stream_connect_failed", "stream_ip_not_public", "stream_redirect_limit",
    "stream_redirect_location_missing", "stream_redirect_loop",
    "stream_response_close_failed", "stream_session_closed",
    "uplink_dns_no_public_address", "uplink_dns_reply_invalid", "uplink_dns_resolution_failed",
})


def _http_status(value):
    return value if type(value) is int and 100 <= value <= 599 else None


def _request_error_details(error, http_status=None):
    from stream_network import DNS_PROVIDER_REASONS, StreamNetworkError

    reason, dns_reason = "request_failed", None
    if isinstance(error, StreamNetworkError):
        if isinstance(error.reason, str) and error.reason in _STREAM_REQUEST_REASONS:
            reason = error.reason
        if isinstance(error.dns_reason, str) and error.dns_reason in DNS_PROVIDER_REASONS:
            dns_reason = error.dns_reason
    code = getattr(error, "code", None)
    curl_code = int(code) if isinstance(code, int) and not isinstance(code, bool) and 0 <= code <= 999 else None
    response = getattr(error, "response", None)
    status = _http_status(getattr(response, "status_code", None)) or _http_status(http_status)
    return f"reason={reason} dns_reason={dns_reason} curl_code={curl_code} http_status={status}"


def _pull_loop_impl(tag: str, web_rid: str, writer: FlvSegWriter, log: Log,
                   refresh_min: int, poll_sec: int, max_minutes: float,
                   node: str, cap: Dict[str, Any], quit_flag: List[bool],
                   quality: str, refresh_margin_sec: int, session_factory) -> str:
    """返回停止原因：offline / quota / stalled / error / max_minutes / signal"""
    url, _ = pick_stream_url(cap, quality)
    if not url:
        log("❌ capture 里没有可用 flv 地址")
        return "error"
    session = session_factory(cap)
    fail_streak = 0
    last_cap = now()
    last_state_check = 0.0
    started = now()

    def should_stop() -> bool:
        return quit_flag[0] or bool(writer.stop_check and writer.stop_check())

    def wait(seconds) -> bool:
        return interruptible_wait(seconds, should_stop)

    def recapture() -> bool:
        """重抓 URL+cookie；成功则就地更新 url/session。"""
        nonlocal url, session, last_cap
        c2 = capture_stream(web_rid, DATA / "_cap" / ("cap_%s.json" % tag), node)
        if not c2:
            return False
        u2, _ = pick_stream_url(c2, quality)
        if not u2:
            return False
        url, session = u2, session_factory(c2)
        last_cap = now()
        return True

    def still_live() -> bool:
        """连续失败时先问一句：人是不是已经下播了？

        实测（2026-10-19 研途）：主播一关播，CDN 立刻回 404，三次失败就把进程判死退出；
        但此时根本不是故障，而是正常下播。不区分的话每播完一场就要靠外部拉起，
        中间的空档等于没守（当天框框就是这么丢了 11 分钟）。
        """
        st = read_watch_state(tag)
        if st is None:
            return True          # 读不到状态 → 保守当作还播着
        return not is_confirmed_offline(st)

    def need_refresh() -> bool:
        """该不该重抓签名。

        - 地址里能读到 expire：剩余不足 margin 才换（默认 15 分钟），基本不 reload
        - 读不到 expire：退回按 refresh_min 定时换
        - 另有 6 小时硬上限：防"签名没到期但会话侧失效"这种看不见的坑。
          实测签名有效期 7 天（expire-now≈604800s），所以正常一场直播基本一次都不重抓。
        """
        if (now() - last_cap) / 60 >= 360:
            return True
        exp = url_expire_ts(url)
        if exp is None:
            return (now() - last_cap) / 60 >= refresh_min
        return (exp - now()) <= refresh_margin_sec

    log("🎙 开录 seg=%ds 配额=%.0fMB" %
        (writer.seg_seconds, writer.quota_bytes / 1048576))

    while True:
        if should_stop():
            return "signal"
        if max_minutes and (now() - started) / 60 >= max_minutes:
            return "max_minutes"
        if now() - last_state_check > 30:
            last_state_check = now()
            st = read_watch_state(tag)
            if st is not None and is_confirmed_offline(st):
                log("📴 下播（watch jsonl is_live=false，%s）" % st.get("time"))
                return "offline"
        if need_refresh():
            log("🔄 签名将到期，重抓 URL+cookie")
            log("🔄 已换新 URL" if recapture() else "⚠️ 重抓失败，沿用旧 URL 继续")
            last_cap = now()
        writer.begin_connection()
        request_started = time.monotonic()
        try:
            r = session.get(url, headers={"Referer": "https://live.douyin.com/"},
                            stream=True, timeout=(10, 30))
        except Exception as error:
            elapsed_ms = round((time.monotonic() - request_started) * 1000)
            writer.end_connection("error")
            fail_streak += 1
            log("❌ 连接失败(%d/3) %s elapsed_ms=%d" %
                (fail_streak, _request_error_details(error), elapsed_ms))
            if fail_streak >= 3:
                if not still_live():
                    log("📴 连续失败但已下播 → 按正常下播收尾")
                    return "offline"
                return "error"
            if wait(5 * fail_streak):
                return "signal"
            recapture()
            continue
        if r.status_code != 200:
            elapsed_ms = round((time.monotonic() - request_started) * 1000)
            status = _http_status(r.status_code)
            r.close()
            writer.end_connection("error")
            fail_streak += 1
            log("❌ HTTP %s(%d/3)，重抓地址 elapsed_ms=%d" % (status, fail_streak, elapsed_ms))
            if fail_streak >= 3:
                if not still_live():
                    log("📴 CDN 回 %s 且已下播 → 按正常下播收尾" % status)
                    return "offline"
                return "error"
            if wait(5 * fail_streak):
                return "signal"
            recapture()
            continue
        got = 0
        refresh_due = False
        connection_phase = "reconnecting"
        try:
            for chunk in r.iter_content(65536):
                if not chunk:
                    continue
                if should_stop():
                    return "signal"
                before_audio = writer.valid_audio_packets
                writer.feed(chunk)
                got += len(chunk)
                if writer.valid_audio_packets > before_audio:
                    fail_streak = 0
                # A video/metadata-only trickle must not masquerade as recording.
                # This bounds progressing HTTP streams; a blocked iter_content()
                # remains governed by curl's timeout and external health checks.
                audio_since = (writer.connection_audio_at if writer.connection_audio_at is not None
                               else writer.connection_started_at)
                if now() - audio_since > 120:
                    writer._invalid("audio_stalled")
                if max_minutes and (now() - started) / 60 >= max_minutes:
                    return "max_minutes"
                # 长流中也要按时换签名：不 break 出去，refresh 永远不会触发（实测踩过）
                if need_refresh():
                    refresh_due = True
                    break
                if now() - last_state_check > 30:
                    last_state_check = now()
                    st = read_watch_state(tag)
                    if st is not None and is_confirmed_offline(st):
                        log("📴 下播（watch jsonl is_live=false，%s）" % st.get("time"))
                        return "offline"
        except QuotaExceeded:
            return "quota"
        except Exception as error:
            elapsed_ms = round((time.monotonic() - request_started) * 1000)
            connection_phase = "error"
            writer.end_connection("error")
            log("⚠️ 拉流中断（本轮收 %.1f MB）：%s elapsed_ms=%d" %
                (got / 1048576, _request_error_details(error, r.status_code), elapsed_ms))
            fail_streak += 1
            if fail_streak >= 3:
                if not still_live():
                    log("📴 反复中断且已下播 → 按正常下播收尾")
                    return "offline"
                return "stalled"
            if wait(3 * fail_streak):
                return "signal"
            recapture()
            continue
        finally:
            writer.end_connection(connection_phase)
            try:
                r.close()
            except Exception:
                pass
        if refresh_due:
            log("🔄 到期换签名（流内触发，本轮收 %.1f MB）" % (got / 1048576))
            log("🔄 已换新 URL" if recapture() else "⚠️ 重抓失败，沿用旧 URL 继续")
            continue
        # 正常结束（服务端关流，通常是换清晰度/换 CDN）
        log("⚠️ 流被服务端结束（本轮收 %.1f MB），重抓" % (got / 1048576))
        fail_streak += 1
        if fail_streak >= 3:
            if not still_live():
                log("📴 服务端反复关流且已下播 → 按正常下播收尾")
                return "offline"
            return "stalled"
        if wait(3 * fail_streak):
            return "signal"
        recapture()


# ------------------------------------------------------------------ 主流程
def _process_created_at(pid: int) -> Optional[float]:
    """Read process creation time, returning unknown on unsupported/denied access."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        times = [ctypes.c_ulonglong(0) for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            return None
        return times[0].value / 1e7 - 11644473600.0
    finally:
        kernel.CloseHandle(handle)


def pid_owns_file(pid: int, pid_path: Path, skew: float = 5.0,
                  max_gap: float = 300.0, *, require_identity: bool = False) -> bool:
    """pid 活着**且**它确实是当初写这个 pidfile 的进程（防 PID 复用）。

    2026-10-25 踩过：Windows 把死掉的 recorder 的 PID 回收给了 WorkBuddy，
    只看 pid_alive 就会一直判「已有实例在跑」，于是死掉的那路永远不会自愈
    （日志停在两小时前，页面/名册都显示正常）。
    """
    if not pid_alive(pid):
        return False
    try:
        ct = _process_created_at(pid)
    except Exception:
        ct = None
    if ct is None:
        return not require_identity
    try:
        mt = pid_path.stat().st_mtime
    except OSError:
        return not require_identity
    return not (ct > mt + skew or mt - ct > max_gap)


def _record_main() -> int:
    ap = argparse.ArgumentParser(description="抖音直播间音频录流器")
    ap.add_argument("--tag", required=True, help="主播 tag，对应 data/watch_<tag>.jsonl")
    ap.add_argument("--seg-seconds", type=int, default=60)
    ap.add_argument("--quota-mb", type=int, default=1024, help="单路单场配额（MB）")
    ap.add_argument("--refresh-min", type=int, default=30, help="重抓 URL 间隔（分钟）")
    ap.add_argument("--poll", type=int, default=30, help="未开播时的轮询间隔（秒）")
    ap.add_argument("--max-minutes", type=float, default=0, help="单次最长录制（0=不限）")
    ap.add_argument("--once", action="store_true", help="只看状态，不录")
    ap.add_argument("--keep-video", action="store_true", help="保留视频轨（默认只留音频）")
    ap.add_argument("--quality", default="auto",
                    help="画质 SD1/SD2/HD1/FULL_HD1；auto=最低清（只录音频，省带宽）")
    ap.add_argument("--node", default=None)
    args = ap.parse_args()
    if CUSTOMER:
        global _CUSTOMER_IDENTITY
        _CUSTOMER_IDENTITY = publish_role_ready(CUSTOMER, "record", args.tag)

    log = Log(DATA / ("stream_%s.log" % args.tag))
    try:
        if installed_layout() is not None:
            selected = resolve_node()
            if args.node and Path(args.node).resolve() != Path(selected).resolve():
                log("客户安装的 Node.js 路径不匹配")
                return 4
            node = selected
        else:
            node = args.node or resolve_node()
    except RuntimeError:
        log("客户安装缺少固定的 Node.js；请检查安装组件。")
        return 4
    pid_path = DATA / ("stream_%s.pid" % args.tag)
    log("=== 录流器启动 tag=%s pid=%d node=%s py=%s ===" %
        (args.tag, os.getpid(), node, sys.executable))

    # 依赖前置检查：缺 curl_cffi 时**在这里就退出**，不要等开播后在 build_session()
    # 里炸（2026-10-25 教训：异常被 finally 吞成 stop_reason=unknown，一整天
    # 每次"开播"都写一条 0 分片 session，日志里一句错都没有）。
    try:
        import curl_cffi  # noqa: F401
    except Exception as e:
        log("❌ 缺依赖 curl_cffi：%s" % str(e)[:120])
        log("   当前解释器=%s" % sys.executable)
        if CUSTOMER:
            log("   客户录音依赖尚未就绪，请在应用选定的解释器中安装 curl_cffi。")
            return 4
        log('Install curl_cffi in the selected interpreter; use WB_RECORD_PY to select it.')
        log("   修法：由 anchor_manager 用 _resolve_record_python() 拉起，"
            "或设环境变量 WB_RECORD_PY 指向该解释器")
        return 4

    # 防双开
    if pid_path.exists():
        try:
            old = int(pid_path.read_text().strip())
            if pid_owns_file(old, pid_path):
                log("⚠️ 已有实例 pid=%d 在跑，退出" % old)
                return 0
            log("⚠️ pidfile 里的 pid=%d 已被别的进程占用（PID 复用），按残留清理" % old)
        except Exception:
            pass
    pid_path.write_text(str(os.getpid()), encoding="utf-8")

    quit_flag = [False]
    process_created_at = _process_created_at(os.getpid())
    record_progress = RecordingProgress(DATA / ("stream_%s.progress.json" % args.tag),
                                        session_id="", web_rid="",
                                        process_created_at=process_created_at)
    # Ready evidence is emitted even if the current room is idle or capture blocks.
    last_stop_check = [None]

    def should_stop():
        if quit_flag[0]:
            return True
        stamp = now()
        if last_stop_check[0] is None or stamp - last_stop_check[0] >= 1:
            last_stop_check[0] = stamp
            if (customer_stop_requested(CUSTOMER, _CUSTOMER_IDENTITY)
                    or stop_requested(args.tag, process_created_at)):
                quit_flag[0] = True
                log("收到匹配本进程身份的维护停止请求，准备收尾")
        return quit_flag[0]

    def wait(seconds):
        return interruptible_wait(seconds, should_stop)

    def _sig(signum, frame):
        quit_flag[0] = True
        log("收到信号 %s，准备收尾" % signum)
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, _sig)
        except Exception:
            pass

    try:
        record_progress.publish(phase="starting", force=True)
        quota = (None if args.once else
                 SessionQuota(DATA / ("stream_%s.state.json" % args.tag),
                              args.quota_mb * 1048576))
        while not should_stop():
            st = read_watch_state(args.tag)
            if st is None:
                log("⚠️ 找不到 data/watch_%s.jsonl，10s 后重试" % args.tag)
                if args.once:
                    return 2
                wait(10)
                continue
            if args.once:
                print(json.dumps(st, ensure_ascii=False))
                return 0
            may_record = quota.observe(st)
            if not st.get("is_live") or not st.get("web_rid"):
                if not getattr(main, "_idle_logged", False):
                    log("💤 未开播（%s），每 %ds 轮询" % (st.get("time"), args.poll))
                    main._idle_logged = True  # type: ignore[attr-defined]
                wait(args.poll)
                continue
            if not may_record:
                wait(max(60, args.poll))
                continue
            main._idle_logged = False  # type: ignore[attr-defined]
            web_rid = str(st["web_rid"])
            log("🔴 检测到开播 web_rid=%s nick=%s 在线=%s" %
                (web_rid, st.get("nick"), st.get("user_count")))
            day = datetime.now().strftime("%Y-%m-%d")
            out_dir = DATA / ("audio_%s_%s" % (args.tag, day))
            cap_path = DATA / "_cap" / ("cap_%s.json" % args.tag)
            cap = capture_stream(web_rid, cap_path, node)
            if not cap:
                log("❌ 抓不到 flv 地址（可能刚开播未起播/已结束），60s 后重试")
                wait(60)
                continue

            writer = FlvSegWriter(out_dir, args.seg_seconds, quota.remaining,
                                  args.keep_video, log)
            previous_progress = record_progress
            record_progress = RecordingProgress(
                DATA / ("stream_%s.progress.json" % args.tag),
                session_id=str(st.get("room_id_str") or st.get("room_id") or ""),
                web_rid=web_rid, process_created_at=process_created_at)
            writer.progress, writer.stop_check = record_progress, should_stop
            if (previous_progress.state["session_id"] == record_progress.state["session_id"]
                    and previous_progress.state["web_rid"] == web_rid
                    and previous_progress.state["phase"] != "offline"):
                for key in ("last_valid_audio_at", "valid_audio_packets", "valid_audio_bytes"):
                    setattr(writer, key, previous_progress.state[key])
                    record_progress.state[key] = previous_progress.state[key]
            record_progress.publish(phase="connecting", force=True)
            session_start = now()
            reason = "unknown"
            try:
                reason = pull_loop(args.tag, web_rid, writer, log, args.refresh_min,
                                   args.poll, args.max_minutes, node, cap, quit_flag,
                                   args.quality)
            except Exception as error:
                # Preserve the exception class without including request arguments or traceback text.
                reason = "exception:%s" % type(error).__name__
                log("💥 录制异常终止 %s %s" % (reason, _request_error_details(error)))
            finally:
                writer.close()
                quota.finish(writer.total_bytes, reason)
                meta = {
                    "tag": args.tag, "date": day, "web_rid": web_rid,
                    "session_start": fmt_ts(session_start),
                    "session_start_ts": round(session_start, 3),
                    "session_end": fmt_ts(now()),
                    "stop_reason": reason,
                    "seg_seconds": args.seg_seconds,
                    "session_mb": round(writer.total_bytes / 1048576, 2),
                    "segments": writer.segments,
                    "python": sys.executable,
                }
                # 0 分片不写 meta：失败场次混进 meta 会让"今天录了几场"失真
                # （10-25 一天写了 10 条 0 字节 session，看起来像录了 10 场）
                if writer.segments:
                    write_meta(out_dir, meta)
                else:
                    log("（0 分片，不写 meta —— 避免把失败场次混进场次记录）")
                log("⏹ 录制结束 reason=%s 分片=%d 共 %.1f MB 目录=%s" %
                    (reason, len(writer.segments), writer.total_bytes / 1048576, out_dir))
                record_progress.publish(phase=(reason if reason in
                    ("offline", "quota", "error", "stalled") else "stopped"), force=True)
            if reason == "signal":
                break
            if str(reason).startswith("exception:"):
                log("⏳ 异常后冷却 60s 再试（别在开播期间反复重建会话）")
                wait(60)
                continue
            if reason in ("error", "stalled"):
                log("😵 连续失败，退出（等 watchdog/计划任务拉起）")
                return 3
            if reason == "quota":
                log("💾 配额已满，停止本次录制；等下次开播")
                wait(max(60, args.poll))
                continue
            if reason == "max_minutes":
                log("⏱ 达到 --max-minutes，退出")
                return 0
            # offline → 先转写这一场，再回轮询
            if reason == "offline" and writer.segments:
                spawn_transcribe(args.tag, day, log)
            wait(10)
    finally:
        if CUSTOMER and any(child.poll() is None for child in _TRANSCRIBE_CHILDREN):
            try:
                record_progress.publish(phase="stopping", force=True)
            except OSError:
                log("⚠️ 无法写入停止中进度；继续等待已有转写")
            log("录音已收尾，等待本实例已有转写自然结束")
            while any(child.poll() is None for child in _TRANSCRIBE_CHILDREN):
                time.sleep(.25)
        try:
            record_progress.publish(phase="stopped", force=True)
        except OSError:
            log("⚠️ 无法写入录音停止进度")
        try:
            if pid_path.exists() and pid_path.read_text().strip() == str(os.getpid()):
                pid_path.unlink()
        except Exception:
            pass
        log("=== 录流器退出 pid=%d ===" % os.getpid())
    return 0


def main() -> int:
    if not CUSTOMER:
        return _record_main()
    # The OS-held sidecar protects the complete recorder/ASR drain lifetime.
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--tag", required=True)
    args, _ = parser.parse_known_args()
    validate_role_tag(args.tag)
    lock = FileLock(DATA / ("stream_%s.pid" % args.tag))
    if not lock.acquire(blocking=False):
        return 3
    try:
        return _record_main()
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
