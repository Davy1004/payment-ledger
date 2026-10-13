from fastapi import APIRouter

from app.api.v1 import accounts, admin

router = APIRouter()
router.include_router(accounts.router)  # every route authenticates via X-API-Key (see deps.Service)
router.include_router(admin.router)     # every route needs the operator key
