from pathlib import Path

import pytest

from tests.fake_provider import FakeProvider

FIXTURE_AUDIO_DIR = Path(__file__).parent / "fixtures" / "audio"


@pytest.fixture
def fixture_audio_dir() -> Path:
    return FIXTURE_AUDIO_DIR


@pytest.fixture
def fake_provider(fixture_audio_dir: Path) -> FakeProvider:
    return FakeProvider(fixture_audio_dir)
