"""LLM API 封装层：火山方舟（豆包系列）/ DeepSeek 双 Provider 主备路由。

职责：
1. LLMClient.chat(system, user) 单次对话，同步阻塞、短超时（默认 30s）；
   失败重试 2 次（共 3 次尝试），认证/参数类 4xx 不重试（重试无意义），
   超时/连接/限流/5xx 退避重试，全部失败返回 None，绝不向调用方抛异常；
2. LLMClient.probe() 探测请求：messages=[{"role":"user","content":"ping"}]、
   max_tokens=1、独立 probe 超时（5s），用于 LLMRouter 主备切换的回切探测，
   不计入业务成功率/延迟统计；
3. LLMRouter 多级主备链路由（doubao -> 方舟deepseek -> 官方deepseek）：
   - 全局单一失败计数器，达 failover_threshold（默认 2）沿链切下一级，链尾回 primary；
   - 处于备 Provider 时按退避间隔（初始 1800s，失败翻倍封顶 14400s）探测主，
     成功切回并重置退避；主 API 通但日预算护栏超限时暂不切回（不翻倍退避）；
   - 长时间无调用（fail_count_reset_seconds，默认 1800s）自动归零计数器；
   - 日 token 预算护栏只约束主 provider：主超限按链依次借道备用发调用
     （先方舟后官方），全链借道失败才跳过（daily_budget_exhausted）。
4. 每次调用通过 on_metrics 回调写出 metrics 记录
   (ts/provider/model/latency_ms/success/prompt_tokens/completion_tokens/error_type/probe)，
   由调用方（llm_service.run_llm_worker）负责落盘到 data/llm_metrics_YYYY-MM-DD.jsonl。

线程模型：本模块只做同步调用，由 llm_service.run_llm_worker 在独立 daemon
线程内使用，保证 LLM 慢/挂都不阻塞采集主循环与规则分析线程。

依赖：openai SDK（延迟 import）。未安装时 LLM 层自动降级为不可用，
不影响 `llm.enabled=false`（默认）时采集器的既有行为。

安全红线：api_key 只存内存，任何日志、metrics、异常文本都不打印 key 与请求体。
"""

from __future__ import annotations

import glob
from functools import wraps
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional
from urllib.parse import unquote

from file_lock import file_lock

logger = logging.getLogger("llm")

# 不含 400：个别网关把超长 prompt 报 400，属于不可重试；401/403 同理
_FATAL_STATUS = {400, 401, 403, 404}
# 总尝试次数 = 1 次初始 + 2 次重试
_MAX_ATTEMPTS = 3
# 重试退避基数（秒）：第 1 次失败后等 0.5s，第 2 次等 1.0s
_RETRY_BACKOFF_BASE = 0.5
# 探测退避封顶（秒）：1800 → 3600 → 7200 → 14400
_PROBE_BACKOFF_CAP = 14400

# metrics 回调签名：调用方传入，本类只负责构造 record 并回传，不直接落盘
MetricsCallback = Callable[[Dict[str, Any]], None]
_source_secret_sets = {}
_source_secret_lock = threading.RLock()


def _token_count(value):
    """Only an explicit nonnegative integer is known usage, including zero."""
    return value if type(value) is int and value >= 0 else None


def _usage_part(usage, name):
    try:
        return _token_count(usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None))
    except Exception:
        return None


def _known_token_sum(records, name):
    values = [value for record in records if (value := _token_count(record.get(name))) is not None]
    return sum(values) if values else None


class _SDKUsage:
    """Bounded SDK invocation observations within one provider logical call."""
    def __init__(self, provider, model):
        self.provider, self.model = provider, model
        self.attempts = []

    def begin(self, price):
        self.attempts.append({"index": len(self.attempts) + 1, "ts": time.time(),
                              "response_at": None, "prompt_tokens": None,
                              "completion_tokens": None, "price_snapshot": price})

    def response(self, response=None):
        attempt = self.attempts[-1]
        if attempt["response_at"] is not None:
            return  # A later choices/usage parsing error cannot record a response twice.
        attempt["response_at"] = time.time()
        try:
            usage = getattr(response, "usage", None)
        except Exception:
            usage = None
        if usage is None:
            try:
                body = getattr(response, "body", None)
                usage = body.get("usage") if isinstance(body, dict) else None
            except Exception:
                usage = None
        for field in ("prompt_tokens", "completion_tokens"):
            attempt[field] = _usage_part(usage, field)

    def fields(self):
        count = len(self.attempts)
        known = {field: count > 0 and all(attempt[field] is not None for attempt in self.attempts)
                 for field in ("prompt_tokens", "completion_tokens")}
        unknown = sum(any(attempt[field] is None for field in known) for attempt in self.attempts)
        has_known = any(attempt[field] is not None for attempt in self.attempts for field in known)
        responses = [attempt["response_at"] for attempt in self.attempts if attempt["response_at"] is not None]
        return {"sent": bool(count), "sdk_attempts": count, "unknown_attempts": unknown,
                "usage_coverage": "not_sent" if not count else "complete" if not unknown else
                                  "partial" if has_known else "unknown",
                **{field: _known_token_sum(self.attempts, field) for field in known},
                **{field + "_known": value for field, value in known.items()},
                "first_sent_at": self.attempts[0]["ts"] if count else None,
                "last_response_at": responses[-1] if responses else None,
                "sdk_attempt_usage": [dict(attempt) for attempt in self.attempts]}


def source_secret_set(path):
    """Private in-memory redaction history, shared by source-bound callers."""
    with _source_secret_lock:
        return _source_secret_sets.setdefault(str(path), set())


def _contains_secret(value, secrets, *, url=False):
    if not isinstance(value, str):
        return False
    values = (value, unquote(value)) if url else (value,)
    return any(secret in text if url or len(secret) > 4 else text.strip() == secret
               for secret in tuple(secrets) if secret for text in values)


def _safe_metadata(value, secrets):
    return "" if _contains_secret(value, secrets) else value


def validate_llm_metadata(providers, active_provider="", *, known_secrets=None, identities=()):
    """Validate before any source-bound use/logging; never return secret data."""
    secrets = known_secrets if known_secrets is not None else set()
    for raw in providers.values():
        if isinstance(raw, dict):
            for field in ("api_key", "key"):
                key = str(raw.get(field) or "").strip()
                if key:
                    secrets.add(key)
    values = [(value, False) for value in (active_provider, *identities)]
    for name, raw in providers.items():
        values.append((name, False))
        if isinstance(raw, dict):
            values.extend((str(raw.get(field) or ""), field == "base_url")
                          for field in ("base_url", "model", "protocol"))
    return not any(_contains_secret(value, secrets, url=url) for value, url in values)


def remember_llm_secrets(section, secrets):
    """Remember raw legacy aliases for privacy only; never activate an alias."""
    if not isinstance(section, dict):
        return
    rows = [section]
    providers = section.get("providers")
    if isinstance(providers, dict):
        rows.extend(value for value in providers.values() if isinstance(value, dict))
    for row in rows:
        for field in ("api_key", "key"):
            value = row.get(field)
            if isinstance(value, str) and value.strip():
                secrets.add(value.strip())


def _safe_status(exc: Exception) -> Optional[int]:
    """兼容不同 openai 版本的异常取 status_code。"""
    try:
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    except Exception:
        return None
    return status if type(status) is int and 100 <= status <= 599 else None


def canonical_llm_source(path: str) -> str:
    """Resolve a trusted local loader path; watch copies never own credentials.

    This is not a JSON/HTTP configuration field. Only local constructors/loaders
    may supply a path. The standard watch copy lives in <project>/data/.
    """
    source = Path(path).expanduser().resolve()
    if re.fullmatch(r"watch_.+\.config\.json", source.name):
        root = source.parent.parent if source.parent.name == "data" else Path(__file__).resolve().parent
        return str(root / "config.json")
    return str(source)


