"""``jy fonts``: enumerate usable fonts and validate a family name.

Sources: the usual macOS font directories plus 剪映's own bundled fonts
(``VideoFusion-macOS.app/Contents/Resources/Font``). Names are read straight out
of the sfnt ``name`` table with the stdlib — no fontconfig, no external tools.

``--check`` is strict on purpose: a typo'd family must fail fast instead of
silently falling back to a default face (which is how you get tofu).
"""

from __future__ import annotations

import difflib
import struct
from pathlib import Path

from ..envelope import JyError

APP_FONT_DIR = Path("/Applications/VideoFusion-macOS.app/Contents/Resources/Font")
SYSTEM_FONT_DIRS = (
    Path("/System/Library/Fonts"),
    Path("/System/Library/Fonts/Supplemental"),
    Path("/Library/Fonts"),
    Path.home() / "Library" / "Fonts",
)

FONT_SUFFIXES = (".ttf", ".ttc", ".otf", ".otc", ".dfont")


def _read_name_table(data, offset):
    """family name from the sfnt name table at ``offset`` (first font in a TTC)."""
    if offset + 12 > len(data):
        return None
    num_tables = struct.unpack(">H", data[offset + 4 : offset + 6])[0]
    for index in range(num_tables):
        record = offset + 12 + index * 16
        if record + 16 > len(data):
            return None
        tag = data[record : record + 4]
        if tag != b"name":
            continue
        table_offset = struct.unpack(">I", data[record + 8 : record + 12])[0]
        return _parse_names(data, table_offset)
    return None


def _parse_names(data, table_offset):
    if table_offset + 6 > len(data):
        return None
    count = struct.unpack(">H", data[table_offset + 2 : table_offset + 4])[0]
    strings_offset = table_offset + struct.unpack(">H", data[table_offset + 4 : table_offset + 6])[0]
    families = {}
    typographic = {}
    for index in range(count):
        record = table_offset + 6 + index * 12
        if record + 12 > len(data):
            break
        platform, encoding, language, name_id, length, offset = struct.unpack(
            ">HHHHHH", data[record : record + 12]
        )
        if name_id not in (1, 16):
            continue
        raw = data[strings_offset + offset : strings_offset + offset + length]
        try:
            text = raw.decode("utf-16-be") if platform == 3 else raw.decode("mac_roman")
        except UnicodeDecodeError:
            continue
        text = text.replace("\x00", "").strip()
        if not text:
            continue
        target = typographic if name_id == 16 else families
        # Prefer English-ish records (platform 3, language 0x409) but keep the first.
        if platform == 3 and language in (0x409, 0):
            target["en"] = text
        else:
            target.setdefault("other", text)
    chosen = typographic.get("en") or typographic.get("other") or families.get("en") or families.get("other")
    return chosen


def font_families(path):
    """Every family name inside one font file (TTCs carry several faces)."""
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if len(data) < 12:
        return []
    families = []
    try:
        if data[:4] == b"ttcf":
            count = struct.unpack(">I", data[8:12])[0]
            for index in range(min(count, 64)):
                offset = struct.unpack(">I", data[12 + index * 4 : 16 + index * 4])[0]
                name = _read_name_table(data, offset)
                if name and name not in families:
                    families.append(name)
        else:
            name = _read_name_table(data, 0)
            if name:
                families.append(name)
    except (struct.error, IndexError):
        return families
    return families


def font_family(path):
    families = font_families(path)
    return families[0] if families else None


def _collect(directory, *, recursive=True):
    entries = []
    if not directory.is_dir():
        return entries
    iterator = directory.rglob("*") if recursive else directory.iterdir()
    for path in iterator:
        if not path.is_file() or path.suffix.lower() not in FONT_SUFFIXES:
            continue
        families = font_families(path)
        stems = {path.stem}
        if not stems & set(families):
            stems.add(path.stem)
        for family in families:
            entries.append(
                {
                    "family": family,
                    "aliases": sorted(stem for stem in stems if stem),
                    "path": str(path),
                    "kind": path.suffix.lower().lstrip("."),
                    "source": "jianying" if APP_FONT_DIR in path.parents else "system",
                }
            )
    return entries


