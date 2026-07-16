"""Documentation-parity lint: human-facing docs must not drift from reality.

Cohort's golden locks guard *compiled* output, but nothing checks that the
prose in README/DESIGN/etc. still matches the canonical filesystem — and doc
drift is the drift a user sees first (a stale "18-agent roster" line). This
lint closes that gap in the same spirit as the rest of the harness: derive the
truth from the filesystem, never store it, and fail closed on a mismatch.

v1 checks **count parity**: any count stated in the unambiguous compound-
adjective form — ``17-agent``, ``5-hook``, ``3-memory`` — must equal the actual
number of canonical artifacts of that kind. The compound form is used
deliberately: a real count claim is written ``17-agent roster`` (hyphenated),
never ``10 agents in flight`` (spaced, and not a roster count), so this form
carries essentially zero false positives. The spaced form and other checks
(version strings, IDE-mention parity) are deferred rather than shipped noisy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# canonical kind (singular) -> its directory under canonical/
_KIND_DIR = {
    "agent": "agents",
    "skill": "skills",
    "command": "commands",
    "hook": "hooks",
    "memory": "memories",
}

# Human-facing docs scanned for count claims. CHANGELOG is excluded on purpose:
# its dated entries legitimately record the counts that were true at the time.
_DOCS = ("README.md", "CONTRIBUTING.md", "GOVERNANCE.md")
_DOC_DIRS = ("docs",)

# A count claim in compound-adjective form: "17-agent", "5-hook", "3-memory".
_COMPOUND = re.compile(r"\b(\d+)-(" + "|".join(_KIND_DIR) + r")\b", re.IGNORECASE)


@dataclass(frozen=True)
class LintFinding:
    """One doc/reality mismatch, anchored to a file and line."""

    file: str
    line: int
    message: str


def canonical_counts(repo_root: Path) -> dict[str, int]:
    """The true count of canonical artifacts per kind, derived from the
    filesystem (never stored, so it cannot drift)."""
    return {
        kind: len(list((repo_root / "canonical" / subdir).glob("*.md")))
        for kind, subdir in _KIND_DIR.items()
    }


def _iter_docs(repo_root: Path):
    for name in _DOCS:
        p = repo_root / name
        if p.is_file():
            yield p
    for d in _DOC_DIRS:
        for p in sorted((repo_root / d).glob("*.md")):
            if p.is_file():
                yield p


def run_lint(repo_root: Path) -> list[LintFinding]:
    """Return every doc-parity finding under ``repo_root`` (empty = clean)."""
    counts = canonical_counts(repo_root)
    findings: list[LintFinding] = []
    for doc in _iter_docs(repo_root):
        rel = doc.relative_to(repo_root).as_posix()
        for lineno, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            for m in _COMPOUND.finditer(line):
                claimed = int(m.group(1))
                kind = m.group(2).lower()
                actual = counts[kind]
                if claimed != actual:
                    findings.append(
                        LintFinding(
                            file=rel,
                            line=lineno,
                            message=(
                                f'states "{claimed}-{kind}" but canonical has '
                                f"{actual} {kind}(s)"
                            ),
                        )
                    )
    return findings
