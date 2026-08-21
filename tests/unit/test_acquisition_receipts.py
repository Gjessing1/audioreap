"""Contracts for shared acquisition receipts and active-request reuse."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from service.acquisition.jobs import create_or_get_active_job
from service.api.routes.discography import discography_acquire_missing
from service.api.routes.jobs import _job_list_ctx
from service.api.routes.playlists import acquire_playlist, retry_failed_playlist
from service.api.shared import _acquisition_batch_receipt
from service.core.models import TrackCandidate
from service.db.schema import AcquisitionJobRow, Base
from service.main import acquire


def _request(path: str = "/api/acquire") -> Request:
    return Request({
        "type": "http",
        "method": "POST",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("test", 123),
        "root_path": "",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"hx-request", b"true")],
    })


def test_receipt_names_work_and_links_to_stable_job_anchor() -> None:
    templates_dir = Path(__file__).parents[2] / "service" / "templates"
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        undefined=StrictUndefined,
        autoescape=True,
    )
    html = env.get_template("partials/acquisition_receipt.html").render(
        job_id="job-123",
        title="Test Track",
        artist="Test Artist",
        state="queued",
        created=True,
    )

    assert "Test Track" in html
    assert "Test Artist" in html
    assert "Queued" in html
    assert 'href="/jobs#job-job-123"' in html


def test_batch_receipt_exposes_counts_retry_and_stable_group() -> None:
    response = _acquisition_batch_receipt(
        _request(),
        batch_id="batch-123",
        title="Road Trip",
        queued_count=4,
        owned_count=2,
        failed_count=1,
        jobs_anchor="playlist-batch-123",
        retry_url="/playlists/batch-123/retry-failed",
        unit="track",
        retry_field="job_ids",
        failed_items=[{
            "id": "failed-job",
            "title": "Missed Track",
            "artist": "Test Artist",
        }],
    )
    html = response.body.decode()
    assert "4 queued · 2 already owned · 1 failed" in html
    assert "Retry failed track" in html
    assert 'href="/jobs#playlist-batch-123"' in html
    assert 'name="job_ids" value="failed-job" checked' in html
    assert "Missed Track" in html
    assert "Test Artist" in html
    assert "data-acquire-jobs-link" in html
    assert "jobsChanged" in response.headers["HX-Trigger"]


@asynccontextmanager
async def _pool(redis):
    yield redis


async def test_repeated_active_provider_reference_reuses_job() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    candidate = TrackCandidate(
        provider="ytdlp",
        provider_ref="https://youtu.be/test",
        title="Test Track",
        artist="Test Artist",
    )
    async with sessions() as session:
        first_id, first_created = await create_or_get_active_job(
            session,
            provider_name="ytdlp",
            provider_ref=candidate.provider_ref,
            candidate=candidate,
        )
        await session.commit()
        second_id, second_created = await create_or_get_active_job(
            session,
            provider_name="ytdlp",
            provider_ref=candidate.provider_ref,
            candidate=candidate,
        )

    await engine.dispose()
    assert first_created is True
    assert second_created is False
    assert second_id == first_id


async def test_completed_provider_reference_can_be_requested_again() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    candidate = TrackCandidate(
        provider="ytdlp",
        provider_ref="https://youtu.be/test",
        title="Test Track",
        artist="Test Artist",
    )
    async with sessions() as session:
        first_id, _ = await create_or_get_active_job(
            session,
            provider_name="ytdlp",
            provider_ref=candidate.provider_ref,
            candidate=candidate,
        )
        row = await session.get(AcquisitionJobRow, first_id)
        assert row is not None
        row.state = "done"
        row.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await session.commit()

        second_id, second_created = await create_or_get_active_job(
            session,
            provider_name="ytdlp",
            provider_ref=candidate.provider_ref,
            candidate=candidate,
        )

    await engine.dispose()
    assert second_created is True
    assert second_id != first_id


async def test_acquire_endpoint_returns_receipt_and_does_not_enqueue_twice() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    candidate = TrackCandidate(
        provider="ytdlp",
        provider_ref="https://youtu.be/test",
        title="Test Track",
        artist="Test Artist",
    )
    redis = AsyncMock()
    with patch("arq.create_pool", AsyncMock(return_value=redis)):
        async with sessions() as session:
            first = await acquire(
                _request(),
                session,
                "ytdlp",
                candidate.provider_ref,
                candidate.model_dump_json(),
                "Test Artist - Test Track",
            )
            second = await acquire(
                _request(),
                session,
                "ytdlp",
                candidate.provider_ref,
                candidate.model_dump_json(),
                "Test Artist - Test Track",
            )

    await engine.dispose()
    assert "Queued" in first.body.decode()
    assert "Already requested" in second.body.decode()
    assert "jobsChanged" in first.headers["HX-Trigger"]
    redis.enqueue_job.assert_awaited_once()


async def test_queue_failure_marks_job_failed_so_next_request_can_retry() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    candidate = TrackCandidate(
        provider="ytdlp",
        provider_ref="https://youtu.be/retry",
        title="Retry Track",
        artist="Test Artist",
    )
    async with sessions() as session:
        with (
            patch("arq.create_pool", AsyncMock(side_effect=RuntimeError("redis down"))),
            pytest.raises(HTTPException, match="Queue unavailable"),
        ):
            await acquire(
                _request(),
                session,
                "ytdlp",
                candidate.provider_ref,
                candidate.model_dump_json(),
                "Test Artist - Retry Track",
            )

        failed = (await session.execute(select(AcquisitionJobRow))).scalars().one()
        assert failed.state == "failed"
        assert failed.failure_class == "queue_unavailable"

        redis = AsyncMock()
        with patch("arq.create_pool", AsyncMock(return_value=redis)):
            response = await acquire(
                _request(),
                session,
                "ytdlp",
                candidate.provider_ref,
                candidate.model_dump_json(),
                "Test Artist - Retry Track",
            )

        jobs = (await session.execute(select(AcquisitionJobRow))).scalars().all()

    await engine.dispose()
    assert "Queued" in response.body.decode()
    assert len(jobs) == 2
    redis.enqueue_job.assert_awaited_once()


async def test_playlist_batch_reports_partial_failure_and_retries_only_failed() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    candidates = [
        TrackCandidate(
            provider="ytdlp",
            provider_ref=f"https://youtu.be/{index}",
            title=f"Track {index}",
            artist="Test Artist",
        ).model_dump_json()
        for index in range(2)
    ]
    first_redis = AsyncMock()
    first_redis.enqueue_job.side_effect = [None, RuntimeError("redis write failed")]
    async with sessions() as session:
        with patch(
            "service.api.routes.playlists.arq_pool",
            lambda: _pool(first_redis),
        ):
            response = await acquire_playlist(
                _request("/playlists/acquire"),
                "https://example.test/playlist",
                "Road Trip",
                "youtube",
                3,
                candidates,
                session,
            )
        rows = (await session.execute(select(AcquisitionJobRow))).scalars().all()
        failed_id = next(row.id for row in rows if row.state == "failed")
        import_id = rows[0].playlist_import_id
        assert import_id is not None

        retry_redis = AsyncMock()
        with patch(
            "service.api.routes.playlists.arq_pool",
            lambda: _pool(retry_redis),
        ):
            retry = await retry_failed_playlist(
                _request(f"/playlists/{import_id}/retry-failed"), import_id, session
            )
        retried_row = await session.get(AcquisitionJobRow, failed_id)

    await engine.dispose()
    assert "1 queued · 3 already owned · 1 failed" in response.body.decode()
    assert f'name="job_ids" value="{failed_id}" checked' in response.body.decode()
    assert "Track 1" in response.body.decode()
    assert retried_row is not None and retried_row.state == "queued"
    assert "2 queued · 3 already owned · 0 failed" in retry.body.decode()
    retry_redis.enqueue_job.assert_awaited_once()


async def test_discography_batch_creates_trackable_album_rows_before_enqueue() -> None:
    from service.db.schema import AlbumAcquisitionJob

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    releases = [
        {"release_group_id": "rg-1", "title": "First", "owned": False},
        {"release_group_id": "rg-2", "title": "Second", "owned": False},
    ]
    redis = AsyncMock()
    async with sessions() as session:
        with (
            patch(
                "service.api.routes.discography._release_entries",
                AsyncMock(return_value=("Test Artist", releases, ["Album"])),
            ),
            patch(
                "service.api.routes.discography.arq_pool",
                lambda: _pool(redis),
            ),
        ):
            response = await discography_acquire_missing(
                _request("/discography/artist/acquire-missing"),
                "artist-id",
                [],
                session,
            )
            repeated = await discography_acquire_missing(
                _request("/discography/artist/acquire-missing"),
                "artist-id",
                [],
                session,
            )
        albums = (await session.execute(select(AlbumAcquisitionJob))).scalars().all()

    await engine.dispose()
    assert len(albums) == 2
    assert "2 queued · 0 already owned · 0 failed" in response.body.decode()
    assert "2 queued · 0 already owned · 0 failed" in repeated.body.decode()
    assert redis.enqueue_job.await_count == 2
    enqueued_ids = {
        call.kwargs["album_job_id"] for call in redis.enqueue_job.await_args_list
    }
    assert enqueued_ids == {album.id for album in albums}


async def test_jobs_exposes_stable_album_and_playlist_batch_anchors() -> None:
    from service.db.schema import AlbumAcquisitionJob, PlaylistImport

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    now = datetime.now(UTC).replace(tzinfo=None)
    candidate = TrackCandidate(
        provider="ytdlp",
        provider_ref="https://youtu.be/batch",
        title="Batch Track",
        artist="Batch Artist",
    )
    async with sessions() as session:
        session.add_all([
            AlbumAcquisitionJob(
                id="album-batch",
                provider="ytdlp",
                album_ref="mbid:release-group",
                album_title="Batch Album",
                album_artist="Batch Artist",
                track_count=1,
                state="running",
                policy="partial_ok",
                query="Batch Artist — Batch Album",
                candidate_json=None,
                created_at=now,
                updated_at=now,
            ),
            PlaylistImport(
                id="playlist-batch",
                url="https://example.test/playlist",
                title="Road Trip",
                source="youtube",
                track_count=1,
                enqueued_count=1,
                owned_count=0,
                state="active",
                created_at=now,
                updated_at=now,
            ),
            AcquisitionJobRow(
                id="album-track",
                provider="ytdlp",
                provider_ref=candidate.provider_ref,
                state="queued",
                query="Batch Track",
                candidate_json=candidate.model_dump_json(),
                album_job_id="album-batch",
                created_at=now,
                updated_at=now,
            ),
            AcquisitionJobRow(
                id="playlist-track",
                provider="ytdlp",
                provider_ref="https://youtu.be/playlist",
                state="queued",
                query="Playlist Track",
                candidate_json=candidate.model_copy(
                    update={"provider_ref": "https://youtu.be/playlist"}
                ).model_dump_json(),
                playlist_import_id="playlist-batch",
                created_at=now,
                updated_at=now,
            ),
        ])
        await session.commit()
        ctx = await _job_list_ctx(session)

    templates_dir = Path(__file__).parents[2] / "service" / "templates"
    html = Environment(
        loader=FileSystemLoader(str(templates_dir)), autoescape=True
    ).get_template("partials/job_list.html").render(**ctx)
    await engine.dispose()
    assert 'id="album-album-batch"' in html
    assert 'id="playlist-playlist-batch"' in html
