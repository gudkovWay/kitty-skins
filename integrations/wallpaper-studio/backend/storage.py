#!/usr/bin/env python3
"""Durable Wallpaper Studio state.

The studio shares <XDG_DATA_HOME>/wallpaper-context with the wallpaper-context
helpers: the catalog index (index.json), the analysis records under analysis/
and the studio records under studio.json live in one root, while generated packs
live under frames/<content-id>/<job-id>/.

Writes go through atomic_json (same-directory temp file, fsync, rename). The
studio record itself is serialised by an exclusive flock on studio.lock while it
is read, mutated and written. Readers never lock: a rename is atomic, so a
snapshot sees either the previous or the next document, never a torn one. No
external process may run while the state lock is held.

Schema 2 moved the flat Kitty geometry (detail/thickness/motion) into a nested
``preferences.kitty`` object and added ``design_profiles``. Schema 3 adds
``design_overrides``: the owner's per-wallpaper design basis, kept apart from the
raw model analysis and from the immutable design profiles. Schema 4 adds the
``preferences.design_mode`` selector (quality/gothic, default quality) and the
top-level ``feedback`` list of reasoned owner approve/reject decisions.

A schema-1, schema-2 or schema-3 record is migrated in place, once, under the
state lock: the pre-migration bytes are copied to ``studio.json.schema1.bak`` /
``studio.json.schema2.bak`` / ``studio.json.schema3.bak`` first (each backup is
written once and never overwritten by a later step), so any original is always
recoverable. Migration preserves every old job, variant, profile and mapping
unchanged; only the new fields are added.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Flat module: studio.py puts backend/ on sys.path (the sibling modules load the
# same way). designs has no imports of its own, so there is no cycle.
import designs  # type: ignore

SCHEMA_VERSION = 4
#: The flat-preference schema the studio shipped before the unified studio.
LEGACY_SCHEMA_VERSION = 1
#: The nested-preferences schema the studio shipped before per-wallpaper design overrides.
PREVIOUS_SCHEMA_VERSION = 2
#: The per-wallpaper-design-overrides schema, before design modes and feedback.
SCHEMA3_VERSION = 3
#: Pre-migration copies of the studio record, written once per migration step.
LEGACY_BACKUP_FILE = "studio.json.schema1.bak"
PREVIOUS_BACKUP_FILE = "studio.json.schema2.bak"
SCHEMA3_BACKUP_FILE = "studio.json.schema3.bak"

DATA_DIR_ENV = "WALLPAPER_STUDIO_DATA_DIR"
STATE_FILE = "studio.json"
LOCK_FILE = "studio.lock"
WORKER_LOCK_FILE = "worker.lock"
ANALYSIS_DIR = "analysis"
FRAMES_DIR = "frames"
LOGS_DIR = "logs"
WORKER_LOG_FILE = "worker.log"
#: Durable model-attempt ledger; deliberately outside every reset-scoped tree.
USAGE_FILE = "generation-usage.json"
#: Managed copies of imported wallpapers (owned by the playback module).
IMPORTS_DIR = "imports"

MAX_TEXT_CHARS = 2000
MAX_OUTPUT_CHARS = 200
MAX_MODEL_CHARS = 120
MAX_ERROR_CHARS = 2000

#: The four editable design-basis fields of one wallpaper, in panel order.
DESIGN_BASIS_FIELDS = ("style_mood", "materials_motifs", "palette_lighting", "composition")
#: Bound of one stored design-basis string (derived source and owner override alike).
MAX_DESIGN_BASIS_CHARS = 800

DETAIL_LEVELS = ("minimal", "balanced", "ornate")
THICKNESS_LEVELS = ("thin", "normal", "bold")
MOTION_MODES = ("static", "candles")
#: The shared design identity of a generation: a craft-transferring quality pass
#: or an explicitly gothic pass. Kept apart from the per-application renderer.
DESIGN_MODES = ("quality", "gothic")
DEFAULT_DESIGN_MODE = "quality"

#: Bound of one owner feedback reason (approve/reject).
MAX_FEEDBACK_REASON_CHARS = 2000
#: Feedback entries recorded by the owner's approve/reject decisions.
FEEDBACK_KEYS = ("variant_id", "content_id", "target", "verdict", "reason", "created_at")
FEEDBACK_VERDICTS = ("approved", "rejected")

#: Kitty pack geometry: application-specific settings belong under their app.
DEFAULT_KITTY_PREFERENCES: dict[str, Any] = {
    "detail": "balanced",
    "thickness": "normal",
    "motion": "static",
}

#: Common artistic preferences plus the per-application section. No flat
#: legacy aliases survive schema 2.
DEFAULT_PREFERENCES: dict[str, Any] = {
    "likes": "",
    "dislikes": "",
    "notes": "",
    "auto_apply": False,
    "output": "",
    "vision_model": "openai-codex/gpt-6-luna",
    "design_mode": DEFAULT_DESIGN_MODE,
    "kitty": dict(DEFAULT_KITTY_PREFERENCES),
}

PREFERENCE_FIELDS = tuple(DEFAULT_PREFERENCES)
KITTY_PREFERENCE_FIELDS = tuple(DEFAULT_KITTY_PREFERENCES)

_STATE_KEYS = (
    "schema_version",
    "preferences",
    "jobs",
    "variants",
    "mappings",
    "appearance",
    "last_applied",
    "sync_error",
    "design_profiles",
    "design_overrides",
    "feedback",
)

_PROFILE_KEYS = (
    "schema_version",
    "id",
    "content_id",
    "evidence",
    "visual",
    "palette",
    "preferences",
    "provenance",
)
_PROFILE_SCHEMA_VERSION = 1
_CONTENT_ID_RE = re.compile(r"^[0-9a-f]{64}$")

# Paths already locked by this process, with a reentrancy depth. The backend is
# single-threaded (CLI calls and the worker drain loop), so a plain map is enough.
_HELD_LOCKS: dict[str, int] = {}


# ------------------------------------------------------------------------- paths


def xdg_data_home() -> Path:
    """Absolute $XDG_DATA_HOME, otherwise $HOME/.local/share (XDG rule)."""
    raw = os.environ.get("XDG_DATA_HOME", "").strip()
    if raw and os.path.isabs(raw):
        return Path(raw)
    return Path.home() / ".local" / "share"


def data_root() -> Path:
    """Studio/catalog data root: $WALLPAPER_STUDIO_DATA_DIR or the shared XDG root."""
    raw = os.environ.get(DATA_DIR_ENV, "").strip()
    if raw:
        return Path(os.path.abspath(os.path.expanduser(raw)))
    return xdg_data_home() / "wallpaper-context"


def resolve_root(data: Path | None = None) -> Path:
    return Path(data) if data is not None else data_root()


def state_path(data: Path | None = None) -> Path:
    return resolve_root(data) / STATE_FILE


def legacy_backup_path(data: Path | None = None) -> Path:
    """Where the pre-migration schema-1 record is copied (write-once)."""
    return resolve_root(data) / LEGACY_BACKUP_FILE


def previous_backup_path(data: Path | None = None) -> Path:
    """Where the pre-migration schema-2 record is copied (write-once)."""
    return resolve_root(data) / PREVIOUS_BACKUP_FILE


def schema3_backup_path(data: Path | None = None) -> Path:
    """Where the pre-migration schema-3 record is copied (write-once)."""
    return resolve_root(data) / SCHEMA3_BACKUP_FILE


def lock_path(data: Path | None = None) -> Path:
    return resolve_root(data) / LOCK_FILE


def worker_lock_path(data: Path | None = None) -> Path:
    return resolve_root(data) / WORKER_LOCK_FILE


def analysis_path(content_id: str, data: Path | None = None) -> Path:
    return resolve_root(data) / ANALYSIS_DIR / f"{content_id}.json"


def frames_dir(data: Path | None = None) -> Path:
    return resolve_root(data) / FRAMES_DIR


def imports_dir(data: Path | None = None) -> Path:
    """Managed copies of imported wallpapers (written by the playback module)."""
    return resolve_root(data) / IMPORTS_DIR


def analysis_binding_path(content_id: str, data: Path | None = None) -> Path:
    """Render-setting binding of a stored analysis record.

    The analysis envelope itself has a fixed, closed schema (the installed
    analyzer rejects any extra key), so the capture render key an envelope was
    produced from is recorded next to the envelope instead: a scene/web analysis
    may only be reused while its recorded render key still matches the live
    render settings.
    """
    return analysis_path(content_id, data).with_name(f"{content_id}.binding.json")


def logs_dir(data: Path | None = None) -> Path:
    return resolve_root(data) / LOGS_DIR


def worker_log_path(data: Path | None = None) -> Path:
    return logs_dir(data) / WORKER_LOG_FILE


def usage_path(data: Path | None = None) -> Path:
    """Durable generation-attempt ledger.

    It lives directly in the data root, beside the studio record but outside
    every tree a scoped reset quarantines (analysis/, frames/, previews/), so
    removing one wallpaper's studio artifacts never erases the accounting of the
    paid attempts already made.
    """
    return resolve_root(data) / USAGE_FILE


def now() -> str:
    """Current UTC time as ISO-8601 with a trailing Z (matches catalog.utc_now)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ------------------------------------------------------------------------- json io


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Write bytes atomically: same-directory temp file, fsync, rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle_fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_json(path: Path, value) -> None:
    """Write JSON atomically: same-directory temp file, fsync, rename."""
    payload = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8", "surrogateescape")
    payload += b"\n"
    atomic_write_bytes(path, payload)


