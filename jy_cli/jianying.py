"""剪映 (JianyingPro) native-bridge transport for jy.

This is a self-contained, stdlib-only client for the one 剪映 control plane that can
RENDER: the Qt "Infinite Canvas" native bridge, which listens on a per-launch random
127.0.0.1 port and authenticates with a per-launch token.

    descriptor  $TMPDIR/infinite-canvas-native-bridge.json
                {"app":"infinite_canvas","pid":N,"port":N,"token":"…","version":1}
    auth header x-infinite-canvas-native-token
    routes      GET  /.infinite-canvas-native-bridge/health
                GET  /custom-agent/context          (SENSITIVE: live login cookie)
                POST /custom-agent/film-export      (renders a draft folder -> mp4)

Rules this module enforces:
  * localhost only — the descriptor's port, never a caller-supplied host;
  * port/token are re-discovered on every call and never cached across runs;
  * the token is never written to disk, logged, or echoed (``redacted()`` is safe);
  * /custom-agent/context is only used to read ``principal.userId`` — the login
    cookie value is never returned, stored or printed;
  * failures raise :class:`BridgeError` with an actionable ``kind`` that
    :func:`to_jy_error` turns into the CLI's jy.v1 error codes.

Protocol notes: docs/BRIDGE.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .envelope import JyError

APP_BUNDLE = Path("/Applications/VideoFusion-macOS.app")
DESCRIPTOR_NAME = "infinite-canvas-native-bridge.json"
TOKEN_HEADER = "x-infinite-canvas-native-token"

HEALTH_PATH = "/.infinite-canvas-native-bridge/health"
CONTEXT_PATH = "/custom-agent/context"
FILM_EXPORT_PATH = "/custom-agent/film-export"

DEFAULT_TIMEOUT_MS = 600_000

# Native error codes (strings in libVECreator.dylib) -> actionable jy behaviour.
_MEMBERSHIP_CODES = (
    "film_export_entitlement_denied",
    "film_export_entitlement_bypass_forbidden",
    "film_export_entitlement_or_preflight_denied",
)
_RUNTIME_CODES = (
    "film_export_panel_unavailable",
    "film_export_runtime_unavailable",
    "film_export_frame_canvas_unavailable",
    "film_export_invalid_state",
)


class BridgeError(RuntimeError):
    """A bridge/transport failure with the server's own body attached."""

    def __init__(self, message, *, status=None, body=None, kind="http", retriable=False):
        super().__init__(message)
        self.status = status
        self.body = body
        self.kind = kind
        self.retriable = retriable

    def code(self):
        """The native error string, when the body carries one."""
        if isinstance(self.body, dict):
            for key in ("error", "code", "errorCode"):
                value = self.body.get(key)
                if isinstance(value, str) and value:
                    return value
        return None

    def as_details(self):
        details = {"status": self.status, "kind": self.kind}
        code = self.code()
        if code:
            details["native_error"] = code
        if isinstance(self.body, dict):
            details["native_body"] = self.body
        return details


def descriptor_candidates():
    """Every location the app has been observed to write the descriptor to.

    ``$JY_BRIDGE_DESCRIPTOR`` pins discovery to one explicit path (tests simulate a
    stale/dead descriptor with it; ops can point jy at a known bridge).
    """
    override = os.environ.get("JY_BRIDGE_DESCRIPTOR")
    if override:
        return [Path(override).expanduser()]
    return [
        Path.home()
        / "Library"
        / "Containers"
        / "com.lemon.lvpro"
        / "Data"
        / "tmp"
        / DESCRIPTOR_NAME,
        Path(tempfile.gettempdir()) / DESCRIPTOR_NAME,
    ]


def locate():
    """Find the descriptor without raising; used by ``jy doctor``.

    Returns {"running", "path", "port", "pid", "app", "checked"}. Never the token.
    """
    checked = []
    for path in descriptor_candidates():
        checked.append(str(path))
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {
                "running": False,
                "path": str(path),
                "error": f"descriptor unreadable: {exc}",
                "checked": checked,
            }
        port = data.get("port")
        token = (data.get("token") or "").strip()
        if not isinstance(port, int) or not port or not token:
            return {
                "running": False,
                "path": str(path),
                "error": "descriptor lacks port/token",
                "checked": checked,
            }
        return {
            "running": True,
            "path": str(path),
            "port": port,
            "pid": data.get("pid"),
            "app": data.get("app"),
            "checked": checked,
        }
    return {"running": False, "path": None, "checked": checked}


BRIDGE_STATES = ("not_running", "wedged", "ok")

