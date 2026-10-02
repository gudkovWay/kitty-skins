#!/usr/bin/env python3
"""Six real temporal frames of one scene/web wallpaper, from ONE continuous run.

Two roles live in this file:

  driver (default)  capture.py runs it as
      vd-run.sh dbus-run-session -- <python> capture_scene.py <request.json>
    It never renders anything itself. It composes the renderer argv through the
    shared playback.renderer_command (the validated `--screen-root <output> --bg
    <background>` prefix becomes the proven window form `--window 0x0x960x540`
    with the absolute background as the trailing positional argument), asks the
    LIVE compositor to start that renderer inside the hidden
    `special:agent-tests` workspace (proven `hyprctl eval` + `hl.exec_cmd`
    rules), waits until the renderer itself owns a mapped XWayland window there,
    then takes exactly six x11grab frames at 2 fps in ONE continuous ffmpeg run,
    and finally reclaims only its own renderer process group.

  renderer role (`--renderer <request.json>`) is the tiny process the compositor
    spawns. It builds the renderer's explicitly isolated environment (private
    XDG roots, private session bus, X11 session, no Wayland), takes its own
    session/process group, publishes its identity (pid, /proc start time,
    process group) and `exec`s the renderer — so the recorded identity stays the
    renderer's own and the parent can verify it before anything is signalled.

There is no nested compositor and no display screenshot here: one ordinary
XWayland application renders on a hidden workspace of the live compositor and
only that application's own window is ever captured. The root window and the
monitors are never captured, no Noctalia is launched, no global rule, config or
monitor is created, and no process this module did not start is ever signalled.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

FRAME_COUNT = 6
# Six frames at 2 fps: one continuous x11grab run, no repeated renderer restart.
FRAMERATE = 2
FRAME_PATTERN = "frame-%02d.png"
# The proven hidden-preview geometry (X offset 0, Y offset 0, 960x540 window).
WINDOW_GEOMETRY = "0x0x960x540"
HIDDEN_WORKSPACE = "special:agent-tests"

IDENTITY_FILE = "_renderer_identity.json"
ROLE_REQUEST_FILE = "_renderer_request.json"
ROLE_LOG_FILE = "_renderer.log"
RESULT_FILE = "_capture_result.json"

STARTUP_WAIT_S = 20.0
MAP_WAIT_S = 45.0
WARMUP_S = 2.0
CAPTURE_TIMEOUT_S = 30.0
HYPRCTL_TIMEOUT_S = 10.0
XPROP_TIMEOUT_S = 10.0
POLL_S = 0.25
GROUP_POLL_S = 0.05
RENDERER_GRACE_S = 2.0
DIAGNOSTIC_LIMIT = 800

# The renderer inherits exactly these variables and nothing else: the live
# session's Wayland socket, its compositor signature and its host environment
# never reach it, and its runtime stays the private one vd-run.sh created.
ENV_KEYS = (
    "PATH",
    "HOME",
    "LD_LIBRARY_PATH",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
    "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR",
    "VD_ROOT",
    "NOCTALIA_STATE_HOME",
    "DBUS_SESSION_BUS_ADDRESS",
)
# renderer_command validates the output name, but a capture never renders to a
# monitor; any well-formed connector name is a valid placeholder for the prefix
# that is replaced below.
PLACEHOLDER_OUTPUT = "X11-1"

_RENDERER: dict = {"identity": None, "identity_path": None}


def _fail(message: str, code: int = 2):
    print(f"capture_scene: {message}", file=sys.stderr, flush=True)
    raise SystemExit(code)


# ----------------------------------------------------------------- json files


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json_atomic(path: Path, payload: dict) -> None:
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _tail(path: Path, limit: int = DIAGNOSTIC_LIMIT) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    return raw[-limit:].decode("utf-8", "replace").strip()


def _detail(message: str, log: Path) -> str:
    """A failure message with the bounded renderer stderr, when there is one."""
    tail = _tail(log)
    return f"{message}: {tail}" if tail else message


# ------------------------------------------------------------------ /proc probes


def _proc_stat(pid: int) -> tuple[str, int] | None:
    """(start time, process group) of a live process, or None when it is gone."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return None
    text = raw.decode("utf-8", "surrogateescape")
    close = text.rfind(")")
    if close < 0:
        return None
    fields = text[close + 2:].split()
    # Field 3 (state) is fields[0]: pgrp is field 5, starttime is field 22.
    if len(fields) < 20 or fields[0] == "Z" or not fields[2].isdigit():
        return None
    return fields[19], int(fields[2])


