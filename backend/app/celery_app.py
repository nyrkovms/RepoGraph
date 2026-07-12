from celery import Celery

from app.config import get_settings
from app.logging_config import setup_logging

setup_logging()
settings = get_settings()

celery_app = Celery("packagehub", broker=settings.redis_url, backend=settings.redis_url)
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
        }
    },
)

celery_app.autodiscover_tasks(["app.tasks"])
