r"""抖音弹幕 WebSocket 客户端。

职责：
1. 从 web_rid（短链 / 完整 URL / 纯数字）解析出真实 room_id 与 ttwid；
2. 拼接 webcast/im/push/v2 WSS URL，用 sign.gen_signature 生成握手 signature；
3. 建立 WebSocket 长连，发送心跳（30s，PushFrame payload_type='hb'）；
4. 接收 PushFrame，gzip 解压后按 Response.messages_list 分发解析并落盘；
5. 服务端 need_ack 时回 ack 包维持数据流；
6. 断线自动重连，退避序列 1s/2s/5s/10s 封顶。

参考开源实现：DouyinLiveWebFetcher/liveMan.py 的 DouyinLiveWebFetcher 类
（URL 模板、心跳字节、ack 包构造、消息分发均对齐）。

TODO(联调):
- wss_host 实测可能因地域/负载均衡下发不同 host，若握手 401/拒连，先用浏览器
  实测一个能用的 host 替换 config.wss_host；
- 30s 心跳若被服务端踢线（开源原版 5s），先降到 15s 再 5s；
- roomIdStr 正则若改版失效，可在响应里搜 ``"roomIdStr":"(\d+)"`` 双引号变体。
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
import websocket

import parser as douyin_parser
import sign as douyin_sign
from config import Config
from sources.base import normalize_record
from storage import JsonlStorage

logger = logging.getLogger(__name__)

# 断线重连退避序列（秒），到达上限后封顶
_RECONNECT_BACKOFF = [1, 2, 5, 10]

# roomId 提取正则：页面 SSR 数据为转义形式 \"roomId\":\"123\"
# 只匹配 roomId（不含 roomIdStr，排除 SPA 占位），取所有匹配中最后一个非 "0" 值
_ROOM_ID_RE = re.compile(r'roomId\\":\\"(\d+)\\"')

# 房间元数据「来源质量」排序（越大越好）。用于断线重连时防止兜底值覆盖好值。
META_SOURCE_RANK = {"fallback": 0, "room_page_html": 1, "webcast/room/web/enter": 2}


def _prefer_richer_meta(new: Dict[str, Any], prev: Dict[str, Any]) -> Dict[str, Any]:
    """元数据合并（纯函数）：本次来源质量**低于**已有值时，不覆盖 anchor_name/title。

    背景（2026-10-01 实测）：直播结束后断线重连，enter 接口取不到 → 退到房间页 HTML 正则，
    该正则会误抓页面上任意 nickname（把「冤种小徐」写成「かみ」）。
    规则：每项只在「新值非空」且「来源质量 >= 已有」时才允许覆盖。
    """
    new = dict(new or {})
    prev = dict(prev or {})
    if META_SOURCE_RANK.get(prev.get("source", ""), -1) > META_SOURCE_RANK.get(new.get("source", ""), -1):
        for k in ("anchor_name", "title"):
            new[k] = prev.get(k) or new.get(k, "")
        new["source"] = prev.get("source", new.get("source", ""))
    return new

# 心跳字节：PushFrame(payload_type='hb') 序列化结果（与开源 liveMan._sendHeartbeat 一致）
# 用 betterproto 在线构造更稳，这里仅作占位说明
_HB_PAYLOAD_TYPE = "hb"


class DanmuClient:
    def __init__(self, cfg: Config, storage: JsonlStorage,
                record_queue: Optional["queue.Queue"] = None,
                shared_state=None,
                monitor=None) -> None:
        self.cfg = cfg
        self.storage = storage
        # 规则层旁路队列：可选。若提供，则每条 record 落盘后会推送到该队列，
        # 由独立线程的 analyzer 消费；队列满时丢旧保新，绝不阻塞采集主循环。
        self.record_queue = record_queue
        # v2.3 主播人设卡：可选共享容器，_resolve_room() 成功后写 room_id
        self.shared_state = shared_state
        self._live_connection = None
        # v3.3 竞品监控旁路：可选 Monitor 实例（人气快照/系统事件/房间元数据独立落盘），
        # 与主链路完全独立；None 时零开销（B站源不接监控层）
        self.monitor = monitor

        self._closed = threading.Event()
        self._ws: Optional[websocket.WebSocketApp] = None
        self._stop_heartbeat = threading.Event()
        self._threads: List[threading.Thread] = []

        self._ttwid: Optional[str] = None
        self._room_id: Optional[str] = None
        self._user_unique_id: str = douyin_sign.gen_user_unique_id()
        # 认证是否成功过（用于重连退避计数清零）
        self._authenticated = False
        self.msg_count = 0

    # ================================================================ 房间解析
    def _live_signal(self, method, *args):
        callback = getattr(self.shared_state, method, None)
        if callable(callback):
            try:
                return callback(*args)
            except Exception:
                logger.debug("自动分析直播状态更新失败（不影响采集）")
        return None

    def _resolve_web_rid(self) -> str:
        """从 self.cfg.web_rid 提取 web_rid（短链跟随 302，完整 URL 取末段）。"""
        raw = self.cfg.web_rid.strip()
        # 纯数字直接返回
        if raw.isdigit():
            return raw
        # 短链 v.douyin.com/xxx → 跟随 302
        if raw.startswith("http"):
            try:
                resp = requests.get(raw, headers={"User-Agent": self.cfg.user_agent},
                                    allow_redirects=True, timeout=10)
                final_url = resp.url
            except Exception as e:
                raise RuntimeError(f"解析短链失败: {raw} ({e})") from e
            # 末段
            path = urlparse(final_url).path.rstrip("/")
            web_rid = path.rsplit("/", 1)[-1]
            if not web_rid:
                raise RuntimeError(f"无法从重定向 URL 提取 web_rid: {final_url}")
            return web_rid
        raise RuntimeError(f"web_rid 格式不支持: {raw}")

    def _resolve_room(self) -> Tuple[str, str]:
        """返回 (room_id, ttwid)。先取 ttwid，再请求直播间页面拿真实 room_id。"""
        self._live_connection = self._live_signal("begin_live_connection")
        web_rid = self._resolve_web_rid()
        headers = {"User-Agent": self.cfg.user_agent}

        # 1. ttwid：GET 直播首页 Set-Cookie
        if self._ttwid is None:
            try:
                resp = requests.get(self.cfg.api_base + "/", headers=headers, timeout=10)
                resp.raise_for_status()
                self._ttwid = resp.cookies.get("ttwid")
                if not self._ttwid:
                    logger.warning("未能从 %s 取到 ttwid cookie，可能匿名握手不稳", self.cfg.api_base)
            except Exception as e:
                raise RuntimeError(f"取 ttwid 失败: {e}") from e

        # 2. room_id：GET 直播间页正则取
        url = f"{self.cfg.api_base}/{web_rid}"
        cookie = self.cfg.cookie or ""
        if self._ttwid:
            cookie = f"{cookie}; ttwid={self._ttwid}" if cookie else f"ttwid={self._ttwid}"
        cookie = f"{cookie}; msToken={douyin_sign.gen_ms_token()}; __ac_nonce=0123407cc00a9e438deb4"
        try:
            resp = requests.get(url, headers={**headers, "cookie": cookie}, timeout=10)
            resp.raise_for_status()
        except Exception as e:
            raise RuntimeError(f"请求直播间页面失败: {url} ({e})") from e
        matches = _ROOM_ID_RE.findall(resp.text)
        if not matches:
            raise RuntimeError(f"未能从直播间页面解析出 room_id（web_rid={web_rid}）")
        # 排除 SPA 占位 "0"，取最后一个真实房间号
        real = [m for m in matches if m != "0"]
        room_id = (real or matches)[-1]
        # v2.3：解析成功后共享给 LLM worker（人设卡惰性加载依据），写入前判空
        if self.shared_state is not None:
            self.shared_state.room_id = room_id
        logger.info("解析到 room_id=%s ttwid=%s... web_rid=%s",
                    room_id, (self._ttwid or "")[:8], web_rid)
        # v3.3 监控层：房间元数据（主播名/标题/开播时间），失败只告警不阻塞
        if self.monitor is not None:
            self._fetch_room_info(room_id, web_rid, page_html=resp.text)
        return room_id, self._ttwid or ""

    def _fetch_room_info(self, room_id: str, web_rid: str,
                         page_html: str = "") -> None:
        """抓房间元数据（主播名/标题），写入监控层。

        实测（2026-09-30）：``webcast/room/info/`` 已 404；可用的是
        ``webcast/room/web/enter/``（需带真实 cookie，否则返回空体），
        数据在 ``data.data[0].{title,owner.nickname,status}``。
        兜底：直播间页 HTML（_resolve_room 已抓）里有 ``nickname\":\"…\"``。
        开播时间两个来源都不给 → 用监控层「首条消息时间」近似（first_msg_ts）。
        任何失败都只 WARNING，绝不阻塞采集。
        """
        meta = {
            "web_rid": web_rid,
            "room_id": room_id,
            "anchor_name": "",
            "title": "",
            "start_time": 0,
            "room_status": None,
            "source": "fallback",
        }
        live_status = None
        # 1) enter 接口（带真实 cookie）
        try:
            cookie = self.cfg.cookie or ""
            if self._ttwid:
                cookie = f"{cookie}; ttwid={self._ttwid}" if cookie else f"ttwid={self._ttwid}"
            resp = requests.get(
                f"{self.cfg.api_base}/webcast/room/web/enter/",
                params={"aid": "6383", "device_platform": "web", "web_rid": web_rid},
                headers={"User-Agent": self.cfg.user_agent, "cookie": cookie},
                timeout=10)
            resp.raise_for_status()
            data = (resp.json() or {}).get("data") or {}
            inner = data.get("data")
            room_items = inner if isinstance(inner, list) else [inner or {}]
            # 兼容旧结构 data.room_info
            for it in room_items:
                if not isinstance(it, dict):
                    continue
                ri = it.get("room_info") or it
                room = ri.get("room") or ri
                owner = ri.get("owner") or room.get("owner") or {}
                returned_room = room.get("id_str") or room.get("id")
                if returned_room is not None and str(returned_room) == str(room_id):
                    live_status = room.get("status")
                if room.get("title") or owner.get("nickname"):
                    meta.update({
                        "room_id": str(room.get("id_str") or room_id),
                        "anchor_name": owner.get("nickname") or "",
                        "title": room.get("title") or "",
                        "room_status": room.get("status"),
                        "source": "webcast/room/web/enter",
                    })
                    break
        except Exception as e:
            logger.warning("[监控] enter 接口元数据失败: %s", e)
        # Only this response may permit automatic work; richer persisted metadata cannot.
        self._live_signal("set_live_status", live_status, self._live_connection)
        # 2) 兜底：房间页 HTML 里的昵称（转义形式 nickname\":\"…\"）
        if not meta["anchor_name"] and page_html:
            m = re.search(r'nickname\\?":\\?"([^"\\]{1,80})', page_html)
            if m:
                meta["anchor_name"] = m.group(1)
                meta["source"] = "room_page_html"
        # ★ 2026-10-01 修：断线重连时房间常已下播 → enter 接口拿不到 → 落到房间页 HTML 正则，
        #   而那个正则会**误抓页面上任意一个 nickname**（实测把「冤种小徐」写成了「かみ」）。
        #   规则见 _prefer_richer_meta：来源质量更低时不覆盖已有的好值。
        try:
            prev = self.monitor.read_room_meta()
        except Exception:
            prev = {}
        meta = _prefer_richer_meta(meta, prev)
        try:
            self.monitor.write_room_meta(meta)
            logger.info("[监控] 房间元数据已落盘(source=%s): 主播=%s 标题=%s",
                        meta["source"], (meta["anchor_name"] or "")[:20],
                        (meta["title"] or "")[:30])
        except Exception as e:
            logger.warning("[监控] 房间元数据写盘失败（不影响采集）: %s", e)

    # ================================================================ WSS URL 拼装
    def _build_wss_url(self, room_id: str) -> str:
        """拼抖音 webcast/im/push/v2 WSS URL，含 signature。"""
        wss = (
            f"wss://{self.cfg.wss_host}/webcast/im/push/v2/?app_name=douyin_web"
            "&version_code=180800&webcast_sdk_version=1.0.14-beta.0"
            "&update_version_code=1.0.14-beta.0&compress=gzip&device_platform=web&cookie_enabled=true"
            "&screen_width=1536&screen_height=864&browser_language=zh-CN&browser_platform=Win32"
            "&browser_name=Mozilla"
            "&browser_version=5.0%20(Windows%20NT%2010.0;%20Win64;%20x64)%20AppleWebKit/537.36%20"
            "(KHTML,%20like%20Gecko)%20Chrome/126.0.0.0%20Safari/537.36"
            "&browser_online=true&tz_name=Asia/Shanghai"
            "&cursor=d-1_u-1_fh-7392091211001140287_t-1721106114633_r-1"
            f"&internal_ext=internal_src:dim|wss_push_room_id:{room_id}"
            f"|wss_push_did:{self._user_unique_id}"
            f"|first_req_ms:1721106114541|fetch_time:1721106114633|seq:1|"
            f"wss_info:0-1721106114633-0-0|wrds_v:7392094459690748497"
            f"&host={self.cfg.api_base}&aid=6383&live_id=1&did_rule=3&endpoint=live_pc&support_wrds=1"
            f"&user_unique_id={self._user_unique_id}&im_path=/webcast/im/fetch/&identity=audience"
            f"&need_persist_msg_count=15&insert_task_id=&live_reason=&room_id={room_id}&heartbeatDuration=0"
        )
        try:
            signature = douyin_sign.gen_signature(wss, backend=self.cfg.sign_backend)
            wss += f"&signature={signature}"
            logger.info("WSS URL 已签名（backend=%s）", self.cfg.sign_backend)
        except Exception as e:
            # B 档降级：不带 signature 试连（大概率被拒，但留口子）
            logger.warning("签名失败，将不带 signature 试连: %s", e)
        return wss

    # ================================================================ 心跳
    def _heartbeat_loop(self, connection=None) -> None:
        """WebSocket 长连心跳，间隔由配置 heartbeat_interval 决定（默认 30s）。"""
        if connection is None:
            connection = self._live_connection
        from protobuf.douyin import PushFrame  # 局部导入避免模块加载循环
        while not self._stop_heartbeat.wait(self.cfg.heartbeat_interval):
            if self._ws is None:
                continue
            try:
                hb = PushFrame(payload_type=_HB_PAYLOAD_TYPE).SerializeToString()
                self._ws.send(hb, opcode=websocket.ABNF.OPCODE_PING)
                logger.info("已发送心跳（间隔 %ss）", self.cfg.heartbeat_interval)
            except Exception as e:
                logger.warning("发送心跳失败: %s", e)
                self._live_signal("set_live_connected", False, connection)
                break

    def _start_thread(self, target, name: str) -> None:
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    # ================================================================ 消息处理
    def _handle_message(self, ws: websocket.WebSocketApp, message: bytes,
                        connection=None) -> None:
        """收包 → PushFrame → gzip 解压 → Response → ack + 业务分发落盘。"""
        if isinstance(message, str):
            message = message.encode("utf-8")
        try:
            pushframe = douyin_parser.parse_pushframe(message)
            payload = douyin_parser.decompress_payload(pushframe)
            response = douyin_parser.parse_response(payload)
        except Exception as e:
            logger.warning("PushFrame/Response 解析失败: %s（前 80 字节: %r）", e, message[:80])
            return

        # 服务端要求 ack：回包以维持数据流
        if response.need_ack:
            try:
                ack = douyin_parser.build_ack(pushframe.log_id, response.internal_ext)
                ws.send(ack, opcode=websocket.ABNF.OPCODE_BINARY)
            except Exception as e:
                logger.warning("发送 ack 失败: %s", e)

        # v3.3 监控层旁路：人气快照/系统事件独立落盘，异常兜底不影响主链路
        try:
            for _, kind, payload in douyin_parser.iter_monitor_messages(response):
                if kind != "control":
                    continue
                control = douyin_parser.normalize_monitor(kind, payload)
                if (control and control.get("status") == 3
                        and str(control.get("room_id") or "") in ("", str(self._room_id or ""))):
                    self._live_signal("set_live_status", 3,
                                      self._live_connection if connection is None else connection)
        except Exception:
            logger.debug("自动分析下播信号解析失败（不影响采集）")
        if self.monitor is not None:
            try:
                self.monitor.handle_response(response)
                # 冷门 method（福袋 / PK 等，proto 无类）原始留存，绝不丢
                self.monitor.handle_raw_methods(response)
            except Exception as e:
                logger.warning("[监控] 监控消息处理失败（不影响主链路）: %s", e)

        # 业务消息分发
        had_msg = False
        for method, msg_type, msg_payload in douyin_parser.iter_messages(response):
            had_msg = True
            record = douyin_parser.normalize(msg_type, msg_payload)
            if record is None:
                continue
            # v2.6 适配器改造（最小 diff，唯一改动点）：record 构造后接入统一事件
            # schema，仅新增 platform/room_id/extra 三个字段；归一化逻辑集中在
            # sources/base.normalize_record，抖音协议解析逻辑零改动。
            record = normalize_record(
                "douyin", record,
                room_id=self._room_id or self.cfg.web_rid)
            self.storage.write(record)
            # 规则层旁路：落盘后推入队列供 analyzer 独立线程消费
            if self.record_queue is not None:
                try:
                    self.record_queue.put_nowait(record)
                except queue.Full:
                    # 弹幕高峰丢旧保新，绝不阻塞采集
                    pass
            self.msg_count += 1
            if self.msg_count % 100 == 0:
                logger.info("已接收并落盘消息 %s 条", self.msg_count)
        # 首次成功收到消息即认为认证/链路 OK
        if had_msg and not self._authenticated:
            self._authenticated = True
            logger.info("WebSocket 认证成功，开始接收 web_rid=%s 的消息", self.cfg.web_rid)
            # 监控层：首条消息时间（room/info 拿不到 start_time 时的开播近似）
            if self.monitor is not None:
                try:
                    self.monitor.patch_room_meta(first_msg_ts=int(time.time()))
                except Exception:
                    pass

    # ================================================================ 连接与重连
    def run(self) -> None:
        """阻塞运行：解析房间 → 拼 URL → 连接 → 收消息 → 断线重连。"""
        attempt = 0
        while not self._closed.is_set():
            self._authenticated = False
            self._stop_heartbeat.clear()
            self._threads = []

            try:
                room_id, ttwid = self._resolve_room()
                self._room_id = room_id
                wss_url = self._build_wss_url(room_id)
                self._serve(wss_url, ttwid)
            except Exception as e:
                logger.error("连接/保活异常: %s", e)

            if self._closed.is_set():
                break

            # 认证成功过说明链路本身正常，重连退避计数清零；否则按序列退避
            if self._authenticated:
                attempt = 0
            delay = _RECONNECT_BACKOFF[min(attempt, len(_RECONNECT_BACKOFF) - 1)]
            attempt += 1
            logger.warning("连接断开，%s 秒后尝试重连（第 %s 次）", delay, attempt)
            if self._closed.wait(delay):
                break

    def _serve(self, wss_url: str, ttwid: str) -> None:
        """建立一次 WebSocket 长连并阻塞到连接关闭。"""
        logger.info("正在连接 WebSocket: wss://%s/...room_id=%s",
                    self.cfg.wss_host, self._room_id)

        connection = self._live_connection
        def on_open(ws: websocket.WebSocketApp) -> None:
            self._live_signal("set_live_connected", True, connection)
            logger.info("WebSocket 已建立，启动心跳线程")
            self._start_thread(lambda: self._heartbeat_loop(connection), "ws-heartbeat")

        def on_message(ws: websocket.WebSocketApp, message) -> None:
            self._handle_message(ws, message, connection=connection)

        def on_error(ws: websocket.WebSocketApp, error) -> None:
            self._live_signal("set_live_connected", False, connection)
            logger.warning("WebSocket 错误: %s", error)

        def on_close(ws: websocket.WebSocketApp, *args) -> None:
            self._live_signal("set_live_connected", False, connection)
            logger.warning("WebSocket 连接已关闭")
            self._stop_heartbeat.set()

        cookie = self.cfg.cookie or ""
        if ttwid:
            cookie = f"{cookie}; ttwid={ttwid}" if cookie else f"ttwid={ttwid}"
        headers = {
            "cookie": cookie,
            "user-agent": self.cfg.user_agent,
        }
        self._ws = websocket.WebSocketApp(
            wss_url,
            header=headers,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        # ping 由我们自己按抖音 PushFrame 协议发，这里关掉 websocket-client 的协议层 ping
        try:
            self._ws.run_forever(ping_interval=0)
        finally:
            self._live_signal("set_live_connected", False, connection)
            self._stop_heartbeat.set()
            self._ws = None

    def close(self) -> None:
        """优雅退出：停止重连循环 → 关闭长连 → 关闭落盘文件。"""
        logger.info("正在停止采集客户端...")
        self._live_signal("set_live_connected", False, self._live_connection)
        self._closed.set()
        self._stop_heartbeat.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
