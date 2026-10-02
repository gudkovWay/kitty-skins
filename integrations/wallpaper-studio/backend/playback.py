#!/usr/bin/env python3
"""Sole owner of linux-wallpaperengine playback for Wallpaper Studio.

One durable record per output lives in ``playback.json`` under the Noctalia
state home.  The module builds the exact argv the legacy W-Engine plugin built,
launches the renderer detached in its own session, and never trusts a stored pid
on its own: every signal, relaunch or adopted process is re-verified against
``/proc`` (start time, output and played background) before it is touched.  It
has its own file lock and never takes the studio state lock.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable

# Flat modules: studio.py puts backend/ on sys.path.
import process  # type: ignore
import storage  # type: ignore


SCHEMA_VERSION = 1
STATE_FILE = "playback.json"
LOCK_FILE = "playback.lock"
LOGS_DIR = "logs"
LOG_FILE = "playback.log"
FRAMES_DIR = "frames"
IMPORTS_DIR = "imports"

RENDERER_NAME = "linux-wallpaperengine"
RENDERER_FALLBACK = Path("/opt/linux-wallpaperengine/linux-wallpaperengine")
RENDERER_LIB_DIR = "/opt/linux-wallpaperengine/lib"

CONNECTOR_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

PROBE_TIMEOUT_S = 60.0
POSTER_TIMEOUT_S = 120.0
PALETTE_TIMEOUT_S = 30.0
WALLPAPER_SET_TIMEOUT_S = 5.0
LAUNCH_WAIT_S = 2.5
LAUNCH_POLL_S = 0.1
TERMINATE_GRACE_S = 3.0
KILL_GRACE_S = 2.0
HASH_CHUNK = 1 << 20
MAX_ERROR_CHARS = 400
MAX_TITLE_CHARS = 200

STATUS_VALUES = ("playing", "stopped", "error")

MEDIA_PROJECT_TYPES = {"scene": "scene", "web": "web", "video": "video", "image": "image"}

# Mirrors the installed W-Engine start.luau so a relaunch with unchanged
# settings produces the same command.
ENGINE_FLAGS = (
    ("silent", "--silent"),
    ("noautomute", "--noautomute"),
    ("no_audio_processing", "--no-audio-processing"),
    ("disable_particles", "--disable-particles"),
    ("disable_mouse", "--disable-mouse"),
    ("disable_parallax", "--disable-parallax"),
    ("no_fullscreen_pause", "--no-fullscreen-pause"),
    ("fullscreen_pause_only_active", "--fullscreen-pause-only-active"),
)
ENGINE_SCREEN_VALUES = (("scaling", "--scaling"), ("clamp", "--clamp"))
ENGINE_GLOBAL_VALUES = (("layer", "--layer"), ("fps", "--fps"), ("volume", "--volume"))


class PlaybackError(RuntimeError):
    """User-visible playback failure; the message is safe to show."""


# --------------------------------------------------------------------------- paths


def _state_root() -> Path:
    """Noctalia state home: $NOCTALIA_STATE_HOME wins, then the XDG state home."""
    raw = os.environ.get("NOCTALIA_STATE_HOME", "").strip()
    if raw and os.path.isabs(raw):
        return Path(raw)
    base = os.environ.get("XDG_STATE_HOME", "").strip()
    root = Path(base) if base and os.path.isabs(base) else Path.home() / ".local" / "state"
    return root / "noctalia"


def plugin_dir() -> Path:
    """This plugin's Noctalia data directory (state file, logs and frames)."""
    return _state_root() / "plugins" / "data" / "q" / "wallpaper-studio"


def state_path() -> Path:
    return plugin_dir() / STATE_FILE


def lock_path() -> Path:
    return plugin_dir() / LOCK_FILE


def log_path() -> Path:
    return plugin_dir() / LOGS_DIR / LOG_FILE


def frames_dir() -> Path:
    return plugin_dir() / FRAMES_DIR


def imports_dir() -> Path:
    """Managed import root, shared with the studio state module."""
    helper = getattr(storage, "imports_dir", None)
    if callable(helper):
        return Path(helper())
    return storage.data_root() / IMPORTS_DIR


def _legacy_dir() -> Path:
    return _state_root() / "plugins" / "data" / "tadomika_ari" / "w-engine"


# --------------------------------------------------------------------------- lock


_HELD: dict[str, list] = {}


class _Lock:
    """Exclusive flock on the playback lock file, reentrant within this process."""

    def __enter__(self) -> "_Lock":
        path = str(lock_path())
        held = _HELD.get(path)
        if held is not None:
            held[1] += 1
            return self
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+b")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _HELD[path] = [handle, 1]
        return self

    def __exit__(self, *_exc) -> bool:
        path = str(lock_path())
        held = _HELD.get(path)
        if held is not None:
            held[1] -= 1
            if held[1] <= 0:
                try:
                    fcntl.flock(held[0].fileno(), fcntl.LOCK_UN)
                finally:
                    held[0].close()
                    _HELD.pop(path, None)
        return False


def playback_lock() -> _Lock:
    return _Lock()


# -------------------------------------------------------------------------- state


def default_state() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "enabled": False,
        "current": {},
        "defaults": {"engine": {}},
        "options": {},
        "library_roots": [],
        "migration": {},
    }


def _clean_engine(value) -> dict:
    if not isinstance(value, dict):
        return {}
    out: dict = {}
    for key, item in value.items():
        if isinstance(key, str) and key and isinstance(item, (bool, int, float, str)):
            out[key] = item
    return out


def _clean_entry(value) -> dict:
    if not isinstance(value, dict):
        return {"engine": {}, "properties": {}}
    return {"engine": _clean_engine(value.get("engine")), "properties": _clean_engine(value.get("properties"))}


