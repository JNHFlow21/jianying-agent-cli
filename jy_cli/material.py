"""剪映 draft material graph: styled text, and the per-kind material builders.

Why this module exists
----------------------
``draft.py`` used to write a *flat* text material (``font_size``/``text_color``/
``alignment`` as scalar fields).  剪映's renderer ignores those: it renders its own
default style — white, ~272 px tall, clipped to the canvas.  Proven on
2026-10-24 with a frame diff.

The real schema, recovered from a plaintext draft authored in the
剪映 GUI (``materials.texts[0]``, 125 keys):

* styling is **not** in the material's scalar fields — it is a JSON *string* in
  ``content`` (and ``base_content``)::

      {"styles": [{"fill":   {"alpha": 1.0, "content": {"render_type": "solid",
                            "solid": {"alpha": 1.0, "color": [r, g, b]}}},
                   "font":   {"id": "", "path": "<abs path to a .ttf>"},
                   "range":  [0, <len(text)>],
                   "size":   <font_size>,
                   "strokes":[{"alpha": 1.0, "content": {"render_type": "solid",
                            "solid": {"alpha": 1.0, "color": [r, g, b]}},
                            "width": <width>}],
                   "useLetterColor": true}],
       "text": "<the string>"}

* colour components are **0..1 floats**, not hex;
* ``range`` is ``[start, end)`` over characters;
* ``font_path`` must be an absolute path to a real font file (剪映 ships
  ``Contents/Resources/Font/SystemFont/zh-hans.ttf``);
* ``check_flag`` is 15 for a styled text (7 is the legacy/flat shape);
* the scalar ``font_size`` mirrors ``styles[0].size`` and must agree with it.

Calibration (measured, 1080x1920 canvas, frame diff vs a no-text render)
------------------------------------------------------------------------
===========  ========  ==================
style size   height    note
===========  ========  ==================
5.0          31 px     colour round-trip exact
10.0         67 px     colour round-trip exact
20.0         135 px    colour round-trip exact
===========  ========  ==================

So ``px ≈ 6.7 x size`` at canvas width 1080.  On a 1920-wide canvas the same
``size: 8.0`` measured 80 px (≈10 px/unit), so the ratio is **canvas dependent**
and not exactly proportional to width — treat :func:`size_to_px` as an
approximation and re-calibrate per canvas before promising exact typography.

Position is *not* a text-material field: it is the segment's
``clip.transform``, normalised to **half the canvas**, y axis up-positive::

    screen_x = canvas_w/2 + transform.x * canvas_w/2
    screen_y = canvas_h/2 - transform.y * canvas_h/2

Verified exactly: transform ``(-0.4, -0.4)`` on 1080x1920 put the glyph centre at
``(323.5, 1344.0)``; predicted ``(324.0, 1344.0)``.

``alignment`` is *intra-box* only (which edge multiple lines align to).  It does
**not** move the text box: single-line text with alignment 0, 1 and 2 all centred
at x≈540 on a 1080 canvas.  Use ``transform`` to place text.

Status per kind
---------------
``text``            — PROVEN (frame diff, see :data:`PROVEN_TO_RENDER`).
``transition``/``filter``/``video_effect``/``sticker``/``face_beauty``/
``matting``         — the builders emit the shapes 剪映's own drafts use where a
                      reference existed, and a best-effort shape otherwise; they
                      are tagged ``SCHEMA_UNVERIFIED`` and are NOT proven to
                      render.  Those resources are resolved by ``resource_id`` +
                      cached package, and this install has no local transition or
                      filter catalogue at all.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

from .envelope import JyError

DATA = Path(__file__).with_name("data")
TEXT_TEMPLATE = DATA / "text_material_template.json"
VIDEO_TEMPLATE = DATA / "video_material_template.json"

# 剪映 ships a CJK-covering system font; a bare family name is not enough because
# styles[].font.path must be an absolute file path.
APP_FONT_DIR = Path("/Applications/VideoFusion-macOS.app/Contents/Resources/Font/SystemFont")
DEFAULT_FONT = APP_FONT_DIR / "zh-hans.ttf"

# Measured on 2026-10-24 (see module docstring).  Kept as data so the CLI can
# report the assumption it used instead of silently hard-coding typography.
PX_PER_SIZE_UNIT_AT_1080 = 6.7
TEXT_RENDER_INDEX = 14000

ALIGNMENTS = {"left": 0, "center": 1, "right": 2}

# Kinds whose material shape has been reproduced from a 剪映-authored draft and
# proven to change pixels through `jy render`. `text` and `video_effect` both have
# frame/pixel-diff evidence.
PROVEN_TO_RENDER = (
    "text",        # JSON content.styles + check_flag 15, frame diff
    "video_effect",  # own `effect` track (not segment refs), pixel diff
    "transition",  # left clip's extra_material_refs, junction PSNR 27.5/31.2 dB
    "face_beauty",  # materials.effects[] type=figure + is_set_beauty_mode, face-region luma +8.86
    "sticker",     # own `sticker` track, render_index 14000, PSNR 11.98 dB
    "filter",      # materials.effects[] type=filter + own `filter` track; PSNR 20.34 dB
    "matting",     # videos[i].matting{flag:3,...}; bg→16.0, face unchanged, PSNR 8.95 dB
    "text_effect",  # 花字: effects[] type=text_effect + text content.styles[].effectStyle; PSNR 22.37 dB
)
# Kinds whose material shape has NOT been reproduced from a 剪映-authored draft.
# Kept deliberately small: `filter` (every visible card was an uncached cloud
# asset, so the + never applied and no structure was recoverable) and `matting`
# (never exercised). Everything else moved to PROVEN_TO_RENDER.
# Every 剪映 material kind in scope has now been reproduced from a GUI-authored
# draft and pixel-proven. Nothing is left in this list; keep it (empty) so the
# status payload keeps a stable shape.
SCHEMA_UNVERIFIED = ()


def _uid():
    return str(uuid.uuid4()).upper()


def hex_to_rgb(value, label="color"):
    """``#RRGGBB`` -> ``[r, g, b]`` floats in 0..1 (剪映's style colour form)."""
    text = str(value or "").strip()
    if not text.startswith("#"):
        text = "#" + text
    if len(text) != 7:
        raise JyError("VALIDATION", f"{label} 需要 #RRGGBB，收到 {value!r}")
    try:
        return [int(text[i:i + 2], 16) / 255.0 for i in (1, 3, 5)]
    except ValueError as exc:
        raise JyError("VALIDATION", f"{label} 不是合法十六进制：{value!r}") from exc


