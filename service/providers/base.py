from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from pathlib import Path

from pydantic import BaseModel

from service.core.models import (
    AlbumCandidate,
    FetchResult,
    ProviderHealth,
    SearchQuery,
    TrackCandidate,
)


class ProviderCapabilities(BaseModel):
    supports_search: bool
    supports_album_search: bool
    supports_quality_selection: bool
    search_is_async: bool
    requires_credentials: bool


class Provider(ABC):
    name: str
    capabilities: ProviderCapabilities

    def search(self, query: SearchQuery) -> AsyncIterator[TrackCandidate]:
        """Return an async iterator of track candidates.

        Implementations are async generator functions. Yields all at once for
        yt-dlp-style providers, or trickles over time for P2P-style providers.
        """
        raise NotImplementedError

    @abstractmethod
    async def fetch(self, provider_ref: str, dest_dir: Path) -> FetchResult:
        """Download/confirm audio file into dest_dir. Returns provenance."""

    async def fetch_album(self, album_ref: str) -> AlbumCandidate:
        """Resolve album_ref to an ordered track list.

        album_ref is typically a playlist URL. Providers without album support
        should raise NotImplementedError (guarded by capabilities.supports_album_search).
        """
        raise NotImplementedError

    @abstractmethod
    async def health_check(self) -> ProviderHealth:
        """Return current reachability status."""
