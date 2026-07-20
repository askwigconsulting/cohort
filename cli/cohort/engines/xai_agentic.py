"""xAI (Grok) agentic transport — a bounded, read-only tool-calling loop.

Where the one-shot :mod:`cohort.engines.xai` transport has Claude package a context
bundle and Grok answer once, this transport lets Grok EXPLORE a repository itself
through four read-only tools. That closes the "the callers weren't in my bundle, so I
can't prove reachability" blind spot the RFC 0004 review exposed: the model's reasoning
was good, the transport was the bottleneck.

Safety model (non-negotiable — this is what keeps an external engine advisory, not
trusted):

* **Read-only only.** ``list_dir``, ``read_file``, ``grep``, ``find_files`` — nothing
  else. No shell, no write tool. Producing changes is :mod:`cohort.engines.patch_proposal`'s
  job, behind its own gates and the human PR review; a better reviewer does not need to
  write.
* **The toolbox is the egress gate.** Every path a tool touches is run through the same
  fail-closed :mod:`cohort.engines.gates` used for patch egress: a path that escapes the
  repo root, resolves through a symlink, or classifies sensitive (``.env``, ``.git``
  internals, auth/crypto/secret material) — or a file whose *content* trips the secret
  scanner — returns a **refusal string as the tool result**, never the bytes. The gate
  moves from one up-front bundle scan to a per-read check, so nothing the gate would have
  blocked can leave the machine.
* **The transcript is inspectable.** Every tool call and its (possibly refused) result is
  recorded. Exploration you cannot audit is not advisory, it is trusted — the transcript
  is what makes "untrusted" real in practice.
* **Bounded.** A maximum tool-iteration count and a cumulative tool-output byte cap bound
  cost and stop a runaway loop; the API ``max_tokens`` bounds each response.

Reuses the one-shot transport's key handling and error taxonomy — the key is read on
demand and never logged, printed, or embedded in an exception message.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from cohort.engines import EngineSpec, get_engine
from cohort.engines import gates
from cohort.engines.xai import (
    EngineAuthError,
    EngineError,
    EnginePayloadError,
    EngineUnavailableError,
    _build_request,
    _resolve_api_key,
    _retry_after_seconds,
)

# Bounds. A read-only review loop that needs more than this is either stuck or being
# abused; stop rather than spend unboundedly.
_DEFAULT_MAX_ITERATIONS = 24
_DEFAULT_MAX_OUTPUT_BYTES = 400_000  # cumulative tool-result bytes fed back to the model
_MAX_READ_BYTES = 64_000  # per read_file call
_MAX_MATCHES = 200  # per grep / find_files call
_MAX_DIR_ENTRIES = 500  # per list_dir call
_TIMEOUT = 120.0

# Path-level read denylist (the content scanner is the real backstop). Any path
# segment that is an internal/credential directory, or a basename that is a known
# credential/key file, is refused before its bytes are ever read.
_SECRET_PATH_SEGMENTS = frozenset({".git", ".ssh", ".gnupg"})
_SECRET_FILE_NAMES = frozenset(
    {".netrc", ".npmrc", ".pgpass", "credentials", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
)
_SECRET_FILE_SUFFIXES = (".pem", ".key", ".pfx", ".p12", ".keystore", ".jks")


class ToolPolicyError(EngineError):
    """A tool request was refused by the read-only path policy. Not fatal to the loop —
    the refusal is returned to the model as the tool result so it can adapt."""


@dataclass
class ToolCall:
    """One recorded tool invocation and its result, for the inspectable transcript."""

    iteration: int
    name: str
    arguments: dict[str, Any]
    result: str
    refused: bool = False


@dataclass
class AgenticResult:
    """The outcome of an agentic run: the model's final text plus the full transcript."""

    text: str
    transcript: list[ToolCall] = field(default_factory=list)
    iterations: int = 0
    stopped_reason: str = "final"  # "final" | "max_iterations" | "output_cap"


# --------------------------------------------------------------------------- #
# Read-only toolbox — the egress boundary
# --------------------------------------------------------------------------- #


