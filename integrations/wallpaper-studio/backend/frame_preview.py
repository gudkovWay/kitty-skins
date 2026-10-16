#!/usr/bin/env python3
"""Interactive live Kitty frame preview: one dedicated real window.

Instead of recording frames, the studio opens a real Kitty terminal on the
live Hyprland session, framed by the actual kitty-skins plugin, loaded straight
from the candidate pack (packs are immutable and validated, so no copy is
made). The window belongs to the dedicated class `kitty-skin-preview`, so
persisted active skins, the global active runtime and every other Kitty window
stay untouched; the plugin only sees its address-scoped Lua preview API for
this one window.

Ownership is a small status record under $XDG_RUNTIME_DIR/wallpaper-studio/:
PID with its /proc start time, the Hyprland `0x...` address and the pack the
window was opened for. `cached()` reports the live window only while that exact
process is still alive and its window is still mapped; anything stale is
removed and reported as absent. No preview media is ever written under the
data root — there is no media at all.

A pack may ship an optional `companion.json` (`{"schema_version": 1, "profile":
"..."}`) naming a shader profile from the trusted local effects catalog. The
catalog is the effect plugin's own profiles.json (XDG-aware default,
$WALLPAPER_STUDIO_EFFECTS_CATALOG override); a pack only ever names a profile,
never a path. The resolved shader is attached to the dedicated preview window
class through HyprWindowShade's Lua surface and cleared again on replacement,
explicit close, failure and stale-record removal. Packs without a companion
keep their previous behaviour.

A failed open raises FramePreviewError with an actionable message and reclaims
only the Kitty it launched itself; unrelated Kitty processes are never touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

# Flat modules: put this backend directory on sys.path like every sibling.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import process  # type: ignore
import storage  # type: ignore

UUID32 = re.compile(r"^[0-9a-f]{32}$")
MOTIONS = ("static", "candles")

#: Actual Hyprland plugin; WALLPAPER_STUDIO_KITTY_PLUGIN overrides.
DEFAULT_PLUGIN = Path("/home/q/.local/lib/kitty-skins/libkitty-skins.so")
PLUGIN_ENV = "WALLPAPER_STUDIO_KITTY_PLUGIN"

#: The one dedicated live preview window.
WINDOW_CLASS = "kitty-skin-preview"
WINDOW_TITLE = "Kitty frame preview"

#: The owner's Super+A semantics, applied programmatically.
FLOAT_W = 1280
FLOAT_H = 720

MAP_WAIT_S = 45.0
HYPRCTL_TIMEOUT_S = 10.0
POLL_S = 0.25

#: Ownership/status record, never under the data root.
RECORD_NAME = "kitty-preview.json"

#: Optional pack companion: {"schema_version": 1, "profile": "<catalog name>"}.
COMPANION_NAME = "companion.json"
COMPANION_SCHEMA = 1

#: Trusted local effects catalog: profile name -> shader file under shader_dir.
#: The default is the effect plugin's own config, resolved XDG-aware; the
#: override exists for sandboxes and tests.
EFFECTS_CATALOG_ENV = "WALLPAPER_STUDIO_EFFECTS_CATALOG"
EFFECTS_CATALOG_REL = ("hypr", "noctalia-plugins", "w-engine-effects", "profiles.json")
EFFECTS_CLEAR = "clear"

#: Catalog tokens (profile names and shader file names) are never paths.
SAFE_EFFECT_TOKEN = re.compile(r"^[A-Za-z0-9_.%-]+$")

_SAMPLE = """\
Wallpaper Studio — живое превью рамки
Настоящее окно Kitty в рамке kitty-skins; кадр переключает плагин.

