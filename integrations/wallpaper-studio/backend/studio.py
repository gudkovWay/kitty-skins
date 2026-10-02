#!/usr/bin/env python3
"""wallpaper-studio backend CLI.

Commands (JSON on stdout for every frontend call):
  snapshot              read-only snapshot: state plus inventory/analysis status
  sync                  provider observation, playback reconcile and auto-apply
  request <json>        queue/dispatch one action, returns a snapshot
  worker                the detached single worker that drains the queue

Actions: the Kitty lifecycle (scan/analyze/analyze_all/generate/cancel/retry/
preferences/approve/reject/apply) plus the playback lifecycle (capture/import/
wallpaper_apply/wallpaper_stop). The frontend never owns a long job: `request`
only records state and starts the detached worker, so a 60s timeout is enough.
No model call, image generation, catalog scan or panel work happens on startup,
snapshot, sync or status, and a scene/web capture happens automatically before
an explicit analysis or generation instead of on its own.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

# The installed backend directory is shared and read by other tools: never drop
# __pycache__ next to these modules (the helpers already load the same way).
sys.dont_write_bytecode = True

sys.path.insert(0, str(Path(__file__).resolve().parent))

import context
import designs
import process
import storage
import usage

SNAPSHOT_SCHEMA_VERSION = 1

ACTIVE_STATES = ("queued", "running")
RETRYABLE_STATES = ("failed", "interrupted", "cancelled", "needs_capture")
#: Kinds whose evidence can only come from a real render (never from the source
#: bytes), so a capture has to exist before the analyzer can run.
CAPTURE_KINDS = ("scene", "web")
#: Kinds the capture module can produce frames for. Image/video captures are for
#: the panel's frame view: the analyzer builds its own still/temporal evidence
#: from the source file and must not be handed a scene manifest.
CAPTUREABLE_KINDS = ("image", "video", "scene", "web")
#: Jobs whose design profiles must freeze the overrides they were queued with, so
#: a later owner edit cannot change an in-flight analysis or generation.
MODEL_JOB_KINDS = ("analyze", "analyze_all", "generate")
INITIAL_STAGE = {
    "scan": "scanning",
    "analyze_all": "scanning",
    "analyze": "analyzing",
    "generate": "analyzing",
    "capture": "capturing",
    "preview": "opening",
    "import": "importing",
    "wallpaper_apply": "applying",
    "wallpaper_stop": "stopping",
}

ANALYZE_TIMEOUT_S = 2400.0
SCAN_TIMEOUT_S = 3600.0
MAX_STAGE_CHARS = 32
MAX_NOTES_CHARS = 2000
MAX_PATH_CHARS = 4096
MAX_ID_CHARS = 64
# Monitor connector names are bounded exactly like the stored preference, so a
# request can never smuggle a longer name through the panel.
MAX_OUTPUT_CHARS = storage.MAX_OUTPUT_CHARS
#: The panel's `output` sentinel for "every connected monitor at click time";
#: `_target_outputs` expands it into concrete connector names before any job.
ALL_OUTPUTS = "ALL"
# Batch jobs report one entry per item they acted on; the list is bounded so a
# huge library cannot grow studio.json without limit. The numeric counts always
# describe the whole batch.
MAX_RESULT_ITEMS = 2000
MAX_SCAN_ERROR_ITEMS = 200
MAX_SCAN_ERROR_CHARS = 500
# The reasoned owner feedback a generate job carries, frozen at queue time: the
# last N entries for the same content and target, so a long history cannot grow
# the job payload without bound.
MAX_JOB_FEEDBACK = 8
# A reconcile pass reports one entry per failed output; bound what is joined
# into the single `sync_error` slot so a wide compositor cannot flood it.
MAX_SYNC_ERROR_ITEMS = 20
# Minimum spacing between persisted intermediate batch results, so a large
# batch rewrites studio.json at a human pace instead of once per item.
PROGRESS_INTERVAL_S = 2.0
_ID_CHARS = set("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_")


# --------------------------------------------------------------------- cli i/o


def _emit(value) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError):
        pass
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _log(data: Path, message: str) -> None:
    line = f"{storage.now()} {storage.clean_text(str(message), 1000)}\n"
    try:
        path = storage.worker_log_path(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass


def main(argv=None) -> int:
    os.umask(0o077)
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if not arguments:
            raise ValueError("usage: studio.py snapshot|sync|request JSON|worker")
        command, rest = arguments[0], arguments[1:]
        data = storage.data_root()
        if command == "worker":
            if rest:
                raise ValueError("worker takes no arguments")
            return _worker_main(data)
        if command == "request":
            if len(rest) != 1:
                raise ValueError("request needs exactly one JSON argument")
            _emit(_cmd_request(data, rest[0]))
            return 0
        if rest:
            raise ValueError(f"{command} takes no arguments")
        if command == "snapshot":
            _emit(_snapshot(data))
            return 0
        if command == "sync":
            _emit(_cmd_sync(data))
            return 0
        raise ValueError(f"unknown command {command!r}")
    except _RequestError as exc:
        _emit({"error": str(exc)})
        return 1
    except process.Cancelled as exc:
        _emit({"error": f"cancelled: {exc}"})
        return 1
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        _emit({"error": storage.clean_text(f"{type(exc).__name__}: {exc}", storage.MAX_ERROR_CHARS)})
        return 1
    except Exception as exc:  # never leak a traceback instead of JSON
        _emit({"error": storage.clean_text(f"internal error: {type(exc).__name__}: {exc}", storage.MAX_ERROR_CHARS)})
        return 1


class _RequestError(RuntimeError):
    """A malformed frontend request."""


def _cmd_request(data: Path, raw: str) -> dict:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _RequestError(f"request is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise _RequestError("request must be a JSON object")
    action = payload.get("action")
    if not isinstance(action, str) or not action:
        raise _RequestError("request needs a string 'action'")
    handler = _ACTIONS.get(action)
    if handler is None:
        raise _RequestError(f"unknown action {action!r}; known: {', '.join(sorted(_ACTIONS))}")
    try:
        return handler(data, payload)
    except _RequestError:
        raise
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        raise _RequestError(storage.clean_text(f"{exc}", storage.MAX_ERROR_CHARS)) from exc


# ----------------------------------------------------------------- state access


def _find_job(state: dict, job_id: str) -> dict | None:
    for job in state["jobs"]:
        if job.get("id") == job_id:
            return job
    return None


def _active_job(state: dict, kind: str, content_id: str | None, payload: dict | None = None) -> dict | None:
    """An already active copy of the same work.

    Playback work is per monitor output; frame previews are per variant; the
    same content for a different design target is different work. Other kinds
    are keyed by their content id.
    """
    for job in state["jobs"]:
        if job.get("state") not in ACTIVE_STATES or job.get("kind") != kind:
            continue
        if (job.get("content_id") or None) != (content_id or None):
            continue
        if payload is not None and (
            ((job.get("payload") or {}).get("target") or designs.DEFAULT_TARGET)
            != (payload.get("target") or designs.DEFAULT_TARGET)
        ):
            continue
        if payload is not None and (job.get("payload") or {}).get("output") != payload.get("output"):
            continue
        if kind == "preview" and (job.get("payload") or {}).get("variant_id") != (payload or {}).get("variant_id"):
            continue
        return job
    return None


def _new_job(kind: str, content_id: str | None, payload: dict) -> dict:
    stamp = storage.now()
    return {
        "id": uuid.uuid4().hex,
        "kind": kind,
        "content_id": content_id,
        "state": "queued",
        "stage": "",
        "created_at": stamp,
        "updated_at": stamp,
        "error": None,
        "cancel_requested": False,
        "payload": payload,
        "result": None,
    }


def _capture_design_overrides(state: dict, content_id: str | None) -> dict:
    """The design-override map a model job runs with, frozen at queue time.

    A batch job carries the whole map so every item keeps its own settings; a
    single-content job carries only that content's entry. An item without
    overrides still gets an entry-free map, so later edits never leak into a job
    that was already queued.
    """
    stored = state.get("design_overrides")
    if not isinstance(stored, dict):
        return {}
    if content_id is None:
        return json.loads(json.dumps(stored))
    overrides = stored.get(content_id)
    if not isinstance(overrides, dict) or not overrides:
        return {}
    return {content_id: json.loads(json.dumps(overrides))}


def _capture_feedback(state: dict, content_id: str | None, target: str) -> list:
    """The reasoned owner feedback a generate job runs with, frozen at queue time.

    Only entries for the same content and target, bearing a non-empty reason,
    are kept; the last MAX_JOB_FEEDBACK in append order are handed on. The job
    carries its own copy, so a later owner decision never rewrites a queued
    generation, and the shared feedback list is never mutated here.
    """
    stored = state.get("feedback")
    if not isinstance(stored, list) or not content_id:
        return []
    selected = [
        entry for entry in stored
        if isinstance(entry, dict)
        and entry.get("content_id") == content_id
        and (entry.get("target") or designs.DEFAULT_TARGET) == target
        and str(entry.get("reason") or "").strip()
    ]
    return json.loads(json.dumps(selected[-MAX_JOB_FEEDBACK:]))


def _enqueue(data: Path, kind: str, content_id: str | None, payload: dict) -> str | None:
    """Append a job unless the same work is already queued or running."""
    job = _new_job(kind, content_id, payload)

    def mutate(state: dict):
        if _active_job(state, kind, content_id, job["payload"]) is not None:
            return None
        job["payload"]["preferences"] = json.loads(json.dumps(state["preferences"]))
        if kind in MODEL_JOB_KINDS:
            job["payload"]["design_overrides"] = _capture_design_overrides(state, content_id)
        if kind == "generate":
            target = designs.require_target(job["payload"].get("target"))
            job["payload"]["target"] = target
            job["payload"]["feedback"] = _capture_feedback(state, content_id, target)
        state["jobs"].append(job)
        return job["id"]

    return storage.update_state(mutate, data)


def _set_stage(data: Path, job_id: str, stage) -> None:
    if not isinstance(stage, str):
        return
    cleaned = "".join(ch for ch in stage.strip().lower() if ch.isalnum() or ch in "_-")[:MAX_STAGE_CHARS]
    if not cleaned:
        return

    def mutate(state: dict):
        job = _find_job(state, job_id)
        if job is None or job.get("state") != "running" or job.get("stage") == cleaned:
            return
        job["stage"] = cleaned
        job["updated_at"] = storage.now()

    try:
        storage.update_state(mutate, data)
    except (ValueError, OSError) as exc:
        _log(data, f"job {job_id}: could not record stage {cleaned!r}: {type(exc).__name__}: {exc}")


def _set_progress(data: Path, job_id: str, result) -> None:
    """Persist an intermediate batch result so the frontend sees real progress.

    The intermediate value uses the same shape as the final result, so a
    frontend can render counts and per-item outcomes while the job runs.
    """
    def mutate(state: dict):
        job = _find_job(state, job_id)
        if job is None or job.get("state") != "running":
            return
        job["result"] = result
        job["updated_at"] = storage.now()

    try:
        storage.update_state(mutate, data)
    except (ValueError, OSError) as exc:
        _log(data, f"job {job_id}: could not record progress: {type(exc).__name__}: {exc}")


def _finish_job(data: Path, job_id: str, state: str, error: str | None, result) -> None:
    def mutate(current: dict):
        job = _find_job(current, job_id)
        if job is None:
            raise ValueError(f"unknown job id {job_id}")
        if job.get("state") != "running":
            # Raced with an explicit cancel of a queued job; keep the terminal record.
            return None
        job["state"] = state
        job["stage"] = ""
        job["updated_at"] = storage.now()
        job["error"] = storage.clean_text(error, storage.MAX_ERROR_CHARS) if error else None
        job["result"] = result
        return state

    storage.update_state(mutate, data)


def _cancel_requested(data: Path, job_id: str) -> bool:
    """Whether this job must stop; an unusable state fails closed.

    A state that cannot be read or validated means the cancellation flag cannot
    be trusted, and work must not continue on that basis: the job stops and the
    failure to record its outcome is logged by the caller.
    """
    try:
        state = storage.read_state(data)
    except (ValueError, OSError):
        return True
    job = _find_job(state, job_id)
    if job is None:
        # Job records are append-only, so a missing id is a broken record.
        return True
    return bool(job.get("cancel_requested"))


def _recover_orphaned_jobs(data: Path) -> int:
    """Mark orphaned running jobs interrupted while owning the worker lease.

    Only the lease holder may decide that a running job is dead — a live worker
    holds it. Recovery never dispatches queued work: a crashed worker's queue
    keeps waiting for an explicit request to start a worker again.
    """
    with storage.state_lock(data):
        lease = process.acquire_worker_lock(data)
        if lease is None:
            return 0
        try:
            return _recover_running_jobs(data)
        finally:
            process.release_worker_lock(lease)


def _recover_running_jobs(data: Path) -> int:
    """Only ever called while holding the worker lease: no live job can be clobbered."""

    def mutate(state: dict):
        count = 0
        for job in state["jobs"]:
            if job.get("state") != "running":
                continue
            job["state"] = "interrupted"
            job["stage"] = ""
            job["updated_at"] = storage.now()
            job["error"] = "the worker stopped while this job was running; retry it explicitly"
            count += 1
        return count

    return storage.update_state(mutate, data)


# ------------------------------------------------------------- inventory bridge


def _inventory_items(data: Path) -> list:
    index = context.inventory(data)
    items = index.get("items") if isinstance(index, dict) else None
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _inventory_by_id(data: Path) -> dict:
    return {
        item["id"]: item
        for item in _inventory_items(data)
        if isinstance(item.get("id"), str)
    }


def _playback_module():
    """The playback module: the single owner of the desktop player."""
    return _sibling("playback")


def _capture_module():
    """The capture module: immutable temporal evidence for the panel and analyzer."""
    return _sibling("capture")


def _item_options(item: dict, state: dict | None = None) -> dict:
    """Effective renderer options of one wallpaper (playback merges its state)."""
    options = _playback_module().effective_options(item, state)
    return options if isinstance(options, dict) else {}


def _item_origin(data: Path, item: dict) -> str:
    """Where a wallpaper came from: a managed import or a library source.

    The imports root lives inside the studio data root, so a path check (not the
    catalog's provider field) is the only reliable signal.
    """
    path = item.get("path")
    if not isinstance(path, str) or not path:
        return "workshop"
    try:
        root = os.path.realpath(str(storage.imports_dir(data)))
        candidate = os.path.realpath(path)
    except OSError:
        return "workshop"
    if candidate == root or candidate.startswith(root + os.sep):
        return "import"
    return "workshop"


def _file_digest(path: Path) -> str | None:
    """sha256 of a stored record; None when it cannot be read."""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _analysis_binding(data: Path, content_id: str) -> dict | None:
    value = storage.read_json(storage.analysis_binding_path(content_id, data))
    return value if isinstance(value, dict) else None


def _record_binding(data: Path, content_id: str, render_key: str) -> None:
    """Bind a freshly written analysis record to the render settings it saw.

    A scene/web analysis is only reusable while this binding still matches the
    live render key and the analysis file it was written for.
    """
    digest = _file_digest(storage.analysis_path(content_id, data))
    if digest is None:
        return
    storage.atomic_json(
        storage.analysis_binding_path(content_id, data),
        {
            "schema_version": 1,
            "content_id": content_id,
            "render_key": render_key,
            "analysis_sha256": digest,
            "at": storage.now(),
        },
    )


def _binding_matches(data: Path, content_id: str, render_key: str) -> bool:
    binding = _analysis_binding(data, content_id)
    if binding is None:
        return False
    if binding.get("render_key") != render_key:
        return False
    digest = _file_digest(storage.analysis_path(content_id, data))
    return digest is not None and digest == binding.get("analysis_sha256")


def _analysis_record(data: Path, content_id) -> tuple[dict | None, str]:
    """Read a stored analysis record once and label it.

    Metadata-only: deliberately no frame rehash and no model call. The analyzer
    re-validates evidence, frame hashes and the visual schema when the record is
    consumed. `recorded` means a full-evidence envelope is present, not verified.
    Every malformed record shape reports `invalid` instead of raising, so the
    snapshot stays cheap *and* total.
    """
    try:
        analyzer = context.analyzer_module()
    except (RuntimeError, OSError, ValueError):
        return None, "invalid"
    if not isinstance(content_id, str) or not analyzer.ID_RE.match(content_id):
        return None, "invalid"
    try:
        raw = storage.analysis_path(content_id, data).read_bytes()
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "invalid"
    try:
        envelope = json.loads(raw.decode("utf-8", "surrogateescape"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "invalid"
    label = _envelope_label(envelope, content_id, analyzer)
    if label == "invalid":
        return None, "invalid"
    return envelope, label


def _analysis_stale(data: Path, item: dict, options: dict) -> bool:
    """Whether a stored analysis describes different render settings.

    Only scene/web evidence depends on renderer settings, and its binding is the
    sole record of them; every other kind is identified by its source bytes.
    """
    if item.get("kind") not in CAPTURE_KINDS:
        return False
    content_id = item.get("id")
    if not isinstance(content_id, str) or not content_id:
        return True
    key = _capture_module().render_key(item, options)
    return not _binding_matches(data, content_id, key)


def _source_identity_current(item: dict) -> bool:
    """Whether the item's source still hashes to its recorded content id.

    The catalog helper is the single content-identity authority (the same
    routine capture and generation use); a missing helper or an unreadable
    source fails closed, so a stored analysis is never reused once identity
    cannot be proven.
    """
    content_id = item.get("id")
    if not isinstance(content_id, str) or not content_id:
        return False
    try:
        return context.catalog().current_content_id(item) == content_id
    except (ValueError, OSError, RuntimeError):
        return False


def _analysis_view(data: Path, item: dict, options: dict) -> tuple[str, dict | None]:
    """(analysis_status, envelope-or-null) for one inventory item.

    A scene/web envelope is only reported as current while its recorded render
    key still matches the live render settings: metadata produced under different
    engine options or properties is reported as `stale` and withheld, never
    silently presented as describing the current wallpaper.
    """
    envelope, label = _analysis_record(data, item.get("id"))
    if envelope is None:
        return label, None
    if _analysis_stale(data, item, options):
        return "stale", None
    return label, envelope


def _envelope_label(envelope, content_id: str, analyzer) -> str:
    """Label an already-read envelope without trusting any field's type."""
    if not isinstance(envelope, dict):
        return "invalid"
    version = envelope.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != analyzer.SCHEMA_VERSION:
        return "invalid"
    if envelope.get("profile") != analyzer.PROFILE:
        return "invalid"
    if envelope.get("content_sha256") != content_id:
        return "invalid"
    if not isinstance(envelope.get("visual"), dict):
        return "invalid"
    evidence = envelope.get("evidence")
    if not isinstance(evidence, dict):
        return "invalid"
    kind = evidence.get("kind")
    if not isinstance(kind, str):
        return "invalid"
    if kind == "preview-only":
        return "preview"
    if kind not in analyzer.FULL_EVIDENCE_KINDS:
        return "invalid"
    return "recorded"


def _strict_analysis(data: Path, content_id, *, require_full: bool) -> tuple[dict | None, str]:
    """Strictly validate a stored analysis record with the installed analyzer.

    The analyzer's own validator is the single authority for execution: it
    re-checks the envelope schema, evidence shape, recorded frame hashes and the
    visual object, so a record no real analysis could have produced is never
    used for paid work. Returns (envelope, "") or (None, reason).
    """
    if not isinstance(content_id, str) or not content_id:
        return None, "no content id"
    try:
        analyzer = context.analyzer_module()
    except (RuntimeError, OSError, ValueError) as exc:
        return None, f"analyzer unavailable: {type(exc).__name__}: {exc}"
    envelope = storage.read_json(storage.analysis_path(content_id, data))
    if not isinstance(envelope, dict):
        return None, "no readable analysis record"
    try:
        analyzer.validate_envelope(envelope, content_id, require_full=require_full)
    except (ValueError, TypeError, KeyError, OSError) as exc:
        return None, storage.clean_text(f"{type(exc).__name__}: {exc}", 300)
    return envelope, ""


def _scan_errors(index) -> list:
    """Bounded path/message list from a validated catalog index."""
    raw = index.get("errors") if isinstance(index, dict) else None
    if not isinstance(raw, list):
        return []
    bounded = []
    for entry in raw[:MAX_SCAN_ERROR_ITEMS]:
        if not isinstance(entry, dict):
            continue
        bounded.append(
            {
                "path": storage.clean_text(entry.get("path"), MAX_PATH_CHARS),
                "message": storage.clean_text(entry.get("message"), MAX_SCAN_ERROR_CHARS),
            }
        )
    return bounded


def _scan_error_count(index) -> int:
    raw = index.get("errors") if isinstance(index, dict) else None
    return len(raw) if isinstance(raw, list) else 0


def _counts(state: dict) -> dict:
    jobs = state["jobs"]
    variants = state["variants"]
    return {
        "queued": sum(1 for job in jobs if job.get("state") == "queued"),
        "running": sum(1 for job in jobs if job.get("state") == "running"),
        "candidates": sum(1 for variant in variants if variant.get("state") == "candidate"),
        "needs_capture": sum(1 for job in jobs if job.get("state") == "needs_capture"),
    }


def _variant_view(data: Path, variant: dict) -> dict:
    """One variant plus its design-profile status.

    Variants produced before common design profiles existed stay usable exactly
    as they are; they are labelled `pre-profile` instead of having a provenance
    they never had.
    """
    entry = dict(variant)
    provenance = variant.get("provenance") if isinstance(variant.get("provenance"), dict) else {}
    profile_id = provenance.get("profile_id")
    entry["profile_status"] = "common" if isinstance(profile_id, str) and profile_id else "pre-profile"
    entry["target"] = variant.get("target") or designs.DEFAULT_TARGET
    entry["render_preview"] = _sibling("frame_preview").cached(variant, data=data)
    return entry


def _item_view(data: Path, item: dict, playback_state: dict, design_overrides: dict | None = None) -> dict:
    entry = dict(item)
    options = _item_options(item, playback_state)
    entry["origin"] = _item_origin(data, item)
    entry["capture"] = _capture_module().cached(item, options)
    status, envelope = _analysis_view(data, item, options)
    entry["analysis_status"] = status
    entry["analysis"] = envelope
    visual = envelope.get("visual") if isinstance(envelope, dict) else None
    entry["design_basis"] = _sibling("profiles").design_basis_snapshot(visual, design_overrides)
    return entry


def _snapshot(data: Path, *, last_action: dict | None = None, state: dict | None = None) -> dict:
    if state is None:
        state = storage.read_state(data)
    playback_state = _playback_module().read_state()
    overrides = state.get("design_overrides") if isinstance(state.get("design_overrides"), dict) else {}
    items = [
        _item_view(data, item, playback_state, overrides.get(item.get("id")))
        for item in _inventory_items(data)
    ]
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "preferences": state["preferences"],
        "jobs": state["jobs"],
        "variants": [_variant_view(data, variant) for variant in state["variants"]],
        "mappings": state["mappings"],
        "appearance": state["appearance"],
        "last_applied": state["last_applied"],
        "sync_error": state["sync_error"],
        "design_profiles": state["design_profiles"],
        "design_targets": designs.targets_snapshot(),
        "design_feedback": state["feedback"],
        "playback": playback_state if isinstance(playback_state, dict) else {},
        "items": items,
        "counts": _counts(state),
        # The durable attempt ledger, with pre-ledger generation jobs surfaced as
        # unknown attempts. Aggregates are known subtotals, never estimates.
        "generation_usage": usage.summary(data, state["jobs"]),
        "last_action": last_action,
    }


