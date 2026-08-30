"""Unit tests for track_path() — canonical library path generation."""
import pytest
from pathlib import Path

from service.library.layout import track_path

ROOT = Path("/music")


def _p(**kw):
    defaults = dict(artist="Artist", album=None, year=None, track_number=None,
                    disc_number=None, title="Title", ext="flac", albumartist=None)
    defaults.update(kw)
    return track_path(ROOT, **defaults)


# ── Singles (no album) ────────────────────────────────────────────────────────

def test_single_no_album():
    p = _p(artist="Daft Punk", title="Around the World")
    assert p == ROOT / "Singles" / "Daft Punk" / "Around the World.flac"


def test_single_sanitizes_unsafe_chars():
    p = _p(artist='A/B:C', title='D<E>F')
    assert "/" not in str(p.name)
    assert ":" not in str(p.name)


# ── Albums ────────────────────────────────────────────────────────────────────

def test_album_with_year_and_track():
    p = _p(artist="Daft Punk", album="Homework", year=1997, track_number=1)
    assert p == ROOT / "Daft Punk" / "Homework (1997)" / "01 - Title.flac"


def test_album_no_year():
    p = _p(artist="Artist", album="Album", year=None, track_number=5)
    assert p == ROOT / "Artist" / "Album" / "05 - Title.flac"


def test_album_no_track_number():
    p = _p(artist="Artist", album="Album", year=2000)
    assert p == ROOT / "Artist" / "Album (2000)" / "Title.flac"


def test_album_uses_albumartist_over_artist():
    p = _p(artist="Track Artist", albumartist="Album Artist", album="Album", year=2000, track_number=1)
    assert p.parts[-3] == "Album Artist"


def test_disc_number_prefixes_track():
    p = _p(artist="Artist", album="Album", year=2000, track_number=3, disc_number=2)
    assert p.name.startswith("203 -")


def test_disc_number_1_not_prefixed():
    p = _p(artist="Artist", album="Album", year=2000, track_number=3, disc_number=1)
    assert p.name.startswith("03 -")


# ── Compilations ─────────────────────────────────────────────────────────────

def test_various_artists_goes_to_compilations():
    p = _p(albumartist="Various Artists", album="Now 100", year=2024, track_number=1,
           artist="Some Artist", title="Some Song")
    assert p.parts[-3] == "Compilations"


def test_compilation_filename_includes_artist():
    p = _p(albumartist="Various Artists", album="Comp", year=2000, track_number=2,
           artist="Guest", title="Song")
    assert "Guest" in p.name


def test_collapsed_compilation_filename_drops_the_repeated_album_artist():
    """With ARTIST = "Various Artists" the performer is already in the title —
    "01 - Various Artists - Silent Night (Mahalia Jackson)" only repeats the
    folder. See compilation_artist_mode in config.py."""
    p = _p(albumartist="Various Artists", album="Now 100", year=2018, track_number=1,
           artist="Various Artists", title="Silent Night (Mahalia Jackson)")
    assert p == (ROOT / "Compilations" / "Now 100 (2018)"
                 / "01 - Silent Night (Mahalia Jackson).flac")


def test_collapsed_compilation_without_track_number():
    p = _p(albumartist="Various Artists", album="Mix", year=2000,
           artist="various", title="Song")
    assert p.name == "Song.flac"


def test_various_lowercase_also_compilation():
    p = _p(albumartist="various", album="Mix", year=2000, track_number=1)
    assert "Compilations" in str(p)


# ── Edge cases ────────────────────────────────────────────────────────────────

def test_long_component_truncated():
    long = "A" * 300
    p = _p(artist=long, album="Album", year=2000)
    for part in p.parts:
        assert len(part) <= 185  # 180 + extension chars


def test_ext_leading_dot_stripped():
    p = _p(ext=".ogg")
    assert p.suffix == ".ogg"
    assert not p.name.endswith("..ogg")
