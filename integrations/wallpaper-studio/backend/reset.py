#!/usr/bin/env python3
"""Scoped reset of one Wallpaper Studio wallpaper.

Removes exactly one wallpaper's studio-side artifacts: its analysis record and
render binding, its per-wallpaper design overrides and its owned design
profiles, every candidate variant with its mapping, the job history that
produced them, and the generated packs, owned installed packs and preview cache
those variants own.  Original media, captures, global preferences and player
state are never touched; a variant belonging to another wallpaper is never
touched.

Everything removed is *moved* into a scoped recovery backup under
``<data>/reset/<content_id>/<token>/`` before the studio record is rewritten,
and a journal records every step.  A failure therefore restores the filesystem
and the record instead of leaving a half-reset wallpaper; when the native
``terminal-skin clear`` already ran, that fact is reported explicitly as
compensation that needs a human.

The selected wallpaper's active skin is cleared natively only when the
observed active pack is actually owned by this content. The native clear
independently checks the compositor runtime before changing it. An unrelated
active skin is left alone.

Concurrency: the ``studio-apply.lock`` the library serializes applications on,
then the state lock, the non-blocking worker lease and the per-content analyzer
lock.  A busy studio (active or queued job, running worker, analysis in flight)
refuses the reset instead of racing it.  Never runs a model, never regenerates
anything and never signals a compositor.
"""

from __future__ import annotations

import fcntl
import importlib
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

# The installed backend directory is shared and read by other tools: never drop
# __pycache__ next to these modules (the helpers already load the same way).
sys.dont_write_bytecode = True

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ``library`` owns the skin store layout and the native CLI invocation; reusing
# its helpers keeps the env overrides (KITTY_SKINS_ROOT, WALLPAPER_STUDIO_*)
# and the apply lock single-sourced instead of re-implemented here.
import context  # type: ignore
import library  # type: ignore
import process  # type: ignore
import storage  # type: ignore

#: Backup root: one recovery directory per (content id, reset token).
RESET_DIR = "reset"
#: Frame pack root: ``<data>/frames/<content id>/<variant id>``.
FRAMES_DIR = storage.FRAMES_DIR
#: Real-frame preview cache root: ``<data>/previews/<variant id>/<digest>``.
PREVIEWS_DIR = "previews"
#: Quarantine subdirectory for installed packs moved out of the skin store.
INSTALLED_DIR = "installed"
JOURNAL_FILE = "journal.json"
STATE_SNAPSHOT_FILE = "studio.json"
JOURNAL_SCHEMA_VERSION = 1
#: ``terminal-skin clear`` is a local dispatch; same bound as ``use``.
CLEAR_TIMEOUT = 60.0
ACTIVE_JOB_STATES = ("queued", "running")


class ResetError(RuntimeError):
    """User-visible reset failure; the message is safe to show."""


