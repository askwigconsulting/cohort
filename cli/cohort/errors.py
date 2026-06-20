"""Stable error-code contract for canonical artifact validation.

The codes are part of the public contract: tests assert on them, so schema
refactors must not change them silently. See the Phase 0 spec §1.4.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

# --- Stable error codes -----------------------------------------------------
E001_FRONTMATTER_PARSE = "E001_FRONTMATTER_PARSE"
E010_MISSING_FIELD = "E010_MISSING_FIELD"
E011_FIELD_LENGTH = "E011_FIELD_LENGTH"
E020_BAD_ENUM = "E020_BAD_ENUM"
E030_NAME_MISMATCH = "E030_NAME_MISMATCH"
E040_TARGETS_INVALID = "E040_TARGETS_INVALID"
E050_TYPE = "E050_TYPE"
E060_SAFETY_INVARIANT = "E060_SAFETY_INVARIANT"
E070_SCOPE_CONSTRAINT = "E070_SCOPE_CONSTRAINT"
E080_DUPLICATE = "E080_DUPLICATE"
E090_UNKNOWN_FIELD = "E090_UNKNOWN_FIELD"

ALL_CODES = frozenset(
    {
        E001_FRONTMATTER_PARSE,
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
    }
)


@dataclass(frozen=True)
class ArtifactError:
    """A single validation failure.

    ``variant`` is an optional sub-kind used where one code covers more than one
    distinct failure path (e.g. E030 covers both the slug-pattern path and the
    name-not-equal-to-stem path); it is surfaced in messages and JSON so each
    path is provably distinct.
    """

    code: str
    field: Optional[str]
    message: str
    variant: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code,
            "field": self.field,
            "message": self.message,
        }
        if self.variant is not None:
            out["variant"] = self.variant
        return out
