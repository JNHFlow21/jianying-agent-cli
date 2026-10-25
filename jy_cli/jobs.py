"""Background jobs and the jy cache directory.

``jy render --async`` (and any verb given ``--async``) spawns a detached child
that runs the same command; the parent returns a job id immediately.  Job state
lives as one JSON file per job under ``$JY_CACHE_DIR/jobs`` (default
``~/.cache/jy``) so ``jy jobs list|status|wait|kill`` — and later batches — can
read it without a daemon.

Also owns the scratch-draft area used by the 剪映 engine path, and the sweeps
behind ``jy clean``: rendered scratch drafts must not accumulate.

No daemon, no sockets, no network: a job is a child process plus a JSON file.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .envelope import JyError

SCHEMA = "jy.job.v1"
DRAFT_MARKER = ".jy-draft.json"
TERMINAL_STATES = ("succeeded", "failed", "killed")

CACHE_ENV = "JY_CACHE_DIR"
JOB_ENV = "JY_JOB_ID"


# ---------------------------------------------------------------------------
# locations


def cache_root():
    override = os.environ.get(CACHE_ENV)
    base = Path(override).expanduser() if override else Path.home() / ".cache" / "jy"
    return base


def jobs_dir():
    return cache_root() / "jobs"


def drafts_dir():
    return cache_root() / "drafts"


def ensure_dirs():
    for path in (jobs_dir(), drafts_dir()):
        path.mkdir(parents=True, exist_ok=True)
    return cache_root()


# ---------------------------------------------------------------------------
# records


def job_path(job_id):
    if not job_id or any(sep in job_id for sep in ("/", "\\", "..")):
        raise JyError("VALIDATION", f"invalid job id: {job_id!r}")
    return jobs_dir() / f"{job_id}.json"


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
    os.replace(temporary, path)


def write_record(record):
    record["updated_at"] = _now()
    _write_json(job_path(record["id"]), record)
    return record


def read_job(job_id):
    path = job_path(job_id)
    if not path.is_file():
        raise JyError(
            "NOT_FOUND",
            f"job not found: {job_id}",
            details={"jobs_dir": str(jobs_dir())},
        )
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JyError(
            "INTERNAL", f"job record unreadable: {exc}", details={"path": str(path)}
        ) from exc


def list_jobs(limit=None):
    directory = jobs_dir()
    if not directory.is_dir():
        return []
    records = []
    for path in directory.glob("*.json"):
        try:
            record = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(record, dict) and record.get("id"):
            records.append(annotate(record))
    records.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return records[:limit] if limit else records


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def new_id():
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"j-{stamp}-{uuid.uuid4().hex[:4]}"


def alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False
    return True


def annotate(record):
    """Add the derived fields every consumer wants (never rewrites the file)."""
    record = dict(record)
    active = record.get("state") in ("queued", "running")
    record["alive"] = alive(record.get("pid")) if active else False
    record["stale"] = bool(active and not record["alive"])
    return record


# ---------------------------------------------------------------------------
# spawning


def _launcher():
    """How to re-enter the CLI for a child process.

    ``bin/jy`` has no ``.py`` suffix, so trust any existing script file with the
    current interpreter (a shebang line is just a comment to Python); only fall
    back to ``-m`` when there is no script to run.
    """
    script = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if script and os.path.isfile(script):
        return [sys.executable, script]
    return [sys.executable, "-m", "jy_cli.cli"]


def spawn(argv, *, label="jy", timeout_ms=None, cwd=None):
    """Run ``argv`` detached; return the job record (state=queued)."""
    ensure_dirs()
    job_id = new_id()
    log_path = jobs_dir() / f"{job_id}.log"
    command_argv = [token for token in argv if token != "--async"]
    child = _launcher() + command_argv
    environment = dict(os.environ)
    environment[JOB_ENV] = job_id
    with open(log_path, "wb") as log:
        process = subprocess.Popen(
            child,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env=environment,
            cwd=cwd,
            start_new_session=True,
            close_fds=True,
        )
    record = {
        "schema": SCHEMA,
        "id": job_id,
        "command": label,
        "argv": command_argv,
        "state": "queued",
        "pid": process.pid,
        "created_at": _now(),
        "started_at": None,
        "finished_at": None,
        "timeout_ms": timeout_ms,
        "output": None,
        "log": str(log_path),
        "result": None,
        "exit_code": None,
        "error": None,
    }
    return write_record(record)


def mark_started(job_id):
    """Called by the child: flip the record to running."""
    try:
        record = read_job(job_id)
    except JyError:
        return None
    if record.get("state") in TERMINAL_STATES:
        return record
    record["state"] = "running"
    record["pid"] = os.getpid()
    record["started_at"] = record.get("started_at") or _now()
    return write_record(record)


def record_result(job_id, payload, exit_code):
    """Called by the child once it has its final jy.v1 payload."""
    try:
        record = read_job(job_id)
    except JyError:
        return None
    ok = bool(payload.get("ok"))
    data = payload.get("data") or {}
    output = data.get("output") if isinstance(data, dict) else None
    record.update(
        {
            "state": "succeeded" if ok else "failed",
            "finished_at": _now(),
            "exit_code": exit_code,
            "result": payload,
            "output": (output or {}).get("path") if isinstance(output, dict) else None,
            "error": payload.get("error"),
        }
    )
    return write_record(record)


def kill_job(job_id):
    """SIGTERM then SIGKILL the job's process group; mark the record killed."""
    record = read_job(job_id)
    state = record.get("state")
    if state in TERMINAL_STATES:
        return dict(annotate(record), killed=False, reason=f"job already {state}")
    pid = int(record.get("pid") or 0)
    if not alive(pid):
        record.update(
            {
                "state": "killed",
                "finished_at": _now(),
                "error": {
                    "code": "UNAVAILABLE",
                    "message": "job process was already gone",
                    "retriable": False,
                    "details": {},
                },
            }
        )
        return dict(write_record(record), killed=False, reason="process already gone")
    terminations = 0
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                break
        terminations += 1
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if not alive(pid):
                break
            time.sleep(0.05)
        if not alive(pid):
            break
    record.update(
        {
            "state": "killed",
            "finished_at": _now(),
            "exit_code": -signal.SIGTERM,
            "error": {
                "code": "UNAVAILABLE",
                "message": "job killed by jy (a 剪映 render already started may still finish inside 剪映)",
                "retriable": True,
                "details": {"signals": terminations},
            },
        }
    )
    return dict(write_record(record), killed=True, reason="terminated")