def _clean_record(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    path = value.get("path")
    if not isinstance(path, str) or not path:
        return None
    pid = value.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        pid = None
    start_time = value.get("start_time")
    status = value.get("status")
    source_id = value.get("source_id")
    preview = value.get("preview_path")
    pgid = value.get("pgid")
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 0:
        pgid = None
    bg_id = value.get("bg_id")
    error = value.get("error")
    return {
        "content_id": value.get("content_id") if isinstance(value.get("content_id"), str) else "",
        "source_id": source_id if isinstance(source_id, str) else None,
        "path": path,
        "kind": value.get("kind") if isinstance(value.get("kind"), str) else "unknown",
        "preview_path": preview if isinstance(preview, str) else None,
        "pid": pid,
        "start_time": str(start_time) if start_time is not None else None,
        "pgid": pgid,
        "bg_id": bg_id if isinstance(bg_id, str) and bg_id else None,
        "status": status if status in STATUS_VALUES else "stopped",
        "error": error if isinstance(error, str) and error else None,
        "engine": _clean_engine(value.get("engine")),
        "properties": _clean_engine(value.get("properties")),
    }


def _normalize_state(value) -> dict:
    state = default_state()
    if not isinstance(value, dict):
        return state
    state["enabled"] = value.get("enabled") is True

    defaults = value.get("defaults")
    if isinstance(defaults, dict):
        state["defaults"] = {"engine": _clean_engine(defaults.get("engine"))}

    options = value.get("options")
    if isinstance(options, dict):
        state["options"] = {
            key: _clean_entry(entry)
            for key, entry in options.items()
            if isinstance(key, str) and key
        }

    current = value.get("current")
    if isinstance(current, dict):
        cleaned: dict = {}
        for output, record in current.items():
            if not isinstance(output, str) or not output:
                continue
            item = _clean_record(record)
            if item is not None:
                cleaned[output] = item
        state["current"] = cleaned

    roots = value.get("library_roots")
    if isinstance(roots, list):
        state["library_roots"] = [root for root in roots if isinstance(root, str)]

    migration = value.get("migration")
    if isinstance(migration, dict):
        state["migration"] = migration
    return state


def read_state() -> dict:
    """Read the playback record. Missing or malformed yields canonical defaults.

    Pure and cheap: no lock, no directory creation, no process probe.  The file
    is written atomically, so an unlocked read always sees a complete record.
    """
    return _normalize_state(storage.read_json(state_path()))


def _write_state(state: dict) -> None:
    storage.atomic_json(state_path(), state)


def _log(message: str) -> None:
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{storage.now()} {message}\n")
    except OSError:
        pass


# --------------------------------------------------------------------- options


def _is_under(child: str, parent: str) -> bool:
    try:
        child_abs = os.path.abspath(os.path.expanduser(child))
        parent_abs = os.path.abspath(os.path.expanduser(parent))
    except (OSError, ValueError):
        return False
    return child_abs == parent_abs or child_abs.startswith(parent_abs + os.sep)


def _is_import(item) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("origin") == "import":
        return True
    path = item.get("path")
    if not isinstance(path, str) or not path:
        return False
    return _is_under(path, str(imports_dir()))


def _merge_options(defaults_engine: dict, entry: dict) -> dict:
    engine = dict(defaults_engine)
    engine.update(entry.get("engine", {}))
    return {"engine": engine, "properties": dict(entry.get("properties", {}))}


def effective_options(item: dict, state: dict | None = None) -> dict:
    """Resolved {engine, properties} for one item: defaults, then its overrides.

    Properties are never layered (they are declared per wallpaper).  Managed
    MP4/GIF imports are forced silent at volume 0 regardless of inherited
    defaults, so an import can never play the audio it carries.
    """
    if state is None:
        state = read_state()
    source_id = item.get("source_id") if isinstance(item, dict) else None
    entry = state["options"].get(source_id, {}) if isinstance(source_id, str) else {}
    options = _merge_options(state["defaults"].get("engine", {}), entry)
    if _is_import(item):
        options["engine"]["silent"] = True
        options["engine"]["volume"] = 0
    return options


# ------------------------------------------------------------------ renderer argv


def renderer_executable() -> str:
    found = shutil.which(RENDERER_NAME)
    if found:
        return found
    if RENDERER_FALLBACK.exists():
        return str(RENDERER_FALLBACK)
    raise PlaybackError(f"{RENDERER_NAME} not found on PATH or at {RENDERER_FALLBACK}")


def _validate_output(output) -> str:
    if not isinstance(output, str) or not CONNECTOR_RE.match(output):
        raise PlaybackError(f"invalid monitor output name: {output!r}")
    return output


def _background_value(item) -> str:
    if not isinstance(item, dict):
        raise PlaybackError("wallpaper item must be an object")
    raw = item.get("path")
    if not isinstance(raw, str) or not raw.strip():
        raise PlaybackError("wallpaper item has no path")
    path = os.path.abspath(os.path.expanduser(raw.strip()))
    if not os.path.exists(path):
        raise PlaybackError(f"wallpaper path does not exist: {path}")
    return path


def _property_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def renderer_command(item: dict, output: str, options: dict) -> list[str]:
    """Validated argv for one linux-wallpaperengine launch.

    ``output`` is any well-formed connector name (callers such as the sandboxed
    capture use a nested monitor name), ``options`` is the resolved
    ``{engine, properties}`` from :func:`effective_options`.  The flag order and
    formatting mirror the installed W-Engine plugin.
    """
    output = _validate_output(output)
    background = _background_value(item)
    raw = options if isinstance(options, dict) else {}
    engine = _clean_engine(raw.get("engine"))
    properties = _clean_engine(raw.get("properties"))

    argv = [renderer_executable(), "--screen-root", output, "--bg", background]
    for key, flag in ENGINE_SCREEN_VALUES:
        value = engine.get(key)
        if value is not None and value != "" and value != "default":
            argv.extend([flag, str(value)])
    for key, flag in ENGINE_GLOBAL_VALUES:
        if key == "volume" and engine.get("silent") is True:
            continue
        value = engine.get(key)
        if value is not None and value != "":
            argv.extend([flag, str(value)])
    for key, flag in ENGINE_FLAGS:
        if engine.get(key) is True:
            argv.append(flag)
    for name in sorted(properties):
        argv.extend(["--set-property", f"{name}={_property_value(properties[name])}"])
    return argv


def _canonical_path(value) -> str | None:
    """Resolve a filesystem path to its canonical absolute form, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return os.path.realpath(os.path.abspath(os.path.expanduser(value)))
    except (OSError, ValueError):
        return None


def _same_background(record: dict, background) -> bool:
    """Whether a record already plays exactly ``background`` (canonical or recorded id)."""
    left = _canonical_path(record.get("path"))
    right = _canonical_path(background)
    if left is not None and right is not None and left == right:
        return True
    bg_id = record.get("bg_id")
    return isinstance(bg_id, str) and bool(bg_id) and isinstance(background, str) and bg_id == background


def _expected_argv(record: dict, output: str) -> list[str]:
    item = {"path": record.get("path"), "kind": record.get("kind", "unknown")}
    options = {"engine": record.get("engine", {}), "properties": record.get("properties", {})}
    return renderer_command(item, output, options)


# --------------------------------------------------------------- /proc inspection


def _proc_start_time(pid: int) -> str | None:
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_bytes()
    except OSError:
        return None
    text = raw.decode("utf-8", "surrogateescape")
    close = text.rfind(")")
    if close < 0:
        return None
    fields = text[close + 2:].split()
    # Field 3 (state) is fields[0]; starttime is field 22, i.e. fields[19].
    if len(fields) < 20:
        return None
    return fields[19]


def _proc_pgrp(pid: int) -> int | None:
    """Process group id of ``pid`` from /proc, or None when it cannot be read."""
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_bytes()
    except OSError:
        return None
    text = raw.decode("utf-8", "surrogateescape")
    close = text.rfind(")")
    if close < 0:
        return None
    fields = text[close + 2:].split()
    # Field 3 (state) is fields[0]; pgrp is field 5, i.e. fields[2].
    if len(fields) < 3 or not fields[2].isdigit():
        return None
    return int(fields[2])


def _proc_cmdline(pid: int) -> list[str] | None:
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return None
    argv = [part.decode("utf-8", "surrogateescape") for part in raw.split(b"\0") if part]
    return argv or None


def _parse_renderer_argv(argv: list[str]) -> dict:
    output = None
    background = None
    for index, arg in enumerate(argv):
        if arg == "--screen-root" and index + 1 < len(argv):
            output = argv[index + 1]
        elif arg == "--bg" and index + 1 < len(argv):
            background = argv[index + 1]
        elif arg.startswith("--bg="):
            background = arg[len("--bg="):]
    return {"output": output, "bg": background}


def _renderers() -> list[dict]:
    """Every running linux-wallpaperengine process with its output and --bg."""
    found: list[dict] = []
    proc_root = Path("/proc")
    try:
        entries = [entry for entry in proc_root.iterdir() if entry.name.isdigit()]
    except OSError:
        return found
    for entry in entries:
        argv = _proc_cmdline(int(entry.name))
        if not argv or RENDERER_NAME not in os.path.basename(argv[0]):
            continue
        parsed = _parse_renderer_argv(argv)
        if parsed["output"] is None:
            continue
        found.append({"pid": int(entry.name), "output": parsed["output"], "bg": parsed["bg"]})
    return found


def _proc_environ(pid: int) -> dict | None:
    try:
        raw = (Path("/proc") / str(pid) / "environ").read_bytes()
    except OSError:
        return None
    env: dict = {}
    for part in raw.split(b"\0"):
        if not part:
            continue
        key, sep, value = part.decode("utf-8", "surrogateescape").partition("=")
        if sep:
            env[key] = value
    return env


def _same_session(pid: int) -> bool:
    """Whether a candidate renderer lives in this compositor session.

    Guards adoption against picking up a sandboxed capture renderer (nested
    Hyprland uses a different ``XDG_RUNTIME_DIR``/``WAYLAND_DISPLAY``).  Returns
    False when the current session cannot be established from our own env.
    """
    checks = 0
    our_runtime = os.environ.get("XDG_RUNTIME_DIR")
    our_display = os.environ.get("WAYLAND_DISPLAY")
    our_signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    env = _proc_environ(pid)
    if env is None:
        return False
    if our_runtime:
        checks += 1
        if env.get("XDG_RUNTIME_DIR") != our_runtime:
            return False
    if our_display:
        checks += 1
        if env.get("WAYLAND_DISPLAY") != our_display:
            return False
    if our_signature:
        checks += 1
        if env.get("HYPRLAND_INSTANCE_SIGNATURE") != our_signature:
            return False
    return checks > 0


def _renderers_for(output: str) -> list[dict]:
    return [row for row in _renderers() if row["output"] == output]


def _live_rows(output: str) -> list[dict]:
    """Same-session renderer rows for one output (a sandbox capture never counts)."""
    return [row for row in _renderers_for(output) if _same_session(row["pid"])]


def _verify(record: dict, output: str) -> bool:
    """Prove the recorded pid still is our renderer for this output and background.

    A record is trusted only with a positive pid, a nonempty exact kernel start
    time, the same compositor session, the same output and an exactly matching
    canonical background path (or the explicitly recorded legacy background id
    from an owned adoption).  Anything less is not ours and is never signalled.
    """
    if not isinstance(record, dict):
        return False
    pid = record.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    recorded = record.get("start_time")
    if not isinstance(recorded, str) or not recorded.strip():
        return False  # a record without a kernel start time is unverifiable
    start = _proc_start_time(pid)
    if start is None or recorded.strip() != start:
        return False  # process gone, or pid reused by an unrelated process
    if not _same_session(pid):
        return False
    argv = _proc_cmdline(pid)
    if not argv or RENDERER_NAME not in os.path.basename(argv[0]):
        return False
    parsed = _parse_renderer_argv(argv)
    if parsed["output"] != output:
        return False
    bg = parsed["bg"]
    if not isinstance(bg, str) or not bg:
        return False
    return _same_background(record, bg)


# ------------------------------------------------------------------- signalling


def _signal_pid(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except OSError:
        pass


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except OSError:
        pass


def _group_alive(pgid: int) -> bool:
    """Whether a process group still has members (mirrors process.py semantics)."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _group_in_session(pgid: int) -> bool:
    """Whether a live group has a member inside this compositor session.

    Proves ownership of a group whose leader has already exited: only a group
    carrying our session environment (or the same sandbox session) is ever
    signalled, so copied state in another session cannot touch live processes.
    """
    proc_root = Path("/proc")
    try:
        entries = [entry for entry in proc_root.iterdir() if entry.name.isdigit()]
    except OSError:
        return False
    for entry in entries:
        member = int(entry.name)
        if _proc_pgrp(member) != pgid:
            continue
        if _same_session(member):
            return True
    return False


def _leader_gone(pid: int, start: str | None) -> bool:
    current = _proc_start_time(pid)
    return current is None or (start is not None and current != start)


def _wait_gone(pid: int, start: str | None, pgid: int | None, grace: float) -> bool:
    deadline = time.monotonic() + max(0.0, grace)
    while True:
        if _leader_gone(pid, start) and (pgid is None or not _group_alive(pgid)):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(LAUNCH_POLL_S)


def _terminate(record: dict, output: str) -> bool:
    """Stop a renderer we own, cleaning its whole process group.

    The leader is signalled only after its identity is re-proven.  When it has
    already exited, the recorded group is cleaned only if it is still provably
    ours (a member lives in this compositor session); an unproven group is never
    signalled.  Returns True when nothing of ours is left alive, False when a
    process we own could not be stopped.
    """
    pid = record.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    start = _proc_start_time(pid)
    leader_ok = start is not None and _verify(record, output)
    pgid = record.get("pgid")
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 0:
        pgid = pid
    grouped = pgid == pid
    if not leader_ok:
        if start is not None:
            return False  # the pid is alive but not provably our renderer
        if not grouped:
            return True  # leader gone and we own no group to clean
        if not _group_alive(pgid):
            return True  # the group is already gone
        if not _group_in_session(pgid):
            return True  # not provably ours: never signal a stranger

    def signal_owned(sig: int) -> None:
        if grouped:
            _signal_group(pgid, sig)
        _signal_pid(pid, sig)

    signal_owned(signal.SIGTERM)
    if _wait_gone(pid, start, pgid if grouped else None, TERMINATE_GRACE_S):
        return True
    signal_owned(signal.SIGKILL)
    return _wait_gone(pid, start, pgid if grouped else None, KILL_GRACE_S)


# ------------------------------------------------------------------------ launch


def _renderer_context(executable: str) -> tuple[dict | None, str | None]:
    try:
        bare = Path(executable).resolve() == RENDERER_FALLBACK.resolve()
    except OSError:
        bare = False
    if not bare:
        return None, None
    base = os.environ.get("LD_LIBRARY_PATH", "")
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = RENDERER_LIB_DIR + (":" + base if base else "")
    return env, str(RENDERER_FALLBACK.parent)


def _renderer_log_path(output: str) -> Path:
    return plugin_dir() / LOGS_DIR / f"{output}.log"


def _read_log_tail(path: Path, limit: int = MAX_ERROR_CHARS) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    text = data[-4096:].decode("utf-8", "replace")
    joined = " ".join(line.strip() for line in text.splitlines() if line.strip())
    return joined[-limit:]


def _reap_child(child: subprocess.Popen) -> None:
    """Reclaim a just-spawned child's own process group (never a stranger's).

    The child was started with a new session, so its group id is its pid; both
    the leader and any members that outlived it are signalled, escalating like
    the legacy plugin.
    """
    pgid = child.pid

    def signal_owned(sig: int) -> None:
        _signal_group(pgid, sig)
        _signal_pid(child.pid, sig)

    for grace, sig in ((TERMINATE_GRACE_S, signal.SIGTERM), (KILL_GRACE_S, signal.SIGKILL)):
        signal_owned(sig)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if child.poll() is not None and not _group_alive(pgid):
                return
            time.sleep(LAUNCH_POLL_S)


def _launch(record: dict, output: str) -> dict:
    """Spawn the renderer detached, then confirm it survived startup."""
    argv = _expected_argv(record, output)
    env, cwd = _renderer_context(argv[0])
    log = _renderer_log_path(output)
    log.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log, "wb")
    try:
        child = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            env=env,
            cwd=cwd,
        )
    except OSError as exc:
        handle.close()
        raise PlaybackError(f"cannot start {RENDERER_NAME}: {exc}") from exc
    finally:
        handle.close()

    deadline = time.monotonic() + LAUNCH_WAIT_S
    while time.monotonic() < deadline:
        if child.poll() is not None:
            _reap_child(child)
            tail = _read_log_tail(log)
            detail = f": {tail}" if tail else ""
            raise PlaybackError(
                f"{RENDERER_NAME} exited immediately (code {child.returncode}){detail}"
            )
        time.sleep(LAUNCH_POLL_S)

    start = None
    for _ in range(10):
        if child.poll() is not None:
            break
        start = _proc_start_time(child.pid)
        if start:
            break
        time.sleep(LAUNCH_POLL_S)

    if not start:
        # No provable process identity: never record a playing renderer, and
        # reap the half-started group so it cannot leak.
        _reap_child(child)
        tail = _read_log_tail(log)
        detail = f": {tail}" if tail else ""
        raise PlaybackError(f"{RENDERER_NAME} did not report a process identity{detail}")

    launched = dict(record)
    launched["pid"] = child.pid
    launched["start_time"] = start
    launched["pgid"] = child.pid  # start_new_session: the child leads its own group
    launched["bg_id"] = None
    launched["status"] = "playing"
    launched["error"] = None
    return launched


