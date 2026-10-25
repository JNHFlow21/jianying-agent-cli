"""Background-job control and cache maintenance: ``jy jobs …`` and ``jy clean``."""

from __future__ import annotations

from .. import jobs
from ..envelope import JyError

TERMINAL = ", ".join(f"`{state}`" for state in jobs.TERMINAL_STATES)


def _job_or_error(job_id):
    return jobs.annotate(jobs.read_job(job_id))


def jobs_list(ctx, args):
    records = jobs.list_jobs(limit=args.limit)
    if args.state:
        records = [record for record in records if record.get("state") == args.state]
    running = sum(1 for record in records if record.get("state") == "running")
    return "readonly", {
        "cache_root": str(jobs.cache_root()),
        "jobs_dir": str(jobs.jobs_dir()),
        "count": len(records),
        "running": running,
        "jobs": records,
    }


def jobs_status(ctx, args):
    record = _job_or_error(args.job_id)
    return "readonly", {"job": record}


def jobs_wait(ctx, args):
    timeout_ms = int(args.timeout_ms)
    if timeout_ms < 1000:
        raise JyError("VALIDATION", "--timeout-ms 至少 1000")
    record, timed_out = jobs.wait_for(args.job_id, timeout_ms)
    if record.get("stale"):
        raise JyError(
            "UNAVAILABLE",
            f"任务 {args.job_id} 的子进程已经退出但没有写回结果（可能启动失败）。"
            f"日志：{record.get('log')}",
            retriable=True,
            details={"job": record},
        )
    if timed_out:
        raise JyError(
            "UNAVAILABLE",
            f"任务 {args.job_id} 在 {timeout_ms}ms 内没有结束（仍在 {record.get('state')}）。"
            "可以继续用 `jy jobs wait/status` 观察，或用 `jy jobs kill` 终止。",
            retriable=True,
            details={"job": record},
        )
    result = record.get("result") or {}
    if result.get("ok"):
        return "readonly", {
            "job": record,
            "result": result,
            "output": (result.get("data") or {}).get("output"),
        }
    error = record.get("error") or {
        "code": "INTERNAL",
        "message": f"job ended in state {record.get('state')} without a result",
        "retriable": False,
        "details": {},
    }
    raise JyError(
        error.get("code") or "INTERNAL",
        f"任务 {args.job_id} 失败：{error.get('message')}",
        retriable=bool(error.get("retriable")),
        details={"job": record, "child_error": error},
    )


def jobs_kill(ctx, args):
    record = jobs.kill_job(args.job_id)
    ctx.warnings.append(
        "如果子进程已经把导出交给剪映，剪映里的渲染可能仍会继续并留下输出文件；"
        "jy 只保证杀死自己的子进程。"
    )
    outcome = "executed" if record.get("killed") else "noop"
    return outcome, {"job": record}


def clean(ctx, args):
    if args.jobs_only and args.drafts_only:
        raise JyError("VALIDATION", "--jobs-only 与 --drafts-only 只能二选一")
    if args.staging and (args.jobs_only or args.drafts_only):
        raise JyError("VALIDATION", "--staging 不能和 --jobs-only/--drafts-only 一起用")
    hours = float(args.older_than_hours)
    if hours < 0:
        raise JyError("VALIDATION", "--older-than-hours 必须 >= 0")
    # Cache sweeps keep their historical default (delete unless --dry-run). The
    # user-visible ~/Movies areas are dry-run unless --apply, so a typo cannot wipe
    # staged media.
    extra_areas = bool(args.staging or args.assets or args.all)
    dry_run = bool(args.dry_run) or (extra_areas and not args.apply)
    if extra_areas:
        # extra areas are opt-in: they live under ~/Movies (user-visible), not the cache
        report = {"cache_root": str(jobs.cache_root()), "dry_run": dry_run,
                  "older_than_hours": hours, "removed": [], "kept": 0, "freed_bytes": 0}
        if args.staging or args.all:
            staging = jobs.sweep_staging(dry_run=dry_run, older_than_hours=hours)
            report["staging"] = staging
            report["removed"].extend(staging["removed"])
            report["kept"] += staging["kept"]
            report["freed_bytes"] += staging["freed_bytes"]
        if args.assets or args.all:
            from . import assets as assets_verbs

            assets_report = assets_verbs.sweep_assets(
                older_than_hours=hours, dry_run=dry_run
            )
            report["assets"] = assets_report
            report["removed"].extend(assets_report["removed"])
            report["kept"] += assets_report["kept"]
            report["freed_bytes"] += assets_report["freed_bytes"]
    else:
        report = jobs.sweep(
            dry_run=dry_run,
            older_than_hours=hours,
            jobs=not args.drafts_only,
            drafts=not args.jobs_only,
        )
    removed = report["removed"]
    report["summary"] = {
        "removed": len(removed),
        "freed_bytes": report["freed_bytes"],
        "kept": report["kept"],
        "dry_run": report["dry_run"],
    }
    if dry_run:
        ctx.warnings.append("--dry-run：只报告，未删除任何文件")
    outcome = "executed" if removed and not dry_run else "noop"
    if not removed:
        ctx.warnings.append(
            f"没有超过 {hours:g} 小时的 jy 临时草稿 / 已结束任务可清理"
        )
    return outcome, report


