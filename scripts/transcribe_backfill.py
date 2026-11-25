#!/usr/bin/env python
"""录制分片 → 主播原话 jsonl（主播原话工程化 · 第 2 步）。

输入：``data/audio_<tag>_<YYYY-MM-DD>/seg_NNNN.flv``（stream_recorder.py 产出，音频轨 only）
输出：``data/speech_<tag>_<YYYY-MM-DD>.jsonl``，一行一句，**与弹幕 jsonl 同构**：

    {"ts": 1792400201.4, "time": "2026-10-19 16:56:41", "text": "……", "dur": 3.2,
     "tag": "研途-张老师团队", "seg": "seg_0001", "off": 12.5}

- `ts` = 绝对墙钟秒（= 分片起始墙钟 + 句内偏移），和 danmu 的 ts 可直接对齐做交叉分析
- `dur` = 该句时长（秒）
- `off` = 在分片内的偏移（秒），需要回听原片时用 `seg`+`off`

流程：分片 flv 直接喂 faster-whisper（PyAV 解码，内部统一到 16k mono）→ 落 jsonl；
      解不动时才用 ffmpeg 抽 flac 兜底（本机 ffmpeg 是 QQ 浏览器裁剪版，无 pcm 编码器，不能出 wav）。
幂等：写者发布的闭合来源、每片原子规范结果及可恢复的 speech 投影共同验证；
      ``--force`` 新代次成功前保留旧结果和文本，未知旧数据保守拒写。

模型：``--model`` 显式值优先；auto 时先用 ``WB_ASR_MODEL`` 指定的完整本地目录，
      未设置时兼容旧目录 ``D:/wb-bin/models/faster-whisper-small`` / small 默认。
      显式本地路径或环境目录无效/加载失败会报错，不静默回退联网下载。

用法
----
    python scripts/transcribe_backfill.py --list                     # 看还有哪些没转
    python scripts/transcribe_backfill.py --tag 框框看升学 --date 2026-10-19
    python scripts/transcribe_backfill.py --all                      # 全部补齐
    python scripts/transcribe_backfill.py --tag X --date Y --force   # 重做
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.local_installation import bootstrap_entry, installed_layout
bootstrap_entry()
from file_lock import FileLock
from scripts.local_context import activate_customer_environment, load_customer_context
from scripts import transcript_results as durable

CUSTOMER = load_customer_context()
activate_customer_environment(CUSTOMER)

# 模型缓存/下载走国内源（必须在 import huggingface_hub 之前设）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
pass  # Use explicit HF_HOME or the standard Hugging Face cache.


def _fix_stdio() -> None:
    """Windows 下 stdout 默认按 locale（cp936）编码，`print("▶ ...")` 会直接抛
    UnicodeEncodeError 把整个进程打断。

    2026-10-26 实测：stream_recorder 的 `spawn_transcribe()` 起子进程时没传
    PYTHONIOENCODING，子进程按 GBK 写日志 → 每场直播下播即转的任务**在打印第
    一个 `▶` 时就崩了**，一天 5 场 643 MB 音频一片都没转出去，而日志里只留一行
    traceback（外层只看到 exit=1）。

    这里只放宽 errors（不换 encoding）：真控制台仍是 GBK，中文照常显示，
    个别符号退化成 `?`；重定向到 utf-8 文件时行为不变。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")   # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


