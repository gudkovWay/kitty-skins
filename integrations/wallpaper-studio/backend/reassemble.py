#!/usr/bin/env python3
"""Offline reassembly of a Wallpaper Studio frame pack from an existing master.

Some packs already installed were built before the aperture was measured: their
manifest declares the thickness preset while the model painted a different
material edge, so the client shows a strip of painted void. Rebuilding them must
not cost another image call and must not touch the approved original.

This entrypoint therefore takes an existing pack directory (or a raw master
image), a destination directory that must not exist yet, and rebuilds a
*separate* complete schema-2 pack from the same immutable pixels. It never calls
the model, never writes into the source and never overwrites anything.

    reassemble.py --source PACK_OR_IMAGE --dest NEW_DIRECTORY \
                  [--material-band PIXELS] [--thickness thin|normal|bold] \
                  [--motion static|candles] [--detail minimal|balanced|ornate] \
                  [--note TEXT]

Without ``--material-band`` the aperture is measured from the master
(:mod:`opening`) and a raster whose opening cannot be measured safely is
refused. With ``--material-band`` the cut is the explicitly reviewed one and the
provenance records ``"mode": "manual-reviewed"`` next to the automatic
measurement's own (accepted or refused) result — a reviewed band is never
presented as an inference.

The source format is decided by the file's magic, not by its extension: legacy
masters are named ``generated.webp`` while actually carrying JPEG bytes, and
both are copied into the new pack under their real format.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# The installed backend directory is shared and read by other tools: never drop
# __pycache__ next to these modules (the helpers already load the same way).
sys.dont_write_bytecode = True

sys.path.insert(0, str(Path(__file__).resolve().parent))

import generator  # type: ignore
import storage  # type: ignore

__all__ = ["main", "reassemble"]

#: Generated-master file prefix the generator writes into every pack.
MASTER_PREFIX = "generated."
FIELD_FLAGS = (
    ("--source", "source"),
    ("--dest", "dest"),
    ("--material-band", "material_band"),
    ("--thickness", "thickness"),
    ("--motion", "motion"),
    ("--detail", "detail"),
    ("--note", "note"),
)


class ReassembleError(RuntimeError):
    """A user-visible reassembly failure; the message is safe to show."""


def _usage() -> str:
    return (
        "usage: reassemble.py --source PACK_OR_IMAGE --dest NEW_DIRECTORY "
        "[--material-band PIXELS] [--thickness thin|normal|bold] "
        "[--motion static|candles] [--detail minimal|balanced|ornate] [--note TEXT]"
    )


def _emit(value) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError):
        pass
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    sys.stdout.flush()


# ------------------------------------------------------------------ arguments


def _parse(argv: list[str]) -> dict:
    flags = dict(FIELD_FLAGS)
    values: dict = {"note": ""}
    index = 0
    if not argv:
        raise ReassembleError(_usage())
    while index < len(argv):
        argument = argv[index]
        name, separator, inline = argument.partition("=")
        key = flags.get(name)
        if key is None:
            raise ReassembleError(f"unknown argument {argument!r}\n{_usage()}")
        if separator:
            value = inline
        else:
            index += 1
            if index >= len(argv):
                raise ReassembleError(f"{name} needs a value\n{_usage()}")
            value = argv[index]
        values[key] = value
        index += 1

    for key in ("source", "dest"):
        if not str(values.get(key) or "").strip():
            raise ReassembleError(f"{key.replace('_', '-')} is required\n{_usage()}")
    for key, allowed in (
        ("thickness", tuple(generator.BAND_BY_THICKNESS)),
        ("motion", generator.MOTION_VALUES),
        ("detail", generator.DETAIL_VALUES),
    ):
        value = values.get(key)
        if value is not None and value not in allowed:
            raise ReassembleError(f"{key} must be one of: {', '.join(allowed)}")
    band = values.get("material_band")
    if band is not None:
        try:
            values["material_band"] = int(str(band).strip())
        except ValueError as error:
            raise ReassembleError(f"--material-band needs a whole number of pixels, got {band!r}") from error
    return values


# -------------------------------------------------------------------- sources


def _master_of(source: Path) -> tuple[Path, dict]:
    """The immutable raw master and the provenance of its pack, if it has one."""
    if source.is_dir():
        record = storage.read_json(source / "source.json")
        provenance = record if isinstance(record, dict) else {}
        names = sorted(name for name in os.listdir(source) if name.startswith(MASTER_PREFIX))
        if not names:
            raise ReassembleError(f"{source} holds no {MASTER_PREFIX}* master to reassemble")
        if len(names) > 1:
            raise ReassembleError(
                f"{source} holds more than one generated master ({', '.join(names)}); pass the file directly"
            )
        return source / names[0], provenance
    if not source.is_file():
        raise ReassembleError(f"source {source} is neither a pack directory nor an image file")
    return source, {}


def _context(provenance: dict) -> dict:
    """The design context a repaired pack records, taken from the original.

    Nothing is invented: a field the original pack does not carry stays empty,
    and the pack identity is derived from the same content id and the new
    destination, so a repaired sibling never claims to be the original.
    """
    wallpaper = provenance.get("wallpaper") if isinstance(provenance.get("wallpaper"), dict) else {}
    analysis = provenance.get("analysis") if isinstance(provenance.get("analysis"), dict) else {}
    appearance = provenance.get("appearance") if isinstance(provenance.get("appearance"), dict) else {}
    design_basis = provenance.get("design_basis") if isinstance(provenance.get("design_basis"), dict) else None
    roles = appearance.get("roles") if isinstance(appearance.get("roles"), dict) else {}
    palette = {"source": appearance.get("source"), "mode": appearance.get("mode"), "roles": roles}
    summary = analysis.get("summary") if isinstance(analysis.get("summary"), str) else ""
    kind = analysis.get("evidence_kind") if isinstance(analysis.get("evidence_kind"), str) else ""
    visual = {"summary": summary} if summary else {}
    evidence = {"kind": kind} if kind else {}
    recorded = provenance.get("kitty") if isinstance(provenance.get("kitty"), dict) else {}
    kitty = {key: recorded[key] for key in ("detail", "thickness", "motion") if isinstance(recorded.get(key), str)}
    preferences = provenance.get("preferences") if isinstance(provenance.get("preferences"), dict) else {}
    content_id = provenance.get("content_id") if isinstance(provenance.get("content_id"), str) else None
    return {
        "item": {
            "id": content_id,
            "title": wallpaper.get("title"),
            "path": wallpaper.get("path"),
            "provider": wallpaper.get("provider"),
        },
        "analysis": {
            "profile": analysis.get("profile"),
            "model": analysis.get("model"),
            "evidence": evidence,
            "visual": visual,
            "content_sha256": content_id,
        },
        "appearance": {"palette": palette},
        "reference": {
            "visual": visual,
            "palette": palette,
            "evidence": evidence,
            "profile_id": provenance.get("profile_id"),
            "design_basis": design_basis if isinstance(design_basis, dict) else {},
            "basis_recorded": isinstance(design_basis, dict),
        },
        "preferences": preferences,
        "kitty": kitty,
    }


# ------------------------------------------------------------------- assembly


def reassemble(values: dict) -> dict:
    """Rebuild one pack from an existing master into a fresh directory."""
    source = Path(os.path.abspath(os.path.expanduser(str(values["source"]))))
    dest = Path(os.path.abspath(os.path.expanduser(str(values["dest"]))))
    if not source.exists():
        raise ReassembleError(f"source {source} does not exist")
    if dest.exists():
        raise ReassembleError(f"destination {dest} already exists; reassembly never overwrites anything")
    if source.is_dir() and dest.is_relative_to(source.resolve()):
        raise ReassembleError("the destination must live outside the source pack")

    master, provenance = _master_of(source)
    context = _context(provenance)
    for key in ("detail", "thickness", "motion"):
        if values.get(key) is not None:
            context["kitty"][key] = values[key]
    notes = values.get("note") or (context["preferences"].get("notes") or "")
    reviewed_band = values.get("material_band")

    if not master.is_file():
        raise ReassembleError(f"master {master} is not a readable file")

    generator._preflight(context["kitty"].get("motion", "static"), require_child=False)
    dest.mkdir(parents=True)
    generated_name, generated_sha = generator._copy_generated(master, dest)

    original = provenance.get("generated") if isinstance(provenance.get("generated"), dict) else {}
    generated = {
        "tool": original.get("tool"),
        "provider": original.get("provider"),
        "model": original.get("model"),
        "prompt_sha256": original.get("prompt_sha256"),
        "file": generated_name,
        "sha256": generated_sha,
    }
    assembly = provenance.get("assembly") if isinstance(provenance.get("assembly"), dict) else {}
    repair = {
        "tool": "reassemble.py",
        "source": str(master),
        "source_kind": "pack" if source.is_dir() else "raw",
        "source_pack_id": provenance.get("id"),
        "source_band": assembly.get("band"),
        "source_aperture": assembly.get("aperture"),
        "reviewed_band": reviewed_band,
        "note": storage.clean_text(str(notes), storage.MAX_TEXT_CHARS),
        "repaired_at": storage.now(),
    }

    result = generator.assemble_pack(
        master,
        dest,
        item=context["item"],
        analysis=context["analysis"],
        appearance=context["appearance"],
        reference=context["reference"],
        preferences=context["preferences"],
        notes=notes,
        kitty=context["kitty"],
        generated=generated,
        reviewed_band=reviewed_band,
        repair=repair,
    )
    geometry = result["provenance"].get("geometry") or {}
    return {
        "pack_id": result["pack_id"],
        "name": result["name"],
        "pack_path": result["pack_path"],
        "preview_path": result["preview_path"],
        "motion": result["motion"],
        "mode": geometry.get("mode"),
        "requested_band": geometry.get("requested_band"),
        "measured_band": geometry.get("measured_band"),
        "reviewed_band": geometry.get("reviewed_band"),
        "applied_band": geometry.get("applied_band"),
        "adaptive_scale": geometry.get("adaptive_scale"),
        "measurement": geometry.get("measurement"),
        "measurement_error": geometry.get("measurement_error"),
        "source": str(master),
        "provenance": result["provenance"],
    }


def main(argv=None) -> int:
    os.umask(0o077)
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        values = _parse(arguments)
        result = reassemble(values)
    except ReassembleError as error:
        _emit({"error": storage.clean_text(str(error), storage.MAX_ERROR_CHARS)})
        return 1
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        _emit({"error": storage.clean_text(f"{type(error).__name__}: {error}", storage.MAX_ERROR_CHARS)})
        return 1
    except Exception as error:  # never leak a traceback instead of JSON
        _emit({"error": storage.clean_text(f"internal error: {type(error).__name__}: {error}", storage.MAX_ERROR_CHARS)})
        return 1
    _emit(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