def rgb_to_hex(rgb):
    return "#" + "".join(f"{max(0, min(255, int(round(c * 255)))):02X}" for c in rgb[:3])


def size_to_px(size, canvas_width=1080):
    """Approximate rendered glyph height in px for a given style size."""
    return float(size) * PX_PER_SIZE_UNIT_AT_1080 * (float(canvas_width) / 1080.0)


def px_to_size(px, canvas_width=1080):
    """Inverse of :func:`size_to_px`; use to express a wanted px height as `size`."""
    factor = PX_PER_SIZE_UNIT_AT_1080 * (float(canvas_width) / 1080.0)
    return float(px) / factor


def _load_json(path, label):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - shipped data
        raise JyError("INTERNAL", f"jy 的{label}模板损坏：{path}", details={"error": str(exc)}) from exc


def text_style_payload(text, *, size, color, font_path=None, stroke_color=None,
                       stroke_width=0.08, bold_width=0.0, alignment=None):
    """Build the ``content`` JSON payload 剪映 actually renders."""
    font = str(font_path or DEFAULT_FONT)
    style = {
        "fill": {
            "alpha": 1.0,
            "content": {"render_type": "solid",
                        "solid": {"alpha": 1.0, "color": hex_to_rgb(color)}},
        },
        "font": {"id": "", "path": font},
        "range": [0, len(text)],
        "size": float(size),
        "useLetterColor": True,
    }
    if stroke_width:
        style["strokes"] = [{
            "alpha": 1.0,
            "content": {"render_type": "solid",
                        "solid": {"alpha": 1.0, "color": hex_to_rgb(stroke_color or "#000000")}},
            "width": float(stroke_width),
        }]
    payload = {"styles": [style], "text": text}
    return payload


def text_effect_material(resource_id, *, name="", path="", value=1.0, category_id="",
                         category_name="", source_platform=1, request_id=None):
    """``(id, material)`` for a 花字 — container verified: ``materials.effects[type="text_effect"]``.

    Recovered from a GUI-saved draft (文本 → 花字库 → 热门 → 黑色手绘质感花字)::

        id, effect_id, resource_id, third_resource_id:"0", name, type:"text_effect",
        sub_type:"none", path, value, category_id, category_name, source_platform,
        request_id, color_match_info:{}, multi_language_current:"",
        beauty_face_auto_retouch_info:{}

    A 花字 must be written **twice**: this effects entry (referenced from the text
    segment's ``extra_material_refs``) **and** ``content.styles[0].effectStyle =
    {id: <resource_id>, path: <cache path>}`` inside the text material — see
    ``text_material(text_effect=…)``. ``path`` is
    ``…/Cache/artistEffect/<rid>/<md5>`` and has to be downloaded first.
    """
    material_id = _uid()
    material = {
        "id": material_id,
        "effect_id": str(resource_id),
        "resource_id": str(resource_id),
        "third_resource_id": "0",
        "name": name,
        "type": "text_effect",
        "sub_type": "none",
        "path": path,
        "value": float(value),
        "category_id": str(category_id),
        "category_name": str(category_name),
        "source_platform": int(source_platform),
        "color_match_info": {},
        "multi_language_current": "",
        "beauty_face_auto_retouch_info": {},
    }
    if request_id:
        material["request_id"] = str(request_id)
    return material_id, material


