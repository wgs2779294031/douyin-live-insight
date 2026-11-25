"""冷场台词对比脚本：同一输入跑 无人设(A) vs 有人设(B) 两次，落盘对比记录。

用途（任务书 v2.3 第 6.10 节验收 / 论文个性化生成实验）：
    python scripts/compare_coldline.py --snapshot-file <快照.json> --profile <人设.json>

快照文件格式（JSON，二选一）：
    {"cold_event": {...}, "recent_topics": {...}}          # 显式两段
    {"event": "cold_start", "topics": [...], ...}          # 直接是 cold_event dict

人设文件：anchor_profiles.example/求职辅导示例.json 等任一人设卡 JSON。

输出：data/coldline_compare_YYYY-MM-DD.jsonl，每行：
    {"ts", "input", "lines_a", "lines_b", "anchor_used", "anchor_name",
     "catchphrase_hit_count", "forbidden_filtered_count"}

说明：
- A/B 两轮共用 config.json 的 llm providers（走 LLMRouter 主备逻辑）；
- A 轮为无人设（v2.2 原样 prompt），B 轮注入 --profile 人设；
- 本脚本只做一次性对比，不启采集、不写 coldline_/portrait_ 正式落盘。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm_client import LLMRouter  # noqa: E402
from llm_service import ColdlineService, load_llm_config  # noqa: E402

logger = logging.getLogger("llm.compare")


def _load_snapshot(path: str) -> "tuple[Dict[str, Any], Dict[str, Any]]":
    """读快照文件，返回 (cold_event, recent_topics)。"""
    # utf-8-sig 兼容用户用记事本/PowerShell 手写的带 BOM 文件
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"快照文件必须是 JSON 对象: {path}")
    if "cold_event" in data or "recent_topics" in data:
        cold_event = data.get("cold_event") or {}
        topics = data.get("recent_topics") or {}
    else:
        # 整个文件就是 cold_event（规则层 on_event 回调产出格式）
        cold_event = data
        topics = {
            "top_words": data.get("topics") or [],
            "recent_danmaku": [],
        }
    return cold_event, topics


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser(
        description="冷场台词 A/B 对比：无人设(A) vs 有人设(B)")
    parser.add_argument("--snapshot-file", required=True,
                        help="快照 JSON 文件路径（相对/绝对均可）")
    parser.add_argument("--profile", required=True,
                        help="人设卡 JSON 文件路径（如 anchor_profiles.example/求职辅导示例.json）")
    parser.add_argument("--config", default=os.path.join(project_root, "config.json"),
                        help="config.json 路径（默认 项目根/config.json）")
    parser.add_argument("--provider", default=None,
                        help="强制指定 provider 名（如 deepseek），默认走 config.json 的 active_provider；"
                             "主 provider 限流时可用备通道做对比实验")
    parser.add_argument("--output-dir", default=os.path.join(project_root, "data", "_ab"),
                        help="输出目录（默认 项目根/data/_ab —— 别落 data/ 根目录："
                             "看板 tail 会把它当线上冷场事件，生成 room_None 占位 tab，2026-10-28 已踩）")
    args = parser.parse_args()

    # 快照
    try:
        cold_event, topics = _load_snapshot(args.snapshot_file)
    except Exception as e:
        print(f"[错误] 读取快照失败: {e}", file=sys.stderr)
        return 1

    # 人设（完整版直接读文件，不走 room_id 目录查找）
    try:
        with open(args.profile, "r", encoding="utf-8-sig") as f:
            profile = json.load(f)
    except Exception as e:
        print(f"[错误] 读取人设失败: {e}", file=sys.stderr)
        return 1
    if not isinstance(profile, dict):
        print("[错误] 人设文件必须是 JSON 对象", file=sys.stderr)
        return 1

    # LLM 路由（复用 config.json 的 llm 节与主备逻辑）
    cfg = load_llm_config(args.config)
    if args.provider:
        p = cfg.providers.get(args.provider)
        if not p or not str(p.get("api_key") or "").strip():
            print(f"[错误] provider '{args.provider}' 不存在或未填 api_key",
                  file=sys.stderr)
            return 1
        cfg.active_provider = args.provider
    router = cfg.build_router(on_metrics=None)
    if router is None or not router.is_available:
        print("[错误] 无可用 LLM provider（请检查 config.json llm.providers 的 api_key）",
              file=sys.stderr)
        return 1

    svc = ColdlineService(router, timeout_seconds=cfg.coldline_timeout_seconds)

    logger.info("A 轮：无人设（v2.2 原样 prompt）...")
    lines_a, stats_a = svc.generate(cold_event, topics, profile=None)
    logger.info("A 轮产出 %s 条", len(lines_a))

    logger.info("B 轮：有人设 name=%s ...", profile.get("name"))
    lines_b, stats_b = svc.generate(cold_event, topics, profile=profile)
    logger.info("B 轮产出 %s 条（命中口头禅 %s，过滤 %s）",
                len(lines_b), stats_b["catchphrase_hit_count"],
                stats_b["forbidden_filtered_count"])

    record = {
        "ts": int(time.time()),
        "input": {"cold_event": cold_event, "recent_topics": topics},
        "lines_a": lines_a,
        "lines_b": lines_b,
        "anchor_used": stats_b["anchor_used"],
        "anchor_name": stats_b["anchor_name"],
        "catchphrase_hit_count": stats_b["catchphrase_hit_count"],
        "forbidden_filtered_count": stats_b["forbidden_filtered_count"],
    }
    os.makedirs(args.output_dir, exist_ok=True)
    out_path = os.path.join(
        args.output_dir,
        f"coldline_compare_{time.strftime('%Y-%m-%d', time.localtime())}.jsonl")
    with open(out_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"[OK] 已写入 {out_path}")
    print(f"  A(无人设): {lines_a}")
    print(f"  B(有人设 {stats_b['anchor_name']}): {lines_b}")
    print(f"  B 轮口头禅命中 {stats_b['catchphrase_hit_count']} 条，"
          f"禁区过滤丢弃 {stats_b['forbidden_filtered_count']} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
