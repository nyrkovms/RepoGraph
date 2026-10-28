import hashlib
import traceback as traceback_lib
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.ecosystems.base import EcosystemPlugin, PackageRef
from app.ecosystems.registry import get_plugin, load_builtin_plugins
from app.models.enums import Ecosystem
from app.models.sync_failure import SyncFailure
from app.normalizers.names import normalize_package_name
from app.services.retry_policy import compute_backoff_delay_seconds, is_retryable_upstream_error


_NAME_LENGTH = 255  # sync_failures.package_name

# Probe failures are not retried by retry_failed, and a successful sync of the package does not clear them:
# re-fetching the package would not repeat the probe. The next weekly probe is the retry.
NOT_RETRIED = ("presence_probe",)


def _format_error_message(exc: Exception) -> str:
    message = str(exc).strip()
    return message if message else exc.__class__.__name__


def _format_traceback(exc: Exception) -> str:
    rendered = "".join(traceback_lib.format_exception(type(exc), exc, exc.__traceback__))
    return rendered[:16000]


def _legacy_ref(package_name: str) -> PackageRef:
    return PackageRef(ecosystem="pypi", namespace="", name=normalize_package_name(package_name))


def _resolve_ref(ref: PackageRef | None, package_name: str | None) -> PackageRef:
    if ref is not None:
        return ref
    if package_name is None:
        raise TypeError("a sync failure needs `ref` (or the legacy `package_name`)")
    return _legacy_ref(package_name)


def _plugin(ecosystem: str) -> EcosystemPlugin:
    load_builtin_plugins()
    return get_plugin(ecosystem)


def _key(ref: PackageRef) -> tuple[str, str]:
    """(package_ref, legacy package_name) of a package. `package_ref` is the purl without version and the key of
    the table. The legacy column still takes part in the unique key (operation, package_name) until 0007, so it
    must tell packages of different ecosystems apart: bare names for PyPI (as before), the purl for the rest."""
    plugin = _plugin(ref.ecosystem)
    purl = plugin.coordinates.to_purl(ref)
    if ref.ecosystem == "pypi":
        return purl, ref.name[:_NAME_LENGTH]
    if len(purl) <= _NAME_LENGTH:
        return purl, purl
    return purl, f"{purl[:_NAME_LENGTH - 17]}#{hashlib.sha1(purl.encode()).hexdigest()[:16]}"


def _matches(ref: PackageRef):
    """Rows of the package: by purl, or written before `package_ref` existed (only `package_name`, PyPI by T9)."""
    purl, name = _key(ref)
    condition = SyncFailure.package_ref == purl
    if ref.ecosystem == "pypi":
        condition = or_(condition, and_(SyncFailure.package_ref.is_(None), SyncFailure.package_name == name))
    return condition


async def upsert_sync_failure(
    db: AsyncSession,
    *,
    operation: str,
    exc: Exception,
    ref: PackageRef | None = None,
    package_name: str | None = None,
    retryable: bool | None = None,
) -> SyncFailure:
    """Record or count up a failure of `ref`. `package_name` (a bare PyPI name) is the legacy way to say
    `pkg:pypi/<name>`."""
    settings = get_settings()
    ref = _resolve_ref(ref, package_name)
    purl, name = _key(ref)
    is_retryable = is_retryable_upstream_error(exc) if retryable is None else retryable
    now = datetime.now(UTC)

    row = await db.scalar(select(SyncFailure).where(SyncFailure.operation == operation, _matches(ref)).limit(1))
    if row is None:
        row = SyncFailure(
            operation=operation,
            package_name=name,
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
        row.ecosystem = Ecosystem(ref.ecosystem)
        row.package_ref = purl
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


async def clear_sync_failure(
    db: AsyncSession, *, ref: PackageRef | None = None, package_name: str | None = None, operation: str | None = None
) -> None:
    query = delete(SyncFailure).where(_matches(_resolve_ref(ref, package_name)))
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
