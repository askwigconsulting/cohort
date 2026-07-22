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


# The single documented tier→model mapping, and the orchestration canon it governs.
_MODEL_TIERS_DOC = "docs/model-tiers.md"
_ORCH_CANON = (
    "canonical/commands/crew.md",
    "canonical/memories/model-orchestration.md",
    "canonical/memories/fable-mode.md",
)
# A "| tier | model |" table row (first two cells), lower-cased.
_TABLE_ROW = re.compile(r"^\|\s*([a-z0-9]+)\s*\|\s*([a-z0-9]+)\s*\|", re.IGNORECASE)


def _parse_tier_table(doc_text: str, heading: str) -> dict[str, str]:
    """Parse the two-column ``| tier | model |`` rows under a ``## heading``
    section of the model-tiers doc. Skips the header/separator rows."""
    out: dict[str, str] = {}
    in_section = False
    for line in doc_text.splitlines():
        if line.startswith("## "):
            in_section = heading in line
            continue
        if not in_section:
            continue
        m = _TABLE_ROW.match(line)
        if not m:
            continue
        tier, model = m.group(1).lower(), m.group(2).lower()
        if tier in ("tier", "---") or set(model) <= {"-"}:
            continue
        out[tier] = model
    return out


def _model_tier_findings(repo_root: Path) -> list[LintFinding]:
    """The `docs/model-tiers.md` registry must not drift: its agent-tier table
    must equal the renderer's `_MODEL_MAP`, and every orchestration tier it
    lists must still appear in the orchestration canon."""
    from .adapters.claude import _MODEL_MAP  # code source of truth for agent tiers

    doc = repo_root / _MODEL_TIERS_DOC
    if not doc.is_file():
        return [LintFinding(_MODEL_TIERS_DOC, 0, "the model-tiers mapping doc is missing")]
    text = doc.read_text(encoding="utf-8")
    findings: list[LintFinding] = []

    documented = _parse_tier_table(text, "Agent model tier")
    if documented != _MODEL_MAP:
        findings.append(
            LintFinding(
                _MODEL_TIERS_DOC,
                0,
                f"agent-tier table {documented} disagrees with renderer _MODEL_MAP {_MODEL_MAP}",
            )
        )

    orch = _parse_tier_table(text, "Orchestration routing tier")
    canon_text = "\n".join(
        (repo_root / p).read_text(encoding="utf-8")
        for p in _ORCH_CANON
        if (repo_root / p).is_file()
    )
    for tier in orch:
        if not re.search(rf"\b{re.escape(tier)}\b", canon_text, re.IGNORECASE):
            findings.append(
                LintFinding(
                    _MODEL_TIERS_DOC,
                    0,
                    f'orchestration tier "{tier}" is documented but appears in no canon file',
                )
            )
    return findings


# A "≤N agents in flight" orchestration cap, wherever it is restated in prose.
_ORCH_CAP_RE = re.compile(
    r"(\d+)\s+(?:agents?\s+|reviewers?\s+)?in\s+flight", re.IGNORECASE
)


def _declared_orch_cap(text: str) -> int | None:
    """The in-flight cap declared in the model-tiers registry (source of truth)."""
    m = re.search(r"in-flight cap[^0-9]*?(\d+)", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _orchestration_cap_findings(repo_root: Path) -> list[LintFinding]:
    """The "≤N agents in flight" cap is restated in prose across the orchestration
    canon (crew.md, scout.md, the office/adversarial-review skills, ...). This keeps
    that *number* consistent: ``docs/model-tiers.md`` declares it once, and every
    canonical file that restates an in-flight cap must use the same value — a drift is
    a lint failure, not a silently divergent protocol.

    This checks agreement of the documented number only; it is not runtime enforcement.
    The cap itself is coordinator discipline plus the human PR gate by design (see the
    DESIGN ``[S]`` decision) — the lint just stops the canon saying two different things.
    """
    doc = repo_root / _MODEL_TIERS_DOC
    if not doc.is_file():
        return []  # a missing registry is already reported by _model_tier_findings
    cap = _declared_orch_cap(doc.read_text(encoding="utf-8"))
    if cap is None:
        return [
            LintFinding(
                _MODEL_TIERS_DOC,
                0,
                "no in-flight cap is declared (expected an 'in-flight cap ... N' line)",
            )
        ]
    findings: list[LintFinding] = []
    for path in sorted((repo_root / "canonical").rglob("*.md")):
        rel = path.relative_to(repo_root).as_posix()
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in _ORCH_CAP_RE.finditer(line):
                stated = int(m.group(1))
                if stated != cap:
                    findings.append(
                        LintFinding(
                            rel,
                            lineno,
                            f'states "{stated} ... in flight" but the orchestration '
                            f"cap is {cap} (docs/model-tiers.md)",
                        )
                    )
    return findings


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
    findings.extend(_model_tier_findings(repo_root))
    findings.extend(_orchestration_cap_findings(repo_root))
    return findings
