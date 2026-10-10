"""Ledger business rules.

How a money movement works (every operation goes through _execute):
  1. open ONE database transaction
  2. idempotency check: have we already processed this Idempotency-Key? -> return it
  3. decide the legs, e.g. [(from, -100), (to, +100)]
  4. lock the involved accounts (ordered), check currency + funds
  5. write the transaction, its entries, and the new balances
  6. commit - all of it, or none of it
"""
import hashlib
import json
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (AccountNotFound, CurrencyMismatch, IdempotencyKeyReuse,
                             InsufficientFunds, TransactionNotFound)
from app.models import Account, AccountKind, LedgerEntry, Transaction, TransactionType
from app.repositories.ledger import LedgerRepository

MAX_ATTEMPTS = 3


def fingerprint(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()


class LedgerService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = LedgerRepository(session)

    # ------------------------------------------------------------ accounts
    async def create_account(self, owner_name: str, currency: str) -> Account:
        account = Account(owner_name=owner_name, currency=currency, kind=AccountKind.USER)
        async with self.session.begin():
            self.session.add(account)
        return account

    async def get_account(self, account_id: uuid.UUID) -> Account:
        account = await self.repo.get_account(account_id)
        if account is None or account.kind != AccountKind.USER:
            raise AccountNotFound("Account not found.", {"account_id": str(account_id)})
        return account

    async def statement(self, account_id: uuid.UUID, limit: int, offset: int) -> list[LedgerEntry]:
        await self.get_account(account_id)
        return await self.repo.list_entries(account_id, limit, offset)

    async def get_transaction(self, tx_id: uuid.UUID) -> Transaction:
        tx = await self.repo.get_transaction(tx_id)
        if tx is None:
            raise TransactionNotFound("Transaction not found.", {"transaction_id": str(tx_id)})
        return tx

    # ------------------------------------------------------------ money movements
    async def transfer(self, *, from_id, to_id, amount, key, description=None):
        async def plan():
            return [(from_id, -amount), (to_id, amount)]
        fp = fingerprint("TRANSFER", from_id, to_id, amount, description)
        return await self._execute(TransactionType.TRANSFER, key, fp, description, plan)

    async def deposit(self, *, account_id, amount, key, description=None):
        async def plan():
            account = await self._user_account(account_id)
            system = await self.repo.get_or_create_system_account(account.currency)
            return [(system.id, -amount), (account_id, amount)]
        fp = fingerprint("DEPOSIT", account_id, amount, description)
        return await self._execute(TransactionType.DEPOSIT, key, fp, description, plan)

    async def withdraw(self, *, account_id, amount, key, description=None):
        async def plan():
            account = await self._user_account(account_id)
            system = await self.repo.get_or_create_system_account(account.currency)
            return [(account_id, -amount), (system.id, amount)]
        fp = fingerprint("WITHDRAWAL", account_id, amount, description)
        return await self._execute(TransactionType.WITHDRAWAL, key, fp, description, plan)

    # ------------------------------------------------------------ integrity check
    async def reconcile(self) -> dict:
        total = await self.repo.ledger_sum()
        bad = await self.repo.balance_mismatches()
        return {"ok": total == 0 and not bad, "ledger_sum": total, "mismatched_accounts": bad}

    # ------------------------------------------------------------ internals
    async def _user_account(self, account_id) -> Account:
        account = await self.repo.get_account(account_id)
        if account is None or account.kind != AccountKind.USER:
            raise AccountNotFound("Account not found.", {"account_id": str(account_id)})
        return account

    async def _execute(self, tx_type, key, fp, description, plan):
        """Returns (transaction, created). created=False means an idempotent replay."""
        for attempt in range(MAX_ATTEMPTS):
            try:
                async with self.session.begin():
                    existing = await self.repo.get_transaction_by_key(key)
                    if existing is not None:
                        if existing.request_hash != fp:
                            raise IdempotencyKeyReuse(
                                "This Idempotency-Key was already used with a different request.")
                        return existing, False
                    legs = await plan()
                    return await self._post(tx_type, key, fp, description, legs), True
            except IntegrityError:
                # Two identical requests raced and the UNIQUE constraint picked a winner
                # (or two requests created the SYSTEM account at once). Retry: next time the
                # winner's row is visible and we simply replay it.
                if attempt == MAX_ATTEMPTS - 1:
                    raise

    async def _post(self, tx_type, key, fp, description, legs) -> Transaction:
        assert sum(amount for _, amount in legs) == 0, "double-entry invariant violated"

        accounts = await self.repo.lock_accounts(sorted({acc_id for acc_id, _ in legs}, key=str))
        for acc_id, _ in legs:
            if acc_id not in accounts:
                raise AccountNotFound("Account not found.", {"account_id": str(acc_id)})
        if len({a.currency for a in accounts.values()}) > 1:
            raise CurrencyMismatch("Accounts use different currencies.",
                                   {"currencies": sorted({a.currency for a in accounts.values()})})
        # public transfers may only involve USER accounts; SYSTEM legs come from deposit/withdraw
        if tx_type == TransactionType.TRANSFER and any(a.kind != AccountKind.USER for a in accounts.values()):
            raise AccountNotFound("Account not found.")

        entries = []
        for acc_id, amount in legs:
            account = accounts[acc_id]
            new_balance = account.balance + amount
            if account.kind == AccountKind.USER and new_balance < 0:
                raise InsufficientFunds("Insufficient funds.", {
                    "account_id": str(acc_id), "balance": account.balance, "required": -amount})
            account.balance = new_balance
            entries.append(LedgerEntry(account_id=acc_id, amount=amount, balance_after=new_balance))

        tx = Transaction(type=tx_type, idempotency_key=key, request_hash=fp,
                         description=description, entries=entries)
        self.session.add(tx)
        await self.session.flush()
        return tx
