from fastapi import Depends, FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.api.deps import require_admin
from app.api.v1 import router as v1_router
from app.core.config import settings
from app.core.errors import register_error_handlers
from app.core.observability import observe_requests, configure_logging

configure_logging(settings.log_level)

app = FastAPI(
    title="Payment Transaction Ledger",
    version="2.0.0",
    description="Double-entry ledger with idempotent money movements, reversals, API-key auth, "
                "rate limiting, a transactional outbox and Prometheus metrics.",
)
app.middleware("http")(observe_requests)
register_error_handlers(app)
app.include_router(v1_router, prefix="/api/v1")


@app.get("/health", tags=["ops"])
async def health_check():
    return {"status": "ok"}


@app.get("/metrics", tags=["ops"], dependencies=[Depends(require_admin)])
async def metrics():
    """Prometheus text format. Scrape with `Authorization: Bearer <ADMIN_API_KEY>`."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
