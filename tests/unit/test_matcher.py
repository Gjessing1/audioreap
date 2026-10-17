"""Unit tests for the fuzzy track matcher."""
import pytest

from service.search.matcher import DEDUP_THRESHOLD, is_confident_match, track_similarity


@pytest.mark.parametrize(
    "title_a, artist_a, dur_a, title_b, artist_b, dur_b, expect_match",
    [
        # Exact match
        ("Around the World", "Daft Punk", 429, "Around the World", "Daft Punk", 429, True),
        # feat. stripped — should match
        ("Song (feat. Artist B)", "Artist A", 240, "Song", "Artist A", 240, True),
        # [Official Video] stripped — should match
        ("Song [Official Video]", "Artist", 180, "Song", "Artist", 180, True),
        # (Remastered) stripped — should match
        ("Song (Remastered)", "Artist", 200, "Song", "Artist", 200, True),
        # Live version — should NOT match (live not stripped)
        ("Song (Live)", "Artist", 300, "Song", "Artist", 240, False),
        # Remix — should NOT match
        ("Song (Radio Edit)", "Artist", 180, "Song (Club Mix)", "Artist", 420, False),
        # Different artists — should NOT match
        ("Same Title", "Artist A", 200, "Same Title", "Artist B", 200, False),
        # Different titles entirely — should NOT match
        ("Song A", "Artist", 200, "Song B", "Artist", 200, False),
        # Duration far off — should NOT be confident match
        ("Around the World", "Daft Punk", 100, "Around the World", "Daft Punk", 429, False),
        # Diacritics stripped — should match
        ("Jóga", "Björk", 300, "Joga", "Bjork", 300, True),
        # Case difference — should match
        ("SONG", "ARTIST", 200, "song", "artist", 200, True),
        # Unknown duration on one side — still matches on title+artist
        ("Song", "Artist", None, "Song", "Artist", 200, True),
    ],
)
def test_match_table(
    title_a: str, artist_a: str, dur_a: int | None,
    title_b: str, artist_b: str, dur_b: int | None,
    expect_match: bool,
) -> None:
    result = is_confident_match(title_a, artist_a, dur_a, title_b, artist_b, dur_b)
    assert result == expect_match, (
        f"Expected {'match' if expect_match else 'no match'} for "
        f"{title_a!r}/{artist_a!r} vs {title_b!r}/{artist_b!r}"
    )


def test_similarity_is_symmetric() -> None:
    s1 = track_similarity("Song A", "Artist", 200, "Song B", "Artist", 200)
    s2 = track_similarity("Song B", "Artist", 200, "Song A", "Artist", 200)
    assert abs(s1 - s2) < 0.001


def test_identical_tracks_score_one() -> None:
    score = track_similarity("Song", "Artist", 200, "Song", "Artist", 200)
    assert score >= DEDUP_THRESHOLD


def test_completely_different_score_near_zero() -> None:
    score = track_similarity("Alpha", "Artist X", 100, "Zeta", "Artist Y", 500)
    assert score < 0.3


@pytest.mark.parametrize(
    "a, b, same",
    [
        # MB's short title for the same recording
        ("The Losing End (When You're On)", "The Losing End", True),
        # Identical title plus a mix qualifier
        ("Words (Between the Lines of Age)", "Words (Between the Lines of Age) (5.1 mix)", True),
        ("Help!", "Help! - Remastered 2009", True),
        ("Help!", "Help! (2009 Remaster)", True),
        ("Song (Mono)", "Song (Stereo)", True),
        ("Song (Single Version)", "Song", True),
        ("Live and Let Die", "Live and Let Die (2018 Remaster)", True),
        # A different recording, not a different version note
        ("Song (Live)", "Song", False),
        ("Song (Remix)", "Song", False),
        ("Song (Radio Edit)", "Song (Club Mix)", False),
        # A different track of the same name
        ("Song (Part 2)", "Song", False),
        ("Song (Reprise)", "Song", False),
        ("Song (A)", "Song (B)", False),
        # Containment is not a prefix match on words
        ("Love", "Love Me Do", False),
        ("Song A", "Song B", False),
    ],
)
def test_same_song_title(a: str, b: str, same: bool) -> None:
    from service.search.matcher import same_song_title

    assert same_song_title(a, b) is same
    assert same_song_title(b, a) is same
