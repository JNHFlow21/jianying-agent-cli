"""剪映's live material catalog (Loki panels + search, Artist per-id lookup).

The identifiers below are the **public client identifiers of the shipping 剪映 app**
(read from its ``libVEConfig.dylib``), the same values every copy of the app sends —
comparable to Telegram's public ``api_id``. They are not user credentials. jy never
sends the user's cookies, tokens, account data or 剪映's real device id: it uses one
random synthetic 19-digit ``device_id``/``iid`` pair generated once per machine and kept
in ``~/.config/jy/device.json``.

Requests are sequential with a short timeout; panel listings are cached for a day under
``~/.cache/jy/live/`` so repeated searches do not hit the network.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .envelope import JyError

# --- public client identifiers of the shipping 剪映 app (libVEConfig.dylib) -------------
LOKI_BASE = "https://lv-effect.ulikecam.com"
LOKI_PARAMS = {
    "aid": "1059",
    "access_key": "0051d530508b11e9b441ed975323ebf8",  # gitleaks:allow (public app id)
    "app_version": "11.7.0",
    "version_code": "722688",
    "device_platform": "macos",
    "device_type": "Mac",
    "sdk_version": "22.4.0",
    "app_language": "zh-hans",
    "channel": "online",
}
ARTIST_BASE = "https://lv-api.ulikecam.com"
ARTIST_PARAMS = {"app_id": "3704", "device_platform": "mac", "version_code": "13148796",
                 "language": "zh-Hans"}
ARTIST_APP_VERSION = "11.7.0"
ARTIST_SIGN_PREFIX, ARTIST_SIGN_SUFFIX = "9e2c|", "|11ac"  # gitleaks:allow (public constants)
# -----------------------------------------------------------------------------------------

BASE_ENV = "JY_LIVE_BASE"            # tests: point both APIs at a mock server
ARTIST_BASE_ENV = "JY_LIVE_ARTIST_BASE"
OFFLINE_ENV = "JY_LIVE_OFFLINE"       # "1" disables live calls entirely
TIMEOUT_S = 12
PANEL_TTL_S = 24 * 3600

# Loki panel name -> jy kind (plural/singular as the server expects)
PANELS = {"transitions": "transition", "filter": "filter", "effects": "video_effect",
          "flower": "text_effect", "text": "text_animation", "sticker": "sticker"}
KIND_PANEL = {kind: panel for panel, kind in PANELS.items()}
ANIMATION_TYPES = {"入场": "in", "出场": "out", "循环": "loop"}
_MD5 = re.compile(r"^[0-9a-f]{32}$")


def _config_dir():
    return Path(os.environ.get("JY_CONFIG_DIR") or Path.home() / ".config" / "jy")


def _cache_dir():
    from .jobs import cache_root
    return cache_root() / "live"


def device_ids():
    """The machine's synthetic (random) device_id/iid, created on first use."""
    path = _config_dir() / "device.json"
    try:
        data = json.loads(path.read_text("utf-8"))
        if str(data.get("device_id", "")).isdigit() and str(data.get("iid", "")).isdigit():
            return data["device_id"], data["iid"]
    except (OSError, ValueError):
        pass
    rng = random.SystemRandom()
    data = {"device_id": str(rng.randint(10**18, 10**19 - 1)),
            "iid": str(rng.randint(10**18, 10**19 - 1)),
            "note": "random synthetic ids for 剪映's public material API; not 剪映's real ids"}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1), "utf-8")
    except OSError:
        pass
    return data["device_id"], data["iid"]


def offline():
    return os.environ.get(OFFLINE_ENV) == "1"


def _request(url, *, data=None, headers=None, what):
    req = urllib.request.Request(url, data=data, headers=dict(
        {"Accept": "application/json", "User-Agent": "jy-cli"}, **(headers or {})),
        method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise JyError("UNAVAILABLE", f"剪映素材接口 {what} 返回 HTTP {exc.code}",
                      retriable=exc.code >= 500, details={"http_status": exc.code}) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise JyError("UNAVAILABLE", f"连不上剪映素材接口（{what}）：{exc}", retriable=True,
                      details={"error": str(exc)[:200]}) from exc
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise JyError("UNAVAILABLE", f"剪映素材接口 {what} 返回的不是 JSON",
                      details={"body": raw[:200].decode("utf-8", "replace")}) from exc


def _loki(path, params, what):
    device_id, iid = device_ids()
    query = dict(LOKI_PARAMS, device_id=device_id, iid=iid, **params)
    base = os.environ.get(BASE_ENV) or LOKI_BASE
    payload = _request(f"{base}{path}?{urllib.parse.urlencode(query)}", what=what)
    if not isinstance(payload, dict) or payload.get("status_code") != 0:
        raise JyError("UNAVAILABLE", f"剪映素材接口拒绝了请求（{what}）："
                      f"{payload.get('message') if isinstance(payload, dict) else payload}",
                      details={"status_code": payload.get("status_code") if isinstance(payload, dict) else None})
    return payload.get("data") or {}


def _json_obj(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return {}


def _normalize_loki(effect, panel, category):
    file_url = effect.get("file_url") if isinstance(effect.get("file_url"), dict) else {}
    md5 = next((str(v).lower() for v in (file_url.get("uri"), file_url.get("md5"), effect.get("id"))
                if v and _MD5.match(str(v).lower())), "")
    urls = []
    for url in file_url.get("url_list") or []:
        if isinstance(url, str) and url:
            url = "https:" + url if url.startswith("//") else url
            if url not in urls:
                urls.append(url)
    extra = _json_obj(effect.get("extra"))
    kind = PANELS[panel]
    entry = {"name": str(effect.get("name") or ""), "kind": kind, "effect_type": effect.get("effect_type"),
             "category": category, "vip": bool(extra.get("is_vip") or effect.get("is_vip")),
             "md5": md5, "urls": urls, "publish_source": "loki", "cache_dir": "effect",
             "source": "live", "effect_id": str(effect.get("effect_id") or "")}
    if kind == "text_animation":
        entry["animation_type"] = ANIMATION_TYPES.get(category)
    return str(effect.get("resource_id") or ""), entry


def _categories(data):
    mapping = {}
    for cat in data.get("category") or []:
        for eid in cat.get("effects") or []:
            mapping.setdefault(str(eid), str(cat.get("name") or ""))
    return mapping


def panel(name, *, refresh=False):
    """``{resource_id: entry}`` for one Loki panel, cached for a day."""
    path = _cache_dir() / f"panel-{name}.json"
    if not refresh:
        try:
            saved = json.loads(path.read_text("utf-8"))
            if time.time() - saved.get("fetched_at", 0) < PANEL_TTL_S:
                return saved["items"]
        except (OSError, ValueError, KeyError):
            pass
    data = _loki("/effect/api/v3/effects", {"panel": name, "count": "1", "cursor": "0"},
                 f"panel={name}")
    cats = _categories(data)
    items = {}
    for effect in data.get("effects") or []:
        rid, entry = _normalize_loki(effect, name, cats.get(str(effect.get("effect_id")), ""))
        if rid.isdigit() and entry["name"]:
            items[rid] = entry
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"fetched_at": time.time(), "items": items}, ensure_ascii=False),
                        "utf-8")
    except OSError:
        pass
    return items


