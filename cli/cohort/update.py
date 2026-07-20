"""Cohort self-update substrate: detect when the local clone is behind upstream.

Advisory only — this module never pulls, merges, or recompiles (that is ``/update``,
Phase 2). The session-start hook calls :func:`do_update_check`; it MUST never raise,
never block the session, and the command wrapper exits 0 always.

We use ``git fetch`` + ``rev-list --count`` rather than ``git ls-remote`` because an
exact "N commits behind" needs the upstream objects locally (ls-remote returns only
the remote tip SHA). ``fetch`` updates only remote-tracking refs — never the working
tree, never a merge — and is run fully non-interactively with a hard timeout so it
cannot prompt for credentials or hang a session.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .gitutil import GIT_ENV
from .install_model import CohortPaths
from .source import SourceUnresolved, resolve_source

# Non-interactive git hardening lives in gitutil so update.py and improve.py share it.
_GIT_ENV = GIT_ENV
_FETCH_TIMEOUT = 8  # seconds; a flaky network must not stall session startup
_GIT_TIMEOUT = 10
_PULL_TIMEOUT = 30  # a local ff-only merge is fast, but allow headroom for a big one
_DEFAULT_BRANCH = "main"


def _git(source: Path, *args: str, timeout: int = _GIT_TIMEOUT) -> tuple[Optional[int], str]:
    """Run a git command in ``source``; return ``(returncode, stdout)`` or
    ``(None, "")`` on timeout/missing-git/any failure. Never raises."""
    try:
        # credential.helper='' and the remote-transport allowlist (bans ext::/fd::)
        # live in the shared GIT_ENV (gitutil) — so this path, which fetches from a
        # config-derived upstream, can't be steered onto a code-executing transport.
        proc = subprocess.run(
            ["git", "-C", str(source), *args],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, **_GIT_ENV},
        )
        return proc.returncode, proc.stdout.strip()
    except Exception:  # noqa: BLE001 - offline, timeout, git absent, …
        return None, ""


def _read_update_config(home: Path) -> tuple[Optional[str], Optional[str]]:
    """``(upstream_remote, upstream_branch)`` from the global ``cohort.toml``
    ``[update]`` table, or ``(None, None)``. Never raises."""
    cfg = CohortPaths(home).cohort_home / "cohort.toml"
    try:
        import tomllib  # 3.11+; absent on 3.10 → fall back to defaults

        data = tomllib.loads(cfg.read_text(encoding="utf-8"))
        upd = data.get("update", {}) if isinstance(data, dict) else {}
        return upd.get("upstream_remote"), upd.get("upstream_branch")
    except Exception:  # noqa: BLE001 - missing file, parse error, old python
        return None, None


def _config_text(home: Path) -> Optional[str]:
    """Raw global cohort.toml text, or None when the file is absent. Raises only
    on a present-but-unreadable file, so callers can distinguish 'no config'
    (fail open — clone-and-go) from 'unreadable' (fail closed)."""
    cfg = CohortPaths(home).cohort_home / "cohort.toml"
    if not cfg.exists():
        return None
    return cfg.read_text(encoding="utf-8")


def _update_table_value(text: str, key: str) -> Optional[str]:
    """The raw right-hand side of ``key`` inside the ``[update]`` table, or None.

    A minimal, stdlib-only line scan scoped to that one table — deliberately not
    ``tomllib``, which is absent on Python 3.10 (the project floor) and would make
    a security flag silently unreadable there. Enough for the simple ``key =
    value`` lines Cohort itself writes."""
    in_update = False
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("[") and s.endswith("]"):
            in_update = s == "[update]"
            continue
        if in_update and "=" in s:
            k, _, v = s.partition("=")
            if k.strip() == key:
                return v.strip()
    return None


def _update_array_text(text: str, key: str) -> Optional[str]:
    """The full right-hand side of an ``[update]`` array-valued ``key``, joined
    across physical lines when the array spans several (``key = [`` / ``"a",`` /
    ``]``). Returns None when the key is absent from the table.

    ``_update_table_value`` reads a single line, so a multi-line array would yield
    just ``[`` and parse to *zero* elements — a silent fail-open for a security
    gate like ``signed_by``. This joins the array's lines so every element is seen
    (and an unterminated array still surfaces its elements rather than vanishing).
    Comments are stripped per line; the pinned fingerprints Cohort matches never
    contain ``#``."""
    in_update = False
    collecting = False
    buf: list[str] = []
    for line in text.splitlines():
        s = line.split("#", 1)[0].strip()  # drop comments; fingerprints have no '#'
        if collecting:
            buf.append(s)
            if "]" in s:
                return " ".join(buf)
            continue
        if not s:
            continue
        if s.startswith("[") and s.endswith("]"):
            in_update = s == "[update]"
            continue
        if in_update and "=" in s:
            k, _, v = s.partition("=")
            if k.strip() == key:
                v = v.strip()
                if "[" not in v or "]" in v:  # scalar or single-line array — done
                    return v
                buf.append(v)  # array opened but not closed on this line
                collecting = True
    return " ".join(buf) if buf else None


def _truthy_toml(rhs: str) -> bool:
    """A TOML boolean-ish right-hand side read as True: bare ``true`` or a common
    truthy string. For a security opt-in, honoring a mistyped ``"true"`` fails
    safe; silently reading it as off is the dangerous direction."""
    v = rhs.split("#", 1)[0].strip().strip('"').strip("'").lower()
    return v in {"true", "1", "yes", "on"}


def _require_signed(home: Path) -> bool:
    """Whether ``[update] require_signed`` gates the pull (default False).

    Fail-closed on the control's *enablement*: a missing config reads as off (so
    clone-and-go is unchanged), but a config that exists yet cannot be read
    returns True — a corrupt/locked cohort.toml refuses unsigned updates rather
    than silently disabling the gate. The value is read with a stdlib scanner
    (not tomllib), so the flag is honored on Python 3.10 too, where a tomllib
    parse would raise and no-op the gate."""
    try:
        text = _config_text(home)
    except Exception:  # noqa: BLE001 - present but unreadable (perms/encoding) → fail closed
        return True
    if text is None:
        return False  # no config at all → off
    rhs = _update_table_value(text, "require_signed")
    return _truthy_toml(rhs) if rhs is not None else False


def _commit_is_signed(source: Path, sha: str) -> bool:
    """True iff ``git verify-commit`` accepts the signature on the resolved commit
    ``sha`` — a good signature from a key git already trusts. Fail-closed: a bad
    signature, no signature, unknown key, or any git error all yield False.

    ``sha`` must be an already-resolved object id (never a config-derived ref), so
    it can never be read as a git flag, and the object verified is exactly the one
    the caller will merge — closing the verify-vs-apply TOCTOU window."""
    if not sha:
        return False
    rc, _ = _git(source, "verify-commit", sha)
    return rc == 0


def _signed_by(home: Path) -> list[str]:
    """Pinned signer fingerprints from ``[update] signed_by`` (default []).

    The strict tier layered over ``require_signed`` (#105): ``verify-commit`` only
    proves "signed by a key git trusts," so pinning ties acceptance to specific
    key fingerprints — an SSH ``SHA256:…`` fingerprint (``ssh-keygen -lf key.pub``)
    or a full GPG fingerprint. Given as a TOML array of strings, single- or
    multi-line: ``signed_by = ["SHA256:abc…", "SHA256:def…"]``. A non-empty list
    implies ``require_signed``. Read with the stdlib scanner (not tomllib, absent
    on the 3.10 floor); an unreadable/absent config yields [] — but
    ``require_signed`` still fail-closes there, so an unsigned tip is refused
    regardless."""
    try:
        text = _config_text(home)
    except Exception:  # noqa: BLE001 - unreadable; require_signed still refuses unsigned
        return []
    if text is None:
        return []
    rhs = _update_array_text(text, "signed_by")
    if rhs is None:
        return []
    return [a or b for a, b in re.findall(r'"([^"]*)"|\'([^\']*)\'', rhs) if (a or b).strip()]


def _signing_key_fingerprints(raw: str) -> set[str]:
    """The signing-key fingerprints named by ``git verify-commit --raw`` output —
    and *only* those, never free-text a signer controls.

    Two formats appear:

    * GPG status lines ``[GNUPG:] VALIDSIG <fpr> … <primary-fpr>`` — the fpr fields
      are the actual key, distinct from the ``GOODSIG <keyid> <user-id>`` line
      whose trailing user-id is set by the key's owner. Anchoring to ``VALIDSIG``
      as the status keyword (position 1, right after ``[GNUPG:]``) means a crafted
      user-id embedding a pinned fingerprint cannot be mistaken for the key.
    * SSH ``… with <ALGO> key SHA256:<b64>`` — the token right after ``key``.

    Extracting the key token and comparing for equality (below) closes the bypass
    where ``pin in raw_output`` would match a pinned string planted anywhere in the
    blob, e.g. a malicious commit's UID."""
    fingerprints: set[str] = set()
    for line in raw.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "[GNUPG:]" and len(parts) >= 3 and parts[1] == "VALIDSIG":
            fingerprints.update(
                tok.upper() for tok in parts[2:] if re.fullmatch(r"[0-9A-Fa-f]{40,}", tok)
            )
        for i, tok in enumerate(parts[:-1]):
            if tok == "key" and parts[i + 1].startswith("SHA256:"):
                fingerprints.add(parts[i + 1])
    return fingerprints


def _commit_signer_allowed(source: Path, sha: str, pins: list[str]) -> bool:
    """True iff ``sha`` carries a good signature AND its signing key's fingerprint
    equals one of the pinned fingerprints. Runs ``git verify-commit --raw`` (which
    both sets a non-zero exit on a bad/absent/untrusted signature and names the
    signing key), extracts the key fingerprint from that output, and requires a
    *whole-token* match — not a substring, which a signer-controlled user-id could
    otherwise spoof. Fail-closed: no pins, empty sha, non-zero exit, no
    identifiable key, or any error → False."""
    if not sha or not pins:
        return False
    try:
        proc = subprocess.run(
            ["git", "-C", str(source), "verify-commit", "--raw", sha],  # hardening via _GIT_ENV
            capture_output=True, text=True, timeout=_GIT_TIMEOUT, env={**os.environ, **_GIT_ENV},
        )
    except Exception:  # noqa: BLE001 - missing git / timeout → fail closed
        return False
    if proc.returncode != 0:  # not a good, trusted signature at all
        return False
    keys = _signing_key_fingerprints((proc.stdout or "") + "\n" + (proc.stderr or ""))
    if not keys:  # signed, but we cannot identify the key → fail closed
        return False
    wanted = {p if p.startswith("SHA256:") else p.upper() for p in (pin.strip() for pin in pins)}
    return not keys.isdisjoint(wanted)


def _remote_default_branch(source: Path, remote: str) -> Optional[str]:
    """The remote's actual default branch, asked directly of the remote via
    ``git ls-remote --symref <remote> HEAD`` — used when the local
    ``refs/remotes/<remote>/HEAD`` symref is unset (e.g. a manual ``remote add``
    + ``fetch`` rather than ``git clone``/a followRemoteHEAD-managed fetch, which
    set it automatically). Parses the ``ref: refs/heads/<name>\tHEAD`` line.
    ``--`` terminates option parsing so a config-derived ``remote`` that looks
    like a flag is always read as the remote name. None on any failure/timeout/
    missing-git — callers fall back to a hardcoded default."""
    rc, out = _git(source, "ls-remote", "--symref", "--", remote, "HEAD", timeout=_FETCH_TIMEOUT)
    if rc != 0 or not out:
        return None
    for line in out.splitlines():
        if not line.startswith("ref:"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1].startswith("refs/heads/"):
            branch = parts[1][len("refs/heads/"):]
            if branch:
                return branch
    return None


def resolve_upstream(source: Path, home: Path) -> tuple[str, str]:
    """The ``(remote, branch)`` to compare against. Config overrides win; else
    ``origin`` + the remote's default branch — from the local
    ``refs/remotes/<remote>/HEAD`` symref when that resolves cleanly, else asked
    directly of the remote (``ls-remote --symref``), else the hardcoded
    ``_DEFAULT_BRANCH`` fallback.

    The direct-remote lookup matters because this repo's (and many repos')
    default branch is not ``main``: if ``origin/HEAD`` is unset locally and we
    fell straight to a hardcoded ``main``, ``update_status``'s ``rev-list`` would
    fail against a nonexistent ``origin/main`` and the caller silently reads
    that as ``unavailable`` — the user is never told they're behind (#O7)."""
    remote_cfg, branch_cfg = _read_update_config(home)
    remote = remote_cfg or "origin"
    if branch_cfg:
        return remote, branch_cfg
    # symbolic-ref --quiet exits non-zero with empty stdout when origin/HEAD is
    # unset (unlike rev-parse --abbrev-ref, which prints "origin/HEAD" on failure).
    rc, out = _git(source, "symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD")
    prefix = f"refs/remotes/{remote}/"
    if rc == 0 and out.startswith(prefix):
        branch = out[len(prefix):]
        if branch and branch != "HEAD":
            return remote, branch
    branch = _remote_default_branch(source, remote)
    if branch:
        return remote, branch
    return remote, _DEFAULT_BRANCH


def update_status(source: Path, home: Path) -> dict:
    """Best-effort: how far the clone is behind upstream. Never raises.

    Returns ``{available, behind, diverged, current, upstream}``. ``available`` is
    False whenever the answer is indeterminate (offline, no remote, detached HEAD,
    bad/shallow ref) — callers must not advise in that case.
    """
    remote, branch = resolve_upstream(source, home)
    upstream = f"{remote}/{branch}"
    unavailable = {"available": False, "upstream": upstream}

    # Detached HEAD (CI checkout, bisect, viewing a tag) → no branch to compare.
    rc, head = _git(source, "symbolic-ref", "--quiet", "HEAD")
    if rc != 0 or not head:
        return unavailable

    # ``--`` terminates option parsing: a config-supplied remote that looks like
    # an option (e.g. ``--upload-pack=<cmd>``) must be treated as a remote name,
    # never a git flag — otherwise a tampered global cohort.toml yields RCE.
    rc, _ = _git(source, "fetch", "--quiet", "--", remote, timeout=_FETCH_TIMEOUT)
    if rc != 0:
        return unavailable  # offline / auth-required / no such remote

    rc, count = _git(source, "rev-list", "--count", f"HEAD..{upstream}")
    if rc != 0 or not count.isdigit():
        return unavailable  # bad ref, shallow clone, etc. — don't int("") -> raise
    behind = int(count)

    # Only a clean fast-forward (HEAD is an ancestor of upstream) is a plain
    # "you're behind"; a diverged history must not advise a simple /update.
    rc, _ = _git(source, "merge-base", "--is-ancestor", "HEAD", upstream)
    diverged = rc != 0
    rc2, current = _git(source, "rev-parse", "--short", "HEAD")
    return {
        "available": True,
        "behind": behind,
        "diverged": diverged,
        "current": current if rc2 == 0 else "",
        "upstream": upstream,
    }


def advisory_message(status: dict) -> Optional[str]:
    """The one-line advisory when a clean upgrade is available, else None."""
    if not status.get("available") or status.get("diverged"):
        return None
    behind = status.get("behind", 0)
    if behind <= 0:
        return None
    plural = "s" if behind != 1 else ""
    return (
        f"cohort: {behind} commit{plural} behind {status['upstream']} — "
        f"run /update to upgrade."
    )


def _throttle_marker(home: Path) -> Path:
    return CohortPaths(home).state / ".update-checked"


def _throttled_today(home: Path, today: str) -> bool:
    marker = _throttle_marker(home)
    try:
        return marker.exists() and marker.read_text(encoding="utf-8").strip() == today
    except Exception:  # noqa: BLE001 - corrupt/unreadable marker → re-check
        return False


def _mark_checked(home: Path, today: str) -> None:
    marker = _throttle_marker(home)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)  # state/ may not exist yet
        marker.write_text(today, encoding="utf-8")
    except Exception:  # noqa: BLE001 - best-effort throttle; never block
        pass