$ echo "живой кандидат, без записи"
живой кандидат, без записи
$ █
"""


class FramePreviewError(RuntimeError):
    """User-visible preview failure; message is safe to show and actionable."""


# ------------------------------------------------------------------ discovery


def _plugin_path() -> Path:
    override = os.environ.get(PLUGIN_ENV)
    path = Path(override) if override else DEFAULT_PLUGIN
    if not path.is_file():
        raise FramePreviewError(
            f"kitty-skins Hyprland plugin not found at {path}; "
            f"install it (hyprpm) or set {PLUGIN_ENV}"
        )
    return path


def _which(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise FramePreviewError(
            f"required tool {name!r} not found in PATH; install it to open previews"
        )
    return found


def _require_live_session() -> None:
    """The preview is interactive: it needs the real Hyprland session."""
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") or not os.environ.get("WAYLAND_DISPLAY"):
        raise FramePreviewError(
            "live preview needs a running Hyprland session "
            "(HYPRLAND_INSTANCE_SIGNATURE and WAYLAND_DISPLAY are unset)"
        )


def _runtime_record_path() -> Path:
    root = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if not root:
        raise FramePreviewError(
            "XDG_RUNTIME_DIR is unset; cannot own the live preview window"
        )
    return Path(root) / "wallpaper-studio" / RECORD_NAME


# --------------------------------------------------------------------- digest


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pack_fingerprint(pack: Path) -> str:
    """Digest of every regular pack file plus the plugin identity.

    Symlinks and other non-regular entries are refused: a pack is a plain
    directory tree, and digesting through a link would silently pin a target
    the variant does not own.
    """
    digest = hashlib.sha256()
    digest.update(b"live-preview-v1\n")
    try:
        plugin = _plugin_path()
    except FramePreviewError:
        digest.update(b"plugin\x00missing\n")
    else:
        stat = plugin.stat()
        digest.update(f"plugin\x00{stat.st_size}\x00{stat.st_mtime_ns}\n".encode())
    for item in sorted(pack.rglob("*")):
        if "tmp" in item.relative_to(pack).parts:
            continue
        if item.is_symlink():
            raise FramePreviewError(f"pack contains a symlink: {item}")
        if item.is_dir():
            continue
        if not item.is_file():
            raise FramePreviewError(f"pack contains a non-file entry: {item}")
        rel = item.relative_to(pack).as_posix()
        digest.update(rel.encode() + b"\n")
        digest.update(_file_digest(item).encode() + b"\n")
    return digest.hexdigest()


def _variant_pack(variant: dict) -> tuple[str, str, Path]:
    """(variant_id, motion, pack directory) after strict validation."""
    variant_id = variant.get("id")
    if not isinstance(variant_id, str) or not UUID32.fullmatch(variant_id):
        raise FramePreviewError("invalid variant id for preview")
    motion = variant.get("motion", "static")
    if motion not in MOTIONS:
        raise FramePreviewError(f"variant motion {motion!r} cannot be previewed")
    pack = variant.get("installed_path") or variant.get("pack_path")
    if not isinstance(pack, str) or not pack:
        raise FramePreviewError("variant has no pack path to preview")
    pack_dir = Path(pack)
    if not pack_dir.is_absolute():
        raise FramePreviewError(f"variant pack path is not absolute: {pack}")
    if not pack_dir.is_dir():
        raise FramePreviewError(f"variant pack directory is missing: {pack_dir}")
    if not (pack_dir / "kitty.conf").is_file():
        raise FramePreviewError(f"variant pack has no kitty.conf: {pack_dir}")
    return variant_id, motion, pack_dir


# -------------------------------------------------------------------- hyprctl


def _run_hyprctl(*args: str, timeout: float = HYPRCTL_TIMEOUT_S):
    try:
        return subprocess.run(
            ["hyprctl", *args], capture_output=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _hyprctl(*args: str, timeout: float = HYPRCTL_TIMEOUT_S) -> str | None:
    proc = _run_hyprctl(*args, timeout=timeout)
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


def _hyprctl_checked(label: str, *args: str) -> str:
    proc = _run_hyprctl(*args)
    if proc is None:
        raise FramePreviewError(f"{label}: hyprctl did not respond")
    output = proc.stdout.decode("utf-8", "replace")
    if proc.returncode != 0:
        error = proc.stderr.decode("utf-8", "replace").strip() or output.strip()
        detail = error[:400] if error else f"hyprctl exited with code {proc.returncode}"
        raise FramePreviewError(f"{label}: {detail}")
    return output


def _clients() -> list:
    raw = _hyprctl("-j", "clients")
    if not raw:
        return []
    try:
        clients = json.loads(raw)
    except ValueError:
        return []
    return clients if isinstance(clients, list) else []


def _owned_window(pid: int, address: str | None = None) -> dict | None:
    """The mapped preview-class client for `pid`, optionally at `address`."""
    for client in _clients():
        if not isinstance(client, dict):
            continue
        if client.get("class") != WINDOW_CLASS or client.get("pid") != pid:
            continue
        if not client.get("mapped", False):
            continue
        if address is not None and str(client.get("address") or "") != address:
            continue
        return client
    return None


# -------------------------------------------------------------- pid ownership


def _pid_start_time(pid: int) -> str | None:
    """Field 22 of /proc/<pid>/stat: the kernel's process start time."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return None
    # comm may contain spaces and parentheses; split after the last ')'.
    head = stat.rpartition(b")")[2].split()
    if len(head) < 20:
        return None
    return head[19].decode("ascii", "replace")


