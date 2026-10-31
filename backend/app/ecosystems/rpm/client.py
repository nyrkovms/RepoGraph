"""Calls into rpm-worker (SPEC-MULTI-ECOSYSTEM.md §3.2): Celery tasks on the `rpm` queue, awaited off the event loop."""

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from app.ecosystems.base import RegistryConfig

TIMEOUT_S = 600.0
DETECT_TIMEOUT_S = 3600.0
# The daily work has its own queue and worker in rpm-worker: it never waits in front of what a person waits on.
_QUEUES = {"rpm.detect_changes": "rpm-batch", "rpm.sync_repo": "rpm-batch"}


class WorkerError(httpx.TransportError):
    """rpm-worker failed the task (a source error: authorization, network, broken repodata) or did not answer. An
    httpx transport error, so discovery and the daily update record it as the source failing, like a network error
    of any other adapter."""


def source_payload(registry: RegistryConfig) -> dict[str, Any]:
    return {"id": registry.id, "slug": registry.slug, "base_url": registry.base_url, "auth_type": registry.auth_type,
            "credentials_ref": registry.credentials_ref, "config": dict(registry.config)}


async def call(task: str, *args: Any, timeout: float = TIMEOUT_S) -> Any:
    from app.celery_app import celery_app      # the app's broker; imported late so plugins load without Celery

    def run() -> Any:
        result = celery_app.send_task(task, args=list(args), queue=_QUEUES.get(task, "rpm"))
        try:
            # Inside the backend's own Celery tasks (daily update, fetch_packages, seed) Celery forbids waiting on
            # another task; this one runs elsewhere (rpm-worker), so waiting cannot deadlock the pool.
            return result.get(timeout=timeout, propagate=True, disable_sync_subtasks=False)
        except Exception as exc:  # noqa: BLE001 - every failure of the worker is a source error to the caller
            raise WorkerError(f"{task}: {type(exc).__name__}: {exc}") from exc
        finally:
            result.forget()

    return await asyncio.to_thread(run)


async def fetch_package(registry: RegistryConfig, name: str) -> Mapping[str, Any] | None:
    result = await call("rpm.fetch_package", source_payload(registry), name)
    return result if isinstance(result, dict) else None


async def detect_changes(registry: RegistryConfig, tracked: Mapping[str, str | None],
                         known_revisions: Mapping[str, str] | None = None) -> Mapping[str, Any]:
    result = await call("rpm.detect_changes", source_payload(registry), dict(tracked), dict(known_revisions or {}),
                        timeout=DETECT_TIMEOUT_S)
    return result if isinstance(result, dict) else {}


async def whatprovides(registries: Sequence[RegistryConfig], arch: str, capability: str) -> list[Mapping[str, Any]]:
    result = await call("rpm.whatprovides", [source_payload(r) for r in registries], arch, capability)
    return list(result) if isinstance(result, list) else []


async def test_connection(registry: RegistryConfig) -> Mapping[str, Any]:
    result = await call("rpm.test_connection", source_payload(registry), timeout=120.0)
    return result if isinstance(result, dict) else {}
