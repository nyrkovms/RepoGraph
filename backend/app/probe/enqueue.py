"""Enqueue probe of the representative distribution after a successful sync.

Kept import-light: the Celery task is imported lazily so the sync pipeline
does not depend on the worker at import time, and a broker outage degrades
to a logged warning instead of failing the sync.
"""

import logging

from app.config import get_settings

logger = logging.getLogger(__name__)


def schedule_latest_probe(latest_version: str, version_id_by_version: dict[str, str]) -> None:
    settings = get_settings()
    if not settings.probe_enabled:
        return
    version_id = version_id_by_version.get(latest_version)
    if version_id is None:
        return
    try:
        from app.tasks.probe import probe_version

        probe_version.delay(str(version_id))
    except Exception:  # noqa: BLE001
        logger.warning("Failed to enqueue probe: version_id=%s", version_id, exc_info=True)
