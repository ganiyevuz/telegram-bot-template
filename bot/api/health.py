from __future__ import annotations

from fastapi import APIRouter, Request, Response, status
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict[str, str]:
    """Liveness: the process is running. Never touches a dependency — a failing
    database must not get the container killed and restarted in a loop."""
    return {"status": "alive"}


@router.get("/ready")
async def ready(request: Request, response: Response) -> dict[str, object]:
    """Readiness: this replica can serve traffic. The load balancer uses this."""
    container = request.app.state.ctx.container
    checks: dict[str, bool] = {}

    try:
        sessionmaker = await container.get(async_sessionmaker[AsyncSession])
        async with sessionmaker() as session:
            await session.execute(text("SELECT 1"))
        checks["postgres"] = True
    except Exception:  # noqa: BLE001 - readiness reports, never raises
        checks["postgres"] = False

    try:
        checks["redis"] = bool(await (await container.get(Redis)).ping())
    except Exception:  # noqa: BLE001
        checks["redis"] = False

    ok = all(checks.values())
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if ok else "degraded", "checks": checks}