def _proc_start_time(pid: int) -> str | None:
    stat = _proc_stat(pid)
    return None if stat is None else stat[0]


def _alive(identity: dict) -> bool:
    """Whether the recorded renderer is still exactly that process/group."""
    observed = _proc_stat(identity["pid"])
    if observed is None:
        return False
    start_time, pgid = observed
    return start_time == identity["start_time"] and pgid == identity["pgid"]


# ----------------------------------------------------------------- group reclaim


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _reclaim(identity: dict) -> None:
    """Reclaim exactly the renderer group this capture started, then confirm it.

    The group is only ever signalled when the recorded identity proved it leads
    its own group (pgid == pid), so a stranger's group can never be hit; there is
    no pkill and no name-based kill anywhere in this module.
    """
    pgid = identity["pgid"]
    if pgid != identity["pid"] or pgid <= 0:
        return
    for grace, sig in ((RENDERER_GRACE_S, signal.SIGTERM), (RENDERER_GRACE_S, signal.SIGKILL)):
        observed = _proc_stat(identity["pid"])
        if observed is not None and observed != (identity["start_time"], pgid):
            return
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        except OSError:
            return
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not _group_alive(pgid):
                return
            time.sleep(GROUP_POLL_S)


def _interrupt(signum, _frame) -> None:
    """On cancellation reclaim our own renderer, then exit by signal."""
    identity = _RENDERER.get("identity")
    if identity is None and _RENDERER.get("identity_path") is not None:
        identity = _late_identity(_RENDERER["identity_path"])
    if identity is not None:
        _reclaim(identity)
    raise SystemExit(128 + signum)


