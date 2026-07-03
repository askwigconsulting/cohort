"""`cohort dashboard` — a local web lens over the office, plus human-gated actions.

Serves a single-file UI from the stdlib HTTP server (no new dependencies, no
daemon: it runs in the foreground and dies with Ctrl-C). Reads are the same
read-only aggregates the CLI exposes (`status`, signals, proposals, sessions);
writes are the same human-gated command functions the CLI calls (`feedback`,
`add-/remove-specialist`, `propose-improvement`, `snapshot`, `init`, `update`,
`recompile`, roster subsets) — the dashboard adds no new write paths and never
edits the global source canonical. Submitting proposals as draft PRs stays in
the CLI.

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

from dataclasses import asdict

from . import __version__
from .compile import RENDERERS, CompileError, compile_ide, planned_dests, write_staging
from .executor import ClobberRefused
from .improve import (
    FeedbackError,
    ProposeError,
    aggregate_signals,
    do_feedback,
    do_propose_improvement,
)
from .install import UsageError, do_install
from .install_model import CohortPaths, resolve_mode
from .loader import load_artifact
from .manifest import load_manifest
from .office_setup import SetupError, canonical_agents, effective_roster, persist_roster
from .parity import check_parity
from .project import do_init, do_snapshot, find_repo_root
from .source import SourceUnresolved, resolve_source
from .specialists import (
    AddSpecialistError,
    RemoveSpecialistError,
    do_add_specialist,
    do_remove_specialist,
)
from .status import do_status
from .update import do_update, update_status

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


_UPDATE_UNKNOWN = {"available": False, "upstream": ""}


class _UpdateCache:
    """TTL cache around ``update_status``, refreshed off the request thread.

    ``update_status`` runs a ``git fetch`` (up to 8s), so it must never run while
    a request holds the lock — a poll would stall for the whole fetch. Instead a
    stale/empty ``get`` kicks a single background refresh and returns the last
    value (or the "unknown" placeholder on the very first call); the next poll
    picks up the result. The lock is only ever held for trivial dict swaps."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value: Optional[dict] = None
        self._at = 0.0
        self._refreshing = False

    def get(self, source: Optional[Path], home: Path) -> dict:
        if source is None:
            return dict(_UPDATE_UNKNOWN)
        with self._lock:
            fresh = self._value is not None and time.monotonic() - self._at <= _UPDATE_TTL_SECONDS
            value = self._value if self._value is not None else dict(_UPDATE_UNKNOWN)
            start_refresh = not fresh and not self._refreshing
            if start_refresh:
                self._refreshing = True
        if start_refresh:
            threading.Thread(
                target=self._refresh, args=(source, home), daemon=True
            ).start()
        return value

    def _refresh(self, source: Path, home: Path) -> None:
        try:
            result = update_status(source, home)  # never raises (its contract)
        except Exception:  # noqa: BLE001 - a refresh must never crash the daemon thread
            result = dict(_UPDATE_UNKNOWN)
        with self._lock:
            self._value = result
            self._at = time.monotonic()
            self._refreshing = False

    def invalidate(self) -> None:
        """Drop the cached value (e.g. right after a successful update action),
        so the next poll re-fetches instead of showing a stale behind-count."""
        with self._lock:
            self._value = None
            self._at = 0.0


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


def _agent_cards(agents_dir: Path) -> list[dict[str, Any]]:
    """Display metadata per agent file (read-only frontmatter peek)."""
    if not agents_dir.exists():
        return []
    cards = []
    for p in sorted(agents_dir.glob("*.md")):
        fm = load_artifact(p).frontmatter or {}
        cards.append({
            "name": p.stem,
            "display_name": fm.get("display_name", p.stem),
            "department": fm.get("department", ""),
            "description": fm.get("description", ""),
            "topology": fm.get("topology", "specialist"),
        })
    return cards


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

    # The full catalog (source canonical when reachable, else the installed one)
    # with an installed flag — the roster editor's working set.
    gpaths = CohortPaths.for_global(home)
    catalog_dir = (source / "canonical" / "agents") if source else (gpaths.canonical / "agents")
    installed = set(state["global"]["roster"]["names"])
    cards = _agent_cards(catalog_dir)
    for card in cards:
        card["installed"] = card["name"] in installed
        card["layer"] = "office"
    my_cards = _agent_cards(gpaths.my / "canonical" / "agents")
    for card in my_cards:
        card["installed"] = True  # my-layer agents always compile (#84)
        card["layer"] = "my"
    state["global"]["roster"]["agents"] = cards + my_cards

    if "project" in state:
        ppaths = CohortPaths.for_project(Path(state["project"]["repo"]))
        state["project"]["specialist_cards"] = _agent_cards(ppaths.canonical / "agents")
        state["project"]["signals"] = aggregate_signals(ppaths)
        state["project"]["proposals"] = _recent(ppaths.cohort_home / "proposals", _proposal_entry)
        state["project"]["feedback"] = _recent(ppaths.cohort_home / "feedback", _feedback_entry)
        state["project"]["sessions"] = _recent(ppaths.cohort_home / "sessions", _session_entry)
    return state


class ActionError(Exception):
    """A refused dashboard action (bad input or a command-level refusal)."""


def _require_source(home: Path) -> Path:
    source = _resolve_source_lenient(home)
    if source is None:
        raise ActionError("source clone not found; run `cohort relink` in a terminal")
    return source


