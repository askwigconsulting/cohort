"""`cohort setup` — the guided install interview (issue #51, phase 1).

Asks the three deterministic wiring questions a new install needs — *is there a
company office to point to*, *which IDEs*, *which agents* — then runs the same
compile-and-install pipeline `recompile` uses. Every question has a flag, so
CI/headless installs skip the interview entirely (`--non-interactive` accepts
the defaults for anything not flagged).

The open-ended tailoring conversation ("what do you do → draft new agents") is
NOT here: that is an LLM interview and lives in compiled canonical commands
(issue #51 phases 2–3). This module only wires facts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .compile import CompileError, compile_ide, planned_dests, write_staging
from .install import do_install, parse_ide, prompt_ide_selection
from .install_model import IDES, CohortPaths, resolve_mode
from .manifest import load_manifest
from .update import _git

COMPANY_REMOTE = "company"
CHIEF = "chief-of-staff"


class SetupError(Exception):
    """A refused setup request (bad roster name, unreachable company repo, …)."""


# --- roster subset ------------------------------------------------------------


def canonical_agents(source: Path) -> list[str]:
    """The agent names the source's canonical roster offers, sorted."""
    agents_dir = source / "canonical" / "agents"
    return sorted(p.stem for p in agents_dir.glob("*.md")) if agents_dir.exists() else []


def parse_roster(value: Optional[str], source: Path) -> Optional[list[str]]:
    """``--agents`` → a validated subset, or None for the full roster.

    ``all`` (or None) means the full roster; names are validated against the
    canonical agents so a typo fails loudly instead of silently shrinking the
    office.
    """
    if value is None or value.strip().lower() == "all":
        return None
    names = [n.strip() for n in value.split(",") if n.strip()]
    if not names:
        raise SetupError("empty --agents selection; use 'all' for the full roster")
    known = set(canonical_agents(source))
    unknown = sorted(set(names) - known)
    if unknown:
        raise SetupError(
            f"unknown agent(s) {', '.join(unknown)}; available: {', '.join(sorted(known))}"
        )
    deduped: list[str] = []
    for n in names:
        if n not in deduped:
            deduped.append(n)
    return deduped


def effective_roster(home: Path, flag_value: Optional[str], source: Path) -> Optional[list[str]]:
    """The roster to compile: the flag when given, else the persisted subset."""
    if flag_value is not None:
        return parse_roster(flag_value, source)
    manifest = load_manifest(CohortPaths(home).manifest)
    return list(manifest.roster) if manifest and manifest.roster else None


def persist_roster(home: Path, roster: Optional[list[str]]) -> None:
    """Record the tailored subset on the manifest so update-recompiles honor it."""
    paths = CohortPaths(home)
    manifest = load_manifest(paths.manifest)
    if manifest is None:
        return  # nothing installed yet; the install that follows will persist
    manifest.roster = list(roster) if roster else None
    manifest.persist(paths.manifest)


# --- company office wiring ------------------------------------------------------


def _write_update_config(home: Path, remote: str, branch: Optional[str]) -> Path:
    """Set ``[update] upstream_remote/branch`` in the global cohort.toml.

    A *surgical* line edit: only the two keys inside the ``[update]`` table are
    rewritten (or the table appended if absent). Every other line — comments,
    other tables, nested tables, arrays — is preserved verbatim, so a hand-added
    key is never dropped and the file always stays parseable (a corrupt rewrite
    would make ``_read_update_config`` fall back and silently ignore the company
    upstream just configured).
    """
    cfg = CohortPaths(home).cohort_home / "cohort.toml"
    existing = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    keys = {"upstream_remote": remote}
    if branch:
        keys["upstream_branch"] = branch
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(_set_update_table(existing, keys), encoding="utf-8")
    return cfg


def _toml_basic_string(value: str) -> str:
    """Escape a scalar as a TOML basic string (quotes/backslashes/controls)."""
    out = value.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{out}"'


def _set_update_table(text: str, keys: dict[str, str]) -> str:
    """Return ``text`` with the ``[update]`` table's ``keys`` set, all else intact."""
    assignments = [f"{k} = {_toml_basic_string(v)}" for k, v in keys.items()]
    lines = text.splitlines()
    out: list[str] = []
    in_update = False
    seen_update = False
    for line in lines:
        stripped = line.strip()
        is_header = stripped.startswith("[") and stripped.endswith("]")
        if in_update and is_header:
            in_update = False  # leaving the [update] table → emit our keys first
            out.extend(assignments)
        if is_header and stripped == "[update]":
            in_update, seen_update = True, True
            out.append(line)
            continue
        if in_update:
            key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
            if key in keys:
                continue  # drop the old assignment; the new one is emitted on exit
        out.append(line)
    if in_update:  # file ended inside [update]
        out.extend(assignments)
    if not seen_update:
        if out and out[-1].strip():
            out.append("")
        out.append("[update]")
        out.extend(assignments)
    return "\n".join(out) + "\n"


