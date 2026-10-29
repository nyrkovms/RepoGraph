import traceback as traceback_lib
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.ecosystems.base import EcosystemPlugin, PackageRef
from app.ecosystems.registry import get_plugin, load_builtin_plugins
from app.models.enums import Ecosystem
from app.models.sync_failure import SyncFailure
from app.services.retry_policy import compute_backoff_delay_seconds, is_retryable_upstream_error


# Probe failures are not retried by retry_failed, and a successful sync of the package does not clear them:
# re-fetching the package would not repeat the probe. The next weekly probe is the retry.
NOT_RETRIED = ("presence_probe",)


def _format_error_message(exc: Exception) -> str:
    message = str(exc).strip()
    return message if message else exc.__class__.__name__


def _format_traceback(exc: Exception) -> str:
    rendered = "".join(traceback_lib.format_exception(type(exc), exc, exc.__traceback__))
    return rendered[:16000]


def _plugin(ecosystem: str) -> EcosystemPlugin:
    load_builtin_plugins()
    return get_plugin(ecosystem)


def _purl(ref: PackageRef) -> str:
    """The key of a failure: the purl without version (with operation, ecosystem and registry)."""
    return _plugin(ref.ecosystem).coordinates.to_purl(ref)


async def upsert_sync_failure(
    db: AsyncSession,
    *,
    operation: str,
    exc: Exception,
    ref: PackageRef,
    retryable: bool | None = None,
) -> SyncFailure:
    """Record or count up a failure of `ref`."""
    settings = get_settings()
    purl = _purl(ref)
    is_retryable = is_retryable_upstream_error(exc) if retryable is None else retryable
    now = datetime.now(UTC)

    row = await db.scalar(
        select(SyncFailure).where(SyncFailure.operation == operation, SyncFailure.package_ref == purl,
                                  SyncFailure.registry_id.is_(None)).limit(1)
    )
    if row is None:
        row = SyncFailure(
            operation=operation,
            ecosystem=Ecosystem(ref.ecosystem),
            package_ref=purl,
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


async def clear_sync_failure(db: AsyncSession, *, ref: PackageRef, operation: str | None = None) -> None:
    query = delete(SyncFailure).where(SyncFailure.package_ref == _purl(ref))
    if operation is not None:
        query = query.where(SyncFailure.operation == operation)
    else:
        query = query.where(SyncFailure.operation.not_in(NOT_RETRIED))
    await db.execute(query)


async def list_due_sync_failures(db: AsyncSession, limit: int) -> list[SyncFailure]:
    now = datetime.now(UTC)
    rows = (
        await db.scalars(
            select(SyncFailure)
            .where(
                SyncFailure.retryable.is_(True),
                SyncFailure.operation.not_in(NOT_RETRIED),
                or_(SyncFailure.next_retry_at.is_(None), SyncFailure.next_retry_at <= now),
            )
            .order_by(SyncFailure.next_retry_at.asc().nullsfirst(), SyncFailure.last_seen_at.asc())
            .limit(limit)
        )
    ).all()
    return rows