def text_material(text, style, *, canvas_width=1080, font_path=None, text_effect=None):
    """Return ``(id, material)`` for a **styled** text, ready for ``materials.texts``.

    ``style`` is the normalised dict produced by ``draft._normalise_style``
    (keys: font_size, color, alignment, bold, italic, underline, letter_spacing,
    line_spacing, and optionally stroke_color / stroke_width / font_path).
    """
    if not isinstance(text, str) or not text:
        raise JyError("VALIDATION", "text_material 需要非空字符串")
    size = float(style.get("font_size", 5.0))
    color = style.get("color", "#FFFFFF")
    font = font_path or style.get("font_path") or str(DEFAULT_FONT)
    stroke_width = style.get("stroke_width", 0.08)

    material = copy.deepcopy(_load_json(TEXT_TEMPLATE, "文字材质"))
    material["id"] = _uid()
    payload = text_style_payload(
        text, size=size, color=color, font_path=font,
        stroke_color=style.get("stroke_color", "#000000"),
        stroke_width=stroke_width,
    )
    if text_effect:
        effect_style = {"id": str(text_effect["resource_id"]),
                        "path": str(text_effect.get("path") or "")}
        for style_block in payload.get("styles") or []:
            style_block["effectStyle"] = dict(effect_style)
    encoded = json.dumps(payload, ensure_ascii=False)
    material["content"] = encoded
    # base_content is the pre-edit copy 剪映 keeps for "reset"; without a stroke
    # variant 剪映 shows a stale outline after reset, so mirror the payload.
    material["base_content"] = encoded
    material["font_size"] = size
    material["font_path"] = font
    material["text_color"] = color if str(color).startswith("#") else "#" + str(color)
    material["alignment"] = int(style.get("alignment", 1))
    material["letter_spacing"] = float(style.get("letter_spacing", 0.0))
    material["line_spacing"] = float(style.get("line_spacing", 0.02))
    material["bold_width"] = float(style.get("bold_width", 0.0))
    material["italic_degree"] = int(style.get("italic_degree", 0))
    material["underline"] = bool(style.get("underline", False))
    material["check_flag"] = 15
    material["words"] = []
    material["shapes"] = []
    material["fonts"] = []
    material["combo_info"] = {"text_templates": []}
    material["text_size"] = 30
    return material["id"], material


def _flat_text_material(text, style):
    """The legacy flat shape — kept only so old drafts and A/B tests still build."""
    material_id = "text-" + uuid.uuid4().hex[:8]
    return material_id, {
        "id": material_id, "type": "text", "content": text,
        "font_size": float(style.get("font_size", 8.0)),
        "text_color": style.get("color", "#FFFFFF"), "text_alpha": 1.0,
        "alignment": int(style.get("alignment", 1)),
        "letter_spacing": float(style.get("letter_spacing", 0.0)),
        "line_spacing": float(style.get("line_spacing", 1.0)),
        "fixed_width": -1, "fixed_height": -1, "line_feed": 1, "check_flag": 7,
        "text_size": 30, "typesetting": 0,
        "bold": bool(style.get("bold", True)), "italic": bool(style.get("italic", False)),
        "underline": bool(style.get("underline", False)), "global_alpha": 1.0,
        "render_index": 0, "text_to_audio_ids": [], "background_color": "",
        "background_style": 0, "font_id": "", "font_path": "", "font_resource_id": "",
        "font_source_platform": 0, "force_apply_line_max_width": False, "language": "",
        "shapes": [], "words": [], "text_effect_ids": [], "text_animation_ids": [],
    }


# --------------------------------------------------------------------------- #
# video
# --------------------------------------------------------------------------- #

def video_material(probe, *, name=None):
    """``(id, material)`` for ``materials.videos`` using 剪映's own key set."""
    try:
        template = _load_json(VIDEO_TEMPLATE, "视频材质")
    except JyError:
        template = {"type": "video", "crop_ratio": "free", "crop_scale": 1.0,
                    "check_flag": 62978047, "source": 0, "source_platform": 0,
                    "video_algorithm": {"algorithms": []}}
    material = copy.deepcopy(template)
    material["id"] = _uid()
    width = int(probe.get("width") or 0)
    height = int(probe.get("height") or 0)
    duration_us = int(round(float(probe.get("duration_s") or 0.0) * 1_000_000))
    material.update({
        "type": "video",
        "duration": duration_us,
        "path": probe["path"],
        "material_name": name or Path(probe["path"]).name,
        "width": width,
        "height": height,
        "has_audio": bool(probe.get("audio_streams")),
        # real drafts mark a locally imported file this way; an empty value makes the
        # engine treat it as an unresolved cloud asset
        "category_name": "local",
    })
    material["crop"] = {
        "lower_left_x": 0.0, "lower_left_y": 1.0,
        "lower_right_x": 1.0, "lower_right_y": 1.0,
        "upper_left_x": 0.0, "upper_left_y": 0.0,
        "upper_right_x": 1.0, "upper_right_y": 0.0,
    }
    material["crop_source_size"] = {"width": width, "height": height}
    material["crop_img_size"] = {"width": width, "height": height}
    material["crop_timerange"] = {"start": 0, "duration": duration_us}
    material.setdefault("matting", {})
    material["matting"].update({
        "flag": 0, "has_use_quick_brush": False, "has_use_quick_eraser": False,
        "interactiveTime": [], "path": "", "strokes": [],
    })
    material.setdefault("stable", {"matrix_path": "", "stable_level": 0,
                                   "time_range": {"start": 0, "duration": 0}})
    return material["id"], material