# ---------------------------------------------------------------- siblings/worker


def _sibling(name: str):
    """Import a backend sibling on first use; a missing file is an explicit error."""
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise RuntimeError(f"backend module {name}.py is unavailable: {exc}") from exc


def _ensure_worker(data: Path) -> None:
    """Start the detached worker unless one already owns the lease."""
    with storage.state_lock(data):
        lease = process.acquire_worker_lock(data)
        if lease is None:
            return
        process.release_worker_lock(lease)
        try:
            pid = process.spawn_worker(process.worker_argv(), data)
        except OSError as exc:
            raise RuntimeError(f"cannot start the studio worker: {exc}") from exc
        _log(data, f"worker started (pid {pid})")


# ------------------------------------------------------------ request validation


def _require_identifier(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value) > MAX_ID_CHARS:
        raise ValueError(f"{key!r} must be a non-empty identifier string")
    if any(ch not in _ID_CHARS for ch in value):
        raise ValueError(f"{key!r} contains unsupported characters")
    return value


def _require_content_id(data: Path, payload: dict) -> str:
    value = payload.get("content_id")
    if not isinstance(value, str) or not context.content_id_re().match(value):
        raise ValueError("'content_id' must be a lowercase 64-hex content id")
    if value not in _inventory_by_id(data):
        raise ValueError("unknown content_id; scan the wallpaper library first")
    return value


