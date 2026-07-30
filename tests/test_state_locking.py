"""#4 (lost-update / gate-bypass) and #6 (first-pull trust) regressions.

The concurrent-writer tests inject a delay into each write so the load→write
window is forced open: without the lock around the read-modify-write cycle a
second writer reads the pre-write state and clobbers the first writer's update
(for the quarantine store that is a security-gate bypass, not just lost data).
With the lock the second writer blocks until the first fully commits, so both
updates survive — which is what these assert.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from cohort import project, quarantine as q


def _state(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir()
    return d


def _art(name: str, h: str) -> q.QuarantinedArtifact:
    return q.QuarantinedArtifact(kind="hook", name=name, content_hash=h, first_seen="t")


def _art_file(path: Path, *, kind: str, name: str, action: str = "cohort x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\nkind: {kind}\nscope: global\ndescription: x.\n"
        f"targets: [claude]\nevent: session_start\naction: {action}\n---\nbody\n",
        encoding="utf-8",
    )
    return path


def _run_both(fn_a, fn_b) -> list:
    errors: list = []

    def guarded(fn):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - surface a worker crash to the test
            errors.append(exc)

    threads = [threading.Thread(target=guarded, args=(f,)) for f in (fn_a, fn_b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


# --- #4: concurrent quarantine writers do not lose an update ------------------


def test_concurrent_add_pending_keeps_both_updates(tmp_path, monkeypatch):
    state = _state(tmp_path)
    real_write = q._write_pending_file

    def slow_write(state_dir, path, items):
        time.sleep(0.25)  # widen the load→write window the lock must cover
        return real_write(state_dir, path, items)

    monkeypatch.setattr(q, "_write_pending_file", slow_write)

    errors = _run_both(
        lambda: q.add_pending(state, [_art("alpha", "h1")]),
        lambda: q.add_pending(state, [_art("beta", "h2")]),
    )
    assert not errors
    names = {a.name for a in q.load_pending(state)}
    # Without the lock, one writer reads the empty store while the other is mid-write
    # and overwrites it — only one name would survive. The lock keeps both.
    assert names == {"alpha", "beta"}


def test_concurrent_add_and_approve_do_not_lose_the_add(tmp_path, monkeypatch):
    """An add racing an approve is the gate-bypass case: a fresh pull recording a
    new gated artifact must not be dropped by a concurrent approve's save."""
    state = _state(tmp_path)
    q.add_pending(state, [_art("existing", "h0")])  # something for approve to clear

    real_write = q._write_pending_file

    def slow_write(state_dir, path, items):
        time.sleep(0.25)
        return real_write(state_dir, path, items)

    monkeypatch.setattr(q, "_write_pending_file", slow_write)

    errors = _run_both(
        lambda: q.approve(state, ["existing"]),
        lambda: q.add_pending(state, [_art("newly-pulled", "h1")]),
    )
    assert not errors
    names = {a.name for a in q.load_pending(state)}
    assert "newly-pulled" in names  # the add was never clobbered by the approve


# --- #4: registry is atomic and concurrency-safe ------------------------------


def test_write_registry_is_atomic_no_tmp_left(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo-a"
    project._register_project(home, repo)
    reg = project._registry_path(home)
    assert reg.exists()
    data = json.loads(reg.read_text(encoding="utf-8"))
    assert str(repo.resolve()) in data["projects"]
    # The atomic tmp+os.replace must leave no sidecar behind.
    assert not reg.with_suffix(".json.tmp").exists()


def test_concurrent_register_project_keeps_both(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"

    real_write = project._write_registry

    def slow_write(h, projects):
        time.sleep(0.25)
        return real_write(h, projects)

    monkeypatch.setattr(project, "_write_registry", slow_write)

    errors = _run_both(
        lambda: project._register_project(home, repo_a),
        lambda: project._register_project(home, repo_b),
    )
    assert not errors
    registered = set(project._read_registry(home))
    assert {str(repo_a.resolve()), str(repo_b.resolve())} <= registered


# --- #6: the first update pull is measured against the pre-pull tree ----------


def test_seed_office_baseline_if_absent_seeds_once(tmp_path):
    state = _state(tmp_path)
    office = tmp_path / "office"
    _art_file(office / "canonical" / "hooks" / "a.md", kind="hook", name="a")
    assert q.seed_office_baseline_if_absent(state, office) is True
    # Idempotent: a second call never clobbers the established baseline.
    assert q.seed_office_baseline_if_absent(state, office) is False


def test_pre_pull_seed_quarantines_artifact_introduced_by_the_first_pull(tmp_path):
    """The #6 fix: seed from the PRE-pull tree, then the pull that introduces a new
    auto-activating artifact has it quarantined rather than folded into 'trusted'."""
    state = _state(tmp_path)
    office = tmp_path / "office"
    _art_file(office / "canonical" / "hooks" / "a.md", kind="hook", name="a")

    # cohort update seeds the baseline from the pre-pull (install-time) tree first.
    q.seed_office_baseline_if_absent(state, office)

    # The same pull then introduces a NEW auto-activating hook on the shared remote.
    _art_file(office / "canonical" / "hooks" / "evil.md", kind="hook", name="evil",
              action="cohort rce")
    added = q.record_office_delta(state, office)

    assert [a.name for a in added] == ["evil"]  # withheld, not trusted
    assert any(k[1] == "evil" for k in q.office_pending_keys(state))


def test_without_pre_pull_seed_the_first_pull_would_be_trusted(tmp_path):
    """Contrast that locks in why the pre-pull seed matters: with no baseline,
    record_office_delta's fallback trusts the current set — so 'evil' escapes."""
    state = _state(tmp_path)
    office = tmp_path / "office"
    _art_file(office / "canonical" / "hooks" / "a.md", kind="hook", name="a")
    _art_file(office / "canonical" / "hooks" / "evil.md", kind="hook", name="evil",
              action="cohort rce")
    added = q.record_office_delta(state, office)  # baseline absent → fallback trusts all
    assert added == []
    assert q.office_pending_keys(state) == set()
