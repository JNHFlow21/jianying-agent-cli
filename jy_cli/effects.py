"""ASS scripting, colour helpers, look presets and filter graphs for the effect verbs.

Pure builders only (no subprocess, no printing, no filesystem writes), mirroring
``exportspec.py``: the handlers in ``commands.py`` probe/validate and this module
turns parameters into ffmpeg filter graphs and ASS scripts.
"""

from __future__ import annotations

import re

from .envelope import JyError

# Default family for burned-in text. This macOS has no PingFang and this libass
# build does no per-glyph fallback (missing glyphs -> tofu), so pick a family that
# ships with macOS and covers CJK: "Hiragino Sans GB" (verified on this machine).
DEFAULT_FONT = "Hiragino Sans GB"

# Subtitle look presets, expressed as libass style overrides for force_style=.
# ``karaoke`` is a *look* (bright fill, heavy outline): per-word highlighting needs
# word-level timing, which SRT does not carry.
SUBTITLE_STYLES = {
    "default": "",
    "bold": "Bold=1,Outline=3",
    "cinematic": (
        "Bold=1,PrimaryColour=&H00E6F2FF,OutlineColour=&H00000000,Outline=2,Shadow=1,Spacing=1"
    ),
    "karaoke": (
        "Bold=1,PrimaryColour=&H0000FFFF,OutlineColour=&H00000000,Outline=4,Shadow=0"
    ),
}

# Look presets at full strength; --intensity blends them back towards the source.
FILTER_PRESETS = {
    "vivid": "eq=saturation=1.55:contrast=1.15:gamma=1.02",
    "warm": "colorbalance=rs=0.10:gs=0.02:bs=-0.10,eq=saturation=1.08",
    "cool": "colorbalance=rs=-0.10:gs=0.0:bs=0.12,eq=contrast=1.08:saturation=1.02",
    "film": "curves=preset=medium_contrast,eq=saturation=0.88:gamma=1.05,"
    "colorbalance=rs=0.04:bs=0.05",
    "bw": "hue=s=0,eq=contrast=1.10",
    "vintage": "curves=preset=vintage,eq=saturation=0.82:gamma=1.06",
}
# Local look presets behind the 剪映 filter aliases (see data/effect_aliases.json).
# Names match the alias table's `local` field; chains are ffmpeg-native and the
# fidelity of each mapping is reported by the verb, never assumed here.
FILTER_PRESETS.update(
    {
        "none": "null",
        "clarity": "eq=contrast=1.12,unsharp=5:5:0.8:5:5:0",
        "natural": "eq=saturation=1.05",
        "fair_skin": "eq=brightness=0.06:gamma=1.05",
        "pink": "colorbalance=rs=0.06:bs=0.04,eq=saturation=1.05",
        "cool_white": "colorbalance=rs=-0.05:bs=0.10,curves=preset=lighter",
        "creamy": "gblur=sigma=3,eq=brightness=0.05:saturation=0.95",
        "hk_retro": "colorbalance=rs=0.08:bs=0.08,curves=preset=medium_contrast,"
        "noise=alls=6:allf=t",
        "japanese": "curves=preset=lighter,eq=saturation=0.8:gamma=1.05",
        "mono": "hue=s=0",
        "mono_hc": "hue=s=0,eq=contrast=1.4",
        "faded": "curves=preset=lighter,eq=saturation=0.7:contrast=0.92",
        "warm_sun": "colorbalance=rs=0.10:gs=0.03:bs=-0.08,eq=brightness=0.03",
        "teal_orange": "colorbalance=rs=0.10:gs=-0.02:bs=0.08,eq=saturation=1.1",
        "crisp": "eq=contrast=1.10,unsharp=5:5:0.6:5:5:0",
        "food": "eq=saturation=1.25:contrast=1.08,colorbalance=rs=0.06",
        "texture": "unsharp=7:7:1.0:7:7:0,eq=contrast=1.10",
        "cyber": "colorbalance=rs=-0.04:bs=0.12,eq=saturation=1.2:contrast=1.1",
        "ash": "eq=saturation=0.4:contrast=0.95,curves=preset=medium_contrast",
        "dark_cinema": "curves=preset=darker,eq=contrast=1.15,vignette=angle=PI/4.5",
        "anamorphic": "curves=preset=medium_contrast,"
        "drawbox=x=0:y=0:w=iw:h=ih*0.07:color=black:t=fill,"
        "drawbox=x=0:y=ih*0.93:w=iw:h=ih*0.07:color=black:t=fill",
        "morandi": "eq=saturation=0.6,curves=preset=lighter",
        "sketch": "edgedetect=low=0.1:high=0.3",
        "comic_bw": "edgedetect=mode=colormix:high=0.4,negate",
        "mosaic": "pixelize=w=16:h=16",
        "mirror": "hflip",
    }
)

