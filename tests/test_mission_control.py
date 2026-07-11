"""WS-B: interactive mission control — CSP hardening, the life parser, the
``collect_state`` life extension, ``dashboard.private`` fail-safe exclusion, and
the edit/enqueue action dispatch (no mutation logic in the dashboard)."""

from __future__ import annotations

import http.client
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Reuse the dashboard test harness (fixtures + helpers) verbatim so the server
# wiring, source/home scaffolding, and request plumbing stay in one place.
from test_dashboard import (  # noqa: F401 - fixtures are used by pytest by name
    home,
    inited_repo,
    make_git_repo,
    request,
    run_cli,
    server,
    source,
)


# --- header-aware request helper -------------------------------------------


def request_full(srv, method, path, token=None, host=None):
    """Like ``test_dashboard.request`` but also returns the response headers, so
    CSP/content-type assertions are possible."""
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    headers = {"Host": host or f"127.0.0.1:{srv.server_address[1]}"}
    if token is not None:
        headers["X-Cohort-Token"] = token
    conn.request(method, path, headers=headers)
    res = conn.getresponse()
    data = res.read()
    hdrs = {k.lower(): v for k, v in res.getheaders()}
    conn.close()
    return res.status, data, hdrs


# === CSP hardening (lands first) =============================================


def test_page_csp_forbids_inline_script(server):
    srv, _ = server
    status, _, hdrs = request_full(srv, "GET", "/")
    assert status == 200
    csp = hdrs["content-security-policy"]
    # The load-bearing control: no inline script may execute, so an injected
    # <script> in rendered briefing/job output cannot read the in-DOM token.
    assert "script-src 'self'" in csp
    assert "'unsafe-inline'" not in csp.split("style-src")[0]  # not in script-src
    assert "img-src 'none'" in csp  # connector/job images can't beacon out


def test_page_has_no_inline_script_body(server):
    srv, _ = server
    status, data = request(srv, "GET", "/")
    assert status == 200
    page = data.decode("utf-8")
    # The page script is external and same-origin; nothing executable is inlined.
    assert 'src="/dashboard.js"' in page
    assert "use strict" not in page
    assert "__COHORT_TOKEN__" not in page  # token substituted into the meta tag
    assert srv.token in page


def test_dashboard_js_served_static_without_token(server):
    srv, _ = server
    status, data, hdrs = request_full(srv, "GET", "/dashboard.js")
    assert status == 200
    assert hdrs["content-type"].startswith("application/javascript")
    body = data.decode("utf-8")
    assert "use strict" in body
    # The static file carries no token — it reads one from the page's meta tag.
    assert "__COHORT_TOKEN__" not in body
    assert srv.token not in body


def test_dashboard_js_requires_loopback_host(server):
    srv, _ = server
    status, _, _ = request_full(srv, "GET", "/dashboard.js", host="evil.example.com")
    assert status == 403  # DNS-rebinding cannot even pull the script


def test_dashboard_js_source_has_no_innerhtml():
    """The rendering-discipline invariant, statically: the page script never
    assigns innerHTML/insertAdjacentHTML (briefing + job stdout are textContent)."""
    js = (Path(__file__).resolve().parents[1] / "cli" / "cohort" / "dashboard.js").read_text(
        encoding="utf-8"
    )
    # Match the dangerous forms (property write / HTML-injecting call), not the
    # bare word — a comment naming the invariant must not trip the lock.
    assert ".innerHTML" not in js
    assert ".outerHTML" not in js
    assert "insertAdjacentHTML(" not in js


# === the §1a life parser =====================================================

from cohort import lifedata  # noqa: E402

FIXTURE_LIFE = Path(__file__).resolve().parent / "fixtures" / "life"


def test_parse_day_extracts_agenda_top3_and_log():
    text = (FIXTURE_LIFE / "days" / "2026-07-10.md").read_text(encoding="utf-8")
    day = lifedata.parse_day(text, expected_date="2026-07-10")
    assert day["date"] == "2026-07-10"
    assert day["diagnostics"] == []  # all known headings present, title matches
    # agenda: timed + non-timed events, time+title only
    assert day["agenda"][0] == {"time": "09:00", "title": "Standup"}
    assert day["agenda"][-1] == {"time": None, "title": "Lunch with Sam"}
    # checklist states: [x] done, [ ] open
    assert day["top3"][0] == {"text": "Ship the CSP fix", "done": True}
    assert [t["done"] for t in day["top3"]] == [True, False, False]
    assert "focused this morning" in day["log"]


def test_parse_day_preserves_unknown_sections():
    # An unknown, user-added section round-trips into `sections` — never dropped,
    # never flagged as a missing known heading (RFC §1a "unknown preserved").
    text = (FIXTURE_LIFE / "days" / "2026-07-10.md").read_text(encoding="utf-8")
    day = lifedata.parse_day(text, expected_date="2026-07-10")
    assert "Notes" in day["sections"]
    assert "must be preserved" in day["sections"]["Notes"]
    assert not any("Notes" in d for d in day["diagnostics"])


def test_parse_day_diagnoses_missing_known_heading():
    day = lifedata.parse_day("# 2026-07-10\n\n## Agenda\n- 09:00 Standup\n")
    assert "missing heading: ## Top 3" in day["diagnostics"]
    assert "missing heading: ## Log" in day["diagnostics"]
    assert "missing heading: ## Agenda" not in day["diagnostics"]


