#!/usr/bin/env python3
"""Noctalia skin catalog: strict Recipe v1, builtin seeding and native IPC.

This is the target-specific half of the studio for the ``noctalia`` design
target.  It owns exactly these things:

* the frozen **Recipe v1** validator: a closed 10-key JSON object is data for the
  native skin runtime, never executable code (no commands, no URLs, no shaders);
* the immutable install store ``$XDG_DATA_HOME/noctalia/skins/<variant_id>/``
  (a validated ``recipe.json`` beside its preview copy), owned by the backend
  alone — the native runtime receives the recipe body as one argv argument and
  never reads this directory;
* builtin seeding: per-wallpaper candidate variants, one per skin family, from
  product recipes/previews resolved by :func:`source_root` — an explicit
  ``$NOCTALIA_SKIN_SOURCE_ROOT`` override, else the checkout's
  ``integrations/noctalia-theme/skins`` while it still carries a real catalog,
  else the durable installed catalog under
  ``$XDG_DATA_HOME/wallpaper-studio/resources/noctalia-skins``.  Seeding is
  append-only and deterministic: a variant id is derived from the content id and
  family, so the same wallpaper always yields the same stable ids;
* the narrow native bridge: ``noctalia msg skin-status`` / ``skin-apply``, run as
  an argv table from ``$NOCTALIA_SKIN_BINARY`` (else the durable installed
  runtime record, else the plain ``noctalia`` command), stdout parsed as bounded
  JSON.

Candidate and mapping state lives in ``state["noctalia"]`` (see ``storage``), so
a Noctalia apply can never rewrite the Kitty ``mappings``/``variants`` and the
Kitty frame path is untouched by everything in this module.

Per-family readiness is not a guess: the last observed native ``supported_families``
is recorded in the state namespace, unsupported directions are labelled
unavailable, and approve/apply re-check the live runtime before any mutation.
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

import noctalia_presentation  # type: ignore
import process  # type: ignore
import storage  # type: ignore

SCHEMA_VERSION = 1
TARGET = "noctalia"

#: The closed stored capability shape (mirrors storage.NOCTALIA_CAPABILITY_KEYS).
#: A live observation may carry an extra ``surface_catalog``; it is projected
#: away before storage so the persisted namespace keeps its frozen shape.
_STORED_CAPABILITY_KEYS = ("at", "available", "supported_families", "reason")

#: Every skin family the catalog knows, in panel order.
FAMILIES: tuple[str, ...] = (
    "stagecraft",
    "cut-stone",
    "wayfinding-field",
    "adaptive-workbench",
    "kumihimo",
)

FAMILY_LABELS: dict[str, str] = {
    "stagecraft": "Stagecraft",
    "cut-stone": "Cut Stone",
    "wayfinding-field": "Wayfinding Field",
    "adaptive-workbench": "Adaptive Workbench",
    "kumihimo": "Kumihimo",
}

TOPOLOGIES: tuple[str, ...] = (
    "top-music-side",
    "top-music-bottom",
    "desktop-widget-side",
    "compact-rail",
    "hanging-tags",
)

#: Family/topology compatibility, mirrored by the native schema.
FAMILY_TOPOLOGIES: dict[str, tuple[str, ...]] = {
    "stagecraft": ("top-music-side", "top-music-bottom", "desktop-widget-side"),
    "cut-stone": ("compact-rail",),
    "wayfinding-field": ("compact-rail",),
    "adaptive-workbench": ("compact-rail",),
    "kumihimo": ("hanging-tags",),
}

DENSITIES: tuple[str, ...] = ("compact", "comfortable")
COLOR_KEYS: tuple[str, ...] = ("surface", "surface_variant", "text", "muted", "accent", "outline")
RECIPE_KEYS: tuple[str, ...] = (
    "schema_version",
    "target",
    "variant_id",
    "content_id",
    "name",
    "family",
    "topology",
    "colors",
    "density",
    "preview_path",
)

#: Env overrides; both are optional and never guessed from the owner's HOME.
SOURCE_ROOT_ENV = "NOCTALIA_SKIN_SOURCE_ROOT"
BINARY_ENV = "NOCTALIA_SKIN_BINARY"
DEFAULT_BINARY = "noctalia"

STATUS_TIMEOUT = 15.0
APPLY_TIMEOUT = 45.0
MAX_NATIVE_OUTPUT = 1 << 20
MAX_NAME_BYTES = 100
MAX_PATH_CHARS = 4096

RECIPE_FILE = "recipe.json"
#: The whole-skin package manifest stored beside the recipe of a full install.
PRESENTATION_FILE = "presentation.json"
_PREVIEW_SUFFIXES = (".png", ".webp", ".jpg", ".jpeg")
_VARIANT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_CONTENT_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_COLOR_RE = re.compile(r"^#[0-9a-f]{6}$")
_TEMPLATE_KEYS = ("schema_version", "target", "family", "name", "topology", "colors", "density", "preview")
#: Namespace tag that keeps builtin ids stable across independent runs.
_SOURCE_TAG = "noctalia-skin-v1"


class NoctaliaError(RuntimeError):
    """User-visible Noctalia failure; the message is safe to show."""


# ------------------------------------------------------------------------ paths


def skins_root() -> Path:
    """The backend-owned immutable install store (XDG, never the repo)."""
    return storage.xdg_data_home() / "noctalia" / "skins"


#: Durable installed builtin catalog, written by
#: ``integrations/wallpaper-studio/install.py``: the fallback source root of a
#: packaged backend on a machine that has no product checkout.
INSTALLED_SOURCE_RELATIVE = Path("wallpaper-studio") / "resources" / "noctalia-skins"

#: Durable record of the installed native runtime, written atomically by the
#: native installer (tools/install-skin-runtime.py) as
#: ``{"schema_version": 1, "binary": <prefix>/bin/noctalia}``.
NATIVE_RUNTIME_RECORD_RELATIVE = Path("wallpaper-studio") / "native-runtime.json"
NATIVE_RUNTIME_RECORD_SCHEMA_VERSION = 1
NATIVE_RUNTIME_RECORD_MAX_BYTES = 4096


def _repository_source_root() -> Path:
    """The product catalog beside this module inside a checkout."""
    return Path(__file__).resolve().parents[2] / "noctalia-theme" / "skins"


def _has_source_catalog(root: Path) -> bool:
    """Whether ``root`` is a real catalog: at least one family recipe present."""
    recipes = root / "recipes"
    return recipes.is_dir() and any(
        (recipes / f"{family}.json").is_file() for family in FAMILIES
    )


def source_root() -> Path:
    """Product source root: env override, else the checkout, else the install.

    Resolution is explicit: ``$NOCTALIA_SKIN_SOURCE_ROOT`` wins; a repository
    checkout is used only while it still carries a real catalog
    (``integrations/noctalia-theme/skins`` with family recipes), so a developer
    run keeps the live source; otherwise the durable catalog installed under
    ``$XDG_DATA_HOME/wallpaper-studio/resources/noctalia-skins`` is used — that
    is what the packaged backend resolves, and its recipes reference the durable
    installed previews instead of the transient worktree.  It never guesses an
    absolute path under the owner's HOME.  A missing root is reported through
    ``state["noctalia"]["seed_error"]`` and yields no builtin candidates.
    """
    override = os.environ.get(SOURCE_ROOT_ENV, "").strip()
    if override:
        return Path(os.path.abspath(os.path.expanduser(override)))
    repository = _repository_source_root()
    if _has_source_catalog(repository):
        return repository
    return storage.xdg_data_home() / INSTALLED_SOURCE_RELATIVE


# -------------------------------------------------------------------- validation


def validate_recipe(recipe) -> dict:
    """Strictly validate Recipe v1 and return a normalized independent copy.

    The object must carry exactly ``RECIPE_KEYS``; anything else (unknown keys,
    commands, URLs, shader code) is refused.  Colors are canonicalized to
    lowercase ``#rrggbb`` and the preview path is normalized, so the returned
    object is the one that is installed and sent to the native runtime.
    """
    if not isinstance(recipe, dict):
        raise NoctaliaError("recipe must be a JSON object")
    unknown = sorted(set(recipe) - set(RECIPE_KEYS))
    if unknown:
        raise NoctaliaError("recipe has unknown fields: " + ", ".join(unknown))
    missing = sorted(set(RECIPE_KEYS) - set(recipe))
    if missing:
        raise NoctaliaError("recipe is missing fields: " + ", ".join(missing))

    version = recipe["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        raise NoctaliaError(f"unsupported recipe schema_version {version!r}")
    if recipe["target"] != TARGET:
        raise NoctaliaError(f"recipe target must be {TARGET!r}")

    variant_id = recipe["variant_id"]
    if not isinstance(variant_id, str) or not _VARIANT_ID_RE.fullmatch(variant_id):
        raise NoctaliaError("recipe variant_id must be 32 lowercase hex characters")
    content_id = recipe["content_id"]
    if not isinstance(content_id, str) or not _CONTENT_ID_RE.fullmatch(content_id):
        raise NoctaliaError("recipe content_id must be 64 lowercase hex characters")

    name = recipe["name"]
    if not isinstance(name, str) or not name.strip():
        raise NoctaliaError("recipe name must be a non-empty string")
    try:
        name_bytes = name.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise NoctaliaError("recipe name must contain valid UTF-8 text") from exc
    if len(name_bytes) > MAX_NAME_BYTES or any(byte < 0x20 or byte == 0x7F for byte in name_bytes):
        raise NoctaliaError(
            f"recipe name must be at most {MAX_NAME_BYTES} UTF-8 bytes without control characters"
        )

    family = recipe["family"]
    if family not in FAMILIES:
        raise NoctaliaError("recipe family must be one of: " + ", ".join(FAMILIES))
    topology = recipe["topology"]
    allowed = FAMILY_TOPOLOGIES[family]
    if topology not in allowed:
        raise NoctaliaError(
            f"recipe family {family!r} only supports topology: " + ", ".join(allowed)
        )

    colors = recipe["colors"]
    if not isinstance(colors, dict):
        raise NoctaliaError("recipe colors must be a JSON object")
    unknown = sorted(set(colors) - set(COLOR_KEYS))
    if unknown:
        raise NoctaliaError("recipe colors have unknown fields: " + ", ".join(unknown))
    missing = sorted(set(COLOR_KEYS) - set(colors))
    if missing:
        raise NoctaliaError("recipe colors are missing fields: " + ", ".join(missing))
    checked_colors: dict[str, str] = {}
    for key in COLOR_KEYS:
        value = colors[key]
        if not isinstance(value, str) or not _COLOR_RE.fullmatch(value.lower()):
            raise NoctaliaError(f"recipe color {key!r} must be #RRGGBB")
        checked_colors[key] = value.lower()

    density = recipe["density"]
    if density not in DENSITIES:
        raise NoctaliaError("recipe density must be one of: " + ", ".join(DENSITIES))

    preview = recipe["preview_path"]
    if not isinstance(preview, str) or not preview or len(preview) > MAX_PATH_CHARS:
        raise NoctaliaError("recipe preview_path must be a bounded absolute path")
    if not os.path.isabs(preview):
        raise NoctaliaError("recipe preview_path must be absolute")
    if "://" in preview or any(ord(ch) < 0x20 for ch in preview):
        raise NoctaliaError("recipe preview_path must be a plain local path")

    return {
        "schema_version": SCHEMA_VERSION,
        "target": TARGET,
        "variant_id": variant_id,
        "content_id": content_id,
        "name": name,
        "family": family,
        "topology": topology,
        "colors": checked_colors,
        "density": density,
        "preview_path": os.path.normpath(preview),
    }


# ------------------------------------------------------------------- native IPC


def _installed_native_binary() -> str:
    """The installed native binary from the durable runtime record, or "".

    An absent record means "no installed runtime known" and yields "" so the
    caller may fall back to the plain command name. A present but malformed,
    oversized, stale-schema or non-executable record is an honest failure: it
    must never silently select the distro binary or fabricate availability.
    """
    record_path = storage.xdg_data_home() / NATIVE_RUNTIME_RECORD_RELATIVE
    try:
        with record_path.open("rb") as handle:
            raw_bytes = handle.read(NATIVE_RUNTIME_RECORD_MAX_BYTES + 1)
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise NoctaliaError(
            f"installed native-runtime record {record_path} is unreadable: "
            f"{storage.clean_text(str(exc), storage.MAX_ERROR_CHARS)}"
        ) from exc
    if len(raw_bytes) > NATIVE_RUNTIME_RECORD_MAX_BYTES:
        raise NoctaliaError(
            f"installed native-runtime record {record_path} exceeds "
            f"{NATIVE_RUNTIME_RECORD_MAX_BYTES} bytes"
        )
    try:
        record = json.loads(raw_bytes.decode("utf-8", "surrogateescape"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NoctaliaError(
            f"installed native-runtime record {record_path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(record, dict):
        raise NoctaliaError("installed native-runtime record must be a JSON object")
    unknown = sorted(set(record) - {"schema_version", "binary"})
    if unknown:
        raise NoctaliaError(
            "installed native-runtime record has unknown fields: " + ", ".join(unknown)
        )
    missing = sorted({"schema_version", "binary"} - set(record))
    if missing:
        raise NoctaliaError(
            "installed native-runtime record is missing fields: " + ", ".join(missing)
        )
    version = record["schema_version"]
    if (isinstance(version, bool) or not isinstance(version, int)
            or version != NATIVE_RUNTIME_RECORD_SCHEMA_VERSION):
        raise NoctaliaError(f"unsupported native-runtime schema_version {version!r}")
    binary = record["binary"]
    if not isinstance(binary, str) or not binary.strip():
        raise NoctaliaError("native-runtime record binary must be a non-empty string")
    if len(binary) > MAX_PATH_CHARS or "://" in binary or any(ord(ch) < 0x20 for ch in binary):
        raise NoctaliaError("native-runtime record binary must be a plain local path")
    if not os.path.isabs(binary):
        raise NoctaliaError("native-runtime record binary must be an absolute path")
    target = Path(binary)
    if not target.is_file() or not os.access(target, os.X_OK):
        raise NoctaliaError(
            f"native-runtime record binary is not an executable regular file: {binary}"
        )
    return binary


def _binary() -> str:
    """The native binary to exec, in strict precedence order.

    1. a nonempty ``$NOCTALIA_SKIN_BINARY`` override (unchanged behaviour);
    2. the current instance's exported ``$NOCTALIA_BINARY``;
    3. the durable installed runtime from
       ``$XDG_DATA_HOME/wallpaper-studio/native-runtime.json`` — a present but
       invalid record fails loudly instead of degrading;
    4. the plain ``noctalia`` command name, only when no installed record exists.
    """
    override = os.environ.get(BINARY_ENV, "").strip()
    if override:
        return override
    instance = os.environ.get("NOCTALIA_BINARY", "").strip()
    if instance:
        return instance
    installed = _installed_native_binary()
    if installed:
        return installed
    found = shutil.which(DEFAULT_BINARY)
    if not found:
        raise NoctaliaError(
            f"noctalia executable not found on PATH or via {BINARY_ENV}"
        )
    return found


def _parse_reply(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise NoctaliaError(f"native reply is not valid JSON: {exc}") from exc


def _run_native(args: list[str]) -> Any:
    """Run ``noctalia msg <args>`` as argv and parse its bounded JSON stdout.

    The native CLI prints its JSON payload on stdout even when it refuses an
    apply, so a JSON stdout is preferred over the exit code; only when there is
    no parseable stdout does the process failure become the error.
    """
    binary = _binary()
    try:
        completed = process.run(
            [binary, "msg", *args],
            STATUS_TIMEOUT if args[:1] == ["skin-status"] else APPLY_TIMEOUT,
            cancel=lambda: False,
            max_output=MAX_NATIVE_OUTPUT,
        )
    except process.RunFailed as exc:
        text = (exc.stdout or "").strip()
        if text:
            return _parse_reply(text)
        raise NoctaliaError(
            f"noctalia {args[0] if args else ''} failed: "
            f"{storage.clean_text(str(exc), storage.MAX_ERROR_CHARS)}"
        ) from exc
    text = completed.stdout.strip()
    if not text:
        raise NoctaliaError(f"noctalia {args[0] if args else ''} returned no output")
    return _parse_reply(text)


def native_status() -> dict:
    """Live native skin-status, normalized to the known families."""
    reply = _run_native(["skin-status"])
    if not isinstance(reply, dict):
        raise NoctaliaError("native skin-status reply is not a JSON object")
    version = reply.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        raise NoctaliaError(f"unsupported native skin-status schema_version {version!r}")
    families = reply.get("supported_families")
    if not isinstance(families, list) or any(not isinstance(item, str) for item in families):
        raise NoctaliaError("native skin-status supported_families must be a list of strings")
    known = [family for family in FAMILIES if family in families]
    return {
        "schema_version": SCHEMA_VERSION,
        "supported_families": known,
        "active": reply.get("active") if isinstance(reply.get("active"), dict) else None,
        "wallpaper_path": reply.get("wallpaper_path") if isinstance(reply.get("wallpaper_path"), str) else None,
        "music": reply.get("music") if isinstance(reply.get("music"), dict) else None,
        "ai": reply.get("ai") if isinstance(reply.get("ai"), dict) else None,
        "persistent_music_open": reply.get("persistent_music_open") is True,
        # The live actual surface inventory, when the native runtime projects it.
        # A runtime that predates the catalog simply omits it (whole-generation
        # stays disabled); a malformed catalog is treated as absent, never as a
        # reason to make the rest of the status unreadable.
        "surface_catalog": _surface_catalog(reply.get("surface_catalog")),
    }


def _surface_catalog(value) -> dict | None:
    """The normalized live surface catalog, or ``None`` when absent/malformed."""
    if value is None:
        return None
    try:
        return noctalia_presentation.normalize_catalog(value)
    except noctalia_presentation.PresentationError:
        return None


def _stored_capability(observed: dict | None) -> dict | None:
    """Project a live observation onto the frozen stored capability shape.

    The live catalog is intentionally dropped here: whole-generation reads it
    from the ephemeral live status, never from the persisted namespace.
    """
    if not isinstance(observed, dict):
        return observed
    return {key: observed.get(key) for key in _STORED_CAPABILITY_KEYS}


def capability() -> dict:
    """One bounded native capability observation, safe to store in the state.

    The returned object may carry the optional live ``surface_catalog``; callers
    persisting it must store ``_stored_capability(...)`` so the closed namespace
    shape never widens.
    """
    try:
        status = native_status()
    except NoctaliaError as exc:
        return {
            "at": storage.now(),
            "available": False,
            "supported_families": [],
            "reason": storage.clean_text(str(exc), storage.MAX_ERROR_CHARS),
            "surface_catalog": None,
        }
    return {
        "at": storage.now(),
        "available": True,
        "supported_families": list(status["supported_families"]),
        "reason": "",
        "surface_catalog": status.get("surface_catalog"),
    }


def family_available(capability: dict | None, family: str) -> tuple[bool, str]:
    """Whether the native runtime can render ``family`` right now."""
    if not isinstance(capability, dict):
        return False, "native skin runtime capability is unknown"
    if not capability.get("available"):
        return False, str(capability.get("reason") or "native skin runtime is unavailable")
    if family not in (capability.get("supported_families") or []):
        return False, f"family {family!r} is not implemented by the native skin runtime"
    return True, ""


def native_apply(recipe: dict) -> dict:
    """Apply one validated recipe through the native runtime.

    The JSON recipe is passed as a single argv element (never a shell string) and
    the runtime answer is authoritative: a refusal raises instead of pretending a
    configuration was applied.
    """
    checked = validate_recipe(recipe)
    payload = json.dumps(checked, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    reply = _run_native(["skin-apply", payload])
    if not isinstance(reply, dict):
        raise NoctaliaError("native skin-apply reply is not a JSON object")
    if reply.get("ok") is not True:
        reason = reply.get("error")
        raise NoctaliaError(
            storage.clean_text(str(reason or "native skin-apply refused the recipe"), storage.MAX_ERROR_CHARS)
        )
    return reply


# --------------------------------------------------------------------- install


def _recipe_bytes(recipe: dict) -> bytes:
    return (json.dumps(recipe, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_presentation(variant: dict, recipe: dict, source_preview: Path) -> dict | None:
    """The validated whole-skin package of a variant, or ``None`` for legacy.

    A full package must keep every image (scene included) inside the variant's
    own ``frames/<content_id>/<variant_id>`` cage, and its scene must be exactly
    the recipe preview; a legacy single-preview variant legitimately has none.
    """
    raw = variant.get("presentation")
    if raw is None:
        return None
    cage = storage.generation_dir(recipe["content_id"], recipe["variant_id"])
    try:
        presentation = noctalia_presentation.validate_presentation(
            raw,
            variant_id=recipe["variant_id"],
            content_id=recipe["content_id"],
            root=cage,
            verify_files=True,
        )
    except noctalia_presentation.PresentationError as exc:
        raise NoctaliaError(f"invalid noctalia presentation: {exc}") from exc
    scene = Path(presentation["scene"]["preview_path"])
    if Path(os.path.realpath(scene)) != Path(os.path.realpath(source_preview)):
        raise NoctaliaError("presentation scene image must be the recipe preview")
    return presentation


def _presentation_identity_matches(source: dict, installed: dict) -> bool:
    """Compare complete approved metadata, excluding only relocated image paths."""
    def identity(value):
        return {
            **value,
            "scene": {key: item for key, item in value["scene"].items() if key != "preview_path"},
            "surfaces": [
                {key: item for key, item in record.items() if key != "preview_path"}
                for record in value["surfaces"]
            ],
        }

    return identity(source) == identity(installed)


def installed_presentation_matches(source, installed: Path) -> bool:
    """Whether an installed directory holds the identical whole-skin package.

    The installed ``presentation.json`` is validated with file hashing confined
    to the install directory, then every identity-bearing field and image digest
    is compared to the approved source package.
    """
    try:
        expected = noctalia_presentation.validate_presentation(source)
    except noctalia_presentation.PresentationError:
        return False
    raw = storage.read_json(Path(installed) / PRESENTATION_FILE)
    if not isinstance(raw, dict):
        return False
    try:
        actual = noctalia_presentation.validate_presentation(
            raw, root=Path(installed), verify_files=True
        )
    except noctalia_presentation.PresentationError:
        return False
    return _presentation_identity_matches(expected, actual)


def _rebase_presentation(value: dict, old_root: Path, new_root: Path) -> dict:
    """Rewrite a staged package's image paths from the staging root to ``dest``."""
    old = os.path.realpath(old_root)
    new = os.path.realpath(new_root)

    def rebase(path: str) -> str:
        real = os.path.realpath(path)
        if real == old or not real.startswith(old + os.sep):
            raise NoctaliaError("staged presentation image escapes its staging directory")
        return str(Path(new) / Path(real).relative_to(old))

    rebased = dict(value)
    rebased["scene"] = {**value["scene"], "preview_path": rebase(value["scene"]["preview_path"])}
    rebased["surfaces"] = [
        {**record, "preview_path": rebase(record["preview_path"])}
        for record in value["surfaces"]
    ]
    return rebased