def _require_captureable_kind(item: dict) -> None:
    if item.get("kind") not in CAPTUREABLE_KINDS:
        raise ValueError("this wallpaper kind cannot be captured")


def _reject_capture_path(payload: dict) -> None:
    """The studio captures evidence itself; a caller-supplied manifest is gone."""
    value = payload.get("capture_path")
    if value is None or value == "":
        return
    raise ValueError(
        "manual capture paths are no longer supported: the studio captures "
        "scene/web evidence automatically before analysis"
    )


def _validate_output(value, label: str) -> str:
    """One concrete monitor output name (e.g. DP-2); playback validates it too."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a monitor output name")
    value = value.strip()
    if len(value) > MAX_OUTPUT_CHARS:
        raise ValueError(f"{label} is longer than {MAX_OUTPUT_CHARS} characters")
    if any(ch in value for ch in "/\\\0") or any(ch < " " for ch in value):
        raise ValueError(f"{label} must be a plain monitor output name")
    return value


def _require_output(payload: dict) -> str:
    """The single monitor output a legacy request names in `output`."""
    return _validate_output(payload.get("output"), "'output'")


def _target_outputs(payload: dict) -> list[str]:
    """Every concrete monitor output a playback request targets, in panel order.

    A request names one monitor in `output` (the historical shape) or asks for
    every connected output with ``output == "ALL"`` plus the connector names
    collected at click time in `outputs`. The whole list is validated here,
    before the caller queues anything: one malformed name fails the request
    instead of leaving a half-expanded prefix behind, and duplicates collapse
    while keeping the first occurrence's position. The sentinel itself never
    leaves this function: every caller downstream sees a real connector name.
    """
    raw = payload.get("output")
    if not isinstance(raw, str) or raw.strip() != ALL_OUTPUTS:
        return [_require_output(payload)]
    requested = payload.get("outputs")
    if not isinstance(requested, list) or not requested:
        raise ValueError("'outputs' must be a non-empty list of monitor output names when 'output' is 'ALL'")
    targets: list[str] = []
    for index, value in enumerate(requested):
        name = _validate_output(value, f"'outputs[{index}]'")
        if name == ALL_OUTPUTS:
            raise ValueError("'outputs' must list concrete monitor outputs, never 'ALL'")
        if name not in targets:
            targets.append(name)
    return targets


def _optional_bool(payload: dict, key: str, default: bool) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key!r} must be a boolean")
    return value


def _optional_text(payload: dict, key: str, limit: int) -> str:
    value = payload.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{key!r} must be a string")
    return storage.clean_text(value, limit)


def _optional_path(payload: dict, key: str) -> str | None:
    value = payload.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key!r} must be a string path")
    if len(value) > MAX_PATH_CHARS:
        raise ValueError(f"{key!r} is longer than {MAX_PATH_CHARS} characters")
    value = os.path.expanduser(value)
    if not os.path.isabs(value):
        raise ValueError(f"{key!r} must be an absolute path")
    return value


def _require_supported_model(value) -> None:
    """Reject a vision selector the installed analyzer would refuse."""
    if not isinstance(value, str):
        raise ValueError("preference 'vision_model' must be a non-empty string")
    try:
        allowed = set(context.analyzer_module().ALLOWED_MODELS)
    except (RuntimeError, OSError, ValueError):
        return
    if value not in allowed:
        raise ValueError(f"unsupported vision model {value!r}; choose one of: {', '.join(sorted(allowed))}")


# --------------------------------------------------------------------- actions


def _cmd_sync(data: Path) -> dict:
    """Provider observation, playback reconcile and approved auto-apply.

    Deliberately reports no `last_action`: a periodic refresh is not a command
    the user issued, so it must not raise a success notice in the panel. A real
    playback failure from the reconcile pass is surfaced through `sync_error`
    (only when the library sync left no error of its own), so it stays visible
    in the snapshot/service feedback instead of vanishing into the log.
    """
    recovered = 0
    try:
        recovered = _recover_orphaned_jobs(data)
    except (ValueError, OSError) as exc:
        _log(data, f"sync: orphan recovery failed: {type(exc).__name__}: {exc}")

    playback_error = None
    try:
        summary = _playback_module().reconcile()
    except (RuntimeError, ValueError, OSError) as exc:
        playback_error = f"playback reconcile failed: {type(exc).__name__}: {exc}"
        _log(data, f"sync: {playback_error}")
    else:
        errors = summary.get("errors") if isinstance(summary, dict) else None
        if isinstance(errors, list) and errors:
            playback_error = "playback: " + "; ".join(
                storage.clean_text(str(entry), 300) for entry in errors[:MAX_SYNC_ERROR_ITEMS]
            )

    _sibling("library").sync()
    if playback_error is not None:
        _record_sync_feedback(data, playback_error)
    if recovered:
        _log(data, f"sync: marked {recovered} orphaned running job(s) as interrupted")
    return _snapshot(data)


def _record_sync_feedback(data: Path, message: str) -> None:
    """Record a real playback failure without destroying a library sync error.

    `sync_error` is a single slot: an auto-apply blocker the library wrote is
    the more specific outcome and is kept, so only an otherwise-empty slot is
    filled with the playback failure.
    """
    def mutate(state: dict):
        if state.get("sync_error") is None:
            state["sync_error"] = storage.clean_text(message, storage.MAX_ERROR_CHARS)

    try:
        storage.update_state(mutate, data)
    except (ValueError, OSError) as exc:
        _log(data, f"sync: could not record playback failure: {type(exc).__name__}: {exc}")


def _action_scan(data: Path, payload: dict) -> dict:
    if _enqueue(data, "scan", None, {}) is None:
        return _snapshot(data, last_action={"ok": False, "message": "a scan is already queued"})
    _ensure_worker(data)
    return _snapshot(data, last_action={"ok": True, "message": "scan queued"})


def _action_analyze_all(data: Path, payload: dict) -> dict:
    payload_record = {"preview_only": False, "notes": ""}
    if _enqueue(data, "analyze_all", None, payload_record) is None:
        return _snapshot(data, last_action={"ok": False, "message": "an analysis batch is already queued"})
    _ensure_worker(data)
    return _snapshot(data, last_action={"ok": True, "message": "analysis batch queued"})


def _action_analyze(data: Path, payload: dict) -> dict:
    content_id = _require_content_id(data, payload)
    _reject_capture_path(payload)
    record = {
        "content_id": content_id,
        "preview_only": _optional_bool(payload, "preview_only", False),
        "notes": "",
    }
    if _enqueue(data, "analyze", content_id, record) is None:
        return _snapshot(data, last_action={"ok": False, "message": "this wallpaper is already queued for analysis"})
    _ensure_worker(data)
    return _snapshot(data, last_action={"ok": True, "message": "analysis queued"})


def _action_generate(data: Path, payload: dict) -> dict:
    content_id = _require_content_id(data, payload)
    _reject_capture_path(payload)
    # The design target is validated before anything is queued: a browser target
    # (or any unknown one) fails here, before a worker and before any spend.
    target = designs.require_target(payload.get("target"))
    record = {
        "content_id": content_id,
        "target": target,
        "preview_only": _optional_bool(payload, "preview_only", False),
        "notes": _optional_text(payload, "notes", MAX_NOTES_CHARS),
    }
    if _enqueue(data, "generate", content_id, record) is None:
        return _snapshot(data, last_action={"ok": False, "message": "generation for this wallpaper is already active"})
    _ensure_worker(data)
    return _snapshot(data, last_action={"ok": True, "message": "generation queued"})


def _action_capture(data: Path, payload: dict) -> dict:
    """Queue the evidence capture of one wallpaper (never a model call).

    The request never inspects or hashes a capture itself: the worker runs
    ``capture.prepare``, which re-identifies the source before it may reuse a
    cache entry, so stale bytes can never satisfy an explicit capture.
    """
    content_id = _require_content_id(data, payload)
    item = _inventory_by_id(data).get(content_id) or {}
    _require_captureable_kind(item)
    if _enqueue(data, "capture", content_id, {}) is None:
        return _snapshot(data, last_action={"ok": False, "message": "a capture for this wallpaper is already queued"})
    _ensure_worker(data)
    return _snapshot(data, last_action={"ok": True, "message": "capture queued"})


def _action_import(data: Path, payload: dict) -> dict:
    """Queue a managed import of the file the user picked (never a model call)."""
    source = _optional_path(payload, "path")
    if source is None:
        raise ValueError("import needs an absolute 'path'")
    if not os.path.isfile(source):
        raise ValueError(f"cannot import {source}: not a readable file")
    if _enqueue(data, "import", None, {"path": source}) is None:
        return _snapshot(data, last_action={"ok": False, "message": "an import is already queued"})
    _ensure_worker(data)
    return _snapshot(data, last_action={"ok": True, "message": "import queued"})


def _action_wallpaper_apply(data: Path, payload: dict) -> dict:
    content_id = _require_content_id(data, payload)
    outputs = _target_outputs(payload)
    queued: list[str] = []
    active: list[str] = []
    for output in outputs:
        record = {"content_id": content_id, "output": output}
        if _enqueue(data, "wallpaper_apply", content_id, record) is None:
            active.append(output)
        else:
            queued.append(output)
    if not queued:
        if len(outputs) == 1:
            message = "this wallpaper is already being applied"
        else:
            message = f"this wallpaper is already being applied to {', '.join(active)}"
        return _snapshot(data, last_action={"ok": False, "message": message})
    _ensure_worker(data)
    message = f"applying to {', '.join(queued)}"
    if active:
        message += f"; already applying to {', '.join(active)}"
    return _snapshot(data, last_action={"ok": True, "message": message})


def _action_wallpaper_stop(data: Path, payload: dict) -> dict:
    outputs = _target_outputs(payload)
    queued: list[str] = []
    active: list[str] = []
    for output in outputs:
        if _enqueue(data, "wallpaper_stop", None, {"output": output}) is None:
            active.append(output)
        else:
            queued.append(output)
    if not queued:
        if len(outputs) == 1:
            message = f"stopping {outputs[0]} is already queued"
        else:
            message = f"stopping {', '.join(active)} is already queued"
        return _snapshot(data, last_action={"ok": False, "message": message})
    _ensure_worker(data)
    message = f"stopping {', '.join(queued)}"
    if active:
        message += f"; already queued: {', '.join(active)}"
    return _snapshot(data, last_action={"ok": True, "message": message})


def _action_cancel(data: Path, payload: dict) -> dict:
    job_id = _require_identifier(payload, "job_id")

    def mutate(state: dict):
        job = _find_job(state, job_id)
        if job is None:
            raise ValueError(f"unknown job id {job_id!r}")
        if job.get("state") == "queued":
            # A queued job has not started: cancellation is terminal right now.
            job["state"] = "cancelled"
            job["cancel_requested"] = True
            job["stage"] = ""
            job["updated_at"] = storage.now()
            job["error"] = "cancelled before start"
            return "cancelled queued job"
        if job.get("state") == "running":
            job["cancel_requested"] = True
            job["updated_at"] = storage.now()
            return "requested cancellation"
        raise ValueError(f"job {job_id} is already {job.get('state')}")

    message = storage.update_state(mutate, data)
    # A live worker notices the flag; a crashed worker is replaced and the job is
    # recovered as interrupted instead of looking busy forever.
    _ensure_worker(data)
    return _snapshot(data, last_action={"ok": True, "message": message})


def _action_retry(data: Path, payload: dict) -> dict:
    job_id = _require_identifier(payload, "job_id")
    created: list[str] = []

    def mutate(state: dict):
        job = _find_job(state, job_id)
        if job is None:
            raise ValueError(f"unknown job id {job_id!r}")
        if job.get("state") not in RETRYABLE_STATES:
            raise ValueError(
                f"job {job_id} is {job.get('state')}; only {', '.join(RETRYABLE_STATES)} jobs can be retried"
            )
        if _active_job(state, job.get("kind"), job.get("content_id"), job.get("payload")) is not None:
            return None
        retry = _new_job(job.get("kind"), job.get("content_id"), json.loads(json.dumps(job.get("payload") or {})))
        state["jobs"].append(retry)
        created.append(retry["id"])
        return retry["id"]

    storage.update_state(mutate, data)
    if not created:
        return _snapshot(data, last_action={"ok": False, "message": "the same work is already queued"})
    _ensure_worker(data)
    return _snapshot(data, last_action={"ok": True, "message": "retry queued"})


def _action_preferences(data: Path, payload: dict) -> dict:
    values = payload.get("values")
    if not isinstance(values, dict):
        raise ValueError("preferences needs an object 'values'")
    if not values:
        raise ValueError("preferences needs at least one field in 'values'")
    # The optional per-wallpaper basis patch: a selected content id plus only the
    # edited basis fields. It is validated together with the global values before
    # anything is written, and stored in the same locked mutation, so a rejected
    # field cannot leave preferences or overrides half updated.
    content_id = payload.get("content_id")
    design_basis = None
    if content_id is None:
        if "design_basis" in payload:
            raise ValueError("'design_basis' requires a 'content_id'")
    else:
        content_id = _require_content_id(data, payload)
        design_basis = storage.validate_design_basis(payload.get("design_basis", {}))
    storage.validate_preferences(values, base=storage.read_state(data)["preferences"])
    if isinstance(values.get("vision_model"), str):
        _require_supported_model(values["vision_model"])

    def mutate(state: dict):
        state["preferences"] = storage.validate_preferences(values, base=state["preferences"])
        if content_id is not None and design_basis:
            # Patch only the selected wallpaper; every other entry is preserved.
            overrides = dict(state["design_overrides"].get(content_id) or {})
            overrides.update(design_basis)
            state["design_overrides"][content_id] = overrides
        # The stored auto-apply failure described the previous profile: it is no
        # longer evidence about the current selection.
        state["sync_error"] = None

    storage.update_state(mutate, data)
    return _snapshot(data, last_action={"ok": True, "message": "preferences updated"})


def _action_approve(data: Path, payload: dict) -> dict:
    variant_id = _require_identifier(payload, "variant_id")
    # Validate before the decision so a bad reason has no side effect at all.
    reason = storage.validate_feedback_reason(payload.get("reason"))
    _sibling("library").approve(variant_id, reason)
    return _snapshot(data, last_action={
        "ok": True,
        "message": f"approved variant {variant_id}",
        "variant_id": variant_id,
        "verdict": "approved",
        "reason": reason,
    })


def _action_reject(data: Path, payload: dict) -> dict:
    variant_id = _require_identifier(payload, "variant_id")
    reason = storage.validate_feedback_reason(payload.get("reason"))
    _sibling("library").reject(variant_id, reason)
    return _snapshot(data, last_action={
        "ok": True,
        "message": f"rejected variant {variant_id}",
        "variant_id": variant_id,
        "verdict": "rejected",
        "reason": reason,
    })


def _action_apply(data: Path, payload: dict) -> dict:
    variant_id = _require_identifier(payload, "variant_id")
    _sibling("library").apply(variant_id)
    return _snapshot(data, last_action={"ok": True, "message": f"applied variant {variant_id}"})


def _request_variant(data: Path, payload: dict) -> dict:
    variant_id = _require_identifier(payload, "variant_id")
    for variant in storage.read_state(data)["variants"]:
        if variant.get("id") == variant_id:
            return variant
    raise ValueError("unknown variant; refresh the library")


def _action_preview(data: Path, payload: dict) -> dict:
    # Reset owns the same state lock while removing variants and their files.
    with storage.state_lock(data):
        variant = _request_variant(data, payload)
        designs.require_target(variant.get("target") or designs.DEFAULT_TARGET)
        if _sibling("frame_preview").cached(variant, data=data) is not None:
            return _snapshot(data, last_action={"ok": True, "message": "live frame preview window is open"})
        queued = _enqueue(data, "preview", variant["content_id"], {"variant_id": variant["id"]})
    if queued is None:
        return _snapshot(data, last_action={"ok": False, "message": "this live frame preview is already opening"})
    _ensure_worker(data)
    return _snapshot(data, last_action={"ok": True, "message": "opening the live frame preview window; no model call"})


def _viewer_file(raw, *, root: Path | None = None) -> str:
    if not isinstance(raw, str) or not os.path.isabs(raw):
        raise ValueError("media has no absolute file path")
    path = Path(raw).resolve(strict=True)
    if not path.is_file():
        raise ValueError("media is not a regular file")
    if root is not None and not path.is_relative_to(root.resolve()):
        raise ValueError("cached media escapes its wallpaper directory")
    return str(path)


def _view_capture(data: Path, item: dict, index=0) -> dict:
    if isinstance(index, bool) or not isinstance(index, int):
        raise ValueError("capture index must be an integer")
    capture = _capture_module()
    options = _item_options(item, _playback_module().read_state())
    record = capture.cached(item, options, data=data)
    if not record:
        raise ValueError("no captured frames yet; capture this wallpaper first")
    frames = record["frames"]
    if index < 0 or index >= len(frames):
        raise ValueError("capture index is outside the gallery")
    root = capture.capture_dir(item, record["render_key"], data)
    paths = [_viewer_file(frame["file"], root=root) for frame in frames]
    labels = [
        f"Кадр {position + 1}/{len(frames)} · {frame['time_s']:.2f} с"
        for position, frame in enumerate(frames)
    ]
    return _sibling("viewer").open_media(
        paths, title=str(item.get("title") or "Кадры обоев"),
        content_id=item["id"], kind="gallery", index=index, labels=labels, data=data,
    )


def _action_view(data: Path, payload: dict) -> dict:
    def open_selected():
        target = payload.get("target")
        if target not in ("wallpaper", "capture"):
            raise ValueError("view target must be wallpaper or capture")
        content_id = _require_content_id(data, payload)
        item = _inventory_by_id(data)[content_id]
        if target == "capture":
            return _view_capture(data, item, payload.get("index", 0))
        if item.get("kind") in CAPTURE_KINDS:
            return _view_capture(data, item)
        if item.get("kind") not in ("image", "video"):
            raise ValueError("this wallpaper has no viewable media")
        path = _viewer_file(item.get("media_path"))
        animated = item.get("kind") == "video" or Path(path).suffix.lower() in (".gif", ".apng", ".webp")
        return _sibling("viewer").open_media(
            [path], title=str(item.get("title") or Path(path).name),
            content_id=content_id, kind="video" if animated else "image", data=data,
        )

    # Serialize opening a cached file with reset moving the same cache away.
    _sibling("library")._with_apply_lock(open_selected)
    return _snapshot(data, last_action={"ok": True, "message": "fullscreen viewer opened; Esc to close"})


def _action_reset(data: Path, payload: dict) -> dict:
    content_id = _require_content_id(data, payload)
    result = _sibling("reset").reset_content(
        content_id, confirmed=_optional_bool(payload, "confirm", False), data=data,
    )
    return _snapshot(data, last_action={
        **result, "ok": True, "action": "reset",
        "message": "Оформление и анализ обоев сброшены; резервная копия сохранена",
    })


_ACTIONS = {
    "scan": _action_scan,
    "analyze_all": _action_analyze_all,
    "analyze": _action_analyze,
    "generate": _action_generate,
    "cancel": _action_cancel,
    "retry": _action_retry,
    "preferences": _action_preferences,
    "approve": _action_approve,
    "reject": _action_reject,
    "apply": _action_apply,
    "capture": _action_capture,
    "import": _action_import,
    "wallpaper_apply": _action_wallpaper_apply,
    "wallpaper_stop": _action_wallpaper_stop,
    "preview": _action_preview,
    "view": _action_view,
    "reset": _action_reset,
}



# ---------------------------------------------------------------------- worker


def _worker_main(data: Path) -> int:
    with storage.state_lock(data):
        lease = process.acquire_worker_lock(data)
    if lease is None:
        _log(data, "worker: another worker owns the lease; exiting")
        _emit({"ok": True, "worker": "already-running"})
        return 0
    try:
        _log(data, "worker: started")
        try:
            recovered = _recover_running_jobs(data)
            if recovered:
                _log(data, f"worker: marked {recovered} running job(s) as interrupted")
        except (ValueError, OSError) as exc:
            _log(data, f"worker: recovery failed: {type(exc).__name__}: {exc}")
        try:
            # Any attempt left `started` by a worker that died mid-call belongs to
            # a job that is no longer running; the paid call is accounted as
            # interrupted instead of staying open forever.
            abandoned = usage.reconcile(data, storage.read_state(data).get("jobs"))
            if abandoned:
                _log(data, f"worker: marked {abandoned} generation attempt(s) as interrupted")
        except (ValueError, OSError) as exc:
            _log(data, f"worker: usage reconcile failed: {type(exc).__name__}: {exc}")
        while True:
            lock = storage.state_lock(data)
            lock.acquire()
            try:
                job = _take_next_job(data)
                if job is None:
                    # Wake-safe idle exit: drop the worker lease while the state
                    # lock is still held, so an enqueue that already saw the lock
                    # free will find no worker and start one.
                    process.release_worker_lock(lease)
                    lease = None
            finally:
                lock.release()
            if job is None:
                _log(data, "worker: queue empty; exiting")
                _emit({"ok": True, "worker": "idle"})
                return 0
            _execute_job(data, job)
    finally:
        process.release_worker_lock(lease)


def _take_next_job(data: Path) -> dict | None:
    """Called with the state lock held."""
    state = storage.read_state(data)
    picked = None
    changed = False
    for job in state["jobs"]:
        if job.get("state") != "queued":
            continue
        if job.get("cancel_requested"):
            job["state"] = "cancelled"
            job["stage"] = ""
            job["updated_at"] = storage.now()
            job["error"] = "cancelled before start"
            changed = True
            _log(data, f"job {job['id']} cancelled before start")
            continue
        if picked is not None:
            continue
        job["state"] = "running"
        job["stage"] = INITIAL_STAGE.get(job.get("kind"), "")
        job["updated_at"] = storage.now()
        job["error"] = None
        changed = True
        picked = json.loads(json.dumps(job))
    if changed:
        storage.write_state(state, data)
    return picked


def _execute_job(data: Path, job: dict) -> None:
    _log(data, f"job {job['id']} kind={job['kind']} content={job.get('content_id')} running")
    try:
        state, error, result = _run_job(data, job)
    except process.Cancelled:
        # Keep whatever progress was already recorded so a cancelled batch does
        # not lose the per-item outcomes it had reached.
        state, error, result = "cancelled", "cancelled by user", _recorded_result(data, job["id"])
    except Exception as exc:
        state, error, result = "failed", f"{type(exc).__name__}: {exc}", None
    try:
        _finish_job(data, job["id"], state, error, result)
    except Exception as exc:
        _log(data, f"job {job['id']}: could not record state {state}: {type(exc).__name__}: {exc}")
        return
    _log(data, f"job {job['id']} kind={job['kind']} finished {state}" + (f" error={error}" if error else ""))


def _recorded_result(data: Path, job_id: str):
    """Best-effort read of a job's recorded result (its partial progress)."""
    try:
        state = storage.read_state(data)
    except (ValueError, OSError):
        return None
    job = _find_job(state, job_id)
    return job.get("result") if isinstance(job, dict) else None


