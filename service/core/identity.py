import hashlib

from service.core.normalize import normalize

_DURATION_BUCKET_SECONDS = 2
ID_ALGORITHM_VERSION = 1


def _duration_bucket(seconds: int) -> int:
    return round(seconds / _DURATION_BUCKET_SECONDS) * _DURATION_BUCKET_SECONDS


def make_id(
    artist: str,
    title: str,
    duration_seconds: int | None,
    musicbrainz_recording_id: str | None = None,
) -> str:
    if musicbrainz_recording_id:
        return f"mb:{musicbrainz_recording_id}"

    parts = [normalize(artist), "|", normalize(title)]
    if duration_seconds is not None:
        parts.append(f"|{_duration_bucket(duration_seconds)}")

    digest = hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()
    return f"hash:{digest}"
