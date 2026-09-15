"""`cohort dashboard` — the loopback web lens: state aggregation, token guard, actions."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from cohort.dashboard import (
    DashboardServer,
    claude_memory_summary,
    collect_state,
    read_artifact,
)
from cohort.project import list_projects

COHORT_SRC = Path(__file__).resolve().parents[1]


def run_cli(*args, home, cwd=None):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env.pop("COHORT_SOURCE", None)
    # timeout: a CLI that unexpectedly serves (e.g. a port "collision" that
    # binds anyway) must fail the test, never hang the suite.
    return subprocess.run(
        [sys.executable, "-m", "cohort", *args], cwd=cwd, capture_output=True, text=True,
        env=env, timeout=120,
    )


def tree_hash(root: Path) -> str:
    if not root.exists():
        return "MISSING"
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file() and not p.is_symlink():
            h.update(p.read_bytes())
        elif p.is_symlink():
            h.update(os.readlink(p).encode())
    return h.hexdigest()


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
def source(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    shutil.copytree(COHORT_SRC / "canonical", src / "canonical")
    shutil.copytree(COHORT_SRC / "adapters", src / "adapters")
    # Pin resolution for the in-process aggregator: no git repo → the update
    # check degrades to unavailable and never reaches the network in tests.
    monkeypatch.setenv("COHORT_SOURCE", str(src))
    monkeypatch.setenv("COHORT_ADAPTERS_DIR", str(src / "adapters"))
    return src


@pytest.fixture
def home(tmp_path, source):
    h = tmp_path / "home"
    h.mkdir()
    run_cli("recompile", "--ide", "claude", "--source", str(source), home=h)
    return h


def inited_repo(tmp_path, source, home, name="repo") -> Path:
    repo = make_git_repo(tmp_path / name)
    run_cli("init", "--source", str(source), home=home, cwd=repo)
    return repo


def add_specialist(repo, home, name="data-modeler"):
    return run_cli(
        "add-specialist", "--name", name, "--display-name", name.title(),
        "--department", "Data", "--description", "x.", home=home, cwd=repo,
    )


@pytest.fixture
def server(home, tmp_path, source):
    repo = inited_repo(tmp_path, source, home)
    srv = DashboardServer(home, repo, 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, repo
    srv.shutdown()
    srv.server_close()


def request(srv, method, path, token=None, body=None, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    headers = {"Host": host or f"127.0.0.1:{srv.server_address[1]}"}
    if token is not None:
        headers["X-Cohort-Token"] = token
    payload = json.dumps(body) if body is not None else None
    conn.request(method, path, body=payload, headers=headers)
    res = conn.getresponse()
    data = res.read()
    conn.close()
    return res.status, data


# === state aggregation =======================================================


def test_read_artifact_finds_a_specialist_in_the_focused_project(home, tmp_path, source):
    """A project agent lives in the *switched-to* repo, not the dashboard's launch
    directory. Regression: the detail pane rendered the card from the focused
    project's inventory but then reported "no agent '<name>' in project", because
    the artifact lookup resolved against cwd instead of the focused project."""
    launch_repo = inited_repo(tmp_path, source, home, name="launch")
    other_repo = inited_repo(tmp_path, source, home, name="other")
    add_specialist(other_repo, home, name="atlas-agent-schema")

    index = next(
        e["index"] for e in list_projects(home, include_private=False)
        if Path(e["path"]) == other_repo
    )

    # Focused on the other project → the specialist resolves, body included.
    art = read_artifact(home, launch_repo, "project", "agent", "atlas-agent-schema", index)
    assert art["name"] == "atlas-agent-schema"
    assert art["layer"] == "project"

    # Without the focus it is (correctly) not in the launch repo — the old bug.
    with pytest.raises(Exception, match="no agent"):
        read_artifact(home, launch_repo, "project", "agent", "atlas-agent-schema")


def test_collect_state_merges_global_and_project(home, tmp_path, source):
    repo = inited_repo(tmp_path, source, home)
    add_specialist(repo, home)
    run_cli("feedback", "--rating", "down", "--agent", "data-modeler", home=home, cwd=repo)
    state = collect_state(home, repo)
    assert state["version"]
    assert state["global"]["roster"]["count"] > 0
    assert state["global"]["update"] == {"available": False, "upstream": ""} or \
        state["global"]["update"]["available"] is False
    assert "claude" in state["global"]["parity"]
    assert state["project"]["specialists"] == ["data-modeler"]
    assert state["project"]["signals"]["feedback_total"] == 1
    assert state["project"]["feedback"][0]["rating"] == "down"


def _plant_claude_memory(home, repo, names):
    """Create a fake Claude Code agent-memory store for ``repo`` under ``home``,
    matching Claude Code's ``<slug>/memory/`` dir naming (non-alnum → '-')."""
    import re

    slug = re.sub(r"[^a-zA-Z0-9]", "-", str(repo))
    memdir = home / ".claude" / "projects" / slug / "memory"
    memdir.mkdir(parents=True)
    for n in names:
        (memdir / n).write_text("x", encoding="utf-8")
    return memdir


