#!/usr/bin/env python
"""通用 protobuf 裸解码器：不依赖 .proto 定义，直接按 wire format 还原字段树。

为什么需要
----------
真机实测发现抖音会推 **proto 里没有对应类**的 method（如
``WebcastLotteryEventNewMessage``）——「proto 里没有」只说明公开快照不全，
**不等于平台不推**。要把这些冷门 method 钉死，就得直接读原始字节。

用法
----
    # 从探针产物里解某个 method（probe_methods.py --raw-limit 会存完整 hex）
    python scripts/pb_decode.py --probe data/probe_lottery.json --method WebcastLotteryEventNewMessage

    # 直接解一串 hex
    python scripts/pb_decode.py --hex 0a1b57656263617374...

输出：字段树（字段号 / wire type / 值）。length-delimited 字段会依次尝试
UTF-8 文本、嵌套 message、否则按 hex 展示 —— 足够人工反推字段语义。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, List, Tuple

WIRE_NAMES = {0: "varint", 1: "fixed64", 2: "bytes", 5: "fixed32"}


def _read_varint(buf: bytes, i: int) -> Tuple[int, int]:
    shift = 0
    val = 0
    while True:
        if i >= len(buf):
            raise ValueError("varint 越界（数据可能被截断）")
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, i
        shift += 7
        if shift > 63:
            raise ValueError("varint 过长")


def _parse(buf: bytes, depth: int = 0, max_depth: int = 6) -> List[dict]:
    """把一段字节解成字段列表。解不动就抛异常，调用方降级处理。"""
    fields: List[dict] = []
    i = 0
    while i < len(buf):
        tag, i = _read_varint(buf, i)
        fno, wtype = tag >> 3, tag & 7
        if fno == 0:
            raise ValueError("字段号为 0")
        entry = {"f": fno, "wire": WIRE_NAMES.get(wtype, wtype)}
        if wtype == 0:
            v, i = _read_varint(buf, i)
            entry["varint"] = v
        elif wtype == 1:
            entry["fixed64"] = buf[i:i + 8].hex()
            i += 8
        elif wtype == 5:
            entry["fixed32"] = buf[i:i + 4].hex()
            i += 4
        elif wtype == 2:
            ln, i = _read_varint(buf, i)
            raw = buf[i:i + ln]
            if len(raw) < ln:
                raise ValueError("length-delimited 越界")
            i += ln
            entry["len"] = ln
            if depth < max_depth and raw:
                # 依次尝试：可打印文本 → 嵌套 message → hex
                text = _try_text(raw)
                if text is not None:
                    entry["text"] = text
                else:
                    try:
                        sub = _parse(raw, depth + 1, max_depth)
                        if sub:
                            entry["msg"] = sub
                        else:
                            entry["hex"] = raw.hex()
                    except Exception:
                        entry["hex"] = raw.hex()
            else:
                entry["hex"] = raw.hex()
        else:
            raise ValueError(f"未知 wire type {wtype}（字段 {fno}）")
        fields.append(entry)
    return fields


def _try_text(raw: bytes) -> str | None:
    """可打印 UTF-8（含中文）才当文本，否则返回 None。"""
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not s:
        return None
    printable = sum(1 for ch in s if ch.isprintable() or ch in "\n\r\t")
    if printable / len(s) < 0.9:
        return None
    # 纯控制/极短数字串更可能是嵌套消息，交给上层再试
    if len(s) <= 2 and not s.isalpha():
        return None
    return s


def _fmt(fields: List[dict], indent: int = 0) -> List[str]:
    out = []
    pad = "  " * indent
    for f in fields:
        if "varint" in f:
            out.append(f"{pad}#{f['f']} varint = {f['varint']}")
        elif "fixed64" in f:
            out.append(f"{pad}#{f['f']} fixed64 = {f['fixed64']}")
        elif "fixed32" in f:
            out.append(f"{pad}#{f['f']} fixed32 = {f['fixed32']}")
        elif "text" in f:
            out.append(f"{pad}#{f['f']} str({f['len']}) = {f['text']!r}")
        elif "msg" in f:
            out.append(f"{pad}#{f['f']} msg({f['len']}):")
            out.extend(_fmt(f["msg"], indent + 1))
        else:
            out.append(f"{pad}#{f['f']} bytes({f.get('len')}) = {f.get('hex', '')[:120]}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="通用 protobuf 裸解码器")
    ap.add_argument("--hex", default=None, help="直接给十六进制串")
    ap.add_argument("--probe", default=None, help="探针产物 JSON（含 raw_unknown_methods）")
    ap.add_argument("--method", default=None, help="配合 --probe：要解的 method 名")
    ap.add_argument("--index", type=int, default=0, help="解第几条样本（默认 0）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而非文本树")
    args = ap.parse_args()

    if args.hex:
        raw = bytes.fromhex(args.hex)
        method = "(hex)"
    elif args.probe:
        data = json.loads(Path(args.probe).read_text(encoding="utf-8"))
        raws = data.get("raw_unknown_methods") or {}
        if args.method:
            items = raws.get(args.method) or []
        else:
            print("可解的 method：")
            for k, v in raws.items():
                print(f"  {k}: {len(v)} 条")
            return 0
        if not items:
            print(f"{args.method} 没有留存样本")
            return 1
        raw = bytes.fromhex(items[args.index]["hex"])
        method = args.method
        print(f"# {method} 第 {args.index} 条，{len(raw)} 字节\n")
    else:
        print("需要 --hex 或 --probe")
        return 1

    try:
        fields = _parse(raw)
    except Exception as e:
        print(f"解析失败: {e}\n原始 hex: {raw.hex()}")
        return 1
    if args.json:
        print(json.dumps(fields, ensure_ascii=False, indent=1))
    else:
        print("\n".join(_fmt(fields)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
