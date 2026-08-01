"""`AGENTS.md` is an install contract an AI agent follows unsupervised — so it must not drift.

Cohort is distributed by asking an agent to install it, not via a package index. That makes
this file part of the product surface: if it names a script that no longer exists, or loses
the warning about the same-named PyPI package, an agent will confidently do the wrong thing
and report success.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_AGENTS = REPO / "AGENTS.md"
_README = REPO / "README.md"


def test_agents_doc_exists() -> None:
    """It is the entry point for "hey, install Cohort for me"."""
    assert _AGENTS.is_file()


def test_agents_doc_warns_against_the_same_named_pypi_package() -> None:
    """`pip install cohort` installs an unrelated multi-agent tool. An agent told to
    "install cohort" will reach for exactly that, and it looks like success."""
    text = _AGENTS.read_text(encoding="utf-8")
    assert "pip install cohort" in text
    assert "different project" in text.lower()


def test_readme_carries_the_same_warning_at_the_install_step() -> None:
    """An agent (or human) that reads only the README must still hit it."""
    text = _README.read_text(encoding="utf-8")
    quickstart = text.index("## Quickstart")
    warning = text.index("Do not `pip install cohort`")
    # The warning must precede the commands it is warning about, not trail them.
    assert warning - quickstart < 400, "the pip warning drifted away from Quickstart"


def test_every_repo_path_the_agents_doc_tells_an_agent_to_run_exists() -> None:
    """The failure this guards: a renamed installer leaves the doc pointing at nothing, and
    an agent improvises — which is precisely what the doc tells it never to do."""
    text = _AGENTS.read_text(encoding="utf-8")
    referenced = set(re.findall(r"\./((?:installer|scripts)/[\w./-]+)", text))
    assert referenced, "expected the doc to reference the installer"
    missing = sorted(rel for rel in referenced if not (REPO / rel).exists())
    assert not missing, f"AGENTS.md references paths that do not exist: {missing}"


def test_agents_doc_states_the_egress_default_and_the_literal_opt_out_marker() -> None:
    """An agent installing this for someone else must be able to tell them that consult
    commands send source to a vendor by default — and the marker is literal, so quoting it
    approximately is useless."""
    text = _AGENTS.read_text(encoding="utf-8")
    assert "cohort:egress=deny" in text
    assert "by default" in text


# --------------------------------------------------------------------------- #
# Privacy — audit r3 never covered this dimension in three runs
# --------------------------------------------------------------------------- #


def test_scaffolded_gitignore_excludes_the_engine_transcripts() -> None:
    """Transcripts record what an external engine read and was sent — excerpts of the
    repo's source plus the model's analysis. They are a local audit trail, not shared
    context, and nothing else ignores them: without this a routine `git add -A` commits
    them, and a repo that later goes public publishes the lot.
    """
    from cohort.project import GITIGNORE_CONTENT

    ignored = {line.strip() for line in GITIGNORE_CONTENT.splitlines() if line.strip()}
    assert "engine-transcripts/" in ignored
    assert "state/" in ignored      # machine-local bookkeeping
    assert "compiled/" in ignored   # derived output


def test_scaffolded_gitignore_still_shares_the_context_it_is_meant_to_share() -> None:
    """The inverse failure: over-ignoring would silently stop the repo sharing the things
    Cohort exists to share, and nobody would notice until a teammate had no context."""
    from cohort.project import GITIGNORE_CONTENT

    ignored = {line.strip() for line in GITIGNORE_CONTENT.splitlines() if line.strip()}
    for shared in ("project_context.md", "sessions/", "cohort.toml"):
        assert shared not in ignored
