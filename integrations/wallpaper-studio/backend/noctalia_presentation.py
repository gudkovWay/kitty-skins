#!/usr/bin/env python3
"""Whole-skin presentation contract: actual surface catalog, image records,
coverage view and immutable package copy.

This module is the frozen, dependency-free half of the whole-skin feature: the
only import is the Python standard library, and it deliberately never imports
``noctalia_bundle``, ``library`` or ``studio`` (the bundle imports *this* file,
so the reverse would be a cycle).  It owns exactly:

* ``normalize_catalog`` — the strict bounded surface catalog the native runtime
  projects (visual surfaces plus non-visual services/shortcuts/launcher
  providers), with deterministic sorting and no room for unknown fields;
* ``catalog_digest`` — the canonical SHA-256 that binds a generated package to
  the exact inventory it was generated against;
* ``validate_presentation`` — strict metadata validation of one whole-skin
  package: one scene image plus exactly one image per actual visual surface, each
  with an absolute regular non-symlink raster path and a matching digest;
* ``presentation_view`` — the pure, filesystem-free readiness projection the UI
  reads (full coverage, current inventory, honest runtime applicability);
* ``copy_presentation`` — the immutable package copy used by install and by
  surface refinement, which rebinds paths without ever overwriting differing
  files;
* ``image_call_count`` — the single source of the paid-call quote.

File reads/hashes happen only when ``verify_files=True`` is requested; the pure
view never touches the filesystem or IPC.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any


class PresentationError(ValueError):
    """User-visible whole-skin presentation failure; message is safe to show."""


SCHEMA_VERSION = 1

#: Visual surface kinds the native runtime projects.  ``scene`` is deliberately
#: absent: the scene is the host's synthetic composition of the whole desktop,
#: never a fake plugin surface.
SURFACE_KINDS: tuple[str, ...] = (
    "bar",
    "bar-widget",
    "desktop-widget",
    "lockscreen-widget",
    "panel",
    "plugin-widget",
    "plugin-desktop-widget",
)
#: Non-visual entries carry metadata only and never get an image.
NONVISUAL_KINDS: tuple[str, ...] = ("service", "shortcut", "launcher-provider")
RUNTIME_SUPPORT: tuple[str, ...] = ("implemented", "preserved", "concept-only")
SCOPES: tuple[str, ...] = ("whole", "surface")

#: The native ID scheme; an id must carry its own kind's prefix.
_ID_PREFIX: dict[str, str] = {
    "bar": "bar:",
    "bar-widget": "bar-widget:",
    "desktop-widget": "desktop-widget:",
    "lockscreen-widget": "lockscreen-widget:",
    "panel": "panel:",
    "plugin-widget": "plugin-widget:",
    "plugin-desktop-widget": "plugin-desktop-widget:",
}

SURFACE_KEYS = (
    "id",
    "name",
    "kind",
    "plugin_id",
    "entry_id",
    "type",
    "persistent",
    "runtime_support",
)
NONVISUAL_KEYS = ("id", "name", "kind", "plugin_id", "entry_id")
CATALOG_KEYS = ("schema_version", "surfaces", "nonvisual")

IMAGE_KEYS = ("surface_id", "name", "kind", "preview_path", "sha256", "preview_kind", "provenance")
PRESENTATION_KEYS = (
    "schema_version",
    "variant_id",
    "content_id",
    "inventory_digest",
    "catalog",
    "scene",
    "surfaces",
    "refinement",
)
REFINEMENT_KEYS = ("scope", "surface_id", "parent_variant_id")

RASTER_EXTENSIONS: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".webp")
PREVIEW_KIND = "concept-mockup"

MAX_SURFACES = 128
MAX_NONVISUAL = 256
MAX_STR_BYTES = 256
MAX_NAME_BYTES = 512
MAX_PATH_CHARS = 4096
MAX_PROVENANCE_BYTES = 65536

LEGACY_REASON = "Legacy single preview; full widget coverage not generated"

_VARIANT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_CONTENT_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"


# ------------------------------------------------------------------ primitives


def _fail(message: str) -> None:
    raise PresentationError(message)


def _bounded_str(value: Any, field: str, *, limit: int, allow_empty: bool = False) -> str:
    """A UTF-8 bounded string without control characters."""
    if not isinstance(value, str):
        _fail(f"{field} must be a string")
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError:
        _fail(f"{field} must contain valid UTF-8 text")
    if not allow_empty and not raw:
        _fail(f"{field} must not be empty")
    if len(raw) > limit:
        _fail(f"{field} must be at most {limit} UTF-8 bytes")
    if any(byte < 0x20 or byte == 0x7F for byte in raw):
        _fail(f"{field} must not contain control characters")
    return value


def _hex_id(value: Any, length: int, field: str) -> str:
    pattern = _VARIANT_ID_RE if length == 32 else _CONTENT_ID_RE
    if not isinstance(value, str) or not pattern.fullmatch(value):
        _fail(f"{field} must be {length} lowercase hex characters")
    return value


def _enum(value: Any, allowed: tuple[str, ...], field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        _fail(f"{field} must be one of: " + ", ".join(allowed))
    return value


def _plain_object(value: Any, field: str) -> dict:
    if not isinstance(value, dict):
        _fail(f"{field} must be a JSON object")
    return value


def _exact_keys(value: dict, keys: tuple[str, ...], field: str) -> None:
    unknown = sorted(set(value) - set(keys))
    if unknown:
        _fail(f"{field} has unknown fields: " + ", ".join(unknown))
    missing = sorted(set(keys) - set(value))
    if missing:
        _fail(f"{field} is missing fields: " + ", ".join(missing))


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _raster_kind(path: Path) -> str | None:
    """The raster family of a file from its magic bytes, or ``None``."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(16)
    except OSError:
        return None
    if head.startswith(_PNG_MAGIC):
        return "png"
    if head.startswith(_JPEG_MAGIC):
        return "jpeg"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