def _process_alive(pid: int, start_time: str) -> bool:
    return _pid_start_time(pid) == start_time


def _read_record() -> dict | None:
    try:
        record = storage.read_json(_runtime_record_path())
    except (OSError, ValueError, FramePreviewError):
        return None
    return record if isinstance(record, dict) else None


def _write_record(record: dict) -> None:
    path = _runtime_record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    storage.atomic_json(path, record)


def _drop_record() -> None:
    try:
        _runtime_record_path().unlink()
    except OSError:
        pass


def _discard_stale(record: dict) -> None:
    """Drop a dead record only after its companion cleanup succeeds.

    Preserve ownership after an IPC failure so a later close/render can retry.
    """
    if isinstance(record.get("companion"), dict) and not _clear_companion():
        return
    _drop_record()


def _reclaim_pid(pid: int, start_time: str) -> None:
    """Terminate only the exact process this record still owns."""
    if not _process_alive(pid, start_time):
        return
    try:
        os.killpg(pid, 15)
    except (ProcessLookupError, PermissionError, OSError):
        return
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if not _process_alive(pid, start_time):
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, 9)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _preview_lua(action: str, address: str, pack: Path) -> str:
    return (
        f"hl.dispatch(hl.plugin.kittyskins.{action}("
        f"{json.dumps(address)}, {json.dumps(str(pack))}))"
    )


def _clear_preview(address: str, pack: Path) -> None:
    """Best-effort direct clear; a stale-clear refusal is tolerated."""
    _hyprctl("eval", _preview_lua("preview_clear", address, pack))


# ----------------------------------------------------------- pack companion


def _xdg_config_home() -> Path:
    raw = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if raw:
        return Path(raw)
    home = os.environ.get("HOME", "").strip() or str(Path.home())
    return Path(home) / ".config"


def _effects_catalog_path() -> Path:
    override = os.environ.get(EFFECTS_CATALOG_ENV, "").strip()
    if override:
        return Path(os.path.expanduser(override))
    return _xdg_config_home().joinpath(*EFFECTS_CATALOG_REL)


