import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.api.deps import IdempotencyKey, Service
from app.schemas.ledger import (AccountCreate, AccountOut, EntryOut, MoneyRequest,
                                TransactionOut, TransferCreate)

router = APIRouter(tags=["accounts & money"])


@router.post("/accounts", response_model=AccountOut, status_code=status.HTTP_201_CREATED)
async def create_account(body: AccountCreate, service: Service):
    return await service.create_account(body.owner_name, body.currency)


@router.get("/accounts/{account_id}", response_model=AccountOut)
async def get_account(account_id: uuid.UUID, service: Service):
    return await service.get_account(account_id)


@router.get("/accounts/{account_id}/entries", response_model=list[EntryOut])
async def statement(account_id: uuid.UUID, service: Service,
                    limit: Annotated[int, Query(ge=1, le=200)] = 50,
                    offset: Annotated[int, Query(ge=0)] = 0):
    """Newest first."""
    return await service.statement(account_id, limit, offset)


def _respond(result, response: Response):
    tx, created = result
    # 201 = newly created, 200 = this Idempotency-Key was already processed (replay)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return tx


@router.post("/accounts/{account_id}/deposits", response_model=TransactionOut, status_code=201)
async def deposit(account_id: uuid.UUID, body: MoneyRequest, key: IdempotencyKey,
                  service: Service, response: Response):
    return _respond(await service.deposit(account_id=account_id, amount=body.amount, key=key,
                                          description=body.description), response)


@router.post("/accounts/{account_id}/withdrawals", response_model=TransactionOut, status_code=201)
async def withdraw(account_id: uuid.UUID, body: MoneyRequest, key: IdempotencyKey,
                   service: Service, response: Response):
    return _respond(await service.withdraw(account_id=account_id, amount=body.amount, key=key,
                                           description=body.description), response)


@router.post("/transfers", response_model=TransactionOut, status_code=201)
async def transfer(body: TransferCreate, key: IdempotencyKey, service: Service, response: Response):
    return _respond(await service.transfer(from_id=body.from_account_id, to_id=body.to_account_id,
                                           amount=body.amount, key=key,
                                           description=body.description), response)


@router.get("/transactions/{transaction_id}", response_model=TransactionOut)
async def get_transaction(transaction_id: uuid.UUID, service: Service):
    return await service.get_transaction(transaction_id)
