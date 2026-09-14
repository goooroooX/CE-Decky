from pathlib import Path

import pytest

from ce_decky.artifact_resolutions import ArtifactResolutions


def test_exact_artifact_resolution_survives_reload_and_keeps_revisions_distinct(tmp_path: Path):
    path = tmp_path / "resolutions.json"
    store = ArtifactResolutions(path)
    store.record("vgtimes", "game-a:file-1", "a" * 64, "b" * 64)
    store.record("vgtimes", "game-a:file-1", "c" * 64, "d" * 64)
    rows = ArtifactResolutions(path).snapshot()["entries"]
    assert [(row["artifact_sha256"], row["table_sha256"]) for row in rows] == [("a" * 64, "b" * 64), ("c" * 64, "d" * 64)]
    with pytest.raises(ValueError, match="conflicting"):
        store.record("vgtimes", "game-a:file-1", "a" * 64, "d" * 64)


def test_unreadable_resolution_cache_supplies_no_identity(tmp_path: Path):
    path = tmp_path / "resolutions.json"
    path.write_text('{"schema":1,"entries":[{}]}')
    snapshot = ArtifactResolutions(path).snapshot()
    assert snapshot["entries"] == []
    assert snapshot["reason"]
