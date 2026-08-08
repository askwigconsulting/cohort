"""File a ticket back to the Cohort source repo.

`cohort feedback` records what a user noticed, but the entry stays on their machine — so a
report only reaches the maintainer if the user separately remembers to mention it. In
practice that means it does not: four detailed feedback entries sat on disk for a day and
reached the maintainer only because the user brought them up in conversation, then had to
re-file them by hand as issues.

`submit-proposals` already pushes *fixes* upstream as draft PRs. This is the other half: a
user who has a **problem but not a fix** had no path at all.

Three properties this has to hold, none of them optional:

* **Filing is egress.** A report body can quote code, config, paths and command output, so
  it goes through the same secret scan as any other payload leaving the machine. A ticket
  is not a special case just because it is text a human wrote.
* **It is an outward, effectively irreversible act.** A filed issue is public and visible to
  strangers the moment it exists; deleting it does not unsend it. So the body is shown and
  confirmed before anything is sent, never filed silently as a side effect.
* **Anyone may file; only maintainers integrate.** That asymmetry is the point — an issue is
  a claim to triage, not a change. Nothing here can write to the source repo.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cohort.engines import gates

_GH_TIMEOUT = 60
# The upstream this project's improvements flow to. A report is only useful where someone
# can act on it, and that is the source repo — never the user's own project.
DEFAULT_UPSTREAM = "askwigconsulting/cohort"


class ReportError(Exception):
    """Filing could not proceed. Never raised after an issue has been created."""


@dataclass
class ReportDraft:
    """A ticket, assembled and scanned, but not yet sent."""

    title: str
    body: str
    repo: str

    @property
    def preview(self) -> str:
        return f"repo:  {self.repo}\ntitle: {self.title}\n\n{self.body}"


def environment_block(cohort_version: str) -> str:
    """The context that decides whether a report is actionable.

    Deliberately narrow: version, OS and Python. No hostname, no username, no paths, no
    environment variables — a bug report should not be a fingerprint of the reporter's
    machine, and this is going somewhere public.
    """
    return (
        "\n\n---\n\n### Environment\n\n"
        f"- Cohort: `{cohort_version}`\n"
        f"- OS: `{platform.system()} {platform.release()}`\n"
        f"- Python: `{platform.python_version()}`\n"
    )


def build_draft(
    *, title: str, body: str, cohort_version: str, repo: str = DEFAULT_UPSTREAM,
    include_environment: bool = True,
) -> ReportDraft:
    """Assemble and gate a report. Raises before anything leaves the machine.

    Raises:
        ReportError: the title or body is empty.
        gates.SecretFoundError: the body carries credential-shaped content. Filing is
            egress, so this is the same refusal any other outbound payload gets.
    """
    title = title.strip()
    body = body.strip()
    if not title:
        raise ReportError("a report needs a title")
    if not body:
        raise ReportError("a report needs a body — describe what happened and how to repro")
    gates.assert_no_secrets(title)
    gates.assert_no_secrets(body)
    if include_environment:
        body += environment_block(cohort_version)
    return ReportDraft(title=title, body=body, repo=repo)


def gh_available() -> Optional[str]:
    """The path to an authenticated ``gh``, or None with the reason left to the caller."""
    exe = shutil.which("gh")
    if exe is None:
        return None
    try:
        proc = subprocess.run(
            [exe, "auth", "status"], capture_output=True, timeout=_GH_TIMEOUT
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return exe if proc.returncode == 0 else None


def file_issue(draft: ReportDraft, *, gh: str) -> str:
    """Create the issue and return its URL. Call only after the user has confirmed.

    The title and body are passed as separate argv entries, never interpolated into a
    shell string: both are user-authored text that can contain quotes, backticks and
    dollar signs, which is exactly the hazard `--note-file` exists to avoid upstream.
    """
    try:
        proc = subprocess.run(
            [gh, "issue", "create", "--repo", draft.repo,
             "--title", draft.title, "--body", draft.body],
            capture_output=True, text=True, timeout=_GH_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReportError(f"could not run gh: {type(exc).__name__}") from exc
    if proc.returncode != 0:
        raise ReportError(
            f"gh refused to create the issue: {(proc.stderr or '').strip()[:300]}"
        )
    return (proc.stdout or "").strip().splitlines()[-1] if proc.stdout.strip() else ""


def read_feedback_entry(path: Path) -> tuple[str, str]:
    """Turn a `cohort feedback` entry into a (title, body) pair.

    Lets a user file the thing they already wrote instead of writing it twice — which is
    the gap that made four entries sit on disk unreported.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReportError(f"could not read {path}: {exc}") from exc
    lines = text.splitlines()
    # Strip the frontmatter block, keeping its fields for the title.
    fields: dict[str, str] = {}
    body_start = 0
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                body_start = i + 1
                break
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip().strip("'\"")
    body = "\n".join(lines[body_start:]).strip()
    subject = fields.get("area") or fields.get("command") or fields.get("agent") or "cohort"
    first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "feedback")
    return f"[{subject}] {first[:90]}", body