def _read_pack_companion(pack: Path) -> str | None:
    """The requested effect profile from an optional companion.json, or None."""
    path = pack / COMPANION_NAME
    if not path.exists():
        return None
    if not path.is_file():
        raise FramePreviewError(f"pack companion {COMPANION_NAME} is not a regular file")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FramePreviewError(f"pack companion {COMPANION_NAME} is not readable JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise FramePreviewError(f"pack companion {COMPANION_NAME} must be a JSON object")
    unexpected = sorted(set(raw) - {"schema_version", "profile"})
    if unexpected:
        raise FramePreviewError(
            f"pack companion {COMPANION_NAME} has unexpected keys: {unexpected}"
        )
    schema = raw.get("schema_version")
    if isinstance(schema, bool) or schema != COMPANION_SCHEMA:
        raise FramePreviewError(
            f"pack companion {COMPANION_NAME} has unsupported schema_version {schema!r}; "
            f"this preview understands schema {COMPANION_SCHEMA}"
        )
    profile = raw.get("profile")
    if not isinstance(profile, str) or not profile:
        raise FramePreviewError(f"pack companion {COMPANION_NAME} needs a non-empty profile name")
    if not SAFE_EFFECT_TOKEN.fullmatch(profile):
        raise FramePreviewError(
            f"pack companion profile {profile!r} is not a safe catalog name"
        )
    return profile


def resolve_companion_shader(profile: str) -> Path:
    """Map a pack's profile name to a shader through the trusted local catalog.

    The pack never supplies a path: only the catalog's shader_dir and its
    profile -> file entry take part, and both are validated as plain tokens, so
    a generated manifest cannot point the compositor at an arbitrary shader.
    """
    catalog_path = _effects_catalog_path()
    try:
        raw = catalog_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FramePreviewError(
            f"pack requests effect profile {profile!r} but the effects catalog "
            f"is missing at {catalog_path}; install the effects plugin or set "
            f"{EFFECTS_CATALOG_ENV}"
        ) from None
    except OSError as exc:
        raise FramePreviewError(f"effects catalog {catalog_path} is unreadable: {exc}") from exc
    try:
        catalog = json.loads(raw)
    except ValueError as exc:
        raise FramePreviewError(f"effects catalog {catalog_path} is not valid JSON: {exc}") from exc
    if not isinstance(catalog, dict):
        raise FramePreviewError(f"effects catalog {catalog_path} must be a JSON object")
    shader_dir = catalog.get("shader_dir")
    profiles = catalog.get("profiles")
    if not isinstance(shader_dir, str) or not shader_dir:
        raise FramePreviewError(f"effects catalog {catalog_path} has no shader_dir")
    if not isinstance(profiles, dict):
        raise FramePreviewError(f"effects catalog {catalog_path} has no profiles map")
    filename = profiles.get(profile)
    if not isinstance(filename, str) or not filename:
        raise FramePreviewError(
            f"effects catalog {catalog_path} does not declare profile {profile!r}"
        )
    if not SAFE_EFFECT_TOKEN.fullmatch(filename) or filename in (".", ".."):
        raise FramePreviewError(
            f"effects catalog profile {profile!r} maps to unsafe shader name {filename!r}"
        )
    directory = Path(os.path.expanduser(shader_dir))
    if not directory.is_absolute():
        raise FramePreviewError(f"effects catalog shader_dir is not absolute: {shader_dir!r}")
    shader = directory / filename
    if not shader.is_file():
        raise FramePreviewError(
            f"effects catalog profile {profile!r} names a missing shader: {shader}"
        )
    return shader


def _classshader_expression(target: str) -> str:
    """The HyprWindowShade Lua call, evaluated directly by hyprctl."""
    return (
        f"hl.plugin.HyprWindowShade.classshader("
        f"{json.dumps(WINDOW_CLASS)}, {json.dumps(target)})"
    )


def _dispatch_classshader(target: str) -> None:
    """Set `target` (a shader path or "clear") on the dedicated preview class.

    `hl.plugin.HyprWindowShade.classshader` is a plain Lua function, so it is
    evaluated with `hyprctl eval` rather than routed through `hl.dispatch`,
    which would reject the void return as "expected a dispatcher" and report a
    spurious failure. A nil expression (plugin not loaded) or any other Lua
    error surfaces as a checked hyprctl failure.
    """
    _hyprctl_checked(
        f"HyprWindowShade could not set {target!r} on class {WINDOW_CLASS}",
        "eval",
        _classshader_expression(target),
    )


def _clear_companion() -> bool:
    """Best-effort removal of any class shader on the dedicated preview class.

    Returns whether the clear actually reached the compositor, so callers can
    report the true state instead of assuming it succeeded.
    """
    try:
        _dispatch_classshader(EFFECTS_CLEAR)
    except FramePreviewError:
        return False
    return True


def _companion_view(stored) -> dict:
    """Normalised, JSON-safe companion status for a record or a fresh apply."""
    if isinstance(stored, dict) and stored.get("profile"):
        return {
            "requested": True,
            "applied": bool(stored.get("shader")),
            "profile": stored.get("profile"),
            "shader": stored.get("shader"),
        }
    return {"requested": False, "applied": False, "profile": None, "shader": None}


# ----------------------------------------------------------------- public API


def cached(variant: dict, data: Path | None = None) -> dict | None:
    """The live preview record for this exact pack, or None.

    Cheap in the steady state: no pack bytes are read, only /proc and one
    `hyprctl clients` call. A structurally invalid record or stale ownership
    (dead/reused process, unmapped window) is removed here — including its
    companion shader — so the next snapshot cannot resurrect it. A record for
    a different variant/pack is left intact: the active preview window belongs
    to another snapshot, not to nobody.
    """
    del data
    try:
        variant_id, _motion, pack = _variant_pack(variant)
        record = _read_record()
        if record is None:
            return None
        pid = record.get("pid")
        start_time = record.get("pid_start_time")
        address = record.get("address")
        if not isinstance(pid, int) or not isinstance(start_time, str) \
                or not isinstance(address, str) or not address.startswith("0x"):
            _discard_stale(record)
            return None
        if record.get("variant_id") != variant_id or record.get("pack_path") != str(pack):
            return None
        if not _process_alive(pid, start_time) or _owned_window(pid, address) is None:
            _discard_stale(record)
            return None
        return {
            "kind": "live",
            "source": "kitty-live",
            "variant_id": variant_id,
            "pack_path": str(pack),
            "address": address,
            "window_class": WINDOW_CLASS,
            "window_title": WINDOW_TITLE,
            "pid": pid,
            "companion": _companion_view(record.get("companion")),
        }
    except (OSError, ValueError, FramePreviewError):
        return None


def render(variant: dict, cancel: Callable[[], bool] | None = None,
           data: Path | None = None) -> dict:
    """Open (or replace) the dedicated live preview window for this pack.

    An optional companion.json is resolved against the trusted effects catalog
    before anything is replaced, so a missing catalog/profile fails without
    destroying the preview it would have replaced. The return value carries
    the frame record and the companion application status.

    Raises FramePreviewError with an actionable message on any failure; on
    failure only the Kitty this call launched is reclaimed. A cancel callable
    turns true -> process.Cancelled, again reclaiming only the new window.
    """
    del data
    return _render_locked(variant, cancel)


def _cli_variant(pack: Path) -> dict:
    """Deterministic variant identity for a pack opened straight from disk.

    The id is the pack path digest, so repeated previews of the same authored
    pack match their own cached record without being registered in studio
    state.
    """
    resolved = pack.resolve()
    if not resolved.is_dir():
        raise FramePreviewError(f"pack directory not found: {pack}")
    identity = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:32]
    return {"id": identity, "motion": "static", "pack_path": str(resolved)}


