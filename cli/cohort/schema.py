"""Schema loading and the canonical-artifact validator.

The JSON Schema files under ``canonical/schema/`` are the declarative source for
structural rules (allowed fields, types, enums, required, defaults). They are
genuine draft 2020-12 schemas (a unit test asserts ``check_schema`` accepts
them), but validation is driven by a small interpreter here rather than the
generic ``jsonschema`` validator, because the contract requires *stable error
codes* (E0xx) and a specific staged, collect-all evaluation model:

  Stage 1  frontmatter parse (E001) — a hard stop, handled by the loader.
  Stage 2  shared-field checks — collect *all* shared errors.
  Stage 3  per-kind checks — run only if ``kind`` is present and valid;
           otherwise skipped (so a missing/bad kind produces no per-kind noise).

Cross-field rules JSON Schema cannot express (name↔stem equality, ``targets``
``all``-exclusivity, the context scope/name rule, the safety invariants) live in
code and emit their dedicated codes.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from .errors import (
    ArtifactError,
    E010_MISSING_FIELD,
    E011_FIELD_LENGTH,
    E020_BAD_ENUM,
    E030_NAME_MISMATCH,
    E040_TARGETS_INVALID,
    E050_TYPE,
    E060_SAFETY_INVARIANT,
    E070_SCOPE_CONSTRAINT,
    E080_DUPLICATE,
    E090_UNKNOWN_FIELD,
)
from .loader import LoadResult, load_artifact

# --- Constants --------------------------------------------------------------

KINDS = ("agent", "skill", "command", "hook", "memory", "context")

# Explicit kind -> directory map (R1): never naive "<kind>s" — memory pluralizes
# to "memories", not "memorys".
KIND_DIRS: dict[str, str] = {
    "agent": "agents",
    "skill": "skills",
    "command": "commands",
    "hook": "hooks",
    "memory": "memories",
    "context": "contexts",
}

TARGET_VALUES = ("claude", "codex", "cursor", "all")
NAME_PATTERN = "^[a-z][a-z0-9-]*$"
DESCRIPTION_MAX = 1024
DEFAULT_VERSION = "0.1.0"
CONTEXT_REQUIRED_NAME = "project-context"

_REPO_ROOT = Path(__file__).resolve().parents[2]


def schema_dir() -> Path:
    """Resolve the schema directory (env override → in-repo ``canonical/schema``)."""
    override = os.environ.get("COHORT_SCHEMA_DIR")
    return Path(override) if override else _REPO_ROOT / "canonical" / "schema"


@lru_cache(maxsize=None)
def _load_schema(stem: str) -> dict[str, Any]:
    return json.loads((schema_dir() / f"{stem}.json").read_text(encoding="utf-8"))


def shared_schema() -> dict[str, Any]:
    return _load_schema("shared")


def kind_schema(kind: str) -> dict[str, Any]:
    return _load_schema(kind)


# --- Helpers ----------------------------------------------------------------


def json_type(value: Any) -> str:
    """Return the JSON Schema type name for a Python value (bool before int)."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) or isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return type(value).__name__


def _check_type(
    fm: dict[str, Any], field: str, expected: str, errors: list[ArtifactError]
) -> bool:
    """If ``field`` is present, confirm its JSON type; emit E050 otherwise.

    Returns True when the field is present AND correctly typed (safe to inspect
    its value further), else False.
    """
    if field not in fm:
        return False
    actual = json_type(fm[field])
    if actual != expected:
        errors.append(
            ArtifactError(
                E050_TYPE,
                field,
                f"{field} must be of type {expected}, got {actual}",
            )
        )
        return False
    return True


def _check_array_of_strings(
    fm: dict[str, Any], field: str, errors: list[ArtifactError]
) -> bool:
    """Confirm ``field`` (if present) is an array whose elements are strings."""
    if not _check_type(fm, field, "array", errors):
        return False
    for i, item in enumerate(fm[field]):
        if not isinstance(item, str):
            errors.append(
                ArtifactError(
                    E050_TYPE,
                    field,
                    f"{field}[{i}] must be a string, got {json_type(item)}",
                )
            )
            return False
    return True


