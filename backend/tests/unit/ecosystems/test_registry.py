import pytest

from app.ecosystems import registry
from app.ecosystems.registry import UnknownEcosystem, get_plugin, register
from app.ecosystems.schemas import Engines, PythonEnvironmentMarkers


class _FakePlugin:
    ecosystem = "go"


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    monkeypatch.setattr(registry, "_PLUGINS", {})


def test_register_and_get() -> None:
    plugin = _FakePlugin()
    register(plugin)  # type: ignore[arg-type]

    assert get_plugin("go") is plugin
    assert registry.registered() == [plugin]


def test_duplicate_registration_is_rejected() -> None:
    register(_FakePlugin())  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        register(_FakePlugin())  # type: ignore[arg-type]


def test_unknown_ecosystem() -> None:
    with pytest.raises(UnknownEcosystem):
        get_plugin("rpm")


def test_jsonb_schemas_reject_unknown_fields() -> None:
    markers = {"type": "python_environment", "expression": 'python_version < "3.10"',
               "conditions": [{"marker": "python_version", "op": "<", "value": "3.10"}]}
    assert PythonEnvironmentMarkers.model_validate(markers).conditions[0].value == "3.10"

    with pytest.raises(ValueError):
        PythonEnvironmentMarkers.model_validate({**markers, "unexpected": 1})

    assert Engines.model_validate({"python": ">=3.9"}).python == ">=3.9"
