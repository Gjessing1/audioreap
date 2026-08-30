"""Round-trip contract tests for tagger.read_tags / write_tags.

One parametrized test per container format (mp3 / flac / ogg / opus / m4a):
write the full normalized field set, read it back, and assert every field
survives. This pins the per-format dispatch branches (ID3 / Vorbis / MP4)
to one shared contract — the branches have already diverged once
(MP4 write_tags silently dropped disc_number until 2026-07).

Audio fixtures are generated from tests/fixtures/audio/tone_1s.wav with
ffmpeg at test time. Only the tests that generate fixtures require ffmpeg;
the format-independent read tests still run on hosts without it.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from service.library.tagger import _get_field, _tag_format, read_tags, write_tags


def _read_adapter_field(path: Path, field: str) -> str | None:
    """Read one normalized field straight through the container adapter.

    Fields that TaggedFile does not expose still have to survive the per-format
    write branch, which is where MP4/ID3/Vorbis have diverged before.
    """
    from mutagen import File as MutagenFile

    audio = MutagenFile(path)
    assert audio is not None
    return _get_field(_tag_format(audio, audio.tags), audio.tags, field)

FIXTURE_WAV = Path(__file__).parent.parent / "fixtures" / "audio" / "tone_1s.wav"

# (extension, ffmpeg codec args) — remux/encode the 1s tone into each container.
FORMATS = {
    "mp3": ["-c:a", "libmp3lame", "-b:a", "64k"],
    "flac": ["-c:a", "flac"],
    "ogg": ["-c:a", "libvorbis"],
    "opus": ["-c:a", "libopus"],
    "m4a": ["-c:a", "aac", "-b:a", "64k"],
}

# Minimal valid JPEG (SOI + EOI with a JFIF header) — tagger embeds bytes
# verbatim without validating image content.
TINY_JPEG = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffd9")

TAGS = {
    "title": "Round Trip",
    "artist": "Tagger Test & Friends",
    "albumartist": "Tagger Test",
    "album": "Contract Album",
    "year": 2021,
    "track_number": 7,
    "disc_number": 2,
    "genre": "Electronic",
    "artist_sort": "Tagger Test, The",
    "mb_recording_id": "11111111-2222-3333-4444-555555555555",
    "mb_release_id": "66666666-7777-8888-9999-aaaaaaaaaaaa",
    "mb_artist_id": "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
}

# Written by every format but not surfaced on TaggedFile — read back through the
# container adapter instead. `original_artist` records the credit that ARTIST was
# collapsed out of; `mb_albumartist_id` is the album artist's own identity, which
# Navidrome keys album grouping on.
ADAPTER_ONLY_TAGS = {
    "original_artist": "Tagger Test med Guest Performer",
    "mb_albumartist_id": "cccccccc-dddd-eeee-ffff-000000000000",
}


@pytest.fixture(scope="module")
def format_files(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """One pristine (untagged) audio file per container format."""
    base = tmp_path_factory.mktemp("tagger_roundtrip")
    files: dict[str, Path] = {}
    for ext, codec_args in FORMATS.items():
        out = base / f"tone.{ext}"
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", str(FIXTURE_WAV), *codec_args, str(out)],
            capture_output=True,
            timeout=60,
        )
        if proc.returncode == 0 and out.exists():
            files[ext] = out
    return files


@pytest.mark.parametrize("ext", list(FORMATS))
@pytest.mark.requires_ffmpeg
def test_write_then_read_round_trips_every_field(
    ext: str, format_files: dict[str, Path], tmp_path: Path
) -> None:
    if ext not in format_files:
        pytest.skip(f"ffmpeg could not produce a .{ext} file")
    path = tmp_path / f"track.{ext}"
    shutil.copy(format_files[ext], path)

    write_tags(path, **TAGS, **ADAPTER_ONLY_TAGS, compilation=True, artwork_bytes=TINY_JPEG)
    tagged = read_tags(path)

    assert tagged is not None, f"read_tags returned None for {ext}"
    assert tagged.title == TAGS["title"]
    assert tagged.artist == TAGS["artist"]
    assert tagged.albumartist == TAGS["albumartist"]
    assert tagged.album == TAGS["album"]
    assert tagged.year == TAGS["year"]
    assert tagged.track_number == TAGS["track_number"]
    assert tagged.disc_number == TAGS["disc_number"]
    assert tagged.genre == TAGS["genre"]
    assert tagged.artist_sort == TAGS["artist_sort"]
    assert tagged.mb_recording_id == TAGS["mb_recording_id"]
    assert tagged.mb_release_id == TAGS["mb_release_id"]
    assert tagged.mb_artist_id == TAGS["mb_artist_id"]
    assert tagged.has_cover_art is True
    assert tagged.duration_seconds is not None

    for field, expected in ADAPTER_ONLY_TAGS.items():
        assert _read_adapter_field(path, field) == expected, f"{field} lost on {ext}"


@pytest.mark.parametrize("ext", list(FORMATS))
@pytest.mark.requires_ffmpeg
def test_partial_write_leaves_other_fields_untouched(
    ext: str, format_files: dict[str, Path], tmp_path: Path
) -> None:
    """Only non-None fields are written — a title-only update must not clear the rest."""
    if ext not in format_files:
        pytest.skip(f"ffmpeg could not produce a .{ext} file")
    path = tmp_path / f"track.{ext}"
    shutil.copy(format_files[ext], path)

    write_tags(path, **TAGS)
    write_tags(path, title="Renamed")
    tagged = read_tags(path)

    assert tagged is not None
    assert tagged.title == "Renamed"
    assert tagged.artist == TAGS["artist"]
    assert tagged.album == TAGS["album"]
    assert tagged.mb_release_id == TAGS["mb_release_id"]
    assert tagged.track_number == TAGS["track_number"]


def test_read_tags_unsupported_extension(tmp_path: Path) -> None:
    p = tmp_path / "notes.txt"
    p.write_text("not audio")
    assert read_tags(p) is None


def test_read_tags_garbage_file(tmp_path: Path) -> None:
    p = tmp_path / "broken.mp3"
    p.write_bytes(b"\x00" * 128)
    assert read_tags(p) is None