# ------------------------------------------------------------------- catalog


def _normalize_surface(value: Any, field: str) -> dict:
    surface = _plain_object(value, field)
    _exact_keys(surface, SURFACE_KEYS, field)
    kind = _enum(surface["kind"], SURFACE_KINDS, f"{field}.kind")
    ident = _bounded_str(surface["id"], f"{field}.id", limit=MAX_STR_BYTES)
    prefix = _ID_PREFIX[kind]
    if not ident.startswith(prefix) or len(ident) == len(prefix):
        _fail(f"{field}.id must start with {prefix!r} for kind {kind!r}")
    _bounded_str(surface["name"], f"{field}.name", limit=MAX_NAME_BYTES)
    _bounded_str(surface["plugin_id"], f"{field}.plugin_id", limit=MAX_STR_BYTES, allow_empty=True)
    _bounded_str(surface["entry_id"], f"{field}.entry_id", limit=MAX_STR_BYTES, allow_empty=True)
    _bounded_str(surface["type"], f"{field}.type", limit=MAX_STR_BYTES, allow_empty=True)
    persistent = surface["persistent"]
    if not isinstance(persistent, bool):
        _fail(f"{field}.persistent must be a boolean")
    runtime_support = _enum(surface["runtime_support"], RUNTIME_SUPPORT, f"{field}.runtime_support")
    return {
        "id": surface["id"],
        "name": surface["name"],
        "kind": kind,
        "plugin_id": surface["plugin_id"],
        "entry_id": surface["entry_id"],
        "type": surface["type"],
        "persistent": persistent,
        "runtime_support": runtime_support,
    }


def _normalize_nonvisual(value: Any, field: str) -> dict:
    entry = _plain_object(value, field)
    _exact_keys(entry, NONVISUAL_KEYS, field)
    kind = _enum(entry["kind"], NONVISUAL_KINDS, f"{field}.kind")
    _bounded_str(entry["id"], f"{field}.id", limit=MAX_STR_BYTES)
    _bounded_str(entry["name"], f"{field}.name", limit=MAX_NAME_BYTES)
    _bounded_str(entry["plugin_id"], f"{field}.plugin_id", limit=MAX_STR_BYTES, allow_empty=True)
    _bounded_str(entry["entry_id"], f"{field}.entry_id", limit=MAX_STR_BYTES, allow_empty=True)
    return {
        "id": entry["id"],
        "name": entry["name"],
        "kind": kind,
        "plugin_id": entry["plugin_id"],
        "entry_id": entry["entry_id"],
    }


