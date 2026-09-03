"""HTMX web UI — aggregates the per-section route modules.

Route modules live in service/api/routes/; shared infrastructure (templates,
scan scheduling, cross-section helpers) in service/api/shared.py.
"""
from fastapi import APIRouter

from service.api.routes import (
    acquire,
    jobs,
    library,
    tracks,
    albums,
    artists,
    artwork,
    health,
    discography,
    playlists,
    admin,
    app_release,
)
from service.api.shared import templates  # noqa: F401  (re-export)

router = APIRouter()
for _mod in (acquire, jobs, library, tracks, albums, artists, artwork, health,
             discography, playlists, admin, app_release):
    router.include_router(_mod.router)
