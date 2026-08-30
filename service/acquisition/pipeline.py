"""Acquisition pipeline: download → identify (needs_review) → approve → place → index → scan.

Phase 1 (run_acquisition): download, remux, fingerprint, MB lookup, place in staging,
store resolved_metadata_json on job row, set state needs_review.

Phase 2 (place_approved_track): called from the API when the user approves the review.
Writes final tags, moves file from staging to /music, indexes, triggers Navidrome scan.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from service.acquisition.states import classify_failure, is_age_gate_error
from service.config import settings
from service.core.identity import make_id
from service.core.models import CandidateScore, ResolvedTrackMetadata, TrackCandidate
from service.core.normalize import clean_for_search, normalize
from service.db.schema import AcquisitionJobRow
from service.index.scanner import index_file
from service.library.layout import track_path
from service.library.tagger import has_cover_art, read_tags, write_cover_jpg, write_tags
from service.library.writer import atomic_place
from service.metadata.musicbrainz import VARIOUS_ARTISTS_NAME
from service.metadata.quality import compute_quality_score
from service.providers.base import Provider

logger = logging.getLogger(__name__)

ScanTrigger = Callable[[], Awaitable[None]]

_REMUX_CONTAINERS = frozenset({".webm", ".weba"})

# Fire-and-forget tasks (lyrics sidecars) kept referenced so they aren't GC'd
# mid-flight; cancelled on worker shutdown alongside the progress tasks.
_bg_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]


async def _write_lyrics_sidecar(
    dest: Path,
    artist: str | None,
    title: str,
    album: str | None,
    duration_seconds: int | None,
) -> None:
    """Best-effort LRCLIB fetch + .lrc sidecar, off the approval critical path."""
    try:
        from service.metadata.lyrics import fetch_lyrics, write_lrc_sidecar
        lyrics = await fetch_lyrics(
            artist=artist,
            title=title,
            album=album,
            duration_seconds=duration_seconds,
            cache_dir=settings.cache_dir,
        )
        if lyrics is not None and lyrics.best:
            await asyncio.to_thread(write_lrc_sidecar, dest, lyrics.best)
            logger.info(
                "Lyrics: wrote %s sidecar for %s",
                "synced" if lyrics.synced else "plain", dest.name,
            )
    except Exception as exc:
        logger.debug("Lyrics fetch failed for %s: %s", dest.name, exc)


async def _set_state(
    session_factory: async_sessionmaker[AsyncSession],
    job_id: str,
    state: str,
    *,
    progress: float | None = None,
    failure_class: str | None = None,
    error: str | None = None,
    track_id: str | None = None,
) -> None:
    """Write one state transition in its own short transaction.

    Each transition commits immediately so SQLite's single writer lock is held
    for milliseconds — never across the download/remux/MB phases in between.
    A long-lived transaction here used to block every other writer (progress
    writes, rate-gate countdowns, approvals) for the duration of a download.
    """
    async with session_factory() as session, session.begin():
        row = await session.get(AcquisitionJobRow, job_id)
        if row is None:
            return
        row.state = state
        row.updated_at = datetime.now(UTC).replace(tzinfo=None)
        if progress is not None:
            row.progress = progress
        if failure_class is not None:
            row.failure_class = failure_class
        if error is not None:
            row.error = error
        if track_id is not None:
            row.track_id = track_id


async def _remux_to_ogg(src: Path, dest_dir: Path) -> Path:
    """Remux WebM/WebA container to OGG without re-encoding the audio stream."""
    out = dest_dir / (src.stem + ".ogg")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", str(src), "-c", "copy", str(out), "-y", "-loglevel", "error",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
    except TimeoutError:
        proc.kill()
        raise TimeoutError("ffmpeg remux timed out after 120s")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg remux failed: {stderr.decode().strip()}")
    return out


async def _find_local_match(
    session: AsyncSession,
    candidate: TrackCandidate,
) -> str | None:
    """Return the internal_id of a local track that confidently matches candidate."""
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload

    from service.db.schema import Track
    from service.search.matcher import is_confident_match

    norm_title = normalize(candidate.title)
    first_word = norm_title.split()[0] if norm_title.split() else norm_title
    stmt = (
        select(Track)
        .join(Track.artist)
        .options(joinedload(Track.artist), joinedload(Track.file))
        .where(Track.title.ilike(f"%{first_word}%"))
    )
    rows = (await session.execute(stmt)).unique().scalars().all()

    for row in rows:
        if is_confident_match(
            candidate.title, candidate.artist, candidate.duration_seconds,
            row.title, row.artist.name, row.duration_seconds,
        ):
            return row.id
    return None


# Minimum source-match score to auto-substitute for an age-gated video. The picker's
# own "no match" floor is 0.35; we hold substitutes a little higher so we only swap in
# a clearly-good alternative, never a weak guess.
_SUBSTITUTE_MIN_SCORE = 0.45
# Original + how many alternative sources we'll try before giving up.
_MAX_SOURCE_ATTEMPTS = 3


async def _download_with_source_fallback(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    job_id: str,
    provider: Provider,
    provider_ref: str,
    candidate: TrackCandidate,
    tmp_dir: Path,
    on_progress: Callable[[float], None] | None,
):
    """Fetch the audio, auto-substituting a non-gated source on a YouTube age-gate.

    When YouTube age-gates the chosen video and no usable login cookies are configured,
    the user still wants the *track* — not this particular upload — so we pick the next
    best non-gated match (`yt_search_best`, excluding ids already tried) and retry,
    bounded by `_MAX_SOURCE_ATTEMPTS`. If cookies are configured we leave the gate to
    them. Returns the FetchResult, or None after recording the failure on the job row.
    """
    from service.providers.ytdlp import (
        cookies_are_configured,
        video_id_from_url,
        yt_search_best,
    )

    active_ref = provider_ref
    tried_ids: set[str] = set()
    first_id = video_id_from_url(provider_ref)
    if first_id:
        tried_ids.add(first_id)

    for attempt in range(_MAX_SOURCE_ATTEMPTS):
        try:
            return await provider.fetch(active_ref, tmp_dir, on_progress=on_progress)
        except Exception as exc:
            fc, err = classify_failure(exc)
            eligible = (
                is_age_gate_error(exc)
                and provider.name == "ytdlp"
                and not cookies_are_configured()
                and bool(candidate.title and candidate.artist)
                and attempt < _MAX_SOURCE_ATTEMPTS - 1
            )
            if not eligible:
                await _set_state(session_factory, job_id, "failed", failure_class=fc, error=err)
                logger.error("Download failed [%s] %s: %s", fc, job_id, err)
                return None

            alt_url, alt_score = await asyncio.to_thread(
                yt_search_best,
                candidate.artist,
                candidate.title,
                candidate.duration_seconds,
                exclude_ids=tried_ids,
            )
            alt_id = video_id_from_url(alt_url)
            if not alt_id or alt_id in tried_ids or alt_score < _SUBSTITUTE_MIN_SCORE:
                await _set_state(session_factory, job_id, "failed", failure_class=fc, error=err)
                logger.error(
                    "Download failed [%s] %s (age-gated, no non-gated alternative ≥ %.2f): %s",
                    fc, job_id, _SUBSTITUTE_MIN_SCORE, err,
                )
                return None

            tried_ids.add(alt_id)
            active_ref = alt_url
            logger.warning(
                "Job %s age-gated (%r); substituting non-gated source %s (score %.2f)",
                job_id, candidate.title, alt_url, alt_score,
            )
    return None


# ── Phase 1 sub-steps (identify) ─────────────────────────────────────────────


@dataclass
class _SourceMeta:
    """Artist/title/etc parsed from the provider's raw metadata (yt-dlp info dict)."""
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    year: int | None = None
    duration_seconds: int | None = None
    raw_title: str | None = None   # provider "title" field as-is
    channel: str | None = None     # uploader/channel name


