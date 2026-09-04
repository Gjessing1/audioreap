"""Device credentials for the Android app's background check.

The poll is made by a broadcast receiver in the APK, not by the WebView, so it carries
none of the browser's basic-auth header or SSO cookie. It presents a credential of its
own instead: the already-authenticated web UI asks this server to mint a device secret,
hands it to the shell over the Capacitor bridge, and the shell sends it as a bearer
token from then on.

Only the SHA-256 is stored. The plaintext is returned exactly once, at mint time, and is
unrecoverable afterwards — so a leaked database backup leaks no usable credential, and a
device is revoked individually rather than by changing the one shared UI password.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from service.db.schema import PushDevice

# 32 bytes of CSPRNG, urlsafe-base64: 256 bits, unguessable, and safe in a header.
_TOKEN_BYTES = 32
_KNOWN_PLATFORMS = frozenset({"android"})


def hash_device_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def issue_device_token(session: AsyncSession, platform: str = "android") -> str:
    """Mint and store a device credential, returning the plaintext exactly once."""
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    session.add(PushDevice(
        id=uuid.uuid4().hex,
        token_hash=hash_device_token(token),
        platform=platform if platform in _KNOWN_PLATFORMS else "android",
        created_at=_now(),
    ))
    await session.flush()
    return token


async def revoke_device_token(session: AsyncSession, token: str) -> bool:
    """Forget a device. True when a row was actually removed."""
    device = await _device_for_token(session, token)
    if device is None:
        return False
    await session.delete(device)
    await session.flush()
    return True


async def device_for_auth_header(
    session: AsyncSession, header: str | None
) -> PushDevice | None:
    """The device an ``Authorization: Bearer …`` belongs to, or None."""
    if not header:
        return None
    scheme, _, token = header.strip().partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return await _device_for_token(session, token.strip())


async def _device_for_token(session: AsyncSession, token: str) -> PushDevice | None:
    """Look a credential up by digest, then confirm it in constant time.

    The database comparison is already over a hash rather than the secret, so it leaks
    nothing about the credential itself. The explicit ``compare_digest`` closes the one
    case that leaves — a stored digest that collides would otherwise authenticate — and
    does it without branching on how much of the value matched.
    """
    digest = hash_device_token(token)
    device = (await session.execute(
        select(PushDevice).where(PushDevice.token_hash == digest)
    )).scalar_one_or_none()
    if device is None:
        return None
    return device if secrets.compare_digest(device.token_hash, digest) else None


async def touch_device(session: AsyncSession, device: PushDevice) -> None:
    """Record that this device asked. Purely diagnostic — nothing depends on it."""
    device.last_seen_at = _now()
    await session.flush()
