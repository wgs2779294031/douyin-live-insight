"""The one admitted customer ASR process; no work before parent acceptance."""
from __future__ import annotations

import os
from pathlib import Path
import re
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import asr_protocol as protocol
from scripts import audio_segments as audio
from scripts.local_context import activate_customer_environment, load_customer_context, publish_role_ready


class ModelUnavailable(RuntimeError):
    reason = "model_unavailable"


class DecodeFailed(RuntimeError):
    reason = "decode_failed"


class ModelAdapter:
    def __init__(self, context):
        # Importing the existing decoder is also deferred until admission.
        from scripts import transcribe_backfill as transcriber
        self.transcriber = transcriber
        self.context = context
        self.model = None
        self.model_name = os.environ.get("WB_ASR_MODEL", str(Path(context["root"]) / "models/faster-whisper-small"))

    def signature(self):
        value = self.model_name.strip()
        if value.startswith(("\\\\", "//")) or (re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value)
                                                  and not re.match(r"^[A-Za-z]:[\\/]", value)):
            return ["invalid_model_path"]
        directory = Path(value).expanduser()
        if not directory.is_absolute():
            directory = Path(self.context["root"]) / directory
        token = [str(directory)]
        for name in ("model.bin", "config.json", "tokenizer.json"):
            try:
                info = (directory / name).stat()
                token.append([info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns])
            except (OSError, ValueError):
                token.append(None)
        return token

    def prepare(self):
        if self.model is None:
            try:
                # An imported legacy module must never turn this customer's
                # missing model into a model-name/network fallback.
                local = self.transcriber._local_model_directory(self.model_name.strip(), "customer local model")
                self.model = self.transcriber.load_model(local, "cpu", "int8")
            except Exception as error:
                raise ModelUnavailable("model_unavailable") from error

    def recognize(self, source):
        if self.model is None:
            raise ModelUnavailable("model_unavailable")
        try:
            return self.transcriber.transcribe_audio(self.model, source)
        except Exception:
            # This subprocess, if needed, remains in the held private Job.
            try:
                ffmpeg = self.transcriber.find_ffmpeg()
                if not ffmpeg:
                    raise DecodeFailed("decode_failed")
                with tempfile.TemporaryDirectory(prefix="decode-", dir=protocol.paths(self.context)["directory"]) as temporary:
                    flac = Path(temporary) / "audio.flac"
                    if not self.transcriber.extract_flac(ffmpeg, source, flac):
                        raise DecodeFailed("decode_failed")
                    return self.transcriber.transcribe_audio(self.model, flac)
            except Exception as error:
                raise DecodeFailed("decode_failed") from error


def empty_summary(state, error=""):
    return {"state": state, "counts": {key: None for key in
            ("pending", "projection_pending", "retryable", "blocked", "complete")},
            "scan": {"partial": True, "limited": False, "checked": 0, "last_complete_at": None,
                     "counts_scope": "recent_window", "sample_limit": 256},
            "current": {"tag": "", "date": "", "segment": "", "phase": "waiting_admission", "attempt": 0},
            "error": error, "next_retry_at": None, "last_progress_at": None, "last_completed_at": None}


class Publisher:
    def __init__(self, context, nonce, parent, worker, scope, wall_time=time.time):
        self.context, self.nonce, self.parent, self.worker, self.scope = context, nonce, parent, worker, scope
        self.sequence, self.failed, self.wall_time = 0, False, wall_time

    def __call__(self, summary):
        self.sequence += 1
        try:
            if audio.data_scope(self.context["data"]) != self.scope:
                raise protocol.ProtocolError("data_scope_changed")
            record = protocol.envelope(self.context, self.nonce, self.parent, self.worker, "asr_status", self.scope)
            record.update(sequence=self.sequence, updated_at=self.wall_time(), summary=summary)
            protocol.write_record(protocol.paths(self.context)["status"], record)
            return True
        except (OSError, ValueError):
            self.failed = True
            return False