def _run_job(data: Path, job: dict):
    kind = job.get("kind")
    if kind == "scan":
        return _job_scan(data, job)
    if kind == "analyze_all":
        return _job_analyze_all(data, job)
    if kind == "analyze":
        return _job_analyze(data, job)
    if kind == "generate":
        return _job_generate(data, job)
    if kind == "capture":
        return _job_capture(data, job)
    if kind == "preview":
        return _job_preview(data, job)
    if kind == "import":
        return _job_import(data, job)
    if kind == "wallpaper_apply":
        return _job_wallpaper_apply(data, job)
    if kind == "wallpaper_stop":
        return _job_wallpaper_stop(data, job)
    raise RuntimeError(f"unsupported job kind {kind!r}")


def _require_job_item(data: Path, job: dict) -> dict | None:
    """The inventory item a job acts on; None once it vanished from the scan."""
    return _inventory_by_id(data).get(job.get("content_id"))


def _job_model(job: dict) -> str:
    preferences = (job.get("payload") or {}).get("preferences")
    if isinstance(preferences, dict):
        model = preferences.get("vision_model")
        if isinstance(model, str) and model.strip():
            return model.strip()
    return storage.DEFAULT_PREFERENCES["vision_model"]


def _job_preferences(data: Path, job: dict) -> dict:
    """The validated preferences a job runs with.

    A job carries the copy of the preferences the request was queued with; only
    a record that predates the copy falls back to the live state.
    """
    preferences = (job.get("payload") or {}).get("preferences")
    if isinstance(preferences, dict):
        return preferences
    try:
        return storage.read_state(data)["preferences"]
    except (ValueError, OSError, KeyError):
        return json.loads(json.dumps(storage.DEFAULT_PREFERENCES))