def _parse_source_metadata(raw_metadata: dict) -> _SourceMeta:
    """Best-effort artist/title extraction from the provider metadata.

    Flat search entries rarely carry ``track``/``artist``; fall back to
    splitting an "Artist - Title" video title, then to the uploader name.
    """
    def _s(key: str) -> str | None:
        v = raw_metadata.get(key)
        s = str(v).strip() if v is not None else ""
        return s if s and s.lower() not in ("none", "unknown") else None

    artist = _s("artist")
    title_raw = _s("track") or _s("title")
    uploader = _s("uploader") or _s("channel")

    if artist:
        title = title_raw
    elif title_raw and " - " in title_raw:
        artist, title = (part.strip() for part in title_raw.split(" - ", 1))
    else:
        title = title_raw
        artist = uploader

    ry = raw_metadata.get("release_year")
    sd = raw_metadata.get("duration")
    return _SourceMeta(
        title=title,
        artist=artist,
        album=_s("album"),
        year=int(ry) if isinstance(ry, (int, float)) and ry else None,
        duration_seconds=int(sd) if isinstance(sd, (int, float)) and sd else None,
        raw_title=_s("title"),
        channel=uploader,
    )


@dataclass
class _IdentifyState:
    """Evolving identification state for one acquisition (Phase 1).

    Starts from the merged tag/provider/candidate metadata and is refined by
    the MB identification step; finally serialized into ResolvedTrackMetadata.
    """
    title: str | None
    artist: str | None
    album: str | None
    year: int | None
    track_number: int | None
    disc_number: int | None
    duration: int | None
    prov_title: str
    prov_artist: str
    prov_album: str
    prov_year: str
    prov_recording: str
    album_locked: bool
    mb_recording_id: str | None = None
    mb_release_id: str | None = None
    # Seeded from candidate so discography/album jobs always carry the RG ID
    # even when the MB lookup returns a result without release_group_id.
    mb_release_group_id: str | None = None
    # Phase 5: the release group we prefer for cohesion — the locked candidate
    # RG, or (Path B) one discovered from a locally-owned album. Biases ranking
    # and stops AcoustID from redefining album grouping.
    preferred_rg: str | None = None
    mb_artist_id: str | None = None
    mb_artist_sort: str | None = None
    # Full credit ("A & B") when `artist` holds only the collapsed primary.
    # Preserved into ORIGINALARTIST so the collapse stays reversible.
    artist_credit: str | None = None
    # The guests alone ("B"), which move into the title rather than into ARTIST.
    artist_guests: str | None = None
    # What `_apply_credit_placement` settled: the credit ARTIST no longer holds
    # (→ ORIGINALARTIST), and whether ARTIST was replaced by the album artist —
    # which decides whether the artist MBID may still travel with it.
    original_artist: str | None = None
    artist_collapsed: bool = False
    mb_original_year: int | None = None
    isrc: str | None = None
    acoustid_confidence: float | None = None
    mb_match_source: str | None = None
    text_search_similarity: float | None = None
    candidate_scores: list[CandidateScore] = field(default_factory=list)
    force_staging_reason: str | None = None

    def flag_for_review(self, reason: str) -> None:
        """Append a force-staging reason (` | `-joined) so the job stops in review."""
        self.force_staging_reason = (
            f"{self.force_staging_reason} | {reason}"
            if self.force_staging_reason else reason
        )


def _merge_source_metadata(
    tagged, src: _SourceMeta, candidate: TrackCandidate, job_id: str
) -> _IdentifyState:
    """Merge embedded tags, provider metadata, and the candidate into the
    initial identification state. Provenance: "tagged" > "provider" > "candidate".

    Album-locked candidates (album coordinator) override album/year/track/disc,
    and a large duration delta versus the locked candidate flags the job as a
    possible wrong track.
    """
    t = tagged
    state = _IdentifyState(
        title=(t.title if t else None) or src.title or candidate.title,
        artist=(t.artist if t else None) or src.artist or candidate.artist,
        album=(t.album if t else None) or src.album or candidate.album,
        year=(t.year if t else None) or src.year,
        track_number=(t.track_number if t else None),
        disc_number=(t.disc_number if t else None),
        duration=(t.duration_seconds if t else None) or candidate.duration_seconds,
        prov_title="tagged" if (t and t.title) else ("provider" if src.title else "candidate"),
        prov_artist="tagged" if (t and t.artist) else ("provider" if src.artist else "candidate"),
        prov_album="tagged" if (t and t.album) else ("provider" if src.album else "candidate"),
        prov_year="tagged" if (t and t.year) else ("provider" if src.year else "candidate"),
        prov_recording="candidate" if candidate.mb_recording_id else "none",
        # Explicit album_locked flag takes priority; also infer from album+track_number
        # for backwards-compatibility with candidates created before the flag existed.
        album_locked=candidate.album_locked or bool(candidate.album and candidate.track_number),
        mb_recording_id=candidate.mb_recording_id,
        mb_release_id=candidate.mb_release_id,
        mb_release_group_id=candidate.mb_release_group_id,
        preferred_rg=candidate.mb_release_group_id,
        mb_artist_id=candidate.mb_artist_id,
        # The album coordinator resolved these off the release tracklist. MB
        # identification overwrites them when it succeeds; keeping them here
        # means a track whose per-recording lookup fails still gets its guest
        # credit handled instead of silently losing it.
        artist_credit=candidate.artist_credit,
    )

    # Wrong-track detection: duration delta versus the locked candidate
    got_dur = t.duration_seconds if t else None
    if state.album_locked and candidate.duration_seconds and got_dur:
        delta = abs(got_dur - candidate.duration_seconds)
        tol = max(30, int(candidate.duration_seconds * 0.2))
        if delta > tol:
            state.flag_for_review(
                f"Duration mismatch: expected ~{candidate.duration_seconds}s, "
                f"got {got_dur}s — may be wrong track"
            )
            logger.warning("Job %s %r: %s", job_id, candidate.title, state.force_staging_reason)

    # Candidate's pre-resolved fields (from the album coordinator) are authoritative
    if candidate.album:
        state.album = candidate.album
        state.prov_album = "candidate:locked" if state.album_locked else "candidate"
    if candidate.year:
        state.year = candidate.year
        state.prov_year = "candidate:locked" if state.album_locked else "candidate"
    if candidate.track_number:
        state.track_number = candidate.track_number
    if candidate.disc_number:
        state.disc_number = candidate.disc_number
    return state


async def _fetch_user_query(
    session_factory: async_sessionmaker[AsyncSession], job_id: str
) -> str | None:
    """The user's raw search query — an intent signal for ranking.

    URL and synthetic queries (re-acquire, replacement) are not user intent
    and return None.
    """
    async with session_factory() as session:
        row = await session.get(AcquisitionJobRow, job_id)
        raw: str | None = row.query if row else None
    if (
        raw
        and not raw.startswith("http")
        and "[re-acquire]" not in raw
        and "[replacement]" not in raw
    ):
        return raw
    return None


