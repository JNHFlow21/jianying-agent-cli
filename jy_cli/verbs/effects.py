"""Effect verbs: subtitle, transition, text, montage, filter, key, beautify.

``transition`` and ``filter`` also speak 剪映's Chinese names. The alias table
lives in ``jy_cli/data/effect_aliases.json`` (a hand-distilled
剪映 name -> local ffmpeg approximation table); every resolved
alias is reported in the envelope as ``data.resolution`` with an explicit
fidelity verdict, so a local approximation can never masquerade as the 剪映
original. ``subtitle --export-srt`` is registered here too.
"""

from __future__ import annotations

import difflib
import json
import unicodedata
from functools import lru_cache
from pathlib import Path

from .. import commands, effects, ffmpeg
from ..envelope import JyError

ALIAS_FILE = Path(__file__).resolve().parent.parent / "data" / "effect_aliases.json"

# ffmpeg 9.0.2's xfade names. The selected build is authoritative at run time; this
# list is only used to recognise a raw name and to suggest close matches.
XFADE_NAMES = (
    "fade", "wipeleft", "wiperight", "wipeup", "wipedown", "slideleft", "slideright",
    "slideup", "slidedown", "circlecrop", "rectcrop", "distance", "fadeblack",
    "fadewhite", "radial", "smoothleft", "smoothright", "smoothup", "smoothdown",
    "circleopen", "circleclose", "vertopen", "vertclose", "horzopen", "horzclose",
    "dissolve", "pixelize", "diagtl", "diagtr", "diagbl", "diagbr", "hlslice",
    "hrslice", "vuslice", "vdslice", "hblur", "fadegrays", "wipetl", "wipetr",
    "wipebl", "wipebr", "squeezeh", "squeezev", "zoomin", "fadefast", "fadeslow",
    "hlwind", "hrwind", "vuwind", "vdwind", "coverleft", "coverright", "coverup",
    "coverdown", "revealleft", "revealright", "revealup", "revealdown",
)

FIDELITY_LEGEND = {
    "exact": "同一个 ffmpeg 原语，效果与剪映原条目同类",
    "close": "可辨认的近似（方向/观感一致，细节不同）",
    "rough": "机制不同的退化近似：能用，但明显不是剪映原效果",
    "no": "本地没有可用近似（UNSUPPORTED 并给出最接近的选项）",
}

_SUFFIXES = ("转场", "滤镜", "过渡", "效果")


def _normalise(value):
    """Case/space/full-width-insensitive key, with 剪映's kind suffix stripped."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    text = "".join(text.split())
    for suffix in _SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
            break
    return text


@lru_cache(maxsize=1)
def aliases():
    """The distilled alias table (loaded once per process)."""
    try:
        payload = json.loads(ALIAS_FILE.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - data is shipped
        raise JyError(
            "INTERNAL",
            f"别名表不可读：{ALIAS_FILE}（{exc}）",
            details={"path": str(ALIAS_FILE)},
        ) from exc
    payload["_transition_index"] = {_normalise(zh): zh for zh in payload["transitions"]}
    payload["_filter_index"] = {_normalise(zh): zh for zh in payload["filters"]}
    return payload


def _suggest(key, candidates, limit=5, table=None):
    """Close names, then (for queries naming a 剪映 group) that group's entries."""
    lookup = {}
    for item in candidates:
        if item:
            lookup.setdefault(_normalise(item), item)
    matches = difflib.get_close_matches(key, list(lookup), n=limit, cutoff=0.4)
    found = [lookup[match] for match in matches]
    if table:
        # "电影感"/"复古滤镜" name a 剪映 group rather than an entry: point at it.
        for zh, entry in table.items():
            group = _normalise(entry.get("group") or "")
            if group and group in key and zh not in found:
                found.append(zh)
    return found[:limit]