def register(r):
    group = r.parser_for(
        "jobs",
        help="inspect and control background jobs started with --async",
        description="后台任务：`jy <verb> --async` 会立刻返回 job id；这里查询/等待/终止。"
        "状态是 ~/.cache/jy/jobs 下的 JSON（可用 $JY_CACHE_DIR 覆盖），没有守护进程。",
        group=True,
    )
    sub = group.add_subparsers(
        dest="jobs_action", required=True, parser_class=r.parser_class, metavar="action"
    )

    listing = sub.add_parser("list", parents=[r.verbose_parent], help="list recent jobs")
    listing.add_argument("--limit", type=int, default=20, metavar="N")
    listing.add_argument(
        "--state",
        choices=("queued", "running") + jobs.TERMINAL_STATES,
        help="only show jobs in this state",
    )
    r.bind(listing, "jobs list", jobs_list)

    status = sub.add_parser("status", parents=[r.verbose_parent], help="one job's record")
    status.add_argument("job_id", metavar="job-id")
    r.bind(status, "jobs status", jobs_status)

    wait = sub.add_parser(
        "wait", parents=[r.verbose_parent], help=f"block until a job is {TERMINAL}"
    )
    wait.add_argument("job_id", metavar="job-id")
    wait.add_argument(
        "--timeout-ms", type=int, default=600_000, metavar="MS", help="wait limit"
    )
    r.bind(wait, "jobs wait", jobs_wait)

    kill = sub.add_parser(
        "kill", parents=[r.verbose_parent], help="terminate a running job (SIGTERM→SIGKILL)"
    )
    kill.add_argument("job_id", metavar="job-id")
    r.bind(kill, "jobs kill", jobs_kill)

    sweep = r.add(
        "clean",
        parents=[r.verbose_parent],
        help="remove jy scratch drafts and finished job records from the cache",
        description="清理 jy 自己的缓存：临时草稿（~/.cache/jy/drafts 里带 .jy-draft.json 的）"
        "和已结束的任务记录/日志。默认只删 24h 以前的；--dry-run 只报告。",
        handler=clean,
    )
    sweep.add_argument("--dry-run", action="store_true", help="report only, delete nothing")
    sweep.add_argument(
        "--older-than-hours",
        type=float,
        default=24.0,
        metavar="H",
        help="only remove entries older than H hours (default 24)",
    )
    sweep.add_argument("--jobs-only", action="store_true", help="only sweep job records")
    sweep.add_argument("--drafts-only", action="store_true", help="only sweep scratch drafts")
    sweep.add_argument(
        "--staging",
        action="store_true",
        help="sweep draft-build staging dirs (~/Movies/jy-staging or $JY_STAGING_DIR)",
    )
    sweep.add_argument(
        "--assets",
        action="store_true",
        help="sweep the jy asset box (~/Movies/jy-assets or $JY_ASSET_DIR)",
    )
    sweep.add_argument(
        "--all", action="store_true", help="sweep staging + assets as well as the cache"
    )
    sweep.add_argument(
        "--apply",
        action="store_true",
        help="really delete in --staging/--assets/--all areas (those default to dry-run)",
    )
