"""RFC 0003 WS-A: the life template — scaffold, marker semantics, purge safety,
permission-profile locks, and the sync-boundary refusals (distill / adopt /
promote) plus connector-presence reporting.

The permission-profile assertions are wording-locked contracts: the scaffolded
profiles must match RFC 0003 §3 (enumerated read allowlist, every outbound tool
denied, no server wildcard, Bash/WebFetch/WebSearch denied + dontAsk in the
briefing profile). Do not weaken them without updating the RFC.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cohort.adopt import AdoptError, do_adopt
from cohort.distill import do_distill
from cohort.install_model import CohortPaths
from cohort.life import connector_status
from cohort.project import (
    _parse_toml_minimal,
    _read_staleness_hours,
    do_deinit,
    do_init,
    is_life_project,
    project_template,
    read_dashboard_private,
    read_project_config,
    write_life_data,
)
from cohort.specialists import PromoteError, do_promote
from cohort.status import do_status

COHORT_SRC = Path(__file__).resolve().parents[1]


def make_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "d@e.com"], cwd=path, check=True)
    (path / "README.md").write_text("# r\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return path


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def life_repo(tmp_path, home):
    repo = make_git_repo(tmp_path / "my_life")
    report = do_init(repo, COHORT_SRC, dry_run=False, home=home, template="life")
    assert "error" not in report
    return repo


@pytest.fixture
def code_repo(tmp_path, home):
    repo = make_git_repo(tmp_path / "code")
    do_init(repo, COHORT_SRC, dry_run=False, home=home)
    return repo


# === scaffold & marker ========================================================


def test_init_life_scaffolds_layout_marker_and_profiles(life_repo):
    year = datetime.now().astimezone().year
    assert (life_repo / "inbox.md").exists()
    assert (life_repo / "goals" / f"{year}.md").exists()
    assert (life_repo / "weeks").is_dir() and not any((life_repo / "weeks").iterdir())
    assert (life_repo / "days").is_dir() and not any((life_repo / "days").iterdir())
    assert (life_repo / ".mcp.json.example").exists()
    assert (life_repo / ".claude" / "settings.json").exists()
    assert (life_repo / ".claude" / "settings.briefing.json").exists()
    assert (life_repo / ".cohort" / "reports" / "briefings").is_dir()
    paths = CohortPaths.for_project(life_repo)
    assert project_template(paths) == "life"
    assert is_life_project(paths)
    assert read_dashboard_private(paths) is True
    context = (life_repo / ".cohort" / "project_context.md").read_text(encoding="utf-8")
    assert "never push this repository to a public remote" in context


def test_life_mcp_example_pins_canonical_server_keys(life_repo):
    data = json.loads((life_repo / ".mcp.json.example").read_text(encoding="utf-8"))
    assert sorted(data["mcpServers"]) == ["calendar", "drive", "gmail"]


def test_init_life_only_seeds_non_dated_files(life_repo):
    # Dated files are the rhythm commands' job — a scaffolded days/<today>.md
    # would be stale by construction and manifest-recorded.
    assert not any((life_repo / "days").glob("*.md"))
    assert not any((life_repo / "weeks").glob("*.md"))


def test_reinit_with_template_over_existing_cohort_toml_refuses(life_repo, code_repo, home):
    for repo in (life_repo, code_repo):
        report = do_init(repo, COHORT_SRC, dry_run=False, home=home, template="life")
        assert 'template = "life"' in report["error"]  # prints the line to add by hand


def test_init_with_unknown_template_refuses(tmp_path, home):
    repo = make_git_repo(tmp_path / "x")
    report = do_init(repo, COHORT_SRC, dry_run=False, home=home, template="startup")
    assert "unknown template" in report["error"]


def test_plain_reinit_of_life_project_is_idempotent_and_preserves_edits(life_repo, home):
    goals = next((life_repo / "goals").glob("*.md"))
    goals.write_text("# my goals\n\n- [ ] ship the thing\n", encoding="utf-8")
    (life_repo / "inbox.md").write_text("# Inbox\n\n- [ ] call mom\n", encoding="utf-8")
    report = do_init(life_repo, COHORT_SRC, dry_run=False, home=home)  # no --template
    assert "error" not in report
    assert report["template"] == "life"  # the marker drives the re-init plan
    assert "ship the thing" in goals.read_text(encoding="utf-8")
    assert "call mom" in (life_repo / "inbox.md").read_text(encoding="utf-8")


def test_write_life_data_never_overwrites_existing_files(life_repo):
    (life_repo / "inbox.md").write_text("mine\n", encoding="utf-8")
    written = write_life_data(life_repo)
    assert "inbox.md" not in written
    assert (life_repo / "inbox.md").read_text(encoding="utf-8") == "mine\n"


# === purge safety =============================================================


def test_deinit_purge_leaves_life_data_intact(life_repo, home):
    goals = next((life_repo / "goals").glob("*.md"))
    goals.write_text("# a year of goals — must survive purge\n", encoding="utf-8")
    report = do_deinit(life_repo, purge=True, dry_run=False, home=home)
    assert not (life_repo / ".cohort").exists()  # the office wiring is gone
    assert goals.read_text(encoding="utf-8").startswith("# a year of goals")
    assert (life_repo / "inbox.md").exists()
    assert (life_repo / "weeks").is_dir() and (life_repo / "days").is_dir()
    assert "left in place" in report["life_data_note"]


def test_deinit_dry_run_also_warns_life_data_is_kept(life_repo, home):
    report = do_deinit(life_repo, purge=True, dry_run=True, home=home)
    assert "left in place" in report["life_data_note"]
    assert (life_repo / ".cohort").exists()  # dry-run changed nothing


def test_life_data_is_not_in_the_scaffold_manifest(life_repo):
    manifest = json.loads(
        (life_repo / ".cohort" / "state" / "manifest.json").read_text(encoding="utf-8")
    )
    dests = [op["dest"] for op in manifest["ops"]]
    for fragment in ("inbox.md", "goals", "weeks", "days"):
        assert not any(Path(d).name == fragment for d in dests), fragment


# === permission-profile locks (RFC 0003 §3, verbatim) =========================

OUTBOUND_TOOLS = [
    "mcp__gmail__create_draft", "mcp__gmail__create_label",
    "mcp__gmail__label_message", "mcp__gmail__label_thread",
    "mcp__gmail__unlabel_message", "mcp__gmail__unlabel_thread",
    "mcp__calendar__create_event", "mcp__calendar__update_event",
    "mcp__calendar__delete_event", "mcp__calendar__respond_to_event",
    "mcp__drive__create_file", "mcp__drive__copy_file",
]


def _profile(repo: Path, name: str) -> dict:
    return json.loads((repo / ".claude" / name).read_text(encoding="utf-8"))


def test_interactive_profile_denies_every_outbound_tool_and_web(life_repo):
    perms = _profile(life_repo, "settings.json")["permissions"]
    for tool in OUTBOUND_TOOLS + ["WebFetch", "WebSearch"]:
        assert tool in perms["deny"], tool
        assert tool not in perms["allow"], tool


def test_briefing_profile_is_strictly_less_and_denies_bash_and_web(life_repo):
    interactive = _profile(life_repo, "settings.json")["permissions"]
    briefing = _profile(life_repo, "settings.briefing.json")["permissions"]
    for tool in ("Bash", "WebFetch", "WebSearch"):
        assert tool in briefing["deny"], tool
    assert briefing["defaultMode"] == "dontAsk"  # any unmatched tool auto-denies
    mcp_allows = [e for e in briefing["allow"] if e.startswith("mcp__")]
    assert set(mcp_allows) < set(interactive["allow"])  # strictly less than interactive
    # writes confined to the quarantine only — never the trusted day/week tier
    assert "Write(.cohort/reports/briefings/**)" in briefing["allow"]
    assert "Write(days/**)" in briefing["deny"]
    assert "Write(weeks/**)" in briefing["deny"]


def test_no_server_wildcard_in_any_tier_of_either_profile(life_repo):
    for name in ("settings.json", "settings.briefing.json"):
        perms = _profile(life_repo, name)["permissions"]
        for tier in ("allow", "deny", "ask"):
            for entry in perms.get(tier) or []:
                assert entry != "*", name
                if entry.startswith("mcp__"):
                    assert "*" not in entry, f"{name}: {entry}"  # every tool named exactly


def test_briefing_quarantine_is_gitignored(life_repo):
    probe = ".cohort/reports/briefings/2026-07-10.md"
    rc = subprocess.run(["git", "check-ignore", "-q", probe], cwd=life_repo).returncode
    assert rc == 0, "briefing quarantine must be gitignored"
    # and the trusted tiers are NOT ignored (they are the git-tracked life system)
    rc = subprocess.run(["git", "check-ignore", "-q", "inbox.md"], cwd=life_repo).returncode
    assert rc != 0


# === life-scoped distill (targets week ## Review; refuses project_context) ===


def _write_session(repo: Path, name: str, decision: str) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (repo / ".cohort" / "sessions" / name).write_text(
        f"---\ntimestamp: {ts}\nauthor: Dev\nbranch: main\n---\n"
        f"## Decisions\n- {decision}\n\n## Open items\n\n",
        encoding="utf-8",
    )


def test_life_distill_targets_week_review_and_refuses_project_context(life_repo):
    _write_session(life_repo, "s1.md", "Planned the garden overhaul")
    context_before = (life_repo / ".cohort" / "project_context.md").read_text(encoding="utf-8")
    report = do_distill(life_repo, days=30, dry_run=False, confirm=lambda _d: True)
    assert report["applied"] is True
    assert report["target"].startswith("weeks/")
    week = (life_repo / report["target"]).read_text(encoding="utf-8")
    assert "## Review" in week
    assert "### Distilled" in week  # nested under Review, one level down
    assert "Planned the garden overhaul" in week
    # the refusal: connector-adjacent text never enters the @imported corpus
    after = (life_repo / ".cohort" / "project_context.md").read_text(encoding="utf-8")
    assert after == context_before


def test_life_distill_appends_into_an_existing_week_file(life_repo):
    from cohort.life import local_today, week_label

    week_file = life_repo / "weeks" / f"{week_label(local_today())}.md"
    week_file.parent.mkdir(exist_ok=True)
    week_file.write_text(
        "# week\n\n## Plan\n\n- [ ] existing plan item\n\n## Review\n\nhand-written review\n",
        encoding="utf-8",
    )
    _write_session(life_repo, "s2.md", "Fixed the bike")
    report = do_distill(life_repo, days=30, dry_run=False, confirm=lambda _d: True)
    assert report["applied"] is True
    text = week_file.read_text(encoding="utf-8")
    assert "existing plan item" in text and "hand-written review" in text
    assert text.index("hand-written review") < text.index("### Distilled")


def test_code_project_distill_still_targets_project_context(code_repo):
    _write_session(code_repo, "s1.md", "Chose SQLite")
    report = do_distill(code_repo, days=30, dry_run=False, confirm=lambda _d: True)
    assert report["applied"] is True
    assert report["target"] == "project_context.md"


# === sync-boundary refusals (adopt / promote) =================================


def test_adopt_refuses_an_agent_from_a_life_project(life_repo, home):
    agent = life_repo / ".claude" / "agents" / "life-helper.md"
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_text(
        "---\ndescription: Helps with life stuff.\n---\nBe helpful.\n", encoding="utf-8"
    )
    with pytest.raises(AdoptError, match="life project"):
        do_adopt(home, COHORT_SRC, agent)
    assert agent.exists()  # refused before any move/backup


def test_adopt_still_works_from_a_code_project(code_repo, home):
    agent = code_repo / ".claude" / "agents" / "code-helper.md"
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_text(
        "---\ndescription: Helps with code stuff.\n---\nBe helpful.\n", encoding="utf-8"
    )
    report = do_adopt(home, COHORT_SRC, agent, dry_run=True)
    assert report["dry_run"] is True and report["name"] == "code-helper"


def test_promote_refuses_any_lift_out_of_a_life_project(life_repo, home):
    for to in ("my", "office"):
        with pytest.raises(PromoteError, match="life project"):
            do_promote(life_repo, home, "anything", dry_run=True, to=to)


# === connector presence (status; presence/keys only) ==========================


def test_status_reports_absent_connectors_in_a_life_project(life_repo, home):
    report = do_status(home, life_repo)
    assert report["project"]["template"] == "life"
    conn = report["project"]["connectors"]
    assert conn["mcp_json"] is False and conn["example"] is True
    assert conn["profile_keys"] == ["calendar", "drive", "gmail"]


def test_status_warns_on_server_key_mismatch(life_repo):
    (life_repo / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"gmail": {"type": "http", "url": "https://x"}}}),
        encoding="utf-8",
    )
    conn = connector_status(life_repo)
    assert conn["configured_keys"] == ["gmail"]
    assert conn["missing_keys"] == ["calendar", "drive"]  # rules silently match nothing


def test_status_connector_check_reads_keys_only_and_survives_bad_json(life_repo):
    (life_repo / ".mcp.json").write_text("{not json", encoding="utf-8")
    conn = connector_status(life_repo)
    assert conn["parse_error"] is True and conn["missing_keys"] == []


def test_code_project_status_has_no_connector_section(code_repo, home):
    report = do_status(home, code_repo)
    assert "connectors" not in report["project"]
    assert "template" not in report["project"]


# === read_project_config (the one shared reader) ==============================


def test_read_project_config_is_fail_safe_on_garbage(tmp_path):
    repo = tmp_path / "r"
    (repo / ".cohort").mkdir(parents=True)
    (repo / ".cohort" / "cohort.toml").write_text("= = = not toml [[[", encoding="utf-8")
    paths = CohortPaths.for_project(repo)
    assert read_project_config(paths) == {}
    assert project_template(paths) is None  # absent/unreadable = code project
    assert read_dashboard_private(paths) is False


def test_minimal_toml_fallback_parses_the_scaffolded_shape():
    from cohort.project import LIFE_COHORT_TOML_CONTENT

    data = _parse_toml_minimal(LIFE_COHORT_TOML_CONTENT)
    assert data["template"] == "life"
    assert data["staleness_hours"] == 720
    assert data["auto_capture"] is False
    assert data["dashboard"]["private"] is True


def test_dashboard_private_is_fail_safe_private_for_life_only(tmp_path):
    repo = tmp_path / "r"
    (repo / ".cohort").mkdir(parents=True)
    toml = repo / ".cohort" / "cohort.toml"
    toml.write_text('template = "life"\n', encoding="utf-8")  # no [dashboard] at all
    assert read_dashboard_private(CohortPaths.for_project(repo)) is True
    toml.write_text('template = "life"\n[dashboard]\nprivate = false\n', encoding="utf-8")
    assert read_dashboard_private(CohortPaths.for_project(repo)) is False  # deliberate opt-out
    toml.write_text("staleness_hours = 24\n", encoding="utf-8")
    assert read_dashboard_private(CohortPaths.for_project(repo)) is False


def test_life_staleness_threshold_is_large(life_repo):
    assert _read_staleness_hours(CohortPaths.for_project(life_repo)) == 720.0
