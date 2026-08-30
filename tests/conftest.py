import shutil
from pathlib import Path

import pytest

from tests.fake_provider import FakeProvider

FIXTURE_AUDIO_DIR = Path(__file__).parent / "fixtures" / "audio"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip tests with declared host-tool requirements when the tool is absent."""
    if shutil.which("ffmpeg") is not None:
        return

    skip_ffmpeg = pytest.mark.skip(
        reason="requires ffmpeg; install it locally or run the tests in the app container"
    )
    for item in items:
        if "requires_ffmpeg" in item.keywords:
            item.add_marker(skip_ffmpeg)


@pytest.fixture
def fixture_audio_dir() -> Path:
    return FIXTURE_AUDIO_DIR


@pytest.fixture
def fake_provider(fixture_audio_dir: Path) -> FakeProvider:
    return FakeProvider(fixture_audio_dir)