# Filters each preset needs, so `jy filter` can demand exactly what it uses.
PRESET_FILTERS = {
    "vivid": ("eq",),
    "warm": ("colorbalance", "eq"),
    "cool": ("colorbalance", "eq"),
    "film": ("curves", "eq", "colorbalance"),
    "bw": ("hue", "eq"),
    "vintage": ("curves", "eq"),
    # alias-backed local presets
    "none": (),
    "clarity": ("eq", "unsharp"),
    "natural": ("eq",),
    "fair_skin": ("eq",),
    "pink": ("colorbalance", "eq"),
    "cool_white": ("colorbalance", "curves"),
    "creamy": ("gblur", "eq"),
    "hk_retro": ("colorbalance", "curves", "noise"),
    "japanese": ("curves", "eq"),
    "mono": ("hue",),
    "mono_hc": ("hue", "eq"),
    "faded": ("curves", "eq"),
    "warm_sun": ("colorbalance", "eq"),
    "teal_orange": ("colorbalance", "eq"),
    "crisp": ("eq", "unsharp"),
    "food": ("eq", "colorbalance"),
    "texture": ("unsharp", "eq"),
    "cyber": ("colorbalance", "eq"),
    "ash": ("eq", "curves"),
    "dark_cinema": ("curves", "eq", "vignette"),
    "anamorphic": ("curves", "drawbox"),
    "morandi": ("eq", "curves"),
    "sketch": ("edgedetect",),
    "comic_bw": ("edgedetect", "negate"),
    "mosaic": ("pixelize",),
    "mirror": ("hflip",),
}

# ASS alignment per anchor; 8=top, 5=middle, 2=bottom (all horizontally centred).
ANCHOR_ALIGNMENT = {"top": 8, "center": 5, "bottom": 2}
_ANCHOR_RATIO = {"top": 0.12, "center": 0.5, "bottom": 0.88}

_NAMED_COLORS = {
    "white": "FFFFFF",
    "black": "000000",
    "red": "FF0000",
    "green": "00FF00",
    "blue": "0000FF",
    "yellow": "FFFF00",
    "cyan": "00FFFF",
    "magenta": "FF00FF",
    "orange": "FF8C00",
    "pink": "FF9EC4",
    "gray": "808080",
    "grey": "808080",
}

_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff")
_HEX6 = re.compile(r"^[0-9a-f]{6}$")
# ffmpeg knows ~140 SVG colour names; accept the common ones and send the rest to
# RRGGBB instead of letting the ffmpeg colour parser fail late with INTERNAL.
_FFMPEG_COLORS = frozenset(
    """black white red green blue cyan magenta yellow gray grey silver maroon olive
    navy teal purple orange pink brown gold violet indigo khaki salmon beige ivory
    coral crimson aqua lime fuchsia darkblue darkgreen darkred skyblue""".split()
)


def normalize_hex(value, *, label="colour"):
    """Validate RRGGBB/#RRGGBB/0xRRGGBB (no alpha) and return bare upper-case hex."""
    token = str(value or "").strip().lower()
    for prefix in ("#", "0x"):
        if token.startswith(prefix):
            token = token[len(prefix) :]
    if not _HEX6.match(token):
        raise JyError(
            "VALIDATION",
            f"invalid {label} {value!r}; use RRGGBB, #RRGGBB or 0xRRGGBB",
        )
    return token.upper()


def parse_color(value, *, label="colour"):
    """A --color value (name or hex) as an ASS ``&HAABBGGRR`` literal."""
    token = str(value or "").strip().lower()
    if token in _NAMED_COLORS:
        hex_rgb = _NAMED_COLORS[token]
    else:
        hex_rgb = normalize_hex(token, label=label)
    red, green, blue = hex_rgb[0:2], hex_rgb[2:4], hex_rgb[4:6]
    return f"&H00{blue}{green}{red}"


