#!/usr/bin/env python3
"""Subprocess execution and worker process management for the studio backend.

Everything runs through subprocess.Popen with an argv list (never a shell). The
child starts a new session, so cancelling it signals only that child's process
group — there is no global pkill and no other process is ever touched.

Output goes to unlinked temporary files, so a chatty child can never deadlock
the parent on a full pipe and no output file is ever visible in the data root.
The captured text is bounded: reading stops after `max_output` bytes.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, IO

import storage

DEFAULT_MAX_OUTPUT = 4 * 1024 * 1024
DEFAULT_POLL_S = 0.25
TERMINATE_GRACE_S = 5.0
# Interval between liveness probes of an owned process group while terminating.
GROUP_POLL_S = 0.05

# Lines carrying these markers are dropped from diagnostics: an auth failure
# from the model CLI must never be copied into a studio record or the log.
_SECRET_MARKERS = (
    "authorization",
    "bearer",
    "api_key",
    "apikey",
    "api-key",
    "access_token",
    "refresh_token",
    "secret",
    "password",
    "credential",
    "cookie",
)


class Cancelled(Exception):
    """The requested work was cancelled by the user."""


class RunFailed(RuntimeError):
    """A subprocess did not succeed.

    `stdout` holds the bounded raw stdout (the data channel callers parse);
    `stderr` holds the bounded, secret-scrubbed stderr (safe to log).
    """

    def __init__(self, message: str, *, returncode: int | None = None, stdout: str = "", stderr: str = "") -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _sanitize(text: str, limit: int = 600) -> str:
    lines = []
    for line in str(text).splitlines():
        if any(marker in line.lower() for marker in _SECRET_MARKERS):
            continue
        line = line.strip()
        if line:
            lines.append(line)
    return " ".join(lines)[:limit]


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


def _read_bounded(handle: IO[bytes], limit: int) -> str:
    try:
        handle.seek(0)
        return _decode(handle.read(max(0, int(limit))))
    except OSError:
        return ""


def run(
    argv: list[str],
    timeout: float,
    cancel: Callable[[], bool] | None,
    cwd: Path | None = None,
    env: dict | None = None,
    *,
    max_output: int = DEFAULT_MAX_OUTPUT,
    poll: float = DEFAULT_POLL_S,
) -> subprocess.CompletedProcess[str]:
    """Run argv to completion, honouring `cancel` and `timeout`.

    `env` is merged over the inherited environment. Cancellation is checked
    before the child is created and again after it finished, so a request that
    arrives while a fast child runs can never be mistaken for success. Raises
    Cancelled when the cancel callable turns true, RunFailed for a timeout, a
    start failure or a nonzero exit, otherwise returns CompletedProcess with
    decoded output. Every exit path reclaims this child's own process group.
    """
    if not argv:
        raise ValueError("argv must not be empty")
    argv = [str(argument) for argument in argv]
    name = Path(argv[0]).name
    if cancel is not None and cancel():
        raise Cancelled(f"{name} cancelled before start")
    child_env = None
    if env:
        child_env = dict(os.environ)
        child_env.update({str(key): str(value) for key, value in env.items()})

    deadline = time.monotonic() + float(timeout)
    with tempfile.TemporaryFile() as out_file, tempfile.TemporaryFile() as err_file:
        try:
            child = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=out_file,
                stderr=err_file,
                cwd=None if cwd is None else str(cwd),
                env=child_env,
                shell=False,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise RunFailed(f"cannot start {name}: {exc}") from exc

        try:
            while True:
                if cancel is not None and cancel():
                    raise Cancelled(f"{name} cancelled")
                if child.poll() is not None:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RunFailed(
                        f"{name} timed out after {float(timeout):.0f}s",
                        stdout=_read_bounded(out_file, max_output),
                        stderr=_sanitize(_read_bounded(err_file, max_output)),
                    )
                try:
                    child.wait(timeout=min(poll, remaining))
                except subprocess.TimeoutExpired:
                    continue
        finally:
            # Reclaim the owned group on every exit path: a child that exits
            # leaving members behind must not leak them past its own run.
            _terminate(child)

        if cancel is not None and cancel():
            raise Cancelled(f"{name} cancelled after completion")

        stdout = _read_bounded(out_file, max_output)
        stderr = _sanitize(_read_bounded(err_file, max_output))

    code = child.returncode
    if code != 0:
        detail = stderr or _sanitize(stdout) or "no diagnostic output"
        raise RunFailed(
            f"{Path(argv[0]).name} exited with {code}: {detail}",
            returncode=code,
            stdout=stdout,
            stderr=stderr,
        )
    return subprocess.CompletedProcess(argv, 0, stdout, stderr)


def _terminate(child: subprocess.Popen, grace: float = TERMINATE_GRACE_S) -> None:
    """SIGTERM the child's own process group, then SIGKILL; wait bounded.

    The group is signalled even when the leader already exited: members that
    inherited the session must not survive their cancellation. Only the group
    of the process this module started is ever signalled — never a global
    pattern and never another process's group.
    """
    _signal_group(child, signal.SIGTERM)
    if _group_gone(child, grace):
        return
    _signal_group(child, signal.SIGKILL)
    _group_gone(child, grace)


def _group_gone(child: subprocess.Popen, grace: float) -> bool:
    """True once the child is reaped and no member of its group is left."""
    deadline = time.monotonic() + max(0.0, grace)
    while True:
        if child.poll() is not None and not _group_alive(child.pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(GROUP_POLL_S)


def _group_alive(pgid: int) -> bool:
    """Whether the process group `pgid` still has members.

    A group id stays reserved while the group exists, so probing the recorded
    id of a reaped leader cannot hit an unrelated recycled process group.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _signal_group(child: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(child.pid, sig)
    except ProcessLookupError:
        return
    except OSError:
        with contextlib.suppress(OSError):
            child.send_signal(sig)


# ----------------------------------------------------------------- worker lease


def acquire_worker_lock(data: Path | None = None) -> IO[bytes] | None:
    """Non-blocking exclusive worker lease; None when another worker owns it."""
    path = storage.worker_lock_path(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    except OSError:
        # Not contention (e.g. a filesystem without flock): surface it instead of
        # leaving every queued job silently stalled behind a phantom worker.
        handle.close()
        raise
    return handle


def release_worker_lock(handle: IO[bytes] | None) -> None:
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            handle.close()


def spawn_worker(argv: list[str], data: Path | None = None) -> int:
    """Start the detached worker: new session, empty stdin, output to a private log."""
    log_path = storage.worker_log_path(data)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "ab", buffering=0) as log:
        child = subprocess.Popen(
            [str(argument) for argument in argv],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            shell=False,
            close_fds=True,
            start_new_session=True,
        )
    return child.pid


def worker_argv() -> list[str]:
    """argv that re-enters this backend's worker command with the current interpreter."""
    return [sys.executable, str(Path(__file__).resolve().parent / "studio.py"), "worker"]