class ReadOnlyToolbox:
    """The four read-only tools, each rooted at and confined to ``root``.

    A tool never raises on a policy violation — it returns a human-readable refusal
    string, which the loop hands back to the model as the tool result. This keeps the
    model exploring (it can try a different path) while guaranteeing the bytes of a
    refused file never enter the conversation.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    # -- path policy (fail-closed) ------------------------------------------ #

    def _gate(self, rel: str) -> str | None:
        """Return a refusal string if ``rel`` may not be read, else ``None``.

        A read-appropriate policy — deliberately narrower than the patch-egress gate,
        which refuses a file merely *named* ``auth.py``. A reviewer must read source
        freely (that's the point), so this blocks only:

        * paths that escape the repo — absolute, ``..``, backslash, or a symlink whose
          resolved target lands outside the root;
        * ``.git`` / ``.ssh`` internals and credential *files* by path (``.env*``, key
          material, ``.netrc``/``.npmrc``/``credentials``/``id_rsa`` …).

        Everything else is allowed at the path level; the real backstop is the
        content-level :func:`gates.scan_for_secrets` in :meth:`read_file`/:meth:`grep`,
        which refuses any file whose *bytes* look like a credential, whatever its name.
        """
        if not rel or not isinstance(rel, str):
            return "refused: empty or non-string path"
        if "\\" in rel:
            return f"refused: backslash in path {rel!r} (use forward slashes)"
        parts = PurePosixPath(rel).parts
        if PurePosixPath(rel).is_absolute() or ".." in parts:
            return f"refused: {rel!r} escapes the repository root"
        segments = [p.lower() for p in parts]
        base = segments[-1] if segments else ""
        if any(seg in _SECRET_PATH_SEGMENTS for seg in segments):
            return f"refused: {rel!r} is an internal/credential directory"
        if (
            base in _SECRET_FILE_NAMES
            or base.startswith(".env")
            or base.endswith(_SECRET_FILE_SUFFIXES)
        ):
            return f"refused: {rel!r} is a credential/key file"
        resolved = (self.root / rel).resolve()
        if resolved != self.root and self.root not in resolved.parents:
            return f"refused: {rel!r} resolves outside the repository root (symlink?)"
        return None

    def _rel(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()

    # -- tools -------------------------------------------------------------- #

    def list_dir(self, path: str = ".") -> str:
        """List the entries of a directory (directories suffixed with ``/``)."""
        rel = "." if path in ("", ".", "./") else path
        if rel != ".":
            refusal = self._gate(rel)
            if refusal:
                return refusal
        target = (self.root / rel).resolve()
        if not target.is_dir():
            return f"error: {path!r} is not a directory"
        entries: list[str] = []
        for child in sorted(target.iterdir(), key=lambda p: p.name):
            name = child.name + ("/" if child.is_dir() else "")
            # Hide dotfiles the policy would refuse anyway, so the listing doesn't
            # advertise paths that can't be read; keep ordinary dot-config visible.
            if self._gate(self._rel(child)) is None or child.is_dir():
                entries.append(name)
            if len(entries) >= _MAX_DIR_ENTRIES:
                entries.append(f"... (truncated at {_MAX_DIR_ENTRIES} entries)")
                break
        return "\n".join(entries) if entries else "(empty)"

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        """Read a text file, line-numbered. Refuses sensitive paths and, after reading,
        any file whose content trips the secret scanner (so a secret in an unexpected
        file still never egresses)."""
        refusal = self._gate(path)
        if refusal:
            return refusal
        target = (self.root / path).resolve()
        if not target.is_file():
            return f"error: {path!r} is not a file"
        try:
            raw = target.read_bytes()[: _MAX_READ_BYTES + 1]
        except OSError as exc:
            return f"error: could not read {path!r}: {exc}"
        truncated = len(raw) > _MAX_READ_BYTES
        try:
            text = raw[:_MAX_READ_BYTES].decode("utf-8")
        except UnicodeDecodeError:
            return f"refused: {path!r} is not UTF-8 text (binary file)"
        # Content-level egress gate: a secret in any file, expected or not, is refused.
        labels = gates.scan_for_secrets(text)
        if labels:
            return (
                f"refused: {path!r} contains credential-shaped content "
                f"({', '.join(sorted(set(labels)))}); not returned"
            )
        lines = text.splitlines()
        start = max(1, start_line)
        end = len(lines) if end_line is None else min(end_line, len(lines))
        numbered = [f"{n:>6}\t{lines[n - 1]}" for n in range(start, end + 1)]
        out = "\n".join(numbered)
        if truncated:
            out += f"\n... (truncated at {_MAX_READ_BYTES} bytes)"
        return out or "(empty file)"

    def grep(self, pattern: str, path: str = ".", glob: str = "**/*") -> str:
        """Search for a literal substring across files under ``path`` matching ``glob``.
        Returns ``file:line: text`` matches. Files the policy refuses are skipped
        silently (their contents never appear in a match line)."""
        if not pattern:
            return "error: empty pattern"
        base_refusal = self._gate(path) if path not in ("", ".", "./") else None
        if base_refusal:
            return base_refusal
        base = (self.root / (path if path not in ("", ".", "./") else ".")).resolve()
        matches: list[str] = []
        for file in sorted(base.glob(glob)):
            if not file.is_file():
                continue
            rel = self._rel(file)
            if self._gate(rel) is not None:
                continue  # refused paths are invisible to search
            try:
                content = file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if gates.scan_for_secrets(content):
                continue  # never surface a line from a secret-bearing file
            for lineno, line in enumerate(content.splitlines(), 1):
                if pattern in line:
                    matches.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                    if len(matches) >= _MAX_MATCHES:
                        matches.append(f"... (truncated at {_MAX_MATCHES} matches)")
                        return "\n".join(matches)
        return "\n".join(matches) if matches else "(no matches)"

    def find_files(self, glob: str) -> str:
        """List repo-relative paths matching a glob (e.g. ``**/*.py``). Refused paths
        are omitted."""
        if not glob:
            return "error: empty glob"
        found: list[str] = []
        for file in sorted(self.root.glob(glob)):
            if not file.is_file():
                continue
            rel = self._rel(file)
            if self._gate(rel) is not None:
                continue
            found.append(rel)
            if len(found) >= _MAX_MATCHES:
                found.append(f"... (truncated at {_MAX_MATCHES} matches)")
                break
        return "\n".join(found) if found else "(no matches)"

    def dispatch(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Run a named tool with validated arguments. Returns (result, refused)."""
        try:
            if name == "list_dir":
                out = self.list_dir(str(arguments.get("path", ".")))
            elif name == "read_file":
                out = self.read_file(
                    str(arguments["path"]),
                    int(arguments.get("start_line", 1)),
                    (int(arguments["end_line"]) if arguments.get("end_line") is not None else None),
                )
            elif name == "grep":
                out = self.grep(
                    str(arguments["pattern"]),
                    str(arguments.get("path", ".")),
                    str(arguments.get("glob", "**/*")),
                )
            elif name == "find_files":
                out = self.find_files(str(arguments["glob"]))
            else:
                return f"error: unknown tool {name!r}", True
        except (KeyError, ValueError, TypeError) as exc:
            return f"error: bad arguments for {name!r}: {exc}", True
        return out, out.startswith("refused:")


# The tool schemas advertised to the model (OpenAI function-calling shape).
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List the entries of a directory in the repository.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Repo-relative directory path; default '.'"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file, returned line-numbered. Sensitive or secret-bearing files are refused.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Repo-relative file path"},
                    "start_line": {"type": "integer", "description": "1-based first line (default 1)"},
                    "end_line": {"type": "integer", "description": "1-based last line (default end of file)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Find a literal substring across files, returning file:line: text matches.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Literal substring to find"},
                    "path": {"type": "string", "description": "Repo-relative base dir; default '.'"},
                    "glob": {"type": "string", "description": "Glob under path; default '**/*'"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": "List repo-relative file paths matching a glob (e.g. '**/*.py').",
            "parameters": {
                "type": "object",
                "properties": {"glob": {"type": "string", "description": "Glob pattern"}},
                "required": ["glob"],
            },
        },
    },
]

