"""Tests for the fail-closed external-engine safety gates (RFC 0004).

Every gate blocks on doubt. These tests assert on behaviour — which labels a scan
returns, which paths a footprint check rejects, and the ordering of the preflight —
and on the invariant that a matched secret *value* never leaks into a raised message.
No test performs network I/O.
"""

from __future__ import annotations

import pytest

from cohort.engines.gates import (
    EgressBlockedError,
    GateError,
    PathViolationError,
    PayloadTooLargeError,
    SecretFoundError,
    assert_no_secrets,
    assert_paths_allowed,
    assert_payload_within,
    check_changed_paths,
    egress_opted_out,
    preflight,
    require_egress_allowed,
    scan_for_secrets,
)

# A stand-in secret value that must never surface in any label or error message.
_SECRET_VALUE = "sk-live-0123456789abcdefZZ"


# --------------------------------------------------------------------------- #
# 1. Egress opt-out
# --------------------------------------------------------------------------- #


def test_egress_opted_out_via_literal_marker() -> None:
    text = "# Project Context\n\nsome prose\ncohort:egress=deny\nmore prose\n"
    assert egress_opted_out(text) is True


def test_egress_marker_is_case_insensitive_and_whitespace_tolerant() -> None:
    # The directive itself tolerates internal whitespace/case and a little leading
    # indentation, but (per the negation-proof fix below) must still stand alone on
    # its own line.
    text = "Notes below.\n  Cohort : Egress = DENY  \nMore notes.\n"
    assert egress_opted_out(text) is True


def test_egress_marker_embedded_in_a_sentence_does_not_count() -> None:
    # EGRESS-PROSE regression: the marker text is no longer matched as a whole-file
    # substring -- it must be the entire line, or a sentence that merely *contains*
    # the marker text does not trip it either way.
    text = "Notes: Cohort : Egress = DENY is set here.\n"
    assert egress_opted_out(text) is False


def test_egress_section_denies_by_default_without_allow_marker() -> None:
    # Merely having an '## Egress' section is a deliberate policy statement; absent an
    # explicit allow marker it denies (deny-by-default), whatever the prose says.
    text = (
        "# Project Context\n\n"
        "## Egress\n\n"
        "External engines are restricted for this repo.\n\n"
        "## Other\n"
    )
    assert egress_opted_out(text) is True


def test_egress_section_with_explicit_allow_marker_is_not_opted_out() -> None:
    text = (
        "## Egress\n\n"
        "External engines are permitted for advisory consults.\n"
        "cohort:egress=allow\n"
    )
    assert egress_opted_out(text) is False


def test_egress_section_prose_allow_does_not_fail_open() -> None:
    # The negation trap: a section that says engines are "disabled"/"NOT allowed" must
    # never be misread as permission. Without the structured allow marker it denies.
    for body in (
        "External engines are disabled.\n",
        "External engines are forbidden.\n",
        "External engines are NOT allowed.\n",
        "Egress is turned off.\n",
    ):
        text = f"## Egress\n\n{body}"
        assert egress_opted_out(text) is True, body


def test_egress_prose_mentioning_allow_marker_inside_prohibition_stays_opted_out() -> (
    None
):
    # EGRESS-PROSE: a whole-file substring search for the allow marker used to let a
    # sentence that merely *mentions* the marker text disable the opt-out -- e.g. a
    # prohibition like "do NOT add cohort:egress=allow" contains the literal allow
    # string, so a substring match misread the prohibition as permission. The marker
    # must now be the entire line to count, so this prose is not read as the allow
    # directive, and the '## Egress' section still denies by default.
    text = (
        "# Project Context\n\n"
        "## Egress\n\n"
        "Do NOT add cohort:egress=allow to this file under any circumstance.\n"
    )
    assert egress_opted_out(text) is True


def test_egress_deny_marker_beats_allow_marker() -> None:
    # deny always wins over allow (fail closed) regardless of order.
    assert egress_opted_out("cohort:egress=allow\ncohort:egress=deny\n") is True
    assert egress_opted_out("cohort:egress=deny\ncohort:egress=allow\n") is True


