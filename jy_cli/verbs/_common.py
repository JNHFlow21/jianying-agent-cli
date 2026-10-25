"""Helpers shared by the newer local verb modules (not discovered: ``_`` prefix)."""

from __future__ import annotations

from pathlib import Path

from .. import effects
from ..commands import (  # noqa: F401 - re-exported for the verb modules
    _checked_output,
    _encode_defaults,
    _even,
    _filter_complex_argv,
    _fps_rate,
    _input_summary,
    _movflags,
    _parse_at,
    _probe,
    _require_video,
    _require_video_container,
    _resolve_input,
    _run,
    _sec,
    _video_encode_args,
)
from ..envelope import JyError

AUDIO_ARGS = ["-c:a", "aac", "-b:a", "192k"]


def number(value):
    return f"{float(value):g}"


def require_audio(source, hint=None):
    if not source.get("audio_streams"):
        raise JyError(
            "UNSUPPORTED",
            hint or f"no audio stream in {source['path']}",
            details={"path": source["path"]},
        )
    return source["audio_streams"][0]


def between(value, low, high, flag):
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        raise JyError("VALIDATION", f"{flag} must be a number, got {value!r}") from None
    if not low <= numeric <= high:
        raise JyError("VALIDATION", f"{flag} must be between {low:g} and {high:g}")
    return numeric


def anchor(position, base_width, base_height, item_width, item_height, margin=24):
    """Top-left (x, y) for an overlay inside the base frame."""
    token = str(position or "center").strip().lower()
    named = {
        "top-left": (margin, margin),
        "top": ((base_width - item_width) // 2, margin),
        "top-right": (base_width - item_width - margin, margin),
        "left": (margin, (base_height - item_height) // 2),
        "center": ((base_width - item_width) // 2, (base_height - item_height) // 2),
        "right": (base_width - item_width - margin, (base_height - item_height) // 2),
        "bottom-left": (margin, base_height - item_height - margin),
        "bottom": ((base_width - item_width) // 2, base_height - item_height - margin),
        "bottom-right": (
            base_width - item_width - margin,
            base_height - item_height - margin,
        ),
    }
    if token in named:
        return named[token]
    parts = [piece.strip() for piece in token.split(",")]
    if len(parts) == 2:
        try:
            return int(float(parts[0])), int(float(parts[1]))
        except ValueError:
            raise JyError(
                "VALIDATION",
                f"invalid position {position!r}; use a named anchor or X,Y",
            ) from None
    raise JyError(
        "VALIDATION",
        f"invalid position {position!r}; use top-left|top|top-right|left|center|right|"
        "bottom-left|bottom|bottom-right|X,Y",
    )


def is_image(path):
    return effects.is_image_path(path)


def write_text_output(path, text, force):
    """Refuse-overwrite writer for non-media artifacts (SRT, beats files)."""
    target = Path(path).expanduser()
    if target.exists() and not force:
        raise JyError(
            "VALIDATION", f"output already exists: {target} (pass --force to overwrite)"
        )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise JyError("VALIDATION", f"cannot write {target}: {exc}") from exc
    return target.resolve()


def text_output_info(path):
    target = Path(path)
    return {
        "path": str(target.resolve()),
        "size_bytes": target.stat().st_size,
        "lines": sum(1 for _ in target.open(encoding="utf-8")),
    }