def do_update_check(home: Path, *, now: Optional[datetime] = None) -> Optional[str]:
    """Advisory string if a newer Cohort is available, else None. Never raises.

    Throttles the *network check* to once per UTC day per machine (one fetch/day),
    accepting that an upstream bump later the same day surfaces the next day. The
    marker is written only after a successful (available) check, so offline
    sessions retry rather than burning the daily slot.
    """
    today = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    try:
        source = resolve_source()  # may raise SourceUnresolved (site-packages, no clone)
    except SourceUnresolved:
        return None
    if _throttled_today(home, today):
        return None
    status = update_status(source, home)
    if not status.get("available"):
        return None
    _mark_checked(home, today)
    return advisory_message(status)


# --- Phase 2: the explicit `cohort update` command -------------------------
#
# Advisory-only updates are Phase 1; this applies one. The contract: never touch a
# dirty or diverged tree, only ever fast-forward (never synthesize a merge commit),
# reinstall the package only when its deps change, and recompile exactly the IDEs
# the install manifest records. Nothing changes on a dry run.

PipRunner = Callable[[list], int]


@dataclass
class UpdateResult:
    """Outcome of :func:`do_update`. ``status`` drives the CLI exit code.

    Statuses: ``up_to_date`` / ``dry_run`` / ``updated`` / ``rolled_back``
    (success, exit 0); ``unavailable`` / ``diverged`` / ``dirty`` / ``unsigned``
    / ``pull_failed`` / ``reset_failed`` / ``pip_failed`` / ``recompile_refused``
    / ``no_rollback_point`` / ``unknown_ref`` / ``not_earlier`` (refused/failed, exit 1).
    """

    status: str
    upstream: str = ""
    behind: int = 0
    current: str = ""
    target: str = ""
    commits: list = field(default_factory=list)
    changed_files: list = field(default_factory=list)
    pip_reinstalled: bool = False
    recompiled_ides: list = field(default_factory=list)
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("up_to_date", "dry_run", "updated", "rolled_back")

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "upstream": self.upstream,
            "behind": self.behind,
            "current": self.current,
            "target": self.target,
            "commits": list(self.commits),
            "changed_files": list(self.changed_files),
            "pip_reinstalled": self.pip_reinstalled,
            "recompiled_ides": list(self.recompiled_ides),
            "detail": self.detail,
        }


