#!/usr/bin/env python3
"""Fullscreen media viewer for Wallpaper Studio: one owned mpv per request.

The backend resolves trusted absolute paths (inventory media, a capture record's
frames or a variant's real frame render) and this module opens exactly those
paths in one detached, fullscreen mpv: the aspect ratio is preserved, audio is
muted, videos and GIFs loop, stills are kept indefinitely and a gallery is
navigated with the arrow keys (Esc closes). `open_media` returns as soon as the
owned viewer is up — never while it stays open — so a panel click never blocks
on a dismissal.

Ownership is a record under `<data>/viewer/` (never wallpaper playback state)
plus the viewer's own IPC socket. Every close first proves the recorded pid is
still the process we started (its `/proc` start time, its executable and our
socket path in its argv) and then contacts that viewer over its own socket;
only its own process group is ever signalled. There is no global pkill and no
shell interpolation anywhere: mpv is always started from an argv list.

Gallery captions stay visible: the playlist written for mpv carries one
`#EXTINF` caption per entry (the per-frame timestamps main supplies), so mpv's
own for-entry title machinery names the current frame, and the overlay script
this module installs repaints that caption on the OSD on every navigation.

Sandbox rule: a real user click opens the window on the output the user is
looking at. Inside the desktop sandbox — proven the same way capture_scene.py
proves it, VD_ROOT with every XDG root redirected under it — the viewer inherits
the nested display and gets the `agent-test` window class, so the compositor
parks it on the hidden test workspace instead of a working desktop.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import storage  # type: ignore

SCHEMA_VERSION = 1
VIEWER_DIR = "viewer"
OWNER_FILE = "owner.json"
LOCK_FILE = "lock"
SOCKET_FILE = "ipc.sock"
LOG_FILE = "viewer.log"
PLAYLIST_FILE = "playlist.m3u"
INPUT_FILE = "input.conf"
SCRIPT_DIR = "script"
SCRIPT_MAIN = "main.lua"

MPV_NAME = "mpv"
KINDS = ("image", "video", "gallery")

#: The inventory's own media vocabulary (consumers/catalog.py IMAGE_EXTENSIONS
#: plus VIDEO_EXTENSIONS), so every path main can resolve from the catalog,
#: from capture frames or from a real frame render is accepted, and nothing
#: else is handed to mpv.
MEDIA_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp", ".tif", ".tiff", ".jxl",
    ".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".wmv", ".flv", ".mpg", ".mpeg",
    ".ogv",
})

MAX_PATHS = 512
MAX_PATH_CHARS = 4096
MAX_TITLE_CHARS = 200
MAX_LABEL_CHARS = 200
MAX_LOG_TAIL = 800

# A unix socket path is capped by a short kernel limit; refuse instead of
# letting mpv fail obscurely on a deep data root.
SOCKET_PATH_LIMIT = 100

POLL_S = 0.05
LOCK_TIMEOUT_S = 5.0
READY_TIMEOUT_S = 8.0
IPC_TIMEOUT_S = 1.0
IPC_QUIT_WAIT_S = 2.0
TERM_GRACE_S = 3.0
KILL_GRACE_S = 2.0

# Nested Hyprland shares XDG_RUNTIME_DIR for Wayland socket negotiation.
SANDBOX_ROOT_ENV = "VD_ROOT"
SANDBOX_XDG_ROOTS = (
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
    "XDG_DATA_HOME",
)
TEST_WINDOW_CLASS = "agent-test"

_CONTENT_ID_RE = re.compile(r"^[0-9a-f]{64}$")

# Arrows move through the gallery instead of seeking: a frame gallery is
# navigated, not scrubbed. mpv's remaining weak defaults (space, volume, ...)
# stay in force because this file only overrides the listed keys.
INPUT_CONF = (
    "# Wallpaper Studio viewer: arrows walk the gallery, Esc closes it.\n"
    "LEFT playlist-prev\n"
    "RIGHT playlist-next\n"
    "UP playlist-prev\n"
    "DOWN playlist-next\n"
    "ENTER playlist-next\n"
    "ESC quit\n"
    "q quit\n"
)

# Loaded with --script (a directory script, so mp.get_script_directory() points
# at the files written next to it). The playlist is the single source of truth
# for the captions: the same #EXTINF lines mpv turns into per-entry titles are
# parsed here, and every navigation repaints the caption on the OSD, so the
# timestamp of the frame the user landed on is always visible.
MAIN_LUA = """-- Wallpaper Studio viewer overlay: show the current entry's caption.
--
-- The backend writes playlist.m3u next to this script, one #EXTINF caption per
-- entry. Navigating a gallery must always name the frame that is now on
-- screen, so every file-loaded event and every playlist move paints that
-- entry's caption.
local directory = mp.get_script_directory()
local captions = {}

