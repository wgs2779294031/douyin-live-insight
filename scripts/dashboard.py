# -*- coding: utf-8 -*-
"""直播观众画像实时看板 —— 零依赖本地只读服务（仅 Python 标准库）。

数据源（只读、按天命名，文件不存在按空集合处理）：
  data/danmu_douyin_YYYY-MM-DD.jsonl   弹幕/进场/点赞/礼物
  data/portrait_YYYY-MM-DD.jsonl       观众画像
  data/coldline_YYYY-MM-DD.jsonl       冷场救场台词
  data/llm_metrics_YYYY-MM-DD.jsonl    LLM 调用指标

用法：
  python scripts/dashboard.py                      # 实时模式，读 data/ 今日文件
  python scripts/dashboard.py --date 2026-09-17    # 回放模式
  python scripts/dashboard.py --port 8765 --data-dir data

安全约束：只绑定 127.0.0.1；只读 JSONL，绝不写 data/；不读取任何密钥配置；
不输出 api_key / cookie；不读取 coldline_compare_* 对比文件。
"""
import argparse
import json
import os
import re
import socket
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# 项目根 = 本文件上一级；默认数据目录 = 项目根/data（不硬编码绝对路径）
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
DEFAULT_DATA_DIR = os.path.join(PROJECT_ROOT, "data")
ASSETS_DIR = os.path.join(SCRIPT_DIR, "dashboard_assets")

# v2.6.1：看板需查询数据源能力声明（capabilities）做降级显示；项目根入 sys.path
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
try:
    from sources import get_capabilities_by_platform
except Exception as _e:  # 能力查询不可用时降级为"全部支持"（显示数字口径不变）
    get_capabilities_by_platform = None

from board_usage import read_usage

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 启动横幅在输出被重定向时也要即时可见
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

# 弹幕数据文件模板 → 平台标识（v2.6 起按文件名识别平台；record 自带 platform
# 字段时优先用字段值。danmu_web_* 为旧版 B站落盘名，保留向后兼容）
DANMU_FILES = (
    ("danmu_douyin_{date}.jsonl", "douyin"),
    ("danmu_bilibili_{date}.jsonl", "bilibili"),
    ("danmu_web_{date}.jsonl", "bilibili"),
)
PORTRAIT_FILE = "portrait_{date}.jsonl"
COLDLINE_FILE = "coldline_{date}.jsonl"
METRICS_FILE = "llm_metrics_{date}.jsonl"

# 解析结果缓存：path -> (mtime_ns, size, rows)，文件未变化时不重复解析
_cache = {}
_cache_lock = threading.Lock()


