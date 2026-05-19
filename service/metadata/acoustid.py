"""AcoustID fingerprint → MusicBrainz recording MBID lookup.

Runs fpcalc (chromaprint) in a thread to generate an audio fingerprint, then
submits it to the AcoustID API and returns the best MB recording MBID.

Requires:
  - system package: libchromaprint-tools (provides fpcalc binary)
  - python package: pyacoustid
  - env var: AUDIOREAP_ACOUSTID_API_KEY (free key from acoustid.org)

Returns None silently if fpcalc is unavailable, API key is missing, or
confidence is below the threshold — callers fall back to text search.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_CONFIDENCE_THRESHOLD = 0.85


async def acoustid_to_mbid(path: Path, api_key: str) -> str | None:
    """Return the best MB recording MBID for the audio file, or None.

    Wraps pyacoustid's blocking match() call in asyncio.to_thread.
    Confidence below 0.85 is treated as no match.
    """
    if not api_key:
        return None

    def _sync() -> str | None:
        try:
            import acoustid  # type: ignore[import-untyped]
        except ImportError:
            logger.debug("pyacoustid not installed — AcoustID fingerprinting unavailable")
            return None

        best_score = 0.0
        best_rid: str | None = None
        try:
            for score, rid, _title, _artist in acoustid.match(api_key, str(path)):
                if score > best_score:
                    best_score = score
                    best_rid = rid
        except acoustid.FingerprintGenerationError as exc:
            logger.debug("AcoustID fingerprint failed (fpcalc unavailable?): %s", exc)
            return None
        except acoustid.WebServiceError as exc:
            logger.warning("AcoustID web service error: %s", exc)
            return None
        except Exception as exc:
            logger.debug("AcoustID unexpected error: %s", exc)
            return None

        if best_rid and best_score >= _CONFIDENCE_THRESHOLD:
            logger.info("AcoustID match: %s (score=%.2f)", best_rid, best_score)
            return best_rid

        logger.debug("AcoustID: no confident match (best score=%.2f)", best_score)
        return None

    return await asyncio.to_thread(_sync)
