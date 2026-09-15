"""`cohort report` files a ticket upstream — an outward, effectively irreversible act.

`feedback` records what a user noticed locally, and the entry then stays on their machine:
four detailed entries sat on disk for a day and reached the maintainer only because the user
mentioned them in conversation. `submit-proposals` pushes *fixes* upstream; a user with a
problem and no fix had no path at all.

These tests pin the three properties that make an automatic issue-filer safe: the body is
secret-scanned before anything is sent, nothing is filed without confirmation, and the
report carries enough context to be actionable without fingerprinting the reporter.
"""

from __future__ import annotations

import pytest

from cohort import report as rm
from cohort.engines import gates


def test_a_secret_in_the_body_refuses_before_anything_is_sent() -> None:
    """Filing is egress. A report is public the moment it exists, and deleting an issue does
    not unsend it — so the scan has to run before the network call, not after.

    Regression: this exact body once filed a real public issue, because the scanner's
    assignment pattern let a prose label swallow the credential on the following line.
    """
    body = 'Repro:\n\nAWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY"\n'
    with pytest.raises(gates.SecretFoundError):
        rm.build_draft(title="bug", body=body, cohort_version="0.16.0")


def test_a_secret_in_the_title_also_refuses() -> None:
    with pytest.raises(gates.SecretFoundError):
        rm.build_draft(
            title='API_KEY = "abcdef123456"', body="something broke", cohort_version="0.16.0"
        )


def test_an_ordinary_report_builds() -> None:
    draft = rm.build_draft(
        title="[engines] consult times out", body="Steps:\n1. run it\n2. wait\n",
        cohort_version="0.16.0",
    )
    assert draft.title == "[engines] consult times out"
    assert "Steps:" in draft.body
    assert draft.repo == rm.DEFAULT_UPSTREAM


def test_an_empty_report_is_refused() -> None:
    """A ticket with no body wastes the reader's time, which is the scarce resource."""
    with pytest.raises(rm.ReportError):
        rm.build_draft(title="x", body="   \n", cohort_version="0.16.0")
    with pytest.raises(rm.ReportError):
        rm.build_draft(title="  ", body="real body", cohort_version="0.16.0")


def test_the_environment_block_is_actionable_without_fingerprinting() -> None:
    """Version and OS decide whether a report is reproducible. Hostname, username and paths
    decide nothing and would be published to strangers."""
    block = rm.environment_block("0.16.0")

    assert "0.16.0" in block
    import getpass
    import socket

    assert socket.gethostname() not in block
    assert getpass.getuser() not in block
    assert "/home/" not in block


def test_the_environment_block_can_be_omitted() -> None:
    draft = rm.build_draft(
        title="t", body="b", cohort_version="0.16.0", include_environment=False
    )
    assert "Environment" not in draft.body


def test_a_feedback_entry_becomes_a_ticket_without_retyping_it() -> None:
    """The gap that let four reports sit unread: the user had already written them."""
    entry = (
        "---\nrating: down\narea: engines\ntimestamp: '2026-08-03T02:13:16+00:00'\n---\n"
        "Consult times out at the default token cap.\n\nMore detail here.\n"
    )
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "fb.md"
        path.write_text(entry, encoding="utf-8")
        title, body = rm.read_feedback_entry(path)

    assert title.startswith("[engines]")           # grouped by its declared area
    assert "Consult times out" in title
    assert "More detail here." in body             # the whole note survives
    assert "rating: down" not in body              # frontmatter is not the report


def test_a_feedback_entry_falls_back_to_command_then_agent_for_its_subject() -> None:
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "fb.md"
        path.write_text(
            "---\nrating: down\ncommand: engine consult\n---\nIt broke.\n", encoding="utf-8"
        )
        title, _ = rm.read_feedback_entry(path)
    assert title.startswith("[engine consult]")


def test_an_unreadable_feedback_entry_is_a_clean_error() -> None:
    from pathlib import Path

    with pytest.raises(rm.ReportError):
        rm.read_feedback_entry(Path("/nonexistent/feedback.md"))


def test_file_issue_timeout_warns_it_may_already_be_filed(monkeypatch) -> None:
    """`gh` can reach GitHub, have the issue created, and still time out waiting on the
    response — the request is not undone just because the local process gave up on it.
    A caller told this was a plain failure and retrying files the same issue twice, which
    is the duplicate-on-retry this message exists to prevent."""
    import subprocess

    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["gh"], timeout=rm._GH_TIMEOUT)

    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    draft = rm.build_draft(title="t", body="b", cohort_version="0.17.0")

    with pytest.raises(rm.ReportError) as excinfo:
        rm.file_issue(draft, gh="/usr/bin/gh")

    message = str(excinfo.value)
    assert "already have been filed" in message
    assert draft.repo in message


def test_file_issue_other_subprocess_failures_stay_generic(monkeypatch) -> None:
    """A non-timeout failure (`gh` missing, killed, etc.) never reached GitHub, so it must
    not carry the "may already be filed" warning meant only for the timeout case."""
    import subprocess

    def _raise_oserror(*args, **kwargs):
        raise OSError("no such file or directory")

    monkeypatch.setattr(subprocess, "run", _raise_oserror)
    draft = rm.build_draft(title="t", body="b", cohort_version="0.17.0")

    with pytest.raises(rm.ReportError) as excinfo:
        rm.file_issue(draft, gh="/usr/bin/gh")

    assert "already have been filed" not in str(excinfo.value)
