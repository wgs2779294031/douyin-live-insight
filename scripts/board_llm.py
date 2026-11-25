# -*- coding: utf-8 -*-
"""LLM 状态读写（P5，2026-10-23）。

读（`read_status`）：给设置页用的只读快照。存在的理由很具体 —— 设置页
`web/src/views/Settings.vue` 早就在调 `/api/llm`，后端一直没这个路由，页面每次打开
都是一条 404（10-23 用 `web/tools/shot.mjs` 截图时抓到的真实报错）。

写（本文件后半段）：运行时闸门 + 启动期开关 + 换 key。分成两层是因为它们的生效
方式完全不同：

- **运行时总控制** `data/llm_runtime.json`：实际发送前fresh读取，暂停覆盖所有内置
  模型用途；显式允许不等于开启任何主播自动分析。
- **配置默认许可** `config.json → llm.enabled`：没有runtime enabled时的默认总许可。
  标准loader绑定主配置，副本不再拥有独立控制权；worker按当前许可热启停。

三条纪律：
1. **绝不抛异常**（读路径）。设置页不能因为 LLM 数据不全就白屏。
2. **密钥只返回是否存在**。任何接口不返回明文 key、尾号或长度。
3. 写 config 一律 **原子写 + 先备份 .bak**：这台机器上唯一带 key 的文件，写坏了
   比开关失效严重得多。
"""
from __future__ import annotations

import json
import copy
import math
import http.client
import ipaddress
import os
import re
import socket
import ssl
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from urllib.parse import unquote, urlsplit

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from file_lock import file_lock
from llm_client import application_control

_BUSINESS_FIELDS = ("prompt_tokens", "completion_tokens")


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0


def _key_tail(k):
    # Keep the old response field for clients, without exposing even short keys.
    return ""


_PROVIDER_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_PROVIDER_TEST_TIMEOUT = 8.0
_TEST_LOCKS = {}
_TEST_LOCKS_GUARD = threading.Lock()


def _has_controls(value):
    return any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)


def _plain_field(value, limit, *, empty=False):
    if not isinstance(value, str) or len(value) > limit or _has_controls(value):
        raise ValueError("invalid_model_settings")
    value = value.strip()
    if not empty and not value:
        raise ValueError("invalid_model_settings")
    return value


def _provider_name(value):
    value = _plain_field(value, 64)
    if not _PROVIDER_NAME.fullmatch(value):
        raise ValueError("invalid_model_settings")
    return value


def _provider_protocol(name, value=None):
    if value is None:
        return "doubao" if name == "doubao" else "openai_compatible"
    if value not in ("doubao", "openai_compatible"):
        raise ValueError("invalid_model_settings")
    return value


def _provider_url(value):
    value = _plain_field(value, 2048)
    if (any(char.isspace() for char in value) or "\\" in value
            or "?" in value or "#" in value or _has_controls(unquote(value))):
        raise ValueError("invalid_model_settings")
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
        if (parts.scheme not in ("http", "https") or not host
                or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or parts.netloc.endswith(":")):
            raise ValueError
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
            ascii_host = host.encode("idna").decode("ascii")
            if not re.fullmatch(r"[A-Za-z0-9.-]+", ascii_host):
                raise ValueError
        if parts.scheme == "http" and not (host.lower() == "localhost"
                                           or address is not None and address.is_loopback):
            raise ValueError
    except (TypeError, ValueError, UnicodeError):
        raise ValueError("invalid_model_settings") from None
    return value.rstrip("/")


def _provider_key(value):
    value = _plain_field(value, 4096, empty=True)
    if value and (not value.isascii() or any(char.isspace() for char in value)):
        raise ValueError("invalid_model_settings")
    return value


def _provider_draft(body):
    fields = {"provider", "protocol", "base_url", "model", "api_key"}
    required = {"provider", "protocol", "base_url", "model"}
    if (not isinstance(body, dict) or set(body) - fields or not required <= set(body)
            or body.get("protocol") not in ("doubao", "openai_compatible")):
        raise ValueError("invalid_model_settings")
    name = _provider_name(body.get("provider"))
    return {"provider": name, "protocol": _provider_protocol(name, body.get("protocol")),
            "base_url": _provider_url(body.get("base_url")),
            "model": _plain_field(body.get("model"), 256),
            "api_key": _provider_key(body.get("api_key", ""))}


def _safe_display(value, limit, secret=""):
    try:
        value = _plain_field(value, limit, empty=True)
    except ValueError:
        return ""
    return "" if secret and secret in value else value


def _saved_provider_key(entry):
    if not isinstance(entry, dict):
        return ""
    return entry.get("api_key") or entry.get("key") or ""


def _secret_embedded(settings, secret):
    if not isinstance(secret, str) or not secret:
        return False
    for field in ("provider", "model"):
        value = settings.get(field, "")
        if secret == value or len(secret) > 4 and secret in value:
            return True
    return secret in unquote(settings.get("base_url", ""))


