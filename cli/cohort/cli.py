"""The ``cohort`` Typer application: ``validate``, ``install``, ``uninstall``.

Commands map domain exceptions to the exit triad (0 success · 1 refused/failed ·
2 usage) and reuse the Phase 0 structured-log format.
"""

from __future__ import annotations

import sys


def _force_utf8_io() -> None:
    """Force UTF-8 on stdout/stderr so Cohort's Unicode output (→, …, —) never
    crashes on a legacy Windows console (whose default cp1252 can't encode it).

    Must run before `typer`/`rich` are imported: Rich's Console inspects the
    stream's encoding/terminal capabilities at construction time (which for
    Typer's app-level Console happens at import time, not at command-run
    time), so reconfiguring the stream later — even "early" in a command
    callback or entry point — is too late to change what Rich already
    decided. No-op where the stream can't be reconfigured (e.g. a
    pytest-captured stream).
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (ValueError, OSError):
                pass


_force_utf8_io()

import json as _json
import re
import time
from pathlib import Path
from typing import Optional

import typer

from . import __version__
from .compile import CompileError, CompileResult, compile_ide, planned_dests, write_staging
from .executor import ClobberRefused
from .install import (
    CancelledSelection,
    InstallReport,
    UninstallReport,
    UsageError,
    _isatty as _install_isatty,
    do_install,
    do_uninstall,
    resolve_selection,
)
from .improve import (
    FeedbackError,
    do_feedback,
    do_propose_improvement,
    do_submit_proposals,
    validate_enrichment_body,
)
from .install_model import CohortPaths, resolve_mode
from .lint import run_lint
from .office_setup import (
    SetupError,
    do_setup,
    effective_roster,
    persist_roster,
    prompt_setup_inputs,
)
from .update import UpdateResult, do_relink, do_rollback, do_update, do_update_check
from .logconf import emit_log
from .project import (
    do_context_refresh,
    do_deinit,
    do_init,
    do_snapshot,
    find_repo_root,
    list_projects,
    session_capture,
    staleness_check,
)
from .reports import do_report
from .distill import DEFAULT_DAYS, do_distill
from .adopt import AdoptError, do_adopt
from .roster import (
    AddAgentError,
    AddMemoryError,
    AuthoringError,
    EditError,
    PersonalizeError,
    do_add_agent,
    do_add_command,
    do_add_hook,
    do_add_memory,
    do_add_skill,
    do_edit,
    do_personalize,
    prompt_add_agent_inputs,
)
from .schema import TreeResult, validate_tree
from .source import SourceUnresolved, resolve_source
from .specialists import (
    AddSpecialistError,
    PromoteError,
    RemoveSpecialistError,
    do_add_specialist,
    do_promote,
    do_remove_specialist,
    prompt_add_specialist_inputs,
)
from .dashboard import do_dashboard
from .status import do_status
from .trial import TryError, do_try
from .engines import ENGINES, UnknownEngineError, get_engine
from .engines import xai as engine_xai

app = typer.Typer(
    add_completion=False,
    help="Cohort — portable, multi-IDE agentic office harness.",
    no_args_is_help=True,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


# NEL + U+2028/U+2029 included alongside ASCII controls (matches roster.py).
_UNTRUSTED_CONTROL = re.compile("[\x00-\x1f\x7f\x85\u2028\u2029]")


def _escape_untrusted(text: str) -> str:
    """Escape control characters in untrusted text (e.g. filenames) before it
    reaches the terminal, so a crafted name can't overwrite or forge output."""
    return _UNTRUSTED_CONTROL.sub(lambda m: repr(m.group())[1:-1], text)


@app.callback()
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", help="Show the Cohort version and exit.",
        is_eager=True, callback=_version_callback,
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the file operations a command would perform without making changes.",
    ),
) -> None:
    """Global options shared by every command."""
    ctx.obj = {"dry_run": dry_run}


# --- validate (Phase 0) -----------------------------------------------------


def _print_validate_human(tree: TreeResult) -> None:
    for r in tree.results:
        if r.status == "pass":
            typer.echo(f"OK {r.path}")
        else:
            typer.echo(f"FAIL {r.path}")
            for err in r.errors:
                field = f" field={err.field}" if err.field else ""
                typer.echo(f"  {err.code}{field}: {err.message}")
    s = tree.summary
    typer.echo(f"{s['valid']} valid, {s['invalid']} invalid")


@app.command()
def validate(
    ctx: typer.Context,
    path: Path = typer.Argument(
        Path("./canonical"),
        help="Directory of canonical artifacts to validate (default: ./canonical).",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of human output."
    ),
) -> None:
    """Schema-validate every canonical artifact under PATH (exit 0/1/2)."""
    if not path.exists():
        typer.echo(f"error: path does not exist: {path}", err=True)
        raise typer.Exit(code=2)
    if not path.is_dir():
        typer.echo(f"error: path is not a directory: {path}", err=True)
        raise typer.Exit(code=2)

    start = time.perf_counter()
    tree = validate_tree(path)
    elapsed_ms = int((time.perf_counter() - start) * 1000)

    for r in tree.results:
        emit_log(
            component="validate",
            action="validate_artifact",
            scope=r.scope or "-",
            ide="-",
            artifact=r.name or r.path.name,
            status=r.status,
            duration_ms=elapsed_ms,
        )

    if json_output:
        typer.echo(_json.dumps(tree.to_dict(), indent=2))
    else:
        _print_validate_human(tree)
    raise typer.Exit(code=0 if tree.valid else 1)


@app.command()
def lint(
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of human output."
    ),
) -> None:
    """Check human-facing docs against the canonical filesystem (exit 0/1).

    Guards the drift the golden locks don't: counts stated in prose (e.g. an
    "N-agent roster" line) must match the real number of canonical artifacts.
    """
    repo_root = Path.cwd()
    findings = run_lint(repo_root)
    if json_output:
        typer.echo(
            _json.dumps(
                [{"file": f.file, "line": f.line, "message": f.message} for f in findings],
                indent=2,
            )
        )
    elif findings:
        for f in findings:
            typer.echo(f"{f.file}:{f.line}: {_escape_untrusted(f.message)}", err=True)
        typer.echo(f"lint: {len(findings)} doc-parity issue(s)", err=True)
    else:
        typer.echo("lint: docs match canonical (0 issues)")
    raise typer.Exit(code=1 if findings else 0)


@app.command()
def reference(
    source: Optional[str] = typer.Option(None, "--source", help="Cohort source root (default: auto-resolve)."),
) -> None:
    """Regenerate the quick-reference (docs/quick-reference.html + .pdf) from canonical.

    Run this after adding or renaming a command or skill; a parity test fails CI when the
    committed reference is missing one, so it stays current.
    """
    from . import reference as _ref

    source_path = resolve_source(source)
    html_path, pdf_path = _ref.write_reference(source_path, source_path / "docs")
    typer.echo(f"wrote {html_path}")
    if pdf_path is not None:
        typer.echo(f"wrote {pdf_path}")
    else:
        typer.echo(
            "note: no Chrome found — HTML written but PDF not rendered; install "
            "chromium/google-chrome and re-run to refresh the PDF.",
            err=True,
        )
    raise typer.Exit(code=0)


# --- install / uninstall (Phase 1) -----------------------------------------


def _log_records(component: str, action: str, records, elapsed_ms: int) -> None:
    for r in records:
        emit_log(
            component=component,
            action=action,
            scope="global",
            ide=r.op.ide,
            artifact=r.op.dest,
            status=r.status,
            duration_ms=elapsed_ms,
        )


def _print_install_human(report: InstallReport) -> None:
    for r in report.records:
        src = f" → {r.op.src}" if r.op.src else ""
        typer.echo(f"{r.status:>9}  {r.op.op} {r.op.dest}{src}")
    s = report.summary
    typer.echo(
        f"installed: {', '.join(report.ides) or '-'} · "
        f"applied: {s['applied']} · skipped: {s['skipped']} · backed_up: {s['backed_up']}"
    )


def _print_uninstall_human(report: UninstallReport) -> None:
    if report.nothing:
        typer.echo("nothing installed")
        return
    for r in report.records:
        typer.echo(f"{r.status:>11}  {r.op.op} {r.op.dest}")
    s = report.summary
    typer.echo(
        f"removed: {s['removed']} · restored: {s['restored']} · dirs_removed: {s['dirs_removed']}"
    )


