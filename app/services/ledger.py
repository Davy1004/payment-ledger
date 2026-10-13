"""Ledger business rules.

How a money movement works (every operation goes through _execute):
  1. open ONE database transaction
  2. idempotency check: have we already processed this Idempotency-Key? -> return it
  3. decide the legs, e.g. [(from, -100), (to, +100)]
  4. lock the involved accounts (ordered), check currency + funds
  5. write the transaction, its entries, the new balances and an outbox event
  6. commit - all of it, or none of it

Every service instance belongs to ONE API client (tenant): it can only see and move money in
that client's accounts, and idempotency keys are scoped to it.
"""
import hashlib
import json
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (AccountNotFound, AlreadyReversed, CurrencyMismatch,
                             IdempotencyKeyReuse, InsufficientFunds, NotReversible,
                             TransactionNotFound)
from app.core.observability import LEDGER_TRANSACTIONS
from app.models import (Account, AccountKind, LedgerEntry, OutboxEvent, Transaction,
                        TransactionType)
from app.repositories.ledger import LedgerRepository

MAX_ATTEMPTS = 3


def fingerprint(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()


class LedgerService:
    def __init__(self, session: AsyncSession, client_id: uuid.UUID | None):
        self.session = session
        self.client_id = client_id
        self.repo = LedgerRepository(session)

    # ------------------------------------------------------------ accounts
    async def create_account(self, owner_name: str, currency: str) -> Account:
        account = Account(owner_name=owner_name, currency=currency, kind=AccountKind.USER,
                          client_id=self.client_id)
        async with self.session.begin():
            self.session.add(account)
        return account

    async def get_account(self, account_id: uuid.UUID) -> Account:
        account = await self.repo.get_account(account_id)
        if not self._owns(account):
            raise AccountNotFound("Account not found.", {"account_id": str(account_id)})
        return account

    def _owns(self, account: Account | None) -> bool:
        # another tenant's account looks exactly like a missing one (404, never 403)
        return account is not None and account.kind == AccountKind.USER and account.client_id == self.client_id

    async def statement(self, account_id: uuid.UUID, limit: int, offset: int) -> list[LedgerEntry]:
        await self.get_account(account_id)
        return await self.repo.list_entries(account_id, limit, offset)

    async def get_transaction(self, tx_id: uuid.UUID) -> Transaction:
        tx = await self.repo.get_transaction(tx_id)
        if tx is None or tx.client_id != self.client_id:
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

    async def reverse(self, *, transaction_id, key, description=None):
        """Undo a transaction by posting a NEW one with every leg negated (the ledger is
        append-only: history is never edited). A transaction can be reversed once - the
        UNIQUE(reversal_of_id) column enforces it even under races. It fails with
        insufficient_funds if the money has already been spent."""
        meta = {}

        async def plan():
            original = await self.repo.lock_transaction(transaction_id)
            if original is None or original.client_id != self.client_id:
                raise TransactionNotFound("Transaction not found.", {"transaction_id": str(transaction_id)})
            if original.type == TransactionType.REVERSAL:
                raise NotReversible("A reversal cannot itself be reversed; make a new transaction instead.")
            if await self.repo.get_reversal_of(original.id) is not None:
                raise AlreadyReversed("This transaction was already reversed.",
                                      {"transaction_id": str(original.id)})
            meta["reversal_of_id"] = original.id
            return [(e.account_id, -e.amount) for e in original.entries]

        fp = fingerprint("REVERSAL", transaction_id, description)
        return await self._execute(TransactionType.REVERSAL, key, fp, description, plan, meta)

    # ------------------------------------------------------------ integrity check
    async def reconcile(self) -> dict:
        total = await self.repo.ledger_sum()
        bad = await self.repo.balance_mismatches()
        return {"ok": total == 0 and not bad, "ledger_sum": total, "mismatched_accounts": bad}

    # ------------------------------------------------------------ internals
    async def _user_account(self, account_id) -> Account:
        account = await self.repo.get_account(account_id)
        if not self._owns(account):
            raise AccountNotFound("Account not found.", {"account_id": str(account_id)})
        return account

    async def _execute(self, tx_type, key, fp, description, plan, meta=None):
        """Returns (transaction, created). created=False means an idempotent replay."""
        for attempt in range(MAX_ATTEMPTS):
            try:
                async with self.session.begin():
                    existing = await self.repo.get_transaction_by_key(self.client_id, key)
                    if existing is not None:
                        if existing.request_hash != fp:
                            raise IdempotencyKeyReuse(
                                "This Idempotency-Key was already used with a different request.")
                        LEDGER_TRANSACTIONS.labels(tx_type.value, "replayed").inc()
                        return existing, False
                    legs = await plan()
                    tx = await self._post(tx_type, key, fp, description, legs, meta or {})
                    LEDGER_TRANSACTIONS.labels(tx_type.value, "created").inc()
                    return tx, True
            except IntegrityError:
                # Two identical requests raced and the UNIQUE constraint picked a winner
                # (or two requests created the SYSTEM account at once). Retry: next time the
                # winner's row is visible and we simply replay it.
                if attempt == MAX_ATTEMPTS - 1:
                    raise

    async def _post(self, tx_type, key, fp, description, legs, meta) -> Transaction:
        assert sum(amount for _, amount in legs) == 0, "double-entry invariant violated"

        accounts = await self.repo.lock_accounts(sorted({acc_id for acc_id, _ in legs}, key=str))
        for acc_id, _ in legs:
            account = accounts.get(acc_id)
            if account is None or (account.kind == AccountKind.USER and account.client_id != self.client_id):
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

        tx = Transaction(type=tx_type, client_id=self.client_id, idempotency_key=key, request_hash=fp,
                         description=description, entries=entries,
                         reversal_of_id=meta.get("reversal_of_id"))
        self.session.add(tx)
        await self.session.flush()
        # same DB transaction as the money movement => the event exists iff the movement committed
        self.session.add(OutboxEvent(event_type=f"transaction.{tx_type.value.lower()}", payload={
            "transaction_id": str(tx.id), "type": tx_type.value, "client_id": str(self.client_id),
            "description": description,
            "reversal_of": str(tx.reversal_of_id) if tx.reversal_of_id else None,
            "entries": [{"account_id": str(e.account_id), "amount": e.amount,
                         "balance_after": e.balance_after} for e in entries]}))
        await self.session.flush()
        return tx