def close(data: Path | None = None) -> dict:
    """Close the owned live preview window and clear its companion.

    Idempotent: without an owned record it reports `closed: false`. The frame
    clear tolerates an already-closed window. Failed companion cleanup retains
    the ownership record for retry and reports an unknown applied state.
    """
    del data
    record = _read_record()
    if record is None:
        return {"closed": False, "reason": "no owned live preview"}
    pid = record.get("pid")
    start_time = record.get("pid_start_time")
    address = record.get("address")
    pack_path = record.get("pack_path")
    if isinstance(address, str) and address.startswith("0x") \
            and isinstance(pack_path, str) and pack_path:
        _clear_preview(address, Path(pack_path))
    stored_companion = record.get("companion")
    cleared = _clear_companion() if isinstance(stored_companion, dict) else False
    if isinstance(pid, int) and isinstance(start_time, str):
        _reclaim_pid(pid, start_time)
    if not isinstance(stored_companion, dict) or cleared:
        _drop_record()
    if isinstance(stored_companion, dict) and stored_companion.get("profile"):
        companion_status = {
            "requested": True,
            "applied": False if cleared else None,
            "cleared": cleared,
            "profile": stored_companion.get("profile"),
            "shader": stored_companion.get("shader"),
        }
    else:
        companion_status = _companion_view(None)
    return {
        "closed": True,
        "variant_id": record.get("variant_id"),
        "pack_path": pack_path if isinstance(pack_path, str) else None,
        "address": address if isinstance(address, str) else None,
        "companion": companion_status,
    }


