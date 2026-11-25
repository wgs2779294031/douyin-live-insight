"""Optional user-entered estimate prices; immutable snapshots live in metrics.

This module neither calls a model nor reads/writes a budget or usage ledger.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import json
import math
import os
from pathlib import Path
import re
import tempfile
import uuid

from file_lock import file_lock

FIELD = "cost_estimates"
_IDENTITY = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_CURRENCY = re.compile(r"[A-Z]{3,8}\Z")
_VERSION = re.compile(r"[0-9a-f]{32}\Z")


class PriceError(ValueError):
    def __init__(self, code="invalid_price"):
        self.code = code
        super().__init__("估算单价字段无效或配置不可读取。")


def _canonical(path):
    from llm_client import canonical_llm_source
    return Path(canonical_llm_source(str(path)))


def _config(path):
    try:
        raw = path.read_bytes()
        if len(raw) > 1024 * 1024:
            raise ValueError
        value = json.loads(raw)
        if not isinstance(value, dict) or not isinstance(value.get("llm", {}), dict):
            raise ValueError
        return value
    except Exception:
        raise PriceError("price_config_unavailable") from None


def _secrets(config, path=None):
    llm = config.get("llm", {})
    values = [llm.get("api_key"), llm.get("key")]
    providers = llm.get("providers", {})
    if isinstance(providers, dict):
        for row in providers.values():
            if isinstance(row, dict):
                values.extend([row.get("api_key"), row.get("key")])
    result = {value.strip() for value in values if isinstance(value, str) and value.strip()}
    if path is not None:
        from llm_client import source_secret_set
        result.update(source_secret_set(str(path)).copy())
    return result


def _contains_secret(value, secrets):
    if isinstance(value, dict):
        return any(_contains_secret(item, secrets) for item in value.values())
    if isinstance(value, list):
        return any(_contains_secret(item, secrets) for item in value)
    if not isinstance(value, str):
        return False
    return any(value == secret or len(secret) > 4 and secret in value for secret in secrets)


def _identity(provider, model):
    if not isinstance(provider, str) or not _IDENTITY.fullmatch(provider):
        raise PriceError()
    if (not isinstance(model, str) or not 1 <= len(model) <= 256 or model != model.strip()
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in model)):
        raise PriceError()
    return provider, model


def _rate(value):
    if value is None:
        return None
    if type(value) not in (int, float) or not 0 <= value <= 10**12 or not math.isfinite(value):
        raise PriceError()
    return value


def _snapshot(value):
    fields = {"provider", "model", "currency", "input_per_million", "output_per_million",
              "source", "version", "saved_at"}
    if not isinstance(value, dict) or set(value) != fields:
        raise PriceError()
    _identity(value["provider"], value["model"])
    if not isinstance(value["currency"], str) or not _CURRENCY.fullmatch(value["currency"]):
        raise PriceError()
    _rate(value["input_per_million"])
    _rate(value["output_per_million"])
    if value["source"] != "user_entered" or not isinstance(value["version"], str) or not _VERSION.fullmatch(value["version"]):
        raise PriceError()
    try:
        stamp = datetime.fromisoformat(value["saved_at"])
        if stamp.tzinfo is None:
            raise ValueError
    except (TypeError, ValueError):
        raise PriceError() from None
    return copy.deepcopy(value)


def _entries(config, path=None):
    raw = config.get("llm", {}).get(FIELD, {"schema": 1, "entries": []})
    if (not isinstance(raw, dict) or set(raw) != {"schema", "entries"}
            or type(raw["schema"]) is not int or raw["schema"] != 1
            or not isinstance(raw["entries"], list) or len(raw["entries"]) > 256):
        raise PriceError("price_config_unavailable")
    entries, seen, secrets = [], set(), _secrets(config, path)
    for value in raw["entries"]:
        try:
            row = _snapshot(value)
        except PriceError:
            raise PriceError("price_config_unavailable") from None
        key = row["provider"], row["model"]
        if key in seen or _contains_secret(row, secrets):
            raise PriceError("price_config_unavailable")
        entries.append(row)
        seen.add(key)
    return entries


def read_prices(config_path):
    path = _canonical(config_path)
    config = _config(path)
    return {"schema": 1, "entries": _entries(config, path)}


def price_snapshot(config_path, provider, model):
    """Capture before final send admission. Missing/invalid prices remain absent."""
    if not config_path:
        return None
    try:
        _identity(provider, model)
        return next((row for row in read_prices(config_path)["entries"]
                     if row["provider"] == provider and row["model"] == model), None)
    except Exception:
        return None


def _write(path, value):
    temporary = None
    try:
        raw = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8") + b"\n"
        if len(raw) > 1024 * 1024 or path.is_symlink():
            raise ValueError
        before = path.read_bytes()
        # Keep the established private configuration backup convention.
        backup = Path(str(path) + ".bak")
        if backup.is_symlink():
            raise ValueError
        backup_fd, backup_temp = tempfile.mkstemp(prefix=backup.name + ".", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(backup_fd, "wb") as stream:
                stream.write(before)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(backup_temp, backup)
        finally:
            if os.path.exists(backup_temp):
                os.unlink(backup_temp)
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        if path.read_bytes() != raw:
            raise OSError
    except Exception:
        raise PriceError("price_storage_error") from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def save_prices(config_path, body):
    """Save only estimate metadata; blank both sides removes the current price."""
    fields = {"provider", "model", "currency", "input_per_million", "output_per_million"}
    if not isinstance(body, dict) or set(body) != fields:
        raise PriceError()
    provider, model = _identity(body["provider"], body["model"])
    input_rate, output_rate = _rate(body["input_per_million"]), _rate(body["output_per_million"])
    removing = input_rate is None and output_rate is None
    if not isinstance(body["currency"], str) or (not removing or body["currency"]) and not _CURRENCY.fullmatch(body["currency"]):
        raise PriceError()
    path = _canonical(config_path)
    with file_lock(path):
        config = _config(path)
        if _contains_secret(body, _secrets(config, path)):
            raise PriceError()
        entries = _entries(config, path)
        entries = [row for row in entries if (row["provider"], row["model"]) != (provider, model)]
        entry = None if removing else dict(body, source="user_entered", version=uuid.uuid4().hex,
                                           saved_at=datetime.now(timezone.utc).isoformat())
        if entry:
            entries.append(_snapshot(entry))
        if len(entries) > 256:
            raise PriceError()
        entries.sort(key=lambda row: (row["provider"], row["model"]))
        llm = config.setdefault("llm", {})
        llm[FIELD] = {"schema": 1, "entries": entries}
        _write(path, config)
    return {"ok": True, "config_saved": True, "entry": entry,
            "prices": {"schema": 1, "entries": entries}}


def estimate_known_usage(record):
    """Exact decimal known-part estimate using only this record's saved snapshot."""
    if not isinstance(record, dict):
        return None
    try:
        price = _snapshot(record.get("price_snapshot"))
        if any(record.get(key) is not None and record[key] != price[key] for key in ("provider", "model")):
            return None
    except PriceError:
        return None
    amounts, priced = {}, {}
    with localcontext() as context:
        context.prec = 80
        for name, token_field in (("input", "prompt_tokens"), ("output", "completion_tokens")):
            count, rate = record.get(token_field), price[name + "_per_million"]
            priced[name] = type(count) is int and 0 <= count <= 10**30 and rate is not None
            amounts[name] = Decimal(count) * Decimal(str(rate)) / Decimal(1000000) if priced[name] else None
        known = [value for value in amounts.values() if value is not None]
        amount = format(sum(known), "f") if known else None
    return {"currency": price["currency"], "amount": amount,
            "input_amount": format(amounts["input"], "f") if amounts["input"] is not None else None,
            "output_amount": format(amounts["output"], "f") if amounts["output"] is not None else None,
            "input_priced": priced["input"], "output_priced": priced["output"],
            "complete": all(priced.values()), "version": price["version"],
            "source": price["source"], "saved_at": price["saved_at"]}