@app.command()
def install(
    ctx: typer.Context,
    ide: Optional[str] = typer.Option(None, "--ide", help="claude,codex,cursor or all (codex/cursor experimental)."),
    copy: bool = typer.Option(False, "--copy", help="Materialize copies instead of symlinks."),
    force: bool = typer.Option(False, "--force", help="Back up and replace foreign files at a dest."),
    source: Optional[str] = typer.Option(None, "--source", help="Path to the Cohort source repo."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan; change nothing."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Install Cohort's global home and the selected IDE experiences."""
    effective_dry_run = dry_run or ctx.obj.get("dry_run", False)
    try:
        selection = resolve_selection(ide)
        source_path = resolve_source(source)
    except CancelledSelection:
        typer.echo("cancelled")
        raise typer.Exit(code=0)
    except (UsageError, SourceUnresolved) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)

    mode = resolve_mode(copy)
    if mode == "copy" and not copy:
        typer.echo(
            "note: Windows detected — placing copies instead of symlinks "
            "(symlinks need Developer Mode/admin).",
            err=True,
        )
    start = time.perf_counter()
    try:
        report = do_install(
            home=Path.home(),
            selection=selection,
            mode=mode,
            force=force,
            source=source_path,
            dry_run=effective_dry_run,
        )
    except ClobberRefused as exc:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        typer.echo(f"error: {exc}", err=True)
        typer.echo("re-run with --force to back up and replace them.", err=True)
        for c in exc.clobbers:
            emit_log(
                component="install",
                action="install",
                scope="global",
                ide=c.op.ide,
                artifact=c.op.dest,
                status="refused",
                duration_ms=elapsed_ms,
            )
        raise typer.Exit(code=1)

    elapsed_ms = int((time.perf_counter() - start) * 1000)
    _log_records("install", "install", report.records, elapsed_ms)
    if json_output:
        typer.echo(_json.dumps(report.to_dict(), indent=2))
    else:
        _print_install_human(report)
        _warn_divergence(report)
        if report.staging_missing:
            typer.echo(
                f"note: no compiled artifacts for {', '.join(report.staging_missing)}; "
                f"run `cohort recompile` to compile and place them.",
                err=True,
            )
    raise typer.Exit(code=0)


# --- compile / recompile (Phase 2) -----------------------------------------


def _print_compile_human(results: list[CompileResult]) -> None:
    for result in results:
        for sf in result.staged:
            typer.echo(f"  staged  {result.ide}/{sf.staged_rel}")
        typer.echo(f"compiled: {result.ide} · staged: {len(result.staged)}")
        if result.overridden:
            typer.echo(
                f"note: my office overrides: {', '.join(result.overridden)}", err=True
            )
        if result.scope_filtered:
            names = ", ".join(result.scope_filtered)
            typer.echo(
                f"note: not compiled at this tier (wrong scope): {names}. Move the "
                f"artifact to the other tier's canonical, or fix its `scope:`.",
                err=True,
            )


def _resolve_for_compile(ide: Optional[str], source: Optional[str]):
    """Shared selection + source resolution for compile/recompile."""
    selection = resolve_selection(ide)
    source_path = resolve_source(source)
    return selection, source_path


@app.command()
def compile(  # noqa: A001 - matches the user-facing command name
    ctx: typer.Context,
    ide: Optional[str] = typer.Option(None, "--ide", help="claude,codex,cursor or all (codex/cursor experimental)."),
    agents: Optional[str] = typer.Option(None, "--agents", help="Agent subset (comma-separated) or 'all'."),
    source: Optional[str] = typer.Option(None, "--source", help="Path to the Cohort source repo."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Render to memory; write no staging."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Render canonical artifacts into staging (no install)."""
    effective_dry_run = dry_run or ctx.obj.get("dry_run", False)
    try:
        selection, source_path = _resolve_for_compile(ide, source)
        roster = effective_roster(Path.home(), agents, source_path)
    except CancelledSelection:
        typer.echo("cancelled")
        raise typer.Exit(code=0)
    except (UsageError, SourceUnresolved, SetupError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)

    if agents is not None:
        typer.echo(
            "note: `compile --agents` only shrinks staging; it is not persisted. Run "
            "`cohort recompile --agents ...` (or `cohort setup`) to place and remember a subset.",
            err=True,
        )
    paths = CohortPaths(Path.home())
    only = frozenset(roster) if roster is not None else None
    start = time.perf_counter()
    try:
        overlay = paths.my
        results = [
            compile_ide(source_path, i, scope="global", only_agents=only, overlay=overlay)
            for i in selection
        ]
    except CompileError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    if not effective_dry_run:
        for result in results:
            write_staging(paths, result)
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    for result in results:
        emit_log(
            component="compile",
            action="compile",
            scope="global",
            ide=result.ide,
            artifact=str(paths.compiled_ide(result.ide)),
            status="dry-run" if effective_dry_run else "staged",
            duration_ms=elapsed_ms,
        )
    if json_output:
        typer.echo(_json.dumps([r.to_dict() for r in results], indent=2))
    else:
        _print_compile_human(results)
    raise typer.Exit(code=0)


@app.command()
def recompile(
    ctx: typer.Context,
    ide: Optional[str] = typer.Option(None, "--ide", help="IDEs to recompile + install (codex/cursor experimental)."),
    agents: Optional[str] = typer.Option(None, "--agents", help="Agent subset (comma-separated) or 'all'; persists."),
    copy: bool = typer.Option(False, "--copy", help="Materialize copies instead of symlinks."),
    force: bool = typer.Option(False, "--force", help="Back up and replace foreign files at a dest."),
    source: Optional[str] = typer.Option(None, "--source", help="Path to the Cohort source repo."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Render to a temp + show the plan; write nothing."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Compile canonical → staging, then install (idempotent when unchanged)."""
    effective_dry_run = dry_run or ctx.obj.get("dry_run", False)
    try:
        selection, source_path = _resolve_for_compile(ide, source)
        roster = effective_roster(Path.home(), agents, source_path)
    except CancelledSelection:
        typer.echo("cancelled")
        raise typer.Exit(code=0)
    except (UsageError, SourceUnresolved, SetupError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)

    paths = CohortPaths(Path.home())
    only = frozenset(roster) if roster is not None else None
    try:
        overlay = paths.my
        results = [
            compile_ide(source_path, i, scope="global", only_agents=only, overlay=overlay)
            for i in selection
        ]
    except CompileError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    # dry-run recompile renders without writing staging (so install sees no new
    # staging) and prints the plan it *would* apply.
    if not effective_dry_run:
        for result in results:
            write_staging(paths, result)
    for result in results:
        if result.scope_filtered:
            typer.echo(
                f"note: not compiled at this tier (wrong scope): "
                f"{', '.join(result.scope_filtered)}. Move the artifact to the other "
                f"tier's canonical, or fix its `scope:`.",
                err=True,
            )
            break  # the same set repeats per IDE; say it once
    for result in results:
        if result.overridden:
            typer.echo(
                f"note: my office overrides: {', '.join(result.overridden)}", err=True
            )
            break

    mode = resolve_mode(copy)
    if mode == "copy" and not copy:
        typer.echo(
            "note: Windows detected — placing copies instead of symlinks "
            "(symlinks need Developer Mode/admin).",
            err=True,
        )
    # Prune what this fresh compile no longer produces (a shrunk roster, a
    # deleted-upstream artifact). Dests come from the in-memory results so the
    # dry-run plan shows removals it would otherwise miss (staging isn't written).
    fresh_dests = planned_dests(paths, results)
    fresh_ides = {r.ide for r in results if r.staged}
    start = time.perf_counter()
    try:
        report = do_install(
            home=Path.home(),
            selection=selection,
            mode=mode,
            force=force,
            source=source_path,
            dry_run=effective_dry_run,
            prune_stale=True,
            fresh_dests=fresh_dests,
            fresh_ides=fresh_ides,
        )
    except ClobberRefused as exc:
        typer.echo(f"error: {exc}", err=True)
        typer.echo("re-run with --force to back up and replace them.", err=True)
        raise typer.Exit(code=1)
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    if not effective_dry_run:
        persist_roster(Path.home(), roster)  # update-recompiles honor the subset
    _log_records("compile", "recompile", report.records, elapsed_ms)
    if json_output:
        typer.echo(_json.dumps(report.to_dict(), indent=2))
    else:
        _print_install_human(report)
        _warn_divergence(report)
    raise typer.Exit(code=0)


@app.command()
def setup(
    ctx: typer.Context,
    ide: Optional[str] = typer.Option(None, "--ide", help="IDEs to install (comma-separated or 'all'; default: all)."),
    agents: Optional[str] = typer.Option(None, "--agents", help="Agent subset (comma-separated) or 'all'."),
    company_url: Optional[str] = typer.Option(None, "--company-url", help="Your org's Cohort repo (shared office upstream)."),
    company_branch: Optional[str] = typer.Option(None, "--company-branch", help="Company repo default branch."),
    copy: bool = typer.Option(False, "--copy", help="Materialize copies instead of symlinks."),
    force: bool = typer.Option(False, "--force", help="Back up and replace foreign files at a dest."),
    non_interactive: bool = typer.Option(False, "--non-interactive", help="Skip the interview; use flags/defaults."),
    source: Optional[str] = typer.Option(None, "--source", help="Path to the Cohort source repo."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Guided first-run interview — company office, IDEs, roster — then compile + install."""
    effective_dry_run = dry_run or ctx.obj.get("dry_run", False)
    try:
        source_path = resolve_source(source)
    except SourceUnresolved as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)
    flagged = non_interactive or any(v is not None for v in (ide, agents, company_url))
    if not flagged and _install_isatty():
        answers = prompt_setup_inputs(source_path)
        ide, agents = answers["ide"], answers["agents"]
        company_url, company_branch = answers["company_url"], answers["company_branch"]
    try:
        report = do_setup(
            home=Path.home(), source=source_path, ide=ide, agents=agents,
            company_url=company_url, company_branch=company_branch,
            copy=copy, force=force, dry_run=effective_dry_run,
        )
    except (SetupError, UsageError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)
    except ClobberRefused as exc:
        typer.echo(f"error: {exc}", err=True)
        typer.echo("re-run with --force to back up and replace them.", err=True)
        raise typer.Exit(code=1)
    for warning in report["warnings"]:
        typer.echo(f"warning: {warning}", err=True)
    if json_output:
        typer.echo(_json.dumps(report, indent=2))
    else:
        prefix = "(dry-run) " if report["dry_run"] else ""
        roster = report["roster"]
        roster_text = "full roster" if roster == "all" else f"{len(roster)} agents ({', '.join(roster)})"
        typer.echo(f"setup: {prefix}{', '.join(report['ides'])} · {roster_text}")
        if report["company"]:
            typer.echo(f"setup: company office upstream → {report['company']['url']}")
        summary = report["install"]["summary"]
        removed = f" · removed {summary['removed']}" if summary.get("removed") else ""
        typer.echo(
            f"setup: install applied {summary['applied']} · skipped {summary['skipped']}{removed}"
        )
    raise typer.Exit(code=0)


@app.command()
def relink(
    source: Optional[str] = typer.Option(None, "--source", help="Path to the Cohort source repo."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Re-point a moved/renamed install at the source and recompile installed IDEs."""
    try:
        source_path = resolve_source(source)
    except SourceUnresolved as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)
    result = do_relink(source_path, Path.home())
    if json_output:
        typer.echo(_json.dumps(result, indent=2))
    elif result["refused"]:
        typer.echo(f"error: {result['refused']}", err=True)
    elif result["recompiled_ides"]:
        typer.echo(f"Relinked at {source_path}; recompiled: {', '.join(result['recompiled_ides'])}.")
    else:
        typer.echo("No installed IDEs to relink (run `cohort install`).")
    raise typer.Exit(code=1 if result["refused"] else 0)


def _warn_divergence(report: InstallReport) -> None:
    if report.diverged:
        typer.echo(
            f"warning: left {report.diverged} user-edited merge entr"
            f"{'y' if report.diverged == 1 else 'ies'} untouched "
            f"(divergence). Run `cohort recompile --force` to restore Cohort's entries.",
            err=True,
        )


_UPDATE_FAILED = (
    "unavailable", "diverged", "dirty", "unsigned", "pull_failed", "pip_failed",
    "reset_failed", "no_rollback_point", "unknown_ref", "not_earlier",
)


def _printable(line: str) -> str:
    """Drop control/escape bytes so an attacker-influenced upstream commit message
    can't smuggle terminal escape sequences into our output."""
    return "".join(c for c in line if c.isprintable())


def _print_update_human(result: UpdateResult) -> None:
    if result.status == "up_to_date":
        typer.echo(f"Cohort is up to date with {result.upstream}.")
        return
    if result.status in _UPDATE_FAILED:
        typer.echo(f"error: {result.detail}", err=True)
        return
    if result.status == "recompile_refused":
        # The fast-forward (and any pip reinstall) already landed — only re-placing
        # the IDE artifacts is pending. Lead with that so it doesn't read as a
        # total failure, then surface the actionable guidance to stderr.
        pip = " (package reinstalled)" if result.pip_reinstalled else ""
        typer.echo(f"Updated Cohort to {result.target}{pip}.")
        typer.echo(f"warning: {result.detail}", err=True)
        return
    plural = "s" if result.behind != 1 else ""
    head = "Would update" if result.status == "dry_run" else "Updated"
    typer.echo(
        f"{head} Cohort: {result.behind} commit{plural} behind {result.upstream} "
        f"({result.current} → {result.target})."
    )
    if result.commits:
        typer.echo("Incoming commits:")
        for line in result.commits[:15]:
            typer.echo(f"  {_printable(line)}")
        if len(result.commits) > 15:
            typer.echo(f"  … and {len(result.commits) - 15} more")
    if result.changed_files:
        typer.echo(f"Changed files: {len(result.changed_files)}")
    if result.status == "dry_run":
        typer.echo("Dry run — nothing changed. Re-run `cohort update` to apply.")
        return
    if result.pip_reinstalled:
        typer.echo("Reinstalled the cohort package (pyproject.toml changed).")
    if result.recompiled_ides:
        typer.echo(f"Recompiled: {', '.join(result.recompiled_ides)}")
    else:
        typer.echo("No installed IDEs to recompile (run `cohort install`).")
    # The renderer-staleness warning was machine-only (result.detail, surfaced in
    # --json). Surface it to the human too: a compiler/adapter change means the
    # recompile just run may have used stale logic until a fresh process recompiles.
    if result.status == "updated" and result.detail:
        typer.echo(f"warning: {result.detail}", err=True)


def _print_rollback_human(result: UpdateResult) -> None:
    if result.status == "up_to_date":
        typer.echo(result.detail or "Already at that version.")
        return
    if result.status in _UPDATE_FAILED:
        typer.echo(f"error: {result.detail}", err=True)
        return
    if result.status == "recompile_refused":
        pip = " (package reinstalled)" if result.pip_reinstalled else ""
        typer.echo(f"Rolled Cohort back to {result.target}{pip}.")
        typer.echo(f"warning: {result.detail}", err=True)
        return
    n = len(result.commits)
    head = "Would roll back" if result.status == "dry_run" else "Rolled back"
    typer.echo(
        f"{head} Cohort: {result.current} → {result.target} "
        f"(discards {n} commit{'s' if n != 1 else ''})."
    )
    if result.commits:
        typer.echo("Discarded commits:")
        for line in result.commits[:15]:
            typer.echo(f"  {_printable(line)}")
        if len(result.commits) > 15:
            typer.echo(f"  … and {len(result.commits) - 15} more")
    if result.status == "dry_run":
        typer.echo("Dry run — nothing changed. Re-run `cohort rollback` to apply. "
                   "(A later `cohort update` restores what a rollback discards.)")
        return
    if result.pip_reinstalled:
        typer.echo("Reinstalled the cohort package (pyproject.toml changed).")
    if result.recompiled_ides:
        typer.echo(f"Recompiled: {', '.join(result.recompiled_ides)}")


@app.command()
def rollback(
    ctx: typer.Context,
    to: Optional[str] = typer.Option(
        None, "--to", help="Tag or ref to roll back to (e.g. v0.2.0); "
        "default: the version before the last `cohort update`.",
    ),
    source: Optional[str] = typer.Option(None, "--source", help="Path to the Cohort source repo."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview the rollback; change nothing."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Roll the Cohort clone back to an earlier version and recompile (reversible)."""
    effective_dry_run = dry_run or ctx.obj.get("dry_run", False)
    try:
        source_path = resolve_source(source)
    except SourceUnresolved as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)

    start = time.perf_counter()
    result = do_rollback(source_path, Path.home(), to=to, dry_run=effective_dry_run)
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    emit_log(
        component="update", action="rollback", scope="global", ide="-",
        artifact=str(source_path), status=result.status, duration_ms=elapsed_ms,
    )
    if json_output:
        typer.echo(_json.dumps(result.to_dict(), indent=2))
    else:
        _print_rollback_human(result)
    raise typer.Exit(code=0 if result.ok else 1)


@app.command()
def update(
    ctx: typer.Context,
    source: Optional[str] = typer.Option(None, "--source", help="Path to the Cohort source repo."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview the update; change nothing."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Update Cohort to the latest upstream and recompile installed IDEs (ff-only)."""
    effective_dry_run = dry_run or ctx.obj.get("dry_run", False)
    try:
        source_path = resolve_source(source)
    except SourceUnresolved as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)

    start = time.perf_counter()
    result = do_update(source_path, Path.home(), dry_run=effective_dry_run)
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    emit_log(
        component="update",
        action="update",
        scope="global",
        ide="-",
        artifact=str(source_path),
        status=result.status,
        duration_ms=elapsed_ms,
    )
    if json_output:
        typer.echo(_json.dumps(result.to_dict(), indent=2))
    else:
        _print_update_human(result)
    raise typer.Exit(code=0 if result.ok else 1)


@app.command()
def uninstall(
    ctx: typer.Context,
    ide: Optional[str] = typer.Option(None, "--ide", help="Reverse only these IDEs' ops."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan; change nothing."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Reverse a Cohort install (whole, or a per-IDE slice with --ide)."""
    effective_dry_run = dry_run or ctx.obj.get("dry_run", False)
    selection = None
    if ide is not None:
        try:
            from .install import parse_ide

            selection = parse_ide(ide)
        except UsageError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2)

    start = time.perf_counter()
    report = do_uninstall(home=Path.home(), selection=selection, dry_run=effective_dry_run)
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    _log_records("install", "uninstall", report.records, elapsed_ms)

    if json_output:
        typer.echo(_json.dumps(report.to_dict(), indent=2))
    else:
        _print_uninstall_human(report)
    raise typer.Exit(code=0)


# --- project scope (Phase 4) -----------------------------------------------

context_app = typer.Typer(add_completion=False, help="Project context commands.")
app.add_typer(context_app, name="context")

my_office_app = typer.Typer(add_completion=False, help="Personal-layer (my office) commands.")
app.add_typer(my_office_app, name="my-office")

engine_app = typer.Typer(add_completion=False, help="External-engine (RFC 0004) commands.")
app.add_typer(engine_app, name="engine")

office_app = typer.Typer(
    add_completion=False, help="Office-layer (shared source) quarantine commands."
)
app.add_typer(office_app, name="office")


def _repo_has_egress_provenance(cwd: Path) -> bool:
    """True if ``cwd`` sits inside a repository context — a ``.git`` or ``.cohort``
    ancestor. When neither exists (e.g. a bare ``/tmp`` working dir), there is no repo
    whose ``.cohort/project_context.md`` could carry an egress opt-out, so a piped
    payload's provenance cannot be checked and engine egress must fail closed (F5)."""
    cwd = Path(cwd).resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists() or (candidate / ".cohort").exists():
            return True
    return False


def _guard_engine_egress_provenance(allow_egress: bool) -> None:
    """Fail closed (RFC 0004 F5) when the working directory has no repository context
    to check a piped payload's provenance against; ``--allow-egress`` overrides.

    Inside a real repo behaviour is unchanged (the ``.cohort`` egress opt-out is
    consulted as before). Only the no-repo-context case is refused: run from a bare
    directory, there is no per-repo opt-out to honour and no way to know where the
    piped code came from.

    Residual (documented): stdin provenance is fundamentally unknowable — even inside
    a repo, piped bytes need not originate from that repo. This guard closes only the
    "no repo context at all" hole; it cannot vouch for what a caller feeds to stdin.
    """
    if allow_egress:
        return
    if not _repo_has_egress_provenance(Path.cwd()):
        typer.echo(
            "error: refusing external-engine egress from a directory with no "
            "repository context (no .git or .cohort ancestor). The piped code's "
            "provenance can't be checked against any per-repo egress opt-out. Re-run "
            "inside the repository, or pass --allow-egress to override deliberately.",
            err=True,
        )
        raise typer.Exit(code=1)


def _resolve_engine_model(spec, tier: Optional[str], model: Optional[str]) -> str:
    """Resolve the concrete model id to request for ``spec`` from a ``--tier`` name or
    an explicit ``--model`` override (mutually exclusive). Raises ``typer.Exit(2)`` on
    a conflict or an unknown tier, with a message listing the engine's tiers."""
    if model is not None and tier is not None:
        typer.echo(
            "error: --tier and --model are mutually exclusive; pass one or the other",
            err=True,
        )
        raise typer.Exit(code=2)
    if model is not None:
        return model
    resolved_tier = tier or "flagship"
    try:
        return spec.model_tiers[resolved_tier]
    except KeyError:
        typer.echo(
            f"error: unknown tier {resolved_tier!r} for engine {spec.name!r}; "
            f"available tiers: {', '.join(sorted(spec.model_tiers))}",
            err=True,
        )
        raise typer.Exit(code=2)


@engine_app.command("consult")
def engine_consult(
    engine: str = typer.Argument(..., help="Registered engine name (e.g. 'grok')."),
    prompt_file: Optional[Path] = typer.Option(
        None,
        "--prompt-file",
        help="Path to a file holding the prompt. Omit to read the prompt from stdin.",
    ),
    tier: Optional[str] = typer.Option(
        None,
        "--tier",
        help="Model tier to request: flagship (default) | cheap. "
        "Mutually exclusive with --model.",
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        help="Explicit model id, overriding the tier. Mutually exclusive with --tier.",
    ),
    allow_egress: bool = typer.Option(
        False,
        "--allow-egress",
        help="Permit egress when run outside any repo context (no .git/.cohort "
        "ancestor), where a piped payload's provenance can't be checked (F5).",
    ),
    max_tokens: int = typer.Option(
        4096,
        "--max-tokens",
        help="Cap on the engine's response length (bounds cost).",
    ),
) -> None:
    """Consult an external engine (advisory only) and print its reply to stdout.

    The prompt is read from ``--prompt-file`` or piped stdin — NEVER accepted as a
    positional shell argument — so it can't leak via shell history, quoting, or the
    process list. The response is capped by ``--max-tokens`` to bound cost. ``--tier``
    (flagship|cheap) or an explicit ``--model`` selects which model answers.
    """
    try:
        spec = get_engine(engine)
    except UnknownEngineError:
        typer.echo(
            f"error: unknown engine {engine!r}; registered engines: "
            f"{', '.join(sorted(ENGINES))}",
            err=True,
        )
        raise typer.Exit(code=2)

    if "consult" not in spec.roles:
        typer.echo(
            f"error: engine {engine!r} is not registered for the 'consult' role",
            err=True,
        )
        raise typer.Exit(code=2)

    chosen_model = _resolve_engine_model(spec, tier, model)

    if prompt_file is not None:
        try:
            prompt = prompt_file.read_text(encoding="utf-8")
        except OSError as exc:
            typer.echo(f"error: could not read --prompt-file {prompt_file}: {exc}", err=True)
            raise typer.Exit(code=2)
    elif not sys.stdin.isatty():
        prompt = sys.stdin.read()
    else:
        typer.echo(
            "error: no prompt given — pass --prompt-file PATH or pipe the prompt to "
            "stdin (the prompt is never accepted as a shell argument)",
            err=True,
        )
        raise typer.Exit(code=2)

    if not prompt.strip():
        typer.echo("error: prompt is empty", err=True)
        raise typer.Exit(code=2)

    # F5 fail-closed provenance: outside any repo context there is no per-repo opt-out
    # to honour and no way to check where the piped code came from — refuse unless
    # --allow-egress is passed. Inside a real repo this is a no-op.
    _guard_engine_egress_provenance(allow_egress)

    # Gate the outbound prompt the same way `engine propose` does. Without this the
    # per-repo egress opt-out and the secret scan exist only as prose in the compiled
    # `/consult-grok` command — an instruction to a model, not a control. RFC 0004 §5
    # is explicit that enforcement moves from prose to code, and does not scope that
    # to the patch path; consult is the higher-traffic one.
    from .engines import gates as engine_gates

    repo_root = find_repo_root(Path.cwd())
    context_path = repo_root / ".cohort" / "project_context.md"
    project_context_text = (
        context_path.read_text(encoding="utf-8") if context_path.is_file() else ""
    )

    try:
        engine_gates.preflight(
            prompt=prompt,
            project_context_text=project_context_text,
        )
    except engine_gates.EgressBlockedError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    except engine_gates.SecretFoundError as exc:
        typer.echo(f"error: {exc}. Nothing was sent.", err=True)
        raise typer.Exit(code=1)
    except engine_gates.GateError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)

    try:
        text = engine_xai.consult(prompt, model=chosen_model, max_tokens=max_tokens)
    except engine_xai.EngineAuthError:
        typer.echo(
            "error: GROK_API_KEY is unset or was rejected by xAI; export a developer "
            "key from https://console.x.ai and retry",
            err=True,
        )
        raise typer.Exit(code=1)
    except engine_xai.EnginePayloadError as exc:
        typer.echo(f"error: {exc} — trim the prompt/context and retry", err=True)
        raise typer.Exit(code=1)
    except engine_xai.EngineUnavailableError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)

    # The reply is engine-controlled and untrusted: escape it per line before it
    # reaches the terminal, exactly as `engine propose` does. Raw echo would let a
    # reply containing ANSI/OSC sequences rewrite prior output and spoof the very
    # result a human is about to act on.
    for line in text.splitlines():
        typer.echo(_display_safe(line))
    raise typer.Exit(code=0)


def _next_transcript_path(repo_root: Path, override: Optional[Path]) -> Path:
    """Resolve the JSONL transcript path for an ``engine review`` run.

    An explicit ``override`` wins. Otherwise the next zero-padded index in the
    ``.cohort/engine-transcripts`` directory is chosen — deterministic (no wall-clock),
    so a caller/test controls the name and successive runs never collide.
    """
    if override is not None:
        return override
    tdir = repo_root / ".cohort" / "engine-transcripts"
    highest = 0
    if tdir.is_dir():
        for existing in tdir.glob("*.jsonl"):
            if existing.stem.isdigit():
                highest = max(highest, int(existing.stem))
    return tdir / f"{highest + 1:04d}.jsonl"


@engine_app.command("review")
def engine_review(
    engine: str = typer.Argument(..., help="Registered engine name (e.g. 'grok')."),
    task_file: Optional[Path] = typer.Option(
        None,
        "--task-file",
        help="Path to a file holding the review task. Omit to read the task from stdin.",
    ),
    tier: Optional[str] = typer.Option(
        None,
        "--tier",
        help="Model tier: flagship (default) | cheap. Mutually exclusive with --model.",
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        help="Explicit model id, overriding the tier. Mutually exclusive with --tier.",
    ),
    allow_egress: bool = typer.Option(
        False,
        "--allow-egress",
        help="Permit egress when run outside any repo context (no .git/.cohort "
        "ancestor), where a piped payload's provenance can't be checked (F5).",
    ),
    max_tokens: int = typer.Option(
        4096, "--max-tokens", help="Cap on each response's length (bounds cost)."
    ),
    max_iterations: int = typer.Option(
        24, "--max-iterations", help="Hard cap on read-only tool-calling rounds."
    ),
    transcript: Optional[Path] = typer.Option(
        None,
        "--transcript",
        help="Write the JSONL transcript here (default: the next index under "
        ".cohort/engine-transcripts/).",
    ),
) -> None:
    """Have an external engine EXPLORE this repo through read-only tools and report.

    Unlike ``consult`` (one-shot, Claude packages the context), ``review`` gives the
    engine a bounded, read-only tool loop rooted at the repo — its toolbox is the
    egress gate, refusing sensitive paths and secret-bearing content. The task is read
    from ``--task-file`` or piped stdin (never a shell argument), the outbound task is
    run through the same egress opt-out + secret scan as ``consult``, and every tool
    call is recorded to an inspectable JSONL transcript.
    """
    from .engines import gates as engine_gates
    from .engines import xai_agentic

    try:
        spec = get_engine(engine)
    except UnknownEngineError:
        typer.echo(
            f"error: unknown engine {engine!r}; registered engines: "
            f"{', '.join(sorted(ENGINES))}",
            err=True,
        )
        raise typer.Exit(code=2)

    if "consult" not in spec.roles:
        typer.echo(
            f"error: engine {engine!r} is not registered for the 'consult' role",
            err=True,
        )
        raise typer.Exit(code=2)

    chosen_model = _resolve_engine_model(spec, tier, model)

    if task_file is not None:
        try:
            task = task_file.read_text(encoding="utf-8")
        except OSError as exc:
            typer.echo(f"error: could not read --task-file {task_file}: {exc}", err=True)
            raise typer.Exit(code=2)
    elif not sys.stdin.isatty():
        task = sys.stdin.read()
    else:
        typer.echo(
            "error: no task given — pass --task-file PATH or pipe the task to stdin "
            "(the task is never accepted as a shell argument)",
            err=True,
        )
        raise typer.Exit(code=2)

    if not task.strip():
        typer.echo("error: task is empty", err=True)
        raise typer.Exit(code=2)

    # F5 fail-closed provenance (see consult): refuse egress with no repo context.
    _guard_engine_egress_provenance(allow_egress)

    repo_root = find_repo_root(Path.cwd())
    context_path = repo_root / ".cohort" / "project_context.md"
    project_context_text = (
        context_path.read_text(encoding="utf-8") if context_path.is_file() else ""
    )

    try:
        engine_gates.preflight(
            prompt=task,
            project_context_text=project_context_text,
        )
    except engine_gates.EgressBlockedError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    except engine_gates.SecretFoundError as exc:
        typer.echo(f"error: {exc}. Nothing was sent.", err=True)
        raise typer.Exit(code=1)
    except engine_gates.GateError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)

    transcript_path = _next_transcript_path(repo_root, transcript)
    try:
        outcome = xai_agentic.run_agentic(
            task,
            root=repo_root,
            model=chosen_model,
            engine_name=engine,
            max_iterations=max_iterations,
            max_tokens=max_tokens,
            transcript_path=transcript_path,
        )
    except engine_xai.EngineAuthError:
        typer.echo(
            "error: GROK_API_KEY is unset or was rejected by xAI; export a developer "
            "key from https://console.x.ai and retry",
            err=True,
        )
        raise typer.Exit(code=1)
    except engine_xai.EngineUnavailableError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    except engine_xai.EngineError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)

    # The final answer is engine-controlled and untrusted: escape each line before it
    # reaches the terminal, exactly as consult does.
    for line in outcome.text.splitlines():
        typer.echo(_display_safe(line))
    typer.echo(f"transcript: {transcript_path}")
    typer.echo(f"stopped_reason: {outcome.stopped_reason}")
    raise typer.Exit(code=0)


def _display_safe(text: str) -> str:
    """Render engine-controlled ``text`` safe to echo to a terminal.

    An external engine's reply (a patch summary, a proposed path) is untrusted; echoing
    it raw would let embedded ANSI/control sequences rewrite the reviewer's terminal.
    Replace every non-printable character with a visible escape (e.g. ``\\x1b``) while
    leaving ordinary printable text — including spaces — untouched. Callers that render
    multi-line output split into lines first, then sanitize each line.
    """
    return "".join(ch if ch.isprintable() or ch == " " else repr(ch)[1:-1] for ch in text)


@engine_app.command("propose")
def engine_propose(
    engine: str = typer.Argument(..., help="Registered engine name trusted for patches (e.g. 'grok')."),
    task_file: Optional[Path] = typer.Option(
        None,
        "--task-file",
        help="Path to a file holding the task description. Omit to read the task from stdin.",
    ),
    footprint: list[str] = typer.Option(
        [],
        "--footprint",
        help="Repo-relative path/glob the patch may touch (repeatable; required).",
    ),
    max_tokens: int = typer.Option(
        4096,
        "--max-tokens",
        help="Cap on the engine's response length (bounds cost).",
    ),
    agentic: bool = typer.Option(
        False,
        "--agentic",
        help="Let the engine EXPLORE the repo read-only (gated per read, transcript "
        "recorded) to gather its own context before proposing, instead of a bundle.",
    ),
    max_iterations: int = typer.Option(
        24,
        "--max-iterations",
        help="With --agentic: cap on read-only tool rounds before the engine must "
        "propose.",
    ),
) -> None:
    """Ask an external engine to propose a patch, staged in an isolated worktree.

    The engine only returns text; Cohort parses it, gates the proposed paths/content,
    and applies it inside a throwaway git worktree — never in this repo's working tree
    and never committed. The task is read from ``--task-file`` or stdin (never a shell
    argument), and ``--footprint`` is required so there is no unbounded write scope.

    With ``--agentic`` the engine first explores the repo through the read-only tools
    (each read egress-gated, the whole exploration recorded to a transcript) to gather
    its own context, then proposes — the same gates and worktree isolation apply.
    """
    from .engines import patch_proposal
    from .engines import gates as engine_gates
    from .engines.patch import PatchError

    try:
        get_engine(engine)
    except UnknownEngineError:
        typer.echo(
            f"error: unknown engine {engine!r}; registered engines: "
            f"{', '.join(sorted(ENGINES))}",
            err=True,
        )
        raise typer.Exit(code=2)

    cleaned_footprint = [entry.strip() for entry in footprint if entry.strip()]
    if not cleaned_footprint:
        typer.echo(
            "error: at least one --footprint is required (an empty footprint would "
            "grant unbounded write scope)",
            err=True,
        )
        raise typer.Exit(code=2)

    if task_file is not None:
        try:
            task = task_file.read_text(encoding="utf-8")
        except OSError as exc:
            typer.echo(f"error: could not read --task-file {task_file}: {exc}", err=True)
            raise typer.Exit(code=2)
    elif not sys.stdin.isatty():
        task = sys.stdin.read()
    else:
        typer.echo(
            "error: no task given — pass --task-file PATH or pipe the task to stdin "
            "(the task is never accepted as a shell argument)",
            err=True,
        )
        raise typer.Exit(code=2)

    if not task.strip():
        typer.echo("error: task is empty", err=True)
        raise typer.Exit(code=2)

    repo_root = find_repo_root(Path.cwd())
    context_path = repo_root / ".cohort" / "project_context.md"
    project_context_text = (
        context_path.read_text(encoding="utf-8") if context_path.is_file() else ""
    )

    try:
        if agentic:
            transcript = _next_transcript_path(repo_root, None)
            outcome = patch_proposal.propose_patch_agentic(
                engine,
                task,
                repo_root=repo_root,
                allowed_footprint=cleaned_footprint,
                project_context_text=project_context_text,
                max_iterations=max_iterations,
                max_tokens=max_tokens,
                transcript_path=transcript,
            )
            typer.echo(f"exploration transcript: {transcript}", err=True)
        else:
            outcome = patch_proposal.propose_patch(
                engine,
                task,
                repo_root=repo_root,
                allowed_footprint=cleaned_footprint,
                project_context_text=project_context_text,
                max_tokens=max_tokens,
            )
    except engine_gates.EgressBlockedError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    except engine_gates.PathViolationError as exc:
        typer.echo(
            f"error: the proposed patch was rejected — {exc}. Nothing was applied.",
            err=True,
        )
        raise typer.Exit(code=1)
    except engine_gates.SecretFoundError as exc:
        typer.echo(f"error: {exc}. Nothing was applied.", err=True)
        raise typer.Exit(code=1)
    except engine_gates.GateError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    except engine_xai.EngineAuthError:
        typer.echo(
            "error: GROK_API_KEY is unset or was rejected by xAI; export a developer "
            "key from https://console.x.ai and retry",
            err=True,
        )
        raise typer.Exit(code=1)
    except engine_xai.EngineError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    except PatchError as exc:
        typer.echo(f"error: could not use the engine's proposal — {exc}", err=True)
        raise typer.Exit(code=1)
    except patch_proposal.ProposalError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)

    # The summary, proposed paths, and commit subject all originate in the engine's
    # (untrusted) reply; escape control characters before echoing so an embedded ANSI
    # sequence cannot manipulate the reviewer's terminal.
    typer.echo(f"summary: {_display_safe(outcome.summary)}")
    for path in outcome.manifest.changed:
        typer.echo(f"  changed: {_display_safe(path)}")
    for path in outcome.manifest.created:
        typer.echo(f"  created: {_display_safe(path)}")
    if outcome.risk_labels:
        typer.echo(f"risk: {', '.join(outcome.risk_labels)}")
    typer.echo(f"worktree: {outcome.worktree}")
    typer.echo("suggested commit message:")
    for line in outcome.suggested_commit_message.splitlines():
        typer.echo(f"  {_display_safe(line)}")
    typer.echo(
        "review the change in the worktree, run the tests, then merge — "
        "nothing was committed and this repo's working tree is unchanged.",
        err=True,
    )
    raise typer.Exit(code=0)


@engine_app.command("work")
def engine_work(
    engine: str = typer.Argument(..., help="Engine with a sandboxed CLI doer (e.g. 'gpt'). Grok has none — use 'engine propose --agentic'."),
    task_file: Optional[Path] = typer.Option(
        None, "--task-file", help="Path to the task. Omit to read from stdin.",
    ),
    footprint: list[str] = typer.Option(
        [], "--footprint", help="Repo-relative path/glob the change should stay within (repeatable; advisory — reported, not enforced by the sandbox).",
    ),
    model: Optional[str] = typer.Option(None, "--model", help="Model id override for the CLI."),
    allow_egress: bool = typer.Option(
        False, "--allow-egress", help="Permit the doer when run from a directory with no repository context (see engine consult).",
    ),
) -> None:
    """Dispatch a vendor's own agentic CLI as a WRITE doer, confined to a throwaway worktree.

    The CLI edits files directly (and may run its own tests) inside its OS sandbox, which
    is pinned to a fresh detached worktree — never this repo's working tree. The task is
    read from ``--task-file`` or stdin (never a shell argument) and egress-gated first.
    Nothing is committed or merged: review the diff in the worktree and integrate it, or
    discard the worktree. Codex (ChatGPT) is sandboxed; Grok is refused with a pointer to
    ``engine propose --agentic``.
    """
    from .engines import cli_doer

    if task_file is not None:
        try:
            task = task_file.read_text(encoding="utf-8")
        except OSError as exc:
            typer.echo(f"error: could not read --task-file {task_file}: {exc}", err=True)
            raise typer.Exit(code=2)
    elif not sys.stdin.isatty():
        task = sys.stdin.read()
    else:
        typer.echo(
            "error: no task given — pass --task-file PATH or pipe the task to stdin "
            "(the task is never accepted as a shell argument)",
            err=True,
        )
        raise typer.Exit(code=2)
    if not task.strip():
        typer.echo("error: task is empty", err=True)
        raise typer.Exit(code=2)

    _guard_engine_egress_provenance(allow_egress)
    repo_root = find_repo_root(Path.cwd())
    context_path = repo_root / ".cohort" / "project_context.md"
    project_context_text = (
        context_path.read_text(encoding="utf-8") if context_path.is_file() else ""
    )
    cleaned_footprint = [e.strip() for e in footprint if e.strip()] or None

    from .engines import gates as engine_gates
    try:
        result = cli_doer.run_doer(
            engine,
            task,
            repo_root=repo_root,
            model=model,
            footprint=cleaned_footprint,
            project_context_text=project_context_text,
        )
    except cli_doer.DoerUnavailableError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)
    except engine_gates.EgressBlockedError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    except engine_gates.SecretFoundError as exc:
        typer.echo(f"error: {exc}. Nothing was sent.", err=True)
        raise typer.Exit(code=1)
    except cli_doer.DoerError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)

    if not result.changed_files:
        typer.echo(f"the doer made no changes (exit {result.returncode}).", err=True)
        typer.echo(f"worktree: {result.worktree}")
        raise typer.Exit(code=1)

    for path in result.changed_files:
        typer.echo(f"  changed: {_display_safe(path)}")
    if result.footprint_violations:
        typer.echo("footprint violations (the doer edited paths outside the declared scope):", err=True)
        for v in result.footprint_violations:
            typer.echo(f"  ! {_display_safe(v)}", err=True)
    typer.echo(f"worktree: {result.worktree}")
    typer.echo(
        "review the diff in the worktree, run the tests, then integrate — nothing was "
        "committed and this repo's working tree is unchanged.",
        err=True,
    )
    raise typer.Exit(code=0)


@engine_app.command("ratchet")
def engine_ratchet(
    engine: str = typer.Argument(..., help="Proposing doer: 'gpt' (Codex) or 'grok' (agentic patch)."),
    evaluator: str = typer.Option(..., "--evaluator", help="Shell command, run in the worktree, that prints the objective number (e.g. 'pytest -q 2>&1 | tail -1')."),
    task_file: Optional[Path] = typer.Option(None, "--task-file", help="Path to the optimization task. Omit to read from stdin."),
    metric_regex: Optional[str] = typer.Option(None, "--metric-regex", help="Capture the metric with group 1 (default: last number in the output)."),
    goal: str = typer.Option("minimize", "--goal", help="'minimize' (default) or 'maximize' the metric."),
    budget: int = typer.Option(10, "--budget", help="Hard cap on iterations."),
    footprint: list[str] = typer.Option([], "--footprint", help="Scope for the change (repeatable)."),
    model: Optional[str] = typer.Option(None, "--model", help="Model id override for the doer."),
    allow_egress: bool = typer.Option(False, "--allow-egress", help="Permit the doer from a directory with no repo context."),
) -> None:
    """Climb a metric autonomously in a throwaway worktree, keeping only real gains.

    A metric-gated optimization loop (Karpathy's AutoResearch, adapted): each iteration a
    gated doer proposes one change in the worktree, ``--evaluator`` measures it, and the
    change is committed if the metric improved or reverted if not — up to ``--budget``
    iterations. Nothing touches this repo's working tree; review the staircase and the
    worktree, then merge via PR. The evaluator is your own trusted command.
    """
    from .engines import ratchet
    from .engines import gates as engine_gates
    from .engines import cli_doer

    if task_file is not None:
        try:
            task = task_file.read_text(encoding="utf-8")
        except OSError as exc:
            typer.echo(f"error: could not read --task-file {task_file}: {exc}", err=True)
            raise typer.Exit(code=2)
    elif not sys.stdin.isatty():
        task = sys.stdin.read()
    else:
        typer.echo("error: no task given — pass --task-file PATH or pipe it to stdin", err=True)
        raise typer.Exit(code=2)

    _guard_engine_egress_provenance(allow_egress)
    repo_root = find_repo_root(Path.cwd())
    context_path = repo_root / ".cohort" / "project_context.md"
    project_context_text = (
        context_path.read_text(encoding="utf-8") if context_path.is_file() else ""
    )
    cleaned_footprint = [e.strip() for e in footprint if e.strip()] or None

    try:
        result = ratchet.run_ratchet(
            engine, task, repo_root=repo_root, evaluator_cmd=evaluator,
            metric_regex=metric_regex, goal=goal, budget=budget,
            footprint=cleaned_footprint, model=model,
            project_context_text=project_context_text,
        )
    except cli_doer.DoerUnavailableError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)
    except engine_gates.EgressBlockedError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    except engine_gates.SecretFoundError as exc:
        typer.echo(f"error: {exc}. Nothing was sent.", err=True)
        raise typer.Exit(code=1)
    except ratchet.RatchetError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)

    # The staircase.
    typer.echo(f"baseline: {result.baseline}  →  best: {result.best}  ({result.goal})")
    kept = sum(1 for s in result.steps if s.kept)
    typer.echo(f"{kept}/{len(result.steps)} iterations improved the metric")
    for s in result.steps:
        mark = "✓ keep" if s.kept else "· revert"
        typer.echo(f"  i{s.iteration}: {s.metric}  {mark}  ({_display_safe(s.note)})")
    typer.echo(f"ledger: {result.ledger_path}")
    typer.echo(f"worktree: {result.worktree}")
    if result.improved:
        typer.echo(
            "review the accumulated diff in the worktree and merge via PR — nothing "
            "touched this repo's working tree.",
            err=True,
        )
    else:
        typer.echo("no improvement found within the budget; the worktree can be discarded.", err=True)
    raise typer.Exit(code=0 if result.improved else 1)