def _is_dirty(source: Path) -> bool:
    """True if the working tree has any uncommitted change — modified tracked files
    OR untracked files (``--porcelain`` lists both; it only omits ``.gitignore``-d
    paths, so Cohort's own .cohort/.claude artifacts are excluded). A failed status
    read counts as dirty — refuse rather than risk it."""
    rc, out = _git(source, "status", "--porcelain")
    return rc != 0 or bool(out.strip())


def _incoming_commits(source: Path, upstream: str) -> list:
    """One-line summaries of the commits ``HEAD..upstream`` would bring in. Includes
    merges so the count matches ``behind`` from ``update_status``. ``upstream`` is
    embedded mid-token (``HEAD..<ref>``) so it can never be read as a git option."""
    rc, out = _git(source, "log", "--oneline", f"HEAD..{upstream}")
    return out.splitlines() if rc == 0 and out else []


def _changed_files(source: Path, upstream: str) -> list:
    """Repo-relative paths changed between HEAD and the upstream tip."""
    rc, out = _git(source, "diff", "--name-only", f"HEAD..{upstream}")
    return out.splitlines() if rc == 0 and out else []


def _range_files(source: Path, rng: str) -> list:
    """Repo-relative paths changed across an ``a..b`` range (refs are resolved
    SHAs, so ``rng`` can never be read as a git option)."""
    rc, out = _git(source, "diff", "--name-only", rng)
    return out.splitlines() if rc == 0 and out else []


