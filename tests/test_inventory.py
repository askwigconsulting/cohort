"""The inventory carries each agent's `advisory` flag so the dashboard can show a
project doer (advisory: false) distinctly from an advisor."""

from __future__ import annotations

from pathlib import Path

from cohort.inventory import inventory


def _agent(agents_dir: Path, name: str, *, advisory: bool) -> None:
    agents_dir.mkdir(parents=True, exist_ok=True)
    adv = "true" if advisory else "false"
    tools = "[read]" if advisory else "[read, edit, bash]"
    (agents_dir / f"{name}.md").write_text(
        f"---\nname: {name}\nkind: agent\nscope: project\ndescription: X.\n"
        f"targets: [claude]\ndepartment: Ops\nadvisory: {adv}\ntools: {tools}\n---\nBody.\n",
        encoding="utf-8",
    )


def test_inventory_carries_advisory_and_flags_project_doers(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    agents = repo / ".cohort" / "canonical" / "agents"
    _agent(agents, "deployer", advisory=False)  # a doer
    _agent(agents, "reviewer", advisory=True)    # an advisor

    project = {it["name"]: it for it in inventory(home, repo) if it["layer"] == "project"}
    assert project["deployer"]["advisory"] is False
    assert project["reviewer"]["advisory"] is True