def _read_request_config(path: str, known_secrets=None, identities=()):
    """Read actual content for every new request; never fall back to a last key."""
    try:
        with open(path, "rb") as handle:
            content = handle.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            return None
        raw = json.loads(content)
        if not isinstance(raw, dict) or not isinstance(raw.get("llm"), dict):
            return None
        # Runtime import avoids the llm_service -> llm_client import cycle.
        from llm_service import LLMConfig
        secrets = known_secrets if known_secrets is not None else source_secret_set(path)
        remember_llm_secrets(raw["llm"], secrets)
        cfg = LLMConfig.from_dict(raw["llm"])
        if not validate_llm_metadata(cfg.providers, cfg.active_provider,
                                     known_secrets=secrets, identities=identities):
            logger.warning("LLM 配置元数据含凭据，本次新请求已阻断")
            return None
        return cfg
    except Exception:
        # JSON/type/SDK exceptions can contain their original document or key.
        logger.warning("LLM 主配置不可读取或无效，本次新请求已阻断")
        return None


def _provider_fields(raw):
    if not isinstance(raw, dict):
        return None
    fields = tuple(str(raw.get(name) or "").strip()
                   for name in ("api_key", "base_url", "model", "protocol"))
    return fields if all(fields[:3]) else None


def _client_request(method):
    """The send lock + fresh read defines one immutable request/retry snapshot."""
    @wraps(method)
    def call(self, *args, **kwargs):
        control = application_control(self._config_path, self._gate_dir)
        if not control["allowed"]:
            if self._config_path:
                self._invalidate_credentials()
            return False if method.__name__ == "probe" else None
        original_guard = kwargs.get("_before_send")
        if self._config_path or self._gate_dir:
            def check_control():
                current = application_control(self._config_path, self._gate_dir)
                if not current["allowed"] or current["epoch"] != control["epoch"]:
                    raise _DispatchCancelled("control_paused")
                final_check = getattr(original_guard, "_control_check", None)
                if final_check is not None:
                    final_check()
            def guarded_send():
                check_control()
                if original_guard is not None:
                    original_guard()
                check_control()
            guarded_send._control_check = check_control
            kwargs["_before_send"] = guarded_send
        with self._request_lock:
            if not self._reload_credentials():
                self._emit_metrics(latency_ms=0, success=False,
                                   error_type="config_unavailable",
                                   probe=method.__name__ == "probe",
                                   on_metrics=kwargs.get("on_metrics"))
                return False if method.__name__ == "probe" else None
            self._sending_depth += 1
            try:
                return method(self, *args, **kwargs)
            except _DispatchCancelled:
                if original_guard is not None:
                    raise
                return False if method.__name__ == "probe" else None
            finally:
                self._sending_depth -= 1
                if self._invalidate_pending and not self._sending_depth:
                    self._invalidate_credentials()
    return call


# ================================================================ 运行时闸门
GATE_FILENAME = "llm_runtime.json"
# 文件没变就不重复解析：每路 watch 每次调用前都会问一遍，15 分钟一次画像也不算密，
# 但 2s 缓存让「连发几十条」的场景不去反复解析同一个文件；stat 每次检查，
# 避免权限/文件系统异常时继续复用已开启的缓存。
GATE_CACHE_TTL = 2.0
_gate_cache: Dict[str, Any] = {"path": "", "mtime": 0.0, "ts": 0.0, "value": {}}


def _gate_read_failure(path: str, now: float, reason: str) -> Dict[str, Any]:
    logger.warning("运行时 LLM 闸门读取失败（%s），本轮关闭调用，等待文件恢复", reason)
    value = {"enabled": False, "control_error": True}
    # 不记作一个成功读取的版本：权限恢复但 mtime 不变时也须重新读取。
    _gate_cache.update({"path": path, "mtime": None, "ts": now, "value": value})
    return value


