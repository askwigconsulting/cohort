"""`cohort life` + `cohort run` — the life project's interactive CLI surface (RFC 0003 §4).

This is the contract mission control (WS-B) dispatches to. Its shape is its
safety model:

- **`cohort life <verb>`** — deterministic markdown writers (`toggle-task`,
  `set-top3`, `add-task`) to `days/`/`weeks/`/`inbox.md`. Write targets are
  resolved by **enumeration** (the `read_artifact` pattern): a caller supplies a
  target *name* from a fixed enum, never a path, so `..`/separators/absolute
  paths cannot select a destination. Each verb is a testable `do_*` function.
- **`cohort life enqueue <command>`** — writes a *bounded* job-request file
  (`.cohort/jobs/<command>-<ts>.json`: allowlisted command name + timestamp +
  status — **never a free-text prompt**).
- **`cohort run`** — the foreground runner, the ONLY thing that ever spawns
  `claude` (the dashboard's http.server writes requests; it never spawns — RFC
  principle 2). Fail-closed: argv is a **constant** from the exact-key allowlist
  ``_JOBS`` (a crafted name like ``briefing --permission-mode=…`` misses the key
  and is refused), ``shell=False``, the request contributes **zero** argv
  tokens, ``--settings`` is a runner-pinned constant path, the child env is a
  minimal curated set (never ``{**os.environ}``), cwd is pinned from the
  resolved project registry, every job has a timeout, one job per command is
  in flight at a time (extra requests are *rejected*, never queued), and child
  PIDs are terminated on shutdown so nothing outlives the terminal.

Stdlib-only; no new dependencies.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .install_model import CohortPaths
from .manifest import now_iso
from .project import LIFE_INBOX_SEED, find_repo_root, is_life_project, list_projects


class LifeError(Exception):
    """A refused `cohort life` / `cohort run` request."""


# --- the §1a data model (filenames, skeletons, checklist grammar) ------------

_TARGETS = ("today", "week", "inbox")
_SECTION_BY_TARGET = {"today": "Top 3", "week": "Plan"}  # inbox appends at EOF

# GitHub-style checklist at line start, one optional leading indent level.
# `[x]` = done; anything else = open (§1a).
_CHECKLIST = re.compile(r"^(?:\t| {1,4})?- \[(?P<state>[^\]])\] ")

DAY_SKELETON = "# {date}\n\n## Agenda\n\n## Top 3\n\n## Log\n"
WEEK_SKELETON = "# {label}\n\n## Plan\n\n## Review\n"


def local_today(now: Optional[datetime] = None) -> date:
    """Today in the user's local timezone — computed once per command and passed
    in (never re-read mid-logic), so callers agree across midnight/UTC (§1a)."""
    return (now or datetime.now().astimezone()).date()


def week_label(d: date) -> str:
    """ISO-8601 week label (``YYYY-Wnn``, zero-padded) for a date."""
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def resolve_target(repo: Path, target: str, today: Optional[date] = None) -> tuple[Path, str]:
    """Map an enumerated target name to its file: ``(absolute path, repo-relative)``.

    Enumeration IS the boundary (the ``read_artifact`` pattern): the caller never
    supplies a path or filename, so a crafted ``target`` (``..``, separators, an
    absolute path) cannot select a write destination — anything outside the enum
    is refused outright."""
    d = today or local_today()
    if target == "today":
        rel = f"days/{d.isoformat()}.md"
    elif target == "week":
        rel = f"weeks/{week_label(d)}.md"
    elif target == "inbox":
        rel = "inbox.md"
    else:
        raise LifeError(
            f"unknown target {target!r} — targets are enumerated ({', '.join(_TARGETS)}), "
            "never a path"
        )
    return repo / rel, rel


def _require_life(repo: Path) -> None:
    if not is_life_project(CohortPaths.for_project(repo)):
        raise LifeError(
            'not a life project (no template = "life" in .cohort/cohort.toml) — '
            "`cohort life` verbs write day/week files and only run in a life project"
        )


_TASK_MAX = 500
# ASCII controls (incl. newline/CR), DEL, NEL, and the Unicode line separators:
# a task line must stay ONE physical line so it can never inject a heading or a
# second (unreviewed) item into a trusted-tier file.
_TASK_CONTROL = re.compile("[\\x00-\\x1f\\x7f\\x85\\u2028\\u2029]")


def _clean_task_text(text: str) -> str:
    text = text.strip()
    if not text:
        raise LifeError("task text is empty")
    if len(text) > _TASK_MAX:
        raise LifeError(f"task text exceeds {_TASK_MAX} characters")
    if _TASK_CONTROL.search(text):
        raise LifeError("task text must be a single line with no control characters")
    return text


def _initial_content(target: str, d: date) -> str:
    if target == "today":
        return DAY_SKELETON.format(date=d.isoformat())
    if target == "week":
        return WEEK_SKELETON.format(label=week_label(d))
    return LIFE_INBOX_SEED


def _section_bounds(lines: list[str], heading: str) -> Optional[tuple[int, int]]:
    """``(start, end)`` of the ``## <heading>`` section: start is the heading
    line's index; end is the next ``## `` heading's index (or EOF)."""
    for i, ln in enumerate(lines):
        if ln.strip() == f"## {heading}":
            end = next(
                (j for j in range(i + 1, len(lines)) if lines[j].startswith("## ")),
                len(lines),
            )
            return i, end
    return None


def _write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")


# --- verbs (each a testable do_* function; WS-B dispatches to these) ---------


def do_add_task(
    repo: Path, target: str, text: str, today: Optional[date] = None
) -> dict[str, Any]:
    """Append ``- [ ] <text>`` to an enumerated target: today's ``## Top 3``
    (refused when full), this week's ``## Plan``, or the end of ``inbox.md``."""
    _require_life(repo)
    text = _clean_task_text(text)
    d = today or local_today()
    path, rel = resolve_target(repo, target, d)
    content = path.read_text(encoding="utf-8") if path.exists() else _initial_content(target, d)
    item = f"- [ ] {text}"
    lines = content.splitlines()
    if target == "inbox":
        while lines and not lines[-1].strip():
            lines.pop()
        lines.append(item)
    else:
        heading = _SECTION_BY_TARGET[target]
        bounds = _section_bounds(lines, heading)
        if bounds is None:
            # A known heading is missing (§1a diagnoses it elsewhere); the write
            # still lands deterministically — append the section.
            lines += ["", f"## {heading}", "", item]
        else:
            start, end = bounds
            if target == "today":
                existing = sum(1 for ln in lines[start:end] if _CHECKLIST.match(ln))
                if existing >= 3:
                    raise LifeError(
                        "Top 3 already has 3 items — toggle one done or edit the file"
                    )
            insert = end
            while insert > start + 1 and not lines[insert - 1].strip():
                insert -= 1  # keep the blank line separating sections below the item
            lines.insert(insert, item)
    _write(path, lines)
    return {"action": "life-add-task", "target": target, "file": rel, "text": text}