def _record_for(item: dict, options: dict, background: str) -> dict:
    return {
        "content_id": item.get("content_id") or item.get("id") or "",
        "source_id": item.get("source_id") if isinstance(item.get("source_id"), str) else None,
        "path": background,
        "kind": item.get("kind") if isinstance(item.get("kind"), str) else "unknown",
        "preview_path": item.get("preview_path") if isinstance(item.get("preview_path"), str) else None,
        "pid": None,
        "start_time": None,
        "pgid": None,
        "bg_id": None,
        "status": "stopped",
        "error": None,
        "engine": options["engine"],
        "properties": options["properties"],
    }


# ------------------------------------------------------------------------ public


def _foreign_rows(output: str, owned: dict | None) -> list[dict]:
    """Same-session renderers on ``output`` that are not part of our owned group."""
    owned_pid = owned.get("pid") if isinstance(owned, dict) else None
    owned_group = None
    if isinstance(owned, dict):
        group = owned.get("pgid")
        if isinstance(group, int) and not isinstance(group, bool) and group > 0:
            owned_group = group
        elif isinstance(owned_pid, int) and not isinstance(owned_pid, bool) and owned_pid > 0:
            owned_group = owned_pid
    foreign: list[dict] = []
    for row in _live_rows(output):
        if owned_pid is not None and row["pid"] == owned_pid:
            continue
        if owned_group is not None and _proc_pgrp(row["pid"]) == owned_group:
            continue  # a helper process of our own renderer group
        foreign.append(row)
    return foreign


