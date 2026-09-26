"""Tests for reducing a MusicBrainz track title to something searchable.

MB titles are catalogue text. On a score compilation they name the work each cue
came from, and a CD's final track can hold several works in one entry — neither
of which any YouTube uploader has ever typed. `yt_search_best` builds its query
from this, so the raw title was sending searches after strings that cannot match.

Real source: Nick Cave & Warren Ellis, *White Lunar* (MB release 83125eb1).
"""
from service.providers.ytdlp import is_compound_title, normalize_search_title


def test_strips_work_annotation() -> None:
    assert normalize_search_title(
        "Rat’s Tooth Forceps (The English Surgeon)"
    ) == "Rat’s Tooth Forceps"
    assert normalize_search_title(
        "Song for Jesse (The Assassination of Jesse James by the Coward Robert Ford)"
    ) == "Song for Jesse"
    assert normalize_search_title("Window (The Girls of Phnom Penh)") == "Window"


def test_keeps_a_parenthetical_that_is_part_of_the_name() -> None:
    """Only the trailing group goes — "(Suture)" is the song's own name."""
    assert normalize_search_title(
        "Black Silk (Suture) (The English Surgeon)"
    ) == "Black Silk (Suture)"


def test_keeps_version_qualifiers() -> None:
    """These pick one upload over another, so the query needs them."""
    for title in (
        "Long Black Veil (2009 Remaster)",
        "Words (Between the Lines of Age) (5.1 mix)",
        "Mysteriet deg (feat. Lisa Nilsson)",
        "California Love (remix)",
        "Smash (acoustic reprise)",
    ):
        assert normalize_search_title(title) == title


def test_compound_entry_reduced_to_its_first_work() -> None:
    title = "Sorya Market (The Girls of Phnom Penh) / [silence] / [unknown]"
    assert is_compound_title(title) is True
    assert normalize_search_title(title) == "Sorya Market"


def test_bare_titles_are_untouched() -> None:
    for title in ("Halo", "Zanstra", "Daedalus", "Magma"):
        assert normalize_search_title(title) == title
        assert is_compound_title(title) is False


def test_never_returns_empty() -> None:
    """A title that is nothing but an annotation must still be searchable."""
    assert normalize_search_title("(The Proposition)") == "(The Proposition)"
    assert normalize_search_title("   ") == ""
