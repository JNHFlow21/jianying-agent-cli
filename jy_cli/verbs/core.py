"""Core verbs: inspect, cut, join, speed, audio extraction, stills, export, gif."""

from __future__ import annotations

from .. import commands, exportspec


def register(r):
    r.add(
        "doctor",
        parents=[r.verbose_parent],
        help="report ffmpeg builds, capabilities, jobs, focus and 剪映 state",
        handler=commands.doctor,
    )

    probe = r.parser_for(
        "probe", parents=[r.verbose_parent], help="ffprobe files and concat compatibility"
    )
    probe.add_argument("files", nargs="+", metavar="file")
    r.bind(probe, "probe", commands.probe)

    trim = r.parser_for(
        "trim", parents=[r.output_parent], help="cut a time range out of a clip"
    )
    trim.add_argument("input", metavar="input")
    trim.add_argument("--start", type=float, default=0.0, metavar="SEC")
    bounds = trim.add_mutually_exclusive_group()
    bounds.add_argument("--end", type=float, metavar="SEC")
    bounds.add_argument("--duration", type=float, metavar="SEC")
    trim.add_argument(
        "--copy", action="store_true", help="stream copy instead of re-encoding"
    )
    r.bind(trim, "trim", commands.trim)

    concat = r.parser_for(
        "concat", parents=[r.output_parent], help="join clips (copy or normalised re-encode)"
    )
    concat.add_argument("inputs", nargs="+", metavar="input")
    concat.add_argument("--mode", choices=("auto", "copy", "reencode"), default="auto")
    r.bind(concat, "concat", commands.concat)

    speed = r.parser_for(
        "speed", parents=[r.output_parent], help="change playback speed (video + audio)"
    )
    speed.add_argument("input", metavar="input")
    speed.add_argument("--factor", type=float, required=True)
    speed.add_argument("--no-audio", action="store_true")
    r.bind(speed, "speed", commands.speed)

    extract = r.parser_for(
        "extract-audio", parents=[r.output_parent], help="export the audio track"
    )
    extract.add_argument("input", metavar="input")
    extract.add_argument("--bitrate", metavar="RATE", help="lossy audio bitrate (default 192k)")
    r.bind(extract, "extract-audio", commands.extract_audio)

    frame = r.parser_for(
        "frame", parents=[r.output_parent], help="extract one still frame"
    )
    frame.add_argument("input", metavar="input")
    frame.add_argument(
        "--at", required=True, metavar="SEC|PCT", help="e.g. 3.5 or 10%%"
    )
    r.bind(frame, "frame", commands.frame)

    export = r.parser_for(
        "export", parents=[r.output_parent, r.async_parent], help="the main encode verb"
    )
    export.add_argument("input", metavar="input")
    export.add_argument("--resolution", default="1080p", metavar="SPEC")
    export.add_argument("--fit", choices=("contain", "cover", "stretch"), default="contain")
    export.add_argument("--no-pad", action="store_true")
    export.add_argument("--fps", choices=exportspec.FPS_CHOICES, default="same")
    export.add_argument(
        "--codec",
        choices=("h264", "h265", "h264_vt", "hevc_vt", "prores"),
        default="h264",
    )
    rate = export.add_mutually_exclusive_group()
    rate.add_argument("--quality", type=int, metavar="1-51")
    rate.add_argument("--bitrate", metavar="RATE")
    export.add_argument("--preset", choices=exportspec.PRESETS_X264, default="veryfast")
    export.add_argument("--audio-codec", choices=("aac", "copy", "none"), default="aac")
    export.add_argument("--audio-bitrate", default="192k", metavar="RATE")
    export.add_argument("--audio-sample-rate", type=int, default=48000, metavar="HZ")
    export.add_argument("--bitdepth", type=int, choices=(8, 10), default=8)
    faststart = export.add_mutually_exclusive_group()
    faststart.add_argument("--faststart", dest="faststart", action="store_true")
    faststart.add_argument("--no-faststart", dest="faststart", action="store_false")
    export.set_defaults(faststart=True)
    export.add_argument("--hw", action="store_true", help="use VideoToolbox encoders")
    export.add_argument("--dry-run", action="store_true", help="print argv, encode nothing")
    r.bind(export, "export", commands.export)

    gif = r.parser_for(
        "gif", parents=[r.output_parent], help="palette-optimised GIF export"
    )
    gif.add_argument("input", metavar="input")
    gif.add_argument("--start", type=float, default=0.0, metavar="SEC")
    gif.add_argument("--duration", type=float, metavar="SEC")
    gif.add_argument("--width", type=int, default=480)
    gif.add_argument("--fps", dest="gif_fps", type=int, default=12)
    r.bind(gif, "gif", commands.gif)
