"""A search wrapper must never become the downloaded video's identity."""
from pathlib import Path


async def test_search_download_preserves_actual_video_details(tmp_path: Path, monkeypatch) -> None:
    import yt_dlp
    from service.providers.ytdlp import YtdlpProvider

    audio = tmp_path / "video-id.webm"
    audio.write_bytes(b"audio")
    video = {
        "id": "video-id", "title": "Artist - Song (Official Audio)",
        "channel": "Artist Official", "duration": 201,
        "webpage_url": "https://www.youtube.com/watch?v=video-id",
        "ext": "webm", "acodec": "opus", "abr": 160,
    }

    class FakeYoutubeDL:
        def __init__(self, opts):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, *args, **kwargs):
            return {"title": "Artist Song", "entries": [None, video]}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYoutubeDL)
    result = await YtdlpProvider().fetch("ytsearch1:Artist Song", tmp_path)
    assert result.source_url == video["webpage_url"]
    assert result.raw_metadata["title"] == video["title"]
    assert result.raw_metadata["channel"] == "Artist Official"
    assert result.raw_metadata["duration"] == 201
    assert result.codec == "opus"
    assert result.bitrate_kbps == 160


async def test_youtube_search_does_not_block_other_search_branches(monkeypatch) -> None:
    import threading
    import yt_dlp
    from service.core.models import SearchQuery
    from service.providers.ytdlp import YtdlpProvider

    caller_thread = threading.get_ident()
    search_threads = []

    class FakeYoutubeDL:
        def __init__(self, opts):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, *args, **kwargs):
            search_threads.append(threading.get_ident())
            return {"entries": [{"id": "video", "title": "Artist - Song", "duration": 200}]}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYoutubeDL)
    candidates = [c async for c in YtdlpProvider().search(SearchQuery(q="Artist Song"))]
    assert candidates[0].title == "Song"
    assert search_threads and search_threads[0] != caller_thread
