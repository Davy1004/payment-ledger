import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_AMOUNT = 10**12  # sanity cap in minor units


class AccountCreate(BaseModel):
    owner_name: str = Field(min_length=1, max_length=120)
    currency: str = Field(default="INR", min_length=3, max_length=3)

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str) -> str:
        if not v.isalpha():
            raise ValueError("currency must be 3 letters, e.g. INR")
        return v.upper()


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    owner_name: str
    currency: str
    balance: int = Field(description="minor units (paise)")
    created_at: datetime


class MoneyRequest(BaseModel):
    amount: int = Field(gt=0, le=MAX_AMOUNT, description="minor units, e.g. 10050 = INR 100.50")
    description: str | None = Field(default=None, max_length=255)


class TransferCreate(MoneyRequest):
    from_account_id: uuid.UUID
    to_account_id: uuid.UUID

    @model_validator(mode="after")
    def _different_accounts(self):
        if self.from_account_id == self.to_account_id:
            raise ValueError("cannot transfer to the same account")
        return self


class EntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    transaction_id: uuid.UUID
    account_id: uuid.UUID
    amount: int
    balance_after: int
    created_at: datetime


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    type: str
    description: str | None
    created_at: datetime
    entries: list[EntryOut]


class ReconciliationOut(BaseModel):
    ok: bool
    ledger_sum: int = Field(description="sum of ALL entries; must be 0")
    mismatched_accounts: list[dict]