def test_absent_file_or_section_is_not_opted_out() -> None:
    assert egress_opted_out("") is False
    assert egress_opted_out("# Project Context\n\nNothing about egress here.\n") is False


def test_require_egress_allowed_raises_when_opted_out() -> None:
    with pytest.raises(EgressBlockedError):
        require_egress_allowed("cohort:egress=deny")


def test_require_egress_allowed_is_silent_when_allowed() -> None:
    require_egress_allowed("# Project Context\n\nno opt-out marker\n")


# --------------------------------------------------------------------------- #
# 2. Secret scan
# --------------------------------------------------------------------------- #


def test_scan_detects_aws_access_key_id() -> None:
    assert scan_for_secrets("id: AKIAIOSFODNN7EXAMPLE here") == ["aws-access-key-id"]


def test_scan_detects_aws_key_embedded_in_other_text() -> None:
    # An AKIA key with no surrounding whitespace boundary must still be flagged.
    labels = scan_for_secrets("prefixAKIAIOSFODNN7EXAMPLEsuffix")
    assert "aws-access-key-id" in labels


def test_scan_detects_private_key_block() -> None:
    for header in (
        "-----BEGIN PRIVATE KEY-----",
        "-----BEGIN RSA PRIVATE KEY-----",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
    ):
        assert scan_for_secrets(f"{header}\nMIIB...\n") == ["private-key-block"]


def test_scan_detects_bearer_token() -> None:
    assert scan_for_secrets("Authorization: Bearer abcdef0123456789xyz") == [
        "bearer-token"
    ]


def test_scan_ignores_bearer_english_word() -> None:
    # "Bearer" followed by a short ordinary word is not a token.
    assert scan_for_secrets("the Bearer of news") == []


def test_scan_detects_generic_assignment_keywords() -> None:
    assert scan_for_secrets("API_KEY = 'abcdef123456'") == [
        "generic-assignment:API_KEY"
    ]
    assert scan_for_secrets("DATABASE_PASSWORD=hunter2secret") == [
        "generic-assignment:PASSWORD"
    ]
    assert scan_for_secrets("GITHUB_TOKEN: ghp_abcdef123456") == [
        "generic-assignment:TOKEN"
    ]


def test_scan_detects_dotenv_style_sensitive_key() -> None:
    assert scan_for_secrets("MY_SECRET=supersecretvalue") == [
        "generic-assignment:SECRET"
    ]


def test_scan_detects_github_token() -> None:
    labels = scan_for_secrets("classic token ghp_" + "a" * 36 + " in the wild")
    assert labels == ["github-token"]


def test_scan_detects_github_fine_grained_pat() -> None:
    labels = scan_for_secrets("fine-grained github_pat_" + "a" * 30 + " leaked")
    assert labels == ["github-token"]


def test_scan_detects_slack_token() -> None:
    labels = scan_for_secrets("found xoxb-1234567890-abcdefghij in logs")
    assert labels == ["slack-token"]


def test_scan_detects_openai_or_anthropic_api_key() -> None:
    assert scan_for_secrets("leaked sk-ant-" + "a" * 25 + " value") == ["ai-api-key"]
    assert scan_for_secrets("leaked sk-" + "a" * 25 + " value") == ["ai-api-key"]


def test_scan_detects_google_api_key() -> None:
    assert scan_for_secrets("leaked AIza" + "a" * 35 + " value") == ["google-api-key"]


def test_scan_detects_jwt() -> None:
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    assert scan_for_secrets(f"the header carries {jwt} in transit") == ["jwt"]


def test_scan_detects_connection_string_credential() -> None:
    assert scan_for_secrets("db url is postgres://svc:S3cret@db/prod") == [
        "connection-string-credential"
    ]


def test_scan_catches_previously_missed_vendor_credential_shapes() -> None:
    # F4 regression: an adversarial review found these all passed `scan_for_secrets`
    # silently before the vendor patterns were added.
    assert "connection-string-credential" in scan_for_secrets(
        "postgres://svc:S3cret@db/prod"
    )
    assert "github-token" in scan_for_secrets("ghp_" + "a" * 36)
    assert "ai-api-key" in scan_for_secrets("sk-" + "a" * 25)
    assert "slack-token" in scan_for_secrets("xoxb-1234567890-abcdefghij")
    assert "google-api-key" in scan_for_secrets("AIza" + "a" * 35)


