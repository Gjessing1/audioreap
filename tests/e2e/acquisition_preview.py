"""Deterministic UI audit server; no live DB, workers or external providers.

Run from the repository root:
  .venv/bin/python -m uvicorn tests.e2e.acquisition_preview:app --port 8018
"""
from __future__ import annotations

import json
import shutil
import tempfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi import Depends, FastAPI, Form, Request
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from service.api.routes import acquire, discography
from service.api.shared import _acquisition_receipt
from service.api.webui import router
from service.config import settings
from service.core.models import TrackCandidate
from service.db.schema import (
    AcquisitionJobRow,
    Album,
    AlbumAcquisitionJob,
    Artist,
    Base,
    Track,
    TrackFile,
)
from service.db.session import get_session
from service.metadata import musicbrainz
from service.providers import ytdlp
from tests.unit.test_jobs_triage import _row
from tests.unit.test_template_contracts import _meta

ROOT = Path(__file__).resolve().parents[2]
AUDIT_DIR = Path(tempfile.mkdtemp(prefix="audioreap-ui-audit-"))
settings.music_dir = AUDIT_DIR / "music"
settings.staging_dir = AUDIT_DIR / "staging"
settings.cache_dir = AUDIT_DIR / "cache"
settings.data_dir = AUDIT_DIR
settings.private_subdir = ""
settings.lyrics_enabled = False
engine = create_async_engine("sqlite+aiosqlite:///:memory:")
sessions = async_sessionmaker(engine, expire_on_commit=False)

ARTISTS = [
    ("Daft Punk", "French electronic duo", 1.0),
    ("Daft Punk Tribute Orchestra & Friends", "Live orchestral tribute ensemble, not the original duo", .86),
    ("Daft Punk Experience", "Italian tribute band", .82),
    ("Daft Punk NFT", "", .72),
    ("Digital Punk", "Dutch hardstyle DJ", .68),
    ("Daft", "Russian metal band from the 1990s", .65),
    ("3 Daft Monkeys", "English folk band", .6),
    ("Björk & the Reykjavík Chamber Ensemble", "Icelandic experimental ensemble", .55),
]
TITLES = [
    "Harder, Better, Faster, Stronger (Official Video — Extended Remastered Edition)",
    "Get Lucky (Official Audio) feat. Pharrell Williams and Nile Rodgers",
    "Instant Crush (Official Video) ft. Julian Casablancas",
    "Around the World (Live at the Pyramid Stage, Full Performance)",
    "Something About Us / Digital Love — Acoustic Cover by The Midnight Orchestra",
]


def candidates():
    return [dict(title=title, artist="Daft Punk Official", duration_seconds=223 + i,
                 provider_ref=f"https://www.youtube.com/watch?v=demo{i}", thumbnail_url=None,
                 candidate_json=TrackCandidate(provider="ytdlp", provider_ref=f"https://youtu.be/demo{i}",
                                               title=title, artist="Daft Punk").model_dump_json(),
                 owned_title=None, owned_artist=None) for i, title in enumerate(TITLES)]


async def cloud(*args, **kwargs):
    return candidates()


async def releases(*args):
    return "Daft Punk", [dict(title="TRON: Legacy — The Complete Original Motion Picture Soundtrack (Deluxe Edition)",
        release_group_id="release-demo", release_type="Album", year=2010, owned=False,
        owned_track_count=0, cover_track_id=None)], ["Album", "Single", "EP"]


acquire._search_cloud_candidates = cloud
discography._release_entries = releases
musicbrainz.search_artists = lambda *args: [SimpleNamespace(
    artist_id=f"artist-{i}", name=name, disambiguation=detail, score=score,
) for i, (name, detail, score) in enumerate(ARTISTS)]
musicbrainz.get_related_artists = lambda *args: []
ytdlp.yt_search_ranked = lambda *args, **kwargs: [SimpleNamespace(
    title=TITLES[0], artist="Daft Punk Official", duration_seconds=223,
    provider_ref="https://youtu.be/demo0", score=.93, is_official=True,
)]
musicbrainz.get_release_group_tracks = lambda *args: SimpleNamespace(
    album_title="TRON: Legacy — The Complete Original Motion Picture Soundtrack (Deluxe Edition)",
    album_artist="Daft Punk", release_id="release-demo", is_compilation=False,
    tracks=[SimpleNamespace(recording_id=f"recording-{i}", title=title, number=i + 1,
                            disc=1, duration_seconds=223 + i, artist="Daft Punk")
            for i, title in enumerate([*TITLES[:2], "Derezzed — Extended Cinematic Reprise",
                                       "The Grid (Complete Score Version)", "End of Line (Original Album Mix)"])],
)


async def session_dep():
    async with sessions() as session:
        yield session


