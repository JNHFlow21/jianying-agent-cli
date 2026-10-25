"""``jy cover``: extract a cover frame and embed it as the MP4 cover art."""

from __future__ import annotations

import os
import tempfile

from .. import ffmpeg
from ..envelope import JyError
from ._common import (
    _checked_output,
    _input_summary,
    _parse_at,
    _probe,
    _require_video,
    _require_video_container,
    _movflags,
    _resolve_input,
    _run,
)


def cover(ctx, args):
    """Pull one frame out and mux it back as an attached picture (covr)."""
    tool = ctx.require()
    input_path = _resolve_input(args.input)
    source = _probe(tool, input_path)
    _require_video(source)
    _require_video_container(args.output)
    at_seconds = _parse_at(args.at, source["duration_s"] or 0.0)
    output = _checked_output(args.output, args.force)
    quality = int(args.quality)
    if not 1 <= quality <= 31:
        raise JyError("VALIDATION", "--quality must be 1..31 (ffmpeg -q:v)")

    with tempfile.TemporaryDirectory(prefix="jy-cover-") as tmp_dir:
        frame_path = os.path.join(tmp_dir, "cover.jpg")
        extract = [
            tool.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-ss", f"{at_seconds:g}", "-i", str(input_path),
            "-frames:v", "1", "-q:v", str(quality), "-y", frame_path,
        ]
        ffmpeg.run_ffmpeg(tool, extract, verbose=ctx.verbose)
        if not os.path.isfile(frame_path) or os.path.getsize(frame_path) == 0:
            raise JyError(
                "INTERNAL",
                f"frame extraction produced no image at {at_seconds:g}s",
                details={"argv": extract},
            )
        if args.cover_out:
            target = _checked_output(args.cover_out, args.force)
            with open(frame_path, "rb") as handle:
                target.write_bytes(handle.read())

        argv = [
            tool.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", str(input_path), "-i", frame_path,
            "-map", "0", "-map", "1",
            "-c", "copy",
            "-disposition:v:1", "attached_pic",
            "-metadata:s:v:1", "title=Cover (front)",
            "-metadata:s:v:1", "comment=Cover (front)",
        ]
        argv.extend(_movflags(str(output)))
        argv.extend(["-y", str(output)])
        info = _run(tool, argv, output, ctx.verbose)

    streams = ffmpeg.probe_streams(tool, str(output))
    attached = [
        stream for stream in streams if (stream.get("disposition") or {}).get("attached_pic")
    ]
    if not attached:
        raise JyError(
            "INTERNAL",
            "cover mux reported success but the output has no attached_pic stream",
            details={"streams": streams},
        )
    data = {
        "input": _input_summary(source),
        "at": args.at,
        "at_s": round(at_seconds, 3),
        "cover": {
            "stream_index": attached[0].get("index"),
            "codec": attached[0].get("codec_name"),
            "width": attached[0].get("width"),
            "height": attached[0].get("height"),
            "saved_image": str(args.cover_out) if args.cover_out else None,
        },
        "output": info,
        "ffmpeg_argv": argv,
    }
    return "executed", data


def register(r):
    parser = r.parser_for(
        "cover",
        parents=[r.output_parent],
        help="extract a cover frame and embed it as the MP4 cover art",
        description="Pull one frame (--at seconds or percent) and mux it into the "
        "output as an attached_pic stream (mp4 'covr'), which is what Finder/QuickTime "
        "and most players show as the cover. Optionally also save the JPEG.",
    )
    parser.add_argument("input", metavar="input")
    parser.add_argument("--at", default="0", metavar="SEC|PCT", help="default 0")
    parser.add_argument(
        "--quality", type=int, default=2, help="JPEG quality (ffmpeg -q:v 1..31, lower is better)"
    )
    parser.add_argument(
        "--cover-out", metavar="PATH", help="also write the extracted JPEG here"
    )
    r.bind(parser, "cover", cover)
