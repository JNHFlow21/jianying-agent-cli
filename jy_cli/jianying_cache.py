"""Materials actually available in the local 剪映 cache, by name (read-only).

剪映 resolves a draft's transitions / filters / effects / 花字 / text animations from
its own download cache (``…/User Data/Cache/{effect,artistEffect}/<resource_id>/<md5>``).
An id that is not cached renders as a silent no-op, so an agent needs to know which
ids are there and what they are called.

The packages carry no human names. 剪映 keeps the panel listings it fetched in
``…/Cache/ressdk_db/<n>/rp.db`` (table ``http_cache``: raw JSON responses). This module
copies those SQLite files (db + -wal + -shm) to a temp dir and reads the **copy**: the
live database is never opened in place and nothing under 剪映's folders is written.
Drafts are never read.

The joined index is memoised in ``~/.cache/jy/jianying_materials.json`` keyed by the
size/mtime of the cache dirs and databases, so repeat calls are fast.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path

from .envelope import JyError

CACHE_ENV = "JY_JIANYING_CACHE"
DEFAULT_CACHE = Path.home() / "Movies" / "JianyingPro" / "User Data" / "Cache"
PACKAGE_DIRS = ("effect", "artistEffect")
INDEX_SCHEMA = "jy.jianying_materials.v1"

# 剪映 effect_type -> jy kind (observed in 剪映 11.x panel listings).
EFFECT_TYPES = {
    19: "transition",
    12: "filter",
    7: "video_effect",
    18: "text_animation",
    1: "text_effect",
    6: "text_template",
    29: "text_glow",
    20: "face_beauty", 21: "face_beauty", 22: "face_beauty", 23: "face_beauty",
    24: "face_beauty", 148: "face_beauty",
    97: "video_mask",
    28: "matting_stroke",
    144: "color_preset",
    26: "voice_change",
}
# kinds `jy draft build --spec` can reference, and where
SPEC_KINDS = ("transition", "filter", "video_effect", "text_effect", "text_animation",
              "text_template")
_ID_RE = re.compile(r"\d{6,20}")
ANIMATION_TYPES = {"入场": "in", "出场": "out", "循环": "loop"}


def cache_root():
    override = os.environ.get(CACHE_ENV)
    return Path(override).expanduser() if override else DEFAULT_CACHE


def _packages(root):
    """{resource_id: absolute package dir} for every downloaded effect package."""
    found = {}
    for sub in PACKAGE_DIRS:
        base = root / sub
        if not base.is_dir():
            continue
        for entry in os.scandir(base):
            if not entry.is_dir() or not entry.name.isdigit():
                continue
            for pkg in os.scandir(entry.path):
                if pkg.is_dir():
                    found.setdefault(entry.name, pkg.path)
                    break
    return found


def _databases(root):
    base = root / "ressdk_db"
    return sorted(base.glob("*/rp.db")) if base.is_dir() else []


def _fingerprint(root, dbs):
    parts = []
    for path in [root / sub for sub in PACKAGE_DIRS] + list(dbs):
        for suffix in ("", "-wal"):
            candidate = Path(str(path) + suffix)
            try:
                stat = candidate.stat()
            except OSError:
                continue
            parts.append(f"{candidate}:{stat.st_size}:{stat.st_mtime_ns}")
    return "|".join(parts)


def _walk(node, wanted, names, category=""):
    if isinstance(node, dict):
        category = str(node.get("category_name") or category)
        ids = {str(node.get(key)) for key in ("resource_id", "effect_id", "id") if node.get(key)}
        hit = ids & wanted
        name = node.get("title") or node.get("name")
        if hit and name and isinstance(node.get("effect_type"), int):
            extra = node.get("extra")
            vip = None
            if isinstance(extra, str) and extra.startswith("{"):
                try:
                    vip = bool(json.loads(extra).get("is_vip"))
                except ValueError:
                    vip = None
            for rid in hit:
                names.setdefault(rid, {
                    "name": str(name),
                    "effect_type": node["effect_type"],
                    "category": category,
                    "vip": vip,
                })
        for value in node.values():
            if isinstance(value, (dict, list)):
                _walk(value, wanted, names, category)
    elif isinstance(node, list):
        for value in node:
            _walk(value, wanted, names, category)


def _names_from_copies(dbs, wanted):
    names = {}
    with tempfile.TemporaryDirectory(prefix="jy-ressdk-") as tmp:
        for index, db in enumerate(dbs):
            target_dir = Path(tmp) / str(index)
            target_dir.mkdir()
            for suffix in ("", "-wal", "-shm"):
                source = Path(str(db) + suffix)
                if source.is_file():
                    shutil.copy2(source, target_dir / source.name)
            try:
                conn = sqlite3.connect(str(target_dir / "rp.db"))
                rows = conn.execute("SELECT response_body FROM http_cache").fetchall()
                conn.close()
            except sqlite3.Error:
                continue
            for (body,) in rows:
                if not body or wanted.isdisjoint(_ID_RE.findall(body)):
                    continue
                try:
                    _walk(json.loads(body), wanted, names)
                except ValueError:
                    continue
    return names


def index(*, refresh=False):
    """The cached-material index: ``{root, items:[{kind,name,resource_id,path,…}]}``."""
    root = cache_root()
    if not root.is_dir():
        raise JyError(
            "UNAVAILABLE",
            f"没有找到剪映缓存目录 {root}：本机未安装剪映专业版，或从未打开过。"
            "安装并打开剪映、在里面用一次需要的素材后再试（或用 $JY_JIANYING_CACHE 指定目录）",
            details={"cache_root": str(root)},
        )
    packages = _packages(root)
    if not packages:
        raise JyError(
            "NOT_FOUND",
            f"剪映缓存 {root} 里没有已下载的素材包：先在剪映里点开要用的转场/滤镜/特效让它下载",
            details={"cache_root": str(root)},
        )
    dbs = _databases(root)
    fingerprint = _fingerprint(root, dbs)
    from .jobs import cache_root as jy_cache_root
    memo = jy_cache_root() / "jianying_materials.json"
    if not refresh:
        try:
            saved = json.loads(memo.read_text("utf-8"))
            if saved.get("schema") == INDEX_SCHEMA and saved.get("fingerprint") == fingerprint:
                return saved
        except (OSError, ValueError):
            pass
    names = _names_from_copies(dbs, set(packages))
    items = []
    for rid, path in packages.items():
        meta = names.get(rid)
        if not meta:
            continue
        kind = EFFECT_TYPES.get(meta["effect_type"], "other")
        # status: "cached" = package on disk. A future `jy effects fetch` can add
        # "remote" rows (known from 剪映's listings, not downloaded yet).
        item = {"kind": kind, "name": meta["name"], "resource_id": rid, "status": "cached",
                "path": path,
                "category": meta["category"], "vip": meta["vip"],
                "effect_type": meta["effect_type"]}
        if kind == "text_animation":
            item["animation_type"] = ANIMATION_TYPES.get(meta["category"], "in")
        items.append(item)
    items.sort(key=lambda it: (it["kind"], it["name"], it["resource_id"]))
    data = {"schema": INDEX_SCHEMA, "fingerprint": fingerprint, "cache_root": str(root),
            "packages": len(packages), "unnamed": len(packages) - len(items),
            "databases": len(dbs), "items": items}
    try:
        memo.parent.mkdir(parents=True, exist_ok=True)
        memo.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    except OSError:
        pass
    return data


def spec_snippet(item):
    """The exact fragment to paste into a `jy draft build` spec, or None."""
    ref = {"resource_id": item["resource_id"], "name": item["name"], "path": item["path"]}
    kind = item["kind"]
    if kind == "transition":
        return {"clips[i].transition": dict(ref, duration=0.5)}
    if kind == "filter":
        return {"filters[]": dict(ref, value=1.0)}
    if kind == "video_effect":
        return {"effects[]": ref}
    if kind == "text_effect":
        return {"texts[i].text_effect": ref}
    if kind == "text_animation":
        return {"texts[i].animations[]": dict(ref, type=item.get("animation_type", "in"),
                                               duration=0.5)}
    if kind == "text_template":
        return {"texts[i].text_template": ref}
    return None


def lookup(kind, name):
    """The single cached item of ``kind`` called ``name`` (VALIDATION if ambiguous)."""
    rows = [it for it in index()["items"] if it["kind"] == kind and it["name"] == name]
    if not rows:
        same_kind = sorted({it["name"] for it in index()["items"] if it["kind"] == kind})
        raise JyError(
            "NOT_FOUND",
            f"剪映缓存里没有名为「{name}」的 {kind}；可用的：{'、'.join(same_kind) or '（无）'}。"
            "先在剪映里点开该素材让它下载，或用 `jy effects cached --kind "
            f"{kind}` 查看",
            details={"kind": kind, "name": name, "available": same_kind},
        )
    if len({it["resource_id"] for it in rows}) > 1:
        raise JyError(
            "VALIDATION",
            f"「{name}」在剪映缓存里对应多个 {kind}，请改用 resource_id："
            + "、".join(it["resource_id"] for it in rows),
            details={"kind": kind, "name": name,
                     "candidates": [it["resource_id"] for it in rows]},
        )
    return rows[0]


def _fill(entry, kind, where, filled):
    """Give a name-only (or path-less) material reference its resource_id + path."""
    if not isinstance(entry, dict):
        return
    if entry.get("resource_id") and entry.get("path"):
        return
    if not entry.get("resource_id") and not entry.get("name"):
        return
    if entry.get("resource_id"):
        rows = [it for it in index()["items"]
                if it["resource_id"] == str(entry["resource_id"])]
        if not rows:
            return  # not cached / unnamed: leave it for draft.py to report as before
        item = rows[0]
    else:
        item = lookup(kind, str(entry["name"]))
    entry["resource_id"] = item["resource_id"]
    entry["path"] = item["path"]
    entry.setdefault("name", item["name"])
    filled.append({"where": where, "kind": kind, "name": item["name"],
                   "resource_id": item["resource_id"]})


def resolve_spec(spec):
    """Fill resource_id/path from the local cache for materials given by name.

    Returns the list of filled references (empty when nothing needed resolving; the
    cache is only scanned when some reference lacks an id or a path).
    """
    refs = []
    for index_, clip in enumerate(spec.get("clips") or []):
        if isinstance(clip, dict):
            if clip.get("transition"):
                refs.append((clip["transition"], "transition", f"clips[{index_}].transition"))
            for j, eff in enumerate(clip.get("video_effects") or []):
                refs.append((eff, "video_effect", f"clips[{index_}].video_effects[{j}]"))
    for j, filt in enumerate(spec.get("filters") or []):
        refs.append((filt, "filter", f"filters[{j}]"))
    for j, eff in enumerate(spec.get("effects") or []):
        refs.append((eff, "video_effect", f"effects[{j}]"))
    for i, text in enumerate(spec.get("texts") or []):
        if not isinstance(text, dict):
            continue
        if text.get("text_effect"):
            refs.append((text["text_effect"], "text_effect", f"texts[{i}].text_effect"))
        for j, anim in enumerate(text.get("animations") or []):
            refs.append((anim, "text_animation", f"texts[{i}].animations[{j}]"))
    pending = [r for r in refs if isinstance(r[0], dict)
               and not (r[0].get("resource_id") and r[0].get("path"))
               and (r[0].get("resource_id") or r[0].get("name"))]
    filled = []
    for entry, kind, where in pending:
        if not entry.get("resource_id"):
            _fill(entry, kind, where, filled)
        else:
            try:
                _fill(entry, kind, where, filled)
            except JyError:
                pass  # an explicit id stays the caller's responsibility
    return filled
