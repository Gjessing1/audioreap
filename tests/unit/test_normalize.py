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


# ── extract_modifiers (Phase 2) ───────────────────────────────────────────────
from service.core.normalize import ModifierFlags, extract_modifiers


@pytest.mark.parametrize(
    "text, flag",
    [
        ("Creep (Live at Glastonbury)", "is_live"),
        ("Song - Unplugged", "is_live"),
        ("Karma Police (In Concert)", "is_live"),
        ("Get Lucky (Daft Punk Remix)", "is_remix"),
        ("Song [Remixed]", "is_remix"),
        ("Layla (Acoustic)", "is_acoustic"),
        ("Hurt (Johnny Cash Cover)", "is_cover"),
        ("Imagine - Tribute", "is_cover"),
        ("WAP [Explicit]", "is_explicit"),
        ("Bohemian Rhapsody (Karaoke Version)", "is_karaoke"),
        ("Comfortably Numb (Instrumental)", "is_instrumental"),
    ],
)
def test_extract_modifiers_detects(text: str, flag: str) -> None:
    flags = extract_modifiers(text)
    assert getattr(flags, flag) is True
    assert flags.any is True


@pytest.mark.parametrize(
    "text",
    [
        "Stayin' Alive",          # 'alive' must not trigger is_live
        "Discover",               # 'discover' must not trigger is_cover
        "Paranoid Android",       # plain studio title
        "",                       # empty
    ],
)
def test_extract_modifiers_no_false_positives(text: str) -> None:
    flags = extract_modifiers(text)
    assert flags == ModifierFlags()
    assert flags.any is False


def test_extract_modifiers_multiple() -> None:
    flags = extract_modifiers("Song (Live Acoustic) [Explicit]")
    assert flags.is_live and flags.is_acoustic and flags.is_explicit
    assert not flags.is_remix
