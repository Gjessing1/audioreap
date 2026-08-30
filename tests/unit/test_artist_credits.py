"""Artist-credit resolution, compilation detection and guest-credit placement.

The failure these guard against is library fragmentation: MusicBrainz hands back
an artist credit as an ARRAY joined by free text, and flattening it into the
ARTIST tag invents an artist that does not exist. Navidrome then lists that
invented artist separately from the real one, so one album shows up as several
artists. The rule under test: ARTIST gets the main artist alone, the guest moves
into the title, and the verbatim credit survives in ORIGINALARTIST.
"""
from __future__ import annotations

from service.library.tagger import title_with_guests
from service.metadata.musicbrainz import (
    VARIOUS_ARTISTS_MBID,
    MBTrack,
    is_various_artists_release,
    parse_artist_credit,
)


def _credit(*parts: object) -> list[object]:
    """Build a raw musicbrainzngs artist-credit list from names and join phrases."""
    out: list[object] = []
    for p in parts:
        if isinstance(p, str):
            out.append(p)
        else:
            name, mbid = p  # type: ignore[misc]
            out.append({"artist": {"id": mbid, "name": name, "sort-name": name}})
    return out


# ── parse_artist_credit ────────────────────────────────────────────────────


def test_single_artist_credit_has_no_guest() -> None:
    c = parse_artist_credit(_credit(("George Ezra", "a1")))
    assert (c.full, c.primary, c.primary_id) == ("George Ezra", "George Ezra", "a1")
    assert not c.has_guest
    assert c.guests == ""


def test_free_text_join_phrase_does_not_become_an_artist() -> None:
    """The Bjørn Eidsvåg case: "med" is not a name and not a separator we know."""
    c = parse_artist_credit(_credit(("Bjørn Eidsvåg", "a1"), " med ", ("Lisa Nilsson", "a2")))
    assert c.primary == "Bjørn Eidsvåg"
    assert c.primary_id == "a1"          # the main artist's identity, not the pair's
    assert c.full == "Bjørn Eidsvåg med Lisa Nilsson"
    assert c.guests == "Lisa Nilsson"    # the join phrase is dropped, not the name


def test_ampersand_and_feat_join_phrases() -> None:
    amp = parse_artist_credit(_credit(("Calvin Harris", "a1"), " & ", ("Dua Lipa", "a2")))
    assert (amp.primary, amp.guests) == ("Calvin Harris", "Dua Lipa")
    feat = parse_artist_credit(_credit(("Clean Bandit", "a1"), " feat. ", ("Demi Lovato", "a2")))
    assert (feat.primary, feat.guests) == ("Clean Bandit", "Demi Lovato")


def test_three_way_credit_keeps_all_guests() -> None:
    c = parse_artist_credit(
        _credit(("A", "a1"), ", ", ("B", "a2"), " & ", ("C", "a3"))
    )
    assert c.primary == "A"
    assert c.guests == "B & C"
    assert c.full == "A, B & C"


def test_joinphrase_as_dict_key_is_also_handled() -> None:
    """Some responses carry the phrase on the dict instead of as a bare string."""
    c = parse_artist_credit([
        {"artist": {"id": "a1", "name": "A"}, "joinphrase": " feat. "},
        {"artist": {"id": "a2", "name": "B"}},
    ])
    assert (c.full, c.primary, c.guests) == ("A feat. B", "A", "B")


def test_credited_as_name_wins_over_canonical_name() -> None:
    c = parse_artist_credit([{"artist": {"id": "a1", "name": "P!nk"}, "name": "Pink"}])
    assert c.primary == "Pink"
    assert c.primary_id == "a1"  # identity still tracks the real artist


def test_malformed_credit_degrades_instead_of_raising() -> None:
    for junk in (None, "", [], ["just a string"], [{"no": "artist"}], 42):
        c = parse_artist_credit(junk)
        assert c.primary == ""
        assert not c.has_guest


