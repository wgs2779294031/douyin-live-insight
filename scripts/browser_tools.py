"""Portable paths for the repository's CDP helpers; importing performs no I/O."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def node_binary() -> str:
    return os.environ.get("NODE_BIN") or os.environ.get("WATCH_NODE") or shutil.which("node") or "node"


def cdp_script() -> Path:
    """Use the bundled bridge, with an explicit per-machine override if needed."""
    override = os.environ.get("CDP_SCRIPT")
    return Path(override).expanduser().resolve() if override else ROOT / "scripts" / "cdp.mjs"


def captures_dir() -> Path:
    override = os.environ.get("WB_CAPTURES_DIR")
    return Path(override).expanduser().resolve() if override else ROOT / "data" / "captures"


def resolve_capture(path: str) -> Path:
    p = Path(path)
    if p.is_file():
        return p
    if not p.is_absolute():
        for candidate in (captures_dir() / p,
                          Path(".browser_bridge") / "captures" / p.name):
            if candidate.is_file():
                return candidate
    raise FileNotFoundError("抓包文件不存在；请提供完整路径或设置 WB_CAPTURES_DIR")
