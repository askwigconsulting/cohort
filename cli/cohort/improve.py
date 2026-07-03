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

import os
import re
import shutil
import subprocess
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .frontmatter import dump_frontmatter
from .gitutil import GIT_ENV, GIT_TIMEOUT
from .install_model import CohortPaths
from .loader import load_artifact, load_artifact_text
from .project import _short_id, _stage, _utc_compact, now_iso
from .update import resolve_upstream

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


# --- Phase 3: upstream-candidate heuristic + project-marker sanitize ---------
#
# "Cohort learns from its consumers": a proposal that is generally useful (no
# project-specific identifiers, canonical-shaped) is flagged as an upstream
# candidate. The flag is advisory — a human confirms by running
# `submit-proposals --upstream`, which filters to candidates, derives the upstream
# repo identity, and runs a second sanitize pass before any upstream PR.

# User-home absolute paths carry usernames → project/operator-identifying. Anchored
# to a non-path boundary so it doesn't match mid-path (e.g. /var/home/...). We do NOT
# treat .cohort/.claude refs as markers: they exist in every install and are
# Cohort-internal, not project-identifying.
_LOCAL_PATH = re.compile(r"(?<![\w/])(?:/(?:home|Users|root)/[^\s)]+|[A-Za-z]:\\Users\\[^\s)]+)")
# High-signal identifiers that should never be published upstream regardless of
# project. Every quantifier is bounded (upper limits) so attacker-influenced
# proposal text can't trigger catastrophic backtracking (ReDoS).
_EMAIL = re.compile(r"\b[\w.+-]{1,64}@[\w-]{1,255}(?:\.[\w-]{1,255}){1,8}\b")
_SECRET = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{20,255}|github_pat_[A-Za-z0-9_]{20,255}|AKIA[0-9A-Z]{16}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,255}|AIza[A-Za-z0-9_-]{35}|sk-[A-Za-z0-9]{20,255}"
    r"|eyJ[A-Za-z0-9_-]{8,512}\.[A-Za-z0-9_-]{8,512}\.[A-Za-z0-9_-]{8,512}"
    r"|-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----)"
)
_MAX_SCAN = 200_000  # cap the surface fed to the marker scanners (defense vs huge inputs)
_MAX_FIELD = 200  # max length of a feedback agent/command field (input-boundary guard)

# owner/repo from an SSH (git@host:owner/repo.git) or HTTPS (https://host/owner/repo.git)
# URL — anchored on the host boundary and accepting ONLY a 2-segment path. A nested
# path (GitLab subgroup, GHE sub-org) is ambiguous → fail closed (None).
_SLUG = re.compile(r"(?:@|//)[^/:\s]+[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")
# A safe GitHub OWNER/REPO target for `gh --repo` (no leading dash, no metachars).
_REPO_TARGET = re.compile(r"^[A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*$")


@dataclass(frozen=True)
class ProjectMarkers:
    """Identifiers that mark content as project-specific (must not leak upstream).

    ``slug`` is the project repo's ``owner/repo`` (from its git remote); ``specialists``
    are the project-scope specialist agent names (``<repo>/.cohort/agents/*.md``);
    ``identity`` is the committer's git name/email. The bare repo *directory* name is
    deliberately excluded — too collision-prone to be a reliable marker. This set is a
    best-effort defense-in-depth filter, not an exhaustive PII detector: the human PR
    review is the real publish gate.
    """

    slug: Optional[str]
    specialists: tuple[str, ...]
    identity: tuple[str, ...] = ()


