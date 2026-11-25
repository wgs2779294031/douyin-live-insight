"""Project the local collector's six business streams into one shareable dated JSONL.

One invocation owns a single anchor, Beijing calendar day and optional time range.
The data directory, staging area and export directory must be on the local machine.
The destination is the dedicated UPLOAD directory selected in the NAS client.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from datetime import date, datetime, time, timedelta, timezone


BEIJING = timezone(timedelta(hours=8))
NUMBER = re.compile(r"-?\d{1,20}(?:\.\d{1,10})?\Z")
TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,99}\Z")
URL = re.compile(r"(?:https?://|www\.)[^\s，。；！？、<>\"]+", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", re.ASCII)
PHONE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
TYPES = {
    "stats": {"room_stats", "room_user_seq"},
    "danmu": {"danmaku", "enter", "gift", "like"},
    "events": {"control", "room", "social", "room_rank", "fansclub", "product_change",
               "live_shopping", "match_against", "fan_ticket", "lottery_draw_result",
               "lottery_event", "luckybox", "luckybox_reward", "linkmic_emoji",
               "linker_contribute", "teamfight_score", "teamfight", "raw_event"},
}
NUMERIC = {
    "stats": "online total_pv popularity is_hidden display_type",
    "danmu": "",
    "events": ("status action share_type follow_count roommessagetype fan_type total "
               "update_timestamp msg_type left_score right_score match_status display_status "
               "current_goal_stage final_goal_stage ticket_count candidate_num start_time "
               "end_time duration winner_count total_score score outer_status side_count"),
    "speech": "dur off",
    "watch": "live_status room_status fans user_count",
    "room_meta": "room_status start_time",
}
TEXT = {
    "stats": "display_long",
    "danmu": "text",
    "events": "content toast prize_name box_name",
    "speech": "text",
    "watch": "title",
    "room_meta": "title",
}


def clean_text(value):
    return PHONE.sub("[电话]", EMAIL.sub("[邮箱]", URL.sub("[链接]", value)))


def numbers(obj, fields):
    out = {}
    for key in fields.split():
        value = obj.get(key)
        if isinstance(value, (int, float)) and math.isfinite(value):
            out[key] = value
        elif isinstance(value, str) and NUMBER.fullmatch(value):
            out[key] = value
    return out


def poll_state(row):
    if ("error" in row or "room_data_parse_error" in row or
            str(row.get("api_msg", "")).lower() == "blocked" or row.get("api_code") not in (0, None)):
        return "unknown"
    if row.get("is_live") is True and row.get("web_rid"):
        return "live"
    if row.get("is_live") is False and type(row.get("live_status")) is int and row["live_status"] == 0 and row.get("room_status") != 2:
        return "offline"
    return "unknown"


def project(kind, row, ts, tag, platform):
    out = {"stream": kind, "platform": platform, "tag": tag, "ts": ts}
    if kind in TYPES:
        typ = row.get("type")
        if not isinstance(typ, str) or not TOKEN.fullmatch(typ):
            raise ValueError("Missing or invalid event type")
        out["type"] = typ if typ in TYPES[kind] else "unrecognized"
    out.update(numbers(row, NUMERIC[kind]))
    for key in TEXT[kind].split():
        if isinstance(row.get(key), str):
            out[key] = clean_text(row[key])
    # A source's room_id can be a fallback URL. Only copy actual numeric room IDs.
    room = row.get("room_id_str") if kind == "watch" else row.get("room_id")
    if re.fullmatch(r"[0-9]{1,30}", str(room)):
        out["room_id"] = str(room)
    if kind == "watch":
        out["state"] = poll_state(row)
    if kind == "speech" and re.fullmatch(r"seg_\d+(?:\.flv)?", str(row.get("seg", ""))):
        out["seg"] = row["seg"]
    if kind == "events":
        if row.get("type") == "raw_event" and TOKEN.fullmatch(str(row.get("method", ""))):
            out["method"] = row["method"]
        for key, fields in (("sides", "score"), ("products", "promotion_id index explain_type")):
            if isinstance(row.get(key), list):
                out[key] = [numbers(item, fields) for item in row[key] if isinstance(item, dict)]
    if kind in {"stats", "events"} and isinstance(row.get("ranks"), list):
        out["listed_rank_entries"] = len(row["ranks"])  # Limited push, not room population.
    return out


def sources(root, tag, platform, day):
    if platform == "douyin":
        yield "watch", root / f"watch_{tag}.jsonl"
    yield "room_meta", root / f"room_meta_{platform}_{tag}.json"
    # The producer rotates by arrival date. Adjacent files cover midnight spillover.
    for offset in (-1, 0, 1):
        source_day = day + timedelta(days=offset)
        for kind in ("stats", "danmu", "events"):
            yield kind, root / f"{kind}_{platform}_{tag}_{source_day}.jsonl"
        if platform == "douyin":
            yield "speech", root / f"speech_{tag}_{source_day}.jsonl"


def records(path, is_json):
    with path.open("rb") as stream:
        initial = os.fstat(stream.fileno())
        remaining = initial.st_size
        line_number = 0
        while remaining:
            data = stream.read(remaining) if is_json else stream.readline(remaining)
            if not data:
                raise ValueError(f"{path.name} was truncated during export")
            remaining -= len(data)
            line_number += 1
            if not is_json and not data.endswith(b"\n"):
                break  # Producer has not committed the last row; retry on next run.
            if not data.strip():
                continue
            try:
                item = json.loads(data)
                if not isinstance(item, dict):
                    raise ValueError
            except (UnicodeError, ValueError) as error:
                raise ValueError(f"{path.name} row {line_number}: invalid source JSON") from error
            yield line_number, item
        current = path.stat()
        if ((initial.st_dev, initial.st_ino) != (current.st_dev, current.st_ino)
                or current.st_size < initial.st_size or
                (initial.st_size == current.st_size and initial.st_mtime_ns != current.st_mtime_ns)):
            raise ValueError(f"{path.name} changed in place; rerun after the writer finishes")


def publish(data, dest, tag, day, platform="douyin", start=None, end=None):
    if (not re.fullmatch(r"[\w\u3400-\u9fff .()（）·-]{1,95}", tag)
            or tag != tag.strip(" .") or platform not in {"douyin", "bilibili"}):
        raise ValueError("Invalid anchor tag or platform")
    midnight = datetime.combine(day, time.min, BEIJING)
    tomorrow = midnight + timedelta(days=1)
    beginning = datetime.combine(day, time.fromisoformat(start), BEIJING) if start else midnight
    finish = datetime.combine(day, time.fromisoformat(end), BEIJING) if end else tomorrow
    if not midnight <= beginning < finish <= tomorrow:
        raise ValueError("Choose a non-empty time interval within the Beijing date")
    data, dest = data.resolve(strict=True), dest.resolve()
    if data == dest or data in dest.parents or dest in data.parents:
        raise ValueError("The export and collector directories must be separate")
    dest.mkdir(parents=True, exist_ok=True)
    published = dest / day.isoformat()
    if published.is_symlink() or (published.exists() and published.resolve() != published):
        raise ValueError("Export day directory must not be a link")
    published.mkdir(exist_ok=True)
    suffix = "-" + beginning.strftime("%H%M%S") + "-" + finish.strftime("%H%M%S") if start or end else ""
    target = published / f"{platform}_{tag}{suffix}.jsonl"
    if target.is_symlink():
        raise ValueError("Existing output cannot be a link")
    counts = {k: 0 for k in NUMERIC}
    present = {k: False for k in NUMERIC}
    # Sibling of the ENTIRE synchronized directory, not a temporary inside it.
    with tempfile.TemporaryDirectory(prefix=".nas-export-", dir=dest.parent) as tmp:
        stage = Path(tmp) / "records.jsonl"
        with stage.open("wb") as output:
            def emit(record):
                output.write((json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8"))
            for kind, path in sources(data, tag, platform, day):
                if not path.exists():
                    continue
                if path.resolve() != path or not stat.S_ISREG(path.stat().st_mode):
                    raise ValueError(f"Unexpected linked or nonregular input: {path.name}")
                if kind in ("watch", "room_meta") or day.isoformat() in path.stem[-10:]:
                    present[kind] = True
                for line, row in records(path, kind == "room_meta"):
                    stamp = row.get("fetched_at" if kind == "room_meta" else "ts")
                    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
                        raise ValueError(f"{path.name} row {line}: missing timestamp")
                    if not beginning.timestamp() <= stamp < finish.timestamp():
                        continue
                    if row.get("platform", platform) != platform or row.get("tag", tag) != tag:
                        raise ValueError(f"{path.name} row {line}: anchor/platform mismatch")
                    emit(project(kind, row, stamp, tag, platform))
                    counts[kind] += 1
                    present[kind] = True
            if not any(counts.values()):
                raise ValueError("No business rows in the selected window; no output published")
            emit({"stream": "export_info", "schema": 1, "platform": platform, "tag": tag,
                  "from": beginning.isoformat(), "until_exclusive": finish.isoformat(),
                  "counts": counts, "source_present": present, "window_is_session": False})
            output.flush()
            os.fsync(output.fileno())
        with stage.open("rb") as f:
            digest = hashlib.file_digest(f, "sha256").hexdigest()
        changed = True
        if target.exists():
            with target.open("rb") as f:
                changed = hashlib.file_digest(f, "sha256").hexdigest() != digest
        if changed:
            os.replace(stage, target)
    return {"file": target.name, "date": day.isoformat(), "counts": counts, "sha256": digest, "changed": changed}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--dest", type=Path, required=True)
    p.add_argument("--tag", required=True)
    p.add_argument("--date", type=date.fromisoformat, required=True)
    p.add_argument("--platform", choices=("douyin", "bilibili"), default="douyin")
    p.add_argument("--start", help="inclusive Beijing time, HH:MM:SS")
    p.add_argument("--end", help="exclusive Beijing time, HH:MM:SS")
    args = p.parse_args()
    try:
        print(json.dumps(publish(args.data, args.dest, args.tag, args.date, args.platform, args.start, args.end), ensure_ascii=False))
    except (OSError, ValueError) as exc:
        p.exit(1, str(exc) + "\n")
