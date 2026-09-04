"""Contracts for the Android app's background notification poll.

The phone acts on what these return without a person in the loop, so the cases that
matter are the ones where a notification would be *wrong*: a batch still downloading, a
queue already cleared from the desktop, or the same batch offered twice.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from service.db.schema import (
    AcquisitionJobRow,
    AlbumAcquisitionJob,
    Base,
    PlaylistImport,
    PushDevice,
)
from service.push.devices import (
    device_for_auth_header,
    hash_device_token,
    issue_device_token,
    revoke_device_token,
)
from service.push.pending import pending_for_device

NOW = datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


def _job(job_id: str, state: str, *, at: datetime = NOW, album: str | None = None,
         playlist: str | None = None, title: str | None = None,
         query: str | None = None) -> AcquisitionJobRow:
    return AcquisitionJobRow(
        id=job_id,
        provider="ytdlp",
        provider_ref=f"https://youtu.be/{job_id}",
        state=state,
        album_job_id=album,
        playlist_import_id=playlist,
        query=query,
        resolved_metadata_json=(
            json.dumps({"title": title, "artist": "Nirvana"}) if title else None
        ),
        created_at=at,
        updated_at=at,
    )


async def _device(session) -> PushDevice:
    token = await issue_device_token(session)
    await session.flush()
    device = await device_for_auth_header(session, f"Bearer {token}")
    assert device is not None
    return device


# ── Credentials ───────────────────────────────────────────────────────────────

async def test_only_the_hash_is_stored_and_the_token_authenticates(session) -> None:
    token = await issue_device_token(session)
    row = (await session.execute(__import__("sqlalchemy").select(PushDevice))).scalar_one()

    assert token not in json.dumps({c.name: str(getattr(row, c.name))
                                    for c in PushDevice.__table__.columns})
    assert row.token_hash == hash_device_token(token)
    assert (await device_for_auth_header(session, f"Bearer {token}")).id == row.id


@pytest.mark.parametrize("header", [None, "", "Bearer ", "Basic abc", "nonsense"])
async def test_malformed_authorization_headers_are_refused(session, header) -> None:
    await issue_device_token(session)
    assert await device_for_auth_header(session, header) is None


async def test_a_revoked_token_stops_authenticating(session) -> None:
    token = await issue_device_token(session)
    assert await revoke_device_token(session, token) is True
    assert await device_for_auth_header(session, f"Bearer {token}") is None
    assert await revoke_device_token(session, token) is False


# ── What a device is owed ─────────────────────────────────────────────────────

async def test_a_settled_album_batch_is_one_notification(session) -> None:
    session.add(AlbumAcquisitionJob(
        id="alb", provider="mb", album_ref="rg-1", album_title="Nevermind",
        album_artist="Nirvana", state="done", created_at=NOW, updated_at=NOW,
    ))
    for index in range(3):
        session.add(_job(f"t{index}", "needs_review", album="alb"))
    session.add(_job("t3", "failed", album="alb"))
    device = await _device(session)

    events = await pending_for_device(session, device)

    assert len(events) == 1
    assert events[0].id == "album:alb"
    assert events[0].title == "Nirvana — Nevermind"
    assert events[0].body == "3 tracks ready to review · 1 failed"
    assert events[0].url == "/jobs?view=review"


async def test_a_batch_still_downloading_says_nothing(session) -> None:
    session.add(AlbumAcquisitionJob(
        id="alb", provider="mb", album_ref="rg-1", album_title="Nevermind",
        album_artist="Nirvana", state="running", created_at=NOW, updated_at=NOW,
    ))
    session.add(_job("t0", "needs_review", album="alb"))
    session.add(_job("t1", "downloading", album="alb"))
    device = await _device(session)

    assert await pending_for_device(session, device) == []


async def test_a_queue_already_cleared_is_not_mentioned(session) -> None:
    session.add(AlbumAcquisitionJob(
        id="alb", provider="mb", album_ref="rg-1", album_title="Nevermind",
        album_artist="Nirvana", state="done", created_at=NOW, updated_at=NOW,
    ))
    session.add(_job("t0", "done", album="alb"))
    session.add(_job("t1", "done", album="alb"))
    device = await _device(session)

    assert await pending_for_device(session, device) == []


async def test_the_same_batch_is_not_offered_twice(session) -> None:
    session.add(AlbumAcquisitionJob(
        id="alb", provider="mb", album_ref="rg-1", album_title="Nevermind",
        album_artist="Nirvana", state="done", created_at=NOW, updated_at=NOW,
    ))
    session.add(_job("t0", "needs_review", album="alb"))
    session.add(_job("t1", "needs_review", album="alb"))
    device = await _device(session)

    assert len(await pending_for_device(session, device)) == 1
    assert await pending_for_device(session, device) == []


async def test_approving_part_of_a_batch_does_not_re_notify(session) -> None:
    """The event time comes from the rows still waiting, so triage is not an event."""
    session.add(AlbumAcquisitionJob(
        id="alb", provider="mb", album_ref="rg-1", album_title="Nevermind",
        album_artist="Nirvana", state="done", created_at=NOW, updated_at=NOW,
    ))
    session.add(_job("t0", "needs_review", album="alb"))
    session.add(_job("t1", "needs_review", album="alb"))
    device = await _device(session)
    assert len(await pending_for_device(session, device)) == 1

    approved = await session.get(AcquisitionJobRow, "t0")
    approved.state = "done"
    approved.updated_at = NOW + timedelta(minutes=5)
    await session.flush()

    assert await pending_for_device(session, device) == []


async def test_a_playlist_import_groups_like_an_album(session) -> None:
    session.add(PlaylistImport(
        id="pl", url="https://youtube.com/playlist?list=x", title="Road trip",
        source="youtube", state="active", created_at=NOW, updated_at=NOW,
    ))
    for index in range(4):
        session.add(_job(f"p{index}", "needs_review", playlist="pl"))
    device = await _device(session)

    events = await pending_for_device(session, device)

    assert [(event.id, event.title, event.body) for event in events] == [
        ("playlist:pl", "Road trip", "4 tracks ready to review")
    ]


async def test_a_standalone_track_names_itself(session) -> None:
    session.add(_job("solo", "needs_review", title="Smells Like Teen Spirit"))
    device = await _device(session)

    events = await pending_for_device(session, device)

    assert len(events) == 1
    assert events[0].id == "job:solo"
    assert events[0].title == "Smells Like Teen Spirit — Nirvana"
    assert events[0].body == "Ready to review"


async def test_a_failed_standalone_falls_back_to_the_query(session) -> None:
    session.add(_job("solo", "failed", query="nirvana something in the way"))
    device = await _device(session)

    events = await pending_for_device(session, device)

    assert events[0].title == "nirvana something in the way"
    assert events[0].body == "Download failed"
    assert events[0].url == "/jobs?view=failed"


async def test_nothing_older_than_a_day_is_worth_a_notification(session) -> None:
    session.add(_job("stale", "needs_review", at=NOW - timedelta(days=3),
                     title="Old news"))
    device = await _device(session)

    assert await pending_for_device(session, device) == []


async def test_a_long_absence_is_summarised_not_flooded(session) -> None:
    for index in range(25):
        session.add(_job(f"s{index}", "needs_review", title=f"Track {index}",
                         at=NOW - timedelta(minutes=25 - index)))
    device = await _device(session)

    events = await pending_for_device(session, device)

    assert len(events) == 10
    # The newest, oldest first — the order a live stream would have produced.
    assert [event.title for event in events][0] == "Track 15 — Nirvana"
    assert [event.title for event in events][-1] == "Track 24 — Nirvana"


async def test_the_poll_records_that_the_device_asked(session) -> None:
    device = await _device(session)
    assert device.last_seen_at is None

    await pending_for_device(session, device)

    assert device.last_seen_at is not None