def _derive_slug(url: str) -> Optional[str]:
    """``owner/repo`` from a GitHub SSH or HTTPS remote URL, else None."""
    m = _SLUG.search(url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _git_env() -> dict:
    return {**os.environ, **GIT_ENV}


def _git_config(path: Path, key: str) -> Optional[str]:
    r = subprocess.run(
        ["git", "-C", str(path), "config", "--get", key],
        capture_output=True, text=True, env=_git_env(), timeout=GIT_TIMEOUT,
    )
    return r.stdout.strip() or None if r.returncode == 0 else None


def _remote_slug(path: Path, remote: str = "origin") -> Optional[str]:
    """The ``owner/repo`` of ``path``'s ``remote`` (best-effort, read-only). ``--``
    terminates options so a config-supplied remote name can never be read as a flag."""
    if remote.startswith("-"):
        return None
    r = subprocess.run(
        ["git", "-C", str(path), "remote", "get-url", "--", remote],
        capture_output=True, text=True, env=_git_env(), timeout=GIT_TIMEOUT,
    )
    return _derive_slug(r.stdout) if r.returncode == 0 else None


def _project_specialists(repo: Path) -> tuple[str, ...]:
    """Names of the project-scope specialist agents, if any.

    Reads the unified layout *and* the pre-unification ``.cohort/agents/``:
    these names are privacy markers scrubbed from proposals, so an unmigrated
    repo must keep scrubbing its old specialist names.
    """
    paths = CohortPaths.for_project(repo)
    names: set[str] = set()
    for agents_dir in (paths.canonical / "agents", paths.cohort_home / "agents"):
        if agents_dir.exists():
            names.update(p.stem for p in agents_dir.glob("*.md"))
    return tuple(sorted(names))


def project_markers(repo: Path) -> ProjectMarkers:
    """Collect the project-identifying markers for ``repo``."""
    identity = tuple(v for v in (_git_config(repo, "user.name"), _git_config(repo, "user.email")) if v)
    return ProjectMarkers(
        slug=_remote_slug(repo), specialists=_project_specialists(repo), identity=identity
    )


def _scan_text(frontmatter: dict, body: str) -> str:
    """The full surface a proposal exposes upstream: body + frontmatter string values
    (so the candidacy gate sees `evidence_summary` etc., not just the body)."""
    fm_vals = " ".join(str(v) for v in (frontmatter or {}).values())
    # NFKC-normalize so homoglyph/full-width variants of identifiers can't slip past
    # the marker scanners; cap the surface against pathological inputs.
    return unicodedata.normalize("NFKC", f"{body}\n{fm_vals}")[:_MAX_SCAN]


def score_generality(frontmatter: dict, body: str, markers: ProjectMarkers) -> tuple[bool, str]:
    """Is this proposal generally useful to upstream Cohort? Returns
    ``(is_candidate, rationale)``. A candidate is canonical-shaped (``kind:
    improvement``) and exposes no project slug/specialist/identity, user-home path,
    email, or secret-shaped token. Markers are matched whole-word (specialists/identity)
    or case-insensitively (slug); a collision biases toward *not* upstreaming — the
    safe direction (never leak). This is best-effort, not exhaustive — a human reviews
    the rendered PR before publishing."""
    kind = (frontmatter or {}).get("kind", "")
    if kind != "improvement":
        return False, f"kind is {kind or 'unset'!r}, not a canonical-shaped improvement"
    text = _scan_text(frontmatter, body)
    hits = []
    if markers.slug and re.search(re.escape(markers.slug), text, re.IGNORECASE):
        hits.append(f"project repo {markers.slug}")
    for name in markers.specialists:
        if re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE):
            hits.append(f"project specialist {name!r}")
    for who in markers.identity:
        if re.search(rf"\b{re.escape(who)}\b", text, re.IGNORECASE):
            hits.append("committer identity")
    if _LOCAL_PATH.search(text):
        hits.append("a user-home filesystem path")
    if _EMAIL.search(text):
        hits.append("an email address")
    if _SECRET.search(text):
        hits.append("a secret-shaped token")
    if hits:
        return False, "references " + "; ".join(dict.fromkeys(hits))
    return True, "generic improvement; no project, identity, path, email, or secret markers"