def do_toggle_task(
    repo: Path, target: str, line: int, today: Optional[date] = None
) -> dict[str, Any]:
    """Toggle the N-th checklist item (1-based, file order) in an enumerated
    target between open (``[ ]``) and done (``[x]``)."""
    _require_life(repo)
    path, rel = resolve_target(repo, target, today)
    try:
        n = int(line)
    except (TypeError, ValueError):
        raise LifeError(f"line must be an integer, got {line!r}")
    if n < 1:
        raise LifeError("line is 1-based")
    if not path.exists():
        raise LifeError(f"{rel} does not exist yet")
    lines = path.read_text(encoding="utf-8").splitlines()
    count = 0
    for i, ln in enumerate(lines):
        m = _CHECKLIST.match(ln)
        if not m:
            continue
        count += 1
        if count != n:
            continue
        checked = m.group("state").lower() != "x"  # [x] = done; anything else = open
        bracket = ln.index("[")
        lines[i] = ln[:bracket + 1] + ("x" if checked else " ") + ln[bracket + 2:]
        _write(path, lines)
        return {
            "action": "life-toggle-task", "target": target, "file": rel,
            "line": n, "checked": checked, "text": ln[m.end():].strip(),
        }
    raise LifeError(f"{rel} has {count} checklist item(s); there is no item #{n}")


