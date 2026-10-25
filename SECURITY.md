# Security

`jy` runs locally and makes no network requests of its own. The 剪映 engine talks only to the
127.0.0.1 bridge that the running 剪映 app exposes; its per-launch token is read on every call and is
never written to disk, logs, or argv. `jy` never reads or prints the account cookie available from
the bridge context.

If you find a way `jy` leaks a credential, writes outside the paths it documents, or modifies a
剪映 draft it did not create without `--apply`, please report it privately via
[GitHub Security Advisories](https://github.com/JNHFlow21/jianying-agent-cli/security/advisories/new)
rather than a public issue.