def index():
    """All discoverable families, with every file that provides them."""
    entries = []
    for directory in SYSTEM_FONT_DIRS:
        entries.extend(_collect(directory))
    entries.extend(_collect(APP_FONT_DIR))
    families = {}
    for entry in entries:
        families.setdefault(entry["family"], []).append(entry)
    return families


def _family_list(families):
    return sorted(families, key=lambda name: name.lower())


def quick_family_present(name):
    """Fast (~ms) sanity check used by text/subtitle warnings: no name-table parse.

    Looks for the family in file names only; False means "likely missing", which is
    enough to warn about a typo without paying for a full font scan on every burn.
    """
    key = "".join(str(name or "").lower().split())
    if not key:
        return False
    for directory in SYSTEM_FONT_DIRS + (APP_FONT_DIR,):
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if path.suffix.lower() not in FONT_SUFFIXES:
                continue
            if key in "".join(path.stem.lower().split()):
                return True
    return False


def resolve(name):
    """Strict family lookup: exact (case-insensitive) then typo suggestions."""
    families = index()
    key = str(name or "").strip()
    if not key:
        raise JyError("VALIDATION", "--font 不能为空；用 `jy fonts` 看可用字体")
    for family in families:
        if family.lower() == key.lower():
            files = families[family]
            return {
                "family": family,
                "files": files,
                "sources": sorted({entry["source"] for entry in files}),
                "matched_by": "family",
            }
    for family, files in families.items():
        if any(key.lower() == stem.lower() for entry in files for stem in entry.get("aliases") or []):
            return {
                "family": family,
                "files": files,
                "sources": sorted({entry["source"] for entry in files}),
                "matched_by": "filename",
            }
    names = _family_list(families)
    close = difflib.get_close_matches(key, names, n=5, cutoff=0.5)
    raise JyError(
        "VALIDATION",
        f"没有这个字族 {name!r}（系统 + 剪映自带字体都没找到）"
        + (f"；是不是想用：{'、'.join(close)}" if close else "")
        + "。用 `jy fonts` 列出全部字族。",
        details={"requested": key, "suggestions": close},
    )


def fonts(ctx, args):
    """List font families, or validate one family with --check/--font."""
    if args.check or args.font:
        wanted = args.check or args.font
        resolved = resolve(wanted)
        return "readonly", {
            "requested": wanted,
            "family": resolved["family"],
            "sources": resolved["sources"],
            "files": resolved["files"],
            "count": len(resolved["files"]),
        }
    families = index()
    rows = []
    for family in _family_list(families):
        files = families[family]
        if args.source and not any(entry["source"] == args.source for entry in files):
            continue
        rows.append(
            {
                "family": family,
                "sources": sorted({entry["source"] for entry in files}),
                "files": [
                    {"path": entry["path"], "kind": entry["kind"]} for entry in files
                ],
            }
        )
    if args.family:
        needle = args.family.lower()
        rows = [row for row in rows if needle in row["family"].lower()]
    return "readonly", {
        "count": len(rows),
        "family_count": len(families),
        "sources": {
            "system": [str(path) for path in SYSTEM_FONT_DIRS if path.is_dir()],
            "jianying": str(APP_FONT_DIR) if APP_FONT_DIR.is_dir() else None,
        },
        "families": rows,
    }


def register(r):
    parser = r.parser_for(
        "fonts",
        parents=[r.verbose_parent],
        help="list usable font families (system + 剪映) or validate --font",
        description="Enumerates macOS system fonts and 剪映's bundled Font directory and "
        "reads family names from the sfnt name table (stdlib only). --check/--font is "
        "strict: a typo fails with suggestions instead of silently falling back.",
    )
    parser.add_argument("--family", metavar="TEXT", help="substring filter for the listing")
    parser.add_argument(
        "--source", choices=("system", "jianying"), help="only fonts from this source"
    )
    parser.add_argument(
        "--check", metavar="FAMILY", help="validate a family name strictly (fails on typos)"
    )
    parser.add_argument(
        "--font", metavar="FAMILY", help="alias for --check (matches text/subtitle's --font-name)"
    )
    r.bind(parser, "fonts", fonts)