def sanitize_for_upstream(text: str, markers: ProjectMarkers) -> tuple[str, list[str]]:
    """Defense-in-depth scrub before an upstream PR: replace residual project
    slug/specialist/identity, user-home paths, emails, and secret-shaped tokens with
    placeholders. Returns ``(clean, removed)``. Candidates are pre-filtered to be
    clean, so this mainly catches enrichment-injected content; it is NOT a complete
    PII filter — the human PR review is the real gate."""
    removed: list[str] = []
    out = text

    def _sub(pattern: str, repl: str, s: str, *, flags: int = 0) -> str:
        def _r(m: re.Match) -> str:
            removed.append(m.group(0))
            return repl
        return re.sub(pattern, _r, s, flags=flags)

    if markers.slug:
        out = _sub(re.escape(markers.slug), "[project repo]", out, flags=re.IGNORECASE)
    for name in markers.specialists:
        out = _sub(rf"\b{re.escape(name)}\b", "[project specialist]", out, flags=re.IGNORECASE)
    for who in markers.identity:
        out = _sub(rf"\b{re.escape(who)}\b", "[identity]", out, flags=re.IGNORECASE)
    out = _sub(_SECRET.pattern, "[redacted token]", out)
    out = _sub(_EMAIL.pattern, "[email]", out)
    out = _sub(_LOCAL_PATH.pattern, "[user path]", out)
    return out, removed


# --- feedback ---------------------------------------------------------------


def do_feedback(
    repo: Path, rating: str, agent: Optional[str], command: Optional[str],
    note: str, dry_run: bool,
) -> dict[str, Any]:
    paths = _require_project(repo)
    if rating not in RATINGS:
        raise FeedbackError(f"rating must be one of {RATINGS}, got {rating!r}")
    for field, value in (("agent", agent), ("command", command)):
        if value and len(value) > _MAX_FIELD:
            raise FeedbackError(f"{field} too long (max {_MAX_FIELD} chars)")
    pairs = [("rating", rating)]
    if agent:
        pairs.append(("agent", agent))
    if command:
        pairs.append(("command", command))
    pairs.append(("timestamp", now_iso()))
    content = dump_frontmatter(pairs) + (note.strip() + "\n" if note.strip() else "")
    filename = f"{_utc_compact()}-{_short_id()}.md"
    if dry_run:
        return {"action": "feedback", "dry_run": True, "file": filename}
    fb_dir = paths.cohort_home / "feedback"
    fb_dir.mkdir(parents=True, exist_ok=True)
    (fb_dir / filename).write_text(content, encoding="utf-8")
    return {"action": "feedback", "dry_run": False, "file": filename}


# --- propose-improvement: deterministic core + optional enrichment seam ------

# Multi-line markdown is fine in an enrichment draft; NUL/escape bytes are not.
_ENRICHMENT_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def validate_enrichment_body(text: str) -> str:
    """Refuse an empty or control-character-bearing enrichment draft (--body-file).

    The draft lands verbatim in the proposal's Rationale section, so it needs no
    single-line restriction — but reject raw control bytes at the input boundary
    the same way add-specialist metadata is rejected. Raises ``FeedbackError``."""
    if not text.strip():
        raise FeedbackError("--body-file is empty")
    if _ENRICHMENT_CONTROL.search(text):
        raise FeedbackError("--body-file must not contain control characters")
    return text


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
    fm = dump_frontmatter(
        [("kind", "improvement"), ("generated_at", now_iso()),
         ("evidence_summary", evidence_summary)]
    ).rstrip("\n")
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
    # Classify generality up front so the human reviewing the proposal sees whether
    # it's an upstream candidate (advisory; they confirm via submit-proposals --upstream).
    markers = project_markers(repo)
    parsed = load_artifact_text(proposal, name_stem=filename[:-3])
    candidate, rationale = score_generality(parsed.frontmatter or {}, parsed.body or "", markers)
    if dry_run:
        return {
            "action": "propose-improvement", "dry_run": True, "file": filename,
            "upstream_candidate": candidate, "upstream_rationale": rationale, "body": proposal,
        }
    dest = paths.cohort_home / "proposals" / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(proposal, encoding="utf-8")
    _stamp(dest, upstream_candidate=candidate, upstream_rationale=rationale)
    return {
        "action": "propose-improvement", "dry_run": False, "file": filename,
        "upstream_candidate": candidate,
    }


