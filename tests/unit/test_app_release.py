"""Unit tests for the Android release endpoints' metadata reader.

Everything here is about refusing a release rather than serving one. The APK is offered
unauthenticated to any phone that asks, and the filename in version.json becomes both a
path join and a Content-Disposition header, so a malformed or half-written release must
read as "nothing published" — never as a usable one.
"""
import json
from pathlib import Path

import pytest

from service.api.routes.app_release import read_published_app

GOOD_SHA = "a" * 64


def _publish(app_dir: Path, **overrides) -> Path:
    metadata = {
        "versionCode": 3,
        "versionName": "0.2.1",
        "file": "audioreap-0.2.1.apk",
        "sha256": GOOD_SHA,
    }
    metadata.update(overrides)
    (app_dir / "version.json").write_text(json.dumps(metadata))
    name = metadata.get("file")
    if isinstance(name, str) and name == Path(name).name:
        (app_dir / name).write_bytes(b"apk bytes")
    return app_dir


def test_reads_a_published_release(tmp_path):
    published = read_published_app(_publish(tmp_path))

    assert published.version_code == 3
    assert published.version_name == "0.2.1"
    assert published.file == "audioreap-0.2.1.apk"
    assert published.sha256 == GOOD_SHA
    assert published.bytes == len(b"apk bytes")
    assert published.apk_path == tmp_path / "audioreap-0.2.1.apk"


def test_nothing_published(tmp_path):
    with pytest.raises((FileNotFoundError, OSError)):
        read_published_app(tmp_path)


def test_metadata_without_its_apk(tmp_path):
    """The publish script copies the APK first, but a hand-edited dir can disagree."""
    (tmp_path / "version.json").write_text(json.dumps({
        "versionCode": 3, "versionName": "0.2.1",
        "file": "audioreap-0.2.1.apk", "sha256": GOOD_SHA,
    }))

    with pytest.raises(FileNotFoundError):
        read_published_app(tmp_path)


@pytest.mark.parametrize("overrides", [
    pytest.param({"versionCode": 0}, id="version code below one"),
    pytest.param({"versionCode": "3"}, id="version code as a string"),
    pytest.param({"versionCode": True}, id="version code as a bool"),
    pytest.param({"versionName": "0.2 1"}, id="version name with a space"),
    pytest.param({"versionName": ""}, id="empty version name"),
    pytest.param({"sha256": "nope"}, id="sha256 that is not a digest"),
    pytest.param({"sha256": GOOD_SHA.upper()}, id="uppercase sha256"),
    pytest.param({"file": "evil.apk"}, id="filename outside the audioreap- prefix"),
    pytest.param({"file": "audioreap-0.2.1.zip"}, id="filename that is not an apk"),
    pytest.param({"file": 7}, id="filename that is not a string"),
])
def test_rejects_malformed_metadata(tmp_path, overrides):
    with pytest.raises(ValueError):
        read_published_app(_publish(tmp_path, **overrides))


@pytest.mark.parametrize("name", [
    "../../etc/passwd",
    "audioreap-x/../../../etc/passwd.apk",
    "/etc/passwd",
])
def test_rejects_a_filename_that_leaves_the_directory(tmp_path, name):
    """The name is joined onto the app dir, so it has to be a plain basename."""
    (tmp_path / "version.json").write_text(json.dumps({
        "versionCode": 3, "versionName": "0.2.1", "file": name, "sha256": GOOD_SHA,
    }))

    with pytest.raises(ValueError):
        read_published_app(tmp_path)


def test_rejects_metadata_that_is_not_an_object(tmp_path):
    (tmp_path / "version.json").write_text(json.dumps(["audioreap-0.2.1.apk"]))

    with pytest.raises(ValueError):
        read_published_app(tmp_path)


def test_rejects_unparseable_metadata(tmp_path):
    """A version.json caught mid-write is not a release."""
    (tmp_path / "version.json").write_text('{"versionCode": 3, "versi')

    with pytest.raises(ValueError):
        read_published_app(tmp_path)


def test_a_directory_named_like_the_apk_is_not_the_apk(tmp_path):
    (tmp_path / "version.json").write_text(json.dumps({
        "versionCode": 3, "versionName": "0.2.1",
        "file": "audioreap-0.2.1.apk", "sha256": GOOD_SHA,
    }))
    (tmp_path / "audioreap-0.2.1.apk").mkdir()

    with pytest.raises(FileNotFoundError):
        read_published_app(tmp_path)
