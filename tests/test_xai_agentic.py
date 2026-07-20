"""Tests for the xai_agentic read-only exploration transport.

The security-critical surface is the path policy and the content-level secret gate —
both are exercised WITHOUT any network. The tool-calling loop is driven by an injected
poster so the whole loop (tool dispatch, transcript, bounds) is tested offline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cohort.engines import xai_agentic
from cohort.engines.xai_agentic import ReadOnlyToolbox, ToolCall, run_agentic
from conftest import requires_symlinks


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A small fake repo with an ordinary source file, a secret file, and a .env."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def main():\n    return 42\n", encoding="utf-8")
    (tmp_path / "src" / "auth.py").write_text("def login(user):\n    return True\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET_TOKEN=abc123def456\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (tmp_path / "config.py").write_text(
        'AWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY"\n', encoding="utf-8"
    )
    return tmp_path


# --------------------------------------------------------------------------- #
# Path policy — the egress boundary (no network)
# --------------------------------------------------------------------------- #


def test_reads_ordinary_source_including_auth_named_files(repo: Path) -> None:
    box = ReadOnlyToolbox(repo)
    # A reviewer must be able to read source, including a file named auth.py.
    out = box.read_file("src/auth.py")
    assert "def login" in out
    assert out.startswith("     1\t")  # line-numbered


def test_refuses_dotenv_by_path(repo: Path) -> None:
    out = ReadOnlyToolbox(repo).read_file(".env")
    assert out.startswith("refused:")
    assert "abc123def456" not in out  # the secret value never appears


def test_refuses_git_internals(repo: Path) -> None:
    assert ReadOnlyToolbox(repo).read_file(".git/config").startswith("refused:")


@pytest.mark.parametrize("path", ["id_rsa", "server.pem", "app.key", ".netrc"])
def test_refuses_credential_files_by_name(repo: Path, path: str) -> None:
    (repo / path).write_text("sensitive", encoding="utf-8")
    assert ReadOnlyToolbox(repo).read_file(path).startswith("refused:")


def test_refuses_content_that_trips_the_secret_scanner(repo: Path) -> None:
    # config.py is not a credential *filename*, but its bytes are secret-shaped —
    # the content gate refuses it so the key never egresses.
    out = ReadOnlyToolbox(repo).read_file("config.py")
    assert out.startswith("refused:")
    assert "wJalrXUtnFEMIK" not in out


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", "src/../../escape"])
def test_refuses_paths_that_escape_the_root(repo: Path, path: str) -> None:
    assert ReadOnlyToolbox(repo).read_file(path).startswith("refused:")


def test_refuses_backslash_paths(repo: Path) -> None:
    assert ReadOnlyToolbox(repo).read_file("src\\app.py").startswith("refused:")


@requires_symlinks
def test_refuses_symlink_escaping_the_root(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("classified", encoding="utf-8")
    (repo / "link.txt").symlink_to(outside)
    out = ReadOnlyToolbox(repo).read_file("link.txt")
    assert out.startswith("refused:")
    assert "classified" not in out


def test_grep_skips_refused_and_secret_files(repo: Path) -> None:
    box = ReadOnlyToolbox(repo)
    # Search for a token present in both the .env and an ordinary file.
    (repo / "src" / "note.py").write_text("# see SECRET_TOKEN handling\n", encoding="utf-8")
    out = box.grep("SECRET_TOKEN")
    assert "note.py" in out  # ordinary file matched
    assert ".env" not in out  # refused-by-path file never searched
    assert "abc123def456" not in out


def test_find_files_omits_refused_paths(repo: Path) -> None:
    out = ReadOnlyToolbox(repo).find_files("**/*")
    assert "src/app.py" in out
    assert ".env" not in out
    assert ".git/config" not in out


def test_list_dir_lists_entries(repo: Path) -> None:
    out = ReadOnlyToolbox(repo).list_dir("src")
    assert "app.py" in out and "auth.py" in out


# --------------------------------------------------------------------------- #
# The agentic loop — injected poster, no network
# --------------------------------------------------------------------------- #


def _assistant_toolcall(name: str, arguments: dict) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "call_1", "type": "function",
                         "function": {"name": name, "arguments": json.dumps(arguments)}}
                    ],
                }
            }
        ]
    }


def _assistant_final(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


@pytest.fixture(autouse=True)
def _dummy_key(monkeypatch):
    # The loop resolves the key before the (injected) poster runs; give it a dummy so
    # no real network/credential is needed.
    monkeypatch.setenv("GROK_API_KEY", "test-key-not-real")


def test_loop_executes_a_tool_then_returns_final_answer(repo: Path) -> None:
    calls = iter([
        _assistant_toolcall("read_file", {"path": "src/app.py"}),
        _assistant_final("The app returns 42."),
    ])

    def poster(spec, key, body):
        return next(calls)

    result = run_agentic("review src/app.py", root=repo, _poster=poster)
    assert result.text == "The app returns 42."
    assert result.stopped_reason == "final"
    assert len(result.transcript) == 1
    call = result.transcript[0]
    assert call.name == "read_file" and "return 42" in call.result
    assert not call.refused


def test_loop_records_a_refused_tool_result_in_the_transcript(repo: Path) -> None:
    calls = iter([
        _assistant_toolcall("read_file", {"path": ".env"}),
        _assistant_final("Could not read the env file."),
    ])
    result = run_agentic("try to read secrets", root=repo, _poster=lambda s, k, b: next(calls))
    assert result.transcript[0].refused is True
    assert "abc123def456" not in result.transcript[0].result


def test_loop_stops_at_max_iterations(repo: Path) -> None:
    def poster(spec, key, body):
        return _assistant_toolcall("list_dir", {"path": "."})  # never finishes

    result = run_agentic("loop forever", root=repo, _poster=poster, max_iterations=3)
    assert result.stopped_reason == "max_iterations"
    assert result.iterations == 3


def test_transcript_is_written_to_disk_when_a_path_is_given(repo: Path, tmp_path: Path) -> None:
    calls = iter([
        _assistant_toolcall("find_files", {"glob": "**/*.py"}),
        _assistant_final("done"),
    ])
    sink = tmp_path / "transcripts" / "run.jsonl"
    run_agentic("x", root=repo, _poster=lambda s, k, b: next(calls), transcript_path=sink)
    assert sink.exists()
    lines = sink.read_text(encoding="utf-8").strip().splitlines()
    record = json.loads(lines[0])
    assert record["tool"] == "find_files" and "refused" in record
