"""Thread/process locks for a resource, using a permanent ``<resource>.lock`` sidecar.

The sidecar must not be unlinked: replacing it would let two processes lock
different files. The operating system releases the lock when a process exits.
All writers of a protected resource must cooperate through this module.
"""
from __future__ import annotations

import errno
import math
import os
import threading
import time
from contextlib import contextmanager
from typing import Iterator


class _State:
    def __init__(self) -> None:
        self.gate = threading.RLock()
        self.depth = 0
        self.handle = None


_states: dict[str, _State] = {}
_states_guard = threading.Lock()


def _after_fork() -> None:
    global _states, _states_guard
    # A child must not keep an inherited descriptor holding its parent's flock.
    for state in _states.values():
        if state.handle is not None:
            try:
                state.handle.close()
            except OSError:
                pass
    _states = {}
    _states_guard = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def _state_for(path: str) -> _State:
    with _states_guard:
        return _states.setdefault(path, _State())


def _try_os_lock(handle) -> bool:
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            return False
        raise


def _os_unlock(handle) -> None:
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class FileLock:
    """Lock a resource path; timeout covers both thread and process contention.

    ``acquire(blocking=False)`` returns False on contention. Blocking acquisition
    raises TimeoutError on expiry. I/O/permission errors always propagate.
    Nested locks of the same canonical path in one thread are reentrant, even
    when they use separate FileLock instances. Release must run in that thread.
    """

    def __init__(self, path, *, timeout: float = 10.0) -> None:
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("lock timeout must be finite and nonnegative")
        resource = os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))
        self.path = resource + ".lock"
        self.timeout = timeout
        self._state = None
        self._depth = 0
        self._owner = None

    def acquire(self, blocking: bool = True) -> bool:
        if self._owner is not None and self._owner[0] != os.getpid():
            # Reusing an inherited object must acquire a fresh child-process lock.
            self._state, self._depth, self._owner = None, 0, None
        deadline = time.monotonic() + self.timeout
        state = _state_for(self.path)
        if blocking:
            got_gate = state.gate.acquire(timeout=max(0.0, deadline - time.monotonic()))
        else:
            got_gate = state.gate.acquire(blocking=False)
        if not got_gate:
            if blocking:
                raise TimeoutError("timed out acquiring file lock: %s" % self.path)
            return False
        handle = None
        try:
            if state.depth == 0:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                handle = open(self.path, "a+b", buffering=0)
                # msvcrt locks one byte from the current file position. Existing
                # legacy PID text is harmless; never truncate or remove it.
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b"\0")
                while not _try_os_lock(handle):
                    if not blocking:
                        handle.close()
                        state.gate.release()
                        return False
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("timed out acquiring file lock: %s" % self.path)
                    time.sleep(min(0.02, remaining))
                state.handle = handle
            state.depth += 1
            self._state = state
            self._depth += 1
            self._owner = (os.getpid(), threading.get_ident())
            return True
        except BaseException:
            try:
                if handle is not None:
                    handle.close()
            finally:
                state.gate.release()
            raise

    def release(self) -> None:
        if not self._depth:
            return
        if self._owner != (os.getpid(), threading.get_ident()):
            raise RuntimeError("file lock must be released by its acquiring thread")
        state = self._state
        self._depth -= 1
        state.depth -= 1
        try:
            if state.depth == 0:
                handle, state.handle = state.handle, None
                try:
                    _os_unlock(handle)
                finally:
                    handle.close()
        finally:
            state.gate.release()
            if not self._depth:
                self._owner = None
                self._state = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


@contextmanager
def file_lock(path, *, timeout: float = 10.0) -> Iterator[None]:
    """Serialize a complete operation on ``path`` across threads/processes."""
    with FileLock(path, timeout=timeout):
        yield
