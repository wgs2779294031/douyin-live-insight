#!/usr/bin/env python
"""蹲一个抖音主播开播 → 抓 web_rid → （可选）直接起弹幕采集。

为什么需要它（2026-10-01 实测结论）
--------------------------------
抖音 web 里，「某个主播现在开没开播 / 他的直播间号」**只有一个可靠来源**：

    GET https://www.douyin.com/aweme/v1/web/user/profile/other/?sec_user_id=<sec_uid>

- 未开播：`user.live_status = 0`、`user.room_id_str = "0"`、**没有 `room_data`**
- 已开播：`user.live_status = 1`、`user.room_id_str = <19 位内部房间号>`、
  `user.room_data` 是一个 **JSON 字符串**，里面有 **`owner.web_rid`（= 直播间 URL 里那段）**

⚠️ 两个坑（都实测过）：
1. **采集端 `main.py` 要的是 `web_rid`，不是 19 位 `room_id`。**
   把 19 位 room_id 灌进 `live.douyin.com/<id>` 时，页面会把 `web_rid` 回显成它自己，
   `client._ROOM_ID_RE`（`roomId\":\"(\\d+)\"`）**取不到真 room_id** → 采集起不来。
   所以必须从 `room_data.owner.web_rid` 拿。
2. **接口带 `a_bogus` 签名，纯 requests 会被风控**（`status_msg=blocked`）。
   本仓 `a_bogus.js` 生成的签名实测**过不了**（可能缺 webid/verifyFp 等环境绑定）。
   → **走已登录浏览器通道**（CDP + 页面内 fetch，同源自带 cookie/签名）。
   平台仍可能要求人工登录或验证，不能保证持续可用。

用法
----
    # 只看状态（单次）
    python scripts/watch_anchor_live.py --sec-uid <ms4w...> --once

    # 蹲到开播为止（每 60s 一查），开播后自动起采集
    python scripts/watch_anchor_live.py --sec-uid <ms4w...> --tag "就业-冤种小徐" --start

    # 只蹲不采集（把命令打出来，自己决定何时起）
    python scripts/watch_anchor_live.py --sec-uid <ms4w...> --tag "就业-冤种小徐"

前置：浏览器已用 `--remote-debugging-port=9222` 起好、且**有一个 www.douyin.com 的标签页**
（同源才能带 cookie fetch）。真机启动命令见 douyin-web-channel 技能 §二。

产物：``data/watch_<tag>.jsonl``（每次轮询一行，含 live_status / web_rid / room_id / 在线人数）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import notify  # noqa: E402  告警推送（缺凭据/失败时静默，不拖垮监控）

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.local_installation import bootstrap_entry, installed_layout
bootstrap_entry()
from file_lock import file_lock  # noqa: E402
from scripts.local_context import (activate_customer_environment, browser_owner_valid,
    customer_stop_requested, load_customer_context)
from scripts.monitoring_alerts import (MonitoringAlertState, deliver_event,
    business_filename_unique, latest_business_timestamp, target_watch_enabled)
from config import RoomConfig

CUSTOMER = load_customer_context()
activate_customer_environment(CUSTOMER)
DATA = Path(CUSTOMER["data"]) if CUSTOMER else ROOT / "data"


def _notify(*args, **kwargs):
    if CUSTOMER:
        kwargs["config_path"] = CUSTOMER["config"]
    return notify.send_text(*args, **kwargs)


PROFILE_API = ("https://www.douyin.com/aweme/v1/web/user/profile/other/"
               "?device_platform=webapp&aid=6383&channel=channel_pc_web"
               "&publish_video_strategy_type=2&source=channel_pc_web"
               "&sec_user_id={sec}&update_version_code=170400")

# ---------------------------------------------------------------- 可移植路径解析
# 2026-10-04 移植准备：默认值不再绑定本机绝对路径，解析顺序 =
#   环境变量（WATCH_NODE / WATCH_CDP）→ 仓内自持副本 → PATH 探测 → 本机遗留默认。
# cdp.mjs 已拷入 scripts/（零依赖单文件），clone 仓库即可用，不再依赖 WorkBuddy 技能目录。


def resolve_node(env: Optional[Dict[str, str]] = None) -> str:
    """定位 node 可执行文件：环境变量 → PATH → 本机已知位置（Windows）。"""
    env = os.environ if env is None else env
    installed = installed_layout()
    if installed is not None:
        selected = installed["node"]
        if not selected.is_file():
            raise NodeExecutableNotFound("客户安装缺少固定的 Node.js")
        return str(selected)
    cand = [env.get("WATCH_NODE"), shutil.which("node")]
    pass  # No machine-specific fallback; explicit configuration and bundled tools remain.
    for c in cand:
        if c and Path(c).exists():
            return c
    return "node"


class NodeExecutableNotFound(RuntimeError):
    """A missing local dependency is a configuration error, not browser failure."""


def resolve_cdp(env: Optional[Dict[str, str]] = None) -> str:
    """定位 cdp.mjs：环境变量 → 仓内自持副本 → WorkBuddy 技能目录（旧位置兜底）。"""
    env = os.environ if env is None else env
    if installed_layout() is not None:
        return str(ROOT / "scripts/cdp.mjs")
    cand = [env.get("WATCH_CDP"), str(ROOT / "scripts" / "cdp.mjs")]
    pass  # No machine-specific fallback; explicit configuration and bundled tools remain.
    for c in cand:
        if c and Path(c).exists():
            return c
    return cand[0] or "cdp.mjs"

_JS_TMPL = (
    "(async()=>{const sec=%s;const u=%s;"
    "try{const r=await fetch(u,{credentials:'include'});const j=await r.json();"
    "const x=j.user||{};return JSON.stringify({ok:true,code:j.status_code,msg:j.status_msg,"
    "live_status:x.live_status,room_id_str:x.room_id_str,room_data:x.room_data||null,"
    "nick:x.nickname,fans:x.follower_count});}"
    "catch(e){return JSON.stringify({ok:false,err:String(e)});}})()"
)


def _q(s: str) -> str:
    """把 python 字符串塞进 JS 单引号字面量。"""
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"


def build_js(sec_uid: str) -> str:
    return _JS_TMPL % (_q(sec_uid), _q(PROFILE_API.format(sec=sec_uid)))


def cdp_eval(node: str, cdp: str, js: str, target_url: Optional[str], timeout: int = 40) -> str:
    if CUSTOMER and not browser_owner_valid(CUSTOMER):
        raise RuntimeError("customer browser ownership unavailable")
    env = dict(os.environ)
    if target_url:
        env["CDP_TARGET_URL"] = target_url
    # encoding 必须显式 utf-8（2026-10-05 真机根因）：node 输出恒为 UTF-8 且含中文昵称，
    # 默认按系统 locale（cp936）解码会 UnicodeDecodeError → 读取线程炸掉 → stdout=None
    # → 被 (p.stdout or "") 防御吞成空串 → 报「浏览器返回非 JSON」这种误导性错误。
    try:
        p = subprocess.run([node, cdp, "eval", js], capture_output=True, text=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                           encoding="utf-8", errors="replace",
                           timeout=timeout, env=env, cwd=str(ROOT))
    except FileNotFoundError as exc:
        raise NodeExecutableNotFound("未找到 Node.js 可执行文件，请安装 Node.js 或设置 WATCH_NODE") from exc
    if p.returncode != 0:
        # 2026-10-05 真机出现过 p.stderr 为 None（原因未复现），防御掉，
        # 让真实错误进 error 字段而不是被 AttributeError 掩盖
        raise RuntimeError((p.stderr or "").strip()
                           or "cdp eval 失败（浏览器没起 / 没有可定位的标签页？）")
    return (p.stdout or "").strip()


def poll_once(node: str, cdp: str, sec_uid: str, target_url: Optional[str]) -> Dict[str, Any]:
    raw = cdp_eval(node, cdp, build_js(sec_uid), target_url)
    try:
        data = json.loads(raw)
    except Exception as e:
        raise RuntimeError("浏览器返回非 JSON：%s（%s）" % (raw[:200], e)) from e
    if not data.get("ok"):
        raise RuntimeError("页面 fetch 失败：%s" % data.get("err"))

    out = {
        "ts": int(time.time()),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "live_status": data.get("live_status"),
        "room_id_str": data.get("room_id_str"),
        "nick": data.get("nick"),
        "fans": data.get("fans"),
        "web_rid": None,
        "room_status": None,
        "user_count": None,
        "title": None,
        "api_code": data.get("code"),   # status_code 原值（0=正常）
        "api_msg": data.get("msg"),     # status_msg 原值（如 blocked）
    }
    rd = data.get("room_data")
    if rd:
        try:
            rdo = json.loads(rd) if isinstance(rd, str) else rd
            out["web_rid"] = (rdo.get("owner") or {}).get("web_rid")
            out["room_status"] = rdo.get("status")
            out["user_count"] = rdo.get("user_count")
            out["title"] = rdo.get("title")
        except Exception as e:
            out["room_data_parse_error"] = str(e)
    out["is_live"] = bool(out["web_rid"]) and (out.get("live_status") == 1
                                               or out.get("room_status") == 2)
    return out


def cached_web_rid(tag: str) -> Optional[str]:
    """A cached room number is only a lookup hint; it does not prove identity."""
    if not tag or any(c in '/\\:*?"<>|' for c in tag):
        return None
    path = DATA / ("watch_%s.config.json" % tag)
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    rooms = config.get("rooms") if isinstance(config, dict) else None
    if not isinstance(rooms, list):
        return None
    matches = [r.get("room_id") for r in rooms if isinstance(r, dict) and r.get("label") == tag]
    if len(matches) != 1 or type(matches[0]) not in (str, int):
        return None
    rid = str(matches[0])
    return rid if rid.isascii() and rid.isdigit() and rid.strip("0") else None


def build_room_enter_js(sec_uid: str, rid: str) -> str:
    # Compare owner identity inside the page. Do not return raw owner or signed URLs.
    return r"""(async () => {
        const expected = %s, rid = %s;
        if (!['https://www.douyin.com','https://live.douyin.com'].includes(location.origin))
            return {reason:'unexpected_origin'};
        try {
            const url = new URL('https://live.douyin.com/webcast/room/web/enter/');
            url.search = new URLSearchParams({aid:'6383', device_platform:'web', web_rid:rid});
            const res = await fetch(url, {credentials:'include', cache:'no-store', redirect:'error'});
            if (!res.ok) return {reason:'http_failure'};
            const j = await res.json();
            if (j?.status_code !== 0) return {reason:'api_failure'};
            const items = j.data?.data;
            if (!Array.isArray(items) || items.length !== 1) return {reason:'unconfirmed_room'};
            const info = items[0]?.room_info || items[0];
            const room = info?.room || info;
            const owner = info?.owner || room?.owner;
            if (!expected || owner?.sec_uid !== expected) return {reason:'owner_unconfirmed'};
            const roomId = room.id_str || (typeof room.id === 'string' ? room.id : '');
            if (room.status !== 2 || typeof roomId !== 'string' || !/^[1-9][0-9]*$/.test(roomId))
                return {reason:'live_unconfirmed'};
            return {reason:'verified_live', room_id_str:roomId,
                    nick:owner.nickname, title:room.title, user_count:room.user_count};
        } catch (_) { return {reason:'request_failed'}; }
    })()""" % (json.dumps(sec_uid), json.dumps(rid))


def poll_with_fallback(node: str, cdp: str, sec_uid: str, target_url: str, tag: str) -> Dict[str, Any]:
    try:
        primary = poll_once(node, cdp, sec_uid, target_url)
        kind = classify_env_failure(primary)
        if kind is None and primary.get("api_code") in (None, 0):
            if (primary.get("is_live") is True and primary.get("web_rid")) or is_confirmed_offline(primary):
                return primary
        reason = "profile_blocked_or_empty" if kind == "auth" else "profile_unconfirmed"
    except NodeExecutableNotFound:
        raise
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError, AttributeError, TypeError) as exc:
        reason = "profile_blocked_or_empty" if "blocked" in str(exc).lower() else "profile_request_failed"

    result = {"ts": int(time.time()), "time": time.strftime("%Y-%m-%d %H:%M:%S"),
              "profile_error": reason, "error": reason, "is_live": False}
    rid = cached_web_rid(tag)
    if not rid:
        result["room_enter_result"] = "no_cached_room"
        return result
    try:
        details = json.loads(cdp_eval(node, cdp, build_room_enter_js(sec_uid, rid), target_url))
    except NodeExecutableNotFound:
        raise
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError):
        details = None
    outcome = details.get("reason") if isinstance(details, dict) else None
    internal = details.get("room_id_str") if isinstance(details, dict) else None
    if outcome != "verified_live" or not (isinstance(internal, str) and internal.isascii()
                                          and internal.isdigit() and internal.strip("0")):
        safe = ("unexpected_origin", "http_failure", "api_failure", "unconfirmed_room",
                "owner_unconfirmed", "live_unconfirmed", "request_failed")
        result["room_enter_result"] = outcome if outcome in safe else "request_failed"
        return result
    return {"ts": int(time.time()), "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "is_live": True, "live_status": None, "room_status": 2, "web_rid": rid,
            "room_id_str": internal, "nick": details.get("nick"),
            "title": details.get("title"), "user_count": details.get("user_count"),
            "api_code": 0, "probe_source": "room_enter_fallback", "profile_error": reason}


def _append(out_path: Path, rec: Dict[str, Any]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 环境异常判定
# 老大 2026-10-04 拍板：遇到登录态/通道受限**不放弃任务**，暂停并请求人工协助，
# 恢复后自动继续（脚本不退出，不丢采集）。
ASSIST_STREAK = 3        # 连续多少轮环境异常才判定「需要人工」
ASSIST_RETRY_SECONDS = 300  # 等待人工期间的重试间隔（降频，别 60s 打一次风控）

# ---- 采集进程自愈（2026-10-29） ----
# main.py 会因外部原因被硬杀（日志无 traceback、rc=1）。以前这里用一次性
# `started` 标志，杀了就再也不重生，形成「探测正常 / 直播中 / 零数据」的静默失效。
COLLECTOR_FAST_EXIT_SECONDS = 180.0  # 存活短于这个算「刚起就退」
COLLECTOR_FUSE = 4                   # 连续这么多次刚起就退 → 暂停自动重启
COLLECTOR_PAUSE_SECONDS = 900.0      # 暂停多久后自动再试（不永久放弃）
COLLECTOR_HEALTHY_SECONDS = 600.0    # 跑满这么久算健康，清零快速退出计数


def classify_env_failure(rec: Dict[str, Any]) -> Optional[str]:
    """把一轮探测结果分类：None=正常；'auth'=登录态/风控受限；'channel'=浏览器通道不可达。

    判据（全部来自实测过的失败形态）：
    - rec 带 `error`：cdp 不可达 / 页面 fetch 抛错 → channel；错误文本含 blocked → auth
    - `api_msg == "blocked"`：接口被风控（登录态或环境异常）→ auth
    - 三要素全空（live_status/nick/room_id_str 均为 None）：接口返回空壳，
      多半是未登录或被风控 → auth（正常未开播时 live_status=0、nick 有值）
    """
    if "error" in rec:
        return "auth" if "blocked" in str(rec["error"]) else "channel"
    if str(rec.get("api_msg") or "").lower() == "blocked":
        return "auth"
    if rec.get("live_status") is None and rec.get("nick") is None \
            and rec.get("room_id_str") is None:
        return "auth"
    if "room_data_parse_error" in rec:
        return "channel"
    if rec.get("is_live") and rec.get("web_rid"):
        return None
    # is_live=False also means "could not derive the room URL". A profile that
    # says live, lacks its status, or has an unknown status is not an offline
    # observation. Only the documented live_status=0 shape is affirmative.
    if type(rec.get("live_status")) is not int or rec["live_status"] != 0 \
            or rec.get("room_status") == 2:
        return "channel"
    return None


def is_confirmed_offline(rec: Dict[str, Any], kind: Optional[str] = None) -> bool:
    """本轮是否是「**有效响应**里明确未在播」—— 只有这种才允许累计下播。

    ★ 2026-11-01 审计 P0：main 循环原来直接看 ``rec["is_live"]``，而探测抛异常时
      except 分支会兜底造一个 ``is_live=False`` 的 rec。于是浏览器掉线 / CDP 端口
      没响应 / 登录态被风控，两轮（约 2 分钟）就把一个**正在正常收弹幕**的采集器
      杀掉（走 finally 的 proc.terminate()），整场数据断在这里；而 watch 子进程
      本身还活着、看门狗看它活着判健康 →「进程活着 + 直播中 + 零数据」重演。

      环境异常时探测结果无效：既不能累加 offline_streak（否则迟早凑够 2 次），
      也不能动采集器（它跟探测通道是两条独立链路）。

    :param rec: 本轮探测记录（含兜底 rec）
    :param kind: 已算好的 classify_env_failure 结果；None 表示调用方没算，本函数自算
    """
    if kind is None:
        kind = classify_env_failure(rec)
    if kind is not None:
        return False
    return (not rec.get("is_live") and type(rec.get("live_status")) is int
            and rec["live_status"] == 0 and rec.get("room_status") != 2
            and "room_data_parse_error" not in rec)


def _announce_assist(streak: int) -> None:
    print("[探测] 连续 %d 轮未能确认主播开播状态，将间隔至少 %d 秒重试。"
          % (streak, ASSIST_RETRY_SECONDS))


def _customer_monitoring_step(state, tag, rec, kind, *, sec_uid, file_tag=None,
                              now=None, wall_now=None):
    """Observe one installed-role sample without changing collection decisions."""
    if not CUSTOMER:
        return None
    if customer_stop_requested(CUSTOMER, allow_parent=True):
        state.retire()
        return state.snapshot()
    enabled = target_watch_enabled(CUSTOMER["roster"], tag, sec_uid)
    if enabled is None:
        state.pause()
        return state.snapshot()
    now = time.monotonic() if now is None else now
    wall_now = time.time() if wall_now is None else wall_now
    probe = "unknown"
    if kind is None and rec.get("api_code") in (None, 0):
        if rec.get("is_live") is True and rec.get("web_rid"):
            probe = "live"
        elif is_confirmed_offline(rec, kind):
            probe = "offline"
    event = state.observe(now=now, wall_now=wall_now, probe=probe,
        probe_ts=rec.get("ts"), active=enabled,
        business_ts=latest_business_timestamp(DATA, tag, wall_now, file_tag=file_tag)
        if enabled and business_filename_unique(CUSTOMER["roster"], tag, file_tag) else None)
    if event is not None:
        # A target can be stopped or disabled while a slow browser query runs.
        if customer_stop_requested(CUSTOMER, allow_parent=True):
            state.retire()
        else:
            current = target_watch_enabled(CUSTOMER["roster"], tag, sec_uid)
            if current is False:
                state.retire()
            elif current is None:
                state.pause()
            else:
                def permitted():
                    return (not customer_stop_requested(CUSTOMER, allow_parent=True, strict=True)
                            and target_watch_enabled(CUSTOMER["roster"], tag, sec_uid) is True)
                delivered = deliver_event(state, event, tag, now, _notify, permitted=permitted)
                if delivered:
                    print("[通知] 持续异常%s已发送。" % (
                        "恢复" if event.action == "recovery" else "提醒"))
                else:
                    if customer_stop_requested(CUSTOMER, allow_parent=True):
                        state.retire()
                    else:
                        current = target_watch_enabled(CUSTOMER["roster"], tag, sec_uid)
                        if current is False:
                            state.retire()
                        elif current is None:
                            state.pause()
                    print("[通知] 持续异常通知未送达；仅在目标仍启用时按限频重试。")
    return state.snapshot()


HEAL_COOLDOWN_SECONDS = 90.0   # 通道自愈的最小间隔（多实例并发也各自限频，防反复导航）


def heal_channel(node: str, cdp: str, target_url: str) -> Optional[str]:
    """通道自愈（2026-10-05 实测新增）：定位页面是「假 douyin.com」时自动导航修复。

    实测场景：Chrome 被非正常关闭后重启走崩溃恢复，启动参数里的 URL 被忽略，
    标签页实际停在 about:blank（/json 里 url 仍显示 douyin.com，不可信）→
    页面内 fetch 全部 Failed to fetch，六实例同时报 [需人工协助]。
    导航结果仅说明页面操作是否成功，下一次探测再确认主播状态。

    返回动作描述字符串；页面本身健康返回 None（不动）。
    """
    probe = ('JSON.stringify({href:location.href,ready:document.readyState,'
             'title:document.title})')
    try:
        st = json.loads(cdp_eval(node, cdp, probe, target_url, timeout=20))
        if ("douyin.com" in st.get("href", "")
                and st.get("ready") == "complete" and st.get("title")):
            return None
    except Exception:
        pass  # eval 本身失败（端口死/页面异常）也走修复；goto 再失败由下轮兜底
    if CUSTOMER and not browser_owner_valid(CUSTOMER):
        return "客户浏览器身份待确认，未导航未知浏览器"
    r = subprocess.run([node, cdp, "goto", "https://www.douyin.com"],
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=60, cwd=str(ROOT))
    if r.returncode == 0 and '"url"' in (r.stdout or ""):
        return "已发起监控页面重新导航，开播状态待下轮探测确认"
    return "监控页面重新导航未成功，等下轮探测"


def make_config(web_rid: str, tag: str, llm: bool, template: Path) -> Path:
    """基于现有 config.json 生成只含目标房间的配置（落盘文件名带 tag）。"""
    template = Path(template)
    out = DATA / ("watch_%s.config.json" % tag)
    # Same lock order as board_llm: fresh main read -> derived copy write.
    # Holding the main lock prevents an older derived llm snapshot from being
    # published after the settings transaction has synchronized a newer one.
    with file_lock(template):
        cfg = json.loads(template.read_text(encoding="utf-8"))
        if CUSTOMER:
            if template.resolve() != Path(CUSTOMER["config"]):
                raise ValueError("customer watch template does not match this instance")
            cfg["output_dir"] = CUSTOMER["data"]
        cfg["rooms"] = [{"room_id": web_rid, "label": tag, "llm": llm, "feishu": False}]
        cfg.pop("web_rid", None)
        out.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(out):
            fd, tmp = tempfile.mkstemp(prefix=out.name + ".", suffix=".tmp", dir=out.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, out)
            finally:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
    return out


def start_collector(cfg_path: Path, llm: bool) -> subprocess.Popen:
    if CUSTOMER and customer_stop_requested(CUSTOMER, allow_parent=True):
        raise KeyboardInterrupt
    py = ROOT / ".venv" / "Scripts" / "python.exe"
    exe = CUSTOMER["python"] if CUSTOMER else str(py) if py.exists() else sys.executable
    cmd = [exe, *(["-B"] if installed_layout() is not None else []), str(ROOT / "main.py"), "--config", str(cfg_path)]
    if llm:
        cmd.append("--llm")
    # cfg_path.stem 形如 watch_<标签>.config，去掉 .config 后缀 = SOP 产物名 watch_<标签>.collector.log
    log = open(DATA / (cfg_path.stem.replace(".config", "") + ".collector.log"),
               "a", encoding="utf-8")
    print("[采集] 启动：%s" % " ".join(cmd))
    return subprocess.Popen(
        cmd, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def rebuild_dashboard(timeout: float = 60.0) -> bool:
    """在 watch 退出前有界等待重算，避免后台构建被本轮 Job 一并回收。"""
    dash = str(Path(__file__).resolve().parent / "dashboard_build.py")
    try:
        result = subprocess.run(
            [sys.executable, dash], cwd=str(ROOT), capture_output=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        print("[监控] 看板重算超时（不影响监控）")
        return False
    except OSError as exc:
        print("[监控] 看板重算启动失败（%s，不影响监控）" % type(exc).__name__)
        return False
    if result.returncode != 0:
        print("[监控] 看板重算失败 rc=%s（不影响监控）" % result.returncode)
        return False
    print("[监控] 看板重算完成")
    return True


def plan_collector(started: bool, rc: Optional[int], alive_seconds: float,
                   restarts: int, fast_exits: int, paused_until: float,
                   now: float, fuse: int = COLLECTOR_FUSE,
                   healthy_seconds: float = COLLECTOR_HEALTHY_SECONDS,
                   pause_seconds: float = COLLECTOR_PAUSE_SECONDS,
                   fast_exit_seconds: float = COLLECTOR_FAST_EXIT_SECONDS):
    """本轮该对采集进程做什么（纯函数，零 IO，供单测）。

    为什么要有它：以前这里是 `if args.start and not started:` —— 一次性标志，
    main.py 死掉后再也不会重生，而本进程照常探测、照常打「★开播」，
    看门狗只看本进程活没活 → 「进程活着 + 直播中 + 零数据」可以静默几十分钟。

    :return: (action, restarts, fast_exits, paused_until)
        action:
          ``alive``   上一轮还活着 → 什么都不做
          ``start``   从未起过 → 首次启动
          ``restart`` 上一轮已退出 → 重生
          ``paused``  熔断冷却中 → 继续探测，本轮不起
          ``fuse``    本次触发熔断（调用方发告警），本轮不起
    """
    if started and rc is None:
        # 活着：跑够久就把「刚起就退」计数清零（否则一个陈年计数会一直压着）
        if alive_seconds >= healthy_seconds:
            fast_exits = 0
        return "alive", restarts, fast_exits, paused_until
    if not started:
        return "start", restarts, fast_exits, paused_until
    if now < paused_until:
        return "paused", restarts, fast_exits, paused_until

    restarts += 1
    fast_exits = fast_exits + 1 if alive_seconds < fast_exit_seconds else 0
    if fast_exits >= fuse:
        # 熔断：计数归零 + 冷却一段时间。不永久放弃 —— 配置修好后自动恢复。
        return "fuse", restarts, 0, now + pause_seconds
    return "restart", restarts, fast_exits, paused_until


def main() -> int:
    ap = argparse.ArgumentParser(description="蹲主播开播 → 抓 web_rid → 起采集")
    ap.add_argument("--sec-uid", required=True, help="主播 sec_uid（MS4wLjABAAAA…）")
    ap.add_argument("--tag", default="", help="落盘标签（用 web_rid 时留空则用 room_id）")
    ap.add_argument("--interval", type=float, default=60.0, help="轮询间隔秒（默认 60）")
    ap.add_argument("--start", action="store_true", help="开播后自动启动 main.py 采集")
    ap.add_argument("--llm", action="store_true", help="采集时启用 LLM 层（会花钱）")
    ap.add_argument("--once", action="store_true", help="只查一次就退出")
    ap.add_argument("--max-rounds", type=int, default=0, help="最多轮询多少次（0=不限）")
    ap.add_argument("--node", default=None,
                    help="node 可执行文件；默认按 WATCH_NODE → PATH → 仓内约定探测")
    ap.add_argument("--cdp", default=None,
                    help="cdp.mjs 路径；默认按 WATCH_CDP → 仓内 scripts/cdp.mjs 探测")
    ap.add_argument("--config", default=CUSTOMER["config"] if CUSTOMER else str(ROOT / "config.json"))
    ap.add_argument("--target-url", default="https://www.douyin.com",
                    help="监控浏览器的目标页面，默认定位抖音 www 主页")
    args = ap.parse_args()
    # 路径延迟解析：环境变量 → 仓内自持副本 → PATH / 旧位置兜底
    if installed_layout() is not None:
        try:
            node = resolve_node()
        except NodeExecutableNotFound:
            print("客户安装缺少固定的 Node.js；请检查安装组件。", flush=True)
            return 4
        cdp = resolve_cdp()
        if (args.node and Path(args.node).resolve() != Path(node).resolve()
                or args.cdp and Path(args.cdp).resolve() != Path(cdp).resolve()):
            ap.error("客户安装的 Node.js 或 CDP 脚本路径不匹配")
        args.node, args.cdp = node, cdp
    if not args.node:
        args.node = resolve_node()
    if not args.cdp:
        args.cdp = resolve_cdp()

    tag = args.tag or "watch"
    out_path = DATA / ("watch_%s.jsonl" % tag)

    proc: Optional[subprocess.Popen] = None
    resume_room = os.environ.get("WATCH_RESUME_ROOM_ID")
    prompted = False          # 只在「不自动采集」模式下用于「命令只提示一次」
    offline_streak = 0
    # ---- 采集进程自愈状态（2026-10-29） ----
    # ★ 原实现是 `started` 一次性标志：第一次开播起完 main.py 就再也不管了。
    #   后果 —— main.py 被外部杀掉后，本进程仍每 60s 探测、每轮都打「★开播」，
    #   但**永远不再起采集**；而看门狗只看本进程活没活，于是「进程活着 + 直播中 +
    #   零数据」可以静默持续几十分钟（10-29 20:01 起框框就是这样丢的）。
    #   现在改成每轮检查 proc.poll()，死了就重生。
    collector_started_at = 0.0
    collector_restarts = 0    # 本场累计重启次数（跑够 COLLECTOR_HEALTHY_SECONDS 才清零）
    collector_fast_exits = 0  # 连续「刚起就死」次数，用于熔断防打爆
    collector_paused_until = 0.0  # 熔断冷却截止时刻（过了自动再试）
    collector_file_tag = None

    print("[监控] 目标 sec_uid=%s 间隔=%.0fs 自动采集=%s 状态文件=%s"
          % (args.sec_uid, args.interval, args.start, out_path))

    rnd = 0
    fail_streak = 0
    assist_kind: Optional[str] = None
    announced = False
    last_heal = 0.0
    monitoring_alert = MonitoringAlertState(time.monotonic()) if CUSTOMER else None
    try:
        while True:
            if CUSTOMER and customer_stop_requested(CUSTOMER, allow_parent=True):
                break
            rnd += 1
            try:
                rec = poll_with_fallback(args.node, args.cdp, args.sec_uid, args.target_url, tag)
            except NodeExecutableNotFound as e:
                print("[配置错误] %s" % e)
                return 2
            except Exception as e:
                print("[轮 %d] 轮询失败：%s" % (rnd, e))
                rec = {"ts": int(time.time()), "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "error": str(e), "is_live": False}

            if CUSTOMER and customer_stop_requested(CUSTOMER, allow_parent=True):
                break
            # The installed watcher owns sustained-unknown notifications; its
            # legacy external Health probe is not installed or scheduled here.
            kind = classify_env_failure(rec)
            if monitoring_alert is not None:
                try:
                    rec["monitoring_alert"] = _customer_monitoring_step(
                        monitoring_alert, tag, rec, kind, sec_uid=args.sec_uid,
                        file_tag=collector_file_tag)
                except Exception:
                    # Notification diagnostics never turn an unknown probe into
                    # offline, stop a collector, or expose an upstream exception.
                    rec["monitoring_alert"] = {"status": "check_unavailable"}
            if kind is None:
                if announced:
                    rec["probe_transition"] = "confirmed_again"
                    print("[探测] 本轮已重新确认主播开播状态（轮 %d）。" % rnd)
                fail_streak, announced, assist_kind = 0, False, None
            else:
                if kind != assist_kind:          # 异常类型变了，重新计数
                    fail_streak, assist_kind, announced = 1, kind, False
                else:
                    fail_streak += 1
                if fail_streak >= ASSIST_STREAK and not announced:
                    rec["probe_transition"] = "sustained_unknown"
                    _announce_assist(fail_streak)
                    announced = True
            _append(out_path, rec)
            if kind == "channel" and time.time() - last_heal >= HEAL_COOLDOWN_SECONDS:
                last_heal = time.time()
                action = heal_channel(args.node, args.cdp, args.target_url)
                if action:
                    print("[页面检查] %s" % action)

            if rec.get("is_live"):
                offline_streak = 0
                print("[轮 %d] ★开播 %s | web_rid=%s room_id=%s 在线=%s | %s"
                      % (rnd, rec.get("nick"), rec.get("web_rid"), rec.get("room_id_str"),
                         rec.get("user_count"), rec.get("title")))
                if args.start:
                    # ★ 每轮都检查采集进程死没死（不是一次性标志，见 plan_collector）
                    alive = (time.time() - collector_started_at) if proc is not None else 0.0
                    action, collector_restarts, collector_fast_exits, \
                        collector_paused_until = plan_collector(
                            started=proc is not None,
                            rc=(proc.poll() if proc is not None else None),
                            alive_seconds=alive,
                            restarts=collector_restarts,
                            fast_exits=collector_fast_exits,
                            paused_until=collector_paused_until,
                            now=time.time())

                    if action == "restart":
                        print("[采集] ⚠️ 上一轮采集已退出 rc=%s（存活 %.0fs，"
                              "本场第 %d 次重启）"
                              % (proc.returncode, alive, collector_restarts))
                        _notify(
                            "[竞品监控][采集中断] %s：采集进程退出 rc=%s（存活 %.0fs），"
                                "准备第 %d 次重启"
                            % (tag, proc.returncode, alive, collector_restarts),
                            key="collector-restart-%s" % tag, cooldown=900.0)
                    elif action == "fuse":
                        print("[采集] ⛔ 连续刚起就退，暂停自动重启 %.0fs（探测继续）"
                              % COLLECTOR_PAUSE_SECONDS)
                        _notify(
                            "[竞品监控][需人工处理] %s：采集连续 %d 次刚起就退，"
                            "已暂停自动重启 %.0f 分钟，之后自动再试"
                            % (tag, COLLECTOR_FUSE, COLLECTOR_PAUSE_SECONDS / 60),
                            key="collector-fuse-%s" % tag, cooldown=1800.0)

                    if action in ("start", "restart"):
                        web_rid = str(rec["web_rid"])
                        cfg_path = make_config(web_rid, tag, args.llm, Path(args.config))
                        print("[监控] 已生成配置：%s" % cfg_path)
                        was_restart = proc is not None
                        proc = start_collector(cfg_path, args.llm)
                        collector_file_tag = RoomConfig(room_id=web_rid, label=tag).file_tag()
                        collector_started_at = time.time()
                        # 首次开播才发「★开播」；重生已经在上面发过「采集中断」
                        if not was_restart and resume_room and resume_room == rec.get("room_id_str"):
                            print("[监控] 维护前已确认同一场直播，本次不重复推送开播消息。")
                        elif not was_restart:
                            _notify(
                                "[竞品监控] ★开播 %s | %s | web_rid=%s，采集进程已启动"
                                % (rec.get("nick"), rec.get("title") or "无标题",
                                   web_rid),
                                key="live-%s" % tag, cooldown=600.0)
                        resume_room = None
                elif not prompted:
                    print("[监控] 采集命令（自取）：")
                    print("    .venv\\Scripts\\python.exe main.py --room %s%s"
                          % (rec.get("web_rid"), "  # 注意：--room 落盘文件名不带 tag"))
                    print("    或（带 tag，推荐）：先写 rooms 再 python main.py --config <cfg>")
                    prompted = True  # 只提示一次
            elif not is_confirmed_offline(rec, kind):
                # ★ 探测异常（登录态/通道坏）→ 本轮结果无效，不判定开播状态。
                #   offline_streak 不动、采集器不动：下播只能是「有效响应里
                #   明确未在播」，不能是「我们没探到」（2026-11-01 审计 P0）。
                print("[轮 %d] 探测异常（%s），本轮不判定开播状态；采集器%s"
                      % (rnd, kind, "保持运行" if proc is not None else "未启动"))
            elif proc is not None and args.start:
                offline_streak += 1
                print("[轮 %d] 未在播（连续 %d 次）" % (rnd, offline_streak))
                if offline_streak >= 2:
                    print("[监控] 已下播，停止采集。")
                    _notify("[竞品监控] 已确认下播，准备结束采集：%s" % tag,
                                     key="offline-%s" % tag, cooldown=600.0)
                    break
            else:
                print("[轮 %d] 未开播 live_status=%s nick=%s"
                      % (rnd, rec.get("live_status"), rec.get("nick")))

            if args.once:
                break
            if args.max_rounds and rnd >= args.max_rounds:
                break
            # 等待人工协助期间降频重试（风控恢复需要时间，60s 打一次反而刺激风控）
            delay = max(args.interval, ASSIST_RETRY_SECONDS) if announced else args.interval
            if CUSTOMER:
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline:
                    if customer_stop_requested(CUSTOMER, allow_parent=True):
                        raise KeyboardInterrupt
                    time.sleep(min(.25, max(0, deadline - time.monotonic())))
            else:
                time.sleep(delay)
    except KeyboardInterrupt:
        print("\n[监控] Ctrl+C，退出。")
    finally:
        if monitoring_alert is not None:
            monitoring_alert.retire()
        if proc is not None:
            print("[监控] 终止采集进程 pid=%s" % proc.pid)
            if CUSTOMER and customer_stop_requested(CUSTOMER, allow_parent=True):
                # main sees the same exact ancestor request and runs rt.stop().
                while proc.poll() is None:
                    time.sleep(.25)
            else:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except Exception:
                    proc.kill()
        # 2026-10-07：下播后重算看板（弹幕已 flush 完；失败只打日志不拖垮监控）
        if not CUSTOMER:
            rebuild_dashboard()
    return 0


if __name__ == "__main__":
    sys.exit(main())
