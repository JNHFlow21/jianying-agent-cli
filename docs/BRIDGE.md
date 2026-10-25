# 剪映 native bridge — protocol notes

How `jy` drives the installed 剪映专业版 without touching its UI. Reverse-engineered from
`/Applications/VideoFusion-macOS.app` (11.6.13345 / build 11.7.0-beta5). Everything here is
localhost-only.

## Discovery

剪映 starts a local HTTP server on a **random port** and writes a descriptor:

```
$TMPDIR/infinite-canvas-native-bridge.json
{"app":"infinite_canvas","pid":49256,"port":62124,"token":"<uuid>","version":1}
```

Both port **and** token change on every app launch. Re-read the descriptor on every run; never cache
it, never persist the token. If the file is absent, 剪映 is not running.

Auth: header `x-infinite-canvas-native-token: <token>`. Without it every route answers
`401 {"error":"unauthorized","message":"Invalid native bridge token."}`.

## Routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/.infinite-canvas-native-bridge/health` | liveness + `local_agent_server` state |
| GET | `/custom-agent/context` | host context: appid, device id, uid, account scope, login cookie (**credential — never persist**) |
| POST | `/custom-agent/film-export` | render a draft folder to MP4 with 剪映's own renderer |

Only these three are served by the native bridge; unknown paths return
`404 {"error":"not_found","message":"Unknown Infinite Canvas native bridge route."}`.

## film-export

Request body:

```json
{"accountScope":"uid:<uid from /custom-agent/context>",
 "requestId":"<uuid>",
 "snapshotId":"film-export-<same uuid>",
 "revision":"sha256:<sha256 of draft_info.json>",
 "draftPath":"/abs/path/to/draft-folder",
 "draftSha256":"<same sha256>",
 "outputPath":"/abs/path/out.mp4",
 "format":"mp4",
 "timeoutMs":600000}
```

Success: `200 {"ok":true,"format":"mp4","path":"...","requestId":"...","revision":"sha256:...","snapshotId":"..."}`
and the file on disk is a real MP4 (verify with ffprobe, not with the JSON).

Observations that matter:

- **No window, no tab, no click, no agent server, no LLM turn.** The only requirement is that 剪映 is
  running. The job is a native `InfiniteCanvasFilmExportJob`.
- The job **may raise the 剪映 window** and take focus; `jy render` compensates with a focus guard.
- 剪映 **mutates the draft folder** during a render (adds `draft_content.json`, `template-2.tmp`,
  normalises `draft_info.json`, so the file's SHA changes afterwards). Re-hash before re-rendering an
  already-rendered draft.
- A plaintext `draft_info.json` is accepted, so `jy draft build` writes readable JSON. jy never
  reads, decrypts or rewrites drafts that 剪映 itself saved.

## Error catalogue (native strings)

`film_export_invalid_request`, `film_export_draft_manager_unavailable`, `film_export_runtime_unavailable`,
`film_export_frame_canvas_unavailable`, `film_export_panel_unavailable`, `film_export_invalid_state`,
`film_export_unsupported_timeline`, `film_export_cancelled_or_timeout`,
`film_export_entitlement_bypass_forbidden`.

`jy` maps these to actionable envelope errors: entitlement ⇒ `BLOCKED` (needs membership quota),
missing dependency ⇒ `UNSUPPORTED` (resource not cached), transport/timeout ⇒ `UNAVAILABLE` (retriable).

## What is deliberately NOT used

- The bundled first-party CLI — it only runs when launched by 剪映 itself and has no general
  draft→MP4 command. jy does not try to work around that.
- Draft encryption — not touched; unnecessary because plaintext drafts are accepted.
- The LLM agent tools of 剪映's local service — not used; only the two endpoints above are called.
