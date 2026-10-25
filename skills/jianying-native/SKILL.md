---
name: jianying-native
description: Edit videos headlessly with the `jy` CLI (jianying-native) — probe, trim, concat, speed/curve speed, montage/beat cut, subtitles, text, transitions, filters/LUTs, chroma key, audio mix, covers, keyframes, masks, batch export — and optionally render drafts with the locally installed 剪映专业版 / JianyingPro (CapCut China desktop) native engine for 剪映 materials (effects, transitions, filters, stickers, 花字, beauty, matting, color wheels, text animations). Every call prints one jy.v1 JSON object. Use for "cut this footage into a video", "add subtitles / a title / a cover", "add transitions and a filter", "batch export", 剪辑视频 / 加字幕 / 加转场滤镜 / 批量出片. Not for uploading or publishing to platforms.
---

# jianying-native (`jy`)

`jy` must be on PATH (`jy doctor` works). Run `jy doctor` once per session; check flags with
`jy <verb> --help`. The contract lives in `USAGE.md` of the repo — do not guess flags from this file.

## Pick the engine first

| Need | Engine |
|---|---|
| Deterministic editing: cut / join / speed / subtitles / titles / transitions / grading / keying / audio / resolution / codec | **local** (ffmpeg): fast, offline, no membership quota |
| 剪映-only materials: effects, 花字, stickers, templates, beauty, smart matting | **jianying**: `jy draft build` + `jy render` (剪映 must be running) |

Unsure? `jy doctor` reports both ffmpeg builds, whether 剪映 is reachable, and membership notes.

## Hard rules

- **Trust only `ok`.** stdout is always exactly one `jy.v1` JSON object. On failure `data` is `null`
  and `error.code` is one of `VALIDATION` `NOT_FOUND` `UNSUPPORTED` `UNAVAILABLE` `BLOCKED` `INTERNAL`.
- Retry only when `error.retriable` is true. `BLOCKED` means a human must act (membership, material
  download) — never loop on it.
- `data.output` of an executed verb is re-probed with ffprobe, so it is a real file.
- Outputs are never overwritten without `--force`. Treat user media as read-only; write to new paths.
- Never modify the user's own 剪映 drafts. `draft build` writes a new folder; `render` only renders
  drafts jy generated (or a copy).
- 剪映's bridge port/token change every launch; `jy` rediscovers them. Never write the token to a file,
  argv or logs.
- Long jobs (renders, batches): `--async`, then `jy jobs wait <id>`.

## Envelope

```json
{"schema":"jy.v1","command":"export","ok":true,"outcome":"executed",
 "data":{"output":{"path":"out.mp4","size_bytes":123456,"duration_s":3.0,
                   "codec":"h264","width":1920,"height":1080,"fps":30.0}},
 "error":null,"warnings":[],"meta":{"ffmpeg":"/path/to/ffmpeg","elapsed_ms":1234}}
```

`outcome` is `readonly` / `preview` / `executed` / `noop` / `unknown`. Exit codes: `0` ok, `1` failure,
`2` validation.

## Common recipes

```bash
jy doctor                                                    # 0) self-check
jy probe a.mp4 b.mp4 | jq '.data.files[], .data.concat'       # 1) inspect, decide specs
jy trim a.mp4 --start 4 --duration 8 -o seg1.mp4              # 2) rough cut
jy concat seg1.mp4 b.mp4 -o rough.mp4                         #    (stream copy when specs match)
jy asr rough.mp4 -o rough.srt                                 # 3) local speech-to-text (whisper.cpp)
jy subtitle rough.mp4 --srt rough.srt --style cinematic --max-chars-per-line 14 -o subbed.mp4
jy text subbed.mp4 --content "Title" --position top --font-size 56 --outline 3 --animation slideup -o titled.mp4
jy montage a.mp4 b.mp4 c.mp4 --per-clip 2 --transition fade --transition-duration 0.3 -o montage.mp4
jy beats music.m4a -o beats.txt && jy montage a.mp4 b.mp4 --sync-beats beats.txt -o beatcut.mp4
jy transition a.mp4 b.mp4 --type 叠化 --duration 0.5 -o joined.mp4   # 剪映 Chinese names or xfade names
jy filter montage.mp4 --preset film --intensity 70 -o graded.mp4
jy export graded.mp4 -o final.mp4 --resolution 1080p --fps 30 --quality 20 --faststart
jy cover final.mp4 --at 50% -o final-cover.mp4
jy batch *.mp4 -o out/ --resolution 720p --async               # same recipe over many files
```

### 剪映 engine

```bash
jy draft build --spec spec.json -o ./draft-out
jy render ./draft-out -o final.mp4 --async && jy jobs wait <job-id>
```

```json
{"canvas":{"width":1080,"height":1920,"fps":30},
 "clips":[{"path":"/abs/a.mp4","start":0,"duration":3}],
 "texts":[{"content":"Title","start":0.5,"duration":2,
           "position":{"x":0,"y":0.3},
           "style":{"color":"#FFD400","font_size":14,"stroke_width":0.08}}]}
```

- Text styling **must** live under `style`; top-level style keys fail with `VALIDATION`.
- `position` is normalised to the half canvas, y up: `screen_x = W/2 + x·W/2`, `screen_y = H/2 − y·H/2`.
- `font_size` is 剪映's unit (≈ 6.7 px per unit at 1080 wide).
- Per-clip `keyframes`, `curve_speed`, `mask`, `color_wheel`, `filter`, `video_effects`, `face_beauty`,
  `matting`, and per-text `text_effect` / `text_template` / `animations`, plus top-level `effects`,
  `stickers`, `transitions` are supported — see `USAGE.md` §9. Look up ids with
  `jy effects search "<name>"`. Unknown keys fail with a suggestion.

## Engine pitfalls

- **剪映 must be running.** If not: `UNAVAILABLE` with "open 剪映 and retry". Do not spin.
- **Cold start**: the first export after launch often times out; `jy` retries once and sets
  `data.cold_start`. Wait for it instead of retrying manually.
- **Focus**: 剪映's export may raise its window. `jy` returns focus to the previous app and never moves
  the pointer; report `data.focus_guard` honestly. Never click the 剪映 UI yourself.
- **Membership (VIP / SVIP)**: tiers are server-side and unreadable offline. A refusal names the
  material (id + name). `--on-entitlement local` re-renders with local approximations and lists every
  loss in `data.degraded_items[]` — tell the user. `jy` never bypasses membership checks.
- **Material not cached**: `UNSUPPORTED` naming the resource; ask the user to open it once in 剪映.
- **One render at a time**: a cross-process lock serialises renders; never render in parallel.
- **Render success ≠ effect applied**: 剪映 silently keeps materials it cannot compose (no change or a
  black layer). Always pull a frame and look.

## Local approximations are not 剪映 materials

`jy transition / filter / text / beautify / key` are ffmpeg approximations: `beautify` is a global soft
look (not face-aware), `transition` is xfade, `key` is single-colour chroma key. Each alias reports a
`data.resolution.fidelity`. Say which one you used when reporting results.

## Delivery discipline

1. Before delivering, check 2–3 frames: `jy frame out.mp4 --at 10%`, `--at 50%`, `--at 90%` — titles
   inside the frame, subtitles not clipped, no black frames at transitions.
2. After 剪映 opens a jy draft it may migrate to `Timelines/<uuid>/`; `jy` resolves this automatically.
3. Bridge wedged → `jy render ... --relaunch-app` once; do not hammer a dead bridge.
4. Debug: `jy doctor`, `jy jobs status <id>`, `error.details.tail` (last ffmpeg stderr lines).