def _range_commits(source: Path, rng: str) -> list:
    """One-line summaries across an ``a..b`` range."""
    rc, out = _git(source, "log", "--oneline", rng)
    return out.splitlines() if rc == 0 and out else []


# --- update history (the rollback ledger) -----------------------------------


def _history_path(home: Path) -> Path:
    return CohortPaths(home).state / "update-history.json"


def _record_update(home: Path, from_sha: str, to_sha: str, action: str, *, at: str) -> None:
    """Append one clone-move to the history ledger (kept to the last 20). Advisory
    state only — a write failure never fails the update/rollback itself."""
    import json

    path = _history_path(home)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        entries = data.get("entries", []) if isinstance(data, dict) else []
    except Exception:  # noqa: BLE001 - a corrupt ledger must not block the operation
        entries = []
    entries.append({"from": from_sha, "to": to_sha, "action": action, "at": at})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"entries": entries[-20:]}, indent=2), encoding="utf-8")
    except OSError:
        pass


def _last_rollback_point(home: Path) -> Optional[str]:
    """The pre-update SHA of the most recent recorded *update* — where a bare
    ``cohort rollback`` returns to. None if nothing has been updated."""
    import json

    path = _history_path(home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        for entry in reversed(data.get("entries", [])):
            if entry.get("action") == "update" and entry.get("from"):
                return str(entry["from"])
    except Exception:  # noqa: BLE001 - missing/corrupt ledger → nothing to roll back to
        return None
    return None


_PIP_TIMEOUT = 600  # a hung pip must never wedge a caller (the dashboard holds its
# action lock across do_update); ten minutes dwarfs any healthy reinstall


def _default_pip_run(args: list) -> int:
    """Run a pip command, returning its exit code. Isolated so tests fake it."""
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=_PIP_TIMEOUT).returncode
    except subprocess.TimeoutExpired:
        return 1  # surfaces as pip_failed, a clean refusal


