"""The durable record of which table sources this user switched off."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ce_decky.provider_sources import MAX_DISABLED_PROVIDERS, ProviderSourceSelection


def _selection(tmp_path: Path) -> ProviderSourceSelection:
    return ProviderSourceSelection(tmp_path / "provider_sources.json")


def test_every_source_is_on_before_anything_is_recorded(tmp_path: Path):
    store = _selection(tmp_path)
    assert store.disabled() == frozenset()
    assert store.is_enabled("github") is True
    assert store.snapshot() == {
        "schema": 1, "disabled": [], "updated_at": None, "reason": None,
    }


def test_switching_one_source_off_records_only_that_refusal(tmp_path: Path):
    store = _selection(tmp_path)
    assert store.set_enabled("github", False) is True
    assert store.set_enabled("github", False) is False
    assert store.disabled() == frozenset({"github"})
    assert store.is_enabled("github") is False
    assert store.is_enabled("fearless") is True


def test_switching_a_source_back_on_removes_its_entry_rather_than_approving_it(tmp_path: Path):
    # The file holds refusals and nothing else, which is what lets a provider
    # added by a later version be on for everyone who never had an opinion
    # about it. An approval recorded here would invert that.
    store = _selection(tmp_path)
    store.set_enabled("vgtimes", False)
    assert store.set_enabled("vgtimes", True) is True
    assert store.set_enabled("vgtimes", True) is False
    assert json.loads((tmp_path / "provider_sources.json").read_text())["disabled"] == []


def test_the_record_survives_a_reload_and_is_written_sorted(tmp_path: Path):
    store = _selection(tmp_path)
    store.set_enabled("vgtimes", False)
    store.set_enabled("fearless", False)
    payload = json.loads((tmp_path / "provider_sources.json").read_text())
    assert payload["disabled"] == ["fearless", "vgtimes"]
    assert isinstance(payload["updated_at"], int)
    assert _selection(tmp_path).disabled() == frozenset({"fearless", "vgtimes"})


def test_a_provider_id_that_is_not_one_is_refused(tmp_path: Path):
    store = _selection(tmp_path)
    with pytest.raises(ValueError):
        store.set_enabled("../etc", False)
    with pytest.raises(ValueError):
        store.set_enabled("github", "no")  # type: ignore[arg-type]


def test_a_corrupt_record_raises_for_a_reader_and_is_repaired_by_clearing(tmp_path: Path):
    path = tmp_path / "provider_sources.json"
    path.write_text(json.dumps({"schema": 9, "disabled": []}), encoding="utf-8")
    store = ProviderSourceSelection(path)
    with pytest.raises(ValueError):
        store.disabled()
    snapshot = store.snapshot()
    assert snapshot["disabled"] == [] and snapshot["reason"]
    # Clearing is the repair primitive: it must not depend on reading the very
    # file that cannot be read.
    assert store.clear() == 0
    assert store.disabled() == frozenset()


def test_clearing_reports_how_many_refusals_it_removed(tmp_path: Path):
    store = _selection(tmp_path)
    store.set_enabled("github", False)
    store.set_enabled("vgtimes", False)
    assert store.clear() == 2
    assert store.clear() == 0


def test_the_record_is_bounded(tmp_path: Path):
    path = tmp_path / "provider_sources.json"
    path.write_text(
        json.dumps({
            "schema": 1,
            "disabled": [f"p{index}" for index in range(MAX_DISABLED_PROVIDERS + 1)],
            "updated_at": 1,
        }),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        ProviderSourceSelection(path).disabled()


def test_a_duplicate_entry_is_a_corrupt_record(tmp_path: Path):
    path = tmp_path / "provider_sources.json"
    path.write_text(
        json.dumps({"schema": 1, "disabled": ["github", "github"], "updated_at": 1}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        ProviderSourceSelection(path).disabled()


def test_an_unknown_key_is_a_corrupt_record(tmp_path: Path):
    path = tmp_path / "provider_sources.json"
    path.write_text(
        json.dumps({"schema": 1, "disabled": [], "updated_at": 1, "enabled": ["github"]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        ProviderSourceSelection(path).disabled()


def test_a_provider_absent_from_the_registry_keeps_its_refusal(tmp_path: Path):
    # A source removed for one version and restored in the next must not come
    # back on by itself. The store validates the ID grammar, not the registry.
    path = tmp_path / "provider_sources.json"
    path.write_text(
        json.dumps({"schema": 1, "disabled": ["opencheattables"], "updated_at": 1}),
        encoding="utf-8",
    )
    assert ProviderSourceSelection(path).disabled() == frozenset({"opencheattables"})