def _late_identity(path: Path, timeout: float = 3.0) -> dict | None:
    """Best-effort identity read while cancelling: the role may just be starting."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        identity = _verified_identity(path)
        if identity is not None:
            return identity
        time.sleep(GROUP_POLL_S)
    return None


# ------------------------------------------------------------------- requests


def _load_driver_request(path: Path) -> dict:
    value = _read_json(path)
    if not isinstance(value, dict):
        _fail("capture request is unreadable or not a JSON object")
    if not isinstance(value.get("item"), dict) or not isinstance(value.get("options"), dict):
        _fail("capture request needs an item object and an options object")
    if not isinstance(value.get("output_dir"), str) or not os.path.isabs(value["output_dir"]):
        _fail("capture request needs an absolute output_dir")
    for key in ("parent_display", "parent_hypr_signature", "parent_runtime"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            _fail(f"capture request lacks the parent display identity ({key})")
    return value


def _load_role_request(path: Path) -> dict:
    value = _read_json(path)
    if not isinstance(value, dict):
        _fail("renderer request is unreadable or not a JSON object")
    command = value.get("command")
    if not isinstance(command, list) or not command:
        _fail("renderer request needs a command argv")
    if not all(isinstance(part, str) and part for part in command):
        _fail("renderer request command must be a list of non-empty strings")
    env = value.get("env")
    if not isinstance(env, dict):
        _fail("renderer request needs an environment object")
    if not all(
        isinstance(entry_key, str) and isinstance(entry_value, str)
        for entry_key, entry_value in env.items()
    ):
        _fail("renderer request environment must map strings to strings")
    cwd = value.get("renderer_cwd")
    if cwd is not None and (not isinstance(cwd, str) or not os.path.isabs(cwd)):
        _fail("renderer request renderer_cwd must be an absolute path or null")
    for key in ("identity_file", "log_file"):
        if not isinstance(value.get(key), str) or not os.path.isabs(value[key]):
            _fail(f"renderer request needs an absolute {key}")
    return value


# -------------------------------------------------------------- sandbox guard


def _inside(path: str, root: str) -> bool:
    try:
        child = os.path.realpath(path)
    except OSError:
        return False
    return child == root or child.startswith(root + os.sep)


def _require_sandbox() -> None:
    """Refuse direct/live execution: this driver may only run inside vd-run.sh.

    Containment is proven by VD_ROOT with every XDG root — including
    XDG_RUNTIME_DIR — redirected under it. Unlike the old nested-compositor
    path there is no nested Wayland display to demand any more: this driver
    renders one ordinary XWayland application on the live compositor's hidden
    workspace and captures only that application's own window, so the live
    session is expected and safe. There is no fallback to a display screenshot.
    """
    root = os.environ.get("VD_ROOT", "").strip()
    if not root or not os.path.isabs(root):
        _fail("refusing to run outside the desktop sandbox: VD_ROOT is unset")
    root_real = os.path.realpath(root)
    for name in (
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
    ):
        value = os.environ.get(name, "").strip()
        if not value or not _inside(value, root_real):
            _fail(f"refusing to run outside the desktop sandbox: {name} is not inside VD_ROOT")


# ----------------------------------------------------------------- renderer argv


def _playback():
    try:
        import playback
    except ImportError as exc:
        _fail(f"backend playback module is unavailable: {exc}")
    return playback


def _capture_options(options) -> dict:
    """Effective settings forced for a short hidden capture.

    Rendering only: silence, no audio detector and no fullscreen pause, so the
    renderer never opens an audio device or waits on a pulse loop. `volume` is
    kept at 0 as well; playback.renderer_command drops `--volume` while
    `silent` is true, so the argv never carries both flags.
    """
    options = options if isinstance(options, dict) else {}
    engine = dict(options["engine"]) if isinstance(options.get("engine"), dict) else {}
    properties = dict(options["properties"]) if isinstance(options.get("properties"), dict) else {}
    engine.update(
        {
            "silent": True,
            "noautomute": True,
            "no_audio_processing": True,
            "no_fullscreen_pause": True,
            "volume": 0,
            "fps": 30,
        }
    )
    return {"engine": engine, "properties": properties}


def _renderer_argv(item: dict, options) -> list[str]:
    """The real renderer argv for a hidden preview window.

    playback.renderer_command stays the single authority on flags and property
    overrides; only its validated monitor prefix is replaced by the proven
    window form, with the absolute background as the trailing positional.
    """
    playback = _playback()
    try:
        command = playback.renderer_command(item, PLACEHOLDER_OUTPUT, options)
    except Exception as exc:  # renderer_command rejects unusable settings
        _fail(f"renderer_command refused the capture request: {type(exc).__name__}: {exc}")
    if len(command) < 5 or command[1] != "--screen-root" or command[3] != "--bg":
        _fail("renderer_command did not produce the validated --screen-root/--bg prefix")
    background = command[4]
    if not os.path.isabs(background):
        _fail(f"renderer_command produced a non-absolute background: {background!r}")
    return [command[0], "--window", WINDOW_GEOMETRY, *command[5:], background]


def _renderer_environment(request: dict, executable: str) -> tuple[dict, str | None]:
    """The isolated renderer environment plus its optional working directory.

    Every value comes from this driver's own (vd-run.sh + dbus-run-session)
    environment; DISPLAY and the X11 session type come from the capture request.
    Only the bare-binary fallback needs playback's library directory and working
    directory, exactly like a normal live launch.
    """
    env: dict[str, str] = {}
    for key in ENV_KEYS:
        value = os.environ.get(key)
        if isinstance(value, str) and value:
            env[key] = value
    if not env.get("PATH"):
        _fail("refusing to start the renderer without PATH")
    if not env.get("DBUS_SESSION_BUS_ADDRESS"):
        _fail(
            "refusing to start the renderer without a private session bus "
            "(DBUS_SESSION_BUS_ADDRESS is unset; run through dbus-run-session)"
        )
    env["DISPLAY"] = request["parent_display"].strip()
    env["XDG_SESSION_TYPE"] = "x11"

    cwd = None
    playback = _playback()
    try:
        bare = os.path.realpath(executable) == os.path.realpath(str(playback.RENDERER_FALLBACK))
    except OSError:
        bare = False
    if bare:
        cwd = str(playback.RENDERER_FALLBACK.parent)
        base = env.get("LD_LIBRARY_PATH")
        env["LD_LIBRARY_PATH"] = (
            f"{playback.RENDERER_LIB_DIR}:{base}" if base else str(playback.RENDERER_LIB_DIR)
        )
    return env, cwd


# ------------------------------------------------------------ live IPC helpers


def _ipc_env(request: dict) -> dict:
    """Environment for talking to the LIVE compositor over its own socket.

    Only the parent's runtime and instance signature are used: the renderer's
    private XDG roots must never reach hyprctl, and this driver's private
    runtime must never be mistaken for the live one.
    """
    env = {
        "HYPRLAND_INSTANCE_SIGNATURE": request["parent_hypr_signature"].strip(),
        "XDG_RUNTIME_DIR": request["parent_runtime"].strip(),
    }
    for key in ("PATH", "HOME"):
        value = os.environ.get(key)
        if isinstance(value, str) and value:
            env[key] = value
    env.setdefault("PATH", "/usr/bin:/bin")
    return env


def _hyprctl(ipc_env: dict, args: list[str], *, json_mode: bool):
    argv = ["hyprctl"]
    if json_mode:
        argv.append("-j")
    argv.extend(["-i", ipc_env["HYPRLAND_INSTANCE_SIGNATURE"], *args])
    try:
        proc = subprocess.run(
            argv, capture_output=True, timeout=HYPRCTL_TIMEOUT_S, check=False, env=ipc_env
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.decode("utf-8", "replace")
    if not json_mode:
        return out
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def _x11_env(display: str) -> dict:
    env = {"DISPLAY": display}
    for key in ("PATH", "HOME"):
        value = os.environ.get(key)
        if isinstance(value, str) and value:
            env[key] = value
    env.setdefault("PATH", "/usr/bin:/bin")
    return env


def _xprop(display: str, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["xprop", *args],
            capture_output=True,
            timeout=XPROP_TIMEOUT_S,
            check=False,
            env=_x11_env(display),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


def _window_xid(display: str, pid: int) -> str | None:
    """The X11 window id owned by `pid`, matched through _NET_WM_PID only."""
    root = _xprop(display, "-root", "_NET_CLIENT_LIST")
    if not root:
        return None
    _, _, value = root.partition("#")
    for xid in re.findall(r"0x[0-9A-Fa-f]+", value):
        props = _xprop(display, "-id", xid, "_NET_WM_PID")
        if not props:
            continue
        found = re.search(r"_NET_WM_PID\s*\(CARDINAL\)\s*=\s*(\d+)", props)
        if found and int(found.group(1)) == pid:
            return xid
    return None


def _client(ipc_env: dict, pid: int):
    """(owns_window, workspace_name) for our renderer pid, or None if unreadable."""
    clients = _hyprctl(ipc_env, ["clients"], json_mode=True)
    if not isinstance(clients, list):
        return None
    for entry in clients:
        if not isinstance(entry, dict) or entry.get("pid") != pid:
            continue
        if entry.get("xwayland") is not True:
            _fail("the renderer window is not an XWayland window; refusing to capture")
        workspace = entry.get("workspace")
        name = workspace.get("name") if isinstance(workspace, dict) else None
        return True, name if isinstance(name, str) else None
    return False, None


# -------------------------------------------------------------------- identity


def _verified_identity(path: Path) -> dict | None:
    """The published identity, but only while that exact process still runs."""
    value = _read_json(path)
    if not isinstance(value, dict):
        return None
    pid = value.get("pid")
    start_time = value.get("start_time")
    pgid = value.get("pgid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if not isinstance(start_time, str) or not start_time:
        return None
    if isinstance(pgid, bool) or not isinstance(pgid, int) or pgid != pid:
        return None
    identity = {"pid": pid, "start_time": start_time, "pgid": pgid}
    return identity if _alive(identity) else None


def _await_identity(path: Path, log: Path) -> dict:
    """Wait for the identity record, and only accept a proven live ownership."""
    deadline = time.monotonic() + STARTUP_WAIT_S
    while time.monotonic() < deadline:
        value = _read_json(path)
        if isinstance(value, dict):
            error = value.get("error")
            if isinstance(error, str) and error:
                _fail(_detail(f"the renderer role refused to start: {error}", log))
            pid = value.get("pid")
            start_time = value.get("start_time")
            pgid = value.get("pgid")
            if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
                if not isinstance(start_time, str) or not start_time:
                    _fail("the renderer identity record has no /proc start time")
                if isinstance(pgid, bool) or not isinstance(pgid, int) or pgid != pid:
                    _fail(
                        "the renderer identity record does not prove its own process "
                        "group; refusing to continue"
                    )
                identity = {"pid": pid, "start_time": start_time, "pgid": pgid}
                if _alive(identity):
                    return identity
                _fail(
                    _detail(
                        "the renderer exited right after publishing its identity", log
                    )
                )
        time.sleep(POLL_S)
    _fail(
        _detail(
            "the renderer did not publish a live identity within "
            f"{STARTUP_WAIT_S:.0f}s",
            log,
        )
    )


def _require_live(identity: dict, phase: str, log: Path) -> None:
    if not _alive(identity):
        _fail(_detail(f"the renderer exited {phase}", log))


# --------------------------------------------------------------------- launch


def _launch(ipc_env: dict, script: Path, role_request: Path) -> None:
    """Start the renderer role on the live compositor's hidden workspace."""
    shell_command = "exec {python} {script} --renderer {request}".format(
        python=shlex.quote(sys.executable),
        script=shlex.quote(str(script)),
        request=shlex.quote(str(role_request)),
    )
    rules = (
        '{{workspace = "{workspace} silent", float = true, '
        "no_focus = true, render_unfocused = true}}"
    ).format(workspace=HIDDEN_WORKSPACE)
    script_text = f"hl.exec_cmd({json.dumps(shell_command)}, {rules})"
    result = _hyprctl(ipc_env, ["eval", script_text], json_mode=False)
    if result is None:
        _fail("the live compositor refused the hidden launch (hyprctl eval failed)")
    if "error" in result.lower():
        _fail(f"the live compositor rejected the hidden launch: {result.strip()[:400]}")


