# -*- coding: utf-8 -*-
"""看板 Web 化的鉴权层：用户库 + HMAC 无状态 token + Cookie + 登录限流。

设计取舍（见《看板Web化_实施方案》）：
- 不引 JWT 库：stdlib `hmac` + `base64` 足够，少一个依赖。
- 无状态签名 token：board 进程重启不掉线（签名密钥落 data/board_users.json）。
- 密码只存 pbkdf2 哈希；首次启动生成随机密码写 data/board_initial_password.txt。
- **绝不做「源 IP=127.0.0.1 免登录」**：frp 的入站在本机看来全部来自
  127.0.0.1，任何本地豁免等于全公网豁免。

CLI：
    python scripts/board_auth.py init                     # 初始化/查看用户
    python scripts/board_auth.py adduser <名字>            # 加用户（交互输密码）
    python scripts/board_auth.py passwd  <名字>            # 改密码（交互输密码）
    python scripts/board_auth.py list                     # 列用户
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections import deque

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_USERS_PATH = os.path.join(REPO_ROOT, "data", "board_users.json")

PBKDF2_ITER = 120_000
TOKEN_TTL = 12 * 3600
COOKIE_NAME = "wb_token"

# 初始密码字符集：去掉 0/O/1/l/I 等易混字符，方便老大照抄
_PW_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def gen_password(length: int = 12) -> str:
    return "".join(secrets.choice(_PW_ALPHABET) for _ in range(length))


# ---------------- 密码 ----------------

def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    """返回 (salt_hex, hash_hex)。"""
    if not isinstance(password, str) or not password:
        raise ValueError("密码不能为空")
    salt = salt or os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITER)
    return salt.hex(), h.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
    except (ValueError, TypeError):
        return False
    h = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), salt, PBKDF2_ITER)
    return hmac.compare_digest(h.hex(), hash_hex or "")


# ---------------- token ----------------

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def make_token(user: str, secret: str, ttl: int = TOKEN_TTL,
               now: float | None = None) -> str:
    exp = int((now if now is not None else time.time()) + ttl)
    payload = "%s|%d" % (user, exp)
    sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"),
                   hashlib.sha256).hexdigest()[:32]
    return _b64e(("%s|%s" % (payload, sig)).encode("utf-8"))


def verify_token(token: str, secret: str, now: float | None = None) -> str | None:
    """校验通过返回用户名，否则 None（签名不符 / 过期 / 结构坏都算失败）。"""
    if not token or not secret:
        return None
    try:
        raw = _b64d(token).decode("utf-8")
        user, exp_s, sig = raw.rsplit("|", 2)
        payload = "%s|%s" % (user, exp_s)
        want = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"),
                        hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, want):
            return None
        if int(exp_s) < int(now if now is not None else time.time()):
            return None
        return user or None
    except Exception:
        return None


# ---------------- Cookie ----------------

def parse_cookies(header: str | None) -> dict:
    out: dict[str, str] = {}
    for part in (header or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def cookie_header(token: str, max_age: int = TOKEN_TTL) -> str:
    return "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % (
        COOKIE_NAME, token, max_age)


def clear_cookie_header() -> str:
    return "%s=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0" % COOKIE_NAME


# ---------------- 用户库 ----------------

def _write_db(path: str, db: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    try:                      # Windows 上 chmod 基本无效，但 Linux 上有效
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_db(path: str = DEFAULT_USERS_PATH) -> dict | None:
    """读用户库；不存在或坏掉返回 None（由 ensure_db 兜底重建）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            db = json.load(f)
        if not isinstance(db, dict) or not db.get("secret") or not db.get("users"):
            return None
        return db
    except (OSError, ValueError):
        return None


def ensure_db(path: str = DEFAULT_USERS_PATH, user: str = "admin",
              password: str | None = None) -> tuple[dict, str | None]:
    """保证用户库存在。返回 (db, 新生成的明文密码或 None)。

    首次创建时把明文密码写到同目录 board_initial_password.txt（只写一次，
    提醒老大登录后改掉）；后续调用不会再生成。
    """
    db = load_db(path)
    if db:
        return db, None
    # A migrated runtime must not turn a missing/corrupt existing database into
    # a new account after the separate launch-time preflight has already passed.
    if os.environ.get("WB_REQUIRE_EXISTING_STATE") == "1":
        raise RuntimeError("Required existing user database is missing or invalid")
    plain = password or gen_password()
    salt, h = hash_password(plain)
    db = {
        "secret": secrets.token_hex(32),
        "users": [{"name": user, "salt": salt, "hash": h, "role": "admin",
                   "created_at": int(time.time())}],
    }
    _write_db(path, db)
    try:
        note = os.path.join(os.path.dirname(os.path.abspath(path)),
                            "board_initial_password.txt")
        with open(note, "w", encoding="utf-8") as f:
            f.write("看板初始登录\n用户名: %s\n密码: %s\n"
                    "（登录后请改密码：python scripts/board_auth.py passwd %s）\n"
                    % (user, plain, user))
        os.chmod(note, 0o600)
    except OSError:
        pass
    return db, plain


