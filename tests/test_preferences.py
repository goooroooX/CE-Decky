from pathlib import Path
import json

import pytest

from ce_decky.preferences import PreferenceStore, Preferences


def test_preferences_default_to_on_and_survive_a_round_trip(tmp_path: Path):
    store = PreferenceStore(tmp_path / "preferences.json")
    assert store.load() == Preferences(update_auto_check=True, mascot_visible=True)
    store.set(mascot_visible=False)
    assert store.load().mascot_visible is False
    # One choice at a time: setting one keeps the other.
    assert store.load().update_auto_check is True
    store.set(update_auto_check=False)
    assert store.load() == Preferences(update_auto_check=False, mascot_visible=False)


def test_a_preference_file_a_later_version_wrote_still_reads_here(tmp_path: Path):
    """The whole reason these left the configuration.

    That file is parsed strictly, so a key an older build does not know costs it
    the file and the registered Cheat Engine with it. This one is the opposite:
    a key from the future is ignored and everything known is still answered.
    """
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({
        "schema": 1, "mascot_visible": False, "something_a_later_version_added": "whatever",
    }), encoding="utf-8")
    assert PreferenceStore(path).load() == Preferences(update_auto_check=True, mascot_visible=False)


def test_an_unreadable_preference_file_is_the_defaults_rather_than_a_refusal(tmp_path: Path):
    path = tmp_path / "preferences.json"
    for damage in ("{ not json", json.dumps([1, 2]), json.dumps({"mascot_visible": "sometimes"})):
        path.write_text(damage, encoding="utf-8")
        assert PreferenceStore(path).load() == Preferences()


def test_a_preference_this_does_not_have_is_refused_rather_than_stored(tmp_path: Path):
    store = PreferenceStore(tmp_path / "preferences.json")
    with pytest.raises(ValueError, match="unknown preference"):
        store.set(colour_scheme=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="on or off"):
        store.set(mascot_visible="yes")  # type: ignore[arg-type]
    assert not (tmp_path / "preferences.json").exists()