# The compiler/renderer modules `_recompile_installed` imports (below). Once
# imported, a module stays cached in this process's `sys.modules` for the rest
# of the process lifetime — so a same-process recompile right after a fast-
# forward that touched one of these still runs the PRE-update rendering logic
# against the POST-update source tree. The placed artifacts would then reflect
# old logic while `do_update` reports a clean success (#O3).
_RENDERER_FILES = ("cli/cohort/compile.py",)
_RENDERER_DIR_PREFIXES = ("cli/cohort/adapters/",)


def _renderer_files_changed(changed_files: list) -> bool:
    """True if the incoming range (``HEAD..upstream``) touches a compiler/
    renderer module — ``compile.py`` or an ``adapters/*`` file — that this
    process may already hold stale in ``sys.modules``."""
    return any(
        f in _RENDERER_FILES or f.startswith(_RENDERER_DIR_PREFIXES)
        for f in changed_files
    )


def _recompile_installed(source: Path, home: Path) -> tuple:
    """Recompile + reinstall every IDE the manifest records, in its install mode.

    Returns ``(recompiled_ides, refused_detail)``. ``refused_detail`` is set when
    ``do_install`` hits a foreign file at a managed path — we never auto-``--force``
    during an update the user didn't opt into. A missing manifest (no install yet)
    recompiles nothing.
    """
    from .compile import CompileError, compile_ide, planned_dests, write_staging
    from .executor import ClobberRefused
    from .install import do_install
    from .install_model import resolve_mode
    from .manifest import load_manifest

    paths = CohortPaths(home)
    ides: list = []
    # Everything here runs AFTER the fast-forward has applied, so it must never
    # raise: a corrupt manifest (load_manifest), a malformed pulled tree
    # (CompileError), a foreign file (ClobberRefused), or an OS error
    # (read-only FS, full disk) all degrade to a refused_detail.
    try:
        manifest = load_manifest(paths.manifest)
        ides = list(manifest.ides) if manifest else []
        if not ides:
            return [], None
        mode = manifest.mode if (manifest and manifest.mode) else resolve_mode(copy=False)
        # A tailored roster (cohort setup / recompile --agents) survives updates.
        only = frozenset(manifest.roster) if manifest and manifest.roster else None
        results = [
            compile_ide(source, ide, scope="global", only_agents=only, overlay=paths.my)
            for ide in ides
        ]
        for result in results:
            write_staging(paths, result)
        # Prune an agent/artifact the pulled canonical no longer produces, so an
        # upstream deletion (or a persisted subset) doesn't leave a dangling link.
        do_install(
            home=home, selection=ides, mode=mode, force=False, source=source, dry_run=False,
            prune_stale=True, fresh_dests=planned_dests(paths, results),
            fresh_ides={r.ide for r in results if r.staged},
        )
    except ClobberRefused as exc:
        return ides, str(exc)
    except CompileError as exc:
        return ides, f"the updated canonical tree failed to compile: {exc}"
    except Exception as exc:  # noqa: BLE001 - do_update must not raise once the merge applied
        return ides, f"recompile failed after the update applied: {exc}"
    return ides, None


