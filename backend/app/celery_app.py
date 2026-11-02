from celery import Celery
from celery.schedules import crontab

from app.config import get_settings
from app.logging_config import setup_logging

setup_logging()
settings = get_settings()

# Task modules are listed explicitly: autodiscover_tasks(["app.tasks"]) looked for app.tasks.tasks and found nothing (#8).
TASK_MODULES = [
    "app.tasks.crates_api",
    "app.tasks.daily_update",
    "app.tasks.fetch_deps",
    "app.tasks.fetch_packages",
    "app.tasks.release_dates",
    "app.tasks.presence_probe",
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
if settings.presence_probe_enabled:
    # Weekly (SPEC-MULTI-ECOSYSTEM.md §6.6): finds catalog packages in sources they appeared in later.
    beat_schedule["presence-probe"] = {
        "task": "app.tasks.presence_probe.run_presence_probe",
        "schedule": crontab(day_of_week=settings.presence_probe_day_of_week, hour=settings.presence_probe_hour_utc,
                            minute=0),
    }

if settings.crates_api_enabled:
    beat_schedule["crates-api"] = {
        "task": "app.tasks.crates_api.fill_crates_api_fields",
        "schedule": settings.crates_api_interval_seconds,
    }

if settings.release_dates_enabled:
    beat_schedule["release-dates"] = {
        "task": "app.tasks.release_dates.fill_release_dates",
        "schedule": settings.release_dates_interval_seconds,
    }

celery_app.conf.update(
    timezone="UTC",
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    beat_schedule=beat_schedule,
)