# --- submit-proposals: the human gate (draft PR, never merge/main) ----------

Runner = Callable[[list], object]


_PUSH_TIMEOUT = 120  # push / `gh pr create` reach the network; allow more headroom


def _default_run(cmd: list) -> object:
    return subprocess.run(
        cmd, check=True, capture_output=True, text=True,
        env=_git_env(), timeout=_PUSH_TIMEOUT,
    )


def _gh_available(source: Path) -> bool:
    if shutil.which("gh") is None or shutil.which("git") is None:
        return False
    r = subprocess.run(
        ["git", "-C", str(source), "remote", "get-url", "origin"],
        capture_output=True, text=True, env=_git_env(), timeout=GIT_TIMEOUT,
    )
    return r.returncode == 0


# Proposal filenames are generated (timestamp+id, or a validated slug); this
# guards the value before it reaches `git`/`gh` argv (no leading '-', no metachars).
_SAFE_STEM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _stamp(path: Path, **kv: Any) -> None:
    """Add idempotency-marker keys to a proposal's frontmatter via the safe emitter.

    Parses the existing frontmatter and re-emits through ``dump_frontmatter`` (not
    hand-spliced), so an unsafe key/value cannot corrupt the block (P9 [R-audit]).
    """
    loaded = load_artifact(path)
    if loaded.load_error is not None:
        raise ProposeError(f"cannot stamp {path.name}: {loaded.load_error.message}")
    fm = dict(loaded.frontmatter or {})
    fm.update(kv)
    path.write_text(dump_frontmatter(list(fm.items())) + (loaded.body or ""), encoding="utf-8")