def read_json(path: Path):
    """Parse a JSON file; None when it is absent, unreadable or malformed."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    try:
        return json.loads(raw.decode("utf-8", "surrogateescape"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


# ------------------------------------------------------------------- validation


def clean_text(value, limit: int = MAX_TEXT_CHARS) -> str:
    """Untrusted text -> single string without control characters, bounded length."""
    if not isinstance(value, str):
        return ""
    cleaned = "".join(ch for ch in value if ch >= " " or ch in "\n\t")
    return cleaned[:limit]


def validate_preferences(values, *, base: dict | None = None, complete: bool = False) -> dict:
    """Validate a preference patch (or a whole stored profile) and return a profile.

    Unknown fields are always rejected, at the top level and inside ``kitty``.
    With complete=True the input must carry exactly the canonical field set
    (used when validating a stored state record). A patch merges partially: a
    ``kitty`` object may carry a single key and the rest is taken from `base`
    (or from the defaults when there is no base), so a UI that submits one
    nested control at a time never rewrites its neighbours.
    """
    if not isinstance(values, dict):
        raise ValueError("preferences must be a JSON object")
    unknown = sorted(set(values) - set(PREFERENCE_FIELDS))
    if unknown:
        raise ValueError("unknown preference fields: " + ", ".join(unknown))
    if complete:
        missing = sorted(set(PREFERENCE_FIELDS) - set(values))
        if missing:
            raise ValueError("missing preference fields: " + ", ".join(missing))

    profile = json.loads(json.dumps(DEFAULT_PREFERENCES))
    if base is not None:
        profile = json.loads(json.dumps(validate_preferences(base, complete=True)))

    if "kitty" in values:
        kitty = values["kitty"]
        if not isinstance(kitty, dict):
            raise ValueError("preference 'kitty' must be a JSON object")
        unknown_kitty = sorted(set(kitty) - set(KITTY_PREFERENCE_FIELDS))
        if unknown_kitty:
            raise ValueError("unknown kitty preference fields: " + ", ".join(unknown_kitty))
        if complete:
            nested_missing = sorted(set(KITTY_PREFERENCE_FIELDS) - set(kitty))
            if nested_missing:
                raise ValueError("missing kitty preference fields: " + ", ".join(nested_missing))
        merged = dict(profile["kitty"])
        if "detail" in kitty:
            merged["detail"] = _enum(kitty["detail"], DETAIL_LEVELS, "kitty.detail")
        if "thickness" in kitty:
            merged["thickness"] = _enum(kitty["thickness"], THICKNESS_LEVELS, "kitty.thickness")
        if "motion" in kitty:
            merged["motion"] = _enum(kitty["motion"], MOTION_MODES, "kitty.motion")
        profile["kitty"] = merged

    for field in ("likes", "dislikes", "notes"):
        if field in values:
            value = values[field]
            if not isinstance(value, str):
                raise ValueError(f"preference {field!r} must be a string")
            profile[field] = clean_text(value, MAX_TEXT_CHARS)
    if "auto_apply" in values:
        value = values["auto_apply"]
        if not isinstance(value, bool):
            raise ValueError("preference 'auto_apply' must be a boolean")
        profile["auto_apply"] = value
    if "output" in values:
        value = values["output"]
        if not isinstance(value, str):
            raise ValueError("preference 'output' must be a string")
        if len(value) > MAX_OUTPUT_CHARS:
            raise ValueError(f"preference 'output' must be at most {MAX_OUTPUT_CHARS} characters")
        profile["output"] = clean_text(value, MAX_OUTPUT_CHARS).strip()
    if "vision_model" in values:
        value = values["vision_model"]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("preference 'vision_model' must be a non-empty string")
        if len(value) > MAX_MODEL_CHARS:
            raise ValueError(f"preference 'vision_model' must be at most {MAX_MODEL_CHARS} characters")
        profile["vision_model"] = value.strip()
    if "design_mode" in values:
        profile["design_mode"] = _enum(values["design_mode"], DESIGN_MODES, "design_mode")
    return profile


def _enum(value, allowed: tuple[str, ...], field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"preference {field!r} must be one of: {', '.join(allowed)}")
    return value


def _design_fields(fields, label: str) -> dict:
    """One partial design-basis object: only the four known keys, strings only.

    Accepted strings are kept verbatim — an explicit empty string stays empty and
    nothing is trimmed or stripped — so the panel's acknowledgment compares the
    exact text it submitted. A field longer than the storage bound is rejected
    visibly instead of being silently truncated and saved as a different string.
    """
    if not isinstance(fields, dict):
        raise ValueError(f"{label} must be a JSON object")
    unknown = sorted(set(fields) - set(DESIGN_BASIS_FIELDS))
    if unknown:
        raise ValueError(f"unknown design basis fields in {label}: " + ", ".join(unknown))
    checked = {}
    for field, text in fields.items():
        if not isinstance(text, str):
            raise ValueError(f"design basis {field!r} in {label} must be a string")
        if len(text) > MAX_DESIGN_BASIS_CHARS:
            raise ValueError(
                f"design basis {field!r} in {label} must be at most {MAX_DESIGN_BASIS_CHARS} characters"
            )
        checked[field] = text
    return checked


def validate_design_basis(value) -> dict:
    """A partial design-basis patch from a request (zero or more of the four fields)."""
    return _design_fields(value, "'design_basis'")


def validate_design_overrides(value) -> dict:
    """Stored per-content design overrides: {content_id: partial four-field object}."""
    if not isinstance(value, dict):
        raise ValueError("'design_overrides' must be a JSON object")
    out = {}
    for content_id, fields in value.items():
        if not isinstance(content_id, str) or not _CONTENT_ID_RE.match(content_id):
            raise ValueError("design override keys must be lowercase 64-hex content ids")
        out[content_id] = _design_fields(fields, f"design overrides for {content_id!r}")
    return out


def validate_feedback_reason(value) -> str:
    """The reason text of one owner decision.

    A missing reason (``None``) is allowed for the old panel and cleans to the
    empty string; a non-string or an over-long reason is rejected before the
    decision's side effects. Control characters are stripped like every other
    stored text.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("'reason' must be a string")
    if len(value) > MAX_FEEDBACK_REASON_CHARS:
        raise ValueError(f"'reason' must be at most {MAX_FEEDBACK_REASON_CHARS} characters")
    return clean_text(value, MAX_FEEDBACK_REASON_CHARS)


