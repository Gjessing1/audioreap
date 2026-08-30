"""Regression tests for the previously-untested musicbrainz.py functions.

All musicbrainzngs calls are mocked — no network. The highest-value coverage
is _fetch_release_tracks (multi-disc numbering, cross-medium recording dedup,
video-medium filtering): it feeds the whole album pipeline, and a silent
regression there breaks album acquisition without any other test failing.
"""
from __future__ import annotations

from unittest.mock import patch

from service.metadata.musicbrainz import (
    _fetch_release_tracks,
    get_artist_release_groups,
    get_recording_by_id,
    get_release_group_genres,
    get_release_group_tracks,
    search_release_groups,
)


def _medium(fmt: str, tracks: list[dict]) -> dict:
    return {"format": fmt, "track-list": tracks}


def _track(pos: int, title: str, rid: str, length_ms: int = 200000) -> dict:
    return {
        "position": str(pos),
        "title": title,
        "length": str(length_ms),
        "recording": {"id": rid, "title": title, "length": str(length_ms)},
    }


# ── _fetch_release_tracks ──────────────────────────────────────────────────


def test_single_disc_tracks_have_no_disc_number() -> None:
    resp = {"release": {
        "title": "Homework",
        "date": "1997-01-20",
        "medium-list": [_medium("CD", [_track(2, "WDPK", "r2"), _track(1, "Daftendirekt", "r1")])],
    }}
    with patch("musicbrainzngs.get_release_by_id", return_value=resp):
        rel = _fetch_release_tracks("rel-1")
    title, rel_id, year, tracks = rel.album_title, rel.release_id, rel.year, rel.tracks

    assert title == "Homework"
    assert rel_id == "rel-1"
    assert year == 1997  # falls back to the release date when rg_year is None
    assert [t.title for t in tracks] == ["Daftendirekt", "WDPK"]  # sorted by position
    assert all(t.disc is None for t in tracks)  # single disc → no DISCNUMBER
    assert tracks[0].duration_seconds == 200
    assert tracks[0].recording_id == "r1"


def test_multi_disc_sorts_disc_major_and_numbers_discs() -> None:
    resp = {"release": {
        "title": "All Eyez on Me",
        "medium-list": [
            _medium("CD", [_track(1, "Ambitionz", "r1"), _track(2, "All Bout U", "r2")]),
            _medium("CD", [_track(1, "Can't C Me", "r3")]),
        ],
    }}
    with patch("musicbrainzngs.get_release_by_id", return_value=resp):
        tracks = _fetch_release_tracks("rel-2", rg_year=1996).tracks

    assert [(t.disc, t.number) for t in tracks] == [(1, 1), (1, 2), (2, 1)]


def test_video_medium_is_skipped_and_does_not_count_as_disc() -> None:
    # DualDisc: the DVD-Video side lists the same songs as the audio side —
    # without filtering, the tracklist doubles.
    resp = {"release": {
        "title": "Greatest Hits",
        "medium-list": [
            _medium("DVD-Video", [_track(1, "Come Out and Play", "v1")]),
            _medium("CD", [_track(1, "Come Out and Play", "r1")]),
        ],
    }}
    with patch("musicbrainzngs.get_release_by_id", return_value=resp):
        tracks = _fetch_release_tracks("rel-3").tracks

    assert len(tracks) == 1
    assert tracks[0].recording_id == "r1"
    assert tracks[0].disc is None  # only one *audio* disc → single-disc behaviour


def test_same_recording_on_two_media_deduped() -> None:
    resp = {"release": {
        "title": "Deluxe",
        "medium-list": [
            _medium("CD", [_track(1, "Song", "dup")]),
            _medium("CD", [_track(1, "Song", "dup"), _track(2, "Bonus", "r9")]),
        ],
    }}
    with patch("musicbrainzngs.get_release_by_id", return_value=resp):
        tracks = _fetch_release_tracks("rel-4").tracks

    assert [t.recording_id for t in tracks] == ["dup", "r9"]


def test_fetch_release_tracks_api_failure_returns_empty() -> None:
    with patch("musicbrainzngs.get_release_by_id", side_effect=Exception("boom")):
        rel = _fetch_release_tracks("rel-5", "Fallback", 2001)
    title, rel_id, year, tracks = rel.album_title, rel.release_id, rel.year, rel.tracks
    assert (title, rel_id, year, tracks) == ("Fallback", "rel-5", 2001, [])


# ── get_release_group_tracks ───────────────────────────────────────────────


def test_release_group_tracks_prefers_official_release() -> None:
    rg_resp = {"release-group": {
        "title": "OK Computer",
        "first-release-date": "1997-05-21",
        "release-list": [
            {"id": "rel-bootleg", "status": "Bootleg"},
            {"id": "rel-official", "status": "Official"},
            {"id": "rel-promo", "status": "Promotion"},
        ],
    }}
    rel_resp = {"release": {
        "title": "OK Computer",
        "medium-list": [_medium("CD", [_track(1, "Airbag", "r1")])],
    }}
    with patch("musicbrainzngs.get_release_group_by_id", return_value=rg_resp), \
         patch("musicbrainzngs.get_release_by_id", return_value=rel_resp) as mock_rel:
        rel = get_release_group_tracks("rg-1")
    title, rel_id, year, tracks = rel.album_title, rel.release_id, rel.year, rel.tracks

    assert title == "OK Computer"
    assert year == 1997  # from the release group's first-release-date
    assert rel_id == "rel-official"
    mock_rel.assert_called_once()
    assert mock_rel.call_args[0][0] == "rel-official"
    assert len(tracks) == 1