def _presentation_manifest_bytes(value: dict) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) + "\n").encode("utf-8")


def install_variant(variant: dict) -> Path:
    """Validate and immutably install one approved variant; return its directory.

    A legacy single-preview variant installs exactly as it always did (recipe +
    preview).  A full package additionally installs its whole immutable
    presentation — manifest and every image — in the same atomic rename, and
    idempotence validates the recipe, the preview and every manifest/image
    digest.  A different body already occupying the id is refused, so the
    owner's installed skin is never silently replaced.
    """
    variant_id = variant.get("id")
    if not isinstance(variant_id, str) or not _VARIANT_ID_RE.fullmatch(variant_id):
        raise NoctaliaError("variant id must be 32 lowercase hex characters")
    recipe = validate_recipe(variant.get("recipe"))
    if recipe["variant_id"] != variant_id:
        raise NoctaliaError("recipe variant_id does not match the variant id")
    if recipe["content_id"] != variant.get("content_id"):
        raise NoctaliaError("recipe content_id does not match the variant content id")

    source_preview = Path(str(variant.get("preview_path") or ""))
    if (not source_preview.is_absolute() or source_preview.is_symlink()
            or not source_preview.is_file()):
        raise NoctaliaError("variant preview is missing or not a regular file")
    suffix = source_preview.suffix.lower()
    if suffix not in _PREVIEW_SUFFIXES:
        raise NoctaliaError("variant preview must be a png, webp or jpeg image")
    source_presentation = _source_presentation(variant, recipe, source_preview)

    root = skins_root()
    root.mkdir(parents=True, exist_ok=True)
    dest = root / variant_id
    installed_recipe = dict(recipe)
    installed_recipe["preview_path"] = str(dest / f"preview{suffix}")
    payload = _recipe_bytes(installed_recipe)

    def _recipe_matches(target: Path) -> bool:
        recipe_file = target / RECIPE_FILE
        preview_file = target / f"preview{suffix}"
        return (
            recipe_file.is_file()
            and not recipe_file.is_symlink()
            and recipe_file.read_bytes() == payload
            and preview_file.is_file()
            and not preview_file.is_symlink()
            and _file_digest(preview_file) == _file_digest(source_preview)
        )

    def _matches(target: Path) -> bool:
        if not _recipe_matches(target):
            return False
        return source_presentation is None or installed_presentation_matches(source_presentation, target)

    if dest.exists() or dest.is_symlink():
        if not dest.is_dir() or dest.is_symlink():
            raise NoctaliaError(f"install path {dest} is not a directory")
        if _matches(dest):
            return dest
        raise NoctaliaError(
            f"a different noctalia skin already occupies {dest.name}; not overwritten"
        )

    staging = root / f".staging-{variant_id}-{uuid.uuid4().hex[:8]}"
    try:
        staging.mkdir()
        storage.atomic_write_bytes(staging / RECIPE_FILE, payload)
        shutil.copyfile(source_preview, staging / f"preview{suffix}")
        if source_presentation is not None:
            staged = noctalia_presentation.copy_presentation(
                source_presentation,
                staging,
                variant_id=variant_id,
                content_id=recipe["content_id"],
            )
            storage.atomic_write_bytes(
                staging / PRESENTATION_FILE,
                _presentation_manifest_bytes(_rebase_presentation(staged, staging, dest)),
            )
        try:
            os.rename(staging, dest)
        except OSError:
            if not _matches(dest):
                raise NoctaliaError(
                    f"a different noctalia skin already occupies {dest.name}; not overwritten"
                )
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return dest


