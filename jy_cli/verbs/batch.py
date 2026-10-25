"""``jy batch``: one trim+export recipe across many inputs into a directory."""

from __future__ import annotations

import types
from pathlib import Path

from .. import exportspec, ffmpeg
from ..envelope import JyError
from ._common import (
    _checked_output,
    _input_summary,
    _probe,
    _require_video,
    _resolve_input,
    _run,
    number,
)


def _recipe(args):
    """A namespace exportspec.build_argv understands (keeps export rules identical)."""
    return types.SimpleNamespace(
        resolution=args.resolution,
        fit="contain",
        no_pad=False,
        fps=args.fps,
        codec=args.codec,
        quality=args.quality,
        bitrate=None,
        preset=args.preset,
        audio_codec="aac",
        audio_bitrate="192k",
        audio_sample_rate=48000,
        bitdepth=8,
        faststart=True,
        hw=False,
        output=None,
    )


def batch(ctx, args):
    """Apply the same trim + export recipe to N inputs, writing into a directory."""
    if args.start < 0:
        raise JyError("VALIDATION", "--start must be >= 0")
    if args.duration is not None and args.duration <= 0:
        raise JyError("VALIDATION", "--duration must be > 0")
    outdir = Path(args.output).expanduser()
    if outdir.exists() and not outdir.is_dir():
        raise JyError("VALIDATION", f"output path is not a directory: {outdir}")

    recipe = _recipe(args)
    encoder, _family, _codec = exportspec.required_encoder(recipe)
    tool = ctx.require(encoders=[encoder])

    planned = []
    for value in args.inputs:
        source_path = _resolve_input(value)
        source = _probe(tool, source_path)
        _require_video(source)
        target = _checked_output(
            str(outdir / f"{source_path.stem}{args.suffix}.mp4"), args.force
        )
        argv, _plan, warnings = exportspec.build_argv(
            tool, recipe, source, str(source_path), str(target)
        )
        # exportspec builds `-i <input>`; prepend the trim window for this file.
        insert_at = argv.index("-i")
        if args.start:
            argv[insert_at:insert_at] = ["-ss", number(args.start)]
            insert_at += 2
        if args.duration is not None:
            argv[insert_at:insert_at] = ["-t", number(args.duration)]
        planned.append(
            {
                "input": str(source_path),
                "output": str(target),
                "summary": _input_summary(source),
                "warnings": warnings,
                "argv": argv,
            }
        )

    if args.dry_run:
        return "preview", {
            "outdir": str(outdir),
            "recipe": {
                "resolution": args.resolution,
                "codec": args.codec,
                "fps": args.fps,
                "quality": args.quality,
                "start": args.start,
                "duration": args.duration,
                "suffix": args.suffix,
            },
            "files": [
                {
                    "input": item["input"],
                    "output": item["output"],
                    "ffmpeg_argv": ffmpeg.with_loglevel(item["argv"], ctx.verbose),
                }
                for item in planned
            ],
        }

    results = []
    failures = []
    for item in planned:
        target = Path(item["output"])
        entry = {
            "input": item["input"],
            "output": str(target),
            "ok": False,
        }
        try:
            info = _run(tool, item["argv"], target, ctx.verbose)
            entry.update({"ok": True, "video": info})
            results.append(entry)
        except JyError as error:
            entry.update({"error": error.as_dict()})
            failures.append(entry)
            results.append(entry)

    data = {
        "outdir": str(outdir),
        "count": len(results),
        "succeeded": len(results) - len(failures),
        "failed": len(failures),
        "files": results,
    }
    if failures:
        first = failures[0]["error"]
        raise JyError(
            first["code"],
            f"batch 只成功 {data['succeeded']}/{data['count']} 个："
            f"{Path(failures[0]['input']).name} 失败（{first['message']}）",
            retriable=bool(first.get("retriable")),
            details={"batch": data},
        )
    return "executed", data


def register(r):
    parser = r.parser_for(
        "batch",
        parents=[r.output_parent],
        help="apply the same trim+export recipe across N inputs into a directory",
        description="-o is the OUTPUT DIRECTORY (created if needed). Every input gets "
        "the same export recipe (resolution/codec/fps/quality) plus the same trim "
        "window; file names are <stem><suffix>.mp4. One bad input does not abort the "
        "rest: the envelope reports per-file results and `ok:false` if any failed.",
    )
    parser.add_argument("inputs", nargs="+", metavar="input")
    parser.add_argument("--start", type=float, default=0.0, metavar="SEC")
    parser.add_argument("--duration", type=float, metavar="SEC")
    parser.add_argument("--resolution", default="1080p", metavar="SPEC")
    parser.add_argument("--codec", choices=("h264", "h265", "h264_vt", "hevc_vt"), default="h264")
    parser.add_argument("--fps", choices=exportspec.FPS_CHOICES, default="same")
    parser.add_argument("--quality", type=int, default=20, metavar="1-51")
    parser.add_argument("--preset", choices=exportspec.PRESETS_X264, default="veryfast")
    parser.add_argument("--suffix", default="", metavar="TEXT", help="file-name suffix")
    parser.add_argument("--dry-run", action="store_true", help="plan only, encode nothing")
    r.bind(parser, "batch", batch)