def resolve_transition(requested):
    """剪映 中文名 / xfade name -> local xfade + fidelity (or UNSUPPORTED)."""
    raw = str(requested)
    key = _normalise(raw)
    table = aliases()["transitions"]
    if not key:
        raise JyError("VALIDATION", "--type 不能为空", details={"requested": raw})

    if key in XFADE_NAMES:
        return {
            "requested": raw,
            "resolved": key,
            "engine": "local",
            "kind": "transition",
            "fidelity": "exact",
            "matched_by": "xfade",
            "note": "直接使用了 ffmpeg xfade 原语名（不是剪映条目）",
        }

    zh = aliases()["_transition_index"].get(key)
    matched_by = "zh"
    if zh is None:
        for candidate, entry in table.items():
            if entry.get("id_hint") and _normalise(entry["id_hint"]) == key:
                zh, matched_by = candidate, "id_hint"
                break
    if zh is None:
        suggestions = _suggest(
            key,
            list(table)
            + list(XFADE_NAMES)
            + [entry.get("id_hint") or "" for entry in table.values()],
            table=table,
        )
        raise JyError(
            "VALIDATION",
            f"不认识的转场 {raw!r}。剪映中文名或 ffmpeg xfade 名都可以；是不是想用："
            + "、".join(suggestions or ["（用 --list 看全部）"]),
            details={"requested": raw, "suggestions": suggestions},
        )

    entry = dict(table[zh])
    if entry.get("fidelity") == "no" or not entry.get("xfade"):
        alternatives = entry.get("alternatives") or []
        raise JyError(
            "UNSUPPORTED",
            f"剪映转场「{zh}」没有可用的本地近似（fidelity=no）：{entry.get('note')}。"
            + (f"最接近的可本地渲染的剪映名：{'、'.join(alternatives)}。" if alternatives else "")
            + "（用 `jy transition --list` 看全部条目）",
            details={
                "requested": raw,
                "resolved": None,
                "engine": "local",
                "fidelity": "no",
                "matched_by": matched_by,
                "catalogue_grade": entry.get("catalogue_grade"),
                "note": entry.get("note"),
                "alternatives": alternatives,
            },
        )
    return {
        "requested": raw,
        "resolved": entry["xfade"],
        "engine": "local",
        "kind": "transition",
        "fidelity": entry["fidelity"],
        "matched_by": matched_by,
        "catalogue_grade": entry.get("catalogue_grade"),
        "group": entry.get("group"),
        "id_hint": entry.get("id_hint"),
        "note": entry.get("note"),
    }


def resolve_filter(requested):
    """剪映 中文滤镜名 / local preset -> local preset + fidelity (or UNSUPPORTED)."""
    raw = str(requested)
    key = _normalise(raw)
    table = aliases()["filters"]
    if not key:
        raise JyError("VALIDATION", "--preset 不能为空", details={"requested": raw})

    zh = aliases()["_filter_index"].get(key)
    matched_by = "zh"
    if zh is None:
        for candidate, entry in table.items():
            if entry.get("id_hint") and _normalise(entry["id_hint"]) == key:
                zh, matched_by = candidate, "id_hint"
                break
    if zh is None and key in effects.FILTER_PRESETS:
        return {
            "requested": raw,
            "resolved": key,
            "engine": "local",
            "kind": "filter",
            "fidelity": "exact",
            "matched_by": "local_preset",
            "note": "本动词的本地预设名（不是剪映滤镜，jy 不会假装它是剪映条目）",
        }
    if zh is None:
        suggestions = _suggest(
            key, list(table) + list(effects.FILTER_PRESETS), table=table
        )
        raise JyError(
            "VALIDATION",
            f"不认识的滤镜 {raw!r}。剪映中文名或本地预设名都可以；是不是想用："
            + "、".join(suggestions or ["（用 --list 看全部）"]),
            details={"requested": raw, "suggestions": suggestions},
        )

    entry = dict(table[zh])
    if entry.get("fidelity") == "no" or not entry.get("local"):
        alternatives = entry.get("alternatives") or []
        raise JyError(
            "UNSUPPORTED",
            f"剪映滤镜「{zh}」没有可用的本地近似（fidelity=no）：{entry.get('note')}。"
            + (f"最接近的本地近似：{'、'.join(alternatives)}。" if alternatives else "")
            + "（用 `jy filter --list` 看全部条目）",
            details={
                "requested": raw,
                "resolved": None,
                "engine": "local",
                "fidelity": "no",
                "matched_by": matched_by,
                "catalogue_grade": entry.get("catalogue_grade"),
                "note": entry.get("note"),
                "alternatives": alternatives,
            },
        )
    return {
        "requested": raw,
        "resolved": entry["local"],
        "engine": "local",
        "kind": "filter",
        "fidelity": entry["fidelity"],
        "matched_by": matched_by,
        "catalogue_grade": entry.get("catalogue_grade"),
        "group": entry.get("group"),
        "id_hint": entry.get("id_hint"),
        "note": entry.get("note"),
    }