def normalize_catalog(value) -> dict:
    """Strictly validate one surface catalog and return a normalized copy.

    Exactly ``1..128`` visual surfaces and at most ``256`` non-visual entries;
    every id unique across both lists; every string bounded and free of control
    characters; ``persistent`` a literal boolean and ``schema_version`` a literal
    ``1`` (a boolean integer is refused).  Lists are sorted by id, so the
    normalized object is deterministic for hashing and display.
    """
    catalog = _plain_object(value, "catalog")
    _exact_keys(catalog, CATALOG_KEYS, "catalog")
    version = catalog["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        _fail(f"catalog schema_version must be the literal {SCHEMA_VERSION}")
    raw_surfaces = catalog["surfaces"]
    raw_nonvisual = catalog["nonvisual"]
    if not isinstance(raw_surfaces, list):
        _fail("catalog.surfaces must be a list")
    if not isinstance(raw_nonvisual, list):
        _fail("catalog.nonvisual must be a list")
    if not 1 <= len(raw_surfaces) <= MAX_SURFACES:
        _fail(f"catalog.surfaces must hold 1..{MAX_SURFACES} entries")
    if len(raw_nonvisual) > MAX_NONVISUAL:
        _fail(f"catalog.nonvisual must hold at most {MAX_NONVISUAL} entries")

    surfaces = [
        _normalize_surface(item, f"catalog.surfaces[{index}]")
        for index, item in enumerate(raw_surfaces)
    ]
    nonvisual = [
        _normalize_nonvisual(item, f"catalog.nonvisual[{index}]")
        for index, item in enumerate(raw_nonvisual)
    ]
    seen: set[str] = set()
    for entry in surfaces:
        if entry["id"] in seen:
            _fail(f"catalog has a duplicate id {entry['id']!r}")
        seen.add(entry["id"])
    for entry in nonvisual:
        if entry["id"] in seen:
            _fail(f"catalog has a duplicate id {entry['id']!r}")
        seen.add(entry["id"])

    surfaces.sort(key=lambda entry: entry["id"])
    nonvisual.sort(key=lambda entry: entry["id"])
    return {
        "schema_version": SCHEMA_VERSION,
        "surfaces": surfaces,
        "nonvisual": nonvisual,
    }


def catalog_digest(catalog) -> str:
    """Canonical SHA-256 of a normalized catalog (deterministic JSON)."""
    normalized = normalize_catalog(catalog)
    return hashlib.sha256(_canonical_bytes(normalized)).hexdigest()


# --------------------------------------------------------------- presentation


def _image_record(value: Any, field: str, *, scene: bool) -> dict:
    record = _plain_object(value, field)
    _exact_keys(record, IMAGE_KEYS, field)
    surface_id = _bounded_str(record["surface_id"], f"{field}.surface_id", limit=MAX_STR_BYTES)
    _bounded_str(record["name"], f"{field}.name", limit=MAX_NAME_BYTES)
    if scene:
        if surface_id != "scene" or record["kind"] != "scene":
            _fail(f"{field} must be the scene image (surface_id/kind 'scene')")
        kind = "scene"
    else:
        kind = _enum(record["kind"], SURFACE_KINDS, f"{field}.kind")
    preview_path = record["preview_path"]
    if not isinstance(preview_path, str) or not preview_path:
        _fail(f"{field}.preview_path must be a non-empty string")
    if len(preview_path) > MAX_PATH_CHARS:
        _fail(f"{field}.preview_path is too long")
    if not os.path.isabs(preview_path):
        _fail(f"{field}.preview_path must be absolute")
    if "://" in preview_path or any(ord(ch) < 0x20 for ch in preview_path):
        _fail(f"{field}.preview_path must be a plain local path")
    if Path(preview_path).suffix.lower() not in RASTER_EXTENSIONS:
        _fail(f"{field}.preview_path must be a png, jpg, jpeg or webp image")
    if not isinstance(record["sha256"], str) or not _SHA256_RE.fullmatch(record["sha256"]):
        _fail(f"{field}.sha256 must be 64 lowercase hex characters")
    if record["preview_kind"] != PREVIEW_KIND:
        _fail(f"{field}.preview_kind must be {PREVIEW_KIND!r}")
    provenance = _plain_object(record["provenance"], f"{field}.provenance")
    try:
        serialized = _canonical_bytes(provenance)
    except (TypeError, ValueError) as exc:
        _fail(f"{field}.provenance must be a JSON-serializable object: {exc}")
    if len(serialized) > MAX_PROVENANCE_BYTES:
        _fail(f"{field}.provenance exceeds {MAX_PROVENANCE_BYTES} bytes")
    return {
        "surface_id": surface_id,
        "name": record["name"],
        "kind": kind,
        "preview_path": preview_path,
        "sha256": record["sha256"],
        "preview_kind": PREVIEW_KIND,
        "provenance": provenance,
    }


def _check_root(path: str, root: Path) -> None:
    real = Path(os.path.realpath(path))
    real_root = Path(os.path.realpath(root))
    if real == real_root or not real.is_relative_to(real_root):
        _fail(f"image path escapes the allowed root: {path}")


def _verify_file(path: str, sha256: str, field: str) -> None:
    target = Path(path)
    try:
        os.lstat(target)
    except OSError as exc:
        _fail(f"{field} image is missing: {exc}")
    if os.path.islink(target) or not os.path.isfile(target):
        _fail(f"{field} must be a regular non-symlink file")
    detected = _raster_kind(target)
    if detected is None:
        _fail(f"{field} is not a supported png/jpg/jpeg/webp image")
    suffix = target.suffix.lower()
    expected = "jpeg" if suffix in (".jpg", ".jpeg") else suffix.lstrip(".")
    if detected != expected:
        _fail(f"{field} extension {suffix!r} does not match its image magic ({detected})")
    if _sha256_file(target) != sha256:
        _fail(f"{field} sha256 does not match the file on disk")


def _refinement(value: Any, surfaces_by_id: dict[str, dict]) -> dict:
    refinement = _plain_object(value, "refinement")
    _exact_keys(refinement, REFINEMENT_KEYS, "refinement")
    scope = _enum(refinement["scope"], SCOPES, "refinement.scope")
    surface_id = refinement["surface_id"]
    parent = refinement["parent_variant_id"]
    if surface_id is not None:
        _bounded_str(surface_id, "refinement.surface_id", limit=MAX_STR_BYTES)
    if parent is not None:
        _hex_id(parent, 32, "refinement.parent_variant_id")
    if scope == "whole":
        if surface_id is not None:
            _fail("a whole refinement must not select a surface")
    else:
        if surface_id is None or surface_id not in surfaces_by_id:
            _fail("a surface refinement must select a catalog surface id")
        if parent is None:
            _fail("a surface refinement must name its parent variant")
    return {"scope": scope, "surface_id": surface_id, "parent_variant_id": parent}


def validate_presentation(value, *, variant_id=None, content_id=None, root=None,
                          verify_files: bool = False) -> dict:
    """Strictly validate one whole-skin package and return a normalized copy.

    Exactly one scene image plus exactly one image for every catalog visual
    surface (no fake, missing or duplicate previews).  Paths must be absolute
    rasters with matching digests; ``root`` confines every image to one package
    directory, and ``verify_files`` additionally reads and hashes the files.
    Metadata validation performs no file reads unless ``verify_files`` is set.
    """
    presentation = _plain_object(value, "presentation")
    _exact_keys(presentation, PRESENTATION_KEYS, "presentation")
    version = presentation["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        _fail(f"presentation schema_version must be the literal {SCHEMA_VERSION}")
    stored_variant = _hex_id(presentation["variant_id"], 32, "presentation.variant_id")
    stored_content = _hex_id(presentation["content_id"], 64, "presentation.content_id")
    inventory = _hex_id(presentation["inventory_digest"], 64, "presentation.inventory_digest")
    if variant_id is not None and _hex_id(variant_id, 32, "variant_id") != stored_variant:
        _fail("presentation variant_id does not match the requested variant")
    if content_id is not None and _hex_id(content_id, 64, "content_id") != stored_content:
        _fail("presentation content_id does not match the requested content")

    catalog = normalize_catalog(presentation["catalog"])
    if catalog_digest(catalog) != inventory:
        _fail("presentation inventory_digest does not match its catalog")
    surfaces_by_id = {surface["id"]: surface for surface in catalog["surfaces"]}

    scene = _image_record(presentation["scene"], "presentation.scene", scene=True)
    raw_surfaces = presentation["surfaces"]
    if not isinstance(raw_surfaces, list):
        _fail("presentation.surfaces must be a list")
    records: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_surfaces):
        record = _image_record(raw, f"presentation.surfaces[{index}]", scene=False)
        surface_id = record["surface_id"]
        if surface_id in seen:
            _fail(f"presentation covers surface {surface_id!r} more than once")
        match = surfaces_by_id.get(surface_id)
        if match is None:
            _fail(f"presentation covers unknown surface {surface_id!r}")
        if record["name"] != match["name"] or record["kind"] != match["kind"]:
            _fail(f"presentation image for {surface_id!r} does not match its catalog surface")
        seen.add(surface_id)
        records.append(record)
    missing = sorted(set(surfaces_by_id) - seen)
    if missing:
        _fail("presentation is missing images for: " + ", ".join(missing))

    if root is not None:
        real_root = Path(root)
        _check_root(scene["preview_path"], real_root)
        for record in records:
            _check_root(record["preview_path"], real_root)
    if verify_files:
        _verify_file(scene["preview_path"], scene["sha256"], "scene")
        for record in records:
            _verify_file(record["preview_path"], record["sha256"], f"surface {record['surface_id']!r}")

    refinement = _refinement(presentation["refinement"], surfaces_by_id)
    return {
        "schema_version": SCHEMA_VERSION,
        "variant_id": stored_variant,
        "content_id": stored_content,
        "inventory_digest": inventory,
        "catalog": catalog,
        "scene": scene,
        "surfaces": records,
        "refinement": refinement,
    }


def presentation_view(value, *, catalog=None) -> dict:
    """Pure readiness projection of one stored presentation, never I/O.

    A missing presentation is the honest legacy state (one single preview, no
    full coverage); a malformed one is an incomplete/error view, never a crash.
    When a live catalog is supplied it is compared by digest and by surface ids,
    and per-surface ``runtime_support`` comes from that current catalog with a
    conservative ``concept-only`` fallback.
    """
    base: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "complete": False,
        "coverage_current": False,
        "legacy": False,
        "scene": None,
        "surfaces": [],
        "nonvisual": [],
        "inventory_digest": "",
        "apply_ready": False,
        "unavailable_reason": "",
        "runtime_verified": False,
    }
    if value is None:
        base["legacy"] = True
        base["unavailable_reason"] = LEGACY_REASON
        return base
    try:
        presentation = validate_presentation(value)
    except PresentationError as exc:
        base["unavailable_reason"] = str(exc) or "presentation is malformed"
        return base

    base["complete"] = True
    base["scene"] = presentation["scene"]
    base["inventory_digest"] = presentation["inventory_digest"]

    current: dict | None = None
    catalog_error = ""
    if catalog is not None:
        try:
            current = normalize_catalog(catalog)
        except PresentationError as exc:
            catalog_error = str(exc)
    if current is None:
        coverage = False
        base["coverage_current"] = False
    else:
        base["nonvisual"] = [dict(entry) for entry in current["nonvisual"]]
        same_ids = (
            {surface["id"] for surface in current["surfaces"]}
            == {record["surface_id"] for record in presentation["surfaces"]}
        )
        coverage = catalog_digest(current) == presentation["inventory_digest"] and same_ids
        base["coverage_current"] = coverage

    runtime_by_id = (
        {surface["id"]: surface["runtime_support"] for surface in current["surfaces"]}
        if current is not None else {}
    )
    surfaces: list[dict] = []
    for record in presentation["surfaces"]:
        item = dict(record)
        item["runtime_support"] = runtime_by_id.get(record["surface_id"], "concept-only")
        surfaces.append(item)
    base["surfaces"] = surfaces

    if not coverage:
        reason = catalog_error or (
            "live surface catalog is unavailable" if catalog is None
            else "surface catalog changed since this package was generated"
        )
    elif not surfaces:
        reason = "presentation covers no visual surfaces"
    else:
        concepts = [item["surface_id"] for item in surfaces if item["runtime_support"] != "implemented"]
        if concepts:
            reason = "runtime does not implement: " + ", ".join(concepts)
        else:
            reason = ""
            base["apply_ready"] = True
    if not base["apply_ready"]:
        base["unavailable_reason"] = reason or "full-skin coverage is not currently applicable"
    return base


