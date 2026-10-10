"""All SQL lives here. Nothing else in the app writes queries."""
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, AccountKind, LedgerEntry, Transaction


class LedgerRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    # ---- accounts ----
    async def get_account(self, account_id: uuid.UUID) -> Account | None:
        return await self.session.get(Account, account_id)

    async def lock_accounts(self, ids: list[uuid.UUID]) -> dict[uuid.UUID, Account]:
        """SELECT ... FOR UPDATE, always in id order.

        FOR UPDATE makes other transactions wait for these rows (no lost updates).
        Fixed ordering means A->B and B->A transfers lock in the same order,
        so they can never deadlock.
        """
        stmt = select(Account).where(Account.id.in_(ids)).order_by(Account.id).with_for_update()
        rows = (await self.session.execute(stmt)).scalars().all()
        return {a.id: a for a in rows}

    async def get_or_create_system_account(self, currency: str) -> Account:
        stmt = select(Account).where(Account.kind == AccountKind.SYSTEM, Account.currency == currency)
        account = (await self.session.execute(stmt)).scalar_one_or_none()
        if account is None:
            account = Account(owner_name=f"SYSTEM ({currency})", currency=currency, kind=AccountKind.SYSTEM)
            self.session.add(account)
            await self.session.flush()  # unique index makes a concurrent duplicate fail -> caller retries
        return account

    # ---- transactions ----
    async def get_transaction(self, tx_id: uuid.UUID) -> Transaction | None:
        return await self.session.get(Transaction, tx_id)

    async def get_transaction_by_key(self, key: str) -> Transaction | None:
        stmt = select(Transaction).where(Transaction.idempotency_key == key)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    # ---- statements & integrity ----
    async def list_entries(self, account_id: uuid.UUID, limit: int, offset: int) -> list[LedgerEntry]:
        stmt = (select(LedgerEntry).where(LedgerEntry.account_id == account_id)
                .order_by(LedgerEntry.id.desc()).limit(limit).offset(offset))
        return list((await self.session.execute(stmt)).scalars())

    async def ledger_sum(self) -> int:
        return int((await self.session.execute(select(func.coalesce(func.sum(LedgerEntry.amount), 0)))).scalar_one())

    async def balance_mismatches(self) -> list[dict]:
        entry_sum = func.coalesce(func.sum(LedgerEntry.amount), 0)
        stmt = (select(Account.id, Account.balance, entry_sum)
                .outerjoin(LedgerEntry, LedgerEntry.account_id == Account.id)
                .group_by(Account.id, Account.balance)
                .having(Account.balance != entry_sum))
        return [{"account_id": str(i), "cached_balance": int(b), "entries_sum": int(s)}
                for i, b, s in (await self.session.execute(stmt)).all()]
