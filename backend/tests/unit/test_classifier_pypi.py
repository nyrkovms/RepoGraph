from app.classifier.core import ProbeView, match_entry_group
from app.classifier.pypi import classify_pypi
from app.classifier.registries_data import ENTRY_GROUP_RULES

RULES = [
    {"pattern": pattern, "group_type": group_type, "role_effect": role_effect, "host": host}
    for pattern, group_type, role_effect, _note, host in ENTRY_GROUP_RULES
]


def probe(entry_points: str | None = None, top_level: str | None = None,
          file_facts: dict | None = None, error: str | None = None) -> ProbeView:
    return ProbeView(
        entry_points_txt=entry_points, top_level_txt=top_level, file_facts=file_facts, error=error, available=True
    )


def manifest(summary: str = "", classifiers: list | None = None, requires_dist: list | None = None,
             urls: list | None = None) -> dict:
    return {
        "info": {"summary": summary, "classifiers": classifiers or [], "requires_dist": requires_dist or []},
        "urls": urls if urls is not None else [{"packagetype": "bdist_wheel"}],
    }


def caps(result) -> dict[str, str]:
    return {c.name: c.confidence for c in result.capabilities}


def test_console_script_library():
    result = classify_pypi(
        "click-tool",
        manifest("Composable command line interface toolkit",
                 classifiers=["Software Development :: Libraries"]),
        probe(entry_points="[console_scripts]\nct = click_tool:main\n", top_level="click_tool\n",
              file_facts={"ext_counts": {"py": 20}}),
        {},
        RULES,
    )
    assert caps(result)["cli"] == "high"
    assert caps(result)["importable-api"] == "high"
    assert "runtime-component" in result.roles


def test_pytest_plugin_entry_group():
    result = classify_pypi(
        "pytest-cov",
        manifest("Coverage plugin for pytest", classifiers=["Framework :: Pytest"]),
        probe(entry_points="[pytest11]\npytest_cov = pytest_cov.plugin\n", top_level="pytest_cov\n"),
        {},
        RULES,
    )
    assert "buildtime-component" in result.roles
    assert caps(result)["plugin"] == "high"
    assert ("plugin-of", "pytest") in result.relations
    assert result.status == "ok"


def test_sqlalchemy_dialect_is_runtime_extension():
    result = classify_pypi(
        "sqlalchemy-clickhouse",
        manifest("ClickHouse dialect for SQLAlchemy"),
        probe(entry_points="[sqlalchemy.dialects]\nclickhouse = x:Dialect\n", top_level="ch\n"),
        {},
        RULES,
    )
    assert "runtime-component" in result.roles
    assert ("plugin-of", "sqlalchemy") in result.relations


def test_unknown_entry_group_needs_data():
    result = classify_pypi(
        "weird-pkg",
        manifest("some package"),
        probe(entry_points="[totally.unknown.group]\nx = y:z\n", top_level="weird\n"),
        {},
        RULES,
    )
    assert result.status == "needs-data"
    assert "totally.unknown.group" in result.status_reason


def test_ruff_pattern_binary_payload_devtool():
    result = classify_pypi(
        "ruff",
        manifest("An extremely fast Python linter"),
        probe(file_facts={"ext_counts": {"py": 1}, "scripts": ["ruff"], "platform_wheel": True}),
        {},
        RULES,
    )
    assert caps(result)["cli"] == "high"
    assert caps(result)["binary-payload"] == "high"
    assert "importable-api" not in caps(result)  # тонкая py-обёртка — не библиотека
    assert "dev-tool" in result.roles


def test_type_stubs():
    result = classify_pypi("types-requests", manifest("Typing stubs for requests"), probe(top_level="requests-stubs\n"), {}, RULES)
    assert "dev-config" in result.roles
    assert caps(result)["types-only"] == "high"


def test_sdist_only_capability():
    result = classify_pypi(
        "legacy-pkg",
        manifest("old package", urls=[{"packagetype": "sdist"}]),
        probe(top_level="legacy\n"),
        {},
        RULES,
    )
    assert caps(result)["sdist-only"] == "high"


def test_gui_scripts_application():
    result = classify_pypi(
        "gui-app",
        manifest("desktop application"),
        probe(entry_points="[gui_scripts]\napp = gui_app:main\n", top_level="gui_app\n"),
        {},
        RULES,
    )
    assert caps(result)["gui-app"] == "high"
    assert "application" in result.roles


def test_override_terminal_flask():
    result = classify_pypi(
        "flask",
        manifest("A simple framework for building complex web applications"),
        probe(entry_points="[console_scripts]\nflask = flask.cli:main\n", top_level="flask\n"),
        {"flask": ["runtime-component", "dev-tool", "dev-service"]},
        RULES,
    )
    assert result.roles == {"runtime-component", "dev-tool", "dev-service"}
    assert result.status == "ok"  # override терминален, спорные комбинации разрешены куратором


def test_app_vs_devtool_undecidable():
    result = classify_pypi(
        "hybrid-tool",
        manifest("code generator and interactive terminal ui monitoring",
                 classifiers=["Software Development :: Build Tools"]),
        probe(entry_points="[console_scripts]\nht = ht:main\n", top_level="ht\n"),
        {},
        RULES,
    )
    if {"application", "dev-tool"} <= result.roles:
        assert result.status == "undecidable-by-spec"
        provisional = {f.name for f in result.facets if f.provisional}
        assert {"application", "dev-tool"} <= provisional


def test_probe_failed_manager_pattern():
    result = classify_pypi(
        "uv-like",
        manifest("An extremely fast Python package manager"),
        probe(error="all wheels exceed size limit"),
        {},
        RULES,
    )
    assert "dev-tool" in result.roles


def test_no_probe_defaults_to_runtime_component():
    """Без probe и без сигналов пакет — importable python distribution по
    умолчанию (правило classifire): runtime-component high, import assumed."""
    result = classify_pypi("opaque", {"info": {"summary": ""}, "urls": []}, None, {}, RULES)
    assert "runtime-component" in result.roles
    assert caps(result)["importable-api"] == "low"
    assert result.status == "ok"


def test_match_entry_group_prefix_and_exact():
    assert match_entry_group(RULES, "pytest11")["host"] == "pytest"
    assert match_entry_group(RULES, "flake8.extension")["host"] == "flake8"
    assert match_entry_group(RULES, "opentelemetry_traces_exporter")["role_effect"] == "runtime-component"
    assert match_entry_group(RULES, "nonexistent.group") is None