def apply(item: dict, output: str) -> dict:
    """Play one wallpaper on one output, reusing only our own verified renderer.

    A regular apply never adopts or kills a process it cannot prove belongs to
    its own verified record, and never runs a generic duplicate sweep.  A legacy
    renderer identified by a bare/numeric ``--bg`` is adopted only through the
    explicit :func:`migrate_legacy` path.
    """
    output = _validate_output(output)
    background = _background_value(item)

    with playback_lock():
        state = read_state()
        options = effective_options(item, state)
        record = _record_for(item, options, background)
        current = state["current"].get(output)
        owned = current if (isinstance(current, dict) and _verify(current, output)) else None
        same_content = (
            owned is not None
            and (
                not owned.get("content_id")
                or not record.get("content_id")
                or owned.get("content_id") == record.get("content_id")
            )
        )

        if owned is not None and same_content and _same_background(owned, background):
            # Our own renderer already plays exactly this wallpaper: reuse it.
            record["pid"] = owned["pid"]
            record["start_time"] = owned["start_time"]
            record["pgid"] = owned.get("pgid")
            record["bg_id"] = owned.get("bg_id")
            record["status"] = "playing"
            record["error"] = None
            state["current"][output] = record
            state["enabled"] = True
            _write_state(state)
            adopted = True
        else:
            # A dead owned record may still leave members of our own group alive;
            # reap only a group we can prove is ours before touching the output.
            if owned is None and isinstance(current, dict):
                _terminate(current, output)
            if _foreign_rows(output, owned):
                raise PlaybackError(
                    f"another renderer already owns {output}; not replacing or killing "
                    "an unverified process"
                )

            previous = owned
            if previous is not None and not _terminate(previous, output):
                message = f"could not stop the previous renderer on {output}"
                previous["status"] = "error"
                previous["error"] = message
                state["current"][output] = previous
                state["enabled"] = True
                _write_state(state)
                _log(f"apply {output}: {message}")
                raise PlaybackError(message)

            try:
                record = _launch(record, output)
            except PlaybackError as exc:
                if previous is not None:
                    restored = None
                    try:
                        restored = _launch(dict(previous), output)
                    except PlaybackError as restore_exc:
                        previous["status"] = "error"
                        previous["error"] = (
                            f"{exc}; restoring the previous wallpaper failed: {restore_exc}"
                        )
                        previous["pid"] = None
                        previous["start_time"] = None
                        previous["pgid"] = None
                        state["current"][output] = previous
                        state["enabled"] = True
                        _write_state(state)
                        _log(f"apply {output}: launch failed and the previous wallpaper was lost")
                        raise PlaybackError(previous["error"]) from exc
                    state["current"][output] = restored
                    state["enabled"] = True
                    _write_state(state)
                    _log(f"apply {output}: launch failed, previous wallpaper restored")
                    raise PlaybackError(f"{exc}; the previous wallpaper was restored") from exc
                record["status"] = "error"
                record["error"] = str(exc)
                record["pid"] = None
                record["start_time"] = None
                record["pgid"] = None
                state["current"][output] = record
                state["enabled"] = True
                _write_state(state)
                _log(f"apply {output}: launch failed")
                raise

            state["current"][output] = record
            state["enabled"] = True
            _write_state(state)
            adopted = False

        result = {
            "output": output,
            "content_id": record["content_id"],
            "status": record["status"],
            "pid": record["pid"],
            "adopted": adopted,
        }

    # Palette sync is an explicit apply/migrate action, never part of capture.
    warning = _sync_palette(item, output)
    if warning:
        result["warning"] = warning
    return result