def wire_company(source: Path, home: Path, url: str, branch: Optional[str]) -> dict[str, Any]:
    """Point update-checks and upstream proposals at the company's Cohort repo.

    Adds (or re-points) a ``company`` remote on the source clone and records it
    as ``[update] upstream_remote``. The clone itself is untouched — the company
    repo becomes the office's upstream, not its working tree.
    """
    if url.startswith("-"):
        raise SetupError(f"{url!r} is not a valid repo URL")  # never an option to git argv
    rc, _ = _git(source, "remote", "get-url", COMPANY_REMOTE)
    verb = "set-url" if rc == 0 else "add"
    rc, _ = _git(source, "remote", verb, "--", COMPANY_REMOTE, url)
    if rc != 0:
        raise SetupError(f"could not {verb} the {COMPANY_REMOTE!r} remote on {source}")
    cfg = _write_update_config(home, COMPANY_REMOTE, branch)
    return {"remote": COMPANY_REMOTE, "url": url, "branch": branch, "config": str(cfg)}


# --- the interview ------------------------------------------------------------


def prompt_setup_inputs(source: Path) -> dict[str, Optional[str]]:
    """Interactive collection of the wiring answers (patchable in tests).

    Returns flag-shaped values; empty answers mean "default" (no company repo,
    full roster). IDE selection reuses the numbered picker.
    """
    print("Cohort setup — three questions; Enter accepts the default.\n")
    url = input(
        "Company Cohort repo URL, if your org maintains a shared office (Enter = none): "
    ).strip()
    branch = ""
    if url:
        branch = input("Company default branch (Enter = main): ").strip()
    picked = prompt_ide_selection()
    ide = ",".join(picked) if picked else None
    roster = canonical_agents(source)
    print("\nOffice roster:")
    for name in roster:
        print(f"  - {name}")
    agents = input(
        "Agents to install, comma-separated (Enter = all): "
    ).strip()
    return {
        "company_url": url or None,
        "company_branch": branch or None,
        "ide": ide,
        "agents": agents or None,
    }


def do_setup(
    home: Path,
    source: Path,
    ide: Optional[str],
    agents: Optional[str],
    company_url: Optional[str],
    company_branch: Optional[str],
    copy: bool,
    force: bool,
    dry_run: bool,
) -> dict[str, Any]:
    """Wire the answers, then compile + install (the recompile pipeline)."""
    selection = parse_ide(ide) if ide else list(IDES)
    roster = parse_roster(agents, source)
    only = frozenset(roster) if roster is not None else None
    warnings: list[str] = []
    if roster is not None and CHIEF not in roster:
        warnings.append(
            f"{CHIEF} is not in the selected roster; cross-agent routing is degraded "
            f"until you add it (cohort setup --agents ...,{CHIEF})."
        )

    company: Optional[dict[str, Any]] = None
    if company_url and not dry_run:
        company = wire_company(source, home, company_url, company_branch)
    elif company_url:
        company = {"remote": COMPANY_REMOTE, "url": company_url,
                   "branch": company_branch, "dry_run": True}

    paths = CohortPaths(home)
    try:
        overlay = paths.my
        results = [
            compile_ide(source, i, scope="global", only_agents=only, overlay=overlay)
            for i in selection
        ]
    except CompileError as exc:
        raise SetupError(str(exc)) from exc
    if not dry_run:
        for result in results:
            write_staging(paths, result)
    report = do_install(
        home=home, selection=selection, mode=resolve_mode(copy), force=force,
        source=source, dry_run=dry_run,
        prune_stale=True,
        fresh_dests=planned_dests(paths, results),
        fresh_ides={r.ide for r in results if r.staged},
    )
    if not dry_run:
        persist_roster(home, roster)
    return {
        "action": "setup",
        "dry_run": dry_run,
        "ides": selection,
        "roster": roster or "all",
        "company": company,
        "warnings": warnings,
        "install": report.to_dict(),
    }
