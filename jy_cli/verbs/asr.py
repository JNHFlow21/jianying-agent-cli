"""``jy asr``: local speech-to-text to SRT (whisper.cpp only — never uploads).

Nothing leaves the machine: jy decodes the audio to 16 kHz mono WAV, runs a local
whisper.cpp binary with a local model, and writes the SRT whisper produced. If no
engine or model is installed the verb fails with ``UNSUPPORTED`` and an install
hint instead of pretending.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..envelope import JyError
from ._common import _probe, _resolve_input, require_audio, text_output_info, write_text_output

WHISPER_BINARIES = ("whisper-cli", "whisper-cpp", "main")
MODEL_ENV = "JY_WHISPER_MODEL"
MODEL_SUFFIXES = (".bin",)


CRASH_SIGNATURES = (
    "abort",
    "ggml",
    "metal",
    "segmentation fault",
    "assert",
    "std::bad_alloc",
    "terminate called",
)


def _looks_like_crash(stderr):
    lowered = (stderr or "").lower()
    return any(signature in lowered for signature in CRASH_SIGNATURES)


def model_search_paths():
    paths = []
    override = os.environ.get(MODEL_ENV)
    if override:
        paths.append(Path(override).expanduser())
    for directory in (
        Path.home() / ".whisper-models",
        Path.home() / ".cache" / "whisper",
        Path("/opt/homebrew/opt/whisper-cpp/share/whisper-cpp/models"),
        Path("/usr/local/opt/whisper-cpp/share/whisper-cpp/models"),
    ):
        if directory.is_dir():
            for candidate in sorted(directory.glob("ggml-*.bin")):
                paths.append(candidate)
    return paths


def find_binary():
    for name in WHISPER_BINARIES:
        found = shutil.which(name)
        if found:
            return found
    return None


def state():
    """Compact, side-effect-free engine report for ``jy doctor``."""
    binary = find_binary()
    models = model_search_paths()
    return {
        "available": bool(binary and models),
        "engine": "whisper.cpp" if binary else None,
        "binary": binary,
        "models": [str(path) for path in models[:4]],
        "model": str(models[0]) if models else None,
        "network": "never (local binary + local model only)",
        "install_hint": None
        if binary and models
        else "brew install whisper-cpp 并放一个 ggml-*.bin 模型到 ~/.whisper-models/ "
        f"（或用 ${MODEL_ENV} 指定）",
    }


def asr(ctx, args):
    """Transcribe the clip's audio locally and write an SRT."""
    engine = state()
    if not engine["binary"]:
        raise JyError(
            "UNSUPPORTED",
            "没有找到本地语音识别引擎（whisper.cpp）。jy 从不上传音频，所以不会走云端。"
            "安装：brew install whisper-cpp；再放一个模型（ggml-base.bin 等）到 "
            f"~/.whisper-models/ 或用 ${MODEL_ENV} 指定。",
            details={"engine": engine},
        )
    model = Path(args.model).expanduser() if args.model else Path(engine["model"] or "")
    if not args.model and not engine["model"]:
        raise JyError(
            "UNSUPPORTED",
            "找到 whisper-cli 但没有模型文件。把一个 ggml-*.bin 放到 ~/.whisper-models/，"
            f"或用 --model / ${MODEL_ENV} 指定。",
            details={"engine": engine},
        )
    if not model.is_file():
        raise JyError("NOT_FOUND", f"whisper model not found: {model}")

    tool = ctx.require()
    input_path = _resolve_input(args.input)
    source = _probe(tool, input_path)
    require_audio(
        source,
        hint=f"{source['path']} has no audio stream; nothing to transcribe",
    )
    threads = int(args.threads)
    if not 1 <= threads <= 32:
        raise JyError("VALIDATION", "--threads must be 1..32")
    language = args.language

    default_model = Path(engine["model"]) if engine.get("model") else None
    candidates = [model]
    fallback_from = None
    if default_model and default_model.resolve() != model.resolve():
        candidates.append(default_model)

    attempts = []
    srt_text = None
    used_model = None
    with tempfile.TemporaryDirectory(prefix="jy-asr-") as tmp_dir:
        wav_path = Path(tmp_dir) / "audio.wav"
        extract = [
            tool.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", str(input_path), "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", "-y", str(wav_path),
        ]
        subprocess.run(
            extract,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        if not wav_path.is_file() or wav_path.stat().st_size == 0:
            raise JyError(
                "INTERNAL",
                "could not extract a 16 kHz mono WAV for the recogniser",
                details={"argv": extract},
            )
        output_base = Path(tmp_dir) / "transcript"
        for candidate in candidates:
            attempt = {"model": str(candidate)}
            command = [
                engine["binary"],
                "-m", str(candidate),
                "-f", str(wav_path),
                "-osrt",
                "-of", str(output_base),
                "-t", str(threads),
            ]
            if language and language != "auto":
                command.extend(["-l", language])
            done = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=max(60, int((source["duration_s"] or 60) * 4)),
            )
            attempt["returncode"] = done.returncode
            srt_path = output_base.with_suffix(".srt")
            if done.returncode == 0 and srt_path.is_file():
                attempt["ok"] = True
                srt_text = srt_path.read_text(encoding="utf-8", errors="replace").strip()
                used_model = candidate
                attempts.append(attempt)
                break
            attempt["ok"] = False
            attempt["crash"] = done.returncode < 0 or _looks_like_crash(done.stderr or "")
            attempt["tail"] = (done.stderr or "")[-400:]
            attempts.append(attempt)
            # A crashing model is a "this model cannot run here" signal, not a
            # reason to hand the user a raw INTERNAL tail.
            if len(candidates) == 1:
                break

        if srt_text is None:
            failed = attempts[0] if attempts else {}
            reason = failed.get("tail") or "whisper.cpp produced no SRT"
            raise JyError(
                "UNSUPPORTED",
                "whisper.cpp 无法用这个模型跑完（模型与本地 whisper.cpp 不匹配，"
                f"返回码 {failed.get('returncode')}）。换一个模型：ggml-medium / ggml-large-v3，"
                "或用 faster-whisper（pip install faster-whisper）。"
                + (
                    f" 已尝试自动回退到 {default_model.name}，同样失败。"
                    if default_model and len(candidates) > 1
                    else ""
                ),
                details={
                    "attempts": attempts,
                    "model_requested": str(model),
                    "default_model": str(default_model) if default_model else None,
                    "tail": reason[-800:],
                    "engine": engine["engine"],
                },
            )
        if used_model != model:
            fallback_from = str(model)
            ctx.warnings.append(
                f"--model {model} 跑不起来（返回码 "
                f"{attempts[0].get('returncode')}，多半是模型与 whisper.cpp/Metal 不匹配）；"
                f"已自动回退到 {used_model.name}，这份 SRT 来自 {used_model.name}。"
            )
        if not srt_text:
            raise JyError(
                "UNSUPPORTED",
                "whisper.cpp ran but produced no speech segments (silence or music?)",
                details={"engine": engine["engine"], "model": str(used_model)},
            )
        path = write_text_output(args.output, srt_text + "\n", bool(args.force))
        info = text_output_info(path)

    cues = sum(1 for line in srt_text.splitlines() if "-->" in line)
    ctx.warnings.append(
        "ASR 是本地 whisper.cpp 单遍识别：中文标点/断句来自模型，可能有错字；"
        "重要片子建议人工校对，再 `jy subtitle` 烧录。"
    )
    return "executed", {
        "engine": engine["engine"],
        "binary": engine["binary"],
        "model": str(used_model),
        "model_requested": str(model),
        "model_fallback": (
            {"from": fallback_from, "to": str(used_model), "reason": "whisper.cpp crash"}
            if fallback_from
            else None
        ),
        "attempts": attempts,
        "language": language,
        "duration_s": source["duration_s"],
        "cues": cues,
        "network": "never",
        "output": info,
        "next": f"jy subtitle {input_path} --srt {path} -o <out.mp4>",
    }


def register(r):
    parser = r.parser_for(
        "asr",
        parents=[r.output_parent],
        help="local speech-to-text -> SRT (whisper.cpp; never uploads audio)",
        description="Decode audio to 16 kHz mono WAV, run a local whisper.cpp binary "
        "with a local ggml model, write the SRT. No network, no cloud, no fake: without "
        "a local engine+model this fails with UNSUPPORTED and an install hint.",
    )
    parser.add_argument("input", metavar="media")
    parser.add_argument("--model", metavar="FILE", help="ggml-*.bin (default: auto-discover)")
    parser.add_argument(
        "--language", default="auto", metavar="auto|zh|en|ja",
        help="whisper language code (auto = let the model detect)",
    )
    parser.add_argument("--threads", type=int, default=4)
    r.bind(parser, "asr", asr)
