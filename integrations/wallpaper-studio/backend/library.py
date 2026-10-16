"""Wallpaper Studio candidate library: approve, reject, apply, sync.

Owns the candidate variant lifecycle on top of the fixed shared modules
(``storage``, ``context``, ``process``).  External work (terminal-skin
validation/use, filesystem copies) always happens outside the state lock;
state mutations go through ``storage.update_state`` and stay pure JSON.
Concurrency: a dedicated application lock (``studio-apply.lock``) serializes
live applications.  Lock order is strictly apply-lock -> state-lock (brief
``update_state`` calls), never the reverse, so no deadlock is possible.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

# Flat modules: studio.py puts backend/ on sys.path (confirmed by Worker A).
import context  # type: ignore
import designs  # type: ignore
import noctalia_bundle  # type: ignore
import noctalia_presentation  # type: ignore
import process  # type: ignore
import storage  # type: ignore

VALIDATE_TIMEOUT = 120.0
USE_TIMEOUT = 60.0
_APPLY_LOCK = "studio-apply.lock"
_PACK_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_UUID32 = re.compile(r"^[0-9a-f]{32}$")
_CONTENT_ID = re.compile(r"^[0-9a-f]{64}$")
_MOTION = ("static", "candles")


class LibraryError(RuntimeError):
    """User-visible library failure; message is safe to show."""


# --------------------------------------------------------------------- helpers


def _check_id(value: Any, pattern: re.Pattern[str], what: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise LibraryError(f"invalid {what}")
    return value


def _find_variant(state: dict, variant_id: str) -> dict:
    """A variant by id, from the Kitty list or the target-specific Noctalia list."""
    for variant in state.get("variants", []):
        if isinstance(variant, dict) and variant.get("id") == variant_id:
            return variant
    namespace = state.get("noctalia") if isinstance(state.get("noctalia"), dict) else {}
    for variant in namespace.get("variants") or []:
        if isinstance(variant, dict) and variant.get("id") == variant_id:
            return variant
    raise LibraryError(f"unknown variant {variant_id}")


def _is_noctalia(variant: dict) -> bool:
    """Whether this variant belongs to the Noctalia target namespace."""
    return str(variant.get("target") or designs.DEFAULT_TARGET) == designs.NOCTALIA


def _variant_target(variant: dict) -> str:
    """The design target of a variant; a pre-feature variant is a frame.

    Raises ``LibraryError`` for a known-but-unavailable target, so a variant that
    could never be rendered here is refused before validate/install/apply touches
    anything.
    """
    try:
        return designs.require_target(variant.get("target") or designs.DEFAULT_TARGET)
    except ValueError as exc:
        raise LibraryError(str(exc)) from exc


def _decision_feedback(variant: dict, verdict: str, reason: str) -> dict:
    """The owner-decision record appended to the state feedback list."""
    return {
        "variant_id": variant["id"],
        "content_id": variant["content_id"],
        "target": _variant_target(variant),
        "verdict": verdict,
        "reason": reason,
        "created_at": storage.now(),
    }


def _terminal_skin() -> str:
    override = os.environ.get("WALLPAPER_STUDIO_TERMINAL_SKIN")
    found = override if override else shutil.which("terminal-skin")
    if not found:
        raise LibraryError("terminal-skin executable not found on PATH")
    return found


def _run_terminal_skin(args: list[str], timeout: float) -> str:
    try:
        completed = process.run(
            [_terminal_skin(), *args], timeout, cancel=lambda: False
        )
    except RuntimeError as exc:
        raise LibraryError(f"terminal-skin {args[0]} failed: {exc}") from exc
    return completed.stdout.strip()


def _skins_root() -> Path:
    root = os.environ.get("KITTY_SKINS_ROOT")
    if not root:
        config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.environ.get("HOME", ""), ".config"
        )
        root = os.path.join(config_home, "kitty-skins")
    return Path(root) / "skins"


def _inside(path: Path, root: Path) -> Path:
    real = Path(os.path.realpath(path))
    real_root = Path(os.path.realpath(root))
    if real == real_root or not real.is_relative_to(real_root):
        raise LibraryError(f"path escapes allowed area: {path}")
    return real


def _tree_digest(directory: Path) -> str:
    """Digest of the regular files under ``directory``, excluding tmp/ trees.

    Any symlink or non-regular entry anywhere in the tree is rejected: the
    same selection is used for installation, so the digest always describes
    exactly the bytes that would be copied.
    """
    import hashlib

    digest = hashlib.sha256()
    for base, dirs, names in os.walk(directory):
        pruned = []
        for name in sorted(dirs):
            if name == "tmp":
                continue
            entry = Path(base) / name
            if entry.is_symlink():
                raise LibraryError(f"pack contains a symlink: {entry}")
            pruned.append(name)
        dirs[:] = pruned
        for name in sorted(names):
            file = Path(base) / name
            if file.is_symlink() or not file.is_file():
                raise LibraryError(f"pack contains a symlink or non-regular entry: {file}")
            rel = file.relative_to(directory).as_posix()
            digest.update(rel.encode())
            digest.update(b"\0")
            with open(file, "rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(chunk)
            digest.update(b"\1")
    return digest.hexdigest()


def _check_pack(pack_path: Path, pack_id: str) -> None:
    """Cheap structural check; terminal-skin validate does the deep one."""
    manifest_path = pack_path / "skin.json"
    if not manifest_path.is_file():
        raise LibraryError("pack has no skin.json")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError) as exc:
        raise LibraryError(f"pack skin.json unreadable: {exc}") from exc
    if not isinstance(manifest, dict):
        raise LibraryError("pack skin.json is not an object")
    if manifest.get("id") != pack_id:
        raise LibraryError("pack manifest id does not match pack_id")
    source = manifest.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("width"), int) \
            or not isinstance(source.get("height"), int):
        raise LibraryError("pack skin.json has no source geometry")
    for key in ("exact", "adaptive"):
        mode = manifest.get(key)
        atlas = mode.get("atlas") if isinstance(mode, dict) else None
        if not isinstance(atlas, str) or not atlas:
            raise LibraryError(f"pack skin.json missing {key}.atlas")
        atlas_path = Path(os.path.realpath(pack_path / atlas))
        if not atlas_path.is_relative_to(Path(os.path.realpath(pack_path))) \
                or not atlas_path.is_file():
            raise LibraryError(f"pack {key} atlas missing")


def _observed_content(output: str, active: list) -> tuple[str | None, str]:
    """Freshly observed content id for the selected output or unique common one."""
    if not active:
        return None, "no active wallpaper outputs observed"
    if output:
        for entry in active:
            if entry.get("output") == output:
                if entry.get("status") != "observed":
                    return None, f"output {output} wallpaper context is {entry.get('status')}"
                content = entry.get("content_sha256")
                if not content:
                    return None, f"output {output} content is unknown"
                return content, ""
        return None, f"configured output {output} is not active"
    contents = {entry.get("content_sha256") for entry in active}
    if None in contents or len(contents) != 1:
        return None, "outputs show different or unknown wallpapers; selection is ambiguous"
    for entry in active:
        if entry.get("status") != "observed":
            return None, f"output {entry.get('output')} wallpaper context is {entry.get('status')}"
    return next(iter(contents)), ""


def _observe() -> dict:
    """Read-only fresh observation via the existing context helper."""
    return context.discover()


def _appearance_of(observed: dict) -> dict:
    return {
        "observed_at": storage.now(),
        "status": observed.get("status"),
        "active": [
            {
                "output": entry.get("output"),
                "content_sha256": entry.get("content_sha256"),
                "status": entry.get("status"),
            }
            for entry in observed.get("active", [])
            if isinstance(entry, dict)
        ],
        "palette": observed.get("palette"),
    }


def _current_pack_path() -> Path | None:
    """Resolve the natively reported pack ID against the skin store.

    ``terminal-skin current`` prints a pack ID, not a path; an unvalidated
    ID resolved against the cwd is never trusted.
    """
    try:
        reported = _run_terminal_skin(["current"], USE_TIMEOUT)
    except LibraryError:
        return None
    if not reported:
        return None
    pack_id = reported.splitlines()[-1].strip()
    if not _PACK_ID.fullmatch(pack_id):
        return None
    root = Path(os.path.realpath(_skins_root()))
    candidate = Path(os.path.realpath(root / pack_id))
    if not candidate.is_relative_to(root) or candidate == root or not candidate.is_dir():
        return None
    return candidate


def _with_apply_lock(action):
    """Run ``action`` while holding the dedicated application lock.

    One lock serializes manual apply, auto apply, approve and reject so a
    rejection can never race an application into applying a rejected
    variant.  Lock order is apply-lock -> state-lock only.
    """
    lock_path = storage.data_root() / _APPLY_LOCK
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return action()
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _fingerprint(preferences: dict, observed: dict, content_id: str | None,
                 variant_id: str | None, variant_state: str | None) -> str:
    """Memoize effective context, not observation timestamps."""
    import hashlib

    palette = observed.get("palette") or {}
    effective = {
        "preferences": preferences,
        "status": observed.get("status"),
        "palette": {key: palette.get(key) for key in
                    ("status", "source", "mode", "scheme", "roles", "wallpaper_source")},
        "active": observed.get("active"),
        "content_id": content_id,
        "variant_id": variant_id,
        "variant_state": variant_state,
    }
    digest = hashlib.sha256(json.dumps(effective, sort_keys=True).encode()).hexdigest()
    return f"[selection={digest}]"


# -------------------------------------------------------------------- mutate API


def add_candidate(
    item: dict,
    job: dict,
    generated: dict,
    preferences: dict,
    appearance: Any,
) -> dict:
    """Atomically append one complete generated pack as a candidate variant.

    ``item``/``job``/``generated`` are trusted-shape records produced upstream.
    The design target decides the shape: a Kitty pack keeps the historical
    ``frames/<content_id>/<job_id>`` cage and ``state["variants"]``, while a
    Noctalia recipe is validated and appended to the separate
    ``state["noctalia"]["variants"]`` namespace in the same record (a recipe
    instead of a pack).  The variant id equals the job id.  Inside the state
    mutation the live job is rechecked: it must still be running and not
    cancelled, otherwise ``process.Cancelled`` is raised and nothing is written.
    Re-adding a finished job's variant returns the existing one unchanged.
    """
    content_id = _check_id(item.get("id"), _CONTENT_ID, "item content id")
    job_id = _check_id(job.get("id"), _UUID32, "job id")
    # The design target is resolved before any target-specific path work: a
    # Noctalia recipe never has to look like a Kitty pack, and an unavailable
    # target (browser) is refused here, before anything becomes a candidate.
    try:
        target = designs.require_target(
            generated.get("target") or (job.get("payload") or {}).get("target")
        )
    except ValueError as exc:
        raise LibraryError(str(exc)) from exc
    if target == designs.NOCTALIA:
        return _add_noctalia_candidate(
            item, job, generated, preferences, appearance, content_id, job_id
        )
    pack_id = _check_id(generated.get("pack_id"), _PACK_ID, "pack id")
    pack_path = Path(str(generated.get("pack_path") or ""))
    preview_path = Path(str(generated.get("preview_path") or ""))
    if not pack_path.is_absolute() or not preview_path.is_absolute():
        raise LibraryError("generated paths must be absolute")
    cage = storage.generation_dir(content_id, job_id)
    # The pack is the job directory itself; strict containment is for its files.
    real_pack = pack_path.resolve()
    real_preview = _inside(preview_path, cage)
    if real_pack != cage.resolve():
        raise LibraryError("generated pack must occupy its exact job directory")
    if not real_pack.is_dir():
        raise LibraryError("generated pack directory is missing")
    _check_pack(real_pack, pack_id)
    if not real_preview.is_file():
        raise LibraryError("generated preview is missing")
    motion = generated.get("motion", "static")
    if motion not in _MOTION:
        raise LibraryError(f"unsupported motion mode {motion!r}")

    provenance = dict(generated.get("provenance") or {})
    provenance["job_id"] = job_id
    provenance["item"] = {
        "id": content_id,
        "title": item.get("title"),
        "kind": item.get("kind"),
        "path": item.get("path"),
    }

    variant = {
        "id": job_id,
        "content_id": content_id,
        "pack_id": pack_id,
        "name": str(generated.get("name") or pack_id),
        "state": "candidate",
        "target": target,
        "pack_path": str(real_pack),
        "preview_path": str(real_preview),
        "preview_kind": generated.get("preview_kind", "static-mockup"),
        "created_at": storage.now(),
        "preferences": dict(preferences),
        "appearance": appearance if isinstance(appearance, dict) else None,
        "motion": motion,
        "provenance": provenance,
        "installed_path": None,
    }

    def mutator(state: dict) -> dict:
        for existing in state["variants"]:
            if isinstance(existing, dict) and existing.get("id") == job_id:
                return existing  # idempotent: this job already has a variant
        job_entry = next(
            (j for j in state["jobs"] if isinstance(j, dict) and j.get("id") == job_id),
            None,
        )
        if not isinstance(job_entry, dict):
            raise LibraryError(f"job {job_id} is not in the studio record")
        if job_entry.get("cancel_requested") or job_entry.get("state") == "cancelled":
            raise process.Cancelled(f"job {job_id} was cancelled")
        if job_entry.get("state") != "running":
            raise LibraryError(f"job {job_id} is no longer running ({job_entry.get('state')})")
        state["variants"].append(variant)
        return variant

    return storage.update_state(mutator)


def _add_noctalia_candidate(
    item: dict,
    job: dict,
    generated: dict,
    preferences: dict,
    appearance: Any,
    content_id: str,
    job_id: str,
) -> dict:
    """Append one generated Noctalia recipe as a candidate variant.

    Same trusted-shape contract as the Kitty path, with a recipe instead of a
    pack: the recipe must validate, its ids must match the job/item, and a
    validated whole-skin ``presentation`` (one scene plus one image per actual
    surface) must be rooted in the job's cage with its scene equal to the recipe
    preview. The record lives in
    ``state["noctalia"]["variants"]``, so the Kitty variant list and its reset
    path stay untouched. Idempotent by job id and rechecked inside the state
    mutation (running, not cancelled).
    """
    try:
        recipe = noctalia_bundle.validate_recipe(generated.get("recipe"))
    except noctalia_bundle.NoctaliaError as exc:
        raise LibraryError(str(exc)) from exc
    if recipe["variant_id"] != job_id:
        raise LibraryError("noctalia recipe variant_id must equal the job id")
    if recipe["content_id"] != content_id:
        raise LibraryError("noctalia recipe content_id must equal the item content id")

    preview_path = Path(str(generated.get("preview_path") or ""))
    if not preview_path.is_absolute():
        raise LibraryError("noctalia preview path must be absolute")
    cage = storage.generation_dir(content_id, job_id)
    real_preview = _inside(preview_path, cage)
    if not real_preview.is_file():
        raise LibraryError("noctalia generated preview is missing")
    recipe["preview_path"] = str(real_preview)

    # A NEW generated Noctalia variant must carry a validated whole-skin
    # package: one scene plus exactly one image per actual visual surface, all
    # rooted in the job's own cage. The scene must be the recipe preview, so the
    # legacy ``preview_path`` stays the honest top image. The builtin seed is
    # deliberately not routed through here and remains an explicit single
    # preview.
    raw_presentation = generated.get("presentation")
    if raw_presentation is None:
        raise LibraryError("generated noctalia variant requires a validated presentation")
    try:
        presentation = noctalia_presentation.validate_presentation(
            raw_presentation,
            variant_id=job_id,
            content_id=content_id,
            root=cage,
            verify_files=True,
        )
    except noctalia_presentation.PresentationError as exc:
        raise LibraryError(f"invalid noctalia presentation: {exc}") from exc
    scene = Path(presentation["scene"]["preview_path"])
    if Path(os.path.realpath(scene)) != Path(os.path.realpath(real_preview)):
        raise LibraryError("presentation scene image must be the recipe preview")

    provenance = dict(generated.get("provenance") or {})
    provenance["job_id"] = job_id
    provenance["item"] = {
        "id": content_id,
        "title": item.get("title"),
        "kind": item.get("kind"),
        "path": item.get("path"),
    }
    parent = provenance.get("parent") if isinstance(provenance.get("parent"), dict) else {}
    parent_variant_id = generated.get("parent_variant_id") or parent.get("variant_id")

    variant = {
        "id": job_id,
        "content_id": content_id,
        "target": designs.NOCTALIA,
        "name": recipe["name"],
        "state": "candidate",
        "family": recipe["family"],
        "topology": recipe["topology"],
        "colors": dict(recipe["colors"]),
        "density": recipe["density"],
        "recipe": recipe,
        "presentation": presentation,
        "preview_path": str(real_preview),
        "preview_kind": str(generated.get("preview_kind") or "static-mockup"),
        "created_at": storage.now(),
        "preferences": dict(preferences),
        "appearance": appearance if isinstance(appearance, dict) else None,
        "builtin": False,
        "source": "generated",
        "installed_path": None,
        "batch_id": generated.get("batch_id"),
        "ordinal": generated.get("ordinal"),
        "count": generated.get("count"),
        "parent_variant_id": parent_variant_id if isinstance(parent_variant_id, str) else None,
        "provenance": provenance,
    }

    def mutator(state: dict) -> dict:
        namespace = state["noctalia"]
        for existing in namespace["variants"]:
            if isinstance(existing, dict) and existing.get("id") == job_id:
                return existing  # idempotent: this job already has a variant
        job_entry = next(
            (j for j in state["jobs"] if isinstance(j, dict) and j.get("id") == job_id),
            None,
        )
        if not isinstance(job_entry, dict):
            raise LibraryError(f"job {job_id} is not in the studio record")
        if job_entry.get("cancel_requested") or job_entry.get("state") == "cancelled":
            raise process.Cancelled(f"job {job_id} was cancelled")
        if job_entry.get("state") != "running":
            raise LibraryError(f"job {job_id} is no longer running ({job_entry.get('state')})")
        namespace["variants"].append(variant)
        return variant

    return storage.update_state(mutator)


def approve(variant_id: str, reason: Any = None) -> dict:
    """Validate the pack, install an immutable copy into the skin store, mark approved.

    Approval records the choice, the mapping and the owner's reason (if any) in
    one state mutation; it never applies the pack. Re-approving an already
    identical installation is idempotent; a different pack already occupying the
    id fails without touching the user's art. The reason is validated before any
    external work, so a bad reason has no side effects. Serialized with the other
    applicants via the dedicated application lock.
    """
    vid = _check_id(variant_id, _UUID32, "variant id")
    clean_reason = storage.validate_feedback_reason(reason)

    def _install() -> dict:
        state = storage.read_state()
        variant = _find_variant(state, vid)
        _variant_target(variant)  # refuse an unavailable target before any work
        if _is_noctalia(variant):
            return _approve_noctalia(state, vid, clean_reason)
        content_id = _check_id(variant.get("content_id"), _CONTENT_ID, "variant content id")
        pack_id = _check_id(variant.get("pack_id"), _PACK_ID, "pack id")
        # The exact variant/job directory this candidate came from, not any
        # frame under the content id.
        pack_path = Path(str(variant.get("pack_path"))).resolve()
        if not pack_path.is_dir():
            raise LibraryError("candidate pack directory no longer exists")
        if pack_path != storage.generation_dir(content_id, vid).resolve():
            raise LibraryError("candidate pack must occupy its exact job directory")
        source_digest = _tree_digest(pack_path)

        installed = Path(str(variant.get("installed_path"))) if variant.get("installed_path") else None
        if installed is not None:
            installed = _inside(installed, _skins_root())
            if installed.parent != _skins_root().resolve() or installed.name != pack_id:
                raise LibraryError("installed pack must be a direct child named after its pack id")

        def _matches_installed(target: Path) -> bool:
            return target.is_dir() and _tree_digest(target) == source_digest

        if not (installed and _matches_installed(_inside(installed, _skins_root()))):
            # External: validate the candidate, then atomically install a real copy.
            # The digest walk above rejects symlinks and non-regular entries.
            _run_terminal_skin(["validate", str(pack_path)], VALIDATE_TIMEOUT)
            skins = _skins_root()
            skins.mkdir(parents=True, exist_ok=True)
            dest = skins / pack_id
            if dest.exists():
                if _matches_installed(dest):
                    installed = dest
                else:
                    raise LibraryError(
                        f"a different pack already occupies skins/{pack_id}; not overwritten"
                    )
            else:
                staging = skins / f".staging-{pack_id}-{uuid.uuid4().hex[:8]}"
                try:
                    shutil.copytree(
                        pack_path, staging,
                        ignore=shutil.ignore_patterns("tmp"),
                    )
                    try:
                        os.rename(staging, dest)
                    except OSError:
                        if not _matches_installed(dest):
                            raise LibraryError(
                                f"a different pack already occupies skins/{pack_id}; not overwritten"
                            )
                    installed = dest
                finally:
                    if staging.exists():
                        shutil.rmtree(staging, ignore_errors=True)
            if installed is None:
                raise LibraryError("installation did not produce a pack directory")
            # External: verify what actually landed in the store.
            _run_terminal_skin(["validate", str(dest)], VALIDATE_TIMEOUT)
            installed = dest

        def mutator(state: dict) -> dict:
            variant = _find_variant(state, vid)
            variant["state"] = "approved"
            variant["installed_path"] = str(installed)
            state["mappings"][content_id] = vid
            state["sync_error"] = None  # a decision resets the auto-apply memo
            state["feedback"].append(_decision_feedback(variant, "approved", clean_reason))
            return variant

        return storage.update_state(mutator)

    return _with_apply_lock(_install)


def _require_fullpack_ready(variant: dict, observed: dict | None) -> None:
    """Refuse a full whole-skin action unless live coverage and runtime support.

    A legacy recipe-only variant (no stored presentation) is not a full pack and
    passes through unchanged.  A full pack requires a current live surface
    catalog and every one of its actual surfaces implemented by the runtime;
    concepts and a missing catalog are honest reasons to refuse, never a silent
    partial apply.
    """
    if variant.get("presentation") is None:
        return
    catalog = observed.get("surface_catalog") if isinstance(observed, dict) else None
    view = noctalia_presentation.presentation_view(variant["presentation"], catalog=catalog)
    if view["apply_ready"]:
        return
    raise LibraryError(
        "full-skin package is not applicable: "
        + (view["unavailable_reason"] or "full-skin coverage is not currently applicable")
    )


def _approve_noctalia(state: dict, variant_id: str, reason: str) -> dict:
    """Approve one Noctalia variant: gate on live capability, install, map.

    The native runtime is asked first: a family it does not implement is refused
    before any install or state mutation, so an unavailable direction can never
    be labelled applicable. A full whole-skin package additionally requires the
    live surface catalog to be current and every actual surface implemented.
    The install is the immutable backend copy; the mapping lives in the Noctalia
    namespace only, so the Kitty mapping for the same wallpaper is untouched.
    """
    variant = _find_variant(state, variant_id)
    family = variant.get("family")
    if not isinstance(family, str) or not family:
        raise LibraryError("noctalia variant has no family")
    observed = noctalia_bundle.capability()
    available, why = noctalia_bundle.family_available(observed, family)
    if not available:
        raise LibraryError(f"cannot approve: {why}")
    _require_fullpack_ready(variant, observed)
    try:
        installed = noctalia_bundle.install_variant(variant)
    except noctalia_bundle.NoctaliaError as exc:
        raise LibraryError(str(exc)) from exc
    content_id = _check_id(variant.get("content_id"), _CONTENT_ID, "variant content id")

    def mutator(state: dict) -> dict:
        namespace = state["noctalia"]
        current = _find_variant(state, variant_id)
        current["state"] = "approved"
        current["installed_path"] = str(installed)
        namespace["mappings"][content_id] = variant_id
        namespace["auto_error"] = None  # a decision resets the auto-apply memo
        state["sync_error"] = None
        state["feedback"].append(_decision_feedback(current, "approved", reason))
        return current

    return storage.update_state(mutator)


def reject(variant_id: str, reason: Any = None) -> dict:
    """Mark rejected; drop the mapping only if it points here. Files stay.

    The owner's reason (if any) is recorded in the same state mutation as the
    rejection. Shares the application lock with apply/auto-apply/approve so a
    rejection can never interleave with an application of the same variant.
    """
    vid = _check_id(variant_id, _UUID32, "variant id")
    clean_reason = storage.validate_feedback_reason(reason)

    def mutator(state: dict) -> dict:
        variant = _find_variant(state, vid)
        variant["state"] = "rejected"
        content_id = variant["content_id"]
        if _is_noctalia(variant):
            namespace = state["noctalia"]
            if namespace["mappings"].get(content_id) == vid:
                del namespace["mappings"][content_id]
            namespace["auto_error"] = None
        elif state["mappings"].get(content_id) == vid:
            del state["mappings"][content_id]
        state["sync_error"] = None  # a decision resets the auto-apply memo
        state["feedback"].append(_decision_feedback(variant, "rejected", clean_reason))
        return variant

    return _with_apply_lock(lambda: storage.update_state(mutator))


def apply(variant_id: str, expected_content_id: str | None = None) -> dict:
    """Explicitly apply an approved variant to the live desktop.

    Re-observes the current wallpaper immediately before use and fails on a
    stale/ambiguous/unknown context, so a stale job can never steal the current
    desktop.  Serialized with approve/reject/auto-apply through the dedicated
    application lock.  Activation is delegated to the active target's owner:
    ``terminal-skin use`` for a Kitty frame, the native ``skin-apply`` IPC for a
    Noctalia recipe; the Kitty palette requirement applies only to Kitty.
    """
    vid = _check_id(variant_id, _UUID32, "variant id")
    return _with_apply_lock(lambda: _apply_locked(vid, expected_content_id, auto=False))


def _apply_locked(variant_id: str, expected_content_id: str | None,
                  auto: bool = False) -> dict:
    state = storage.read_state()
    variant = _find_variant(state, variant_id)
    # A target this studio cannot render is refused before any observation or
    # terminal-skin dispatch, explicit and automatic alike.
    _variant_target(variant)
    if variant.get("state") != "approved":
        raise LibraryError("only approved variants can be applied")
    if _is_noctalia(variant):
        return _apply_noctalia(variant_id, expected_content_id, auto)
    content_id = _check_id(variant.get("content_id"), _CONTENT_ID, "variant content id")
    pack_id = _check_id(variant.get("pack_id"), _PACK_ID, "pack id")
    if expected_content_id is not None:
        _check_id(expected_content_id, _CONTENT_ID, "expected content id")
    installed_raw = variant.get("installed_path")
    if not installed_raw:
        raise LibraryError("variant has not been installed; approve it first")
    installed = _inside(Path(str(installed_raw)), _skins_root())
    skins_root = Path(os.path.realpath(_skins_root()))
    if (installed.parent != skins_root or installed.name != pack_id
            or not installed.is_dir()):
        raise LibraryError("installed pack is not a direct child of the skin store named after the pack id")

    output = str(state.get("preferences", {}).get("output") or "")

    observed = _observe()
    # Fresh context and palette: unknown/stale wallpaper or palette state must
    # refuse the application, explicit or automatic alike.
    if observed.get("status") != "observed":
        raise LibraryError(f"cannot apply: observed context is {observed.get('status')}")
    palette = observed.get("palette")
    palette_status = palette.get("status") if isinstance(palette, dict) else None
    if palette_status != "observed":
        raise LibraryError(f"cannot apply: palette context is {palette_status}")
    active = [e for e in observed.get("active", []) if isinstance(e, dict)]
    content, reason = _observed_content(output, active)
    if content is None:
        raise LibraryError(f"cannot apply: {reason}")
    if expected_content_id is not None and content != expected_content_id:
        raise LibraryError(
            f"cannot apply: observed content changed to {content[:12]}"
        )
    if content != content_id:
        raise LibraryError(
            f"cannot apply: current wallpaper content {content[:12]} does not match "
            f"variant content {content_id[:12]}"
        )

    # Discovery was slow; re-read the record immediately before dispatch and
    # reconfirm the decision this application was based on.
    state = storage.read_state()
    variant = _find_variant(state, variant_id)
    if variant.get("state") != "approved":
        raise LibraryError("cannot apply: variant approval changed during discovery")
    if variant.get("content_id") != content_id:
        raise LibraryError("cannot apply: variant content changed during discovery")
    installed_now = variant.get("installed_path")
    if not installed_now or _inside(Path(str(installed_now)), _skins_root()) != installed:
        raise LibraryError("cannot apply: installation changed during discovery")
    preferences = state.get("preferences", {})
    if str(preferences.get("output") or "") != output:
        raise LibraryError("cannot apply: output preference changed during discovery")
    if auto:
        # Preserve the explicit-versus-auto permission: auto application only
        # proceeds while it is still enabled and the approved mapping for the
        # observed content still points at this variant.
        if not preferences.get("auto_apply"):
            raise LibraryError("cannot apply: auto_apply was disabled during discovery")
        if state.get("mappings", {}).get(content_id) != variant_id:
            raise LibraryError("cannot apply: approved mapping changed during discovery")

    _run_terminal_skin(["use", pack_id], USE_TIMEOUT)

    def mutator(state: dict) -> dict:
        state["last_applied"] = {
            "variant_id": variant_id,
            "pack_id": pack_id,
            "content_id": content_id,
            "output": output or None,
            "at": storage.now(),
        }
        state["sync_error"] = None
        return state["last_applied"]

    last = storage.update_state(mutator)
    return {
        "ok": True,
        "variant_id": variant_id,
        "pack_id": pack_id,
        "content_id": content_id,
        "last_applied": last,
    }


def _noctalia_inside(path: Any, variant_id: str) -> Path:
    """The variant's installed directory, or a refusal.

    Only a real directory that is a direct child of the Noctalia store named
    after the variant id is accepted; a recorded path pointing anywhere else
    (or a symlink) is never trusted.
    """
    installed = _inside(Path(str(path)), noctalia_bundle.skins_root())
    root = Path(os.path.realpath(noctalia_bundle.skins_root()))
    if installed.parent != root or installed.name != variant_id or not installed.is_dir():
        raise LibraryError(
            "installed noctalia skin is not a direct child of the store named after the variant id"
        )
    return installed


def _apply_noctalia(variant_id: str, expected_content_id: str | None,
                    auto: bool = False) -> dict:
    """Apply one approved Noctalia variant through the native runtime.

    Freshly observes the live wallpaper and refuses a mismatch or unknown
    context before dispatching, re-reads the record immediately before the call,
    and re-checks the live native capability so an unimplemented family is never
    applied. Activation itself is delegated to the native ``skin-apply``; the
    backend records only what the runtime actually accepted.
    """
    state = storage.read_state()
    variant = _find_variant(state, variant_id)
    if variant.get("state") != "approved":
        raise LibraryError("cannot apply: noctalia variant is not approved")
    content_id = _check_id(variant.get("content_id"), _CONTENT_ID, "variant content id")
    family = variant.get("family")
    if not isinstance(family, str) or not family:
        raise LibraryError("noctalia variant has no family")
    if expected_content_id is not None:
        _check_id(expected_content_id, _CONTENT_ID, "expected content id")
    installed_raw = variant.get("installed_path")
    if not installed_raw:
        raise LibraryError("variant has not been installed; approve it first")
    installed = _noctalia_inside(installed_raw, variant_id)

    # The native capability is the runtime authority for the family: a direction
    # the runtime does not implement is refused before any config mutation.
    observed_capability = noctalia_bundle.capability()
    available, why = noctalia_bundle.family_available(observed_capability, family)
    if not available:
        raise LibraryError(f"cannot apply: {why}")
    _require_fullpack_ready(variant, observed_capability)

    output = str(state.get("preferences", {}).get("output") or "")

    observed = _observe()
    if observed.get("status") != "observed":
        raise LibraryError(f"cannot apply: observed context is {observed.get('status')}")
    active = [entry for entry in observed.get("active", []) if isinstance(entry, dict)]
    content, reason = _observed_content(output, active)
    if content is None:
        raise LibraryError(f"cannot apply: {reason}")
    if expected_content_id is not None and content != expected_content_id:
        raise LibraryError(f"cannot apply: observed content changed to {content[:12]}")
    if content != content_id:
        raise LibraryError(
            f"cannot apply: current wallpaper content {content[:12]} does not match "
            f"variant content {content_id[:12]}"
        )

    # Discovery was slow; re-read and reconfirm the decision this application
    # was based on before touching the live shell.
    state = storage.read_state()
    variant = _find_variant(state, variant_id)
    if variant.get("state") != "approved" or variant.get("content_id") != content_id:
        raise LibraryError("cannot apply: noctalia variant approval changed during discovery")
    installed_now = variant.get("installed_path")
    if not installed_now or _noctalia_inside(installed_now, variant_id) != installed:
        raise LibraryError("cannot apply: installation changed during discovery")
    preferences = state.get("preferences", {})
    if str(preferences.get("output") or "") != output:
        raise LibraryError("cannot apply: output preference changed during discovery")
    if auto:
        if not preferences.get("auto_apply"):
            raise LibraryError("cannot apply: auto_apply was disabled during discovery")
        if state["noctalia"]["mappings"].get(content_id) != variant_id:
            raise LibraryError("cannot apply: approved noctalia mapping changed during discovery")

    try:
        noctalia_bundle.apply_variant(variant)
    except noctalia_bundle.NoctaliaError as exc:
        raise LibraryError(str(exc)) from exc

    def mutator(state: dict) -> dict:
        namespace = state["noctalia"]
        namespace["last_applied"] = {
            "variant_id": variant_id,
            "content_id": content_id,
            "family": family,
            "at": storage.now(),
        }
        namespace["auto_error"] = None
        state["sync_error"] = None
        return namespace["last_applied"]

    last = storage.update_state(mutator)
    return {
        "ok": True,
        "variant_id": variant_id,
        "content_id": content_id,
        "family": family,
        "last_applied": last,
    }


def sync() -> dict:
    """Refresh observed appearance; opt-in approved-only auto application.

    Never runs models or catalog scans.  Blocked outcomes are stored in
    ``sync_error`` with an ephemeral-free fingerprint (context/palette
    status, content, output, auto_apply, approval state — never
    ``observed_at``), so the service does not retry-spam the same failing
    selection every 5 seconds while a changed situation retries anew.
    Approve/reject/apply decisions clear the memo.
    """
    observed = _observe()
    appearance = _appearance_of(observed)

    plan: dict = {}
    noctalia_plan: dict = {}

    def mutator(state: dict) -> dict:
        state["appearance"] = appearance
        preferences = state.get("preferences", {})
        output = str(preferences.get("output") or "")
        auto_apply = preferences.get("auto_apply")
        palette = appearance.get("palette")
        palette_status = palette.get("status") if isinstance(palette, dict) else None
        content, reason = _observed_content(output, appearance["active"])
        # Independent of the Kitty branch and its early returns: the Noctalia
        # namespace keeps its own memo, so neither target can clobber the other.
        _plan_noctalia(state, preferences, appearance, content, auto_apply, noctalia_plan)
        variant_state: str | None = None
        variant_id = state.get("mappings", {}).get(content) if content else None
        if isinstance(variant_id, str):
            variant = next(
                (v for v in state["variants"] if isinstance(v, dict) and v.get("id") == variant_id),
                None,
            )
            variant_state = variant.get("state") if isinstance(variant, dict) else None
        fingerprint = _fingerprint(
            preferences, appearance, content, variant_id, variant_state,
        )

        def _blocked(reason_text: str) -> dict:
            message = f"auto-apply: {reason_text} {fingerprint}"
            state["sync_error"] = message
            return {"applied": False, "blocked": message}

        if not auto_apply:
            plan.clear()
            state["sync_error"] = None
            return {"applied": False, "skipped": "auto_apply is disabled"}
        if content is None:
            plan.clear()
            return _blocked(reason)
        if not variant_id:
            plan.clear()
            return _blocked("no approved mapping for current content")
        if variant_state != "approved":
            plan.clear()
            return _blocked("mapping is not approved")
        variant = next(
            (v for v in state["variants"] if isinstance(v, dict) and v.get("id") == variant_id),
            None,
        )
        plan.update(
            {
                "variant_id": variant_id,
                "pack_id": variant.get("pack_id"),
                "installed_path": variant.get("installed_path"),
                "content_id": content,
                "output": output,
                "last_applied": state.get("last_applied"),
                "sync_error": state.get("sync_error"),
                "fingerprint": fingerprint,
            }
        )
        return {"applied": None}  # decision finished outside the lock

    outcome = storage.update_state(mutator)
    if plan:
        outcome = _auto_apply(plan)
    if noctalia_plan:
        noctalia_outcome = _noctalia_auto_apply(noctalia_plan)
        if not plan:
            outcome = noctalia_outcome
    return {"appearance": appearance, **outcome}


def _plan_noctalia(state: dict, preferences: dict, appearance: dict,
                   content: str | None, auto_apply: Any, plan: dict) -> None:
    """Decide the opt-in Noctalia auto-apply target inside the state lock.

    Mirrors the Kitty branch but keeps its own memo (``noctalia.auto_error``), so
    a failing Noctalia selection never overwrites — and is never overwritten by —
    the Kitty ``sync_error`` slot. No plan is produced without an explicit
    ``auto_apply`` and an approved mapping for the observed wallpaper.
    """
    plan.clear()
    namespace = state.get("noctalia")
    if not isinstance(namespace, dict):
        return
    mappings = namespace.get("mappings") if isinstance(namespace.get("mappings"), dict) else {}
    variant_id = mappings.get(content) if content else None
    if not isinstance(variant_id, str):
        namespace["auto_error"] = None
        return
    variant = next(
        (v for v in namespace.get("variants", []) if isinstance(v, dict) and v.get("id") == variant_id),
        None,
    )
    variant_state = variant.get("state") if isinstance(variant, dict) else None
    fingerprint = _fingerprint(preferences, appearance, content, variant_id, variant_state)
    if not auto_apply or variant_state != "approved":
        namespace["auto_error"] = None
        return
    saved = namespace.get("auto_error")
    if (isinstance(saved, str) and saved.startswith("auto-apply:") and fingerprint
            and saved.endswith(fingerprint)):
        return  # unchanged failing selection; do not retry every 5 seconds
    plan.update(
        {
            "variant_id": variant_id,
            "content_id": content,
            "last_applied": namespace.get("last_applied"),
            "fingerprint": fingerprint,
        }
    )


def _noctalia_auto_apply(plan: dict) -> dict:
    variant_id = _check_id(plan["variant_id"], _UUID32, "variant id")
    content_id = _check_id(plan["content_id"], _CONTENT_ID, "content id")
    fingerprint = plan.get("fingerprint", "")
    last = plan.get("last_applied") or {}
    if (isinstance(last, dict) and last.get("variant_id") == variant_id
            and last.get("content_id") == content_id):
        return {"applied": False, "skipped": "noctalia skin already applied for this content"}
    try:
        return _with_apply_lock(
            lambda: {"applied": True, **_apply_locked(variant_id, content_id, auto=True)}
        )
    except LibraryError as exc:
        message = f"auto-apply: {exc} {fingerprint}".strip()

        def mutator(state: dict) -> None:
            state["noctalia"]["auto_error"] = message

        storage.update_state(mutator)
        return {"applied": False, "error": message}


def _auto_apply(plan: dict) -> dict:
    variant_id = _check_id(plan["variant_id"], _UUID32, "variant id")
    pack_id = _check_id(plan["pack_id"], _PACK_ID, "pack id")
    content_id = _check_id(plan["content_id"], _CONTENT_ID, "content id")
    output = plan["output"]
    fingerprint = plan.get("fingerprint", "")

    # Observe the live pack so an old saved success is not mistaken for proof
    # after the user manually switched packs.
    current = _current_pack_path()
    last = plan.get("last_applied") or {}
    if (
        isinstance(last, dict)
        and last.get("variant_id") == variant_id
        and last.get("content_id") == content_id
        and current is not None
        and current == Path(os.path.realpath(str(plan.get("installed_path") or current)))
    ):
        storage.update_state(lambda state: state.update(sync_error=None))
        return {"applied": False, "skipped": f"{pack_id} already active for this content"}

    saved = plan.get("sync_error")
    if isinstance(saved, str) and saved.startswith("auto-apply:") and fingerprint \
            and saved.endswith(fingerprint):
        # The failing situation is unchanged by the ephemeral-free fingerprint;
        # do not retry it every 5 seconds.
        return {"applied": False, "skipped": "unchanged failing selection", "error": saved}

    try:
        return _with_apply_lock(lambda: {"applied": True, **_apply_locked(variant_id, content_id, auto=True)})
    except LibraryError as exc:
        message = f"auto-apply: {exc} {fingerprint}"

        def mutator(state: dict) -> None:
            state["sync_error"] = message

        storage.update_state(mutator)
        return {"applied": False, "error": message}
