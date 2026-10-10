from fastapi import APIRouter

from app.api.deps import Service
from app.schemas.ledger import ReconciliationOut

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/reconciliation", response_model=ReconciliationOut)
async def reconciliation(service: Service):
    """Integrity check: all entries must sum to 0, and every cached balance must equal
    the sum of that account's entries. In a real system this runs on a schedule + alerts."""
    return await service.reconcile()
