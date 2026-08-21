"""Contracts for the Jobs triage queue and review-first ordering."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from service.api.routes.jobs import (
    _REJECTION_META_KEY,
    _job_list_ctx,
    _jobs_view,
    _reject_row,
    _restore_rejected_row,
)
from service.config import settings
from service.core.models import TrackCandidate
from service.db.schema import AcquisitionJobRow, Base


def _row(
    job_id: str,
    state: str,
    now: datetime,
    *,
    staging_path: Path | None = None,
    meta: dict[str, object] | None = None,
) -> AcquisitionJobRow:
    candidate = TrackCandidate(
        provider="ytdlp",
        provider_ref=f"https://youtu.be/{job_id}",
        title=f"Title {job_id}",
        artist="Test Artist",
    )
    return AcquisitionJobRow(
        id=job_id,
        provider="ytdlp",
        provider_ref=candidate.provider_ref,
        state=state,
        query=f"Test Artist - Title {job_id}",
        candidate_json=candidate.model_dump_json(),
        staging_path=str(staging_path) if staging_path else None,
        resolved_metadata_json=json.dumps(meta) if meta else None,
        created_at=now,
        updated_at=now,
    )


async def test_default_queue_is_review_with_counts_and_risk_first_order(
    tmp_path: Path,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    now = datetime.now(UTC).replace(tzinfo=None)
    paths = {name: tmp_path / f"{name}.ogg" for name in ("flagged", "probable", "verified")}
    for path in paths.values():
        path.touch()

    common: dict[str, object] = {"title": "Track", "artist": "Artist"}
    async with sessions() as session:
        session.add_all([
            _row(
                "verified",
                "needs_review",
                now,
                staging_path=paths["verified"],
                meta={**common, "mb_match_source": "acoustid"},
            ),
            _row(
                "probable",
                "needs_review",
                now + timedelta(seconds=1),
                staging_path=paths["probable"],
                meta={**common, "mb_match_source": "text_search", "text_search_similarity": 0.7},
            ),
            _row(
                "flagged",
                "needs_review",
                now + timedelta(seconds=2),
                staging_path=paths["flagged"],
                meta={**common, "force_staging_reason": "Artist mismatch"},
            ),
            _row("active", "queued", now),
            _row("failed", "failed", now),
            _row("done", "done", now),
        ])
        await session.commit()
        ctx = await _job_list_ctx(session)

    await engine.dispose()
    assert ctx["jobs_view"] == "review"
    assert ctx["queue_counts"] == {
        "review": 3,
        "active": 1,
        "failed": 1,
        "completed": 1,
    }
    assert [group["job"].id for group in ctx["review_groups"]] == [
        "flagged",
        "probable",
        "verified",
    ]
    assert ctx["review_groups"][0]["reason"] == "Artist mismatch"
    assert "audio fingerprint confirmed" in ctx["review_groups"][2]["reason"]


async def test_explicit_failed_view_is_isolated_and_deep_linkable() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    now = datetime.now(UTC).replace(tzinfo=None)
    async with sessions() as session:
        session.add_all([
            _row("failed", "failed", now),
            _row("done", "done", now),
            _row("active", "downloading", now),
        ])
        await session.commit()
        ctx = await _job_list_ctx(session, "failed")

    templates = Path(__file__).parents[2] / "service" / "templates"
    html = Environment(
        loader=FileSystemLoader(str(templates)), autoescape=True
    ).get_template("partials/job_list.html").render(**ctx)
    await engine.dispose()

    assert ctx["jobs_view"] == "failed"
    assert [job.id for job in ctx["failed_jobs"]] == ["failed"]
    assert ctx["completed"] == []
    assert 'href="/jobs?view=failed"' in html
    assert 'aria-current="page"' in html
    assert "Title failed" in html
    assert "Title done" not in html


def test_queue_default_preference_and_interaction_contracts() -> None:
    assert _jobs_view(None, {"review": 2, "active": 4, "failed": 1, "completed": 9}) == "review"
    assert _jobs_view(None, {"review": 0, "active": 4, "failed": 1, "completed": 9}) == "active"
    assert _jobs_view("completed", {"review": 2, "active": 4, "failed": 1, "completed": 9}) == "completed"

    root = Path(__file__).parents[2]
    app_js = (root / "service" / "static" / "app.js").read_text()
    review = (root / "service" / "templates" / "partials" / "review_card.html").read_text()
    queue = (root / "service" / "templates" / "partials" / "job_list.html").read_text()
    assert "_hoveredCard" not in app_js
    assert "_focusedReviewCard" in app_js
    assert "reviewApproved" in app_js
    assert 'name="advance" value="next"' in review
    assert "data-requires-job-selection disabled" in queue
    assert "selected in " in app_js
    assert "window.confirm" not in app_js
    assert "requestConfirmation" in app_js

    base = (root / "service" / "templates" / "base.html").read_text()
    assert 'id="confirm-dialog"' in base
    assert 'aria-labelledby="confirm-dialog-title"' in base


def test_rejected_staging_file_can_be_restored_to_review(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "staging_dir", tmp_path)
    now = datetime.now(UTC).replace(tzinfo=None)
    original = tmp_path / "job-1" / "track.ogg"
    original.parent.mkdir()
    original.write_bytes(b"audio")
    row = _row(
        "reject-me",
        "needs_review",
        now,
        staging_path=original,
        meta={"title": "Track", "artist": "Artist"},
    )

    is_enrichment, can_restore = _reject_row(row)

    assert not is_enrichment
    assert can_restore
    assert row.state == "failed"
    assert row.staging_path is None
    assert not original.exists()
    rejection = json.loads(row.resolved_metadata_json)[_REJECTION_META_KEY]
    trashed = Path(rejection["trash_path"])
    assert trashed.read_bytes() == b"audio"
    assert rejection["original_path"] == str(original)

    _restore_rejected_row(row)

    assert row.state == "needs_review"
    assert row.failure_class is None
    assert row.error is None
    assert row.staging_path == str(original)
    assert original.read_bytes() == b"audio"
    assert _REJECTION_META_KEY not in json.loads(row.resolved_metadata_json)


def test_rejecting_enrichment_never_trashes_library_file(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "staging_dir", tmp_path / "staging")
    now = datetime.now(UTC).replace(tzinfo=None)
    library_file = tmp_path / "music" / "track.ogg"
    library_file.parent.mkdir()
    library_file.write_bytes(b"library audio")
    row = _row(
        "enrichment",
        "needs_review",
        now,
        staging_path=library_file,
        meta={"is_enrichment": True, "title": "Track"},
    )

    is_enrichment, can_restore = _reject_row(row)

    assert is_enrichment
    assert not can_restore
    assert library_file.read_bytes() == b"library audio"
    assert row.state == "failed"
