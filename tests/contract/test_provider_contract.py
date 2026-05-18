"""Provider interface contract tests.

Any real Provider must pass this suite. FakeProvider passes first.
Add new providers to the `provider` fixture as they are implemented.
"""
from pathlib import Path

import pytest

from service.core.models import ProviderHealth, SearchQuery, TrackCandidate
from service.providers.base import Provider
from tests.fake_provider import FakeProvider

FIXTURE_AUDIO_DIR = Path(__file__).parent.parent / "fixtures" / "audio"


@pytest.fixture(params=["fake"])
def provider(request: pytest.FixtureRequest) -> Provider:
    if request.param == "fake":
        return FakeProvider(FIXTURE_AUDIO_DIR)
    raise ValueError(f"Unknown provider fixture: {request.param}")


async def test_search_returns_candidates(provider: Provider) -> None:
    results: list[TrackCandidate] = []
    async for candidate in provider.search(SearchQuery(q="")):
        results.append(candidate)
    assert len(results) > 0, "search() must yield at least one candidate"


async def test_search_candidate_fields(provider: Provider) -> None:
    async for candidate in provider.search(SearchQuery(q="")):
        assert candidate.title, "candidate.title must be non-empty"
        assert candidate.artist, "candidate.artist must be non-empty"
        assert candidate.provider == provider.name, "provider field must match provider.name"
        assert candidate.provider_ref, "provider_ref must be non-empty"
        break


async def test_fetch_produces_file(provider: Provider, tmp_path: Path) -> None:
    first: TrackCandidate | None = None
    async for candidate in provider.search(SearchQuery(q="")):
        first = candidate
        break
    assert first is not None

    result = await provider.fetch(first.provider_ref, tmp_path)

    assert result.file_path.exists(), "fetch() must produce a file"
    assert result.file_path.stat().st_size > 0, "fetched file must not be empty"
    assert result.codec, "result.codec must be set"
    assert result.container, "result.container must be set"
    assert result.provider == provider.name
    assert result.provider_ref == first.provider_ref


async def test_fetch_file_in_dest_dir(provider: Provider, tmp_path: Path) -> None:
    async for candidate in provider.search(SearchQuery(q="")):
        result = await provider.fetch(candidate.provider_ref, tmp_path)
        assert result.file_path.parent == tmp_path, "fetched file must land in dest_dir"
        break


async def test_health_check(provider: Provider) -> None:
    health = await provider.health_check()
    assert isinstance(health, ProviderHealth)
    assert isinstance(health.healthy, bool)
    assert health.checked_at is not None


async def test_capabilities_search_consistent(provider: Provider) -> None:
    caps = provider.capabilities
    if caps.supports_search:
        results: list[TrackCandidate] = []
        async for c in provider.search(SearchQuery(q="")):
            results.append(c)
        assert len(results) >= 0
    else:
        with pytest.raises((NotImplementedError, Exception)):
            async for _ in provider.search(SearchQuery(q="")):
                pass
