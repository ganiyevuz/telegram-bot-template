from __future__ import annotations
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request, Response, status
from loguru import logger
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.notifier import AlertLevel, NotifierService

if TYPE_CHECKING:
    from dishka import AsyncContainer

router = APIRouter(prefix="/health", tags=["health"])

# The readiness verdict this process last reported. A load balancer polls /health/ready
# every few seconds, so the alert has to fire on the *transition*, not on the probe —
# otherwise a ten-minute outage is a hundred alerts, and even the fingerprint cooldown
# only thins that to one per cooldown window for as long as it lasts.
#
# Per-process by design, and module-level state is exactly the right scope for it: each
# replica reports its own transitions, and the shared Redis cooldown collapses the
# duplicates when several flip at once.
#
# It starts True — "assumed serving" — rather than None, so a replica that comes up
# already broken alerts on its first probe instead of quietly adopting "degraded" as its
# baseline and never mentioning it.
_last_ready = True


async def _report_readiness(container: AsyncContainer, *, ok: bool, checks: dict[str, bool]) -> None:
    """Alert when this replica's readiness verdict changes, and only then."""
    global _last_ready  # noqa: PLW0603 - one verdict per process; see the comment above
    if ok == _last_ready:
        return
    # Recorded before the alert is attempted, not after: if alerting fails, the next
    # probe must not read the old verdict and try again on every poll.
    _last_ready = ok

    try:
        notifier = await container.get(NotifierService)
        await notifier.send(
            AlertLevel.INFO if ok else AlertLevel.ERROR,
            "readiness recovered" if ok else "readiness lost",
            "\n".join(f"{name}: {'ok' if healthy else 'FAIL'}" for name, healthy in sorted(checks.items())),
            "health:ready",
        )
    except Exception as exc:  # noqa: BLE001 - readiness reports, never raises
        logger.warning(f"failed to alert on readiness change | {type(exc).__name__}: {exc}")


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
    await _report_readiness(container, ok=ok, checks=checks)
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if ok else "degraded", "checks": checks}