async def _identify_locked_recording(
    state: _IdentifyState,
    candidate: TrackCandidate,
    audio_path: Path,
    job_id: str,
) -> tuple[object, bool]:
    """Path A — locked recording ID (album batch jobs).

    Look up the locked recording directly and run AcoustID in parallel to
    verify the downloaded file matches the locked choice. Returns
    (mb_recording_or_None, mb_from_acoustid).
    """
    from service.metadata.acoustid import acoustid_to_mbid
    from service.metadata.musicbrainz import get_recording_by_id
    from service.search.matcher import (
        artist_similarity as _artist_sim,
        title_similarity as _title_sim,
    )

    mb_coro = asyncio.to_thread(
        get_recording_by_id, candidate.mb_recording_id, settings.cache_dir
    )
    acoustid_coro = (
        acoustid_to_mbid(audio_path, settings.acoustid_api_key)
        if settings.acoustid_api_key else asyncio.sleep(0)
    )
    mb, aq_result = await asyncio.gather(mb_coro, acoustid_coro)
    mb_from_acoustid = False
    if mb is not None:
        state.mb_match_source = "locked_recording"
    if aq_result and isinstance(aq_result, tuple):
        acoustid_mbid, state.acoustid_confidence = aq_result
        if acoustid_mbid != candidate.mb_recording_id:
            # A different recording MBID is NOT automatically a wrong track.
            # MusicBrainz holds many distinct recording entities for one song
            # (album cut vs single vs remaster); a fingerprint legitimately
            # matches any of them. Resolve both sides to title/artist — if it's
            # the same song, the fingerprint actually CONFIRMS the audio and we
            # keep the locked recording. Only a genuinely different song is
            # flagged for review.
            _exp_t = (mb.title if mb else None) or candidate.title
            _exp_a = (mb.artist if mb else None) or candidate.artist
            got_rec = await asyncio.to_thread(
                get_recording_by_id, acoustid_mbid, settings.cache_dir
            )
            _got_t = got_rec.title if got_rec else None
            _got_a = got_rec.artist if got_rec else None
            _same_song = (
                _got_t is not None
                and _title_sim(
                    clean_for_search(_exp_t), clean_for_search(_got_t)
                ) >= 0.85
                and (
                    not _exp_a or not _got_a
                    or _artist_sim(_exp_a, _got_a) >= 0.55
                )
            )
            if _same_song:
                # Same song, sibling recording entity — confirmation, not a
                # mismatch. Avoids the confusing "expected X, got X" review flag
                # that looked identical to the user.
                mb_from_acoustid = True
                logger.info(
                    "Job %s %r: AcoustID matched sibling recording %s of the "
                    "same song (expected %s) — accepting as confirmation",
                    job_id, state.title, acoustid_mbid, candidate.mb_recording_id,
                )
            else:
                # Genuinely different song — surface "Title — Artist" so the
                # review card names what was matched vs expected.
                expected_label = f"{_exp_t} — {_exp_a}" if _exp_a else _exp_t
                if _got_t:
                    got_label = f"{_got_t} — {_got_a}" if _got_a else _got_t
                else:
                    got_label = f"recording {acoustid_mbid[:8]}…"
                state.flag_for_review(
                    f"Fingerprint mismatch: expected “{expected_label}”, "
                    f"got “{got_label}”"
                )
                logger.warning(
                    "Job %s %r: AcoustID mismatch (expected %s [%s], got %s [%s])",
                    job_id, state.title, candidate.mb_recording_id, expected_label,
                    acoustid_mbid, got_label,
                )
        else:
            mb_from_acoustid = True  # AcoustID confirmed the locked recording
    return mb, mb_from_acoustid


async def _identify_by_ranking(
    state: _IdentifyState,
    audio_path: Path,
    clean_query: str | None,
    session_factory: async_sessionmaker[AsyncSession],
    job_id: str,
) -> tuple[object, bool]:
    """Path B — standalone downloads: multi-signal candidate ranking.

    - User query encodes intent (live vs studio, explicit etc.)
    - MB text search on yt-dlp metadata provides the candidate pool
    - AcoustID fingerprint runs in parallel; contributes a boost when it agrees
      with a text-search candidate, or adds a new candidate when text search
      finds nothing.

    Final score = text_sim + 0.10 × query_sim + 0.15 × acoustid_boost.
    mb_from_acoustid = True only when AcoustID was essential (text_sim < 0.85).
    Returns (mb_recording_or_None, mb_from_acoustid); stores the ranked pool on
    the state for review-card observability.
    """
    from service.metadata.acoustid import acoustid_to_mbid
    from service.metadata.candidates import rank_candidates
    from service.metadata.musicbrainz import (
        get_recording_by_id,
        get_recording_candidates,
    )
    from service.search.matcher import DEDUP_THRESHOLD as _DEDUP

    # Phase 5: if the user already owns an album by this artist with a matching
    # title, bias retrieval toward its release group so we don't fragment it
    # with a remaster / alternate edition.
    if not state.preferred_rg and state.album:
        from service.library.cohesion import find_local_release_group
        async with session_factory() as rg_session:
            state.preferred_rg = await find_local_release_group(
                rg_session, state.album, state.artist
            )
        if state.preferred_rg:
            logger.info(
                "Path B cohesion: %r / %r → preferred release group %s",
                state.artist, state.album, state.preferred_rg,
            )

    # Fetch candidate pool and AcoustID fingerprint in parallel.
    candidates_coro = asyncio.to_thread(
        get_recording_candidates,
        clean_for_search(state.title),
        clean_for_search(state.artist),
        state.duration,
        settings.cache_dir,
        state.preferred_rg,
    )
    acoustid_coro = (
        acoustid_to_mbid(audio_path, settings.acoustid_api_key)
        if settings.acoustid_api_key else asyncio.sleep(0)
    )
    _candidates, aq_result = await asyncio.gather(candidates_coro, acoustid_coro)

    acoustid_mbid: str | None = None
    if aq_result and isinstance(aq_result, tuple):
        acoustid_mbid, state.acoustid_confidence = aq_result
        # If AcoustID found a recording not in the candidate pool, add it
        if not any(r.recording_id == acoustid_mbid for r, _ in _candidates):
            _bonus = await asyncio.to_thread(
                get_recording_by_id, acoustid_mbid, settings.cache_dir
            )
            if _bonus is not None:
                from service.search.matcher import track_similarity as _track_sim
                _base_sim = _track_sim(
                    clean_for_search(state.title), clean_for_search(state.artist),
                    state.duration,
                    _bonus.title, _bonus.artist, _bonus.duration_seconds,
                )
                _candidates.append((_bonus, _base_sim))

    # Score each candidate with all three signals (pure, testable)
    _ranked = rank_candidates(
        _candidates,
        clean_query=clean_query,
        acoustid_mbid=acoustid_mbid,
        clean_artist=clean_for_search(state.artist) if state.artist else None,
    )
    # Persist the ranked pool (top 5) with component scores for the review card.
    state.candidate_scores = [
        CandidateScore(
            recording_id=c.recording.recording_id,
            title=c.recording.title,
            artist=c.recording.artist,
            text_sim=c.text_sim,
            query_sim=c.query_sim,
            acoustid_match=c.acoustid_match,
            combined=c.combined,
            artist_sim=c.artist_sim,
            artist_penalty=c.artist_penalty,
        )
        for c in _ranked[:5]
    ]

    if not _ranked:
        return None, False
    top = _ranked[0]
    if top.recording is None or top.combined < _DEDUP:
        return None, False

    mb = top.recording
    mb.score = top.combined
    # AcoustID is "essential" only when it rescued a below-threshold text match
    mb_from_acoustid = (
        acoustid_mbid == mb.recording_id and top.text_sim < _DEDUP
    )
    state.mb_match_source = "acoustid" if mb_from_acoustid else "text_search"
    # Expose the raw title+artist score (before query/AcoustID bonuses) for
    # fast-approve eligibility in the review UI — a fairer measure of MB match
    # quality than the boosted combined score.
    if not mb_from_acoustid:
        state.text_search_similarity = top.text_sim
    logger.info(
        "MB ranked: %r → %s (text=%.2f combined=%.2f acoustid=%s query=%s)",
        state.title, mb.recording_id, top.text_sim, top.combined,
        bool(acoustid_mbid == mb.recording_id), bool(clean_query),
    )
    return mb, mb_from_acoustid