def test_release_group_tracks_no_releases() -> None:
    rg_resp = {"release-group": {"title": "Unreleased", "release-list": []}}
    with patch("musicbrainzngs.get_release_group_by_id", return_value=rg_resp):
        rel = get_release_group_tracks("rg-2")
    assert (rel.album_title, rel.release_id, rel.year, rel.tracks) == ("Unreleased", None, None, [])


def test_release_group_tracks_api_failure() -> None:
    with patch("musicbrainzngs.get_release_group_by_id", side_effect=Exception("boom")):
        rel = get_release_group_tracks("rg-3")
    assert (rel.album_title, rel.release_id, rel.year, rel.tracks) == ("Unknown Album", None, None, [])


# ── get_recording_by_id ────────────────────────────────────────────────────


def test_get_recording_by_id_parses_fields() -> None:
    resp = {"recording": {
        "id": "rec-1",
        "title": "Around the World",
        "artist-credit": [{"artist": {"id": "art-1", "name": "Daft Punk"}}],
        "release-list": [{
            "id": "rel-1",
            "title": "Homework",
            "date": "1997",
            "medium-list": [{"track-list": [{"position": "2", "number": "2"}]}],
        }],
    }}
    with patch("musicbrainzngs.get_recording_by_id", return_value=resp):
        rec = get_recording_by_id("rec-1")

    assert rec is not None
    assert rec.recording_id == "rec-1"
    assert rec.title == "Around the World"
    assert rec.artist == "Daft Punk"
    assert rec.album == "Homework"
    assert rec.year == 1997


def test_get_recording_by_id_failure_returns_none() -> None:
    with patch("musicbrainzngs.get_recording_by_id", side_effect=Exception("boom")):
        assert get_recording_by_id("rec-x") is None
    with patch("musicbrainzngs.get_recording_by_id", return_value={}):
        assert get_recording_by_id("rec-y") is None


# ── get_artist_release_groups ──────────────────────────────────────────────


def test_artist_release_groups_paginates_and_sorts() -> None:
    artist_resp = {"artist": {"name": "Radiohead"}}
    page1 = {
        "release-group-list": [
            {"id": "rg-b", "title": "The Bends", "type": "Album", "first-release-date": "1995-03-13"},
        ],
        "release-group-count": 2,
    }
    page2 = {
        "release-group-list": [
            {"id": "rg-a", "title": "Pablo Honey", "type": "Album", "first-release-date": "1993-02-22"},
        ],
        "release-group-count": 2,
    }
    with patch("musicbrainzngs.get_artist_by_id", return_value=artist_resp), \
         patch("musicbrainzngs.browse_release_groups", side_effect=[page1, page2]) as mock_browse:
        name, groups = get_artist_release_groups("artist-1")

    assert name == "Radiohead"
    assert mock_browse.call_count == 2  # count=2 forced a second page
    assert [g.title for g in groups] == ["Pablo Honey", "The Bends"]  # year-sorted
    assert groups[0].year == 1993 and groups[0].release_type == "Album"


def test_artist_release_groups_artist_fetch_failure() -> None:
    with patch("musicbrainzngs.get_artist_by_id", side_effect=Exception("boom")):
        assert get_artist_release_groups("artist-x") == ("Unknown Artist", [])


# ── get_release_group_genres ───────────────────────────────────────────────


def test_release_group_genres_sorted_by_votes_and_capped() -> None:
    resp = {"release-group": {"tag-list": [
        {"name": "rock", "count": "3"},
        {"name": "electronic", "count": "7"},
        {"name": "zero votes", "count": "0"},  # filtered out
        {"name": "idm", "count": "5"},
    ]}}
    with patch("musicbrainzngs.get_release_group_by_id", return_value=resp):
        assert get_release_group_genres("rg-1") == ["electronic", "idm", "rock"]
        assert get_release_group_genres("rg-1", max_genres=1) == ["electronic"]


def test_release_group_genres_failure_returns_empty() -> None:
    with patch("musicbrainzngs.get_release_group_by_id", side_effect=Exception("boom")):
        assert get_release_group_genres("rg-x") == []


# ── search_release_groups ──────────────────────────────────────────────────


def test_search_release_groups_returns_typed_matches() -> None:
    resp = {"release-group-list": [{
        "id": "rg-1",
        "title": "Discovery",
        "type": "Album",
        "first-release-date": "2001-03-12",
        "disambiguation": "remastered",
        "artist-credit": [{"artist": {"id": "a1", "name": "Daft Punk"}}],
    }]}
    with patch("musicbrainzngs.search_release_groups", return_value=resp):
        results = search_release_groups("Daft Punk", "Discovery")

    assert len(results) == 1
    rg = results[0]
    assert rg.release_group_id == "rg-1"
    assert rg.title == "Discovery"
    assert rg.artist == "Daft Punk"
    assert rg.release_type == "Album"
    assert rg.year == "2001"
    assert rg.disambiguation == "remastered"


def test_search_release_groups_failure_returns_empty() -> None:
    with patch("musicbrainzngs.search_release_groups", side_effect=Exception("boom")):
        assert search_release_groups("A", "B") == []
