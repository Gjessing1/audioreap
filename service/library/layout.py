"""Library path template.

Target layout (layout_version=2):
  Albums:       /music/<AlbumArtist>/<Album> (<Year>)/<NN> - <Title>.<ext>
  Singles:      /music/Singles/<Artist>/<Title>.<ext>
  Compilations: /music/Compilations/<Album> (<Year>)/<NN> - <Artist> - <Title>.<ext>

All path components are sanitised to be filesystem-safe.
"""
import re
from pathlib import Path

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_COMPONENT = 180

_VARIOUS_ARTISTS = frozenset({"various artists", "various"})


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
    albumartist: str | None = None,
) -> Path:
    """Return the canonical destination path for a track in the library."""
    safe_artist = _safe(artist)
    safe_title = _safe(title)
    clean_ext = ext.lstrip(".")

    effective_albumartist = albumartist or artist
    is_compilation = effective_albumartist.lower() in _VARIOUS_ARTISTS

    if album:
        safe_album = _safe(album)
        album_dir = f"{safe_album} ({year})" if year else safe_album

        if track_number is not None:
            if disc_number and disc_number > 1:
                tn = f"{disc_number:01d}{track_number:02d}"
            else:
                tn = f"{track_number:02d}"
            if is_compilation:
                filename = f"{tn} - {safe_artist} - {safe_title}.{clean_ext}"
            else:
                filename = f"{tn} - {safe_title}.{clean_ext}"
        else:
            if is_compilation:
                filename = f"{safe_artist} - {safe_title}.{clean_ext}"
            else:
                filename = f"{safe_title}.{clean_ext}"

        if is_compilation:
            return music_dir / "Compilations" / album_dir / filename

        return music_dir / _safe(effective_albumartist) / album_dir / filename

    # No album — place as singleton
    return music_dir / "Singles" / safe_artist / f"{safe_title}.{clean_ext}"
