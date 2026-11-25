# -*- coding: utf-8 -*-
"""stream_recorder.resolve_node 不能靠 `where node` 的输出来取值（2026-11-01 审计 P0）。

原实现把 shell 探测塞进了候选列表：

    cand = [os.environ.get("WATCH_NODE"),
            r"...22.22.2-6\node.exe",
            r"...22.22.2-3\node.exe",
            os.popen("where node").read().strip().splitlines()[0] if os.name == "nt" else "node"]

列表元素是**先全部求值再赋值**的，所以只要 `where node` 输出为空（PATH 里没有 node /
装的是 nvm 还没激活 / PATH 被 launcher 截断），`splitlines()[0]` 直接 IndexError，
`resolve_node()` 整个炸掉 —— 而它被调用在录制启动路径上，等于「一台没配 node 的机器
连报错都报成崩溃栈」。本机两个硬编码候选存在，所以永远走不到那一步，问题只在别人
机器上出现（移植准备的直接障碍）。

修法：`shutil.which("node")`（与 watch_anchor_live.resolve_node 同款），候选顺序照旧。
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import stream_recorder as sr  # noqa: E402


class _EmptyOutput:
    """`os.popen(...).read()` 的空输出替身。"""

    def read(self):
        return ""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_empty_where_output_does_not_crash(monkeypatch):
    monkeypatch.delenv("WATCH_NODE", raising=False)
    monkeypatch.setattr(sr.os, "popen", lambda *a, **k: _EmptyOutput())
    monkeypatch.setattr(sr.shutil, "which", lambda *a, **k: None)

    got = sr.resolve_node()          # 原实现这里 IndexError
    assert isinstance(got, str) and got


def test_does_not_shell_out_to_where(monkeypatch):
    """探测一律走 shutil.which，不再拉 cmd 子进程（无窗形态下 shell 调用更不该有）。"""
    calls = []
    monkeypatch.setattr(sr.os, "popen",
                        lambda *a, **k: calls.append(a) or _EmptyOutput())
    sr.resolve_node()
    assert calls == []


def test_watch_node_env_wins(monkeypatch):
    monkeypatch.setenv("WATCH_NODE", sys.executable)

    def _boom(*a, **k):
        raise AssertionError("WATCH_NODE 已给出且文件存在，不该再往下探测")

    monkeypatch.setattr(sr.shutil, "which", _boom)
    assert sr.resolve_node() == sys.executable


def test_falls_back_to_which(monkeypatch, tmp_path):
    """硬编码候选都不存在时，用 which 找到的那个。"""
    fake = tmp_path / "node.exe"
    fake.write_text("", encoding="utf-8")
    monkeypatch.delenv("WATCH_NODE", raising=False)
    monkeypatch.setattr(sr, "_NODE_FALLBACKS", [])
    monkeypatch.setattr(sr.shutil, "which", lambda *a, **k: str(fake))

    assert sr.resolve_node() == str(fake)