def _check_enum(
    fm: dict[str, Any], field: str, allowed: tuple[str, ...], errors: list[ArtifactError]
) -> None:
    """Confirm ``field`` (if present and a string) is within ``allowed``."""
    if not _check_type(fm, field, "string", errors):
        return
    if fm[field] not in allowed:
        errors.append(
            ArtifactError(
                E020_BAD_ENUM,
                field,
                f"{field} must be one of {list(allowed)}, got {fm[field]!r}",
            )
        )


def _slug_ok(value: str) -> bool:
    import re

    return re.fullmatch(NAME_PATTERN, value) is not None


# --- Stage 2: shared-field validation --------------------------------------


def _validate_shared(
    fm: dict[str, Any], name_stem: str, errors: list[ArtifactError]
) -> None:
    schema = shared_schema()
    required = schema["required"]

    # Required presence (E010). Empty/whitespace description counts as missing.
    for field in required:
        if field not in fm:
            errors.append(
                ArtifactError(E010_MISSING_FIELD, field, f"required field {field!r} is missing")
            )

    # name: type (E050) → slug + stem equality (E030, two variants).
    if _check_type(fm, "name", "string", errors):
        name = fm["name"]
        if not _slug_ok(name):
            errors.append(
                ArtifactError(
                    E030_NAME_MISMATCH,
                    "name",
                    f"name {name!r} does not match slug pattern {NAME_PATTERN}",
                    variant="slug",
                )
            )
        elif name != name_stem:
            errors.append(
                ArtifactError(
                    E030_NAME_MISMATCH,
                    "name",
                    f"name {name!r} does not equal filename stem {name_stem!r}",
                    variant="stem",
                )
            )

    # kind / scope: enum.
    _check_enum(fm, "kind", KINDS, errors)
    _check_enum(fm, "scope", ("global", "project"), errors)

    # description: type (E050) → empty → E010, over-length → E011.
    if _check_type(fm, "description", "string", errors):
        desc = fm["description"]
        if desc.strip() == "":
            errors.append(
                ArtifactError(
                    E010_MISSING_FIELD, "description", "description must be non-empty"
                )
            )
        elif len(desc) > DESCRIPTION_MAX:
            errors.append(
                ArtifactError(
                    E011_FIELD_LENGTH,
                    "description",
                    f"description must be ≤ {DESCRIPTION_MAX} chars, got {len(desc)}",
                )
            )

    # targets: element type (E050) before domain (E040).
    if "targets" in fm and _check_array_of_strings(fm, "targets", errors):
        _validate_targets(fm["targets"], errors)

    # Optional string fields: type only (version is free-form in v0.1).
    for field in ("version", "owner", "display_name", "office_sha256"):
        _check_type(fm, field, "string", errors)
    _check_type(fm, "overrides", "boolean", errors)


def _validate_targets(targets: list[Any], errors: list[ArtifactError]) -> None:
    if len(targets) == 0:
        errors.append(ArtifactError(E040_TARGETS_INVALID, "targets", "targets must be non-empty"))
        return
    unknown = [t for t in targets if t not in TARGET_VALUES]
    if unknown:
        errors.append(
            ArtifactError(
                E040_TARGETS_INVALID, "targets", f"targets has unknown value(s): {unknown}"
            )
        )
        return
    if len(set(targets)) != len(targets):
        errors.append(
            ArtifactError(E040_TARGETS_INVALID, "targets", "targets has duplicate values")
        )
        return
    if "all" in targets and len(targets) != 1:
        errors.append(
            ArtifactError(
                E040_TARGETS_INVALID, "targets", "'all' must be the only target when present"
            )
        )


# --- Stage 3: per-kind validation ------------------------------------------


def _validate_unknown_fields(
    fm: dict[str, Any], kind: str, errors: list[ArtifactError]
) -> None:
    allowed = set(shared_schema()["properties"]) | set(kind_schema(kind)["properties"])
    for key in fm:
        if key not in allowed:
            errors.append(
                ArtifactError(E090_UNKNOWN_FIELD, key, f"unknown top-level field {key!r}")
            )


def _validate_required(
    fm: dict[str, Any], schema: dict[str, Any], errors: list[ArtifactError]
) -> None:
    for field in schema.get("required", []):
        if field not in fm:
            errors.append(
                ArtifactError(E010_MISSING_FIELD, field, f"required field {field!r} is missing")
            )


