from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_session
from app.services.ledger import LedgerService


def get_service(session: Annotated[AsyncSession, Depends(get_session)]) -> LedgerService:
    return LedgerService(session)


Service = Annotated[LedgerService, Depends(get_service)]
# Required on every money movement. The client generates one unique value per *intent*
# (e.g. a UUID) and reuses it when retrying.
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)]
