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
_FEAT_RE = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+", re.IGNORECASE)
_COLLAB_RE = re.compile(r"\s*[,&]\s*")


def parse_artists(artist: str) -> list[str]:
    """Split a combined artist string into individual artists.

    Handles: "Artist feat. X", "A & B", "A, B, C".
    Returns the list deduplicated and ordered as they appear.
    """
    if not artist:
        return []
    parts = _FEAT_RE.split(artist, maxsplit=1)
    primary = parts[0].strip()
    featured = parts[1].strip() if len(parts) > 1 else ""

    primaries = [p.strip() for p in _COLLAB_RE.split(primary) if p.strip()]
    if not primaries:
        primaries = [primary]

    featureds = [p.strip() for p in _COLLAB_RE.split(featured) if p.strip()] if featured else []

    seen: set[str] = set()
    result: list[str] = []
    for a in primaries + featureds:
        if a and a not in seen:
            seen.add(a)
            result.append(a)
    return result or [artist]


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
    has_cover_art: bool = False
    genre: str | None = None
    artist_sort: str | None = None
    mb_artist_id: str | None = None
    mb_recording_id: str | None = None


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


def _check_cover_art(audio: object, tags: object) -> bool:
    """Return True if the file has embedded cover art."""
    if isinstance(audio, MP4):
        return bool(tags and tags.get("covr"))  # type: ignore[union-attr]
    elif isinstance(tags, ID3):
        return any(k.startswith("APIC") for k in tags.keys())
    else:
        from mutagen.flac import FLAC
        if isinstance(audio, FLAC):
            return bool(audio.pictures)  # type: ignore[union-attr]
        # OGG / Opus: METADATA_BLOCK_PICTURE vorbis comment
        return bool(
            tags
            and (
                tags.get("METADATA_BLOCK_PICTURE")  # type: ignore[union-attr]
                or tags.get("metadata_block_picture")  # type: ignore[union-attr]
            )
        )


def has_cover_art(path: Path) -> bool:
    """Return True if the audio file at path has embedded cover art."""
    try:
        audio = MutagenFile(path)
    except Exception:
        return False
    if audio is None:
        return False
    return _check_cover_art(audio, audio.tags)


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

    genre: str | None = None
    artist_sort: str | None = None
    mb_artist_id: str | None = None
    mb_recording_id: str | None = None

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
        genre_raw = (tags.get("©gen") or [None])[0] if tags else None
        genre = str(genre_raw).strip() if genre_raw else None
        soar_raw = (tags.get("soar") or [None])[0] if tags else None
        artist_sort = str(soar_raw).strip() if soar_raw else None
        mb_aid_raw = tags.get("----:com.apple.iTunes:MusicBrainz Artist Id") if tags else None
        if mb_aid_raw and isinstance(mb_aid_raw, list) and mb_aid_raw:
            val = mb_aid_raw[0]
            mb_artist_id = val.decode() if isinstance(val, bytes) else str(val)
        mb_rid_raw = tags.get("----:com.apple.iTunes:MusicBrainz Track Id") if tags else None
        if mb_rid_raw and isinstance(mb_rid_raw, list) and mb_rid_raw:
            val = mb_rid_raw[0]
            mb_recording_id = val.decode() if isinstance(val, bytes) else str(val)

    elif isinstance(tags, ID3):
        # MP3, WAV, AIFF — all have ID3-based tags regardless of audio FileType
        title = _id3_str(tags, "TIT2")
        artist = _id3_str(tags, "TPE1")
        albumartist = _id3_str(tags, "TPE2")
        album = _id3_str(tags, "TALB")
        year = _parse_year(_id3_str(tags, "TDRC") or _id3_str(tags, "TYER"))
        track_number = _parse_tracknum(_id3_str(tags, "TRCK"))
        disc_number = _parse_tracknum(_id3_str(tags, "TPOS"))
        genre = _id3_str(tags, "TCON")
        artist_sort = _id3_str(tags, "TSOP")
        mb_artist_id = _id3_str(tags, "TXXX:MusicBrainz Artist Id")
        mb_recording_id = _id3_str(tags, "TXXX:MusicBrainz Track Id")

    else:
        # Vorbis comment: FLAC, OGG Vorbis, Opus
        title = _vorbis_str(tags, "title")
        artist = _vorbis_str(tags, "artist")
        albumartist = _vorbis_str(tags, "albumartist")
        album = _vorbis_str(tags, "album")
        year = _parse_year(_vorbis_str(tags, "date"))
        track_number = _parse_tracknum(_vorbis_str(tags, "tracknumber"))
        disc_number = _parse_tracknum(_vorbis_str(tags, "discnumber"))
        genre = _vorbis_str(tags, "genre")
        artist_sort = _vorbis_str(tags, "artistsort")
        mb_artist_id = _vorbis_str(tags, "musicbrainz_artistid")
        mb_recording_id = _vorbis_str(tags, "musicbrainz_trackid")

    cover = _check_cover_art(audio, tags)

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
        has_cover_art=cover,
        genre=genre,
        artist_sort=artist_sort,
        mb_artist_id=mb_artist_id,
        mb_recording_id=mb_recording_id,
    )