def stop(output: str) -> dict:
    """Stop the owned renderer for one output and drop its record.

    Never drops or claims to have stopped a process whose identity cannot be
    proven ours; a still-live renderer we own but cannot terminate keeps its
    record and surfaces an actionable error.
    """
    output = _validate_output(output)
    with playback_lock():
        state = read_state()
        record = state["current"].get(output)
        if record is None:
            return {"output": output, "status": "stopped", "stopped": False, "terminated": False}

        if not _verify(record, output):
            pid = record.get("pid")
            alive = (
                isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
                and _proc_start_time(pid) is not None
            )
            if alive:
                message = f"refusing to stop {output}: the recorded process is not our renderer"
                record["status"] = "error"
                record["error"] = message
                state["current"][output] = record
                _write_state(state)
                _log(f"stop {output}: {message}")
                raise PlaybackError(message)
            # The recorded process is genuinely gone: drop the stale record.
            state["current"].pop(output, None)
            state["enabled"] = bool(state["current"])
            _write_state(state)
            return {"output": output, "status": "stopped", "stopped": True, "terminated": False}

        if not _terminate(record, output):
            message = f"the renderer for {output} did not terminate"
            record["status"] = "error"
            record["error"] = message
            state["current"][output] = record
            _write_state(state)
            _log(f"stop {output}: {message}")
            raise PlaybackError(message)

        state["current"].pop(output, None)
        state["enabled"] = bool(state["current"])
        _write_state(state)
    return {"output": output, "status": "stopped", "stopped": True, "terminated": True}


def reconcile() -> dict:
    """Refresh owned active selections when playback is enabled; never sweep broadly.

    For every output the state claims is playing, the recorded process is
    re-verified against /proc.  Only a genuinely gone previous playing record is
    relaunched, and only when no same-session renderer already occupies the
    output.  A record whose identity was reused or does not match becomes a
    durable error instead of a retry loop; no generic process sweep runs, so
    captures in other sessions are never touched.
    """
    with playback_lock():
        state = read_state()
        summary = {
            "enabled": bool(state["enabled"]),
            "checked": 0,
            "playing": 0,
            "restarted": 0,
            "errors": [],
        }
        if not state["enabled"]:
            return summary
        changed = False
        for output, record in list(state["current"].items()):
            summary["checked"] += 1
            if record.get("status") == "error":
                # An explicit apply must clear this; observation never retries it.
                summary["errors"].append(
                    f"{output}: {record.get('error') or 'playback error; apply the wallpaper again'}"
                )
                continue
            if _verify(record, output):
                if record.get("status") != "playing" or record.get("error"):
                    record["status"] = "playing"
                    record["error"] = None
                    changed = True
                summary["playing"] += 1
                continue

            pid = record.get("pid")
            alive = (
                isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
                and _proc_start_time(pid) is not None
            )
            if alive:
                # Reused pid / foreign identity: never relaunch, never signal.
                record["status"] = "error"
                record["error"] = f"pid {pid} no longer matches our renderer; not relaunching"
                changed = True
                summary["errors"].append(f"{output}: {record['error']}")
                continue

            if record.get("status") != "playing":
                # Nothing was playing here before: do not start a new renderer.
                continue

            # The previous playing renderer is genuinely gone; reap any leftover
            # members of our own group before deciding whether to relaunch.
            _terminate(record, output)
            if _live_rows(output):
                record["status"] = "error"
                record["error"] = "another renderer already occupies this output; not launching a duplicate"
                changed = True
                summary["errors"].append(f"{output}: {record['error']}")
                continue

            target = dict(record)
            try:
                target = _launch(target, output)
            except PlaybackError as exc:
                target["pid"] = None
                target["start_time"] = None
                target["pgid"] = None
                target["status"] = "error"
                target["error"] = str(exc)
                state["current"][output] = target
                changed = True
                summary["errors"].append(f"{output}: {exc}")
                _log(f"reconcile {output}: {exc}")
                continue
            state["current"][output] = target
            changed = True
            summary["restarted"] += 1
            summary["playing"] += 1
        if changed:
            _write_state(state)
        return summary


# ------------------------------------------------------------------ palette sync


