#!/usr/bin/env python3
"""Common, target-independent design profiles.

A profile says what a wallpaper looks like: the visual analysis, the exact
evidence it was derived from and the palette it was observed against, together
with the user's common artistic preferences at that moment. It deliberately
carries no Kitty geometry, no pack preset and no per-application setting — the
Kitty generator reads the common profile plus ``preferences.kitty`` and records
the profile id it consumed, and any later consumer does the same for its own
target.

A profile is immutable: ``id`` hashes the semantic content (content id, evidence
identity, visual object, palette, common preferences) and excludes timestamps and
provenance, so the same design observed twice is one record and a changed
observation is a separate one. Two runs whose frame *files* or timings differ but
whose frames are byte-identical describe the same design and share an id; a
re-render of a live scene produces different frames and therefore a new profile
instead of silently rewriting the old one.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# Flat modules: studio.py puts backend/ on sys.path.
import storage  # type: ignore

SCHEMA_VERSION = 1
KIND = "wallpaper-studio-design-profile"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_TITLE_CHARS = 200
_MAX_PATH_CHARS = 500


# --------------------------------------------------------------------- helpers


def _copy(value):
    """Deep copy through JSON: the record never aliases a caller's object."""
    return json.loads(json.dumps(value))


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def _text(value, limit: int) -> str:
    return storage.clean_text(value, limit)


def _text_map(value) -> dict:
    if not isinstance(value, dict):
        return {}
    out = {}
    for key, item in value.items():
        if not isinstance(item, str):
            continue
        out[str(key)] = _text(item, 64)
    return out


# --------------------------------------------------------------- design basis

#: The four editable design-basis fields; storage is the single authority.
DESIGN_BASIS_FIELDS = storage.DESIGN_BASIS_FIELDS
_MAX_BASIS_ITEMS = 12
_MAX_BASIS_ITEM_CHARS = 80
_MAX_BASIS_COLORS = 6


def _string_list(value, count: int = _MAX_BASIS_ITEMS, length: int = _MAX_BASIS_ITEM_CHARS) -> list[str]:
    """Cleaned non-empty strings of one visual list field (bounded, never invented)."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for entry in value:
        cleaned = _text(entry, length).strip()
        if cleaned:
            out.append(cleaned)
        if len(out) >= count:
            break
    return out


def _color_list(value) -> str:
    """Readable colors of a visual colors list: hex, role and weight when known."""
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        hex_value = _text(entry.get("hex"), 16).strip()
        if not hex_value:
            continue
        details = []
        role = _text(entry.get("role"), 40).strip()
        if role:
            details.append(role)
        weight = entry.get("weight")
        if not isinstance(weight, bool) and isinstance(weight, (int, float)):
            details.append(f"{round(weight * 100)}%")
        parts.append(hex_value + (f" ({', '.join(details)})" if details else ""))
        if len(parts) >= _MAX_BASIS_COLORS:
            break
    return ", ".join(parts)


def design_source(visual) -> dict:
    """The deterministic design basis a visual analysis provides.

    Every field is always present; a field without evidence is an empty string,
    so a caller never has to invent a value. The mapping is: style/vibe ->
    style_mood, materials/objects/tags -> materials_motifs, temperature/colors ->
    palette_lighting, composition -> composition.
    """
    values = visual if isinstance(visual, dict) else {}
    style = ", ".join(_string_list(values.get("style")))
    vibe = ", ".join(_string_list(values.get("vibe")))
    mood = "; ".join(part for part in (style, vibe) if part)

    materials = ", ".join(_string_list(values.get("materials")))
    motifs = ", ".join(_string_list(values.get("objects")) or _string_list(values.get("tags")))
    material_text = "; ".join(part for part in (materials, motifs) if part)

    lighting = "; ".join(
        part for part in (
            _text(values.get("temperature"), 40).strip(),
            _color_list(values.get("colors")),
        ) if part
    )

    return {
        "style_mood": _text(mood, storage.MAX_DESIGN_BASIS_CHARS),
        "materials_motifs": _text(material_text, storage.MAX_DESIGN_BASIS_CHARS),
        "palette_lighting": _text(lighting, storage.MAX_DESIGN_BASIS_CHARS),
        "composition": _text(values.get("composition"), storage.MAX_DESIGN_BASIS_CHARS).strip(),
    }


def _overlay(source: dict, overrides) -> dict:
    """The source overlaid by stored overrides; key presence, not truthiness.

    Stored overrides are already validated and bounded, so their strings are
    carried verbatim — an explicit empty string stays empty — and the panel's
    acknowledgment always compares the exact text the owner submitted.
    """
    values = dict(source)
    if isinstance(overrides, dict):
        for field in DESIGN_BASIS_FIELDS:
            if field in overrides and isinstance(overrides[field], str):
                values[field] = overrides[field]
    return values


def design_values(visual, overrides) -> dict:
    """The effective design basis of one wallpaper: its analysis source plus the
    owner's overrides. An explicit empty override stays meaningful."""
    return _overlay(design_source(visual), overrides)