def _log(data: Path, message: str) -> None:
    line = f"{storage.now()} reset: {storage.clean_text(str(message), 1000)}\n"
    try:
        path = storage.worker_log_path(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass


# ------------------------------------------------------------------- locking


class _ApplyLock:
    """Exclusive ``studio-apply.lock``: the lock every library application takes.

    The path is ``storage.data_root() / studio-apply.lock`` — the exact file
    ``library`` locks, which resolves the data root without the caller's
    override, so both modules really serialize on one lock.

    Acquired non-blocking: a reset must never hang a single-threaded backend
    request behind a slow application, so a busy lock is refused explicitly.
    """

    def __init__(self) -> None:
        self.path = storage.data_root() / library._APPLY_LOCK
        self._handle = None

    def __enter__(self) -> "_ApplyLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise ResetError("another studio application is in progress; retry the reset") from None
        self._handle = handle
        return self

    def __exit__(self, *exc) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _analyzer_lock(data: Path, content_id: str):
    """Non-blocking per-content analyzer lock, released by :func:`_release_lock`.

    The installed analyzer takes exactly this lock (``<data>/locks/<id>.lock``)
    around evidence extraction and the model call, so holding it here means no
    analysis of this content can publish a record while the reset runs.
    """
    path = data / "locks" / f"{content_id}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise ResetError("this wallpaper is being analyzed right now; retry the reset") from None
    return handle


def _release_lock(handle) -> None:
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        try:
            handle.close()
        except OSError:
            pass


# ------------------------------------------------------------ observations


def _check_content_id(value) -> str:
    return library._check_id(value, library._CONTENT_ID, "content id")


def _inventory_ids(data: Path) -> set[str]:
    """Content ids the saved inventory knows; a broken index means 'unknown'."""
    try:
        index = context.inventory(data)
    except (RuntimeError, ValueError, OSError):
        return set()
    items = index.get("items") if isinstance(index, dict) else None
    ids = set()
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            ids.add(item["id"])
    return ids


def _active_pack_ids(data: Path) -> set[str]:
    """Observe the active store selection; native clear also guards runtime.

    `terminal-skin current` reads the store, not compositor memory. Never treat
    `last_applied` as proof of current ownership.
    """
    observed: set[str] = set()
    skins_root = Path(os.path.realpath(library._skins_root()))
    link = skins_root.parent / "active"
    try:
        if link.is_symlink():
            target = Path(os.path.realpath(link))
            if target.parent == skins_root and target.name and target != skins_root:
                observed.add(target.name)
            else:
                raise ResetError(f"active link points outside the skin store: {link}")
    except OSError as exc:
        raise ResetError(f"cannot read active link {link}: {exc}") from exc
    current = library._current_pack_path()
    if current is not None:
        observed.add(current.name)
    return observed


def _installed_pack(variant: dict, pack_id: str) -> Path | None:
    """The variant's installed pack, or None when the record is not trustworthy.

    The recorded path is only accepted when it really is a direct child of the
    skin store named after the variant's pack id; anything else is left alone.
    """
    raw = variant.get("installed_path")
    if not isinstance(raw, str) or not raw or not os.path.isabs(raw):
        return None
    try:
        if os.path.islink(raw):
            return None
    except OSError:
        return None
    real = Path(os.path.realpath(raw))
    skins_root = Path(os.path.realpath(library._skins_root()))
    if real.parent != skins_root or real.name != pack_id or not real.is_dir():
        return None
    return real


def _shared_installed_refs(state: dict, own_ids: set[str]) -> set[str]:
    """Installed packs or pack ids another wallpaper's variant still references."""
    refs: set[str] = set()
    for variant in state.get("variants", []):
        if not isinstance(variant, dict) or variant.get("id") in own_ids:
            continue
        raw = variant.get("installed_path")
        if isinstance(raw, str) and raw:
            refs.add(os.path.realpath(raw))
        pack_id = variant.get("pack_id")
        if isinstance(pack_id, str) and pack_id:
            refs.add(f"pack:{pack_id}")
    return refs


# ------------------------------------------------------------------- journal


def _save_journal(path: Path, record: dict) -> None:
    storage.atomic_json(path, record)


def _note(journal: dict, message: str) -> None:
    journal["operations"].append({"kind": "note", "message": storage.clean_text(message, 500)})


# ---------------------------------------------------------------------- plan


def _managed_path(path: Path, root: Path) -> Path:
    """Refuse symlinked ancestors before moving anything out of an owned tree."""
    if not path.is_relative_to(root) or path == root:
        raise ResetError(f"reset path is outside its managed root: {path}")
    current = path
    while current != root:
        if current.is_symlink():
            raise ResetError(f"reset refuses a symlinked managed path: {current}")
        current = current.parent
    if not path.resolve().is_relative_to(root.resolve()):
        raise ResetError(f"reset path escapes its managed root: {path}")
    return path


def _quarantine_targets(data: Path, content_id: str, variants: list, shared: set[str]) -> list:
    """Owned analysis, all generated attempts, previews and installed variants."""
    targets: list[tuple[Path, str]] = []
    analysis = storage.analysis_path(content_id, data)
    targets.append((analysis, f"{storage.ANALYSIS_DIR}/{analysis.name}"))
    binding = storage.analysis_binding_path(content_id, data)
    targets.append((binding, f"{storage.ANALYSIS_DIR}/{binding.name}"))
    # Failed/unpublished generations belong to this wallpaper too.
    targets.append((data / FRAMES_DIR / content_id, f"{FRAMES_DIR}/{content_id}"))
    seen_installed: set[str] = set()
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        variant_id = library._check_id(variant.get("id"), library._UUID32, "variant id")
        pack_id = library._check_id(variant.get("pack_id"), library._PACK_ID, "pack id")
        preview = data / PREVIEWS_DIR / variant_id
        targets.append((preview, f"{PREVIEWS_DIR}/{variant_id}"))
        installed = _installed_pack(variant, pack_id)
        if installed is None:
            continue
        if os.path.realpath(installed) in shared or f"pack:{pack_id}" in shared:
            _log(data, f"installed pack {pack_id} is still referenced by another wallpaper; kept")
            continue
        if pack_id in seen_installed:
            continue
        seen_installed.add(pack_id)
        targets.append((installed, f"{INSTALLED_DIR}/{pack_id}"))
    skins_root = Path(os.path.realpath(library._skins_root()))
    return [
        (_managed_path(src, skins_root if rel.startswith(INSTALLED_DIR + "/") else data), rel)
        for src, rel in targets
    ]


def _next_state(state: dict, content_id: str, own_ids: set[str]) -> tuple[dict, dict]:
    """The studio record with exactly this wallpaper's entries removed.

    Nothing outside the selection changes: other wallpapers' variants, their
    mappings, their profiles and broad batch jobs stay verbatim.
    """
    updated = json.loads(json.dumps(state))
    updated["variants"] = [
        variant
        for variant in updated["variants"]
        if not (isinstance(variant, dict) and (variant.get("content_id") == content_id or variant.get("id") in own_ids))
    ]
    referenced = set()
    for variant in updated["variants"]:
        if not isinstance(variant, dict):
            continue
        provenance = variant.get("provenance")
        profile_id = provenance.get("profile_id") if isinstance(provenance, dict) else None
        if isinstance(profile_id, str) and profile_id:
            referenced.add(profile_id)
    dropped_profiles = [
        profile_id
        for profile_id, profile in updated["design_profiles"].items()
        if isinstance(profile, dict) and profile.get("content_id") == content_id and profile_id not in referenced
    ]
    for profile_id in dropped_profiles:
        del updated["design_profiles"][profile_id]
    updated["mappings"] = {
        key: value for key, value in updated["mappings"].items() if key != content_id and value not in own_ids
    }
    updated["design_overrides"].pop(content_id, None)
    updated["feedback"] = [
        entry for entry in updated["feedback"]
        if entry["content_id"] != content_id and entry["variant_id"] not in own_ids
    ]
    dropped_job_ids = {
        job.get("id")
        for job in updated["jobs"]
        if isinstance(job, dict) and job.get("content_id") == content_id
    }
    updated["jobs"] = [
        job for job in updated["jobs"] if not (isinstance(job, dict) and job.get("id") in dropped_job_ids)
    ]
    for job in updated["jobs"]:
        # A retry of an old batch must not resurrect the reset design edits.
        payload = job.get("payload") or {}
        overrides = payload.get("design_overrides")
        if isinstance(overrides, dict):
            overrides.pop(content_id, None)
    last_applied = updated.get("last_applied")
    if isinstance(last_applied, dict) and (
        last_applied.get("content_id") == content_id or last_applied.get("variant_id") in own_ids
    ):
        updated["last_applied"] = None
    sync_error = updated.get("sync_error")
    if isinstance(sync_error, str) and (
        content_id in sync_error or any(variant_id in sync_error for variant_id in own_ids)
    ):
        updated["sync_error"] = None
    return updated, {"profiles": len(dropped_profiles), "jobs": len(dropped_job_ids)}


# ------------------------------------------------------------------ recovery


def _rollback(journal: dict, journal_path: Path, state_path: Path, state_bytes: bytes | None,
              state_written: bool) -> list[str]:
    """Undo what was already done; returns explicit compensation failures."""
    compensation: list[str] = []
    if state_written and state_bytes is not None:
        try:
            storage.atomic_write_bytes(state_path, state_bytes)
            _note(journal, "studio record restored from the snapshot")
        except OSError as exc:
            compensation.append(f"the studio record could not be restored: {exc}")
    moves = [op for op in journal["operations"] if op.get("kind") == "move"]
    for op in reversed(moves):
        src = Path(op["src"])
        dst = Path(op["dst"])
        try:
            if not dst.exists():
                _note(journal, f"{dst} vanished before it could be restored")
                continue
            if src.exists() or src.is_symlink():
                compensation.append(f"{src} is occupied; its quarantined copy stays at {dst}")
                continue
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dst), str(src))
            op["status"] = "restored"
        except OSError as exc:
            compensation.append(f"{dst} could not be restored to {src}: {exc}")
    if journal.get("native_clear", {}).get("status") == "done":
        compensation.append(
            f"the active skin {journal['native_clear'].get('pack_id')} was already cleared by "
            "terminal-skin clear; decorations are gone and the wallpaper must be re-applied to restore them"
        )
    journal["status"] = "rollback_failed" if compensation else "rolled_back"
    try:
        _save_journal(journal_path, journal)
    except OSError:
        # The journal must never mask the failure it was recording.
        pass
    return compensation