def wait_for(job_id, timeout_ms):
    """Poll until the job reaches a terminal state; returns (record, timed_out)."""
    deadline = time.monotonic() + max(0.0, timeout_ms / 1000.0)
    while True:
        record = annotate(read_job(job_id))
        if record.get("state") in TERMINAL_STATES:
            return record, False
        if record.get("stale"):
            return record, False
        if time.monotonic() >= deadline:
            return record, True
        time.sleep(0.1)


# ---------------------------------------------------------------------------
# render lock (剪映 film-export is single-instance)


RENDER_LOCK = "render.lock"


class RenderLock:
    """Cross-process exclusive lock around one 剪映 film-export.

    剪映's native export window is single-instance: two overlapping exports wedge
    the app (observed as native_export_busy / native_export_window_close_timeout
    with the process spinning). Every jy render takes this lock, so a second render
    waits and then fails actionably instead of piling on.
    """

    def __init__(self, timeout_ms=30_000):
        self.timeout_ms = max(0, int(timeout_ms))
        self.path = cache_root() / RENDER_LOCK
        self.waited_ms = 0
        self._handle = None

    def acquire(self):
        import fcntl

        ensure_dirs()
        started = time.monotonic()
        deadline = started + self.timeout_ms / 1000.0
        handle = open(self.path, "a+", encoding="utf-8")
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    holder = ""
                    try:
                        holder = self.path.read_text("utf-8").strip()
                    except OSError:
                        pass
                    handle.close()
                    self.waited_ms = int(round((time.monotonic() - started) * 1000))
                    raise JyError(
                        "UNAVAILABLE",
                        "另一个 jy render 正在占用剪映的 film-export（剪映的导出窗口是单实例的，"
                        "并发会让它卡死）。等它结束再试，或调大 --lock-wait-ms。",
                        retriable=True,
                        details={
                            "lock": str(self.path),
                            "holder": holder or None,
                            "waited_ms": self.waited_ms,
                        },
                    ) from None
                time.sleep(0.1)
        self.waited_ms = int(round((time.monotonic() - started) * 1000))
        self._handle = handle
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"pid={os.getpid()} since={_now()}\n")
            handle.flush()
        except OSError:
            pass
        return {"path": str(self.path), "waited_ms": self.waited_ms}

    def release(self):
        if not self._handle:
            return
        import fcntl

        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._handle.close()
        except OSError:
            pass
        self._handle = None


# ---------------------------------------------------------------------------
# scratch drafts (剪映 engine path)


def is_scratch_draft(path):
    """True only for jy-authored drafts inside the cache (never user folders)."""
    candidate = Path(path).expanduser().resolve()
    root = drafts_dir().resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return (candidate / DRAFT_MARKER).is_file()


def mark_scratch_draft(path, label=None):
    target = Path(path).expanduser().resolve()
    _write_json(
        target / DRAFT_MARKER,
        {
            "schema": "jy.draft.v1",
            "author": "jy",
            "label": label,
            "created_at": _now(),
        },
    )
    return target


