"""`cohort dashboard` — a local web lens over the office, plus human-gated actions.

Serves a single-file UI from the stdlib HTTP server (no new dependencies, no
daemon: it runs in the foreground and dies with Ctrl-C). Reads are the same
read-only aggregates the CLI exposes (`status`, signals, proposals, sessions);
writes are the same human-gated command functions the CLI calls (`feedback`,
`remove-specialist`, `propose-improvement`, `snapshot`) — the dashboard adds no
new write paths and never touches canonical.

Hardening (the server is loopback-only but shares the machine with browsers):
- binds 127.0.0.1 only, never 0.0.0.0;
- every ``/api`` call must carry the per-launch random token (embedded in the
  served page), so a hostile web page cannot drive the API cross-origin;
- the Host header must be loopback, which defeats DNS-rebinding token theft;
- no CORS headers are ever emitted.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Optional

from . import __version__
from .compile import RENDERERS
from .improve import (
    FeedbackError,
    aggregate_signals,
    do_feedback,
    do_propose_improvement,
)
from .install_model import CohortPaths
from .loader import load_artifact
from .parity import check_parity
from .project import do_snapshot, find_repo_root
from .source import SourceUnresolved, resolve_source
from .specialists import RemoveSpecialistError, do_remove_specialist
from .status import do_status
from .update import update_status

_UPDATE_TTL_SECONDS = 900  # update_status fetches the network; don't per-poll it
_RECENT_LIMIT = 10


def _resolve_source_lenient(home: Path) -> Optional[Path]:
    """The source clone, via the normal resolution or the installed symlink."""
    try:
        return resolve_source(None)
    except SourceUnresolved:
        canonical = CohortPaths.for_global(home).canonical
        if canonical.is_symlink() and canonical.exists():
            return canonical.resolve().parent
        return None


class _UpdateCache:
    """TTL cache around ``update_status`` (it fetches; polls must stay fast)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value: Optional[dict] = None
        self._at = 0.0

    def get(self, source: Optional[Path], home: Path) -> dict:
        if source is None:
            return {"available": False, "upstream": ""}
        with self._lock:
            if self._value is None or time.monotonic() - self._at > _UPDATE_TTL_SECONDS:
                self._value = update_status(source, home)
                self._at = time.monotonic()
            return self._value


def _proposal_entry(path: Path) -> dict[str, Any]:
    fm = load_artifact(path).frontmatter or {}
    return {
        "file": path.name,
        "kind": fm.get("kind", "unknown"),
        "created_at": fm.get("generated_at") or fm.get("requested_at"),
        "upstream_candidate": fm.get("upstream_candidate", False),
        "submitted_at": fm.get("submitted_at"),
        "submitted_upstream": fm.get("submitted_upstream"),
    }


def _feedback_entry(path: Path) -> dict[str, Any]:
    loaded = load_artifact(path)
    fm = loaded.frontmatter or {}
    return {
        "file": path.name,
        "rating": fm.get("rating"),
        "agent": fm.get("agent"),
        "command": fm.get("command"),
        "timestamp": fm.get("timestamp"),
        "note": (loaded.body or "").strip()[:200],
    }


def _session_entry(path: Path) -> dict[str, Any]:
    fm = load_artifact(path).frontmatter or {}
    return {
        "file": path.name,
        "timestamp": fm.get("timestamp"),
        "author": fm.get("author"),
        "branch": fm.get("branch"),
    }


def _recent(directory: Path, render: Callable[[Path], dict]) -> list[dict[str, Any]]:
    if not directory.exists():
        return []
    files = sorted(directory.glob("*.md"), reverse=True)[:_RECENT_LIMIT]
    return [render(p) for p in files]


def collect_state(home: Path, cwd: Path, update_cache: Optional[_UpdateCache] = None) -> dict[str, Any]:
    """Everything the dashboard shows, as one JSON-safe dict. Read-only."""
    state = do_status(home, cwd)
    state["version"] = __version__
    source = _resolve_source_lenient(home)
    state["global"]["update"] = (update_cache or _UpdateCache()).get(source, home)
    parity = {}
    if source is not None:
        for ide in state["global"]["ides"]:
            if ide in RENDERERS:
                parity[ide] = check_parity(source, ide, RENDERERS).to_dict()
    state["global"]["parity"] = parity

    if "project" in state:
        ppaths = CohortPaths.for_project(Path(state["project"]["repo"]))
        state["project"]["signals"] = aggregate_signals(ppaths)
        state["project"]["proposals"] = _recent(ppaths.cohort_home / "proposals", _proposal_entry)
        state["project"]["feedback"] = _recent(ppaths.cohort_home / "feedback", _feedback_entry)
        state["project"]["sessions"] = _recent(ppaths.cohort_home / "sessions", _session_entry)
    return state