def _await_window(ipc_env: dict, identity: dict, display: str, log: Path) -> str:
    """Block until the renderer owns a mapped XWayland window on the hidden workspace."""
    deadline = time.monotonic() + MAP_WAIT_S
    last = "no compositor answer"
    while time.monotonic() < deadline:
        if not _alive(identity):
            _fail(_detail("the renderer exited before mapping its window", log))
        state = _client(ipc_env, identity["pid"])
        if state is None:
            last = "the live compositor did not answer hyprctl clients"
        else:
            owns_window, workspace = state
            if owns_window:
                if workspace != HIDDEN_WORKSPACE:
                    _fail(
                        f"the renderer window appeared on workspace {workspace!r}, "
                        f"not {HIDDEN_WORKSPACE!r}; refusing to capture"
                    )
                xid = _window_xid(display, identity["pid"])
                if xid is not None:
                    return xid
                last = "the renderer window is not an X11 window yet"
        time.sleep(POLL_S)
    _fail(
        _detail(
            "the renderer never mapped a hidden XWayland window within "
            f"{MAP_WAIT_S:.0f}s ({last})",
            log,
        )
    )


# -------------------------------------------------------------------- capture


def _capture_frames(display: str, xid: str, out_dir: Path) -> str:
    """ONE continuous ffmpeg x11grab run of six frames; returns its stderr log."""
    argv = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-v",
        "info",
        "-f",
        "x11grab",
        "-window_id",
        xid,
        "-draw_mouse",
        "0",
        "-framerate",
        str(FRAMERATE),
        "-i",
        display,
        "-frames:v",
        str(FRAME_COUNT),
        "-vf",
        "showinfo",
        "-y",
        str(out_dir / FRAME_PATTERN),
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=CAPTURE_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        _fail(f"the ffmpeg x11grab capture did not finish within {CAPTURE_TIMEOUT_S:.0f}s")
    except OSError as exc:
        _fail(f"the ffmpeg x11grab capture could not start: {exc}")
    stderr = proc.stderr.decode("utf-8", "replace")
    if proc.returncode != 0:
        _fail(
            f"the ffmpeg x11grab capture failed (code {proc.returncode}): "
            f"{stderr.strip()[-DIAGNOSTIC_LIMIT:]}"
        )
    return stderr