def find_user(db: dict, name: str) -> dict | None:
    for u in db.get("users") or []:
        if u.get("name") == name:
            return u
    return None


def check_login(db: dict, name: str, password: str) -> bool:
    u = find_user(db, name)
    if not u:
        # 用户不存在也走一次哈希，避免「用户名不存在」被时间差探测
        verify_password(password, "00" * 16, "00" * 32)
        return False
    return verify_password(password, u.get("salt", ""), u.get("hash", ""))


def add_user(path: str, name: str, password: str, role: str = "member") -> dict:
    db, _ = ensure_db(path)
    if find_user(db, name):
        raise ValueError("用户已存在：%s" % name)
    salt, h = hash_password(password)
    db["users"].append({"name": name, "salt": salt, "hash": h, "role": role,
                        "created_at": int(time.time())})
    _write_db(path, db)
    return db


def set_password(path: str, name: str, password: str) -> dict:
    db, _ = ensure_db(path)
    u = find_user(db, name)
    if not u:
        raise ValueError("用户不存在：%s" % name)
    salt, h = hash_password(password)
    u["salt"], u["hash"] = salt, h
    _write_db(path, db)
    return db


def remove_user(path: str, name: str) -> dict:
    db, _ = ensure_db(path)
    before = len(db.get("users") or [])
    db["users"] = [u for u in (db.get("users") or []) if u.get("name") != name]
    if len(db["users"]) == before:
        raise ValueError("用户不存在：%s" % name)
    if not db["users"]:
        raise ValueError("不能删掉最后一个用户")
    _write_db(path, db)
    return db


# ---------------- 请求鉴权 ----------------

def user_from_headers(headers, db: dict | None) -> str | None:
    """从 WSGI 风格 headers（含 Cookie）解析当前用户名；失败返回 None。"""
    if not db:
        return None
    token = parse_cookies(headers.get("Cookie")).get(COOKIE_NAME)
    name = verify_token(token or "", db.get("secret", ""))
    if not name or not find_user(db, name):
        return None
    return name


class LoginThrottle:
    """按 IP 的登录失败限流：window 秒内失败超过 max_fails 次 → 拒绝。"""

    def __init__(self, max_fails: int = 8, window: float = 60.0):
        self.max_fails = max_fails
        self.window = window
        self._fails: dict[str, deque] = {}
        self._lock = threading.Lock()

    def _prune(self, ip: str, now: float) -> deque:
        dq = self._fails.setdefault(ip, deque())
        cut = now - self.window
        while dq and dq[0] < cut:
            dq.popleft()
        return dq

    def blocked(self, ip: str, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        with self._lock:
            return len(self._prune(ip, now)) >= self.max_fails

    def fail(self, ip: str, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        with self._lock:
            self._prune(ip, now).append(now)

    def reset(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)


# ---------------- CLI ----------------

def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="看板用户管理")
    ap.add_argument("cmd", choices=["init", "adduser", "passwd", "list"])
    ap.add_argument("name", nargs="?")
    ap.add_argument("--path", default=DEFAULT_USERS_PATH)
    ap.add_argument("--role", default="member")
    args = ap.parse_args(argv)

    if args.cmd == "init":
        db, plain = ensure_db(args.path)
        if plain:
            print("已创建用户库：%s" % args.path)
            print("初始密码已写入同目录 board_initial_password.txt")
        else:
            print("用户库已存在：%s（%d 个用户）" % (args.path, len(db["users"])))
        return 0

    if args.cmd == "list":
        db = load_db(args.path)
        if not db:
            print("用户库不存在，先跑 init")
            return 1
        for u in db["users"]:
            print("%-16s %s" % (u["name"], u.get("role", "member")))
        return 0

    if not args.name:
        print("需要用户名")
        return 1
    pw = getpass.getpass("新密码: ")
    pw2 = getpass.getpass("再输一次: ")
    if pw != pw2 or not pw:
        print("两次不一致或为空")
        return 1
    if args.cmd == "adduser":
        add_user(args.path, args.name, pw, args.role)
        print("已添加：%s" % args.name)
    else:
        set_password(args.path, args.name, pw)
        print("已改密：%s" % args.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
