# -*- coding: utf-8 -*-
"""移植准备测试：路径解析不再绑定本机绝对路径（纯函数，零网络）。"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import run_watchdog as rw  # noqa: E402
import watch_anchor_live as wal  # noqa: E402


def test_resolve_cdp_prefers_env(tmp_path):
    f = tmp_path / "my-cdp.mjs"
    f.write_text("//x", encoding="utf-8")
    assert wal.resolve_cdp({"WATCH_CDP": str(f)}) == str(f)


def test_resolve_cdp_falls_back_to_in_repo_copy():
    """不设环境变量时用仓内自持副本（移植后不依赖 WorkBuddy 技能目录）。"""
    got = wal.resolve_cdp({})
    assert pathlib.Path(got).exists()
    assert got.endswith("cdp.mjs")


def test_resolve_node_prefers_env(tmp_path):
    f = tmp_path / "node.exe"
    f.write_bytes(b"x")
    assert wal.resolve_node({"WATCH_NODE": str(f)}) == str(f)


def test_resolve_node_returns_existing_path():
    """无环境变量时命中 PATH 或本机已知位置——无论哪条，返回值必须真实存在。"""
    got = wal.resolve_node({})
    assert pathlib.Path(got).exists(), f"解析出的 node 不存在: {got}"


def test_resolve_profile_env_overrides_default():
    assert rw.resolve_profile({"WATCH_BROWSER_PROFILE": "/x/prof"}) == "/x/prof"


def test_resolve_profile_platform_default():
    got = rw.resolve_profile({})
    if sys.platform == "win32":
        # 2026-10-28 起默认浏览器是 Edge，profile 跟着走（exe 与 profile 必须成对）
        want = ("D:/wb-edge-profile" if rw.browser_name({}) == "edge"
                else "D:/wb-browser-profile")
        assert got == want
    else:
        assert got.endswith("browser-profile")    # 云/Linux 放仓库内


def test_browser_name_default_prefers_edge():
    """本机装了 Edge 就默认 Edge —— 32 位 Chrome 有 2 GB 地址空间硬上限。"""
    has_edge = any(pathlib.Path(p).exists() for p in rw._BROWSER_CANDIDATES["edge"])
    assert rw.browser_name({}) == ("edge" if has_edge else "chrome")


def test_browser_name_env_still_wins():
    """WATCH_BROWSER 仍是显式开关（回滚手段）。"""
    assert rw.browser_name({"WATCH_BROWSER": "chrome"}) == "chrome"
    assert rw.browser_name({"WATCH_BROWSER": "EDGE"}) == "edge"
    assert rw.browser_name({"WATCH_BROWSER": "safari"}) in ("chrome", "edge")  # 乱填不生效