_SYSTEM_PROMPT = (
    "You are a code reviewer exploring a repository through read-only tools. "
    "Use list_dir, read_file, grep, and find_files to gather the evidence you need — "
    "verify a claim in the actual code before making it. You cannot write, run shell "
    "commands, or read outside the repository; sensitive files (.env, .git internals, "
    "credentials) are refused by policy, not an oversight. When you have enough evidence, "
    "stop calling tools and give your final answer grounded in the files you read."
)


def _post_chat(spec: EngineSpec, key: str, body: dict[str, Any]) -> dict[str, Any]:
    """POST one chat/completions turn and return the parsed JSON, with one retry on a
    transient failure. Never includes the key in any raised message."""
    endpoint = spec.endpoint or "https://api.x.ai/v1/chat/completions"
    for attempt in range(2):
        try:
            with urllib.request.urlopen(
                _build_request(endpoint, key, body), timeout=_TIMEOUT
            ) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise EngineAuthError("xAI rejected the API key (401/403)") from None
            if exc.code == 429 and attempt == 0:
                import time

                time.sleep(_retry_after_seconds(exc.headers))
                continue
            if 500 <= exc.code < 600 and attempt == 0:
                continue
            raise EngineUnavailableError(f"xAI returned HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, ValueError, UnicodeDecodeError):
            if attempt == 0:
                continue
            raise EngineUnavailableError(
                "xAI request failed to reach the API after one retry"
            ) from None
    raise EngineUnavailableError("xAI request failed")


def run_agentic(
    task: str,
    *,
    root: Path,
    model: str | None = None,
    engine_name: str = "grok",
    max_iterations: int = _DEFAULT_MAX_ITERATIONS,
    max_tokens: int = 4096,
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
    transcript_path: Path | None = None,
    _poster: Callable[[EngineSpec, str, dict[str, Any]], dict[str, Any]] | None = None,
) -> AgenticResult:
    """Run a bounded, read-only agentic loop and return the model's final answer plus
    the full tool transcript.

    Args:
        task: The review/analysis instruction for the model.
        root: The repository root the toolbox is confined to.
        model: Model id to request; defaults to the engine's flagship tier.
        engine_name: Registry key for the engine (default ``"grok"``).
        max_iterations: Hard cap on tool-calling rounds.
        max_tokens: Per-response API token cap.
        max_output_bytes: Cumulative cap on tool-result bytes fed back to the model.
        transcript_path: If given, the transcript is also written there as JSONL.
        _poster: Injection seam for tests (defaults to the real HTTP poster).

    Raises:
        EngineAuthError / EngineUnavailableError / EnginePayloadError: as the one-shot
            transport, with no secret ever in the message.
    """
    spec = get_engine(engine_name)
    key = _resolve_api_key(spec)
    model = model or spec.model_tiers.get("flagship") or spec.model_tiers.get("cheap")
    poster = _poster or _post_chat
    toolbox = ReadOnlyToolbox(root)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]
    transcript: list[ToolCall] = []
    output_bytes = 0

    for iteration in range(1, max_iterations + 1):
        body = {
            "model": model,
            "messages": messages,
            "tools": TOOL_SCHEMAS,
            "tool_choice": "auto",
            "max_tokens": max_tokens,
        }
        payload = poster(spec, key, body)
        try:
            message = payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise EngineUnavailableError("xAI response missing choices[0].message") from None

        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            final = message.get("content")
            result = AgenticResult(
                text=final if isinstance(final, str) and final.strip() else "(no answer)",
                transcript=transcript,
                iterations=iteration,
                stopped_reason="final",
            )
            _maybe_write_transcript(transcript_path, transcript)
            return result

        # Record the assistant's tool-calling turn verbatim so the follow-up tool
        # messages attach to it correctly.
        messages.append(message)
        for call in tool_calls:
            fn = call.get("function", {})
            name = fn.get("name", "")
            try:
                arguments = json.loads(fn.get("arguments") or "{}")
            except (ValueError, TypeError):
                arguments = {}
            out, refused = toolbox.dispatch(name, arguments if isinstance(arguments, dict) else {})
            output_bytes += len(out.encode("utf-8"))
            transcript.append(ToolCall(iteration, name, arguments if isinstance(arguments, dict) else {}, out, refused))
            messages.append(
                {"role": "tool", "tool_call_id": call.get("id", ""), "content": out}
            )
            if output_bytes > max_output_bytes:
                _maybe_write_transcript(transcript_path, transcript)
                return AgenticResult(
                    text="(stopped: tool-output byte cap reached before a final answer)",
                    transcript=transcript,
                    iterations=iteration,
                    stopped_reason="output_cap",
                )

    _maybe_write_transcript(transcript_path, transcript)
    return AgenticResult(
        text="(stopped: reached the maximum tool-iteration budget before a final answer)",
        transcript=transcript,
        iterations=max_iterations,
        stopped_reason="max_iterations",
    )


def _maybe_write_transcript(path: Path | None, transcript: list[ToolCall]) -> None:
    """Persist the transcript as JSONL if a path was given — the audit trail that keeps
    the engine's exploration inspectable rather than trusted."""
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for call in transcript:
            fh.write(
                json.dumps(
                    {
                        "iteration": call.iteration,
                        "tool": call.name,
                        "arguments": call.arguments,
                        "refused": call.refused,
                        "result": call.result[:2000],
                    }
                )
                + "\n"
            )