def _validate_agent(fm: dict[str, Any], errors: list[ArtifactError]) -> None:
    _validate_required(fm, kind_schema("agent"), errors)
    _check_type(fm, "department", "string", errors)
    _check_enum(fm, "topology", ("specialist", "generalist"), errors)
    _check_array_of_strings(fm, "tools", errors)
    # advisory: type before invariant (R6). Wrong type → E050 suppresses E060.
    if _check_type(fm, "advisory", "boolean", errors) and fm["advisory"] is False:
        errors.append(
            ArtifactError(E060_SAFETY_INVARIANT, "advisory", "agents must be advisory: true")
        )


def _validate_command(fm: dict[str, Any], errors: list[ArtifactError]) -> None:
    _validate_required(fm, kind_schema("command"), errors)
    _check_type(fm, "invocation", "string", errors)
    _validate_command_args(fm, errors)
    if _check_type(fm, "dry_run", "boolean", errors) and fm["dry_run"] is False:
        errors.append(
            ArtifactError(E060_SAFETY_INVARIANT, "dry_run", "commands must not set dry_run: false")
        )


def _validate_command_args(fm: dict[str, Any], errors: list[ArtifactError]) -> None:
    if "args" not in fm:
        return
    if not _check_type(fm, "args", "array", errors):
        return
    for i, arg in enumerate(fm["args"]):
        if not isinstance(arg, dict):
            errors.append(
                ArtifactError(E050_TYPE, "args", f"args[{i}] must be an object, got {json_type(arg)}")
            )
            continue
        if "name" not in arg:
            errors.append(
                ArtifactError(E010_MISSING_FIELD, "args", f"args[{i}] is missing required 'name'")
            )
        elif not isinstance(arg["name"], str):
            errors.append(
                ArtifactError(E050_TYPE, "args", f"args[{i}].name must be a string")
            )
        if "required" in arg and not isinstance(arg["required"], bool):
            errors.append(
                ArtifactError(E050_TYPE, "args", f"args[{i}].required must be a boolean")
            )
        if "description" in arg and not isinstance(arg["description"], str):
            errors.append(
                ArtifactError(E050_TYPE, "args", f"args[{i}].description must be a string")
            )


def _validate_hook(fm: dict[str, Any], errors: list[ArtifactError]) -> None:
    _validate_required(fm, kind_schema("hook"), errors)
    event_enum = tuple(kind_schema("hook")["properties"]["event"]["enum"])
    _check_enum(fm, "event", event_enum, errors)
    _check_type(fm, "action", "string", errors)
    _check_type(fm, "matcher", "string", errors)


def _validate_memory(fm: dict[str, Any], errors: list[ArtifactError]) -> None:
    _check_enum(fm, "priority", ("low", "normal", "high"), errors)
    # Both scopes compile: global memories land in ~/.claude's CLAUDE.md corpus;
    # project memories compile into the repo's own corpus (.claude/cohort/
    # CLAUDE.cohort.md), imported by the managed CLAUDE.md block (do_install_project
    # wires the second @import). No scope constraint.


def _validate_context(fm: dict[str, Any], errors: list[ArtifactError]) -> None:
    if fm.get("scope") != "project":
        errors.append(
            ArtifactError(
                E070_SCOPE_CONSTRAINT, "scope", "context artifacts must have scope: project"
            )
        )
    if fm.get("name") != CONTEXT_REQUIRED_NAME:
        errors.append(
            ArtifactError(
                E070_SCOPE_CONSTRAINT,
                "name",
                f"context name must be {CONTEXT_REQUIRED_NAME!r}",
            )
        )


_KIND_VALIDATORS = {
    "agent": _validate_agent,
    "command": _validate_command,
    "hook": _validate_hook,
    "memory": _validate_memory,
    "context": _validate_context,
    "skill": lambda fm, errors: _check_array_of_strings(fm, "triggers", errors),
}


# --- Top-level validation ---------------------------------------------------


def validate_frontmatter(fm: dict[str, Any], name_stem: str) -> list[ArtifactError]:
    """Validate a parsed frontmatter mapping; return all collected errors.

    Implements the staged model: shared checks always run and collect; per-kind
    checks (including unknown-field detection) run only when ``kind`` is a valid
    enum value.
    """
    errors: list[ArtifactError] = []
    _validate_shared(fm, name_stem, errors)

    kind = fm.get("kind")
    kind_valid = isinstance(kind, str) and kind in KINDS
    if kind_valid:
        _validate_unknown_fields(fm, kind, errors)
        _KIND_VALIDATORS[kind](fm, errors)
    return errors