def validate_feedback_entry(entry) -> dict:
    """One stored owner feedback record: reasoned approve/reject of a variant."""
    if not isinstance(entry, dict):
        raise ValueError("feedback entries must be objects")
    unknown = sorted(set(entry) - set(FEEDBACK_KEYS))
    if unknown:
        raise ValueError("feedback entry has unknown fields: " + ", ".join(unknown))
    missing = sorted(set(FEEDBACK_KEYS) - set(entry))
    if missing:
        raise ValueError("feedback entry is missing fields: " + ", ".join(missing))
    if not isinstance(entry["variant_id"], str) or not entry["variant_id"]:
        raise ValueError("feedback entry needs a non-empty string 'variant_id'")
    if not isinstance(entry["content_id"], str) or not _CONTENT_ID_RE.match(entry["content_id"]):
        raise ValueError("feedback entry 'content_id' must be a lowercase 64-hex content id")
    if not designs.is_known(entry["target"]):
        raise ValueError(f"feedback entry has an unknown target {entry['target']!r}")
    if entry["verdict"] not in FEEDBACK_VERDICTS:
        raise ValueError("feedback entry 'verdict' must be one of: " + ", ".join(FEEDBACK_VERDICTS))
    entry["reason"] = validate_feedback_reason(entry["reason"])
    if not isinstance(entry["created_at"], str):
        raise ValueError("feedback entry 'created_at' must be a string")
    return entry


