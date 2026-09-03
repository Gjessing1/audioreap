# audioreap

Self-hosted companion service for [Navidrome](https://www.navidrome.org/) that enables on-demand music acquisition from cloud sources (YouTube via yt-dlp). Acquired tracks become permanent local library entries.

## Quick start

```bash
cp .env.example .env   # fill in AUDIOREAP_NAVIDROME_PASSWORD etc.
docker compose up -d
```

`GET http://localhost:8000/health` → `{"status":"ok",...}`

## Android app

audioreap installs as an Android app: a Capacitor shell (`android/`) whose WebView loads
this same UI from your own server. There is no bundled copy of the app, so a deploy
updates it like a browser reload — the APK only changes when the shell itself does.

Once a release has been published, **Settings → Android app** offers the download; an
installed app finds its own update there too. Build and publish one with:

```bash
npm install
npm run android:release   # needs the Android SDK and release signing; see scripts/
```

## Dev

```bash
uv sync --extra dev
uv run pytest -m "not e2e"
uv run ruff check .
uv run mypy service/
```

Pytest always reports skip reasons. Tests marked `requires_ffmpeg` are expected
to skip on hosts without FFmpeg and run normally in the app container, whose
image includes it. The suite's mocked cache and API tests do not require
network access.