# --------------------------------------------------------------- package copy


def _target_path(record: dict, directory: Path, stem: str) -> Path:
    return directory / f"{stem}{Path(record['preview_path']).suffix.lower()}"


def _copy_image(record: dict, target: Path) -> None:
    """Copy one verified source image, refusing a differing pre-existing file."""
    source = Path(record["preview_path"])
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file():
            _fail(f"destination image {target} is not a regular file")
        if _sha256_file(target) != record["sha256"]:
            _fail(f"destination image {target} already exists with different content")
        return
    shutil.copyfile(source, target)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        with open(temporary, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _owned_directory(path: Path, field: str) -> Path:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_dir():
            _fail(f"{field} {path} is not a regular directory")
    return path


def copy_presentation(value, destination, *, variant_id=None, content_id=None) -> dict:
    """Copy a validated whole package into ``destination`` and return it.

    The scene and every surface image land in ``destination/presentation/`` as
    ``<scene|index>.<extension>``; paths are rewritten, per-image provenance and
    digests are preserved, and the top variant/content ids may be rebound.  A
    differing file already present at a destination path is refused, so a
    package is never silently overwritten; an identical package is idempotent.
    """
    source = validate_presentation(value, verify_files=True)
    dest = Path(destination)
    if not dest.is_absolute():
        _fail("destination must be absolute")
    _owned_directory(dest, "destination")
    dest.mkdir(parents=True, exist_ok=True)
    presentation_dir = _owned_directory(dest / "presentation", "presentation directory")
    presentation_dir.mkdir(parents=True, exist_ok=True)

    scene_target = _target_path(source["scene"], presentation_dir, "scene")
    surface_targets = [
        _target_path(record, presentation_dir, str(index))
        for index, record in enumerate(source["surfaces"])
    ]
    result = dict(source)
    result["scene"] = {**source["scene"], "preview_path": str(scene_target)}
    result["surfaces"] = [
        {**record, "preview_path": str(target)}
        for record, target in zip(source["surfaces"], surface_targets)
    ]
    if variant_id is not None:
        result["variant_id"] = _hex_id(variant_id, 32, "variant_id")
    if content_id is not None:
        result["content_id"] = _hex_id(content_id, 64, "content_id")

    manifest = dest / "presentation.json"
    payload = _canonical_bytes(result) + b"\n"
    if manifest.exists() or manifest.is_symlink():
        if manifest.is_symlink() or not manifest.is_file():
            _fail(f"destination manifest {manifest} is not a regular file")
        if manifest.read_bytes() != payload:
            _fail("destination presentation.json already exists with different content")
        # Identical manifest: the images must already be the same bytes.
        return validate_presentation(result, root=dest, verify_files=True)

    _copy_image(source["scene"], scene_target)
    for record, target in zip(source["surfaces"], surface_targets):
        _copy_image(record, target)
    checked = validate_presentation(result, root=dest, verify_files=True)
    _atomic_write_bytes(manifest, payload)
    return checked


# ------------------------------------------------------------------- quoting


def image_call_count(catalog, scope: str = "whole", surface_id=None) -> int:
    """The exact number of paid image calls one generation or refinement costs.

    Whole generation renders the scene plus one image per actual visual surface;
    a surface refinement costs exactly two (the selected surface and a refreshed
    scene).  A selected refinement must name a real catalog surface.
    """
    normalized = normalize_catalog(catalog)
    if scope not in SCOPES:
        _fail("scope must be one of: " + ", ".join(SCOPES))
    surface_ids = [surface["id"] for surface in normalized["surfaces"]]
    if scope == "whole":
        return 1 + len(surface_ids)
    if not isinstance(surface_id, str) or surface_id not in surface_ids:
        _fail("a selected refinement requires a valid catalog surface id")
    return 2