def test_parse_day_flags_date_mismatch_and_overlong_top3():
    text = (
        "# 2026-07-09\n\n## Agenda\n\n## Top 3\n"
        "- [ ] a\n- [ ] b\n- [ ] c\n- [ ] d\n\n## Log\n"
    )
    day = lifedata.parse_day(text, expected_date="2026-07-10")
    assert any("does not match filename" in d for d in day["diagnostics"])
    assert any("Top 3 has 4 items" in d for d in day["diagnostics"])


def test_checklist_tolerates_one_indent_level_and_x_casing():
    text = "# 2026-07-10\n\n## Agenda\n\n## Top 3\n  - [X] indented done\n\t- [ ] tabbed open\n\n## Log\n"
    day = lifedata.parse_day(text, expected_date="2026-07-10")
    assert day["top3"] == [
        {"text": "indented done", "done": True},
        {"text": "tabbed open", "done": False},
    ]


def test_parse_week_and_goals():
    week = lifedata.parse_week(
        (FIXTURE_LIFE / "weeks" / "2026-W28.md").read_text(encoding="utf-8"),
        expected_week="2026-W28",
    )
    assert week["week"] == "2026-W28"
    assert week["diagnostics"] == []
    assert week["plan"][0] == {"text": "Land RFC 0003", "done": True}

    goals = lifedata.parse_goals((FIXTURE_LIFE / "goals" / "2026.md").read_text(encoding="utf-8"))
    assert goals["title"] == "2026 goals"
    assert goals["diagnostics"] == []
    names = [g["goal"] for g in goals["goals"]]
    assert names == ["Ship Cohort 1.0", "Health"]
    assert goals["goals"][0]["items"][0] == {"text": "RFC 0003 accepted", "done": True}


def test_parse_goals_diagnoses_empty_file():
    g = lifedata.parse_goals("just some prose, no headings\n")
    assert "missing goals title (# <year|quarter> goals)" in g["diagnostics"]
    assert "no goal sections (## <goal>)" in g["diagnostics"]


# === timezone boundary (computed once, passed in) ============================


def test_day_and_week_stems_resolve_in_the_passed_timezone():
    instant = datetime(2026, 7, 11, 2, 30, tzinfo=timezone.utc)
    west = instant.astimezone(timezone(timedelta(hours=-5)))  # still 2026-07-10 locally
    east = instant.astimezone(timezone.utc)                   # 2026-07-11
    assert lifedata.day_stem(west) == "2026-07-10"
    assert lifedata.day_stem(east) == "2026-07-11"
    # ISO week-year tracks the ISO calendar across a year boundary.
    assert lifedata.week_stem(datetime(2027, 1, 1, 12, 0, tzinfo=timezone.utc)) == "2026-W53"


def test_load_life_views_picks_today_from_the_passed_now(tmp_path):
    repo = tmp_path / "life"
    (repo / "days").mkdir(parents=True)
    (repo / "days" / "2026-07-10.md").write_text(
        "# 2026-07-10\n\n## Agenda\n\n## Top 3\n- [ ] tenth\n\n## Log\n", encoding="utf-8"
    )
    (repo / "days" / "2026-07-11.md").write_text(
        "# 2026-07-11\n\n## Agenda\n\n## Top 3\n- [ ] eleventh\n\n## Log\n", encoding="utf-8"
    )
    instant = datetime(2026, 7, 11, 2, 30, tzinfo=timezone.utc)
    west = instant.astimezone(timezone(timedelta(hours=-5)))
    views = lifedata.load_life_views(repo, west)
    assert views["today"]["date"] == "2026-07-10"
    assert views["today"]["top3"] == [{"text": "tenth", "done": False}]


def test_load_life_views_missing_files_diagnose_not_crash(tmp_path):
    views = lifedata.load_life_views(tmp_path, datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc))
    assert views["today"]["date"] == "2026-07-10"
    assert any("no day file yet" in d for d in views["today"]["diagnostics"])
    assert views["goals"] == []


# === config fail-safe ========================================================


def test_read_life_config_is_fail_safe_private_for_life():
    cfg = lifedata.read_life_config(FIXTURE_LIFE / ".cohort")
    assert cfg["template"] == "life" and cfg["private"] is True


def test_life_template_absent_private_key_is_private(tmp_path):
    ch = tmp_path / ".cohort"
    ch.mkdir()
    (ch / "cohort.toml").write_text('template = "life"\n', encoding="utf-8")
    assert lifedata.is_private(ch) is True  # absent key ⇒ private


def test_life_private_false_is_the_deliberate_opt_out(tmp_path):
    ch = tmp_path / ".cohort"
    ch.mkdir()
    (ch / "cohort.toml").write_text(
        'template = "life"\n[dashboard]\nprivate = false\n', encoding="utf-8"
    )
    assert lifedata.is_private(ch) is False


def test_code_project_is_not_private_and_not_life(tmp_path):
    ch = tmp_path / ".cohort"
    ch.mkdir()
    (ch / "cohort.toml").write_text("staleness_hours = 24\n", encoding="utf-8")
    assert lifedata.is_life(ch) is False
    assert lifedata.is_private(ch) is False