def _current_branch(source: Path) -> Optional[str]:
    """The source repo's current branch (best-effort, read-only)."""
    try:
        r = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT, env=_git_env(),
        )
        return r.stdout.strip() or None if r.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def do_submit_proposals(
    repo: Path,
    source: Path,
    dry_run: bool,
    run: Optional[Runner] = None,
    gh_ok: Optional[bool] = None,
    target_repo: Optional[str] = None,
    home: Optional[Path] = None,
    upstream: bool = False,
) -> dict[str, Any]:
    """Turn proposals/ entries into draft PRs against the source repo (or
    ``target_repo``, e.g. your fork).

    With ``upstream=True`` (Phase 3): only proposals stamped ``upstream_candidate:
    true`` are submitted, the PR targets the **upstream Cohort** repo (the
    ``resolve_upstream`` remote from Phase 1 — push and PR target are the *same*
    repo, so no fork-head mismatch), and each body gets a sanitize pass first. To
    contribute from a fork without upstream push access, point ``[update]
    upstream_remote`` at a fork remote you can push to.

    Only ever: create/push a feature branch ``cohort/proposal-<id>`` and run
    ``gh pr create --draft``. Never merges; never pushes the default branch;
    never writes canonical/. Idempotent *per destination* — local submits skip on
    ``submitted_at``, upstream submits on ``submitted_upstream`` — so a proposal can
    go both places, but never twice to the same one. On any git/gh failure it degrades
    cleanly *and restores the original branch* so the working tree is never stranded.
    """
    run = run or _default_run
    paths = CohortPaths.for_project(repo)
    proposals_dir = paths.cohort_home / "proposals"
    proposals = sorted(proposals_dir.glob("*.md")) if proposals_dir.exists() else []
    if not proposals:
        return {"action": "submit-proposals", "submitted": [], "skipped": [], "degraded": False}

    # Upstream mode resolves a coherent (push-remote, PR-target) pair up front, so a
    # failure to identify the upstream short-circuits before any branch is created.
    push_remote, pr_target, markers = "origin", target_repo, None
    seen_key = "submitted_at"  # local idempotency key (per-destination)
    if upstream:
        seen_key = "submitted_upstream"
        push_remote, _ = resolve_upstream(source, home or Path.home())
        pr_target = _remote_slug(source, push_remote)
        markers = project_markers(repo)
        if pr_target is None and not dry_run:
            return {
                "action": "submit-proposals", "submitted": [], "skipped": [], "degraded": True,
                "detail": f"could not resolve the upstream repo from remote {push_remote!r}; "
                "set [update] upstream_remote in cohort.toml to a GitHub remote.",
            }
    # Any PR target — derived upstream or an explicit --repo — must be a valid OWNER/REPO
    # before it reaches `gh` argv.
    if pr_target is not None and not _REPO_TARGET.match(pr_target) and not dry_run:
        return {
            "action": "submit-proposals", "submitted": [], "skipped": [], "degraded": True,
            "detail": f"{pr_target!r} is not a valid OWNER/REPO PR target.",
        }

    available = _gh_available(source) if gh_ok is None else gh_ok
    submitted, skipped, redacted = [], [], []
    degraded = not available and not dry_run

    for p in proposals:
        fm = load_artifact(p).frontmatter or {}
        if fm.get(seen_key):
            skipped.append(p.name)  # idempotent: already submitted to this destination
            continue
        if not _SAFE_STEM.match(p.stem):
            skipped.append(p.name)  # unsafe filename → never feed to git/gh argv
            continue
        if upstream and fm.get("upstream_candidate") is not True:
            skipped.append(p.name)  # not a human-confirmed upstream candidate
            continue
        kind = fm.get("kind", "promotion")  # back-compat default
        branch = f"cohort/proposal-{p.stem}"
        if dry_run or not available:
            continue
        staged = source / "proposals" / p.name  # review staging area — NEVER canonical/
        original = _current_branch(source)
        try:
            run(["git", "-C", str(source), "checkout", "-b", branch])
            staged.parent.mkdir(parents=True, exist_ok=True)
            if upstream:
                clean, removed = sanitize_for_upstream(p.read_text(encoding="utf-8"), markers)
                staged.write_text(clean, encoding="utf-8")
                redacted.extend(removed)
            else:
                staged.write_bytes(p.read_bytes())
            run(["git", "-C", str(source), "add", str(staged)])
            run(["git", "-C", str(source), "commit", "-m", f"Proposal ({kind}): {p.stem}"])
            run(["git", "-C", str(source), "push", "--", push_remote, branch])
            title = f"Cohort {'upstream ' if upstream else ''}proposal ({kind}): {p.stem}"
            pr_cmd = ["gh", "pr", "create", "--draft", "--head", branch,
                      "--title", title, "--body-file", str(staged)]
            if pr_target:
                pr_cmd += ["--repo", pr_target]
            run(pr_cmd)
        except Exception:  # noqa: BLE001 - git/gh failure (no push access, not a GitHub remote, …)
            degraded = True
        finally:
            # Always restore the working tree to where it started — never strand
            # the user on the proposal branch.
            if original:
                try:
                    run(["git", "-C", str(source), "checkout", original])
                except Exception:  # noqa: BLE001
                    pass
            if degraded:
                # Clean up the partial attempt so a retry isn't permanently wedged:
                # the leftover branch would fail the next `checkout -b`, and the
                # staged file would make a later `cohort update` refuse as dirty.
                try:
                    run(["git", "-C", str(source), "branch", "-D", branch])
                except Exception:  # noqa: BLE001
                    pass
                if staged.exists():
                    staged.unlink()
        if degraded:
            break  # leave the proposal as a file (unstamped) for manual PR creation
        # Stamp only this destination's key, so a local submit doesn't bar a later
        # upstream one (or vice versa).
        if upstream:
            stamp = {"submitted_upstream": pr_target, "submitted_upstream_at": now_iso(), "branch": branch}
        else:
            stamp = {"submitted_at": now_iso(), "branch": branch}
        _stamp(p, **stamp)
        submitted.append(p.name)

    result = {
        "action": "submit-proposals", "dry_run": dry_run, "degraded": degraded,
        "submitted": submitted, "skipped": skipped,
    }
    if upstream:
        result["redacted"] = sorted(set(redacted))
    return result