def _warn_rough(ctx, resolution):
    if resolution.get("fidelity") in ("rough", "no"):
        ctx.warnings.append(
            f"「{resolution['requested']}」本地近似保真度 {resolution['fidelity']}："
            f"{resolution.get('note') or ''}"
        )


# ---------------------------------------------------------------------------
# handlers (thin wrappers around the ffmpeg implementations)


def transition(ctx, args):
    """Transition by 剪映 中文名 or xfade name, with an explicit fidelity report."""
    if getattr(args, "list", False):
        tool = ctx.require(filters=["xfade"])
        names = ffmpeg.xfade_transitions(tool.ffmpeg)
        table = aliases()["transitions"]
        return "readonly", {
            "engine": "local",
            "ffmpeg": tool.ffmpeg,
            "count": len(names),
            "transitions": names,
            "alias_count": len(table),
            "aliases": [
                {
                    "requested": zh,
                    "xfade": entry.get("xfade"),
                    "fidelity": entry.get("fidelity"),
                    "catalogue_grade": entry.get("catalogue_grade"),
                    "group": entry.get("group"),
                    "id_hint": entry.get("id_hint"),
                    "alternatives": entry.get("alternatives"),
                }
                for zh, entry in table.items()
            ],
            "fidelity_legend": FIDELITY_LEGEND,
            "note": "剪映中文名在本地用 ffmpeg xfade 近似；fidelity 如实标注，绝不假装是剪映转场。",
        }

    resolution = resolve_transition(args.type)
    args.type = resolution["resolved"]
    _warn_rough(ctx, resolution)
    outcome, data = commands.transition(ctx, args)
    data["resolution"] = resolution
    return outcome, data


def filter_look(ctx, args):
    """Filter by 剪映 中文名 / local preset / LUT, with an explicit fidelity report."""
    if getattr(args, "list", False):
        table = aliases()["filters"]
        return "readonly", {
            "engine": "local",
            "presets": sorted(effects.FILTER_PRESETS),
            "alias_count": len(table),
            "aliases": [
                {
                    "requested": zh,
                    "local": entry.get("local"),
                    "fidelity": entry.get("fidelity"),
                    "catalogue_grade": entry.get("catalogue_grade"),
                    "group": entry.get("group"),
                    "id_hint": entry.get("id_hint"),
                    "alternatives": entry.get("alternatives"),
                }
                for zh, entry in table.items()
            ],
            "fidelity_legend": FIDELITY_LEGEND,
            "note": "剪映中文滤镜在本地用 ffmpeg 调色链近似；fidelity 如实标注，"
            "需要更接近请自备 .cube 用 --lut。",
        }

    if args.lut:
        if args.preset:
            raise JyError("VALIDATION", "use either --preset or --lut, not both")
        outcome, data = commands.filter_look(ctx, args)
        data["resolution"] = {
            "requested": args.lut,
            "resolved": "lut3d",
            "engine": "local",
            "kind": "filter",
            "fidelity": "exact",
            "matched_by": "lut",
            "note": "用户提供的 .cube LUT，本地 lut3d 应用（不是剪映滤镜库）",
        }
        return outcome, data

    if not args.input or not args.output:
        raise JyError(
            "VALIDATION", "-o/--output and <input> are required to render a filter"
        )
    if not args.preset:
        raise JyError("VALIDATION", "give --preset <剪映名|本地预设> or --lut <file.cube>")
    resolution = resolve_filter(args.preset)
    args.preset = resolution["resolved"]
    _warn_rough(ctx, resolution)
    outcome, data = commands.filter_look(ctx, args)
    data["resolution"] = resolution
    return outcome, data


