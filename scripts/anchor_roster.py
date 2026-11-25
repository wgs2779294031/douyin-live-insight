# -*- coding: utf-8 -*-
"""Customer-owned anchor roster, shared by watch and recording.

New installations start empty; existing-state protection is preserved.
Account identifiers belong to local configuration, never the release seed.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from file_lock import file_lock

ROSTER_PATH = ROOT / "anchors.json"
SEC_UID_PREFIX = "MS4wLjABAAAA"
_UID = re.compile(SEC_UID_PREFIX + r"[A-Za-z0-9_-]+={0,2}")
_SCHEME = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://")
MAX_TAG_LEN = 40

# The customer supplies its own accounts after installation.
SEED = []  # Customer baseline never embeds monitored accounts.


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def parse_anchor_uid(value: str) -> str:
    """Parse one identity at the entry boundary; never visit or follow a URL."""
    raw = value.strip()
    if not raw:
        raise ValueError("请粘贴主播的抖音个人主页长链接或 sec_uid")
    schemes = list(_SCHEME.finditer(raw))
    if len(schemes) > 1:
        raise ValueError("一次只能添加一个主播，请只保留一个主页链接")
    if schemes:
        # 分享文案常用换行、中文标点、引号把 URL 与昵称或说明隔开。
        text = raw[schemes[0].start():]
        url = re.split(r"""[\s<>"'【】（）《》「」，。！？；、]""", text, maxsplit=1)[0]
        parsed = urlsplit(url.rstrip("),.;!"))
        host = parsed.netloc.lower()
        if host == "v.douyin.com":
            raise ValueError("这是抖音短链接，请打开主播个人主页后复制完整网址")
        if parsed.scheme not in {"https", "http"} or host not in {
                "douyin.com", "www.douyin.com", "www.iesdouyin.com", "iesdouyin.com"}:
            raise ValueError("请使用抖音个人主页长链接或 sec_uid")
        route = "/share/user/" if host.endswith("iesdouyin.com") else "/user/"
        path = parsed.path.removesuffix("/")
        uid = path.removeprefix(route) if path.startswith(route) else ""
    else:
        # 兼容从地址栏手工截取的「ID?跟踪参数」，正文中的裸标识不猜测。
        uid, _, tracking = raw.partition("?")
        if tracking and any(char.isspace() for char in tracking):
            raise ValueError("请只保留一个主播的 sec_uid 或完整个人主页链接")
    if not 40 <= len(uid) <= 256 or not _UID.fullmatch(uid):
        raise ValueError("请提供个人主页 /user/ 下的完整 sec_uid；视频或直播间链接无法用来添加主播")
    if set(_UID.findall(raw)) != {uid}:
        raise ValueError("发现多个主播标识，请只保留要添加的一个")
    return uid


def _normalize(raw: dict) -> dict:
    tag = str(raw.get("tag") or "").strip()
    return {
        "tag": tag,
        "name": str(raw.get("name") or tag).strip() or tag,
        "sec_uid": str(raw.get("sec_uid") or "").strip(),
        "watch": bool(raw.get("watch", True)),
        "record": bool(raw.get("record", False)),
        "llm": bool(raw.get("llm", False)),
        "enabled": bool(raw.get("enabled", True)),
        "note": str(raw.get("note") or ""),
        "added_at": str(raw.get("added_at") or _today()),
    }


def validate(entry: dict) -> list:
    """返回错误列表（空 = 通过）。只做形状检查，不做在线验证。"""
    errs = []
    tag = str(entry.get("tag") or "").strip()
    if not tag:
        errs.append("tag 不能为空")
    elif len(tag) > MAX_TAG_LEN:
        errs.append("tag 超过 %d 字" % MAX_TAG_LEN)
    elif any(c in tag for c in '/\\:*?"<>|'):
        errs.append("tag 不能含 / \\ : * ? \" < > |")
    uid = str(entry.get("sec_uid") or "").strip()
    if not uid:
        errs.append("sec_uid 不能为空")
    elif not uid.startswith(SEC_UID_PREFIX):
        errs.append("sec_uid 应以 %s 开头" % SEC_UID_PREFIX)
    elif len(uid) < 40:
        errs.append("sec_uid 长度不对（正常 50+ 位）")
    if not entry.get("watch", True) and not entry.get("record", False):
        errs.append("watch 和 record 至少开一个，否则这一路没有任何作用")
    return errs


