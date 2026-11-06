"""Repair stored release events (#56, #58)

Events written before those fixes measured a release against the version walked before it, so a backport became the
reference of the current line, and reported every version of a newly added source as a new release. The migration
replays `release_added` events package by package under the current rule (`diff_package`) and marks the versions that
arrived with a new source as `backfill`.

Only `details` of `release_added` rows changes. It is the one migration that imports application code: the bump rule
lives in seven version schemes, too much to copy here. The import happens only when there are events to repair, so a
fresh database never reaches it. The downgrade keeps the repaired values: the old ones were wrong, and old code ignores
the `backfill` key.

Revision ID: 20261106_0014
Revises: 20261031_0013
Create Date: 2026-11-06
"""

import json
from bisect import bisect_left, insort
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261106_0014"
down_revision: Union[str, Sequence[str], None] = "20261031_0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# A release of an unknown run whose every source was added to the package as a new source.
_BACKFILL = sa.text("""
    SELECT e.id
    FROM package_events e
    WHERE e.kind = 'release_added'
      AND e.run_id IS NULL
      AND EXISTS (SELECT 1 FROM package_versions v
                  JOIN version_sources s ON s.version_id = v.id
                  WHERE v.package_id = e.package_id AND v.version = e.version)
      AND NOT EXISTS (SELECT 1 FROM package_versions v
                      JOIN version_sources s ON s.version_id = v.id
                      JOIN registries r ON r.id = s.registry_id
                      WHERE v.package_id = e.package_id AND v.version = e.version
                        AND r.slug NOT IN (SELECT a.details ->> 'registry' FROM package_events a
                                           WHERE a.package_id = e.package_id AND a.kind = 'source_added'
                                             AND a.version IS NULL))
""")

_EVENTS = sa.text("""
    SELECT e.id, e.package_id, e.version, e.details, p.ecosystem::text AS ecosystem
    FROM package_events e
    JOIN packages p ON p.id = e.package_id
    WHERE e.kind = 'release_added' AND e.version IS NOT NULL
    ORDER BY e.package_id, e.id
""")

_VERSIONS = sa.text("""
    SELECT version, sort_key, is_prerelease FROM package_versions WHERE package_id = :package_id
""")


@dataclass(slots=True)
class StoredEvent:
    id: int
    version: str
    details: dict[str, Any]


def replay(
    events: Sequence[StoredEvent],
    versions: Mapping[str, tuple[bytes, bool]],
    bump: Callable[[str | None, str], str | None],
    backfill: Collection[int] = (),
) -> dict[int, dict[str, Any]]:
    """The `details` that change, by event id. `events` are one package's, oldest first; `versions` maps every version
    of it to `(sort_key, is_prerelease)`. A version an event introduced is unknown until that event, the same rule as
    `diff_package`; with nothing lower known, the stored `previous_version` stays the reference."""
    introduced = {event.version for event in events}
    known = sorted((key, name) for name, (key, _) in versions.items() if name not in introduced)
    stable = [(key, name) for key, name in known if not versions[name][1]]

    changed: dict[int, dict[str, Any]] = {}
    for event in events:
        if event.version not in versions:
            continue
        key, prerelease = versions[event.version]
        position = (key, "")
        lower = bisect_left(known, position)
        lower_stable = bisect_left(stable, position)
        stored = event.details.get("previous_version")
        details = {
            **event.details,
            "previous_version": known[lower - 1][1] if lower else stored,
            "bump": bump(stable[lower_stable - 1][1] if lower_stable else stored, event.version),
            **({"backfill": True} if event.id in backfill else {}),
        }
        if details != event.details:
            changed[event.id] = details
        insort(known, (key, event.version))
        if not prerelease:
            insort(stable, (key, event.version))
    return changed


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(_EVENTS).mappings().all()
    if not rows:
        return

    from app.ecosystems.registry import get_plugin

    backfill = {row[0] for row in bind.execute(_BACKFILL)}
    by_package: dict[Any, list[Any]] = {}
    for row in rows:
        by_package.setdefault(row["package_id"], []).append(row)

    changed: dict[int, dict[str, Any]] = {}
    for package_id, package_rows in by_package.items():
        versions = {row.version: (bytes(row.sort_key), row.is_prerelease)
                    for row in bind.execute(_VERSIONS, {"package_id": package_id})}
        events = [StoredEvent(row["id"], row["version"], row["details"]) for row in package_rows]
        changed.update(replay(events, versions, get_plugin(package_rows[0]["ecosystem"]).versions.bump, backfill))

    if changed:
        bind.execute(sa.text("UPDATE package_events SET details = CAST(:details AS jsonb) WHERE id = :id"),
                     [{"id": event_id, "details": json.dumps(details)} for event_id, details in changed.items()])


def downgrade() -> None:
    pass
