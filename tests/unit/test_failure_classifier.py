"""Unit tests for the failure classifier."""
import yt_dlp.utils as yt_utils

from service.acquisition.states import classify_failure


def test_video_unavailable_is_permanent() -> None:
    exc = yt_utils.DownloadError("ERROR: Video unavailable")
    fc, _ = classify_failure(exc)
    assert fc == "permanent"


def test_private_video_is_permanent() -> None:
    exc = yt_utils.DownloadError("ERROR: Private video")
    fc, _ = classify_failure(exc)
    assert fc == "permanent"


def test_copyright_is_permanent() -> None:
    exc = yt_utils.DownloadError("ERROR: This video contains content from X (copyright)")
    fc, _ = classify_failure(exc)
    assert fc == "permanent"


def test_age_restricted_is_permanent() -> None:
    exc = yt_utils.DownloadError("ERROR: Sign in to confirm your age")
    fc, _ = classify_failure(exc)
    assert fc == "permanent"


def test_geo_blocked_is_permanent() -> None:
    exc = yt_utils.DownloadError("ERROR: geo-blocked in your region")
    fc, _ = classify_failure(exc)
    assert fc == "permanent"


def test_rate_limit_is_transient() -> None:
    exc = yt_utils.DownloadError("ERROR: HTTP Error 429: Too Many Requests")
    fc, _ = classify_failure(exc)
    assert fc == "transient"


def test_service_unavailable_is_transient() -> None:
    exc = yt_utils.DownloadError("ERROR: HTTP Error 503: Service Unavailable")
    fc, _ = classify_failure(exc)
    assert fc == "transient"


def test_connection_error_is_transient() -> None:
    exc = ConnectionError("Connection reset by peer")
    fc, _ = classify_failure(exc)
    assert fc == "transient"


def test_timeout_is_transient() -> None:
    exc = TimeoutError("timed out")
    fc, _ = classify_failure(exc)
    assert fc == "transient"


def test_unknown_download_error_is_permanent() -> None:
    exc = yt_utils.DownloadError("ERROR: Something completely unexpected")
    fc, _ = classify_failure(exc)
    assert fc == "permanent"


def test_unknown_exception_is_transient() -> None:
    exc = RuntimeError("mystery error")
    fc, _ = classify_failure(exc)
    assert fc == "transient"


def test_error_message_preserved() -> None:
    msg = "ERROR: Video unavailable. The video is not available."
    exc = yt_utils.DownloadError(msg)
    _, returned_msg = classify_failure(exc)
    assert msg in returned_msg