def read_jsonl(path):
    """共享读模式读取 JSONL。

    容错：文件不存在/打不开 -> None；空行跳过；单行解析失败（含采集进程
    正在写入的末尾半行）-> 跳过该行。绝不锁文件、绝不写文件。
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    sig = (st.st_mtime_ns, st.st_size)
    cached = _cache.get(path)
    if cached is not None and cached[0] == sig[0] and cached[1] == sig[1]:
        return cached[2]
    rows = []
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    # 半行 JSON（写入中）或坏行：丢弃，不报错
                    continue
    except OSError:
        return cached[2] if cached is not None else None
    with _cache_lock:
        _cache[path] = (sig[0], sig[1], rows)
    return rows


def file_meta(path):
    """返回文件是否存在与最后修改时间（epoch 秒），供前端判断采集状态。"""
    try:
        st = os.stat(path)
    except OSError:
        return {"exists": False, "mtime": None}
    return {"exists": True, "mtime": st.st_mtime}


def _num(value):
    return value if isinstance(value, (int, float)) else 0


def merge_capabilities(platforms):
    """把当日有数据的各平台能力做"或"聚合，供 hero 统计区降级显示。

    - 仅依据落盘 platform 经 sources.get_capabilities_by_platform 查询，
      不在看板内硬编码任何平台判断分支；
    - 多平台同日有数据时，任一平台支持即视为支持（显示统计数字）；
      全部平台均不支持的事件类型标记 False（前端显示"该平台不支持"）；
    - 无数据平台/能力查询不可用/未知 platform → 返回 {}，前端按原口径显示
      数字（缺省即支持，绝不打挂看板）。
    """
    active = [p for p, c in (platforms or {}).items() if c]
    if not active or get_capabilities_by_platform is None:
        return {}
    merged = {}
    for plat in active:
        try:
            caps = get_capabilities_by_platform(plat)
        except ValueError:
            # 未知 platform（如历史/外来文件）：不参与降级判定
            continue
        for event_type, supported in caps.items():
            merged[event_type] = merged.get(event_type, False) or bool(supported)
    return merged


def build_danmu(data_dir, date):
    """合并抖音/B站弹幕文件（如有），按 ts 升序；输出时不带 user_id。

    v2.6：同时按文件名/记录字段统计各平台条数（platforms），供 hero 区显示
    平台标识；total/counts 数字口径不变。
    """
    merged = []
    metas = []
    platforms = {}
    for tpl, plat in DANMU_FILES:
        path = os.path.join(data_dir, tpl.format(date=date))
        metas.append(file_meta(path))
        rows = read_jsonl(path)
        if rows:
            merged.extend(rows)
            for r in rows:
                p = r.get("platform") or plat
                platforms[p] = platforms.get(p, 0) + 1
    merged.sort(key=lambda r: _num(r.get("ts")))
    counts = {}
    for r in merged:
        t = r.get("type") or ""
        counts[t] = counts.get(t, 0) + 1
    latest = []
    for r in merged[-50:]:
        latest.append({
            "ts": r.get("ts"),
            "type": r.get("type", ""),
            "uname": r.get("uname", ""),
            "text": r.get("text", ""),
        })
    return {
        "total": len(merged),
        "counts": counts,
        "platforms": platforms,
        "latest": latest,
    }, metas


def build_portrait(data_dir, date):
    path = os.path.join(data_dir, PORTRAIT_FILE.format(date=date))
    rows = read_jsonl(path) or []
    latest = None
    series = []
    if rows:
        last = rows[-1]
        latest = {
            "ts": last.get("ts"),
            "room_id": last.get("room_id", ""),
            "portrait": last.get("portrait", {}),
        }
        for r in rows[-30:]:
            p = r.get("portrait", {}) or {}
            series.append({
                "ts": r.get("ts"),
                "portrait_ts": p.get("portrait_ts"),
                "sentiment": p.get("sentiment", {}) or {},
                "interest_topics": p.get("interest_topics", []) or [],
            })
    return {"latest": latest, "series": series}, file_meta(path)


def build_coldline(data_dir, date):
    path = os.path.join(data_dir, COLDLINE_FILE.format(date=date))
    rows = read_jsonl(path) or []
    # 最新 20 条完整记录（保持时间升序，前端自行反转展示）；早期缺字段原样保留
    return {"latest": rows[-20:]}, file_meta(path)


def build_metrics(data_dir, date):
    """业务统计排除 probe=true 探测行（探测数单独给），口径与 summarize.py 一致。"""
    path = os.path.join(data_dir, METRICS_FILE.format(date=date))
    usage = read_usage(data_dir, date)
    calls = usage["calls"]
    probes = usage["diagnostics"]["probe_records"]
    success = usage["totals"]["successful_records"]
    provider_counts = {}
    model_counts = {}
    for r in usage["by_provider"]:
        p = r["provider"]
        provider_counts[p] = provider_counts.get(p, 0) + r["metric_records"]
        m = r["model"]
        if m:
            model_counts[m] = model_counts.get(m, 0) + r["metric_records"]
    provider = max(provider_counts, key=provider_counts.get) if provider_counts else ""
    last_provider = usage["by_provider"][-1]["provider"] if usage["by_provider"] else ""
    metrics = {
        "calls": calls,
        "probe_count": probes,
        "success": success,
        "success_rate": round(success / calls, 4) if calls else None,
        "avg_latency_ms": round(usage["totals"]["latency_ms_total"] / usage["totals"]["latency_records"])
            if usage["totals"]["latency_records"] else None,
        "prompt_tokens": usage["prompt_tokens"],
        "completion_tokens": usage["completion_tokens"],
        "cost_cny": usage["cost_cny"],
        "usage": usage,
        "provider": provider,
        "last_provider": last_provider,
        "providers": provider_counts,
        "models": model_counts,
    }
    return metrics, file_meta(path)


def build_state(data_dir, date):
    danmu, danmu_metas = build_danmu(data_dir, date)
    portrait, portrait_meta = build_portrait(data_dir, date)
    coldline, coldline_meta = build_coldline(data_dir, date)
    metrics, metrics_meta = build_metrics(data_dir, date)

    room_id = ""
    if portrait.get("latest"):
        room_id = portrait["latest"].get("room_id") or ""
    if not room_id and coldline.get("latest"):
        for r in reversed(coldline["latest"]):
            if r.get("room_id"):
                room_id = r["room_id"]
                break

    # v2.6：平台标识（有数据的平台按字母序 "+" 连接，如 douyin / bilibili / 两者）
    platforms = danmu.get("platforms") or {}
    platform = "+".join(sorted(p for p, c in platforms.items() if c))
    # v2.6.1：能力降级标记（{"enter": False, ...}），前端对 False 项显示
    # "该平台不支持"；{} 表示无法判定，前端按原口径显示数字
    danmu["capabilities"] = merge_capabilities(platforms)

    return {
        "date": date,
        "now": time.time(),
        "room_id": room_id,
        "platform": platform,
        "danmu": danmu,
        "portrait": portrait,
        "coldline": coldline,
        "metrics": metrics,
        "files": {
            "danmu": danmu_metas,           # 弹幕可能有 douyin/web 两个文件
            "portrait": portrait_meta,
            "coldline": coldline_meta,
            "metrics": metrics_meta,
        },
    }


def make_handler(data_dir, fixed_date):
    """生成绑定数据目录/日期的请求处理器。"""

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "DashBoard/2.5"

        def log_message(self, fmt, *args):
            # 静默常规访问日志，仅异常打印到 stderr
            if args and isinstance(args[1], str) and args[1].startswith(("4", "5")):
                sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _reply(self, code, body, content_type):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._serve_index()
            elif parsed.path == "/api/state":
                self._serve_state(parsed.query)
            else:
                self._reply(404, "Not Found", "text/plain; charset=utf-8")

        def _serve_index(self):
            page = getattr(self.server, "index_page", b"")
            if not page:
                self._reply(500, "index.html 缺失",
                            "text/plain; charset=utf-8")
                return
            self._reply(200, page, "text/html; charset=utf-8")

        def _serve_state(self, query):
            qs = parse_qs(query)
            raw_date = (qs.get("date", [""])[0] or "").strip()
            if raw_date and not DATE_RE.match(raw_date):
                self._reply(400, json.dumps({"error": "bad date"}),
                            "application/json; charset=utf-8")
                return
            date = raw_date or fixed_date or datetime.now().strftime("%Y-%m-%d")
            try:
                state = build_state(data_dir, date)
            except Exception as e:  # 数据异常绝不能打挂看板
                sys.stderr.write("build_state error: %r\n" % e)
                self._reply(500, json.dumps({"error": "internal"}),
                            "application/json; charset=utf-8")
                return
            self._reply(200, json.dumps(state, ensure_ascii=False),
                        "application/json; charset=utf-8")

    return DashboardHandler


def port_in_use(host, port):
    """端口占用探测：用“无 SO_REUSEADDR”的临时套接字试绑。

    Windows 上 SO_REUSEADDR 语义与 Linux 不同，允许重复绑定正在监听的端口，
    仅靠服务端 bind 会误判，故先显式探一次。
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError:
        return True
    finally:
        probe.close()
    return False