def apply_defaults(fm: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``fm`` with documented defaults filled in.

    Defaults: ``version=0.1.0``; agent ``topology=specialist``, ``advisory=true``,
    ``tools=[]``; command ``dry_run=true`` and per-arg ``required=false``; memory
    ``priority=normal``. Validation operates on the *raw* mapping, so omitting a
    field never trips its invariant — defaults are for the normalized artifact.
    """
    out = dict(fm)
    out.setdefault("version", DEFAULT_VERSION)
    kind = out.get("kind")
    if kind == "agent":
        out.setdefault("topology", "specialist")
        out.setdefault("advisory", True)
        out.setdefault("tools", [])
    elif kind == "command":
        out.setdefault("dry_run", True)
        if isinstance(out.get("args"), list):
            normalized_args = []
            for arg in out["args"]:
                if isinstance(arg, dict):
                    arg = dict(arg)
                    arg.setdefault("required", False)
                normalized_args.append(arg)
            out["args"] = normalized_args
    elif kind == "memory":
        out.setdefault("priority", "normal")
    return out


# --- File- and tree-level validation ---------------------------------------


class FileResult:
    """Validation outcome for one artifact file."""

    def __init__(
        self,
        path: Path,
        kind: Optional[str],
        name: Optional[str],
        scope: Optional[str],
        errors: list[ArtifactError],
    ) -> None:
        self.path = path
        self.kind = kind
        self.name = name
        self.scope = scope
        self.errors = errors

    @property
    def status(self) -> str:
        return "fail" if self.errors else "pass"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "kind": self.kind,
            "name": self.name,
            "scope": self.scope,
            "status": self.status,
            "errors": [e.to_dict() for e in self.errors],
        }


def validate_load_result(result: LoadResult) -> FileResult:
    """Validate an already-loaded artifact (E001 is a hard stop)."""
    if result.load_error is not None:
        return FileResult(result.path, None, None, None, [result.load_error])
    fm = result.frontmatter or {}
    errors = validate_frontmatter(fm, result.name_stem)
    return FileResult(
        path=result.path,
        kind=fm.get("kind") if isinstance(fm.get("kind"), str) else None,
        name=fm.get("name") if isinstance(fm.get("name"), str) else None,
        scope=fm.get("scope") if isinstance(fm.get("scope"), str) else None,
        errors=errors,
    )


def validate_file(path: Path | str) -> FileResult:
    """Load and validate one artifact file."""
    return validate_load_result(load_artifact(path))


def discover_artifacts(root: Path | str) -> list[Path]:
    """Return all ``.md`` artifact files under ``root``, recursively, sorted."""
    root = Path(root)
    return sorted(root.rglob("*.md"))


class TreeResult:
    """Aggregate validation outcome for a tree of artifacts."""

    def __init__(self, results: list[FileResult]) -> None:
        self.results = results

    @property
    def valid(self) -> bool:
        return all(r.status == "pass" for r in self.results)

    @property
    def summary(self) -> dict[str, int]:
        valid = sum(1 for r in self.results if r.status == "pass")
        return {"total": len(self.results), "valid": valid, "invalid": len(self.results) - valid}

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "artifacts": [r.to_dict() for r in self.results],
            "summary": self.summary,
        }


def validate_tree(root: Path | str) -> TreeResult:
    """Validate every artifact under ``root`` and apply tree-level uniqueness.

    Duplicate detection is keyed on ``(kind, name, scope)`` — the same name under
    different scopes is *not* a duplicate. The second (and later) occurrence in
    sorted-path order receives E080.
    """
    results = [validate_file(p) for p in discover_artifacts(root)]
    seen: dict[tuple[str, str, str], Path] = {}
    for r in results:
        if r.kind and r.name and r.scope:
            key = (r.kind, r.name, r.scope)
            if key in seen:
                r.errors.append(
                    ArtifactError(
                        E080_DUPLICATE,
                        None,
                        f"duplicate (kind={r.kind}, name={r.name}, scope={r.scope}); "
                        f"first defined at {seen[key]}",
                    )
                )
            else:
                seen[key] = r.path
    return TreeResult(results)
