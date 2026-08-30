"""Normalized tag reader/writer over mutagen.

Nothing outside this module touches mutagen directly. All callers work with
TaggedFile for reads, and keyword arguments for writes.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from mutagen import File as MutagenFile  # type: ignore[attr-defined]
from mutagen.id3 import ID3
from mutagen.mp4 import MP4

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = frozenset(
    {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav", ".wma"}
)

_TRACKNUM_RE = re.compile(r"^(\d+)")
_FEAT_RE = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+", re.IGNORECASE)
_COLLAB_RE = re.compile(r"\s*[,&]\s*")


def primary_artist(artist: str) -> str:
    """Artist string with any featuring credit removed ("A feat. B" → "A").

    Only feat./ft./featuring is stripped — "&" and "," collaborations are kept
    intact because legitimate duo names ("Simon & Garfunkel") are
    indistinguishable from collaborations at this level. Used for ALBUMARTIST,
    which must never carry guests or albums fragment into per-featuring artists.
    """
    if not artist:
        return artist
    return _FEAT_RE.split(artist, maxsplit=1)[0].strip() or artist


def title_with_guests(title: str, guests: str | None) -> str:
    """Move a collapsed guest credit into the title: "Eg ser (feat. Lisa Nilsson)".

    ARTIST carries only the main artist, because a guest in that tag becomes its
    own artist entry in Navidrome and fragments the album. The credit is not
    dropped — it moves where it reads as information rather than as identity.

    MusicBrainz' own join phrase is free text and language-specific ("med", "&",
    "feat."), so the suffix is normalised to "feat." for consistency across the
    library; the verbatim credit still goes to ORIGINALARTIST.

    Idempotent: a title that already names the guest is returned unchanged, so
    re-acquiring or re-tagging never stacks suffixes.
    """
    guests = (guests or "").strip()
    if not title or not guests:
        return title
    if guests.lower() in title.lower():
        return title
    return f"{title} (feat. {guests})"


def title_with_performer(title: str, performer: str | None) -> str:
    """Move a compilation performer into the title: "Silent Night (Mahalia Jackson)".

    Same trade as `title_with_guests`, for the other case that fragments an
    artist list: on a various-artists compilation every track has a different
    performer, and leaving each one in ARTIST gives Navidrome twenty one-track
    artists for one album. ARTIST becomes the shared album artist and the
    performer moves where it reads as information rather than as identity.

    No "feat." here — the performer IS the act, not a guest on someone else's
    track — so the credit is appended verbatim in bare parentheses, matching the
    CD ripper this convention comes from. The full credit still goes to
    ORIGINALARTIST, so the substitution is reversible from the file alone.

    Idempotent: a title that already names the performer is returned unchanged,
    so re-acquiring or re-tagging never stacks suffixes.
    """
    performer = (performer or "").strip()
    if not title or not performer:
        return title
    if performer.lower() in title.lower():
        return title
    return f"{title} ({performer})"


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
    mb_release_id: str | None = None


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


# ── Container adapter ─────────────────────────────────────────────────────────
# The single place that knows how each tag family (MP4 atoms, ID3 frames,
# Vorbis comments) stores every normalized field. All read/write functions
# dispatch through _tag_format + _get_field/_set_field instead of
# reimplementing isinstance checks and key literals per function — the
# per-function branches had diverged twice before this existed (MP4 write
# silently dropped disc_number; read_mb_release_id substring-matched ID3
# frame keys while read_tags matched exactly).

_MP4, _ID3, _VORBIS = "mp4", "id3", "vorbis"
_FMT_INDEX = {_MP4: 0, _ID3: 1, _VORBIS: 2}

# field → (MP4 atom, ID3 frame key, Vorbis comment key); None = the format
# has no representation for this field.
_FIELD_KEYS: dict[str, tuple[str | None, str | None, str | None]] = {
    "title": ("©nam", "TIT2", "title"),
    "artist": ("©ART", "TPE1", "artist"),
    "albumartist": ("aART", "TPE2", "albumartist"),
    "album": ("©alb", "TALB", "album"),
    "year": ("©day", "TDRC", "date"),
    "original_year": (None, "TDOR", "originaldate"),
    "track_number": ("trkn", "TRCK", "tracknumber"),
    "disc_number": ("disk", "TPOS", "discnumber"),
    "genre": ("©gen", "TCON", "genre"),
    "artist_sort": ("soar", "TSOP", "artistsort"),
    "compilation": ("cpil", "TCMP", "compilation"),
    # The credit ARTIST was collapsed out of ("A med B" → "A"). Keeps the
    # substitution visible and reversible from the file alone.
    "original_artist": (
        "----:com.apple.iTunes:ORIGINALARTIST",
        "TOPE",
        "originalartist",
    ),
    "isrc": ("----:com.apple.iTunes:ISRC", "TSRC", "isrc"),
    "mb_recording_id": (
        "----:com.apple.iTunes:MusicBrainz Track Id",
        "TXXX:MusicBrainz Track Id",
        "musicbrainz_trackid",
    ),
    "mb_release_id": (
        "----:com.apple.iTunes:MusicBrainz Album Id",
        "TXXX:MusicBrainz Album Id",
        "musicbrainz_albumid",
    ),
    "mb_artist_id": (
        "----:com.apple.iTunes:MusicBrainz Artist Id",
        "TXXX:MusicBrainz Artist Id",
        "musicbrainz_artistid",
    ),
    "mb_albumartist_id": (
        "----:com.apple.iTunes:MusicBrainz Album Artist Id",
        "TXXX:MusicBrainz Album Artist Id",
        "musicbrainz_albumartistid",
    ),
}


def _tag_format(audio: object, tags: object) -> str:
    if isinstance(audio, MP4):
        return _MP4  # must be checked before ID3 — MP4 tags aren't an ID3 dict
    if isinstance(tags, ID3):
        return _ID3  # MP3, WAV, AIFF
    return _VORBIS  # FLAC, OGG Vorbis, Opus


def _field_key(fmt: str, field: str) -> str | None:
    return _FIELD_KEYS[field][_FMT_INDEX[fmt]]


def _get_field(fmt: str, tags: object, field: str) -> str | None:
    """Read one normalized field as a stripped string, or None if absent."""
    key = _field_key(fmt, field)
    if key is None or tags is None:
        return None
    if fmt == _MP4:
        val = getattr(tags, "get", lambda k, d=None: d)(key)
        if not val:
            return None
        first = val[0] if isinstance(val, (list, tuple)) else val
        if isinstance(first, tuple):  # trkn/disk atoms hold (number, total)
            first = first[0]
        if first is None:
            return None
        if isinstance(first, bytes):  # ----:freeform atoms hold bytes
            first = first.decode(errors="replace")
        text = str(first).strip()
        return text or None
    if fmt == _ID3:
        return _id3_str(tags, key)
    return _vorbis_str(tags, key)


def _set_field(fmt: str, tags: object, field: str, value: str | int) -> None:
    """Write one normalized field in the container's native representation.

    compilation must be passed as 1 (it becomes cpil=True / "1" per format).
    """
    key = _field_key(fmt, field)
    if key is None:
        return
    if fmt == _MP4:
        if key in ("trkn", "disk"):
            tags[key] = [(int(value), 0)]  # type: ignore[index]
        elif key == "cpil":
            tags[key] = bool(value)  # type: ignore[index]
        elif key.startswith("----:"):
            tags[key] = [str(value).encode()]  # type: ignore[index]
        else:
            tags[key] = [str(value)]  # type: ignore[index]
    elif fmt == _ID3:
        from mutagen import id3 as _id3

        if key.startswith("TXXX:"):
            desc = key.split(":", 1)[1]
            tags[key] = _id3.TXXX(encoding=3, desc=desc, text=[str(value)])  # type: ignore[index]
        else:
            frame_cls = getattr(_id3, key)
            tags[key] = frame_cls(encoding=3, text=[str(value)])  # type: ignore[index]
    else:
        tags[key] = [str(value)]  # type: ignore[index]


def _check_cover_art(audio: object, tags: object) -> bool:
    """Return True if the file has embedded cover art (existence only — cheap)."""
    fmt = _tag_format(audio, tags)
    if fmt == _MP4:
        return bool(tags and tags.get("covr"))  # type: ignore[union-attr]
    if fmt == _ID3:
        return any(k.startswith("APIC") for k in tags.keys())
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
    except Exception as exc:
        logger.debug("has_cover_art: unreadable file %s: %s", path, exc)
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
    except Exception as exc:
        logger.warning("read_tags: unreadable file %s: %s", path, exc)
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
    fmt = _tag_format(audio, tags)

    title = _get_field(fmt, tags, "title")
    artist = _get_field(fmt, tags, "artist")
    albumartist = _get_field(fmt, tags, "albumartist")
    album = _get_field(fmt, tags, "album")
    year = _parse_year(_get_field(fmt, tags, "year"))
    if year is None and fmt == _ID3:
        year = _parse_year(_id3_str(tags, "TYER"))  # legacy ID3v2.3 frame
    track_number = _parse_tracknum(_get_field(fmt, tags, "track_number"))
    disc_number = _parse_tracknum(_get_field(fmt, tags, "disc_number"))
    genre = _get_field(fmt, tags, "genre")
    artist_sort = _get_field(fmt, tags, "artist_sort")
    mb_artist_id = _get_field(fmt, tags, "mb_artist_id")
    mb_recording_id = _get_field(fmt, tags, "mb_recording_id")
    mb_release_id = _get_field(fmt, tags, "mb_release_id")

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
        mb_release_id=mb_release_id,
    )


def read_cover_art_bytes(path: Path) -> bytes | None:
    """Return the embedded cover art bytes from an audio file, or None."""
    try:
        audio = MutagenFile(path)
        if audio is None:
            return None
        tags = audio.tags
        fmt = _tag_format(audio, tags)
        if fmt == _MP4:
            covr = tags.get("covr") if tags else None
            return bytes(covr[0]) if covr else None
        if fmt == _ID3:
            for key in tags.keys():
                if key.startswith("APIC"):
                    return tags[key].data  # type: ignore[union-attr]
            return None
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
    except Exception as exc:
        logger.debug("read_cover_art_bytes failed for %s: %s", path, exc)
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
    original_artist: str | None = None,
    mb_recording_id: str | None = None,
    mb_release_id: str | None = None,
    mb_artist_id: str | None = None,
    mb_albumartist_id: str | None = None,
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

    if audio.tags is None:
        # Untagged files (fresh remuxes, WAV/AIFF) — mutagen adds the right
        # tag container for the FileType (ID3 for MP3/WAV, VComment for OGG…).
        try:
            audio.add_tags()
        except Exception as exc:
            logger.warning("write_tags: cannot add tags to %s: %s", path, exc)
            return
    tags = audio.tags
    fmt = _tag_format(audio, tags)

    fields: dict[str, str | int | None] = {
        "title": title,
        "artist": artist,
        "albumartist": albumartist,
        "album": album,
        "year": year,
        "original_year": original_year,
        "track_number": track_number,
        "disc_number": disc_number,
        "artist_sort": artist_sort,
        "genre": genre,
        "original_artist": original_artist,
        "mb_recording_id": mb_recording_id,
        "mb_release_id": mb_release_id,
        "mb_artist_id": mb_artist_id,
        "mb_albumartist_id": mb_albumartist_id,
        "isrc": isrc,
    }
    for field, value in fields.items():
        if value is not None:
            _set_field(fmt, tags, field, value)
    if compilation:
        _set_field(fmt, tags, "compilation", 1)

    if artist is not None and fmt == _VORBIS and "artists" in tags:  # type: ignore[operator]
        # Never keep a multi-value `artists` tag — Navidrome splits each
        # value into a separate artist entry, creating ghost entries like
        # "Thomax" from "RSP & Thomax".
        del tags["artists"]  # type: ignore[operator]

    if artwork_bytes is not None:
        if fmt == _MP4:
            from mutagen.mp4 import MP4Cover
            tags["covr"] = [MP4Cover(artwork_bytes, imageformat=MP4Cover.FORMAT_JPEG)]  # type: ignore[index]
        elif fmt == _ID3:
            from mutagen.id3 import APIC
            tags["APIC"] = APIC(
                encoding=3, mime="image/jpeg", type=3, desc="Cover", data=artwork_bytes
            )
        else:
            _embed_vorbis_art(audio, artwork_bytes)

    audio.save()


def write_cover_jpg(album_dir: Path, artwork_bytes: bytes) -> None:
    """Write artwork as cover.jpg in the album directory (sidecar file)."""
    try:
        cover_path = album_dir / "cover.jpg"
        cover_path.write_bytes(artwork_bytes)
    except Exception as exc:
        logger.warning("write_cover_jpg failed for %s: %s", album_dir, exc)


def read_mb_release_id(path: Path) -> str | None:
    """Read MUSICBRAINZ_ALBUMID from file tags across all container formats.

    Uses the same exact-key adapter as read_tags — so the two can never
    disagree about whether a file carries a release ID.
    """
    try:
        audio = MutagenFile(path)
        if audio is None:
            return None
        tags = audio.tags
        return _get_field(_tag_format(audio, tags), tags, "mb_release_id")
    except Exception as exc:
        logger.warning("read_mb_release_id failed for %s: %s", path, exc)
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
        fmt = _tag_format(audio, tags)
        removed = False

        if fmt == _MP4:
            for key in (
                "----:com.apple.iTunes:VERSION",
                "----:com.apple.iTunes:ALBUMVERSION",
                "----:com.apple.iTunes:MusicBrainz Album Version",
            ):
                if key in tags:
                    del tags[key]
                    removed = True
        elif fmt == _ID3:
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
    except Exception as exc:
        logger.warning("strip_album_version_tags failed for %s: %s", path, exc)
        return False


def run_rsgain(paths: list[Path], *, album: bool, skip_existing: bool = False) -> bool:
    """Analyze loudness/peak via rsgain and write ReplayGain 2.0 tags in place.

    Always writes REPLAYGAIN_TRACK_GAIN/PEAK on every path. When album=True,
    all paths are treated as one release and also get shared
    REPLAYGAIN_ALBUM_GAIN/PEAK — this requires every file to be analyzed in
    the same pass, so callers must batch a whole release into one call rather
    than invoking this once per track. album=False (singles) writes track
    tags only, since unrelated singles can share one filesystem folder and
    must never be gain-grouped together.

    skip_existing=True (rsgain -S) skips only the tag *write* for files that
    already carry ReplayGain info — every file is still loudness-scanned, so
    album-gain math stays correct even when re-run over a partially-tagged
    folder. Only pass True for backfill sweeps; the live per-track/per-album
    hooks in pipeline.py must leave it False so a changed track's siblings get
    their (now-stale) album gain recomputed too.

    True-peak calculation and positive-gain clipping protection are enabled.
    Opus files still get standard REPLAYGAIN_* tags (not R128_*_GAIN) so the
    four tags are consistent across every container. Best-effort: returns
    False (never raises) if rsgain is missing, times out, or fails.

    Target loudness is -14 LUFS (streaming-loudness, matches Spotify/YouTube)
    rather than rsgain's own -18 LUFS default — quieter masters ended up
    audibly attenuated relative to the rest of the library, and louder is
    the preferred tradeoff here as long as it stays off clipping (-c p still
    guards positive gains against inter-sample peaks).
    """
    import subprocess

    if not paths:
        return False
    cmd = ["rsgain", "custom", "-s", "i", "-t", "-c", "p", "-l", "-14"]
    if album:
        cmd.append("-a")
    if skip_existing:
        cmd.append("-S")
    cmd.extend(str(p) for p in paths)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max(120, 20 * len(paths)),
        )
        if result.returncode != 0:
            logger.warning(
                "rsgain exited %s for %d file(s): %s",
                result.returncode,
                len(paths),
                (result.stderr or result.stdout or "").strip()[:500],
            )
            return False
        return True
    except Exception as exc:
        logger.warning("rsgain failed for %d file(s): %s", len(paths), exc)
        return False


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
