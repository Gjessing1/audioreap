"""Convert AcquisitionJobRow ORM rows to AcquisitionJob Pydantic models.

Kept in core/ so both main.py and api/webui.py can import it without
creating a circular dependency.
"""
from __future__ import annotations

from service.core.models import AcquisitionJob, TrackCandidate, TrackRef
from service.db.schema import AcquisitionJobRow


def job_row_to_model(row: AcquisitionJobRow) -> AcquisitionJob:
    label = row.query or f"{row.provider}:{row.provider_ref}"
    candidate: TrackCandidate | None = None
    if row.candidate_json:
        try:
            candidate = TrackCandidate.model_validate_json(row.candidate_json)
        except Exception:
            pass
    track_ref = TrackRef(
        internal_id=row.track_id or f"job:{row.id}",
        source="cloud",
        status="acquiring" if row.state not in ("done", "failed") else
               "available" if row.state == "done" else "failed",
        title=candidate.title if candidate else label,
        artist=candidate.artist if candidate else "Unknown",
        provider=row.provider,
        provider_ref=row.provider_ref,
    )
    return AcquisitionJob(
        id=row.id,
        track_ref=track_ref,
        state=row.state,  # type: ignore[arg-type]
        progress=row.progress,
        error=row.error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        is_replacement=bool(candidate and candidate.skip_dedup),
    )
