#!/usr/bin/env python3
"""Install the `jy` entry point into a directory on PATH.

Mirrors the `wechat-native` / `telegram-native` convention: a thin launcher on
PATH that execs the project's real entry point, so the checkout stays the single
source of truth.

    ./install_global.py              # symlink into ~/.local/bin (default)
    ./install_global.py --dir DIR    # symlink into DIR
    ./install_global.py --copy       # copy instead of symlink
    ./install_global.py --dry-run    # show what would happen
    ./install_global.py --with-skill # also install the agent skill
                                     # (~/.claude/skills and ~/.agents/skills)

An existing file at the target that is not already our launcher is moved aside
to ``<name>.bak-<timestamp>`` first; nothing is deleted.

Prints one JSON object on stdout so an agent can consume the result.
"""
import argparse
import json
import os
import shutil
import stat
import sys
import time

NAME = "jy"
SKILL = "jianying-native"
DEFAULT_SKILL_DIRS = ("~/.claude/skills", "~/.agents/skills")


def _backup(path):
    """Move ``path`` aside (never delete) and return the backup path."""
    backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    os.rename(path, backup)
    return backup


def _install_skill(root, skill_dirs, dry_run):
    src = os.path.join(root, "skills", SKILL)
    rows = []
    for base in skill_dirs:
        dst = os.path.join(os.path.expanduser(base), SKILL)
        row = {"target": dst, "backup": None}
        exists = os.path.lexists(dst)
        if dry_run:
            row["would_backup"] = exists
            rows.append(row)
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if exists:
            row["backup"] = _backup(dst)
        shutil.copytree(src, dst)
        rows.append(row)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--dir", default=os.path.expanduser("~/.local/bin"))
    ap.add_argument("--copy", action="store_true", help="copy the launcher instead of symlinking")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--with-skill", action="store_true",
                    help="also copy skills/jianying-native into the agent skill dirs")
    ap.add_argument("--skill-dir", action="append", default=None,
                    help="skill directory to install into (repeatable; default: "
                         + ", ".join(DEFAULT_SKILL_DIRS) + ")")
    args = ap.parse_args()

    root = os.path.dirname(os.path.realpath(__file__))
    src = os.path.join(root, "bin", NAME)
    dst = os.path.join(os.path.expanduser(args.dir), NAME)

    result = {"schema": "jy.install.v1", "ok": False, "source": src, "target": dst, "mode": None}
    if not os.path.isfile(src):
        result["error"] = {"code": "NOT_FOUND", "message": f"launcher missing: {src}"}
        print(json.dumps(result, ensure_ascii=False))
        return 1

    os.makedirs(os.path.dirname(dst), exist_ok=True)
    on_path = os.path.dirname(dst) in os.environ.get("PATH", "").split(":")

    skill_dirs = args.skill_dir or list(DEFAULT_SKILL_DIRS)
    ours = os.path.islink(dst) and os.path.realpath(dst) == os.path.realpath(src)

    if args.dry_run:
        result.update({"ok": True, "mode": "copy" if args.copy else "symlink",
                       "would_write": dst, "on_path": on_path,
                       "would_backup": os.path.lexists(dst) and not ours})
        if args.with_skill:
            result["skill"] = _install_skill(root, skill_dirs, dry_run=True)
        print(json.dumps(result, ensure_ascii=False))
        return 0

    try:
        if ours:
            os.unlink(dst)  # our own symlink: safe to refresh
        elif os.path.lexists(dst):
            result["backup"] = _backup(dst)
        if args.copy:
            shutil.copy2(src, dst)
            result["mode"] = "copy"
        else:
            os.symlink(os.path.realpath(src), dst)
            result["mode"] = "symlink"
        st = os.stat(dst)
        os.chmod(dst, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        if args.with_skill:
            result["skill"] = _install_skill(root, skill_dirs, dry_run=False)
    except OSError as exc:
        result["error"] = {"code": "INTERNAL", "message": str(exc)}
        print(json.dumps(result, ensure_ascii=False))
        return 1

    result.update({"ok": True, "on_path": on_path})
    if not on_path:
        result["warning"] = f"{os.path.dirname(dst)} is not on PATH; add it to use `{NAME}` directly"
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
