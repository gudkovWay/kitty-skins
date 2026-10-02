#!/usr/bin/env python3
"""Wallpaper inventory and provider-discovery helpers for the wallpaper-context skill.

Read-only with respect to the owner's machine: this module never writes outside
its own data directory, never launches a wallpaper renderer, never changes
Noctalia settings and never mutates a library. Noctalia IPC use is limited to
the read-only `wallpaper-get` command; provider state is the persisted Studio
playback.json, never the disabled W Engine provider. Nothing here invokes a
model or captures the desktop.

Public API (stable for the skill and for analyze-wallpapers.py):

    data_root() -> Path              inventory root       ($XDG_DATA_HOME/wallpaper-context)
    cache_root() -> Path             analysis cache root  ($XDG_CACHE_HOME/wallpaper-context)
    utc_now() -> str                 UTC ISO-8601, trailing "Z"
    atomic_json(path, value) -> None atomic, advisory-locked JSON write
    advisory_lock(target)            context manager: flock on <dir>/.<name>.lock
    emit_json(value) -> None         JSON to stdout (CLI helper, never partial output)
    load_index(base) -> dict         canonical empty when absent, ValueError when malformed
    scan(roots, base) -> dict        incremental inventory; empty roots -> discovery
    current_content_id(item) -> str  rehash one item's source, no fingerprint reuse
    discover(base) -> dict           appearance context + library_roots, read-only

Records follow references/metadata-contract.md:

    <base>/index.json          inventory    (contract "Inventory")
    <base>/fingerprints.json   internal stat fingerprints (device, inode, size,
                               timestamps); lets an unchanged source reuse its
                               content id without re-reading bytes

CLI:

    catalog.py [--data-dir PATH] scan [--root PATH ...]   writes index/fingerprints
    catalog.py [--data-dir PATH] list                     prints the saved index

Errors are reported as JSON on stdout ({"error": {...}}, exit status 2) or in the
record's "errors" list; they are never silently swallowed.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tomllib
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
INDEX_FILE = "index.json"
FINGERPRINTS_FILE = "fingerprints.json"

IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp", ".tif", ".tiff", ".jxl",
}
VIDEO_EXTENSIONS = {
    ".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".wmv", ".flv", ".mpg", ".mpeg", ".ogv",
}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

PROJECT_MARKER = "project.json"
WE_PROVIDER = "wallpaper-engine"
LOOSE_PROVIDER = "filesystem"
STUDIO_PROVIDER = "q/wallpaper-studio"
STUDIO_DATA_DIR_ENV = "WALLPAPER_STUDIO_DATA_DIR"
PLAYBACK_FILE = "playback.json"
STEAM_APPID = "431960"  # Wallpaper Engine on Steam

# Wallpaper Engine project.json "type" -> inventory "kind".
PROJECT_TYPE_MAP = {
    "scene": "scene",
    "web": "web",
    "video": "video",
    "image": "image",
    "application": "unknown",
}

MAX_SCAN_DEPTH = 8
MAX_PROJECT_DEPTH = 12
MAX_TITLE_CHARS = 512
HASH_CHUNK = 1 << 20
IPC_TIMEOUT_S = 5.0
VDF_MAX_BYTES = 1 << 20

HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
CONTENT_ID_RE = re.compile(r"^[0-9a-f]{64}$")
ITEM_KINDS = frozenset({"image", "video", "scene", "web", "unknown"})

WARN_UNKNOWN_PROVIDER = "no wallpaper-studio playback state found; library roots come from defaults only"


# --------------------------------------------------------------------------- paths


def _xdg_dir(env_var: str, default_rel: str) -> Path:
    """XDG base directory: absolute env value wins, otherwise the $HOME default."""
    raw = os.environ.get(env_var, "").strip()
    if raw and os.path.isabs(raw):
        return Path(raw)
    return Path.home() / default_rel


def xdg_config_home() -> Path:
    return _xdg_dir("XDG_CONFIG_HOME", ".config")


def xdg_state_home() -> Path:
    return _xdg_dir("XDG_STATE_HOME", ".local/state")


def xdg_cache_home() -> Path:
    return _xdg_dir("XDG_CACHE_HOME", ".cache")


def data_root() -> Path:
    """Inventory root: <XDG_DATA_HOME>/wallpaper-context."""
    return _xdg_dir("XDG_DATA_HOME", ".local/share") / "wallpaper-context"


def cache_root() -> Path:
    """Analysis cache root: <XDG_CACHE_HOME>/wallpaper-context."""
    return xdg_cache_home() / "wallpaper-context"


def noctalia_state_home() -> Path:
    """Noctalia state home ($NOCTALIA_STATE_HOME wins, then XDG state home)."""
    raw = os.environ.get("NOCTALIA_STATE_HOME", "").strip()
    if raw and os.path.isabs(raw):
        return Path(raw)
    return xdg_state_home()


def utc_now() -> str:
    """Current UTC time as ISO-8601 with a trailing Z."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _abspath(value) -> str:
    return os.path.abspath(os.path.expanduser(str(value)))


def _norm_root(value) -> Path:
    """Absolute, symlink-resolved, trailing-slash-free root path."""
    return Path(os.path.realpath(_abspath(value)))


def _is_under(child, parent) -> bool:
    """Lexical containment (equality included) for already absolute paths."""
    c = _abspath(child)
    p = _abspath(parent)
    return c == p or c.startswith(p + os.sep)


# ------------------------------------------------------------------------- json io

_LOCK_GUARD = threading.Lock()
_HELD_LOCKS: set[str] = set()