def read_cover_art_bytes(path: Path) -> bytes | None:
    """Return the embedded cover art bytes from an audio file, or None."""
    try:
        audio = MutagenFile(path)
        if audio is None:
            return None
        tags = audio.tags
        if isinstance(audio, MP4):
            covr = tags.get("covr") if tags else None
            return bytes(covr[0]) if covr else None
        elif isinstance(tags, ID3):
            for key in tags.keys():
                if key.startswith("APIC"):
                    return tags[key].data  # type: ignore[union-attr]
            return None
        else:
            from mutagen.flac import FLAC
            if isinstance(audio, FLAC) and audio.pictures:
                return audio.pictures[0].data
            # OGG / Opus
            import base64
            from mutagen.flac import Picture
            raw = (tags or {}).get("METADATA_BLOCK_PICTURE") or (tags or {}).get("metadata_block_picture")
            if raw:
                pic = Picture(base64.b64decode(raw[0] if isinstance(raw, list) else raw))
                return pic.data
            return None
    except Exception:
        return None


def write_tags(
    path: Path,
    *,
    title: str | None = None,
    artist: str | None = None,
    albumartist: str | None = None,
    album: str | None = None,
    year: int | None = None,
    original_year: int | None = None,
    track_number: int | None = None,
    disc_number: int | None = None,
    artist_sort: str | None = None,
    genre: str | None = None,
    compilation: bool = False,
    mb_recording_id: str | None = None,
    mb_release_id: str | None = None,
    mb_artist_id: str | None = None,
    isrc: str | None = None,
    artwork_bytes: bytes | None = None,
) -> None:
    """Write normalized tags back to an audio file.

    Only fields that are not None (or False for booleans) are written —
    existing tags for omitted fields are left untouched.
    """
    audio = MutagenFile(path)
    if audio is None:
        return

    tags = audio.tags

    # WAV (and AIFF) files use ID3 tags but start with tags=None.
    # Detect this early so they fall into the ID3 branch below.
    if tags is None and not isinstance(audio, MP4):
        try:
            from mutagen.wave import WAVE as _WAVE
            if isinstance(audio, _WAVE):
                audio.add_tags()
                tags = audio.tags
        except Exception:
            pass

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
        if artist_sort is not None:
            tags["soar"] = [artist_sort]  # type: ignore[index]
        if compilation:
            tags["cpil"] = True  # type: ignore[index]
        if mb_recording_id is not None:
            tags["----:com.apple.iTunes:MusicBrainz Track Id"] = [  # type: ignore[index]
                mb_recording_id.encode()
            ]
        if mb_release_id is not None:
            tags["----:com.apple.iTunes:MusicBrainz Album Id"] = [  # type: ignore[index]
                mb_release_id.encode()
            ]
        if mb_artist_id is not None:
            tags["----:com.apple.iTunes:MusicBrainz Artist Id"] = [  # type: ignore[index]
                mb_artist_id.encode()
            ]
        if isrc is not None:
            tags["----:com.apple.iTunes:ISRC"] = [isrc.encode()]  # type: ignore[index]
        if genre is not None:
            tags["©gen"] = [genre]  # type: ignore[index]
        if artwork_bytes is not None:
            from mutagen.mp4 import MP4Cover
            tags["covr"] = [MP4Cover(artwork_bytes, imageformat=MP4Cover.FORMAT_JPEG)]  # type: ignore[index]

    elif isinstance(tags, ID3):
        from mutagen.id3 import (
            APIC, TALB, TCMP, TDOR, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TSOP, TXXX,
        )

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
        if original_year is not None:
            tags["TDOR"] = TDOR(encoding=3, text=[str(original_year)])
        if track_number is not None:
            tags["TRCK"] = TRCK(encoding=3, text=[str(track_number)])
        if disc_number is not None:
            tags["TPOS"] = TPOS(encoding=3, text=[str(disc_number)])
        if artist_sort is not None:
            tags["TSOP"] = TSOP(encoding=3, text=[artist_sort])
        if compilation:
            tags["TCMP"] = TCMP(encoding=3, text=["1"])
        if mb_recording_id is not None:
            tags["TXXX:MusicBrainz Track Id"] = TXXX(
                encoding=3, desc="MusicBrainz Track Id", text=[mb_recording_id]
            )
        if mb_release_id is not None:
            tags["TXXX:MusicBrainz Album Id"] = TXXX(
                encoding=3, desc="MusicBrainz Album Id", text=[mb_release_id]
            )
        if mb_artist_id is not None:
            tags["TXXX:MusicBrainz Artist Id"] = TXXX(
                encoding=3, desc="MusicBrainz Artist Id", text=[mb_artist_id]
            )
        if isrc is not None:
            from mutagen.id3 import TSRC
            tags["TSRC"] = TSRC(encoding=3, text=[isrc])
        if genre is not None:
            from mutagen.id3 import TCON
            tags["TCON"] = TCON(encoding=3, text=[genre])
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
            # Never write a multi-value `artists` tag — Navidrome splits each
            # value into a separate artist entry, creating ghost entries like
            # "Thomax" from "RSP & Thomax".
            if "artists" in tags:
                del tags["artists"]  # type: ignore[operator]
        if albumartist is not None:
            tags["albumartist"] = [albumartist]  # type: ignore[index]
        if album is not None:
            tags["album"] = [album]  # type: ignore[index]
        if year is not None:
            tags["date"] = [str(year)]  # type: ignore[index]
        if original_year is not None:
            tags["originaldate"] = [str(original_year)]  # type: ignore[index]
        if track_number is not None:
            tags["tracknumber"] = [str(track_number)]  # type: ignore[index]
        if disc_number is not None:
            tags["discnumber"] = [str(disc_number)]  # type: ignore[index]
        if artist_sort is not None:
            tags["artistsort"] = [artist_sort]  # type: ignore[index]
        if compilation:
            tags["compilation"] = ["1"]  # type: ignore[index]
        if mb_recording_id is not None:
            tags["musicbrainz_trackid"] = [mb_recording_id]  # type: ignore[index]
        if mb_release_id is not None:
            tags["musicbrainz_albumid"] = [mb_release_id]  # type: ignore[index]
        if mb_artist_id is not None:
            tags["musicbrainz_artistid"] = [mb_artist_id]  # type: ignore[index]
        if isrc is not None:
            tags["isrc"] = [isrc]  # type: ignore[index]
        if genre is not None:
            tags["genre"] = [genre]  # type: ignore[index]
        if artwork_bytes is not None:
            _embed_vorbis_art(audio, artwork_bytes)

    audio.save()