def _render_locked(variant: dict, cancel) -> dict:
    variant_id, _motion, pack = _variant_pack(variant)
    _require_live_session()
    _which("kitty")
    _which("hyprctl")
    _plugin_path()  # actionable error early if the plugin is missing

    # Resolve the companion and fingerprint the immutable pack before any
    # mutation: a pack that asks for a profile the local catalog cannot supply
    # must fail without destroying the preview it would have replaced.
    profile = _read_pack_companion(pack)
    shader = resolve_companion_shader(profile) if profile is not None else None
    fingerprint = _pack_fingerprint(pack)

    pid: int | None = None
    start_time: str | None = None
    companion_applied = False
    try:
        previous = _read_record()
        previous_companion = isinstance(previous, dict) \
            and isinstance(previous.get("companion"), dict)
        # Clear before dropping ownership or replacing the window. A failed
        # clear must not let a plain preview inherit the previous shader.
        if shader is not None or previous_companion:
            _dispatch_classshader(EFFECTS_CLEAR)
        if previous is not None:
            previous_pid = previous.get("pid")
            previous_start = previous.get("pid_start_time")
            previous_address = previous.get("address")
            previous_pack = previous.get("pack_path")
            if isinstance(previous_pid, int) and isinstance(previous_start, str):
                _reclaim_pid(previous_pid, previous_start)
            if isinstance(previous_address, str) and previous_address.startswith("0x") \
                    and isinstance(previous_pack, str) and previous_pack:
                _clear_preview(previous_address, Path(previous_pack))
            _drop_record()

        if shader is not None:
            _dispatch_classshader(str(shader))
            companion_applied = True

        pid = _launch_kitty(pack)
        start_time = _pid_start_time(pid)
        if start_time is None:
            # Without the original start time we could never safely reclaim
            # this PID again, so the window is unusable by design.
            raise FramePreviewError(
                "the preview kitty vanished immediately after spawn; "
                "check that the pack's kitty.conf is valid"
            )
        address = _await_address(pid, cancel)
        _apply_super_a(address)
        _hyprctl_checked(
            "kitty-skins could not apply the preview frame",
            "eval",
            _preview_lua("preview", address, pack),
        )
        _write_record({
            "version": 1,
            "pid": pid,
            "pid_start_time": start_time,
            "address": address,
            "window_class": WINDOW_CLASS,
            "window_title": WINDOW_TITLE,
            "variant_id": variant_id,
            "pack_path": str(pack),
            "fingerprint": fingerprint,
            "launched_at": storage.now(),
            "companion": (
                {"profile": profile, "shader": str(shader)}
                if shader is not None else None
            ),
        })
    except BaseException:
        if pid is not None:
            _reclaim_pid(pid, start_time or "")
        if companion_applied:
            _clear_companion()
        raise
    return {
        "kind": "live",
        "source": "kitty-live",
        "variant_id": variant_id,
        "pack_path": str(pack),
        "address": address,
        "window_class": WINDOW_CLASS,
        "window_title": WINDOW_TITLE,
        "pid": pid,
        "companion": _companion_view(
            {"profile": profile, "shader": str(shader)} if shader is not None else None
        ),
    }