# --------------------------------------------------------------------------- #
# effect-family materials — shapes are NOT proven to render (see module docstring)
# --------------------------------------------------------------------------- #

def transition_material(resource_id, *, name="", duration_us=500_000, path="",
                        category_id="", category_name="热门", source_platform=1,
                        request_id=None):
    """``(id, material)`` for ``materials.transitions`` — shape copied from 剪映.

    Recovered from a GUI-authored draft (转场 → 热门 → 叠化)::

        id, type="transition", name, effect_id, resource_id, third_resource_id,
        source_platform=1, path, duration(µs), is_overlap=true,
        category_id, category_name, request_id

    Container (verified): the transition id goes into the **left clip's**
    ``extra_material_refs`` — i.e. the segment *before* the junction. 剪映 does not
    put it on a track of its own and does not use ``overlap_duration`` (that key was
    a guess and is gone). ``duration`` is what the app actually stored — the GUI
    showed 0.5 s but wrote 466666 µs because it snapped to whole frames.
    """
    material_id = _uid()
    material = {
        "id": material_id,
        "type": "transition",
        "name": name,
        "effect_id": str(resource_id),
        "resource_id": str(resource_id),
        "third_resource_id": str(resource_id),
        "source_platform": int(source_platform),
        "path": path,
        "duration": int(duration_us),
        "is_overlap": True,
        "category_id": str(category_id),
        "category_name": str(category_name),
    }
    if request_id:
        material["request_id"] = str(request_id)
    return material_id, material


def filter_effect_material(resource_id, *, name="", path="", value=1.0,
                           category_id="", category_name="", source_platform=1,
                           request_id=None, third_resource_id="0"):
    """``(id, material)`` for a 滤镜 — container verified: ``materials.effects[type="filter"]``.

    **Not** ``materials.filters``: that array stays empty in 剪映 11.  The material is
    an entry in ``materials.effects`` with ``type:"filter", sub_type:"none"`` and an
    intensity in ``value`` (0..1), and it renders only from a dedicated **filter
    track** (``draft.filter_track``, segment ``render_index`` 10000). Recovered from
    a GUI-authored draft (秋日 → 暗灰电影感)::

        id, effect_id, resource_id, third_resource_id:"0", name, type:"filter",
        sub_type:"none", path, value, category_id, category_name, source_platform,
        request_id, color_match_info:{}, multi_language_current:"",
        beauty_face_auto_retouch_info:{}

    ``path`` points into ``…/Cache/**artistEffect**/<rid>/<md5>`` (note artistEffect,
    not effect) — the package must already be downloaded, which is also why the GUI
    card's ``+`` only works for cached filters.
    """
    material_id = _uid()
    material = {
        "id": material_id,
        "effect_id": str(resource_id),
        "resource_id": str(resource_id),
        "third_resource_id": str(third_resource_id),
        "name": name,
        "type": "filter",
        "sub_type": "none",
        "path": path,
        "value": float(value),
        "category_id": str(category_id),
        "category_name": str(category_name),
        "source_platform": int(source_platform),
        "color_match_info": {},
        "multi_language_current": "",
        "beauty_face_auto_retouch_info": {},
    }
    if request_id:
        material["request_id"] = str(request_id)
    return material_id, material


def filter_track_segment(material_id, duration_us, *, start_us=0, render_index=10000):
    """The segment shape 剪映 writes on a ``filter`` track (verbatim from the GUI)."""
    window = {"duration": int(duration_us)}
    if start_us:
        window["start"] = int(start_us)
    return {
        "id": _uid(),
        "target_timerange": window,
        "render_timerange": {},
        "material_id": material_id,
        "render_index": int(render_index),
        "enable_lut": False,
        "enable_adjust": False,
        "enable_hsl": False,
        "responsive_layout": {},
        "enable_adjust_mask": False,
        "source": "segmentsourcenormal",
    }


