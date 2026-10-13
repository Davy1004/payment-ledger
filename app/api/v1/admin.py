import secrets
import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy import select

from app.api.deps import Admin, AdminService, hash_api_key
from app.models import ApiClient
from app.schemas.ledger import (ClientCreate, ClientOut, DispatchResult, OutboxSummary,
                                ReconciliationOut)
from app.services import outbox

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Admin])


@router.get("/reconciliation", response_model=ReconciliationOut)
async def reconciliation(service: AdminService):
    """Integrity check: all entries must sum to 0, and every cached balance must equal
    the sum of that account's entries. In a real system this runs on a schedule + alerts."""
    return await service.reconcile()


@router.post("/clients", response_model=ClientOut, status_code=status.HTTP_201_CREATED)
async def create_client(body: ClientCreate, service: AdminService):
    """Onboard a tenant. The API key is returned once and never stored in plain text."""
    api_key = "pl_" + secrets.token_urlsafe(32)
    client = ApiClient(id=uuid.uuid4(), name=body.name, key_hash=hash_api_key(api_key))
    async with service.session.begin():
        service.session.add(client)
    return ClientOut(id=client.id, name=client.name, api_key=api_key)


@router.post("/clients/{client_id}/deactivate", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_client(client_id: uuid.UUID, service: AdminService):
    async with service.session.begin():
        client = (await service.session.execute(
            select(ApiClient).where(ApiClient.id == client_id).with_for_update())).scalar_one_or_none()
        if client is not None:
            client.is_active = False


@router.get("/outbox", response_model=OutboxSummary)
async def outbox_summary(service: AdminService):
    return await outbox.summary(service.session)


@router.post("/outbox/dispatch", response_model=DispatchResult)
async def outbox_dispatch(service: AdminService):
    """Deliver one batch of pending events now (see app/services/outbox.py)."""
    return await outbox.dispatch_batch(service.session)
