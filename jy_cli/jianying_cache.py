"""剪映 materials by name: what is cached locally, what can be fetched, and fetching it.

剪映 resolves a draft's transitions / filters / effects / 花字 / text animations from
its own download cache (``…/User Data/Cache/{effect,artistEffect}/<resource_id>/<md5>``).
An id that is not cached renders as a silent no-op, so an agent needs to know which
ids are there and what they are called.

The packages carry no human names. 剪映 keeps the panel listings it fetched in
``…/Cache/ressdk_db/<n>/rp.db`` (table ``http_cache``: raw JSON responses). This module
copies those SQLite files (db + -wal + -shm) to a temp dir and reads the **copy**: the
live database is never opened in place. Drafts and account data are never read.

The same listings also describe materials that are *not* downloaded yet (name, md5 and
the ByteDance-CDN package URLs 剪映 itself would use). ``fetch_material`` downloads one of
those packages with a plain HTTPS GET (no cookies, tokens or device ids), checks its md5,
and unzips it atomically into the layout 剪映 uses:
``<Cache>/<effect|artistEffect>/<resource_id>/<md5>/`` plus ``<md5>_modity_time.txt``.
That is the only write into 剪映's folders, it never overwrites an existing package, and
it only happens on an explicit `jy effects fetch` / `jy draft build --fetch`.
(剪映's live listing API is not called: its Loki endpoint rejects the public app key and
the Artist endpoint is request-signed, so search covers the panels 剪映 has loaded.)

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
INDEX_SCHEMA = "jy.jianying_materials.v2"

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


def _vip(node):
    extra = node.get("extra")
    if isinstance(extra, str) and extra.startswith("{"):
        try:
            return bool(json.loads(extra).get("is_vip"))
        except ValueError:
            return None
    return None


def _walk(node, catalog, category=""):
    """Collect every effect record (id, name, type, md5, CDN urls) from a listing body."""
    if isinstance(node, dict):
        category = str(node.get("category_name") or category)
        name = node.get("title") or node.get("name")
        rid = str(node.get("effect_id") or node.get("resource_id") or node.get("id") or "")
        if rid.isdigit() and name and isinstance(node.get("effect_type"), int):
            urls = [u for u in (node.get("item_urls") or node.get("url_list") or [])
                    if isinstance(u, str)]
            entry = {"name": str(name), "effect_type": node["effect_type"],
                     "category": category, "vip": _vip(node),
                     "md5": str(node.get("md5") or ""), "urls": urls,
                     "publish_source": str(node.get("publish_source") or "")}
            old = catalog.get(rid)
            if old is None or (not old["urls"] and urls):
                catalog[rid] = entry
        for value in node.values():
            if isinstance(value, (dict, list)):
                _walk(value, catalog, category)
    elif isinstance(node, list):
        for value in node:
            _walk(value, catalog, category)


def _catalog_from_copies(dbs):
    catalog = {}
    with tempfile.TemporaryDirectory(prefix="jy-ressdk-") as tmp:
        for index_, db in enumerate(dbs):
            target_dir = Path(tmp) / str(index_)
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
                if not body or '"effect_type"' not in body:
                    continue
                try:
                    _walk(json.loads(body), catalog)
                except ValueError:
                    continue
    return catalog


def _row(rid, meta, status, path=None):
    kind = EFFECT_TYPES.get(meta["effect_type"], "other")
    row = {"kind": kind, "name": meta["name"], "resource_id": rid, "status": status,
           "path": path, "category": meta["category"], "vip": meta["vip"],
           "effect_type": meta["effect_type"]}
    if kind == "text_animation":
        row["animation_type"] = ANIMATION_TYPES.get(meta["category"], "in")
    return row


def _require_root():
    root = cache_root()
    if not root.is_dir():
        raise JyError(
            "UNAVAILABLE",
            f"没有找到剪映缓存目录 {root}：本机未安装剪映专业版，或从未打开过。"
            "安装并打开剪映、在里面用一次需要的素材后再试（或用 $JY_JIANYING_CACHE 指定目录）",
            details={"cache_root": str(root)},
        )
    return root


def _memo_path():
    from .jobs import cache_root as jy_cache_root
    return jy_cache_root() / "jianying_materials.json"


def index(*, refresh=False):
    """``{cache_root, items:[cached rows], catalog:{rid: listing record}}``.

    ``items`` are packages on disk (status "cached"); ``catalog`` is every material
    剪映's cached panel listings mention, downloaded or not (used by search-remote/fetch).
    """
    root = _require_root()
    packages = _packages(root)
    dbs = _databases(root)
    fingerprint = _fingerprint(root, dbs)
    memo = _memo_path()
    if not refresh:
        try:
            saved = json.loads(memo.read_text("utf-8"))
            if saved.get("schema") == INDEX_SCHEMA and saved.get("fingerprint") == fingerprint:
                return saved
        except (OSError, ValueError):
            pass
    catalog = _catalog_from_copies(dbs)
    items = [_row(rid, catalog[rid], "cached", path)
             for rid, path in packages.items() if rid in catalog]
    items.sort(key=lambda it: (it["kind"], it["name"], it["resource_id"]))
    data = {"schema": INDEX_SCHEMA, "fingerprint": fingerprint, "cache_root": str(root),
            "packages": len(packages), "unnamed": len(packages) - len(items),
            "databases": len(dbs), "items": items, "catalog": catalog,
            "cached_ids": sorted(packages)}
    try:
        memo.parent.mkdir(parents=True, exist_ok=True)
        memo.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    except OSError:
        pass
    return data


def require_packages(data):
    if not data["packages"]:
        raise JyError(
            "NOT_FOUND",
            f"剪映缓存 {data['cache_root']} 里没有已下载的素材包：先在剪映里点开要用的素材让它下载，"
            "或用 `jy effects fetch <名字>` 下载",
            details={"cache_root": data["cache_root"]},
        )


def search_known(keyword, *, kind=None, limit=20):
    """Materials 剪映's cached listings know about, each marked cached|remote."""
    data = index()
    cached = set(data.get("cached_ids") or [])
    needle = str(keyword or "").strip().lower()
    rows = []
    for rid, meta in data["catalog"].items():
        if needle and needle not in meta["name"].lower() and needle != rid:
            continue
        row = _row(rid, meta, "cached" if rid in cached else "remote")
        if kind and row["kind"] != kind:
            continue
        row["downloadable"] = bool(meta["urls"] and meta["md5"])
        rows.append(row)
    rows.sort(key=lambda r: (r["name"].lower() != needle, r["status"] != "cached", r["kind"], r["name"]))
    return data, rows[:limit] if limit else rows


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
            "加 `--fetch` 让 jy draft build 自动下载，或先 `jy effects fetch <名字>`；"
            f"`jy effects search-remote <关键词> --kind {kind}` 可查可下载的素材",
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