def _sample_times(stderr_text: str) -> list[float]:
    """Relative sample times from FFmpeg's actual showinfo timestamps."""
    found: list[float] = []
    for line in stderr_text.splitlines():
        if "showinfo" not in line:
            continue
        match = re.search(r"pts_time:\s*(-?\d+(?:\.\d+)?)", line)
        if match:
            found.append(float(match.group(1)))
    if len(found) != FRAME_COUNT:
        _fail("ffmpeg did not report timestamps for exactly six captured frames")
    base = found[0]
    relative = [round(value - base, 6) for value in found]
    if relative[0] < 0:
        _fail("ffmpeg reported a negative capture timestamp")
    for index in range(1, len(relative)):
        if relative[index] <= relative[index - 1]:
            _fail("ffmpeg capture timestamps are not strictly increasing")
    return relative


def _frames(out_dir: Path, times: list[float]) -> list[dict]:
    """The six real frames on disk, or a failure; nothing is published otherwise."""
    frames = []
    for index, time_s in enumerate(times, start=1):
        path = out_dir / (FRAME_PATTERN % index)
        try:
            size = path.stat().st_size
        except OSError as exc:
            _fail(f"the capture produced no frame {index}: {exc}")
        if size <= 0:
            _fail(f"the capture produced an empty frame {index}: {path}")
        frames.append({"file": str(path), "time_s": round(float(time_s), 6)})
    extra = out_dir / (FRAME_PATTERN % (FRAME_COUNT + 1))
    if extra.exists():
        _fail(f"the capture produced more than {FRAME_COUNT} frames: {extra}")
    return frames


