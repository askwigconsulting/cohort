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
from pathlib import Path

import pytest

from cohort.dashboard import DashboardServer, collect_state

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


# === server: guard rails =====================================================


def test_page_serves_with_token_injected(server):
    srv, _ = server
    code, data = request(srv, "GET", "/")
    assert code == 200
    page = data.decode("utf-8")
    assert "__COHORT_TOKEN__" not in page  # placeholder substituted
    assert srv.token in page


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