def test_scan_ignores_compiled_regex_naming_a_secret_keyword() -> None:
    # COORD-1: an identifier that merely *names* a secret keyword and is assigned a
    # compiled regex (code, not a credential) must not false-positive.
    text = '_URL_PASSWORD = re.compile(r"://[^:]+:([^@]+)@")\n'
    assert scan_for_secrets(text) == []


def test_scan_still_detects_real_password_assignment() -> None:
    # The converse of the COORD-1 fix: a genuine credential-shaped value assigned to
    # a sensitive identifier must still be flagged.
    assert scan_for_secrets('PASSWORD = "hunter2abc123"') == [
        "generic-assignment:PASSWORD"
    ]


def test_scan_clean_string_returns_empty_list() -> None:
    assert scan_for_secrets("just a normal sentence with no secrets") == []
    # A non-sensitive assignment must not be flagged.
    assert scan_for_secrets("DATABASE_HOST = localhost.internal") == []


def test_scan_returns_sorted_deduped_labels() -> None:
    text = "AKIAIOSFODNN7EXAMPLE and API_KEY=abcdef123 and API_KEY=zzzzzz999"
    labels = scan_for_secrets(text)
    assert labels == sorted(labels)
    assert labels.count("generic-assignment:API_KEY") == 1


def test_secret_value_never_appears_in_exception_message() -> None:
    text = (
        f"API_KEY={_SECRET_VALUE}\n"
        "Bearer tokenvalue0123456789\n"
        "AKIAIOSFODNN7EXAMPLE\n"
    )
    with pytest.raises(SecretFoundError) as excinfo:
        assert_no_secrets(text)
    message = str(excinfo.value)
    assert _SECRET_VALUE not in message
    assert "tokenvalue0123456789" not in message
    assert "AKIAIOSFODNN7EXAMPLE" not in message
    # The non-secret labels are still reported.
    assert "generic-assignment:API_KEY" in message


def test_assert_no_secrets_is_silent_on_clean_text() -> None:
    assert_no_secrets("nothing sensitive here")


# --------------------------------------------------------------------------- #
# 3. Path / scope gate
# --------------------------------------------------------------------------- #


def test_path_within_footprint_is_allowed() -> None:
    assert (
        check_changed_paths(
            ["src/app/main.py"], allowed_footprint=["src/"]
        )
        == []
    )


def test_path_outside_footprint_is_blocked() -> None:
    violations = check_changed_paths(
        ["docs/readme.md"], allowed_footprint=["src/"]
    )
    assert violations == ["docs/readme.md: outside-footprint"]


def test_glob_footprint_does_not_span_directory_boundary() -> None:
    # A single-star glob must not silently match across a "/".
    assert check_changed_paths(["src/a.py"], allowed_footprint=["src/*"]) == []
    assert check_changed_paths(
        ["src/sub/a.py"], allowed_footprint=["src/*"]
    ) == ["src/sub/a.py: outside-footprint"]
    # A double-star glob does span boundaries.
    assert check_changed_paths(["src/sub/a.py"], allowed_footprint=["src/**"]) == []


def test_path_traversal_is_normalized_and_blocked() -> None:
    # A "src/.." escape resolves into a sensitive .git path and is caught.
    violations = check_changed_paths(
        ["src/../.git/hooks/pre-commit"], allowed_footprint=["src/"]
    )
    assert len(violations) == 1
    assert "sensitive:" in violations[0]


def test_absolute_and_escaping_paths_are_blocked() -> None:
    violations = check_changed_paths(
        ["/etc/passwd", "../../elsewhere/x.py"], allowed_footprint=["**"]
    )
    assert violations == [
        "/etc/passwd: escapes-repo-root",
        "../../elsewhere/x.py: escapes-repo-root",
    ]


