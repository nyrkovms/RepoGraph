import logging
from datetime import UTC, datetime
from time import perf_counter

from celery.exceptions import MaxRetriesExceededError
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.celery_app import celery_app
from app.config import get_settings
from app.db import SessionLocal
from app.fetcher.transport import RegistryAuthError, RegistryClient
from app.connectors.factory import registry_info_from_row
from app.models.artifact import Artifact
from app.models.dist_probe import DistProbe
from app.models.package import Package
from app.models.registry import Registry
from app.models.version import PackageVersion
from app.models.version_description import VersionDescription
from app.probe.extract import extract_wheel_facts
from app.probe.extract_npm import extract_tarball_facts
from app.probe.selection import WheelCandidate, select_npm_tarball, select_representative_wheel
from app.services.metrics import inc_fetch_error, observe_probe
from app.services.retry_policy import compute_backoff_delay_seconds, is_retryable_upstream_error
from app.services.sync_failures import upsert_sync_failure
from app.tasks.runner import run_async

logger = logging.getLogger(__name__)


class RetryableProbeError(Exception):
    """Transient network/upstream error: retry the task, do not record a probe row."""


async def _insert_probe_row(
    db,
    *,
    version_id,
    registry_slug: str,
    artifact_filename: str,
    is_representative: bool,
    entry_points_txt: str | None = None,
    top_level_txt: str | None = None,
    file_facts: dict | None = None,
    readme_extracted: bool = False,
    error: str | None = None,
) -> bool:
    stmt = insert(DistProbe).values(
        version_id=version_id,
        registry_slug=registry_slug,
        artifact_filename=artifact_filename,
        is_representative=is_representative,
        fetched_at=datetime.now(UTC),
        entry_points_txt=entry_points_txt,
        top_level_txt=top_level_txt,
        file_facts=file_facts,
        readme_extracted=readme_extracted,
        error=error,
    )
    stmt = stmt.on_conflict_do_nothing(index_elements=["version_id", "artifact_filename"])
    result = await db.execute(stmt)
    await db.commit()
    return bool(result.rowcount)


async def _store_version_description(db, version_id, origin: str, content: str) -> None:
    stmt = insert(VersionDescription).values(
        version_id=version_id,
        origin=origin,
        content=content,
        fetched_at=datetime.now(UTC),
    )
    stmt = stmt.on_conflict_do_nothing(index_elements=["version_id", "origin"])
    await db.execute(stmt)
    await db.commit()


async def _download_wheel(
    client: RegistryClient, url: str, *, max_bytes: int
) -> tuple[bytes | None, str | None]:
    """Returns (payload, permanent_error). Raises RetryableProbeError on transient failures."""
    try:
        response = await client.get(url)
    except RegistryAuthError:
        raise
    except Exception as exc:  # noqa: BLE001
        if is_retryable_upstream_error(exc):
            raise RetryableProbeError(str(exc)) from exc
        return None, f"wheel download failed: {exc.__class__.__name__}"

    if response.status_code == 429 or response.status_code >= 500:
        raise RetryableProbeError(f"upstream status {response.status_code}")
    if response.status_code >= 400:
        return None, f"wheel download failed ({response.status_code})"
    payload = response.content
    if len(payload) > max_bytes:
        return None, "wheel exceeds size limit"
    return payload, None


def _pick_explicit_artifact(artifacts, artifact_filename: str, max_bytes: int) -> tuple[WheelCandidate | None, str | None]:
    row = next((a for a in artifacts if a.filename == artifact_filename), None)
    if row is None:
        return None, f"artifact '{artifact_filename}' not found for version"
    if not (row.filename.endswith(".whl") or row.artifact_type == "tgz"):
        return None, "explicit probe supports wheels and npm tarballs only"
    if (row.size_bytes or 0) > max_bytes:
        return None, "artifact exceeds size limit"
    return WheelCandidate(filename=row.filename, download_url=row.download_url, size_bytes=row.size_bytes), None