def design_basis_snapshot(visual, overrides) -> dict:
    """The `design_basis` block of one inventory item snapshot.

    `available` says whether a usable analysis visual exists at all; the source
    and the effective values stay readable either way, and only the override keys
    the owner actually set are reported, verbatim.
    """
    source = design_source(visual)
    values = _overlay(source, overrides)
    used = {}
    if isinstance(overrides, dict):
        for field in DESIGN_BASIS_FIELDS:
            if field in overrides and isinstance(overrides[field], str):
                used[field] = overrides[field]
    return {
        "available": isinstance(visual, dict) and bool(visual),
        "source": source,
        "values": values,
        "overrides": used,
    }


def palette_of(appearance) -> dict:
    """Design-relevant palette context of an observed appearance (never invented).

    Only the keys that describe the design are kept: volatile observation
    provenance (observed file lists, their mtimes, association bookkeeping) never
    enters a profile and never influences its id.
    """
    palette = appearance.get("palette") if isinstance(appearance, dict) else None
    if not isinstance(palette, dict):
        palette = {}
    return {
        "status": _text(palette.get("status"), 40) or None,
        "source": _text(palette.get("source"), 60) or None,
        "mode": _text(palette.get("mode"), 40) or None,
        "scheme": _text(palette.get("scheme"), 80) or None,
        "scheme_origin": _text(palette.get("scheme_origin"), 80) or None,
        "roles": _text_map(palette.get("roles")),
        "wallpaper_source": _text(palette.get("wallpaper_source"), _MAX_PATH_CHARS) or None,
    }


def evidence_of(analysis) -> dict:
    """The evidence block of a validated analysis envelope, defensively rechecked."""
    evidence = analysis.get("evidence") if isinstance(analysis, dict) else None
    if not isinstance(evidence, dict):
        raise ValueError("analysis has no evidence block")
    kind = evidence.get("kind")
    if not isinstance(kind, str) or not kind:
        raise ValueError("analysis evidence has no kind")
    frames = evidence.get("frames")
    if not isinstance(frames, list) or not frames:
        raise ValueError("analysis evidence has no frames")
    observable = evidence.get("motion_observable")
    if not isinstance(observable, bool):
        raise ValueError("analysis evidence has no motion_observable flag")
    clean_frames = []
    for frame in frames:
        if not isinstance(frame, dict):
            raise ValueError("analysis evidence frames must be objects")
        path = frame.get("file")
        time_s = frame.get("time_s")
        digest = frame.get("sha256")
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("analysis evidence frame file must be an absolute path")
        if isinstance(time_s, bool) or not isinstance(time_s, (int, float)) or time_s < 0:
            raise ValueError("analysis evidence frame time_s must be a nonnegative number")
        if not isinstance(digest, str) or not _SHA256_RE.match(digest):
            raise ValueError("analysis evidence frame sha256 must be a lowercase 64-hex digest")
        clean_frames.append({"file": path, "time_s": float(time_s), "sha256": digest})
    return {"kind": kind, "frames": clean_frames, "motion_observable": observable}


