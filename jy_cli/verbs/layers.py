"""``jy overlay``: picture-in-picture / sticker overlay (video or still)."""

from __future__ import annotations

from ..envelope import JyError
from ._common import (
    _checked_output,
    _even,
    _filter_complex_argv,
    _input_summary,
    _probe,
    _require_video,
    _require_video_container,
    _resolve_input,
    _run,
    anchor,
    between,
    is_image,
    number,
)


def overlay(ctx, args):
    """Composite a second video or an image onto the base clip."""
    tool = ctx.require(filters=["overlay"])
    base_path = _resolve_input(args.input)
    item_path = _resolve_input(args.overlay)
    base = _probe(tool, base_path)
    item = _probe(tool, item_path)
    _require_video(base)
    _require_video(item)
    _require_video_container(args.output)

    scale = between(args.scale, 0.05, 2.0, "--scale")
    opacity = between(args.opacity, 0.0, 100.0, "--opacity")
    start = between(args.start, 0.0, 86_400.0, "--start")
    duration = None if args.duration is None else between(
        args.duration, 0.05, 86_400.0, "--duration"
    )
    margin = between(args.margin, 0, 500, "--margin")
    if base["duration_s"] and start >= base["duration_s"]:
        raise JyError(
            "VALIDATION",
            f"--start {start:g}s is beyond the source duration {base['duration_s']:g}s",
        )
    end = start + duration if duration else None
    if base["duration_s"] and end and end > base["duration_s"] + 0.05:
        ctx.warnings.append("overlay window ends past the clip; it is clipped at EOF")

    item_width = _even(base["width"] * scale)
    item_height = max(2, _even(item_width * item["height"] / item["width"]))
    if item_height > base["height"]:
        ctx.warnings.append(
            f"scaled overlay is {item_width}x{item_height}, taller than the "
            f"{base['width']}x{base['height']} frame; part of it is off-frame"
        )
    x, y = anchor(args.position, base["width"], base["height"], item_width, item_height, int(margin))

    window = f":enable='between(t,{number(start)},{number(end)})'" if end else ""
    if opacity < 100:
        item_chain = (
            f"scale={item_width}:{item_height}:flags=lanczos,setsar=1,format=rgba,"
            f"colorchannelmixer=aa={number(opacity / 100.0)}"
        )
    else:
        item_chain = f"scale={item_width}:{item_height}:flags=lanczos,setsar=1,format=rgba"
    graph = (
        f"[1:v]{item_chain}[ov];"
        f"[0:v][ov]overlay=x={x}:y={y}:format=auto{window},format=yuv420p[v]"
    )
    input_args = ["-i", str(base_path)]
    if is_image(str(item_path)):
        input_args.extend(["-loop", "1", "-i", str(item_path)])
    else:
        if item["duration_s"] and end and item["duration_s"] < duration:
            ctx.warnings.append(
                f"overlay clip is {item['duration_s']:g}s, shorter than "
                f"--duration {duration:g}s; its last frame is repeated"
            )
        input_args.extend(["-i", str(item_path)])
    output = _checked_output(args.output, args.force)
    # A looped still never ends on its own, so cap the output at the base length.
    cap = base["duration_s"] or None
    if not cap and not end:
        raise JyError(
            "UNSUPPORTED",
            "cannot determine the base duration; pass --duration for the overlay window",
        )
    extra = ["-t", number(cap)] if cap else []
    argv = _filter_complex_argv(
        tool,
        input_args,
        graph,
        output,
        audio="source" if base["audio_streams"] else "none",
        extra=extra,
    )
    info = _run(tool, argv, output, ctx.verbose)
    data = {
        "input": _input_summary(base),
        "overlay": _input_summary(item),
        "placement": {
            "x": x,
            "y": y,
            "width": item_width,
            "height": item_height,
            "scale": scale,
            "opacity": opacity,
            "start_s": start,
            "end_s": end,
            "source": "image" if is_image(str(item_path)) else "video",
        },
        "output": info,
        "ffmpeg_argv": argv,
    }
    return "executed", data


def register(r):
    parser = r.parser_for(
        "overlay",
        parents=[r.output_parent],
        help="picture-in-picture / sticker overlay (second video or an image)",
        description="Composite a second video or a still image onto the base clip: "
        "position, scale, opacity and a time window. Audio comes from the base clip.",
    )
    parser.add_argument("input", metavar="base")
    parser.add_argument("overlay", metavar="overlay", help="video or image (.png/.jpg)")
    parser.add_argument(
        "--position",
        default="bottom-right",
        metavar="ANCHOR|X,Y",
        help="top-left|top|top-right|left|center|right|bottom-left|bottom|"
        "bottom-right|X,Y (default bottom-right)",
    )
    parser.add_argument("--scale", type=float, default=0.3, help="fraction of base width")
    parser.add_argument("--opacity", type=float, default=100.0, metavar="0-100")
    parser.add_argument("--start", type=float, default=0.0, metavar="SEC")
    parser.add_argument(
        "--duration", type=float, metavar="SEC", help="window length (default: to EOF)"
    )
    parser.add_argument("--margin", type=int, default=24, help="edge margin in px")
    r.bind(parser, "overlay", overlay)