def write_cover_jpg(album_dir: Path, artwork_bytes: bytes) -> None:
    """Write artwork as cover.jpg in the album directory (sidecar file)."""
    try:
        cover_path = album_dir / "cover.jpg"
        cover_path.write_bytes(artwork_bytes)
    except Exception:
        pass


def read_mb_release_id(path: Path) -> str | None:
    """Read MUSICBRAINZ_ALBUMID from file tags across all container formats."""
    try:
        f = MutagenFile(path)
        if f is None:
            return None
        # Vorbis / OGG / FLAC
        for key in ("musicbrainz_albumid", "MUSICBRAINZ_ALBUMID"):
            if key in f:
                v = f[key]
                if isinstance(v, list):
                    return str(v[0]) if v else None
                return str(v) if v else None
        # ID3 (MP3): TXXX:MusicBrainz Album Id
        if hasattr(f, "tags") and f.tags:
            for frame_key in f.tags.keys():
                if "musicbrainz album id" in frame_key.lower():
                    frame = f.tags[frame_key]
                    if hasattr(frame, "text"):
                        return str(frame.text[0]) if frame.text else None
        # MP4
        if "----:com.apple.iTunes:MusicBrainz Album Id" in f:
            raw = f["----:com.apple.iTunes:MusicBrainz Album Id"]
            return raw[0].decode() if raw and isinstance(raw[0], bytes) else None
    except Exception:
        pass
    return None


