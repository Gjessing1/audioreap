"""What the Android app has not been told about yet.

The APK holds no connection to audioreap. Android only lets an app keep a socket open
indefinitely from a foreground service, and a foreground service must post a permanent
notification — an "audioreap is running" notice in the shade forever, which is a poor
trade for a phone that can simply ask. So the device wakes on its own alarm every few
minutes and asks this: *what have I missed?*

That inverts where the state lives, and this is the state. A device row carries a cursor
(``last_event_at``); everything that became actionable after it is what the phone is
still owed a notification for. "Actionable" is checked at poll time, not at event time,
so a batch approved from the desktop in the meantime is simply not mentioned — the
clearest possible signal that a notification for it would be noise.

The cursor advances as the answer is written, not on a later acknowledgement from the
phone. An answer lost in flight therefore goes un-notified rather than being offered
again on every subsequent poll: a duplicate alert about a queue the user has already
seen is worse than a missed one, and the queue itself is never lost — it is sitting in
/jobs either way.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from service.db.schema import (
    AcquisitionJobRow,
    AlbumAcquisitionJob,
    PlaylistImport,
    PushDevice,
)
from service.push.devices import touch_device

# Ceiling on one answer, so a long offline stretch is a summary rather than a flood of
# notifications for a queue the user is about to see in full anyway.
_PENDING_LIMIT = 10
# Nothing older than this is worth a notification by the time the phone asks.
_PENDING_MAX_AGE = timedelta(hours=24)

# A row the user can do something about. Both are terminal for the pipeline: a track
# waiting at the review gate, or one that gave up.
_ACTIONABLE = ("needs_review", "failed")
# Where a job stops moving. Anything else — queued, downloading, importing, placing —
# is a batch still landing, and a notification then would be about half an album. Stated
# as the settled set rather than the in-flight one deliberately: a state added to the
# pipeline later must read as "still working", never as "finished".
_SETTLED = ("needs_review", "failed", "done", "cancelled")

_REVIEW_URL = "/jobs?view=review"
_FAILED_URL = "/jobs?view=failed"


@dataclass(frozen=True)
class PushEvent:
    """One notification. ``id`` is also the shade tag, so a repost replaces itself."""

    id: str
    title: str
    body: str
    url: str
    event_at: datetime

    def as_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "body": self.body,
            "url": self.url,
            "eventAt": self.event_at.replace(tzinfo=UTC).isoformat(),
        }


async def pending_for_device(
    session: AsyncSession, device: PushDevice
) -> list[PushEvent]:
    """The notifications this device is still owed, oldest first.

    Oldest first because that is the order a live stream would have produced, and the
    shade stacks in arrival order. Advances the device's cursor past everything returned.
    """
    await touch_device(session, device)

    floor = datetime.now(UTC).replace(tzinfo=None) - _PENDING_MAX_AGE
    since = max(device.last_event_at, floor) if device.last_event_at else floor

    events = [
        *await _batch_events(
            session, since,
            column=AcquisitionJobRow.album_job_id,
            prefix="album",
            labels=_album_labels,
            fallback="Album download",
        ),
        *await _batch_events(
            session, since,
            column=AcquisitionJobRow.playlist_import_id,
            prefix="playlist",
            labels=_playlist_labels,
            fallback="Playlist import",
        ),
        *await _solo_events(session, since),
    ]
    events.sort(key=lambda event: event.event_at)
    # Keep the newest when there are too many: an old event the user never saw matters
    # less than the one that just happened.
    events = events[-_PENDING_LIMIT:]

    if events:
        device.last_event_at = max(event.event_at for event in events)
        await session.flush()
    return events


# ── Batches ───────────────────────────────────────────────────────────────────

async def _batch_events(
    session: AsyncSession,
    since: datetime,
    *,
    column: InstrumentedAttribute,
    prefix: str,
    labels: Callable[[AsyncSession, set[str]], Awaitable[dict[str, str]]],
    fallback: str,
) -> list[PushEvent]:
    """One notification per album batch or playlist import, not per track.

    A batch is worth mentioning once it has *settled* — nothing of it is still queued or
    downloading — and only for the tracks that still need the user: a twelve-track album
    is one line in the shade, which is the whole point of waiting for the batch instead
    of notifying each track as it lands.
    """
    recent = (await session.execute(
        select(column)
        .where(
            column.is_not(None),
            AcquisitionJobRow.state.in_(_ACTIONABLE),
            AcquisitionJobRow.updated_at > since,
        )
        .distinct()
    )).scalars().all()
    batch_ids = {value for value in recent if value}
    if not batch_ids:
        return []

    rows = (await session.execute(
        select(AcquisitionJobRow).where(column.in_(batch_ids))
    )).scalars().all()
    grouped: dict[str, list[AcquisitionJobRow]] = {}
    for row in rows:
        grouped.setdefault(getattr(row, column.key), []).append(row)

    names = await labels(session, batch_ids)
    events = []
    for batch_id, members in grouped.items():
        if any(row.state not in _SETTLED for row in members):
            continue  # still landing
        review = [row for row in members if row.state == "needs_review"]
        failed = [row for row in members if row.state == "failed"]
        if not review and not failed:
            continue  # every track was approved before the phone asked
        # The event time comes from the actionable rows alone, so approving part of a
        # batch on another device cannot push it back over the cursor and re-notify.
        event_at = max(row.updated_at for row in (review + failed))
        if event_at <= since:
            continue
        events.append(PushEvent(
            id=f"{prefix}:{batch_id}",
            title=names.get(batch_id) or fallback,
            body=_batch_body(len(review), len(failed)),
            url=_REVIEW_URL if review else _FAILED_URL,
            event_at=event_at,
        ))
    return events


def _batch_body(review: int, failed: int) -> str:
    parts = []
    if review:
        parts.append(f"{review} {_tracks(review)} ready to review")
    if failed:
        parts.append(
            f"{failed} failed" if review else f"{failed} {_tracks(failed)} failed to download"
        )
    return " · ".join(parts)


def _tracks(count: int) -> str:
    return "track" if count == 1 else "tracks"


async def _album_labels(session: AsyncSession, ids: set[str]) -> dict[str, str]:
    rows = (await session.execute(
        select(AlbumAcquisitionJob).where(AlbumAcquisitionJob.id.in_(ids))
    )).scalars().all()
    return {
        row.id: " — ".join(part for part in (row.album_artist, row.album_title) if part)
        for row in rows
    }


async def _playlist_labels(session: AsyncSession, ids: set[str]) -> dict[str, str]:
    rows = (await session.execute(
        select(PlaylistImport).where(PlaylistImport.id.in_(ids))
    )).scalars().all()
    return {row.id: (row.title or "") for row in rows}


# ── Standalone downloads ──────────────────────────────────────────────────────

async def _solo_events(session: AsyncSession, since: datetime) -> list[PushEvent]:
    """A track downloaded on its own gets its own line — there is no batch to wait for."""
    rows = (await session.execute(
        select(AcquisitionJobRow).where(
            AcquisitionJobRow.album_job_id.is_(None),
            AcquisitionJobRow.playlist_import_id.is_(None),
            AcquisitionJobRow.state.in_(_ACTIONABLE),
            AcquisitionJobRow.updated_at > since,
        )
    )).scalars().all()
    return [
        PushEvent(
            id=f"job:{row.id}",
            title=_solo_title(row),
            body="Ready to review" if row.state == "needs_review" else "Download failed",
            url=_REVIEW_URL if row.state == "needs_review" else _FAILED_URL,
            event_at=row.updated_at,
        )
        for row in rows
    ]


def _solo_title(row: AcquisitionJobRow) -> str:
    """The best name this row can offer, in descending order of how settled it is.

    The resolved metadata is what the review card shows and what the tags will say; the
    candidate is what was asked for; the query is what the user typed. A failed job has
    no resolved metadata at all, which is exactly when the last two matter.
    """
    for payload in (row.resolved_metadata_json, row.candidate_json):
        if not payload:
            continue
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        title = _text(data.get("title"))
        artist = _text(data.get("artist"))
        if title:
            return f"{title} — {artist}" if artist else title
    return _text(row.query) or "Download"


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def events_json(events: Sequence[PushEvent]) -> dict[str, object]:
    return {"events": [event.as_json() for event in events]}
