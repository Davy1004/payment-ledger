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

from sqlalchemy import (BigInteger, CheckConstraint, DateTime, Enum, ForeignKey, Index,
                        Integer, String, Uuid, func, text)
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


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_name: Mapped[str] = mapped_column(String(120))
    currency: Mapped[str] = mapped_column(String(3))
    kind: Mapped[AccountKind] = mapped_column(Enum(AccountKind, native_enum=False, length=10), default=AccountKind.USER)
    # Cached running balance (fast reads). The entries are the source of truth;
    # /admin/reconciliation proves the two agree.
    balance: Mapped[int] = mapped_column(BigInteger, default=0)
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
    # UNIQUE: the database guarantees one transaction per Idempotency-Key.
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))  # fingerprint of the request body
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    entries: Mapped[list["LedgerEntry"]] = relationship(
        back_populates="transaction", lazy="selectin", order_by="LedgerEntry.id")


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
