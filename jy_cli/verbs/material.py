"""`jy effects` and `jy material` — the 剪映 material vocabulary, offline.

Auto-discovered: this module exposes :func:`register`, so ``cli.py`` can pick it up
without being edited.

``jy effects`` answers "what can I ask 剪映 for?" straight from a shipped copy of
the local catalog (``jy_cli/data/effects_catalog.json``: names of 剪映's bundled
resources; ids and names only, no assets). A fuller catalog can be supplied via
``$JY_EFFECTS_CATALOG`` or ``~/.config/jy/effects_catalog.json``, which win.
Nothing here touches the network or the app: these are read-only listings.

``jy material status`` reports which material kinds jy has actually proven to
render through 剪映, so a caller never has to guess.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .. import material
from ..envelope import JyError

BUNDLED_CATALOG = Path(__file__).resolve().parent.parent / "data" / "effects_catalog.json"


def _catalog_path():
    override = os.environ.get("JY_EFFECTS_CATALOG")
    if override:
        return Path(override).expanduser()
    user = Path.home() / ".config" / "jy" / "effects_catalog.json"
    return user if user.is_file() else BUNDLED_CATALOG

KIND_ZH = {
    "text": "文字/字幕",
    "subtitle_template": "字幕模板",
    "text_template": "文字模板",
    "lyric_animation": "歌词动效",
    "template": "剪同款模板",
    "tone": "音色",
    "voice_change": "变声",
    "effect": "画面特效",
    "face_beauty": "美颜美体",
    "video_mask": "蒙版",
    "matting_stroke": "抠像描边",
    "mix_mode": "混合模式",
    "sticker": "贴纸",
    "sound": "音效",
    "transition": "转场",
    "filter": "滤镜",
}


def _catalog():
    try:
        path = _catalog_path()
        return json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JyError("INTERNAL", f"effects 目录数据损坏：{path}",
                      details={"error": str(exc)}) from exc


def _match(entry, needle):
    haystack = " ".join(str(entry.get(key) or "") for key in ("name", "id", "kind"))
    return needle in haystack.lower()


def effects_list(ctx, args):
    data = _catalog()
    rows = data["effects"]
    if args.kind:
        rows = [row for row in rows if row["kind"] == args.kind]
    if args.limit is not None:
        rows = rows[: args.limit]
    kinds = {}
    for row in data["effects"]:
        kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
    return "readonly", {
        "total": len(data["effects"]),
        "returned": len(rows),
        "kind_counts": dict(sorted(kinds.items(), key=lambda kv: -kv[1])),
        "kind_zh": KIND_ZH,
        "asset_types": data["kinds"],
        "items": rows,
    }


def effects_search(ctx, args):
    data = _catalog()
    needle = str(args.query or "").strip().lower()
    if not needle:
        raise JyError("VALIDATION", "搜索词不能为空")
    rows = [row for row in data["effects"] if _match(row, needle)]
    if args.kind:
        rows = [row for row in rows if row["kind"] == args.kind]
    rows = rows[: args.limit or 20]
    if not rows:
        raise JyError(
            "NOT_FOUND",
            f"没有匹配 {args.query!r} 的素材；试试更短的关键词，或 `jy effects list --kind <kind>`",
            details={"query": args.query, "kind": args.kind},
        )
    return "readonly", {
        "query": args.query,
        "returned": len(rows),
        "items": rows,
        "note": "resource_id 可在 spec 里直接引用；这些条目来自本机剪映缓存，未缓存的资源导出时会缺依赖",
    }


def effects_params(ctx, args):
    """The parameter namespaces a spec may legally use."""
    data = _catalog()
    return "readonly", {
        "effect_sliders": data["parameter_namespaces"]["effects_adjust_*"],
        "face_and_body_sliders": data["parameter_namespaces"]["face_adjust_*"],
        "slider_params_item_shape": data["parameter_namespaces"]["slider_params_item_shape"],
        "note": "特效没有整体强度，只能用它声明过的 slider_params；滤镜才有 intensity 0..100",
    }


def effects_cached(ctx, args):
    """Materials really present in the local 剪映 cache, with paste-ready spec snippets."""
    from .. import jianying_cache

    data = jianying_cache.index(refresh=bool(args.refresh))
    jianying_cache.require_packages(data)
    rows = data["items"]
    if args.kind:
        rows = [row for row in rows if row["kind"] == args.kind]
    needle = str(args.search or "").strip().lower()
    if needle:
        rows = [row for row in rows if needle in row["name"].lower() or needle == row["resource_id"]]
    counts = {}
    for row in data["items"]:
        counts[row["kind"]] = counts.get(row["kind"], 0) + 1
    limited = rows[: args.limit] if args.limit else rows
    items = []
    for row in limited:
        item = {key: row.get(key) for key in ("kind", "name", "resource_id", "status", "category", "vip")}
        if row.get("animation_type"):
            item["animation_type"] = row["animation_type"]
        item["spec"] = jianying_cache.spec_snippet(row)
        items.append(item)
    return "readonly", {
        "cache_root": data["cache_root"],
        "packages": data["packages"],
        "unnamed_packages": data["unnamed"],
        "kind_counts": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "spec_kinds": list(jianying_cache.SPEC_KINDS),
        "matched": len(rows),
        "returned": len(items),
        "items": items,
        "note": "只列本机剪映已下载的素材包（未缓存的 id 渲染时会被静默忽略）。spec 里也可以只写 name，"
        "`jy draft build` 会按名字从缓存补全 resource_id 和 path；spec 为 null 的种类 draft build 还不支持。",
    }


def _public(row, with_spec):
    from .. import jianying_cache

    item = {key: row.get(key) for key in ("kind", "name", "resource_id", "status", "category", "vip")}
    if row.get("animation_type"):
        item["animation_type"] = row["animation_type"]
    if "downloadable" in row:
        item["downloadable"] = row["downloadable"]
    if with_spec:
        item["spec"] = jianying_cache.spec_snippet(row) if row.get("path") else None
    return item


def effects_search_remote(ctx, args):
    """Search every material 剪映's cached panel listings know, downloaded or not."""
    from .. import jianying_cache

    data, rows = jianying_cache.search_known(args.keyword, kind=args.kind, limit=args.limit)
    if not rows:
        raise JyError(
            "NOT_FOUND",
            f"剪映缓存的素材列表里没有匹配 {args.keyword!r} 的素材；在剪映里浏览一次对应面板可刷新列表",
            details={"keyword": args.keyword, "kind": args.kind},
        )
    return "readonly", {
        "cache_root": data["cache_root"],
        "known": len(data["catalog"]),
        "returned": len(rows),
        "items": [_public(row, with_spec=False) for row in rows],
        "source": "剪映 cached panel listings (Cache/ressdk_db http_cache, read from a copy)",
        "note": "status=remote 的素材可用 `jy effects fetch <名字|resource_id>` 下载；"
        "vip=true 需要剪映会员才能导出。列表只包含剪映曾经加载过的面板。",
    }


