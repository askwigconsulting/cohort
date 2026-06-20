"""The self-improvement loop (Steward): feedback, propose-improvement, submit.

The spine is the safety boundary, not the feature. The loop is human-initiated
and human-gated at every consequential step:

    cohort feedback  →  cohort propose-improvement  →  (human reads)
                     →  cohort submit-proposals (draft PR)  →  (human reviews + merges)

Three invariants make it structurally unable to change the harness unattended:
  1. no auto-edit of canonical/ (global or source) — propose/submit only write
     proposals/ (and a review staging area);
  2. no auto-merge / no push to the default branch — submit only ever creates a
     feature branch + a *draft* PR;
  3. PRs are drafts.

``propose-improvement`` has **no runtime-LLM dependency**: its core is
deterministic signal aggregation; LLM enrichment is an optional, mockable seam
that defaults to the deterministic summary (the real Steward enriches in-IDE).
"""

from __future__ import annotations

import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Optional

from .install_model import CohortPaths
from .loader import load_artifact
from .project import _short_id, _stage, _utc_compact, now_iso

RATINGS = ("up", "down")


class FeedbackError(Exception):
    pass


class ProposeError(Exception):
    pass


def _require_project(repo: Path) -> CohortPaths:
    paths = CohortPaths.for_project(repo)
    if not paths.cohort_home.exists():
        raise FeedbackError("not a Cohort project; run `cohort init` first")
    return paths


# --- feedback ---------------------------------------------------------------


def do_feedback(
    repo: Path, rating: str, agent: Optional[str], command: Optional[str],
    note: str, dry_run: bool,
) -> dict[str, Any]:
    paths = _require_project(repo)
    if rating not in RATINGS:
        raise FeedbackError(f"rating must be one of {RATINGS}, got {rating!r}")
    fm = ["---", f"rating: {rating}"]
    if agent:
        fm.append(f"agent: {agent}")
    if command:
        fm.append(f"command: {command}")
    fm += [f"timestamp: {now_iso()}", "---"]
    content = "\n".join(fm) + "\n" + (note.strip() + "\n" if note.strip() else "")
    filename = f"{_utc_compact()}-{_short_id()}.md"
    if dry_run:
        return {"action": "feedback", "dry_run": True, "file": filename}
    fb_dir = paths.cohort_home / "feedback"
    fb_dir.mkdir(parents=True, exist_ok=True)
    (fb_dir / filename).write_text(content, encoding="utf-8")
    return {"action": "feedback", "dry_run": False, "file": filename}


# --- propose-improvement: deterministic core + optional enrichment seam ------


def aggregate_signals(paths: CohortPaths) -> dict[str, Any]:
    """Deterministic evidence from feedback/ + sessions/ — no LLM, no network."""
    fb_dir = paths.cohort_home / "feedback"
    sessions_dir = paths.cohort_home / "sessions"
    up: Counter = Counter()
    down: Counter = Counter()
    cmd_down: Counter = Counter()
    total = 0
    for f in sorted(fb_dir.glob("*.md")) if fb_dir.exists() else []:
        fm = load_artifact(f).frontmatter or {}
        total += 1
        rating, agent, command = fm.get("rating"), fm.get("agent"), fm.get("command")
        if agent:
            (up if rating == "up" else down)[agent] += 1
        if command and rating == "down":
            cmd_down[command] += 1
    sessions = len(list(sessions_dir.glob("*.md"))) if sessions_dir.exists() else 0
    usage = up + down
    low_rated = sorted(a for a in down if down[a] > up.get(a, 0))
    return {
        "feedback_total": total,
        "sessions": sessions,
        "agent_usage": dict(sorted(usage.items())),
        "low_rated_agents": low_rated,
        "friction_commands": sorted(cmd_down),
    }


def _deterministic_summary(ev: dict) -> str:
    return (
        f"{ev['feedback_total']} feedback entries over {ev['sessions']} sessions; "
        f"low-rated: {ev['low_rated_agents'] or 'none'}; "
        f"command friction: {ev['friction_commands'] or 'none'}"
    )


def _suggestions(ev: dict) -> list[str]:
    out = [f"Revisit low-rated agent '{a}' (more down than up ratings)." for a in ev["low_rated_agents"]]
    out += [f"Reduce friction in command '{c}'." for c in ev["friction_commands"]]
    return out or ["No corrective signal; roster and commands are tracking well."]