def validate_feedback(value) -> list:
    """Stored owner feedback list, in append order."""
    if not isinstance(value, list):
        raise ValueError("studio state 'feedback' must be a list")
    return [validate_feedback_entry(entry) for entry in value]


def _validate_job_entry(job) -> None:
    if not isinstance(job, dict):
        raise ValueError("jobs entries must be objects")
    for key in ("id", "kind", "state", "stage"):
        if not isinstance(job.get(key), str):
            raise ValueError(f"job entry needs a string {key!r}")
    if not isinstance(job.get("payload"), dict):
        raise ValueError(f"job {job.get('id')!r} needs an object 'payload'")


def _validate_variant_entry(variant) -> None:
    if not isinstance(variant, dict):
        raise ValueError("variants entries must be objects")
    for key in ("id", "content_id", "state"):
        if not isinstance(variant.get(key), str):
            raise ValueError(f"variant entry needs a string {key!r}")


def _validate_profile_entry(profile_id, profile) -> None:
    """A stored design profile must be exactly the shared immutable record."""
    if not isinstance(profile, dict):
        raise ValueError("design profile entries must be objects")
    unknown = sorted(set(profile) - set(_PROFILE_KEYS))
    if unknown:
        raise ValueError(f"design profile {profile_id!r} has unknown fields: " + ", ".join(unknown))
    missing = sorted(set(_PROFILE_KEYS) - set(profile))
    if missing:
        raise ValueError(f"design profile {profile_id!r} is missing fields: " + ", ".join(missing))
    if profile["schema_version"] != _PROFILE_SCHEMA_VERSION or isinstance(profile["schema_version"], bool):
        raise ValueError(f"design profile {profile_id!r} has an unsupported schema_version")
    if profile["id"] != profile_id:
        raise ValueError(f"design profile {profile_id!r} does not match its key")
    if not isinstance(profile_id, str) or not _CONTENT_ID_RE.match(profile_id):
        raise ValueError("design profile keys must be lowercase 64-hex ids")
    if not isinstance(profile["content_id"], str) or not _CONTENT_ID_RE.match(profile["content_id"]):
        raise ValueError(f"design profile {profile_id!r} has an invalid content_id")
    for field in ("evidence", "visual", "palette", "preferences", "provenance"):
        if not isinstance(profile[field], dict):
            raise ValueError(f"design profile {profile_id!r} field {field!r} must be an object")


