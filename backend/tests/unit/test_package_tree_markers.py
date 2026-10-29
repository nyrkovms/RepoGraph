from types import SimpleNamespace

from app.api.v1.package_detail import _build_marker_environment, _dependency_sort_key, _tree_marker_applies
from app.models.enums import DependencyType


def test_tree_markers_include_extra_only_dependencies_when_optional_enabled() -> None:
    markers = {
        "expression": 'extra == "grpc"',
        "conditions": [{"marker": "extra", "op": "==", "value": "grpc"}],
    }

    assert _tree_marker_applies(markers, _build_marker_environment("3.12", "linux"), include_optional=True) is True
    assert _tree_marker_applies(markers, _build_marker_environment("3.12", "linux"), include_optional=False) is False


def test_tree_markers_still_apply_non_extra_conditions_when_optional_enabled() -> None:
    markers = {
        "expression": 'extra == "argon2" and platform_system == "Linux" and platform_machine == "x86_64"',
        "conditions": [
            {"marker": "extra", "op": "==", "value": "argon2"},
            {"marker": "platform_system", "op": "==", "value": "Linux"},
            {"marker": "platform_machine", "op": "==", "value": "x86_64"},
        ],
    }

    assert _tree_marker_applies(markers, _build_marker_environment("3.12", "linux"), include_optional=True) is True
    assert _tree_marker_applies(markers, _build_marker_environment("3.12", "windows"), include_optional=True) is False


def test_tree_markers_skip_extra_conditions_when_optional_disabled_for_child_levels() -> None:
    markers = {
        "expression": 'extra == "css"',
        "conditions": [{"marker": "extra", "op": "==", "value": "css"}],
    }

    assert _tree_marker_applies(markers, _build_marker_environment("3.12", "linux"), include_optional=False) is False


def test_dependency_sort_key_places_extra_dependencies_after_plain_runtime() -> None:
    plain = SimpleNamespace(
        dependency_name="webencodings",
        dependency_type=DependencyType.runtime,
        extra_name=None,
        environment_markers=None,
    )
    extra_runtime = SimpleNamespace(
        dependency_name="tinycss2",
        dependency_type=DependencyType.runtime,
        extra_name=None,
        environment_markers={
            "conditions": [{"marker": "extra", "op": "==", "value": "css"}],
            "expression": 'extra == "css"',
        },
    )
    optional = SimpleNamespace(
        dependency_name="sqlalchemy",
        dependency_type=DependencyType.optional,
        extra_name="postgresql",
        environment_markers={
            "conditions": [{"marker": "extra", "op": "==", "value": "postgresql"}],
            "expression": 'extra == "postgresql"',
        },
    )

    ordered = sorted([optional, extra_runtime, plain], key=_dependency_sort_key)

    assert [dep.dependency_name for dep in ordered] == ["webencodings", "tinycss2", "sqlalchemy"]
