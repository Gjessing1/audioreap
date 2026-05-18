# audioreap

Self-hosted companion service for [Navidrome](https://www.navidrome.org/) that enables on-demand music acquisition from cloud sources (YouTube via yt-dlp). Acquired tracks become permanent local library entries.

## Quick start

```bash
cp .env.example .env   # fill in AUDIOREAP_NAVIDROME_PASSWORD etc.
docker compose up -d
```

`GET http://localhost:8000/health` → `{"status":"ok",...}`

## Dev

```bash
uv sync --extra dev
uv run pytest -m "not e2e"
uv run ruff check .
uv run mypy service/
```

## Architecture

See `CLAUDE.md` (local only, not committed) for full design doc, phasing, and conventions.
