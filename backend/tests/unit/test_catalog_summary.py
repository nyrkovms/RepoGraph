from datetime import UTC, datetime, timedelta

from sqlalchemy.dialects import postgresql

from app.api.v1.packages import PackageSort, _cursor_key_for_sort, deprecated_clause
from app.models.enums import Ecosystem, PackageOrigin
from app.models.package import Package
from app.services.usage import STALE_YEARS, latest_released_before, stale_cutoff, used_by_counts


def _sql(clause) -> str:
    return str(clause.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_stale_cutoff_is_the_start_of_a_day_three_years_back() -> None:
    now = datetime(2026, 11, 7, 10, 3, 55, 123, tzinfo=UTC)
    cutoff = stale_cutoff(now)
    assert STALE_YEARS == 3
    assert cutoff.date() == datetime(2023, 11, 7, tzinfo=UTC).date()
    assert (cutoff.minute, cutoff.second, cutoff.microsecond) == (0, 0, 0)
    assert timedelta(days=1096) <= now - cutoff < timedelta(days=1097)


def test_stale_cutoff_is_stable_through_the_day() -> None:
    morning = stale_cutoff(datetime(2026, 11, 7, 0, 0, 1, tzinfo=UTC))
    evening = stale_cutoff(datetime(2026, 11, 7, 23, 59, 59, tzinfo=UTC))
    assert morning == evening


def test_deprecated_means_neither_sql_null_nor_json_null() -> None:
    sql = _sql(deprecated_clause())
    assert "deprecation IS NOT NULL" in sql and "deprecation != 'null'::jsonb" in sql


def test_used_by_counts_is_one_definition_for_the_sort_and_the_lookup() -> None:
    sql = _sql(used_by_counts().select())
    # Dev and build dependencies and self-edges do not make a dependent.
    assert "'runtime'" in sql and "'peer'" in sql and "'provided'" in sql
    assert "'dev'" not in sql and "'build'" not in sql
    assert "count(distinct" in sql


def test_latest_released_before_looks_at_the_latest_version_only() -> None:
    sql = _sql(latest_released_before(datetime(2023, 1, 1, tzinfo=UTC)))
    assert "package_versions.version = packages.latest_version" in sql and "release_date <" in sql


def test_used_by_cursor_keeps_the_name_for_the_tie_break() -> None:
    package = Package(ecosystem=Ecosystem.npm, name="Left-Pad", normalized_name="left-pad", origin=PackageOrigin.manual)
    assert _cursor_key_for_sort(package, PackageSort.used_by, "name") == "left-pad"