def parse_ffmpeg_color(value):
    """A --bg ``color:`` value as something the ffmpeg ``color=`` source accepts."""
    token = str(value or "").strip()
    if not token:
        raise JyError("VALIDATION", "empty --bg colour")
    lowered = token.lower()
    for prefix in ("#", "0x"):
        if lowered.startswith(prefix):
            return "0x" + normalize_hex(token, label="background colour")
    if lowered in _FFMPEG_COLORS:
        return lowered
    if _HEX6.match(lowered):
        return "0x" + lowered.upper()
    raise JyError(
        "VALIDATION",
        f"invalid --bg colour {value!r}; use a common name (black, navy, white...) "
        "or #RRGGBB",
    )


def is_image_path(value):
    return str(value or "").lower().endswith(_IMAGE_SUFFIXES)


def ass_time(seconds):
    """H:MM:SS.cc, the only timestamp format libass accepts."""
    centis = int(round(max(0.0, float(seconds)) * 100))
    hours, remainder = divmod(centis, 360000)
    minutes, remainder = divmod(remainder, 6000)
    secs, fraction = divmod(remainder, 100)
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{fraction:02d}"


def ass_escape(text):
    """Escape ASS event text.

    Braces open an override block, so they must be backslash-escaped (libass
    renders ``\\{`` as a literal brace). Newlines become hard breaks. Commas are
    safe because Text is the last Dialogue field; backslashes are literal here.
    """
    normalized = str(text).replace("\r\n", "\n").replace("\r", "\n")
    parts = [part.replace("{", "\\{").replace("}", "\\}") for part in normalized.split("\n")]
    return "\\N".join(parts)


def anchor_point(position, width, height):
    """An (x, y) anchor in script pixels for top|center|bottom|X,Y."""
    token = str(position or "bottom").strip().lower()
    if token in _ANCHOR_RATIO:
        return width // 2, int(height * _ANCHOR_RATIO[token])
    parts = [piece.strip() for piece in token.split(",")]
    if len(parts) == 2:
        try:
            x, y = int(float(parts[0])), int(float(parts[1]))
        except ValueError:
            raise JyError(
                "VALIDATION",
                f"invalid --position {position!r}; use top|center|bottom|X,Y",
            ) from None
        if not (0 <= x <= width and 0 <= y <= height):
            raise JyError(
                "VALIDATION",
                f"--position {position} is outside the {width}x{height} frame",
            )
        return x, y
    raise JyError(
        "VALIDATION", f"invalid --position {position!r}; use top|center|bottom|X,Y"
    )


def text_events(content, *, start, duration, animation, x, y, font_size):
    """(start_s, end_s, event text) triples for one --content string.

    ``typewriter`` emits one event per character; the others emit one event.
    """
    rise = max(8, int(font_size * 0.5))
    anchor = f"\\an5\\pos({x},{y})"
    if animation == "typewriter":
        characters = list(content)
        step = duration / len(characters)
        events = []
        for index, _char in enumerate(characters, 1):
            begins = start + (index - 1) * step
            ends = start + duration if index == len(characters) else start + index * step
            events.append(
                (begins, ends, f"{{{anchor}}}{ass_escape(content[:index])}")
            )
        return events
    if animation == "fadein":
        override = f"{{{anchor}\\fad(300,0)}}"
    elif animation == "slideup":
        override = f"{{{anchor}\\move({x},{y + rise},{x},{y},0,400)\\fad(200,0)}}"
    else:
        override = f"{{{anchor}}}"
    return [(start, start + duration, override + ass_escape(content))]


def ass_script(*, width, height, events, font_name=DEFAULT_FONT, font_size=64,
               primary="&H00FFFFFF", outline_colour="&H00000000", outline=2,
               alignment=5, margin_v=0):
    """A complete ASS document with one Default style and the given events."""
    font = str(font_name).replace(",", " ").replace("\n", " ").strip() or DEFAULT_FONT
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {int(width)}",
        f"PlayResY: {int(height)}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{font},{int(font_size)},{primary},&H000000FF,{outline_colour},"
        f"&H00000000,0,0,0,0,100,100,0,0,1,{int(outline)},0,{int(alignment)},0,0,"
        f"{int(margin_v)},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for begins, ends, text in events:
        lines.append(
            f"Dialogue: 0,{ass_time(begins)},{ass_time(ends)},Default,,0,0,0,,{text}"
        )
    return "\n".join(lines) + "\n"