def load(path=ROSTER_PATH) -> dict:
    """读名册。文件不存在或坏掉抛异常，由 ensure() 兜底。"""
    with open(str(path), "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("名册根节点必须是对象")
    anchors = data.get("anchors")
    if not isinstance(anchors, list):
        raise ValueError("名册 anchors 必须是列表")
    if os.environ.get("WB_REQUIRE_EXISTING_STATE") == "1" or not SEED:
        if any(not isinstance(a, dict) or any(
                key in a and not isinstance(a[key], bool)
                for key in ("watch", "record", "enabled", "llm")) for a in anchors):
            raise ValueError("既有名册条目或开关类型错误，拒绝自动归一化")
    out = {"version": int(data.get("version") or 1),
           "updated_at": data.get("updated_at") or "",
           "anchors": [_normalize(a) for a in anchors if isinstance(a, dict)]}
    seen = set()
    for a in out["anchors"]:
        if a["tag"] in seen:
            raise ValueError("名册里 tag 重复：%s" % a["tag"])
        seen.add(a["tag"])
    return out


@contextmanager
def transaction(path=None):
    """锁住读取到替换的整个事务；只锁 save 无法防止旧快照覆盖。"""
    with file_lock(ROSTER_PATH if path is None else path):
        yield


def _save_unlocked(roster: dict, path) -> None:
    data = {
        "version": int(roster.get("version") or 1),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "anchors": [_normalize(a) for a in roster.get("anchors") or []],
    }
    p = Path(str(path))
    try:
        if p.exists():
            shutil.copyfile(str(p), str(p) + ".bak")
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(p))
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def save(roster: dict, path=ROSTER_PATH) -> None:
    """原子替换完整名册；读改写调用方须同时使用 transaction/update。"""
    with transaction(path):
        _save_unlocked(roster, path)


def update(mutator, path=ROSTER_PATH):
    """重新读盘、修改并落盘，返回 mutator 的结果；异常时不保存。"""
    with transaction(path):
        roster = load(path)
        result = mutator(roster)
        _save_unlocked(roster, path)
        return result


def ensure(path=ROSTER_PATH) -> tuple:
    """保证名册存在。返回 (roster, 是否新建/回退)。

    客户默认无种子时只允许首次缺失创建空名册；既有坏文件直接报错。
    显式提供迁移 SEED 时才沿原有回退分支，并在
    返回值里标记，调用方负责告警。坏文件先改名保留（.broken-<时间戳>），
    免得用户手工加的条目被种子静默覆盖后无处追溯。
    非文件缺失的读盘错误直接上抛，不能把暂时无权读取当成损坏重建。
    已迁移的运行环境设置 WB_REQUIRE_EXISTING_STATE=1：每次只读取既有名册，
    包括预检之后的自愈/看板调用；任何缺失或损坏都上抛，不改名、不写种子。
    """
    with transaction(path):
        if os.environ.get("WB_REQUIRE_EXISTING_STATE") == "1":
            return load(path), False
        try:
            return load(path), False
        except (FileNotFoundError, ValueError, TypeError):
            if not SEED and Path(str(path)).exists():
                raise  # Never erase an existing customer roster by reseeding empty.
            p = Path(str(path))
            try:
                if p.exists():
                    os.replace(str(p), str(p) + ".broken-" + datetime.now().strftime("%Y%m%d%H%M%S"))
            except OSError:
                pass
            roster = {"version": 1, "updated_at": "",
                      "anchors": [_normalize(dict(a, llm=False, enabled=True, note="",
                                                  added_at=_today())) for a in SEED]}
            _save_unlocked(roster, path)
            return roster, True


def find(roster: dict, tag: str) -> dict | None:
    for a in roster.get("anchors") or []:
        if a["tag"] == tag:
            return a
    return None


def upsert(roster: dict, entry: dict) -> dict:
    """新增或按 tag 覆盖（tag 不可改：改 tag 等于删旧增新）。"""
    e = _normalize(entry)
    e["sec_uid"] = parse_anchor_uid(e["sec_uid"])
    errs = validate(e)
    if errs:
        raise ValueError("；".join(errs))
    for i, a in enumerate(roster["anchors"]):
        if a["tag"] == e["tag"]:
            e["added_at"] = a.get("added_at") or e["added_at"]
            roster["anchors"][i] = e
            return e
    roster["anchors"].append(e)
    return e


def remove(roster: dict, tag: str) -> dict:
    target = find(roster, tag)
    if not target:
        raise ValueError("名册里没有：%s" % tag)
    if len(roster["anchors"]) <= 1:
        raise ValueError("不能删掉最后一个主播")
    roster["anchors"] = [a for a in roster["anchors"] if a["tag"] != tag]
    return target


def watch_tags(roster: dict, only_enabled: bool = True) -> list:
    return [a["tag"] for a in roster["anchors"]
            if a.get("watch") and (a.get("enabled") or not only_enabled)]


def record_tags(roster: dict, only_enabled: bool = True) -> list:
    return [a["tag"] for a in roster["anchors"]
            if a.get("record") and (a.get("enabled") or not only_enabled)]


def as_legacy_anchors(roster: dict) -> list:
    """(实例名, sec_uid, tag) 三元组，给仍按老格式消费的调用方（dashboard_build）。"""
    return [(a["tag"], a["sec_uid"], a["tag"]) for a in roster["anchors"]
            if a.get("watch") and a.get("enabled")]
