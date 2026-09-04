"""Background notifications for the Android app: registration and the poll.

Three routes, and they do not authenticate the same way.

``POST /api/push/device`` and ``/api/push/device/unregister`` are called by the web UI,
which is already past whatever gate the deployment uses (basic auth here, an SSO proxy
in front of it). They mint and revoke the credential the shell will present.

``GET /api/push/pending`` is called by an alarm-woken broadcast receiver in the APK
(service/push/pending.py). That request has no session to ride on and cannot follow an
SSO redirect, so it is exempted from the basic-auth middleware in main.py and checks the
device credential itself, unconditionally — this route never inherits a gate's bypass,
whatever the rest of the deployment is configured to do.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from service.db.session import get_session
from service.push.devices import (
    device_for_auth_header,
    issue_device_token,
    revoke_device_token,
)
from service.push.pending import events_json, pending_for_device

router = APIRouter()


@router.post("/api/push/device")
async def register_device(
    request: Request, session: AsyncSession = Depends(get_session)
) -> JSONResponse:
    """Mint a credential for the shell to poll with. Returned once, never again."""
    platform = "android"
    try:
        body = await request.json()
        if isinstance(body, dict) and isinstance(body.get("platform"), str):
            platform = body["platform"]
    except Exception:
        pass  # an empty body is the ordinary case; the default covers it
    token = await issue_device_token(session, platform)
    await session.commit()
    return JSONResponse({"token": token}, headers={"Cache-Control": "no-store"})


@router.post("/api/push/device/unregister")
async def unregister_device(
    request: Request, session: AsyncSession = Depends(get_session)
) -> JSONResponse:
    """Forget a device. Turning notifications off is the caller handing the token back."""
    token = ""
    try:
        body = await request.json()
        if isinstance(body, dict) and isinstance(body.get("token"), str):
            token = body["token"]
    except Exception:
        pass
    revoked = bool(token) and await revoke_device_token(session, token)
    await session.commit()
    # 200 either way: a shell dropping a credential the server already forgot has
    # nothing left to do about it, and neither has the user.
    return JSONResponse({"revoked": revoked}, headers={"Cache-Control": "no-store"})


@router.get("/api/push/pending")
async def pending(
    request: Request, session: AsyncSession = Depends(get_session)
) -> JSONResponse:
    """What this device has missed. Reads its own cursor forward as it answers."""
    device = await device_for_auth_header(
        session, request.headers.get("authorization")
    )
    if device is None:
        # 401 without a WWW-Authenticate challenge: a browser that wanders here should
        # not be prompted for credentials, and the shell tells "revoked" from
        # "unreachable" by the status code alone.
        return JSONResponse(
            {"error": "unauthorized"}, status_code=401,
            headers={"Cache-Control": "no-store"},
        )
    events = await pending_for_device(session, device)
    await session.commit()
    return JSONResponse(events_json(events), headers={"Cache-Control": "no-store"})
