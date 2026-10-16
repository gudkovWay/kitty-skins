#!/usr/bin/env python3
"""Durable accounting of every attempted image-generation child invocation.

The generator runs OMP with ``--no-session``: the paid child's token usage is
recorded nowhere else, and the image backend bills against a subscription quota
that OMP reports as zero catalog cost. This module is the studio's own ledger,
and it exists so that no attempt is ever silently lost or silently counted as
zero.

Rules it enforces:

* a record is persisted *before* the child process starts, so an attempt that
  dies with the worker is still visible;
* every attempt is finalized exactly once, as ``succeeded``, ``failed``,
  ``cancelled`` or (by :func:`reconcile`, after a crashed worker) ``interrupted``;
* assistant messages repeat across the JSON event stream (``message_end``,
  ``turn_end``, ``agent_end``), so token usage is read per distinct message and
  never double counted;
* the child's own text tokens are kept apart from the image tool's actual usage
  object: they are different units and neither is derived from the other;
* an unreported field stays JSON ``null`` and is listed in ``unknown_fields`` —
  never ``0``, never estimated;
* catalog cost and subscription percentages are never inferred: OMP reports a
  zero cost for this provider, and zero is not a measurement;
* generation jobs that predate the ledger are surfaced as ``unknown`` attempts
  without inventing historical token counts, and are imported into the ledger
  itself exactly once so a later reset cannot erase them;
* the ledger is never truncated: every attempt ever recorded stays on disk and
  only the status response's ``recent`` view is bounded;
* every read-modify-write of the ledger holds an exclusive flock on its own
  ``<usage_path>.lock`` (never the studio record's lock), so a UI legacy import
  and a worker ``finish`` cannot overwrite each other;
* a ledger file that exists but cannot be parsed is surfaced as an error, not
  silently reset: accounting history is never discarded and no paid call can
  slip past a corrupt ledger unnoticed.

The document lives in the studio data root (:func:`storage.usage_path`), outside
every tree a scoped reset quarantines, so resetting a wallpaper never erases the
accounting of the attempts already paid for.
"""

from __future__ import annotations

import contextlib
import fcntl
import re
import uuid
from pathlib import Path

import storage

__all__ = ["begin", "finish", "reconcile", "summary", "entry", "ledger_path"]

SCHEMA_VERSION = 1
KIND = "wallpaper-studio-generation-usage"
#: Attempts reported in the status contract's `recent` list. The ledger keeps
#: every attempt on disk; only this response view is bounded.
RECENT_LIMIT = 10
#: Bound of one recorded error/diagnostic string.
MAX_ERROR_CHARS = 500
#: Bound of the recorded image-usage object.
MAX_USAGE_KEYS = 24
MAX_USAGE_DEPTH = 2
MAX_USAGE_STRING = 200
MAX_PROVIDER_CHARS = 80
MAX_MODEL_CHARS = 120

STARTED = "started"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
INTERRUPTED = "interrupted"
#: Status of a pre-ledger job: it happened, its usage was never recorded.
UNKNOWN = "unknown"

FINAL_STATUSES = (SUCCEEDED, FAILED, CANCELLED, INTERRUPTED)
KNOWN_STATUSES = (STARTED, *FINAL_STATUSES, UNKNOWN)

#: Contract field names, in report order.
TEXT_TOKEN_FIELDS = ("input", "output", "total")
CONTRACT_FIELDS = (
    "id",
    "status",
    "provider",
    "model",
    "started_at",
    "finished_at",
    "text_tokens",
    "image_usage",
    "unknown_fields",
)

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def ledger_path(data: Path | None = None) -> Path:
    return storage.usage_path(data)


# ------------------------------------------------------------------ primitives