def _apply_mb_result(
    state: _IdentifyState,
    mb,
    mb_from_acoustid: bool,
    candidate: TrackCandidate,
    source_modifiers,
    job_id: str,
) -> None:
    """Fold the winning MB recording into the state and run the wrong-track gates.

    Locked candidates keep their recording/release-group IDs authoritative;
    the gates (artist mismatch on Path B, live/cover modifier incompatibility)
    flag the job for review rather than rejecting it.
    """
    from service.core.modifiers import modifier_mismatch_reason
    from service.core.normalize import extract_modifiers as _extract_modifiers

    resolved_recording_id = mb.recording_id
    # When the candidate has a locked recording ID, only accept a different
    # recording via AcoustID fingerprint — text search is not authoritative.
    recording_id_overridden = False
    if candidate.mb_recording_id and not mb_from_acoustid:
        if resolved_recording_id != candidate.mb_recording_id:
            logger.info(
                "Text search returned different recording %s (expected %s) for %r "
                "— keeping locked recording_id",
                resolved_recording_id, candidate.mb_recording_id, state.title,
            )
            resolved_recording_id = candidate.mb_recording_id
            recording_id_overridden = True

    state.mb_recording_id = resolved_recording_id
    state.mb_release_id = state.mb_release_id or mb.release_id
    state.mb_artist_id = mb.artist_id
    state.mb_artist_sort = mb.artist_sort
    state.mb_original_year = mb.original_year
    # Album-locked downloads: the release group was chosen in Discover
    # (acquire_album_from_mb seeds candidate.mb_release_group_id from the
    # release group the user picked). It is authoritative for the whole album —
    # a single recording can belong to many release groups (single /
    # compilation / album), so a per-recording MB lookup must NOT be allowed to
    # redefine which album these tracks belong to. For standalone jobs, keep
    # candidate's RG only as a fallback when MB omits it.
    if state.album_locked and candidate.mb_release_group_id:
        state.mb_release_group_id = candidate.mb_release_group_id
    else:
        state.mb_release_group_id = mb.release_group_id or state.mb_release_group_id
    # Phase 5 AcoustID boundary: a fingerprint match may pick the right
    # RECORDING but must not redefine the album's release group when we hold a
    # strong local cohesion signal — re-anchor to the owned RG.
    if state.preferred_rg and mb_from_acoustid and state.mb_release_group_id != state.preferred_rg:
        logger.info(
            "Keeping owned release group %s over AcoustID's %s for %r",
            state.preferred_rg, state.mb_release_group_id, state.title,
        )
        state.mb_release_group_id = state.preferred_rg
    state.isrc = mb.isrc
    state.prov_recording = state.mb_match_source or "mb"
    # Only accept title/artist from MB if the MB result actually corresponds
    # to the track we expected.  When the recording ID was overridden back to
    # the candidate's value, the MB result is for the WRONG track — using its
    # title/artist would mislabel the file (e.g. "For Whom the Bell Tolls"
    # instead of "The God That Failed").
    if mb.title and not recording_id_overridden:
        state.title = mb.title
        state.prov_title = f"mb:{state.mb_match_source}"
    if mb.artist and not recording_id_overridden:
        state.artist = mb.artist
        state.artist_credit = mb.artist_credit
        state.artist_guests = mb.artist_guests
        state.prov_artist = f"mb:{state.mb_match_source}"
    if not state.album_locked:
        if mb.album:
            state.album = mb.album
            state.prov_album = f"mb:{state.mb_match_source}"
        if mb.year:
            state.year = mb.year
            state.prov_year = "mb:release"
        state.track_number = mb.track_number or state.track_number
    if mb.original_year:
        state.prov_year = "mb:original"

    # ── Phase 3 incompatibility gates ─────────────────────────────────────────
    # Run regardless of match source. Skipped when the recording ID was
    # overridden back to the locked candidate — there mb.title/artist describe
    # the WRONG track and would mis-fire. AcoustID can rescue a weak text match
    # but does NOT exempt a track from these gates.
    if recording_id_overridden:
        return

    # (a) Artist mismatch — Path B only (all match sources, not just AcoustID).
    #     Catches both fingerprint wrong-matches and text-search candidates
    #     from a different artist (title dominated the score). Skipped on
    #     Path A: there candidate.artist is the ALBUM artist and mb.artist
    #     comes from the very recording we locked, so a mismatch is expected
    #     for VA/feature tracks and says nothing about the downloaded file —
    #     the AcoustID fingerprint check is Path A's wrong-file gate.
    if not candidate.mb_recording_id and candidate.artist and mb.artist:
        from service.search.matcher import artist_similarity as _artist_sim
        a_sim = _artist_sim(candidate.artist, mb.artist)
        if a_sim < 0.55:
            _how = "fingerprint identified" if mb_from_acoustid else "MB matched"
            mismatch = (
                f"Artist mismatch: expected \"{candidate.artist}\", "
                f"{_how} \"{mb.artist}\" (similarity {a_sim:.2f}) — may be wrong track"
            )
            state.flag_for_review(mismatch)
            logger.warning("Job %s %r: %s", job_id, candidate.title, mismatch)

    # (b) Modifier incompatibility — e.g. a live/cover/karaoke source that
    #     matched a studio/original MB recording. The MB winner's own
    #     title+album supply its flags.
    mb_modifiers = _extract_modifiers(f"{mb.title or ''} {mb.album or ''}")
    mod_reason = modifier_mismatch_reason(source_modifiers, mb_modifiers)
    if mod_reason:
        state.flag_for_review(mod_reason)
        logger.warning("Job %s %r: %s", job_id, candidate.title, mod_reason)


def _check_title_mismatch(
    state: _IdentifyState, candidate: TrackCandidate, job_id: str
) -> None:
    """Flag the job when the resolved title is substantially different from the
    candidate's expected title — the wrong track was likely downloaded."""
    if not (candidate.title and state.title):
        return
    from service.search.matcher import title_similarity as _title_sim
    t_sim = _title_sim(clean_for_search(candidate.title), clean_for_search(state.title))
    if t_sim < 0.55:
        mismatch_note = (
            f"Title mismatch: expected \"{candidate.title}\", "
            f"got \"{state.title}\" (similarity {t_sim:.2f}) — may be wrong track"
        )
        state.flag_for_review(mismatch_note)
        logger.warning("Job %s: %s", job_id, mismatch_note)


async def _identify_recording(
    state: _IdentifyState,
    candidate: TrackCandidate,
    audio_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
    job_id: str,
) -> None:
    """Phase 1 step 3b/3c: resolve the MB recording and run the wrong-track gates.

    Two paths:
      A. Locked recording ID (album batch jobs): look up directly, then run
         AcoustID in parallel to verify the choice is correct.
      B. Standalone: multi-signal ranking over a candidate pool.

    Never raises — identification failure flags the job for review instead
    (an untagged, unverified track must not silently look clean).
    """
    try:
        raw_query = await _fetch_user_query(session_factory, job_id)
        clean_query = clean_for_search(raw_query) if raw_query else None

        # Phase 3: capture the source's semantic modifiers (live/cover/karaoke/…)
        # from the download title + user query BEFORE MB overwrites the title.
        from service.core.normalize import extract_modifiers as _extract_modifiers
        source_modifiers = _extract_modifiers(f"{state.title} {raw_query or ''}")

        if candidate.mb_recording_id:
            mb, mb_from_acoustid = await _identify_locked_recording(
                state, candidate, audio_path, job_id
            )
        else:
            mb, mb_from_acoustid = await _identify_by_ranking(
                state, audio_path, clean_query, session_factory, job_id
            )

        if mb is not None:
            _apply_mb_result(state, mb, mb_from_acoustid, candidate, source_modifiers, job_id)

        _check_title_mismatch(state, candidate, job_id)

    except Exception as mb_exc:
        # Identification failing is NOT business as usual — without it the track
        # reaches review untagged and unverified. Log loudly and tell the review
        # card why there's no MB match.
        logger.warning(
            "Job %s %r: MB/AcoustID identification failed: %s",
            job_id, state.title, mb_exc,
        )
        state.flag_for_review(
            f"Identification failed ({type(mb_exc).__name__}): {str(mb_exc)[:160]}"
        )


async def _fetch_mb_genres(release_group_id: str | None) -> list[str]:
    """MB folksonomy genres for the review card (best-effort)."""
    if not release_group_id:
        return []
    try:
        from service.metadata.musicbrainz import get_release_group_genres
        return await asyncio.to_thread(
            get_release_group_genres, release_group_id, settings.cache_dir
        )
    except Exception as genre_exc:
        logger.debug("Genre fetch skipped: %s", genre_exc)
        return []


def _resolve_albumartist(
    state: _IdentifyState, candidate: TrackCandidate
) -> tuple[str, bool]:
    """Pick the ALBUMARTIST and decide whether this is a compilation track.

    ALBUMARTIST never carries featuring credits: an "A feat. B" track must
    group under A, not fragment the library into a separate "A feat. B"
    artist. Guests stay in the per-track ARTIST tag.

    On an album-locked job the coordinator has already resolved the album artist
    against the whole release (including the various-artists check that no single
    track can make on its own), so its verdict is authoritative. `candidate.artist`
    is the per-track performer there and must not be mistaken for the album artist.
    """
    if state.album_locked:
        albumartist = candidate.albumartist or candidate.artist
    else:
        from service.library.tagger import primary_artist
        albumartist = primary_artist(state.artist)
    is_compilation = state.album is not None and (
        candidate.is_compilation
        or albumartist.lower() in ("various artists", "various")
    )
    if is_compilation:
        # One name for every compilation, whatever the release was credited to,
        # so they all land in /music/Compilations/ and group as one album artist.
        albumartist = VARIOUS_ARTISTS_NAME
    return albumartist, is_compilation


