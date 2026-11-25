# -*- coding: utf-8 -*-
"""无人值守健康检查脚本（仅标准库，无第三方依赖）。

检查项：
  1. 采集进程存活 + 内存占用（读 data/stability_pid.txt，或按进程名匹配）
  2. 今日弹幕 JSONL 行数与最近写入时间
  3. 断线重连次数 + 最近一次重连是否成功（扫日志关键词）
  4. portrait / coldline / llm_metrics 当日是否各有新产出
  5. data/ 目录磁盘占用
  6. LLM 当日已记录用量和发送时单价快照估算（缺口明确标未知）

用法：
  python scripts/health_check.py            # 人类可读汇总
  python scripts/health_check.py --json     # JSON 输出（便于接监控）
  python scripts/health_check.py --data D:/path/to/data  # 指定 data 目录

输出每项 OK/WARN/FAIL，总判定：全部 OK=健康。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

from board_usage import read_usage, usage_text

# 日志扫描关键词（兼容抖音/B站两套措辞）
RECONNECT_PATTERNS = ("断开", "断线", "重连", "reconnect", "close")
CONNECT_OK_PATTERNS = ("已连接", "connected", "连接成功", "认证")

STATUS_OK = "OK"
STATUS_WARN = "WARN"
STATUS_FAIL = "FAIL"


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def _safe_int(s, default=0):
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------ 检查 1：进程
def check_process(data_dir):
    pid_file = os.path.join(data_dir, "stability_pid.txt")
    pid = None
    if os.path.exists(pid_file):
        try:
            with open(pid_file, encoding="utf-8") as f:
                pid = _safe_int(f.read().strip())
        except Exception:
            pass

    alive = False
    mem_kb = 0
    if pid:
        alive = _is_pid_alive(pid)
        if alive:
            mem_kb = _get_pid_memory_kb(pid)

    if pid and alive:
        return {
            "status": STATUS_OK,
            "pid": pid,
            "memory_mb": round(mem_kb / 1024, 1),
            "detail": f"PID={pid} 存活，内存 {mem_kb / 1024:.1f} MB",
        }
    if pid and not alive:
        return {
            "status": STATUS_FAIL,
            "pid": pid,
            "memory_mb": 0,
            "detail": f"PID={pid} 已不存在（进程退出/崩溃）",
        }
    # 无 PID 文件：尝试按进程名模糊匹配（python 运行 main.py / web_client.py）
    found = _find_collector_process()
    if found:
        return {
            "status": STATUS_WARN,
            "pid": found["pid"],
            "memory_mb": round(found["mem_kb"] / 1024, 1),
            "detail": f"未找到 stability_pid.txt，按进程名匹配到 PID={found['pid']} "
                      f"（{found['name']}），内存 {found['mem_kb'] / 1024:.1f} MB",
        }
    return {
        "status": STATUS_FAIL,
        "pid": None,
        "memory_mb": 0,
        "detail": "未找到采集进程（无 PID 文件，也无匹配进程）",
    }


def _is_pid_alive(pid):
    """跨平台进程存活检查。"""
    if os.name == "nt":
        # Windows: os.kill(pid,0) 在部分版本异常，用 tasklist 判断
        try:
            out = subprocess.check_output(
                ["tasklist", "/fi", f"PID eq {pid}", "/fo", "csv", "/nh"],
                stderr=subprocess.DEVNULL,
            ).decode("gbk", errors="ignore")
            # 进程存在时输出形如 "name","pid",...；不存在时输出 "INFO: 没有..."
            return str(pid) in out
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False


def _get_pid_memory_kb(pid):
    """获取进程内存占用（KB），失败返 0。"""
    try:
        if os.name == "nt":
            out = subprocess.check_output(
                ["tasklist", "/fi", f"PID eq {pid}", "/fo", "csv", "/nh"],
                stderr=subprocess.DEVNULL,
            ).decode("gbk", errors="ignore")
            # CSV 行："imagename","pid","session","session#","mem usage"
            parts = [p.strip('"') for p in out.strip().split(",")]
            if len(parts) >= 5:
                mem_str = parts[-1].replace(",", "").replace(" K", "").strip()
                return _safe_int(mem_str, 0)
        else:
            with open(f"/proc/{pid}/status", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return _safe_int(line.split()[1], 0)
    except Exception:
        pass
    return 0


def _find_collector_process():
    """无 PID 文件时，按命令行包含 main.py / web_client.py 匹配。"""
    try:
        if os.name == "nt":
            out = subprocess.check_output(
                ["wmic", "process", "get", "ProcessId,Name,CommandLine",
                 "/format:csv"],
                stderr=subprocess.DEVNULL,
            ).decode("utf-8", errors="ignore")
            for line in out.splitlines():
                if any(k in line for k in ("main.py", "web_client.py")):
                    parts = line.strip().split(",")
                    # csv 格式: Node,CommandLine,Name,ProcessId
                    if len(parts) >= 4:
                        pid = _safe_int(parts[-1])
                        name = parts[-2]
                        mem_kb = _get_pid_memory_kb(pid)
                        return {"pid": pid, "name": name, "mem_kb": mem_kb}
        else:
            out = subprocess.check_output(["ps", "aux"], stderr=subprocess.DEVNULL
                                          ).decode("utf-8", errors="ignore")
            for line in out.splitlines():
                if any(k in line for k in ("main.py", "web_client.py")):
                    cols = line.split()
                    if len(cols) >= 11:
                        pid = _safe_int(cols[1])
                        mem_kb = int(float(cols[5]))
                        return {"pid": pid, "name": cols[10], "mem_kb": mem_kb}
    except Exception:
        pass
    return None


# ------------------------------------------------------------ 检查 2：今日弹幕
def check_danmu(data_dir):
    today = _today()
    files = [f for f in os.listdir(data_dir)
             if f.startswith("danmu_") and f.endswith(f"_{today}.jsonl")]
    if not files:
        return {
            "status": STATUS_FAIL,
            "lines": 0,
            "last_write": None,
            "detail": f"今日（{today}）无弹幕 JSONL 文件",
        }
    total_lines = 0
    latest_mtime = 0
    latest_file = ""
    for fn in files:
        path = os.path.join(data_dir, fn)
        try:
            with open(path, encoding="utf-8") as f:
                total_lines += sum(1 for _ in f)
            mt = os.path.getmtime(path)
            if mt > latest_mtime:
                latest_mtime = mt
                latest_file = fn
        except Exception:
            continue
    age_sec = time.time() - latest_mtime if latest_mtime else 0
    # 5 分钟内有写入=正常；5~30 分钟=WARN；>30 分钟=FAIL
    if age_sec < 300:
        status = STATUS_OK
    elif age_sec < 1800:
        status = STATUS_WARN
    else:
        status = STATUS_FAIL
    return {
        "status": status,
        "lines": total_lines,
        "last_write": datetime.fromtimestamp(latest_mtime).strftime("%H:%M:%S")
        if latest_mtime else None,
        "detail": f"今日弹幕 {total_lines} 行，最近写入 "
                  f"{int(age_sec)}s 前（{latest_file}）",
    }


# ------------------------------------------------------------ 检查 3：断线重连
def check_reconnect(data_dir):
    today = _today()
    log_files = [f for f in os.listdir(data_dir)
                 if f.startswith("stability_") and f.endswith(".log")
                 and today in f]
    # 也扫 err 日志
    log_files += [f for f in os.listdir(data_dir)
                  if f.startswith("stability_") and f.endswith("_err.log")
                  and today in f]
    if not log_files:
        return {
            "status": STATUS_WARN,
            "disconnect_count": 0,
            "last_reconnect_ok": None,
            "detail": f"今日（{today}）无 stability 日志，无法判断重连情况",
        }
    disconnect_count = 0
    last_reconnect_ok = None
    for fn in log_files:
        path = os.path.join(data_dir, fn)
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if any(p in line for p in RECONNECT_PATTERNS):
                        disconnect_count += 1
                    if any(p in line for p in CONNECT_OK_PATTERNS):
                        last_reconnect_ok = line.strip()[:120]
        except Exception:
            continue
    if disconnect_count == 0:
        status = STATUS_OK
        detail = "无断线记录"
    elif last_reconnect_ok:
        status = STATUS_OK
        detail = f"断线 {disconnect_count} 次，最近重连成功：{last_reconnect_ok}"
    else:
        status = STATUS_WARN
        detail = f"断线 {disconnect_count} 次，未在日志中找到重连成功记录"
    return {
        "status": status,
        "disconnect_count": disconnect_count,
        "last_reconnect_ok": last_reconnect_ok,
        "detail": detail,
    }


# ------------------------------------------------------------ 检查 4：LLM 产出
def check_llm_outputs(data_dir):
    today = _today()
    results = {}
    for prefix in ("portrait", "coldline", "llm_metrics"):
        fn = f"{prefix}_{today}.jsonl"
        path = os.path.join(data_dir, fn)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            count = 0
            try:
                with open(path, encoding="utf-8") as f:
                    count = sum(1 for line in f if line.strip())
            except Exception:
                count = 0
            results[prefix] = {"status": STATUS_OK, "count": count,
                               "detail": f"{fn}: {count} 条"}
        else:
            results[prefix] = {"status": STATUS_WARN, "count": 0,
                               "detail": f"{fn}: 今日无产出或为空"}
    overall = STATUS_OK if all(r["status"] == STATUS_OK for r in results.values()) \
        else STATUS_WARN
    return {
        "status": overall,
        "items": results,
        "detail": "；".join(r["detail"] for r in results.values()),
    }


# ------------------------------------------------------------ 检查 5：磁盘占用
def check_disk_usage(data_dir):
    total = 0
    file_count = 0
    try:
        for root, _, files in os.walk(data_dir):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                    file_count += 1
                except OSError:
                    continue
    except Exception:
        return {
            "status": STATUS_FAIL,
            "size_mb": 0,
            "file_count": 0,
            "detail": f"无法访问 data 目录: {data_dir}",
        }
    size_mb = round(total / 1024 / 1024, 1)
    # 阈值：>500MB WARN，>2GB FAIL
    if size_mb > 2048:
        status = STATUS_FAIL
    elif size_mb > 512:
        status = STATUS_WARN
    else:
        status = STATUS_OK
    return {
        "status": status,
        "size_mb": size_mb,
        "file_count": file_count,
        "detail": f"data/ 共 {file_count} 个文件，{size_mb} MB",
    }


# ------------------------------------------------------------ 检查 6：LLM 费用
def check_llm_cost(data_dir):
    usage = read_usage(data_dir, _today())
    # Optional currency estimates are not a health threshold or a money cap.
    status = STATUS_OK if usage["coverage"]["status"] == "complete" else STATUS_WARN
    return {
        "status": status,
        "prompt_tokens": usage["prompt_tokens"],
        "completion_tokens": usage["completion_tokens"],
        "cost_yuan": usage["cost_cny"],
        "usage": usage,
        "detail": "今日全局已记录用量；" + usage_text(usage),
    }


# ------------------------------------------------------------ 主流程
def run_health_check(data_dir):
    checks = {
        "采集进程": check_process(data_dir),
        "今日弹幕": check_danmu(data_dir),
        "断线重连": check_reconnect(data_dir),
        "LLM产出": check_llm_outputs(data_dir),
        "磁盘占用": check_disk_usage(data_dir),
        "LLM费用": check_llm_cost(data_dir),
    }
    overall = STATUS_OK
    for c in checks.values():
        if c["status"] == STATUS_FAIL:
            overall = STATUS_FAIL
            break
        if c["status"] == STATUS_WARN:
            overall = STATUS_WARN
    return {"overall": overall, "checks": checks, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}


def print_human(result):
    print(f"===== 健康检查报告  {result['timestamp']}  =====")
    for name, c in result["checks"].items():
        print(f"[{c['status']}] {name}: {c['detail']}")
    print(f"----- 总判定: {result['overall']} -----")


def main():
    ap = argparse.ArgumentParser(description="采集系统无人值守健康检查")
    ap.add_argument("--data", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"),
        help="data 目录路径（默认项目根/data）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = ap.parse_args()

    data_dir = os.path.abspath(args.data)
    result = run_health_check(data_dir)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_human(result)
    # 进程退出码：FAIL=2, WARN=1, OK=0
    sys.exit(0 if result["overall"] == STATUS_OK
             else (1 if result["overall"] == STATUS_WARN else 2))


if __name__ == "__main__":
    main()
