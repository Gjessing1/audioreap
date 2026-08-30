"""Artist-credit resolution, compilation detection and guest-credit placement.

The failure these guard against is library fragmentation: MusicBrainz hands back
an artist credit as an ARRAY joined by free text, and flattening it into the
ARTIST tag invents an artist that does not exist. Navidrome then lists that
invented artist separately from the real one, so one album shows up as several
artists. The rule under test: ARTIST gets the main artist alone, the guest moves
into the title, and the verbatim credit survives in ORIGINALARTIST.
"""
from __future__ import annotations

import pytest

from service.acquisition.pipeline import _IdentifyState, _apply_credit_placement
from service.config import settings
from service.library.tagger import title_with_guests, title_with_performer
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


# ── title_with_performer ───────────────────────────────────────────────────


def test_performer_moves_into_the_title_without_a_feat() -> None:
    """A compilation performer is the act, not a guest on someone else's track."""
    assert title_with_performer("Silent Night", "Mahalia Jackson") == (
        "Silent Night (Mahalia Jackson)"
    )


def test_performer_suffix_is_idempotent() -> None:
    once = title_with_performer("Umbrella", "Rihanna")
    assert title_with_performer(once, "Rihanna") == once


def test_no_performer_leaves_the_title_alone() -> None:
    assert title_with_performer("Shotgun", None) == "Shotgun"
    assert title_with_performer("Shotgun", "  ") == "Shotgun"


# ── compilation credit placement ───────────────────────────────────────────
#
# The other half of the fragmentation problem: on a various-artists compilation
# every track has a different performer, so leaving each one in ARTIST turns one
# 20-track album into 20 one-track artists in Navidrome.


def _state(**kw: object) -> _IdentifyState:
    """An identification state as it reaches the credit-placement step."""
    base: dict[str, object] = dict(
        title="Silent Night", artist="Mahalia Jackson", album="Now 100",
        year=2018, track_number=1, disc_number=None, duration=180,
        prov_title="mb", prov_artist="mb", prov_album="mb", prov_year="mb",
        prov_recording="mb", album_locked=True, mb_artist_id="performer-mbid",
    )
    base.update(kw)
    return _IdentifyState(**base)  # type: ignore[arg-type]


@pytest.fixture
def comp_mode(monkeypatch: pytest.MonkeyPatch):
    """Set the compilation ARTIST policy for one test."""
    def _set(mode: str) -> None:
        monkeypatch.setattr(settings, "compilation_artist_mode", mode)
    return _set


def test_compilation_performer_collapses_into_the_title(comp_mode) -> None:
    comp_mode("append_to_title")
    state = _state()
    _apply_credit_placement(state, "Various Artists", is_compilation=True)
    assert state.title == "Silent Night (Mahalia Jackson)"
    assert state.artist == "Various Artists"
    assert state.original_artist == "Mahalia Jackson"
    assert state.artist_collapsed


def test_collapsed_artist_never_keeps_the_performers_mbid(comp_mode) -> None:
    """Navidrome keys identity on the MBID, so it must not outlive the name."""
    comp_mode("append_to_title")
    state = _state()
    _apply_credit_placement(state, "Various Artists", is_compilation=True)
    assert state.mb_artist_id is None


def test_album_artist_mode_leaves_the_title_alone(comp_mode) -> None:
    comp_mode("album_artist")
    state = _state()
    _apply_credit_placement(state, "Various Artists", is_compilation=True)
    assert state.title == "Silent Night"
    assert state.artist == "Various Artists"
    assert state.original_artist == "Mahalia Jackson"  # still reversible


def test_keep_mode_is_the_old_behaviour(comp_mode) -> None:
    comp_mode("keep")
    state = _state()
    _apply_credit_placement(state, "Various Artists", is_compilation=True)
    assert (state.title, state.artist) == ("Silent Night", "Mahalia Jackson")
    assert state.mb_artist_id == "performer-mbid"
    assert not state.artist_collapsed


def test_unknown_mode_falls_back_to_the_shipped_default(comp_mode) -> None:
    """A typo in the env var must not silently pick a different policy."""
    comp_mode("appendToTitle!")
    state = _state()
    _apply_credit_placement(state, "Various Artists", is_compilation=True)
    assert state.title == "Silent Night (Mahalia Jackson)"


def test_regular_album_track_is_untouched(comp_mode) -> None:
    comp_mode("append_to_title")
    state = _state(title="Eg ser", artist="Bjørn Eidsvåg")
    _apply_credit_placement(state, "Bjørn Eidsvåg", is_compilation=False)
    assert (state.title, state.artist) == ("Eg ser", "Bjørn Eidsvåg")
    assert state.mb_artist_id == "performer-mbid"


def test_performer_already_the_album_artist_is_untouched(comp_mode) -> None:
    """A track credited to Various Artists itself has nothing to collapse."""
    comp_mode("append_to_title")
    state = _state(artist="Various Artists")
    _apply_credit_placement(state, "Various Artists", is_compilation=True)
    assert state.title == "Silent Night"
    assert not state.artist_collapsed


def test_compilation_title_carries_the_whole_credit_including_guests(comp_mode) -> None:
    """The full credit goes in, so the guest step then finds it and adds nothing."""
    comp_mode("append_to_title")
    state = _state(
        title="Eg ser", artist="Bjørn Eidsvåg",
        artist_credit="Bjørn Eidsvåg med Lisa Nilsson", artist_guests="Lisa Nilsson",
    )
    _apply_credit_placement(state, "Various Artists", is_compilation=True)
    assert state.title == "Eg ser (Bjørn Eidsvåg med Lisa Nilsson)"
    assert state.original_artist == "Bjørn Eidsvåg med Lisa Nilsson"


def test_guest_placement_still_applies_off_a_compilation(comp_mode) -> None:
    comp_mode("append_to_title")
    state = _state(
        title="Eg ser", artist="Bjørn Eidsvåg",
        artist_credit="Bjørn Eidsvåg med Lisa Nilsson", artist_guests="Lisa Nilsson",
    )
    _apply_credit_placement(state, "Bjørn Eidsvåg", is_compilation=False)
    assert state.title == "Eg ser (feat. Lisa Nilsson)"
    assert state.artist == "Bjørn Eidsvåg"
    assert state.original_artist == "Bjørn Eidsvåg med Lisa Nilsson"
