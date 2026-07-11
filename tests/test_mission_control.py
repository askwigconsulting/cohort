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
