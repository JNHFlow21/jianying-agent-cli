# Contributing

Thanks for helping improve `jy`.

1. Fork, branch, and keep each change small and focused.
2. Every command must keep the `jy.v1` contract: exactly one JSON object on stdout, logs on stderr.
3. Use the standard library only (no pip dependencies). ffmpeg is invoked with argv lists, never a shell.
4. Add or update a check in `tests/smoke.sh` and run it: `./tests/smoke.sh`. 剪映-engine checks skip
   when the app is not running; a skip is not a pass, so describe what you verified manually.
5. Never commit media, rendered output, 剪映 drafts, or anything from your own 剪映 account.
6. Use conventional commit messages (`feat:`, `fix:`, `docs:` …).

Bug reports: include `jy doctor` output (it contains no credentials) and the failing envelope.