def render_improvement_proposal(ev: dict, enrichment: Optional[str] = None) -> str:
    summary = enrichment or _deterministic_summary(ev)
    # YAML-safe one-liner (no ':' or '[' which would break frontmatter parsing);
    # the human-readable summary lives in the body Rationale.
    evidence_summary = (
        f"{ev['feedback_total']} feedback entries, {ev['sessions']} sessions, "
        f"{len(ev['low_rated_agents'])} low-rated, {len(ev['friction_commands'])} friction-commands"
    )
    fm = "\n".join(
        ["---", "kind: improvement", f"generated_at: {now_iso()}",
         f"evidence_summary: {evidence_summary}", "---"]
    )
    lines = [
        "# Improvement proposal",
        "",
        "## Evidence",
        f"- Feedback entries: {ev['feedback_total']}",
        f"- Sessions: {ev['sessions']}",
        f"- Agent usage: {ev['agent_usage'] or 'none recorded'}",
        f"- Low-rated agents: {ev['low_rated_agents'] or 'none'}",
        f"- Command friction: {ev['friction_commands'] or 'none'}",
        "",
        "## Suggested changes",
        *[f"- {s}" for s in _suggestions(ev)],
        "",
        "## Rationale",
        summary,
    ]
    return f"{fm}\n" + "\n".join(lines) + "\n"


def do_propose_improvement(
    repo: Path, dry_run: bool, enrich: Optional[Callable[[dict], str]] = None
) -> dict[str, Any]:
    """Aggregate signals → a kind:improvement proposal. ``enrich`` is the optional
    (mockable) LLM seam; default None → the deterministic summary stands."""
    paths = _require_project(repo)
    evidence = aggregate_signals(paths)
    enrichment = enrich(evidence) if enrich is not None else None
    proposal = render_improvement_proposal(evidence, enrichment)
    filename = f"improvement-{_utc_compact()}-{_short_id()}.md"
    if dry_run:
        return {"action": "propose-improvement", "dry_run": True, "file": filename, "body": proposal}
    dest = paths.cohort_home / "proposals" / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(proposal, encoding="utf-8")
    return {"action": "propose-improvement", "dry_run": False, "file": filename}


# --- submit-proposals: the human gate (draft PR, never merge/main) ----------

Runner = Callable[[list], object]


def _default_run(cmd: list) -> object:
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


def _gh_available(source: Path) -> bool:
    if shutil.which("gh") is None or shutil.which("git") is None:
        return False
    r = subprocess.run(
        ["git", "-C", str(source), "remote", "get-url", "origin"],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def _stamp(path: Path, **kv: str) -> None:
    """Insert frontmatter keys before the closing '---' (idempotency marker)."""
    lines = path.read_text(encoding="utf-8").split("\n")
    close = lines.index("---", 1)
    inject = [f"{k}: {v}" for k, v in kv.items()]
    path.write_text("\n".join(lines[:close] + inject + lines[close:]), encoding="utf-8")


def do_submit_proposals(
    repo: Path,
    source: Path,
    dry_run: bool,
    run: Optional[Runner] = None,
    gh_ok: Optional[bool] = None,
) -> dict[str, Any]:
    """Turn proposals/ entries into draft PRs against the source repo.

    Only ever: create/push a feature branch ``cohort/proposal-<id>`` and run
    ``gh pr create --draft``. Never merges; never pushes the default branch;
    never writes canonical/. Idempotent via the ``submitted_at`` stamp.
    """
    run = run or _default_run
    paths = CohortPaths.for_project(repo)
    proposals_dir = paths.cohort_home / "proposals"
    proposals = sorted(proposals_dir.glob("*.md")) if proposals_dir.exists() else []
    if not proposals:
        return {"action": "submit-proposals", "submitted": [], "skipped": [], "degraded": False}

    available = _gh_available(source) if gh_ok is None else gh_ok
    submitted, skipped = [], []
    degraded = not available and not dry_run

    for p in proposals:
        fm = load_artifact(p).frontmatter or {}
        if fm.get("submitted_at"):
            skipped.append(p.name)  # idempotent: already submitted
            continue
        kind = fm.get("kind", "promotion")  # back-compat default
        branch = f"cohort/proposal-{p.stem}"
        if dry_run or not available:
            continue
        staged = source / "proposals" / p.name  # review staging area — NEVER canonical/
        run(["git", "-C", str(source), "checkout", "-b", branch])
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(p.read_bytes())
        run(["git", "-C", str(source), "add", str(staged)])
        run(["git", "-C", str(source), "commit", "-m", f"Proposal ({kind}): {p.stem}"])
        run(["git", "-C", str(source), "push", "origin", branch])
        run(["gh", "pr", "create", "--draft", "--head", branch,
             "--title", f"Cohort proposal ({kind}): {p.stem}", "--body-file", str(staged)])
        run(["git", "-C", str(source), "checkout", "-"])
        _stamp(p, submitted_at=now_iso(), branch=branch)
        submitted.append(p.name)

    return {
        "action": "submit-proposals", "dry_run": dry_run, "degraded": degraded,
        "submitted": submitted, "skipped": skipped,
    }