def do_relink(source: Path, home: Path) -> dict:
    """Re-point a moved/renamed install at ``source`` and recompile its IDEs.

    Reuses the recompile path, which (since the executor self-heals Cohort-owned
    links) re-points a dangling ``~/.cohort/canonical`` and re-stages without
    ``--force``. Returns ``{recompiled_ides, refused}``.
    """
    ides, refused = _recompile_installed(source, home)
    return {"recompiled_ides": ides, "refused": refused}


def do_update(
    source: Path,
    home: Path,
    *,
    dry_run: bool = False,
    pip_run: Optional[PipRunner] = None,
) -> UpdateResult:
    """Apply a pending Cohort update: fast-forward the clone, reinstall the package
    if its deps changed, and recompile installed IDEs. Refuses on a dirty/diverged
    tree; previews only on ``dry_run``. ``pip_run`` is injectable for tests.
    """
    pip_run = pip_run or _default_pip_run
    status = update_status(source, home)  # fetches; yields behind/diverged/upstream
    upstream = status.get("upstream", "")
    if not status.get("available"):
        return UpdateResult(
            status="unavailable", upstream=upstream,
            detail="Upstream is unreachable or the checkout can't be compared "
            "(offline, detached HEAD, no remote, or shallow clone).",
        )
    current = status.get("current", "")
    if status.get("diverged"):
        return UpdateResult(
            status="diverged", upstream=upstream, current=current,
            detail="Local history has diverged from upstream; reconcile (rebase or "
            "merge) before updating.",
        )
    behind = int(status.get("behind", 0))
    if behind <= 0:
        return UpdateResult(status="up_to_date", upstream=upstream, current=current, behind=0)

    if _is_dirty(source):
        return UpdateResult(
            status="dirty", upstream=upstream, current=current, behind=behind,
            detail="Working tree has uncommitted changes; commit or stash them "
            "before running cohort update.",
        )

    # Resolve the upstream tip to a concrete SHA ONCE and bind everything to it —
    # summary, signature check, and the merge. The tracking ref is shared mutable
    # state (a concurrent fetch, e.g. the session-start update-check, can advance
    # it mid-run), so verifying the ref then merging it *by name* would let an
    # unverified child slip in between (TOCTOU). Merging the pinned SHA closes that
    # window and makes the preview identical to what is applied.
    rc, tip = _git(source, "rev-parse", "--verify", f"{upstream}^{{commit}}")
    if rc != 0 or not tip:
        return UpdateResult(
            status="pull_failed", upstream=upstream, behind=behind, current=current,
            detail="Could not resolve the upstream tip to a commit (the tracking "
            "ref may be mid-fetch); re-run `cohort update`.",
        )
    commits = _incoming_commits(source, tip)
    changed_files = _changed_files(source, tip)
    _, target = _git(source, "rev-parse", "--short", tip)

    # Opt-in supply-chain gate (#30, #105): the residual risk once transport and
    # local config are trusted is a *compromised upstream* whose malicious commit
    # is still a valid fast-forward. Two tiers, both gating the dry run too so a
    # preview never implies an apply that would then be refused:
    #   * signed_by — a non-empty pin list requires the tip's signing key to match
    #     a pinned fingerprint (and implies require_signed); the strong assurance.
    #   * require_signed — the tip must be a verifiably signed commit (any key git
    #     trusts). Weaker without a pinned allowed-signers store, but a real gate.
    # Fail-closed throughout.
    pins = _signed_by(home)
    if pins:
        if not _commit_signer_allowed(source, tip, pins):
            return UpdateResult(
                status="unsigned", upstream=upstream, behind=behind, current=current,
                target=target, commits=commits, changed_files=changed_files,
                detail="[update] signed_by is set, but the upstream tip "
                f"({target}) is not signed by a pinned key. Verify the source and "
                "its signing key, then re-run — only change signed_by as a "
                "deliberate trust decision.",
            )
    elif _require_signed(home) and not _commit_is_signed(source, tip):
        return UpdateResult(
            status="unsigned", upstream=upstream, behind=behind, current=current,
            target=target, commits=commits, changed_files=changed_files,
            detail="[update] require_signed is set, but the upstream tip "
            f"({target}) is not a verifiably signed commit. Confirm the source is "
            "trustworthy and pin its signing key (git's gpg.ssh.allowedSignersFile, "
            "or a trusted GPG key), then re-run — only unset require_signed as a "
            "deliberate downgrade. For key-identity assurance, pin signed_by.",
        )

    if dry_run:
        return UpdateResult(
            status="dry_run", upstream=upstream, behind=behind, current=current,
            target=target, commits=commits, changed_files=changed_files,
        )

    # Capture the pre-merge HEAD so the update is reversible (cohort rollback).
    _, pre_sha = _git(source, "rev-parse", "HEAD")

    # Fast-forward only, to the SAME SHA summarized and verified above (not the ref
    # name) — refuses (rc != 0) rather than ever creating a merge commit. A 40-hex
    # object id can never be read as a git flag.
    rc, _ = _git(source, "merge", "--ff-only", "--", tip, timeout=_PULL_TIMEOUT)
    if rc != 0:
        return UpdateResult(
            status="pull_failed", upstream=upstream, behind=behind, current=current,
            target=target, commits=commits, changed_files=changed_files,
            detail="git merge --ff-only failed (the tree changed under us). "
            "Run `git status` in the Cohort source and retry.",
        )
    # The clone moved — record the rollback point before any downstream step.
    _, post_sha = _git(source, "rev-parse", "HEAD")
    if pre_sha and post_sha:
        _record_update(home, pre_sha, post_sha, "update",
                       at=datetime.now(timezone.utc).isoformat())

    pip_reinstalled = False
    if "pyproject.toml" in changed_files:
        if pip_run([sys.executable, "-m", "pip", "install", "-e", str(source)]) != 0:
            return UpdateResult(
                status="pip_failed", upstream=upstream, behind=behind, current=current,
                target=target, commits=commits, changed_files=changed_files,
                detail="The fast-forward landed but `pip install -e` failed; run it "
                "manually from the Cohort source, then `cohort recompile`.",
            )
        pip_reinstalled = True

    recompiled, refused = _recompile_installed(source, home)
    if refused is not None:
        return UpdateResult(
            status="recompile_refused", upstream=upstream, behind=behind, current=current,
            target=target, commits=commits, changed_files=changed_files,
            pip_reinstalled=pip_reinstalled, recompiled_ides=recompiled,
            detail="Updated the clone, but recompile found foreign files at a managed "
            "path. Run `cohort recompile --force` to back up and replace them. " + refused,
        )

    detail = ""
    if _renderer_files_changed(changed_files):
        # The recompile just above already ran — but under this process's stale,
        # pre-update compile.py/adapters, against the just-pulled source. Warn
        # rather than let `behind == 0` go quiet about it: nothing will
        # auto-recompile this correctly until a fresh process does.
        detail = (
            "This update changed Cohort's own compiler/renderer (compile.py or "
            "an adapter). This process already had the old version loaded, so "
            "the recompile just performed may have used stale rendering logic. "
            "Run `cohort recompile` in a new shell/session to regenerate the "
            "installed artifacts under the updated code."
        )
        warnings.warn(detail, UserWarning, stacklevel=2)

    return UpdateResult(
        status="updated", upstream=upstream, behind=behind, current=current, target=target,
        commits=commits, changed_files=changed_files, pip_reinstalled=pip_reinstalled,
        recompiled_ides=recompiled, detail=detail,
    )