def _apply_credit_placement(
    state: _IdentifyState, albumartist: str, is_compilation: bool
) -> None:
    """Decide what ARTIST and TITLE actually say — the single place that does.

    Two kinds of credit fragment a Navidrome artist list, and both are resolved
    the same way: ARTIST keeps the name that identifies the album, the credit
    that would have split it moves into the title, and the verbatim credit is
    preserved in ORIGINALARTIST so the substitution is reversible from the file.

      * a guest on a track ("A med B")  → "Eg ser (feat. B)", ARTIST = A
      * a performer on a compilation    → "Silent Night (B)", ARTIST = Various
        Artists — otherwise a 20-track "Now …" adds 20 one-track artists

    A compilation performer is not a guest, so the whole credit is appended
    verbatim without a "feat."; because that credit already names any guest, the
    guest step below then finds it in the title and adds nothing.

    Runs after the wrong-track gates, so a "(…)" suffix can never drag the title
    similarity down and flag a track that matched perfectly well.
    """
    from service.config import compilation_artist_mode
    from service.library.tagger import title_with_guests, title_with_performer

    performer = (state.artist or "").strip()
    mode = compilation_artist_mode()
    if is_compilation and mode != "keep" and performer and performer != albumartist:
        # The full credit, so the title and ORIGINALARTIST carry the guests too.
        credit = (state.artist_credit or performer).strip()
        if mode == "append_to_title":
            state.title = title_with_performer(state.title, credit)
        state.artist = albumartist
        state.original_artist = credit
        # ARTIST no longer names the artist this MBID identifies, and Navidrome
        # keys identity on the MBID as much as on the name — keeping it would say
        # the performer and "Various Artists" are the same artist. What it
        # becomes is the album artist's ID, settled in `_stage_for_review`.
        state.mb_artist_id = None
        state.artist_collapsed = True

    if state.title and state.artist_guests:
        state.title = title_with_guests(state.title, state.artist_guests)

    if state.original_artist is None and state.artist_credit != state.artist:
        state.original_artist = state.artist_credit or None


async def _stage_for_review(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    job_id: str,
    state: _IdentifyState,
    candidate: TrackCandidate,
    fetch_result,
    src: _SourceMeta,
    audio_path: Path,
    albumartist: str,
    is_compilation: bool,
    mb_genres: list[str],
    quality_score: float,
) -> Path | None:
    """Phase 1 steps 4–5: atomically place the file in /music-staging and store
    the resolved metadata on the job row → needs_review.

    Returns the staging path, or None when placement failed (the failure state
    is already recorded on the job row).
    """
    await _set_state(session_factory, job_id, "importing")

    ext = audio_path.suffix.lstrip(".")
    staging_dest = track_path(
        settings.staging_dir,
        artist=state.artist,
        album=state.album,
        year=state.year,
        track_number=state.track_number,
        disc_number=state.disc_number,
        title=state.title,
        ext=ext,
        albumartist=albumartist,
    )

    try:
        await asyncio.to_thread(atomic_place, audio_path, staging_dest)
    except Exception as exc:
        await _set_state(session_factory, job_id, "failed", failure_class="transient", error=str(exc))
        logger.error("Staging placement failed %s: %s", job_id, exc)
        return None

    # Navidrome keys artist identity on the MBID as well as the name, so the
    # album artist's MBID may only be written when the album artist really IS
    # that MB artist. On a compilation ("Various Artists") or wherever the name
    # was reshaped, the performer's MBID would merge two different artists.
    mb_albumartist_id = candidate.mb_albumartist_id if state.album_locked else None
    if mb_albumartist_id is None and albumartist == state.artist:
        # None once a compilation performer was collapsed out of ARTIST — the
        # performer's ID was cleared there precisely so it can't arrive here.
        mb_albumartist_id = state.mb_artist_id
    if state.artist_collapsed:
        # ARTIST now holds the album artist, so the artist MBID must be the album
        # artist's as well: MusicBrainz' own Various Artists entity, or nothing.
        state.mb_artist_id = mb_albumartist_id

    # The credit ARTIST gave up — a collapsed guest, or a compilation performer.
    original_artist = state.original_artist

    resolved_metadata = ResolvedTrackMetadata(
        title=state.title,
        artist=state.artist,
        albumartist=albumartist,
        original_artist=original_artist,
        album=state.album,
        year=state.year,
        original_year=state.mb_original_year,
        track_number=state.track_number,
        disc_number=state.disc_number,
        duration_seconds=state.duration,
        ext=ext,
        source_codec=fetch_result.codec,
        source_bitrate_kbps=fetch_result.bitrate_kbps,
        source_url=fetch_result.source_url,
        source_title=src.raw_title,
        source_channel=src.channel,
        source_duration_seconds=src.duration_seconds,
        mb_recording_id=state.mb_recording_id,
        mb_release_id=state.mb_release_id,
        mb_release_group_id=state.mb_release_group_id,
        mb_artist_id=state.mb_artist_id,
        mb_albumartist_id=mb_albumartist_id,
        mb_artist_sort=state.mb_artist_sort,
        isrc=state.isrc,
        acoustid_confidence=state.acoustid_confidence,
        text_search_similarity=state.text_search_similarity,
        mb_match_source=state.mb_match_source,
        candidates=state.candidate_scores,
        is_compilation=is_compilation,
        force_staging_reason=state.force_staging_reason,
        quality_score=quality_score,
        thumbnail_url=candidate.thumbnail_url,
        mb_genres=mb_genres,
        # Metadata provenance: which source contributed each key field
        prov_title=state.prov_title,
        prov_artist=state.prov_artist,
        prov_album=state.prov_album,
        prov_year=state.prov_year,
        prov_recording=state.prov_recording,
        # Propagate replacement flag so place_approved_track can trash the old file
        is_replacement=candidate.skip_dedup,
        replace_path=candidate.replace_path,
    )

    async with session_factory() as session, session.begin():
        row = await session.get(AcquisitionJobRow, job_id)
        if row is not None:
            row.state = "needs_review"
            row.staging_path = str(staging_dest)
            row.resolved_metadata_json = resolved_metadata.model_dump_json()
            # Always (re)assign — None clears any stale "⏳ Pacing downloads…"
            # back-off message left on the row by the rate gate, so a completed
            # review card doesn't keep showing a countdown that already elapsed.
            row.error = state.force_staging_reason
            row.updated_at = datetime.now(UTC).replace(tzinfo=None)

    logger.info(
        "Identify done (source=%s, quality=%.0f%%): %s → staged at %s",
        state.mb_match_source or "none", quality_score * 100, job_id, staging_dest,
    )
    return staging_dest


