"""Audio verbs: ``jy audio`` (gain/fades/BGM/ducking/loudnorm/denoise) and
``jy beats`` (local onset detection → a file for ``jy montage --sync-beats``)."""

from __future__ import annotations

import array
import subprocess

from ..envelope import JyError
from ._common import (
    _checked_output,
    _input_summary,
    _movflags,
    _probe,
    _require_video,
    _require_video_container,
    _resolve_input,
    _run,
    between,
    number,
    require_audio,
    write_text_output,
)


# ---------------------------------------------------------------------------
# audio


def _audio_actions(args):
    actions = []
    if float(args.volume) != 1.0:
        actions.append("volume")
    if args.fade_in:
        actions.append("fade-in")
    if args.fade_out:
        actions.append("fade-out")
    if args.bgm:
        actions.append("bgm")
    if args.loudnorm:
        actions.append("loudnorm")
    if args.denoise:
        actions.append("denoise")
    return actions


def _voice_chain(args, duration):
    chain = []
    volume = between(args.volume, 0.0, 8.0, "--volume")
    if volume != 1.0:
        chain.append(f"volume={number(volume)}")
    fade_in = between(args.fade_in, 0.0, 3600.0, "--fade-in")
    fade_out = between(args.fade_out, 0.0, 3600.0, "--fade-out")
    if fade_in:
        chain.append(f"afade=t=in:st=0:d={number(fade_in)}")
    if fade_out:
        if duration:
            chain.append(
                f"afade=t=out:st={number(max(0.0, duration - fade_out))}:d={number(fade_out)}"
            )
        else:
            raise JyError(
                "UNSUPPORTED", "--fade-out needs a known source duration (probe failed)"
            )
    if args.denoise:
        chain.append("afftdn=nf=-25")
    if args.loudnorm:
        chain.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    return ",".join(chain)


def audio(ctx, args):
    """Change/augment the audio track; the video stream is copied untouched."""
    actions = _audio_actions(args)
    if not actions:
        raise JyError(
            "VALIDATION",
            "nothing to do: give at least one of --volume/--fade-in/--fade-out/"
            "--bgm/--loudnorm/--denoise",
        )
    if args.duck and not args.bgm:
        raise JyError("VALIDATION", "--duck only makes sense with --bgm")
    if args.bgm_replace and not args.bgm:
        raise JyError("VALIDATION", "--bgm-replace needs --bgm")
    if args.duck and args.bgm_replace:
        raise JyError("VALIDATION", "--duck needs the original voice; drop --bgm-replace")
    filters = ["volume", "afade"]
    if args.loudnorm:
        filters.append("loudnorm")
    if args.denoise:
        filters.append("afftdn")
    if args.bgm:
        filters.append("amix")
    if args.duck:
        filters.append("sidechaincompress")
    tool = ctx.require(filters=filters)

    input_path = _resolve_input(args.input)
    source = _probe(tool, input_path)
    duration = source["duration_s"] or None
    has_audio = bool(source["audio_streams"])
    bgm_path = _resolve_input(args.bgm) if args.bgm else None
    bgm_source = _probe(tool, bgm_path) if bgm_path else None
    if bgm_source:
        if not bgm_source["audio_streams"]:
            raise JyError("UNSUPPORTED", f"--bgm has no audio stream: {bgm_path}")
    if not has_audio and not args.bgm:
        raise JyError(
            "UNSUPPORTED",
            f"{source['path']} has no audio stream; pass --bgm to add music",
        )
    if source["codec"]:
        _require_video(source)
    _require_video_container(args.output)
    output = _checked_output(args.output, args.force)
    voice_chain = _voice_chain(args, duration) if has_audio else ""
    warnings = []

    if not args.bgm:
        argv = [tool.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                "-i", str(input_path)]
        if source["codec"]:
            argv.extend(["-c:v", "copy"])
        if voice_chain:
            argv.extend(["-af", voice_chain])
        if not has_audio:
            argv.extend(["-an"])
        else:
            argv.extend(["-c:a", "aac", "-b:a", "192k"])
        argv.extend(_movflags(str(output)))
        argv.extend(["-y", str(output)])
    else:
        bgm_volume = between(args.bgm_volume, 0.0, 4.0, "--bgm-volume")
        pieces = []
        if args.bgm_replace or not has_audio:
            if args.bgm_replace:
                warnings.append("--bgm-replace：原片音轨被丢弃")
            chain = f"volume={number(bgm_volume)}"
            if voice_chain:
                warnings.append(
                    "替换音轨时 --volume/--fade-in/--fade-out/--loudnorm/--denoise "
                    "作用在 BGM 上"
                )
                chain = ",".join(piece for piece in (chain, voice_chain) if piece)
            if duration:
                chain = f"atrim=duration={number(duration)}," + chain
            pieces.append(f"[1:a]{chain}[a]")
        elif args.duck:
            pieces.append(f"[0:a]{voice_chain + ',' if voice_chain else ''}asplit=2[voice][key]")
            pieces.append(f"[1:a]volume={number(bgm_volume)}[bgm]")
            pieces.append(
                "[bgm][key]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=350[ducked]"
            )
            pieces.append(
                "[voice][ducked]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]"
            )
        else:
            pieces.append(f"[0:a]{voice_chain + ',' if voice_chain else ''}anull[voice]")
            pieces.append(f"[1:a]volume={number(bgm_volume)}[bgm]")
            pieces.append(
                "[voice][bgm]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]"
            )
        argv = [tool.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                "-i", str(input_path), "-i", str(bgm_path),
                "-filter_complex", ";".join(pieces), "-map", "0:v?", "-map", "[a]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k"]
        argv.extend(_movflags(str(output)))
        argv.extend(["-y", str(output)])
    if args.loudnorm:
        warnings.append("loudnorm 是单遍（single-pass）；对精确响度要求高的场景请用两遍法")
    if args.denoise:
        warnings.append("afftdn 是频谱降噪的保守档；强噪声会留下金属音，重的降噪需要 AI 模型")
    ctx.warnings.extend(warnings)
    info = _run(tool, argv, output, ctx.verbose)
    return "executed", {
        "input": _input_summary(source),
        "actions": actions,
        "voice_chain": voice_chain or None,
        "bgm": _input_summary(bgm_source) if bgm_source else None,
        "bgm_volume": float(args.bgm_volume) if args.bgm else None,
        "duck": bool(args.duck),
        "replace": bool(args.bgm_replace),
        "output": info,
        "ffmpeg_argv": argv,
    }


