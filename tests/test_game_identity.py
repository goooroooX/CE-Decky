from __future__ import annotations

import pytest

from ce_decky.game_identity import (
    Candidate, MatchAction, aliases, decide_match, install_directory_title, match_breakdown, query_plan,
)


def decision(name, candidates, exe=None):
    return decide_match(aliases(name, exe), [Candidate(x, provider_trust=.8) for x in candidates])


def test_repo_punctuation_matches_compact_name():
    d = decision('R.E.P.O.', ['R.E.P.O.', 'Repo Man'])
    assert d.action == MatchAction.AUTO
    assert d.ranked[0].candidate.title == 'R.E.P.O.'


def test_elex_roman_matches_arabic():
    d = decision('ELEX II', ['Elex', 'Elex 2'])
    assert d.action == MatchAction.AUTO
    assert d.ranked[0].candidate.title == 'Elex 2'


def test_executable_alias_and_small_query_plan():
    q = query_plan('ELEX II - GOG', r'C:\Games\ELEX2.exe')
    assert q[0] == 'elex 2 gog'
    assert 'elex 2' in q
    assert len(q) <= 3


def test_bilingual_provider_title_matches_russian_alias():
    d = decision('Ведьмак 3: Дикая Охота', [
        'The Witcher 2 / Ведьмак 2: Убийцы королей',
        'The Witcher 3: Wild Hunt / Ведьмак 3: Дикая Охота',
    ])
    assert d.action == MatchAction.AUTO
    assert 'Witcher 3' in d.ranked[0].candidate.title


def test_gta_v_acronym_and_numeric_guard():
    d = decision('GTA V', ['Grand Theft Auto IV', 'Grand Theft Auto 5'])
    assert d.action == MatchAction.AUTO
    assert d.ranked[0].candidate.title == 'Grand Theft Auto 5'
    wrong = match_breakdown(aliases('Resident Evil 4'), Candidate('Resident Evil 3', .8))
    assert wrong.numeric_conflict and wrong.confidence <= .34


def test_release_year_and_strong_qualifier_require_evidence():
    d = decision('Resident Evil 4 (2023)', ['Resident Evil 4', 'Resident Evil 4 Remake'])
    assert d.action == MatchAction.ASK
    assert d.ranked[0].match.year_uncertain
    q = aliases('Resident Evil 4 (2023)')
    resolved = decide_match(q, [
        Candidate('Resident Evil 4', .8, release_year=2005),
        Candidate('Resident Evil 4 Remake', .8, release_year=2023),
    ])
    assert resolved.action == MatchAction.AUTO
    assert resolved.ranked[0].candidate.title == 'Resident Evil 4 Remake'


def test_a_candidate_with_no_scorable_identity_is_a_non_match_not_a_crash():
    # Live provider data contains it: one FearLess topic is titled exactly `.`,
    # which normalizes to nothing. Reaching the assertion raised out of the
    # whole provider job, so that one row cost every result the source would
    # have returned for any query the cheap prefilter did not narrow first.
    for title in ('.', '...', '-'):
        assert match_breakdown(aliases('Hidden Blade'), Candidate(title, .7)).confidence == float('-inf')


def test_request_and_table_version_do_not_defeat_identity_guards():
    q = aliases('Cyberpunk 2077')
    assert match_breakdown(q, Candidate('Cyberpunk 2077', .8)).confidence > match_breakdown(q, Candidate('[REQUEST] Cyberpunk 2077', 1.0)).confidence
    misleading = match_breakdown(aliases('Resident Evil 3'), Candidate('Resident Evil 4: Таблица для Cheat Engine [3.0] {author}', .9))
    assert misleading.numeric_conflict and misleading.confidence <= .34


def test_realistic_catalog_labels_stay_high_confidence():
    cases = [
        ('R.E.P.O.', 'R.E.P.O "Таблица для Cheat Engine +12" [UPD: 25.03.2025] {Wookie1337}'),
        ('ELEX II', 'Elex 2: Таблица для Cheat Engine [UPD: 06.03.2022] {Tuuuup!}'),
        ('Cyberpunk2077', 'Cyberpunk 2077 "Таблица +10 для Cheat Engine" [UPD: 20.07.2025] {sergey979}'),
        ('The Witcher 3 Complete Edition', 'The Witcher 3: Wild Hunt / Ведьмак 3: Дикая Охота - GOTY Edition: Таблица для Cheat Engine [1.32] {serjik979}'),
    ]
    for query, title in cases:
        match = match_breakdown(aliases(query), Candidate(title, .8))
        assert match.confidence >= .88
        assert not match.numeric_conflict


def test_identity_boundaries_fail_closed():
    with pytest.raises(ValueError, match='control'):
        aliases('bad\nname')
    with pytest.raises(ValueError, match='valid Unicode'):
        aliases('bad\ud800')
    with pytest.raises(ValueError, match='provider trust'):
        Candidate('Game', 1.1)
    with pytest.raises(ValueError, match='release year'):
        Candidate('Game', release_year=1800)
    with pytest.raises(ValueError, match='max_queries'):
        query_plan('Game', max_queries=0)
    with pytest.raises(ValueError, match='threshold'):
        decide_match(aliases('Game'), [Candidate('Game')], auto_threshold=.5, ask_threshold=.7)