@asynccontextmanager
async def lifespan(app):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    settings.staging_dir.mkdir(parents=True)
    settings.music_dir.mkdir(parents=True)
    now = datetime.now(UTC).replace(tzinfo=None)
    async with sessions() as session:
        artist = Artist(id="artist-library", name="Daft Punk", created_at=now, updated_at=now)
        album = Album(id="album-library", title="TRON: Legacy — Original Motion Picture Soundtrack (Deluxe Edition)",
                      artist=artist, year=2010, created_at=now, updated_at=now)
        session.add_all([artist, album])
        for i in range(8):
            track = Track(id=f"local-{i}", title=TITLES[i % 5], artist=artist, album=album,
                          created_at=now, updated_at=now)
            path = settings.music_dir / f"local-{i}.wav"
            shutil.copyfile(ROOT / "tests/fixtures/audio/tone_1s.wav", path)
            session.add(TrackFile(track=track, path=str(path), codec="pcm", container="wav", created_at=now))
        for name, extra in [
            ("flagged", {"force_staging_reason": "Duration mismatch: expected 3:43, downloaded 5:27. This may be a live performance or a different recording."}),
            ("probable", {"mb_match_source": "text_search", "text_search_similarity": .72}),
            ("verified", {"mb_match_source": "acoustid", "mb_from_acoustid": True}),
            ("enrichment", {"is_enrichment": True, "current_title": "Old title", "mb_match_source": "text_search"}),
            ("missing", {}),
        ]:
            meta = {**_meta(), "title": "Harder, Better, Faster, Stronger (Extended Album Version)",
                    "artist": "Daft Punk", "album": "Discovery", "mb_match_source": "text_search",
                    "source_title": TITLES[0], "source_channel": "Daft Punk Official",
                    "source_url": "https://www.youtube.com/watch?v=demo1234567", "source_duration_seconds": 327,
                    **extra}
            path = settings.staging_dir / f"{name}.wav"
            if name != "missing":
                shutil.copyfile(ROOT / "tests/fixtures/audio/tone_1s.wav", path)
            session.add(_row(f"demo-{name}", "needs_review", now, staging_path=path, meta=meta))
        failed = _row("demo-failed", "failed", now)
        failed.error = "This video is no longer available. Choose a different source to download this track, or retry if the provider was temporarily unavailable."
        session.add_all([failed, _row("demo-active", "queued", now), _row("demo-completed", "done", now)])
        session.add(AlbumAcquisitionJob(id="demo-album", provider="musicbrainz", album_ref="release-demo",
            album_title="TRON: Legacy — Complete Original Motion Picture Soundtrack (Deluxe Edition)",
            album_artist="Daft Punk", track_count=2, state="done", created_at=now, updated_at=now))
        for i in range(2):
            path = settings.staging_dir / f"album-{i}.wav"
            shutil.copyfile(ROOT / "tests/fixtures/audio/tone_1s.wav", path)
            meta = {**_meta(), "title": f"Album track {i + 1}", "artist": "Daft Punk",
                    "source_title": TITLES[i], "mb_match_source": "locked_recording",
                    "source_url": f"https://youtu.be/album{i}",
                    "force_staging_reason": "Different live recording — compare the source before saving" if i else None}
            row = _row(f"demo-album-{i}", "needs_review", now, staging_path=path, meta=meta)
            row.album_job_id = "demo-album"
            session.add(row)
        await session.commit()
    yield
    await engine.dispose()
    shutil.rmtree(AUDIT_DIR)


app = FastAPI(lifespan=lifespan)
app.include_router(router)
app.mount("/static", StaticFiles(directory=ROOT / "service/static"), name="static")
app.dependency_overrides[get_session] = session_dep


@app.get("/__audit__")
async def audit_marker():
    return {"isolated_acquisition_fixture": True}


@app.post("/api/acquire")
async def demo_acquire(request: Request, candidate_json: str = Form(...),
                       session: AsyncSession = Depends(get_session)):
    """Simulate a finished download; the actual UI and Jobs routes handle the handoff."""
    candidate = TrackCandidate.model_validate_json(candidate_json)
    now = datetime.now(UTC).replace(tzinfo=None)
    job_id = "demo-acquired"
    existing = await session.get(AcquisitionJobRow, job_id)
    if not existing:
        path = settings.staging_dir / "acquired.wav"
        shutil.copyfile(ROOT / "tests/fixtures/audio/tone_1s.wav", path)
        meta = {**_meta(), "title": candidate.title, "artist": candidate.artist,
                "source_title": candidate.title, "source_url": candidate.provider_ref}
        row = _row(job_id, "needs_review", now, staging_path=path, meta=meta)
        row.candidate_json = candidate.model_dump_json()
        row.resolved_metadata_json = json.dumps(meta)
        session.add(row)
        await session.commit()
    return _acquisition_receipt(request, job_id=job_id, title=candidate.title,
                                artist=candidate.artist, state="needs_review", created=not existing)
