"""Generate the Cohort quick-reference (HTML + PDF) from canonical.

The reference is a *derived* doc, never hand-maintained: descriptions come from the
canonical command/skill frontmatter, so they can't drift. Curated sections order the
day-to-day workflow first; any canonical command or skill not placed in a curated section
falls into a "More" catch-all, so a newly added artifact can never silently vanish from
the reference — and a parity test fails CI if the committed reference is missing one,
which is what keeps ``docs/quick-reference.*`` current.

``render_pdf`` shells out to headless Chrome; it's best-effort (returns ``False`` if no
Chrome is available), so PDF rendering is never a hard dependency of the build or the test.
"""

from __future__ import annotations

import html as _html
import shutil
import subprocess
from pathlib import Path

# Curated layout: section title -> ordered entries. An entry is one of:
#   ("cmd", name)          a canonical /command (description pulled from canonical)
#   ("skill", name)        a canonical skill    (description pulled from canonical)
#   ("cli", label, desc)   a `cohort ...` CLI command (not a canonical artifact)
#   ("note", text)         a highlighted callout
# Any canonical command/skill NOT named here is appended to a "More" section.
_SECTIONS: list[tuple[str, list[tuple]]] = [
    ("Build & ship · daily", [
        ("cmd", "plan"), ("cmd", "build"), ("cmd", "test"), ("cmd", "goal"),
        ("cmd", "code-review"), ("cmd", "code-simplify"), ("cmd", "spec"), ("cmd", "ship"),
    ]),
    ("Multi-vendor · orchestrate & review", [
        ("cmd", "crew"), ("cmd", "scout"), ("cmd", "ratchet"),
        ("cmd", "consult-gpt"), ("cmd", "consult-grok"),
    ]),
    ("External engines · cohort engine …", [
        ("cli", "consult &lt;e&gt; --tier", "One-shot advisory, read-only. Tier: flagship | cheap."),
        ("cli", "review &lt;e&gt;", "Read-only agentic explore loop — gated per read, transcript recorded."),
        ("cli", "propose &lt;e&gt; --agentic", "Engine proposes a patch; Cohort applies it in a worktree behind the gates. Grok's write path."),
        ("cli", "work gpt", "Codex edits natively in its OS sandbox, confined to a throwaway worktree."),
        ("cli", "ratchet &lt;gpt|grok&gt;", "The /ratchet loop from the CLI. --evaluator \"…\" --budget N."),
        ("note", "The line: Claude writes your tree; external engines only ever touch a throwaway worktree — gated proposal (Grok) or OS-sandbox (Codex). You review &amp; merge."),
    ]),
    ("The office", [
        ("cli", "ChiefOfStaff", "Ask first for cross-functional questions — it names the right specialist(s) to consult."),
        ("cmd", "office-setup"), ("cmd", "project-setup"),
    ]),
    ("Manage Cohort · cohort …", [
        ("cli", "dashboard", "Local web UI (mission control) at 127.0.0.1:8787."),
        ("cmd", "update"),
        ("cli", "recompile --ide X", "Recompile the office into an IDE (claude/codex/cursor)."),
        ("cli", "status", "Read-only view of the install."),
        ("cli", "my-office sync", "Sync your personal layer to/from a git remote."),
        ("cli", "my-office review·approve", "Review & approve quarantined pulled artifacts."),
        ("cli", "office review·approve", "Review & approve office-layer quarantine."),
        ("cmd", "snapshot"), ("cmd", "feedback"),
        ("cli", "distill", "Compound recent sessions + feedback into project memory."),
    ]),
    ("Skills · auto-fire when relevant", [
        ("skill", "office-guide"), ("skill", "adversarial-review"), ("skill", "karpathy-discipline"),
    ]),
]

_STYLE = """
  @page { size: Letter; margin: 0.5in 0.55in; }
  :root{ --ink:#1a1a1a; --sub:#5c5c5c; --accent:#a4650f; --line:#e2ddd4; --chip:#f4efe6;
    --mono:"SF Mono",ui-monospace,Menlo,Consolas,monospace;
    --sans:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  *{box-sizing:border-box} html,body{margin:0;padding:0}
  body{font-family:var(--sans);color:var(--ink);font-size:10.3px;line-height:1.4;
    -webkit-print-color-adjust:exact;print-color-adjust:exact}
  header{border-bottom:2px solid var(--accent);padding-bottom:8px;margin-bottom:12px}
  h1{font-size:20px;margin:0;letter-spacing:-.01em} h1 .c{color:var(--accent)}
  .tag{font-size:9.5px;color:var(--sub);margin-top:3px}
  .cols{column-count:2;column-gap:20px}
  section{break-inside:avoid;margin-bottom:13px}
  h2{font-family:var(--mono);font-size:9px;letter-spacing:.13em;text-transform:uppercase;
    color:var(--accent);margin:0 0 6px;padding-bottom:3px;border-bottom:1px solid var(--line);font-weight:700}
  .row{display:grid;grid-template-columns:auto 1fr;gap:8px;padding:2.5px 0;align-items:baseline}
  .cmd{font-family:var(--mono);font-size:9.6px;color:var(--ink);white-space:nowrap;font-weight:600}
  .cmd .o{color:var(--sub);font-weight:400}
  .desc{color:var(--sub);font-size:9.6px}
  .note{background:var(--chip);border-left:2px solid var(--accent);padding:4px 7px;margin-top:5px;
    font-size:9px;color:#463c2c;border-radius:0 3px 3px 0}
  footer{margin-top:6px;padding-top:6px;border-top:1px solid var(--line);font-size:8.5px;color:var(--sub);
    display:flex;justify-content:space-between} .mono{font-family:var(--mono)}
"""