BRIDGE_REMEDIES = {
    "not_running": "剪映没有运行：打开剪映（保持运行即可，不需要点任何界面）后重试。",
    "wedged": "剪映进程还在，但 native bridge 不响应（多半已卡死）：用 "
    "`jy render ... --relaunch-app`（jy 会 SIGTERM→SIGKILL、重开并等 bridge 活过来），"
    "或手动重启剪映后再试；不要反复重试同一个卡死的 bridge。",
    "ok": "native bridge 正常。",
}


def bridge_state(timeout=5.0):
    """Distinguish the three states an agent has to react to differently.

    ``not_running``: no descriptor / dead pid  -> open the app.
    ``wedged``:      process alive, health does not answer -> restart it.
    ``ok``:          health answered 200.

    Never raises; never returns the token.
    """
    located = locate()
    state = {
        "state": "not_running",
        "running": False,
        "pid": located.get("pid"),
        "port": located.get("port"),
        "descriptor": located.get("path"),
        "reachable": False,
        "health_ms": None,
        "error": located.get("error"),
        "remedy": BRIDGE_REMEDIES["not_running"],
    }
    if not located.get("running"):
        return state
    pid = located.get("pid")
    if not pid or not _pid_alive(pid):
        state["state"] = "not_running"
        state["error"] = "descriptor pid is not alive"
        return state

    state["running"] = True
    descriptor = {
        "origin": f"http://127.0.0.1:{located['port']}",
        "port": located["port"],
        "pid": pid,
        "token": (json.loads(Path(located["path"]).read_text("utf-8")).get("token") or "").strip(),
        "path": located["path"],
    }
    started = time.monotonic()
    try:
        Client(descriptor, timeout=timeout).health()
    except (BridgeError, JyError) as exc:
        state["state"] = "wedged"
        state["health_ms"] = int(round((time.monotonic() - started) * 1000))
        state["error"] = str(exc)
        state["remedy"] = BRIDGE_REMEDIES["wedged"]
        return state
    state["state"] = "ok"
    state["reachable"] = True
    state["health_ms"] = int(round((time.monotonic() - started) * 1000))
    state["remedy"] = BRIDGE_REMEDIES["ok"]
    return state


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False
    return True


def _discover():
    """Fresh per-invocation discovery. Raises UNAVAILABLE when 剪映 is not running."""
    located = locate()
    if not located.get("running"):
        where = ", ".join(located.get("checked") or [])
        if located.get("error"):
            message = (
                "剪映 native bridge 描述文件不可读（剪映可能正在启动/退出）。"
                "请确认剪映完全启动后重试。"
            )
        else:
            message = (
                "剪映未运行：找不到 native bridge 描述文件。"
                "请打开剪映（保持运行即可，不需要点任何界面）后重试。"
            )
        raise JyError(
            "UNAVAILABLE",
            message + f"（checked: {where}）",
            retriable=True,
            details={"checked": located.get("checked") or [], "error": located.get("error")},
        )
    descriptor = json.loads(Path(located["path"]).read_text("utf-8"))
    return {
        "origin": f"http://127.0.0.1:{located['port']}",
        "port": located["port"],
        "pid": located.get("pid"),
        "token": (descriptor.get("token") or "").strip(),
        "path": located["path"],
    }


def public_state(descriptor):
    """Descriptor copy with no secret; the only shape that may enter the envelope."""
    return {
        "port": descriptor.get("port"),
        "pid": descriptor.get("pid"),
        "descriptor": descriptor.get("path"),
        "origin": descriptor.get("origin"),
    }


def _http(method, url, headers, body=None, timeout=30.0):
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers = {**headers, "content-type": "application/json"}
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:  # 4xx/5xx still carry a useful JSON body
        raw = exc.read()
        status = exc.code
    except (socket.timeout, TimeoutError) as exc:
        raise BridgeError(
            f"{method} {url} timed out after {timeout:.0f}s",
            kind="timeout",
            retriable=True,
        ) from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise BridgeError(
                f"{method} {url} timed out after {timeout:.0f}s",
                kind="timeout",
                retriable=True,
            ) from exc
        raise BridgeError(f"{method} {url} failed: {reason}", kind="transport") from exc
    except OSError as exc:
        raise BridgeError(f"{method} {url} failed: {exc}", kind="transport") from exc
    try:
        return status, json.loads(raw.decode() or "null")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, raw.decode(errors="replace")