def _path_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def _color_source(item: dict) -> str | None:
    """An image representing the live wallpaper, mirroring the legacy provider."""
    preview = item.get("preview_path")
    base = preview if isinstance(preview, str) and Path(preview).is_file() else None
    if item.get("kind") != "video":
        return base
    media = item.get("media_path") or item.get("path")
    if not isinstance(media, str) or not Path(media).is_file():
        return base
    identity = item.get("content_id") or item.get("id") or _path_digest(media)
    frame = frames_dir() / f"{identity}.jpg"
    if frame.is_file():
        return str(frame)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return base
    frame.parent.mkdir(parents=True, exist_ok=True)
    tmp = frame.with_name(frame.name + ".tmp.jpg")
    argv = [
        ffmpeg, "-y", "-loglevel", "error", "-ss", "1",
        "-i", media, "-frames:v", "1", "-threads", "2", "-filter_threads", "1",
        "-vf", "scale=960:-2", str(tmp),
    ]
    try:
        process.run(argv, PALETTE_TIMEOUT_S, None)
    except Exception:  # noqa: BLE001 - a missing frame must not fail the apply
        try:
            tmp.unlink()
        except OSError:
            pass
        return base
    if tmp.is_file():
        os.replace(tmp, frame)
        return str(frame)
    return base


def _sync_palette(item: dict, output: str) -> str | None:
    """Keep Noctalia's palette context by setting the still the legacy provider set."""
    binary = shutil.which("noctalia")
    if not binary:
        return None
    try:
        source = _color_source(item)
    except Exception as exc:  # noqa: BLE001
        return f"palette source failed: {type(exc).__name__}: {exc}"
    if not source:
        return None
    try:
        process.run([binary, "msg", "wallpaper-set", output, source], WALLPAPER_SET_TIMEOUT_S, None)
    except (process.RunFailed, process.Cancelled, OSError) as exc:
        return f"palette sync failed: {exc}"
    return None


# ---------------------------------------------------------------------- imports