def do_set_top3(
    repo: Path, items: list[str], today: Optional[date] = None
) -> dict[str, Any]:
    """Replace today's ``## Top 3`` with 1–3 checklist items (creates the day
    file from the §1a skeleton when absent)."""
    _require_life(repo)
    if not items or len(items) > 3:
        raise LifeError(f"set-top3 takes 1–3 items, got {len(items)}")
    cleaned = [_clean_task_text(t) for t in items]
    d = today or local_today()
    path, rel = resolve_target(repo, "today", d)
    content = path.read_text(encoding="utf-8") if path.exists() else _initial_content("today", d)
    lines = content.splitlines()
    body = [""] + [f"- [ ] {t}" for t in cleaned] + [""]
    bounds = _section_bounds(lines, "Top 3")
    if bounds is None:
        lines += ["", "## Top 3"] + body[:-1]
    else:
        start, end = bounds
        lines[start + 1:end] = body
    _write(path, lines)
    return {"action": "life-set-top3", "file": rel, "items": cleaned}


# --- jobs: enqueue (bounded request) + run (the only claude spawner) ---------

# CONSTANT argv per allowlisted command — the job request contributes ZERO
# tokens. `--settings` is the runner-pinned, egress-closed briefing profile (a
# constant path relative to the pinned project root, never a client value):
# both job commands read connector content unattended, so both run with every
# outbound channel closed (RFC principle 5).
_BRIEFING_SETTINGS = ".claude/settings.briefing.json"
_JOBS: dict[str, tuple[str, ...]] = {
    "briefing": ("claude", "-p", "/briefing", "--settings", _BRIEFING_SETTINGS),
    "triage": ("claude", "-p", "/triage", "--settings", _BRIEFING_SETTINGS),
}

_JOB_TIMEOUT_S = 1800.0  # per-job wall clock; the runner terminates overruns
_POLL_INTERVAL_S = 2.0
_JOB_FILE = re.compile(r"^([a-z][a-z0-9-]*)-(\d{8}T\d{6}Z)\.json$")
# The bounded job-request schema: these keys and nothing else, never free text.
_JOB_KEYS = ("command", "requested_at", "status", "started_at", "finished_at",
             "exit_code", "output", "error")
