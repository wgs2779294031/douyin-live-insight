"""Build the shared file catalog from date folders. Write only to a private staging file."""
import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

STREAMS = ("watch", "room_meta", "stats", "danmu", "events", "speech")
READING_GUIDE = "索引使用说明.md"
DATA_DIR = "数据"
BEIJING = timezone(timedelta(hours=8))
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def checked_directory(path, *, create=False):
    """Check existing ancestors before following a date or creating its children."""
    if path.absolute() != path.resolve():
        raise ValueError(f"Linked published directory: {path}")
    if create:
        path.mkdir(exist_ok=True)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if (not stat.S_ISDIR(info.st_mode) or
            getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError(f"Expected a regular published directory: {path}")
    return True


def inventory(root):
    data = root / DATA_DIR
    if not checked_directory(data):
        return []
    files = []
    for day in data.iterdir():
        if DAY.fullmatch(day.name) and checked_directory(day):
            files.extend(day.glob("*.jsonl"))
    return sorted(files)


def signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def iso_time(ts):
    return datetime.fromtimestamp(ts, BEIJING).isoformat(timespec="milliseconds")


def inspect(root, path):
    digest = hashlib.sha256()
    counts = dict.fromkeys(STREAMS, 0)
    chat = 0
    first, last = float("inf"), float("-inf")
    footer = None
    identity = None
    with path.open("rb") as src:
        before = signature(os.fstat(src.fileno()))
        for line_no, raw in enumerate(src, 1):
            digest.update(raw)
            row = json.loads(raw)
            if footer is not None:
                raise ValueError(f"Unexpected row after footer at {line_no}")
            if row["stream"] == "export_info":
                footer = row
                continue
            kind = row["stream"]
            counts[kind] += 1
            row_identity = (row["platform"], row["tag"])
            if identity is None:
                identity = row_identity
            elif row_identity != identity:
                raise ValueError(f"Mixed platform or tags at {line_no}")
            ts = row["ts"]
            if type(ts) not in (float, int) or not -float("inf") < ts < float("inf"):
                raise ValueError(f"Invalid ts at {line_no}")
            first, last = min(first, ts), max(last, ts)
            if kind == "danmu" and row.get("type") == "danmaku":
                chat += 1
    if before != signature(path.stat()):
        raise ValueError("File changed during verification")
    if not footer or footer.get("schema") != 1 or footer["counts"] != counts:
        raise ValueError("Missing or incompatible footer, or row-count mismatch")
    if (footer["platform"], footer["tag"]) != identity or footer["window_is_session"] is not False:
        raise ValueError("Unexpected footer identity or window type")
    beginning = datetime.fromisoformat(footer["from"])
    finish = datetime.fromisoformat(footer["until_exclusive"])
    if (beginning.utcoffset() != timedelta(hours=8) or finish.utcoffset() != timedelta(hours=8)
            or beginning.date().isoformat() != path.parent.name
            or not beginning.timestamp() <= first <= last < finish.timestamp()):
        raise ValueError("Invalid date partition or record outside the declared window")
    return {
        "path": path.relative_to(root).as_posix(), "date": path.parent.name,
        "platform": identity[0], "tag": identity[1], "from": footer["from"],
        "until_exclusive": footer["until_exclusive"], "first_record_at": iso_time(first),
        "last_record_at": iso_time(last), "counts": counts, "chat_messages": chat,
        "records": sum(counts.values()), "source_present": footer["source_present"],
        "window_is_session": False, "bytes": before[2], "sha256": digest.hexdigest(),
        "reading_guide": READING_GUIDE,
    }, before


def build(root, output):
    if output.is_relative_to(root):
        raise ValueError("Use private staging outside the synchronized dataset")
    paths = inventory(root)
    if not paths:
        raise ValueError("No daily data files found")
    rows, signatures = [], {}
    for path in paths:
        try:
            row, signatures[path] = inspect(root, path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"{path.relative_to(root)}: {exc}") from exc
        rows.append(row)
    rows.sort(key=lambda r: (r["date"], r["platform"], r["tag"], r["from"], r["path"]))
    previous_end = {}
    for row in rows:
        key = (row["platform"], row["tag"])
        if row["from"] < previous_end.get(key, ""):
            raise ValueError(f"Overlapping export windows: {row['path']}")
        previous_end[key] = row["until_exclusive"]
    if paths != inventory(root) or any(signature(p.stat()) != signatures[p] for p in paths):
        raise ValueError("Source inventory changed while building the catalog. Retry.")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=output.parent,
                                     delete=False) as handle:
        tmp = Path(handle.name)
        try:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        except BaseException:
            handle.close()
            tmp.unlink(missing_ok=True)
            raise
    try:
        os.replace(tmp, output)
    finally:
        tmp.unlink(missing_ok=True)
    print(json.dumps({"files": len(rows), "dates": len({r["date"] for r in rows}),
                      "tags": len({r["tag"] for r in rows}),
                      "records": sum(r["records"] for r in rows),
                      "files_with_speech": sum(bool(r["counts"]["speech"]) for r in rows),
                      "index_bytes": output.stat().st_size}))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root", type=Path)
    p.add_argument("staging_output", type=Path)
    args = p.parse_args()
    build(args.root.resolve(strict=True), args.staging_output.resolve())
