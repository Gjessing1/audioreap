"""Normalized tag reader/writer over mutagen.

Nothing outside this module touches mutagen directly. All callers work with
TaggedFile for reads, and keyword arguments for writes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from mutagen import File as MutagenFile  # type: ignore[attr-defined]
from mutagen.id3 import ID3
from mutagen.mp4 import MP4

SUPPORTED_EXTENSIONS = frozenset(
    {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav", ".wma"}
)

_TRACKNUM_RE = re.compile(r"^(\d+)")


@dataclass
class TaggedFile:
    path: Path
    title: str | None
    artist: str | None
    albumartist: str | None
    album: str | None
    year: int | None
    track_number: int | None
    disc_number: int | None
    duration_seconds: int | None
    codec: str
    container: str
    bitrate_kbps: int | None
    sample_rate_hz: int | None


def _parse_tracknum(value: str | None) -> int | None:
    if not value:
        return None
    m = _TRACKNUM_RE.match(value.strip())
    return int(m.group(1)) if m else None


def _parse_year(value: str | None) -> int | None:
    if not value:
        return None
    m = re.match(r"(\d{4})", value.strip())
    return int(m.group(1)) if m else None


def _codec_from_path(path: Path) -> tuple[str, str]:
    ext = path.suffix.lower()
    mapping = {
        ".mp3": ("mp3", "mp3"),
        ".flac": ("flac", "flac"),
        ".ogg": ("vorbis", "ogg"),
        ".opus": ("opus", "ogg"),
        ".m4a": ("aac", "m4a"),
        ".aac": ("aac", "aac"),
        ".wav": ("pcm_s16le", "wav"),
        ".wma": ("wma", "asf"),
    }
    return mapping.get(ext, ("unknown", ext.lstrip(".")))


def _id3_str(tags: object, key: str) -> str | None:
    """Get a text value from an ID3 tag dict by frame key (e.g. 'TIT2')."""
    if tags is None:
        return None
    frame = getattr(tags, "get", lambda k, d=None: d)(key)
    if frame is None:
        return None
    text = str(frame).strip()
    return text or None


def _vorbis_str(tags: object, key: str) -> str | None:
    """Get a text value from a Vorbis/FLAC/OGG tag dict by lowercase key."""
    if tags is None:
        return None
    val = getattr(tags, "get", lambda k, d=None: d)(key.lower())
    if not val:
        return None
    first = val[0] if isinstance(val, list) else val
    text = str(first).strip()
    return text or None


def read_tags(path: Path) -> TaggedFile | None:
    """Return normalized tags for an audio file, or None if unreadable."""
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return None

    try:
        audio = MutagenFile(path)
    except Exception:
        return None

    if audio is None:
        return None

    codec, container = _codec_from_path(path)
    duration: int | None = None
    bitrate: int | None = None
    sample_rate: int | None = None

    info = getattr(audio, "info", None)
    if info is not None:
        raw_len = getattr(info, "length", None)
        duration = int(raw_len) if raw_len is not None else None
        raw_br = getattr(info, "bitrate", None)
        bitrate = int(raw_br // 1000) if raw_br else None
        raw_sr = getattr(info, "sample_rate", None)
        sample_rate = int(raw_sr) if raw_sr else None

    tags = audio.tags

    if isinstance(audio, MP4):
        # note: MP4 check must come before ID3 check
        title = (tags.get("©nam") or [None])[0] if tags else None
        artist = (tags.get("©ART") or [None])[0] if tags else None
        albumartist = (tags.get("aART") or [None])[0] if tags else None
        album = (tags.get("©alb") or [None])[0] if tags else None
        year = _parse_year(str((tags.get("©day") or [None])[0]) if tags and tags.get("©day") else None)
        trkn = (tags.get("trkn") or [(None, None)])[0][0] if tags else None
        track_number = int(trkn) if trkn is not None else None
        disk = (tags.get("disk") or [(None, None)])[0][0] if tags else None
        disc_number = int(disk) if disk is not None else None

    elif isinstance(tags, ID3):
        # MP3, WAV, AIFF — all have ID3-based tags regardless of audio FileType
        title = _id3_str(tags, "TIT2")
        artist = _id3_str(tags, "TPE1")
        albumartist = _id3_str(tags, "TPE2")
        album = _id3_str(tags, "TALB")
        year = _parse_year(_id3_str(tags, "TDRC") or _id3_str(tags, "TYER"))
        track_number = _parse_tracknum(_id3_str(tags, "TRCK"))
        disc_number = _parse_tracknum(_id3_str(tags, "TPOS"))

    else:
        # Vorbis comment: FLAC, OGG Vorbis, Opus
        title = _vorbis_str(tags, "title")
        artist = _vorbis_str(tags, "artist")
        albumartist = _vorbis_str(tags, "albumartist")
        album = _vorbis_str(tags, "album")
        year = _parse_year(_vorbis_str(tags, "date"))
        track_number = _parse_tracknum(_vorbis_str(tags, "tracknumber"))
        disc_number = _parse_tracknum(_vorbis_str(tags, "discnumber"))

    return TaggedFile(
        path=path,
        title=title,
        artist=artist,
        albumartist=albumartist,
        album=album,
        year=year,
        track_number=track_number,
        disc_number=disc_number,
        duration_seconds=duration,
        codec=codec,
        container=container,
        bitrate_kbps=bitrate,
        sample_rate_hz=sample_rate,
    )


def write_tags(
    path: Path,
    *,
    title: str | None = None,
    artist: str | None = None,
    albumartist: str | None = None,
    album: str | None = None,
    year: int | None = None,
    track_number: int | None = None,
    disc_number: int | None = None,
    artwork_bytes: bytes | None = None,
) -> None:
    """Write normalized tags back to an audio file.

    Only fields that are not None are written — existing tags for omitted
    fields are left untouched.
    """
    audio = MutagenFile(path)
    if audio is None:
        return

    tags = audio.tags

    if isinstance(audio, MP4):
        if tags is None:
            audio.add_tags()
            tags = audio.tags
        if title is not None:
            tags["©nam"] = [title]  # type: ignore[index]
        if artist is not None:
            tags["©ART"] = [artist]  # type: ignore[index]
        if albumartist is not None:
            tags["aART"] = [albumartist]  # type: ignore[index]
        if album is not None:
            tags["©alb"] = [album]  # type: ignore[index]
        if year is not None:
            tags["©day"] = [str(year)]  # type: ignore[index]
        if track_number is not None:
            tags["trkn"] = [(track_number, 0)]  # type: ignore[index]
        if artwork_bytes is not None:
            from mutagen.mp4 import MP4Cover
            tags["covr"] = [MP4Cover(artwork_bytes, imageformat=MP4Cover.FORMAT_JPEG)]  # type: ignore[index]

    elif isinstance(tags, ID3):
        from mutagen.id3 import APIC, TALB, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK

        if title is not None:
            tags["TIT2"] = TIT2(encoding=3, text=[title])
        if artist is not None:
            tags["TPE1"] = TPE1(encoding=3, text=[artist])
        if albumartist is not None:
            tags["TPE2"] = TPE2(encoding=3, text=[albumartist])
        if album is not None:
            tags["TALB"] = TALB(encoding=3, text=[album])
        if year is not None:
            tags["TDRC"] = TDRC(encoding=3, text=[str(year)])
        if track_number is not None:
            tags["TRCK"] = TRCK(encoding=3, text=[str(track_number)])
        if disc_number is not None:
            tags["TPOS"] = TPOS(encoding=3, text=[str(disc_number)])
        if artwork_bytes is not None:
            tags["APIC"] = APIC(
                encoding=3,
                mime="image/jpeg",
                type=3,
                desc="Cover",
                data=artwork_bytes,
            )

    else:
        # Vorbis comment: FLAC, OGG, Opus
        if tags is None:
            audio.add_tags()
            tags = audio.tags
        if title is not None:
            tags["title"] = [title]  # type: ignore[index]
        if artist is not None:
            tags["artist"] = [artist]  # type: ignore[index]
        if albumartist is not None:
            tags["albumartist"] = [albumartist]  # type: ignore[index]
        if album is not None:
            tags["album"] = [album]  # type: ignore[index]
        if year is not None:
            tags["date"] = [str(year)]  # type: ignore[index]
        if track_number is not None:
            tags["tracknumber"] = [str(track_number)]  # type: ignore[index]
        if disc_number is not None:
            tags["discnumber"] = [str(disc_number)]  # type: ignore[index]
        if artwork_bytes is not None:
            _embed_vorbis_art(audio, artwork_bytes)

    audio.save()


def _embed_vorbis_art(audio: object, data: bytes) -> None:
    """Embed JPEG artwork into a Vorbis-comment-based file."""
    import base64

    from mutagen.flac import FLAC, Picture

    if isinstance(audio, FLAC):
        pic = Picture()
        pic.type = 3
        pic.mime = "image/jpeg"
        pic.data = data
        audio.clear_pictures()
        audio.add_picture(pic)
    else:
        # OGG / Opus: METADATA_BLOCK_PICTURE tag
        pic = Picture()
        pic.type = 3
        pic.mime = "image/jpeg"
        pic.data = data
        pic_data = pic.write()
        encoded = base64.b64encode(pic_data).decode("ascii")
        tags = getattr(audio, "tags", None)
        if tags is not None:
            tags["METADATA_BLOCK_PICTURE"] = [encoded]  # type: ignore[index]
