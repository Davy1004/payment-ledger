from fastapi import FastAPI

from app.api.v1 import router as v1_router
from app.core.errors import register_error_handlers

app = FastAPI(
    title="Payment Transaction Ledger",
    version="1.0.0",
    description="Double-entry ledger with idempotent money movements.",
)
register_error_handlers(app)
app.include_router(v1_router, prefix="/api/v1")


@app.get("/health", tags=["ops"])
async def health_check():
    return {"status": "ok"}