def _fill(entry, kind, where, filled, fetch=False):
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
        try:
            item = lookup(kind, str(entry["name"]))
        except JyError as exc:
            if not fetch or exc.code != "NOT_FOUND":
                raise
            got = fetch_material(str(entry["name"]), kind=kind)
            entry["resource_id"], entry["path"] = got["resource_id"], got["path"]
            filled.append({"where": where, "kind": kind, "name": got["name"],
                           "resource_id": got["resource_id"], "fetched": True})
            return
    entry["resource_id"] = item["resource_id"]
    entry["path"] = item["path"]
    entry.setdefault("name", item["name"])
    filled.append({"where": where, "kind": kind, "name": item["name"],
                   "resource_id": item["resource_id"]})


def resolve_spec(spec, *, fetch=False):
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
            _fill(entry, kind, where, filled, fetch=fetch)
        else:
            try:
                _fill(entry, kind, where, filled)
            except JyError:
                pass  # an explicit id stays the caller's responsibility
    return filled


# --------------------------------------------------------------------------- #
# fetch: download a package 剪映's own listings point to, into 剪映's cache layout
# --------------------------------------------------------------------------- #

# CDN hosts 剪映's listings use for effect packages (ByteDance object storage).
ALLOWED_HOST_SUFFIXES = (".bytecdn.com", ".byteimg.com", ".byteeffecttos-g.com", ".ibytedtos.com",
                         ".bytetos.com", ".capcutstatic.com", ".vlabstatic.com",
                         ".draftstatic.com", ".pstatp.com", ".zjcdn.com", ".snssdk.com")
LOOPBACK_ENV = "JY_FETCH_ALLOW_LOOPBACK"  # tests only: allow http://127.0.0.1 mock servers
MAX_PACKAGE_BYTES = 512 * 1024 * 1024


def _url_ok(url):
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme == "https" and any(host.endswith(sfx) for sfx in ALLOWED_HOST_SUFFIXES):
        return True
    return (os.environ.get(LOOPBACK_ENV) == "1" and parts.scheme == "http"
            and host in ("127.0.0.1", "localhost"))


def _expired(url):
    match = re.search(r"[?&]x-expires=(\d+)", url)
    import time
    return bool(match) and int(match.group(1)) < time.time()


def _target_dir(meta):
    """effect/ for Loki/Vimo packages, artistEffect/ for Artist (user_post*) and text glow/stroke."""
    if meta["publish_source"].startswith("user_post") or meta["effect_type"] in (28, 29):
        return "artistEffect"
    return "effect"