def test_claude_memory_summary_counts_entries_excluding_the_index(home, tmp_path, source):
    repo = inited_repo(tmp_path, source, home)
    _plant_claude_memory(home, repo, ["MEMORY.md", "decision-one.md", "decision-two.md"])
    summary = claude_memory_summary(home, str(repo))
    assert summary["count"] == 2  # MEMORY.md (the index) is excluded from the count
    assert summary["updated"] is not None  # freshness derived from the newest write


def test_claude_memory_summary_absent_store_is_zero(home, tmp_path, source):
    repo = inited_repo(tmp_path, source, home)
    assert claude_memory_summary(home, str(repo)) == {"count": 0, "updated": None}


def test_collect_state_attaches_claude_memory_to_each_project(home, tmp_path, source):
    repo = inited_repo(tmp_path, source, home)
    _plant_claude_memory(home, repo, ["MEMORY.md", "note.md"])
    state = collect_state(home, repo)
    proj = next(p for p in state["projects"] if p["path"] == str(repo))
    assert proj["claude_memory"]["count"] == 1


def test_collect_state_outside_project_has_no_project_key(home, tmp_path):
    plain = make_git_repo(tmp_path / "plain")
    state = collect_state(home, plain)
    assert "project" not in state
    assert state["global"]["roster"]["count"] > 0


def test_collect_state_is_read_only(home, tmp_path, source):
    repo = inited_repo(tmp_path, source, home)
    add_specialist(repo, home)
    before_repo = tree_hash(repo / ".cohort")
    before_home = tree_hash(home / ".cohort")
    collect_state(home, repo)
    assert tree_hash(repo / ".cohort") == before_repo
    assert tree_hash(home / ".cohort") == before_home


def test_collect_state_surfaces_proposals(home, tmp_path, source):
    repo = inited_repo(tmp_path, source, home)
    run_cli("propose-improvement", home=home, cwd=repo)
    state = collect_state(home, repo)
    assert len(state["project"]["proposals"]) == 1
    prop = state["project"]["proposals"][0]
    assert prop["kind"] == "improvement"
    assert prop["submitted_at"] is None


# === cross-project activity & scorecards (#145) ==============================


def test_collect_state_activity_and_scorecards_are_empty_with_no_projects(home, tmp_path):
    plain = make_git_repo(tmp_path / "plain")
    state = collect_state(home, plain)
    assert state["activity"] == []
    assert state["scorecards"] == []


def test_collect_state_activity_and_scorecards_present_with_no_signal(home, tmp_path, source):
    # A registered project with no sessions/feedback yet still yields the
    # empty-state shape, not an error.
    repo = inited_repo(tmp_path, source, home)
    state = collect_state(home, repo)
    assert state["activity"] == []
    assert state["scorecards"] == []


def test_collect_state_activity_aggregates_sessions_across_projects(home, tmp_path, source):
    repo_a = inited_repo(tmp_path, source, home, name="repo-a")
    repo_b = inited_repo(tmp_path, source, home, name="repo-b")
    run_cli("snapshot", home=home, cwd=repo_a)
    run_cli("snapshot", home=home, cwd=repo_b)
    # collect_state's cross-project views are office-wide, independent of the
    # focused project (here, neither repo_a nor repo_b — a third, plain cwd).
    plain = make_git_repo(tmp_path / "plain")
    state = collect_state(home, plain)
    assert len(state["activity"]) == 2
    projects_seen = {entry["project"] for entry in state["activity"]}
    assert projects_seen == {"repo-a", "repo-b"}
    # newest-first
    timestamps = [entry["timestamp"] for entry in state["activity"]]
    assert timestamps == sorted(timestamps, reverse=True)


def test_collect_state_scorecards_aggregate_feedback_across_projects(home, tmp_path, source):
    repo_a = inited_repo(tmp_path, source, home, name="repo-a")
    repo_b = inited_repo(tmp_path, source, home, name="repo-b")
    run_cli("feedback", "--rating", "up", "--agent", "counsel", home=home, cwd=repo_a)
    run_cli("feedback", "--rating", "up", "--agent", "counsel", home=home, cwd=repo_b)
    run_cli("feedback", "--rating", "down", "--agent", "counsel", home=home, cwd=repo_b)
    plain = make_git_repo(tmp_path / "plain")
    state = collect_state(home, plain)
    assert len(state["scorecards"]) == 1
    card = state["scorecards"][0]
    assert card["agent"] == "counsel"
    assert card["up"] == 2
    assert card["down"] == 1
    assert card["net"] == 1


