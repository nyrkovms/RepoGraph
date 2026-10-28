import subprocess
import sys
from pathlib import Path

EXPECTED = {
    "app.tasks.daily_update.run_daily_update",
    "app.tasks.fetch_deps.fetch_missing_dependencies",
    "app.tasks.refresh.run_batch_refresh",
    "app.tasks.retry_failed.run_retry_failed_sync",
    "app.tasks.seed.run_seed",
}

# What a fresh worker process registers: in this process other tests may have imported the task modules already (#8).
_WORKER_VIEW = """
from app.celery_app import celery_app
celery_app.loader.import_default_modules()
print("\\n".join(celery_app.tasks))
print("--")
print("\\n".join(entry["task"] for entry in celery_app.conf.beat_schedule.values()))
"""


def test_worker_registers_every_task_and_every_scheduled_job() -> None:
    out = subprocess.run([sys.executable, "-c", _WORKER_VIEW], cwd=Path(__file__).parents[2], check=True,
                         capture_output=True, text=True).stdout
    registered, scheduled = (set(part.split()) for part in out.split("--\n"))
    assert EXPECTED <= registered
    assert scheduled <= registered
