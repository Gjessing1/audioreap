"""The Android app's push routes, end to end through the real app and its middleware.

The point of these is the *seam*: `/api/push/pending` is exempted from the basic-auth
middleware so an alarm-woken broadcast receiver can reach it at all, and that exemption
is only safe because the route checks a device credential itself. A regression that
removed the check would leave the queue readable by anyone, and nothing else in the test
suite would notice.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from service.config import settings
from service.db.schema import AcquisitionJobRow, Base
from service.main import app

NOW = datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
async def client(tmp_path: Path):  # type: ignore[misc]
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'push.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session, session.begin():
        session.add(AcquisitionJobRow(
            id="solo", provider="ytdlp", provider_ref="https://youtu.be/solo",
            state="needs_review",
            resolved_metadata_json=json.dumps({"title": "Come as You Are",
                                               "artist": "Nirvana"}),
            created_at=NOW, updated_at=NOW,
        ))

    from service.db import session as session_module

    async def override_session():  # type: ignore[override]
        async with factory() as s:
            yield s

    app.dependency_overrides[session_module.get_session] = override_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
    await engine.dispose()


async def test_register_then_poll_then_unregister(client) -> None:
    token = (await client.post("/api/push/device", json={"platform": "android"})).json()["token"]

    poll = await client.get("/api/push/pending", headers={"Authorization": f"Bearer {token}"})

    assert poll.status_code == 200
    assert poll.headers["cache-control"] == "no-store"
    assert [event["title"] for event in poll.json()["events"]] == ["Come as You Are — Nirvana"]
    # The cursor moved with the answer, so the next poll is quiet.
    again = await client.get("/api/push/pending", headers={"Authorization": f"Bearer {token}"})
    assert again.json() == {"events": []}

    assert (await client.post("/api/push/device/unregister",
                              json={"token": token})).json() == {"revoked": True}
    revoked = await client.get("/api/push/pending",
                               headers={"Authorization": f"Bearer {token}"})
    assert revoked.status_code == 401


async def test_the_poll_needs_a_credential_even_with_the_ui_password_off(client) -> None:
    """The exemption is not a bypass: no gate anywhere means this route is the gate."""
    assert not settings.ui_password  # the deployment shape this guards against

    assert (await client.get("/api/push/pending")).status_code == 401
    assert (await client.get("/api/push/pending",
                             headers={"Authorization": "Bearer nope"})).status_code == 401


async def test_the_poll_is_not_challenged_by_basic_auth_but_registration_is(
    client, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "ui_password", "hunter2")

    poll = await client.get("/api/push/pending")
    register = await client.post("/api/push/device")

    # Both 401, from different places: the route answers JSON, the middleware answers
    # with a challenge a browser would prompt on — and an alarm could never satisfy.
    assert poll.status_code == 401
    assert "www-authenticate" not in poll.headers
    assert poll.json() == {"error": "unauthorized"}
    assert register.status_code == 401
    assert register.headers["www-authenticate"].startswith("Basic")