def installed_recipe(installed: Path) -> dict:
    """The validated recipe body stored in an installed variant directory."""
    try:
        raw = json.loads(Path(installed, RECIPE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise NoctaliaError(f"installed noctalia recipe is unreadable: {exc}") from exc
    return validate_recipe(raw)


def apply_variant(variant: dict) -> dict:
    """Recheck the complete approved install, then send its recipe to native."""
    installed = Path(str(variant.get("installed_path") or ""))
    recipe = installed_recipe(installed)
    presentation = variant.get("presentation")
    if presentation is not None:
        try:
            source = noctalia_presentation.validate_presentation(
                presentation, variant_id=variant.get("id"), content_id=variant.get("content_id"))
        except noctalia_presentation.PresentationError as exc:
            raise NoctaliaError(f"invalid approved presentation: {exc}") from exc
        expected = validate_recipe(variant.get("recipe"))
        suffix = Path(str(variant.get("preview_path") or "")).suffix.lower()
        preview = installed / f"preview{suffix}"
        expected["preview_path"] = str(preview)
        if recipe != expected or (installed / RECIPE_FILE).is_symlink():
            raise NoctaliaError("installed recipe differs from the approved whole-skin recipe")
        if (not preview.is_file() or preview.is_symlink()
                or _file_digest(preview) != source["scene"]["sha256"]
                or not installed_presentation_matches(source, installed)):
            raise NoctaliaError("installed whole-skin images or metadata differ from the approved package")
        status = native_status()
        available, reason = family_available(
            {"available": True, "supported_families": status["supported_families"]}, recipe["family"])
        if not available:
            raise NoctaliaError(reason)
        view = noctalia_presentation.presentation_view(source, catalog=status.get("surface_catalog"))
        if not view["apply_ready"]:
            raise NoctaliaError(view["unavailable_reason"])
    return native_apply(recipe)


# --------------------------------------------------------------------- seeding


def _builtin_variant_id(content_id: str, family: str) -> str:
    """Deterministic 32-hex candidate id for one content and family."""
    seed = f"{_SOURCE_TAG}\0{content_id}\0{family}".encode("utf-8")
    return hashlib.sha256(seed).hexdigest()[:32]


def _load_template(root: Path, path: Path, family: str) -> dict:
    """One product recipe template plus its resolved preview path."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise NoctaliaError(f"builtin template {path.name} is unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise NoctaliaError(f"builtin template {path.name} is not a JSON object")
    unknown = sorted(set(raw) - set(_TEMPLATE_KEYS))
    if unknown:
        raise NoctaliaError(f"builtin template {path.name} has unknown fields: " + ", ".join(unknown))
    if raw.get("family") != family:
        raise NoctaliaError(f"builtin template {path.name} family does not match its file name")
    version = raw.get("schema_version")
    if version is not None and (isinstance(version, bool) or version != SCHEMA_VERSION):
        raise NoctaliaError(f"builtin template {path.name} has an unsupported schema_version")
    if raw.get("target") not in (None, TARGET):
        raise NoctaliaError(f"builtin template {path.name} target must be {TARGET!r}")
    preview = raw.get("preview")
    if not isinstance(preview, str) or not preview or os.path.isabs(preview) or "://" in preview:
        raise NoctaliaError(f"builtin template {path.name} needs a relative 'preview' path")
    real_root = Path(os.path.realpath(root))
    real_preview = Path(os.path.realpath(root / preview))
    if (real_preview == real_root or not real_preview.is_relative_to(real_root)
            or not real_preview.is_file()):
        raise NoctaliaError(f"builtin template {path.name} preview is missing or outside the source root")
    if real_preview.suffix.lower() not in _PREVIEW_SUFFIXES:
        raise NoctaliaError(f"builtin template {path.name} preview must be a png, webp or jpeg image")
    return {
        "family": family,
        "name": raw.get("name"),
        "topology": raw.get("topology"),
        "colors": raw.get("colors"),
        "density": raw.get("density"),
        "preview": real_preview,
        "template": path.name,
    }


def _seed_record(content_id: str, template: dict) -> dict:
    variant_id = _builtin_variant_id(content_id, template["family"])
    recipe = validate_recipe({
        "schema_version": SCHEMA_VERSION,
        "target": TARGET,
        "variant_id": variant_id,
        "content_id": content_id,
        "name": template["name"],
        "family": template["family"],
        "topology": template["topology"],
        "colors": template["colors"],
        "density": template["density"],
        "preview_path": str(template["preview"]),
    })
    return {
        "id": variant_id,
        "content_id": content_id,
        "target": TARGET,
        "name": recipe["name"],
        "state": "candidate",
        "family": recipe["family"],
        "topology": recipe["topology"],
        "colors": dict(recipe["colors"]),
        "density": recipe["density"],
        "recipe": recipe,
        "preview_path": recipe["preview_path"],
        "preview_kind": "static-mockup",
        "created_at": storage.now(),
        "builtin": True,
        "source": "builtin",
        "installed_path": None,
        "provenance": {
            "source": "builtin",
            "template": template["template"],
            "source_root": str(source_root()),
        },
    }


def ensure_builtin_candidates(data: Path | None, items) -> None:
    """Append the per-wallpaper builtin family candidates; never a fake entry.

    Idempotent and append-only: an already seeded id is left byte-for-byte
    untouched, and a run with nothing new performs no write.  A missing or
    malformed product source root records an explicit ``seed_error`` instead of
    inventing a candidate.  The native capability is (re)observed whenever new
    records are seeded, and recorded once otherwise.
    """
    root = source_root()
    state = storage.read_state(data)
    namespace = state["noctalia"]
    known_ids = {variant.get("id") for variant in namespace["variants"] if isinstance(variant, dict)}

    seed_error: str | None = None
    resolved: list[dict] = []
    if root.is_dir():
        for family in FAMILIES:
            path = root / "recipes" / f"{family}.json"
            if not path.is_file():
                continue
            try:
                resolved.append(_load_template(root, path, family))
            except NoctaliaError as exc:
                seed_error = str(exc)
    else:
        seed_error = f"builtin source root is missing: {root}"
    if root.is_dir() and not resolved and seed_error is None:
        seed_error = f"no builtin recipes found under {root / 'recipes'}"

    records: list[dict] = []
    if resolved and isinstance(items, list):
        for item in items:
            content_id = item.get("id") if isinstance(item, dict) else None
            if not isinstance(content_id, str) or not _CONTENT_ID_RE.fullmatch(content_id):
                continue
            for template in resolved:
                if _builtin_variant_id(content_id, template["family"]) in known_ids:
                    continue
                try:
                    record = _seed_record(content_id, template)
                except NoctaliaError as exc:
                    seed_error = str(exc)
                    continue
                records.append(record)
                known_ids.add(record["id"])

    recorded_capability = namespace.get("capability")
    have_capability = isinstance(recorded_capability, dict)
    if not records and have_capability and seed_error == namespace.get("seed_error"):
        return None
    if records or not have_capability:
        observed_capability = capability()
    else:
        observed_capability = recorded_capability

    def mutator(current: dict) -> None:
        target = current["noctalia"]
        existing = {variant.get("id") for variant in target["variants"] if isinstance(variant, dict)}
        for record in records:
            if record["id"] not in existing:
                target["variants"].append(record)
                existing.add(record["id"])
        # The live catalog is ephemeral: the closed namespace stores only the
        # frozen capability shape, never the surface inventory.
        target["capability"] = _stored_capability(observed_capability)
        target["seed_error"] = seed_error

    storage.update_state(mutator, data)
    return None


# --------------------------------------------------------------------- catalog


def _variant_view(variant: dict, cap_available: bool, cap_reason: str,
                  supported: list[str], active_id: Any, surface_catalog=None) -> dict:
    family = variant.get("family") if isinstance(variant.get("family"), str) else ""
    if not family:
        available, reason = False, "variant has no family"
    elif not cap_available:
        available, reason = False, cap_reason or "native skin runtime is unavailable"
    elif family not in supported:
        available, reason = False, f"family {family!r} is not implemented by the native skin runtime"
    else:
        available, reason = True, ""
    colors = variant.get("colors") if isinstance(variant.get("colors"), dict) else {}
    return {
        "variant_id": variant.get("id"),
        "name": variant.get("name"),
        "family": family or None,
        "topology": variant.get("topology"),
        "colors": {key: colors.get(key) for key in COLOR_KEYS},
        "density": variant.get("density"),
        "preview_path": variant.get("preview_path"),
        "preview_kind": variant.get("preview_kind"),
        "state": variant.get("state"),
        # The stored whole-skin package projected purely (no I/O): a legacy
        # single-preview variant exposes the honest incomplete view instead of
        # a fabricated component gallery.
        "presentation": noctalia_presentation.presentation_view(
            variant.get("presentation"), catalog=surface_catalog),
        "builtin": bool(variant.get("builtin")),
        "source": variant.get("source") or ("builtin" if variant.get("builtin") else "generated"),
        "created_at": variant.get("created_at"),
        "available": available,
        "unavailable_reason": reason,
        "installed": bool(variant.get("installed_path")),
        "active": isinstance(active_id, str) and variant.get("id") == active_id,
        "parent_variant_id": variant.get("parent_variant_id"),
        "batch_id": variant.get("batch_id"),
        "ordinal": variant.get("ordinal"),
        "count": variant.get("count"),
        "revision": variant.get("revision"),
    }


def noctalia_catalog(state: dict, content_id, *, surface_catalog=None) -> dict:
    """Per-content Noctalia catalog and readiness, derived from stored state.

    Pure state read: no native IPC, no filesystem work, no implicit apply.  A
    ``content_id`` that is not a valid content id yields an empty variant list
    while the global capability and mappings remain available.
    """
    namespace = state.get("noctalia") if isinstance(state.get("noctalia"), dict) else {}
    recorded = namespace.get("capability")
    if isinstance(recorded, dict):
        cap_available = recorded.get("available") is True
        cap_reason = str(recorded.get("reason") or "")
        supported = [family for family in FAMILIES if family in (recorded.get("supported_families") or [])]
    else:
        cap_available, cap_reason, supported = False, "native skin runtime capability is unknown", []

    mappings = namespace.get("mappings") if isinstance(namespace.get("mappings"), dict) else {}
    current = content_id if isinstance(content_id, str) and _CONTENT_ID_RE.fullmatch(content_id) else None
    active_id = mappings.get(current) if current else None
    if not isinstance(active_id, str):
        active_id = None
    variants = [
        _variant_view(variant, cap_available, cap_reason, supported, active_id, surface_catalog)
        for variant in (namespace.get("variants") or [])
        if isinstance(variant, dict) and (current is None or variant.get("content_id") == current)
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "content_id": current,
        "available": cap_available,
        "reason": cap_reason,
        "supported_families": supported,
        "families": [
            {
                "id": family,
                "label": FAMILY_LABELS[family],
                "available": cap_available and family in supported,
                "topologies": list(FAMILY_TOPOLOGIES[family]),
                "reason": "" if (cap_available and family in supported) else (
                    cap_reason or f"family {family!r} is not implemented by the native skin runtime"
                ),
            }
            for family in FAMILIES
        ],
        "mappings": dict(mappings),
        "active_variant_id": active_id,
        "last_applied": namespace.get("last_applied") if isinstance(namespace.get("last_applied"), dict) else None,
        "seed_error": namespace.get("seed_error") if isinstance(namespace.get("seed_error"), str) else None,
        "variants": variants,
    }