def draft_report(path):
    candidate = Path(path).expanduser().resolve()
    marker = candidate / DRAFT_MARKER
    if not marker.is_file():
        return {"scratch": False, "path": str(candidate)}
    try:
        info = json.loads(marker.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        info = {}
    return {"scratch": is_scratch_draft(candidate), "path": str(candidate), "marker": info}


def remove_scratch_draft(path):
    """Delete a jy scratch draft; refuses anything outside the cache area."""
    if not is_scratch_draft(path):
        return {"removed": False, "reason": "not a jy scratch draft", "path": str(path)}
    target = Path(path).expanduser().resolve()
    files = 0
    size = 0
    for child in sorted(target.rglob("*"), reverse=True):
        try:
            if child.is_file():
                size += child.stat().st_size
                child.unlink()
                files += 1
            elif child.is_dir():
                child.rmdir()
        except OSError:
            pass
    try:
        target.rmdir()
    except OSError:
        pass
    return {
        "removed": not target.exists(),
        "path": str(target),
        "files": files,
        "freed_bytes": size,
    }


# ---------------------------------------------------------------------------
# staging + asset box (leftovers, swept by `jy clean --staging/--assets`)


def staging_root():
    override = os.environ.get("JY_STAGING_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Movies" / "jy-staging"


def sweep_staging(*, dry_run=False, older_than_hours=24.0):
    """Remove staging hash dirs older than the cutoff (draft build copies clips here)."""
    root = staging_root()
    cutoff = max(0.0, float(older_than_hours)) * 3600.0
    report = {"root": str(root), "dry_run": bool(dry_run), "removed": [], "kept": 0,
              "freed_bytes": 0}
    if not root.is_dir():
        return report
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if not _older_than(child, cutoff):
            report["kept"] += 1
            continue
        size = sum(item.stat().st_size for item in child.rglob("*") if item.is_file())
        report["removed"].append({"kind": "staging", "path": str(child), "bytes": size})
        report["freed_bytes"] += size
        if not dry_run:
            import shutil

            shutil.rmtree(child, ignore_errors=True)
    return report


# ---------------------------------------------------------------------------
# clean


def _older_than(path, seconds):
    try:
        return (time.time() - path.stat().st_mtime) > seconds
    except OSError:
        return False


def sweep(*, dry_run=False, older_than_hours=24.0, jobs=True, drafts=True):
    """Remove finished job records + jy scratch drafts older than the cutoff."""
    cutoff = max(0.0, float(older_than_hours)) * 3600.0
    report = {
        "cache_root": str(cache_root()),
        "dry_run": bool(dry_run),
        "older_than_hours": float(older_than_hours),
        "removed": [],
        "kept": 0,
        "freed_bytes": 0,
    }
    if jobs:
        directory = jobs_dir()
        if directory.is_dir():
            for path in sorted(directory.glob("*.json")):
                try:
                    record = json.loads(path.read_text("utf-8"))
                except (OSError, json.JSONDecodeError):
                    record = {}
                state = record.get("state")
                if state == "running" and alive(record.get("pid")):
                    report["kept"] += 1
                    continue
                if state not in TERMINAL_STATES and path.name.endswith(".json"):
                    # Unfinished but orphaned record (child died before writing state).
                    if alive(record.get("pid")):
                        report["kept"] += 1
                        continue
                if not _older_than(path, cutoff):
                    report["kept"] += 1
                    continue
                log_path = directory / f"{path.stem}.log"
                size = 0
                for candidate in (path, log_path):
                    try:
                        size += candidate.stat().st_size
                    except OSError:
                        pass
                report["removed"].append(
                    {"kind": "job", "path": str(path), "bytes": size, "id": path.stem}
                )
                report["freed_bytes"] += size
                if not dry_run:
                    path.unlink(missing_ok=True)
                    log_path.unlink(missing_ok=True)
    if drafts:
        directory = drafts_dir()
        if directory.is_dir():
            for child in sorted(directory.iterdir()):
                if not child.is_dir():
                    continue
                if not is_scratch_draft(child):
                    report["kept"] += 1
                    continue
                if not _older_than(child, cutoff):
                    report["kept"] += 1
                    continue
                size = sum(
                    item.stat().st_size for item in child.rglob("*") if item.is_file()
                )
                report["removed"].append(
                    {"kind": "draft", "path": str(child), "bytes": size}
                )
                report["freed_bytes"] += size
                if not dry_run:
                    remove_scratch_draft(child)
    return report


def jobs_state(limit=5):
    """Compact job summary for ``jy doctor``."""
    records = list_jobs(limit=limit)
    return {
        "cache_root": str(cache_root()),
        "jobs_dir": str(jobs_dir()),
        "drafts_dir": str(drafts_dir()),
        "count": len(list_jobs()),
        "recent": [
            {
                "id": record.get("id"),
                "command": record.get("command"),
                "state": record.get("state"),
                "alive": record.get("alive"),
                "stale": record.get("stale"),
                "created_at": record.get("created_at"),
            }
            for record in records
        ],
    }