def _provider_secrets(config, extra=""):
    """All present/legacy credentials are confidential across provider names."""
    values = [extra]
    llm = config.get("llm") if isinstance(config, dict) else None
    if isinstance(llm, dict):
        values.extend(llm.get(field) for field in ("api_key", "key"))
        providers = llm.get("providers")
        if isinstance(providers, dict):
            for entry in providers.values():
                if isinstance(entry, dict):
                    values.extend(entry.get(field) for field in ("api_key", "key"))
    return {value.strip() for value in values if isinstance(value, str) and value.strip()}


def _remember_provider_secrets(config_path, secrets):
    """Keep observed credentials private across edits, within this source only."""
    from llm_client import canonical_llm_source, source_secret_set
    known = source_secret_set(canonical_llm_source(config_path))
    known.update(secrets)
    return known.copy()


def _matches_secret(value, secrets, *, url=False):
    if not isinstance(value, str):
        return False
    value = unquote(value) if url else value
    return any(secret == value or (url or len(secret) > 4) and secret in value
               for secret in secrets)


def _public_metadata(config):
    llm = config.get("llm") if isinstance(config, dict) else None
    if not isinstance(llm, dict):
        return []
    values = [(llm.get("active_provider"), False)]
    fields = ("model", "base_url", "label", "owner", "protocol")
    values.extend((llm.get(field), field == "base_url") for field in fields)
    providers = llm.get("providers")
    if isinstance(providers, dict):
        for name, entry in providers.items():
            values.append((name, False))
            if isinstance(entry, dict):
                values.extend((str(entry[field]), field == "base_url")
                              for field in fields if field in entry)
    prices = llm.get("cost_estimates")
    if isinstance(prices, dict) and isinstance(prices.get("entries"), list):
        for entry in prices["entries"]:
            if isinstance(entry, dict):
                values.extend((value, False) for value in entry.values() if isinstance(value, str))
    return values


def _metadata_has_secret(config, secrets):
    return any(_matches_secret(value, secrets, url=url)
               for value, url in _public_metadata(config))


def _public_status(value, secrets, field=""):
    """Redact public values and dynamic metric identities, never key fragments."""
    if isinstance(value, dict):
        return {key: _public_status(item, secrets, key)
                for key, item in value.items()
                if field != "by_provider" or not _matches_secret(key, secrets)}
    if isinstance(value, list):
        return [_public_status(item, secrets, field) for item in value]
    if isinstance(value, str) and _matches_secret(value, secrets, url=field == "base_url"):
        return ""
    return value


def _runtime_gate(data_dir):
    """闸门文件优先于 config.json：改了秒生效，不用重启（也不受 *副本* 影响）。"""
    from llm_client import runtime_gate
    raw = runtime_gate(data_dir, fresh=True)
    if "enabled" in raw:
        return raw["enabled"], "runtime"
    return None, None


def _metrics_today(data_dir, date):
    """Actual SDK-send day, including records written after midnight."""
    from board_usage import read_usage
    stat = read_usage(data_dir, date)
    stat["date"] = date  # Existing clients may still read this compatibility field.
    stat["total_tokens"] = (stat["totals"]["known_prompt_tokens"]
                            + stat["totals"]["known_completion_tokens"])
    stat["probe_calls"] = stat["diagnostics"]["probe_records"]
    stat["failed"] = stat["totals"]["failed_records"]
    return stat


def _budget(data_dir, provider, date):
    """预算账本：主账无 provider 后缀（`llm_budget_<date>.json`），备用账才有。

    2026-10-23 踩过：只找带 provider 的文件名 → 主账永远读不到，
    设置页的「已用」一直是 0。
    """
    for fn in ("llm_budget_%s.json" % date,
               "llm_budget_%s_%s.json" % (provider, date)):
        raw = _load_json(os.path.join(data_dir, fn))
        if isinstance(raw, dict):
            total = _num(raw.get("total"))
            cap = _num(raw.get("budget"))
            return {"used_tokens": total, "cap_tokens": cap,
                    "ratio": round(total / cap, 4) if cap > 0 else None}
    return None