if directory then
    local playlist = io.open(directory .. '/playlist.m3u', 'rb')
    if playlist then
        for line in playlist:lines() do
            if line:sub(1, 8) == '#EXTINF:' then
                local comma = line:find(',', 9, true)
                captions[#captions + 1] = comma and line:sub(comma + 1) or ''
            end
        end
        playlist:close()
    end
end

local function show()
    local position = mp.get_property_number('playlist-pos')
    if position == nil or position < 0 then return end
    local caption = captions[position + 1]
    if caption == nil or caption == '' then return end
    mp.osd_message(caption, 3)
end

mp.register_event('file-loaded', show)
mp.observe_property('playlist-pos', 'number', show)
"""


class ViewerError(RuntimeError):
    """User-visible viewer failure; the message is safe to show."""


# --------------------------------------------------------------------- paths


def viewer_dir(data: Path | None = None) -> Path:
    """This module's owned directory: ownership record, socket, log, playlist."""
    return storage.resolve_root(data) / VIEWER_DIR


def owner_path(data: Path | None = None) -> Path:
    return viewer_dir(data) / OWNER_FILE


def lock_path(data: Path | None = None) -> Path:
    return viewer_dir(data) / LOCK_FILE


def socket_path(data: Path | None = None) -> Path:
    return viewer_dir(data) / SOCKET_FILE


def log_path(data: Path | None = None) -> Path:
    return viewer_dir(data) / LOG_FILE


def script_path(data: Path | None = None) -> Path:
    return viewer_dir(data) / SCRIPT_DIR


def playlist_path(data: Path | None = None) -> Path:
    """The m3u handed to mpv, kept beside the overlay script that reads it."""
    return script_path(data) / PLAYLIST_FILE


def input_conf_path(data: Path | None = None) -> Path:
    return viewer_dir(data) / INPUT_FILE


# ------------------------------------------------------------------ helpers


def _one_line(value, limit: int) -> str:
    """Untrusted text -> one bounded line: no control characters, no runs of space."""
    if not isinstance(value, str):
        return ""
    cleaned = "".join(ch if ch >= " " else " " for ch in value)
    return " ".join(cleaned.split())[:limit]


def _expand_escape(value: str) -> str:
    """mpv expands properties in --title; `$$` is its documented literal `$`."""
    return value.replace("$", "$$")


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)


def _ensure_text(path: Path, text: str) -> None:
    """Write `text` atomically unless the file already holds exactly it."""
    try:
        if path.read_text(encoding="utf-8") == text:
            return
    except OSError:
        pass
    storage.atomic_write_bytes(path, text.encode("utf-8"))


def _inside(path: str, root: str) -> bool:
    try:
        child = os.path.realpath(path)
    except OSError:
        return False
    return child == root or child.startswith(root + os.sep)


def _test_window_class() -> str | None:
    """`agent-test` only inside the desktop sandbox, nothing for a real click.

    A real explicit click must leave mpv's own class alone. vd-run redirects the
    four writable XDG roots; nested Hyprland intentionally keeps the shared
    runtime directory for Wayland sockets.
    """
    root = os.environ.get(SANDBOX_ROOT_ENV, "").strip()
    if not root or not os.path.isabs(root):
        return None
    real = os.path.realpath(root)
    for name in SANDBOX_XDG_ROOTS:
        value = os.environ.get(name, "").strip()
        if not value or not _inside(value, real):
            return None
    return TEST_WINDOW_CLASS