def validate_state(value) -> dict:
    """Reject a studio record this backend cannot trust instead of guessing."""
    if not isinstance(value, dict):
        raise ValueError("studio state is not a JSON object")
    unknown = sorted(set(value) - set(_STATE_KEYS))
    if unknown:
        raise ValueError("studio state has unknown fields: " + ", ".join(unknown))
    missing = sorted(set(_STATE_KEYS) - set(value))
    if missing:
        raise ValueError("studio state is missing fields: " + ", ".join(missing))
    version = value["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        raise ValueError(f"unsupported studio schema_version {version!r}")
    validate_preferences(value["preferences"], complete=True)
    if not isinstance(value["jobs"], list):
        raise ValueError("studio state 'jobs' must be a list")
    for job in value["jobs"]:
        _validate_job_entry(job)
    if not isinstance(value["variants"], list):
        raise ValueError("studio state 'variants' must be a list")
    for variant in value["variants"]:
        _validate_variant_entry(variant)
    profiles = value["design_profiles"]
    if not isinstance(profiles, dict):
        raise ValueError("studio state 'design_profiles' must be an object")
    for profile_id, profile in profiles.items():
        _validate_profile_entry(profile_id, profile)
    validate_design_overrides(value["design_overrides"])
    validate_feedback(value["feedback"])
    mappings = value["mappings"]
    if not isinstance(mappings, dict):
        raise ValueError("studio state 'mappings' must be an object")
    for key, item in mappings.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise ValueError("studio state 'mappings' must map strings to strings")
    for field in ("appearance", "last_applied"):
        if value[field] is not None and not isinstance(value[field], dict):
            raise ValueError(f"studio state {field!r} must be an object or null")
    if value["sync_error"] is not None and not isinstance(value["sync_error"], str):
        raise ValueError("studio state 'sync_error' must be a string or null")
    return value


def default_state() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "preferences": json.loads(json.dumps(DEFAULT_PREFERENCES)),
        "jobs": [],
        "variants": [],
        "mappings": {},
        "appearance": None,
        "last_applied": None,
        "sync_error": None,
        "design_profiles": {},
        "design_overrides": {},
        "feedback": [],
    }