def _job_design_overrides(job: dict, content_id) -> dict:
    """The design overrides a job was queued with; a pre-feature job has none.

    A batch job carries the whole per-content map, a single job only its own
    entry, so each analyzed wallpaper keeps its own settings.
    """
    payload = job.get("payload")
    captured = payload.get("design_overrides") if isinstance(payload, dict) else None
    if not isinstance(captured, dict) or not isinstance(content_id, str):
        return {}
    overrides = captured.get(content_id)
    return overrides if isinstance(overrides, dict) else {}


def _observed_appearance(data: Path) -> dict:
    """Appearance for a design profile: the last observation, else a fresh read.

    Observation is read-only and never reaches a model; it is only a palette
    context, so the last observed one is as good as a new discovery.
    """
    try:
        state = storage.read_state(data)
    except (ValueError, OSError):
        state = None
    appearance = state.get("appearance") if isinstance(state, dict) else None
    if isinstance(appearance, dict):
        return appearance
    try:
        observed = context.discover()
    except (RuntimeError, ValueError, OSError):
        return {}
    return observed if isinstance(observed, dict) else {}


def _publish_profile(data: Path, item: dict, job: dict, *, appearance, notes: str) -> tuple[dict | None, str]:
    """Persist (or return) the immutable design profile of one explicit attempt.

    The profile is built from the strictly validated analysis record only, so a
    preview-only or broken record produces no profile instead of an invented
    one. The id hashes the semantic content, and the first record for that id is
    canonical: an existing profile is never overwritten with a new provenance,
    and the stored record is what callers get back. Returns (profile, reason);
    reason is empty on success.
    """
    envelope, reason = _strict_analysis(data, item.get("id"), require_full=True)
    if envelope is None:
        return None, f"the analysis record is not usable for a design profile: {reason}"
    try:
        profile = _sibling("profiles").build(
            item,
            envelope,
            appearance,
            _job_preferences(data, job),
            notes,
            design_overrides=_job_design_overrides(job, item.get("id")),
        )
    except (ValueError, RuntimeError, OSError) as exc:
        message = storage.clean_text(f"{type(exc).__name__}: {exc}", storage.MAX_ERROR_CHARS)
        _log(data, f"job {job['id']}: no design profile: {message}")
        return None, f"the design profile could not be built: {message}"

    def mutate(state: dict):
        profiles = state["design_profiles"]
        existing = profiles.get(profile["id"])
        if isinstance(existing, dict):
            return existing  # immutable: keep the canonical stored record
        profiles[profile["id"]] = profile
        return profile

    try:
        stored = storage.update_state(mutate, data)
    except (ValueError, OSError) as exc:
        message = storage.clean_text(f"{type(exc).__name__}: {exc}", storage.MAX_ERROR_CHARS)
        _log(data, f"job {job['id']}: could not store design profile: {message}")
        return None, f"the design profile could not be stored: {message}"
    if not isinstance(stored, dict):
        return None, "the design profile was not stored"
    return stored, ""


