"""The ``cohort`` Typer application.

Phase 0 exposes a single command, ``validate``, plus the global ``--dry-run``
flag and structured logging that every later command will reuse.
"""

from __future__ import annotations

import json as _json
import sys
import time
from pathlib import Path

import typer

from .logconf import emit_log
from .schema import TreeResult, validate_tree

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


def _print_human(tree: TreeResult) -> None:
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
    """Schema-validate every canonical artifact under PATH.

    Exit codes: 0 all valid · 1 one or more invalid · 2 usage error.
    The global ``--dry-run`` flag is accepted but a no-op (validate is read-only).
    """
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
    emit_log(
        component="validate",
        action="validate_tree",
        scope="-",
        ide="-",
        artifact=str(path),
        status="valid" if tree.valid else "invalid",
        duration_ms=elapsed_ms,
    )

    if json_output:
        typer.echo(_json.dumps(tree.to_dict(), indent=2))
    else:
        _print_human(tree)

    raise typer.Exit(code=0 if tree.valid else 1)


def run() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