# -------------------------------------------------------------------- migration


def _upgrade_preferences4(preferences):
    """Add the schema-4 design_mode to an older, complete preferences object.

    The historical value is the default quality mode: no pre-feature record ever
    chose a mode, and every one of them described the craft-transferring pass.
    """
    if not isinstance(preferences, dict):
        return preferences
    upgraded = dict(preferences)
    upgraded.setdefault("design_mode", DEFAULT_DESIGN_MODE)
    return upgraded


def migrate_preferences(value) -> dict:
    """Bring a schema-1 preference object (or job payload copy) to the current schema.

    The flat Kitty geometry moves under ``kitty``; every other value is carried
    over untouched. A field the current schema rejects keeps its canonical
    default instead of failing the whole migration — the pre-migration record is
    backed up, so nothing is lost. Fields added after schema 1 (design_mode)
    start at their canonical default.
    """
    source = value if isinstance(value, dict) else {}
    kitty_source = source.get("kitty") if isinstance(source.get("kitty"), dict) else {}
    migrated = json.loads(json.dumps(DEFAULT_PREFERENCES))
    for field in PREFERENCE_FIELDS:
        if field == "kitty" or field not in source:
            continue
        try:
            migrated = validate_preferences({field: source[field]}, base=migrated)
        except ValueError:
            continue
    for name in KITTY_PREFERENCE_FIELDS:
        if name in source:
            raw = source[name]
        elif name in kitty_source:
            raw = kitty_source[name]
        else:
            continue
        try:
            migrated = validate_preferences({"kitty": {name: raw}}, base=migrated)
        except ValueError:
            continue
    return migrated