async def run_acquisition(
    *,
    job_id: str,
    provider: Provider,
    provider_ref: str,
    candidate: TrackCandidate,
    tmp_acquire_dir: Path,
    session_factory: async_sessionmaker[AsyncSession],
    scan_trigger: ScanTrigger | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> None:
    """Phase 1 (identify): download, fingerprint, MB lookup, stage for review.

    Places the file in /music-staging and stores resolved_metadata_json on the
    job row. Sets state to needs_review. Never raises — errors go to the job row.

    Takes a session factory, not a session: every DB touch opens its own short
    transaction so no SQLite write lock is ever held across the download or the
    MusicBrainz/AcoustID network phases (which can run for minutes).
    """
    # ── 0. Dedup check ────────────────────────────────────────────────────────
    if not candidate.skip_dedup:
        try:
            async with session_factory() as session:
                local_match = await _find_local_match(session, candidate)
            if local_match is not None:
                logger.info("Dedup: skipping — local match exists: %s", local_match)
                await _set_state(session_factory, job_id, "done", track_id=local_match)
                return
        except Exception as exc:
            logger.debug("Dedup check failed (continuing): %s", exc)

    tmp_acquire_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=tmp_acquire_dir) as tmp_str:
        tmp_dir = Path(tmp_str)

        # ── 1. Download ────────────────────────────────────────────────────────
        await _set_state(session_factory, job_id, "downloading")
        fetch_result = await _download_with_source_fallback(
            session_factory=session_factory,
            job_id=job_id,
            provider=provider,
            provider_ref=provider_ref,
            candidate=candidate,
            tmp_dir=tmp_dir,
            on_progress=on_progress,
        )
        if fetch_result is None:
            return  # failure state already recorded on the job row

        # The download may have taken minutes: re-check the row before investing
        # in identification. The user may have cancelled, or stuck-job recovery
        # may have re-queued the job to another worker (state back to "queued") —
        # continuing would race that worker and stage the same track twice.
        async with session_factory() as session:
            row_after_dl = await session.get(AcquisitionJobRow, job_id)
        if row_after_dl is None or row_after_dl.state in ("cancelled", "queued"):
            logger.info(
                "Job %s state became %s during download; discarding result",
                job_id, row_after_dl.state if row_after_dl else "deleted",
            )
            return

        audio_path = fetch_result.file_path
        src = _parse_source_metadata(fetch_result.raw_metadata)

        # ── 2. Remux if needed ─────────────────────────────────────────────────
        await _set_state(session_factory, job_id, "processing")
        if audio_path.suffix.lower() in _REMUX_CONTAINERS:
            try:
                audio_path = await _remux_to_ogg(audio_path, tmp_dir)
            except Exception as exc:
                await _set_state(session_factory, job_id, "failed", failure_class="transient", error=str(exc))
                logger.error("Remux failed %s: %s", job_id, exc)
                return

        # ── 3. Read tags + merge with provider/candidate metadata ─────────────
        await _set_state(session_factory, job_id, "tagging")
        tagged = read_tags(audio_path)
        state = _merge_source_metadata(tagged, src, candidate, job_id)

        # ── 3b/3c. MB identification + wrong-track gates ───────────────────────
        await _identify_recording(state, candidate, audio_path, session_factory, job_id)

        # When no MB match, clean the raw YouTube title/artist so the review card
        # shows a sensible default instead of "(Official Music Video)" noise.
        if state.mb_match_source is None:
            state.title = clean_for_search(state.title)
            state.artist = clean_for_search(state.artist)

        mb_genres = await _fetch_mb_genres(state.mb_release_group_id)
        albumartist, is_compilation = _resolve_albumartist(state, candidate)
        # Needs the album artist, so it can only run once that is resolved.
        _apply_credit_placement(state, albumartist, is_compilation)
        quality_score = compute_quality_score(
            title=state.title,
            artist=state.artist,
            album=state.album,
            year=state.year,
            track_number=state.track_number,
            musicbrainz_recording_id=state.mb_recording_id,
            has_cover_art=False,
        )

        # ── 4/5. Stage in /music-staging + store metadata → needs_review ──────
        staging_dest = await _stage_for_review(
            session_factory=session_factory,
            job_id=job_id,
            state=state,
            candidate=candidate,
            fetch_result=fetch_result,
            src=src,
            audio_path=audio_path,
            albumartist=albumartist,
            is_compilation=is_compilation,
            mb_genres=mb_genres,
            quality_score=quality_score,
        )
        if staging_dest is None:
            return

        # ── Source replacement: auto-approve when no flags raised ──────────────
        # The user already previewed and explicitly chose the replacement source;
        # the rest of the track's metadata stays unchanged. Skip the review queue
        # unless something looks wrong (force_staging_reason set).
        if candidate.skip_dedup and not state.force_staging_reason:
            logger.info("Auto-approving replacement job %s — user already vetted source", job_id)
            try:
                async with session_factory() as approve_session, approve_session.begin():
                    await place_approved_track(job_id, {}, approve_session, scan_trigger)
            except Exception as exc:
                logger.warning(
                    "Auto-approve for replacement %s failed — left in needs_review: %s",
                    job_id, exc,
                )


# ── Phase 2 sub-steps (approve → place) ──────────────────────────────────────


def _apply_review_overrides(meta: ResolvedTrackMetadata, overrides: dict[str, str | None]) -> None:
    """Apply user-supplied review-form overrides to the resolved metadata.

    Non-empty string values win; empty strings clear the field. An artist
    override without an albumartist override re-derives albumartist sans
    featuring credit, so a "feat." edit can't split the album grouping.
    """
    previous_artist = meta.artist
    for k in ("title", "artist", "album", "mb_recording_id", "mb_release_id", "genre"):
        if k in overrides:
            val = (overrides[k] or "").strip()
            setattr(meta, k, val or None)
    for k in ("year", "track_number", "disc_number"):
        if k in overrides:
            raw = (overrides[k] or "").strip()
            if raw:
                try:
                    setattr(meta, k, int(raw))
                except (ValueError, TypeError):
                    pass
            else:
                setattr(meta, k, None)

    if "artist" in overrides and meta.artist != previous_artist:
        # The MBID names an artist; the name the user just typed is a different
        # one. Navidrome keys artist identity on both, so keeping the old ID
        # would silently merge the two.
        meta.mb_artist_id = None
        # A compilation's album artist is the shared "Various Artists", not
        # something derived from any one performer's credit — re-deriving it
        # from an edited track artist would split the compilation apart.
        if "albumartist" not in overrides and not meta.is_compilation:
            from service.library.tagger import primary_artist
            meta.albumartist = primary_artist(meta.artist)
            meta.mb_albumartist_id = None
        # Typing the collapsed performer back into ARTIST undoes the collapse,
        # so there is no longer a replaced credit for ORIGINALARTIST to preserve.
        if meta.original_artist and meta.original_artist == meta.artist:
            meta.original_artist = None


async def _apply_album_cohesion(
    session: AsyncSession,
    *,
    album: str,
    albumartist: str,
    year: int | None,
    mb_artist_id: str | None,
    mb_release_group_id: str | None,
) -> tuple[str, str, int | None, str | None, bool]:
    """Anchor the track to the existing local album grouping.

    Returns (album, albumartist, year, canonical_release_id, found): the
    locally-established names/year win over the incoming metadata, and the
    canonical MUSICBRAINZ_ALBUMID is authoritative *even when None* — a
    mismatch in that tag causes Navidrome to split the album.
    """
    from service.library.cohesion import find_canonical_album, stable_albumartist

    albumartist = await stable_albumartist(session, albumartist, mb_artist_id)
    canonical = await find_canonical_album(session, album, albumartist, mb_release_group_id)
    if canonical is None:
        return album, albumartist, year, None, False
    album, albumartist, canonical_year, canonical_release_id = canonical
    if canonical_year is not None:
        year = canonical_year
    return album, albumartist, year, canonical_release_id, True


async def _trash_replaced_files(
    session: AsyncSession, meta: ResolvedTrackMetadata, dest: Path
) -> None:
    """Source replacement: trash the original library file (and anything already
    at the destination) before the new version lands.

    The original is trashed by its recorded path — a replacement that remuxes
    to a different extension (e.g. .mp3 → .ogg) recomputes a `dest` that no
    longer matches the original, so trashing only `dest` would leave the old
    file behind and Navidrome would show a duplicate.
    """
    from service.db.schema import TrackFile
    from service.library.writer import safe_trash as _safe_trash

    trash_dir = settings.music_dir / ".trash"
    original = Path(meta.replace_path) if meta.replace_path else None
    if original is not None and original != dest and original.exists():
        await asyncio.to_thread(_safe_trash, original, trash_dir)
        # Drop the stale DB row so the library doesn't list both files
        # before the next full scan reconciles it.
        await session.execute(
            sa_delete(TrackFile).where(TrackFile.path == str(original))
        )
        logger.info("Approve replacement: trashed original file at %s", original)
    # Also trash anything already sitting at the destination so the new
    # version actually lands instead of being skipped (same-extension case).
    if dest.exists():
        await asyncio.to_thread(_safe_trash, dest, trash_dir)
        logger.info("Approve replacement: trashed old file at %s", dest)