def _log_tail(data: Path | None = None, limit: int = MAX_LOG_TAIL) -> str:
    try:
        raw = log_path(data).read_bytes()
    except OSError:
        return "no diagnostic output"
    return _one_line(raw[-limit:].decode("utf-8", "replace"), limit) or "no diagnostic output"


# ------------------------------------------------------------------ /proc


def _proc_fields(pid: int) -> list[str] | None:
    """`/proc/<pid>/stat` fields after the comm field, or None when unreadable."""
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_bytes()
    except OSError:
        return None
    text = raw.decode("utf-8", "surrogateescape")
    close = text.rfind(")")
    if close < 0:
        return None
    fields = text[close + 2:].split()
    return fields or None


def _proc_start_time(pid: int) -> str | None:
    """Start time of `pid`: field 22, i.e. fields[19] after the state field."""
    fields = _proc_fields(pid)
    if fields is None or len(fields) < 20:
        return None
    return fields[19]


def _proc_gone(pid: int, start: str) -> bool:
    """True once the recorded process is gone: reaped, a zombie, or another pid.

    A zombie is already closed — the window is gone and it holds no socket — but
    `/proc` keeps reporting the same start time until its parent reaps it, so a
    shutdown that waited on the start time alone could stall on it.
    """
    fields = _proc_fields(pid)
    if fields is None or len(fields) < 20:
        return True
    if fields[0] == "Z":
        return True
    return fields[19] != start


def _proc_cmdline(pid: int) -> list[str] | None:
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return None
    argv = [part.decode("utf-8", "surrogateescape") for part in raw.split(b"\0") if part]
    return argv or None


def _identify(owner: dict) -> bool:
    """Prove the recorded pid still is the viewer this module started.

    A pid alone can be recycled; the start time pins the same process and the
    argv check pins the same viewer (mpv with our own socket path), so a
    stranger's process is never contacted or signalled.
    """
    pid = owner["pid"]
    current = _proc_start_time(pid)
    if current is None or current != owner["start_time"]:
        return False
    argv = _proc_cmdline(pid)
    if not argv or MPV_NAME not in os.path.basename(argv[0]):
        return False
    return f"--input-ipc-server={owner['socket']}" in argv


# ------------------------------------------------------------- signalling


def _signal_group(pgid: int, sig: int) -> None:
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 0:
        return
    with contextlib.suppress(OSError):
        os.killpg(pgid, sig)


def _signal_pid(pid: int, sig: int) -> None:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return
    with contextlib.suppress(OSError):
        os.kill(pid, sig)


def _group_alive(pgid: int) -> bool:
    """Whether a process group still has members (mirrors process.py semantics)."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _wait_gone(pid: int, start: str, pgid: int | None, grace: float) -> bool:
    """Wait for the pinned leader; never signal a recycled process-group id."""
    deadline = time.monotonic() + max(0.0, grace)
    while True:
        if _proc_gone(pid, start):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_S)


def _ipc_quit(path: str) -> bool:
    """Ask the owned viewer to quit over its own socket; False when it is gone."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(IPC_TIMEOUT_S)
            client.connect(path)
            client.sendall(json.dumps({"command": ["quit"]}).encode("utf-8") + b"\n")
            with contextlib.suppress(OSError):
                client.recv(4096)
        return True
    except OSError:
        return False


