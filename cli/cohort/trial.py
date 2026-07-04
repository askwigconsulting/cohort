"""`cohort try` — preview a compiled agent before it joins any roster (#68).

``--dry-run`` shows file *operations*; this shows *behavior*: the exact bytes
Claude Code loads as the agent's system prompt (frontmatter with the read-only
tool set resolved, the office header, and the body), after full schema
validation — so a broken draft is caught here, not after it lands sight-unseen.

With ``--place`` it also installs the agent as a project specialist in the
current repo (project subagents override user-level), a free sandbox tier: try
it live in this repo's Claude session, then keep it (``add-agent``) or drop it
(``remove-specialist``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .adapters.claude import MarkerError, render_agent
from .ir import build_ir
from .install_model import CohortPaths
from .loader import load_artifact
from .schema import validate_frontmatter


class TryError(Exception):
    """A refused ``cohort try`` (unresolved target, wrong kind, invalid artifact)."""


def _resolve_target(source: Path, home: Path, target: str) -> tuple[Path, str]:
    """Locate the agent to try, returning ``(path, layer)``.

    Resolution order: an explicit file path, then the office layer
    (``<source>/canonical/agents``), then my office (``~/.cohort/my``). A bare
    name never reaches the filesystem as anything but ``<dir>/<name>.md``.
    """
    p = Path(target).expanduser()
    if p.suffix == ".md" or "/" in target or p.exists():
        if not p.is_file():
            raise TryError(f"no such file: {target}")
        return p, "file"
    office = source / "canonical" / "agents" / f"{target}.md"
    if office.is_file():
        return office, "office"
    mine = CohortPaths.for_global(home).my / "canonical" / "agents" / f"{target}.md"
    if mine.is_file():
        return mine, "my"
    raise TryError(
        f"no agent {target!r} found — pass a file path, or an agent name in the "
        f"office roster or my office"
    )


def do_try(
    source: Path,
    home: Path,
    target: str,
    *,
    place: bool = False,
    repo: Optional[Path] = None,
) -> dict[str, Any]:
    """Render + validate the target agent; optionally sandbox it into ``repo``.

    Read-only unless ``place`` is set (which reuses the project add-specialist
    path — the same human-gated, reversible install)."""
    path, layer = _resolve_target(source, home, target)
    parsed = load_artifact(path)
    if parsed.load_error is not None:
        raise TryError(f"{path.name} does not parse: {parsed.load_error.message}")
    fm = parsed.frontmatter or {}
    if fm.get("kind") != "agent":
        raise TryError(f"{path.name} is a {fm.get('kind', 'non-agent')!r}, not an agent")
    # Validate against the artifact's own declared name, not the file stem — a
    # scratch draft may be named draft.md; the file-must-match-name rule is an
    # authoring concern (add-agent enforces it), not a preview concern.
    errors = validate_frontmatter(fm, str(fm.get("name") or path.stem))
    if errors:
        raise TryError(f"{path.name} is invalid: {errors[0].code} {errors[0].message}")
    ir = build_ir(fm, parsed.body or "", path)
    try:
        rendered = render_agent(ir).content.decode("utf-8")
    except MarkerError as exc:
        raise TryError(str(exc))

    tools_line = next(
        (ln for ln in rendered.splitlines() if ln.startswith("tools:")), "tools: (none)"
    )
    result: dict[str, Any] = {
        "action": "try", "name": ir.name, "layer": layer, "source": str(path),
        "tools": tools_line.split(":", 1)[1].strip(),
        "rendered": rendered,
    }
    if place:
        from .specialists import AddSpecialistError, do_add_specialist  # lazy: cycle

        if repo is None:
            raise TryError("--place needs a repository (run it inside a Cohort project)")
        try:
            report = do_add_specialist(
                repo, home, ir.name, ir.display_name or ir.name,
                ir.fields.get("department", "Trial"), ir.description, dry_run=False,
                body=ir.body,
            )
        except AddSpecialistError as exc:
            raise TryError(f"could not sandbox {ir.name!r}: {exc}")
        result["placed"] = report.get("compiled")
    return result
