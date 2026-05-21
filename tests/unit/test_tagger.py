"""Unit tests for parse_artists() in tagger.py."""
import pytest

from service.library.tagger import parse_artists


@pytest.mark.parametrize("input_str, expected", [
    # Single artist
    ("Daft Punk", ["Daft Punk"]),
    # feat. variants
    ("Artist feat. Guest", ["Artist", "Guest"]),
    ("Artist ft. Guest", ["Artist", "Guest"]),
    ("Artist featuring Guest", ["Artist", "Guest"]),
    # Ampersand collaboration
    ("A & B", ["A", "B"]),
    ("A &B", ["A", "B"]),
    # Comma-separated
    ("A, B, C", ["A", "B", "C"]),
    # feat. then ampersand in featured part
    ("Main feat. A & B", ["Main", "A", "B"]),
    # Primary with ampersand + feat.
    ("A & B feat. C", ["A", "B", "C"]),
    # Deduplication
    ("Artist feat. Artist", ["Artist"]),
    # Empty string
    ("", []),
    # Whitespace-only after strip
    ("  Artist  ", ["Artist"]),
    # No splitting needed for plain names
    ("Björk", ["Björk"]),
])
def test_parse_artists(input_str: str, expected: list[str]) -> None:
    result = parse_artists(input_str)
    assert result == expected, f"parse_artists({input_str!r}) = {result!r}, expected {expected!r}"


def test_single_artist_returns_list():
    result = parse_artists("Daft Punk")
    assert isinstance(result, list)
    assert len(result) == 1


def test_order_preserved():
    # Primary should come before featured
    result = parse_artists("Artist A feat. Artist B")
    assert result.index("Artist A") < result.index("Artist B")


def test_primary_ampersand_order():
    result = parse_artists("A & B")
    assert result[0] == "A"
    assert result[1] == "B"
