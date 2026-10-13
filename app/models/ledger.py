"""Database tables.

Money is ALWAYS an integer number of minor units (paise / cents): floats cannot
represent 0.1 exactly, which is unacceptable for money.

Double-entry rule: every transaction has entries whose amounts add up to exactly 0
(money never appears or disappears, it only moves). A deposit moves money from the
per-currency SYSTEM account ("the outside world") into a user account.
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import (JSON, BigInteger, Boolean, CheckConstraint, DateTime, Enum, ForeignKey, Index,
                        Integer, String, Text, UniqueConstraint, Uuid, func, text)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# BIGINT in Postgres, INTEGER in SQLite (SQLite only auto-increments plain INTEGER keys)
BigIntPK = BigInteger().with_variant(Integer, "sqlite")


class AccountKind(str, enum.Enum):
    USER = "USER"
    SYSTEM = "SYSTEM"


class TransactionType(str, enum.Enum):
    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    TRANSFER = "TRANSFER"
    REVERSAL = "REVERSAL"  # compensating transaction that undoes an earlier one


class ApiClient(Base):
    """A tenant. Authenticates with an API key; we store only the key's SHA-256 hash."""
    __tablename__ = "api_clients"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_name: Mapped[str] = mapped_column(String(120))
    currency: Mapped[str] = mapped_column(String(3))
    kind: Mapped[AccountKind] = mapped_column(Enum(AccountKind, native_enum=False, length=10), default=AccountKind.USER)
    # Cached running balance (fast reads). The entries are the source of truth;
    # /admin/reconciliation proves the two agree.
    balance: Mapped[int] = mapped_column(BigInteger, default=0)
    # owner tenant; NULL only for the shared SYSTEM accounts
    client_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("api_clients.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        # the database itself refuses an overdraft on user accounts
        CheckConstraint("kind = 'SYSTEM' OR balance >= 0", name="ck_accounts_no_overdraft"),
        # at most one SYSTEM account per currency
        Index("uq_accounts_one_system_per_currency", "currency", unique=True,
              postgresql_where=text("kind = 'SYSTEM'"), sqlite_where=text("kind = 'SYSTEM'")),
    )


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    type: Mapped[TransactionType] = mapped_column(Enum(TransactionType, native_enum=False, length=12))
    client_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("api_clients.id"), index=True)
    # UNIQUE per client: the database guarantees one transaction per (client, Idempotency-Key).
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))  # fingerprint of the request body
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # set on REVERSAL transactions. UNIQUE: a transaction can be reversed at most once.
    reversal_of_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transactions.id"), nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    entries: Mapped[list["LedgerEntry"]] = relationship(
        back_populates="transaction", lazy="selectin", order_by="LedgerEntry.id")

    __table_args__ = (UniqueConstraint("client_id", "idempotency_key", name="uq_transactions_client_key"),)


class LedgerEntry(Base):
    """One immutable line: +amount = money in, -amount = money out."""
    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    transaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transactions.id"), index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"))
    amount: Mapped[int] = mapped_column(BigInteger)
    balance_after: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    transaction: Mapped[Transaction] = relationship(back_populates="entries")

    __table_args__ = (
        CheckConstraint("amount <> 0", name="ck_entries_nonzero"),
        Index("ix_entries_account_id_id", "account_id", "id"),  # account statements
    )


class OutboxStatus(str, enum.Enum):
    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    DEAD = "DEAD"  # gave up after too many attempts; needs a human


class OutboxEvent(Base):
    """Transactional outbox: written in the SAME database transaction as the money movement,
    so an event exists if and only if the movement committed. A worker delivers it afterwards
    (at-least-once), which avoids the classic "DB committed but the message was lost" bug."""
    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[OutboxStatus] = mapped_column(
        Enum(OutboxStatus, native_enum=False, length=10), default=OutboxStatus.PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_outbox_status_next", "status", "next_attempt_at"),)