def test_cross_project_views_name_the_projects_they_skipped(home, tmp_path, source, monkeypatch):
    """#270: a project whose store is unreadable is dropped (never fatal, #226) — but a
    dropped project must be visible, or office-wide totals silently undercount."""
    from cohort import dashboard
    from cohort.dashboard import cross_project_activity, cross_project_scorecards

    inited_repo(tmp_path, source, home, name="repo-a")
    repo_b = inited_repo(tmp_path, source, home, name="repo-b")
    run_cli("feedback", "--rating", "up", "--agent", "counsel", home=home, cwd=repo_b)

    def unreadable(paths, **_kwargs):  # the loaders also take limit= (#295)
        if paths.cohort_home == repo_b / ".cohort":
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad feedback file")
        return []

    monkeypatch.setattr(dashboard, "load_feedback_entries", unreadable)
    monkeypatch.setattr(dashboard, "load_session_entries", unreadable)
    projects = list_projects(home, include_private=False)

    skipped: list[str] = []
    assert cross_project_scorecards(home, projects, skipped=skipped) == []
    assert skipped == ["repo-b"]
    skipped = []
    assert cross_project_activity(home, projects, skipped=skipped) == []
    assert skipped == ["repo-b"]


def test_collect_state_carries_the_skipped_projects(home, tmp_path, source, monkeypatch):
    from cohort import dashboard

    inited_repo(tmp_path, source, home, name="repo-a")
    repo_b = inited_repo(tmp_path, source, home, name="repo-b")

    def unreadable(paths):
        if paths.cohort_home == repo_b / ".cohort":
            raise OSError("permission denied")
        return []

    monkeypatch.setattr(dashboard, "load_feedback_entries", unreadable)
    plain = make_git_repo(tmp_path / "plain")
    state = collect_state(home, plain)
    assert state["skipped"] == ["repo-b"]

    monkeypatch.setattr(dashboard, "load_feedback_entries", lambda paths: [])
    assert collect_state(home, plain)["skipped"] == []


# === server: guard rails =====================================================


def test_page_serves_without_the_token(server):
    """Any loopback client can GET / (another uid, a `--share-net` doer jail), so
    the served page must not carry the per-launch token (#293). It travels in
    the URL fragment instead, which the browser never sends to the server."""
    srv, _ = server
    code, data = request(srv, "GET", "/")
    assert code == 200
    page = data.decode("utf-8")
    assert srv.token not in page
    assert "cohort-token" not in page  # no meta-tag carrier left to scrape
    assert "__COHORT_TOKEN__" not in page


def test_served_script_carries_no_token_and_reads_the_fragment(server):
    srv, _ = server
    code, data = request(srv, "GET", "/dashboard.js")
    assert code == 200
    script = data.decode("utf-8")
    assert srv.token not in script
    assert "location.hash" in script
    assert 'meta[name="cohort-token"]' not in script
    # The fragment is scrubbed only after it is parked for reload: the
    # replaceState call sits inside the same try as setItem, after it, so a
    # blocked sessionStorage leaves the fragment in the address bar instead of
    # stranding a reload with no token anywhere.
    park = script.index("sessionStorage.setItem(")
    scrub = script.index("history.replaceState(")
    assert park < scrub < script.index("catch", park)
    opened = script.rindex("try", 0, park)
    assert script[opened:park].split() == ["try", "{"]  # nothing between the try and the park


def test_url_carries_the_token_in_the_fragment(server):
    srv, _ = server
    assert srv.url == f"http://127.0.0.1:{srv.server_address[1]}/#{srv.token}"


def test_bare_page_fetch_does_not_unlock_the_api(server):
    """The r5 probe: scrape /, then drive /api with whatever was found."""
    srv, _ = server
    code, data = request(srv, "GET", "/")
    assert code == 200
    assert srv.token not in data.decode("utf-8")
    assert request(srv, "GET", "/api/state")[0] == 401
    code, _ = request(srv, "POST", "/api/action",
                      body={"action": "add-hook",
                            "args": {"name": "x", "event": "session_start", "action_cmd": "id"}})
    assert code == 401


def test_do_dashboard_opens_the_browser_on_the_fragment_url(home, tmp_path, source, monkeypatch):
    import webbrowser

    from cohort.dashboard import do_dashboard

    repo = inited_repo(tmp_path, source, home)
    opened: list[str] = []
    done = threading.Event()

    def fake_open(url: str) -> bool:
        opened.append(url)
        done.set()
        return True

    monkeypatch.setattr(webbrowser, "open", fake_open)
    srv = do_dashboard(home, repo, 0, open_browser=True)
    try:
        assert done.wait(timeout=10)
    finally:
        srv.server_close()
    assert opened == [srv.url]
    assert opened[0].endswith("/#" + srv.token)


