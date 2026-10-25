"""Geometry verbs: ``reverse``, ``rotate``, ``flip``, ``crop``."""

from __future__ import annotations

from ..envelope import JyError
from ._common import (
    _checked_output,
    _even,
    _input_summary,
    _movflags,
    _probe,
    _require_video,
    _require_video_container,
    _resolve_input,
    _run,
    _video_encode_args,
    between,
)


def _run_filter(ctx, args, chain, *, audio_chain=None, extra=None):
    """Apply one video filter chain; audio is copied unless ``audio_chain`` is set."""
    tool = ctx.require()
    input_path = _resolve_input(args.input)
    source = _probe(tool, input_path)
    _require_video(source)
    _require_video_container(args.output)
    output = _checked_output(args.output, args.force)
    argv = [
        tool.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", str(input_path), "-vf", chain,
    ]
    argv.extend(_video_encode_args())
    if audio_chain:
        argv.extend(["-af", audio_chain, "-c:a", "aac", "-b:a", "192k"])
    elif source["audio_streams"]:
        argv.extend(["-c:a", "copy"])
    else:
        argv.append("-an")
    if extra:
        argv.extend(extra)
    argv.extend(_movflags(str(output)))
    argv.extend(["-y", str(output)])
    info = _run(tool, argv, output, ctx.verbose)
    return source, output, argv, info


def reverse(ctx, args):
    """Play the clip (and its audio) backwards."""
    tool = ctx.require()
    source = _probe(tool, _resolve_input(args.input))
    if source["duration_s"] and source["duration_s"] > 120:
        ctx.warnings.append(
            "reverse buffers the whole video in RAM; clips over ~2 minutes can be slow "
            "or run out of memory — trim first"
        )
    has_audio = bool(source["audio_streams"])
    source, output, argv, info = _run_filter(
        ctx, args, "reverse", audio_chain="areverse" if has_audio else None
    )
    return "executed", {
        "input": _input_summary(source),
        "reverse": True,
        "audio_reversed": has_audio,
        "output": info,
        "ffmpeg_argv": argv,
    }


def rotate(ctx, args):
    """Rotate by 90/180/270 degrees (clockwise)."""
    degrees = int(args.degrees)
    chains = {
        90: "transpose=1",
        180: "transpose=1,transpose=1",
        270: "transpose=2",
    }
    if degrees not in chains:
        raise JyError("VALIDATION", "--degrees must be 90, 180 or 270")
    source, output, argv, info = _run_filter(ctx, args, chains[degrees])
    data = {
        "input": _input_summary(source),
        "degrees": degrees,
        "output": info,
        "ffmpeg_argv": argv,
    }
    if degrees in (90, 270):
        data["note"] = "90/270 swaps width and height"
    return "executed", data


def flip(ctx, args):
    """Mirror horizontally or vertically."""
    chain = {"h": "hflip", "v": "vflip"}[args.axis]
    source, output, argv, info = _run_filter(ctx, args, chain)
    return "executed", {
        "input": _input_summary(source),
        "axis": "horizontal" if args.axis == "h" else "vertical",
        "output": info,
        "ffmpeg_argv": argv,
    }


def crop(ctx, args):
    """Crop to an explicit box or to a centred aspect ratio."""
    tool = ctx.require()
    source = _probe(tool, _resolve_input(args.input))
    _require_video(source)
    width, height = int(source["width"]), int(source["height"])

    if args.aspect:
        try:
            left, _, right = args.aspect.partition(":")
            ratio = float(left) / float(right)
        except (ValueError, ZeroDivisionError):
            raise JyError(
                "VALIDATION", f"invalid --aspect {args.aspect!r}; use e.g. 9:16"
            ) from None
        if ratio <= 0:
            raise JyError("VALIDATION", "--aspect must be positive")
        crop_w, crop_h = width, height
        if width / height > ratio:
            crop_w = int(round(height * ratio))
        else:
            crop_h = int(round(width / ratio))
        x = (width - crop_w) // 2
        y = (height - crop_h) // 2
    else:
        if not args.width or not args.height:
            raise JyError(
                "VALIDATION", "give --width and --height (or --aspect for a centred crop)"
            )
        crop_w = int(between(args.width, 2, width, "--width"))
        crop_h = int(between(args.height, 2, height, "--height"))
        x = int(between(args.x, 0, width - crop_w, "--x"))
        y = int(between(args.y, 0, height - crop_h, "--y"))
    if x + crop_w > width or y + crop_h > height:
        raise JyError(
            "VALIDATION",
            f"crop box {crop_w}x{crop_h}+{x}+{y} does not fit inside {width}x{height}",
        )
    crop_w, crop_h = _even(crop_w), _even(crop_h)
    if (crop_w, crop_h) != (int(args.width or crop_w), int(args.height or crop_h)) and (
        args.width or args.height
    ):
        ctx.warnings.append(
            f"crop size rounded to even pixels ({crop_w}x{crop_h}) for yuv420p"
        )
    chain = f"crop={crop_w}:{crop_h}:{x}:{y}"
    source, output, argv, info = _run_filter(ctx, args, chain)
    return "executed", {
        "input": _input_summary(source),
        "crop": {"x": x, "y": y, "width": crop_w, "height": crop_h},
        "aspect": args.aspect,
        "output": info,
        "ffmpeg_argv": argv,
    }


def register(r):
    rev = r.parser_for(
        "reverse",
        parents=[r.output_parent],
        help="play the clip and its audio backwards",
        description="ffmpeg `reverse` + `areverse`. Buffers the whole clip in RAM, so "
        "trim long sources first.",
    )
    rev.add_argument("input", metavar="input")
    r.bind(rev, "reverse", reverse)

    rot = r.parser_for(
        "rotate", parents=[r.output_parent], help="rotate 90/180/270 degrees clockwise"
    )
    rot.add_argument("input", metavar="input")
    rot.add_argument("--degrees", type=int, default=90, choices=(90, 180, 270))
    r.bind(rot, "rotate", rotate)

    flipper = r.parser_for(
        "flip", parents=[r.output_parent], help="mirror horizontally (h) or vertically (v)"
    )
    flipper.add_argument("input", metavar="input")
    flipper.add_argument("--axis", choices=("h", "v"), default="h")
    r.bind(flipper, "flip", flip)

    cropper = r.parser_for(
        "crop",
        parents=[r.output_parent],
        help="crop to a pixel box or a centred aspect ratio",
        description="Either --width/--height/--x/--y (pixels, centre by default) or "
        "--aspect 9:16 for the largest centred crop with that ratio. Sizes are rounded "
        "to even pixels for yuv420p.",
    )
    cropper.add_argument("input", metavar="input")
    cropper.add_argument("--width", type=int, metavar="PX")
    cropper.add_argument("--height", type=int, metavar="PX")
    cropper.add_argument("--x", type=float, default=None, metavar="PX")
    cropper.add_argument("--y", type=float, default=None, metavar="PX")
    cropper.add_argument("--aspect", metavar="W:H", help="e.g. 9:16, 1:1, 4:3")
    r.bind(cropper, "crop", crop)
