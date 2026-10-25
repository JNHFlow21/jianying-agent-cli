"""Grading verbs: ``adjust``, ``curves``, ``hsl``, ``hdr``, ``mix``.

Every parameter uses 0-100 "how strong" semantics (0 = untouched), documented in
README.  These are ffmpeg primitives, not the 剪映 colour-engine presets.
"""

from __future__ import annotations

import subprocess

from .. import effects
from ..envelope import JyError
from ._common import (
    _checked_output,
    _even,
    _filter_complex_argv,
    _fps_rate,
    _input_summary,
    _movflags,
    _probe,
    _require_video,
    _require_video_container,
    _resolve_input,
    _run,
    _video_encode_args,
    between,
    number,
)

CURVE_PRESETS = (
    "none",
    "color_negative",
    "cross_process",
    "darker",
    "increase_contrast",
    "lighter",
    "linear_contrast",
    "medium_contrast",
    "negative",
    "strong_contrast",
    "vintage",
)
BLEND_MODES = (
    "normal",
    "screen",
    "multiply",
    "overlay",
    "lighten",
    "darken",
    "difference",
    "add",
    "average",
    "softlight",
    "hardlight",
    "exclusion",
    "subtract",
    "negation",
)


# ---------------------------------------------------------------------------
# adjust


def adjust_chain(args):
    """The documented 0-100 → ffmpeg mapping for ``jy adjust``."""
    brightness = between(args.brightness, 0, 100, "--brightness") / 100.0
    contrast = between(args.contrast, 0, 100, "--contrast") / 100.0
    saturation = between(args.saturation, 0, 100, "--saturation") / 100.0
    gamma = between(args.gamma, 0, 100, "--gamma") / 100.0
    temperature = between(args.temperature, 0, 100, "--temperature") / 100.0
    sharpness = between(args.sharpness, 0, 100, "--sharpness") / 100.0
    vignette = between(args.vignette, 0, 100, "--vignette") / 100.0

    chain = []
    eq = []
    if brightness:
        eq.append(f"brightness={number(brightness * 0.30)}")
    if contrast:
        eq.append(f"contrast={number(1 + contrast * 0.60)}")
    if saturation:
        eq.append(f"saturation={number(1 + saturation * 0.80)}")
    if gamma:
        eq.append(f"gamma={number(1 + gamma * 0.50)}")
    if eq:
        chain.append("eq=" + ":".join(eq))
    if temperature:
        chain.append(
            f"colorbalance=rs={number(temperature * 0.20)}:bs={number(-temperature * 0.20)}"
        )
    if sharpness:
        chain.append(f"unsharp=5:5:{number(sharpness * 1.5)}:5:5:0")
    if vignette:
        chain.append(f"vignette=angle={number(3.141592653589793 / 5 * (1 + vignette * 0.6667))}")
    return ",".join(chain), {
        "brightness": brightness,
        "contrast": contrast,
        "saturation": saturation,
        "gamma": gamma,
        "temperature": temperature,
        "sharpness": sharpness,
        "vignette": vignette,
    }


def adjust(ctx, args):
    """Per-parameter global grading (0 = untouched, 100 = strongest)."""
    chain, levels = adjust_chain(args)
    needed = []
    if chain:
        needed.append("eq")
    if levels["sharpness"]:
        needed.append("unsharp")
    if levels["temperature"]:
        needed.append("colorbalance")
    if levels["vignette"]:
        needed.append("vignette")
    tool = ctx.require(filters=needed)
    input_path = _resolve_input(args.input)
    source = _probe(tool, input_path)
    _require_video(source)
    _require_video_container(args.output)
    output = _checked_output(args.output, args.force)

    if not chain:
        ctx.warnings.append("all adjust strengths are 0; the output is a plain re-encode")
        argv = [tool.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                "-i", str(input_path)]
        argv.extend(_video_encode_args())
        if source["audio_streams"]:
            argv.extend(["-c:a", "aac", "-b:a", "192k"])
        else:
            argv.append("-an")
        argv.extend(_movflags(str(output)))
        argv.append(str(output))
        argv.append("-y")
    else:
        graph = f"[0:v]{chain},format=yuv420p[v]"
        argv = _filter_complex_argv(
            tool,
            ["-i", str(input_path)],
            graph,
            output,
            audio="source" if source["audio_streams"] else "none",
        )
    info = _run(tool, argv, output, ctx.verbose)
    data = {
        "input": _input_summary(source),
        "parameters": {name: round(value * 100, 1) for name, value in levels.items()},
        "chain": chain,
        "mapping": {
            "brightness": "eq=brightness 0..0.30",
            "contrast": "eq=contrast 1..1.60",
            "saturation": "eq=saturation 1..1.80",
            "gamma": "eq=gamma 1..1.50",
            "temperature": "colorbalance rs 0..0.20 / bs 0..-0.20 (warmer)",
            "sharpness": "unsharp luma_amount 0..1.5",
            "vignette": "vignette=angle PI/5..PI/3",
        },
        "output": info,
        "ffmpeg_argv": argv,
    }
    return "executed", data