# ---------------------------------------------------------------------- api


def reset_content(content_id: str, *, confirmed: bool = False, data: Path | None = None) -> dict:
    """Reset one wallpaper's studio-side artifacts into a recovery backup.

    Returns ``{content_id, reset_token, backup_path, disabled_active}`` plus a
    bounded ``removed`` summary.  Raises :class:`ResetError` (or ``ValueError``
    for a malformed request) instead of reporting success whenever anything —
    the backup, the native clear, a move, the record write — failed.
    """
    if confirmed is not True:
        raise ValueError("reset requires explicit confirmation")
    root = storage.resolve_root(data).resolve()
    identifier = _check_content_id(content_id)
    with _ApplyLock():
        with storage.state_lock(root):
            lease = process.acquire_worker_lock(root)
            if lease is None:
                raise ResetError("another studio worker is running; retry the reset once it is idle")
            try:
                analyzer_lock = _analyzer_lock(root, identifier)
                try:
                    return _perform(root, identifier)
                finally:
                    _release_lock(analyzer_lock)
            finally:
                process.release_worker_lock(lease)


def _perform(root: Path, content_id: str) -> dict:
    state = storage.read_state(root)
    busy = next(
        (
            job
            for job in state["jobs"]
            if isinstance(job, dict) and job.get("state") in ACTIVE_JOB_STATES
        ),
        None,
    )
    if isinstance(busy, dict):
        raise ResetError(
            f"a {busy.get('kind')} job is {busy.get('state')}; wait for it or cancel it before resetting"
        )

    own_variants = [
        variant
        for variant in state["variants"]
        if isinstance(variant, dict) and variant.get("content_id") == content_id
    ]
    own_ids = {variant["id"] for variant in own_variants if isinstance(variant.get("id"), str)}
    analysis = storage.analysis_path(content_id, root)
    has_state = bool(
        own_variants
        or state["mappings"].get(content_id) is not None
        or content_id in state["design_overrides"]
        or any(entry["content_id"] == content_id for entry in state["feedback"])
        or any(
            isinstance(profile, dict) and profile.get("content_id") == content_id
            for profile in state["design_profiles"].values()
        )
        or analysis.is_file()
        or any(isinstance(job, dict) and job.get("content_id") == content_id for job in state["jobs"])
    )
    if not has_state and content_id not in _inventory_ids(root):
        raise ResetError("this content id is not a known wallpaper; nothing to reset")

    shared = _shared_installed_refs(state, own_ids)
    owned_packs = {
        variant["pack_id"]
        for variant in own_variants
        if isinstance(variant.get("pack_id"), str)
        and _installed_pack(variant, variant["pack_id"]) is not None
        and os.path.realpath(variant["installed_path"]) not in shared
        and f"pack:{variant['pack_id']}" not in shared
    }

    observed = _active_pack_ids(root)
    clear_pack: str | None = None
    if len(observed) > 1:
        raise ResetError(
            "the active skin is reported inconsistently (store link vs runtime); refusing to clear it"
        )
    if observed:
        pack_id = next(iter(observed))
        if pack_id in owned_packs:
            clear_pack = pack_id
        elif any(variant.get("pack_id") == pack_id for variant in own_variants):
            raise ResetError("the selected wallpaper's active pack has ambiguous ownership; nothing reset")

    targets = _quarantine_targets(root, content_id, own_variants, shared)
    updated, dropped = _next_state(state, content_id, own_ids)

    token = uuid.uuid4().hex
    backup = root / RESET_DIR / content_id / token
    backup.mkdir(parents=True, mode=0o700, exist_ok=False)
    journal_path = backup / JOURNAL_FILE
    state_path = storage.state_path(root)
    journal = {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "content_id": content_id,
        "reset_token": token,
        "created_at": storage.now(),
        "backup_path": str(backup),
        "status": "in_progress",
        "native_clear": {"required": clear_pack is not None, "pack_id": clear_pack, "status": "not-needed"},
        "operations": [],
    }

    state_bytes: bytes | None = None
    state_written = False
    moved = 0
    disabled_active = False
    try:
        try:
            state_bytes = state_path.read_bytes() if state_path.exists() else (
                json.dumps(state, ensure_ascii=False, indent=2) + "\n"
            ).encode("utf-8")
            (backup / STATE_SNAPSHOT_FILE).write_bytes(state_bytes)
            _note(journal, "studio record snapshotted")
        except OSError as exc:
            raise ResetError(f"could not snapshot the studio record: {exc}") from exc
        _save_journal(journal_path, journal)

        # Close only the selected content's viewer before moving cached media.
        importlib.import_module("viewer").close_for_content(content_id, data=root)
        _note(journal, "closed the selected wallpaper's viewer")
        _save_journal(journal_path, journal)

        # 2. Clear the selected wallpaper's own active skin (never another one).
        if clear_pack is not None:
            library._run_terminal_skin(["clear", clear_pack], CLEAR_TIMEOUT)
            journal["native_clear"]["status"] = "done"
            disabled_active = True
            _note(journal, f"terminal-skin clear {clear_pack} succeeded")
            _save_journal(journal_path, journal)

        # 3. Quarantine the owned filesystem artifacts.
        for src, rel in targets:
            try:
                if not src.exists() or src.is_symlink():
                    continue
                dest = backup / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dest))
            except OSError as exc:
                raise ResetError(f"could not quarantine {src}: {exc}") from exc
            journal["operations"].append({"kind": "move", "src": str(src), "dst": str(dest), "status": "done"})
            _save_journal(journal_path, journal)
            moved += 1

        # 4. Rewrite the studio record without this wallpaper's entries.
        storage.write_state(updated, root)
        state_written = True
        _note(journal, "studio record rewritten")
        journal["status"] = "committed"
        _save_journal(journal_path, journal)
    except Exception as exc:
        compensation = _rollback(journal, journal_path, state_path, state_bytes, state_written)
        detail = f"{type(exc).__name__}: {exc}" if not isinstance(exc, ResetError) else str(exc)
        suffix = (
            "; recovery backup kept at " + str(backup)
            + ("; compensation failed: " + "; ".join(compensation) if compensation else "; rollback complete")
        )
        raise ResetError(storage.clean_text(f"reset failed: {detail}{suffix}", storage.MAX_ERROR_CHARS)) from exc

    _log(root, f"reset {content_id[:12]} committed; backup {backup}")
    return {
        "content_id": content_id,
        "reset_token": token,
        "backup_path": str(backup),
        "disabled_active": disabled_active,
        "removed": {
            "variants": len(own_ids),
            "profiles": dropped["profiles"],
            "jobs": dropped["jobs"],
            "quarantined": moved,
        },
    }