async def _embed_release_artwork(
    dest: Path, mb_release_id: str | None, thumbnail_url: str | None
) -> None:
    """Fetch cover art (cached — cheap on second call) and embed it + write the
    cover.jpg sidecar. Best-effort: art must never block an approval."""
    if not mb_release_id:
        return
    artwork_bytes: bytes | None = None
    try:
        from service.metadata.artwork import fetch_artwork
        artwork_bytes = await fetch_artwork(
            release_mbid=mb_release_id,
            thumbnail_url=thumbnail_url,
            cache_dir=settings.cache_dir,
        )
    except Exception as exc:
        logger.debug("Approve: artwork fetch failed: %s", exc)
    if not artwork_bytes:
        return
    try:
        await asyncio.to_thread(write_tags, dest, artwork_bytes=artwork_bytes)
    except Exception as exc:
        logger.debug("Approve: artwork embed failed: %s", exc)
    write_cover_jpg(dest.parent, artwork_bytes)


def _spawn_lyrics_task(
    dest: Path, artist: str, title: str, album: str | None, duration_seconds: int | None
) -> None:
    """Start the LRCLIB sidecar fetch as a fire-and-forget background task.

    The network fetch added a serial round-trip to every approval, which adds
    up fast in a batch. It only touches the sidecar file, never the audio or
    the DB, so nothing downstream depends on it.
    """
    if not settings.lyrics_enabled:
        return
    try:
        from service.metadata.lyrics import has_lyrics_sidecar
        if not has_lyrics_sidecar(dest):
            task = asyncio.create_task(
                _write_lyrics_sidecar(dest, artist, title, album, duration_seconds)
            )
            _bg_tasks.add(task)
            task.add_done_callback(_bg_tasks.discard)
    except Exception as exc:
        logger.debug("Approve: lyrics task not started: %s", exc)


async def _retag_sibling_release_ids(
    session: AsyncSession,
    album_id: str,
    placed_track_id: str,
    effective_release_id: str,
) -> None:
    """Normalize MUSICBRAINZ_ALBUMID on sibling tracks of the album.

    If this track has a release ID and existing siblings don't (or differ),
    rewrite their file tags now so Navidrome groups them as one album — the
    "Fix file tags" operation users had to run manually. Writes only when the
    tag actually differs: unconditional writes made batch approval O(n²) in
    tag rewrites and churned every sibling's mtime, forcing full re-scans.
    """
    from sqlalchemy import select as _sel_sib
    from sqlalchemy.orm import joinedload as _jl_sib

    from service.db.schema import Track as _SibTrack

    sibling_tracks = (await session.execute(
        _sel_sib(_SibTrack)
        .options(_jl_sib(_SibTrack.file))
        .where(
            _SibTrack.album_id == album_id,
            _SibTrack.id != placed_track_id,
        )
    )).unique().scalars().all()
    for sib in sibling_tracks:
        if not sib.file:
            continue
        sib_fp = Path(sib.file.path)
        if not sib_fp.exists():
            continue
        try:
            sib_tags = await asyncio.to_thread(read_tags, sib_fp)
            if sib_tags is not None and sib_tags.mb_release_id == effective_release_id:
                continue
            await asyncio.to_thread(
                write_tags, sib_fp, mb_release_id=effective_release_id
            )
        except Exception as sib_exc:
            logger.debug("Approve: sibling retag failed for %s: %s", sib_fp, sib_exc)


async def _index_placed_track(
    session: AsyncSession,
    dest: Path,
    *,
    fallback_track_id: str,
    title: str,
    artist: str,
    album: str | None,
    year: int | None,
    track_number: int | None,
    genre: str | None,
    mb_recording_id: str | None,
    mb_release_id: str | None,
    mb_release_group_id: str | None,
    mb_artist_id: str | None,
    is_enrichment: bool,
) -> tuple[str, str | None]:
    """Index the placed file and propagate MB IDs to the Track/Artist/Album rows.

    Runs inside a savepoint so a flush failure only rolls back the indexing —
    the outer transaction stays clean and the job-state update after it always
    succeeds (the scanner will pick the file up later). Returns
    (track_id, album_id) — track_id falls back to the caller's hash when
    indexing failed, album_id is None in that case.
    """
    from service.db.schema import Track as _Track

    track_id = fallback_track_id
    album_id: str | None = None
    try:
        async with session.begin_nested():
            # Clear any tombstone for this recording so the user's explicit
            # approval re-admits the track to the library (tombstones block the
            # background scanner, not conscious user re-acquisition).
            if mb_recording_id:
                from service.db.schema import DeletedTrack as _DeletedTrack
                tombstones = (await session.execute(
                    select(_DeletedTrack).where(
                        _DeletedTrack.mb_recording_id == mb_recording_id
                    )
                )).scalars().all()
                for tombstone in tombstones:
                    await session.delete(tombstone)
                    await session.flush()

            indexed_track_id = await index_file(session, dest)
            if indexed_track_id:
                track_id = indexed_track_id
            hca = await asyncio.to_thread(has_cover_art, dest)
            # Eager-load the file relationship: async SQLAlchemy can't lazy-load
            # it synchronously when accessed below (greenlet_spawn error otherwise).
            from sqlalchemy.orm import selectinload as _selin_file
            track_row = await session.get(
                _Track, track_id, options=[_selin_file(_Track.file)]
            )
            if track_row is not None:
                album_id = track_row.album_id
                if mb_recording_id:
                    track_row.musicbrainz_recording_id = mb_recording_id
                if genre:
                    track_row.genre = genre
                if track_row.file:
                    track_row.file.has_cover_art = hca
                track_row.tag_quality_score = compute_quality_score(
                    title=title,
                    artist=artist,
                    album=album,
                    year=year,
                    track_number=track_number,
                    musicbrainz_recording_id=mb_recording_id,
                    has_cover_art=hca,
                )
                # Store MB artist ID on Artist row for artist page discography
                if mb_artist_id and track_row.artist_id:
                    from service.db.schema import Artist as _Artist
                    artist_row = await session.get(_Artist, track_row.artist_id)
                    if artist_row is not None and not artist_row.musicbrainz_artist_id:
                        artist_row.musicbrainz_artist_id = mb_artist_id
                # Store MB IDs on Album row for cohesion on future tracks
                if track_row.album_id and (mb_release_group_id or mb_release_id):
                    from service.db.schema import Album as _Album
                    album_row = await session.get(_Album, track_row.album_id)
                    if album_row is not None:
                        if mb_release_group_id and not album_row.mb_release_group_id:
                            album_row.mb_release_group_id = mb_release_group_id
                        # First track to land in an album sets the release ID; all
                        # subsequent tracks inherit it (prevents Navidrome album
                        # splits caused by different editions matching different
                        # release IDs)
                        if mb_release_id and not album_row.musicbrainz_release_id:
                            album_row.musicbrainz_release_id = mb_release_id
                        effective_release_id = mb_release_id or album_row.musicbrainz_release_id
                        if effective_release_id and not is_enrichment:
                            await _retag_sibling_release_ids(
                                session, track_row.album_id, track_id, effective_release_id
                            )
                await session.flush()
    except Exception as exc:
        logger.warning("Approve: DB index failed for %s: %s", dest, exc)
    return track_id, album_id


async def _enqueue_album_replaygain(album_id: str) -> None:
    """Enqueue the debounced per-album ReplayGain job.

    Deferred a couple minutes so repeat enqueues while a batch of siblings is
    still being approved collapse into arq's job-id dedup (see
    compute_album_replaygain in jobs.py) instead of re-scanning the whole album
    once per track. Best-effort — the backfill sweep can repair misses.
    """
    try:
        from datetime import timedelta

        from service.acquisition.queue import arq_pool

        async with arq_pool() as redis:
            await redis.enqueue_job(
                "compute_album_replaygain",
                album_id,
                _job_id=f"replaygain:{album_id}",
                _defer_by=timedelta(seconds=120),
            )
    except Exception as rg_exc:
        logger.debug("Approve: ReplayGain enqueue failed for album %s: %s", album_id, rg_exc)