def _evidence_identity(evidence: dict) -> dict:
    """The timestamp-free identity of one evidence block."""
    return {
        "kind": evidence["kind"],
        "motion_observable": evidence["motion_observable"],
        "frames": sorted(frame["sha256"] for frame in evidence["frames"]),
    }


def common_preferences(preferences, notes: str = "", design_basis=None) -> dict:
    """The common artistic preferences a design profile records.

    `notes` is the effective per-attempt note (the same string the generator
    consumes); when it is empty the stored common note is used instead, so the
    profile always describes the brief the design was actually produced for.
    `design_basis`, when given, is stored under ``design_basis`` with all four
    fields: it is the effective per-wallpaper basis of this attempt, so the
    profile id changes when the owner edits it.
    """
    values = preferences if isinstance(preferences, dict) else {}
    effective = _text(notes, storage.MAX_TEXT_CHARS).strip()
    if not effective:
        effective = _text(values.get("notes"), storage.MAX_TEXT_CHARS).strip()
    common = {
        "likes": _text(values.get("likes"), storage.MAX_TEXT_CHARS),
        "dislikes": _text(values.get("dislikes"), storage.MAX_TEXT_CHARS),
        "notes": effective,
    }
    if isinstance(design_basis, dict):
        common["design_basis"] = {
            field: design_basis[field] if isinstance(design_basis.get(field), str) else ""
            for field in DESIGN_BASIS_FIELDS
        }
    return common


# ------------------------------------------------------------------------ api


def build(item, analysis, appearance, preferences, notes="", design_overrides=None) -> dict:
    """Build the common design profile for one analyzed wallpaper.

    `analysis` is a strictly validated analysis envelope, `appearance` an
    observed appearance context (or None), `preferences` the validated studio
    preferences of the attempt and `design_overrides` the per-wallpaper
    overrides frozen when the attempt was queued. The raw visual stays exactly as
    analyzed; the effective design basis (derived source plus overrides) is
    stored under ``preferences.design_basis`` and therefore part of the id.
    Raises ValueError when the inputs cannot describe a design instead of
    producing a partial profile.
    """
    if not isinstance(item, dict):
        raise ValueError("a design profile needs an inventory item")
    if not isinstance(analysis, dict):
        raise ValueError("a design profile needs an analysis envelope")
    content_id = item.get("id") or analysis.get("content_sha256")
    if not isinstance(content_id, str) or not _SHA256_RE.match(content_id):
        raise ValueError("a design profile needs a lowercase 64-hex content id")
    envelope_id = analysis.get("content_sha256")
    if envelope_id is not None and envelope_id != content_id:
        raise ValueError("the analysis envelope belongs to a different content id")
    visual = analysis.get("visual")
    if not isinstance(visual, dict):
        raise ValueError("analysis has no visual object")

    evidence = evidence_of(analysis)
    visual = _copy(visual)
    palette = palette_of(appearance)
    common = common_preferences(preferences, notes, design_values(visual, design_overrides))

    identity = {
        "schema_version": SCHEMA_VERSION,
        "content_id": content_id,
        "evidence": _evidence_identity(evidence),
        "visual": visual,
        "palette": palette,
        "preferences": common,
    }
    profile_id = hashlib.sha256(_canonical(identity)).hexdigest()

    return {
        "schema_version": SCHEMA_VERSION,
        "id": profile_id,
        "content_id": content_id,
        "evidence": evidence,
        "visual": visual,
        "palette": palette,
        "preferences": common,
        "provenance": {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "created_at": storage.now(),
            "model": _text(analysis.get("model"), storage.MAX_MODEL_CHARS),
            "analysis_profile": _text(analysis.get("profile"), 80),
            "evidence_kind": evidence["kind"],
            "wallpaper": {
                "title": _text(item.get("title"), _MAX_TITLE_CHARS),
                "path": _text(item.get("path"), _MAX_PATH_CHARS),
                "provider": _text(item.get("provider"), 80),
                "kind": _text(item.get("kind"), 40),
            },
        },
    }