@pytest.mark.parametrize(
    "path, expected_class",
    [
        (".git/config", "git-internal"),
        ("submodule/.git/config", "git-internal"),
        ("path/to/.git/HEAD", "git-internal"),
        ("hooks/pre-push", "git-hook"),
        (".github/workflows/ci.yml", "ci-config"),
        (".gitlab-ci.yml", "ci-config"),
        ("package-lock.json", "lockfile"),
        ("uv.lock", "lockfile"),
        ("vendored/thing.lock", "lockfile"),
        ("setup.py", "build-manifest"),
        ("pyproject.toml", "build-manifest"),
        ("install.sh", "install-script"),
        ("deploy.sh", "install-script"),
        ("src/auth/session.py", "auth-crypto-secret"),
        ("lib/crypto_utils.py", "auth-crypto-secret"),
        ("app/secrets.py", "auth-crypto-secret"),
        (".env", "dotenv"),
        (".env.production", "dotenv"),
    ],
)
def test_sensitive_class_blocked_even_when_nominally_in_footprint(
    path: str, expected_class: str
) -> None:
    # Even with a repo-wide footprint, a sensitive path is blocked with its class.
    violations = check_changed_paths([path], allowed_footprint=["**", "."])
    assert violations == [f"{path}: sensitive:{expected_class}"]


@pytest.mark.parametrize(
    "path, expected_class",
    [
        ("scripts/release.sh", "executable-script"),
        ("tools/deploy/setup.bash", "executable-script"),
        ("scripts/provision.ps1", "executable-script"),
        ("Makefile", "executable-script"),
        ("docker/Dockerfile", "executable-script"),
    ],
)
def test_executable_script_is_sensitive_at_any_depth(
    path: str, expected_class: str
) -> None:
    # F6: `.sh` used to classify sensitive only at repo-root depth, so a nested
    # `scripts/release.sh` or a `Makefile`/`Dockerfile` slipped past the gate.
    violations = check_changed_paths([path], allowed_footprint=["**", "."])
    assert violations == [f"{path}: sensitive:{expected_class}"]


def test_explicit_override_allows_a_sensitive_path() -> None:
    # Listing the sensitive path by name is a deliberate, reviewed override.
    assert check_changed_paths([".env"], allowed_footprint=[".env"]) == []
    assert (
        check_changed_paths(
            ["src/auth/session.py"], allowed_footprint=["src/auth/session.py"]
        )
        == []
    )
    assert (
        check_changed_paths(
            [".github/workflows/ci.yml"],
            allowed_footprint=[".github/workflows/**"],
        )
        == []
    )


def test_broad_footprint_does_not_override_sensitivity() -> None:
    # A non-sensitive broad footprint may not launder a sensitive path.
    violations = check_changed_paths(
        ["src/auth.py"], allowed_footprint=["src/"]
    )
    assert violations == ["src/auth.py: sensitive:auth-crypto-secret"]


def test_override_requires_the_same_sensitive_class_as_the_path() -> None:
    # An entry sensitive in one class must not authorize a path sensitive in another.
    # Listing an auth path consents to auth writes, not to writes under its `.git`.
    assert check_changed_paths(
        ["src/auth/.git/config"], allowed_footprint=["src/auth/**"]
    ) == ["src/auth/.git/config: sensitive:git-internal"]
    # The same-class override still works.
    assert check_changed_paths(["src/auth/x.py"], allowed_footprint=["src/auth/**"]) == []


@pytest.mark.parametrize(
    "footprint_dir, path, expected_class",
    [
        ("authors", "authors/.git/config", "git-internal"),
        ("secretariat", "secretariat/.env", "dotenv"),
        ("cryptography", "cryptography/package-lock.json", "lockfile"),
    ],
)
def test_innocuous_directory_name_does_not_launder_a_sensitive_path(
    footprint_dir: str, path: str, expected_class: str
) -> None:
    # A directory whose name merely *starts with* an auth/crypto/secret keyword used to
    # classify as sensitive, which made it a blanket override for every sensitive class
    # beneath it -- so `--footprint authors/**` silently granted `.git` writes.
    violations = check_changed_paths([path], allowed_footprint=[f"{footprint_dir}/**"])
    assert violations == [f"{path}: sensitive:{expected_class}"]