# Minimal curated child env — never {**os.environ}: a mail-reading session must
# not inherit tokens/keys the parent shell happens to carry.
_ENV_KEYS = ("PATH", "HOME", "USERPROFILE", "LANG", "LC_ALL", "LC_CTYPE", "TERM",
             "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "COMSPEC")


def jobs_dir(repo: Path) -> Path:
    return repo / ".cohort" / "jobs"


def _read_job(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - a malformed request is treated as empty
        return {}


def _update_job(path: Path, **fields: Any) -> None:
    """Rewrite a job file, keeping only the bounded ``_JOB_KEYS`` schema."""
    data = {k: v for k, v in _read_job(path).items() if k in _JOB_KEYS}
    data.update({k: v for k, v in fields.items() if k in _JOB_KEYS})
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass  # status is advisory; a write failure never crashes the runner


def do_enqueue(repo: Path, command: str, now: Optional[datetime] = None) -> dict[str, Any]:
    """Write a bounded job-request file for an allowlisted command.

    The request carries the command *name*, a timestamp, and a status — never a
    free-text prompt, flags, or paths. One outstanding request per command."""
    _require_life(repo)
    if command not in _JOBS:
        raise LifeError(
            f"unknown job command {command!r} — allowlisted commands: "
            + ", ".join(sorted(_JOBS))
        )
    d = jobs_dir(repo)
    if d.exists():
        for existing in sorted(d.glob(f"{command}-*.json")):
            status = _read_job(existing).get("status")
            if status in ("queued", "running"):
                raise LifeError(
                    f"a {command} job is already {status} ({existing.name}) — "
                    "single-flight per command; wait for it to finish"
                )
    ts = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    job = d / f"{command}-{ts}.json"
    if job.exists():
        raise LifeError(f"{job.name} already exists — try again in a second")
    d.mkdir(parents=True, exist_ok=True)
    payload = {"command": command, "requested_at": now_iso(), "status": "queued"}
    job.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return {"action": "life-enqueue", "command": command,
            "job": f".cohort/jobs/{job.name}"}


# A spawner takes (argv, cwd, stdout_handle) and returns a Popen-like object
# (.poll/.terminate/.kill/.wait). Injectable so tests never spawn `claude`.
Spawner = Callable[[list[str], Path, Any], Any]


def _curated_env() -> dict[str, str]:
    """The child's environment: the ``_ENV_KEYS`` allowlist and nothing else —
    never ``{**os.environ}`` (see the module docstring)."""
    return {k: os.environ[k] for k in _ENV_KEYS if k in os.environ}


def _default_spawn(argv: list[str], cwd: Path, stdout: Any) -> subprocess.Popen:
    return subprocess.Popen(  # noqa: S603 - constant argv from _JOBS, shell=False
        argv, cwd=str(cwd), stdout=stdout, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, env=_curated_env(), shell=False,
    )


def resolve_run_root(home: Path, cwd: Path) -> Path:
    """The execution cwd, pinned from the project registry.

    The current repo must be a *registered* Cohort project and a life project;
    the registry's recorded path — never a job-file value, never a client path —
    is what the child process runs in."""
    repo = find_repo_root(cwd)
    resolved = str(repo.resolve())
    for entry in list_projects(home):
        if str(Path(entry["path"]).resolve()) == resolved:
            root = Path(entry["path"])
            if not is_life_project(CohortPaths.for_project(root)):
                raise LifeError(
                    "cohort run executes life-project jobs only; this project has no "
                    'template = "life" marker'
                )
            return root
    raise LifeError("this repo is not a registered Cohort project (run `cohort init`)")


class _RunningJob:
    def __init__(self, command: str, job_file: Path, proc: Any, out: Any, output: Path):
        self.command = command
        self.job_file = job_file
        self.proc = proc
        self.out = out
        self.output = output
        self.started = time.monotonic()


def _queued_files(jdir: Path) -> list[Path]:
    out = []
    for p in sorted(jdir.glob("*.json")):
        if _JOB_FILE.match(p.name) and _read_job(p).get("status") == "queued":
            out.append(p)
    return out


def _start_job(
    job_file: Path, root: Path, quarantine: Path,
    running: dict[str, _RunningJob], spawn: Spawner,
    summary: dict[str, list], say: Callable[[str], None],
) -> None:
    m = _JOB_FILE.match(job_file.name)
    command = _read_job(job_file).get("command")
    if (
        not isinstance(command, str) or command not in _JOBS
        or m is None or m.group(1) != command
    ):
        # Fail-closed: a crafted name ("briefing --permission-mode=…") misses the
        # exact-key allowlist and is refused — it never reaches argv.
        _update_job(job_file, status="rejected",
                    error="command not in the allowlist: " + ", ".join(sorted(_JOBS)))
        summary["rejected"].append(job_file.name)
        say(f"cohort run: rejected {job_file.name} (not an allowlisted command)")
        return
    if command in running:
        # Single-flight per command: reject with a clear error, never queue.
        _update_job(job_file, status="rejected",
                    error=f"a {command} job is already running — single-flight per "
                          "command; re-enqueue after it finishes")
        summary["rejected"].append(job_file.name)
        say(f"cohort run: rejected {job_file.name} ({command} already running)")
        return
    argv = list(_JOBS[command])  # constant argv — zero tokens from the request
    quarantine.mkdir(parents=True, exist_ok=True)
    output = quarantine / f"{command}-{m.group(2)}.md"  # job stdout is QUARANTINE
    out = open(output, "wb")
    try:
        proc = spawn(argv, root, out)
    except OSError as exc:
        out.close()
        _update_job(job_file, status="failed", finished_at=now_iso(),
                    error=f"could not spawn claude: {exc}")
        summary["rejected"].append(job_file.name)
        say(f"cohort run: failed to spawn {command} ({exc})")
        return
    running[command] = _RunningJob(command, job_file, proc, out, output)
    _update_job(job_file, status="running", started_at=now_iso(),
                output=output.relative_to(root).as_posix())  # POSIX slashes: portable job record
    summary["started"].append(job_file.name)
    say(f"cohort run: started {command} → {output.relative_to(root)}")


def _finish(job: _RunningJob, status: str, summary: dict[str, list],
            say: Callable[[str], None], **fields: Any) -> None:
    try:
        job.out.close()
    except Exception:  # noqa: BLE001
        pass
    _update_job(job.job_file, status=status, finished_at=now_iso(), **fields)
    summary["finished"].append(job.job_file.name)
    say(f"cohort run: {job.command} {status}")


def _stop_proc(proc: Any) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001 - escalate to kill
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 - nothing more we can do
            pass


def _reap(running: dict[str, _RunningJob], summary: dict[str, list],
          say: Callable[[str], None], job_timeout: float) -> None:
    for command in list(running):
        job = running[command]
        code = job.proc.poll()
        if code is None:
            if time.monotonic() - job.started > job_timeout:
                _stop_proc(job.proc)
                _finish(job, "failed", summary, say,
                        error=f"timed out after {int(job_timeout)}s")
                del running[command]
            continue
        _finish(job, "done" if code == 0 else "failed", summary, say, exit_code=code)
        del running[command]


def do_run(
    home: Path, cwd: Path, once: bool = False, spawn: Optional[Spawner] = None,
    echo: Optional[Callable[[str], None]] = None,
    poll_interval: float = _POLL_INTERVAL_S, job_timeout: float = _JOB_TIMEOUT_S,
) -> dict[str, Any]:
    """The foreground job runner (the human-started actor; nothing else spawns).

    Watches ``.cohort/jobs/`` in the registry-pinned project root, executes each
    queued request from the constant-argv allowlist, streams stdout to the
    briefing quarantine, and terminates children on shutdown. With ``once``,
    drains the current queue and exits when nothing is running."""
    say = echo or (lambda _m: None)
    root = resolve_run_root(home, cwd)
    jdir = jobs_dir(root)
    jdir.mkdir(parents=True, exist_ok=True)
    quarantine = root / ".cohort" / "reports" / "briefings"
    running: dict[str, _RunningJob] = {}
    summary: dict[str, list] = {"started": [], "finished": [], "rejected": []}
    spawn = spawn or _default_spawn
    try:
        while True:
            _reap(running, summary, say, job_timeout)
            for job_file in _queued_files(jdir):
                _start_job(job_file, root, quarantine, running, spawn, summary, say)
            if once and not running:
                break
            time.sleep(0.05 if once else poll_interval)
    except KeyboardInterrupt:
        say("cohort run: interrupted — terminating jobs")
    finally:
        # Nothing outlives the terminal: terminate every child on the way out.
        for command in list(running):
            job = running.pop(command)
            _stop_proc(job.proc)
            _finish(job, "failed", summary, say, error="runner shut down")
    return {"action": "run", "root": str(root), **summary}


# --- connector presence (status surface; presence/keys only) -----------------

_MCP_RULE = re.compile(r"^mcp__(.+?)__")


def connector_status(repo: Path) -> dict[str, Any]:
    """Presence-only connector report for ``cohort status``.

    Reads `.mcp.json` **entry keys** and the permission profile's rule prefixes —
    never tokens, credentials, or server contents, and never calls a server. A
    profile key with no configured server means every rule for it silently
    matches nothing (the §2 server-key-mismatch failure)."""
    out: dict[str, Any] = {
        "mcp_json": (repo / ".mcp.json").exists(),
        "example": (repo / ".mcp.json.example").exists(),
        "configured_keys": [], "profile_keys": [], "missing_keys": [],
    }
    if out["mcp_json"]:
        try:
            servers = json.loads(
                (repo / ".mcp.json").read_text(encoding="utf-8")
            ).get("mcpServers")
            if isinstance(servers, dict):
                out["configured_keys"] = sorted(str(k) for k in servers)
        except Exception:  # noqa: BLE001 - unreadable config is reported, not fatal
            out["parse_error"] = True
    profile = repo / ".claude" / "settings.json"
    keys: set = set()
    if profile.exists():
        try:
            perms = json.loads(profile.read_text(encoding="utf-8")).get("permissions", {})
            for tier in ("allow", "deny", "ask"):
                for rule in perms.get(tier) or []:
                    m = _MCP_RULE.match(str(rule))
                    if m:
                        keys.add(m.group(1))
        except Exception:  # noqa: BLE001 - unreadable profile → no keys to compare
            pass
    out["profile_keys"] = sorted(keys)
    if out["mcp_json"] and not out.get("parse_error"):
        configured = set(out["configured_keys"])
        out["missing_keys"] = sorted(k for k in keys if k not in configured)
    return out
