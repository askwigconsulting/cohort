"""The ``cohort`` Typer application: ``validate``, ``install``, ``uninstall``.

Commands map domain exceptions to the exit triad (0 success · 1 refused/failed ·
2 usage) and reuse the Phase 0 structured-log format.
"""

from __future__ import annotations

import json as _json
import sys
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
from .office_setup import (
    SetupError,
    do_setup,
    effective_roster,
    persist_roster,
    prompt_setup_inputs,
)
from .update import UpdateResult, do_relink, do_update, do_update_check
from .logconf import emit_log
from .project import (
    do_context_refresh,
    do_deinit,
    do_init,
    do_snapshot,
    find_repo_root,
    session_capture,
    staleness_check,
)
from .reports import do_report
from .adopt import AdoptError, do_adopt
from .roster import (
    AddAgentError,
    AddMemoryError,
    do_add_agent,
    do_add_memory,
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

app = typer.Typer(
    add_completion=False,
    help="Cohort — portable, multi-IDE agentic office harness.",
    no_args_is_help=True,
)


def _force_utf8_io() -> None:
    """Force UTF-8 on stdout/stderr so Cohort's Unicode output (→, …, —) never
    crashes on a legacy Windows console (whose default cp1252 can't encode it).

    No-op where the stream can't be reconfigured (e.g. a pytest-captured stream).
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (ValueError, OSError):
                pass


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


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
    _force_utf8_io()
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
        results = [compile_ide(source_path, i, scope="global", only_agents=only) for i in selection]
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
        results = [compile_ide(source_path, i, scope="global", only_agents=only) for i in selection]
    except CompileError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    # dry-run recompile renders without writing staging (so install sees no new
    # staging) and prints the plan it *would* apply.
    if not effective_dry_run:
        for result in results:
            write_staging(paths, result)

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


_UPDATE_FAILED = ("unavailable", "diverged", "dirty", "pull_failed", "pip_failed")


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
    report = do_init(find_repo_root(Path.cwd()), source_path, effective_dry_run, force)

    def human(r: dict) -> None:
        for op in r["ops"]:
            typer.echo(f"{op['status']:>8}  {op['op']} {op['dest']}")
        s = r["summary"]
        typer.echo(f"init: applied {s['applied']} · skipped {s['skipped']}")

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
    report = do_deinit(find_repo_root(Path.cwd()), purge, effective_dry_run)

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


@app.command("add-agent")
def add_agent(
    ctx: typer.Context,
    name: Optional[str] = typer.Option(None, "--name", help="Agent slug (kebab-case)."),
    display_name: Optional[str] = typer.Option(None, "--display-name"),
    department: Optional[str] = typer.Option(None, "--department"),
    topology: str = typer.Option("specialist", "--topology", help="specialist | generalist"),
    description: Optional[str] = typer.Option(None, "--description"),
    source: Optional[str] = typer.Option(None, "--source", help="Path to the Cohort source repo."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Author a new agent into the global roster, then recompile."""
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
        )
    except AddAgentError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    _emit(report, json_output, lambda r: typer.echo(
        f"add-agent: {'(dry-run) ' if r['dry_run'] else ''}{r['name']} → {r['path']}"))
    raise typer.Exit(code=0)


@app.command("adopt")
def adopt(
    ctx: typer.Context,
    path: str = typer.Argument(
        ..., help="A loose file under ~/.claude/agents/ or ~/.claude/commands/ to adopt."
    ),
    description: Optional[str] = typer.Option(
        None, "--description", help="Required if the file's frontmatter has none."
    ),
    department: Optional[str] = typer.Option(None, "--department", help="Agents only; default: Adopted."),
    display_name: Optional[str] = typer.Option(None, "--display-name", help="Agents only."),
    source: Optional[str] = typer.Option(None, "--source", help="Path to the Cohort source repo."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Lift a loose, unmanaged Claude agent/command into canonical and recompile.

    The original is backed up under ~/.cohort/state/adopt-backups/, never deleted.
    Adopted agents become advisory read-only like the rest of the roster.
    """
    effective_dry_run = dry_run or ctx.obj.get("dry_run", False)
    try:
        source_path = resolve_source(source)
    except SourceUnresolved as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)
    try:
        report = do_adopt(
            Path.home(), source_path, Path(path),
            description=description, department=department, display_name=display_name,
            dry_run=effective_dry_run,
        )
    except AdoptError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    _emit(report, json_output, lambda r: typer.echo(
        f"adopt: {'(dry-run) ' if r['dry_run'] else ''}{r['kind']} {r['name']} → {r['path']}"))
    if report.get("advisory_enforced"):
        typer.echo(
            "note: adopted agents are advisory read-only (Cohort's v1 safety invariant), "
            "even if the loose original inherited all tools.",
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
    source: Optional[str] = typer.Option(None, "--source", help="Path to the Cohort source repo."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Author a new global office memory (compiled into every session's corpus), then recompile."""
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
            dry_run=effective_dry_run,
        )
    except AddMemoryError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    _emit(report, json_output, lambda r: typer.echo(
        f"add-memory: {'(dry-run) ' if r['dry_run'] else ''}{r['name']} → {r['path']}"))
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
        src = g.get("source", {})
        if src.get("linked") and not src.get("ok"):
            typer.echo(
                f"  ! source link is broken (moved/deleted clone) — run "
                f"`{src.get('restore', 'cohort relink')}`",
                err=True,
            )
        for f in g.get("unmanaged", []):
            typer.echo(
                f"  ! unmanaged: {f} (invisible to the office directory) — "
                f"`cohort adopt {f}`",
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
    specialist: str = typer.Argument(..., help="The project specialist to propose for global."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Stage a proposal to promote a project specialist to the global roster (human-gated)."""
    effective_dry_run = dry_run or ctx.obj.get("dry_run", False)
    try:
        report = do_promote(find_repo_root(Path.cwd()), specialist, effective_dry_run)
    except PromoteError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    _emit(report, json_output, lambda r: typer.echo(
        f"promote: {'(dry-run) ' if r['dry_run'] else ''}proposal staged at {r['proposal']} "
        f"(human-reviewed; no direct global write)"))
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