@pytest.mark.parametrize(
    "path", ["authors/list.md", "secretariat/notes.md", "authorship.py"]
)
def test_innocuous_names_are_not_classified_sensitive(path: str) -> None:
    # The converse of the laundering fix: these must not be over-blocked either.
    assert check_changed_paths([path], allowed_footprint=["**"]) == []


@pytest.mark.parametrize(
    "path", ["auth.py", "src/auth_helpers.py", "crypto/keys.py", "app/secrets.py"]
)
def test_genuine_auth_names_are_still_classified_sensitive(path: str) -> None:
    assert check_changed_paths([path], allowed_footprint=["**"]) == [
        f"{path}: sensitive:auth-crypto-secret"
    ]


@pytest.mark.parametrize(
    "path", ["C:\\Windows\\System32\\drivers\\etc\\hosts", "C:/Windows/x", "d:\\x"]
)
def test_windows_drive_absolute_path_escapes_repo_root(path: str) -> None:
    # `_normalize_path` folds "\" to "/", leaving "C:/Windows/x" -- which has no leading
    # "/" and so read as an ordinary relative path. Testable on every platform because
    # the fold is unconditional.
    violations = check_changed_paths([path], allowed_footprint=["**"])
    assert violations == [f"{path}: escapes-repo-root"]


def test_assert_paths_allowed_raises_listing_violations() -> None:
    with pytest.raises(PathViolationError) as excinfo:
        assert_paths_allowed(
            ["docs/x.md", ".env"], allowed_footprint=["src/"]
        )
    message = str(excinfo.value)
    assert "docs/x.md: outside-footprint" in message
    assert ".env: sensitive:dotenv" in message


def test_assert_paths_allowed_is_silent_when_clean() -> None:
    assert_paths_allowed(["src/app.py"], allowed_footprint=["src/"])


# --------------------------------------------------------------------------- #
# 4. Payload bound
# --------------------------------------------------------------------------- #


def test_payload_under_cap_is_allowed() -> None:
    assert_payload_within("x" * 100, max_bytes=200)


def test_payload_over_cap_is_rejected() -> None:
    with pytest.raises(PayloadTooLargeError):
        assert_payload_within("x" * 300, max_bytes=200)


def test_payload_cap_counts_utf8_bytes_not_chars() -> None:
    # A 3-char string of 3-byte code points is 9 bytes, over an 8-byte cap.
    with pytest.raises(PayloadTooLargeError):
        assert_payload_within("一一一", max_bytes=8)


# --------------------------------------------------------------------------- #
# 5. Preflight ordering (fail-closed, first failure wins)
# --------------------------------------------------------------------------- #


def test_preflight_blocks_opted_out_repo_before_scanning() -> None:
    # The prompt carries a secret, but egress opt-out must win first.
    with pytest.raises(EgressBlockedError):
        preflight(
            prompt=f"API_KEY={_SECRET_VALUE}",
            project_context_text="cohort:egress=deny",
        )


def test_preflight_rejects_oversized_prompt_before_scanning() -> None:
    # Oversized-and-secret prompt: the payload bound must win over the secret scan.
    with pytest.raises(PayloadTooLargeError):
        preflight(
            prompt=f"API_KEY={_SECRET_VALUE}" + "x" * 500,
            project_context_text="egress allowed",
            max_bytes=20,
        )


def test_preflight_scans_for_secrets_when_egress_and_size_pass() -> None:
    with pytest.raises(SecretFoundError):
        preflight(
            prompt=f"API_KEY={_SECRET_VALUE}",
            project_context_text="egress allowed",
        )


def test_preflight_passes_a_clean_bounded_prompt() -> None:
    preflight(
        prompt="please refactor the parser for clarity",
        project_context_text="# Project Context\n\nno opt-out\n",
    )


def test_gate_errors_share_a_common_base() -> None:
    for exc_type in (
        EgressBlockedError,
        SecretFoundError,
        PathViolationError,
        PayloadTooLargeError,
    ):
        assert issubclass(exc_type, GateError)