async def _probe_version_async(version_id: str, artifact_filename: str | None = None) -> dict[str, str | bool]:
    settings = get_settings()
    if not settings.probe_enabled:
        return {"status": "skipped", "reason": "probe disabled"}
    started = perf_counter()
    is_representative = artifact_filename is None

    async with SessionLocal() as db:
        version = await db.get(PackageVersion, version_id)
        if version is None:
            return {"status": "skipped", "reason": "version not found"}

        if is_representative:
            existing = await db.scalar(
                select(DistProbe).where(DistProbe.version_id == version.id, DistProbe.is_representative.is_(True))
            )
        else:
            existing = await db.scalar(
                select(DistProbe).where(
                    DistProbe.version_id == version.id, DistProbe.artifact_filename == artifact_filename
                )
            )
        if existing is not None:
            return {"status": "skipped", "reason": "already probed"}

        package = await db.get(Package, version.package_id)
        ecosystem = package.ecosystem.value if package else "pypi"
        artifacts = (await db.scalars(select(Artifact).where(Artifact.version_id == version.id))).all()
        max_bytes = settings.probe_max_tarball_bytes if ecosystem == "npm" else settings.probe_max_wheel_bytes

        if is_representative:
            if ecosystem == "npm":
                selection = select_npm_tarball(artifacts, settings.probe_max_tarball_bytes)
            else:
                selection = select_representative_wheel(artifacts, settings.probe_max_wheel_bytes)
            candidate, permanent_error = selection.candidate, selection.error
        else:
            candidate, permanent_error = _pick_explicit_artifact(artifacts, artifact_filename, max_bytes)

        if permanent_error:
            inserted = await _insert_probe_row(
                db,
                version_id=version.id,
                registry_slug="",
                artifact_filename=artifact_filename or "",
                is_representative=is_representative,
                error=permanent_error,
            )
            observe_probe("", "unusable", perf_counter() - started)
            return {"status": "completed", "outcome": "unusable", "error": permanent_error, "inserted": inserted}

        artifact_row = next(a for a in artifacts if a.filename == candidate.filename)
        registry_row = await db.get(Registry, artifact_row.registry_id)
        registry_slug = registry_row.slug if registry_row else ""
        client = RegistryClient(
            registry=registry_info_from_row(registry_row),
            timeout_seconds=settings.probe_http_timeout_seconds,
        )

        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        lock_key = f"probe:{version.id}:{candidate.filename}"
        try:
            acquired = await redis.set(lock_key, "1", nx=True, ex=settings.probe_lock_ttl_seconds)
            if not acquired:
                observe_probe(registry_slug, "skipped", perf_counter() - started)
                return {"status": "skipped", "reason": "locked"}

            try:
                payload, permanent_error = await _download_wheel(
                    client, candidate.download_url, max_bytes=max_bytes
                )
            except RegistryAuthError as exc:
                # Stale credentials are an operator problem, not a fact about
                # the distribution: no probe row, no retry queue — log + metric.
                logger.error("Probe auth error: registry=%s version_id=%s error=%s", registry_slug, version_id, exc)
                inc_fetch_error(registry_slug, "auth")
                observe_probe(registry_slug, "failed", perf_counter() - started)
                return {"status": "failed", "reason": "registry auth error"}

            if permanent_error:
                inserted = await _insert_probe_row(
                    db,
                    version_id=version.id,
                    registry_slug=registry_slug,
                    artifact_filename=candidate.filename,
                    is_representative=is_representative,
                    error=permanent_error,
                )
                observe_probe(registry_slug, "unusable", perf_counter() - started)
                return {"status": "completed", "outcome": "unusable", "error": permanent_error, "inserted": inserted}

            if ecosystem == "npm":
                tarball = extract_tarball_facts(payload)
                readme_extracted = False
                if tarball.readme:
                    await _store_version_description(db, version.id, "tarball-readme", tarball.readme)
                    readme_extracted = True
                inserted = await _insert_probe_row(
                    db,
                    version_id=version.id,
                    registry_slug=registry_slug,
                    artifact_filename=candidate.filename,
                    is_representative=is_representative,
                    file_facts=tarball.file_facts,
                    readme_extracted=readme_extracted,
                    error=tarball.error,
                )
                outcome = "unusable" if tarball.error else "probed"
                observe_probe(registry_slug, outcome, perf_counter() - started)
                return {"status": "completed", "outcome": outcome, "inserted": inserted}

            facts = extract_wheel_facts(payload, candidate.filename)
            readme_extracted = False
            if facts.metadata_description:
                await _store_version_description(db, version.id, "wheel-metadata", facts.metadata_description)
                readme_extracted = True
            inserted = await _insert_probe_row(
                db,
                version_id=version.id,
                registry_slug=registry_slug,
                artifact_filename=candidate.filename,
                is_representative=is_representative,
                entry_points_txt=facts.entry_points_txt,
                top_level_txt=facts.top_level_txt,
                file_facts=facts.file_facts,
                readme_extracted=readme_extracted,
                error=facts.error,
            )
            outcome = "unusable" if facts.error else "probed"
            observe_probe(registry_slug, outcome, perf_counter() - started)
            return {"status": "completed", "outcome": outcome, "inserted": inserted}
        finally:
            try:
                await redis.delete(lock_key)
            finally:
                await redis.aclose()


async def _record_probe_failure(version_id: str, exc: Exception) -> None:
    async with SessionLocal() as db:
        version = await db.get(PackageVersion, version_id)
        if version is None:
            return
        await upsert_sync_failure(
            db,
            operation="probe",
            package_name=str(version_id),
            exc=exc,
        )
        await db.commit()


@celery_app.task(name="app.tasks.probe.probe_version", bind=True, max_retries=3)
def probe_version(self, version_id: str, artifact_filename: str | None = None) -> dict[str, str | bool]:
    settings = get_settings()
    try:
        return run_async(_probe_version_async(version_id, artifact_filename))
    except RetryableProbeError as exc:
        delay = compute_backoff_delay_seconds(
            attempt=self.request.retries + 1,
            base_seconds=settings.sync_http_retry_base_seconds,
            max_seconds=settings.sync_http_retry_max_seconds,
            jitter_seconds=settings.sync_http_retry_jitter_seconds,
        )
        try:
            raise self.retry(exc=exc, countdown=delay)
        except MaxRetriesExceededError:
            logger.warning("Probe retries exhausted: version_id=%s error=%s", version_id, exc)
            run_async(_record_probe_failure(version_id, exc))
            return {"status": "failed", "reason": str(exc)}