def _socket_ready(path: Path) -> bool:
    """Require decoded video/image parameters, not merely an open IPC socket."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(IPC_TIMEOUT_S)
            client.connect(str(path))
            client.sendall(b'{"command":["get_property","video-params"],"request_id":1}\n')
            with client.makefile("rb") as stream:
                for _ in range(32):
                    line = stream.readline(65537)
                    if not line or len(line) > 65536:
                        return False
                    message = json.loads(line)
                    if message.get("request_id") == 1:
                        value = message.get("data")
                        return isinstance(value, dict) and value.get("w", 0) > 0 and value.get("h", 0) > 0
    except (OSError, ValueError):
        pass
    return False


# ------------------------------------------------------------------- lock


@contextlib.contextmanager
def _viewer_lock(data: Path | None = None):
    """Exclusive lock over one open/close: never two owned viewers at once."""
    path = lock_path(data)
    _ensure_dir(path.parent)
    handle = open(path, "a+b")
    deadline = time.monotonic() + LOCK_TIMEOUT_S
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                raise ViewerError("another viewer request is still in progress")
            time.sleep(POLL_S)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            handle.close()


# -------------------------------------------------------------- validation


def _media_path(raw) -> str:
    """One trusted absolute media path: regular, non-empty, known extension."""
    if not isinstance(raw, str) or not raw or len(raw) > MAX_PATH_CHARS:
        raise ValueError("viewer path must be a bounded non-empty string")
    if any(ch < " " for ch in raw):
        raise ValueError("viewer path must not contain control characters")
    if not os.path.isabs(raw):
        raise ValueError(f"viewer path must be absolute: {raw!r}")
    path = os.path.realpath(raw)
    if not os.path.isfile(path):
        raise ValueError(f"viewer path is not a regular file: {raw!r}")
    try:
        if os.path.getsize(path) <= 0:
            raise ValueError(f"viewer path is empty: {raw!r}")
    except OSError as exc:
        raise ValueError(f"viewer path is unreadable: {raw!r} ({exc})") from exc
    if os.path.splitext(path)[1].lower() not in MEDIA_EXTENSIONS:
        raise ValueError(f"viewer path is not a supported media file: {raw!r}")
    return path


def _validate(paths, kind, index, content_id, title, labels) -> dict:
    if not isinstance(kind, str) or kind not in KINDS:
        raise ValueError(f"viewer kind must be one of: {', '.join(KINDS)}")
    if not isinstance(paths, (list, tuple)) or not paths:
        raise ValueError("viewer paths must be a non-empty list")
    if len(paths) > MAX_PATHS:
        raise ValueError(f"viewer paths must contain at most {MAX_PATHS} entries")
    if kind in ("image", "video") and len(paths) != 1:
        raise ValueError(f"viewer kind {kind!r} takes exactly one path")
    resolved = [_media_path(raw) for raw in paths]
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(resolved):
        raise ValueError("viewer index must be an integer inside the path list")
    if not isinstance(content_id, str) or not _CONTENT_ID_RE.match(content_id):
        raise ValueError("viewer content_id must be a 64-character lowercase hex id")
    clean_title = _one_line(title, MAX_TITLE_CHARS) or Path(resolved[index]).name
    if labels is None:
        clean_labels = []
    elif isinstance(labels, (list, tuple)):
        if len(labels) > MAX_PATHS:
            raise ValueError(f"viewer labels must contain at most {MAX_PATHS} entries")
        clean_labels = [
            _one_line(label, MAX_LABEL_CHARS) if isinstance(label, str) else ""
            for label in labels
        ]
    else:
        raise ValueError("viewer labels must be a list of strings")
    # Missing captions fall back to the file's own name, so the OSD can always
    # name the entry the user landed on.
    captions = [
        clean_labels[position] if position < len(clean_labels) and clean_labels[position]
        else Path(path).name
        for position, path in enumerate(resolved)
    ]
    return {
        "content_id": content_id,
        "kind": kind,
        "index": index,
        "title": clean_title,
        "paths": resolved,
        "captions": captions,
    }


# ------------------------------------------------------------------ files


def _prepare(request: dict, data: Path | None = None) -> None:
    """Write the playlist, key bindings and overlay script the viewer loads."""
    script = script_path(data)
    _ensure_dir(script)
    lines = ["#EXTM3U"]
    for path, caption in zip(request["paths"], request["captions"]):
        lines.append(f"#EXTINF:-1,{caption}")
        lines.append(path)
    storage.atomic_write_bytes(
        playlist_path(data), ("\n".join(lines) + "\n").encode("utf-8")
    )
    _ensure_text(input_conf_path(data), INPUT_CONF)
    _ensure_text(script / SCRIPT_MAIN, MAIN_LUA)


def _argv(request: dict, data: Path | None = None) -> list[str]:
    executable = shutil.which(MPV_NAME)
    if not executable:
        raise ViewerError(f"{MPV_NAME} is not installed on PATH")
    argv = [
        executable,
        # Deterministic viewer: the user's mpv config must not rebind the keys
        # or open a different display; resume/cache files stay untouched.
        "--no-config",
        "--load-scripts=no",
        "--fullscreen",
        "--force-window=yes",
        "--keep-open=always",
        "--image-display-duration=inf",
        # Contain, never crop or stretch: an ultrawide frame stays whole.
        "--keepaspect=yes",
        "--panscan=0",
        "--mute=yes",
        "--aid=no",
        # The window title is the caller's own title; the current frame's caption
        # rides the playlist's per-entry titles and the overlay OSD instead.
        "--title=" + _expand_escape(request["title"]),
        "--playlist=" + str(playlist_path(data)),
        "--playlist-start=" + str(request["index"]),
        "--input-conf=" + str(input_conf_path(data)),
        "--script=" + str(script_path(data)),
        "--input-ipc-server=" + str(socket_path(data)),
        "--msg-level=all=warn",
    ]
    if request["kind"] in ("video", "gallery"):
        argv.append("--loop-file=inf")
    window_class = _test_window_class()
    if window_class is not None:
        argv.append("--x11-name=" + window_class)
        argv.append("--wayland-app-id=" + window_class)
    return argv


def _spawn(argv: list[str], data: Path | None = None) -> subprocess.Popen:
    """Detached viewer: its own session, empty stdin, output to a private log."""
    log = log_path(data)
    _ensure_dir(log.parent)
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with os.fdopen(fd, "ab", buffering=0) as handle:
            return subprocess.Popen(
                [str(argument) for argument in argv],
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                shell=False,
                close_fds=True,
                start_new_session=True,
            )
    except OSError as exc:
        raise ViewerError(f"cannot start {MPV_NAME}: {exc}") from exc


def _terminate_child(child: subprocess.Popen, grace: float = TERM_GRACE_S) -> None:
    """Reclaim the viewer this process just started (its own group, nobody else's).

    mpv starts in a new session, so it leads a process group whose id is its own
    pid. Only that group is signalled, and only while this Popen has not reaped
    the leader: after that the id could be recycled and must not be touched.
    """
    if child.poll() is not None:
        return
    _signal_group(child.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        child.wait(timeout=grace)
    if child.poll() is not None or not _group_alive(child.pid):
        return
    _signal_group(child.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        child.wait(timeout=KILL_GRACE_S)


def _await_ready(child: subprocess.Popen, data: Path | None = None) -> str:
    """Wait, bounded, for mpv to listen on its socket; return its start time.

    The request must return once the viewer is up, never when it closes, so this
    is the only wait: a viewer that dies or never opens its socket is reported
    with its own log tail instead of hanging the caller.
    """
    deadline = time.monotonic() + READY_TIMEOUT_S
    start_time = None
    while True:
        if child.poll() is not None:
            raise ViewerError(
                f"{MPV_NAME} exited before the viewer was ready: {_log_tail(data)}"
            )
        if start_time is None:
            start_time = _proc_start_time(child.pid)
        if start_time is not None and _socket_ready(socket_path(data)):
            return start_time
        if time.monotonic() >= deadline:
            raise ViewerError(
                f"{MPV_NAME} did not open its viewer socket within "
                f"{READY_TIMEOUT_S:.0f}s: {_log_tail(data)}"
            )
        time.sleep(POLL_S)


# ---------------------------------------------------------------- ownership


def _read_owner(data: Path | None = None) -> dict | None:
    value = storage.read_json(owner_path(data))
    if not isinstance(value, dict):
        return None
    pid = value.get("pid")
    start = value.get("start_time")
    recorded = value.get("socket")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if not isinstance(start, str) or not start:
        return None
    if not isinstance(recorded, str) or recorded != str(socket_path(data)):
        return None
    return value


def _forget(data: Path | None = None) -> None:
    """Drop the record and the socket file of a viewer that is gone.

    Only this module's own socket path is ever unlinked, never a path a record
    could point at.
    """
    owner_path(data).unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        socket_path(data).unlink()


def _stop(owner: dict, data: Path | None = None) -> None:
    """Close only the pinned viewer, rechecking identity before each signal."""
    pid, start = owner["pid"], owner["start_time"]
    if _identify(owner):
        _ipc_quit(owner["socket"])
        if not _wait_gone(pid, start, None, IPC_QUIT_WAIT_S) and _identify(owner):
            _signal_pid(pid, signal.SIGTERM)
            if not _wait_gone(pid, start, None, TERM_GRACE_S) and _identify(owner):
                _signal_pid(pid, signal.SIGKILL)
                if not _wait_gone(pid, start, None, KILL_GRACE_S):
                    raise ViewerError("the owned viewer did not close; media was left in place")
    _forget(data)


def _close_owned(data: Path | None = None) -> None:
    owner = _read_owner(data)
    if owner is not None:
        _stop(owner, data)


# -------------------------------------------------------------------- public


def open_media(paths: list[str], *, title: str, content_id: str, kind: str,
               index: int = 0, labels: list[str] | None = None,
               data: Path | None = None) -> dict:
    """Open one fullscreen viewer for already-trusted absolute media paths.

    `paths` arrive resolved by the caller (inventory media, capture frames, a
    variant's real render); every path is still validated here as a regular,
    non-empty, known-extension media file and the list is bounded. `kind` is
    `image`, `video` (exactly one path each) or `gallery`; `index` is the
    0-based entry to start on. `labels` optionally carries one caption per
    entry (for instance `Кадр 1/6 · 0.00 с`); the caption of the entry on screen
    is painted on the OSD, so arrow-key navigation always names its frame.

    Returns once the owner record is written and the viewer listens — never
    while it stays open. Any previously owned viewer is closed first.
    """
    request = _validate(paths, kind, index, content_id, title, labels)
    with _viewer_lock(data):
        _close_owned(data)
        socket_file = socket_path(data)
        if len(os.fsencode(str(socket_file))) > SOCKET_PATH_LIMIT:
            raise ViewerError(
                f"viewer socket path is too long for a unix socket: {socket_file}"
            )
        _prepare(request, data)
        socket_file.unlink(missing_ok=True)
        child = _spawn(_argv(request, data), data)
        try:
            start_time = _await_ready(child, data)
            storage.atomic_json(
                owner_path(data),
                {
                    "schema_version": SCHEMA_VERSION,
                    "content_id": request["content_id"],
                    "kind": request["kind"],
                    "pid": child.pid,
                    "pgid": child.pid,
                    "start_time": start_time,
                    "socket": str(socket_file),
                    "paths": list(request["paths"]),
                    "index": request["index"],
                    "title": request["title"],
                    "started_at": storage.now(),
                },
            )
        except BaseException:
            _terminate_child(child)
            raise
    return {
        "content_id": request["content_id"],
        "kind": request["kind"],
        "pid": child.pid,
        "index": request["index"],
        "count": len(request["paths"]),
        "title": request["title"],
        "paths": list(request["paths"]),
    }


def close_for_content(content_id: str, data: Path | None = None) -> None:
    """Close the owned viewer only when it belongs to `content_id`.

    Another wallpaper's viewer, and a record whose process is already gone (or
    whose pid was recycled), are left strictly alone; a stale record and this
    module's own socket file are still cleaned up.
    """
    if not isinstance(content_id, str) or not content_id:
        return
    with _viewer_lock(data):
        owner = _read_owner(data)
        if owner is None or owner.get("content_id") != content_id:
            return
        _stop(owner, data)