def search(keyword, kind=None, limit=20):
    """Live keyword search (per panel) merged with the day-cached panel listings."""
    kinds = [kind] if kind else list(KIND_PANEL)
    out = {}
    for k in kinds:
        name = KIND_PANEL.get(k)
        if not name:
            continue
        data = _loki("/effect/api/search/effects",
                     {"keyword": keyword, "panel": name, "count": str(max(1, min(limit or 50, 50))),
                      "cursor": "0"}, f"search {name}")
        for effect in data.get("effects") or []:
            rid, entry = _normalize_loki(effect, name, "")
            if rid.isdigit() and entry["name"]:
                out.setdefault(rid, entry)
        needle = keyword.lower()
        for rid, entry in panel(name).items():
            if needle in entry["name"].lower():
                out.setdefault(rid, entry)
            elif rid in out and not out[rid]["category"]:
                out[rid]["category"] = entry["category"]
                if entry.get("animation_type"):
                    out[rid]["animation_type"] = entry["animation_type"]
    return out


def lookup_id(resource_id):
    """One item by id: the Loki panels first, then Artist ``mget_artist_item``."""
    for name in PANELS:
        items = panel(name)
        if resource_id in items:
            return items[resource_id]
        for entry in items.values():
            if entry.get("effect_id") == resource_id:
                return entry
    return artist_mget([resource_id]).get(resource_id)


def artist_sign(payload, *, device_id, timestamp):
    canonical = (f"{ARTIST_SIGN_PREFIX}{payload}|{ARTIST_PARAMS['device_platform']}|"
                 f"{ARTIST_APP_VERSION}|{timestamp}|{device_id}{ARTIST_SIGN_SUFFIX}")
    return hashlib.md5(canonical.encode("utf-8")).hexdigest()


def artist_mget(ids):
    """``{id: entry}`` from Artist ``mget_artist_item`` (Artist packages -> artistEffect/)."""
    device_id, iid = device_ids()
    body = json.dumps({"app_id": int(ARTIST_PARAMS["app_id"]), "id_list": [str(i) for i in ids],
                       "pack_optional": True}, ensure_ascii=False, separators=(",", ":"))
    ts = str(int(time.time()))
    headers = {"Content-Type": "application/json", "device-time": ts, "loc": "cn",
               "lan": "zh-hans", "pf": ARTIST_PARAMS["device_platform"], "appv": ARTIST_APP_VERSION,
               "appid": ARTIST_PARAMS["app_id"], "tdid": device_id,
               "sign": artist_sign(body, device_id=device_id, timestamp=ts), "sign-ver": "1",
               "Business-Sign-Version": "v2", "App-Sdk-Version": "48.0.0"}
    base = os.environ.get(ARTIST_BASE_ENV) or os.environ.get(BASE_ENV) or ARTIST_BASE
    query = urllib.parse.urlencode(dict(ARTIST_PARAMS, device_id=device_id, iid=iid))
    payload = _request(f"{base}/artist/v1/effect/mget_artist_item?{query}", data=body.encode(),
                       headers=headers, what="artist mget")
    if not isinstance(payload, dict) or str(payload.get("ret")) != "0":
        raise JyError("UNAVAILABLE", f"剪映 Artist 接口拒绝了请求：{payload.get('errmsg') if isinstance(payload, dict) else payload}",
                      details={"ret": payload.get("ret") if isinstance(payload, dict) else None})
    out = {}
    for item in (payload.get("data") or {}).get("effect_item_list") or []:
        attr = item.get("common_attr") or {}
        rid = str(attr.get("id") or attr.get("effect_id") or "")
        business = _json_obj((attr.get("business_info") or {}).get("json_str"))
        if not rid.isdigit():
            continue
        out[rid] = {"name": str(attr.get("title") or ""), "kind": None,
                    "effect_type": attr.get("effect_type"), "category": "",
                    "vip": bool(business.get("is_vip")), "md5": str(attr.get("md5") or "").lower(),
                    "urls": [u for u in attr.get("item_urls") or [] if isinstance(u, str)],
                    "publish_source": "artist", "cache_dir": "artistEffect", "source": "live"}
    return out
