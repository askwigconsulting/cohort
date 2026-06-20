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

from .executor import ClobberRefused
from .install import (
    CancelledSelection,
    InstallReport,
    UninstallReport,
    UsageError,
    do_install,
    do_uninstall,
    resolve_selection,
)
from .logconf import emit_log
from .schema import TreeResult, validate_tree
from .source import SourceUnresolved, resolve_source

app = typer.Typer(
    add_completion=False,
    help="Cohort — portable, multi-IDE agentic office harness.",
    no_args_is_help=True,
)


@app.callback()
def main(
    ctx: typer.Context,
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
    ide: Optional[str] = typer.Option(None, "--ide", help="claude,codex,cursor or all."),
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

    mode = "copy" if copy else "link"
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
    raise typer.Exit(code=0)


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


def run() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
