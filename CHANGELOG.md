# Changelog

## 0.3.0 — 2026-10-25

First public release (tag `v0.3.0`; earlier 0.1 and 0.2 builds were private).

- `jy` CLI for editing and rendering video with 剪映专业版 from the command line: the agent writes a draft and 剪映's own native engine renders it, no GUI clicks.
- 39 commands (56 with subcommands): probe, trim, concat, speed, reverse, montage and beat cut, subtitles, ASS text animations, 50+ transitions, filters and LUTs, curves/HSL, chroma key, picture-in-picture, audio mix, ducking and loudnorm, local ASR, covers, GIF, batch export and background jobs.
- Two engines: `jianying` (renders drafts through the installed 剪映 app) and `local` (ffmpeg, offline, deterministic).
- Live material catalog: `jy effects search-remote` searches 剪映's full online catalog of transitions, filters, effects, 花字, text animations and stickers; `jy effects fetch` downloads packages into 剪映's cache (md5-checked, atomic); `jy draft build --fetch` resolves specs by name.
- Chinese 剪映 names such as `--type 叠化` and `--preset 日系` resolve to local implementations with a `fidelity` grade (`exact | close | rough`).
- One `jy.v1` JSON object on stdout for every command; outputs are re-probed with ffprobe so `ok:true` means a real, playable file.
- Bundled agent skill (`skills/jianying-native`), zero Python dependencies, `tests/smoke.sh` (260 checks) in CI on macOS, and a rendered hero demo (`docs/render_demo.py`).
