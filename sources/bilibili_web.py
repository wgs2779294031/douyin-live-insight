# -*- coding: utf-8 -*-
"""B站网页端弹幕采集适配器（游客模式，无需登录、无需开放平台权限）。

从兄弟项目 danmu-collect/web_client.py 移植并改造（v2.6 任务书第 4 节）：
- 构造函数统一为 (cfg, storage, record_queue, shared_state=None)，不再自己
  open 文件落盘，统一走 JsonlStorage（danmu_bilibili_YYYY-MM-DD.jsonl）；
- _on_message 解析后：normalize_record 归一化 → storage.write(record) →
  record_queue.put_nowait(record)，双出口都做（队列满丢弃，与抖音源一致）；
- 短号解析成功后写 shared_state.room_id（shared_state 为 None 时跳过）；
- 保留原指数退避重连（1/2/4/8... 封顶 30s，连接成功重置）、brotli(protover=3)、
  运行统计、Ctrl+C 优雅退出全部能力。

协议：getDanmuInfo(带 buvid3 + wbi 签名) 换 token → WSS 连接 → op7 认证(uid=0)
      → op2 心跳 → op5 收弹幕
说明：游客模式昵称被 B站脱敏（如 "b***"），内容完整，用户以 user_hash 区分
（归一化后填入 user_id，原值放 extra.user_hash；见 sources/base.py）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import queue
import threading
import time
import urllib.parse
import zlib

import requests
import websocket

from sources.base import SourceAdapter, normalize_record

# brotli 为可选依赖（protover=3 压缩包需要）；未安装时降级
try:
    import brotli
    _HAS_BROTLI = True
except ImportError:
    _HAS_BROTLI = False

logger = logging.getLogger("bili")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

MAX_BACKOFF = 30  # 重连退避封顶秒数


# ============================================================ 纯函数（协议层，无副作用）
def pack_header(total_len, header_len, version, operation):
    return total_len.to_bytes(4, "big") + header_len.to_bytes(2, "big") \
        + version.to_bytes(2, "big") + operation.to_bytes(4, "big") + (1).to_bytes(4, "big")


def pack_auth(room_id, token, buvid):
    body = json.dumps({
        "uid": 0,
        "roomid": room_id,
        "protover": 2,
        "platform": "web",
        "type": 2,
        "key": token,
        "buvid": buvid,
    }, separators=(",", ":")).encode()
    return pack_header(16 + len(body), 16, 1, 7) + body


def pack_heartbeat():
    return pack_header(16, 16, 1, 2)


def unpack_packets(data):
    """按 16 字节包头拆包，返回 (operation, payload) 列表，处理 zlib/brotli 嵌套。"""
    out = []
    offset = 0
    n = len(data)
    while offset + 16 <= n:
        total_len = int.from_bytes(data[offset:offset+4], "big")
        header_len = int.from_bytes(data[offset+4:offset+6], "big")
        version = int.from_bytes(data[offset+6:offset+8], "big")
        operation = int.from_bytes(data[offset+8:offset+12], "big")
        if total_len == 0 or offset + total_len > n:
            break
        body = data[offset+header_len:offset+total_len]
        if version == 2:  # zlib 压缩：内部是嵌套包
            try:
                out.extend(unpack_packets(zlib.decompress(body)))
            except zlib.error:
                logger.warning("zlib 解压失败，跳过该包")
        elif version == 3:  # brotli 压缩
            if _HAS_BROTLI:
                try:
                    out.extend(unpack_packets(brotli.decompress(body)))
                except Exception as e:
                    logger.warning("brotli 解压失败，跳过该包: %s", type(e).__name__)
            else:
                logger.warning("收到 protover=3(brotli) 包但未安装 brotli，已跳过；"
                               "请 pip install brotli")
        else:  # version 0/1 明文 JSON
            out.append((operation, body))
        offset += total_len
    return out


def parse_danmaku(body):
    """解析 DANMU_MSG：info[1]=内容, info[2][1]=昵称, info[0][7]=user_hash"""
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if obj.get("cmd") != "DANMU_MSG":
        return None
    info = obj.get("info", [])
    try:
        content = info[1] if len(info) > 1 and isinstance(info[1], str) else ""
        uname = info[2][1] if len(info) > 2 and len(info[2]) > 1 else ""
        user_hash = info[0][7] if len(info) > 0 and len(info[0]) > 7 else ""
    except (IndexError, TypeError):
        content, uname, user_hash = "", "", ""
    return {"ts": int(time.time()), "type": "danmaku", "user_hash": user_hash,
            "uname": uname, "text": content}


# 系统消息白名单：正常推送但本采集器不解析（避免刷 WARNING；ENTRY_EFFECT
# 与 INTERACT_WORD 同一人重复，忽略避免双计）
_BILI_SYSTEM_CMDS = {
    "ENTRY_EFFECT", "WATCHED_CHANGE", "ONLINE_RANK_COUNT", "ONLINE_RANK_V3",
    "RANK_CHANGED_V2", "NOTICE_MSG", "STOP_LIVE_ROOM_LIST",
    "LOG_IN_NOTICE", "ROOM_REAL_TIME_MESSAGE_UPDATE",
    # v2.7.1：点赞聚合/飘屏通知类，非个人事件，统一忽略
    "LIKE_INFO_V3_UPDATE", "DM_INTERACTION", "HOT_ROOM_NOTIFY",
}


def _decode_pb(buf: bytes) -> dict:
    """裸 wire-format protobuf 解码器（标准库实现，无第三方依赖）。

    返回 {field_num: [(wire_type, value), ...]}，同字段可重复故用 list。
    wire 0=varint → (0, int)；wire 2=length-delimited，能严格 UTF-8 解码且"像文本"
    时存 (2, str)，否则递归存 (2, dict)；wire 1/5 跳过；任何异常立即停止并返回
    已解析部分（截断/脏数据不崩）。
    """
    out: dict = {}
    pos = 0
    n = len(buf)
    while pos < n:
        try:
            # read tag
            shift = 0
            tag = 0
            while True:
                if pos >= n:
                    return out
                b = buf[pos]
                pos += 1
                tag |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            field = tag >> 3
            wire = tag & 7
            if wire == 0:
                # varint
                shift = 0
                val = 0
                while True:
                    if pos >= n:
                        return out
                    b = buf[pos]
                    pos += 1
                    val |= (b & 0x7F) << shift
                    if not (b & 0x80):
                        break
                    shift += 7
                out.setdefault(field, []).append((0, val))
            elif wire == 2:
                # length-delimited
                shift = 0
                ln = 0
                while True:
                    if pos >= n:
                        return out
                    b = buf[pos]
                    pos += 1
                    ln |= (b & 0x7F) << shift
                    if not (b & 0x80):
                        break
                    shift += 7
                if ln < 0 or pos + ln > n:
                    return out
                seg = buf[pos:pos + ln]
                pos += ln
                stored = False
                try:
                    s = seg.decode("utf-8")
                    if s and _looks_like_text(s):
                        out.setdefault(field, []).append((2, s))
                        stored = True
                except UnicodeDecodeError:
                    pass
                if not stored:
                    out.setdefault(field, []).append((2, _decode_pb(seg)))
            elif wire == 1:
                pos += 8
                if pos > n:
                    return out
            elif wire == 5:
                pos += 4
                if pos > n:
                    return out
            else:
                return out
        except Exception:
            return out
    return out


def _looks_like_text(s: str) -> bool:
    """判断 length-delimited 段是否"像文本"（含 CJK 或字母，且无控制字符残留）。"""
    has_visible = any(c.isalpha() or ord(c) > 0x4E00 for c in s)
    has_ctrl = any(ord(c) < 0x20 and c not in "\t\n\r" for c in s)
    return has_visible and not has_ctrl


def _pb_first(fields: dict, num: int, wire_type=None, default=None):
    """取某字段第一个匹配值（同字段可重复）。"""
    items = fields.get(num) or []
    for wt, val in items:
        if wire_type is None or wt == wire_type:
            return val
    return default


def _pb_text(fields: dict, num: int, default: str = "") -> str:
    """取某字段第一个 wire2 值，仅当它是文本 str 时返回；否则（嵌套
    message 被递归成 dict）返回 default，杜绝把 dict repr 当文本落盘。"""
    v = _pb_first(fields, num, wire_type=2)
    return v if isinstance(v, str) else default


def _b64_decode_pb(pb_str):
    """base64 解码 pb 字段；失败尝试 urlsafe；都失败返回空 dict，不抛异常。"""
    if not pb_str:
        return {}
    if isinstance(pb_str, (bytes, bytearray)):
        return _decode_pb(bytes(pb_str))
    for fn in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            return _decode_pb(fn(pb_str, validate=False))
        except Exception:
            continue
    return {}


def parse_event(body):
    """解析 B站 WSS op5 业务消息：danmaku/enter/gift/like，其余忽略返 None。

    parse_danmaku 仅认 DANMU_MSG（向后兼容旧测试），parse_event 为新主入口，
    内部分发 cmd；产出与 parse_danmaku 同构的 dict（ts/type/user_hash/uname/text），
    交 normalize_record 走与弹幕完全一致的归一化链路。
    """
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    cmd = obj.get("cmd") or ""
    if cmd == "DANMU_MSG":
        return parse_danmaku(body)
    if cmd in ("INTERACT_WORD_V2", "INTERACT_WORD"):
        return _parse_enter(obj)
    if cmd in ("SEND_GIFT_V2", "SEND_GIFT"):
        return _parse_gift(obj)
    if cmd == "LIKE_INFO_V3_CLICK":
        return _parse_like(obj)
    if cmd in _BILI_SYSTEM_CMDS:
        return None
    logger.debug("[B站] 未识别 cmd=%s，已忽略", cmd)
    return None


def _parse_enter(obj):
    """INTERACT_WORD_V2 / INTERACT_WORD → enter 事件。

    V2 用户信息编码在 data.pb（base64 protobuf）：uid=f1、uname=f2、ts=f7；
    老版明文回退 data.uid/data.uname。uid 为空不丢弃（匿名进场仍记录）。
    """
    d = obj.get("data") or {}
    pb = _b64_decode_pb(d.get("pb"))
    if pb:
        uid = _pb_first(pb, 1, wire_type=0)
        uname = _pb_text(pb, 2)
        ts = _pb_first(pb, 7, wire_type=0)
    else:
        uid = d.get("uid")
        raw_uname = d.get("uname")
        uname = raw_uname if isinstance(raw_uname, str) else ""
        ts = d.get("timestamp")
    ts = int(ts) if ts else int(time.time())
    return {
        "ts": ts, "type": "enter",
        "user_hash": str(uid) if uid else "",
        "uname": uname or "",
        "text": "进入直播间",
    }


def _parse_gift(obj):
    """SEND_GIFT_V2 / SEND_GIFT → gift 事件。

    V2 用户/礼物信息编码在 data.pb：
    - uid = 顶层 f1，缺失试 f15.f1（免费匿名礼物两者皆无 → 留空）
    - uname = 顶层 f2，缺失试 f15.f2.f1（可能是平台打码名，原样保留）
    - 礼物对象 f10：f1=id、f2=name、f3=num、f10=ts
    老版明文回退 data.uid/data.uname/data.giftName/data.num。
    免费匿名礼物必须正常落盘（user_id 空），不得返 None。
    """
    d = obj.get("data") or {}
    pb = _b64_decode_pb(d.get("pb"))
    if pb:
        uid = _pb_first(pb, 1, wire_type=0)
        uname = _pb_text(pb, 2)
        if uid is None:
            f15 = _pb_first(pb, 15, wire_type=2)
            if isinstance(f15, dict):
                uid = _pb_first(f15, 1, wire_type=0)
        if not uname:
            f15 = _pb_first(pb, 15, wire_type=2)
            if isinstance(f15, dict):
                f2 = _pb_first(f15, 2, wire_type=2)
                if isinstance(f2, dict):
                    uname = _pb_text(f2, 1)
        f10 = _pb_first(pb, 10, wire_type=2)
        f10 = f10 if isinstance(f10, dict) else {}
        gift_id = _pb_first(f10, 1, wire_type=0)
        gift_name = _pb_text(f10, 2, default="礼物")
        num = _pb_first(f10, 3, wire_type=0)
        ts = _pb_first(f10, 10, wire_type=0)
    else:
        uid = d.get("uid")
        raw_uname = d.get("uname")
        uname = raw_uname if isinstance(raw_uname, str) else ""
        gift_id = d.get("giftId")
        raw_name = d.get("giftName")
        gift_name = raw_name if isinstance(raw_name, str) else None
        num = d.get("num")
        ts = d.get("timestamp")
    num = int(num) if num else 1
    gift_name = gift_name or "礼物"
    ts = int(ts) if ts else int(time.time())
    return {
        "ts": ts, "type": "gift",
        "user_hash": str(uid) if uid else "",
        "uname": uname or "",
        "text": f"赠送 {gift_name} x{num}",
    }


def _parse_like(obj):
    """LIKE_INFO_V3_CLICK → like 事件（明文 JSON，无 pb）。"""
    d = obj.get("data") or {}
    uid = d.get("uid")
    uname = d.get("uname")
    if not uname:
        uname = ((d.get("uinfo") or {}).get("base") or {}).get("name")
    text = str(d.get("like_text") or "为主播点赞了")
    return {
        "ts": int(time.time()), "type": "like",
        "user_hash": str(uid) if uid else "",
        "uname": str(uname) if uname else "",
        "text": text,
    }


# ============================================================ 采集器
class BilibiliWebAdapter(SourceAdapter):
    """B站网页游客源适配器（移植自 danmu-collect BiliDanmuCollector）。"""

    platform = "bilibili"
    # v2.7.1：四项全支持（LIKE_INFO_V3_CLICK 明文点赞流）
    capabilities = {"danmaku": True, "enter": True, "like": True, "gift": True}

    def __init__(self, cfg, storage, record_queue=None, shared_state=None) -> None:
        super().__init__(cfg, storage, record_queue, shared_state)
        # cfg.web_rid 复用为 B站房间号（--room 覆盖；纯数字直接用，短号待解析）
        raw_room = str(getattr(cfg, "web_rid", "") or "").strip()
        self.room_id = int(raw_room) if raw_room.isdigit() else raw_room
        self.heartbeat_interval = getattr(cfg, "heartbeat_interval", 30) or 30
        self.buvid = ""
        self.token = ""
        self.hosts = []  # [(host, wss_port), ...]
        # 运行统计
        self.danmaku_count = 0
        self.disconnect_count = 0
        self.last_reconnect_success = None
        # 控制
        self._stop = threading.Event()
        self._ws = None
        # 重连退避秒数：连接成功时在 _on_open 重置为 1
        self._backoff = 1
        # v2.7：op8 认证成功仅记一次 INFO（连接生命周期内）
        self._auth_ok_logged = False

    @property
    def msg_count(self) -> int:
        return self.danmaku_count

    # ---------- 风控指纹 + wbi 签名 ----------
    def fetch_buvid3(self):
        try:
            resp = requests.get(
                "https://api.bilibili.com/x/frontend/finger/spi",
                headers={"User-Agent": UA}, timeout=10,
            )
            b_3 = resp.json()["data"]["b_3"]
            logger.info("获取 buvid3 成功（%s...）", b_3[:20])
            return b_3
        except Exception as e:
            logger.warning("获取 buvid3 失败: %s", e)
            return ""

    def _cookie(self):
        return f"buvid3={self.buvid}"

    def _mixin_key(self, orig):
        return "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]

    def _wbi_keys(self):
        r = requests.get("https://api.bilibili.com/x/web-interface/nav",
                         headers={"Cookie": self._cookie(), "User-Agent": UA}, timeout=10)
        j = r.json()
        img_url = j["data"]["wbi_img"]["img_url"]
        sub_url = j["data"]["wbi_img"]["sub_url"]
        return img_url.rsplit("/", 1)[1].split(".")[0] + sub_url.rsplit("/", 1)[1].split(".")[0]

    def _wbi_sign(self, params):
        p = dict(params)
        p["wts"] = int(time.time())
        q = urllib.parse.urlencode(sorted(p.items()))
        p["w_rid"] = hashlib.md5((q + self._mixin_key(self._wbi_keys())).encode()).hexdigest()
        return p

    # ---------- getDanmuInfo 换 token ----------
    def resolve_room_id(self):
        try:
            r = requests.get(
                "https://api.live.bilibili.com/room/v1/Room/room_init",
                params={"id": self.room_id},
                headers={"Cookie": self._cookie(), "User-Agent": UA}, timeout=10,
            )
            j = r.json()
            real = (j.get("data") or {}).get("room_id")
            if real:
                if real != self.room_id:
                    logger.info("短号 %s → 真实房间号 %s", self.room_id, real)
                self.room_id = real
                # v2.6：解析成功后共享给 LLM worker（人设卡惰性加载依据），写入前判空
                if self.shared_state is not None:
                    self.shared_state.room_id = str(real)
        except Exception as e:
            logger.warning("房间号解析跳过（%s），使用 %s", e, self.room_id)

    def fetch_danmu_info(self):
        self.resolve_room_id()
        url = "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"
        resp = requests.get(
            url,
            params=self._wbi_sign({"id": self.room_id, "type": 0}),
            headers={
                "Cookie": self._cookie(),
                "User-Agent": UA,
                "Referer": f"https://live.bilibili.com/{self.room_id}",
            },
            timeout=10,
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"getDanmuInfo 失败: code={data.get('code')} "
                               f"msg={data.get('message')}")
        body = data["data"]
        self.token = body["token"]
        self.hosts = [(h["host"], h["wss_port"]) for h in body["host_list"]]
        logger.info("获取 token 成功，服务器: %s", self.hosts)

    # ---------- WS 回调 ----------
    def _on_message(self, ws, message):
        for operation, payload in unpack_packets(message):
            if operation == 8:
                self._handle_auth_reply(ws, payload)
            elif operation == 5:
                rec = parse_event(payload)
                if rec:
                    # v2.6：归一化（user_hash→user_id）→ 落盘 → 投递下游，双出口
                    record = normalize_record(self.platform, rec, room_id=self.room_id)
                    self.storage.write(record)
                    if self.record_queue is not None:
                        try:
                            self.record_queue.put_nowait(record)
                        except queue.Full:
                            # 弹幕高峰丢旧保新，绝不阻塞采集（与抖音源一致）
                            pass
                    self.danmaku_count += 1
                    logger.debug("[弹幕] %s %s: %s",
                                 record.get("uname"), record.get("user_id"),
                                 record.get("text"))

    def _handle_auth_reply(self, ws, payload):
        """v2.7：校验 op=8 认证回复。code=0 放行（仅记一次 INFO）；
        非 0 或非法 JSON 主动 close，触发 run_forever 返回走指数退避重连
        （fetch_danmu_info 会重换 token，不在此重复实现）。"""
        try:
            obj = json.loads(payload.decode("utf-8"))
            if not isinstance(obj, dict):
                raise ValueError("认证回复非 JSON 对象")
            raw_code = obj.get("code")
            code = int(raw_code) if raw_code is not None else -1
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            logger.error("[B站] WSS 认证回复非合法 JSON，主动 close 触发重连")
            ws.close()
            return
        if code == 0:
            if not self._auth_ok_logged:
                logger.info("WSS 认证成功（op8 code=0）")
                self._auth_ok_logged = True
        else:
            logger.error("[B站] WSS 认证失败 code=%s msg=%s，主动 close 触发重连",
                         code, obj.get("message") or "")
            ws.close()

    def _on_error(self, ws, error):
        logger.warning("WS 错误: %s", error)

    def _on_close(self, ws, code, msg):
        self.disconnect_count += 1
        logger.warning("连接关闭 code=%s（累计断线 %s 次）", code, self.disconnect_count)

    def _on_open(self, ws):
        # 连接成功：退避重置为 1，避免历史抖动推高的退避影响后续断线重连
        self._backoff = 1
        self.last_reconnect_success = time.strftime("%Y-%m-%d %H:%M:%S")
        logger.info("WSS 已连接，发送认证包")
        ws.send(pack_auth(self.room_id, self.token, self.buvid),
                opcode=websocket.ABNF.OPCODE_BINARY)

    def _heartbeat_loop(self, ws):
        while not self._stop.is_set():
            if self._stop.wait(self.heartbeat_interval):
                break
            try:
                ws.send(pack_heartbeat(), opcode=websocket.ABNF.OPCODE_BINARY)
                logger.debug("已发送心跳")
            except Exception as e:
                logger.warning("心跳失败: %s", e)
                break

    # ---------- 主循环（含断线重连） ----------
    def run(self):
        # 日志配置由 main.py 统一负责；落盘目录由 JsonlStorage 负责
        logger.info("启动 B站弹幕采集（游客模式）room_id=%s", self.room_id)

        self.buvid = self.fetch_buvid3()
        try:
            while not self._stop.is_set():
                try:
                    # 每次重连前重新换取 token（token 会过期，不复用旧值）
                    self.fetch_danmu_info()
                    host, port = self.hosts[0]
                    url = f"wss://{host}:{port}/sub"
                    logger.info("连接 %s", url)
                    self._ws = websocket.WebSocketApp(
                        url,
                        on_open=self._on_open,
                        on_message=self._on_message,
                        on_error=self._on_error,
                        on_close=self._on_close,
                    )
                    t = threading.Thread(target=self._heartbeat_loop,
                                         args=(self._ws,), daemon=True)
                    t.start()
                    self._ws.run_forever()
                    # run_forever 返回 = 连接断开
                    if self._stop.is_set():
                        break
                    logger.info("%ss 后尝试重连（退避）...", self._backoff)
                    if self._stop.wait(self._backoff):
                        break
                    self._backoff = min(self._backoff * 2, MAX_BACKOFF)
                except Exception as e:
                    logger.error("连接异常: %s，%ss 后重试", type(e).__name__, self._backoff)
                    if self._stop.wait(self._backoff):
                        break
                    self._backoff = min(self._backoff * 2, MAX_BACKOFF)
        except KeyboardInterrupt:
            logger.info("收到 Ctrl+C，正在退出...")
        finally:
            self.close()
            self._print_stats()

    def _print_stats(self):
        logger.info("===== 运行统计 =====")
        logger.info("收到弹幕数: %s", self.danmaku_count)
        logger.info("断线次数: %s", self.disconnect_count)
        logger.info("最近重连成功时间: %s",
                    self.last_reconnect_success or "（未发生重连）")
        logger.info("====================")

    def close(self):
        """优雅退出：置停止位并关闭 WebSocket（幂等）。"""
        self._stop.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
