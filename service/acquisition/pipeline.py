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
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from service.acquisition.states import classify_failure
from service.config import settings
from service.core.identity import make_id
from service.core.models import CandidateScore, ResolvedTrackMetadata, TrackCandidate
from service.core.normalize import clean_for_search, normalize
from service.db.schema import AcquisitionJobRow
from service.index.scanner import index_file
from service.library.layout import track_path
from service.library.tagger import has_cover_art, read_tags, write_cover_jpg, write_tags
from service.library.writer import atomic_place
from service.metadata.quality import compute_quality_score
from service.providers.base import Provider

logger = logging.getLogger(__name__)

ScanTrigger = Callable[[], Awaitable[None]]

_REMUX_CONTAINERS = frozenset({".webm", ".weba"})


async def _set_state(
    session: AsyncSession,
    job_id: str,
    state: str,
    *,
    progress: float | None = None,
    failure_class: str | None = None,
    error: str | None = None,
    track_id: str | None = None,
) -> None:
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
    await session.flush()


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


async def run_acquisition(
    *,
    job_id: str,
    provider: Provider,
    provider_ref: str,
    candidate: TrackCandidate,
    tmp_acquire_dir: Path,
    session: AsyncSession,
    scan_trigger: ScanTrigger | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> None:
    """Phase 1 (identify): download, fingerprint, MB lookup, stage for review.

    Places the file in /music-staging and stores resolved_metadata_json on the
    job row. Sets state to needs_review. Never raises — errors go to the job row.
    """
    # ── 0. Dedup check ────────────────────────────────────────────────────────
    if not candidate.skip_dedup:
        try:
            local_match = await _find_local_match(session, candidate)
            if local_match is not None:
                logger.info("Dedup: skipping — local match exists: %s", local_match)
                await _set_state(session, job_id, "done", track_id=local_match)
                return
        except Exception as exc:
            logger.debug("Dedup check failed (continuing): %s", exc)

    tmp_acquire_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=tmp_acquire_dir) as tmp_str:
        tmp_dir = Path(tmp_str)

        # ── 1. Download ────────────────────────────────────────────────────────
        await _set_state(session, job_id, "downloading")
        try:
            fetch_result = await provider.fetch(provider_ref, tmp_dir, on_progress=on_progress)
        except Exception as exc:
            fc, err = classify_failure(exc)
            await _set_state(session, job_id, "failed", failure_class=fc, error=err)
            logger.error("Download failed [%s] %s: %s", fc, job_id, err)
            return

        audio_path = fetch_result.file_path
        _rm = fetch_result.raw_metadata

        def _rm_str(key: str) -> str | None:
            v = _rm.get(key)
            s = str(v).strip() if v is not None else ""
            return s if s and s.lower() not in ("none", "unknown") else None

        _artist_from_meta = _rm_str("artist")
        _title_raw = _rm_str("track") or _rm_str("title")
        _uploader = _rm_str("uploader") or _rm_str("channel")

        if _artist_from_meta:
            _fetch_title = _title_raw
            _fetch_artist = _artist_from_meta
        elif _title_raw and " - " in _title_raw:
            _split = _title_raw.split(" - ", 1)
            _fetch_artist = _split[0].strip()
            _fetch_title = _split[1].strip()
        else:
            _fetch_title = _title_raw
            _fetch_artist = _uploader

        _fetch_album = _rm_str("album")
        _ry = _rm.get("release_year")
        _fetch_year: int | None = int(_ry) if isinstance(_ry, (int, float)) and _ry else None

        # ── 2. Remux if needed ─────────────────────────────────────────────────
        await _set_state(session, job_id, "processing")
        if audio_path.suffix.lower() in _REMUX_CONTAINERS:
            try:
                audio_path = await _remux_to_ogg(audio_path, tmp_dir)
            except Exception as exc:
                await _set_state(session, job_id, "failed", failure_class="transient", error=str(exc))
                logger.error("Remux failed %s: %s", job_id, exc)
                return

        # ── 3. Read tags + merge ───────────────────────────────────────────────
        await _set_state(session, job_id, "tagging")
        tagged = read_tags(audio_path)

        # Initial provenance: "tagged" > "provider" > "candidate"
        _t = tagged
        prov_title  = "tagged" if (_t and _t.title)  else ("provider" if _fetch_title  else "candidate")
        prov_artist = "tagged" if (_t and _t.artist) else ("provider" if _fetch_artist else "candidate")
        prov_album  = "tagged" if (_t and _t.album)  else ("provider" if _fetch_album  else "candidate")
        prov_year   = "tagged" if (_t and _t.year)   else ("provider" if _fetch_year   else "candidate")

        title = (tagged.title if tagged else None) or _fetch_title or candidate.title
        artist = (tagged.artist if tagged else None) or _fetch_artist or candidate.artist
        album = (tagged.album if tagged else None) or _fetch_album or candidate.album
        year = (tagged.year if tagged else None) or _fetch_year
        track_number = (tagged.track_number if tagged else None)
        disc_number = (tagged.disc_number if tagged else None)
        duration = (tagged.duration_seconds if tagged else None) or candidate.duration_seconds

        # When album coordinator locked album metadata, treat it as authoritative.
        # Explicit album_locked flag takes priority; also infer from album+track_number
        # for backwards-compatibility with candidates created before the flag existed.
        candidate_album_locked = candidate.album_locked or bool(candidate.album and candidate.track_number)

        # ── 3a. Wrong-track detection (duration delta) ─────────────────────────
        force_staging_reason: str | None = None
        _got_dur = tagged.duration_seconds if tagged else None
        if candidate_album_locked and candidate.duration_seconds and _got_dur:
            _delta = abs(_got_dur - candidate.duration_seconds)
            _tol = max(30, int(candidate.duration_seconds * 0.2))
            if _delta > _tol:
                force_staging_reason = (
                    f"Duration mismatch: expected ~{candidate.duration_seconds}s, "
                    f"got {_got_dur}s — may be wrong track"
                )
                logger.warning("Job %s %r: %s", job_id, candidate.title, force_staging_reason)

        # Apply candidate's pre-resolved fields (from album coordinator)
        if candidate.album:
            album = candidate.album
            prov_album = "candidate:locked" if candidate_album_locked else "candidate"
        if candidate.year:
            year = candidate.year
            prov_year = "candidate:locked" if candidate_album_locked else "candidate"
        if candidate.track_number:
            track_number = candidate.track_number

        prov_recording = "candidate" if candidate.mb_recording_id else "none"
        mb_recording_id: str | None = candidate.mb_recording_id
        mb_release_id: str | None = candidate.mb_release_id
        mb_artist_id: str | None = None
        mb_artist_sort: str | None = None
        mb_original_year: int | None = None
        # Seed from candidate so discography/album jobs always carry the RG ID
        # even when the MB lookup returns a result without release_group_id.
        mb_release_group_id: str | None = candidate.mb_release_group_id
        # Phase 5: the release group we prefer for cohesion — the locked candidate
        # RG, or (Path B) one discovered from a locally-owned album below. Biases
        # ranking and stops AcoustID from redefining album grouping.
        _preferred_rg: str | None = candidate.mb_release_group_id
        isrc: str | None = None
        acoustid_confidence: float | None = None
        mb_match_source: str | None = None
        candidate_scores: list[CandidateScore] = []

        # ── 3b. MB identification ──────────────────────────────────────────────
        # Two paths:
        #   A. Locked recording ID (album batch jobs): look up directly, then
        #      run AcoustID in parallel to verify the choice is correct.
        #   B. Standalone: multi-signal ranking over a candidate pool.
        #      - User query encodes intent (live vs studio, explicit etc.)
        #      - MB text search on yt-dlp metadata provides the candidate pool
        #      - AcoustID fingerprint run in parallel; contributes a boost when
        #        it agrees with a text-search candidate, or adds a new candidate
        #        when text search finds nothing.
        #      Final score = text_sim + 0.10 × query_sim + 0.15 × acoustid_boost
        #      mb_from_acoustid = True only when AcoustID was essential (text_sim < 0.85)
        try:
            from service.metadata.acoustid import acoustid_to_mbid
            from service.metadata.musicbrainz import (
                get_recording_by_id,
                get_recording_candidates,
            )
            from service.search.matcher import (
                DEDUP_THRESHOLD as _DEDUP,
                artist_similarity as _artist_sim,
                title_similarity as _title_sim,
            )

            mb: object = None
            mb_from_acoustid = False

            # Read original user query for intent signal (cached in session identity map)
            _job_row_q = await session.get(AcquisitionJobRow, job_id)
            _raw_query: str | None = _job_row_q.query if _job_row_q else None
            # Skip URL and synthetic queries (re-acquire, replacement) — not user intent
            _search_query: str | None = (
                _raw_query
                if _raw_query and not _raw_query.startswith("http")
                and "[re-acquire]" not in _raw_query
                and "[replacement]" not in _raw_query
                else None
            )
            _clean_query = clean_for_search(_search_query) if _search_query else None

            # Phase 3: capture the source's semantic modifiers (live/cover/karaoke/…)
            # from the download title + user query BEFORE MB overwrites `title`.
            # Fed to the incompatibility gates once a winner is chosen.
            from service.core.modifiers import modifier_mismatch_reason
            from service.core.normalize import extract_modifiers as _extract_modifiers
            _source_modifiers = _extract_modifiers(f"{title} {_search_query or ''}")

            if candidate.mb_recording_id:
                # ── Path A: locked recording from album coordinator ──────────
                # Look up and verify with AcoustID in parallel.
                mb_coro = asyncio.to_thread(
                    get_recording_by_id, candidate.mb_recording_id, settings.cache_dir
                )
                acoustid_coro = (
                    acoustid_to_mbid(audio_path, settings.acoustid_api_key)
                    if settings.acoustid_api_key else asyncio.sleep(0)
                )
                mb, _aq_result = await asyncio.gather(mb_coro, acoustid_coro)
                if mb is not None:
                    mb_match_source = "locked_recording"
                if _aq_result and isinstance(_aq_result, tuple):
                    acoustid_mbid_a, acoustid_confidence = _aq_result
                    if acoustid_mbid_a != candidate.mb_recording_id:
                        # A different recording MBID is NOT automatically a wrong
                        # track. MusicBrainz holds many distinct recording entities
                        # for one song (album cut vs single vs remaster); a fingerprint
                        # legitimately matches any of them. Resolve both sides to
                        # title/artist — if it's the same song, the fingerprint
                        # actually CONFIRMS the audio and we keep the locked recording.
                        # Only a genuinely different song is flagged for review.
                        _exp_t = (mb.title if mb else None) or candidate.title
                        _exp_a = (mb.artist if mb else None) or candidate.artist
                        got_rec = await asyncio.to_thread(
                            get_recording_by_id, acoustid_mbid_a, settings.cache_dir
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
                            # Same song, sibling recording entity — confirmation, not
                            # a mismatch. Avoids the confusing "expected X, got X"
                            # review flag that looked identical to the user.
                            mb_from_acoustid = True
                            logger.info(
                                "Job %s %r: AcoustID matched sibling recording %s of the "
                                "same song (expected %s) — accepting as confirmation",
                                job_id, title, acoustid_mbid_a, candidate.mb_recording_id,
                            )
                        else:
                            # Genuinely different song — surface "Title — Artist" so the
                            # review card names what was matched vs expected.
                            expected_label = f"{_exp_t} — {_exp_a}" if _exp_a else _exp_t
                            if _got_t:
                                got_label = f"{_got_t} — {_got_a}" if _got_a else _got_t
                            else:
                                got_label = f"recording {acoustid_mbid_a[:8]}…"
                            mismatch_note = (
                                f"Fingerprint mismatch: expected “{expected_label}”, "
                                f"got “{got_label}”"
                            )
                            force_staging_reason = (
                                f"{force_staging_reason} | {mismatch_note}"
                                if force_staging_reason else mismatch_note
                            )
                            logger.warning(
                                "Job %s %r: AcoustID mismatch (expected %s [%s], got %s [%s])",
                                job_id, title, candidate.mb_recording_id, expected_label,
                                acoustid_mbid_a, got_label,
                            )
                    else:
                        mb_from_acoustid = True  # AcoustID confirmed the locked recording

            else:
                # ── Path B: multi-signal candidate ranking ───────────────────
                # Phase 5: if the user already owns an album by this artist with a
                # matching title, bias retrieval toward its release group so we
                # don't fragment it with a remaster / alternate edition.
                if not _preferred_rg and album:
                    from service.library.cohesion import find_local_release_group
                    _preferred_rg = await find_local_release_group(session, album, artist)
                    if _preferred_rg:
                        logger.info(
                            "Path B cohesion: %r / %r → preferred release group %s",
                            artist, album, _preferred_rg,
                        )
                # Fetch candidate pool and AcoustID fingerprint in parallel.
                candidates_coro = asyncio.to_thread(
                    get_recording_candidates,
                    clean_for_search(title),
                    clean_for_search(artist),
                    duration,
                    settings.cache_dir,
                    _preferred_rg,
                )
                acoustid_coro = (
                    acoustid_to_mbid(audio_path, settings.acoustid_api_key)
                    if settings.acoustid_api_key else asyncio.sleep(0)
                )
                _candidates, _aq_result = await asyncio.gather(candidates_coro, acoustid_coro)

                _acoustid_mbid: str | None = None
                if _aq_result and isinstance(_aq_result, tuple):
                    _acoustid_mbid, acoustid_confidence = _aq_result
                    # If AcoustID found a recording not in the candidate pool, add it
                    if not any(r.recording_id == _acoustid_mbid for r, _ in _candidates):
                        _bonus = await asyncio.to_thread(
                            get_recording_by_id, _acoustid_mbid, settings.cache_dir
                        )
                        if _bonus is not None:
                            from service.search.matcher import track_similarity as _track_sim
                            _base_sim = _track_sim(
                                clean_for_search(title), clean_for_search(artist), duration,
                                _bonus.title, _bonus.artist, _bonus.duration_seconds,
                            )
                            _candidates.append((_bonus, _base_sim))

                # Score each candidate with all three signals (pure, testable)
                from service.metadata.candidates import rank_candidates
                _ranked = rank_candidates(
                    _candidates,
                    clean_query=_clean_query,
                    acoustid_mbid=_acoustid_mbid,
                    clean_artist=clean_for_search(artist) if artist else None,
                )
                # Persist the ranked pool (top 5) with component scores for the
                # review card — Phase 1 observability.
                candidate_scores = [
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

                best_rec = None
                best_combined = 0.0
                best_text_sim = 0.0
                if _ranked:
                    _top = _ranked[0]
                    best_rec, best_combined, best_text_sim = (
                        _top.recording, _top.combined, _top.text_sim
                    )

                if best_rec is not None and best_combined >= _DEDUP:
                    mb = best_rec
                    mb.score = best_combined
                    # AcoustID is "essential" only when it rescued a below-threshold match
                    mb_from_acoustid = (
                        _acoustid_mbid == best_rec.recording_id
                        and best_text_sim < _DEDUP
                    )
                    mb_match_source = "acoustid" if mb_from_acoustid else "text_search"
                    logger.info(
                        "MB ranked: %r → %s (text=%.2f combined=%.2f acoustid=%s query=%s)",
                        title, best_rec.recording_id, best_text_sim, best_combined,
                        bool(_acoustid_mbid == best_rec.recording_id) if best_rec else False,
                        bool(_clean_query),
                    )

            # Capture text-search similarity for fast-approve eligibility UX.
            # Expose text similarity for fast-approve eligibility in the review UI.
            # For Path B (multi-signal), best_text_sim is the raw title+artist score
            # before query/AcoustID bonuses — a fairer measure of MB match quality.
            text_search_similarity: float | None = (
                best_text_sim  # type: ignore[possibly-undefined]
                if mb is not None and mb_match_source == "text_search"
                else None
            )

            if mb is not None:
                resolved_recording_id = mb.recording_id  # type: ignore[union-attr]
                # When the candidate has a locked recording ID, only accept a different
                # recording via AcoustID fingerprint — text search is not authoritative.
                _recording_id_overridden = False
                if candidate.mb_recording_id and not mb_from_acoustid:
                    if resolved_recording_id != candidate.mb_recording_id:
                        logger.info(
                            "Text search returned different recording %s (expected %s) for %r "
                            "— keeping locked recording_id",
                            resolved_recording_id, candidate.mb_recording_id, title,
                        )
                        resolved_recording_id = candidate.mb_recording_id
                        _recording_id_overridden = True

                mb_recording_id = resolved_recording_id
                mb_release_id = mb_release_id or mb.release_id  # type: ignore[union-attr]
                mb_artist_id = mb.artist_id  # type: ignore[union-attr]
                mb_artist_sort = mb.artist_sort  # type: ignore[union-attr]
                mb_original_year = mb.original_year  # type: ignore[union-attr]
                # Keep candidate's release_group_id as fallback so album-locked jobs
                # never lose the RG ID when MB returns an incomplete result.
                mb_release_group_id = mb.release_group_id or mb_release_group_id  # type: ignore[union-attr]
                # Phase 5 AcoustID boundary: a fingerprint match may pick the right
                # RECORDING but must not redefine the album's release group when we
                # hold a strong local cohesion signal — re-anchor to the owned RG.
                if _preferred_rg and mb_from_acoustid and mb_release_group_id != _preferred_rg:
                    logger.info(
                        "Keeping owned release group %s over AcoustID's %s for %r",
                        _preferred_rg, mb_release_group_id, title,
                    )
                    mb_release_group_id = _preferred_rg
                isrc = mb.isrc  # type: ignore[union-attr]
                prov_recording = mb_match_source or "mb"
                # Only accept title/artist from MB if the MB result actually corresponds
                # to the track we expected.  When the recording ID was overridden back to
                # the candidate's value, the MB result is for the WRONG track — using its
                # title/artist would mislabel the file (e.g. "For Whom the Bell Tolls"
                # instead of "The God That Failed").
                if mb.title and not _recording_id_overridden:  # type: ignore[union-attr]
                    title = mb.title  # type: ignore[union-attr]
                    prov_title = f"mb:{mb_match_source}"
                if mb.artist and not _recording_id_overridden:  # type: ignore[union-attr]
                    artist = mb.artist  # type: ignore[union-attr]
                    prov_artist = f"mb:{mb_match_source}"
                if not candidate_album_locked:
                    if mb.album:  # type: ignore[union-attr]
                        album = mb.album  # type: ignore[union-attr]
                        prov_album = f"mb:{mb_match_source}"
                    if mb.year:  # type: ignore[union-attr]
                        year = mb.year  # type: ignore[union-attr]
                        prov_year = "mb:release"
                    track_number = mb.track_number or track_number  # type: ignore[union-attr]
                if mb.original_year:  # type: ignore[union-attr]
                    prov_year = "mb:original"

                # ── Phase 3 incompatibility gates ─────────────────────────────
                # Run regardless of match source. Skipped when the recording ID was
                # overridden back to the locked candidate — there mb.title/artist
                # describe the WRONG track and would mis-fire. AcoustID can rescue a
                # weak text match but does NOT exempt a track from these gates.
                if not _recording_id_overridden:
                    # (a) Artist mismatch — on ALL paths (was AcoustID-only before).
                    #     Catches both fingerprint wrong-matches and text-search
                    #     candidates from a different artist (title dominated the score).
                    if candidate.artist and mb.artist:  # type: ignore[union-attr]
                        from service.search.matcher import artist_similarity as _artist_sim
                        a_sim = _artist_sim(candidate.artist, mb.artist)  # type: ignore[union-attr]
                        if a_sim < 0.55:
                            _how = "fingerprint identified" if mb_from_acoustid else "MB matched"
                            mismatch = (
                                f"Artist mismatch: expected \"{candidate.artist}\", "
                                f"{_how} \"{mb.artist}\" (similarity {a_sim:.2f}) — may be wrong track"  # type: ignore[union-attr]
                            )
                            force_staging_reason = (
                                f"{force_staging_reason} | {mismatch}"
                                if force_staging_reason else mismatch
                            )
                            logger.warning("Job %s %r: %s", job_id, candidate.title, mismatch)

                    # (b) Modifier incompatibility — e.g. a live/cover/karaoke source
                    #     that matched a studio/original MB recording. The MB winner's
                    #     own title+album supply its flags.
                    _mb_modifiers = _extract_modifiers(
                        f"{mb.title or ''} {mb.album or ''}"  # type: ignore[union-attr]
                    )
                    _mod_reason = modifier_mismatch_reason(_source_modifiers, _mb_modifiers)
                    if _mod_reason:
                        force_staging_reason = (
                            f"{force_staging_reason} | {_mod_reason}"
                            if force_staging_reason else _mod_reason
                        )
                        logger.warning("Job %s %r: %s", job_id, candidate.title, _mod_reason)

            # ── 3c. Title mismatch detection ──────────────────────────────────────────
            # When the candidate specifies an expected title and the resolved title is
            # substantially different, the wrong track was likely downloaded.  Force to
            # staging so the user sees it before it lands in /music.
            if candidate.title and title:
                from service.search.matcher import title_similarity as _title_sim
                t_sim = _title_sim(clean_for_search(candidate.title), clean_for_search(title))
                if t_sim < 0.55:
                    mismatch_note = (
                        f"Title mismatch: expected \"{candidate.title}\", "
                        f"got \"{title}\" (similarity {t_sim:.2f}) — may be wrong track"
                    )
                    force_staging_reason = (
                        f"{force_staging_reason} | {mismatch_note}"
                        if force_staging_reason else mismatch_note
                    )
                    logger.warning("Job %s: %s", job_id, mismatch_note)

        except Exception as mb_exc:
            logger.debug("MB lookup skipped: %s", mb_exc)

        # When no MB match, clean the raw YouTube title/artist so the review card
        # shows a sensible default instead of "(Official Music Video)" noise.
        if mb_match_source is None:
            title = clean_for_search(title)
            artist = clean_for_search(artist)

        # Fetch MB folksonomy genres for the review card
        mb_genres: list[str] = []
        if mb_release_group_id:
            try:
                from service.metadata.musicbrainz import get_release_group_genres
                mb_genres = await asyncio.to_thread(
                    get_release_group_genres, mb_release_group_id, settings.cache_dir
                )
            except Exception as genre_exc:
                logger.debug("Genre fetch skipped: %s", genre_exc)

        albumartist = candidate.artist if candidate_album_locked else artist
        is_compilation = (album is not None) and albumartist.lower() in ("various artists", "various")

        quality_score = compute_quality_score(
            title=title,
            artist=artist,
            album=album,
            year=year,
            track_number=track_number,
            musicbrainz_recording_id=mb_recording_id,
            has_cover_art=False,
        )

        # ── 4. Place in staging (holding area for review) ─────────────────────
        await _set_state(session, job_id, "importing")

        ext = audio_path.suffix.lstrip(".")
        staging_dest = track_path(
            settings.staging_dir,
            artist=artist,
            album=album,
            year=year,
            track_number=track_number,
            disc_number=disc_number,
            title=title,
            ext=ext,
            albumartist=albumartist,
        )

        try:
            await asyncio.to_thread(atomic_place, audio_path, staging_dest)
        except Exception as exc:
            await _set_state(session, job_id, "failed", failure_class="transient", error=str(exc))
            logger.error("Staging placement failed %s: %s", job_id, exc)
            return

        # ── 5. Store resolved metadata → needs_review ─────────────────────────
        resolved_metadata = ResolvedTrackMetadata(
            title=title,
            artist=artist,
            albumartist=albumartist,
            album=album,
            year=year,
            original_year=mb_original_year,
            track_number=track_number,
            disc_number=disc_number,
            duration_seconds=duration,
            ext=ext,
            source_codec=fetch_result.codec,
            source_bitrate_kbps=fetch_result.bitrate_kbps,
            source_url=fetch_result.source_url,
            mb_recording_id=mb_recording_id,
            mb_release_id=mb_release_id,
            mb_release_group_id=mb_release_group_id,
            mb_artist_id=mb_artist_id,
            mb_artist_sort=mb_artist_sort,
            isrc=isrc,
            acoustid_confidence=acoustid_confidence,
            text_search_similarity=text_search_similarity,
            mb_match_source=mb_match_source,
            candidates=candidate_scores,
            is_compilation=is_compilation,
            force_staging_reason=force_staging_reason,
            quality_score=quality_score,
            thumbnail_url=candidate.thumbnail_url,
            mb_genres=mb_genres,
            # Metadata provenance: which source contributed each key field
            prov_title=prov_title,
            prov_artist=prov_artist,
            prov_album=prov_album,
            prov_year=prov_year,
            prov_recording=prov_recording,
            # Propagate replacement flag so place_approved_track can trash the old file
            is_replacement=candidate.skip_dedup,
        )

        row = await session.get(AcquisitionJobRow, job_id)
        if row is not None:
            row.state = "needs_review"
            row.staging_path = str(staging_dest)
            row.resolved_metadata_json = resolved_metadata.model_dump_json()
            if force_staging_reason:
                row.error = force_staging_reason
            row.updated_at = datetime.now(UTC).replace(tzinfo=None)
            await session.flush()

        logger.info(
            "Identify done (source=%s, quality=%.0f%%): %s → staged at %s",
            mb_match_source or "none", quality_score * 100, job_id, staging_dest,
        )

        # ── Source replacement: auto-approve when no flags raised ──────────────
        # The user already previewed and explicitly chose the replacement source;
        # the rest of the track's metadata stays unchanged. Skip the review queue
        # unless something looks wrong (force_staging_reason set).
        if candidate.skip_dedup and not force_staging_reason:
            logger.info("Auto-approving replacement job %s — user already vetted source", job_id)
            try:
                await place_approved_track(job_id, {}, session, scan_trigger)
            except Exception as exc:
                logger.warning(
                    "Auto-approve for replacement %s failed — left in needs_review: %s",
                    job_id, exc,
                )


async def place_approved_track(
    job_id: str,
    overrides: dict[str, str | None],
    session: AsyncSession,
    scan_trigger: ScanTrigger | None = None,
    mark_progress: bool = False,
) -> Path:
    """Phase 2 (place): write tags, move staging → /music, index, scan.

    Called from the API when the user approves a needs_review job.
    User overrides from the review form take precedence over stored metadata.
    Raises on file errors so the caller can keep the job in needs_review for retry.
    """
    from service.db.schema import Track as _Track
    from service.navidrome.client import trigger_scan as _trigger_scan

    if scan_trigger is None:
        scan_trigger = _trigger_scan

    row = await session.get(AcquisitionJobRow, job_id)
    if row is None:
        raise ValueError(f"Job {job_id} not found")
    # "importing" is accepted so a job left mid-placement (worker/route crash after
    # mark_progress committed) can be re-approved.
    if row.state not in ("needs_review", "importing"):
        raise ValueError(f"Job {job_id} is in state {row.state!r}, expected needs_review")
    if not row.resolved_metadata_json:
        raise ValueError(f"Job {job_id} has no resolved metadata")
    if not row.staging_path:
        raise ValueError(f"Job {job_id} has no staging path")

    resolved_json = row.resolved_metadata_json
    staging_path = Path(row.staging_path)
    if not staging_path.exists():
        raise FileNotFoundError(f"Staged file missing: {staging_path}")

    # Publish an intermediate "importing" state (committed) so the job-list poll —
    # which runs in a separate transaction — never momentarily sees this job as
    # needs_review while its staging file has already been moved out. That race
    # produced a spurious "Staging file missing — use Re-download" flag during the
    # several seconds of ReplayGain + artwork + scan. Only the API approval paths
    # set mark_progress; the in-transaction auto-approve path (run_acquisition)
    # keeps its single atomic commit.
    if mark_progress and row.state != "importing":
        row.state = "importing"
        row.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await session.commit()

    meta = ResolvedTrackMetadata.model_validate_json(resolved_json)

    # Apply user-supplied overrides — non-empty string values win
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

    # Sync albumartist when artist was overridden but albumartist wasn't
    if "artist" in overrides and "albumartist" not in overrides:
        meta.albumartist = meta.artist

    is_enrichment: bool = meta.is_enrichment
    title: str = meta.title or "Unknown"
    artist: str = meta.artist or "Unknown"
    albumartist: str = meta.albumartist or artist
    album: str | None = meta.album or None
    year: int | None = meta.year
    original_year: int | None = meta.original_year
    track_number: int | None = meta.track_number
    disc_number: int | None = meta.disc_number
    mb_recording_id: str | None = meta.mb_recording_id or None
    mb_release_id: str | None = meta.mb_release_id or None
    mb_release_group_id: str | None = meta.mb_release_group_id or None
    mb_artist_id: str | None = meta.mb_artist_id or None
    mb_artist_sort: str | None = meta.mb_artist_sort or None
    isrc: str | None = meta.isrc or None
    is_compilation: bool = meta.is_compilation
    genre: str | None = meta.genre or None
    duration_seconds: int | None = meta.duration_seconds
    ext: str = meta.ext or staging_path.suffix.lstrip(".")

    # ── Canonical album cohesion ───────────────────────────────────────────────
    # Anchor to existing local album grouping before writing tags or computing
    # the destination path. AlbumArtist stability also applied here.
    if not is_enrichment and album:
        from service.library.cohesion import find_canonical_album, stable_albumartist
        albumartist = await stable_albumartist(session, albumartist, mb_artist_id)
        canonical = await find_canonical_album(session, album, albumartist, mb_release_group_id)
        if canonical is not None:
            album, albumartist, canonical_year, canonical_release_id = canonical
            if canonical_year is not None:
                year = canonical_year
            # Always use the album's established release ID (even if None) so all
            # tracks share the same MUSICBRAINZ_ALBUMID tag. If existing tracks have no
            # release ID, the new track must also have none — a mismatch causes Navidrome
            # to split the album into two entries.
            mb_release_id = canonical_release_id

    # Write final tags — raise on failure so the approval is aborted and the
    # job stays in needs_review rather than placing an untagged file in /music.
    await asyncio.to_thread(
        write_tags,
        staging_path,
        title=title,
        artist=artist,
        albumartist=albumartist,
        album=album,
        year=year,
        original_year=original_year,
        track_number=track_number,
        disc_number=disc_number,
        artist_sort=mb_artist_sort,
        compilation=is_compilation,
        genre=genre,
        mb_recording_id=mb_recording_id,
        mb_release_id=mb_release_id,
        mb_artist_id=mb_artist_id,
        isrc=isrc,
    )

    if is_enrichment:
        # File is already in /music — no move needed
        dest = staging_path
    else:
        # Compute final /music destination
        dest = track_path(
            settings.music_dir,
            artist=artist,
            album=album,
            year=year,
            track_number=track_number,
            disc_number=disc_number,
            title=title,
            ext=ext,
            albumartist=albumartist,
        )

        is_replacement: bool = meta.is_replacement
        if dest.exists() and is_replacement:
            # Trash the old file before placing the replacement so the new
            # version actually lands instead of being skipped.
            from service.library.writer import safe_trash as _safe_trash
            trash_dir = settings.music_dir / ".trash"
            await asyncio.to_thread(_safe_trash, dest, trash_dir)
            logger.info("Approve replacement: trashed old file at %s", dest)

        # Idempotency: file already in place — still fall through to indexing so
        # that a previous approval that hit the tombstone bug (file placed but no
        # DB row created) gets recovered on the next approve attempt.
        if dest.exists():
            logger.info("Approve: track already at %s — ensuring DB record", dest)
        else:
            # Atomic place: staging → music
            await asyncio.to_thread(atomic_place, staging_path, dest)

        # ReplayGain (best-effort; adds ~5s but runs only once per track)
        try:
            from service.library.tagger import compute_replaygain, write_replaygain
            rg = await asyncio.to_thread(compute_replaygain, dest)
            if rg is not None:
                await asyncio.to_thread(write_replaygain, dest, rg)
        except Exception as rg_exc:
            logger.debug("Approve: ReplayGain failed for %s: %s", dest, rg_exc)

    # Fetch and embed artwork (cached — cheap on second call)
    artwork_bytes: bytes | None = None
    if mb_release_id:
        try:
            from service.metadata.artwork import fetch_artwork
            artwork_bytes = await fetch_artwork(
                release_mbid=mb_release_id,
                thumbnail_url=meta.thumbnail_url,
                cache_dir=settings.cache_dir,
            )
        except Exception as exc:
            logger.debug("Approve: artwork fetch failed: %s", exc)

    if artwork_bytes:
        try:
            await asyncio.to_thread(write_tags, dest, artwork_bytes=artwork_bytes)
        except Exception as exc:
            logger.debug("Approve: artwork embed failed: %s", exc)
        write_cover_jpg(dest.parent, artwork_bytes)

    # Index in DB using a savepoint so failures don't roll back the outer transaction.
    # Everything (index + track-row updates) lives inside begin_nested() so any flush
    # failure only rolls back the savepoint — the outer transaction stays clean and the
    # job-state update below always succeeds.
    hash_track_id = make_id(artist=artist, title=title, duration_seconds=duration_seconds)
    try:
        async with session.begin_nested():
            # Clear any tombstone for this recording so the user's explicit approval
            # re-admits the track to the library (tombstones block the background
            # scanner, not conscious user re-acquisition).
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
            await index_file(session, dest)
            hca = await asyncio.to_thread(has_cover_art, dest)
            # Eager-load the file relationship: async SQLAlchemy can't lazy-load it
            # synchronously when accessed below (greenlet_spawn error otherwise).
            from sqlalchemy.orm import selectinload as _selin_file
            track_row = await session.get(
                _Track, hash_track_id, options=[_selin_file(_Track.file)]
            )
            if track_row is not None:
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
                        # subsequent tracks inherit it (prevents Navidrome album splits
                        # caused by different editions matching different release IDs)
                        if mb_release_id and not album_row.musicbrainz_release_id:
                            album_row.musicbrainz_release_id = mb_release_id
                        # Normalize MUSICBRAINZ_ALBUMID on sibling tracks: if this track
                        # has a release ID and existing siblings don't (or differ), rewrite
                        # their file tags now so Navidrome groups them as one album.
                        # This is the "Fix file tags" operation that users had to run manually.
                        effective_release_id = mb_release_id or album_row.musicbrainz_release_id
                        if effective_release_id and not is_enrichment:
                            from sqlalchemy import select as _sel_sib
                            from service.db.schema import Track as _SibTrack, TrackFile as _SibTF
                            from sqlalchemy.orm import joinedload as _jl_sib
                            sibling_tracks = (await session.execute(
                                _sel_sib(_SibTrack)
                                .options(_jl_sib(_SibTrack.file))
                                .where(
                                    _SibTrack.album_id == track_row.album_id,
                                    _SibTrack.id != hash_track_id,
                                )
                            )).unique().scalars().all()
                            for sib in sibling_tracks:
                                if sib.file:
                                    sib_fp = Path(sib.file.path)
                                    if sib_fp.exists():
                                        try:
                                            await asyncio.to_thread(
                                                write_tags, sib_fp,
                                                mb_release_id=effective_release_id,
                                            )
                                        except Exception as sib_exc:
                                            logger.debug(
                                                "Approve: sibling retag failed for %s: %s",
                                                sib_fp, sib_exc,
                                            )
                await session.flush()
    except Exception as exc:
        logger.warning("Approve: DB index failed for %s: %s", dest, exc)

    # Navidrome scan
    try:
        await scan_trigger()
    except Exception as exc:
        logger.warning("Approve: Navidrome scan failed: %s", exc)

    row = await session.get(AcquisitionJobRow, job_id)
    if row is not None:
        row.state = "done"
        row.track_id = hash_track_id
        row.staging_path = None
        row.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await session.flush()

    logger.info("Approved and placed: %s → %s", job_id, dest)
    return dest