async def place_approved_track(
    job_id: str,
    overrides: dict[str, str | None],
    session: AsyncSession,
    scan_trigger: ScanTrigger | None = None,
    mark_progress: bool = False,
) -> Path:
    """Phase 2 (place): cohesion → tag → place → index → replaygain → scan.

    Called from the API when the user approves a needs_review job.
    User overrides from the review form take precedence over stored metadata.
    Raises on file errors so the caller can keep the job in needs_review for retry.
    """
    from service.navidrome.client import trigger_scan as _trigger_scan

    if scan_trigger is None:
        scan_trigger = _trigger_scan

    # ── Load + validate the job row ────────────────────────────────────────────
    row = await session.get(AcquisitionJobRow, job_id)
    if row is None:
        raise ValueError(f"Job {job_id} not found")
    # "placing" is accepted so a job left mid-placement (worker/route crash after
    # mark_progress committed) can be re-approved. "importing" covers rows from
    # before Phase 2 got its own state.
    if row.state not in ("needs_review", "placing", "importing"):
        raise ValueError(f"Job {job_id} is in state {row.state!r}, expected needs_review")
    if not row.resolved_metadata_json:
        raise ValueError(f"Job {job_id} has no resolved metadata")
    if not row.staging_path:
        raise ValueError(f"Job {job_id} has no staging path")

    resolved_json = row.resolved_metadata_json
    staging_path = Path(row.staging_path)
    if not staging_path.exists():
        raise FileNotFoundError(f"Staged file missing: {staging_path}")

    # Publish an intermediate "placing" state (committed) so the job-list poll —
    # which runs in a separate transaction — never momentarily sees this job as
    # needs_review while its staging file has already been moved out. That race
    # produced a spurious "Staging file missing — use Re-download" flag during the
    # several seconds of ReplayGain + artwork + scan. Only the API approval paths
    # set mark_progress; the in-transaction auto-approve path (run_acquisition)
    # keeps its single atomic commit. Deliberately NOT "importing" (Phase 1's
    # state): stuck-job recovery re-queues "importing" rows through a full
    # re-download, which must never happen to a job whose file may already have
    # been atomically placed into /music.
    if mark_progress and row.state != "placing":
        row.state = "placing"
        row.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await session.commit()

    meta = ResolvedTrackMetadata.model_validate_json(resolved_json)
    _apply_review_overrides(meta, overrides)

    from service.library.tagger import primary_artist as _primary_artist

    is_enrichment: bool = meta.is_enrichment
    title: str = meta.title or "Unknown"
    artist: str = meta.artist or "Unknown"
    albumartist: str = meta.albumartist or _primary_artist(artist)
    # Album cohesion may rename the album artist below; the MBID only describes
    # the name it was resolved for, so remember which one that was.
    staged_albumartist: str = albumartist
    album: str | None = meta.album or None
    year: int | None = meta.year
    mb_recording_id: str | None = meta.mb_recording_id or None
    mb_release_id: str | None = meta.mb_release_id or None
    mb_release_group_id: str | None = meta.mb_release_group_id or None
    mb_artist_id: str | None = meta.mb_artist_id or None
    genre: str | None = meta.genre or None
    ext: str = meta.ext or staging_path.suffix.lstrip(".")

    # ── Canonical album cohesion ───────────────────────────────────────────────
    # Anchor to existing local album grouping before writing tags or computing
    # the destination path. AlbumArtist stability also applied here.
    if not is_enrichment and album:
        album, albumartist, year, canonical_release_id, found = await _apply_album_cohesion(
            session,
            album=album,
            albumartist=albumartist,
            year=year,
            mb_artist_id=mb_artist_id,
            mb_release_group_id=mb_release_group_id,
        )
        if found:
            # Always use the album's established release ID (even if None) so all
            # tracks share the same MUSICBRAINZ_ALBUMID tag. If existing tracks
            # have no release ID, the new track must also have none — a mismatch
            # causes Navidrome to split the album into two entries.
            mb_release_id = canonical_release_id

    # ── Artist-identity MBIDs ──────────────────────────────────────────────────
    # Navidrome keys artist identity on the MBID as much as on the name, so an ID
    # that no longer describes the name next to it merges two different artists
    # (or re-splits one we just merged). Resolve them together, last:
    mb_albumartist_id = meta.mb_albumartist_id
    if albumartist == artist:
        # Same artist in both tags — one identity, so one ID.
        mb_albumartist_id = mb_artist_id
    elif albumartist != staged_albumartist:
        # Cohesion picked the locally-established spelling; the staged ID was
        # resolved for a different name and can no longer be vouched for.
        mb_albumartist_id = None

    # ── Write final tags ───────────────────────────────────────────────────────
    # Raise on failure so the approval is aborted and the job stays in
    # needs_review rather than placing an untagged file in /music.
    await asyncio.to_thread(
        write_tags,
        staging_path,
        title=title,
        artist=artist,
        albumartist=albumartist,
        album=album,
        year=year,
        original_year=meta.original_year,
        track_number=meta.track_number,
        disc_number=meta.disc_number,
        artist_sort=meta.mb_artist_sort,
        compilation=meta.is_compilation,
        original_artist=meta.original_artist or None,
        genre=genre,
        mb_recording_id=mb_recording_id,
        mb_release_id=mb_release_id,
        mb_artist_id=mb_artist_id,
        mb_albumartist_id=mb_albumartist_id,
        isrc=meta.isrc or None,
    )

    # ── Place: staging → /music ────────────────────────────────────────────────
    if is_enrichment:
        # File is already in /music — no move needed
        dest = staging_path
    else:
        dest = track_path(
            settings.music_dir,
            artist=artist,
            album=album,
            year=year,
            track_number=meta.track_number,
            disc_number=meta.disc_number,
            title=title,
            ext=ext,
            albumartist=albumartist,
        )

        if meta.is_replacement:
            await _trash_replaced_files(session, meta, dest)

        # Idempotency: file already in place — still fall through to indexing so
        # that a previous approval that hit the tombstone bug (file placed but no
        # DB row created) gets recovered on the next approve attempt.
        if dest.exists():
            logger.info("Approve: track already at %s — ensuring DB record", dest)
        else:
            await asyncio.to_thread(atomic_place, staging_path, dest)

        # ReplayGain: singles get track-only tags immediately (best-effort).
        # Album tracks are deferred to a per-album job below — album gain must
        # combine loudness across every track in the release, which isn't known
        # until (and is re-derived each time) a track lands, so per-track analysis
        # here would be redundant and couldn't produce a correct album value.
        if not album:
            try:
                from service.library.tagger import run_rsgain
                await asyncio.to_thread(run_rsgain, [dest], album=False)
            except Exception as rg_exc:
                logger.debug("Approve: ReplayGain failed for %s: %s", dest, rg_exc)

    # ── Artwork + lyrics (best-effort) ─────────────────────────────────────────
    await _embed_release_artwork(dest, mb_release_id, meta.thumbnail_url)
    _spawn_lyrics_task(dest, artist, title, album, meta.duration_seconds)

    # ── Index in DB ────────────────────────────────────────────────────────────
    # Fallback identity only — the authoritative ID comes from index_file(),
    # computed from the placed file's *actual* tags. Recomputing make_id() here
    # with the candidate-side duration can land in a different duration bucket
    # than the real file, yielding a hash: ID that no row matches → track_row is
    # None → the album's MB release/release-group IDs never get stored (Navidrome
    # album shows as "unlinked", forcing a manual MB link). Discography batches
    # hit this routinely because their candidate duration is the MB tracklist
    # value, not the download.
    hash_track_id = make_id(artist=artist, title=title, duration_seconds=meta.duration_seconds)
    hash_track_id, album_id_for_rg = await _index_placed_track(
        session, dest,
        fallback_track_id=hash_track_id,
        title=title,
        artist=artist,
        album=album,
        year=year,
        track_number=meta.track_number,
        genre=genre,
        mb_recording_id=mb_recording_id,
        mb_release_id=mb_release_id,
        mb_release_group_id=mb_release_group_id,
        mb_artist_id=mb_artist_id,
        is_enrichment=is_enrichment,
    )

    # ── Album ReplayGain (debounced) + Navidrome scan ──────────────────────────
    if album_id_for_rg and not is_enrichment:
        await _enqueue_album_replaygain(album_id_for_rg)

    try:
        await scan_trigger()
    except Exception as exc:
        logger.warning("Approve: Navidrome scan failed: %s", exc)

    # ── Mark done ──────────────────────────────────────────────────────────────
    row = await session.get(AcquisitionJobRow, job_id)
    if row is not None:
        row.state = "done"
        row.track_id = hash_track_id
        row.staging_path = None
        row.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await session.flush()

    logger.info("Approved and placed: %s → %s", job_id, dest)
    return dest