def _run_catalog_scan(data: Path, job: dict) -> dict:
    """Scan through the installed catalog CLI, cancellably, and read the index.

    A scan can walk thousands of source files, so it must be interruptible and
    must never run inside this process. The helper CLI stays the scan authority:
    it writes index.json atomically and the validated index is read back through
    the helper, so the result does not depend on the size of the CLI's stdout.
    """
    process.run(
        context.catalog_scan_argv(data),
        SCAN_TIMEOUT_S,
        lambda: _cancel_requested(data, job["id"]),
    )
    index = context.inventory(data)
    return index if isinstance(index, dict) else {}


def _job_scan(data: Path, job: dict):
    _set_stage(data, job["id"], "scanning")
    index = _run_catalog_scan(data, job)
    items = index.get("items")
    scan_errors = _scan_errors(index)
    # A scan records what it found: counts plus the helper's own scan failures.
    # There are no per-item analysis outcomes yet, so `results` stays empty.
    result = {
        "items": len(items) if isinstance(items, list) else 0,
        "errors": _scan_error_count(index),
        "scanned_at": index.get("scanned_at"),
        "results": [],
        "scan_errors": scan_errors,
    }
    if result["errors"]:
        return "failed", f"scan reported {result['errors']} problem(s)", result
    return "succeeded", None, result


def _batch_result(counts: dict, results: list, scan_errors: list) -> dict:
    """Batch result payload: numeric counts, per-item outcomes, scan failures."""
    payload = dict(counts)
    payload["results"] = list(results)
    payload["scan_errors"] = list(scan_errors)
    return payload


def _capture_result(item: dict, record: dict) -> dict:
    frames = record.get("frames") if isinstance(record.get("frames"), list) else []
    return {
        "content_id": item.get("id"),
        "render_key": record.get("render_key"),
        "kind": record.get("kind"),
        "method": record.get("method"),
        "frames": len(frames),
        "manifest_path": record.get("manifest_path"),
    }