def read_status(data_dir: str, config_path: str, date: str) -> dict:
    """给设置页看的 LLM 现状。任何缺文件都不抛异常，用 `errors` 说明缺什么。"""
    errors = []
    out = {
        "available": False,
        "enabled": False,
        "enabled_source": "default",
        "active_provider": "",
        "providers": [],
        "today": {"date": date, "calls": 0, "probe_calls": 0, "failed": 0,
                  "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                  "by_provider": {}},
        "budget": None,
        "flags": {},
        "note": "总控制决定是否允许新的内置AI调用；自动实况还需逐主播明确开启。保存密钥不会自动调用。",
        "control": application_control(config_path, data_dir),
    }

    cfg = _load_json(config_path)
    out["enabled"] = out["control"]["allowed"]
    secrets = _remember_provider_secrets(config_path, _provider_secrets(cfg))
    llm = (cfg or {}).get("llm") if isinstance(cfg, dict) else None
    if not isinstance(llm, dict):
        errors.append("读不到 config.json 的 llm 段（文件缺失或格式不对）")
        # 配置缺失也要如实回报闸门状态：这两个东西是独立的
        gate, src = _runtime_gate(data_dir)
        out["enabled_source"] = src or "default"
        out["config_enabled"] = False
        out["today"] = _metrics_today(data_dir, date)
        out["errors"] = errors
        return _public_status(out, secrets)

    out["available"] = True
    cfg_enabled = bool(llm.get("enabled"))
    gate, src = _runtime_gate(data_dir)
    out["enabled_source"] = src or "config"
    # 兼容诊断字段：配置默认值不是worker运行证明，当前许可看control。
    out["config_enabled"] = cfg_enabled
    out["active_provider"] = str(llm.get("active_provider") or "")

    providers = llm.get("providers")
    if isinstance(providers, dict):
        for name, p in providers.items():
            p = p if isinstance(p, dict) else {}
            key = _saved_provider_key(p)
            key = key if isinstance(key, str) else ""
            try:
                public_url = _provider_url(p.get("base_url") or "")
                if key and key in unquote(public_url):
                    public_url = ""
            except ValueError:
                public_url = ""
            try:
                protocol = _provider_protocol(name, p.get("protocol"))
            except ValueError:
                protocol = "openai_compatible"
            out["providers"].append({
                "name": _safe_display(name, 64, key),
                "protocol": protocol,
                "model": _safe_display(p.get("model") or "", 256, key),
                "base_url": public_url,
                "label": _safe_display(p.get("label") or "", 128, key),
                "owner": _safe_display(p.get("owner") or "", 128, key),
                "has_key": bool(key),
                "key_tail": _key_tail(key),
                "active": name == out["active_provider"],
            })
    else:
        errors.append("config.json 的 llm.providers 不是字典")

    out["today"] = _metrics_today(data_dir, date)
    out["budget"] = _budget(data_dir, out["active_provider"] or "default", date)
    if out["budget"] is None:
        out["budget"] = {"used_tokens": out["today"]["total_tokens"],
                         "cap_tokens": _num(llm.get("daily_token_budget")),
                         "ratio": None}
    # config 里的值是「目标值」，账本里的 cap 是「进程启动时写死、当前真正在生效」的值。
    # 2026-10-25 起前端可改护栏：改完账本不会跟着变（账本是采集进程建的），
    # 所以两个数都要给出来，页面才不会出现"改了没反应"。
    cfg_cap = _num(llm.get("daily_token_budget"))
    out["budget"]["config_cap_tokens"] = cfg_cap
    out["budget"]["pending"] = (cfg_cap or 0) != (out["budget"].get("cap_tokens") or 0)

    out["flags"] = {
        "coldline_enabled": bool(llm.get("coldline_enabled")),
        "recommend_enabled": bool(llm.get("recommend_enabled")),
        "metrics_enabled": bool(llm.get("metrics_enabled")),
        "daily_token_budget": _num(llm.get("daily_token_budget")),
        "portrait_interval_seconds": _num(llm.get("portrait_interval_seconds")),
    }
    # 用途维度开关（2026-10-25）：realtime=实时台词辅助，review=对外复盘建议。
    # 没配 = 都算开（旧行为）。
    raw_gate = _load_json(gate_path(data_dir))
    pur = raw_gate.get("purposes") if isinstance(raw_gate, dict) else None
    out["purposes"] = {
        "realtime": True if not isinstance(pur, dict) else bool(pur.get("realtime", True)),
        "review": True if not isinstance(pur, dict) else bool(pur.get("review", True)),
    }
    # 单主播开关：这些 tag 的 LLM 调用会被 LLMRouter 直接跳过（秒生效）。
    out["tags_off"] = ([str(x) for x in (raw_gate.get("tags_off") or [])]
                       if isinstance(raw_gate, dict) else [])
    out["tags_on"] = ([str(x) for x in (raw_gate.get("tags_on") or [])]
                      if isinstance(raw_gate, dict) else [])
    out["errors"] = errors
    return _public_status(out, secrets)


# ================================================================ 写（P5）
# 1. 原子写（tmp + os.replace），主 config.json 先备份 .bak。
# 2. 只改 llm 段，config 里其余段一个字节都不碰。
# 3. 副本必须同步：六路 watch 读 `data/watch_<tag>.config.json`，改主配置传不过去。

def _atomic_write_json(path, obj, backup=False):
    """同目录 tmp → fsync →（可选）备份 → os.replace。半截文件永远不会出现在原位。"""
    import shutil
    path = os.fspath(path)
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if backup and os.path.isfile(path):
            # This is the recovery copy of the private main configuration.
            # A failed backup must prevent replacing that original.
            shutil.copy2(path, path + ".bak")
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    return path


def _now_iso():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def gate_path(data_dir):
    return os.path.join(data_dir, "llm_runtime.json")


def write_gate(data_dir, enabled=None, active_provider=None, actor="board",
               purposes=None, tag_off=None):
    """写运行时闸门（秒生效，不需要重启任何进程）。

    :param purposes: {"realtime": bool, "review": bool} —— 用途维度开关，与全局
        enabled 是**与**关系（enabled=false 时全关）。缺省不动已有配置。
    :param tag_off: (tag, bool) —— 单主播自动许可。关闭移出tags_on并递增该主播
        暂停代际；开启显式加入tags_on。此开关不限制人工复盘。
    """
    path = gate_path(data_dir)
    with file_lock(path):
        cur = _write_gate_unlocked(path, enabled, active_provider, actor, purposes, tag_off)
    # 立刻让同进程内的 2s 缓存失效，别让读接口自己打自己的脸
    try:
        import llm_client
        llm_client.invalidate_gate_cache()
    except Exception:
        pass
    return cur


def _write_gate_unlocked(path, enabled, active_provider, actor, purposes, tag_off):
    cur = _load_json(path)
    if cur is None and not os.path.exists(path):
        cur = {}
    if not isinstance(cur, dict):
        raise ValueError("运行时闸门损坏，保留原文件并拒绝覆盖")
    def advance(name, identity=None):
        if identity is None:
            old = cur.get(name, 0)
            if type(old) is not int or old < 0:
                raise ValueError("运行时暂停代际损坏，保留原文件")
            cur[name] = old + 1
        else:
            values = cur.get(name, {})
            if not isinstance(values, dict) or any(type(v) is not int or v < 0 for v in values.values()):
                raise ValueError("运行时暂停代际损坏，保留原文件")
            cur[name] = dict(values, **{identity: values.get(identity, 0) + 1})
    if enabled is not None:
        if type(enabled) is not bool:
            raise ValueError("enabled必须是布尔值")
        cur["enabled"] = enabled
        if not enabled:
            advance("pause_generation")
    if active_provider is not None:
        cur["active_provider"] = str(active_provider)
    if purposes:
        p = cur.get("purposes")
        p = p if isinstance(p, dict) else {}
        p.update({str(k): bool(v) for k, v in purposes.items()})
        cur["purposes"] = p
        for name, allowed in purposes.items():
            if allowed is False:
                advance("purpose_pause_generation", str(name))
    if tag_off:
        tag, off = str(tag_off[0]), bool(tag_off[1])
        cur_tags = [str(x) for x in (cur.get("tags_off") or []) if x]
        if off and tag not in cur_tags:
            cur_tags.append(tag)
        elif not off:
            cur_tags = [x for x in cur_tags if x != tag]
        cur["tags_off"] = cur_tags
        on_tags = [str(x) for x in (cur.get("tags_on") or []) if x]
        if off:
            on_tags = [x for x in on_tags if x != tag]
            advance("tag_pause_generation", tag)
        elif tag not in on_tags:
            on_tags.append(tag)
        cur["tags_on"] = on_tags
    cur["updated_at"] = _now_iso()
    cur["updated_by"] = actor
    _atomic_write_json(path, cur)
    return cur


def read_tags_off(data_dir):
    """单主播关闭名单（闸门 tags_off）。"""
    g = _load_json(gate_path(data_dir))
    if not isinstance(g, dict):
        return []
    return [str(x) for x in (g.get("tags_off") or []) if x]


def automatic_status(data_dir, config_path, tag, *, is_live=None, identity_reader=None):
    """Read intent and a bounded, current process-owned worker receipt separately."""
    from llm_client import runtime_gate
    gate = runtime_gate(data_dir, fresh=True)
    enabled = tag in (gate.get("tags_on") or []) and tag not in (gate.get("tags_off") or [])
    control = application_control(config_path, data_dir, tag=tag, automatic=True)
    intent = {"enabled": enabled, "effective": control["allowed"], "reason": control["reason"]}
    worker = {"state": "waiting_collector", "reason": "no_worker_receipt", "worker_alive": False,
              "collector_alive": False}
    try:
        with open(os.path.join(data_dir, "llm_worker_%s.json" % tag), "rb") as handle:
            raw = handle.read(16385)
        if len(raw) > 16384:
            raise ValueError
        record = json.loads(raw)
        now = time.time()
        pid, created, observed = record.get("pid"), record.get("process_created_at"), record.get("observed_at")
        if (type(pid) is not int or pid <= 0 or record.get("room_tag") != tag
                or any(type(n) not in (int, float) or not math.isfinite(n) for n in (created, observed))
                or not created <= observed <= now + 1 or now - observed > 10
                or type(record.get("worker_alive")) is not bool
                or type(record.get("collector_alive")) is not bool
                or type(record.get("generation")) is not int or record["generation"] < 0
                or type(record.get("live_ready")) is not bool
                or type(record.get("live_generation")) is not int or record["live_generation"] < 0):
            raise ValueError
        epoch = record.get("epoch")
        if not (epoch is None and not record["worker_alive"]):
            if not isinstance(epoch, list) or len(epoch) != 3 or any(type(x) is not int or x < 0 for x in epoch):
                raise ValueError
        if identity_reader is None:
            from anchor_manager import process_create_time
            identity_reader = process_create_time
        actual = identity_reader(pid)
        if actual is None or abs(actual - created) > .001:
            raise ValueError
        states = {"starting", "running", "waiting_input", "stopping", "waiting_permission", "no_key",
                  "waiting_collector", "waiting_live", "stopped", "error"}
        if record.get("state") not in states:
            raise ValueError
        if (record["worker_alive"] and (type(record.get("thread_ident")) is not int
                                        or type(record.get("thread_native_id")) is not int
                                        or type(record.get("worker_live_generation")) is not int
                                        or record["worker_live_generation"] < 0)):
            raise ValueError
        worker = {key: record.get(key) for key in ("state", "reason", "worker_alive", "collector_alive",
                  "pid", "process_created_at", "observed_at", "generation", "thread_ident", "thread_native_id", "epoch",
                  "live_ready", "live_generation", "worker_live_generation")}
        reasons = {"not_started", "collection_stopped", "collector_not_running", "no_credentials",
                   "worker_retry_wait", "worker_starting", "worker_running", "worker_waiting_input",
                   "worker_stopped", "worker_error", "configuration_unavailable", "control_epoch_changed",
                   "legacy_memory_config", "global_paused", "control_unavailable", "automatic_disabled",
                   "purpose_paused", "allowed", "live_ready", "live_unknown", "not_live",
                   "waiting_connection", "live_generation_changed"}
        if worker["reason"] not in reasons:
            worker["reason"] = "unavailable"
        if worker["worker_alive"] and worker["epoch"] != control["epoch"]:
            worker["state"], worker["reason"] = "stopping", "control_epoch_changed"
        elif worker["worker_alive"] and (not worker["live_ready"]
                                          or worker["live_generation"] != worker["worker_live_generation"]):
            worker["state"], worker["reason"] = "stopping", "live_generation_changed"
        elif not control["allowed"] and worker["state"] in ("starting", "running", "waiting_input"):
            worker["state"] = "stopping" if worker["worker_alive"] else "waiting_permission"
            worker["reason"] = control["reason"]
        elif is_live is False and worker["state"] in ("running", "waiting_input"):
            worker["state"], worker["reason"] = "waiting_live", "room_offline"
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        worker = {"state": "unknown", "reason": "unverified_worker", "worker_alive": False,
                  "collector_alive": False}
    return {"automatic_analysis": intent, "worker_state": worker}


def watch_config_paths(data_dir):
    import glob
    return sorted(glob.glob(os.path.join(data_dir, "watch_*.config.json")))


def sync_llm_config(config_path, data_dir, enabled=None, providers=None,
                    budget=None, sync_copies=True):
    """把 llm 段写回 config.json（备份 .bak）并同步到各路 watch 副本。

    :param enabled: 非 None 时改 llm.enabled（无runtime enabled时的默认总许可）
    :param providers: 非 None 时整体替换 llm.providers（换 key / 加 provider 用）
    :param budget: 非 None 时改 llm.daily_token_budget（日 token 护栏；0=不设限）
    """
    # Keep the main lock through all copies: a later edit must not publish its
    # copies before this transaction finishes publishing an older llm snapshot.
    with file_lock(config_path):
        return _sync_llm_config_unlocked(config_path, data_dir, enabled, providers,
                                         budget, sync_copies)


def _sync_llm_config_unlocked(config_path, data_dir, enabled, providers,
                              budget, sync_copies):
    cfg = _load_json(config_path)
    if not isinstance(cfg, dict):
        raise RuntimeError("config.json 读不出来：%s" % config_path)
    llm = cfg.get("llm")
    if not isinstance(llm, dict):
        llm = {}
    if enabled is not None:
        llm["enabled"] = bool(enabled)
    if providers is not None:
        llm["providers"] = providers
    if budget is not None:
        llm["daily_token_budget"] = max(0, int(budget))
    cfg["llm"] = llm
    copies = []
    with ExitStack() as copy_locks:
        paths = sorted(watch_config_paths(data_dir)) if sync_copies else []
        for p in paths:
            copy_locks.enter_context(file_lock(p))
        prepared = []
        # Validate every enumerated copy before publishing anything. Keep all
        # copy locks until the transaction finishes, so validation stays valid.
        for p in paths:
            sub = _load_json(p)
            if not isinstance(sub, dict):
                raise RuntimeError("watch 配置副本无法读取或已损坏，未开始同步: %s" % p)
            sub["llm"] = llm
            prepared.append((p, sub))
        _atomic_write_json(config_path, cfg, backup=True)
        # Publication spans files: an I/O failure can leave partial progress.
        # Propagate it explicitly; never report a partial synchronization as OK.
        for p, sub in prepared:
            _atomic_write_json(p, sub)
            copies.append(p)

    return {"config": config_path, "copies": copies,
            "enabled": bool(llm.get("enabled")),
            "budget": _num(llm.get("daily_token_budget")), "count": len(copies)}


def _settings_result(code, saved=False, total=0, synced=0, *, deleting=False):
    messages = {
        "ok": "密钥已删除并同步。" if deleting else "模型配置已保存并同步。",
        "invalid": "配置字段不正确，请检查协议、地址和模型。",
        "config_error": "配置文件无法读取，未保存。",
        "storage_error": "配置保存未完成，请刷新核对后重试。",
        "partial_sync": "主配置已保存，部分监控配置尚未同步，请重试。",
        "provider_not_found": "未找到该提供商配置，未删除密钥。",
    }
    message = messages[code]
    result = {"ok": code == "ok", "code": code, "config_saved": bool(saved),
              "sync": {"total": total, "synced": synced, "failed": total - synced},
              "message": message}
    if not result["ok"]:
        result["error"] = message
    return result


def _provider_sections(config):
    if not isinstance(config, dict):
        raise ValueError("invalid_saved_config")
    llm = config.get("llm", {})
    if not isinstance(llm, dict):
        raise ValueError("invalid_saved_config")
    providers = llm.get("providers", {})
    if not isinstance(providers, dict):
        raise ValueError("invalid_saved_config")
    return llm, providers


def _write_confirmed(path, value):
    try:
        _atomic_write_json(path, value, backup=True)
    except Exception:
        # A replace can succeed before cleanup fails. Report verified contents,
        # rather than falsely claiming that an already-published edit was lost.
        pass
    return _load_json(path) == value


def _persist_provider(config_path, data_dir, name, draft=None, *, deleting=False):
    total = synced = 0
    saved = False
    try:
        with file_lock(config_path):
            config = _load_json(config_path)
            try:
                llm, providers = _provider_sections(config)
            except ValueError:
                return _settings_result("config_error", deleting=deleting)
            current = providers.get(name)
            if current is not None and not isinstance(current, dict):
                return _settings_result("config_error", deleting=deleting)
            if deleting and current is None:
                return _settings_result("provider_not_found", deleting=True)
            entry = copy.deepcopy(current or {})
            secrets = _provider_secrets(config, draft["api_key"] if draft else "")
            _remember_provider_secrets(config_path, secrets)
            if draft and draft["api_key"] and _metadata_has_secret(config, {draft["api_key"]}):
                return _settings_result("invalid")
            if deleting:
                entry.pop("api_key", None)
                entry.pop("key", None)
            else:
                entry.update({field: draft[field] for field in ("protocol", "base_url", "model")})
                try:
                    secret = _provider_key(draft["api_key"] or _saved_provider_key(entry))
                except ValueError:
                    return _settings_result("config_error")
                if secret:
                    entry["api_key"] = secret
                    entry.pop("key", None)
                if any(_secret_embedded(draft, value) for value in secrets):
                    return _settings_result("invalid")
            providers[name] = entry
            llm["providers"] = providers
            config["llm"] = llm
            if _metadata_has_secret(config, secrets):
                return _settings_result("invalid", deleting=deleting)
            paths = sorted(watch_config_paths(data_dir))
            total = len(paths)
            if not _write_confirmed(config_path, config):
                return _settings_result("storage_error", total=total, deleting=deleting)
            saved = True
            # Keep the canonical lock through copy publication. Only this provider
            # is changed; per-room/global gates, budgets and active choice stay put.
            for path in paths:
                try:
                    with file_lock(path):
                        sub = _load_json(path)
                        sub_llm, sub_providers = _provider_sections(sub)
                        if deleting:
                            sub_entry = sub_providers.get(name)
                            if sub_entry is not None and not isinstance(sub_entry, dict):
                                continue
                            if sub_entry is not None:
                                sub_entry = copy.deepcopy(sub_entry)
                                sub_entry.pop("api_key", None)
                                sub_entry.pop("key", None)
                                sub_providers[name] = sub_entry
                        else:
                            sub_providers[name] = copy.deepcopy(entry)
                        sub_llm["providers"] = sub_providers
                        sub["llm"] = sub_llm
                        if _write_confirmed(path, sub):
                            synced += 1
                except Exception:
                    # Do not expose filenames, exceptions or private config bytes.
                    continue
            return _settings_result("ok" if synced == total else "partial_sync",
                                    saved=True, total=total, synced=synced, deleting=deleting)
    except Exception:
        return _settings_result("storage_error", saved=saved, total=total,
                                synced=synced, deleting=deleting)


def save_provider_config(config_path, data_dir, body):
    """Save one provider without enabling LLM or changing runtime controls."""
    try:
        draft = _provider_draft(body)
    except (TypeError, ValueError):
        return _settings_result("invalid")
    return _persist_provider(config_path, data_dir, draft["provider"], draft)


def delete_provider_key(config_path, data_dir, body):
    """Only this explicit operation removes both current and legacy key fields."""
    try:
        if not isinstance(body, dict) or set(body) != {"provider"}:
            raise ValueError
        name = _provider_name(body["provider"])
    except (TypeError, ValueError):
        return _settings_result("invalid", deleting=True)
    return _persist_provider(config_path, data_dir, name, deleting=True)


_TEST_MESSAGES = {
    "application_stopping": "应用正在停止，连接测试未发送。",
    "global_paused": "内置AI调用已暂停；请先明确允许，再进行连接测试。",
    "control_unavailable": "暂时无法确认AI控制状态；连接测试未发送。",
    "ok": "连接测试成功。",
    "missing_key": "请先填写或保存 API 密钥。",
    "invalid_config": "测试配置不正确，请检查协议、地址和模型。",
    "auth_failed": "接口未接受该密钥，请核对密钥及权限。",
    "rate_limited": "接口限流或额度不足，请稍后重试。",
    "model_not_found": "接口地址或模型不存在，请核对设置。",
    "timeout": "连接测试超时，请稍后重试。",
    "network_error": "无法连接模型接口，请检查网络与地址。",
    "busy": "该提供商已有连接测试进行中。",
    "upstream_error": "接口未返回可用的测试结果，请核对协议和模型。",
}


def _test_result(code, usage=None):
    result = {"ok": code == "ok", "code": code, "message": _TEST_MESSAGES[code],
              "transport": "direct"}
    if usage:
        result["usage"] = {key: value for key, value in usage.items()
                           if key in ("prompt_tokens", "completion_tokens", "total_tokens")
                           and type(value) is int and 0 <= value <= 10**9}
    return result


def _test_remaining(deadline, cancelled):
    remaining = deadline - time.monotonic()
    if cancelled.is_set() or remaining <= 0:
        raise TimeoutError
    return remaining


class _TestControlPaused(RuntimeError):
    def __init__(self, code):
        self.code = code


def _provider_http_request(settings, deadline, cancelled, state):
    """Exactly one POST, no redirects/retries; never log response or credentials.

    DNS may block below Python's socket timeout. The outer deadline returns on
    time, and the post-connect cancellation check prevents a late HTTP request.
    """
    parts = urlsplit(settings["base_url"])
    connection_type = (http.client.HTTPSConnection if parts.scheme == "https"
                       else http.client.HTTPConnection)
    options = {"timeout": _test_remaining(deadline, cancelled)}
    if parts.scheme == "https":
        options["context"] = ssl.create_default_context()
    connection = connection_type(parts.hostname, parts.port, **options)
    state["connection"] = connection
    try:
        connection.connect()
        state["socket"] = connection.sock
        remaining = _test_remaining(deadline, cancelled)
        if connection.sock is not None:
            connection.sock.settimeout(remaining)
        payload = {"model": settings["model"],
                   "messages": [{"role": "user", "content": "Reply OK."}],
                   "max_tokens": 8, "stream": False}
        if settings["protocol"] == "doubao":
            payload["thinking"] = {"type": "disabled"}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        path = (parts.path.rstrip("/") or "") + "/chat/completions"
        state["check_control"]()  # DNS/connect may have outlived a global pause.
        connection.request("POST", path, body=data, headers={
            "Authorization": "Bearer " + settings["api_key"],
            "Content-Type": "application/json", "Accept": "application/json"})
        response = connection.getresponse()
        if response.status in (401, 403):
            return _test_result("auth_failed")
        if response.status == 429:
            return _test_result("rate_limited")
        if response.status == 404:
            return _test_result("model_not_found")
        if not 200 <= response.status < 300:
            return _test_result("upstream_error")
        content = bytearray()
        while True:
            remaining = _test_remaining(deadline, cancelled)
            wire_socket = state.get("socket")
            if wire_socket is not None:
                wire_socket.settimeout(remaining)
            chunk = response.read1(min(8192, 65537 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > 65536:
                return _test_result("upstream_error")
        _test_remaining(deadline, cancelled)
        try:
            record = json.loads(content)
            choice = record["choices"][0]
            text = choice["message"]["content"]
            if not isinstance(text, str) or not text.strip():
                return _test_result("upstream_error")
            usage = record.get("usage")
        except (KeyError, IndexError, TypeError, ValueError, UnicodeError):
            return _test_result("upstream_error")
        return _test_result("ok", usage if isinstance(usage, dict) else None)
    finally:
        connection.close()


def test_provider_connection(config_path, data_dir, body, *, application_guard=None):
    """Test an unsaved draft using an entered or saved key, without saving it.

    Caller gets an answer within the eight-second wall-clock budget. A timed-out
    worker retains its provider lock until it really exits, preventing a second
    potentially billable request while a blocked resolver/connection unwinds.
    """
    deadline = time.monotonic() + _PROVIDER_TEST_TIMEOUT
    try:
        draft = _provider_draft(body)
    except (TypeError, ValueError):
        return _test_result("invalid_config")
    control = application_control(config_path, data_dir)
    if not control["allowed"]:
        return _test_result("control_unavailable" if control["reason"] == "control_unavailable"
                            else "global_paused")
    identity = (os.path.normcase(os.path.abspath(config_path)), draft["provider"])
    with _TEST_LOCKS_GUARD:
        lock = _TEST_LOCKS.setdefault(identity, threading.Lock())
    if not lock.acquire(blocking=False):
        return _test_result("busy")
    cancelled, finished = threading.Event(), threading.Event()
    def check_control():
        if application_guard is not None and application_guard() is not True:
            raise _TestControlPaused("application_stopping")
        current = application_control(config_path, data_dir)
        if not current["allowed"] or current["epoch"] != control["epoch"]:
            raise _TestControlPaused("control_unavailable" if current["reason"] == "control_unavailable"
                                     else "global_paused")
    state = {"check_control": check_control}

    def run():
        try:
            settings = dict(draft)
            config = _load_json(config_path)
            secrets = _provider_secrets(config, settings["api_key"])
            if not settings["api_key"]:
                try:
                    _, providers = _provider_sections(config)
                    settings["api_key"] = _provider_key(
                        _saved_provider_key(providers.get(settings["provider"])))
                except ValueError:
                    settings["api_key"] = ""
            if (any(_secret_embedded(settings, secret) for secret in secrets)
                    or settings["api_key"] and _metadata_has_secret(config, {settings["api_key"]})):
                result = _test_result("invalid_config")
            elif not settings["api_key"]:
                result = _test_result("missing_key")
            else:
                _test_remaining(deadline, cancelled)
                check_control()
                result = _provider_http_request(settings, deadline, cancelled, state)
        except _TestControlPaused as error:
            result = _test_result(error.code)
        except (TimeoutError, socket.timeout):
            result = _test_result("timeout")
        except Exception:
            result = _test_result("network_error")
        finally:
            state["result"] = locals().get("result", _test_result("network_error"))
            lock.release()
            finished.set()

    try:
        threading.Thread(target=run, name="provider-connection-test", daemon=True).start()
    except Exception:
        lock.release()
        return _test_result("network_error")
    if finished.wait(max(0, deadline - time.monotonic())):
        return state["result"]
    cancelled.set()
    # Interrupt slow response bodies without waiting on buffered-reader locks.
    connection = state.get("connection")
    sock = state.get("socket") or getattr(connection, "sock", None)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
            sock.close()
        except OSError:
            pass
    return _test_result("timeout")


def set_key(config_path, data_dir, provider, key):
    """换/加 key。调用方负责交互输入（不回显、不进 shell 历史）。"""
    with file_lock(config_path):
        cfg = _load_json(config_path)
        if not isinstance(cfg, dict):
            raise RuntimeError("config.json 读不出来，保留原文件并拒绝换 key: %s" % config_path)
        llm = cfg.get("llm") if isinstance(cfg.get("llm"), dict) else {}
        providers = llm.get("providers") if isinstance(llm.get("providers"), dict) else {}
        entry = providers.get(provider)
        if not isinstance(entry, dict):
            entry = {}
        entry["api_key"] = str(key).strip()
        providers[provider] = entry
        return sync_llm_config(config_path, data_dir, providers=providers)


def _main(argv):
    """命令行入口。状态机不在这里，这里只改文件。

    status | on | off | provider <name> | config-on | config-off | set-key <provider>
    purpose-on <realtime|review> | purpose-off <realtime|review> | budget <tokens>
    """
    import argparse
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir = os.path.join(root, "data")
    config_path = os.path.join(root, "config.json")

    ap = argparse.ArgumentParser(prog="board_llm")
    ap.add_argument("cmd", choices=["status", "on", "off", "provider",
                                    "config-on", "config-off", "set-key",
                                    "purpose-on", "purpose-off", "budget"])
    ap.add_argument("arg", nargs="?")
    args = ap.parse_args(argv)

    from datetime import datetime
    date = datetime.now().strftime("%Y-%m-%d")

    if args.cmd == "status":
        st = read_status(data_dir, config_path, date)
        st.pop("providers", None)
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0
    if args.cmd in ("on", "off"):
        print(json.dumps(write_gate(data_dir, enabled=(args.cmd == "on"), actor="cli"),
                         ensure_ascii=False))
        print("已写入 %s（调用链 2s 内生效，无需重启）" % gate_path(data_dir))
        return 0
    if args.cmd == "provider":
        if not args.arg:
            print("用法：provider <name>")
            return 2
        print(json.dumps(write_gate(data_dir, active_provider=args.arg, actor="cli"),
                         ensure_ascii=False))
        return 0
    if args.cmd == "budget":
        if not args.arg:
            print("用法：budget <每日 token 上限>（0=不设限）")
            return 2
        try:
            n = int(float(args.arg))
        except ValueError:
            print("不是数字：%s" % args.arg)
            return 2
        r = sync_llm_config(config_path, data_dir, budget=n)
        print("日护栏 daily_token_budget=%s，同步副本 %d 份"
              % (r["budget"], r["count"]))
        print("注意：账本进程启动时读一次；改完想立刻按新值算，重启对应 watch。")
        return 0
    if args.cmd in ("purpose-on", "purpose-off"):
        if args.arg not in ("realtime", "review"):
            print("用法：purpose-on|purpose-off <realtime|review>"
                  "（realtime=实时台词辅助/画像，review=对外复盘建议）")
            return 2
        cur = write_gate(data_dir, purposes={args.arg: args.cmd == "purpose-on"},
                         actor="cli")
        print(json.dumps(cur.get("purposes"), ensure_ascii=False))
        print("已写入 %s（调用链 2s 内生效，无需重启）" % gate_path(data_dir))
        return 0
    if args.cmd in ("config-on", "config-off"):
        if args.cmd == "config-off":
            write_gate(data_dir, enabled=False, actor="cli")
        r = sync_llm_config(config_path, data_dir, enabled=(args.cmd == "config-on"))
        print("默认总许可 enabled=%s，同步副本 %d 份" % (r["enabled"], r["count"]))
        print("config-off同时暂停运行许可；config-on不解除既有运行时暂停，需显式on。主播自动另行开启。")
        return 0
    if args.cmd == "set-key":
        import getpass
        if not args.arg:
            print("用法：set-key <provider>")
            return 2
        key = getpass.getpass("输入 %s 的 api_key（不回显）：" % args.arg)
        if not key.strip():
            print("空值，已放弃")
            return 1
        r = set_key(config_path, data_dir, args.arg, key)
        print("已写入 config.json（.bak 已备份）并同步 %d 份副本" % r["count"])
        return 0
    return 2


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