def keyframe_channel(property_type, points, *, material_id=""):
    """One ``common_keyframes[]`` channel (verified container: the **segment**, not the material).

    ``property_type`` is 剪映's per-property channel id (``KFTypeScaleX``,
    ``KFTypeScaleY``, ``KFTypePositionX``, ``KFTypePositionY``, ``KFTypeAlpha``,
    ``KFTypeVolume`` …). ``points`` is a list of ``(time_offset_us, [values])`` or
    ``(time_offset_us, [values], left_control, right_control)``.

    Easing: each keyframe carries its own Bezier handles
    ``left_control``/``right_control`` = ``{x, y}``; 剪映 writes ``{0, 0}`` for its
    default (auto) easing and non-zero handles for a custom curve. Recovered
    from a GUI-authored draft.
    ``keyframe_graph_list`` exists in the schema but was **null** on this build, so it
    is deliberately not written.
    """
    channel = {"id": _uid(), "material_id": str(material_id or ""),
               "property_type": str(property_type), "keyframe_list": []}
    for point in points:
        time_offset, values = point[0], point[1]
        left = point[2] if len(point) > 2 and point[2] else {"x": 0.0, "y": 0.0}
        right = point[3] if len(point) > 3 and point[3] else {"x": 0.0, "y": 0.0}
        channel["keyframe_list"].append({
            "id": _uid(),
            "time_offset": int(time_offset),
            "left_control": {"x": float(left.get("x", 0.0)), "y": float(left.get("y", 0.0))},
            "right_control": {"x": float(right.get("x", 0.0)), "y": float(right.get("y", 0.0))},
            "values": [float(v) for v in values],
        })
    return channel


def curve_speed_material(points, *, name="", preset_id="", speed=None):
    """``(id, material)`` for ``materials.speeds[i]`` in 曲线变速 mode.

    Verified shape: ``{id, type:"speed", mode:1, speed:<avg>,
    curve_speed:{id:<preset id>, name:<中文名>, speed_points:[{x,y}…]}}`` — ``x`` is the
    normalised position along the clip (0..1) and ``y`` the speed multiplier.
    Constant-speed segments are just ``{id, type:"speed"}``.
    """
    material_id = _uid()
    pts = [{"x": round(float(x), 4), "y": round(float(y), 4)} for x, y in points]
    avg = float(speed) if speed is not None else round(
        sum(p["y"] for p in pts) / max(len(pts), 1), 4)
    return material_id, {
        "id": material_id,
        "type": "speed",
        "mode": 1,
        "speed": avg,
        "curve_speed": {"id": str(preset_id or _uid()), "name": str(name), "speed_points": pts},
    }


def mask_material(resource_type, *, name="", resource_id="", path="", config=None,
                  category="video", category_name="基础", category_id="jichu",
                  resource_type_name=None):
    """``(id, mask, constant_id)`` for ``materials.common_mask[]`` (verified).

    Verified shape (线性 mask)::

        {id, type:"mask", category:"video", category_name:"基础", category_id:"jichu",
         resource_id, constant_material_id, name:"线性", resource_type:"line",
         path:<Cache/effect/<rid>/<md5>>, config:{width:0.5}, text_config:{align_type:15}}

    ``constant_material_id`` is a companion material 剪映 also references from the
    segment, so both ids go into ``extra_material_refs``. ``config`` is the shape
    parameter block (linear uses ``{"width": 0.5}``); pass what 剪映 wrote for the
    shape you use.
    """
    mask_id, constant_id = _uid(), _uid()
    mask = {
        "id": mask_id,
        "type": "mask",
        "category": str(category),
        "category_name": str(category_name),
        "category_id": str(category_id),
        "resource_id": str(resource_id),
        "constant_material_id": constant_id,
        "name": str(name or resource_type_name or resource_type),
        "resource_type": str(resource_type),
        "path": str(path),
        "config": dict(config or {}),
        "text_config": {"align_type": 15},
    }
    return mask_id, mask, constant_id


def color_wheel_material(segment, *, red=0.0, green=0.0, blue=0.0,
                         resource_id="", path="", hsl=True, name=""):
    """``(wheel_id, wheel, hsl_id, hsl, companion_id)`` for 剪映 色轮 (调色).

    Shape recovered from a GUI-authored draft (agent-bridge): the wheel lives in
    ``materials.primary_color_wheels[]`` with a per-segment selector
    (``segment`` = gamma|lift|gain|offset) and three channel offsets. When ``hsl``
    is on, a companion goes to ``materials.hsl[]`` and both ids are attached to the
    segment (mirrors 剪映's ``constant_material_id`` pairing).

    The HSL companion's exact field set is NOT pixel-verified yet; the wheel itself
    follows the recovered keys. Callers should surface that distinction when they
    report success.
    """
    wheel_id, companion_id = _uid(), _uid()
    wheel = {
        "id": wheel_id,
        "type": "primary_color_wheel",
        "segment": str(segment),
        "red": float(red),
        "green": float(green),
        "blue": float(blue),
        "resource_id": str(resource_id),
        "path": str(path),
        "name": str(name or segment),
        "enable_hsl": bool(hsl),
        "constant_material_id": companion_id if hsl else "",
    }
    companion = None
    if hsl:
        companion = {
            "id": companion_id,
            "type": "hsl",
            "segment": str(segment),
            "red": float(red),
            "green": float(green),
            "blue": float(blue),
            "resource_id": str(resource_id),
            "path": str(path),
            "constant_material_id": wheel_id,
            "enable_hsl": True,
        }
    return wheel_id, wheel, companion_id if hsl else "", companion, companion_id if hsl else ""


