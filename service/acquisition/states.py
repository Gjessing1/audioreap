"""Acquisition job state machine and failure classifier."""
from __future__ import annotations

from typing import Literal

# Failure classifications
FailureClass = Literal["transient", "permanent", "match"]

_PERMANENT_PATTERNS: list[str] = [
    "video unavailable",
    "has been removed",
    "private video",
    "this video is not available",
    "account has been terminated",
    "geo-blocked",
    "geographic",
    "not available in your country",
    "copyright",
    "age-restricted",
    "sign in to confirm",
    "who can watch this video",
    "no video formats found",
    "unable to extract",
]

_TRANSIENT_PATTERNS: list[str] = [
    "http error 429",
    "http error 503",
    "http error 502",
    "rate limit",
    "connection reset",
    "connection refused",
    "timed out",
    "timeout",
    "temporary",
    "try again later",
    "too many requests",
    "network",
]


# Age-gate: YouTube refuses the streams without an age-verified login. Distinct from
# the generic permanent class because the pipeline can auto-substitute a non-gated
# source when no usable cookies are configured.
_AGE_GATE_PATTERNS: list[str] = [
    "confirm your age",
    "sign in to confirm",
    "age-restricted",
    "inappropriate for some users",
]


def is_age_gate_error(exc: Exception) -> bool:
    """True when the failure is YouTube's age-confirmation wall."""
    msg = str(exc).lower()
    return any(p in msg for p in _AGE_GATE_PATTERNS)


def classify_failure(exc: Exception) -> tuple[FailureClass, str]:
    """Map an exception to (failure_class, human-readable message).

    Rules:
    - DownloadError with permanent message → permanent (no auto-retry)
    - DownloadError with transient message → transient (backoff retry)
    - Unknown DownloadError → permanent (safer than retrying forever)
    - Network / OS errors → transient
    - Everything else → transient
    """
    import yt_dlp.utils as yt_utils

    msg = str(exc).lower()
    error_str = str(exc)

    if isinstance(exc, (yt_utils.DownloadError, yt_utils.ExtractorError)):
        for pattern in _PERMANENT_PATTERNS:
            if pattern in msg:
                return "permanent", error_str
        for pattern in _TRANSIENT_PATTERNS:
            if pattern in msg:
                return "transient", error_str
        return "permanent", error_str

    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return "transient", error_str

    return "transient", error_str
