"""Tests for the "For you" suggestion builder (metadata/suggest.py).

MB calls are monkeypatched — these pin the cross-referencing rules: owned
artists are never suggested, relationship links outrank tag-only hits, and
multiple independent signals stack.
"""
from service.metadata.musicbrainz import MBArtist, MBRelatedArtist, _parse_artist_tags
from service.metadata import suggest
from service.metadata.suggest import build_for_you


def _rel(artist_id: str, name: str, relation: str = "member") -> MBRelatedArtist:
    return MBRelatedArtist(artist_id=artist_id, name=name, relation=relation, disambiguation="")


def _tag_artist(artist_id: str, name: str, score: float = 1.0) -> MBArtist:
    return MBArtist(artist_id=artist_id, name=name, disambiguation=None, score=score)


def test_owned_artists_never_suggested(monkeypatch) -> None:
    monkeypatch.setattr(suggest, "get_related_artists",
                        lambda mbid, cache_dir: [_rel("id-owned", "Thom Yorke"),
                                                 _rel("id-new", "Atoms for Peace")])
    monkeypatch.setattr(suggest, "search_artists_by_tag",
                        lambda tag, limit, cache_dir: [_tag_artist("id-seed", "Radiohead")])

    out = build_for_you(
        seed_artists=[("id-seed", "Radiohead")],
        seed_genres=["rock"],
        owned_names={"thom yorke", "radiohead"},
        cache_dir=None,
    )
    assert [s.artist_id for s in out] == ["id-new"]


def test_relationship_outranks_tag_only_hit(monkeypatch) -> None:
    monkeypatch.setattr(suggest, "get_related_artists",
                        lambda mbid, cache_dir: [_rel("id-rel", "Side Project")])
    monkeypatch.setattr(suggest, "search_artists_by_tag",
                        lambda tag, limit, cache_dir: [_tag_artist("id-tag", "Random Tagged", 1.0)])

    out = build_for_you([("id-seed", "Seed")], ["shoegaze"], set(), None)
    assert out[0].artist_id == "id-rel"
    assert out[0].score > out[1].score


def test_signals_stack_and_reasons_accumulate(monkeypatch) -> None:
    monkeypatch.setattr(suggest, "get_related_artists",
                        lambda mbid, cache_dir: [_rel("id-both", "Slowdive")])
    monkeypatch.setattr(suggest, "search_artists_by_tag",
                        lambda tag, limit, cache_dir: [_tag_artist("id-both", "Slowdive")])

    out = build_for_you([("id-seed", "Ride")], ["shoegaze"], set(), None)
    assert len(out) == 1
    s = out[0]
    assert s.score > suggest._RELATION_WEIGHT  # relationship + tag stacked
    assert any("member" in r for r in s.reasons)
    assert any("shoegaze" in r for r in s.reasons)


def test_empty_seeds_yield_nothing(monkeypatch) -> None:
    monkeypatch.setattr(suggest, "get_related_artists",
                        lambda mbid, cache_dir: (_ for _ in ()).throw(AssertionError("should not be called")))
    assert build_for_you([], [], set(), None) == []


def test_parse_artist_tags_orders_by_votes_and_caps() -> None:
    raw = {"tag-list": [
        {"name": "rock", "count": "3"},
        {"name": "shoegaze", "count": "12"},
        {"name": "dream pop", "count": 7},
        {"name": "seen live", "count": 5},
        {"name": "british", "count": 1},
        {"count": 9},          # nameless entry ignored
        "not-a-dict",          # junk ignored
    ]}
    assert _parse_artist_tags(raw) == ["shoegaze", "dream pop", "seen live", "rock"]


def test_parse_artist_tags_missing_list() -> None:
    assert _parse_artist_tags({}) == []


def test_tag_search_drops_special_purpose_artists(monkeypatch) -> None:
    import musicbrainzngs

    from service.metadata.musicbrainz import search_artists_by_tag

    monkeypatch.setattr(musicbrainzngs, "search_artists", lambda **kw: {"artist-list": [
        {"id": "89ad4ac3-39f7-470e-963a-56509c546377", "name": "Various Artists", "ext:score": "100"},
        {"id": "aaaa", "name": "Slowdive", "ext:score": "98"},
    ]})
    out = search_artists_by_tag("shoegaze", cache_dir=None)
    assert [a.name for a in out] == ["Slowdive"]
