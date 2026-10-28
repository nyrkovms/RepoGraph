from app.tasks.fetch_deps import fetch_missing_dependencies


def test_tasks_run_one_after_another_in_one_worker_process() -> None:
    # Every task runs its own event loop (asyncio.run); a pooled connection of the previous task's loop must not be
    # reused by the next one (#8).
    assert fetch_missing_dependencies("tzdata")["status"] == "completed"
    assert fetch_missing_dependencies("idna")["status"] == "completed"
