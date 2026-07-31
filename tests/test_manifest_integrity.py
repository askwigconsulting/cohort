"""Audit r3, H3 — the manifest must fail closed and leave a way back.

The manifest is the record a ``reverse`` replays, so it is the single file whose loss
strands every placed artifact. It was also the least defended: ``load_manifest`` called
``json.loads`` bare, so a truncated, hand-edited, or schema-drifted file surfaced as a raw
``JSONDecodeError`` out of whichever command happened to touch it — and there was no copy
to fall back to.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

from cohort.manifest import (
    Manifest,
    ManifestCorruptError,
    load_manifest,
)


def _manifest() -> Manifest:
    return Manifest(
        install_id=str(uuid.uuid4()),
        created_at=datetime.now(timezone.utc).isoformat(),
        mode="copy",
        ides=["claude"],
    )


def test_absent_manifest_is_not_an_error(tmp_path) -> None:
    """Nothing installed yet is a legitimate state, distinct from corruption."""
    assert load_manifest(tmp_path / "manifest.json") is None


def test_corrupt_manifest_raises_instead_of_crashing_with_a_json_error(tmp_path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text('{"ides": ["claude"], truncated', encoding="utf-8")

    with pytest.raises(ManifestCorruptError) as excinfo:
        load_manifest(path)

    message = str(excinfo.value)
    assert str(path) in message          # names the file
    assert "cohort init --force" in message   # and a way forward


def test_a_manifest_missing_required_keys_is_corrupt_not_empty(tmp_path) -> None:
    """Schema drift must fail closed too — an install record Cohort cannot read is not
    an install record of nothing."""
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"not": "a manifest"}), encoding="utf-8")

    with pytest.raises(ManifestCorruptError):
        load_manifest(path)


def test_persist_keeps_the_previous_version_beside_the_live_one(tmp_path) -> None:
    path = tmp_path / "manifest.json"
    first = _manifest()
    first.persist(path)
    assert not path.with_suffix(".json.bak").exists()  # nothing to back up yet

    second = _manifest()
    second.persist(path)

    backup = path.with_suffix(".json.bak")
    assert backup.exists()
    assert json.loads(backup.read_text(encoding="utf-8"))["install_id"] == first.install_id
    assert json.loads(path.read_text(encoding="utf-8"))["install_id"] == second.install_id


def test_the_backup_is_what_makes_recovery_possible(tmp_path) -> None:
    """The end-to-end point of H3: after corruption there is something to restore from,
    and the error says so."""
    path = tmp_path / "manifest.json"
    original = _manifest()
    original.persist(path)
    _manifest().persist(path)          # now a .bak exists
    path.write_text("}}} not json", encoding="utf-8")

    with pytest.raises(ManifestCorruptError) as excinfo:
        load_manifest(path)
    assert ".json.bak" in str(excinfo.value)

    # Restoring the backup yields a loadable manifest again.
    path.write_text(
        path.with_suffix(".json.bak").read_text(encoding="utf-8"), encoding="utf-8"
    )
    restored = load_manifest(path)
    assert restored is not None
    assert restored.install_id == original.install_id