def _recompile_claude(home: Path, source: Path, roster: Optional[list]) -> dict[str, Any]:
    """The same compile → stage → install path `cohort recompile --ide claude` runs.

    Claude-only while codex/cursor are experimental; a codex/cursor install picks
    the roster up on its next `cohort update`/`recompile`. Honors the manifest's
    recorded install mode so a --copy install is never converted to symlinks."""
    gpaths = CohortPaths.for_global(home)
    manifest = load_manifest(gpaths.manifest)
    mode = (manifest.mode if manifest and manifest.mode else None) or resolve_mode(copy=False)
    only = frozenset(roster) if roster is not None else None
    result = compile_ide(source, "claude", scope="global", only_agents=only, overlay=gpaths.my)
    write_staging(gpaths, result)
    report = do_install(
        home=home, selection=["claude"], mode=mode, force=False,
        source=source, dry_run=False,
        prune_stale=True, fresh_dests=planned_dests(gpaths, [result]),
        fresh_ides={"claude"} if result.staged else set(),
    )
    return {
        "action": "recompile", "ide": "claude", "staged": len(result.staged),
        "summary": report.summary, "scope_filtered": result.scope_filtered,
    }


_ACTION_ERRORS = (
    FeedbackError, ProposeError, RemoveSpecialistError, AddSpecialistError,
    SetupError, CompileError, ClobberRefused, UsageError,
)


def run_action(home: Path, cwd: Path, action: str, args: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one human-initiated action to the same function the CLI uses.

    Only this fixed allowlist exists — every entry is an existing human-gated
    command, so the dashboard adds no new write paths. Nothing here edits the
    global source canonical, merges, or pushes; `submit-proposals` (the draft-PR
    gate) deliberately stays in the CLI. `add-specialist` authors the project's
    own canonical, exactly as the CLI command does.
    """
    repo = find_repo_root(cwd)
    try:
        if action == "feedback":
            report = do_feedback(
                repo, str(args.get("rating", "")), args.get("agent") or None,
                args.get("command") or None, str(args.get("note", "")), dry_run=False,
            )
        elif action == "remove-specialist":
            report = do_remove_specialist(repo, home, str(args.get("name", "")), dry_run=False)
        elif action == "add-specialist":
            name = str(args.get("name", "")).strip()
            report = do_add_specialist(
                repo, home, name,
                str(args.get("display_name") or "").strip() or name,
                str(args.get("department") or "").strip() or "Project",
                str(args.get("description") or "").strip() or f"{name} (project specialist).",
                dry_run=False,
            )
        elif action == "propose-improvement":
            report = do_propose_improvement(repo, dry_run=False)
        elif action == "snapshot":
            report = do_snapshot(repo, dry_run=False, refresh_index=True)
        elif action == "init":
            if repo == home:
                raise ActionError(
                    "refusing to init the home directory as a project (it is the "
                    "global office's home) — open the dashboard from a repository"
                )
            report = do_init(repo, _require_source(home), False, bool(args.get("force")))
        elif action == "update":
            result = do_update(_require_source(home), home)
            if not result.ok:
                raise ActionError(result.detail or f"update refused: {result.status}")
            report = asdict(result)
            report["action"] = "update"
        elif action == "recompile":
            source = _require_source(home)
            report = _recompile_claude(home, source, effective_roster(home, None, source))
        elif action == "set-roster":
            names = args.get("agents")
            if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                raise ActionError("agents must be a list of names")
            if not names:
                raise ActionError("select at least one agent")
            source = _require_source(home)
            # Validate the list directly (never through the comma-string parser,
            # whose "all" sentinel and comma-splitting don't belong in an API).
            catalog = set(canonical_agents(source))
            unknown = sorted(set(names) - catalog)
            if unknown:
                raise ActionError(f"unknown agents: {', '.join(unknown)}")
            deduped = list(dict.fromkeys(names))
            # the full catalog means "follow upstream" (no persisted subset)
            roster = None if set(deduped) == catalog else deduped
            report = _recompile_claude(home, source, roster)
            # recompile-then-persist matches the CLI ordering; a persist failure
            # self-heals on the next recompile (placements win, roster reverts)
            persist_roster(home, roster)
            report["action"] = "set-roster"
            report["roster"] = roster if roster is not None else "all"
            if roster is not None and "chief-of-staff" not in roster:
                report["warning"] = (
                    "chief-of-staff is not in the subset; the office loses its triage agent"
                )
        else:
            raise ActionError(f"unknown action {action!r}")
    except _ACTION_ERRORS as exc:
        raise ActionError(str(exc))
    # Some command functions (e.g. do_snapshot outside a project) *return* an
    # error field rather than raising — surface it as a refusal, not a "done".
    if isinstance(report, dict) and report.get("error"):
        raise ActionError(str(report["error"]))
    return report


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
            # Clamp non-negative: a negative Content-Length would make read(-1)
            # read to EOF, defeating the cap and blocking the thread.
            length = max(0, min(int(self.headers.get("Content-Length", "0")), 65536))
            body = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"error": "malformed request body"})
            return
        try:
            action = str(body.get("action", ""))
            args = body.get("args") or {}
            if not isinstance(args, dict):
                raise ActionError("args must be an object")
            with self.server.action_lock:  # mutating commands never run concurrently
                report = run_action(self.server.home, self.server.cwd, action, args)
            if action == "update":
                self.server.update_cache.invalidate()  # drop the stale behind-count
            self._send_json(200, report)
        except ActionError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - never drop the connection: report as 500
            self._send_json(500, {"error": f"action failed: {exc}"})


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