def _tee_log(path: Path) -> None:
    """把 stdout/stderr 同时追加进日志文件。

    计划任务改成直调 `python.exe` 后没有 shell 的 `>>` 重定向（原来靠
    `transcribe_all.bat`，但那个 bat 里的中文路径写成了 GBK 乱码，`cd` 一直失败，
    每天 02:30 的任务其实从未真正跑过）。有了 tee，不管谁调用都留得下日志。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        return

    class _Tee:
        def __init__(self, *streams: Any) -> None:
            self.streams = [s for s in streams if s is not None]

        def write(self, s: str) -> int:
            for st in self.streams:
                try:
                    st.write(s)
                except Exception:
                    pass
            return len(s)

        def flush(self) -> None:
            for st in self.streams:
                try:
                    st.flush()
                except Exception:
                    pass

        def isatty(self) -> bool:
            return False

        def __getattr__(self, name: str) -> Any:
            return getattr(self.streams[0], name)

    sys.stdout = _Tee(sys.stdout, fh)   # type: ignore[assignment]
    sys.stderr = _Tee(sys.stderr, fh)   # type: ignore[assignment]


_run_lock: Optional[FileLock] = None


def _release_run_lock() -> None:
    global _run_lock
    if _run_lock is not None:
        _run_lock.release()
        _run_lock = None


def _acquire_run_lock() -> bool:
    """全局单实例锁：`--all` 是从 02:30 定时任务和「下播即转」两条路进来的，
    两路撞上会同时写同一个 speech_*.jsonl / .transcribed.json，把进度文件写坏。
    拿不到锁返回明确busy；--list / --verify 不占锁、不加载模型。
    """
    global _run_lock
    if _run_lock is not None:
        return False
    lock = FileLock(DATA / ".transcribe.run", timeout=0)
    if not lock.acquire(blocking=False):
        log("⏭ 已有转写进程在跑，本次退出以免并发写坏进度")
        return False
    _run_lock = lock
    import atexit
    atexit.register(_release_run_lock)
    return True

DATA = Path(CUSTOMER["data"]) if CUSTOMER else ROOT / "data"
LOCAL_MODEL = ROOT / "models" / "faster-whisper-small" if CUSTOMER else Path(r"D:/wb-bin/models/faster-whisper-small")
FFMPEG_CANDIDATES = [os.environ.get("FFMPEG_PATH")] if installed_layout() is not None else [
    os.environ.get("FFMPEG_PATH"),
    None if CUSTOMER else r"D:/wb-bin/ffmpeg/bin/ffmpeg.exe",
    shutil.which("ffmpeg"),
]


def find_ffmpeg() -> Optional[str]:
    installed = installed_layout()
    if installed is not None:
        selected = installed["ffmpeg"]
        return str(selected) if selected.is_file() else None
    for c in FFMPEG_CANDIDATES:
        if c and Path(c).exists():
            return str(c)
    return None


def log(msg: str) -> None:
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


# ------------------------------------------------------------------ 目录发现
def list_audio_dirs(tag: Optional[str] = None, date: Optional[str] = None) -> List[Path]:
    out = []
    for d in sorted(DATA.glob("audio_*_*")):
        if not d.is_dir():
            continue
        # audio_<tag>_<YYYY-MM-DD>
        try:
            ddate = d.name[-10:]
            datetime.strptime(ddate, "%Y-%m-%d")
        except Exception:
            continue
        dtag = d.name[len("audio_"):-11]
        if tag and dtag != tag:
            continue
        if date and ddate != date:
            continue
        out.append(d)
    return out


def dir_tag_date(d: Path) -> Tuple[str, str]:
    return d.name[len("audio_"):-11], d.name[-10:]


# ------------------------------------------------------------------ ffmpeg
def extract_flac(ffmpeg: str, src: Path, dst: Path) -> bool:
    """兜底抽音轨。

    ⚠️ 不用 wav：本机可直接调用的 ffmpeg 来自 QQ 浏览器（--disable-everything 裁剪版），
       **没有 pcm_s16le 编码器**，`-f wav` 必失败；flac 编码器在，且无损。
    """
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
           "-vn", "-ac", "1", "-ar", "16000", "-f", "flac", str(dst)]
    try:
        cp = subprocess.run(cmd, capture_output=True, timeout=300,
                            encoding="utf-8", errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return False
    if cp.returncode != 0 or not dst.exists() or dst.stat().st_size < 1024:
        log("  ⚠️ ffmpeg 抽音频失败 %s: %s" % (src.name, (cp.stderr or "")[:160]))
        return False
    return True


# ------------------------------------------------------------------ 转写
def _local_model_directory(value: str, source: str) -> str:
    # C://models is a local Windows drive path, not a URL scheme.
    windows_absolute = re.match(r"^[A-Za-z]:[\\/]", value)
    uri_scheme = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value)
    if not value.strip() or value.startswith(("\\\\", "//")) or (uri_scheme and not windows_absolute):
        raise ValueError("%s 必须指向本地模型目录，不能留空或使用网络地址" % source)
    try:
        directory = Path(value).expanduser()
        valid_directory = directory.is_dir()
        required = ("model.bin", "config.json", "tokenizer.json")
        missing = [name for name in required if not (directory / name).is_file()
                   or (directory / name).stat().st_size == 0] if valid_directory else []
    except (OSError, ValueError, RuntimeError):
        raise ValueError("%s 本地模型目录无法访问，拒绝回退下载" % source) from None
    if not valid_directory:
        raise ValueError("%s 指定的本地模型目录不存在或不是目录" % source)
    if missing:
        # faster-whisper 缺 tokenizer.json 时会 from_pretrained；仅 local_files_only 不够。
        raise ValueError("%s 本地模型缺少非空文件：%s" % (source, ", ".join(missing)))
    return str(directory)


def _is_explicit_local_model(value: str) -> bool:
    # An unprefixed org/model may be a Hugging Face ID. Use ./org/model to
    # unambiguously request a relative local path, including when it is missing.
    if re.match(r"^[A-Za-z]:", value) or value.startswith(("/", "\\", "~", "./", "../")) or "\\" in value:
        return True
    try:
        return Path(value).exists()
    except (OSError, ValueError):
        raise ValueError("--model 路径无法访问，拒绝回退下载") from None


def load_model(model_arg: str, device: str = "cpu", compute_type: str = "int8"):
    path = model_arg
    local_source = ""
    if model_arg == "auto":
        if "WB_ASR_MODEL" in os.environ:
            local_source = "WB_ASR_MODEL"
            path = _local_model_directory(os.environ[local_source].strip(), local_source)
        elif CUSTOMER:
            local_source = "customer local model"
            path = _local_model_directory(str(LOCAL_MODEL), local_source)
        else:
            path = str(LOCAL_MODEL) if LOCAL_MODEL.exists() else "small"
    elif _is_explicit_local_model(model_arg):
        local_source = "--model"
        path = _local_model_directory(model_arg, local_source)
    from faster_whisper import WhisperModel
    log("加载模型 %s（%s/%s）…" % (path, device, compute_type))
    t0 = time.time()
    options = {"local_files_only": True} if local_source else {}
    try:
        m = WhisperModel(path, device=device, compute_type=compute_type, **options)
    except Exception as exc:
        if local_source:
            raise RuntimeError("%s 本地模型加载失败，未回退或下载其他模型" % local_source) from exc
        raise
    log("模型就绪，用时 %.1fs" % (time.time() - t0))
    return m


def decode_audio_16k_mono(path: Path):
    """自己解码成 16k mono float32 —— 不走 faster_whisper.decode_audio。

    ⚠️ faster-whisper 1.2.1 调用 `av.open(..., metadata_errors="ignore")`，PyAV 19 已删这个
       参数（TypeError）。而 av 14 没有 cp313 轮子（要从源码编）。→ 自己用 PyAV 解码，
       版本无关，顺手省掉一次临时文件。
    """
    import av
    import numpy as np

    pcm_parts = []
    with av.open(str(path)) as container:
        if not container.streams.audio:
            raise RuntimeError("无音频轨")
        stream = container.streams.audio[0]
        res = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in container.decode(stream):
            for out in res.resample(frame):
                pcm_parts.append(out.to_ndarray().reshape(-1))
        try:
            for out in res.resample(None):
                pcm_parts.append(out.to_ndarray().reshape(-1))
        except Exception:
            pass
    if not pcm_parts:
        raise RuntimeError("解不出音频帧")
    pcm = np.concatenate(pcm_parts)
    return pcm.astype("float32") / 32768.0


try:                      # 繁→简（whisper 中文偶尔吐繁体；缺包不影响主流程）
    import zhconv
except Exception:         # pragma: no cover
    zhconv = None


def to_simplified(text: str) -> str:
    if not zhconv or not text:
        return text
    try:
        return zhconv.convert(text, "zh-cn")
    except Exception:
        return text


DEFAULT_PROMPT = ("以下是普通话直播内容，请用简体中文输出。常见词：专业、院校、就业、考研、"
                  "公费师范生、提前批、调剂、保研、志愿填报、分数、位次。")


def transcribe_audio(model, path: Path, prompt: str = DEFAULT_PROMPT):
    pcm = decode_audio_16k_mono(path)
    audio_sec = len(pcm) / 16000.0
    segments, info = model.transcribe(
        pcm, language="zh", task="transcribe",
        initial_prompt=prompt or None,
        beam_size=5, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        condition_on_previous_text=False,   # 分段独立，防幻觉滚雪球
        without_timestamps=False,
    )
    out = []
    for s in segments:
        text = (s.text or "").strip()
        if not text:
            continue
        out.append({"start": round(float(s.start), 2), "end": round(float(s.end), 2),
                    "text": to_simplified(text)})
    return out, audio_sec


# ------------------------------------------------------------------ 主流程
class TranscriptionError(RuntimeError):
    def __init__(self, completed: int, failed: List[str]) -> None:
        self.completed = completed
        self.failed = failed
        super().__init__("%d 项未完成（分片或投影），保留材料供明确重试" % len(failed))


class _LazyModel:
    """A projection-only repair must not load a model or locate ffmpeg."""
    def __init__(self, name, device, compute_type):
        self.settings = name, device, compute_type
        self.model = None

    def get(self):
        if self.model is None:
            self.model = load_model(*self.settings)
        return self.model


def _directory_status(folder):
    status = durable.directory_status(DATA, folder)
    tag, day = dir_tag_date(folder)
    legacy = (DATA / ("speech_%s_%s.jsonl" % (tag, day)),
              folder / ".transcribed.json", folder / ".transcribed.json.rebuild.pending")
    if not status and any(path.exists() or path.is_symlink() for path in legacy):
        raise durable.ResultError("legacy_conflict")
    return status


def transcribe_dir(model, d: Path, ffmpeg: Optional[str], force: bool, model_name: str,
                  prompt: str = DEFAULT_PROMPT) -> int:
    # Also protect direct Python callers. FileLock is reentrant in the CLI's
    # owning thread, using the same permanent global sidecar in every entry.
    lock = FileLock(DATA / ".transcribe.run", timeout=0)
    if not lock.acquire(blocking=False):
        raise durable.WriterBusy("busy")
    try:
        status = _directory_status(d)
        completed, failed = 0, []

        def recognize(path):
            actual_model = model.get() if isinstance(model, _LazyModel) else model
            try:
                return transcribe_audio(actual_model, path, prompt)
            except Exception as error:
                log("  ⚠️ 直接解码失败（%s），转 ffmpeg 兜底" % str(error)[:80])
                fallback = find_ffmpeg() if isinstance(model, _LazyModel) else ffmpeg
                with tempfile.TemporaryDirectory(prefix="tr_", dir=str(d)) as td:
                    flac = Path(td) / "a.flac"
                    if not fallback or not extract_flac(fallback, path, flac):
                        raise RuntimeError("ffmpeg 兜底不可用或抽取失败") from error
                    return transcribe_audio(actual_model, flac, prompt)

        for item in status:
            name = item["name"]
            result = durable.process_segment(DATA, d, name, recognize, model_name, force=force)
            completed += bool(result.get("completed_now"))
            if result.get("state") not in ("completed", "reused"):
                failed.append(name)
                log("  ✗ %s: %s" % (name, result.get("reason", result.get("state", "unknown"))))
        try:
            projection = durable.rebuild_projection(DATA, d)
            if not projection.get("projection_ready"):
                failed.append("projection:" + projection.get("reason", "unconfirmed"))
        except (OSError, ValueError, durable.ResultError) as error:
            failed.append("projection:" + str(error))
        if failed:
            raise TranscriptionError(completed, failed)
        log("  ✓ 本次提交%d片，兼容投影已验证；未提交片不计成功" % completed)
        return completed
    finally:
        lock.release()


def verify_speech(tag: Optional[str], date: Optional[str]) -> int:
    """Read-only source/result/projection verification, never format-only success."""
    ok, known = True, set()
    for folder in list_audio_dirs(tag, date):
        folder_tag, folder_date = dir_tag_date(folder)
        known.add("speech_%s_%s.jsonl" % (folder_tag, folder_date))
        try:
            status = _directory_status(folder)
            for item in status:
                print(json.dumps({key: value for key, value in item.items() if key != "closed"}, ensure_ascii=False))
                ok = ok and bool(item.get("result_committed") and item.get("projection_ready")
                                 and item.get("current_result"))
        except (OSError, ValueError, durable.ResultError) as error:
            print("来源/结果无法验证：%s" % error)
            ok = False
    for path in DATA.glob("speech_*.jsonl"):
        if ((not tag or path.name.startswith("speech_%s_" % tag))
                and (not date or path.stem.endswith(date)) and path.name not in known):
            print("legacy_conflict: 未确认来源的旧speech保留，不能作为完成或清理依据：%s" % path.name)
            ok = False
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="录制分片 → 主播原话 jsonl")
    ap.add_argument("--tag", default=None)
    ap.add_argument("--date", default=None)
    ap.add_argument("--all", action="store_true", help="处理所有 audio_* 目录")
    ap.add_argument("--list", action="store_true", help="只列待转清单")
    ap.add_argument("--verify", action="store_true", help="体检已产出的 speech_*.jsonl")
    ap.add_argument("--force", action="store_true", help="重做（重建 speech 文件）")
    ap.add_argument("--model", default="auto", help="auto=WB_ASR_MODEL/旧本地 small；显式模型名或路径优先")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--compute-type", default="int8")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT, help="引导词（压繁体/补领域词），传空串关闭")
    args = ap.parse_args()

    if args.verify:
        return verify_speech(args.tag, args.date)

    dirs = list_audio_dirs(args.tag, args.date)
    if not dirs:
        print("没有匹配的 audio_* 目录")
        return 0

    if args.list:
        total_pending = 0
        for d in dirs:
            try:
                status = _directory_status(d)
            except (OSError, ValueError, durable.ResultError) as exc:
                log("无法列出来源/结果状态（材料保留）：%s" % exc)
                return 1
            for item in status:
                print(json.dumps(dict(directory=d.name, **{key: value for key, value in item.items() if key != "closed"}), ensure_ascii=False))
                total_pending += not bool(item.get("result_committed") and item.get("projection_ready")
                                          and item.get("current_result"))
        print("未确认完成合计 %d 片（含来源未知/投影待恢复；不自动派发）" % total_pending)
        return 0

    if not (args.all or args.tag or args.date):
        ap.error("需要 --tag / --date / --all 之一（或用 --list 先看）")

    try:
        if not _acquire_run_lock():
            log("busy: 未取得全局writer，本次没有分片完成结论")
            return 3
    except OSError as exc:
        log("⛔ 无法取得转写锁，未开始转写：%s" % exc)
        return 2

    try:
        model = _LazyModel(args.model, args.device, args.compute_type)
        model_name = args.model if args.model != "auto" else os.environ.get("WB_ASR_MODEL", str(LOCAL_MODEL))
        grand = 0
        failed_dirs = 0
        for d in dirs:
            tag, date = dir_tag_date(d)
            log("▶ %s / %s" % (tag, date))
            try:
                grand += transcribe_dir(model, d, None, args.force, model_name, args.prompt)
            except TranscriptionError as exc:
                grand += exc.completed
                failed_dirs += 1
                log("  ✗ %s" % exc)
            except durable.WriterBusy:
                log("busy: 未取得全局writer，无完成结论")
                return 3
            except (OSError, ValueError, durable.ResultError) as exc:
                failed_dirs += 1
                log("  ✗ 转写未完成，原进度保留：%s" % exc)
        if failed_dirs:
            log("未全部完成，本次转写 %d 片，%d 个目录失败（退出码 1）" % (grand, failed_dirs))
            return 1
        log("完成，本次转写 %d 片" % grand)
        return 0
    finally:
        _release_run_lock()


if __name__ == "__main__":
    _fix_stdio()
    _tee_log(DATA / "transcribe.log")
    sys.exit(main())
