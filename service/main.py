from datetime import datetime

from fastapi import FastAPI

from service.config import settings

app = FastAPI(title="audioreap", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "version": "0.1.0",
        "music_dir": str(settings.music_dir),
        "timestamp": datetime.utcnow().isoformat(),
    }
