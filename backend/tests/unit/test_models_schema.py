"""Metadata-only checks of the models; the full comparison with the database is `alembic check`."""

from sqlalchemy import Enum

from app.models import Base
from app.models.enums import DependenciesState, PackageOrigin, RegistryProtocol
from app.models.registry import Registry


def _enum_labels(table: str, column: str) -> list[str]:
    enum_type = Base.metadata.tables[table].c[column].type
    assert isinstance(enum_type, Enum)
    return list(enum_type.enums)


def test_tables_of_migration_0006_are_declared():
    assert {"version_sources", "source_profiles", "source_profile_registries", "source_documents", "runs"} <= set(
        Base.metadata.tables
    )


def test_registry_protocol_is_stored_by_value():
    assert _enum_labels("registries", "protocol") == [
        "pypi-warehouse",
        "pypi-simple",
        "npm-registry",
        "maven2",
        "nuget-v3",
        "goproxy",
        "cargo-sparse",
        "rpm-repodata",
    ]
    assert Registry.protocol.type.name == "registry_protocol_enum"
    assert [m.value for m in RegistryProtocol] == _enum_labels("registries", "protocol")


def test_enum_columns_use_database_type_names_and_values():
    assert _enum_labels("packages", "origin") == [m.value for m in PackageOrigin]
    assert _enum_labels("package_versions", "dependencies_state") == [m.value for m in DependenciesState]
    assert "rpm" in _enum_labels("packages", "ecosystem")
    assert "superseded" in _enum_labels("package_versions", "status")
    assert {"release_deprecated", "source_added"} <= set(_enum_labels("package_events", "kind"))


def test_package_registries_has_composite_ecosystem_keys():
    table = Base.metadata.tables["package_registries"]
    pairs = {
        (tuple(c.name for c in fk.columns), fk.referred_table.name)
        for fk in table.foreign_key_constraints
        if len(fk.columns) == 2
    }
    assert pairs == {(("package_id", "ecosystem"), "packages"), (("registry_id", "ecosystem"), "registries")}


def test_package_events_run_id_points_to_runs():
    assert {fk.column.table.name for fk in Base.metadata.tables["package_events"].c.run_id.foreign_keys} == {"runs"}


# Columns and tables migration 0007 drops (IMPL §2). The models are what the code reads and writes, so none of
# them may be declared here; `alembic check` compares the models with the migrated database.
DROPPED_BY_0007 = {
    "packages": {"deprecated", "deprecation_message", "deprecation_alternative", "last_serial"},
    "package_registries": {"is_primary"},
    "package_versions": {"requires_python", "needed_in_index"},
    "artifacts": {"requires_dist_metadata"},
    "dist_tags": {"id"},
    "registries": {"tier", "registry_type", "api_url", "url_pattern", "auth_required", "auth_config", "sync_enabled",
                   "sync_priority"},
    "sync_states": {"sync_type", "packages_synced", "packages_total"},
    "sync_failures": {"package_name"},
}


def test_models_do_not_declare_what_0007_drops():
    assert "update_runs" not in Base.metadata.tables
    declared = {table: set(Base.metadata.tables[table].c.keys()) & columns for table, columns in DROPPED_BY_0007.items()}
    assert declared == {table: set() for table in DROPPED_BY_0007}


def test_columns_0007_makes_required():
    tables = Base.metadata.tables
    required = [("packages", "namespace"), ("package_registries", "ecosystem"), ("package_versions", "sort_key"),
                ("package_versions", "is_prerelease"), ("dependencies", "dependency_namespace"), ("registries", "protocol"),
                ("sync_failures", "ecosystem"), ("sync_failures", "package_ref")]
    assert [(t, c) for t, c in required if tables[t].c[c].nullable] == []