def runtime_gate(data_dir: str = "data", *, fresh=False) -> Dict[str, Any]:
    """读运行时闸门 `data/llm_runtime.json`（2026-10-23 新增）。

    为什么需要这个东西：`config.json` 是「这路要不要 LLM」的**启动期**开关，改了必须
    重启进程；而六路 watch 各自还有一份 `data/watch_<tag>.config.json` 副本，
    改主配置根本传不过去。所以真正的「开关」必须是一个**运行时读的文件**：

        {"enabled": false, "active_provider": "doubao", "updated_at": "...", "updated_by": "board"}

    - 文件不存在 → 返回 {}（调用方按 config 的既有行为走，不改变任何东西）
    - 已有文件无法读取或格式损坏 → 关闭调用并告警，恢复有效文件后自动恢复
    - enabled=false → 调用直接跳过，采集/规则分析不受影响
    - active_provider 有值且在可用列表里 → 本次调用就走它（切 provider 秒生效）

    :returns: dict，可能含 "enabled"(bool) 与 "active_provider"(str)
    """
    path = os.path.join(data_dir or "data", GATE_FILENAME)
    now = time.time()
    c = _gate_cache
    value: Dict[str, Any] = {}
    try:
        st = os.stat(path)
        # 用纳秒 + 大小做版本键：秒级 st_mtime 在同一秒内改两次会相等，
        # 缓存就会「改了没生效」（2026-10-25 全量测试偶发失败暴露出来的，
        # 生产上表现为「刚关掉闸门又打开，还是没调用」）。
        mtime = (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        mtime = 0
    except OSError as exc:
        return _gate_read_failure(path, now, type(exc).__name__)
    else:
        if (not fresh and c["path"] == path and mtime == c["mtime"] and c["ts"]
                and now - c["ts"] < GATE_CACHE_TTL):
            return c["value"]          # 文件版本没变且缓存未过期
        else:
            raw = None
            try:
                with open(path, encoding="utf-8") as f:
                    raw = json.load(f)
            except FileNotFoundError:
                mtime = 0             # stat 后被删除，仍按不存在处理
            except (OSError, ValueError) as exc:
                return _gate_read_failure(path, now, type(exc).__name__)
            else:
                if not isinstance(raw, dict):
                    return _gate_read_failure(path, now, "根节点不是对象")
            if isinstance(raw, dict):
                if "enabled" in raw:
                    if type(raw["enabled"]) is not bool:
                        return _gate_read_failure(path, now, "enabled不是布尔值")
                    value["enabled"] = raw["enabled"]
                inner = raw.get("llm")
                if isinstance(inner, dict):
                    if "enabled" in inner and "enabled" not in value:
                        if type(inner["enabled"]) is not bool:
                            return _gate_read_failure(path, now, "enabled不是布尔值")
                        value["enabled"] = inner["enabled"]
                    if inner.get("active_provider"):
                        value["active_provider"] = inner.get("active_provider")
                if raw.get("active_provider"):
                    value["active_provider"] = raw.get("active_provider")
                # 用途维度开关（2026-10-25）：实时台词辅助与对外复盘建议是两个消费者，
                # 以前只能同开同关。现在可以 {"purposes": {"realtime": true, "review": false}}。
                # 缺省（不写、或写了非 bool）= 不限制，保持旧行为。
                purposes = raw.get("purposes")
                if isinstance(purposes, dict):
                    norm = {str(k): bool(v) for k, v in purposes.items()
                            if isinstance(v, bool)}
                    if norm:
                        value["purposes"] = norm
                # 单主播开关（2026-10-25）：["某主播tag", ...] 里的 tag 不花钱。
                # 与作用域无关——只有那条链路自己的 router 带 tag，才判得中。
                tags_off = raw.get("tags_off")
                if isinstance(tags_off, list):
                    value["tags_off"] = [str(x) for x in tags_off if x]
                tags_on = raw.get("tags_on")
                if isinstance(tags_on, list):
                    value["tags_on"] = [x for x in tags_on if isinstance(x, str) and x]
                for name in ("pause_generation", "tag_pause_generation", "purpose_pause_generation"):
                    item = raw.get(name, 0 if name == "pause_generation" else {})
                    if name == "pause_generation":
                        if type(item) is not int or item < 0:
                            return _gate_read_failure(path, now, "暂停代际无效")
                    elif (not isinstance(item, dict)
                          or any(not isinstance(k, str) or type(v) is not int or v < 0
                                 for k, v in item.items())):
                        return _gate_read_failure(path, now, "暂停代际无效")
                    value[name] = item

    c.update({"path": path, "mtime": mtime, "ts": now, "value": value})
    return value


def invalidate_gate_cache() -> None:
    """写完闸门立刻失效缓存（同进程内改完马上读，不用等 TTL）。"""
    _gate_cache.update({"path": "", "mtime": 0.0, "ts": 0.0, "value": {}})


def application_control(config_path=None, data_dir=None, tag="", automatic=False, purpose=""):
    """Fresh application permission, independent of keys and worker existence.

    Runtime enabled is an explicit override of canonical enabled. Source-bound
    automatic calls additionally require an explicit tags_on choice. Pure
    in-memory clients retain their compatibility behavior; loaders always bind
    application clients to the canonical source.
    """
    source = canonical_llm_source(config_path) if config_path else None
    directory = data_dir or (str(Path(source).parent / "data") if source else None)
    gate = runtime_gate(directory, fresh=True) if directory else {}
    epoch = [gate.get("pause_generation", 0),
             (gate.get("tag_pause_generation") or {}).get(tag, 0) if automatic else 0,
             (sum((gate.get("purpose_pause_generation") or {}).values()) if automatic
              else (gate.get("purpose_pause_generation") or {}).get(purpose, 0) if purpose else 0)]
    def result(allowed, reason):
        return {"allowed": allowed, "reason": reason, "epoch": epoch}
    if gate.get("control_error"):
        return result(False, "control_unavailable")
    enabled = True  # Only unbound in-memory compatibility callers use this.
    if source:
        try:
            with open(source, "rb") as handle:
                raw = handle.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError
            section = json.loads(raw).get("llm")
            if not isinstance(section, dict) or type(section.get("enabled", False)) is not bool:
                raise ValueError
            enabled = section.get("enabled", False)
        except (OSError, ValueError, TypeError, AttributeError):
            return result(False, "control_unavailable")
    if not gate.get("enabled", enabled):
        return result(False, "global_paused")
    if purpose and (gate.get("purposes") or {}).get(purpose) is False:
        return result(False, "purpose_paused")
    if automatic and (tag in (gate.get("tags_off") or [])
                      or source and tag not in (gate.get("tags_on") or [])):
        return result(False, "automatic_disabled")
    return result(True, "allowed")



# ================================================================ 日 token 预算
class BudgetLedgerError(RuntimeError):
    """Usage is unknown; preserve the ledger/evidence and refuse new calls."""


class _DispatchCancelled(RuntimeError):
    """Internal fixed reason, with no credentials or request data."""

    def __init__(self, reason, issued=False):
        super().__init__(reason)
        self.reason, self.issued = reason, issued


class DailyTokenBudget:
    """按自然日计的 token 预算（文件记账，可跨进程累加）。

    背景（老大 2026-10-04 拍板）：每天有 200 万免费 token，日常小规模调用可接受，
    **不因成本顾虑回避使用**。本护栏的目的不是省钱，是防止异常放量（比如画像
    调用频率被误配）把免费额度打穿：超过预算后跳过新调用并显式记日志，
    预算 0 = 不设限。

    记账文件：``<output_dir>/llm_budget_<YYYY-MM-DD>.json``（主账，无 name 后缀），
    备用账本带 provider 名：``llm_budget_<provider>_<YYYY-MM-DD>.json``。
    采集进程与日报进程共用同一文件 → 真正的「按天」口径。
    读取、累加、原子替换由跨进程锁串行化。已有账本损坏或上次写入未完成时，
    保留证据并拒绝新调用，不把未知用量当零。调用前不预扣，仍按返回 usage 记账。
    """

    def __init__(self, budget: int, output_dir: str = "data",
                 name: str = "") -> None:
        self.budget = max(0, int(budget))
        self.output_dir = output_dir
        self.name = str(name or "")
        # 供日志/外部读取的最近一次快照
        self.last_day = ""
        self.last_total = 0
        self._unaccounted_day = ""

    def _path(self, day: Optional[str] = None) -> str:
        day = day or time.strftime("%Y-%m-%d")
        stem = ("llm_budget_%s_" % self.name) if self.name else "llm_budget_"
        return os.path.join(self.output_dir, stem + "%s.json" % day)

    @staticmethod
    def _read_total(path: str, day: str) -> int:
        # Writers hold the same resource lock, so a remaining temp is evidence of
        # an interrupted/failed update, rather than another live transaction.
        if os.path.exists(path + ".tmp") or glob.glob(glob.escape(path) + ".*.tmp"):
            raise BudgetLedgerError("预算账本有未完成写入，保留临时文件并阻断调用: %s" % path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
        except FileNotFoundError:
            return 0
        except (OSError, ValueError) as exc:
            raise BudgetLedgerError("预算账本无法读取，保留原文件并阻断调用: %s" % path) from exc
        if not isinstance(d, dict) or d.get("date") != day \
                or type(d.get("total")) is not int or d["total"] < 0:
            raise BudgetLedgerError("预算账本格式损坏，保留原文件并阻断调用: %s" % path)
        return d["total"]

    def check(self) -> bool:
        """未超预算返回 True（预算 0 = 不设限，恒 True）。"""
        if self.budget <= 0:
            return True
        day = time.strftime("%Y-%m-%d")
        self.last_day = day
        if self._unaccounted_day == day:
            raise BudgetLedgerError("本进程存在未能落盘的用量，阻断当日调用")
        path = self._path(day)
        try:
            with file_lock(path):
                self.last_total = self._read_total(path, day)
        except OSError as exc:
            raise BudgetLedgerError("预算账本锁定/读取失败，阻断调用: %s" % path) from exc
        return self.last_total < self.budget

    def add(self, prompt_tokens: int, completion_tokens: int) -> None:
        """把一次调用的 token 用量累加进当日账本。预算 0 时不记账。"""
        if self.budget <= 0:
            return
        day = time.strftime("%Y-%m-%d")
        path = self._path(day)
        prompt, completion = int(prompt_tokens or 0), int(completion_tokens or 0)
        if prompt < 0 or completion < 0:
            raise ValueError("token usage must be nonnegative")
        tmp = None
        try:
            with file_lock(path):
                total = self._read_total(path, day) + prompt + completion
                fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".",
                                           suffix=".tmp", dir=self.output_dir)
                # On failure leave the unique temp as evidence. Other processes
                # detect it under the lock and cannot silently reuse an old total.
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({"date": day, "total": total, "budget": self.budget},
                              f, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
        except (OSError, BudgetLedgerError) as exc:
            self._unaccounted_day = day
            if tmp is None:
                # Lock acquisition itself can fail after a paid call completed.
                # Preserve its delta separately, without changing a ledger owned
                # by another writer. Even an empty evidence file blocks readers.
                try:
                    os.makedirs(self.output_dir, exist_ok=True)
                    fd, pending = tempfile.mkstemp(prefix=os.path.basename(path) + ".failed.",
                                                   suffix=".tmp", dir=self.output_dir)
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump({"date": day, "unaccounted_prompt_tokens": prompt,
                                   "unaccounted_completion_tokens": completion,
                                   "error_type": type(exc).__name__}, f)
                        f.flush()
                        os.fsync(f.fileno())
                except OSError:
                    logger.error("未能保存预算失败证据，需人工核对当日用量: %s", path)
            raise BudgetLedgerError("日 token 用量未能记账，保留证据并阻断后续调用: %s" % path) from exc
        self.last_day = day
        self.last_total = total


class LLMClient:
    """单 Provider 的 OpenAI 兼容接口薄封装（同步）。

    :param api_key: API Key（只进内存，禁止打日志/metrics/异常文本）
    :param base_url: OpenAI 兼容地址
    :param model: 模型/接入点 ID
    :param timeout: 单次请求默认超时秒数（可被 chat 的 timeout_override 覆盖）
    :param provider_name: 用于 metrics 记录的 provider 标识（如 "doubao"）
    :param on_metrics: 全局 metrics 回调；chat/probe 每次调用结束都会回调一次
    """
    _supports_dispatch_guard = True

    def __init__(self, api_key: str, base_url: str, model: str,
                 timeout: int = 30, provider_name: str = "",
                 on_metrics: Optional[MetricsCallback] = None,
                 protocol: str = "", config_path: Optional[str] = None,
                 _known_secrets=None, gate_dir=None) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        self.provider_name = provider_name
        self.protocol = str(protocol or "").strip()
        self._on_metrics = on_metrics
        self._config_path = canonical_llm_source(config_path) if config_path else None
        self._gate_dir = gate_dir
        self._known_secrets = (_known_secrets if _known_secrets is not None
                               else source_secret_set(self._config_path) if self._config_path else set())
        if api_key:
            self._known_secrets.add(api_key)
        self._request_lock = threading.RLock()
        self._sending_depth = 0
        self._invalidate_pending = False
        self._credential_state = (api_key, base_url, model, self.protocol, timeout)
        # 延迟 import：未装 openai 包 / 初始化失败时仅标记不可用，不影响采集主程序
        self._client = None
        if self._config_path:
            self._reload_credentials()
        else:
            self._initialize_sdk()

    def _initialize_sdk(self):
        try:
            from openai import OpenAI
        except ImportError:
            logger.error("未安装 openai SDK，LLM 调用将全部降级（pip install openai）")
            return
        try:
            # max_retries=0：重试节奏由本类统一控制，避免 SDK 与本类双重重试
            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout,
                max_retries=0,
            )
        except Exception as e:
            # 不打印 e 细节，防止底层把请求头/key 带进异常文本
            logger.error("LLM 客户端初始化失败 provider=%s: %s",
                         _safe_metadata(self.provider_name, self._known_secrets), type(e).__name__)
            self._client = None

    def _discard_sdk(self):
        previous, self._client = self._client, None
        close = getattr(previous, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                logger.debug("LLM 旧客户端资源清理失败（已停用）")

    def _invalidate_credentials(self):
        self._invalidate_pending = True
        if self._sending_depth or not self._request_lock.acquire(blocking=False):
            return
        try:
            self._discard_sdk()
            self.api_key = self.base_url = self.model = ""
            self._credential_state = None
            self._invalidate_pending = False
        finally:
            self._request_lock.release()

    def _reload_credentials(self):
        if not self._config_path:
            return True  # Explicit in-memory callers have no file reload contract.
        cfg = _read_request_config(self._config_path, self._known_secrets,
                                   identities=(self.provider_name,))
        fields = _provider_fields(cfg.providers.get(self.provider_name)) if cfg is not None else None
        if fields is None:
            self._invalidate_credentials()
            return False
        state = (*fields, cfg.timeout_seconds)
        if state != self._credential_state or self._client is None:
            self._discard_sdk()
            self.api_key, self.base_url, self.model, self.protocol = fields
            self.timeout = cfg.timeout_seconds
            self._credential_state = state
            self._initialize_sdk()
        return True

    def _request_extensions(self):
        if self.protocol == "doubao" or (not self.protocol and self.provider_name == "doubao"):
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        return {}

    def _before_http_send(self, guard, issued):
        """Revoke unsent HTTP attempts without swapping an issued call's SDK."""
        def credentials_current():
            if not self._config_path:
                return True
            cfg = _read_request_config(self._config_path, self._known_secrets,
                                       identities=(self.provider_name,))
            fields = _provider_fields(cfg.providers.get(self.provider_name)) if cfg is not None else None
            return fields is not None and (*fields, cfg.timeout_seconds) == self._credential_state

        if not credentials_current():
            self._invalidate_pending = True
            raise _DispatchCancelled("credentials_changed", issued)
        if guard is not None:
            try:
                guard()
            except _DispatchCancelled as error:
                error.issued = error.issued or issued
                raise
        # Budget I/O and its callbacks may have changed the canonical file.
        if not credentials_current():
            self._invalidate_pending = True
            raise _DispatchCancelled("credentials_changed", issued)
        final_check = getattr(guard, "_control_check", None)
        if final_check is not None:
            try:
                final_check()
            except _DispatchCancelled as error:
                error.issued = error.issued or issued
                raise

    # ------------------------------------------------------------------ metrics
    def _price_snapshot(self):
        if not self._config_path:
            return None
        try:
            from llm_pricing import price_snapshot
            return price_snapshot(self._config_path, self.provider_name, self.model)
        except Exception:
            # Optional estimates never imply a free call, and never block core use.
            return None

    def _emit_metrics(self, *, latency_ms: int, success: bool,
                      error_type: str = "", probe: bool = False,
                      on_metrics: Optional[MetricsCallback] = None, _usage=None) -> None:
        """构造 metrics record 并回调；回调异常不传播（metrics 写挂不影响业务）。"""
        cb = on_metrics or self._on_metrics
        if cb is None:
            return
        try:
            usage = _usage or _SDKUsage(self.provider_name, self.model)
            fields = usage.fields()
            for attempt in fields["sdk_attempt_usage"]:
                price = attempt["price_snapshot"]
                if price is not None and (not isinstance(price, dict) or any(
                        _contains_secret(value, self._known_secrets) for value in price.values())):
                    attempt["price_snapshot"] = None
            cb({
                "ts": fields["first_sent_at"] if fields["sent"] else time.time(),
                "metric_id": uuid.uuid4().hex,
                "metrics_version": 2,
                "provider": _safe_metadata(usage.provider, self._known_secrets),
                "model": _safe_metadata(usage.model, self._known_secrets),
                "latency_ms": latency_ms,
                "success": success,
                "error_type": error_type,
                "probe": probe,
                **fields,
            })
        except Exception as e:
            logger.debug("metrics 回调异常（忽略）: %s", type(e).__name__)

    # ------------------------------------------------------------------ chat
    @_client_request
    def chat(self, system: str, user: str, temperature: float = 0.7,
             timeout_override: Optional[int] = None,
             on_metrics: Optional[MetricsCallback] = None,
             _before_send=None, _on_send=None) -> Optional[str]:
        """单次对话，返回模型文本；任何失败均返回 None。

        :param system: system prompt
        :param user: user prompt
        :param temperature: 采样温度（画像用低温求稳，台词用高温求发散）
        :param timeout_override: 单次请求超时覆盖（如冷场台词独立 5s）；None 用默认
        :param on_metrics: 单次调用 metrics 回调（覆盖类构造时传入的全局回调）
        """
        if self._client is None:
            self._emit_metrics(latency_ms=0, success=False,
                               error_type="client_unavailable",
                               on_metrics=on_metrics)
            return None
        if not system or not user:
            logger.warning("chat 入参为空，跳过调用")
            self._emit_metrics(latency_ms=0, success=False,
                               error_type="empty_input", on_metrics=on_metrics)
            return None

        req_timeout = timeout_override if timeout_override and timeout_override > 0 \
            else self.timeout
        start = time.time()
        usage = _SDKUsage(self.provider_name, self.model)
        last_error_type = ""
        issued = False

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            sdk_entered = False
            try:
                price = self._price_snapshot()
                create = self._client.chat.completions.create
                extensions = self._request_extensions()
                self._before_http_send(_before_send, issued)
                # Only admission to SDK.create counts; a cancelled final guard does not.
                if _on_send is not None:
                    _on_send()
                issued = True
                usage.begin(price)
                sdk_entered = True
                resp = create(
                    model=usage.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    temperature=temperature,
                    timeout=req_timeout,
                    # doubao-seed-2-1-pro/turbo 默认开启深度思考，复杂 JSON 指令会
                    # 长时间推理导致超时；画像/台词/推荐均为文本生成任务，显式关闭思考
                    **extensions,
                )
                # Usage belongs to the SDK response even when choices/business JSON is bad.
                usage.response(resp)
                content = resp.choices[0].message.content
                if isinstance(content, str) and _contains_secret(content, self._known_secrets):
                    logger.warning("LLM 返回内容含凭据，已阻断输出")
                    self._emit_metrics(latency_ms=int((time.time() - start) * 1000),
                                       success=False, error_type="credential_in_response",
                                       on_metrics=on_metrics, _usage=usage)
                    return None
                if content is None or not str(content).strip():
                    logger.warning("LLM 返回空内容（第%s次尝试）", attempt)
                    # 空内容视为成功响应但无业务结果，仍记 success=False 便于统计
                    self._emit_metrics(
                        latency_ms=int((time.time() - start) * 1000),
                        success=False, error_type="empty_content", on_metrics=on_metrics,
                        _usage=usage,
                    )
                    return None
                logger.debug("LLM 调用成功 provider=%s model=%s attempt=%s",
                             _safe_metadata(self.provider_name, self._known_secrets),
                             _safe_metadata(self.model, self._known_secrets), attempt)
                self._emit_metrics(
                    latency_ms=int((time.time() - start) * 1000),
                    success=True, on_metrics=on_metrics, _usage=usage,
                )
                return str(content).strip()
            except _DispatchCancelled:
                self._emit_metrics(latency_ms=int((time.time() - start) * 1000), success=False,
                                   error_type="send_cancelled", on_metrics=on_metrics, _usage=usage)
                if _before_send is not None:
                    raise
                return None
            except BudgetLedgerError:
                raise
            except Exception as e:
                status = _safe_status(e)
                if status is not None and sdk_entered:
                    usage.response(e)
                last_error_type = type(e).__name__
                # 认证失败 / 参数错误等：重试不会自愈，立即降级
                if status in _FATAL_STATUS:
                    logger.warning(
                        "LLM 请求不可重试错误 provider=%s %s status=%s，本次跳过",
                        _safe_metadata(self.provider_name, self._known_secrets), type(e).__name__, status,
                    )
                    self._emit_metrics(
                        latency_ms=int((time.time() - start) * 1000),
                        success=False,
                        error_type=f"{last_error_type}_{status}",
                        on_metrics=on_metrics, _usage=usage,
                    )
                    return None
                if attempt < _MAX_ATTEMPTS:
                    wait = _RETRY_BACKOFF_BASE * attempt
                    logger.warning(
                        "LLM 请求失败 provider=%s %s status=%s（第%s/%s次），%.1fs 后重试",
                        _safe_metadata(self.provider_name, self._known_secrets), type(e).__name__, status,
                        attempt, _MAX_ATTEMPTS, wait,
                    )
                    time.sleep(wait)
                else:
                    logger.warning(
                        "LLM 连续 %s 次失败，放弃本次调用 provider=%s: %s status=%s",
                        _MAX_ATTEMPTS, _safe_metadata(self.provider_name, self._known_secrets),
                        type(e).__name__, status,
                    )
        self._emit_metrics(
            latency_ms=int((time.time() - start) * 1000),
            success=False,
            error_type=last_error_type or "retry_exhausted",
            on_metrics=on_metrics, _usage=usage,
        )
        return None

    # ------------------------------------------------------------------ probe
    @_client_request
    def probe(self, probe_timeout: int = 5,
              on_metrics: Optional[MetricsCallback] = None,
              _before_send=None, _on_send=None) -> bool:
        """探测请求（旁路）：ping + max_tokens=1，独立超时，不计入业务统计。

        用于 LLMRouter 处于备 Provider 时周期性探测主是否恢复。
        """
        if self._client is None:
            self._emit_metrics(latency_ms=0, success=False,
                               error_type="client_unavailable",
                               probe=True, on_metrics=on_metrics)
            return False
        start = time.time()
        usage = _SDKUsage(self.provider_name, self.model)
        sdk_entered = False
        try:
            price = self._price_snapshot()
            create = self._client.chat.completions.create
            extensions = self._request_extensions()
            self._before_http_send(_before_send, False)
            if _on_send is not None:
                _on_send()
            usage.begin(price)
            sdk_entered = True
            resp = create(
                model=usage.model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
                timeout=probe_timeout,
                **extensions,
            )
            usage.response(resp)
            # 任何非异常响应都视为 provider 可达
            logger.debug("探测成功 provider=%s model=%s",
                         _safe_metadata(self.provider_name, self._known_secrets),
                         _safe_metadata(self.model, self._known_secrets))
            self._emit_metrics(
                latency_ms=int((time.time() - start) * 1000),
                success=True, probe=True, on_metrics=on_metrics, _usage=usage,
            )
            return True
        except _DispatchCancelled:
            self._emit_metrics(latency_ms=0, success=False, error_type="send_cancelled",
                               probe=True, on_metrics=on_metrics, _usage=usage)
            if _before_send is not None:
                raise
            return False
        except BudgetLedgerError:
            raise
        except Exception as e:
            status = _safe_status(e)
            if status is not None and sdk_entered:
                usage.response(e)
            # 4xx 中只有 429（限流）/5xx/超时表示 provider 暂时不可达；
            # 400/401/403/404 表示配置错误，也算探测失败（切回无意义）
            logger.debug("探测失败 provider=%s %s status=%s",
                         _safe_metadata(self.provider_name, self._known_secrets), type(e).__name__, status)
            self._emit_metrics(
                latency_ms=int((time.time() - start) * 1000),
                success=False, error_type=type(e).__name__,
                probe=True, on_metrics=on_metrics, _usage=usage,
            )
            return False


# ================================================================ 双 Provider 路由
class LLMRouter:
    """双 Provider 主备路由 + 全局失败计数 + 探测退避回切。

    主备语义：
    - primary = 配置的 active_provider（如 "doubao"）
    - backup  = 另一个有 api_key 的 provider
    - 只有一个 provider 有 key 时，backup 为 None，无主备切换（行为退化为单 provider）

    失败计数（全局单一计数器，不区分画像/台词/推荐/探测）：
    - 任一成功调用 → consecutive_failures 归零
    - 任一失败调用 → +1；达 failover_threshold → 切换到 backup，计数器归零
    - 距上次调用超过 fail_count_reset_seconds 无调用 → 自动归零

    探测回切（仅在 active=backup 时触发）：
    - 每 probe_backoff 秒探测 primary 一次（probe_timeout 秒，ping+max_tokens=1）
    - 探测成功 → 切回 primary，重置 probe_backoff = probe_interval_seconds
    - 探测失败 → probe_backoff 翻倍，封顶 14400s（4h）
    """

    def __init__(self, providers_cfg: Dict[str, Dict[str, str]],
                 active_provider: str, timeout_seconds: int = 30,
                 probe_timeout_seconds: int = 5,
                 failover_threshold: int = 2,
                 probe_interval_seconds: int = 1800,
                 fail_count_reset_seconds: int = 1800,
                 on_metrics: Optional[MetricsCallback] = None,
                 daily_token_budget: int = 0,
                 budget_dir: str = "data",
                 backup_token_budgets: Optional[Dict[str, int]] = None,
                 gate_dir: Optional[str] = None,
                 tag: str = "", config_path: Optional[str] = None,
                 requested_provider: Optional[str] = None,
                 automatic=False, control_epoch=None, stop_event=None, request_guard=None,
                 on_dispatch=None) -> None:
        self._route_lock = threading.RLock()
        self._config_path = canonical_llm_source(config_path) if config_path else None
        self._config_signature = None
        self._outer_metrics = on_metrics
        self._on_dispatch = on_dispatch
        self._dispatch_local = threading.local()
        self._requested_provider = str(requested_provider or "").strip()
        self._automatic = bool(automatic)
        self._control_epoch = list(control_epoch) if control_epoch is not None else None
        self._stop_event = stop_event
        self._closed = False
        self._request_guard = (request_guard if request_guard is not None or not self._automatic
                               or not self._config_path else lambda: False)
        self._known_secrets = source_secret_set(self._config_path) if self._config_path else set()
        metadata_ok = validate_llm_metadata(providers_cfg, active_provider,
                                            known_secrets=self._known_secrets,
                                            identities=(self._requested_provider,))
        blocked_metadata = bool(self._config_path and not metadata_ok)
        if blocked_metadata:
            logger.warning("LLM 配置元数据含凭据，客户端初始化已阻断")
            providers_cfg, active_provider = {}, ""
        # 运行时闸门目录（2026-10-23）：只有显式给了才启用闸门。
        # 生产走 llm_service.build_router(gate_dir=data)，直接 new 出来的（测试、
        # 临时脚本）不读闸门 —— 否则单测会被本机 data/llm_runtime.json 的状态左右。
        self._gate_dir = gate_dir
        # 本 router 服务的主播标签（2026-10-25）：用于单主播开关 tags_off。
        # 空串 = 不属于任何主播（日报、事后批处理），不受 tags_off 限制。
        self._tag = str(tag or "")
        # 过滤出有 api_key 的 provider
        self._clients: Dict[str, LLMClient] = {}
        for name, cfg in providers_cfg.items():
            key = str((cfg or {}).get("api_key") or "").strip()
            base_url = str((cfg or {}).get("base_url") or "").strip()
            model = str((cfg or {}).get("model") or "").strip()
            if not key or not base_url or not model:
                logger.warning("provider %s 配置不完整（key/base_url/model 任一为空），跳过", name)
                continue
            self._clients[name] = LLMClient(
                api_key=key, base_url=base_url, model=model,
                timeout=timeout_seconds, provider_name=name,
                on_metrics=on_metrics,
                protocol=str((cfg or {}).get("protocol") or ""),
                config_path=self._config_path,
                _known_secrets=self._known_secrets,
                gate_dir=self._gate_dir,
            )

        # 确定主备顺序
        self.primary = (self._requested_provider or
                        (active_provider if active_provider in self._clients else next(iter(self._clients), "")))
        if blocked_metadata:
            self.primary = ""
        # CLI selection does not move canonical provider budget ownership.
        self._budget_primary_provider = (active_provider if active_provider in providers_cfg else self.primary)
        # 主备链：primary 之后按配置顺序排其余 provider（2026-10-08 扩为三级：
        # doubao -> 方舟deepseek -> 官方deepseek，失败沿链前进，链尾回主）。
        # self.backup 保留为首备用（兼容既有调用方）。
        self.backup_chain = [n for n in self._clients if n != self.primary]
        self.backup = self.backup_chain[0] if self.backup_chain else None
        # 当前 active provider；若 primary 不可用，退化到 backup
        self.active = self.primary if self.primary in self._clients else ""

        self.timeout_seconds = timeout_seconds
        self.probe_timeout_seconds = probe_timeout_seconds
        self.failover_threshold = max(1, failover_threshold)
        self.probe_interval_seconds = max(1, probe_interval_seconds)
        self.fail_count_reset_seconds = max(1, fail_count_reset_seconds)

        # 路由状态
        self.consecutive_failures = 0
        self.last_call_ts = 0.0
        self.last_probe_ts = 0.0
        self.probe_backoff = self.probe_interval_seconds

        # 日 token 预算护栏（budget<=0 = 不设限）。分账本（2026-10-12）：
        # 主账（无 name 后缀）= primary 的 50% 额度；备用各有自己的账本
        # （backup_token_budgets 指定，如方舟 deepseek 90%），没配账本的
        # 备用（如官方按量计费）不设限。
        self._budget: Optional[DailyTokenBudget] = \
            DailyTokenBudget(daily_token_budget, budget_dir) \
            if daily_token_budget and daily_token_budget > 0 else None
        self._backup_budgets: Dict[str, DailyTokenBudget] = {}
        self._backup_budget_limits = {name: int(value) for name, value in (backup_token_budgets or {}).items()
                                      if int(value) > 0}
        self._budget_dir = budget_dir
        for prov, b in (backup_token_budgets or {}).items():
            if prov in self._clients and int(b) > 0:
                self._backup_budgets[prov] = DailyTokenBudget(
                    int(b), budget_dir, name=prov)

        # 记账回调：包住调用方的 on_metrics —— 凡带 token 用量的记录（probe 不计）
        # 按 record 里的 provider 分进各自账本，再原样转发给真正的 metrics 落盘回调
        outer = on_metrics

        def _accounting_cb(rec: Dict[str, Any]) -> None:
            if not rec.get("probe"):
                pt = _token_count(rec.get("prompt_tokens")) or 0
                ct = _token_count(rec.get("completion_tokens")) or 0
                if pt or ct:
                    prov = rec.get("provider") or ""
                    # 无 provider 标识的记录兜底记主账（真实 LLMClient 恒带）
                    acct = self._budget if (prov == self._budget_primary_provider or not prov) \
                        else self._backup_budgets.get(prov)
                    if acct is not None:
                        try:
                            acct.add(pt, ct)
                            if getattr(acct, "last_day", ""):
                                rec["ledger_date"] = acct.last_day
                        except BudgetLedgerError as exc:
                            logger.error("LLM 用量记账失败，后续预算检查将阻断调用: %s", exc)
            if outer is not None:
                try:
                    outer(rec)
                except Exception as e:
                    logger.debug("metrics 回调异常（忽略）: %s", type(e).__name__)

        self._on_metrics: MetricsCallback = _accounting_cb

        logger.info(
            "LLMRouter 初始化 primary=%s backup=%s active=%s clients=%s",
            _safe_metadata(self.primary, self._known_secrets) or "(无)",
            _safe_metadata(self.backup, self._known_secrets) or "(无)",
            _safe_metadata(self.active, self._known_secrets) or "(无)",
            [_safe_metadata(name, self._known_secrets) for name in self._clients],
        )

    def _reload_clients(self):
        with self._route_lock:
            return self._reload_clients_locked()

    def _reload_clients_locked(self):
        if self._closed:
            return False
        if not self._config_path:
            return not self._requested_provider or self._requested_provider in self._clients
        cfg = _read_request_config(self._config_path, self._known_secrets,
                                   identities=(self._requested_provider,))
        providers = ({name: _provider_fields(value) for name, value in cfg.providers.items()}
                     if cfg is not None else {})
        providers = {name: value for name, value in providers.items() if value is not None}
        signature = (tuple((name, *value) for name, value in providers.items()),
                     cfg.active_provider, cfg.timeout_seconds, cfg.probe_timeout_seconds) if cfg is not None else None
        if signature is not None and signature == self._config_signature:
            return bool(self._clients) and (not self._requested_provider or self._requested_provider in self._clients)
        old, old_primary, old_active = self._clients, self.primary, self.active
        clients = {}
        for name, fields in providers.items():
            previous = old.get(name)
            state = (*fields, cfg.timeout_seconds)
            if previous is not None and getattr(previous, "_credential_state", None) == state:
                clients[name] = previous
            else:
                clients[name] = LLMClient(
                    api_key=fields[0], base_url=fields[1], model=fields[2],
                    protocol=fields[3], timeout=cfg.timeout_seconds,
                    provider_name=name, on_metrics=self._outer_metrics,
                    config_path=self._config_path, gate_dir=self._gate_dir)
                clients[name]._known_secrets = self._known_secrets
        self._clients = clients
        for name, limit in self._backup_budget_limits.items():
            if name in clients and name not in self._backup_budgets:
                self._backup_budgets[name] = DailyTokenBudget(limit, self._budget_dir, name=name)
        self.primary = (self._requested_provider or
                        (cfg.active_provider if cfg is not None and cfg.active_provider in clients
                         else next(iter(clients), "")))
        self.backup_chain = [name for name in clients if name != self.primary]
        self.backup = self.backup_chain[0] if self.backup_chain else None
        self.active = (old_active if old_primary == self.primary and old_active in clients
                       else self.primary if self.primary in clients else "")
        if cfg is not None:
            self.timeout_seconds = cfg.timeout_seconds
            self.probe_timeout_seconds = cfg.probe_timeout_seconds
        self._config_signature = signature
        for name, previous in old.items():
            if clients.get(name) is not previous:
                invalidate = getattr(previous, "_invalidate_credentials", None)
                if invalidate is not None:
                    invalidate()
        return bool(clients) and (not self._requested_provider or self._requested_provider in clients)

    def close(self):
        """Retire SDKs after their issued calls finish, without waiting for send locks."""
        with self._route_lock:
            self._closed = True
            clients = list(self._clients.values())
        for client in clients:
            invalidate = getattr(client, "_invalidate_credentials", None)
            if invalidate is not None:
                invalidate()

    def _request_metrics(self):
        """Keep an issued request's usage on its send-time ledger identities."""
        with self._route_lock:
            primary, budget = self._budget_primary_provider, self._budget
            backups, outer = dict(self._backup_budgets), self._outer_metrics

        def record(rec):
            if not rec.get("probe"):
                prompt = _token_count(rec.get("prompt_tokens")) or 0
                completion = _token_count(rec.get("completion_tokens")) or 0
                if prompt or completion:
                    provider = rec.get("provider") or ""
                    account = budget if provider == primary or not provider else backups.get(provider)
                    if account is not None:
                        try:
                            account.add(prompt, completion)
                            if getattr(account, "last_day", ""):
                                rec["ledger_date"] = account.last_day
                        except BudgetLedgerError:
                            logger.error("LLM 用量记账失败，后续预算检查将阻断调用")
            if outer is not None:
                try:
                    outer(rec)
                except Exception:
                    logger.debug("metrics 回调异常（忽略）")
        return record

    @property
    def active_client(self) -> Optional[LLMClient]:
        with self._route_lock:
            if not self._reload_clients() or not self.active:
                return None
            return self._clients.get(self.active)

    @property
    def active_model(self) -> str:
        c = self.active_client
        return c.model if c else ""

    @property
    def is_available(self) -> bool:
        """至少有一个可用 provider。"""
        return self.active_client is not None

    def _other_provider(self) -> Optional[str]:
        """失败切换的下一站：链上前进一级，链尾回 primary（二级行为与旧版一致）。"""
        if self._requested_provider:
            return None
        if self.active == self.primary:
            return self.backup_chain[0] if self.backup_chain else None
        if self.active in self.backup_chain:
            idx = self.backup_chain.index(self.active)
            if idx + 1 < len(self.backup_chain):
                return self.backup_chain[idx + 1]
            return self.primary          # 链尾：回头重试主
        return None

    def _provider_budget(self, name):
        return self._budget if name == self._budget_primary_provider else self._backup_budgets.get(name)

    def _selection_metrics(self, account, purpose=""):
        """The selected ledger owns usage, even if public provider text is scrubbed."""
        outer = self._outer_metrics

        def record(rec):
            rec["purpose"] = purpose
            if not rec.get("probe") and account is not None:
                # Nullable/partial sums contribute only their explicitly known part.
                prompt = _token_count(rec.get("prompt_tokens")) or 0
                completion = _token_count(rec.get("completion_tokens")) or 0
                if prompt or completion:
                    try:
                        account.add(prompt, completion)
                        if getattr(account, "last_day", ""):
                            rec["ledger_date"] = account.last_day
                    except BudgetLedgerError:
                        logger.error("LLM 用量记账失败，后续预算检查将阻断调用")
            if outer is not None:
                try:
                    outer(rec)
                except Exception:
                    logger.debug("metrics 回调异常（忽略）")
        return record

    @property
    def last_block_reason(self):
        """Fixed admission reason for this calling thread; never an SDK exception."""
        return getattr(self._dispatch_local, "block_reason", "")

    def _block(self, reason):
        self._dispatch_local.block_reason = reason

    def _lifecycle_allows(self):
        if self._closed or self._stop_event is not None and self._stop_event.is_set():
            return False
        try:
            return self._request_guard is None or bool(self._request_guard())
        except Exception:
            return False

    def _selection(self, purpose="", provider=None, expected=None):
        """Bind provider, client and ledger while holding only the route lock."""
        with self._route_lock:
            control = application_control(self._config_path, self._gate_dir,
                                          self._tag, self._automatic, purpose)
            if (not control["allowed"] or not self._lifecycle_allows()
                    or self._control_epoch is not None
                    and control["epoch"] != self._control_epoch):
                self._block(control["reason"] if not control["allowed"] else
                            "control_changed" if self._control_epoch is not None
                            and control["epoch"] != self._control_epoch else "task_blocked")
                if control["reason"] == "control_unavailable":
                    self._reload_clients()
                return None
            if not self._reload_clients():
                self._block("client_unavailable")
                return None
            gate = runtime_gate(self._gate_dir, fresh=True) if self._gate_dir else {}
            forced = self._requested_provider or gate.get("active_provider")
            if forced in self._clients and forced != self.active:
                self.active = forced
                self.consecutive_failures = 0
            base = self.active
            token = (self._config_signature, self.primary, base, tuple(control["epoch"]))
            if expected is not None and (token != expected["token"] or base != expected["base"]):
                raise _DispatchCancelled("route_changed")
            name = provider or base
            if name not in self._clients or self._requested_provider and name != self._requested_provider:
                self._block("client_unavailable")
                return None
            account = self._provider_budget(name)
            return {"name": name, "base": base, "client": self._clients[name],
                    "account": account, "main_account": self._budget,
                    "token": token, "purpose": purpose, "metrics": self._selection_metrics(account, purpose),
                    "control_epoch": control["epoch"],
                    "borrowed": False}

    def _selection_current(self, plan):
        with self._route_lock:
            current = self._selection(plan["purpose"])
            return bool(current is not None and current["token"] == plan["token"]
                        and current["base"] == plan["base"]
                        and self._clients.get(plan["name"]) is plan["client"]
                        and self._provider_budget(plan["name"]) is plan["account"]
                        and self._budget is plan["main_account"])

    def _selection_budget_ok(self, plan):
        # Never hold a route lock across ledger I/O: callbacks may reload routes.
        if plan["borrowed"]:
            main = plan["main_account"]
            if main is None or main.check():
                raise _DispatchCancelled("route_changed")
        account = plan["account"]
        allowed = account.check() if account is not None else True
        if not self._selection_current(plan):
            raise _DispatchCancelled("route_changed")
        return allowed

    def _selected_call(self, plan, system=None, user=None, temperature=0.7,
                       timeout_override=None, probe=False, dispatch_state=None):
        approvals = [0]

        def check_control():
            current = application_control(self._config_path, self._gate_dir,
                                          self._tag, self._automatic, plan["purpose"])
            if (not current["allowed"] or current["epoch"] != plan["control_epoch"]
                    or not self._lifecycle_allows()):
                reason = (current["reason"] if not current["allowed"] else
                          "control_changed" if current["epoch"] != plan["control_epoch"]
                          else "task_blocked")
                self._block(reason)
                raise _DispatchCancelled(reason, bool(approvals[0]))

        def before_send():
            check_control()
            if not self._selection_current(plan):
                raise _DispatchCancelled("route_changed", bool(approvals[0]))
            if not probe and not self._selection_budget_ok(plan):
                raise _DispatchCancelled("budget_exhausted", bool(approvals[0]))
            if not self._selection_current(plan):
                raise _DispatchCancelled("route_changed", bool(approvals[0]))
            approvals[0] += 1
        before_send._control_check = check_control

        client = plan["client"]
        extra = {"_before_send": before_send} if getattr(client, "_supports_dispatch_guard", False) else {}
        def mark_sent():
            if self._on_dispatch is not None:
                try:
                    admitted = self._on_dispatch(provider=plan["name"], model=client.model)
                except Exception:
                    self._block("task_state_unavailable")
                    raise _DispatchCancelled("task_state_unavailable") from None
                if admitted is False:
                    self._block("task_blocked")
                    raise _DispatchCancelled("task_blocked")
                # The durable write may outlive a pause, key change or lease loss.
                # A conservative pending marker is harmless; sending afterwards is not.
                if extra:
                    client._before_http_send(before_send, bool(dispatch_state and dispatch_state["issued"]))
                check_control()
                if not self._selection_current(plan):
                    raise _DispatchCancelled("route_changed")
            if dispatch_state is not None:
                dispatch_state["issued"] = True
        if extra and (not probe or self._on_dispatch is not None):
            extra["_on_send"] = mark_sent
        if not extra:
            before_send()  # Compatibility for offline stubs, not real SDK sends.
            if not probe or self._on_dispatch is not None:
                mark_sent()
        if probe:
            return client.probe(probe_timeout=self.probe_timeout_seconds,
                                on_metrics=plan["metrics"], **extra)
        return client.chat(system, user, temperature=temperature,
                           timeout_override=timeout_override,
                           on_metrics=plan["metrics"], **extra)

    @staticmethod
    def _selection_metric(plan, error):
        try:
            plan["client"]._emit_metrics(latency_ms=0, success=False, error_type=error,
                                         on_metrics=plan["metrics"])
        except Exception:
            pass

    def chat(self, system: str, user: str, temperature: float = 0.7,
             timeout_override: Optional[int] = None,
             purpose: str = "") -> Optional[str]:
        """Route a call; unknown budget usage blocks it without provider fallback."""
        self._block("")
        # One logical call owns this state across all borrowed candidates/reroutes.
        control = application_control(self._config_path, self._gate_dir,
                                      self._tag, self._automatic, purpose)
        if not control["allowed"]:
            self._block(control["reason"])
            if control["reason"] == "control_unavailable":
                self._reload_clients()
            return None
        dispatch_state = {"issued": False, "control_epoch": control["epoch"]}
        try:
            for _ in range(4):
                try:
                    return self._chat(system, user, temperature=temperature,
                                      timeout_override=timeout_override, purpose=purpose,
                                      _dispatch_state=dispatch_state)
                except _DispatchCancelled as error:
                    if (error.reason == "route_changed" and not error.issued
                            and not dispatch_state["issued"]):
                        continue
                    self._block({"budget_exhausted": "daily_budget_exhausted"}.get(
                        error.reason, error.reason))
                    return None
            self._block("route_changed")
            return None  # Continuous configuration churn is not permission to send.
        except BudgetLedgerError as exc:
            self._block("budget_ledger_error")
            logger.error("预算账本异常，本次 LLM 调用已阻断: %s", exc)
            client = self.active_client
            if client is not None:
                try:
                    client._emit_metrics(latency_ms=0, success=False,
                                         error_type="budget_ledger_error",
                                         on_metrics=self._on_metrics)
                except Exception:
                    pass
            return None

    def _chat(self, system: str, user: str, temperature: float = 0.7,
              timeout_override: Optional[int] = None,
              purpose: str = "", _dispatch_state=None) -> Optional[str]:
        """Use one provider/client/ledger selection; reselect if budget I/O changed it."""
        control = application_control(self._config_path, self._gate_dir,
                                      self._tag, self._automatic, purpose)
        if (not control["allowed"] or _dispatch_state is not None
                and control["epoch"] != _dispatch_state["control_epoch"]):
            self._block(control["reason"] if not control["allowed"] else "control_changed")
            return None
        plan = self._selection(purpose)
        if plan is None:
            return None
        if (_dispatch_state is not None
                and plan["control_epoch"] != _dispatch_state["control_epoch"]):
            self._block("control_changed")
            return None
        provider = plan["name"]
        allowed = self._selection_budget_ok(plan)
        if not allowed and provider == self._budget_primary_provider:
            # An explicit CLI choice is a single-provider request, never a loan.
            if not self._requested_provider:
                for name in list(self.backup_chain):
                    borrow = self._selection(purpose, provider=name, expected=plan)
                    if borrow is None:
                        continue
                    borrow["borrowed"] = True
                    if not self._selection_budget_ok(borrow):
                        continue
                    result = self._selected_call(borrow, system, user, temperature,
                                                  timeout_override, dispatch_state=_dispatch_state)
                    if result is not None:
                        return result
            self._selection_metric(plan, "daily_budget_exhausted")
            self._block("daily_budget_exhausted")
            return None

        with self._route_lock:
            if not self._selection_current(plan):
                raise _DispatchCancelled("route_changed")
            now = time.time()
            if self.last_call_ts and now - self.last_call_ts > self.fail_count_reset_seconds:
                self.consecutive_failures = 0
            self.last_call_ts = now
        if allowed:
            result = self._selected_call(plan, system, user, temperature, timeout_override,
                                         dispatch_state=_dispatch_state)
        else:
            self._selection_metric(plan, "daily_budget_exhausted")
            self._block("daily_budget_exhausted")
            result = None

        # A completed HTTP may return its result, but must not mutate a newer route.
        with self._route_lock:
            if not self._selection_current(plan):
                return result
            if result is not None:
                self.consecutive_failures = 0
                return result
            self.consecutive_failures += 1
            if self.consecutive_failures >= self.failover_threshold:
                other = self._other_provider()
                if other:
                    self.active = other
                    self.last_probe_ts = time.time()
                    self.probe_backoff = self.probe_interval_seconds
                self.consecutive_failures = 0
        return None

    def probe_and_maybe_switch(self) -> None:
        """处于备 Provider 时按退避间隔探测主，成功切回，失败翻倍。"""
        control = application_control(self._config_path, self._gate_dir,
                                      self._tag, self._automatic)
        if not control["allowed"]:
            if control["reason"] == "control_unavailable":
                self._reload_clients()
            return
        base = self._selection()
        if base is None or self._requested_provider:
            return
        if base["control_epoch"] != control["epoch"]:
            return
        if not self.primary or self.active == self.primary:
            return  # 在主上或无 primary，无需探测
        if self.primary not in self._clients:
            return  # primary 客户端不存在
        now = time.time()
        if now - self.last_probe_ts < self.probe_backoff:
            return
        self.last_probe_ts = now
        try:
            plan = self._selection(provider=self.primary, expected=base)
            if plan is None:
                return
            logger.debug("探测主 provider=%s（当前 backoff=%ss）",
                         _safe_metadata(plan["name"], self._known_secrets), self.probe_backoff)
            ok = self._selected_call(plan, probe=True)
        except (_DispatchCancelled, BudgetLedgerError):
            return
        if not self._selection_current(plan):
            return
        # 主 API 通但当日预算护栏超限时不切回（切回会被 chat 借道逻辑拦），
        # 退避不翻倍：护栏次日重置后最多一个正常间隔即回主。
        try:
            probe_account = plan["account"]
            budget_ok = probe_account.check() if ok and probe_account is not None else True
        except BudgetLedgerError as exc:
            logger.error("预算账本异常，不切回主 provider: %s", exc)
            return
        if not budget_ok:
            logger.warning(
                "探测主 provider=%s 成功，但日 token 预算仍超限，暂不切回"
                "（护栏次日重置后自动回主）", self.primary,
            )
            return
        with self._route_lock:
            if not self._selection_current(plan):
                return
            name = _safe_metadata(plan["name"], self._known_secrets)
            if ok:
                logger.warning("探测主 provider=%s 成功，切回主", name)
                self.active = plan["name"]
                self.consecutive_failures = 0
                self.probe_backoff = self.probe_interval_seconds
            else:
                self.probe_backoff = min(self.probe_backoff * 2, _PROBE_BACKOFF_CAP)
                logger.warning("探测主 provider=%s 失败，下次探测间隔 %ss",
                               name, self.probe_backoff)
