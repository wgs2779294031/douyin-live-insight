# -*- coding: utf-8 -*-
"""飞书 IM 告警推送（2026-10-05）：监控故障/开播事件推一条消息给老大。

为什么不用 feishu_sink：那是「弹幕落多维表格」的通道（31 列契约 + 行聚合），
告警只需要一条 IM 文本，走 `/open-apis/im/v1/messages`。

凭据：config.json 的 `feishu` 节——`app_id` / `app_secret` 复用落盘通道的
同一个自建应用，接收目标二选一：
- `alert_chat_id`：接收告警的会话 ID（群聊/单聊均可）；
- `alert_open_id`：直接私聊某个用户（机器人主动发，无需建群），chat_id 留空时用它。
- `alert_chat_id` 留空时，`list_chats()` 可列出该应用所在的会话供挑选。
- 缺凭据 / 发送失败 → 只打一行日志，**绝不影响监控主流程**。
- 进程内限频：同一 `key` 在 cooldown 秒内只发一次（多主播同时异常时不刷屏）。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Dict, Optional

ROOT = Path(__file__).resolve().parents[1]

_last_sent: Dict[str, float] = {}   # key -> ts（进程内限频，跨进程不保证）

# 恒直连 opener（2026-10-07 真机坑）：urllib 默认吃系统代理，老大开 Clash 系统代理
# 且代理端口没响应时，飞书推送会报 WinError 10061「积极拒绝」。飞书是国内站，
# 告警通道不应依赖代理开关，故显式绕过一切系统代理。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _load_feishu_cfg(config_path: Optional[str] = None):
    path = Path(config_path) if config_path else ROOT / "config.json"
    f = (json.loads(path.read_text(encoding="utf-8")).get("feishu") or {})
    return (f.get("app_id") or "", f.get("app_secret") or "",
            f.get("api_base") or "https://open.feishu.cn",
            f.get("alert_chat_id") or "", f.get("alert_open_id") or "")


def _get_token(app_id: str, app_secret: str, base: str,
               timeout: float = 10.0) -> str:
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    req = urllib.request.Request(
        base + "/open-apis/auth/v3/tenant_access_token/internal",
        data=body, headers={"Content-Type": "application/json"})
    d = json.load(_OPENER.open(req, timeout=timeout))
    if d.get("code") != 0:
        raise RuntimeError("获取 token 失败 code=%s msg=%s"
                           % (d.get("code"), d.get("msg")))
    return str(d["tenant_access_token"])


def list_chats(config_path: Optional[str] = None) -> str:
    """列出应用所在的会话（chat_id + 名称），供首次配置 alert_chat_id 用。"""
    app_id, app_secret, base, _, _ = _load_feishu_cfg(config_path)
    if not (app_id and app_secret):
        return "config.json 的 feishu 节缺 app_id/app_secret，先填凭据"
    tok = _get_token(app_id, app_secret, base)
    req = urllib.request.Request(
        base + "/open-apis/im/v1/chats?page_size=50",
        headers={"Authorization": "Bearer " + tok})
    d = json.load(_OPENER.open(req, timeout=10.0))
    items = (d.get("data") or {}).get("items") or []
    if not items:
        return "应用不在任何会话里——先在飞书里给这个应用发一条消息（激活单聊）或拉它进群"
    return "\n".join("%s  %s" % (c.get("chat_id"), c.get("name") or "(单聊)")
                     for c in items)


def send_text(text: str, key: Optional[str] = None, cooldown: float = 600.0,
              config_path: Optional[str] = None) -> bool:
    """发文本消息到 `alert_chat_id`。返回是否真的发出（False=未配置/限频/失败）。

    key + cooldown：同一 key 在 cooldown 秒内只发一次（进程内）。

    2026-10-15：新增群自定义机器人 webhook 通道（feishu.webhook_url）。
    背景：自建应用被企业访问控制拦（21001），而自定义机器人无需应用发布/
    权限审批。webhook 已配置时优先走它，未配置回落原自建应用通道。
    """
    try:
        f = (json.loads(Path(config_path or (ROOT / "config.json"))
                        .read_text(encoding="utf-8")).get("feishu") or {})
        webhook = str(f.get("webhook_url") or "").strip()
        if webhook:
            now = time.time()
            k = key or text[:40]
            if now - _last_sent.get("wh:" + k, 0.0) < cooldown:
                return False
            body = json.dumps({"msg_type": "text",
                               "content": {"text": text}},
                              ensure_ascii=False).encode()
            req = urllib.request.Request(
                webhook, data=body,
                headers={"Content-Type": "application/json"})
            d = json.load(_OPENER.open(req, timeout=10.0))
            if d.get("code") != 0 and d.get("StatusCode") != 0:
                raise RuntimeError("webhook 发送失败 code=%s msg=%s"
                                   % (d.get("code"), d.get("msg")))
            _last_sent["wh:" + k] = now
            return True
        app_id, app_secret, base, chat_id, open_id = _load_feishu_cfg(config_path)
        if not (app_id and app_secret and (chat_id or open_id)):
            return False
        now = time.time()
        k = key or text[:40]
        if now - _last_sent.get(k, 0.0) < cooldown:
            return False
        tok = _get_token(app_id, app_secret, base)
        # 优先 chat_id；只有 open_id 时走机器人私聊（receive_id_type=open_id）
        receive_id, id_type = ((chat_id, "chat_id") if chat_id
                               else (open_id, "open_id"))
        body = json.dumps({
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }).encode()
        req = urllib.request.Request(
            base + "/open-apis/im/v1/messages?receive_id_type=" + id_type,
            data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + tok})
        d = json.load(_OPENER.open(req, timeout=10.0))
        if d.get("code") != 0:
            raise RuntimeError("发消息失败 code=%s msg=%s"
                               % (d.get("code"), d.get("msg")))
        _last_sent[k] = now
        print("[告警] 已推送飞书：%s" % text[:60], flush=True)
        return True
    except Exception as e:                      # 推送失败绝不拖垮监控
        print("[告警] 飞书推送失败：%s" % e, flush=True)
        return False


if __name__ == "__main__":
    # 用法：python scripts/notify.py chats   -> 列会话
    #      python scripts/notify.py test    -> 发一条测试消息
    action = sys.argv[1] if len(sys.argv) > 1 else "chats"
    if action == "chats":
        print(list_chats())
    elif action == "test":
        ok = send_text("[竞品监控] 告警通道测试：能看到这条说明推送已打通",
                       key="manual-test")
        print("已发送" if ok else "未发出（看上面日志：缺凭据/未配 chat_id/失败）")