def parse_srt(text):
    """Cues from an SRT document: [{'timing': '...', 'lines': [...]}] (no re-timing)."""
    normalized = str(text).replace("\r\n", "\n").replace("\r", "\n").strip()
    cues = []
    for block in re.split(r"\n\s*\n", normalized):
        lines = [line for line in block.split("\n")]
        arrow = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if arrow is None:
            continue
        cues.append({"timing": lines[arrow].strip(), "lines": lines[arrow + 1 :]})
    return cues


def _wrap_line(line, limit):
    if limit <= 0 or len(line) <= limit:
        return [line]
    wrapped = []
    rest = line.rstrip()
    while len(rest) > limit:
        window = rest[:limit]
        space = window.rfind(" ")
        if space > 0:
            wrapped.append(rest[:space].rstrip())
            rest = rest[space + 1 :].lstrip()
        else:
            wrapped.append(window)
            rest = rest[limit:]
    if rest:
        wrapped.append(rest)
    return wrapped


def wrap_srt(text, limit):
    """Re-wrap every cue at ``limit`` characters; None when nothing parsed."""
    cues = parse_srt(text)
    if not cues:
        return None
    rendered = []
    for index, cue in enumerate(cues, 1):
        body = []
        for line in cue["lines"]:
            body.extend(_wrap_line(line, limit))
        rendered.append(f"{index}\n{cue['timing']}\n" + "\n".join(body))
    return "\n\n".join(rendered) + "\n"