def _launch_kitty(pack: Path) -> int:
    """Start the dedicated preview Kitty, detached; return its PID."""
    kitty = _which("kitty")
    argv = [
        kitty,
        "--class", WINDOW_CLASS,
        "--title", WINDOW_TITLE,
        "--config", str(pack / "kitty.conf"),
        "--override", "linux_display_server=wayland",
        "--hold",
        "-e", "sh", "-c", f"cat <<'EOF'\n{_SAMPLE}\nEOF\nsleep infinity",
    ]
    try:
        return os.posix_spawn(kitty, argv, os.environ, setsid=True)
    except OSError as exc:
        raise FramePreviewError(f"cannot start the preview kitty: {exc}") from exc


def _await_address(pid: int, cancel) -> str:
    """Bounded, cancellable wait for the mapped client's exact 0x address."""
    deadline = time.monotonic() + MAP_WAIT_S
    while time.monotonic() < deadline:
        if cancel is not None and cancel():
            raise process.Cancelled("preview opening cancelled")
        client = _owned_window(pid)
        if client is not None:
            address = str(client.get("address") or "")
            if address.startswith("0x"):
                return address
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            raise FramePreviewError(
                "the preview kitty exited before its window mapped; "
                "check that the pack's kitty.conf is valid"
            ) from None
        time.sleep(POLL_S)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        raise FramePreviewError(
            "the preview kitty exited before its window mapped; "
            "check that the pack's kitty.conf is valid"
        ) from None
    raise FramePreviewError(
        f"the preview kitty window did not map within {MAP_WAIT_S:.0f}s; "
        "close other windows or retry"
    )


def _apply_super_a(address: str) -> None:
    """Float, resize to 1280x720 and center without stealing focus.

    Hyprland 0.55+ dispatchers accept an explicit window object. Resolve the
    exact preview address once and apply every operation to that object; the
    user's active window never changes.
    """
    lua = (
        f'local window = hl.get_window("address:{address}"); '
        'if window == nil then error("preview window disappeared") end; '
        'hl.dispatch(hl.dsp.window.float({ action = "set", window = window })); '
        f'hl.dispatch(hl.dsp.window.resize({{ x = {FLOAT_W}, y = {FLOAT_H}, '
        'relative = false, window = window })); '
        'hl.dispatch(hl.dsp.window.center({ window = window }))'
    )
    _hyprctl_checked(
        "Hyprland could not float, resize and center the preview window",
        "eval",
        lua,
    )


def _emit(value) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError):
        pass
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _cli_usage() -> str:
    return (
        "usage: frame_preview.py --pack PATH\n"
        "       frame_preview.py --close\n"
        "  --pack PATH  open a live preview for an authored pack without "
        "registering it in studio state\n"
        "  --close      close the owned live preview and clear its companion"
    )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        pack_arg: str | None = None
        close_flag = False
        index = 0
        while index < len(arguments):
            argument = arguments[index]
            if argument == "--close":
                close_flag = True
                index += 1
                continue
            name, separator, inline = argument.partition("=")
            if name == "--pack":
                if separator:
                    value = inline
                else:
                    index += 1
                    if index >= len(arguments):
                        raise FramePreviewError(f"--pack needs a value\n{_cli_usage()}")
                    value = arguments[index]
                if not value:
                    raise FramePreviewError(f"--pack needs a value\n{_cli_usage()}")
                pack_arg = value
                index += 1
                continue
            raise FramePreviewError(f"unknown argument {argument!r}\n{_cli_usage()}")
        if close_flag and pack_arg is not None:
            raise FramePreviewError("--pack and --close are mutually exclusive")
        if close_flag:
            _emit(close())
            return 0
        if pack_arg is None:
            raise FramePreviewError(_cli_usage())
        _emit(render(_cli_variant(Path(pack_arg))))
        return 0
    except FramePreviewError as error:
        _emit({"error": storage.clean_text(str(error), storage.MAX_ERROR_CHARS)})
        return 1
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        _emit({"error": storage.clean_text(f"{type(error).__name__}: {error}",
                                           storage.MAX_ERROR_CHARS)})
        return 1
    except Exception as error:  # never leak a traceback instead of JSON
        _emit({"error": storage.clean_text(f"internal error: {type(error).__name__}: {error}",
                                           storage.MAX_ERROR_CHARS)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
