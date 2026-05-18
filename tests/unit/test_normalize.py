import pytest

from service.core.normalize import normalize, strip_diacritics


@pytest.mark.parametrize(
    "input_text, expected",
    [
        # Official video variants
        ("Song [Official Video]", "song"),
        ("Song (Official Video)", "song"),
        ("Song [Official Music Video]", "song"),
        ("Song (Official Music Video)", "song"),
        ("Song [Official Audio]", "song"),
        ("Song [Official Lyric Video]", "song"),
        # Quality tags
        ("Song [HD]", "song"),
        ("Song (HD)", "song"),
        ("Song [HQ]", "song"),
        ("Song [4K]", "song"),
        # Remaster variants
        ("Song (Remastered)", "song"),
        ("Song (Remastered 2011)", "song"),
        ("Song [Remastered]", "song"),
        # Explicit/clean
        ("Song (Explicit)", "song"),
        ("Song [Clean]", "song"),
        # Featuring variants
        ("Song (feat. Other Artist)", "song"),
        ("Song (ft. Other Artist)", "song"),
        ("Song (featuring Other Artist)", "song"),
        ("Song (with Other Artist)", "song"),
        ("Song [feat. Other Artist]", "song"),
        # Production credit
        ("Song (prod. DJ Khaled)", "song"),
        # Lyrics tags
        ("Song (Lyrics)", "song"),
        ("Song [Lyrics]", "song"),
        ("Song (Lyric)", "song"),
        # Audio/visualizer
        ("Song (Audio)", "song"),
        ("Song [Audio]", "song"),
        ("Song (Visualizer)", "song"),
        # Diacritics
        ("Ñoño", "nono"),
        ("Björk", "bjork"),
        ("Café del Mar", "cafe del mar"),
        # Case
        ("ARTIST NAME", "artist name"),
        ("Artist Name", "artist name"),
        # Whitespace
        ("  Song  ", "song"),
        ("Song   Name", "song name"),
        # Multiple noise patterns
        ("Song (feat. X) [Official Video] (HD)", "song"),
        # Clean title passes through unchanged
        ("Around the World", "around the world"),
        ("One More Time", "one more time"),
    ],
)
def test_normalize(input_text: str, expected: str) -> None:
    assert normalize(input_text) == expected


def test_strip_diacritics_preserves_ascii() -> None:
    assert strip_diacritics("hello world") == "hello world"


def test_normalize_empty_string() -> None:
    assert normalize("") == ""
