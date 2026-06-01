"""Tests for the shared YouTube source scorer (score_yt_candidate).

This scorer is the single source of truth used by both the auto-picker
(yt_search_best) and the review card's ranked "Different source" pool
(yt_search_ranked) — so these assertions also pin the behaviour the review UI
surfaces, which is what makes weight-tuning safe.
"""
from service.providers.ytdlp import score_yt_candidate

_WANT = dict(artist="Adele", title="Hello", duration_seconds=295)


def _score(entry: dict) -> float:
    s, _ = score_yt_candidate(entry, **_WANT)
    return s


def test_official_topic_channel_flagged_and_boosted() -> None:
    score, is_official = score_yt_candidate(
        {"track": "Hello", "artist": "Adele", "channel": "Adele - Topic", "duration": 295},
        **_WANT,
    )
    assert is_official is True
    assert score > 0.9


def test_exact_official_beats_live_cover_and_wrong_artist() -> None:
    official = {"track": "Hello", "artist": "Adele", "channel": "AdeleVEVO", "duration": 295}
    live = {"title": "Hello (Live at the BRITs)", "channel": "AdeleVEVO", "duration": 320}
    cover = {"track": "Hello", "artist": "JFla", "channel": "JFlaMusic", "duration": 288}

    s_official = _score(official)
    assert s_official > _score(live)
    assert s_official > _score(cover)


def test_gross_duration_mismatch_is_penalised() -> None:
    # A 10-minute "Hello" (full-album rip / extended) is almost never the same cut.
    on_time = {"track": "Hello", "artist": "Adele", "channel": "Adele - Topic", "duration": 295}
    way_off = {"track": "Hello", "artist": "Adele", "channel": "Adele - Topic", "duration": 600}
    assert _score(on_time) > _score(way_off)


def test_wrong_artist_demoted_below_neutral_blank_artist() -> None:
    wrong_artist = {"track": "Hello", "artist": "Some Cover Band", "channel": "Some Cover Band", "duration": 295}
    blank_artist = {"title": "Hello", "duration": 295}  # no artist/channel exposed
    assert _score(blank_artist) > _score(wrong_artist)
