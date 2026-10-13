import hashlib
import hmac
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AdminDisabled, RateLimited, Unauthorized
from app.core.ratelimit import limiter
from app.db.database import get_session
from app.models import ApiClient
from app.services.ledger import LedgerService


def hash_api_key(key: str) -> str:
    """SHA-256 is fine here (unlike for passwords): API keys are long random strings, so there
    is nothing to brute-force, and a fast hash keeps every request cheap."""
    return hashlib.sha256(key.encode()).hexdigest()


async def current_client(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    x_api_key: Annotated[str | None, Header()] = None,
) -> ApiClient:
    if not x_api_key:
        raise Unauthorized("Missing X-API-Key header.", headers={"WWW-Authenticate": "ApiKey"})
    client = (await session.execute(
        select(ApiClient).where(ApiClient.key_hash == hash_api_key(x_api_key)))).scalar_one_or_none()
    if client is None or not client.is_active:
        raise Unauthorized("Invalid API key.", headers={"WWW-Authenticate": "ApiKey"})
    session.expunge(client)   # keep the loaded row usable after we end this read-only transaction
    await session.rollback()  # ...so the service can open its own transaction
    request.state.client_id = str(client.id)
    retry_after = limiter.check(str(client.id), settings.rate_limit_per_minute)
    if retry_after is not None:
        raise RateLimited("Too many requests.", {"retry_after_seconds": round(retry_after, 2)},
                          headers={"Retry-After": str(max(1, int(retry_after + 0.999)))})
    return client


Client = Annotated[ApiClient, Depends(current_client)]


def get_service(client: Client, session: Annotated[AsyncSession, Depends(get_session)]) -> LedgerService:
    return LedgerService(session, client.id)


Service = Annotated[LedgerService, Depends(get_service)]


def require_admin(x_admin_key: Annotated[str | None, Header()] = None,
                  authorization: Annotated[str | None, Header()] = None) -> None:
    """Operator-only endpoints. Accepts `X-Admin-Key: …` or `Authorization: Bearer …` (Prometheus)."""
    expected = settings.admin_api_key
    if not expected:
        raise AdminDisabled("Admin endpoints are disabled: ADMIN_API_KEY is not configured.")
    given = x_admin_key or (authorization[7:] if authorization and authorization.lower().startswith("bearer ") else "")
    if not hmac.compare_digest(given.encode(), expected.encode()):
        raise Unauthorized("Invalid admin key.")


Admin = Depends(require_admin)


def get_admin_service(session: Annotated[AsyncSession, Depends(get_session)]) -> LedgerService:
    return LedgerService(session, None)


AdminService = Annotated[LedgerService, Depends(get_admin_service)]

# Required on every money movement. The client generates one unique value per *intent*
# (e.g. a UUID) and reuses it when retrying. Keys are scoped per API client.
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)]