def register(r):
    subtitle = r.parser_for(
        "subtitle",
        parents=[r.verbose_parent],
        help="burn an SRT/ASS subtitle file in, or export an SRT",
        description="Burn .srt/.ass subtitles with libass, or with --export-srt convert "
        "a subtitle file to SRT without rendering video. --style presets are ASS "
        "style overrides; --max-chars-per-line re-wraps .srt text before burning.",
    )
    subtitle.add_argument("input", nargs="?", metavar="input")
    subtitle.add_argument("--srt", required=True, metavar="FILE", help=".srt or .ass")
    subtitle.add_argument("--font-size", type=int, default=48)
    subtitle.add_argument("--font-name", default=effects.DEFAULT_FONT)
    subtitle.add_argument("--margin-v", type=int, default=40)
    subtitle.add_argument(
        "--style",
        choices=tuple(effects.SUBTITLE_STYLES),
        default="default",
        help="look preset applied as ASS style overrides",
    )
    subtitle.add_argument(
        "--position",
        choices=tuple(effects.ANCHOR_ALIGNMENT),
        help="anchor the whole subtitle track (default: the style's own alignment)",
    )
    subtitle.add_argument(
        "--max-chars-per-line",
        type=int,
        metavar="N",
        help="re-wrap .srt lines longer than N characters before burning",
    )
    subtitle.add_argument(
        "--export-srt",
        metavar="PATH",
        help="write SRT here instead of burning (no -o, no video needed)",
    )
    subtitle.add_argument("-o", "--output", metavar="PATH")
    subtitle.add_argument(
        "--force", action="store_true", help="overwrite an existing output"
    )
    r.bind(subtitle, "subtitle", commands.subtitle)

    transition_parser = r.parser_for(
        "transition",
        parents=[r.verbose_parent],
        help="apply one transition (剪映 中文名 or xfade name)",
        description="Join two clips with a single transition. --type accepts ffmpeg xfade "
        "names AND 剪映's Chinese names (叠化/闪白/黑场/推移/…), resolved to the best "
        "local approximation and reported in data.resolution{fidelity}. `--list` shows "
        "both columns. Geometry/fps are normalised to the first clip.",
    )
    transition_parser.add_argument("inputs", nargs="*", metavar="input")
    transition_parser.add_argument(
        "--type",
        default="fade",
        metavar="NAME",
        help="剪映中文名（叠化/闪白/模糊/拉近/…）或 xfade 名（fade/dissolve/wipeleft/…）",
    )
    transition_parser.add_argument("--duration", type=float, default=0.5, metavar="SEC")
    transition_parser.add_argument(
        "--offset",
        type=float,
        metavar="SEC",
        help="transition start on the first clip (default: first duration - duration)",
    )
    transition_parser.add_argument(
        "--list",
        action="store_true",
        help="list xfade names + 剪映 aliases with their fidelity and exit",
    )
    transition_parser.add_argument("-o", "--output", metavar="PATH")
    transition_parser.add_argument(
        "--force", action="store_true", help="overwrite an existing output"
    )
    r.bind(transition_parser, "transition", transition)

    text = r.parser_for(
        "text",
        parents=[r.output_parent],
        help="burn one styled text overlay in (ASS/libass)",
        description="Burn a single styled text line in with libass/ASS: animations "
        "(fadein/slideup/typewriter), outline and any position. Use \\n for a line "
        "break. Needs an ffmpeg build with libass.",
    )
    text.add_argument("input", metavar="input")
    text.add_argument("--content", required=True, metavar="TEXT")
    text.add_argument("--start", type=float, default=0.0, metavar="SEC")
    text.add_argument("--duration", type=float, default=3.0, metavar="SEC")
    text.add_argument("--position", default="bottom", metavar="top|center|bottom|X,Y")
    text.add_argument("--font-size", type=int, default=64)
    text.add_argument("--font-name", default=effects.DEFAULT_FONT)
    text.add_argument("--color", default="white", metavar="NAME|RRGGBB")
    text.add_argument("--outline", type=int, default=2, help="outline width in px")
    text.add_argument(
        "--animation",
        choices=("fadein", "slideup", "typewriter", "none"),
        default="none",
    )
    r.bind(text, "text", commands.text)

    montage = r.parser_for(
        "montage",
        parents=[r.output_parent],
        help="cut clips into segments and join them (蒙太奇 / beat cut)",
        description="Montage assembly: trim each clip to --per-clip seconds, or cut "
        "on --sync-beats timestamps (see `jy beats`), then join the segments "
        "(optionally with an xfade transition; 剪映 中文名 accepted too). "
        "Normalised to the first clip.",
    )
    montage.add_argument("inputs", nargs="+", metavar="input")
    mode = montage.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--per-clip", type=float, metavar="SEC", help="equal segment length per clip"
    )
    mode.add_argument(
        "--sync-beats",
        metavar="FILE",
        help="cut-timestamp file (milliseconds per line); one clip per cut",
    )
    montage.add_argument(
        "--transition", default="none", metavar="NAME", help="xfade name, 剪映 中文名, or none"
    )
    montage.add_argument("--transition-duration", type=float, default=0.3, metavar="SEC")
    r.bind(montage, "montage", commands.montage)

    look = r.parser_for(
        "filter",
        parents=[r.verbose_parent],
        help="apply a 剪映 中文滤镜名 / look preset / LUT at an intensity",
        description="Accepts 剪映's Chinese filter names (日系/黑白/暗调/…), the older "
        "local preset names (vivid/warm/cool/film/bw/vintage), or --lut file.cube. "
        "Every resolution is reported in data.resolution{fidelity}; `--list` shows "
        "剪映名 → 本地预设 → fidelity.",
    )
    look.add_argument("input", nargs="?", metavar="input")
    look.add_argument("-o", "--output", metavar="PATH")
    look.add_argument(
        "--force", action="store_true", help="overwrite an existing output"
    )
    look.add_argument(
        "--preset",
        metavar="NAME",
        help="剪映中文滤镜名（日系/黑白/暗调/…）或本地预设（vivid/film/…）",
    )
    look.add_argument("--lut", metavar="FILE", help=".cube 3D LUT (lut3d)")
    look.add_argument(
        "--intensity", type=float, default=100.0, metavar="0-100", help="blend strength"
    )
    look.add_argument(
        "--list", action="store_true", help="list 剪映 aliases -> local preset -> fidelity"
    )
    r.bind(look, "filter", filter_look)

    chroma = r.parser_for(
        "key",
        parents=[r.output_parent],
        help="chroma key one colour out over a colour or image background",
        description="色度抠图 only: colorkey + overlay. Smart matte / custom matte / "
        "matte outline need the 剪映 (VESDK) engine and are not implemented.",
    )
    chroma.add_argument("input", metavar="input")
    chroma.add_argument("--color", default="00FF00", metavar="RRGGBB")
    chroma.add_argument("--similarity", type=float, default=0.3)
    chroma.add_argument("--blend", type=float, default=0.1)
    chroma.add_argument(
        "--bg", default="color:black", metavar="color:NAME|#RRGGBB|image.jpg"
    )
    r.bind(chroma, "key", commands.key)

    beautify = r.parser_for(
        "beautify",
        parents=[r.output_parent],
        help="global soft-skin look (NOT 剪映 face-aware 美颜)",
        description="Offline approximation only: gblur + blend + eq + unsharp applied "
        "to the whole frame. This is a global look, not face detection — true 美颜/"
        "磨皮/瘦脸 needs the 剪映 (VESDK) engine.",
    )
    beautify.add_argument("input", metavar="input")
    beautify.add_argument("--smooth", type=int, default=40, metavar="0-100")
    beautify.add_argument("--brighten", type=int, default=20, metavar="0-100")
    beautify.add_argument("--sharpen", type=int, default=30, metavar="0-100")
    r.bind(beautify, "beautify", commands.beautify)
