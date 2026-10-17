"""Path A wrong-version gates: duration agreement and same-song titles.

Built on the 2026-09-03 re-acquire run, where the fingerprint flagged three
correct files and missed two wrong-length ones. Duration is the signal that
got both right, so it flags on its own and outweighs a fingerprint guess.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from service.acquisition.pipeline import (
    _check_duration,
    _check_title_mismatch,
    _identify_locked_recording,
    _IdentifyState,
)
from service.config import settings
from service.core.models import TrackCandidate
from service.metadata import acoustid, musicbrainz
from service.metadata.musicbrainz import MBRecording

LOCKED = "locked-rec"
OTHER = "other-rec"


def _state(file_duration: int | None, title: str = "Song") -> _IdentifyState:
    return _IdentifyState(
        title=title, artist="Neil Young", album=None, year=None,
        track_number=None, disc_number=None, duration=file_duration,
        prov_title="tagged", prov_artist="tagged", prov_album="candidate",
        prov_year="candidate", prov_recording="candidate", album_locked=False,
        file_duration=file_duration,
    )


def _candidate(title: str = "Song", duration: int | None = 224) -> TrackCandidate:
    return TrackCandidate(
        provider="ytdlp", provider_ref="ytsearch1:x", title=title,
        artist="Neil Young", duration_seconds=duration, mb_recording_id=LOCKED,
    )


def _rec(rid: str, title: str, artist: str = "Neil Young",
         duration: int | None = None) -> MBRecording:
    return MBRecording(
        recording_id=rid, title=title, artist=artist, album=None, year=None,
        track_number=None, score=1.0, duration_seconds=duration,
    )


# ── duration verdicts ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "expected, got, verdict",
    [
        (224, 224, "agree"),
        (224, 227, "agree"),
        (224, 230, "close"),       # 6 s — neither confirms nor flags
        (224, 150, "mismatch"),    # Don't Let It Bring You Down
        (222, 252, "mismatch"),    # When You Dance I Can Really Love
        (600, 625, "close"),       # 5% of a long track is 30 s
        (600, 640, "mismatch"),
        (None, 224, None),
        (224, None, None),
    ],
)
def test_duration_verdict(expected: int | None, got: int | None, verdict: str | None) -> None:
    state = _state(got)
    assert _check_duration(state, expected, "job") == verdict
    assert (state.force_staging_reason is not None) == (verdict == "mismatch")


def test_duration_mismatch_names_both_lengths() -> None:
    state = _state(150)
    _check_duration(state, 224, "job")
    assert "expected 3:44, got 2:30" in (state.force_staging_reason or "")


# ── Path A with AcoustID ──────────────────────────────────────────────────


@pytest.fixture
def mb_world(monkeypatch: pytest.MonkeyPatch):
    """Point Path A at canned MB recordings and a canned AcoustID answer."""
    recordings: dict[str, MBRecording] = {}
    answer: dict[str, tuple[str, float] | None] = {"aq": None}

    async def _fake_acoustid(path: Path, api_key: str):
        return answer["aq"]

    monkeypatch.setattr(settings, "acoustid_api_key", "test-key")
    monkeypatch.setattr(acoustid, "acoustid_to_mbid", _fake_acoustid)
    monkeypatch.setattr(
        musicbrainz, "get_recording_by_id", lambda rid, cache: recordings.get(rid)
    )

    def _set(aq_mbid: str | None, **recs: MBRecording) -> None:
        recordings.update({r.recording_id: r for r in recs.values()})
        answer["aq"] = (aq_mbid, 0.95) if aq_mbid else None

    return _set


async def test_fingerprint_guess_overruled_by_matching_duration(mb_world) -> None:
    # Are You Ready for the Country? — fingerprint said "Alley Cat", length exact.
    mb_world(OTHER, a=_rec(LOCKED, "Are You Ready for the Country?"),
             b=_rec(OTHER, "Alley Cat", "Chris Johnson"))
    state = _state(233)
    _, confirmed = await _identify_locked_recording(
        state, _candidate("Are You Ready for the Country?", 233), Path("x"), "job"
    )
    assert state.force_staging_reason is None
    assert confirmed is False  # not flagged, but not a confirmation either


async def test_fingerprint_mismatch_still_flags_when_duration_is_off(mb_world) -> None:
    mb_world(OTHER, a=_rec(LOCKED, "Song"), b=_rec(OTHER, "Alley Cat", "Chris Johnson"))
    state = _state(230)  # 6 s off — not close enough to outweigh the fingerprint
    await _identify_locked_recording(state, _candidate(duration=224), Path("x"), "job")
    assert "Fingerprint mismatch" in (state.force_staging_reason or "")


async def test_wrong_length_flags_even_when_fingerprint_agrees(mb_world) -> None:
    mb_world(LOCKED, a=_rec(LOCKED, "Song"))
    state = _state(150)
    _, confirmed = await _identify_locked_recording(
        state, _candidate(duration=224), Path("x"), "job"
    )
    assert "Duration mismatch: expected 3:44, got 2:30" in (state.force_staging_reason or "")
    assert confirmed is True


async def test_wrong_length_flags_without_acoustid(mb_world) -> None:
    mb_world(None, a=_rec(LOCKED, "Song", duration=222))
    state = _state(252)
    await _identify_locked_recording(state, _candidate(duration=None), Path("x"), "job")
    # Falls back to MB's recording length when the candidate carries none.
    assert "expected 3:42, got 4:12" in (state.force_staging_reason or "")


async def test_sibling_recording_with_subtitle_confirms(mb_world) -> None:
    mb_world(OTHER, a=_rec(LOCKED, "The Losing End (When You're On)"),
             b=_rec(OTHER, "The Losing End"))
    state = _state(240)
    _, confirmed = await _identify_locked_recording(
        state, _candidate("The Losing End", 240), Path("x"), "job"
    )
    assert state.force_staging_reason is None
    assert confirmed is True


async def test_sibling_live_recording_is_not_the_same_song(mb_world) -> None:
    mb_world(OTHER, a=_rec(LOCKED, "Song"), b=_rec(OTHER, "Song (Live)"))
    state = _state(250)
    await _identify_locked_recording(state, _candidate(duration=244), Path("x"), "job")
    assert "Fingerprint mismatch" in (state.force_staging_reason or "")


# ── title gate ────────────────────────────────────────────────────────────


def test_title_gate_accepts_a_mix_qualifier() -> None:
    state = _state(300, title="Words (Between the Lines of Age) (5.1 mix)")
    _check_title_mismatch(state, _candidate("Words"), "job")
    assert state.force_staging_reason is None


def test_title_gate_still_catches_a_different_song() -> None:
    state = _state(300, title="Alley Cat")
    _check_title_mismatch(state, _candidate("Are You Ready for the Country?"), "job")
    assert "Title mismatch" in (state.force_staging_reason or "")
