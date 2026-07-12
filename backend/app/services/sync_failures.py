import traceback as traceback_lib
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.sync_failure import SyncFailure
from app.normalizers.names import normalize_package_name
from app.services.retry_policy import compute_backoff_delay_seconds, is_retryable_upstream_error


def _format_error_message(exc: Exception) -> str:
    message = str(exc).strip()
    return message if message else exc.__class__.__name__


def _format_traceback(exc: Exception) -> str:
    rendered = "".join(traceback_lib.format_exception(type(exc), exc, exc.__traceback__))
    return rendered[:16000]


async def upsert_sync_failure(
    db: AsyncSession,
    *,
    operation: str,
    package_name: str,
    exc: Exception,
    retryable: bool | None = None,
    ecosystem: str = "pypi",
) -> SyncFailure:
    settings = get_settings()
    normalized_name = normalize_package_name(package_name)
    is_retryable = is_retryable_upstream_error(exc) if retryable is None else retryable
    now = datetime.now(UTC)

    row = await db.scalar(
        select(SyncFailure).where(
            SyncFailure.operation == operation,
            SyncFailure.package_name == normalized_name,
        )
    )
    if row is None:
        row = SyncFailure(
            ecosystem=ecosystem,
            operation=operation,
            package_name=normalized_name,
            error_class=exc.__class__.__name__,
            error_message=_format_error_message(exc),
            traceback=_format_traceback(exc),
            retryable=is_retryable,
            attempts=1,
            next_retry_at=None,
            last_seen_at=now,
        )
        db.add(row)
    else:
        row.ecosystem = ecosystem
        row.error_class = exc.__class__.__name__
        row.error_message = _format_error_message(exc)
        row.traceback = _format_traceback(exc)
        row.retryable = is_retryable
        row.attempts += 1
        row.last_seen_at = now

    if row.retryable and row.attempts <= settings.sync_failed_retry_max_attempts:
        delay_seconds = compute_backoff_delay_seconds(
            attempt=row.attempts,
            base_seconds=settings.sync_failed_retry_backoff_seconds,
            max_seconds=settings.sync_failed_retry_max_backoff_seconds,
            jitter_seconds=settings.sync_failed_retry_jitter_seconds,
        )
        row.next_retry_at = now + timedelta(seconds=delay_seconds)
    else:
        row.retryable = False
        row.next_retry_at = None

    await db.flush()
    return row


async def clear_sync_failure(db: AsyncSession, *, package_name: str, operation: str | None = None) -> None:
    normalized_name = normalize_package_name(package_name)
    query = delete(SyncFailure).where(SyncFailure.package_name == normalized_name)
    if operation is not None:
        query = query.where(SyncFailure.operation == operation)
    await db.execute(query)


async def list_due_sync_failures(db: AsyncSession, limit: int) -> list[SyncFailure]:
    now = datetime.now(UTC)
    rows = (
        await db.scalars(
            select(SyncFailure)
            .where(
                SyncFailure.retryable.is_(True),
                or_(SyncFailure.next_retry_at.is_(None), SyncFailure.next_retry_at <= now),
            )
            .order_by(SyncFailure.next_retry_at.asc().nullsfirst(), SyncFailure.last_seen_at.asc())
            .limit(limit)
        )
    ).all()
    return rows