# ---------------------------------------------------------------------------
# beats


def decode_mono(tool, path, rate=22050):
    """Decode the first audio stream to mono s16 PCM (bytes) via ffmpeg."""
    done = subprocess.run(
        [
            tool.ffmpeg,
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(rate),
            "-f",
            "s16le",
            "-",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )
    if done.returncode != 0:
        raise JyError(
            "INTERNAL",
            "ffmpeg could not decode the audio track",
            details={"tail": done.stderr.decode("utf-8", "replace")[-800:]},
        )
    return done.stdout, rate


def detect_onsets(samples, rate, *, sensitivity=1.0, min_gap_ms=200, frame=1024, hop=256):
    """Energy-flux onset detection (no ML, no numpy).

    Half-wave-rectified difference of short-time energy, thresholded against a
    local adaptive mean. Good on steady 4/4 material with clear transients;
    orchestral/legato music needs a real beat tracker.
    """
    pcm = array.array("h")
    pcm.frombytes(samples[: len(samples) - (len(samples) % 2)])
    if not pcm:
        return []
    energies = []
    for start in range(0, len(pcm) - frame, hop):
        chunk = pcm[start : start + frame]
        total = 0
        for value in chunk:
            total += value * value
        energies.append(total / frame)
    if not energies:
        return []
    flux = [0.0] + [
        max(0.0, energies[index] - energies[index - 1])
        for index in range(1, len(energies))
    ]
    window = 12
    peak = max(flux) or 1.0
    onsets = []
    last_ms = -1e9
    for index, value in enumerate(flux):
        low = max(0, index - window)
        high = min(len(flux), index + window + 1)
        local = flux[low:high]
        mean = sum(local) / len(local)
        threshold = mean * (1.6 / max(0.1, sensitivity)) + peak * 0.02
        if value <= threshold:
            continue
        if value < 0.05 * peak:
            continue
        # local maximum inside +-3 frames
        neighbours = flux[max(0, index - 3) : index + 4]
        if value < max(neighbours):
            continue
        time_ms = (index * hop / rate) * 1000.0
        if time_ms - last_ms < min_gap_ms:
            continue
        if time_ms <= 0:
            continue
        onsets.append(time_ms)
        last_ms = time_ms
    return onsets


def beats(ctx, args):
    """Detect beat/onset timestamps and write a ``--sync-beats`` compatible file."""
    sensitivity = between(args.sensitivity, 0.1, 10.0, "--sensitivity")
    min_gap = between(args.min_gap_ms, 20, 5000, "--min-gap-ms")
    tool = ctx.require()
    input_path = _resolve_input(args.input)
    source = _probe(tool, input_path)
    require_audio(
        source,
        hint=f"{source['path']} has no audio stream; --sync-beats needs audio",
    )
    samples, rate = decode_mono(tool, input_path, int(args.analysis_rate))
    onsets = detect_onsets(
        samples,
        rate,
        sensitivity=sensitivity,
        min_gap_ms=min_gap,
        frame=int(args.frame),
        hop=max(64, int(args.frame) // 4),
    )
    if not onsets:
        raise JyError(
            "UNSUPPORTED",
            "no onsets detected; try a lower --sensitivity (e.g. 0.6) or a track with "
            "clear transients",
            details={"duration_s": source["duration_s"]},
        )
    lines = [f"{int(round(value))}" for value in onsets]
    path = write_text_output(args.output, "\n".join(lines) + "\n", bool(args.force))
    intervals = [round((b - a) / 1000.0, 3) for a, b in zip(onsets, onsets[1:])]
    ctx.warnings.append(
        "beats 用能量通量（onset flux）检测，没有 ML/节拍网格；"
        "电子/流行 4/4 准，连奏/管弦乐会漏或抖，请用 --sensitivity 调整。"
    )
    return "executed", {
        "input": _input_summary(source),
        "engine": "energy-flux-onsets",
        "analysis": {
            "sample_rate": rate,
            "frame": int(args.frame),
            "sensitivity": sensitivity,
            "min_gap_ms": min_gap,
        },
        "count": len(onsets),
        "first_ms": int(round(onsets[0])),
        "last_ms": int(round(onsets[-1])),
        "mean_interval_s": round(sum(intervals) / len(intervals), 3) if intervals else None,
        "output": {
            "path": str(path),
            "size_bytes": path.stat().st_size,
            "lines": len(lines),
        },
        "beats_ms": [int(round(value)) for value in onsets],
    }


def register(r):
    audio_parser = r.parser_for(
        "audio",
        parents=[r.output_parent],
        help="volume / fades / BGM mix or replace / ducking / loudnorm / denoise",
        description="Audio-only post: the video stream is copied. Actions can be "
        "combined. --duck lowers the BGM under the original voice; --bgm-replace "
        "discards the original track. loudnorm is single-pass; afftdn is a "
        "conservative spectral denoiser.",
    )
    audio_parser.add_argument("input", metavar="input")
    audio_parser.add_argument("--volume", type=float, default=1.0, help="0..8 (1 = unchanged)")
    audio_parser.add_argument("--fade-in", type=float, default=0.0, metavar="SEC")
    audio_parser.add_argument("--fade-out", type=float, default=0.0, metavar="SEC")
    audio_parser.add_argument("--bgm", metavar="FILE", help="music/voice-over to mix in")
    audio_parser.add_argument("--bgm-volume", type=float, default=0.3, help="0..4")
    audio_parser.add_argument(
        "--bgm-replace", action="store_true", help="drop the original track"
    )
    audio_parser.add_argument(
        "--duck", action="store_true", help="compress the BGM under the original voice"
    )
    audio_parser.add_argument(
        "--loudnorm", action="store_true", help="EBU R128 loudness normalisation (-16 LUFS)"
    )
    audio_parser.add_argument(
        "--denoise", action="store_true", help="spectral denoise (afftdn)"
    )
    r.bind(audio_parser, "audio", audio)

    beats_parser = r.parser_for(
        "beats",
        parents=[r.output_parent],
        help="detect beat timestamps locally -> file for `jy montage --sync-beats`",
        description="Local onset detection (no network, no ML): decodes the audio, "
        "computes energy flux, picks local maxima with an adaptive threshold and a "
        "minimum gap, then writes one millisecond timestamp per line — exactly the "
        "format `jy montage --sync-beats` reads. Accuracy: solid on steady 4/4 "
        "material with clear transients; not a musicological beat tracker.",
    )
    beats_parser.add_argument("input", metavar="audio-or-video")
    beats_parser.add_argument("--sensitivity", type=float, default=1.0)
    beats_parser.add_argument("--min-gap-ms", type=int, default=200)
    beats_parser.add_argument("--analysis-rate", type=int, default=22050)
    beats_parser.add_argument("--frame", type=int, default=1024)
    r.bind(beats_parser, "beats", beats)