def effects_fetch(ctx, args):
    """Download one material package into 剪映's cache layout."""
    from .. import jianying_cache

    row = jianying_cache.fetch_material(args.target, kind=args.kind)
    item = _public(row, with_spec=True)
    for key in ("fetched", "reason", "md5", "bytes", "files", "cache_dir"):
        if key in row:
            item[key] = row[key]
    if row.get("vip"):
        ctx.warnings.append(f"「{row['name']}」是会员素材：非会员账号导出时剪映可能拒绝（BLOCKED），jy 不会绕过")
    return ("executed" if row.get("fetched") else "noop"), item


def material_status(ctx, args):
    return "readonly", material.describe()



def register(r):
    """Register `jy effects …` and `jy material status` (called by verbs.discover)."""
    effects = r.parser_for(
        "effects",
        parents=[r.verbose_parent],
        help="list/search the 剪映 material catalog (offline, from local caches)",
        description="剪映素材目录（离线）：剪映内置资源的 id 与中文名（可用 ~/.config/jy/effects_catalog.json 覆盖），"
        "外加 33 种 asset_type 和可用的参数命名空间。不联网、不启动剪映。",
        group=True,
    )
    effects_sub = effects.add_subparsers(
        dest="effects_action", required=True, parser_class=r.parser_class, metavar="action"
    )

    listing = effects_sub.add_parser(
        "list", help="list catalog entries with per-kind counts",
        description="按 kind 分组统计并列出素材；--kind 只看一类。",
    )
    listing.add_argument("--kind", metavar="KIND",
                         help="effect/filter/transition/face_beauty/sound/sticker/…")
    listing.add_argument("--limit", type=int, default=50, metavar="N")
    r.bind(listing, "effects list", effects_list)

    search = effects_sub.add_parser(
        "search", help="search catalog entries by Chinese name or id",
        description="按中文名或资源 id 搜索；命中项可直接把 id 写进 spec 的 resource_id。",
    )
    search.add_argument("query", metavar="QUERY")
    search.add_argument("--kind", metavar="KIND")
    search.add_argument("--limit", type=int, default=20, metavar="N")
    r.bind(search, "effects search", effects_search)

    cached = effects_sub.add_parser(
        "cached", help="list materials actually downloaded in the local 剪映 cache, by name",
        description="只读：扫描剪映本地素材缓存（Cache/effect、Cache/artistEffect），并从 ressdk_db 的"
        "副本里解析中文名；每条带可直接粘进 `jy draft build --spec` 的片段。不打开剪映、不读草稿、"
        "不写剪映目录（索引缓存在 ~/.cache/jy）。",
    )
    cached.add_argument("--kind", metavar="KIND",
                        help="transition/filter/video_effect/text_effect/text_animation/"
                        "text_template/face_beauty/…")
    cached.add_argument("--search", metavar="NAME", help="substring of the Chinese name (or an exact id)")
    cached.add_argument("--limit", type=int, default=50, metavar="N", help="0 = no limit")
    cached.add_argument("--refresh", action="store_true", help="rebuild the name index now")
    r.bind(cached, "effects cached", effects_cached)

    remote = effects_sub.add_parser(
        "search-remote", help="search all materials 剪映's cached listings know (cached + downloadable)",
        description="只读：在剪映缓存的面板列表（ressdk_db 副本）里按名字搜素材，含尚未下载的；"
        "每条带 status cached|remote、vip、downloadable。列表只覆盖剪映加载过的面板。",
    )
    remote.add_argument("keyword", metavar="KEYWORD")
    remote.add_argument("--kind", metavar="KIND",
                        help="transition/filter/video_effect/text_effect/text_animation/…")
    remote.add_argument("--limit", type=int, default=20, metavar="N", help="0 = no limit")
    r.bind(remote, "effects search-remote", effects_search_remote)

    fetch = effects_sub.add_parser(
        "fetch", help="download a 剪映 material package into 剪映's local cache",
        description="按名字或 resource_id 下载素材包：地址取自剪映缓存的面板列表（字节 CDN），"
        "校验 md5，原子解压到 Cache/<effect|artistEffect>/<resource_id>/<md5>/；已存在则不动。",
    )
    fetch.add_argument("target", metavar="NAME|RESOURCE_ID")
    fetch.add_argument("--kind", metavar="KIND", help="disambiguate a name shared by several kinds")
    r.bind(fetch, "effects fetch", effects_fetch)

    params = effects_sub.add_parser(
        "params", help="show legal parameter namespaces and their ranges",
        description="特效滑杆（effects_adjust_*）与美颜美体（face_adjust_*）的合法参数和值域。",
    )
    r.bind(params, "effects params", effects_params)

    mat = r.parser_for(
        "material",
        parents=[r.verbose_parent],
        help="report which 剪映 material kinds jy can author and render",
        description="素材图层实现状态：哪些已经本机剪映渲染验证，哪些只有 schema。",
        group=True,
    )
    mat_sub = mat.add_subparsers(
        dest="material_action", required=True, parser_class=r.parser_class, metavar="action"
    )
    status = mat_sub.add_parser("status", help="proven vs unverified material kinds")
    r.bind(status, "material status", material_status)

    register = mat_sub.add_parser(
        "register",
        help="add an authored draft to 剪映's draft list (root_meta_info.json)",
        description="把 jy 写的草稿登记进剪映草稿列表：往 root_meta_info.json 的 "
        "all_draft_store 追加/刷新一条，并把 draft_ids 同步为条目数（它是整数计数，不是数组）。"
        "默认只预演；--apply 才写入，且写入前自动备份索引。",
    )
    register.add_argument("draft", metavar="draft-dir", help="含 draft_info.json 的草稿目录")
    register.add_argument("--root-meta", metavar="FILE",
                          help="剪映草稿根的 root_meta_info.json（默认取草稿目录的上一级）")
    register.add_argument("--name", help="列表里显示的名字（默认用草稿 name）")
    register.add_argument("--apply", action="store_true",
                          help="真正写入索引（会先备份成 root_meta_info.json.bak-<epoch>）")
    r.bind(register, "material register", material_register)
    return effects


def material_register(ctx, args):
    """Register an authored draft folder in 剪映's draft list (root_meta_info.json)."""
    from .. import draft as draft_mod

    data = draft_mod.register_draft(
        args.draft,
        root_meta=args.root_meta,
        name=args.name,
        apply=bool(args.apply),
    )
    return ("executed" if data["applied"] else "preview"), data
