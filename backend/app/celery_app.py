from celery import Celery
from celery.signals import worker_process_init

from app.config import get_settings
from app.logging_config import setup_logging

setup_logging()
settings = get_settings()


@worker_process_init.connect
def _use_loop_safe_db_engine(**_kwargs) -> None:
    """Celery tasks run each in a fresh asyncio.run() loop; pooled asyncpg
    connections stay bound to the loop that created them and break the next
    task in the same fork child. NullPool opens/closes connections inside the
    current loop, so worker processes swap the shared engine for a pool-less one.
    """
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    import app.db as db

    db.engine = create_async_engine(settings.async_db_url, poolclass=NullPool)
    db.SessionLocal.configure(bind=db.engine)

celery_app = Celery(
    "packagehub",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "app.tasks.classify",
        "app.tasks.enumeration",
        "app.tasks.fetch_deps",
        "app.tasks.probe",
        "app.tasks.refresh",
        "app.tasks.retry_failed",
        "app.tasks.seed",
    ],
)
celery_app.conf.update(
    timezone="UTC",
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    beat_schedule={
        "batch-refresh": {
            "task": "app.tasks.refresh.run_batch_refresh",
            "schedule": settings.sync_refresh_interval_hours * 3600,
            "kwargs": {"limit": settings.sync_refresh_batch_limit},
        },
        "retry-failed-sync": {
            "task": "app.tasks.retry_failed.run_retry_failed_sync",
            "schedule": settings.sync_failed_retry_interval_seconds,
            "kwargs": {"limit": settings.sync_failed_retry_batch_limit},
        },
        "enumeration": {
            "task": "app.tasks.enumeration.run_enumeration",
            "schedule": settings.enumeration_interval_hours * 3600,
            "kwargs": {},
        },
    },
)

