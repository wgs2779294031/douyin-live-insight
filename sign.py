"""抖音网页版签名模块。

职责：
1. 生成 `_ac_signature`（纯 Python 哈希，复用开源 ac_signature.py）；
2. 生成 `msToken`（随机字符串）；
3. 生成 `user_unique_id`（19 位随机数字串）；
4. 生成 WebSocket 握手 `signature`（即 X-Bogus 风格）：mini_racer 跑 sign.js 的 `get_sign(md5(param))`。

签名 JS 执行后端：默认 auto，优先 mini_racer（Windows 预编译 wheel，纯 pip 安装），
装失败时 fallback 到 PyExecJS（需要系统装 Node.js 18+），并打 warning 日志。

参考开源实现：DouyinLiveWebFetcher/liveMan.py 的 generateSignature / generateMsToken。

TODO(联调): sign.js 必须原样拷贝，勿改；若抖音更新算法导致握手 401/断连，
需重新从开源仓库拉取新版 sign.js。
"""

from __future__ import annotations

import codecs
import hashlib
import logging
import os
import random
import string
import time
import urllib.parse
from typing import Optional

from ac_signature import get__ac_signature  # 开源纯 Python 实现

logger = logging.getLogger(__name__)

# sign.js 与本模块同目录
_SIGN_JS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sign.js")

# 参与签名的字段顺序（与开源 sign.js 的 get_sign 入参对齐，勿改顺序）
_SIGN_PARAMS = (
    "live_id,aid,version_code,webcast_sdk_version,"
    "room_id,sub_room_id,sub_channel_id,did_rule,"
    "user_unique_id,device_platform,device_type,ac,"
    "identity"
).split(",")

# mini_racer 上下文缓存：sign.js 体量大（约 492KB），避免每次握手都重编译
_mini_racer_ctx = None
_mini_racer_unavailable = False


# ---------------------------------------------------------------- JS 运行时探测
def _get_mini_racer_ctx():
    """惰性加载并缓存 MiniRacer + sign.js。失败时抛出并置位 unavailable 标志。"""
    global _mini_racer_ctx, _mini_racer_unavailable
    if _mini_racer_unavailable:
        return None
    if _mini_racer_ctx is not None:
        return _mini_racer_ctx
    try:
        from py_mini_racer import MiniRacer  # type: ignore
    except ImportError as e:
        _mini_racer_unavailable = True
        logger.warning("未安装 py-mini-racer，签名将降级到 execjs（需 Node.js）。原因: %s", e)
        return None
    try:
        with codecs.open(_SIGN_JS_PATH, "r", encoding="utf-8") as f:
            script = f.read()
        ctx = MiniRacer()
        ctx.eval(script)
        _mini_racer_ctx = ctx
        logger.info("MiniRacer 已加载 sign.js（%s 字节）", os.path.getsize(_SIGN_JS_PATH))
        return ctx
    except Exception as e:
        _mini_racer_unavailable = True
        logger.warning("MiniRacer 加载 sign.js 失败，降级到 execjs: %s", e)
        return None


def _call_sign_via_execjs(md5_param: str, ua: str) -> str:
    """execjs + Node.js 的降级路径。需系统装 Node.js。"""
    try:
        import execjs  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "签名运行时均不可用：py-mini-racer 装失败，PyExecJS 也未安装。"
            "请 `pip install py-mini-racer`（推荐）或 `pip install PyExecJS` 并装 Node.js 18+。"
        ) from e
    with codecs.open(_SIGN_JS_PATH, "r", encoding="utf-8") as f:
        script = f.read()
    # sign.js 入口函数 get_sign(md5_param) 与 mini_racer 路径一致
    ctx = execjs.compile(script)
    return ctx.call("get_sign", md5_param)


# ---------------------------------------------------------------- 各签名生成
def gen_ac_signature(site: str, nonce: str, ua: str, ts: Optional[int] = None) -> str:
    """`_ac_signature` 参数（纯 Python，无 JS 引擎依赖）。

    :param site: 网站域名，如 "www.douyin.com"
    :param nonce: __ac_nonce（从 cookie 获取或自造）
    :param ua: User-Agent
    :param ts: 时间戳（秒），默认当前
    """
    if ts is None:
        ts = int(time.time())
    return get__ac_signature(site, nonce, ua, ts)


def gen_ms_token(length: int = 182) -> str:
    """`msToken` cookie 参数：随机 base64 风格字符串。"""
    base_str = string.ascii_letters + string.digits + "-_"
    return "".join(random.choice(base_str) for _ in range(length))


def gen_user_unique_id() -> str:
    """`user_unique_id`：19 位随机数字串（模拟游客设备 id）。"""
    return "".join(random.choice(string.digits) for _ in range(19))


def gen_nonce() -> str:
    """`__ac_nonce`：16 位十六进制随机串（开源 demo 用的是固定 0123407cc00a9e438deb4）。"""
    return "".join(random.choice("0123456789abcdef") for _ in range(20))


def gen_signature(wss_url: str, backend: str = "auto") -> str:
    """WebSocket 握手 signature（X-Bogus 风格）。

    流程：
    1. 从 wss URL query 提取 _SIGN_PARAMS 中列出的字段；
    2. 按 `,` 拼成 `key=value,key=value,...`；
    3. 对拼接串做 MD5；
    4. mini_racer 调 `get_sign(md5_hex)` 得到 signature。

    :param wss_url: 完整 wss URL（含所有 query 参数）
    :param backend: auto / mini_racer / execjs
    """
    # 1. 提取 query 参数
    query = urllib.parse.urlparse(wss_url).query
    if not query:
        raise ValueError(f"wss URL 缺少 query 参数: {wss_url}")
    # 注意：开源实现用 i.split("=")[-1]，对带 = 的值会截断，但抖音参数不含 =，沿用此行为
    wss_maps: dict = {}
    for item in query.split("&"):
        if "=" in item:
            k, v = item.split("=", 1)
            wss_maps[k] = v
    tpl_params = [f"{k}={wss_maps.get(k, '')}" for k in _SIGN_PARAMS]
    param = ",".join(tpl_params)

    # 2-3. MD5
    md5_param = hashlib.md5(param.encode("utf-8")).hexdigest()

    # 4. 调 JS
    if backend == "execjs":
        return _call_sign_via_execjs(md5_param, "")
    if backend == "mini_racer":
        ctx = _get_mini_racer_ctx()
        if ctx is None:
            raise RuntimeError("MiniRacer 不可用，请改 backend=execjs 或装 py-mini-racer")
        return ctx.call("get_sign", md5_param)
    # auto
    ctx = _get_mini_racer_ctx()
    if ctx is not None:
        return ctx.call("get_sign", md5_param)
    return _call_sign_via_execjs(md5_param, "")
