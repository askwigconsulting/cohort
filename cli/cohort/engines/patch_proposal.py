"""The ``patch_proposal`` loop — RFC 0004 Phase 3.

This module wires the already-built pieces (:mod:`cohort.engines.xai` transport,
:mod:`cohort.engines.patch` parse/apply, :mod:`cohort.engines.gates` fail-closed
gates, and the :mod:`cohort.engines` registry) into a single, safe control flow that
lets an external engine (e.g. Grok) *propose* a code change without ever being trusted
to make one.

The invariant this loop enforces, in order:

1. The engine must be **registered for the ``patch_proposal`` role** — an engine
   trusted only to *consult* may not propose edits.
2. Every safety gate on the **outbound** prompt runs *before any network call*
   (egress opt-out → payload bound → secret scan). A gate failure fails closed: the
   engine is never called and no worktree is created.
3. The engine's reply is **untrusted text**. Cohort — never the engine — parses it,
   re-gates the proposed paths (footprint + sensitive-class) and the proposed content
   (secret backstop), and only then applies it.
4. Writes land in an **isolated, detached git worktree**, never in ``repo_root``'s
   working tree. On success the worktree is left in place for a human/coordinator to
   review and merge; on *any* failure it is cleaned up. Nothing is ever committed here
   and ``repo_root``'s branch is never touched.

The engine's authorship is recorded, not hidden: the suggested commit message carries
a ``Co-Authored-By`` trailer attributing the change to the foreign engine.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cohort.engines import EngineSpec, get_engine
from cohort.engines import gates, patch
from cohort.engines import xai
from cohort.engines.patch import PatchResult, PatchProposal

# A proposal that rewrites a large number of files is not blocked, but it is flagged:
# a wide blast radius warrants closer human review before merge.
_MANY_FILES_THRESHOLD: int = 10

# Attribution trailer for a foreign-engine-authored change. The engine produced the
# text; Cohort applied it — so the change is co-authored, never silently absorbed.
_COAUTHOR_TRAILER: str = "Co-Authored-By: Grok (xAI) via Cohort <noreply@x.ai>"


class ProposalError(Exception):
    """A loop-level failure: a bad/unauthorised engine role, or a git worktree failure.

    Component failures (:class:`~cohort.engines.gates.GateError`,
    :class:`~cohort.engines.patch.PatchError`,
    :class:`~cohort.engines.xai.EngineError`) are *not* wrapped in this — they
    propagate as-is so the CLI can map each to its own message and exit code.
    """


@dataclass(frozen=True)
class ProposalOutcome:
    """The result of a successful proposal, left staged in an isolated worktree.

    Attributes:
        worktree: Path to the detached git worktree holding the applied change. The
            caller inspects/tests it, then a human reviews and merges; the loop never
            commits it. It must be cleaned up (``git worktree remove``) once done.
        manifest: What :func:`cohort.engines.patch.apply_patch` changed and created.
        summary: The engine's one-line description of the change.
        risk_labels: Non-blocking review flags (secret backstop labels — expected
            empty after the pre-apply gate — plus a wide-blast-radius note).
        suggested_commit_message: A subject line from ``summary`` plus a
            ``Co-Authored-By`` trailer attributing the foreign-engine authorship.
    """

    worktree: Path
    manifest: PatchResult
    summary: str
    risk_labels: list[str]
    suggested_commit_message: str


def _assemble_prompt(
    task: str, *, allowed_footprint: list[str], project_context_text: str
) -> str:
    """Build the outbound prompt instructing the engine to return the JSON patch only.

    The prompt states the task, the repo conventions and declared footprint, and the
    locked wire contract — paths are repo-relative and must stay inside the footprint,
    ``search`` must be an exact unique substring, and the reply must be JSON only.
    """
    footprint_lines = "\n".join(f"  - {entry}" for entry in allowed_footprint)
    context_section = (
        f"Repository conventions and context:\n{project_context_text.strip()}\n\n"
        if project_context_text.strip()
        else ""
    )
    return (
        "You are proposing a code change for a repository. You do not have write "
        "access; you only return a structured patch that the repository's own tooling "
        "will review and apply.\n\n"
        f"Task:\n{task.strip()}\n\n"
        f"{context_section}"
        "You may only touch files inside this declared footprint (paths are "
        "repo-relative):\n"
        f"{footprint_lines}\n\n"
        "Return ONLY a single JSON object matching exactly this contract — no prose, "
        "no markdown fence, nothing else:\n"
        '{"summary":"...","edits":[{"path":"...","search":"<exact existing '
        'substring>","replace":"..."}],"new_files":[{"path":"...","content":"..."}]}\n\n'
        "Rules:\n"
        "- Every 'path' is repo-relative and MUST stay within the declared footprint.\n"
        "- Each 'search' MUST be an exact substring that occurs exactly once in the "
        "current file; do not guess line numbers or emit a unified diff.\n"
        "- 'edits' and 'new_files' may each be empty or omitted.\n"
        "- Do not introduce secrets, API keys, or credentials.\n"
        "- Return JSON only."
    )


def cleanup_worktree(repo_root: Path, worktree: Path) -> None:
    """Best-effort removal of a detached worktree created by :func:`propose_patch`.

    Used on every failure path (and by the caller once a successful outcome has been
    reviewed). Never raises: a cleanup failure must not mask the original error that
    triggered it. Removes the git worktree registration and the temp directory, then
    prunes any stale administrative entry.

    Args:
        repo_root: The repository the worktree was created from (the git command's
            working directory).
        worktree: The worktree directory to remove.
    """
    try:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=repo_root,
            capture_output=True,
            check=False,
        )
    except OSError:
        pass
    # Remove the temp parent even if `git worktree remove` failed or was a no-op.
    parent = worktree.parent
    try:
        if parent.name.startswith("cohort-proposal-") and parent.exists():
            shutil.rmtree(parent, ignore_errors=True)
    except OSError:
        pass
    try:
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=repo_root,
            capture_output=True,
            check=False,
        )
    except OSError:
        pass


def _create_worktree(repo_root: Path) -> Path:
    """Create a fresh detached worktree off ``repo_root`` at HEAD in a temp dir.

    Returns the worktree path. Raises :class:`ProposalError` if git fails (e.g.
    ``repo_root`` is not a git repository or has no commits yet), so a git failure is
    surfaced as a loop-level error rather than a raw ``CalledProcessError``.
    """
    parent = Path(tempfile.mkdtemp(prefix="cohort-proposal-"))
    worktree = parent / "worktree"
    try:
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        # The mkdtemp parent has no worktree registered, so a plain rmtree suffices.
        shutil.rmtree(parent, ignore_errors=True)
        raise ProposalError(
            f"could not create an isolated git worktree from {repo_root} "
            "(is it a git repository with at least one commit?)"
        ) from exc
    return worktree


def _risk_labels(proposal: PatchProposal, manifest: PatchResult) -> list[str]:
    """Compute non-blocking review flags for a successfully applied proposal.

    Combines any secret-backstop labels found in the proposed content (expected empty,
    because the pre-apply gate would have blocked them) with a wide-blast-radius note
    when the change touches many files.
    """
    labels: list[str] = []
    concatenated = "\n".join(
        [e.replace for e in proposal.edits] + [f.content for f in proposal.new_files]
    )
    labels.extend(gates.scan_for_secrets(concatenated))
    touched = len(manifest.changed) + len(manifest.created)
    if touched > _MANY_FILES_THRESHOLD:
        labels.append(f"touches-many-files:{touched}")
    return labels


def _suggested_commit_message(summary: str) -> str:
    """Build a commit message: a subject from ``summary`` plus the attribution trailer.

    The subject is the first line of ``summary`` (whitespace-trimmed); the foreign
    authorship is recorded in a ``Co-Authored-By`` trailer separated by a blank line.
    """
    subject = summary.strip().splitlines()[0].strip() if summary.strip() else "Apply proposed change"
    return f"{subject}\n\n{_COAUTHOR_TRAILER}"


def propose_patch(
    engine_name: str,
    task: str,
    *,
    repo_root: Path,
    allowed_footprint: list[str],
    project_context_text: str,
    model: str | None = None,
    max_tokens: int = 4096,
    max_prompt_bytes: int = 200_000,
) -> ProposalOutcome:
    """Ask an external engine to propose a patch, and stage it safely in a worktree.

    The full loop, fail-closed at every step:

    1. Confirm ``engine_name`` is registered and trusted with the ``patch_proposal``
       role (else :class:`ProposalError`).
    2. Assemble the outbound prompt (task + conventions + the locked JSON contract).
    3. Run the pre-egress gates on that prompt *before any network call*; a gate error
       propagates and the engine is never called.
    4. Create an isolated detached git worktree off ``repo_root`` at HEAD.
    5. Call the engine. On any :class:`~cohort.engines.xai.EngineError`, clean up and
       re-raise.
    6. Parse the reply. On :class:`~cohort.engines.patch.PatchParseError`, clean up and
       re-raise.
    7. Re-gate the proposed *paths* (footprint + sensitive class) and *content*
       (secret backstop). On a gate error, clean up and re-raise.
    8. Apply the patch inside the worktree. On
       :class:`~cohort.engines.patch.PatchApplyError`, clean up and re-raise.
    9. Return a :class:`ProposalOutcome`. The change is left applied-but-uncommitted in
       the worktree; ``repo_root``'s working tree and branch are untouched.

    Args:
        engine_name: Registry key of the engine to ask (e.g. ``"grok"``).
        task: The change to request, in natural language.
        repo_root: The repository to branch the worktree from (must be a git repo).
        allowed_footprint: Repo-relative path prefixes/globs the patch may touch; an
            empty footprint is rejected here with :class:`ProposalError` (a write
            scope must be declared — no unbounded or no-op proposals).
        project_context_text: The repo's project-context file text (egress policy +
            conventions surfaced to the engine).
        model: Optional model id override; defaults to the engine's flagship.
        max_tokens: Cap on the engine's response length (bounds cost).
        max_prompt_bytes: Hard UTF-8 byte cap on the outbound prompt.

    Returns:
        A :class:`ProposalOutcome` whose ``worktree`` holds the applied change.

    Raises:
        ProposalError: unknown/unauthorised engine, an empty footprint, or a git
            worktree failure.
        GateError: any pre-egress or post-parse gate blocked (fail closed).
        EngineError: the engine call failed.
        PatchError: the reply could not be parsed or applied.
    """
    spec = _require_patch_proposal_engine(engine_name)

    # Enforce the non-empty-footprint invariant here, not only in the CLI: any caller
    # (a future orchestrator included) must declare a write scope. An empty footprint
    # would otherwise silently reduce to "block every path" — safe, but a no-op patch
    # would then report success against an empty worktree, which is misleading.
    if not any(entry.strip() for entry in allowed_footprint):
        raise ProposalError(
            "allowed_footprint is empty; refusing to propose with no declared write "
            "scope (pass at least one repo-relative path or glob)"
        )

    prompt = _assemble_prompt(
        task,
        allowed_footprint=allowed_footprint,
        project_context_text=project_context_text,
    )

    # Gate the outbound prompt BEFORE any network I/O or worktree creation. A gate
    # error propagates here — the engine is never called and no worktree exists.
    gates.preflight(
        prompt=prompt,
        project_context_text=project_context_text,
        max_bytes=max_prompt_bytes,
    )

    worktree = _create_worktree(repo_root)
    try:
        text = xai.consult(
            prompt,
            model=model,
            max_tokens=max_tokens,
            max_prompt_bytes=max_prompt_bytes,
        )
    except xai.EngineError:
        cleanup_worktree(repo_root, worktree)
        raise

    try:
        proposal = patch.parse_patch(text)
    except patch.PatchParseError:
        cleanup_worktree(repo_root, worktree)
        raise

    try:
        proposed_paths = [e.path for e in proposal.edits] + [
            f.path for f in proposal.new_files
        ]
        gates.assert_paths_allowed(proposed_paths, allowed_footprint=allowed_footprint)
        gates.assert_no_secrets(
            "\n".join(
                [e.replace for e in proposal.edits]
                + [f.content for f in proposal.new_files]
            )
        )
    except gates.GateError:
        cleanup_worktree(repo_root, worktree)
        raise

    try:
        manifest = patch.apply_patch(proposal, worktree)
    except patch.PatchApplyError:
        cleanup_worktree(repo_root, worktree)
        raise

    # Success: leave the worktree in place for review/merge. Never commit here.
    return ProposalOutcome(
        worktree=worktree,
        manifest=manifest,
        summary=proposal.summary,
        risk_labels=_risk_labels(proposal, manifest),
        suggested_commit_message=_suggested_commit_message(proposal.summary),
    )


def _require_patch_proposal_engine(engine_name: str) -> EngineSpec:
    """Return the engine spec, or raise :class:`ProposalError` if it may not propose.

    An unregistered engine, or one whose ``roles`` does not include
    ``"patch_proposal"``, is a loop-level failure — the engine is not trusted with
    this role.
    """
    try:
        spec = get_engine(engine_name)
    except KeyError as exc:
        raise ProposalError(f"unknown engine {engine_name!r}") from exc
    if "patch_proposal" not in spec.roles:
        raise ProposalError(
            f"engine {engine_name!r} is not trusted with the 'patch_proposal' role"
        )
    return spec