class DashboardServer(ThreadingHTTPServer):
    # Windows 下避免与已存在的监听者重复绑定同一端口
    allow_reuse_address = False


def find_port(host, start_port, handler):
    """从 start_port 起最多尝试 6 个端口（+5），返回 (httpd, port)。"""
    last_err = None
    for port in range(start_port, start_port + 6):
        if port_in_use(host, port):
            print("端口 %d 被占用，尝试下一个…" % port)
            continue
        try:
            httpd = DashboardServer((host, port), handler)
            httpd.daemon_threads = True
            return httpd, port
        except OSError as e:
            last_err = e
            print("端口 %d 绑定失败，尝试下一个…" % port)
    raise SystemExit("无可用端口（%d-%d）：%s" % (start_port, start_port + 5, last_err))


def main():
    ap = argparse.ArgumentParser(description="直播观众画像实时看板（本地只读服务）")
    ap.add_argument("--date", default="", help="回放日期 YYYY-MM-DD，缺省读今天（实时模式）")
    ap.add_argument("--port", type=int, default=8765, help="起始端口，默认 8765")
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                    help="JSONL 数据目录，默认 项目根/data")
    ap.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = ap.parse_args()

    if args.date and not DATE_RE.match(args.date):
        ap.error("--date 格式应为 YYYY-MM-DD")

    data_dir = os.path.abspath(args.data_dir)
    fixed_date = args.date
    host = "127.0.0.1"  # 红线：只绑定本机回环

    handler = make_handler(data_dir, fixed_date)
    httpd, port = find_port(host, args.port, handler)
    url = "http://%s:%d/" % (host, port)

    # 页面为纯静态文件，仅替换一个日期占位符（空串=实时模式按当天读取）
    try:
        with open(os.path.join(ASSETS_DIR, "index.html"), "rb") as f:
            index_tpl = f.read()
        httpd.index_page = index_tpl.replace(
            b"__DASHBOARD_DATE__", fixed_date.encode("ascii"))
    except OSError as e:
        httpd.server_close()
        raise SystemExit("无法读取 dashboard_assets/index.html：%s" % e)

    mode = "回放模式 %s" % fixed_date if fixed_date else "实时模式（今日数据）"
    print("=" * 56)
    print("直播观众画像实时看板已启动")
    print("  地址     : %s" % url)
    print("  模式     : %s" % mode)
    print("  数据目录 : %s" % data_dir)
    print("  绑定     : %s（仅本机访问）" % host)
    print("  退出     : 按 Ctrl+C")
    print("=" * 56)

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在退出…")
    finally:
        try:
            httpd.shutdown()
        finally:
            httpd.server_close()
    print("已退出，端口已释放。")


if __name__ == "__main__":
    main()
