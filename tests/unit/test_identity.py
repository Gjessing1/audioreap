
from service.core.identity import make_id


def test_mb_id_takes_priority() -> None:
    id1 = make_id("Artist", "Title", 240, musicbrainz_recording_id="abc-123")
    assert id1 == "mb:abc-123"


def test_hash_id_format() -> None:
    id1 = make_id("Artist", "Title", 240)
    assert id1.startswith("hash:")
    assert len(id1) > len("hash:")


def test_stability_across_calls() -> None:
    id1 = make_id("Daft Punk", "Around the World", 429)
    id2 = make_id("Daft Punk", "Around the World", 429)
    assert id1 == id2


def test_noise_in_title_stripped() -> None:
    id1 = make_id("Daft Punk", "Around the World", 429)
    id2 = make_id("Daft Punk", "Around the World (Official Video)", 429)
    assert id1 == id2


def test_diacritics_stripped() -> None:
    id1 = make_id("Bjork", "Joga", 300)
    id2 = make_id("Björk", "Jóga", 300)
    assert id1 == id2


def test_case_insensitive() -> None:
    id1 = make_id("DAFT PUNK", "AROUND THE WORLD", 429)
    id2 = make_id("daft punk", "around the world", 429)
    assert id1 == id2


def test_duration_bucket_tolerance() -> None:
    # Within 2-second bucket → same ID
    id1 = make_id("Artist", "Title", 240)
    id2 = make_id("Artist", "Title", 241)
    assert id1 == id2


def test_duration_bucket_boundary() -> None:
    # 240 and 243 are in different buckets (240→240, 243→244)
    id1 = make_id("Artist", "Title", 240)
    id2 = make_id("Artist", "Title", 243)
    assert id1 != id2


def test_none_duration_stable() -> None:
    id1 = make_id("Artist", "Title", None)
    id2 = make_id("Artist", "Title", None)
    assert id1 == id2


def test_different_artists_differ() -> None:
    id1 = make_id("Artist A", "Same Title", 240)
    id2 = make_id("Artist B", "Same Title", 240)
    assert id1 != id2


def test_different_titles_differ() -> None:
    id1 = make_id("Same Artist", "Title A", 240)
    id2 = make_id("Same Artist", "Title B", 240)
    assert id1 != id2


def test_known_collision_same_duration() -> None:
    # Cover versions with identical duration → same ID (accepted v1 behaviour)
    id1 = make_id("Original Artist", "My Song", 200)
    id2 = make_id("Cover Artist", "My Song", 200)
    # Not necessarily equal — different artists produce different IDs
    # (collision only if artist+title+duration all match after normalization)
    assert id1 != id2


def test_hash_is_url_safe() -> None:
    id1 = make_id("Artist", "Title", 240)
    # prefix is "hash:", digest is hex — both are URL-safe
    prefix, digest = id1.split(":", 1)
    assert prefix == "hash"
    assert all(c in "0123456789abcdef" for c in digest)
