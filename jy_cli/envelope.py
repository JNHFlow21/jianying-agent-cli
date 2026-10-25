"""The jy.v1 stdout contract: error type, payload builder, single emitter.

Every real invocation writes exactly one JSON object to stdout through
:func:`write`.  Nothing else may print to stdout.  The payload is built by
:func:`build` so background jobs can persist the same object they emit.
"""

from __future__ import annotations

import json
import sys

SCHEMA = "jy.v1"

OUTCOMES = ("readonly", "preview", "executed", "noop", "unknown")

ERROR_CODES = (
    "VALIDATION",
    "NOT_FOUND",
    "UNSUPPORTED",
    "UNAVAILABLE",
    "INTERNAL",
    "BLOCKED",
)

_EXIT_CODES = {"VALIDATION": 2}


class JyError(Exception):
    """A reported failure: error{code, message, retriable, details}."""

    def __init__(self, code, message, *, retriable=False, details=None):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code: {code}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.retriable = retriable
        self.details = details or {}

    def as_dict(self):
        return {
            "code": self.code,
            "message": self.message,
            "retriable": self.retriable,
            "details": self.details,
        }


def build(*, command, ok, outcome, data, error, warnings, ffmpeg, elapsed_ms):
    """The single jy.v1 object shape (also what a finished job stores)."""
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome: {outcome}")
    return {
        "schema": SCHEMA,
        "command": command,
        "ok": ok,
        "outcome": outcome,
        "data": data,
        "error": error,
        "warnings": list(warnings or ()),
        "meta": {"ffmpeg": ffmpeg, "elapsed_ms": elapsed_ms},
    }


def exit_code(payload):
    """Process exit code for one payload: 0 ok, 2 VALIDATION, 1 everything else."""
    if payload.get("ok"):
        return 0
    error = payload.get("error") or {}
    return _EXIT_CODES.get(error.get("code"), 1)


def write(payload):
    """Write the one and only stdout JSON object; return the process exit code."""
    try:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except BrokenPipeError:
        return 1
    return exit_code(payload)


def emit(*, command, ok, outcome, data, error, warnings, ffmpeg, elapsed_ms):
    """Build and write one payload (kept for callers that do not need the dict)."""
    return write(
        build(
            command=command,
            ok=ok,
            outcome=outcome,
            data=data,
            error=error,
            warnings=warnings,
            ffmpeg=ffmpeg,
            elapsed_ms=elapsed_ms,
        )
    )


def tail_lines(text, limit=20):
    """Last ``limit`` non-empty stderr lines, for error.details.tail."""
    lines = (text or "").rstrip().splitlines()
    return lines[-limit:] if lines else []
