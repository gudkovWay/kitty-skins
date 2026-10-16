#!/usr/bin/env python3
"""Bridge to the installed wallpaper-context helpers.

The helper scripts stay the single source of truth: catalog.py owns inventory,
discovery and content identity, analyze-wallpapers.py owns evidence, model calls
and analysis records. This module imports those files from their installed
location instead of copying any of their source, and performs no writes, no
analysis and no model calls itself.

Importing writes no bytecode into the skill directory: the module loader runs
with bytecode caching disabled for the duration of the load.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
from pathlib import Path

SCRIPTS_ENV = "WALLPAPER_CONTEXT_SCRIPTS"
DEFAULT_SCRIPTS_DIR = Path.home() / ".omp" / "agent" / "skills" / "wallpaper-context" / "scripts"
CATALOG_FILE = "catalog.py"
ANALYZER_FILE = "analyze-wallpapers.py"

_LOCK = threading.Lock()
_MODULES: dict[str, object] = {}


def scripts_dir() -> Path:
    """Installed helper directory, overridable with $WALLPAPER_CONTEXT_SCRIPTS."""
    raw = os.environ.get(SCRIPTS_ENV, "").strip()
    if raw:
        return Path(os.path.abspath(os.path.expanduser(raw)))
    return DEFAULT_SCRIPTS_DIR


def _load(name: str, filename: str):
    with _LOCK:
        module = _MODULES.get(name)
        if module is not None:
            return module
        path = scripts_dir() / filename
        if not path.is_file():
            raise RuntimeError(f"wallpaper-context helper not found: {path}")
        spec = importlib.util.spec_from_file_location(f"wallpaper_context_{name}", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load wallpaper-context helper: {path}")
        loaded = importlib.util.module_from_spec(spec)
        # The helpers are read-only inputs: never drop __pycache__ next to them.
        previous = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec.loader.exec_module(loaded)
        finally:
            sys.dont_write_bytecode = previous
        _MODULES[name] = loaded
        return loaded


def catalog():
    """The catalog module (inventory, discovery, content identity)."""
    return _load("catalog", CATALOG_FILE)


def analyzer_module():
    """The analyzer module (envelope constants and the visual schema)."""
    return _load("analyzer", ANALYZER_FILE)


def analyzer_path() -> Path:
    """Absolute path of the analyzer CLI used for analysis subprocesses."""
    path = scripts_dir() / ANALYZER_FILE
    if not path.is_file():
        raise RuntimeError(f"wallpaper-context analyzer not found: {path}")
    return path


def catalog_path() -> Path:
    """Absolute path of the catalog CLI used for cancellable scans."""
    path = scripts_dir() / CATALOG_FILE
    if not path.is_file():
        raise RuntimeError(f"wallpaper-context catalog not found: {path}")
    return path


def catalog_scan_argv(data: Path, roots: list[str] | None = None) -> list[str]:
    """argv for an installed-catalog inventory scan.

    The helper CLI stays the scan authority; the caller runs this argv through
    `process.run` so a long scan stays cancellable. Without `roots` the full
    root set of :func:`scan_roots` is used, so a scan covers the same libraries a
    no-root scan would plus the managed imports root.
    """
    argv = [sys.executable, str(catalog_path()), "--data-dir", str(data), "scan"]
    for root in scan_roots(data) if roots is None else roots:
        argv.extend(["--root", str(root)])
    return argv


def scan_roots(data: Path | None = None) -> list[str]:
    """Library roots a full scan has to cover.

    The helper scans exactly the roots it is given, so passing them explicitly
    must still cover everything a no-root scan would: the discovered libraries
    (Steam workshop, declared personal paths) plus the roots the index already
    saved. The managed imports root lives inside the studio data root, which the
    helper never walks, so it is added here explicitly.
    """
    base = data if data is not None else data_root()
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

    discovered = discover(base)
    for root in discovered.get("library_roots") or []:
        add(root)
    index = inventory(base)
    for root in index.get("roots") or []:
        add(root)
    # Only once an import actually created it: the helper reports a missing root
    # as a scan problem, and an empty studio has nothing to import yet.
    imports = imports_root(base)
    if imports.is_dir():
        add(str(imports))
    return roots


def imports_root(base: Path | None = None) -> Path:
    """Managed copies of imported wallpapers (contents belong to playback)."""
    import storage

    return storage.imports_dir(base)


def analyzer_argv(
    data: Path,
    content_id: str,
    *,
    model: str,
    preview_only: bool = False,
    capture: str | None = None,
    limit: int = 1,
) -> list[str]:
    """argv for one installed-analyzer call covering exactly one content id.

    `--capture` needs exactly one `--id` and cannot be combined with
    `--preview-only`; the helper CLI enforces both, and so does this builder.
    """
    if capture and preview_only:
        raise ValueError("capture cannot be combined with preview-only analysis")
    argv = [
        sys.executable,
        str(analyzer_path()),
        "--data-dir",
        str(data),
        "--id",
        str(content_id),
        "--limit",
        str(int(limit)),
        "--model",
        str(model),
    ]
    if preview_only:
        argv.append("--preview-only")
    if capture:
        argv.extend(["--capture", str(capture)])
    return argv


def content_id_re():
    """The catalog's canonical content-id pattern."""
    return catalog().CONTENT_ID_RE


def inventory(base: Path | None = None) -> dict:
    """Read the saved inventory index; read-only, never scans."""
    return catalog().load_index(base if base is not None else data_root())


def discover(base: Path | None = None) -> dict:
    """Read-only provider/palette/library-root discovery (no writes, no IPC changes)."""
    return catalog().discover(base if base is not None else data_root())


def data_root() -> Path:
    import storage

    return storage.data_root()