def _frontmatter(path: Path) -> tuple[str, str]:
    """Return (name, description) from a canonical artifact's frontmatter."""
    name = path.stem
    description = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("name:"):
            name = s[len("name:"):].strip()
        elif s.startswith("description:"):
            description = s[len("description:"):].strip()
        elif s == "---" and description:
            break
    return name, description


def _canonical(source_root: Path, kind: str) -> dict[str, str]:
    """Map ``name -> description`` for every canonical artifact of a kind (commands/skills)."""
    d: dict[str, str] = {}
    folder = source_root / "canonical" / kind
    for f in sorted(folder.glob("*.md")):
        name, desc = _frontmatter(f)
        d[name] = desc
    return d


def _row(cmd: str, desc: str, mono_cmd: bool = True) -> str:
    c = cmd if mono_cmd else _html.escape(cmd)
    return f'<div class="row"><span class="cmd">{c}</span><span class="desc">{_html.escape(desc)}</span></div>'


def build_html(source_root: Path) -> str:
    """Build the deterministic reference HTML from canonical + the curated layout."""
    commands = _canonical(source_root, "commands")
    skills = _canonical(source_root, "skills")
    placed_cmds: set[str] = set()
    placed_skills: set[str] = set()

    body_parts: list[str] = []
    for title, entries in _SECTIONS:
        rows: list[str] = []
        for entry in entries:
            if entry[0] == "cmd":
                nm = entry[1]
                rows.append(_row(f"/{nm}", commands.get(nm, "")))
                placed_cmds.add(nm)
            elif entry[0] == "skill":
                nm = entry[1]
                rows.append(_row(nm, skills.get(nm, "")))
                placed_skills.add(nm)
            elif entry[0] == "cli":
                rows.append(f'<div class="row"><span class="cmd">{entry[1]}</span><span class="desc">{_html.escape(entry[2])}</span></div>')
            elif entry[0] == "note":
                rows.append(f'<div class="note">{entry[1]}</div>')
        body_parts.append(f"<section><h2>{_html.escape(title)}</h2>{''.join(rows)}</section>")

    # Catch-all: any canonical command/skill not placed above, so nothing is ever dropped.
    extra_cmds = [n for n in sorted(commands) if n not in placed_cmds]
    extra_skills = [n for n in sorted(skills) if n not in placed_skills]
    if extra_cmds:
        rows = "".join(_row(f"/{n}", commands[n]) for n in extra_cmds)
        body_parts.append(f"<section><h2>More commands</h2>{rows}</section>")
    if extra_skills:
        rows = "".join(_row(n, skills[n]) for n in extra_skills)
        body_parts.append(f"<section><h2>More skills</h2>{rows}</section>")

    return (
        "<!doctype html>\n<html><head><meta charset=\"utf-8\">\n"
        f"<style>{_STYLE}</style></head><body>\n"
        "<header><h1><span class=\"c\">Cohort</span> — Quick Reference</h1>"
        "<div class=\"tag\">Commands &amp; skills, ordered for day-to-day use. Slash commands run in your "
        "IDE; <span class=\"mono\">cohort …</span> in the terminal. Generated from canonical.</div></header>\n"
        f"<div class=\"cols\">\n{''.join(body_parts)}\n</div>\n"
        "<footer><span>Cohort agentic office · quick reference</span>"
        "<span>Fable coordinates · Claude writes · external engines propose, gated · human merges</span>"
        "</footer></body></html>\n"
    )


def canonical_names(source_root: Path) -> set[str]:
    """Every canonical command name (``/name``) and skill name — what the reference must
    always contain (the parity contract)."""
    cmds = {f"/{n}" for n in _canonical(source_root, "commands")}
    skills = set(_canonical(source_root, "skills"))
    return cmds | skills


def render_pdf(html_path: Path, pdf_path: Path) -> bool:
    """Render ``html_path`` to ``pdf_path`` via headless Chrome. Returns ``False`` (never
    raises) if no Chrome binary is available — PDF rendering is best-effort."""
    chrome = next((b for b in ("google-chrome", "chromium", "chromium-browser") if shutil.which(b)), None)
    if chrome is None:
        return False
    try:
        subprocess.run(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-pdf-header-footer",
             f"--print-to-pdf={pdf_path}", f"file://{html_path}"],
            check=True, capture_output=True, timeout=120,
        )
        return pdf_path.exists() and pdf_path.stat().st_size > 0
    except (subprocess.SubprocessError, OSError):
        return False


def write_reference(source_root: Path, docs_dir: Path) -> tuple[Path, Path | None]:
    """Write ``docs/quick-reference.html`` and render ``.pdf`` (best-effort). Returns the
    two paths (pdf is ``None`` if Chrome was unavailable)."""
    docs_dir.mkdir(parents=True, exist_ok=True)
    html_path = docs_dir / "quick-reference.html"
    pdf_path = docs_dir / "quick-reference.pdf"
    html_path.write_text(build_html(source_root), encoding="utf-8")
    ok = render_pdf(html_path, pdf_path)
    return html_path, (pdf_path if ok else None)