# ---------------------------------------------------------------------- roles


def _driver(request_path: Path) -> int:
    request = _load_driver_request(request_path)
    _require_sandbox()
    out_dir = Path(request["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    display = request["parent_display"].strip()
    ipc_env = _ipc_env(request)

    options = _capture_options(request["options"])
    command = _renderer_argv(request["item"], options)
    environ, renderer_cwd = _renderer_environment(request, command[0])

    script = Path(__file__).resolve()
    role_request = out_dir / ROLE_REQUEST_FILE
    identity_path = out_dir / IDENTITY_FILE
    log_path = out_dir / ROLE_LOG_FILE
    role_request.write_text(
        json.dumps(
            {
                "command": command,
                "env": environ,
                "renderer_cwd": renderer_cwd,
                "identity_file": str(identity_path),
                "log_file": str(log_path),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    _RENDERER["identity"] = None
    _RENDERER["identity_path"] = identity_path
    signal.signal(signal.SIGTERM, _interrupt)
    signal.signal(signal.SIGINT, _interrupt)

    frames = None
    try:
        _launch(ipc_env, script, role_request)
        identity = _await_identity(identity_path, log_path)
        _RENDERER["identity"] = identity
        xid = _await_window(ipc_env, identity, display, log_path)
        time.sleep(WARMUP_S)
        _require_live(identity, "during the capture warmup", log_path)
        stderr_text = _capture_frames(display, xid, out_dir)
        _require_live(identity, "while the frames were captured", log_path)
        frames = _frames(out_dir, _sample_times(stderr_text))
    finally:
        identity = _RENDERER.get("identity")
        if identity is not None:
            _reclaim(identity)
        _RENDERER["identity"] = None
        _RENDERER["identity_path"] = None
        for scratch in (role_request, identity_path, log_path):
            try:
                scratch.unlink()
            except OSError:
                pass

    _write_json_atomic(out_dir / RESULT_FILE, {"frames": frames})
    return 0


def _role(request_path: Path) -> int:
    request = _load_role_request(request_path)
    identity_path = Path(request["identity_file"])
    log_path = Path(request["log_file"])
    command = [str(part) for part in request["command"]]
    env = {str(key): str(value) for key, value in request["env"].items()}

    try:
        if os.getsid(0) != os.getpid():
            os.setsid()
        if os.getsid(0) != os.getpid() or os.getpgrp() != os.getpid():
            _write_json_atomic(
                identity_path,
                {"error": "the renderer role is not its own session/process group leader"},
            )
            _fail("refusing to exec the renderer inside the compositor's session")
    except OSError as exc:
        _write_json_atomic(identity_path, {"error": f"cannot take its own session: {exc}"})
        _fail(f"cannot take its own session: {exc}")

    start_time = _proc_start_time(os.getpid())
    if start_time is None:
        _write_json_atomic(identity_path, {"error": "cannot read its own /proc start time"})
        _fail("cannot read its own /proc start time")
    # Published before exec: the /proc start time and the pid survive exec, so
    # the parent can verify this exact process before it ever signals anything.
    _write_json_atomic(
        identity_path,
        {"pid": os.getpid(), "start_time": start_time, "pgid": os.getpgrp()},
    )

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError as exc:
        _write_json_atomic(identity_path, {"error": f"cannot open the renderer log: {exc}"})
        _fail(f"cannot open the renderer log: {exc}")
    os.dup2(handle, 1)
    os.dup2(handle, 2)
    if handle > 2:
        os.close(handle)

    cwd = request.get("renderer_cwd")
    if cwd:
        try:
            os.chdir(cwd)
        except OSError as exc:
            _write_json_atomic(identity_path, {"error": f"cannot enter {cwd}: {exc}"})
            _fail(f"cannot enter the renderer directory {cwd}: {exc}")

    # The renderer sees exactly the isolated environment and nothing of the
    # compositor's host environment.
    os.environ.clear()
    os.environ.update(env)
    try:
        os.execvpe(command[0], command, env)
    except OSError as exc:
        _write_json_atomic(identity_path, {"error": f"cannot exec the renderer: {exc}"})
        _fail(f"cannot exec the renderer: {exc}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "--renderer":
        return _role(Path(argv[2]))
    if len(argv) != 2:
        _fail("usage: capture_scene.py <request.json> | capture_scene.py --renderer <request.json>")
    return _driver(Path(argv[1]))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