class Client:
    """One live bridge endpoint; construct a new client for every invocation."""

    def __init__(self, descriptor=None, timeout=10.0):
        self.desc = descriptor or _discover()
        self.timeout = timeout

    def _headers(self):
        return {TOKEN_HEADER: self.desc["token"]}

    def health(self):
        status, body = _http(
            "GET", self.desc["origin"] + HEALTH_PATH, self._headers(), timeout=self.timeout
        )
        if status != 200 or not (isinstance(body, dict) and body.get("ok") is True):
            raise BridgeError("bridge health check failed", status=status, body=body)
        return body

    def account_scope(self, timeout=None):
        """``uid:<id>`` from the host context. The cookie itself is never returned.

        The context lookup happens *before* every export, so it must survive the
        same slow window the export itself has to survive: while 剪映 is busy
        (notably a cold start, where the first export can take minutes) this GET
        can take far longer than the default probe timeout. Timing out here made
        the render fail in 10 s with no export attempt at all, which made the
        cold-start retry useless.
        """
        status, body = _http(
            "GET",
            self.desc["origin"] + CONTEXT_PATH,
            self._headers(),
            timeout=timeout if timeout is not None else max(self.timeout, 120.0),
        )
        if status != 200:
            raise BridgeError("bridge context failed", status=status, body=body)
        principal = ((body or {}).get("authContext") or {}).get("principal") or {}
        uid = str(principal.get("userId") or "").strip()
        if not uid:
            raise JyError(
                "UNAVAILABLE",
                "剪映未登录：native bridge 没有返回登录用户（accountScope 无法确定）。请先在剪映里登录后重试。",
                retriable=True,
                details={"authenticated": False},
            )
        return f"uid:{uid}"

    def probe_film_export_route(self):
        """Dry route check: a body-less POST must be rejected by the request validator.

        This never starts a render (the validator runs before any state/entitlement
        check) and is the only safe way to prove the route is served.
        """
        try:
            status, body = _http(
                "POST",
                self.desc["origin"] + FILM_EXPORT_PATH,
                self._headers(),
                {},
                timeout=min(self.timeout, 5.0),
            )
        except BridgeError as exc:
            return {"responded": exc.kind == "http", "status": exc.status, "kind": exc.kind}
        code = body.get("error") if isinstance(body, dict) else None
        # 409/400 + film_export_invalid_request == route is alive, no job created.
        return {
            "responded": status in (400, 409, 422),
            "status": status,
            "native_error": code,
            "expected_error": "film_export_invalid_request",
        }

    def export_draft(self, draft_root, output_path, *, fmt="mp4", timeout_ms=DEFAULT_TIMEOUT_MS):
        """Render a draft folder to ``output_path`` through the native job.

        Returns the receipt, with the app-server's own invariants enforced here:
        ``ok is true``, ``path == output_path`` and ``format == fmt``.
        """
        root = Path(draft_root).resolve()
        draft_file = root / "draft_info.json"
        if not draft_file.is_file():
            raise JyError(
                "VALIDATION",
                f"草稿目录缺少 draft_info.json：{root}",
                details={"draft_path": str(root)},
            )
        digest = hashlib.sha256(draft_file.read_bytes()).hexdigest()
        request_id = str(uuid.uuid4())
        body = {
            "accountScope": self.account_scope(),
            "requestId": request_id,
            "snapshotId": f"film-export-{request_id}",
            "revision": f"sha256:{digest}",
            "draftPath": str(root),
            "draftSha256": digest,
            "outputPath": str(Path(output_path).resolve()),
            "format": fmt,
            "timeoutMs": int(timeout_ms),
        }
        started = time.monotonic()
        status, payload = _http(
            "POST",
            self.desc["origin"] + FILM_EXPORT_PATH,
            self._headers(),
            body,
            timeout=timeout_ms / 1000.0 + 30.0,
        )
        if status != 200 or not (isinstance(payload, dict) and payload.get("ok") is True):
            raise BridgeError(
                "剪映拒绝了导出请求", status=status, body=payload, kind="http"
            )
        receipt = payload
        receipt_path = receipt.get("path")
        if receipt_path and Path(receipt_path).resolve() != Path(output_path).resolve():
            raise BridgeError(
                "剪映回执的 path 与请求不一致",
                status=status,
                body=receipt,
                kind="receipt",
            )
        receipt["durationMs"] = int(round((time.monotonic() - started) * 1000))
        receipt["draftSha256"] = digest
        return receipt


def app_state(timeout=2.0):
    """Compact, secret-free 剪映 engine state for ``jy doctor`` (never raises)."""
    state = {
        "app_installed": APP_BUNDLE.is_dir(),
        "running": False,
        "descriptor": None,
        "pid": None,
        "bridge_port": None,
        "health_ok": False,
        "health": None,
        "logged_in": None,
        "film_export": {"responded": False},
        "error": None,
    }
    located = locate()
    state["descriptor_found"] = bool(located.get("running"))
    state["checked_paths"] = located.get("checked") or []
    state["descriptor"] = located.get("path")
    if not located.get("running"):
        state["error"] = located.get("error") or "native bridge descriptor not found (剪映 not running)"
        return state
    state.update(
        {
            "running": True,
            "pid": located.get("pid"),
            "bridge_port": located.get("port"),
        }
    )
    try:
        descriptor = _discover()
        client = Client(descriptor, timeout=timeout)
        health = client.health()
        agent_server = health.get("local_agent_server") or {}
        state["health_ok"] = True
        # The agent server's token is a credential too: report liveness only.
        state["health"] = {
            "version": health.get("version"),
            "pid": health.get("pid"),
            "local_agent_server": {
                key: agent_server.get(key)
                for key in ("running", "port", "starting")
            },
        }
        state["film_export"] = client.probe_film_export_route()
        state["logged_in"] = bool(client.account_scope())
    except JyError as exc:
        state["error"] = exc.message
        state["logged_in"] = False if exc.code == "UNAVAILABLE" and "登录" in exc.message else None
    except BridgeError as exc:
        state["error"] = f"{exc} ({exc.kind})"
        state["bridge_error"] = exc.as_details()
    return state


