"""shift_lrc — LRC timestamp offset rewriting."""
from __future__ import annotations

from service.metadata.lyrics import shift_lrc


def test_shift_later_preserves_precision() -> None:
    assert shift_lrc("[00:12.34] Hello", 1.5) == "[00:13.84] Hello"


def test_shift_earlier() -> None:
    assert shift_lrc("[01:00.00] Line", -2.25) == "[00:57.75] Line"


def test_clamps_at_zero() -> None:
    assert shift_lrc("[00:01.00] Early", -5.0) == "[00:00.00] Early"


def test_whole_second_timestamps_stay_whole() -> None:
    assert shift_lrc("[00:59] Line", 1.0) == "[01:00] Line"


def test_millisecond_precision_kept() -> None:
    assert shift_lrc("[00:10.123] Line", 0.5) == "[00:10.623] Line"


def test_minute_rollover() -> None:
    assert shift_lrc("[00:59.90] Line", 0.2) == "[01:00.10] Line"


def test_metadata_tags_untouched() -> None:
    text = "[ar:Artist]\n[ti:Title]\n[offset:+500]\n[00:05.00] Line"
    out = shift_lrc(text, 1.0)
    assert "[ar:Artist]" in out
    assert "[ti:Title]" in out
    assert "[offset:+500]" in out
    assert "[00:06.00] Line" in out


def test_multiple_timestamps_per_line() -> None:
    assert shift_lrc("[00:10.00][00:40.00] Chorus", 1.0) == "[00:11.00][00:41.00] Chorus"


def test_plain_text_unchanged() -> None:
    text = "Just some plain lyrics\nwith no timestamps"
    assert shift_lrc(text, 3.0) == text


def test_zero_offset_is_stable() -> None:
    text = "[00:12.34] Hello\n[01:02.50] World"
    assert shift_lrc(text, 0.0) == text
