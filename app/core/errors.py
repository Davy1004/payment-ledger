"""Business-rule errors. Each one knows its HTTP status, so the API layer stays tiny."""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.observability import LEDGER_REJECTIONS


class LedgerError(Exception):
    status_code = 400
    code = "ledger_error"

    def __init__(self, message: str, details: dict | None = None, headers: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.headers = headers or {}


class AccountNotFound(LedgerError):
    status_code, code = 404, "account_not_found"


class TransactionNotFound(LedgerError):
    status_code, code = 404, "transaction_not_found"


class InsufficientFunds(LedgerError):
    status_code, code = 422, "insufficient_funds"


class CurrencyMismatch(LedgerError):
    status_code, code = 422, "currency_mismatch"


class IdempotencyKeyReuse(LedgerError):
    """Same Idempotency-Key, but a different request body: almost certainly a client bug."""
    status_code, code = 409, "idempotency_key_reuse"


class AlreadyReversed(LedgerError):
    status_code, code = 409, "already_reversed"


class NotReversible(LedgerError):
    status_code, code = 422, "not_reversible"


class Unauthorized(LedgerError):
    status_code, code = 401, "unauthorized"


class AdminDisabled(LedgerError):
    status_code, code = 503, "admin_disabled"


class RateLimited(LedgerError):
    status_code, code = 429, "rate_limited"


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(LedgerError)
    async def _handle(_: Request, exc: LedgerError):
        LEDGER_REJECTIONS.labels(exc.code).inc()
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
            headers=exc.headers,
        )
