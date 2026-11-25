"""从 config.json 生成脱敏的 config.example.json。

用途：把含真实 cookie / api_key 的 config.json 脱敏后输出为示例配置，
供 git 提交与团队共享；脚本递归遍历所有键名，凡含
key/cookie/token/secret/password 的字段值一律替换为空字符串，
避免手动复制漏掉敏感字段。

用法：
    python scripts/gen_example_config.py
    # 默认读写脚本父目录下的 config.json / config.example.json

    python scripts/gen_example_config.py --src my.json --dst my.example.json
    # 自定义输入/输出路径

注意：本脚本不修改 config.json 原文件，只生成 config.example.json。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 敏感字段名匹配：忽略大小写
SENSITIVE_PATTERN = re.compile(
    r"key|cookie|token|secret|password|webhook|authorization|credential|session|"
    r"signature|fingerprint|verifyfp|bogus|ttwid|s_v_web_id|user_unique_id|(?:^|_)auth(?:$|_)", re.IGNORECASE
)


def _scrub_url(value):
    candidate = value.strip()
    if not candidate.lower().startswith(("https://", "http://", "wss://", "ws://")):
        return value
    try:
        url = urlsplit(candidate)
        host = url.hostname or ""
        if ":" in host:
            host = "[" + host + "]"
        if url.port:
            host += ":" + str(url.port)
        query = [(k, "" if SENSITIVE_PATTERN.search(k) or k.lower() == "auth" else v)
                 for k, v in parse_qsl(url.query, keep_blank_values=True)]
        return urlunsplit((url.scheme, host, url.path, urlencode(query), ""))
    except ValueError:
        return ""


def scrub(obj):
    """递归替换敏感字段值为空字符串。"""
    if isinstance(obj, dict):
        return {
            k: ("" if SENSITIVE_PATTERN.search(k) else scrub(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [scrub(x) for x in obj]
    if isinstance(obj, str):
        return _scrub_url(obj)
    return obj


def validate_example(obj, path="root"):
    """Independent publication check; return field paths only, never secret values."""
    # Deliberately separate from the scrubber regexp, so omissions in that rule
    # cannot make generation and validation agree on publishing a credential.
    exact = {"apikey", "accesskey", "webhookurl", "webhook", "authorization",
             "accesstoken", "refreshtoken", "password", "secret", "cookie",
             "sessionid", "signature", "verifyfp", "svwebid", "ttwid", "auth"}
    markers = ("token", "cookie", "password", "secret", "webhook", "credential",
               "signature", "fingerprint", "authorization", "session", "bogus")

    def sensitive(key):
        name = re.sub(r"[^a-z0-9]", "", str(key).lower())
        return name in exact or any(part in name for part in markers)

    leaked = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            where = f"{path}.{key}"
            if sensitive(key) and value not in ("", None, False, [], {}):
                leaked.append(where)
            leaked.extend(validate_example(value, where))
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            leaked.extend(validate_example(value, f"{path}[{i}]"))
    elif isinstance(obj, str) and obj.strip().lower().startswith(("http://", "https://", "ws://", "wss://")):
        try:
            url = urlsplit(obj.strip())
            if url.username or url.password or url.fragment:
                leaked.append(path)
            for key, value in parse_qsl(url.query, keep_blank_values=True):
                if value and sensitive(key):
                    leaked.append(path + ".query." + key)
        except ValueError:
            leaked.append(path)
    return leaked


def retarget_example_paths(config: dict) -> dict:
    """示例配置指向 example 资源目录：clone 后无需真实人设即可直接跑。"""
    anchor = config.get("anchor")
    if isinstance(anchor, dict) and anchor.get("profile_dir") == "anchor_profiles":
        anchor["profile_dir"] = "anchor_profiles.example"
    return config


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从 config.json 生成脱敏的 config.example.json")
    # 默认路径基于脚本位置，不硬编码绝对路径
    project_root = Path(__file__).resolve().parent.parent
    parser.add_argument(
        "--src",
        default=str(project_root / "config.json"),
        help="输入配置文件路径（默认 项目根/config.json）",
    )
    parser.add_argument(
        "--dst",
        default=str(project_root / "config.example.json"),
        help="输出示例配置路径（默认 项目根/config.example.json）",
    )
    args = parser.parse_args()

    src = Path(args.src)
    dst = Path(args.dst)

    if src.resolve() == dst.resolve():
        print("[错误] 输入输出不能是同一个文件", file=sys.stderr)
        return 1

    if not src.exists():
        print(f"[错误] 输入文件不存在: {src}", file=sys.stderr)
        return 1

    try:
        with open(src, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception as e:
        print(f"[错误] 解析 {src} 失败: {e}", file=sys.stderr)
        return 1

    example = scrub(config)
    example = retarget_example_paths(example)

    leaked = validate_example(example)
    if leaked:
        print(f"[错误] 脱敏校验失败，未写入输出；字段：{leaked}", file=sys.stderr)
        return 1

    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=dst.name + ".", suffix=".tmp", dir=str(dst.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(example, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dst)
    except Exception as e:
        print(f"[错误] 写入 {dst} 失败: {e}", file=sys.stderr)
        return 1
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass

    print(f"[OK] 已生成 {dst}（敏感字段已脱敏）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
