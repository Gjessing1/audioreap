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


# ── Real-world flat-search entry shapes ──────────────────────────────────────
# yt-dlp's flat ytsearch entries NEVER carry `track`/`artist`; titles follow the
# "Artist - Title" convention and the channel name is the only artist evidence
# ("Adele - Topic", "AdeleVEVO"). These shapes are what the auto-picker actually
# sees in production — the older tests above use the idealised full-extraction
# shape, which only the review panel might encounter.

# Verbatim from a live ytsearch6:"Adele Hello" (2026-06).
_REAL_POOL = {
    "official_video": {"title": "Adele - Hello (Official Music Video)", "channel": "Adele", "uploader": "Adele", "duration": 367},
    "lyrics_reupload": {"title": "Adele - Hello (Lyrics)", "channel": "Rare Vibes", "uploader": "Rare Vibes", "duration": 290},
    "live": {"title": "Adele - Hello (Live at the NRJ Awards)", "channel": "Adele", "uploader": "Adele", "duration": 308},
    "topic_audio": {"title": "Adele - Hello", "channel": "Adele - Topic", "uploader": "Adele - Topic", "duration": 296},
    "fan_live": {"title": "Adele - hello (live - in munich) (simulated DVD)", "channel": "Adele fasbrasil", "uploader": "Adele fasbrasil", "duration": 388},
}


def test_topic_audio_wins_the_real_world_pool() -> None:
    scores = {name: _score(e) for name, e in _REAL_POOL.items()}
    best = max(scores, key=scores.get)
    assert best == "topic_audio", scores


def test_topic_channel_without_artist_field_is_official_and_high() -> None:
    # The artist's own Topic channel must not trip the wrong-artist guard just
    # because its name ends in "- Topic".
    score, is_official = score_yt_candidate(_REAL_POOL["topic_audio"], **_WANT)
    assert is_official is True
    assert score > 0.9


def test_concatenated_vevo_channel_is_official_not_wrong_artist() -> None:
    entry = {"title": "Adele - Hello", "channel": "AdeleVEVO", "uploader": "AdeleVEVO", "duration": 296}
    score, is_official = score_yt_candidate(entry, **_WANT)
    assert is_official is True
    assert score > 0.9


def test_other_artists_topic_channel_gets_no_official_bonus() -> None:
    # A cover act's Topic channel must not inherit the official-audio bonus.
    cover_topic = {"title": "Hello", "channel": "JFla - Topic", "uploader": "JFla - Topic", "duration": 295}
    _, is_official = score_yt_candidate(cover_topic, **_WANT)
    assert is_official is False


def test_dash_title_parsing_lifts_title_match() -> None:
    # "Adele - Hello" should read as title="Hello" by artist="Adele", not as a
    # diluted token soup that halves the title similarity.
    dashed = {"title": "Adele - Hello", "channel": "Adele - Topic", "duration": 296}
    plain = {"title": "Hello", "channel": "Adele - Topic", "duration": 296}
    assert abs(_score(dashed) - _score(plain)) < 0.05


def test_bracketed_decorations_dont_sink_title_match() -> None:
    # MB titles are bare ("Derezzed"); YouTube adds "(From TRON: Legacy)" — that
    # decoration must not cost the candidate half its title similarity.
    want = dict(artist="Daft Punk", title="Derezzed", duration_seconds=104)
    decorated = {"title": "Daft Punk - Derezzed (From TRON: Legacy)", "channel": "Daft Punk - Topic", "duration": 104}
    bare = {"title": "Daft Punk - Derezzed", "channel": "Daft Punk - Topic", "duration": 104}
    s_dec, _ = score_yt_candidate(decorated, **want)
    s_bare, _ = score_yt_candidate(bare, **want)
    assert abs(s_dec - s_bare) < 0.05


def test_sped_up_and_nightcore_versions_heavily_penalised() -> None:
    plain = {"title": "Adele - Hello", "channel": "Adele - Topic", "duration": 296}
    sped = {"title": "Adele - Hello (sped up)", "channel": "Speedy Songs", "duration": 250}
    nightcore = {"title": "Hello (Nightcore)", "channel": "Nightcore Nation", "duration": 250}
    assert _score(plain) - _score(sped) > 0.4
    assert _score(plain) - _score(nightcore) > 0.4


def test_remix_penalised_unless_remix_is_wanted() -> None:
    remix = {"title": "Adele - Hello (Dave Audé Remix)", "channel": "remixes4u", "duration": 296}
    s_unwanted = _score(remix)
    s_wanted, _ = score_yt_candidate(
        remix, artist="Adele", title="Hello (Dave Audé Remix)", duration_seconds=296
    )
    assert s_wanted - s_unwanted > 0.4