def run(context, *, engine_factory=None, adapter_factory=ModelAdapter, permission_fn=None,
        ready_fn=publish_role_ready, sleep=time.sleep, monotonic=time.monotonic, wall_time=time.time):
    """Test seams are Python callables; the production CLI exposes no bypass."""
    activate_customer_environment(context)
    scope = protocol.ensure(context, expected_scope=protocol.scope_from_environment())
    parent = protocol.parent_from_environment()
    nonce = os.environ.get("DY_CUSTOMER_ROLE_NONCE", "")
    if audio.data_scope(context["data"]) != scope:
        raise protocol.ProtocolError("data_scope_changed")
    worker = ready_fn(context, "asr", "global")
    if not protocol.identity_valid(worker) or not nonce or len(nonce) > 128:
        raise protocol.ProtocolError("identity_unavailable")
    publisher = Publisher(context, nonce, parent, worker, scope, wall_time)
    permission_fn = protocol.permission if permission_fn is None else permission_fn
    state = "waiting"
    def permission():
        nonlocal state
        try:
            state = permission_fn(context, nonce, parent, worker, scope)
        except Exception:
            state = "unknown"
        return state
    publisher(empty_summary("starting"))
    last_publish = monotonic()
    while permission() != "admitted":
        if state == "stop" or publisher.failed:
            return 0 if state == "stop" else 2
        if monotonic() - last_publish >= 2:
            publisher(empty_summary("starting" if state == "waiting" else "identity_unknown",
                                    "" if state == "waiting" else "admission_unavailable"))
            last_publish = monotonic()
        sleep(.2)
    engine = None
    try:
        # No scanner, retry file, model adapter or new attempt exists before here.
        adapter = adapter_factory(context)
        if engine_factory is None:
            from scripts.asr_sweep import SweepEngine
            engine_factory = SweepEngine
        from scripts.audio_storage import StorageSummary
        storage = StorageSummary(context["data"], scope=scope, monotonic=monotonic, wall_time=wall_time)
        should_stop = lambda: publisher.failed or permission() != "admitted"
        engine = engine_factory(Path(context["data"]), recognize=adapter.recognize,
            prepare_model=adapter.prepare, should_stop=should_stop, publish=publisher,
            monotonic=monotonic, wall_time=wall_time, model_name=adapter.model_name,
            state_path=protocol.paths(context)["retry"], dependency_token=getattr(adapter, "signature", lambda: None),
            cleanup_enabled=True, storage=storage)
        control_sequence = 0
        def control_pending():
            nonlocal control_sequence
            try:
                control = protocol.read_record(protocol.paths(context)["control"])
                if (protocol.matches(control, context, nonce, parent, worker, "asr_control", scope)
                        and control.get("action") == "retry" and type(control.get("sequence")) is int
                        and control_sequence < control["sequence"] <= 2 ** 53 - 1):
                    control_sequence = control["sequence"]
                    engine.retry()
                    return True
            except (OSError, ValueError):
                pass  # Unverifiable control never authorizes a retry or changes facts.
            return False
        while not should_stop():
            control_pending()
            storage.poll_disk()
            delay = engine.step()
            until = monotonic() + max(.05, min(10.0, float(delay)))
            while monotonic() < until and not should_stop():
                if control_pending():
                    break
                sleep(min(.2, max(.01, until - monotonic())))
        summary = engine.snapshot()
        summary.update(state="storage_error" if publisher.failed else "stopping" if state == "stop" else "identity_unknown",
                       error="status_unavailable" if publisher.failed else "" if state == "stop" else "admission_unavailable")
        publisher(summary)
        return 0 if state == "stop" else 2
    except Exception:
        publisher(empty_summary("storage_error", "worker_error"))
        return 2
    finally:
        if engine is not None:
            engine.close()


def main():
    if len(sys.argv) != 1:
        raise protocol.ProtocolError("unsupported_arguments")
    context = load_customer_context()
    if context is None:
        raise protocol.ProtocolError("customer_context_required")
    return run(context)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError):
        print("ASR worker could not verify its customer runtime", flush=True)
        raise SystemExit(2)
