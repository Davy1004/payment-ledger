"""Background worker: `python -m app.worker` (loops forever) or `python -m app.worker --once`."""
import asyncio
import sys

from app.core.config import settings
from app.core.observability import configure_logging, event
from app.db.database import AsyncSessionLocal
from app.services.outbox import dispatch_batch


async def main(once: bool) -> None:
    configure_logging(settings.log_level)
    if not settings.webhook_url:
        event("worker_idle", reason="WEBHOOK_URL is not set; events are recorded but not delivered")
    while True:
        async with AsyncSessionLocal() as session:
            result = await dispatch_batch(session)
        if result["attempted"]:
            event("outbox_batch", **result)
        if once:
            return
        await asyncio.sleep(0.2 if result["attempted"] else 2.0)


if __name__ == "__main__":
    asyncio.run(main("--once" in sys.argv))
