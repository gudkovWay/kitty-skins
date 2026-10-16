"""Design targets: the product a generation is for, and their availability.

A target is the product the generated art is meant for (a window frame, a
browser interface, a Noctalia skin), never the application renderer: the Kitty
renderer settings stay under ``preferences.kitty`` exactly as they shipped. The
frame and Noctalia targets are implemented; the browser target is advertised in
the snapshot as unavailable so the panel can explain the boundary instead of
queueing work no renderer can honour.

Noctalia is the one target with a per-direction capability boundary: the target
exists, but each skin family (Stagecraft, Cut Stone, Wayfinding Field, Adaptive
Workbench) is only applicable while the native runtime lists it in
``supported_families``. That gate lives in ``noctalia_bundle`` (live native
capability), never here: this module only answers whether the product exists.

``require_target`` is the single gate: it runs before a job is enqueued, before
any paid model call and before apply/approve/preview touches a variant, so an
unavailable target fails visibly with no side effects. A missing target defaults
to ``frame`` — the historical single target every older record belonged to.
"""

from __future__ import annotations

from typing import Any

#: The frame target id: the historical generatable product.
FRAME = "frame"

#: The Noctalia skin target id: recipes validated and applied by the native
#: runtime. Target availability is product-level; a specific skin family is
#: gated separately by ``noctalia_bundle`` against live native capability.
NOCTALIA = "noctalia"

#: Every design target the studio knows, in panel order. ``available`` is
#: enforced by ``require_target``; ``reason`` explains the boundary for the panel
#: when it is not available (empty for an available target).
TARGETS: list[dict[str, Any]] = [
    {
        "id": "frame",
        "label": "Рамка окон",
        "available": True,
        "reason": "",
    },
    {
        "id": "browser",
        "label": "Интерфейс браузера",
        "available": False,
        "reason": "Генерация интерфейса браузера ещё не подключена",
    },
    {
        "id": NOCTALIA,
        "label": "Скины Noctalia",
        "available": True,
        "reason": "",
    },
]

#: The target a missing or empty value resolves to.
DEFAULT_TARGET = FRAME

_TARGET_IDS = tuple(entry["id"] for entry in TARGETS)


def is_known(value) -> bool:
    """Whether a value names any known target, available or not."""
    return isinstance(value, str) and value in _TARGET_IDS


def targets_snapshot() -> list[dict[str, Any]]:
    """A copy of the target metadata, safe to hand to the panel."""
    return [dict(entry) for entry in TARGETS]


def require_target(value) -> str:
    """Return a known, available target id or raise before any side effect.

    A missing value (``None`` or the empty string) is the historical frame
    default. An unknown id, or a known but unavailable target, raises
    ``ValueError`` with a message safe to show the owner.
    """
    if value is None or value == "":
        return DEFAULT_TARGET
    if not isinstance(value, str) or value not in _TARGET_IDS:
        raise ValueError(f"unknown design target {value!r}")
    for entry in TARGETS:
        if entry["id"] == value:
            if not entry["available"]:
                raise ValueError(
                    f"design target {value!r} is not available: {entry['reason']}"
                )
            return value
    # Unreachable: _TARGET_IDS and TARGETS are built from the same list.
    raise ValueError(f"unknown design target {value!r}")
