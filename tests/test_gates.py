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
    text = "Notes: Cohort : Egress = DENY is set here."
    assert egress_opted_out(text) is True


def test_egress_opted_out_via_egress_section_deny_word() -> None:
    text = (
        "# Project Context\n\n"
        "## Egress\n\n"
        "External engines are restricted for this repo.\n\n"
        "## Other\n"
    )
    assert egress_opted_out(text) is True


def test_egress_section_without_deny_word_is_not_opted_out() -> None:
    text = (
        "## Egress\n\n"
        "External engines are allowed for advisory consults.\n\n"
        "## Next\ndeny appears only here, outside the egress section\n"
    )
    assert egress_opted_out(text) is False


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
