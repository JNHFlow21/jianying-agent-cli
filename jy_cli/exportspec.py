"""Export parameter validation and argv construction (pure, no subprocess).

Kept separate from the command handlers so the resolution/fit/fps/codec/rate
rules that mirror 剪映's export sheet live in one readable place.
"""

from __future__ import annotations

import os
import re

from .envelope import JyError

PRESETS = {
    "480p": (854, 480),
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
    "2k": (2560, 1440),
    "4k": (3840, 2160),
    "8k": (7680, 4320),
}
CODECS = {
    "h264": ("libx264", "x264"),
    "h265": ("libx265", "x265"),
    "h264_vt": ("h264_videotoolbox", "vt_h264"),
    "hevc_vt": ("hevc_videotoolbox", "vt_hevc"),
    "prores": ("prores_ks", "prores"),
}
CONTAINERS = (".mp4", ".mov", ".mkv")
FPS_CHOICES = ("same", "24", "25", "29.97", "30", "50", "59.94", "60")
PRESETS_X264 = (
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
)
_SIZE_RE = re.compile(r"^(\d{2,5})x(\d{2,5})$")
_BITRATE_RE = re.compile(r"^\d+(?:\.\d+)?[kKmM]?$")


def parse_size_token(spec):
    """Classify --resolution as a preset, an explicit WxH, or 'original'."""
    token = (spec or "").strip().lower()
    if token == "original":
        return "original", None
    if token in PRESETS:
        return "preset", PRESETS[token]
    match = _SIZE_RE.match(token)
    if match:
        width, height = int(match.group(1)), int(match.group(2))
        if width < 16 or height < 16:
            raise JyError("VALIDATION", f"resolution too small: {spec}")
        return "size", (width, height)
    raise JyError(
        "VALIDATION",
        f"unknown --resolution {spec!r}; use "
        + "|".join(PRESETS)
        + "|original|WxH",
    )


def target_size(spec, source):
    """Even pixel dimensions for the target frame, honouring source orientation."""
    kind, value = parse_size_token(spec)
    if kind == "original":
        width = source.get("width")
        height = source.get("height")
        if not width or not height:
            raise JyError(
                "VALIDATION",
                "--resolution original needs a source with video dimensions",
            )
        return _even(width), _even(height)
    width, height = value
    if kind == "preset" and source.get("width") and source.get("height"):
        if source["height"] > source["width"]:
            width, height = height, width
    return width, height


def _even(value):
    return max(2, int(value) - int(value) % 2)


def validate(args):
    """Cheap parameter checks that do not need the source probe."""
    warnings = []
    if args.quality is not None and not 1 <= args.quality <= 51:
        raise JyError("VALIDATION", "--quality must be between 1 and 51 (CRF)")
    if args.bitrate is not None and not _BITRATE_RE.match(args.bitrate):
        raise JyError(
            "VALIDATION",
            f"invalid --bitrate {args.bitrate!r}; use e.g. 8M, 12000k, 500000",
        )
    parse_size_token(args.resolution)
    suffix = os.path.splitext(args.output)[1].lower()
    if suffix not in CONTAINERS:
        raise JyError(
            "VALIDATION",
            f"export container {suffix or '(none)'!r} not supported; use "
            + ", ".join(CONTAINERS),
        )
    if args.no_pad and args.fit != "contain":
        warnings.append("--no-pad only applies to --fit contain; ignored")
    if args.faststart and suffix not in (".mp4", ".mov"):
        warnings.append(f"faststart not applicable to {suffix}; ignored")
    return warnings


def required_encoder(args):
    """Resolve --codec/--hw/--bitdepth to (encoder, family, codec_key)."""
    codec = args.codec
    if args.hw:
        if codec in ("h264", "h264_vt"):
            codec = "h264_vt"
        elif codec in ("h265", "hevc_vt"):
            codec = "hevc_vt"
        else:
            raise JyError("VALIDATION", "--hw supports h264/h265 only, not prores")
    if args.bitdepth == 10 and codec not in ("h265", "hevc_vt"):
        raise JyError(
            "VALIDATION",
            "--bitdepth 10 requires --codec h265 (or --hw with h265)",
        )
    encoder, family = CODECS[codec]
    return encoder, family, codec