def _job_capture(data: Path, job: dict):
    """Capture one wallpaper's evidence: real frames, never a model call."""
    item = _require_job_item(data, job)
    if item is None:
        return "failed", "the selected wallpaper is no longer in the scan inventory; scan again", None
    record, failure = _capture_ready(data, job, item)
    if record is None:
        state, message = failure
        return state, message, None
    return "succeeded", None, _capture_result(item, record)

def _job_preview(data: Path, job: dict):
    variant = _request_variant(data, job.get("payload") or {})
    designs.require_target(variant.get("target") or designs.DEFAULT_TARGET)
    record = _sibling("frame_preview").render(
        variant, cancel=lambda: _cancel_requested(data, job["id"]), data=data,
    )
    return "succeeded", None, {"variant_id": variant["id"], "render_preview": record}



def _match_imported(data: Path, imported: dict) -> str | None:
    """Content id of the freshly indexed managed copy, or None when absent.

    The playback record names the managed project; the catalog names the same
    directory in its own item, so the id is found by path, not by guessing.
    """
    path = imported.get("path")
    if not isinstance(path, str) or not path:
        return None
    target = os.path.realpath(path)
    for item in _inventory_items(data):
        path = item.get("path")
        if not isinstance(path, str) or not path:
            continue
        try:
            candidate = os.path.realpath(path)
        except OSError:
            continue
        if candidate == target:
            return item.get("id")
    return None


def _job_import(data: Path, job: dict):
    """Copy a picked file into the managed library and index it.

    The copy itself belongs to the playback module; the scan afterwards is what
    makes the imported wallpaper selectable, so both run in one cancellable job.
    """
    payload = job.get("payload") or {}
    source = payload.get("path")
    if not isinstance(source, str) or not source:
        return "failed", "the import job has no source path", None
    imported = _playback_module().import_media(
        source,
        progress=lambda stage: _set_stage(data, job["id"], stage),
        cancel=lambda: _cancel_requested(data, job["id"]),
    )
    if not isinstance(imported, dict):
        return "failed", "the import produced no record", None
    _set_stage(data, job["id"], "scanning")
    index = _run_catalog_scan(data, job)
    content_id = _match_imported(data, imported)
    result = {
        "path": imported.get("path"),
        "root": imported.get("root"),
        "title": imported.get("title"),
        "content_id": content_id,
        "items": len(index.get("items")) if isinstance(index.get("items"), list) else 0,
        "scan_errors": _scan_errors(index),
    }
    if content_id is None:
        return "failed", "the managed copy was created but is not in the scan inventory; scan again", result
    return "succeeded", None, result


def _job_wallpaper_apply(data: Path, job: dict):
    """Hand one wallpaper to the desktop player on one output."""
    payload = job.get("payload") or {}
    output = payload.get("output")
    if not isinstance(output, str) or not output:
        return "failed", "the wallpaper_apply job has no output", None
    item = _require_job_item(data, job)
    if item is None:
        return "failed", "the selected wallpaper is no longer in the scan inventory; scan again", None
    record = _playback_module().apply(item, output)
    return "succeeded", None, record if isinstance(record, dict) else None


def _job_wallpaper_stop(data: Path, job: dict):
    """Stop the studio's own player on one output."""
    payload = job.get("payload") or {}
    output = payload.get("output")
    if not isinstance(output, str) or not output:
        return "failed", "the wallpaper_stop job has no output", None
    record = _playback_module().stop(output)
    return "succeeded", None, record if isinstance(record, dict) else None


def _job_analyze_all(data: Path, job: dict):
    job_id = job["id"]
    _set_stage(data, job_id, "scanning")
    index = _run_catalog_scan(data, job)
    scan_errors = _scan_errors(index)
    items = [item for item in _inventory_items(data) if isinstance(item.get("id"), str)]

    counts = {
        "total": 0,
        "analyzed": 0,
        "cache_hit": 0,
        "preview": 0,
        "needs_capture": 0,
        "failed": 0,
        "skipped": 0,
    }
    results: list[dict] = []
    first_error = None

    def record(content_id: str, title, status: str, error: str | None = None) -> None:
        if len(results) >= MAX_RESULT_ITEMS:
            return
        entry = {"id": content_id, "title": title if isinstance(title, str) else "", "status": status}
        if error:
            entry["error"] = error
        results.append(entry)

    # Classify with the analyzer's strict validator before spending anything: a
    # cache hit is a full valid record, and a scene/web record additionally has
    # to be bound to the live render settings. Everything else needs a real
    # analysis, and scene/web items get their capture prepared automatically in
    # the loop below — manual capture manifests no longer exist.
    _set_stage(data, job_id, "classifying")
    playback_state = _playback_module().read_state()
    todo: list[dict] = []
    for item in items:
        if _cancel_requested(data, job_id):
            raise process.Cancelled("cancelled while classifying the analysis batch")
        content_id = item["id"]
        envelope, _reason = _strict_analysis(data, content_id, require_full=True)
        if (
            envelope is not None
            and not _analysis_stale(data, item, _item_options(item, playback_state))
            and _source_identity_current(item)
        ):
            counts["cache_hit"] += 1
            continue
        todo.append(item)
    counts["total"] = len(todo)
    _set_progress(data, job_id, _batch_result(counts, results, scan_errors))

    _set_stage(data, job_id, "analyzing")
    appearance = _observed_appearance(data)
    last_progress = time.monotonic()
    for item in todo:
        if _cancel_requested(data, job_id):
            raise process.Cancelled("cancelled during the analysis batch")
        content_id = item["id"]
        outcome = _analyze_item(data, job, item, preview_only=False)
        status = outcome.get("status")
        error = None
        if status == "analyzed":
            counts["analyzed"] += 1
        elif status == "cache_hit":
            counts["cache_hit"] += 1
        elif status == "preview_only":
            counts["preview"] += 1
        elif status == "needs_capture":
            counts["needs_capture"] += 1
            error = outcome.get("error") or "six-frame capture required"
        elif status == "skipped_busy":
            counts["skipped"] += 1
        else:
            status = "error"
            error = storage.clean_text(
                outcome.get("error") or "analysis failed", storage.MAX_ERROR_CHARS
            )
            counts["failed"] += 1
            first_error = first_error or error
        if status in ("analyzed", "cache_hit"):
            _publish_profile(data, item, job, appearance=appearance, notes="")
        record(content_id, item.get("title"), status, error)
        now = time.monotonic()
        if now - last_progress >= PROGRESS_INTERVAL_S:
            last_progress = now
            _set_progress(data, job_id, _batch_result(counts, results, scan_errors))

    if _cancel_requested(data, job_id):
        raise process.Cancelled("cancelled at the end of the analysis batch")

    result = _batch_result(counts, results, scan_errors)
    attempted = (
        counts["analyzed"] + counts["cache_hit"] + counts["preview"]
        + counts["needs_capture"] + counts["failed"]
    )
    if attempted and counts["failed"] == attempted:
        return "failed", f"all {attempted} analysis attempts failed: {first_error}", result
    # Partial failures, missing captures and scan problems stay visible on the
    # finished batch instead of disappearing behind a succeeded state.
    notes = []
    if counts["failed"]:
        notes.append(f"{counts['failed']} of {attempted} analyses failed: {first_error}")
    if counts["needs_capture"]:
        notes.append(f"{counts['needs_capture']} wallpaper(s) need a six-frame capture")
    if counts["skipped"]:
        notes.append(f"{counts['skipped']} wallpaper(s) were busy in another analysis")
    if scan_errors:
        notes.append(f"the scan reported {_scan_error_count(index)} problem(s)")
    state = "succeeded"
    if counts["failed"] or counts["skipped"] or scan_errors:
        state = "failed"
    elif counts["needs_capture"]:
        state = "needs_capture"
    return state, "; ".join(notes) if notes else None, result


def _capture_ready(data: Path, job: dict, item: dict) -> tuple[dict | None, tuple[str, str] | None]:
    """Guarantee the scene/web capture record the analyzer needs.

    The capture module is the single authority on reuse: ``prepare`` re-identifies
    the source and only then may return an existing record, so a stale cache is
    never handed to the analyzer and no renderer starts for a valid one. Returns
    (record, None) or (None, (job_state, message)). Cancellation propagates.
    """
    capture = _capture_module()
    options = _item_options(item)
    try:
        record = capture.prepare(
            item,
            options,
            progress=lambda stage: _set_stage(data, job["id"], stage),
            cancel=lambda: _cancel_requested(data, job["id"]),
            data=data,
        )
    except (RuntimeError, OSError, ValueError) as exc:
        return None, (
            "failed",
            storage.clean_text(f"capture failed: {exc}", storage.MAX_ERROR_CHARS),
        )
    if not isinstance(record, dict):
        return None, ("failed", "capture produced no evidence record")
    if item.get("kind") in CAPTURE_KINDS and not isinstance(record.get("manifest_path"), str):
        return None, ("needs_capture", "six-frame capture required")
    return record, None