def test_cli_prints_the_fragment_url(home, tmp_path, source):
    """The printed URL is the only place the token is handed out."""
    import re

    repo = inited_repo(tmp_path, source, home)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env.pop("COHORT_SOURCE", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "cohort", "dashboard", "--no-open", "--port", "0"],
        cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    first_line: list[str] = []
    reader = threading.Thread(target=lambda: first_line.append(proc.stdout.readline()), daemon=True)
    reader.start()
    reader.join(timeout=60)
    proc.terminate()
    proc.wait(timeout=30)
    assert first_line, "the CLI printed nothing before the timeout"
    assert re.fullmatch(
        r"cohort dashboard: http://127\.0\.0\.1:\d+/#[A-Za-z0-9_-]{32,} \(Ctrl-C to stop\)\n",
        first_line[0],
    ), first_line[0]


def test_state_requires_token(server):
    srv, _ = server
    assert request(srv, "GET", "/api/state")[0] == 401
    assert request(srv, "GET", "/api/state", token="wrong")[0] == 401
    code, data = request(srv, "GET", "/api/state", token=srv.token)
    assert code == 200
    assert "global" in json.loads(data)


def test_non_loopback_host_is_rejected(server):
    srv, _ = server
    code, _ = request(srv, "GET", "/api/state", token=srv.token, host="evil.example.com")
    assert code == 403
    code, _ = request(srv, "GET", "/", host="evil.example.com")
    assert code == 403  # DNS-rebinding cannot read the page (and its token)


def test_unknown_paths_404(server):
    srv, _ = server
    assert request(srv, "GET", "/api/other", token=srv.token)[0] == 404
    assert request(srv, "POST", "/api/state", token=srv.token, body={})[0] == 404


def test_server_binds_loopback_only(server):
    srv, _ = server
    assert srv.server_address[0] == "127.0.0.1"


# === server: actions =========================================================


def test_action_feedback_writes_entry(server):
    srv, repo = server
    code, data = request(srv, "POST", "/api/action", token=srv.token,
                         body={"action": "feedback",
                               "args": {"rating": "up", "agent": "counsel", "note": "solid"}})
    assert code == 200
    report = json.loads(data)
    assert report["action"] == "feedback"
    assert (repo / ".cohort" / "feedback" / report["file"]).exists()


def test_action_requires_token(server):
    srv, repo = server
    code, _ = request(srv, "POST", "/api/action",
                      body={"action": "feedback", "args": {"rating": "up"}})
    assert code == 401
    assert not (repo / ".cohort" / "feedback").exists()  # nothing written


def test_action_remove_specialist_prunes(server):
    srv, repo = server
    home = srv.home
    add_specialist(repo, home)
    assert (repo / ".cohort" / "canonical" / "agents" / "data-modeler.md").exists()
    code, _ = request(srv, "POST", "/api/action", token=srv.token,
                      body={"action": "remove-specialist", "args": {"name": "data-modeler"}})
    assert code == 200
    assert not (repo / ".cohort" / "canonical" / "agents" / "data-modeler.md").exists()
    assert not (repo / ".claude" / "agents" / "data-modeler.md").is_symlink()


def test_action_unknown_or_invalid_is_400(server):
    srv, _ = server
    code, data = request(srv, "POST", "/api/action", token=srv.token,
                         body={"action": "uninstall", "args": {}})
    assert code == 400
    assert "unknown action" in json.loads(data)["error"]
    code, _ = request(srv, "POST", "/api/action", token=srv.token,
                      body={"action": "feedback", "args": {"rating": "sideways"}})
    assert code == 400
    code, _ = request(srv, "POST", "/api/action", token=srv.token,
                      body={"action": "feedback", "args": "not-an-object"})
    assert code == 400


def test_action_snapshot_and_propose(server):
    srv, repo = server
    code, _ = request(srv, "POST", "/api/action", token=srv.token,
                      body={"action": "snapshot", "args": {}})
    assert code == 200
    assert list((repo / ".cohort" / "sessions").glob("*.md"))
    code, data = request(srv, "POST", "/api/action", token=srv.token,
                         body={"action": "propose-improvement", "args": {}})
    assert code == 200
    report = json.loads(data)
    assert (repo / ".cohort" / "proposals" / report["file"]).exists()


# === CLI surface =============================================================


def test_dashboard_port_collision_errors(home, tmp_path, source, server):
    srv, repo = server
    proc = run_cli("dashboard", "--port", str(srv.server_address[1]), "--no-open",
                   home=home, cwd=repo)
    assert proc.returncode == 1
    assert "--port" in proc.stderr


