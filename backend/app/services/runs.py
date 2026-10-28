"""Journal of operations (IMPL R1): one `runs` row per seed, daily_update, presence_probe or retry run."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import Ecosystem
from app.models.package_event import PackageEvent
from app.models.run import Run

RUNNING, SUCCESS, PARTIAL, FAILED = "running", "success", "partial", "failed"
# A run still "running" after this long died without finishing; it no longer blocks the next one.
OVERLAP_GUARD = timedelta(hours=6)


async def running_run(db: AsyncSession, operation: str, ecosystem: str | None) -> Run | None:
    condition = Run.ecosystem.is_(None) if ecosystem is None else Run.ecosystem == Ecosystem(ecosystem)
    return await db.scalar(
        select(Run).where(Run.operation == operation, condition, Run.status == RUNNING,
                          Run.started_at > datetime.now(UTC) - OVERLAP_GUARD)
        .order_by(Run.started_at.desc()).limit(1)
    )


async def start_run(db: AsyncSession, operation: str, ecosystem: str | None, *, exclusive: bool = True) -> Run | None:
    """A new running row, flushed. With `exclusive`, None while a run of the same operation and ecosystem is going;
    the caller commits right away, which releases the lock taken here."""
    if exclusive:
        # Serializes the check and the insert: beat and a manual start at the same moment get one run, not two.
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"run:{operation}:{ecosystem}"})
        if await running_run(db, operation, ecosystem) is not None:
            return None
    run = Run(operation=operation, ecosystem=Ecosystem(ecosystem) if ecosystem else None, status=RUNNING,
              started_at=datetime.now(UTC))
    db.add(run)
    await db.flush()
    return run


async def finish_run(db: AsyncSession, run: Run, status: str, *, error: str | None = None, **counts: object) -> dict[str, int]:
    """Close the run with its counters (`packages_*`, `per_registry`) and the events it produced; returns them by kind."""
    events = {
        kind.value: count
        for kind, count in (
            await db.execute(
                select(PackageEvent.kind, func.count(PackageEvent.id)).where(PackageEvent.run_id == run.id)
                .group_by(PackageEvent.kind)
            )
        ).all()
    }
    for name, value in counts.items():
        if not hasattr(Run, name):
            raise AttributeError(f"runs has no column {name}")
        setattr(run, name, value)
    run.status = status
    run.error = error[:2000] if error else None
    run.finished_at = datetime.now(UTC)
    run.events_by_kind = events
    run.events_created = sum(events.values())
    await db.flush()
    return events