def _migrate_job(job: dict) -> dict:
    migrated = dict(job)
    payload = job.get("payload")
    if isinstance(payload, dict):
        payload = dict(payload)
        payload.pop("capture_path", None)  # manual manifests are gone; capture is automatic
        if isinstance(payload.get("preferences"), dict):
            payload["preferences"] = migrate_preferences(payload["preferences"])
        migrated["payload"] = payload
    return migrated


def _migrate_variant(variant: dict) -> dict:
    migrated = dict(variant)
    if isinstance(variant.get("preferences"), dict):
        migrated["preferences"] = migrate_preferences(variant["preferences"])
    return migrated


def migrate_state(value) -> dict:
    """Convert a schema-1 studio record into the current (schema-4) record.

    The structures a schema-1 record was validated with are taken as they are:
    a malformed one raises instead of being silently dropped, because losing a
    job, a candidate or a mapping is worse than a visible migration failure.
    The design-override and feedback structures start empty: no pre-feature
    record ever held one.
    """
    raw_jobs = value.get("jobs")
    raw_variants = value.get("variants")
    raw_mappings = value.get("mappings")
    if not isinstance(raw_jobs, list) or not isinstance(raw_variants, list) or not isinstance(raw_mappings, dict):
        raise ValueError("schema-1 studio state has malformed jobs/variants/mappings")
    jobs = []
    for job in raw_jobs:
        if not isinstance(job, dict):
            raise ValueError("schema-1 studio state has a malformed job entry")
        jobs.append(_migrate_job(job))
    variants = []
    for variant in raw_variants:
        if not isinstance(variant, dict):
            raise ValueError("schema-1 studio state has a malformed variant entry")
        variants.append(_migrate_variant(variant))
    mappings = {}
    for key, item in raw_mappings.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise ValueError("schema-1 studio state has a malformed mapping entry")
        mappings[key] = item
    migrated = {
        "schema_version": SCHEMA_VERSION,
        "preferences": migrate_preferences(value.get("preferences")),
        "jobs": jobs,
        "variants": variants,
        "mappings": mappings,
        "appearance": value.get("appearance"),
        "last_applied": value.get("last_applied"),
        "sync_error": value.get("sync_error"),
        "design_profiles": {},
        "design_overrides": {},
        "feedback": [],
    }
    return validate_state(migrated)


def migrate_state2(value) -> dict:
    """Convert a schema-2 studio record into the current (schema-4) record.

    Every schema-2 field (jobs, variants, mappings, analysis bindings, profiles,
    preferences, appearance, last_applied, sync_error) is carried over
    unchanged; the design-override map and feedback list are added empty and the
    preferences gain the default design_mode.
    """
    if not isinstance(value, dict):
        raise ValueError("schema-2 studio state is not a JSON object")
    migrated = dict(value)
    migrated["schema_version"] = SCHEMA_VERSION
    migrated["preferences"] = _upgrade_preferences4(value.get("preferences"))
    migrated["design_overrides"] = {}
    migrated["feedback"] = []
    return validate_state(migrated)


def migrate_state3(value) -> dict:
    """Convert a schema-3 studio record into the current (schema-4) record.

    Every schema-3 field (jobs, variants, mappings, profiles, design overrides,
    preferences, appearance, last_applied, sync_error) is carried over
    unchanged; only the feedback list and the preferences design_mode are added.
    Historical profiles and their ids are never rewritten.
    """
    if not isinstance(value, dict):
        raise ValueError("schema-3 studio state is not a JSON object")
    migrated = dict(value)
    migrated["schema_version"] = SCHEMA_VERSION
    migrated["preferences"] = _upgrade_preferences4(value.get("preferences"))
    migrated["feedback"] = []
    return validate_state(migrated)