# ---------------------------------------------------------------------------
# curves


def _parse_points(text):
    points = []
    for chunk in str(text).split():
        left, _, right = chunk.partition("/")
        try:
            x, y = float(left), float(right)
        except ValueError:
            raise JyError(
                "VALIDATION",
                f"invalid --points entry {chunk!r}; use 'x/y' pairs like 0/0 0.5/0.6 1/1",
            ) from None
        if not (0 <= x <= 1 and 0 <= y <= 1):
            raise JyError("VALIDATION", f"--points must stay in 0..1, got {chunk!r}")
        points.append((x, y))
    if len(points) < 2:
        raise JyError("VALIDATION", "--points needs at least two 'x/y' pairs")
    points.sort()
    return points


def curves(ctx, args):
    """Apply one of ffmpeg's curve presets, or explicit master points."""
    if bool(args.preset) == bool(args.points):
        raise JyError("VALIDATION", "use either --preset or --points, not both")
    if args.points:
        points = _parse_points(args.points)
        curve = "curves=master='" + " ".join(f"{number(x)}/{number(y)}" for x, y in points) + "'"
        label = {"points": [f"{x:g}/{y:g}" for x, y in points]}
    else:
        curve = f"curves=preset={args.preset}"
        label = {"preset": args.preset}
    tool = ctx.require(filters=["curves"])
    input_path = _resolve_input(args.input)
    source = _probe(tool, input_path)
    _require_video(source)
    _require_video_container(args.output)
    output = _checked_output(args.output, args.force)
    graph = f"[0:v]{curve},format=yuv420p[v]"
    argv = _filter_complex_argv(
        tool,
        ["-i", str(input_path)],
        graph,
        output,
        audio="source" if source["audio_streams"] else "none",
    )
    info = _run(tool, argv, output, ctx.verbose)
    return "executed", {
        "input": _input_summary(source),
        "curve": label,
        "chain": curve,
        "output": info,
        "ffmpeg_argv": argv,
    }


# ---------------------------------------------------------------------------
# hsl


def hsl(ctx, args):
    """Global hue rotation + saturation + lightness (not per-colour-band HSL)."""
    hue = between(args.hue, -180.0, 180.0, "--hue")
    saturation = between(args.saturation, -1.0, 1.0, "--saturation")
    lightness = between(args.lightness, -1.0, 1.0, "--lightness")
    if not any((hue, saturation, lightness)):
        raise JyError("VALIDATION", "give at least one of --hue/--saturation/--lightness")
    parts = [f"hue=h={number(hue)}:s={number(1 + saturation)}"]
    if lightness:
        parts.append(f"eq=brightness={number(lightness * 0.20)}")
    chain = ",".join(parts)
    tool = ctx.require(filters=["hue"] + (["eq"] if lightness else []))
    input_path = _resolve_input(args.input)
    source = _probe(tool, input_path)
    _require_video(source)
    _require_video_container(args.output)
    output = _checked_output(args.output, args.force)
    graph = f"[0:v]{chain},format=yuv420p[v]"
    argv = _filter_complex_argv(
        tool,
        ["-i", str(input_path)],
        graph,
        output,
        audio="source" if source["audio_streams"] else "none",
    )
    info = _run(tool, argv, output, ctx.verbose)
    return "executed", {
        "input": _input_summary(source),
        "hue_deg": hue,
        "saturation": saturation,
        "lightness": lightness,
        "chain": chain,
        "note": "global hue/saturation/lightness, not per-colour-range HSL",
        "output": info,
        "ffmpeg_argv": argv,
    }


# ---------------------------------------------------------------------------
# hdr


HDR_TRANSFERS = {"709": "bt709", "2020": "bt2020-10", "hlg": "arib-std-b67", "pq": "smpte2084"}
HDR_PRIMARIES = {"709": "bt709", "2020": "bt2020", "hlg": "bt2020", "pq": "bt2020"}
HDR_TO_SDR = (
    "zscale=pin=bt2020:tin={tin}:min=bt2020nc:t=linear:npl={npl},format=gbrpf32le,"
    "zscale=p=bt709:t=bt709:m=bt709,tonemap=tonemap={tonemap}:desat=2,"
    "zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
)

_tonemap_cache = {}


