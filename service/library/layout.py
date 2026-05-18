"""Library path template.

Default layout:
  /music/<AlbumArtist>/<Year> - <Album>/<TrackNum> - <Title>.<ext>
  /music/<Artist>/Singles/<Title>.<ext>   (no album known)

All path components are sanitised to be filesystem-safe.
"""
import re
from pathlib import Path

# Characters illegal on most filesystems (Windows-superset, safe on Linux too)
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_COMPONENT = 180


def _safe(name: str) -> str:
    safe = _UNSAFE.sub("_", name).strip(". ")
    return safe[:_MAX_COMPONENT] or "_"


def track_path(
    music_dir: Path,
    *,
    artist: str,
    album: str | None,
    year: int | None,
    track_number: int | None,
    disc_number: int | None,
    title: str,
    ext: str,
) -> Path:
    """Return the canonical destination path for a track in the library."""
    safe_artist = _safe(artist)
    safe_title = _safe(title)
    clean_ext = ext.lstrip(".")

    if album:
        safe_album = _safe(album)
        album_dir = f"{year} - {safe_album}" if year else safe_album

        if track_number is not None:
            if disc_number and disc_number > 1:
                filename = f"{disc_number:01d}{track_number:02d} - {safe_title}.{clean_ext}"
            else:
                filename = f"{track_number:02d} - {safe_title}.{clean_ext}"
        else:
            filename = f"{safe_title}.{clean_ext}"

        return music_dir / safe_artist / album_dir / filename

    return music_dir / safe_artist / "Singles" / f"{safe_title}.{clean_ext}"