def test_query_plan_rejects_blank_or_punctuation_only_identity():
    for value in ["", "   ", "---", "™®"]:
        with pytest.raises(ValueError, match="no searchable aliases"):
            query_plan(value)


def test_search_identity_is_the_library_name_not_the_executable():
    """Observed on the target for `Cadence: A Silent Requiem`.

    Its repack ships `Cadence.exe`, and the alias `cadence` scored the
    unrelated `Cadence of Fortune` at 0.808, above the provider floor, filling a
    search for a 2026 game with results for a 2010 one. Steam's own name for a
    library game is authoritative and a non-Steam entry's name is the user's to
    correct, so an executable never widens the search.
    """
    assert aliases(
        "Cadence: A Silent Requiem",
        '"/home/deck/Games/Cadence.A.Silent.Requiem-EMPRESS/Cadence.exe"',
    ) == ["cadence a silent requiem"]
    # The real installed path: its folder repeats the title, so nothing is added.
    assert aliases("Cobalt Ascent", '"/home/deck/Games/Cobalt.Ascent.Ultimate.Edition-RUNE/CobaltAscent.exe"') == [
        "cobalt ascent",
    ]
    assert query_plan("Cobalt Ascent", "CobaltAscent.exe") == ["cobalt ascent"]

    query = aliases("Cadence: A Silent Requiem", "Cadence.exe")
    assert match_breakdown(query, Candidate("Cadence of Fortune 4K HD Edition", .7)).confidence < 0.55
    assert match_breakdown(query, Candidate('Cadence: A Silent Requiem "Таблица"', .7)).confidence > 0.8


def test_search_identity_is_ranked_rather_than_merged():
    """The library name is the only identity while it exists.

    Merging a second source in would let a folder naming a different game - a
    mod installed into its base game's directory - outrank the entry the user
    chose. The install directory is a retry the search performs on results, and
    the executable is the last resort for an entry with no usable name at all.
    """
    assert aliases("SRO: Hidden Blade", '"/home/deck/Games/Shadow.Recon.Operative.Hidden.Blade.2025-InsaneRamZes/HiddenBladeDelta.exe"') == [
        "sro hidden blade",
    ]
    assert aliases("", '"/home/deck/Games/Neon Bazaar Kyoto/NBK.exe"') == ["neon bazaar kyoto"]
    assert aliases("", '"/NBK.exe"') == ["nbk"]


def test_the_install_directory_ignores_structure_and_packaging():
    """Only a folder that reads like a title becomes an alias."""
    # Build output, a library root and an emulator launcher directory say nothing.
    assert install_directory_title('"/home/deck/Games/SkyForge7/bin/SkyForge7.exe"') == "skyforge 7"
    assert install_directory_title('"/home/deck/Games/AmberTundra/bin64/AT.exe"') == "ambertundra"
    assert install_directory_title('"/home/deck/Emulation/tools/launchers/em-fe/em-fe.sh"') is None
    # Arguments after the launched program are never read, so an emulator
    # contributes its launcher directory rather than the ROM it was given.
    assert install_directory_title('"/home/deck/Emulation/tools/launchers/emu.sh" "Z:/roms/console/Ember.Fields.2.GOTY/game.iso"') is None
    # A release group and packaging words go; a number that is not a year stays.
    assert install_directory_title("/home/deck/Games/Protocol.7040-FLTDOX/Protocol7040.exe") == "protocol 7040"
    assert install_directory_title(
        '"/home/deck/Games/Lumen.Hollow.Voyage.12.Deluxe.Edition-InsaneRamZes/Voyage12_Steam.exe"'
    ) == "lumen hollow voyage 12"


def test_the_release_group_suffix_is_a_shape_rather_than_a_known_list():
    """No group is enumerated: the trailing `-Token` of a repack folder goes.

    Groups change constantly and a list would be stale the week after it was
    written, so the rule is structural. That also means a real title whose own
    last word follows a hyphen loses it, which is why the library entry's own
    name stays the primary identity and the directory is only a retry.
    """
    for group in ("InsaneRamZes", "FLTDOX", "RUNE", "EMPRESS", "P2P", "GOG", "Repack", "x64"):
        assert install_directory_title(
            f'"/home/deck/Games/Lumen.Hollow.Voyage.12-{group}/Voyage12_Steam.exe"'
        ) == "lumen hollow voyage 12"
    # A single trailing character is not a group suffix and is kept.
    assert install_directory_title('"/home/deck/Games/Amber.Tundra-Q/AT.exe"') == "amber tundra q"


def test_executable_is_the_last_resort_when_the_entry_has_no_usable_name():
    """A library entry with no name at all still has to be searchable."""
    assert aliases("", "ironveil.exe") == ["ironveil"]
    assert match_breakdown(aliases("", "ironveil.exe"), Candidate("Iron Veil", .7)).confidence > 0.55
    with pytest.raises(ValueError):
        query_plan("", None)