def do_rollback(
    source: Path,
    home: Path,
    *,
    to: Optional[str] = None,
    dry_run: bool = False,
    pip_run: Optional[PipRunner] = None,
) -> UpdateResult:
    """Move the clone back to an earlier version and recompile — the inverse of
    ``do_update``. With no ``to``, returns to the pre-update SHA of the most recent
    update (the recorded rollback point); with ``to``, to that tag/ref.

    Rollback only ever goes *backward*: the target must be an ancestor of HEAD
    (moving forward is ``cohort update``). Reset-then-recompile is fully reversible
    — the discarded commits still live upstream, so a later ``cohort update`` brings
    them right back. Refuses on a dirty tree; never touches ``~/.cohort/my``.
    """
    pip_run = pip_run or _default_pip_run
    rc, current = _git(source, "rev-parse", "--short", "HEAD")
    if rc != 0 or not current:
        return UpdateResult(
            status="unavailable",
            detail="Not a git checkout, or git is unavailable — cannot roll back.",
        )
    if _is_dirty(source):
        return UpdateResult(
            status="dirty", current=current,
            detail="Working tree has uncommitted changes; commit or stash them "
            "before rolling back.",
        )

    if to:
        rc, target_full = _git(source, "rev-parse", "--verify", f"{to}^{{commit}}")
        if rc != 0 or not target_full:
            return UpdateResult(
                status="unknown_ref", current=current,
                detail=f"no such tag or ref {to!r} in the Cohort source.",
            )
    else:
        target_full = _last_rollback_point(home)
        if not target_full:
            return UpdateResult(
                status="no_rollback_point", current=current,
                detail="No recorded update to roll back. Pass `--to <tag>` to pick a "
                "version (see the CHANGELOG / `git tag` in the Cohort source).",
            )

    _, head_full = _git(source, "rev-parse", "HEAD")
    _, target = _git(source, "rev-parse", "--short", target_full)
    if head_full and head_full == target_full:
        return UpdateResult(
            status="up_to_date", current=current, target=target,
            detail="Already at that version — nothing to roll back.",
        )
    # Backward-only: the target must be reachable from HEAD.
    rc_anc, _ = _git(source, "merge-base", "--is-ancestor", target_full, "HEAD")
    if rc_anc != 0:
        return UpdateResult(
            status="not_earlier", current=current, target=target,
            detail=f"{to or target!r} is not an earlier version of this checkout; "
            "use `cohort update` to move forward.",
        )

    rng = f"{target_full}..HEAD"
    undone = _range_commits(source, rng)  # the commits this rollback discards
    changed = _range_files(source, rng)
    if dry_run:
        return UpdateResult(
            status="dry_run", current=current, target=target,
            commits=undone, changed_files=changed,
        )

    rc, _ = _git(source, "reset", "--hard", target_full, timeout=_PULL_TIMEOUT)
    if rc != 0:
        return UpdateResult(
            status="reset_failed", current=current, target=target,
            detail="git reset --hard failed; inspect the Cohort source manually.",
        )
    if head_full and target_full:
        _record_update(home, head_full, target_full, "rollback",
                       at=datetime.now(timezone.utc).isoformat())

    pip_reinstalled = False
    if "pyproject.toml" in changed:
        if pip_run([sys.executable, "-m", "pip", "install", "-e", str(source)]) != 0:
            return UpdateResult(
                status="pip_failed", current=current, target=target,
                commits=undone, changed_files=changed,
                detail="Rolled the clone back but `pip install -e` failed; run it "
                "manually from the Cohort source, then `cohort recompile`.",
            )
        pip_reinstalled = True

    recompiled, refused = _recompile_installed(source, home)
    if refused is not None:
        return UpdateResult(
            status="recompile_refused", current=current, target=target,
            commits=undone, changed_files=changed, pip_reinstalled=pip_reinstalled,
            recompiled_ides=recompiled,
            detail="Rolled the clone back, but recompile found foreign files at a "
            "managed path. Run `cohort recompile --force` to back up and replace "
            "them. " + refused,
        )
    return UpdateResult(
        status="rolled_back", current=current, target=target, commits=undone,
        changed_files=changed, pip_reinstalled=pip_reinstalled, recompiled_ides=recompiled,
    )
