"""Local approximation of a 剪映 draft spec (the ``--on-entitlement local`` path).

When 剪映 refuses an item because the account's membership tier is too low, jy can
re-render the *same timeline* with ffmpeg: clips (trim/scale/join), optional
per-clip filters and transitions, and ASS text. This is explicitly a downgrade —
every replaced element is listed in ``degraded_items`` with what fidelity was lost.

Not discovered as a verb (leading underscore); ``jianying_commands.render`` calls
:func:`render_spec` only when the operator asked for it.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .. import effects, ffmpeg
from ..envelope import JyError
from ._common import _even, _probe, _require_video, _video_encode_args


def _clip_plan(tool, spec, spec_dir):
    clips = []
    for index, raw in enumerate(spec.get("clips") or []):
        path = Path(raw.get("path") or "").expanduser()
        if not path.is_absolute():
            path = (spec_dir / path).resolve()
        if not path.is_file():
            raise JyError("NOT_FOUND", f"clips[{index}] 的源文件不存在：{path}")
        probe = _probe(tool, str(path))
        _require_video(probe)
        source_start = float(raw.get("source_start") or 0.0)
        duration = raw.get("duration")
        duration = float(duration) if duration is not None else max(
            0.0, (probe["duration_s"] or 0.0) - source_start
        )
        if duration <= 0:
            raise JyError(
                "VALIDATION",
                f"clips[{index}] 没有可用时长（source_start={source_start:g}s）",
                details={"path": str(path)},
            )
        clips.append(
            {
                "path": str(path),
                "source_start": source_start,
                "duration": round(duration, 3),
                "probe": probe,
                "filter": raw.get("filter") if isinstance(raw.get("filter"), dict) else None,
                "transition": raw.get("transition"),
                "unrenderable": {
                    key: raw.get(key)
                    for key in ("filters", "video_effects", "face_beauty")
                    if raw.get(key)
                },
            }
        )
    return clips


def _canvas(spec, clips):
    canvas = spec.get("canvas") if isinstance(spec.get("canvas"), dict) else {}
    width = int(canvas.get("width") or (clips[0]["probe"]["width"] if clips else 1280))
    height = int(canvas.get("height") or (clips[0]["probe"]["height"] if clips else 720))
    fps = canvas.get("fps") or (clips[0]["probe"]["fps"]["r_frame_rate"] if clips else "30")
    if not fps or fps == "0/0":
        fps = "30"
    return _even(width), _even(height), fps


def _grade_chain(clip, index):
    """Per-clip filter preset → ffmpeg chain, blended by intensity when < 100."""
    spec = clip.get("filter") or {}
    preset = spec.get("preset")
    if preset not in effects.FILTER_PRESETS:
        return None
    intensity = float(spec.get("intensity", 100.0))
    grade = effects.FILTER_PRESETS[preset]
    if intensity >= 100:
        return grade
    opacity = 1.0 - max(0.0, min(100.0, intensity)) / 100.0
    return (
        f"split=2[src{index}][graded{index}];[graded{index}]{grade}[look{index}];"
        f"[src{index}][look{index}]blend=all_mode=normal:all_opacity={opacity:g}"
    )


def _text_events(spec, width, height):
    """ASS events for every text, styled inline (no 剪映 text template)."""
    events = []
    for raw in spec.get("texts") or []:
        content = str(raw.get("content") or "").strip()
        if not content:
            continue
        start = float(raw.get("start") or 0.0)
        duration = float(raw.get("duration") or 3.0)
        style = raw.get("style") if isinstance(raw.get("style"), dict) else {}
        size = int(style.get("font_size") or 64)
        colour = effects.parse_color(style.get("color") or "white", label="texts.style.color")
        alignment = str(style.get("alignment") or "center").lower()
        margin = max(24, size // 2)
        if alignment == "left":
            x, anchor = margin, 4
        elif alignment == "right":
            x, anchor = width - margin, 6
        else:
            x, anchor = width // 2, 5
        y = height // 2
        tags = [f"\\an{anchor}", f"\\pos({x},{y})", f"\\fs{size}", f"\\c{colour}&", "\\bord2"]
        if style.get("bold"):
            tags.append("\\b1")
        if style.get("italic"):
            tags.append("\\i1")
        if style.get("underline"):
            tags.append("\\u1")
        prefix = "{" + "".join(tags) + "}"
        events.append((start, start + duration, prefix + effects.ass_escape(content)))
    return events


def render_spec(tool, spec, output, *, spec_dir=None, verbose=False, warnings=None):
    """Render ``spec`` locally to ``output``; returns data with degraded_items."""
    warnings = warnings if warnings is not None else []
    spec_dir = Path(spec_dir or ".").resolve()
    clips = _clip_plan(tool, spec, spec_dir)
    if not clips and not (spec.get("texts") or []):
        raise JyError("VALIDATION", "spec 既没有 clips 也没有 texts，无法本地近似渲染")
    width, height, fps = _canvas(spec, clips)

    degraded = []

    def degrade(kind, item, replaced_with, fidelity_lost):
        degraded.append(
            {
                "kind": kind,
                "item": item,
                "replaced_with": replaced_with,
                "fidelity_lost": fidelity_lost,
            }
        )

    known_keys = {"canvas", "clips", "texts", "duration"}
    for key in spec:
        if key not in known_keys:
            degrade(
                str(key),
                f"spec.{key}",
                "dropped",
                "jy 的本地近似只渲染片段/滤镜/转场/文字；"
                f"spec 里的 `{key}` 段没有本地等价物，已丢弃",
            )

    if clips and clips[0].get("transition"):
        degrade(
            "transition",
            "clips[0]",
            "dropped",
            "第一个片段之前没有片段可转场；本地近似不加入场转场（剪映里这是从黑场进入）",
        )

    has_audio = [bool(clip["probe"]["audio_streams"]) for clip in clips]
    with_audio = bool(clips) and all(has_audio)
    if clips and any(has_audio) and not with_audio:
        degrade(
            "audio",
            "部分片段没有音轨",
            "视频-only",
            "本地近似要求所有片段都有音轨才会混音；这段配音/音乐被丢弃",
        )

    parts = []
    labels = []
    for index, clip in enumerate(clips):
        chain = effects.normalize_chain(width, height, fps, reset_pts=True)
        grade = _grade_chain(clip, index)
        if grade:
            preset = clip["filter"].get("preset")
            intensity = float(clip["filter"].get("intensity", 100.0))
            chain = f"{grade},{chain}"
            degrade(
                "filter",
                f"clips[{index}] {Path(clip['path']).name}",
                f"ffmpeg 预设 {preset} @ {intensity:g}%",
                "剪映滤镜是 VESDK/LUT 素材包；ffmpeg 预设只是色彩近似，"
                "没有原滤镜的局部权重/肤色保护",
            )
        for key, value in (clip.get("unrenderable") or {}).items():
            degrade(
                key,
                f"clips[{index}] {Path(clip['path']).name}",
                "dropped (no local equivalent)",
                "剪映的 "
                + key
                + " 是 VESDK/素材包能力，ffmpeg 没有等价实现；本地近似里这一项不存在",
            )
        parts.append(f"[{index}:v]{chain}[v{index}]")
        labels.append(f"v{index}")
        if with_audio:
            parts.append(f"[{index}:a]{effects.audio_chain(reset_pts=True)}[a{index}]")

    transition_count = 0
    if len(clips) == 1:
        parts.append("[v0]null[v]")
        if with_audio:
            parts.append("[a0]anull[a]")
    elif any(isinstance(clip.get("transition"), (dict, str)) for clip in clips[1:]):
        current_v = "v0"
        current_a = "a0" if with_audio else None
        for index in range(1, len(clips)):
            raw = clips[index].get("transition")
            kind = "fade"
            duration = 0.5
            if isinstance(raw, dict):
                kind = str(raw.get("type") or "fade")
                duration = float(raw.get("duration") or 0.5)
            elif isinstance(raw, str):
                kind = raw
            segment = clips[index]["duration"]
            if duration >= segment:
                duration = max(0.1, segment / 2)
                warnings.append(
                    f"clips[{index}] 的转场比片段长；本地近似把转场缩到 {duration:g}s"
                )
            offset = sum(clip["duration"] for clip in clips[:index]) - index * duration
            target_v = "v" if index == len(clips) - 1 else f"xv{index}"
            parts.append(
                f"[{current_v}][v{index}]xfade=transition={kind}"
                f":duration={duration:g}:offset={max(0.0, offset):g}[{target_v}]"
            )
            current_v = target_v
            if with_audio:
                target_a = "a" if index == len(clips) - 1 else f"xa{index}"
                parts.append(
                    f"[{current_a}][a{index}]acrossfade=d={duration:g}:c1=tri:c2=tri[{target_a}]"
                )
                current_a = target_a
            transition_count += 1
            degrade(
                "transition",
                f"clips[{index - 1}]→clips[{index}]",
                f"xfade {kind} {duration:g}s",
                "剪映转场库是 GLSL/shader 包；xfade 只有基础像素转场，没有原转场的运动/光效",
            )
    else:
        sources = []
        for index in range(len(clips)):
            sources.append(f"[v{index}]")
            if with_audio:
                sources.append(f"[a{index}]")
        parts.append(
            "".join(sources)
            + f"concat=n={len(clips)}:v=1:a={1 if with_audio else 0}"
            + ("[v][a]" if with_audio else "[v]")
        )

    events = _text_events(spec, width, height)
    if events:
        degrade(
            "text",
            f"{len(events)} 条文字",
            "ASS/libass 烧录（静态）",
            "剪映文字模板/花字/入场动画 → 纯 ASS 静态文字；字号/颜色/对齐按 spec 近似，"
            "没有剪映的花字贴图与动画曲线",
        )

    argv = [tool.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
    for clip in clips:
        argv.extend(["-ss", f"{clip['source_start']:g}", "-t", f"{clip['duration']:g}"])
        argv.extend(["-i", clip["path"]])
    graph = ";".join(parts)
    video_label = "[v]"
    if with_audio:
        audio_label = "[a]"
    output_target = Path(output)

    with tempfile.TemporaryDirectory(prefix="jy-local-") as tmp_dir:
        if events:
            script_path = Path(tmp_dir) / "timeline.ass"
            script = effects.ass_script(
                width=width,
                height=height,
                events=events,
                font_size=64,
                alignment=5,
            )
            script_path.write_text(script, encoding="utf-8")
            graph += (
                f";{video_label}ass=filename='"
                + ffmpeg.escape_filter_path(str(script_path))
                + "',format=yuv420p[vt]"
            )
            video_label = "[vt]"
        argv.extend(["-filter_complex", graph, "-map", video_label])
        if with_audio:
            argv.extend(["-map", audio_label])
        argv.extend(_video_encode_args())
        if with_audio:
            argv.extend(["-c:a", "aac", "-b:a", "192k"])
        else:
            argv.append("-an")
        argv.extend(["-movflags", "+faststart"])
        argv.extend(["-y", str(output_target)])
        ffmpeg.run_ffmpeg(tool, argv, verbose=verbose)

    info = ffmpeg.output_info(tool, str(output_target))
    data = {
        "engine": "local-ffmpeg-approximation",
        "output": info,
        "degraded": True,
        "degraded_items": degraded,
        "timeline": {
            "clips": [Path(clip["path"]).name for clip in clips],
            "texts": len(events),
            "transitions": transition_count,
            "canvas": {"width": width, "height": height, "fps": fps},
            "audio": with_audio,
        },
        "note": "本地产物是剪映时间线的降级近似：素材特效/花字/转场库/贴纸/蒙版/美颜都不存在，"
        "已逐项列在 degraded_items（这不是静默替换）。",
        "ffmpeg_argv": argv,
    }
    return data
