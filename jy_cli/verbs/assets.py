"""``jy asset import|list|prune`` — jy's own asset box (not 剪映's library).

Files are copied into a jy-owned directory (``$JY_ASSET_DIR`` or
``~/Movies/jy-assets``) with a small ``index.json`` manifest. Nothing here talks to
剪映 or the network: it is the bookkeeping jy needs so drafts can reference a stable
path instead of a TCC-protected Downloads file.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path

from ..envelope import JyError

SCHEMA = "jy.assets.v1"
INDEX = "index.json"


def asset_root():
    override = os.environ.get("JY_ASSET_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Movies" / "jy-assets"


def _index_path(root):
    return Path(root) / INDEX


def load_index(root):
    path = _index_path(root)
    if not path.is_file():
        return {"schema": SCHEMA, "assets": []}
    try:
        payload = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JyError(
            "INTERNAL", f"资产索引不可读：{path}（{exc}）", details={"path": str(path)}
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("assets"), list):
        raise JyError("INTERNAL", f"资产索引结构不对：{path}", details={"path": str(path)})
    return payload


def save_index(root, payload):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = _index_path(root)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
    os.replace(temporary, path)
    return path


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _staged_path(root, digest, name):
    return Path(root) / digest[:12] / name


def assets_import(ctx, args):
    """Copy files into the asset box and record them in the manifest."""
    root = asset_root()
    payload = load_index(root)
    by_hash = {entry.get("sha256"): entry for entry in payload["assets"]}
    imported = []
    for value in args.paths:
        source = Path(value).expanduser()
        if not source.is_file():
            raise JyError("NOT_FOUND", f"asset not found: {value}")
        digest = _sha256(source)
        existing = by_hash.get(digest)
        if existing and Path(existing["staged"]).is_file() and not args.force:
            imported.append({**existing, "deduped": True})
            continue
        target = _staged_path(root, digest, source.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        entry = {
            "sha256": digest,
            "original": str(source.resolve()),
            "staged": str(target.resolve()),
            "bytes": target.stat().st_size,
            "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        payload["assets"] = [item for item in payload["assets"] if item.get("sha256") != digest]
        payload["assets"].append(entry)
        by_hash[digest] = entry
        imported.append({**entry, "deduped": False})
    index_path = save_index(root, payload)
    return "executed", {
        "asset_root": str(root),
        "index": str(index_path),
        "count": len(imported),
        "assets": imported,
    }


def assets_list(ctx, args):
    root = asset_root()
    payload = load_index(root)
    rows = []
    for entry in payload["assets"]:
        staged = Path(entry.get("staged") or "")
        rows.append(
            {
                **entry,
                "present": staged.is_file(),
                "age_hours": round(
                    max(0.0, (time.time() - staged.stat().st_mtime) / 3600.0), 2
                )
                if staged.is_file()
                else None,
            }
        )
    return "readonly", {
        "asset_root": str(root),
        "count": len(rows),
        "missing": sum(1 for row in rows if not row["present"]),
        "bytes": sum(row.get("bytes") or 0 for row in rows),
        "assets": rows,
    }


def assets_prune(ctx, args):
    hours = float(args.older_than_hours)
    if hours < 0:
        raise JyError("VALIDATION", "--older-than-hours 必须 >= 0")
    root = asset_root()
    payload = load_index(root)
    cutoff = time.time() - hours * 3600.0
    removable = []
    kept = 0
    for entry in payload["assets"]:
        staged = Path(entry.get("staged") or "")
        if staged.is_file() and staged.stat().st_mtime <= cutoff:
            removable.append(entry)
        else:
            kept += 1
    removed = []
    if args.apply:
        for entry in removable:
            staged = Path(entry["staged"])
            try:
                staged.unlink()
                removed.append(str(staged))
                try:
                    staged.parent.rmdir()
                except OSError:
                    pass
            except OSError as exc:
                ctx.warnings.append(f"删不掉 {staged}: {exc}")
        payload["assets"] = [item for item in payload["assets"] if item not in removable]
        save_index(root, payload)
    return ("executed" if args.apply else "preview"), {
        "asset_root": str(root),
        "older_than_hours": hours,
        "apply": bool(args.apply),
        "would_remove": [entry["staged"] for entry in removable],
        "removed": removed,
        "kept": kept,
    }


def sweep_assets(*, older_than_hours=24.0, dry_run=True):
    """Remove staged assets older than the cutoff (used by ``jy clean --assets``)."""
    root = asset_root()
    payload = load_index(root)
    cutoff = time.time() - max(0.0, float(older_than_hours)) * 3600.0
    removed = []
    freed = 0
    kept = 0
    for entry in payload["assets"]:
        staged = Path(entry.get("staged") or "")
        if not staged.is_file() or staged.stat().st_mtime > cutoff:
            kept += 1
            continue
        freed += staged.stat().st_size
        removed.append({"kind": "asset", "path": str(staged), "bytes": staged.stat().st_size})
        if not dry_run:
            try:
                staged.unlink()
                try:
                    staged.parent.rmdir()
                except OSError:
                    pass
            except OSError:
                pass
    if removed and not dry_run:
        payload["assets"] = [
            item for item in payload["assets"] if Path(item.get("staged") or "").is_file()
        ]
        save_index(root, payload)
    return {"root": str(root), "dry_run": bool(dry_run), "removed": removed,
            "kept": kept, "freed_bytes": freed}


def register(r):
    group = r.parser_for(
        "asset",
        help="jy's own asset box (import/list/prune) — not 剪映's material library",
        description="把素材复制进 jy 自己的素材箱（$JY_ASSET_DIR 或 ~/Movies/jy-assets），"
        "并记在 index.json 里。它只是 jy 的记账，不会导入剪映素材库、也不联网。",
        group=True,
    )
    sub = group.add_subparsers(
        dest="asset_action", required=True, parser_class=r.parser_class, metavar="action"
    )

    importer = sub.add_parser(
        "import", parents=[r.verbose_parent], help="copy files into the asset box"
    )
    importer.add_argument("paths", nargs="+", metavar="path")
    importer.add_argument("--force", action="store_true", help="re-copy even if hashed")
    r.bind(importer, "asset import", assets_import)

    listing = sub.add_parser(
        "list", parents=[r.verbose_parent], help="what jy has staged (with missing flags)"
    )
    r.bind(listing, "asset list", assets_list)

    prune = sub.add_parser(
        "prune", parents=[r.verbose_parent], help="remove staged assets older than N hours"
    )
    prune.add_argument("--older-than-hours", type=float, default=168.0, metavar="H")
    prune.add_argument(
        "--apply", action="store_true", help="actually delete (dry-run by default)"
    )
    r.bind(prune, "asset prune", assets_prune)