def _download(urls, md5):
    import hashlib
    import urllib.error
    import urllib.request
    errors = []
    for url in urls:
        if not _url_ok(url):
            errors.append({"url": url[:80], "error": "host not allowed"})
            continue
        if _expired(url):
            errors.append({"url": url[:80], "error": "signed URL expired"})
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "jy-cli"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                blob = resp.read(MAX_PACKAGE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            errors.append({"url": url[:80], "http_status": exc.code})
            continue
        except (urllib.error.URLError, OSError) as exc:
            errors.append({"url": url[:80], "error": str(exc)[:120]})
            continue
        if len(blob) > MAX_PACKAGE_BYTES:
            errors.append({"url": url[:80], "error": "package too large"})
            continue
        got = hashlib.md5(blob).hexdigest()
        if md5 and got != md5:
            errors.append({"url": url[:80], "error": f"md5 mismatch {got}"})
            continue
        return blob, errors
    statuses = [e.get("http_status") for e in errors if e.get("http_status")]
    if statuses and all(code in (401, 403) for code in statuses):
        raise JyError("BLOCKED", "CDN 拒绝下载这个素材包（401/403）：服务器端限制，jy 不会绕过",
                      details={"attempts": errors})
    if any("expired" in str(e.get("error")) for e in errors):
        raise JyError("UNAVAILABLE", "剪映缓存里这个素材的下载地址已过期：在剪映里打开一次对应面板刷新列表后重试",
                      details={"attempts": errors})
    retriable = any("error" in e and "mismatch" not in e["error"] and "allowed" not in e["error"]
                    for e in errors) or any(code and code >= 500 for code in statuses)
    raise JyError("UNAVAILABLE", "素材包下载失败（网络或 CDN 错误）", retriable=bool(retriable),
                  details={"attempts": errors})


def _safe_extract(blob, dest):
    import io
    import zipfile
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise JyError("INTERNAL", "下载的素材包不是 zip", details={"error": str(exc)}) from exc
    names = []
    for info in archive.infolist():
        name = info.filename
        parts = Path(name).parts
        if name.startswith("/") or ".." in parts or (parts and parts[0].startswith("__MACOSX")):
            continue
        if Path(name).name == ".DS_Store":
            continue
        target = dest / name
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
        names.append(name)
    return names


def resolve_target(target, kind=None):
    """(resource_id, listing record) for a name or id, from 剪映's cached listings."""
    data = index()
    catalog = data["catalog"]
    key = str(target).strip()
    if key.isdigit():
        meta = catalog.get(key)
        if meta is None:
            raise JyError("NOT_FOUND", f"剪映缓存的素材列表里没有 resource_id {key}",
                          details={"resource_id": key})
        return key, meta
    rows = [(rid, m) for rid, m in catalog.items() if m["name"] == key
            and (not kind or EFFECT_TYPES.get(m["effect_type"], "other") == kind)]
    if not rows:
        near = [m["name"] for rid, m in catalog.items() if key.lower() in m["name"].lower()
                and (not kind or EFFECT_TYPES.get(m["effect_type"], "other") == kind)][:10]
        raise JyError(
            "NOT_FOUND",
            f"剪映缓存的素材列表里没有名为「{key}」的{kind or '素材'}"
            + (f"；相近的：{'、'.join(near)}" if near else "；用 `jy effects search-remote <关键词>` 查找"),
            details={"name": key, "kind": kind, "similar": near},
        )
    if len(rows) > 1:
        raise JyError(
            "VALIDATION",
            f"「{key}」对应多个素材，请加 --kind 或改用 resource_id：" + "、".join(
                f"{rid}({EFFECT_TYPES.get(m['effect_type'], 'other')})" for rid, m in rows),
            details={"candidates": [{"resource_id": rid,
                                     "kind": EFFECT_TYPES.get(m["effect_type"], "other")}
                                    for rid, m in rows]},
        )
    return rows[0]


def fetch_material(target, *, kind=None):
    """Download one package into ``<cache>/<effect|artistEffect>/<rid>/<md5>/`` (atomic, no overwrite)."""
    rid, meta = resolve_target(target, kind)
    root = _require_root()
    row = _row(rid, meta, "cached")
    existing = _packages(root).get(rid)
    if existing:
        row.update(path=existing, fetched=False, reason="already cached")
        return row
    md5 = meta["md5"]
    if not (meta["urls"] and re.fullmatch(r"[0-9a-f]{32}", md5 or "")):
        raise JyError("UNSUPPORTED", f"「{meta['name']}」在剪映缓存的列表里没有下载地址/md5，无法下载",
                      details={"resource_id": rid})
    blob, attempts = _download(meta["urls"], md5)
    base = root / _target_dir(meta) / rid
    final = base / md5
    base.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".jy-fetch-{md5}-", dir=str(base)))
    try:
        files = _safe_extract(blob, tmp)
        if final.exists():  # raced with 剪映 itself: keep theirs
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            os.rename(tmp, final)
            import time
            stamp = int(time.time())
            lines = [f"{final / name}, {stamp}" for name in files]
            (base / f"{md5}_modity_time.txt").write_text("\n".join(lines) + "\n", "utf-8")
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    try:
        _memo_path().unlink()
    except OSError:
        pass
    row.update(path=str(final), fetched=True, md5=md5, bytes=len(blob), files=len(files),
               cache_dir=_target_dir(meta), failed_attempts=attempts)
    return row