def _analyze_item(data: Path, job: dict, item: dict, *, preview_only: bool) -> dict:
    """Reuse or run the analysis of one wallpaper; returns an outcome dict.

    A scene/web record is only reusable while its render-setting binding still
    matches the live capture key, so metadata produced under different engine
    options is never reused silently. Every reuse additionally requires the
    source to still hash to the item's content id (the catalog helper is the
    identity authority), so a stale inventory entry can never present old bytes
    as the current explicit analysis. Anything else (missing record, stale
    binding, changed source, preview-only request) goes to the installed
    analyzer. Statuses: cache_hit | analyzed | preview_only | needs_capture |
    skipped_busy | error.
    """
    content_id = item.get("id")
    manifest = None
    render_key = None
    if item.get("kind") in CAPTURE_KINDS and not preview_only:
        # prepare re-identifies the source before it may reuse a capture, so the
        # record (and the analysis bound to it) describes the current bytes.
        capture, failure = _capture_ready(data, job, item)
        if failure is not None:
            return {"status": failure[0], "error": failure[1]}
        manifest = capture.get("manifest_path")
        render_key = capture.get("render_key")
        if (
            isinstance(render_key, str)
            and _binding_matches(data, content_id, render_key)
            and _source_identity_current(item)
        ):
            envelope, _reason = _strict_analysis(data, content_id, require_full=True)
            if envelope is not None:
                return {
                    "status": "cache_hit",
                    "analysis": str(storage.analysis_path(content_id, data)),
                }
    elif not preview_only:
        envelope, _reason = _strict_analysis(data, content_id, require_full=True)
        if envelope is not None and _source_identity_current(item):
            return {
                "status": "cache_hit",
                "analysis": str(storage.analysis_path(content_id, data)),
            }

    _set_stage(data, job["id"], "analyzing")
    outcome = _analyze_one(data, job, item, preview_only=preview_only, capture=manifest)
    if render_key and outcome.get("status") == "analyzed":
        _record_binding(data, content_id, render_key)
    return outcome


def _job_analyze(data: Path, job: dict):
    item = _require_job_item(data, job)
    if item is None:
        return "failed", "the selected wallpaper is no longer in the scan inventory; scan again", None
    payload = job.get("payload") or {}
    preview_only = bool(payload.get("preview_only"))
    outcome = _analyze_item(data, job, item, preview_only=preview_only)
    status = outcome.get("status")
    if status in ("analyzed", "cache_hit", "preview_only"):
        profile, reason = _publish_profile(
            data, item, job, appearance=_observed_appearance(data), notes=""
        )
        if profile is None and not preview_only:
            # A full assignment that cannot publish its design profile must fail
            # visibly instead of silently returning a profile-less success.
            return "failed", reason or "no common design profile for this analysis", outcome
        return "succeeded", None, outcome
    if status == "needs_capture":
        return "needs_capture", outcome.get("error") or "six-frame capture required", outcome
    if status == "skipped_busy":
        return "failed", "another analysis is already running for this wallpaper", outcome
    return "failed", outcome.get("error") or "analysis failed", outcome


def _analyze_one(data: Path, job: dict, item: dict, *, preview_only: bool, capture: str | None) -> dict:
    """Run the installed analyzer for one item; returns a bounded outcome dict."""
    content_id = item.get("id")
    argv = context.analyzer_argv(
        data,
        content_id,
        model=_job_model(job),
        preview_only=preview_only,
        capture=capture,
    )

    payload = None
    failure = None
    try:
        completed = process.run(argv, ANALYZE_TIMEOUT_S, lambda: _cancel_requested(data, job["id"]))
        payload = _loads(completed.stdout)
    except process.RunFailed as exc:
        payload = _loads(exc.stdout)
        failure = str(exc)
    except OSError as exc:
        failure = f"{type(exc).__name__}: {exc}"

    entry = None
    if isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, list):
            for candidate in results:
                if isinstance(candidate, dict) and candidate.get("id") == content_id:
                    entry = candidate
                    break
        if entry is None:
            for reported in payload.get("errors") or []:
                if isinstance(reported, dict) and reported.get("id") == content_id and reported.get("error"):
                    failure = failure or str(reported["error"])
                    break
    if entry is None:
        return {
            "status": "error",
            "error": storage.clean_text(failure or "the analyzer produced no result for this wallpaper", storage.MAX_ERROR_CHARS),
        }
    status = entry.get("status")
    if status in ("analyzed", "cache_hit", "preview_only"):
        return {"status": status, "analysis": entry.get("analysis"), "normalizations": entry.get("normalizations")}
    if status == "needs_capture":
        return {"status": "needs_capture", "error": "six-frame capture required"}
    if status == "skipped_busy":
        return {"status": "skipped_busy"}
    message = entry.get("error") or failure or f"analyzer status {status!r}"
    return {"status": "error", "error": storage.clean_text(message, storage.MAX_ERROR_CHARS)}


def _job_generate(data: Path, job: dict):
    payload = job.get("payload") or {}
    preview_only = bool(payload.get("preview_only"))
    notes = payload.get("notes") or ""
    preferences = _job_preferences(data, job)
    # The target is re-validated here as a guard: a job queued before the target
    # existed, or any unavailable target, defaults to frame / fails before spend.
    target = designs.require_target(payload.get("target"))
    feedback = payload.get("feedback")
    if not isinstance(feedback, list):
        feedback = []

    item = _require_job_item(data, job)
    if item is None:
        return "failed", "the selected wallpaper is no longer in the scan inventory; scan again", None
    content_id = item.get("id")

    # Paid work must describe the bytes the inventory recorded: recompute the
    # content id from the source right now and refuse a source that changed.
    try:
        current_id = context.catalog().current_content_id(item)
    except (OSError, ValueError, RuntimeError) as exc:
        return "failed", f"cannot re-identify the wallpaper source: {exc}", None
    if current_id != content_id:
        return "failed", "the wallpaper source changed since the scan; scan and analyze it again", None

    analysis, failure = _ensure_analysis(data, job, item, preview_only=preview_only)
    if analysis is None:
        state, message = failure
        return state, message, None

    if _cancel_requested(data, job["id"]):
        raise process.Cancelled("cancelled before generation started")

    # Appearance is observed fresh for this attempt: a saved snapshot may
    # describe a palette the desktop no longer shows.
    appearance = context.discover()
    # The common design profile of this attempt is durable metadata: it is
    # persisted and handed to the generator, which records its id. A full
    # generation must stop before the paid image call when no profile can be
    # published; a preview-only attempt may intentionally carry none.
    profile, profile_error = _publish_profile(data, item, job, appearance=appearance, notes=notes)
    if profile is None and not preview_only:
        return "failed", profile_error or "no common design profile for this generation", None
    output_dir = storage.frames_dir(data) / str(content_id) / str(job["id"])
    _set_stage(data, job["id"], "generating")
    # Fixed shared signature: item, analysis, appearance, preferences, notes,
    # output_dir, progress, cancel — plus the common design profile.
    generated = _sibling("generator").generate(
        item,
        analysis,
        appearance,
        preferences,
        notes,
        output_dir,
        lambda stage: _set_stage(data, job["id"], stage),
        lambda: _cancel_requested(data, job["id"]),
        profile=profile,
        design_overrides=_job_design_overrides(job, content_id),
        feedback=feedback,
        target=target,
        job_id=job["id"],
    )
    _set_stage(data, job["id"], "publishing")
    # Last barrier before a visible candidate: a cancel that arrived while the
    # image was being produced must not publish. library re-checks the live job
    # atomically while appending and raises Cancelled itself.
    if _cancel_requested(data, job["id"]):
        raise process.Cancelled("cancelled before the candidate was published")
    variant = _sibling("library").add_candidate(item, job, generated, preferences, appearance)
    result = {"variant_id": None, "pack_id": None, "preview_path": None, "profile_id": None,
              "generation_usage": generated.get("generation_usage")}
    if isinstance(variant, dict):
        result = {
            "variant_id": variant.get("id"),
            "pack_id": variant.get("pack_id"),
            "preview_path": variant.get("preview_path"),
            "profile_id": (variant.get("provenance") or {}).get("profile_id"),
        }
    return "succeeded", None, result


def _ensure_analysis(
    data: Path, job: dict, item: dict, *, preview_only: bool
) -> tuple[dict | None, tuple[str, str] | None]:
    """Guarantee a strictly valid analysis envelope before paid generation.

    A valid cache — and, for scene/web, a record still bound to the live render
    settings — produces no model call and no new capture. Anything else is
    analyzed by the installed analyzer, which stays the only authority on
    evidence and model output. Returns (envelope, None) or (None, (state, message)).
    """
    outcome = _analyze_item(data, job, item, preview_only=preview_only)
    status = outcome.get("status")
    if status == "needs_capture":
        return None, ("needs_capture", "six-frame capture required before generation")
    if status == "skipped_busy":
        return None, ("failed", "another analysis is already running for this wallpaper")
    if status not in ("analyzed", "cache_hit", "preview_only"):
        message = outcome.get("error") or "analysis failed; no image was requested"
        return None, ("failed", storage.clean_text(message, storage.MAX_ERROR_CHARS))

    envelope, reason = _strict_analysis(data, item.get("id"), require_full=not preview_only)
    if envelope is None:
        return None, ("failed", f"the analysis record is not usable: {reason}")
    return envelope, None


def _loads(text):
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


if __name__ == "__main__":
    sys.exit(main())
