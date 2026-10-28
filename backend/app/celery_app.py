from celery import Celery
from celery.schedules import crontab

from app.config import get_settings
from app.logging_config import setup_logging

setup_logging()
settings = get_settings()

# Task modules are listed explicitly: autodiscover_tasks(["app.tasks"]) looked for app.tasks.tasks and found nothing (#8).
TASK_MODULES = [
    "app.tasks.daily_update",
    "app.tasks.fetch_deps",
    "app.tasks.refresh",
    "app.tasks.retry_failed",
    "app.tasks.seed",
]
celery_app = Celery("packagehub", broker=settings.redis_url, backend=settings.redis_url, include=TASK_MODULES)
beat_schedule = {
    "retry-failed-sync": {
        "task": "app.tasks.retry_failed.run_retry_failed_sync",
        "schedule": settings.sync_failed_retry_interval_seconds,
        "kwargs": {"limit": settings.sync_failed_retry_batch_limit},
    },
}
if settings.daily_update_enabled:
    beat_schedule["daily-update"] = {
        "task": "app.tasks.daily_update.run_daily_update",
        "schedule": crontab(hour=settings.daily_update_hour_utc, minute=settings.daily_update_minute_utc),
    }
if settings.sync_refresh_enabled:
    beat_schedule["batch-refresh"] = {
        "task": "app.tasks.refresh.run_batch_refresh",
        "schedule": settings.sync_refresh_interval_hours * 3600,
        "kwargs": {"limit": settings.sync_refresh_batch_limit},
    }

celery_app.conf.update(
    timezone="UTC",
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    beat_schedule=beat_schedule,
)
