#!/usr/bin/env python3
"""wallpaper-studio backend CLI.

Commands (JSON on stdout for every frontend call):
  snapshot              read-only snapshot: state plus inventory/analysis status
  sync                  provider observation, playback reconcile and auto-apply
  request <json>        queue/dispatch one action, returns a snapshot
  worker                the detached background worker that drains its lane
  worker playback       the detached playback worker: wallpaper apply/stop only

Actions: the Kitty lifecycle (scan/analyze/analyze_all/generate/cancel/retry/
preferences/approve/reject/apply) plus the playback lifecycle (capture/import/
wallpaper_apply/wallpaper_stop). The frontend never owns a long job: `request`
only records state and starts the detached worker of the job's lane, so a 60s
timeout is enough. Two independent lanes (background and playback, each with
its own lease) keep a wallpaper apply from waiting behind a paid generation.
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
import theme_store
import theme_guidance
import theme_workflow
import theme_primary

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
#: The Noctalia design target id of the Recipe v1/apply contract (designs owns it).
NOCTALIA_TARGET = designs.NOCTALIA
#: A Noctalia generate request queues one job per candidate ordinal, 1..4.
MAX_BATCH_COUNT = 4
#: Terminal states that stop the rest of a Noctalia batch from spending; a
#: missing capture is terminal for the batch because every sibling would hit it.
BATCH_FAILED_STATES = ("failed", "cancelled", "interrupted", "needs_capture")
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

#: The playback lane runs only wallpaper apply/stop; every other kind belongs to
#: the background lane. The two lanes have independent workers and leases, so an
#: apply is never queued behind a paid generation.
PLAYBACK_KINDS = ("wallpaper_apply", "wallpaper_stop")
LANE_DEFAULT = process.LANE_DEFAULT
LANE_PLAYBACK = process.LANE_PLAYBACK


def _job_lane(kind) -> str:
    """Which worker lane owns a job kind (unknown kinds are background work)."""
    return LANE_PLAYBACK if kind in PLAYBACK_KINDS else LANE_DEFAULT

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
        if command == "theme-import":
            if len(rest) != 1:
                raise ValueError("theme-import requires one legacy directory")
            result = theme_store.import_legacy(Path(rest[0]), data)
            theme_guidance.seed(data, Path(__file__).parent.parent / "resources/theme-library")
            _emit(result)
            return 0
        if command == "theme-quote":
            if len(rest) != 1:
                raise ValueError("theme-quote requires one JSON request")
            _emit(theme_workflow.quote(json.loads(rest[0])))
            return 0
        if command == "theme-library":
            if rest:
                raise ValueError("theme-library takes no arguments")
            _emit(theme_store.Store(data).browse())
            return 0
        if command == "worker":
            if len(rest) > 1 or (rest and rest[0] != LANE_PLAYBACK):
                raise ValueError("usage: worker [playback]")
            return _worker_main(data, rest[0] if rest else LANE_DEFAULT)
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
        if kind == "generate" and (job.get("payload") or {}).get("target") == NOCTALIA_TARGET:
            # A Noctalia batch is one job per candidate ordinal: the batch nonce
            # and the ordinal are part of the work identity, so N distinct
            # children never dedupe into one while an identical child still does.
            if (job.get("payload") or {}).get("batch_id") != (payload or {}).get("batch_id"):
                continue
            if (job.get("payload") or {}).get("ordinal") != (payload or {}).get("ordinal"):
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


def _noctalia_variant(state: dict, variant_id: str) -> dict | None:
    """One stored Noctalia variant by its id, or None.

    The stored record uses the Kitty variant contract's ``id`` key (the catalog
    view exposes it as ``variant_id``); ``variant_id`` is never a storage key.
    """
    section = state.get("noctalia")
    variants = section.get("variants") if isinstance(section, dict) else None
    if not isinstance(variants, list):
        return None
    for variant in variants:
        if isinstance(variant, dict) and variant.get("id") == variant_id:
            return variant
    return None


def _capture_parent_recipe(state: dict, payload: dict) -> None:
    """Freeze the parent recipe a refine was queued from, at queue time.

    A refine request names a parent variant; the exact recipe it consumed is
    copied into the job payload, so a later re-install or rejection can never
    rewrite the revision the owner asked to refine.
    """
    parent_id = payload.get("parent_variant_id")
    if not parent_id:
        return
    parent = _noctalia_variant(state, parent_id)
    if parent is None:
        raise ValueError(f"unknown Noctalia parent variant {parent_id!r}")
    recipe = parent.get("recipe")
    if not isinstance(recipe, dict) or not recipe:
        raise ValueError(f"the parent variant {parent_id!r} carries no frozen recipe")
    frozen_recipe = payload.get("parent_recipe")
    if isinstance(frozen_recipe, dict) and recipe != frozen_recipe:
        raise ValueError(f"the parent variant {parent_id!r} recipe changed while queueing; refresh and retry")
    payload["parent_recipe"] = json.loads(json.dumps(recipe))
    # The parent's whole-skin files were read and hashed OUTSIDE this lock; here
    # the exact stored record must still be the one that was verified, so the
    # owner never refines against a package that changed while queueing.
    stored_presentation = parent.get("presentation")
    frozen = payload.get("parent_presentation")
    if isinstance(frozen, dict):
        if stored_presentation != frozen:
            raise ValueError(
                f"the parent variant {parent_id!r} changed while the refine was being "
                "queued; refresh and retry")
    elif isinstance(stored_presentation, dict) and "legacy" not in stored_presentation:
        raise ValueError(
            f"the parent variant {parent_id!r} carries an unverified whole-skin "
            "package; refresh the chooser and retry")


def _enqueue(data: Path, kind: str, content_id: str | None, payload: dict) -> str | None:
    """Append a job unless the same work is already queued or running."""
    job = _new_job(kind, content_id, payload)
    if kind == "generate" and content_id:
        target = designs.require_target(payload.get("target"))
        theme_guidance.freeze(storage.generation_dir(content_id, job["id"], data), target, base=data)
        theme_primary.freeze(storage.generation_dir(content_id, job["id"], data), content_id, base=data)

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
            if target == NOCTALIA_TARGET:
                _capture_parent_recipe(state, job["payload"])
        state["jobs"].append(job)
        return job["id"]

    return storage.update_state(mutate, data)


def _enqueue_noctalia_batch(data: Path, content_id: str, records: list[dict]) -> list[str]:
    """Freeze and publish every child together; a stale parent queues nothing."""
    jobs = [_new_job("generate", content_id, record) for record in records]
    for job in jobs:
        theme_guidance.freeze(storage.generation_dir(content_id, job["id"], data), "noctalia", base=data)
        theme_primary.freeze(storage.generation_dir(content_id, job["id"], data), content_id, base=data)

    def mutate(state: dict):
        if any(_active_job(state, "generate", content_id, job["payload"]) is not None for job in jobs):
            return []
        for job in jobs:
            payload = job["payload"]
            payload["preferences"] = json.loads(json.dumps(state["preferences"]))
            payload["design_overrides"] = _capture_design_overrides(state, content_id)
            payload["feedback"] = _capture_feedback(state, content_id, NOCTALIA_TARGET)
            _capture_parent_recipe(state, payload)
        state["jobs"].extend(jobs)
        return [job["id"] for job in jobs]

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
    """Mark orphaned running jobs interrupted per lane, owning each lease.

    Only a lane's lease holder may decide that its running job is dead — a live
    worker holds it. Each lane is checked independently while its own lease is
    free; the playback lane is additionally gated on the background lease being
    free, because a live (legacy) background worker may still be executing a
    playback job while holding only ``worker.lock``. Recovery never dispatches
    queued work and never launches a worker.
    """
    total = 0
    with storage.state_lock(data):
        default_lease = process.acquire_worker_lock(data, lane=LANE_DEFAULT)
        playback_lease = process.acquire_worker_lock(data, lane=LANE_PLAYBACK)
        try:
            if default_lease is not None:
                total += _recover_running_jobs(data, LANE_DEFAULT)
            if playback_lease is not None and default_lease is not None:
                # No background worker is alive: a running playback job is dead.
                total += _recover_running_jobs(data, LANE_PLAYBACK)
        finally:
            process.release_worker_lock(playback_lease)
            process.release_worker_lock(default_lease)
    return total


def _recover_running_jobs(data: Path, lane: str) -> int:
    """Mark this lane's running jobs interrupted.

    Only ever called while holding that lane's lease and, for the playback lane,
    while no background worker owns ``worker.lock``: no live job of any lane can
    be clobbered, and an opposite-lane job is left untouched.
    """

    def mutate(state: dict):
        count = 0
        for job in state["jobs"]:
            if job.get("state") != "running":
                continue
            if _job_lane(job.get("kind")) != lane:
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
    if type(version) is not int or (version, envelope.get("profile")) not in ((1, "visual-v1"), (2, "visual-v2")):
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


def _variant_view(data: Path, variant: dict, *, live_catalog: dict | None = None) -> dict:
    """One variant plus its design-profile status.

    Variants produced before common design profiles existed stay usable exactly
    as they are; they are labelled `pre-profile` instead of having a provenance
    they never had.

    A Noctalia variant carrying a raw whole-skin presentation is additionally
    projected through the presentation helper against the LIVE catalog, so an
    incomplete, legacy or stale package stays inspectable with its reason
    instead of looking like full coverage.
    """
    entry = dict(variant)
    provenance = variant.get("provenance") if isinstance(variant.get("provenance"), dict) else {}
    profile_id = provenance.get("profile_id")
    entry["profile_status"] = "common" if isinstance(profile_id, str) and profile_id else "pre-profile"
    entry["target"] = variant.get("target") or designs.DEFAULT_TARGET
    entry["render_preview"] = _sibling("frame_preview").cached(variant, data=data)
    presentation = variant.get("presentation")
    if entry["target"] == NOCTALIA_TARGET and isinstance(presentation, dict) \
            and "legacy" not in presentation:
        try:
            entry["presentation"] = _presentation_module().presentation_view(
                presentation, catalog=live_catalog)
        except (ValueError, TypeError) as exc:
            # A malformed stored package never crashes the whole snapshot; it
            # surfaces as an explicitly incomplete presentation.
            entry["presentation"] = {
                "schema_version": 1,
                "complete": False,
                "coverage_current": False,
                "legacy": False,
                "scene": None,
                "surfaces": [],
                "nonvisual": [],
                "inventory_digest": "",
                "apply_ready": False,
                "unavailable_reason": storage.clean_text(
                    f"invalid stored presentation: {exc}", storage.MAX_ERROR_CHARS),
                "runtime_verified": False,
            }
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


def _seed_noctalia_candidates(data: Path, items: list) -> None:
    """Best-effort local seeding of the builtin Noctalia candidates.

    Seeding uses product previews and recipes only: no model call, no IPC apply,
    no capture. It must never make the shared snapshot fail, so an unavailable
    source is logged and the capability/catalog still report what is stored.
    """
    try:
        _sibling("noctalia_bundle").ensure_builtin_candidates(data, items)
    except Exception as exc:
        # Seeding is local product art, not the shared status path's business:
        # any failure is logged and the catalog reports what is actually stored,
        # so a broken seed can never break the frame/Kitty snapshot.
        _log(data, f"noctalia: builtin candidate seeding failed: {type(exc).__name__}: {exc}")


def _observed_content_id(state: dict) -> str | None:
    """The content id of the wallpaper the last observation saw, when unambiguous."""
    appearance = state.get("appearance") if isinstance(state.get("appearance"), dict) else {}
    active = appearance.get("active")
    if not isinstance(active, list) or not active:
        return None
    contents: set[str] = set()
    for entry in active:
        if not isinstance(entry, dict) or entry.get("status") != "observed":
            return None
        content = entry.get("content_sha256")
        if not isinstance(content, str) or not content:
            return None
        contents.add(content)
    return next(iter(contents)) if len(contents) == 1 else None


def _noctalia_jobs_view(state: dict) -> dict:
    """Batch-level status of Noctalia generation jobs for the panel/chooser."""
    batches: dict[str, dict] = {}
    counts = {"queued": 0, "running": 0, "failed": 0, "cancelled": 0}
    for job in state["jobs"]:
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        if job.get("kind") != "generate" or payload.get("target") != NOCTALIA_TARGET:
            continue
        batch_id = payload.get("batch_id")
        if not isinstance(batch_id, str) or not batch_id:
            continue
        batch = batches.get(batch_id)
        if batch is None:
            batch = {
                "batch_id": batch_id,
                "content_id": job.get("content_id"),
                "count": payload.get("count"),
                "family": payload.get("family"),
                "parent_variant_id": payload.get("parent_variant_id"),
                # The frozen refinement consent the batch runs with; pre-feature
                # batches simply report whole-mode with no quote.
                "refinement_scope": payload.get("refinement_scope") or "whole",
                "surface_id": payload.get("surface_id"),
                "inventory_digest": payload.get("inventory_digest"),
                "image_calls_per_variant": payload.get("image_calls"),
                "image_calls_total": (
                    payload.get("count") * payload.get("image_calls")
                    if isinstance(payload.get("count"), int)
                    and isinstance(payload.get("image_calls"), int) else None
                ),
                "ordinals": [],
                "states": [],
                "stage": "",
            }
            batches[batch_id] = batch
        ordinal = payload.get("ordinal")
        if isinstance(ordinal, int) and ordinal not in batch["ordinals"]:
            batch["ordinals"].append(ordinal)
        state_name = job.get("state")
        if isinstance(state_name, str) and state_name not in batch["states"]:
            batch["states"].append(state_name)
        if state_name in ACTIVE_STATES and (job.get("stage") or not batch["stage"]):
            batch["stage"] = job.get("stage") or batch["stage"]
        if state_name in counts:
            counts[state_name] += 1
        elif state_name == "interrupted":
            counts["failed"] += 1
    active = []
    for batch in batches.values():
        states = batch.pop("states")
        batch["ordinals"] = sorted(batch["ordinals"])
        if "running" in states:
            batch["state"] = "running"
        elif "queued" in states:
            batch["state"] = "queued"
        else:
            batch["state"] = "done"
        if batch["state"] in ACTIVE_STATES:
            active.append(batch)
    return {"active": active, **counts}


def _snapshot(data: Path, *, last_action: dict | None = None, state: dict | None = None) -> dict:
    items_source = _inventory_items(data)
    # Builtin Noctalia candidates are seeded before the state is read so the
    # snapshot can show them. Seeding is local product art only, never a model
    # call, an apply or a capture.
    _seed_noctalia_candidates(data, items_source)
    state = storage.read_state(data)
    noctalia = _sibling("noctalia_bundle")
    observed_content_id = _observed_content_id(state)
    # One live native reading per snapshot: the runtime, not the saved approval,
    # is the authority on what is supported and what is actually applied. The
    # reading only overrides the returned catalog objects; nothing is written.
    available, supported, reason, active, surface_catalog, inventory_digest = \
        _noctalia_live_capability()
    active_variant_id, active_content_id = _active_recipe_ids(active)
    image_calls_per_variant, generation_available, generation_unavailable_reason = \
        _noctalia_quote(surface_catalog)
    playback_state = _playback_module().read_state()
    overrides = state.get("design_overrides") if isinstance(state.get("design_overrides"), dict) else {}
    items = []
    for item in items_source:
        entry = _item_view(data, item, playback_state, overrides.get(item.get("id")))
        entry["noctalia"] = _project_noctalia(
            noctalia.noctalia_catalog(state, item.get("id"), surface_catalog=surface_catalog),
            available, supported, reason, active_variant_id, active_content_id,
            surface_catalog, inventory_digest, image_calls_per_variant,
            generation_available, generation_unavailable_reason,
        )
        items.append(entry)
    noctalia_view = _project_noctalia(
        noctalia.noctalia_catalog(state, observed_content_id, surface_catalog=surface_catalog),
        available, supported, reason, active_variant_id, active_content_id,
        surface_catalog, inventory_digest, image_calls_per_variant,
        generation_available, generation_unavailable_reason,
    )
    noctalia_view["jobs"] = _noctalia_jobs_view(state)
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "preferences": state["preferences"],
        "jobs": state["jobs"],
        "variants": [
            _variant_view(data, variant, live_catalog=surface_catalog)
            for variant in state["variants"]
        ],
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
        "noctalia": noctalia_view,
        "theme_library": theme_store.Store(data).browse(summary_only=True),
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


def _ensure_worker(data: Path, lane: str = LANE_DEFAULT) -> None:
    """Start the detached worker for one lane unless it already owns its lease.

    Lanes are independent: a live background worker never blocks the playback
    worker from starting, so an apply/stop runs while a generation continues.
    """
    with storage.state_lock(data):
        lease = process.acquire_worker_lock(data, lane=lane)
        if lease is None:
            return
        process.release_worker_lock(lease)
        try:
            pid = process.spawn_worker(process.worker_argv(lane), data)
        except OSError as exc:
            raise RuntimeError(f"cannot start the studio {lane} worker: {exc}") from exc
        _log(data, f"{lane} worker started (pid {pid})")


def _ensure_pending_workers(data: Path) -> None:
    """Start the worker of each lane that still has queued work.

    Called by a periodic sync: a lane whose lease is free and whose queue is not
    empty has no live worker to drain it, so wake exactly that lane. A lane with
    no queued job is left alone.
    """
    try:
        state = storage.read_state(data)
    except (ValueError, OSError):
        return
    pending: set[str] = set()
    for job in state.get("jobs") or []:
        if isinstance(job, dict) and job.get("state") == "queued":
            pending.add(_job_lane(job.get("kind")))
    for lane in (LANE_DEFAULT, LANE_PLAYBACK):
        if lane in pending:
            _ensure_worker(data, lane)


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

    try:
        _ensure_pending_workers(data)
    except (ValueError, OSError, RuntimeError) as exc:
        _log(data, f"sync: could not wake pending workers: {type(exc).__name__}: {exc}")

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


def _noctalia_module():
    """The Noctalia generator module: family ids, topologies and the entry point."""
    return _sibling("noctalia_generator")


def _optional_count(payload: dict) -> int:
    value = payload.get("count", 1)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("'count' must be an integer")
    if value < 1 or value > MAX_BATCH_COUNT:
        raise ValueError(f"'count' must be between 1 and {MAX_BATCH_COUNT}")
    return value


def _optional_family(payload: dict) -> str | None:
    value = payload.get("family")
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("'family' must be a string")
    module = _noctalia_module()
    if not module.is_known_family(value):
        raise ValueError(f"unknown Noctalia family {value!r}; known: {', '.join(module.FAMILIES)}")
    return value


_HEX_DIGITS = set("0123456789abcdef")


def _is_hex_id(value, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and all(ch in _HEX_DIGITS for ch in value)


def _noctalia_refinement(payload: dict, surface_catalog: dict, inventory_digest: str) -> tuple[str, str | None, int]:
    """Validate the frozen refinement consent a chooser must send and quote it.

    A Noctalia request must carry ``inventory_digest`` and ``image_calls`` read
    from the live snapshot and equal to the current normalized catalog and its
    exact per-variant call count; anything else (missing quote, stale digest,
    wrong count) is an explicit error, never a silent paid default. Returns
    ``(refinement_scope, surface_id, per_variant_image_calls)``.
    """
    scope = payload.get("refinement_scope", "whole") or "whole"
    if scope not in ("whole", "surface"):
        raise ValueError("'refinement_scope' must be 'whole' or 'surface'")
    surface_id = payload.get("surface_id")
    if scope == "surface":
        if not isinstance(surface_id, str) or not surface_id:
            raise ValueError("'surface_id' is required for 'surface' refinement")
    elif surface_id not in (None, ""):
        raise ValueError("'surface_id' is only valid for 'surface' refinement")
    supplied_digest = payload.get("inventory_digest")
    supplied_calls = payload.get("image_calls")
    if not isinstance(supplied_digest, str) or not supplied_digest or supplied_calls is None:
        raise ValueError(
            "this request carries no frozen surface quote; refresh the chooser and "
            "send inventory_digest and image_calls from the live snapshot")
    if isinstance(supplied_calls, bool) or not isinstance(supplied_calls, int):
        raise ValueError("'image_calls' must be an integer")
    surface_id = surface_id if scope == "surface" else None
    try:
        scoped = _presentation_module().image_call_count(
            surface_catalog, scope=scope, surface_id=surface_id)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"the selected refinement cannot be quoted: {exc}") from exc
    if supplied_digest != inventory_digest or supplied_calls != scoped:
        raise ValueError(
            "the frozen surface quote is stale: the native surface catalog changed; "
            "refresh the chooser and resend inventory_digest and image_calls")
    return scope, surface_id, scoped


def _noctalia_parent_presentation(data: Path, state: dict, parent_variant_id: str,
                                  scope: str, surface_catalog: dict,
                                  inventory_digest: str) -> tuple[dict, dict | None]:
    """Load a refine parent and verify its whole-skin files OUTSIDE the lock.

    Returns ``(parent, frozen_parent_presentation_or_None)``. When the stored
    parent carries a raw whole-skin presentation, every file it names is read
    and hashed here — before the enqueue lock and before anything is queued.
    A ``surface`` refinement additionally requires a complete parent that
    covers exactly the current inventory; a ``whole`` refinement of a legacy
    single-preview parent stays allowed with no presentation freeze.
    """
    parent = _noctalia_variant(state, parent_variant_id)
    if parent is None:
        raise ValueError(f"unknown Noctalia parent variant {parent_variant_id!r}")
    raw = parent.get("presentation")
    if not (isinstance(raw, dict) and "legacy" not in raw):
        if scope == "surface":
            raise ValueError(
                "surface refinement requires a complete whole-skin parent variant; "
                "this parent has only a legacy single preview")
        return parent, None
    try:
        _presentation_module().validate_presentation(
            raw, variant_id=parent_variant_id, content_id=parent.get("content_id"),
            root=storage.generation_dir(parent["content_id"], parent_variant_id, data),
            verify_files=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"the parent variant's whole-skin package failed verification: {exc}") from exc
    if scope == "surface":
        view = _presentation_module().presentation_view(raw, catalog=surface_catalog)
        if not view.get("complete") or not view.get("coverage_current") \
                or view.get("inventory_digest") != inventory_digest:
            raise ValueError(
                "surface refinement requires a complete parent variant whose coverage "
                "matches the current surface inventory")
    return parent, json.loads(json.dumps(raw))


def _presentation_module():
    """The bounded presentation helper: normalization, digests and views."""
    return _sibling("noctalia_presentation")


def _noctalia_live_capability() -> tuple[bool, list, str, dict | None, dict | None, str | None]:
    """One live native skin-status reading.

    Returns ``(available, families, reason, active, surface_catalog,
    inventory_digest)``. The native runtime is the authority on what it
    supports and what it has actually applied. An unreachable or malformed
    reply is reported as ``available=False`` with the real error, never as a
    guessed capability. The authoritative whole-skin surface catalog is
    normalized and digested here once per reading; a missing or malformed
    native catalog stays ``None`` (whole-skin generation disabled), it is
    never guessed.
    """
    try:
        status = _sibling("noctalia_bundle").native_status()
    except Exception as exc:
        return False, [], storage.clean_text(str(exc), storage.MAX_ERROR_CHARS), None, None, None
    if not isinstance(status, dict):
        return False, [], "native skin-status reply is not a JSON object", None, None, None
    families = status.get("supported_families")
    supported = [name for name in families if isinstance(name, str)] if isinstance(families, list) else []
    active = status.get("active") if isinstance(status.get("active"), dict) else None
    catalog, digest = _noctalia_live_catalog(status)
    return True, supported, "", active, catalog, digest


def _noctalia_live_catalog(status: dict) -> tuple[dict | None, str | None]:
    """(normalized surface_catalog, digest) of one native status, or (None, None).

    A malformed native catalog never becomes a spend authorization: it is
    reported as absent and the chooser is told generation is unavailable.
    """
    raw = status.get("surface_catalog")
    if not isinstance(raw, dict):
        return None, None
    try:
        catalog = _presentation_module().normalize_catalog(raw)
    except (ValueError, TypeError):
        # A malformed native catalog is never a spend authorization: it is
        # reported as absent and the quote says generation is unavailable.
        return None, None
    return catalog, _presentation_module().catalog_digest(catalog)


def _noctalia_quote(catalog: dict | None) -> tuple[int | None, bool, str]:
    """(image_calls_per_variant, generation_available, reason) for one catalog.

    The quote counts whole-skin variants only (scene + one image per visual
    surface); a missing catalog means no authorized spend, never a default.
    """
    if catalog is None:
        return None, False, "no authoritative surface catalog from the native skin runtime; refresh the chooser"
    try:
        calls = _presentation_module().image_call_count(catalog, scope="whole")
    except (ValueError, TypeError) as exc:
        return None, False, storage.clean_text(f"cannot quote the surface catalog: {exc}", storage.MAX_ERROR_CHARS)
    return calls, True, ""


def _active_recipe_ids(active) -> tuple[str | None, str | None]:
    """The variant/content ids of the native active recipe, when it carries them."""
    if not isinstance(active, dict):
        return None, None
    variant_id = active.get("variant_id")
    content_id = active.get("content_id")
    return (
        variant_id if _is_hex_id(variant_id, 32) else None,
        content_id if _is_hex_id(content_id, 64) else None,
    )


def _project_noctalia(view: dict, available: bool, supported: list, reason: str,
                      active_variant_id: str | None, active_content_id: str | None,
                      surface_catalog: dict | None = None, inventory_digest: str | None = None,
                      image_calls_per_variant: int | None = None,
                      generation_available: bool = False,
                      generation_unavailable_reason: str = "") -> dict:
    """Overlay the live native capability and the ACTUAL active recipe on a catalog view.

    The stored catalog derives availability and ``active`` from saved state, so a
    saved approval would look applied before the native runtime actually applied
    it. Each returned view is projected with one live reading instead: a family
    is available only while the native runtime reports it, ``active`` is the real
    native variant, and the saved approval is exposed separately as
    ``mapped_variant_id``. Unknown native status keeps ``active`` unknown with the
    real error and never promotes a mapping to active.

    The same live reading also carries the authoritative whole-skin surface
    catalog and its spend quote: ``surface_catalog``/``inventory_digest`` are the
    normalized native values (``None`` when absent), ``image_calls_per_variant``
    is the exact per-package generate_image count, and
    ``generation_available``/``generation_unavailable_reason`` gate the chooser.
    Per-variant ``presentation`` views that were computed against an older
    catalog are re-labelled ``coverage_current`` against this live digest.
    """
    projected = dict(view)
    projected["available"] = available
    projected["reason"] = reason
    projected["supported_families"] = list(supported)
    projected["surface_catalog"] = surface_catalog
    projected["inventory_digest"] = inventory_digest
    projected["image_calls_per_variant"] = image_calls_per_variant
    projected["generation_available"] = generation_available
    projected["generation_unavailable_reason"] = generation_unavailable_reason
    families = []
    for entry in view.get("families") or []:
        if not isinstance(entry, dict):
            continue
        family_id = entry.get("id")
        family_available = available and family_id in supported
        item = dict(entry)
        item["available"] = family_available
        item["reason"] = "" if family_available else (
            reason or f"family {family_id!r} is not implemented by the native skin runtime"
        )
        families.append(item)
    projected["families"] = families

    content_id = view.get("content_id") if isinstance(view.get("content_id"), str) else None
    matches = bool(active_content_id) and active_content_id == content_id
    projected["active_variant_id"] = active_variant_id if matches else None
    projected["active_content_id"] = active_content_id if matches else None
    projected["active_known"] = available
    projected["active_error"] = None if available else (reason or "native skin runtime is unavailable")
    mappings = view.get("mappings") if isinstance(view.get("mappings"), dict) else {}
    mapped = mappings.get(content_id) if content_id else None
    projected["mapped_variant_id"] = mapped if isinstance(mapped, str) else None

    variants = []
    for variant in view.get("variants") or []:
        if not isinstance(variant, dict):
            continue
        family = variant.get("family")
        available_variant = available and family in supported
        entry = {
            **variant,
            "available": available_variant,
            "unavailable_reason": "" if available_variant else (
                reason or f"family {family!r} is not implemented by the native skin runtime"
            ),
            "active": matches and variant.get("variant_id") == active_variant_id,
            "mapped": variant.get("variant_id") == projected["mapped_variant_id"],
        }
        variants.append(entry)
    projected["variants"] = variants
    return projected


def _cancel_batch_jobs(data: Path, batch_id: str) -> int:
    """Cancel every queued Noctalia child of one batch and flag running ones."""
    def mutate(state: dict):
        changed = 0
        for job in state["jobs"]:
            payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
            if payload.get("batch_id") != batch_id:
                continue
            if job.get("state") == "queued":
                job["state"] = "cancelled"
                job["cancel_requested"] = True
                job["stage"] = ""
                job["updated_at"] = storage.now()
                job["error"] = "cancelled before start"
                changed += 1
            elif job.get("state") == "running":
                job["cancel_requested"] = True
                job["updated_at"] = storage.now()
                changed += 1
        return changed

    return storage.update_state(mutate, data)


def _action_generate(data: Path, payload: dict) -> dict:
    content_id = _require_content_id(data, payload)
    _reject_capture_path(payload)
    # The design target is validated before anything is queued: a browser target
    # (or any unknown one) fails here, before a worker and before any spend.
    target = designs.require_target(payload.get("target"))
    count = _optional_count(payload)
    notes = _optional_text(payload, "notes", MAX_NOTES_CHARS)
    if target != NOCTALIA_TARGET:
        if count != 1:
            raise ValueError(f"'count' above 1 is supported only for the {NOCTALIA_TARGET!r} target")
        record = {
            "content_id": content_id,
            "target": target,
            "preview_only": _optional_bool(payload, "preview_only", False),
            "notes": notes,
        }
        if _enqueue(data, "generate", content_id, record) is None:
            return _snapshot(data, last_action={"ok": False, "message": "generation for this wallpaper is already active"})
        _ensure_worker(data)
        return _snapshot(data, last_action={"ok": True, "message": "generation queued"})

    # Noctalia: one job per candidate ordinal, all frozen in the same batch.
    module = _noctalia_module()
    family = _optional_family(payload) or module.DEFAULT_FAMILY
    parent_variant_id = _optional_text(payload, "parent_variant_id", MAX_ID_CHARS) or None
    # Live capability and catalog are mandatory before any job is queued.
    available, supported, _reason, _active, surface_catalog, inventory_digest = \
        _noctalia_live_capability()
    if available and family not in supported:
        return _snapshot(data, last_action={
            "ok": False,
            "message": (
                f"the running Noctalia service does not support the {family!r} family yet "
                f"(supported: {', '.join(supported) or 'none'})"
            ),
        })
    # The whole-skin quote is authoritative and live: a missing native catalog
    # is an explicit refusal, never a silent paid default.
    _per_variant, generation_available, generation_unavailable_reason = _noctalia_quote(surface_catalog)
    if not generation_available:
        return _snapshot(data, last_action={
            "ok": False, "message": generation_unavailable_reason,
        })
    scope, surface_id, per_variant_calls = _noctalia_refinement(
        payload, surface_catalog, inventory_digest)
    if scope == "surface" and not parent_variant_id:
        raise ValueError("surface refinement requires a parent variant")
    frozen_parent_presentation = None
    frozen_parent_recipe = None
    if parent_variant_id:
        parent, frozen_parent_presentation = _noctalia_parent_presentation(
            data, storage.read_state(data), parent_variant_id, scope,
            surface_catalog, inventory_digest)
        if parent.get("content_id") != content_id:
            raise ValueError("the parent variant belongs to a different wallpaper")
        frozen_parent_recipe = json.loads(json.dumps(parent.get("recipe")))
    batch_id = _optional_text(payload, "batch_id", MAX_ID_CHARS) or uuid.uuid4().hex
    records = []
    for ordinal in range(1, count + 1):
        record = {
            "content_id": content_id,
            "target": target,
            "notes": notes,
            "batch_id": batch_id,
            "ordinal": ordinal,
            "count": count,
            "family": family,
            # The consent and the exact catalog it was quoted from are frozen
            # into EVERY payload: the worker re-checks them before any spend.
            "refinement_scope": scope,
            "surface_id": surface_id,
            "inventory_digest": inventory_digest,
            "image_calls": per_variant_calls,
            "surface_catalog": json.loads(json.dumps(surface_catalog)),
        }
        if parent_variant_id:
            record["parent_variant_id"] = parent_variant_id
            record["parent_recipe"] = frozen_parent_recipe
            if frozen_parent_presentation is not None:
                record["parent_presentation"] = frozen_parent_presentation
        records.append(record)
    created = _enqueue_noctalia_batch(data, content_id, records)
    if len(created) != count:
        # No child was published when an existing batch or stale parent wins.
        return _snapshot(data, last_action={"ok": False, "message": "this Noctalia batch is already queued"})
    _ensure_worker(data)
    return _snapshot(data, last_action={
        "ok": True,
        "message": f"queued {count} Noctalia candidate(s)",
        "batch_id": batch_id,
        "count": count,
        "family": family,
        "refinement_scope": scope,
        "surface_id": surface_id,
        "inventory_digest": inventory_digest,
        "image_calls_per_variant": per_variant_calls,
        "image_calls_total": count * per_variant_calls,
    })


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
    _ensure_worker(data, LANE_PLAYBACK)
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
    _ensure_worker(data, LANE_PLAYBACK)
    message = f"stopping {', '.join(queued)}"
    if active:
        message += f"; already queued: {', '.join(active)}"
    return _snapshot(data, last_action={"ok": True, "message": message})


def _action_cancel(data: Path, payload: dict) -> dict:
    if payload.get("batch_id") not in (None, ""):
        # Whole-batch control for a Noctalia generation: every queued child is
        # cancelled now and every running child is flagged, so no further
        # candidate can spend.
        batch_id = _require_identifier(payload, "batch_id")
        changed = _cancel_batch_jobs(data, batch_id)
        if not changed:
            return _snapshot(data, last_action={"ok": False, "message": "this Noctalia batch has no active jobs"})
        _ensure_worker(data)
        return _snapshot(data, last_action={
            "ok": True,
            "message": f"cancellation requested for {changed} job(s) of the batch",
            "batch_id": batch_id,
        })
    job_id = _require_identifier(payload, "job_id")
    lane_holder: list[str] = []

    def mutate(state: dict):
        job = _find_job(state, job_id)
        if job is None:
            raise ValueError(f"unknown job id {job_id!r}")
        lane_holder.append(_job_lane(job.get("kind")))
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
    # A live worker of the job's lane notices the flag; a crashed worker is
    # replaced and the job is recovered as interrupted instead of looking busy.
    _ensure_worker(data, lane_holder[0] if lane_holder else LANE_DEFAULT)
    return _snapshot(data, last_action={"ok": True, "message": message})


def _action_retry(data: Path, payload: dict) -> dict:
    job_id = _require_identifier(payload, "job_id")
    created: list[str] = []
    lane_holder: list[str] = []

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
        if retry["kind"] in theme_workflow.JOB_KINDS:
            raise ValueError("Use a new authoring request with the displayed quote; no automatic paid retry.")
        if retry.get("kind") == "generate" and (retry.get("payload") or {}).get("target") == NOCTALIA_TARGET:
            # A retried Noctalia child becomes its own single-candidate batch, so
            # the aborted-batch gate cannot refuse it and it gets a new variant id.
            retry["payload"]["batch_id"] = uuid.uuid4().hex
            retry["payload"]["ordinal"] = 1
            retry["payload"]["count"] = 1
        state["jobs"].append(retry)
        created.append(retry["id"])
        lane_holder.append(_job_lane(retry.get("kind")))
        return retry["id"]

    storage.update_state(mutate, data)
    if not created:
        return _snapshot(data, last_action={"ok": False, "message": "the same work is already queued"})
    _ensure_worker(data, lane_holder[0] if lane_holder else LANE_DEFAULT)
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
        cached = _sibling("frame_preview").cached(variant, data=data) is not None
        queued = None if cached else _enqueue(data, "preview", variant["content_id"], {"variant_id": variant["id"]})
    if cached:
        return _snapshot(data, last_action={"ok": True, "message": "live frame preview window is open"})
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

def _action_theme(data: Path, payload: dict) -> dict:
    action = payload["action"]
    if action in theme_workflow.JOB_KINDS:
        content_id = _require_content_id(data, payload)
        if _active_job(storage.read_state(data), action, content_id, payload):
            raise ValueError("this authoring operation is already queued/running")
        job = _new_job(action, content_id, dict(payload))
        theme_workflow.prepare(data, job)
        def mutate(state):
            if _active_job(state, action, content_id, payload):
                raise ValueError("authoring operation was queued concurrently")
            state["jobs"].append(job)
        storage.update_state(mutate, data)
        _ensure_worker(data)
        return _snapshot(data, last_action={"ok": True, "job_id": job["id"], "quoted_calls": job["payload"]["quoted_calls"]})
    result = theme_workflow.action(data, payload)
    workspace = None
    if payload.get("id") and action not in ("theme_browse", "theme_files"):
        workspace = theme_store.Store(data).browse(content_id=payload["id"])
    return _snapshot(data, last_action={"ok": True, "action": action, "result": result, "workspace": workspace,
                                      "message": action + ": " + str(result.get("status", "saved; no implicit live apply"))})



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
_ACTIONS.update({name: _action_theme for name in theme_workflow.ACTIONS + theme_workflow.JOB_KINDS})



# ---------------------------------------------------------------------- worker


def _recover_lane_on_start(data: Path, lane: str) -> int:
    """Recover this lane's orphaned running jobs at worker start.

    The caller holds this lane's lease. The playback lane is recovered only
    while no background worker owns ``worker.lock``: a live (legacy) background
    worker may still be executing a playback job, so its held lease forbids
    concluding that a running playback job is dead. An opposite-lane job is
    never touched.
    """
    if lane == LANE_DEFAULT:
        return _recover_running_jobs(data, LANE_DEFAULT)
    with storage.state_lock(data):
        default_lease = process.acquire_worker_lock(data, lane=LANE_DEFAULT)
        if default_lease is None:
            return 0
        try:
            return _recover_running_jobs(data, LANE_PLAYBACK)
        finally:
            process.release_worker_lock(default_lease)


def _worker_main(data: Path, lane: str = LANE_DEFAULT) -> int:
    with storage.state_lock(data):
        lease = process.acquire_worker_lock(data, lane=lane)
    if lease is None:
        _log(data, f"{lane} worker: another worker owns the lease; exiting")
        _emit({"ok": True, "worker": "already-running"})
        return 0
    try:
        _log(data, f"{lane} worker: started")
        try:
            recovered = _recover_lane_on_start(data, lane)
            if recovered:
                _log(data, f"{lane} worker: marked {recovered} running job(s) as interrupted")
        except (ValueError, OSError) as exc:
            _log(data, f"{lane} worker: recovery failed: {type(exc).__name__}: {exc}")
        if lane == LANE_DEFAULT:
            try:
                # Any attempt left `started` by a worker that died mid-call belongs
                # to a job that is no longer running; the paid call is accounted as
                # interrupted instead of staying open forever.
                abandoned = usage.reconcile(data, storage.read_state(data).get("jobs"))
                if abandoned:
                    _log(data, f"{lane} worker: marked {abandoned} generation attempt(s) as interrupted")
            except (ValueError, OSError) as exc:
                _log(data, f"{lane} worker: usage reconcile failed: {type(exc).__name__}: {exc}")
        while True:
            lock = storage.state_lock(data)
            lock.acquire()
            try:
                job = _take_next_job(data, lane)
                if job is None:
                    # Wake-safe idle exit: drop the worker lease while the state
                    # lock is still held, so an enqueue that already saw the lock
                    # free will find no worker and start one.
                    process.release_worker_lock(lease)
                    lease = None
            finally:
                lock.release()
            if job is None:
                _log(data, f"{lane} worker: queue empty; exiting")
                _emit({"ok": True, "worker": "idle"})
                return 0
            _execute_job(data, job)
    finally:
        process.release_worker_lock(lease)


def _take_next_job(data: Path, lane: str) -> dict | None:
    """Claim the oldest queued job of one lane. Called with the state lock held.

    Only jobs of `lane` are claimed and only their pre-start cancellation is
    applied, so the background worker never takes playback work and vice versa.
    """
    state = storage.read_state(data)
    picked = None
    changed = False
    for job in state["jobs"]:
        if job.get("state") != "queued":
            continue
        if _job_lane(job.get("kind")) != lane:
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
    if kind in theme_workflow.JOB_KINDS:
        _set_stage(data, job["id"], "authoring")
        result = theme_workflow.run_job(data, job, lambda: _cancel_requested(data, job["id"]))
        return "succeeded", None, result
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
    # The target is re-validated before dispatch: an unavailable target fails
    # before any spend, and Noctalia has its own worker path.
    if designs.require_target(payload.get("target")) == NOCTALIA_TARGET:
        return _job_generate_noctalia(data, job)
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
    output_dir = storage.generation_dir(content_id, job["id"], data)
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


def _noctalia_catalog_stale(job: dict) -> str | None:
    """Why the live native catalog no longer matches this job's frozen consent.

    Called outside any lock, before paid analysis and again before publication.
    The quoted catalog and exact call count must remain unchanged. A queued
    legacy one-image request cannot upgrade to a paid whole-skin package
    without fresh owner consent.
    """
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    digest = payload.get("inventory_digest")
    calls = payload.get("image_calls")
    if not isinstance(digest, str) or not digest or isinstance(calls, bool) or not isinstance(calls, int):
        return "this queued request has no whole-skin cost consent; refresh the chooser and queue again"
    try:
        status = _sibling("noctalia_bundle").native_status()
    except Exception as exc:
        return storage.clean_text(
            f"the native skin runtime is unavailable: {exc}", storage.MAX_ERROR_CHARS)
    catalog, current_digest = _noctalia_live_catalog(status if isinstance(status, dict) else {})
    if catalog is None or current_digest != digest:
        return ("the native surface catalog changed since this generation was queued; "
                "refresh the chooser and queue again")
    scope = payload.get("refinement_scope") or "whole"
    surface_id = payload.get("surface_id") if scope == "surface" else None
    try:
        current_calls = _presentation_module().image_call_count(
            catalog, scope=scope, surface_id=surface_id)
    except (ValueError, TypeError) as exc:
        return storage.clean_text(
            f"the native surface catalog is no longer quotable: {exc}", storage.MAX_ERROR_CHARS)
    if current_calls != calls:
        return "the surface catalog quote changed since queueing; refresh the chooser and queue again"
    return None


def _noctalia_batch_aborted(data: Path, job: dict) -> bool:
    """Whether an earlier child of this Noctalia batch already stopped it.

    A batch that lost an earlier candidate must not spend on the rest; the
    completed siblings stay published and a retry starts a fresh batch.
    """
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    batch_id = payload.get("batch_id")
    ordinal = payload.get("ordinal")
    if not isinstance(batch_id, str) or not isinstance(ordinal, int):
        return False
    try:
        state = storage.read_state(data)
    except (ValueError, OSError):
        # An unreadable state fails closed: never spend on an unknown batch.
        return True
    for sibling in state["jobs"]:
        if sibling.get("id") == job["id"]:
            continue
        sibling_payload = sibling.get("payload") if isinstance(sibling.get("payload"), dict) else {}
        if sibling_payload.get("batch_id") != batch_id:
            continue
        sibling_ordinal = sibling_payload.get("ordinal")
        if isinstance(sibling_ordinal, int) and sibling_ordinal < ordinal \
                and sibling.get("state") in BATCH_FAILED_STATES:
            return True
    return False


def _job_generate_noctalia(data: Path, job: dict):
    """One Noctalia candidate child: one whole-skin package, whole or refined.

    The whole mode spends scene + one image per visual surface; a surface
    refinement spends exactly two calls (selected surface + scene) against a
    verified parent package. The catalog consent frozen at queue time is
    re-checked before any spend and before the publish.
    """
    payload = job.get("payload") or {}
    module = _noctalia_module()
    family = payload.get("family") or module.DEFAULT_FAMILY
    notes = payload.get("notes") or ""
    preferences = _job_preferences(data, job)
    feedback = payload.get("feedback")
    if not isinstance(feedback, list):
        feedback = []
    parent_variant_id = payload.get("parent_variant_id")
    parent_recipe = payload.get("parent_recipe") if isinstance(payload.get("parent_recipe"), dict) else None

    # Batch gate first: an earlier failure stops this child before it can spend
    # on analysis or an image call.
    if _noctalia_batch_aborted(data, job):
        return "cancelled", "the batch stopped after an earlier candidate did not complete", _recorded_result(data, job["id"])

    # Analysis may itself spend: validate native capability before either model path.
    supported = module.native_supported_families()
    if family not in supported:
        raise RuntimeError(f"the running Noctalia service does not support {family!r}; refusing to spend")

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

    # The frozen catalog consent is re-checked before the paid analysis too: a
    # catalog that changed after queueing never authorizes this spend.
    stale = _noctalia_catalog_stale(job)
    if stale:
        return "failed", stale, None
    frozen_parent = payload.get("parent_presentation")
    if frozen_parent is not None:
        try:
            checked = _presentation_module().validate_presentation(
                frozen_parent, variant_id=parent_variant_id, content_id=content_id,
                root=storage.generation_dir(content_id, parent_variant_id, data), verify_files=True)
            checked_recipe = _sibling("noctalia_bundle").validate_recipe(parent_recipe)
            if (checked_recipe["variant_id"] != parent_variant_id
                    or checked_recipe["content_id"] != content_id
                    or checked_recipe["preview_path"] != checked["scene"]["preview_path"]):
                raise ValueError("the frozen parent recipe does not match its presentation")
            if (payload.get("refinement_scope") == "surface"
                    and checked["inventory_digest"] != payload.get("inventory_digest")):
                raise ValueError("the frozen parent coverage differs from the quoted inventory")
        except (ValueError, TypeError, OSError) as exc:
            return "failed", f"the frozen parent package changed before analysis: {exc}", None
    elif payload.get("refinement_scope") == "surface":
        return "failed", "surface refinement requires a frozen complete parent package", None

    analysis, failure = _ensure_analysis(data, job, item, preview_only=False)
    if analysis is None:
        state, message = failure
        return state, message, None

    if _cancel_requested(data, job["id"]):
        raise process.Cancelled("cancelled before generation started")

    # Appearance is observed fresh for this attempt: a saved snapshot may
    # describe a palette the desktop no longer shows.
    appearance = context.discover()
    output_dir = storage.generation_dir(content_id, job["id"], data)
    _set_stage(data, job["id"], "generating")
    generated = module.generate(
        item,
        analysis,
        appearance,
        preferences,
        notes,
        output_dir,
        lambda stage: _set_stage(data, job["id"], stage),
        lambda: _cancel_requested(data, job["id"]),
        job_id=job["id"],
        family=family,
        parent_recipe=parent_recipe,
        parent_variant_id=parent_variant_id,
        feedback=feedback,
        design_overrides=_job_design_overrides(job, content_id),
        batch_id=payload.get("batch_id"),
        ordinal=payload.get("ordinal"),
        count=payload.get("count"),
        surface_catalog=payload.get("surface_catalog"),
        refinement_scope=payload.get("refinement_scope") or "whole",
        surface_id=payload.get("surface_id"),
        parent_presentation=payload.get("parent_presentation"),
    )
    _set_stage(data, job["id"], "publishing")
    # Last barrier before a visible candidate: a cancel that arrived while the
    # image was being produced must not publish. library re-checks the live job
    # atomically while appending and raises Cancelled itself.
    if _cancel_requested(data, job["id"]):
        raise process.Cancelled("cancelled before the candidate was published")
    # And the catalog consent is re-checked before the library publish, so a
    # package produced against a stale inventory is never added as a candidate.
    stale = _noctalia_catalog_stale(job)
    if stale:
        return "failed", stale, None
    variant = _sibling("library").add_candidate(item, job, generated, preferences, appearance)
    recipe = generated.get("recipe") if isinstance(generated.get("recipe"), dict) else {}
    return "succeeded", None, {
        "variant_id": variant.get("id") if isinstance(variant, dict) else None,
        "batch_id": payload.get("batch_id"),
        "ordinal": payload.get("ordinal"),
        "count": payload.get("count"),
        "family": recipe.get("family") or family,
        "topology": recipe.get("topology"),
        "density": recipe.get("density"),
        "preview_path": generated.get("preview_path"),
        "generation_usage": generated.get("generation_usage"),
        "refinement_scope": payload.get("refinement_scope") or "whole",
        "surface_id": payload.get("surface_id"),
        "inventory_digest": payload.get("inventory_digest"),
        "image_calls_per_variant": payload.get("image_calls"),
        "image_calls_total": (
            payload.get("count") * payload.get("image_calls")
            if isinstance(payload.get("count"), int)
            and isinstance(payload.get("image_calls"), int) else None
        ),
    }


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
