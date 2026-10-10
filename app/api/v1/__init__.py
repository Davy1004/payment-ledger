from fastapi import APIRouter

from app.api.v1 import accounts, admin

router = APIRouter()
router.include_router(accounts.router)
router.include_router(admin.router)
