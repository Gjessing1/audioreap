"""Tests for explicit/clean edition detection used by YouTube source scoring."""
import pytest

from service.providers.ytdlp import explicit_score


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Superman (Explicit)", 1),
        ("Lose Yourself - Dirty", 1),
        ("Some Song [Uncensored]", 1),
        ("Track (Uncut)", 1),
        ("Superman (Clean)", -1),
        ("Some Song (Radio Edit)", -1),
        ("Track [Radio Version]", -1),
        ("Censored Mix", -1),
        ("Edited Version", -1),
        ("Family Friendly Edit", -1),
        # Neutral — no marker either way
        ("Superman", 0),
        ("Paranoid Android", 0),
    ],
)
def test_explicit_score_keywords(title: str, expected: int) -> None:
    assert explicit_score(title) == expected


def test_age_limit_overrides_to_explicit() -> None:
    # age_limit >= 18 wins even when the title has no marker.
    assert explicit_score("Superman", age_limit=18) == 1


def test_no_false_positive_on_substrings() -> None:
    # "explicitly" / "cleaner" must not trip the word-boundaried patterns.
    assert explicit_score("Explicitly Yours") == 0
    assert explicit_score("Cleaner Than Before") == 0