def strip_album_version_tags(path: Path) -> bool:
    """Remove any album-version tags that fold into Navidrome's album PID.

    Navidrome folds a Vorbis ``VERSION`` (and ``albumversion``) tag into the
    album-version part of the computed PID, so a stray value on a single track
    splits an otherwise-identical release (navidrome issue #5082). The tagger
    never writes these, but a source file may carry them in. Returns True if a
    tag was removed (and the file re-saved).
    """
    try:
        audio = MutagenFile(path)
        if audio is None or audio.tags is None:
            return False
        tags = audio.tags
        removed = False

        if isinstance(audio, MP4):
            for key in (
                "----:com.apple.iTunes:VERSION",
                "----:com.apple.iTunes:ALBUMVERSION",
                "----:com.apple.iTunes:MusicBrainz Album Version",
            ):
                if key in tags:
                    del tags[key]
                    removed = True
        elif isinstance(tags, ID3):
            for frame_key in list(tags.keys()):
                desc = frame_key.split(":", 1)[1].lower() if ":" in frame_key else ""
                if frame_key.startswith("TXXX:") and desc in ("version", "albumversion"):
                    del tags[frame_key]
                    removed = True
        else:
            # Vorbis comment: FLAC, OGG, Opus — keys are case-insensitive
            for key in list(tags.keys()):
                if key.lower() in ("version", "albumversion"):
                    del tags[key]
                    removed = True

        if removed:
            audio.save()
        return removed
    except Exception:
        return False


def compute_replaygain(path: Path) -> float | None:
    """Run ffmpeg ebur128 loudness analysis and return track gain in dB.

    Reference level is -18 LUFS (EBU R128). Returns None if ffmpeg fails
    or the output cannot be parsed.
    """
    import re
    import subprocess

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner",
                "-i", str(path),
                "-filter:a", "ebur128=framelog=quiet",
                "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result.stderr
        m = re.search(r"I:\s+(-?\d+\.?\d*)\s+LUFS", output)
        if m:
            integrated_lufs = float(m.group(1))
            return round(-18.0 - integrated_lufs, 2)
    except Exception:
        pass
    return None


def write_replaygain(path: Path, track_gain_db: float) -> None:
    """Write ReplayGain track gain tag to an audio file.

    Format per container:
      FLAC/OGG/Vorbis: REPLAYGAIN_TRACK_GAIN = "+2.30 dB"
      Opus:            R128_TRACK_GAIN = integer in Q7.8 (gain * 256)
      MP3 ID3:         TXXX:REPLAYGAIN_TRACK_GAIN = "+2.30 dB"
      MP4:             ----:com.apple.iTunes:REPLAYGAIN_TRACK_GAIN
    """
    audio = MutagenFile(path)
    if audio is None:
        return

    gain_str = f"{track_gain_db:+.2f} dB"
    tags = audio.tags

    if isinstance(audio, MP4):
        if tags is None:
            audio.add_tags()
            tags = audio.tags
        tags["----:com.apple.iTunes:REPLAYGAIN_TRACK_GAIN"] = [  # type: ignore[index]
            gain_str.encode()
        ]

    elif isinstance(tags, ID3):
        from mutagen.id3 import TXXX
        tags["TXXX:REPLAYGAIN_TRACK_GAIN"] = TXXX(
            encoding=3, desc="REPLAYGAIN_TRACK_GAIN", text=[gain_str]
        )

    else:
        if tags is None:
            audio.add_tags()
            tags = audio.tags
        # Detect Opus by checking for R128 support
        from mutagen.oggopus import OggOpus
        if isinstance(audio, OggOpus):
            # Opus uses R128_TRACK_GAIN in Q7.8 fixed-point (integers, 1/256 dB)
            gain_q78 = round(track_gain_db * 256)
            tags["R128_TRACK_GAIN"] = [str(gain_q78)]  # type: ignore[index]
        else:
            tags["REPLAYGAIN_TRACK_GAIN"] = [gain_str]  # type: ignore[index]

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