@contextlib.contextmanager
def advisory_lock(target):
    """Advisory exclusive lock for writers of `target` (reentrant per process).

    The lock file lives next to the record as .<name>.lock; readers do not lock.
    """
    key = _abspath(target)
    with _LOCK_GUARD:
        if key in _HELD_LOCKS:
            yield
            return
        _HELD_LOCKS.add(key)
    lock_path = Path(key).parent / ("." + Path(key).name + ".lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        with _LOCK_GUARD:
            _HELD_LOCKS.discard(key)


def _read_json(path: Path):
    """Parse a JSON file. Raises ValueError for unreadable or malformed input."""
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    try:
        return json.loads(raw.decode("utf-8", "surrogateescape"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed JSON in {path}: {exc}") from exc


def atomic_json(path, value) -> None:
    """Write JSON atomically (same-directory temp file + rename) under a lock.

    Paths with undecodable bytes are preserved through surrogateescape so that a
    round trip never rewrites a filename.
    """
    path = Path(path)
    with advisory_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(value, ensure_ascii=False, indent=2)
        payload = payload.encode("utf-8", "surrogateescape") + b"\n"
        handle_fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(handle_fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise


def emit_json(value) -> None:
    """Print a whole JSON document to stdout, one document per invocation."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError):
        pass
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _clean_text(value, limit: int = MAX_TITLE_CHARS) -> str | None:
    """Untrusted label -> single-line, control-character-free, bounded text."""
    if not isinstance(value, str):
        return None
    cleaned = "".join(ch for ch in value if ch >= " " or ch == "\t")
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return None
    return cleaned[:limit]


# ------------------------------------------------------------------- index record


def _empty_index() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "scanned_at": None,
        "roots": [],
        "items": [],
        "errors": [],
    }


def _validate_index_item(path: Path, entry) -> None:
    """Reject an inventory item whose canonical fields the analyzer cannot trust."""
    if not isinstance(entry, dict):
        raise ValueError(f"{path}: item entries must be objects")
    label = f"{path}: item {entry.get('id')!r}"
    canonical = ("id", "title", "kind", "path", "media_path", "preview_path", "provider", "source_id")
    for key in canonical:
        if key not in entry:
            raise ValueError(f"{label}: missing canonical field {key!r}")
    item_id = entry["id"]
    if not isinstance(item_id, str) or not CONTENT_ID_RE.match(item_id):
        raise ValueError(f"{label}: 'id' must be a lowercase 64-digit SHA-256")
    kind = entry["kind"]
    if not isinstance(kind, str) or kind not in ITEM_KINDS:
        raise ValueError(f"{label}: unsupported 'kind' {kind!r}")
    for key in ("title", "provider"):
        if not isinstance(entry[key], str):
            raise ValueError(f"{label}: {key!r} must be a string")
    if not isinstance(entry["path"], str) or not os.path.isabs(entry["path"]):
        raise ValueError(f"{label}: 'path' must be an absolute path string")
    for key in ("media_path", "preview_path"):
        value = entry[key]
        if value is None:
            continue
        if not isinstance(value, str) or not os.path.isabs(value):
            raise ValueError(f"{label}: {key!r} must be an absolute path string or null")
    if entry["source_id"] is not None and not isinstance(entry["source_id"], str):
        raise ValueError(f"{label}: 'source_id' must be a string or null")


def load_index(base) -> dict:
    """Read <base>/index.json.

    A missing file yields the canonical empty inventory. A malformed file raises
    ValueError: the caller reports it instead of overwriting real records.

    Validation covers the canonical record fields (id, title, kind, path,
    media_path, preview_path, provider, source_id), not only presence: the
    analyzer consumes these records, so an id that is not a lowercase SHA-256,
    an unknown kind or a non-absolute path is an error rather than a guess.
    """
    path = Path(base) / INDEX_FILE
    if not path.exists():
        return _empty_index()
    value = _read_json(path)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: top level is not a JSON object")
    version = value.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool) or version != SCHEMA_VERSION:
        raise ValueError(f"{path}: unsupported schema_version {version!r}")
    for key in ("roots", "items", "errors"):
        if not isinstance(value.get(key), list):
            raise ValueError(f"{path}: {key!r} is not a list")
    for root in value["roots"]:
        if not isinstance(root, str) or not os.path.isabs(root):
            raise ValueError(f"{path}: 'roots' entries must be absolute path strings")
    for entry in value["items"]:
        _validate_index_item(path, entry)
    for entry in value["errors"]:
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: error entries must be objects")
        if not isinstance(entry.get("path"), str) or not isinstance(entry.get("message"), str):
            raise ValueError(f"{path}: error entries need string 'path' and 'message'")
    return value


def _fingerprints_path(base) -> Path:
    return Path(base) / FINGERPRINTS_FILE


def _load_fingerprints(base, errors: list) -> dict:
    """Read the internal stat-fingerprint cache. A damaged file only costs a rehash."""
    path = _fingerprints_path(base)
    if not path.exists():
        return {}
    try:
        value = _read_json(path)
    except ValueError as exc:
        errors.append({"path": str(path), "message": f"{exc}; content hashes will be recomputed"})
        return {}
    entries = value.get("entries") if isinstance(value, dict) else None
    if not isinstance(entries, dict):
        errors.append(
            {"path": str(path), "message": "fingerprints.json has no entries object; recomputing"}
        )
        return {}
    clean: dict = {}
    for item_path, entry in entries.items():
        if not isinstance(item_path, str) or not isinstance(entry, dict):
            continue
        fingerprint = entry.get("fingerprint")
        content_id = entry.get("id")
        if (
            isinstance(fingerprint, str)
            and len(fingerprint) == 64
            and isinstance(content_id, str)
            and len(content_id) == 64
        ):
            clean[item_path] = {
                "id": content_id,
                "fingerprint": fingerprint,
                "kind": entry.get("kind") if isinstance(entry.get("kind"), str) else None,
                "source_id": entry.get("source_id")
                if isinstance(entry.get("source_id"), str)
                else None,
                "updated_at": entry.get("updated_at")
                if isinstance(entry.get("updated_at"), str)
                else None,
            }
    return clean


# ------------------------------------------------------------------ provider state


def _load_toml(path: Path, errors: list, label: str, required: bool) -> dict:
    if not path.is_file():
        if required:
            errors.append({"path": str(path), "message": f"{label} not found"})
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8", errors="surrogateescape"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        errors.append({"path": str(path), "message": f"{label} unreadable: {exc}"})
        return {}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursive merge; `override` (effective state) wins over `base` (static config)."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _effective_settings(errors: list) -> dict:
    config = _load_toml(xdg_config_home() / "noctalia" / "config.toml", errors, "noctalia config.toml", False)
    state = _load_toml(noctalia_state_home() / "noctalia" / "settings.toml", errors, "noctalia settings.toml", False)
    return _deep_merge(config, state)


def _ipc(command: list, errors: list, label: str, timeout: float = IPC_TIMEOUT_S) -> str | None:
    """Run a read-only `noctalia msg` command. Never a setter, never a shell."""
    executable = shutil.which("noctalia")
    if not executable:
        errors.append({"path": "noctalia", "message": f"{label}: noctalia CLI not found"})
        return None
    argv = [executable, "msg", *command]
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append(
            {"path": " ".join(argv), "message": f"{label}: {type(exc).__name__}: {exc}"}
        )
        return None
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        errors.append(
            {
                "path": " ".join(argv),
                "message": f"{label}: exit {completed.returncode}"
                + (f": {detail[0][:200]}" if detail else ""),
            }
        )
        return None
    return completed.stdout.strip()


def _playback_state_path(state_home: Path) -> Path:
    """Studio playback state file (NOCTALIA_STATE_HOME already applied)."""
    return state_home / "noctalia" / "plugins" / "data" / STUDIO_PROVIDER / PLAYBACK_FILE


def _provider_state(errors: list, query_ipc: bool = True) -> dict:
    """Read-only provider discovery from the Studio playback.json contract.

    The old W Engine provider is never consulted, not even as a fallback: while
    it is disabled its data.json keeps describing stale selections that must
    not surface as active wallpapers. `query_ipc` is accepted for caller
    compatibility and intentionally ignored: reading the persisted playback
    state needs no IPC. `enabled` is the state's own flag; `data` is the whole
    document, whose `current` maps output -> renderer record and whose
    `library_roots` feeds catalog root discovery.
    """
    state_home = noctalia_state_home()
    data_path = _playback_state_path(state_home)
    info = {
        "provider": STUDIO_PROVIDER,
        "plugin": STUDIO_PROVIDER,
        "version": None,
        "enabled": None,
        "state_home": str(state_home),
        "source": None,
        "data_path": str(data_path),
        "data": None,
        "ipc": "unavailable",
    }
    if not data_path.is_file():
        info["provider"] = "unknown"
        errors.append({"path": str(data_path), "message": WARN_UNKNOWN_PROVIDER})
        return info
    info["source"] = "playback.json"
    try:
        value = _read_json(data_path)
        info["data"] = value if isinstance(value, dict) else None
        if info["data"] is None:
            errors.append({"path": str(data_path), "message": "playback state is not a JSON object"})
            return info
    except ValueError as exc:
        errors.append({"path": str(data_path), "message": str(exc)})
        return info
    info["enabled"] = info["data"].get("enabled") is True
    return info


# ------------------------------------------------------------------ Steam library


def _steam_roots() -> list[Path]:
    home = Path.home()
    return [
        home / ".steam" / "steam",
        home / ".steam" / "root",
        home / ".local" / "share" / "Steam",
        home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
        home / "snap" / "steam" / "common" / ".local" / "share" / "Steam",
        home / "snap" / "steam" / "common" / ".local" / "share" / "steam",
    ]


def _steam_apps_dir(steam_root: Path) -> Path:
    return steam_root if steam_root.name == "steamapps" else steam_root / "steamapps"


def _workshop_content_dir(steam_root: Path) -> Path:
    return _steam_apps_dir(steam_root) / "workshop" / "content" / STEAM_APPID


def _vdf_tokens(text: str):
    """Tokenize a Valve keyvalue file: quoted strings, bare words, braces."""
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in " \t\r\n":
            index += 1
            continue
        if char == "/" and text[index : index + 2] == "//":
            end = text.find("\n", index)
            index = length if end < 0 else end + 1
            continue
        if char == "{":
            yield "{"
            index += 1
            continue
        if char == "}":
            yield "}"
            index += 1
            continue
        if char == '"':
            buffer: list[str] = []
            index += 1
            while index < length:
                if text[index] == "\\" and index + 1 < length:
                    buffer.append(text[index + 1])
                    index += 2
                    continue
                if text[index] == '"':
                    break
                buffer.append(text[index])
                index += 1
            yield ("str", "".join(buffer))
            index += 1
            continue
        end = index
        while end < length and text[end] not in ' \t\r\n{}"':
            end += 1
        yield ("str", text[index:end])
        index = end


def _parse_vdf(text: str, max_depth: int = 32) -> dict:
    """Bounded keyvalue parser (no evaluation, no recursion on user input)."""
    tokens = list(_vdf_tokens(text))

    def parse_object(position: int, depth: int) -> tuple[dict, int]:
        result: dict = {}
        while position < len(tokens):
            token = tokens[position]
            if token == "}":
                return result, position + 1
            if token == "{":
                position += 1
                continue
            if depth > max_depth:
                return result, len(tokens)
            key = token[1]
            position += 1
            if position < len(tokens) and tokens[position] == "{":
                value, position = parse_object(position + 1, depth + 1)
                result[key] = value
            elif position < len(tokens) and isinstance(tokens[position], tuple):
                result[key] = tokens[position][1]
                position += 1
            else:
                result[key] = None
        return result, position

    root, _position = parse_object(0, 0)
    return root


def _vdf_library_paths(vdf: dict) -> list[Path]:
    """Extract additional Steam library paths from libraryfolders.vdf (both formats)."""
    folders = vdf.get("libraryfolders") if isinstance(vdf, dict) else None
    if not isinstance(folders, dict):
        return []
    out: list[Path] = []
    for key, value in folders.items():
        if isinstance(value, dict) and isinstance(value.get("path"), str):
            out.append(Path(value["path"]))
        elif isinstance(value, str) and str(key).isdigit():
            out.append(Path(value))
    return out


def _steam_library_roots(errors: list) -> list[Path]:
    """Existing workshop content directories from default installs and VDF libraries."""
    roots: list[Path] = []
    for steam_root in _steam_roots():
        apps = _steam_apps_dir(steam_root)
        vdf_path = apps / "libraryfolders.vdf"
        if vdf_path.is_file():
            try:
                raw = vdf_path.read_bytes()[:VDF_MAX_BYTES]
                vdf = _parse_vdf(raw.decode("utf-8", "surrogateescape"))
            except OSError as exc:
                errors.append({"path": str(vdf_path), "message": f"libraryfolders.vdf unreadable: {exc}"})
                vdf = {}
            for library in _vdf_library_paths(vdf):
                candidate = _workshop_content_dir(library)
                if candidate.is_dir():
                    roots.append(_norm_root(candidate))
        candidate_root = _workshop_content_dir(steam_root)
        if candidate_root.is_dir():
            roots.append(_norm_root(candidate_root))
    return roots


def _imports_root() -> Path:
    """Managed imports root; mirrors the Studio backend storage.data_root().

    Same env override and same XDG default as the backend: explicit
    WALLPAPER_STUDIO_DATA_DIR wins, otherwise <XDG_DATA_HOME>/wallpaper-context.
    """
    raw = os.environ.get(STUDIO_DATA_DIR_ENV, "").strip()
    if raw:
        return Path(os.path.abspath(os.path.expanduser(raw))) / "imports"
    return data_root() / "imports"


def _library_roots(provider: dict, errors: list) -> tuple[list[Path], list[str]]:
    """Catalog roots: Studio-declared library_roots first, then the managed
    imports root, then Steam workshop dirs (standard discovery preserved)."""
    roots: list[Path] = []
    missing: list[str] = []
    data = provider.get("data") if isinstance(provider.get("data"), dict) else {}
    declared = data.get("library_roots") if isinstance(data.get("library_roots"), list) else []
    for value in declared:
        if not isinstance(value, str) or not value.strip():
            errors.append(
                {"path": "library_roots", "message": f"ignored non-string root entry {value!r}"}
            )
            continue
        root = _norm_root(value)
        if root.is_dir():
            roots.append(root)
        else:
            missing.append(str(root))
            errors.append(
                {"path": str(root), "message": "declared Studio library root is not a directory"}
            )
    imports = _imports_root()
    if imports.is_dir():
        roots.append(_norm_root(imports))
    roots.extend(_steam_library_roots(errors))
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        resolved = _norm_root(root)
        if resolved.is_dir() and str(resolved) not in seen:
            seen.add(str(resolved))
            unique.append(resolved)
    return unique, missing


def _discovery_roots(base, errors: list, saved_roots: list) -> list[Path]:
    """Roots used by `scan` when no --root was given (cheap subset of discover()).

    Discovered libraries plus the roots already saved in the index, so a repeated
    no-root scan still refreshes a manually added generic library instead of
    silently forgetting about it.
    """
    provider = _provider_state(errors, query_ipc=False)
    discovered, _missing = _library_roots(provider, errors)
    requested: list[Path] = list(discovered)
    seen = {str(_norm_root(root)) for root in requested}
    for saved in saved_roots:
        if not isinstance(saved, str) or not saved.strip():
            continue
        try:
            candidate = _norm_root(saved)
        except (OSError, ValueError) as exc:
            errors.append({"path": saved, "message": f"saved root is unusable: {exc}"})
            continue
        if str(candidate) in seen:
            continue
        seen.add(str(candidate))
        requested.append(candidate)
    if not requested:
        errors.append(
            {
                "path": str(base),
                "message": "no library roots discovered or saved; pass --root PATH to scan a directory",
            }
        )
    return requested


# -------------------------------------------------------------------------- palette


def _read_text(path: Path, errors: list, label: str) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError as exc:
        errors.append({"path": str(path), "message": f"{label} unreadable: {exc}"})
        return None


def _parse_kitty_conf(text: str) -> dict:
    """Generated kitty theme: keep observed role names whose value is a #RRGGBB color."""
    roles: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        if HEX_COLOR_RE.match(parts[1]):
            roles[parts[0]] = parts[1].lower()
    return roles


def _parse_starship_palette(text: str) -> dict:
    """Generated starship palette: [palettes.noctalia] names as written by Noctalia."""
    try:
        value = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}
    section = value.get("palettes")
    if not isinstance(section, dict):
        return {}
    catalog = section.get("noctalia")
    if not isinstance(catalog, dict):
        return {}
    roles: dict = {}
    for key, entry in catalog.items():
        if isinstance(entry, str) and HEX_COLOR_RE.match(entry):
            roles[key] = entry.lower()
    return roles


def _generated_palette(errors: list) -> tuple[dict, list]:
    """Roles from the generated kitty theme and starship palette, plus provenance."""
    roles: dict = {}
    sources: list = []
    for path, kind, parser in (
        (xdg_config_home() / "kitty" / "themes" / "noctalia.conf", "kitty", _parse_kitty_conf),
        (xdg_cache_home() / "noctalia" / "starship-palette.toml", "starship", _parse_starship_palette),
    ):
        if not path.is_file():
            errors.append({"path": str(path), "message": f"generated {kind} palette not found"})
            continue
        text = _read_text(path, errors, f"generated {kind} palette")
        if text is None:
            continue
        parsed = parser(text)
        if not parsed:
            errors.append({"path": str(path), "message": f"generated {kind} palette has no colors"})
            continue
        collisions = sorted(key for key in parsed if key in roles and roles[key] != parsed[key])
        for key, value in parsed.items():
            roles.setdefault(key, value)
        for key in collisions:
            errors.append(
                {
                    "path": str(path),
                    "message": f"role {key!r} also defined by an earlier palette source; kept that value",
                }
            )
        with contextlib.suppress(OSError):
            sources.append(
                {
                    "file": str(path),
                    "kind": kind,
                    "roles": len(parsed),
                    "mtime": datetime.fromtimestamp(
                        path.stat().st_mtime, timezone.utc
                    ).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                }
            )
    return roles, sources


def _color_scheme(errors: list, settings: dict) -> tuple[str | None, str | None, str | None]:
    """(origin, scheme name, mode) preferring read-only IPC over static config."""
    origin = None
    scheme = None
    listing = _ipc(["color-scheme-get"], errors, "color-scheme-get")
    if listing:
        parts = listing.split(None, 1)
        origin = parts[0]
        scheme = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
    theme = settings.get("theme") if isinstance(settings.get("theme"), dict) else {}
    if scheme is None:
        value = theme.get("wallpaper_scheme")
        if isinstance(value, str) and value.strip():
            scheme = value.strip()
            origin = origin or (theme.get("source") if isinstance(theme.get("source"), str) else None)
    mode = _ipc(["theme-mode-get"], errors, "theme-mode-get")
    if not mode:
        value = theme.get("mode")
        mode = value.strip() if isinstance(value, str) and value.strip() else None
    return origin, scheme, mode


# ------------------------------------------------------------------- wallpapers


def _proc_start_time(pid_dir: Path) -> str | None:
    """Field 22 of /proc/<pid>/stat as a string, or None when unreadable.

    The comm field may contain spaces and parentheses, so the remainder after
    the last ')' is split; starttime is the 20th field of that remainder.
    """
    try:
        text = (pid_dir / "stat").read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        return None
    tail = text.rpartition(")")[2].split()
    return tail[19] if len(tail) > 19 else None


def _proc_session_env(pid: int) -> dict:
    """WAYLAND_DISPLAY and HYPRLAND_INSTANCE_SIGNATURE of a live process.

    Read from /proc/<pid>/environ, never from assumptions: a record copied from
    another machine or a sandbox must not pass as this compositor's renderer.
    Missing keys are absent from the result; the caller fails closed on them.
    """
    wanted = ("WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE")
    try:
        raw = (Path("/proc") / str(pid) / "environ").read_bytes()
    except OSError:
        return {}
    env = {}
    for part in raw.split(b"\0"):
        key, sep, value = part.partition(b"=")
        try:
            name = key.decode("utf-8", "surrogateescape")
        except ValueError:
            continue
        if name in wanted:
            env[name] = value.decode("utf-8", "surrogateescape")
    return env


def _proc_running_wallpapers(errors: list) -> dict:
    """Observed linux-wallpaperengine processes, read-only: pid -> record.

    Each record carries the kernel start time plus the --screen-root output
    and --bg background from argv, so a playback record can be corroborated by
    pid AND start time — a recycled pid alone proves nothing.
    """
    running: dict = {}
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return running
    try:
        pid_dirs = sorted(entry for entry in proc_root.iterdir() if entry.name.isdigit())
    except OSError as exc:
        errors.append({"path": "/proc", "message": f"cannot list /proc: {exc}"})
        return running
    for pid_dir in pid_dirs:
        try:
            raw = (pid_dir / "cmdline").read_bytes()
        except OSError:
            continue
        argv = [part.decode("utf-8", "surrogateescape") for part in raw.split(b"\0") if part]
        if not argv:
            continue
        if "linux-wallpaperengine" not in os.path.basename(argv[0]):
            continue
        background = None
        output = None
        for index, arg in enumerate(argv):
            if arg == "--bg" and index + 1 < len(argv):
                background = argv[index + 1]
            elif arg.startswith("--bg="):
                background = arg[5:]
            elif arg == "--screen-root" and index + 1 < len(argv):
                output = argv[index + 1]
        if not background:
            continue
        running[int(pid_dir.name)] = {
            "start_time": _proc_start_time(pid_dir),
            "output": output,
            "background": background,
        }
    return running


_WORKSHOP_PATH_RE = re.compile(r"/workshop/content/" + STEAM_APPID + r"/([^/]+)/")


def _workshop_id_from_path(value) -> str | None:
    if not isinstance(value, str):
        return None
    match = _WORKSHOP_PATH_RE.search(value)
    return match.group(1) if match else None


def _active_wallpapers(base: Path, provider: dict, errors: list) -> list:
    """Bind each output's playback record to inventory content, /proc-corroborated.

    Active requires the provider state to be enabled and each record to survive
    every corroboration gate: status "playing", a positive pid, a recorded
    nonempty kernel start time matching the live process, a process whose
    WAYLAND_DISPLAY/HYPRLAND_INSTANCE_SIGNATURE match this compositor session,
    and a content identity that agrees with the current inventory item — an
    exact canonical path match first, a unique source_id match only as
    fallback. Any failed gate leaves the entry stale/unknown with content
    identity unset: stale or copied state never relabels the running bytes.
    """
    if provider.get("enabled") is not True:
        errors.append(
            {
                "path": provider.get("data_path") or PLAYBACK_FILE,
                "message": "playback state is not enabled; no provider content is active",
            }
        )
        return []
    data = provider.get("data") if isinstance(provider.get("data"), dict) else {}
    declared = data.get("current") if isinstance(data.get("current"), dict) else {}
    running = _proc_running_wallpapers(errors)

    try:
        inventory = load_index(base)
    except ValueError as exc:
        errors.append({"path": str(Path(base) / INDEX_FILE), "message": str(exc)})
        inventory = _empty_index()

    content_by_path_items: dict = {}
    ids_by_source: dict = {}
    items_by_content: dict = {}
    for item in inventory.get("items", []):
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        content_id = item.get("id")
        if isinstance(path, str) and isinstance(content_id, str):
            content_by_path_items[_norm_root(path)] = item
            items_by_content[content_id] = item
        if item.get("provider") != WE_PROVIDER:
            continue
        source_id = item.get("source_id")
        if isinstance(source_id, str) and isinstance(content_id, str):
            ids_by_source.setdefault(source_id, set()).add(content_id)
    content_by_source: dict = {}
    ambiguous_sources: set = set()
    for source_id, ids in ids_by_source.items():
        if len(ids) == 1:
            content_by_source[source_id] = items_by_content.get(next(iter(ids)))
            continue
        # The same provider id exists in more than one root with different
        # content; binding either one would be a guess, so report the ambiguity
        # and leave content_sha256 null.
        ambiguous_sources.add(source_id)
        errors.append(
            {
                "path": str(Path(base) / INDEX_FILE),
                "message": (
                    f"source id {source_id!r} matches {len(ids)} distinct content ids "
                    f"({', '.join(sorted(ids))}); content identity left unknown"
                ),
            }
        )

    entries: list = []
    for output in sorted(key for key in declared if isinstance(key, str)):
        record = declared.get(output) if isinstance(declared.get(output), dict) else {}
        if record.get("status") != "playing":
            continue
        source_id = record.get("source_id") if isinstance(record.get("source_id"), str) else None
        record_path = record.get("path") if isinstance(record.get("path"), str) else None
        # Exact canonical path wins over any source-id reasoning: a duplicate
        # source id in the inventory must not downgrade a record that names its
        # project unambiguously. Ambiguity rejection applies only to the
        # source-id fallback; with no unique identity either way we stay closed.
        resolved_via = None
        item = None
        if record_path:
            item = content_by_path_items.get(_norm_root(record_path))
            if item is not None:
                resolved_via = "path"
        if item is None and source_id:
            if source_id in ambiguous_sources:
                errors.append(
                    {
                        "path": f"/proc#{output}",
                        "message": (
                            f"record has no exact path match and source id {source_id!r} is "
                            "ambiguous in the inventory; content identity left unknown"
                        ),
                    }
                )
            else:
                item = content_by_source.get(source_id)
                if item is not None:
                    resolved_via = "source"
        content_id = item.get("id") if item is not None and resolved_via else None
        entry = {
            "output": output,
            "provider": STUDIO_PROVIDER,
            "source_id": source_id,
            "content_sha256": content_id,
            "shell_wallpaper_path": None,
            "status": "observed",
        }

        # Content identity gate: the record must name the same bytes the
        # inventory item identifies right now. A record captured against older
        # (or copied, unverifiable) content never labels the live wallpaper.
        recorded_content = record.get("content_id")
        if item is not None:
            if not isinstance(recorded_content, str) or not recorded_content:
                entry["status"] = "stale"
                entry["content_sha256"] = None
                errors.append(
                    {
                        "path": f"/proc#{output}",
                        "message": (
                            "playback record carries no content_id; content identity "
                            "cannot be verified against the inventory"
                        ),
                    }
                )
            elif recorded_content != item.get("id"):
                entry["status"] = "stale"
                entry["content_sha256"] = None
                errors.append(
                    {
                        "path": f"/proc#{output}",
                        "message": (
                            f"record content_id {recorded_content} does not match the current "
                            f"inventory item id {item.get('id')}; the source changed since "
                            "the record was written"
                        ),
                    }
                )

        # Corroboration: the recorded renderer must still be alive with the
        # same kernel start time, same output and same background, inside this
        # compositor session. Any mismatch is stale, a missing process is
        # stale — never silently trusted.
        pid = record.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            entry["status"] = "stale"
            errors.append(
                {
                    "path": f"/proc#{output}",
                    "message": (
                        f"playback record has no positive pid ({pid!r}); it cannot be "
                        "corroborated against a live renderer"
                    ),
                }
            )
        else:
            observed = running.get(pid) if isinstance(pid, int) else None
            if observed is None:
                entry["status"] = "stale"
                errors.append(
                    {
                        "path": f"/proc#{output}",
                        "message": (
                            f"playback state says playing with pid {pid!r} but no live "
                            "linux-wallpaperengine process matches it"
                        ),
                    }
                )
            else:
                recorded_start = record.get("start_time")
                if not recorded_start:
                    entry["status"] = "stale"
                    errors.append(
                        {
                            "path": f"/proc#{output}",
                            "message": (
                                f"recorded start_time for pid {pid} is missing or empty; a "
                                "recycled pid cannot be excluded"
                            ),
                        }
                    )
                elif str(recorded_start) != observed["start_time"]:
                    entry["status"] = "stale"
                    errors.append(
                        {
                            "path": f"/proc#{output}",
                            "message": (
                                f"pid {pid} was reused: kernel start time {observed['start_time']} "
                                f"differs from the recorded {recorded_start}"
                            ),
                        }
                    )
                session_env = _proc_session_env(pid)
                for name in ("WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE"):
                    proc_value = session_env.get(name)
                    live_value = os.environ.get(name)
                    if not proc_value or not live_value or proc_value != live_value:
                        entry["status"] = "stale"
                        errors.append(
                            {
                                "path": f"/proc/{pid}/environ",
                                "message": (
                                    f"process session {name}={proc_value!r} does not match this "
                                    f"compositor session {live_value!r}; state was not written "
                                    "by this session"
                                ),
                            }
                        )
                observed_output = observed.get("output")
                if observed_output and observed_output != output:
                    entry["status"] = "stale"
                    errors.append(
                        {
                            "path": f"/proc#{pid}",
                            "message": (
                                f"process renders output {observed_output!r} while the record "
                                f"names {output!r}"
                            ),
                        }
                    )
                background = observed.get("background")
                if background:
                    if "/" in background:
                        if record_path and _norm_root(background) != _norm_root(record_path):
                            entry["status"] = "stale"
                            errors.append(
                                {
                                    "path": f"/proc#{pid}",
                                    "message": (
                                        f"process plays {background!r} while the record names "
                                        f"{record_path!r}"
                                    ),
                                }
                            )
                    elif source_id and background != source_id:
                        entry["status"] = "stale"
                        errors.append(
                            {
                                "path": f"/proc#{pid}",
                                "message": (
                                    f"process plays {background!r} while the record names "
                                    f"{source_id!r}"
                                ),
                            }
                        )
        if resolved_via == "source" and source_id in ambiguous_sources and entry["status"] != "stale":
            # More than one inventory item carries this provider id, so neither
            # content id may be presented as the live wallpaper's content.
            entry["status"] = "unknown"
            entry["content_sha256"] = None
        shell_path = _ipc(["wallpaper-get", output], errors, f"wallpaper-get {output}")
        if shell_path:
            entry["shell_wallpaper_path"] = shell_path
            shell_id = _workshop_id_from_path(shell_path)
            if source_id and shell_id and shell_id != source_id:
                entry["status"] = "stale"
                errors.append(
                    {
                        "path": shell_path,
                        "message": (
                            f"shell wallpaper belongs to item {shell_id!r} while the provider plays "
                            f"{source_id!r}; the shell still is not the live animation"
                        ),
                    }
                )
        entries.append(entry)
    return entries


def _palette_context(active: list, errors: list) -> dict:
    """Appearance palette: observed role names only, provenance recorded, never invented."""
    settings = _effective_settings(errors)
    origin, scheme, mode = _color_scheme(errors, settings)
    roles, sources = _generated_palette(errors)

    wallpaper_paths = set()
    wallpaper_cfg = settings.get("wallpaper") if isinstance(settings.get("wallpaper"), dict) else {}
    monitors = wallpaper_cfg.get("monitors") if isinstance(wallpaper_cfg.get("monitors"), dict) else {}
    for config in monitors.values():
        if isinstance(config, dict) and isinstance(config.get("path"), str) and config["path"]:
            wallpaper_paths.add(config["path"])
    last = wallpaper_cfg.get("last") if isinstance(wallpaper_cfg.get("last"), dict) else {}
    if isinstance(last.get("path"), str) and last["path"]:
        wallpaper_paths.add(last["path"])
    for entry in active:
        path = entry.get("shell_wallpaper_path")
        if path:
            wallpaper_paths.add(path)

    wallpaper_source = None
    if len(wallpaper_paths) == 1:
        wallpaper_source = next(iter(wallpaper_paths))
    elif wallpaper_paths:
        errors.append(
            {
                "path": "palette",
                "message": (
                    f"{len(wallpaper_paths)} distinct shell wallpaper paths observed; "
                    "palette provenance is ambiguous"
                ),
            }
        )

    status = "observed"
    if not roles:
        status = "unknown"
    elif not wallpaper_source and wallpaper_paths:
        status = "stale"
    if any(entry.get("status") == "stale" for entry in active):
        status = "stale"
    return {
        "source": "noctalia",
        "mode": mode,
        "scheme": scheme,
        "scheme_origin": origin,
        "roles": roles,
        "association": "current-session",
        "wallpaper_source": wallpaper_source,
        "status": status,
        "sources": sources,
    }


def discover(base) -> dict:
    """Read-only discovery: appearance context (contract) + library_roots.

    Never writes, never creates directories, never activates anything. Missing
    information is reported as null/unknown together with an "errors" entry.
    """
    base = Path(base)
    errors: list = []
    observed_at = utc_now()
    provider = _provider_state(errors)
    roots, missing = _library_roots(provider, errors)
    active = _active_wallpapers(base, provider, errors)
    palette = _palette_context(active, errors)

    status = "observed"
    if any(entry.get("status") == "stale" for entry in active) or palette["status"] == "stale":
        status = "stale"
    elif (
        not active
        or any(entry.get("status") == "unknown" for entry in active)
        or palette["status"] == "unknown"
    ):
        status = "unknown"

    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at": observed_at,
        "provider": {
            "id": provider["provider"],
            "plugin": provider["plugin"],
            "version": provider["version"],
            "enabled": provider["enabled"],
            "source": provider["source"],
            "data_path": provider["data_path"],
            "ipc": provider["ipc"],
            "state_home": provider["state_home"],
        },
        "active": active,
        "palette": palette,
        "library_roots": [str(root) for root in roots],
        "library_roots_missing": missing,
        "status": status,
        "errors": errors,
    }


# -------------------------------------------------------------------- inventory


class _ItemFailed(Exception):
    """An item could not be identified safely; its previous record is kept."""


def _fingerprint_stat(kind_key: str, stat_result) -> str:
    """Stat fingerprint of one loose file.

    Device and inode belong to the key: without them two unrelated files that
    happen to share size and timestamps could reuse each other's cached content
    id. The content id itself stays independent of path and stat.
    """
    digest = hashlib.sha256()
    digest.update(
        (
            f"{kind_key}\0{stat_result.st_dev}\0{stat_result.st_ino}\0"
            f"{stat_result.st_size}\0{stat_result.st_mtime_ns}\0{stat_result.st_ctime_ns}"
        ).encode()
    )
    return digest.hexdigest()


def _fingerprint_entries(entries: list) -> str:
    digest = hashlib.sha256()
    digest.update(b"project\0")
    for relative, _path, stat_result in entries:
        digest.update(
            (
                f"{relative}\0{stat_result.st_dev}\0{stat_result.st_ino}\0"
                f"{stat_result.st_size}\0{stat_result.st_mtime_ns}\0{stat_result.st_ctime_ns}\0"
            ).encode("utf-8", "surrogateescape")
        )
    return digest.hexdigest()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_project(entries: list) -> str:
    """Content digest over sorted relative names and bytes (path independent)."""
    digest = hashlib.sha256()
    digest.update(b"wallpaper-context-project-v1\0")
    for relative, path, stat_result in entries:
        digest.update(b"\x01")
        digest.update(relative.encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        digest.update(str(stat_result.st_size).encode())
        digest.update(b"\0")
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(HASH_CHUNK), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _collect_project_files(project: Path, project_real: str, current: Path, out: list, errors: list, depth: int) -> None:
    """Collect every file belonging to a project digest.

    Any dependency that cannot be read or trusted fails the whole item
    (_ItemFailed): a project hashed from an unknown subset would be given a
    content identity its bytes do not have. The caller keeps the previous record.
    """
    if depth > MAX_PROJECT_DEPTH:
        errors.append(
            {"path": str(current), "message": f"project depth limit ({MAX_PROJECT_DEPTH}) reached; not descended"}
        )
        raise _ItemFailed(
            f"project exceeds the depth limit ({MAX_PROJECT_DEPTH}); refusing to hash an incomplete project"
        )
    try:
        with os.scandir(current) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
    except OSError as exc:
        errors.append({"path": str(current), "message": f"cannot list project directory: {exc}"})
        raise _ItemFailed(f"cannot list project directory {current}: {exc}") from exc
    for entry in entries:
        path = Path(entry.path)
        try:
            is_link = entry.is_symlink()
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError as exc:
            errors.append({"path": str(path), "message": f"cannot stat entry: {exc}"})
            raise _ItemFailed(f"cannot stat project entry {path}: {exc}") from exc
        if is_link:
            resolved = os.path.realpath(str(path))
            if not (resolved == project_real or resolved.startswith(project_real + os.sep)):
                errors.append(
                    {"path": str(path), "message": "symlink escapes the project; excluded from digest"}
                )
                raise _ItemFailed(f"project entry {path} is a symlink escaping the project")
            if os.path.isdir(resolved):
                errors.append({"path": str(path), "message": "symlinked directory not followed"})
                raise _ItemFailed(f"project entry {path} is a symlinked directory")
            if not os.path.isfile(resolved):
                errors.append({"path": str(path), "message": "symlink target is not a regular file"})
                raise _ItemFailed(f"project entry {path} does not resolve to a regular file")
            try:
                stat_result = os.stat(resolved)
            except OSError as exc:
                errors.append({"path": str(path), "message": f"cannot stat symlink target: {exc}"})
                raise _ItemFailed(f"cannot stat symlink target {path}: {exc}") from exc
            out.append((_relative_name(path, project), path, stat_result))
            continue
        if is_dir:
            _collect_project_files(project, project_real, path, out, errors, depth + 1)
            continue
        try:
            is_file = entry.is_file(follow_symlinks=False)
            stat_result = entry.stat() if is_file else None
        except OSError as exc:
            errors.append({"path": str(path), "message": f"cannot stat entry: {exc}"})
            raise _ItemFailed(f"cannot stat project entry {path}: {exc}") from exc
        if not is_file or stat_result is None:
            errors.append(
                {
                    "path": str(path),
                    "message": "project entry is not a regular file; refusing a partial digest",
                }
            )
            raise _ItemFailed(f"project entry {path} is not a regular file")
        out.append((_relative_name(path, project), path, stat_result))


def _relative_name(path: Path, project: Path) -> str:
    return os.path.relpath(str(path), str(project)).replace(os.sep, "/")


def _project_entries(project: Path, errors: list) -> list:
    out: list = []
    _collect_project_files(project, os.path.realpath(str(project)), project, out, errors, 0)
    out.sort(key=lambda item: item[0])
    return out


def _read_project_json(project: Path, errors: list) -> dict:
    """Read project.json; invalid or missing input fails the whole item."""
    manifest = project / PROJECT_MARKER
    try:
        value = _read_json(manifest)
    except ValueError as exc:
        errors.append({"path": str(manifest), "message": str(exc)})
        raise _ItemFailed(f"cannot read project manifest: {exc}") from exc
    if not isinstance(value, dict):
        errors.append({"path": str(manifest), "message": "project.json is not a JSON object"})
        raise _ItemFailed(f"{manifest} is not a JSON object")
    return value


def _resolve_project_ref(project: Path, project_real: str, value, label: str, errors: list, warn_missing: bool = False):
    """Resolve a project.json relative reference, refusing absolute/escaping paths.

    A malformed or escaping reference raises _ItemFailed (the item's contents are
    not what project.json claims). A reference that is merely absent only records
    an error, because a packed Wallpaper Engine project may keep the referenced
    asset inside its package.
    """
    if value is None:
        return None
    manifest = project / PROJECT_MARKER
    if not isinstance(value, str) or not value.strip():
        errors.append({"path": str(manifest), "message": f"{label!r} is not a usable path string"})
        raise _ItemFailed(f"project.json {label!r} is not a usable path string")
    candidate = value.strip().replace("\\", "/")
    if os.path.isabs(candidate) or candidate.startswith(".."):
        errors.append(
            {"path": str(manifest), "message": f"{label!r} escapes the project directory: {value!r}"}
        )
        raise _ItemFailed(f"project.json {label!r} escapes the project directory: {value!r}")
    target = project / candidate
    resolved = os.path.realpath(str(target))
    if not (resolved == project_real or resolved.startswith(project_real + os.sep)):
        errors.append(
            {"path": str(target), "message": f"{label!r} resolves outside the project directory; refused"}
        )
        raise _ItemFailed(f"project.json {label!r} resolves outside the project directory")
    if not os.path.isfile(resolved):
        if warn_missing:
            errors.append({"path": str(target), "message": f"declared {label} is missing"})
        return None
    return target


def _project_item(project: Path, content_id: str, errors: list) -> dict:
    manifest = _read_project_json(project, errors)
    project_real = os.path.realpath(str(project))
    title = _clean_text(manifest.get("title")) or project.name
    type_key = manifest.get("type")
    kind = PROJECT_TYPE_MAP.get(type_key.strip().lower(), "unknown") if isinstance(type_key, str) else "unknown"
    media = _resolve_project_ref(
        project, project_real, manifest.get("file"), "file", errors,
        warn_missing=kind not in {"scene", "web"},
    )
    media_path = (
        str(media) if media is not None and media.suffix.lower() in MEDIA_EXTENSIONS else None
    )
    preview = _resolve_project_ref(project, project_real, manifest.get("preview"), "preview", errors, warn_missing=True)
    return {
        "id": content_id,
        "title": title,
        "kind": kind,
        "path": str(project),
        "media_path": media_path,
        "preview_path": str(preview) if preview is not None else None,
        "provider": WE_PROVIDER,
        "source_id": project.name,
    }


def _media_item(path: Path, content_id: str, kind_key: str) -> dict:
    return {
        "id": content_id,
        "title": _clean_text(path.name) or path.name,
        "kind": kind_key,
        "path": str(path),
        "media_path": str(path),
        "preview_path": None,
        "provider": LOOSE_PROVIDER,
        "source_id": None,
    }


def _identify(candidate: dict, fingerprint_ids: dict, errors: list) -> tuple:
    """Return (item, fingerprint entry) for one candidate, or raise _ItemFailed."""
    path: Path = candidate["path"]
    kind_key: str = candidate["kind_key"]
    if kind_key == "project":
        entries = _project_entries(path, errors)
        fingerprint = _fingerprint_entries(entries)
        content_id = fingerprint_ids.get(fingerprint)
        if content_id is None:
            content_id = _hash_project(entries)
            # Re-stat after hashing: a source that changed while being read is not indexed.
            if _fingerprint_entries(_project_entries(path, [])) != fingerprint:
                raise _ItemFailed("project changed while hashing; previous record kept")
        item = _project_item(path, content_id, errors)
    else:
        stat_result = path.stat()
        fingerprint = _fingerprint_stat(kind_key, stat_result)
        content_id = fingerprint_ids.get(fingerprint)
        if content_id is None:
            content_id = _hash_file(path)
            if _fingerprint_stat(kind_key, path.stat()) != fingerprint:
                raise _ItemFailed("file changed while hashing; previous record kept")
        item = _media_item(path, content_id, kind_key)
    entry = {
        "id": item["id"],
        "fingerprint": fingerprint,
        "kind": item["kind"],
        "source_id": item["source_id"],
        "updated_at": utc_now(),
    }
    return item, entry


def current_content_id(item: dict) -> str:
    """Recompute one inventory item's content id from its source, right now.

    The digest algorithm is exactly the scan algorithm, but no saved stat
    fingerprint is consulted, so the result is the identity of the bytes as they
    are at this moment: a caller can compare it with the recorded id before and
    after preparing evidence, and detect a source that changed under it.

    Read-only: nothing is written and no fingerprint cache is touched.
    Raises ValueError when the item is malformed or its source is incomplete or
    changed while being read (unreadable project dependency, escaping reference,
    invalid project.json, no project marker, file instability); OSError only when
    the source itself cannot be read at all.
    """
    if not isinstance(item, dict):
        raise ValueError("inventory item must be a JSON object")
    raw_path = item.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("inventory item has no usable 'path' string")
    path = Path(_abspath(raw_path))
    candidate: dict = {"path": path}
    if path.is_dir():
        if not (path / PROJECT_MARKER).is_file():
            raise ValueError(
                f"{path}: directory item without {PROJECT_MARKER}; not a Wallpaper Engine project"
            )
        candidate["kind_key"] = "project"
    else:
        candidate["kind_key"] = "video" if item.get("kind") == "video" else "image"
    errors: list = []
    try:
        identified, _fingerprint_entry = _identify(candidate, {}, errors)
    except _ItemFailed as exc:
        raise ValueError(f"{path}: {exc}") from exc
    return identified["id"]


def _symlink_state(entry, path: Path, root: Path, errors: list) -> str:
    """Classify a symlink entry: 'directory', 'file' or 'denied' (outside the root)."""
    try:
        target_is_dir = entry.is_dir(follow_symlinks=True)
    except OSError:
        target_is_dir = os.path.isdir(os.path.realpath(str(path)))
    if target_is_dir:
        return "directory"
    resolved = os.path.realpath(str(path))
    root_real = os.path.realpath(str(root))
    if not (resolved == root_real or resolved.startswith(root_real + os.sep)):
        errors.append({"path": str(path), "message": "symlink points outside the scan root; skipped"})
        return "denied"
    if not os.path.isfile(resolved):
        errors.append({"path": str(path), "message": "symlink target is not a regular file; skipped"})
        return "denied"
    return "file"


def _walk(root: Path, excluded: list, errors: list) -> tuple[list, set]:
    """Collect candidate items below a root: project dirs and loose media files.

    Returns (candidates, failed_paths). `failed_paths` names every directory the
    walk could not enumerate (or was told not to descend into): the caller keeps
    the records previously indexed under such a subtree instead of reading the
    unreadable directory as "everything here was deleted".
    """
    out: list = []
    failed: set = set()
    stack: list = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            errors.append({"path": str(directory), "message": f"cannot list directory: {exc}"})
            failed.add(str(directory))
            continue
        for entry in entries:
            path = Path(entry.path)
            if any(_is_under(path, prefix) for prefix in excluded):
                continue
            try:
                is_link = entry.is_symlink()
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError as exc:
                errors.append({"path": str(path), "message": f"cannot stat entry: {exc}"})
                failed.add(str(path))
                continue
            state = "directory" if is_dir and not is_link else None
            if is_link:
                state = _symlink_state(entry, path, root, errors)
            if state == "directory":
                if not is_link:
                    try:
                        if (path / PROJECT_MARKER).is_file():
                            out.append({"path": path, "kind_key": "project"})
                            continue
                    except OSError as exc:
                        errors.append({"path": str(path), "message": f"cannot inspect {PROJECT_MARKER}: {exc}"})
                        failed.add(str(path))
                        continue
                if is_link:
                    errors.append({"path": str(path), "message": "symlinked directory not followed"})
                    continue
                if depth + 1 > MAX_SCAN_DEPTH:
                    errors.append(
                        {"path": str(path), "message": f"scan depth limit ({MAX_SCAN_DEPTH}) reached; not descended"}
                    )
                    failed.add(str(path))
                    continue
                stack.append((path, depth + 1))
                continue
            if state == "denied":
                continue
            if not is_link:
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError as exc:
                    errors.append({"path": str(path), "message": f"cannot stat entry: {exc}"})
                    failed.add(str(path))
                    continue
            extension = path.suffix.lower()
            if extension in IMAGE_EXTENSIONS:
                out.append({"path": path, "kind_key": "image"})
            elif extension in VIDEO_EXTENSIONS:
                out.append({"path": path, "kind_key": "video"})
    return out, failed


def _excluded_prefixes(base: Path) -> list:
    """Never index studio bookkeeping subtrees when a root happens to contain them.

    Only the studio-owned output subtrees are excluded: frames/, captures/,
    analysis/, logs/ and the analysis cache. The managed imports root lives
    under the same data root and MUST be walked — it contains real project
    directories.
    """
    prefixes = [
        base / "frames",
        base / "captures",
        base / "analysis",
        base / "logs",
        cache_root(),
    ]
    unique: list = []
    seen: set[str] = set()
    for prefix in prefixes:
        try:
            resolved = _norm_root(prefix)
        except OSError:
            continue
        if str(resolved) not in seen:
            seen.add(str(resolved))
            unique.append(resolved)
    return unique


def _dedup_roots(roots: list, errors: list) -> list:
    """Normalize, deduplicate and drop roots nested inside another root."""
    unique: list = []
    seen: set[str] = set()
    for root in roots:
        resolved = _norm_root(root)
        key = str(resolved)
        if key in seen:
            errors.append({"path": key, "message": "duplicate root; scanned once"})
            continue
        seen.add(key)
        unique.append(resolved)
    final: list = []
    for root in unique:
        parent = next((other for other in unique if other != root and _is_under(root, other)), None)
        if parent is not None:
            errors.append(
                {"path": str(root), "message": f"root is inside {parent}; skipped to avoid double indexing"}
            )
            continue
        final.append(root)
    return final


def scan(roots, base) -> dict:
    """Incremental inventory scan. Returns the index that was written.

    Unchanged source fingerprints reuse the recorded content id without reading
    file bytes again; project digests stay identical after a move. Only content
    under a root that was fully scanned can disappear from the inventory: any
    failed root, failed item or unreadable directory keeps its previous records
    (and analyses are never touched). With no --root, the discovered libraries
    and the roots already saved in the index are scanned.
    """
    base = Path(base)
    with advisory_lock(base / INDEX_FILE):
        errors: list = []
        previous = load_index(base)
        previous_items = {
            item["path"]: item for item in previous.get("items", []) if isinstance(item.get("path"), str)
        }
        previous_fingerprints = _load_fingerprints(base, errors)
        fingerprint_ids: dict = {}
        for entry in previous_fingerprints.values():
            fingerprint_ids.setdefault(entry["fingerprint"], entry["id"])

        previous_roots_raw = [root for root in previous.get("roots", []) if isinstance(root, str)]
        requested = (
            [Path(root) for root in roots]
            if roots
            else _discovery_roots(base, errors, previous_roots_raw)
        )
        roots_norm = _dedup_roots(requested, errors)
        excluded = _excluded_prefixes(base)
        previous_roots = {_norm_root(root) for root in previous_roots_raw}

        discovered: dict = {}
        computed_fingerprints: dict = {}
        failed_paths: set = set()
        active_roots: list = []

        for root in roots_norm:
            if not root.is_dir():
                errors.append(
                    {"path": str(root), "message": "root missing or not a directory; previous records preserved"}
                )
                continue
            try:
                root_is_project = (root / PROJECT_MARKER).is_file()
            except OSError as exc:
                errors.append({"path": str(root), "message": f"cannot inspect {PROJECT_MARKER}: {exc}"})
                failed_paths.add(str(root))
                continue
            active_roots.append(root)
            if root_is_project:
                # A root that is itself a Wallpaper Engine project is one asset.
                candidates = [{"path": root, "kind_key": "project"}]
            else:
                candidates, walk_failed = _walk(root, excluded, errors)
                failed_paths |= walk_failed
            for candidate in candidates:
                try:
                    item, fingerprint_entry = _identify(candidate, fingerprint_ids, errors)
                except _ItemFailed as exc:
                    failed_paths.add(str(candidate["path"]))
                    errors.append({"path": str(candidate["path"]), "message": str(exc)})
                    continue
                except OSError as exc:
                    failed_paths.add(str(candidate["path"]))
                    errors.append({"path": str(candidate["path"]), "message": f"cannot read item: {exc}"})
                    continue
                discovered[item["path"]] = item
                computed_fingerprints[item["path"]] = fingerprint_entry
                fingerprint_ids.setdefault(fingerprint_entry["fingerprint"], item["id"])

        items: list = []
        for path in sorted(discovered):
            items.append(discovered[path])
        for path in sorted(previous_items):
            if path in discovered:
                continue
            # A path that failed to be identified, or that sits inside a directory
            # the walk could not enumerate, is not proof that the source is gone:
            # keep the previous record (and its analyses) instead of dropping it.
            if any(_is_under(path, root) for root in active_roots) and not any(
                _is_under(path, failed) for failed in failed_paths
            ):
                continue
            items.append(previous_items[path])
        items.sort(key=lambda item: (item["path"], item["id"]))

        kept_roots = {
            str(root)
            for root in previous_roots
            if not any(_is_under(root, scanned) for scanned in roots_norm)
        }
        index = {
            "schema_version": SCHEMA_VERSION,
            "scanned_at": utc_now(),
            "roots": sorted(kept_roots | {str(root) for root in roots_norm}),
            "items": items,
            "errors": errors,
        }

        fingerprints: dict = {}
        for item in items:
            path = item["path"]
            if path in computed_fingerprints:
                fingerprints[path] = computed_fingerprints[path]
            elif path in previous_fingerprints:
                fingerprints[path] = previous_fingerprints[path]
        atomic_json(
            _fingerprints_path(base),
            {"schema_version": SCHEMA_VERSION, "updated_at": index["scanned_at"], "entries": fingerprints},
        )
        atomic_json(base / INDEX_FILE, index)
        return index


# --------------------------------------------------------------------------- CLI


def _cli(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="catalog.py",
        description="Inventory wallpapers (Wallpaper Engine projects and loose media) with an incremental content cache.",
    )
    parser.add_argument("--data-dir", default=None, help="record directory (default: XDG data home + /wallpaper-context)")
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan_parser = subparsers.add_parser("scan", help="scan roots and write index.json/fingerprints.json")
    scan_parser.add_argument(
        "--root",
        action="append",
        default=[],
        help="library root to scan (repeatable); without it the discovered libraries and the saved roots are used",
    )
    subparsers.add_parser("list", help="print the saved index (read-only)")
    args = parser.parse_args(argv)

    base = Path(os.path.expanduser(args.data_dir)) if args.data_dir else data_root()
    try:
        if args.command == "scan":
            result = scan([Path(root) for root in args.root], base)
        else:
            result = load_index(base)
    except (ValueError, OSError) as exc:
        emit_json({"error": {"message": str(exc), "data_dir": str(base)}})
        print(f"catalog.py: {exc}", file=sys.stderr)
        return 2
    emit_json(result)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