def _clean(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = _CONTROL.sub(" ", value).strip()
    return text[:limit] or None


def _count(value) -> int | None:
    """A reported token count; anything that is not a whole number is unknown."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _bounded_object(value, depth: int = 0) -> dict | None:
    """A shallow copy of a reported usage object: scalars only, bounded.

    The tool's usage object is untrusted JSON. Only its own scalar leaves are
    kept, so the ledger can never grow an unbounded payload and no nested
    structure is re-interpreted. An object without a single scalar leaf is not
    a report and becomes null.
    """
    if not isinstance(value, dict) or depth > MAX_USAGE_DEPTH:
        return None
    out: dict = {}
    for key in sorted(value)[:MAX_USAGE_KEYS]:
        item = value[key]
        name = _clean(key, 60)
        if not name:
            continue
        if isinstance(item, bool) or isinstance(item, int):
            out[name] = item
        elif isinstance(item, float):
            out[name] = round(item, 6)
        elif isinstance(item, str):
            out[name] = _clean(item, MAX_USAGE_STRING)
        elif isinstance(item, dict) and depth < MAX_USAGE_DEPTH:
            nested = _bounded_object(item, depth + 1)
            if nested:
                out[name] = nested
    return out or None


def _text_tokens(value) -> dict:
    """Normalize a token report: every field present, unknown stays null.

    ``total`` is only derived from input+output when the reporter omitted it and
    both halves are known; a partial report keeps a null total rather than
    presenting a sum of two different units as a total.
    """
    source = value if isinstance(value, dict) else {}
    tokens = {field: _count(source.get(field)) for field in TEXT_TOKEN_FIELDS}
    if tokens["total"] is None and tokens["input"] is not None and tokens["output"] is not None:
        tokens["total"] = tokens["input"] + tokens["output"]
    return tokens


def _unknown_fields(provider, model, tokens: dict, image_usage) -> list[str]:
    """Every contract field this attempt could not observe."""
    fields: list[str] = []
    if provider is None:
        fields.append("provider")
    if model is None:
        fields.append("model")
    for field in TEXT_TOKEN_FIELDS:
        if tokens[field] is None:
            fields.append(f"text_tokens.{field}")
    if image_usage is None:
        fields.append("image_usage")
    return fields


def entry(record: dict) -> dict:
    """One attempt in the shared `generation_usage` entry shape.

    Exactly the contract keys, always present: an unknown value is null and is
    named in ``unknown_fields``.
    """
    source = record if isinstance(record, dict) else {}
    provider = _clean(source.get("provider"), MAX_PROVIDER_CHARS)
    model = _clean(source.get("model"), MAX_MODEL_CHARS)
    tokens = _text_tokens(source.get("text_tokens"))
    image_usage = source.get("image_usage")
    image_usage = image_usage if isinstance(image_usage, dict) else None
    status = source.get("status")
    return {
        "id": _clean(source.get("id"), 64) or "unknown",
        "status": status if status in KNOWN_STATUSES else UNKNOWN,
        "provider": provider,
        "model": model,
        "started_at": _clean(source.get("started_at"), 40),
        "finished_at": _clean(source.get("finished_at"), 40),
        "text_tokens": tokens,
        "image_usage": image_usage,
        "unknown_fields": _unknown_fields(provider, model, tokens, image_usage),
    }


def _has_report(item: dict) -> bool:
    """Whether an attempt carries any observed usage value at all."""
    if item["image_usage"] is not None:
        return True
    return any(item["text_tokens"][field] is not None for field in TEXT_TOKEN_FIELDS)


# --------------------------------------------------------------------- storage


def _read(data: Path | None = None) -> dict:
    """The current ledger, or an empty document when no ledger exists yet.

    Fail-closed: a ledger file that exists but cannot be parsed is surfaced
    instead of silently reset, so accounting history is never discarded and no
    paid call can slip past a corrupt ledger unnoticed.
    """
    path = ledger_path(data)
    if not path.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "updated_at": None,
            "attempts": [],
        }
    raw = storage.read_json(path)
    if raw is None:
        raise ValueError(f"generation usage ledger is malformed: {path}")
    attempts = raw.get("attempts") if isinstance(raw, dict) else None
    if not isinstance(attempts, list):
        raise ValueError(f"generation usage ledger has no attempt list: {path}")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "updated_at": raw.get("updated_at") if isinstance(raw, dict) else None,
        "attempts": [record for record in attempts if isinstance(record, dict)],
    }


@contextlib.contextmanager
def _ledger_lock(data: Path | None = None):
    """Exclusive flock on `<usage_path>.lock` around one read-modify-write.

    A distinct lock, never the studio record's lock: the ledger has its own
    writers (worker `begin`/`finish`, UI legacy import via :func:`summary`), and
    every mutation must be serialized against concurrent readers of the same
    file. The lock file is never deleted, so a lost lock inode race is avoided.
    """
    path = Path(str(ledger_path(data)) + ".lock")
    handle = open(path, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _write(data: Path | None, document: dict) -> None:
    """Atomically persist the whole ledger. Every attempt ever recorded stays."""
    document["schema_version"] = SCHEMA_VERSION
    document["kind"] = KIND
    document["updated_at"] = storage.now()
    storage.atomic_json(ledger_path(data), document)


def _find(document: dict, attempt_id: str) -> dict | None:
    for record in document["attempts"]:
        if record.get("id") == attempt_id:
            return record
    return None


def _stamp() -> str:
    return storage.now().replace("-", "").replace(":", "")[:15]


# ------------------------------------------------------------------------- api


def begin(
    data: Path | None = None,
    *,
    job_id: str | None = None,
    kind: str = "generate",
    requested_model: str | None = None,
) -> str:
    """Persist a `started` attempt and return its unique id.

    Called immediately before the child process is created; a failure to persist
    here must abort the attempt, because an unrecorded paid call is exactly what
    this ledger exists to prevent.
    """
    attempt_id = f"gu-{_stamp()}-{uuid.uuid4().hex[:8]}"
    with _ledger_lock(data):
        document = _read(data)
        document["attempts"].insert(
            0,
            {
                "id": attempt_id,
                "kind": _clean(kind, 40) or "generate",
                "job_id": _clean(job_id, 64),
                "status": STARTED,
                "provider": None,
                "model": None,
                "requested_model": _clean(requested_model, MAX_MODEL_CHARS),
                "started_at": storage.now(),
                "finished_at": None,
                "text_tokens": _text_tokens(None),
                "image_usage": None,
                "error": None,
            },
        )
        _write(data, document)
    return attempt_id


def finish(
    data: Path | None,
    attempt_id: str,
    *,
    status: str,
    provider: str | None = None,
    model: str | None = None,
    text_tokens=None,
    image_usage=None,
    error: str | None = None,
) -> dict | None:
    """Finalize one attempt with whatever the child actually reported.

    Best effort by construction: it is called from failure paths, so it never
    raises for a missing record — an attempt whose record vanished is recreated
    in its final state rather than losing the accounting — and an unwritable
    ledger cannot mask the outcome of the call it was accounting for. The
    `started` record written by :func:`begin` still exists in that case, and
    :func:`reconcile` finalizes it on the next worker start.
    """
    if status not in FINAL_STATUSES:
        raise ValueError(f"attempt status must be one of: {', '.join(FINAL_STATUSES)}")
    with _ledger_lock(data):
        document = _read(data)
        record = _find(document, attempt_id)
        if record is None:
            record = {
                "id": attempt_id,
                "kind": "generate",
                "job_id": None,
                "started_at": storage.now(),
                "requested_model": None,
                "recovered": True,
            }
            document["attempts"].insert(0, record)
        record["status"] = status
        record["finished_at"] = storage.now()
        if provider is not None:
            record["provider"] = _clean(provider, MAX_PROVIDER_CHARS)
        if model is not None:
            record["model"] = _clean(model, MAX_MODEL_CHARS)
        if text_tokens is not None:
            record["text_tokens"] = _text_tokens(text_tokens)
        if image_usage is not None:
            record["image_usage"] = _bounded_object(image_usage)
        if error is not None:
            record["error"] = _clean(error, MAX_ERROR_CHARS)
        try:
            _write(data, document)
        except OSError:
            # The attempt record is already durable from `begin`; a failing
            # write must not replace the error this call is accounting for.
            pass
        return record


def reconcile(data: Path | None = None, jobs=None) -> int:
    """Finalize attempts abandoned by a worker that died mid-generation.

    Only ever called on the worker's start path, after orphaned running jobs
    have been marked interrupted and before the queue is drained, so an attempt
    whose job is no longer running cannot still be in flight.
    """
    live = set()
    for job in jobs if isinstance(jobs, list) else []:
        if not isinstance(job, dict):
            continue
        if job.get("state") in ("queued", "running") and isinstance(job.get("id"), str):
            live.add(job["id"])
    with _ledger_lock(data):
        document = _read(data)
        changed = 0
        for record in document["attempts"]:
            if record.get("status") != STARTED:
                continue
            if record.get("job_id") in live:
                continue
            record["status"] = INTERRUPTED
            record["finished_at"] = storage.now()
            record["error"] = record.get("error") or "the generation worker stopped before this attempt was finalized"
            changed += 1
        if changed:
            _write(data, document)
    return changed


def _legacy_entry(job: dict) -> dict:
    """A pre-ledger generation job: it happened, its usage was never recorded.

    The raw ledger record carries ``job_id`` and ``source`` so that the import
    happens exactly once: once persisted, the record itself proves the job is
    already known and no later :func:`summary` call re-imports it.
    """
    job_id = _clean(job.get("id"), 64) or "unknown"
    stamp = _clean(job.get("updated_at"), 40) or _clean(job.get("created_at"), 40)
    return {
        "id": f"legacy-{job_id}",
        "kind": "generate",
        "job_id": job_id,
        "status": UNKNOWN,
        "provider": None,
        "model": None,
        "requested_model": None,
        "started_at": stamp,
        "finished_at": stamp,
        "text_tokens": None,
        "image_usage": None,
        "error": None,
        "source": "legacy",
    }


def _has_unknown(item: dict) -> bool:
    """Whether any usage contract field of an attempt went unreported.

    An attempt is *not* fully known when ``image_usage`` is null or when any
    ``text_tokens`` field is null. A partially reported attempt (child text
    known, image usage never observed) is therefore unknown too, even though it
    also counts towards ``reported_attempts`` — the two counters overlap by
    design: ``reported_attempts`` means "carries at least one observed value",
    ``unknown_attempts`` means "carries at least one unobserved field".
    """
    if item["image_usage"] is None:
        return True
    return any(item["text_tokens"][field] is None for field in TEXT_TOKEN_FIELDS)


def summary(data: Path | None = None, jobs=None) -> dict:
    """The shared `generation_usage` status contract.

    ``jobs`` is the studio record's job list: generation jobs with no ledger
    attempt are imported into the ledger itself as `unknown` records (never for
    queued/running jobs, which their own `started` record represents), so the
    migration is durable and survives a later reset that removes the jobs.
    Aggregates are the sum of the values that were actually reported (null when
    none were): they are a known subtotal, never a complete total while any
    attempt is unknown. See :func:`_has_unknown` for the exact overlap between
    ``reported_attempts`` and ``unknown_attempts``.
    """
    with _ledger_lock(data):
        document = _read(data)
        known = {
            record.get("job_id")
            for record in document["attempts"]
            if isinstance(record.get("job_id"), str)
        }
        imported = []
        for job in jobs if isinstance(jobs, list) else []:
            if not isinstance(job, dict) or job.get("kind") != "generate":
                continue
            job_id = job.get("id")
            if not isinstance(job_id, str) or job_id in known:
                continue
            # A job that never left the queue attempted nothing; a job that is
            # still running is already represented by its own ledger record.
            if job.get("state") in ("queued", "running"):
                continue
            known.add(job_id)
            imported.append(_legacy_entry(job))
        if imported:
            document["attempts"].extend(imported)
            _write(data, document)
        attempts = [entry(record) for record in document["attempts"]]

    attempts.sort(key=lambda item: (item["started_at"] or "", item["id"]), reverse=True)
    reported = sum(1 for item in attempts if _has_report(item))

    def subtotal(field: str) -> int | None:
        values = [item["text_tokens"][field] for item in attempts if item["text_tokens"][field] is not None]
        return sum(values) if values else None

    return {
        "attempts": len(attempts),
        "reported_attempts": reported,
        "unknown_attempts": sum(1 for item in attempts if _has_unknown(item)),
        "tokens": {field: subtotal(field) for field in TEXT_TOKEN_FIELDS},
        "latest": attempts[0] if attempts else None,
        "recent": attempts[:RECENT_LIMIT],
    }