def text_animation_entry(resource_id, kind, *, name="", duration_us=500_000, path="",
                         category_id="", category_name="", material_type="sticker",
                         request_id=None, source_platform=1):
    """One ``animations[]`` entry for a text (or sticker) animation — verified shape.

    Recovered from a GUI-saved draft (text → 动画 → 入场 → 渐显入场)::

        {id:"<resource_id>", type:"in"|"out"|"loop", duration:<µs>,
         path:"…/Cache/effect/<rid>/<md5>", resource_id, third_resource_id:"0",
         source_platform:1, name:"渐显入场", category_id:"ruchang",
         category_name:"入场", material_type:"sticker", request_id}

    The entry lives in ``materials.material_animations[i].animations[]`` and that
    material's id is referenced by the **text segment**'s ``extra_material_refs``.
    剪映 writes ``material_type:"sticker"`` even for text animations.
    """
    entry = {
        "id": str(resource_id),
        "type": str(kind),
        "duration": int(duration_us),
        "path": path,
        "resource_id": str(resource_id),
        "third_resource_id": "0",
        "source_platform": int(source_platform),
        "name": name,
        "category_id": str(category_id),
        "category_name": str(category_name),
        "material_type": str(material_type),
    }
    if request_id:
        entry["request_id"] = str(request_id)
    return entry


def text_template_material(resource_id, *, name="", path="", resources=None,
                           version="1.0.0", category_id="", category_name="",
                           request_id=None, effect_id=None):
    """``(id, material)`` for ``materials.text_templates[]`` — 文字模板 (verified NEW bucket).

    Recovered from a GUI-saved draft (文本 → 文字模板 → 热门 → 划重点)::

        {id, version:"1.0.0", effect_id, resource_id, name:"互动引导-划重点",
         type:"text_template", path:"…/Cache/artistEffect/<rid>/<md5>",
         category_id:"10577", category_name:"热门", source_platform:1,
         resources:[{panel:"default"|"sticker"|"fonts"|"text",
                     path:"…", resource_id, source_platform}, …],
         text_info_resources, non_text_info_resources, aigc_config,
         request_id, origin_word_info, current_word_info, preview_time,
         ai_generate_task_info}

    This is **not** 花字 (``text_effect`` + ``content.styles[].effectStyle``). 剪映 uses
    the returned id as the **text track segment's ``material_id``**, and the segment's
    ``extra_material_refs`` then carries the text material, one ``text_effect`` and the
    template's own ``material_animations`` (in ≈500 ms, loop ≈600 ms).
    ``resources`` must be copied from the template's package (panel types above); the
    packages live under ``Cache/artistEffect`` and ``Cache/effect`` (the download-side
    copy sits in ``Cache/AITextTemplate/Resource/<rid>``).
    """
    material_id = _uid()
    return material_id, {
        "id": material_id,
        "version": str(version),
        "effect_id": str(effect_id or resource_id),
        "resource_id": str(resource_id),
        "name": str(name),
        "type": "text_template",
        "path": str(path),
        "category_id": str(category_id),
        "category_name": str(category_name),
        "source_platform": 1,
        "resources": list(resources or []),
        "text_info_resources": [],
        "non_text_info_resources": [],
        "aigc_config": {},
        "origin_word_info": {},
        "current_word_info": {},
        "preview_time": 0,
        "ai_generate_task_info": {},
        **({"request_id": str(request_id)} if request_id else {}),
    }


def sticker_material(resource_id, *, name="", path="", sticker_id=None,
                     category_id="none", category_name="none", source_platform=6):
    """``(id, material)`` for ``materials.stickers`` — shape copied from 剪映.

    Recovered from a GUI-saved draft (贴纸 → 数据图表 → 添加到轨道)::

        id, type="sticker", path, sticker_id, resource_id, category_id="none",
        category_name="none", source_platform=6, combo_info:{}, radius:{},
        shadow_point:{x,y}, shape_param:{}, shape_fill_render_style:{…}

    Container (verified): a **sticker track** (``draft.sticker_track``) whose segment
    carries ``clip.scale`` / ``clip.transform`` and ``render_index`` 14000.
    ``resource_id`` here is a UUID, not a numeric id, and ``path`` points into
    剪映's plugin download cache (``…/Download/com.lemon.openplugins.tmp/<plugin>/
    PluginSticker_<uuid>``) — that package must already be downloaded.
    """
    material_id = _uid()
    return material_id, {
        "id": material_id,
        "type": "sticker",
        "path": path,
        "sticker_id": str(sticker_id or resource_id),
        "resource_id": str(resource_id),
        "category_id": str(category_id),
        "category_name": str(category_name),
        "source_platform": int(source_platform),
        "combo_info": {},
        "radius": {},
        "shadow_point": {"x": 0.0, "y": 0.0},
        "shape_param": {},
        "shape_fill_render_style": {
            "color": {"solid": {}, "gradient": {},
                      "texture": {"path": "", "resource_id": ""}}
        },
    }