def _ffprobe_path() -> str:
    found = shutil.which("ffprobe")
    if not found:
        raise PlaybackError("ffprobe is required to import media")
    return found


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _probe_media(path: Path, cancel: Callable[[], bool] | None) -> dict:
    argv = [
        _ffprobe_path(), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    try:
        completed = process.run(argv, PROBE_TIMEOUT_S, cancel)
    except process.RunFailed as exc:
        raise PlaybackError(f"ffprobe could not read the file: {exc}") from exc
    try:
        data = json.loads(completed.stdout or "{}")
    except ValueError as exc:
        raise PlaybackError("ffprobe returned unreadable metadata") from exc
    if not isinstance(data, dict):
        raise PlaybackError("ffprobe returned unreadable metadata")
    streams = data.get("streams") if isinstance(data.get("streams"), list) else []
    if not any(isinstance(stream, dict) and stream.get("codec_type") == "video" for stream in streams):
        raise PlaybackError("the file has no video stream")

    fmt = data.get("format") if isinstance(data.get("format"), dict) else {}
    names = {part.strip().lower() for part in str(fmt.get("format_name") or "").split(",") if part.strip()}
    if "gif" in names:
        container = "gif"
    elif names & {"mp4", "mov", "m4a", "3gp", "3g2", "mj2"}:
        container = "mp4"
    else:
        raise PlaybackError(
            f"unsupported container {fmt.get('format_name')!r}; only MP4 and GIF are supported"
        )
    duration = _to_float(fmt.get("duration"))
    if duration is None:
        for stream in streams:
            duration = _to_float(stream.get("duration")) if isinstance(stream, dict) else None
            if duration is not None:
                break
    return {"container": container, "duration": duration}


def _check_cancel(cancel: Callable[[], bool] | None) -> None:
    if cancel is not None and cancel():
        raise process.Cancelled("cancelled")


def _sha256_file(path: Path, cancel: Callable[[], bool] | None) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            _check_cancel(cancel)
            chunk = handle.read(HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _copy_file(source: Path, target: Path, cancel: Callable[[], bool] | None) -> None:
    with open(source, "rb") as reader, open(target, "wb") as writer:
        while True:
            _check_cancel(cancel)
            chunk = reader.read(HASH_CHUNK)
            if not chunk:
                break
            writer.write(chunk)
    try:
        shutil.copystat(source, target, follow_symlinks=True)
    except OSError:
        pass


def _verify_png(path: Path) -> None:
    try:
        header = path.read_bytes()[:8]
    except OSError as exc:
        raise PlaybackError(f"preview image is unreadable: {exc}") from exc
    if header != b"\x89PNG\r\n\x1a\n":
        raise PlaybackError("ffmpeg did not produce a real PNG preview")


def _extract_poster(media: Path, target: Path, duration: float | None,
                    cancel: Callable[[], bool] | None) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise PlaybackError("ffmpeg is required to build the preview")
    seeks = ["1", "0"] if duration and duration > 1.5 else ["0"]
    last: str | None = None
    for seek in seeks:
        argv = [
            ffmpeg, "-y", "-loglevel", "error", "-ss", seek,
            "-i", str(media), "-frames:v", "1", "-threads", "2", "-filter_threads", "1",
            "-vf", "scale=960:-2", str(target),
        ]
        try:
            process.run(argv, POSTER_TIMEOUT_S, cancel)
        except process.Cancelled:
            raise
        except (process.RunFailed, OSError) as exc:
            last = str(exc)
            continue
        if target.is_file() and target.stat().st_size > 0:
            return
    raise PlaybackError(f"ffmpeg could not produce a preview frame{': ' + last if last else ''}")


def _existing_import(final: Path) -> dict | None:
    if not final.is_dir():
        return None
    manifest = storage.read_json(final / "project.json")
    if not isinstance(manifest, dict):
        return None
    media_name = manifest.get("file")
    preview_name = manifest.get("preview")
    if not isinstance(media_name, str) or not isinstance(preview_name, str):
        return None
    if not (final / media_name).is_file() or not (final / preview_name).is_file():
        return None
    return {
        "path": str(final),
        "root": str(imports_dir()),
        "title": str(manifest.get("title") or final.name),
    }


def _progress(callback) -> Callable[[str], None]:
    if callback is None:
        return lambda _stage: None

    def emit(stage: str) -> None:
        try:
            callback(str(stage))
        except Exception:  # noqa: BLE001 - progress must never abort the import
            pass

    return emit


def import_media(path: str, *, progress=None, cancel=None) -> dict:
    """Copy one MP4/GIF into the managed import root with a real PNG poster.

    The source file is preserved, the copy is content-addressed by its sha256 so
    a repeated import is idempotent, and the project is published atomically: a
    partial or cancelled import never leaves a directory the catalog can pick up.
    """
    emit = _progress(progress)
    source = Path(os.path.abspath(os.path.expanduser(str(path))))
    if not source.is_file():
        raise PlaybackError(f"source file does not exist: {source}")

    emit("probing")
    _check_cancel(cancel)
    probe = _probe_media(source, cancel)

    emit("hashing")
    digest = _sha256_file(source, cancel)

    root = imports_dir()
    final = root / digest
    existing = _existing_import(final)
    if existing is not None:
        emit("ready")
        return existing

    title = storage.clean_text(source.stem, MAX_TITLE_CHARS) or source.name
    work = root / f".tmp-{digest}-{os.getpid()}"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    try:
        emit("copying")
        media_name = source.name
        media_target = work / media_name
        _copy_file(source, media_target, cancel)

        # The identity is the hash of the source; the copied bytes must match it
        # exactly, otherwise the source changed while it was being read.
        _check_cancel(cancel)
        copied = _sha256_file(media_target, cancel)
        if copied != digest:
            raise PlaybackError("the source file changed while it was being imported")

        emit("poster")
        _check_cancel(cancel)
        poster = work / "preview.png"
        _extract_poster(media_target, poster, probe.get("duration"), cancel)
        _verify_png(poster)

        manifest = {
            "file": media_name,
            "preview": "preview.png",
            "title": title,
            "type": "video",
            "general": {"properties": {}},
        }
        storage.atomic_json(work / "project.json", manifest)

        emit("publishing")
        _check_cancel(cancel)
        if final.exists():
            # A directory under our content hash that is not a readable project
            # is a collision we must not silently destroy: fail explicitly.
            raise PlaybackError(
                f"a managed import already exists at {final} but is not a valid project; "
                "refusing to overwrite it"
            )
        try:
            os.replace(work, final)
        except OSError as exc:
            raise PlaybackError(f"cannot publish the managed import at {final}: {exc}") from exc
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return {"path": str(final), "root": str(root), "title": title}


# -------------------------------------------------------------------- migration


def _manifest_path() -> Path | None:
    materialized = _state_root() / "plugins" / "materialized"
    candidates = [materialized / "community" / "w-engine" / "plugin.toml"]
    try:
        candidates.extend(sorted(materialized.glob("*/w-engine/plugin.toml")))
    except OSError:
        pass
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _manifest_id(path: Path) -> str | None:
    try:
        text = path.read_text("utf-8", "surrogateescape")
    except OSError:
        return None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "id":
            return value.strip().strip('"').strip("'") or None
    return None


def _legacy_data_path() -> Path | None:
    exact = _legacy_dir() / "data.json"
    if exact.is_file():
        return exact
    root = _state_root() / "plugins" / "data"
    try:
        for candidate in sorted(root.glob("*/w-engine/data.json")):
            if candidate.is_file():
                return candidate
    except OSError:
        pass
    return None


def _legacy_mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _project_item_from_dir(project: Path, source_id: str) -> dict | None:
    """Build a complete catalog-shaped item for a project dir, with real identity.

    The identity is computed through the catalog's own helper on a full item
    (provider/kind/media fields included); when it cannot be resolved the item is
    returned with ``id`` None so the caller can mark the entry unresolved instead
    of presenting a fabricated identity.
    """
    manifest = storage.read_json(project / "project.json")
    if not isinstance(manifest, dict):
        return None
    type_key = manifest.get("type")
    kind = MEDIA_PROJECT_TYPES.get(type_key.strip().lower(), "unknown") if isinstance(type_key, str) else "unknown"
    file_name = manifest.get("file")
    media = project / file_name if isinstance(file_name, str) and file_name else None
    preview = manifest.get("preview")
    preview_path = project / preview if isinstance(preview, str) and preview else None
    item = {
        "id": None,
        "title": str(manifest.get("title") or project.name),
        "kind": kind,
        "path": str(project),
        "media_path": str(media) if media is not None and media.exists() else None,
        "preview_path": str(preview_path) if preview_path is not None and preview_path.exists() else None,
        "provider": "wallpaper-engine",
        "source_id": source_id,
    }
    try:
        import context  # type: ignore

        item["id"] = context.catalog().current_content_id(item)
    except Exception:  # noqa: BLE001 - unresolved identity, reported by the caller
        item["id"] = None
    return item


def _resolve_source(source_id: str, inventory_items, roots) -> dict | None:
    matches = [
        item for item in inventory_items
        if isinstance(item, dict)
        and item.get("provider") == "wallpaper-engine"
        and item.get("source_id") == source_id
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return None  # ambiguous identity; never guess
    for root in roots:
        project = Path(root) / source_id
        if (project / "project.json").is_file():
            return _project_item_from_dir(project, source_id)
    return None


def _personal_roots(value) -> list[str]:
    """String roots from the legacy ``personnalPath`` value (list or mapping)."""
    roots: list[str] = []
    entries = value.values() if isinstance(value, dict) else value if isinstance(value, list) else []
    for entry in entries:
        if isinstance(entry, str):
            roots.append(entry)
        elif isinstance(entry, dict):
            for key in ("path", "dir"):
                candidate = entry.get(key)
                if isinstance(candidate, str) and candidate:
                    roots.append(candidate)
                    break
    return roots


def _library_roots(state: dict, legacy_paths: list[str]) -> list[str]:
    """Merge discovered, previously saved and legacy personal library roots."""
    roots: list[str] = []
    seen: set[str] = set()

    def add(value) -> None:
        if not isinstance(value, str) or not value.strip():
            return
        try:
            resolved = os.path.abspath(os.path.expanduser(value))
        except (OSError, ValueError):
            return
        if resolved not in seen:
            seen.add(resolved)
            roots.append(resolved)

    try:
        import context  # type: ignore

        discovered = context.discover()
        for root in discovered.get("library_roots", []):
            add(root)
    except Exception:  # noqa: BLE001 - fall back to the saved and legacy roots
        pass
    for root in state.get("library_roots", []):
        add(root)
    for root in legacy_paths:
        add(root)
    return roots


def _adopt_legacy(record: dict, output: str, source_id, state: dict) -> bool:
    """Adopt a live renderer left by the old plugin, only when it is provably ours.

    The legacy ``--bg`` may be a bare/numeric provider id rather than a path, so
    adoption is allowed only here, and only when the live renderer in this
    compositor session uniquely matches this resolved source (by canonical path
    or by that exact source id).
    """
    canonical = _canonical_path(record.get("path"))
    matches: list[dict] = []
    for row in _live_rows(output):
        bg = row.get("bg")
        if not isinstance(bg, str) or not bg:
            continue
        if (canonical is not None and _canonical_path(bg) == canonical) or (
            isinstance(source_id, str) and source_id and bg == source_id
        ):
            matches.append(row)
    if len(matches) != 1:
        return False
    row = matches[0]
    start = _proc_start_time(row["pid"])
    if not start:
        return False
    record["pid"] = row["pid"]
    record["start_time"] = start
    record["pgid"] = row["pid"] if _proc_pgrp(row["pid"]) == row["pid"] else None
    record["bg_id"] = row["bg"]
    record["status"] = "playing"
    record["error"] = None
    state["current"][output] = record
    return True


def migrate_legacy(*, activate: bool = False) -> dict:
    """Adopt the legacy W-Engine configuration, reversibly and without killing.

    Reads the old ``data.json``, merges its library roots, defaults, options and
    settings, resolves its ids against the catalog index and project roots, and
    stores an exact snapshot (selections, cycles, saved wallpapers, personal
    paths and favorites) for rollback.  It never disables the old plugin and
    never signals an unowned process; with ``activate=True`` it only adopts or
    launches renderers whose target matches, and reports the real handoff result.
    """
    data_path = _legacy_data_path()
    manifest_path = _manifest_path()
    legacy = storage.read_json(data_path) if data_path is not None else None
    legacy = legacy if isinstance(legacy, dict) else None

    if legacy is None:
        with playback_lock():
            state = read_state()
            snapshot = {
                "schema_version": SCHEMA_VERSION,
                "found": False,
                "migrated_at": storage.now(),
                "data_path": str(data_path) if data_path is not None else None,
                "activated": False,
                "activated_at": None,
                "legacy": None,
                "desired": {},
            }
            state["migration"] = snapshot
            _write_state(state)
        return {
            "found": False,
            "data_path": snapshot["data_path"],
            "desired": {},
            "activated": [],
            "errors": [],
        }

    legacy_defaults = {"engine": _clean_engine(_legacy_mapping(legacy.get("defaults")).get("engine"))}
    legacy_options = {
        key: _clean_entry(value)
        for key, value in _legacy_mapping(legacy.get("options")).items()
        if isinstance(key, str) and key
    }
    personal = _personal_roots(legacy.get("personnalPath"))

    with playback_lock():
        state = read_state()
        state["library_roots"] = _library_roots(state, personal)

        merged_defaults = dict(state["defaults"].get("engine", {}))
        merged_defaults.update(legacy_defaults["engine"])
        state["defaults"] = {"engine": merged_defaults}

        merged_options = dict(state["options"])
        merged_options.update(legacy_options)
        state["options"] = merged_options
        roots = state["library_roots"]

        inventory_items: list = []
        try:
            import context  # type: ignore

            index = context.inventory()
            if isinstance(index, dict) and isinstance(index.get("items"), list):
                inventory_items = index["items"]
        except Exception:  # noqa: BLE001 - fall back to project roots
            inventory_items = []

        desired: dict = {}
        for output, source_id in _legacy_mapping(legacy.get("current")).items():
            if not isinstance(output, str) or not isinstance(source_id, str) or not source_id:
                continue
            item = _resolve_source(source_id, inventory_items, roots)
            entry = _merge_options(legacy_defaults["engine"], legacy_options.get(source_id, {}))
            if item is None or not item.get("id"):
                reason = (
                    "id not found uniquely in the catalog inventory or project roots"
                    if item is None
                    else "the project's content identity could not be resolved"
                )
                desired[output] = {
                    "source_id": source_id,
                    "content_id": None,
                    "path": item.get("path") if item is not None else None,
                    "kind": item.get("kind") if item is not None else None,
                    "media_path": item.get("media_path") if item is not None else None,
                    "preview_path": item.get("preview_path") if item is not None else None,
                    "engine": entry["engine"],
                    "properties": entry["properties"],
                    "resolved": False,
                    "reason": reason,
                }
                continue
            desired[output] = {
                "source_id": source_id,
                "content_id": item.get("id"),
                "path": item.get("path"),
                "kind": item.get("kind"),
                "media_path": item.get("media_path"),
                "preview_path": item.get("preview_path"),
                "engine": entry["engine"],
                "properties": entry["properties"],
                "resolved": True,
                "reason": None,
            }

        snapshot = {
            "schema_version": SCHEMA_VERSION,
            "found": True,
            "migrated_at": storage.now(),
            "data_path": str(data_path),
            "manifest_path": str(manifest_path) if manifest_path is not None else None,
            "plugin": _manifest_id(manifest_path) if manifest_path is not None else None,
            "activated": False,
            "activated_at": None,
            "legacy": legacy,
            "defaults": legacy_defaults,
            "options": legacy_options,
            "selections": _legacy_mapping(legacy.get("selection")),
            "cycles": _legacy_mapping(legacy.get("cycle")),
            "favorites": legacy.get("favorites") if isinstance(legacy.get("favorites"), list) else [],
            "saved_wallpaper": _legacy_mapping(legacy.get("saved_wallpaper")),
            "personnal_path": legacy.get("personnalPath") if isinstance(legacy.get("personnalPath"), (list, dict)) else {},
            "desired": desired,
        }
        if activate:
            state["enabled"] = True
        state["migration"] = snapshot
        _write_state(state)

    activated: list = []
    errors: list = []
    if activate:
        for output, entry in desired.items():
            if not entry.get("resolved"):
                errors.append(f"{output}: {entry.get('reason') or 'unresolved'}")
                continue
            item = {
                "id": entry.get("content_id"),
                "source_id": entry.get("source_id"),
                "kind": entry.get("kind"),
                "path": entry.get("path"),
                "media_path": entry.get("media_path"),
                "preview_path": entry.get("preview_path"),
            }
            record = _record_for(
                item,
                {"engine": entry["engine"], "properties": entry["properties"]},
                entry.get("path"),
            )
            adopted = False
            with playback_lock():
                state = read_state()
                if _adopt_legacy(record, output, entry.get("source_id"), state):
                    state["enabled"] = True
                    _write_state(state)
                    adopted = True
            if adopted:
                activated.append(output)
                continue
            try:
                apply(item, output)
                activated.append(output)
            except (PlaybackError, OSError) as exc:
                errors.append(f"{output}: {exc}")
        _log(f"migrate_legacy activate: activated={activated} errors={errors}")

    handoff = bool(activate) and not errors and all(
        entry.get("resolved") for entry in desired.values()
    )

    with playback_lock():
        state = read_state()
        snapshot["activated"] = handoff
        snapshot["activated_at"] = storage.now() if handoff else None
        state["migration"] = snapshot
        _write_state(state)

    return {
        "found": True,
        "data_path": str(data_path),
        "desired": desired,
        "activated": activated,
        "errors": errors,
    }
