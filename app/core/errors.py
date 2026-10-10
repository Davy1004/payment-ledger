"""Business-rule errors. Each one knows its HTTP status, so the API layer stays tiny."""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class LedgerError(Exception):
    status_code = 400
    code = "ledger_error"

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


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


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(LedgerError)
    async def _handle(_: Request, exc: LedgerError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
        )