def sticker_track_segment(material_id, start_us, duration_us, *, scale=0.26,
                          transform=(0.0, 0.0), render_index=14000):
    """The segment shape 剪映 writes on a ``sticker`` track (verbatim from the GUI)."""
    return {
        "id": _uid(),
        "target_timerange": {"start": int(start_us), "duration": int(duration_us)},
        "render_timerange": {},
        "clip": {
            "scale": {"x": float(scale), "y": float(scale)},
            "transform": {"x": float(transform[0]), "y": float(transform[1])},
            "flip": {},
        },
        "uniform_scale": {},
        "material_id": material_id,
        "render_index": int(render_index),
        "enable_lut": False,
        "enable_adjust": False,
        "enable_hsl": False,
        "responsive_layout": {},
        "enable_adjust_mask": False,
        "source": "segmentsourcenormal",
    }


def video_effect_material(resource_id, *, name="", apply_target_type=2, value=1.0,
                          adjust_params=None, path="", category_id="", category_name="热门",
                          source_platform=1, request_id=None):
    """``(id, material)`` for ``materials.video_effects``.

    The key set, order and defaults are copied **verbatim from 剪映 itself**: this
    is the shape the app writes when you pick an effect in the GUI, recovered from a
    GUI-authored draft::

        id, effect_id, resource_id, name, type="video_effect", path, value,
        category_id, category_name, apply_target_type, source_platform,
        adjust_params=[{name, value, default_value}], request_id

    Two details are load-bearing and were wrong before:

    * the effect belongs on its **own ``effect`` track** (``draft.effect_track``),
      not in a video segment's ``extra_material_refs`` — a clip-bound effect
      composites as a black layer;
    * ``path`` is the cached package directory
      ``…/User Data/Cache/effect/<resource_id>/<package_md5>`` (no trailing slash),
      ``apply_target_type`` is 2 (not 0) and ``source_platform`` is 1 (not 0).

    ``adjust_params`` accepts the 剪映 shape (``name``/``value``/``default_value``)
    or jy's older ``key``/``value``; only sliders the resource declares are legal.
    """
    material_id = _uid()
    params = []
    for entry in adjust_params or []:
        if not isinstance(entry, dict):
            continue
        key = entry.get("name") or entry.get("key") or ""
        if not key:
            continue
        raw_value = entry.get("value", 1.0)
        default = entry.get("default_value", raw_value)
        params.append({"name": str(key), "value": float(raw_value),
                       "default_value": float(default)})
    material = {
        "id": material_id,
        "effect_id": str(resource_id),
        "resource_id": str(resource_id),
        "name": name,
        "type": "video_effect",
        "path": path,
        "value": float(value),
        "category_id": str(category_id),
        "category_name": str(category_name),
        "apply_target_type": int(apply_target_type),
        "source_platform": int(source_platform),
        "adjust_params": params,
    }
    # 剪映 writes a request_id for cloud-resolved effects; only pass through one we
    # actually know — an invented value is worse than an absent key.
    if request_id:
        material["request_id"] = str(request_id)
    return material_id, material


def effect_track_segment(material_id, duration_us, *, start_us=0, render_index=11000):
    """The segment shape 剪映 uses on an ``effect`` track (verbatim from the GUI draft).

    The GUI writes ``target_timerange`` with **only** ``duration`` (no ``start``) and
    no ``source_timerange``/``speed``/``volume``; ``render_index`` is 11000.
    """
    window = {"duration": int(duration_us)}
    if start_us:
        window["start"] = int(start_us)
    return {
        "id": _uid(),
        "target_timerange": window,
        "render_timerange": {},
        "material_id": material_id,
        "render_index": int(render_index),
        "enable_lut": False,
        "enable_adjust": False,
        "enable_hsl": False,
        "responsive_layout": {},
        "enable_adjust_mask": False,
        "source": "segmentsourcenormal",
    }


def filter_material(resource_id, *, name="", intensity=100.0, path=""):
    """``(id, material)`` for a filter (SCHEMA_UNVERIFIED).

    剪映's filter uses a 0..100 panel intensity; unlike an effect it *does* have a
    single overall strength.
    """
    material_id = _uid()
    return material_id, {
        "id": material_id,
        "type": "filter",
        "name": name,
        "effect_id": str(resource_id),
        "resource_id": str(resource_id),
        "value": float(intensity) / 100.0,
        "intensity_value": float(intensity),
        "path": path,
        "platform": "all",
        "source_platform": 0,
        "category_id": "",
        "category_name": "",
    }


