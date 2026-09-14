"""Where the provider surveys get the Steam user's paths from.

`DECKY_USER_HOME` owns the Steam library, the provider cache and the index
these helpers read. The account a developer helper runs as is not that home,
and where the two differ a survey of the wrong library returns an empty answer
that reads exactly like an empty answer from the right one. So the helpers
refuse rather than guess, and they ask only for the paths the selected mode
will actually open.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts import provider_corpus_survey as corpus
from scripts import provider_search_survey as search
from scripts import steam_user


@pytest.mark.parametrize("module", [corpus, search])
def test_no_home_anywhere_is_refused_rather_than_guessed(module, monkeypatch):
    monkeypatch.delenv("DECKY_USER_HOME", raising=False)
    with pytest.raises(SystemExit) as refusal:
        module.steam_user_home(None)
    assert "user_home" in str(refusal.value)


@pytest.mark.parametrize("module", [corpus, search])
def test_the_environment_answers_when_no_flag_does(module, monkeypatch, tmp_path: Path):
    monkeypatch.setenv("DECKY_USER_HOME", str(tmp_path))
    assert module.steam_user_home(None) == tmp_path


@pytest.mark.parametrize("module", [corpus, search])
def test_an_explicit_home_wins_over_the_environment(module, monkeypatch, tmp_path: Path):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("DECKY_USER_HOME", str(tmp_path))
    assert module.steam_user_home(str(other)) == other


@pytest.mark.parametrize("module", [corpus, search])
@pytest.mark.parametrize("bad", ["relative/home", "/does/not/exist/at/all", ""])
def test_a_home_that_is_not_an_existing_absolute_path_is_refused(module, bad, monkeypatch):
    monkeypatch.delenv("DECKY_USER_HOME", raising=False)
    with pytest.raises(SystemExit):
        module.steam_user_home(bad or None)


def test_summarising_the_local_corpus_needs_no_home(monkeypatch, tmp_path: Path):
    # It reads only what is already on this disk, so being asked which home
    # holds the plugin's index would refuse a run that never opens it.
    monkeypatch.delenv("DECKY_USER_HOME", raising=False)
    monkeypatch.setattr(corpus, "steam_user_home", lambda _explicit: pytest.fail("resolved a home it does not need"))
    seen: dict[str, object] = {}
    monkeypatch.setattr(corpus.asyncio, "run", lambda coroutine: seen.setdefault("ran", coroutine.close()) or 0)
    assert corpus.main(["--summarise-only", "--corpus", str(tmp_path)]) == 0
    assert "ran" in seen


def test_collecting_a_corpus_still_asks_for_the_home(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("DECKY_USER_HOME", raising=False)
    with pytest.raises(SystemExit):
        corpus.main(["--corpus", str(tmp_path)])


def test_listing_the_library_needs_only_the_userdata_it_was_given(monkeypatch, tmp_path: Path, capsys):
    # The cache is copied by a search and by nothing else, so listing must not
    # be refused for having no home to derive a path it will never open.
    monkeypatch.delenv("DECKY_USER_HOME", raising=False)
    monkeypatch.setattr(search, "steam_user_home", lambda _explicit: pytest.fail("resolved a home it does not need"))
    monkeypatch.setattr(search, "read_library", lambda _userdata: [{"name": "A game", "executable": "/games/a.exe"}])
    assert search.main(["--list", "--userdata", str(tmp_path)]) == 0
    assert "A game" in capsys.readouterr().out


def test_a_real_search_still_asks_for_the_home_the_cache_lives_in(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("DECKY_USER_HOME", raising=False)
    monkeypatch.setattr(search, "read_library", lambda _userdata: [{"name": "A game", "executable": "/games/a.exe"}])
    with pytest.raises(SystemExit):
        search.main(["--userdata", str(tmp_path)])


def test_both_helpers_refuse_through_one_implementation():
    """One copy, because it is a refusal rather than a computation.

    The two surveys carried this function verbatim, docstring included, and the
    cases above ran the same assertions against each of them: a rule about an
    authority, stated twice, drifts apart quietly and the tests that prove it
    pass either way.
    """
    assert corpus.steam_user_home is steam_user.steam_user_home
    assert search.steam_user_home is steam_user.steam_user_home