def _accept_state(data: Path | None, path: Path, value) -> dict:
    if isinstance(value, dict) and value.get("schema_version") in (
        LEGACY_SCHEMA_VERSION, PREVIOUS_SCHEMA_VERSION, SCHEMA3_VERSION,
    ):
        return _migrate_state_file(data, path)
    return validate_state(value)


def _migrate_state_file(data: Path | None, path: Path) -> dict:
    """Migrate the record in place, once, under the state lock, with a backup.

    The lock is re-entered through the reentrant StateLock when a migration is
    triggered from inside ``update_state``; the file is re-read under the lock so
    two processes never migrate the same document twice. Each pre-migration step
    copies its own original bytes before the first write and never overwrites an
    existing backup, so a schema-1 record keeps its schema-1 copy even after the
    later steps run.
    """
    with state_lock(data):
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return default_state()
        except OSError as exc:
            raise ValueError(f"cannot read {path}: {exc}") from exc
        try:
            current = json.loads(raw.decode("utf-8", "surrogateescape"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"malformed JSON in {path}: {exc}") from exc
        version = current.get("schema_version") if isinstance(current, dict) else None
        if version == LEGACY_SCHEMA_VERSION:
            backup = legacy_backup_path(data)
            if not backup.exists():
                atomic_write_bytes(backup, raw)
            migrated = migrate_state(current)
        elif version == PREVIOUS_SCHEMA_VERSION:
            backup = previous_backup_path(data)
            if not backup.exists():
                atomic_write_bytes(backup, raw)
            migrated = migrate_state2(current)
        elif version == SCHEMA3_VERSION:
            backup = schema3_backup_path(data)
            if not backup.exists():
                atomic_write_bytes(backup, raw)
            migrated = migrate_state3(current)
        else:
            return _accept_state(data, path, current)
        write_state(migrated, data)
        return migrated


def read_state(data: Path | None = None) -> dict:
    """Read the studio record. Missing yields canonical defaults, malformed raises.

    A schema-1, schema-2 or schema-3 record is migrated (with its own backup)
    before it is returned, so every consumer sees the current schema.
    """
    path = state_path(data)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return default_state()
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    try:
        value = json.loads(raw.decode("utf-8", "surrogateescape"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed JSON in {path}: {exc}") from exc
    return _accept_state(data, path, value)


def write_state(value: dict, data: Path | None = None) -> None:
    """Atomically replace the studio record; the caller owns the state lock."""
    validate_state(value)
    atomic_json(state_path(data), value)


# ------------------------------------------------------------------------ locks


class StateLock:
    """Exclusive flock on <data>/studio.lock, reentrant within this process."""

    def __init__(self, data: Path | None = None) -> None:
        self.path = lock_path(data)
        self._handle = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> "StateLock":
        key = str(self.path)
        depth = _HELD_LOCKS.get(key, 0)
        if depth:
            _HELD_LOCKS[key] = depth + 1
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError:
            handle.close()
            raise
        self._handle = handle
        _HELD_LOCKS[key] = 1
        return self

    def release(self) -> None:
        key = str(self.path)
        depth = _HELD_LOCKS.get(key, 0)
        if depth > 1:
            _HELD_LOCKS[key] = depth - 1
            return
        if depth == 0:
            return
        _HELD_LOCKS.pop(key, None)
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "StateLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False


def state_lock(data: Path | None = None) -> StateLock:
    return StateLock(data)


def update_state(mutator: Callable[[dict], Any], data: Path | None = None) -> Any:
    """Exclusive read-mutate-write on the studio record; returns the callback result.

    The lock is held only for the mutation. No external process, model call or
    long filesystem walk may be performed inside `mutator`.
    """
    lock = state_lock(data)
    with lock:
        state = read_state(data)
        result = mutator(state)
        write_state(state, data)
        return result