def face_beauty_materials(params, *, category="skin", face_mode="multi"):
    """Build ``(ids, materials)`` for 美颜 sliders (SCHEMA_UNVERIFIED).

    ``params`` maps a ``face_adjust_*`` key to a 0..100 value; keys and ranges come
    from 剪映's local effect metadata.  ``category`` is one of
    skin / shape / feature / makeup.
    """
    if category not in ("skin", "shape", "feature", "makeup"):
        raise JyError("VALIDATION", f"face_beauty category 只能是 skin/shape/feature/makeup，收到 {category!r}")
    ids, materials = [], []
    for key, value in (params or {}).items():
        if not str(key).startswith(("face_adjust_", "eyebrow_adjust_")):
            raise JyError("VALIDATION", f"不是已核实的face_adjust_*/eyebrow_adjust_* 参数：{key!r}")
        number = float(value)
        if not 0.0 <= number <= 100.0:
            raise JyError("VALIDATION", f"{key} 必须在 0..100，收到 {number:g}")
        material_id = _uid()
        ids.append(material_id)
        materials.append({
            "id": material_id,
            "type": "manual_beauty",
            "name": key,
            "key": key,
            "value": number,
            "category": category,
            "face_mode": face_mode,
            "resource_id": "",
            "platform": "all",
        })
    return ids, materials


def _beauty_presets():
    """The recovered 皮肤管理 slider table (``jy_cli/data/beauty_presets.json``)."""
    import json as _json
    import pathlib as _path
    path = _path.Path(__file__).with_name("data") / "beauty_presets.json"
    try:
        return _json.loads(path.read_text("utf-8")).get("skin_management") or {}
    except (OSError, ValueError):
        return {}


def beauty_effect_materials(params, *, cache_root=None):
    """Build ``(ids, materials)`` for 美颜 sliders — container verified: ``materials.effects``.

    剪映 does **not** use ``materials.manual_beautys`` for 皮肤管理. It writes one
    ``materials.effects[]`` entry per slider with ``type:"figure"`` and
    ``item_effect_type:21``, ``value`` normalised to 0..1, ``category_id:
    "skin_management"``, plus an ``algorithm_artifact_path`` pointing at
    ``##_draftpath_placeholder_<draftId>_##/video/figure_algorithm/<uuid>`` and a
    companion ``makeup-root`` entry.  The owning video material also gets
    ``is_set_beauty_mode: true``, and the segment carries the effect ids in
    ``extra_material_refs``.  Recovered from
    a GUI-authored draft.

    ``params`` maps a preset name (磨皮 / 美白, see ``data/beauty_presets.json``) to
    a 0..100 value; it also accepts the legacy ``face_adjust_*`` keys, which are
    translated to the preset that owns them when possible.
    """
    presets = _beauty_presets()
    if not presets:
        raise JyError("UNAVAILABLE", "缺少 jy_cli/data/beauty_presets.json，无法生成美颜素材")
    import os as _os
    root = _os.path.expanduser(cache_root or "~/Movies/JianyingPro/User Data/Cache/effect").rstrip("/")
    ids, materials = [], []
    for name, raw in (params or {}).items():
        preset = presets.get(str(name))
        if preset is None:
            raise JyError(
                "VALIDATION",
                f"未知美颜参数 {name!r}；本机已核实的是 {', '.join(sorted(presets))}"
                "（data/beauty_presets.json，由 GUI 保存的草稿反向恢复）")
        number = float(raw)
        if not 0.0 <= number <= 100.0:
            raise JyError("VALIDATION", f"{name} 必须在 0..100，收到 {number:g}")
        material_id = _uid()
        ids.append(material_id)
        materials.append({
            "id": material_id,
            "resource_id": str(preset["resource_id"]),
            "third_resource_id": str(preset["third_resource_id"]),
            "name": str(name),
            "type": preset.get("type") or "figure",
            "sub_type": preset.get("sub_type") or "none",
            "path": f"{root}/{preset['path_suffix']}",
            "value": round(number / 100.0, 4),
            "item_effect_type": int(preset.get("item_effect_type") or 21),
            "category_id": preset.get("category_id") or "skin_management",
            "source_platform": int(preset.get("source_platform") or 1),
            "color_match_info": {},
            "multi_language_current": "",
            "beauty_face_auto_retouch_info": {},
        })
    return ids, materials


def attach(segment, material_ids):
    """Add material ids to a segment's ``extra_material_refs`` (order preserved)."""
    refs = segment.setdefault("extra_material_refs", [])
    for material_id in material_ids or ():
        if material_id and material_id not in refs:
            refs.append(material_id)
    return segment


# --------------------------------------------------------------------------- #
# spec plumbing used by draft.py
# --------------------------------------------------------------------------- #

KIND_BUILDERS = {
    "text": "text_material",
    "transition": "transition_material",
    "filter": "filter_material",
    "video_effect": "video_effect_material",
    "sticker": "sticker_material",
}


def describe():
    """Machine-readable status of every kind, for `jy material status`."""
    return {
        "proven": list(PROVEN_TO_RENDER),
        "unverified": list(SCHEMA_UNVERIFIED),
        "font": str(DEFAULT_FONT),
        "font_exists": DEFAULT_FONT.exists(),
        "px_per_size_unit_at_1080": PX_PER_SIZE_UNIT_AT_1080,
        "position": "segment.clip.transform, normalised to half-canvas, y up-positive",
        "alignment": "intra-box only; does not move the text box",
        "templates": [str(TEXT_TEMPLATE), str(VIDEO_TEMPLATE)],
    }