def build_argv(tool, args, source, input_path, output_path):
    """Assemble the full ffmpeg argv plus what was planned (for --dry-run/data)."""
    warnings = []
    encoder, family, codec_key = required_encoder(args)
    suffix = os.path.splitext(output_path)[1].lower()
    if codec_key == "prores" and suffix not in (".mov", ".mkv"):
        raise JyError("VALIDATION", "prores output requires .mov or .mkv")

    size = target_size(args.resolution, source)
    fps = None if args.fps == "same" else str(args.fps)
    video_filter = _video_filter(args, size, fps)
    video_args, pix_fmt = _video_args(args, encoder, family, codec_key, warnings)

    argv = [
        tool.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        input_path,
    ]
    if video_filter:
        argv.extend(["-vf", video_filter])
    argv.extend(video_args)
    argv.extend(_audio_args(args))
    faststart = bool(args.faststart and suffix in (".mp4", ".mov"))
    if faststart:
        argv.extend(["-movflags", "+faststart"])
    argv.extend(["-y", output_path])

    planned = {
        "resolution": args.resolution,
        "width": size[0],
        "height": size[1],
        "fit": args.fit,
        "pad": bool(args.fit == "contain" and not args.no_pad),
        "fps": fps or source.get("fps", {}).get("value") or None,
        "codec": codec_key,
        "encoder": encoder,
        "pix_fmt": pix_fmt,
        "quality": None if args.bitrate else (args.quality or 20),
        "bitrate": args.bitrate,
        "preset": args.preset if family in ("x264", "x265") else None,
        "audio_codec": args.audio_codec,
        "faststart": faststart,
    }
    return argv, planned, warnings


def _video_filter(args, size, fps):
    filters = []
    if size:
        width, height = size
        if args.fit == "stretch":
            filters.append(f"scale={width}:{height}:flags=lanczos")
        elif args.fit == "cover":
            filters.append(
                f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos"
            )
            filters.append(f"crop={width}:{height}")
        else:
            filters.append(
                f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos"
            )
            if not args.no_pad:
                filters.append(f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black")
    if fps:
        filters.append(f"fps={fps}")
    if filters:
        filters.append("setsar=1")
    return ",".join(filters)


def _video_args(args, encoder, family, codec_key, warnings):
    if family == "prores":
        if args.quality is not None:
            warnings.append("--quality is ignored by prores")
        if args.bitrate:
            warnings.append("--bitrate is ignored by prores")
        return ["-c:v", encoder, "-profile:v", "3", "-pix_fmt", "yuv422p10le"], "yuv422p10le"

    if args.bitdepth == 10:
        pix_fmt = "yuv420p10le"
    else:
        pix_fmt = "yuv420p"

    if family in ("x264", "x265"):
        video = ["-c:v", encoder, "-preset", args.preset, "-pix_fmt", pix_fmt]
        if family == "x265":
            video.extend(["-tag:v", "hvc1"])
            if args.bitdepth == 10:
                video.extend(["-profile:v", "main10"])
        if args.bitrate:
            video.extend(["-b:v", args.bitrate])
        else:
            video.extend(["-crf", str(args.quality or 20)])
        return video, pix_fmt

    # VideoToolbox encoders take an ABR bitrate or a 1-100 quality index.
    video = ["-c:v", encoder, "-pix_fmt", pix_fmt]
    if args.bitrate:
        video.extend(["-b:v", args.bitrate])
    else:
        crf = args.quality or 20
        video.extend(["-q:v", str(max(1, min(100, round((52 - crf) * 100 / 51))))])
    return video, pix_fmt


def _audio_args(args):
    if args.audio_codec == "none":
        return ["-an"]
    if args.audio_codec == "copy":
        return ["-c:a", "copy"]
    return [
        "-c:a",
        "aac",
        "-b:a",
        args.audio_bitrate,
        "-ar",
        str(args.audio_sample_rate),
    ]
