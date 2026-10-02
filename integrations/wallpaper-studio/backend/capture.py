#!/usr/bin/env python3
"""Automatic, immutable temporal evidence for the Wallpaper Studio.

One cache record per (wallpaper content identity, render settings). The record
carries real frames with real timestamps and their hashes:

  * image      -> one actual still (ffmpeg normalization);
  * animated   -> six real time-sampled frames (ffprobe duration + ffmpeg);
  * video      -> six real time-sampled frames (ffprobe duration + ffmpeg);
  * scene/web  -> six real frames from ONE continuous renderer, launched as a
                  hidden preview window on the live compositor's
                  `special:agent-tests` workspace and sampled with one x11grab
                  ffmpeg run (capture_scene.py, inside vd-run.sh +
                  dbus-run-session).

Publication is all-or-nothing: frames, manifest and record are written into a
staging directory that is atomically renamed into place only when everything
succeeded. A partial or failed attempt is removed, so a cache directory is
either absent or a complete, trustworthy record.

Source bytes are re-identified (catalog.current_content_id) before and after the
expensive capture: a wallpaper that changed under us is never cached under the
old inventory id.

This module never launches Noctalia, never touches the live compositor state,
the live wallpaper choice, the palette or the config, and never plays audio.
Scene/web capture runs only through capture_scene.py inside `vd-run.sh`; the
only compositor interaction is the hidden preview window it starts and
reclaims, and only that window is ever captured.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import context
import process
import storage

# Bumped whenever the on-disk record semantics change: old records are not read.
# Version 2 is the hidden-window x11grab scene path; every v1 record (including
# the removed nested-compositor scene frames) is invalid by construction.
CAPTURE_VERSION = 2

CAPTURE_DIR = "captures"
RECORD_FILE = "record.json"
MANIFEST_FILE = "manifest.json"
REQUEST_FILE = "_capture_request.json"
RESULT_FILE = "_capture_result.json"

# Evidence kinds, exactly the vocabulary the installed analyzer uses.
STILL_EVIDENCE = "image"
TEMPORAL_EVIDENCE = "video-frames"
RENDER_EVIDENCE = "rendered-frames"

METHOD_STILL = "ffmpeg-still"
METHOD_FRAMES = "ffmpeg-frames"
# One continuous hidden preview window, sampled with a single x11grab run.
METHOD_RENDER = "x11grab-window"

SCENE_KINDS = ("scene", "web")
MEDIA_KINDS = ("image", "video")
SUPPORTED_KINDS = MEDIA_KINDS + SCENE_KINDS
METHODS = (METHOD_STILL, METHOD_FRAMES, METHOD_RENDER)

EVIDENCE_FRAMES = {STILL_EVIDENCE: 1, TEMPORAL_EVIDENCE: 6, RENDER_EVIDENCE: 6}

VD_RUN_ENV = "WALLPAPER_STUDIO_VD_RUN"
DEFAULT_VD_RUN = Path.home() / ".agents" / "skills" / "desktop-sandbox" / "scripts" / "vd-run.sh"

# Whole hidden-window capture is bounded; the child reports its own details.
SCENE_TIMEOUT_S = 240.0
# Process diagnostics are bounded (frames never travel through stdout).
CHILD_OUTPUT_LIMIT = 512 * 1024


def _stage(progress, text: str) -> None:
    if progress is not None:
        progress(text)


def _check_cancel(cancel) -> None:
    if cancel is not None and cancel():
        raise process.Cancelled("capture cancelled")


# --------------------------------------------------------------------- paths


def capture_dir(item: dict, key: str, data: Path | None = None) -> Path:
    """Cache directory of one (content id, render key) capture."""
    return storage.resolve_root(data) / CAPTURE_DIR / str(item["id"]) / key


def render_key(item: dict, options) -> str:
    """Hash over content identity, capture format version and render settings."""
    options = options if isinstance(options, dict) else {}
    payload = {
        "capture_version": CAPTURE_VERSION,
        "content_id": item.get("id"),
        "kind": item.get("kind"),
        "engine": options.get("engine") if isinstance(options.get("engine"), dict) else {},
        "properties": options.get("properties") if isinstance(options.get("properties"), dict) else {},
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# --------------------------------------------------------------------- cache


def _expected_frames(evidence_kind) -> int | None:
    return EVIDENCE_FRAMES.get(evidence_kind)


def _record_shape_ok(record) -> bool:
    """Cheap structural check: no decode, no ffprobe, no renderer startup."""
    if not isinstance(record, dict):
        return False
    kind = record.get("kind")
    count = _expected_frames(kind)
    if count is None:
        return False
    if record.get("method") not in METHODS:
        return False
    if not isinstance(record.get("motion_observable"), bool):
        return False
    if not isinstance(record.get("created_at"), str) or not record["created_at"]:
        return False
    if not isinstance(record.get("options"), dict):
        return False
    frames = record.get("frames")
    if not isinstance(frames, list) or len(frames) != count:
        return False
    previous = None
    for frame in frames:
        if not isinstance(frame, dict):
            return False
        path = frame.get("file")
        if not isinstance(path, str) or not os.path.isabs(path):
            return False
        digest = frame.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            return False
        time_s = frame.get("time_s")
        if not isinstance(time_s, (int, float)) or isinstance(time_s, bool):
            return False
        if previous is not None and not time_s > previous:
            return False
        previous = time_s
        try:
            if not Path(path).is_file() or Path(path).stat().st_size <= 0:
                return False
        except OSError:
            return False
    manifest = record.get("manifest_path")
    if kind == RENDER_EVIDENCE:
        if not isinstance(manifest, str) or not os.path.isabs(manifest):
            return False
        if not Path(manifest).is_file():
            return False
    elif manifest is not None:
        return False
    return True


def cached(item: dict, options, data: Path | None = None) -> dict | None:
    """Return a complete published record, or None. Read-only and cheap."""
    if not isinstance(item, dict) or not isinstance(item.get("id"), str):
        return None
    key = render_key(item, options)
    record = storage.read_json(capture_dir(item, key, data) / RECORD_FILE)
    if not isinstance(record, dict):
        return None
    if record.get("content_id") != item["id"] or record.get("render_key") != key:
        return None
    if not _record_shape_ok(record):
        return None
    return record


# ------------------------------------------------------------------ identity


def _identify(item: dict, when: str) -> None:
    catalog = context.catalog()
    try:
        observed = catalog.current_content_id(item)
    except (ValueError, OSError) as exc:
        raise process.RunFailed(f"content identity check failed ({when}): {exc}") from exc
    if observed != item.get("id"):
        raise process.RunFailed(
            f"content changed since inventory scan ({when}): inventory id {item.get('id')} "
            f"but source now identifies as {observed}; rescan the catalog"
        )


# -------------------------------------------------------------------- ffmpeg


def _ffmpeg(analyzer, argv: list[str], cancel) -> None:
    """Run one bounded, cancellable ffmpeg call built from analyzer constants.

    The flag construction mirrors analyze-wallpapers.extract_frame /
    normalize_still_png (protocol whitelist, bound dimension); it is reproduced
    here only to route the call through the cancellable runner.
    """
    _check_cancel(cancel)
    process.run([str(part) for part in argv], float(analyzer.FFMPEG_TIMEOUT), cancel)


def _scale_filter(analyzer) -> str:
    bound = analyzer.MAX_BOUND_DIM
    return f"scale={bound}:{bound}:force_original_aspect_ratio=decrease"


def _write_still(analyzer, src: Path, out: Path, cancel) -> None:
    argv = [
        "ffmpeg", "-v", "error", "-filter_threads", "1", *analyzer.FFMPEG_PROTOCOL_ARGS,
        "-i", str(src),
        "-frames:v", "1", "-vf", _scale_filter(analyzer),
        "-threads", "2", "-y", str(out),
    ]
    _ffmpeg(analyzer, argv, cancel)
    _require_frame(out, "image normalization")


def _write_frame(analyzer, src: Path, time_s: float, out: Path, cancel) -> None:
    argv = [
        "ffmpeg", "-v", "error", "-filter_threads", "1", *analyzer.FFMPEG_PROTOCOL_ARGS,
        "-ss", f"{time_s:.6f}", "-i", str(src),
        "-frames:v", "1", "-vf", _scale_filter(analyzer),
        "-threads", "2", "-y", str(out),
    ]
    _ffmpeg(analyzer, argv, cancel)
    _require_frame(out, f"frame extraction at t={time_s:.3f}s")


def _require_frame(path: Path, label: str) -> None:
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            raise process.RunFailed(f"ffmpeg produced no {label}: {path}")
    except OSError as exc:
        raise process.RunFailed(f"ffmpeg output unreadable for {label}: {exc}") from exc


def _media_source(item: dict) -> Path:
    raw = item.get("media_path")
    if not isinstance(raw, str) or not os.path.isabs(raw):
        raise process.RunFailed(f"wallpaper {item.get('id')} has no absolute media_path")
    path = Path(raw)
    if not path.is_file():
        raise process.RunFailed(f"media file missing: {path}")
    return path


# ------------------------------------------------------------------- builders


def _still(analyzer, item: dict, staging: Path, cancel):
    src = _media_source(item)
    name = "frame-1.png"
    _write_still(analyzer, src, staging / name, cancel)
    return STILL_EVIDENCE, METHOD_STILL, [name], [0.0]


def _temporal(analyzer, item: dict, staging: Path, cancel):
    src = _media_source(item)
    probe = analyzer.probe_media(src)
    duration = analyzer.media_duration(probe)
    names, times = [], []
    for index, fraction in enumerate(analyzer.VIDEO_FRACTIONS, start=1):
        time_s = duration * fraction
        name = f"frame-{index}.png"
        _write_frame(analyzer, src, time_s, staging / name, cancel)
        names.append(name)
        times.append(time_s)
    return TEMPORAL_EVIDENCE, METHOD_FRAMES, names, times


def _monotonic(times: list[float]) -> list[float]:
    out: list[float] = []
    for index, value in enumerate(times):
        value = float(value)
        if value < 0:
            raise process.RunFailed(f"capture timestamp is negative: {value}")
        if index and value <= out[-1]:
            raise process.RunFailed("capture timestamps are not strictly increasing")
        out.append(value)
    return out


def _render(analyzer, item: dict, options, staging: Path, cancel, progress):
    """Six frames of one continuous hidden preview window, from one x11grab run.

    The child renders an ordinary XWayland preview on the live compositor's
    hidden `special:agent-tests` workspace (through vd-run.sh without a nested
    compositor, and dbus-run-session for a private bus) and samples only that
    window. It needs the live display and the compositor's own IPC identity; a
    session without them cannot host a hidden preview at all.
    """
    wrapper = _vd_run_path()
    child = Path(__file__).resolve().parent / "capture_scene.py"
    if not child.is_file():
        raise process.RunFailed(f"capture_scene.py is unavailable: {child}")
    parent_display = os.environ.get("DISPLAY", "").strip()
    if not parent_display:
        raise process.RunFailed(
            "scene/web capture needs an X11 display for the hidden preview (DISPLAY is unset)"
        )
    parent_signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "").strip()
    if not parent_signature:
        raise process.RunFailed(
            "scene/web capture needs the live compositor "
            "(HYPRLAND_INSTANCE_SIGNATURE is unset)"
        )
    parent_runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if not parent_runtime or not os.path.isabs(parent_runtime):
        raise process.RunFailed(
            "scene/web capture needs the live compositor runtime "
            "(XDG_RUNTIME_DIR is unset)"
        )
    request = staging / REQUEST_FILE
    request.write_text(
        json.dumps({
            "item": item,
            "options": options,
            "output_dir": str(staging),
            "parent_display": parent_display,
            "parent_hypr_signature": parent_signature,
            "parent_runtime": parent_runtime,
        }),
        encoding="utf-8",
    )
    _stage(progress, "render")
    argv = [
        str(wrapper), "dbus-run-session", "--",
        sys.executable, str(child), str(request),
    ]
    _check_cancel(cancel)
    try:
        process.run(argv, SCENE_TIMEOUT_S, cancel, max_output=CHILD_OUTPUT_LIMIT)
    finally:
        request.unlink(missing_ok=True)
    result = storage.read_json(staging / RESULT_FILE)
    if not isinstance(result, dict):
        raise process.RunFailed("scene capture produced no result record")
    raw_frames = result.get("frames")
    if not isinstance(raw_frames, list) or len(raw_frames) != EVIDENCE_FRAMES[RENDER_EVIDENCE]:
        raise process.RunFailed("scene capture must report exactly six frames")
    names, times = [], []
    staging_real = os.path.realpath(str(staging))
    for index, frame in enumerate(raw_frames, start=1):
        if not isinstance(frame, dict):
            raise process.RunFailed("scene capture reported a malformed frame entry")
        raw = frame.get("file")
        time_s = frame.get("time_s")
        if not isinstance(raw, str) or not os.path.isabs(raw):
            raise process.RunFailed("scene capture frame path must be absolute")
        resolved = os.path.realpath(raw)
        if os.path.dirname(resolved) != staging_real:
            raise process.RunFailed(f"scene capture frame escaped its directory: {raw}")
        if not isinstance(time_s, (int, float)) or isinstance(time_s, bool):
            raise process.RunFailed("scene capture frame time_s must be a number")
        _require_frame(Path(resolved), f"scene frame {index}")
        names.append(os.path.basename(resolved))
        times.append(float(time_s))
    (staging / RESULT_FILE).unlink(missing_ok=True)
    return RENDER_EVIDENCE, METHOD_RENDER, names, _monotonic(times)


def _vd_run_path() -> Path:
    raw = os.environ.get(VD_RUN_ENV, "").strip()
    path = Path(os.path.abspath(os.path.expanduser(raw))) if raw else DEFAULT_VD_RUN
    if not path.is_file():
        raise process.RunFailed(
            f"desktop sandbox wrapper unavailable ({VD_RUN_ENV} or default): {path}"
        )
    return path


def _build(item: dict, options, staging: Path, cancel, progress):
    analyzer = context.analyzer_module()
    kind = item.get("kind")
    if kind == "scene" or kind == "web":
        return _render(analyzer, item, options, staging, cancel, progress)
    if kind == "video":
        return _temporal(analyzer, item, staging, cancel)
    if kind == "image":
        src = _media_source(item)
        if analyzer.image_is_temporal(src):
            return _temporal(analyzer, item, staging, cancel)
        return _still(analyzer, item, staging, cancel)
    raise process.RunFailed(f"unsupported wallpaper kind for capture: {kind!r}")


# ------------------------------------------------------------------- records


def _normalized_options(options) -> dict:
    options = options if isinstance(options, dict) else {}
    engine = options.get("engine") if isinstance(options.get("engine"), dict) else {}
    properties = options.get("properties") if isinstance(options.get("properties"), dict) else {}
    return {"engine": dict(engine), "properties": dict(properties)}


def _assemble(analyzer, item: dict, key: str, evidence_kind, method, names, times, staging: Path, final: Path, options) -> tuple:
    frames = []
    for name, time_s in zip(names, times):
        # Hash the real bytes in staging; publish under the final absolute path.
        frames.append({
            "file": str(final / name),
            "time_s": round(float(time_s), 6),
            "sha256": analyzer.sha256_file(staging / name),
        })
    manifest_frames = [{"file": frame["file"], "time_s": frame["time_s"]} for frame in frames]
    hashes = {frame["sha256"] for frame in frames}
    record = {
        "content_id": item["id"],
        "render_key": key,
        "kind": evidence_kind,
        "frames": frames,
        "motion_observable": len(hashes) > 1,
        "manifest_path": str(final / MANIFEST_FILE) if evidence_kind == RENDER_EVIDENCE else None,
        "created_at": storage.now(),
        "options": _normalized_options(options),
        "method": method,
    }
    return record, manifest_frames


def _publish(staging: Path, record: dict, manifest_frames) -> None:
    if record["manifest_path"] is not None:
        # Exactly the shape build_capture_evidence accepts: two keys, absolute
        # still-image paths, strictly increasing finite timestamps.
        storage.atomic_json(
            staging / MANIFEST_FILE,
            {"content_sha256": record["content_id"], "frames": manifest_frames},
        )
    storage.atomic_json(staging / RECORD_FILE, record)
    for frame in record["frames"]:
        _seal(Path(frame["file"]).name, staging)
    _seal(MANIFEST_FILE, staging)
    _seal(RECORD_FILE, staging)


def _seal(name: str, staging: Path) -> None:
    path = staging / name
    if path.is_file():
        try:
            os.chmod(path, 0o444)
        except OSError:
            pass


# ---------------------------------------------------------------------- api


def prepare(item: dict, options, *, progress=None, cancel=None, data: Path | None = None) -> dict:
    """Build (or reuse) the complete capture record for one wallpaper.

    Returns the immutable record; `manifest_path` is an absolute JSON file for
    scene/web and None otherwise. Raises process.Cancelled on cancellation and
    process.RunFailed on any failure; a failed attempt never leaves a cache.
    """
    if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
        raise ValueError("capture.prepare needs an inventory item with a content id")
    kind = item.get("kind")
    if kind not in SUPPORTED_KINDS:
        raise ValueError(f"unsupported wallpaper kind for capture: {kind!r}")

    _check_cancel(cancel)
    # Explicit capture proves source identity before reusing a cached record;
    # only the read-only snapshot path (cached) stays cheap.
    _identify(item, "before capture")
    hit = cached(item, options, data)
    if hit is not None:
        _stage(progress, "cached")
        return hit

    key = render_key(item, options)
    parent = storage.resolve_root(data) / CAPTURE_DIR / str(item["id"])
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=str(parent)))
    try:
        _stage(progress, "capturing")
        evidence_kind, method, names, times = _build(item, options, staging, cancel, progress)
        _check_cancel(cancel)
        # Changed source bytes must never be published under the inventory id.
        _identify(item, "after capture")
        final = parent / key
        if final.exists():
            shutil.rmtree(staging, ignore_errors=True)
            staging = None
            winner = cached(item, options, data)
            if winner is not None:
                _stage(progress, "cached")
                return winner
            raise process.RunFailed(f"capture cache directory is already in use: {final}")
        analyzer = context.analyzer_module()
        record, manifest_frames = _assemble(
            analyzer, item, key, evidence_kind, method, names, times, staging, final, options
        )
        _publish(staging, record, manifest_frames)
        os.rename(str(staging), str(final))
        staging = None
        _stage(progress, "ready")
        return record
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
