"""Argument parsing and the single dispatch that emits the jy.v1 envelope.

This module owns no verb: the parser is assembled from ``jy_cli/verbs/*.py``
(each exposes ``register(registry)``), so new verbs land in their own files.  It
also owns the one stdout writer, the exit-code mapping, ``--async`` job
spawning, and the job-result hook for background children.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback

from . import commands, envelope, jobs, verbs
from .envelope import JyError


class _Parser(argparse.ArgumentParser):
    """Help/usage go to stderr so stdout stays JSON-only."""

    def print_help(self, file=None):
        super().print_help(file or sys.stderr)

    def print_usage(self, file=None):
        super().print_usage(file or sys.stderr)


def _parents(parser_class):
    verbose_parent = parser_class(add_help=False)
    verbose_parent.add_argument(
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="stream ffmpeg logs to stderr",
    )
    output_parent = parser_class(add_help=False, parents=[verbose_parent])
    output_parent.add_argument("-o", "--output", required=True, metavar="PATH")
    output_parent.add_argument(
        "--force", action="store_true", help="overwrite an existing output"
    )
    async_parent = parser_class(add_help=False)
    async_parent.add_argument(
        "--async",
        dest="async_run",
        action="store_true",
        default=argparse.SUPPRESS,
        help="run in the background and return a job id (see `jy jobs`)",
    )
    return verbose_parent, output_parent, async_parent


def build_parser():
    parser = _Parser(
        prog="jy",
        description="Headless, ffmpeg-backed local video CLI (jy.v1 JSON on stdout).",
        epilog="Every invocation prints one jy.v1 JSON object on stdout. "
        "See USAGE.md for the contract.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="stream ffmpeg logs to stderr",
    )
    parser.add_argument(
        "--async",
        dest="async_run",
        action="store_true",
        default=argparse.SUPPRESS,
        help="run the verb in the background and return a job id (`jy jobs`)",
    )
    subparsers = parser.add_subparsers(
        dest="command", required=True, parser_class=_Parser, metavar="command"
    )
    verbose_parent, output_parent, async_parent = _parents(_Parser)
    registry = verbs.Registry(
        parser, subparsers, _Parser, verbose_parent, output_parent, async_parent
    )
    verbs.discover(registry)
    parser.jy_commands = registry.defaults()
    parser.jy_groups = tuple(registry.groups)
    parser.jy_problems = registry.problems()
    return parser


def _guess_command(argv, parser):
    """The verb label for an argparse failure (precise: only the command position)."""
    positionals = [token for token in argv if not token.startswith("-")]
    known = set(getattr(parser, "jy_commands", ()))
    if len(positionals) >= 2:
        pair = f"{positionals[0]} {positionals[1]}"
        if pair in known:
            return pair
    if positionals and positionals[0] in known:
        return positionals[0]
    return "unknown"


def _elapsed_ms(started):
    return int(round((time.monotonic() - started) * 1000))


def _payload(command, ok, outcome, data, error, ctx, started):
    return envelope.build(
        command=command,
        ok=ok,
        outcome=outcome,
        data=data,
        error=error.as_dict() if error else None,
        warnings=ctx.warnings if ctx else [],
        ffmpeg=ctx.ffmpeg_path if ctx else None,
        elapsed_ms=_elapsed_ms(started),
    )


def _wants_async(args):
    return bool(getattr(args, "async_run", False))


def _spawn_async(parser, argv, ctx, started):
    """Start the same invocation as a detached child and return its job record."""
    command = _guess_command(argv, parser)
    timeout_ms = getattr(ctx, "job_timeout_ms", None)
    try:
        record = jobs.spawn(argv, label=command, timeout_ms=timeout_ms)
    except OSError as exc:
        error = JyError(
            "INTERNAL", f"could not start the background job: {exc}", details={}
        )
        return envelope.write(
            _payload(command, False, "unknown", None, error, ctx, started)
        )
    data = {
        "job": jobs.annotate(record),
        "async": True,
        "poll": [
            f"jy jobs status {record['id']}",
            f"jy jobs wait {record['id']}",
        ],
    }
    return envelope.write(_payload(command, True, "executed", data, None, ctx, started))


def main(argv=None):
    started = time.monotonic()
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code in (0, None):
            return 0
        return envelope.write(
            _payload(
                _guess_command(argv, parser),
                False,
                "unknown",
                None,
                JyError("VALIDATION", "invalid command line; usage was written to stderr"),
                None,
                started,
            )
        )

    job_id = os.environ.get(jobs.JOB_ENV)
    ctx = commands.Context(verbose=bool(getattr(args, "verbose", False)))
    ctx.registry = getattr(parser, "jy_problems", {})
    ctx.job_id = job_id
    timeout_ms = getattr(args, "timeout_ms", None)
    if isinstance(timeout_ms, int) and timeout_ms > 0:
        ctx.job_timeout_ms = timeout_ms

    if _wants_async(args) and not job_id:
        return _spawn_async(parser, argv, ctx, started)

    def finish(payload):
        if job_id:
            jobs.record_result(job_id, payload, envelope.exit_code(payload))
        return envelope.write(payload)

    if job_id:
        jobs.mark_started(job_id)

    try:
        outcome, data = args.handler(ctx, args)
    except JyError as error:
        return finish(_payload(args.command, False, "unknown", None, error, ctx, started))
    except KeyboardInterrupt:
        return finish(
            _payload(
                args.command,
                False,
                "unknown",
                None,
                JyError("INTERNAL", "interrupted"),
                ctx,
                started,
            )
        )
    except Exception as exc:  # noqa: BLE001 - last-resort envelope, never a traceback on stdout
        details = {"type": type(exc).__name__}
        if ctx.verbose:
            details["traceback"] = traceback.format_exc()
        return finish(
            _payload(
                args.command,
                False,
                "unknown",
                None,
                JyError(
                    "INTERNAL",
                    f"unexpected error: {type(exc).__name__}: {exc}",
                    details=details,
                ),
                ctx,
                started,
            )
        )

    return finish(_payload(args.command, True, outcome, data, None, ctx, started))


if __name__ == "__main__":  # allows `python -m jy_cli.cli` for detached job children
    raise SystemExit(main())