def test_various_artists_recognised_by_mbid_and_by_name() -> None:
    by_id = parse_artist_credit(_credit(("Verschiedene Interpreten", VARIOUS_ARTISTS_MBID)))
    assert by_id.is_various_artists  # localised name, canonical MBID
    by_name = parse_artist_credit(_credit(("Various Artists", "not-the-va-mbid")))
    assert by_name.is_various_artists
    assert not parse_artist_credit(_credit(("Various Cruelties", "a1"))).is_various_artists


# ── is_various_artists_release ─────────────────────────────────────────────


def _t(n: int, artist: str, artist_id: str | None) -> MBTrack:
    return MBTrack(
        number=n, title=f"Track {n}", duration_seconds=200,
        recording_id=f"r{n}", artist=artist, artist_id=artist_id,
    )


def test_release_credited_to_various_artists_is_a_compilation() -> None:
    credit = parse_artist_credit(_credit(("Various Artists", VARIOUS_ARTISTS_MBID)))
    assert is_various_artists_release(release_credit=credit, tracks=[])


def test_single_artist_album_is_not_a_compilation() -> None:
    credit = parse_artist_credit(_credit(("Radiohead", "rh")))
    tracks = [_t(i, "Radiohead", "rh") for i in range(1, 11)]
    assert not is_various_artists_release(release_credit=credit, tracks=tracks)


def test_greatest_hits_with_guest_spots_is_not_a_compilation() -> None:
    """MB types every best-of as "Compilation"; that must not file it under
    Various Artists. Only differing PERFORMERS count."""
    credit = parse_artist_credit(_credit(("Bjørn Eidsvåg", "be")))
    tracks = [_t(i, "Bjørn Eidsvåg", "be") for i in range(1, 15)]
    tracks[3] = _t(4, "Bjørn Eidsvåg", "be")  # "med Lisa Nilsson" — still him
    assert not is_various_artists_release(release_credit=credit, tracks=tracks)


def test_mostly_different_performers_is_a_compilation() -> None:
    credit = parse_artist_credit(_credit(("Some Label Act", "sl")))
    tracks = [_t(i, f"Artist {i}", f"a{i}") for i in range(1, 9)]
    assert is_various_artists_release(release_credit=credit, tracks=tracks)


def test_detection_is_by_mbid_not_by_spelling() -> None:
    """Same artist, differently spelled per track — not a compilation."""
    credit = parse_artist_credit(_credit(("The Beatles", "tb")))
    tracks = [_t(1, "Beatles, The", "tb"), _t(2, "THE BEATLES", "tb"), _t(3, "the beatles", "tb")]
    assert not is_various_artists_release(release_credit=credit, tracks=tracks)


def test_too_few_tracks_to_guess() -> None:
    """A 2-track single with a guest spot must not trip the ratio."""
    credit = parse_artist_credit(_credit(("Main", "m1")))
    tracks = [_t(1, "Main", "m1"), _t(2, "Guest", "g1")]
    assert not is_various_artists_release(release_credit=credit, tracks=tracks)


def test_blank_credits_do_not_vote() -> None:
    credit = parse_artist_credit(_credit(("Main", "m1")))
    tracks = [_t(i, "", None) for i in range(1, 6)]
    assert not is_various_artists_release(release_credit=credit, tracks=tracks)


# ── title_with_guests ──────────────────────────────────────────────────────


def test_guest_moves_into_the_title() -> None:
    assert title_with_guests("Eg ser", "Kringkastingsorkestret") == (
        "Eg ser (feat. Kringkastingsorkestret)"
    )


def test_title_suffix_is_idempotent() -> None:
    """Re-acquiring or re-tagging must not stack suffixes."""
    once = title_with_guests("One Kiss", "Dua Lipa")
    assert title_with_guests(once, "Dua Lipa") == once


def test_title_untouched_when_it_already_names_the_guest() -> None:
    assert title_with_guests("Solo (with Demi Lovato)", "Demi Lovato") == (
        "Solo (with Demi Lovato)"
    )


def test_no_guest_leaves_the_title_alone() -> None:
    assert title_with_guests("Shotgun", None) == "Shotgun"
    assert title_with_guests("Shotgun", "  ") == "Shotgun"