def tonemap_supported(tool):
    """Does this ffmpeg's zimg actually have a PQ/HLG -> linear path?

    Verified empirically per binary on a synthetic frame (cached): this machine's
    zimg answers ZIMG 3074 "no path between colorspaces" for every transfer
    conversion, while matrix/primaries conversion works.
    """
    key = tool.ffmpeg
    if key in _tonemap_cache:
        return _tonemap_cache[key]
    chain = HDR_TO_SDR.format(tin="smpte2084", npl="100", tonemap="hable")
    done = subprocess.run(
        [
            tool.ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=gray:s=64x64:d=0.1",
            "-vf",
            chain,
            "-frames:v",
            "1",
            "-f",
            "null",
            "-",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    _tonemap_cache[key] = done.returncode == 0
    return _tonemap_cache[key]


def hdr_chain(args):
    source = args.source
    target = args.target
    peak = between(args.peak, 100, 10_000, "--peak")
    if source == target:
        raise JyError("VALIDATION", "--source and --target must differ")
    hdr_source = source in ("hlg", "pq")
    hdr_target = target in ("hlg", "pq")
    if hdr_target and not hdr_source:
        raise JyError(
            "UNSUPPORTED",
            "HDR output（--to hlg/pq）没有实现：真正的 HDR 需要 10-bit + mastering "
            "metadata，本工具只做 HDR→SDR 的 tone mapping 和 SDR 709↔2020 转换。",
            details={"from": source, "to": target},
        )
    if hdr_source:
        chain = HDR_TO_SDR.format(
            tin=HDR_TRANSFERS[source], npl=number(peak), tonemap=args.tonemap
        )
        tags = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
        return chain, tags
    chain = (
        f"zscale=pin={HDR_PRIMARIES[source]}:tin={HDR_TRANSFERS[source]}:"
        f"min=bt2020nc:p={HDR_PRIMARIES[target]}:t={HDR_TRANSFERS[target]}:"
        f"m=bt2020nc:r=limited,format=yuv420p"
    )
    tags = [
        "-color_primaries", HDR_PRIMARIES[target],
        "-color_trc", HDR_TRANSFERS[target],
        "-colorspace", "bt2020nc",
    ]
    return chain, tags


def hdr(ctx, args):
    """709/2020 SDR conversion, and PQ/HLG -> 709 tone mapping when zimg can."""
    chain, tags = hdr_chain(args)
    hdr_source = args.source in ("hlg", "pq")
    needed = ["zscale"] + (["tonemap"] if hdr_source else [])
    tool = ctx.require(filters=needed)
    if hdr_source and not tonemap_supported(tool):
        raise JyError(
            "UNSUPPORTED",
            "这个 ffmpeg 的 zimg 没有 PQ/HLG→线性光的转换路径（ZIMG 3074 no path），"
            "所以无法做 HDR→SDR tone mapping；本机也没有可用的 Vulkan/libplacebo 回退。"
            "709↔2020 的 SDR 转换仍然可用，HDR 素材请先用别的工具转 SDR。",
            details={
                "ffmpeg": tool.ffmpeg,
                "probe": "zscale=t=linear (transfer conversion)",
                "hint": "jy hdr --from 709 --to 2020 works; pq/hlg input needs a build "
                "with zimg transfer support or libplacebo+Vulkan",
            },
        )
    input_path = _resolve_input(args.input)
    source = _probe(tool, input_path)
    _require_video(source)
    _require_video_container(args.output)
    output = _checked_output(args.output, args.force)
    graph = f"[0:v]{chain}[v]"
    argv = _filter_complex_argv(
        tool,
        ["-i", str(input_path)],
        graph,
        output,
        audio="source" if source["audio_streams"] else "none",
        extra=tags,
    )
    info = _run(tool, argv, output, ctx.verbose)
    return "executed", {
        "input": _input_summary(source),
        "from": args.source,
        "to": args.target,
        "tonemap": args.tonemap if hdr_source else None,
        "chain": chain,
        "color_tags": tags,
        "note": "8-bit yuv420p output; no HDR mastering metadata is written",
        "output": info,
        "ffmpeg_argv": argv,
    }


# ---------------------------------------------------------------------------
# mix


def mix(ctx, args):
    """Blend two clips with an ffmpeg blend mode; the second clip is normalised."""
    tool = ctx.require(filters=["blend"])
    base_path = _resolve_input(args.input)
    top_path = _resolve_input(args.top)
    base = _probe(tool, base_path)
    top = _probe(tool, top_path)
    _require_video(base)
    _require_video(top)
    _require_video_container(args.output)
    opacity = between(args.opacity, 0.0, 100.0, "--opacity")
    width, height = _even(base["width"]), _even(base["height"])
    fps_rate = _fps_rate(base)
    # blend's all_opacity weights the *first* input: 0 -> pure blend, 1 -> pure base.
    weight = 1.0 - opacity / 100.0
    graph = (
        f"[0:v]{effects.normalize_chain(width, height, fps_rate)}[base];"
        f"[1:v]{effects.normalize_chain(width, height, fps_rate)},setpts=PTS-STARTPTS[top];"
        f"[base][top]blend=all_mode={args.mode}:all_opacity={number(weight)},"
        "format=yuv420p[v]"
    )
    output = _checked_output(args.output, args.force)
    argv = _filter_complex_argv(
        tool,
        ["-i", str(base_path), "-i", str(top_path)],
        graph,
        output,
        audio="source" if base["audio_streams"] else "none",
        extra=["-t", number(base["duration_s"])] if base["duration_s"] else [],
    )
    info = _run(tool, argv, output, ctx.verbose)
    return "executed", {
        "input": _input_summary(base),
        "top": _input_summary(top),
        "mode": args.mode,
        "opacity": opacity,
        "output": info,
        "ffmpeg_argv": argv,
    }


def register(r):
    adjust_parser = r.parser_for(
        "adjust",
        parents=[r.output_parent],
        help="per-parameter grading (brightness/contrast/saturation/gamma/temperature/…)",
        description="Global grading with 0-100 strength per parameter (0 = untouched). "
        "Mapping: brightness→eq 0..0.30, contrast→eq 1..1.60, saturation→eq 1..1.80, "
        "gamma→eq 1..1.50, temperature→colorbalance 0..±0.20, sharpness→unsharp 0..1.5, "
        "vignette→vignette angle PI/5..PI/3.",
    )
    adjust_parser.add_argument("input", metavar="input")
    for name in (
        "brightness",
        "contrast",
        "saturation",
        "gamma",
        "temperature",
        "sharpness",
        "vignette",
    ):
        adjust_parser.add_argument(f"--{name}", type=int, default=0, metavar="0-100")
    r.bind(adjust_parser, "adjust", adjust)

    curves_parser = r.parser_for(
        "curves",
        parents=[r.output_parent],
        help="apply an ffmpeg curve preset or explicit master points",
        description="Curves: either a named ffmpeg preset (medium_contrast, vintage, "
        "cross_process…) or --points 'x/y x/y …' master control points in 0..1.",
    )
    curves_parser.add_argument("input", metavar="input")
    curve_source = curves_parser.add_mutually_exclusive_group(required=True)
    curve_source.add_argument("--preset", choices=CURVE_PRESETS)
    curve_source.add_argument("--points", metavar="'0/0 0.5/0.6 1/1'")
    r.bind(curves_parser, "curves", curves)

    hsl_parser = r.parser_for(
        "hsl",
        parents=[r.output_parent],
        help="global hue rotation / saturation / lightness",
        description="hue=h, saturation factor and lightness via eq. This is a global "
        "adjustment, not the 剪映 per-colour-range HSL panel.",
    )
    hsl_parser.add_argument("input", metavar="input")
    hsl_parser.add_argument("--hue", type=float, default=0.0, metavar="DEG", help="-180..180")
    hsl_parser.add_argument("--saturation", type=float, default=0.0, help="-1..1 (added to 1)")
    hsl_parser.add_argument("--lightness", type=float, default=0.0, help="-1..1")
    r.bind(hsl_parser, "hsl", hsl)

    hdr_parser = r.parser_for(
        "hdr",
        parents=[r.output_parent],
        help="709/2020/HLG/PQ conversion with tone mapping (zscale)",
        description="Colour-space conversion through zscale. HDR→SDR uses tonemap; "
        "SDR→HDR expands and tags the transfer. Output is 8-bit yuv420p without HDR "
        "mastering metadata.",
    )
    hdr_parser.add_argument("input", metavar="input")
    hdr_parser.add_argument("--from", dest="source", choices=("709", "2020", "hlg", "pq"),
                            default="709")
    hdr_parser.add_argument("--to", dest="target", choices=("709", "2020", "hlg", "pq"),
                            default="709")
    hdr_parser.add_argument(
        "--tonemap", choices=("hable", "mobius", "reinhard", "linear", "clip"), default="hable"
    )
    hdr_parser.add_argument("--peak", type=float, default=1000.0, metavar="NITS")
    r.bind(hdr_parser, "hdr", hdr)

    mix_parser = r.parser_for(
        "mix",
        parents=[r.output_parent],
        help="blend two clips with an ffmpeg blend mode",
        description="Composite the second clip over the first with a blend mode; the "
        "second clip is scaled/padded to the first.",
    )
    mix_parser.add_argument("input", metavar="base")
    mix_parser.add_argument("top", metavar="top")
    mix_parser.add_argument("--mode", choices=BLEND_MODES, default="screen")
    mix_parser.add_argument(
        "--opacity", type=float, default=100.0, metavar="0-100",
        help="weight of the blended (top) result; 0 = the base clip",
    )
    r.bind(mix_parser, "mix", mix)