def parse_beats(text):
    """Cut timestamps (milliseconds per line) as seconds; strictly increasing."""
    boundaries = []
    for number, raw in enumerate(str(text).splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            value = float(line)
        except ValueError:
            raise JyError(
                "VALIDATION", f"beats line {number} is not a number: {line!r}"
            ) from None
        if value <= 0:
            raise JyError("VALIDATION", f"beats line {number} must be > 0 ms")
        seconds = value / 1000.0
        if boundaries and seconds <= boundaries[-1]:
            raise JyError(
                "VALIDATION",
                f"beats line {number} ({value:g} ms) is not after the previous cut "
                f"({boundaries[-1] * 1000:g} ms)",
            )
        boundaries.append(seconds)
    if not boundaries:
        raise JyError("VALIDATION", "beats file has no timestamps")
    lengths = [boundaries[0]] + [
        later - earlier for earlier, later in zip(boundaries, boundaries[1:])
    ]
    return boundaries, lengths


# ---------------------------------------------------------------------------
# ffmpeg filter chains


def _num(value):
    return f"{float(value):g}"


def normalize_chain(width, height, fps_rate=None, reset_pts=False):
    """Scale/pad to an exact frame size, then pin pixel format (and timebase)."""
    chain = (
        f"scale={int(width)}:{int(height)}:force_original_aspect_ratio=decrease"
        ":flags=lanczos,"
        f"pad={int(width)}:{int(height)}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
    )
    if fps_rate:
        chain += f",fps={fps_rate}"
    chain += ",format=yuv420p"
    if reset_pts:
        chain += ",setpts=PTS-STARTPTS,settb=AVTB"
    return chain


def audio_chain(reset_pts=False):
    """48 kHz stereo fltp, the layout every join/acrossfade filter accepts."""
    chain = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
    if reset_pts:
        chain += ",asetpts=PTS-STARTPTS"
    return chain


def join_graph(*, count, durations, width, height, fps_rate=None, transition="none",
               transition_duration=0.3, has_audio=False, trims=None, first_offset=None):
    """Filter graph joining ``count`` inputs, with xfade/acrossfade or concat.

    ``durations`` are the effective (post-trim) lengths; they drive the xfade
    offsets. ``trims`` additionally cuts each input to its segment length
    (montage). ``first_offset`` overrides where the first transition starts
    (default: the first input ends with the transition).
    """
    parts = []
    for index in range(count):
        cut = ""
        audio_cut = ""
        if trims:
            cut = f"trim=duration={_num(trims[index])},"
            audio_cut = f"atrim=duration={_num(trims[index])},"
        if index == 0 and first_offset is not None:
            # Moving the transition earlier shortens clip 0 on the video side, so
            # its audio has to end at the same point or the muxed duration drifts.
            audio_cut = f"atrim=duration={_num(first_offset + transition_duration)}," + audio_cut
        parts.append(
            f"[{index}:v]{cut}{normalize_chain(width, height, fps_rate, reset_pts=bool(trims))}"
            f"[v{index}]"
        )
        if has_audio:
            parts.append(
                f"[{index}:a]{audio_cut}{audio_chain(reset_pts=bool(trims))}[a{index}]"
            )

    if count == 1:
        parts.append("[v0]null[v]")
        if has_audio:
            parts.append("[a0]anull[a]")
        return ";".join(parts)

    if transition in (None, "", "none"):
        # concat wants its inputs interleaved: [v0][a0][v1][a1]...
        sources = []
        for index in range(count):
            sources.append(f"[v{index}]")
            if has_audio:
                sources.append(f"[a{index}]")
        parts.append(
            "".join(sources)
            + f"concat=n={count}:v=1:a={1 if has_audio else 0}"
            + ("[v][a]" if has_audio else "[v]")
        )
        return ";".join(parts)

    video_label = "v0"
    audio_label = "a0" if has_audio else None
    offset = (
        durations[0] - transition_duration
        if first_offset is None
        else first_offset
    )
    for index in range(1, count):
        if index > 1:
            offset = offset + durations[index - 1] - transition_duration
        video_target = "v" if index == count - 1 else f"xv{index}"
        parts.append(
            f"[{video_label}][v{index}]xfade=transition={transition}"
            f":duration={_num(transition_duration)}:offset={_num(offset)}[{video_target}]"
        )
        video_label = video_target
        if has_audio:
            audio_target = "a" if index == count - 1 else f"xa{index}"
            parts.append(
                f"[{audio_label}][a{index}]acrossfade=d={_num(transition_duration)}"
                f":c1=tri:c2=tri[{audio_target}]"
            )
            audio_label = audio_target
    return ";".join(parts)


def filter_graph(chain, intensity):
    """Apply ``chain`` at full strength, then blend back by 0..100 intensity.

    ``blend``'s all_opacity is the weight of the *first* input, so the identity
    end of the range is opacity=1 (pure source) and the graded end is opacity=0.
    """
    opacity = 1.0 - max(0.0, min(100.0, float(intensity))) / 100.0
    return (
        f"[0:v]split=2[source][graded];[graded]{chain}[look];"
        f"[source][look]blend=all_mode=normal:all_opacity={_num(opacity)},"
        "format=yuv420p[v]"
    )


def beautify_graph(*, smooth, brighten, sharpen):
    """Global soft-skin look: gaussian soften, blended; then brighten and sharpen."""
    sigma = max(0.1, float(smooth) / 100.0 * 8.0)
    # Weight of the *unblurred* input; 60% blur at --smooth 100 keeps some detail.
    softness = 1.0 - float(smooth) / 100.0 * 0.6
    brightness = float(brighten) / 100.0 * 0.12
    saturation = 1.0 + float(brighten) / 100.0 * 0.10
    amount = float(sharpen) / 100.0 * 1.5
    return (
        "[0:v]split=2[detail][blursrc];"
        f"[blursrc]gblur=sigma={_num(sigma)}[blurred];"
        f"[detail][blurred]blend=all_mode=normal:all_opacity={_num(softness)}[soft];"
        f"[soft]eq=brightness={_num(brightness)}:saturation={_num(saturation)},"
        f"unsharp=5:5:{_num(amount)}:5:5:0,"
        "format=yuv420p[v]"
    )


def key_graph(*, colour_hex, similarity, blend, width, height, duration, fps_rate=None,
              background_colour=None, image_input=False):
    """Chroma key input 0 over either a solid colour or a still image (input 1)."""
    key = (
        f"colorkey=0x{normalize_hex(colour_hex, label='--color')}"
        f":{_num(similarity)}:{_num(blend)},format=yuva420p"
    )
    if image_input:
        base = (
            f"[1:v]scale={int(width)}:{int(height)}:force_original_aspect_ratio=increase"
            ":flags=lanczos:out_range=tv,"
            f"crop={int(width)}:{int(height)},setsar=1[bg]"
        )
    else:
        rate = f":r={fps_rate}" if fps_rate else ""
        base = (
            f"color=c={background_colour}:s={int(width)}x{int(height)}"
            f":d={_num(duration)}{rate}[bg]"
        )
    return (
        f"[0:v]{key}[keyed];{base};"
        "[bg][keyed]overlay=shortest=1:format=auto,format=yuv420p[v]"
    )