class ActionError(Exception):
    """A refused dashboard action (bad input or a command-level refusal)."""


def run_action(home: Path, cwd: Path, action: str, args: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one human-initiated action to the same function the CLI uses.

    Only this fixed allowlist exists; there is deliberately no action that edits
    canonical, merges, or pushes — those stay in the CLI (`submit-proposals`).
    """
    repo = find_repo_root(cwd)
    try:
        if action == "feedback":
            return do_feedback(
                repo, str(args.get("rating", "")), args.get("agent") or None,
                args.get("command") or None, str(args.get("note", "")), dry_run=False,
            )
        if action == "remove-specialist":
            return do_remove_specialist(repo, home, str(args.get("name", "")), dry_run=False)
        if action == "propose-improvement":
            return do_propose_improvement(repo, dry_run=False)
        if action == "snapshot":
            return do_snapshot(repo, dry_run=False, refresh_index=True)
    except (FeedbackError, RemoveSpecialistError) as exc:
        raise ActionError(str(exc))
    raise ActionError(f"unknown action {action!r}")


_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def _host_is_loopback(host: str) -> bool:
    bare = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
    return bare in _LOOPBACK_HOSTS


def load_page() -> str:
    return (resources.files("cohort") / "dashboard.html").read_text(encoding="utf-8")


class DashboardHandler(BaseHTTPRequestHandler):
    """Routes: GET / (the page), GET /api/state, POST /api/action."""

    server: "DashboardServer"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass  # request logging would drown the terminal; errors surface as responses

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if content_type.startswith("text/html"):
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                "connect-src 'self'; img-src data:",
            )
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    def _guard(self) -> bool:
        """Loopback Host + per-launch token on every /api call."""
        if not _host_is_loopback(self.headers.get("Host", "")):
            self._send_json(403, {"error": "forbidden host"})
            return False
        token = self.headers.get("X-Cohort-Token", "")
        if not hmac.compare_digest(token, self.server.token):
            self._send_json(401, {"error": "missing or bad token"})
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path == "/":
            if not _host_is_loopback(self.headers.get("Host", "")):
                self._send_json(403, {"error": "forbidden host"})
                return
            page = load_page().replace("__COHORT_TOKEN__", self.server.token)
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            if not self._guard():
                return
            state = collect_state(self.server.home, self.server.cwd, self.server.update_cache)
            self._send_json(200, state)
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        if self.path != "/api/action":
            self._send_json(404, {"error": "not found"})
            return
        if not self._guard():
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 65536)
            body = json.loads(self.rfile.read(length) or b"{}")
            action = str(body.get("action", ""))
            args = body.get("args") or {}
            if not isinstance(args, dict):
                raise ActionError("args must be an object")
            with self.server.action_lock:  # mutating commands never run concurrently
                report = run_action(self.server.home, self.server.cwd, action, args)
            self._send_json(200, report)
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"error": "malformed request body"})
        except ActionError as exc:
            self._send_json(400, {"error": str(exc)})


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    # HTTPServer turns SO_REUSEADDR on; Windows interprets that flag as "let a
    # second socket bind this port", so a port collision would silently start a
    # second server instead of failing. Keep it only where it means fast-rebind.
    allow_reuse_address = os.name != "nt"

    def __init__(self, home: Path, cwd: Path, port: int) -> None:
        super().__init__(("127.0.0.1", port), DashboardHandler)
        self.home = home
        self.cwd = cwd
        self.token = secrets.token_urlsafe(32)
        self.update_cache = _UpdateCache()
        self.action_lock = threading.Lock()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/"


def do_dashboard(home: Path, cwd: Path, port: int, open_browser: bool) -> DashboardServer:
    """Start the dashboard server (caller owns serve_forever / shutdown)."""
    server = DashboardServer(home, cwd, port)
    if open_browser:
        import webbrowser

        threading.Timer(0.3, webbrowser.open, args=[server.url]).start()
    return server
