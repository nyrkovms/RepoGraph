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


def test_package_events_run_id_has_no_foreign_key_until_0007():
    assert not Base.metadata.tables["package_events"].c.run_id.foreign_keys
