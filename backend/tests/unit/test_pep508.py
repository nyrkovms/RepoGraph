import pytest

from app.ecosystems.pypi.pep508 import parse_pep508


@pytest.mark.parametrize(
    ("spec", "dependency_type", "extra_name"),
    [
        ("zipp>=3.20", "runtime", None),
        ('importlib-metadata>=3.6; python_version < "3.10"', "runtime", None),
        # Extras requested from the dependency do not make it optional.
        ("requests[socks]>=2.0", "runtime", None),
        ('pytest>=6; extra == "test"', "optional", "test"),
        ("sphinx; extra == 'doc'", "optional", "doc"),
        ('pytest-cov; python_version >= "3.9" and extra == "Test_Suite"', "optional", "test-suite"),
        ('coverage[toml]; extra == "test"', "optional", "test"),
        ('mypy; extra == "check" or extra == "all"', "optional", "check"),
    ],
)
def test_parse_pep508_takes_extra_from_marker(spec: str, dependency_type: str, extra_name: str | None) -> None:
    parsed = parse_pep508(spec)

    assert parsed.dependency_type == dependency_type
    assert parsed.extra_name == extra_name
    assert parsed.raw_spec == spec


def test_parse_pep508_keeps_marker_conditions() -> None:
    parsed = parse_pep508('pytest>=6; python_version < "3.12" and extra == "test"')

    assert parsed.dependency_name == "pytest"
    assert parsed.version_constraint == ">=6"
    assert parsed.environment_markers["conditions"] == [
        {"marker": "python_version", "op": "<", "value": "3.12"},
        {"marker": "extra", "op": "==", "value": "test"},
    ]


def test_parse_pep508_rejects_invalid_requirement() -> None:
    with pytest.raises(ValueError):
        parse_pep508("not a valid requirement ;;")