def test_negative_content_length_does_not_bypass_cap(server):
    srv, _ = server
    import socket
    s = socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=5)
    s.sendall(
        f"POST /api/action HTTP/1.1\r\nHost: 127.0.0.1:{srv.server_address[1]}\r\n"
        f"X-Cohort-Token: {srv.token}\r\nContent-Type: application/json\r\n"
        f"Content-Length: -1\r\n\r\n".encode()
    )
    s.settimeout(5)
    # A clamped length reads 0 bytes → empty body → 400, not a hang.
    data = s.recv(256).decode("latin-1")
    s.close()
    assert "400" in data.split("\r\n", 1)[0]


def test_action_snapshot_outside_project_is_400(home, tmp_path, source):
    # do_snapshot *returns* an error field rather than raising; the API must
    # surface it as a refusal, not a 200 "done".
    plain = make_git_repo(tmp_path / "plain")
    srv = DashboardServer(home, plain, 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        code, data = request(srv, "POST", "/api/action", token=srv.token,
                             body={"action": "snapshot", "args": {}})
        assert code == 400
        assert "not a Cohort project" in json.loads(data)["error"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_update_cache_does_not_block_get(home, tmp_path, source):
    # A stale/first get returns immediately with the placeholder and refreshes
    # off-thread; it must never run the git fetch inline.
    from cohort.dashboard import _UpdateCache
    cache = _UpdateCache()
    repo = inited_repo(tmp_path, source, home)
    val = cache.get(repo, home)  # first call: placeholder, background refresh kicked
    assert val == {"available": False, "upstream": ""}


# === expanded action surface (dashboard v2) ==================================

from cohort.dashboard import ActionError, run_action  # noqa: E402


def test_state_includes_full_inventory(home, tmp_path, source):
    # the inventory recognizes every kind across layers, not just agents
    state = collect_state(home, tmp_path)
    items = state["inventory"]
    assert items, "inventory must not be empty"
    for it in items:
        assert {"name", "kind", "layer", "description", "active"} <= set(it)
    kinds = {it["kind"] for it in items}
    assert "agent" in kinds and "command" in kinds and "hook" in kinds  # more than agents
    assert all(it["layer"] == "office" for it in items)  # only office populated in the fixture
    assert all(it["active"] for it in items)  # fixture placed the full roster


# === #226: failure isolation + per-poll memoization ==========================


def test_state_survives_a_corrupt_session_file_in_another_project(server, home, tmp_path, source):
    """A non-UTF-8 (or otherwise unreadable) .md in ANY OTHER project must not 500
    the office-wide cross-project scan — the bad project is skipped and logged,
    the focused project and every healthy one is still served."""
    srv, focused_repo = server  # the fixture's inited + registered repo ("repo")
    other_repo = inited_repo(tmp_path, source, home, name="repo-b")
    run_cli("snapshot", home=home, cwd=focused_repo)
    run_cli("snapshot", home=home, cwd=other_repo)
    # Poison the *other* project's session store with invalid UTF-8 bytes.
    bad = other_repo / ".cohort" / "sessions" / "corrupt.md"
    bad.write_bytes(b"\xff\xfe not valid utf-8 \x80\x81\x00")
    code, data = request(srv, "GET", "/api/state", token=srv.token)
    assert code == 200  # one bad file no longer 500s the dashboard
    state = json.loads(data)
    assert "activity" in state  # full state served, not the degraded backstop
    seen = {entry["project"] for entry in state["activity"]}
    assert "repo" in seen  # the healthy focused project survives
    assert "repo-b" not in seen  # the corrupt one is skipped, not fatal


def test_aggregate_scans_are_memoized_within_ttl(home, tmp_path, source, monkeypatch):
    """A second poll inside the TTL reuses the first poll's parity + cross-project
    scans instead of recomputing them (call-count spy)."""
    from cohort import dashboard

    repo = inited_repo(tmp_path, source, home)
    calls = {"parity": 0, "activity": 0, "scorecards": 0, "list_projects": 0}
    for name, key in (
        ("check_parity", "parity"), ("cross_project_activity", "activity"),
        ("cross_project_scorecards", "scorecards"), ("list_projects", "list_projects"),
    ):
        real = getattr(dashboard, name)

        def make_spy(real, key):
            def spy(*a, **k):
                calls[key] += 1
                return real(*a, **k)
            return spy

        monkeypatch.setattr(dashboard, name, make_spy(real, key))

    cache = dashboard._AggregateCache()
    first = dashboard.collect_state(home, repo, aggregate_cache=cache)
    counts_after_first = dict(calls)
    assert counts_after_first["parity"] >= 1  # the first poll really did the work
    assert counts_after_first["activity"] == 1
    assert counts_after_first["list_projects"] == 1  # one scan shared by all consumers
    second = dashboard.collect_state(home, repo, aggregate_cache=cache)
    assert calls == counts_after_first  # nothing recomputed within the TTL
    # ...and the served values are unchanged (memo does not alter the shape).
    assert second["activity"] == first["activity"]
    assert second["scorecards"] == first["scorecards"]
    assert second["global"]["parity"] == first["global"]["parity"]


def test_mutating_action_invalidates_the_aggregate_memo(server, monkeypatch):
    """A POST action drops the memo so the next poll recomputes rather than
    serving pre-action state."""
    from cohort import dashboard

    srv, _ = server
    calls = {"n": 0}
    real = dashboard.check_parity

    def spy(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(dashboard, "check_parity", spy)

    assert request(srv, "GET", "/api/state", token=srv.token)[0] == 200
    after_first = calls["n"]
    assert after_first >= 1
    # second poll within TTL: served from the memo, no recompute
    assert request(srv, "GET", "/api/state", token=srv.token)[0] == 200
    assert calls["n"] == after_first
    # a mutating action invalidates the memo
    code, _ = request(
        srv, "POST", "/api/action", token=srv.token,
        body={"action": "feedback", "args": {"rating": "up", "agent": "counsel", "note": "n"}},
    )
    assert code == 200
    assert request(srv, "GET", "/api/state", token=srv.token)[0] == 200
    assert calls["n"] > after_first  # recomputed after invalidation


def test_set_roster_action_is_gone(server):
    # the roster editor was removed from the dashboard (no value); the CLI keeps
    # subsets via `cohort setup --agents`
    srv, _ = server
    status, data = request(srv, "POST", "/api/action", token=srv.token,
                           body={"action": "set-roster", "args": {"agents": ["counsel"]}})
    assert status == 400 and b"unknown action" in data


def test_action_add_specialist_places(server):
    srv, repo = server
    status, data = request(srv, "POST", "/api/action", token=srv.token,
                           body={"action": "add-specialist",
                                 "args": {"name": "growth-analyst", "description": "Growth metrics."}})
    assert status == 200, data
    assert (repo / ".cohort" / "canonical" / "agents" / "growth-analyst.md").exists()
    assert (repo / ".claude" / "agents" / "growth-analyst.md").exists()
    # bad slug is a clean refusal
    status, data = request(srv, "POST", "/api/action", token=srv.token,
                           body={"action": "add-specialist", "args": {"name": "Bad Name"}})
    assert status == 400, data


def test_action_init_force_restores_wiring(server, home):
    srv, repo = server
    claude_md = repo / ".claude" / "CLAUDE.md"
    claude_md.write_text("user gutted this file\n", encoding="utf-8")
    status, data = request(srv, "POST", "/api/action", token=srv.token,
                           body={"action": "init", "args": {"force": True}})
    assert status == 200, data
    state = collect_state(home, repo)
    assert state["project"]["wiring"]["state"] == "present"
    assert "user gutted this file" in claude_md.read_text(encoding="utf-8")  # user content kept


def test_action_update_degrades_gracefully_offline(server):
    # the test source is not a git clone → a clean 400 refusal, never a 500
    srv, _ = server
    status, data = request(srv, "POST", "/api/action", token=srv.token,
                           body={"action": "update", "args": {}})
    assert status == 400, data
    assert b"error" in data


def test_action_init_refused_at_home(home, tmp_path, source):
    with pytest.raises(ActionError, match="home directory"):
        run_action(home, home, "init", {})


def test_action_wrong_token_is_401(server):
    srv, _ = server
    status, _ = request(srv, "POST", "/api/action", token="not-the-token",
                        body={"action": "snapshot", "args": {}})
    assert status == 401


def test_action_recompile_preserves_copy_mode(tmp_path, source):
    # a real --copy install: the dashboard recompile must honor the manifest's
    # recorded mode, never silently converting copies to symlinks
    home = tmp_path / "copyhome"
    home.mkdir()
    run_cli("recompile", "--ide", "claude", "--copy", "--source", str(source), home=home)
    placed = home / ".claude" / "agents" / "counsel.md"
    assert placed.exists() and not placed.is_symlink()
    report = run_action(home, tmp_path, "recompile", {})
    assert report["action"] == "recompile"
    assert placed.exists()
    assert not placed.is_symlink()  # still a copy after the dashboard recompile


# === dashboard authoring + edit (increment 3) ================================


def test_action_add_skill_authors_my_office(server, home):
    srv, _ = server
    status, data = request(srv, "POST", "/api/action", token=srv.token,
                           body={"action": "add-skill",
                                 "args": {"name": "weekly-review", "description": "Sum up the week."}})
    assert status == 200, data
    assert (home / ".cohort" / "my" / "canonical" / "skills" / "weekly-review.md").exists()
    assert (home / ".claude" / "skills" / "weekly-review" / "SKILL.md").exists()


def test_action_add_hook_and_command(server, home):
    srv, _ = server
    for action, args, sub, name in [
        ("add-hook", {"name": "note", "description": "n.", "event": "session_start",
                      "action_cmd": "cohort status"}, "hooks", "note"),
        ("add-command", {"name": "standup", "description": "Daily standup."}, "commands", "standup"),
    ]:
        status, data = request(srv, "POST", "/api/action", token=srv.token,
                               body={"action": action, "args": args})
        assert status == 200, data
        assert (home / ".cohort" / "my" / "canonical" / sub / (name + ".md")).exists()


def test_action_edit_updates_my_artifact(server, home):
    srv, _ = server
    request(srv, "POST", "/api/action", token=srv.token,
            body={"action": "add-skill", "args": {"name": "s", "description": "Old."}})
    status, data = request(srv, "POST", "/api/action", token=srv.token,
                           body={"action": "edit", "args": {"kind": "skill", "name": "s",
                                 "body": "New body here.", "description": "New."}})
    assert status == 200, data
    placed = (home / ".claude" / "skills" / "s" / "SKILL.md").read_text(encoding="utf-8")
    assert "New body here." in placed and "New." in placed


def test_artifact_endpoint_returns_body(server, home):
    srv, _ = server
    request(srv, "POST", "/api/action", token=srv.token,
            body={"action": "add-skill", "args": {"name": "s", "description": "D.",
                  "body": "The skill body."}})
    status, data = request(srv, "GET", "/api/artifact?layer=my&kind=skill&name=s", token=srv.token)
    assert status == 200, data
    art = json.loads(data)
    assert art["description"] == "D." and "The skill body." in art["body"]


def test_artifact_endpoint_requires_token(server):
    srv, _ = server
    status, _ = request(srv, "GET", "/api/artifact?layer=my&kind=skill&name=s")
    assert status == 401


def test_action_add_skill_to_office_writes_clone(server, home, source):
    srv, _ = server
    status, data = request(srv, "POST", "/api/action", token=srv.token,
                           body={"action": "add-skill",
                                 "args": {"name": "shared-skill", "description": "x.", "to": "office"}})
    assert status == 200, data
    assert (source / "canonical" / "skills" / "shared-skill.md").exists()


def test_action_add_memory_creates_my_office_memory(server, home):
    # user-level memory: authored in my office and compiled into the CLAUDE.md corpus
    srv, _ = server
    status, data = request(srv, "POST", "/api/action", token=srv.token,
                           body={"action": "add-memory", "args": {
                               "name": "team-norms", "description": "How we work.",
                               "priority": "high", "body": "MEMORY-MARKER-XYZ team norms."}})
    assert status == 200, data
    src = home / ".cohort" / "my" / "canonical" / "memories" / "team-norms.md"
    assert src.exists()
    body = src.read_text(encoding="utf-8")
    assert "kind: memory" in body and "scope: global" in body
    corpus = home / ".claude" / "cohort" / "CLAUDE.cohort.md"
    assert corpus.exists() and "MEMORY-MARKER-XYZ" in corpus.read_text(encoding="utf-8")


def test_action_create_project_skill_places(server, home):
    # project-level Create (Part A) via the dashboard action: a skill at project scope
    srv, repo = server
    status, data = request(srv, "POST", "/api/action", token=srv.token,
                           body={"action": "create-project", "args": {
                               "kind": "skill", "name": "repo-lint", "description": "Repo lint rules."}})
    assert status == 200, data
    assert (repo / ".cohort" / "canonical" / "skills" / "repo-lint.md").exists()
    assert (repo / ".claude" / "skills" / "repo-lint" / "SKILL.md").exists()


# === #295 item 3 / #299 item 5: cache tuning, the invalidate fence, limits ===


def test_aggregate_ttl_is_at_least_the_ui_poll_interval():
    """A TTL below the poll means the memo never hits in single-tab steady state —
    every poll pays the cold scan (#295). Pinned against the poll the UI actually
    uses, read out of dashboard.js rather than restated here."""
    import re

    from cohort import dashboard

    js = (Path(dashboard.__file__).parent / "dashboard.js").read_text(encoding="utf-8")
    match = re.search(r"setInterval\(\(\) => \{ if \(!PENDING\) refresh\(\); \}, (\d+)\)", js)
    assert match, "dashboard.js poll interval not found — keep this pin in step with the UI"
    poll_seconds = int(match.group(1)) / 1000.0
    assert dashboard._AGGREGATE_TTL_SECONDS >= poll_seconds
    assert dashboard._AGGREGATE_TTL_SECONDS >= 30.0


def test_update_cache_invalidate_fences_an_inflight_refresh(home, tmp_path, source, monkeypatch):
    """A refresh that started BEFORE ``invalidate()`` carries a pre-action answer; it
    must not be stamped fresh for the whole TTL afterwards (#299 item 5)."""
    from cohort import dashboard

    started, release = threading.Event(), threading.Event()

    def slow_update_status(src, hm):
        started.set()
        release.wait(timeout=10)
        return {"available": False, "upstream": "STALE-PRE-UPDATE"}

    monkeypatch.setattr(dashboard, "update_status", slow_update_status)
    cache = dashboard._UpdateCache()
    repo = inited_repo(tmp_path, source, home)

    assert cache.get(repo, home) == {"available": False, "upstream": ""}  # kicks the refresh
    assert started.wait(timeout=10)
    cache.invalidate()  # the user ran Update while the fetch was in flight
    release.set()
    for _ in range(100):  # let the refresh thread finish
        if not cache._refreshing:
            break
        time.sleep(0.02)
    assert cache._value is None, "a pre-invalidate result must not be re-stamped fresh"

    # ...and the fence must not starve later refreshes.
    monkeypatch.setattr(dashboard, "update_status",
                        lambda src, hm: {"available": True, "upstream": "FRESH"})
    cache.get(repo, home)  # kicks a new refresh
    for _ in range(100):
        if cache._value is not None:
            break
        time.sleep(0.02)
    assert cache._value == {"available": True, "upstream": "FRESH"}


def test_cross_project_activity_parses_only_the_newest_records_per_project(
    home, tmp_path, source, monkeypatch
):
    """The feed shows ``limit`` entries, so it must open at most ``limit`` files per
    project — filenames are timestamp-prefixed, so the newest by name are the newest
    by clock (#295 item 3)."""
    from cohort import dashboard, improve

    repo = make_git_repo(tmp_path / "many")
    sessions = repo / ".cohort" / "sessions"
    sessions.mkdir(parents=True)
    for i in range(12):
        (sessions / f"202607{i + 10:02d}T100000Z-{i:04x}-auto.md").write_text(
            f"---\ntimestamp: '2026-07-{i + 10}T10:00:00+00:00'\nauthor: dev\n"
            f"branch: b{i}\n---\nbody\n",
            encoding="utf-8",
        )
    parses = {"n": 0}
    real = improve.load_artifact

    def counting(path):
        parses["n"] += 1
        return real(path)

    monkeypatch.setattr(improve, "load_artifact", counting)
    projects = [{"name": "many", "path": str(repo)}]
    entries = dashboard.cross_project_activity(home, projects, limit=3)
    assert parses["n"] == 3  # not 12
    assert [e["branch"] for e in entries] == ["b11", "b10", "b9"]  # newest first


def test_cross_project_activity_survives_an_unquoted_yaml_timestamp(home, tmp_path):
    """An unquoted timestamp parses as a ``datetime``: it used to make the merge sort
    raise ``TypeError`` (degrading the whole /api/state) and was not JSON-safe (#299)."""
    from cohort import dashboard

    repo = tmp_path / "hand-edited"
    sessions = repo / ".cohort" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "20260701T090000Z-a-auto.md").write_text(
        "---\ntimestamp: 2026-07-01T09:00:00Z\nauthor: dev\nbranch: hand\n---\nbody\n",
        encoding="utf-8",
    )
    (sessions / "20260701T100000Z-b-auto.md").write_text(
        "---\ntimestamp: '2026-07-01T10:00:00+00:00'\nauthor: dev\nbranch: tool\n---\nbody\n",
        encoding="utf-8",
    )
    entries = dashboard.cross_project_activity(home, [{"name": "p", "path": str(repo)}])
    assert [e["branch"] for e in entries] == ["tool", "hand"]  # sorted, newest first
    assert all(isinstance(e["timestamp"], str) for e in entries)
    json.dumps(entries)  # a datetime would raise here


def test_compute_aggregates_parses_canonical_once_for_every_ide(home, source, monkeypatch):
    """``check_parity`` used to re-parse all canonical per IDE; the IR load is hoisted
    so N IDEs cost one pass, and the per-IDE answers are unchanged (#295 item 3)."""
    from cohort import parity
    from cohort.compile import RENDERERS
    from cohort.dashboard import _compute_aggregates
    from cohort.schema import discover_artifacts

    ides = [i for i in ("claude", "codex", "cursor", "copilot") if i in RENDERERS]
    assert len(ides) > 1, "the hoist is only observable with more than one IDE"
    artifact_count = len(list(discover_artifacts(source / "canonical")))

    parses = {"n": 0}
    real = parity.load_artifact

    def counting(path):
        parses["n"] += 1
        return real(path)

    monkeypatch.setattr(parity, "load_artifact", counting)
    aggregates = _compute_aggregates(home, source, ides)
    assert parses["n"] == artifact_count  # one pass, not len(ides) passes

    for ide in ides:
        assert aggregates["parity"][ide] == parity.check_parity(source, ide, RENDERERS).to_dict()