@my_office_app.command("sync")
def my_office_sync(
    remote: Optional[str] = typer.Option(
        None, "--remote", help="Set the Git remote URL to sync my office to/from (once)."
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Sync my office (~/.cohort/my) with its Git remote, then recompile.

    Backs the personal layer with a Git repo so your agents/skills/settings follow
    you across machines. Fast-forwards from the remote first (refuses a diverged
    history), commits your local changes on top, pushes, and recompiles so anything
    pulled is placed. The personal layer is pushed wholesale — don't store secrets
    in ~/.cohort/my.
    """
    from .myoffice import MySyncError, do_my_sync

    try:
        report = do_my_sync(Path.home(), remote=remote, dry_run=dry_run)
    except MySyncError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)

    def human(r: dict) -> None:
        if r.get("dry_run"):
            typer.echo(f"my-office sync (dry-run) → remote {r.get('remote') or '(none set)'}")
            return
        bits = []
        if r.get("pulled"):
            bits.append("pulled")
        if r.get("pushed"):
            bits.append("pushed")
        typer.echo(f"my-office sync: {', '.join(bits) or 'up to date'} · {r['remote']}"
                   + (" · recompiled" if r.get("recompiled") else ""))
        if r.get("quarantine_state_unreadable"):
            typer.echo(
                "  ⚠ quarantine state is unreadable — every pulled hook/memory is "
                "withheld. Run `cohort my-office review` to repair and see what is held."
            )
        held = r.get("quarantined") or []
        if held:
            typer.echo(
                f"  ⚠ withheld {len(held)} pulled artifact(s) pending review: "
                + ", ".join(held)
                + "\n    They will NOT activate until you run `cohort my-office review` "
                "and `cohort my-office approve`."
            )

    _emit(report, json_output, human)
    raise typer.Exit(code=0)


@my_office_app.command("review")
def my_office_review(json_output: bool = typer.Option(False, "--json")) -> None:
    """List pulled-but-unreviewed artifacts held back by the quarantine (#107).

    ``my-office sync`` withholds hooks and memories a pull introduced — a hook runs
    on IDE events and a memory loads into every session, so on a shared remote they
    are code/prompt-injection sinks. They stay withheld from *every* recompile until
    you review the file in ~/.cohort/my/canonical and run ``my-office approve``.
    """
    from . import quarantine
    from .install_model import CohortPaths

    paths = CohortPaths.for_global(Path.home())
    try:
        pending = quarantine.reconcile(paths.state, paths.my)  # prune stale, then list
    except quarantine.QuarantineStateError as exc:
        typer.echo(
            f"error: {exc}\nThe quarantine state is unreadable, so every pulled "
            "hook/memory stays withheld. Delete the file to reset, then re-sync.",
            err=True,
        )
        raise typer.Exit(code=1)
    report = {"action": "my-office-review", "pending": [a.to_dict() for a in pending]}

    def human(r: dict) -> None:
        items = r["pending"]
        if not items:
            typer.echo("my-office review: nothing pending — no pulled artifacts awaiting approval.")
            return
        typer.echo(
            f"my-office review: {len(items)} pulled artifact(s) awaiting approval "
            "(withheld from every recompile until approved):"
        )
        for a in items:
            typer.echo(f"  • {a['kind']} {a['name']}  ({a['content_hash'][:12]}…)")
        typer.echo(
            "Review each file in ~/.cohort/my/canonical, then "
            "`cohort my-office approve <name>` (or --all)."
        )

    _emit(report, json_output, human)
    raise typer.Exit(code=0)


@my_office_app.command("approve")
def my_office_approve(
    name: Optional[str] = typer.Argument(
        None,
        help="The artifact name to approve. If one name has two pending records with "
        "different content, select one with 'name@hashprefix'.",
    ),
    approve_all: bool = typer.Option(False, "--all", help="Approve every pending artifact."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Clear the quarantine for reviewed artifacts, then recompile so they activate.

    Approve pins nothing forever: it clears the exact bytes you reviewed. If the same
    name is later pulled with different content, it is quarantined afresh — and if that
    leaves two pending records under one name, approving the bare name is refused;
    disambiguate with ``name@hashprefix`` (the prefix shown by ``my-office review``).
    """
    from . import quarantine
    from .install_model import CohortPaths
    from .myoffice import _recompile_if_installed

    if not approve_all and not name:
        typer.echo("error: give an artifact name, or pass --all", err=True)
        raise typer.Exit(code=1)

    paths = CohortPaths.for_global(Path.home())
    try:
        cleared = quarantine.approve(paths.state, [name] if name else [], approve_all=approve_all)
    except quarantine.AmbiguousApprovalError as exc:
        # A bare name maps to >1 pending hash; approving would guess which bytes were
        # reviewed (and clear an unreviewed record sharing the name). Print the message
        # (it lists the pending hashes) and exit non-zero cleanly — never a traceback.
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    except quarantine.QuarantineStateError as exc:
        typer.echo(
            f"error: {exc}\nRefusing to approve against unreadable state. Delete the "
            "file to reset, then re-sync to re-record what is pending.",
            err=True,
        )
        raise typer.Exit(code=1)
    recompiled = _recompile_if_installed(Path.home()) if cleared else False
    report = {"action": "my-office-approve", "approved": cleared, "recompiled": recompiled}

    def human(r: dict) -> None:
        if not r["approved"]:
            typer.echo("my-office approve: nothing matched (already approved, or wrong name?).")
            return
        typer.echo(
            "my-office approve: cleared " + ", ".join(r["approved"])
            + (" · recompiled" if r["recompiled"]
               else " — run `cohort recompile` to place them.")
        )

    _emit(report, json_output, human)
    raise typer.Exit(code=0)


@office_app.command("review")
def office_review(json_output: bool = typer.Option(False, "--json")) -> None:
    """List office-layer artifacts an update pull held back for review (F3).

    ``cohort update`` fast-forwards the shared office source. On a shared office remote
    an update pull can introduce an auto-activating gated artifact (hook/memory/skill/
    agent) a recompile would otherwise place with no review. Those are withheld from
    every recompile until you review the file in the office source's canonical/ and run
    ``cohort office approve``.
    """
    from . import quarantine
    from .install_model import CohortPaths

    paths = CohortPaths.for_global(Path.home())
    try:
        keys = quarantine.office_pending_keys(paths.state)
    except quarantine.QuarantineStateError as exc:
        typer.echo(
            f"error: {exc}\nThe office quarantine state is unreadable, so every "
            "update-pulled office artifact stays withheld. Delete the file to reset, "
            "then re-run `cohort update`.",
            err=True,
        )
        raise typer.Exit(code=1)
    report = {
        "action": "office-review",
        "pending": [
            {"kind": k, "name": n, "content_hash": h} for (k, n, h) in sorted(keys)
        ],
    }

    def human(r: dict) -> None:
        items = r["pending"]
        if not items:
            typer.echo(
                "office review: nothing pending — no update-pulled office artifacts "
                "awaiting approval."
            )
            return
        typer.echo(
            f"office review: {len(items)} office artifact(s) awaiting approval "
            "(withheld from every recompile until approved):"
        )
        for a in items:
            typer.echo(f"  • {a['kind']} {a['name']}  ({a['content_hash'][:12]}…)")
        typer.echo(
            "Review each file in the office source's canonical/, then "
            "`cohort office approve <name>` (or --all)."
        )

    _emit(report, json_output, human)
    raise typer.Exit(code=0)


@office_app.command("approve")
def office_approve(
    name: Optional[str] = typer.Argument(
        None,
        help="Office artifact name to approve. If one name has two pending records "
        "with different content, select one with 'name@hashprefix'.",
    ),
    approve_all: bool = typer.Option(False, "--all", help="Approve every pending office artifact."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Clear the office-layer quarantine for reviewed artifacts, then recompile.

    The office analogue of ``my-office approve``: same content-addressed semantics, so
    a bare name matching two distinct pending hashes is refused (disambiguate with
    ``name@hashprefix``).
    """
    from . import quarantine
    from .install_model import CohortPaths
    from .myoffice import _recompile_if_installed

    if not approve_all and not name:
        typer.echo("error: give an artifact name, or pass --all", err=True)
        raise typer.Exit(code=1)

    paths = CohortPaths.for_global(Path.home())
    try:
        cleared = quarantine.approve_office(
            paths.state, [name] if name else [], approve_all=approve_all
        )
    except quarantine.AmbiguousApprovalError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    except quarantine.QuarantineStateError as exc:
        typer.echo(
            f"error: {exc}\nRefusing to approve against unreadable state. Delete the "
            "file to reset, then re-run `cohort update` to re-record what is pending.",
            err=True,
        )
        raise typer.Exit(code=1)
    recompiled = _recompile_if_installed(Path.home()) if cleared else False
    report = {"action": "office-approve", "approved": cleared, "recompiled": recompiled}

    def human(r: dict) -> None:
        if not r["approved"]:
            typer.echo("office approve: nothing matched (already approved, or wrong name?).")
            return
        typer.echo(
            "office approve: cleared " + ", ".join(r["approved"])
            + (" · recompiled" if r["recompiled"]
               else " — run `cohort recompile` to place them.")
        )

    _emit(report, json_output, human)
    raise typer.Exit(code=0)


def _emit(report: dict, json_output: bool, human) -> None:
    if json_output:
        typer.echo(_json.dumps(report, indent=2))
    else:
        human(report)


@app.command()
def init(
    ctx: typer.Context,
    source: Optional[str] = typer.Option(None, "--source", help="Path to the Cohort source repo."),
    force: bool = typer.Option(False, "--force", help="Restore Cohort blocks the user removed/edited."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan; change nothing."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Scaffold the project home and wire the context into Claude memory."""
    effective_dry_run = dry_run or ctx.obj.get("dry_run", False)
    try:
        source_path = resolve_source(source)
    except SourceUnresolved as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)
    repo = find_repo_root(Path.cwd())
    if repo == Path.home():
        # $HOME's .cohort is the global office home; a project init here would
        # rewire the global CLAUDE.md managed block with the project import.
        typer.echo(
            "error: refusing to init the home directory as a Cohort project "
            "(it is the global office's home) — run init inside a repository",
            err=True,
        )
        raise typer.Exit(code=2)
    report = do_init(
        repo, source_path, effective_dry_run, force, home=Path.home()
    )
    if "error" in report:
        typer.echo(f"error: {report['error']}", err=True)
        raise typer.Exit(code=1)

    def human(r: dict) -> None:
        for op in r["ops"]:
            typer.echo(f"{op['status']:>8}  {op['op']} {op['dest']}")
        s = r["summary"]
        typer.echo(f"init: applied {s['applied']} · skipped {s['skipped']}")
        if not r.get("dry_run"):
            typer.echo(
                "project office ready: .cohort/project_context.md is now loaded into this "
                "repo's Claude memory; the global roster is unchanged. Next: /project-setup "
                "(in your IDE) or `cohort add-specialist`."
            )

    _emit(report, json_output, human)
    if report.get("diverged"):
        typer.echo(
            "warning: a Cohort-managed block (e.g. the Claude @import wiring) was "
            "edited or removed; left as-is. Run `cohort init --force` to restore it.",
            err=True,
        )
    raise typer.Exit(code=0)


@app.command()
def snapshot(
    ctx: typer.Context,
    dry_run: bool = typer.Option(False, "--dry-run"),
    refresh_index: bool = typer.Option(False, "--refresh-index", help="Also regenerate the index."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Write a dated session snapshot (one unique file; conflict-free)."""
    effective_dry_run = dry_run or ctx.obj.get("dry_run", False)
    report = do_snapshot(find_repo_root(Path.cwd()), effective_dry_run, refresh_index)
    if "error" in report:
        typer.echo(f"error: {report['error']}", err=True)
        raise typer.Exit(code=1)
    _emit(report, json_output, lambda r: typer.echo(
        f"snapshot: {'(dry-run) ' if r['dry_run'] else ''}sessions/{r['file']}"))
    raise typer.Exit(code=0)


@context_app.command("refresh")
def context_refresh(
    ctx: typer.Context,
    force: bool = typer.Option(False, "--force", help="Restore a user-removed/edited index block."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Regenerate the managed Recent-sessions index in project_context.md."""
    effective_dry_run = dry_run or ctx.obj.get("dry_run", False)
    report = do_context_refresh(find_repo_root(Path.cwd()), effective_dry_run, force)
    if "error" in report:
        typer.echo(f"error: {report['error']}", err=True)
        raise typer.Exit(code=1)
    _emit(report, json_output, lambda r: typer.echo(
        f"context refresh: {'changed' if r.get('changed') else 'no change'}"))
    if report.get("diverged"):
        typer.echo(
            "warning: the managed Recent-sessions block was edited or removed; left "
            "as-is. Run `cohort context refresh --force` to restore it.",
            err=True,
        )
    raise typer.Exit(code=0)


@app.command()
def deinit(
    ctx: typer.Context,
    purge: bool = typer.Option(False, "--purge", help="Also remove git-tracked content."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Reverse the project install (preserving team content unless --purge)."""
    effective_dry_run = dry_run or ctx.obj.get("dry_run", False)
    report = do_deinit(find_repo_root(Path.cwd()), purge, effective_dry_run, home=Path.home())

    def human(r: dict) -> None:
        if r.get("nothing"):
            typer.echo("nothing to deinit")
            return
        if r["dry_run"]:
            for op in r["ops"]:
                typer.echo(f"{op['action']:>6}  {op['op']} {op['dest']}")
            return
        s = r["summary"]
        typer.echo(
            f"deinit{' --purge' if r['purge'] else ''}: removed {s['removed']} · "
            f"restored {s['restored']} · preserved {s['preserved']}"
        )

    _emit(report, json_output, human)
    raise typer.Exit(code=0)


@app.command()
def projects(json_output: bool = typer.Option(False, "--json")) -> None:
    """List the Cohort projects on this machine (every repo you've `cohort init`ed)."""
    items = list_projects(Path.home())
    if json_output:
        typer.echo(_json.dumps({"projects": items}, indent=2))
        raise typer.Exit(code=0)
    if not items:
        typer.echo("No Cohort projects registered yet — run `cohort init` in a repository.")
        raise typer.Exit(code=0)
    for it in items:
        wiring = "" if it["wiring"] == "present" else f" · wiring {it['wiring']}"
        typer.echo(f"  {it['name']}  ({it['specialists']} specialist"
                   f"{'s' if it['specialists'] != 1 else ''}{wiring})  {it['path']}")
    raise typer.Exit(code=0)


def _echo_layer_note(report: dict) -> None:
    """Say where an authored artifact landed and how to choose the other layer."""
    if report.get("dry_run"):
        return
    if report.get("layer") == "my":
        typer.echo(
            "added to my office (~/.cohort/my) — updates and proposals never touch it; "
            "use `--to office` to author into the shared clone instead.",
            err=True,
        )
        if report.get("first_my_write"):
            typer.echo(
                "note: my office is not version-controlled — `git init ~/.cohort/my` "
                "if you want history/backup.",
                err=True,
            )
    elif report.get("layer") == "office":
        typer.echo(
            "added to the office layer (the shared source clone) — commit it, or open "
            "a PR on your company fork so the whole org benefits.",
            err=True,
        )


@app.command("add-agent")
def add_agent(
    ctx: typer.Context,
    name: Optional[str] = typer.Option(None, "--name", help="Agent slug (kebab-case)."),
    display_name: Optional[str] = typer.Option(None, "--display-name"),
    department: Optional[str] = typer.Option(None, "--department"),
    topology: str = typer.Option("specialist", "--topology", help="specialist | generalist"),
    description: Optional[str] = typer.Option(None, "--description"),
    to: str = typer.Option(
        "my", "--to",
        help="my (default: the personal layer, ~/.cohort/my) | office (the shared clone).",
    ),
    source: Optional[str] = typer.Option(None, "--source", help="Path to the Cohort source repo."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Author a new agent into the global roster (my office by default), then recompile."""
    effective_dry_run = dry_run or ctx.obj.get("dry_run", False)
    try:
        source_path = resolve_source(source)
    except SourceUnresolved as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)
    if name is None:
        inputs = prompt_add_agent_inputs()
    else:
        inputs = {
            "name": name, "display_name": display_name or name,
            "department": department or "General", "topology": topology,
            "description": description or f"{display_name or name} advisor.",
        }
    try:
        report = do_add_agent(
            source_path, Path.home(), inputs["name"], inputs["display_name"],
            inputs["department"], inputs["topology"], inputs["description"], effective_dry_run,
            to=to,
        )
    except AddAgentError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    _emit(report, json_output, lambda r: typer.echo(
        f"add-agent: {'(dry-run) ' if r['dry_run'] else ''}{r['name']} → {r['path']}"))
    _echo_layer_note(report)
    raise typer.Exit(code=0)


@app.command("adopt")
def adopt(
    ctx: typer.Context,
    path: str = typer.Argument(
        ..., help="A native Claude agent/command .md file, OR a whole .claude/agents/ directory."
    ),
    to: str = typer.Option(
        "my", "--to", help="Where to import: 'my' (your office, advisory) or 'project' (this repo)."
    ),
    description: Optional[str] = typer.Option(
        None, "--description", help="Single file: required if the file's frontmatter has none."
    ),
    department: Optional[str] = typer.Option(None, "--department", help="Agents only; default: Imported."),
    display_name: Optional[str] = typer.Option(None, "--display-name", help="Single file, --to my only."),
    advisory_only: bool = typer.Option(
        False, "--advisory-only", help="Skip write-capable (doer) agents instead of importing them."
    ),
    source: Optional[str] = typer.Option(None, "--source", help="Path to the Cohort source repo."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Bring pre-existing native Claude agents/commands into the office.

    A single file OR a whole `.claude/agents/` directory. `--to project` imports
    into this repo's project tier, PRESERVING write-capable "doer" agents (tools
    kept); `--to my` (default) imports into your office, where the advisory-only
    safety invariant applies (a doer is imported read-only and flagged). Originals
    are backed up under ~/.cohort/state/adopt-backups/, never deleted.
    """
    effective_dry_run = dry_run or ctx.obj.get("dry_run", False)
    if to not in ("my", "project"):
        typer.echo("error: --to must be my|project", err=True)
        raise typer.Exit(code=2)

    # Single loose file → my, with no import-only flags: the classic single-file
    # adopt (keeps its precise message + backup path). Everything else — a
    # directory, or --to project, or --advisory-only — goes through the importer.
    single = Path(path).expanduser().is_file()
    if single and to == "my" and not advisory_only:
        try:
            source_path = resolve_source(source)
            report = do_adopt(
                Path.home(), source_path, Path(path),
                description=description, department=department, display_name=display_name,
                dry_run=effective_dry_run,
            )
        except SourceUnresolved as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2)
        except AdoptError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1)
        _emit(report, json_output, lambda r: typer.echo(
            f"adopt: {'(dry-run) ' if r['dry_run'] else ''}{r['kind']} {r['name']} → {r['path']}"))
        if report.get("advisory_enforced"):
            typer.echo(
                "note: adopted agents are advisory read-only in your office (a synced tier); "
                "use `--to project` to keep a write-capable agent as a project doer.", err=True)
        raise typer.Exit(code=0)

    from .adopt import do_import_agents
    try:
        # source is only needed to recompile the global tier (--to my)
        source_path = resolve_source(source) if to == "my" else Path.cwd()
        report = do_import_agents(
            Path.home(), source_path, Path(path),
            to=to, department=department, advisory_only=advisory_only, dry_run=effective_dry_run,
        )
    except SourceUnresolved as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)
    except AdoptError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    if json_output:
        typer.echo(_json.dumps(report))
        raise typer.Exit(code=0)
    pre = "(dry-run) " if report["dry_run"] else ""
    where = "this project" if to == "project" else "your office"
    typer.echo(f"import: {pre}{len(report['imported'])} agent(s) → {where}")
    for a in report["imported"]:
        kind = "doer" if a.get("as_doer") or a.get("was_doer") else "advisory"
        typer.echo(f"  - {a['name']} ({kind})")
    for s in report.get("skipped", []):
        typer.echo(f"  - {s['name']}: {s['reason']}", err=True)
    if report.get("doers_downgraded"):
        typer.echo(
            f"note: {', '.join(report['doers_downgraded'])} had write tools but were imported "
            "read-only (your office is a synced, advisory-only tier). Use `--to project` to keep "
            "them as doers.", err=True)
    _warn_project_doers(report)  # loud disclosure of placed write-capable agents
    raise typer.Exit(code=0)


@app.command()
def personalize(
    ctx: typer.Context,
    kind: str = typer.Argument(..., help="agent | command | memory | hook | skill"),
    name: str = typer.Argument(..., help="The office artifact to copy into my office."),
    source: Optional[str] = typer.Option(None, "--source", help="Path to the Cohort source repo."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Copy an office artifact into my office as a deliberate override, then recompile.

    The copy carries the override marker, so it replaces the office version at
    compile time; `cohort status` flags it if the office version later changes
    (stale) or disappears (dangling).
    """
    effective_dry_run = dry_run or ctx.obj.get("dry_run", False)
    try:
        source_path = resolve_source(source)
    except SourceUnresolved as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)
    try:
        report = do_personalize(source_path, Path.home(), kind, name, effective_dry_run)
    except PersonalizeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    _emit(report, json_output, lambda r: typer.echo(
        f"personalize: {'(dry-run) ' if r['dry_run'] else ''}{r['kind']} {r['name']} → {r['path']}"))
    if not report.get("dry_run"):
        typer.echo(
            "your copy now overrides the office version — edit it, then `cohort recompile`. "
            "status will flag it if the office version changes or disappears.",
            err=True,
        )
    if report.get("first_my_write"):
        typer.echo(
            "note: my office is not version-controlled — `git init ~/.cohort/my` "
            "if you want history/backup.",
            err=True,
        )
    raise typer.Exit(code=0)


@app.command("try")
def try_agent(
    ctx: typer.Context,
    agent: str = typer.Argument(
        ..., help="Agent name (office roster or my office) or a path to a draft .md file."
    ),
    place: bool = typer.Option(
        False, "--place",
        help="Also install it as a trial project specialist in the current repo "
        "(project agents override the global roster) so you can invoke it live.",
    ),
    source: Optional[str] = typer.Option(None, "--source", help="Path to the Cohort source repo."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Preview a compiled agent (the exact system prompt Claude loads) before installing it."""
    try:
        source_path = resolve_source(source)
    except SourceUnresolved as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)
    repo = find_repo_root(Path.cwd()) if place else None
    try:
        report = do_try(source_path, Path.home(), agent, place=place, repo=repo)
    except TryError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)

    if json_output:
        typer.echo(_json.dumps(report, indent=2))
        raise typer.Exit(code=0)
    layer = {"office": "the office roster", "my": "my office", "file": "a draft file"}[report["layer"]]
    typer.echo(f"── {report['name']} (from {layer}) ───────────────")
    typer.echo(report["rendered"].rstrip("\n"))
    typer.echo("─────────────────────────────────────────────")
    typer.echo(f"read-only tools: {report['tools']}", err=True)
    if report.get("placed"):
        typer.echo(
            f"sandboxed as a project specialist → {report['placed']}. Invoke it in this "
            f"repo's Claude session; keep it with `cohort add-agent`, or drop it with "
            f"`cohort remove-specialist {report['name']}`.",
            err=True,
        )
    else:
        typer.echo(
            "preview only — nothing installed. Add it for real with `cohort add-agent` "
            "(global/my office) or `--place` to sandbox it in this repo.",
            err=True,
        )
    raise typer.Exit(code=0)


@app.command("add-memory")
def add_memory(
    ctx: typer.Context,
    name: Optional[str] = typer.Option(None, "--name", help="Memory slug (kebab-case)."),
    description: Optional[str] = typer.Option(None, "--description"),
    display_name: Optional[str] = typer.Option(None, "--display-name", help="Corpus heading."),
    priority: str = typer.Option("normal", "--priority", help="low | normal | high (corpus order)."),
    body_file: Optional[str] = typer.Option(
        None, "--body-file", help="Markdown file supplying the memory body (replaces the template)."
    ),
    to: str = typer.Option(
        "my", "--to",
        help="my (default: the personal layer, ~/.cohort/my) | office (the shared clone) | "
             "project (this repo — travels with it).",
    ),
    source: Optional[str] = typer.Option(None, "--source", help="Path to the Cohort source repo."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Author a new memory (compiled into a session's corpus), then recompile.

    `--to project` writes it into this repo, where it loads in every session here
    and travels with the repo — commit it and everyone who clones gets it.
    """
    effective_dry_run = dry_run or ctx.obj.get("dry_run", False)
    try:
        source_path = resolve_source(source)
    except SourceUnresolved as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)
    if name is None or description is None:
        typer.echo("error: --name and --description are required", err=True)
        raise typer.Exit(code=2)
    body = None
    if body_file is not None:
        body_path = Path(body_file)
        if not body_path.is_file():
            typer.echo(f"error: --body-file not found: {body_file}", err=True)
            raise typer.Exit(code=2)
        body = body_path.read_text(encoding="utf-8")
    try:
        report = do_add_memory(
            source_path, Path.home(), name, description,
            priority=priority, display_name=display_name, body=body,
            dry_run=effective_dry_run, to=to,
            repo=find_repo_root(Path.cwd()) if to == "project" else None,
        )
    except AddMemoryError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    _emit(report, json_output, lambda r: typer.echo(
        f"add-memory: {'(dry-run) ' if r['dry_run'] else ''}{r['name']} → {r['path']}"))
    _echo_layer_note(report)
    _echo_project_memory_note(report)
    raise typer.Exit(code=0)


def _echo_project_memory_note(report: dict) -> None:
    """Disclose what a project memory implies, and its git state.

    A project memory loads into every session in the repo **and travels with the
    repo**. Whether that is acceptable is the user's call, so this reports the
    state and never blocks: tracked means changes are reviewable (history, PRs);
    untracked or no-git means there is no audit trail (#182).
    """
    if report.get("layer") != "project" or report.get("dry_run"):
        return
    git = report.get("git") or {}
    typer.echo(
        "note: a project memory loads in every session in this repo and travels with it — "
        "commit it and everyone who clones gets these standing instructions.",
        err=True,
    )
    if not git.get("git"):
        typer.echo("      git: not a git repo — no audit trail for changes to it.", err=True)
    elif not git.get("tracked"):
        typer.echo(
            "      git: untracked — not shared yet, and changes aren't reviewable; "
            "`git add` it to put changes under review.",
            err=True,
        )
    elif git.get("dirty"):
        typer.echo("      git: tracked, with uncommitted changes.", err=True)
    else:
        typer.echo("      git: tracked — changes are reviewable in history/PRs.", err=True)


def _read_body_file(body_file: Optional[str]) -> Optional[str]:
    if body_file is None:
        return None
    p = Path(body_file)
    if not p.is_file():
        typer.echo(f"error: --body-file not found: {body_file}", err=True)
        raise typer.Exit(code=2)
    return p.read_text(encoding="utf-8")


def _run_authoring(kind: str, call, json_output: bool) -> None:
    try:
        report = call()
    except (AuthoringError, SourceUnresolved) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1 if isinstance(exc, AuthoringError) else 2)
    _emit(report, json_output, lambda r: typer.echo(
        f"add-{kind}: {'(dry-run) ' if r['dry_run'] else ''}{r['name']} → {r['path']}"))
    _echo_layer_note(report)
    raise typer.Exit(code=0)


@app.command("add-skill")
def add_skill(
    name: str = typer.Argument(..., help="Skill slug (kebab-case)."),
    description: str = typer.Option(..., "--description"),
    display_name: Optional[str] = typer.Option(None, "--display-name"),
    triggers: Optional[str] = typer.Option(None, "--triggers", help="Comma-separated trigger phrases."),
    body_file: Optional[str] = typer.Option(None, "--body-file"),
    to: str = typer.Option("my", "--to", help="my (default) | office (the shared clone)."),
    source: Optional[str] = typer.Option(None, "--source"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Author a skill into my office (default) or the shared office, then recompile."""
    trig = [t.strip() for t in triggers.split(",") if t.strip()] if triggers else None
    body = _read_body_file(body_file)
    _run_authoring("skill", lambda: do_add_skill(
        resolve_source(source), Path.home(), name, description,
        display_name=display_name, triggers=trig, body=body, to=to, dry_run=dry_run,
    ), json_output)


@app.command("add-command")
def add_command(
    name: str = typer.Argument(..., help="Command slug (kebab-case)."),
    description: str = typer.Option(..., "--description"),
    invocation: Optional[str] = typer.Option(None, "--invocation", help="Slash name (default: the slug)."),
    body_file: Optional[str] = typer.Option(None, "--body-file"),
    to: str = typer.Option("my", "--to", help="my (default) | office (the shared clone)."),
    source: Optional[str] = typer.Option(None, "--source"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Author a slash command (always dry_run-safe) into my office or the shared office."""
    body = _read_body_file(body_file)
    _run_authoring("command", lambda: do_add_command(
        resolve_source(source), Path.home(), name, description,
        invocation=invocation, body=body, to=to, dry_run=dry_run,
    ), json_output)


@app.command("add-hook")
def add_hook(
    name: str = typer.Argument(..., help="Hook slug (kebab-case)."),
    description: str = typer.Option(..., "--description"),
    event: str = typer.Option(..., "--event", help="session_start | session_end | pre_write | "
                              "post_write | pre_command | post_command | on_stale"),
    action: str = typer.Option(..., "--action", help="The command the hook runs."),
    matcher: Optional[str] = typer.Option(None, "--matcher"),
    body_file: Optional[str] = typer.Option(None, "--body-file"),
    to: str = typer.Option("my", "--to", help="my (default) | office (the shared clone)."),
    source: Optional[str] = typer.Option(None, "--source"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Author a hook into my office or the shared office, then recompile."""
    body = _read_body_file(body_file)
    _run_authoring("hook", lambda: do_add_hook(
        resolve_source(source), Path.home(), name, description, event, action,
        matcher=matcher, body=body, to=to, dry_run=dry_run,
    ), json_output)


@app.command()
def edit(
    kind: str = typer.Argument(..., help="agent | skill | command | hook | memory"),
    name: str = typer.Argument(..., help="The artifact to edit."),
    body_file: Optional[str] = typer.Option(None, "--body-file", help="New body (markdown)."),
    description: Optional[str] = typer.Option(None, "--description", help="New description."),
    layer: str = typer.Option("my", "--layer", help="my (default) | office (edits the shared clone)."),
    source: Optional[str] = typer.Option(None, "--source"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Edit a global artifact's body/description in place, then recompile.

    Round-trips the existing frontmatter (keeps hand-added keys and a personalized
    copy's override markers). Editing `--layer office` rewrites the shared clone.
    """
    body = _read_body_file(body_file)
    try:
        report = do_edit(
            resolve_source(source), Path.home(), kind, name,
            body=body, description=description, layer=layer, dry_run=dry_run,
        )
    except EditError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    except SourceUnresolved as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)
    _emit(report, json_output, lambda r: typer.echo(
        f"edit: {'(dry-run) ' if r['dry_run'] else ''}{r['kind']} {r['name']} → {r['path']}"))
    if not report.get("dry_run") and report.get("layer") == "office":
        typer.echo("edited the office layer (the shared clone) — commit it or open a PR.", err=True)
    raise typer.Exit(code=0)


@app.command()
def dashboard(
    port: int = typer.Option(8787, "--port", help="Localhost port to serve on."),
    no_open: bool = typer.Option(False, "--no-open", help="Do not open the browser."),
) -> None:
    """Serve the local office dashboard (loopback-only; Ctrl-C to stop)."""
    try:
        server = do_dashboard(Path.home(), Path.cwd(), port, open_browser=not no_open)
    except OSError as exc:
        typer.echo(f"error: could not bind 127.0.0.1:{port} ({exc.strerror}); try --port", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"cohort dashboard: {server.url} (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        typer.echo("cohort dashboard: stopped")
    finally:
        server.shutdown()
        server.server_close()
    raise typer.Exit(code=0)


@app.command()
def status(json_output: bool = typer.Option(False, "--json")) -> None:
    """Read-only aggregate of the install (global + project)."""
    report = do_status(Path.home(), Path.cwd())

    def human(r: dict) -> None:
        g = r["global"]
        typer.echo(f"IDEs: {', '.join(g['ides']) or '-'}")
        typer.echo(f"Roster: {g['roster']['count']} agents")
        if g["roster"].get("my"):
            typer.echo(f"  my office: {', '.join(g['roster']['my'])}")
        for layer in ("office", "my", "project"):
            counts = r.get("inventory", {}).get(layer)
            if counts:
                parts = ", ".join(f"{n} {k}{'s' if n != 1 else ''}" for k, n in sorted(counts.items()))
                typer.echo(f"  {layer}: {parts}")
        srcs = r.get("sources", {})
        labels = [("office", "office ←", srcs.get("office")),
                  ("my", "my office ←", srcs.get("my")),
                  ("project", "project ←", srcs.get("project"))]
        shown = [(lbl, val) for _, lbl, val in labels if val]
        if shown:
            typer.echo("Sources:")
            for lbl, val in shown:
                typer.echo(f"  {lbl} {val}")
            if srcs.get("my") is None:
                typer.echo("  my office ← (local only — `cohort my-office sync --remote <url>` to back it up)")
        src = g.get("source", {})
        if src.get("linked") and not src.get("ok"):
            typer.echo(
                f"  ! source link is broken (moved/deleted clone) — run "
                f"`{src.get('restore', 'cohort relink')}`",
                err=True,
            )
        for o in g.get("overrides", []):
            if o["state"] == "dangling":
                typer.echo(
                    f"  ! override {o['name']} is dangling — its office counterpart is gone "
                    f"(renamed/removed upstream?); consider retiring or renaming your copy",
                    err=True,
                )
            else:
                typer.echo(
                    f"  ! override {o['name']} is stale — the office version changed since "
                    f"you personalized; compare and re-personalize if you want the update",
                    err=True,
                )
        for f in g.get("office_local_only", []):
            typer.echo(
                f"  ! local-only in the office clone: {f} — personal? move it to "
                f"~/.cohort/my/ (or PR it to your org's fork)",
                err=True,
            )
        for f in g.get("unmanaged", []):
            # untrusted filename → escape control chars so it can't forge or
            # overwrite terminal output (CR/ESC injection)
            shown = _escape_untrusted(f["path"])
            hint = (
                f" — `cohort adopt {shown}`" if f.get("adoptable")
                else " (nested; not directly adoptable)"
            )
            typer.echo(
                f"  ! unmanaged: {shown} (invisible to the office directory){hint}",
                err=True,
            )
        if "project" in r:
            p = r["project"]
            typer.echo(f"Project: {p['repo']}")
            specs = p.get("specialists", [])
            typer.echo(f"  specialists: {', '.join(specs) or '-'}")
            for s in p.get("shadowed", []):
                typer.echo(f"    ! {s} shadows a global agent (project wins in this repo)")
            for s in p.get("legacy_agents", []):
                typer.echo(
                    f"    ! {s} is in the legacy .cohort/agents/ (no longer compiled) — run "
                    f"`git mv .cohort/agents/{s}.md .cohort/canonical/agents/{s}.md`",
                    err=True,
                )
            st = p["staleness"]
            typer.echo(f"  staleness: {'STALE' if st['stale'] else 'fresh'} (>{st['threshold_hours']:g}h)")
            w = p["wiring"]
            extra = f" — run `{w['restore']}`" if "restore" in w else ""
            typer.echo(f"  wiring: {w['state']}{extra}")

    _emit(report, json_output, human)
    raise typer.Exit(code=0)


def _run_report(period, since, until, dry_run, json_output, ctx) -> None:
    effective_dry_run = dry_run or ctx.obj.get("dry_run", False)
    report = do_report(find_repo_root(Path.cwd()), period, since, until, effective_dry_run)
    if "error" in report:
        typer.echo(f"error: {report['error']}", err=True)
        raise typer.Exit(code=1)
    if dry_run or report.get("dry_run"):
        typer.echo(report.get("body", "")) if not json_output else typer.echo(_json.dumps(report, indent=2))
    else:
        _emit(report, json_output, lambda r: typer.echo(f"{period}-report: .cohort/reports/{r['file']}"))
    raise typer.Exit(code=0)


@app.command("weekly-report")
def weekly_report(
    ctx: typer.Context,
    since: Optional[str] = typer.Option(None, "--since"),
    until: Optional[str] = typer.Option(None, "--until"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Generate a trailing-7-day report."""
    _run_report("weekly", since, until, dry_run, json_output, ctx)


@app.command("monthly-report")
def monthly_report(
    ctx: typer.Context,
    since: Optional[str] = typer.Option(None, "--since"),
    until: Optional[str] = typer.Option(None, "--until"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Generate a trailing-30-day report."""
    _run_report("monthly", since, until, dry_run, json_output, ctx)


def _warn_project_doers(report: dict) -> None:
    """Loudly disclose write-capable ("doer") agents a project install placed —
    a Bash/Edit-capable agent appearing on clone is materially louder than a
    passive file, so it must never be a silent surprise."""
    doers = report.get("doers") or []
    if not doers:
        return
    typer.echo(
        f"warning: this project defines {len(doers)} write-capable agent(s) "
        "(advisory: false) — they can modify files/run commands when invoked:",
        err=True,
    )
    for d in doers:
        tools = ", ".join(d.get("tools") or []) or "(none)"
        bash = "  ⚠ includes Bash (arbitrary execution)" if d.get("bash") else ""
        typer.echo(f"  - {d['name']}: {tools}{bash}", err=True)


@app.command("add-specialist")
def add_specialist(
    ctx: typer.Context,
    name: Optional[str] = typer.Option(None, "--name", help="Specialist slug (kebab-case)."),
    display_name: Optional[str] = typer.Option(None, "--display-name"),
    department: Optional[str] = typer.Option(None, "--department"),
    description: Optional[str] = typer.Option(None, "--description"),
    body_file: Optional[str] = typer.Option(
        None, "--body-file", help="Markdown file supplying the agent body (replaces the template)."
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Add a project-isolated specialist to the current repo (requires `cohort init`)."""
    effective_dry_run = dry_run or ctx.obj.get("dry_run", False)
    if name is None:
        inputs = prompt_add_specialist_inputs()
    else:
        inputs = {
            "name": name, "display_name": display_name or name,
            "department": department or "Project",
            "description": description or f"{display_name or name} (project specialist).",
        }
    body = None
    if body_file is not None:
        body_path = Path(body_file)
        if not body_path.is_file():
            typer.echo(f"error: --body-file not found: {body_file}", err=True)
            raise typer.Exit(code=2)
        body = body_path.read_text(encoding="utf-8")
    try:
        report = do_add_specialist(
            find_repo_root(Path.cwd()), Path.home(), inputs["name"], inputs["display_name"],
            inputs["department"], inputs["description"], effective_dry_run, body=body,
        )
    except AddSpecialistError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    _emit(report, json_output, lambda r: typer.echo(
        f"add-specialist: {'(dry-run) ' if r['dry_run'] else ''}{r['name']} → {r['path']}"))
    if report.get("shadow"):
        typer.echo(
            f"warning: {report['name']} shares a name with a global roster agent; the project "
            f"specialist takes precedence over the global one in this repo.",
            err=True,
        )
    if report.get("scope_filtered"):
        typer.echo(
            f"note: not compiled at the project tier (wrong scope): "
            f"{', '.join(report['scope_filtered'])} — set `scope: project` or move the "
            f"artifact to the global office.",
            err=True,
        )
    _warn_project_doers(report)
    raise typer.Exit(code=0)


@app.command("remove-specialist")
def remove_specialist(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="The project specialist to remove."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Remove (prune) a project specialist: source, compiled output, and manifest records."""
    effective_dry_run = dry_run or ctx.obj.get("dry_run", False)
    try:
        report = do_remove_specialist(find_repo_root(Path.cwd()), Path.home(), name, effective_dry_run)
    except RemoveSpecialistError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    _emit(report, json_output, lambda r: typer.echo(
        f"remove-specialist: {'(dry-run) ' if r['dry_run'] else ''}{r['name']} ({r['path']})"))
    if report.get("unshadows"):
        typer.echo(
            f"note: the global roster agent {report['name']!r} is no longer shadowed in this repo.",
            err=True,
        )
    raise typer.Exit(code=0)


@app.command()
def promote(
    ctx: typer.Context,
    specialist: str = typer.Argument(..., help="The project specialist to lift up a level."),
    to: str = typer.Option(
        "my", "--to",
        help="my (default: direct copy into your personal layer) | office (a human-gated "
        "proposal for the shared roster — consumed by submit-proposals).",
    ),
    source: Optional[str] = typer.Option(None, "--source", help="Path to the Cohort source repo."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Lift a project specialist to my office (direct) or propose it for the shared office."""
    effective_dry_run = dry_run or ctx.obj.get("dry_run", False)
    source_path = None
    if to == "my":
        try:
            source_path = resolve_source(source)
        except SourceUnresolved as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2)
    try:
        report = do_promote(
            find_repo_root(Path.cwd()), Path.home(), specialist, effective_dry_run,
            to=to, source=source_path,
        )
    except PromoteError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)

    def human(r: dict) -> None:
        if r["to"] == "office":
            typer.echo(
                f"promote: {'(dry-run) ' if r['dry_run'] else ''}proposal staged at "
                f"{r['proposal']} (human-reviewed; no direct global write)"
            )
        else:
            typer.echo(
                f"promote: {'(dry-run) ' if r['dry_run'] else ''}{r['name']} → {r['path']} "
                f"(my office — the project copy remains and still wins inside its repo)"
            )

    _emit(report, json_output, human)
    raise typer.Exit(code=0)


@app.command()
def feedback(
    ctx: typer.Context,
    rating: str = typer.Option(..., "--rating", help="up | down"),
    agent: Optional[str] = typer.Option(None, "--agent", help="Agent the feedback is about."),
    command: Optional[str] = typer.Option(None, "--command", help="Command the feedback is about."),
    note: str = typer.Option("", "--note", help="Free-text note."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Record one feedback entry (conflict-free file) for the Steward to learn from."""
    effective_dry_run = dry_run or ctx.obj.get("dry_run", False)
    try:
        report = do_feedback(find_repo_root(Path.cwd()), rating, agent, command, note, effective_dry_run)
    except FeedbackError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    _emit(report, json_output, lambda r: typer.echo(
        f"feedback: {'(dry-run) ' if r['dry_run'] else ''}feedback/{r['file']}"))
    raise typer.Exit(code=0)


@app.command("propose-improvement")
def propose_improvement(
    ctx: typer.Context,
    body_file: Optional[str] = typer.Option(
        None, "--body-file",
        help="Markdown draft (e.g. Steward-written) that becomes the proposal's rationale, "
        "replacing the deterministic summary.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Synthesize a structured improvement proposal from feedback + sessions (deterministic core)."""
    effective_dry_run = dry_run or ctx.obj.get("dry_run", False)
    enrich = None
    if body_file is not None:
        body_path = Path(body_file)
        if not body_path.is_file():
            typer.echo(f"error: --body-file not found: {body_file}", err=True)
            raise typer.Exit(code=2)
        draft = body_path.read_text(encoding="utf-8")

        def enrich(ev: dict, _draft: str = draft) -> str:
            # The Steward's in-IDE draft, delivered through the existing enrichment
            # seam; the deterministic evidence sections still frame it.
            return _draft

    try:
        if body_file is not None:
            validate_enrichment_body(draft)
        report = do_propose_improvement(
            find_repo_root(Path.cwd()), effective_dry_run, enrich=enrich
        )
    except FeedbackError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    if report.get("dry_run"):
        typer.echo(report["body"]) if not json_output else typer.echo(_json.dumps(report, indent=2))
    else:
        flag = "yes" if report.get("upstream_candidate") else "no"
        _emit(report, json_output, lambda r: typer.echo(
            f"propose-improvement: proposals/{r['file']} (upstream candidate: {flag})"
        ))
    raise typer.Exit(code=0)


@app.command()
def distill(
    ctx: typer.Context,
    days: int = typer.Option(
        DEFAULT_DAYS, "--days",
        help="Look back this many days over sessions/ + feedback/ (not --since; "
        "--since means an ISO date in weekly-report/monthly-report).",
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Compound recent sessions + feedback into a provenance-cited addition to
    project_context.md — shown as a diff, appended only on explicit confirm.

    sessions/ and feedback/ are git-tracked and contributor-writable, therefore
    untrusted input; the confirm diff is the security gate — review provenance before
    approving. Deterministic (no LLM, no network); never invoked from a hook.
    """
    effective_dry_run = dry_run or ctx.obj.get("dry_run", False)

    def _confirm(diff: str) -> bool:
        # The diff is the review gate; print it (already control-char-escaped) to
        # stderr so stdout stays clean for --json, then prompt. The diff header
        # names the target (project_context.md).
        typer.echo(diff, err=True)
        return typer.confirm("Append this distilled section?")

    report = do_distill(
        find_repo_root(Path.cwd()), days, effective_dry_run,
        confirm=None if effective_dry_run else _confirm,
    )
    if "error" in report:
        typer.echo(f"error: {report['error']}", err=True)
        raise typer.Exit(code=1)
    if report.get("empty"):
        _emit(report, json_output, lambda r: typer.echo(
            f"distill: no sessions or feedback in the last {r['days']} days — "
            "nothing to distill."))
        raise typer.Exit(code=0)
    if report.get("dry_run"):
        typer.echo(_json.dumps(report, indent=2)) if json_output else typer.echo(report["diff"])
        raise typer.Exit(code=0)

    def human(r: dict) -> None:
        target = r.get("target", "project_context.md")
        if r.get("applied"):
            typer.echo(f"distill: appended '{r['header']}' to {target}")
        else:
            typer.echo(f"distill: declined — {target} unchanged.")

    _emit(report, json_output, human)
    raise typer.Exit(code=0)


@app.command("submit-proposals")
def submit_proposals(
    ctx: typer.Context,
    source: Optional[str] = typer.Option(None, "--source", help="Cohort source repo (PR target)."),
    repo: Optional[str] = typer.Option(
        None, "--repo",
        help="GitHub repo (OWNER/NAME) to open the PR against, e.g. your fork. "
        "If you cloned Cohort and lack push access to the upstream, fork it first "
        "and pass your fork here. (Not used with --upstream.)",
    ),
    upstream: bool = typer.Option(
        False, "--upstream",
        help="Submit only upstream-candidate proposals to the upstream Cohort repo "
        "(resolved from [update] upstream_remote), each sanitized of project markers.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Open a draft PR per proposal against the source repo (human reviews + merges)."""
    effective_dry_run = dry_run or ctx.obj.get("dry_run", False)
    if upstream and repo:
        typer.echo(
            "error: --upstream resolves the target from upstream_remote; --repo is not used with it. "
            "To submit from a fork, set [update] upstream_remote to your fork remote.",
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        source_path = resolve_source(source)
    except SourceUnresolved as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)
    report = do_submit_proposals(
        find_repo_root(Path.cwd()), source_path, effective_dry_run,
        target_repo=repo, home=Path.home(), upstream=upstream,
    )

    def human(r: dict) -> None:
        if r.get("degraded"):
            typer.echo(
                r.get("detail")
                or "note: gh/remote unavailable — proposals left as files in .cohort/proposals/ "
                "for manual PR creation.",
                err=True,
            )
        skipped_why = "not an upstream candidate / already submitted" if upstream else "already submitted"
        typer.echo(
            f"submit-proposals:{' (upstream)' if upstream else ''} "
            f"{'(dry-run) ' if r.get('dry_run') else ''}"
            f"submitted {len(r['submitted'])} · skipped {len(r['skipped'])} ({skipped_why})"
        )
        if r.get("redacted"):
            typer.echo(
                f"sanitized {len(r['redacted'])} project marker(s) before upstreaming; "
                "review the rendered PR body before publishing.",
                err=True,
            )

    _emit(report, json_output, human)
    raise typer.Exit(code=0)


@app.command("staleness-check", hidden=True)
def staleness_check_cmd() -> None:
    """Internal: the session_start staleness hook target. Always exits 0."""
    message = staleness_check(Path.cwd())
    if message:
        typer.echo(message, err=True)
    raise typer.Exit(code=0)


@app.command("session-capture", hidden=True)
def session_capture_cmd() -> None:
    """Internal: the session_end capture hook target (opt-in per repo). Always exits 0."""
    try:
        written = session_capture(Path.cwd())
        if written:
            typer.echo(f"cohort: session captured → {written}", err=True)
    except Exception:  # noqa: BLE001 - a capture must never break session end
        pass
    raise typer.Exit(code=0)


COMPACT_RECALL_TEXT = """\
Context was just compacted. Before resuming the task, commit the critical parts of
the pre-compaction session to durable memory now: (1) decisions made and why;
(2) in-flight work state — branches, PRs, pending approvals, unfinished steps;
(3) unresolved questions and the user's directives stated this session. Write to
your persistent memory directory, and offer `cohort snapshot` for repo-shared
context. Skip anything already recorded; keep it to the facts a fresh session
would need."""


@app.command("compact-recall", hidden=True)
def compact_recall_cmd() -> None:
    """Internal: the post_compact hook target. Prints the memory-commit instruction
    to stdout, which the IDE injects into the model's context right after
    compaction. Print-only — writes nothing. Always exits 0."""
    typer.echo(COMPACT_RECALL_TEXT)
    raise typer.Exit(code=0)


@app.command("update-check", hidden=True)
def update_check_cmd() -> None:
    """Internal: the session_start update-advisory hook target. Always exits 0."""
    try:
        message = do_update_check(Path.home())
        if message:
            typer.echo(message, err=True)
    except Exception:  # noqa: BLE001 - an advisory must never break a session
        pass
    raise typer.Exit(code=0)


def run() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