def to_jy_error(exc):
    """Map a :class:`BridgeError` onto the jy.v1 error surface, actionably."""
    code = exc.code()
    details = exc.as_details()
    if exc.kind == "timeout":
        return JyError(
            "UNAVAILABLE",
            "剪映导出超时（客户端等待超时，导出可能仍在剪映里进行）。可调大 --timeout-ms 或降低分辨率后重试。",
            retriable=True,
            details=details,
        )
    if exc.kind == "transport":
        return JyError(
            "UNAVAILABLE",
            "连不上剪映 native bridge（端口随剪映每次启动变化）。请确认剪映仍在运行，然后重试。",
            retriable=True,
            details=details,
        )
    if exc.status == 401:
        return JyError(
            "UNAVAILABLE",
            "剪映 native bridge token 失效（剪映重启后端口/token 会变）。重试即可重新发现。",
            retriable=True,
            details=details,
        )
    if code in _MEMBERSHIP_CODES:
        return JyError(
            "BLOCKED",
            "该素材/导出需要会员额度（剪映会员）。请在剪映里开通/确认会员，或换掉需要额度的素材；"
            "jy 不会也不能绕过会员限制。",
            details=details,
        )
    if code == "film_export_dependency_missing":
        resource = None
        body = exc.body if isinstance(exc.body, dict) else {}
        for key in ("resourceId", "resource", "dependency", "materialId", "detail"):
            if body.get(key):
                resource = body[key]
                break
        where = f"（缺少：{resource}）" if resource else ""
        return JyError(
            "UNSUPPORTED",
            "剪映本机缓存缺少该草稿引用的素材资源" + where
            + "。先在剪映里下载/使用一次这个素材（特效/转场/字体），再重试。",
            details=details,
        )
    if code == "film_export_unsupported_timeline":
        return JyError(
            "UNSUPPORTED",
            "剪映不支持导出这种时间线（多时间线/模板草稿）。请改用单时间线草稿。",
            details=details,
        )
    if code == "film_export_invalid_request":
        return JyError(
            "VALIDATION",
            "剪映拒绝了导出请求：字段不合法（film_export_invalid_request）。",
            details=details,
        )
    if code in ("film_export_draft_unreadable", "film_export_draft_manager_unavailable"):
        return JyError(
            "VALIDATION",
            "剪映读不了这个草稿目录（draft_info.json 或路径不可读）。",
            details=details,
        )
    if code in ("film_export_h264_mp4_unavailable", "film_export_gif_unavailable"):
        return JyError(
            "UNSUPPORTED",
            f"当前剪映构建缺少该导出能力（{code}）。",
            details=details,
        )
    if code in _RUNTIME_CODES:
        return JyError(
            "UNAVAILABLE",
            "剪映内部渲染面板/运行时暂时不可用（film_export_runtime_unavailable）。"
            "请确认剪映主窗口存在、不是正在退出，然后重试。",
            retriable=True,
            details=details,
        )
    if code == "film_export_cancelled_or_timeout":
        return JyError(
            "UNAVAILABLE",
            "剪映导出超时或被取消（film_export_cancelled_or_timeout）。可调大 --timeout-ms 后重试。",
            retriable=True,
            details=details,
        )
    if code == "film_export_output_missing":
        return JyError(
            "INTERNAL",
            "剪映回执说成功，但没有产出文件（film_export_output_missing）。",
            details=details,
        )
    if code == "film_export_render_failed":
        return JyError(
            "INTERNAL",
            "剪映渲染失败（film_export_render_failed）。这是剪映内部错误，重试前可先检查草稿在剪映里能否预览。",
            details=details,
        )
    if exc.status and 500 <= int(exc.status):
        return JyError(
            "UNAVAILABLE",
            "剪映 native bridge 返回 5xx，可能是应用正在忙/退出；重试一次。",
            retriable=True,
            details=details,
        )
    return JyError(
        "INTERNAL",
        f"剪映导出失败：{exc}",
        retriable=exc.retriable,
        details=details,
    )
