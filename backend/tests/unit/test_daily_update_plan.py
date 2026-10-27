from app.tasks.daily_update import TrackedPackage, plan_updates


def _pkg(name: str, last_serial: int | None, unavailable: bool = False) -> TrackedPackage:
    return TrackedPackage(id=name, name=name, last_serial=last_serial, unavailable=unavailable)


def test_plan_updates_selects_only_changed_and_missing() -> None:
    tracked = [
        _pkg("flask", 100),
        _pkg("django", 200),
        _pkg("never-synced", None),
        _pkg("gone", 50),
        _pkg("already-gone", 60, unavailable=True),
        _pkg("back-again", 70, unavailable=True),
    ]
    index = {"flask": 150, "django": 200, "never-synced": 10, "back-again": 70, "not-in-catalog": 999}

    changed, missing = plan_updates(tracked, index)

    assert [(package.name, serial) for package, serial in changed] == [
        ("flask", 150),
        ("never-synced", 10),
        ("back-again", 70),
    ]
    assert [package.name for package in missing] == ["gone"]


def test_plan_updates_ignores_lower_index_serial_from_cached_index() -> None:
    changed, missing = plan_updates([_pkg("flask", 150)], {"flask": 140})

    assert changed == []
    assert missing == []
